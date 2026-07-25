"""
build_dot.py — Mode 2 artifact builder.

Reads entries/*.json, applies a confidence gate, then runs curator workers
to synthesize curated Sub-DOT markdown files in raw/<entity_type>.md.

These raw/*.md files are what Milna's persona routing rules point to.
Milna never reads entries/*.json — she reads only what Mode 2 produces.

Two outputs per entity type:
  raw/<type>.md         — confident, curated, Milna-ready
  raw/<type>_review.md  — low-confidence records for human review

Pipeline:
  entries/*.json
      ↓ confidence gate
  confident -> LLM curator batch -> raw/<type>.md
  flagged   -> deterministic dump -> raw/<type>_review.md

Usage:
    python build_dot.py --topic battletech
    python build_dot.py --topic battletech --types battlemech weapon
    python build_dot.py --topic battletech --confidence 0.35 --batch-size 40
    python build_dot.py --topic battletech --force
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs   import LegRegistry, KeyInventory, Leg
from kilonova.tools.ingest import _safe_name

KB_ROOT  = Path(__file__).parent / 'kb'
RAW_SUBDIR = 'raw'

# ── Confidence gate ────────────────────────────────────────────────────────────

def _confidence_score(record: dict, schema: dict) -> float:
    """
    Field fill ratio: populated_fields / total_schema_fields.
    A field counts as populated if it is not None, '', [], or 0.
    """
    fields = schema.get('fields', {})
    if not fields:
        # No schema — accept everything
        return 1.0
    total = len(fields)
    filled = sum(
        1 for key in fields
        if record.get(key) not in (None, '', [], 0, 0.0)
    )
    return filled / total if total else 1.0


def _gate_records(
    records: list[dict],
    schema: dict,
    threshold: float,
) -> tuple[list[dict], list[tuple[dict, float]]]:
    """
    Split records into (confident, flagged).
    flagged entries are (record, score) tuples for the review file.
    """
    confident: list[dict] = []
    flagged:   list[tuple[dict, float]] = []
    for rec in records:
        score = _confidence_score(rec, schema)
        if score >= threshold:
            confident.append(rec)
        else:
            flagged.append((rec, score))
    return confident, flagged


# ── Curator system prompts ─────────────────────────────────────────────────────

_CURATOR_BASE = """\
You are a knowledge curator preparing Sub-DOT files for a GM AI persona named Milna.
Milna runs BattleTech tabletop sessions. She is precise, tactical, and immersive.

You receive a batch of structured entity records in JSON.
Your job is to reformat them into compact, scannable markdown that Milna can use
directly — no translation needed, no re-parsing required.

