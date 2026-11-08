// Admin panel: accounts and their limits, the audit log, and usage numbers for one period.
// An admin sees counts, never chat content.
import { api, download, escapeHtml, formatK } from "./api.js";
import { icon } from "./icons.js";
import { openMenu, openDialog, toast } from "./ui.js";
import { startShell } from "./shell.js";

const el = (id) => document.getElementById(id);
const ui = {
  tabUsers: el("tab-users"), tabUsage: el("tab-usage"), usersView: el("users-view"), usageView: el("usage-view"),
  status: el("admin-status"), create: el("create-user"), setting: el("allow-self-registration"),
  userRows: el("user-rows"), auditRows: el("audit-rows"), period: el("period"), periodLabel: el("period-label"),
  prevPeriod: el("prev-period"), nextPeriod: el("next-period"), currentPeriod: el("this-period"),
  totals: el("totals"), usageHead: el("usage-head"), usageRows: el("usage-rows"), filter: el("user-filter"),
  stacked: el("stacked"), export: el("export-csv"), chart: el("chart"), legend: el("chart-legend"), caption: el("chart-caption"),
};

const PERIODS = [{ key: "day", label: "Day" }, { key: "month", label: "Month" }, { key: "year", label: "Year" }, { key: "all", label: "All time" }];
const COLUMNS = [
  { key: "username", label: "User", sortable: true }, { key: "tokens", label: "Tokens", sortable: true },
  { key: "prompt_tokens", label: "Prompt", sortable: true }, { key: "completion_tokens", label: "Completion", sortable: true },
  { key: "requests", label: "Requests", sortable: true }, { key: "share", label: "Share", sortable: true },
  { key: "daily", label: "Today", sortable: false }, { key: "weekly", label: "This week", sortable: false },
];
const TOTALS = [["Total tokens", "tokens"], ["Prompt", "prompt_tokens"], ["Completion", "completion_tokens"], ["Chat", "chat_tokens"], ["Dreaming", "dreaming_tokens"]];
const DATE_FORMAT = { year: "numeric", month: "short", day: "2-digit" };
const TIME_FORMAT = { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" };

let accounts = null, usage = null, period = "month", anchor = new Date();
let filterUser = null, stacked = false, sort = { key: "tokens", desc: true }, headers = [];

function showView(which) {
  ui.usersView.hidden = which !== "users";
  ui.usageView.hidden = which !== "usage";
  for (const [tab, name] of [[ui.tabUsers, "users"], [ui.tabUsage, "usage"]]) {
    tab.classList.toggle("on", name === which);
    tab.setAttribute("aria-selected", String(name === which));
  }
}

// ---------- data ----------

async function loadUsers() {
  try {
    accounts = await api("/api/admin/users");
  } catch (error) {
    say(`Cannot read the accounts: ${error.message}`, true);
    return;
  }
  ui.setting.checked = Boolean(accounts.settings.allow_self_registration);
  renderUsers();
  renderAudit();
}

async function loadUsage() {
  try {
    usage = await api(`/api/admin/usage?period=${period}&date=${periodDate()}`);
  } catch (error) {
    say(`Cannot read usage: ${error.message}`, true);
    return;
  }
  ui.periodLabel.textContent = usage.label;
  ui.prevPeriod.disabled = period === "all";
  ui.nextPeriod.disabled = periodDate() === periodDate(new Date());
  renderFilter();
  renderTotals();
  renderUsageTable();
  renderChart();
  if (accounts) renderUsers();   // the users table shows tokens for this same period
}

function say(message, isError) {
  ui.status.textContent = message;
  ui.status.classList.toggle("error", Boolean(isError));
}

// ---------- users ----------

function renderUsers() {
  const used = new Map((usage?.per_user || []).map((row) => [String(row.user_id), row]));
  ui.userRows.innerHTML = accounts.users.map((user) => {
    const name = escapeHtml(user.username);
    return `<tr>
      <td class="name">${name}</td><td>${badge(user.role, user.role === "admin" ? "role" : "")}</td>
      <td>${badge(user.status, user.status === "active" ? "success" : "failed")}</td>
      <td class="value">${stampText(user.created, DATE_FORMAT)}</td>
      <td class="value">${user.last_login ? stampText(user.last_login, TIME_FORMAT) : "never"}</td>
      <td>${valueCell(used.get(String(user.id)))}</td>
      <td>${limitCell(user, "daily")}</td><td>${limitCell(user, "weekly")}</td>
      <td><button class="row-menu" data-id="${user.id}" title="Options for ${name}" aria-label="Options for ${name}">${icon("dots", 16)}</button></td>
    </tr>`;
  }).join("");
}

function rowActions(user) {
  return [
    { label: user.role === "admin" ? "Remove admin" : "Make admin", action: () => setField(user, "role", user.role === "admin" ? "user" : "admin") },
    { label: "Reset password", action: () => resetPassword(user) },
    { label: user.status === "active" ? "Disable" : "Enable", action: () => setField(user, "status", user.status === "active" ? "disabled" : "active") },
    { separator: true },
    { label: "Delete", danger: true, action: () => confirmDelete(user) },
  ];
}

// The last-active-admin rules live on the server, so its message is shown as it is.
async function setField(user, field, value) {
  try {
    const result = await api(`/api/admin/users/${user.id}/${field}`, { method: "POST", body: JSON.stringify({ [field]: value }) });
    Object.assign(user, result.user || {});
    say(result.note || `${user.username}: ${field} is now ${value}.`);
    renderUsers();
  } catch (error) { say(`${user.username}: ${error.message}`, true); }
}

async function saveLimit(user, window, input, result) {
  const value = parseLimit(input.value);
  if (value === null) { showResult(result, "a number, 100k / 1.5m, or empty", "error"); return; }
  try {
    // The limits endpoint writes both windows, so the other one is sent as it is now.
    const other = window === "daily" ? "weekly" : "daily";
    const saved = await api(`/api/admin/users/${user.id}/limits`, {
      method: "POST",
      body: JSON.stringify({ [`${window}_limit`]: value, [`${other}_limit`]: user[`${other}_limit`] || 0 }),
    });
    Object.assign(user, saved.user || {});
    input.value = user[`${window}_limit`] ? String(user[`${window}_limit`]) : "";
    showResult(result, "saved", "saved");
    if (usage) renderUsageTable();
  } catch (error) { showResult(result, error.message, "error"); }
}

function showResult(node, message, kind) {
  node.textContent = message;
  node.className = `result${kind ? ` ${kind}` : ""}`;
}

// A generated password is shown once here; the server marks it as one that must be changed.
async function resetPassword(user) {
  const password = tempPassword();
  const card = openDialog(`Temporary password for ${escapeHtml(user.username)}`, (dialog) => {
    const status = paragraph("Show this once. The user has to change it at their next login.");
    dialog.append(status, Object.assign(paragraph(password), { className: "value" }),
      button("Copy", () => { navigator.clipboard?.writeText(password); toast("Copied."); }));
  });
  try {
    const result = await api(`/api/admin/users/${user.id}/password`, { method: "POST", body: JSON.stringify({ password }) });
    card.querySelector(".muted").textContent = result.note || `Password for ${user.username} changed.`;
    Object.assign(user, result.user || {});
    renderUsers();
  } catch (error) { card.querySelector(".muted").textContent = error.message; }
}

function confirmDelete(user) {
  openDialog(`Delete ${escapeHtml(user.username)}`, (card) => {
    const status = paragraph("");
    const confirm = field("Type the username to confirm", user.username);
    const matches = () => confirm.value.trim().toLowerCase() === user.username.toLowerCase();

    // Deleting is the one action that needs the username typed out, so the button stays disabled until it matches.
    const remove = button("Delete", async () => {
      remove.disabled = true;
      try {
        const result = await api(`/api/admin/users/${user.id}`, { method: "DELETE", body: JSON.stringify({ confirm: confirm.value }) });
        status.textContent = result.note || `Deleted ${user.username}.`;
        loadUsers();
        loadUsage();
      } catch (error) { status.textContent = error.message; remove.disabled = !matches(); }
    });
    remove.disabled = true;
    confirm.addEventListener("input", () => { remove.disabled = !matches(); });
    card.append(paragraph("Their data folder is removed. Usage rows stay, without a user."),
      fieldRow("Type the username to confirm", confirm), status, remove);
  });
}

function openCreateUser() {
  openDialog("Create user", (card) => {
    const name = field("Username", "");
    const password = field("Temporary password", "");
    const role = selectField("Role", ["user", "admin"]);
    const daily = field("Daily limit", "empty = no limit");
    const weekly = field("Weekly limit", "empty = no limit");
    const status = paragraph("");
    password.value = tempPassword();

    const create = button("Create user", async () => {
      const limits = [parseLimit(daily.value), parseLimit(weekly.value)];
      if (limits.some((value) => value === null)) { status.textContent = "Limits must be a number, 100k / 1.5m, or empty."; return; }
      create.disabled = true;
      status.textContent = "Creating…";
      try {
        const result = await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({ username: name.value.trim(), password: password.value, role: role.value, daily_limit: limits[0], weekly_limit: limits[1] }),
        });
        status.textContent = result.note || `Created ${result.user.username}.`;
        loadUsers();
        loadUsage();
      } catch (error) { status.textContent = error.message; create.disabled = false; }
    });

    card.append(fieldRow("Username", name), fieldRow("Temporary password", password, button("Generate", () => { password.value = tempPassword(); })),
      fieldRow("Role", role), fieldRow("Daily limit", daily), fieldRow("Weekly limit", weekly), status, create);
  });
}

