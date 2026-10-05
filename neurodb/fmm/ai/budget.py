"""How much AI Monitoring insights may use: the switch, the pause, the day's caps and the quotas.

Every AI call of Monitoring insights (a brief, a test run, a chat answer) asks :func:`allowed` first, with
the tokens it may need (:func:`estimate`). It says no, with a reason, when:

- **off**: Monitoring insights, its AI (``FMM_AI``) or the assistant is switched off, or no prompt version
  is published (:func:`switched_on`);
- **paused**: the OpenAI credit ran out on an earlier call (:func:`trip`), so the AI rests for 6 hours
  (``AIState.paused_until``);
- **budget**: Monitoring insights' tokens today plus the call would pass ``FMM_DAILY_TOKEN_CAP``, or its
  calls today reached ``FMM_MAX_CALLS_PER_DAY``; or every AI feature on the shared key together would
  pass 80% of ``AI_DAILY_TOKEN_SOFT_CAP`` for a nightly brief or a test run, or 100% for a person's
  Regenerate or chat question (people come before background work).

The day's use is read from the AI use ledger (``assistant.AIUsage``, feature ``fmm``), where each call is
recorded right after it returns. "Today" is the local date (Beirut): the caps and quotas start again at
local midnight.

The per-person quotas (:func:`quota`) come from the published prompt version: briefs written on request
("Regenerate") and chat questions per person per day. A brief found up to date (no call) and a refused
request do not count; a brief being written does. Test runs count against the office cap only.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Literal

from django.apps import apps
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.watch.budget import SOFT_SHARE, is_quota

from ..models import AIState, Insight, PromptVersion
from . import profiles

logger = logging.getLogger(__name__)

PAUSE_HOURS = 6
QUOTA_REASON = "the OpenAI credit ran out"
CHARS_PER_TOKEN = 3.5
OFF, PAUSED, BUDGET = "off", "paused", "budget"
REASONS = {
    OFF: "AI is switched off",
    PAUSED: "AI is paused: the OpenAI credit ran out",
    BUDGET: "Today's AI budget for Monitoring insights is used; it resets at midnight.",
}


def switched_on(version: PromptVersion | None = None) -> bool:
    """Monitoring insights, its AI and the assistant are switched on, and a prompt version is published
    (``version``: the published one, when the caller has already read it)."""
    if not (settings.FMM_ENABLED and settings.FMM_AI and settings.AI_ASSISTANT_ENABLED):
        return False
    return version is not None or profiles.published() is not None


def paused_until(now: datetime.datetime | None = None) -> datetime.datetime | None:
    """When the AI may be used again while it is paused; None when it is not paused."""
    now = now or timezone.now()
    state = AIState.objects.filter(pk=1).first()  # read only: the row is created on the first pause
    if state is not None and state.paused_until and state.paused_until > now:
        return state.paused_until
    return None


def allowed(
    kind: Literal["user", "nightly", "test", "chat"],
    user=None,
    tokens: int = 0,
    now=None,
    version: PromptVersion | None = None,
) -> tuple[bool, str]:
    """Whether a call of ``kind`` needing ``tokens`` may start now, and why not (``off``, ``paused`` or
    ``budget``; see the module's notes). ``(True, "")`` when it may. ``user`` is the person asking, if
    any (their own quota is :func:`quota`); ``version`` as in :func:`switched_on`."""
    if kind not in ("user", "nightly", "test", "chat"):
        raise ValueError(f"unknown kind of AI call {kind!r}")
    if not switched_on(version):
        return False, OFF
    if paused_until(now) is not None:
        return False, PAUSED
    if usage.today_total(usage.FMM) + tokens > settings.FMM_DAILY_TOKEN_CAP:
        return False, BUDGET
    if usage.today_calls(usage.FMM) >= settings.FMM_MAX_CALLS_PER_DAY:
        return False, BUDGET
    every_feature = usage.today_total() + tokens
    if kind in ("nightly", "test") and every_feature > settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE:
        return False, BUDGET
    if kind in ("user", "chat") and every_feature > settings.AI_DAILY_TOKEN_SOFT_CAP:
        return False, BUDGET
    return True, ""


def _today_start() -> datetime.datetime:
    """Local midnight today, as an aware time (the quotas start again then)."""
    today = timezone.localdate()
    return timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))


def quota(kind: Literal["insights", "chat"], user, version: PromptVersion | None = None) -> tuple[int, int]:
    """(used today, allowed per day) of ``user``'s ``kind`` (briefs written on request, or chat
    questions), with the limits of ``version`` (default: the published one; none published: 0 allowed)."""
    version = version or profiles.published()
    if kind == "insights":
        allowed_ = version.insights_per_user_per_day if version else 0
    elif kind == "chat":
        allowed_ = version.chat_per_user_per_day if version else 0
    else:
        raise ValueError(f"unknown quota {kind!r}")
    if user is None or not getattr(user, "pk", None):
        return 0, allowed_
    since = _today_start()
    if kind == "insights":  # a call was made, or the brief is being written
        used = (
            Insight.objects.filter(trigger=Insight.Trigger.USER, created_by=user, created_at__gte=since)
            .filter(Q(called=True) | Q(status=Insight.Status.RUNNING))
            .count()
        )
    else:
        used = _chat_questions_today(user, since)
    return used, allowed_


def _chat_questions_today(user, since: datetime.datetime) -> int:
    """The person's chat questions today, refused ones (over a limit) left out. The chat's log arrives with
    the chat itself; before then no question was asked."""
    try:
        model = apps.get_model("fmm", "ChatQuestion")
    except LookupError:
        return 0
    return model.objects.filter(user=user, created_at__gte=since).exclude(status="limited").count()


def office_share() -> float:
    """The share of today's ``FMM_DAILY_TOKEN_CAP`` used (0-100, one decimal), for the quota pill
    ("office AI budget 62% used")."""
    cap = settings.FMM_DAILY_TOKEN_CAP
    if cap <= 0:
        return 100.0
    return round(min(100.0, usage.today_total(usage.FMM) * 100 / cap), 1)


def estimate(facts: Any, version: PromptVersion) -> int:
    """The tokens a brief may use: its payload and its instructions (about 3.5 characters a token) and
    the version's output limit (reasoning included)."""
    payload = getattr(facts, "payload", facts)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    instructions = profiles.compose(version, "insights")
    return int(len(text) / CHARS_PER_TOKEN + len(instructions) / CHARS_PER_TOKEN) + version.max_output_tokens


