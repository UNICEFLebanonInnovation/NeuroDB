"""The AI monitoring brief of a filter: written in a background process, checked, kept, shown.

The brief is one call without tools whose answer must follow the strict JSON schema built from the
parts its prompt version lists (:mod:`.sections`, like FMS's insight sections): each part a list of
sentences (a paragraph or bullets), each sentence with the keys of the facts it rests on, and the
priority action points, structured, with fixed priorities and timeframes. The schema has no ``$defs``
and no ``maxItems`` (strict mode refuses them); the most sentences or bullets of each part are the
version's, enforced in code. :data:`SCHEMA_VERSION` is part of every prompt version's content hash.

**Where it runs.** A person's Regenerate and an administrator's test run never call the AI inside a web
request: :func:`start` inserts a ``running`` brief (one per filter and version: a second click gets the
same row) and, once that is committed, starts ``manage.py fmm_insights --insight <pk>`` in the
background, which calls :func:`generate` for that row; the page polls it every 3 seconds. The nightly
job (:func:`write_nightly`) calls :func:`generate` itself. A ``running`` row older than the call's
time limit plus a minute is taken as stopped (:func:`expire_stale`).

**What generate does** (it never raises): the gates (switched on, not paused, within the caps, the
version's brief switched on, at least ``FMM_MIN_VISITS_FOR_AI`` visits, the person's quota), the facts
(:mod:`.facts`); a brief already written from the very same input is reused without a call; the last
privacy check (``privacy.assert_clean``); the call (``sampling.call``, recorded in the AI use ledger);
then the checks of every sentence and action (:func:`validate`, ``watch.grounding``). What passes is
kept; when nothing passes, the code-written brief (:mod:`.fallback`) is kept instead.

**What the page shows** (:func:`current`): the newest brief of the filter under the published version
("Up to date", or "the data has changed since"), else one under an older version, else the
code-written brief of the figures, built on the spot and not kept.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any

import openai
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import QueryDict
from django.utils import timezone
from django.utils.formats import date_format

from neurodb.assistant import usage
from neurodb.integrations import background
from neurodb.integrations.background import FMM_INSIGHTS_LOCK_ID
from neurodb.watch import grounding, people, redact

from .. import privacy
from ..models import Insight, PromptVersion
from . import FMM_NAMED_FIELDS, budget, profiles, prompts, sampling
from . import sections as sections_module

logger = logging.getLogger(__name__)

LOCK_ID = FMM_INSIGHTS_LOCK_ID  # 7_140_433, the nightly briefs
SCHEMA_VERSION = 2  # 2: the parts of the brief are the version's (sections), action points name a partner

PRIORITIES = sections_module.PRIORITIES
TIMEFRAMES = sections_module.TIMEFRAMES
CACHE_KEY = "neurodb-fmm-insights"
FORMAT_NAME = "fmm_brief"
ACTION_CHARS = 300
OWNER_CHARS = 80
PARTNER_CHARS = 120
OWNER_DEFAULT = "Section lead"
SECTION_DEFAULT = "All sections"
SECTIONS_ALWAYS = ("Field operations", "All sections")
ROUGH_PAYLOAD_TOKENS = 6000  # the payload's tokens before the facts are built (the cheap gate)
STALE_AFTER = 60  # seconds past the call's time limit after which a running brief is taken as stopped
FAILURES_IN_A_ROW = 3  # the nightly run stops after this many failed briefs in a row
REFUSED_KEPT_DAYS = 30  # refused and skipped briefs are deleted after this many days

# Reasons kept with a brief that was not written (``Insight.reason``)
OFF = budget.REASONS[budget.OFF]
DISABLED = "switched off by an administrator"
TOO_FEW = "Too few visits in this filter for an AI brief ({n} needed)."
UP_TO_DATE = "up to date"
STOPPED = "stopped"
PRIVACY = "privacy"
CUT_OFF = "cut off"
REFUSED = "refused"
MALFORMED = "malformed"
QUOTA = "quota"
NOTHING_KEPT = "The AI's sentences did not pass the checks"
BUSY_YOURS = "another brief of yours is being written"
ERROR = "error"
NOT_YET = "no brief has been written for this filter yet"

PUBLISHED = object()  # "the published version, read here" (a version argument not given)

KINDS = {Insight.Trigger.USER: "user", Insight.Trigger.NIGHTLY: "nightly", Insight.Trigger.TEST: "test"}
SHOWN = (Insight.Status.OK, Insight.Status.PARTIAL, Insight.Status.FALLBACK)
WRITTEN = (Insight.Status.OK, Insight.Status.PARTIAL)


# ------------------------------------------------------------------------------------------ the scope
def scope_of(canonical: dict[str, Any], today: datetime.date | None = None):
    """The :class:`~neurodb.fmm.scope.Scope` a brief's ``scope`` (``Scope.canonical()``) describes: the
    same filters, the preset's period as of ``today``. Its hash is the brief's ``scope_hash``."""
    from ..scope import Scope, period

    today = today or timezone.localdate()
    preset = canonical.get("preset") or "this_year"
    year = canonical.get("year")
    if preset == "custom":
        start = datetime.date.fromisoformat(canonical["from"])
        end = datetime.date.fromisoformat(canonical["to"])
    else:
        start, end = period(preset, today, year)
    return Scope(
        preset=preset,
        start=start,
        end=end,
        year=year if preset == "year" else None,
        sections=tuple(canonical.get("sections") or ()),
        governorate=canonical.get("governorate") or "",
        offices=tuple(canonical.get("offices") or ()),
        partners=tuple(int(p) for p in canonical.get("partners") or ()),
        pd=canonical.get("pd"),
        entity_types=tuple(canonical.get("entity_types") or ()),
        ratings=tuple(canonical.get("ratings") or ()),
        statuses=tuple(canonical.get("statuses") or ()),
        modalities=tuple(canonical.get("modalities") or ()),
        quality_bands=tuple(canonical.get("quality_bands") or ()),
        urgency_levels=tuple(canonical.get("urgency_levels") or ()),
        programmatic=bool(canonical.get("programmatic")),
        q=canonical.get("q") or "",
        drill=tuple((k, v) for k, v in canonical.get("drill") or ()),
        empty=bool(canonical.get("empty")),
    )


def _refresh_state() -> tuple[int, datetime.datetime | None]:
    """The rules version the visits are scored with, and when the last refresh finished."""
    from .. import status

    last = status.last_refresh()
    if last is None:
        return 0, None
    return int((last.details or {}).get("rules_version") or 0), last.finished_at


# ------------------------------------------------------------------------------------------ the request
def request(facts, version: PromptVersion, user=None) -> dict[str, Any]:
    """The call's parameters: the whole prompt, the facts as JSON, the strict format; nothing stored at
    OpenAI, its own prompt cache key, and a safety identifier only for a brief someone asked for."""
    from neurodb.assistant import agent

    from .facts import dump

    out = {
        "model": profiles.model_of(version),
        "instructions": profiles.compose(version, "insights"),
        "input": [{"role": "user", "content": dump(facts.payload)}],
        "reasoning": {"effort": version.effort},
        "max_output_tokens": version.max_output_tokens,
        "store": False,
        "prompt_cache_key": CACHE_KEY,
        "text": {
            "format": {
                "type": "json_schema",
                "name": FORMAT_NAME,
                "strict": True,
                "schema": sections_module.schema(sections_module.of(version)),
            }
        },
    }
    identifier = agent.safety_identifier(user) if user is not None else None
    if identifier:
        out["safety_identifier"] = identifier
    return out


# ------------------------------------------------------------------------------------------ the gates
def _rough_estimate(version: PromptVersion) -> int:
    instructions = prompts.compose(version, "insights")
    return int(len(instructions) / budget.CHARS_PER_TOKEN) + ROUGH_PAYLOAD_TOKENS + version.max_output_tokens


def _visits(scope) -> int:
    """The visits of ``scope`` (the key figure, kept ten minutes per refresh)."""
    from .. import metrics

    return metrics.kpis(scope)["visits"]


def _user(user):
    return user if getattr(user, "pk", None) else None


def gate(
    scope, version: PromptVersion, user=None, trigger: str = Insight.Trigger.USER, insight=None
) -> tuple[str, str] | None:
    """Why a brief of ``scope`` may not be written now, as ``(status, reason)``; None when it may.
    The cheap gates, in order: switched on, not paused and within the caps (``budget.allowed``), the
    version's brief switched on, enough visits, and for a person's Regenerate their quota and no other
    brief of theirs being written (``insight``, their own running row, does not count)."""
    user = _user(user)
    published = version if version.status == PromptVersion.Status.PUBLISHED else None
    ok, why = budget.allowed(KINDS.get(trigger, "user"), user, _rough_estimate(version), version=published)
    if not ok:
        return (Insight.Status.SKIPPED if why == budget.OFF else Insight.Status.LIMITED), budget.REASONS[why]
    if not version.insights_enabled:
        return Insight.Status.SKIPPED, DISABLED
    if _visits(scope) < settings.FMM_MIN_VISITS_FOR_AI:
        return Insight.Status.SKIPPED, TOO_FEW.format(n=settings.FMM_MIN_VISITS_FOR_AI)
    if trigger == Insight.Trigger.USER and user is not None:
        running = getattr(insight, "status", "") == Insight.Status.RUNNING
        own = getattr(insight, "pk", None) if running else None
        used, allowed = budget.quota("insights", user, version)
        if used - (1 if own else 0) >= allowed:
            return Insight.Status.LIMITED, f"{allowed} of {allowed} today — resets at midnight"
        others = Insight.objects.filter(created_by=user, status=Insight.Status.RUNNING)
        if own:
            others = others.exclude(pk=own)
        if others.exists():
            return Insight.Status.LIMITED, BUSY_YOURS
    return None


# ------------------------------------------------------------------------------------------ starting
def _new_row(scope, version: PromptVersion, user, trigger: str, status: str, reason: str = "") -> Insight:
    rules_version, as_of = _refresh_state()
    return Insight(
        scope_hash=scope.hash(),
        scope=scope.canonical(),
        scope_label=scope.label()[:300],
        trigger=trigger,
        version=version,
        rules_version=rules_version,
        input_hash="",
        data_as_of=as_of,
        status=status,
        reason=reason[:200],
        created_by=_user(user),
    )


def record_refusal(scope, version: PromptVersion, user, trigger: str, refused: tuple[str, str]) -> Insight:
    """Keep a brief a gate refused when someone pressed Regenerate (over the quota, the budget used,
    the AI off); too few visits keeps nothing (the row is returned unsaved)."""
    status_, reason = refused
    row = _new_row(scope, version, user, trigger, status_, reason)
    if not reason.startswith("Too few visits"):
        row.save()
    return row


def start(
    scope, version: PromptVersion | None = None, user=None, trigger: str = Insight.Trigger.USER
) -> Insight:
    """A brief of ``scope`` being written: the ``running`` row (the existing one when a brief of the same
    filter and version is already being written), and once it is committed, ``fmm_insights --insight
    <pk>`` started in the background. The gates are the caller's (:func:`gate`)."""
    version = version or profiles.published()
    if version is None:
        raise ValueError("No prompt version is published.")
    expire_stale()
    for _attempt in range(2):
        try:
            with transaction.atomic():
                row = _new_row(scope, version, user, trigger, Insight.Status.RUNNING)
                row.save()
        except IntegrityError:  # another click got there first: poll its row
            found = Insight.objects.filter(
                scope_hash=scope.hash(), version=version, status=Insight.Status.RUNNING
            ).first()
            if found is not None:
                return found
            continue  # it finished in between: start a new one
        who = f"{'admin' if trigger == Insight.Trigger.TEST else 'user'}:{getattr(user, 'pk', '') or ''}"
        transaction.on_commit(
            partial(background.start_command, "fmm_insights", "--insight", str(row.pk), "--triggered-by", who)
        )
        return row
    raise RuntimeError("The brief could not be started.")