function renderAudit() {
  ui.auditRows.innerHTML = accounts.audit.map((entry) => `<tr>
    <td class="value">${escapeHtml(entry.actor)}</td><td class="action">${escapeHtml(entry.action)}</td>
    <td class="value">${escapeHtml(entry.target)}</td><td class="value">${escapeHtml(entry.detail)}</td>
    <td class="value">${stampText(entry.at, TIME_FORMAT)}</td>
  </tr>`).join("") || `<tr><td class="value" colspan="5">nothing recorded</td></tr>`;
}

// ---------- usage ----------

function renderTotals() {
  const totals = usage.totals;
  const marks = totals.estimated_requests ? " ~" : "";
  const title = (value) => `${value} tokens${totals.estimated_requests ? `, ${totals.estimated_requests} estimated calls` : ""}`;
  const row = (label, key) => `<div class="usage-row"><span class="label">${label}</span>` +
    `<span class="value" title="${escapeHtml(title(totals[key]))}">${formatK(totals[key])}${marks}</span></div>`;

  // Numbers the engine did not count are named as estimates, not presented as fact.
  ui.totals.innerHTML = TOTALS.map((entry) => row(entry[0], entry[1])).join("") +
    `<div class="usage-row"><span class="label">Requests</span><span class="value" title="${totals.rows} recorded calls">${totals.requests}</span></div>` +
    (totals.estimated_requests ? `<p class="muted">${totals.estimated_requests} of those calls are estimates (characters / 4).</p>` : "");
}

