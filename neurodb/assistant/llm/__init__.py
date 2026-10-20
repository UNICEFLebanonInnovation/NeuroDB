"""The AI behind every AI feature: Claude (Anthropic's model, through the Messages API; the preferred one)
or ChatGPT (OpenAI's GPT model, through the Responses API), chosen in configuration.

Which provider runs, first match: ``AI_PROVIDER`` when the hosting team set it to ``claude`` or
``openai``; else the administrators' choice in the admin's AI settings; else (Automatic) Claude when an
Anthropic key is available, from the environment or entered in AI settings, and OpenAI otherwise. A key in
the environment comes before one entered in AI settings (sealed with the site's secret key, :mod:`.keys`);
a model or default effort chosen in AI settings comes before ``CLAUDE_MODEL``, ``CLAUDE_EFFORT``,
``AI_ASSISTANT_MODEL`` and ``AI_ASSISTANT_EFFORT``.

A caller describes a model call once (:class:`Request`) and sends it through a connection to the provider
in use (:func:`connect`), which renders it for that provider and reads the answer back as a
:class:`Result`. Only :mod:`.openai_backend` imports the openai SDK and only :mod:`.claude_backend` the
anthropic one; this module imports either only inside a function.

The AI settings row is read through a snapshot of its values that are not secret (:func:`snapshot`), kept
in the shared cache and, in front of it, for 10 seconds in each process: a page asks the database nothing,
and a save clears both, so the saving worker follows at once and the others within 10 seconds. No restart
is needed when a key, a provider or a model is changed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Protocol

from django.conf import settings
from django.core.cache import caches
from django.core.signals import setting_changed
from django.db import DatabaseError, transaction
from django.db.models.signals import post_delete, post_save
from django.utils.crypto import salted_hmac

from . import keys

logger = logging.getLogger(__name__)

CLAUDE, OPENAI = "claude", "openai"
AUTO = "auto"
CONNECT_TIMEOUT = 10.0  # seconds to reach the API: an unreachable API fails fast
READ_TIMEOUT = 120.0  # longest silence in a stream (the model may reason for a while before it writes)
MEMO_SECONDS = 10.0  # how long a process keeps the AI settings it read
SNAPSHOT_SECONDS = 300  # how long the shared cache keeps them (a save clears it at once)

# What the person reads when a call fails, whichever provider made it.
NOT_SET_UP = "The AI assistant is not set up. Ask an administrator to add the API key."
KEY_REJECTED = "The AI assistant's API key was rejected. Ask an administrator."
NOT_ALLOWED = "The AI assistant's API key is not allowed to use this model."
NO_MODEL = "The AI assistant's model is not available. Ask an administrator."
BUSY = "The AI service is busy right now. Please try again in a minute."
NO_CREDIT = "The AI service's credit for this application has run out. Ask an administrator."
NOT_PROCESSED = "The AI service could not process this question."
LATE = "The AI service did not answer in time. Please try again."
UNREACHABLE = "The AI service could not be reached. Please try again."
UNAVAILABLE = "The AI service is temporarily unavailable. Please try again."
UNFINISHED = "The AI service could not finish this answer. Please try again."


class AssistantUnavailable(Exception):
    """The AI is switched off or has no API key."""


class ServiceError(Exception):
    """The model service ended a response with an error: a failed response, an error event in the
    stream, or a stream that stopped before the response was complete."""

    def __init__(self, code: str | None, message: str = "") -> None:
        self.code = code or "error"
        self.message = message
        super().__init__(f"{self.code}: {message}" if message else self.code)


# ------------------------------------------------------------------------------------- a model call
@dataclass(frozen=True)
class Text:
    """A text part of the user's content."""

    text: str


@dataclass(frozen=True)
class Pdf:
    """A PDF sent whole (the country programme document)."""

    filename: str
    data: bytes