def stale_before(now: datetime.datetime | None = None) -> datetime.datetime:
    """Running briefs started before this are taken as stopped."""
    now = now or timezone.now()
    return now - datetime.timedelta(seconds=settings.FMM_INSIGHTS_TIMEOUT_SECONDS + STALE_AFTER)


def expire_stale(now: datetime.datetime | None = None) -> int:
    """Close the briefs left ``running`` by a process that stopped (older than the call's time limit
    plus a minute): failed, reason "stopped". They use no quota."""
    return Insight.objects.filter(status=Insight.Status.RUNNING, created_at__lt=stale_before(now)).update(
        status=Insight.Status.FAILED, reason=STOPPED, called=False
    )


# ------------------------------------------------------------------------------------------ checking
def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _known_partners(facts) -> dict[str, str]:
    """The partners the facts name (the partner breakdown and the visit cards), folded -> as written."""
    names: dict[str, str] = {}
    for entry in (facts.payload.get("partners") or {}).values():
        if entry.get("name"):
            names.setdefault(_fold(entry["name"]), entry["name"])
    for card in (facts.payload.get("visits") or {}).values():
        if card.get("partner"):
            names.setdefault(_fold(card["partner"]), card["partner"])
    return names


def _known_sections(facts) -> set[str]:
    names = {_fold(s) for s in SECTIONS_ALWAYS}
    for group in ("sections", "offices"):
        for entry in (facts.payload.get(group) or {}).values():
            names.add(_fold(entry.get("name")))
    return names


