"""Chat runs: a turn is a background task, not a request.

A run keeps running when the client goes away - the tab is closed, the phone locks, the network
drops. The HTTP request that started it only creates the run and reads its events; a dropped
connection never cancels anything, and coming back re-attaches to the same run.

Two parts:

- The runs table is the record: status, start, finish, error. It is what a client reads when it
  comes back and the run is no longer in memory.
- The events of a live run are kept in a bounded in-memory buffer with sequential ids, so a client
  can replay them with `after` or Last-Event-ID and continue where it stopped. A finished run is
  read from the transcript instead.

Runs execute on this module's own event loop, not on the loop of the request that started them, so
a run outlives the request. That is also what makes the tests honest: a run can be awaited with
asyncio.run while the TestClient's request has already returned.

The partial assistant message is written to the transcript every few seconds and finalized at the
end, so a crash loses at most a couple of seconds. Writing at the position of the current turn's
record (store.write_at) keeps the transcript as one record per turn rather than one per flush.
"""
import asyncio
import threading
import time

import accounts
import agent
import appdata
import connections
import db
import limits
import llm
import reasoning
import settings
import store
import titles
import uploads
import usage as usagelog

ACTIVE = ("queued", "running", "waiting_approval")
ACTIVE_TUPLE = "('queued','running','waiting_approval')"
STATUSES = ACTIVE + ("completed", "failed", "cancelled", "interrupted")
MAX_BUFFER = 2000          # events kept for replay; a long turn is a few hundred
FLUSH_SECONDS = 2.0

RUNS: dict[str, "Run"] = {}
LOCK = threading.RLock()
ACCEPTING = True

LOOP: asyncio.AbstractEventLoop | None = None
THREAD: threading.Thread | None = None


def ensure_loop() -> asyncio.AbstractEventLoop:
    """The loop runs on its own thread, so a run is not tied to any request."""
    global LOOP, THREAD
    if LOOP is None:
        LOOP = asyncio.new_event_loop()
        THREAD = threading.Thread(target=LOOP.run_forever, name="runs", daemon=True)
        THREAD.start()
    return LOOP


