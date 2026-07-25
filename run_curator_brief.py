"""
run_curator_brief.py

Fires the persona_curator implementation brief through Milna's specialist round.
Three providers analyze the spec in parallel; synthesizer produces final implementation.

Run: python D:\Runtime\run_curator_brief.py
"""
import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.engine import ConversationEngine
from kilonova.core.legs import LegRegistry, KeyInventory
from kilonova.core.mode import ModeController
from kilonova.core.specialist_prompts import get_specialist_configs

# ── Brief ─────────────────────────────────────────────────────────────────────

BRIEF = """\
Write a complete Python implementation of `kilonova/tools/persona_curator.py`.

## Context

Kilonova is a multi-provider AI engine (D:/Runtime/). It has:
- `ConversationEngine` in `kilonova/core/engine.py`
  - `specialist_round(user_prompt, specialist_configs, dot_content='') -> dict[str, dict]`
    - fires N specialist providers in parallel, each with its own system prompt
    - returns role_id → {role, leg, text, artifact, ok, error?}
  - `_extract_json_artifact(text) -> dict` — strips markdown fences, parses JSON
  - `registry.legs` — dict[str, Leg]; `leg.alive: bool`
- `LegRegistry` in `kilonova/core/leg_registry.py`
  - Each leg has: `leg.provider.complete(model, system, messages) -> str`
- `kilonova/core/specialist_prompts.py`
  - `get_specialist_configs(registry) -> dict[str, dict]`
    - assigns narrative→anthropic, mechanics→openai, philosophy→gemini with fallbacks
    - returns role_id → {leg_name, system_prompt}

## The Tool to Implement

`kilonova/tools/persona_curator.py` — the final stage of the GM Persona build pipeline.

### What it does

1. Receives `specialist_outputs: dict[str, dict]` — output of `engine.specialist_round()`
   - Keys: 'narrative', 'mechanics', 'philosophy' (some may be missing if that leg failed)
   - Each value: {role, leg, text, artifact (parsed JSON or None), ok, error?}
2. Filters to successful outputs only (`out['ok'] == True and out['artifact'] is not None`)
3. Fires ONE curator LLM call on the best available leg (prefer anthropic > openai > gemini)
4. The curator synthesizes all specialist artifacts into a single GM Persona JSON file
5. Stamps metadata onto the result
6. Validates that `dotPath` entries in `routingRules` actually exist on disk
7. Returns the assembled persona dict

### GM Persona output schema

The persona file is a routing document for a small play-time LLM. It contains:

```json
{
  "meta": {
    "personaId": "string",
    "moduleId": "string",
    "personaVersion": "1.0.0",
    "createdAt": "ISO8601",
    "specialists": ["narrative", "mechanics", "philosophy"],
    "dotTopic": "string"
  },
  "coreInstructions": {
    "identity": "who the GM is",
    "authority": "where knowledge comes from (the DOT)",
    "failureMode": "what to do when uncertain"
  },
  "voiceFramework": {
    "overallTone": "string",
    "sentenceStructure": "string",
    "pacing": {
      "exploration": "string",
      "combat": "string",
      "downtime": "string"
    },
    "avoid": ["things the GM never does"]
  },
  "routingRules": [
    {
      "id": "string",
      "trigger": "what player input activates this rule",
      "dotPath": "relative path in kb/<topic>/",
      "fallbackChain": ["string"],
      "voiceOverride": "string or null",
      "domain": "narrative|mechanics|philosophy",
      "confidence": 0.85
    }
  ],
  "factionVoices": [
    {
      "factionId": "string",
      "factionName": "string",
      "tone": "string",
      "speechPatterns": ["string"],
      "example": "string"
    }
  ],
  "fallbackProtocol": {
    "routingFailure": "what to say when no rule matches",
    "dotMissing": "what to say when DOT path not found",
    "lowConfidence": "what to say when uncertain"
  },
  "curatorNotes": "conflicts resolved, decisions made, open issues"
}
```

### Conflict resolution rules (bake into curator system prompt)

- Narrative decisions: narrative 60%, philosophy 30%, mechanics 10%
- Mechanical decisions: mechanics 70%, narrative 20%, philosophy 10%
- Faction decisions: philosophy 50%, narrative 35%, mechanics 15%

### Class interface

```python
class PersonaCurator:
    def __init__(self, engine): ...

    async def synthesize(
        self,
        topic: str,
        specialist_outputs: dict[str, dict],
        dot_overview: str,
        kb_dir: Path,
        module_id: str | None = None,
    ) -> dict:
        # Returns a persona dict (never raises — returns stub on failure)
        ...

    def _validate_routing(self, persona: dict, kb_dir: Path) -> dict:
        # Checks dotPath entries exist on disk; flags missing with _warning key
        ...

    @staticmethod
    def _stub_persona(topic: str, module_id: str | None, error: str) -> dict:
        # Minimal stub returned on synthesis failure
        ...
```

### Module-level helper

```python
def load_dot_content(kb_dir: Path, max_chars: int = 20000) -> str:
    # Combines overview.md + first 10 raw/*.md files into a single string
    # Truncates to max_chars to stay under token limits
    ...
```

## Requirements

- Never raises — all failures return a stub persona
- Stub persona has `meta.status = 'stub'` and `meta.error` set
- Curator call fires on anthropic > openai > gemini (first alive)
- `_validate_routing` strips `#fragment` before checking file existence
- Missing dotPaths append to `curatorNotes` with a VALIDATION WARNINGS block
- Curator system prompt is a module-level constant (`CURATOR_SYSTEM`)
- Domain weights are a module-level constant (`DOMAIN_WEIGHTS`)
- All imports at the top; no circular imports

## Output

Return the complete `persona_curator.py` file. No prose before or after — only the
Python source code.
"""

