"""
kilonova_mcp.py — Kilonova MCP Server

Exposes Kilonova KB tools as native Claude Code tools via the
Model Context Protocol. Install once, use in any Claude session.

Tools:
  kb_search(topic, query)              — search entity records
  kb_load(topic, entity_type, name)    — load a specific record
  kb_topics()                          — list available KB topics
  corpus_status(topic)                 — scrape progress stats

Setup (Claude Code):
  Add to ~/.claude/settings.json:
  {
    "mcpServers": {
      "kilonova": {
        "command": "python",
        "args": ["D:/Runtime/kilonova_mcp.py"],
        "env": {"KILONOVA_KB_ROOT": "D:/Runtime/kb"}
      }
    }
  }
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Allow running from any directory
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from kilonova.tools.kb_search import search_kb

# ── Private tool globals ──────────────────────────────────────────────────────

_SHARD_DIR = Path('D:/shard-accelerator')
_JOBS_DIR  = _HERE / 'session_logs' / 'jobs'

# In-memory job registry — keyed by job_id, holds proc + metadata.
# Lost on server restart, but logs persist on disk.
_JOBS: dict[str, dict] = {}

# ── GCP VM constants ──────────────────────────────────────────────────────────

_VM_INSTANCE  = 'instance-20260709-161148'
_VM_ZONE      = 'us-west1-a'
_VM_USER      = 'jeremy'
_VM_VENV      = '/home/jeremy/venv'
_VM_SHARD_DIR = '/home/jeremy/shard-accelerator/shard-accelerator'
_VM_JOBS_DIR  = '/home/jeremy/jobs'


def _vm_ssh(command: str, timeout: int = 60) -> tuple[str, int]:
    """Run a command on the GCP VM via IAP tunnel. Returns (output, returncode)."""
    result = subprocess.run(
        [
            'gcloud', 'compute', 'ssh',
            f'{_VM_USER}@{_VM_INSTANCE}',
            f'--zone={_VM_ZONE}',
            '--tunnel-through-iap',
            '--quiet',
            f'--command={command}',
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    return (result.stdout + result.stderr).strip(), result.returncode

# ── Config ────────────────────────────────────────────────────────────────────

KB_ROOT = Path(os.environ.get('KILONOVA_KB_ROOT', _HERE / 'kb'))

# ── Server ────────────────────────────────────────────────────────────────────

server = Server('kilonova')


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name='kb_search',
            description=(
                'Search the Kilonova knowledge base for entities matching a query. '
                'Returns formatted entity records (mechs, weapons, factions, etc.). '
                'Use this whenever the user asks about game system content.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {
                        'type': 'string',
                        'description': 'KB topic to search (e.g. "battletech", "starfinder")',
                    },
                    'query': {
                        'type': 'string',
                        'description': 'Natural language search query (e.g. "Atlas mech stats", "PPC weapon")',
                    },
                    'entity_types': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional: filter to specific entity types (e.g. ["battlemech", "weapon"])',
                    },
                },
                'required': ['topic', 'query'],
            },
        ),
        Tool(
            name='kb_load',
            description=(
                'Load a specific entity record from the Kilonova KB by name. '
                'Use when you need the full record for a named entity.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {'type': 'string', 'description': 'KB topic'},
                    'entity_type': {'type': 'string', 'description': 'Entity type folder name'},
                    'name': {'type': 'string', 'description': 'Entity name (filename without .json)'},
                },
                'required': ['topic', 'entity_type', 'name'],
            },
        ),
        Tool(
            name='kb_topics',
            description='List all available KB topics and their entity type counts.',
            inputSchema={'type': 'object', 'properties': {}, 'required': []},
        ),
        Tool(
            name='corpus_status',
            description='Show scrape progress for a topic — chunk count, file size, last modified.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {'type': 'string', 'description': 'KB topic'},
                },
                'required': ['topic'],
            },
        ),
        Tool(
            name='kb_write',
            description=(
                'Write or update an entity record in the Kilonova KB. '
                'Use this to persist new data or corrections discovered during a conversation. '
                'Also updates the topic _index.json automatically.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {'type': 'string', 'description': 'KB topic (e.g. "battletech")'},
                    'entity_type': {'type': 'string', 'description': 'Entity type folder (e.g. "battlemech", "weapon")'},
                    'name': {'type': 'string', 'description': 'Entity name — used as filename (spaces become underscores)'},
                    'data': {'type': 'object', 'description': 'Entity record as a JSON object matching the schema fields'},
                },
                'required': ['topic', 'entity_type', 'name', 'data'],
            },
        ),
        Tool(
            name='dot_load',
            description=(
                'Load the full DOT (Document of Truth) for a KB topic. '
                'Returns the overview + all card sections as a single reference document.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {'type': 'string', 'description': 'KB topic (e.g. "battletech", "linguistics")'},
                },
                'required': ['topic'],
            },
        ),
        Tool(
            name='kb_backup',
            description=(
                'Mirror the Kilonova KB and core Runtime files to OneDrive. '
                'Use kb_only=true for a faster KB-only backup. '
                'Use dry=true to preview what would be copied without copying.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'kb_only': {'type': 'boolean', 'description': 'Back up KB folder only (skip core scripts)'},
                    'dry':     {'type': 'boolean', 'description': 'Preview only — do not copy anything'},
                },
                'required': [],
            },
        ),
        Tool(
            name='kb_schema',
            description=(
                'Show the field schema for an entity type in a KB topic. '
                'Call with no entity_type to list all schema types available for a topic.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {'type': 'string', 'description': 'KB topic'},
                    'entity_type': {'type': 'string', 'description': 'Optional: specific entity type to inspect'},
                },
                'required': ['topic'],
            },
        ),

        # ── Private VM tools ──────────────────────────────────────────────────
        Tool(
            name='milna_legs',
            description=(
                'List the active Milna legs — which API providers have keys stored '
                'and which Ollama models are running locally.'
            ),
            inputSchema={'type': 'object', 'properties': {}, 'required': []},
        ),
        Tool(
            name='milna_webseed',
            description=(
                'Launch a Milna webseed run in the background. '
                'Seeds the KB for a topic from one or more Wikipedia / web URLs. '
                'Returns a job_id you can pass to job_status to watch progress.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'topic': {
                        'type': 'string',
                        'description': 'KB topic to seed (e.g. "linguistics")',
                    },
                    'seed_urls': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'One or more seed URLs to crawl',
                    },
                    'depth': {
                        'type': 'integer',
                        'description': 'BFS crawl depth (default 3)',
                    },
                    'resume': {
                        'type': 'boolean',
                        'description': 'Resume from last crawl state (default false)',
                    },
                },
                'required': ['topic', 'seed_urls'],
            },
        ),
        Tool(
            name='shard_run',
            description=(
                'Launch a Shard Accelerator distillation run in the background. '
                'Returns a job_id you can pass to job_status to watch progress. '
                'Available configs: baseline_ollama, dev_ollama, anthropic_leg, cloud_ollama, '
                'cloud_4leg, hybrid_2rung, starfinder_lore, universal_translator, universal_translator_lite.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'config': {
                        'type': 'string',
                        'description': 'Config yaml filename (without .yaml, e.g. "anthropic_leg")',
                    },
                    'prompt': {
                        'type': 'string',
                        'description': 'Distillation prompt / topic to research',
                    },
                    'max_runs': {
                        'type': 'integer',
                        'description': 'Max pipeline runs (default 6)',
                    },
                    'threshold': {
                        'type': 'number',
                        'description': 'Convergence threshold 0–1 (default 0.70 = artisan depth)',
                    },
                },
                'required': ['config', 'prompt'],
            },
        ),
        Tool(
            name='job_status',
            description=(
                'Check the status of a background job (webseed or shard run). '
                'Returns running/done status, exit code, and the last N lines of the log.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'job_id': {
                        'type': 'string',
                        'description': 'Job ID returned by milna_webseed or shard_run',
                    },
                    'tail_lines': {
                        'type': 'integer',
                        'description': 'How many log lines to return (default 40)',
                    },
                },
                'required': ['job_id'],
            },
        ),
        Tool(
            name='job_list',
            description='List all background jobs started in this MCP server session.',
            inputSchema={'type': 'object', 'properties': {}, 'required': []},
        ),

        # ── GCP VM tools ──────────────────────────────────────────────────────
        Tool(
            name='vm_shard_run',
            description=(
                'Launch a Shard Accelerator distillation run on the GCP VM in the background. '
                'The VM has more RAM and runs the job without tying up your local machine. '
                'Returns a job_id — use vm_job_status to follow progress. '
                'Available configs: baseline_ollama, dev_ollama, anthropic_leg, cloud_ollama, '
                'cloud_4leg, hybrid_2rung, starfinder_lore, universal_translator, universal_translator_lite.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'config': {
                        'type': 'string',
                        'description': 'Config yaml name (without .yaml, e.g. "anthropic_leg")',
                    },
                    'prompt': {
                        'type': 'string',
                        'description': 'Distillation prompt / topic to research',
                    },
                    'max_runs': {
                        'type': 'integer',
                        'description': 'Max pipeline passes (default 6)',
                    },
                    'threshold': {
                        'type': 'number',
                        'description': 'Convergence threshold 0–1 (default 0.70)',
                    },
                },
                'required': ['config', 'prompt'],
            },
        ),
        Tool(
            name='vm_job_status',
            description=(
                'Check the status of a job running on the GCP VM. '
                'Returns running/done, the exit code if finished, and the last N lines of the log.'
            ),
            inputSchema={
                'type': 'object',
                'properties': {
                    'job_id': {
                        'type': 'string',
                        'description': 'Job ID returned by vm_shard_run',
                    },
                    'tail_lines': {
                        'type': 'integer',
                        'description': 'Log lines to return (default 40)',
                    },
                },
                'required': ['job_id'],
            },
        ),
        Tool(
            name='vm_job_list',
            description='List shard accelerator jobs currently running on the GCP VM.',
            inputSchema={'type': 'object', 'properties': {}, 'required': []},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == 'kb_search':
        result = search_kb(
            topic=arguments['topic'],
            query=arguments['query'],
            entity_types=arguments.get('entity_types'),
            kb_root=KB_ROOT,
        )
        text = result if result else f'No records found for "{arguments["query"]}" in {arguments["topic"]} KB.'
        return [TextContent(type='text', text=text)]

    if name == 'kb_load':
        topic       = arguments['topic']
        entity_type = arguments['entity_type']
        name_arg    = arguments['name']
        # Try exact filename first, then fuzzy
        safe = name_arg.lower().replace(' ', '_')
        candidates = [
            KB_ROOT / topic / 'entries' / entity_type / f'{safe}.json',
            KB_ROOT / topic / 'entries' / entity_type / f'{name_arg}.json',
        ]
        for path in candidates:
            if path.exists():
                record = json.loads(path.read_text(encoding='utf-8'))
                return [TextContent(type='text', text=json.dumps(record, indent=2))]
        # Fuzzy fallback — find closest filename
        entity_dir = KB_ROOT / topic / 'entries' / entity_type
        if entity_dir.exists():
            matches = [f for f in entity_dir.glob('*.json')
                       if safe[:5] in f.stem or f.stem[:5] in safe]
            if matches:
                record = json.loads(matches[0].read_text(encoding='utf-8'))
                return [TextContent(type='text', text=json.dumps(record, indent=2))]
        return [TextContent(type='text', text=f'Record not found: {topic}/{entity_type}/{name_arg}')]

    if name == 'kb_topics':
        entries_roots = [d for d in KB_ROOT.iterdir() if d.is_dir()] if KB_ROOT.exists() else []
        lines = ['Available KB topics:\n']
        for topic_dir in sorted(entries_roots):
            topic = topic_dir.name
            entries_dir = topic_dir / 'entries'
            corpus_file = topic_dir / 'corpus.txt'
            entity_types = [d.name for d in entries_dir.iterdir() if d.is_dir()] if entries_dir.exists() else []
            corpus_kb = f'{corpus_file.stat().st_size // 1024}KB corpus' if corpus_file.exists() else 'no corpus'
            lines.append(f'  {topic}: {len(entity_types)} entity types ({corpus_kb})')
            for et in sorted(entity_types):
                index = entries_dir / et / '_index.json'
                if index.exists():
                    count = len(json.loads(index.read_text(encoding='utf-8')))
                    lines.append(f'    - {et}: {count} records')
        return [TextContent(type='text', text='\n'.join(lines))]

    if name == 'corpus_status':
        topic = arguments['topic']
        corpus  = KB_ROOT / topic / 'corpus.txt'
        visited = KB_ROOT / topic / 'corpus.visited'
        lines = [f'Corpus status: {topic}\n']
        if corpus.exists():
            import re as _re
            text = corpus.read_text(encoding='utf-8', errors='ignore')
            chunks = len(_re.findall(r'^\[\d{4}\]', text, _re.MULTILINE))
            size_kb = corpus.stat().st_size // 1024
            modified = corpus.stat().st_mtime
            from datetime import datetime
            mod_str = datetime.fromtimestamp(modified).strftime('%Y-%m-%d %H:%M')
            lines.append(f'  Chunks   : {chunks}')
            lines.append(f'  Size     : {size_kb} KB')
            lines.append(f'  Modified : {mod_str}')
        else:
            lines.append('  No corpus found. Run the scraper first.')
        if visited.exists():
            url_count = len(visited.read_text(encoding='utf-8').splitlines())
            lines.append(f'  Visited  : {url_count} URLs (sidecar)')
        return [TextContent(type='text', text='\n'.join(lines))]

    if name == 'kb_backup':
        import importlib.util, sys as _sys
        _spec = importlib.util.spec_from_file_location('backup', Path(__file__).parent / 'backup.py')
        _mod  = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        result = _mod.run(
            kb_only=arguments.get('kb_only', False),
            dry=arguments.get('dry', False),
        )
        if result['ok']:
            label = 'Dry run — would copy' if result['dry'] else 'Backed up'
            return [TextContent(type='text', text=f'{label}: {result["files"]} files, {result["kb"]} KB → OneDrive')]
        return [TextContent(type='text', text=f'Backup failed: {result["error"]}')]

    if name == 'kb_write':
        topic       = arguments['topic']
        entity_type = arguments['entity_type']
        raw_name    = arguments['name']
        data        = arguments['data']
        safe        = raw_name.lower().replace(' ', '_')
        entity_dir  = KB_ROOT / topic / 'entries' / entity_type
        entity_dir.mkdir(parents=True, exist_ok=True)
        record_path = entity_dir / f'{safe}.json'
        # Merge with existing record if present (don't overwrite good fields with null)
        if record_path.exists():
            try:
                existing = json.loads(record_path.read_text(encoding='utf-8'))
                for k, v in data.items():
                    if v is not None and v != '' and v != []:
                        existing[k] = v
                data = existing
            except Exception:
                pass
        record_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        # Update _index.json
        index_path = entity_dir / '_index.json'
        try:
            index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.exists() else []
            name_key = data.get('name') or data.get('id') or raw_name
            for i, entry in enumerate(index):
                if (entry.get('name') or entry.get('id')) == name_key:
                    index[i] = data
                    break
            else:
                index.append(data)
            index_path.write_text(json.dumps(index, indent=2), encoding='utf-8')
        except Exception:
            pass
        return [TextContent(type='text', text=f'Written: {topic}/{entity_type}/{safe}.json ({len(json.dumps(data))} bytes)')]

    if name == 'dot_load':
        topic = arguments['topic']
        topic_dir = KB_ROOT / topic
        # Try <topic>_dot.txt first, then overview.md
        candidates = [
            topic_dir / f'{topic}_dot.txt',
            topic_dir / 'overview.md',
            topic_dir / f'{topic}.dot',
        ]
        for path in candidates:
            if path.exists():
                text = path.read_text(encoding='utf-8', errors='ignore')
                return [TextContent(type='text', text=text)]
        return [TextContent(type='text', text=f'No DOT found for topic "{topic}". Expected: {topic_dir}/{topic}_dot.txt')]

    if name == 'kb_schema':
        topic       = arguments['topic']
        entity_type = arguments.get('entity_type')
        schemas_dir = KB_ROOT / topic / 'schemas'
        if not schemas_dir.exists():
            return [TextContent(type='text', text=f'No schemas found for topic "{topic}".')]
        if entity_type:
            schema_path = schemas_dir / f'{entity_type}.json'
            if not schema_path.exists():
                available = [f.stem for f in schemas_dir.glob('*.json')]
                return [TextContent(type='text', text=f'Schema "{entity_type}" not found. Available: {", ".join(sorted(available))}')]
            schema = json.loads(schema_path.read_text(encoding='utf-8'))
            lines = [f'Schema: {topic}/{entity_type}\n']
            for field, defn in schema.items():
                ftype = defn.get('type', 'string') if isinstance(defn, dict) else str(defn)
                desc  = defn.get('description', '') if isinstance(defn, dict) else ''
                lines.append(f'  {field} ({ftype}): {desc}')
            return [TextContent(type='text', text='\n'.join(lines))]
        else:
            lines = [f'Schemas available for topic "{topic}":\n']
            for sf in sorted(schemas_dir.glob('*.json')):
                try:
                    schema = json.loads(sf.read_text(encoding='utf-8'))
                    fields = list(schema.keys())
                    lines.append(f'  {sf.stem}: {", ".join(fields[:6])}{"..." if len(fields) > 6 else ""}')
                except Exception:
                    lines.append(f'  {sf.stem}: (unreadable)')
            return [TextContent(type='text', text='\n'.join(lines))]

    # ── Private VM tools ──────────────────────────────────────────────────────

    if name == 'milna_legs':
        from kilonova.core.legs import KeyInventory, PROVIDER_DEFAULTS, probe_ollama
        inv   = KeyInventory()
        lines = ['Milna leg configuration:\n']
        for provider, default_model in PROVIDER_DEFAULTS.items():
            if provider == 'ollama':
                continue
            key    = inv.get_key(provider)
            status = 'key stored' if key else 'no key'
            lines.append(f'  {provider:<12}  {status:<12}  default model: {default_model}')
        ollama_models = probe_ollama()
        if ollama_models:
            lines.append(f'\n  ollama running — {len(ollama_models)} model(s): {", ".join(ollama_models[:5])}')
        else:
            lines.append('\n  ollama: not running')
        return [TextContent(type='text', text='\n'.join(lines))]

    if name == 'milna_webseed':
        topic     = arguments['topic']
        seed_urls = arguments['seed_urls']
        depth     = int(arguments.get('depth', 3))
        resume    = bool(arguments.get('resume', False))

        job_id   = f'webseed_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        _JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _JOBS_DIR / f'{job_id}.log'

        cmd = [sys.executable, str(_HERE / 'milna.py'), 'webseed', topic] + seed_urls + ['--depth', str(depth)]
        if resume:
            cmd.append('--resume')

        log_file = open(log_path, 'w', encoding='utf-8')
        flags    = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        proc     = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                    cwd=str(_HERE), creationflags=flags)
        _JOBS[job_id] = {
            'type': 'webseed', 'cmd': cmd, 'log_path': str(log_path),
            'started': datetime.now().isoformat(), 'pid': proc.pid, 'proc': proc,
        }
        return [TextContent(type='text', text=(
            f'Job started: {job_id}\n'
            f'PID:  {proc.pid}\n'
            f'Log:  {log_path}\n'
            f'Cmd:  {" ".join(cmd)}'
        ))]

    if name == 'shard_run':
        config_name = arguments['config']
        if not config_name.endswith('.yaml'):
            config_name += '.yaml'
        prompt    = arguments['prompt']
        max_runs  = int(arguments.get('max_runs', 6))
        threshold = float(arguments.get('threshold', 0.70))

        config_path = _SHARD_DIR / 'config' / config_name
        if not config_path.exists():
            available = sorted(p.stem for p in (_SHARD_DIR / 'config').glob('*.yaml'))
            return [TextContent(type='text', text=(
                f'Config not found: {config_name}\n'
                f'Available: {", ".join(available)}'
            ))]

        job_id   = f'shard_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        _JOBS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _JOBS_DIR / f'{job_id}.log'

        cmd = [
            sys.executable, str(_SHARD_DIR / 'headless.py'),
            '--config',    config_name,
            '--prompt',    prompt,
            '--max-runs',  str(max_runs),
            '--threshold', str(threshold),
        ]

        log_file = open(log_path, 'w', encoding='utf-8')
        flags    = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        proc     = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                    cwd=str(_SHARD_DIR), creationflags=flags)
        _JOBS[job_id] = {
            'type': 'shard', 'cmd': cmd, 'log_path': str(log_path),
            'started': datetime.now().isoformat(), 'pid': proc.pid, 'proc': proc,
        }
        return [TextContent(type='text', text=(
            f'Job started: {job_id}\n'
            f'PID:  {proc.pid}\n'
            f'Log:  {log_path}\n'
            f'Config: {config_name}  max_runs={max_runs}  threshold={threshold}'
        ))]

    if name == 'job_status':
        job_id     = arguments['job_id']
        tail_lines = int(arguments.get('tail_lines', 40))
        job        = _JOBS.get(job_id)

        if job:
            log_path = Path(job['log_path'])
        else:
            log_path = _JOBS_DIR / f'{job_id}.log'
            if not log_path.exists():
                return [TextContent(type='text', text=f'Job not found: {job_id}')]

        lines = [f'Job: {job_id}']
        if job:
            rc = job['proc'].poll()
            if rc is None:
                lines.append(f'Status:  running  (PID {job["pid"]})')
            else:
                lines.append(f'Status:  done  (exit {rc})')
            lines.append(f'Type:    {job["type"]}')
            lines.append(f'Started: {job["started"]}')
            lines.append(f'Log:     {job["log_path"]}')
        else:
            lines.append('Status:  unknown (server restarted — check log)')

        if log_path.exists():
            all_lines = log_path.read_text(encoding='utf-8', errors='ignore').splitlines()
            tail      = all_lines[-tail_lines:] if len(all_lines) > tail_lines else all_lines
            lines.append(f'\n--- last {len(tail)} of {len(all_lines)} lines ---')
            lines.extend(tail)
        else:
            lines.append('\n(log not yet created)')

        return [TextContent(type='text', text='\n'.join(lines))]

    if name == 'job_list':
        if not _JOBS:
            return [TextContent(type='text', text='No jobs started in this session.')]
        lines = [f'{"Job ID":<36}  {"Type":<10}  {"Status":<16}  Started']
        lines.append('-' * 80)
        for jid, job in sorted(_JOBS.items()):
            rc     = job['proc'].poll()
            status = 'running' if rc is None else f'exit {rc}'
            lines.append(f'{jid:<36}  {job["type"]:<10}  {status:<16}  {job["started"][:19]}')
        return [TextContent(type='text', text='\n'.join(lines))]

    # ── GCP VM tools ──────────────────────────────────────────────────────────

    if name == 'vm_shard_run':
        import shlex
        config_name = arguments['config']
        if not config_name.endswith('.yaml'):
            config_name += '.yaml'
        prompt    = arguments['prompt']
        max_runs  = int(arguments.get('max_runs', 6))
        threshold = float(arguments.get('threshold', 0.70))

        job_id   = f'vm_shard_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        log_path = f'{_VM_JOBS_DIR}/{job_id}.log'
        pid_path = f'{_VM_JOBS_DIR}/{job_id}.pid'

        # Pull API keys from local keyring and inject as env vars on the VM.
        # Keys never need to be stored on the VM — they travel in the SSH session.
        from kilonova.core.legs import KeyInventory
        inv      = KeyInventory()
        env_vars = ''
        for provider, env_name in [
            ('groq',       'GROQ_API_KEY'),
            ('anthropic',  'ANTHROPIC_API_KEY'),
            ('openai',     'OPENAI_API_KEY'),
            ('gemini',     'GEMINI_API_KEY'),
            ('cerebras',   'CEREBRAS_API_KEY'),
            ('openrouter', 'OPENROUTER_API_KEY'),
            ('together',   'TOGETHER_API_KEY'),
            ('sambanova',  'SAMBANOVA_API_KEY'),
        ]:
            key = inv.get_key(provider)
            if key:
                env_vars += f'{env_name}={shlex.quote(key)} '

        q_prompt = shlex.quote(prompt)
        cmd = (
            f'mkdir -p {_VM_JOBS_DIR} && '
            f'cd {_VM_SHARD_DIR} && '
            f'nohup env {env_vars}'
            f'{_VM_VENV}/bin/python headless.py '
            f'--config {config_name} --prompt {q_prompt} '
            f'--max-runs {max_runs} --threshold {threshold} '
            f'> {log_path} 2>&1 & PID=$! && echo $PID > {pid_path} && echo $PID'
        )
        output, rc = _vm_ssh(cmd, timeout=30)
        if rc != 0:
            return [TextContent(type='text', text=f'Failed to start VM job:\n{output}')]
        pid = output.strip().splitlines()[-1]
        providers_loaded = [p for p in ['groq', 'anthropic', 'openai', 'gemini'] if inv.get_key(p)]
        return [TextContent(type='text', text=(
            f'VM job started: {job_id}\n'
            f'PID:      {pid}\n'
            f'Log:      {log_path}\n'
            f'Config:   {config_name}  max_runs={max_runs}  threshold={threshold}\n'
            f'Keys:     {", ".join(providers_loaded)}\n\n'
            f'Use vm_job_status with job_id="{job_id}" to follow progress.'
        ))]

    if name == 'vm_job_status':
        job_id     = arguments['job_id']
        tail_lines = int(arguments.get('tail_lines', 40))
        pid_path   = f'{_VM_JOBS_DIR}/{job_id}.pid'
        log_path   = f'{_VM_JOBS_DIR}/{job_id}.log'

        cmd = (
            f'PID=$(cat {pid_path} 2>/dev/null); '
            f'if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "running PID=$PID"; '
            f'else echo "done"; fi; '
            f'echo "---"; '
            f'tail -n {tail_lines} {log_path} 2>/dev/null || echo "(no log yet)"'
        )
        output, rc = _vm_ssh(cmd, timeout=20)
        lines = [f'VM Job: {job_id}', '']
        lines.append(output)
        return [TextContent(type='text', text='\n'.join(lines))]

    if name == 'vm_job_list':
        cmd = f"ps aux | grep -E 'headless\\.py|milna\\.py' | grep -v grep || echo 'no jobs running'"
        output, _ = _vm_ssh(cmd, timeout=20)
        return [TextContent(type='text', text=f'VM running jobs:\n{output}')]

    return [TextContent(type='text', text=f'Unknown tool: {name}')]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
