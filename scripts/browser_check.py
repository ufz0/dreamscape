"""A headless-browser check of the parts that only show up in the browser.

The app is served from a throwaway data folder with one session and a fake upstream, so a run can
finish without a model server anywhere. Nothing here reaches the network.

What it checks: the grouped model picker, the run client (send, follow, the queue line, a dropped
stream, a reconnect with nothing delivered twice), the sidebar row state, the wording for a refused
second message and an interrupted turn, the effective limit in both places, the admin Connections
tab with its per-check rows and write-only secrets, and the runtime settings grid. Screenshots are
taken in both themes.

Run: .venv/bin/python scripts/browser_check.py

It needs `playwright` and a browser on this machine; neither is a dependency of the app. Screenshots
are written to a folder under the system temp directory, and the app runs against a throwaway data
folder, never the real `data/`.
"""
import asyncio
import glob
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time

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
import app as appmodule
import appdata
import connections
import db
import llm
import runs
import settings
import store
import uvicorn
from playwright.sync_api import sync_playwright

PORT = 8731
BASE = f"http://127.0.0.1:{PORT}"

problems = []

MODEL_ID = "check-model"
MODEL = ""
ANSWER = "An answer written by the fake upstream."

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
    return {"text": "ok", "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}


async def fake_stream(target, model, messages, temperature, tools=None, max_tokens=None, extra_body=None):
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


def main():
    global MODEL
    conn = db.connect()
    user = accounts.create_user(conn, "browsercheck", "a long enough password", role="admin", actor="check")
    accounts.create_user(conn, "inherits", "a long enough password", actor="check")
    accounts.create_user(conn, "ownlimit", "a long enough password", actor="check",
                         daily_limit=2000, weekly_limit=-1)
    settings.set_value(conn, user, "global_daily_limit", 5000)
    conn.commit()
    token = accounts.create_session(conn, user)
    conn.commit()
    appdata.set_current(user["id"])
    appdata.current().ensure()

    connection = connections.create(conn, user, {
        "preset": "custom", "name": "Check engine", "base_url": "http://example.invalid",
        "api_key": "sk-check-1234", "max_concurrent": 1,
    })
    MODEL = connections.add_model(conn, user, connection["id"], MODEL_ID,
                                  {"display_name": "Check model"})["namespaced"]
    asyncio.run(connections.test_connection(conn, connection["id"]))
    conn.commit()

    serve()

    with sync_playwright() as playwright:
        browser = browser_for(playwright)
        context = browser.new_context(viewport={"width": 1100, "height": 720})
        context.add_cookies([{"name": "session", "value": token, "url": BASE}])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text)
                if message.type == "error" and "Failed to load resource" not in message.text else None)
        page.on("response", lambda response: errors.append(f"{response.status} {response.url}")
                if response.status >= 400 and response.status != 409 else None)

        # ---------- the model picker names the connection ----------
        page.goto(BASE + "/", wait_until="networkidle")
        check("the page starts without a script error", not errors, "; ".join(errors[:3]))
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
        shown = probe()
        check("the dot appears for a chat that is not open, and the tab title with it",
              shown["dot"] and shown["title"].startswith("•"), str(shown)[:300])

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

        # ---------- the admin panel ----------
        # A held run and one waiting for the slot, so the connection card shows the queue as it is.
        start_run(conn, user, store.new_conversation(MODEL, "")["id"], "hold a slot for the admin view")
        waiting = start_run(conn, user, store.new_conversation(MODEL, "")["id"], "wait for the slot")
        admin = context.new_page()
        admin.goto(BASE + "/admin", wait_until="networkidle")
        admin.wait_for_selector("#runtime-settings .field", timeout=10000)
        fields = admin.evaluate("""() => [...document.querySelectorAll('#runtime-settings .field')].map((row) => ({
          label: row.querySelector('label').textContent,
          help: row.querySelector('.help')?.textContent || '',
          value: row.querySelector('input, select').value,
        }))""")
        names = [field["label"] for field in fields]
        wanted = ["Global daily limit", "Default chat model", "Runs at one time per account", "Queue timeout, in seconds"]
        check("the runtime settings grid holds the values an admin can change",
              all(name in names for name in wanted), str(names)[:220])
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
        check("a capability nobody has tested reads as unknown, not as no",
              card["flags"][0] == "?" and "yes" in card["flags"], str(card["flags"]))
        check("a model is named with its connection", card["model"] == f"{connection['slug']}/{MODEL_ID}", card["model"])
        refresh = lambda: admin.evaluate("async () => { const c = await import('/static/js/connections.js'); await c.load(); return true; }")
        read_queue = lambda: admin.evaluate("() => document.querySelector('.conn .log-row-head .meta').textContent")
        queue_shown = wait_for(lambda: refresh() and read_queue() == "running 1/1, waiting 1")
        check("the live queue is shown as it is", queue_shown, read_queue())

        admin.evaluate("() => document.documentElement.dataset.theme = 'dark'")
        admin.wait_for_timeout(400)
        admin.screenshot(path=f"{SHOT_DIR}/browser-admin-dark.png")
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
        admin.evaluate("() => document.documentElement.dataset.theme = 'light'")
        admin.wait_for_timeout(400)
        admin.screenshot(path=f"{SHOT_DIR}/browser-admin-light.png")
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
