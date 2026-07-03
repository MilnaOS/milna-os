"""
KB Search — searches entity indexes in a DOT knowledge base.
Called automatically on each turn when a KB is active.
Returns matching records as a formatted context block for injection.
"""
import json
import re
from pathlib import Path


_DEFAULT_KB_ROOT = Path('kb')
_MAX_RECORDS = 12      # max records to inject per turn
_MAX_CHARS   = 6000    # hard cap on injected context size


def _load_index(topic: str, entity: str, kb_root: Path) -> list[dict]:
    path = kb_root / topic / 'entries' / entity / '_index.json'
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []


def _fuzzy_match(term: str, target: str) -> bool:
    """True if term matches target exactly, or shares a prefix of 5+ chars."""
    if term in target:
        return True
    min_len = min(len(term), len(target))
    if min_len >= 5 and target[:min_len - 1] == term[:min_len - 1]:
        return True
    return False


def _score_record(record: dict, terms: list[str]) -> int:
    """Score a record against query terms. Higher = better match."""
    text = json.dumps(record, default=str).lower()
    score = 0
    for term in terms:
        if _fuzzy_match(term, text):
            # Name/variant matches score higher than body matches
            name = (record.get('name') or '').lower()
            variant = (record.get('variant') or '').lower()
            if _fuzzy_match(term, name) or _fuzzy_match(term, variant):
                score += 10
            else:
                score += 1
    return score


def _tokenize(query: str) -> list[str]:
    """Extract meaningful search terms from a query."""
    # Strip common stopwords, keep nouns/names
    stopwords = {
        'a', 'an', 'the', 'is', 'are', 'was', 'what', 'who', 'how', 'many',
        'tell', 'me', 'about', 'and', 'or', 'of', 'to', 'in', 'on', 'for',
        'with', 'can', 'do', 'does', 'did', 'list', 'give', 'show', 'find',
        'which', 'where', 'when', 'i', 'my', 'its', 'it', 'from', 'that',
        'this', 'there', 'any', 'all', 'some', 'their', 'they', 'have',
        'has', 'had', 'not', 'but', 'by', 'at', 'be', 'as', 'up', 'get',
    }
    words = re.findall(r"[a-z0-9][a-z0-9'\-]*", query.lower())
    return [w for w in words if w not in stopwords and len(w) > 2]


def _format_record(record: dict) -> str:
    """Format a single record as compact readable text."""
    lines = []
    name = record.get('name') or 'Unknown'
    variant = record.get('variant')
    header = f"**{name}**" + (f" ({variant})" if variant else "")
    lines.append(header)

    fields = [
        ('tonnage',      'Tonnage'),
        ('weight_class', 'Class'),
        ('walk_mp',      'Walk MP'),
        ('run_mp',       'Run MP'),
        ('jump_mp',      'Jump MP'),
        ('speed_kph',    'Speed'),
        ('armor_total',  'Armor'),
        ('heat_sinks',   'Heat Sinks'),
        ('battle_value', 'BV'),
        ('cost_cbills',  'Cost (C-bills)'),
        ('tech_base',    'Tech Base'),
        ('manufacturer', 'Manufacturer'),
        ('primary_factory', 'Factory'),
        ('era',          'Era'),
        ('affiliation',  'Affiliation'),
        ('role',         'Role'),
    ]
    for key, label in fields:
        val = record.get(key)
        if val is not None and val != '' and val != [] and val != 0.0:
            lines.append(f"  {label}: {val}")

    weapons = record.get('weapons')
    if weapons:
        w_parts = []
        for w in weapons:
            if isinstance(w, dict):
                wname = w.get('name', '')
                qty   = w.get('quantity')
                loc   = w.get('location')
                part  = f"{qty}× {wname}" if qty else wname
                if loc:
                    part += f" ({loc})"
                w_parts.append(part)
        if w_parts:
            lines.append(f"  Weapons: {', '.join(w_parts)}")

    notes = record.get('notes')
    if notes:
        lines.append(f"  Notes: {notes}")

    return '\n'.join(lines)


def search_kb(
    topic: str,
    query: str,
    entity_types: list[str] | None = None,
    kb_root: Path | None = None,
) -> str:
    """
    Search the KB for records relevant to the query.
    Returns a formatted context block ready for injection, or '' if nothing found.
    """
    root = kb_root or _DEFAULT_KB_ROOT
    terms = _tokenize(query)
    if not terms:
        return ''

    entries_root = root / topic / 'entries'
    if not entries_root.exists():
        return ''

    # Determine which entity types to search
    if entity_types:
        types_to_search = entity_types
    else:
        types_to_search = [
            p.name for p in entries_root.iterdir() if p.is_dir()
        ]

    # Score all records across all entity types
    scored: list[tuple[int, str, dict]] = []
    for entity in types_to_search:
        records = _load_index(topic, entity, root)
        for record in records:
            score = _score_record(record, terms)
            if score > 0:
                scored.append((score, entity, record))

    if not scored:
        return ''

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:_MAX_RECORDS]

    # Format into context block
    blocks = []
    total_chars = 0
    for score, entity, record in top:
        formatted = _format_record(record)
        if total_chars + len(formatted) > _MAX_CHARS:
            break
        blocks.append(f"[{entity}]\n{formatted}")
        total_chars += len(formatted)

    if not blocks:
        return ''

    header = f"## KB SEARCH RESULTS: {topic.upper()} ({len(blocks)} records matching \"{query[:60]}\")\n\n"
    return header + '\n\n---\n\n'.join(blocks)
