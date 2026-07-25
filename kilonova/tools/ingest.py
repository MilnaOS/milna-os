"""
/ingest — Kilonova knowledge base builder.

Pipeline:
  1. Load all PDFs from a folder (via loader.py)
  2. Run each document through Kilonova distillation (multi-leg + reconciler)
  3. Extract entities and structure from the distilled output
  4. Write persistent KB files to kb/<topic>/

KB layout on disk:
  kb/<topic>/
      manifest.json          — what was ingested, when, word counts
      overview.md            — high-level summary of the full corpus
      schemas/               — entity type definitions
          <entity>.json      — one file per discovered entity type
      entries/               — individual entity records
          <entity>/
              <name>.json    — one record per entity instance
      raw/                   — distilled text per source document
          <docname>.md

Usage from milna.py:
  ingestor = Ingestor(engine, kb_root=Path('kb'))
  await ingestor.run(folder=Path('D:/PDF Source/Battletech'), topic='battletech')
"""
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from .loader import load_file, list_folder, extract_pdf_pages, classify_pdf_pages, DEFAULT_BUDGET
from .kb_tape import tag_setting


def _acquire_index_lock(index_path: Path, timeout: int = 30) -> None:
    lock = index_path.with_suffix('.lock')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lock.open('x').close()
            return
        except FileExistsError:
            time.sleep(0.2)
    raise TimeoutError(f'Index lock timeout: {index_path}')


def _release_index_lock(index_path: Path) -> None:
    index_path.with_suffix('.lock').unlink(missing_ok=True)


# For ingestion we want more content per document than a chat /load
INGEST_WORD_BUDGET = 8000
INGEST_MAX_PAGES   = 60


# ── Prompts ────────────────────────────────────────────────────────────────────

DISTILL_SYSTEM = """You are a knowledge extraction engine.
Your job is to read source material and produce a structured research summary.
Be exhaustive. Preserve all named entities, stats, rules, and relationships.
Do not summarize away specific data — numbers, names, and mechanics must be kept.
Output in clean markdown with clear headings."""

DISTILL_USER = """Read the following source document and extract ALL of the following:

1. **Named entities** — characters, units, factions, locations, items, weapons, vehicles, ships, creatures, organizations. For each: name, type, key stats or attributes, relationships to other entities.
2. **Rules and mechanics** — how the system works, dice, modifiers, conditions, turn structure.
3. **Lore and world structure** — factions, history, geography, political relationships.
4. **Tables and stat blocks** — reproduce them completely, do not summarize.

Format your output as structured markdown. Use ## for entity categories, ### for individual entries.

SOURCE DOCUMENT:
{text}"""

SCHEMA_SYSTEM = """You are a data architect.
You will read a distilled research document and define the JSON schemas for every entity type found.
Output ONLY valid JSON. No prose, no markdown fences, just the JSON object."""

SCHEMA_USER = """Read this distilled research document and output a JSON object defining schemas for every entity type present.

Format:
{{
  "<entity_type>": {{
    "description": "what this entity type is",
    "fields": {{
      "<field_name>": {{"type": "<string|number|boolean|array|object>", "description": "..."}}
    }}
  }}
}}

Be specific and exhaustive. For each entity type capture ALL meaningful fields present in the material. Examples:

- BattleMech: name, variant, tonnage, weight_class, weapons (array), heat_sinks, armor, speed_kph, battle_value, cost_cbills, manufacturer, tech_base, era, affiliation
- MechWarrior/Character: name, rank, affiliation, piloting_skill, gunnery_skill, skills (array of {{name, level}}), attributes (str/dex/int etc), xp, notable_actions
- Skill (AToW): name, linked_attribute, base_cost, advancement_cost, specializations (array), description, page_reference
- Weapon: name, damage, heat, range_short, range_medium, range_long, ammo, weight, cost, tech_base
- Faction/House: name, capital, leader, military_strength, notable_units, political_stance, era
- Rule/Mechanic: name, category, description, modifiers, related_skills, page_reference

Do NOT use generic catch-all fields. Derive the actual fields from what is present in the document.
Include entity types for: mechs, characters, skills, weapons, factions, locations, rules, vehicles, equipment, scenarios.

DISTILLED DOCUMENT:
{text}"""

