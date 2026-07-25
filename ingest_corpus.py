"""
ingest_corpus.py — Convert a flat corpus.txt into KB entity records.

Page-level extraction: each source page is sent whole to the LLM.
One entity per page. No chunking. No deduplication needed.

Pipeline:
  1. Split corpus.txt into individual source pages on the --- separator
  2. Load entity schemas from kb/<topic>/schemas/
  3. Carousel extraction — N workers rotating across provider legs
     Each work item: (entity_type, page_url, page_text)
     LLM returns one JSON object or null (null = page is not about this entity)
     Non-null results written immediately to kb/<topic>/entries/<type>/<slug>.json
  4. Build _index.json per entity type from all written files

Usage:
    python ingest_corpus.py --topic battletech
    python ingest_corpus.py --topic battletech --force --types battlemech weapon
    python ingest_corpus.py --topic battletech --concurrency 5
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs   import LegRegistry, KeyInventory, Leg
from kilonova.tools.ingest import _parse_json_safe, _safe_name
from kilonova.core.providers.ollama_provider import OllamaProvider


# ── Extraction prompts ────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are a structured data extractor for a knowledge base.
You receive one wiki page and a target entity type with a JSON schema.

Rules:
- If the page is a dedicated article for a single entity of that type, return a JSON object matching the schema.
- If the page is a category listing, lore article, manufacturer profile, pilot biography, disambiguation page, \
or any page that is not primarily about a single entity of the requested type — return null.
- Use null for any schema field not present in the page. Do not invent data.
- Output ONLY valid JSON: one object or the word null. No prose, no markdown fences."""

_EXTRACT_USER = """\
Entity type: {entity_type}

Schema:
{schema}

Return a JSON object for this {entity_type}, or null if the page is not a primary {entity_type} article.

PAGE:
{text}"""


# ── Corpus loader ─────────────────────────────────────────────────────────────

def _load_pages(corpus_path: Path) -> list[tuple[str, str]]:
    """Split corpus.txt into (url, body) tuples — one per source page."""
    text = corpus_path.read_text(encoding='utf-8', errors='ignore')
    pages = []
    for page in text.split('\n\n---\n\n'):
        page = page.strip()
        if not page:
            continue
        m = re.match(r'\[\d+\] SOURCE:(\S+)\n', page)
        url  = m.group(1) if m else ''
        body = page[m.end():].strip() if m else page
        if body:
            pages.append((url, body))
    return pages


def _url_slug(url: str) -> str:
    """Derive a safe filename stem from a page URL."""
    path = urlparse(url).path
    slug = path.rstrip('/').split('/')[-1] if path else url
    return _safe_name(slug) or 'unknown'


# ── Leg selection ─────────────────────────────────────────────────────────────

def _build_extraction_legs(registry: LegRegistry) -> list[Leg]:
    active = {l.name: l for l in registry.active_legs()}
    legs: list[Leg] = []
    for name in ['cerebras', 'openai', 'groq', 'together', 'sambanova', 'gemini', 'openrouter']:
        if name in active:
            legs.append(active[name])
    for model in ['qwen2.5:3b', 'llama3.2:3b', 'llama3.2:1b', 'qwen2.5:0.5b']:
        key = f'ollama:{model}'
        if key in active:
            legs.append(active[key])
            break
    return legs


# ── Single-page extraction ───────────────────────────────────────────���────────

