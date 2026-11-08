"""FastAPI app: chat with wiki tools, model list, wiki viewer API, dream trigger, accounts and admin.

Every route below is behind a session: API calls without one get 401, pages redirect to /login.
State-changing requests also need the CSRF header. The current user is set for the call, so the
data modules work inside that user's folder. `/healthz` is the one route without a session, so a
container health check can reach it.
"""
import asyncio
import html
import json
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

import accounts
import agent
import appdata
import auth
import config
import db
import dream
import dreamlog
import graph
import limits
import llm
import reasoning
import store
import titles
import usage as usagelog
import websearch
import wiki

app = FastAPI(title="dreamscape")
STOP: set[str] = set()

MEDIA = {"html": "text/html", "css": "text/css", "js": "text/javascript", "woff2": "font/woff2",
         "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon",
         "webmanifest": "application/manifest+json"}


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.middleware("http")
async def csrf_check(request: Request, call_next):
    """Double-submit check: the CSRF cookie must be echoed back as X-CSRF-Token.

    A cross-site form post cannot send that header, and a cross-site script cannot read our
    cookie, so a state-changing request without both is not one the page itself made.
    """
    if not auth.csrf_ok(request):
        return JSONResponse({"detail": "CSRF check failed. Reload the page and try again."}, status_code=403)
    return await call_next(request)


def system_prompt(system: str, inject_index: bool = True, effort_line: str = "", web: bool = False) -> str:
    if inject_index:
        index = wiki.index_text()
    else:
        index = "(wiki index not injected for this conversation)"
    base = config.read_prompt("system.md", date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             index=index, system=system or "(none)")
    # Only when the web tools are actually offered to this conversation.
    if web:
        base = f"{base}\n\n{config.read_prompt('web_tools.md')}"
    return f"{base}\n\n{effort_line}" if effort_line else base


# ---------- pages ----------

def page(file: str, request: Request, values: dict | None = None) -> HTMLResponse:
    text = (config.STATIC / file).read_text(encoding="utf-8")
    for key, value in (values or {}).items():
        text = text.replace("{{" + key + "}}", str(value))
    response = HTMLResponse(text)
    auth.set_csrf_cookie(response, request)
    return response


@app.get("/")
def root(request: Request):
    user, redirect = auth.page_gate(request)
    if redirect:
        return redirect
    return page("index.html", request)


@app.get("/wiki")
def wiki_page(request: Request):
    user, redirect = auth.page_gate(request)
    if redirect:
        return redirect
    return page("wiki.html", request)


@app.get("/admin")
def admin_page(request: Request):
    user, redirect = auth.page_gate(request)
    if redirect:
        return redirect
    if user["role"] != "admin":
        return JSONResponse({"detail": "Admins only."}, status_code=403)
    return page("admin.html", request)


AUTH_MODES = {
    "login": ("Log in", "Log in", ""),
    "setup": ("Set up the account", "Create the first account",
             "No accounts yet. The first one created here becomes the admin."),
    "register": ("Create an account", "Create account", "Self-registration is on, so a new account is a normal user."),
}


@app.get("/login")
def login_page(request: Request):
    return auth_page("login", request)


@app.get("/setup")
def setup_page(request: Request):
    with db.transaction() as conn:
        existing = int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
    if existing:
        raise HTTPException(404, "Setup is finished. /setup only runs while no accounts exist.")
    return auth_page("setup", request)


@app.get("/register")
def register_page(request: Request):
    with db.transaction() as conn:
        if not accounts.settings(conn)["allow_self_registration"]:
            raise HTTPException(404, "Self-registration is off. An admin creates accounts.")
    return auth_page("register", request)


def auth_page(mode: str, request: Request) -> HTMLResponse:
    heading, button, note = AUTH_MODES[mode]
    with db.transaction() as conn:
        self_registration = accounts.settings(conn)["allow_self_registration"]
    # The values go into HTML attributes, so they are escaped; json.dumps here would put quotes
    # inside a quoted attribute and the page would read them as part of the value.
    return page("auth.html", request, {
        "mode": mode, "heading": heading, "button": button, "note": note,
        "next": html.escape(request.query_params.get("next") or "/", quote=True),
        "register": "1" if self_registration else "0",
    })


