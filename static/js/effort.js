// Reasoning effort control: a small button in the composer that opens a four-step slider.
// The last choice is remembered per conversation; new chats start from the Settings default.
import { api } from "./api.js";
import { openPanel } from "./ui.js";
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

export function openEffort(button, model, chatId, onChange) {
  openPanel(button, async (panel) => {
    const data = await mapping(model);
    const levels = data.levels && data.levels.length ? data.levels : LEVELS.map((level) => ({
      level, label: capitalize(level), description: DESCRIPTIONS[level], approximated: true,
    }));

    const current = effortFor(chatId);
    const index = Math.max(0, levels.findIndex((item) => item.level === current));

    const stops = document.createElement("div");
    stops.className = "effort-stops";
    for (const level of levels) {
      const stop = document.createElement("button");
      stop.textContent = level.label;
      stop.setAttribute("aria-pressed", String(level.level === current));
      stop.addEventListener("click", () => pick(level.level));
      stops.append(stop);
    }

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = String(levels.length - 1);
    slider.step = "1";
    slider.value = String(index);
    slider.setAttribute("aria-label", "Reasoning effort");
    slider.addEventListener("input", () => pick(levels[Number(slider.value)].level));

    const description = document.createElement("p");
    description.className = "effort-description";

    const note = document.createElement("p");
    note.className = "effort-note";
    const approximated = levels.some((level) => level.approximated);
    if (approximated || data.note) {
      note.hidden = false;
      note.textContent = data.note || "Effort is approximated via prompt for this model";
    }

    function pick(level) {
      const position = levels.findIndex((item) => item.level === level);
      const chosen = levels[position] || { level, label: capitalize(level), description: DESCRIPTIONS[level] };
      remember(chatId, level);
      [...stops.children].forEach((stop, index) => {
        stop.classList.toggle("on", index === position);
        stop.setAttribute("aria-pressed", String(index === position));
      });
      slider.value = String(position);
      slider.setAttribute("aria-valuetext", `${chosen.label} — ${chosen.description}`);
      description.textContent = `${chosen.label} — ${chosen.description}`;
      paintEffort(button, model, chatId);
      onChange?.(level);
    }

    panel.append(stops, slider, description, note);
    const position = levels.findIndex((item) => item.level === current);
    description.textContent = `${levels[position].label} — ${levels[position].description}`;
    slider.setAttribute("aria-valuetext", `${current} — ${levels[position].description}`);
    stops.children[position]?.classList.add("on");
  });
}

function capitalize(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
