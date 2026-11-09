"""Nightly dreaming job: read idle transcripts, update the wiki, commit.

Run manually with `python dream.py`, or from cron at 03:00.
  --dry-run  show what would be processed, write nothing
  --now      ignore the idle rule
  --user     run for one user only; without it every active user is processed in turn
  --trigger  what started the run: manual (default), cron, button. DREAM_TRIGGER does the same.
  --include-attachments  read the attached files, not only their names

Each run writes a structured record to that user's data/users/<id>/dream-runs/ (see dreamlog.py)
and appends a short human-readable summary to the user's dream-log.md. Every upstream call is
recorded in the usage table with source="dreaming".

One process, one global lockfile: the job never runs twice at the same time.
"""
import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import accounts
import agent
import appdata
import connections
import config
import db
import dreamlog
import llm
import reasoning
import store
import uploads
import usage
import wiki


class Locked(Exception):
    pass


LOCK_FILE = config.DATA_DIR / ".dream.lock"
TRANSCRIPT_MAX_CHARS = config.env_int("TRANSCRIPT_MAX_CHARS", 30000)
GIT_NAME = config.env("GIT_NAME", "dreamscape")
GIT_EMAIL = config.env("GIT_EMAIL", "dreamscape@localhost")
RETRIES = config.env_int("DREAM_RETRIES", 1)


def acquire_lock() -> None:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except ValueError:
            pid = 0
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)
                raise Locked(f"another dream run is already running (pid {pid})")
            except OSError:
                pass  # stale lock from a dead process
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


def render_transcript(path: Path, include_reasoning: bool = False, include_attachments: bool = False) -> str:
    """A transcript as text the model can read, tool calls and results included.

    Attachments are names only by default: a file is the user's material, and the nightly job has
    enough to work with from what the conversation said about it. --include-attachments reads the
    cached text for each one, which is what a wiki page about a document would need.
    """
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        head = f"[{record.get('ts', '')}] {record.get('role')}"
        for call in record.get("tool_calls") or []:
            lines.append(f"{head} calls {call.get('name')}({json.dumps(call.get('arguments'), ensure_ascii=False)})")
        if record.get("content"):
            lines.append(f"{head}: {record['content']}")
        attachments = record.get("attachments") or []
        if attachments:
            if include_attachments:
                for item in attachments:
                    text = uploads.text_for(item.get("id"))
                    if text:
                        lines.append(f"{head} attached {item.get('name')}: {text}")
            else:
                names = ", ".join(str(item.get("name") or item.get("id")) for item in attachments)
                lines.append(f"{head} attached: {names}")
        # Thinking is kept out of the dreaming context unless asked for: it is the model's own
        # reasoning about the same facts we are already reading.
        if include_reasoning and record.get("reasoning"):
            lines.append(f"{head} (thinking): {record['reasoning']}")
    text = "\n".join(lines)
    if len(text) > TRANSCRIPT_MAX_CHARS:
        text = text[:TRANSCRIPT_MAX_CHARS] + "\n[truncated]"
    return text


def format_findings(checks: dict) -> str:
    parts = [f"wiki: {checks['pages']} pages, {checks['total_bytes']} bytes",
             f"index.md: {checks['index_lines']} lines (limit {checks['index_limit']})"]
    if checks["broken_links"]:
        parts.append("broken links:\n" + "\n".join(
            f"  {item['page']} -> [[{item['link']}]]" for item in checks["broken_links"]))
    if checks["orphans"]:
        parts.append("orphan pages, nothing links to them: " + ", ".join(checks["orphans"]))
    if checks["unindexed"]:
        parts.append("pages with no index entry: " + ", ".join(checks["unindexed"]))
    if checks["oversize"]:
        parts.append("oversize pages:\n" + "\n".join(f"  {p} ({n} lines)" for p, n in checks["oversize"]))
    if len(parts) == 2:
        parts.append("Nothing looks wrong mechanically. Check that index descriptions match the pages, then stop.")
    return "\n".join(parts)


