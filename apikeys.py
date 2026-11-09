"""API keys for the OpenAI-compatible API (/api/v1).

A key is `dsk-` plus 40 random characters. Only its SHA-256 hash and the first 8 characters are
stored, so a key can be shown once at creation and never read back. Keys are accepted on /api/v1/*
only; the session cookie is not accepted there, and a key is not accepted on the UI routes.

A key stops working when it is revoked, when its expiry passes, or when its account is disabled or
deleted — all of that is checked at use time, not stored on the key.

The per-key rate limit and the per-user concurrency are in memory, in the same shape as the search
rate limit in websearch.py: a load guard, not something to look up after a restart.
"""
import hashlib
import secrets
import time
from datetime import datetime, timezone

import db
import settings

PREFIX = "dsk-"
DISPLAY_CHARS = 8

# key id -> the request times this process has seen, and user id -> how many requests are in flight.
_seen: dict[str, list[float]] = {}
_running: dict[str, int] = {}


def new_token() -> str:
    return PREFIX + secrets.token_hex(20)          # 40 characters after the prefix


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def shown(token: str) -> str:
    """What the list shows instead of the key: the prefix plus the first 8 characters."""
    return f"{PREFIX}{(token or '')[len(PREFIX):][:DISPLAY_CHARS]}"


def bearer(value: str) -> str:
    """The token from an Authorization header, with or without the `Bearer` word."""
    value = str(value or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


# ---------- the keys ----------

def create(conn, user: dict, name: str, expires: str = "") -> tuple[dict, str]:
    """The row and the key itself. The key is returned once, here, and never again."""
    token = new_token()
    key_id = secrets.token_hex(12)
    end = str(expires or "").strip() or None
    if end:
        try:
            datetime.fromisoformat(end)
        except ValueError:
            raise ValueError("The expiry must be a date, for example 2026-12-31.")
    conn.execute("INSERT INTO api_keys (id, user_id, name, prefix, token_hash, created, expires) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (key_id, user["id"], str(name or "key").strip()[:60] or "key", shown(token),
                  hash_token(token), db.now(), end))
    db.audit(conn, user["username"], "create key", str(name or "key").strip()[:60] or "key", f"id={key_id}")
    return get(conn, key_id), token


def get(conn, key_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return public(dict(row), conn) if row else None


def list_for(conn, user_id) -> list[dict]:
    rows = conn.execute("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created DESC", (user_id,)).fetchall()
    return [public(dict(row), conn) for row in rows]


def list_all(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM api_keys ORDER BY created DESC LIMIT 200").fetchall()
    return [public(dict(row), conn) for row in rows]


def revoke(conn, key_id: str, actor: dict) -> bool:
    """Revoke one key. An admin can revoke any key, without seeing it."""
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    db.audit(conn, actor["username"], "revoke key", row["name"], f"id={row['id']}")
    return True


def is_past(value: str) -> bool:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


def public(row: dict, conn=None) -> dict:
    out = {"id": row["id"], "user_id": row["user_id"], "name": row["name"], "prefix": row["prefix"],
           "created": row["created"], "expires": row["expires"], "last_used": row["last_used"],
           "revoked": bool(row["revoked"]), "expired": bool(row["expires"]) and is_past(row["expires"])}
    if conn is not None:
        out["tokens_used"] = int(conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS tokens FROM usage_events WHERE key_id = ?",
            (row["id"],)).fetchone()["tokens"])
    return out


# ---------- using one ----------

def authenticate(token: str) -> dict | None:
    """The key for a Bearer token, or None. Not revoked, not expired, and its account can still use the API."""
    if not token:
        return None
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE token_hash = ?", (hash_token(token),)).fetchone()
        if row is None:
            return None
        user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if user is None or user["status"] != "active":
            return None
        if not settings.flags(conn)["allow_api"] or not user["api_access"]:
            return None
        if bool(row["revoked"]) or (row["expires"] and is_past(row["expires"])):
            return None
        conn.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (db.now(), row["id"]))
        record = public(dict(row), None)
        record["user"] = dict(user)
    return record


# ---------- the load guard ----------

def rate_limit(key_id: str) -> int | None:
    """Seconds until this key may make another request, or None when it may make one now."""
    per_minute = settings.number(db.connect(), "api_rate_limit_per_minute")
    if not per_minute:
        return None
    window = 60.0
    now = time.monotonic()
    seen = [moment for moment in _seen.get(key_id, []) if now - moment < window]
    _seen[key_id] = seen
    if len(seen) >= per_minute:
        return int(round(window - (now - seen[0]))) or 1
    seen.append(now)
    return None


def hold(user_id) -> bool:
    """Take one of this user's concurrent slots. False when all of them are taken."""
    limit = settings.number(db.connect(), "api_max_concurrent_per_user")
    key = str(user_id)
    if limit and _running.get(key, 0) >= limit:
        return False
    _running[key] = _running.get(key, 0) + 1
    return True


def release(user_id) -> None:
    key = str(user_id)
    _running[key] = max(0, _running.get(key, 0) - 1)


def clear() -> None:
    """The in-memory counters, which the tests reset between cases."""
    _seen.clear()
    _running.clear()
