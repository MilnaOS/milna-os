"""
dot_smoketest.py — Verify DOT compression is transparent to the model.

Runs the same question against:
  A) Raw DOT (28K chars)
  B) Symbol header + compressed DOT (~27K chars)

If both answers are substantively equivalent, compression is safe to wire in.

Usage:
    python dot_smoketest.py
    python dot_smoketest.py --question "How does the CFS handle pro-drop languages?"
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.core.legs  import LegRegistry, KeyInventory
from kilonova.tools.ingest import _parse_json_safe

_MEMORY_PATH    = Path(r'D:\shard-accelerator\runs\prompt_memory.json')
_UT_KEY         = '38fa9977c40113b4'
_TABLE_PATH     = Path(r'D:\Runtime\dot_compression_v2.json')
_COMPRESSED_PATH = Path(r'D:\Runtime\dot_compressed.txt')

_DEFAULT_Q = (
    "What does the transfer engine do when the target language cannot express "
    "a particular scope configuration with a single lexical item? "
    "Give a concrete example."
)

_SYSTEM_RAW = (
    "You are a linguistics expert. Answer the question using only the "
    "knowledge document provided. Be specific and concise."
)

_SYSTEM_COMPRESSED = (
    "You are a linguistics expert. The knowledge document below uses a "
    "symbol compression table — each Greek letter stands for a phrase or frame. "
    "Decode transparently and answer the question using only this document. "
    "Be specific and concise."
)


def _load_raw_dot() -> str:
    data = json.loads(_MEMORY_PATH.read_text(encoding='utf-8'))
    return data[_UT_KEY]['last_output']


def _load_compressed() -> str:
    table = json.loads(_TABLE_PATH.read_text(encoding='utf-8'))
    compressed_dot = _COMPRESSED_PATH.read_text(encoding='utf-8')

    header_lines = ['## Symbol Definitions\n']
    for entry in table:
        header_lines.append(f"`{entry['symbol']}` = {entry['text']}")
    header = '\n'.join(header_lines)

    return f"{header}\n\n---\n\n{compressed_dot}"


async def _ask(leg, system: str, context: str, question: str) -> str:
    user = f"KNOWLEDGE DOCUMENT:\n\n{context}\n\n---\n\nQUESTION: {question}"
    try:
        return await asyncio.wait_for(
            leg.provider.complete(leg.model, system, [{'role': 'user', 'content': user}]),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        return '[TIMEOUT]'
    except Exception as e:
        return f'[ERROR: {e}]'


async def main(args) -> None:
    print('DOT Smoke Test')
    print('=' * 50)
    print(f'Question: {args.question}\n')

    inventory = KeyInventory()
    registry  = LegRegistry(inventory)
    registry.build()

    # Pick best available leg
    active = list(registry.active_legs())
    leg = next((l for l in active if l.name == 'anthropic'), None)
    if not leg:
        leg = next((l for l in active if 'ollama' not in l.name), None)
    if not leg:
        leg = active[0] if active else None
    if not leg:
        print('ERROR: No legs available.')
        sys.exit(1)

    print(f'Leg: {leg.name} / {leg.model}\n')

    raw_dot        = _load_raw_dot()
    compressed_dot = _load_compressed()

    raw_tokens  = len(raw_dot) // 4
    comp_tokens = len(compressed_dot) // 4

    print(f'Raw DOT:        {len(raw_dot):,} chars (~{raw_tokens:,} tokens)')
    print(f'Compressed DOT: {len(compressed_dot):,} chars (~{comp_tokens:,} tokens)')
    print(f'Reduction:      {100*(1 - comp_tokens/raw_tokens):.1f}%\n')

    print('Running both calls in parallel...')
    answer_raw, answer_comp = await asyncio.gather(
        _ask(leg, _SYSTEM_RAW,        raw_dot,        args.question),
        _ask(leg, _SYSTEM_COMPRESSED, compressed_dot, args.question),
    )

    sep = '-' * 50

    out_path = Path(r'D:\Runtime\smoketest_results.txt')
    results = [
        sep,
        'A) RAW DOT ANSWER:',
        sep,
        answer_raw,
        '',
        sep,
        'B) COMPRESSED DOT ANSWER:',
        sep,
        answer_comp,
        '',
        sep,
        'SMOKE TEST CHECKLIST:',
        '  [ ] Both answers mention the same mechanism',
        '  [ ] Compressed answer is not missing key terms',
        '  [ ] No symbol leakage (no raw Greek letters in answer)',
        '  [ ] Concrete example quality is comparable',
        sep,
    ]
    out_path.write_text('\n'.join(results), encoding='utf-8')
    print(f'Results written to: {out_path}')

    # Quick symbol-leakage check
    greek = [c for c in answer_comp if 'Ͱ' <= c <= 'Ͽ']
    if greek:
        unique = sorted(set(ord(c) for c in greek))
        print(f'  WARNING: {len(greek)} Greek chars leaked — codepoints: {unique}')
    else:
        print('  OK: No symbol leakage in compressed answer')


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--question', default=_DEFAULT_Q)
    return p.parse_args()


if __name__ == '__main__':
    asyncio.run(main(_parse_args()))
