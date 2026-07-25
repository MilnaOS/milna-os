"""
fix_wrapped_entries.py — Unwrap entries where the LLM echoed the schema envelope.

Fixes files where the top level is {"description": "...", "fields": {...}}
by replacing them with just the fields object.

Usage:
    python fix_wrapped_entries.py --topic battletech_mechs
    python fix_wrapped_entries.py --topic battletech_mechs --dry-run
"""
import argparse
import json
from pathlib import Path


def fix_topic(topic: str, dry_run: bool) -> None:
    entries_dir = Path('kb') / topic / 'entries'
    if not entries_dir.exists():
        print(f'No entries dir for topic "{topic}"')
        return

    fixed = skipped = already_flat = 0

    for f in sorted(entries_dir.rglob('*.json')):
        if f.name == '_index.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'  [skip] {f.name}: {e}')
            skipped += 1
            continue

        if not isinstance(data, dict):
            skipped += 1
            continue

        if 'fields' in data and isinstance(data['fields'], dict):
            unwrapped = data['fields']
            # Only write if the unwrapped object has at least one non-null value
            if any(v is not None for v in unwrapped.values()):
                if not dry_run:
                    f.write_text(json.dumps(unwrapped, indent=2), encoding='utf-8')
                fixed += 1
            else:
                # All-null fields object — delete the file (false positive)
                if not dry_run:
                    f.unlink()
                print(f'  [delete] {f.name} — all-null fields')
                skipped += 1
        else:
            already_flat += 1

    print(f'\nTopic: {topic}')
    print(f'  Fixed (unwrapped):  {fixed}')
    print(f'  Already flat:       {already_flat}')
    print(f'  Skipped/deleted:    {skipped}')
    if dry_run:
        print('  (dry run — no files written)')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--topic',   required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    fix_topic(args.topic, args.dry_run)
