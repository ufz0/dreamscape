"""Inference connections: the endpoints an admin manages, and the models each one reports.

Every connection speaks the OpenAI chat completions protocol; the preset only fills in defaults, and
every field stays editable. Model ids are namespaced as `<slug>/<model id>` outside this module, so
two engines that both call a model the same name cannot collide.

What is stored encrypted (cipher.py): the API key and the values of the extra headers. Secrets are
write-only: the API returns only whether one is set and the last four characters, so a value can be
left as it is by sending nothing.

The URLs here are admin-trusted, so the web_fetch SSRF block does not apply to them. Only admins can
create or edit a connection, and the URL must be http or https.

On first start the app creates one connection from the STRATA_* .env values (see bootstrap). After
that the database is the source of truth; the .env values only fill the preset defaults.
"""
import asyncio
import json
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import cipher
import config
import db
import llm
import settings
import store

PRESETS = {
    "strata": {
        "label": "Strata",
        "base_url": lambda: config.env("STRATA_BASE_URL", "http://10.0.0.57:8080/v1"),
        "api_key": lambda: config.env("STRATA_API_KEY", ""),
    },
    "llamacpp": {"label": "llama.cpp (llama-server)", "base_url": "http://localhost:8080/v1", "api_key": ""},
    "ollama": {"label": "Ollama (OpenAI-compatible mode)", "base_url": "http://localhost:11434/v1", "api_key": ""},
    "vllm": {"label": "vLLM", "base_url": "http://localhost:8000/v1", "api_key": ""},
    "lmstudio": {"label": "LM Studio", "base_url": "http://localhost:1234/v1", "api_key": ""},
    "custom": {"label": "Custom OpenAI-compatible", "base_url": "http://localhost:8000/v1", "api_key": ""},
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
VISIBILITY = ("all", "admins")
CAPABILITY_FIELDS = ("supports_vision", "supports_tools", "supports_reasoning", "supports_usage")


class QueueTimeout(Exception):
    """The queue for a connection was full for longer than its timeout."""


def slugify(value: str, taken=None) -> str:
    """A url-safe slug from a name, kept unique."""
    base = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:40] or "connection"
    if not SLUG_RE.match(base):
        base = re.sub(r"[^a-z0-9_-]", "-", "c-" + base)[:40]
    if not taken:
        return base
    candidate, index = base, 2
    while candidate in taken:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def is_http(url: str) -> bool:
    try:
        return urlparse(str(url or "")).scheme in ("http", "https")
    except ValueError:
        return False


def namespace(slug: str, model_id: str) -> str:
    return f"{slug}/{model_id}"


def split_namespaced(value: str) -> tuple[str, str]:
    """`slug/model id`. A model id can itself contain a slash, so the first part is the slug."""
    slug, _, model = str(value or "").partition("/")
    return slug, model


# ---------- reading ----------

def list_connections(conn) -> list[dict]:
    rows = []
    for row in conn.execute("SELECT * FROM connections ORDER BY name").fetchall():
        entry = public(conn, row)
        entry["models"] = models(conn, row["id"], entry)
        rows.append(entry)
    return rows


def get(conn, connection_id) -> dict | None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return public(conn, row) if row else None


def get_by_slug(conn, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM connections WHERE slug = ?", (slug,)).fetchone()
    return public(conn, row) if row else None


def public(conn, row) -> dict:
    """A connection as the API shows it. The stored secret is not in here, only what it ends with."""
    headers = {key: cipher.shown(value) for key, value in json.loads(row["headers"] or "{}").items()}
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "preset": row["preset"],
        "label": PRESETS.get(row["preset"], {}).get("label", row["preset"]),
        "base_url": row["base_url"],
        "api_key": cipher.shown(row["api_key"]),
        "headers": headers,
        "enabled": bool(row["enabled"]),
        "visibility": row["visibility"],
        "timeout_seconds": int(row["timeout_seconds"]),
        "max_concurrent": int(row["max_concurrent"]),
        "body_overrides": json.loads(row["body_overrides"] or "{}"),
        "path_models": row["path_models"],
        "path_chat": row["path_chat"],
        "reasoning_map": json.loads(row["reasoning_map"] or "{}"),
        "created": row["created"],
        "last_test": row["last_test"],
        "test_result": json.loads(row["test_result"] or "{}"),
        "model_count": int(conn.execute("SELECT COUNT(*) AS n FROM connection_models WHERE connection_id = ?",
                                        (row["id"],)).fetchone()["n"]),
    }


