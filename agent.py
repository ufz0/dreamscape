"""The wiki tools the model can call, and the tool loop shared by chat and dreaming."""
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import AsyncIterator

import connections
import config
import db
import llm
import reasoning
import uploads
import usage
import webfetch
import websearch
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

# The two tools the chat can use for current information. Dreaming keeps the wiki-only tools, so it
# is never fed pages it cannot verify.
WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns title, url, snippet, source domain, and the date when the provider gives one.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "what to search for"},
            "max_results": {"type": "integer", "description": "default 5, at most 10"},
            "recency": {"type": "string", "enum": ["day", "week", "month", "year"],
                        "description": "only when recent results are what matters"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Read one page as plain text. Truncated; refuses binary files, non-http urls and private addresses.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "the page to read"},
            "max_chars": {"type": "integer", "description": "default comes from .env"},
        }, "required": ["url"]},
    }},
]

WEB_TOOL_NAMES = {tool["function"]["name"] for tool in WEB_TOOLS}

# Offered only when a message actually attaches a file longer than the inline cap, so a turn that has
# nothing to page through does not get a tool it cannot use.
FILE_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": ("Read part of an attached file that is longer than what was shown in the message. "
                        "offset and length count characters into the cached text."),
        "parameters": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "the attachment id named in the <attachment> block"},
            "offset": {"type": "integer", "description": "character to start at, from 0"},
            "length": {"type": "integer", "description": "characters to read, default 4000"},
        }, "required": ["file_id"]}},
    }
]

FILE_TOOL_NAMES = {tool["function"]["name"] for tool in FILE_TOOLS}


def tools_for(web: bool = False, files: bool = False) -> list[dict]:
    """The tool list for one turn. The file tool is only there when a file needs paging."""
    tools = list(TOOLS)
    if web:
        tools += WEB_TOOLS
    if files:
        tools += FILE_TOOLS
    return tools


def system_prompt(system: str, inject_index: bool = True, effort_line: str = "", web: bool = False,
                  attachments: bool = False) -> str:
    """The system prompt for a turn. Lives here because the run engine builds turns too."""
    if inject_index:
        index = wiki.index_text()
    else:
        index = "(wiki index not injected for this conversation)"
    base = config.read_prompt("system.md", date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             index=index, system=system or "(none)")
    # Only when the web tools are actually offered to this conversation.
    if web:
        base = f"{base}\n\n{config.read_prompt('web_tools.md')}"
    if attachments:
        base = f"{base}\n\n{config.read_prompt('attachments.md')}"
    return f"{base}\n\n{effort_line}" if effort_line else base


async def run_tool(name: str, arguments: dict, user_id=None) -> dict:
    """One tool call. A provider failure is an error the model can react to, never a crashed turn."""
    if name == "web_search":
        return await websearch.search(user_id, arguments.get("query"), arguments.get("max_results", 5),
                                     arguments.get("recency"))
    if name == "web_fetch":
        return await webfetch.fetch(arguments.get("url"), arguments.get("max_chars"))
    if name == "read_file":
        with db.transaction() as conn:
            return uploads.read_text(conn, user_id, arguments.get("file_id"),
                                    arguments.get("offset", 0), arguments.get("length", 4000))
    return execute_tool(name, arguments)


def tool_message(name: str, result: dict) -> str:
    """What the model sees for a tool result. Web and file content is delimited, so it reads as data."""
    text = json.dumps(result, ensure_ascii=False)
    if name in WEB_TOOL_NAMES:
        return f"<web_content>\n{text}\n</web_content>"
    if name in FILE_TOOL_NAMES:
        return f"<file_content>\n{text}\n</file_content>"
    return text


