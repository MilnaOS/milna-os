"""
CorpusCollector — Milna scrape mode tool.

Two async fetch workers crawl a target site, a cheap parser leg strips each
page to clean text, and an append-only numbered corpus file grows on disk.

The corpus is raw stockpile — no schema, no hierarchy.  Each chunk is:

    [NNNN] SOURCE:<url>
    <clean text>

    ---

Milna's build-mode distillation (specialist_round + PersonaCurator) reads
the corpus later to produce the DOT.  These two phases are deliberately
decoupled: collect first, think later.

Usage:
    collector = CorpusCollector(engine, corpus_path=Path('kb/starfinder/corpus.txt'))
    await collector.collect(seed_urls=[...], max_pages=500)
"""
import asyncio
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Safe console print — replaces characters the terminal codec can't handle
# rather than raising UnicodeEncodeError (common on Windows cp1252 consoles).
def _cprint(msg: str) -> None:
    enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    sys.stdout.buffer.write(msg.encode(enc, errors='replace') + b'\n')
    sys.stdout.buffer.flush()

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


# ── Parser leg system prompt ──────────────────────────────────────────────────

_PARSER_SYSTEM = """\
You are a content extractor for a rules knowledge base.

You receive raw text scraped from a tabletop RPG rules website. Your job:
1. Remove all navigation, menus, ads, footers, breadcrumbs, and UI chrome.
2. Keep ALL rules content: mechanics, stats, descriptions, tables, examples.
3. Preserve structure using plain text — use dashes for tables, blank lines for sections.
4. Do NOT summarize, paraphrase, or add commentary.
5. Output ONLY the cleaned rules text. No preamble, no "Here is the content:".

If the page contains no rules content (error page, login wall, empty page), output exactly:
SKIP
"""


# ── CorpusWriter ──────────────────────────────────────────────────────────────

class CorpusWriter:
    """Append-only numbered chunk writer with persistent URL tracking."""

    def __init__(self, corpus_path: Path):
        self.path = corpus_path
        self.visited_path = corpus_path.with_suffix('.visited')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._counter = self._count_existing()
        self._visited_urls: set[str] = self._load_visited()

    def _count_existing(self) -> int:
        if not self.path.exists():
            return 0
        text = self.path.read_text(encoding='utf-8', errors='ignore')
        matches = re.findall(r'^\[(\d+)\]', text, re.MULTILINE)
        return max((int(m) for m in matches), default=0)

    def _load_visited(self) -> set[str]:
        """Load previously-scraped URLs from sidecar file."""
        if not self.visited_path.exists():
            return set()
        return set(self.visited_path.read_text(encoding='utf-8').splitlines())

    def already_visited(self, url: str) -> bool:
        return url in self._visited_urls

    def visited_urls(self) -> set[str]:
        return set(self._visited_urls)

    async def write(self, url: str, text: str) -> int:
        async with self._lock:
            self._counter += 1
            chunk = f'[{self._counter:04d}] SOURCE:{url}\n{text.strip()}\n\n---\n\n'
            with self.path.open('a', encoding='utf-8') as f:
                f.write(chunk)
            # Persist URL so re-runs skip it
            self._visited_urls.add(url)
            with self.visited_path.open('a', encoding='utf-8') as f:
                f.write(url + '\n')
            return self._counter


# ── URL discovery ─────────────────────────────────────────────────────────────

def _parse_html(html: str):
    """Parse HTML once; returns a BeautifulSoup object or None."""
    if BeautifulSoup is None:
        return None
    return BeautifulSoup(html, 'html.parser')


