// One icon set, one line style: 1.7 stroke, round caps, 24x24 grid.
import { LOGO } from "./logo.js";

const PATHS = {
  logo: LOGO,
  plus: `<path d="M12 5.5v13M5.5 12h13"/>`,
  chat: `<path d="M20 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>`,
  // The wiki mark: an open book, two pages and a spine. Drawn with straight edges so it still
  // reads as a book at 18px; the rounded version read as a plain square in the rail, and either
  // way it was easy to mistake for the chat bubble.
  book: `<path d="M12 7v11"/><path d="M12 7L4 4.5V15.5L12 18"/><path d="M12 7l8-2.5V15.5L12 18"/>`,
  search: `<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>`,
  gear: `<circle cx="12" cy="12" r="3.2"/><path d="M12 3.5v2.2M12 18.3v2.2M4.4 7.6l2 1.1M17.6 15.3l2 1.1M4.4 16.4l2-1.1M17.6 8.7l2-1.1"/>`,
  sliders: `<path d="M4 8h11M19 8h1M4 16h5M13 16h7"/><circle cx="17" cy="8" r="2"/><circle cx="11" cy="16" r="2"/>`,
  chevron: `<path d="M9 5l7 7-7 7"/>`,
  caret: `<path d="M6 10l6 6 6-6"/>`,
  copy: `<rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 5H6.5A2.5 2.5 0 0 0 4 7.5V16"/>`,
  check: `<path d="M5 12.5l4.5 4.5L19 7"/>`,
  pencil: `<path d="M16.5 4.5l3 3L8 19H5v-3z"/><path d="M14.5 6.5l3 3"/>`,
  dots: `<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>`,
  square: `<rect x="7" y="7" width="10" height="10" rx="2"/>`,
  arrowUp: `<path d="M12 19V5"/><path d="M6.5 10.5L12 5l5.5 5.5"/>`,
  arrowDown: `<path d="M12 5v14"/><path d="M6.5 13.5L12 19l5.5-5.5"/>`,
  wrench: `<path d="M14.5 14.5l-8 8a2.1 2.1 0 0 1-3-3l8-8"/><path d="M14.5 14.5l5.5-5.5a3.5 3.5 0 0 0-4.5-4.5L13 7l1.5 1.5z"/>`,
  brain: `<circle cx="12" cy="12" r="7.5"/><path d="M12 4.5v15M4.5 12h15"/>`,
  spinner: `<circle cx="12" cy="12" r="7" stroke-dasharray="30 14"/>`,
  x: `<path d="M6 6l12 12M18 6L6 18"/>`,
  retry: `<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>`,
  menu: `<path d="M4 7h16M4 12h16M4 17h16"/>`,
  panel: `<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M10 4v16"/>`,
  graph: `<circle cx="6.5" cy="12" r="2.6"/><circle cx="17" cy="7" r="2.6"/><circle cx="17" cy="17" r="2.6"/><path d="M8.9 11.1l5.6-2.8M8.9 12.9l5.6 2.8"/>`,
  globe: `<circle cx="12" cy="12" r="7.5"/><path d="M4.5 12h15"/><path d="M12 4.5c2.4 2.1 2.4 7.3 0 9.5M12 4.5c-2.4 2.1-2.4 7.3 0 9.5"/>`,
  play: `<path d="M7 5.5l12 6.5-12 6.5z"/>`,
  fit: `<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>`,
};

export function icon(name, size = 18) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" ` +
    `stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] || ""}</svg>`;
}
