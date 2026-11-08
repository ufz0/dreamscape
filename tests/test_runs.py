"""Chat runs: a turn is a background task, not a request.

The run must finish when the client that started it is gone, replay what it already produced,
and stop only when someone asks it to.

The tests that read a stream run the run on the current loop with `asyncio.create_task`, so the
consumer and the run are both live at the same time. The API tests use `finish()`, which polls the
run the app started on the module's own loop.
"""
import asyncio
import json

import app
import appdata
import connections
import db
import runs
import settings
import store
from conftest import create_run, delta, fake_upstream, finish, make_user
from test_isolation import client_for

ACTOR = {"username": "admin"}


def engine_for(conn, name="Runs"):
    """A connection and one model, so a run has something it is allowed to use."""
    created = connections.create(conn, ACTOR, {"name": name, "preset": "custom",
                                               "base_url": "http://example.invalid/v1"})
    model = connections.add_model(conn, ACTOR, created["id"], "m")
    settings.set_value(conn, ACTOR, "default_chat_model", model["namespaced"])
    conn.commit()
    return model["namespaced"]


def chat_for(conn, user, model):
    appdata.set_current(user["id"])
    appdata.current().ensure()
    return store.new_conversation(model, "")["id"]


def params(message, model):
    return {"message": message, "model": model, "system": "", "temperature": 0.7,
            "wiki_index": False, "web": False, "generate_titles": False}


def stream(chunks, delay=0.02):
    """A scripted stream, one chunk at a time, with a pause so a test can read part of it."""
    async def script(payload):
        for chunk in chunks:
            yield "data: " + json.dumps(chunk)
            if delay:
                await asyncio.sleep(delay)
        yield "data: [DONE]"

    return script


def frames(source):
    """Parse the SSE frames the app would write, keeping the event ids."""
    found = []
    for frame in source:
        event_id, payload = None, None
        for line in frame.split("\n"):
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        if event_id is not None:
            found.append((event_id, payload))
    return found


async def collect(source):
    return [frame async for frame in source]


def test_a_run_finishes_when_the_client_that_started_it_is_gone(conn, monkeypatch):
    """Nothing here is inside a request: the run is created, the client leaves, the turn completes."""
    user = make_user(conn, "runuser")
    model = engine_for(conn)
    cid = chat_for(conn, user, model)

    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="Half an answer."), delta(content=" The rest of it.")], delay=0)})

    run = create_run(conn, user, cid, params("hello", model))
    status = asyncio.run(runs.execute(run))

    assert status["status"] == "completed"
    appdata.set_current(user["id"])
    records = store.messages_raw(cid)
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert records[1]["content"] == "Half an answer. The rest of it."
    assert records[1]["run"] == run.id

    # The run row is the record, so a client that comes back later can read it.
    row = runs.row(conn, run.id)
    assert row["status"] == "completed" and row["conversation_id"] == cid


def test_replaying_after_a_drop_has_no_duplicates_and_no_gaps(conn, monkeypatch):
    """A client reads part of a stream, goes away, comes back with `after`."""
    user = make_user(conn, "dropuser")
    model = engine_for(conn, "Drop")
    cid = chat_for(conn, user, model)
    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="one"), delta(content="two"), delta(content="three")])})
    run = create_run(conn, user, cid, params("start", model))

    async def read():
        first = []
        queue = run.subscribe()
        task = asyncio.create_task(runs.execute(run))
        # The first client takes three events and then the connection drops.
        for _ in range(3):
            first.append(await queue.get())
        run.unsubscribe(queue)
        await task                                  # the run kept going with nobody watching

        # Re-attaching from the last event it saw must give the rest, once.
        second = frames(await collect(app.stream_run(run, first[-1]["id"])))
        return first, second

    first, second = asyncio.run(read())
    ids = [event["id"] for event in first] + [event[0] for event in second]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), "no event is sent twice"
    assert ids == list(range(min(ids), max(ids) + 1)), "nothing between them is missing"
    first_text = "".join(payload.get("content", "") for _, payload in
                         [(event["id"], event["payload"]) for event in first] if payload["type"] == "delta")
    second_text = "".join(payload.get("content", "") for _, payload in second if payload["type"] == "delta")
    assert first_text + second_text == "onetwothree", "everything the run produced is delivered once"
    assert second_text == "twothree", "the client that reconnects gets what it missed"
    assert run.status == "completed"


