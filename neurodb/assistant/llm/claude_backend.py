"""Claude (Anthropic's Messages API): the client, a call described for either provider rendered for Claude,
and its answer read back. The only module that imports the anthropic SDK.

Every call is streamed (``client.beta.messages.stream``, then the final message), so a long answer never
meets the SDK's limit on calls left waiting. A request carries the effort (always), the answer's JSON
schema as structured output, the instructions marked for caching, a keyed hash of the person who asked
(``metadata.user_id``, never a name), and, while AI settings allows it and the model offers it, the
server-side refusal fallback: a declined answer is run again on another Claude model Anthropic chooses for
the reason. It never carries ``thinking`` (always on), ``temperature``, ``top_p``, ``top_k``,
``tool_choice`` or a prefill: Claude Opus 5.5 refuses each with a 400.

An account that does not accept the fallback's beta header refuses the request with a 400 before anything
is streamed: the call is made again without the fallback, and every call goes without it for 24 hours
(Needs attention says so).

Ask NeuroDB's answers with tools (``agent``) stream each round through :func:`stream_round` and send the
conversation back append-only: each round adds the assistant's whole content as Claude wrote it
(:func:`assistant_turn`, thinking blocks and their signatures unchanged) and then one user message with
every tool result (:func:`tool_result`).

A network failure while an answer streams is not wrapped by the SDK: it is raised here as a ServiceError
(``timeout`` for an answer that went silent, ``stream_cut`` for a dropped connection), so the person reads
"did not answer in time" or "could not finish this answer", never "Something went wrong".
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
from django.core.cache import caches

from neurodb.assistant import llm

logger = logging.getLogger(__name__)

Timeout = anthropic.Timeout
FALLBACK_BETA = (
    "server-side-fallback-2026-07-01"  # the header of the "default" form (the list form has its own)
)
MIN_OUTPUT_TOKENS = 2000  # thinking is always on and counts toward max_tokens
EPHEMERAL = {"type": "ephemeral"}  # the 5-minute cache
REFUSED_KEY = "neurodb.ai.fallbacks-refused"  # in the shared cache: the account refused the fallback's header
REFUSED_SECONDS = 24 * 3600

ERRORS = (
    # most specific first (APITimeoutError is an APIConnectionError; status errors are APIErrors)
    (anthropic.AuthenticationError, llm.KEY_REJECTED),
    (anthropic.PermissionDeniedError, llm.NOT_ALLOWED),
    (anthropic.NotFoundError, llm.NO_MODEL),
    (anthropic.RateLimitError, llm.BUSY),
    (anthropic.OverloadedError, llm.BUSY),
    (anthropic.RequestTooLargeError, llm.NOT_PROCESSED),
    (anthropic.BadRequestError, llm.NOT_PROCESSED),
    (anthropic.UnprocessableEntityError, llm.NOT_PROCESSED),
    (anthropic.APITimeoutError, llm.LATE),
    (anthropic.DeadlineExceededError, llm.LATE),
    (anthropic.APIConnectionError, llm.UNREACHABLE),
    (anthropic.APIStatusError, llm.UNAVAILABLE),  # other 5xx, and an error event mid-stream
    (anthropic.APIError, llm.UNAVAILABLE),
)
# An error event in the stream arrives as an APIStatusError (status 200) with the API's error type.
STREAM_TYPES = {
    "rate_limit_error": llm.BUSY,
    "overloaded_error": llm.BUSY,
    "timeout_error": llm.LATE,
}


def client() -> anthropic.Anthropic:
    if not llm.enabled() or not (key := llm.anthropic_key()):
        raise llm.AssistantUnavailable("The AI assistant is not configured.")
    # The key is always given here, so the SDK reads none from the environment or a profile (it still
    # reads ANTHROPIC_BASE_URL). Its own traces keep to metadata whatever the environment says: the
    # prompts never reach Application Insights.
    return anthropic.Anthropic(
        api_key=key,
        timeout=Timeout(llm.READ_TIMEOUT, connect=llm.CONNECT_TIMEOUT),
        max_retries=2,
        open_telemetry={"traces": {"content_mode": "metadata_only"}},
    )


# ------------------------------------------------------------------------------------- failures
def _no_credit(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.APIStatusError) and (exc.type == "billing_error" or exc.status_code == 402):
        return True
    return isinstance(exc, anthropic.BadRequestError) and "credit balance" in llm.api_message(exc).lower()


def friendly(exc: BaseException) -> str | None:
    """The message for one of the anthropic SDK's errors (``llm.friendly`` covers the rest)."""
    if _no_credit(exc):
        return llm.NO_CREDIT
    if isinstance(exc, anthropic.APIStatusError) and exc.type in STREAM_TYPES:
        return STREAM_TYPES[exc.type]
    return next((m for cls, m in ERRORS if isinstance(exc, cls)), None)


