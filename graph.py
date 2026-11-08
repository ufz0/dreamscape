"""Wiki graph: pages as nodes, [[links]] as edges, and which dreaming run created each page.

Cached and rebuilt only when wiki files change or a new commit appears, so the graph view stays
fast for 1000+ pages. Run numbers come from the git history of data/wiki: the first commit that
added a file is the run that created it. A link's appearance is approximated as the run where both
pages exist, since a link cannot exist before either page does.
"""
import subprocess
from datetime import datetime, timezone

import appdata
import config
import wiki

# Keyed per user, so one user's graph is never served to another.
CACHE = {"key": None, "data": None}
SKIP = ("index", "dream-log")


def git(*args) -> str:
    folder = wiki.wiki_dir()
    if not (folder / ".git").exists():
        return ""
    result = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def cache_key() -> tuple:
    folder = wiki.wiki_dir()
    files = [p for p in folder.glob("*.md") if p.stem not in SKIP]
    newest = max((p.stat().st_mtime_ns for p in files), default=0)
    return (str(appdata.current().base), len(files), newest, git("rev-parse", "--short", "HEAD"))


def archive() -> list[dict]:
    """Commits oldest first. Each commit counts as one run; dream commits are marked as such."""
    raw = git("log", "--name-status", "--no-renames", "--date=iso-strict",
              "--format=\x1f%H\x1f%cI\x1f%s")
    if not raw:
        return []
    entries = []
    for line in raw.splitlines():
        if line.startswith("\x1f"):
            _, hash, date, subject = line.split("\x1f", 3)
            entries.append({"hash": hash, "date": date, "subject": subject,
                            "is_dream": subject.startswith("dream"), "added": []})
        elif entries:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == "A":
                name = parts[1].removesuffix(".md")
                if name not in SKIP:
                    entries[-1]["added"].append(name)
    entries.reverse()
    for number, entry in enumerate(entries):
        entry["index"] = number
    return entries


def title_for(content: str) -> str:
    for line in content.splitlines():
        text = line.strip()
        if text.startswith("#"):
            return text.lstrip("# ").strip()[:120]
    return ""


def build() -> dict:
    key = cache_key()
    if CACHE["key"] == key and CACHE["data"] is not None:
        return CACHE["data"]

    appdata.current().ensure()
    commits = archive()
    first, last = {}, {}
    for commit in commits:
        for slug in commit["added"]:
            first.setdefault(slug, commit["date"])
            last[slug] = commit["date"]
    latest = len(commits) - 1 if commits else 0

    pages = sorted(p for p in wiki.wiki_dir().glob("*.md") if p.stem not in SKIP)
    nodes = {}
    for path in pages:
        slug = path.stem
        content = path.read_text(encoding="utf-8")
        stat = path.stat()
        birth = getattr(stat, "st_birthtime", None) or stat.st_ctime
        # Without git history a page counts as part of the current state, i.e. the last run.
        run = next((c["index"] for c in commits if slug in c["added"]), latest)
        nodes[slug] = {
            "id": slug,
            "title": title_for(content) or slug,
            "bytes": stat.st_size,
            "degree": 0,
            "created_at": first.get(slug) or datetime.fromtimestamp(birth, timezone.utc).isoformat(timespec="seconds"),
            "updated_at": last.get(slug) or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "dream_run": run,
            "orphan": False,
            "description": wiki.description(content),
            "out": [],
        }

    edges = []
    seen = set()
    incoming = {slug: set() for slug in nodes}
    ghosts = {}
    for path in pages:
        slug = path.stem
        if slug not in nodes:
            continue
        source_run = nodes[slug]["dream_run"]
        for target in sorted({m.group(1) for m in wiki.LINK_RE.finditer(path.read_text(encoding="utf-8"))}):
            if target == slug:
                continue
            if target in nodes:
                pair = (slug, target)
                if pair in seen:
                    continue
                seen.add(pair)
                run = max(source_run, nodes[target]["dream_run"])
                edges.append({"source": slug, "target": target, "run": run})
                nodes[slug]["out"].append(target)
                nodes[slug]["degree"] += 1
                incoming[target].add(slug)
                nodes[target]["degree"] += 1
            else:
                ghost = ghosts.setdefault(target, {"id": target, "referenced_by": [], "run": None})
                ghost["referenced_by"].append(slug)
                ghost["run"] = source_run if ghost["run"] is None else min(ghost["run"], source_run)

    for slug, sources in incoming.items():
        nodes[slug]["orphan"] = not sources
    for ghost in ghosts.values():
        ghost["title"] = ghost["id"]
        ghost["degree"] = len(ghost["referenced_by"])

    data = {
        "nodes": sorted(nodes.values(), key=lambda node: -node["degree"]),
        "edges": edges,
        "broken": sorted(ghosts.values(), key=lambda ghost: -ghost["degree"]),
        "stats": {"pages": len(nodes), "links": len(edges), "runs": max(len(commits), 1),
                  "from_git": bool(commits)},
        "run_list": [{"index": c["index"], "date": c["date"], "subject": c["subject"],
                      "is_dream": c["is_dream"]} for c in commits],
    }
    CACHE["key"] = key
    CACHE["data"] = data
    return data
