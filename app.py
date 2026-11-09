"""FastAPI app: chat with wiki tools, model list, wiki viewer API, dream trigger, accounts and admin.

Every route below is behind a session: API calls without one get 401, pages redirect to /login.
State-changing requests also need the CSRF header. The current user is set for the call, so the
data modules work inside that user's folder. The routes without a session are /healthz, /login,
/setup while no accounts exist, /register when self-registration is on, and the static assets.

One exception: /api/v1 is the OpenAI-compatible API and is entered with a Bearer key, not a cookie, so
a cookie is not accepted there and a key is not accepted on the UI routes. See openai_api.py.

The automatic /docs, /redoc and /openapi.json are turned off, because that would put the route map
on a public address. /healthz stays open and says only whether the app can read its own data.
"""
import html
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

import accounts
import agent
import apikeys
import appdata
import auth
import config
import connections
import db
import dream
import dreamlog
import graph
import limits
import migrations
import openai_api
import reasoning
import runs
import settings
import store
import titles
import uploads
import usage as usagelog
import websearch
import wiki

# No /docs, /redoc or /openapi.json: the app is reachable from the network, and a route map is not
# something a health check or a login page needs.
app = FastAPI(title="dreamscape", docs_url=None, redoc_url=None, openapi_url=None)

# /api/v1 is the OpenAI-compatible API: a Bearer key, not a session cookie, and errors in the OpenAI
# shape. It is a router of its own so its error format does not mix with the UI routes, which answer
# {"detail": ...}.
app.include_router(openai_api.router)
app.exception_handler(openai_api.ApiError)(openai_api.handle)

MEDIA = {"html": "text/html", "css": "text/css", "js": "text/javascript", "woff2": "font/woff2",
         "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon",
         "webmanifest": "application/manifest+json"}


def sse(payload: dict, event_id: int | None = None) -> str:
    """One SSE frame. The id is the event's position in the run, so a client can pick up after it."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """What happens at start and at stop.

    Start: the pending migrations (with the backup), then the runs this process did not finish.
    Stop: no new runs are accepted, and active ones get 60 seconds to write what they have.
    """
    with db.transaction() as conn:
        result = migrations.apply(conn, log=lambda message: print(message, flush=True))
        if result["backup"]:
            print(f"migrations: data/app.db copied to {result['backup']} before applying "
                  f"{len(result['applied'])} file(s)", flush=True)
    runs.mark_interrupted_at_startup()

    warning = auth.startup_warning()
    if warning:
        print(f"startup: {warning}", flush=True)

    yield
    finished = runs.shutdown(grace=60)
    if finished:
        print(f"shutdown: {finished} run(s) finished within the grace period", flush=True)


app.router.lifespan_context = lifespan


@app.middleware("http")
async def csrf_check(request: Request, call_next):
    """Double-submit check: the CSRF cookie must be echoed back as X-CSRF-Token.

    A cross-site form post cannot send that header, and a cross-site script cannot read our
    cookie, so a state-changing request without both is not one the page itself made.
    """
    if not auth.csrf_ok(request):
        return JSONResponse({"detail": "CSRF check failed. Reload the page and try again."}, status_code=403)
    return await call_next(request)


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
    """The address the login rate limiting records. auth.py owns when a forwarded header is believed."""
    return auth.client_ip(request)


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


@app.post("/api/admin/users/{user_id}/api_access")
async def admin_api_access(user_id: int, request: Request):
    """One account's API access. Off means its keys stop working on the next call."""
    admin = auth.require_admin(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            updated = accounts.set_api_access(conn, admin, user_id, bool(body.get("api_access")))
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"user": updated}


@app.get("/api/admin/keys")
def admin_keys(request: Request):
    """Every key, so the Users tab can revoke one it cannot see."""
    auth.require_admin(request)
    with db.transaction() as conn:
        return {"keys": apikeys.list_all(conn), "allow_api": settings.flags(conn)["allow_api"]}


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
    """Everything an admin can change at runtime: the flags, the global limits, the defaults, the
    run limits. All of it lives in the settings table, not in .env."""
    auth.require_admin(request)
    with db.transaction() as conn:
        return {"settings": settings.values(conn)}


@app.post("/api/admin/settings")
async def admin_set_setting(request: Request):
    admin = auth.require_admin(request)
    body = await request.json()
    key = str(body.get("key") or "")
    value = body.get("value")
    with db.transaction() as conn:
        try:
            updated = settings.set_value(conn, admin, key, bool(value) if key in settings.FLAGS else value)
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"settings": updated}


