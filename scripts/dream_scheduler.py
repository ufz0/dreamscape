"""Nightly dream scheduler: runs the dreaming job at a fixed local time each day.

This is what the container runs as the second service (docker-compose `dreamer`), and it is also
fine on a host where you would rather not use a crontab.

- The time is a wall-clock time in the app's timezone (TIMEZONE, default Europe/Vienna), so a
  changeover to winter time moves the run with the clock, which a plain cron hour does not.
- On start it waits for the next occurrence of that time; it does not dream on startup unless
  `--run-now` or RUN_ON_START=1 is set, which is what you want when a container restarts often.
- A run that cannot take the lock (the "Dream now" button is busy, or a previous run is still
  going) is skipped, and the next night is scheduled as normal. A failed night is logged and the
  scheduler keeps running.
- SIGTERM and SIGINT stop it between runs, so the container shuts down cleanly.

Run: .venv/bin/python scripts/dream_scheduler.py
     .venv/bin/python scripts/dream_scheduler.py --at 02:30 --run-now
"""
import argparse
import asyncio
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone

import dream
import limits

STOPPED = False


def stop(signum, frame):
    global STOPPED
    STOPPED = True


def parse_clock(value: str) -> tuple[int, int]:
    value = str(value).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError(f"DREAM_TIME must be HH:MM, not {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"DREAM_TIME must be a time of day, not {value!r}")
    return hour, minute


def next_run(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """The next occurrence of the wall-clock time in the app's timezone."""
    zone = limits.tz()
    moment = now or datetime.now(zone)
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return target


def seconds_until(target: datetime, now: datetime | None = None) -> float:
    """Real seconds, not wall-clock hours: the two can differ on a changeover day."""
    moment = now or datetime.now(timezone.utc)
    return max(0.0, (target.astimezone(timezone.utc) - moment).total_seconds())


def wait(seconds: float) -> None:
    """Sleep in short chunks, so a stop request is honoured straight away."""
    end = time.monotonic() + max(0.0, seconds)
    while not STOPPED:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))


def say(message: str) -> None:
    print(f"[{datetime.now(limits.tz()).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def dream_once(trigger: str) -> None:
    try:
        result = asyncio.run(dream.run(trigger=trigger))
    except dream.Locked as error:
        say(f"skipped: {error}")
        return
    except Exception as error:            # a bad night should not stop the scheduler
        say(f"failed: {error}")
        return

    records = result.get("records") or [result]
    for record in records:
        who = record.get("username") or "the first account"
        commit = f" committed {record['commit']}" if record.get("commit") else ""
        say(f"{who}: {record.get('note') or record.get('status')}{commit}")


def main() -> int:
    parser = argparse.ArgumentParser(description="run the dreaming job at a fixed local time each day")
    parser.add_argument("--at", default="", help="wall-clock time, HH:MM (default DREAM_TIME, then 03:00)")
    parser.add_argument("--run-now", action="store_true", help="dream once on startup, then keep the schedule")
    parser.add_argument("--trigger", default="", help="what started the run, recorded in the dream log (default cron)")
    args = parser.parse_args()

    hour, minute = parse_clock(args.at or os.environ.get("DREAM_TIME", "03:00"))
    trigger = args.trigger or os.environ.get("DREAM_TRIGGER", "cron")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    say(f"dream scheduler: {hour:02d}:{minute:02d} in {limits.tz()}")
    if args.run_now or os.environ.get("RUN_ON_START", "").lower() in ("1", "true", "yes", "on"):
        dream_once(trigger)

    while not STOPPED:
        target = next_run(hour, minute)
        seconds = seconds_until(target)
        say(f"next dream at {target.strftime('%Y-%m-%d %H:%M')} ({int(seconds // 60)} minutes)")
        wait(seconds)
        if STOPPED:
            break
        dream_once(trigger)

    say("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
