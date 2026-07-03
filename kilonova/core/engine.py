"""
Kilonova ConversationEngine — the heart of Milna OS.

Two execution strategies, chosen automatically each turn:

  Pipeline  (single provider, e.g. Ollama-only)
    Isolates fire sequentially — each gets the full GPU. Every isolate writes
    a compact note card alongside its prose. A single synthesis call reads the
    cards and writes the final answer. No analysis-role contention.

  Parallel  (multi-provider, e.g. Ollama + Anthropic)
    Isolates fire in parallel across providers. Two-stage reconciler: analysis
    roles run concurrently, then notes_taker synthesizes their output.

/deep triggers multi-round distillation in both modes.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from .legs import Leg, LegRegistry
from .mode import ModeController
from .reconciler import Reconciler
from .providers.base import BaseProvider


MILNA_SYSTEM = """\
You are Milna — a multi-model AI assistant powered by the Kilonova engine.

## What you are

You are the synthesized output of multiple AI models (legs) running in parallel. \
The Kilonova runtime orchestrates them and you are the reconciled result. \
You are not a single model — you are the sum of all active legs.

## How file access works

You do not browse the file system directly during conversation — that is correct. \
However, the Kilonova runtime has commands that load file content into the context window before you see it:

- `/load <path>` — extracts text from a PDF, .md, or .txt file and injects it into context
- `/ingest <folder>` — builds a persistent knowledge base (DOT) from a folder of PDFs
- `/kb <topic>` — loads a pre-built knowledge base as your context anchor

If a user asks whether you can access a folder or file, tell them to use the appropriate \
command above. Do NOT say "upload files" or "copy and paste" — Milna has real commands for this. \
Do NOT suggest the user needs an external tool or interface. Milna handles it natively.

## Web access

Web browsing and URL fetching are not yet available in this version of Milna. \
When a user asks if you can read a website, say so directly and specifically: \
"Web access is coming in a future update. For now, use /load or /ingest to bring \
document content into context." \
Do NOT say "I don't have the capability" or generic AI disclaimers. \
Be specific: the feature is planned, not built yet.

## What a DOT is

A DOT (Document of Truth) is a persistent knowledge base built from source documents. \
When a DOT is loaded, the overview appears in context as a verified, distilled reference. \
Treat DOT content as authoritative source material, not as something you need to verify.

## How to behave

- Answer directly and completely. Do not hedge about what you "can" or "cannot" do as an AI \
  — the runtime handles capabilities, you handle answers.