def is_quota(exc: BaseException) -> bool:
    """The Anthropic credit ran out: ``billing_error`` (a 402), or a 400 about the credit balance."""
    return _no_credit(exc)


def effort_refused(exc: BaseException) -> bool:
    """A 400 about the effort: the model does not take the one sent."""
    return isinstance(exc, anthropic.BadRequestError) and "effort" in llm.api_message(exc)


def too_large(exc: BaseException) -> bool:
    """A 413: the request (a long document, say) is larger than the API takes."""
    return isinstance(exc, anthropic.RequestTooLargeError)


def beta_refused(exc: BaseException) -> bool:
    """A 400 naming the ``anthropic-beta`` header: this account does not accept the refusal fallback."""
    return isinstance(exc, anthropic.BadRequestError) and "anthropic-beta" in llm.api_message(exc)


# ------------------------------------------------------------------------------- refusal fallback
def offers_fallbacks(model: str) -> bool:
    """Whether ``model`` takes the server-side refusal fallback (the price table's ``fallbacks`` column,
    exact ids: the models whose reference sends ``fallbacks: "default"``)."""
    from neurodb.assistant.usage import CLAUDE_PRICES

    entry = CLAUDE_PRICES.get(model)
    return bool(entry and entry["fallbacks"])


def fallbacks_refused() -> bool:
    """The account refused the fallback's header in the last 24 hours (one cache read, no query)."""
    try:
        return bool(caches["shared"].get(REFUSED_KEY))
    except Exception:
        return False


def _refuse_fallbacks() -> None:
    try:
        caches["shared"].set(REFUSED_KEY, True, REFUSED_SECONDS)
    except Exception:
        logger.warning("Claude: the refused fallback could not be remembered", exc_info=True)


def fallback_params(model: str, *, check: bool = False) -> dict[str, Any]:
    """The refusal fallback's request parameters for ``model``: none when AI settings switched it off, the
    model does not offer it, or (except for the connection test, where a refusal is shown) the account
    refused its header in the last 24 hours."""
    if not llm.snapshot().claude_fallbacks or not offers_fallbacks(model):
        return {}
    if not check and fallbacks_refused():
        return {}
    return {"betas": [FALLBACK_BETA], "fallbacks": "default"}


# --------------------------------------------------------------------------------- the request
def _no_type_unions(node: Any) -> Any:
    """A schema with ``"type": [X, "null"]`` written as ``anyOf`` (structured outputs take no type list),
    its description and enum kept on the outer object. ``properties`` is a map of names: walked by value."""
    if isinstance(node, list):
        return [_no_type_unions(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {
        key: (
            {name: _no_type_unions(value) for name, value in node[key].items()}
            if key in ("properties", "$defs", "definitions") and isinstance(node[key], dict)
            else _no_type_unions(value)
        )
        for key, value in node.items()
        if key != "type"
    }
    kind = node.get("type")
    if isinstance(kind, list):
        out["anyOf"] = [{"type": each} for each in kind]
    elif "type" in node:
        out = {"type": kind, **out}
    return out


_SCHEMAS: dict[str, dict[str, Any]] = {}


def schema(s: dict[str, Any]) -> dict[str, Any]:
    """``s`` as a Claude structured-output schema: type lists as ``anyOf``, then the SDK's
    ``transform_schema`` (``additionalProperties: false`` on every object; a keyword structured outputs do
    not take, such as ``maxItems``, moves into the description, and the code keeps enforcing it). Built once
    per schema, so the bytes sent stay the same and the API's schema cache keeps hitting."""
    key = json.dumps(s, sort_keys=True)
    if key not in _SCHEMAS:
        _SCHEMAS[key] = anthropic.transform_schema(_no_type_unions(s))
    return copy.deepcopy(_SCHEMAS[key])


def _block(part: llm.Text | llm.Pdf) -> dict[str, Any]:
    if isinstance(part, llm.Pdf):
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(part.data).decode("ascii"),
            },
            "title": part.filename,
        }
    return {"type": "text", "text": part.text}