@dataclass(frozen=True)
class Request:
    """One model call, described for either provider (each renders it its own way).

    - ``system``: the instructions; "" for none;
    - ``user``: the user's content, one text or parts in order;
    - ``as_message`` (OpenAI only): a text goes inside one user message; False sends the bare input
      string, as the daily review, the What's new note and the Watch note always have;
    - ``schema``: the JSON schema of the answer (strict), and ``schema_name`` OpenAI's name for it;
    - ``effort``: in the caller's terms (any of ``AI_ASSISTANT_EFFORTS``; Claude maps it, ``effort_for``);
    - ``max_output_tokens``: per call (Claude takes at least 2,000: its thinking counts too);
    - ``model``: what the caller asks for, resolved with :func:`model_for`;
    - ``cache_key``: OpenAI's prompt cache key; on Claude it marks the instructions for caching;
    - ``user_id``: ``agent.safety_identifier(user)``, only for a call a person asked for;
    - ``sampling``: (name, value) pairs sent to OpenAI only (Claude Opus 5.5 refuses them). A call through
      ``sampling.complete`` leaves it empty: the guard adds the plan's parameters itself.
    """

    system: str = ""
    user: str | tuple[Text | Pdf, ...] = ""
    as_message: bool = True
    schema: dict[str, Any] | None = None
    schema_name: str = ""
    effort: str = "low"
    max_output_tokens: int = 4000
    model: str = ""
    cache_key: str = ""
    user_id: str | None = None
    sampling: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Usage:
    """The tokens of one call (or of an answer's calls), counted the same way for both providers, with the
    attribute names ``usage.split`` reads: ``input_tokens + cache_read_tokens`` is the whole prompt."""

    input_tokens: int = 0  # prompt tokens not read from the cache (Claude: those written to it included)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0  # of input_tokens, those written to the cache (Claude only; 0 on OpenAI)
    output_tokens: int = 0  # the reasoning (OpenAI) or thinking (Claude) included

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass
class Result:
    """A model call's answer, read the same way whichever provider wrote it.

    ``stop`` is ``ok``, ``max_tokens``, ``refused``, ``incomplete`` or ``failed`` (OpenAI only), and
    ``reason`` its detail (OpenAI's incomplete reason, "refusal: <category>", Claude's stop reason). On
    Claude a refused answer has no text (a partial answer is never used); on OpenAI ``text`` is always the
    response's output_text, whatever its status, as the callers read it."""

    text: str
    parsed: Any  # with a schema: the JSON answer when the text parses, else None
    stop: str
    reason: str
    model: str  # the model asked for (after model_for)
    served_by: str  # the model that answered when a refusal fallback answered, else ""
    usage: Usage
    provider: str
    raw: Any = None  # the SDK's response (tests, logs); never stored

    @property
    def complete(self) -> bool:
        return self.stop == "ok"

    @property
    def ledger_model(self) -> str:
        return self.served_by or self.model


class Connection(Protocol):
    """The provider in use, ready for calls (:func:`connect`)."""

    provider: str

    def complete(self, request: Request) -> Result: ...


def connect(timeout: float | None = None, retries: int | None = None, p: str | None = None) -> Connection:
    """The provider in use (or ``p``), ready for calls held to ``timeout`` seconds each with ``retries``
    retries (None: the client's own, 120 s of silence, 10 s to connect and 2 retries). Raises
    AssistantUnavailable when the AI is off or that provider has no key."""
    if (p or provider()) == CLAUDE:
        from . import claude_backend

        return claude_backend.Connection(timeout, retries)
    from . import openai_backend

    return openai_backend.Connection(timeout, retries)


def complete(request: Request, timeout: float | None = None, retries: int | None = None) -> Result:
    """One call on the provider in use: ``connect(timeout, retries).complete(request)``."""
    return connect(timeout, retries).complete(request)


