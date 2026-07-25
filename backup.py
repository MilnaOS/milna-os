"""
backup.py — Mirror Runtime KB and core files to OneDrive.

OneDrive handles versioning automatically (keeps 30+ days of file history),
so we just sync — no date-stamped folders needed.

Usage:
    python backup.py          # full backup
    python backup.py --kb     # KB only (faster)
    python backup.py --dry    # show what would be copied, don't copy
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

RUNTIME     = Path('D:/Runtime')
ONEDRIVE    = Path('C:/Users/jerem/OneDrive')
BACKUP_ROOT = ONEDRIVE / 'Runtime'

# What to mirror — (source, dest_name)
KB_TARGETS = [
    (RUNTIME / 'kb', BACKUP_ROOT / 'kb'),
]

CORE_TARGETS = [
    (RUNTIME / 'milna.py',          BACKUP_ROOT / 'milna.py'),
    (RUNTIME / 'kilonova_mcp.py',   BACKUP_ROOT / 'kilonova_mcp.py'),
    (RUNTIME / 'backup.py',         BACKUP_ROOT / 'backup.py'),
    (RUNTIME / 'pyproject.toml',    BACKUP_ROOT / 'pyproject.toml'),
    (RUNTIME / 'kilonova',          BACKUP_ROOT / 'kilonova'),
]


def _copy(src: Path, dst: Path, dry: bool) -> tuple[int, int]:
    """Copy src → dst. Returns (files_copied, bytes_copied)."""
    files = 0
    size  = 0
    if src.is_file():
        if not dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        files += 1
        size  += src.stat().st_size
    elif src.is_dir():
        for f in src.rglob('*'):
            if f.is_file():
                rel  = f.relative_to(src)
                dest = dst / rel
                if not dry:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                files += 1
                size  += f.stat().st_size
    return files, size


def run(kb_only: bool = False, dry: bool = False) -> dict:
    if not ONEDRIVE.exists():
        return {'ok': False, 'error': 'OneDrive not found at ' + str(ONEDRIVE)}

    if not dry:
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    targets = KB_TARGETS + ([] if kb_only else CORE_TARGETS)

    total_files = 0
    total_bytes = 0
    copied = []

    for src, dst in targets:
        if not src.exists():
            print(f'  skip (not found): {src}')
            continue
        f, b = _copy(src, dst, dry)
        total_files += f
        total_bytes += b
        label = 'would copy' if dry else 'copied'
        print(f'  {label}: {src.name} -> {dst} ({f} files, {b//1024}KB)')
        copied.append(str(src))

    # Write a backup receipt
    if not dry:
        receipt = BACKUP_ROOT / '_last_backup.txt'
        receipt.write_text(
            f'Backup: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
            f'Files : {total_files}\n'
            f'Size  : {total_bytes // 1024} KB\n'
            f'Items : {chr(10).join(copied)}\n',
            encoding='utf-8',
        )

    return {
        'ok':    True,
        'files': total_files,
        'kb':    total_bytes // 1024,
        'dry':   dry,
    }


def main():
    parser = argparse.ArgumentParser(description='Mirror Runtime to OneDrive')
    parser.add_argument('--kb',  action='store_true', help='KB only')
    parser.add_argument('--dry', action='store_true', help='Dry run')
    args = parser.parse_args()

    prefix = '[DRY RUN] ' if args.dry else ''
    print(f'{prefix}Backing up Runtime -> {BACKUP_ROOT}')
    result = run(kb_only=args.kb, dry=args.dry)
    if result['ok']:
        print(f'\n{"Would copy" if args.dry else "Done"}: {result["files"]} files, {result["kb"]} KB')
    else:
        print(f'\nError: {result["error"]}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
