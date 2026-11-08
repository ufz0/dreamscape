#!/usr/bin/env bash
# Start the web app. Creates the venv on first run.
#
# Binds 0.0.0.0 by default, so the app is reachable from the network. Override with HOST=127.0.0.1
# for a machine-only instance. Past a trusted network put it behind an HTTPS reverse proxy: the
# session cookie is only marked Secure when the request arrives over https (COOKIE_SECURE=auto).
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
