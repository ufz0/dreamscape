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

# Web tools. The provider is duckduckgo unless Brave is configured with a key.
SEARCH_PROVIDER = env("SEARCH_PROVIDER", "duckduckgo").strip().lower()
BRAVE_API_KEY = env("BRAVE_API_KEY", "")
SEARCH_TIMEOUT = env_int("SEARCH_TIMEOUT", 10)
SEARCH_CACHE_MINUTES = env_int("SEARCH_CACHE_MINUTES", 10)
SEARCH_RATE_LIMIT = env_int("SEARCH_RATE_LIMIT", 20)
SEARCH_RATE_LIMIT_MINUTES = env_int("SEARCH_RATE_LIMIT_MINUTES", 10)

# The tool loop keeps its own limit; a turn with web tools may use a few more iterations, up to a
# hard cap.
WEB_TOOL_ITERATIONS = env_int("WEB_TOOL_ITERATIONS", 12)
MAX_TOOL_ITERATIONS_CAP = env_int("MAX_TOOL_ITERATIONS_CAP", 16)

WEB_FETCH_MAX_CHARS = env_int("WEB_FETCH_MAX_CHARS", 24000)     # about 6000 tokens worth of text
WEB_FETCH_MAX_BYTES = env_int("WEB_FETCH_MAX_BYTES", 2_000_000)
WEB_FETCH_TIMEOUT = env_int("WEB_FETCH_TIMEOUT", 10)
WEB_FETCH_MAX_REDIRECTS = env_int("WEB_FETCH_MAX_REDIRECTS", 3)
WEB_FETCH_ALLOW_PRIVATE = env("WEB_FETCH_ALLOW_PRIVATE", "false").strip().lower() in ("1", "true", "yes", "on")
USER_AGENT = env("WEB_USER_AGENT", "Dreamscape/1.0")

# Slug rules for the wiki sandbox: lowercase slug, .md only, no traversal.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")


def ensure_dirs() -> None:
    """Root data folder. Per-user folders are created by appdata.Paths.ensure()."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def read_prompt(name: str, **values) -> str:
    """Prompt templates are plain text files so they can be tweaked without touching code."""
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
