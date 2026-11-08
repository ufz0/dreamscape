"""Usage recording and period aggregation. Times are stored UTC and grouped in local time."""
from datetime import datetime, timezone

import pytest

import usage
import wiki
from conftest import PASSWORD, add_usage, make_user

VIENNA = timezone.utc   # tests use explicit UTC instants; the local shift is what is being checked


def test_usage_block_fields_and_nested_splits(conn):
    bob = make_user(conn, "usagebob", role="admin")
    # A real response often carries a nested split; only the flat numbers are stored.
    usage.record(conn, bob["id"], {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100,
                                   "completion_tokens_details": {"reasoning_tokens": 30}},
                 "chat", "test-model", "c1")
    row = conn.execute("SELECT * FROM usage_events").fetchone()
    assert row["prompt_tokens"] == 60 and row["completion_tokens"] == 40 and row["total_tokens"] == 100
    assert row["source"] == "chat" and row["conversation_id"] == "c1" and row["estimated"] == 0

    # Alternate names some servers use, and a total that has to be derived.
    usage.record(conn, bob["id"], {"input_tokens": 10, "output_tokens": 5}, "dreaming", "test-model")
    second = conn.execute("SELECT * FROM usage_events ORDER BY id DESC").fetchone()
    assert second["total_tokens"] == 15 and second["source"] == "dreaming"

    # Nothing to record when the block is empty or non-numeric.
    usage.record(conn, bob["id"], {"details": {"nested": 1}}, "chat", "test-model")
    assert conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"] == 2


def test_estimated_flag_and_estimator(conn):
    bob = make_user(conn, "estbob", role="admin")
    messages = [{"role": "system", "content": "x" * 400}, {"role": "user", "content": "y" * 400}]
    estimate = usage.estimate(messages, "z" * 200)
    # Characters / 4, plus a small per-message overhead so the estimate is not too low.
    assert estimate["prompt_tokens"] == round((800 + 2 * usage.PER_MESSAGE_OVERHEAD) / usage.CHARS_PER_TOKEN)
    assert estimate["completion_tokens"] == 50
    usage.record(conn, bob["id"], estimate, "chat", "test-model", estimated=True)
    # A reported usage block is not marked as estimated.
    usage.record(conn, bob["id"], {"total_tokens": 10}, "chat", "test-model")
    flags = [row["estimated"] for row in conn.execute("SELECT estimated FROM usage_events ORDER BY id")]
    assert flags == [1, 0]


def test_periods_group_in_local_time(conn):
    bob = make_user(conn, "periodbob", role="admin")
    # Placed by local time: two local days in November and one in October.
    add_usage(conn, bob["id"], 1000, timestamp="2026-11-07T23:30:00+01:00")      # local day 7 Nov
    add_usage(conn, bob["id"], 2000, timestamp="2026-11-08T10:00:00+01:00")      # local day 8 Nov
    add_usage(conn, bob["id"], 700, source="dreaming", timestamp="2026-11-08T11:00:00+01:00")
    add_usage(conn, bob["id"], 5000, timestamp="2026-10-31T23:30:00+01:00")      # local month October

    day = usage.aggregate(conn, "day", datetime(2026, 11, 8).date())
    assert day["totals"]["tokens"] == 2700
    assert [bucket["label"] for bucket in day["chart"]] == ["10:00", "11:00"]
    assert day["chart"][0]["chat"] == 2000 and day["chart"][1]["dreaming"] == 700
    assert usage.aggregate(conn, "day", datetime(2026, 11, 7).date())["totals"]["tokens"] == 1000

    month = usage.aggregate(conn, "month", datetime(2026, 11, 8).date())
    assert month["totals"]["tokens"] == 3700, "October 31 23:30 local belongs to October"
    assert month["totals"]["chat_tokens"] == 3000 and month["totals"]["dreaming_tokens"] == 700
    assert month["totals"]["requests"] == 3
    assert month["label"] == "November 2026"
    assert len(month["chart"]) == 2

    assert usage.aggregate(conn, "month", datetime(2026, 10, 15).date())["totals"]["tokens"] == 5000
    year = usage.aggregate(conn, "year", datetime(2026, 6, 1).date())
    assert year["totals"]["tokens"] == 8700 and year["label"] == "2026"
    assert len(year["chart"]) == 2
    assert usage.aggregate(conn, "all")["totals"]["tokens"] == 8700


def test_share_and_current_window_per_user(conn):
    admin = make_user(conn, "shareadmin", role="admin")
    bob = make_user(conn, "sharebob")
    add_usage(conn, bob["id"], 3000, timestamp="2026-11-08T10:00:00+01:00")
    add_usage(conn, admin["id"], 1000, timestamp="2026-11-08T10:00:00+01:00")
    report = usage.aggregate(conn, "day", datetime(2026, 11, 8).date())
    rows = {row["username"]: row for row in report["per_user"]}
    assert rows["sharebob"]["share"] == 75.0 and rows["shareadmin"]["share"] == 25.0
    assert rows["sharebob"]["requests"] == 1
    # Deleting a user leaves its usage rows with user_id NULL, so the totals do not change.
    conn.execute("DELETE FROM users WHERE id = ?", (bob["id"],))
    conn.commit()
    after = usage.aggregate(conn, "day", datetime(2026, 11, 8).date())
    assert after["totals"]["tokens"] == 4000
    assert any(row["username"] == "(deleted user)" for row in after["per_user"])
    # The chart keeps those tokens under "deleted", so filtering to that row still draws them.
    bucket = after["chart"][0]
    assert bucket["users"]["deleted"] == 3000
    assert sum(bucket["users"].values()) == bucket["tokens"]


def test_unknown_period_is_rejected(conn):
    with pytest.raises(ValueError):
        usage.aggregate(conn, "week")
