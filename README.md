# dreamscape

A small self-hosted chat app with a markdown wiki as its memory and a nightly dreaming job:
the model reads idle transcripts, updates the wiki, and commits. FastAPI + httpx, SQLite for
accounts and usage, plain files for everything else — no framework, no build step, nothing from a CDN.

![screenshot](docs/screenshot.png)

## Features

- Chat with a wiki tool loop (`wiki_list`, `wiki_read`, `wiki_search`, `wiki_write`), thinking
  shown separately from the answer, and a four-step reasoning-effort control.
- A wiki viewer with List, Graph and Dream log tabs. The graph is a canvas force layout with no
  library; the dream log shows each run, its steps per transcript, the diffs, and live progress.
- A nightly dreaming job, runnable from cron or from the "Dream now" button, with structured
  records per run and a human-readable summary in each user's `dream-log.md`.
- Accounts with an admin role, session cookies, CSRF, login rate limiting, per-user data folders,
  per-user token usage statistics and optional daily/weekly token limits. An admin panel at
  `/admin` shows the users and the usage numbers, with a chart and CSV export.

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
TIMEZONE=Europe/Vienna
IDLE_MINUTES=60
MAX_TOOL_ITERATIONS=8
INDEX_MAX_LINES=150
PAGE_MAX_LINES=120
```

`TIMEZONE` only decides where a day, week, month or year begins in the usage views. Token counts
are stored in UTC and aggregated in that zone; weeks start on Monday.

## Run

```sh
./run.sh                    # serves http://127.0.0.1:8000
```

Open `/` for chat and `/wiki` for the wiki viewer.

```sh
python dream.py             # processes idle transcripts for every active user
python dream.py --user bob  # one user only
python dream.py --dry-run   # shows what would be processed, writes nothing
python dream.py --now       # ignores the idle rule
```

Crontab line for the 03:00 dream:

```cron
0 3 * * * cd /Users/lukas/Projects/dreamscape && .venv/bin/python dream.py >> data/dream.cron.log 2>&1
```

The job is one process with one lockfile (`data/.dream.lock`), so cron and the "Dream now" button
never overlap. "Dream now" in the UI dreams only for the signed-in user; the cron job loops over
all active users.

## Accounts

The default bind is `127.0.0.1`, so on a home network you can open it over plain HTTP. If you
serve it on the network, put it behind HTTPS: the session cookie is `HttpOnly` and `SameSite=Lax`,
and it is marked `Secure` when the request arrives over https (read from `X-Forwarded-Proto`).
The CSRF cookie is deliberately not `HttpOnly`, because the page has to read it and send it back
as `X-CSRF-Token`; every state-changing request is checked for that.

- Passwords are hashed with argon2, minimum 10 characters. Usernames are case-insensitive unique,
  3–32 characters, `[a-z0-9._-]`.
- Sessions are random tokens stored only as a SHA-256 hash, 30-day expiry, sliding on use. Logout
  deletes the session; a password change, disabling a user or deleting a user invalidates their sessions.
- Five failed logins for a username or an IP within 10 minutes temporarily lock the login, with a
  message saying how long to wait.
- **First run:** while no accounts exist every page redirects to `/setup`, and the first account
  created there is the admin. After that `/setup` returns 404.
- By default only admins create accounts (an admin sets a temporary password, the user must change
  it at first login). The admin setting "Allow self-registration" (off by default) enables `/register`;
  self-registered accounts are normal users.
- Every page needs a session (API calls get 401, pages redirect to `/login`). The upstream API key
  stays server-side.

## Per-user data

Each user has their own folder: `data/users/<user_id>/{transcripts,wiki,dream-runs,conversations.json}`.
The wiki tool sandbox, the transcript index, the graph cache and the live dream-log stream are all
scoped to it, so no endpoint or tool can read another user's data. Admins see account information
and token numbers only — never chat content — and the admin panel says so.

`data/app.db` and `data/reasoning.json` stay global: the database is the app's, the reasoning map is
configuration rather than a user's data.

**Migration.** On first setup, the existing single-user data is moved into the first admin's folder.
A copy of `data/` is written to `data-backup-<timestamp>/` first, and the wiki's own `.git` moves with
it, so the wiki history survives.

## Token usage and limits

Every upstream call is recorded in `usage_events` — one row per call, including each iteration of
the tool loop: user, time (UTC), prompt and completion tokens, model, source (`chat` or `dreaming`),
conversation, and whether the number is an estimate.

- The server's `usage` object is used when it is present. For streaming requests the app sends
  `stream_options: {"include_usage": true}` and reads the usage from the final chunk. When the
  endpoint does not report usage, the numbers are estimated as characters / 4 with a small per-message
  overhead, marked `estimated = 1`, and shown with a `~` / "estimated" note in the UI.
- Dreaming usage is recorded with `source = "dreaming"` and shown separately in the statistics.
  An admin setting, "Count dreaming tokens toward limits", is off by default: dreaming is recorded
  but does not block a chat turn.
- Users see their own usage in the settings dialog and in the sidebar account popup: today and this
  week, with a thin bar that turns amber at 80 % and red at 100 %.

**Limits.** Each user may have a daily and a weekly limit. An empty value or 0 means no limit for
that window; if both are empty the user is unlimited; each limit is checked on its own only if it is
greater than 0. Daily means the current local calendar day, weekly the current local calendar week
(Monday to Sunday) in `TIMEZONE`. Counted tokens are the `total_tokens` of that user's events in the
window, chat only unless the admin setting above is on.

The check happens before a chat turn and before each call inside the tool loop. When a limit is
reached no call is made; the API returns 429 with which limit, the usage, the limit and when it
resets, and the chat shows a plain notice with sending disabled until then. A response that is
already streaming is allowed to finish, so a user can overshoot by at most one response; that usage
is recorded afterwards.

The admin panel (`/admin`) has a Users tab (limits edited inline, accepting `100k` or `1.5m`; make/remove
admin, reset password, disable, delete, create user, self-registration, the last 50 audit entries) and a
Usage tab (Day/Month/Year/All time with a period navigator, totals, per-user table with share and current
day/week, a bar chart with stacked-by-user mode, and CSV export). There must always be at least one
active admin, so the last one cannot be demoted, disabled or deleted.

Deleting a user removes their data folder but keeps their usage rows with `user_id` set to NULL, so the
aggregate numbers for a period do not change; those rows show as "(deleted user)".

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
```