# ------------------------------------------------------------------------------------------ the pause
def pause(reason: str = QUOTA_REASON, hours: int = PAUSE_HOURS, now: datetime.datetime | None = None) -> None:
    """Stop Monitoring insights' AI for ``hours``: every brief and chat answer is refused ("paused")
    until then."""
    now = now or timezone.now()
    AIState.load()
    AIState.objects.filter(pk=1).update(
        paused_until=now + datetime.timedelta(hours=hours), reason=str(reason)[:200], updated_at=now
    )
    logger.warning("Monitoring insights: AI paused for %s hours: %s", hours, reason)


def trip(exc: BaseException, now: datetime.datetime | None = None) -> bool:
    """A call failed with ``exc``: when the OpenAI credit ran out (``watch.budget.is_quota``), pause the
    AI for 6 hours. True when it paused."""
    if is_quota(exc):
        pause(QUOTA_REASON, now=now)
        return True
    return False


def status(user=None) -> dict[str, Any]:
    """Today's use against the caps, the pause and (for ``user``) the quotas, for the page and the admin."""
    until = paused_until()
    out: dict[str, Any] = {
        "on": switched_on(),
        "fmm_tokens": usage.today_total(usage.FMM),
        "fmm_token_cap": settings.FMM_DAILY_TOKEN_CAP,
        "fmm_calls": usage.today_calls(usage.FMM),
        "fmm_call_cap": settings.FMM_MAX_CALLS_PER_DAY,
        "office_share": office_share(),
        "paused_until": until.isoformat(timespec="minutes") if until else "",
    }
    if user is not None:
        out["insights"] = quota("insights", user)
        out["chat"] = quota("chat", user)
    return out
