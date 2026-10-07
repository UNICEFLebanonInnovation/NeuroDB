"""Temperature and top_p: sent only when a version sets them and the model accepts them.

Reasoning models may refuse sampling parameters with a 400. A refusal is kept in the database
(:class:`~neurodb.fmm.models.ModelCapability`, per model, effort and parameter), so every process knows
it, and the parameter is tried again after ``FMM_SAMPLING_RECHECK_DAYS``.

- :func:`plan` says what will be sent: per parameter ``not_set`` (the version leaves it empty), ``off``
  (``FMM_SAMPLING="off"``), ``known_rejected`` (refused recently) or ``sent``;
- :func:`call` makes the call. When the API refuses a parameter **it names**, that parameter alone is
  recorded as refused and dropped, and the call is made again with the other one still sent (at most two
  retries, one per parameter); its state becomes ``not_applied``. When the call succeeds, every parameter
  still sent is recorded as accepted and its state becomes ``applied``. A 400 that names no sampling
  parameter (or one that was not sent) is raised as it is, without a retry.

The chips of a brief and of a chat answer show these states ("temp 0.30 · not applied"), with
:func:`why` as their hover text. Ask NeuroDB and NeuroDB Watch never send either parameter.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import openai
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.formats import date_format

from ..models import ModelCapability

PARAMETERS = ("temperature", "top_p")
LABELS = {"temperature": "temperature", "top_p": "top-p"}
MAX_RETRIES = 2
DETAIL_CHARS = 300
_REFUSAL_CODES = ("unsupported_parameter", "unsupported_value")
_REFUSAL_WORDS = ("not supported", "unsupported")

# The state of one parameter
NOT_SET, OFF, KNOWN_REJECTED, SENT = "not_set", "off", "known_rejected", "sent"
APPLIED, NOT_APPLIED = "applied", "not_applied"


@dataclass(frozen=True)
class Plan:
    """What one call will send: ``params`` (the parameters and their values) and, per parameter, its
    state (``not_set``, ``off``, ``known_rejected`` or ``sent``)."""

    params: dict[str, float] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)


def known_rejection(model: str, effort: str, parameter: str, now: datetime.datetime | None = None):
    """The record that ``model`` at ``effort`` refused ``parameter``, while it still holds (checked within
    ``FMM_SAMPLING_RECHECK_DAYS``); None when it was accepted, never tried, or refused too long ago."""
    now = now or timezone.now()
    since = now - datetime.timedelta(days=settings.FMM_SAMPLING_RECHECK_DAYS)
    return ModelCapability.objects.filter(
        model=model, effort=effort, parameter=parameter, accepted=False, checked_at__gte=since
    ).first()


def plan(version, model: str, effort: str, now: datetime.datetime | None = None) -> Plan:
    """What a call of ``version`` with ``model`` at ``effort`` will send (see the module's notes)."""
    params: dict[str, float] = {}
    states: dict[str, str] = {}
    wanted = [p for p in PARAMETERS if getattr(version, p, None) is not None]
    rejected: set[str] = set()
    if wanted and settings.FMM_SAMPLING != "off":  # the refusals that still hold, in one query
        now = now or timezone.now()
        since = now - datetime.timedelta(days=settings.FMM_SAMPLING_RECHECK_DAYS)
        rejected = set(
            ModelCapability.objects.filter(
                model=model, effort=effort, parameter__in=wanted, accepted=False, checked_at__gte=since
            ).values_list("parameter", flat=True)
        )
    for parameter in PARAMETERS:
        value = getattr(version, parameter, None)
        if value is None:
            states[parameter] = NOT_SET
        elif settings.FMM_SAMPLING == "off":
            states[parameter] = OFF
        elif parameter in rejected:
            states[parameter] = KNOWN_REJECTED
        else:
            states[parameter] = SENT
            params[parameter] = float(Decimal(str(value)))
    return Plan(params=params, states=states)


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
    if param in PARAMETERS:
        return param
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    if code in _REFUSAL_CODES or any(word in message.lower() for word in _REFUSAL_WORDS):
        return _named(f"{param} {message}")
    return None


def record(model: str, effort: str, parameter: str, accepted: bool, detail: str = "") -> None:
    """Keep whether ``model`` at ``effort`` accepted ``parameter`` (one row per three, updated)."""
    values = {"accepted": accepted, "checked_at": timezone.now(), "detail": str(detail or "")[:DETAIL_CHARS]}
    key = {"model": (model or "")[:64], "effort": (effort or "")[:8], "parameter": parameter}
    for attempt in range(2):  # two processes recording the first row at once: the second updates it
        try:
            with transaction.atomic():
                ModelCapability.objects.update_or_create(**key, defaults=values)
            return
        except IntegrityError:
            if attempt:
                raise


def call(api, request: dict[str, Any], plan: Plan, model: str, effort: str) -> tuple[Any, dict[str, str]]:
    """``api.responses.create(**request, **sent parameters)``, retried without a parameter the API refuses
    (see the module's notes). Returns the response and each parameter's final state."""
    params = dict(plan.params)
    states = dict(plan.states)
    retries = 0
    while True:
        try:
            response = api.responses.create(**request, **params)
        except openai.BadRequestError as exc:
            parameter = unsupported_param(exc)
            if parameter is None or parameter not in params or retries >= MAX_RETRIES:
                raise
            record(model, effort, parameter, accepted=False, detail=str(getattr(exc, "message", "") or exc))
            del params[parameter]
            states[parameter] = NOT_APPLIED
            retries += 1
            continue
        for parameter in params:
            record(model, effort, parameter, accepted=True)
            states[parameter] = APPLIED
        return response, states


def why(parameter: str, state: str, model: str, effort: str) -> str:
    """The hover text of a parameter's chip."""
    label = LABELS.get(parameter, parameter)
    if state in (NOT_APPLIED, KNOWN_REJECTED):
        row = ModelCapability.objects.filter(model=model, effort=effort, parameter=parameter).first()
        day = date_format(timezone.localtime(row.checked_at), "j M Y") if row else ""
        checked = f" (checked {day})" if day else ""
        return (
            f"OpenAI refused {label} for {model} at effort {effort}{checked}. It was written without it. "
            "An administrator can clear this check in the admin (Sampling checks)."
        )
    if state == NOT_SET:
        return f"The prompt version does not set {label}; the API's default applies."
    if state == OFF:
        return f"Sampling parameters are switched off (FMM_SAMPLING=off); {label} was not sent."
    if state == APPLIED:
        return f"{label.capitalize()} was sent and accepted."
    return ""


def summary(version, states: dict[str, str], model: str, effort: str) -> dict[str, dict[str, Any]]:
    """Per parameter, what was asked, what happened and why, as a brief or a chat answer keeps it
    (``Insight.sampling``): ``{"temperature": {"asked": 0.3, "state": "not_applied", "why": "…"}}``."""
    out = {}
    for parameter in PARAMETERS:
        value = getattr(version, parameter, None)
        state = states.get(parameter, NOT_SET)
        out[parameter] = {
            "asked": None if value is None else float(Decimal(str(value))),
            "state": state,
            "why": why(parameter, state, model, effort),
        }
    return out