@app.get("/api/admin/audit")
def admin_audit(request: Request):
    auth.require_admin(request)
    with db.transaction() as conn:
        return {"audit": accounts.audit_log(conn, 50)}


@app.get("/api/admin/usage")
def admin_usage(request: Request, period: str = "month", date: str = "", source: str = ""):
    """Usage numbers for admins. Only numbers: an admin cannot read another user's chat content.

    `source` narrows the numbers to one kind of call (chat, dreaming, api); empty is everything. The
    per-key breakdown is part of the report whenever a key was used in the period.
    """
    admin = auth.require_admin(request)
    if period not in ("day", "month", "year", "all"):
        raise HTTPException(400, "Period must be day, month, year or all.")
    if source and source not in limits.SOURCES:
        raise HTTPException(400, f"Source must be one of: {', '.join(limits.SOURCES)}.")
    target = None
    if date:
        try:
            target = datetime.fromisoformat(date).date()
        except ValueError:
            raise HTTPException(400, f"Date must be YYYY-MM-DD, not {date!r}.")
    with db.transaction() as conn:
        report = usagelog.aggregate(conn, period, target, source=source or None)
        report["source"] = source or ""
        report["sources"] = list(limits.SOURCES)
        report["admins_cannot_read_content"] = "Admins can see usage numbers, not chat content."
        return report


@app.get("/api/usage/me")
def my_usage(request: Request):
    """The signed-in user's own numbers, for the settings dialog and the account popup."""
    user = auth.require(request)
    with db.transaction() as conn:
        state = limits.usage_state(conn, user)
        return {**state, "totals": usagelog.aggregate(conn, "all", None, user_id=user["id"])["totals"]}


# ---------- connections (admins only) ----------

def actor(request: Request) -> dict:
    """The audit column wants a username, so the admin object is what acts here."""
    return auth.require_admin(request)


@app.get("/api/connections")
def connection_list(request: Request):
    admin = actor(request)
    with db.transaction() as conn:
        rows = connections.list_connections(conn)
    for row in rows:
        row["queue"] = connections.gate(row).state()
    return {"connections": rows, "presets": {key: value["label"] for key, value in connections.PRESETS.items()}}


