"""A headless-browser check of the parts that only show up in the browser.

The app is served from a throwaway data folder with one session and a fake upstream, so a run can
finish without a model server anywhere. Nothing here reaches the network.

What it checks: the grouped model picker, the run client (send, follow, the queue line, a dropped
stream, a reconnect with nothing delivered twice), the sidebar row state, the wording for a refused
second message and an interrupted turn, the effective limit in both places, the admin Connections
tab with its per-check rows, write-only secrets and capability flags that were never tested, the
runtime settings grid, the upload flow (the file input, drag & drop, paste, a chip with its estimate,
the cost line and its context warning, the error states, the attachment chips in a sent message, the
read_file tool for a file over the inline cap, the Storage section), the API keys section and what a
key is accepted on, and the admin Usage tab with its source filter and per-key breakdown.
Screenshots are taken in both themes.

Run: .venv/bin/python scripts/browser_check.py

It needs `playwright` and a browser on this machine; neither is a dependency of the app. Screenshots
are written to a folder under the system temp directory, and the app runs against a throwaway data
folder, never the real `data/`.
"""
import asyncio
import glob
import json
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = pathlib.Path(os.environ.get("BROWSER_CHECK_DIR", tempfile.gettempdir())) / "dreamscape-browser-check"
DATA_DIR = WORK / "data"
SHOT_DIR = WORK / "shots"
shutil.rmtree(WORK, ignore_errors=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
SHOT_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(DATA_DIR)

import accounts
import apikeys
import app as appmodule
import appdata
import config
import connections
import db
import llm
import runs
import settings
import store
import uploads
import usage
import uvicorn
from playwright.sync_api import sync_playwright

PORT = 8731
BASE = f"http://127.0.0.1:{PORT}"

# What api_request needs to look like the browser: the session this check creates, and the CSRF token
# the app only hands out when a page is loaded.
LOGIN_TOKEN = ""
CSRF_TOKEN = ""

problems = []

MODEL_ID = "check-model"
NO_VISION_ID = "check-no-vision"
UNTESTED_ID = "check-untested"     # added without any capability flag, so nothing about it is known
MODEL = ""
ANSWER = "An answer written by the fake upstream."

# What the fake was asked for, so a check can look at the messages and the tools a run put on the wire.
seen_calls = []

# How long the fake upstream holds its slot. Three seconds is enough for a second request to be
# seen waiting in the queue, and for a stream closed in the middle to be seen not cancelling.
HOLD_SECONDS = 3.0
FLUSH_GAP = 2.5      # longer than runs.FLUSH_SECONDS, so a flush happens between two events


def check(name, ok, detail=""):
    print(f"{'ok   ' if ok else 'FAIL '}{name}{': ' + detail if detail else ''}")
    if not ok:
        problems.append(name)


# ---------- the fake upstream ----------

async def fake_models(target):
    return [{"id": MODEL_ID}]


async def fake_complete(target, model, messages, temperature, max_tokens=None, extra_body=None):
    seen_calls.append({"model": model, "messages": messages, "tools": []})
    return {"text": "ok", "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}


async def fake_stream(target, model, messages, temperature, tools=None, max_tokens=None, extra_body=None):
    seen_calls.append({"model": model, "messages": messages, "tools": tools or []})
    text = messages[-1].get("content") or ""

    if tools and any(tool["function"]["name"] == "probe" for tool in tools):
        yield {"type": "usage", "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}}
        yield {"type": "tool_calls", "tool_calls": [{"id": "call_probe", "name": "probe", "arguments": {}}]}
        return

    # The reasoning event carries the field name, which is what the reasoning check reports.
    yield {"type": "reasoning", "content": "Looking at what was asked.", "field": "reasoning_content"}
    yield {"type": "usage", "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}}

    if "drop" in text:
        await asyncio.sleep(2.0)        # the client has closed its stream; the run is still here
    if "hold" in text:
        await asyncio.sleep(HOLD_SECONDS)

    yield {"type": "delta", "content": ANSWER}

    if "flush" in text:
        await asyncio.sleep(FLUSH_GAP)
        yield {"type": "delta", "content": " And a second part."}
        await asyncio.sleep(1.5)        # the partial record is on disk while the run is still going

    yield {"type": "finish", "finish_reason": "stop"}


llm.list_models = fake_models
llm.complete = fake_complete
llm.stream_chat = fake_stream

# /api/v1 is a passthrough to a connection, so it needs the same shape as httpx.AsyncClient.
API_ANSWER = {"id": "chatcmpl-check", "object": "chat.completion", "created": 1, "model": MODEL_ID,
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "An answer through the API."},
                           "finish_reason": "stop"}]}
# The engine is not asked to report usage here, so what the app records for this call is an estimate.


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("the fake upstream refused")

    def json(self):
        return self.payload


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, path, json=None):
        return FakeResponse(API_ANSWER)


llm.client = lambda target=None: FakeClient()


def serve():
    import app

    config = uvicorn.Config(app.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            return
        time.sleep(0.05)
    raise RuntimeError("the app did not start")


def browser_for(playwright):
    """The installed playwright build has no matching download here, so use a browser already here."""
    home = pathlib.Path.home()
    found = sorted(pathlib.Path(path) for path in glob.glob(str(home / "Library/Caches/ms-playwright/chromium_headless_shell-*")))
    if found:
        executable = found[-1] / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
        if executable.exists():
            return playwright.chromium.launch(executable_path=str(executable), args=["--no-proxy-server"])
    return playwright.chromium.launch(channel="chrome", args=["--no-proxy-server"])


def start_run(conn, user, conversation_id, message):
    """A run started from here, so the page can be shown a state it did not create."""
    params = appmodule.run_params({"message": message, "model": MODEL}, MODEL)
    run = runs.create(conn, user, conversation_id, params)
    conn.commit()      # the run opens its own transaction; this one must not be held open
    runs.start(run)
    return run


def wait_for(predicate, seconds=12.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.15)
    return False


def usage_rows():
    return [dict(row) for row in db.connect().execute("SELECT * FROM usage_events ORDER BY id")]


def chat_calls():
    """The calls the chat made. Naming a conversation goes through the same fake, so those are left out."""
    title_prompt = config.read_prompt("title.md")
    return [call for call in seen_calls if call["messages"][0]["content"] != title_prompt]


def call_for(text):
    """The call for one message. Naming a chat uses the same fake, so the last call is not always this one."""
    calls = [call for call in chat_calls() if text in json.dumps(call["messages"])]
    return calls[-1] if calls else {"messages": [], "tools": []}


def shown(call):
    """What the model was sent, as text. Content parts are joined, so a check can look for a string
    without the quotes being escaped by json."""
    lines = []
    for message in call["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            lines.append("\n".join(part.get("text", "") for part in content))
        else:
            lines.append(str(content))
    return "\n".join(lines)


def png(width, height):
    """A real PNG, so the browser can show it. The image estimate reads its size from this header."""
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))

    def piece(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + piece(b"IHDR", struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00")
            + piece(b"IDAT", zlib.compress(raw)) + piece(b"IEND", b""))


def write_file(name, data):
    path = WORK / name
    path.write_bytes(data)
    return str(path)


def api_request(path, headers=None, body=None, method="GET"):
    """One request to this app, as a client would make it, so the OpenAI error shape is what comes back.

    The CSRF cookie only exists after a page or a login, so this carries the cookies the browser has.
    A check that sends its own Cookie header is left alone, because that is what tests a session.
    """
    request = urllib.request.Request(BASE + path, method=method)
    given = dict(headers or {})
    if "Cookie" not in given:
        cookies = [f"session={LOGIN_TOKEN}"] + ([f"csrf={CSRF_TOKEN}"] if CSRF_TOKEN else [])
        given["Cookie"] = "; ".join(cookies)
    if method != "GET" and CSRF_TOKEN:
        given.setdefault("X-CSRF-Token", CSRF_TOKEN)
    for name, value in given.items():
        request.add_header(name, value)
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode() or "{}"), dict(response.headers)
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode() or "{}")
        except Exception:
            payload = {}
        return error.code, payload, dict(error.headers)