@app.get("/healthz")
def healthz():
    """Unauthenticated, for the container health check. Says whether the app can read its own data."""
    try:
        with db.transaction() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as error:
        return JSONResponse(status_code=503, content={"status": "unavailable", "reason": str(error)})
    return {"status": "ok"}


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


# ---------- auth ----------

@app.get("/api/me")
def me(request: Request):
    user = auth.require(request)
    with db.transaction() as conn:
        return {"user": user, "csrf": request.cookies.get(auth.CSRF_COOKIE),
                "usage": limits.usage_state(conn, user)}


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    username = accounts.normalize_username(body.get("username"))
    error = accounts.validate_username(username)
    if error:
        raise HTTPException(400, error)
    with db.transaction() as conn:
        blocked = accounts.lockout(conn, username, client_ip(request))
        if blocked:
            raise HTTPException(429, blocked["message"])
        user = accounts.get_by_username(conn, username)
        if user is None or not accounts.verify_password(
                conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()["password_hash"],
                str(body.get("password") or "")):
            accounts.record_login(conn, username, client_ip(request), False)
            raise HTTPException(401, "Username or password is not correct.")
        if user["status"] != "active":
            accounts.record_login(conn, username, client_ip(request), False)
            raise HTTPException(403, "That account is disabled.")
        accounts.record_login(conn, username, client_ip(request), True)
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (db.now(), user["id"]))
        token = accounts.create_session(conn, user)

    appdata.set_current(user["id"])
    response = JSONResponse({"user": user})
    auth.set_session_cookie(response, token, request)
    auth.set_csrf_cookie(response, request)
    return response


@app.post("/api/auth/setup")
async def setup(request: Request):
    body = await request.json()
    with db.transaction() as conn:
        if int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]):
            raise HTTPException(404, "Setup is finished. /setup only runs while no accounts exist.")
        user = accounts.create_user(conn, str(body.get("username") or ""), str(body.get("password") or ""),
                                   role="admin", actor="first-run")
        token = accounts.create_session(conn, user)
    appdata.set_current(user["id"])
    response = JSONResponse({"user": user, "note": f"First account created: {user['username']}. It is the admin."})
    auth.set_session_cookie(response, token, request)
    auth.set_csrf_cookie(response, request)
    return response


@app.post("/api/auth/register")
async def register(request: Request):
    body = await request.json()
    with db.transaction() as conn:
        if not accounts.settings(conn)["allow_self_registration"]:
            raise HTTPException(403, "Self-registration is off. An admin creates accounts.")
        user = accounts.create_user(conn, str(body.get("username") or ""), str(body.get("password") or ""),
                                   role="user", actor="self")
        token = accounts.create_session(conn, user)
    appdata.set_current(user["id"])
    response = JSONResponse({"user": user})
    auth.set_session_cookie(response, token, request)
    auth.set_csrf_cookie(response, request)
    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        with db.transaction() as conn:
            accounts.delete_session(conn, accounts.hash_token(token))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.post("/api/auth/change-password")
async def change_password(request: Request):
    user = auth.require(request)
    body = await request.json()
    token = request.cookies.get(auth.SESSION_COOKIE)
    with db.transaction() as conn:
        accounts.change_password(conn, user, user["id"], str(body.get("current") or ""),
                                str(body.get("new") or ""))
        # Keep this request's own session alive, invalidate the user's others.
        if token:
            new_token = accounts.create_session(conn, user)
        else:
            new_token = None
    response = JSONResponse({"ok": True, "note": "Password changed."})
    if new_token:
        auth.set_session_cookie(response, new_token, request)
    return response


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "")


# ---------- accounts and usage for the current user ----------

