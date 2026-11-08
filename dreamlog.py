"""Dream log: structured records of what a dreaming run did, and the live progress stream.

Each run writes data/dream-runs/<YYYYMMDD-HHMMSS>.json while it runs, so a running run can be read
and shown live. The human-readable dream-log.md is still appended (see dream.py); runs recorded
before this existed are read back from it as a read-only legacy run.

Record shape (see build_record):
  id, started, finished, duration_seconds, trigger, status, model, reasoning_effort, usage, commit,
  transcripts: [{file, messages, note, steps}], cleanup: {findings_before, findings_after, note, steps},
  errors: [{message, attempt, at, transcript}], counts

A step is one event: a tool call with its arguments, a one-line result summary, and for wiki_write
the page, what happened to it, and a unified diff against the version before the call.

Run nothing here; dream.py writes, app.py reads.
"""
import asyncio
import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import appdata
import config

RESULT_MAX_CHARS = 200
DIFF_MAX_CHARS = config.env_int("DREAM_DIFF_MAX_CHARS", 4000)

# Subscribers are per user, so a live run is only shown to the user it belongs to.
SUBSCRIBERS: dict[asyncio.Queue, str] = {}
ACTIVE: dict[str, str | None] = {}      # user id -> the run in progress, so a late listener can catch up

STEP_TYPES = ("read", "search", "write", "fix", "error", "note")


def runs_dir() -> Path:
    return appdata.current().runs


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def build_record(trigger: str, model: str, levels: dict, user: dict | None = None) -> dict:
    return {
        "id": run_id(),
        "started": now(),
        "finished": None,
        "duration_seconds": None,
        "trigger": trigger,
        "status": "running",
        "model": model,
        "user": str(user["id"]) if user else "",
        "username": user["username"] if user else "",
        "reasoning_effort": levels,
        "usage": None,
        "commit": None,
        "note": "",
        "transcripts": [],
        "cleanup": {"findings_before": None, "findings_after": None, "note": "", "steps": []},
        "errors": [],
        "counts": {"created": 0, "updated": 0, "merged": 0, "deleted": 0, "links_fixed": 0,
                   "index_added": 0, "errors": 0},
    }


def add_step(record: dict, target: dict, step: dict) -> dict:
    step.setdefault("type", "note")
    step["at"] = step.get("at") or now()
    target.append(step)
    counts = record["counts"]
    if step["type"] == "write":
        counts[step.get("action", "updated")] = counts.get(step.get("action", "updated"), 0) + 1
    if step.get("index_entry_added"):
        counts["index_added"] += 1
    if step["type"] == "error":
        counts["errors"] += 1
    return step


def unified_diff(old: str, new: str, name: str) -> dict:
    """A unified diff, capped so a very large rewrite does not bloat the record."""
    before = (old or "").splitlines()
    after = (new or "").splitlines()
    lines = list(difflib.unified_diff(before, after, fromfile=f"a/{name}", tofile=f"b/{name}",
                                     lineterm="", n=2))
    text = "\n".join(lines)
    truncated = len(text) > DIFF_MAX_CHARS
    return {"diff": text[:DIFF_MAX_CHARS], "truncated": truncated, "added": len(after) - len(before),
            "changed_lines": sum(1 for line in lines if line.startswith(("+", "-")) and not
                                 line.startswith(("+++", "---")))}


def add_usage(total: dict | None, usage: dict) -> dict:
    """Add one usage block to the run total.

    A usage block is not always flat: some servers put the split in a nested dict, e.g.
    completion_tokens_details: {"reasoning_tokens": 42}. Only numbers are added; nested dicts are
    merged the same way, so a dict-valued field never ends up added to an int.
    """
    total = dict(total or {})
    for key, value in usage.items():
        if isinstance(value, dict):
            total[key] = add_usage(total.get(key), value)
        elif isinstance(value, bool):
            total[key] = total.get(key, 0) + int(value)
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
    return total


def summary(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result)[:RESULT_MAX_CHARS]
    if result.get("error"):
        return result["error"][:RESULT_MAX_CHARS]
    parts = []
    if "lines" in result:
        parts.append(f"{result['lines']} lines")
    if result.get("index_entry_added"):
        parts.append("index entry added")
    if "matches" in result:
        parts.append(f"{result['matches']} matches")
    if "pages" in result:
        parts.append(f"{result['pages']} pages")
    return " · ".join(parts) or json.dumps(result, ensure_ascii=False)[:RESULT_MAX_CHARS]


