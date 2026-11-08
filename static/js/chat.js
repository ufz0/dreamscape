// Message rendering: user bubbles, plain assistant text, tool rows, reasoning blocks, actions.
import { escapeHtml } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { icon } from "./icons.js";

// Which messages the user expanded in this session.
const expanded = new Map();

function summarize(name, args) {
  if (name === "wiki_read") return `Read wiki page: ${args.name || "?"}`;
  if (name === "wiki_write") return `${args.mode === "append" ? "Updated" : "Wrote"} wiki page: ${args.name || "?"}`;
  if (name === "wiki_search") return `Searched: ${args.query || "?"}`;
  if (name === "wiki_list") return "Listed wiki pages";
  return name;
}

// Transcripts are append-only, so a regenerated answer sits after the one it replaced.
// Show only the last version of a turn: an assistant record followed directly by another one.
function currentTurns(records) {
  const skipped = new Set();
  for (let index = 0; index < records.length; index++) {
    if (records[index].role !== "assistant") continue;
    for (let next = index + 1; next < records.length && records[next].role !== "user"; next++) {
      if (records[next].role === "tool") break;
      if (records[next].role === "assistant") { skipped.add(index); break; }
    }
  }
  const deadCalls = new Set();
  for (const [index, record] of records.entries()) {
    if (skipped.has(index)) for (const call of record.tool_calls || []) deadCalls.add(call.id);
  }
  return records.filter((record, index) =>
    !skipped.has(index) && !(record.role === "tool" && deadCalls.has(record.tool_call_id)));
}

function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function toolRow(name, args, result, running) {
  const row = document.createElement("div");
  row.className = "tool";
  row.dataset.tool = name;

  const head = document.createElement("button");
  head.className = "tool-head";
  head.setAttribute("aria-expanded", "false");
  head.innerHTML = `${icon(running ? "spinner" : "wrench", 16)}` +
    `<span class="label">${escapeHtml(summarize(name, args))}</span>${icon("chevron", 14)}`;

  const body = document.createElement("div");
  body.className = "tool-body";
  const argumentsSection = `arguments\n${escapeHtml(pretty(args))}`;
  const resultSection = result === undefined ? "" : `result\n${escapeHtml(pretty(result))}`;
  body.innerHTML = `<div class="section">${argumentsSection}</div>` +
    (resultSection ? `<div class="section">${resultSection}</div>` : "");

  head.addEventListener("click", () => {
    const open = row.classList.toggle("open");
    head.setAttribute("aria-expanded", open ? "true" : "false");
    expanded.set(`tool:${row.dataset.key || summarize(name, args)}`, open);
  });

  if (result && typeof result === "object" && result.error) {
    row.classList.add("failed");
    head.querySelector(".label").textContent = `${summarize(name, args)} — ${result.error}`;
  }

  row.append(head, body);
  return row;
}

function thinkingBlock(settings, label) {
  const block = document.createElement("div");
  block.className = "thinking";

  const line = document.createElement("button");
  line.className = "line";
  line.setAttribute("aria-expanded", "false");
  line.innerHTML = `<span class="text-part">${escapeHtml(label)}</span>${icon("chevron", 14)}`;

  const body = document.createElement("div");
  body.className = "body";

  line.addEventListener("click", () => {
    const open = block.classList.toggle("open");
    line.setAttribute("aria-expanded", open ? "true" : "false");
    if (block.dataset.key) expanded.set(block.dataset.key, open);
  });

  block.append(line, body);
  return { block, line, body, label: () => line.querySelector(".text-part") };
}

function actions(model, copyText, options) {
  const row = document.createElement("div");
  row.className = "msg-actions";

  const copy = button("Copy message", "copy", () => {
    navigator.clipboard?.writeText(copyText());
    copy.innerHTML = icon("check", 16);
    setTimeout(() => { copy.innerHTML = icon("copy", 16); }, 2000);
  });
  row.append(copy);

  if (options.edit) {
    const edit = button("Edit and resend", "pencil", () => options.edit(copyText()));
    row.append(edit);
  }
  if (options.retry) {
    const retry = button("Regenerate", "retry", () => options.retry());
    row.append(retry);
  }

  if (model) {
    const label = document.createElement("span");
    label.className = "model";
    label.textContent = model;
    row.append(label);
  }
  return row;
}

function button(label, name, action) {
  const node = document.createElement("button");
  node.title = label;
  node.setAttribute("aria-label", label);
  node.innerHTML = icon(name, 16);
  node.addEventListener("click", action);
  return node;
}

