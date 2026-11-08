// Shared chrome for both pages: theme, sidebar, chat list, the account popup, settings dialog.
import { api } from "./api.js";
import { icon } from "./icons.js";
import { openMenu, openPanel, openDialog, closePopups, toast } from "./ui.js";
import { load, save, applyTheme, greeting } from "./settings.js";
import { loadMe, account, mustChange, initial, readUsage, usageBlock, openChangePassword, logout } from "./account.js";

export const settings = load();

let models = [];
let groups = [];
let defaultModel = "";
let activeChat = null;
let hooks = {};

const el = (id) => document.getElementById(id);
let ui = {};

export function currentModel() {
  return settings.model || defaultModel || "model";
}

// The name a model is shown by. The id is what the server needs; a display name set by an admin is
// what a person reads, and the connection it belongs to is what tells two same-named models apart.
export function displayName(namespaced) {
  for (const group of groups) {
    const model = group.models.find((entry) => entry.namespaced === namespaced);
    if (model) return model.display_name;
  }
  return namespaced;
}

export function paintIcons() {
  for (const node of document.querySelectorAll("[data-icon]")) {
    node.innerHTML = icon(node.dataset.icon, Number(node.dataset.size) || 18);
  }
}

export async function loadModels() {
  if (models.length) return models;
  try {
    const data = await api("/api/models");
    models = data.models;
    groups = data.groups;
    defaultModel = data.default;
  } catch (error) {
    toast(`Cannot reach the model server: ${error.message}`);
  }
  return models;
}

export async function modelGroups() {
  await loadModels();
  return groups;
}

export function chooseModel(namespaced) {
  settings.model = namespaced;
  save(settings);
  if (hooks.onModelChange) hooks.onModelChange(namespaced);
}

export async function startShell(options) {
  hooks = options;
  ui = {
    sidebar: el("sidebar"), scrim: el("scrim"), list: el("chat-list"), search: el("search"),
    userMenu: el("user-menu"), avatar: el("avatar"), who: el("who"), newChat: el("new-chat"),
    collapse: el("collapse"), hamburger: el("hamburger"),
  };

  paintIcons();
  applyTheme(settings);
  applySidebar();

  try {
    await loadMe();
  } catch (error) {
    if (error.status === 401) return;   // api() already redirected to /login
    toast(`Cannot read the account: ${error.message}`);
    return;
  }

  const user = account().user;
  ui.avatar.textContent = initial(user.username);
  ui.who.textContent = user.username;

  ui.userMenu.addEventListener("click", () => openUserMenu(ui.userMenu));
  if (mustChange()) openChangePassword({ required: true });

  ui.newChat.addEventListener("click", () => {
    if (hooks.onNewChat) hooks.onNewChat();
    else window.location.href = "/";
    closeDrawer();
  });
  ui.collapse.addEventListener("click", () => {
    settings.sidebarCollapsed = !settings.sidebarCollapsed;
    save(settings);
    applySidebar();
  });
  ui.hamburger.addEventListener("click", openDrawer);
  ui.scrim.addEventListener("click", closeDrawer);
  ui.search.addEventListener("input", () => renderList());

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "o") {
      event.preventDefault();
      if (hooks.onNewChat) hooks.onNewChat();
      else window.location.href = "/";
    }
  });

  await renderList();
}

// ---------- account popup ----------

// The numbers are read again when the popup opens, so they are the current day and not what
// happened to be loaded when the page was opened.
async function openUserMenu(anchor) {
  const usage = await readUsage();
  openPanel(anchor, (panel) => {
    panel.classList.add("account-panel");
    const user = account().user;

    panel.append(usageBlock(usage, { showReset: true }));
    panel.append(document.createElement("hr"));

    for (const item of [
      { label: "Settings", action: openSettings },
      ...(user.role === "admin" ? [{ label: "Admin", action: () => { window.location.href = "/admin"; } }] : []),
      { label: "Change password", action: () => openChangePassword({ required: mustChange() }) },
      { separator: true },
      { label: "Log out", danger: true, action: logout },
    ]) {
      if (item.separator) { panel.append(document.createElement("hr")); continue; }
      const button = document.createElement("button");
      button.setAttribute("role", "menuitem");
      if (item.danger) button.classList.add("danger");
      button.textContent = item.label;
      button.addEventListener("click", () => { closePopups(); item.action(); });
      panel.append(button);
    }
  });
}

export function setActiveChat(id) {
  activeChat = id;
  if (!ui.list) return;
  for (const item of ui.list.querySelectorAll("li")) item.classList.toggle("active", item.dataset.id === id);

  // Opening a chat is what clears its dot: the same request that showed the conversation marks it
  // read on the server, so a dot still standing after this is one the user has already seen.
  const state = ui.list.querySelector(`[data-state="${cssEscape(id)}"]`);
  if (state) state.classList.remove("unread");
  setTabDot(Boolean(ui.list.querySelector(".row-state.unread")));
}

// ---------- chat list ----------