def flag(value) -> bool | None:
    """A capability flag. None means nobody has checked it, which is different from False."""
    return None if value is None else bool(value)


def public_model(connection: dict, row) -> dict:
    """A model as the API shows it. NULL capability flags are shown as null: nobody has checked."""
    return {
        "row_id": row["id"],
        "id": row["model_id"],
        "namespaced": namespace(connection["slug"], row["model_id"]),
        "display_name": row["display_name"] or row["model_id"],
        "hidden": bool(row["hidden"]),
        "connection": {"id": connection["id"], "slug": connection["slug"], "name": connection["name"]},
        "supports_vision": flag(row["supports_vision"]),
        "supports_tools": flag(row["supports_tools"]),
        "supports_reasoning": flag(row["supports_reasoning"]),
        "supports_usage": flag(row["supports_usage"]),
        "context_length": row["context_length"],
        "body_overrides": json.loads(row["body_overrides"] or "{}"),
        "reasoning_map": json.loads(row["reasoning_map"] or "{}"),
        "added": row["added"],
        "last_seen": row["last_seen"],
    }


def models(conn, connection_id, connection: dict | None = None) -> list[dict]:
    """The stored model rows for one connection, in the form the API shows."""
    rows = [dict(row) for row in conn.execute(
            "SELECT * FROM connection_models WHERE connection_id = ? ORDER BY model_id", (connection_id,))]
    connection = connection or get(conn, connection_id)
    return [public_model(connection, row) for row in rows] if connection else []


def model_for(conn, namespaced: str) -> dict | None:
    """The stored row for one namespaced model id."""
    slug, model_id = split_namespaced(namespaced)
    row = conn.execute("SELECT * FROM connection_models WHERE model_id = ? AND connection_id = "
                       "(SELECT id FROM connections WHERE slug = ?)", (model_id, slug)).fetchone()
    if row is None:
        return None
    connection = get(conn, row["connection_id"])
    return public_model(connection, row) if connection else None


def target(row) -> dict:
    """What llm.py needs for one connection: base url, headers, paths, timeout. Secrets are decrypted here.

    Accepts a database row or the public form of one, since both are used.
    """
    row = dict(row)
    headers = {"Content-Type": "application/json"}
    key = cipher.decrypt(row.get("api_key") or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for name, value in json.loads(row.get("headers") or "{}").items():
        # A header name is never a secret; only its value is stored encrypted.
        headers[name] = cipher.decrypt(value)
    return {
        "connection_id": row["id"],
        "slug": row["slug"],
        "base_url": str(row["base_url"] or "").rstrip("/"),
        "headers": headers,
        "timeout": float(row.get("timeout_seconds") or 120),
        "max_concurrent": int(row.get("max_concurrent") or 1),
        "path_models": row.get("path_models") or "/models",
        "path_chat": row.get("path_chat") or "/chat/completions",
        # The connection's own request fields. llm.py merges these into every request, so the
        # reasoning mapping can still add to them.
        "body_overrides": json_field(row.get("body_overrides")),
    }


def json_field(value) -> dict:
    """A stored JSON column, or the already-parsed form of it."""
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def request_body(resolved: dict, mapping: dict | None = None) -> dict:
    """What to send for one model: the connection's fields, then the model's own, then the mapping."""
    body = dict(json_field(resolved["connection"].get("body_overrides")))
    body.update(json_field(resolved["model"].get("body_overrides")))
    body.update(json_field(mapping))
    return body


def resolve(conn, namespaced: str) -> dict | None:
    """A model the app can actually use: the connection row plus the model, or None when either is gone."""
    model = model_for(conn, namespaced)
    if model is None:
        return None
    connection = get_by_slug(conn, split_namespaced(namespaced)[0])
    if connection is None or not connection["enabled"]:
        return None
    return {"connection": connection, "model": model}


def default_model(conn, purpose: str = "chat") -> str:
    """The model the app falls back to: what the admin set, or the first model that is visible."""
    key = "default_dream_model" if purpose == "dream" else "default_chat_model"
    stored = settings.raw(conn, key)
    if stored and resolve(conn, stored):
        return stored
    for connection in list_connections(conn):
        if not connection["enabled"] or connection["visibility"] == "admins":
            continue
        for model in connection["models"]:
            if not model["hidden"]:
                return model["namespaced"]
    return stored or ""


# ---------- writing ----------

def preset_default(defaults: dict, key: str) -> str:
    """A preset value, which may be a function so it reads the environment at call time."""
    value = defaults.get(key, "")
    return str(value() if callable(value) else value)


def create(conn, actor: dict, values: dict) -> dict:
    preset = str(values.get("preset") or "custom")
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    defaults = PRESETS[preset]
    name = str(values.get("name") or "").strip()
    if not name:
        raise ValueError("A connection needs a name.")
    url = str(values.get("base_url") or preset_default(defaults, "base_url")).strip()
    if not is_http(url):
        raise ValueError("The base URL must be http or https.")
    taken = {row["slug"] for row in conn.execute("SELECT slug FROM connections")}
    headers = {str(key): cipher.encrypt(value) for key, value in (values.get("headers") or {}).items()}

    conn.execute(
        "INSERT INTO connections (name, slug, preset, base_url, api_key, headers, enabled, visibility, "
        "timeout_seconds, max_concurrent, body_overrides, path_models, path_chat, reasoning_map, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, slugify(name, taken), preset, url,
         cipher.encrypt(values.get("api_key") or preset_default(defaults, "api_key")),
         json.dumps(headers, ensure_ascii=False), 1 if values.get("enabled", True) else 0,
         values.get("visibility") if values.get("visibility") in VISIBILITY else "all",
         int(values.get("timeout_seconds") or 120), int(values.get("max_concurrent") or 2),
         json.dumps(values.get("body_overrides") or {}, ensure_ascii=False),
         values.get("path_models") or "/models", values.get("path_chat") or "/chat/completions",
         json.dumps(values.get("reasoning_map") or {}, ensure_ascii=False), db.now()),
    )
    row = conn.execute("SELECT * FROM connections WHERE name = ?", (name,)).fetchone()
    db.audit(conn, actor["username"], "connection_create", name, f"preset={preset} url={url}")
    return public(conn, row)


