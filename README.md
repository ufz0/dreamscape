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
  per-user token usage statistics and optional daily/weekly token limits, plus a global default limit
  per window that a user's own value overrides. An admin panel at `/admin` shows the users, the usage
  numbers with a chart and CSV export, the runtime settings, and the inference connections.
- Inference connections managed from the admin panel: presets for Strata, llama.cpp, Ollama, vLLM and
  LM Studio, models read from each connection or added by hand, per-model capability flags, namespaced
  model ids (`<connection-slug>/<model-id>`), a default chat model and a separate dreaming model, and a
  "Test connection" that reports each check separately. API keys are stored encrypted.
- A chat turn is a run on the server, not a request: sending returns 202 with a run id, the answer
  arrives over an event stream that can be dropped and re-attached without the text being delivered
  twice, a partial answer is written to the transcript while the run is going, and a run keeps going
  when the browser tab is closed. Per-user and app-wide run caps, a per-connection queue, and a stop
  button that is the only way a run ends on purpose.
- Chat titles: a new chat is named after its first reply, unless a name was typed by hand or the
  setting is off. The title call is metered like any other call.
- Files a user attaches to a message (the `+` button, drag & drop, paste), with chips showing size and
  the estimated cost, images sent only to a model flagged for vision, text files, PDF and docx read as
  `<attachment>` blocks, `read_file` for a file over the inline cap, per-file and per-account caps,
  owner-only file endpoints, and Settings → Storage.
- An OpenAI-compatible API at `/api/v1` (models, chat completions, embeddings) entered with a `dsk-…`
  key that is stored only as a hash and shown once at creation, answers in the OpenAI error shape,
  records usage as `source = "api"` counted toward the same limits, has a per-key rate limit and a
  per-key breakdown in the admin usage view.
