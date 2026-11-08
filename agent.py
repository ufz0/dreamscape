"""The wiki tools the model can call, and the tool loop shared by chat and dreaming."""
import json
import time
from typing import AsyncIterator

import config
import llm
import reasoning
import wiki

TOOLS = [
    {"type": "function", "function": {
        "name": "wiki_list",
        "description": "List wiki page names with their one-line descriptions.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "wiki_read",
        "description": "Read a wiki page by slug.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "page slug, e.g. network-lab"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "wiki_search",
        "description": "Search wiki pages and processed transcripts. Returns file, line and snippet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "text to search for"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "wiki_write",
        "description": "Create or update a wiki page. mode is 'replace' or 'append'. Keeps index.md in sync.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "page slug (lowercase, no path, .md)"},
            "content": {"type": "string", "description": "markdown content"},
            "mode": {"type": "string", "enum": ["replace", "append"], "description": "default replace"},
        }, "required": ["name", "content"]},
    }},
]

TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


def execute_tool(name: str, arguments: dict) -> dict:
    if name == "wiki_list":
        return {"pages": wiki.list_pages()}
    if name == "wiki_read":
        content = wiki.read_page(wiki.slugify(arguments.get("name", "")) or "")
        if content is None:
            return {"error": f"no such page: {arguments.get('name')!r}"}
        return {"name": wiki.slugify(arguments["name"]), "content": content}
    if name == "wiki_search":
        return {"results": wiki.search(arguments.get("query", ""))}
    if name == "wiki_write":
        return wiki.write_page(arguments.get("name", ""), arguments.get("content", ""), arguments.get("mode", "replace"))
    return {"error": f"unknown tool: {name}"}


async def run_loop(messages: list[dict], model: str, temperature: float,
                   max_iterations: int = config.MAX_TOOL_ITERATIONS,
                   mapping: dict | None = None) -> AsyncIterator[dict]:
    """Call the model, run any tool calls, repeat until it answers or the limit is hit.

    `mapping` is the resolved reasoning-effort request for this turn (see reasoning.py). It is
    resolved once and used for every iteration of the loop, so tool calls in one turn do not
    change the effort mid-way.

    Yields the upstream events plus two of its own:
      {"type": "assistant_message", "content", "reasoning", "reasoning_ms", "reasoning_only", "tool_calls"}
      {"type": "tool", "id", "name", "arguments", "result"}

    Thinking stays in its own field and is never sent back to the model in later turns.
    "reasoning_only" is the case where the model put everything inside <think> and stopped.
    """
    if mapping is None:
        mapping = reasoning.resolve(model, reasoning.settings().get("default_level", "medium"))
    extra_body = mapping.get("body") or {}

    for iteration in range(max_iterations):
        content = ""
        reasoning_text = ""
        started = time.monotonic()
        calls: list[dict] = []
        async for event in llm.stream_chat(messages, model, temperature, tools=TOOLS, extra_body=extra_body):
            if event["type"] == "delta":
                content += event["content"]
            elif event["type"] == "reasoning":
                reasoning_text += event["content"]
            elif event["type"] == "tool_calls":
                calls = event["tool_calls"]
            yield event

        if not calls:
            fallback = llm.parse_text_tool_call(content, TOOL_NAMES)
            if fallback:
                calls = [fallback]

        turn = {"content": content, "reasoning": reasoning_text,
                "reasoning_ms": int((time.monotonic() - started) * 1000),
                "reasoning_only": bool(reasoning_text) and not content}

        if not calls:
            yield {"type": "assistant_message", **turn, "tool_calls": []}
            return

        normalized = [{"id": call["id"] or f"call_{iteration}", "name": call["name"],
                       "arguments": call["arguments"] if isinstance(call["arguments"], dict)
                       else _parse_args(call["arguments"])} for call in calls]
        yield {"type": "assistant_message", **turn, "tool_calls": normalized}
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                       for c in normalized]})

        for call in normalized:
            started_tool = time.monotonic()
            result = execute_tool(call["name"], call["arguments"])
            yield {"type": "tool", "id": call["id"], "name": call["name"], "arguments": call["arguments"],
                   "result": result, "duration_ms": int((time.monotonic() - started_tool) * 1000)}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})

    yield {"type": "limit", "iterations": max_iterations}


def _parse_args(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"value": value}