function buildUsageHead() {
  const headRow = document.createElement("tr");
  for (const column of COLUMNS) {
    const th = document.createElement("th");
    if (!column.sortable) { th.textContent = column.label; headRow.append(th); continue; }
    // Header buttons take their look from .admin-table th button, so they carry no class of their own.
    const header = button(column.label, () => {
      if (sort.key === column.key) sort.desc = !sort.desc;
      else { sort.key = column.key; sort.desc = column.key !== "username"; }
      markSort();
      renderUsageTable();
    }, "");
    const arrow = Object.assign(document.createElement("span"), { className: "arrow" });
    th.append(header, arrow);
    headers.push({ th, arrow, key: column.key });
    headRow.append(th);
  }
  ui.usageHead.append(headRow);
  markSort();
}

function markSort() {
  for (const header of headers) {
    const active = header.key === sort.key;
    header.th.setAttribute("aria-sort", active ? (sort.desc ? "descending" : "ascending") : "none");
    header.arrow.textContent = active ? (sort.desc ? "▾" : "▴") : "";
  }
}

function sortedRows() {
  const rows = [...(usage?.per_user || [])];
  rows.sort((a, b) => {
    if (typeof a[sort.key] === "string") return String(a[sort.key]).localeCompare(String(b[sort.key])) * (sort.desc ? -1 : 1);
    return ((a[sort.key] || 0) - (b[sort.key] || 0)) * (sort.desc ? -1 : 1);
  });
  return rows;
}

