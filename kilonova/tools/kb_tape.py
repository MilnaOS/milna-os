"""
KB Tape — non-destructive record filtering for DOTs.

tape.json per KB stores which setting tags are currently masked.
Records are never deleted — tape only controls what surfaces in context and search.

Usage:
    from kilonova.tools.kb_tape import load_tape, save_tape, tag_all_records, tape_summary

Settings:
    'battletech'     — core BattleTech universe (default)
    'wwii_crossover' — WWII historical crossover material
"""
import json
from pathlib import Path


# Known WWII indicators — specific enough to avoid false positives in BT lore
_WWII_NAMES = {
    # Vehicles
    't-34', 't-70', 't-60', 't-28', 't-26',
    'ha-gō', 'ha-go', 'chi-ha',
    'type 95 ha', 'type 97 chi',
    'm4 sherman', 'sherman tank',
    'tiger tank', 'panzer iv',
    # Aircraft
    'supermarine spitfire', 'hawker hurricane',
    'north american p-51', 'de havilland mosquito',
    'ilyushin il-2', 'yakovlev yak',
    # Factions
    'allied powers', 'axis powers',
    'soviet union', 'red army',
    'imperial japanese', 'imperial japan',
    'nazi germany', 'third reich', 'der führer',
    # People (WWII crossover characters)
    'liao yaoxiang', 'shimada', 'gotanda',
    # Explicit era labels in record text
    'world war ii', 'world war 2', 'ww2', 'wwii',
    'second world war',
}

KNOWN_SETTINGS = {
    'battletech':     'Core BattleTech universe',
    'wwii_crossover': 'WWII historical crossover',
}


def _record_text(record: dict) -> str:
    """Flatten record values to one string for keyword detection."""
    parts = []
    def _walk(val):
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            for x in val:
                _walk(x)
        elif isinstance(val, dict):
            for x in val.values():
                _walk(x)
    _walk(record)
    return ' '.join(parts).lower()


def tag_setting(record: dict) -> str:
    """Detect the setting for a record. Returns 'wwii_crossover' or 'battletech'."""
    text = _record_text(record)
    for indicator in _WWII_NAMES:
        if indicator in text:
            return 'wwii_crossover'
    return 'battletech'


def tag_all_records(kb_dir: Path, progress_cb=None) -> dict:
    """
    Stamp a 'setting' field on every record in the KB that doesn't have one.
    Updates both individual .json files and _index.json in-place.
    Returns {'total': N, 'wwii_crossover': N, 'battletech': N}
    """
    entries_dir = kb_dir / 'entries'
    if not entries_dir.exists():
        return {'total': 0, 'wwii_crossover': 0, 'battletech': 0}

    counts: dict[str, int] = {'wwii_crossover': 0, 'battletech': 0}
    total = 0

    for entity_dir in sorted(entries_dir.iterdir()):
        if not entity_dir.is_dir():
            continue

        entity_wwii = 0

        # Tag individual record files
        for record_file in entity_dir.glob('*.json'):
            if record_file.name == '_index.json':
                continue
            try:
                record = json.loads(record_file.read_text(encoding='utf-8'))
                if not isinstance(record, dict):
                    continue
                if 'setting' not in record:
                    record['setting'] = tag_setting(record)
                    record_file.write_text(json.dumps(record, indent=2), encoding='utf-8')
                setting = record.get('setting', 'battletech')
                counts[setting] = counts.get(setting, 0) + 1
                if setting == 'wwii_crossover':
                    entity_wwii += 1
                total += 1
            except Exception:
                continue

        # Rebuild _index.json with setting stamps
        index_path = entity_dir / '_index.json'
        if index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding='utf-8'))
                if isinstance(existing, list):
                    updated = []
                    for r in existing:
                        if isinstance(r, dict):
                            if 'setting' not in r:
                                r['setting'] = tag_setting(r)
                            updated.append(r)
                    index_path.write_text(json.dumps(updated, indent=2), encoding='utf-8')
            except Exception:
                pass

        if progress_cb and entity_wwii:
            progress_cb(f'  {entity_dir.name}: {entity_wwii} WWII record(s) tagged')

    return {'total': total, **counts}


# ── Tape state ─────────────────────────────────────────────────────────────────

def _tape_path(kb_dir: Path) -> Path:
    return kb_dir / 'tape.json'


def load_tape(kb_dir: Path) -> set[str]:
    """Return the set of currently masked setting tags for this KB."""
    p = _tape_path(kb_dir)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return set(data.get('masked', []))
    except Exception:
        return set()


def save_tape(kb_dir: Path, masked: set[str]) -> None:
    _tape_path(kb_dir).write_text(
        json.dumps({'masked': sorted(masked)}, indent=2), encoding='utf-8'
    )


def apply_tape(records: list[dict], masked: set[str]) -> list[dict]:
    """Filter a record list, excluding any whose 'setting' is currently masked."""
    if not masked:
        return records
    return [r for r in records if r.get('setting', 'battletech') not in masked]


def tape_summary(kb_dir: Path) -> dict:
    """
    Return record counts by setting and current tape state.
    {'masked': set, 'counts': {'battletech': N, 'wwii_crossover': N}, 'total': N, 'tagged': bool}
    """
    masked = load_tape(kb_dir)
    counts: dict[str, int] = {}
    tagged = False
    entries_dir = kb_dir / 'entries'

    if entries_dir.exists():
        for entity_dir in entries_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            index_path = entity_dir / '_index.json'
            if not index_path.exists():
                continue
            try:
                records = json.loads(index_path.read_text(encoding='utf-8'))
                for r in records:
                    if isinstance(r, dict):
                        s = r.get('setting')
                        if s:
                            tagged = True
                        counts[s or 'battletech'] = counts.get(s or 'battletech', 0) + 1
            except Exception:
                continue

    total = sum(counts.values())
    return {'masked': masked, 'counts': counts, 'total': total, 'tagged': tagged}