- If information is in the context or KB, use it. If it is not, say so plainly: "I don't have that in my knowledge base." Do NOT fill gaps with training data presented as confirmed facts.
- For lore, narrative, or world-building questions: answer from the loaded KB first. If the KB is silent on a detail, say so before offering general knowledge.
- For code questions: write real, complete code. No placeholders.
- For design questions: give a recommendation with a stated tradeoff, not a menu of options.
- You know your own name. You know what you are. Answer questions about your identity accurately.
"""

# Appended to the user message in pipeline mode — asks each isolate to self-note.
_CARD_SUFFIX = (
    '\n\n[CARD]\n'
    '• Claim: your direct answer in one sentence\n'
    '• Facts: 2-3 key supporting points\n'
    '• Confidence: high / medium / low\n'
    '• Unique: one thing the other models might miss'
)

# System prompt for the card synthesis step in pipeline mode.
_CARD_SYNTH_SYSTEM = (
    'You are Milna. You have received note cards from AI models that each answered '
    'the same question independently. Write one direct, complete answer synthesized '
    'from the cards. Favor high-confidence claims. Include anything unique if it adds '
    'value. Write as a confident assistant — no bullet lists, no meta-commentary about '
    'the cards or the process. Do NOT repeat the question.'
)


def _extract_card(text: str) -> str:
    """Pull the [CARD] section out of an isolate response. Falls back to truncation."""
    if '[CARD]' in text:
        return text.split('[CARD]', 1)[1].strip()
    return text[:400]


@dataclass
class TurnResult:
    response: str
    isolate_outputs: list[dict]     # [{'leg': name, 'text': ..., 'card': ...}]
    reconciler_roles: dict[str, str]
    dead_legs: list[str]
    token_estimate: int
    elapsed_ms: int
    deep: bool = False
    rounds_run: int = 1
    pipeline: bool = False


class ConversationEngine:
    def __init__(
        self,
        leg_registry: LegRegistry,
        mode_controller: ModeController,
        reconciler_provider: BaseProvider,
        reconciler_model: str,
        max_history_turns: int = 10,
        reconciler_fallbacks: list[tuple] | None = None,
    ):
        self.registry = leg_registry
        self.mode     = mode_controller
        fallbacks     = reconciler_fallbacks or []
        self.reconciler = Reconciler(
            provider=reconciler_provider,
            model=reconciler_model,
            roles=['coherence', 'structure', 'pattern_recognition',
                   'data_validity', 'notes_taker'],
            fallback_providers=fallbacks,
        )
        self.judge = Reconciler(
            provider=reconciler_provider,
            model=reconciler_model,
            roles=['judge'],
            fallback_providers=fallbacks,
        )
        self._rec_providers = [(reconciler_provider, reconciler_model)] + fallbacks
        self.history: list[dict] = []
        self.max_history_turns   = max_history_turns
        self._anchor_count: int  = 0

    # ── History management ────────────────────────────────────────────────────

    def _trim_history(self) -> None:
        anchors = self.history[:self._anchor_count]
        rolling = self.history[self._anchor_count:]
        max_msgs = self.max_history_turns * 2
        if len(rolling) > max_msgs:
            rolling = rolling[-max_msgs:]
        self.history = anchors + rolling

    def _build_messages(self, user_message: str) -> list[dict]:
        return self.history + [{'role': 'user', 'content': user_message}]

    def _history_text(self) -> str:
        if not self.history:
            return ''
        lines = []
        for m in self.history[-6:]:
            role = 'User' if m['role'] == 'user' else 'Milna'
            lines.append(f'{role}: {m["content"][:300]}')
        return '\n'.join(lines)

    def reset(self) -> None:
        self.history.clear()
        self._anchor_count = 0

    # ── Isolate execution ─────────────────────────────────────────────────────

    async def _fire_isolate(self, leg: Leg, messages: list[dict], system: str) -> dict:
        try:
            text = await leg.provider.complete(leg.model, system, messages)
            return {'leg': leg.name, 'text': text, 'ok': True}
        except Exception as e:
            err = str(e)
            transient = any(c in err for c in ('429', '503', '502', 'rate', 'quota', 'timeout'))
            if not transient:
                self.registry.kill_leg(leg.name, err)
            return {'leg': leg.name, 'text': '', 'ok': False, 'error': err, 'transient': transient}

    def _is_pipeline_mode(self) -> bool:
        """Pipeline when all active legs share the same provider (single compute resource)."""
        active = self.registry.active_legs()
        if not active:
            return False
        provider_classes = {leg.provider.__class__.__name__ for leg in active}
        return len(provider_classes) == 1

    async def _pipeline_round(
        self,
        user_message: str,
        system: str,
        isolates_per_leg: int,
    ) -> tuple[list[dict], list[str]]:
        """
        Sequential isolate firing — each gets full GPU and writes a note card.
        Uses diverse models (one per available Ollama model) so each card brings
        genuinely different signal rather than repeating the same weights.
        """
        legs     = self.registry.diverse_pipeline_legs(isolates_per_leg)
        messages = self._build_messages(user_message)
        # Only inject card format for conversation turns — ingest/tool calls
        # pass their own system prompts and need clean JSON output.
        if system == MILNA_SYSTEM:
            fire_messages = messages[:-1] + [{
                'role': 'user',
                'content': messages[-1]['content'] + _CARD_SUFFIX,
            }]
        else:
            fire_messages = messages

        ok_outputs: list[dict] = []
        dead_legs:  list[str]  = []
        for leg in legs:
            result = await self._fire_isolate(leg, fire_messages, system)
            if result['ok']:
                if system == MILNA_SYSTEM:
                    result['card'] = _extract_card(result['text'])
                ok_outputs.append(result)
            else:
                dead_legs.append(
                    leg.name + (' [rate limit]' if result.get('transient') else '')
                )

        return ok_outputs, dead_legs

    async def _parallel_round(
        self,
        user_message: str,
        system: str,
        isolates_per_leg: int,
    ) -> tuple[list[dict], list[str]]:
        """Parallel isolate firing across multiple providers."""
        legs     = self.registry.active_legs()
        messages = self._build_messages(user_message)
        tasks    = [
            self._fire_isolate(leg, messages, system)
            for leg in legs
            for _ in range(isolates_per_leg)
        ]
        results   = await asyncio.gather(*tasks)
        ok_outputs = [r for r in results if r['ok']]
        dead_legs  = [
            r['leg'] + (' [rate limit]' if r.get('transient') else '')
            for r in results if not r['ok']
        ]
        return ok_outputs, dead_legs

    async def _synth_from_cards(self, isolate_outputs: list[dict]) -> str:
        """Single synthesis call that reads note cards — used in pipeline mode."""
        cards_block = '\n\n---\n\n'.join(
            f'### Card {i+1} ({o["leg"]})\n{o["card"]}'
            for i, o in enumerate(isolate_outputs)
        )
        content = f'## NOTE CARDS\n\n{cards_block}'
        for provider, model in self._rec_providers:
            try:
                return await provider.complete(
                    model,
                    _CARD_SYNTH_SYSTEM,
                    [{'role': 'user', 'content': content}],
                )
            except Exception:
                continue
        return isolate_outputs[0]['text']  # last-resort fallback

    # ── Main turn ─────────────────────────────────────────────────────────────

    async def turn(
        self,
        user_message: str,
        system: str | None = None,
        deep: bool = False,
    ) -> TurnResult:
        if system is None:
            system = MILNA_SYSTEM

        t_start          = datetime.now()
        isolates_per_leg = self.mode.mode.isolates_per_leg
        max_rounds       = self.mode.mode.max_rounds if deep else 1
        all_dead:        list[str]  = []
        isolate_outputs: list[dict] = []
        reconciler_roles:dict[str, str] = {}
        prev_synthesis   = ''
        pipeline         = self._is_pipeline_mode()
        round_num        = 1

        for round_num in range(1, max_rounds + 1):
            round_message = user_message
            if prev_synthesis and deep:
                round_message = (
                    f'{user_message}\n\n'
                    f'## PREVIOUS ROUND SYNTHESIS\n\n{prev_synthesis}'
                )

            if pipeline:
                isolate_outputs, dead = await self._pipeline_round(
                    round_message, system, isolates_per_leg)
            else:
                isolate_outputs, dead = await self._parallel_round(
                    round_message, system, isolates_per_leg)
            all_dead.extend(dead)

            if not isolate_outputs:
                return TurnResult(
                    response='All legs failed. Check your API keys and connections.',
                    isolate_outputs=[], reconciler_roles={},
                    dead_legs=all_dead, token_estimate=0, elapsed_ms=0,
                    deep=deep, rounds_run=round_num, pipeline=pipeline,
                )

            if pipeline and any('card' in o for o in isolate_outputs):
                response         = await self._synth_from_cards(isolate_outputs)
                reconciler_roles = {'notes_taker': response}
                prev_synthesis   = response
            elif pipeline:
                # Tool call in pipeline mode — no cards, use first output directly
                response         = isolate_outputs[0]['text']
                reconciler_roles = {'notes_taker': response}
                prev_synthesis   = response
            else:
                reconciler_roles = await self.reconciler.run(
                    isolate_outputs,
                    conversation_context=self._history_text(),
                )
                prev_synthesis = reconciler_roles.get('notes_taker', isolate_outputs[0]['text'])

                if deep and round_num < max_rounds:
                    judge_out = await self.judge.run(isolate_outputs)
                    note = judge_out.get('judge', '')
                    if 'no further' in note.lower() or 'complete' in note.lower():
                        break

        response = reconciler_roles.get('notes_taker', isolate_outputs[0]['text'])

        self.history.append({'role': 'user',      'content': user_message})
        self.history.append({'role': 'assistant', 'content': response})
        self._trim_history()

        elapsed_ms     = int((datetime.now() - t_start).total_seconds() * 1000)
        token_estimate = sum(len(o['text'].split()) * 4 // 3 for o in isolate_outputs)

        return TurnResult(
            response=response,
            isolate_outputs=isolate_outputs,
            reconciler_roles=reconciler_roles,
            dead_legs=list(set(all_dead)),
            token_estimate=token_estimate,
            elapsed_ms=elapsed_ms,
            deep=deep,
            rounds_run=round_num,
            pipeline=pipeline,
        )