async def _call_provider(
    leg: Leg, entity_type: str, schema: dict, page_text: str
) -> tuple[dict | None, float | bool]:
    """
    Extract one entity record from one page.
    Returns (record_or_None, backoff) where backoff is False or seconds-to-wait.
    """
    # Send only the fields definition — strip outer description/fields wrapper
    # so the LLM returns a flat object rather than mirroring the schema envelope.
    schema_body = schema.get('fields', schema)
    user_msg = _EXTRACT_USER.format(
        entity_type=entity_type,
        schema=json.dumps(schema_body, indent=2),
        text=page_text,
    )
    try:
        raw = await asyncio.wait_for(
            leg.provider.complete(leg.model, _EXTRACT_SYSTEM, [{'role': 'user', 'content': user_msg}]),
            timeout=90.0,
        )
        stripped = raw.strip()
        if not stripped or stripped.lower() == 'null':
            return None, False
        record = _parse_json_safe(raw, default=None)
        if isinstance(record, dict):
            # Unwrap schema envelope if the LLM echoed the outer fields wrapper
            if 'fields' in record and isinstance(record['fields'], dict):
                record = record['fields']
        return (record if isinstance(record, dict) else None), False

    except asyncio.TimeoutError:
        return None, False
    except Exception as e:
        err = str(e)
        if '429' in err or 'rate limit' in err.lower() or 'rate_limit' in err.lower():
            m = re.search(r'try again in (\d+)m([\d.]+)s', err)
            secs = int(m.group(1)) * 60 + float(m.group(2)) if m else 60
            return None, secs
        if '402' in err or 'credit limit' in err.lower() or 'payment required' in err.lower():
            print(f'    [{leg.name}] credit exhausted (402) — disabling for this run', flush=True)
            return None, 86400  # item re-queued; leg effectively out for the run
        if any(c in err for c in ('503', '502', '529', 'overloaded', 'service unavailable')):
            print(f'    [{leg.name}] transient error — backing off 30s', flush=True)
            return None, 30
        print(f'    [{leg.name}] error on {entity_type}: {err[:100]}', flush=True)
        return None, False


async def _delayed_return(pool: asyncio.Queue, leg: Leg, delay: float) -> None:
    await asyncio.sleep(min(delay, 600))
    await pool.put(leg)
    print(f'  [{leg.name}] back in rotation', flush=True)


# ── Carousel ──────────────────────────────────────────────────────────────────

async def _gatling_extract(
    legs: list[Leg],
    schemas: dict,
    pages: list[tuple[str, str]],
    kb_dir: Path,
    force: bool = False,
    concurrency: int = 3,
) -> dict[str, int]:
    """
    Carousel extraction — page-level, one entity per call.
    Writes results immediately. Returns {entity_type: records_written}.
    """
    if not legs:
        print('  ERROR: No extraction legs available.', flush=True)
        return {}

    entity_dirs: dict[str, Path] = {}
    counts:      dict[str, int]  = {}
    work_queue:  asyncio.Queue   = asyncio.Queue()
    total_items = 0

    for entity_type, schema in schemas.items():
        entity_dir = kb_dir / 'entries' / _safe_name(entity_type)
        entity_dir.mkdir(parents=True, exist_ok=True)
        entity_dirs[entity_type] = entity_dir
        counts[entity_type] = 0

        for url, body in pages:
            slug     = _url_slug(url)
            out_path = entity_dir / f'{slug}.json'
            if out_path.exists() and not force:
                continue
            work_queue.put_nowait((entity_type, schema, url, body))
            total_items += 1

    if total_items == 0:
        print('  All pages already processed — use --force to re-extract.', flush=True)
        return counts

    concurrency = min(concurrency, len(legs), total_items)
    print(f'  Carousel: {total_items} work items, {concurrency} slots, {len(legs)} legs', flush=True)
    for i, leg in enumerate(legs):
        print(f'    leg-{i}: {leg.name} / {leg.model}', flush=True)
    print(flush=True)

    leg_pool: asyncio.Queue = asyncio.Queue()
    for leg in legs:
        await leg_pool.put(leg)

    lock          = asyncio.Lock()
    done_total    = [0]

    async def worker() -> None:
        while True:
            leg = await leg_pool.get()
            try:
                item = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                await leg_pool.put(leg)
                break

            entity_type, schema, url, body = item
            record, backoff = await _call_provider(leg, entity_type, schema, body)

            if backoff:
                work_queue.put_nowait(item)
                secs = float(backoff)
                print(f'  [{leg.name}] rate-limited — rotating out for {secs:.0f}s', flush=True)
                asyncio.create_task(_delayed_return(leg_pool, leg, secs))
                continue

            if record is not None:
                slug     = _url_slug(url)
                out_path = entity_dirs[entity_type] / f'{slug}.json'
                async with lock:
                    out_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
                    counts[entity_type] += 1
                    done_total[0] += 1
                    n = done_total[0]
                    if n % 25 == 0 or n <= 5:
                        summary = '  |  '.join(f'{et}: {c}' for et, c in counts.items() if c)
                        print(f'  [{n}/{total_items}]  {summary}', flush=True)

            await leg_pool.put(leg)

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    return counts


