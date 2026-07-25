"""
dot_compress_v2.py — Multi-pass DOT compression.

Pass 1: Run phrase analyst twice on raw DOT, union results (high-confidence phrases)
Apply:  Rewrite DOT with phrase symbols substituted inline
Pass 2: Run phrase+sentence analyst on compressed DOT (structural frames emerge)
Output: Final symbol table, compressed DOT, variance score

If it's a machine language: Pass 2 finds stable structural frames built on
top of Pass 1 symbols. If it's a farce: Pass 2 finds noise, no coherent frames.

Usage:
    python dot_compress_v2.py
    python dot_compress_v2.py --min-value 8 --top 64
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs   import LegRegistry, KeyInventory
from kilonova.tools.ingest import _parse_json_safe


# ── Config ────────────────────────────────────────────────────────────────────

_MEMORY_PATH = Path(r'D:\shard-accelerator\runs\prompt_memory.json')
_UT_KEY      = '38fa9977c40113b4'
_OUT_DIR     = Path(r'D:\Runtime')

_PREFERRED = ['anthropic', 'openrouter', 'openai']
_SKIP      = ['together', 'sambanova', 'gemini', 'cerebras', 'groq', 'openai']

_GREEK_LOWER = list('αβγδεζηθικλμνξοπρστυφχψω')
_GREEK_UPPER = list('ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ')
_SYMBOLS     = _GREEK_LOWER + _GREEK_UPPER   # 48 slots


# ── Prompts ───────────────────────────────────────────────────────────────────

_PHRASE_PROMPT_PATH   = Path(r'D:\shard-accelerator\prompts\isolates\linguistics_phrase_35.md')
_FRAME_PROMPT_PATH    = Path(r'D:\shard-accelerator\prompts\isolates\linguistics_frame.md')

_SYSTEM = (
    'You are a precise document analyst. Follow your role instructions exactly. '
    'Output ONLY valid JSON as specified. No markdown fences, no prose.'
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_dot() -> str:
    data = json.loads(_MEMORY_PATH.read_text(encoding='utf-8'))
    entry = data.get(_UT_KEY)
    if not entry:
        raise ValueError(f'Key {_UT_KEY!r} not found in prompt_memory.json')
    return entry['last_output']


def _pick_legs(registry: LegRegistry, n: int = 4) -> list:
    active = {l.name: l for l in registry.active_legs()}
    legs = []
    for name in _PREFERRED:
        if name in active and name not in _SKIP:
            legs.append(active[name])
    for leg in registry.active_legs():
        if leg.name.startswith('ollama') and leg not in legs:
            legs.append(leg)
            break
    return legs[:n]


async def _call(leg, system: str, user: str) -> list | None:
    try:
        raw = await asyncio.wait_for(
            leg.provider.complete(leg.model, system, [{'role': 'user', 'content': user}]),
            timeout=150.0,
        )
        result = _parse_json_safe(raw, default=None)
        if isinstance(result, list):
            return result
        print(f'    [{leg.name}] unexpected type: {type(result).__name__}')
        return None
    except asyncio.TimeoutError:
        print(f'    [{leg.name}] timeout')
        return None
    except Exception as e:
        print(f'    [{leg.name}] error: {str(e)[:100]}')
        return None


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def _merge(lists: list, kind: str) -> dict:
    """Merge multiple analyst result lists into a deduped dict keyed by normalized text."""
    seen: dict[str, dict] = {}
    for lst in lists:
        if not lst:
            continue
        for item in lst:
            text = item.get('phrase') or item.get('sentence') or ''
            if not text:
                continue
            key  = _normalize(text)
            occ  = int(item.get('occurrences', 1))
            wds  = int(item.get('words', len(text.split())))
            val  = occ * wds
            if key in seen:
                if occ > seen[key]['occurrences']:
                    seen[key]['occurrences'] = occ
                    seen[key]['value']       = occ * seen[key]['words']
            else:
                seen[key] = {'text': text.strip(), 'kind': kind,
                             'occurrences': occ, 'words': wds, 'value': val}
    return seen


def _apply_symbols(dot: str, table: list[dict]) -> str:
    """Replace all phrase occurrences in dot with their symbols. Longest first."""
    compressed = dot
    for entry in sorted(table, key=lambda x: -x['words']):
        phrase  = re.escape(entry['text'])
        # case-insensitive, word-boundary aware
        pattern = rf'(?i)(?<!\w){phrase}(?!\w)'
        compressed = re.sub(pattern, entry['symbol'], compressed)
    return compressed


def _build_table(ranked: list[dict], symbol_offset: int, top: int, min_value: int) -> list[dict]:
    table = []
    idx   = symbol_offset
    for entry in ranked:
        if entry['value'] < min_value:
            continue
        if len(table) >= top:
            break
        sym = _SYMBOLS[idx] if idx < len(_SYMBOLS) else f'S{idx}'
        table.append({
            'symbol':       sym,
            'text':         entry['text'],
            'kind':         entry['kind'],
            'pass':         entry.get('pass', 1),
            'occurrences':  entry['occurrences'],
            'words':        entry['words'],
            'value':        entry['value'],
            'tokens_saved': (entry['occurrences'] - 1) * entry['words'],
        })
        idx += 1
    return table


def _format_report(p1_table: list, p2_table: list, variance_score: float,
                   raw_dot_len: int, compressed_dot_len: int) -> str:
    all_entries = p1_table + p2_table
    total_saved = sum(e['tokens_saved'] for e in all_entries)

    lines = [
        '# DOT Compression v2 — Multi-Pass Symbol Table',
        '',
        f'Raw DOT: {raw_dot_len:,} chars | Compressed DOT: {compressed_dot_len:,} chars',
        f'Size reduction: {100*(1 - compressed_dot_len/raw_dot_len):.1f}%',
        f'Symbols: {len(p1_table)} from Pass 1 + {len(p2_table)} from Pass 2 = {len(all_entries)} total',
        f'Estimated tokens saved per injection: ~{total_saved}',
        f'Pass 1 phrase agreement score: {variance_score:.1%}',
        '',
        '---',
        '',
        '## Pass 1 Symbols — High-Frequency Phrases (raw DOT)',
        '',
    ]

    lines.append('| Symbol | Phrase | Occurrences | Words | Tokens saved |')
    lines.append('|--------|--------|-------------|-------|--------------|')
    for e in p1_table:
        lines.append(f"| `{e['symbol']}` | {e['text'][:60]} | {e['occurrences']} | {e['words']} | {e['tokens_saved']} |")

    lines += ['', '---', '', '## Pass 2 Symbols — Structural Frames (compressed DOT)', '']
    if p2_table:
        lines.append('| Symbol | Frame | Occurrences | Words | Tokens saved |')
        lines.append('|--------|-------|-------------|-------|--------------|')
        for e in p2_table:
            lines.append(f"| `{e['symbol']}` | {e['text'][:80]} | {e['occurrences']} | {e['words']} | {e['tokens_saved']} |")
    else:
        lines.append('*No structural frames found above threshold — the DOT does not repeat sentence-level templates.*')
        lines.append('')
        lines.append('**Verdict: The compression is phrase-level, not frame-level. Useful but not a machine language.**')

    lines += ['', '---', '', '## Symbol Definitions (inject as cached header)', '']
    for e in all_entries:
        lines.append(f"`{e['symbol']}` = {e['text']}")

    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    print('DOT Compression v2 — Multi-Pass')
    print('=' * 50)

    dot = _load_dot()
    print(f'DOT loaded: {len(dot):,} chars (~{len(dot)//4:,} tokens)')

    phrase_prompt = _PHRASE_PROMPT_PATH.read_text(encoding='utf-8')
    frame_prompt  = _FRAME_PROMPT_PATH.read_text(encoding='utf-8')

    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()
    legs = _pick_legs(registry, n=4)

    if not legs:
        print('ERROR: No usable legs found.')
        sys.exit(1)

    print(f'\nLegs: {", ".join(l.name for l in legs)}')

    # ── Pass 1: Run phrase analyst TWICE on raw DOT ───────────────────────────
    # Use two legs if available; if only one live leg, run the same leg twice.
    print('\n[Pass 1] Phrase analysis on raw DOT (2 runs for confidence)...')
    leg_a = legs[0]
    leg_b = legs[1] if len(legs) >= 2 else legs[0]

    run_a, run_b = await asyncio.gather(
        _call(leg_a, _SYSTEM, f'{phrase_prompt}\n\n---\n\nDOCUMENT:\n\n{dot}'),
        _call(leg_b, _SYSTEM, f'{phrase_prompt}\n\n---\n\nDOCUMENT:\n\n{dot}'),
    )

    count_a = len(run_a) if run_a else 0
    count_b = len(run_b) if run_b else 0
    print(f'  Run A ({leg_a.name}): {count_a} phrases')
    print(f'  Run B ({leg_b.name}): {count_b} phrases')

    # If one run failed entirely, use whichever succeeded; skip agreement scoring
    if not run_a and not run_b:
        print('  ERROR: Both phrase runs failed. Aborting.')
        sys.exit(1)

    merged_p1 = _merge([run_a, run_b], 'phrase')

    # Variance score: fraction of phrases both runs independently found
    # Only meaningful when both runs returned results
    norms_a = {_normalize(i['phrase']) for i in (run_a or []) if i.get('phrase')}
    norms_b = {_normalize(i['phrase']) for i in (run_b or []) if i.get('phrase')}
    overlap  = norms_a & norms_b
    union    = norms_a | norms_b
    if norms_a and norms_b:
        variance_score = len(overlap) / len(union)
        print(f'  Agreement: {len(overlap)}/{len(union)} phrases in both runs ({variance_score:.1%})')
    else:
        variance_score = 0.0
        which = leg_a.name if run_a else leg_b.name
        print(f'  Single-leg run ({which}) — agreement score N/A')

    ranked_p1 = sorted(merged_p1.values(), key=lambda x: -x['value'])
    p1_table  = _build_table(ranked_p1, symbol_offset=0,
                              top=args.top // 2, min_value=max(6, args.min_value // 2))
    print(f'  {len(p1_table)} symbols assigned from Pass 1')

    # ── Apply Pass 1 symbols to DOT ───────────────────────────────────────────
    print('\n[Apply] Substituting Pass 1 symbols into DOT...')
    compressed_dot = _apply_symbols(dot, p1_table)
    ratio = len(compressed_dot) / len(dot)
    print(f'  {len(dot):,} -> {len(compressed_dot):,} chars  ({ratio:.1%} of original)')

    # Save compressed DOT for inspection
    (_OUT_DIR / 'dot_compressed.txt').write_text(compressed_dot, encoding='utf-8')
    print(f'  Saved: {_OUT_DIR / "dot_compressed.txt"}')

    # ── Pass 2: Analyze compressed DOT for structural frames ──────────────────
    # Always use the best available leg — don't rely on openrouter/ollama
    print('\n[Pass 2] Structural frame analysis on compressed DOT...')
    p2_legs = [leg_a, leg_b]   # same legs that worked in Pass 1

    frame_results = await asyncio.gather(*[
        _call(leg, _SYSTEM,
              f'{frame_prompt}\n\n---\n\nDOCUMENT:\n\n{compressed_dot}')
        for leg in p2_legs
    ])

    for leg, result in zip(p2_legs, frame_results):
        count = len(result) if result else 0
        print(f'  [{leg.name}]: {count} frames found')

    # Also run a second phrase pass on compressed DOT
    print('  Running phrase pass on compressed DOT...')
    phrase_results_p2 = await asyncio.gather(*[
        _call(leg, _SYSTEM,
              f'{phrase_prompt}\n\n---\n\nDOCUMENT:\n\n{compressed_dot}')
        for leg in [leg_a, leg_b]
    ])
    for leg, result in zip(p2_legs[:2], phrase_results_p2):
        count = len(result) if result else 0
        print(f'  [{leg.name}] phrase pass: {count} phrases found')

    merged_p2_frames  = _merge(list(frame_results), 'frame')
    merged_p2_phrases = _merge(list(phrase_results_p2), 'phrase')
    merged_p2 = {**merged_p2_frames, **merged_p2_phrases}

    # Dedup: strip P2 entries already covered by P1 symbols
    p1_covered = {_normalize(e['text']) for e in p1_table}
    merged_p2 = {k: v for k, v in merged_p2.items()
                 if k not in p1_covered
                 and len(k.strip()) > 2
                 and v['occurrences'] > 1
                 and v['words'] >= 2}   # no single-word frames

    ranked_p2 = sorted(merged_p2.values(), key=lambda x: -x['value'])
    for entry in ranked_p2:
        entry['pass'] = 2
    p2_table = _build_table(ranked_p2, symbol_offset=len(p1_table),
                             top=args.top // 2, min_value=args.min_value)
    print(f'  {len(p2_table)} symbols assigned from Pass 2')

    # ── Output ────────────────────────────────────────────────────────────────
    all_table = p1_table + p2_table
    total_saved = sum(e['tokens_saved'] for e in all_table)

    report = _format_report(p1_table, p2_table, variance_score,
                             len(dot), len(compressed_dot))

    out_json = _OUT_DIR / 'dot_compression_v2.json'
    out_md   = _OUT_DIR / 'dot_compression_v2.md'
    out_json.write_text(json.dumps(all_table, indent=2, ensure_ascii=False), encoding='utf-8')
    out_md.write_text(report, encoding='utf-8')

    print(f'\nOutputs:')
    print(f'  {out_json}')
    print(f'  {out_md}')
    print(f'  {_OUT_DIR / "dot_compressed.txt"}')
    print(f'\nTotal tokens saved per injection: ~{total_saved}')
    print(f'Pass 1 agreement: {variance_score:.1%}')
    if p2_table:
        print(f'\nVERDICT: Structural frames found — there is a machine language emerging.')
    else:
        print(f'\nVERDICT: No stable frames — phrase compression only. Useful, not a language.')
    print('\nDone.')


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--top',       type=int, default=48)
    p.add_argument('--min-value', type=int, default=8)
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