def _owner_ok(owner: str, names_: frozenset[str]) -> bool:
    """An owner role may be kept: short, a role and not a person, and no figure above 10."""
    from neurodb.review.services import numbers_in

    if not owner or len(owner) > OWNER_CHARS:
        return False
    if people.EMAIL.search(owner) or redact.LINK.search(owner) or grounding.MARKUP_IN_TEXT.search(owner):
        return False
    if people.mentions(owner, names_) or privacy.HONORIFIC_NAME.search(owner):
        return False
    if grounding.SYSTEM_WORDS.search(owner):
        return False
    return not any(float(n) > 10 for n in numbers_in(owner))


def _check_action(
    entry: Any, facts, today, names_, dropped: Counter, known: set[str], partners: dict[str, str]
) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("keys"), list):
        dropped[grounding.MALFORMED] += 1
        return None
    keys = list(dict.fromkeys(k for k in entry["keys"] if isinstance(k, str) and k))
    if not keys:
        dropped[grounding.NO_KEYS] += 1
        return None
    if any(k not in facts.citable for k in keys):
        dropped[grounding.UNKNOWN_KEY] += 1
        return None
    priority, timeframe = entry.get("priority"), entry.get("timeframe")
    if priority not in PRIORITIES or timeframe not in TIMEFRAMES:
        dropped[grounding.MALFORMED] += 1
        return None
    action = " ".join(str(entry.get("action") or "").split())
    if len(action) > ACTION_CHARS:
        dropped[grounding.TOO_LONG] += 1
        return None
    verdict = grounding.check(
        action, [facts.citable[k] for k in keys], today, names_, named_fields=FMM_NAMED_FIELDS
    )
    if not verdict:
        dropped[verdict.reason] += 1
        return None
    owner = " ".join(str(entry.get("owner_role") or "").split())
    if not _owner_ok(owner, names_):
        owner = OWNER_DEFAULT
        dropped["owner_replaced"] += 1
    section = " ".join(str(entry.get("section") or "").split())
    if _fold(section) not in known:
        section = SECTION_DEFAULT
        dropped["section_replaced"] += 1
    # the partner: one the facts name, written as they write it; any other text is left out
    partner = " ".join(str(entry.get("partner") or "").split())[:PARTNER_CHARS]
    if partner and _fold(partner) not in partners:
        partner = ""
        dropped["partner_removed"] += 1
    partner = partners.get(_fold(partner), "") if partner else ""
    return {
        "priority": priority,
        "section": section,
        "partner": partner,
        "action": action,
        "owner_role": owner,
        "timeframe": timeframe,
        "keys": keys,
    }