@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    user = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        created = accounts.create_user(conn, str(body.get("username") or ""), str(body.get("password") or ""),
                                      role=str(body.get("role") or "user"), must_change=True,
                                      daily_limit=limits.parse_limit(body.get("daily_limit")),
                                      weekly_limit=limits.parse_limit(body.get("weekly_limit")),
                                      actor=user["username"])
    return {"user": created, "note": f"Created {created['username']}. The user must change that password at first login."}


@app.get("/api/admin/users")
def admin_users(request: Request):
    user = auth.require_admin(request)
    with db.transaction() as conn:
        return {"users": accounts.list_users(conn), "settings": accounts.settings(conn),
                "audit": accounts.audit_log(conn, 50)}


@app.post("/api/admin/users/{user_id}/limits")
async def admin_limits(user_id: int, request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            updated = accounts.set_limits(conn, admin, user_id, body.get("daily_limit"), body.get("weekly_limit"))
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"user": updated}


@app.post("/api/admin/users/{user_id}/role")
async def admin_role(user_id: int, request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            updated = accounts.set_role(conn, admin, user_id, str(body.get("role") or ""))
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"user": updated}


@app.post("/api/admin/users/{user_id}/status")
async def admin_status(user_id: int, request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            updated = accounts.set_status(conn, admin, user_id, str(body.get("status") or ""))
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"user": updated}


