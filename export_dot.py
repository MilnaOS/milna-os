"""
export_dot.py — Save the assembled DOT for a topic to a standalone file.

load_dot_content() assembles the DOT on-the-fly from raw/*.md files each
build run.  This script persists that assembled content so it can be:
  - Bundled into the Cloudflare Worker as a static context anchor
  - Versioned and checksummed per module release
  - Inspected / diffed between builds

Usage:
    python export_dot.py --topic batteltech
    python export_dot.py --topic starfinder --out custom/path/starfinder_dot.md
"""
import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from kilonova.tools.persona_curator import load_dot_content


def main(args) -> None:
    topic   = args.topic
    kb_dir  = Path('kb') / topic

    if not kb_dir.exists():
        print(f'ERROR: KB directory not found: {kb_dir}')
        sys.exit(1)

    print(f'Exporting DOT — {topic}')
    print('=' * 40)

    dot = load_dot_content(kb_dir)

    if not dot.strip():
        print(f'ERROR: No content found in {kb_dir}')
        print('Run the ingest pipeline or scrape_corpus.py first.')
        sys.exit(1)

    out_path = Path(args.out) if args.out else Path('personas') / f'{topic}_dot.md'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dot, encoding='utf-8')

    checksum = hashlib.sha256(dot.encode()).hexdigest()[:12]

    print(f'  chars    : {len(dot):,}')
    print(f'  words    : {len(dot.split()):,}')
    print(f'  checksum : sha256:{checksum}...')
    print(f'  saved to : {out_path}')


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Export assembled DOT to a standalone file'
    )
    parser.add_argument('--topic', required=True, help='KB topic name (matches kb/<topic>/)')
    parser.add_argument('--out', help='Output path (default: personas/<topic>_dot.md)')
    return parser.parse_args()


if __name__ == '__main__':
    main(_parse_args())