function renderUsageTable() {
  const rows = sortedRows();
  ui.usageRows.innerHTML = rows.length ? rows.map((row) => {
    const name = escapeHtml(row.username);
    const key = rowKey(row);
    const active = filterUser === key;
    return `<tr>
      <td><button class="user-link${active ? " active" : ""}" data-id="${key}" title="${active ? "Show all users in the chart" : `Chart only ${name}`}">${name}</button></td>
      <td>${valueCell(row)}</td><td>${valueCell(row, "prompt_tokens")}</td><td>${valueCell(row, "completion_tokens")}</td>
      <td><span class="value" title="${row.estimated} estimated calls">${row.requests}${row.estimated ? " ~" : ""}</span></td>
      <td><div class="window"><span class="value" title="${row.share}% of ${usage.totals.tokens} tokens">${row.share}%</span>
        <span class="usage-bar share"><span class="fill" style="width:${Math.min(100, row.share)}%"></span></span></div></td>
      <td>${windowCell(row, "daily")}</td><td>${windowCell(row, "weekly")}</td>
    </tr>`;
  }).join("") : `<tr><td class="value" colspan="8">no usage in this period</td></tr>`;
}

function setFilter(userId) {
  filterUser = userId;
  ui.filter.value = userId || "";
  renderUsageTable();
  renderChart();
}

function renderFilter() {
  const options = [`<option value="">all users</option>`];
  for (const row of usage.per_user) {
    const key = rowKey(row);
    options.push(`<option value="${key}"${key === String(filterUser) ? " selected" : ""}>${escapeHtml(row.username)}</option>`);
  }
  ui.filter.innerHTML = options.join("");
}

// Empty buckets are added so the chart shows the whole period, not only the days that had usage.
function series() {
  const known = new Map((usage?.chart || []).map((bucket) => [bucket.label, bucket]));
  const blank = (label) => ({ label, tokens: 0, chat: 0, dreaming: 0, users: {}, estimated: 0 });
  if (period === "all") return usage?.chart || [];

  const labels = [];
  if (period === "day") {
    for (let hour = 0; hour < 24; hour += 1) labels.push(`${pad(hour)}:00`);
  } else if (period === "month") {
    const start = firstOfMonth(anchor);
    for (let day = 1; day <= new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate(); day += 1) {
      labels.push(`${start.getFullYear()}-${pad(start.getMonth() + 1)}-${pad(day)}`);
    }
  } else {
    for (let month = 1; month <= 12; month += 1) labels.push(pad(month));
  }
  return labels.map((label) => known.get(label) || blank(label));
}

function colorMap() {
  const colors = new Map();
  (usage?.per_user || []).forEach((row, index) => colors.set(rowKey(row), index % 6));
  return colors;
}

