"""Auth wiring for the app: the session cookie, the CSRF check, and who may see what.

A request resolves the session cookie to a user. That user becomes the current user for the whole
call, so store/wiki/dreamlog/graph work inside its folder and can not reach another user's data.

Cookies: HttpOnly, SameSite=Lax, path /. Secure is set when the request came in over https, which
behind a reverse proxy is read from X-Forwarded-Proto. The CSRF token is a random value in a
separate cookie, and a state-changing request must send it back as X-CSRF-Token.
"""
import secrets
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import accounts
import appdata
import db

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# These still work while a temporary password has not been changed yet.
ALLOWED_WHILE_UNCHANGED = ("/api/me", "/api/auth/change-password", "/api/auth/logout")


def is_https(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return forwarded == "https" or request.url.scheme == "https"


def cookie_options(request: Request) -> dict:
    return {"httponly": True, "samesite": "lax", "path": "/", "secure": is_https(request)}


def set_session_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=30 * 24 * 3600, **cookie_options(request))


def set_csrf_cookie(response, request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
    # Readable by JS so the page can send it back as a header; that is what makes the
    # double-submit check work for cross-site requests.
    options = dict(cookie_options(request))
    options["httponly"] = False
    response.set_cookie(CSRF_COOKIE, token, max_age=30 * 24 * 3600, **options)
    return token


def csrf_ok(request: Request) -> bool:
    if request.method in SAFE_METHODS:
        return True
    token = request.cookies.get(CSRF_COOKIE)
    return bool(token) and request.headers.get("x-csrf-token") == token


def resolve(request: Request) -> dict | None:
    """The session cookie to a user, sliding the expiry, and the current user for this call."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with db.transaction() as conn:
        user = accounts.get_session(conn, token)
    if user is None:
        return None
    appdata.set_current(user["id"])
    return user


def require(request: Request) -> dict:
    user = resolve(request)
    if user is None:
        raise HTTPException(401, "Please log in.")
    if user["must_change_password"] and request.url.path not in ALLOWED_WHILE_UNCHANGED:
        raise HTTPException(403, "Change the temporary password before using the app.")
    return user


def page_gate(request: Request):
    """For pages: the user, or a redirect to setup (no accounts yet) / login."""
    with db.transaction() as conn:
        first_run = int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]) == 0
    if first_run:
        return None, RedirectResponse("/setup")
    user = resolve(request)
    if user is None:
        return None, RedirectResponse("/login")
    return user, None


def require_admin(request: Request) -> dict:
    user = require(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only.")
    return user


def redirect_to(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={quote(request.url.path)}")


def no_users(request: Request) -> JSONResponse | None:
    """API call before any account exists: say what is needed rather than a bare 401."""
    with db.transaction() as conn:
        if int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]) == 0:
            return JSONResponse({"detail": "No accounts yet. Open /setup to create the first one."}, status_code=401)
    return None