@app.post("/api/connections")
async def connection_create(request: Request):
    admin = actor(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            return connections.create(conn, admin, body)
        except ValueError as error:
            raise HTTPException(400, str(error))


@app.post("/api/connections/{connection_id}")
async def connection_update(connection_id: int, request: Request):
    admin = actor(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            return connections.update(conn, admin, connection_id, body)
        except ValueError as error:
            raise HTTPException(400, str(error))


@app.delete("/api/connections/{connection_id}")
async def connection_delete(connection_id: int, request: Request):
    admin = actor(request)
    with db.transaction() as conn:
        try:
            result = connections.delete(conn, admin, connection_id)
        except ValueError as error:
            raise HTTPException(409, str(error))
    connections.GATES.pop(connection_id, None)
    return {"ok": True, "note": f"Removed {result['name']} and {result['models_removed']} model rows."}


@app.post("/api/connections/{connection_id}/test")
async def connection_test(connection_id: int, request: Request):
    """Run the checks one at a time and keep the result, so the row can show status badges."""
    admin = actor(request)
    body = await request.json() if (raw := await request.body()) else {}
    with db.transaction() as conn:
        try:
            return await connections.test_connection(conn, connection_id, str(body.get("model") or ""))
        except ValueError as error:
            raise HTTPException(400, str(error))
        except Exception as error:
            raise HTTPException(502, f"Could not reach the connection: {error}")


@app.post("/api/connections/{connection_id}/models")
async def connection_add_model(connection_id: int, request: Request):
    """Add a model by hand, for a server that does not list its models."""
    admin = actor(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            return connections.add_model(conn, admin, connection_id, str(body.get("model_id") or ""), body)
        except ValueError as error:
            raise HTTPException(400, str(error))


@app.post("/api/connections/{connection_id}/models/refresh")
async def connection_refresh_models(connection_id: int, request: Request):
    admin = actor(request)
    with db.transaction() as conn:
        try:
            return await connections.refresh_models(conn, admin, connection_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
        except Exception as error:
            raise HTTPException(502, f"Could not reach the connection: {error}")


@app.post("/api/connections/models/{model_row_id}")
async def connection_update_model(model_row_id: int, request: Request):
    """Per model: display name, hidden, the capability flags, context length, its own overrides."""
    admin = actor(request)
    body = await request.json()
    with db.transaction() as conn:
        try:
            return connections.update_model(conn, admin, model_row_id, body)
        except ValueError as error:
            raise HTTPException(400, str(error))


@app.delete("/api/connections/models/{model_row_id}")
async def connection_delete_model(model_row_id: int, request: Request):
    admin = actor(request)
    with db.transaction() as conn:
        try:
            result = connections.delete_model(conn, admin, model_row_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"ok": True, "note": f"Removed {result['model']}."}


# ---------- conversations ----------

@app.get("/api/models")
def model_list(request: Request):
    """The models this user may use, grouped by connection. Read from the stored list, not live.

    A connection that is admins-only, or a model marked hidden, is not shown to a normal user.
    """
    user = auth.require(request)
    with db.transaction() as conn:
        groups = connections.visible(conn, user)
        return {"models": [model["namespaced"] for group in groups for model in group["models"]],
                "groups": groups, "default": connections.default_model(conn),
                "default_dream_model": connections.default_model(conn, "dream")}


@app.get("/api/web")
def web_status(request: Request):
    """Which search provider is active. The tools popup and the Settings page read this."""
    auth.require(request)
    return websearch.provider_status()


@app.get("/api/conversations")
def conversations(request: Request):
    """The chat list. Each row also says whether a run is active in it and whether the result of a
    finished run has not been seen yet, which is what the sidebar shows."""
    user = auth.require(request)
    appdata.current().ensure()
    with db.transaction() as conn:
        active = {entry["conversation_id"]: entry["id"] for entry in conn.execute(
            f"SELECT conversation_id, id FROM runs WHERE user_id = ? AND status IN {runs.ACTIVE_TUPLE}",
            (user["id"],)).fetchall()}
        unread = set(runs.unread_for(conn, user["id"]))
    rows = store.list_conversations()
    for row in rows:
        row["active_run_id"] = active.get(row["id"])
        row["unread"] = row["id"] in unread
    return {"conversations": rows}


@app.post("/api/conversations")
async def create_conversation(request: Request):
    user = auth.require(request)
    body = await request.json()
    with db.transaction() as conn:
        model = str(body.get("model") or "") or connections.default_model(conn)
    row = store.new_conversation(model, body.get("system") or "")
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
    model = str(body.get("model") or row.get("model") or "")
    with db.transaction() as conn:
        resolved = connections.resolve(conn, model)
    if resolved is None:
        raise HTTPException(404, f"No connection for {model}.")
    return await titles.generate(user, cid, model, target=connections.target(resolved["connection"]),
                                connection_id=resolved["connection"]["id"])


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, request: Request):
    user = auth.require(request)
    if not store.get(cid):
        raise HTTPException(404, "no such conversation")
    with db.transaction() as conn:
        cancelled = runs.cancel_for(conn, cid, "conversation")
        removed = uploads.delete_for_conversation(conn, user["id"], cid)
    if not store.delete(cid):
        raise HTTPException(404, "no such conversation")
    return {"ok": True, "cancelled_runs": cancelled, "files_removed": removed}


@app.get("/api/conversations/{cid}/messages")
def conversation_messages(cid: str, request: Request):
    user = auth.require(request)
    row = store.get(cid)
    if row is None:
        raise HTTPException(404, "no such conversation")
    with db.transaction() as conn:
        active = runs.active_for(conn, user["id"], cid)
        last = runs.last_for(conn, user["id"], cid)
        unavailable = bool(row.get("model")) and connections.resolve(conn, row["model"]) is None
        runs.mark_seen(conn, user["id"], cid)
    return {"conversation": {**row, "title": store.title_for(row)}, "messages": store.messages_raw(cid),
            "active_run_id": active["id"] if active else None,
            "active_run": active, "last_run": last, "model_unavailable": unavailable}


def run_params(body: dict, model: str) -> dict:
    """What a run needs to run a turn. The same fields the chat page sends."""
    return {
        "message": str(body.get("message") or ""),
        "model": model,
        "system": str(body.get("system") or ""),
        "temperature": float(body.get("temperature", 0.7)),
        "wiki_index": bool(body.get("wiki_index", True)),
        "regenerate": bool(body.get("regenerate")),
        "reasoning_effort": body.get("reasoning_effort") or reasoning.settings().get("default_level", "medium"),
        "web": bool(body.get("web_search")),
        "generate_titles": bool(body.get("generate_titles")),
        # File ids the message references. The run reads them from disk; the transcript stores only
        # the metadata, so a run is not the place the bytes live.
        "files": [str(item) for item in (body.get("files") or []) if str(item)],
    }


@app.post("/api/conversations/{cid}/messages")
async def create_run(cid: str, request: Request):
    """Create a run and answer immediately. The turn keeps running when this request ends.

    A second message while a run is active in the same chat is refused, because two answers in one
    conversation would not be in order. The Stop button calls /api/runs/<id>/cancel.
    """
    user = auth.require(request)
    body = await request.json()
    row = store.get(cid)
    if row is None:
        raise HTTPException(404, "no such conversation")

    with db.transaction() as conn:
        active = runs.active_for(conn, user["id"], cid)
        if active:
            return JSONResponse({"detail": "This chat is still generating. Stop that run first, or wait "
                                           "for it to finish.", "run_id": active["id"]}, status_code=409)

        # Checked before the run starts, as before; the same check runs again inside the loop.
        blocked = limits.check(conn, user)
        if blocked:
            return JSONResponse(blocked, status_code=429)

        model = str(body.get("model") or row.get("model") or "")
        params = run_params(body, model)
        per_message = settings.number(conn, "max_files_per_message")
        if per_message and len(params["files"]) > per_message:
            return JSONResponse({"detail": f"At most {per_message} files per message."}, status_code=400)
        # A file id the sender does not own is not a file: another user's id reads as no such file.
        for file_id in params["files"]:
            if uploads.get_for(conn, user["id"], file_id) is None:
                return JSONResponse({"detail": f"No such file: {file_id}. Upload it again."}, status_code=404)
        try:
            run = runs.create(conn, user, cid, params)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)

    runs.start(run)
    return JSONResponse({"run_id": run.id, "run": run.status_row()}, status_code=202)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request):
    user = auth.require(request)
    with db.transaction() as conn:
        stored = runs.row(conn, run_id)
    if stored is None or stored["user_id"] != user["id"]:
        raise HTTPException(404, "no such run")
    return {"run": stored}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    """The only way a run stops. A dropped SSE connection does not cancel anything."""
    user = auth.require(request)
    with db.transaction() as conn:
        result = runs.cancel(conn, run_id, user["id"])
    if result is None:
        raise HTTPException(404, "no such run")
    return {"run": result}


def last_event_id(request: Request, after: int) -> int:
    """Where to start replaying: the `after` query, or the browser's Last-Event-ID on a reconnect."""
    header = request.headers.get("last-event-id") or ""
    if header.strip().isdigit():
        return int(header)
    return int(after or 0)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0):
    """Replay the run's events from `after`, then continue live.

    Subscribing first is what makes this gap-free: an event that arrives while the replay is being
    written goes to both the buffer and the queue, so the queue part skips anything already sent.
    A client that comes back after the run is finished reads the transcript instead.
    """
    user = auth.require(request)
    with db.transaction() as conn:
        stored = runs.row(conn, run_id)
    if stored is None or stored["user_id"] != user["id"]:
        raise HTTPException(404, "no such run")

    run = runs.get(run_id)
    if run is None:
        # Not in this process. Nothing to stream: the transcript is the record.
        return JSONResponse({"run": stored, "replay": False,
                            "note": "This run is not in memory. Read the conversation instead."})

    return StreamingResponse(stream_run(run, last_event_id(request, after)),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def stream_run(run: runs.Run, after: int) -> AsyncIterator[str]:
    queue = run.subscribe()
    sent = after
    try:
        for record in run.events_from(after):
            sent = record["id"]
            yield sse(record["payload"], record["id"])
        while run.status in runs.ACTIVE:
            record = await queue.get()
            if record["id"] <= sent:
                continue
            sent = record["id"]
            yield sse(record["payload"], record["id"])
        for record in run.events_from(sent):
            yield sse(record["payload"], record["id"])
    finally:
        run.unsubscribe(queue)


# ---------- uploads ----------

UPLOAD_CHUNK = 64 * 1024
FORM_OVERHEAD = 4096   # the boundary lines and the small fields around the file in a multipart body


def read_chunks(handle, limit: int):
    """Read an upload piece by piece. The caller stops at the cap, so the rest of a file is never read.

    The framework has already put the body somewhere readable by the time this runs, so the cap is met
    part way through the file; a request whose declared length is over the cap is refused before its
    body is read at all, which is the case a browser announces up front.
    """
    while True:
        piece = handle.read(UPLOAD_CHUNK)
        if not piece:
            break
        yield piece


def attachment_headers(row: dict) -> dict:
    """Both file endpoints say: this is the user's own file, served as a download, not sniffed."""
    return {"Content-Disposition": f'attachment; filename="{uploads.clean_name(row["name"])}"',
            "X-Content-Type-Options": "nosniff"}


@app.post("/api/uploads")
async def upload_file(request: Request):
    """One file for the current user. The bytes go to that user's upload folder under a generated name;
    the name in the form is what the chip shows, never a path.

    The type is decided from the content, not from the filename. The size cap is checked while reading,
    and the storage quota is checked before the file is kept.
    """
    user = auth.require(request)
    per_file = settings.number(db.connect(), "max_upload_mb")
    declared = int(request.headers.get("content-length") or 0)
    if declared and declared > per_file * 1_000_000 + FORM_OVERHEAD:
        # The length is announced before the body arrives, so this is refused without reading it.
        raise HTTPException(413, uploads.too_large_message(per_file))

    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "file"):
        raise HTTPException(400, "No file was sent.")
    name = str(form.get("name") or upload.filename or "file")
    conversation_id = str(form.get("conversation_id") or "") or None
    with db.transaction() as conn:
        try:
            saved = uploads.create(conn, user, name, conversation_id,
                                  read_chunks(upload.file, per_file * 1_000_000))
        except uploads.UploadError as error:
            raise HTTPException(error.status, error.message)
    return {"file": saved}


