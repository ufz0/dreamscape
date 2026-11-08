# dreamscape

A small self-hosted chat app with a markdown wiki as its memory and a nightly dreaming job:
the model reads idle transcripts, updates the wiki, and commits. FastAPI + httpx and plain
files — no framework, no build step, no database, nothing from a CDN.

![screenshot](docs/screenshot.png)

## Features

- Chat with a wiki tool loop (`wiki_list`, `wiki_read`, `wiki_search`, `wiki_write`), thinking
  shown separately from the answer, and a four-step reasoning-effort control.
- A wiki viewer with List, Graph and Dream log tabs. The graph is a canvas force layout with no
  library; the dream log shows each run, its steps per transcript, the diffs, and live progress.
- A nightly dreaming job, runnable from cron or from the "Dream now" button, with structured
  records per run and a human-readable summary in `data/wiki/dream-log.md`.

## Setup

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` (the API key is only needed if your upstream asks for one):

```
STRATA_BASE_URL=http://10.0.0.57:8080/v1
STRATA_API_KEY=
STRATA_MODEL=qwen3.8-flash-next
HOST=127.0.0.1
PORT=8000
DATA_DIR=./data
IDLE_MINUTES=60
MAX_TOOL_ITERATIONS=8
INDEX_MAX_LINES=150
PAGE_MAX_LINES=120
```

## Run

```sh
./run.sh                    # serves http://127.0.0.1:8000
```

Open `/` for chat and `/wiki` for the wiki viewer.

```sh
python dream.py             # processes idle transcripts
python dream.py --dry-run   # shows what would be processed, writes nothing
python dream.py --now       # ignores the idle rule
```

Crontab line for the 03:00 dream:

```cron
0 3 * * * cd /Users/lukas/Projects/dreamscape && .venv/bin/python dream.py >> data/dream.cron.log 2>&1
```

## Layout

```
app.py         FastAPI: chat (SSE), wiki API, dream records (/api/dreams), reasoning mapping
agent.py       wiki tools + tool loop
llm.py         httpx upstream, normalised streaming events
dream.py       the dreaming job
dreamlog.py    run records, readers, live progress
graph.py       graph data for the wiki view
reasoning.py   effort levels -> request fields per model
wiki.py store.py config.py
prompts/       system.md, dream_per_transcript.md, dream_cleanup.md
static/        index.html, wiki.html, styles.css, js/ modules, fonts/, logo and favicon files
scripts/       make_logo.py, make_icons.py — regenerate the logo and the icon files
design/        the logo candidates that were not picked
data/          transcripts, conversations, reasoning.json, wiki/ (its own git repo),
               dream-runs/ (one JSON record per run) — not committed
```