STEP_NAMES = {"wiki_read": "read", "wiki_search": "search", "wiki_list": "read", "wiki_write": "write"}


async def ask(prompt: str, model: str, level: str = "per_transcript", on_step=None, user=None,
             on_usage=None, target: dict | None = None, connection_map: dict | None = None,
             connection_id: int | None = None) -> dict:
    """One model turn with wiki tools.

    The dreaming job has its own effort levels in data/reasoning.json (medium for transcripts,
    high for the cleanup pass by default), read through the same mapping as the chat, with the
    connection's own map layered over it.

    `on_step` is called with every tool event so the run can be recorded as it happens.
    Returns the answer, the wiki_write calls it made, the usage the server reported, and the level
    that was actually used.
    """
    levels = reasoning.settings().get("dream", {})
    mapping = reasoning.resolve(model, levels.get(level, levels.get("per_transcript", "medium")), connection_map)
    changes = []
    final = ""
    usage_block = {}
    async for event in agent.run_loop([{"role": "system", "content": prompt}], model, 0.2, mapping=mapping,
                                      on_usage=lambda block, estimated: record_usage(user, block, model,
                                                                                   estimated, on_usage,
                                                                                   connection_id),
                                      target=target):
        if event["type"] == "assistant_message" and not event["tool_calls"]:
            final = event["content"]
        elif event["type"] == "usage":
            usage_block = usage_block or {}
            usage_block.update(event.get("usage") or {})
        elif event["type"] == "tool":
            result = event["result"]
            if event["name"] == "wiki_write" and not result.get("error"):
                action = "created" if result.get("created") else (
                    "merged" if event["arguments"].get("mode") == "append" else "updated")
                changes.append({"page": result["name"], "mode": event["arguments"].get("mode", "replace"),
                                "created": result.get("created"), "action": action,
                                "index_entry_added": result.get("index_entry_added")})
            if on_step:
                on_step(event)
    return {"answer": final.strip(), "changes": changes, "usage": usage_block, "level": mapping["level"]}


def record_usage(user, block: dict, model: str, estimated: bool, on_usage=None,
                 connection_id: int | None = None) -> None:
    """One upstream call, recorded as it happened, plus the running total for the run record."""
    if user is None:
        return
    with db.transaction() as conn:
        usage.record(conn, user["id"], block, "dreaming", model, None, estimated,
                    connection_id=connection_id)
    if on_usage:
        on_usage(block, estimated)


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=wiki.wiki_dir(), capture_output=True, text=True)


def commit(message: str) -> str | None:
    if not (wiki.wiki_dir() / ".git").exists():
        git("init", "-q")
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return None
    result = git("-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}", "commit", "-q", "-m", message)
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed: {result.stderr.strip()}")
    return git("rev-parse", "--short", "HEAD").stdout.strip()


def committed_files(commit_hash: str) -> list[dict]:
    """What the commit actually contains, so deletions show up too (no tool can delete a page)."""
    output = git("show", "--name-status", "--format=", commit_hash).stdout
    rows = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        kind, name = parts[0], parts[1].removesuffix(".md")
        action = {"A": "created", "M": "updated", "D": "deleted"}.get(kind, "updated")
        rows.append({"page": name, "action": action})
    return rows


def record_tool_step(record: dict, target: list, event: dict, snapshot: dict, group: str = "") -> None:
    """One tool call as an event: arguments, a one-line result, duration, and the diff for a write."""
    result = event["result"]
    step = {
        "type": STEP_NAMES.get(event["name"], "note"),
        "tool": event["name"],
        "args": event["arguments"],
        "summary": dreamlog.summary(result),
        "duration_ms": event.get("duration_ms"),
    }
    if event["name"] == "wiki_write" and not result.get("error"):
        slug = result["name"]
        step["page"] = slug
        step["action"] = "created" if result.get("created") else (
            "merged" if event["arguments"].get("mode") == "append" else "updated")
        step["index_entry_added"] = bool(result.get("index_entry_added"))
        new = (wiki.wiki_dir() / f"{slug}.md").read_text(encoding="utf-8")
        step.update(dreamlog.unified_diff(snapshot.get(slug, ""), new, slug))
        snapshot[slug] = new
    dreamlog.add_step(record, target, step)
    dreamlog.emit({"type": "step", "user": str(record.get("user") or ""), "id": record["id"], "phase": group, "transcript": group, "step": step})


