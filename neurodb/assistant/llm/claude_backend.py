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
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from typing import Any

import anthropic
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


def usage(message: Any) -> llm.Usage:
    """A message's tokens. With refusal fallbacks the attempts are in ``usage.iterations``: a declined
    attempt's (``message``) and the fallback's (``fallback_message``) are added up. Anthropic's
    ``input_tokens`` counts neither cache reads nor writes, so the writes are added to it (they are prompt
    tokens not read from the cache) and also kept apart."""
    top = getattr(message, "usage", None)
    attempts = [
        entry
        for entry in (getattr(top, "iterations", None) or [])
        if getattr(entry, "type", "") in ("message", "fallback_message")
    ]
    prompt = read = write = output = 0
    for entry in attempts or ([top] if top is not None else []):
        created = _count(getattr(entry, "cache_creation_input_tokens", 0))
        prompt += _count(getattr(entry, "input_tokens", 0)) + created
        read += _count(getattr(entry, "cache_read_input_tokens", 0))
        write += created
        output += _count(getattr(entry, "output_tokens", 0))
    return llm.Usage(
        input_tokens=prompt, cache_read_tokens=read, cache_write_tokens=write, output_tokens=output
    )


def fell_back(message: Any) -> bool:
    """Another Claude model answered after a refusal (a sticky turn too, which has no fallback block)."""
    top = getattr(message, "usage", None)
    return any(
        getattr(entry, "type", "") == "fallback_message" for entry in getattr(top, "iterations", None) or []
    )


def answer_text(message: Any) -> str:
    """The text blocks in order, apart by a blank line, except across a fallback block: after a fallback
    in the middle of the answer, the continuation goes on from the partial text."""
    text, wrote, joined = "", False, False
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "fallback":
            joined = True
        elif kind == "text":
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
    return llm.Result(
        text=text,
        parsed=parsed,
        stop=stop,
        reason=reason,
        model=llm.model_for(req.model, llm.CLAUDE),
        served_by=(getattr(message, "model", "") or "") if fell_back(message) else "",
        usage=usage(message),
        provider=llm.CLAUDE,
        raw=message,
    )


def _final(api: anthropic.Anthropic, params: dict[str, Any]) -> Any:
    try:
        with api.beta.messages.stream(**params) as stream:
            return stream.get_final_message()
    except AssertionError as exc:  # the stream ended before the message started
        raise llm.ServiceError("stream_ended", str(exc)) from exc
    except RuntimeError as exc:  # the stream helper could not follow the events
        raise llm.ServiceError("stream_error", str(exc)) from exc


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
