"""Token usage: record every upstream call, and aggregate by period in local time.

One row per upstream call, including each iteration of the tool loop. `source` is "chat" or
"dreaming". `estimated` is set when the server did not report usage, in which case the numbers
are characters / 4 rather than what the engine counted, and the UI marks them as estimates.

Rows are stored in UTC; periods (day/month/year) are computed in the local timezone from .env,
with weeks starting on Monday, through limits.py.
"""
from datetime import date, datetime, timedelta, timezone

import limits
import db

CHARS_PER_TOKEN = 4
PER_MESSAGE_OVERHEAD = 4


def token_field(usage: dict, *keys) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def estimate(messages: list[dict], completion_text: str) -> dict:
    """Characters / 4, used when the server reports no usage for a call."""
    prompt_chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            prompt_chars += len(content)
        elif content:
            prompt_chars += len(str(content))
        prompt_chars += PER_MESSAGE_OVERHEAD
        for call in message.get("tool_calls") or []:
            prompt_chars += len(str(call))
    prompt = round(prompt_chars / CHARS_PER_TOKEN)
    completion = round(len(completion_text or "") / CHARS_PER_TOKEN)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def record(conn, user_id, usage: dict, source: str, model: str, conversation_id: str | None = None,
           estimated: bool = False, at: datetime | None = None, connection_id: int | None = None) -> None:
    """One row for one upstream call. `at` is for the seed script and the tests; normally now."""
    prompt = token_field(usage, "prompt_tokens", "input_tokens")
    completion = token_field(usage, "completion_tokens", "output_tokens")
    total = token_field(usage, "total_tokens") or prompt + completion
    if not total:
        return
    moment = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn.execute(
        "INSERT INTO usage_events (user_id, timestamp, prompt_tokens, completion_tokens, total_tokens, "
        "model, source, conversation_id, estimated, connection_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, moment.isoformat(timespec="seconds"), prompt, completion, total, model or "", source,
         conversation_id, 1 if estimated else 0, connection_id),
    )


# ---------- periods ----------

def period_range(period: str, target: date | None = None) -> tuple[datetime, datetime, str]:
    """Half-open local range for one period, plus the label the UI shows.

    Day = one local calendar day, month = one local calendar month, year = one local calendar year,
    all = everything. The boundaries use the local timezone, not UTC.
    """
    tz = limits.tz()
    today = datetime.now(tz)
    if period == "day":
        day = target or today.date()
        start = datetime.combine(day, datetime.min.time(), tz)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        label = day.strftime("%-d %B %Y").lstrip("0")
    elif period == "month":
        day = target or today.date()
        start = datetime.combine(day.replace(day=1), datetime.min.time(), tz)
        end = datetime.combine((day.replace(day=1) + timedelta(days=32)).replace(day=1), datetime.min.time(), tz)
        label = start.strftime("%B %Y")
    elif period == "year":
        year = (target or today.date()).year
        start = datetime(year, 1, 1, 0, 0, tzinfo=tz)
        end = datetime(year + 1, 1, 1, 0, 0, tzinfo=tz)
        label = str(year)
    elif period == "all":
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        label = "all time"
    else:
        raise ValueError(f"unknown period: {period}")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), label


