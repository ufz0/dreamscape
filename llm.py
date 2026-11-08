"""Thin OpenAI-compatible client for one inference connection.

Everything here normalises upstream responses into one event shape:
  {"type": "delta", "content": str}                answer text only
  {"type": "reasoning", "content": str, "field": str}   thinking text, from the reasoning_content
      field or from <think>...</think> inside content. `field` says which one it was: the admin panel
      reports the field names an engine actually answers with.
  {"type": "tool_calls", "tool_calls": [...]}        complete tool calls for this turn
  {"type": "usage", "usage": {...}}

Answer text and thinking are kept apart because the UI shows thinking collapsed by default and the
transcript stores thinking in its own field.

  {"type": "finish", "finish_reason": str}           how the turn ended, after the text is complete

The functions take a resolved target from connections.py (base url, headers, paths, timeout), so one
protocol can point at several engines at the same time.
"""
import json
from typing import AsyncIterator

import httpx

import config

DECODER = json.JSONDecoder()

OPEN_TAGS = ("<think>", "<thinking>")
CLOSE_TAGS = ("</think>", "</thinking>")
MAX_PARTIAL = max(len(tag) for tag in OPEN_TAGS + CLOSE_TAGS) - 1


class ThinkSplitter:
    """Split <think>...</think> out of streamed content, including tags split across chunks."""

    def __init__(self):
        self.pending = ""
        self.in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        buffer = self.pending + text
        self.pending = ""
        events = []
        while buffer:
            if self.in_think:
                close = next((tag for tag in CLOSE_TAGS if tag in buffer), None)
                if close is None:
                    hold = _possible_tag(buffer)
                    piece = buffer[: len(buffer) - len(hold)]
                    if piece:
                        events.append(("reasoning", piece))
                    self.pending = buffer[len(buffer) - len(hold):]
                    return events
                index = buffer.index(close)
                if index:
                    events.append(("reasoning", buffer[:index]))
                buffer = buffer[index + len(close):]
                self.in_think = False
            else:
                open_tag = next((tag for tag in OPEN_TAGS if tag in buffer), None)
                if open_tag is None:
                    hold = _possible_tag(buffer)
                    piece = buffer[: len(buffer) - len(hold)]
                    if piece:
                        events.append(("delta", piece))
                    self.pending = buffer[len(buffer) - len(hold):]
                    return events
                index = buffer.index(open_tag)
                if index:
                    events.append(("delta", buffer[:index]))
                buffer = buffer[index + len(open_tag):]
                self.in_think = True
        return events

    def flush(self) -> list[tuple[str, str]]:
        if not self.pending:
            return []
        kind = "reasoning" if self.in_think else "delta"
        text, self.pending = self.pending, ""
        return [(kind, text)]


def _possible_tag(buffer: str) -> str:
    """Longest tail of the buffer that could still turn out to be a think tag."""
    for length in range(min(len(buffer), MAX_PARTIAL), 0, -1):
        tail = buffer[-length:]
        if any(tag.startswith(tail) for tag in OPEN_TAGS + CLOSE_TAGS):
            return tail
    return ""


def parse_text_tool_call(text: str, tool_names: set | None = None) -> dict | None:
    """Fallback for engines without native tool calling: a JSON tool call in plain text.

    Looks for any object in the reply that names a tool, for example
    {"tool": "wiki_read", "args": {"name": "my-page"}}. A bare {"name": ...} only counts
    when that name is a tool we actually offer.
    """
    if not text or ('"tool"' not in text and '"name"' not in text):
        return None
    candidates = []
    for position, character in enumerate(text):
        if character != "{":
            continue
        try:
            data, _ = DECODER.raw_decode(text[position:])
        except ValueError:
            continue
        if isinstance(data, dict):
            candidates.append(data)

    # Prefer an object that says "tool"; "name" is accepted as an alias.
    for key in ("tool", "name"):
        for data in candidates:
            value = data.get(key)
            if not isinstance(value, str):
                continue
            if tool_names and value not in tool_names:
                continue
            args = data.get("args") or data.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"value": args}
            return {"id": "text", "name": value, "arguments": args}
    return None