def _extract_links(soup, base_url: str, allowed_domain: str) -> list[str]:
    """Pull same-domain links from a parsed soup tree."""
    if soup is None:
        return []
    links = []
    for tag in soup.find_all('a', href=True):
        href = tag['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc == allowed_domain and parsed.scheme in ('http', 'https'):
            clean = parsed._replace(fragment='').geturl().rstrip('/')
            links.append(clean)
    return list(set(links))


def _html_to_raw_text(html: str, soup=None) -> str:
    """Strip HTML tags — uses pre-parsed soup if available."""
    if soup is not None:
        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        return soup.get_text(separator='\n')
    if BeautifulSoup is not None:
        s = BeautifulSoup(html, 'html.parser')
        for tag in s(['script', 'style', 'nav', 'header', 'footer']):
            tag.decompose()
        return s.get_text(separator='\n')
    return re.sub(r'<[^>]+>', ' ', html)


# ── CorpusCollector ───────────────────────────────────────────────────────────

class CorpusCollector:
    def __init__(
        self,
        engine,
        corpus_path: Path,
        parser_leg: 'str | list[str]' = 'anthropic',
        concurrency: int = 2,
        delay_between_requests: float = 1.0,
        max_raw_chars: int = 40_000,
        url_filter: 'callable | None' = None,
    ):
        """
        engine              : ConversationEngine — used for parser leg calls
        corpus_path         : file path for the flat numbered corpus
        parser_leg          : leg name (str) or list of leg names to round-robin
                              across workers. Worker i uses legs[i % len(legs)].
        concurrency         : parallel fetch workers (2 = polite default)
        delay_between_requests: seconds between fetches per worker
        max_raw_chars       : cap raw text sent to parser leg (cost control)
        url_filter          : optional callable(url: str) -> bool
                              if provided, only URLs returning True are fetched
                              and enqueued for link discovery. Seed URLs bypass
                              this filter so the crawl always starts correctly.
        """
        self.engine      = engine
        self.writer      = CorpusWriter(corpus_path)
        self.parser_legs = [parser_leg] if isinstance(parser_leg, str) else list(parser_leg)
        self.concurrency = concurrency
        self.delay       = delay_between_requests
        self.max_raw     = max_raw_chars
        self.url_filter  = url_filter

    # ── Public API ────────────────────────────────────────────────────────────

    async def collect(
        self,
        seed_urls: list[str],
        max_pages: int = 500,
        discover_links: bool = True,
        shuffle: bool = False,
        verbose: bool = True,
    ) -> dict:
        """
        Run the scrape loop.

        seed_urls      : starting URLs — also used as crawl seeds if discover_links=True
        max_pages      : hard cap on total pages processed
        discover_links : follow same-domain links found in each page
        shuffle        : randomise the pending URL pool so crawl order is non-alphabetical;
                         gives a representative random sample when the run ends early
        verbose        : print progress

        Returns summary dict: {collected, skipped, errors, elapsed_s}
        """
        import random as _random

        if httpx is None:
            raise RuntimeError('httpx is required: pip install httpx')

        t_start    = time.time()
        visited    = self.writer.visited_urls()  # pre-populate from sidecar
        stats      = {'collected': 0, 'skipped': 0, 'errors': 0}

        if visited and verbose:
            print(f'  Resuming: {len(visited)} URLs already in corpus, skipping.')

        allowed_domain = urlparse(seed_urls[0]).netloc if seed_urls else ''

        # When shuffling, use a list-backed pool so we can randomise it;
        # workers pop from the front after each periodic shuffle.
        # When not shuffling, fall back to asyncio.Queue (unchanged behaviour).
        if shuffle:
            pending: list[str] = [u for u in seed_urls if u not in visited]
            _random.shuffle(pending)
            _pending_lock = asyncio.Lock()

            async def _get_url() -> str | None:
                async with _pending_lock:
                    return pending.pop(0) if pending else None

            async def _put_url(url: str) -> None:
                async with _pending_lock:
                    pending.append(url)
                    # Re-shuffle occasionally so newly discovered links
                    # don't always land at the end of the queue
                    if len(pending) % 50 == 0:
                        _random.shuffle(pending)

            url_queue = None  # not used in shuffle mode
        else:
            url_queue: asyncio.Queue[str] = asyncio.Queue()
            for url in seed_urls:
                if url not in visited:
                    await url_queue.put(url)

            async def _get_url() -> str | None:
                try:
                    return await asyncio.wait_for(url_queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    return None

            async def _put_url(url: str) -> None:
                await url_queue.put(url)

        stop_event = asyncio.Event()

        async def worker(worker_id: int) -> None:
            leg_name = self.parser_legs[worker_id % len(self.parser_legs)]
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                },
            ) as client:
                while not stop_event.is_set():
                    url = await _get_url()
                    if url is None:
                        break

                    if url in visited:
                        continue
                    visited.add(url)

                    # Apply URL filter — skip fetch if this page isn't wanted
                    if self.url_filter and not self.url_filter(url):
                        continue

                    if max_pages > 0 and stats['collected'] >= max_pages:
                        stop_event.set()
                        break

                    result = await self._process_url(client, url, allowed_domain, leg_name)

                    if result['status'] == 'collected':
                        stats['collected'] += 1
                        if verbose:
                            n = self.writer._counter
                            _cprint(f'  [{n:04d}] worker-{worker_id}({leg_name}) -> {url[:80]}')
                        if discover_links and result.get('links'):
                            for link in result['links']:
                                if link not in visited:
                                    if not self.url_filter or self.url_filter(link):
                                        await _put_url(link)
                    elif result['status'] == 'skipped':
                        stats['skipped'] += 1
                        visited.add(url)  # don't retry skipped pages
                        if verbose:
                            _cprint(f'  [skip] worker-{worker_id}({leg_name}) -> {url[:80]}')
                    else:
                        stats['errors'] += 1
                        if verbose:
                            _cprint(f'  [err]  worker-{worker_id}({leg_name}) -> {url[:80]} | {result.get("error", "")}')

                    await asyncio.sleep(self.delay)

        workers = [asyncio.create_task(worker(i)) for i in range(self.concurrency)]
        await asyncio.gather(*workers)

        elapsed = round(time.time() - t_start, 1)
        stats['elapsed_s'] = elapsed

        if verbose:
            print(f'\n  Corpus collection complete.')
            print(f'  Collected: {stats["collected"]}  Skipped: {stats["skipped"]}  Errors: {stats["errors"]}')
            print(f'  Elapsed: {elapsed}s')
            print(f'  Corpus: {self.writer.path}  ({self.writer._counter} chunks total)')

        return stats

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _process_url(self, client, url: str, allowed_domain: str, leg_name: str | None = None) -> dict:
        """Fetch one URL, parse, write to corpus. Returns status dict."""
        if leg_name is None:
            leg_name = self.parser_legs[0]
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {'status': 'error', 'error': f'HTTP {resp.status_code}'}

            html = resp.text
            soup = _parse_html(html)
            links = _extract_links(soup, url, allowed_domain) if allowed_domain else []

            raw_text = _html_to_raw_text(html, soup)
            raw_text = raw_text[:self.max_raw]  # cap before sending to LLM

            if not raw_text.strip():
                return {'status': 'skipped', 'links': links}

            clean = await self._parse_with_leg(url, raw_text, leg_name)

            if not clean or clean.strip().upper() == 'SKIP':
                return {'status': 'skipped', 'links': links}

            await self.writer.write(url, clean)
            return {'status': 'collected', 'links': links}

        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    async def _parse_with_leg(self, url: str, raw_text: str, leg_name: str | None = None) -> str:
        """Send raw page text to the parser leg for cleaning."""
        if leg_name is None:
            leg_name = self.parser_legs[0]
        leg = self.engine.registry.legs.get(leg_name)
        if not leg or not leg.alive:
            # Fallback: return raw text as-is if no leg available
            return raw_text[:8000]

        prompt = (
            f'URL: {url}\n\n'
            f'--- RAW PAGE TEXT ---\n\n'
            f'{raw_text}\n\n'
            f'--- END RAW TEXT ---'
        )
        try:
            return await leg.provider.complete(
                leg.model,
                _PARSER_SYSTEM,
                [{'role': 'user', 'content': prompt}],
            )
        except Exception:
            return raw_text[:8000]