# ------------------------------------------------------------------------------- the AI settings row
@dataclass(frozen=True)
class Snapshot:
    """The values of the AI settings row that are not secret (the defaults when there is none): the
    choices, whether each key is entered and can be read with the site's secret key, and its end."""

    provider: str = AUTO
    claude_model: str = ""
    claude_effort: str = ""
    claude_fallbacks: bool = True
    openai_model: str = ""
    openai_effort: str = ""
    anthropic_entered: bool = False
    anthropic_readable: bool = False
    anthropic_last4: str = ""
    openai_entered: bool = False
    openai_readable: bool = False
    openai_last4: str = ""


_memo: tuple[float, Snapshot] | None = None
_pinned = False  # the tests' memo never expires (tests/conftest.py)


def _cache_key() -> str:
    # The secret key's fingerprint: after a change of SECRET_KEY the snapshot is read again (whether the
    # sealed keys still open depends on it).
    return "neurodb.ai-settings." + salted_hmac("neurodb.ai-settings", "v1").hexdigest()[:12]


def _read() -> Snapshot:
    from neurodb.assistant.models import AISettings

    row = AISettings.objects.filter(pk=1).first()
    if row is None:
        return Snapshot()
    return Snapshot(
        provider=row.provider or AUTO,
        claude_model=row.claude_model.strip(),
        claude_effort=row.claude_effort.strip(),
        claude_fallbacks=row.claude_fallbacks,
        openai_model=row.openai_model.strip(),
        openai_effort=row.openai_effort.strip(),
        anthropic_entered=bool(row.anthropic_key_sealed),
        anthropic_readable=bool(keys.unseal(row.anthropic_key_sealed)),
        anthropic_last4=row.anthropic_key_last4,
        openai_entered=bool(row.openai_key_sealed),
        openai_readable=bool(keys.unseal(row.openai_key_sealed)),
        openai_last4=row.openai_key_last4,
    )


def _kept(value: Any) -> Snapshot | None:
    """A snapshot the shared cache held (a dict), or None; a field a later release dropped is ignored."""
    if not isinstance(value, dict):
        return None
    names = {f.name for f in fields(Snapshot)}
    try:
        return Snapshot(**{k: v for k, v in value.items() if k in names})
    except TypeError:
        return None


def snapshot() -> Snapshot:
    """The AI settings as this process knows them: its memo (10 s), else the shared cache, else the row.
    Never read at import or start-up. When the table cannot be read (a release's first page before its
    migration ran) it gives the defaults and keeps nothing."""
    global _memo
    now = time.monotonic()
    if _memo is not None and (_pinned or now - _memo[0] < MEMO_SECONDS):
        return _memo[1]
    shared = caches["shared"]
    key = _cache_key()
    try:
        found = _kept(shared.get(key))
    except Exception:  # the shared cache unreachable: the row still answers
        logger.warning("AI settings: the shared cache could not be read", exc_info=True)
        found = None
    if found is None:
        try:
            with transaction.atomic():  # a savepoint: a failed read leaves the caller's transaction usable
                found = _read()
        except DatabaseError:
            logger.warning("AI settings could not be read: the defaults apply for now", exc_info=True)
            return Snapshot()
        try:
            shared.set(key, asdict(found), SNAPSHOT_SECONDS)
        except Exception:
            logger.warning("AI settings: the shared cache could not be written", exc_info=True)
    _memo = (now, found)
    return found


def forget() -> None:
    """Drop the snapshot (this process's memo and the shared cache's copy): the next read takes the row."""
    global _memo
    _memo = None
    try:
        caches["shared"].delete(_cache_key())
    except Exception:
        logger.warning("AI settings: the shared cache could not be cleared", exc_info=True)


def _saved(sender, **kwargs) -> None:
    forget()
    # and once more when the save is committed: another worker may have read the row as it was in between
    transaction.on_commit(forget)
    from neurodb.indicators.services import navigation

    navigation.invalidate()  # the menu's Ask item follows the AI


def _setting_changed(setting, **kwargs) -> None:
    # Only the secret key changes what the snapshot holds (whether the sealed keys open); every other
    # setting is read live on each call.
    if setting.startswith("SECRET_KEY"):
        forget()