@app.post("/api/admin/users/{user_id}/password")
async def admin_password(user_id: int, request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            password = accounts.reset_password(conn, admin, user_id, str(body.get("password") or ""))
            target = accounts.get_user(conn, user_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"note": f"Password for {target['username']} changed. Show it once: {password}"}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        target = accounts.get_user(conn, user_id)
        if target is None:
            raise HTTPException(404, "No such user.")
        if str(body.get("confirm") or "").strip().lower() != target["username"]:
            raise HTTPException(400, "Type the username to confirm.")
        try:
            result = accounts.delete_user(conn, admin, user_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"ok": True, "note": f"Deleted {result['username']}. Their data folder is removed; usage rows are kept without a user."}


@app.get("/api/admin/settings")
def admin_settings(request: Request):
    auth.require_admin(request)
    with db.transaction() as conn:
        return {"settings": accounts.settings(conn)}


@app.post("/api/admin/settings")
async def admin_set_setting(request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        updated = accounts.set_setting(conn, admin, str(body.get("key") or ""), bool(body.get("value")))
    return {"settings": updated}


@app.get("/api/admin/audit")
def admin_audit(request: Request):
    auth.require_admin(request)
    with db.transaction() as conn:
        return {"audit": accounts.audit_log(conn, 50)}


@app.get("/api/admin/usage")
def admin_usage(request: Request, period: str = "month", date: str = ""):
    """Usage numbers for admins. Only numbers: an admin cannot read another user's chat content."""
    admin = auth.require_admin(request)
    if period not in ("day", "month", "year", "all"):
        raise HTTPException(400, "Period must be day, month, year or all.")
    target = None
    if date:
        try:
            target = datetime.fromisoformat(date).date()
        except ValueError:
            raise HTTPException(400, f"Date must be YYYY-MM-DD, not {date!r}.")
    with db.transaction() as conn:
        report = usagelog.aggregate(conn, period, target)
        report["admins_cannot_read_content"] = "Admins can see usage numbers, not chat content."
        return report


@app.get("/api/usage/me")
def my_usage(request: Request):
    """The signed-in user's own numbers, for the settings dialog and the account popup."""
    user = auth.require(request)
    with db.transaction() as conn:
        state = limits.usage_state(conn, user)
        return {**state, "totals": usagelog.aggregate(conn, "all", None, user_id=user["id"])["totals"]}


# ---------- conversations ----------

@app.get("/api/models")
async def models(request: Request):
    auth.require(request)
    try:
        rows = await llm.list_models()
    except Exception as error:
        raise HTTPException(502, f"cannot reach {config.BASE_URL}: {error}")
    return {"models": [row.get("id") for row in rows], "default": config.MODEL}


@app.get("/api/web")
def web_status(request: Request):
    """Which search provider is active. The tools popup and the Settings page read this."""
    auth.require(request)
    return websearch.provider_status()


@app.get("/api/conversations")
def conversations(request: Request):
    auth.require(request)
    appdata.current().ensure()
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations")
async def create_conversation(request: Request):
    user = auth.require(request)
    body = await request.json()
    row = store.new_conversation(body.get("model") or config.MODEL, body.get("system") or "")
    return {"conversation": row | {"user": user["username"]}}


@app.post("/api/conversations/{cid}/rename")
async def rename_conversation(cid: str, request: Request):
    auth.require(request)
    body = await request.json()
    if not store.rename(cid, body.get("title", "")):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


@app.post("/api/conversations/{cid}/title")
async def title_conversation(cid: str, request: Request):
    """Name a chat from its first exchange. A name someone typed by hand is left alone."""
    user = auth.require(request)
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    row = store.get(cid)
    if row is None:
        raise HTTPException(404, "no such conversation")
    return await titles.generate(user, cid, body.get("model") or row.get("model") or config.MODEL)


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, request: Request):
    auth.require(request)
    if not store.delete(cid):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
def conversation_messages(cid: str, request: Request):
    auth.require(request)
    row = store.get(cid)
    if row is None:
        raise HTTPException(404, "no such conversation")
    return {"conversation": {**row, "title": store.title_for(row)}, "messages": store.messages_raw(cid)}


@app.post("/api/chat/stop")
async def stop_generation(request: Request):
    auth.require(request)
    body = await request.json()
    STOP.add(body.get("conversation_id") or "")
    return {"ok": True}


async def chat_stream(user: dict, cid: str, message: str, model: str, system: str, temperature: float,
                     inject_index: bool = True, regenerate: bool = False,
                     effort: str = "medium", web: bool = False):
    mapping = reasoning.resolve(model, effort)
    tools = agent.TOOLS + agent.WEB_TOOLS if web else agent.TOOLS

    def record(block: dict, estimated: bool) -> None:
        with db.transaction() as conn:
            usagelog.record(conn, user["id"], block, "chat", model, cid, estimated)

    def blocked():
        with db.transaction() as conn:
            return limits.check(conn, user)

    if not regenerate:
        store.append(cid, {"role": "user", "content": message})
    messages = [{"role": "system", "content": system_prompt(system, inject_index, mapping["system"], web)}] + store.messages(cid)

    content = ""
    reasoning_text = ""
    started = time.monotonic()

    def save(partial: bool = False, tool_calls: list | None = None, note: str | None = None) -> None:
        record_entry = {"role": "assistant", "content": content or None}
        if reasoning_text:
            record_entry["reasoning"] = reasoning_text
            record_entry["reasoning_ms"] = int((time.monotonic() - started) * 1000)
        # The level used for this turn, kept for later analysis. Not sent back to the model.
        record_entry["reasoning_effort"] = mapping["level"]
        # The tool calls of this step, so the transcript shows what the model actually asked for.
        if tool_calls:
            record_entry["tool_calls"] = tool_calls
        # Only the note for a missing answer, and only on the final step. An intermediate step with
        # thinking and a tool call has no answer text by design, so nothing is written for it.
        if note:
            record_entry["note"] = note
        if partial:
            record_entry["stopped"] = True
        if content or reasoning_text or tool_calls:
            store.append(cid, record_entry)

    try:
        async for event in agent.run_loop(messages, model, temperature, mapping=mapping,
                                         on_usage=record, limit_check=blocked,
                                         tools=tools, user_id=user["id"]):
            if cid in STOP:
                save(partial=True)
                yield sse({"type": "stopped"})
                break
            if event["type"] in ("delta", "reasoning", "usage", "limit_reached"):
                yield sse(event)
            elif event["type"] == "assistant_message":
                content = event["content"]
                reasoning_text = event["reasoning"]
                save(tool_calls=event["tool_calls"], note=event.get("note"))
                yield sse({"type": "assistant_message", "content": content, "reasoning": reasoning_text,
                           "reasoning_ms": event["reasoning_ms"], "reasoning_only": event["reasoning_only"],
                           "note": event.get("note"), "reasoning_effort": mapping["level"],
                           "tool_calls": event["tool_calls"]})
            elif event["type"] == "tool":
                store.append(cid, {"role": "tool", "tool_call_id": event.get("id"), "name": event["name"],
                                   "arguments": event["arguments"],
                                   "content": json.dumps(event["result"], ensure_ascii=False)})
                yield sse(event)
    except asyncio.CancelledError:
        save(partial=True)
        yield sse({"type": "stopped"})
    finally:
        STOP.discard(cid)


@app.post("/api/chat")
async def chat(request: Request):
    user = auth.require(request)
    body = await request.json()
    cid = body.get("conversation_id")
    if not store.get(cid):
        raise HTTPException(404, "no such conversation")

    # Check before the turn starts. A response that is already streaming may finish, so the
    # overshoot is at most one response; the usage for it is recorded afterwards.
    with db.transaction() as conn:
        blocked = limits.check(conn, user)
    if blocked:
        return JSONResponse(blocked, status_code=429)

    return StreamingResponse(
        chat_stream(user, cid, body.get("message", ""), body.get("model") or config.MODEL,
                    body.get("system") or "", float(body.get("temperature", 0.7)),
                    bool(body.get("wiki_index", True)), bool(body.get("regenerate")),
                    body.get("reasoning_effort") or reasoning.settings().get("default_level", "medium"),
                    bool(body.get("web_search"))),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- wiki ----------

@app.get("/api/wiki/list")
def wiki_list(request: Request):
    auth.require(request)
    appdata.current().ensure()
    return {"pages": wiki.list_pages(), "index": wiki.index_text(), "checks": wiki.checks()}


@app.get("/api/wiki/page")
def wiki_get(name: str, request: Request):
    auth.require(request)
    slug = wiki.slugify(name)
    content = wiki.read_page(slug or "")
    if content is None:
        raise HTTPException(404, "no such page")
    return {"name": slug, "content": content, "backlinks": wiki.backlinks(slug)}


@app.post("/api/wiki/page")
async def wiki_put(request: Request):
    auth.require(request)
    body = await request.json()
    result = agent.execute_tool("wiki_write", {"name": body.get("name", ""), "content": body.get("content", "")})
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/wiki/search")
def wiki_search(q: str, request: Request):
    auth.require(request)
    return {"query": q, "results": wiki.search(q)}


@app.get("/api/wiki/graph")
def wiki_graph(request: Request):
    auth.require(request)
    appdata.current().ensure()
    return graph.build()


@app.get("/api/reasoning")
def reasoning_map(request: Request, model: str = ""):
    """What the effort levels resolve to for a model, so the UI can say when a level is only
    approximated by wording."""
    auth.require(request)
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


# ---------- dream log ----------

@app.get("/api/dreams")
def dreams(request: Request):
    auth.require(request)
    appdata.current().ensure()
    return {"runs": dreamlog.list_runs()}


@app.get("/api/dreams/live")
async def dreams_live(request: Request):
    """Live progress of a run, used by the Dream log and the Dream now button."""
    user = auth.require(request)
    return StreamingResponse(
        (sse(event) async for event in dreamlog.stream(str(user["id"]))),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/dreams/{run_id}")
def dream_run(run_id: str, request: Request):
    auth.require(request)
    record = dreamlog.get_run(run_id)
    if record is None:
        raise HTTPException(404, "no such run")
    return record


@app.post("/api/dream")
async def dream_now(request: Request):
    """Dreams for the current user only."""
    user = auth.require(request)
    body = await request.json()
    try:
        return await dream.run(trigger=body.get("trigger") or "manual",
                              dry_run=bool(body.get("dry_run")), force=True,
                              include_reasoning=bool(body.get("include_reasoning")), user=user)
    except dream.Locked as error:
        raise HTTPException(409, str(error))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=config.HOST, port=config.PORT)
