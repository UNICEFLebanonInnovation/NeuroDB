"""The figures of the Monitoring insights page, each from one function that takes a
:class:`~neurodb.fmm.scope.Scope` and reads the stored visits (never the eTools records).

This module holds the key figures (:func:`kpis`), the data notes that say where FMM counts differently
from the overview (:func:`notes`), the shared definitions the other blocks reuse (the average quality,
:func:`avg_quality`, one function for the tile, the highlights, the AI facts and the chat tools; the
urgency bands, :func:`urgency_counts`), and the blocks of the Quality and Analysis tabs: quality and
visits by month, HACT Q1 by month, the score distribution, recurring issues, places, rule analysis,
flags, highlights, governorates, field offices, entities, sections, ratings, points per rule, HACT
programmatic visits and follow-up.

Most blocks read one pass over the scope's visits (:func:`summary`), so a tab runs a handful of
queries whatever the number of visits; the rule results, entity rows, HACT figures and action points
have one query each. Every drill value a block gives (``drill``) is a code the scope parses, and the
visits a drill-down lists are exactly those the block counted.

Every block is cached for ten minutes under ``fmm:v1:<scope hash>:<last refresh run>:<rules
version>:<day>:<block>`` (:func:`cached`), so a refresh, a rescore or a new day shows at once.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.dateformat import format as date_format

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
    once per request by the caller. A scope that drills into the reviews is never kept: a review
    saved a minute ago must count at once, and no refresh marks it."""
    if settings.DEBUG or any(key == "review" for key, _value in scope.drill):
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
        "high_flag": s.high_flag_count,
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


# ------------------------------------------------------------------------------------------ shared
BANDS = ("high", "medium", "low")
BAND_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
BAND_COLORS = {"high": "--nd-success", "medium": "--nd-warning", "low": "--nd-danger"}
RATINGS = ("on_track", "constrained", "off_track", "not_monitored")
RATING_COLORS = {
    "on_track": "--nd-success",
    "constrained": "--nd-warning",
    "off_track": "--nd-danger",
    "not_monitored": "--nd-muted",
}
BUCKET_COLORS = {
    "0-20": "--nd-danger",
    "20-40": "--nd-danger",
    "40-60": "--nd-warning",
    "60-80": "--nd-info",
    "80-100": "--nd-success",
}
FLAG_ROWS = (  # (drill, label, meter colour)
    ("0", "No flags", "success"),
    ("1", "1 flag", "info"),
    ("2", "2 flags", "warning"),
    ("3+", "3 or more flags", "danger"),
)
MONTHS_MAX = 36  # the latest months a monthly chart draws
SECTION_LINES = 50  # visit lines listed under a section
PLACES_MAX = 500  # places kept for the location tables
CHIP_VISITS = 6  # visit chips on a recurring issue
FOLLOW_UP_LIST = 10
QUESTION_ANSWERS = ("fm_questions.answer", "fm_questions.answer_label")


def _today():
    from . import scope as scope_module

    return scope_module._today(None)


