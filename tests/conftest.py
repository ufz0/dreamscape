"""Test setup: a throwaway data folder, so the real data/ is never touched."""
import json as jsonlib
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DATA = Path(tempfile.mkdtemp(prefix="dreamscape-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["TIMEZONE"] = "Europe/Vienna"

import pytest

import accounts
import apikeys
import appdata
import connections
import db
import limits
import llm
import runs


PASSWORD = "a long enough password"


@pytest.fixture()
def conn():
    connection = db.connect()
    for table in ("usage_events", "sessions", "login_attempts", "audit", "app_settings", "users",
                  "connections", "connection_models", "runs", "uploads", "api_keys"):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()
    # The module-level caches are per process, so they have to be cleared for each test: the
    # "applied" set would skip the migrations, and the queues and runs would keep state.
    connections.GATES.clear()
    connections.BOOTSTRAPPED.clear()
    runs.RUNS.clear()
    apikeys.clear()
    # User ids are reused once the rows are gone, so the folders must be cleared as well:
    # otherwise a test reads files another test left in the same folder.
    for folder in appdata.USERS.glob("*"):
        shutil.rmtree(folder, ignore_errors=True)
    appdata.CURRENT.set(None)
    yield connection
    connection.close()


def db_connect():
    return db.connect()


def make_user(conn, name, **kwargs):
    user = accounts.create_user(conn, name, PASSWORD, actor="test", **kwargs)
    conn.commit()
    return user


def add_usage(conn, user_id, total, source="chat", timestamp=None, estimated=False, model="test-model"):
    """One usage row. Timestamps are stored as UTC, so a window comparison is exact."""
    import limits
    moment = limits.parse_time(timestamp).astimezone(timezone.utc) if timestamp else datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO usage_events (user_id, timestamp, prompt_tokens, completion_tokens, total_tokens, "
        "model, source, conversation_id, estimated) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (user_id, moment.isoformat(timespec="seconds"), total // 2, total - total // 2, total, model,
         source, 1 if estimated else 0),
    )
    conn.commit()


def create_run(conn, user, cid, params):
    """A run row and the object, without starting it on the module's loop, so a test can await it."""
    run = runs.create(conn, user, cid, params)
    conn.commit()
    return run


def finish(run, timeout=10):
    """Wait for a run that was started on the module's own loop. Polling, so nothing is guessed."""
    deadline = time.monotonic() + timeout
    while run.status in runs.ACTIVE and time.monotonic() < deadline:
        time.sleep(0.02)
    return run


# ---------- the fake upstream ----------

class FakeResponse:
    """A stand-in response. `status` is here so a test can make the upstream refuse."""

    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"upstream {self.status_code}", request=None, response=self)

    def json(self):
        return self.payload


class FakeStream:
    def __init__(self, lines, status: int = 200):
        self.lines = lines
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"upstream {self.status_code}", request=None, response=self)

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeUpstream:
    """Stand-in for httpx: scripted answers for the model list, a completion and a stream.

    `script["stream"]` is a list of chunk lists, one per call; `script["stream_generator"]` is an
    async generator function, which is what a test uses to hold a stream open while it disconnects.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.streams = list(script.get("stream") or [])
        self.generator = script.get("stream_generator")

    def stream(self, method, path, json=None):
        # The parameter is named `json` because that is what httpx calls it, so the module is
        # reached through a different name here.
        self.calls.append(("stream", path, json))
        if self.generator:
            return FakeStreamContext(self.generator(json))
        chunks = self.streams.pop(0)
        return FakeStream(["data: " + jsonlib.dumps(chunk) for chunk in chunks] + ["data: [DONE]"])

    async def get(self, path):
        self.calls.append(("GET", path, None))
        return FakeResponse(self.script.get("models") or {"data": []})

    async def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if path.endswith("/embeddings"):
            return FakeResponse(self.script.get("embeddings") or {"data": []},
                                status=self.script.get("status", 200))
        return FakeResponse(self.script.get("chat") or {"choices": [{"message": {}}], "usage": {}},
                            status=self.script.get("status", 200))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeStreamContext(FakeStream):
    async def aiter_lines(self):
        async for line in self.lines:
            yield line


def fake_upstream(monkeypatch, script):
    """Patch the client factory, so nothing in the test reaches a real server."""
    upstream = FakeUpstream(script)
    monkeypatch.setattr(llm, "client", lambda target=None: upstream)
    return upstream


def sse(chunks):
    """The lines one streamed response would carry."""
    return ["data: " + jsonlib.dumps(chunk) for chunk in chunks] + ["data: [DONE]"]


def delta(content=None, reasoning=None, tool_calls=None, finish_reason=None):
    """One streamed chunk in the shape an OpenAI-compatible endpoint sends it."""
    body = {}
    if content is not None:
        body["content"] = content
    if reasoning is not None:
        body["reasoning_content"] = reasoning
    if tool_calls:
        body["tool_calls"] = tool_calls
    if finish_reason:
        body["finish_reason"] = finish_reason
    return {"choices": [{"delta": body, "finish_reason": finish_reason}]}


def usage(block):
    return {"choices": [], "usage": block}
