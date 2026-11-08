#!/usr/bin/env python3
"""Seed a demo database with fake users and fake usage so the admin charts have something to show.

Run it by hand; it is never run automatically and it never touches the real data folder:

    .venv/bin/python scripts/seed_demo.py --data-dir data/demo

It writes to its own data folder, so data/demo/app.db and data/demo/users/ are separate from
data/app.db. That folder is inside data/, which .gitignore already ignores.
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_USERS = [
    ("ada.admin", "admin", 200000, 1000000),
    ("bob", "user", 100000, 0),
    ("carol", "user", 0, 250000),
    ("dan.dev", "user", 0, 0),
    ("eve", "user", 20000, 0),
]

# A quiet month, a busy month, and a couple of spikes, so the chart is not a flat line.
MONTHS = 14
REQUESTS_PER_DAY = (1, 3, 5, 8)


def main() -> int:
    parser = argparse.ArgumentParser(description="seed a demo database for the admin charts")
    parser.add_argument("--data-dir", default="data/demo", help="where the demo app.db is written")
    parser.add_argument("--seed", type=int, default=7, help="random seed, so the same run gives the same numbers")
    args = parser.parse_args()

    target = Path(args.data_dir)
    if not target.is_absolute():
        target = ROOT / target
    os.environ["DATA_DIR"] = str(target)

    import accounts
    import appdata
    import db
    import usage

    target.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    conn = db.connect()

    print(f"seeding {target / 'app.db'} (this does not touch the real data folder)")
    users = []
    for name, role, daily, weekly in DEMO_USERS:
        user = accounts.get_by_username(conn, name) or accounts.create_user(
            conn, name, "a demo password for the charts", role=role, actor="seed",
            daily_limit=daily, weekly_limit=weekly)
        users.append(user)
    conn.commit()

    end = datetime.now(timezone.utc)
    rows = 0
    for day in range(MONTHS * 31):
        moment = end - timedelta(days=MONTHS * 31 - day, hours=rng.randint(0, 12))
        for user in users:
            for _ in range(rng.choice(REQUESTS_PER_DAY)):
                prompt = rng.randint(500, 6000)
                completion = rng.randint(100, 1500)
                estimated = rng.random() < 0.1
                usage.record(conn, user["id"], {"prompt_tokens": prompt, "completion_tokens": completion,
                                                "total_tokens": prompt + completion},
                             "chat" if rng.random() > 0.15 else "dreaming",
                             "qwen3.8-flash-next", None, estimated, at=moment)
                rows += 1
    conn.commit()
    conn.close()

    print(f"{len(users)} users, {rows} usage rows across {MONTHS} months")
    print("Open the app with DATA_DIR=data/demo and look at /admin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