EXTRACT_SYSTEM = """You are a data extraction engine.
You will read source material and extract concrete entity records matching a given schema.
Output ONLY a valid JSON array. Start your response with [ and end with ]. No prose, no markdown, no code fences."""

# Stable prefix — entity type + schema — cached across all chunks for the same entity type
EXTRACT_PREFIX = """Extract all {entity_type} entities mentioned in the source text.

Schema (use these field names exactly):
{schema}

Rules:
- Output a JSON array. If you find entities, output [{{"name": "...", ...}}, ...]. If none found, output [].
- Fill every field you can from the text. Use null for any field not mentioned.
- Partial records are welcome — a record with just a name and one other field is better than nothing.
- Do not add fields not in the schema."""

# Variable suffix — just the source chunk; changes every call
EXTRACT_SUFFIX = """\n\nSOURCE TEXT:\n{text}"""

# Legacy single-string format kept for callers that haven't been updated
EXTRACT_USER = EXTRACT_PREFIX + EXTRACT_SUFFIX

OVERVIEW_SYSTEM = """You are a research synthesizer.
Write a comprehensive overview document from a set of distilled source summaries.
This document will serve as the context anchor for all future AI queries about this topic."""

OVERVIEW_USER = """You have distilled the following source documents for the topic: {topic}

Write a comprehensive OVERVIEW document that:
1. Describes the overall system/world/game
2. Lists all major entity categories and what they are
3. Summarizes key rules and mechanics
4. Maps the relationships between factions, units, and entities
5. Serves as a self-contained reference that grounds all future queries

DISTILLED SUMMARIES:
{summaries}"""


# ── Ingestor ───────────────────────────────────────────────────────────────────

