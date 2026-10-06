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

Every block is cached for ten minutes under ``fmm:v3:<scope hash>:<last refresh run>:<rules
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
from django.db.models import Count, Max, Min, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.dateformat import format as date_format

from neurodb.datamart.fm import RATING_ORDER

from .scope import CHART_BUCKETS, Scope

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
    key = f"fmm:v3:{scope.hash()}:{when if when is not None else stamp()}:{block}"
    found = cache.get(key)
    if found is not None:
        return found
    value = compute()
    cache.set(key, value, CACHE_SECONDS)
    return value


def kept(scope: Scope, block: str, when: str | None = None) -> Any:
    """What :func:`cached` keeps for ``scope``'s ``block`` (None when nothing is kept), never computed."""
    if settings.DEBUG or any(key == "review" for key, _value in scope.drill):
        return None
    return cache.get(f"fmm:v3:{scope.hash()}:{when if when is not None else stamp()}:{block}")


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


COUNTED_KINDS = ("pd", "cp_output", "partner")  # the kinds counted by name; the rest is "other"


def kpis(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """The four key figures: visits (with the status breakdown), monitored entities (rated, by rating,
    and Not monitored: planned, not conducted, a count apart), the average quality (with the scored
    visits and the rules version) and the visits of high urgency (with the amber ones; a visit
    without a score has no urgency)."""

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
            # Not monitored (planned, not conducted) on a reported visit only; on a planned or
            # in-progress visit a blank rating is "not rated yet", as for the visits (``counted_rating``)
            not_monitored=Count("pk", filter=Q(rating="not_monitored", visit__status_group="reported")),
            not_rated_yet=Count(
                "pk",
                filter=Q(rating="not_monitored", visit__status_group__in=("planned", "in_progress")),
            ),
            **{f"rating_{code}": Count("pk", filter=Q(rating=code)) for code in RATED},
            **{f"kind_{kind}": Count("pk", filter=Q(kind=kind)) for kind in COUNTED_KINDS},
        )
        by_kind = {kind: entities[f"kind_{kind}"] for kind in COUNTED_KINDS}
        by_kind["other"] = entities["total"] - sum(by_kind.values())  # "other", or no kind at all
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
            # the rated entities by rating: every share of ratings is over the rated ones only
            "entity_ratings": {code: entities[f"rating_{code}"] for code in RATED},
            "entities_not_rated_yet": entities["not_rated_yet"],
            "entities_other": entities["total"]
            - entities["rated"]
            - entities["not_monitored"]
            - entities["not_rated_yet"],
            "entity_kinds": {kind: n for kind, n in by_kind.items() if n},
            "avg_quality": mean_quality(counts["quality_sum"], counts["quality_n"]),
            "scored": counts["scored"],
            "rules_version": counts["version"] or 0,
            "high_urgency": counts["red"],
            "amber": counts["amber"],
            "red_at": limits["red"],
            "amber_at": limits["amber"],
        }

    return cached(scope, "kpis", compute, when)


def data_window(scope: Scope, when: str | None = None) -> dict[str, Any]:
    """The first and last end dates of the visits every filter but the period keeps ("Data available
    from X to Y", shown for all time and for custom dates)."""

    def compute() -> dict[str, Any]:
        found = scope.filtered().exclude(end_date=None).aggregate(first=Min("end_date"), last=Max("end_date"))
        return {"first": found["first"], "last": found["last"]}

    return cached(scope, "window", compute, when)


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


# ------------------------------------------------------------------------------------------ briefing
BRIEFING_STATUSES = ("review", "submitted", "data_collection", "assigned", "completed")
TOP_PARTNERS = 5


def briefing_scope(scope: Scope, today: datetime.date | None = None) -> Scope:
    """The morning briefing's scope: the page's filters over this year so far, 1 January to today,
    whatever the page's period (and without the page's drill-downs)."""
    from dataclasses import replace

    today = today or _today()
    return replace(
        scope, preset="custom", start=datetime.date(today.year, 1, 1), end=today, year=None, drill=()
    )