def request(req: llm.Request) -> dict[str, Any]:
    """The parameters of ``client.beta.messages.stream`` for ``req``. Its sampling values are never sent."""
    model = llm.model_for(req.model, llm.CLAUDE)
    params: dict[str, Any] = {"model": model, "max_tokens": max(req.max_output_tokens, MIN_OUTPUT_TOKENS)}
    if req.system.strip():  # an empty text block is refused
        block: dict[str, Any] = {"type": "text", "text": req.system}
        if req.cache_key:  # the instructions are the stable prefix; the varying content follows them
            block["cache_control"] = EPHEMERAL
        params["system"] = [block]
    content = req.user if isinstance(req.user, str) else [_block(part) for part in req.user]
    params["messages"] = [{"role": "user", "content": content}]
    output: dict[str, Any] = {"effort": llm.effort_for(req.effort, llm.CLAUDE)}
    if req.schema is not None:
        output["format"] = {"type": "json_schema", "schema": schema(req.schema)}
    params["output_config"] = output
    if req.user_id:
        params["metadata"] = {"user_id": req.user_id}  # a keyed hash, never the id, username or e-mail
    params.update(fallback_params(model))
    return params


# ---------------------------------------------------------------------------- reading the answer
def _count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _tokens(entry: Any) -> llm.Usage:
    # Anthropic's input_tokens counts neither cache reads nor writes: the writes are prompt tokens not read
    # from the cache, so they are added to it (and kept apart too)
    created = _count(getattr(entry, "cache_creation_input_tokens", 0))
    return llm.Usage(
        input_tokens=_count(getattr(entry, "input_tokens", 0)) + created,
        cache_read_tokens=_count(getattr(entry, "cache_read_input_tokens", 0)),
        cache_write_tokens=created,
        output_tokens=_count(getattr(entry, "output_tokens", 0)),
    )


def by_model(message: Any, asked: str) -> dict[str, llm.Usage]:
    """A message's tokens per model that ran them. With refusal fallbacks the attempts are in
    ``usage.iterations``: a declined attempt (``message``) under its model, or ``asked`` when it names
    none, and the fallback's attempt (``fallback_message``) under the fallback's model, each at its own
    model's prices. Without them the message's usage, under the model that wrote it."""
    top = getattr(message, "usage", None)
    attempts = [
        entry
        for entry in (getattr(top, "iterations", None) or [])
        if getattr(entry, "type", "") in ("message", "fallback_message")
    ]
    out: dict[str, llm.Usage] = {}
    if attempts:
        for entry in attempts:
            model = getattr(entry, "model", None) or asked
            out[model] = out.get(model, llm.Usage()) + _tokens(entry)
    elif top is not None:
        out[getattr(message, "model", None) or asked] = _tokens(top)
    return out


def usage(message: Any) -> llm.Usage:
    """A message's tokens, every attempt added up (a declined one too: see :func:`by_model`)."""
    return sum(by_model(message, "").values(), llm.Usage())


def fell_back(message: Any) -> bool:
    """Another Claude model answered after a refusal (a sticky turn too, which has no fallback block)."""
    top = getattr(message, "usage", None)
    return any(
        getattr(entry, "type", "") == "fallback_message" for entry in getattr(top, "iterations", None) or []
    )


def answer_text(message: Any) -> str:
    """The text blocks in order, apart by a blank line, except across a fallback block: after a fallback
    in the middle of the answer, the continuation goes on from the partial text. An empty block counts as
    none."""
    text, wrote, joined = "", False, False
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "fallback":
            joined = True
        elif kind == "text" and block.text:
            if wrote and not joined:
                text += "\n\n"
            text += block.text
            wrote, joined = True, False
    return text