def test_the_partials_are_written_to_the_transcript_while_the_run_is_running(conn, monkeypatch):
    """The answer is not kept until the end: a crash loses at most one flush."""
    user = make_user(conn, "flushuser")
    model = engine_for(conn, "Flush")
    cid = chat_for(conn, user, model)
    monkeypatch.setattr(runs, "FLUSH_SECONDS", 0.0)      # so the test does not wait two seconds
    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="written while streaming"), delta(content=" and the end.")])})
    run = create_run(conn, user, cid, params("go", model))

    async def read():
        queue = run.subscribe()
        task = asyncio.create_task(runs.execute(run))
        await queue.get()                                   # the user message
        await queue.get()                                  # the first part
        appdata.set_current(user["id"])
        partial = [record for record in store.messages_raw(cid) if record["role"] == "assistant"]
        assert partial, "the partial answer is in the transcript before the turn ends"
        assert partial[-1]["content"] == "written while streaming" and partial[-1]["partial"] is True
        await task
        return store.messages_raw(cid)

    records = asyncio.run(read())
    assistant = [record for record in records if record["role"] == "assistant"]
    assert len(assistant) == 1, "one record per turn, not one per flush"
    assert assistant[0]["content"] == "written while streaming and the end."
    assert "partial" not in assistant[0], "the finished record is not a partial one"


def test_two_clients_reading_one_run_see_the_same_events(conn, monkeypatch):
    user = make_user(conn, "twousers")
    model = engine_for(conn, "Two")
    cid = chat_for(conn, user, model)
    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="shared"), delta(content="stream"), delta(finish_reason="stop")])})
    run = create_run(conn, user, cid, params("hello", model))

    async def read():
        task = asyncio.create_task(runs.execute(run))
        both = await asyncio.gather(collect(app.stream_run(run, 0)), collect(app.stream_run(run, 0)))
        await task
        return [frames(part) for part in both]

    first, second = asyncio.run(read())
    assert [event[0] for event in first] == [event[0] for event in second]
    text = "".join(payload.get("content", "") for _, payload in first if payload["type"] == "delta")
    assert text == "sharedstream"
    assert run.status == "completed"


def test_cancel_is_the_only_way_a_run_stops(conn, monkeypatch):
    """A dropped connection does not stop anything; the cancel route does."""
    user = make_user(conn, "canceluser")
    model = engine_for(conn, "Cancel")
    cid = chat_for(conn, user, model)
    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="first part"), delta(content="second part")] * 4)})
    run = create_run(conn, user, cid, params("go", model))

    async def read_then_cancel():
        queue = run.subscribe()
        task = asyncio.create_task(runs.execute(run))
        await queue.get()                                  # the user_message event
        await queue.get()                                  # the first part
        run.unsubscribe(queue)                            # the client goes away: nothing stops here
        runs.cancel(conn, run.id, user["id"])
        await task

    asyncio.run(read_then_cancel())
    assert run.status == "cancelled", "the run stops at the next event after the cancel"
    appdata.set_current(user["id"])
    record = [record for record in store.messages_raw(cid) if record["role"] == "assistant"][0]
    assert record["content"] == "first part", "what was produced is kept in the transcript"
    assert record["partial"] is True

    # A run that was never started is cancelled through the row alone.
    other = create_run(conn, user, cid, params("never started", model))
    assert runs.cancel(conn, other.id, user["id"])["status"] == "cancelled"
    # A cancel is only for the run's own user.
    assert runs.cancel(conn, run.id, 999) is None