def aggregate(conn, period: str, target: date | None = None, user_id: int | None = None) -> dict:
    """Totals for a period, per user, and the buckets for the chart."""
    start, end, label = period_range(period, target)
    params = [start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")]
    sql = ("SELECT user_id, timestamp, prompt_tokens, completion_tokens, total_tokens, source, "
           "estimated FROM usage_events WHERE timestamp >= ? AND timestamp < ?")
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    rows = [dict(row) for row in conn.execute(sql, params)]

    usernames = {row["id"]: row["username"] for row in conn.execute("SELECT id, username FROM users")}
    totals = {"tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "chat_tokens": 0,
             "dreaming_tokens": 0, "requests": 0, "estimated_requests": 0, "rows": len(rows)}
    per_user: dict[int, dict] = {}
    buckets: dict[str, dict] = {}
    tz = limits.tz()

    for row in rows:
        totals["tokens"] += row["total_tokens"]
        totals["prompt_tokens"] += row["prompt_tokens"]
        totals["completion_tokens"] += row["completion_tokens"]
        totals["requests"] += 1
        totals["estimated_requests"] += 1 if row["estimated"] else 0
        if row["source"] == "dreaming":
            totals["dreaming_tokens"] += row["total_tokens"]
        else:
            totals["chat_tokens"] += row["total_tokens"]

        key = row["user_id"]
        if key not in per_user:
            per_user[key] = {"user_id": key, "username": usernames.get(key) or "(deleted user)",
                             "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "requests": 0,
                             "chat_tokens": 0, "dreaming_tokens": 0, "estimated": 0}
        entry = per_user[key]
        entry["tokens"] += row["total_tokens"]
        entry["prompt_tokens"] += row["prompt_tokens"]
        entry["completion_tokens"] += row["completion_tokens"]
        entry["requests"] += 1
        entry["estimated"] += 1 if row["estimated"] else 0
        if row["source"] == "dreaming":
            entry["dreaming_tokens"] += row["total_tokens"]
        else:
            entry["chat_tokens"] += row["total_tokens"]

        moment = limits.parse_time(row["timestamp"]).astimezone(tz)
        if period == "day":
            key2 = moment.strftime("%H:00")
        elif period == "year":
            key2 = moment.strftime("%m")
        else:
            key2 = moment.strftime("%Y-%m-%d" if period == "month" else "%Y-%m")
        bucket = buckets.setdefault(key2, {"label": key2, "tokens": 0, "chat": 0, "dreaming": 0,
                                           "users": {}, "estimated": 0})
        bucket["tokens"] += row["total_tokens"]
        if row["source"] == "dreaming":
            bucket["dreaming"] += row["total_tokens"]
        else:
            bucket["chat"] += row["total_tokens"]
        if row["user_id"] is not None:
            bucket_key = str(row["user_id"])
        else:
            # Rows that outlive their user: the key the admin panel's "(deleted user)" row uses.
            bucket_key = "deleted"
        bucket["users"][bucket_key] = bucket["users"].get(bucket_key, 0) + row["total_tokens"]
        bucket["estimated"] += 1 if row["estimated"] else 0

    for entry in per_user.values():
        entry["share"] = round(entry["tokens"] / totals["tokens"] * 100, 1) if totals["tokens"] else 0
        entry["current"] = {}
        for window in ("daily", "weekly"):
            user_row = conn.execute("SELECT daily_limit, weekly_limit FROM users WHERE id = ?",
                                   (entry["user_id"],)).fetchone()
            if user_row is None:
                continue
            fake = {"id": entry["user_id"], f"{window}_limit": int(user_row[f"{window}_limit"] or 0)}
            resolved = limits.resolve(conn, fake, window)
            entry["current"][window] = {"limit": resolved["limit"],
                                        "own": resolved["own"],
                                        "source": resolved["source"],
                                        "used": limits.window_tokens(conn, entry["user_id"], window)}

    chart = sorted(buckets.values(), key=lambda bucket: bucket["label"])
    if period == "all" and len(chart) > 24:
        yearly: dict[str, dict] = {}
        for bucket in chart:
            year = bucket["label"][:4]
            row = yearly.setdefault(year, {"label": year, "tokens": 0, "chat": 0, "dreaming": 0,
                                          "users": {}, "estimated": 0})
            row["tokens"] += bucket["tokens"]
            row["chat"] += bucket["chat"]
            row["dreaming"] += bucket["dreaming"]
            row["estimated"] += bucket["estimated"]
            for key, value in bucket["users"].items():
                row["users"][key] = row["users"].get(key, 0) + value
        chart = sorted(yearly.values(), key=lambda bucket: bucket["label"])

    return {
        "period": period,
        "label": label,
        "period_start": start.astimezone(tz).isoformat(timespec="seconds"),
        "period_end": end.astimezone(tz).isoformat(timespec="seconds"),
        "totals": totals,
        "per_user": sorted(per_user.values(), key=lambda row: row["tokens"], reverse=True),
        "chart": chart,
    }
