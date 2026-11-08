// Login, first-run setup and self-registration. One form for three modes, no framework.
import { api, escapeHtml } from "./api.js";
import { icon } from "./icons.js";
import { applyTheme } from "./settings.js";
import { load } from "./settings.js";

const body = document.body;
const mode = body.dataset.mode;
const next = body.dataset.next || "/";
const status = document.getElementById("status");

const ENDPOINTS = { login: "/api/auth/login", setup: "/api/auth/setup", register: "/api/auth/register" };

for (const node of document.querySelectorAll("[data-icon]")) {
  node.innerHTML = icon(node.dataset.icon, Number(node.dataset.size) || 18);
}
applyTheme(load());

if (mode === "login" && body.dataset.register === "1") {
  document.getElementById("links").innerHTML = `Self-registration is on: <a href="/register">create an account</a>`;
}

document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector(".submit");
  button.disabled = true;
  status.classList.remove("error");
  status.textContent = "…";

  try {
    const result = await api(ENDPOINTS[mode], {
      method: "POST",
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      }),
    });
    status.textContent = result.note || `Signed in as ${result.user.username}.`;
    window.location.href = next;
  } catch (error) {
    status.classList.add("error");
    status.textContent = error.message;
    button.disabled = false;
  }
});
