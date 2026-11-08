"""SQLite for accounts, sessions, usage events, admin settings and the audit log.

One file: data/app.db. A connection is opened per call rather than shared, which keeps the
FastAPI worker threads from using one connection at the same time. Timestamps are stored as ISO
strings in UTC; local-time grouping happens in limits.py.

usage_events.user_id is nullable on purpose: deleting a user keeps its usage rows anonymous, so
the aggregate numbers for a period do not change. Documented in the README.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

DB = config.DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active',
  must_change_password INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  last_login TEXT,
  daily_limit INTEGER NOT NULL DEFAULT 0,
  weekly_limit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created TEXT NOT NULL,
  expires TEXT NOT NULL,
  last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  timestamp TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'chat',
  conversation_id TEXT,
  estimated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL,
  ip TEXT NOT NULL,
  at TEXT NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events (user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_login_time ON login_attempts (at);
"""

DEFAULT_SETTINGS = {
    "allow_self_registration": "off",
    "count_dreaming_toward_limits": "off",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def audit(conn, actor: str, action: str, target: str = "", detail: str = "") -> None:
    conn.execute("INSERT INTO audit (actor, action, target, detail, at) VALUES (?, ?, ?, ?, ?)",
                 (actor, action, target, detail, now()))