def update(conn, actor: dict, connection_id: int, values: dict) -> dict:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    if row is None:
        raise ValueError("No such connection.")

    fields = {}
    for key in ("name", "base_url", "path_models", "path_chat"):
        if key in values:
            text = str(values[key] or "").strip()
            if not text:
                raise ValueError(f"{key} cannot be empty.")
            if key == "base_url" and not is_http(text):
                raise ValueError("The base URL must be http or https.")
            fields[key] = text
    if values.get("visibility") in VISIBILITY:
        fields["visibility"] = values["visibility"]
    if "enabled" in values:
        fields["enabled"] = 1 if bool(values["enabled"]) else 0
    for key in ("timeout_seconds", "max_concurrent"):
        if key in values:
            number = int(values[key])
            if number < 1:
                raise ValueError(f"{key} must be at least 1.")
            fields[key] = number
    for key in ("body_overrides", "reasoning_map"):
        if values.get(key) is not None:
            fields[key] = json.dumps(values[key] if isinstance(values[key], dict) else {}, ensure_ascii=False)

    # A secret is only written when something new was typed: the UI shows "set", not the value.
    if values.get("api_key"):
        fields["api_key"] = cipher.encrypt(values["api_key"])

    if values.get("headers") is not None:
        headers = {key: value for key, value in json.loads(row["headers"] or "{}").items()}
        for key, value in (values["headers"] or {}).items():
            if not str(value or ""):
                continue      # empty field for a value that is already set: leave it
            headers[str(key)] = cipher.encrypt(value)
        fields["headers"] = json.dumps(headers, ensure_ascii=False)

    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE connections SET {assignments} WHERE id = ?", (*fields.values(), connection_id))
    changed = " ".join(key for key in fields if key not in ("api_key", "headers"))
    db.audit(conn, actor["username"], "connection_update", row["name"], changed or "nothing changed")
    return public(conn, conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone())


def delete(conn, actor: dict, connection_id: int) -> dict:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    if row is None:
        raise ValueError("No such connection.")
    for key, what in (("default_chat_model", "the default chat model"), ("default_dream_model", "the dreaming model")):
        if settings.raw(conn, key) and split_namespaced(settings.raw(conn, key))[0] == row["slug"]:
            raise ValueError(f"{row['name']} is {what}, so it cannot be deleted.")
    count = int(conn.execute("SELECT COUNT(*) AS n FROM connection_models WHERE connection_id = ?",
                             (row["id"],)).fetchone()["n"])
    conn.execute("DELETE FROM connections WHERE id = ?", (row["id"],))
    db.audit(conn, actor["username"], "connection_delete", row["name"], f"{count} model rows removed")
    return {"name": row["name"], "models_removed": count}


