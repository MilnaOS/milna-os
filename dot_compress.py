"""
dot_compress.py — Analyze the UT DOT for compression opportunities.

Runs two specialist isolates (phrase_analyst, sentence_analyst) across
available legs, merges their findings, and outputs a ranked symbol table.

Usage:
    python dot_compress.py
    python dot_compress.py --top 50
    python dot_compress.py --min-value 20
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


# ── Load the UT DOT ───────────────────────────────────────────────────────────

_MEMORY_PATH = Path(r'D:\shard-accelerator\runs\prompt_memory.json')
_UT_KEY      = '38fa9977c40113b4'

_PHRASE_PROMPT_PATH   = Path(r'D:\shard-accelerator\prompts\isolates\phrase_analyst.md')
_SENTENCE_PROMPT_PATH = Path(r'D:\shard-accelerator\prompts\isolates\sentence_analyst.md')


def _load_dot() -> str:
    data = json.loads(_MEMORY_PATH.read_text(encoding='utf-8'))
    entry = data.get(_UT_KEY)
    if not entry:
        raise ValueError(f'Key {_UT_KEY!r} not found in prompt_memory.json')
    return entry['last_output']


# ── Leg selection — exclude OpenAI-depleted and Together ─────────────────────

_PREFERRED = ['openai', 'anthropic', 'openrouter']
_SKIP      = ['together', 'sambanova', 'gemini', 'cerebras', 'groq']  # quota-exhausted


def _pick_legs(registry: LegRegistry) -> list:
    active = {l.name: l for l in registry.active_legs()}
    legs = []
    for name in _PREFERRED:
        if name in active and name not in _SKIP:
            legs.append(active[name])
    # always add local ollama as fallback
    for leg in registry.active_legs():
        if leg.name.startswith('ollama') and leg not in legs:
            legs.append(leg)
            break
    return legs[:4]   # cap at 4 legs


# ── Single analyst call ───────────────────────────────────────────────────────

_SYSTEM = (
    'You are a precise document analyst. Follow your role instructions exactly. '
    'Output ONLY valid JSON as specified. No markdown fences, no prose.'
)


async def _call_analyst(leg, role_prompt: str, dot: str) -> list | None:
    user_msg = f'{role_prompt}\n\n---\n\nDOCUMENT:\n\n{dot}'
    try:
        raw = await asyncio.wait_for(
            leg.provider.complete(
                leg.model, _SYSTEM,
                [{'role': 'user', 'content': user_msg}]
            ),
            timeout=120.0,
        )
        result = _parse_json_safe(raw, default=None)
        if isinstance(result, list):
            return result
        print(f'  [{leg.name}] unexpected response type: {type(result).__name__}')
        return None
    except asyncio.TimeoutError:
        print(f'  [{leg.name}] timeout')
        return None
    except Exception as e:
        print(f'  [{leg.name}] error: {str(e)[:120]}')
        return None


# ── Merge + rank ──────────────────────────────────────────────────────────────

def _merge_results(phrase_lists: list[list], sentence_lists: list[list]) -> list[dict]:
    """Merge results from multiple legs, deduplicate, rank by value."""
    seen:    dict[str, dict] = {}

    def _normalize(text: str) -> str:
        return re.sub(r'\s+', ' ', text.strip().lower())

    def _add(item: dict, kind: str) -> None:
        text = item.get('phrase') or item.get('sentence') or ''
        if not text:
            return
        key = _normalize(text)
        occ   = int(item.get('occurrences', 1))
        words = int(item.get('words', len(text.split())))
        value = occ * words
        if key in seen:
            # take higher occurrence count if seen again
            if occ > seen[key]['occurrences']:
                seen[key]['occurrences'] = occ
                seen[key]['value']       = occ * seen[key]['words']
        else:
            seen[key] = {
                'text':        text.strip(),
                'kind':        kind,
                'occurrences': occ,
                'words':       words,
                'value':       value,
            }

    for lst in phrase_lists:
        if lst:
            for item in lst:
                _add(item, 'phrase')

    for lst in sentence_lists:
        if lst:
            for item in lst:
                _add(item, 'sentence')

    ranked = sorted(seen.values(), key=lambda x: x['value'], reverse=True)
    return ranked


# ── Symbol table builder ──────────────────────────────────────────────────────

# Greek alphabet — lowercase then uppercase, giving 48 single-letter slots
_GREEK_LOWER = list('αβγδεζηθικλμνξοπρστυφχψω')
_GREEK_UPPER = list('ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ')
_SYMBOLS     = _GREEK_LOWER + _GREEK_UPPER


def _build_symbol_table(ranked: list[dict], top: int, min_value: int) -> list[dict]:
    table = []
    sym_idx = 0
    for entry in ranked:
        if entry['value'] < min_value:
            continue
        if len(table) >= top:
            break
        symbol = _SYMBOLS[sym_idx] if sym_idx < len(_SYMBOLS) else f'φ{sym_idx}'
        table.append({
            'symbol':      symbol,
            'text':        entry['text'],
            'kind':        entry['kind'],
            'occurrences': entry['occurrences'],
            'words':       entry['words'],
            'value':       entry['value'],
            'tokens_saved': (entry['occurrences'] - 1) * entry['words'],
        })
        sym_idx += 1
    return table


def _format_table(table: list[dict]) -> str:
    lines = ['# DOT Compression Symbol Table', '',
             'Generated by dot_compress.py — Universal Translator DOT', '',
             'Include this header in the cached system prompt:', '']
    lines.append('## Symbol Definitions')
    lines.append('')
    for entry in table:
        lines.append(f"`{entry['symbol']}` = {entry['text']}")
    lines.append('')
    lines.append('## Compression Statistics')
    lines.append('')
    total_saved = sum(e['tokens_saved'] for e in table)
    lines.append(f'- Symbols defined: {len(table)}')
    lines.append(f'- Estimated tokens saved per full DOT injection: ~{total_saved}')
    lines.append(f'- At $3/1M tokens (Sonnet): ~${total_saved * 3 / 1_000_000:.4f} per injection')
    lines.append(f'- At 41 accumulated runs: ~${total_saved * 3 * 41 / 1_000_000:.4f} total retroactive saving')
    lines.append('')
    lines.append('## Full Table (ranked by compression value)')
    lines.append('')
    lines.append('| Symbol | Kind | Occurrences | Words | Value | Tokens saved |')
    lines.append('|--------|------|-------------|-------|-------|--------------|')
    for e in table:
        lines.append(
            f"| `{e['symbol']}` | {e['kind']} | {e['occurrences']} "
            f"| {e['words']} | {e['value']} | {e['tokens_saved']} |"
        )
    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    print('DOT Compression Analyzer — Universal Translator DOT')
    print('=' * 55)

    dot = _load_dot()
    print(f'DOT loaded: {len(dot):,} characters (~{len(dot)//4:,} tokens)')

    phrase_prompt   = _PHRASE_PROMPT_PATH.read_text(encoding='utf-8')
    sentence_prompt = _SENTENCE_PROMPT_PATH.read_text(encoding='utf-8')

    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()
    legs = _pick_legs(registry)

    if not legs:
        print('ERROR: No usable legs found.')
        sys.exit(1)

    print(f'\nLegs selected ({len(legs)}):')
    for leg in legs:
        print(f'  {leg.name:20} {leg.model}')
    print()

    # Run both passes on all available legs — OpenAI + Ollama
    phrase_legs   = legs[:2] if len(legs) >= 2 else legs
    sentence_legs = legs[1:] or legs[:1]   # second leg or fallback to first

    print('Running phrase analysts...')
    phrase_tasks = [_call_analyst(leg, phrase_prompt, dot) for leg in phrase_legs]
    phrase_lists = await asyncio.gather(*phrase_tasks)
    for leg, result in zip(phrase_legs, phrase_lists):
        count = len(result) if result else 0
        print(f'  [{leg.name}] {count} phrases found')

    print('\nRunning sentence analysts...')
    sentence_tasks = [_call_analyst(leg, sentence_prompt, dot) for leg in sentence_legs]
    sentence_lists = await asyncio.gather(*sentence_tasks)
    for leg, result in zip(sentence_legs, sentence_lists):
        count = len(result) if result else 0
        print(f'  [{leg.name}] {count} sentences found')

    print('\nMerging and ranking...')
    ranked = _merge_results(list(phrase_lists), list(sentence_lists))
    print(f'  {len(ranked)} unique entries after merge')

    table = _build_symbol_table(ranked, top=args.top, min_value=args.min_value)
    print(f'  {len(table)} entries meet threshold (min-value={args.min_value})')

    # Output
    out_json = Path('dot_compression_table.json')
    out_md   = Path('dot_compression_table.md')

    out_json.write_text(json.dumps(table, indent=2, ensure_ascii=False), encoding='utf-8')
    out_md.write_text(_format_table(table), encoding='utf-8')

    print(f'\nOutputs written:')
    print(f'  {out_json.resolve()}')
    print(f'  {out_md.resolve()}')

    total_saved = sum(e['tokens_saved'] for e in table)
    print(f'\nEstimated tokens saved per injection: ~{total_saved}')
    print(f'At 41 runs that\'s ~{total_saved * 41:,} tokens retroactively.')
    print('\nDone.')


def _parse_args():
    p = argparse.ArgumentParser(description='DOT compression analyzer')
    p.add_argument('--top',       type=int, default=48, help='Max symbols to generate (default: 48)')
    p.add_argument('--min-value', type=int, default=10, help='Min compression value to include (default: 10)')
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
