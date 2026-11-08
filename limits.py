"""Token limits: the local day and week windows, and the check before a model call.

Semantics, exactly as specified:
- An empty value or 0 means no limit for that window.
- If both are empty/0 the user is unlimited.
- Each limit is checked only if it is > 0, independently of the other.
- Daily window = the current local calendar day; weekly window = the current local calendar week,
  which starts on Monday. Both use the timezone from .env (TIMEZONE, default Europe/Vienna).
- Counted tokens = total_tokens of that user's usage_events in the window. Dreaming counts only
  when the admin setting "count dreaming tokens toward limits" is on.

Times are stored UTC and converted here; nothing in the database is stored in local time.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db


def tz() -> ZoneInfo:
    return ZoneInfo(config.env("TIMEZONE", "Europe/Vienna"))


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_time(value: str) -> datetime:
    return parse_time(value).astimezone(tz())


def local_day(value: str) -> date:
    return local_time(value).date()


def week_start(day: date) -> date:
    """Monday of the week containing that day."""
    return day - timedelta(days=day.weekday())


def local_week_start(value: str) -> date:
    return week_start(local_day(value))


def parse_limit(value) -> int:
    """A limit from user input: '', 0, 100000, '100k', '1.5m'. Empty or 0 means no limit."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return 0
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1000, text[:-1].strip()
    elif text.endswith("m"):
        multiplier, text = 1000000, text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"not a number: {value!r}")
    return max(0, int(round(number * multiplier)))


def format_k(value: int) -> str:
    """12.3k / 1.2M, the way the UI shows token counts."""
    number = int(value)
    if number < 1000:
        return str(number)
    if number < 1000000:
        text = f"{number / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    text = f"{number / 1000000:.1f}".rstrip("0").rstrip(".")
    return f"{text}M"


def window_tokens(conn, user_id, window: str, include_dreaming: bool = False,
                  only_estimated: bool = False, now: datetime | None = None) -> int:
    """Total tokens in the current local day or week for one user.

    `now` defaults to the current time; tests pass a fixed instant to check the rollover.
    """
    moment = now or datetime.now(timezone.utc)
    if window == "daily":
        start = datetime.combine(local_day(moment.isoformat()), datetime.min.time(), tz())
    elif window == "weekly":
        start = datetime.combine(week_start(local_day(moment.isoformat())), datetime.min.time(), tz())
    else:
        raise ValueError(f"unknown window: {window}")

    sql = "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM usage_events WHERE user_id = ? AND timestamp >= ?"
    params = [user_id, start.astimezone(timezone.utc).isoformat(timespec="seconds")]
    if not include_dreaming:
        sql += " AND source = 'chat'"
    if only_estimated:
        sql += " AND estimated = 1"
    return int(conn.execute(sql, params).fetchone()["total"])


def reset_at(window: str, now: datetime | None = None) -> datetime:
    """When the window rolls over, in the configured local timezone."""
    moment = (now or datetime.now(timezone.utc)).astimezone(tz())
    if window == "daily":
        return datetime.combine(moment.date() + timedelta(days=1), datetime.min.time(), tz())
    next_monday = week_start(moment.date()) + timedelta(days=7)
    return datetime.combine(next_monday, datetime.min.time(), tz())


def reset_text(moment: datetime) -> str:
    """How a reset is written in the interface: "00:00 on 09 Nov 2026"."""
    return moment.strftime("%H:%M on %d %b %Y")


def limit_message(window: str, used: int, limit: int, resets: datetime) -> str:
    """One line that names the limit, the usage, the limit and when it resets."""
    return (f"{window.capitalize()} token limit reached ({format_k(limit)}). "
            f"Used {format_k(used)}. Resets at {reset_text(resets)}.")


def check(conn, user: dict, now: datetime | None = None) -> dict | None:
    """None when the call may go ahead, otherwise the limit that is reached.

    Exactly at the limit counts as reached: usage >= limit blocks the call. A response that is
    already streaming is allowed to finish, so a user can overshoot by at most one response;
    the caller records that usage and checks again before the next call.
    """
    include_dreaming = db.setting(conn, "count_dreaming_toward_limits") == "on"
    for window in ("daily", "weekly"):
        limit = int(user.get(f"{window}_limit") or 0)
        if limit <= 0:
            continue
        used = window_tokens(conn, user["id"], window, include_dreaming, now=now)
        if used < limit:
            continue
        resets = reset_at(window, now)
        return {
            "error": "limit",
            "window": window,
            "used": used,
            "limit": limit,
            "resets_at": resets.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "resets_local": resets.isoformat(timespec="minutes"),
            "resets_text": reset_text(resets),
            "message": limit_message(window, used, limit, resets),
        }
    return None


def usage_state(conn, user: dict, now: datetime | None = None) -> dict:
    """What the user's own usage indicator shows: today and this week, with limits."""
    include_dreaming = db.setting(conn, "count_dreaming_toward_limits") == "on"
    moment = now or datetime.now(timezone.utc)
    windows = []
    for window in ("daily", "weekly"):
        limit = int(user.get(f"{window}_limit") or 0)
        used = window_tokens(conn, user["id"], window, include_dreaming, now=now)
        estimated = window_tokens(conn, user["id"], window, include_dreaming, only_estimated=True, now=now)
        resets = reset_at(window, now)
        reached = bool(limit) and used >= limit
        windows.append({
            "window": window,
            "used": used,
            "estimated": estimated,
            "limit": limit,
            "percent": round(used / limit * 100, 1) if limit else 0,
            "reached": reached,
            "resets_at": resets.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "resets_local": resets.isoformat(timespec="minutes"),
            # The same wording the notice in the chat uses, so the app says this one way.
            "resets_text": reset_text(resets) if limit else "",
            "message": limit_message(window, used, limit, resets) if reached else "",
        })
    estimated_total = sum(window["estimated"] for window in windows)
    note = ("Some of these numbers are estimates: the endpoint did not report usage for those calls, "
            "so they are character counts divided by four." if estimated_total else "")
    return {"windows": windows,
            "counted_source": "chat and dreaming" if include_dreaming else "chat only",
            "note": note}
