"""Transcripts (JSONL, append-only) and the conversation index.

Everything here belongs to the current user: the index and the transcript folders come from
appdata.current(), which is set per request (auth.py) or per user in the dreaming loop.
"""
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import appdata
import config

TITLE_MAX = 60


def paths() -> appdata.Paths:
    return appdata.current()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_index() -> list[dict]:
    file = paths().conversations
    if file.exists():
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_index(rows: list[dict]) -> None:
    paths().conversations.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def new_conversation(model: str, system: str) -> dict:
    paths().ensure()
    cid = short_id()
    row = {
        "id": cid,
        "file": str(paths().inbox / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{cid}.jsonl"),
        "model": model,
        "system": system,
        "created": now(),
    }
    save_index(load_index() + [row])
    Path(row["file"]).write_text("", encoding="utf-8")
    return row


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def path_for(row: dict) -> Path:
    """Transcripts move from inbox/ to processed/ during a dream run, so resolve both."""
    name = Path(row["file"]).name
    for folder in (paths().inbox, paths().processed):
        path = folder / name
        if path.exists():
            return path
    return Path(row["file"])


def get(cid: str) -> dict | None:
    for row in load_index():
        if row["id"] == cid:
            return row
    return None


def delete(cid: str) -> bool:
    rows = load_index()
    row = next((r for r in rows if r["id"] == cid), None)
    if row is None:
        return False
    save_index([r for r in rows if r["id"] != cid])
    path = path_for(row)
    if path.exists():
        path.unlink()
    return True


def append(cid: str, record: dict) -> None:
    row = get(cid)
    if row is None:
        return
    record = {"ts": now(), **record}
    with open(path_for(row), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def file_end(cid: str) -> int:
    """Byte offset at the end of a transcript. That is where a run's own records begin."""
    row = get(cid)
    if row is None:
        return 0
    path = path_for(row)
    return path.stat().st_size if path.exists() else 0


def write_tail(cid: str, offset: int, records: list[dict]) -> int:
    """Replace everything from `offset` with `records` and return the new end of the file.

    A run owns the part of the file it wrote: its own assistant record, then the tool records that
    followed it. Re-writing that tail on every partial flush keeps the transcript as one record per
    turn instead of one record per flush, and the order stays the order the turn happened in.
    """
    row = get(cid)
    if row is None:
        return offset
    path = path_for(row)
    with open(path, "r+b" if path.exists() else "wb") as handle:
        handle.seek(offset)
        handle.truncate(offset)
        for record in records:
            handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
    return path.stat().st_size


def messages(cid: str) -> list[dict]:
    """Rebuild the chat message list from a transcript.

    The reasoning field is deliberately left out: thinking is stored for the record and the UI,
    but it is not part of what the model sees in later turns.
    """
    row = get(cid)
    if row is None:
        return []
    path = path_for(row)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        message = {"role": record["role"]}
        if record.get("content"):
            message["content"] = record["content"]
        if record.get("tool_calls"):
            message["tool_calls"] = record["tool_calls"]
        if record.get("tool_call_id"):
            message["tool_call_id"] = record["tool_call_id"]
        if record.get("name"):
            message["name"] = record["name"]
        if "content" not in message and "tool_calls" not in message:
            continue
        out.append(message)
    return out


def messages_raw(cid: str) -> list[dict]:
    """Transcript records as they were written, for the UI."""
    row = get(cid)
    if row is None:
        return []
    path = path_for(row)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repoint_models(slug: str) -> int:
    """Rewrite a bare model id in a conversation index as the namespaced one.

    This is what connections.bootstrap does on first start, once. An index that already holds
    namespaced ids is left alone, so running it again changes nothing.
    """
    moved = 0
    for folder in appdata.USERS.glob("*"):
        moved += _repoint_file(folder / "conversations.json", slug)
    legacy = config.DATA_DIR / "conversations.json"      # the single-user layout, before per-user folders
    return moved + _repoint_file(legacy, slug)


def _repoint_file(file: Path, slug: str) -> int:
    if not file.exists():
        return 0
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    moved = 0
    for row in rows:
        model = str(row.get("model") or "")
        if model and "/" not in model:
            row["model"] = f"{slug}/{model}"
            moved += 1
    if moved:
        file.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return moved


def title_for(row: dict) -> str:
    if row.get("title"):
        return row["title"]
    path = path_for(row)
    if not path.exists():
        return "New chat"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("role") == "user" and record.get("content"):
            text = re.sub(r"\s+", " ", record["content"]).strip()
            return text[:TITLE_MAX]
    return "New chat"


def rename(cid: str, title: str) -> bool:
    rows = load_index()
    row = next((r for r in rows if r["id"] == cid), None)
    if row is None:
        return False
    row["title"] = (title or "").strip()[:120]
    save_index(rows)
    return True


def list_conversations() -> list[dict]:
    rows = []
    for row in load_index():
        path = path_for(row)
        if not path.exists():
            continue
        rows.append({**row, "title": title_for(row), "processed": str(path).startswith(str(paths().processed)),
                     "messages": len(path.read_text(encoding="utf-8").splitlines()),
                     "updated": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(rows, key=lambda r: r["updated"], reverse=True)


def idle_transcripts(minutes: int) -> list[dict]:
    cutoff = time.time() - minutes * 60
    found = []
    for path in sorted(paths().inbox.glob("*.jsonl")):
        if path.stat().st_size == 0:
            continue
        if path.stat().st_mtime <= cutoff:
            found.append({"id": path.stem, "file": str(path), "mtime": path.stat().st_mtime,
                          "messages": len(path.read_text(encoding="utf-8").splitlines())})
    return found