def briefing(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """The morning briefing (FMS §7.1), over this year so far (:func:`briefing_scope`): critical flags
    (visits of urgency at or above red), the average quality, the low-quality visits (below the Medium
    band), the critical partners (with a visit of urgency at or above red; the top five with their
    visits), the visits, the visits in review, submitted, in data collection, assigned and completed,
    and the average quality and visits of each governorate."""
    from neurodb.partnerships.models import PartnerOrganization

    year = briefing_scope(scope)

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        visits = year.visits()
        counts = visits.aggregate(
            visits=Count("pk"),
            critical=Count("pk", filter=Q(urgency__gte=limits["red"])),
            low=Count("pk", filter=Q(quality_score__lt=limits["band_medium"])),
            **QUALITY_AGGREGATES,
            **{f"status_{code}": Count("pk", filter=Q(status=code)) for code in BRIEFING_STATUSES},
        )
        partners: Counter = Counter()
        for ids in visits.filter(urgency__gte=limits["red"]).values_list("partner_ids", flat=True):
            partners.update(set(ids or ()))
        names = {
            p.pk: p.short_name or p.name
            for p in PartnerOrganization.objects.filter(pk__in=list(partners)).only(
                "pk", "name", "short_name"
            )
        }
        top = sorted(partners.items(), key=lambda kv: (-kv[1], str(names.get(kv[0], kv[0])).casefold()))
        governorates = (
            visits.exclude(governorate_key="")
            .order_by()
            .values("governorate_key")
            .annotate(n=Count("pk"), name=Max("governorate_name"), **QUALITY_AGGREGATES)
            .order_by("-n", "governorate_key")
        )
        return {
            "start": year.start,
            "end": year.end,
            "visits": counts["visits"],
            "critical": counts["critical"],
            "avg_quality": mean_quality(counts["quality_sum"], counts["quality_n"]),
            "scored": counts["quality_n"],
            "low": counts["low"],
            "critical_partners": len(partners),
            "top_partners": [
                {"id": pk, "name": names.get(pk, str(pk)), "visits": n} for pk, n in top[:TOP_PARTNERS]
            ],
            "statuses": {code: counts[f"status_{code}"] for code in BRIEFING_STATUSES},
            "governorates": [
                {
                    "key": row["governorate_key"],
                    "name": row["name"],
                    "visits": row["n"],
                    "avg_quality": mean_quality(row["quality_sum"], row["quality_n"]),
                }
                for row in governorates
            ],
            "red_at": limits["red"],
            "low_below": limits["band_medium"],
        }

    return cached(year, "briefing", compute, when)


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
    if value is None or isinstance(value, Decimal):  # a DecimalField reads as a Decimal already
        return value
    return Decimal(str(value))


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


def counted_rating(rating: str, status_group: str) -> str:
    """The rating a visit counts under: its own, but "Not monitored" (planned, not conducted: an
    eTools rating, not a monitoring gap) only for a reported visit; a planned or in-progress visit with
    nothing rated is "not rated yet", and an unrated visit of another status counts in no rating ("")."""
    if rating == "not_monitored" and status_group != "reported":
        return ""
    return rating


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


_BUCKET_KEYS = tuple(CHART_BUCKETS)  # in order: bucket n holds the scores from 10n to below 10(n + 1)


def _bucket(quality: Decimal) -> str:
    """The score bucket of a score (0 to 100), as the ``bucket`` drill-down reads it (90-100 holds
    100): worked out, not searched, as the one pass over the visits calls it for every scored one."""
    if quality < 0 or quality > 100:
        return ""
    return _BUCKET_KEYS[min(int(quality // 10), len(_BUCKET_KEYS) - 1)]


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
        # read at once: a year of visits is a few thousand rows, and a server-side cursor's round
        # trips cost more than the rows
        rows = scope.visits().order_by("end_date", "key").values_list(*SUMMARY_COLUMNS)
        add_everyone = everyone.add
        last_day, month = None, ""
        for row in rows:
            (key, label, activity_id, end_date, group, rating, hact_q1, quality, band, flags, flag_count,
             visit_offices, offices_from, section_names, governorate_key, location_id, location_name, site_id,
             place_name, governorate_name, partner_ids, psea_flag, entities, entities_rated,
             action_points) = row  # fmt: skip
            quality = _quality(quality)
            rating = rating or "not_monitored"
            reported = group == "reported"
            # counted_rating(rating, group), written out: it runs for every visit of the scope
            counted = "" if rating == "not_monitored" and not reported else rating
            add_everyone(quality, band, counted)
            if counted in by_rating:
                by_rating[counted].add(quality, band, counted)
            counts["reported"] += reported
            counts["off_track"] += rating == "off_track"
            counts["gaps"] += reported and not entities_rated
            counts["psea_asked"] += psea_flag is not None
            counts["psea_flagged"] += psea_flag is True
            counts["no_place"] += location_id is None and site_id is None
            counts["unlocated"] += not governorate_key
            for code in flags or ():
                rule_flags[code] += 1
            if quality is None:
                buckets["none"] += 1
            else:
                buckets[_bucket(quality)] += 1
                count = flag_count or 0
                flags_dist["3+" if count >= 3 else str(count)] += 1
                counts["high_flag"] += count >= high_flag
            # by month (the end date: every visit here has one; the rows come in date order)
            if end_date != last_day:
                last_day, month = end_date, f"{end_date.year:04d}-{end_date.month:02d}"
            m = months.get(month)
            if m is None:
                m = months[month] = {"visits": 0, "reported": 0, "q_sum": Decimal(0), "q_n": 0}
            m["visits"] += 1
            m["reported"] += reported
            if quality is not None:
                m["q_sum"] += quality
                m["q_n"] += 1
            if hact_q1:
                m[f"q1:{hact_q1}"] = m.get(f"q1:{hact_q1}", 0) + 1
            if counted:
                m[f"rating:{counted}"] = m.get(f"rating:{counted}", 0) + 1
            # field offices ("none": office not known); a visit counts in each of its offices
            office_from = offices_from or "none"
            office_sources[office_from] += 1
            for name in visit_offices or ["none"]:
                o = offices.get(name)
                if o is None:  # (not setdefault: its default would be built for every visit)
                    o = offices[name] = {"tally": Tally(), "from": Counter(), "flags": Counter()}
                o["tally"].add(quality, band, counted)
                o["from"][office_from] += 1
                if quality is not None and flags:
                    o["flags"].update(flags)
            # sections ("none": no section); a visit counts in each of its sections (its line is a
            # tuple: only the first SECTION_LINES of a section are written out)
            line = (
                quality is None,
                quality or 0,
                end_date,
                key,
                quality,
                activity_id,
                label,
                band,
                rating,
                group,
            )
            for name in section_names or ["none"]:
                s = sections.get(name)
                if s is None:
                    s = sections[name] = {"tally": Tally(), "lines": [], "flagged": 0}
                s["tally"].add(quality, band, counted)
                s["lines"].append(line)
                s["flagged"] += bool(flags)
            # places: the gazetteer location, else the place eTools wrote (a site or a name)
            if location_id is not None:
                place_key: tuple | None = ("location", location_id)
                name = location_name or place_name
            elif place_name:
                place_key, name = ("place", place_name), place_name
            else:
                place_key = None
            if place_key is not None:
                p = places.get(place_key)
                if p is None:
                    p = places[place_key] = {
                        "name": name,
                        "location_id": location_id,
                        "governorate": governorate_name,
                        "tally": Tally(),
                        "last": None,
                        "entities": 0,
                        "rated": 0,
                    }
                p["tally"].add(quality, band, counted)
                p["last"] = max(p["last"] or end_date, end_date)
                p["entities"] += entities or 0
                p["rated"] += entities_rated or 0
                p["governorate"] = p["governorate"] or governorate_name
            if governorate_key:
                governorates.add(governorate_key)
            if partner_ids:
                partners.update(partner_ids)
            if reported and not action_points:
                worse = max([rating, hact_q1 or "not_monitored"], key=_rating_rank)
                if worse in ("off_track", "constrained"):
                    no_follow_up.append((key, _visit_name(key, label, activity_id)))

        out_sections = {}
        for name, s in sections.items():
            lines = sorted(s["lines"], key=lambda ln: ln[:4])  # lowest score first, unscored last
            out_sections[name] = {
                **s["tally"].out(),
                "flagged": s["flagged"],
                "lines": [
                    {
                        "key": key,
                        "name": f"#{activity_id}" if activity_id else label,  # _visit_name
                        "quality": quality,
                        "band": band,
                        "rating": rating,
                        "not_rated_yet": _not_rated_yet(rating, group),
                        "date": end_date,
                    }
                    for *_sort, end_date, key, quality, activity_id, label, band, rating, group in (
                        ln[2:] for ln in lines[:SECTION_LINES]
                    )
                ],
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
    return RATING_ORDER.get(code, 0)


# ------------------------------------------------------------------------------------------ by month
def _month_axis(scope: Scope, seen: Any) -> list[datetime.date]:
    """The first day of each month of the period (for "all time", from the first month with a visit),
    up to this month or the latest month with a visit (whichever is later), at most the latest
    MONTHS_MAX."""
    months = [datetime.date.fromisoformat(f"{m}-01") for m in seen]
    first = scope.start.replace(day=1)
    if scope.preset == "all_time":
        first = min(months or [_today().replace(day=1)])
    latest = max(months or [first])
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

    key = f"fmm:v3:q1:{when if when is not None else stamp()}"
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
    """Block 7: scored visits in ten score buckets of 10 points (90–100 holds 100), each with its drill
    value and the colour of its band (a bucket takes the band of its lowest score: High from 80, Medium
    from 50, else Low, as Score settings set them), and the visits not scored."""
    limits = limits or thresholds()
    data = summary(scope, when, limits)
    found = data["buckets"]
    items = [
        {
            "label": f"{low}–{high}",
            "value": found.get(drill, 0),
            "color": BAND_COLORS[band_of(low, limits)],
            "band": band_of(low, limits),
            "drill": drill,
        }
        for drill, (low, high) in CHART_BUCKETS.items()
    ]
    return {"items": items if data["scored"] else [], "not_scored": found.get("none", 0)}


def band_of(score: float, limits: dict[str, int]) -> str:
    """The band of a score under the thresholds set now: high, medium or low."""
    return "high" if score >= limits["band_high"] else "medium" if score >= limits["band_medium"] else "low"


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
        rows = list(
            VisitRuleResult.objects.filter(status="fail", visit__in=scope.visits().values("pk"))
            .order_by()
            .values_list(
                "rule",
                "detail_key",
                "measure",
                "visit__key",
                "visit__label",
                "visit__activity_id",
                "visit__urgency",
                "visit__end_date",
            )
        )
        # most urgent visit first, then the latest, then by key: sorted here, where it costs a third of
        # what the database took to sort the joined rows
        rows.sort(key=lambda r: (-(r[6] or 0), -(r[7].toordinal() if r[7] else 0), r[3]))
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for rule, key, measure, visit_key, label, activity_id, urgency, _end in rows:
            g = groups.get((rule, key))
            if g is None:  # (not setdefault: its default would be built for every flag)
                g = groups[(rule, key)] = {"visits": [], "n": 0, "urgency": 0, "urgent_n": 0, "measures": []}
            g["n"] += 1
            if len(g["visits"]) < CHIP_VISITS:  # the first visits (most urgent) are the chips
                g["visits"].append({"key": visit_key, "name": _visit_name(visit_key, label, activity_id)})
            if urgency is not None:  # a visit without a score has no urgency: left out of the mean
                g["urgency"] += urgency
                g["urgent_n"] += 1
            if measure is not None:
                g["measures"].append(measure)
        out = []
        for (rule, key), g in groups.items():
            n = g["n"]
            drill = f"{rule}:{key}"
            out.append(
                {
                    "rule": rule,
                    "label": issue_label(rule, key, g["measures"], settings_.get(rule)),
                    "visits": n,
                    "urgency": int(half_up(Decimal(g["urgency"]) / g["urgent_n"], 0))
                    if g["urgent_n"]
                    else None,
                    "lowest": min(g["measures"]) if g["measures"] else None,
                    "chips": g["visits"][:CHIP_VISITS],
                    "more": max(n - CHIP_VISITS, 0),
                    "drill": drill if _ISSUE.match(drill) else "",
                }
            )
        out.sort(key=lambda r: (-r["visits"], -(r["urgency"] or 0), r["rule"], r["label"]))
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


def _rule_rows(scope: Scope, by_month: bool) -> list[dict[str, Any]]:
    """Per rule (and month of the visits' end date, ``by_month``), over the scope's visits: the visits
    in each result state, and the points earned (each capped at its maximum) over the points
    evaluated. One query."""
    from django.db.models import DecimalField
    from django.db.models.functions import Cast, Least, TruncMonth

    from .models import VisitRuleResult
    from .rules import STATES

    evaluated = Q(status__in=("pass", "fail"), max_points__gt=0)
    rows = VisitRuleResult.objects.filter(visit__in=scope.visits().values("pk")).order_by()
    if by_month:
        rows = rows.annotate(month=TruncMonth("visit__end_date")).values("rule", "month")
    else:
        rows = rows.values("rule")
    return list(
        rows.annotate(
            **{state: Count("pk", filter=Q(status=state)) for state in STATES},
            earned=Sum(
                Least("points", Cast("max_points", DecimalField(max_digits=5, decimal_places=1))),
                filter=evaluated,
            ),
            points_max=Sum("max_points", filter=evaluated),
            points_n=Count("pk", filter=evaluated),
        )
    )


def rule_months(scope: Scope, when: str | None = None) -> list[dict[str, Any]]:
    """:func:`_rule_rows` per rule and month (``"2026-05"``), which the rule trends read, and the rule
    figures too when the trends were read first (the Quality tab)."""

    def compute() -> list[dict[str, Any]]:
        return [
            {**row, "month": row["month"].strftime("%Y-%m") if row["month"] else ""}
            for row in _rule_rows(scope, by_month=True)
        ]

    return cached(scope, "rule_months", compute, when)


def rule_stats(scope: Scope, when: str | None = None) -> dict[str, dict[str, Any]]:
    """Per rule over the scope's visits: the visits in each result state, and the points earned (each
    capped at its maximum) over the points evaluated: the rule months summed when they are kept
    already, else a query of its own without the months (cheaper: no join to the visits' dates)."""
    from .rules import STATES

    def compute() -> dict[str, dict[str, Any]]:
        months = kept(scope, "rule_months", when)
        if months is None:
            return {row.pop("rule"): row for row in _rule_rows(scope, by_month=False)}
        out: dict[str, dict[str, Any]] = {}
        for row in months:
            s = out.get(row["rule"])
            if s is None:
                s = out[row["rule"]] = {
                    **dict.fromkeys(STATES, 0),
                    "earned": None,
                    "points_max": None,
                    "points_n": 0,
                }
            for state in STATES:
                s[state] += row[state]
            s["points_n"] += row["points_n"]
            if row["earned"] is not None:
                s["earned"] = (s["earned"] or 0) + row["earned"]
            if row["points_max"] is not None:
                s["points_max"] = (s["points_max"] or 0) + row["points_max"]
        return out

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
    """Block 20: per visit rating, its visits, their average quality and bands; Not monitored (a
    reported visit with nothing rated: planned, not conducted) always has its own bar."""
    from .scope import RATING_LABELS

    data = summary(scope, when, limits)
    out = []
    for code in RATINGS:
        row = data["by_rating"][code]
        if (row["visits"] or code == "not_monitored") and _within(scope.ratings, code):
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


def rule_trends(scope: Scope, rules: list | None = None, when: str | None = None) -> dict[str, Any]:
    """Rule score trends over time: per rule with points, the share of its maximum points earned by the
    visits that ended each month (the results that passed or failed; each capped at its maximum); a
    month where the rule checked no visit has no value. ``{}`` when no rule checked a visit."""
    from .rules import half_up

    rules = [r for r in _rule_settings(rules) if r.enabled and r.points]
    found: dict[tuple[str, str], float] = {}
    for row in rule_months(scope, when):
        if row["month"] and row["points_max"]:
            share = half_up(Decimal(100) * Decimal(row["earned"] or 0) / Decimal(row["points_max"]), 1)
            found[(row["rule"], row["month"])] = float(min(Decimal(100), share))
    if not found:
        return {}
    axis = _month_axis(scope, {month for _rule, month in found})
    keys = [d.strftime("%Y-%m") for d in axis]
    shown = [r for r in sorted(rules, key=lambda r: r.code) if any((r.code, k) in found for k in keys)]
    if not shown:
        return {}
    return {
        "labels": [_month_label(d) for d in axis],
        "series": {f"{r.code} {r.label}": [found.get((r.code, k)) for k in keys] for r in shown},
        "drill": {"labels": keys, "series": {f"{r.code} {r.label}": r.code for r in shown}},
    }


def highlights(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 13: visits, reported, governorates covered, the average quality (the KPI's own
    definition), off-track visits, PSEA-flagged visits out of those with a PSEA question, the share
    of High / Medium / Low scores and the monitored entities by kind."""
    data = summary(scope, when, limits)
    kinds = entity_kinds(scope, when)
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
    "visit__urgency",
    "datamart_id",
)


def _year(scope: Scope) -> int:
    """The calendar year a block of the scope reads plans for: the year chosen, else the period's last."""
    return scope.year if scope.preset == "year" and scope.year else scope.end.year


def entity_kinds(scope: Scope, when: str | None = None) -> dict[str, int]:
    """The monitored entities of the scope (the finding rows; an entity filter keeps the matching rows
    only) counted by kind: the key figures' own count (:func:`kpis`), so no query of its own."""
    return kpis(scope, when)["entity_kinds"]


def entity_rows(scope: Scope, when: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """The monitored entities of the scope (the finding rows; an entity filter keeps the matching rows
    only): their count by kind, and per kind each entity with its visits, average quality, bands, most
    frequent flag and latest rating; PD rows add the visits planned for the year. With ``kind``, only
    the entities of that kind are worked out (the table shows one kind at a time)."""
    from django.db.models import F

    from neurodb.datamart.models import PlannedVisits

    def compute() -> dict[str, Any]:
        year = _year(scope)
        # the visits planned for the year per programme document (only the PD table shows them), read
        # once: a subquery per entity row cost a third of the block at 15,000 rows
        planned = (
            dict(
                PlannedVisits.objects.filter(year=year, intervention_id__isnull=False)
                .order_by()
                .values("intervention_id")
                .annotate(total=Sum(F("q1") + F("q2") + F("q3") + F("q4")))
                .values_list("intervention_id", "total")
            )
            if kind in (None, "pd")
            else {}
        )
        # the rows in the entities' own order (most urgent visit first), sorted here rather than by the
        # database, which sorted the whole joined rows (a third of the block at 15,000 rows)
        found = scope.entities().order_by()
        if kind == "other":
            found = found.filter(Q(kind="other") | Q(kind=""))
        elif kind:
            found = found.filter(kind=kind)
        rows = list(found.values_list(*ENTITY_COLUMNS))
        # most urgent first; a visit without urgency (not scored) after every one that has it
        rows.sort(
            key=lambda r: (
                -(r[15] if r[15] is not None else -1),
                -(r[10].toordinal() if r[10] else 10**9),
                r[16],
            )
        )
        groups: dict[str, dict[tuple, dict[str, Any]]] = {}
        for row in rows:
            row_kind, entity, pd_id, pd_number, partner_id, partner_short, partner_name = row[:7]
            cp_output, rating, visit_id, end_date, quality, band, flags, status_group = row[7:15]
            row_kind = row_kind or "other"
            text = (entity or "").strip()
            if row_kind == "pd" and pd_id:
                key, name, link = ("pd", pd_id), pd_number or text, ("pd", pd_id)
            elif row_kind == "partner" and partner_id:
                name = partner_short or partner_name or text
                key, link = ("partner", partner_id), ("partner", partner_id)
            elif row_kind == "cp_output":
                name = (cp_output or text).strip()
                key, link = ("text", name.casefold()), None
            else:
                key, name, link = ("text", text.casefold()), text, None
            if not name:
                continue
            of_kind = groups.get(row_kind)
            if of_kind is None:
                of_kind = groups[row_kind] = {}
            g = of_kind.get(key)
            if g is None:  # (not setdefault: its default would be built for every row)
                g = of_kind[key] = {"name": name, "link": link, "visits": {}, "planned": planned.get(pd_id)}
            g["visits"][visit_id] = (
                end_date,
                _quality(quality),
                band,
                tuple(flags or ()),
                status_group,
                rating,
            )
        out: dict[str, list[dict[str, Any]]] = {}
        for group_kind, entities in groups.items():
            rows_out = []
            for g in entities.values():
                tally, flags = Tally(), Counter()
                for _day, quality, band, visit_flags, group, rating in g["visits"].values():
                    tally.add(quality, band, counted_rating(rating, group))
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
            out[group_kind] = rows_out
        return {"kinds": entity_kinds(scope, when), "entities": out, "year": year}

    return cached(scope, f"entities:{kind or 'all'}", compute, when)


def entities_performance(scope: Scope, kind: str = "pd", when: str | None = None) -> dict[str, Any]:
    """Block 16: the entities of one kind, worst average quality first, unscored last."""
    data = entity_rows(scope, when, kind)
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
