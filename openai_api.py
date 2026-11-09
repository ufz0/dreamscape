"""The OpenAI-compatible API: /api/v1, entered with a Bearer key instead of a session cookie.

Three routes: the model list, chat completions (stream and non-stream), and embeddings. The body is
passed through unchanged apart from what the connection needs: the model id the engine knows, its auth,
headers, path and body overrides, and the reasoning mapping when `reasoning_effort` is sent. Nothing
here is injected — no wiki, no memory, no web search, no system prompt. This is a plain API.

What the app does add: the connection's queue, the token limits, and a usage row for every call with
source="api" and the key it came through. Errors are always in the OpenAI shape,
{"error": {"message", "type", "code"}}.

Two things the app deliberately does differently from a chat run:

- A stream always asks the upstream for `stream_options.include_usage` so the counts are real, and
  strips that extra chunk when the client did not ask for it.
- A client that goes away mid-stream cancels the upstream and records the tokens counted so far. A chat
  run keeps running when the tab closes; an API call does not, because the call is the client's.
"""
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

import apikeys
import connections
import db
import limits
import llm
import reasoning
import settings
import usage

router = APIRouter(prefix="/api/v1")

# The type for an HTTP status, in the words an OpenAI client reads.
TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_denied_error",
    404: "not_found_error",
    429: "rate_limit_error",
    502: "upstream_error",
    504: "upstream_error",
}