function segments(bucket, colors) {
  if (filterUser) return [{ value: bucket.users[String(filterUser)] || 0, className: `u${colors.get(String(filterUser)) || 0}` }];
  if (!stacked) return [{ value: bucket.chat, className: "bar" }, { value: bucket.dreaming, className: "dreaming" }];
  return Object.entries(bucket.users).filter(([, value]) => value > 0).sort((a, b) => b[1] - a[1])
    .map(([key, value]) => ({ value, className: `u${colors.get(key) || 0}` }));
}

// The tooltip on a bar says what the bar is, estimates included.
const barTitle = (bucket) => `${bucket.label} · ${bucket.tokens} tokens${bucket.estimated ? ` (${bucket.estimated} estimated)` : ""}`;

function renderChart() {
  const buckets = series();
  const colors = colorMap();
  const peak = Math.max(1, ...buckets.map((bucket) => (filterUser ? (bucket.users[String(filterUser)] || 0) : bucket.tokens)));
  const width = 780, height = 240, left = 54, top = 14, bottom = 30, right = 14;
  const plotHeight = height - top - bottom;
  const slot = (width - left - right) / Math.max(1, buckets.length);
  const barWidth = Math.max(3, Math.min(26, slot - 4));
  const scale = plotHeight / peak;
  const every = Math.max(1, Math.ceil(buckets.length / 12));

  const parts = [`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Usage over time for ${escapeHtml(usage.label)}">`,
    `<line class="grid" x1="${left}" y1="${(top + plotHeight / 2).toFixed(1)}" x2="${width - right}" y2="${(top + plotHeight / 2).toFixed(1)}"/>`,
    `<line class="axis" x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}"/>`,
    `<text x="${left - 10}" y="${top + 6}" text-anchor="end">${escapeHtml(formatK(peak))}</text>`];

  buckets.forEach((bucket, index) => {
    let y = top + plotHeight;
    const x = (left + slot * index + (slot - barWidth) / 2).toFixed(1);
    const rects = segments(bucket, colors).map((part) => {
      if (!part.value) return "";
      const barHeight = Math.max(1, part.value * scale);
      y -= barHeight;
      return `<rect class="${part.className}" x="${x}" y="${y.toFixed(1)}" width="${barWidth}" height="${barHeight.toFixed(1)}"/>`;
    }).join("");
    if (rects) parts.push(`<g><title>${escapeHtml(barTitle(bucket))}</title>${rects}</g>`);
    if (index % every === 0) {
      const label = period === "month" ? bucket.label.slice(8) : bucket.label;
      parts.push(`<text x="${(left + slot * index + slot / 2).toFixed(1)}" y="${height - 12}" text-anchor="middle">${escapeHtml(label)}</text>`);
    }
  });

  ui.chart.innerHTML = `${parts.join("")}</svg>`;
  renderLegend(colors);

  const busiest = buckets.slice().sort((a, b) => b.tokens - a.tokens)[0];
  ui.caption.textContent = busiest && busiest.tokens
    ? `peak ${formatK(peak)} tokens in ${busiest.label} · ${usage.totals.rows} calls in ${usage.label}`
    : `nothing recorded in ${usage.label}`;
}

function renderLegend(colors) {
  let items;
  if (stacked && !filterUser) {
    items = (usage?.per_user || []).filter((row) => row.tokens).map((row) => ({ name: row.username, color: colors.get(rowKey(row)) || 0 }));
  } else if (filterUser) {
    const row = (usage?.per_user || []).find((entry) => rowKey(entry) === String(filterUser));
    items = [{ name: row ? row.username : "selected user", color: colors.get(String(filterUser)) || 0 }];
  } else {
    items = [{ name: "chat", color: 0 }, { name: "dreaming", color: 1 }];
  }
  ui.legend.innerHTML = items.map((item) => `<span><i style="background:var(--chart-${item.color + 1})"></i>${escapeHtml(item.name)}</span>`).join("");
}