export class ChatView {
  constructor(container, settings, options = {}) {
    this.container = container;
    this.settings = settings;
    this.onRetry = options.onRetry;
    this.onEdit = options.onEdit;
    this.onLimit = options.onLimit;
    this.jumpButton = options.jumpButton;
    this.stickToBottom = true;

    container.addEventListener("click", (event) => {
      const copy = event.target.closest(".copy[data-copy]");
      if (!copy) return;
      const code = copy.closest("pre").querySelector("code");
      navigator.clipboard?.writeText(code.textContent);
      copy.classList.add("copied");
      copy.innerHTML = `${icon("check", 14)}Copied`;
      setTimeout(() => {
        copy.classList.remove("copied");
        copy.innerHTML = "Copy";
      }, 2000);
    });

    container.addEventListener("scroll", () => {
      const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
      this.stickToBottom = nearBottom;
      if (this.jumpButton) this.jumpButton.hidden = nearBottom;
    });
  }

  clear() {
    this.container.innerHTML = "";
  }

  // ---------- history ----------

  render(records) {
    this.clear();
    this.stickToBottom = true;
    const rows = new Map();

    for (const record of currentTurns(records)) {
      if (record.role === "user") {
        this.addUser(record.content || "", { showActions: true });
        continue;
      }

      if (record.role === "tool") {
        const row = rows.get(record.tool_call_id);
        if (!row) continue;
        const result = (() => { try { return JSON.parse(record.content); } catch (error) { return record.content; } })();
        row.querySelector(".tool-body").innerHTML =
          `<div class="section">arguments\n${escapeHtml(pretty(row.dataset.args))}</div>` +
          `<div class="section">result\n${escapeHtml(pretty(result))}</div>`;
        if (result && typeof result === "object" && result.error) {
          row.classList.add("failed");
          row.querySelector(".label").textContent = `${summarize(record.name, JSON.parse(row.dataset.args))} — ${result.error}`;
        }
        continue;
      }

      const message = document.createElement("div");
      message.className = "msg assistant";

      if (record.reasoning) {
        const label = record.reasoning_ms ? `Thought for ${Math.round(record.reasoning_ms / 1000)}s` : "Thought process";
        const thinking = thinkingBlock(this.settings, label);
        thinking.block.dataset.key = record.ts;
        thinking.body.textContent = record.reasoning;
        const open = this.settings.showReasoning || expanded.get(record.ts);
        if (open) {
          thinking.block.classList.add("open");
          thinking.line.setAttribute("aria-expanded", "true");
        }
        expanded.set(record.ts, Boolean(open));
        message.append(thinking.block);
      }

      if (record.content) {
        const text = document.createElement("div");
        text.className = "text";
        text.innerHTML = renderMarkdown(record.content);
        message.append(text);
      }

      for (const call of record.tool_calls || []) {
        const row = toolRow(call.name, call.arguments, undefined, false);
        row.dataset.key = record.ts + (call.id || "");
        row.dataset.args = JSON.stringify(call.arguments);
        rows.set(call.id, row);
        if (expanded.get(`tool:${row.dataset.key}`)) {
          row.classList.add("open");
          row.querySelector(".tool-head").setAttribute("aria-expanded", "true");
        }
        message.append(row);
      }

      if (record.reasoning_only) message.append(note("No final answer, the model stopped during reasoning."));
      if (record.stopped) message.append(note("Stopped before the answer was complete."));
      if (record.content || record.reasoning) {
        message.append(actions(record.model || this.model, () => record.content || "", {
          retry: this.onRetry, edit: this.onEdit,
        }));
      }

      this.container.append(message);
      for (const node of this.container.querySelectorAll(".msg.last")) node.classList.remove("last");
      if (record.content || record.reasoning) message.classList.add("last");
    }

    this.scrollToBottom();
  }

  addUser(text, { showActions } = {}) {
    const message = document.createElement("div");
    message.className = "msg user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(text);
    message.append(bubble);
    if (showActions) {
      message.append(actions("", () => text, { edit: this.onEdit }));
    }
    this.container.append(message);
    if (this.stickToBottom) this.scrollToBottom();
    return message;
  }

  assistantMessage() {
    const message = document.createElement("div");
    message.className = "msg assistant current";
    this.current = message;
    this.textBlock = null;
    this.thinking = null;
    this.rows = new Map();
    this.raw = "";
    this.container.append(message);
    return message;
  }

  removeLastMessage() {
    const messages = [...this.container.querySelectorAll(".msg")];
    const last = messages[messages.length - 1];
    if (last?.classList.contains("assistant")) last.remove();
  }

  // ---------- streaming ----------