def read(message: Any, req: llm.Request) -> llm.Result:
    """A final message as a Result. A refused answer keeps no text (a partial one is never used); a stream
    that stopped before its end raises ServiceError."""
    stop_reason = getattr(message, "stop_reason", None)
    if message is None or stop_reason is None:
        raise llm.ServiceError("stream_ended", "the answer stopped before its end")
    text = answer_text(message)
    reason = str(stop_reason)
    if stop_reason in ("end_turn", "stop_sequence", "tool_use"):
        stop = "ok"
    elif stop_reason == "max_tokens":
        stop = "max_tokens"
    elif stop_reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        stop, reason, text = "refused", f"refusal: {category}" if category else "refusal", ""
    else:  # model_context_window_exceeded, pause_turn, compaction
        stop = "incomplete"
    parsed = None
    if req.schema is not None and text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    model = llm.model_for(req.model, llm.CLAUDE)
    wrote = getattr(message, "model", "") or ""
    return llm.Result(
        text=text,
        parsed=parsed,
        stop=stop,
        reason=reason,
        model=model,
        served_by=wrote if fell_back(message) else "",
        usage=usage(message),
        provider=llm.CLAUDE,
        raw=message,
        by_model=by_model(message, model),
        model_calls={wrote or model: 1},  # the call, on the model that wrote the answer
    )


def _final(api: anthropic.Anthropic, params: dict[str, Any]) -> Any:
    try:
        with api.beta.messages.stream(**params) as stream:
            return stream.get_final_message()
    except AssertionError as exc:  # the stream ended before the message started
        raise llm.ServiceError("stream_ended", str(exc)) from exc
    except RuntimeError as exc:  # the stream helper could not follow the events
        raise llm.ServiceError("stream_error", str(exc)) from exc
    except httpx2.TimeoutException as exc:  # silent past the read timeout while it streamed
        raise llm.ServiceError("timeout", type(exc).__name__) from exc
    except httpx2.TransportError as exc:  # the connection dropped while it streamed
        raise llm.ServiceError("stream_cut", type(exc).__name__) from exc


def send(api: anthropic.Anthropic, params: dict[str, Any]) -> Any:
    """Stream one call to its final message. A 400 naming the fallback's header (before anything was
    streamed) is sent once more without the fallback, and the next 24 hours' calls go without it."""
    try:
        return _final(api, params)
    except anthropic.BadRequestError as exc:
        if "betas" not in params or not beta_refused(exc):
            raise
        logger.warning("Claude: this account does not accept refusal fallbacks; calls go without them")
        _refuse_fallbacks()
        return _final(api, {k: v for k, v in params.items() if k not in ("betas", "fallbacks")})


# ---------------------------------------------------------------------- an answer with tools (agent)
def _round(api: anthropic.Anthropic, params: dict[str, Any], round_no: int) -> Iterator[dict[str, Any]]:
    try:
        with api.beta.messages.stream(**params) as stream:
            wrote = joined = separate = False
            for event in stream:
                if event.type == "content_block_start":
                    kind = event.content_block.type
                    if kind == "fallback":
                        joined = True  # the continuation goes on from the partial text
                    elif kind == "text":
                        separate = wrote and not joined
                elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                    if not event.delta.text:
                        continue
                    if separate:  # another text block (the answer after a note): keep the two apart
                        yield {"type": "text", "text": "\n\n", "round": round_no}
                    wrote, joined, separate = True, False, False
                    yield {"type": "text", "text": event.delta.text, "round": round_no}
            message = stream.get_final_message()
    except ValueError as exc:
        # A tool's input streamed as JSON the SDK cannot read at all: no tool call to answer (the caller
        # sends the round again once). The SDK's API errors are not ValueErrors and pass through.
        raise llm.ServiceError("tool_input", str(exc)[:300]) from exc
    except AssertionError as exc:  # the stream ended before the message started
        raise llm.ServiceError("stream_ended", str(exc)) from exc
    except RuntimeError as exc:  # the stream helper could not follow the events
        raise llm.ServiceError("stream_error", str(exc)) from exc
    except httpx2.TimeoutException as exc:  # silent past the read timeout while it streamed
        raise llm.ServiceError("timeout", type(exc).__name__) from exc
    except httpx2.TransportError as exc:  # the connection dropped while it streamed
        raise llm.ServiceError("stream_cut", type(exc).__name__) from exc
    if message.stop_reason is None:  # the stream stopped before its end
        raise llm.ServiceError("stream_ended", "the answer stopped before its end")
    return message