post_save.connect(_saved, sender="assistant.AISettings", dispatch_uid="neurodb.assistant.llm.saved")
post_delete.connect(_saved, sender="assistant.AISettings", dispatch_uid="neurodb.assistant.llm.deleted")
setting_changed.connect(_setting_changed, dispatch_uid="neurodb.assistant.llm.setting_changed")


# ------------------------------------------------------------------------------ provider and switch
def provider() -> str:
    """The provider every AI call runs on now: ``claude`` or ``openai``."""
    pinned = settings.AI_PROVIDER
    choice = pinned if pinned != AUTO else snapshot().provider
    if choice in (CLAUDE, OPENAI):
        return choice
    return CLAUDE if anthropic_available() else OPENAI


def chosen_by() -> str:
    """Where :func:`provider` comes from: ``hosting`` (AI_PROVIDER), ``admin`` (AI settings) or ``auto``."""
    if settings.AI_PROVIDER != AUTO:
        return "hosting"
    return "admin" if snapshot().provider in (CLAUDE, OPENAI) else AUTO


def anthropic_available() -> bool:
    """An Anthropic key: set by the hosting team, or entered in AI settings and readable."""
    return bool(settings.ANTHROPIC_API_KEY) or snapshot().anthropic_readable


def openai_available() -> bool:
    """An OpenAI key: set by the hosting team, or entered in AI settings and readable."""
    return bool(settings.OPENAI_API_KEY) or snapshot().openai_readable


def available(p: str) -> bool:
    return anthropic_available() if p == CLAUDE else openai_available()


def enabled() -> bool:
    """The AI is on: the hosting team's switch and a key in the environment (worked out at start-up as
    ``AI_ASSISTANT_ENABLED``; tests set it), or the switch and a readable key entered in AI settings for
    the provider in use. No query when the setting is True or the switch is off; else the snapshot."""
    if settings.AI_ASSISTANT_ENABLED:
        return True
    if not getattr(settings, "AI_ASSISTANT_SWITCH", True):
        return False
    # Only a key entered in AI settings counts here: one in the environment is already inside
    # AI_ASSISTANT_ENABLED, and a test that sets it False beside OPENAI_API_KEY means "off".
    entered = snapshot()
    return entered.anthropic_readable if provider() == CLAUDE else entered.openai_readable


def _entered_key(field: str) -> str:
    from neurodb.assistant.models import AISettings

    sealed = AISettings.objects.filter(pk=1).values_list(field, flat=True).first()
    return keys.unseal(sealed or "") or ""


def anthropic_key() -> str:
    """The Anthropic key to call with: the environment's, else the one entered in AI settings (one query,
    once per client built, never per page), else ""."""
    if settings.ANTHROPIC_API_KEY:
        return settings.ANTHROPIC_API_KEY
    return _entered_key("anthropic_key_sealed") if snapshot().anthropic_readable else ""


def openai_key() -> str:
    """The OpenAI key to call with, as :func:`anthropic_key` (no query when the environment sets it)."""
    if settings.OPENAI_API_KEY:
        return settings.OPENAI_API_KEY
    return _entered_key("openai_key_sealed") if snapshot().openai_readable else ""


# ------------------------------------------------------------------------------------ models, effort
def provider_of(model: str) -> str:
    """Whose model ``model`` is: Claude's ids start with "claude", every other id is OpenAI's."""
    return CLAUDE if (model or "").strip().lower().startswith("claude") else OPENAI


def default_model(p: str | None = None) -> str:
    """The model of the provider in use (or ``p``): the one chosen in AI settings, else the setting's."""
    p = p or provider()
    if p == CLAUDE:
        return snapshot().claude_model or settings.CLAUDE_MODEL
    return snapshot().openai_model or settings.AI_ASSISTANT_MODEL