  handle(event) {
    if (event.type === "reasoning") {
      if (!this.thinking) {
        this.thinking = thinkingBlock(this.settings, "Thinking…");
        this.thinking.line.classList.add("live");
        this.startedAt = Date.now();
        if (this.settings.autoExpandThinking) {
          this.thinking.block.classList.add("open");
          this.thinking.line.setAttribute("aria-expanded", "true");
        }
        this.current.prepend(this.thinking.block);
      }
      this.thinking.body.textContent += event.content;
      const seconds = Math.round((Date.now() - this.startedAt) / 1000);
      this.thinking.label().textContent = `Thinking… ${seconds}s`;
      if (this.stickToBottom) this.scrollToBottom();
      return;
    }

    if (event.type === "delta") {
      this.raw += event.content;
      const block = this.textBlock || this.ensureTextBlock();
      block.innerHTML = renderMarkdown(this.raw);
      const caret = document.createElement("span");
      caret.className = "caret";
      block.append(caret);
      if (this.stickToBottom) this.scrollToBottom();
      return;
    }

    if (event.type === "tool") {
      let row = this.rows.get(event.id);
      if (!row) {
        row = toolRow(event.name, event.arguments, event.result, false);
        this.rows.set(event.id, row);
        this.current.append(row);
      } else {
        row.classList.remove("running");
        row.querySelector(".tool-head svg").outerHTML = icon("wrench", 16);
        const body = row.querySelector(".tool-body");
        body.innerHTML = `<div class="section">arguments\n${escapeHtml(pretty(event.arguments))}</div>` +
          `<div class="section">result\n${escapeHtml(pretty(event.result))}</div>`;
        if (event.result && typeof event.result === "object" && event.result.error) {
          row.classList.add("failed");
          row.querySelector(".label").textContent =
            `${summarize(event.name, event.arguments)} — ${event.result.error}`;
        }
      }
      this.textBlock = null;
      if (this.stickToBottom) this.scrollToBottom();
      return;
    }

    if (event.type === "assistant_message") {
      for (const call of event.tool_calls || []) {
        const row = toolRow(call.name, call.arguments, undefined, true);
        this.rows.set(call.id, row);
        this.current.append(row);
        this.textBlock = null;
        this.raw = "";
      }
      if (event.reasoning && this.thinking) {
        this.thinking.line.classList.remove("live");
        const seconds = event.reasoning_ms ? Math.round(event.reasoning_ms / 1000) : null;
        this.thinking.label().textContent = seconds ? `Thought for ${seconds}s` : "Thought process";
        if (this.settings.showReasoning) {
          this.thinking.block.classList.add("open");
          this.thinking.line.setAttribute("aria-expanded", "true");
        }
      }
      if (event.reasoning_only) this.current.append(note("No final answer, the model stopped during reasoning."));
      if (!event.tool_calls?.length) {
        const block = this.textBlock || this.ensureTextBlock();
        if (this.raw) block.innerHTML = renderMarkdown(this.raw);
        for (const caret of this.current.querySelectorAll(".caret")) caret.remove();
        this.current.classList.remove("current");
        for (const node of this.container.querySelectorAll(".msg.last")) node.classList.remove("last");
        this.current.classList.add("last");
        this.current.append(actions(this.model, () => this.raw, { retry: this.onRetry, edit: this.onEdit }));
        this.textBlock = null;
        this.raw = "";
      }
      return;
    }

    if (event.type === "stopped") {
      this.current.append(note("Stopped before the answer was complete."));
      return;
    }
    if (event.type === "limit") {
      this.current.append(note(`Tool loop stopped at ${event.iterations} iterations.`));
      return;
    }
    if (event.type === "limit_reached") {
      this.showLimit(event);
    }
  }

  // A token limit is a plain notice, not an error code: what happened, which limit, when it resets.
  showLimit(info) {
    if (!info) return;
    const message = document.createElement("p");
    message.className = "note limit";
    message.textContent = info.message || `Token limit reached (${info.window}).`;
    this.container.append(message);
    if (this.stickToBottom) this.scrollToBottom();
    if (this.onLimit) this.onLimit(info);
  }

  ensureTextBlock() {
    if (!this.textBlock) {
      this.textBlock = document.createElement("div");
      this.textBlock.className = "text";
      this.current.append(this.textBlock);
    }
    return this.textBlock;
  }

  scrollToBottom() {
    this.container.scrollTop = this.container.scrollHeight;
  }
}

function note(text) {
  const element = document.createElement("p");
  element.className = "note";
  text.split("\n").forEach((line, index) => {
    if (index) element.append("\n");
    element.append(document.createTextNode(line));
  });
  return element;
}
