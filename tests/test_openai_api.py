"""The OpenAI-compatible API at /api/v1, tested with the official client.

The client is pointed at the app through the test client, so what is checked is the shape a client
reads: the model list, the completion object, the stream frames, and the error format. The upstream is
the same fake the other tests use, so nothing here reaches a real server.

Two things cannot be observed through the official client and are tested against the module instead: a
client that goes away mid-stream (the test client reads the whole response), and what the app sends
upstream when the client did not ask for usage.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import apikeys
import app
import connections
import limits
import openai_api
import settings
import usage
from conftest import add_usage, fake_upstream, make_user
from test_isolation import client_for as session_client_for

openai = pytest.importorskip("openai")

ACTOR = {"username": "admin"}
BASE = "http://testserver/api/v1"
MESSAGES = [{"role": "user", "content": "hi"}]


def engine(conn, name, model_id="m", connection=None, model=None):
    """A connection with one model, so a request has something it is allowed to use."""
    created = connections.create(conn, ACTOR, {"name": name, "preset": "custom",
                                               "base_url": "http://example.invalid/v1",
                                               **(connection or {})})
    row = connections.add_model(conn, ACTOR, created["id"], model_id, model or {})
    # What the API reads is its own connection, so the rows have to be committed to be seen.
    conn.commit()
    return created, row


def key_for(conn, user, name="test key", expires=""):
    key, token = apikeys.create(conn, user, name, expires)
    conn.commit()
    return key, token


def client_for(token):
    """The official client, pointed at this app through the test client."""
    return openai.OpenAI(api_key=token, base_url=BASE, http_client=TestClient(app.app))


def completion(usage_block=None, message=None, content="hello"):
    body = message or {"role": "assistant", "content": content}
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": body, "finish_reason": "stop"}],
            "usage": usage_block or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def chunk(content=None, usage_block=None, tool_calls=None):
    body = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    if content is not None or tool_calls:
        delta = {}
        if content is not None:
            delta["content"] = content
        if tool_calls:
            delta["tool_calls"] = tool_calls
        body["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
    if usage_block:
        body["choices"] = []
        body["usage"] = usage_block
    return body


def usage_rows(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM usage_events ORDER BY id")]


def sent(upstream):
    """The body the last request put on the wire."""
    return upstream.calls[-1][2]


# ---------- the model list ----------

def test_the_model_list_is_the_openai_format_and_only_what_the_key_can_use(conn):
    admin = make_user(conn, "apiadmin", role="admin")
    normal = make_user(conn, "apinormal")

    connection, visible = engine(conn, "Public", "visible")
    hidden = connections.add_model(conn, ACTOR, connection["id"], "hidden-model", {"hidden": True})
    _, private = engine(conn, "Admins only", "private", connection={"visibility": "admins"})
    _, off = engine(conn, "Off", "disabled", connection={"enabled": False})

    _, token = key_for(conn, normal)
    listed = client_for(token).models.list()
    assert listed.object == "list"
    assert [item.id for item in listed.data] == [visible["namespaced"]], \
        "a hidden model, an admins-only connection and a disabled one are not shown to a normal account"
    entry = listed.data[0]
    assert entry.object == "model" and entry.owned_by == normal["username"]
    assert entry.created > 0, "the list format wants a unix timestamp, not the stored ISO string"
    assert hidden["namespaced"] == "public/hidden-model"

    # The same question as an admin answers differently, and the ids stay namespaced.
    _, admin_token = key_for(conn, admin)
    assert sorted(item.id for item in client_for(admin_token).models.list().data) == \
        sorted([private["namespaced"], visible["namespaced"]])


def test_a_key_is_accepted_only_on_the_api_and_a_cookie_only_on_the_ui(conn, monkeypatch):
    user = make_user(conn, "gateuser")
    _, model = engine(conn, "Gate", "m")
    _, token = key_for(conn, user)
    fake_upstream(monkeypatch, {"chat": completion()})

    plain = TestClient(app.app)
    plain.headers["Authorization"] = f"Bearer {token}"
    assert plain.get("/api/models").status_code == 401, "a key is not a session"

    session = session_client_for(conn, user)
    response = session.get("/api/v1/models")
    assert response.status_code == 401, "a cookie is not a key"
    assert response.json()["error"]["type"] == "authentication_error"

    # A state-changing call here needs no CSRF header, because there is no cookie to echo back.
    answer = client_for(token).chat.completions.create(model=model["namespaced"], messages=MESSAGES)
    assert answer.choices[0].message.content == "hello"


def test_a_bad_revoked_or_expired_key_is_refused(conn, monkeypatch):
    user = make_user(conn, "badkeyuser")
    _, model = engine(conn, "Badkey", "m")
    fake_upstream(monkeypatch, {"chat": completion()})
    client = TestClient(app.app)

    def refused(token):
        return client.post("/api/v1/chat/completions", json={"model": model["namespaced"],
                                                              "messages": MESSAGES},
                           headers={"Authorization": f"Bearer {token}"})

    assert refused("dsk-not-a-key").status_code == 401
    key, token = key_for(conn, user)
    assert refused(token).status_code == 200
    assert apikeys.get(conn, key["id"])["last_used"], "using a key is what records it"

    apikeys.revoke(conn, key["id"], user)
    conn.commit()
    assert refused(token).status_code == 401, "a revoked key stops working at once"

    _, expired = key_for(conn, user, "old key", "2020-01-01")
    assert refused(expired).status_code == 401

    # A disabled account's key stops working without anything being changed on the key.
    import accounts
    accounts.set_status(conn, ACTOR, user["id"], "disabled")
    conn.commit()
    _, token = key_for(conn, user, "another")
    assert refused(token).status_code == 401

    body = refused("dsk-not-a-key").json()
    assert body["error"]["type"] == "authentication_error" and body["error"]["code"] == "invalid_api_key"


def test_the_global_and_the_per_account_api_switch_are_checked_at_use(conn, monkeypatch):
    user = make_user(conn, "switchuser")
    _, model = engine(conn, "Switch", "m")
    fake_upstream(monkeypatch, {"chat": completion()})
    _, token = key_for(conn, user)

    def answer():
        return TestClient(app.app).post("/api/v1/chat/completions",
                                        json={"model": model["namespaced"], "messages": MESSAGES},
                                        headers={"Authorization": f"Bearer {token}"})

    assert answer().status_code == 200, "both are on by default"

    settings.set_value(conn, ACTOR, "allow_api", False)
    conn.commit()
    response = answer()
    assert response.status_code == 401 and response.json()["error"]["type"] == "authentication_error"

    settings.set_value(conn, ACTOR, "allow_api", True)
    import accounts
    accounts.set_api_access(conn, ACTOR, user["id"], False)
    conn.commit()
    assert answer().status_code == 401, "the account is off even though the app is on"

    # The keys of a disabled account are listed for its own admin view, and revoking is audited.
    import apikeys as keys
    assert keys.list_for(conn, user["id"]) and keys.revoke(conn, keys.list_for(conn, user["id"])[0]["id"], ACTOR)


# ---------- a completion ----------

def test_the_body_is_passed_through_and_the_answer_is_recorded_as_the_key_used_it(conn, monkeypatch):
    user = make_user(conn, "calluser")
    _, model = engine(conn, "Passthrough", "m")
    _, token = key_for(conn, user)
    upstream = fake_upstream(monkeypatch, {"chat": completion()})

    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    answer = client_for(token).chat.completions.create(
        model=model["namespaced"], messages=MESSAGES, tools=tools, tool_choice="auto",
        temperature=0.2, response_format={"type": "json_object"})

    body = sent(upstream)
    assert body["model"] == "m", "the engine gets the id it knows, not the namespaced one"
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["temperature"] == 0.2 and body["response_format"] == {"type": "json_object"}
    assert body["messages"] == MESSAGES

    # What the client asked for is what it gets back, not what the engine calls the model.
    assert answer.model == model["namespaced"]
    assert answer.usage.total_tokens == 15

    row = usage_rows(conn)[0]
    assert row["source"] == "api" and row["key_id"] and not row["estimated"]
    assert row["model"] == model["namespaced"] and row["total_tokens"] == 15


def test_tool_calls_are_returned_as_the_upstream_sent_them(conn, monkeypatch):
    """Nothing here runs a tool: an API call is one request, and the answer is what the engine sent."""
    user = make_user(conn, "tooluser")
    _, model = engine(conn, "Tools", "m")
    _, token = key_for(conn, user)
    answer_body = completion({"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}, message={
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "lookup", "arguments": "{\"q\": \"x\"}"}}]})
    upstream = fake_upstream(monkeypatch, {"chat": answer_body})
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    answer = client_for(token).chat.completions.create(model=model["namespaced"], messages=MESSAGES,
                                                        tools=tools)
    call = answer.choices[0].message.tool_calls[0]
    assert call.id == "call-1" and call.function.name == "lookup"
    assert call.function.arguments == '{"q": "x"}'
    assert sent(upstream)["tools"] == tools
    assert usage_rows(conn)[0]["source"] == "api", "a tool turn is a call like any other"


def test_image_and_content_parts_reach_the_engine_unchanged(conn, monkeypatch):
    user = make_user(conn, "imageuser")
    _, model = engine(conn, "Images", "m")
    _, token = key_for(conn, user)
    upstream = fake_upstream(monkeypatch, {"chat": completion()})
    parts = [{"type": "text", "text": "what is in this?"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    client_for(token).chat.completions.create(model=model["namespaced"],
                                              messages=[{"role": "user", "content": parts}])
    assert sent(upstream)["messages"][0]["content"] == parts


def test_an_answer_without_usage_is_recorded_as_an_estimate(conn, monkeypatch):
    user = make_user(conn, "estimateuser")
    _, model = engine(conn, "NoUsage", "m")
    _, token = key_for(conn, user)
    no_usage = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "some answer text"},
                             "finish_reason": "stop"}]}
    fake_upstream(monkeypatch, {"chat": no_usage})
    answer = client_for(token).chat.completions.create(model=model["namespaced"], messages=MESSAGES)
    assert answer.usage is None, "the client gets what the engine gave, which is nothing"

    row = usage_rows(conn)[0]
    assert row["estimated"] == 1 and row["source"] == "api"
    assert row["total_tokens"] == usage.estimate(MESSAGES, "some answer text")["total_tokens"]


def test_the_reasoning_mapping_is_applied_when_the_client_sends_reasoning_effort(conn, monkeypatch):
    user = make_user(conn, "reasonuser")
    _, model = engine(conn, "Reasoning", "m",
                      connection={"reasoning_map": {"levels": {"high": {"body": {"thinking_budget": 4000}}}}})
    _, token = key_for(conn, user)
    upstream = fake_upstream(monkeypatch, {"chat": completion()})
    client = client_for(token)

    client.chat.completions.create(model=model["namespaced"], messages=MESSAGES, reasoning_effort="high")
    body = sent(upstream)
    assert body["reasoning_effort"] == "high", "the client's own value is still sent"
    assert body["thinking_budget"] == 4000, "and the mapping for this engine is applied on top"

    # Without reasoning_effort there is nothing to map, so the body is passed through as it came.
    client.chat.completions.create(model=model["namespaced"], messages=MESSAGES)
    assert "thinking_budget" not in sent(upstream)


def test_the_connection_path_and_body_overrides_are_applied(conn, monkeypatch):
    user = make_user(conn, "pathuser")
    _, model = engine(conn, "Pathed", "m", connection={"path_chat": "/custom/chat",
                                                        "body_overrides": {"temperature": 0.9}})
    _, token = key_for(conn, user)
    upstream = fake_upstream(monkeypatch, {"chat": completion()})
    client_for(token).chat.completions.create(model=model["namespaced"], messages=MESSAGES, temperature=0.1)
    assert upstream.calls[-1][1] == "/custom/chat", "the path this connection uses"
    assert sent(upstream)["temperature"] == 9e-1, "the connection's override wins over the client's"


# ---------- streaming ----------

def test_a_stream_is_forwarded_and_usage_is_always_asked_upstream(conn, monkeypatch):
    user = make_user(conn, "streamuser")
    _, model = engine(conn, "Stream", "m")
    _, token = key_for(conn, user)
    script = {"stream": [[chunk("part one"), chunk("part two"),
                          chunk(None, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})]] * 2}
    upstream = fake_upstream(monkeypatch, script)
    client = client_for(token)

    frames = list(client.chat.completions.create(model=model["namespaced"], messages=MESSAGES,
                                                 stream=True, stream_options={"include_usage": True}))
    assert [frame.choices[0].delta.content for frame in frames if frame.choices] == ["part one", "part two"]
    assert frames[-1].usage.total_tokens == 5, "the client asked for usage, so it gets it"
    assert frames[0].model == model["namespaced"]
    assert sent(upstream)["stream_options"]["include_usage"] is True

    row = usage_rows(conn)[0]
    assert row["source"] == "api" and row["key_id"] and row["total_tokens"] == 5 and not row["estimated"]

    # A client that did not ask for the usage chunk still causes it to be requested upstream, and does not see it.
    frames = list(client.chat.completions.create(model=model["namespaced"], messages=MESSAGES, stream=True))
    assert all(not getattr(frame, "usage", None) for frame in frames), "the extra chunk is stripped"
    assert sent(upstream)["stream_options"]["include_usage"] is True
    assert usage_rows(conn)[0]["total_tokens"] == 5, "and the tokens are still recorded"


def test_a_client_that_goes_away_mid_stream_records_what_was_counted(conn, monkeypatch):
    """The intended difference from a chat run: an API call is the client's, so it stops with it."""
    user = make_user(conn, "disconnectuser")
    connection, model = engine(conn, "Disconnect", "m")
    key, token = key_for(conn, user)
    key = apikeys.authenticate(token)      # the same shape the route builds
    fake_upstream(monkeypatch, {"stream_generator": stream_generator})
    apikeys.hold(user["id"])

    class Gone:
        """A request that says it is gone after the first line, which is what the test client cannot show."""

        def __init__(self):
            self.checked = 0

        async def is_disconnected(self):
            self.checked += 1
            return self.checked > 1

    async def read():
        frames = []
        async for frame in openai_api.stream(Gone(), key, model["namespaced"],
                                            connections.target(connection),
                                            {"model": "m", "messages": MESSAGES}, MESSAGES, True):
            frames.append(frame)
        return frames

    frames = asyncio.run(read())
    assert len(frames) == 1, "the first part was sent, then the client was gone"
    row = usage_rows(conn)[0]
    assert row["source"] == "api" and row["key_id"] and row["estimated"] == 1
    assert row["total_tokens"] == usage.estimate(MESSAGES, "part one")["total_tokens"]
    assert apikeys._running.get(str(user["id"])) == 0, "the slot is released either way"


