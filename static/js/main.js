// Chat page: top bar, thread, composer, send flow. The sidebar and settings come from shell.js.
import { api, download } from "./api.js";
import { icon } from "./icons.js";
import { openMenu, openPanel, toast } from "./ui.js";
import { ChatView, stopNote } from "./chat.js";
import { follow as followRun, send as sendRun, stop as stopRun, TERMINAL } from "./runs.js";
import { settings, startShell, renderList, setActiveChat, currentModel, loadModels, greeting,
  controlRow, numberInput, textArea, checkbox, modelGroups, displayName, markRun, setTabDot } from "./shell.js";
import { save } from "./settings.js";
import { effortFor, openEffort, paintEffort, remember, clearPending } from "./effort.js";
import { providerStatus, webToolsFor, rememberWeb, clearPending as clearWebPending } from "./webtools.js";
import { account, mustChange } from "./account.js";

const el = (id) => document.getElementById(id);
const ui = {
  main: el("main"), thread: el("thread"), jump: el("jump"), topbar: el("topbar"),
  title: el("chat-title"), chatMenu: el("chat-menu"), greeting: el("greeting-text"), pills: el("pills"),
  input: el("input"), send: el("send"), modelBtn: el("model-btn"), options: el("options"),
  effort: el("effort"), attach: el("attach"), disclaimer: el("disclaimer"),
};

let chatId = null;
let generating = false;
let activeRun = null;   // the run this page is watching. Watching it is not what keeps it going.
let stream = null;      // this page's connection to the run; closing it does not stop the run
let limitUntil = null;   // when a reached token limit resets; sending stays off until then

const view = new ChatView(ui.thread, settings, {
  jumpButton: ui.jump,
  onRetry: () => regenerate(),
  onEdit: (text) => { ui.input.value = text; grow(); ui.input.focus(); },
  onLimit: (info) => pauseUntil(info),
});

function pauseUntil(info) {
  if (!info) return;
  const resets = new Date(info.resets_at);
  limitUntil = Number.isFinite(resets.getTime()) ? resets : null;
  ui.input.placeholder = "Sending is paused until the limit resets.";
  refreshSendButton();
  if (limitUntil) {
    setTimeout(() => {
      limitUntil = null;
      ui.input.placeholder = ui.thread.querySelector(".msg") ? "Reply..." : "How can I help you today?";
      refreshSendButton();
    }, Math.max(0, limitUntil.getTime() - Date.now()));
  }
}

// A reached limit is stated in the same words the server answers with, where it can be seen:
// in the conversation, or under the greeting when the chat is still empty.
function showLimitNotice(info) {
  if (!ui.main.classList.contains("empty")) {
    view.showLimit(info);
    return;
  }
  const note = document.createElement("p");
  note.className = "note limit";
  note.textContent = info.message;
  ui.greeting.closest(".greeting").append(note);
  pauseUntil(info);
}

// ---------- views ----------

function showConversation(hasMessages) {
  ui.main.classList.toggle("empty", !hasMessages);
  ui.topbar.hidden = !chatId;
  ui.disclaimer.hidden = !hasMessages;
  ui.input.placeholder = limitUntil
    ? "Sending is paused until the limit resets."
    : (hasMessages ? "Reply..." : "How can I help you today?");
}

function newChat() {
  chatId = null;
  activeRun = null;
  stream?.abort();          // this page stops watching; the run is not stopped by that
  clearPending();
  clearWebPending();
  setActiveChat(null);
  view.clear();
  ui.title.textContent = "New chat";
  showConversation(false);
  history.replaceState(null, "", "/");
  paintEffort(ui.effort, currentModel(), null);
  refreshSendButton();
  ui.input.focus();
}