# ── System prompts for the three specialists ──────────────────────────────────

BRIEF_NARRATIVE_SYSTEM = """\
You are a senior Python architect reviewing an implementation brief.
Focus on: API design clarity, error handling completeness, async correctness,
class interface usability, and whether the stub/fallback pattern is well-designed.
Identify any gaps in the brief that would cause implementation ambiguity.
Output structured JSON:
{
  "focus": "narrative",
  "claims": ["string — design observation"],
  "routing_rules": [{"trigger": "...", "destination": "...", "rationale": "..."}],
  "voice_examples": [{"scenario": "...", "response": "..."}],
  "conflicts": [{"rule1": "...", "rule2": "...", "decision_rationale": "..."}],
  "unique_insight": "string",
  "confidence": "high"
}
Return ONLY JSON."""

BRIEF_MECHANICS_SYSTEM = """\
You are a senior Python engineer focused on correctness and implementation detail.
Focus on: the exact Python code needed for each method, edge cases in JSON parsing,
Path handling on Windows, async/await correctness, leg resolution order,
dotPath fragment stripping, and curatorNotes string assembly.
Output structured JSON:
{
  "focus": "mechanics",
  "claims": ["string — implementation note"],
  "routing_rules": [{"trigger": "...", "destination": "...", "rationale": "..."}],
  "voice_examples": [{"scenario": "...", "response": "..."}],
  "conflicts": [{"rule1": "...", "rule2": "...", "decision_rationale": "..."}],
  "unique_insight": "string",
  "confidence": "high"
}
Return ONLY JSON."""

BRIEF_PHILOSOPHY_SYSTEM = """\
You are a systems architect focused on integration fit and long-term maintainability.
Focus on: how PersonaCurator integrates with ConversationEngine, whether the
curator-system-prompt design is correct, whether conflict resolution weights
belong in the system prompt vs. in code, and how this fits the broader
DOT → specialist_round → curator → persona pipeline.
Output structured JSON:
{
  "focus": "philosophy",
  "claims": ["string — architectural observation"],
  "routing_rules": [{"trigger": "...", "destination": "...", "rationale": "..."}],
  "voice_examples": [{"scenario": "...", "response": "..."}],
  "conflicts": [{"rule1": "...", "rule2": "...", "decision_rationale": "..."}],
  "unique_insight": "string",
  "confidence": "high"
}
Return ONLY JSON."""