async def stream_generator(payload):
    for line in [chunk("part one"), chunk("part two"),
                 chunk(None, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})]:
        yield "data: " + json.dumps(line)
    yield "data: [DONE]"


# ---------- the load guard ----------

def test_the_per_key_rate_limit_and_the_per_account_concurrent_cap(conn, monkeypatch):
    user = make_user(conn, "ratelimituser")
    _, model = engine(conn, "Rate", "m")
    fake_upstream(monkeypatch, {"chat": completion()})
    _, token = key_for(conn, user)
    settings.set_value(conn, ACTOR, "api_rate_limit_per_minute", "1")
    conn.commit()

    client = TestClient(app.app)
    headers = {"Authorization": f"Bearer {token}"}
    def call(key):
        return client.post("/api/v1/chat/completions", json={"model": model["namespaced"],
                                                             "messages": MESSAGES},
                           headers={"Authorization": f"Bearer {key}"})

    assert call(token).status_code == 200
    second = call(token)
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_error"
    assert int(second.headers["retry-after"]) >= 1
    assert "1 requests per minute" in second.json()["error"]["message"], "the limit that actually applies"

    # Another key of the same account is not limited by the first one.
    _, other = key_for(conn, user, "second key")
    assert call(other).status_code == 200

    # With the rate limit off, the concurrent cap is what stops a third request in flight.
    settings.set_value(conn, ACTOR, "api_rate_limit_per_minute", "0")
    settings.set_value(conn, ACTOR, "api_max_concurrent_per_user", "1")
    conn.commit()
    apikeys.hold(user["id"])
    blocked = call(token)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "concurrent_limit_exceeded"
    apikeys.release(user["id"])
    assert call(token).status_code == 200


