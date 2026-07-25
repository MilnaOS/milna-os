"""
/load command — extracts text from PDF files and injects as conversation context.
Chunks to a word budget so nothing overflows the context window.
"""
from pathlib import Path

SUPPORTED = {'.pdf', '.md', '.txt'}
DEFAULT_BUDGET = 3000   # words injected per load
DEFAULT_PAGES  = 20     # max pages read per file

# Thresholds for smart page classification
_PROSE_MIN_WORDS = 60    # pages with ≥ this many words and no tables → prose (free)
_TABLE_MIN_COLS  = 2     # tables need ≥ 2 columns to be considered clean
_TABLE_MIN_FILL  = 0.25  # ≥ 25% non-empty cells → clean enough to render as markdown


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


def _is_clean_table(table: list[list]) -> bool:
    """True if pdfplumber extracted a table with enough columns and fill to render as markdown."""
    if not table or not table[0]:
        return False
    ncols = max(len(r) for r in table)
    if ncols < _TABLE_MIN_COLS:
        return False
    total  = sum(len(r) for r in table)
    filled = sum(1 for r in table for c in r if c is not None and str(c).strip())
    return total > 0 and (filled / total) >= _TABLE_MIN_FILL


def _render_table_markdown(table: list[list]) -> str:
    """Render a pdfplumber table as a markdown table string."""
    if not table:
        return ''
    rows = [[str(c).strip() if c is not None else '' for c in row] for row in table]
    ncols = max(len(r) for r in rows)
    rows  = [r + [''] * (ncols - len(r)) for r in rows]
    widths = [max(len(rows[ri][ci]) for ri in range(len(rows))) for ci in range(ncols)]
    widths = [max(w, 3) for w in widths]

    def _row(r: list[str]) -> str:
        return '| ' + ' | '.join(r[i].ljust(widths[i]) for i in range(ncols)) + ' |'

    sep = '|' + '|'.join('-' * (w + 2) for w in widths) + '|'
    return '\n'.join([_row(rows[0]), sep] + [_row(r) for r in rows[1:]])


def classify_pdf_pages(path: Path) -> list[dict]:
    """
    Classify each page of a PDF into one of three types:
      'prose'       — clean readable text, no tables → use as-is, no API call needed
      'clean_table' — has well-formed tables → render as markdown, no API call needed
      'needs_llm'   — sparse or complex layout (stat blocks, multi-col) → needs distillation

    Returns list of dicts: {page_num, type, text, tables_md}
    Skips near-blank pages (≤ 5 words, no tables).
    """
    import pdfplumber
    results = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            text       = (page.extract_text() or '').strip()
            words      = len(text.split())
            raw_tables = page.extract_tables() or []
            clean      = [t for t in raw_tables if _is_clean_table(t)]
            tables_md  = '\n\n'.join(_render_table_markdown(t) for t in clean)

            if words <= 5 and not clean:
                continue   # near-blank — art, divider, cover page

            if clean:
                page_type = 'clean_table'
            elif words >= _PROSE_MIN_WORDS:
                page_type = 'prose'
            else:
                page_type = 'needs_llm'   # sparse — likely stat block or complex layout

            results.append({
                'page_num':  i,
                'type':      page_type,
                'text':      text,
                'tables_md': tables_md,
            })
    return results


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
    """
    Return supported files in a folder (recursive), sorted by name.
    Deduplicates by stem — if foo.pdf and foo.txt both exist, prefers .pdf > .md > .txt.
    Exclude by filename if provided.
    """
    _PREF = {'.pdf': 0, '.md': 1, '.txt': 2}
    candidates: dict[tuple, Path] = {}   # (parent, stem) → best path so far
    for p in path.rglob('*'):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED:
            continue
        if exclude and p.name in exclude:
            continue
        key = (p.parent, p.stem.lower())
        existing = candidates.get(key)
        if existing is None or _PREF.get(p.suffix.lower(), 99) < _PREF.get(existing.suffix.lower(), 99):
            candidates[key] = p
    return sorted(candidates.values(), key=lambda p: p.name)


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