// What a chat row says about a run: the spinner while it is generating, the dot for a result that
// has arrived since the chat was last read. Opening the chat is what clears the dot, so a chat you
// are looking at does not get one.
function rowState(row) {
  const state = document.createElement("span");
  state.className = "row-state";
  state.dataset.state = row.id;
  if (row.active_run_id) {
    state.innerHTML = icon("spinner", 14);
    state.title = "Generating";
    state.setAttribute("aria-label", "Generating");
  } else if (row.unread && row.id !== activeChat) {
    state.classList.add("unread");
    state.title = "A result arrived since you last read this chat";
  }
  return state;
}

// A run ending is what turns the spinner into the dot. This is the in-place version of a list read;
// it returns false when there is no row for this chat yet — a chat that has just been created —
// which is when the caller has to read the list instead.
export function markRun(id, status) {
  if (!ui.list) return false;
  const state = ui.list.querySelector(`[data-state="${cssEscape(id)}"]`);
  if (!state) return false;

  state.classList.remove("unread");
  state.title = "";
  state.innerHTML = "";
  if (["queued", "running"].includes(status)) {
    state.innerHTML = icon("spinner", 14);
    state.title = "Generating";
  } else if (id !== activeChat) {
    state.classList.add("unread");
    state.title = "A result arrived since you last read this chat";
  }
  return true;
}

// The tab title carries the same dot, for when the page is in another window.
export function setTabDot(on) {
  document.title = on ? `• dreamscape` : "dreamscape";
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, "");
}

function dayGroup(value) {
  const date = new Date(value);
  const today = new Date();
  const day = (moment) => new Date(moment.getFullYear(), moment.getMonth(), moment.getDate());
  const offset = Math.round((day(today) - day(date)) / 86400000);
  if (offset <= 0) return "Today";
  if (offset === 1) return "Yesterday";
  if (offset <= 7) return "Previous 7 days";
  return "Older";
}

export async function renderList() {
  let data;
  try {
    data = await api("/api/conversations");
  } catch (error) {
    toast(`Cannot read the chat list: ${error.message}`);
    return;
  }

  const query = ui.search.value.trim().toLowerCase();
  ui.list.innerHTML = "";

  const header = document.createElement("div");
  header.className = "group";
  header.textContent = "Recents";
  ui.list.append(header);

  const rows = data.conversations.filter((row) => !query || row.title.toLowerCase().includes(query));
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "group";
    empty.textContent = query ? "nothing matches" : "no chats yet";
    ui.list.append(empty);
    return;
  }

  let shown = null;
  for (const row of rows) {
    const group = dayGroup(row.updated);
    if (group !== shown) {
      shown = group;
      const title = document.createElement("div");
      title.className = "group";
      title.textContent = group;
      ui.list.append(title);
    }

    const item = document.createElement("li");
    item.dataset.id = row.id;
    item.classList.toggle("active", row.id === activeChat);

    const link = document.createElement("a");
    link.className = "chat-row";
    link.href = `/?c=${encodeURIComponent(row.id)}`;
    link.textContent = row.title;
    link.title = row.title;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (hooks.onOpenChat) hooks.onOpenChat(row.id);
      else window.location.href = link.href;
      closeDrawer();
    });

    const menu = document.createElement("button");
    menu.className = "row-menu";
    menu.title = "Chat options";
    menu.setAttribute("aria-label", `Options for ${row.title}`);
    menu.innerHTML = icon("dots", 16);
    menu.addEventListener("click", () => openMenu(menu, [
      { label: "Rename", action: () => renameChat(row.id, row.title) },
      { label: "Delete", danger: true, action: () => deleteChat(row.id) },
    ]));

    item.append(link, rowState(row), menu);
    ui.list.append(item);
  }

  setTabDot(rows.some((row) => row.unread && row.id !== activeChat));
}

async function renameChat(id, current) {
  const title = window.prompt("Chat name", current);
  if (title === null) return;
  await api(`/api/conversations/${id}/rename`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }),
  });
  if (hooks.onRename) hooks.onRename(id, title.trim());
  await renderList();
}

async function deleteChat(id) {
  if (!window.confirm(`Delete "${id}"?`)) return;
  await api(`/api/conversations/${id}`, { method: "DELETE" });
  if (hooks.onDelete) hooks.onDelete(id);
  await renderList();
}

// ---------- sidebar state ----------

function applySidebar() {
  // Below 768px the sidebar is a drawer, so the rail is not used there.
  const collapsed = settings.sidebarCollapsed && window.innerWidth > 768;
  ui.sidebar.classList.toggle("collapsed", collapsed);
  // In the rail the toggle sits under the mark, so say which way it now goes.
  ui.collapse.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  ui.collapse.setAttribute("aria-label", ui.collapse.title);
}

window.addEventListener("resize", applySidebar);

function openDrawer() {
  ui.sidebar.classList.add("open");
  ui.scrim.hidden = false;
}

function closeDrawer() {
  ui.sidebar.classList.remove("open");
  ui.scrim.hidden = true;
}

// ---------- settings dialog ----------

