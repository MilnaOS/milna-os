"""
Milna OS — CLI entry point.
Powered by Kilonova.

Usage:
    python milna.py                  # start chat
    python milna.py --mode potato    # override mode
    python milna.py --add-key        # store an API key in OS credential store

Commands during chat:
    /deep       — full multi-round distillation on next message
    /mode       — show or change mode (potato/economy/standard/blade)
    /legs       — show active legs and their status
    /reset      — clear conversation history
    /keys       — manage API keys
    /help       — show commands
    exit / quit — shutdown
"""
import asyncio
import argparse
import json as _json
import os
import re
import sys
import getpass
import tempfile
import zipfile
from pathlib import Path

# Allow running from D:\Runtime directly
sys.path.insert(0, str(Path(__file__).parent))

import keyring
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TaskProgressColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from kilonova.core.legs import KeyInventory, LegRegistry, PROVIDER_DEFAULTS
from kilonova.tools.loader import load_file, list_folder, build_context_block, already_ingested
from kilonova.tools.ingest import Ingestor
from kilonova.tools.kb_tape import (
    load_tape, save_tape, tag_all_records, tape_summary, KNOWN_SETTINGS
)
from kilonova.tools.web_fetch import fetch_url, fetch_url_raw_html, extract_links
from kilonova.tools.kb_search import search_kb
from kilonova.core.mode import ModeController, MODES, suggest_mode
from kilonova.core.engine import ConversationEngine, MILNA_SYSTEM
from kilonova.core.session_log import SessionLogger

console = Console()

# Set by main() from --data-dir / MILNA_DATA_DIR / ~/.milna default
KB_ROOT:    Path = Path('kb')
LOG_DIR:    Path = Path('session_logs')
BENCH_FILE: Path = Path('session_logs') / 'bench.json'


SMALL_MODEL_HINTS = ('0.5b', '1b', '1.5b', '3b')


def _pick_ollama_model(models: list[str], mode: str) -> str:
    """Auto-select Ollama model. Potato/economy = smallest available; others = first."""
    if mode in ('potato', 'economy'):
        for hint in SMALL_MODEL_HINTS:
            for m in models:
                if hint in m.lower():
                    return m
    return models[0]


def _session_path() -> Path:
    return LOG_DIR / '_session.json'