- Numbered SQL migrations in `migrations/`, applied at startup with a backup of the database first.

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
HOST=0.0.0.0
PORT=8000
COOKIE_SECURE=auto
TRUSTED_PROXIES=
APP_SECRET_KEY=
DATA_DIR=./data
TIMEZONE=Europe/Vienna
IDLE_MINUTES=60
MAX_TOOL_ITERATIONS=8
INDEX_MAX_LINES=150
PAGE_MAX_LINES=120
```

`HOST` binds every interface by default, so the app is reachable from the network; use `127.0.0.1`
for a machine-only instance. `COOKIE_SECURE` is `auto`, `true` or `false`: `auto` marks the session
cookie `Secure` when the request arrives over https, which is what a reverse proxy reports through
`X-Forwarded-Proto` — and only a proxy listed in `TRUSTED_PROXIES` is believed. Startup prints a
note when the app is bound to a non-loopback address over plain HTTP.

`APP_SECRET_KEY` is the key for the encrypted values in the database (the connection API keys). When
it is not set, a key is generated once into `data/secret.key` with mode 600 and the path is printed at
startup, so a restore is reproducible only if the key is kept. Set it if the data folder is rebuilt.

Runtime settings — the global default limits, the run caps, the queue timeout, the default chat and
dreaming models — are in the admin panel, not in `.env`, because they change while the app is running.
The `STRATA_*` values are the fallback for the first start: they become a connection when the
connections table is empty, and nothing else reads them afterwards.

`TIMEZONE` only decides where a day, week, month or year begins in the usage views. Token counts
are stored in UTC and aggregated in that zone; weeks start on Monday.

## Run

```sh
./run.sh                    # serves on 0.0.0.0:8000 by default
```

Open `/` for chat and `/wiki` for the wiki viewer. The FastAPI docs, ReDoc and the OpenAPI JSON are
turned off: the app has no public API surface beyond its own pages, and every route needs a session
except `/healthz`, `/login`, `/setup`, `/register` and the static assets.

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

- `app` — the web app on port 8000, published as `${BIND_ADDR:-0.0.0.0}:${PORT:-8000}`, so the port is
  open on the host by default; set `BIND_ADDR=127.0.0.1` to keep it on the machine. It always binds
  `0.0.0.0` inside the container, whatever `HOST` says in `.env`; put it behind HTTPS if you publish it
  further, so the session cookie can be marked `Secure`. `stop_grace_period: 90s` gives running chat
  turns time to finish (the app allows 60 s at shutdown) before the container is killed.
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

The default bind is every interface, so on a home network you can open it over plain HTTP. Past a
trusted network, put it behind HTTPS: the session cookie is `HttpOnly` and `SameSite=Lax`, and with
`COOKIE_SECURE=auto` it is marked `Secure` when the request arrives over https. `X-Forwarded-For` and
`X-Forwarded-Proto` are only read from an address listed in `TRUSTED_PROXIES`, so a client cannot
claim to come through a proxy it is not; the login rate limit and the Secure flag follow the address
that is actually trusted. Startup prints a reminder when the app is bound to a non-loopback address
over plain HTTP.
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

`data/app.db`, `data/secret.key` and `data/reasoning.json` stay global: the database is the app's, the
encryption key is the app's, the reasoning map is configuration rather than a user's data. `data/backups/`
holds the database copies written before a migration runs.

**Migration.** On first setup, the existing single-user data is moved into the first admin's folder.
A copy of `data/` is written to `data-backup-<timestamp>/` first, and the wiki's own `.git` moves with
it, so the wiki history survives.

## Migrations and runtime settings

Schema changes are numbered SQL files in `migrations/` (`001_baseline.sql`, `002_connections.sql`,
`003_usage_connection.sql`, `004_runs.sql`), applied at startup in `schema_version` order, one
transaction per file. Before any pending file runs, the database is copied to
`data/backups/app-<timestamp>.db`. `db.py` no longer holds a schema of its own; it only opens the
connection, runs the transactions, the key/value settings table and the audit log, and calls
`migrations.apply()`. Existing data keeps working: the settings table was already there, so the runtime
settings use it rather than a new table, and the first-start migration from `STRATA_*` writes one
connection and repoints the models that were stored by bare name.

Values an admin can change while the app is running live in the settings table, read through
`settings.py`: the global default limits per window, the run caps, the queue timeout, the default chat
and dreaming models, whether dreaming counts toward limits, and self-registration. Secrets in the
database (the connection API keys) are encrypted with Fernet; the key comes from `APP_SECRET_KEY`, or
from a generated `data/secret.key` with mode 600. Secrets are write-only in the API and the UI: the
server returns only whether one is set and what it ends with.

## Inference connections

The Connections tab of the admin panel manages where calls go. A connection is a name, a slug, a preset
(Strata, llama.cpp, Ollama, vLLM, LM Studio, or a custom OpenAI-compatible base URL), an API key, extra
headers, the paths for the model list and the chat completions, a timeout, and how many requests may be
in flight at once. Models are read from the connection or added by hand; each model record keeps a
display name, whether it is shown, and its capabilities (vision, tool calls, reasoning, usage reporting,
context). A capability nobody has tested is stored as unknown and reads as `?`, not as "no".

Model ids are namespaced as `<connection-slug>/<model-id>`, so the same model on two connections is two
models. The default chat model and the model used for dreaming are chosen separately in Runtime settings;
a connection cannot be deleted while it holds either one.

"Test connection" reports each check on its own row: the model list, a plain chat, streaming, tool
calls, usage reporting, and which reasoning field the server answers with. A stream that returns text, or
a call that returns tool calls, sets that model's flags. The result is stored with the connection and
shown in its card.

Requests go through a bounded FIFO queue per connection (2 at a time by default, 5 minutes as the queue
timeout), and the client shows the position while a slot is held. The SSRF block that `web_fetch` applies
does not apply to a connection's own URL, since that address is the administrator's choice. Every change
to a connection, its models or the settings is written to the audit log.

## Chat runs

A turn is a run on the server, not a request. Sending a message (`POST /api/conversations/<id>/messages`)
returns 202 with a run id; the answer arrives on `GET /api/runs/<id>/events`, which replays whatever the
run has already produced (from `after` or `Last-Event-ID`) and then continues live. `POST /api/runs/<id>/cancel`
is the only way a run stops on purpose: a dropped event stream does not cancel it, so closing the tab lets
the turn finish, and the partial answer is written to the transcript about every 2 seconds. Reopening the
chat shows the transcript, and re-attaching with `Last-Event-ID` delivers nothing twice. A run that is not
in memory any more is read from the transcript instead.

A second message while a run is active is refused with 409, and the text stays in the input; the send
button is the stop button while the run is running. A run that was going when the app stopped is marked
`interrupted` at startup, and the chat says so. At most 3 runs per account and 10 app-wide, and no run
longer than 30 minutes — all three are runtime settings. A sidebar row shows a spinner while its chat is
generating and a dot for a result that arrived since that chat was last read; the tab title carries the
same dot for a chat in another window. Dreaming and the rest of the API are unaffected.

## Token usage and limits

Every upstream call is recorded in `usage_events` — one row per call, including each iteration of
the tool loop: user, time (UTC), prompt and completion tokens, model, the connection it went to,
source (`chat` or `dreaming`), conversation, and whether the number is an estimate.

- The server's `usage` object is used when it is present. For streaming requests the app sends
  `stream_options: {"include_usage": true}` and reads the usage from the final chunk. When the
  endpoint does not report usage, the numbers are estimated as characters / 4 with a small per-message
  overhead, marked `estimated = 1`, and shown with a `~` / "estimated" note in the UI.
- Dreaming usage is recorded with `source = "dreaming"` and shown separately in the statistics.
  An admin setting, "Count dreaming tokens toward limits", is off by default: dreaming is recorded
  but does not block a chat turn.
- Users see their own usage in the settings dialog and in the sidebar account popup: today and this
  week, with a thin bar that turns amber at 80 % and red at 100 %. The bar shows the limit that applies
  to them, with where it comes from (their own value or the global default) in the same words the 429
  uses.

**Limits.** Each window is resolved on its own: a user's own value greater than 0 wins, otherwise the
global default for that window, otherwise no limit. A user explicitly set to unlimited (`unlimited`,
`inf`, `∞` or a negative number, stored as -1) overrides a global default. An empty value or 0 means
"no value of their own", not a limit of zero; each limit is checked only if the resolved number is
greater than 0. Daily means the current local calendar day, weekly the current local calendar week
(Monday to Sunday) in `TIMEZONE`. Counted tokens are the `total_tokens` of that user's events in the
window, chat only unless the admin setting above is on.

The resolved number and where it comes from is shown in the admin Users tab and in the user's own bars,
with the same words the 429 uses. The 429 says which window, the limit, whether it is the user's own or
the global default, the usage and when it resets.

The check happens before a chat turn and before each call inside the tool loop. When a limit is
reached no call is made; the API returns 429 with which limit, the usage, the limit and when it
resets, and the chat shows a plain notice with sending disabled until then. A response that is
already streaming is allowed to finish, so a user can overshoot by at most one response; that usage
is recorded afterwards.

The admin panel (`/admin`) has three tabs. Users: limits edited inline, accepting `100k` or `1.5m` or
`unlimited`, each row showing the limit that actually applies and where it comes from; make/remove admin,
reset password, disable, delete, create user; the runtime settings grid and the last 50 audit entries.
Usage: Day/Month/Year/All time with a period navigator, totals, per-user table with share and current
day/week, a bar chart with stacked-by-user mode, and CSV export. Connections: the connection cards with
their per-check test results, the model table with the capability flags, and the live queue for each
connection. There must always be at least one active admin, so the last one cannot be demoted, disabled
or deleted.

Deleting a user removes their data folder but keeps their usage rows with `user_id` set to NULL, so the
aggregate numbers for a period do not change; those rows show as "(deleted user)".

## Chat titles

A new chat is named after its first reply (`titles.py`, `prompts/title.md`): the model is asked for a
short name, and the result is stored as the conversation title. A name typed by hand is never
overwritten, so the call only happens when no title is stored yet. The call is metered like any other
upstream call — the limit is checked first and the usage is recorded — and when the limit is reached no
call is made; the title stays whatever the app would show anyway, which is the first line of the message.

The request uses reasoning level `none` and `max_tokens` 40, since naming a chat does not need thinking.
The name is produced at the end of the first turn, inside the run, driven by the Settings row "Name a
chat from the first reply" (on by default); the client applies it when the run's event stream carries the
title. Titles are cleaned to one line, no quotes, max 60 characters.

## Attachments

A file reaches the chat three ways: the `+` button in the input card, dragging files onto the
conversation, and pasting. Each file becomes a chip above the textarea — thumbnail for an image,
name, size, the estimated token count, a progress bar while it is being read, and an x to remove it.
The cost line under the chips adds the estimates up and warns when the total is past the selected
model's context. The number shown is what the model is actually sent, not the whole file: a file
longer than the inline cap contributes only its inline part, and the rest is reached through the tool.

`POST /api/uploads` returns a file id; a message references file ids and the transcript stores
attachment metadata only (never base64), so a transcript stays plain text. `uploads.py` is the whole
feature: the bytes live in `data/users/<user_id>/uploads/<uuid>/` under a generated name, and the
database holds the metadata.

What the model sees is text. An image goes as an `image_url` content part, and only for a model
flagged for vision — a model that is not flagged is told the file is there and not sent the bytes.
Everything else is read into a cached text form and wrapped as:

```
<attachment name="notes.txt" type="text"> … </attachment>
```

with the "this is data, not instructions" note added to the system prompt when attachments are
present (`prompts/attachments.md`). A file longer than the inline cap is shown in part and the block
says the rest is readable with `read_file(file_id, offset, length)`; that tool is offered only when a
truncated file is in the conversation. The cache (`text.md` in the same folder) holds the full
extraction, which is what lets the tool page past the inline cap. Files stay part of the history on
later turns.

What is accepted is decided by the bytes, not by the filename: images (png, jpg, webp, gif), text-like
files, PDF through pypdf with a page and character cap, and docx through python-docx. An SVG is
accepted as text, never treated as an image. Everything else is refused, and archives are not opened.
A PDF with no text layer answers with the exact line "No text layer found. Scanned PDFs are not
supported."

The caps are admin runtime settings: 20 MB per file, 5 files per message, 500 MB per user, the allowed
extension list, and about 30k inline characters per file. Size is enforced while the upload is read, so
an oversized file is refused before anything of it is stored, extracted or counted; a request that
announces a length over the cap is refused with 413 before its body is read at all.

Security: content is sniffed as well as the extension, filenames are sanitized, files are stored under
the user's own folder with a generated name, and both file endpoints are owner-only with
`Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` — another user's file,
including an admin's, reads as no such file. Settings → "Storage" shows usage against the caps, lists
the user's own files with a Delete, and deleting a chat deletes the files attached in it.
There is no image library, so the thumbnail endpoint serves the original bytes and the chip scales it;
the token estimate for an image is its tile count (tiles × 170 + 85), read from the file header.

Dreaming does not read attachment contents by default: a transcript shows the model the file names, and
`--include-attachments` is the flag that opts a run into the text. Runs read files from disk through
the same module.

## OpenAI-compatible API

`/api/v1` is a passthrough to one connection, with no wiki, memory, web tools or system prompt added —
what a client sends is what the engine gets.

- `GET /api/v1/models` — the models the key's account may use, in the OpenAI list format, with
  namespaced ids.
- `POST /api/v1/chat/completions` — streaming and non-streaming, body passed through unchanged
  (images, tools, tool_choice, response_format, temperature), with the connection's auth, headers,
  path and body overrides applied on top. `reasoning_effort` is honoured when it is sent: the model's
  reasoning mapping is applied to it. `tool_calls` and reasoning come back as the upstream sends them,
  and the model field is the id the client asked for.
- `POST /api/v1/embeddings` — a passthrough, and a 404 when the connection does not answer it.

Errors are always the OpenAI shape `{"error": {"message", "type", "code"}}` with a fitting status:
401 for a bad key, 403 when API access is off, 404 for an unknown model, 429 for a limit or the
per-key rate limit (with `Retry-After`), 502 or 504 for the upstream.

Keys are `dsk-` plus 40 random characters. Only the SHA-256 hash and the first 8 characters are
stored, so a key is shown once at creation and never read back. Settings → "API keys" creates one with
a name and an optional expiry, lists name, prefix, created, last used and tokens used, revokes, and has
a "How to use" box with the base URL and a curl example. There is a global "Allow API access" switch
and a per-user toggle, both on by default; an admin's Users row menu has both and a Keys view, and an
admin can revoke any key. A key stops working when it is revoked, when its expiry passes, or when its
account is disabled or deleted — all of that is checked at use time. A key is accepted on `/api/v1/*`
only and the session cookie is not accepted there; a key is not accepted on the UI routes. Each key is
limited to 60 requests per minute and each account to 4 requests at one time, in memory, as the search
rate limit is.

API calls are recorded as `source = "api"` with the key id, the connection and the model, and count
toward the daily and weekly limits exactly like a chat: checked before the request, and a reached limit
answers 429 in the OpenAI shape with the window, the usage, the limit, when it resets and
`Retry-After`. A response that is already streaming is allowed to finish. The app always sends
`stream_options.include_usage` upstream so the numbers can be recorded, and drops the usage chunk when
the client did not ask for it. A client that goes away mid-stream stops the upstream here and records
what was counted so far — estimated when the engine reported nothing — which is the intended difference
from a chat run, where closing the tab does not cancel the run. The admin Usage tab can be narrowed to
one kind of call, and a selected account's numbers can be broken down by which key was used; the user's
own usage view shows where the tokens in a window came from.

## Tests

```sh
.venv/bin/python -m pytest tests/ -q
node --test tests/js/notes.test.mjs
.venv/bin/python scripts/browser_check.py     # needs playwright and a browser; not an app dependency
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

The newer areas are tested the same way: migrations (a fresh database, an existing one with data, the
backup, `schema_version`, a file applied once), the encrypted secrets (the key from the environment or
generated with mode 600, a value that reads back as itself, the write-only form the API returns), the
bind and the proxy trust (the default bind, `COOKIE_SECURE` in each mode, only a listed proxy is
believed, which routes are reachable without a session), the connections (the presets and their default
paths, slugs, the model list stored and added by hand, the namespaced ids, the flags an unknown
capability keeps as unknown, a test reporting each check separately, deletion refused while a connection
holds a default, the queue handing a slot to the next request in order), and the runs (the caps, the
409 for a second message, cancel being the only stop, a dropped stream not cancelling, replay from an
event id without repeating anything, the partial flush, one record per turn, the interrupted marking at
startup).

Uploads and the OpenAI-compatible API are tested against the running app with the official `openai`
client (its `http_client` pointed at the app, so nothing reaches the network): the models list, a
non-streaming call, a streaming call with and without `include_usage`, tools passed through, image
content parts, a bad / revoked / expired key, API access turned off, the per-key rate limit, a limit
reached (daily for a user and weekly for the app, both counting API calls with chat), a client
disconnect recording the partial usage, cookies not accepted on `/api/v1` and keys not accepted on the
UI routes. And the upload rules: content sniffing against the extension (an executable renamed `.txt`,
an SVG, a file over the cap), path-traversal filenames, the quota, another account's file id reading as
no such file, a PDF with and without a text layer, truncation and `read_file` paging, an image refused
for a model that is not flagged for vision, and deleting a chat removing its files.

`scripts/browser_check.py` is the part that only shows up in a browser: the grouped model picker, the
queue line, the sidebar row state and the tab title, the send button becoming Stop, a reconnect with
nothing delivered twice, a reload showing the transcript once per turn, the effective limit in both
places, the admin Connections card with its per-check rows, write-only secrets and capability flags that
were never tested, the upload flow (file input, drag & drop, paste, a chip with its estimate, the cost
line and its context warning, the error states, the chips in a sent message), the Settings Storage and
API keys sections, and the admin Usage tab with its source filter and per-key breakdown. It serves the
app in-process with a fake upstream, so nothing reaches the network, and it takes screenshots in both
themes. It is not part of pytest, and `playwright` is not in `requirements.txt`; install it separately to run
the check.

They run against a throwaway data folder, never the real `data/`; the fixture clears the user folders
too, because user ids are reused once the rows are deleted. Nothing in the tests calls the network: the
upstream and the search providers are replaced, and the fetch tests use a local server.

`scripts/seed_demo.py` creates a few fake users with fake usage over several months for looking at the
admin charts. It is never run automatically and writes to its own `data/demo` folder.

## Layout

```
app.py         FastAPI: auth gate + CSRF, runs (202, the event stream, cancel), connections API,
               wiki API, dream records, admin and usage API, the upload and API-key routes, the startup
               migration, shutdown grace
auth.py        session cookie, CSRF double-submit check, page gate, admin check, proxy trust, client ip
accounts.py    users, argon2 hashing, sessions, login lockout, admin changes, audit log
db.py          SQLite connection, transactions, the settings table, audit; calls migrations.apply()
migrations.py  numbered SQL files applied at startup, schema_version, the backup before pending files
migrations/    the numbered files, 001_baseline through 006_api_keys
settings.py    the runtime settings read from the settings table (limits, run caps, defaults, upload caps)
cipher.py      Fernet for the secrets in the database; the key from APP_SECRET_KEY or data/secret.key
connections.py presets, slugs, models and their flags, namespaced ids, the test, per-connection queue
runs.py        the run record, the bounded event buffer and replay, the caps, cancel, the partial flush
limits.py      limit parsing, a global default resolved per window, local day/week windows, reset time
usage.py       recording one call with its connection, its source, and period aggregation for the admin views
apikeys.py     dsk- keys: the hash and the prefix, authenticate, per-key rate limit, per-user concurrency
openai_api.py  /api/v1: the models list, chat completions (stream and non-stream), embeddings, the
               OpenAI error shape, the usage chunk, a client leaving mid-stream
appdata.py     per-user data folders, contextvar for the current user, first-run migration
agent.py       wiki tools + web tools + read_file + the tool loop, run against a target (usage and limit hooks)
llm.py         httpx upstream for a target, normalised streaming events, one non-streaming completion
websearch.py   search providers (ddgs, brave), cache, per-user rate limit, one result shape
webfetch.py    one page to text: SSRF check after every redirect, caps, truncation
uploads.py     a file a user attaches: content sniffing, the caps, extraction (pypdf, python-docx), the
               cached text, the <attachment> block, the token estimate, owner-only serving
titles.py      naming a chat from the first exchange, cleaning the result, the metered call
dream.py       the dreaming job, per user
dreamlog.py    run records, readers, live progress per user
graph.py       graph data for the wiki view, cached per user
reasoning.py   effort levels -> request fields per model, per connection and per model
wiki.py store.py config.py
prompts/       system.md, web_tools.md, attachments.md, dream_per_transcript.md, dream_cleanup.md, title.md
static/        index.html, wiki.html, auth.html, admin.html, styles.css, admin.css,
               js/ modules (the run client, the chat view, the model picker, the account popup, the
               attachment chips, the API keys section, the admin panel with its Connections tab),
               fonts/, icons
scripts/       make_logo.py, make_icons.py, seed_demo.py, dream_scheduler.py, healthcheck.py,
               browser_check.py (the headless-browser check, needs playwright)
tests/         pytest, plus tests/js/ for the UI state rule (plain node test)
Dockerfile, docker-compose.yml, .dockerignore
design/        the logo candidates that were not picked
data/          app.db, users/<id>/…, users/<id>/uploads/<uuid>/, reasoning.json, secret.key,
               backups/ — not committed
```