# ---------- the token limits ----------

def test_the_token_limits_are_checked_before_the_call_and_count_api_usage(conn, monkeypatch):
    user = make_user(conn, "limituser", daily_limit=100)
    _, model = engine(conn, "Limits", "m")
    fake_upstream(monkeypatch, {"chat": completion({"prompt_tokens": 40, "completion_tokens": 20,
                                                    "total_tokens": 60})})
    _, token = key_for(conn, user)
    client = TestClient(app.app)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.post("/api/v1/chat/completions", json={"model": model["namespaced"],
                                                          "messages": MESSAGES}, headers=headers).status_code == 200

    # A chat turn counts toward the same window as an API call, so the second call is the one that tips it.
    add_usage(conn, user["id"], 50, source="chat")
    response = client.post("/api/v1/chat/completions", json={"model": model["namespaced"],
                                                             "messages": MESSAGES}, headers=headers)
    assert response.status_code == 429
    assert response.json()["error"]["message"] == limits.limit_message(
        "daily", 110, 100, limits.reset_at("daily"), "own")
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert 1 <= int(response.headers["retry-after"]) <= 24 * 60 * 60

    split = limits.window_by_source(conn, user["id"], "daily")
    assert split["chat"] == 50 and split["api"] == 60, "both kinds are counted in the same window"

    # A response that is already streaming is allowed to finish, so the overshoot is one response.
    assert usage_rows(conn)[0]["total_tokens"] == 60


