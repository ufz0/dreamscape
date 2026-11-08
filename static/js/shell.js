// Shared chrome for both pages: theme, sidebar, chat list, settings dialog.
import { api } from "./api.js";
import { icon } from "./icons.js";
import { openMenu, openDialog, toast } from "./ui.js";
import { load, save, applyTheme, greeting } from "./settings.js";

export const settings = load();

let models = [];
let defaultModel = "";
let activeChat = null;
let hooks = {};

const el = (id) => document.getElementById(id);
let ui = {};

export function currentModel() {
  return settings.model || defaultModel || "model";
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
    defaultModel = data.default;
  } catch (error) {
    toast(`Cannot reach the model server: ${error.message}`);
  }
  return models;
}

export async function startShell(options) {
  hooks = options;
  ui = {
    sidebar: el("sidebar"), scrim: el("scrim"), list: el("chat-list"), search: el("search"),
    avatar: el("avatar"), settings: el("settings"), newChat: el("new-chat"),
    collapse: el("collapse"), hamburger: el("hamburger"),
  };

  paintIcons();
  applyTheme(settings);
  applySidebar();
  ui.avatar.textContent = (settings.name || "L").charAt(0).toUpperCase();

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
  ui.settings.addEventListener("click", openSettings);
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

export function setActiveChat(id) {
  activeChat = id;
  for (const item of ui.list.querySelectorAll("li")) item.classList.toggle("active", item.dataset.id === id);
}

// ---------- chat list ----------

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

    item.append(link, menu);
    ui.list.append(item);
  }
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

export function openSettings() {
  openDialog("Settings", (card) => {
    card.append(section("Appearance", segmented(["system", "light", "dark"], settings.theme, (value) => {
      settings.theme = value;
      save(settings);
      applyTheme(settings);
    })));

    card.append(section("Name", textField(settings.name, "local", (value) => {
      settings.name = value;
      save(settings);
      ui.avatar.textContent = (settings.name || "L").charAt(0).toUpperCase();
      if (hooks.onNameChange) hooks.onNameChange(settings.name);
    })));

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

function modelSelect(onChange) {
  const select = document.createElement("select");
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    option.selected = model === currentModel();
    select.append(option);
  }
  select.addEventListener("change", () => onChange(select.value));
  if (!models.length) loadModels().then(() => renderList());
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
