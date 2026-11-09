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
    "allow_api": "on",
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
    "max_upload_mb": "20",
    "max_files_per_message": "5",
    "upload_quota_mb": "500",
    "attachment_inline_chars": "30000",
    "attachment_tool_iterations": "12",
    "pdf_max_pages": "40",
    "api_rate_limit_per_minute": "60",
    "api_max_concurrent_per_user": "4",
}

# A setting that is a list of words rather than a number. Kept apart so `number()` does not have to
# guess what a key means.
STRINGS = {
    "upload_extensions": (
        "png,jpg,jpeg,webp,gif,pdf,docx,txt,md,csv,json,yaml,yml,xml,html,htm,log,svg,"
        "py,js,mjs,ts,go,rs,c,h,cpp,hpp,java,sh,bash,sql,toml,ini,css,tex"
    ),
}

DEFAULTS = {**FLAGS, **VALUES, **STRINGS}


def raw(conn, key: str) -> str:
    saved = db.setting(conn, key)
    return saved if saved else DEFAULTS.get(key, "")


def number(conn, key: str) -> int:
    try:
        return int(raw(conn, key))
    except ValueError:
        return int(DEFAULTS.get(key, "0"))


def text(conn, key: str) -> str:
    """A list setting as written in the settings table."""
    return raw(conn, key)


def list_setting(conn, key: str) -> list[str]:
    return [item.strip().lower() for item in text(conn, key).replace(" ", ",").split(",") if item.strip()]


def flags(conn) -> dict:
    return {key: raw(conn, key) == "on" for key in FLAGS}


def values(conn) -> dict:
    """What the admin panel shows. The flags stay booleans, the numbers stay numbers."""
    out = {**flags(conn), **{key: number(conn, key) for key in VALUES if not key.startswith("default_")},
           "default_chat_model": raw(conn, "default_chat_model"),
           "default_dream_model": raw(conn, "default_dream_model")}
    for key in STRINGS:
        out[key] = text(conn, key)
    return out


def set_value(conn, actor: dict, key: str, value) -> dict:
    """One change, audited. A flag is stored as on/off; a number as its integer; a list as a
    comma-separated string, so what the panel shows is what is stored."""
    if key in FLAGS:
        db.set_setting(conn, key, "on" if bool(value) else "off")
        db.audit(conn, actor["username"], "setting", key, "on" if bool(value) else "off")
    elif key in VALUES:
        if key.startswith("default_"):
            text_value = str(value or "").strip()
        else:
            try:
                text_value = str(int(value))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number, not {value!r}")
        db.set_setting(conn, key, text_value)
        db.audit(conn, actor["username"], "setting", key, text_value)
    elif key in STRINGS:
        joined = ",".join(item.strip().lower() for item in str(value or "").replace(" ", ",").split(",") if item.strip())
        db.set_setting(conn, key, joined)
        db.audit(conn, actor["username"], "setting", key, joined)
    else:
        raise ValueError(f"Unknown setting: {key}")
    return values(conn)