def _fold_visit_keys(entries: list, citable: dict[str, Any]) -> list:
    """The entries with a redundant visit key set aside: ``visit:1722`` when the visit was not sent in
    full but the same entry cites a note of that visit (``narr:1722:1``, whose ``visit`` it names). The
    entry is then checked against the note alone; a visit key cited on its own still has to be citable."""
    out = []
    for entry in entries:
        keys = entry.get("keys") if isinstance(entry, dict) else None
        if isinstance(keys, list):
            noted = {
                visit_key_of(k) for k in keys if isinstance(k, str) and k.startswith("narr:") and k in citable
            }
            kept = [
                k
                for k in keys
                if not (
                    isinstance(k, str)
                    and k.startswith("visit:")
                    and k not in citable
                    and visit_key_of(k) in noted
                )
            ]
            if len(kept) != len(keys):
                entry = {**entry, "keys": kept}
        out.append(entry)
    return out


def validate(
    raw: Any,
    facts,
    today: datetime.date | None = None,
    names_: frozenset[str] | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[dict]], list[dict], dict[str, int]]:
    """What may be kept of the AI's answer ``raw``, part by part (``parts``: the version's, the defaults
    when not given): each part's sentences that pass ``watch.grounding`` against the facts they cite (at
    most the part's ``max_items``), the priority action points whose keys exist and whose action passes
    the same checks (an owner naming a person, or a section the facts do not name, is replaced; a
    partner the facts do not name is left out), and why the rest was dropped (counts per reason)."""
    today = today or timezone.localdate()
    names_ = privacy.names() if names_ is None else names_
    parts = parts if parts is not None else sections_module.of(None)
    raw = raw if isinstance(raw, dict) else {}
    dropped: Counter = Counter()
    sections: dict[str, list[dict]] = {}
    for part in sections_module.text_parts(parts):
        name, limit = part["key"], part["max_items"]
        entries = raw.get(name)
        entries = _fold_visit_keys(entries if isinstance(entries, list) else [], facts.citable)
        kept, reasons = grounding.validate(
            entries, facts.citable, limit, today, names_, named_fields=FMM_NAMED_FIELDS
        )
        dropped.update(reasons)
        if len(entries) > limit:
            dropped["too_many"] += len(entries) - limit
        sections[name] = kept
    known = _known_sections(facts)
    partners = _known_partners(facts)
    actions = []
    part = sections_module.action_part(parts)
    if part is not None:
        limit = part["max_items"]
        entries = raw.get(part["key"])
        entries = _fold_visit_keys(entries if isinstance(entries, list) else [], facts.citable)
        for entry in entries[:limit]:
            kept_action = _check_action(entry, facts, today, names_, dropped, known, partners)
            if kept_action is not None:
                actions.append(kept_action)
        if len(entries) > limit:
            dropped["too_many"] += len(entries) - limit
    return sections, actions, dict(dropped)