def model_for(asked: str = "", p: str | None = None) -> str:
    """The model a call runs on: ``asked`` when it belongs to the provider in use (or ``p``), else that
    provider's default. A value equal to its provider's settings default (AI_ASSISTANT_MODEL,
    CLAUDE_MODEL) counts as the default, so a model chosen in AI settings replaces it. Nothing stored is
    rewritten: a prompt version that names an OpenAI model keeps it, and runs on Claude's while Claude
    is in use."""
    p = p or provider()
    asked = (asked or "").strip()
    settings_default = settings.CLAUDE_MODEL if p == CLAUDE else settings.AI_ASSISTANT_MODEL
    if not asked or asked == settings_default or provider_of(asked) != p:
        return default_model(p)
    return asked


def default_effort(p: str | None = None) -> str:
    """The default effort of the provider in use (or ``p``): AI settings', else CLAUDE_EFFORT or
    AI_ASSISTANT_EFFORT."""
    p = p or provider()
    if p == CLAUDE:
        return snapshot().claude_effort or settings.CLAUDE_EFFORT
    return snapshot().openai_effort or settings.AI_ASSISTANT_EFFORT


def effort_for(effort: str, p: str | None = None) -> str:
    """``effort`` as the provider in use (or ``p``) takes it: unchanged on OpenAI; on Claude a value it
    does not have (``none``, ``minimal``) runs as ``low``."""
    if (p or provider()) != CLAUDE:
        return effort
    return effort if effort in settings.CLAUDE_EFFORTS else "low"


# ------------------------------------------------------------------------------------------- names
@dataclass(frozen=True)
class Names:
    """How the pages and messages name a provider (templates: ``AI_PROVIDER.label`` and so on)."""

    label: str
    writer: str
    vendor: str
    api: str
    host: str
    credit: str
    key_setting: str


NAMES = {
    CLAUDE: Names(
        label="Claude (Anthropic)",
        writer="Claude (Anthropic's model)",
        vendor="Anthropic",
        api="Anthropic API",
        host="api.anthropic.com",
        credit="the Anthropic credit",
        key_setting="ANTHROPIC_API_KEY",
    ),
    OPENAI: Names(
        label="ChatGPT (OpenAI)",
        writer="ChatGPT (OpenAI's GPT model)",
        vendor="OpenAI",
        api="OpenAI API",
        host="api.openai.com",
        credit="the OpenAI credit",
        key_setting="OPENAI_API_KEY",
    ),
}


def names(p: str | None = None) -> Names:
    """The names of the provider in use (or ``p``)."""
    return NAMES[p or provider()]


def names_of(model: str) -> Names:
    """The names of the provider whose model ``model`` is (a note says who wrote it)."""
    return NAMES[provider_of(model)]


# ------------------------------------------------------------------------------------------ failures
def _sdk(exc: BaseException) -> str:
    """``openai`` or ``anthropic`` when ``exc`` is one of that SDK's errors, else ""."""
    for cls in type(exc).__mro__:
        root = cls.__module__.split(".")[0]
        if root in ("openai", "anthropic"):
            return root
    return ""


def friendly(exc: BaseException) -> str | None:
    """The message shown for a known failure of either provider, or None for an unexpected one."""
    if isinstance(exc, AssistantUnavailable):
        return NOT_SET_UP
    if isinstance(exc, ServiceError):
        if exc.code == "timeout":  # a Claude answer that went silent while it streamed
            return LATE
        from . import openai_backend

        return openai_backend.STREAM_ERRORS.get(exc.code, UNFINISHED)
    sdk = _sdk(exc)
    if sdk == "openai":
        from . import openai_backend

        return openai_backend.friendly(exc)
    if sdk == "anthropic":
        from . import claude_backend

        return claude_backend.friendly(exc)
    return None


