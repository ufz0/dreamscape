// Wiki viewer: page list, rendered page with [[links]], backlinks, search, edit, dream trigger,
// the graph view (full graph + a small local graph for the page being read), and the dream log.
import { api, escapeHtml } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { startShell } from "./shell.js";
import { GraphView } from "./graph.js";
import { DreamLogView, lastDreamLine } from "./dreamlog.js";

const el = (id) => document.getElementById(id);
const ui = {
  pages: el("pages"), body: el("page-body"), backlinks: el("backlinks"), name: el("page-name"),
  editor: el("editor"), save: el("save"), query: el("query"), status: el("status"),
  stats: el("stats"), dream: el("dream"), list: el("wiki-main"), graph: el("graph-view"),
  tabList: el("tab-list"), tabGraph: el("tab-graph"), tabDream: el("tab-dream"),
  dreamView: el("dream-view"), lastDream: el("last-dream"),
  localGraph: el("local-graph"), localBox: el("local-graph-box"),
};

const VIEW_KEY = "dreamscape.wikiView";
let current = null;
let graph = null;
let localGraph = null;
let dreamLog = null;

function showView(which) {
  ui.list.hidden = which !== "list";
  ui.graph.hidden = which !== "graph";
  ui.dreamView.hidden = which !== "dream";
  for (const [tab, name] of [[ui.tabList, "list"], [ui.tabGraph, "graph"], [ui.tabDream, "dream"]]) {
    tab.classList.toggle("on", name === which);
    tab.setAttribute("aria-selected", String(name === which));
  }
  localStorage.setItem(VIEW_KEY, which);
  const params = new URLSearchParams(location.search);
  params.set("view", which);
  if (current) params.set("p", current);
  history.replaceState(null, "", `?${params}`);
  if (which === "graph" && graph) graph.show();
  if (which === "list" && localGraph && ui.localGraph.open) localGraph.show();
  if (which === "dream" && dreamLog) dreamLog.show();
}

async function openGraph() {
  if (!graph) {
    graph = new GraphView(ui.graph, {
      onOpen: (slug) => { showView("list"); showPage(slug); },
      onEdit: async (slug) => {
        showView("list");
        await showPage(slug);
        ui.localGraph.open = true;
        el("editor").focus();
      },
    });
    graph.show();
    const loaded = await graph.load();
    if (!loaded) return;
    if (current) graph.select(current);
  }
  showView("graph");
}

async function openDreamLog() {
  if (!dreamLog) {
    dreamLog = new DreamLogView({
      onOpenPage: (slug) => { showView("list"); showPage(slug); },
      onDream: () => runDream(),
    });
    await dreamLog.connect();
  }
  showView("dream");
  await dreamLog.load();
}

ui.tabList.addEventListener("click", () => showView("list"));
ui.tabGraph.addEventListener("click", () => openGraph());
ui.tabDream.addEventListener("click", () => openDreamLog());

ui.localGraph.addEventListener("toggle", async () => {
  if (!ui.localGraph.open || !current) return;
  if (!localGraph) localGraph = new GraphView(ui.localBox, { compact: true });
  localGraph.show();
  const data = await api(`/api/wiki/page?name=${encodeURIComponent(current)}`);
  await localGraph.load();
  localGraph.setSubset([current, ...data.backlinks, ...outgoingLinks(data.content)]);
});

function outgoingLinks(content) {
  return [...new Set([...content.matchAll(/\[\[([a-z0-9][a-z0-9_-]{0,80})\]\]/g)].map((m) => m[1]))];
}

async function loadList() {
  let data;
  try {
    data = await api("/api/wiki/list");
  } catch (error) {
    ui.status.textContent = error.message;
    return;
  }

  ui.pages.innerHTML = "";
  for (const page of data.pages) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = `?p=${encodeURIComponent(page.name)}`;
    link.textContent = page.name;
    link.classList.toggle("active", page.name === current);
    const description = document.createElement("span");
    description.className = "muted";
    description.textContent = page.description ? ` — ${page.description.slice(0, 60)}` : "";
    item.append(link, description);
    ui.pages.append(item);
  }

  const checks = data.checks;
  const problems = [];
  if (checks.broken_links.length) problems.push(`broken links: ${checks.broken_links.map((b) => `${b.page}→${b.link}`).join(", ")}`);
  if (checks.orphans.length) problems.push(`orphans: ${checks.orphans.join(", ")}`);
  if (checks.unindexed.length) problems.push(`not in index: ${checks.unindexed.join(", ")}`);
  if (checks.oversize.length) problems.push(`oversize: ${checks.oversize.map((o) => `${o.page} (${o.lines})`).join(", ")}`);
  ui.stats.textContent = `${checks.pages} pages · ${checks.total_bytes} bytes · index ${checks.index_lines}/${checks.index_limit} lines` +
    (problems.length ? `\n${problems.join("\n")}` : "");
}

