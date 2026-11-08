// The web tools: which provider is active, and the per-conversation toggle in the tools popup.
// The default comes from Settings and applies when a conversation has no choice stored yet; a
// provider that is not configured turns the toggle off and explains why.
import { api } from "./api.js";
import { settings } from "./shell.js";

const MEMORY_KEY = "dreamscape.webtools";

let status = null;
let pending = null;            // a choice made for a chat that has no id yet

export async function providerStatus() {
  if (status) return status;
  try {
    status = await api("/api/web");
  } catch (error) {
    status = { provider: "", configured: false };
  }
  return status;
}

export function providerConfigured() {
  return Boolean(status && status.configured);
}

function memory() {
  try {
    return JSON.parse(localStorage.getItem(MEMORY_KEY) || "{}");
  } catch (error) {
    return {};
  }
}

export function webToolsFor(chatId) {
  if (!providerConfigured()) return false;
  if (!chatId) return pending === null ? Boolean(settings.webSearch) : pending;
  const saved = memory();
  return chatId in saved ? saved[chatId] : Boolean(settings.webSearch);
}

export function rememberWeb(chatId, on) {
  if (!chatId) { pending = on; return; }
  const saved = memory();
  saved[chatId] = on;
  localStorage.setItem(MEMORY_KEY, JSON.stringify(saved));
}

export function clearPending() {
  pending = null;
}
