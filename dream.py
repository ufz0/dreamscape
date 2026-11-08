"""Nightly dreaming job: read idle transcripts, update the wiki, commit.

Run manually with `python dream.py`, or from cron at 03:00.
  --dry-run  show what would be processed, write nothing
  --now      ignore the idle rule
  --trigger  what started the run: manual (default), cron, button. DREAM_TRIGGER does the same.

Each run writes a structured record to data/dream-runs/ (see dreamlog.py) and appends a short
human-readable summary to the wiki's dream-log.md.
"""
import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import agent
import config
import dreamlog
import reasoning
import store
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


def render_transcript(path: Path, include_reasoning: bool = False) -> str:
    """A transcript as text the model can read, tool calls and results included."""
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


async def ask(prompt: str, model: str, level: str = "per_transcript", on_step=None) -> dict:
    """One model turn with wiki tools.

    The dreaming job has its own effort levels in data/reasoning.json (medium for transcripts,
    high for the cleanup pass by default), read through the same mapping as the chat.

    `on_step` is called with every tool event so the run can be recorded as it happens.
    Returns the answer, the wiki_write calls it made, the usage the server reported, and the level
    that was actually used.
    """
    levels = reasoning.settings().get("dream", {})
    mapping = reasoning.resolve(model, levels.get(level, levels.get("per_transcript", "medium")))
    changes = []
    final = ""
    usage = {}
    async for event in agent.run_loop([{"role": "system", "content": prompt}], model, 0.2, mapping=mapping):
        if event["type"] == "assistant_message" and not event["tool_calls"]:
            final = event["content"]
        elif event["type"] == "usage":
            usage.update(event.get("usage") or {})
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
    return {"answer": final.strip(), "changes": changes, "usage": usage, "level": mapping["level"]}


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=config.WIKI, capture_output=True, text=True)


def commit(message: str) -> str | None:
    if not (config.WIKI / ".git").exists():
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
        new = (config.WIKI / f"{slug}.md").read_text(encoding="utf-8")
        step.update(dreamlog.unified_diff(snapshot.get(slug, ""), new, slug))
        snapshot[slug] = new
    dreamlog.add_step(record, target, step)
    dreamlog.emit({"type": "step", "id": record["id"], "phase": group, "transcript": group, "step": step})


async def run(trigger: str = "", dry_run: bool = False, force: bool = False,
             include_reasoning: bool = False) -> dict:
    config.ensure_dirs()
    model = config.MODEL
    files = store.idle_transcripts(0 if force or dry_run else config.IDLE_MINUTES)
    levels = reasoning.settings().get("dream", {})
    record = dreamlog.build_record(trigger or config.env("DREAM_TRIGGER", "manual"), model,
                                  {"per_transcript": levels.get("per_transcript", "medium"),
                                   "cleanup": levels.get("cleanup", "high")})
    report = record

    def note(message: str) -> None:
        report["note"] = message

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

    acquire_lock()
    dreamlog.save(record)
    dreamlog.emit({"type": "start", "id": record["id"], "transcripts": len(files)})

    snapshot = {}
    for path in sorted(config.WIKI.glob("*.md")):
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
                           "position": len(record["transcripts"]), "total": len(files), "phase": path.name})
            prompt = config.read_prompt("dream_per_transcript.md", index=wiki.index_text(),
                                       source=path.name, transcript=render_transcript(path, include_reasoning))
            for attempt in range(1, RETRIES + 2):
                item["attempts"] = attempt
                try:
                    result = await ask(prompt, model, "per_transcript",
                                      on_step=lambda event: record_tool_step(record, item["steps"], event, snapshot,
                                                                           path.name))
                except Exception as error:
                    step = {"type": "error", "summary": str(error), "attempt": attempt}
                    record["errors"].append({"message": str(error), "attempt": attempt, "at": dreamlog.now(),
                                            "transcript": path.name})
                    dreamlog.add_step(record, item["steps"], step)
                    dreamlog.emit({"type": "error", "id": record["id"], "phase": path.name,
                                   "transcript": path.name, "message": str(error), "attempt": attempt})
                    if attempt <= RETRIES:
                        continue
                    record["status"] = "partial"
                    break
                item["note"] = result["answer"]
                if result["usage"]:
                    report["usage"] = dreamlog.add_usage(report["usage"], result["usage"])
                processed.append((path, item))
                dreamlog.emit({"type": "transcript", "id": record["id"], "phase": path.name,
                               "file": path.name, "transcript": path.name,
                               "position": len(record["transcripts"]), "total": len(files),
                               "note": item["note"]})
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
                               on_step=lambda event: record_tool_step(record, record["cleanup"]["steps"], event, snapshot, "cleanup"))
            record["cleanup"]["note"] = cleanup["answer"]
        except Exception as error:
            record["status"] = "partial"
            record["errors"].append({"message": str(error), "attempt": 1, "at": dreamlog.now(),
                                    "transcript": None})
            record["cleanup"]["steps"].append({"type": "error", "summary": str(error), "at": dreamlog.now()})
            dreamlog.emit({"type": "error", "id": record["id"], "phase": "cleanup",
                           "message": str(error), "attempt": 1})

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
            path.rename(config.PROCESSED / path.name)

        summary_lines = [f"## {record['started'][:10]} {record['started'][11:16]} ({len(files)} transcripts)", ""]
        for item in record["transcripts"]:
            summary_lines.append(f"- {item['file']}: {item.get('note') or 'no answer'}")
        summary_lines.append("")
        summary_lines.append(f"Mechanical checks before cleanup: {format_findings(findings_before)}")
        summary_lines.append("")
        summary_lines.append(f"Cleanup: {record['cleanup']['note'] or 'no answer'}")

        log_path = config.WIKI / "dream-log.md"
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
        dreamlog.emit({"type": "finished", "id": record["id"], "status": record["status"],
                       "commit": report["commit"], "note": report["note"] or "",
                       "counts": record["counts"]})
        release_lock()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="nightly dreaming job")
    parser.add_argument("--dry-run", action="store_true", help="show what would be processed, write nothing")
    parser.add_argument("--now", action="store_true", help="ignore the idle rule")
    parser.add_argument("--include-reasoning", action="store_true",
                        help="pass thinking to the model as well (off by default)")
    parser.add_argument("--trigger", default="", help="what started the run: manual, cron, button")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(trigger=args.trigger, dry_run=args.dry_run, force=args.now,
                                include_reasoning=args.include_reasoning))
    except Locked as error:
        print(f"dream: {error}")
        return 1
    print(f"dream run ({'dry' if args.dry_run else 'live'}) at {result['started']}")
    if not result["transcripts"]:
        print(result["note"])
        return 0
    for item in result["transcripts"]:
        print(f"  {item['file']} ({item['messages']} messages)")
        if item.get("note"):
            print(f"    {item['note']}")
        for step in item.get("steps") or []:
            if step["type"] == "write":
                print(f"    {step['page']} [{step['action']}] {step.get('summary', '')}")
    if result["cleanup"]["note"]:
        print(f"  cleanup: {result['cleanup']['note']}")
    print(result["note"] or (f"  committed {result['commit']}" if result["commit"] else "  no changes to commit"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