class ApiError(Exception):
    """A refusal in the OpenAI shape. Raised anywhere in this module; `handle` turns it into a response."""

    def __init__(self, status: int, message: str, type: str = "", code: str = "", headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type or TYPES.get(status, "api_error")
        self.code = code
        self.headers = headers or {}

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


def handle(request: Request, error: ApiError) -> JSONResponse:
    """The exception handler app.py registers for this module."""
    return error.response()


def frame(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def seconds_until(value: str) -> int:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return 60
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(1, int((moment - datetime.now(timezone.utc)).total_seconds()))


def upstream_error(error: Exception, model: str) -> ApiError:
    """A failed upstream call in the same shape, with the message the engine gave when it gave one."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", 0) or 502
    detail = ""
    if response is not None:
        try:
            body = response.json()
            detail = str(((body or {}).get("error") or {}).get("message") or "")
        except Exception:
            detail = ""
    if isinstance(error, httpx.TimeoutException):
        status = 504
        detail = detail or f"The upstream did not answer for model {model}."
    elif not detail:
        detail = f"The upstream refused the request for model {model}: {error}"
    return ApiError(status, detail, code="upstream_error")


def record(user_id, key_id: str, model: str, block: dict, estimated: bool = False) -> None:
    if not block:
        return
    with db.transaction() as conn:
        usage.record(conn, user_id, block, "api", model, estimated=estimated, key_id=key_id)


def key_for(request: Request) -> dict:
    """The key for this request. A session cookie is not accepted here, and a key is not accepted on the UI routes."""
    record = apikeys.authenticate(apikeys.bearer(request.headers.get("authorization", "")))
    if record is None:
        raise ApiError(401, "No API key. Send one as Authorization: Bearer dsk-….",
                       "authentication_error", "invalid_api_key")
    return record


def guard(key: dict) -> dict:
    """The per-key rate limit and the per-user concurrent-request cap, checked before the call."""
    wait = apikeys.rate_limit(key["id"])
    if wait:
        limit = settings.number(db.connect(), "api_rate_limit_per_minute")
        raise ApiError(429, f"This key is limited to {limit} requests per minute. Try again in {wait} seconds.",
                       "rate_limit_error", "rate_limit_exceeded", headers={"Retry-After": str(wait)})
    maximum = settings.number(db.connect(), "api_max_concurrent_per_user")
    if not apikeys.hold(key["user"]["id"]):
        raise ApiError(429, f"This account already has {maximum} requests in flight, which is its maximum.",
                      "rate_limit_error", "concurrent_limit_exceeded", headers={"Retry-After": "1"})
    return key


# ---------- the model list ----------

@router.get("/models")
async def list_models(request: Request):
    """The models the key's account may use, in the OpenAI list format. The ids are the namespaced ones."""
    key = key_for(request)
    with db.transaction() as conn:
        groups = connections.visible(conn, key["user"])
    data = []
    for group in groups:
        for model in group["models"]:
            data.append({"id": model["namespaced"], "object": "model",
                        "created": created_seconds(model.get("added") or model.get("last_seen") or ""),
                        "owned_by": key["user"]["username"]})
    return {"object": "list", "data": data}


def created_seconds(value: str) -> int:
    """The list format wants a unix timestamp; the app stores an ISO string."""
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return 0


# ---------- chat completions ----------

@router.post("/chat/completions")
async def chat_completions(request: Request):
    key = guard(key_for(request))
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "The request body must be JSON.", "invalid_request_error", "invalid_json")
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.", "invalid_request_error", "invalid_json")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "messages is required.", "invalid_request_error", "missing_messages")
    requested = str(body.get("model") or "")

    with db.transaction() as conn:
        resolved = resolve_for_api(conn, requested, key["user"])
        blocked = limits.check(conn, key["user"])
        if blocked:
            raise ApiError(429, blocked["message"], "rate_limit_error", "limit_exceeded",
                           headers={"Retry-After": str(seconds_until(blocked["resets_at"]))})

    target = connections.target(resolved["connection"])
    payload = request_payload(body, resolved, target)

    if body.get("stream"):
        wanted = bool((body.get("stream_options") or {}).get("include_usage"))
        # The counts are only real if the upstream sends them, so they are always asked for; the
        # chunk is dropped again on the way out when the client did not ask.
        payload = {**payload, "stream_options": {**(body.get("stream_options") or {}),
                                                  "include_usage": True}}
        return StreamingResponse(stream(request, key, requested, target, payload, messages, wanted),
                                media_type="text/event-stream",
                                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        async with connections.gate(target).acquire():
            async with llm.client(target) as client:
                response = await client.post(target.get("path_chat") or "/chat/completions", json=payload)
                response.raise_for_status()
                answer = response.json()
    except ApiError:
        apikeys.release(key["user"]["id"])
        raise
    except Exception as error:
        apikeys.release(key["user"]["id"])
        raise upstream_error(error, requested) from error

    answer = dict(answer)
    answer["model"] = requested          # what the client asked for, not what the engine calls it
    block = answer.get("usage") or {}
    if not block:
        # No usage from the engine: the same estimate the chat uses, and the row says it is one.
        choice = (answer.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "")
        block = usage.estimate(messages, text) if text else {}
    record(key["user"]["id"], key["id"], requested, block, estimated=not bool(answer.get("usage")))
    apikeys.release(key["user"]["id"])
    return answer


def resolve_for_api(conn, requested: str, user: dict) -> dict:
    """The connection and model row for an id, with the refusals a plain API should make."""
    model = connections.model_for(conn, requested)
    if model is None:
        raise ApiError(404, f"No model {requested!r} on this app.", "not_found_error", "model_not_found")
    connection = connections.get_by_slug(conn, connections.split_namespaced(requested)[0])
    if connection is None:
        raise ApiError(404, f"No model {requested!r} on this app.", "not_found_error", "model_not_found")
    if not connection["enabled"]:
        raise ApiError(403, f"{connection['name']} is disabled.", "permission_denied_error", "connection_disabled")
    if connection["visibility"] == "admins" and user["role"] != "admin":
        raise ApiError(403, f"{connection['name']} is only visible to admins.",
                       "permission_denied_error", "model_not_available")
    return {"connection": connection, "model": model}


def request_payload(body: dict, resolved: dict, target: dict) -> dict:
    """The client's body, with the connection's own fields applied on top. Nothing else is changed.

    `reasoning_effort` is passed through as the client sent it, and the model's reasoning mapping is
    applied on top of it, because that is what makes the value mean something to this engine.
    """
    payload = dict(body)
    payload["model"] = llm.request_model(target, body.get("model") or "")
    mapping = {}
    effort = body.get("reasoning_effort")
    if effort:
        layers = reasoning.layer(resolved["connection"].get("reasoning_map"),
                                resolved["model"].get("reasoning_map"))
        mapping = reasoning.resolve(body.get("model") or "", str(effort), layers).get("body") or {}
    for key, value in connections.request_body(resolved, mapping).items():
        payload[key] = value
    return payload


async def stream(request: Request, key: dict, requested: str, target: dict, payload: dict,
                messages: list, keep_usage: bool):
    """Forward the upstream stream as it arrives, and record what it counted.

    The model field is rewritten to the id the client asked for; the usage chunk is dropped when the
    client did not ask for it. A client that goes away mid-stream stops the upstream here, which is
    the intended difference from a chat run.
    """
    user_id = key["user"]["id"]
    counted: dict = {}
    text = ""
    try:
        async with connections.gate(target).acquire():
            async with llm.client(target) as client:
                async with client.stream("POST", target.get("path_chat") or "/chat/completions",
                                        json=payload) as response:
                    try:
                        response.raise_for_status()
                    except Exception as error:
                        yield frame(upstream_error(error, requested).body())
                        return
                    async for line in response.aiter_lines():
                        if await request.is_disconnected():
                            # Nothing to send to a client that is gone; the tokens so far are still theirs.
                            break
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            counted = chunk["usage"]
                            if not keep_usage:
                                continue
                        if chunk.get("choices"):
                            for choice in chunk["choices"]:
                                text += (choice.get("delta") or {}).get("content") or ""
                        chunk["model"] = requested
                        yield frame(chunk)
    except ApiError as error:
        yield frame(error.body())
    except Exception as error:
        yield frame(upstream_error(error, requested).body())
    finally:
        if counted:
            record(user_id, key["id"], requested, counted, estimated=False)
        elif text:
            record(user_id, key["id"], requested, usage.estimate(messages, text), estimated=True)
        apikeys.release(user_id)


# ---------- embeddings ----------

@router.post("/embeddings")
async def embeddings(request: Request):
    """A passthrough, and only for a connection that answers it. There is no embeddings support otherwise."""
    key = guard(key_for(request))
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "The request body must be JSON.", "invalid_request_error", "invalid_json")
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.", "invalid_request_error", "invalid_json")
    requested = str(body.get("model") or "")
    if not body.get("input"):
        raise ApiError(400, "input is required.", "invalid_request_error", "missing_input")

    with db.transaction() as conn:
        resolved = resolve_for_api(conn, requested, key["user"])
        blocked = limits.check(conn, key["user"])
        if blocked:
            raise ApiError(429, blocked["message"], "rate_limit_error", "limit_exceeded",
                           headers={"Retry-After": str(seconds_until(blocked["resets_at"]))})

    target = connections.target(resolved["connection"])
    payload = request_payload(body, resolved, target)
    try:
        async with connections.gate(target).acquire():
            async with llm.client(target) as client:
                response = await client.post("/embeddings", json=payload)
                if response.status_code == 404:
                    raise ApiError(404, f"{resolved['connection']['name']} does not serve embeddings.",
                                   "not_found_error", "model_not_found")
                response.raise_for_status()
                answer = response.json()
    except ApiError:
        apikeys.release(key["user"]["id"])
        raise
    except Exception as error:
        apikeys.release(key["user"]["id"])
        raise upstream_error(error, requested) from error

    answer = dict(answer)
    answer["model"] = requested
    record(key["user"]["id"], key["id"], requested, answer.get("usage") or {},
           estimated=not bool(answer.get("usage")))
    apikeys.release(key["user"]["id"])
    return answer
