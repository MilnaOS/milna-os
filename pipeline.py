"""
pipeline.py — Automated multi-phase crawl + ingest pipeline.

Two phase modes:
  once    — full crawl until exhausted, then full ingest.
  rolling — crawl `batch_size` pages → ingest → repeat until no new pages.

Each phase can set `merge_into` to copy its entries into a canonical KB dir
after each ingest pass (e.g. "battletech").

Crash-resumable: corpus.visited skips re-crawled URLs; existing entry files
skip re-extraction.

Usage:
    python pipeline.py --config pipeline_battletech.json
    python pipeline.py --config pipeline_battletech.json --start-phase bt_equipment
"""
import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs                       import LegRegistry, KeyInventory, Leg
from kilonova.core.mode                       import ModeController
from kilonova.core.engine                     import ConversationEngine
from kilonova.core.providers.ollama_provider  import OllamaProvider
from kilonova.tools.corpus_collector          import CorpusCollector
from kilonova.tools.ingest                    import _parse_json_safe, _safe_name

from ingest_corpus import (
    _load_pages,
    _build_extraction_legs,
    _gatling_extract,
    _build_index,
    _url_slug,
)
from scrape_corpus import _URL_FILTERS, _build_parser_legs


# ── Utilities ─────────────────────────────────────────────────────────────────

def _load_schemas(schema_dir: Path, types_filter: list | None = None) -> dict:
    schemas = {}
    for sf in sorted(schema_dir.glob('*.json')):
        if types_filter and sf.stem not in types_filter:
            continue
        try:
            schemas[sf.stem] = json.loads(sf.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'  WARNING: could not load schema {sf.name}: {e}', flush=True)
    return schemas