def _quality(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _pct(part: int, whole: int) -> Decimal | None:
    """``part`` of ``whole`` in % (half up, one decimal); None when ``whole`` is 0."""
    from .rules import half_up

    return half_up(Decimal(100 * part) / whole, 1) if whole else None


def _visit_name(key: str, label: str, activity_id: int | None) -> str:
    """ "#1804" for an eTools activity, else the visit's own label."""
    return f"#{activity_id}" if activity_id else label


def _not_rated_yet(rating: str, status_group: str) -> bool:
    return rating == "not_monitored" and status_group in ("planned", "in_progress")


class Tally:
    """Visits counted with their quality scores, score bands and ratings."""

    __slots__ = ("bands", "q_n", "q_sum", "ratings", "visits")

    def __init__(self) -> None:
        self.visits = 0
        self.q_sum = Decimal(0)
        self.q_n = 0
        self.bands: Counter = Counter()
        self.ratings: Counter = Counter()

    def add(self, quality: Decimal | None, band: str, rating: str) -> None:
        self.visits += 1
        if quality is not None:
            self.q_sum += quality
            self.q_n += 1
        if band:
            self.bands[band] += 1
        self.ratings[rating] += 1

    def out(self) -> dict[str, Any]:
        return {
            "visits": self.visits,
            "avg": mean_quality(self.q_sum, self.q_n),
            "scored": self.q_n,
            "bands": {band: self.bands[band] for band in BANDS},
            "ratings": {rating: self.ratings[rating] for rating in RATINGS},
        }


def _bucket(quality: Decimal) -> str:
    """The score bucket of a score, as the ``bucket`` drill-down reads it (80-100 holds 100)."""
    from .scope import BUCKETS

    for drill, (low, high) in BUCKETS.items():
        if quality >= low and (quality < high or (high == 100 and quality <= high)):
            return drill
    return ""


# The columns of the one pass over the scope's visits most blocks are computed from
SUMMARY_COLUMNS = (
    "key",
    "label",
    "activity_id",
    "end_date",
    "status_group",
    "rating",
    "hact_q1",
    "quality_score",
    "score_band",
    "flags",
    "flag_count",
    "offices",
    "offices_from",
    "section_names",
    "governorate_key",
    "location_id",
    "location__name",
    "site_id",
    "place_name",
    "governorate_name",
    "partner_ids",
    "psea_flag",
    "entities",
    "entities_rated",
    "action_points",
)


def summary(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """One pass over the scope's visits, kept with the other blocks: the figures by month, score
    bucket, flag count, rating, field office, section and place, the governorates visited, the
    partners, and the visits without follow-up. Every count here is a count of visits."""

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        high_flag = limits.get("high_flag", 3)
        months: dict[str, dict[str, Any]] = {}
        everyone = Tally()
        by_rating: dict[str, Tally] = {r: Tally() for r in RATINGS}
        buckets: Counter = Counter()
        flags_dist: Counter = Counter()
        rule_flags: Counter = Counter()
        offices: dict[str, dict[str, Any]] = {}
        sections: dict[str, dict[str, Any]] = {}
        places: dict[tuple, dict[str, Any]] = {}
        office_sources: Counter = Counter()
        governorates: set[str] = set()
        partners: set[int] = set()
        no_follow_up: list[tuple[str, str]] = []
        counts: Counter = Counter()
        rows = scope.visits().order_by("end_date", "key").values_list(*SUMMARY_COLUMNS)
        for row in rows.iterator(chunk_size=2000):
            v = dict(zip(SUMMARY_COLUMNS, row, strict=True))
            quality = _quality(v["quality_score"])
            band, rating, group = v["score_band"], v["rating"] or "not_monitored", v["status_group"]
            everyone.add(quality, band, rating)
            if rating in by_rating:
                by_rating[rating].add(quality, band, rating)
            counts["reported"] += group == "reported"
            counts["off_track"] += rating == "off_track"
            counts["gaps"] += group == "reported" and not v["entities_rated"]
            counts["psea_asked"] += v["psea_flag"] is not None
            counts["psea_flagged"] += v["psea_flag"] is True
            counts["no_place"] += v["location_id"] is None and v["site_id"] is None
            counts["unlocated"] += not v["governorate_key"]
            for code in v["flags"] or ():
                rule_flags[code] += 1
            if quality is None:
                buckets["none"] += 1
            else:
                buckets[_bucket(quality)] += 1
                count = v["flag_count"] or 0
                flags_dist["3+" if count >= 3 else str(count)] += 1
                counts["high_flag"] += count >= high_flag
            # by month (the end date: every visit here has one)
            month = v["end_date"].strftime("%Y-%m")
            m = months.setdefault(month, {"visits": 0, "reported": 0, "q_sum": Decimal(0), "q_n": 0})
            m["visits"] += 1
            m["reported"] += group == "reported"
            if quality is not None:
                m["q_sum"] += quality
                m["q_n"] += 1
            if v["hact_q1"]:
                m[f"q1:{v['hact_q1']}"] = m.get(f"q1:{v['hact_q1']}", 0) + 1
            m[f"rating:{rating}"] = m.get(f"rating:{rating}", 0) + 1
            # field offices ("none": office not known); a visit counts in each of its offices
            office_sources[v["offices_from"] or "none"] += 1
            for name in v["offices"] or ["none"]:
                o = offices.setdefault(name, {"tally": Tally(), "from": Counter(), "flags": Counter()})
                o["tally"].add(quality, band, rating)
                o["from"][v["offices_from"] or "none"] += 1
                if quality is not None:
                    o["flags"].update(v["flags"] or ())
            # sections ("none": no section); a visit counts in each of its sections
            line = {
                "key": v["key"],
                "name": _visit_name(v["key"], v["label"], v["activity_id"]),
                "quality": quality,
                "band": band,
                "rating": rating,
                "not_rated_yet": _not_rated_yet(rating, group),
                "date": v["end_date"],
            }
            for name in v["section_names"] or ["none"]:
                s = sections.setdefault(name, {"tally": Tally(), "lines": [], "flagged": 0})
                s["tally"].add(quality, band, rating)
                s["lines"].append(line)
                s["flagged"] += bool(v["flags"])
            # places: the gazetteer location, else the place eTools wrote (a site or a name)
            if v["location_id"] is not None:
                place_key: tuple | None = ("location", v["location_id"])
                name = v["location__name"] or v["place_name"]
            elif v["place_name"]:
                place_key, name = ("place", v["place_name"]), v["place_name"]
            else:
                place_key = None
            if place_key is not None:
                p = places.setdefault(
                    place_key,
                    {
                        "name": name,
                        "location_id": v["location_id"],
                        "governorate": v["governorate_name"],
                        "tally": Tally(),
                        "last": None,
                        "entities": 0,
                        "rated": 0,
                    },
                )
                p["tally"].add(quality, band, rating)
                p["last"] = max(p["last"] or v["end_date"], v["end_date"])
                p["entities"] += v["entities"] or 0
                p["rated"] += v["entities_rated"] or 0
                p["governorate"] = p["governorate"] or v["governorate_name"]
            if v["governorate_key"]:
                governorates.add(v["governorate_key"])
            partners.update(v["partner_ids"] or ())
            worse = max([rating, v["hact_q1"] or "not_monitored"], key=_rating_rank)
            if group == "reported" and worse in ("off_track", "constrained") and not v["action_points"]:
                no_follow_up.append((v["key"], _visit_name(v["key"], v["label"], v["activity_id"])))

        out_sections = {}
        for name, s in sections.items():
            lines = sorted(
                s["lines"],
                key=lambda ln: (ln["quality"] is None, ln["quality"] or 0, ln["date"], ln["key"]),
            )
            out_sections[name] = {
                **s["tally"].out(),
                "flagged": s["flagged"],
                "lines": lines[:SECTION_LINES],
                "more": max(len(lines) - SECTION_LINES, 0),
            }
        out_places = sorted(
            (
                {
                    "name": p["name"],
                    "location_id": p["location_id"],
                    "governorate": p["governorate"],
                    "last": p["last"],
                    "entities": p["entities"],
                    "rated": p["rated"],
                    **p["tally"].out(),
                }
                for p in places.values()
            ),
            key=lambda p: (-p["visits"], -(p["last"].toordinal() if p["last"] else 0), p["name"].casefold()),
        )
        return {
            **everyone.out(),
            "reported": counts["reported"],
            "off_track": counts["off_track"],
            "gaps": counts["gaps"],
            "psea_asked": counts["psea_asked"],
            "psea_flagged": counts["psea_flagged"],
            "no_place": counts["no_place"],
            "unlocated": counts["unlocated"],
            "high_flag": counts["high_flag"],
            "high_flag_at": high_flag,
            "buckets": dict(buckets),
            "flags": dict(flags_dist),
            "rule_flags": dict(rule_flags),
            "months": months,
            "by_rating": {code: t.out() for code, t in by_rating.items()},
            "offices": {
                name: {**o["tally"].out(), "from": dict(o["from"]), "flags": dict(o["flags"])}
                for name, o in offices.items()
            },
            "office_sources": dict(office_sources),
            "sections": out_sections,
            "places": out_places[:PLACES_MAX],
            "places_total": len(out_places),
            "governorates": sorted(governorates),
            "partners": sorted(partners),
            "no_follow_up": {"n": len(no_follow_up), "visits": no_follow_up[-FOLLOW_UP_LIST:][::-1]},
        }

    return cached(scope, "summary", compute, when)


def _rating_rank(code: str) -> int:
    from neurodb.datamart.fm import RATING_ORDER

    return RATING_ORDER.get(code, 0)


# ------------------------------------------------------------------------------------------ by month
def _month_axis(scope: Scope, seen: Any) -> list[datetime.date]:
    """The first day of each month of the period, up to this month or the latest month with a visit
    (whichever is later), at most the latest MONTHS_MAX."""
    first = scope.start.replace(day=1)
    latest = max([datetime.date.fromisoformat(f"{m}-01") for m in seen] or [first])
    last = min(scope.end.replace(day=1), max(_today().replace(day=1), latest))
    out = []
    day = first
    while day <= last:
        out.append(day)
        day = (day + datetime.timedelta(days=32)).replace(day=1)
    return out[-MONTHS_MAX:]


def _months(scope: Scope, when: str | None, limits: dict | None) -> tuple[list[datetime.date], dict]:
    data = summary(scope, when, limits)
    return _month_axis(scope, data["months"]), data["months"]


def _month_label(day: datetime.date) -> str:
    return date_format(day, "M Y")


def monthly_quality(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 4: the average quality of the visits that ended each month (bars) and the reported visits
    (line); ``{}`` when no visit of the scope is scored. A bar opens the visits of its month."""
    axis, months = _months(scope, when, limits)
    if not any(m["q_n"] for m in months.values()):
        return {}
    keys = [d.strftime("%Y-%m") for d in axis]
    values = [
        _float(mean_quality(months[k]["q_sum"], months[k]["q_n"])) if k in months else None for k in keys
    ]
    return {
        "months": [_month_label(d) for d in axis],
        "indicators": [
            {
                "id": "q",
                "label": "Average quality",
                "unit": "%",
                "values": values,
                "reports": [months.get(k, {}).get("reported", 0) for k in keys],
            }
        ],
        "bar_name": "Average quality",
        "line_name": "Reports",
        "line_unit": "reports",
        "drill": {"labels": keys, "series": {}},
    }


def monthly_volume(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 5: the visits that ended each month (bars, whatever their status) and their average
    quality (line); ``{}`` when the scope has no visit."""
    axis, months = _months(scope, when, limits)
    if not months:
        return {}
    keys = [d.strftime("%Y-%m") for d in axis]
    return {
        "months": [_month_label(d) for d in axis],
        "indicators": [
            {
                "id": "visits",
                "label": "Visits",
                "unit": "visits",
                "values": [months.get(k, {}).get("visits", 0) for k in keys],
                "reports": [
                    _float(mean_quality(months[k]["q_sum"], months[k]["q_n"])) if k in months else None
                    for k in keys
                ],
            }
        ],
        "bar_name": "Visits",
        "line_name": "Average quality",
        "line_unit": "%",
        "drill": {"labels": keys, "series": {}},
    }


def _by_month(scope: Scope, when, limits, prefix: str, codes: tuple[str, ...]) -> dict[str, Any] | None:
    from .scope import RATING_LABELS

    axis, months = _months(scope, when, limits)
    keys = [d.strftime("%Y-%m") for d in axis]
    totals = {code: sum(m.get(f"{prefix}:{code}", 0) for m in months.values()) for code in codes}
    if not any(totals.values()):
        return None
    return {
        "labels": [_month_label(d) for d in axis],
        "series": {
            RATING_LABELS[code]: [months.get(k, {}).get(f"{prefix}:{code}", 0) for k in keys]
            for code in codes
        },
        "colors": {RATING_LABELS[code]: RATING_COLORS[code] for code in codes},
        "drill": {"labels": keys, "series": {RATING_LABELS[code]: code for code in codes}},
        "totals": [{"code": code, "label": RATING_LABELS[code], "n": totals[code]} for code in codes],
        "key": prefix,
    }


def hact_q1_by_month(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict | None:
    """Block 6: the visits of each month by their HACT Q1 (the worst of the visit's Q1 answers):
    On track, Constrained, Off track. None when no visit of the scope has a Q1 rating."""
    return _by_month(scope, when, limits, "q1", ("on_track", "constrained", "off_track"))


def rating_by_month(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict | None:
    """Block 6 without Q1 answers: the visits of each month by their overall rating."""
    return _by_month(scope, when, limits, "rating", RATINGS)


def q1_question(when: str | None = None) -> str:
    """The HACT Q1 question as eTools writes it (the most frequent text of the answers whose question
    is Q1, in the whole data), "" when no Q1 question was found. Kept ten minutes per refresh."""
    from .models import QuestionAnswer

    key = f"fmm:v1:q1:{when if when is not None else stamp()}"
    found = None if settings.DEBUG else cache.get(key)
    if found is not None:
        return found
    found = (
        QuestionAnswer.objects.filter(role="q1")
        .values("question_text")
        .annotate(n=Count("pk"))
        .order_by("-n", "question_text")
        .values_list("question_text", flat=True)
        .first()
    )
    found = "" if found is None else (found or "HACT Q1")
    if not settings.DEBUG:
        cache.set(key, found, CACHE_SECONDS)
    return found


# ------------------------------------------------------------------------------------------ quality
def score_buckets(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 7: scored visits in five score buckets of 20 points (80–100 holds 100), each with its
    drill value, and the visits not scored."""
    from .scope import BUCKETS

    data = summary(scope, when, limits)
    found = data["buckets"]
    items = [
        {
            "label": f"{low}–{high}",
            "value": found.get(drill, 0),
            "color": BUCKET_COLORS[drill],
            "drill": drill,
        }
        for drill, (low, high) in BUCKETS.items()
    ]
    return {"items": items if data["scored"] else [], "not_scored": found.get("none", 0)}


def issue_label(rule: str, key: str, measures: list[float], setting: Any) -> str:
    """A recurring issue in words, written by NeuroDB from the rule and its detail code."""
    from .rules import R1_LABELS, number

    limit = number(setting.threshold) if setting is not None and setting.threshold is not None else ""
    low = min(measures) if measures else None
    if rule == "R1" and key.startswith("missing:"):
        missing = [R1_LABELS.get(part, part) for part in key.split(":", 1)[1].split(",") if part]
        return "R1: Incomplete monitoring report — missing: " + ", ".join(missing)
    if rule == "R2" and key == "below_threshold":
        text = f"R2: fewer than {limit}% of questions answered"
        return text + (f" (lowest {number(low)}%)" if low is not None else "")
    if rule in ("R4", "R5") and key in ("too_short", "q3_short"):
        what = "narrative" if rule == "R4" else "Q3 answer"
        text = f"{rule}: {what} shorter than {limit} words"
        return text + (f" (shortest {int(low)})" if low is not None else "")
    fixed = {
        ("R3", "q1_missing"): "R3: HACT Q1 not answered",
        ("R3", "q1_unrecognised"): "R3: HACT Q1 answer is not a rating",
        ("R3", "conflict"): "R3: HACT Q1 contradicts the overall finding",
        ("R4", "placeholder"): "R4: narrative is a placeholder",
        ("R4", "copied"): "R4: narrative identical to another visit's",
        ("R5", "q3_missing"): "R5: Q3 not answered",
        ("R5", "q3_placeholder"): "R5: Q3 answer is a placeholder",
        ("R6", "contradiction"): "R6: narrative contradicts the rating",
        ("R6", "not_monitored_described"): "R6: entity rated Not monitored described without a reason",
    }
    if (rule, key) in fixed:
        return fixed[(rule, key)]
    return f"{rule} {setting.label if setting is not None else ''}: flagged".replace("  ", " ")


def top_issues(
    scope: Scope, limit: int = 10, when: str | None = None, rules: list | None = None
) -> list[dict[str, Any]]:
    """Block 8: the flags of the scope grouped by rule and detail (``R2:below_threshold``), most
    frequent first: the issue in words, its visits, their mean urgency and the first visits."""
    from .models import RuleSetting, VisitRuleResult
    from .rules import half_up
    from .scope import _ISSUE

    def compute() -> list[dict[str, Any]]:
        settings_ = {r.code: r for r in (rules if rules is not None else RuleSetting.objects.all())}
        rows = (
            VisitRuleResult.objects.filter(status="fail", visit__in=scope.visits().values("pk"))
            .order_by("-visit__urgency", "-visit__end_date", "visit__key")
            .values_list(
                "rule",
                "detail_key",
                "measure",
                "visit__key",
                "visit__label",
                "visit__activity_id",
                "visit__urgency",
            )
        )
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for rule, key, measure, visit_key, label, activity_id, urgency in rows:
            g = groups.setdefault((rule, key), {"visits": [], "urgency": 0, "measures": []})
            g["visits"].append({"key": visit_key, "name": _visit_name(visit_key, label, activity_id)})
            g["urgency"] += urgency or 0
            if measure is not None:
                g["measures"].append(measure)
        out = []
        for (rule, key), g in groups.items():
            n = len(g["visits"])
            drill = f"{rule}:{key}"
            out.append(
                {
                    "rule": rule,
                    "label": issue_label(rule, key, g["measures"], settings_.get(rule)),
                    "visits": n,
                    "urgency": int(half_up(Decimal(g["urgency"]) / n, 0)),
                    "lowest": min(g["measures"]) if g["measures"] else None,
                    "chips": g["visits"][:CHIP_VISITS],
                    "more": max(n - CHIP_VISITS, 0),
                    "drill": drill if _ISSUE.match(drill) else "",
                }
            )
        out.sort(key=lambda r: (-r["visits"], -r["urgency"], r["rule"], r["label"]))
        return out

    return cached(scope, "issues", compute, when)[:limit]


def locations(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Blocks 9 and 19: the places of the scope's visits (the gazetteer location, else the site or
    place eTools wrote), most visited first: visits, last visit, average quality ("—" when no visit
    is scored), and coverage (rated entities ÷ entities); plus the visits with no place at all."""
    data = summary(scope, when, limits)
    rows = []
    for p in data["places"]:
        rows.append(
            {
                **p,
                "coverage": _pct(p["rated"], p["entities"]),
                "drill": str(p["location_id"]) if p["location_id"] is not None else "",
            }
        )
    return {"rows": rows, "total": data["places_total"], "unlinked": data["no_place"]}


def rule_stats(scope: Scope, when: str | None = None) -> dict[str, dict[str, Any]]:
    """Per rule over the scope's visits: the visits in each result state, and the points earned (each
    capped at its maximum) over the points evaluated."""
    from django.db.models import DecimalField
    from django.db.models.functions import Cast, Least

    from .models import VisitRuleResult
    from .rules import STATES

    def compute() -> dict[str, dict[str, Any]]:
        evaluated = Q(status__in=("pass", "fail"), max_points__gt=0)
        rows = (
            VisitRuleResult.objects.filter(visit__in=scope.visits().values("pk"))
            .order_by()
            .values("rule")
            .annotate(
                **{state: Count("pk", filter=Q(status=state)) for state in STATES},
                earned=Sum(
                    Least("points", Cast("max_points", DecimalField(max_digits=5, decimal_places=1))),
                    filter=evaluated,
                ),
                points_max=Sum("max_points", filter=evaluated),
                points_n=Count("pk", filter=evaluated),
            )
        )
        return {row.pop("rule"): row for row in rows}

    return cached(scope, "rules", compute, when)


NEEDS_ANSWERS = ("R2", "R3", "R5")


def _rule_settings(rules: list | None) -> list:
    """The rules as administrators set them (given by the page, which reads them once, or read here)."""
    from .models import RuleSetting

    return list(rules) if rules is not None else list(RuleSetting.objects.order_by("code"))


def rule_analysis(scope: Scope, rules: list | None = None, when: str | None = None) -> list[dict[str, Any]]:
    """Block 10: per rule, the visits flagged out of the visits evaluated (passed or flagged), the
    visits where it was not available, "off" for a rule switched off and "flag only" for 0 points."""
    rules = _rule_settings(rules)
    stats = rule_stats(scope, when)
    out = []
    for rule in sorted(rules, key=lambda r: r.code):
        s = stats.get(rule.code, {})
        flagged, evaluated = s.get("fail", 0), s.get("fail", 0) + s.get("pass", 0)
        share = _pct(flagged, evaluated)
        if not rule.enabled:
            state = "off"
        elif not evaluated and s.get("na", 0):
            state = "na"
        elif not evaluated:
            state = "none"
        else:
            state = "ok"
        out.append(
            {
                "code": rule.code,
                "label": rule.label,
                "description": rule.description,
                "points": rule.points,
                "flag_only": not rule.points,
                "state": state,
                "flagged": flagged,
                "evaluated": evaluated,
                "na": s.get("na", 0),
                "share": share,
                "fill": float(share or 0),
                "level": "danger"
                if share is not None and share >= 50
                else "warning"
                if flagged
                else "success",
                "needs_answers": rule.code in NEEDS_ANSWERS,
            }
        )
    return out


def issues_summary(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 11: R6 failures and high-flag visits (with their share of the scored visits), and the
    monitoring gaps: reported visits none of whose entities is rated (§0.3)."""
    data = summary(scope, when, limits)
    scored = data["scored"]
    r6 = data["rule_flags"].get("R6", 0)
    return {
        "scored": scored,
        "r6": {"n": r6, "pct": _pct(r6, scored)},
        "gaps": {"n": data["gaps"]},
        "high_flag": {
            "n": data["high_flag"],
            "pct": _pct(data["high_flag"], scored),
            "at": data["high_flag_at"],
        },
    }


def flag_distribution(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 12: the scored visits by their number of flags (0, 1, 2, 3 or more)."""
    data = summary(scope, when, limits)
    scored = data["scored"]
    rows = []
    for drill, label, color in FLAG_ROWS:
        n = data["flags"].get(drill, 0)
        share = _pct(n, scored)
        rows.append(
            {"drill": drill, "label": label, "color": color, "n": n, "pct": share, "fill": float(share or 0)}
        )
    return {"rows": rows, "scored": scored}


# ------------------------------------------------------------------------------------------ analysis
def coverage(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """The governorates covered: the gazetteer's governorates (``overview.governorate_names``) with
    at least one visit of the scope, out of all of them."""
    from neurodb.reports.overview import governorate_names

    def compute() -> dict[str, Any]:
        names = governorate_names()
        visited = [key for key in summary(scope, when, limits)["governorates"] if key in names]
        return {"covered": len(visited), "total": len(names), "visited": visited, "names": names}

    return cached(scope, "coverage", compute, when)


def governorate_gaps(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 14: the gazetteer's governorates no visit of the scope went to (only the one chosen when
    the filter names a governorate), and the visits whose governorate is not known."""
    found = coverage(scope, when, limits)
    keys = [k for k in found["names"] if k not in found["visited"]]
    if scope.governorate and scope.governorate != "none":
        keys = [k for k in keys if k == scope.governorate]
    names = [found["names"][k] for k in keys if (found["names"][k] or "").strip()]
    return {"names": names, "unlocated": summary(scope, when, limits)["unlocated"]}


def _within(values: tuple[str, ...], name: str) -> bool:
    """A row of a multi-valued block (office, section) is shown when the filter keeps it."""
    return not values or name in values


def offices(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 15: per field office, its visits, their average quality and where the office came from;
    the visits whose office is not known in one row. A visit to a PD with two offices counts in both."""
    data = summary(scope, when, limits)
    rows, unknown = [], None
    for name, o in data["offices"].items():
        if not _within(scope.offices, name):
            continue
        row = {"name": name, "drill": name, **o}
        if name == "none":
            unknown = row
        else:
            rows.append(row)
    rows.sort(key=lambda r: (-r["visits"], r["name"].casefold()))
    sources = data["office_sources"]
    return {
        "rows": rows,
        "unknown": unknown,
        "sources": {k: sources.get(k, 0) for k in ("activity", "pd", "action_point")},
        "known": bool(rows),
    }


def office_rule_badges(
    scope: Scope, rules: list | None = None, when: str | None = None, limits: dict | None = None
) -> list[dict[str, Any]]:
    """Block 17: per field office, its scored visits and, for each rule with flags, the scored visits it
    flagged ("R1: 6/16")."""
    rules = _rule_settings(rules)
    data = summary(scope, when, limits)
    labels = {r.code: r.label for r in rules}
    out = []
    for name, o in data["offices"].items():
        if not _within(scope.offices, name):
            continue
        scored = o["scored"]
        badges = [
            {
                "rule": code,
                "label": labels.get(code, code),
                "flagged": n,
                "total": scored,
                "level": "danger" if scored and 2 * n >= scored else "warning",
            }
            for code, n in sorted(o["flags"].items())
            if n
        ]
        out.append({"name": name, "drill": name, "scored": scored, "visits": o["visits"], "badges": badges})
    out.sort(key=lambda r: (r["name"] == "none", -r["scored"], r["name"].casefold()))
    return out


def sections(scope: Scope, when: str | None = None, limits: dict | None = None) -> list[dict[str, Any]]:
    """Block 18: per eTools section, its visits, their average quality, ratings and bands, and a line
    per visit (worst score first, unscored last). A visit with two sections counts in both."""
    data = summary(scope, when, limits)
    out = [
        {"name": name, "drill": name, **s}
        for name, s in data["sections"].items()
        if _within(scope.sections, name)
    ]
    out.sort(key=lambda r: (r["name"] == "none", -r["visits"], r["name"].casefold()))
    return out


def quality_by_rating(
    scope: Scope, when: str | None = None, limits: dict | None = None
) -> list[dict[str, Any]]:
    """Block 20: per visit rating, its visits, their average quality and bands."""
    from .scope import RATING_LABELS

    data = summary(scope, when, limits)
    out = []
    for code in RATINGS:
        row = data["by_rating"][code]
        if row["visits"] and _within(scope.ratings, code):
            avg = row["avg"]
            out.append({"code": code, "label": RATING_LABELS[code], "fill": float(avg or 0), **row})
    return out


def flag_frequency(scope: Scope, rules: list | None = None, when: str | None = None) -> dict[str, Any]:
    """Block 21: the visits each rule flagged, most first, as ``[label, visits, drill]`` triples for the
    chart, with each rule's share of the visits it evaluated."""
    rules = _rule_settings(rules)
    stats = rule_stats(scope, when)
    rows = []
    for rule in rules:
        if not rule.enabled:
            continue
        s = stats.get(rule.code, {})
        flagged, evaluated = s.get("fail", 0), s.get("fail", 0) + s.get("pass", 0)
        rows.append(
            {
                "code": rule.code,
                "label": f"{rule.code} {rule.label}",
                "n": flagged,
                "evaluated": evaluated,
                "pct": _pct(flagged, evaluated),
            }
        )
    rows.sort(key=lambda r: (-r["n"], r["code"]))
    return {"rows": rows, "pairs": [[r["label"], r["n"], r["code"]] for r in rows if r["n"]]}


def dimension_breakdown(scope: Scope, rules: list | None = None, when: str | None = None) -> dict[str, Any]:
    """Block 22: per rule with points, the mean points earned over the visits it evaluated, out of its
    maximum (capped at 100%), weakest first; rules never evaluated last as "not available"."""
    from .rules import half_up

    rules = _rule_settings(rules)
    stats = rule_stats(scope, when)
    rows, unavailable, flag_only = [], [], []
    for rule in sorted(rules, key=lambda r: r.code):
        if not rule.enabled:
            continue
        if not rule.points:
            flag_only.append(rule.code)
            continue
        s = stats.get(rule.code, {})
        n, earned, top = s.get("points_n", 0), s.get("earned"), s.get("points_max")
        if not n or not top:
            unavailable.append({"code": rule.code, "label": rule.label, "points": rule.points})
            continue
        pct = min(Decimal(100), half_up(Decimal(100) * Decimal(earned or 0) / Decimal(top), 1))
        rows.append(
            {
                "code": rule.code,
                "label": rule.label,
                "earned": half_up(Decimal(earned or 0) / n, 1),
                "max": half_up(Decimal(top) / n, 1),
                "pct": pct,
                "fill": float(pct),
                "visits": n,
            }
        )
    rows.sort(key=lambda r: (r["pct"], r["code"]))
    return {"rows": rows, "unavailable": unavailable, "flag_only": flag_only}


def highlights(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 13: visits, reported, governorates covered, the average quality (the KPI's own
    definition), off-track visits, PSEA-flagged visits out of those with a PSEA question, the share
    of High / Medium / Low scores and the monitored entities by kind."""
    data = summary(scope, when, limits)
    kinds = entity_rows(scope, when)["kinds"]
    from .scope import KIND_LABELS

    return {
        "visits": data["visits"],
        "reported": data["reported"],
        "coverage": coverage(scope, when, limits),
        "avg_quality": data["avg"],
        "scored": data["scored"],
        "off_track": data["off_track"],
        "psea": {"flagged": data["psea_flagged"], "asked": data["psea_asked"]},
        "bands": [
            {"label": BAND_LABELS[b], "value": data["bands"][b], "color": BAND_COLORS[b]}
            for b in BANDS
            if data["bands"][b]
        ],
        "kinds": [
            {"kind": kind, "label": KIND_LABELS[kind], "n": kinds.get(kind, 0)}
            for kind in KIND_LABELS
            if kinds.get(kind) or kind in ("pd", "cp_output", "partner")
        ],
    }


# ------------------------------------------------------------------------------------------ entities
ENTITY_COLUMNS = (
    "kind",
    "entity",
    "pd_id",
    "pd__number",
    "partner_id",
    "partner__short_name",
    "partner__name",
    "cp_output",
    "rating",
    "visit_id",
    "visit__end_date",
    "visit__quality_score",
    "visit__score_band",
    "visit__flags",
    "visit__status_group",
    "planned",
)


def _year(scope: Scope) -> int:
    """The calendar year a block of the scope reads plans for: the year chosen, else the period's last."""
    return scope.year if scope.preset == "year" and scope.year else scope.end.year


def entity_rows(scope: Scope, when: str | None = None) -> dict[str, Any]:
    """The monitored entities of the scope (the finding rows; an entity filter keeps the matching rows
    only): their count by kind, and per kind each entity with its visits, average quality, bands, most
    frequent flag and latest rating; PD rows add the visits planned for the year."""
    from django.db.models import F, OuterRef, Subquery

    from neurodb.datamart.models import PlannedVisits

    def compute() -> dict[str, Any]:
        year = _year(scope)
        planned = (
            PlannedVisits.objects.filter(intervention_id=OuterRef("pd_id"), year=year)
            .order_by()
            .values("intervention_id")
            .annotate(total=Sum(F("q1") + F("q2") + F("q3") + F("q4")))
            .values("total")[:1]
        )
        rows = scope.entities().annotate(planned=Subquery(planned)).values_list(*ENTITY_COLUMNS)
        kinds: Counter = Counter()
        groups: dict[str, dict[tuple, dict[str, Any]]] = {}
        for row in rows.iterator(chunk_size=2000):
            e = dict(zip(ENTITY_COLUMNS, row, strict=True))
            kind = e["kind"] or "other"
            kinds[kind] += 1
            text = (e["entity"] or "").strip()
            if kind == "pd" and e["pd_id"]:
                key, name, link = ("pd", e["pd_id"]), e["pd__number"] or text, ("pd", e["pd_id"])
            elif kind == "partner" and e["partner_id"]:
                name = e["partner__short_name"] or e["partner__name"] or text
                key, link = ("partner", e["partner_id"]), ("partner", e["partner_id"])
            elif kind == "cp_output":
                name = (e["cp_output"] or text).strip()
                key, link = ("text", name.casefold()), None
            else:
                key, name, link = ("text", text.casefold()), text, None
            if not name:
                continue
            g = groups.setdefault(kind, {}).setdefault(
                key, {"name": name, "link": link, "visits": {}, "planned": e["planned"]}
            )
            g["visits"][e["visit_id"]] = (
                e["visit__end_date"],
                _quality(e["visit__quality_score"]),
                e["visit__score_band"],
                tuple(e["visit__flags"] or ()),
                e["visit__status_group"],
                e["rating"],
            )
        out: dict[str, list[dict[str, Any]]] = {}
        for kind, entities in groups.items():
            rows_out = []
            for g in entities.values():
                tally, flags = Tally(), Counter()
                for _day, quality, band, visit_flags, _group, rating in g["visits"].values():
                    tally.add(quality, band, rating)
                    flags.update(visit_flags)
                last = max(g["visits"].values(), key=lambda v: v[0] or datetime.date.min)
                top = min(flags.items(), key=lambda kv: (-kv[1], kv[0])) if flags else None
                rows_out.append(
                    {
                        "name": g["name"],
                        "link": g["link"],
                        "planned": g["planned"],
                        "top_issue": {"rule": top[0], "n": top[1]} if top else None,
                        "last": {
                            "date": last[0],
                            "rating": last[5],
                            "not_rated_yet": _not_rated_yet(last[5], last[4]),
                        },
                        **tally.out(),
                    }
                )
            rows_out.sort(key=lambda r: (r["avg"] is None, r["avg"] or 0, -r["visits"], r["name"].casefold()))
            out[kind] = rows_out
        return {"kinds": dict(kinds), "entities": out, "year": year}

    return cached(scope, "entities", compute, when)


def entities_performance(scope: Scope, kind: str = "pd", when: str | None = None) -> dict[str, Any]:
    """Block 16: the entities of one kind, worst average quality first, unscored last."""
    data = entity_rows(scope, when)
    return {
        "kind": kind,
        "rows": data["entities"].get(kind, []),
        "kinds": data["kinds"],
        "year": data["year"],
    }


# ------------------------------------------------------------------------------------------ HACT, follow-up
def hact_programmatic(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """The partners of the scope with HACT programmatic visits required for the scope's year: required,
    planned and completed (eTools), FM programmatic visits (NeuroDB's count, §0.3) and the gap left
    (required minus completed in eTools), largest gap first."""
    from neurodb.datamart import fm
    from neurodb.datamart.models import PartnerHACTYear

    def compute() -> dict[str, Any]:
        year = _year(scope)
        partners = summary(scope, when, limits)["partners"]
        if scope.partners:
            partners = [p for p in partners if p in scope.partners]
        found = (
            PartnerHACTYear.objects.filter(year=year, partner_id__in=partners, pv_required__gt=0)
            .select_related("partner")
            .order_by("partner_id", "-pk")
        )
        rows: dict[int, dict[str, Any]] = {}
        counted = None
        for h in found:
            if h.partner_id in rows:
                continue
            if counted is None:
                counted = fm.programmatic_visits_by_partner(year)
            partner = h.partner
            rows[h.partner_id] = {
                "partner_id": h.partner_id,
                "name": (partner.short_name or partner.name) if partner else h.partner_name,
                "required": h.pv_required,
                "planned": h.pv_planned,
                "completed": h.pv_completed,
                "fm": counted.get(h.partner_id, 0),
                "gap": max(h.pv_required - (h.pv_completed or 0), 0),
            }
        out = sorted(rows.values(), key=lambda r: (-r["gap"], str(r["name"]).casefold()))
        return {"year": year, "rows": out}

    return cached(scope, "hact", compute, when)


def action_points(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Follow-up: the FM action points linked to the scope's visits (open, overdue, high priority and
    open), the reported off-track or constrained visits without one, and the overdue ones, oldest
    due date first, with their visits."""
    from neurodb.datamart.models import ActionPoint

    from .models import VisitActionPoint

    def compute() -> dict[str, Any]:
        today = _today()
        rows = (
            VisitActionPoint.objects.filter(visit__in=scope.visits().values("pk"))
            .order_by("action_point_id", "visit__key")
            .values_list(
                "action_point_id",
                "action_point__status",
                "action_point__due_date",
                "action_point__high_priority",
                "action_point__reference_number",
                "action_point__category",
                "visit__key",
                "visit__label",
                "visit__activity_id",
            )
        )
        points: dict[int, dict[str, Any]] = {}
        for pk, status_, due, high, reference, category, key, label, activity_id in rows:
            p = points.setdefault(
                pk,
                {
                    "reference": reference,
                    "category": category,
                    "due": due,
                    "high": bool(high),
                    "open": status_ in ActionPoint.OPEN_STATUSES,
                    "visits": [],
                },
            )
            p["visits"].append({"key": key, "name": _visit_name(key, label, activity_id)})
        open_ = [p for p in points.values() if p["open"]]
        overdue = [p for p in open_ if p["due"] and p["due"] < today]
        overdue.sort(key=lambda p: (p["due"], p["reference"]))
        return {
            "linked": len(points),
            "open": len(open_),
            "overdue": len(overdue),
            "high_open": sum(p["high"] for p in open_),
            "overdue_list": overdue[:10],
            "without": summary(scope, when, limits)["no_follow_up"],
        }

    return cached(scope, "action_points", compute, when)
