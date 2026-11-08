"""The stream parser and the tool loop.

The bug these tests cover: an assistant turn that thinks and then asks for a tool has no answer
text, which read as "the model stopped during reasoning". That is a normal intermediate step. The
note may only appear on the final step of a turn, decided by the upstream finish_reason, not by
content being empty.

The upstream here is a fake SSE generator, so nothing depends on a server answering.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import accounts
import agent
import app
import appdata
import llm
import store
import runs
from conftest import fake_upstream, finish, make_user
from test_isolation import client_for


def delta(content=None, reasoning=None, tool_calls=None, finish_reason=None):
    """One streamed chunk in the shape an OpenAI-compatible endpoint sends it."""
    body = {}
    if content is not None:
        body["content"] = content
    if reasoning is not None:
        body["reasoning_content"] = reasoning
    if tool_calls:
        body["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": body, "finish_reason": finish_reason}]}


def tool_call(index, name, arguments, call_id="call-1"):
    return {"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}}


def usage(block):
    return {"choices": [], "usage": block}


def sse(chunks):
    """The lines one streamed response would carry."""
    return ["data: " + json.dumps(chunk) for chunk in chunks] + ["data: [DONE]"]


class FakeStream:
    def __init__(self, lines):
        self.lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeUpstream:
    """Stand-in for httpx: each stream() call answers with the next scripted response."""

    def __init__(self, responses):
        self.responses = list(responses)

    def stream(self, method, path, json=None):
        return FakeStream(self.responses.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def use_upstream(monkeypatch, responses):
    """One shared fake: the tool loop calls it once per iteration, so the answers are consumed in order."""
    upstream = FakeUpstream(responses)
    monkeypatch.setattr(llm, "client", lambda target=None: upstream)


@pytest.fixture()
def tool_user(conn):
    """A user for the tool calls inside the loop, so a wiki tool has a folder to read."""
    user = make_user(conn, "loopuser")
    appdata.set_current(user["id"])
    appdata.current().ensure()
    return user


def run(loop):
    return asyncio.run(collect(loop))


async def collect(loop):
    return [event async for event in loop]


def events_for(monkeypatch, responses, user=None):
    use_upstream(monkeypatch, responses)
    if user:
        appdata.set_current(user["id"])
    return run(agent.run_loop([{"role": "user", "content": "hello"}], "test-model", 0.7))


def assistant_events(events):
    return [event for event in events if event["type"] == "assistant_message"]


# ---------- the reported bug ----------

def test_intermediate_step_with_reasoning_and_tool_call_is_not_a_failed_answer(monkeypatch, tool_user):
    """Thinking, then a tool call: no answer text in that turn, and that is normal."""
    responses = [
        sse([delta(reasoning="I should read the page first.", finish_reason="tool_calls",
                    tool_calls=[tool_call(0, "wiki_read", '{"name": "network-lab"}')])]),
        sse([delta(content="Here is what the page says.", finish_reason="stop")]),
    ]
    events = events_for(monkeypatch, responses)
    turns = assistant_events(events)
    assert len(turns) == 2
    # The first turn ended in a tool call, so it is not "reasoning without an answer".
    assert turns[0]["tool_calls"], "the tool call should still be reported"
    assert turns[0]["reasoning"], "the thinking is still recorded"
    assert not turns[0]["content"]
    assert turns[0]["reasoning_only"] is False, "an intermediate step must never read as a failed answer"
    assert turns[0]["note"] is None
    # The final step answered, so no note either.
    assert turns[1]["reasoning_only"] is False
    assert turns[1]["note"] is None
    assert any(event["type"] == "tool" for event in events)


def test_empty_content_with_tool_calls_is_not_a_failed_answer(monkeypatch, tool_user):
    responses = [
        sse([delta(finish_reason="tool_calls", tool_calls=[tool_call(0, "wiki_list", "{}")])]),
        sse([delta(content="Nothing to read.", finish_reason="stop")]),
    ]
    turns = assistant_events(events_for(monkeypatch, responses))
    assert turns[0]["reasoning_only"] is False
    assert turns[0]["note"] is None


def test_reasoning_only_on_the_last_step_is_the_note_case(monkeypatch):
    responses = [sse([delta(reasoning="Thought about it, then stopped.", finish_reason="stop")])]
    turns = assistant_events(events_for(monkeypatch, responses))
    assert turns[0]["reasoning_only"] is True
    assert turns[0]["note"] == "The model produced reasoning but no final answer."


def test_truncated_during_reasoning_names_the_token_limit(monkeypatch):
    responses = [sse([delta(reasoning="Long thinking that ran out of tokens.", finish_reason="length")])]
    turns = assistant_events(events_for(monkeypatch, responses))
    note = turns[0]["note"]
    assert note and "cut off by the token limit" in note
    assert turns[0]["finish_reason"] == "length"
    # The wording points at what can actually be changed.
    assert "reasoning effort" in note or "max_tokens" in note


def test_answer_without_reasoning_has_no_note(monkeypatch):
    responses = [sse([delta(content="A plain answer.", finish_reason="stop")])]
    turns = assistant_events(events_for(monkeypatch, responses))
    assert not turns[0]["reasoning"]
    assert turns[0]["reasoning_only"] is False
    assert turns[0]["note"] is None


def test_text_tool_call_fallback_is_a_tool_call_not_a_missing_answer(monkeypatch, tool_user):
    """An engine without native tool calling puts the call in plain text; that is a tool turn."""
    responses = [
        sse([delta(reasoning="Thinking.", content='{"tool": "wiki_list", "args": {}}', finish_reason="stop")]),
        sse([delta(content="Done.", finish_reason="stop")]),
    ]
    turns = assistant_events(events_for(monkeypatch, responses))
    assert turns[0]["tool_calls"][0]["name"] == "wiki_list"
    assert turns[0]["reasoning_only"] is False
    assert turns[0]["note"] is None


# ---------- the stream parser ----------

def test_think_tags_split_across_chunks_do_not_drop_content(monkeypatch):
    responses = [sse([
        delta(content="<th"),
        delta(content="ink>the thinking"),
        delta(content=".</thin"),
        delta(content="k>the answer"),
    ])]
    events = events_for(monkeypatch, responses)
    reasoning = "".join(event["content"] for event in events if event["type"] == "reasoning")
    answer = "".join(event["content"] for event in events if event["type"] == "delta")
    # The text held back as a possible tag is released at the end, so nothing is lost.
    assert reasoning == "the thinking.", "the thinking must not lose the split tag text"
    assert answer == "the answer", "the answer must not lose the split tag text"
    turns = assistant_events(events)
    assert turns[0]["reasoning"] == "the thinking."
    assert turns[0]["content"] == "the answer"
    assert turns[0]["reasoning_only"] is False


def test_tool_call_in_the_same_chunk_as_the_end_of_reasoning(monkeypatch, tool_user):
    call = tool_call(0, "wiki_search", '{"query": "switch', call_id="call-a")
    responses = [
        sse([
            # The thinking and the beginning of the call arrive in one chunk.
            delta(reasoning="Thinking.", tool_calls=[call]),
            # The arguments finish in the next chunk, which is where the turn ends.
            delta(tool_calls=[{"index": 0, "function": {"arguments": '-lab"}', "name": ""}}],
                  finish_reason="tool_calls"),
        ]),
        sse([delta(content="Answer.", finish_reason="stop")]),
    ]
    events = events_for(monkeypatch, responses, tool_user)
    first = assistant_events(events)[0]
    assert first["reasoning"] == "Thinking."
    assert first["tool_calls"][0]["id"] == "call-a"
    assert first["tool_calls"][0]["arguments"] == {"query": "switch-lab"}, "the arguments are assembled across chunks"
    assert first["reasoning_only"] is False
    assert first["note"] is None


def test_multiple_tool_calls_in_one_step_are_all_recorded(monkeypatch, tool_user):
    responses = [
        sse([delta(reasoning="Two lookups.", finish_reason="tool_calls", tool_calls=[
            tool_call(0, "wiki_list", "{}", call_id="call-1"),
            tool_call(1, "wiki_search", '{"query": "lab"}', call_id="call-2"),
        ])]),
        sse([delta(content="Both came back.", finish_reason="stop")]),
    ]
    events = events_for(monkeypatch, responses)
    first = assistant_events(events)[0]
    assert [call["name"] for call in first["tool_calls"]] == ["wiki_list", "wiki_search"]
    assert first["reasoning_only"] is False
    tools = [event for event in events if event["type"] == "tool"]
    assert [tool["id"] for tool in tools] == ["call-1", "call-2"]
    assert all(tool["result"] is not None for tool in tools)


def test_usage_is_recorded_for_every_iteration(monkeypatch, tool_user):
    seen = []
    responses = [
        sse([delta(reasoning="Thinking.", finish_reason="tool_calls",
                    tool_calls=[tool_call(0, "wiki_list", "{}")]), usage({"total_tokens": 40})]),
        sse([delta(content="Answer.", finish_reason="stop"), usage({"total_tokens": 60})]),
    ]
    use_upstream(monkeypatch, responses)
    run(agent.run_loop([{"role": "user", "content": "hi"}], "test-model", 0.7,
                       on_usage=lambda block, estimated: seen.append((block, estimated))))
    assert [block["total_tokens"] for block, _ in seen] == [40, 60]
    assert all(not estimated for _, estimated in seen)


# ---------- the stored transcript ----------

def test_transcript_keeps_tool_calls_and_the_reasoning_field_separate(conn, monkeypatch):
    import accounts
    import appdata

    bob = make_user(conn, "loopbob")
    client = client_for(conn, bob)
    token = accounts.create_session(conn, bob)
    conn.commit()
    client.cookies.set("session", token)
    client.get("/")
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf")

    created = client.post("/api/conversations", json={}).json()["conversation"]
    fake_upstream(monkeypatch, {
        "stream": [
            [delta(reasoning="Thinking about the wiki.", finish_reason="tool_calls",
                   tool_calls=[tool_call(0, "wiki_list", "{}", call_id="call-1")])],
            [delta(content="Here is the answer.", finish_reason="stop")],
        ],
    })
    started = client.post(f"/api/conversations/{created['id']}/messages",
                          json={"message": "hello", "model": "test-model"})
    assert started.status_code == 202
    run = finish(runs.get(started.json()["run_id"]))
    assert run.status == "completed"

    appdata.set_current(bob["id"])
    records = store.messages_raw(created["id"])
    by_role = [record for record in records if record["role"] == "assistant"]
    tool_turn = by_role[0]
    assert tool_turn["reasoning"] == "Thinking about the wiki."
    assert not tool_turn["content"], "no fake answer text is written for an intermediate step"
    assert tool_turn["tool_calls"][0]["name"] == "wiki_list", "the tool call itself must be stored"
    assert "note" not in tool_turn, "the note belongs to the final step only"

    tool_records = [record for record in records if record["role"] == "tool"]
    assert tool_records and tool_records[0]["tool_call_id"] == "call-1"

    final = by_role[-1]
    assert final["content"] == "Here is the answer."
    assert "note" not in final

    # The model's own view of the transcript must not contain the thinking.
    messages = store.messages(created["id"])
    assert all("reasoning" not in message for message in messages)
    assert any(message.get("tool_calls") for message in messages)


def test_transcript_stores_the_note_only_for_the_reasoning_only_turn(conn, monkeypatch):
    bob = make_user(conn, "notebob")
    client = client_for(conn, bob)

    created = client.post("/api/conversations", json={}).json()["conversation"]
    fake_upstream(monkeypatch, {"stream": [[delta(reasoning="Nothing came out.", finish_reason="length")]]})
    started = client.post(f"/api/conversations/{created['id']}/messages",
                          json={"message": "hello", "model": "test-model"})
    finish(runs.get(started.json()["run_id"]))

    appdata.set_current(bob["id"])
    record = [record for record in store.messages_raw(created["id"]) if record["role"] == "assistant"][0]
    assert record["reasoning"] == "Nothing came out."
    assert not record["content"]
    assert "cut off by the token limit" in record["note"]
    # Nothing is stored for the model: an assistant turn with no content and no tool call is not
    # part of what the model sees in later turns.
    assert store.messages(created["id"]) == [{"role": "user", "content": "hello"}]
