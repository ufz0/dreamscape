// Reasoning effort control: a small button in the composer that opens the level menu.
// The last choice is remembered per conversation; new chats start from the Settings default.
import { api } from "./api.js";
import { openMenu } from "./ui.js";
import { settings } from "./shell.js";

const LEVELS = ["none", "low", "medium", "high"];
const MEMORY_KEY = "dreamscape.effort";
const DESCRIPTIONS = {
  none: "answer directly",
  low: "brief thinking",
  medium: "balanced",
  high: "think thoroughly",
};

const cache = new Map();
let pending = "";            // a level chosen for a chat that has no id yet (a brand new chat)

function memory() {
  try {
    return JSON.parse(localStorage.getItem(MEMORY_KEY) || "{}");
  } catch (error) {
    return {};
  }
}

export function effortFor(chatId) {
  if (!chatId) return pending || settings.reasoningEffort || "medium";
  return memory()[chatId] || settings.reasoningEffort || "medium";
}

export function remember(chatId, level) {
  if (!chatId) { pending = level; return; }
  const saved = memory();
  saved[chatId] = level;
  localStorage.setItem(MEMORY_KEY, JSON.stringify(saved));
  pending = "";
}

async function mapping(model) {
  if (cache.has(model)) return cache.get(model);
  try {
    const data = await api(`/api/reasoning?model=${encodeURIComponent(model)}`);
    cache.set(model, data);
    return data;
  } catch (error) {
    return { levels: [], note: "" };
  }
}

export function clearPending() {
  pending = "";
}

export function paintEffort(button, model, chatId) {
  const label = button.querySelector(".name");
  if (label) label.textContent = capitalize(effortFor(chatId));
  button.classList.toggle("off", effortFor(chatId) === "none");
  button.title = `Reasoning effort: ${effortFor(chatId)} (${DESCRIPTIONS[effortFor(chatId)]})`;
}

// One row per level: the name, what it does, and a check on the one in use. Arrow keys, Enter,
// Esc and type-ahead work through the shared menu.
export async function openEffort(button, model, chatId, onChange) {
  const data = await mapping(model);
  const levels = data.levels && data.levels.length ? data.levels : LEVELS.map((level) => ({
    level, label: capitalize(level), description: DESCRIPTIONS[level], approximated: true,
  }));
  const current = effortFor(chatId);

  const items = levels.map((level) => ({
    label: level.label,
    description: level.description || DESCRIPTIONS[level.level] || "",
    checked: level.level === current,
    action: () => {
      remember(chatId, level.level);
      paintEffort(button, model, chatId);
      onChange?.(level.level);
    },
  }));

  const approximated = levels.some((level) => level.approximated);
  if (approximated || data.note) {
    items.push({ note: data.note || "Effort is approximated via prompt for this model" });
  }

  openMenu(button, items);
}

function capitalize(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