def is_quota(exc: BaseException) -> bool:
    """The provider's credit ran out (retrying will not help until it is topped up): OpenAI's
    ``insufficient_quota``, Anthropic's ``billing_error`` (402) or a 400 about the credit balance."""
    if _sdk(exc) == "anthropic":
        from . import claude_backend

        return claude_backend.is_quota(exc)
    from . import openai_backend

    return openai_backend.is_quota(exc)


def unsupported_param(exc: BaseException) -> str | None:
    """``"temperature"`` or ``"top_p"`` when ``exc`` is OpenAI refusing that sampling parameter; None for
    anything else (Claude is never sent either, so none of its errors names one)."""
    if _sdk(exc) != "openai":
        return None
    from . import openai_backend

    return openai_backend.unsupported_param(exc)


def effort_refused(exc: BaseException) -> bool:
    """The API refused the effort sent (the model does not take it)."""
    if _sdk(exc) == "anthropic":
        from . import claude_backend

        return claude_backend.effort_refused(exc)
    if _sdk(exc) == "openai":
        from . import openai_backend

        return openai_backend.effort_refused(exc)
    return False


def api_message(exc: BaseException) -> str:
    """The API's own words in an error response (``error.message`` of its body), else the exception's."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return str(getattr(exc, "message", "") or exc)


# ------------------------------------------------------------------------------------- AI use ledger
def record(feature: str, model: str, outcome: Any) -> None:
    """Book an answer's model calls in the AI use ledger (``usage.record``, which never fails its caller).
    An answer whose tokens are kept per model (``by_model``, on Claude) gets one row per model that ran
    them, at that model's prices: the model asked for, and after a refusal fallback the fallback's model
    for its own attempt, each call counted on the model that wrote its message (``model_calls``). Any
    other answer (OpenAI's) is one row of ``model``, the model asked for, as always."""
    from neurodb.assistant import usage

    per_model = getattr(outcome, "by_model", None) or {}
    calls = getattr(outcome, "model_calls", None) or {}
    if not per_model and not calls:
        usage.record(feature, model, outcome, calls=getattr(outcome, "calls", 1))
        return
    for name in dict.fromkeys([*per_model, *calls]):
        usage.record(feature, name, per_model.get(name), calls=calls.get(name, 0))


# ----------------------------------------------------------------------------- test the connection
PROBE = "Reply with the word OK."  # all a connection test sends: no NeuroDB data
CHECK_CHARS = 300


@dataclass(frozen=True)
class Check:
    """What a connection test found (AI settings shows ``text`` and keeps it)."""

    ok: bool
    text: str
    model: str
    seconds: float


def test_connection(p: str | None = None) -> Check:
    """One tiny call to the provider in use (or ``p``) with no NeuroDB data, held to 30 seconds and no
    retry (it runs inside the admin's request). Any answer counts: the key, the model and the credit work.
    A failure gives the message a feature would show, with the API's own words for a refused request (a 400
    holds no secret). The tokens go to AI use as the connection test."""
    from neurodb.assistant import usage

    p = p or provider()
    model = default_model(p)
    started = time.monotonic()
    try:
        result = connect(timeout=30, retries=0, p=p).check()
    except Exception as exc:
        seconds = time.monotonic() - started
        text = friendly(exc) or f"The AI service could not be tested ({type(exc).__name__})."
        if getattr(exc, "status_code", None) == 400:
            text = f"{text} The API said: {api_message(exc)}"
            if p == CLAUDE and "anthropic-beta" in api_message(exc):
                text += " Untick Retry a declined answer on another Claude model and test again."
        logger.warning("AI settings: the connection test failed: %s", type(exc).__name__)
        return Check(False, text[:CHECK_CHARS], model, round(seconds, 1))
    seconds = time.monotonic() - started
    usage.record(usage.TEST, result.ledger_model, result.usage)
    text = (
        f"{names(p).label} answered with {result.ledger_model} in {seconds:.1f} s: the key, the model and "
        "the credit work."
    )
    return Check(True, text[:CHECK_CHARS], result.ledger_model, round(seconds, 1))