async function openChat(id) {
  const data = await api(`/api/conversations/${id}/messages`);
  chatId = id;
  setActiveChat(id);
  view.model = data.conversation.model || currentModel();
  ui.title.textContent = data.conversation.title || "New chat";

  const records = data.messages;
  const hasMessages = records.some((record) => record.role === "user");
  const run = data.active_run;

  if (run) {
    // A turn is still being written here. The transcript is the record for what is finished, and
    // the run's own turn comes from its events, so nothing is shown twice.
    const own = records.filter((record) => record.run === run.id);
    view.render(records.filter((record) => record.run !== run.id));

    let after = 0;
    if (!run.buffered) {
      // Not in the process that is serving this request: there is no event stream for it.
      view.render(records);
      view.showNote("This turn is still generating on the server. The transcript shows what has been written so far.");
      setGenerating(true);
    } else if (run.first_event > 1) {
      // The buffer is bounded, so the beginning of this turn is only in the transcript.
      view.replayRun(own);
      after = run.last_event;
      watch(run, after);
    } else {
      watch(run, 0);
    }
  } else {
    // A partial answer in the transcript is a run that stopped early, so the wording comes from the
    // run's own status when it is known. A turn that ended without writing anything has no record,
    // and the run row is the only thing that says why.
    const last = data.last_run;
    view.stoppedNote = last ? stopNote(last.status, last.error) : "";
    view.render(records);
    setGenerating(false);
    if (last && ["failed", "cancelled", "interrupted"].includes(last.status)
        && !records.some((record) => record.run === last.id)) {
      view.showNote(stopNote(last.status, last.error));
    }
  }

  showConversation(hasMessages);
  // A model an admin removed is not a routing failure: the chat is fine, the choice is not.
  if (data.model_unavailable) view.showNote("The model this chat used is not on any connection any more. Choose another one before sending.");
  paintEffort(ui.effort, currentModel(), chatId);
  history.replaceState(null, "", `?c=${encodeURIComponent(id)}`);
}

// ---------- following a run ----------

async function watch(run, after) {
  activeRun = run;
  stream = new AbortController();
  setGenerating(true);

  let result;
  try {
    result = await followRun(run.id, after, (event) => {
      view.handle(event);
      if (event.type === "title") applyTitle(event.title);
      if (event.type === "run_status" && TERMINAL.includes(event.status)) endRun(event.status);
    }, stream.signal);
  } catch (error) {
    if (error.name !== "AbortError") toast(`Cannot follow this run: ${error.message}`);
    return;
  }

  if (result.stopped) return;
  if (!result.replayable) {
    // Nothing left to stream. The transcript is the record, so read the conversation again.
    await openChat(run.conversation_id);
  }
}

// A run that stopped without producing its last answer says so in the thread; the sidebar row
// keeps a dot until the chat is opened, which is what tells the user a result arrived.
async function endRun(status) {
  generating = false;
  activeRun = null;
  stream = null;
  setGenerating(false);
  if (!markRun(chatId, status)) await renderList();
}

function applyTitle(title) {
  if (!title) return;
  ui.title.textContent = title;
  renderList();
}

// ---------- top bar ----------

ui.title.addEventListener("click", () => {
  if (!chatId) return;

  const input = document.createElement("input");
  input.type = "text";
  input.className = "title-input";
  input.value = ui.title.textContent;
  input.setAttribute("aria-label", "Chat name");
  ui.title.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    input.replaceWith(ui.title);
    if (commit && value) {
      ui.title.textContent = value;
      await api(`/api/conversations/${chatId}/rename`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: value }),
      });
      await renderList();
    }
    ui.title.focus();
  };

  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
});

ui.chatMenu.addEventListener("click", () => {
  if (!chatId) return;
  openMenu(ui.chatMenu, [
    { label: "Export as markdown", action: () => exportChat("md") },
    { label: "Export as JSON", action: () => exportChat("json") },
    { separator: true },
    { label: "Delete this chat", danger: true, action: () => deleteChat() },
  ]);
});

async function deleteChat() {
  if (!window.confirm(`Delete "${ui.title.textContent}"?`)) return;
  await api(`/api/conversations/${chatId}`, { method: "DELETE" });
  newChat();
  await renderList();
}