def _count_pages(corpus_path: Path) -> int:
    """Count source pages in corpus.txt without loading the whole file."""
    if not corpus_path.exists():
        return 0
    count = 0
    with open(corpus_path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            if line.startswith('[') and '] SOURCE:' in line:
                count += 1
    return count


def _merge_entries(src_kb: Path, dst_kb: Path) -> int:
    """Copy new entry files from src_kb/entries/ into dst_kb/entries/.
    Skips files that already exist in the destination (no overwrite).
    """
    copied = 0
    src_entries = src_kb / 'entries'
    if not src_entries.exists():
        return 0
    for type_dir in src_entries.iterdir():
        if not type_dir.is_dir():
            continue
        dst_type_dir = dst_kb / 'entries' / type_dir.name
        dst_type_dir.mkdir(parents=True, exist_ok=True)
        for f in type_dir.glob('*.json'):
            if f.name == '_index.json':
                continue
            dst = dst_type_dir / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                copied += 1
    return copied


def _rebuild_indexes(dst_kb: Path, types: list) -> None:
    for t in types:
        entity_dir = dst_kb / 'entries' / _safe_name(t)
        if entity_dir.exists():
            records = _build_index(entity_dir)
            print(f'  [{t}] index rebuilt → {len(records)} records', flush=True)


# ── Engine / collector factory ────────────────────────────────────────────────

def _build_engine_and_registry():
    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()
    active = registry.active_legs()
    if not active:
        print('ERROR: No API keys found. Run add_keys.py first.')
        sys.exit(1)
    mode   = ModeController('potato')
    leg    = active[0]
    engine = ConversationEngine(
        leg_registry=registry,
        mode_controller=mode,
        reconciler_provider=leg.provider,
        reconciler_model=leg.model,
    )
    return engine, registry


def _make_collector(engine, registry, corpus_path: Path, phase: dict) -> CorpusCollector:
    parser_legs = _build_parser_legs(registry)
    concurrency = min(phase.get('crawl_concurrency', 4), len(parser_legs))
    url_filter  = _URL_FILTERS.get(phase.get('url_filter', ''))
    return CorpusCollector(
        engine=engine,
        corpus_path=corpus_path,
        parser_leg=parser_legs[:concurrency],
        concurrency=concurrency,
        delay_between_requests=phase.get('delay', 2.0),
        url_filter=url_filter,
    )


# ── Phase: once ───────────────────────────────────────────────────────────────

async def run_once_phase(phase: dict, engine, registry) -> None:
    name       = phase['name']
    topic      = phase['topic']
    kb_dir     = Path('kb') / topic
    corpus_path = kb_dir / 'corpus.txt'
    schema_dir  = kb_dir / 'schemas'
    merge_into  = phase.get('merge_into')

    kb_dir.mkdir(parents=True, exist_ok=True)

    _banner(f'PHASE [{name}]  mode=once  topic={topic}')

    # ── Crawl ─────────────────────────────────────────────────────────────────
    print(f'\n--- Crawl ---', flush=True)
    collector = _make_collector(engine, registry, corpus_path, phase)
    await collector.collect(
        seed_urls=phase['seeds'],
        max_pages=phase.get('max_pages', 0),
        discover_links=True,
        shuffle=phase.get('shuffle', False),
        verbose=True,
    )
    total_pages = _count_pages(corpus_path)
    print(f'\n  Corpus total: {total_pages} pages', flush=True)

    # ── Ingest ────────────────────────────────────────────────────────────────
    print(f'\n--- Ingest ---', flush=True)
    schemas = _load_schemas(schema_dir, phase.get('types'))
    if not schemas:
        print(f'  WARNING: no schemas in {schema_dir} — skipping ingest.', flush=True)
        return

    pages = _load_pages(corpus_path)
    legs  = _build_extraction_legs(registry)
    counts = await _gatling_extract(
        legs=legs,
        schemas=schemas,
        pages=pages,
        kb_dir=kb_dir,
        force=False,
        concurrency=phase.get('ingest_concurrency', 6),
    )
    print(f'\n  Counts: {counts}', flush=True)

    # ── Merge ─────────────────────────────────────────────────────────────────
    if merge_into:
        _do_merge(kb_dir, merge_into, list(schemas.keys()))

    _banner(f'PHASE [{name}] complete')


# ── Phase: rolling ────────────────────────────────────────────────────────────

async def run_rolling_phase(phase: dict, engine, registry) -> None:
    name        = phase['name']
    topic       = phase['topic']
    kb_dir      = Path('kb') / topic
    corpus_path = kb_dir / 'corpus.txt'
    schema_dir  = kb_dir / 'schemas'
    merge_into  = phase.get('merge_into')
    batch_size  = phase.get('batch_size', 400)
    max_batches = phase.get('max_batches', 0)

    kb_dir.mkdir(parents=True, exist_ok=True)

    _banner(f'PHASE [{name}]  mode=rolling  batch={batch_size}  topic={topic}')

    schemas = _load_schemas(schema_dir, phase.get('types'))
    if not schemas:
        print(f'  WARNING: no schemas in {schema_dir} — aborting phase.', flush=True)
        return

    legs  = _build_extraction_legs(registry)
    batch = 0

    while True:
        batch += 1
        if max_batches and batch > max_batches:
            print(f'\n  Max batches ({max_batches}) reached.', flush=True)
            break

        # ── Crawl batch ───────────────────────────────────────────────────────
        print(f'\n--- Batch {batch}: crawl (up to {batch_size} new pages) ---', flush=True)
        pages_before = _count_pages(corpus_path)

        collector = _make_collector(engine, registry, corpus_path, phase)
        await collector.collect(
            seed_urls=phase['seeds'],
            max_pages=batch_size,
            discover_links=True,
            shuffle=phase.get('shuffle', True),
            verbose=True,
        )

        pages_after = _count_pages(corpus_path)
        new_pages   = pages_after - pages_before
        print(f'\n  New pages this batch: {new_pages}  (total: {pages_after})', flush=True)

        if new_pages == 0:
            print(f'  Crawl exhausted — no new URLs remain.', flush=True)
            break

        # ── Ingest batch ──────────────────────────────────────────────────────
        print(f'\n--- Batch {batch}: ingest ---', flush=True)
        pages  = _load_pages(corpus_path)
        counts = await _gatling_extract(
            legs=legs,
            schemas=schemas,
            pages=pages,
            kb_dir=kb_dir,
            force=False,
            concurrency=phase.get('ingest_concurrency', 6),
        )
        print(f'\n  Counts this run: {counts}', flush=True)

        # ── Merge after each batch ────────────────────────────────────────────
        if merge_into:
            _do_merge(kb_dir, merge_into, list(schemas.keys()))

    _banner(f'PHASE [{name}] complete')


# ── Shared helpers ────────────────────────────────────────────────────────────

def _do_merge(src_kb: Path, merge_into: str, types: list) -> None:
    dst_kb = Path('kb') / merge_into
    dst_kb.mkdir(parents=True, exist_ok=True)
    copied = _merge_entries(src_kb, dst_kb)
    print(f'\n  Merged {copied} new entries → kb/{merge_into}/', flush=True)
    _rebuild_indexes(dst_kb, types)


def _banner(msg: str) -> None:
    bar = '=' * 60
    print(f'\n{bar}', flush=True)
    print(msg, flush=True)
    print(bar, flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    config_path = Path(args.config)
    if not config_path.exists():
        print(f'ERROR: Config not found: {config_path}')
        sys.exit(1)

    phases = json.loads(config_path.read_text(encoding='utf-8'))
    engine, registry = _build_engine_and_registry()

    start_phase = args.start_phase
    started     = (start_phase is None)

    for phase in phases:
        name = phase.get('name', '?')
        if not started:
            if name == start_phase:
                started = True
            else:
                print(f'[skip] {name}', flush=True)
                continue

        mode = phase.get('mode', 'once')
        if mode == 'once':
            await run_once_phase(phase, engine, registry)
        elif mode == 'rolling':
            await run_rolling_phase(phase, engine, registry)
        else:
            print(f'Unknown mode "{mode}" for phase "{name}" — skipping.', flush=True)

    _banner('ALL PHASES COMPLETE')


def _parse_args():
    p = argparse.ArgumentParser(description='Automated crawl + ingest pipeline')
    p.add_argument('--config',      required=True, help='JSON config file (list of phase objects)')
    p.add_argument('--start-phase', help='Skip phases before this name (resume support)')
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
