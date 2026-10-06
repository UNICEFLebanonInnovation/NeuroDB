"""One ledger of AI use for the shared OpenAI key.

Every feature that calls the model adds the call to today's ``AIUsage`` row of its feature and model
right after the call returns (``record``): Ask NeuroDB, the daily review (summary and decisions), the
What's new note, document summaries, periodic report figures, the country programme reading, NeuroDB
Watch and Monitoring insights (its briefs, test runs and chat; its AI checks apart; the action points'
AI review and summaries apart). The day's totals
(``today_total``,
``today_calls``) tell how much of the key's daily spend is used, across every feature or for one; the
admin lists the rows under "AI use", in tokens, and in US dollars when the optional ``AI_PRICE_*``
settings give the prices.

Recording never fails the feature that called the model: an error is logged and the call goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import AIUsage

logger = logging.getLogger(__name__)

# The features on the shared key, with the name the admin shows.
ASK, REVIEW, DIGEST, KNOWLEDGE, PERIODIC, CPD, WATCH, FMM, FMM_RULES, FMM_AP_REVIEW = (
    "ask",
    "review",
    "digest",
    "knowledge",
    "periodic",
    "cpd",
    "watch",
    "fmm",
    "fmm_rules",
    "fmm_ap_review",
)
FEATURES = {
    ASK: "Ask NeuroDB",
    REVIEW: "Daily review",
    DIGEST: "What's new note",
    KNOWLEDGE: "Document summaries",
    PERIODIC: "Periodic report figures",
    CPD: "Country programme reading",
    WATCH: "NeuroDB Watch",
    FMM: "Monitoring insights",
    FMM_RULES: "Monitoring insights (AI checks)",
    FMM_AP_REVIEW: "Action points (AI review and summaries)",
}
MILLION = Decimal(1_000_000)
CENT = Decimal("0.01")


def _count(value: Any) -> int:
    """A token count as a whole number; anything unreadable counts as none."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def split(usage: Any) -> tuple[int, int, int]:
    """The (input, cached, output) tokens of one model call's ``usage``, counted as
    ``agent.Outcome.add_usage`` counts them: OpenAI includes the cached prompt tokens in input_tokens,
    so they are taken out of it and kept apart (input + cached is the whole prompt). An
    ``agent.Outcome``, whose tokens are already added up and split, is read as it is. No usage (a fake
    or an older response) gives zeros."""
    if not usage:
        return 0, 0, 0
    if hasattr(usage, "cache_read_tokens"):  # an agent.Outcome: the tokens of all its calls, split
        return _count(usage.input_tokens), _count(usage.cache_read_tokens), _count(usage.output_tokens)
    cached = _count(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0))
    prompt = _count(getattr(usage, "input_tokens", 0))
    return max(prompt - cached, 0), cached, _count(getattr(usage, "output_tokens", 0))


def record(feature: str, model: str, usage: Any = None, calls: int = 1) -> None:
    """Add ``calls`` model calls and the tokens of ``usage`` (one response's usage, or an
    ``agent.Outcome``) to today's row of ``feature`` and ``model`` (the model the call asked for).

    Called right after each call returns. It never fails its caller: any error is logged. Inside an
    open transaction it writes in a savepoint, so a failed write leaves the caller's work intact.
    """
    try:
        input_tokens, cached, output = split(usage)
        calls = _count(calls)
        if not (calls or input_tokens or cached or output):
            return
        key = {"day": timezone.localdate(), "feature": feature[:20], "model": (model or "")[:64]}
        added = {
            "calls": F("calls") + calls,
            "input_tokens": F("input_tokens") + input_tokens,
            "cached_tokens": F("cached_tokens") + cached,
            "output_tokens": F("output_tokens") + output,
        }
        with transaction.atomic():
            if AIUsage.objects.filter(**key).update(**added):
                return
            try:
                with transaction.atomic():
                    AIUsage.objects.create(
                        **key,
                        calls=calls,
                        input_tokens=input_tokens,
                        cached_tokens=cached,
                        output_tokens=output,
                    )
            except IntegrityError:  # another call wrote the day's first row in between: add to it
                AIUsage.objects.filter(**key).update(**added)
    except Exception:
        logger.exception("AI use of %s could not be recorded", feature)


def _rows(feature: str | Iterable[str] | None):
    rows = AIUsage.objects.filter(day=timezone.localdate())
    if feature is None:
        return rows
    if isinstance(feature, str):
        return rows.filter(feature=feature)
    return rows.filter(feature__in=list(feature))


def today_total(feature: str | Iterable[str] | None = None) -> int:
    """Tokens used today (the whole prompt, cached or not, and the output, reasoning included), by
    ``feature`` (one name or several), else by every feature on the key."""
    tokens = Sum(F("input_tokens") + F("cached_tokens") + F("output_tokens"))
    return int(_rows(feature).aggregate(tokens=tokens)["tokens"] or 0)


def today_calls(feature: str | Iterable[str] | None = None) -> int:
    """Model calls made today, by ``feature`` (one name or several), else by every feature."""
    return int(_rows(feature).aggregate(calls=Sum("calls"))["calls"] or 0)


# ------------------------------------------------------------------------------------- prices
def _price(name: str) -> Decimal | None:
    """One optional AI_PRICE_* setting: US dollars per million tokens, or None when it is not set
    (empty, zero or not a number)."""
    value = getattr(settings, name, None)
    if value in (None, ""):
        return None
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() and price > 0 else None


def prices() -> dict[str, Decimal] | None:
    """US dollars per million input, cached and output tokens, from the optional settings
    AI_PRICE_INPUT_PER_MTOK, AI_PRICE_CACHED_PER_MTOK and AI_PRICE_OUTPUT_PER_MTOK. None unless the
    input and output prices are set; cached tokens cost the input price when theirs is not set."""
    given = {
        "input": _price("AI_PRICE_INPUT_PER_MTOK"),
        "cached": _price("AI_PRICE_CACHED_PER_MTOK"),
        "output": _price("AI_PRICE_OUTPUT_PER_MTOK"),
    }
    if given["input"] is None or given["output"] is None:
        return None
    if given["cached"] is None:
        given["cached"] = given["input"]
    return given


def cost(row: AIUsage, price: dict[str, Decimal]) -> Decimal:
    """What one row cost in US dollars at ``price`` (from ``prices``)."""
    return (
        row.input_tokens * price["input"]
        + row.cached_tokens * price["cached"]
        + row.output_tokens * price["output"]
    ) / MILLION
