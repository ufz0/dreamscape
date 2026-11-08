// The signed-in account: who you are, your own usage, change password, log out.
import { api, formatK } from "./api.js";
import { openDialog, closeDialog, toast } from "./ui.js";

let me = null;

export async function loadMe() {
  me = await api("/api/me");
  return me;
}

export function account() {
  return me;
}

export function mustChange() {
  return Boolean(me && me.user && me.user.must_change_password);
}

export function initial(name) {
  return String(name || "?").charAt(0).toUpperCase();
}

// The bars are read again when they are opened, so they show the current day rather than what
// happened to be loaded when the page was first opened.
export async function readUsage() {
  try {
    const usage = await api("/api/usage/me");
    if (me) me.usage = usage;
    return usage;
  } catch (error) {
    return { windows: [], note: `Cannot read usage: ${error.message}` };
  }
}

export function usageBlock(usage, options = {}) {
  const block = document.createElement("div");
  block.className = "usage-block";
  fillUsage(block, usage, options);
  return block;
}

// Today / this week with a thin bar. Amber at 80%, red at 100%, "no limit" when there is none.
function fillUsage(block, usage, options) {
  block.innerHTML = "";

  for (const window of (usage && usage.windows) || []) {
    const row = document.createElement("div");
    row.className = "usage-row";

    const label = document.createElement("span");
    label.className = "label";
    label.textContent = window.window === "daily" ? "Today" : "This week";

    const value = document.createElement("span");
    value.className = "value";
    value.title = `${window.used} tokens used${window.limit ? ` of ${window.limit}` : ""}`;
    const estimate = window.estimated ? " ~" : "";
    value.textContent = `${formatK(window.used)}${estimate} / ${window.limit ? formatK(window.limit) : "no limit"}`;

    row.append(label, value);

    if (window.limit) {
      const bar = document.createElement("span");
      bar.className = "usage-bar";
      const fill = document.createElement("span");
      fill.className = "fill";
      fill.style.width = `${Math.min(100, window.percent)}%`;
      if (window.percent >= 100) fill.classList.add("over");
      else if (window.percent >= 80) fill.classList.add("high");
      bar.append(fill);
      row.append(bar);
    }

    if (options.showReset && window.limit) {
      const reset = document.createElement("span");
      reset.className = "reset";
      // The same wording as the notice in the chat, so the app says this one way.
      reset.textContent = `resets ${window.resets_text || window.resets_local}`;
      row.append(reset);
    }

    block.append(row);
  }

  if (usage && usage.note) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = usage.note;
    block.append(note);
  }

  if (usage && usage.counted_source && usage.counted_source !== "chat only") {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Dreaming tokens are counted toward these limits.";
    block.append(note);
  }
}

export function openChangePassword(options = {}) {
  const card = openDialog("Change password", (card) => {
    if (options.required) {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "This is the temporary password an admin gave you. Set your own before you continue.";
      card.append(note);
    }

    const current = field("Current password", "password");
    const next = field("New password", "password");
    next.input.placeholder = "at least 8 characters";
    const status = document.createElement("p");
    status.className = "muted";

    const submit = document.createElement("button");
    submit.className = "small-button";
    submit.textContent = "Change password";
    submit.addEventListener("click", async () => {
      submit.disabled = true;
      status.textContent = "Checking…";
      try {
        const result = await api("/api/auth/change-password", {
          method: "POST",
          body: JSON.stringify({ current: current.input.value, new: next.input.value }),
        });
        if (me) me.user.must_change_password = false;
        const composer = document.getElementById("send");
        if (composer) composer.disabled = false;
        closeDialog();
        toast(result.note || "Password changed.");
      } catch (error) {
        status.textContent = error.message;
        submit.disabled = false;
      }
    });

    card.append(current, next, status, submit);
  });
  return card;
}

function field(labelText, type) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = type;
  input.setAttribute("aria-label", labelText);
  row.append(label, input);
  row.input = input;      // the caller reads row.input.value
  return row;
}

export async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    // Signing out should work even if the session is already gone.
  }
  window.location.href = "/login";
}
