"""Accounts: users, passwords, sessions, login rate limiting, admin changes, audit.

Security basics:
- Passwords are hashed with argon2 (argon2-cffi), never stored in plaintext.
- Session tokens are random, stored only as a SHA-256 hash, and sent as an HttpOnly, SameSite=Lax
  cookie (Secure when served over https). 30 day expiry, sliding on each request.
- Changing a password, disabling a user or deleting a user invalidates that user's sessions.
- Failed logins are counted per username and per IP; 5 failures in 10 minutes lock the login
  temporarily for both.
- There must always be at least one active admin.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

import db
import limits
import settings as app_settings

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
SESSION_DAYS = 30
MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 10

passwords = PasswordHasher()


def normalize_username(value: str) -> str:
    return str(value or "").strip().lower()


def validate_username(value: str) -> str | None:
    """None when the username is acceptable, otherwise the reason it is not."""
    name = normalize_username(value)
    if not name:
        return "A username is required."
    if len(name) < 3 or len(name) > 32:
        return "Usernames are 3 to 32 characters."
    if not USERNAME_RE.match(name):
        return "Usernames may only contain lowercase letters, digits, dot, underscore and dash."
    return None


def validate_password(value: str) -> str | None:
    text = str(value or "")
    if len(text) < MIN_PASSWORD_LENGTH:
        return f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def hash_password(value: str) -> str:
    return passwords.hash(value)


def verify_password(hash_value: str, value: str) -> bool:
    try:
        return passwords.verify(hash_value, value)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def public(row) -> dict:
    """What the app may show about a user. The password hash is never returned."""
    if row is None:
        return None
    return {key: row[key] for key in
            ("id", "username", "role", "status", "created", "last_login", "must_change_password",
             "daily_limit", "weekly_limit")} | {"must_change_password": bool(row["must_change_password"])}


# ---------- users ----------

def get_user(conn, user_id) -> dict | None:
    return public(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_by_username(conn, username: str) -> dict | None:
    return public(conn.execute("SELECT * FROM users WHERE username = ?", (normalize_username(username),)).fetchone())


def list_users(conn) -> list[dict]:
    """The users, each with the limit that actually applies per window and where it comes from."""
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    users = [public(row) for row in rows]
    for user in users:
        user["effective"] = {window: limits.resolve(conn, user, window) for window in ("daily", "weekly")}
    return users


def count_admins(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'").fetchone()["n"])


def create_user(conn, username: str, password: str, role: str = "user", must_change: bool = False,
                daily_limit: int = 0, weekly_limit: int = 0, actor: str = "system") -> dict:
    error = validate_username(username) or validate_password(password)
    if error:
        raise ValueError(error)
    name = normalize_username(username)
    if conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone():
        raise ValueError(f"That username is already taken: {name}")
    if role not in ("admin", "user"):
        raise ValueError("Role must be admin or user.")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, status, must_change_password, created, "
        "daily_limit, weekly_limit) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
        (name, hash_password(password), role, 1 if must_change else 0, db.now(),
         limits.parse_limit(daily_limit), limits.parse_limit(weekly_limit)),
    )
    db.audit(conn, actor, "create", name, f"role={role}")
    user = get_by_username(conn, name)
    # The first account owns whatever data existed before per-user folders.
    if int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]) == 1:
        import appdata
        moved = appdata.migrate_legacy(user["id"])
        if moved["moved"]:
            db.audit(conn, actor, "migrate", name, f"{len(moved)} files moved, backup {moved['backup']}")
    return user


def change_password(conn, actor: dict, user_id: int, current: str, new: str) -> None:
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("No such user.")
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not verify_password(row["password_hash"], current or ""):
        raise ValueError("The current password is not correct.")
    error = validate_password(new)
    if error:
        raise ValueError(error)
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                 (hash_password(new), user_id))
    invalidate_sessions(conn, user_id, keep=str(actor.get("session") or ""))
    db.audit(conn, actor["username"], "change_password", user["username"])


def reset_password(conn, actor: dict, user_id: int, new: str) -> str:
    """Admin sets a temporary password; the user must change it at the next login."""
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("No such user.")
    error = validate_password(new)
    if error:
        raise ValueError(error)
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                 (hash_password(new), user_id))
    invalidate_sessions(conn, user_id)
    db.audit(conn, actor["username"], "reset_password", user["username"])
    return new


def set_limits(conn, actor: dict, user_id: int, daily, weekly) -> dict:
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("No such user.")
    try:
        values = (limits.parse_limit(daily), limits.parse_limit(weekly))
    except ValueError as error:
        raise ValueError(f"Limit: {error}")
    conn.execute("UPDATE users SET daily_limit = ?, weekly_limit = ? WHERE id = ?", (*values, user_id))
    db.audit(conn, actor["username"], "limits", user["username"], f"daily={values[0]} weekly={values[1]}")
    return get_user(conn, user_id)


def set_role(conn, actor: dict, user_id: int, role: str) -> dict:
    user = get_user(conn, user_id)
    if role not in ("admin", "user"):
        raise ValueError("Role must be admin or user.")
    if role == "user" and count_admins(conn) <= 1 and user and user["role"] == "admin":
        raise ValueError("That is the last active admin, so it cannot be demoted.")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.audit(conn, actor["username"], "role", user["username"], f"role={role}")
    return get_user(conn, user_id)


def set_status(conn, actor: dict, user_id: int, status: str) -> dict:
    user = get_user(conn, user_id)
    if status not in ("active", "disabled"):
        raise ValueError("Status must be active or disabled.")
    if status == "disabled" and count_admins(conn) <= 1 and user and user["role"] == "admin":
        raise ValueError("That is the last active admin, so it cannot be disabled.")
    conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
    invalidate_sessions(conn, user_id)
    db.audit(conn, actor["username"], "status", user["username"], f"status={status}")
    return get_user(conn, user_id)


def delete_user(conn, actor: dict, user_id: int) -> dict:
    """Deletes the user and its data folder. Usage rows are kept with user_id set to NULL, so the
    aggregate totals for a period do not change; that is documented in the README."""
    user = get_user(conn, user_id)
    if user is None:
        raise ValueError("No such user.")
    if count_admins(conn) <= 1 and user["role"] == "admin":
        raise ValueError("That is the last active admin, so it cannot be deleted.")
    import appdata
    folder = appdata.user_dir(user["id"])
    removed = sum(1 for _ in folder.rglob("*")) if folder.is_dir() else 0
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if folder.is_dir():
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    db.audit(conn, actor["username"], "delete", user["username"], f"{removed} files removed, usage rows kept")
    return {"username": user["username"], "files_removed": removed}


# ---------- sessions ----------

def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def create_session(conn, user: dict) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions (token_hash, user_id, created, expires, last_seen) VALUES (?, ?, ?, ?, ?)",
                 (hash_token(token), user["id"], db.now(), expires, db.now()))
    return token


def get_session(conn, token: str) -> dict | None:
    """Resolve a cookie to a user, sliding the expiry, or None when the session is not usable."""
    row = conn.execute("SELECT s.*, u.username, u.role, u.status, u.must_change_password, "
                       "u.daily_limit, u.weekly_limit FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ?", (hash_token(token),)).fetchone()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = limits.parse_time(row["expires"])
    if expires <= now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if row["status"] != "active":
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if (expires - now).days < SESSION_DAYS - 5:
        sliding = (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        conn.execute("UPDATE sessions SET expires = ?, last_seen = ? WHERE token_hash = ?",
                     (sliding, db.now(), row["token_hash"]))
    return {"id": row["user_id"], "username": row["username"], "role": row["role"],
            "status": row["status"], "must_change_password": bool(row["must_change_password"]),
            "daily_limit": row["daily_limit"], "weekly_limit": row["weekly_limit"],
            "session": row["token_hash"]}


def delete_session(conn, token_hash: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def invalidate_sessions(conn, user_id: int, keep: str = "") -> int:
    sql = "DELETE FROM sessions WHERE user_id = ?"
    params = [user_id]
    if keep:
        sql += " AND token_hash != ?"
        params.append(keep)
    return conn.execute(sql, params).rowcount


# ---------- login rate limiting ----------

def record_login(conn, username: str, ip: str, ok: bool) -> None:
    conn.execute("INSERT INTO login_attempts (username, ip, at, ok) VALUES (?, ?, ?, ?)",
                 (normalize_username(username), ip or "", db.now(), 1 if ok else 0))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM login_attempts WHERE at < ?", (cutoff,))


def lockout(conn, username: str, ip: str) -> dict | None:
    """The remaining lockout for that name or that IP, or None when login is allowed."""
    name = normalize_username(username)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
    row = conn.execute("SELECT COUNT(*) AS n, MAX(at) AS last FROM login_attempts "
                       "WHERE ok = 0 AND at >= ? AND (username = ? OR ip = ?)",
                       (cutoff, name, ip or "")).fetchone()
    if row["n"] < MAX_LOGIN_FAILURES:
        return None
    last = limits.parse_time(row["last"])
    remaining = int((last + timedelta(minutes=LOCKOUT_MINUTES) - datetime.now(timezone.utc)).total_seconds())
    return {"failures": row["n"], "minutes": max(1, remaining // 60),
            "message": f"Too many failed logins for {name}. Try again in {max(1, remaining // 60)} minutes."}


# ---------- admin settings ----------

def settings(conn) -> dict:
    """The boolean flags, through the settings table. Values live in settings.py."""
    return app_settings.flags(conn)


def set_setting(conn, actor: dict, key: str, value: bool) -> dict:
    if key not in app_settings.FLAGS:
        raise ValueError(f"Unknown setting: {key}")
    app_settings.set_value(conn, actor, key, bool(value))
    return settings(conn)


def audit_log(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
