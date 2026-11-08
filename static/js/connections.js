// The connections an admin manages: what an engine is, which models it has, and what a test found.
// Secrets are write-only here — the API returns only whether one is set and what it ends with — so
// the only way to change a stored key is to write a new one.
//
// A test reports each check on its own, because an engine can answer plainly and still not do tool
// calls, or stream without reporting usage. That is why the results are rows rather than one verdict.
import { api, escapeHtml } from "./api.js";
import { icon } from "./icons.js";
import { openMenu, openDialog, toast } from "./ui.js";

const CHECKS = [
  ["models", "Model list"], ["chat", "Plain chat"], ["stream", "Streaming"],
  ["tools", "Tool calls"], ["usage", "Usage"], ["reasoning", "Reasoning field"],
];
const FLAGS = [
  ["supports_vision", "vision"], ["supports_tools", "tools"],
  ["supports_reasoning", "reasoning"], ["supports_usage", "usage"],
];

let say = () => {};
let container = null;
let rows = [];
let presets = {};

export function init(statusLine, target) {
  say = statusLine;
  container = target;
}

export async function load() {
  const data = await api("/api/connections");
  rows = data.connections;
  presets = data.presets;
  render();
}

// ---------- the list ----------

function render() {
  container.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No connection yet. Add one, or start the app with STRATA_BASE_URL and STRATA_API_KEY.";
    container.append(empty);
    return;
  }
  for (const connection of rows) container.append(card(connection));
}

