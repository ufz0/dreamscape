"""Per-user data folders: data/users/<user_id>/{transcripts,wiki,dream-runs,uploads}.

Every module that touches data reads the paths for the current user through current() instead of
the fixed config paths. The current user is set by the request dependency in auth.py and by the
dreaming job per user in its loop.

data/app.db and data/reasoning.json stay global: the database is the app's, the reasoning map is
configuration rather than a user's data.
"""
import contextvars
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config
import db

USERS = config.DATA_DIR / "users"

CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)


@dataclass(frozen=True)
class Paths:
    base: Path
    inbox: Path
    processed: Path
    wiki: Path
    runs: Path
    conversations: Path
    # Files a user uploaded. Every file lives in its own folder named by its id, so a path can never
    # come from a client filename.
    uploads: Path = None

    def ensure(self) -> None:
        for folder in (self.inbox, self.processed, self.wiki, self.runs, self.uploads):
            if folder:
                folder.mkdir(parents=True, exist_ok=True)


def user_dir(user_id) -> Path:
    return USERS / str(user_id)


def paths_for(user_id) -> Paths:
    base = user_dir(user_id)
    return Paths(base=base, inbox=base / "transcripts" / "inbox", processed=base / "transcripts" / "processed",
                 wiki=base / "wiki", runs=base / "dream-runs", conversations=base / "conversations.json",
                 uploads=base / "uploads")


def set_current(user_id) -> Paths:
    CURRENT.set(str(user_id))
    paths = paths_for(user_id)
    paths.ensure()
    return paths


def current() -> Paths:
    user_id = CURRENT.get()
    if user_id is None:
        raise RuntimeError("no user is set for this call")
    return paths_for(user_id)


def legacy_paths() -> Paths:
    """The single-user layout, before per-user folders existed."""
    return Paths(base=config.DATA_DIR, inbox=config.INBOX, processed=config.PROCESSED, wiki=config.WIKI,
                 runs=config.DATA_DIR / "dream-runs", conversations=config.DATA_DIR / "conversations.json",
                 uploads=config.DATA_DIR / "uploads")


def has_legacy_data() -> bool:
    old = legacy_paths()
    if old.conversations.exists() or old.runs.exists():
        return True
    for folder in (old.inbox, old.processed, old.wiki):
        if folder.is_dir() and any(folder.iterdir()):
            return True
    return False


def migrate_legacy(user_id) -> dict:
    """Move the single-user data into the first account's folder.

    A backup copy of data/ is made first, then the folders are moved. Returns what was moved so
    the caller can report it, including whether the wiki git history survived.
    """
    old = legacy_paths()
    target = paths_for(user_id)
    if not has_legacy_data():
        return {"moved": [], "backup": None, "wiki_git": None}

    # A copy of the whole data folder next to it, so nothing is copied into itself.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = config.DATA_DIR.parent / f"data-backup-{stamp}"
    shutil.copytree(config.DATA_DIR, backup)

    moved = []
    for source, destination in ((old.inbox, target.inbox), (old.processed, target.processed),
                               (old.wiki, target.wiki), (old.runs, target.runs)):
        if not source.is_dir():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            shutil.move(str(item), str(destination / item.name))
            moved.append(f"{source.name}/{item.name}")
        source.rmdir()
    if old.conversations.exists():
        shutil.move(str(old.conversations), str(target.conversations))
        moved.append("conversations.json")

    return {
        "moved": sorted(moved),
        "backup": str(backup),
        "wiki_git": bool((target.wiki / ".git").exists()),
    }