def test_the_api_starts_a_run_and_refuses_a_second_message_while_it_is_active(conn, monkeypatch):
    user = make_user(conn, "apiuser")
    model = engine_for(conn, "Api")
    client = client_for(conn, user)

    fake_upstream(monkeypatch, {"stream_generator": stream(
        [delta(content="answer", finish_reason="stop")], delay=0.2)})
    created = client.post("/api/conversations", json={"model": model}).json()["conversation"]

    started = client.post(f"/api/conversations/{created['id']}/messages",
                          json={"message": "hello", "model": model})
    assert started.status_code == 202 and started.json()["run_id"]

    # A second message in the same chat is refused while the first turn is generating.
    busy = client.post(f"/api/conversations/{created['id']}/messages",
                       json={"message": "and then", "model": model})
    assert busy.status_code == 409, "the run is still generating"
    assert busy.status_code == 409 and busy.json()["run_id"] == started.json()["run_id"]

    run = finish(runs.get(started.json()["run_id"]))
    assert run.status == "completed"

    # Now the same message is accepted, and the Stop path is the cancel route.
    stopped = client.post(f"/api/runs/{run.id}/cancel").json()
    assert stopped["run"]["status"] == "completed", "a finished run is not changed by a cancel"

    accepted = client.post(f"/api/conversations/{created['id']}/messages",
                           json={"message": "and then", "model": model})
    assert accepted.status_code == 202
    finish(runs.get(accepted.json()["run_id"]))

    # Reading the conversation says which run is active and what the last one did.
    body = client.get(f"/api/conversations/{created['id']}/messages").json()
    assert body["active_run_id"] is None and body["last_run"]["status"] == "completed"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant", "user", "assistant"]

    # A run belongs to its user.
    other = client_for(conn, make_user(conn, "otheruser", role="admin"))
    assert other.get(f"/api/runs/{run.id}").status_code == 404
    assert client.get(f"/api/runs/{run.id}").json()["run"]["id"] == run.id

    # A model that no connection can serves falls back to the default, and the run says which was asked for.
    gone = client.post(f"/api/conversations/{created['id']}/messages",
                       json={"message": "hello", "model": "no-connection/model"})
    assert gone.status_code == 202
    fell_back = finish(runs.get(gone.json()["run_id"]))
    assert fell_back.status == "completed"
    assert fell_back.params["requested_model"] == "no-connection/model"
    assert fell_back.model == model, "the turn used the default model, not the one that is gone"


def test_the_sidebar_dot_reads_runs_that_have_not_been_seen(conn):
    user = make_user(conn, "dotuser")
    model = engine_for(conn, "Dot")
    cid = chat_for(conn, user, model)
    run = create_run(conn, user, cid, params("hello", model))
    conn.execute("UPDATE runs SET status = 'completed' WHERE id = ?", (run.id,))
    conn.commit()

    assert runs.unread_for(conn, user["id"]) == [cid]
    client = client_for(conn, user)
    assert [row["unread"] for row in client.get("/api/conversations").json()["conversations"]] == [True]

    # Opening the conversation marks it seen, and the active run is named for the spinner.
    body = client.get(f"/api/conversations/{cid}/messages").json()
    assert runs.unread_for(conn, user["id"]) == []
    assert body["active_run_id"] is None

    live = create_run(conn, user, cid, params("still going", model))
    assert client.get("/api/conversations").json()["conversations"][0]["active_run_id"] == live.id


