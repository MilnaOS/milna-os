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
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    # No index yet (ingest in progress) — load individual files directly
    entity_dir = kb_root / topic / 'entries' / entity
    if not entity_dir.exists():
        return []
    records = []
    for f in entity_dir.glob('*.json'):
        if f.name == '_index.json':
            continue
        try:
            records.append(json.loads(f.read_text(encoding='utf-8')))
        except Exception:
            pass
    return records


def _collection_schema_names(kb_root: Path, topic: str) -> set[str]:
    schemas_dir = kb_root / topic / 'schemas'
    names: set[str] = set()
    if schemas_dir.exists():
        for sf in schemas_dir.glob('*.json'):
            try:
                if json.loads(sf.read_text(encoding='utf-8')).get('_collection'):
                    names.add(sf.stem)
            except Exception:
                pass
    return names


def _count_entities(kb_root: Path, topic: str) -> dict[str, int]:
    """Count records per entity type — works with or without _index.json."""
    entries_root = kb_root / topic / 'entries'
    if not entries_root.exists():
        return {}
    skip = _collection_schema_names(kb_root, topic)
    counts = {}
    for entity_dir in entries_root.iterdir():
        if not entity_dir.is_dir():
            continue
        if entity_dir.name in skip:
            continue
        index = entity_dir / '_index.json'
        if index.exists():
            try:
                counts[entity_dir.name] = len(json.loads(index.read_text(encoding='utf-8')))
                continue
            except Exception:
                pass
        counts[entity_dir.name] = sum(1 for f in entity_dir.glob('*.json') if f.name != '_index.json')
    return counts


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
        # Sarna-ingest schema names
        ('class',        'Class'),
        ('mass',         'Mass (t)'),
        ('speed_kph',    'Speed (kph)'),
        ('armor',        'Armor'),
        ('engine',       'Engine'),
        ('heat_sinks',   'Heat Sinks'),
        ('cost',         'Cost (C-bills)'),
        ('B.V_1.0',      'BV 1.0'),
        ('B.V_2.0',      'BV 2.0'),
        ('chassis',      'Chassis'),
        ('model',        'Model'),
        # Kilonova-derived schema names (DOT ingest)
        ('tonnage',      'Tonnage'),
        ('weight_class', 'Weight Class'),
        ('walk_mp',      'Walk MP'),
        ('run_mp',       'Run MP'),
        ('jump_mp',      'Jump MP'),
        ('armor_total',  'Armor'),
        ('battle_value', 'BV'),
        ('cost_cbills',  'Cost (C-bills)'),
        ('tech_base',    'Tech Base'),
        ('manufacturer', 'Manufacturer'),
        ('primary_factory', 'Factory'),
        ('era',          'Era'),
        ('affiliation',  'Affiliation'),
        ('role',         'Role'),
        # Weapon/equipment fields
        ('damage',       'Damage'),
        ('range',        'Range'),
        ('heat',         'Heat'),
        ('ammo_per_ton', 'Ammo/ton'),
        ('type',         'Type'),
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


_COUNT_TERMS = {'how', 'many', 'count', 'total', 'number', 'list', 'all'}


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

    # Count query — "how many battlemechs", "total weapons", etc.
    q_lower = query.lower()
    if any(t in q_lower for t in _COUNT_TERMS):
        counts = _count_entities(root, topic)
        if counts:
            lines = [f'## {topic.upper()} KB — Record Counts\n']
            for entity, n in sorted(counts.items()):
                lines.append(f'  {entity}: {n} records')
            return '\n'.join(lines)

    terms = _tokenize(query)
    if not terms:
        return ''

    entries_root = root / topic / 'entries'
    if not entries_root.exists():
        return ''

    # Load schema collection flags so we can skip prose buckets
    schemas_dir = root / topic / 'schemas'
    _collection_types: set[str] = set()
    if schemas_dir.exists():
        for sf in schemas_dir.glob('*.json'):
            try:
                s = json.loads(sf.read_text(encoding='utf-8'))
                if s.get('_collection'):
                    _collection_types.add(sf.stem)
            except Exception:
                pass

    # Determine which entity types to search
    if entity_types:
        types_to_search = [e for e in entity_types if e not in _collection_types]
    else:
        types_to_search = [
            p.name for p in entries_root.iterdir()
            if p.is_dir() and p.name not in _collection_types
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
