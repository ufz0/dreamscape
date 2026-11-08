"""The nightly scheduler: the time it wakes at, and what it does when a run cannot happen.

Nothing here sleeps for a day or calls the model: the schedule is checked against fixed moments,
and the dreaming job itself is replaced with something that stands for a run.
"""
"""The nightly scheduler: the time it wakes at, and what it does when a run cannot happen.

Nothing here sleeps for a day or calls the model: the schedule is checked against fixed moments,
and the dreaming job itself is replaced with something that stands for a run.
"""
from datetime import datetime

import pytest

import dream
import limits
from scripts import dream_scheduler as scheduler


def test_the_time_is_a_wall_clock_time_in_the_apps_timezone():
    now = datetime(2026, 11, 8, 1, 0, tzinfo=limits.tz())
    target = scheduler.next_run(3, 0, now)
    assert target.strftime("%H:%M") == "03:00"
    assert target.strftime("%Y-%m-%d") == "2026-11-08", "still to come today, so today"
    assert scheduler.seconds_until(target, now) == 2 * 3600


def test_a_time_already_past_is_the_next_night():
    now = datetime(2026, 11, 8, 4, 0, tzinfo=limits.tz())
    target = scheduler.next_run(3, 0, now)
    assert target.strftime("%Y-%m-%d %H:%M") == "2026-11-09 03:00"
    assert scheduler.seconds_until(target, now) == 23 * 3600


def test_the_schedule_follows_the_clock_across_the_changeover():
    zone = limits.tz()

    # 2026-03-29 in Vienna: at 02:00 the clock jumps to 03:00, so two wall-clock hours are one real hour.
    spring = datetime(2026, 3, 29, 1, 0, tzinfo=zone)
    target = scheduler.next_run(3, 0, spring)
    assert target.strftime("%Y-%m-%d %H:%M") == "2026-03-29 03:00"
    assert scheduler.seconds_until(target, spring) == 3600

    # 2026-10-25 in Vienna: at 03:00 the clock goes back to 02:00, so that night is 25 real hours.
    last_summer_run = datetime(2026, 10, 24, 3, 0, tzinfo=zone)
    first_winter_run = scheduler.next_run(3, 0, last_summer_run)
    assert first_winter_run.strftime("%Y-%m-%d %H:%M") == "2026-10-25 03:00"
    assert scheduler.seconds_until(first_winter_run, last_summer_run) == 25 * 3600


def test_clock_times_are_checked():
    assert scheduler.parse_clock("03:00") == (3, 0)
    assert scheduler.parse_clock("23:59") == (23, 59)
    for bad in ("3:0", "24:00", "03:60", "0300", ""):
        with pytest.raises(ValueError):
            scheduler.parse_clock(bad)


def test_a_run_that_cannot_take_the_lock_is_skipped_and_the_schedule_continues(monkeypatch, capsys):
    async def locked(trigger=""):
        raise dream.Locked("another dream run is already running (pid 42)")

    monkeypatch.setattr(dream, "run", locked)
    scheduler.dream_once("cron")
    assert "skipped: another dream run is already running" in capsys.readouterr().out


def test_a_failed_night_does_not_stop_the_scheduler(monkeypatch, capsys):
    async def failing(trigger=""):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(dream, "run", failing)
    scheduler.dream_once("cron")
    assert "failed: upstream is down" in capsys.readouterr().out


def test_each_run_reports_what_it_did(monkeypatch, capsys):
    result = {"records": [
        {"username": "bob", "note": "2 pages updated", "commit": "abc1234", "status": "success"},
        {"username": "ada", "note": "nothing idle", "commit": None, "status": "skipped"},
    ]}

    async def fake(trigger=""):
        return result

    monkeypatch.setattr(dream, "run", fake)
    scheduler.dream_once("cron")
    out = capsys.readouterr().out
    assert "bob: 2 pages updated committed abc1234" in out
    assert "ada: nothing idle" in out
    assert "committed" not in out.split("ada:")[1]


def test_a_stop_request_ends_the_wait_at_once(monkeypatch):
    # Otherwise a container stop would wait for the rest of the day.
    monkeypatch.setattr(scheduler, "STOPPED", False)
    scheduler.stop(15, None)
    assert scheduler.STOPPED is True
    scheduler.wait(600)      # returns at once, because the flag is set
