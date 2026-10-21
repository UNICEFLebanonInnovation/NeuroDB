"""ChatGPT (OpenAI's Responses API): the client, a call described for either provider rendered as the
request each feature has always sent, and its response read back. The only module that imports the openai
SDK.

``agent.client`` is this module's :func:`client` (``agent`` re-exports it), and every call here looks it
up there when it is made, so the tests that replace ``agent.client`` with a scripted client keep working.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import openai

from neurodb.assistant import llm

Timeout = openai.Timeout
# Error codes of OpenAI's safety checks: the question is declined, and asking again will not help.
POLICY_BLOCKS = frozenset({"invalid_prompt", "bio_policy", "cyber_policy", "misalignment_policy_violation"})

ERRORS = (
    # most specific first (APITimeoutError is an APIConnectionError; status errors are APIErrors)
    (openai.AuthenticationError, llm.KEY_REJECTED),
    (openai.PermissionDeniedError, llm.NOT_ALLOWED),
    (openai.NotFoundError, llm.NO_MODEL),
    (openai.RateLimitError, llm.BUSY),
    (openai.BadRequestError, llm.NOT_PROCESSED),
    (openai.APITimeoutError, llm.LATE),
    (openai.APIConnectionError, llm.UNREACHABLE),
    (openai.APIStatusError, llm.UNAVAILABLE),
    (openai.APIError, llm.UNAVAILABLE),  # error mid-stream
)

# The same conditions as the HTTP errors above, when they end a response inside the stream.
STREAM_ERRORS = {
    "insufficient_quota": llm.NO_CREDIT,
    "rate_limit_exceeded": llm.BUSY,
    "slow_down": llm.BUSY,
    "server_is_overloaded": llm.BUSY,
}


def client() -> openai.OpenAI:
    if not llm.enabled() or not (key := llm.openai_key()):
        raise llm.AssistantUnavailable("The AI assistant is not configured.")
    # A plain number would also be the connect timeout: an unreachable API would then hold the
    # request for 3 x 120 s. Each model call is further held to the time left (agent._within).
    return openai.OpenAI(
        api_key=key,
        timeout=Timeout(llm.READ_TIMEOUT, connect=llm.CONNECT_TIMEOUT),
        max_retries=2,
    )


def friendly(exc: BaseException) -> str | None:
    """The message for one of the openai SDK's errors (``llm.friendly`` covers the rest)."""
    if isinstance(exc, openai.RateLimitError) and exc.code == "insufficient_quota":
        return llm.NO_CREDIT  # a 429 that retrying will not fix: the account needs credit
    return next((m for cls, m in ERRORS if isinstance(exc, cls)), None)


def policy_block(exc: BaseException) -> bool:
    """Blocked by OpenAI's safety checks, in the stream or as an HTTP error: the question is declined."""
    return (
        isinstance(exc, (llm.ServiceError, openai.APIError)) and getattr(exc, "code", None) in POLICY_BLOCKS
    )


def is_quota(exc: BaseException) -> bool:
    """The OpenAI credit ran out: ``insufficient_quota`` (as an error code or type, also when the
    stream ended with it), or a 429 whose message says quota (a plain 429 is the service being busy)."""
    for name in ("code", "type"):
        if str(getattr(exc, name, "") or "") == "insufficient_quota":
            return True
    too_many = isinstance(exc, openai.RateLimitError) or getattr(exc, "status_code", None) == 429
    return too_many and "quota" in str(exc).lower()


SAMPLING = ("temperature", "top_p")  # the sampling parameters Monitoring insights may send
_REFUSAL_CODES = ("unsupported_parameter", "unsupported_value")
_REFUSAL_WORDS = ("not supported", "unsupported")


def _named(text: str) -> str | None:
    """The sampling parameter a message names first ("top_p", "top-p", "temperature")."""
    lowered = text.lower()
    found = [
        (lowered.find(form), parameter)
        for parameter, forms in (("temperature", ("temperature",)), ("top_p", ("top_p", "top-p")))
        for form in forms
        if form in lowered
    ]
    return min(found)[1] if found else None


def unsupported_param(exc: BaseException) -> str | None:
    """``"temperature"`` or ``"top_p"`` when ``exc`` is the API refusing that parameter (a 400 whose
    ``param`` is it, or whose code or message says the parameter is not supported and names it); None
    for anything else. A parameter is never taken as refused unless the API named it."""
    if not isinstance(exc, openai.BadRequestError):
        return None
    param = str(getattr(exc, "param", "") or "").strip()
    if param in SAMPLING:
        return param
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    if code in _REFUSAL_CODES or any(word in message.lower() for word in _REFUSAL_WORDS):
        return _named(f"{param} {message}")
    return None


