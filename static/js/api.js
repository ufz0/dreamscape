// Small fetch helpers: JSON API, the SSE chat stream, and a file download.
// Every state-changing request sends the CSRF cookie back as a header, which is what the server
// checks. A 401 from an API call means the session is gone, so the page goes to /login.

export function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.method && options.method !== "GET" ? { "X-CSRF-Token": csrfToken() } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    const error = new Error("Please log in.");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return response.json();
}

export async function streamChat(payload, onEvent, signal) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify(payload),
    signal,
  });
  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    const error = new Error("Please log in.");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || body.message || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const chunk = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 2);
      cut = buffer.indexOf("\n\n");
      if (chunk.startsWith("data:")) {
        const payloadText = chunk.slice(5).trim();
        if (payloadText && payloadText !== "[DONE]") onEvent(JSON.parse(payloadText));
      }
    }
  }
}

export function escapeHtml(text) {
  return String(text).replace(/[&<>]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[character]));
}

export function download(filename, contents, mediaType = "text/plain") {
  const url = URL.createObjectURL(new Blob([contents], { type: mediaType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

// 12.3k / 1.2M, with the exact number available as a tooltip.
export function formatK(value) {
  const number = Number(value) || 0;
  if (number < 1000) return String(number);
  if (number < 1000000) return `${trimNumber(number / 1000)}k`;
  return `${trimNumber(number / 1000000)}M`;
}

function trimNumber(value) {
  return String(Math.round(value * 10) / 10).replace(/\.0$/, "");
}