function card(connection) {
  const card = document.createElement("article");
  card.className = "log-row conn";
  card.dataset.id = connection.id;

  const head = document.createElement("div");
  head.className = "log-row-head";

  const toggle = document.createElement("button");
  toggle.className = "row-toggle";
  toggle.setAttribute("aria-expanded", "false");
  toggle.innerHTML = `${icon("chevron", 15)}<span class="name">${escapeHtml(connection.name)}</span>`;
  toggle.addEventListener("click", () => {
    const open = card.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  const url = document.createElement("span");
  url.className = "value";
  url.textContent = connection.base_url;

  head.append(toggle, url, badge(connection.enabled ? "on" : "off", connection.enabled ? "success" : ""),
    badge(connection.visibility === "admins" ? "admins only" : "all users", "role"),
    queueState(connection));

  const test = document.createElement("button");
  test.className = "small-button";
  test.textContent = "Test connection";
  test.title = "Run each check on its own and keep the result";
  test.addEventListener("click", async () => {
    test.disabled = true;
    test.textContent = "Testing…";
    try {
      const result = await api(`/api/connections/${connection.id}/test`, {
        method: "POST", body: JSON.stringify({}),
      });
      Object.assign(connection, result);
      say(`Tested ${connection.name}. ${summary(result.test_result)}`);
    } catch (error) {
      say(`${connection.name}: ${error.message}`, true);
    } finally {
      test.disabled = false;
      test.textContent = "Test connection";
      render();
    }
  });

  head.append(test, rowMenu(connection));

  const body = document.createElement("div");
  body.className = "log-row-body";
  body.append(checkRows(connection), fields(connection), modelTable(connection), addModelRow(connection));

  card.append(head, body);
  if (connection.last_test) card.classList.add("open");   // a test is worth reading without a click
  return card;
}

// One line per check: what was asked, what came back, how long it took.
function checkRows(connection) {
  const block = document.createElement("div");
  block.className = "checks";
  const result = (connection.test_result || {}).checks || {};

  for (const [key, label] of CHECKS) {
    const check = result[key];
    const row = document.createElement("div");
    row.className = "check";

    const name = document.createElement("span");
    name.className = "label";
    name.textContent = label;

    const kind = { ok: "success", partial: "partial", failed: "failed" }[check?.status] || "";
    row.append(name, badge(check ? check.status : "not tested", kind));

    if (check?.error) {
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = check.error;
      row.append(detail);
    } else if (check && check.status === "ok") {
      const detail = document.createElement("span");
      detail.className = "detail";
      detail.textContent = checkDetail(check);
      row.append(detail);
    }

    if (check?.ms) {
      const ms = document.createElement("span");
      ms.className = "meta";
      ms.textContent = `${check.ms} ms`;
      row.append(ms);
    }
    block.append(row);
  }
  return block;
}

function checkDetail(check) {
  if (check.count !== undefined) return `${check.count} models`;
  if (check.fields?.length) return check.fields.join(", ");
  if (check.stream !== undefined) return `${check.stream ? "stream" : "no stream usage"}, ${check.non_stream ? "plain" : "no plain usage"}`;
  if (check.tool_calls) return "a tool call came back";
  return check.text ? "text came back" : "";
}

// One line for what a connection is set to. Values the API does not return are not invented here.
function fields(connection) {
  const block = document.createElement("div");
  block.className = "conn-fields";
  for (const [label, value] of [
    ["preset", presets[connection.preset] || connection.preset],
    ["api key", connection.api_key?.set ? `set, ends ${connection.api_key.hint}` : "not set"],
    ["headers", Object.keys(connection.headers || {}).length
      ? Object.entries(connection.headers).map(([name, value]) => `${name}: ${value.set ? `set, ends ${value.hint}` : "not set"}`).join(", ")
      : "none"],
    ["paths", `${connection.path_models} and ${connection.path_chat}`],
    ["timeout", `${connection.timeout_seconds} s`],
    ["queue", `up to ${connection.max_concurrent} at a time`],
    ["body fields", Object.keys(connection.body_overrides || {}).length ? JSON.stringify(connection.body_overrides) : "none"],
    ["reasoning map", Object.keys(connection.reasoning_map || {}).length ? JSON.stringify(connection.reasoning_map) : "none"],
    ["last tested", connection.last_test || "never"],
  ]) {
    const row = document.createElement("div");
    row.className = "conn-field";
    row.append(Object.assign(document.createElement("span"), { className: "label", textContent: label }),
      Object.assign(document.createElement("span"), { className: "value", textContent: String(value) }));
    block.append(row);
  }
  return block;
}

function modelTable(connection) {
  const table = document.createElement("table");
  table.className = "admin-table conn-models";
  table.setAttribute("aria-label", `Models on ${connection.name}`);
  table.innerHTML = `<thead><tr><th>Model id</th>${FLAGS.map((flag) => `<th>${flag[1]}</th>`).join("")}` +
    `<th>context</th><th>shown</th><th></th></tr></thead>`;

  const body = document.createElement("tbody");
  const models = connection.models || [];
  if (!models.length) {
    body.innerHTML = `<tr><td class="value" colspan="${FLAGS.length + 4}">no model on this connection</td></tr>`;
  }

  for (const model of models) {
    const row = document.createElement("tr");
    row.dataset.model = model.row_id;

    const id = document.createElement("td");
    id.className = "name";
    id.textContent = model.namespaced;
    if (model.display_name !== model.id) id.title = `Shown as ${model.display_name}`;

    const cells = [id, ...FLAGS.map((flag) => flagCell(model[flag[0]]))];
    const context = document.createElement("td");
    context.className = "value";
    context.textContent = model.context_length || "—";
    cells.push(context);

    const shown = document.createElement("td");
    shown.className = "value";
    shown.textContent = model.hidden ? "hidden" : "shown";
    cells.push(shown);

    const actions = document.createElement("td");
    const edit = document.createElement("button");
    edit.className = "small-button";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => editModel(model));
    const remove = document.createElement("button");
    remove.className = "small-button danger";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => removeModel(model));
    actions.append(edit, remove);
    cells.push(actions);

    row.append(...cells);
    body.append(row);
  }

  table.append(body);
  return table;
}

// A capability flag that nobody has checked is shown as unknown, which is a different thing from a
// model that was tested and cannot do it.
function flagCell(value) {
  const cell = document.createElement("td");
  cell.className = "value flag";
  if (value === null || value === undefined) {
    cell.classList.add("unknown");
    cell.textContent = "?";
    cell.title = "not tested";
  } else if (value) {
    cell.classList.add("yes");
    cell.textContent = "yes";
  } else {
    cell.textContent = "no";
  }
  return cell;
}

