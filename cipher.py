"""Secrets at rest: the values that should not be readable in the database file.

A connection's API key and the values of its extra headers, later MCP tokens. Fernet, from the
`cryptography` package.

The key comes from APP_SECRET_KEY. When that is not set, one is generated once into data/secret.key
with mode 600, and that is logged: a key generated on first run means the data folder is where these
secrets can be read, so it belongs in the backup.

Secrets are write-only in the API and the UI. After saving, the app shows only that one is set, or
the last four characters, and never returns the stored value.
"""
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import config

KEY_FILE = config.DATA_DIR / "secret.key"
announced = False


def key() -> bytes:
    global announced
    from_env = config.env("APP_SECRET_KEY", "").strip()
    if from_env:
        return from_env.encode()

    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()

    value = Fernet.generate_key()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(value)
    KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 600
    if not announced:
        announced = True
        print(f"cipher: APP_SECRET_KEY is not set, so a key was generated at {KEY_FILE} (mode 600). "
              "Set APP_SECRET_KEY if you want the key to live outside the data folder.", flush=True)
    return value


def cipher() -> Fernet:
    return Fernet(key())


def encrypt(value) -> str:
    """Stored form. Empty stays empty, so "no key" is not an encrypted empty string."""
    text = str(value or "")
    if not text:
        return ""
    return cipher().encrypt(text.encode()).decode()


def decrypt(value) -> str:
    """The plaintext, or the stored text unchanged when it was written before encryption existed."""
    text = str(value or "")
    if not text:
        return ""
    try:
        return cipher().decrypt(text.encode()).decode()
    except (InvalidToken, ValueError):
        return text


def shown(value) -> dict:
    """What the API and the UI may show about a secret: that it is set, and the last four characters."""
    plaintext = decrypt(value)
    if not plaintext:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": plaintext[-4:]}
