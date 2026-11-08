#!/usr/bin/env bash
# Start the web app. Creates the venv on first run.
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
