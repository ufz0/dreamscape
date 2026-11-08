You are doing the cleanup pass on a personal wiki after a nightly run. The mechanical checks have
already been done by code; the findings are below. Fix them with the wiki tools.

Current wiki index:

{{index}}

Findings:

{{findings}}

What to do:
- Merge duplicate pages into one page and leave a [[link]] from the merged name.
- Repoint broken links to a page that exists, or remove them if there is nothing to link to.
- Add missing index entries, or remove entries for pages that no longer exist.
- Shorten oversize pages: keep concrete facts, drop padding. Pull orphans into the graph by linking
  them from a related page, or merge them if they should not have been a page.
- Rewrite index.md so it stays small: one line per page with a one-line description, plus the
  current goals and standing rules. Keep it under {{index_limit}} lines.

When you have finished, answer with a plain-text summary of what you changed. If nothing needed
fixing, answer with exactly: nothing to fix
