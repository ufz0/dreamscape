"""Limit semantics: every combination, the boundary, and the local day/week windows."""
import os
from datetime import datetime, timezone

import pytest

import accounts
import limits
import settings
from conftest import add_usage, make_user

# A fixed instant so the window boundaries are deterministic: 2026-11-08 is a Sunday,
# and the week that contains it starts on Monday 2026-11-02.
NOW = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)


def test_both_empty_is_unlimited(conn):
    user = make_user(conn, "nolimits")
    add_usage(conn, user["id"], 10_000_000, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None


def test_both_zero_is_unlimited(conn):
    user = make_user(conn, "zerolimits", daily_limit=0, weekly_limit=0)
    add_usage(conn, user["id"], 5_000_000, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None


def test_daily_only(conn):
    user = make_user(conn, "dailyonly", daily_limit=100000)
    add_usage(conn, user["id"], 50000, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None
    add_usage(conn, user["id"], 50000, timestamp=NOW.isoformat())
    blocked = limits.check(conn, user, NOW)
    assert blocked and blocked["window"] == "daily"
    assert blocked["used"] == 100000 and blocked["limit"] == 100000
    # Weekly is empty, so nothing is derived from the daily figure: up to 7x per week is allowed.
    assert limits.window_tokens(conn, user["id"], "weekly", now=NOW) == 100000
    assert limits.check(conn, user, NOW)["window"] == "daily"


def test_weekly_only(conn):
    user = make_user(conn, "weeklyonly", weekly_limit=200000)
    add_usage(conn, user["id"], 199999, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None
    add_usage(conn, user["id"], 1, timestamp=NOW.isoformat())
    blocked = limits.check(conn, user, NOW)
    assert blocked and blocked["window"] == "weekly"


def test_both_set_reports_the_one_that_is_reached(conn):
    user = make_user(conn, "bothset", daily_limit=100000, weekly_limit=200000)
    # Usage earlier in the same week: over the weekly limit, but not in the current day.
    add_usage(conn, user["id"], 200000, timestamp="2026-11-02T09:00:00+00:00")
    blocked = limits.check(conn, user, NOW)
    assert blocked and blocked["window"] == "weekly"
    add_usage(conn, user["id"], 100000, timestamp=NOW.isoformat())
    # Daily is reached too, and daily is checked first.
    assert limits.check(conn, user, NOW)["window"] == "daily"


def test_exactly_at_the_limit_blocks(conn):
    user = make_user(conn, "boundary", daily_limit=1000)
    add_usage(conn, user["id"], 999, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None
    add_usage(conn, user["id"], 1, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is not None


def test_day_rollover_uses_the_local_day(conn):
    user = make_user(conn, "dayroll", daily_limit=1000)
    add_usage(conn, user["id"], 600, timestamp="2026-11-07T23:30:00+01:00")   # Saturday local
    add_usage(conn, user["id"], 400, timestamp="2026-11-08T00:10:00+01:00")   # Sunday local
    assert limits.window_tokens(conn, user["id"], "daily", now=NOW) == 400
    assert limits.window_tokens(conn, user["id"], "weekly", now=NOW) == 1000


def test_week_starts_on_monday(conn):
    user = make_user(conn, "weekroll", weekly_limit=1000)
    add_usage(conn, user["id"], 700, timestamp="2026-11-01T12:00:00+01:00")   # previous week, not counted
    add_usage(conn, user["id"], 1000, timestamp="2026-11-02T00:05:00+01:00")  # Monday of this week
    assert limits.window_tokens(conn, user["id"], "weekly", now=NOW) == 1000
    assert limits.week_start(datetime(2026, 11, 8).date()).isoformat() == "2026-11-02"
    assert limits.week_start(datetime(2026, 11, 2).date()).isoformat() == "2026-11-02"
    blocked = limits.check(conn, user, NOW)
    assert blocked and blocked["window"] == "weekly"


def test_resets_are_local_midnight_and_next_monday(conn):
    user = make_user(conn, "resets", daily_limit=100, weekly_limit=100)
    add_usage(conn, user["id"], 100, timestamp=NOW.isoformat())
    blocked = limits.check(conn, user, NOW)
    assert blocked["resets_local"] == "2026-11-09T00:00+01:00"
    add_usage(conn, user["id"], 100, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW)["resets_local"] == "2026-11-09T00:00+01:00"
    daily = limits.reset_at("daily", NOW)
    weekly = limits.reset_at("weekly", NOW)
    assert daily.isoformat(timespec="minutes") == "2026-11-09T00:00+01:00"
    assert weekly.isoformat(timespec="minutes") == "2026-11-09T00:00+01:00"      # 8 Nov is a Sunday, so the week ends with it
    assert limits.reset_at("weekly", datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)).isoformat(timespec="minutes") == "2026-11-16T00:00+01:00"


def test_the_notice_and_the_usage_bars_word_a_reached_limit_the_same_way(conn):
    """The chat notice and the usage indicator come from one text, so the app says this one way."""
    user = make_user(conn, "wording", daily_limit=1000)
    add_usage(conn, user["id"], 1200, timestamp=NOW.isoformat())

    blocked = limits.check(conn, user, NOW)
    state = limits.usage_state(conn, user, NOW)
    daily = state["windows"][0]

    assert daily["reached"] is True
    assert daily["message"] == blocked["message"]
    assert daily["resets_text"] == limits.reset_text(limits.reset_at("daily", NOW))
    assert daily["message"] == ("Daily token limit reached (1k, your own limit). Used 1.2k. "
                                "Resets at 00:00 on 09 Nov 2026.")
    assert daily["source"] == "own"
    # The wording names the window, the usage, the limit and the reset, and the values are there too.
    assert (daily["window"], daily["used"], daily["limit"]) == ("daily", 1200, 1000)
    assert state["windows"][1]["reached"] is False and state["windows"][1]["message"] == ""

    # Below the limit there is nothing to say.
    conn.execute("DELETE FROM usage_events WHERE user_id = ?", (user["id"],))
    add_usage(conn, user["id"], 800, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None
    assert limits.usage_state(conn, user, NOW)["windows"][0]["reached"] is False


def test_aggregation_uses_the_configured_timezone(conn):
    """The same instant falls in different local days, so the counted amount changes."""
    user = make_user(conn, "tzuser", daily_limit=1000)
    add_usage(conn, user["id"], 600, timestamp="2026-11-07T23:30:00+00:00")
    original = os.environ.get("TIMEZONE")
    try:
        os.environ["TIMEZONE"] = "UTC"
        # 23:30 UTC is still 7 Nov in UTC, so it is not in the current local day.
        assert limits.window_tokens(conn, user["id"], "daily", now=NOW) == 0
        assert limits.reset_at("daily", NOW).isoformat(timespec="minutes") == "2026-11-09T00:00+00:00"
        os.environ["TIMEZONE"] = "Europe/Vienna"
        # The same instant is 00:30 on 8 Nov in Vienna, so it is in the current local day.
        assert limits.window_tokens(conn, user["id"], "daily", now=NOW) == 600
    finally:
        os.environ["TIMEZONE"] = original or "Europe/Vienna"


def test_dreaming_counts_only_when_the_setting_is_on(conn):
    user = make_user(conn, "dreamlimit", daily_limit=1000)
    add_usage(conn, user["id"], 600, source="chat", timestamp=NOW.isoformat())
    add_usage(conn, user["id"], 500, source="dreaming", timestamp=NOW.isoformat())
    assert limits.window_tokens(conn, user["id"], "daily", now=NOW) == 600
    assert limits.check(conn, user, NOW) is None
    accounts.set_setting(conn, {"username": "test"}, "count_dreaming_toward_limits", True)
    assert limits.window_tokens(conn, user["id"], "daily", include_dreaming=True, now=NOW) == 1100
    assert limits.check(conn, user, NOW)["window"] == "daily"


def test_parse_limit_accepts_the_documented_forms(conn):
    assert limits.parse_limit("") == 0
    assert limits.parse_limit(None) == 0
    assert limits.parse_limit(0) == 0
    assert limits.parse_limit("0") == 0
    assert limits.parse_limit("1000") == 1000
    assert limits.parse_limit("100k") == 100000
    assert limits.parse_limit("1.5m") == 1500000
    assert limits.parse_limit(100000) == 100000
    # "unlimited" is the one value that says this user is not capped whatever the global default is.
    assert limits.parse_limit("unlimited") == -1
    assert limits.parse_limit("∞") == -1
    assert limits.parse_limit(-1) == -1
    with pytest.raises(ValueError):
        limits.parse_limit("not a number")
    assert limits.format_k(12345) == "12.3k"
    assert limits.format_k(1200000) == "1.2M"


# ---------- the global defaults ----------

USER_VALUES = {"empty": "", "zero": 0, "positive": 1000, "unlimited": -1}
GLOBAL_VALUES = {"empty": "", "zero": 0, "positive": 2000}


def test_every_combination_resolves_the_right_limit(conn):
    """Own > 0 wins, otherwise the global default, otherwise nothing. Per window, on its own."""
    for window in ("daily", "weekly"):
        for user_name, user_value in USER_VALUES.items():
            for global_name, global_value in GLOBAL_VALUES.items():
                user = make_user(conn, f"{window}-{user_name}-{global_name}",
                                 **{f"{window}_limit": limits.parse_limit(user_value)})
                settings.set_value(conn, {"username": "test"}, f"global_{window}_limit",
                                   limits.parse_limit(global_value))

                resolved = limits.resolve(conn, user, window, NOW)
                if limits.parse_limit(user_value) > 0:
                    expected = ("own", limits.parse_limit(user_value))
                elif limits.parse_limit(user_value) == -1:
                    expected = ("unlimited", 0)
                elif limits.parse_limit(global_value) > 0:
                    expected = ("global", limits.parse_limit(global_value))
                else:
                    expected = ("none", 0)
                assert (resolved["source"], resolved["limit"]) == expected, (window, user_name, global_name)


def test_the_windows_are_resolved_independently(conn):
    """Daily from the user, weekly from the global default. The daily cap does not create a weekly one."""
    user = make_user(conn, "mixed", daily_limit=1000, weekly_limit=0)
    settings.set_value(conn, {"username": "test"}, "global_daily_limit", 2000)
    settings.set_value(conn, {"username": "test"}, "global_weekly_limit", 5000)

    assert limits.resolve(conn, user, "daily", NOW)["source"] == "own"
    assert limits.resolve(conn, user, "daily", NOW)["limit"] == 1000
    assert limits.resolve(conn, user, "weekly", NOW)["source"] == "global"
    assert limits.resolve(conn, user, "weekly", NOW)["limit"] == 5000

    # Between the two: only the daily is over, and its message names the user's own value.
    add_usage(conn, user["id"], 1100, timestamp=NOW.isoformat())
    blocked = limits.check(conn, user, NOW)
    assert blocked["window"] == "daily" and "your own limit" in blocked["message"]
    state = limits.usage_state(conn, user, NOW)
    assert [window["reached"] for window in state["windows"]] == [True, False]

    # Only the weekly: the global default applies to the weekly window, and a window with no limit is
    # never reached, so the daily one stays clear for this user.
    settings.set_value(conn, {"username": "test"}, "global_daily_limit", 0)
    only_weekly = make_user(conn, "weekly_only", daily_limit=0, weekly_limit=2000)
    add_usage(conn, only_weekly["id"], 2100, timestamp=NOW.isoformat())
    blocked = limits.check(conn, only_weekly, NOW)
    assert blocked["window"] == "weekly" and "your own limit" in blocked["message"]
    assert limits.usage_state(conn, only_weekly, NOW)["windows"][0]["reached"] is False


def test_an_explicit_unlimited_overrides_the_global_default(conn):
    user = make_user(conn, "unlimited", daily_limit=-1, weekly_limit=-1)
    settings.set_value(conn, {"username": "test"}, "global_daily_limit", 100000)
    settings.set_value(conn, {"username": "test"}, "global_weekly_limit", 100000)
    add_usage(conn, user["id"], 500000, timestamp=NOW.isoformat())
    assert limits.check(conn, user, NOW) is None
    state = limits.usage_state(conn, user, NOW)
    for window in state["windows"]:
        assert (window["source"], window["limit"], window["reached"]) == ("unlimited", 0, False)


def test_an_empty_user_value_follows_the_global_default_in_the_usage_bars(conn):
    """The bars show the effective limit, not the value stored on the user row."""
    user = make_user(conn, "inheriting", daily_limit=0, weekly_limit=0)
    settings.set_value(conn, {"username": "test"}, "global_daily_limit", 1000)
    settings.set_value(conn, {"username": "test"}, "global_weekly_limit", 2000)
    add_usage(conn, user["id"], 1200, timestamp=NOW.isoformat())

    state = limits.usage_state(conn, user, NOW)
    daily, weekly = state["windows"]
    assert (daily["own"], daily["source"], daily["limit"], daily["percent"], daily["reached"]) == \
           (0, "global", 1000, 120.0, True)
    assert "global default" in daily["message"]
    assert (weekly["own"], weekly["source"], weekly["limit"], weekly["reached"]) == (0, "global", 2000, False)

    # And the admin table shows the same thing: the effective limit and where it comes from.
    row = accounts.list_users(conn)
    entry = next(entry for entry in row if entry["id"] == user["id"])
    assert entry["effective"]["daily"]["source"] == "global"
    assert entry["effective"]["daily"]["limit"] == 1000
    assert entry["effective"]["weekly"]["source"] == "global"
    assert entry["effective"]["weekly"]["limit"] == 2000


def test_the_create_user_dialog_follows_the_same_rules(conn):
    """An empty value in the dialog is stored as 0, which means "use the global value"."""
    settings.set_value(conn, {"username": "test"}, "global_daily_limit", 1000)
    created = accounts.create_user(conn, "newuser", "a long enough password", actor="test",
                                  daily_limit="", weekly_limit=5000)
    assert created["daily_limit"] == 0 and created["weekly_limit"] == 5000
    assert limits.resolve(conn, created, "daily", NOW)["source"] == "global"
    assert limits.resolve(conn, created, "weekly", NOW)["source"] == "own"
    # "unlimited" from the dialog is stored as -1 and wins over the global default.
    unlimited = accounts.create_user(conn, "unlimiteduser", "a long enough password", actor="test",
                                    daily_limit="unlimited", weekly_limit="")
    assert unlimited["daily_limit"] == -1
    assert limits.resolve(conn, unlimited, "daily", NOW)["source"] == "unlimited"