# ---------- the models a connection reports ----------

def add_model(conn, actor: dict, connection_id: int, model_id: str, values: dict | None = None) -> dict:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    if row is None:
        raise ValueError("No such connection.")
    model_id = str(model_id or "").strip()
    if not model_id:
        raise ValueError("A model id is required.")
    values = values or {}
    conn.execute(
        "INSERT INTO connection_models (connection_id, model_id, display_name, hidden, added, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(connection_id, model_id) DO UPDATE SET display_name = excluded.display_name",
        (connection_id, model_id, str(values.get("display_name") or "").strip(),
         1 if values.get("hidden") else 0, db.now(), db.now()),
    )
    db.audit(conn, actor["username"], "model_add", namespace(row["slug"], model_id))
    return model_for(conn, namespace(row["slug"], model_id))


def update_model(conn, actor: dict, model_id: int, values: dict) -> dict:
    """The per-model settings. A capability flag left as null stays unknown."""
    row = conn.execute("SELECT * FROM connection_models WHERE id = ?", (model_id,)).fetchone()
    if row is None:
        raise ValueError("No such model.")
    fields = {}
    if "display_name" in values:
        fields["display_name"] = str(values["display_name"] or "").strip()
    for key in ("hidden", *CAPABILITY_FIELDS):
        if key in values:
            value = values[key]
            fields[key] = None if value is None else (1 if value else 0)
    if values.get("context_length") is not None:
        fields["context_length"] = None if str(values["context_length"]) == "" else int(values["context_length"])
    for key in ("body_overrides", "reasoning_map"):
        if values.get(key) is not None:
            fields[key] = json.dumps(values[key] if isinstance(values[key], dict) else {}, ensure_ascii=False)

    if fields:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(f"UPDATE connection_models SET {assignments} WHERE id = ?", (*fields.values(), model_id))
        db.audit(conn, actor["username"], "model_update", str(row["model_id"]),
                 " ".join(f"{key}={fields[key] if fields[key] is not None else 'cleared'}" for key in fields))
    return public_model(get(conn, row["connection_id"]),
                        conn.execute("SELECT * FROM connection_models WHERE id = ?", (model_id,)).fetchone())


def delete_model(conn, actor: dict, model_id: int) -> dict:
    row = conn.execute("SELECT * FROM connection_models WHERE id = ?", (model_id,)).fetchone()
    if row is None:
        raise ValueError("No such model.")
    conn.execute("DELETE FROM connection_models WHERE id = ?", (model_id,))
    db.audit(conn, actor["username"], "model_delete", str(row["model_id"]))
    return {"model": row["model_id"]}


def store_reported(conn, connection_id: int, reported: list[dict]) -> dict:
    """What a model list gives back: add what is new, mark what is still there, never touch the flags."""
    added, seen = 0, 0
    for item in reported:
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        existing = conn.execute("SELECT * FROM connection_models WHERE connection_id = ? AND model_id = ?",
                                (connection_id, model_id)).fetchone()
        if existing:
            conn.execute("UPDATE connection_models SET last_seen = ? WHERE id = ?", (db.now(), existing["id"]))
            seen += 1
            target_row = existing
        else:
            conn.execute("INSERT INTO connection_models (connection_id, model_id, display_name, added, last_seen) "
                         "VALUES (?, ?, ?, ?, ?)", (connection_id, model_id,
                                                    str(item.get("name") or model_id), db.now(), db.now()))
            added += 1
            target_row = conn.execute("SELECT * FROM connection_models WHERE connection_id = ? AND model_id = ?",
                                      (connection_id, model_id)).fetchone()
        # A server that reports capabilities fills the ones nobody has set.
        for field in CAPABILITY_FIELDS:
            value = item.get(field)
            if target_row[field] is None and isinstance(value, bool):
                conn.execute(f"UPDATE connection_models SET {field} = ? WHERE id = ?", (1 if value else 0, target_row["id"]))
    return {"added": added, "seen": seen}


# ---------- the queue ----------

GATES: dict[int, "Gate"] = {}


