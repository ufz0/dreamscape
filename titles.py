"""Chat titles: the model names a conversation after its first reply.

A conversation is only named when it has no stored title, so a name someone typed by hand is never
overwritten. The call is metered like any other upstream call: the limit is checked first, and when
it is reached the title stays whatever the app would show anyway (the first line of the message).
"""
import re

import config
import db
import limits
import llm
import reasoning
import store
import usage as usagelog

MAX_TITLE_CHARS = 60
MAX_TRANSCRIPT_CHARS = 1200


def clean(value: str) -> str:
    """One line, no quotes: whatever the model answered, in the form a sidebar name needs."""
    line = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'`").strip()
    return line[:MAX_TITLE_CHARS]


def first_exchange(cid: str) -> str:
    """The first user message and the first answer, which is what the name comes from."""
    records = store.messages_raw(cid)
    parts = []
    for role in ("user", "assistant"):
        for record in records:
            content = re.sub(r"\s+", " ", str(record.get("content") or "")).strip()
            if record.get("role") == role and content:
                parts.append(f"{role}: {content[:MAX_TRANSCRIPT_CHARS]}")
                break
    return "\n".join(parts)


async def generate(user: dict, cid: str, model: str, target: dict | None = None,
                   connection_id: int | None = None) -> dict:
    """{"title", "generated", "note"}. The title is always something the UI can show."""
    row = store.get(cid)
    if row is None:
        raise ValueError("no such conversation")

    stored = (row.get("title") or "").strip()
    if stored:
        return {"title": clean(stored), "generated": False, "note": "This chat already has a name."}

    prompt = config.read_prompt("title.md")
    text = first_exchange(cid)
    if not text:
        return {"title": "New chat", "generated": False, "note": "Nothing to name yet."}

    with db.transaction() as conn:
        blocked = limits.check(conn, user)
    if blocked:
        # Naming a chat is not worth a blocked turn: show the message as the title instead.
        return {"title": store.title_for(row), "generated": False, "note": blocked["message"]}

    body = dict(reasoning.resolve(model, "none").get("body") or {})
    body.setdefault("max_tokens", 40)
    try:
        result = await llm.complete(target or llm.default_target(), model,
                                   [{"role": "system", "content": prompt},
                                    {"role": "user", "content": text}], 0.3, extra_body=body)
    except Exception as error:
        return {"title": store.title_for(row), "generated": False, "note": f"Could not ask the model: {error}"}

    reported = result.get("usage") or {}
    block = reported or usagelog.estimate([{"role": "system", "content": prompt},
                                          {"role": "user", "content": text}], result.get("text") or "")
    with db.transaction() as conn:
        usagelog.record(conn, user["id"], block, "chat", model, cid, not reported,
                       connection_id=connection_id)

    title = clean(result.get("text") or "")
    if not title:
        return {"title": store.title_for(row), "generated": False, "note": "The model did not return a name."}

    store.rename(cid, title)
    return {"title": title, "generated": True, "note": f"Named this chat: {title}"}
