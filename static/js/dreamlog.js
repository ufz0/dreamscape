// Dream log: the list of dreaming runs and the detail of one run.
// Rows are compact and collapsed, like the tool rows in a chat; expanding one shows the diff or the
// tool arguments and result. A running run streams in through /api/dreams/live.
import { api, escapeHtml } from "./api.js";
import { icon } from "./icons.js";

const el = (id) => document.getElementById(id);

const ICONS = { read: "book", search: "search", write: "plus", fix: "wrench", error: "retry", note: "check" };
const ACTION_ICONS = { created: "plus", updated: "pencil", merged: "pencil", deleted: "x" };
const FILTERS = [
  { key: "all", label: "All" },
  { key: "created", label: "Created" },
  { key: "updated", label: "Updated" },
  { key: "deleted", label: "Deleted" },
  { key: "errors", label: "Errors" },
];

const STATUS_LABEL = { running: "running", success: "success", partial: "partial", failed: "failed", legacy: "legacy" };

function timeAgo(value) {
  if (!value) return "";
  const then = new Date(value).getTime();
  const minutes = Math.max(1, Math.round((Date.now() - then) / 60000));
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h ago`;
  return `${Math.round(minutes / 1440)}d ago`;
}

function stamp(value) {
  if (!value) return "";
  const date = new Date(value);
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

export class DreamLogView {
  constructor(options) {
    this.runs = el("dream-runs");
    this.head = el("dream-detail-head");
    this.filters = el("dream-filters");
    this.timeline = el("dream-timeline");
    this.status = el("dream-status");
    this.onOpenPage = options.onOpenPage;
    this.onDream = options.onDream;
    this.selected = null;
    this.filter = "all";
    this.query = "";
    this.source = null;
    this.atBottom = true;
    this.buildFilters();
  }

  buildFilters() {
    this.filters.innerHTML = "";
    for (const filter of FILTERS) {
      const button = document.createElement("button");
      button.textContent = filter.label;
      button.dataset.filter = filter.key;
      button.classList.toggle("on", filter.key === this.filter);
      button.addEventListener("click", () => {
        this.filter = filter.key;
        for (const child of this.filters.querySelectorAll("button[data-filter]")) {
          child.classList.toggle("on", child.dataset.filter === filter.key);
        }
        this.applyFilter();
      });
      this.filters.append(button);
    }

    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Search this run";
    search.setAttribute("aria-label", "Search this run");
    search.addEventListener("input", () => { this.query = search.value.trim().toLowerCase(); this.applyFilter(); });
    this.filters.append(search);

    const toggle = document.createElement("button");
    toggle.className = "toggle";
    toggle.textContent = "Expand all";
    toggle.addEventListener("click", () => {
      const open = toggle.textContent === "Expand all";
      for (const row of this.timeline.querySelectorAll(".log-row")) {
        row.classList.toggle("open", open);
        const button = row.querySelector(".row-toggle");
        if (button) button.setAttribute("aria-expanded", String(open));
      }
      toggle.textContent = open ? "Collapse all" : "Expand all";
    });
    this.filters.append(toggle);
  }

  async connect() {
    if (this.source) return;
    this.source = new EventSource("/api/dreams/live");
    this.source.addEventListener("message", (raw) => {
      let event;
      try { event = JSON.parse(raw.data); } catch (error) { return; }
      if (event.type === "ping") return;
      this.handleLive(event);
    });
  }

  async load() {
    let data;
    try {
      data = await api("/api/dreams");
    } catch (error) {
      this.timeline.innerHTML = `<p class="muted">cannot read the dream log: ${escapeHtml(error.message)}</p>`;
      return;
    }
    this.runs.innerHTML = "";
    if (!data.runs.length) {
      this.runs.append(this.emptyState());
      this.showEmpty();
      return;
    }

    for (const run of data.runs) this.runs.append(this.runRow(run));

    const preferred = new URLSearchParams(location.search).get("run") || this.selected || data.runs[0].id;
    await this.select(preferred);
  }

  emptyState() {
    const box = document.createElement("div");
    box.className = "log-empty";
    box.innerHTML = `<p class="muted">No dreaming runs yet.</p>`;
    const button = document.createElement("button");
    button.className = "small-button";
    button.textContent = "Dream now";
    button.addEventListener("click", () => this.onDream());
    box.append(button);
    return box;
  }

  showEmpty() {
    this.head.innerHTML = "";
    this.timeline.innerHTML = "";
    this.timeline.append(this.emptyState());
  }

  async select(id) {
    let record;
    try {
      record = await api(`/api/dreams/${encodeURIComponent(id)}`);
    } catch (error) {
      this.head.innerHTML = "";
      this.timeline.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
      return;
    }
    this.selected = id;
    for (const row of this.runs.querySelectorAll(".run-row")) row.classList.toggle("active", row.dataset.id === id);
    const params = new URLSearchParams(location.search);
    params.set("view", "dream");
    params.set("run", id);
    history.replaceState(null, "", `?${params}`);

    this.renderHead(record);
    this.timeline.innerHTML = "";
    this.renderTimeline(record);
  }

  renderHead(record) {
    this.head.innerHTML = "";
    const title = document.createElement("h2");
    title.textContent = stamp(record.started) || record.id;
    const badge = document.createElement("span");
    badge.className = `status ${escapeHtml(record.status || "")}`;
    badge.textContent = STATUS_LABEL[record.status] || record.status;
    title.append(badge);

    const facts = [
      record.trigger && `trigger: ${record.trigger}`,
      record.model && `model: ${record.model}`,
      record.reasoning_effort && Object.entries(record.reasoning_effort)
        .map(([phase, level]) => `effort ${phase}: ${level}`).join(", "),
      duration(record.duration_seconds),
      record.usage && Object.entries(record.usage).map(([key, value]) => `${key}: ${value}`).join(" "),
      record.transcripts.length ? `${record.transcripts.length} transcripts` : "",
    ].filter(Boolean);
    const line = document.createElement("p");
    line.className = "muted";
    line.textContent = facts.join(" · ");
    if (record.commit) {
      const link = document.createElement("a");
      link.href = `https://github.com/ufz0/dreamscape`;
      link.textContent = `commit ${record.commit}`;
      link.title = "The wiki is a local git repo; this is the commit hash";
      line.append(document.createTextNode(" · "), link);
    }

    const chips = document.createElement("div");
    chips.className = "chips";
    const touched = new Map();
    for (const group of [...(record.transcripts || []), { steps: (record.cleanup?.steps || []) }]) {
      for (const step of group.steps || []) {
        if (step.type !== "write" || !step.page) continue;
        touched.set(step.page, step.action || "updated");
      }
    }
    if (!touched.size) chips.innerHTML = `<span class="chip none">nothing changed</span>`;
    for (const [page, action] of touched) {
      const chip = document.createElement("a");
      chip.className = `chip ${action}`;
      chip.textContent = `${action === "created" ? "+" : action === "deleted" ? "−" : "~"} ${page}`;
      chip.href = `?p=${encodeURIComponent(page)}&view=list`;
      chip.addEventListener("click", (event) => {
        event.preventDefault();
        this.onOpenPage(page);
      });
      chips.append(chip);
    }

    // The status line is for a run in progress; a finished run keeps whatever the last live
    // message said, so the message does not disappear when the detail is reloaded.
    if (record.status === "running") this.status.textContent = liveLine(record);
    this.head.append(title, line, chips);
  }

  renderTimeline(record) {
    for (const group of record.transcripts || []) {
      this.renderGroup(group.file, group.note, group.steps, group);
    }
    if (record.cleanup) {
      this.renderGroup("Cleanup", record.cleanup.note, record.cleanup.steps, { phase: "cleanup" });
    }
    this.applyFilter();
  }

  renderGroup(title, note, steps, group) {
    const block = document.createElement("section");
    block.className = "log-group";
    const heading = document.createElement("h3");
    heading.textContent = group.phase === "cleanup" ? "Cleanup" : `Transcript · ${title}`;
    block.append(heading);
    if (note) {
      const why = document.createElement("p");
      why.className = "log-note";
      why.textContent = note;
      block.append(why);
    }
    if (!steps || !steps.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = group.phase === "cleanup" ? "nothing to fix" : "no steps recorded";
      block.append(empty);
      return;
    }
    for (const step of steps) block.append(this.row(step, group));
    this.timeline.append(block);
  }

  row(step, group) {
    const row = document.createElement("div");
    row.className = `log-row ${step.type}`;
    row.dataset.action = step.action || "";
    row.dataset.type = step.type;
    row.dataset.search = `${step.page || ""} ${step.summary || ""} ${step.diff || ""} ${JSON.stringify(step.args || {})}`.toLowerCase();

    const head = document.createElement("div");
    head.className = "log-row-head";

    const toggle = document.createElement("button");
    toggle.className = "row-toggle";
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.title = "Show this step";
    const glyph = document.createElement("span");
    glyph.className = "glyph";
    glyph.innerHTML = icon(ACTION_ICONS[step.action] || ICONS[step.type] || "check", 15);
    toggle.append(glyph);
    // The word is only shown when it adds something: the action for a write, otherwise the tool.
    if (step.action) {
      const action = document.createElement("span");
      action.className = "action";
      action.textContent = step.action;
      toggle.append(action);
    }

    const name = document.createElement(step.page ? "a" : "span");
    name.className = "page" + (step.page ? "" : " none");
    name.textContent = step.page || step.type;
    if (step.page) {
      name.href = `?p=${encodeURIComponent(step.page)}`;
      name.title = `Open ${step.page}`;
      name.addEventListener("click", (event) => {
        event.preventDefault();
        this.onOpenPage(step.page);
      });
    }

    const summary = document.createElement("span");
    summary.className = "summary";
    summary.textContent = step.summary || "";

    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = (step.duration_ms ? `${step.duration_ms}ms` : "") +
      (step.attempt ? ` attempt ${step.attempt}` : "");

    head.append(toggle, name, summary, meta);

    const body = document.createElement("div");
    body.className = "log-row-body";
    if (step.args && Object.keys(step.args).length) body.append(part("arguments", JSON.stringify(step.args, null, 2)));
    if (step.diff) body.append(this.diff(step.diff, step.truncated));
    if (step.links) body.append(part("links", step.links.join("\n")));

    const open = () => {
      row.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(row.classList.contains("open")));
    };
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      open();
    });
    head.addEventListener("click", (event) => { if (event.target !== name) open(); });

    row.append(head);
    if (body.childNodes.length) row.append(body);
    return row;
  }

  diff(text, truncated) {
    const box = document.createElement("div");
    box.className = "diff";
    for (const line of text.split("\n")) {
      const row = document.createElement("div");
      if (line.startsWith("+") && !line.startsWith("+++")) row.className = "add";
      else if (line.startsWith("-") && !line.startsWith("---")) row.className = "del";
      else if (line.startsWith("@@")) row.className = "hunk";
      row.textContent = line;
      box.append(row);
    }
    if (truncated) {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "diff capped";
      box.append(note);
    }
    return box;
  }

  applyFilter() {
    for (const row of this.timeline.querySelectorAll(".log-row")) {
      const matchesType = this.filter === "all" ||
        (this.filter === "errors" && row.dataset.type === "error") ||
        row.dataset.action === this.filter;
      const matchesQuery = !this.query || row.dataset.search.includes(this.query);
      row.hidden = !(matchesType && matchesQuery);
    }
    for (const group of this.timeline.querySelectorAll(".log-group")) {
      group.hidden = !group.querySelectorAll(".log-row:not([hidden])").length;
    }
  }

  handleLive(event) {
    if (event.type === "state" && event.run) {
      if (!this.selected || event.run.id === this.selected) this.select(event.run.id);
      return;
    }
    if (event.type === "start") {
      // A run started while we are looking at another one, or at nothing.
      this.selected = event.id;
      for (const row of this.runs.querySelectorAll(".run-row")) row.classList.toggle("active", row.dataset.id === event.id);
      this.renderHead({ id: event.id, started: new Date().toISOString(), status: "running",
                        trigger: "", model: "", reasoning_effort: {}, usage: null, commit: null,
                        transcripts: [], cleanup: { steps: [] } });
      this.status.textContent = `Run ${event.id} started, ${event.transcripts} transcripts.`;
      return;
    }
    if (event.id && event.id !== this.selected) return;
    if (event.type === "transcript") {
      this.status.textContent = `Processing transcript ${event.position} of ${event.total}…`;
      this.appendGroup(event.file);
      if (event.note) {
        const last = this.timeline.lastElementChild;
        const note = last?.querySelector(".log-note");
        if (note) note.textContent = event.note;
      }
    } else if (event.type === "step") {
      const group = this.groupFor(event);
      group.append(this.row(event.step, { phase: event.phase }));
      this.applyFilter();
      this.scroll();
    } else if (event.type === "error") {
      const group = this.groupFor(event);
      group.append(this.row({ type: "error", summary: event.message, at: new Date().toISOString() }, {}));
      this.status.textContent = `Error: ${event.message}`;
      this.scroll();
    } else if (event.type === "finished") {
      this.status.textContent = event.note || "Run finished.";
      this.refreshRuns();
    }
  }

  async refreshRuns() {
    // Rebuild the left column only, so the status line the run just wrote stays visible.
    let data;
    try {
      data = await api("/api/dreams");
    } catch (error) {
      return;
    }
    this.runs.innerHTML = "";
    this.runs.append(this.emptyState());
    for (const run of data.runs) this.runs.append(this.runRow(run));
    if (!this.selected && data.runs.length) this.select(data.runs[0].id);
  }

  runRow(run) {
    const item = document.createElement("button");
    item.className = "run-row";
    item.dataset.id = run.id;
    item.classList.toggle("active", run.id === this.selected);
    item.innerHTML = `
        <span class="status ${escapeHtml(run.status || "")}">${escapeHtml(STATUS_LABEL[run.status] || run.status)}</span>
        <span class="when">${escapeHtml(stamp(run.started))}</span>
        <span class="counts">${escapeHtml(countsLine(run))}</span>
        <span class="meta">${escapeHtml(duration(run.duration_seconds))}${
      run.transcripts ? ` · ${run.transcripts} transcripts` : ""}</span>`;
    item.addEventListener("click", () => this.select(run.id));
    return item;
  }

  groupFor(event) {
    const wanted = event.phase === "cleanup" ? "Cleanup" : `Transcript · ${event.transcript}`;
    for (const group of this.timeline.querySelectorAll(".log-group")) {
      if (group.querySelector("h3").textContent === wanted) return group;
    }
    return this.appendGroup(event.transcript, event.phase);
  }

  appendGroup(title, phase) {
    const block = document.createElement("section");
    block.className = "log-group";
    const heading = document.createElement("h3");
    heading.textContent = phase === "cleanup" ? "Cleanup" : `Transcript · ${title}`;
    const note = document.createElement("p");
    note.className = "log-note";
    block.append(heading, note);
    this.timeline.append(block);
    return block;
  }

  scroll() {
    const box = this.timeline.closest(".dream-detail");
    if (this.atBottom) box.scrollTop = box.scrollHeight;
  }

  show() {
    const box = this.timeline.closest(".dream-detail");
    this.atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  }
}

