"""
Web fetch tool — fetches a URL and returns clean text for injection or ingestion.
Uses stdlib html.parser so no extra dependencies beyond httpx (already required).
"""
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


# Tags whose content we discard entirely
_SKIP_TAGS = {
    'script', 'style', 'nav', 'footer', 'header', 'aside',
    'noscript', 'iframe', 'svg', 'form', 'button',
}

# Tags that imply a line break in output
_BLOCK_TAGS = {
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'li', 'tr', 'br', 'hr', 'blockquote', 'pre',
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip_depth += 1
        if self._skip_depth == 0 and t in _BLOCK_TAGS:
            self._parts.append('\n')

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = ''.join(self._parts)
        # Collapse whitespace runs, preserve paragraph breaks
        raw = re.sub(r'[ \t]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()


def fetch_url(url: str, timeout: float = 15.0) -> dict:
    """
    Fetch a URL and return clean text.
    Returns {'ok': True, 'text': ..., 'url': ..., 'words': N}
           {'ok': False, 'error': ..., 'url': ...}
    """
    try:
        headers = {'User-Agent': 'Milna/1.0 (research tool; +https://github.com/milna-os)'}
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '')
        if 'html' in content_type:
            parser = _TextExtractor()
            parser.feed(response.text)
            text = parser.get_text()
        else:
            # Plain text, markdown, JSON etc — return as-is
            text = response.text.strip()

        if not text:
            return {'ok': False, 'error': 'No text extracted from page', 'url': url}

        return {
            'ok':    True,
            'text':  text,
            'url':   url,
            'words': len(text.split()),
        }
    except httpx.HTTPStatusError as e:
        return {'ok': False, 'error': f'HTTP {e.response.status_code}', 'url': url}
    except httpx.RequestError as e:
        return {'ok': False, 'error': str(e), 'url': url}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'url': url}


# MediaWiki namespaces that are never content pages
_WIKI_SKIP_PREFIXES = (
    'Special:', 'File:', 'Category:', 'Template:', 'Help:',
    'User:', 'Talk:', 'User_talk:', 'Template_talk:', 'Wikipedia:',
    'MediaWiki:', 'Module:', 'Portal:',
)


def extract_links(url: str, text: str, same_domain: bool = True) -> list[str]:
    """
    Extract hrefs from raw HTML and return absolute URLs.
    Used by /webseed to crawl index pages for sub-page links.
    Skips MediaWiki infrastructure pages (Special:, File:, Category:, etc.).
    """
    base_domain = urlparse(url).netloc
    links = []
    for match in re.finditer(r'href=["\']([^"\'#?]+)["\']', text):
        href = match.group(1)
        absolute = urljoin(url, href)
        parsed = urlparse(absolute)
        if same_domain and parsed.netloc != base_domain:
            continue
        # Only follow /wiki/ paths; skip infrastructure namespaces
        path = parsed.path
        if '/wiki/' in path:
            article = path.split('/wiki/')[-1]
            if any(article.startswith(p) for p in _WIKI_SKIP_PREFIXES):
                continue
        links.append(absolute)
    return list(dict.fromkeys(links))   # deduplicate preserving order


def fetch_url_raw_html(url: str, timeout: float = 15.0) -> dict:
    """Fetch URL and return raw HTML (needed for link extraction before parsing)."""
    try:
        headers = {'User-Agent': 'Milna/1.0 (research tool; +https://github.com/milna-os)'}
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return {'ok': True, 'html': response.text, 'url': url}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'url': url}
