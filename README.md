# Milna OS
### Powered by Kilonova

A multi-model AI terminal assistant that runs your keys, your compute, your output — no subscriptions, no data leaving your machine without your knowledge.

Milna fires your prompt across all active AI legs in parallel, reconciles their outputs into a single synthesized response, and lets you build persistent knowledge bases (DOTs) from your own documents.

---

## What it does

- **Multi-leg parallel inference** — Anthropic, OpenAI, Gemini, and local Ollama run simultaneously. Responses are reconciled, not averaged.
- **DOT (Document of Truth)** — Ingest a folder of PDFs into a persistent, structured knowledge base. Load it as a context anchor for any conversation.
- **BYOK** — Bring your own API keys. They live in your OS credential store, never on disk in plaintext.
- **Free local fallback** — Ollama on localhost runs for free when cloud legs are unavailable or too expensive.
- **Session logging** — Every turn is written to disk as markdown. Full audit trail.
- **No subscriptions. No compute surcharge.** Remember the days when you bought something and you just... owned it? This is a tool. I have a hammer and I don't pay by the nail. Your output is yours — never gated, never watermarked.
---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) (optional but recommended — free local inference)
- At least one API key: Anthropic, OpenAI, or Gemini

---

## Install

```bash
pip install milna-os
```

Python 3.11+ required. On Windows, keys are stored in Windows Credential Manager. On Mac/Linux, the OS keychain is used — never on disk in plaintext.

---

## Run

```bash
milna
```

First run: Milna will prompt you to add an API key.

To add or update a key at any time:

```bash
milna --add-key
```

---

## Source

```bash
git clone https://github.com/MilnaOS/milna-os.git
cd milna-os
pip install -e .
```

---

## Commands

| Command | Description |
|---------|-------------|
| `/load <path>` | Load a PDF, .md, or .txt file into conversation context |
| `/load <folder>` | Pick from files in a folder |
| `/fetch <url>` | Fetch a web page into conversation context |
| `/webseed <topic> <url>` | Extract entities from a web page into a DOT |
| `/webseed <topic> <url> --follow` | Crawl linked pages (depth 1, up to 20 by default) |
| `/webseed <topic> <url> --depth <n>` | BFS crawl N levels deep |
| `/webseed <topic> <url> --force` | Re-fetch all URLs, ignoring saved crawl state |
| `/ingest <folder>` | Build a persistent knowledge base (DOT) from PDFs |
| `/ingest <folder> --deep` | Deep distillation — richer extraction, higher cost |
| `/ingest <folder> --topic <name>` | Custom topic name for the DOT |
| `/ingest --rebuild <topic>` | Re-extract entity records from existing distilled docs |
| `/ingest --rebuild <topic> --force` | Wipe and re-extract all records |
| `/ingest --rebuild <topic> --force --deep` | Wipe, re-extract with deep distillation |
| `/kb` | List available knowledge bases |
| `/kb <topic>` | Load a DOT as conversation context anchor |
| `/deep` | Enable deep distillation for the next message |
| `/mode` | Show current processing mode |
| `/mode <name>` | Switch mode: `potato` / `economy` / `standard` / `blade` |
| `/legs` | Show active legs and their status |
| `/keys` | Add or update API keys |
| `/ollama` | List available Ollama models |
| `/ollama <model>` | Switch active Ollama model |
| `/reset` | Clear conversation history |
| `/help` | Show command list |
| `exit` / `quit` | Shutdown |

---

## Modes

| Mode | Isolates per leg | Use when |
|------|-----------------|----------|
| `potato` | 1 | Free local-only, Ollama routes everything |
| `economy` | 1 | Light cloud usage, cost-conscious |
| `standard` | 2 | Default — balanced quality and cost |
| `blade` | 3 | Maximum quality, highest cost |

---

## Building a DOT

A DOT (Document of Truth) is a structured knowledge base built from source documents. Once built, it loads instantly as a permanent context anchor — models treat it as verified reference material, not something to hallucinate around.

```bash
# Ingest a folder of PDFs
/ingest D:/my-documents/research --topic my_research

# Load the DOT into conversation
/kb my_research

# Ask questions grounded in your documents
> summarize the key findings from the research
```

DOTs are stored in `kb/<topic>/` and persist between sessions. Incremental ingest adds new documents without re-processing existing ones.

---

## Project structure

```
milna.py                    — CLI entry point
kilonova/
  core/
    engine.py               — ConversationEngine: parallel firing + reconciler
    legs.py                 — Leg registry, key inventory, provider wiring
    reconciler.py           — Multi-role synthesis
    mode.py                 — Mode controller (potato/economy/standard/blade)
    session_log.py          — Turn logging to disk
    providers/
      anthropic_provider.py
      openai_provider.py
      gemini_provider.py
      ollama_provider.py
  tools/
    loader.py               — PDF / .md / .txt extraction
    ingest.py               — DOT build pipeline
kb/                         — Built knowledge bases (gitignored)
session_logs/               — Turn transcripts (gitignored)
prompts/                    — Loadable system prompt files
```

---

## Local-only operation

Run fully offline with Ollama:

```bash
ollama serve
ollama pull qwen2.5:3b
python milna.py --mode potato
```

No API keys required. No data leaves your machine.

---

## License

Proprietary — source available for inspection. See LICENSE file for full terms.
Licensing inquiries: remy.black.7.77@gmail.com

---

*BYOK — your keys, your compute, your output.*
