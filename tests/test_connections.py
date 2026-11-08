"""Connections against a fake upstream: the model list, the test checks, namespacing, the queue.

Nothing here reaches a real server; the answers are scripted in conftest.
"""
import asyncio
import json

import accounts
import connections
import llm
import settings
import store
from conftest import delta, fake_upstream, make_user, usage
from test_isolation import client_for

ACTOR = {"username": "admin"}


def add(conn, name="Lab", url="http://example.invalid/v1", **values):
    """A connection through the same functions the routes use.

    Committing is what makes it visible to the API: the routes work in their own transaction, so a
    row written here without a commit would not be seen by a request.
    """
    created = connections.create(conn, ACTOR, {"name": name, "preset": "custom", "base_url": url, **values})
    conn.commit()
    return created


def test_the_presets_fill_the_documented_defaults(conn):
    created = connections.create(conn, ACTOR, {"name": "Ollama", "preset": "ollama"})
    assert created["base_url"] == "http://localhost:11434/v1"
    assert created["path_models"] == "/models" and created["path_chat"] == "/chat/completions"
    assert created["max_concurrent"] == 2, "the default concurrency"
    assert created["timeout_seconds"] == 120

    for preset in ("strata", "llamacpp", "vllm", "lmstudio", "custom"):
        row = connections.create(conn, ACTOR, {"name": f"Preset {preset}", "preset": preset})
        assert row["base_url"].startswith("http"), f"{preset} has a default URL"
    try:
        connections.create(conn, ACTOR, {"name": "Nope", "preset": "not a preset"})
    except ValueError as error:
        assert "Unknown preset" in str(error)


def test_a_url_has_to_be_http_and_the_slug_is_url_safe(conn):
    try:
        connections.create(conn, ACTOR, {"name": "Bad", "preset": "custom", "base_url": "ftp://example.invalid"})
        raise AssertionError("the URL must be http or https")
    except ValueError as error:
        assert "http" in str(error)

    created = connections.create(conn, ACTOR, {"name": "My llama Server 2", "preset": "llamacpp"})
    assert created["slug"] == "my-llama-server-2"
    again = connections.create(conn, ACTOR, {"name": "My llama Server 2b", "preset": "llamacpp"})
    assert again["slug"] != created["slug"], "two connections cannot share a slug"
    assert all(connections.SLUG_RE.match(connections.slugify(value)) for value in ("Café ☕", "", "!!!"))


def test_model_ids_are_namespaced_and_do_not_collide(conn):
    first = add(conn, "Engine A")
    second = add(conn, "Engine B")
    connections.add_model(conn, ACTOR, first["id"], "gpt-4o")
    connections.add_model(conn, ACTOR, second["id"], "gpt-4o")

    rows = connections.list_connections(conn)
    ids = [model["namespaced"] for row in rows for model in row["models"]]
    assert len(set(ids)) == 2, "the same model name on two engines is two different models"
    assert "engine-a/gpt-4o" in ids and "engine-b/gpt-4o" in ids

    found = connections.model_for(conn, "engine-b/gpt-4o")
    assert found and found["connection"]["id"] == second["id"]
    # A model id that itself contains a slash still resolves to its connection.
    nested = connections.add_model(conn, ACTOR, first["id"], "team/open-model")
    resolved = connections.model_for(conn, nested["namespaced"])
    assert resolved and resolved["id"] == "team/open-model"


def test_a_conversation_with_a_model_that_is_gone_falls_back_to_the_default(conn):
    """The conversation keeps its namespaced id; the app says it is unavailable and uses the default."""
    import appdata

    user = make_user(conn, "fallbackuser")
    appdata.set_current(user["id"])
    appdata.current().ensure()

    engine = add(conn, "Fading")
    kept = connections.add_model(conn, ACTOR, engine["id"], "kept-model")
    gone = connections.add_model(conn, ACTOR, engine["id"], "gone-model")
    settings.set_value(conn, ACTOR, "default_chat_model", kept["namespaced"])
    row = store.new_conversation(gone["namespaced"], "")

    assert connections.resolve(conn, gone["namespaced"]) is not None
    connections.delete_model(conn, ACTOR, gone["row_id"])
    assert connections.resolve(conn, gone["namespaced"]) is None, "the model is no longer on any connection"
    assert store.get(row["id"])["model"] == gone["namespaced"], "the stored id is not rewritten behind the user"
    assert connections.default_model(conn) == kept["namespaced"], "the fallback is the default model"

    # A disabled connection makes its models unavailable too, without deleting anything.
    connections.update(conn, ACTOR, engine["id"], {"enabled": False})
    assert connections.resolve(conn, kept["namespaced"]) is None
    connections.update(conn, ACTOR, engine["id"], {"enabled": True})
    assert connections.resolve(conn, kept["namespaced"]) is not None
    store.delete(row["id"])


