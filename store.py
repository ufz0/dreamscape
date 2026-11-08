"""Transcripts (JSONL, append-only) and the conversation index."""
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

INDEX_FILE = config.DATA_DIR / "conversations.json"
TITLE_MAX = 60


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_index() -> list[dict]:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_index(rows: list[dict]) -> None:
    INDEX_FILE.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def new_conversation(model: str, system: str) -> dict:
    config.ensure_dirs()
    cid = short_id()
    row = {
        "id": cid,
        "file": str(config.INBOX / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{cid}.jsonl"),
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
    for folder in (config.INBOX, config.PROCESSED):
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
        rows.append({**row, "title": title_for(row), "processed": str(path).startswith(str(config.PROCESSED)),
                     "messages": len(path.read_text(encoding="utf-8").splitlines()),
                     "updated": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(rows, key=lambda r: r["updated"], reverse=True)


def idle_transcripts(minutes: int) -> list[dict]:
    cutoff = time.time() - minutes * 60
    found = []
    for path in sorted(config.INBOX.glob("*.jsonl")):
        if path.stat().st_size == 0:
            continue
        if path.stat().st_mtime <= cutoff:
            found.append({"id": path.stem, "file": str(path), "mtime": path.stat().st_mtime,
                          "messages": len(path.read_text(encoding="utf-8").splitlines())})
    return found