async def run_user(user: dict, trigger: str = "", dry_run: bool = False, force: bool = False,
                   include_reasoning: bool = False, include_attachments: bool = False) -> dict:
    """One dreaming run for one user. Called from run(), which sets the current user."""
    paths = appdata.current()
    paths.ensure()
    # The model for dreaming is the one the admin set, resolved through the connections table.
    with db.transaction() as conn:
        namespaced = connections.default_model(conn, "dream") or config.MODEL
        resolved = connections.resolve(conn, namespaced)
    model = namespaced
    target = connections.target(resolved["connection"]) if resolved else llm.default_target()
    connection_map = resolved["connection"].get("reasoning_map") if resolved else None
    connection_id = resolved["connection"]["id"] if resolved else None
    files = store.idle_transcripts(0 if force or dry_run else config.IDLE_MINUTES)
    levels = reasoning.settings().get("dream", {})
    record = dreamlog.build_record(trigger or config.env("DREAM_TRIGGER", "manual"), model,
                                  {"per_transcript": levels.get("per_transcript", "medium"),
                                   "cleanup": levels.get("cleanup", "high")}, user)
    report = record

    def note(message: str) -> None:
        report["note"] = message

    def count_usage(block: dict, estimated: bool) -> None:
        # Every upstream call is one usage row; the run record keeps its own total as before.
        report["usage"] = dreamlog.add_usage(report["usage"], block)

    if not files:
        record["status"] = "success"
        record["finished"] = dreamlog.now()
        record["duration_seconds"] = 0
        note(f"nothing idle for {config.IDLE_MINUTES} minutes, nothing to do")
        return report

    if dry_run:
        record["status"] = "success"
        record["dry_run"] = True
        record["transcripts"] = [{"file": f["file"], "messages": f["messages"], "note": "",
                                  "steps": [{"type": "note", "summary": "would be processed"}]}
                                 for f in files]
        note("dry run: would process these transcripts, wrote nothing")
        return report

    dreamlog.save(record)
    dreamlog.emit({"type": "start", "id": record["id"], "user": str(user["id"]), "transcripts": len(files)},
                  str(user["id"]))

    snapshot = {}
    for path in sorted(paths.wiki.glob("*.md")):
        snapshot[path.stem] = path.read_text(encoding="utf-8")
    checks_before = wiki.checks()


    processed = []
    try:
        for entry in files:
            path = Path(entry["file"])
            record["transcripts"].append({"file": path.name, "messages": entry["messages"],
                                          "note": "", "steps": [], "attempts": 0})
            item = record["transcripts"][-1]
            dreamlog.emit({"type": "transcript", "id": record["id"], "file": path.name,
                           "position": len(record["transcripts"]), "total": len(files), "phase": path.name},
                          str(user["id"]))
            prompt = config.read_prompt("dream_per_transcript.md", index=wiki.index_text(),
                                       source=path.name,
                                       transcript=render_transcript(path, include_reasoning, include_attachments))
            for attempt in range(1, RETRIES + 2):
                item["attempts"] = attempt
                try:
                    result = await ask(prompt, model, "per_transcript",
                                      on_step=lambda event: record_tool_step(record, item["steps"], event, snapshot,
                                                                           path.name),
                                      user=user, on_usage=count_usage, target=target,
                                      connection_map=connection_map, connection_id=connection_id)
                except Exception as error:
                    step = {"type": "error", "summary": str(error), "attempt": attempt}
                    record["errors"].append({"message": str(error), "attempt": attempt, "at": dreamlog.now(),
                                            "transcript": path.name})
                    dreamlog.add_step(record, item["steps"], step)
                    dreamlog.emit({"type": "error", "id": record["id"], "phase": path.name,
                                   "transcript": path.name, "message": str(error), "attempt": attempt},
                                  str(user["id"]))
                    if attempt <= RETRIES:
                        continue
                    record["status"] = "partial"
                    break
                item["note"] = result["answer"]
                processed.append((path, item))
                dreamlog.emit({"type": "transcript", "id": record["id"], "phase": path.name,
                               "file": path.name, "transcript": path.name,
                               "position": len(record["transcripts"]), "total": len(files),
                               "note": item["note"]}, str(user["id"]))
                print(f"dream: {path.name} -> {item['note'].splitlines()[0] if item['note'] else 'no answer'}")
                break

            dreamlog.save(record)

        findings_before = wiki.checks()
        record["cleanup"]["findings_before"] = findings_before
        try:
            cleanup = await ask(config.read_prompt("dream_cleanup.md", index=wiki.index_text(),
                                                   findings=format_findings(findings_before),
                                                   index_limit=config.INDEX_MAX_LINES),
                               model, "cleanup",
                               on_step=lambda event: record_tool_step(record, record["cleanup"]["steps"], event, snapshot, "cleanup"),
                               user=user, on_usage=count_usage, target=target,
                               connection_map=connection_map, connection_id=connection_id)
            record["cleanup"]["note"] = cleanup["answer"]
        except Exception as error:
            record["status"] = "partial"
            record["errors"].append({"message": str(error), "attempt": 1, "at": dreamlog.now(),
                                    "transcript": None})
            record["cleanup"]["steps"].append({"type": "error", "summary": str(error), "at": dreamlog.now()})
            dreamlog.emit({"type": "error", "id": record["id"], "phase": "cleanup",
                           "message": str(error), "attempt": 1}, str(user["id"]))

        findings_after = wiki.checks()
        record["cleanup"]["findings_after"] = findings_after
        fixed = [item for item in findings_before["broken_links"]
                 if not any(other["page"] == item["page"] and other["link"] == item["link"]
                            for other in findings_after["broken_links"])]
        if fixed:
            record["counts"]["links_fixed"] = len(fixed)
            record["cleanup"]["steps"].append({"type": "fix", "summary": f"{len(fixed)} broken links resolved",
                                              "links": [f"{item['page']} -> {item['link']}" for item in fixed]})

        # The end of the run: only what was processed successfully is moved out of the inbox.
        for path, item in processed:
            path.rename(paths.processed / path.name)

        summary_lines = [f"## {record['started'][:10]} {record['started'][11:16]} ({len(files)} transcripts)", ""]
        for item in record["transcripts"]:
            summary_lines.append(f"- {item['file']}: {item.get('note') or 'no answer'}")
        summary_lines.append("")
        summary_lines.append(f"Mechanical checks before cleanup: {format_findings(findings_before)}")
        summary_lines.append("")
        summary_lines.append(f"Cleanup: {record['cleanup']['note'] or 'no answer'}")

        log_path = paths.wiki / "dream-log.md"
        if not log_path.exists():
            log_path.write_text("# Dream log\n", encoding="utf-8")
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(summary_lines) + "\n")

        report["commit"] = commit(f"dream {record['started'][:10]}: {len(files)} transcripts")
        if report["commit"]:
            for change in committed_files(report["commit"]):
                if change["action"] == "deleted":
                    record["counts"]["deleted"] += 1
                    record["cleanup"]["steps"].append({"type": "write", "page": change["page"],
                                                       "action": "deleted",
                                                       "summary": "page deleted"})
        if record["status"] == "running":
            record["status"] = "success"
    except Exception as error:
        record["status"] = "failed"
        record["errors"].append({"message": str(error), "attempt": 1, "at": dreamlog.now(),
                                "transcript": None})
        report["note"] = f"run failed: {error}. Transcripts left in inbox, nothing committed."
    finally:
        record["finished"] = dreamlog.now()
        record["duration_seconds"] = int(
            (datetime.fromisoformat(record["finished"].replace("Z", "+00:00"))
             - datetime.fromisoformat(record["started"].replace("Z", "+00:00"))).total_seconds())
        dreamlog.save(record)
        dreamlog.emit({"type": "finished", "id": record["id"], "user": str(user["id"]), "status": record["status"],
                       "commit": report["commit"], "note": report["note"] or "",
                       "counts": record["counts"]}, str(user["id"]))
    return report


