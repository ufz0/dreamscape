"""Chat titles: the model names a conversation after its first reply."""
import asyncio

import pytest

import appdata
import llm
import store
import titles
import usage
from conftest import add_usage, make_user


def fake_complete(text, usage_block):
    """A stand-in for the upstream, so the test does not depend on a server answering."""
    calls = []

    async def complete(messages, model, temperature, max_tokens=None, extra_body=None):
        calls.append({"messages": messages, "model": model, "max_tokens": max_tokens, "extra_body": extra_body})
        return {"text": text, "usage": usage_block}

    return complete, calls


def conversation_for(user):
    appdata.set_current(user["id"])
    appdata.current().ensure()
    return store.new_conversation("test-model", "")


def name_it(conn, user, cid, model="test-model"):
    appdata.set_current(user["id"])
    return asyncio.run(titles.generate(user, cid, model))


def test_title_is_generated_clean_and_recorded(conn, monkeypatch):
    bob = make_user(conn, "titlebob")
    row = conversation_for(bob)
    store.append(row["id"], {"role": "user", "content": "Which wiki pages do you have?"})
    store.append(row["id"], {"role": "assistant", "content": "Here are the commands from that page."})

    complete, calls = fake_complete('  "wiki tools and the home network lab"  ',
                                   {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    monkeypatch.setattr(llm, "complete", complete)

    result = name_it(conn, bob, row["id"])
    assert result["generated"] is True
    assert result["title"] == "wiki tools and the home network lab"        # one line, no quotes
    assert store.get(row["id"])["title"] == result["title"]
    assert store.list_conversations()[0]["title"] == result["title"]

    # The prompt asks for a name only, and the transcript is what it is named from.
    assert "for the sidebar" in calls[0]["messages"][0]["content"].lower()
    assert "user: Which wiki pages do you have?" in calls[0]["messages"][1]["content"]
    # A name does not need thinking, and it is a short call.
    assert calls[0]["extra_body"].get("reasoning_effort") == "none"
    assert calls[0]["extra_body"].get("max_tokens") == 40

    saved = dict(conn.execute("SELECT * FROM usage_events WHERE user_id = ?", (bob["id"],)).fetchone())
    assert saved["estimated"] == 0 and saved["source"] == "chat" and saved["total_tokens"] == 30


def test_the_call_is_estimated_when_the_endpoint_reports_no_usage(conn, monkeypatch):
    bob = make_user(conn, "titlebob2")
    row = conversation_for(bob)
    store.append(row["id"], {"role": "user", "content": "Tell me about the lab."})
    store.append(row["id"], {"role": "assistant", "content": "The switch is a Netgear GS108."})

    complete, calls = fake_complete("home network lab", {})
    monkeypatch.setattr(llm, "complete", complete)

    result = name_it(conn, bob, row["id"])
    assert result["generated"] is True
    saved = dict(conn.execute("SELECT * FROM usage_events WHERE user_id = ?", (bob["id"],)).fetchone())
    assert saved["estimated"] == 1
    # Characters / 4, so these are the estimator's numbers rather than the engine's.
    expected = usage.estimate(calls[0]["messages"], "home network lab")
    assert (saved["prompt_tokens"], saved["completion_tokens"], saved["total_tokens"]) == (
        expected["prompt_tokens"], expected["completion_tokens"], expected["total_tokens"])


def test_a_name_typed_by_hand_is_left_alone(conn, monkeypatch):
    bob = make_user(conn, "titlebob3")
    row = conversation_for(bob)
    store.append(row["id"], {"role": "user", "content": "Question"})
    store.append(row["id"], {"role": "assistant", "content": "Answer"})
    store.rename(row["id"], "my own name")

    complete, calls = fake_complete("something else", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    monkeypatch.setattr(llm, "complete", complete)

    result = name_it(conn, bob, row["id"])
    assert result["generated"] is False and result["title"] == "my own name"
    assert calls == []                      # no upstream call, so nothing to record
    assert conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"] == 0


def test_a_reached_limit_skips_the_call_and_keeps_the_fallback_title(conn, monkeypatch):
    bob = make_user(conn, "titlebob4", daily_limit=100)
    row = conversation_for(bob)
    question = "Which wiki pages do you have?"
    store.append(row["id"], {"role": "user", "content": question})
    store.append(row["id"], {"role": "assistant", "content": "Answer."})
    add_usage(conn, bob["id"], 100)

    complete, calls = fake_complete("a new name", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    monkeypatch.setattr(llm, "complete", complete)

    result = name_it(conn, bob, row["id"])
    assert result["generated"] is False and calls == []
    assert result["title"] == question      # what the app shows when there is no stored title
    assert "limit" in result["note"]
    assert conn.execute("SELECT COUNT(*) AS n FROM usage_events WHERE user_id = ?",
                        (bob["id"],)).fetchone()["n"] == 1


def test_nothing_to_name_yet(conn, monkeypatch):
    bob = make_user(conn, "titlebob5")
    row = conversation_for(bob)

    complete, calls = fake_complete("a name", {})
    monkeypatch.setattr(llm, "complete", complete)

    result = name_it(conn, bob, row["id"])
    assert result["generated"] is False and result["title"] == "New chat" and calls == []

    with pytest.raises(ValueError):
        name_it(conn, bob, "no-such-conversation")