@app.get("/api/uploads")
def list_uploads(request: Request):
    """What the Storage section shows: the user's own files and how much space they take."""
    user = auth.require(request)
    with db.transaction() as conn:
        return {"files": uploads.list_for(conn, user["id"]), "usage": uploads.summary(conn, user["id"])}


@app.delete("/api/uploads/{file_id}")
def delete_upload(file_id: str, request: Request):
    user = auth.require(request)
    with db.transaction() as conn:
        if not uploads.delete(conn, user["id"], file_id):
            raise HTTPException(404, "No such file.")
    return {"ok": True}


@app.get("/api/uploads/{file_id}/download")
def download_upload(file_id: str, request: Request):
    """The file itself, for the owner only. Another user's id, including an admin's, is no such file."""
    user = auth.require(request)
    with db.transaction() as conn:
        row = uploads.get_for(conn, user["id"], file_id)
    if row is None:
        raise HTTPException(404, "No such file.")
    path = uploads.file_path(row)
    if not path.exists():
        raise HTTPException(404, "That file is no longer on disk.")
    return Response(path.read_bytes(), media_type=row["media_type"] or "application/octet-stream",
                   headers=attachment_headers(row))


@app.get("/api/uploads/{file_id}/thumbnail")
def thumbnail_upload(file_id: str, request: Request):
    """The same bytes for an image, for the owner only. Nothing here resizes: there is no image library,
    so the chip scales the original."""
    user = auth.require(request)
    with db.transaction() as conn:
        row = uploads.get_for(conn, user["id"], file_id)
    if row is None:
        raise HTTPException(404, "No such file.")
    if row["kind"] != "image":
        raise HTTPException(404, "That is not an image, so there is no thumbnail for it.")
    path = uploads.file_path(row)
    if not path.exists():
        raise HTTPException(404, "That file is no longer on disk.")
    return Response(path.read_bytes(), media_type=row["media_type"], headers=attachment_headers(row))


