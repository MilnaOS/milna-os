"""
kilonova/tools/persona_curator.py

Final stage of the GM Persona build pipeline. Synthesizes specialist outputs
(narrative, mechanics, philosophy) from specialist_round() into a single
routing document for the play-time LLM.

Four-pass independent architecture — each pass handles one domain, then
Pass 4 merges all three outputs. No pass ever carries accumulated context
from a prior pass, so prompt size stays small throughout.

Usage:
    from kilonova.tools.persona_curator import PersonaCurator, load_dot_content

    curator = PersonaCurator(engine)
    persona = await curator.synthesize(
        topic='battletech',
        specialist_outputs=outputs,   # from engine.specialist_round()
        dot_overview=overview_text,
        kb_dir=Path('kb/battletech'),
    )
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DOMAIN_WEIGHTS = {
    'narrative':  {'narrative': 0.60, 'philosophy': 0.30, 'mechanics': 0.10},
    'mechanics':  {'mechanics': 0.70, 'narrative':  0.20, 'philosophy': 0.10},
    'philosophy': {'philosophy': 0.50, 'narrative': 0.35, 'mechanics':  0.15},
    'faction':    {'philosophy': 0.50, 'narrative': 0.35, 'mechanics':  0.15},
}

_CONFLICT_RULES = """\
Conflict resolution weights:
- Narrative decisions (tone, pacing, description): narrative 60%, philosophy 30%, mechanics 10%
- Mechanical decisions (rules, resolution, edge cases): mechanics 70%, narrative 20%, philosophy 10%
- Faction decisions (motivation, voice, stance): philosophy 50%, narrative 35%, mechanics 15%

Return ONLY valid JSON. No markdown fences, no prose, no explanation."""

# ── Pass system prompts — each independent, small, and focused ────────────────

CURATOR_PASS1_SYSTEM = f"""\
You are the GM Persona Curator, Pass 1 of 4.

You receive the NARRATIVE specialist's analysis of a tabletop RPG module.
Extract from it the GM's identity, tone, and voice framework — and any
narrative-domain routing rules the specialist identified.

Return JSON with exactly these keys:
{{
  "coreInstructions": {{
    "identity": "who the GM is and what drives them",
    "authority": "where the GM's knowledge comes from (the DOT knowledge base)",
    "failureMode": "what the GM does when uncertain"
  }},
  "voiceFramework": {{
    "overallTone": "string",
    "sentenceStructure": "string",
    "pacing": {{ "exploration": "string", "combat": "string", "downtime": "string" }},
    "avoid": ["things the GM never does or says"]
  }},
  "routingRules": [
    {{
      "id": "unique-slug",
      "trigger": "what player input or situation activates this rule",
      "dotPath": "raw/filename.md",
      "fallbackChain": ["what to do if dotPath unavailable"],
      "voiceOverride": "optional tone shift, or null",
      "domain": "narrative",
      "confidence": 0.85
    }}
  ]
}}

{_CONFLICT_RULES}"""

CURATOR_PASS2_SYSTEM = f"""\
You are the GM Persona Curator, Pass 2 of 4.

You receive the MECHANICS specialist's analysis of a tabletop RPG module
and the list of actual files in the knowledge base.

Extract mechanics-domain routing rules only. Reference ONLY file paths from
the provided KB file list — do not invent paths.

Return JSON with exactly this key:
{{
  "routingRules": [
    {{
      "id": "unique-slug",
      "trigger": "what player action or mechanic query activates this rule",
      "dotPath": "raw/filename.md",
      "fallbackChain": ["what to do if dotPath unavailable"],
      "voiceOverride": "optional tone shift, or null",
      "domain": "mechanics",
      "confidence": 0.85
    }}
  ]
}}

{_CONFLICT_RULES}"""

CURATOR_PASS3_SYSTEM = f"""\
You are the GM Persona Curator, Pass 3 of 4.

You receive the PHILOSOPHY specialist's analysis of a tabletop RPG module.
Extract faction voices, fallback protocol, and any philosophy-domain routing rules.

