"""How much AI NeuroDB Watch may use, and when it stops using it: the budget and the circuit breaker.

Every AI call of the watch (the morning notes, :mod:`neurodb.watch.explain`, and the background
look-ups, :mod:`neurodb.watch.investigate`) asks :func:`allowed` first, with the room the call needs.
It says no, with the reason the note shows ("AI not used today: ..."), when:

- **off**: the watch, its AI (``WATCH_AI``) or the assistant (``AI_ASSISTANT_ENABLED``) is switched off;
- **paused**: the circuit breaker below stopped the watch's AI for a while
  (``WatchState.ai_paused_until``);
- **budget**: the watch's tokens today plus the room asked for would pass ``WATCH_DAILY_TOKEN_CAP``
  (notes and look-ups together), its calls today plus the calls asked for would pass
  ``WATCH_MAX_MODEL_CALLS_PER_DAY``, or every AI feature on the shared key together would pass 80% of
  ``AI_DAILY_TOKEN_SOFT_CAP``. The watch is the first to stop, so Ask NeuroDB keeps working.

The day's use is read from the AI use ledger (``assistant.AIUsage``, feature "watch"), where each call
is recorded right after it returns.

**The circuit breaker.** After a call fails, :func:`trip` decides:

- the OpenAI credit ran out (``insufficient_quota``, or a 429 that says quota): the watch's AI pauses
  for 6 hours at once, and the administrators get the critical item ``system:openai_quota`` ("The
  OpenAI credit ran out: Ask NeuroDB is affected too");
- any other failure counts one more failure in a row (``WatchState.consecutive_ai_errors``); the third
  in a row pauses the AI for 6 hours.

A call that the service answers (:func:`succeeded`) starts the count again from none and closes the
``system:openai_quota`` item: the credit is back. The admin home lists the pause while it lasts
(``neurodb.web.health``).

Nothing else is written here: only ``WatchState`` and the one ``system:openai_quota`` item.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import openai
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.assistant import usage

from .memory import fingerprint
from .models import DetectorSetting, WatchItem, WatchNote, WatchState

logger = logging.getLogger(__name__)

NOTE_TOKENS = 6_000  # room one morning note needs in the day's caps: about 5k in, up to 1.5k out
SOFT_SHARE = 0.8  # of AI_DAILY_TOKEN_SOFT_CAP, across every feature on the key
PAUSE_HOURS = 6
ERRORS_TO_PAUSE = 3  # failed calls in a row

# Why the AI was not used (the same words as WatchNote.ai_skipped_reason)
OFF, BUDGET, PAUSED = (
    WatchNote.Skipped.OFF.value,
    WatchNote.Skipped.BUDGET.value,
    WatchNote.Skipped.PAUSED.value,
)
QUOTA, ERROR = WatchNote.Skipped.QUOTA.value, WatchNote.Skipped.ERROR.value
# Why the AI paused (WatchState.ai_pause_reason; the admin home shows it)
QUOTA_REASON = "the OpenAI credit ran out"
ERRORS_REASON = f"the AI did not answer {ERRORS_TO_PAUSE} times in a row"

# The administrators' item when the credit runs out. No check module finds it, so the memory never
# closes it as missed: it is opened by :func:`trip` and closed by :func:`succeeded`.
QUOTA_KEY = "system:openai_quota"
QUOTA_CHECK = "openai_quota"
QUOTA_SOURCE = "OpenAI"
QUOTA_DETAIL = (
    "OpenAI refused a call because the credit of the shared key ran out. Ask NeuroDB, the daily review "
    "and the other AI features use the same key, so they are affected too. NeuroDB Watch lists its "
    "notes without AI and tries again later. Add credit to the OpenAI project, or raise its budget."
)
QUOTA_CLOSED = "the AI answered again"


# ---------------------------------------------------------------------------- may the AI be used
def switched_on() -> bool:
    """The watch, its AI and the assistant are all switched on."""
    return bool(settings.WATCH_ENABLED and settings.WATCH_AI and settings.AI_ASSISTANT_ENABLED)


def paused_until(now: datetime.datetime | None = None) -> datetime.datetime | None:
    """When the watch's AI may be used again, while it is paused; None when it is not paused."""
    now = now or timezone.now()
    state = WatchState.objects.filter(pk=1).first()  # read only: the row is created on first write
    if state is not None and state.ai_paused_until and state.ai_paused_until > now:
        return state.ai_paused_until
    return None