def stream_round(api: anthropic.Anthropic, params: dict[str, Any], round_no: int):
    """One round of an answer with tools: yields the text events as Claude writes them (a blank line
    between two text blocks, none across a fallback block) and returns the final message. A 400 naming the
    fallback's header comes before any event: the round is sent again without the fallback, which is taken
    out of ``params`` for the answer's next rounds too (and of every call for 24 hours). When the browser
    goes away the stream is closed, and with it the HTTP response."""
    try:
        return (yield from _round(api, params, round_no))
    except anthropic.BadRequestError as exc:
        if "betas" not in params or not beta_refused(exc):
            raise
        logger.warning("Claude: this account does not accept refusal fallbacks; calls go without them")
        _refuse_fallbacks()
        params.pop("betas", None)
        params.pop("fallbacks", None)
        return (yield from _round(api, params, round_no))


def _after_fallback(message: Any) -> list[tuple[bool, Any]]:
    """The message's blocks, each with whether it comes after the last fallback block (all of them when
    there is none)."""
    content = list(getattr(message, "content", None) or [])
    last = max((i for i, block in enumerate(content) if block.type == "fallback"), default=-1)
    return [(i > last, block) for i, block in enumerate(content)]


def tool_calls(message: Any) -> list[SimpleNamespace]:
    """The tool calls to run: the ``tool_use`` blocks after the last fallback block (a declined attempt's
    calls are never run), each as ``name``, ``arguments`` (its input as JSON text, read and checked like
    OpenAI's) and ``call_id``."""
    return [
        SimpleNamespace(name=block.name, arguments=json.dumps(block.input), call_id=block.id)
        for after, block in _after_fallback(message)
        if after and block.type == "tool_use"
    ]


def assistant_turn(message: Any) -> dict[str, Any]:
    """The assistant's message to send back, rebuilt field by field from what Claude wrote, in order (the
    SDK's own fields, such as ``parsed_output``, never go back). Thinking blocks keep their signature
    unchanged. A fallback block is an audit marker and is left out, and so are the thinking and tool
    calls of a declined attempt, before the last fallback block; a text block with no text is left out too
    (the API refuses one). No other block type is expected: no server tool is offered."""
    content: list[dict[str, Any]] = []
    for after, block in _after_fallback(message):
        kind = block.type
        if kind == "text":
            if block.text.strip():
                content.append({"type": "text", "text": block.text})
        elif not after:
            continue
        elif kind == "thinking":
            content.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif kind == "redacted_thinking":
            content.append({"type": "redacted_thinking", "data": block.data})
        elif kind == "tool_use":
            content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return {"role": "assistant", "content": content}


def tool_result(call_id: str, output: str, ok: bool) -> dict[str, Any]:
    """One tool's result (JSON text) for the user message that follows the round; an error is marked as
    one, so Claude can correct its call."""
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": output,
        **({} if ok else {"is_error": True}),
    }


class Connection:
    """Claude, ready for calls: :func:`client`, held to ``timeout`` seconds (10 to connect) and
    ``retries`` retries when they are given."""

    provider = llm.CLAUDE

    def __init__(self, timeout: float | None = None, retries: int | None = None) -> None:
        api = client()
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = Timeout(timeout, connect=llm.CONNECT_TIMEOUT)
        if retries is not None:
            options["max_retries"] = retries
        self.api = api.with_options(**options) if options else api

    def complete(self, req: llm.Request) -> llm.Result:
        return read(send(self.api, request(req)), req)

    def check(self) -> llm.Result:
        """The connection test's call, not streamed: one short message and 16 output tokens at most, with
        the fallback when it is on (an account that refuses it shows it here, with no retry). Any stop
        counts: the thinking may use the 16 tokens."""
        model = llm.default_model(llm.CLAUDE)
        params = {
            "model": model,
            "max_tokens": 16,
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": llm.PROBE}],
            **fallback_params(model, check=True),
        }
        message = self.api.beta.messages.create(**params)
        if "betas" in params:  # accepted again: the next calls may send it
            try:
                caches["shared"].delete(REFUSED_KEY)
            except Exception:
                logger.warning("Claude: the refused fallback could not be forgotten", exc_info=True)
        return read(message, llm.Request(model=model))
