// Settings live in localStorage. Theme follows the system unless a mode is chosen.
const DEFAULTS = {
  theme: "system",
  name: "",
  model: "",
  temperature: 0.7,
  system: "",
  showReasoning: false,
  autoExpandThinking: false,
  reasoningEffort: "medium",
  wikiIndex: true,
  sidebarCollapsed: false,
};

const KEY = "dreamscape.settings";

export function load() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch (error) {
    return { ...DEFAULTS };
  }
}

export function save(settings) {
  localStorage.setItem(KEY, JSON.stringify(settings));
  applyTheme(settings);
}

export function applyTheme(settings) {
  if (settings.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = settings.theme;
}

export function greeting(settings) {
  const hour = new Date().getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  return `Good ${part}, ${settings.name || "there"}`;
}
