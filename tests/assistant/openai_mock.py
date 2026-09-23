"""A local stand-in for the OpenAI Responses API (``POST /v1/responses`` with ``stream: true``).

The event and object shapes were checked against the installed openai SDK: ``responses.stream()``
iterates what this server sends and ``get_final_response()`` returns the ``response.completed``
response. The same building blocks (``done_item``, ``response_object``) give the scripted client in
test_assistant.py real SDK response objects. No network access and no real API key.

    server = MockResponsesServer([turn([...items]), turn([...]), http_error(429, api_error_body(...))])
    server.base_url    # http://127.0.0.1:<port>/v1, e.g. for OPENAI_BASE_URL
    server.requests    # [{"path", "headers" (lower-cased names), "body" (parsed JSON)}, ...]
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "gpt-5.5"


# ------------------------------------------------------------------------- output items


@dataclass
class Item:
    kind: str  # message | refusal | function_call | reasoning
    id: str
    deltas: list[str]
    call_id: str = ""
    name: str = ""
    encrypted_content: str | None = None
    summary: list[str] = field(default_factory=list)
    phase: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # fields the API may add (e.g. "async")

    @property
    def text(self) -> str:
        return "".join(self.deltas)


def _parts(value: list[str] | str) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def message(item_id: str, deltas: list[str] | str, *, phase: str | None = None) -> Item:
    """An assistant message with one output_text part, streamed as the given deltas."""
    return Item("message", item_id, _parts(deltas), phase=phase)


def refusal(item_id: str, deltas: list[str] | str) -> Item:
    """An assistant message whose only content part is a refusal."""
    return Item("refusal", item_id, _parts(deltas))


def function_call(item_id: str, call_id: str, name: str, arguments: list[str] | str, **extra: Any) -> Item:
    """A function call whose JSON arguments string is streamed as the given deltas."""
    return Item("function_call", item_id, _parts(arguments), call_id=call_id, name=name, extra=extra)


def reasoning(
    item_id: str, encrypted_content: str | None = "enc-not-real", summary: list[str] | None = None
) -> Item:
    """A reasoning item (encrypted_content is returned with include=["reasoning.encrypted_content"])."""
    return Item("reasoning", item_id, [], encrypted_content=encrypted_content, summary=summary or [])


def usage(input_tokens: int, output_tokens: int, *, cached: int = 0, reasoning_tokens: int = 0) -> dict:
    """ResponseUsage: input_tokens includes the cached ones; output_tokens includes reasoning."""
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": input_tokens + output_tokens,
    }


def done_item(item: Item) -> dict:
    """The finished item, as in response.output_item.done and response.completed's output."""
    if item.kind in ("message", "refusal"):
        part = (
            {"type": "output_text", "annotations": [], "logprobs": [], "text": item.text}
            if item.kind == "message"
            else {"type": "refusal", "refusal": item.text}
        )
        out = {
            "id": item.id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        if item.phase:
            out["phase"] = item.phase
    elif item.kind == "function_call":
        out = {
            "id": item.id,
            "type": "function_call",
            "status": "completed",
            "arguments": item.text,
            "call_id": item.call_id,
            "name": item.name,
        }
    elif item.kind == "reasoning":
        out = {
            "id": item.id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": s} for s in item.summary],
        }
        if item.encrypted_content is not None:
            out["encrypted_content"] = item.encrypted_content
    else:
        raise ValueError(item.kind)
    return {**out, **item.extra}


def _added_item(item: Item) -> dict:
    """The item as announced in response.output_item.added (empty, in progress)."""
    if item.kind in ("message", "refusal"):
        return {"id": item.id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
    if item.kind == "function_call":
        return {
            "id": item.id,
            "type": "function_call",
            "status": "in_progress",
            "arguments": "",
            "call_id": item.call_id,
            "name": item.name,
        }
    return {"id": item.id, "type": "reasoning", "summary": []}


def response_object(
    resp_id: str,
    *,
    status: str,
    output: list[dict],
    usage_: dict | None = None,
    model: str = MODEL,
    incomplete_reason: str | None = None,
    error: dict | None = None,
) -> dict:
    """A Response object with every field openai.types.responses.Response requires."""
    return {
        "id": resp_id,
        "object": "response",
        "created_at": 1_790_000_000,
        "status": status,
        "background": False,
        "error": error,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "instructions": None,
        "max_output_tokens": None,
        "max_tool_calls": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": "medium", "summary": None},
        "service_tier": "default",
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": usage_,
        "user": None,
        "metadata": {},
    }


# ------------------------------------------------------------------------- one streamed call

_ids = itertools.count(1)


def turn(
    items: list[Item],
    *,
    usage: dict | None = None,  # noqa: A002 - the API's field name
    end: str = "completed",
    incomplete_reason: str = "max_output_tokens",
    error_code: str = "server_error",
    error_message: str = "The server had an error while processing your request.",
    model: str = MODEL,
) -> list[dict]:
    """The events of one streamed call. ``end`` picks how the stream finishes:

    completed      response.completed (the normal end)
    incomplete     response.incomplete with incomplete_details.reason
    failed         response.failed with error {code, message}
    error          an ``error`` event {type, code, message, param} (the SDK yields it, no exception)
    legacy_error   data {"error": {...}} without a type (the SDK raises openai.APIError)
    cut            the stream just stops
    """
    resp_id = f"resp_{next(_ids):04d}"
    usage = usage if usage is not None else _default_usage()
    seq = itertools.count(0)
    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append({**event, "sequence_number": next(seq)})

    for kind in ("response.created", "response.in_progress"):
        emit(
            {"type": kind, "response": response_object(resp_id, status="in_progress", output=[], model=model)}
        )
    finished: list[dict] = []
    for index, item in enumerate(items):
        emit({"type": "response.output_item.added", "output_index": index, "item": _added_item(item)})
        where = {"item_id": item.id, "output_index": index}
        if item.kind == "message":
            part = {"type": "output_text", "annotations": [], "logprobs": [], "text": ""}
            emit({"type": "response.content_part.added", **where, "content_index": 0, "part": part})
            for delta in item.deltas:
                emit(
                    {
                        "type": "response.output_text.delta",
                        **where,
                        "content_index": 0,
                        "delta": delta,
                        "logprobs": [],
                    }
                )
            emit(
                {
                    "type": "response.output_text.done",
                    **where,
                    "content_index": 0,
                    "text": item.text,
                    "logprobs": [],
                }
            )
            emit(
                {
                    "type": "response.content_part.done",
                    **where,
                    "content_index": 0,
                    "part": {**part, "text": item.text},
                }
            )
        elif item.kind == "refusal":
            part = {"type": "refusal", "refusal": ""}
            emit({"type": "response.content_part.added", **where, "content_index": 0, "part": part})
            for delta in item.deltas:
                emit({"type": "response.refusal.delta", **where, "content_index": 0, "delta": delta})
            emit({"type": "response.refusal.done", **where, "content_index": 0, "refusal": item.text})
            emit(
                {
                    "type": "response.content_part.done",
                    **where,
                    "content_index": 0,
                    "part": {**part, "refusal": item.text},
                }
            )
        elif item.kind == "function_call":
            for delta in item.deltas:
                emit({"type": "response.function_call_arguments.delta", **where, "delta": delta})
            emit({"type": "response.function_call_arguments.done", **where, "arguments": item.text})
        finished.append(done_item(item))
        emit({"type": "response.output_item.done", "output_index": index, "item": finished[-1]})

    if end in ("completed", "incomplete", "failed"):
        response = response_object(
            resp_id,
            status=end,
            output=finished,
            usage_=usage,
            model=model,
            incomplete_reason=incomplete_reason if end == "incomplete" else None,
            error={"code": error_code, "message": error_message} if end == "failed" else None,
        )
        emit({"type": f"response.{end}", "response": response})
    elif end == "error":
        emit({"type": "error", "code": error_code, "message": error_message, "param": None})
    elif end == "legacy_error":
        error = {"type": "server_error", "code": error_code, "message": error_message, "param": None}
        events.append({"__event__": "error", "error": error})  # no "type": the SDK raises APIError
    elif end != "cut":
        raise ValueError(end)
    return events


def _default_usage() -> dict:
    return usage(100, 20)


def sse(events: list[dict]) -> bytes:
    """Server-Sent Events as the Responses API sends them: ``event: <type>`` then ``data: <json>``."""
    chunks = []
    for event in events:
        event = dict(event)
        name = event.pop("__event__", None) or event["type"]
        chunks.append(f"event: {name}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n")
    return "".join(chunks).encode()


@dataclass
class http_error:  # noqa: N801 - reads like a marker in reply lists
    status: int
    body: dict
    headers: dict | None = None


def api_error_body(message: str, *, type_: str = "invalid_request_error", code: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


# ------------------------------------------------------------------------- the server


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        # A client that closes the connection early (a stopped answer) is not an error.
        if not isinstance(sys.exc_info()[1], ConnectionResetError | BrokenPipeError):
            super().handle_error(request, client_address)


class MockResponsesServer:
    """Threaded HTTP server on 127.0.0.1 answering POST /v1/responses from a queue of replies
    (event lists from turn() or http_error(...)); every request is recorded."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw),
                    }
                )
                if self.path.rstrip("/") != "/v1/responses":
                    return self._json(404, api_error_body(f"Unknown path {self.path}"))
                if not outer.replies:
                    return self._json(500, api_error_body("mock: no reply queued", type_="server_error"))
                reply = outer.replies.pop(0)
                if isinstance(reply, http_error):
                    return self._json(reply.status, reply.body, reply.headers)
                payload = sse(reply)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, status: int, body: dict, headers: dict | None = None) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-request-id", f"req_mock_{len(outer.requests)}")
                # No SDK retries for 429/5xx in tests unless a test asks for them.
                for key, value in {"x-should-retry": "false", **(headers or {})}.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> MockResponsesServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
