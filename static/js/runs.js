// The run client: send a message, follow the run, stop it.
//
// A turn is a run on the server, not a request. Sending answers 202 with a run id, and the events
// come over a stream that may be closed and opened again: the server replays what it already
// produced and then continues live, so a page that reloads or loses its connection picks the same
// turn up where it left off. Only a cancel stops a run; a dropped stream does not.
//
// The event id is the position in the run, so the client remembers the last one it read and sends
// it as Last-Event-ID on a reconnect. Nothing is delivered twice.
import { api } from "./api.js";

const RETRY = [400, 1200, 3000];                       // backoff after a dropped stream
export const TERMINAL = ["completed", "failed", "cancelled", "interrupted"];

export async function send(conversationId, payload) {
  return api(`/api/conversations/${conversationId}/messages`, { method: "POST", body: JSON.stringify(payload) });
}

export async function stop(runId) {
  return api(`/api/runs/${runId}/cancel`, { method: "POST" });
}

export async function readRun(runId) {
  return api(`/api/runs/${runId}`);
}

// Follow one run, from the event after `after`. `onEvent` gets every event once, in order.
//
// Returns { replayable: false } when the run is not in this process's memory. There is nothing to
// stream then, and the transcript is the record: the caller reads the conversation instead.
export async function follow(runId, after, onEvent, signal) {
  let last = Number(after) || 0;
  let status = "";

  for (let attempt = 0; ; attempt += 1) {
    if (signal?.aborted) return { stopped: true, last };

    const headers = { Accept: "text/event-stream" };
    if (last) headers["Last-Event-ID"] = String(last);
    const response = await fetch(`/api/runs/${runId}/events?after=${last}`, { headers, signal });

    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      return { stopped: true, last };
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.detail || `${response.status} ${response.statusText}`);
      error.status = response.status;
      error.body = body;
      throw error;
    }

    // A run that is not in memory answers with JSON. That is the signal to read the transcript.
    if (!(response.headers.get("content-type") || "").includes("text/event-stream")) {
      const body = await response.json();
      return { replayable: false, run: body.run, note: body.note, last };
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let delivered = 0;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let cut = buffer.indexOf("\n\n");
      while (cut >= 0) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        cut = buffer.indexOf("\n\n");
        const event = readFrame(frame);
        if (!event) continue;
        delivered += 1;
        if (event.id > last) last = event.id;
        if (event.payload.type === "run_status") status = event.payload.status;
        onEvent(event.payload, event.id);
      }
    }

    if (TERMINAL.includes(status)) return { replayable: true, last };

    // The stream ended with the run still going: the connection dropped, the run did not.
    // Reconnect from the last event this client has seen, a few times, then give up.
    if (attempt >= RETRY.length) return { replayable: true, last, gaveUp: true };
    await wait(RETRY[attempt] * (delivered ? 1 : 2));
  }
}

function readFrame(frame) {
  let id = null;
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) id = Number(line.slice(3).trim());
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data || data === "[DONE]") return null;
  try {
    return { id: Number.isFinite(id) ? id : null, payload: JSON.parse(data) };
  } catch (error) {
    return null;
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