class Run:
    def __init__(self, run_id: str, user_id: int, conversation_id: str):
        self.id = run_id
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.status = "queued"
        self.started = db.now()
        self.finished = None
        self.error = ""
        self.params: dict = {}
        self.model = ""
        self.level = "medium"
        self.connection_id = None
        self.events: list[dict] = []
        self.first_event = 1            # ids below this were dropped from the buffer
        self.subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.cancel_requested = False
        self.task = None
        self.position = time.monotonic()
        self.last_flush = time.monotonic()

    # ---------- events ----------

    def emit(self, payload: dict) -> dict:
        with LOCK:
            record = {"id": len(self.events) + self.first_event, "payload": payload}
            self.events.append(record)
            if len(self.events) > MAX_BUFFER:
                dropped = len(self.events) - MAX_BUFFER
                self.events = self.events[dropped:]
                self.first_event += dropped
            for loop, queue in list(self.subscribers):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, record)
                except RuntimeError:
                    pass      # that subscriber's loop is gone; its handler removes it
        return record

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue()
        self.subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers = [pair for pair in self.subscribers if pair[1] is not queue]

    def events_from(self, after: int) -> list[dict]:
        return [record for record in self.events if record["id"] > after]

    def buffered(self) -> bool:
        return bool(self.events)

    # ---------- status ----------

    def status_row(self) -> dict:
        # The event range is in here because a client that reconnects has to know whether the
        # buffer can replay the whole turn: when it cannot, the transcript is what it has.
        return {"id": self.id, "user_id": self.user_id, "conversation_id": self.conversation_id,
                "status": self.status, "started": self.started, "finished": self.finished,
                "error": self.error, "model": self.model, "buffered": self.buffered(),
                "first_event": self.first_event, "last_event": self.last_event()}

    def last_event(self) -> int:
        return self.first_event + len(self.events) - 1 if self.events else self.first_event - 1

    def set_status(self, status: str, error: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown run status: {status}")
        self.status = status
        self.error = error or self.error
        self.finished = db.now() if status not in ACTIVE else None
        with db.transaction() as conn:
            conn.execute("UPDATE runs SET status = ?, finished = ?, error = ? WHERE id = ?",
                         (status, self.finished, self.error, self.id))
        self.emit({"type": "run_status", "run_id": self.id, "status": status, "error": self.error})

    def elapsed(self) -> float:
        return time.monotonic() - self.position

    def cancel(self) -> None:
        self.cancel_requested = True
        if self.status == "queued" and self.task is None:
            self.set_status("cancelled")


# ---------- creating and reading ----------

def new_id() -> str:
    return f"run-{time.time_ns():x}"


def create(conn, user: dict, conversation_id: str, params: dict) -> Run:
    """A run row plus the object. How many runs one account or the app may have is checked here."""
    if not ACCEPTING:
        raise ValueError("The app is stopping, so no new runs are accepted.")

    per_user = settings.number(conn, "max_runs_per_user")
    global_max = settings.number(conn, "max_runs_global")
    active_here = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND status IN {ACTIVE_TUPLE}",
        (user["id"],)).fetchone()["n"])
    active_total = int(conn.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE status IN {ACTIVE_TUPLE}").fetchone()["n"])
    if per_user and active_here >= per_user:
        raise ValueError(f"You have {active_here} runs already, which is the maximum for one account "
                         f"({per_user}). Wait for one to finish.")
    if global_max and active_total >= global_max:
        raise ValueError(f"{active_total} runs are running, which is the maximum for the app "
                         f"({global_max}). Try again in a moment.")

    run = Run(new_id(), user["id"], conversation_id)
    run.params = params
    conn.execute("INSERT INTO runs (id, user_id, conversation_id, status, started) VALUES (?, ?, ?, ?, ?)",
                 (run.id, user["id"], conversation_id, "queued", run.started))
    RUNS[run.id] = run
    return run


def start(run: Run) -> None:
    """Schedule the run on the module's loop, from any thread.

    `call_soon_threadsafe` rather than `create_task`: the loop runs on another thread, and a task
    added from this thread would sit in its ready queue while the loop waits in select.
    """
    loop = ensure_loop()

    def begin():
        run.task = asyncio.create_task(execute(run))

    loop.call_soon_threadsafe(begin)


def get(run_id: str) -> Run | None:
    return RUNS.get(run_id)


def row(conn, run_id: str) -> dict | None:
    stored = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if stored is None:
        return None
    run = RUNS.get(run_id)
    return (run.status_row() if run else dict(stored)) | {"seen": bool(stored["seen"])}


def active_for(conn, user_id: int, conversation_id: str) -> dict | None:
    """The run a client should attach to when it opens a conversation."""
    stored = conn.execute(
        f"SELECT * FROM runs WHERE conversation_id = ? AND user_id = ? AND status IN {ACTIVE_TUPLE} "
        "ORDER BY started DESC LIMIT 1", (conversation_id, user_id)).fetchone()
    if stored is None:
        return None
    return row(conn, stored["id"])


def last_for(conn, user_id: int, conversation_id: str) -> dict | None:
    """The most recent run for a conversation, active or not. This is what shows an interruption."""
    stored = conn.execute("SELECT * FROM runs WHERE conversation_id = ? AND user_id = ? "
                          "ORDER BY started DESC LIMIT 1", (conversation_id, user_id)).fetchone()
    return row(conn, stored["id"]) if stored else None


def unread_for(conn, user_id: int) -> list[str]:
    """Conversations with a finished run the user has not seen: the dot in the sidebar reads this."""
    rows = conn.execute("SELECT DISTINCT conversation_id FROM runs WHERE user_id = ? AND status IN "
                        "('completed','failed','cancelled','interrupted') AND seen = 0", (user_id,)).fetchall()
    return [entry["conversation_id"] for entry in rows]


def mark_seen(conn, user_id: int, conversation_id: str) -> None:
    conn.execute("UPDATE runs SET seen = 1 WHERE user_id = ? AND conversation_id = ?",
                 (user_id, conversation_id))


def cancel(conn, run_id: str, user_id: int) -> dict | None:
    """The only way a run stops on purpose. A dropped SSE connection never calls this."""
    stored = row(conn, run_id)
    if stored is None or stored["user_id"] != user_id:
        return None
    run = RUNS.get(run_id)
    if run is None:
        # Not in this process: the row is the truth, and the transcript keeps what was written.
        if stored["status"] in ACTIVE:
            with db.transaction() as transaction:
                transaction.execute("UPDATE runs SET status = 'cancelled', finished = ? WHERE id = ?",
                                    (db.now(), run_id))
            stored["status"] = "cancelled"
            stored["error"] = "Cancelled. The answer stops where the transcript ends."
        return stored
    run.cancel()
    return run.status_row()


def cancel_for(conn, target: str, kind: str) -> int:
    """Deleting a chat or a user cancels its runs first."""
    column = "conversation_id" if kind == "conversation" else "user_id"
    key = int(target) if kind == "user" else target
    rows = conn.execute(f"SELECT id FROM runs WHERE {column} = ? AND status IN {ACTIVE_TUPLE}",
                        (key,)).fetchall()
    for entry in rows:
        run = RUNS.get(entry["id"])
        if run:
            run.cancel()
    return len(rows)


def mark_interrupted_at_startup() -> int:
    """Runs this process did not finish. The partial message stays in the transcript.

    Only the rows: a run that is in memory belongs to this process, and its own loop knows whether
    it finished. This is what a restart leaves behind.
    """
    count = 0
    with db.transaction() as conn:
        rows = conn.execute(f"SELECT id FROM runs WHERE status IN {ACTIVE_TUPLE}").fetchall()
        for entry in rows:
            if entry["id"] in RUNS:
                continue
            conn.execute("UPDATE runs SET status = 'interrupted', finished = ?, error = ? WHERE id = ?",
                         (db.now(), "Interrupted by a server restart.", entry["id"]))
            count += 1
    return count


def shutdown(grace: int = 60) -> int:
    """Stop accepting runs, give the active ones `grace` seconds, then mark what is left."""
    global ACCEPTING
    ACCEPTING = False
    for run in RUNS.values():
        if run.status == "queued":
            run.cancel()

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        active = [run for run in RUNS.values() if run.status in ACTIVE]
        if not active:
            break
        time.sleep(0.5)

    finished = 0
    for run in RUNS.values():
        if run.status in ACTIVE:
            run.cancel()
            run.set_status("interrupted", "Stopped by the server before the answer was complete.")
        elif run.status in ("completed", "failed", "cancelled"):
            finished += 1
    return finished


# ---------- one run ----------

async def execute(run: Run) -> dict:
    """The whole turn: the tool loop, the usage rows, the title. Nothing here depends on a client."""
    appdata.set_current(run.user_id)
    params = run.params
    if run.cancel_requested:
        run.set_status("cancelled")
        return run.status_row()

    with db.transaction() as conn:
        user = accounts.get_user(conn, run.user_id)
        model = str(params.get("model") or "")
        resolved = connections.resolve(conn, model)
        if resolved is None:
            fallback = connections.default_model(conn)
            resolved = connections.resolve(conn, fallback)
            if resolved:
                run.params["requested_model"] = model
                model = fallback
        # With no connections in the database the .env values are still what the app uses, so a
        # script or a test that has none keeps working. Once connections exist, a model has to be on one.
        default = None if resolved else (llm.default_target() if not connections.list_connections(conn) else None)
        if resolved is None and default is None:
            run.set_status("failed", f"No model available: {model} is not on a connection that is enabled.")
            return run.status_row()
        if user is None:
            run.set_status("failed", "No account for this run.")
            return run.status_row()

    run.model = model
    run.connection_id = resolved["connection"]["id"] if resolved else None
    target = connections.target(resolved["connection"]) if resolved else default
    connection_map = (reasoning.layer(resolved["connection"].get("reasoning_map"),
                                      resolved["model"].get("reasoning_map")) if resolved else None)

    try:
        run.set_status("running")
        with db.transaction() as conn:
            blocked = limits.check(conn, user)
        if blocked:
            run.emit({"type": "limit_reached", **blocked})
            run.set_status("failed", blocked["message"])
            return run.status_row()

        files = [str(item) for item in (params.get("files") or [])]
        attachments = []
        with db.transaction() as conn:
            # The file may have been uploaded before this conversation existed, so the chat it belongs
            # to is recorded here: deleting the chat is what removes the file.
            for file_id in files:
                conn.execute("UPDATE uploads SET conversation_id = ? WHERE id = ? AND user_id = ?",
                             (run.conversation_id, file_id, run.user_id))
            attachments = uploads.metadata(conn, run.user_id, files)

        if not params.get("regenerate"):
            store.append(run.conversation_id, {"role": "user", "content": params.get("message") or "",
                                               "attachments": attachments, "run": run.id})
        run.emit({"type": "user_message", "content": params.get("message") or "",
                  "attachments": attachments, "run": run.id})

        mapping = reasoning.resolve(model, params.get("reasoning_effort") or "medium", connection_map)
        run.level = mapping["level"]
        body = connections.request_body(resolved, mapping.get("body")) if resolved else dict(mapping.get("body") or {})
        mapping = {**mapping, "body": body}

        with db.transaction() as conn:
            vision = bool(resolved and resolved["model"].get("supports_vision"))
            stored = store.messages(run.conversation_id)
            history = [uploads.model_message(conn, run.user_id, message, vision) for message in stored]

        # `model_message` turns the metadata into what the model sees, so these are read from the stored
        # records: whether any file is in this conversation, and whether one of them is longer than the
        # inline cap (which is the only case where read_file has something to page through).
        has_files = any(message.get("attachments") for message in stored)
        needs_reader = any(item.get("truncated") for message in stored
                           for item in message.get("attachments") or [])
        tools = agent.tools_for(bool(params.get("web")), needs_reader)

        step = {"content": "", "reasoning": "", "tool_calls": [], "started": time.monotonic()}
        turn_position = store.file_end(run.conversation_id)
        own: list[dict] = []          # this turn's records, in the order they happened
        current = None                # index in `own` of the record this step is writing

        def save(partial: bool = False, note: str | None = None) -> None:
            # The note belongs to the step, not to one write: a later flush must not lose it.
            note = note or step.get("note")
            entry = assistant_record(run, step, partial=partial, note=note)
            if not (entry.get("content") or entry.get("reasoning") or entry.get("tool_calls")):
                return
            nonlocal current
            if current is None:
                own.append(entry)
                current = len(own) - 1
            else:
                own[current] = entry    # the same step's record, with whatever came since
            store.write_tail(run.conversation_id, turn_position, own)

        save(partial=True)

        async for event in agent.run_loop(
                [{"role": "system", "content": agent.system_prompt(
                    params.get("system") or "", bool(params.get("wiki_index", True)),
                    mapping.get("system") or "", bool(params.get("web")), has_files)}] + history,
                model, float(params.get("temperature", 0.7)), mapping=mapping,
                on_usage=lambda block, estimated: record_usage(run, user, block, estimated),
                limit_check=lambda: limit_check(run, user), tools=tools, user_id=run.user_id,
                target=target, on_position=lambda position: run.emit(
                    {"type": "queued", "position": position, "connection": target.get("slug") or ""}),
                cancelled=lambda: run.cancel_requested):
            if run.cancel_requested:
                break
            if event["type"] == "delta":
                step["content"] += event["content"]
            elif event["type"] == "reasoning":
                step["reasoning"] += event["content"]
            elif event["type"] == "tool_calls":
                step["tool_calls"] = event["tool_calls"]
            elif event["type"] == "assistant_message":
                step["tool_calls"] = event.get("tool_calls") or []
                step["note"] = event.get("note")
                save()
            elif event["type"] == "tool":
                own.append({"ts": store.now(), "role": "tool", "tool_call_id": event.get("id"),
                            "name": event["name"], "arguments": event["arguments"],
                            "content": agent.tool_message(event["name"], event["result"]),
                            "run": run.id})
                current = None      # the next answer is a new record after this tool result
                store.write_tail(run.conversation_id, turn_position, own)
            run.emit(event)
            if time.monotonic() - run.last_flush >= FLUSH_SECONDS:
                save(partial=True)
                run.last_flush = time.monotonic()
            minutes = settings.number(db.connect(), "max_run_minutes")
            if minutes and run.elapsed() > minutes * 60:
                save(partial=True)
                run.set_status("interrupted", f"Run stopped after the maximum of {minutes} minutes.")
                return run.status_row()

        if run.status in ACTIVE:
            # Only a turn that stopped early has a partial record; a finished one is complete.
            save(partial=run.cancel_requested)
            run.set_status("cancelled" if run.cancel_requested else "completed")

        if run.status == "completed" and not run.cancel_requested and params.get("generate_titles"):
            result = await titles.generate(user, run.conversation_id, run.model, target=target,
                                          connection_id=run.connection_id)
            if result.get("generated"):
                run.emit({"type": "title", "title": result["title"], "conversation_id": run.conversation_id})

    except asyncio.CancelledError:
        save(partial=True)
        run.set_status("cancelled")
    except connections.QueueTimeout as error:
        save(partial=True)
        run.set_status("failed", str(error))
    except Exception as error:
        save(partial=True)
        run.set_status("failed", str(error))
    return run.status_row()


def assistant_record(run: Run, step: dict, partial: bool = False, note: str | None = None) -> dict:
    """A transcript record, in the same shape the rest of the app writes and the UI reads."""
    entry = {"ts": store.now(), "role": "assistant", "content": step["content"] or None, "run": run.id}
    if step["reasoning"]:
        entry["reasoning"] = step["reasoning"]
        entry["reasoning_ms"] = int((time.monotonic() - step["started"]) * 1000)
    entry["reasoning_effort"] = run.level
    if step["tool_calls"]:
        entry["tool_calls"] = step["tool_calls"]
    if note:
        entry["note"] = note
    if partial:
        entry["partial"] = True
    return entry


def record_usage(run: Run, user: dict, block: dict, estimated: bool) -> None:
    with db.transaction() as conn:
        usagelog.record(conn, user["id"], block, "chat", run.model, run.conversation_id,
                        estimated, connection_id=run.connection_id)


def limit_check(run: Run, user: dict) -> dict | None:
    """Checked again before every upstream call inside the loop, as before."""
    with db.transaction() as conn:
        return limits.check(conn, user)