def default_target() -> dict:
    """The target from .env, for the code paths that do not have a connection row (a test, or a
    script started before the database has any connections)."""
    headers = {"Content-Type": "application/json"}
    if config.API_KEY:
        headers["Authorization"] = f"Bearer {config.API_KEY}"
    return {"connection_id": None, "slug": "", "base_url": config.BASE_URL, "headers": headers,
            "timeout": 120.0, "path_models": "/models", "path_chat": "/chat/completions"}


def client(target: dict) -> httpx.AsyncClient:
    """One connection to one engine. `target` is what connections.py resolved for it."""
    return httpx.AsyncClient(base_url=target["base_url"], headers=dict(target["headers"]),
                            timeout=httpx.Timeout(float(target.get("timeout") or 120.0), connect=10.0))


def request_model(target: dict, model: str) -> str:
    """The id this engine knows. The namespaced form is ours, not the server's.

    A slug never contains a slash, so the first slash separates it from the model id, which may
    itself contain one (`team/open-model`).
    """
    if target.get("slug") and "/" in model:
        return model.split("/", 1)[1]
    return model


async def list_models(target: dict) -> list[dict]:
    async with client(target) as c:
        r = await c.get(target.get("path_models") or "/models")
        r.raise_for_status()
        return r.json().get("data", [])


async def complete(target: dict, model: str, messages: list[dict], temperature: float,
                   max_tokens: int | None = None, extra_body: dict | None = None) -> dict:
    """One completion, not streamed. Returns the text and whatever usage the server reported."""
    payload = {"model": request_model(target, model), "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = max_tokens
    for key, value in (target.get("body_overrides") or {}).items():
        payload[key] = value
    for key, value in (extra_body or {}).items():
        payload[key] = value

    async with client(target) as c:
        r = await c.post(target.get("path_chat") or "/chat/completions", json=payload)
        r.raise_for_status()
        body = r.json()

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {"text": message.get("content") or "", "usage": body.get("usage") or {},
            # What the engine answered with, for the capability check: the field names it used.
            "fields": sorted(key for key in message if key != "content" and key != "role")}


async def stream_chat(target: dict, model: str, messages: list[dict], temperature: float,
                     tools: list[dict] | None = None, max_tokens: int | None = None,
                     extra_body: dict | None = None) -> AsyncIterator[dict]:
    """Stream one completion. Tool call arguments arrive as deltas, so assemble them by index.

    The connection's own request fields come from `target`; `extra_body` carries what the reasoning
    mapping resolved for this turn (reasoning_effort, chat_template_kwargs, ...). It is merged last,
    so a per-turn mapping wins over the connection's defaults.
    """
    payload = {"model": request_model(target, model), "messages": messages, "stream": True}
    # Ask for the usage block in the last streamed chunk; servers that support stream_options send
    # it as an extra chunk with no choices. When it does not come back, the caller estimates.
    payload["stream_options"] = {"include_usage": True}
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    if max_tokens:
        payload["max_tokens"] = max_tokens
    for key, value in (target.get("body_overrides") or {}).items():
        payload[key] = value
    for key, value in (extra_body or {}).items():
        payload[key] = value

    calls: dict[int, dict] = {}
    splitter = ThinkSplitter()
    finish_reason = None
    async with client(target) as c:
        async with c.stream("POST", target.get("path_chat") or "/chat/completions", json=payload) as r:
            r.raise_for_status()
            async for raw in r.aiter_lines():
                if not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if not data or data == "[DONE]":
                    break
                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    for kind, piece in splitter.feed(delta.get("content") or ""):
                        if kind == "reasoning":
                            yield {"type": "reasoning", "content": piece, "field": "think_tag"}
                        else:
                            yield {"type": kind, "content": piece}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        yield {"type": "reasoning", "content": reasoning, "field": "reasoning_content"}
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["name"] = function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                        if finish_reason == "tool_calls" and calls:
                            assembled = [calls[i] for i in sorted(calls)]
                            yield {"type": "tool_calls", "tool_calls": assembled}
                if chunk.get("usage"):
                    yield {"type": "usage", "usage": chunk["usage"]}
    # Held-back text that could still have been part of a think tag, then how the turn ended.
    # The finish reason is reported last so the caller knows whether more tool iterations follow.
    for kind, piece in splitter.flush():
        if kind == "reasoning":
            yield {"type": "reasoning", "content": piece, "field": "think_tag"}
        else:
            yield {"type": kind, "content": piece}
    if finish_reason:
        yield {"type": "finish", "finish_reason": finish_reason}
