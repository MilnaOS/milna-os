# Milna OS — Project Context for Claude Code

## What this project is

**Milna OS** is a BYOK (Bring Your Own Keys) multi-provider AI runtime. Users punch in API keys (Anthropic, OpenAI, Gemini, Ollama) and Milna becomes a meta-agent — the sum of every key they hold. No telemetry, no API surcharge, no gated output. $700 one-time license, users own the factory.

**Kilonova** is the engine inside Milna OS. It handles: leg management (one leg per provider), multi-isolate parallel firing, reconciliation into a single coherent response, conversation history, and the /ingest pipeline.

**Grom** is the internal name for the parallel execution step (not user-facing).

## Stack

- Python 3.11+, async throughout (asyncio)
- `rich` for all terminal output (panels, tables, status spinners)
- `keyring` for API key storage (Windows Credential Manager)
- `pdfplumber` for PDF extraction
- All LLM calls go through `kilonova/core/` provider abstraction — never raw API calls in milna.py

## Project layout

```
D:\Runtime\
├── milna.py                    # CLI entry point / REPL
├── kb/                         # Persistent knowledge bases (DOTs)
│   └── <topic>/
│       ├── manifest.json
│       ├── overview.md         # Context anchor document
│       ├── schemas/            # Entity schemas (JSON)
│       ├── entries/            # Extracted records
│       └── raw/                # Distilled source docs (.md)
└── kilonova/
    ├── core/
    │   ├── legs.py             # Provider abstraction, KeyInventory, LegRegistry
    │   ├── mode.py             # potato/economy/standard/blade mode controller
    │   ├── engine.py           # ConversationEngine, /deep distillation, reconciler
    │   └── session_log.py      # Audit trail (no content logged, just metadata)
    └── tools/
        ├── loader.py           # /load command — PDF extraction, context injection
        └── ingest.py           # /ingest pipeline — distill → schema → extract → KB
```

## Core design patterns

**Legs → Isolates → Reconciler**: Each provider key = one leg. Mode controls isolates-per-leg (potato=1, economy=2, standard=3-4, blade=N). All isolates fire in parallel. Reconciler synthesizes into one response.

**DOT (Document of Truth)**: The KB is not RAG. Kilonova distills source documents into a consensus overview (the context anchor), then extracts structured entities. The overview is injected whole into conversation history via `/kb <topic>` — deliberate anchoring, not vector retrieval.

**Fully resumable pipeline**: Every ingest step checks disk before doing work. Kill it with Ctrl+C anywhere, restart, it picks up exactly where it stopped. Zero rework, zero extra API cost.

## Code style rules

- Async throughout — if it touches a provider, it's `async def`
- `rich` for all output — no bare `print()` in milna.py
- No bare `except:` — catch specific exceptions or `Exception as e`
- Progress callbacks (`progress_cb`) pass a string, never block
- Entity extraction never wipes existing records — always merge
- JSON parsing always goes through `_parse_json_safe()` in ingest.py — models wrap output in markdown

## Current state

Phase 3 complete: /ingest pipeline built and running. Battletech DOT live with 20 sourcebooks, 11 entity types, 211 records.

Next: AML schema tuning pass (reads raw docs → rewrites schemas with Battletech-specific fields), then TRO ingest to flood BattleMech records.

## What NOT to do

- Don't add error handling for things that can't happen
- Don't add comments explaining what the code does — only comment the WHY when non-obvious
- Don't create new files when editing an existing one is correct
- Don't use `print()` — always `console.print()` from the Rich instance at the top of milna.py
- Don't re-distill if raw docs exist — the resumability guarantee is load-bearing
