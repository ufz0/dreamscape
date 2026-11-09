// Files a user attaches to a message: the chips above the textarea, the picker, drag & drop, paste,
// and the Storage section in Settings.
//
// An upload starts as soon as the file is chosen, so the chip shows real progress (XHR reports it) and
// the message only has to reference the file id. The estimated token count is what the server worked
// out when it read the file: characters / 4 for text, tiles for an image. It is an estimate, and the
// chip says so with a "~".
import { api, csrfToken, escapeHtml, formatK } from "./api.js";
import { icon } from "./icons.js";

// A file size in the form a person reads: 12.3 kB, not 12300.
function bytes(value) {
  const number = Number(value) || 0;
  if (number < 1000) return `${number} B`;
  if (number < 1000000) return `${Math.round(number / 100) / 10} kB`;
  return `${Math.round(number / 100000) / 10} MB`;
}

let ui = null;
let chips = null;
let picker = null;
let chatId = () => null;
let canSeeImages = () => true;
let modelInfo = () => null;
let onChanged = () => {};
let items = [];

export function setup(options) {
  ui = options.ui;
  chatId = options.chatId || (() => null);
  canSeeImages = options.canSeeImages || (() => true);
  modelInfo = options.modelInfo || (() => null);
  onChanged = options.onChanged || (() => {});

  chips = ui.chips || document.getElementById("chips");
  picker = ui.picker || document.getElementById("file-picker");

  ui.attach.disabled = false;
  ui.attach.title = "Attach a file";
  ui.attach.innerHTML = icon("paperclip", 18);
  ui.attach.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    for (const file of picker.files) add(file);
    picker.value = "";
  });

  // Dropping onto the conversation, not only the card: a file is usually dragged to the chat.
  const area = ui.main || document;
  let depth = 0;
  area.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    depth += 1;
    area.classList.add("dropping");
    event.preventDefault();
  });
  area.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  area.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) area.classList.remove("dropping"); });
  area.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    area.classList.remove("dropping");
    for (const file of event.dataTransfer.files) add(file);
  });

  document.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    for (const file of files) add(file);
  });

  render();
}

function hasFiles(event) {
  return Boolean(event.dataTransfer?.types?.includes("Files"));
}

export function files() {
  return items.filter((item) => item.id && !item.blocked).map((item) => item.id);
}

export function attachments() {
  return items.filter((item) => item.id && !item.blocked);
}

export function estimatedTokens() {
  return items.filter((item) => item.id && !item.blocked).reduce((total, item) => total + (item.estimated_tokens || 0), 0);
}

export function busy() {
  return items.some((item) => item.uploading);
}

export function clear() {
  items = [];
  render();
}

function add(file) {
  const item = { name: file.name || "file", size: file.size, uploading: true, kind: "", id: null,
                 estimated_tokens: 0, error: "", blocked: false, progress: 0 };
  items.push(item);
  upload(item, file);
  render();
}

function upload(item, file) {
  const request = new XMLHttpRequest();
  request.open("POST", "/api/uploads", true);
  request.setRequestHeader("X-CSRF-Token", csrfToken());
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) item.progress = Math.round((event.loaded / event.total) * 100);
    render();
  });
  request.addEventListener("load", () => {
    item.uploading = false;
    let body = {};
    try { body = JSON.parse(request.responseText || "{}"); } catch (error) { body = {}; }
    if (request.status >= 400) {
      item.error = body.detail || `${request.status} ${request.statusText}`;
    } else {
      item.id = body.file?.id;
      item.kind = body.file?.kind || "";
      item.estimated_tokens = body.file?.estimated_tokens || 0;
      item.size = body.file?.size ?? item.size;
    }
    if (item.kind === "image" && !canSeeImages()) {
      item.blocked = true;
      item.error = "This model cannot see images. Choose a model flagged for vision, or remove this file.";
    }
    render();
    onChanged();
  });
  request.addEventListener("error", () => {
    item.uploading = false;
    item.error = "The upload did not reach the server.";
    render();
    onChanged();
  });

  const form = new FormData();
  form.append("file", file);
  form.append("name", file.name || "file");
  const id = chatId();
  if (id) form.append("conversation_id", id);
  request.send(form);
}

function remove(item) {
  const index = items.indexOf(item);
  if (index >= 0) items.splice(index, 1);
  if (item.id) api(`/api/uploads/${encodeURIComponent(item.id)}`, { method: "DELETE" }).catch(() => {});
  render();
  onChanged();
}

function render() {
  if (!chips) return;
  chips.innerHTML = "";
  chips.hidden = !items.length;
  for (const item of items) chips.append(chip(item));
  if (!items.length) return;

  // The cost is what the app thinks the message will cost, not what the engine will count.
  const line = document.createElement("p");
  line.className = "cost";
  const total = estimatedTokens();
  const context = Number(modelInfo()?.context_length) || 0;
  line.textContent = `Estimated cost of these attachments: about ${formatK(total)} tokens.`;
  if (context && total > context) {
    line.classList.add("over");
    line.textContent += ` That is more than this model's context of ${formatK(context)} tokens. ` +
      "Shorten the file, or send less of it.";
  }
  chips.append(line);
}

