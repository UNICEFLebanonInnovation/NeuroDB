"""The AI content summary of the action points page (FMS §10.6): what the action points on screen are
about, as up to 5 dominant themes (a name, how many action points it covers and one example action point)
and a one-sentence overall pattern.

One click is one Responses API call. It reads the action points of the page's filter (at most
``ActionPointSetting.summary_points``, 150, most recent first): each one's reference and its description,
cut to ``TEXT_CHARS`` and cleaned (``privacy.clean``: names, e-mail addresses, phone numbers and links
removed), then checked once more (``privacy.assert_clean``). Who a point is assigned to is never sent. The
instructions are the published prompt version's ``rule_prompts["ap_content_summary"]`` followed by
NeuroDB's fixed rules (``prompts.SAFETY_AP_SUMMARY``); the model and temperature are the AI checks'.

**Checked before it is shown** (:func:`grounded`): a theme whose example is not one of the references
sent, or whose name writes a word NeuroDB never shows, is left out; the counts must add up to at most the
action points sent, or the whole summary is refused; the pattern sentence is cleaned and left out when it
names a number that is neither a count nor in what was sent.

**Limits**: each person may ask ``ActionPointSetting.summary_per_user_per_day`` summaries a day (5; a
refused request does not count); the call counts against ``FMM_AP_REVIEW_DAILY_TOKEN_CAP`` (feature
``fmm_ap_review``) and 100% of ``AI_DAILY_TOKEN_SOFT_CAP`` (a person's request comes before background
work). The summary is shown once, never cached (another filter is another summary) and never kept:
``ActionPointSummary`` records who asked, when and what it cost.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.utils import timezone

from neurodb.assistant import usage

from .. import lebanon, privacy
from ..models import ActionPointSetting, ActionPointSummary, ScoreSetting
from . import budget, checks, prompts, sampling

logger = logging.getLogger(__name__)

KEY = lebanon.AP_SUMMARY_KEY
EFFORT = "low"
FORMAT_NAME = "fmm_ap_summary"
TEXT_CHARS = 300  # characters of each description sent
NAME_CHARS = 80
PATTERN_CHARS = 300
THEMES = 5
OUTPUT_TOKENS = 4000  # the reasoning included
SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "example": {"type": "string"},
                },
                "required": ["name", "count", "example"],
                "additionalProperties": False,
            },
        },
        "pattern": {"type": "string"},
    },
    "required": ["themes", "pattern"],
    "additionalProperties": False,
}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

OFF = "The AI content summary is not available: AI is switched off."
NO_PROMPT = (
    "The AI content summary is not available: the published prompt version holds no instructions for it."
)
PAUSED = "AI is paused: the OpenAI credit ran out. Try again later."
BUDGET = "Today's AI budget for action points is used; it resets at midnight."
QUOTA = "You have used today's AI content summaries ({n} a day); they reset at midnight."
NOTHING = "No action point with a description matches the filter."
FAILED = "The summary could not be written. Try again in a moment."
NOT_CHECKED = "The summary did not add up to the action points read, so it is not shown. Try again."


@dataclass
class Summary:
    """What the card shows: the themes (``{"name", "count", "example"}``), the pattern, how many action
    points were read; or ``message`` when there is no summary."""

    ok: bool
    message: str = ""
    themes: list[dict[str, Any]] = field(default_factory=list)
    pattern: str = ""
    points: int = 0
    total: int = 0  # the action points of the filter


def instructions() -> str | None:
    from . import profiles

    version = profiles.published()
    text = ((version.rule_prompts if version else None) or {}).get(KEY)
    return text.strip() if isinstance(text, str) and text.strip() else None


def _today_start() -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time.min))


def quota(user) -> tuple[int, int]:
    """(used today, allowed per day) of ``user``'s summaries; a refused request does not count."""
    allowed_ = ActionPointSetting.load().summary_per_user_per_day
    if user is None or not getattr(user, "pk", None):
        return 0, allowed_
    used = (
        ActionPointSummary.objects.filter(user=user, created_at__gte=_today_start())
        .exclude(status=ActionPointSummary.Status.LIMITED)
        .count()
    )
    return used, allowed_


def available() -> bool:
    """The button shows: the AI is on and instructions are published."""
    return bool(settings.FMM_ENABLED and settings.FMM_AI and settings.AI_ASSISTANT_ENABLED) and bool(
        instructions()
    )


def collect(points, limit: int, names_: frozenset[str]) -> tuple[list[dict[str, str]], int]:
    """The action points sent (``{"ref", "text"}``, cleaned, each reference once) of the ``points``
    query, at most ``limit``, most recent first; and how many the query holds."""
    from django.db.models import F

    total = points.count()
    rows = (
        points.exclude(description="")
        .order_by(F("due_date").desc(nulls_last=True), "-datamart_id")
        .values_list("datamart_id", "reference_number", "description")[:limit]
    )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for datamart_id, reference, description in rows:
        text = privacy.clean(description, TEXT_CHARS, names_)[0]
        if not text.strip():
            continue
        ref = privacy.clean((reference or "").strip(), 100, names_)[0] or f"AP {datamart_id}"
        if ref in seen:
            ref = f"{ref} ({datamart_id})"
        seen.add(ref)
        out.append({"ref": ref, "text": text})
    return out, total


def request(prompt: str, sent: dict[str, Any], setting: ScoreSetting) -> dict[str, Any]:
    return {
        "model": checks.model_of(setting),
        "instructions": prompts.compose_ap(prompt, "summary"),
        "input": [{"role": "user", "content": json.dumps(sent, ensure_ascii=False, sort_keys=True)}],
        "reasoning": {"effort": EFFORT},
        "max_output_tokens": max(OUTPUT_TOKENS, setting.ai_max_output_tokens),
        "store": False,
        "prompt_cache_key": "neurodb-fmm-ap-summary",
        "text": {"format": {"type": "json_schema", "name": FORMAT_NAME, "strict": True, "schema": SCHEMA}},
    }


