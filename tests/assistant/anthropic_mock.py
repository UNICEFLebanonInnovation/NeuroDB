"""A local stand-in for Anthropic's Messages API (``POST /v1/messages``, streamed or not), as openai_mock.py
is for the Responses API.

The event and object shapes were checked against the installed anthropic SDK: ``beta.messages.stream()``
iterates what this server sends and ``get_final_message()`` returns the message they build. No network
access and no real API key.

    server = MockMessagesServer([turn([thinking("sig-1"), text(["Fresh", "."])]), http_error(529, ...)])
    server = MockMessagesServer([message_json([text("OK")])])  # a call without stream
    server.base_url    # http://127.0.0.1:<port>, for ANTHROPIC_BASE_URL
    server.requests    # [{"path", "headers" (lower-cased names), "body" (parsed JSON)}, ...]
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "claude-opus-5-5"


# ------------------------------------------------------------------------------------- content blocks


@dataclass
class Block:
    kind: str  # thinking | text | tool_use | fallback
    deltas: list[str] = field(default_factory=list)
    signature: str = ""
    id: str = ""
    name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(self.deltas)


def _parts(value: list[str] | str) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def thinking(signature: str, text: list[str] | str = "") -> Block:
    """A thinking block: its text (empty under the default "omitted" display), then its signature."""
    return Block("thinking", [t for t in _parts(text) if t], signature=signature)


def text(deltas: list[str] | str) -> Block:
    """A text block streamed as the given deltas."""
    return Block("text", _parts(deltas))


def tool_use(block_id: str, name: str, input_deltas: list[str] | str) -> Block:
    """A tool call whose JSON input is streamed as the given deltas (input_json_delta)."""
    return Block("tool_use", _parts(input_deltas), id=block_id, name=name)


def fallback(from_model: str, to_model: str, category: str | None = "cyber") -> Block:
    """The marker of a refusal fallback: ``from_model`` declined and ``to_model`` goes on."""
    return Block(
        "fallback",
        extra={
            "from": {"model": from_model},
            "to": {"model": to_model},
            "trigger": {"type": "refusal", "category": category},
        },
    )


def usage(
    input_tokens: int,
    output_tokens: int,
    *,
    read: int = 0,
    write: int = 0,
    iterations: list[dict] | None = None,
) -> dict:
    """The Messages API's usage: ``input_tokens`` counts neither cache reads nor cache writes."""
    out = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }
    if iterations is not None:
        out["iterations"] = iterations
    return out


def iteration(kind: str, input_tokens: int, output_tokens: int, *, model: str = MODEL, read: int = 0,
              write: int = 0) -> dict:  # fmt: skip
    """One attempt in ``usage.iterations``: ``message`` (an attempt of the model asked for, a declined one
    too) or ``fallback_message`` (the fallback's)."""
    return {
        "type": kind,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }


def _start_block(block: Block) -> dict:
    if block.kind == "thinking":
        return {"type": "thinking", "thinking": "", "signature": ""}
    if block.kind == "text":
        return {"type": "text", "text": ""}
    if block.kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": {}}
    if block.kind == "fallback":
        return {"type": "fallback", **block.extra}
    raise ValueError(block.kind)


def done_block(block: Block) -> dict:
    """The finished block, as in a non-streamed message."""
    if block.kind == "thinking":
        return {"type": "thinking", "thinking": block.text, "signature": block.signature}
    if block.kind == "text":
        return {"type": "text", "text": block.text}
    if block.kind == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": json.loads(block.text or "{}"),
        }
    return _start_block(block)


def message_object(msg_id: str, content: list[dict], *, model: str = MODEL, stop_reason: str | None = None,
                   stop_details: dict | None = None, usage_: dict | None = None) -> dict:  # fmt: skip
    """A Message with every field anthropic.types.beta.BetaMessage requires."""
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage_ if usage_ is not None else _default_usage(),
    }


def _default_usage() -> dict:
    return usage(100, 20)


# -------------------------------------------------------------------------------- one streamed call

_ids = itertools.count(1)


def turn(blocks: list[Block], *, stop_reason: str | None = "end_turn", stop_details: dict | None = None,
         usage: dict | None = None, model: str = MODEL, end: str = "complete") -> list[dict]:  # noqa: A002  # fmt: skip
    """The events of one streamed call: message_start, each block (with a ping before it), message_delta
    (the stop reason and the final usage), message_stop. ``end`` picks how the stream finishes:

    complete   message_delta and message_stop (the normal end)
    cut        the stream just stops after the blocks (no message_delta: no stop reason)
    """
    msg_id = f"msg_{next(_ids):04d}"
    usage = usage if usage is not None else _default_usage()
    start_usage = {key: value for key, value in usage.items() if key != "iterations"}
    events: list[dict] = [
        {"type": "message_start", "message": message_object(msg_id, [], model=model, usage_=start_usage)}
    ]
    for index, block in enumerate(blocks):
        events.append({"type": "ping"})
        events.append({"type": "content_block_start", "index": index, "content_block": _start_block(block)})
        if block.kind == "thinking":
            for piece in block.deltas:
                delta = {"type": "thinking_delta", "thinking": piece}
                events.append({"type": "content_block_delta", "index": index, "delta": delta})
            delta = {"type": "signature_delta", "signature": block.signature}
            events.append({"type": "content_block_delta", "index": index, "delta": delta})
        elif block.kind == "text":
            for piece in block.deltas:
                delta = {"type": "text_delta", "text": piece}
                events.append({"type": "content_block_delta", "index": index, "delta": delta})
        elif block.kind == "tool_use":
            for piece in block.deltas:
                delta = {"type": "input_json_delta", "partial_json": piece}
                events.append({"type": "content_block_delta", "index": index, "delta": delta})
        events.append({"type": "content_block_stop", "index": index})
    if end == "cut":
        return events
    if end != "complete":
        raise ValueError(end)
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details},
            "usage": usage,
        }
    )
    events.append({"type": "message_stop"})
    return events


