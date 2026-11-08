"""Reasoning effort: how each level (none/low/medium/high) is expressed to a given model.

The mapping lives in data/reasoning.json so it can be changed without editing code. Each level is a
dict of request-body fields to merge, an optional line appended to the system prompt, and an optional
token budget. `model_overrides` replaces any of that for models matched by name pattern, so a mapping
is applied only where the endpoint actually honours it. A level that resolves to no request fields is
prompt-only, and the UI says so.
"""
import fnmatch
import json

import config

CONFIG_FILE = config.DATA_DIR / "reasoning.json"

# Shipped default, matching what the Strata endpoint answered (see README): reasoning_effort is
# honoured at all four levels, chat_template_kwargs.enable_thinking also turns thinking off. A
# "system" line is used only when a level has no request field, i.e. when the server would ignore
# the control and the level can only be approximated by wording.
DEFAULT = {
    "default_level": "medium",
    "dream": {"per_transcript": "medium", "cleanup": "high"},
    "levels": {
        "none": {"label": "None", "description": "answer directly", "body": {"reasoning_effort": "none"},
                 "system": "Answer directly, without thinking out loud."},
        "low": {"label": "Low", "description": "brief thinking", "body": {"reasoning_effort": "low"},
                "system": "Think very briefly before answering."},
        "medium": {"label": "Medium", "description": "balanced", "body": {"reasoning_effort": "medium"}},
        "high": {"label": "High", "description": "think thoroughly", "body": {"reasoning_effort": "high"},
                 "system": "Think carefully and check your work before answering."},
    },
    "model_overrides": {
        # Qwen3-style chat templates: enable_thinking is the switch that works there.
        "qwen3*": {"levels": {"none": {"body": {"chat_template_kwargs": {"enable_thinking": False}}}}},
    },
}

LEVELS = ["none", "low", "medium", "high"]


def settings() -> dict:
    """The mapping file, or the built-in default when it does not exist yet."""
    if not CONFIG_FILE.exists():
        return dict(DEFAULT)
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(DEFAULT)
    merged = {**DEFAULT, **saved}
    merged["levels"] = {**DEFAULT["levels"], **saved.get("levels", {})}
    merged["model_overrides"] = {**DEFAULT["model_overrides"], **saved.get("model_overrides", {})}
    return merged


def override_for(model: str) -> dict:
    """First pattern that matches the model name; exact names win over wildcards."""
    for pattern, rule in settings().get("model_overrides", {}).items():
        if model == pattern:
            return rule
    for pattern, rule in settings().get("model_overrides", {}).items():
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(model, pattern):
                return rule
    return {}


def resolve(model: str, level: str) -> dict:
    """What to send for one model at one level: body fields, extra system text, budget, and whether
    the level is only approximated by wording."""
    config_map = settings()
    level_key = level if level in LEVELS else config_map.get("default_level", "medium")
    spec = dict(config_map["levels"].get(level_key, {}))
    rule = override_for(model)
    spec = {**spec, **rule.get("levels", {}).get(level_key, {})}

    body = dict(spec.get("body") or {})
    budget = spec.get("budget")
    if budget:
        body.setdefault("max_tokens", budget)

    # A wording line is only used when the level has no request field, so a working server
    # parameter is never applied twice.
    extra = spec.get("system", "") if not body else ""
    approximated = not body and bool(extra)

    return {
        "level": level_key,
        "label": spec.get("label", level_key.capitalize()),
        "description": spec.get("description", ""),
        "body": body,
        "system": extra,
        "budget": budget,
        "approximated": approximated,
    }
