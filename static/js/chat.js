// Message rendering: user bubbles, plain assistant text, tool rows, reasoning blocks, actions.
import { escapeHtml } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { icon } from "./icons.js";
import { canShowNote, noteText } from "./notes.js";

// Which messages the user expanded in this session.
const expanded = new Map();

const WEB_TOOLS = new Set(["web_search", "web_fetch"]);

function domainOf(url) {
  try {
    return new URL(url).hostname;
  } catch (error) {
    return String(url || "");
  }
}

function summarize(name, args) {
  if (name === "web_search") return `Searched the web: ${args.query || "?"}`;
  if (name === "web_fetch") return `Read page: ${domainOf(args.url) || "?"}`;
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

// The body of a tool row. Web results are shown as what was found — title, source, snippet —
// rather than as JSON, which is what makes the row readable when it is expanded.
function toolBody(name, args, result) {
  const body = document.createElement("div");
  body.className = "tool-body";

  const argumentsSection = document.createElement("div");
  argumentsSection.className = "section";
  argumentsSection.textContent = `arguments\n${pretty(args)}`;
  body.append(argumentsSection);
  if (result === undefined) return body;

  if (!result.error && WEB_TOOLS.has(name)) {
    if (name === "web_search" && result.results?.length) {
      const list = document.createElement("div");
      list.className = "results";
      for (const item of result.results) {
        const row = document.createElement("div");
        row.className = "result-row";
        const link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = item.title || item.url;
        const source = document.createElement("span");
        source.className = "domain";
        source.textContent = item.domain || domainOf(item.url);
        if (item.date) source.textContent += ` · ${item.date}`;
        row.append(link, source);
        if (item.snippet) {
          const snippet = document.createElement("div");
          snippet.className = "snippet";
          snippet.textContent = item.snippet;
          row.append(snippet);
        }
        list.append(row);
      }
      body.append(list);
      return body;
    }
    if (name === "web_fetch" && result.text) {
      const link = document.createElement("a");
      link.className = "section";
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = result.title || result.url;
      const text = document.createElement("div");
      text.className = "section page-text";
      text.textContent = result.text;
      body.append(link, text);
      return body;
    }
  }

  const resultSection = document.createElement("div");
  resultSection.className = "section";
  resultSection.textContent = `result\n${pretty(result)}`;
  body.append(resultSection);
  return body;
}

// Which pages a turn read. Domain and title only, no favicons, max six chips then a count.
function collectSources(name, result) {
  if (!result || result.error || !WEB_TOOLS.has(name)) return [];
  if (name === "web_search") {
    return (result.results || []).map((item) => ({ url: item.url, title: item.title, domain: item.domain }));
  }
  return [{ url: result.url, title: result.title || result.url, domain: domainOf(result.url) }];
}

function sourcesRow(sources) {
  const seen = new Set();
  const unique = sources.filter((item) => item.url && !seen.has(item.url) && seen.add(item.url));
  if (!unique.length) return null;

  const row = document.createElement("div");
  row.className = "sources";
  for (const item of unique.slice(0, 6)) {
    const chip = document.createElement("a");
    chip.className = "chip";
    chip.href = item.url;
    chip.target = "_blank";
    chip.rel = "noopener noreferrer";
    const domain = document.createElement("span");
    domain.className = "domain";
    domain.textContent = item.domain || domainOf(item.url);
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = item.title || item.url;
    chip.append(domain, title);
    row.append(chip);
  }
  if (unique.length > 6) {
    const more = document.createElement("span");
    more.className = "more";
    more.textContent = `+${unique.length - 6} more`;
    row.append(more);
  }
  return row;
}

function toolRow(name, args, result, running) {
  const row = document.createElement("div");
  row.className = "tool";
  row.dataset.tool = name;

  const head = document.createElement("button");
  head.className = "tool-head";
  head.setAttribute("aria-expanded", "false");
  head.innerHTML = `${icon(running ? "spinner" : (WEB_TOOLS.has(name) ? "globe" : "wrench"), 16)}` +
    `<span class="label">${escapeHtml(summarize(name, args))}</span>${icon("chevron", 14)}`;

  const body = toolBody(name, args, result);

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
    const turns = currentTurns(records);

    // Results are stored after the call that asked for them, so read them first: a tool row can
    // then be built complete, with the arguments and the result together.
    const results = new Map();
    for (const record of turns) {
      if (record.role !== "tool") continue;
      try {
        results.set(record.tool_call_id, JSON.parse(record.content));
      } catch (error) {
        results.set(record.tool_call_id, record.content);
      }
    }

    for (const record of turns) {
      if (record.role === "user") {
        this.addUser(record.content || "", { showActions: true });
        continue;
      }
      if (record.role === "tool") continue;

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

      const sources = [];
      for (const call of record.tool_calls || []) {
        const result = results.get(call.id);
        const row = toolRow(call.name, call.arguments, result, false);
        row.dataset.key = record.ts + (call.id || "");
        if (expanded.get(`tool:${row.dataset.key}`)) {
          row.classList.add("open");
          row.querySelector(".tool-head").setAttribute("aria-expanded", "true");
        }
        message.append(row);
        sources.push(...collectSources(call.name, result));
      }

      if (canShowNote(record)) message.append(note(noteText(record)));
      if (record.stopped) message.append(note("Stopped before the answer was complete."));
      const chips = sourcesRow(sources);
      if (chips) message.append(chips);
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
    this.sources = [];
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
        row.querySelector(".tool-head svg").outerHTML = icon(WEB_TOOLS.has(event.name) ? "globe" : "wrench", 16);
        row.querySelector(".tool-body").remove();
        row.append(toolBody(event.name, event.arguments, event.result));
        if (event.result && typeof event.result === "object" && event.result.error) {
          row.classList.add("failed");
          row.querySelector(".label").textContent =
            `${summarize(event.name, event.arguments)} — ${event.result.error}`;
        }
      }
      this.sources.push(...collectSources(event.name, event.result));
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
      // Nothing is still streaming for this step, so a note here is the real case: thinking and no
      // answer, on the last step of the turn. A step that ended in a tool call shows its tool row
      // instead, and a tool that is still running keeps the note off until the answer comes.
      const runningTool = [...this.rows.values()].some((row) => row.classList.contains("running"));
      if (canShowNote(event, { runningTool })) this.current.append(note(noteText(event)));
      if (!event.tool_calls?.length) {
        const block = this.textBlock || this.ensureTextBlock();
        if (this.raw) block.innerHTML = renderMarkdown(this.raw);
        for (const caret of this.current.querySelectorAll(".caret")) caret.remove();
        const chips = sourcesRow(this.sources);
        if (chips) this.current.append(chips);
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
