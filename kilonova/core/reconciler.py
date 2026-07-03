"""
Reconciler — ported from shard-accelerator with multi-leg awareness.
Roles: coherence, structure, pattern_recognition, data_validity, notes_taker, judge.
"""
import asyncio
from .providers.base import BaseProvider

ROLE_PROMPTS: dict[str, str] = {
    'coherence': (
        'You are the Coherence role. Read the isolate outputs and identify '
        'where they agree, contradict, or complement each other. '
        'Surface the strongest unified position.'
    ),
    'structure': (
        'You are the Structure role. Organize the isolate outputs into a '
        'clear, logical response. Remove redundancy. Preserve all distinct signal.'
    ),
    'pattern_recognition': (
        'You are the Pattern Recognition role. Identify themes, patterns, and '
        'insights that appear across multiple isolate outputs. '
        'Flag anything surprising or non-obvious.'
    ),
    'data_validity': (
        'You are the Data Validity role. Flag any claims in the isolate outputs '
        'that appear hallucinated, unverifiable, or contradicted by other isolates.'
    ),
    'notes_taker': (
        'You are the Notes Taker. Your job is to write the final user-facing response.\n'
        'You receive the analysis from the Coherence, Structure, Pattern Recognition, and Data Validity roles, '
        'plus the raw isolate outputs for reference. Synthesize these into a direct, concise answer.\n'
        'CRITICAL RULES:\n'
        '1. If the analysis and isolates all indicate no information is available, respond only with: '
        '"I don\'t have enough information to answer that." Do NOT invent content.\n'
        '2. Answer ONLY the current question. Do NOT recap, reference, or repeat any prior turn.\n'
        '3. Do NOT mention the analysis roles, the reconciler, or the pipeline. '
        'Write as if you are a single, confident assistant giving a direct answer.\n'
        'This is the final user-facing output — make it clean and complete.'
    ),
    'judge': (
        'You are the Judge. Score the quality of this set of isolate outputs '
        'on a scale of 0-10. Note what is missing, what is excellent, '
        'and whether another round would meaningfully improve the result.'
    ),
}


class Reconciler:
    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        roles: list[str] | None = None,
        fallback_providers: list[tuple[BaseProvider, str]] | None = None,
    ):
        self.provider  = provider
        self.model     = model
        self.roles     = roles or ['coherence', 'structure', 'notes_taker']
        # [(provider, model), ...] tried in order if primary provider fails permanently
        self.fallback_providers = fallback_providers or []

    async def _run_role(
        self,
        role: str,
        isolate_outputs: list[dict],
        conversation_context: str,
        stage1_outputs: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        system = ROLE_PROMPTS.get(role, f'You are the {role} role.')

        isolates_block = '\n\n---\n\n'.join(
            f'### Isolate {i + 1} ({out["leg"]})\n{out["text"]}'
            for i, out in enumerate(isolate_outputs)
        )

        if stage1_outputs:
            # notes_taker sees the analysis roles' outputs, not raw isolates
            analysis_block = '\n\n---\n\n'.join(
                f'### {r.replace("_", " ").title()}\n{text}'
                for r, text in stage1_outputs.items()
                if not text.startswith('[reconciler error')
            )
            content = f'## ANALYSIS\n\n{analysis_block}\n\n## RAW ISOLATES\n\n{isolates_block}'
        else:
            content = f'## ISOLATE OUTPUTS\n\n{isolates_block}'
            if conversation_context:
                content = f'## CONVERSATION CONTEXT\n\n{conversation_context}\n\n{content}'

        providers_to_try = [(self.provider, self.model)] + self.fallback_providers
        last_err = None
        for provider, model in providers_to_try:
            try:
                output = await provider.complete(
                    model, system, [{'role': 'user', 'content': content}]
                )
                return role, output
            except Exception as e:
                last_err = e
                continue
        return role, f'[reconciler error: {last_err}]'

    async def run(
        self,
        isolate_outputs: list[dict],  # [{'leg': 'anthropic', 'text': '...'}]
        conversation_context: str = '',
    ) -> dict[str, str]:
        analysis_roles = [r for r in self.roles if r != 'notes_taker']
        has_notes_taker = 'notes_taker' in self.roles

        # Stage 1: all analysis roles in parallel against raw isolates
        stage1_results = dict(await asyncio.gather(*[
            self._run_role(role, isolate_outputs, conversation_context)
            for role in analysis_roles
        ])) if analysis_roles else {}

        # Stage 2: notes_taker reads the stage 1 analysis, not raw isolates
        if has_notes_taker:
            _, notes = await self._run_role(
                'notes_taker', isolate_outputs, conversation_context,
                stage1_outputs=stage1_results if stage1_results else None,
            )
            stage1_results['notes_taker'] = notes

        return stage1_results
