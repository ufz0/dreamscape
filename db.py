"""SQLite for accounts, sessions, usage events, the settings table and the audit log.

One file: data/app.db. The schema is the numbered SQL files in migrations/, applied at startup by
migrations.py; this module only opens a connection and reads or writes. A connection is opened per
call rather than shared, which keeps the FastAPI worker threads from using one connection at the
same time. Timestamps are stored as ISO strings in UTC; local-time grouping happens in limits.py.

usage_events.user_id is nullable on purpose: deleting a user keeps its usage rows anonymous, so the
aggregate numbers for a period do not change. Documented in the README.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config
import migrations

DB = config.DATA_DIR / "app.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.apply(conn)
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
    """One value from the key/value settings table. settings.py is what gives it a meaning."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def audit(conn, actor: str, action: str, target: str = "", detail: str = "") -> None:
    conn.execute("INSERT INTO audit (actor, action, target, detail, at) VALUES (?, ?, ?, ?, ?)",
                 (actor, action, target, detail, now()))