async function showPage(name) {
  current = name;
  let data;
  try {
    data = await api(`/api/wiki/page?name=${encodeURIComponent(name)}`);
  } catch (error) {
    ui.status.textContent = error.message;
    return;
  }

  ui.name.textContent = data.name;
  ui.body.innerHTML = renderMarkdown(data.content, { wikiLinks: true });
  ui.editor.value = data.content;

  ui.backlinks.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = "linked from: ";
  ui.backlinks.append(title);
  if (!data.backlinks.length) ui.backlinks.append(document.createTextNode("nothing yet"));
  for (const source of data.backlinks) {
    const link = document.createElement("a");
    link.href = `?p=${encodeURIComponent(source)}`;
    link.textContent = source;
    ui.backlinks.append(link, document.createTextNode(" "));
  }

  for (const link of ui.body.querySelectorAll("a.wikilink")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      showPage(link.getAttribute("href").replace("?p=", ""));
    });
  }

  const params = new URLSearchParams(location.search);
  params.set("p", name);
  params.set("view", localStorage.getItem(VIEW_KEY) || "list");
  history.replaceState(null, "", `?${params}`);

  await loadList();
  if (localGraph && ui.localGraph.open) localGraph.setSubset([...new Set([name, ...data.backlinks, ...outgoingLinks(data.content)])]);
  if (graph && graph.nodes.length) graph.select(name);
}

async function runSearch() {
  const query = ui.query.value.trim();
  if (!query) return;

  const data = await api(`/api/wiki/search?q=${encodeURIComponent(query)}`);
  current = null;
  ui.name.textContent = `search: ${query}`;
  ui.backlinks.innerHTML = "";
  ui.body.innerHTML = "";

  const list = document.createElement("ul");
  list.className = "results";
  if (!data.results.length) list.append(Object.assign(document.createElement("li"), { textContent: "no matches" }));
  for (const hit of data.results) {
    const item = document.createElement("li");
    item.innerHTML = `<span class="hit">${escapeHtml(hit.file)}:${hit.line}</span> ${escapeHtml(hit.snippet)}`;
    list.append(item);
  }
  ui.body.append(list);
}

ui.save.addEventListener("click", async () => {
  try {
    const result = await api("/api/wiki/page", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: current, content: ui.editor.value }),
    });
    ui.status.textContent = result.index_entry_added ? "saved, index entry added" : "saved";
    await showPage(current);
  } catch (error) {
    ui.status.textContent = `save failed: ${error.message}`;
  }
});

async function runDream() {
  ui.dream.disabled = true;
  ui.dream.textContent = "Dreaming…";
  try {
    const result = await api("/api/dream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_reasoning: false, trigger: "button" }),
    });
    ui.status.textContent = result.note ||
      `dreamed ${result.transcripts.length} transcripts${result.commit ? `, committed ${result.commit}` : ", nothing to commit"}`;
    await loadList();
    if (graph) await graph.load();
    if (dreamLog) await dreamLog.load();
    lastDreamLine(ui.lastDream);
  } catch (error) {
    ui.status.textContent = `dream failed: ${error.message}`;
  } finally {
    ui.dream.disabled = false;
    ui.dream.textContent = "Dream now";
  }
}

ui.dream.addEventListener("click", runDream);

document.getElementById("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

document.addEventListener("click", (event) => {
  const button = event.target.closest(".copy[data-copy]");
  if (!button) return;
  const code = button.closest("pre").querySelector("code");
  navigator.clipboard?.writeText(code.textContent);
  button.classList.add("copied");
  button.textContent = "Copied";
  setTimeout(() => {
    button.classList.remove("copied");
    button.textContent = "Copy";
  }, 2000);
});

startShell({
  onNewChat: () => { window.location.href = "/"; },
  onOpenChat: (id) => { window.location.href = `/?c=${encodeURIComponent(id)}`; },
});

async function start() {
  const params = new URLSearchParams(location.search);
  const page = params.get("p") || "index";
  await showPage(page);
  lastDreamLine(ui.lastDream);
  const view = params.get("view") || localStorage.getItem(VIEW_KEY);
  if (view === "graph") await openGraph();
  if (view === "dream") await openDreamLog();
}

start();