def effort_refused(exc: BaseException) -> bool:
    """A 400 about the reasoning effort: the model does not take the one sent."""
    if not isinstance(exc, openai.BadRequestError):
        return False
    param = str(getattr(exc, "param", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    return param.startswith("reasoning") or "reasoning.effort" in message


# ------------------------------------------------------------------------------------- the calls
def _part(part: llm.Text | llm.Pdf) -> dict[str, Any]:
    if isinstance(part, llm.Pdf):
        encoded = base64.b64encode(part.data).decode("ascii")
        return {
            "type": "input_file",
            "filename": part.filename,
            "file_data": f"data:application/pdf;base64,{encoded}",
        }
    return {"type": "input_text", "text": part.text}


def _input(request: llm.Request) -> str | list[dict[str, Any]]:
    if isinstance(request.user, str):
        if not request.as_message:
            return request.user
        return [{"role": "user", "content": request.user}]
    return [{"role": "user", "content": [_part(part) for part in request.user]}]


def _refusal(response: Any) -> bool:
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return True
    return False


class Connection:
    """OpenAI, ready for calls: ``agent.client()``, held to ``timeout`` seconds and ``retries`` retries when
    they are given (as plain values, as every feature has always passed them)."""

    provider = llm.OPENAI

    def __init__(self, timeout: float | None = None, retries: int | None = None) -> None:
        from neurodb.assistant import agent

        api = agent.client()
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        if retries is not None:
            options["max_retries"] = retries
        self.api = api.with_options(**options) if options else api

    def render(self, request: llm.Request) -> dict[str, Any]:
        """The Responses API request for ``request``: exactly the dict each feature sends today."""
        params: dict[str, Any] = {"model": llm.model_for(request.model, llm.OPENAI)}
        if request.system:
            params["instructions"] = request.system
        params["input"] = _input(request)
        params["reasoning"] = {"effort": request.effort}
        params["max_output_tokens"] = request.max_output_tokens
        params["store"] = False
        if request.cache_key:
            params["prompt_cache_key"] = request.cache_key
        if request.schema is not None:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                }
            }
        if request.user_id:
            params["safety_identifier"] = request.user_id
        params.update({name: float(value) for name, value in request.sampling})
        return params

    def read(self, response: Any, request: llm.Request) -> llm.Result:
        """A response as a Result, read through ``getattr`` (scripted fakes are partial). The text is the
        output_text whatever the status, as the features have always read it."""
        from neurodb.assistant import usage

        text = getattr(response, "output_text", "") or ""
        status = getattr(response, "status", None)
        stop, reason = "ok", ""
        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = str(getattr(details, "reason", "") or "")
            stop = {"content_filter": "refused", "max_output_tokens": "max_tokens"}.get(reason, "incomplete")
        elif _refusal(response):
            stop, reason = "refused", "refusal"
        elif status == "failed":
            error = getattr(response, "error", None)
            stop, reason = "failed", str(getattr(error, "code", "") or "failed")
        parsed = None
        if request.schema is not None:
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
        prompt, cached, output = usage.split(getattr(response, "usage", None))
        used = llm.Usage(input_tokens=prompt, cache_read_tokens=cached, output_tokens=output)
        model = llm.model_for(request.model, llm.OPENAI)
        return llm.Result(
            text=text,
            parsed=parsed,
            stop=stop,
            reason=reason,
            model=model,
            served_by="",
            usage=used,
            provider=llm.OPENAI,
            raw=response,
            by_model={model: used},  # one row of the model asked for, as every feature has booked it
            model_calls={model: 1},
        )

    def complete(self, request: llm.Request) -> llm.Result:
        return self.read(self.api.responses.create(**self.render(request)), request)

    def check(self) -> llm.Result:
        """The connection test's call: one short input and 16 output tokens at most; any status counts
        (a reasoning model may spend them all before it writes)."""
        model = llm.default_model(llm.OPENAI)
        response = self.api.responses.create(
            model=model,
            input=llm.PROBE,
            max_output_tokens=16,
            store=False,
            reasoning={"effort": "low"},
        )
        return self.read(response, llm.Request(model=model))
