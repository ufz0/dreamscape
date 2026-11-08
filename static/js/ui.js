// Popup menus, control panels, the settings dialog, and a small toast.
// All popups close on an outside click or Escape, and menu rows are keyboard navigable.
import { icon } from "./icons.js";

const menu = document.createElement("div");
menu.className = "menu";
menu.setAttribute("role", "menu");
menu.hidden = true;
document.body.appendChild(menu);

let openAnchor = null;
let activePanel = null;

function position(popup, anchor) {
  const box = anchor.getBoundingClientRect();
  popup.style.left = `${Math.min(Math.max(8, box.left), window.innerWidth - popup.offsetWidth - 8)}px`;

  // Put the popup below the anchor when it fits, otherwise above it. Anchors in the composer sit
  // at the bottom of the window, so a panel would otherwise be cut off.
  const below = box.bottom + 6;
  const room = window.innerHeight - below;
  if (popup.offsetHeight + 12 <= room) {
    popup.style.top = `${below}px`;
  } else {
    popup.style.top = `${Math.max(8, box.top - popup.offsetHeight - 6)}px`;
  }
}

export function openMenu(anchor, items) {
  closePopups();
  openAnchor = anchor;
  anchor.classList.add("active");
  menu.innerHTML = "";

  for (const item of items) {
    if (item.separator) { menu.appendChild(document.createElement("hr")); continue; }
    if (item.note) {
      // A line that explains the list. It is not a choice, so it is not focusable.
      const line = document.createElement("p");
      line.className = "menu-note";
      line.textContent = item.note;
      menu.append(line);
      continue;
    }
    const button = document.createElement("button");
    button.setAttribute("role", "menuitem");
    if (item.danger) button.classList.add("danger");
    if (item.checked) button.classList.add("checked");
    button.dataset.type = String(item.label).toLowerCase();
    button.innerHTML = `<span class="tick">${item.checked ? icon("check", 16) : ""}</span>` +
      `<span class="item-text"><span class="item-label">${item.label}</span>` +
      (item.description ? `<span class="item-desc">${item.description}</span>` : "") +
      `</span>`;
    button.addEventListener("click", () => { closePopups(); item.action?.(); });
    menu.appendChild(button);
  }

  menu.hidden = false;
  position(menu, anchor);
  menu.querySelector("[role=menuitem]")?.focus();
}

// A popup that holds controls rather than menu rows.
export function openPanel(anchor, build) {
  closePopups();
  openAnchor = anchor;
  anchor.classList.add("active");
  const panel = document.createElement("div");
  panel.className = "menu panel";
  document.body.appendChild(panel);
  activePanel = panel;

  const place = () => position(panel, anchor);
  const built = build(panel);
  place();
  // A build that fetches fills the panel later, so position it again once it has content.
  if (built && typeof built.then === "function") built.then(place);
  panel.querySelector("input, select, textarea, button")?.focus();
  return panel;
}

export function closePopups() {
  menu.hidden = true;
  if (activePanel) activePanel.remove();
  activePanel = null;
  if (openAnchor) openAnchor.classList.remove("active");
  openAnchor = null;
}

document.addEventListener("mousedown", (event) => {
  if (!menu.hidden && !menu.contains(event.target)) closePopups();
  if (activePanel && !activePanel.contains(event.target)) closePopups();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!menu.hidden || activePanel) { closePopups(); return; }
    if (dialog) { closeDialog(); return; }
  }
  if (menu.hidden) return;
  const items = [...menu.querySelectorAll("[role=menuitem]")];
  if (!items.length) return;
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
  if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
  // Type-ahead: a letter jumps to the row that starts with it.
  if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey && !event.repeat) {
    const letter = event.key.toLowerCase();
    const match = items.find((item) => (item.dataset.type || "").startsWith(letter));
    if (match) { event.preventDefault(); match.focus(); }
  }
});

let dialog = null;

export function openDialog(title, build) {
  closeDialog();
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  const card = document.createElement("div");
  card.className = "dialog";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-label", title);
  card.innerHTML = `<button class="icon-btn close" aria-label="Close" title="Close">${icon("x", 16)}</button><h2>${title}</h2>`;
  card.querySelector(".close").addEventListener("click", closeDialog);
  build(card);
  overlay.appendChild(card);
  overlay.addEventListener("mousedown", (event) => { if (event.target === overlay) closeDialog(); });
  document.body.appendChild(overlay);
  dialog = overlay;
  card.querySelector("input, select, textarea")?.focus();
  return card;
}

export function closeDialog() {
  if (dialog) { dialog.remove(); dialog = null; }
}

let toastTimer = null;

export function toast(message) {
  let node = document.getElementById("toast");
  if (!node) {
    node = document.createElement("div");
    node.id = "toast";
    node.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:90;padding:8px 12px;" +
      "border:1px solid var(--border);border-radius:10px;background:var(--panel);box-shadow:var(--shadow-menu);font-size:13px";
    document.body.appendChild(node);
  }
  node.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.textContent = ""; }, 4000);
}