def save(record: dict) -> None:
    runs_dir().mkdir(parents=True, exist_ok=True)
    (runs_dir() / f"{record['id']}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    ACTIVE[str(record.get("user") or "")] = record["id"] if record["status"] == "running" else None


async def stream(user: str = ""):
    """Events for one listener: the current state first, then whatever happens while it listens."""
    queue = subscribe(user)
    try:
        active = ACTIVE.get(user)
        yield {"type": "state", "running": active is not None, "run": get_run(active) if active else None}
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield {"type": "ping"}
    finally:
        unsubscribe(queue)


def emit(event: dict, user: str = "") -> None:
    target = str(event.get("user") or user)
    for queue, owner in list(SUBSCRIBERS.items()):
        if owner and owner != target:
            continue
        try:
            queue.put_nowait(event)
        except Exception:
            pass


def subscribe(user: str = "") -> asyncio.Queue:
    queue = asyncio.Queue()
    SUBSCRIBERS[queue] = str(user or "")
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    SUBSCRIBERS.pop(queue, None)


def list_runs() -> list[dict]:
    """Run summaries for the current user, newest first: the list the left column shows."""
    runs = []
    folder = runs_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            runs.append({key: record.get(key) for key in
                         ("id", "started", "finished", "duration_seconds", "trigger", "status",
                          "model", "reasoning_effort", "commit", "note", "counts")}
                        | {"transcripts": len(record.get("transcripts") or []),
                           "legacy": record.get("legacy", False)})
    legacy = legacy_runs() if not runs else []
    runs.extend({key: run.get(key) for key in
                 ("id", "started", "finished", "duration_seconds", "trigger", "status",
                  "model", "reasoning_effort", "commit", "note", "counts")}
                | {"transcripts": len(run.get("transcripts") or []), "legacy": True}
                for run in legacy)
    if not runs:
        return []
    return sorted(runs, key=lambda run: run["started"] or "", reverse=True)


def get_run(run_id: str) -> dict | None:
    if run_id.startswith("legacy-"):
        for run in legacy_runs():
            if run["id"] == run_id:
                return run
        return None
    path = runs_dir() / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def legacy_runs() -> list[dict]:
    """Runs from dream-log.md, as they were written before records existed. Read only.

    A section looks like:
      ## 2026-11-07 03:00 (2 transcripts)
      - <file>: <what the model said>
      Mechanical checks before cleanup: ...
      Cleanup: ...
    """
    log = appdata.current().wiki / "dream-log.md"
    if not log.is_file():
        return []
    runs = []
    current = None
    for line in log.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+\((\d+) transcripts\)", line)
        if heading:
            started = f"{heading.group(1)}T{heading.group(2)}:00Z"
            current = {
                "id": f"legacy-{heading.group(1)}-{heading.group(2).replace(':', '')}",
                "started": started,
                "finished": None,
                "duration_seconds": None,
                "trigger": "unknown",
                "status": "legacy",
                "model": "",
                "reasoning_effort": {},
                "usage": None,
                "commit": None,
                "note": "",
                "legacy": True,
                "transcripts": [],
                "cleanup": {"findings_before": None, "findings_after": None, "note": "", "steps": []},
                "errors": [],
                "counts": {"created": 0, "updated": 0, "merged": 0, "deleted": 0, "links_fixed": 0,
                           "index_added": 0, "errors": 0},
            }
            runs.append(current)
            continue
        if current is None:
            continue
        match = re.match(r"^-\s+(\S+\.jsonl):\s*(.*)$", line)
        if match:
            entry = {"file": match.group(1), "messages": None, "note": match.group(2).strip(),
                     "steps": [{"type": "note", "at": current["started"],
                                "summary": match.group(2).strip()}]}
            current["transcripts"].append(entry)
            continue
        if line.lower().startswith("mechanical checks"):
            current["cleanup"]["steps"].append({"type": "note", "at": current["started"],
                                                "summary": line, "kind": "findings"})
            continue
        if line.lower().startswith("cleanup:"):
            current["cleanup"]["note"] = line[len("cleanup:"):].strip()
            current["cleanup"]["steps"].append({"type": "note", "at": current["started"],
                                                "summary": current["cleanup"]["note"]})
    return runs