The tests cover the limit semantics (every combination, the boundary at exactly the limit, day and
week rollover including the Monday start and the timezone), period aggregation, the last-admin rule,
first-run setup, login rate limiting, and per-user isolation through both the tools and the endpoints.
They run against a throwaway data folder, never the real `data/`.

`scripts/seed_demo.py` creates a few fake users with fake usage over several months for looking at the
admin charts. It is never run automatically and writes to its own `data/demo` folder.

## Layout

```
app.py         FastAPI: auth gate + CSRF, chat (SSE), wiki API, dream records, admin and usage API
auth.py        session cookie, CSRF double-submit check, page gate, admin check
accounts.py    users, argon2 hashing, sessions, login lockout, admin changes, audit log
db.py          SQLite schema (users, sessions, usage_events, login_attempts, audit, app_settings)
limits.py      limit parsing, local day/week windows, the check, reset time
usage.py       recording one call, and period aggregation for the admin views
appdata.py     per-user data folders, contextvar for the current user, first-run migration
agent.py       wiki tools + tool loop (usage and limit hooks)
llm.py         httpx upstream, normalised streaming events
dream.py       the dreaming job, per user
dreamlog.py    run records, readers, live progress per user
graph.py       graph data for the wiki view, cached per user
reasoning.py   effort levels -> request fields per model
wiki.py store.py config.py
prompts/       system.md, dream_per_transcript.md, dream_cleanup.md
static/        index.html, wiki.html, auth.html, admin.html, styles.css, admin.css,
               js/ modules (including the account popup and the admin panel), fonts/, icons
scripts/       make_logo.py, make_icons.py, seed_demo.py
design/        the logo candidates that were not picked
data/          app.db, users/<id>/…, reasoning.json — not committed
```