function part(label, value) {
  const section = document.createElement("div");
  section.className = "section";
  const strong = document.createElement("strong");
  strong.textContent = label;
  const text = document.createElement("div");
  text.textContent = value;
  section.append(strong, text);
  return section;
}

function countsLine(run) {
  const counts = run.counts || {};
  const parts = [];
  if (counts.created) parts.push(`+${counts.created} pages`);
  if (counts.updated) parts.push(`~${counts.updated} updated`);
  if (counts.deleted) parts.push(`−${counts.deleted} deleted`);
  if (counts.links_fixed) parts.push(`${counts.links_fixed} links fixed`);
  if (counts.errors) parts.push(`${counts.errors} errors`);
  return parts.join(", ") || "nothing changed";
}

function liveLine(record) {
  if (record.status !== "running") return "";
  const done = record.transcripts.length;
  return `Processing transcript ${done} …`;
}

// The short line in the wiki header: last run, how long ago, what it changed.
export async function lastDreamLine(target) {
  try {
    const data = await api("/api/dreams");
    const run = data.runs[0];
    if (!run) { target.hidden = true; return; }
    target.hidden = false;
    target.href = `?view=dream&run=${encodeURIComponent(run.id)}`;
    target.textContent = `Last dream: ${timeAgo(run.started)}, ${countsLine(run)}`;
    target.title = `Run ${run.id}`;
  } catch (error) {
    target.hidden = true;
  }
}
