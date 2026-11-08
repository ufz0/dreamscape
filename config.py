"""Configuration: .env values and the fixed data paths."""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader so we do not need python-dotenv."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


BASE_URL = env("STRATA_BASE_URL", "http://10.0.0.57:8080/v1").rstrip("/")
API_KEY = env("STRATA_API_KEY", "")
MODEL = env("STRATA_MODEL", "qwen3.8-flash-next")
HOST = env("HOST", "127.0.0.1")
PORT = env_int("PORT", 8000)

data_path = Path(env("DATA_DIR", "data"))
DATA_DIR = data_path if data_path.is_absolute() else BASE_DIR / data_path
INBOX = DATA_DIR / "transcripts" / "inbox"
PROCESSED = DATA_DIR / "transcripts" / "processed"
WIKI = DATA_DIR / "wiki"
PROMPTS = BASE_DIR / "prompts"
STATIC = BASE_DIR / "static"

IDLE_MINUTES = env_int("IDLE_MINUTES", 60)
MAX_TOOL_ITERATIONS = env_int("MAX_TOOL_ITERATIONS", 8)
INDEX_MAX_LINES = env_int("INDEX_MAX_LINES", 150)
PAGE_MAX_LINES = env_int("PAGE_MAX_LINES", 120)
SEARCH_LIMIT = env_int("SEARCH_LIMIT", 40)

# Slug rules for the wiki sandbox: lowercase slug, .md only, no traversal.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


def ensure_dirs() -> None:
    for path in (INBOX, PROCESSED, WIKI):
        path.mkdir(parents=True, exist_ok=True)


def read_prompt(name: str, **values) -> str:
    """Prompt templates are plain text files so they can be tweaked without touching code."""
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