class Ingestor:
    def __init__(self, engine, kb_root: Path = Path('kb')):
        self.engine  = engine   # ConversationEngine — we use its legs directly
        self.kb_root = kb_root

    def _kb_dir(self, topic: str) -> Path:
        d = self.kb_root / topic
        d.mkdir(parents=True, exist_ok=True)
        (d / 'schemas').mkdir(exist_ok=True)
        (d / 'entries').mkdir(exist_ok=True)
        (d / 'raw').mkdir(exist_ok=True)
        return d

    async def _distill_document(self, text: str, doc_name: str, deep: bool = False) -> str:
        """Run one document through all active legs and reconcile."""
        user_msg = DISTILL_USER.format(text=text[:12000])
        result = await self.engine.turn(user_msg, system=DISTILL_SYSTEM, deep=deep)
        self.engine.reset()
        return result.response

    async def _distill_chunked(self, path: Path, doc_name: str, chunk_pages: int = 4, deep: bool = False) -> str:
        """
        Distill a structured document (TRO, unit list) page by page.
        Each chunk of pages gets its own distillation call.
        Models figure out the structure autonomously — no special mode needed.
        Results are concatenated into one raw doc.
        """
        pages = extract_pdf_pages(path)
        if not pages:
            return ''
        chunks = [pages[i:i + chunk_pages] for i in range(0, len(pages), chunk_pages)]
        parts = []
        for i, chunk in enumerate(chunks):
            text = '\n\n'.join(chunk)
            user_msg = DISTILL_USER.format(text=text[:12000])
            result = await self.engine.turn(user_msg, system=DISTILL_SYSTEM, deep=deep)
            self.engine.reset()
            parts.append(result.response)
        return '\n\n---\n\n'.join(parts)

    async def _distill_smart(self, path: Path, doc_name: str, deep: bool = False,
                              progress_cb=None) -> tuple[str, int, int]:
        """
        Differential distillation — only burns API credits where actually needed.

        Classify every page:
          prose       → paste raw text directly (free)
          clean_table → paste text + rendered markdown table (free)
          needs_llm   → batch and send to distillation LLM (API call)

        Consecutive needs_llm pages are grouped into batches (≤ 8 pages) to
        minimise API calls. Returns (distilled_text, llm_pages, total_pages).
        """
        def _prog(msg: str):
            if progress_cb:
                progress_cb(msg)

        pages = classify_pdf_pages(path)
        if not pages:
            return '', 0, 0

        total_pages = len(pages)
        llm_pages   = sum(1 for p in pages if p['type'] == 'needs_llm')
        free_pages  = total_pages - llm_pages

        _prog(f'  {doc_name}: {total_pages} pages — '
              f'{free_pages} free ({round(free_pages/total_pages*100)}%), '
              f'{llm_pages} LLM ({round(llm_pages/total_pages*100)}%)')

        output_parts: list[str] = []
        llm_batch:    list[dict] = []

        async def _flush_batch() -> None:
            if not llm_batch:
                return
            combined  = '\n\n'.join(
                f'[Page {p["page_num"] + 1}]\n{p["text"]}' for p in llm_batch
            )
            user_msg  = DISTILL_USER.format(text=combined[:12000])
            result    = await self.engine.turn(user_msg, system=DISTILL_SYSTEM, deep=deep)
            self.engine.reset()
            output_parts.append(result.response)
            llm_batch.clear()

        for page in pages:
            if page['type'] == 'needs_llm':
                llm_batch.append(page)
                if len(llm_batch) >= 8:
                    await _flush_batch()
            else:
                await _flush_batch()
                parts = [page['text']]
                if page['tables_md']:
                    parts.append(page['tables_md'])
                output_parts.append('\n\n'.join(p for p in parts if p))

        await _flush_batch()

        return '\n\n---\n\n'.join(p for p in output_parts if p.strip()), llm_pages, total_pages

    async def _extract_schemas(self, distilled: str) -> dict:
        """Ask the engine to derive entity schemas from distilled text."""
        user_msg = SCHEMA_USER.format(text=distilled[:8000])
        result = await self.engine.turn(user_msg, system=SCHEMA_SYSTEM, deep=False)
        self.engine.reset()
        # Use isolate outputs directly — reconciler synthesizes prose and destroys JSON
        candidates = [o['text'] for o in result.isolate_outputs] if result.isolate_outputs else [result.response]
        schemas = {}
        for candidate in candidates:
            parsed = _parse_json_safe(candidate, default={})
            if isinstance(parsed, dict):
                schemas.update(parsed)
        return schemas

    async def _extract_entities(self, entity_type: str, schema: dict, text: str, deep: bool = False) -> list[dict]:
        """
        Extract entity records by reading the full text in overlapping chunks.
        Each chunk appends to the running list — treatment stacking pattern.
        Deduplication happens at the end.
        """
        CHUNK_SIZE  = 7000   # chars per chunk
        OVERLAP     = 500    # overlap so records split across chunk boundaries aren't lost
        all_records: list[dict] = []

        chunks = []
        pos = 0
        while pos < len(text):
            chunks.append(text[pos:pos + CHUNK_SIZE])
            pos += CHUNK_SIZE - OVERLAP
            if pos >= len(text):
                break

        # Build a stable prefix (schema + instructions) once per entity type — cached.
        # Only the source text chunk changes per call.
        cached_prefix = EXTRACT_PREFIX.format(
            entity_type=entity_type,
            schema=json.dumps(schema, indent=2),
        )

        for chunk in chunks:
            user_content = [
                {'type': 'text', 'text': cached_prefix, 'cache_control': {'type': 'ephemeral'}},
                {'type': 'text', 'text': EXTRACT_SUFFIX.format(text=chunk)},
            ]
            result = await self.engine.turn(user_content, system=EXTRACT_SYSTEM, deep=deep)
            self.engine.reset()
            # Use raw per-leg outputs — the reconciler synthesizes prose and destroys JSON.
            # Parse each leg's output independently and accumulate; dedup handles overlaps.
            candidates = [o['text'] for o in result.isolate_outputs] if result.isolate_outputs else [result.response]
            for candidate in candidates:
                found = _parse_json_safe(candidate, default=[])
                if isinstance(found, list):
                    for r in found:
                        if isinstance(r, dict):
                            if 'setting' not in r:
                                r['setting'] = tag_setting(r)
                            all_records.append(r)

        return _deduplicate(all_records)

    async def _build_overview(self, topic: str, summaries: list[str]) -> str:
        """Synthesize all distilled summaries into one context anchor document."""
        _FAILURE_PATTERNS = (
            "i don't have enough information",
            "i do not have enough information",
            "not enough information",
            "cannot answer",
            "no information",
        )
        combined = '\n\n---\n\n'.join(
            f'### {i+1}\n{s[:3000]}' for i, s in enumerate(summaries)
        )
        user_msg = OVERVIEW_USER.format(topic=topic, summaries=combined)
        result = await self.engine.turn(user_msg, system=OVERVIEW_SYSTEM, deep=True)
        self.engine.reset()
        response = result.response.strip()
        # Reject synthesis failures — short output or known refusal patterns
        low = response.lower()
        if len(response) < 120 or any(p in low for p in _FAILURE_PATTERNS):
            # Fall back to a stub so we never write garbage to disk
            heads = '\n'.join(f'- {s[:120].splitlines()[0]}' for s in summaries[:8] if s.strip())
            return (
                f'# {topic.title()} Knowledge Base\n\n'
                f'Overview synthesis incomplete (model output insufficient).\n'
                f'Run `/ingest --rebuild {topic} --force` to regenerate.\n\n'
                f'## Source documents loaded ({len(summaries)} total)\n{heads}\n'
            )
        return response

    async def rebuild_overview_only(self, topic: str, progress_cb=None) -> dict:
        """Regenerate just overview.md from existing raw docs. Fast — one API call."""
        def _progress(msg: str):
            if progress_cb:
                progress_cb(msg)

        kb_dir = self._kb_dir(topic)
        raw_dir = kb_dir / 'raw'
        if not raw_dir.exists() or not list(raw_dir.glob('*.md')):
            return {'ok': False, 'error': f'No raw docs found for topic: {topic}'}

        raw_files = sorted(raw_dir.glob('*.md'))
        _progress(f'Loading {len(raw_files)} raw docs...')
        summaries = [f.read_text(encoding='utf-8') for f in raw_files]

        _progress('Synthesizing overview...')
        overview = await self._build_overview(topic, summaries)
        (kb_dir / 'overview.md').write_text(overview, encoding='utf-8')
        _progress('Overview written.')
        return {'ok': True, 'topic': topic, 'docs': len(raw_files)}

    async def rebuild_from_raw(
        self,
        topic: str,
        progress_cb=None,
        progress_tick=None,   # (current: int, total: int, label: str) → None
        deep: bool = False,
        force: bool = False,
    ) -> dict:
        """
        Re-run schema extraction and entity records from existing raw distilled docs.
        Use this when the original ingest produced empty schemas.
        Does NOT re-distill — reads raw/*.md files already on disk.
        """
        def _progress(msg: str):
            if progress_cb:
                progress_cb(msg)

        kb_dir = self._kb_dir(topic)
        raw_dir = kb_dir / 'raw'
        if not raw_dir.exists():
            return {'ok': False, 'error': f'No raw docs found for topic: {topic}'}

        raw_files = sorted(raw_dir.glob('*.md'))
        if not raw_files:
            return {'ok': False, 'error': 'No .md files in raw/ directory.'}

        distilled_docs = []
        for f in raw_files:
            text = f.read_text(encoding='utf-8', errors='ignore')
            distilled_docs.append({'name': f.stem, 'stem': f.stem, 'text': text})
            _progress(f'Loaded raw: {f.name}')

        # Schema extraction — skip if schemas already on disk
        (kb_dir / 'schemas').mkdir(exist_ok=True)
        existing_schemas = list((kb_dir / 'schemas').glob('*.json'))
        if existing_schemas:
            _progress(f'Schemas already on disk ({len(existing_schemas)} types) — loading...')
            schemas = {}
            for f in existing_schemas:
                entity_type = f.stem
                schemas[entity_type] = json.loads(f.read_text(encoding='utf-8'))
                _progress(f'  Loaded schema: {entity_type}')
        else:
            _progress('Deriving entity schemas from raw docs...')
            schemas = await self._extract_schemas(distilled_docs[0]['text'])
            for doc in distilled_docs[1:]:
                more = await self._extract_schemas(doc['text'])
                for entity_type, schema_def in more.items():
                    if entity_type not in schemas:
                        schemas[entity_type] = schema_def
                        _progress(f'  New entity type: {entity_type}')

        if not schemas:
            return {'ok': False, 'error': 'Schema extraction returned empty. Check model output.'}

        for entity_type, schema_def in schemas.items():
            schema_path = kb_dir / 'schemas' / f'{_safe_name(entity_type)}.json'
            schema_path.write_text(json.dumps(schema_def, indent=2), encoding='utf-8')
        _progress(f'  Schemas written: {len(schemas)} entity types')

        # Entity extraction — skip types that already have a completed _index.json
        _progress('Extracting entity records...')
        (kb_dir / 'entries').mkdir(exist_ok=True)
        total_records = 0
        schema_items = list(schemas.items())
        if progress_tick:
            progress_tick(0, len(schema_items), 'starting')
        for i, (entity_type, schema_def) in enumerate(schema_items, 1):
            entity_dir = kb_dir / 'entries' / _safe_name(entity_type)
            entity_dir.mkdir(exist_ok=True)
            index_path = entity_dir / '_index.json'

            if index_path.exists():
                if not force:
                    existing = json.loads(index_path.read_text(encoding='utf-8'))
                    total_records += len(existing)
                    _progress(f'  {entity_type}: {len(existing)} records (already done — skipped)')
                    if progress_tick:
                        progress_tick(i, len(schema_items), f'{entity_type} ✓')
                    continue
                # force=True — wipe existing records and re-extract
                for f in entity_dir.glob('*.json'):
                    f.unlink()
                _progress(f'  {entity_type}: cleared, re-extracting...')

            all_records: list[dict] = []
            for doc in distilled_docs:
                records = await self._extract_entities(entity_type, schema_def, doc['text'], deep=deep)
                if isinstance(records, list):
                    all_records.extend(r for r in records if isinstance(r, dict))
            all_records = _deduplicate(all_records)
            for record in all_records:
                name = record.get('name') or record.get('id') or f'record_{len(all_records)}'
                record_path = entity_dir / f'{_safe_name(str(name))}.json'
                record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
            _acquire_index_lock(index_path)
            try:
                index_path.write_text(json.dumps(all_records, indent=2), encoding='utf-8')
            finally:
                _release_index_lock(index_path)
            total_records += len(all_records)
            _progress(f'  {entity_type}: {len(all_records)} records')
            if progress_tick:
                progress_tick(i, len(schema_items), entity_type)

        # Update manifest
        manifest_path = kb_dir / 'manifest.json'
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        else:
            manifest = {}
        manifest['schemas'] = list(schemas.keys())
        manifest['total_records'] = total_records
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

        return {
            'ok':            True,
            'topic':         topic,
            'kb_dir':        str(kb_dir),
            'entity_types':  len(schemas),
            'total_records': total_records,
        }

    async def run(
        self,
        folder: Path,
        topic: str,
        files: list[Path] | None = None,   # pre-selected files; if None, scans folder
        progress_cb=None,
        deep_distill: bool = False,
    ) -> dict:
        """
        Full ingest pipeline. Returns summary dict with counts and kb path.
        progress_cb is called with status strings so milna.py can display them.
        files: explicit list from the picker — already filtered to exclude DOT entries.
        """
        def _progress(msg: str):
            if progress_cb:
                progress_cb(msg)

        kb_dir = self._kb_dir(topic)
        if files is None:
            files = list_folder(folder)

        # Load existing manifest if this topic already has one (incremental ingest)
        manifest_path = kb_dir / 'manifest.json'
        if manifest_path.exists():
            import json as _json
            manifest = _json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['ingested_at'] = datetime.utcnow().isoformat()
        else:
                manifest = {
                'topic':         topic,
                'ingested_at':   datetime.utcnow().isoformat(),
                'source_folder': str(folder),
                'documents':     [],
            }

        # ── Step 1: Distill each document — skip if already on disk ───────────
        distilled_docs: list[dict] = []
        for f in files:
            raw_path = kb_dir / 'raw' / (f.stem + '.md')

            if raw_path.exists():
                _progress(f'Already distilled: {f.name} — loading from disk')
                distilled = raw_path.read_text(encoding='utf-8')
                distilled_docs.append({'name': f.name, 'stem': f.stem, 'text': distilled})
                manifest['documents'].append({
                    'file':       f.name,
                    'words_read': 0,
                    'truncated':  False,
                    'raw_kb':     str(raw_path.relative_to(self.kb_root)),
                })
                continue

            _progress(f'Reading {f.name}...')

            if f.suffix.lower() == '.pdf':
                # Smart differential distillation — free for prose/tables, LLM only for complex pages
                mode_tag = ' [deep]' if deep_distill else ''
                _progress(f'Classifying{mode_tag} {f.name}...')
                distilled, llm_pages, total_pages = await self._distill_smart(
                    f, f.name, deep=deep_distill, progress_cb=_progress)
                if not distilled:
                    _progress(f'  Skipped {f.name}: no extractable text')
                    continue
                words_used = len(distilled.split())
                truncated  = False
            else:
                # Plain text / markdown — read directly, no distillation needed
                loaded = load_file(f, budget_words=INGEST_WORD_BUDGET)
                if not loaded['ok']:
                    _progress(f'  Skipped {f.name}: {loaded["error"]}')
                    continue
                _progress(f'Loaded {f.name} ({loaded["words_used"]:,} words)')
                distilled  = loaded['text']
                words_used = loaded['words_used']
                truncated  = loaded['truncated']

            # Write raw distilled text
            raw_path.write_text(distilled, encoding='utf-8')

            distilled_docs.append({'name': f.name, 'stem': f.stem, 'text': distilled})
            manifest['documents'].append({
                'file':       f.name,
                'words_read': words_used,
                'truncated':  truncated,
                'raw_kb':     str(raw_path.relative_to(self.kb_root)),
            })
            _progress(f'  Done: {f.name}')

        if not distilled_docs:
            return {'ok': False, 'error': 'No documents successfully distilled.'}

        # ── Step 2: Load existing schemas or derive new ones ─────────────────
        (kb_dir / 'schemas').mkdir(exist_ok=True)
        existing_schema_files = list((kb_dir / 'schemas').glob('*.json'))
        schemas = {}

        if existing_schema_files:
            _progress(f'Loading existing schemas ({len(existing_schema_files)} types)...')
            for sf in existing_schema_files:
                schemas[sf.stem] = json.loads(sf.read_text(encoding='utf-8'))
                _progress(f'  Loaded: {sf.stem}')
            # Check new docs for additional entity types not yet in schemas
            for doc in distilled_docs:
                more = await self._extract_schemas(doc['text'])
                for entity_type, schema_def in more.items():
                    if entity_type not in schemas:
                        schemas[entity_type] = schema_def
                        schema_path = kb_dir / 'schemas' / f'{_safe_name(entity_type)}.json'
                        schema_path.write_text(json.dumps(schema_def, indent=2), encoding='utf-8')
                        _progress(f'  New entity type discovered: {entity_type}')
        else:
            _progress('Deriving entity schemas...')
            for doc in distilled_docs:
                more = await self._extract_schemas(doc['text'])
                for entity_type, schema_def in more.items():
                    if entity_type not in schemas:
                        schemas[entity_type] = schema_def
                        _progress(f'  New entity type: {entity_type}')
            for entity_type, schema_def in schemas.items():
                schema_path = kb_dir / 'schemas' / f'{_safe_name(entity_type)}.json'
                schema_path.write_text(json.dumps(schema_def, indent=2), encoding='utf-8')
            _progress(f'  Schemas written: {len(schemas)} entity types')

        # ── Step 3: Extract entity records — merge with existing ──────────────
        _progress('Extracting entity records...')
        total_records = 0
        for entity_type, schema_def in schemas.items():
            entity_dir = kb_dir / 'entries' / _safe_name(entity_type)
            entity_dir.mkdir(exist_ok=True)
            index_path = entity_dir / '_index.json'

            # Load existing records for this entity type
            existing_records = []
            if index_path.exists():
                existing_records = [r for r in json.loads(index_path.read_text(encoding='utf-8')) if isinstance(r, dict)]
                existing_names = {r.get('name') or r.get('id') for r in existing_records}
            else:
                existing_names = set()

            all_records: list[dict] = list(existing_records)

            for doc in distilled_docs:
                records = await self._extract_entities(entity_type, schema_def, doc['text'])
                if isinstance(records, list):
                    all_records.extend(r for r in records if isinstance(r, dict))

            # Deduplicate against existing + new
            all_records = _deduplicate(all_records)

            # Write new records; overwrite existing ones whose content changed
            new_count = 0
            updated_count = 0
            for record in all_records:
                name = record.get('name') or record.get('id') or f'record_{len(all_records)}'
                record_path = entity_dir / f'{_safe_name(str(name))}.json'
                new_content = json.dumps(record, indent=2)
                if name not in existing_names:
                    record_path.write_text(new_content, encoding='utf-8')
                    new_count += 1
                elif not record_path.exists() or record_path.read_text(encoding='utf-8') != new_content:
                    record_path.write_text(new_content, encoding='utf-8')
                    updated_count += 1

            _acquire_index_lock(index_path)
            try:
                index_path.write_text(json.dumps(all_records, indent=2), encoding='utf-8')
            finally:
                _release_index_lock(index_path)
            total_records += len(all_records)
            if existing_records:
                parts_msg = [f'{new_count} new']
                if updated_count:
                    parts_msg.append(f'{updated_count} updated')
                _progress(f'  {entity_type}: {len(existing_records)} existing + {", ".join(parts_msg)} = {len(all_records)} total')
            else:
                _progress(f'  {entity_type}: {len(all_records)} records')

        # ── Step 4: Build overview from ALL raw docs in kb ────────────────────
        _progress('Building context anchor document (overview)...')
        all_raw = sorted((kb_dir / 'raw').glob('*.md'))
        summaries = [f.read_text(encoding='utf-8') for f in all_raw]
        overview  = await self._build_overview(topic, summaries)
        overview_path = kb_dir / 'overview.md'
        overview_path.write_text(overview, encoding='utf-8')
        _progress('  Overview written.')

        # ── Step 5: Write manifest ─────────────────────────────────────────────
        manifest['schemas']       = list(schemas.keys())
        manifest['total_records'] = total_records
        manifest['overview']      = str(overview_path.relative_to(self.kb_root))
        (kb_dir / 'manifest.json').write_text(
            json.dumps(manifest, indent=2), encoding='utf-8')

        return {
            'ok':            True,
            'topic':         topic,
            'kb_dir':        str(kb_dir),
            'documents':     len(distilled_docs),
            'entity_types':  len(schemas),
            'total_records': total_records,
            'overview':      str(overview_path),
        }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_json_safe(text: str, default):
    """Extract and parse JSON from model output. Models sometimes wrap in markdown."""
    # Strip markdown fences — handle ```json, ```JSON, ``` etc.
    clean = re.sub(r'```[a-zA-Z]*\s*', '', text.strip())
    clean = clean.strip()

    def _valid(val):
        return val if val is not None else default

    # Try direct parse first
    try:
        return _valid(json.loads(clean))
    except json.JSONDecodeError:
        pass

    # Try to find the largest JSON object or array in the text
    for pattern in (r'\{[\s\S]*\}', r'\[[\s\S]*\]'):
        matches = list(re.finditer(pattern, clean))
        # Try from largest match to smallest (greedy first)
        for m in sorted(matches, key=lambda x: len(x.group()), reverse=True):
            try:
                return _valid(json.loads(m.group()))
            except json.JSONDecodeError:
                continue

    # Last resort — try to fix common issues (trailing commas, single quotes)
    try:
        fixed = re.sub(r',\s*([}\]])', r'\1', clean)   # trailing commas
        fixed = fixed.replace("'", '"')                  # single → double quotes
        return _valid(json.loads(fixed))
    except (json.JSONDecodeError, Exception):
        pass

    return default


def _safe_name(name: str) -> str:
    """Convert any string to a safe filename."""
    return re.sub(r'[^\w\-]', '_', name.lower()).strip('_')[:64]


def _deduplicate(records: list[dict]) -> list[dict]:
    """Deduplicate by name/id (case-insensitive) — last occurrence wins."""
    seen: dict[str, int] = {}
    out: list[dict] = []
    for r in records:
        raw_key = r.get('name') or r.get('id')
        key = str(raw_key).strip().lower() if raw_key else None
        if key:
            if key in seen:
                out[seen[key]] = r
            else:
                seen[key] = len(out)
                out.append(r)
        else:
            out.append(r)
    return out
