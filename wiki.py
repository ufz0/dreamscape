"""Wiki storage: sandboxed markdown pages, index sync, search, mechanical checks.

All paths are the current user's wiki folder (see appdata.py), so the sandbox is per user: a tool
call or an API request can only ever read and write inside its own user's folder.
"""
import re
from datetime import datetime, timezone

import appdata
import config

LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_-]{0,80})\]\]")
ENTRY_RE = re.compile(r"^-\s*\[\[([a-z0-9][a-z0-9_-]{0,80})\]\]\s*(?:[—–]|-)?\s*(.*)$")


def wiki_dir() -> appdata.Path:
    return appdata.current().wiki


def index_file() -> appdata.Path:
    return wiki_dir() / "index.md"


def log_file() -> appdata.Path:
    return wiki_dir() / "dream-log.md"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str | None:
    """Turn a model-supplied page name into a safe slug. Rejects traversal, absolute paths, non-md."""
    if not name:
        return None
    clean = name.strip().lower().removesuffix(".md").strip("/").replace(" ", "-")
    if not config.SLUG_RE.match(clean):
        return None
    return clean


def page_path(slug: str) -> appdata.Path | None:
    if not config.SLUG_RE.match(slug):
        return None
    wiki = wiki_dir()
    path = wiki / f"{slug}.md"
    if path.resolve().parent != wiki.resolve():
        return None
    return path


def description(content: str) -> str:
    """First usable line of a page, used for the index entry."""
    for line in content.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith(">"):
            continue
        text = re.sub(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]", r"\1", text)
        text = text.lstrip("-* ").strip()
        if text:
            return text[:120]
    return ""


def list_pages() -> list[dict]:
    """Index entries first (they carry the descriptions), then any page not in the index."""
    wiki = wiki_dir()
    entries = {}
    if index_file().exists():
        for line in index_file().read_text(encoding="utf-8").splitlines():
            match = ENTRY_RE.match(line.strip())
            if match:
                entries[match.group(1)] = (match.group(2) or "").strip()
    pages = []
    for path in sorted(wiki.glob("*.md")):
        slug = path.stem
        if slug in ("index", "dream-log"):
            continue
        pages.append({"name": slug, "description": entries.get(slug) or description(path.read_text(encoding="utf-8"))})
    return pages


def read_page(slug: str) -> str | None:
    path = page_path(slug)
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def ensure_index_entry(slug: str, content: str) -> bool:
    """Add an index entry for a new page. Does not rewrite the rest of index.md."""
    file = index_file()
    if not file.exists():
        file.write_text(f"# Wiki index\n\n## Pages\n- [[{slug}]] — {description(content)}\n", encoding="utf-8")
        return True
    lines = file.read_text(encoding="utf-8").splitlines()
    for line in lines:
        match = ENTRY_RE.match(line.strip())
        if match and match.group(1) == slug:
            return False
    entry = f"- [[{slug}]] — {description(content)}"
    try:
        position = next(i for i, line in enumerate(lines) if line.strip().lower().startswith("## pages"))
        lines.insert(position + 1, entry)
    except StopIteration:
        lines.extend(["", "## Pages", entry])
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def write_page(name: str, content: str, mode: str = "replace") -> dict:
    slug = slugify(name)
    if slug is None:
        return {"error": f"invalid page name: {name!r}. Use a lowercase slug like my-project (no path, .md only)."}
    path = page_path(slug)
    if path is None:
        return {"error": f"page name outside the wiki: {name!r}"}
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    body = content if mode == "replace" else content
    if mode == "append":
        body = (existing.rstrip() + "\n\n" + content.strip() + "\n") if existing.strip() else content.strip() + "\n"
    path.write_text(body, encoding="utf-8")
    added = ensure_index_entry(slug, body)
    return {"ok": True, "name": slug, "created": not bool(existing), "index_entry_added": added,
            "lines": len(body.splitlines()), "path": str(path.relative_to(wiki_dir()))}


def search(query: str, include_transcripts: bool = True) -> list[dict]:
    wiki = wiki_dir()
    user = appdata.current()
    query_lower = query.lower()
    results = []
    for path in sorted(wiki.glob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if query_lower in line.lower():
                results.append({"file": str(path.relative_to(wiki)), "line": number, "snippet": line.strip()[:200]})
                if len(results) >= config.SEARCH_LIMIT:
                    return results
    if include_transcripts:
        for folder in (user.processed, user.inbox):
            for path in sorted(folder.glob("*.jsonl")):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if query_lower in line.lower():
                        results.append({"file": f"transcripts/{folder.name}/{path.name}", "line": number,
                                       "snippet": line.strip()[:200]})
                        if len(results) >= config.SEARCH_LIMIT:
                            return results
    return results


def backlinks(slug: str) -> list[str]:
    wiki = wiki_dir()
    out = []
    for path in sorted(wiki.glob("*.md")):
        if path.stem == slug:
            continue
        content = path.read_text(encoding="utf-8")
        if slug in {match.group(1) for match in LINK_RE.finditer(content)}:
            out.append(path.stem)
    return out


def index_text() -> str:
    file = index_file()
    if not file.exists():
        return "# Wiki index\n\n(empty wiki)"
    lines = file.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[:config.INDEX_MAX_LINES])


def checks() -> dict:
    """Mechanical checks done in code, not by the model."""
    wiki = wiki_dir()
    index = index_file().read_text(encoding="utf-8") if index_file().exists() else ""
    indexed = {m.group(1) for line in index.splitlines() if (m := ENTRY_RE.match(line.strip()))}
    pages = [p for p in sorted(wiki.glob("*.md")) if p.stem not in ("index", "dream-log")]
    incoming = {p.stem: set() for p in pages}
    broken = []
    oversize = []
    for page in pages:
        content = page.read_text(encoding="utf-8")
        for target in {m.group(1) for m in LINK_RE.finditer(content)}:
            if not (wiki / f"{target}.md").exists():
                broken.append({"page": page.stem, "link": target})
            if target in incoming:
                incoming[target].add(page.stem)
        if len(content.splitlines()) > config.PAGE_MAX_LINES:
            oversize.append({"page": page.stem, "lines": len(content.splitlines())})
    unindexed = [p.stem for p in pages if p.stem not in indexed]
    orphans = [stem for stem, sources in incoming.items() if not sources and stem not in indexed]
    return {
        "pages": len(pages),
        "total_bytes": sum(p.stat().st_size for p in pages) + (index_file().stat().st_size if index_file().exists() else 0),
        "index_lines": len(index.splitlines()),
        "index_limit": config.INDEX_MAX_LINES,
        "broken_links": broken,
        "orphans": orphans,
        "unindexed": unindexed,
        "oversize": oversize,
    }