def _load_bench(registry) -> None:
    try:
        if BENCH_FILE.exists():
            registry.benched = set(_json.loads(BENCH_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass


def _save_bench(registry) -> None:
    try:
        BENCH_FILE.write_text(
            _json.dumps(sorted(registry.benched), indent=2), encoding='utf-8'
        )
    except Exception:
        pass


def _save_session(engine, active_kb: str | None) -> None:
    try:
        _session_path().write_text(
            _json.dumps({'history': engine.history, 'active_kb': active_kb}, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def _load_session(engine) -> str | None:
    path = _session_path()
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding='utf-8'))
        history = data.get('history', [])
        if not history:
            return None
        engine.history = history
        engine._anchor_count = 0
        return data.get('active_kb')
    except Exception:
        return None


def _clear_session() -> None:
    try:
        p = _session_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def print_banner() -> None:
    console.print()
    console.print(Panel(
        Text.assemble(
            ('MILNA OS\n', 'bold cyan'),
            ('Powered by Kilonova  •  Open-Source Energy  •  Born in Oregon\n', 'dim'),
            ('BYOK — your keys, your compute, your output.', 'dim italic'),
        ),
        border_style='cyan',
        padding=(1, 4),
    ))
    console.print()


def show_legs(registry: LegRegistry) -> None:
    table = Table(title='Active Legs', border_style='cyan')
    table.add_column('Leg',    style='bold')
    table.add_column('Model')
    table.add_column('Status')
    for leg in registry.legs.values():
        status = '[green]LIVE[/green]' if leg.alive else f'[red]DEAD — {leg.error[:40]}[/red]'
        table.add_row(leg.name, leg.model, status)
    console.print(table)


def show_mode(mc: ModeController) -> None:
    console.print(f'[cyan]Mode:[/cyan] {mc.summary()}')
    console.print(f'[dim]Suggested for this hardware: {mc.suggested.upper()}[/dim]')
    console.print(f'[dim]Available: {", ".join(MODES)}[/dim]')


PROVIDER_MENU = {
    '1': ('anthropic', 'Anthropic  (Claude)'),
    '2': ('openai',    'OpenAI     (GPT-4o / GPT-4o-mini)'),
    '3': ('gemini',    'Gemini     (Google)'),
}


def add_key_flow(inventory: KeyInventory) -> None:
    console.print('\n[bold]Add API Key[/bold]')
    console.print('[dim]Keys are stored in Windows Credential Manager — never on disk in plaintext.[/dim]\n')
    for num, (_, label) in PROVIDER_MENU.items():
        stored = inventory.get_key(list(PROVIDER_MENU.values())[int(num)-1][0])
        tag = ' [green](key stored)[/green]' if stored else ''
        console.print(f'  [cyan]{num}[/cyan]  {label}{tag}')
    console.print()
    choice = Prompt.ask(
        '[bold green]>[/bold green]',
        choices=list(PROVIDER_MENU.keys()),
        default='1',
    )
    provider, label = PROVIDER_MENU[choice]
    console.print(f'[dim]Paste your {label} API key and press Enter[/dim]')
    console.print(f'[dim](key is visible while typing - goes to Windows Credential Manager, never to disk)[/dim]')
    try:
        key = input('Key: ').strip()
    except EOFError:
        key = ''
    if key:
        inventory.store_key(provider, key)
        console.print(f'[green]Key stored for {label}.[/green]')
    else:
        console.print('[yellow]No key entered - skipped.[/yellow]')


async def handle_command(
    cmd: str,
    inventory: KeyInventory,
    registry: LegRegistry,
    mc: ModeController,
    engine: ConversationEngine,
) -> tuple[bool, bool]:
    """Returns (handled, deep_next_turn)."""
    parts = cmd.strip().split()
    name  = parts[0].lower()

    if name == '/help':
        console.print(
            '\n[bold]Commands:[/bold]\n'
            '  [cyan]/deep[/cyan]          — full distillation on next message\n'
            '  [cyan]/mode[/cyan]          — show current mode\n'
            '  [cyan]/mode <name>[/cyan]   — switch mode (potato/economy/standard/blade)\n'
            '  [cyan]/legs[/cyan]          — show leg status\n'
            '  [cyan]/reset[/cyan]         — clear conversation history\n'
            '  [cyan]/load <path>[/cyan]   — load PDF(s) into conversation context\n'
            '  [cyan]/ingest <folder>[/cyan]   — build persistent knowledge base from PDFs\n'
            '  [cyan]/ingest <file.zip>[/cyan] — extract zip and ingest all PDFs inside\n'
            '  [cyan]/ingest <folder> --topic <name>[/cyan] — ingest with custom topic name\n'
            '  [cyan]/ingest <folder> --deep[/cyan] — deep distillation (richer extraction, higher cost)\n'
            '  [cyan]/ingest --rebuild <topic>[/cyan] — rebuild schemas from existing raw docs\n'
            '  [cyan]/ingest --rebuild <topic> --force[/cyan] — wipe and re-extract all records\n'
            '  [cyan]/ingest --rebuild <topic> --force --deep[/cyan] — wipe, re-extract with deep distillation\n'
            '  [cyan]/kb[/cyan]                              — list available knowledge bases\n'
            '  [cyan]/kb <topic>[/cyan]                    — load KB into conversation as context anchor\n'
            '  [cyan]/kb <topic> tape <tag>[/cyan]         — mask a setting (e.g. wwii)\n'
            '  [cyan]/kb <topic> untape <tag>[/cyan]       — unmask a setting\n'
            '  [cyan]/kb <topic> status[/cyan]             — show record counts by setting\n'
            '  [cyan]/kb <topic> retag[/cyan]              — re-run setting tagger on all records\n'
            '  [cyan]/fetch <url>[/cyan]   — fetch a web page into conversation context\n'
            '  [cyan]/webseed <topic> <url>[/cyan] — extract entities from a web page into DOT\n'
            '  [cyan]/webseed <topic> <url> --follow[/cyan] — crawl linked pages (depth 1, --limit <n> default 20)\n'
            '  [cyan]/webseed <topic> <url> --depth <n>[/cyan] — BFS crawl N levels deep\n'
            '  [cyan]/webseed <topic> <url> --entity <type>[/cyan] — extract one entity type only\n'
            '  [cyan]/webseed <topic> <url> --force[/cyan]        — ignore crawl state, re-fetch all URLs\n'
            '  [cyan]/webseed <topic> <url> --start-letter V[/cyan] — skip URLs before letter V\n'
            '  [cyan]/keys[/cyan]          — add or list API keys\n'
            '  [cyan]/bench[/cyan]          — show model roster (active vs benched)\n'
            '  [cyan]/bench <model>[/cyan]  — toggle model between active and bench\n'
            '  [cyan]/ollama[/cyan]         — list Ollama models\n'
            '  [cyan]/ollama <model>[/cyan] — switch Ollama model\n'
            '  [cyan]/help[/cyan]          — this message\n'
            '  [cyan]exit / quit[/cyan]    — shutdown\n'
        )
        return True, False

    if name == '/legs':
        show_legs(registry)
        return True, False

    if name == '/mode':
        if len(parts) > 1:
            try:
                mc.set(parts[1])
                console.print(f'[green]Mode set to {mc.mode.name.upper()}.[/green]')
            except ValueError as e:
                console.print(f'[red]{e}[/red]')
        else:
            show_mode(mc)
        return True, False

    if name == '/reset':
        engine.reset()
        console.print('[dim]Conversation history cleared.[/dim]')
        return True, False

    if name == '/keys':
        stored = inventory.list_stored()
        if stored:
            console.print(f'[dim]Keys stored for: {", ".join(stored)}[/dim]')
        else:
            console.print('[dim]No keys stored yet.[/dim]')
        add_key_flow(inventory)
        return True, False

    if name == '/deep':
        console.print('[cyan]Deep mode active for next message - full distillation.[/cyan]')
        return True, True

    if name == '/load':
        folder_or_file = ' '.join(parts[1:]).strip().strip('"')
        if not folder_or_file:
            console.print('[dim]Usage: /load <file.pdf>  or  /load <folder>[/dim]')
            return True, False
        target = Path(folder_or_file)
        if target.is_dir():
            files = list_folder(target)
            if not files:
                console.print('[yellow]No supported files found in folder.[/yellow]')
                return True, False
            console.print(f'\n[bold]Files in {target.name}:[/bold]')
            for i, f in enumerate(files, 1):
                size_mb = f.stat().st_size / 1_048_576
                console.print(f'  [cyan]{i}[/cyan]  {f.name}  [dim]({size_mb:.1f} MB)[/dim]')
            console.print()
            raw = input('Load which? (numbers separated by spaces, or "all"): ').strip()
            if raw.lower() == 'all':
                picks = files
            else:
                indices = [int(x) - 1 for x in raw.split() if x.isdigit()]
                picks = [files[i] for i in indices if 0 <= i < len(files)]
        else:
            picks = [target]

        results = []
        for p in picks:
            with console.status(f'[cyan]Reading {p.name}...[/cyan]', spinner='dots'):
                result = load_file(p)
            if result['ok']:
                console.print(
                    f'[green]Loaded:[/green] {result["name"]}  '
                    f'[dim]{result["words_used"]:,} words'
                    f'{"  (truncated)" if result["truncated"] else ""}[/dim]'
                )
            else:
                console.print(f'[red]Failed:[/red] {result.get("error")}')
            results.append(result)

        context = build_context_block(results)
        if context:
            engine.history.insert(0, {'role': 'user',      'content': context})
            engine.history.insert(1, {'role': 'assistant', 'content':
                'Documents loaded. I have read the provided material and am ready to discuss it.'})
            engine._anchor_count += 2
            ok_count = sum(1 for r in results if r['ok'])
            console.print(f'\n[cyan]{ok_count} document(s) injected into conversation context.[/cyan]')
            console.print('[dim]Ask anything about the loaded material.[/dim]\n')
        return True, False

    if name == '/ingest':
        # Build folder path from non-flag parts only
        folder_parts = [p for p in parts[1:] if not p.startswith('--')]
        folder_or_file = ' '.join(folder_parts).strip().strip('"')
        topic_arg = None

        # /ingest --rebuild <topic> [--deep] — re-run schemas+entities from existing raw docs
        if '--rebuild' in parts:
            idx = parts.index('--rebuild')
            rebuild_topic = parts[idx + 1] if idx + 1 < len(parts) else ''
            if not rebuild_topic:
                console.print('[red]Usage: /ingest --rebuild <topic>[/red]')
                return True, False
            rebuild_deep  = '--deep'  in parts
            rebuild_force = '--force' in parts
            mode_note = ''.join([
                '  [cyan][deep][/cyan]'  if rebuild_deep  else '',
                '  [cyan][force][/cyan]' if rebuild_force else '',
            ])
            console.print(f'\n[cyan]Rebuilding schemas and entities for DOT: {rebuild_topic}[/cyan]{mode_note}\n')
            ingestor = Ingestor(engine, kb_root=KB_ROOT)
            with Progress(
                SpinnerColumn(),
                TextColumn('[bold cyan]{task.description}[/bold cyan]'),
                BarColumn(bar_width=35),
                TaskProgressColumn(),
                TextColumn('[dim]{task.fields[entity]}[/dim]'),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f'Rebuilding {rebuild_topic}', total=None, entity='')

                def _prog(msg):
                    progress.console.print(f'  [dim cyan]{msg}[/dim cyan]')

                def _tick(current, total, entity_type):
                    progress.update(task, total=total, completed=current, entity=entity_type)

                summary = await ingestor.rebuild_from_raw(
                    topic=rebuild_topic,
                    progress_cb=_prog,
                    progress_tick=_tick,
                    deep=rebuild_deep,
                    force=rebuild_force,
                )
            if not summary['ok']:
                console.print(f'[red]Rebuild failed: {summary.get("error")}[/red]')
            else:
                console.print(f'\n[bold green]Rebuild complete.[/bold green]')
                console.print(f'  Entity types: {summary["entity_types"]}')
                console.print(f'  Records:      {summary["total_records"]}')
                console.print(f'  KB location:  {summary["kb_dir"]}')
            return True, False

        # Support: /ingest <folder> --topic <name>
        if '--topic' in parts:
            idx = parts.index('--topic')
            if idx + 1 < len(parts):
                topic_arg = parts[idx + 1]
                # Rebuild folder path excluding --topic, its value, and all other flags
                topic_val = parts[idx + 1]
                folder_parts = [
                    p for p in parts[1:]
                    if p != '--topic' and p != topic_val and not p.startswith('--')
                ]
                folder_or_file = ' '.join(folder_parts).strip().strip('"')

        if not folder_or_file:
            console.print('[dim]Usage: /ingest <folder>  or  /ingest <folder> --topic <name>[/dim]')
            return True, False

        target = Path(folder_or_file)
        if not target.exists():
            console.print(f'[red]Path not found: {target}[/red]')
            return True, False

        _zip_tmpdir = None
        if target.suffix.lower() == '.zip':
            if not zipfile.is_zipfile(target):
                console.print(f'[red]Not a valid zip file: {target}[/red]')
                return True, False
            _zip_tmpdir = tempfile.mkdtemp(prefix='milna_ingest_')
            console.print(f'[dim]Extracting {target.name}...[/dim]')
            with zipfile.ZipFile(target, 'r') as zf:
                zf.extractall(_zip_tmpdir)
            target = Path(_zip_tmpdir)
            console.print(f'[dim]Extracted to temp folder — ingesting...[/dim]')
        elif not target.is_dir():
            console.print('[red]/ingest requires a folder or .zip file.[/red]')
            return True, False

        deep_distill = '--deep' in parts
        topic = topic_arg or target.name.lower().replace(' ', '_')
        mode_note = '  [cyan][deep distillation][/cyan]' if deep_distill else ''
        console.print(f'\n[bold cyan]DOT:[/bold cyan] {topic}  →  kb/{topic}/{mode_note}')

        # Show file picker — exclude files already in the DOT
        done = already_ingested(topic, kb_root=KB_ROOT)
        all_files = list_folder(target)
        available = list_folder(target, exclude=done)

        if not all_files:
            # Check one level of subfolders — maybe PDFs are organized by game system
            subfolders = sorted(p for p in target.iterdir() if p.is_dir())
            if subfolders:
                console.print('[yellow]No PDFs in root folder. Subfolders found:[/yellow]\n')
                for i, sf in enumerate(subfolders, 1):
                    count = len(list_folder(sf))
                    console.print(f'  [cyan]{i}[/cyan]  {sf.name}  [dim]({count} PDFs)[/dim]')
                console.print()
                console.print('[dim]Run /ingest on a subfolder, e.g.:[/dim]')
                console.print(f'[dim]  /ingest "{subfolders[0]}"[/dim]')
            else:
                console.print('[yellow]No PDF files found in folder.[/yellow]')
            return True, False

        console.print()
        for i, f in enumerate(all_files, 1):
            size_mb = f.stat().st_size / 1_048_576
            if f.name in done:
                console.print(f'  [dim]{i:2}  {f.name}  ({size_mb:.1f} MB)  [green]✓ in DOT[/green][/dim]')
            else:
                console.print(f'  [cyan]{i:2}[/cyan]  {f.name}  [dim]({size_mb:.1f} MB)[/dim]')

        if not available:
            console.print('\n[green]All files already ingested into the DOT.[/green]')
            console.print('[dim]Use /kb {topic} to load it into conversation.[/dim]')
            return True, False

        console.print(f'\n[dim]{len(done)} already in DOT  |  {len(available)} available[/dim]')
        console.print('[dim]Enter a number to queue a file  |  "all" to queue all remaining[/dim]')
        console.print('[dim]"ops" to clear the queue  |  "go" to confirm and start  |  "cancel" to abort[/dim]\n')

        queue: list[Path] = []

        def _print_queue():
            if not queue:
                console.print('[dim]  Queue is empty.[/dim]')
            else:
                for q in queue:
                    size_mb = q.stat().st_size / 1_048_576
                    console.print(f'  [green]+[/green] {q.name}  [dim]({size_mb:.1f} MB)[/dim]')
            console.print()

        while True:
            raw = input('queue> ').strip().lower()

            if raw == 'cancel':
                console.print('[dim]Cancelled.[/dim]')
                if _zip_tmpdir:
                    import shutil
                    shutil.rmtree(_zip_tmpdir, ignore_errors=True)
                return True, False

            if raw == 'ops':
                queue.clear()
                console.print('[yellow]Queue cleared.[/yellow]\n')
                continue

            if raw == 'go':
                if not queue:
                    console.print('[yellow]Queue is empty — add files first.[/yellow]')
                    continue
                # Confirm
                console.print(f'\n[bold]Ready to ingest {len(queue)} file(s) into DOT: {topic}[/bold]')
                _print_queue()
                confirm = input('Confirm? [y/n]: ').strip().lower()
                if confirm != 'y':
                    console.print('[dim]Cancelled. Queue preserved — type "go" again or "cancel".[/dim]\n')
                    continue
                break

            if raw == 'all':
                for f in available:
                    if f not in queue:
                        queue.append(f)
                console.print(f'[cyan]Queued all {len(available)} available files:[/cyan]')
                _print_queue()
                continue

            # Single number or space-separated numbers
            indices = [int(x) - 1 for x in raw.split() if x.isdigit()]
            if not indices:
                console.print('[dim]Enter a number, "all", "go", "ops", or "cancel".[/dim]')
                continue

            for idx in indices:
                if not (0 <= idx < len(all_files)):
                    console.print(f'[red]No file #{idx + 1}.[/red]')
                    continue
                f = all_files[idx]
                if f.name in done:
                    console.print(f'[dim]{f.name} already in DOT — skipped.[/dim]')
                    continue
                if f in queue:
                    console.print(f'[dim]{f.name} already queued.[/dim]')
                    continue
                queue.append(f)
                size_mb = f.stat().st_size / 1_048_576
                console.print(f'  [green]+[/green] {f.name}  [dim]({size_mb:.1f} MB)[/dim]')

            console.print(f'[dim]Queue: {len(queue)} file(s)  |  "go" to start  |  "ops" to clear[/dim]\n')

        picks = queue
        console.print(f'\n[cyan]Ingesting {len(picks)} file(s) into DOT: {topic}[/cyan]\n')
        ingestor = Ingestor(engine, kb_root=KB_ROOT)

        def _progress(msg: str):
            console.print(f'  [dim cyan]{msg}[/dim cyan]')

        summary = await ingestor.run(folder=target, topic=topic, files=picks, progress_cb=_progress, deep_distill=deep_distill)

        if not summary['ok']:
            console.print(f'[red]Ingest failed: {summary.get("error")}[/red]')
        else:
            console.print(f'\n[bold green]Ingest complete.[/bold green]')
            console.print(f'  Documents:    {summary["documents"]}')
            console.print(f'  Entity types: {summary["entity_types"]}')
            console.print(f'  Records:      {summary["total_records"]}')
            console.print(f'  KB location:  {summary["kb_dir"]}')
            console.print(f'  Overview:     {summary["overview"]}')
            console.print(f'\n[dim]Use /kb {topic} to load the KB into conversation context.[/dim]\n')

        if _zip_tmpdir:
            import shutil
            shutil.rmtree(_zip_tmpdir, ignore_errors=True)

        return True, False

    if name == '/kb':
        topic    = parts[1] if len(parts) > 1 else ''
        subcmd   = parts[2].lower() if len(parts) > 2 else ''
        tag_arg  = parts[3].lower() if len(parts) > 3 else ''

        if not topic:
            # List available KBs
            if not KB_ROOT.exists() or not any(KB_ROOT.iterdir()):
                console.print('[dim]No knowledge bases built yet. Use /ingest to build one.[/dim]')
            else:
                console.print('[bold]Available knowledge bases:[/bold]')
                for kb in sorted(KB_ROOT.iterdir()):
                    if not kb.is_dir():
                        continue
                    manifest_path = kb / 'manifest.json'
                    tape_note = ''
                    masked = load_tape(kb)
                    if masked:
                        tape_note = f'  [yellow]taped: {", ".join(sorted(masked))}[/yellow]'
                    if manifest_path.exists():
                        m = _json.loads(manifest_path.read_text())
                        docs = m.get('documents', [])
                        doc_count = len(docs) if isinstance(docs, list) else docs
                        console.print(
                            f'  [cyan]{kb.name}[/cyan]  '
                            f'[dim]{doc_count} docs  '
                            f'{m.get("total_records", "?")} records  '
                            f'ingested {m.get("ingested_at", "")[:10]}[/dim]'
                            f'{tape_note}'
                        )
                    else:
                        console.print(f'  [cyan]{kb.name}[/cyan]{tape_note}')
                console.print(
                    '[dim]Usage: /kb <topic>              — load KB\n'
                    '       /kb <topic> tape <tag>     — mask a setting (e.g. wwii)\n'
                    '       /kb <topic> untape <tag>   — unmask a setting\n'
                    '       /kb <topic> status         — show tape state and record counts[/dim]'
                )
            return True, False

        kb_dir = KB_ROOT / topic

        # ── tape / untape / status ────────────────────────────────────────────
        if subcmd in ('tape', 'untape', 'status', 'retag'):
            if not kb_dir.exists():
                console.print(f'[red]No KB found for topic: {topic}[/red]')
                return True, False

            if subcmd == 'retag':
                console.print(f'[cyan]Tagging all records in [{topic}]...[/cyan]')
                result = tag_all_records(kb_dir, progress_cb=lambda m: console.print(f'[dim]{m}[/dim]'))
                console.print(
                    f'[green]Done.[/green]  '
                    f'{result["total"]} records  •  '
                    f'[dim]{result.get("battletech", 0)} battletech  '
                    f'{result.get("wwii_crossover", 0)} wwii_crossover[/dim]'
                )
                return True, False

            if subcmd == 'status':
                summary = tape_summary(kb_dir)
                if not summary['tagged']:
                    console.print(f'[yellow]Records not yet tagged.[/yellow]  Run: /kb {topic} retag')
                    return True, False
                console.print(f'\n[bold]KB [{topic}] tape status:[/bold]')
                for setting, count in sorted(summary['counts'].items()):
                    label = KNOWN_SETTINGS.get(setting, setting)
                    masked_tag = '  [yellow][TAPED][/yellow]' if setting in summary['masked'] else ''
                    console.print(f'  [cyan]{setting}[/cyan]  {count} records  [dim]{label}[/dim]{masked_tag}')
                console.print(f'  [dim]Total: {summary["total"]}[/dim]\n')
                return True, False

            # tape / untape require a tag arg
            # Accept shorthand: 'wwii' → 'wwii_crossover'
            _tag_map = {'wwii': 'wwii_crossover', 'battletech': 'battletech'}
            full_tag = _tag_map.get(tag_arg, tag_arg)
            if not full_tag:
                console.print(f'[dim]Usage: /kb {topic} {subcmd} <tag>  (e.g. wwii)[/dim]')
                return True, False

            masked = load_tape(kb_dir)
            summary = tape_summary(kb_dir)

            if not summary['tagged']:
                # Auto-tag before applying tape
                console.print(f'[dim]Records not yet tagged — running tagger first...[/dim]')
                tag_all_records(kb_dir)
                summary = tape_summary(kb_dir)

            if subcmd == 'tape':
                if full_tag not in summary['counts']:
                    console.print(f'[yellow]No records tagged as "{full_tag}" in [{topic}].[/yellow]')
                    console.print(f'[dim]Available: {", ".join(sorted(summary["counts"].keys()))}[/dim]')
                    return True, False
                masked.add(full_tag)
                save_tape(kb_dir, masked)
                count = summary['counts'].get(full_tag, 0)
                console.print(
                    f'[yellow]Tape on:[/yellow] {count} [{full_tag}] records hidden in [{topic}].  '
                    f'[dim]Use /kb {topic} untape {tag_arg} to restore.[/dim]'
                )
            else:  # untape
                if full_tag not in masked:
                    console.print(f'[dim][{full_tag}] is not currently taped in [{topic}].[/dim]')
                    return True, False
                masked.discard(full_tag)
                save_tape(kb_dir, masked)
                count = summary['counts'].get(full_tag, 0)
                console.print(
                    f'[green]Tape off:[/green] {count} [{full_tag}] records restored in [{topic}].'
                )
            return True, False

        # ── Load KB into context ──────────────────────────────────────────────
        overview_path = kb_dir / 'overview.md'
        if not overview_path.exists():
            console.print(f'[red]No KB found for topic: {topic}[/red]')
            console.print(f'[dim]Run /ingest <folder> --topic {topic} first.[/dim]')
            return True, False

        overview_text = overview_path.read_text(encoding='utf-8')
        masked = load_tape(kb_dir)
        tape_note = ''
        if masked:
            summary = tape_summary(kb_dir)
            hidden_count = sum(
                summary['counts'].get(s, 0) for s in masked
            )
            tape_note = (
                f'\n\n---\n_Note: {hidden_count} records tagged as '
                f'[{", ".join(sorted(masked))}] are currently taped and not surfaced in responses. '
                f'Use /kb {topic} untape to restore._'
            )

        context = f'## KNOWLEDGE BASE: {topic.upper()}\n\n{overview_text}{tape_note}'
        engine.history.insert(0, {'role': 'user',      'content': context})
        engine.history.insert(1, {'role': 'assistant', 'content':
            f'Knowledge base for {topic} loaded. I have the full context anchor document '
            f'and am ready to answer questions grounded in this material.'})
        engine._anchor_count += 2
        console.print(f'[cyan]KB [{topic}] loaded into conversation context.[/cyan]')
        if masked:
            hidden_count = sum(tape_summary(kb_dir)['counts'].get(s, 0) for s in masked)
            console.print(
                f'[yellow]Tape active:[/yellow] {hidden_count} records hidden '
                f'({", ".join(sorted(masked))})  '
                f'[dim]/kb {topic} untape wwii to restore[/dim]'
            )
        console.print('[dim]All responses will be anchored to this knowledge base.[/dim]\n')
        return True, False

    if name == '/fetch':
        url = ' '.join(parts[1:]).strip().strip('"')
        if not url:
            console.print('[dim]Usage: /fetch <url>[/dim]')
            return True, False
        with console.status(f'[cyan]Fetching {url}...[/cyan]', spinner='dots'):
            result = fetch_url(url)
        if not result['ok']:
            console.print(f'[red]Fetch failed:[/red] {result["error"]}')
            return True, False
        console.print(
            f'[green]Fetched:[/green] {result["url"]}  '
            f'[dim]{result["words"]:,} words[/dim]'
        )
        context = f'## WEB CONTENT: {result["url"]}\n\n{result["text"]}'
        engine.history.insert(0, {'role': 'user',      'content': context})
        engine.history.insert(1, {'role': 'assistant', 'content':
            f'Web page loaded from {result["url"]}. Ready to discuss the content.'})
        engine._anchor_count += 2
        console.print('[dim]Page injected into conversation context.[/dim]\n')
        return True, False

    if name == '/webseed':
        # /webseed <topic> <url> [--entity <type>] [--follow] [--depth <n>] [--limit <n>]
        non_flag = [p for p in parts[1:] if not p.startswith('--')]
        if len(non_flag) < 2:
            console.print('[dim]Usage: /webseed <topic> <url> [--follow] [--depth <n>] [--limit <n>] [--entity <type>][/dim]')
            return True, False

        seed_topic   = non_flag[0]
        seed_url     = non_flag[1]
        follow       = '--follow' in parts
        force        = '--force'  in parts
        start_letter = None
        if '--start-letter' in parts:
            idx = parts.index('--start-letter')
            if idx + 1 < len(parts):
                start_letter = parts[idx + 1].upper()[0]
        entity_filter = None
        if '--entity' in parts:
            idx = parts.index('--entity')
            if idx + 1 < len(parts):
                entity_filter = parts[idx + 1]
        limit = 20
        if '--limit' in parts:
            idx = parts.index('--limit')
            if idx + 1 < len(parts) and parts[idx + 1].isdigit():
                limit = int(parts[idx + 1])
        depth = 0
        if '--depth' in parts:
            idx = parts.index('--depth')
            if idx + 1 < len(parts) and parts[idx + 1].isdigit():
                depth = int(parts[idx + 1])
        elif follow:
            depth = 1  # --follow is shorthand for --depth 1

        console.print(f'\n[bold cyan]WebSeed:[/bold cyan] {seed_topic} ← {seed_url}')
        if depth > 0:
            console.print(f'[dim]BFS crawl depth {depth}, limit {limit} pages[/dim]')

        # KB dir must be established before BFS so we can load crawl state
        kb_dir = KB_ROOT / seed_topic

        # Load persisted crawl state so resumed runs skip already-visited URLs
        state_path = kb_dir / '_crawl_state.json'
        crawl_state: dict = {}
        if state_path.exists():
            try:
                crawl_state = _json.loads(state_path.read_text(encoding='utf-8'))
            except Exception:
                crawl_state = {}
        already_visited: set[str] = set() if force else set(crawl_state.get('visited', []))
        if already_visited:
            console.print(f'[dim]Resuming — {len(already_visited)} URLs already visited[/dim]')
        elif force and crawl_state:
            console.print(f'[dim]--force: clearing crawl state, starting fresh[/dim]')
            crawl_state = {}

        # BFS URL collection up to --depth levels
        urls_to_fetch: list[str] = []
        if depth > 0:
            visited: set[str] = set(already_visited)  # seed from persisted state
            queue: list[tuple[str, int]] = [(seed_url, depth)]
            with console.status('[cyan]Crawling...[/cyan]', spinner='dots') as status:
                while queue and len(urls_to_fetch) < limit:
                    url, remaining = queue.pop(0)
                    if url in visited:
                        continue
                    visited.add(url)
                    urls_to_fetch.append(url)
                    status.update(f'[cyan]Crawling... {len(urls_to_fetch)} URLs found[/cyan]')
                    if remaining > 0:
                        raw = fetch_url_raw_html(url)
                        if raw['ok']:
                            links = extract_links(url, raw['html'])
                            for link in links:
                                if link not in visited:
                                    queue.append((link, remaining - 1))
            console.print(f'[dim]Crawled {len(urls_to_fetch)} URLs (depth {depth})[/dim]')
        else:
            if seed_url in already_visited and not force:
                console.print(f'[dim]Already visited {seed_url} — nothing to do. Use --force to re-fetch.[/dim]')
                return True, False
            urls_to_fetch = [seed_url]

        # Filter to starting letter if requested (applies to both BFS and single-URL)
        if start_letter and urls_to_fetch:
            before = len(urls_to_fetch)
            urls_to_fetch = [
                u for u in urls_to_fetch
                if u.split('/')[-1][:1].upper() >= start_letter
            ]
            console.print(f'[dim]--start-letter {start_letter}: {before - len(urls_to_fetch)} URLs skipped, {len(urls_to_fetch)} remaining[/dim]')
        schema_dir = kb_dir / 'schemas'
        if not schema_dir.exists():
            console.print(f'[yellow]No DOT found for topic: {seed_topic} — creating now...[/yellow]')
            kb_dir.mkdir(parents=True, exist_ok=True)
            (kb_dir / 'schemas').mkdir(exist_ok=True)
            (kb_dir / 'entries').mkdir(exist_ok=True)
            (kb_dir / 'raw').mkdir(exist_ok=True)
            manifest = {
                'topic': seed_topic,
                'ingested_at': __import__('datetime').datetime.utcnow().isoformat(),
                'source_folder': 'webseed',
                'documents': [],
                'schemas': [],
                'total_records': 0,
                'overview': f'{seed_topic}\\overview.md',
            }
            (kb_dir / 'manifest.json').write_text(
                _json.dumps(manifest, indent=2), encoding='utf-8')
            (kb_dir / 'overview.md').write_text(
                f'# {seed_topic.title()} Knowledge Base\n\nBuilt from web sources via /webseed.\n',
                encoding='utf-8')
            console.print(f'[green]DOT created: kb/{seed_topic}/[/green]')

        schemas = {}
        for sf in schema_dir.glob('*.json'):
            schemas[sf.stem] = _json.loads(sf.read_text(encoding='utf-8'))

        # If no schemas exist yet, create a generic one from the entity filter or topic name
        if not schemas:
            generic_type = entity_filter or seed_topic.replace(' ', '_').lower()
            schemas[generic_type] = {
                'name': {'type': 'string', 'description': 'Name of the entity'},
                'description': {'type': 'string', 'description': 'Summary description'},
                'key_facts': {'type': 'array', 'description': 'Key facts or data points'},
                'source': {'type': 'string', 'description': 'Source URL or document'},
            }
            schema_path = schema_dir / f'{generic_type}.json'
            schema_path.write_text(_json.dumps(schemas[generic_type], indent=2), encoding='utf-8')
            console.print(f'[dim]Auto-created schema: {generic_type}[/dim]')

        if entity_filter:
            if entity_filter not in schemas:
                console.print(f'[red]Unknown entity type: {entity_filter}[/red]')
                console.print(f'[dim]Available: {", ".join(schemas.keys())}[/dim]')
                return True, False
            schemas = {entity_filter: schemas[entity_filter]}

        console.print(f'[dim]Extracting {len(schemas)} entity type(s) — rolling inject → extract → append[/dim]\n')

        ingestor = Ingestor(engine, kb_root=KB_ROOT)
        total_new   = 0
        pages_hit   = 0

        interrupted = False
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn('[bold cyan]{task.description}[/bold cyan]'),
                BarColumn(bar_width=30),
                TaskProgressColumn(),
                TextColumn('[dim]{task.fields[label]}[/dim]'),
                console=console,
            ) as progress:
                page_task = progress.add_task('WebSeed', total=len(urls_to_fetch), label='')

                for url in urls_to_fetch:
                    page_name = url.split('/')[-1][:35] or url[:35]
                    progress.update(page_task, label=f'fetching {page_name}')

                    result = fetch_url(url)
                    if not result['ok']:
                        progress.console.print(f'  [yellow]Skip {url}: {result["error"]}[/yellow]')
                        progress.update(page_task, advance=1)
                        continue

                    pages_hit += 1
                    page_text = result['text']

                    # Extract each entity type from this page and append to DOT
                    page_new = 0
                    for entity_type, schema_def in schemas.items():
                        progress.update(page_task, label=f'{page_name} → {entity_type}')

                        records = await ingestor._extract_entities(
                            entity_type, schema_def, page_text
                        )
                        if not isinstance(records, list) or not records:
                            continue

                        # Load existing index, append new records, write back immediately
                        entity_dir = kb_dir / 'entries' / entity_type
                        entity_dir.mkdir(parents=True, exist_ok=True)
                        index_path = entity_dir / '_index.json'
                        existing = _json.loads(index_path.read_text(encoding='utf-8')) \
                            if index_path.exists() else []
                        existing_names = {r.get('name') or r.get('id') for r in existing}

                        new_recs = [
                            r for r in records
                            if (r.get('name') or r.get('id')) not in existing_names
                        ]
                        if not new_recs:
                            continue

                        all_recs = existing + new_recs
                        for r in new_recs:
                            rname = r.get('name') or r.get('id') or 'record'
                            safe  = re.sub(r'[^\w\-]', '_', str(rname).lower())[:64]
                            (entity_dir / f'{safe}.json').write_text(
                                _json.dumps(r, indent=2), encoding='utf-8'
                            )
                        index_path.write_text(_json.dumps(all_recs, indent=2), encoding='utf-8')

                        page_new   += len(new_recs)
                        total_new  += len(new_recs)

                    # Persist this URL as visited immediately — survives Ctrl+C
                    already_visited.add(url)
                    crawl_state['visited'] = list(already_visited)
                    crawl_state['last_updated'] = __import__('datetime').datetime.utcnow().isoformat()
                    state_path.write_text(_json.dumps(crawl_state, indent=2), encoding='utf-8')

                    progress.update(page_task, advance=1,
                                    label=f'{page_name} +{page_new} records')

        except KeyboardInterrupt:
            interrupted = True

        # Restore clean engine state
        engine.reset()

        status_line = '[yellow]WebSeed stopped.[/yellow]' if interrupted else '[bold green]WebSeed complete.[/bold green]'
        console.print(f'\n{status_line}')
        console.print(f'  Pages fetched:  {pages_hit}')
        console.print(f'  New records:    {total_new}')
        console.print(f'  DOT:            kb/{seed_topic}')
        if interrupted:
            console.print(f'[dim]Progress saved ({len(already_visited)} URLs visited). Run the same /webseed command to resume.[/dim]\n')
        else:
            console.print(f'[dim]Use /kb {seed_topic} to load the updated DOT.[/dim]\n')
        return True, False

    if name == '/bench':
        models = registry.ollama_models
        if not models:
            console.print('[yellow]No Ollama models detected.[/yellow]')
            return True, False
        if len(parts) > 1:
            target = parts[1]
            if target not in models:
                console.print(f'[red]Unknown model: {target}[/red]')
                console.print(f'[dim]Available: {", ".join(models)}[/dim]')
                return True, False
            now_benched = registry.toggle_bench(target)
            _save_bench(registry)
            if now_benched:
                console.print(f'[yellow]Benched:[/yellow] {target}  [dim](excluded from pipeline)[/dim]')
            else:
                console.print(f'[green]Active:[/green]  {target}  [dim](included in pipeline)[/dim]')
        else:
            console.print('[bold]Ollama model roster:[/bold]')
            for m in models:
                if m in registry.benched:
                    console.print(f'  [dim yellow]BENCH[/dim yellow]  {m}')
                else:
                    console.print(f'  [green]ACTIVE[/green] {m}')
            active_count = len(models) - len([m for m in models if m in registry.benched])
            console.print(f'\n[dim]{active_count} active — use /bench <model> to toggle[/dim]')
        return True, False

    if name == '/ollama':
        models = registry.ollama_models
        if not models:
            console.print('[yellow]Ollama not detected.[/yellow]')
            return True, False
        if len(parts) > 1:
            model = parts[1]
            if model not in models:
                console.print(f'[red]Unknown model: {model}[/red]')
            else:
                if 'ollama' in registry.legs:
                    registry.legs['ollama'].model = model
                    registry.legs['ollama'].provider.default_model = model
                    registry.legs['ollama'].alive = True
                    console.print(f'[green]Ollama leg now using: {model}[/green]')
        else:
            console.print('[bold]Available Ollama models:[/bold]')
            for m in models:
                current = registry.legs.get('ollama')
                tag = ' [cyan](active)[/cyan]' if current and current.model == m else ''
                console.print(f'  {m}{tag}')
            console.print('[dim]Usage: /ollama <model>[/dim]')
        return True, False

    return False, False


async def chat_loop(
    engine: ConversationEngine,
    inventory: KeyInventory,
    registry: LegRegistry,
    mc: ModeController,
    logger: SessionLogger,
    resume_kb: str | None = None,
) -> None:
    deep_next  = False
    active_kb  = resume_kb

    while True:
        try:
            prompt_text = '[bold cyan]/deep [/bold cyan][bold green]>[/bold green]' \
                if deep_next else '[bold green]>[/bold green]'
            user_input = Prompt.ask(prompt_text).strip()
        except KeyboardInterrupt:
            console.print('\n[dim]^C — input cleared.[/dim]')
            continue
        except EOFError:
            console.print('\n[dim]Shutting down.[/dim]')
            break

        if not user_input:
            continue

        if user_input.lower() in ('exit', 'quit', 'q'):
            console.print('[dim]Shutting down. Your keys stay yours.[/dim]')
            break

        if user_input.startswith('/'):
            try:
                handled, set_deep = await handle_command(
                    user_input, inventory, registry, mc, engine)
            except (KeyboardInterrupt, Exception) as e:
                if isinstance(e, KeyboardInterrupt):
                    console.print('\n[yellow]Interrupted — returning to prompt.[/yellow]')
                    engine.reset()
                else:
                    console.print(f'\n[red]Command error:[/red] {e}')
                continue
            if handled:
                if set_deep:
                    deep_next = True
                # Track which KB is active so we can search it on each turn
                if user_input.lower().startswith('/kb '):
                    kb_parts = user_input.split()
                    if len(kb_parts) > 1 and (KB_ROOT / kb_parts[1]).exists():
                        active_kb = kb_parts[1]
                elif user_input.lower() == '/reset':
                    active_kb = None
                    _clear_session()
                continue

        deep_this_turn = deep_next
        deep_next = False

        # If a KB is active, search it and build a grounding system prompt
        # If a KB is active, inject matching records into the user message.
        # Keeps the system prompt as MILNA_SYSTEM so small models aren't overwhelmed.
        turn_input = user_input
        if active_kb:
            kb_context = search_kb(active_kb, user_input, kb_root=KB_ROOT)
            if kb_context:
                turn_input = (
                    f'[KNOWLEDGE BASE: {active_kb.upper()}]\n'
                    f'The following records are verified facts. Use them if relevant to the question below.\n\n'
                    f'{kb_context}\n\n'
                    f'---\n\n'
                    f'[QUESTION]\n{user_input}'
                )

        with console.status(
            '[cyan]Kilonova firing...[/cyan]', spinner='dots', spinner_style='cyan'
        ):
            result = await engine.turn(turn_input, system=None, deep=deep_this_turn)

        _save_session(engine, active_kb)

        logger.log_turn(
            user_message=user_input,   # log original, not KB-augmented turn_input
            response=result.response,
            legs_used=[o['leg'] for o in result.isolate_outputs],
            dead_legs=result.dead_legs,
            deep=result.deep,
            elapsed_ms=result.elapsed_ms,
        )

        if result.dead_legs:
            console.print(
                f'[yellow]Leg(s) dropped this turn: '
                f'{", ".join(result.dead_legs)}[/yellow]'
            )

        mode_tag = '[deep] ' if result.deep else ('[pipeline] ' if result.pipeline else '')
        console.print(Panel(
            result.response,
            title=f'[bold green]MILNA  {mode_tag}[/bold green]'
                  f'[dim]{result.elapsed_ms}ms  '
                  f'~{result.token_estimate} tokens[/dim]',
            border_style='green',
            padding=(1, 2),
        ))
        console.print()


async def main() -> None:
    global KB_ROOT, LOG_DIR

    parser = argparse.ArgumentParser(description='Milna OS — Powered by Kilonova')
    parser.add_argument('--mode',     help='Start in this mode (potato/economy/standard/blade)')
    parser.add_argument('--add-key',  action='store_true', help='Add an API key and exit')
    # Smart default: use script's own directory if it already has a kb/ folder
    # (preserves existing installs). Fresh installs go to ~/.milna.
    _script_dir = Path(__file__).parent
    _default_data = (
        str(_script_dir)
        if (_script_dir / 'kb').exists()
        else os.environ.get('MILNA_DATA_DIR', str(Path.home() / '.milna'))
    )
    parser.add_argument('--data-dir',
        default=_default_data,
        help='Data directory for KB and session logs (default: auto-detect or ~/.milna)')
    args = parser.parse_args()

    data_dir   = Path(args.data_dir)
    KB_ROOT    = data_dir / 'kb'
    LOG_DIR    = data_dir / 'session_logs'
    BENCH_FILE = data_dir / 'session_logs' / 'bench.json'
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print_banner()

    inventory = KeyInventory()

    if args.add_key:
        add_key_flow(inventory)
        return

    # Build legs from stored keys
    registry = LegRegistry(inventory)
    legs     = registry.build()
    _load_bench(registry)

    if not inventory.list_stored():
        console.print('[yellow]No API keys found.[/yellow]')
        console.print('[dim]Ollama on localhost:11434 will be tried as a free local leg.[/dim]')
        setup = Prompt.ask(
            'Add a cloud API key now? [y/n]', default='y').strip().lower()
        if setup == 'y':
            add_key_flow(inventory)
            # Rebuild legs with newly stored key
            registry = LegRegistry(inventory)
            legs = registry.build()
        console.print()

    # Mode
    mc = ModeController(args.mode)
    console.print(f'[dim]Data dir: {data_dir}[/dim]')
    console.print(f'[dim]{mc.summary()}[/dim]')
    if registry.ollama_models:
        chosen = _pick_ollama_model(registry.ollama_models, mc.mode.name)
        if 'ollama' in registry.legs:
            registry.legs['ollama'].model = chosen
            registry.legs['ollama'].provider.default_model = chosen
        console.print(f'[dim]Ollama detected - using: {chosen}  '
                      f'(all: {", ".join(registry.ollama_models)})[/dim]')
        console.print(f'[dim]Use /ollama <model> to switch.[/dim]')
    else:
        console.print('[dim]Ollama not detected on localhost:11434 '
                      '- run: ollama serve[/dim]')
    console.print(f'[dim]Legs: {len(registry.active_legs())} active '
                  f'({", ".join(l.name for l in registry.active_legs())})[/dim]\n')

    # Engine — reconciler uses first available cloud leg, falls back to Ollama
    rec_leg = (
        registry.legs.get('anthropic') or
        registry.legs.get('openai') or
        registry.legs.get('ollama')
    )
    if not rec_leg:
        console.print('[red]No provider available for reconciler. Exiting.[/red]')
        return

    # Build reconciler fallback chain: anthropic → openai → ollama
    rec_fallbacks = []
    for name in ('openai', 'ollama', 'anthropic', 'gemini'):
        leg = registry.legs.get(name)
        if leg and leg is not rec_leg:
            rec_fallbacks.append((leg.provider, leg.model))

    engine = ConversationEngine(
        leg_registry=registry,
        mode_controller=mc,
        reconciler_provider=rec_leg.provider,
        reconciler_model=rec_leg.model,
        reconciler_fallbacks=rec_fallbacks,
    )

    logger = SessionLogger(log_dir=str(LOG_DIR), license_id='trial')

    show_legs(registry)
    console.print()

    # Offer to resume last session if one exists
    _resume_kb = None
    if _session_path().exists():
        resume = Prompt.ask(
            '[dim]Resume last session?[/dim] [y/n]', default='y'
        ).strip().lower()
        if resume == 'y':
            _resume_kb = _load_session(engine)
            turns = len([m for m in engine.history if m['role'] == 'user'])
            console.print(f'[dim]Resumed — {turns} turn(s) restored'
                          + (f', KB: {_resume_kb}' if _resume_kb else '') + '[/dim]')
        else:
            _clear_session()

    console.print('[dim]Type /help for commands. Type /deep before a message for full distillation.[/dim]\n')

    await chat_loop(engine, inventory, registry, mc, logger, resume_kb=_resume_kb)
    logger.log_end()


def main_sync() -> None:
    """Console script entry point — called by `milna` command after pip install."""
    asyncio.run(main())


if __name__ == '__main__':
    main_sync()