def test_the_api_never_returns_a_secret(conn):
    engine = add(conn, "Secret", api_key="sk-abcdef1234")
    connections.update(conn, ACTOR, engine["id"], {"headers": {"X-Key": "headersecret"}})

    shown = connections.get(conn, engine["id"])
    assert shown["api_key"] == {"set": True, "hint": "1234"}
    assert shown["headers"]["X-Key"] == {"set": True, "hint": "cret"}
    assert "abcdef" not in json.dumps(shown) and "headersecret" not in json.dumps(shown)

    # Sending an empty field leaves the stored value as it is, because the UI cannot show it.
    kept = connections.update(conn, ACTOR, engine["id"], {"api_key": "", "headers": {"X-Key": ""}})
    assert kept["api_key"]["set"] is True and kept["headers"]["X-Key"]["set"] is True
    assert connections.target(conn.execute("SELECT * FROM connections WHERE id = ?",
                                          (engine["id"],)).fetchone())["headers"]["X-Key"] == "headersecret"

    # Writing a new value replaces it.
    changed = connections.update(conn, ACTOR, engine["id"], {"api_key": "newsecret9999"})
    assert changed["api_key"]["hint"] == "9999"


def test_every_change_is_in_the_audit_log(conn):
    engine = add(conn, "Audited")
    connections.update(conn, ACTOR, engine["id"], {"max_concurrent": 4})
    connections.add_model(conn, ACTOR, engine["id"], "a-model")
    connections.delete(conn, ACTOR, engine["id"])
    entries = accounts.audit_log(conn, 20)
    actions = [entry["action"] for entry in entries]
    assert "connection_create" in actions and "connection_update" in actions and "connection_delete" in actions
    assert "model_add" in actions
    target = [entry for entry in entries if entry["action"] == "connection_update"][0]
    assert "max_concurrent" in target["detail"]
    assert "sk-" not in target["detail"] and "max_concurrent" in target["detail"]


def test_a_connection_that_is_the_default_cannot_be_deleted(conn):
    engine = add(conn, "Default")
    model = connections.add_model(conn, ACTOR, engine["id"], "m1")
    settings.set_value(conn, ACTOR, "default_chat_model", model["namespaced"])
    try:
        connections.delete(conn, ACTOR, engine["id"])
        raise AssertionError("refused while it is the default chat connection")
    except ValueError as error:
        assert "cannot be deleted" in str(error)

    settings.set_value(conn, ACTOR, "default_dream_model", model["namespaced"])
    settings.set_value(conn, ACTOR, "default_chat_model", "")
    try:
        connections.delete(conn, ACTOR, engine["id"])
        raise AssertionError("refused while it is the dreaming connection")
    except ValueError as error:
        assert "dreaming" in str(error)

    settings.set_value(conn, ACTOR, "default_dream_model", "")
    assert connections.delete(conn, ACTOR, engine["id"])["models_removed"] == 1


def test_hidden_models_and_admins_only_connections_are_not_shown(conn):
    admin = make_user(conn, "connadmin", role="admin")
    normal = make_user(conn, "connuser")
    client = client_for(conn, normal)

    shared = add(conn, "Shared")
    hidden = connections.add_model(conn, ACTOR, shared["id"], "visible-model")
    connections.update_model(conn, ACTOR, hidden["row_id"], {"hidden": False})
    secret = connections.add_model(conn, ACTOR, shared["id"], "hidden-model")
    connections.update_model(conn, ACTOR, secret["row_id"], {"hidden": True})

    private = add(conn, "Admins only")
    connections.update(conn, ACTOR, private["id"], {"visibility": "admins"})
    connections.add_model(conn, ACTOR, private["id"], "admin-model")
    conn.commit()

    data = client.get("/api/models").json()
    namespaced = [model for group in data["groups"] for model in group["models"]]
    assert [model["namespaced"] for model in namespaced] == ["shared/visible-model"]
    assert [entry["connection"]["name"] for entry in data["groups"]] == ["Shared"]

    # An admin sees their own connections.
    admin_client = client_for(conn, admin)
    admin_data = admin_client.get("/api/models").json()
    assert len(admin_data["groups"]) == 2