def test_the_global_default_applies_when_the_account_has_none(conn, monkeypatch):
    user = make_user(conn, "globaluser")
    _, model = engine(conn, "Global", "m")
    fake_upstream(monkeypatch, {"chat": completion({"prompt_tokens": 60, "completion_tokens": 60,
                                                    "total_tokens": 120})})
    settings.set_value(conn, ACTOR, "global_weekly_limit", "100")
    conn.commit()
    add_usage(conn, user["id"], 100, source="chat")   # exactly at the limit is what blocks a call

    response = TestClient(app.app).post("/api/v1/chat/completions",
                                        json={"model": model["namespaced"], "messages": MESSAGES},
                                        headers={"Authorization": f"Bearer {key_for(conn, user)[1]}"})
    assert response.status_code == 429
    assert "the global default" in response.json()["error"]["message"]
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert "Resets at" in response.json()["error"]["message"]


# ---------- embeddings ----------

def test_embeddings_are_a_passthrough_only_for_a_connection_that_answers_them(conn, monkeypatch):
    user = make_user(conn, "embeduser")
    _, model = engine(conn, "Embeddings", "m")
    fake_upstream(monkeypatch, {"embeddings": {"object": "list", "data": [
        {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "m", "usage": {"prompt_tokens": 5, "total_tokens": 5}}})
    answer = client_for(key_for(conn, user)[1]).embeddings.create(model=model["namespaced"], input="a string")
    assert answer.data[0].embedding == [0.1, 0.2]
    assert answer.model == model["namespaced"]
    assert usage_rows(conn)[0]["source"] == "api"

    # A connection that does not serve them says so, in the same shape.
    _, quiet = engine(conn, "Quiet", "m2")
    fake_upstream(monkeypatch, {"embeddings": {"error": {"message": "not served"}}, "status": 404})
    with pytest.raises(openai.NotFoundError) as error:
        client_for(key_for(conn, user)[1]).embeddings.create(model=quiet["namespaced"], input="x")
    assert error.value.status_code == 404
    assert "does not serve embeddings" in error.value.body["message"]


# ---------- the error shape ----------

def test_errors_are_always_in_the_openai_format(conn, monkeypatch):
    user = make_user(conn, "erroruser")
    _, model = engine(conn, "Errors", "m")
    fake_upstream(monkeypatch, {"chat": {"error": {"message": "the engine is full", "type": "server_error"}},
                                "status": 502})
    client = TestClient(app.app)
    headers = {"Authorization": f"Bearer {key_for(conn, user)[1]}"}

    response = client.post("/api/v1/chat/completions", json={"model": model["namespaced"],
                                                             "messages": MESSAGES}, headers=headers)
    assert response.status_code == 502
    assert response.json() == {"error": {"message": "the engine is full", "type": "upstream_error",
                                         "code": "upstream_error"}}, \
        "the engine's own words when it gave them"
    assert response.json()["error"]["type"] == "upstream_error"

    unknown = client.post("/api/v1/chat/completions", json={"model": "no-such/model",
                                                             "messages": MESSAGES}, headers=headers)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"

    empty = client.post("/api/v1/chat/completions", json={"model": model["namespaced"]}, headers=headers)
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "missing_messages"

    # The UI routes answer {"detail": …} and the API the OpenAI shape: the two do not mix.
    import accounts
    session = TestClient(app.app)
    session.cookies.set("session", accounts.create_session(conn, user))
    conn.commit()
    assert session.get("/api/models").status_code == 200
    assert session.post("/api/keys", json={"name": "a key"}).status_code == 403, "no CSRF header here"
    assert session.post("/api/v1/chat/completions",
                        json={"model": model["namespaced"], "messages": MESSAGES},
                        headers=headers).status_code == 502, "a Bearer call needs no CSRF header"

    _, off = engine(conn, "Disabled", "m3", connection={"enabled": False})
    disabled = client.post("/api/v1/chat/completions", json={"model": off["namespaced"],
                                                             "messages": MESSAGES}, headers=headers)
    assert disabled.status_code == 403 and disabled.json()["error"]["code"] == "connection_disabled", \
        "a disabled connection is refused before the call, not after"