Return JSON with exactly these keys:
{{
  "factionVoices": [
    {{
      "factionId": "slug",
      "factionName": "Full Name",
      "tone": "one-line description of this faction's voice",
      "speechPatterns": ["pattern one", "pattern two"],
      "example": "A full example line of dialogue an NPC from this faction would say."
    }}
  ],
  "fallbackProtocol": {{
    "routingFailure": "what to say when no routing rule matches",
    "dotMissing": "what to say when the DOT path is not found",
    "lowConfidence": "what to say when uncertain about the answer"
  }},
  "routingRules": [
    {{
      "id": "unique-slug",
      "trigger": "what player choice or theme query activates this rule",
      "dotPath": "raw/filename.md",
      "fallbackChain": ["what to do if dotPath unavailable"],
      "voiceOverride": "optional tone shift, or null",
      "domain": "philosophy",
      "confidence": 0.85
    }}
  ]
}}

{_CONFLICT_RULES}"""

CURATOR_PASS4_SYSTEM = """\
You are the GM Persona Curator, Pass 4 of 4 — the merge and finalization pass.

You receive three independent specialist outputs (narrative, mechanics, philosophy)
and must assemble them into one complete GM Persona JSON document.

Steps:
1. Merge all routingRules arrays from all three inputs. Deduplicate by id.
   Prefer the mechanics specialist's version of any conflicting mechanic rule.
2. Take coreInstructions and voiceFramework from the narrative input.
3. Take factionVoices and fallbackProtocol from the philosophy input.
4. Add a "meta" block.
5. Write "curatorNotes" summarising any conflicts resolved and open issues.

Final output schema:
{
  "meta": {
    "personaId": "string",
    "moduleId": "string",
    "personaVersion": "1.0.0",
    "createdAt": "ISO8601",
    "specialists": ["narrative", "mechanics", "philosophy"],
    "dotTopic": "string"
  },
  "coreInstructions": { "identity": "", "authority": "", "failureMode": "" },
  "voiceFramework": {
    "overallTone": "", "sentenceStructure": "",
    "pacing": { "exploration": "", "combat": "", "downtime": "" },
    "avoid": []
  },
  "routingRules": [ { "id": "", "trigger": "", "dotPath": "", "fallbackChain": [],
                      "voiceOverride": null, "domain": "", "confidence": 0.0 } ],
  "factionVoices": [ { "factionId": "", "factionName": "", "tone": "",
                       "speechPatterns": [], "example": "" } ],
  "fallbackProtocol": { "routingFailure": "", "dotMissing": "", "lowConfidence": "" },
  "curatorNotes": ""
}