def test_the_test_connection_reports_each_check_on_its_own(conn, monkeypatch):
    engine = add(conn, "Testable")
    fake_upstream(monkeypatch, {
        "models": {"data": [{"id": "probe-model", "name": "Probe"}]},
        # The checks run in this order: plain chat, then stream, then tools, then reasoning.
        "chat": {"choices": [{"message": {"content": "ok", "reasoning_content": "thinking"}}],
                 "usage": {"total_tokens": 12}},
        "stream": [
            [delta(content="ok"), usage({"total_tokens": 9}), delta(finish_reason="stop")],
            [delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "probe",
                                                      "arguments": "{}"}}], finish_reason="tool_calls")],
            [delta(reasoning="thinking"), delta(content="ok", finish_reason="stop")],
        ],
    })
    result = asyncio.run(connections.test_connection(conn, engine["id"]))
    checks = result["test_result"]["checks"]
    for name in ("models", "chat", "stream", "tools", "usage", "reasoning"):
        assert checks[name]["status"] in ("ok", "partial", "none", "failed"), name
    assert checks["models"]["status"] == "ok" and checks["models"]["count"] == 1
    assert checks["stream"]["status"] == "ok"
    assert checks["tools"]["status"] == "ok", "the streamed tool call is assembled and reported"
    assert checks["usage"]["stream"] is True and checks["usage"]["non_stream"] is True
    assert checks["reasoning"]["fields"] == ["reasoning_content"], "the field names seen are recorded"
    assert result["last_test"]

    model = connections.model_for(conn, "testable/probe-model")
    assert model and model["supports_tools"] is True and model["supports_usage"] is True
    assert model["supports_reasoning"] is True

    # A connection that reports no models cannot be checked: each check says why, rather than failing.
    empty = add(conn, "Empty")
    fake_upstream(monkeypatch, {"models": {"data": []}})
    result = asyncio.run(connections.test_connection(conn, empty["id"]))
    checks = result["test_result"]["checks"]
    assert checks["models"]["status"] == "ok" and checks["models"]["count"] == 0
    for name in ("chat", "stream", "tools", "usage", "reasoning"):
        assert checks[name]["status"] == "skipped" and "no model" in checks[name]["error"], name


def test_a_test_does_not_override_a_flag_the_admin_set(conn, monkeypatch):
    engine = add(conn, "Manual")
    model = connections.add_model(conn, ACTOR, engine["id"], "m", {"display_name": "Kept name"})
    connections.update_model(conn, ACTOR, model["row_id"], {"supports_tools": False, "supports_reasoning": True})

    fake_upstream(monkeypatch, {
        "models": {"data": [{"id": "m", "name": "Renamed by the server"}]},
        "chat": {"choices": [{"message": {"content": "ok"}}]},
        "stream": [[delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "probe",
                                                      "arguments": "{}"}}], finish_reason="tool_calls")]],
    })
    asyncio.run(connections.test_connection(conn, engine["id"]))

    row = connections.model_for(conn, "manual/m")
    assert row["supports_tools"] is False, "a flag the admin chose is not overwritten"
    assert row["supports_reasoning"] is True
    # A value the admin typed for the display name is not replaced by the server's name.
    assert row["display_name"] == "Kept name"


def test_body_overrides_are_merged_into_every_chat_request(conn, monkeypatch):
    """The connection's fields, then the model's own, then the reasoning mapping for the turn."""
    engine = add(conn, "Overrides", body_overrides={"chat_template_kwargs": {"enable_thinking": False}})
    model = connections.add_model(conn, ACTOR, engine["id"], "m")
    connections.update_model(conn, ACTOR, model["row_id"], {"body_overrides": {"top_k": 5}})

    resolved = connections.resolve(conn, "overrides/m")
    body = connections.request_body(resolved, {"reasoning_effort": "low"})
    assert body == {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 5,
                    "reasoning_effort": "low"}

    upstream = fake_upstream(monkeypatch, {"chat": {"choices": [{"message": {"content": "ok"}}]}})
    asyncio.run(llm.complete(connections.target(resolved["connection"]), "overrides/m",
                            [{"role": "user", "content": "hi"}], 0.0, extra_body=body))
    sent = upstream.calls[-1][2]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["top_k"] == 5 and sent["reasoning_effort"] == "low"