async function exportChat(format) {
  const data = await api(`/api/conversations/${chatId}/messages`);
  const name = (data.conversation.title || "chat").replace(/[^\w\s-]/g, "").slice(0, 60) || "chat";

  if (format === "json") {
    download(`${name}.json`, JSON.stringify(data.messages, null, 2), "application/json");
    return;
  }

  const lines = [`# ${data.conversation.title || "Chat"}`, ""];
  for (const record of data.messages) {
    if (record.role === "system") continue;
    lines.push(`## ${record.role}`);
    if (record.content) lines.push(record.content);
    for (const call of record.tool_calls || []) {
      lines.push(`tool call: ${call.name}(${JSON.stringify(call.arguments)})`);
    }
    if (record.reasoning) lines.push(`thinking: ${record.reasoning}`);
    lines.push("");
  }
  download(`${name}.md`, lines.join("\n"), "text/markdown");
}

// ---------- composer ----------

function grow() {
  ui.input.style.height = "auto";
  const limit = Math.round(window.innerHeight * 0.4);
  ui.input.style.height = `${Math.min(Math.max(ui.input.scrollHeight, 24), limit)}px`;
}

function refreshSendButton() {
  if (generating) return;
  if (limitUntil || mustChange()) { ui.send.disabled = true; return; }
  ui.send.disabled = !ui.input.value.trim();
}

function setGenerating(on) {
  generating = on;
  ui.send.innerHTML = icon(on ? "square" : "arrowUp", 16);
  ui.send.title = on ? "Stop generating" : "Send";
  ui.send.setAttribute("aria-label", on ? "Stop generating" : "Send");
  ui.send.disabled = on ? false : !ui.input.value.trim();
  ui.modelBtn.disabled = on;
  ui.options.disabled = on;
}

ui.input.addEventListener("input", () => { grow(); refreshSendButton(); });

ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send(ui.input.value);
  }
  if (event.key === "Escape") ui.input.blur();
});

ui.send.addEventListener("click", () => {
  if (generating) stopGeneration();
  else send(ui.input.value);
});

async function send(text, regenerate = false) {
  text = (text || "").trim();
  if (!text || generating) return;
  if (limitUntil || mustChange()) return;

  const level = effortFor(chatId);
  const web = webToolsFor(chatId);
  const firstTurn = !chatId;
  if (!chatId) {
    const created = await api("/api/conversations", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: currentModel(), system: settings.system }),
    });
    chatId = created.conversation.id;
    setActiveChat(chatId);
    ui.title.textContent = text.slice(0, 40);
    history.replaceState(null, "", `?c=${encodeURIComponent(chatId)}`);
  }
  remember(chatId, level);
  rememberWeb(chatId, web);

  // Sending answers as soon as the run exists; the turn is the run, not this request.
  let started;
  try {
    started = await sendRun(chatId, {
      message: text, model: currentModel(), system: settings.system,
      temperature: settings.temperature, wiki_index: settings.wikiIndex, regenerate,
      reasoning_effort: level, web_search: web, generate_titles: settings.generateTitles,
    });
  } catch (error) {
    // A limit and a chat that is still generating are both things the thread should say, not a toast.
    if (error.status === 429) {
      view.showLimit(error.body);
    } else if (error.status === 409) {
      const detail = error.body?.detail || error.message;
      if (activeRun && activeRun.id === error.body?.run_id) { view.showNote(detail); return; }
      await openChat(chatId);
      view.showNote(detail);
    } else {
      toast(`Request failed: ${error.message}`);
    }
    return;
  }

  // The thread shows what the run reports, so the same turn is never shown twice: the message the
  // user typed comes from the run's own event unless it is already on screen.
  if (regenerate) {
    view.removeLastMessage();
    view.markShown(started.run.id);
    view.assistantMessage();
  }
  ui.input.value = "";
  grow();
  view.model = currentModel();
  view.stickToBottom = true;
  ui.jump.hidden = true;
  showConversation(true);
  refreshSendButton();

  // The row for a chat that did not exist a moment ago has to be read, not painted in place.
  if (!markRun(chatId, "running")) await renderList();

  await watch(started.run, 0);
}

async function regenerate() {
  if (!chatId || generating) return;
  const data = await api(`/api/conversations/${chatId}/messages`);
  const lastUser = [...data.messages].reverse().find((record) => record.role === "user");
  if (!lastUser) return;
  await send(lastUser.content || "", true);
}

