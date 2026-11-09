// Settings → API keys. A key is shown once, at creation; the server keeps only its hash and the first
// 8 characters, so nothing here can show a key again after this.
import { api, formatK } from "./api.js";

export function apiKeysSection() {
  const block = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "API keys";
  block.append(heading);

  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "These keys are for the OpenAI-compatible API at /api/v1. A key is shown once, " +
    "when it is created; the app stores only its hash.";
  block.append(note);

  const name = input("Name", "text", "this laptop");
  const expires = input("Expires (optional)", "date", "");
  const create = document.createElement("button");
  create.className = "small-button";
  create.textContent = "Create key";

  const shown = document.createElement("p");
  shown.className = "shown-key";
  shown.hidden = true;

  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const result = await api("/api/keys", { method: "POST", body: JSON.stringify({
        name: name.input.value, expires: expires.input.value,
      }) });
      shown.hidden = false;
      shown.textContent = `${result.token}`;
      const line = document.createElement("p");
      line.className = "muted";
      line.textContent = "Copy it now: this is the only time it is shown.";
      shown.append(line);
      await fill();
    } catch (error) {
      shown.hidden = false;
      shown.textContent = error.message;
    }
    create.disabled = false;
  });

  const list = document.createElement("div");
  list.className = "key-list";

  async function fill() {
    list.innerHTML = "";      // this runs again after a key is created or revoked, so start clean
    let data = { keys: [], base_url: "", allow_api: true };
    try {
      data = await api("/api/keys");
    } catch (error) {
      const line = document.createElement("p");
      line.className = "muted";
      line.textContent = `Cannot read keys: ${error.message}`;
      list.append(line);
      return;
    }

    const usage = document.createElement("p");
    usage.className = "muted";
    usage.textContent = data.allow_api
      ? "API access is on for this app. Each key is limited to 60 requests per minute by default."
      : "API access is off for this app, so no key works until an admin turns it on.";
    list.append(usage);

    if (!data.keys.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No keys yet. Create one to use the API.";
      list.append(empty);
      return;
    }

    for (const key of data.keys) {
      const row = document.createElement("div");
      row.className = "key-row";

      const nameLine = document.createElement("span");
      nameLine.className = "key-name";
      nameLine.textContent = key.name;

      const details = document.createElement("span");
      details.className = "key-details";
      details.textContent = `${key.prefix}… · created ${key.created.slice(0, 10)}` +
        (key.expires ? ` · expires ${key.expires.slice(0, 10)}` : "") +
        (key.last_used ? ` · last used ${key.last_used.slice(0, 10)}` : " · never used") +
        ` · ${formatK(key.tokens_used || 0)} tokens`;

      const revoke = document.createElement("button");
      revoke.className = "small-button danger";
      revoke.textContent = key.revoked ? "revoked" : "Revoke";
      revoke.disabled = key.revoked;
      revoke.addEventListener("click", async () => {
        try {
          await api(`/api/keys/${encodeURIComponent(key.id)}/revoke`, { method: "POST" });
          revoke.textContent = "revoked";
          revoke.disabled = true;
        } catch (error) {
          revoke.textContent = error.message;
        }
      });

      const status = document.createElement("span");
      status.className = "status";
      if (key.revoked) { status.textContent = "revoked"; status.classList.add("failed"); }
      else if (key.expired) { status.textContent = "expired"; status.classList.add("partial"); }

      row.append(nameLine, details, status, revoke);
      list.append(row);
    }
  }

  const how = document.createElement("div");
  how.className = "how-to-use";
  const code = document.createElement("pre");
  code.textContent = "Loading…";

  function paint(base) {
    code.textContent = `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer dsk-..." \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "<connection>/<model>", "messages": [{"role": "user", "content": "hi"}]}'`;
    const withClient = document.createElement("p");
    withClient.className = "muted";
    withClient.textContent = "Or with the official client: OpenAI(api_key=..., base_url=\"" + base + "\").";
    how.append(code, withClient);
  }

  api("/api/keys").then((data) => paint(data.base_url)).catch(() => paint("/api/v1"));

  block.append(name, expires, create, shown, list, how);
  fill();
  return block;
}

function input(labelText, type, placeholder) {
  const row = document.createElement("div");
  row.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const field = document.createElement("input");
  field.type = type;
  field.placeholder = placeholder;
  field.setAttribute("aria-label", labelText);
  row.append(label, field);
  row.input = field;   // the caller reads row.input.value
  return row;
}
