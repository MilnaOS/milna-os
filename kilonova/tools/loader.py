"""
/load command — extracts text from PDF files and injects as conversation context.
Chunks to a word budget so nothing overflows the context window.
"""
from pathlib import Path

SUPPORTED = {'.pdf', '.md', '.txt'}
DEFAULT_BUDGET = 3000   # words injected per load
DEFAULT_PAGES  = 20     # max pages read per file


def _extract_text(path: Path) -> str:
    return path.read_text(encoding='utf-8', errors='replace')


def _extract_pdf(path: Path, max_pages: int = DEFAULT_PAGES) -> str:
    import pdfplumber
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            text = page.extract_text()
            if text:
                pages.append(text.strip())
    return '\n\n'.join(pages)


def extract_pdf_pages(path: Path, min_words: int = 60) -> list[str]:
    """
    Return list of page texts, skipping near-blank pages (art, dividers).
    Used for TRO-style documents where each page is a discrete entry.
    """
    import pdfplumber
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            if len(text.split()) >= min_words:
                pages.append(text.strip())
    return pages


def _truncate(text: str, budget_words: int) -> tuple[str, int, int]:
    words = text.split()
    total = len(words)
    if total <= budget_words:
        return text, total, total
    return ' '.join(words[:budget_words]), budget_words, total


def load_file(path: Path, budget_words: int = DEFAULT_BUDGET) -> dict:
    """Extract text from a single file. Returns dict with text, stats, error."""
    if not path.exists():
        return {'ok': False, 'error': f'File not found: {path}'}
    if path.suffix.lower() not in SUPPORTED:
        return {'ok': False, 'error': f'Unsupported type: {path.suffix} (supported: {SUPPORTED})'}
    try:
        if path.suffix.lower() == '.pdf':
            raw = _extract_pdf(path)
        else:
            raw = _extract_text(path)
        if not raw.strip():
            return {'ok': False, 'error': 'No text extracted — file appears empty'}
        effective_budget = budget_words if path.suffix.lower() == '.pdf' else max(budget_words, len(raw.split()))
        text, used, total = _truncate(raw, effective_budget)
        return {
            'ok':        True,
            'text':      text,
            'words_used': used,
            'words_total': total,
            'truncated': used < total,
            'name':      path.name,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def list_folder(path: Path, exclude: set[str] | None = None) -> list[Path]:
    """Return supported files in a folder (recursive), sorted by name. Exclude by filename if provided."""
    return sorted(
        p for p in path.rglob('*')
        if p.is_file()
        and p.suffix.lower() in SUPPORTED
        and (exclude is None or p.name not in exclude)
    )


def already_ingested(topic: str, kb_root: Path = Path('kb')) -> set[str]:
    """Return set of filenames already in the DOT manifest for a topic."""
    manifest_path = kb_root / topic / 'manifest.json'
    if not manifest_path.exists():
        return set()
    import json
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    return {doc['file'] for doc in manifest.get('documents', [])}


def build_context_block(results: list[dict]) -> str:
    """Format loaded docs as a context block for injection into conversation."""
    parts = []
    for r in results:
        if r['ok']:
            truncated_note = f' [truncated to {r["words_used"]:,} of {r["words_total"]:,} words]' \
                if r['truncated'] else ''
            parts.append(
                f'### {r["name"]}{truncated_note}\n\n{r["text"]}'
            )
    if not parts:
        return ''
    return '## LOADED DOCUMENTS\n\n' + '\n\n---\n\n'.join(parts)