# ── Index builder ─────────────────────────────────────────────────────────────

def _build_index(entity_dir: Path) -> list[dict]:
    """Read all per-page JSON files and write _index.json."""
    records = []
    for f in sorted(entity_dir.glob('*.json')):
        if f.name == '_index.json':
            continue
        try:
            records.append(json.loads(f.read_text(encoding='utf-8')))
        except Exception:
            pass
    (entity_dir / '_index.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    return records


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    topic       = args.topic.lower()
    kb_dir      = Path('kb') / topic
    corpus_path = kb_dir / 'corpus.txt'
    schema_dir  = kb_dir / 'schemas'

    if not corpus_path.exists():
        print(f'ERROR: No corpus at {corpus_path}')
        sys.exit(1)

    print(f'Corpus Ingest — {topic}')
    print(f'Corpus: {corpus_path}  ({corpus_path.stat().st_size / 1024:.1f} KB)')

    pages = _load_pages(corpus_path)
    print(f'Pages:  {len(pages)} source pages')

    if not schema_dir.exists() or not list(schema_dir.glob('*.json')):
        print(f'ERROR: No schemas in {schema_dir}  (run schema discovery first)')
        sys.exit(1)

    schemas: dict[str, dict] = {}
    for sf in sorted(schema_dir.glob('*.json')):
        schemas[sf.stem] = json.loads(sf.read_text(encoding='utf-8'))

    if args.types:
        schemas = {k: v for k, v in schemas.items() if k in args.types}
        if not schemas:
            print(f'ERROR: None of {args.types} found in schemas.')
            sys.exit(1)

    print(f'Types:  {list(schemas.keys())}')
    print(f'Force:  {args.force}')
    print()

    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()

    legs = _build_extraction_legs(registry)
    if not legs:
        print('ERROR: No API keys found. Run add_keys.py to register providers.')
        sys.exit(1)

    counts = await _gatling_extract(
        legs=legs,
        schemas=schemas,
        pages=pages,
        kb_dir=kb_dir,
        force=args.force,
        concurrency=args.concurrency,
    )

    print('\nBuilding indexes...')
    total = 0
    final_counts: dict[str, int] = {}
    for entity_type in schemas:
        entity_dir = kb_dir / 'entries' / _safe_name(entity_type)
        if entity_dir.exists():
            records = _build_index(entity_dir)
            print(f'  {entity_type}: {len(records)} records')
            total += len(records)
            final_counts[entity_type] = len(records)

    # Update overview.md with current record counts so /kb queries are informed
    overview_path = kb_dir / 'overview.md'
    if overview_path.exists():
        overview = overview_path.read_text(encoding='utf-8')
        counts_block = '\n## Record Counts (last ingest)\n\n' + '\n'.join(
            f'- **{et}**: {n} records' for et, n in sorted(final_counts.items())
        ) + '\n'
        # Replace existing counts block if present, otherwise append
        if '## Record Counts' in overview:
            overview = re.sub(r'\n## Record Counts.*', counts_block, overview, flags=re.DOTALL)
        else:
            overview = overview.rstrip() + '\n' + counts_block
        overview_path.write_text(overview, encoding='utf-8')
        print(f'  overview.md updated with record counts.')

    print(f'\nDone. {total} total records across {len(schemas)} entity types.')


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Corpus ingest — page-level entity extraction')
    p.add_argument('--topic',       required=True,           help='KB topic (e.g. battletech)')
    p.add_argument('--force',       action='store_true',     help='Re-extract pages already on disk')
    p.add_argument('--types',       nargs='+',               help='Entity types to run (default: all schemas)')
    p.add_argument('--concurrency', type=int, default=3,     help='Parallel workers (default: 3)')
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