def grounded(
    raw: Any, sent: dict[str, Any], names_: frozenset[str]
) -> tuple[list[dict[str, Any]], str] | None:
    """The themes and pattern that pass the checks (see the module's notes); None when the counts add up
    to more than the action points sent (or the answer is not the format)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("themes"), list):
        return None
    refs = {p["ref"] for p in sent["points"]}
    themes: list[dict[str, Any]] = []
    for theme in raw["themes"]:
        if not isinstance(theme, dict):
            continue
        count, example = theme.get("count"), str(theme.get("example") or "").strip()
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or example not in refs:
            continue
        name = privacy.clean(theme.get("name"), NAME_CHARS, names_)[0].strip()
        if not name or checks._NEVER.search(name) or any(p in name for p in privacy.PLACEHOLDERS):
            continue
        themes.append({"name": name, "count": count, "example": example})
    themes = themes[:THEMES]
    if (
        sum(t["count"] for t in raw["themes"] if isinstance(t, dict) and isinstance(t.get("count"), int))
        > (sent["count"])
    ):
        return None
    pattern = privacy.clean(raw.get("pattern"), PATTERN_CHARS, names_)[0].strip()
    allowed = {str(t["count"]) for t in themes} | {str(sent["count"])}
    sent_text = json.dumps(sent, ensure_ascii=False)
    if checks._NEVER.search(pattern) or any(
        n not in allowed and n not in sent_text for n in _NUMBER.findall(pattern)
    ):
        pattern = ""
    return themes, pattern


def summarise(points, user) -> Summary:
    """The AI content summary of the ``points`` query (``datamart.ActionPoint``, the page's filter) for
    ``user`` (see the module's notes)."""
    from neurodb.assistant import agent

    if not (settings.FMM_ENABLED and settings.FMM_AI and settings.AI_ASSISTANT_ENABLED):
        return Summary(False, OFF)
    prompt = instructions()
    if prompt is None:
        return Summary(False, NO_PROMPT)
    used, allowed_ = quota(user)
    if used >= allowed_:
        ActionPointSummary.objects.create(user=user, status=ActionPointSummary.Status.LIMITED, reason="quota")
        return Summary(False, QUOTA.format(n=allowed_))
    if budget.paused_until() is not None:
        return Summary(False, PAUSED)
    setting = ScoreSetting.load()
    names_ = privacy.names()
    sent_points, total = collect(points, ActionPointSetting.load().summary_points, names_)
    if not sent_points:
        return Summary(False, NOTHING, total=total)
    sent = {"count": len(sent_points), "points": sent_points}
    try:
        privacy.assert_clean(sent, names_)
    except privacy.PrivacyRefused as exc:
        logger.error("Action points: an AI content summary was not sent: %s", exc)
        return Summary(False, FAILED, total=total)
    chars = len(json.dumps(sent, ensure_ascii=False)) + len(prompts.compose_ap(prompt, "summary"))
    tokens = int(chars / checks.CHARS_PER_TOKEN) + max(OUTPUT_TOKENS, setting.ai_max_output_tokens)
    if (
        usage.today_total(usage.FMM_AP_REVIEW) + tokens > settings.FMM_AP_REVIEW_DAILY_TOKEN_CAP
        or usage.today_total() + tokens > settings.AI_DAILY_TOKEN_SOFT_CAP
    ):
        ActionPointSummary.objects.create(
            user=user, status=ActionPointSummary.Status.LIMITED, reason="budget"
        )
        return Summary(False, BUDGET, total=total)
    row = ActionPointSummary.objects.create(
        user=user, status=ActionPointSummary.Status.RUNNING, called=True, points=len(sent_points)
    )
    model = checks.model_of(setting)
    try:
        api = agent.client().with_options(timeout=settings.FMM_AP_REVIEW_TIMEOUT_SECONDS, max_retries=1)
        plan = sampling.plan(SimpleNamespace(temperature=setting.ai_temperature, top_p=None), model, EFFORT)
        response, _states = sampling.call(api, request(prompt, sent, setting), plan, model, EFFORT)
    except Exception as exc:
        budget.trip(exc)
        logger.warning("Action points: an AI content summary failed: %s", type(exc).__name__)
        _finish(row, ActionPointSummary.Status.FAILED, model, None, type(exc).__name__)
        return Summary(False, FAILED, total=total)
    answered = getattr(response, "usage", None)
    usage.record(usage.FMM_AP_REVIEW, model, answered)
    try:
        raw = json.loads(getattr(response, "output_text", "") or "")
    except (TypeError, ValueError):
        raw = None
    found = grounded(raw, sent, names_) if getattr(response, "status", "") != "incomplete" else None
    if found is None or not found[0]:
        _finish(row, ActionPointSummary.Status.FAILED, model, answered, "not checked")
        return Summary(False, NOT_CHECKED if raw is not None else FAILED, total=total)
    _finish(row, ActionPointSummary.Status.DONE, model, answered, "")
    themes, pattern = found
    return Summary(True, themes=themes, pattern=pattern, points=len(sent_points), total=total)


def _finish(row: ActionPointSummary, status: str, model: str, answered, reason: str) -> None:
    input_tokens, cached, output = usage.split(answered)
    row.status, row.model, row.reason = status, model[:64], reason[:200]
    row.input_tokens = min(input_tokens + cached, 2_000_000_000)
    row.output_tokens = min(output, 2_000_000_000)
    row.save(update_fields=["status", "model", "reason", "input_tokens", "output_tokens"])
