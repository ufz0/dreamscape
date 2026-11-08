"""Test setup: a throwaway data folder, so the real data/ is never touched."""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DATA = Path(tempfile.mkdtemp(prefix="dreamscape-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["TIMEZONE"] = "Europe/Vienna"

import pytest

import accounts
import appdata
import db
import limits

PASSWORD = "a long enough password"


@pytest.fixture()
def conn():
    connection = db.connect()
    for table in ("usage_events", "sessions", "login_attempts", "audit", "app_settings", "users"):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()
    # User ids are reused once the rows are gone, so the folders must be cleared as well:
    # otherwise a test reads files another test left in the same folder.
    for folder in appdata.USERS.glob("*"):
        shutil.rmtree(folder, ignore_errors=True)
    appdata.CURRENT.set(None)
    yield connection
    connection.close()


def make_user(conn, name, **kwargs):
    user = accounts.create_user(conn, name, PASSWORD, actor="test", **kwargs)
    conn.commit()
    return user


def add_usage(conn, user_id, total, source="chat", timestamp=None, estimated=False, model="test-model"):
    """One usage row. Timestamps are stored as UTC, so a window comparison is exact."""
    moment = limits.parse_time(timestamp).astimezone(timezone.utc) if timestamp else datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO usage_events (user_id, timestamp, prompt_tokens, completion_tokens, total_tokens, "
        "model, source, conversation_id, estimated) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (user_id, moment.isoformat(timespec="seconds"), total // 2, total - total // 2, total, model,
         source, 1 if estimated else 0),
    )
    conn.commit()