def allowed(
    tokens: int = NOTE_TOKENS, calls: int = 1, now: datetime.datetime | None = None
) -> tuple[bool, str]:
    """Whether a call needing ``tokens`` and ``calls`` (a look-up: its whole token estimate and its
    rounds) may start now, and why not: ``off``, ``paused`` or ``budget`` (see the module's notes).
    ``(True, "")`` when it may."""
    if not switched_on():
        return False, OFF
    if paused_until(now) is not None:
        return False, PAUSED
    if usage.today_total(usage.WATCH) + tokens > settings.WATCH_DAILY_TOKEN_CAP:
        return False, BUDGET
    if usage.today_calls(usage.WATCH) + calls > settings.WATCH_MAX_MODEL_CALLS_PER_DAY:
        return False, BUDGET
    if usage.today_total() + tokens > settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE:
        return False, BUDGET
    return True, ""


def status(now: datetime.datetime | None = None) -> dict[str, Any]:
    """Today's use against the caps, and the pause, for a run's details and the admin."""
    until = paused_until(now)
    state = WatchState.objects.filter(pk=1).first()
    return {
        "watch_tokens": usage.today_total(usage.WATCH),
        "watch_token_cap": settings.WATCH_DAILY_TOKEN_CAP,
        "watch_calls": usage.today_calls(usage.WATCH),
        "watch_call_cap": settings.WATCH_MAX_MODEL_CALLS_PER_DAY,
        "all_tokens": usage.today_total(),
        "all_token_limit": int(settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE),
        "paused_until": until.isoformat(timespec="minutes") if until else "",
        "pause_reason": (state.ai_pause_reason if state and until else ""),
        "errors_in_a_row": state.consecutive_ai_errors if state else 0,
    }


# ---------------------------------------------------------------------------- the circuit breaker
def is_quota(exc: BaseException) -> bool:
    """The OpenAI credit ran out: ``insufficient_quota`` (as an error code or type, also when the
    stream ended with it), or a 429 whose message says quota (a plain 429 is the service being busy)."""
    for name in ("code", "type"):
        if str(getattr(exc, name, "") or "") == "insufficient_quota":
            return True
    too_many = isinstance(exc, openai.RateLimitError) or getattr(exc, "status_code", None) == 429
    return too_many and "quota" in str(exc).lower()


def pause(reason: str, hours: int = PAUSE_HOURS, now: datetime.datetime | None = None) -> None:
    """Stop the watch's AI for ``hours``: the notes are listed without it and no look-up runs until
    then. The admin home says so, with ``reason``. The failures in a row start again from none."""
    now = now or timezone.now()
    WatchState.get()  # the row, created on first use
    WatchState.objects.filter(pk=1).update(
        ai_paused_until=now + datetime.timedelta(hours=hours),
        ai_pause_reason=str(reason)[:200],
        consecutive_ai_errors=0,
    )
    logger.warning("NeuroDB Watch: AI paused for %s hours: %s", hours, reason)


def trip(exc: BaseException, now: datetime.datetime | None = None) -> str:
    """A call failed with ``exc``: pause at once when the credit ran out (and tell the
    administrators), else count one more failure in a row and pause at the third. Returns why the AI
    was not used: ``quota`` or ``error``."""
    now = now or timezone.now()
    if is_quota(exc):
        pause(QUOTA_REASON, now=now)
        try:
            raise_quota_item(now)
        except Exception:  # the pause holds even if the item cannot be written
            logger.exception("NeuroDB Watch: the item for the OpenAI credit could not be written")
        return QUOTA
    WatchState.get()
    WatchState.objects.filter(pk=1).update(consecutive_ai_errors=F("consecutive_ai_errors") + 1)
    if WatchState.objects.filter(pk=1, consecutive_ai_errors__gte=ERRORS_TO_PAUSE).exists():
        pause(ERRORS_REASON, now=now)
    return ERROR