// Stop is the only thing that ends a run. Closing this page's stream does not.
async function stopGeneration() {
  if (!activeRun) return;
  try {
    await stopRun(activeRun.id);
  } catch (error) {
    toast(`Could not stop this run: ${error.message}`);
  }
}

// ---------- composer popups ----------

ui.options.addEventListener("click", () => {
  openPanel(ui.options, async (panel) => {
    const provider = await providerStatus();
    panel.append(
      controlRow("Temperature", numberInput(settings.temperature, (value) => {
        settings.temperature = value;
        save(settings);
      })),
      controlRow("System prompt", textArea(settings.system, (value) => {
        settings.system = value;
        save(settings);
      })),
      controlRow("Inject wiki index", checkbox(settings.wikiIndex, (value) => {
        settings.wikiIndex = value;
        save(settings);
      })),
      webRow(provider),
    );
  });
});

// The web tools are remembered per conversation, like the reasoning level.
function webRow(provider) {
  const row = controlRow("Web search", checkbox(webToolsFor(chatId), (value) => {
    rememberWeb(chatId, value);
    settings.webSearch = value;
    save(settings);
  }));
  const input = row.querySelector("input");
  if (!provider.configured) {
    input.disabled = true;
    row.title = "No search provider is configured. Set SEARCH_PROVIDER, or BRAVE_API_KEY for Brave.";
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = "not configured";
    row.append(hint);
  } else {
    const hint = document.createElement("span");
    hint.className = "muted";
    hint.textContent = provider.provider;
    row.append(hint);
    row.title = `Search provider: ${provider.provider}. Cached for ${provider.cache_minutes} minutes, ${provider.rate_limit} searches per ${provider.rate_limit_minutes} minutes.`;
  }
  return row;
}

ui.modelBtn.addEventListener("click", async () => {
  const groups = await modelGroups();
  if (!groups.length) return;

  const items = [];
  for (const group of groups) {
    items.push({ group: group.connection.name });
    for (const model of group.models) {
      items.push({
        label: model.display_name,
        description: model.id,
        checked: model.namespaced === currentModel(),
        action: () => { chooseModel(model.namespaced); paintModel(); },
      });
    }
  }
  openMenu(ui.modelBtn, items);
});

function paintModel() {
  ui.modelBtn.querySelector(".name").textContent = displayName(currentModel());
  paintEffort(ui.effort, currentModel(), chatId);
}

ui.effort.addEventListener("click", () => openEffort(ui.effort, currentModel(), chatId, () => paintEffort(ui.effort, currentModel(), chatId)));

ui.attach.disabled = true;

// ---------- empty state ----------

const SUGGESTIONS = [
  { label: "Write", prompt: "Write a short paragraph about " },
  { label: "Learn", prompt: "Explain how " },
  { label: "Code", prompt: "Write a small script that " },
  { label: "From the wiki", prompt: "Read the wiki page about " },
];

function buildPills() {
  for (const suggestion of SUGGESTIONS) {
    const pill = document.createElement("button");
    pill.className = "pill";
    pill.textContent = suggestion.label;
    pill.addEventListener("click", () => {
      ui.input.value = suggestion.prompt;
      grow();
      refreshSendButton();
      ui.input.focus();
    });
    ui.pills.append(pill);
  }
}

// ---------- start ----------

async function start() {
  await startShell({
    onNewChat: newChat,
    onOpenChat: openChat,
    onRename: (id, title) => { if (id === chatId) ui.title.textContent = title; },
    onDelete: (id) => { if (id === chatId) newChat(); },
    onModelChange: paintModel,
    onEffortChange: () => paintEffort(ui.effort, currentModel(), chatId),
  });

  buildPills();
  ui.greeting.textContent = greeting(settings, account().user ? account().user.username : "");
  await loadModels();
  paintModel();
  grow();

  const id = new URLSearchParams(location.search).get("c");
  if (id) await openChat(id);
  else newChat();

  // A limit that is already reached is shown before the first request, in the same wording the
  // server would answer with, and sending stays off until it resets.
  for (const window of (account() && account().usage && account().usage.windows) || []) {
    if (window.reached) showLimitNotice(window);
  }

  ui.input.focus();
}

start();
