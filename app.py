"""FastAPI app: chat with wiki tools, model list, wiki viewer API, dream trigger."""
import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import agent
import config
import dream
import dreamlog
import graph
import llm
import reasoning
import store
import wiki

app = FastAPI(title="dreamscape")
STOP: set[str] = set()

MEDIA = {"html": "text/html", "css": "text/css", "js": "text/javascript", "woff2": "font/woff2",
         "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon",
         "webmanifest": "application/manifest+json"}


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def system_prompt(system: str, inject_index: bool = True, effort_line: str = "") -> str:
    if inject_index:
        index = wiki.index_text()
    else:
        index = "(wiki index not injected for this conversation)"
    base = config.read_prompt("system.md", date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             index=index, system=system or "(none)")
    return f"{base}\n\n{effort_line}" if effort_line else base


@app.get("/")
def root():
    return HTMLResponse((config.STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/wiki")
def wiki_page():
    return HTMLResponse((config.STATIC / "wiki.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico")
def favicon():
    # Browsers ask for the icon at the root, so serve it there as well as under /static.
    file = config.STATIC / "favicon.ico"
    if not file.is_file():
        raise HTTPException(404)
    return Response(content=file.read_bytes(), media_type="image/x-icon")


@app.get("/site.webmanifest")
def manifest():
    file = config.STATIC / "site.webmanifest"
    if not file.is_file():
        raise HTTPException(404)
    return Response(content=file.read_bytes(), media_type="application/manifest+json")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    file = config.STATIC / "apple-touch-icon.png"
    if not file.is_file():
        raise HTTPException(404)
    return Response(content=file.read_bytes(), media_type="image/png")


@app.get("/static/{path:path}")
def static_file(path: str):
    file = (config.STATIC / path).resolve()
    if not file.is_file() or config.STATIC.resolve() not in file.parents:
        raise HTTPException(404)
    return Response(content=file.read_bytes(), media_type=MEDIA.get(file.suffix.lstrip("."), "text/plain"))


@app.get("/api/models")
async def models():
    try:
        rows = await llm.list_models()
    except Exception as error:
        raise HTTPException(502, f"cannot reach {config.BASE_URL}: {error}")
    return {"models": [row.get("id") for row in rows], "default": config.MODEL}


@app.get("/api/conversations")
def conversations():
    config.ensure_dirs()
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations")
def create_conversation(body: dict):
    row = store.new_conversation(body.get("model") or config.MODEL, body.get("system") or "")
    return {"conversation": row}


@app.post("/api/conversations/{cid}/rename")
def rename_conversation(cid: str, body: dict):
    if not store.rename(cid, body.get("title", "")):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    if not store.delete(cid):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
def conversation_messages(cid: str):
    row = store.get(cid)
    if row is None:
        raise HTTPException(404, "no such conversation")
    return {"conversation": {**row, "title": store.title_for(row)}, "messages": store.messages_raw(cid)}


@app.post("/api/chat/stop")
def stop_generation(body: dict):
    STOP.add(body.get("conversation_id") or "")
    return {"ok": True}


async def chat_stream(cid: str, message: str, model: str, system: str, temperature: float,
                      inject_index: bool = True, regenerate: bool = False,
                      effort: str = "medium"):
    mapping = reasoning.resolve(model, effort)
    if not regenerate:
        store.append(cid, {"role": "user", "content": message})
    messages = [{"role": "system", "content": system_prompt(system, inject_index, mapping["system"])}] + store.messages(cid)

    content = ""
    reasoning_text = ""
    started = time.monotonic()

    def save(partial: bool = False) -> None:
        record = {"role": "assistant", "content": content or None}
        if reasoning_text:
            record["reasoning"] = reasoning_text
            record["reasoning_ms"] = int((time.monotonic() - started) * 1000)
        # The level used for this turn, kept for later analysis. Not sent back to the model.
        record["reasoning_effort"] = mapping["level"]
        if partial:
            record["stopped"] = True
        if content or reasoning_text:
            store.append(cid, record)

    try:
        async for event in agent.run_loop(messages, model, temperature, mapping=mapping):
            if cid in STOP:
                save(partial=True)
                yield sse({"type": "stopped"})
                break
            if event["type"] in ("delta", "reasoning"):
                yield sse(event)
            elif event["type"] == "assistant_message":
                content = event["content"]
                reasoning_text = event["reasoning"]
                save()
                yield sse({"type": "assistant_message", "content": content, "reasoning": reasoning_text,
                           "reasoning_ms": event["reasoning_ms"], "reasoning_only": event["reasoning_only"],
                           "reasoning_effort": mapping["level"], "tool_calls": event["tool_calls"]})
            elif event["type"] == "tool":
                store.append(cid, {"role": "tool", "tool_call_id": event.get("id"), "name": event["name"],
                                   "arguments": event["arguments"],
                                   "content": json.dumps(event["result"], ensure_ascii=False)})
                yield sse(event)
            elif event["type"] == "limit":
                yield sse(event)
    except asyncio.CancelledError:
        save(partial=True)
        yield sse({"type": "stopped"})
    finally:
        STOP.discard(cid)


@app.post("/api/chat")
def chat(body: dict):
    cid = body.get("conversation_id")
    if not store.get(cid):
        raise HTTPException(404, "no such conversation")
    return StreamingResponse(
        chat_stream(cid, body.get("message", ""), body.get("model") or config.MODEL,
                    body.get("system") or "", float(body.get("temperature", 0.7)),
                    bool(body.get("wiki_index", True)), bool(body.get("regenerate")),
                    body.get("reasoning_effort") or reasoning.settings().get("default_level", "medium")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/wiki/list")
def wiki_list():
    config.ensure_dirs()
    return {"pages": wiki.list_pages(), "index": wiki.index_text(), "checks": wiki.checks()}


@app.get("/api/wiki/page")
def wiki_get(name: str):
    slug = wiki.slugify(name)
    content = wiki.read_page(slug or "")
    if content is None:
        raise HTTPException(404, "no such page")
    return {"name": slug, "content": content, "backlinks": wiki.backlinks(slug)}


@app.post("/api/wiki/page")
def wiki_put(body: dict):
    result = agent.execute_tool("wiki_write", {"name": body.get("name", ""), "content": body.get("content", "")})
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/wiki/search")
def wiki_search(q: str):
    return {"query": q, "results": wiki.search(q)}


@app.get("/api/wiki/graph")
def wiki_graph():
    config.ensure_dirs()
    return graph.build()


@app.get("/api/reasoning")
def reasoning_map(model: str = ""):
    """What the effort levels resolve to for a model, so the UI can say when a level is only
    approximated by wording."""
    target = model or config.MODEL
    mapping = reasoning.settings()
    levels = []
    for level in reasoning.LEVELS:
        resolved = reasoning.resolve(target, level)
        levels.append({"level": level, "label": resolved["label"], "description": resolved["description"],
                      "body": resolved["body"], "approximated": resolved["approximated"]})
    effective = any(level["body"] for level in levels)
    return {
        "model": target,
        "default": mapping.get("default_level", "medium"),
        "levels": levels,
        "endpoint_honours_controls": effective,
        "note": "" if effective else "Effort is approximated via prompt for this model",
    }


@app.get("/api/dreams")
def dreams():
    config.ensure_dirs()
    return {"runs": dreamlog.list_runs()}


@app.get("/api/dreams/live")
async def dreams_live():
    """Live progress of a run, used by the Dream log and the Dream now button."""
    return StreamingResponse(
        (sse(event) async for event in dreamlog.stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/dreams/{run_id}")
def dream_run(run_id: str):
    record = dreamlog.get_run(run_id)
    if record is None:
        raise HTTPException(404, "no such run")
    return record


@app.post("/api/dream")
async def dream_now(body: dict):
    try:
        return await dream.run(trigger=body.get("trigger") or "manual",
                              dry_run=bool(body.get("dry_run")), force=True,
                              include_reasoning=bool(body.get("include_reasoning")))
    except dream.Locked as error:
        raise HTTPException(409, str(error))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=config.HOST, port=config.PORT)