def refusal(category: str | None = "cyber", blocks: list[Block] | None = None, **kwargs: Any) -> list[dict]:
    """A declined answer: stop reason ``refusal`` with its stop details (before any output, or after the
    partial ``blocks``)."""
    details = {"type": "refusal", "category": category, "explanation": "Declined for this test."}
    return turn(blocks or [], stop_reason="refusal", stop_details=details, **kwargs)


def stream_error(type_: str, message: str, blocks: list[Block] | None = None) -> list[dict]:
    """A stream that ends with an ``error`` event (after the partial ``blocks``)."""
    events = turn(blocks or [], end="cut")
    events.append({"__event__": "error", "type": "error", "error": {"type": type_, "message": message}})
    return events


def cut(blocks: list[Block] | None = None) -> list[dict]:
    """A stream that stops before its end: no stop reason, no message_stop."""
    return turn(blocks or [text("Partial")], end="cut")


def cut_before_start() -> list[dict]:
    """A stream that stops before message_start."""
    return [{"type": "ping"}]


def sse(events: list[dict]) -> bytes:
    """Server-Sent Events as the Messages API sends them: ``event: <type>`` then ``data: <json>``."""
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


@dataclass
class json_reply:  # noqa: N801 - reads like a marker in reply lists
    """A whole Message as JSON: the answer to a call made without ``stream`` (the connection test)."""

    body: dict


@dataclass
class stall:  # noqa: N801 - reads like a marker in reply lists
    """Stream these events, then wait ``seconds`` before the rest; or, with ``drop``, close the connection
    then, before the length the response declared (the client reads a body cut short: a network error)."""

    events: list[dict]
    seconds: float = 0.0
    rest: list[dict] = field(default_factory=list)
    drop: bool = False


def message_json(blocks: list[Block], *, stop_reason: str = "end_turn", model: str = MODEL,
                 usage_: dict | None = None) -> json_reply:  # fmt: skip
    """The answer of a call without ``stream``."""
    return json_reply(
        message_object(
            f"msg_{next(_ids):04d}",
            [done_block(block) for block in blocks],
            model=model,
            stop_reason=stop_reason,
            usage_=usage_,
        )
    )


def error_body(type_: str, message: str) -> dict:
    return {"type": "error", "error": {"type": type_, "message": message}, "request_id": "req_mock"}


# ------------------------------------------------------------------------------------------ the server


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        # A client that closes the connection early (a stopped answer, a timeout) is not an error.
        if not isinstance(sys.exc_info()[1], ConnectionResetError | BrokenPipeError):
            super().handle_error(request, client_address)


class MockMessagesServer:
    """Threaded HTTP server on 127.0.0.1 answering POST /v1/messages from a queue of replies (event lists
    from turn(), http_error(...), json_reply, stall); every request is recorded."""

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
                if self.path.split("?")[0].rstrip("/") != "/v1/messages":
                    return self._json(404, error_body("not_found_error", f"Unknown path {self.path}"))
                if not outer.replies:
                    return self._json(500, error_body("api_error", "mock: no reply queued"))
                reply = outer.replies.pop(0)
                if isinstance(reply, http_error):
                    return self._json(reply.status, reply.body, reply.headers)
                if isinstance(reply, json_reply):
                    return self._json(200, reply.body)
                if isinstance(reply, stall):
                    return self._stall(reply)
                payload = sse(reply)
                self._stream_head(len(payload))
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _stream_head(self, length: int | None) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                if length is None:
                    self.send_header("Connection", "close")
                else:
                    self.send_header("Content-Length", str(length))
                self.end_headers()

            def _stall(self, reply: stall) -> None:
                sent = sse(reply.events)
                self._stream_head(len(sent) + 1000 if reply.drop else None)
                try:
                    self.wfile.write(sent)
                    self.wfile.flush()
                    time.sleep(reply.seconds)
                    if not reply.drop:
                        self.wfile.write(sse(reply.rest))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def _json(self, status: int, body: dict, headers: dict | None = None) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("request-id", f"req_mock_{len(outer.requests)}")
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
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> MockMessagesServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