function chip(item) {
  const node = document.createElement("div");
  node.className = "chip";
  if (item.error) node.classList.add(item.blocked ? "warn" : "error");

  const thumb = document.createElement("span");
  thumb.className = "thumb";
  if (item.id && item.kind === "image") {
    const image = document.createElement("img");
    image.src = `/api/uploads/${encodeURIComponent(item.id)}/thumbnail`;
    image.alt = item.name;
    thumb.append(image);
  } else {
    thumb.innerHTML = icon(item.kind === "image" ? "file" : "file", 16);
  }

  const meta = document.createElement("span");
  meta.className = "meta";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = item.name;
  const details = document.createElement("span");
  details.className = "size";
  details.textContent = [bytes(item.size),
    item.estimated_tokens ? `~${formatK(item.estimated_tokens)} tokens` : "",
    item.uploading ? `${item.progress}%` : ""].filter(Boolean).join(" · ");
  meta.append(name, details);

  if (item.uploading) {
    const bar = document.createElement("span");
    bar.className = "progress";
    const fill = document.createElement("span");
    fill.style.width = `${item.progress}%`;
    bar.append(fill);
    meta.append(bar);
  }

  if (item.error) {
    const line = document.createElement("span");
    line.className = "chip-error";
    line.textContent = item.error;
    meta.append(line);
  }

  const close = document.createElement("button");
  close.className = "icon-btn chip-close";
  close.title = "Remove";
  close.setAttribute("aria-label", `Remove ${item.name}`);
  close.innerHTML = icon("x", 15);
  close.addEventListener("click", () => remove(item));

  node.append(thumb, meta, close);
  return node;
}

// ---------- the attachment chips shown inside a message ----------

export function messageChips(attachments) {
  const row = document.createElement("div");
  row.className = "attached";
  for (const item of attachments || []) {
    const chip = document.createElement("a");
    chip.className = "attached-item";
    chip.href = `/api/uploads/${encodeURIComponent(item.id)}/download`;
    chip.title = `Open ${item.name}`;
    if (item.kind === "image") {
      const image = document.createElement("img");
      image.src = `/api/uploads/${encodeURIComponent(item.id)}/thumbnail`;
      image.alt = item.name;
      chip.append(image);
    } else {
      chip.innerHTML = icon("file", 15);
    }
    const label = document.createElement("span");
    label.className = "attached-name";
    label.innerHTML = escapeHtml(item.name);
    const size = document.createElement("span");
    size.className = "attached-size";
    size.textContent = bytes(item.size) + (item.truncated ? " · longer than the inline cap" : "");
    chip.append(label, size);
    row.append(chip);
  }
  return row;
}

// ---------- Settings → Storage ----------

export function storageSection() {
  const block = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "Storage";
  block.append(heading);

  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "Loading…";
  block.append(note);

  const list = document.createElement("div");
  list.className = "storage-list";
  block.append(list);

  api("/api/uploads").then((data) => {
    const usage = data.usage || {};
    note.textContent = `${bytes(usage.bytes)} of ${bytes(usage.quota)}, ` +
      `${data.files.length} file${data.files.length === 1 ? "" : "s"}. ` +
      `The most a single file can be is ${bytes(usage.max_file)}, and a message can carry ` +
      `${usage.per_message || 0} files. Deleting a chat deletes the files attached in it.`;

    for (const file of data.files) {
      const row = document.createElement("div");
      row.className = "storage-row";
      const name = document.createElement("a");
      name.className = "storage-name";
      name.href = `/api/uploads/${encodeURIComponent(file.id)}/download`;
      name.textContent = file.name;
      name.title = `Download ${file.name}`;
      const size = document.createElement("span");
      size.className = "storage-size";
      size.textContent = bytes(file.size);
      const chat = document.createElement("span");
      chat.className = "storage-chat";
      chat.textContent = file.conversation_id ? "in a chat" : "not attached to a chat yet";
      const remove = document.createElement("button");
      remove.className = "small-button danger";
      remove.textContent = "Delete";
      remove.addEventListener("click", async () => {
        try {
          await api(`/api/uploads/${encodeURIComponent(file.id)}`, { method: "DELETE" });
          row.remove();
        } catch (error) {
          remove.textContent = error.message;
        }
      });
      row.append(name, size, chat, remove);
      list.append(row);
    }
    if (!data.files.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No files here yet. Attach one in a chat.";
      list.append(empty);
    }
  }).catch((error) => { note.textContent = `Cannot read storage: ${error.message}`; });

  return block;
}
