"""The figures of the Monitoring insights page, each from one function that takes a
:class:`~neurodb.fmm.scope.Scope` and reads the stored visits (never the eTools records).

This module holds the key figures (:func:`kpis`), the data notes that say where FMM counts differently
from the overview (:func:`notes`), and the shared definitions the other blocks reuse: the average
quality (:func:`avg_quality`, one function for the tile, the highlights, the AI facts and the chat
tools) and the urgency bands (:func:`urgency_counts`).

Every block is cached for ten minutes under ``fmm:v1:<scope hash>:<last refresh run>:<rules
version>:<day>:<block>`` (:func:`cached`), so a refresh, a rescore or a new day shows at once.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Q, QuerySet, Sum
from django.utils import timezone

from .scope import Scope

CACHE_SECONDS = 600
RATED = ("on_track", "constrained", "off_track")
STATUS_ORDER = ("reported", "in_progress", "planned", "cancelled", "unknown")
STATUS_WORDS = {
    "reported": "reported",
    "in_progress": "in progress",
    "planned": "planned",
    "cancelled": "cancelled",
    "unknown": "status unknown",
}


_READ = object()


def stamp(last: Any = _READ) -> str:
    """What every cached figure depends on besides its scope: the last refresh that finished (given,
    or read), the rules version it computed the scores with, and the day (rolling periods move at
    midnight)."""
    from . import status

    last = status.last_refresh() if last is _READ else last
    version = (last.details or {}).get("rules_version", 0) if last else 0
    return f"{last.pk if last else 0}:{version}:{timezone.localdate().isoformat()}"


def cached(scope: Scope, block: str, compute: Callable[[], Any], when: str | None = None) -> Any:
    """``compute()`` for ``scope``, kept ten minutes (not in DEBUG); ``when`` is :func:`stamp`, read
    once per request by the caller."""
    if settings.DEBUG:
        return compute()
    key = f"fmm:v1:{scope.hash()}:{when if when is not None else stamp()}:{block}"
    found = cache.get(key)
    if found is not None:
        return found
    value = compute()
    cache.set(key, value, CACHE_SECONDS)
    return value


# The aggregates the average quality is computed from, so a block that aggregates the visits anyway
# reads them in the same query (:func:`mean_quality`)
QUALITY_AGGREGATES = {
    "quality_sum": Sum("quality_score"),
    "quality_n": Count("pk", filter=Q(quality_score__isnull=False)),
}


def mean_quality(quality_sum: Any, quality_n: int) -> Decimal | None:
    """The mean of scores whose sum and count are given, rounded half up to one decimal (exactly
    ``score.average_quality`` of the same scores); None when nothing is scored."""
    from .rules import half_up

    if not quality_n:
        return None
    return half_up(Decimal(str(quality_sum)) / quality_n, 1)


def avg_quality(visits_qs: QuerySet) -> Decimal | None:
    """The average quality score of the scored visits of ``visits_qs`` (half up, one decimal), or
    None when none is scored. The one definition of the KPI, the highlights, the AI facts and the
    chat tools."""
    return mean_quality(**visits_qs.aggregate(**QUALITY_AGGREGATES))


def thresholds(setting=None) -> dict[str, int]:
    """The urgency and band thresholds administrators set (Score settings, given or read)."""
    from .models import ScoreSetting

    s = setting or ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    return {
        "red": s.urgency_red,
        "amber": s.urgency_amber,
        "band_high": s.band_high,
        "band_medium": s.band_medium,
    }


def urgency_counts(visits_qs: QuerySet, limits: dict[str, int] | None = None) -> dict[str, int]:
    """``{"red": n, "amber": n}``: visits at or above the red threshold, and between amber and red."""
    limits = limits or thresholds()
    return visits_qs.aggregate(
        red=Count("pk", filter=Q(urgency__gte=limits["red"])),
        amber=Count("pk", filter=Q(urgency__gte=limits["amber"], urgency__lt=limits["red"])),
    )


def kpis(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """The four key figures: visits (with the status breakdown), monitored entities (rated / not
    monitored), the average quality (with the scored visits and the rules version) and the visits
    of high urgency (with the amber ones)."""

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        visits = scope.visits()
        counts = visits.aggregate(
            total=Count("pk"),
            red=Count("pk", filter=Q(urgency__gte=limits["red"])),
            amber=Count("pk", filter=Q(urgency__gte=limits["amber"], urgency__lt=limits["red"])),
            scored=Count("pk", filter=Q(quality_score__isnull=False)),
            version=Max("rules_version"),
            **QUALITY_AGGREGATES,
            **{group: Count("pk", filter=Q(status_group=group)) for group in STATUS_ORDER},
        )
        entities = scope.entities().aggregate(
            total=Count("pk"),
            rated=Count("pk", filter=Q(rating__in=RATED)),
            not_monitored=Count("pk", filter=Q(rating="not_monitored")),
        )
        breakdown = [
            {"group": g, "n": counts[g], "label": STATUS_WORDS[g]}
            for g in STATUS_ORDER
            if counts[g] or g != "unknown"
        ]
        return {
            "visits": counts["total"],
            "by_status": breakdown,
            "entities": entities["total"],
            "entities_rated": entities["rated"],
            "entities_not_monitored": entities["not_monitored"],
            "entities_other": entities["total"] - entities["rated"] - entities["not_monitored"],
            "avg_quality": mean_quality(counts["quality_sum"], counts["quality_n"]),
            "scored": counts["scored"],
            "rules_version": counts["version"] or 0,
            "high_urgency": counts["red"],
            "amber": counts["amber"],
            "red_at": limits["red"],
            "amber_at": limits["amber"],
        }

    return cached(scope, "kpis", compute, when)


def notes(scope: Scope, when: str | None = None) -> list[dict[str, Any]]:
    """The data notes of the scope: where FMM counts differently from the overview, each line only
    when it applies, with its count (``{"key", "n", "text"}``)."""

    def compute() -> list[dict[str, Any]]:
        within = Q(end_date__gte=scope.start, end_date__lte=scope.end)
        counts = scope.filtered().aggregate(
            via_site=Count("pk", filter=within & Q(location=None, site__isnull=False)),
            no_reference=Count("pk", filter=within & Q(reference="")),
            no_date=Count("pk", filter=Q(end_date=None)),
        )
        lines: list[dict[str, Any]] = []
        if scope.sections:
            lines.append({"key": "section", "n": None})
        if scope.governorate and scope.governorate != "none" and counts["via_site"]:
            lines.append({"key": "via_site", "n": counts["via_site"]})
        for key in ("no_reference", "no_date"):
            if counts[key]:
                lines.append({"key": key, "n": counts[key]})
        if scope.entity_filtered:
            lines.append({"key": "entity_filter", "n": None})
        return lines

    return cached(scope, "notes", compute, when)
