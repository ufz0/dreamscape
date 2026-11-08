"""The schema, as numbered SQL files.

Every file in migrations/ is applied in order, once, each in its own transaction. schema_version
records what has been applied, so a start that has nothing pending does nothing.

Before anything pending is applied, data/app.db is copied to data/backups/app-<timestamp>.db. A
migration that changes a table can therefore be undone by hand.

001_baseline.sql is the schema this app used before migrations existed, written with IF NOT EXISTS
throughout. Running it against a database that already has those tables changes nothing, which is
what lets an existing data/app.db keep working.
"""
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config

MIGRATIONS = config.BASE_DIR / "migrations"
DB = config.DATA_DIR / "app.db"
BACKUPS = config.DATA_DIR / "backups"

# A file is applied once per process; db.connect() is called for every request.
applied: set[str] = set()


def files() -> list[Path]:
    """The migration files, in the order their numbers give them."""
    found = [path for path in MIGRATIONS.glob("*.sql") if path.name[:3].isdigit()]
    return sorted(found, key=lambda path: int(path.name[:3]))


def version(conn) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def statements(text: str) -> list[str]:
    """One file split into statements. These files are plain DDL: no strings that contain a semicolon."""
    lines = [line for line in text.splitlines() if not line.strip().startswith("--")]
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def backup() -> str | None:
    """Copy the database before anything is changed. Nothing to copy while the file does not exist yet.

    The backups folder is next to the database, so a data folder moved elsewhere keeps its backups
    with it.
    """
    if not DB.exists() or DB.stat().st_size == 0:
        return None
    folder = DB.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = folder / f"app-{stamp}.db"
    shutil.copy2(DB, target)
    return str(target)


def apply(conn, log=None) -> dict:
    """Apply the pending files. Returns what was applied and where the backup was written.

    Each file is one transaction: a file that fails halfway leaves nothing behind, and its number is
    not recorded, so the next start tries it again.
    """
    key = str(DB)
    if key in applied:
        return {"applied": [], "backup": None}

    # Whether there is something to copy has to be decided before anything is written: checking for
    # the file is not enough, because recording the version creates it.
    had_data = DB.exists() and DB.stat().st_size > 0
    pending = [path for path in files() if int(path.name[:3]) > version(conn)]
    if not pending:
        applied.add(key)
        return {"applied": [], "backup": None}

    copied = backup() if had_data else None
    names = []
    for path in pending:
        number = int(path.name[:3])
        conn.commit()          # nothing half-done is carried into the next transaction
        try:
            conn.execute("BEGIN")
            for statement in statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version, applied) VALUES (?, ?)", (number, db_now()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        names.append(path.name)
        if log:
            log(f"migration {path.name} applied")

    applied.add(key)
    return {"applied": names, "backup": copied}


def db_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