class Gate:
    """One FIFO queue per connection, limited to its max concurrent requests.

    A freed slot is handed to the front of the queue rather than back to the pool, so the order stays
    the order the requests arrived in. Polling is deliberate rather than an asyncio event: chat runs,
    dreaming and the API can call from different event loops, and a plain deque works in all of them.

    `active` counts the holders, including a caller that has just been handed a slot.
    """

    def __init__(self, connection: dict):
        self.connection_id = connection["id"]
        self.limit = max(1, int(connection.get("max_concurrent") or 1))
        self.timeout = float(settings.number(db.connect(), "queue_timeout_seconds"))
        self.active = 0
        self.tickets: deque[dict] = deque()

    def position(self) -> int:
        """How many callers are waiting behind the ones already running. 0 when a slot is free."""
        return len(self.tickets)

    @asynccontextmanager
    async def acquire(self, on_position=None, cancelled=None):
        if self.active < self.limit:
            self.active += 1
        else:
            ticket = {"granted": False}
            self.tickets.append(ticket)
            if on_position:
                on_position(self.position())
            deadline = time.monotonic() + self.timeout
            while not ticket["granted"]:
                if cancelled and cancelled():
                    self.tickets.remove(ticket)
                    raise asyncio.CancelledError
                if time.monotonic() >= deadline:
                    self.tickets.remove(ticket)
                    raise QueueTimeout(f"the queue for this connection was full for "
                                      f"{int(self.timeout)} seconds")
                await asyncio.sleep(0.1)

        try:
            yield self
        finally:
            if self.tickets:
                self.tickets.popleft()["granted"] = True   # the slot goes to the front of the queue
            else:
                self.active -= 1

    def state(self) -> dict:
        return {"active": self.active, "waiting": self.position(), "limit": self.limit}


def gate(target) -> Gate:
    """The queue for a connection, rebuilt when its limit changes so the new limit is honoured.

    Accepts a connection row or the target built from one, since both are used.
    """
    target = dict(target)
    connection_id = target.get("connection_id") or target.get("id")
    limit = max(1, int(target.get("max_concurrent") or 1))
    existing = GATES.get(connection_id)
    if existing and existing.limit == limit:
        return existing
    GATES[connection_id] = Gate({"id": connection_id, "max_concurrent": limit})
    return GATES[connection_id]


# ---------- what an engine actually does ----------