def visit_key_of(key: str) -> str | None:
    """The visit a cited key is about: ``visit:1722`` and ``narr:1722:1`` give "1722"."""
    if key.startswith("visit:"):
        return key.split(":", 1)[1] or None
    if key.startswith("narr:"):
        parts = key.split(":")
        return (":".join(parts[1:-1]) or None) if len(parts) >= 3 else None
    return None


def cited_visits(sections: dict[str, Any], actions: list[dict]) -> list[str]:
    """The visit keys every kept sentence and action cites, in order, once each."""
    out: list[str] = []
    entries = [
        s
        for name, kept in (sections or {}).items()
        if name != "notes" and isinstance(kept, list)
        for s in kept
    ] + list(actions or [])
    for entry in entries:
        for key in entry.get("keys") or ():
            visit = visit_key_of(key)
            if visit and visit not in out and len(visit) <= 40:
                out.append(visit)
    return out


# ------------------------------------------------------------------------------------------ writing
def _refusal(response: Any) -> bool:
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return True
    return False


def _effort_refused(exc: BaseException) -> bool:
    if not isinstance(exc, openai.BadRequestError):
        return False
    param = str(getattr(exc, "param", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    return param.startswith("reasoning") or "reasoning.effort" in message


def _friendly(exc: BaseException) -> str:
    from neurodb.assistant.views import _friendly as friendly

    return friendly(exc) or "The AI service could not write the brief. Please try again."


def _tokens(answered: Any) -> tuple[int, int, int]:
    """The prompt's tokens (cached ones included), the cached ones, and the output's."""
    input_tokens, cached, output = usage.split(answered)
    return input_tokens + cached, cached, output


def _save(row: Insight) -> Insight:
    row.reason = (row.reason or "")[:200]
    row.save()
    return row


def _refused_by_gate(row: Insight | None, scope, version, user, trigger, refused) -> Insight:
    """A gate refused: keep the reason on the running row, or on a new row (none for too few visits,
    and none for a nightly brief while the AI is off)."""
    status_, reason = refused
    if row is not None and row.pk:
        row.status, row.reason, row.called = status_, reason, False
        return _save(row)
    row = _new_row(scope, version, user, trigger, status_, reason)
    if reason.startswith("Too few visits") or (reason == OFF and trigger == Insight.Trigger.NIGHTLY):
        return row  # not kept
    return _save(row)


def generate(
    scope,
    *,
    version: PromptVersion | None = None,
    user=None,
    trigger: str = Insight.Trigger.USER,
    insight: Insight | None = None,
    today: datetime.date | None = None,
) -> Insight:
    """Write the brief of ``scope`` (see the module's notes); fills ``insight`` (a running row) or a
    new row, and returns the brief to show: that row, or the earlier brief written from the very same
    input (with ``reused`` set). Never raises: an error is kept as a failed brief."""
    try:
        return _generate(scope, version, user, trigger, insight, today)
    except Exception as exc:
        logger.exception("Monitoring insights: the brief failed")
        reason = f"{ERROR}: {type(exc).__name__}"
        if insight is not None and insight.pk:
            Insight.objects.filter(pk=insight.pk).update(status=Insight.Status.FAILED, reason=reason[:200])
            insight.refresh_from_db()
            return insight
        version = version or profiles.published()
        if version is None:
            return Insight(status=Insight.Status.FAILED, reason=ERROR)
        try:
            return _save(_new_row(scope, version, user, trigger, Insight.Status.FAILED, reason))
        except Exception:
            logger.exception("Monitoring insights: the failed brief could not be kept")
            return Insight(status=Insight.Status.FAILED, reason=ERROR)


def _generate(scope, version, user, trigger, insight, today) -> Insight:
    from neurodb.assistant import agent

    from . import fallback
    from .facts import build

    clock = time.monotonic()
    user = _user(user)
    today = today or timezone.localdate()
    version = version or (insight.version if insight is not None else None) or profiles.published()
    if version is None:  # nothing published: the AI is off
        return Insight(status=Insight.Status.SKIPPED, reason=OFF)
    kind = KINDS.get(trigger, "user")

    # 1. the gates
    refused = gate(scope, version, user, trigger, insight)
    if refused:
        return _refused_by_gate(insight, scope, version, user, trigger, refused)

    # 2. the facts, then the budget for what they really cost
    names_ = privacy.names()
    facts = build(scope, version, today, names_=names_)
    ok, why = budget.allowed(kind, user, budget.estimate(facts, version))
    if not ok:
        status_ = Insight.Status.SKIPPED if why == budget.OFF else Insight.Status.LIMITED
        return _refused_by_gate(insight, scope, version, user, trigger, (status_, budget.REASONS[why]))

    row = insight if insight is not None else _new_row(scope, version, user, trigger, Insight.Status.RUNNING)
    rules_version, as_of = _refresh_state()
    row.rules_version, row.data_as_of, row.input_hash = rules_version, as_of, facts.input_hash
    row.sent = facts.sent
    model, effort = profiles.model_of(version), version.effort
    row.model, row.effort, row.max_output_tokens = model[:64], effort[:8], version.max_output_tokens

    # 3. a brief written from the very same input: shown again, no call (a test run always calls)
    if trigger != Insight.Trigger.TEST:
        found = (
            Insight.objects.filter(
                scope_hash=row.scope_hash,
                version=version,
                rules_version=rules_version,
                input_hash=facts.input_hash,
                status__in=WRITTEN,
            )
            .exclude(pk=row.pk)
            .exclude(trigger=Insight.Trigger.TEST)
            .order_by("-created_at")
            .first()
        )
        if found is not None:
            if row.pk:
                row.status, row.reason, row.called = Insight.Status.SKIPPED, UP_TO_DATE, False
                _save(row)
            found.reused = True
            return found

    # 5. the last privacy check, on everything about to be sent
    try:
        privacy.assert_clean(facts.payload, names_)
    except privacy.PrivacyRefused as exc:
        logger.error("Monitoring insights: a brief was not sent (%s): %s", row.scope_label, exc)
        row.status, row.reason, row.called = Insight.Status.FAILED, PRIVACY, False
        return _save(row)

    # 6. the call
    plan = sampling.plan(version, model, effort)
    states = dict(plan.states)
    call = request(facts, version, user if trigger == Insight.Trigger.USER else None)
    try:
        api = agent.client().with_options(timeout=settings.FMM_INSIGHTS_TIMEOUT_SECONDS, max_retries=1)
        response, states = sampling.call(api, call, plan, model, effort)
    except agent.AssistantUnavailable:
        row.status, row.reason, row.called = Insight.Status.SKIPPED, OFF, False
        return _save(row)
    except Exception as exc:
        if budget.trip(exc):
            reason = QUOTA
        elif _effort_refused(exc):
            reason = f"effort not accepted by {model}"
        else:
            reason = _friendly(exc)
        logger.warning("Monitoring insights: the brief's call failed: %s", type(exc).__name__)
        row.status, row.reason, row.called = Insight.Status.FAILED, reason, False
        row.sampling = sampling.summary(version, states, model, effort)
        row.duration_ms = int((time.monotonic() - clock) * 1000)
        return _save(row)
    answered = getattr(response, "usage", None)
    usage.record(usage.FMM, model, answered)
    row.called = True
    row.input_tokens, row.cached_tokens, row.output_tokens = _tokens(answered)
    row.sampling = sampling.summary(version, states, model, effort)
    row.sent_payload = facts.payload
    row.duration_ms = int((time.monotonic() - clock) * 1000)

    # 7. the answer
    if getattr(response, "status", "") == "incomplete":
        details = getattr(response, "incomplete_details", None)
        why_cut = str(getattr(details, "reason", "") or "")
        row.status = Insight.Status.FAILED
        row.reason = REFUSED if why_cut == "content_filter" else CUT_OFF
        return _save(row)
    if _refusal(response):
        row.status, row.reason = Insight.Status.FAILED, REFUSED
        return _save(row)
    try:
        raw = json.loads(getattr(response, "output_text", "") or "")
    except (TypeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        row.status, row.reason = Insight.Status.FAILED, MALFORMED
        return _save(row)

    # 8-9. the checks, then what is kept
    parts = sections_module.of(version)
    sections, actions, dropped = validate(raw, facts, today, names_, parts)
    row.dropped = dropped
    wants_actions = sections_module.action_part(parts) is not None
    if all(sections.values()) and (actions or not wants_actions):
        row.status, row.reason = Insight.Status.OK, ""
    elif any(sections.values()) or actions:
        row.status, row.reason = Insight.Status.PARTIAL, ""
    else:
        written = fallback.brief(facts, parts)
        sections, actions = written["sections"], written["actions"]
        row.status, row.reason = Insight.Status.FALLBACK, NOTHING_KEPT
    row.sections, row.actions = sections, actions
    row.cited_keys = cited_visits(sections, actions)
    return _save(row)


# ------------------------------------------------------------------------------------------ showing
@dataclass
class CurrentBrief:
    """The brief a page shows for a filter: a kept brief (``insight``), or the code-written one
    (``fallback``, built now, not kept); the badge says how current it is."""

    insight: Insight | None
    badge: str
    fallback: dict[str, Any] | None
    up_to_date: bool = False
    reason: str = ""
    older_version: bool = False


def _when(value: datetime.datetime | None, fmt: str = "j M H:i") -> str:
    return date_format(timezone.localtime(value), fmt) if value else ""


def shown(scope, version: PromptVersion | None = None) -> Insight | None:
    """The kept brief the page shows for ``scope``: the newest under the published version, else the
    newest under an older one; None when there is none. Reads at most two rows, no facts."""
    version = version if version is not None else profiles.published()
    rows = (
        Insight.objects.filter(scope_hash=scope.hash(), status__in=SHOWN)
        .exclude(trigger=Insight.Trigger.TEST)  # test runs are for the admin only
        .select_related("version")
    )
    if version is not None:
        found = rows.filter(version=version).order_by("-created_at").first()
        if found is not None:
            return found
    return rows.order_by("-created_at").first()


def current_hash(scope, version: PromptVersion) -> str:
    """The input hash a brief of ``scope`` written now would have (kept ten minutes per refresh)."""
    from .. import metrics
    from .facts import build

    return metrics.cached(
        scope, f"facts:{version.pk}:{version.content_hash[:12]}", lambda: build(scope, version).input_hash
    )


def why_not(scope, version: PromptVersion | None) -> str:
    """Why the page shows the code-written brief."""
    if version is None or not budget.switched_on(version):
        return OFF
    if budget.paused_until() is not None:
        return budget.REASONS[budget.PAUSED]
    if not version.insights_enabled:
        return DISABLED
    if _visits(scope) < settings.FMM_MIN_VISITS_FOR_AI:
        return TOO_FEW.format(n=settings.FMM_MIN_VISITS_FOR_AI)
    return NOT_YET


def code_written(scope, version=None) -> dict[str, Any]:
    """The code-written brief of ``scope``, from its figures, in the parts of ``version`` (the
    defaults without one); kept ten minutes per refresh."""
    from .. import metrics
    from . import fallback
    from .facts import build

    parts = sections_module.of(version)
    block = "fallback:" + ",".join(p["key"] for p in parts)
    return metrics.cached(scope, block, lambda: fallback.brief(build(scope, None, narratives=False), parts))


def current(scope, version: Any = PUBLISHED) -> CurrentBrief:
    """What the page shows for ``scope`` (see the module's notes); ``version`` is the published one
    when the caller has already read it."""
    version = profiles.published() if version is PUBLISHED else version
    found = shown(scope, version)
    if found is not None and version is not None and found.version_id == version.pk:
        if found.input_hash and found.input_hash == current_hash(scope, version):
            return CurrentBrief(found, "Up to date", None, up_to_date=True)
        badge = f"Written {_when(found.created_at)}"
        if found.data_as_of:
            badge += f" from data of {_when(found.data_as_of, 'j M')}"
        return CurrentBrief(found, badge + " — the data has changed since", None)
    if found is not None:
        now = f" (now v{version.number})" if version is not None else ""
        return CurrentBrief(
            found, f"Written with prompt v{found.version.number}{now}", None, older_version=True
        )
    reason = why_not(scope, version)
    return CurrentBrief(
        None,
        f"Written by NeuroDB from the figures — AI not used: {reason}",
        code_written(scope, version),
        reason=reason,
    )


def cited_keys(scope) -> set[str]:
    """The visits the brief shown for ``scope`` cites (the visits table's AI column)."""
    found = shown(scope)
    return set(found.cited_keys or []) if found is not None else set()


def citing(visit_key: str, scope) -> tuple[Insight | None, list[dict[str, Any]]]:
    """The brief shown for ``scope``, and its sentences and priority actions that cite visit
    ``visit_key`` (``{"section", "text"}``)."""
    found = shown(scope)
    if found is None or visit_key not in (found.cited_keys or []):
        return found, []
    out = []
    parts = sections_module.of(found.version)
    for part in sections_module.text_parts(parts):
        for sentence in (found.sections or {}).get(part["key"]) or []:
            if any(visit_key_of(k) == visit_key for k in sentence.get("keys") or ()):
                out.append({"section": part["label"], "text": sentence.get("text", "")})
    label = (sections_module.action_part(parts) or {}).get("label") or "Priority action points"
    for action in found.actions or []:
        if any(visit_key_of(k) == visit_key for k in action.get("keys") or ()):
            out.append({"section": label, "text": sections_module.action_line(action)})
    return found, out


# ------------------------------------------------------------------------------------------ nightly
NIGHTLY_KINDS = ("country", "sections", "rolling")


def nightly_scopes(today: datetime.date | None = None, kinds: tuple[str, ...] = NIGHTLY_KINDS) -> list:
    """The filters people land on, for the nightly briefs: the whole country this year; each NeuroDB
    section with an active user, as its users land on it (its eTools sections, possibly several; two
    sections landing on the same ones share a brief), kept from ``FMM_NIGHTLY_MIN_VISITS`` visits,
    most visits first; then the whole country over the last 90 days. At most
    ``FMM_NIGHTLY_MAX_INSIGHTS`` in all."""
    from django.contrib.auth import get_user_model

    from neurodb.accounts.models import Section

    from ..scope import Scope

    today = today or timezone.localdate()
    out = []
    if "country" in kinds:
        out.append(Scope.from_params(QueryDict("section="), None, today))
    if "sections" in kinds:
        active = (
            get_user_model()
            .objects.filter(is_active=True, section__isnull=False)
            .values_list("section_id", flat=True)
            .distinct()
        )
        found: dict[tuple[str, ...], tuple[int, Any]] = {}
        for section in Section.objects.filter(pk__in=list(active)).order_by("pk"):
            scope = Scope.from_params(QueryDict(""), SimpleNamespace(section=section), today)
            if not scope.sections or scope.sections in found:
                continue
            visits = scope.visits().count()
            if visits >= settings.FMM_NIGHTLY_MIN_VISITS:
                found[scope.sections] = (visits, scope)
        out += [scope for _n, scope in sorted(found.values(), key=lambda f: (-f[0], f[1].sections))]
    if "rolling" in kinds:
        out.append(Scope.from_params(QueryDict("preset=last_90&section="), None, today))
    seen, unique = set(), []
    for scope in out:
        if scope.hash() not in seen:
            seen.add(scope.hash())
            unique.append(scope)
    return unique[: max(0, settings.FMM_NIGHTLY_MAX_INSIGHTS)]


def housekeeping(now: datetime.datetime | None = None) -> dict[str, int]:
    """Close stopped briefs, blank the payloads kept past ``FMM_PAYLOAD_RETENTION_DAYS``, delete the
    refused and skipped briefs after 30 days and every brief (and chat question) after
    ``FMM_RETENTION_DAYS``."""
    from ..models import ChatQuestion

    now = now or timezone.now()
    out = {"stopped": expire_stale(now)}
    payload_before = now - datetime.timedelta(days=settings.FMM_PAYLOAD_RETENTION_DAYS)
    out["payloads_blanked"] = (
        Insight.objects.filter(created_at__lt=payload_before)
        .exclude(sent_payload=None)
        .update(sent_payload=None)
    )
    refused_before = now - datetime.timedelta(days=REFUSED_KEPT_DAYS)
    out["refused_deleted"] = Insight.objects.filter(
        created_at__lt=refused_before, status__in=(Insight.Status.LIMITED, Insight.Status.SKIPPED)
    ).delete()[0]
    kept_before = now - datetime.timedelta(days=settings.FMM_RETENTION_DAYS)
    out["deleted"] = Insight.objects.filter(created_at__lt=kept_before).delete()[0]
    out["chat_deleted"] = ChatQuestion.objects.filter(created_at__lt=kept_before).delete()[0]
    return out


def write_nightly(scopes, triggered_by: str = "schedule", today: datetime.date | None = None):
    """The nightly briefs of ``scopes``, as one run (``SyncRun`` fmm_insights): each written by
    :func:`generate`. It stops at the first refusal of the budget or the pause (the scopes left count
    as failed), or after 3 failed briefs in a row. With the AI off it writes nothing and finishes
    "skipped". The housekeeping runs first, whatever the switch."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import finish_by_counts, new_run

    run = new_run(SyncRun.Job.FMM_INSIGHTS, "nightly", triggered_by)
    kept = housekeeping()
    version = profiles.published()
    if version is None or not budget.switched_on():
        run.finish(SyncRun.Status.SUCCEEDED, skipped="AI is off", housekeeping=kept)
        return run
    scopes = list(scopes)
    run.rows_in = len(scopes)
    tally: Counter = Counter()
    labels, in_a_row = [], 0
    for index, scope in enumerate(scopes):
        labels.append(scope.label())
        busy = Insight.objects.filter(scope_hash=scope.hash(), version=version, status=Insight.Status.RUNNING)
        if busy.exists():  # a Regenerate is writing it now
            tally["running"] += 1
            continue
        row = generate(scope, version=version, trigger=Insight.Trigger.NIGHTLY, today=today)
        if getattr(row, "reused", False):
            tally["reused"] += 1
            run.rows_written += 1
            in_a_row = 0
        elif row.status in SHOWN:
            tally["generated"] += 1
            tally["tokens"] += row.input_tokens + row.output_tokens
            run.rows_written += 1
            in_a_row = 0
        elif row.status == Insight.Status.LIMITED:
            tally["limited"] += 1
            run.rows_failed += len(scopes) - index
            break
        elif row.status == Insight.Status.SKIPPED:
            tally["skipped"] += 1
        else:
            tally["failed"] += 1
            run.rows_failed += 1
            in_a_row += 1
            if in_a_row >= FAILURES_IN_A_ROW:
                run.rows_failed += len(scopes) - index - 1
                break
    details = {k: tally.get(k, 0) for k in ("generated", "reused", "limited", "failed", "tokens")}
    details.update(skipped=tally.get("skipped", 0), running=tally.get("running", 0))
    return finish_by_counts(run, **details, scopes=labels, housekeeping=kept)