export async function openSettings() {
  const usage = await readUsage();
  let provider = { provider: "", configured: false };
  try {
    provider = await api("/api/web");
  } catch (error) {
    // The line still says what is set, which is what a user needs here.
  }
  openDialog("Settings", (card) => {
    card.append(section("Appearance", segmented(["system", "light", "dark"], settings.theme, (value) => {
      settings.theme = value;
      save(settings);
      applyTheme(settings);
    })));

    card.append(section("Your account", usageBlock(usage, { showReset: true })));

    card.append(section("Defaults",
      controlRow("Model", modelSelect((value) => {
        settings.model = value;
        save(settings);
        if (hooks.onModelChange) hooks.onModelChange(currentModel());
      })),
      controlRow("Temperature", numberInput(settings.temperature, (value) => {
        settings.temperature = value;
        save(settings);
      })),
      controlRow("System prompt", textArea(settings.system, (value) => {
        settings.system = value;
        save(settings);
      })),
      controlRow("Name a chat from the first reply", checkbox(settings.generateTitles, (value) => {
        settings.generateTitles = value;
        save(settings);
      })),
      controlRow("Use web tools for new chats", checkbox(settings.webSearch, (value) => {
        settings.webSearch = value;
        save(settings);
      })),
      providerLine(provider),
    ));

    card.append(section("Reasoning",
      controlRow("Default effort for new chats", segmented(["none", "low", "medium", "high"], settings.reasoningEffort, (value) => {
        settings.reasoningEffort = value;
        save(settings);
        if (hooks.onEffortChange) hooks.onEffortChange(value);
      })),
      controlRow("Show reasoning by default", checkbox(settings.showReasoning, (value) => {
        settings.showReasoning = value;
        save(settings);
      })),
      controlRow("Auto-expand while thinking", checkbox(settings.autoExpandThinking, (value) => {
        settings.autoExpandThinking = value;
        save(settings);
      })),
    ));

    card.append(section("Wiki",
      controlRow("Inject wiki index", checkbox(settings.wikiIndex, (value) => {
        settings.wikiIndex = value;
        save(settings);
      })),
      dreamButton(),
    ));
  });
}

function section(title, ...fields) {
  const block = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = title;
  block.append(heading, ...fields);
  return block;
}

// Read-only: what the app is actually using for search. Nothing here changes .env.
function providerLine(provider) {
  const line = document.createElement("p");
  line.className = "muted";
  line.textContent = provider.configured
    ? `Active provider: ${provider.provider}. ${provider.rate_limit} searches per ${provider.rate_limit_minutes} minutes, results cached ${provider.cache_minutes} minutes.`
    : "No provider is configured. Set SEARCH_PROVIDER in .env, or BRAVE_API_KEY for Brave.";
  return line;
}

export function controlRow(labelText, control) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  row.append(label, control);
  return row;
}

function segmented(options, value, onChange) {
  const group = document.createElement("div");
  group.className = "segmented";
  for (const option of options) {
    const button = document.createElement("button");
    button.textContent = option;
    button.classList.toggle("on", option === value);
    button.addEventListener("click", () => {
      onChange(option);
      for (const child of group.children) child.classList.toggle("on", child === button);
    });
    group.append(button);
  }
  return group;
}

// The models a user may use, grouped by the connection they are on. A same-named model on two
// engines is two different models, so the group is part of what has to be readable here.
function modelSelect(onChange) {
  const select = document.createElement("select");

  const fill = () => {
    select.innerHTML = "";
    for (const group of groups) {
      const block = document.createElement("optgroup");
      block.label = group.connection.name;
      for (const model of group.models) {
        const option = document.createElement("option");
        option.value = model.namespaced;
        option.textContent = model.display_name;
        option.selected = model.namespaced === currentModel();
        block.append(option);
      }
      select.append(block);
    }
  };

  fill();
  select.addEventListener("change", () => onChange(select.value));
  if (!groups.length) loadModels().then(fill);
  return select;
}

function textField(value, placeholder, onChange) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  input.placeholder = placeholder;
  input.addEventListener("change", () => onChange(input.value));
  return input;
}

export function numberInput(value, onChange) {
  const input = document.createElement("input");
  input.type = "number";
  input.step = "0.1";
  input.min = "0";
  input.max = "2";
  input.value = value;
  input.style.width = "70px";
  input.addEventListener("change", () => onChange(Number(input.value)));
  return input;
}

export function textArea(value, onChange) {
  const input = document.createElement("textarea");
  input.value = value;
  input.rows = 3;
  input.style.width = "220px";
  input.addEventListener("change", () => onChange(input.value));
  return input;
}

export function checkbox(value, onChange) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(value);
  input.addEventListener("change", () => onChange(input.checked));
  return input;
}

function dreamButton() {
  const button = document.createElement("button");
  button.className = "small-button";
  button.textContent = "Dream now";
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Dreaming…";
    try {
      const result = await api("/api/dream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ include_reasoning: false, trigger: "manual" }),
      });
      toast(result.note || `dreamed ${result.transcripts.length} transcripts, nothing to commit`);
    } catch (error) {
      toast(`dream failed: ${error.message}`);
    } finally {
      button.disabled = false;
      button.textContent = "Dream now";
    }
  });
  return button;
}

export { greeting };