PROBE_TOOL = {"type": "function", "function": {
    "name": "probe",
    "description": "Call this tool with an empty argument object.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}}

PROBE_MESSAGES = [{"role": "user", "content": "Reply with the single word ok."}]
PROBE_REASONING = [{"role": "user", "content": "Think briefly, then reply with the single word ok."}]


async def test_connection(conn, connection_id: int, model_id: str = "") -> dict:
    """Run the checks one at a time and keep the result.

    Each check is reported on its own, because an engine can answer plainly and still not do tool
    calls, or stream without reporting usage. The capability flags are only filled where nobody has
    set them: a value the admin chose is never overwritten.
    """
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    if row is None:
        raise ValueError("No such connection.")
    target_row = target(row)
    model = model_id or next((model["id"] for model in models(conn, connection_id)), "")

    checks: dict[str, dict] = {}
    started = time.monotonic()

    async def measure(name, action):
        began = time.monotonic()
        try:
            outcome = await action()
        except Exception as error:
            checks[name] = {"status": "failed", "ms": int((time.monotonic() - began) * 1000),
                            "error": str(error)}
            return None
        result = {"status": "ok", "ms": int((time.monotonic() - began) * 1000)}
        result.update(outcome if isinstance(outcome, dict) else {"result": outcome})
        checks[name] = result
        return outcome

    def skipped(name, reason):
        checks[name] = {"status": "skipped", "error": reason}

    reported_rows = []

    async def check_models():
        nonlocal reported_rows
        reported_rows = await llm.list_models(target_row)
        return {"count": len(reported_rows)}

    await measure("models", check_models)
    if reported_rows:
        store_reported(conn, connection_id, reported_rows)
    model = model_id or next((model["id"] for model in models(conn, connection_id)), "")
    if not model:
        for name in ("chat", "stream", "tools", "usage", "reasoning"):
            skipped(name, "no model to ask")
        return finish_test(conn, row, checks, started)

    plain = await measure("chat", lambda: llm.complete(target_row, model, PROBE_MESSAGES, 0.0, max_tokens=16))
    streamed = await measure("stream", lambda: collect_stream(target_row, model))
    tool_use = await measure("tools", lambda: collect_stream(target_row, model, tools=[PROBE_TOOL]))
    reasoned = await measure("reasoning", lambda: collect_stream(target_row, model, PROBE_REASONING))

    # Usage is a capability of its own: an engine can stream text and still not report tokens.
    usage = {"status": "failed", "stream": bool(streamed and streamed.get("usage")),
             "non_stream": bool(plain and plain.get("usage"))}
    if usage["stream"] or usage["non_stream"]:
        usage["status"] = "ok" if usage["stream"] and usage["non_stream"] else "partial"
    usage["ms"] = checks.get("chat", {}).get("ms", 0) + checks.get("stream", {}).get("ms", 0)
    checks["usage"] = usage

    fields = sorted({event.get("field") for event in (reasoned or {}).get("events", []) if event.get("field")})
    checks["reasoning"] = {"status": "ok" if fields else "none", "fields": fields,
                           "ms": checks.get("reasoning", {}).get("ms", 0)}

    fill_flags(conn, row["id"], model, {
        "supports_tools": bool(tool_use and tool_use.get("tool_calls")),
        "supports_usage": usage["status"] != "failed",
        "supports_reasoning": bool(fields),
    })
    return finish_test(conn, row, checks, started)


async def collect_stream(target_row, model, messages=None, tools=None):
    events = []
    async for event in llm.stream_chat(target_row, model, messages or PROBE_MESSAGES, 0.0,
                                      tools=tools, max_tokens=64):
        events.append(event)
    return {"events": events, "usage": any(event["type"] == "usage" for event in events),
            "tool_calls": any(event["type"] == "tool_calls" for event in events),
            "text": any(event["type"] == "delta" for event in events)}


async def refresh_models(conn, actor: dict, connection_id: int) -> dict:
    """Fetch the model list from the connection and store it. Flags an admin set are kept."""
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    if row is None:
        raise ValueError("No such connection.")
    reported = await llm.list_models(target(row))
    result = store_reported(conn, connection_id, reported)
    db.audit(conn, actor["username"], "models_refresh", row["name"],
            f"{result['added']} added, {result['seen']} still listed")
    return result


def fill_flags(conn, connection_id: int, model_id: str, values: dict) -> None:
    """Only the unknowns. A flag the admin set by hand is left alone."""
    row = conn.execute("SELECT * FROM connection_models WHERE connection_id = ? AND model_id = ?",
                       (connection_id, model_id)).fetchone()
    if row is None:
        return
    for field, value in values.items():
        if row[field] is None:
            conn.execute(f"UPDATE connection_models SET {field} = ? WHERE id = ?", (1 if value else 0, row["id"]))


def finish_test(conn, row, checks: dict, started: float) -> dict:
    result = {"total_ms": int((time.monotonic() - started) * 1000), "checks": checks,
            "model": next((model["id"] for model in models(conn, row["id"])), "")}
    conn.execute("UPDATE connections SET last_test = ?, test_result = ? WHERE id = ?",
                 (db.now(), json.dumps(result, ensure_ascii=False), row["id"]))
    conn.commit()
    return public(conn, conn.execute("SELECT * FROM connections WHERE id = ?", (row["id"],)).fetchone())

# ---------- first start ----------

BOOTSTRAPPED = set()


def bootstrap(conn, log=None) -> dict | None:
    """Create the Strata connection from .env on first start, then re-point the conversations.

    After this the database is the source of truth. The .env values are only what the preset defaults
    to, so keeping them does not change anything; the README marks them deprecated.
    """
    key = str(db.DB)
    if key in BOOTSTRAPPED:
        return None
    if int(conn.execute("SELECT COUNT(*) AS n FROM connections").fetchone()["n"]):
        BOOTSTRAPPED.add(key)
        return None

    created = create(conn, {"username": "first-run"}, {
        "name": "Strata",
        "preset": "strata",
        "base_url": PRESETS["strata"]["base_url"](),
        "api_key": PRESETS["strata"]["api_key"](),
    })
    model = config.env("STRATA_MODEL", config.MODEL)
    if model:
        add_model(conn, {"username": "first-run"}, created["id"], model)
        if not settings.raw(conn, "default_chat_model"):
            settings.set_value(conn, {"username": "first-run"}, "default_chat_model", namespace(created["slug"], model))
        if not settings.raw(conn, "default_dream_model"):
            settings.set_value(conn, {"username": "first-run"}, "default_dream_model", namespace(created["slug"], model))

    moved = store.repoint_models(created["slug"])
    BOOTSTRAPPED.add(key)
    if log:
        log(f"connections: created {created['name']} from .env ({created['slug']}), "
          f"re-pointed {moved} conversation(s). The STRATA_* variables are no longer the source of truth.")
    return {"connection": created, "repointed": moved}
