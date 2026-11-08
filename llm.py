"""Thin OpenAI-compatible client for the Strata engine.

Everything here normalises upstream responses into one event shape:
  {"type": "delta", "content": str}          answer text only
  {"type": "reasoning", "content": str}       thinking text, either from the reasoning_content
                                             field or from <think>...</think> inside content
  {"type": "tool_calls", "tool_calls": [...]} complete tool calls for this turn
  {"type": "usage", "usage": {...}}

Answer text and thinking are kept apart because the UI shows thinking collapsed by default and
the transcript stores thinking in its own field.
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


def client() -> httpx.AsyncClient:
    headers = {"Content-Type": "application/json"}
    if config.API_KEY:
        headers["Authorization"] = f"Bearer {config.API_KEY}"
    return httpx.AsyncClient(base_url=config.BASE_URL, headers=headers, timeout=httpx.Timeout(120.0, connect=10.0))


async def list_models() -> list[dict]:
    async with client() as c:
        r = await c.get("/models")
        r.raise_for_status()
        return r.json().get("data", [])


async def stream_chat(messages: list[dict], model: str, temperature: float,
                     tools: list[dict] | None = None, max_tokens: int | None = None,
                     extra_body: dict | None = None) -> AsyncIterator[dict]:
    """Stream one completion. Tool call arguments arrive as deltas, so assemble them by index.

    extra_body carries the request fields the reasoning mapping resolved for this turn
    (reasoning_effort, chat_template_kwargs, ...). It is merged last so the mapping wins.
    """
    payload = {"model": model, "messages": messages, "stream": True}
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    if max_tokens:
        payload["max_tokens"] = max_tokens
    for key, value in (extra_body or {}).items():
        payload[key] = value

    calls: dict[int, dict] = {}
    splitter = ThinkSplitter()
    async with client() as c:
        async with c.stream("POST", "/chat/completions", json=payload) as r:
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
                        yield {"type": kind, "content": piece}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        yield {"type": "reasoning", "content": reasoning}
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        function = call.get("function") or {}
                        if function.get("name"):
                            slot["name"] = function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
                    if choice.get("finish_reason") == "tool_calls" and calls:
                        assembled = [calls[i] for i in sorted(calls)]
                        yield {"type": "tool_calls", "tool_calls": assembled}
                if chunk.get("usage"):
                    yield {"type": "usage", "usage": chunk["usage"]}
    for kind, piece in splitter.flush():
        yield {"type": kind, "content": piece}