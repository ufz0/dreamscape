"""The settings an admin changes while the app is running: the key/value settings table, not .env.

.env is only what a process needs before it can open the database (the data folder, the encryption
key, the bind). Anything the admin can change from the admin panel lives in app_settings.

Values are stored as text, so the meaning of a number is stated here once:
- A token limit of 0 means no limit. -1 means explicitly unlimited, which a user needs now that an
  empty value inherits the global one.
- Models are namespaced ids: <connection slug>/<model id>.
"""
import db

FLAGS = {
    "allow_self_registration": "off",
    "count_dreaming_toward_limits": "off",
}

VALUES = {
    "global_daily_limit": "0",
    "global_weekly_limit": "0",
    "default_chat_model": "",
    "default_dream_model": "",
    "max_runs_per_user": "3",
    "max_runs_global": "10",
    "max_run_minutes": "30",
    "queue_timeout_seconds": "300",
}

DEFAULTS = {**FLAGS, **VALUES}


def raw(conn, key: str) -> str:
    saved = db.setting(conn, key)
    return saved if saved else DEFAULTS.get(key, "")


def number(conn, key: str) -> int:
    try:
        return int(raw(conn, key))
    except ValueError:
        return int(DEFAULTS.get(key, "0"))


def flags(conn) -> dict:
    return {key: raw(conn, key) == "on" for key in FLAGS}


def values(conn) -> dict:
    """What the admin panel shows. The flags stay booleans, the numbers stay numbers."""
    return {**flags(conn), **{key: number(conn, key) for key in VALUES if key != "default_chat_model" and key != "default_dream_model"},
            "default_chat_model": raw(conn, "default_chat_model"),
            "default_dream_model": raw(conn, "default_dream_model")}


def set_value(conn, actor: dict, key: str, value) -> dict:
    """One change, audited. A flag is stored as on/off; a number as its integer."""
    if key in FLAGS:
        db.set_setting(conn, key, "on" if bool(value) else "off")
        db.audit(conn, actor["username"], "setting", key, "on" if bool(value) else "off")
    elif key in VALUES:
        if key.startswith("default_"):
            text = str(value or "").strip()
        else:
            try:
                text = str(int(value))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number, not {value!r}")
        db.set_setting(conn, key, text)
        db.audit(conn, actor["username"], "setting", key, text)
    else:
        raise ValueError(f"Unknown setting: {key}")
    return values(conn)