# ── Synthesizer system ────────────────────────────────────────────────────────

SYNTH_SYSTEM = """\
You are the implementation synthesizer. You receive three specialist code reviews
of a Python implementation brief, then write the final implementation.

Rules:
1. Write the COMPLETE persona_curator.py file — every line, no placeholders.
2. The file must be valid Python 3.11+.
3. Incorporate insights from all three specialists.
4. Where specialists conflict, prefer the mechanics specialist on implementation
   details and the philosophy specialist on API design.
5. Return ONLY the Python source code — no prose, no markdown, no fences.
"""


async def main():
    print('Persona Curator — Implementation Brief via Milna')
    print('=' * 50)

    inventory = KeyInventory()
    registry = LegRegistry(inventory)
    registry.build()

    alive = [n for n, leg in registry.legs.items() if leg.alive]
    if not alive:
        print('ERROR: No alive legs. Check your API keys.')
        sys.exit(1)

    print(f'  Legs: {", ".join(alive)}')

    mc = ModeController()

    # Pick reconciler leg: prefer anthropic
    rec_leg = (
        registry.legs.get('anthropic') or
        registry.legs.get('openai') or
        registry.legs.get('gemini')
    )
    rec_fallbacks = [
        (leg.provider, leg.model)
        for name, leg in registry.legs.items()
        if leg.alive and leg is not rec_leg
    ]

    engine = ConversationEngine(
        leg_registry=registry,
        mode_controller=mc,
        reconciler_provider=rec_leg.provider,
        reconciler_model=rec_leg.model,
        reconciler_fallbacks=rec_fallbacks,
    )

    # Build specialist configs with the brief-specific system prompts
    base_configs = get_specialist_configs(registry)
    specialist_configs = {}
    prompts = {
        'narrative':  BRIEF_NARRATIVE_SYSTEM,
        'mechanics':  BRIEF_MECHANICS_SYSTEM,
        'philosophy': BRIEF_PHILOSOPHY_SYSTEM,
    }
    for role_id, cfg in base_configs.items():
        specialist_configs[role_id] = {
            'leg_name':     cfg['leg_name'],
            'system_prompt': prompts.get(role_id, cfg['system_prompt']),
        }

    print('\n>> Firing specialist isolates...')
    outputs = await engine.specialist_round(
        user_prompt=BRIEF,
        specialist_configs=specialist_configs,
    )

    legs_used = [f"{r}:{o.get('leg','?')}" for r, o in outputs.items()]
    print(f'   Isolates done (legs: {", ".join(legs_used)})')
    print('>> Synthesizing implementation...')

    # Build synthesis prompt
    specialist_block = ''
    for role_id, out in outputs.items():
        if out.get('ok'):
            specialist_block += f'\n\n### {role_id.upper()} SPECIALIST\n\n{out.get("text","")}'
        else:
            specialist_block += f'\n\n### {role_id.upper()} SPECIALIST\n\nFailed: {out.get("error","unknown")}'

    synth_prompt = (
        f'## IMPLEMENTATION BRIEF\n\n{BRIEF}\n\n'
        f'## SPECIALIST REVIEWS\n\n{specialist_block}\n\n'
        f'Write the complete persona_curator.py implementation now.'
    )

    # Fire synthesis on best available leg
    synth_leg = (
        registry.legs.get('anthropic') or
        registry.legs.get('openai') or
        registry.legs.get('gemini')
    )
    if not synth_leg or not synth_leg.alive:
        print('ERROR: No synthesis leg available.')
        sys.exit(1)

    synth_text = await synth_leg.provider.complete(
        synth_leg.model,
        SYNTH_SYSTEM,
        [{'role': 'user', 'content': synth_prompt}],
    )

    out_path = Path(__file__).parent / 'persona_curator_milna.py'
    out_path.write_text(synth_text, encoding='utf-8')

    ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f'   Done. ({ts})')
    print(f'\nOutput written to:\n  {out_path}')


if __name__ == '__main__':
    asyncio.run(main())