Return ONLY the JSON object. No markdown, no prose.\
"""


# ── Module helper ─────────────────────────────────────────────────────────────

def load_dot_content(kb_dir: Path, max_chars: int = 20000) -> str:
    """
    Build a combined DOT content string from overview.md + raw/*.md files.

    Each raw file is capped at 2000 chars so no single doc eats the budget.
    Combined result truncated at max_chars with a notice.
    """
    if not kb_dir.exists():
        return ''

    parts = []

    overview = kb_dir / 'overview.md'
    if overview.exists():
        try:
            parts.append('## OVERVIEW\n\n' + overview.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning('Failed to load overview.md: %s', e)

    raw_dir = kb_dir / 'raw'
    if raw_dir.exists():
        for f in sorted(raw_dir.glob('*.md'))[:10]:
            try:
                text = f.read_text(encoding='utf-8')
                parts.append(f'## {f.stem}\n\n' + text[:2000])
            except Exception as e:
                logger.warning('Failed to load %s: %s', f.name, e)

    combined = '\n\n---\n\n'.join(parts)

    if len(combined) > max_chars:
        omitted = len(combined) - max_chars
        combined = combined[:max_chars] + f'\n\n[... truncated - {omitted} chars omitted]'

    return combined


def list_kb_files(kb_dir: Path) -> str:
    """Return a newline-separated list of raw/*.md files for routing rule grounding."""
    raw_dir = kb_dir / 'raw'
    if not raw_dir.exists():
        return '(no raw files found)'
    files = sorted(f'raw/{f.name}' for f in raw_dir.glob('*.md'))
    return '\n'.join(files) if files else '(no raw files found)'


# ── PersonaCurator ────────────────────────────────────────────────────────────

class PersonaCurator:
    """
    Synthesizes specialist outputs into a GM Persona routing document.

    Never raises — all failures return a stub persona with meta.status='stub'.
    """

    def __init__(self, engine):
        self.engine = engine

    async def synthesize(
        self,
        topic: str,
        specialist_outputs: dict[str, dict],
        dot_overview: str,
        kb_dir: Path,
        module_id: str | None = None,
    ) -> dict:
        """
        Synthesize three specialist outputs into a GM Persona file.

        Four independent passes — each handles one domain, Pass 4 merges:
          Pass 1 (narrative)   -> coreInstructions + voiceFramework + narrative routing
          Pass 2 (mechanics)   -> mechanics routing rules only
          Pass 3 (philosophy)  -> factionVoices + fallbackProtocol + philosophy routing
          Pass 4 (merge)       -> assembles all three + meta + curatorNotes

        The curator never sees the raw DOT — specialists already distilled it.
        Each pass only sees its own specialist artifact, keeping prompts small.
        """
        try:
            ok_outputs = {
                role: out for role, out in specialist_outputs.items()
                if out.get('ok') and out.get('artifact') is not None
            }

            if not ok_outputs:
                return self._stub_persona(topic, module_id, 'All specialists failed.')

            leg = self._select_curator_leg()
            if not leg:
                return self._stub_persona(topic, module_id, 'No curator leg available.')

            def _art(role):
                out = ok_outputs.get(role)
                return json.dumps(out['artifact'], indent=2) if out else '(specialist unavailable)'

            kb_files = list_kb_files(kb_dir)

            # ── Pass 1: narrative → voice + identity + narrative routing ──────
            logger.info('Curator pass 1/4 - voice & identity')
            p1_text = await self._pass(
                leg, CURATOR_PASS1_SYSTEM,
                f'## NARRATIVE SPECIALIST OUTPUT\n\n{_art("narrative")}\n\n'
                f'## AVAILABLE KB FILES\n\n{kb_files}\n\n'
                f'Module: "{topic}". Extract coreInstructions, voiceFramework, '
                f'and any narrative routing rules. Use only paths from the KB file list.',
            )
            p1 = self._extract(p1_text) or {}

            # ── Pass 2: mechanics → routing rules ─────────────────────────────
            logger.info('Curator pass 2/4 - mechanics routing')
            p2_text = await self._pass(
                leg, CURATOR_PASS2_SYSTEM,
                f'## MECHANICS SPECIALIST OUTPUT\n\n{_art("mechanics")}\n\n'
                f'## AVAILABLE KB FILES\n\n{kb_files}\n\n'
                f'Module: "{topic}". Extract mechanics routing rules only. '
                f'Use only paths from the KB file list above.',
            )
            p2 = self._extract(p2_text) or {}

            # ── Pass 3: philosophy → factions + fallback + philosophy routing ─
            logger.info('Curator pass 3/4 - factions & fallback')
            p3_text = await self._pass(
                leg, CURATOR_PASS3_SYSTEM,
                f'## PHILOSOPHY SPECIALIST OUTPUT\n\n{_art("philosophy")}\n\n'
                f'## AVAILABLE KB FILES\n\n{kb_files}\n\n'
                f'Module: "{topic}". Extract factionVoices, fallbackProtocol, '
                f'and any philosophy routing rules. Use only paths from the KB file list.',
            )
            p3 = self._extract(p3_text) or {}

            # ── Pass 4: merge all three into final persona ────────────────────
            logger.info('Curator pass 4/4 - merge & finalize')
            merge_block = json.dumps(
                {'narrative': p1, 'mechanics': p2, 'philosophy': p3},
                indent=2,
            )
            p4_text = await self._pass(
                leg, CURATOR_PASS4_SYSTEM,
                f'## SPECIALIST PASS OUTPUTS\n\n{merge_block}\n\n'
                f'Module: "{topic}". Specialists used: {list(ok_outputs.keys())}.\n'
                f'Merge into final GM Persona. Add meta block and curatorNotes.',
            )
            p4 = self._extract(p4_text)

            if not p4:
                return self._stub_persona(topic, module_id, 'Final merge pass returned no valid JSON.')

            p4 = self._stamp_metadata(p4, topic, ok_outputs, module_id)
            p4 = self._validate_routing(p4, kb_dir)
            return p4

        except Exception as e:
            logger.exception('Unexpected error in PersonaCurator.synthesize')
            return self._stub_persona(topic, module_id, f'{type(e).__name__}: {e}')

    # ── Private helpers ───────────────────────────────────────────────────────

    def _extract(self, text: str | None) -> dict | None:
        """Extract JSON from pass output. Returns None if extraction fails."""
        if not text:
            return None
        try:
            return self.engine._extract_json_artifact(text)
        except ValueError:
            return None

    async def _pass(self, leg, system: str, prompt: str) -> str | None:
        """
        Fire one curator pass. Returns raw text, or None on failure.

        Repair is only triggered when no valid JSON can be extracted at all —
        markdown fences (```json ... ```) are handled by _extract_json_artifact
        and do NOT trigger a repair call.
        """
        try:
            text = await leg.provider.complete(
                leg.model,
                system,
                [{'role': 'user', 'content': prompt}],
            )
        except Exception as e:
            logger.error('Curator pass failed: %s', e)
            return None

        # Only repair if the extractor genuinely can't find any JSON
        if self._extract(text) is None:
            logger.warning('Pass has no extractable JSON; attempting repair. First 200: %s', text[:200])
            try:
                text = await leg.provider.complete(
                    leg.model,
                    'Output only the JSON object from your previous response. No prose, no fences.',
                    [{'role': 'user', 'content': text[:6000]}],
                )
            except Exception as e:
                logger.error('Repair call failed: %s', e)

        return text

    def _select_curator_leg(self):
        """Returns the best alive leg: anthropic > openai > gemini > any."""
        for name in ('anthropic', 'openai', 'gemini'):
            leg = self.engine.registry.legs.get(name)
            if leg and leg.alive:
                return leg
        for leg in self.engine.registry.legs.values():
            if leg.alive:
                return leg
        return None

    def _stamp_metadata(
        self,
        persona: dict,
        topic: str,
        ok_outputs: dict,
        module_id: str | None,
    ) -> dict:
        persona.setdefault('meta', {})
        meta = persona['meta']
        meta.setdefault('personaId',      f'{topic}_gm_v1_0_0')
        meta.setdefault('moduleId',       module_id or topic)
        meta.setdefault('personaVersion', '1.0.0')
        meta.setdefault('createdAt',      datetime.now(timezone.utc).isoformat())
        meta.setdefault('specialists',    list(ok_outputs.keys()))
        meta.setdefault('dotTopic',       topic)
        meta['status'] = 'active'
        return persona

    def _validate_routing(self, persona: dict, kb_dir: Path) -> dict:
        """Flag routing rules whose dotPath doesn't exist on disk."""
        missing = []
        for rule in persona.get('routingRules', []):
            dot_path = rule.get('dotPath', '')
            if not dot_path:
                continue
            file_path = Path(str(kb_dir / dot_path.lstrip('/')).split('#')[0])
            if not file_path.exists():
                rule['_warning'] = f'dotPath not found: {dot_path}'
                missing.append(dot_path)

        if missing:
            block = (
                '\n\n[VALIDATION WARNINGS]\n'
                f'Missing DOT paths ({len(missing)}):\n' +
                ''.join(f'  - {p}\n' for p in missing) +
                'These rules may fail at runtime if paths are not created.'
            )
            existing = persona.get('curatorNotes', '')
            persona['curatorNotes'] = (existing + block).strip()

        return persona

    @staticmethod
    def _stub_persona(topic: str, module_id: str | None, error: str) -> dict:
        return {
            'meta': {
                'personaId':      f'{topic}_gm_stub',
                'moduleId':       module_id or topic,
                'personaVersion': '0.0.0',
                'createdAt':      datetime.now(timezone.utc).isoformat(),
                'specialists':    [],
                'dotTopic':       topic,
                'status':         'stub',
                'error':          error,
            },
            'coreInstructions': {
                'identity':    f'GM for {topic}',
                'authority':   'DOT knowledge base',
                'failureMode': 'Admit uncertainty and ask the player to rephrase.',
            },
            'voiceFramework':   {},
            'routingRules':     [],
            'factionVoices':    [],
            'fallbackProtocol': {
                'routingFailure': "I'm not certain about that. Could you rephrase?",
                'dotMissing':     'The records on that are unclear.',
                'lowConfidence':  'Let me check — what exactly would you like to know?',
            },
            'curatorNotes': f'Synthesis failed: {error}. Run persona build again.',
        }