def main():
    global MODEL, LOGIN_TOKEN, CSRF_TOKEN
    conn = db.connect()
    user = accounts.create_user(conn, "browsercheck", "a long enough password", role="admin", actor="check")
    accounts.create_user(conn, "inherits", "a long enough password", actor="check")
    accounts.create_user(conn, "ownlimit", "a long enough password", actor="check",
                         daily_limit=2000, weekly_limit=-1)
    settings.set_value(conn, user, "global_daily_limit", 5000)
    conn.commit()
    token = accounts.create_session(conn, user)
    conn.commit()
    LOGIN_TOKEN = token
    appdata.set_current(user["id"])
    appdata.current().ensure()

    connection = connections.create(conn, user, {
        "preset": "custom", "name": "Check engine", "base_url": "http://example.invalid",
        "api_key": "sk-check-1234", "max_concurrent": 1,
    })
    model_row = connections.add_model(conn, user, connection["id"], MODEL_ID,
                                      {"display_name": "Check model"})
    MODEL = model_row["namespaced"]
    asyncio.run(connections.test_connection(conn, connection["id"]))
    conn.commit()

    serve()

    with sync_playwright() as playwright:
        browser = browser_for(playwright)
        context = browser.new_context(viewport={"width": 1100, "height": 720})
        context.add_cookies([{"name": "session", "value": token, "url": BASE}])
        page = context.new_page()
        errors = []
        # The upload and key checks make the server refuse things on purpose, so those responses are
        # not page errors here.
        expected = ("/api/uploads", "/api/keys")

        def listen(target):
            target.on("pageerror", lambda error: errors.append(str(error)))
            target.on("console", lambda message: errors.append(message.text)
                     if message.type == "error" and "Failed to load resource" not in message.text else None)
            target.on("response", lambda response: errors.append(f"{response.status} {response.url}")
                     if response.status >= 400 and response.status != 409
                     and not any(part in response.url for part in expected) else None)

        listen(page)

        # ---------- the model picker names the connection ----------
        page.goto(BASE + "/", wait_until="networkidle")
        check("the page starts without a script error", not errors, "; ".join(errors[:3]))
        # Loading a page is what gives the browser the CSRF token; the requests below use the same pair
        # of cookies the page has, which is what a real client sends.
        CSRF_TOKEN = page.evaluate("() => (document.cookie.match(/(?:^|;\\s*)csrf=([^;]*)/) || [])[1] || ''")
        check("a page load is what sets the CSRF token the page can read back", bool(CSRF_TOKEN), repr(CSRF_TOKEN))
        page.click("#model-btn")
        page.wait_for_selector(".menu .menu-group", timeout=8000)
        groups = page.evaluate("""() => [...document.querySelectorAll('.menu .menu-group')].map(n => n.textContent)""")
        labels = page.evaluate("""() => [...document.querySelectorAll('.menu [role=menuitem] .item-label')].map(n => n.textContent)""")
        descs = page.evaluate("""() => [...document.querySelectorAll('.menu [role=menuitem] .item-desc')].map(n => n.textContent)""")
        check("the picker names the connection the model is on", groups == ["Check engine"], str(groups))
        check("the picker shows the display name, with the id under it",
              labels == ["Check model"] and descs == [MODEL_ID], f"{labels} {descs}")
        check("the group header is not a choice",
              not page.evaluate("() => document.querySelector('.menu .menu-group').getAttribute('role')"))
        check("the button shows the display name too",
              page.evaluate("() => document.querySelector('#model-btn .name').textContent") == "Check model",
              page.evaluate("() => document.querySelector('#model-btn .name').textContent"))
        page.screenshot(path=f"{SHOT_DIR}/browser-model-picker-light.png")
        page.keyboard.press("Escape")

        # ---------- a run through the client ----------
        # Another run holds the only slot, so the run this page sends is seen waiting.
        start_run(conn, user, store.new_conversation(MODEL, "")["id"], "hold the only slot")
        page.fill("#input", "answer for the browser check")
        page.press("#input", "Enter")

        page.wait_for_selector(".note.queue", timeout=8000)
        queue = page.evaluate("""() => {
          const line = document.querySelector('.note.queue');
          return { text: line.textContent, style: getComputedStyle(line).fontStyle };
        }""")
        check("a request that waits says its position in the queue", "position 1" in queue["text"], queue["text"])
        check("the queue line reads as a state, not as an explanation", queue["style"] == "normal", queue["style"])
        check("the sidebar row shows a spinner while this page's run is generating",
              page.evaluate("() => Boolean(document.querySelector('.row-state svg'))"))
        check("the send button is Stop while a run is active",
              page.evaluate("() => document.querySelector('#send').getAttribute('aria-label')") == "Stop generating")

        page.wait_for_selector(".msg.assistant .text", timeout=20000)
        answer = page.evaluate("() => document.querySelector('.msg.assistant .text').textContent")
        check("the answer arrives through the run's events", ANSWER in answer, answer[:70])
        check("the queue line goes away when the request starts",
              not page.evaluate("() => Boolean(document.querySelector('.note.queue'))"))
        check("the user message is shown once",
              page.evaluate("() => document.querySelectorAll('.msg.user').length") == 1)
        check("the button is Send again when the run ends",
              page.evaluate("() => document.querySelector('#send').getAttribute('aria-label')") == "Send")

        chat_id = page.evaluate("() => new URLSearchParams(location.search).get('c')")
        page.evaluate("([c, m]) => { window.__chat = c; window.__model = m; }", [chat_id, MODEL])

        # A run in another chat: its result is a dot until that chat is opened.
        other = store.new_conversation(MODEL, "")
        page.evaluate("(id) => { window.__other = id; }", other["id"])
        run_other = start_run(conn, user, other["id"], "answer in another chat")
        probe = lambda: page.evaluate("""async () => {
          const shell = await import('/static/js/shell.js');
          const data = await (await fetch('/api/conversations')).json();
          await shell.renderList();
          const row = document.querySelector(`.row-state[data-state="${window.__other}"]`);
          return { other: window.__other, api: data.conversations.map((c) => [c.id, c.unread]),
                   dot: Boolean(row && row.classList.contains('unread')), title: document.title,
                   rows: [...document.querySelectorAll('.side-list li')].map((li) => li.dataset.id + (li.classList.contains('active') ? '*' : '')) };
        }""")
        check("the run in the other chat finished", wait_for(lambda: runs.row(db.connect(), run_other.id)["status"] == "completed"))
        wait_for(lambda: probe()["dot"], 5.0)
        unread = probe()
        check("the dot appears for a chat that is not open, and the tab title with it",
              unread["dot"] and unread["title"].startswith("•"), str(unread)[:300])

        # ---------- the transcript for one turn ----------
        flushed = store.new_conversation(MODEL, "")
        start_run(conn, user, flushed["id"], "flush the transcript while it is writing")
        check("a partial answer is written before the run ends", wait_for(lambda: any(
            record.get("partial") for record in store.messages_raw(flushed["id"]) if record["role"] == "assistant")))
        check("a partial record is not the model failing: it carries no note", not any(
            record.get("note") for record in store.messages_raw(flushed["id"]) if record["role"] == "assistant"))
        check("the run's usage names the connection", wait_for(lambda: db.connect().execute(
            "SELECT connection_id FROM usage_events ORDER BY id DESC LIMIT 1").fetchone()["connection_id"]
            == connection["id"]))
        check("one record per turn, not one per flush", wait_for(lambda: len([
            record for record in store.messages_raw(flushed["id"]) if record["role"] == "assistant"]) == 1))

        # ---------- a dropped stream, and a reconnect ----------
        dropped = page.evaluate("""async () => {
          const runs = await import('/static/js/runs.js');
          const started = await runs.send(window.__chat, { message: 'drop the stream after one event', model: window.__model });
          const controller = new AbortController();
          let events = 0;
          try {
            await runs.follow(started.run_id, 0, (payload) => { events += 1; if (events === 1) controller.abort(); }, controller.signal);
          } catch (error) { /* the abort is the point */ }
          window.__run = started.run_id;
          return { run: started.run_id, events, status: (await runs.readRun(started.run_id)).run.status };
        }""")
        check("closing the stream does not cancel the run", dropped["status"] in ("queued", "running"), str(dropped))
        check("the run still finished after the stream was dropped",
              wait_for(lambda: (runs.row(db.connect(), dropped["run"]) or {}).get("status") == "completed"))

        replay = page.evaluate("""async () => {
          const runs = await import('/static/js/runs.js');
          const first = [];
          const result = await runs.follow(window.__run, 0, (payload, id) => first.push(id));
          const second = [];
          await runs.follow(window.__run, result.last, (payload, id) => second.push(id));
          return { count: first.length, again: second.length,
                   inOrder: first.every((id, index) => index === 0 || id > first[index - 1]) };
        }""")
        check("nothing is delivered twice on a reconnect",
              replay["count"] > 0 and replay["again"] == 0 and replay["inOrder"], str(replay))

        # ---------- what a reload shows ----------
        page.goto(BASE + "/?c=" + chat_id, wait_until="networkidle")
        page.wait_for_selector(".msg", timeout=8000)
        counts = page.evaluate("""() => ({ user: document.querySelectorAll('.msg.user').length,
          assistant: document.querySelectorAll('.msg.assistant').length,
          text: [...document.querySelectorAll('.msg.assistant .text')].map(n => n.textContent) })""")
        records = store.messages_raw(chat_id)
        asked = [record for record in records if record["role"] == "user"]
        answered = [record for record in records if record["role"] == "assistant"]
        check("a reload shows the transcript as it stands, one message per turn",
              counts["user"] == len(asked) and counts["assistant"] == len(answered),
              f"{counts['user']}/{counts['assistant']} shown against {len(asked)}/{len(answered)} records")
        check("the text shown is the transcript, in order, once per turn",
              counts["text"] == [(record["content"] or "") for record in answered],
              f"{str(counts['text'])[:120]} against {str([record['content'] for record in answered])[:120]}")
        state = page.evaluate("(id) => { const row = document.querySelector('.row-state[data-state=\"' + id + '\"]');"
                              "return { row: Boolean(row), dot: row ? row.classList.contains('unread') : null }; }", chat_id)
        check("the dot is gone for the chat that has just been read", state["row"] and not state["dot"], str(state))

        # ---------- a second message while a run is active ----------
        busy = store.new_conversation(MODEL, "")
        page.goto(BASE + "/?c=" + busy["id"], wait_until="networkidle")
        run_busy = start_run(conn, user, busy["id"], "hold a slot so this chat stays generating")
        page.fill("#input", "a second message")
        page.press("#input", "Enter")
        said = wait_for(lambda: any("still generating" in line for line in page.evaluate(
            "() => [...document.querySelectorAll('.note')].map(n => n.textContent)")))
        notes = page.evaluate("() => [...document.querySelectorAll('.note')].map(n => n.textContent)")
        kept = page.evaluate("() => document.querySelector('#input').value")
        check("a second message while a run is active says which chat and what to do", said,
              str(notes)[:140] + f" | run {run_busy.status}")
        check("the text is not lost when the message is refused", kept == "a second message", kept)

        # ---------- an interrupted turn ----------
        note = page.evaluate("""async () => {
          const chat = await import('/static/js/chat.js');
          const container = document.createElement('div');
          const view = new chat.ChatView(container, { showReasoning: false }, {});
          view.handle({ type: 'user_message', content: 'what was asked', run: 'r1' });
          view.handle({ type: 'run_status', status: 'interrupted', error: '' });
          return [...container.querySelectorAll('.note')].map(n => n.textContent);
        }""")
        check("an interrupted turn says what it is", any("Interrupted" in line for line in note), str(note)[:120])

        # ---------- the effective limit, in the user's own words ----------
        sources = page.evaluate("""async () => {
          const account = await import('/static/js/account.js');
          const usage = await account.readUsage();
          const block = account.usageBlock(usage, {});
          return [...block.querySelectorAll('.usage-row')].map((row) => ({
            value: row.querySelector('.value').textContent,
            source: row.querySelector('.source')?.textContent || '',
          }));
        }""")
        check("the usage bars show the limit that applies and where it comes from",
              any("5k" in row["value"] and "the global default" in row["source"] for row in sources), str(sources)[:200])

        page.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{SHOT_DIR}/browser-chat-dark.png")
        page.evaluate("() => document.documentElement.dataset.theme = 'light'")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{SHOT_DIR}/browser-chat-light.png")

        # ---------- a file the user attaches ----------
        upload_chat = store.new_conversation(MODEL, "")["id"]
        settings.set_value(conn, user, "attachment_inline_chars", 120)
        settings.set_value(conn, user, "max_upload_mb", 1)
        # A context smaller than the estimate is what makes the cost line warn.
        connections.update_model(conn, user, model_row["row_id"], {"supports_vision": True, "context_length": 12})
        # One model is marked as unable to read images, and one has nothing tested at all. The page shows
        # these differently: an image is refused for the first, and the admin view reads the second as unknown.
        no_vision = connections.add_model(conn, user, connection["id"], NO_VISION_ID,
                                          {"display_name": "No vision"})
        untested = connections.add_model(conn, user, connection["id"], UNTESTED_ID, {})
        connections.update_model(conn, user, no_vision["row_id"], {"supports_vision": False})
        conn.commit()

        # modelInfo reads the model list the page loaded, so this reload is what makes the flags known.
        page.goto(BASE + "/?c=" + upload_chat, wait_until="networkidle")

        short = write_file("note.txt", b"x" * 60)               # under the inline cap, so it is shown in full
        long_file = write_file("long.txt", b"y" * 400)         # over the cap, so only part of it is shown
        binary = write_file("program.txt", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff")
        over_cap = write_file("big.txt", b"z" * 1_500_000)
        picture = write_file("photo.png", png(512, 1024))
        inline = settings.number(conn, "attachment_inline_chars")

        def estimate_for(chars):
            """The number the app shows: what the model is actually sent, not the whole file."""
            return round(min(chars, inline) / usage.CHARS_PER_TOKEN) + usage.PER_MESSAGE_OVERHEAD

        def chips():
            return page.evaluate("""() => [...document.querySelectorAll('#chips .chip')].map((node) => ({
              name: node.querySelector('.name').textContent,
              details: node.querySelector('.size').textContent,
              error: node.querySelector('.chip-error')?.textContent || '',
              style: node.className,
              thumb: node.querySelector('.thumb img')?.getAttribute('src') || '',
            }))""")

        def cost_line():
            return page.evaluate("""() => {
              const line = document.querySelector('#chips .cost');
              return line ? { text: line.textContent, warned: line.classList.contains('over') } : null;
            }""")

        def clear_chips():
            page.evaluate("async () => { const files = await import('/static/js/attachments.js'); files.clear(); }")

        def pick(paths):
            clear_chips()
            page.set_input_files("#file-picker", paths)
            wait_for(lambda: chips())
            # A chip shows its progress while the file is being read; the reading is over when it stops.
            wait_for(lambda: chips() and all("%" not in c["details"] for c in chips()))

        # A progress bar only exists while a file is being read, so it is watched as it happens.
        page.evaluate("""() => {
          window.__bars = 0;
          new MutationObserver(() => { if (document.querySelector('#chips .progress')) window.__bars += 1; })
            .observe(document.getElementById('chips'), { childList: true, subtree: true });
        }""")

        pick([short])
        chip = chips()[0]
        check("the file input makes a chip with the name and the size",
              chip["name"] == "note.txt" and "60 B" in chip["details"], str(chip))
        check("the chip shows the estimate as an estimate, with the ~",
              f"~{estimate_for(60)} tokens" in chip["details"], chip["details"])
        check("the chip shows a progress bar while the file is being read", page.evaluate("() => window.__bars") > 0)
        line = cost_line()
        check("the cost line says what these attachments are estimated to cost",
              line and f"about {estimate_for(60)} tokens" in line["text"], str(line))
        check("the cost line warns when the estimate is past the model's context",
              line and line["warned"] and "more than this model's context" in line["text"], str(line))
        check("a file on its own is a message: Send is enabled with nothing typed",
              page.evaluate("() => !document.querySelector('#send').disabled"))

        # A file longer than the inline cap: the number shown is what the model is sent, not the whole file.
        pick([long_file])
        capped = chips()[0]
        whole_file = round(400 / usage.CHARS_PER_TOKEN) + usage.PER_MESSAGE_OVERHEAD
        check("the estimate is the part the model is sent, not the whole file",
              f"~{estimate_for(400)} tokens" in capped["details"] and whole_file > estimate_for(400)
              and f"~{whole_file} tokens" not in capped["details"], capped["details"])
        check("the cost line counts only what the model is sent",
              f"about {estimate_for(400)} tokens" in cost_line()["text"], cost_line()["text"])

        pick(picture)
        wait_for(lambda: any(c["thumb"] for c in chips()))
        image_chip = next(c for c in chips() if c["name"] == "photo.png")
        tiles = uploads.IMAGE_TOKEN_BASE + 2 * uploads.IMAGE_TOKENS_PER_TILE
        check("the estimate for an image is the tiles its dimensions make",
              f"~{tiles} tokens" in image_chip["details"], image_chip["details"])
        check("an image chip shows the picture, from the file's own endpoint",
              image_chip["thumb"].startswith("/api/uploads/") and image_chip["thumb"].endswith("/thumbnail"),
              image_chip["thumb"])
        wait_for(lambda: any(r["width"] == 512 for r in page.evaluate(
            """() => [...document.querySelectorAll('#chips .thumb img')].map((img) => ({
              src: img.getAttribute('src'), width: img.naturalWidth }))""")), 8.0)
        renders = page.evaluate("""() => [...document.querySelectorAll('#chips .thumb img')].map((img) => ({
          src: img.getAttribute('src'), width: img.naturalWidth }))""")
        check("the picture renders in the chip",
              any(r["src"] == image_chip["thumb"] and r["width"] == 512 for r in renders), str(renders)[:200])
        served = page.evaluate("""async (url) => {
          const response = await fetch(url);
          return { status: response.status, type: response.headers.get('content-type'),
                   disposition: response.headers.get('content-disposition'),
                   nosniff: response.headers.get('x-content-type-options') };
        }""", image_chip["thumb"])
        check("the file is served to its owner as an attachment, and is not sniffed",
              served["status"] == 200 and "attachment" in (served["disposition"] or "")
              and served["nosniff"] == "nosniff", str(served))
        page.screenshot(path=f"{SHOT_DIR}/browser-attachments-light.png")

        # Drag & drop is on the conversation, and paste is on the page.
        clear_chips()
        dropped = page.evaluate("""() => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([new Blob(['dropped on the chat'])], 'dropped.txt', { type: 'text/plain' }));
          const area = document.getElementById('main');
          area.dispatchEvent(new DragEvent('dragenter', { dataTransfer: transfer, bubbles: true }));
          const outline = { style: getComputedStyle(area).outlineStyle, width: getComputedStyle(area).outlineWidth,
                            files: transfer.files.length };
          area.dispatchEvent(new DragEvent('drop', { dataTransfer: transfer, bubbles: true }));
          return outline;
        }""")
        wait_for(lambda: any(c["name"] == "dropped.txt" for c in chips()))
        check("the chat shows an outline while a file is over it",
              dropped["style"] == "dashed" and dropped["width"] == "1px", str(dropped))
        check("dropping a file on the conversation attaches it",
              dropped["files"] == 1 and any(c["name"] == "dropped.txt" for c in chips()), str(dropped))

        clear_chips()
        pasted = page.evaluate("""() => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([new Blob(['pasted from the clipboard'])], 'pasted.txt', { type: 'text/plain' }));
          document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true }));
          return transfer.files.length;
        }""")
        wait_for(lambda: any(c["name"] == "pasted.txt" for c in chips()), 8.0)
        check("pasting a file attaches it",
              pasted == 1 and any(c["name"] == "pasted.txt" for c in chips()), str(pasted))

        # What the server refuses, and what it says. One file at a time, so the counts are its own.
        wait_for(lambda: chips() and all("%" not in c["details"] for c in chips()))
        used_before = uploads.used(db.connect(), user["id"])
        pick([binary])
        refused = chips()[0]
        check("the content decides the type, not the filename",
              "not accepted: txt" in refused["error"]
              and "Images, text files, PDF and docx are" in refused["error"], str(refused))
        check("a refused file leaves nothing on disk and is not counted",
              uploads.used(db.connect(), user["id"]) == used_before, str(uploads.used(db.connect(), user["id"])))

        used_before = uploads.used(db.connect(), user["id"])
        pick([over_cap])
        too_big = chips()[0]
        check("an oversized file is refused with the cap in the message",
              too_big["error"] == "Too large: the limit is 1 MB per file." and "error" in too_big["style"],
              str(too_big))
        check("the reading stops at the cap, so a refused file is not counted",
              uploads.used(db.connect(), user["id"]) == used_before, str(uploads.used(db.connect(), user["id"])))

        # A usable chip and a refused one, side by side.
        pick([short, binary])
        usable = page.evaluate("async () => { const files = await import('/static/js/attachments.js'); return files.attachments().map((item) => item.name); }")
        check("a refused file is not part of the message: only the usable file is referenced",
              usable == ["note.txt"], str(usable))
        check("the x on a chip removes that file and the chip with it",
              page.evaluate("""() => {
                const before = document.querySelectorAll('#chips .chip').length;
                document.querySelector('#chips .chip-close').click();
                return { before, after: document.querySelectorAll('#chips .chip').length };
              }""") == {"before": 2, "after": 1})
        check("nothing is left to send once the only usable file is removed",
              page.evaluate("async () => { const files = await import('/static/js/attachments.js'); return files.files(); }") == [])

        # A model that is not flagged for vision is told the file is there, and not sent the bytes.
        clear_chips()
        page.evaluate("(model) => import('/static/js/shell.js').then((shell) => shell.chooseModel(model))",
                      no_vision["namespaced"])
        page.set_input_files("#file-picker", picture)
        wait_for(lambda: any(c["error"] for c in chips()))
        blocked = next(c for c in chips() if c["error"])
        check("an image is refused for a model that cannot see images",
              "cannot see images" in blocked["error"] and "warn" in blocked["style"], str(blocked))
        check("a blocked attachment is not part of the message, so Send waits",
              page.evaluate("""async () => {
                const files = await import('/static/js/attachments.js');
                return files.files().length === 0 && document.querySelector('#send').disabled;
              }"""))
        page.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)
        page.screenshot(path=f"{SHOT_DIR}/browser-attachments-error-dark.png")
        page.evaluate("() => document.documentElement.dataset.theme = 'light'")
        page.wait_for_timeout(400)

        # A message with a file: the chip stays with the message, and the model is shown the file as data.
        pick([short])
        page.fill("#input", "what is in the short file?")
        page.press("#input", "Enter")
        page.wait_for_selector(".msg.user .attached", timeout=10000)
        page.wait_for_selector(".msg.assistant .text", timeout=20000)
        attached = page.evaluate("""() => [...document.querySelectorAll('.msg.user .attached .attached-item')].map((item) => ({
          name: item.querySelector('.attached-name').textContent,
          size: item.querySelector('.attached-size').textContent,
          href: item.getAttribute('href'),
        }))""")
        check("the attachment stays with the message it was sent with",
              len(attached) == 1 and attached[0]["name"] == "note.txt"
              and "60 B" in attached[0]["size"] and "/download" in attached[0]["href"], str(attached))
        first_turn = call_for("what is in the short file?")
        check("a file under the inline cap is inlined as an <attachment> block",
              '<attachment name="note.txt" type="text">' in shown(first_turn), shown(first_turn)[:400])
        check("the system prompt says an attachment is data, not instructions",
              "not instructions" in shown(first_turn), shown(first_turn)[:300])
        check("the read_file tool is not offered when the whole file is already shown",
              "read_file" not in [tool["function"]["name"] for tool in first_turn["tools"]],
              str([tool["function"]["name"] for tool in first_turn["tools"]]))

        pick([long_file])
        page.fill("#input", "read the rest of the long file")
        page.press("#input", "Enter")
        page.wait_for_selector(".msg.assistant .text", timeout=20000)
        wait_for(lambda: call_for("read the rest of the long file")["messages"], 8.0)
        second_turn = call_for("read the rest of the long file")
        check("the read_file tool is offered when a file is longer than the inline cap",
              "read_file" in [tool["function"]["name"] for tool in second_turn["tools"]],
              str([tool["function"]["name"] for tool in second_turn["tools"]]))
        check("a truncated block says the rest is only readable with the tool",
              "Use read_file with file_id" in shown(second_turn), shown(second_turn)[:400])
        sizes = page.evaluate("""() => [...document.querySelectorAll('.msg.user .attached .attached-size')].map((n) => n.textContent)""")
        check("the chip in the message says the file is longer than the inline cap",
              any("longer than the inline cap" in size for size in sizes), str(sizes))
        check("a later turn still sees the file that was attached earlier",
              '<attachment name="note.txt"' in shown(second_turn), shown(second_turn)[:400])
        records = [record for record in store.messages_raw(upload_chat) if record["role"] == "user"]
        check("the transcript stores attachment metadata, never the file content",
              all(record.get("attachments") and "base64" not in json.dumps(record) for record in records),
              str(records)[:200])

        # ---------- Settings → Storage ----------
        page.click("#user-menu")
        page.wait_for_selector("button[role=menuitem]", timeout=8000)
        page.click("button[role=menuitem]:has-text('Settings')")
        page.wait_for_selector(".dialog .storage-list .storage-row", timeout=10000)

        def dialog_section(title):
            return page.evaluate("""(title) => {
              const section = [...document.querySelectorAll('.dialog section')].find(
                (s) => s.querySelector('h3')?.textContent === title);
              return section ? { note: section.querySelector('p.muted')?.textContent || '',
                                 rows: [...section.querySelectorAll('.storage-row')].map((row) => ({
                                   name: row.querySelector('.storage-name').textContent,
                                   href: row.querySelector('.storage-name').getAttribute('href'),
                                   size: row.querySelector('.storage-size').textContent,
                                   button: row.querySelector('button')?.textContent || '',
                                 })) } : null;
            }""", title)

        storage = dialog_section("Storage")
        check("the Storage section states the caps as they are set",
              storage and "1 MB" in storage["note"] and "500 MB" in storage["note"]
              and "5 files" in storage["note"], str(storage)[:220])
        check("the Storage section lists the user's own files with a Delete",
              storage and all(row["button"] == "Delete" and "/download" in row["href"] for row in storage["rows"])
              and any(row["name"] == "photo.png" for row in storage["rows"]), str(storage)[:220])

        other = accounts.create_user(conn, "otherowner", "a long enough password", actor="check")
        conn.commit()
        foreign = uploads.create(db.connect(), other, "other.txt", None, [b"a file of another account"])
        refused_owner = page.evaluate("""async (id) => (await fetch(`/api/uploads/${id}/download`)).status""",
                                      foreign["id"])
        check("another account's file, including an admin's, reads as no such file",
              refused_owner == 404, str(refused_owner))

        target = storage["rows"][0]
        page.evaluate("""(href) => {
          const row = [...document.querySelectorAll('.dialog .storage-row')].find(
            (r) => r.querySelector('a').getAttribute('href') === href);
          row.querySelector('button').click();
        }""", target["href"])
        file_id = target["href"].split("/")[3]
        off_the_list = wait_for(lambda: page.evaluate(
            """(href) => ![...document.querySelectorAll('.dialog .storage-row a')]
                .some((a) => a.getAttribute('href') === href)""", target["href"]))
        off_the_disk = wait_for(lambda: not uploads.get_for(db.connect(), user["id"], file_id))
        check("Delete takes the file off the list and off the disk",
              off_the_list and off_the_disk,
              str({"row": off_the_list, "file": file_id, "still_recorded": bool(uploads.get_for(db.connect(), user["id"], file_id))}))

        refused_without_token = api_request(f"/api/conversations/{upload_chat}", method="DELETE",
                                            headers={"Cookie": f"session={LOGIN_TOKEN}"})
        check("a mutating request is refused without the CSRF token the page was given",
              refused_without_token[0] == 403, str(refused_without_token)[:200])
        removed = api_request(f"/api/conversations/{upload_chat}", method="DELETE")
        check("deleting a chat removes the files that were attached in it",
              removed[0] == 200 and not [row for row in uploads.list_for(db.connect(), user["id"])
                                         if row["conversation_id"] == upload_chat], str(removed)[:200])

        page.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)
        try:
            page.locator(".dialog").screenshot(path=f"{SHOT_DIR}/browser-storage-dark.png", timeout=8000)
        except Exception as error:
            print("storage screenshot:", str(error)[:200])

        # ---------- Settings → API keys ----------
        page.evaluate("""() => {
          const section = [...document.querySelectorAll('.dialog section')].find(
            (s) => s.querySelector('h3')?.textContent === 'API keys');
          section.querySelector('input[type=text]').value = 'the browser check';
          section.querySelector('button.small-button').click();
        }""")
        page.wait_for_selector(".shown-key:not([hidden])", timeout=10000)
        key_token = page.evaluate("() => document.querySelector('.shown-key').childNodes[0].textContent.trim()")
        check("a key is shown once, at creation, in the form a client sends",
              key_token.startswith("dsk-") and len(key_token) == 44
              and "only time" in page.evaluate("() => document.querySelector('.shown-key').textContent"),
              str(key_token))
        key_rows = page.evaluate("""() => [...document.querySelectorAll('.key-row')].map((row) => ({
          name: row.querySelector('.key-name').textContent,
          details: row.querySelector('.key-details').textContent,
          button: row.querySelector('button').textContent,
        }))""")
        check("the list shows a key by its name and its first 8 characters, not by the key itself",
              key_rows and key_rows[0]["name"] == "the browser check"
              and key_rows[0]["details"].startswith(f"{key_token[:12]}…")
              and "never used" in key_rows[0]["details"] and key_rows[0]["button"] == "Revoke", str(key_rows)[:200])
        how_to_use = page.evaluate("""() => {
          const section = [...document.querySelectorAll('.dialog section')].find(
            (s) => s.querySelector('h3')?.textContent === 'API keys');
          return section.querySelector('.how-to-use pre').textContent;
        }""")
        check("the How to use box gives the base URL this app answers on, with an example",
              f"{BASE}/api/v1/chat/completions" in how_to_use and "curl" in how_to_use, how_to_use[:200])
        page.evaluate("() => document.documentElement.dataset.theme = 'light'")
        page.wait_for_timeout(400)
        try:
            page.locator(".dialog").screenshot(path=f"{SHOT_DIR}/browser-api-keys-light.png", timeout=8000)
        except Exception as error:
            print("api keys screenshot:", str(error)[:200])

        # The key is used from here, as a client would use it, so the numbers below are real.
        # A hidden model is one this account may not use, so it must not appear in the list.
        connections.update_model(conn, user, untested["row_id"], {"hidden": True})
        conn.commit()
        status, listed, _ = api_request("/api/v1/models", headers={"Authorization": f"Bearer {key_token}"})
        check("the key works on the API and lists only the models its account may use",
              status == 200 and listed.get("object") == "list"
              and sorted(item["id"] for item in listed.get("data", [])) == sorted([MODEL, no_vision["namespaced"]]),
              str(listed)[:200])
        check("a model this account may not use is not in that list", UNTESTED_ID not in json.dumps(listed),
              str(listed)[:200])
        # Unhidden again, because the admin view check below reads what that row says.
        connections.update_model(conn, user, untested["row_id"], {"hidden": False})
        conn.commit()
        api_request("/api/v1/chat/completions", method="POST", headers={"Authorization": f"Bearer {key_token}"},
                    body={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        api_row = next((row for row in usage_rows() if row["source"] == "api"), None)
        check("a call through the API is recorded as the key used it", bool(api_row), str(api_row)[:200])
        check("what the engine did not count is recorded as an estimate, and says so",
              api_row and api_row["estimated"] == 1 and api_row["total_tokens"] == usage.estimate(
                  [{"role": "user", "content": "hi"}], "An answer through the API.")["total_tokens"],
              str(api_row)[:200])
        check("using a key is what records it",
              any(row["last_used"] for row in apikeys.list_for(db.connect(), user["id"])), str(key_rows)[:150])
        own_usage = page.evaluate("""async () => {
          const account = await import('/static/js/account.js');
          const usage = await account.readUsage();
          const block = account.usageBlock(usage, {});
          return [...block.querySelectorAll('p.muted')].map((p) => p.textContent);
        }""")
        check("the account's own usage view shows where the tokens in a window came from",
              any("From this window:" in line and "api" in line for line in own_usage), str(own_usage)[:250])
        check("it also says which kinds of call those numbers count",
              any("chat and api calls" in line for line in own_usage), str(own_usage)[:250])


        # A key on its own is not a session: this request carries no cookies at all.
        gate = api_request("/api/models", headers={"Cookie": "", "Authorization": f"Bearer {key_token}"})
        cookie = api_request("/api/v1/models", headers={"Cookie": f"session={LOGIN_TOKEN}"})
        check("a key is not a session, and a session is not a key",
              gate[0] == 401 and cookie[0] == 401
              and cookie[1].get("error", {}).get("type") == "authentication_error", f"{gate[0]} {cookie[0]}")
        bad = api_request("/api/v1/chat/completions", method="POST",
                          headers={"Authorization": "Bearer dsk-not-a-key"},
                          body={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        check("an unknown key is refused in the OpenAI error shape",
              bad[0] == 401 and bad[1].get("error", {}).get("code") == "invalid_api_key", str(bad[1])[:200])
        page.evaluate("""() => document.querySelector('.key-row button').click()""")
        wait_for(lambda: page.evaluate("() => document.querySelector('.key-row button').textContent") == "revoked")
        check("revoking in the page stops the key at once",
              api_request("/api/v1/models", headers={"Authorization": f"Bearer {key_token}"})[0] == 401,
              key_token[:12])
        page.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        page.wait_for_timeout(400)
        try:
            page.locator(".dialog").screenshot(path=f"{SHOT_DIR}/browser-api-keys-dark.png", timeout=8000)
        except Exception as error:
            print("api keys screenshot:", str(error)[:200])
        page.evaluate("() => document.documentElement.dataset.theme = 'light'")
        page.evaluate("() => document.querySelector('.dialog .close').click()")


        # ---------- the admin panel ----------
        # A held run and one waiting for the slot, so the connection card shows the queue as it is.
        start_run(conn, user, store.new_conversation(MODEL, "")["id"], "hold a slot for the admin view")
        waiting = start_run(conn, user, store.new_conversation(MODEL, "")["id"], "wait for the slot")
        admin = context.new_page()
        listen(admin)
        admin.goto(BASE + "/admin", wait_until="networkidle")
        admin.wait_for_selector("#runtime-settings .field", timeout=10000)
        fields = admin.evaluate("""() => [...document.querySelectorAll('#runtime-settings .field')].map((row) => ({
          label: row.querySelector('label').textContent,
          help: row.querySelector('.help')?.textContent || '',
          value: row.querySelector('input, select').value,
        }))""")
        names = [field["label"] for field in fields]
        wanted = ["Global daily limit", "Default chat model", "Runs at one time per account",
                  "Queue timeout, in seconds", "Allow API access", "Requests per minute per API key",
                  "Largest uploaded file, in MB", "Files per message", "Storage per account, in MB",
                  "Most characters of a file shown to the model"]
        check("the runtime settings grid holds the values an admin can change",
              all(name in names for name in wanted), str(names)[:320])
        check("a limit field says what an empty value means",
              any("gets this" in field["help"] for field in fields), str(fields)[:200])
        select = admin.evaluate("() => [...document.querySelectorAll('#runtime-settings select')].map((s) => [...s.querySelectorAll('optgroup')].map((g) => g.label))")
        check("both default-model selects are grouped by connection",
              len(select) == 2 and all(groups == ["Check engine"] for groups in select), str(select))

        effective = admin.evaluate("""() => [...document.querySelectorAll('#user-rows .effective')].map(p => p.textContent)""")
        check("the Users tab shows the limit that applies and where it comes from",
              any("the global default" in line for line in effective)
              and any("their own" in line for line in effective)
              and any("unlimited · set unlimited" in line for line in effective), str(effective)[:260])

        # ---------- one account's API access, from the admin side ----------
        admin_key, admin_token = apikeys.create(conn, other, "an admin-seen key")
        conn.commit()

        def row_menu(username):
            return admin.evaluate("""(name) => {
              const row = [...document.querySelectorAll('#user-rows tr')].find(
                (tr) => tr.querySelector('.name')?.textContent === name);
              row.querySelector('.row-menu').click();
              return [...document.querySelectorAll('.menu [role=menuitem]')].map((b) => b.textContent);
            }""", username)

        items = row_menu("otherowner")
        check("the menu for an account can turn its API access off, and lists its keys",
              "Turn off API access" in items and "Keys" in items, str(items)[:200])
        check("the key works while that account is allowed to use the API",
              api_request("/api/v1/models", headers={"Authorization": f"Bearer {admin_token}"})[0] == 200,
              str(admin_token[:12]))
        admin.evaluate("""() => [...document.querySelectorAll('.menu [role=menuitem]')].find(
            (b) => b.textContent === 'Turn off API access').click()""")
        wait_for(lambda: not db.connect().execute("SELECT api_access FROM users WHERE id = ?",
                                                  (other["id"],)).fetchone()["api_access"])
        check("turning an account off in the admin panel stops its keys at once",
              api_request("/api/v1/models", headers={"Authorization": f"Bearer {admin_token}"})[0] == 401,
              str(other["id"]))
        row_menu("otherowner")
        admin.evaluate("""() => [...document.querySelectorAll('.menu [role=menuitem]')].find(
            (b) => b.textContent === 'Turn on API access').click()""")
        wait_for(lambda: db.connect().execute("SELECT api_access FROM users WHERE id = ?",
                                              (other["id"],)).fetchone()["api_access"])
        row_menu("otherowner")
        admin.evaluate("""() => [...document.querySelectorAll('.menu [role=menuitem]')].find(
            (b) => b.textContent === 'Keys').click()""")
        admin.wait_for_selector(".dialog .key-row", timeout=10000)
        admin_view = admin.evaluate("""() => ({
          note: document.querySelector('.dialog p')?.textContent || '',
          rows: [...document.querySelectorAll('.dialog .key-row')].map((row) => ({
            name: row.querySelector('.key-name').textContent,
            details: row.querySelector('.key-details').textContent,
          })),
        })""")
        check("an admin can list another account's keys and revoke them, but cannot read them",
              "API access is on for this account" in admin_view["note"]
              and admin_view["rows"][0]["details"].startswith("dsk-")
              and "…" in admin_view["rows"][0]["details"], str(admin_view)[:200])
        admin.evaluate("() => document.querySelector('.dialog .key-row button').click()")
        wait_for(lambda: db.connect().execute("SELECT revoked FROM api_keys WHERE id = ?",
                                              (admin_key["id"],)).fetchone()["revoked"])
        check("revoke from the admin view stops that key, and the account is allowed again",
              api_request("/api/v1/models", headers={"Authorization": f"Bearer {admin_token}"})[0] == 401
              and db.connect().execute("SELECT api_access FROM users WHERE id = ?",
                                      (other["id"],)).fetchone()["api_access"], str(admin_key["id"]))
        admin.evaluate("() => document.querySelector('.dialog .close')?.click()")
        admin.wait_for_selector(".dialog", state="detached", timeout=8000)

        admin.click("#tab-connections")
        admin.wait_for_selector(".conn", timeout=10000)
        card = admin.evaluate("""() => {
          const row = document.querySelector('.conn');
          return {
            name: row.querySelector('.row-toggle .name').textContent,
            url: row.querySelector('.log-row-head .value').textContent,
            key: [...row.querySelectorAll('.conn-field')].map(f => [f.querySelector('.label').textContent, f.querySelector('.value').textContent]),
            checks: [...row.querySelectorAll('.check')].map(c => ({
              label: c.querySelector('.label').textContent,
              status: c.querySelector('.status').textContent,
              detail: c.querySelector('.detail')?.textContent || '',
            })),
            flags: [...row.querySelectorAll('.flag')].map(f => f.textContent),
            model: row.querySelector('.conn-models .name').textContent,
            queue: row.querySelector('.log-row-head .meta').textContent,
          };
        }""")
        check("the connection card shows the name and the URL",
              card["name"] == "Check engine" and card["url"] == "http://example.invalid",
              f"{card['name']} {card['url']}")
        check("a stored secret is write-only: only that it is set, and what it ends with",
              any(field[0] == "api key" and field[1] == "set, ends 1234" for field in card["key"]), str(card["key"])[:160])
        check("each check is reported on its own",
              len(card["checks"]) == 6
              and any(c["label"] == "Model list" and c["status"] == "ok" for c in card["checks"])
              and any(c["label"] == "Tool calls" and "tool call" in c["detail"] for c in card["checks"]),
              str(card["checks"])[:260])
        flag_rows = admin.evaluate("""() => [...document.querySelectorAll('.conn-models tr[data-model]')].map((row) => ({
          model: row.querySelector('.name').textContent,
          flags: [...row.querySelectorAll('.flag')].map((cell) => cell.textContent),
        }))""")
        unknown = next((row for row in flag_rows if row["model"].endswith(UNTESTED_ID)), None)
        tested = next((row for row in flag_rows if row["model"].endswith(MODEL_ID)), None)
        unchecked = next((row for row in flag_rows if row["model"].endswith(NO_VISION_ID)), None)
        check("a capability nobody has tested reads as unknown, not as no",
              unknown and all(flag == "?" for flag in unknown["flags"]), str(unknown))
        check("a capability the connection test confirmed reads as yes",
              tested and "yes" in tested["flags"], str(tested))
        check("a model marked as unable to read images reads as no, which is not the same as unknown",
              unchecked and unchecked["flags"][0] == "no" and "?" in unchecked["flags"], str(unchecked))
        check("a model is named with its connection",
              any(row["model"] == f"{connection['slug']}/{MODEL_ID}" for row in flag_rows), str(flag_rows)[:200])
        refresh = lambda: admin.evaluate("async () => { const c = await import('/static/js/connections.js'); await c.load(); return true; }")
        read_queue = lambda: admin.evaluate("() => document.querySelector('.conn .log-row-head .meta').textContent")
        queue_shown = wait_for(lambda: refresh() and read_queue() == "running 1/1, waiting 1")
        check("the live queue is shown as it is", queue_shown, read_queue())

        # ---------- the Usage tab, with its source filter ----------
        # A key was used earlier in this run, so the numbers here are a chat turn and an API call.
        admin.click("#tab-usage")
        admin.wait_for_selector("#source button", timeout=10000)
        tabs = admin.evaluate("() => [...document.querySelectorAll('#source button')].map((tab) => tab.textContent)")
        check("the usage view can be narrowed to one kind of call",
              tabs == ["All", "Chat", "Dreaming", "API"], str(tabs))
        legend = admin.evaluate("() => [...document.querySelectorAll('#chart-legend span')].map((item) => item.textContent)")
        check("the chart keeps the three kinds apart, in the legend and in the bars",
              legend == ["chat", "dreaming", "api"], str(legend))

        def read_totals():
            return admin.evaluate("""() => [...document.querySelectorAll('#totals .usage-row')].map((row) => ({
              label: row.querySelector('.label').textContent,
              value: row.querySelector('.value').textContent,
              tokens: Number((row.querySelector('.value').getAttribute('title') || '0').match(/\\d+/)?.[0] || 0),
              estimated: row.querySelector('.value').textContent.includes('~') }))""")

        def narrowed():
            rows = read_totals()
            api_row = next((row for row in rows if row["label"] == "API"), None)
            return bool(api_row) and api_row["tokens"] > 0 and all(
                row["tokens"] == 0 for row in rows if row["label"] in ("Chat", "Dreaming"))

        admin.evaluate("""() => [...document.querySelectorAll('#source button')].find((tab) => tab.textContent === 'API').click()""")
        check("the filter narrows the numbers to the calls that came through the API",
              wait_for(narrowed), str(read_totals())[:250])
        totals = read_totals()
        check("a number that is an estimate is shown as one, and a zero is not marked as one",
              any(row["estimated"] for row in totals if row["label"] == "API")
              and not any(row["estimated"] for row in totals if row["tokens"] == 0), str(totals)[:250])
        check("the estimate is explained as what it is: characters divided by four",
              "characters / 4" in admin.evaluate(
                  """() => document.querySelector('#totals .muted')?.textContent || ''"""),
              admin.evaluate("""() => document.querySelector('#totals .muted')?.textContent || ''""")[:200])
        drawn = admin.evaluate("() => [...document.querySelectorAll('#chart rect')].map((r) => r.getAttribute('class'))")
        check("the chart draws the api numbers as their own part of a bar",
              "api" in drawn, str(drawn)[:150])

        check("the key breakdown only appears for one account, not for all of them at once",
              admin.evaluate("() => document.querySelector('#key-breakdown').hidden"), str(drawn)[:120])
        admin.select_option("#user-filter", str(user["id"]))
        admin.wait_for_selector("#key-rows .key-row", timeout=10000)
        admin_key_rows = admin.evaluate("""() => [...document.querySelectorAll('#key-rows .key-row')].map((row) => ({
          name: row.querySelector('.key-name').textContent,
          details: row.querySelector('.key-details').textContent,
        }))""")
        check("one account's numbers can be broken down by which key was used",
              admin_key_rows and admin_key_rows[0]["name"] == "the browser check"
              and "requests" in admin_key_rows[0]["details"]
              and admin_key_rows[0]["details"].startswith("dsk-"), str(admin_key_rows)[:220])




        admin.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        admin.wait_for_timeout(400)
        admin.screenshot(path=f"{SHOT_DIR}/browser-admin-dark.png")
        try:
            admin.locator("#usage-view").screenshot(path=f"{SHOT_DIR}/browser-admin-usage-dark.png", timeout=8000)
        except Exception as error:
            print("usage screenshot:", str(error)[:200])
        try:
            admin.locator(".conn").screenshot(path=f"{SHOT_DIR}/browser-connections-dark.png", timeout=8000)
        except Exception as error:
            print("connection card screenshot:", str(error)[:200])
        admin.click("#tab-users")
        admin.wait_for_timeout(400)
        try:
            admin.locator("#runtime-settings").screenshot(path=f"{SHOT_DIR}/browser-runtime-settings-dark.png", timeout=8000)
        except Exception as error:
            print("runtime settings screenshot:", str(error)[:200])
        admin.click("#tab-usage")
        admin.wait_for_timeout(400)
        admin.evaluate("() => document.documentElement.dataset.theme = 'light'")
        admin.wait_for_timeout(400)
        admin.screenshot(path=f"{SHOT_DIR}/browser-admin-light.png")
        try:
            admin.locator("#usage-view").screenshot(path=f"{SHOT_DIR}/browser-admin-usage-light.png", timeout=8000)
        except Exception as error:
            print("usage screenshot:", str(error)[:200])
        admin.click("#tab-users")
        admin.wait_for_timeout(400)
        try:
            admin.locator("#runtime-settings").screenshot(path=f"{SHOT_DIR}/browser-runtime-settings-light.png", timeout=8000)
        except Exception as error:
            print("runtime settings screenshot:", str(error)[:200])

        script_errors = [error for error in errors if "example.invalid" not in error]
        check("no page failed while this ran", not script_errors, "; ".join(script_errors[:4]))

        browser.close()

    print()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