Rules:
- Every field in every record must appear. Do not drop or summarize data.
- Present stats in a way that is immediately useful mid-session.
- Do not add lore, opinions, or invented details. Only what the records contain.
- Use markdown headers (##) for entity names, bold (**) for field labels.
- Output ONLY the formatted markdown. No preamble, no closing remarks."""

_CURATOR_PROMPTS = {
    'battlemech': _CURATOR_BASE + """

Format each BattleMech as:

## {Name} ({Model})
**Class:** {class} | **Mass:** {mass}t | **Speed:** {speed_kph} kph | **BV:** {BV_1.0}/{BV_2.0}
**Engine:** {engine} | **Heat Sinks:** {heat_sinks} | **Armor:** {armor} | **Chassis:** {chassis}
**Weapons:** {weapon list as "Nx name" entries, comma-separated}
**Cost:** {cost} C-bills
""",

    'weapon': _CURATOR_BASE + """

Format each weapon as:

## {Name}
**Damage:** {damage} | **Heat:** {heat} | **Weight:** {weight}t | **Cost:** {cost} C-bills
**Range:** Short {short}  Medium {medium}  Long {long}
**Ammo/ton:** {ammo} | **Tech Base:** {tech_base}
""",

    'faction': _CURATOR_BASE + """

Format each faction as:

## {Name}
**Affiliation:** {affiliation} | **Era:** {era}
**Voice:** {tone or description}
**Known for:** {key traits}
**Example line:** "{example dialogue if present}"
""",

    'vehicle': _CURATOR_BASE + """

Format each vehicle as:

## {Name} ({Model})
**Class:** {class} | **Mass:** {mass}t | **Speed:** {speed_kph} kph | **BV:** {battle_value}
**Weapons:** {weapon list}
**Cost:** {cost} C-bills
""",

    'equipment': _CURATOR_BASE + """

Format each piece of equipment as:

## {Name}
**Weight:** {weight}t | **Cost:** {cost} C-bills | **Tech Base:** {tech_base}
**Effect:** {description or effect}
""",

    'aircraft': _CURATOR_BASE + """

Format each aircraft as:

## {Name} ({Model})
**Mass:** {mass}t | **Speed:** {speed_kph} kph | **BV:** {battle_value}
**Weapons:** {weapon list}
**Cost:** {cost} C-bills
""",
}

_CURATOR_GENERIC = _CURATOR_BASE + """

Format each record with its name as a ## header, then all fields as
**Key:** Value lines. Group logically. Keep it compact."""


def _curator_system(entity_type: str) -> str:
    return _CURATOR_PROMPTS.get(entity_type.lower(), _CURATOR_GENERIC)


# ── Curator LLM call ──────────────────────────────────────────────────────────

async def _curate_batch(
    leg: Leg,
    entity_type: str,
    records: list[dict],
) -> tuple[str | None, float | bool]:
    """
    Send a batch of records to the curator leg.
    Returns (markdown_section, backoff) — backoff is False or seconds.
    """
    system = _curator_system(entity_type)
    user   = (
        f'Entity type: {entity_type}\n'
        f'Record count: {len(records)}\n\n'
        f'RECORDS:\n'
        + json.dumps(records, indent=2)
    )
    try:
        raw = await asyncio.wait_for(
            leg.provider.complete(leg.model, system, [{'role': 'user', 'content': user}]),
            timeout=120.0,
        )
        return raw.strip() or None, False

    except asyncio.TimeoutError:
        return None, False
    except Exception as e:
        err = str(e)
        if '429' in err or 'rate limit' in err.lower():
            m = re.search(r'try again in (\d+)m([\d.]+)s', err)
            secs = int(m.group(1)) * 60 + float(m.group(2)) if m else 60
            return None, secs
        if any(c in err for c in ('503', '502', '529', 'overloaded')):
            return None, 30
        print(f'    [{leg.name}] error: {err[:100]}', flush=True)
        return None, False


async def _delayed_return(pool: asyncio.Queue, leg: Leg, delay: float) -> None:
    await asyncio.sleep(min(delay, 600))
    await pool.put(leg)
    print(f'  [{leg.name}] back in rotation', flush=True)


# ── Carousel curator ──────────────────────────────────────────────────────────

async def _carousel_curate(
    legs: list[Leg],
    entity_type: str,
    batches: list[list[dict]],
    concurrency: int = 3,
) -> list[str]:
    """
    Distribute curator batches across the leg carousel.
    Returns ordered list of markdown sections (one per batch).
    """
    if not legs:
        return []

    sections: dict[int, str] = {}
    work_queue: asyncio.Queue = asyncio.Queue()
    for i, batch in enumerate(batches):
        work_queue.put_nowait((i, batch))

    leg_pool: asyncio.Queue = asyncio.Queue()
    for leg in legs:
        await leg_pool.put(leg)

    lock = asyncio.Lock()

    async def worker() -> None:
        while True:
            leg = await leg_pool.get()
            try:
                idx, batch = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                await leg_pool.put(leg)
                break

            print(
                f'  [{leg.name}] curating {entity_type} batch {idx + 1}/{len(batches)}'
                f' ({len(batch)} records)',
                flush=True,
            )
            section, backoff = await _curate_batch(leg, entity_type, batch)

            if backoff:
                work_queue.put_nowait((idx, batch))
                secs = float(backoff)
                print(f'  [{leg.name}] rate-limited — rotating out for {secs:.0f}s', flush=True)
                asyncio.create_task(_delayed_return(leg_pool, leg, secs))
                continue

            if section:
                async with lock:
                    sections[idx] = section
            else:
                print(f'  [{leg.name}] batch {idx + 1} returned empty — skipped', flush=True)

            await leg_pool.put(leg)

    workers = [
        asyncio.create_task(worker())
        for _ in range(min(concurrency, len(legs), len(batches)))
    ]
    await asyncio.gather(*workers)

    # Return sections in original order
    return [sections[i] for i in sorted(sections)]


# ── Review file writer ────────────────────────────────────────────────────────

def _write_review_file(
    entity_type: str,
    flagged: list[tuple[dict, float]],
    raw_dir: Path,
) -> None:
    """Write low-confidence records to <type>_review.md for human inspection."""
    if not flagged:
        return
    lines = [
        f'# {entity_type.upper()} — Low-Confidence Records (Review Required)',
        f'',
        f'These {len(flagged)} records did not pass the confidence gate.',
        f'Review, enrich, or discard before re-running build_dot.',
        f'',
    ]
    for rec, score in sorted(flagged, key=lambda x: x[1]):
        name = rec.get('name') or rec.get('id') or 'Unknown'
        lines.append(f'## {name}  [score: {score:.2f}]')
        lines.append('```json')
        lines.append(json.dumps(rec, indent=2))
        lines.append('```')
        lines.append('')
    path = raw_dir / f'{_safe_name(entity_type)}_review.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'  Review file: {path}  ({len(flagged)} records)', flush=True)


# ── Leg selection ─────────────────────────────────────────────────────────────

def _build_curator_legs(registry: LegRegistry) -> list[Leg]:
    """
    Prefer larger / more capable models for Mode 2 synthesis.
    Falls back gracefully to whatever is alive.
    """
    active = {l.name: l for l in registry.active_legs()}
    legs: list[Leg] = []
    # Prefer in order: anthropic, openai, gemini, together, cerebras, groq, others
    for name in ['anthropic', 'openai', 'gemini', 'together', 'cerebras', 'sambanova', 'groq', 'openrouter']:
        if name in active:
            legs.append(active[name])
    # Include anything we haven't already added
    for leg in active.values():
        if leg not in legs:
            legs.append(leg)
    return legs


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    topic      = args.topic.lower()
    kb_dir     = KB_ROOT / topic
    entries_dir = kb_dir / 'entries'
    raw_dir    = kb_dir / RAW_SUBDIR
    schema_dir = kb_dir / 'schemas'

    if not entries_dir.exists():
        print(f'ERROR: No entries at {entries_dir}')
        print('Run ingest_corpus.py first.')
        sys.exit(1)

    raw_dir.mkdir(parents=True, exist_ok=True)

    # Load schemas for confidence gating
    schemas: dict[str, dict] = {}
    if schema_dir.exists():
        for sf in schema_dir.glob('*.json'):
            schemas[sf.stem] = json.loads(sf.read_text(encoding='utf-8'))

    # Discover entity types to process
    entity_types = [
        p.name for p in entries_dir.iterdir() if p.is_dir()
    ]
    if args.types:
        entity_types = [t for t in entity_types if t in args.types]

    if not entity_types:
        print('No entity types found to process.')
        sys.exit(0)

    print(f'DOT Artifact Build — {topic}')
    print('=' * 50)
    print(f'  Entity types : {entity_types}')
    print(f'  Confidence   : {args.confidence}')
    print(f'  Batch size   : {args.batch_size}')
    print(f'  Force        : {args.force}')
    print()

    # Boot legs
    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()
    legs = _build_curator_legs(registry)
    if not legs:
        print('ERROR: No API keys. Run add_keys.py first.')
        sys.exit(1)
    print(f'  Curator legs ({len(legs)}): {[l.name for l in legs]}')
    print()

    total_confident = 0
    total_flagged   = 0

    for entity_type in entity_types:
        out_path = raw_dir / f'{_safe_name(entity_type)}.md'

        if out_path.exists() and not args.force:
            print(f'  [{entity_type}] Sub-DOT exists — skipping (--force to rebuild)')
            continue

        entity_dir = entries_dir / entity_type
        schema     = schemas.get(entity_type, {})

        # Load all confident records
        all_records: list[dict] = []
        for f in sorted(entity_dir.glob('*.json')):
            if f.name == '_index.json':
                continue
            try:
                all_records.append(json.loads(f.read_text(encoding='utf-8')))
            except Exception:
                pass

        if not all_records:
            print(f'  [{entity_type}] no records found — skipping')
            continue

        # Confidence gate
        confident, flagged = _gate_records(all_records, schema, args.confidence)
        total_confident += len(confident)
        total_flagged   += len(flagged)

        print(
            f'  [{entity_type}] {len(all_records)} records -> '
            f'{len(confident)} confident, {len(flagged)} flagged',
            flush=True,
        )

        # Write review file for flagged records
        _write_review_file(entity_type, flagged, raw_dir)

        if not confident:
            print(f'  [{entity_type}] no confident records — nothing to curate')
            continue

        # Batch confident records
        batches = [
            confident[i:i + args.batch_size]
            for i in range(0, len(confident), args.batch_size)
        ]
        print(f'  [{entity_type}] {len(batches)} batches × ~{args.batch_size} records each', flush=True)

        # Carousel curator
        sections = await _carousel_curate(
            legs=legs,
            entity_type=entity_type,
            batches=batches,
            concurrency=min(args.concurrency, len(legs)),
        )

        if not sections:
            print(f'  [{entity_type}] curator produced no output — skipping write')
            continue

        # Assemble Sub-DOT
        header = (
            f'# {entity_type.upper()} Sub-DOT — {topic.upper()}\n'
            f'*Curated by build_dot.py — {len(confident)} records*\n'
            f'*Confidence threshold: {args.confidence}  |  '
            f'Flagged for review: {len(flagged)}*\n\n'
        )
        dot_content = header + '\n\n---\n\n'.join(sections)

        out_path.write_text(dot_content, encoding='utf-8')
        print(
            f'  [{entity_type}] Sub-DOT written -> {out_path}'
            f'  ({len(dot_content):,} chars)',
            flush=True,
        )
        print()

    print(f'Build complete.')
    print(f'  Confident records : {total_confident}')
    print(f'  Flagged for review: {total_flagged}')
    print(f'  Sub-DOTs in       : {raw_dir}')
    print()
    print('Next step: python build_gm_persona.py --topic', topic)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Mode 2 artifact builder — curate KB entries into Sub-DOT files'
    )
    p.add_argument('--topic',       required=True,           help='KB topic (e.g. battletech)')
    p.add_argument('--types',       nargs='+',               help='Entity types to build (default: all)')
    p.add_argument('--confidence',  type=float, default=0.35,help='Min field-fill ratio to pass gate (default: 0.35)')
    p.add_argument('--batch-size',  type=int,   default=40,  help='Records per curator batch (default: 40)')
    p.add_argument('--concurrency', type=int,   default=3,   help='Parallel curator workers (default: 3)')
    p.add_argument('--force',       action='store_true',     help='Rebuild Sub-DOTs even if they exist')
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
