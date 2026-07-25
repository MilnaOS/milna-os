"""
encode_text.py — Apply symbol table (Layer 1 + Layer 2+) to any plain text file.

Two-pass application:
  Pass 1: Layer 1 — replace raw text phrases with base symbols
  Pass 2: Layer 2+ — replace adjacent base symbol sequences with compound symbols

Usage:
    python encode_text.py input.txt
    python encode_text.py input.txt --output compressed.txt
    python encode_text.py input.txt --table D:\\corpus\\layer1_validated.json
    python encode_text.py input.txt --table D:\\corpus\\symbol_table_combined.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

_DEFAULT_TABLE = Path(r'D:\corpus\corpus_symbols_words1.json')


def load_table(path: Path) -> tuple[list[dict], list[dict]]:
    """Load symbol table and split into Layer 1 (text phrases) and Layer 2+ (sequences)."""
    table = json.loads(path.read_text(encoding='utf-8'))
    l1 = [e for e in table if e.get('layer', 1) == 1 and '[' not in e.get('text', '')]
    l2_plus = [e for e in table if e.get('layer', 1) > 1]
    return l1, l2_plus


def build_header(l1: list[dict], l2_plus: list[dict]) -> str:
    lines = ['## Symbol Key\n', '### Layer 1 — base phrases\n']
    for e in l1:
        lines.append(f"`{e['symbol']}` = {e['text']}")
    if l2_plus:
        lines += ['\n### Layer 2 — compound pairs\n']
        for e in l2_plus:
            lines.append(f"`{e['symbol']}` = [{e['sequence']}] ({e['text']})")
    return '\n'.join(lines)


def apply_symbols(text: str, l1: list[dict], l2_plus: list[dict]) -> tuple[str, int, int]:
    """
    Two-pass substitution.

    Pass 1: Apply Layer 1 phrases to raw text (longest phrase first).
    Pass 2: Apply Layer 2+ sequences to the L1-compressed text (longest sequence first).

    Returns (compressed_text, l1_hits, l2_hits).
    """
    result = text
    l1_hits = 0

    # Pass 1: text phrases → base symbols
    for entry in sorted(l1, key=lambda x: -x['words']):
        pattern = rf'(?i)(?<![a-zA-Z]){re.escape(entry["text"])}(?![a-zA-Z])'
        new, n = re.subn(pattern, entry['symbol'], result)
        l1_hits += n
        result = new

    # Pass 2: collapse adjacent symbols into compound tokens (Layer 2, automatic)
    # No lookup table needed — the LLM infers compound meaning from component symbols.
    # α="with" η="of the" → αη="with of the" — compositional, not enumerated.
    sym_alt = '|'.join(re.escape(c) for c in {e['symbol'] for e in l1})
    compound_pattern = rf'({sym_alt})[ \t]+({sym_alt})'
    prev = None
    l2_hits = 0
    while prev != result:
        prev = result
        result, n = re.subn(compound_pattern, r'\1\2', result)
        l2_hits += n

    return result, l1_hits, l2_hits


def main():
    p = argparse.ArgumentParser(description='Apply symbol table to a text file')
    p.add_argument('input',  help='Input text file (UTF-8)')
    p.add_argument('--output',    help='Output file (default: stdout)')
    p.add_argument('--table',     default=str(_DEFAULT_TABLE), help='Symbol table JSON')
    p.add_argument('--no-header', action='store_true', help='Omit symbol key header')
    args = p.parse_args()

    table_path = Path(args.table)
    if not table_path.exists():
        print(f'ERROR: Table not found: {table_path}', file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f'ERROR: Input not found: {input_path}', file=sys.stderr)
        sys.exit(1)

    l1, l2_plus = load_table(table_path)
    raw = input_path.read_text(encoding='utf-8')
    compressed, l1_hits, l2_hits = apply_symbols(raw, l1, l2_plus)

    header = '' if args.no_header else build_header(l1, l2_plus) + '\n\n---\n\n'
    output = header + compressed

    raw_chars  = len(raw)
    comp_chars = len(compressed)
    reduction  = 100 * (1 - comp_chars / raw_chars)

    print(f'Input:        {raw_chars:,} chars',       file=sys.stderr)
    print(f'Compressed:   {comp_chars:,} chars',      file=sys.stderr)
    print(f'Reduction:    {reduction:.1f}%',           file=sys.stderr)
    print(f'L1 hits:      {l1_hits}',                 file=sys.stderr)
    print(f'L2 hits:      {l2_hits}',                 file=sys.stderr)
    print(f'L1 symbols:   {len(l1)} loaded',          file=sys.stderr)
    print(f'L2 symbols:   {len(l2_plus)} loaded',     file=sys.stderr)

    if args.output:
        Path(args.output).write_text(output, encoding='utf-8')
        print(f'Written to:   {args.output}',         file=sys.stderr)
    else:
        sys.stdout.buffer.write(output.encode('utf-8'))


if __name__ == '__main__':
    main()