async def run(trigger: str = "", dry_run: bool = False, force: bool = False,
             include_reasoning: bool = False, include_attachments: bool = False,
             user: dict | None = None) -> dict:
    """Dream for one user when given, otherwise for every active user in turn.

    One process, one lockfile: the whole batch holds the lock, so the cron job and the UI button
    never overlap. Returns that user's record when a user is given, otherwise the list of records.
    """
    config.ensure_dirs()
    with db.transaction() as conn:
        rows = conn.execute("SELECT id FROM users WHERE status = 'active' ORDER BY username").fetchall()
        users = [user] if user else [accounts.get_user(conn, row["id"]) for row in rows]
    if not users:
        return {"status": "skipped", "note": "no accounts yet", "records": []}

    acquire_lock()
    records = []
    try:
        for entry in users:
            appdata.set_current(entry["id"])
            records.append(await run_user(entry, trigger, dry_run, force, include_reasoning, include_attachments))
    finally:
        release_lock()
    if user:
        return records[0]
    return {"status": "success", "records": records,
            "note": "; ".join(f"{record['username']}: {record['note'] or record['status']}" for record in records)}



def main() -> int:
    parser = argparse.ArgumentParser(description="nightly dreaming job")
    parser.add_argument("--dry-run", action="store_true", help="show what would be processed, write nothing")
    parser.add_argument("--now", action="store_true", help="ignore the idle rule")
    parser.add_argument("--user", default="", help="run for this username only")
    parser.add_argument("--include-reasoning", action="store_true",
                        help="pass thinking to the model as well (off by default)")
    parser.add_argument("--include-attachments", action="store_true",
                        help="read the attached files as well; by default only their names are used")
    parser.add_argument("--trigger", default="", help="what started the run: manual, cron, button")
    args = parser.parse_args()

    user = None
    if args.user:
        with db.transaction() as conn:
            user = accounts.get_by_username(conn, args.user)
        if user is None:
            print(f"dream: no such user: {args.user}")
            return 1

    try:
        result = asyncio.run(run(trigger=args.trigger, dry_run=args.dry_run, force=args.now,
                                 include_reasoning=args.include_reasoning,
                                 include_attachments=args.include_attachments, user=user))
    except Locked as error:
        print(f"dream: {error}")
        return 1

    records = [result] if not result.get("records") else result["records"]
    for record in records:
        print(f"dream run ({'dry' if record.get('dry_run') else 'live'}) at {record['started']} "
              f"for {record.get('username') or 'the first account'}")
        if not record["transcripts"]:
            print(record["note"])
            continue
        for item in record["transcripts"]:
            print(f"  {item['file']} ({item['messages']} messages)")
            if item.get("note"):
                print(f"    {item['note']}")
            for step in item.get("steps") or []:
                if step["type"] == "write":
                    print(f"    {step['page']} [{step['action']}] {step.get('summary', '')}")
        if record["cleanup"]["note"]:
            print(f"  cleanup: {record['cleanup']['note']}")
        print(record["note"] or (f"  committed {record['commit']}" if record["commit"] else "  no changes to commit"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