// The CSV is the table as it is currently shown, in the order it is shown in.
function exportCsv() {
  const header = ["user", "tokens", "prompt_tokens", "completion_tokens", "requests", "share_percent", "daily_limit", "daily_used", "weekly_limit", "weekly_used"];
  const lines = [header.join(",")];
  for (const row of sortedRows()) {
    const daily = (row.current || {}).daily || {};
    const weekly = (row.current || {}).weekly || {};
    lines.push([row.username, row.tokens, row.prompt_tokens, row.completion_tokens, row.requests, row.share,
      daily.limit || "", daily.used || "", weekly.limit || "", weekly.used || ""].map(csvCell).join(","));
  }
  download(`dreamscape-usage-${period}-${periodDate() || "all"}.csv`, lines.join("\n"), "text/csv");
  toast("CSV downloaded.");
}

function csvCell(value) {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

// ---------- controls ----------

function buildControls() {
  for (const item of PERIODS) {
    const tab = button(item.label, () => {
      period = item.key;
      for (const child of ui.period.children) child.classList.toggle("on", child === tab);
      loadUsage();
    }, "");
    tab.classList.toggle("on", item.key === period);
    ui.period.append(tab);
  }

  ui.prevPeriod.addEventListener("click", () => shiftPeriod(-1));
  ui.nextPeriod.addEventListener("click", () => shiftPeriod(1));
  ui.currentPeriod.addEventListener("click", () => { anchor = new Date(); loadUsage(); });
  ui.filter.addEventListener("change", () => setFilter(ui.filter.value || null));
  ui.export.addEventListener("click", exportCsv);
  ui.create.addEventListener("click", openCreateUser);
  ui.tabUsers.addEventListener("click", () => showView("users"));
  ui.tabUsage.addEventListener("click", () => showView("usage"));

  // Table bodies are redrawn as strings, so their buttons and inputs are handled from the body itself.
  ui.userRows.addEventListener("click", (event) => {
    const open = event.target.closest(".row-menu");
    if (open) openMenu(open, rowActions(findUser(open.dataset.id)));
  });
  ui.userRows.addEventListener("change", (event) => {
    const input = event.target.closest("[data-limit]");
    if (input) saveLimit(findUser(input.dataset.id), input.dataset.limit, input, input.nextElementSibling);
  });
  ui.usageRows.addEventListener("click", (event) => {
    const link = event.target.closest(".user-link");
    if (link) setFilter(filterUser === link.dataset.id ? null : link.dataset.id);
  });

  ui.stacked.addEventListener("click", () => {
    stacked = !stacked;
    ui.stacked.setAttribute("aria-pressed", String(stacked));
    renderChart();
  });

  ui.setting.addEventListener("change", async () => {
    const value = ui.setting.checked;
    try {
      const result = await api("/api/admin/settings", { method: "POST", body: JSON.stringify({ key: "allow_self_registration", value }) });
      accounts.settings = result.settings;
      ui.setting.checked = Boolean(result.settings.allow_self_registration);
      say(`Self-registration is ${result.settings.allow_self_registration ? "on" : "off"}.`);
    } catch (error) {
      ui.setting.checked = Boolean(accounts.settings.allow_self_registration);
      say(`Could not save that: ${error.message}`, true);
    }
  });
}

function shiftPeriod(delta) {
  if (period === "all") return;
  if (period === "day") anchor = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() + delta);
  else if (period === "month") anchor = new Date(anchor.getFullYear(), anchor.getMonth() + delta, 1);
  else anchor = new Date(anchor.getFullYear() + delta, 0, 1);
  loadUsage();
}

// The date the usage endpoint wants: the day itself, the first of the month, or January 1.
function periodDate(date = anchor) {
  if (period === "all") return "";
  if (period === "day") return isoDate(date);
  if (period === "month") return isoDate(firstOfMonth(date));
  return `${date.getFullYear()}-01-01`;
}

// ---------- small helpers ----------