def note_for(finish_reason: str | None, content: str, reasoning_text: str) -> str | None:
    """The wording for the one case where "no final answer" is real.

    Only the final step of a turn can be a missing answer. A step that thinks and then asks for a
    tool has no answer text by design, so it is not a failed answer; the loop continues with the
    tool result. Network errors and stops are handled by the caller, not here.
    """
    if content or not reasoning_text:
        return None
    if finish_reason == "length":
        return ("The response was cut off by the token limit during reasoning. "
                "Try a lower reasoning effort, or a higher max_tokens for that level in data/reasoning.json.")
    return "The model produced reasoning but no final answer."


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
                   mapping: dict | None = None, on_usage=None, limit_check=None,
                   tools: list[dict] | None = None, user_id=None, target: dict | None = None,
                   on_position=None, cancelled=None) -> AsyncIterator[dict]:
    """Call the model, run any tool calls, repeat until it answers or the limit is hit.

    `mapping` is the resolved reasoning-effort request for this turn (see reasoning.py). It is
    resolved once and used for every iteration of the loop, so tool calls in one turn do not
    change the effort mid-way.

    `on_usage(usage, estimated)` is called for every upstream call, which is what the usage table
    records: one row per iteration of the loop, not one per turn. `estimated` is true when the
    server did not report usage. `limit_check()` is called before each call; when it returns a
    limit, no call is made and a "limit_reached" event is yielded instead. A response that is
    already streaming is allowed to finish, so a user can overshoot by at most one response.

    Yields the upstream events plus four of its own:
      {"type": "assistant_message", "content", "reasoning", "reasoning_ms", "reasoning_only", "note", "tool_calls"}
      {"type": "tool", "id", "name", "arguments", "result"}
      {"type": "limit_reached", ...}
      {"type": "queued", "position": int}   only when the connection's queue was full

    `tools` is the list for this turn. Without it the wiki tools are used, which is what dreaming
    gets: `user_id` is only there for the web tools' rate limit.

    `on_position(position)` is called when this call has to wait behind other requests on the
    connection's queue (see connections.Gate), and `cancelled()` is checked while waiting so a
    cancelled run does not sit in the queue. A target with no connection is not queued.

    Thinking stays in its own field and is never sent back to the model in later turns.
    "reasoning_only" is only true for the final step of a turn, when the model produced thinking
    and no answer text; `note` carries the wording for that case.
    """
    if mapping is None:
        mapping = reasoning.resolve(model, reasoning.settings().get("default_level", "medium"))
    extra_body = mapping.get("body") or {}
    active = tools or TOOLS
    names = {tool["function"]["name"] for tool in active}
    web = bool(names & WEB_TOOL_NAMES)
    # Web research needs more rounds: search, read a page, read another. Still a hard cap.
    limit = min(config.MAX_TOOL_ITERATIONS_CAP,
                max(max_iterations, config.WEB_TOOL_ITERATIONS) if web else max_iterations)

    queue = connections.gate(target) if target and target.get("connection_id") else None
    resolved_target = target or llm.default_target()

    for iteration in range(limit):
        if cancelled and cancelled():
            return
        if limit_check:
            blocked = limit_check()
            if blocked:
                yield {"type": "limit_reached", **blocked}
                return

        content = ""
        reasoning_text = ""
        started = time.monotonic()
        calls: list[dict] = []
        reported = None
        finish_reason = None
        async with (queue.acquire(on_position, cancelled) if queue else nullcontext()):
            async for event in llm.stream_chat(resolved_target, model, messages, temperature,
                                               tools=active, extra_body=extra_body):
                if event["type"] == "usage":
                    reported = event.get("usage") or {}
                elif event["type"] == "finish":
                    finish_reason = event.get("finish_reason")
                elif event["type"] == "delta":
                    content += event["content"]
                elif event["type"] == "reasoning":
                    reasoning_text += event["content"]
                elif event["type"] == "tool_calls":
                    calls = event["tool_calls"]
                yield event

        if on_usage:
            if reported:
                on_usage(reported, False)
            else:
                on_usage(usage.estimate(messages, content + reasoning_text), True)

        if not calls:
            fallback = llm.parse_text_tool_call(content, names)
            if fallback:
                calls = [fallback]

        # The note belongs to the last step only. A turn that ended in a tool call is an
        # intermediate step: empty content there means "thinking, then a tool call", not a failure.
        note = note_for(finish_reason, content, reasoning_text) if not calls else None
        turn = {"content": content, "reasoning": reasoning_text,
                "reasoning_ms": int((time.monotonic() - started) * 1000),
                "reasoning_only": bool(note), "finish_reason": finish_reason, "note": note}

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
            result = await run_tool(call["name"], call["arguments"], user_id)
            yield {"type": "tool", "id": call["id"], "name": call["name"], "arguments": call["arguments"],
                   "result": result, "duration_ms": int((time.monotonic() - started_tool) * 1000)}
            # What the model sees for the result. Web content arrives delimited.
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": tool_message(call["name"], result)})

    yield {"type": "limit", "iterations": limit}


def _parse_args(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"value": value}
