# dreamscape

A small self-hosted chat app with a markdown wiki as its memory and a nightly dreaming job:
the model reads idle transcripts, updates the wiki, and commits. FastAPI + httpx, SQLite for
accounts and usage, plain files for everything else — no framework, no build step, nothing from a CDN.

![screenshot](docs/screenshot.png)

## Features

- Chat with a wiki tool loop (`wiki_list`, `wiki_read`, `wiki_search`, `wiki_write`), thinking
  shown separately from the answer, and a reasoning-effort menu (four levels, remembered per chat).
- Web tools in chat, off per conversation from the tools popup: `web_search` (DuckDuckGo by default,
  Brave when a key is set) and `web_fetch` with an SSRF check repeated after every redirect. Results
  are cached per query and rate-limited per user, and the model is told that page text is data, not
  instructions. Dreaming keeps the wiki tools only, so it is never fed pages it cannot verify.
- A wiki viewer with List, Graph and Dream log tabs. The graph is a canvas force layout with no
  library; the dream log shows each run, its steps per transcript, the diffs, and live progress.
- A nightly dreaming job, runnable from cron or from the "Dream now" button, with structured
  records per run and a human-readable summary in each user's `dream-log.md`.
- Accounts with an admin role, session cookies, CSRF, login rate limiting, per-user data folders,
  per-user token usage statistics and optional daily/weekly token limits. An admin panel at
  `/admin` shows the users and the usage numbers, with a chart and CSV export.
- Chat titles: a new chat is named after its first reply, unless a name was typed by hand or the
  setting is off. The title call is metered like any other call.

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

Or run the same job from the scheduler, which is what the container does. It wakes at a wall-clock
time in the app's timezone (`DREAM_TIME`, default 03:00, in `TIMEZONE`), so it moves with the
changeover to winter time, and it does nothing else:

```sh
.venv/bin/python scripts/dream_scheduler.py                 # waits for 03:00 in TIMEZONE
.venv/bin/python scripts/dream_scheduler.py --at 02:30      # a different time
.venv/bin/python scripts/dream_scheduler.py --run-now       # dream once, then keep the schedule
```

A run that cannot take the lock (the "Dream now" button is busy, or a previous run is still going)
is skipped and the next night is scheduled as normal. A failed night is logged and the scheduler
keeps running. SIGTERM and SIGINT stop it between runs.

The job is one process with one lockfile (`data/.dream.lock`), so cron, the scheduler and the
"Dream now" button never overlap. "Dream now" in the UI dreams only for the signed-in user; the
scheduler and the cron job loop over all active users.

## Docker

```sh
cp .env.example .env                # both services read this file
docker build --build-arg PUID=$(id -u) --build-arg PGID=$(id -g) .
docker compose up -d
```

Two services from one image:

- `app` — the web app on port 8000, published to loopback only. It always binds `0.0.0.0` inside the
  container, whatever `HOST` says in `.env`; put it behind HTTPS if you publish it further, so the
  session cookie can be marked `Secure`.
- `dreamer` — `scripts/dream_scheduler.py`, running the job at `DREAM_TIME` each night.

`data/` is a bind mount, so the build args should match the user that owns it. The image runs as a
non-root user, and it marks the mounted wiki repositories as safe for git (`git config --global --add
safe.directory '*'`), because a bind mount belongs to the host user; the commit identity itself comes
from `dream.py`. The health check asks the unauthenticated `/healthz`, which answers 200 when the app
can read its own database and 503 when it cannot.

```sh
docker compose up -d app
docker compose logs -f dreamer       # what the nightly runs did
docker compose run --rm dreamer python dream.py --dry-run
```

## Accounts

The default bind is `127.0.0.1`, so on a home network you can open it over plain HTTP. If you
serve it on the network, put it behind HTTPS: the session cookie is `HttpOnly` and `SameSite=Lax`,
and it is marked `Secure` when the request arrives over https (read from `X-Forwarded-Proto`).
The CSRF cookie is deliberately not `HttpOnly`, because the page has to read it and send it back
as `X-CSRF-Token`; every state-changing request is checked for that.

- Passwords are hashed with argon2, minimum 8 characters. Usernames are case-insensitive unique,
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

## Chat titles

A new chat is named after its first reply (`titles.py`, `prompts/title.md`): the model is asked for a
short name, and the result is stored as the conversation title. A name typed by hand is never
overwritten, so the call only happens when no title is stored yet. The call is metered like any other
upstream call — the limit is checked first and the usage is recorded — and when the limit is reached no
call is made; the title stays whatever the app would show anyway, which is the first line of the message.

The request uses reasoning level `none` and `max_tokens` 40, since naming a chat does not need thinking.
The client asks for a name only after the first reply, driven by the Settings row "Name a chat from the
first reply" (on by default). Titles are cleaned to one line, no quotes, max 60 characters.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
node --test tests/js/notes.test.mjs
```

The tests cover the limit semantics (every combination, the boundary at exactly the limit, day and
week rollover including the Monday start and the timezone), period aggregation, the last-admin rule,
first-run setup, login rate limiting, chat titles (cleaning, the metered call and the estimate when the
endpoint reports nothing, a typed name kept, a reached limit skipping the call), per-user isolation
through both the tools and the endpoints, the stream parser and tool loop against a fake upstream
(tool calls split across chunks, thinking tags split across chunks, the missing-answer rule), the web
tools (one result shape for both providers, the cache, the per-user rate limit, the SSRF block
including a redirect to a private address, truncation), and the nightly scheduler's clock across the
changeover to winter time. The one UI rule that is worth testing — when the "no final answer" note may
be shown — is in `static/js/notes.js` and tested as a plain node test, with a pytest wrapper so it
runs with the rest.

They run against a throwaway data folder, never the real `data/`; the fixture clears the user folders
too, because user ids are reused once the rows are deleted. Nothing in the tests calls the network: the
upstream and the search providers are replaced, and the fetch tests use a local server.

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
agent.py       wiki tools + web tools + tool loop (usage and limit hooks)
llm.py         httpx upstream, normalised streaming events, one non-streaming completion
websearch.py   search providers (ddgs, brave), cache, per-user rate limit, one result shape
webfetch.py    one page to text: SSRF check after every redirect, caps, truncation
titles.py      naming a chat from the first exchange, cleaning the result, the metered call
dream.py       the dreaming job, per user
dreamlog.py    run records, readers, live progress per user
graph.py       graph data for the wiki view, cached per user
reasoning.py   effort levels -> request fields per model
wiki.py store.py config.py
prompts/       system.md, web_tools.md, dream_per_transcript.md, dream_cleanup.md, title.md
static/        index.html, wiki.html, auth.html, admin.html, styles.css, admin.css,
               js/ modules (including the account popup and the admin panel), fonts/, icons
scripts/       make_logo.py, make_icons.py, seed_demo.py, dream_scheduler.py, healthcheck.py
tests/         pytest, plus tests/js/ for the UI state rule (plain node test)
Dockerfile, docker-compose.yml, .dockerignore
design/        the logo candidates that were not picked
data/          app.db, users/<id>/…, reasoning.json — not committed
```