const pad = (value) => String(value).padStart(2, "0");
const isoDate = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const firstOfMonth = (date) => new Date(date.getFullYear(), date.getMonth(), 1);
const stampText = (value, format) => value ? new Date(value).toLocaleString(undefined, format) : "";
const findUser = (id) => accounts?.users.find((user) => String(user.id) === String(id)) || {};
// The key a usage row is known by. Rows that outlive their user have no id, so they are keyed "deleted".
const rowKey = (row) => (row.user_id === null || row.user_id === undefined) ? "deleted" : String(row.user_id);
const badge = (text, kind) => `<span class="status${kind ? ` ${kind}` : ""}">${text}</span>`;

// Short form on screen, the exact number in the title.
function valueCell(row, key = "tokens") {
  if (!row) return `<span class="value">—</span>`;
  const title = `${row[key]} tokens${row.estimated ? `, ${row.estimated} estimated calls` : ""}`;
  return `<span class="value" title="${escapeHtml(title)}">${escapeHtml(formatK(row[key]))}${row.estimated ? " ~" : ""}</span>`;
}

function limitCell(user, window) {
  const value = user[`${window}_limit`] || 0;
  return `<div class="limit"><input type="text" data-limit="${window}" data-id="${user.id}" value="${value ? value : ""}"` +
    ` placeholder="no limit" aria-label="${window} limit for ${escapeHtml(user.username)}"><span class="result"></span></div>`;
}

function windowCell(row, window) {
  const current = (row.current || {})[window] || { limit: 0, used: 0 };
  if (!current.limit) return `<span class="value">no limit</span>`;
  const percent = Math.round((current.used / current.limit) * 100);
  const tone = percent >= 100 ? " over" : percent >= 80 ? " high" : "";
  return `<div class="window"><span class="value" title="${current.used} of ${current.limit} in the current ${window} window">` +
    `${escapeHtml(formatK(current.used))} (${percent}%)</span>` +
    `<span class="usage-bar"><span class="fill${tone}" style="width:${Math.min(100, percent)}%"></span></span></div>`;
}

function button(label, handler, className = "small-button") {
  const node = document.createElement("button");
  node.className = className;
  node.textContent = label;
  node.addEventListener("click", handler);
  return node;
}

function paragraph(text) {
  return Object.assign(document.createElement("p"), { className: "muted", textContent: text });
}

function fieldRow(labelText, control, extra) {
  const row = document.createElement("div");
  row.className = "field";
  row.append(Object.assign(document.createElement("label"), { textContent: labelText }), control);
  if (extra) row.append(extra);
  return row;
}

function field(labelText, placeholder) {
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = placeholder;
  input.setAttribute("aria-label", labelText);
  return input;
}

function selectField(labelText, options) {
  const select = document.createElement("select");
  select.setAttribute("aria-label", labelText);
  for (const value of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    option.selected = value === options[0];
    select.append(option);
  }
  return select;
}

// Same rule as the server: empty or 0 means no limit, 100k and 1.5m are accepted. null is not a limit.
function parseLimit(text) {
  const value = String(text || "").trim().toLowerCase().replace(/,/g, "");
  if (!value) return 0;
  const multiplier = value.endsWith("k") ? 1000 : value.endsWith("m") ? 1000000 : 1;
  const number = Number(value.replace(/[km]$/, ""));
  if (!Number.isFinite(number) || number < 0) return null;
  return Math.round(number * multiplier);
}

// A temporary password only has to be readable, since it is replaced at the first login.
function tempPassword() {
  const alphabet = "abcdefghijkmnpqrstuvwxyz23456789";
  const bytes = new Uint32Array(12);
  crypto.getRandomValues(bytes);
  return [...bytes].map((value) => alphabet[value % alphabet.length]).join("");
}

buildControls();
buildUsageHead();

// startShell paints the icons, sets the theme, and goes to /login when there is no session.
startShell({}).then(() => {
  loadUsers();
  loadUsage();
});