function addModelRow(connection) {
  const row = document.createElement("div");
  row.className = "conn-actions";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "model id, for a server that does not list them";
  input.setAttribute("aria-label", `Model id to add to ${connection.name}`);

  const add = document.createElement("button");
  add.className = "small-button";
  add.textContent = "Add model";
  add.addEventListener("click", async () => {
    const id = input.value.trim();
    if (!id) return;
    try {
      const created = await api(`/api/connections/${connection.id}/models`, {
        method: "POST", body: JSON.stringify({ model_id: id }),
      });
      input.value = "";
      say(`Added ${created.namespaced}.`);
      await load();
    } catch (error) { say(`${connection.name}: ${error.message}`, true); }
  });

  const refresh = document.createElement("button");
  refresh.className = "small-button";
  refresh.textContent = "Read the model list";
  refresh.title = "Ask this connection for its models. Flags an admin set are kept.";
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    refresh.textContent = "Reading…";
    try {
      const result = await api(`/api/connections/${connection.id}/models/refresh`, { method: "POST" });
      say(`${connection.name}: ${result.added} added, ${result.seen} still listed.`);
      await load();
    } catch (error) {
      say(`${connection.name}: ${error.message}`, true);
    } finally {
      refresh.disabled = false;
      refresh.textContent = "Read the model list";
    }
  });

  row.append(input, add, refresh);
  return row;
}

// ---------- one model ----------

function editModel(model) {
  openDialog(`Model ${escapeHtml(model.id)}`, (card) => {
    const name = field("Shown as", model.display_name);
    const hidden = checkbox(model.hidden, "Show this model to normal users");
    const context = field("Context length", model.context_length ? String(model.context_length) : "");
    const body = textarea("Request fields", JSON.stringify(model.body_overrides || {}, null, 1));
    const mapping = textarea("Reasoning levels", JSON.stringify(model.reasoning_map || {}, null, 1));
    const status = document.createElement("p");
    status.className = "muted";

    const flagsRow = document.createElement("div");
    flagsRow.className = "flag-editor";
    for (const [key, label] of FLAGS) {
      const select = document.createElement("select");
      select.setAttribute("aria-label", label);
      for (const value of ["unknown", "no", "yes"]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = model[key] === null || model[key] === undefined ? value === "unknown" : Boolean(model[key]) === (value === "yes");
        select.append(option);
      }
      flagsRow.append(Object.assign(document.createElement("label"), { textContent: label }), select);
    }

    const save = document.createElement("button");
    save.className = "small-button";
    save.textContent = "Save model";
    save.addEventListener("click", async () => {
      status.textContent = "Saving…";
      try {
        const flags = {};
        for (const [key] of FLAGS) {
          const value = [...flagsRow.querySelectorAll("select")][FLAGS.findIndex((entry) => entry[0] === key)].value;
          flags[key] = value === "unknown" ? null : value === "yes";
        }
        await api(`/api/connections/models/${model.row_id}`, {
          method: "POST",
          body: JSON.stringify({
            display_name: name.value, hidden: hidden.input.checked,
            context_length: context.value ? Number(context.value) : null,
            body_overrides: parseJson(body.value, status), reasoning_map: parseJson(mapping.value, status),
            ...flags,
          }),
        });
        status.textContent = "Saved.";
        await load();
      } catch (error) { status.textContent = error.message; }
    });

    card.append(fieldRow("Shown as", name), fieldRow("Visible to normal users", hidden),
      fieldRow("Context length", context), fieldRow("Capabilities", flagsRow),
      fieldRow("Request fields", body), fieldRow("Reasoning levels", mapping), status, save);
  });
}

async function removeModel(model) {
  if (!window.confirm(`Remove ${model.namespaced}? Conversations using it will fall back to the default model.`)) return;
  try {
    const result = await api(`/api/connections/models/${model.row_id}`, { method: "DELETE" });
    say(result.note || `Removed ${model.namespaced}.`);
    await load();
  } catch (error) { say(error.message, true); }
}

// ---------- one connection ----------

export function openAddConnection() {
  openDialog("Add a connection", (card) => {
    const preset = presetSelect();
    const name = field("Name", "");
    const url = field("Base URL", "");
    url.placeholder = "empty uses this preset's default";
    const key = field("API key", "");

    const status = document.createElement("p");
    status.className = "muted";
    const add = document.createElement("button");
    add.className = "small-button";
    add.textContent = "Add connection";
    add.addEventListener("click", async () => {
      status.textContent = "Adding…";
      try {
        const created = await api("/api/connections", {
          method: "POST",
          body: JSON.stringify({ preset: preset.value, name: name.value.trim(), base_url: url.value.trim(), api_key: key.value }),
        });
        status.textContent = `Added ${created.name} at ${created.base_url}.`;
        await load();
      } catch (error) { status.textContent = error.message; }
    });

    card.append(fieldRow("Preset", preset), fieldRow("Name", name), fieldRow("Base URL", url),
      fieldRow("API key", key), status, add);
  });
}

function presetSelect() {
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Preset");
  for (const [key, label] of Object.entries(presets)) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    option.selected = key === "custom";
    select.append(option);
  }
  return select;
}