def succeeded(now: datetime.datetime | None = None) -> None:
    """The model service answered a call: the failures in a row start again from none, and the
    administrators' item for the OpenAI credit closes (the credit is back)."""
    WatchState.objects.filter(pk=1, consecutive_ai_errors__gt=0).update(consecutive_ai_errors=0)
    try:
        close_quota_item(timezone.localdate(now or timezone.now()))
    except Exception:
        logger.exception("NeuroDB Watch: the item for the OpenAI credit could not be closed")


# ---------------------------------------------------------------------------- the administrators' item
def _ai_use_url() -> str:
    try:
        return reverse("admin:assistant_aiusage_changelist")
    except NoReverseMatch:
        return ""


def raise_quota_item(now: datetime.datetime | None = None) -> WatchItem:
    """Open (or keep open) the critical item ``system:openai_quota``, told to the administrators only
    and never sent to the AI. Seen again on another day, it says so in its story."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    url = _ai_use_url()
    evidence = {
        "source": QUOTA_SOURCE,
        "source_job": "",
        "synced_at": now.isoformat(timespec="seconds"),
        "records": [
            {
                "label": "OpenAI refused a call: the credit ran out",
                "date": today.isoformat(),
                "value": "insufficient_quota",
                "url": url,
            }
        ],
        "numbers": {},
    }
    values = {
        "detector": QUOTA_CHECK,
        "kind": WatchItem.Kind.SYSTEM,
        "severity": WatchItem.Severity.CRITICAL,
        "confidence": WatchItem.Confidence.SURE,
        "scope": WatchItem.Scope.ADMINS,
        "title": f"The OpenAI credit ran out on {_day(today)}: Ask NeuroDB is affected too",
        "detail": QUOTA_DETAIL,
        "url": url,
        "evidence": evidence,
    }
    DetectorSetting.ensure(QUOTA_CHECK, DetectorSetting.Mode.ON)  # a system check: on from the start
    with transaction.atomic():
        item = WatchItem.objects.select_for_update().filter(key=QUOTA_KEY).first()
        if item is None:
            item = WatchItem(
                key=QUOTA_KEY, first_seen_on=today, last_seen_on=today, changed_on=today, **values
            )
            item.add_story("First noticed: the OpenAI credit ran out", today)
        elif item.state == WatchItem.State.WRONG and item.evidence_hash == fingerprint(evidence):
            return item  # marked wrong, and nothing new since: it stays hidden
        elif item.state != WatchItem.State.OPEN:
            for name, value in values.items():
                setattr(item, name, value)
            item.state = WatchItem.State.OPEN
            item.closed_on = None
            item.close_reason = ""
            item.first_seen_on = today
            item.changed_on = today
            item.missed_runs = 0
            item.add_story("The OpenAI credit ran out again", today)
        elif item.last_seen_on != today:
            item.add_story("The OpenAI credit is still out", today)
        item.last_seen_on = today
        item.evidence_hash = fingerprint(item.evidence)
        item.save()
    return item


def close_quota_item(today: datetime.date | None = None) -> bool:
    """Close the open ``system:openai_quota`` item: the AI answered again. True when one was closed."""
    today = today or timezone.localdate()
    with transaction.atomic():
        alive = (WatchItem.State.OPEN, WatchItem.State.WRONG)
        item = WatchItem.objects.select_for_update().filter(key=QUOTA_KEY, state__in=alive).first()
        if item is None:
            return False
        item.state = WatchItem.State.CLOSED
        item.closed_on = today
        item.changed_on = today
        item.close_reason = QUOTA_CLOSED
        item.add_story(f"Closed: {QUOTA_CLOSED}", today)
        item.save()
    return True


def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"