def test_a_restart_marks_the_runs_it_did_not_finish(conn):
    """Rows left by a previous process. A run this process is still running is not touched."""
    user = make_user(conn, "restartuser")
    model = engine_for(conn, "Restart")
    cid = chat_for(conn, user, model)

    def insert(run_id, status):
        conn.execute("INSERT INTO runs (id, user_id, conversation_id, status, started) "
                     "VALUES (?, ?, ?, ?, ?)", (run_id, user["id"], cid, status, db.now()))
        conn.commit()

    insert("run-left", "running")
    insert("run-queued", "queued")
    insert("run-done", "completed")

    assert runs.mark_interrupted_at_startup() == 2
    assert runs.row(conn, "run-left")["status"] == "interrupted"
    assert runs.row(conn, "run-queued")["status"] == "interrupted"
    assert runs.row(conn, "run-done")["status"] == "completed", "a finished run is not changed"
    assert runs.mark_interrupted_at_startup() == 0, "nothing to mark a second time"

    # A run this process started is its loop's business, not the startup's.
    live = create_run(conn, user, cid, params("half a turn", model))
    assert runs.mark_interrupted_at_startup() == 0
    assert runs.row(conn, live.id)["status"] == "queued"


def test_the_number_of_runs_one_account_and_the_app_may_have_is_enforced(conn):
    user = make_user(conn, "capuser")
    model = engine_for(conn, "Cap")
    first_chat = chat_for(conn, user, model)
    second_chat = chat_for(conn, user, model)

    settings.set_value(conn, ACTOR, "max_runs_per_user", 2)
    one = create_run(conn, user, first_chat, params("one", model))
    two = create_run(conn, user, second_chat, params("two", model))
    try:
        create_run(conn, user, chat_for(conn, user, model), params("three", model))
        raise AssertionError("the third run should be refused")
    except ValueError as error:
        assert "maximum for one account" in str(error)

    # A finished run does not count against the cap.
    conn.execute("UPDATE runs SET status = 'completed' WHERE id = ?", (one.id,))
    conn.commit()
    settings.set_value(conn, ACTOR, "max_runs_per_user", 0)
    settings.set_value(conn, ACTOR, "max_runs_global", 1)
    try:
        create_run(conn, user, second_chat, params("another", model))
        raise AssertionError("the global cap should refuse this")
    except ValueError as error:
        assert "maximum for the app" in str(error)

    # No limit means no limit.
    settings.set_value(conn, ACTOR, "max_runs_global", 0)
    third = create_run(conn, user, chat_for(conn, user, model), params("free", model))
    assert third.status == "queued"


def test_deleting_a_chat_cancels_its_runs(conn):
    user = make_user(conn, "deleteuser")
    model = engine_for(conn, "Delete")
    cid = chat_for(conn, user, model)
    run = create_run(conn, user, cid, params("hello", model))
    client = client_for(conn, user)
    removed = client.delete(f"/api/conversations/{cid}").json()
    assert removed["ok"] and removed["cancelled_runs"] == 1
    assert runs.get(run.id).status == "cancelled"
    assert runs.row(conn, run.id)["status"] == "cancelled"


def test_the_partial_flush_and_the_buffer_are_bounded(conn, monkeypatch):
    """A long turn does not keep every event; a client that asks for something already dropped is told."""
    user = make_user(conn, "boundeduser")
    model = engine_for(conn, "Bounded")
    cid = chat_for(conn, user, model)
    run = create_run(conn, user, cid, params("long turn", model))
    monkeypatch.setattr(runs, "MAX_BUFFER", 8)

    fake_upstream(monkeypatch, {"stream_generator": stream([delta(content=str(n)) for n in range(20)],
                                                           delay=0)})
    asyncio.run(runs.execute(run))
    assert len(run.events) <= 8
    assert run.status == "completed"
    # The event ids are still sequential from the first event of the run.
    ids = [event["id"] for event in run.events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert run.first_event > 1, "the earliest events are no longer in the buffer"
    # The transcript is the record for a finished run, so a client reads that instead.
    appdata.set_current(user["id"])
    assistant = [record for record in store.messages_raw(cid) if record["role"] == "assistant"]
    assert assistant[0]["content"] == "".join(str(n) for n in range(20))