function editConnection(connection) {
  openDialog(`Edit ${escapeHtml(connection.name)}`, (card) => {
    const name = field("Name", connection.name);
    const url = field("Base URL", connection.base_url);
    const key = field("API key", "");
    key.placeholder = connection.api_key?.set ? `set, ends ${connection.api_key.hint}. Leave empty to keep it.` : "not set";
    const visibility = selectField("Visible to", ["all", "admins"]);
    visibility.value = connection.visibility;
    const timeout = field("Timeout seconds", String(connection.timeout_seconds));
    const concurrent = field("At most this many at a time", String(connection.max_concurrent));
    const enabled = checkbox(connection.enabled, "Use this connection");
    const status = document.createElement("p");
    status.className = "muted";

    const save = document.createElement("button");
    save.className = "small-button";
    save.textContent = "Save connection";
    save.addEventListener("click", async () => {
      status.textContent = "Saving…";
      try {
        const body = {
          name: name.value.trim(), base_url: url.value.trim(), visibility: visibility.value,
          timeout_seconds: Number(timeout.value) || 120, max_concurrent: Number(concurrent.value) || 1,
          enabled: enabled.input.checked,
        };
        if (key.value) body.api_key = key.value;   // a stored secret cannot be read back, only replaced
        await api(`/api/connections/${connection.id}`, { method: "POST", body: JSON.stringify(body) });
        status.textContent = "Saved.";
        await load();
      } catch (error) { status.textContent = error.message; }
    });

    card.append(fieldRow("Name", name), fieldRow("Base URL", url), fieldRow("API key", key),
      fieldRow("Visible to", visibility), fieldRow("Timeout seconds", timeout),
      fieldRow("At most this many at a time", concurrent),
      fieldRow("Use this connection", enabled), status, save);
  });
}

async function removeConnection(connection) {
  // The default model may live on this connection, so the server decides whether it can go.
  try {
    const result = await api(`/api/connections/${connection.id}`, { method: "DELETE" });
    say(result.note || `Removed ${connection.name}.`);
    await load();
  } catch (error) { say(error.message, true); }
}

// ---------- parts ----------

function rowMenu(connection) {
  const menu = document.createElement("button");
  menu.className = "row-menu";
  menu.style.display = "inline-flex";
  menu.title = "Connection options";
  menu.setAttribute("aria-label", `Options for ${connection.name}`);
  menu.innerHTML = icon("dots", 16);
  menu.addEventListener("click", () => openMenu(menu, [
    { label: "Edit", action: () => editConnection(connection) },
    { label: "Delete", danger: true, action: () => removeConnection(connection) },
  ]));
  return menu;
}

// The queue is live state, so it is shown as it is: how many are running and how many are waiting.
function queueState(connection) {
  const state = document.createElement("span");
  state.className = "meta";
  const queue = connection.queue || { active: 0, waiting: 0, limit: connection.max_concurrent };
  state.textContent = `running ${queue.active}/${queue.limit}${queue.waiting ? `, waiting ${queue.waiting}` : ""}`;
  if (queue.waiting) state.classList.add("waiting");
  return state;
}

function badge(text, kind) {
  const node = document.createElement("span");
  node.className = `status${kind ? ` ${kind}` : ""}`;
  node.textContent = text;
  return node;
}

function summary(result) {
  const checks = (result || {}).checks || {};
  const failed = Object.entries(checks).filter(([, check]) => check.status === "failed").map(([name]) => name);
  return failed.length ? `failed: ${failed.join(", ")}` : "all checks answered";
}

function field(labelText, value) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.setAttribute("aria-label", labelText);
  return input;
}

function textarea(labelText, value) {
  const input = document.createElement("textarea");
  input.rows = 3;
  input.value = value;
  input.setAttribute("aria-label", labelText);
  return input;
}

function checkbox(value, labelText) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(value);
  input.setAttribute("aria-label", labelText);
  return { input, checked: input.checked };
}

function selectField(labelText, options) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", labelText);
  for (const value of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  return select;
}

function fieldRow(labelText, control) {
  const row = document.createElement("div");
  row.className = "field";
  row.append(Object.assign(document.createElement("label"), { textContent: labelText }), control);
  return row;
}

function parseJson(text, status) {
  try {
    return JSON.parse(text || "{}");
  } catch (error) {
    status.textContent = "Those fields have to be JSON.";
    return {};
  }
}