def test_the_queue_limits_one_connection_to_its_max_concurrent_requests(conn):
    engine = add(conn, "Queued", max_concurrent=2)
    gate = connections.gate(engine)
    seen = []

    async def hold(name, seconds):
        async with gate.acquire(on_position=lambda position: seen.append((name, position))):
            seen.append((name, "running"))
            await asyncio.sleep(seconds)

    async def run():
        # Three at once on a limit of two: the third waits, and sees its position.
        await asyncio.gather(hold("a", 0.3), hold("b", 0.3), hold("c", 0.1))

    asyncio.run(run())
    started = [entry for entry in seen if entry[1] == 1]
    running = [entry for entry in seen if entry[1] == "running"]
    assert len(running) == 3
    assert started and started[0][0] == "c", "the third caller was told it was in position 1"
    assert gate.active == 0 and gate.position() == 0

    # FIFO: a caller that arrives first gets the slot first.
    order = []

    async def second_order():
        async def hold(name):
            async with gate.acquire():
                order.append(name)
                await asyncio.sleep(0.05)
        await asyncio.gather(hold("first"), hold("second"), hold("third"))

    asyncio.run(second_order())
    assert order == ["first", "second", "third"]


def test_the_queue_times_out_and_reports_the_timeout(conn):
    engine = add(conn, "Timeout", max_concurrent=1)
    settings.set_value(conn, ACTOR, "queue_timeout_seconds", 1)
    gate = connections.gate(engine)
    gate.timeout = 1.0

    async def run():
        async with gate.acquire():
            try:
                async with gate.acquire():
                    raise AssertionError("the second caller should not get a slot")
            except connections.QueueTimeout as error:
                return str(error)

    message = asyncio.run(run())
    assert "full for 1 seconds" in message
    assert gate.active == 0 and gate.position() == 0, "the timed-out caller is not left in the queue"


def test_first_start_creates_the_strata_connection_and_repoints_the_conversations(conn, monkeypatch):
    import appdata

    monkeypatch.setenv("STRATA_BASE_URL", "http://10.0.0.57:8080/v1")
    monkeypatch.setenv("STRATA_MODEL", "old-model")
    user = make_user(conn, "firstuser")
    appdata.set_current(user["id"])
    appdata.current().ensure()

    row = store.new_conversation("old-model", "")
    result = connections.bootstrap(conn)
    assert result["connection"]["name"] == "Strata"
    assert result["repointed"] == 1
    assert store.get(row["id"])["model"] == "strata/old-model", "existing conversations are re-pointed"

    # The settings point at the namespaced model now.
    assert settings.raw(conn, "default_chat_model") == "strata/old-model"
    assert connections.default_model(conn) == "strata/old-model"

    # Running it again changes nothing.
    assert connections.bootstrap(conn) is None
    assert store.get(row["id"])["model"] == "strata/old-model"


def test_usage_rows_name_the_connection_and_the_namespaced_model(conn):
    engine = add(conn, "Usage")
    model = connections.add_model(conn, ACTOR, engine["id"], "m1")
    import usage as usagelog
    from datetime import datetime, timezone

    user = make_user(conn, "usageuser")
    usagelog.record(conn, user["id"], {"total_tokens": 100}, "chat", model["namespaced"],
                   connection_id=engine["id"])
    row = conn.execute("SELECT * FROM usage_events").fetchone()
    assert row["connection_id"] == engine["id"]
    assert row["model"] == "usage/m1"

    # An old row, written before connections existed, stays as it is.
    conn.execute("INSERT INTO usage_events (user_id, timestamp, total_tokens, model, source, estimated) "
                 "VALUES (?, ?, 50, 'old-model', 'chat', 0)", (user["id"], datetime.now(timezone.utc).isoformat()))
    conn.commit()
    old = conn.execute("SELECT * FROM usage_events WHERE model = 'old-model'").fetchone()
    assert old["connection_id"] is None
    conn.execute("DELETE FROM connections WHERE id = ?", (engine["id"],))
    conn.commit()
    assert conn.execute("SELECT connection_id FROM usage_events WHERE model = 'usage/m1'").fetchone()["connection_id"] is None