# ---------- api keys ----------

@app.get("/api/keys")
def list_keys(request: Request):
    user = auth.require(request)
    with db.transaction() as conn:
        return {"keys": apikeys.list_for(conn, user["id"]),
                "base_url": base_url(request), "allow_api": settings.flags(conn)["allow_api"]}


@app.post("/api/keys")
async def create_key(request: Request):
    """A key is shown once, here. What is stored is the hash and the first 8 characters."""
    user = auth.require(request)
    body = await request.json()
    with db.transaction() as conn:
        if not settings.flags(conn)["allow_api"]:
            raise HTTPException(403, "API access is off for this app.")
        try:
            created, token = apikeys.create(conn, user, str(body.get("name") or ""), str(body.get("expires") or ""))
        except ValueError as error:
            raise HTTPException(400, str(error))
    return {"key": created, "token": token, "note": "Copy it now: this is the only time it is shown."}


@app.post("/api/keys/{key_id}/revoke")
async def revoke_key(key_id: str, request: Request):
    user = auth.require(request)
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such key.")
        if row["user_id"] != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "That key belongs to another account.")
        apikeys.revoke(conn, key_id, user)
    return {"ok": True}


def base_url(request: Request) -> str:
    """The base a client is told to use, from the address the request came to."""
    scheme = auth.forwarded(request, "x-forwarded-proto") or (request.url.scheme or "http")
    host = request.headers.get("host") or f"{config.HOST}:{config.PORT}"
    return f"{scheme}://{host}/api/v1"


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
