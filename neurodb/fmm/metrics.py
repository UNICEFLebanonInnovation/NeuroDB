"""The figures of the Monitoring insights page, each from one function that takes a
:class:`~neurodb.fmm.scope.Scope` and reads the stored visits and records (never the eTools rows).

As in FMS, each **record** (an entity assessed on a visit: a partner, CP output or PD/SSFA) is scored,
flagged and given an urgency on its own, and the figures of quality, ratings, flags, rules and urgency
count records (:meth:`Scope.records`); "Monitoring visits", the status breakdown, places, PSEA, the
monitoring gaps and the follow-up count distinct visits (:meth:`Scope.visits`), as FMS does. Each block
says which it counts, and gives both where both matter.

This module holds the key figures (:func:`kpis`), the data notes that say where FMM counts differently
from the overview (:func:`notes`), the shared definitions the other blocks reuse (the average quality,
:func:`avg_quality`, one function for the tile, the highlights, the AI facts and the chat tools; the
urgency bands, :func:`urgency_counts`), and the blocks of the Quality and Analysis tabs: quality and
records by month, HACT Q1 by month, the score distribution, recurring issues, places, rule analysis,
flags, highlights, governorates, field offices, entities, sections, ratings, points per rule, HACT
programmatic visits and follow-up.

Most blocks read one pass over the scope's visits and their records (:func:`summary`), so a tab runs a
handful of queries whatever the number of visits; the rule results, the entity table, HACT figures and
action points have one query each. Every drill value a block gives (``drill``) is a code the scope
parses, and the records a drill-down lists are exactly those the block counted (for a block that counts
visits, the records of those visits).

Every block is cached for ten minutes under ``fmm:v5:<scope hash>:<last refresh run>:<rules
version>:<day>:<block>`` (:func:`cached`), so a refresh, a rescore or a new day shows at once.
"""

from __future__ import annotations

import datetime
import heapq
import re
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from itertools import chain
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, DecimalField, Max, Min, Q, QuerySet, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateformat import format as date_format

from neurodb.datamart.fm import RATING_ORDER

from .scope import BUCKETS, CHART_BUCKETS, Scope

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
    or read), the rules version it computed the scores with, the day (rolling periods move at
    midnight) and the last change to the NeuroDB action points (they count as follow-up at once)."""
    from . import status
    from .models import LocalActionPoint

    last = status.last_refresh() if last is _READ else last
    version = (last.details or {}).get("rules_version", 0) if last else 0
    local = LocalActionPoint.objects.aggregate(n=Count("pk"), at=Max("updated_at"))
    changed = f"{local['n']}.{local['at'].timestamp():.6f}" if local["at"] else "0"
    return f"{last.pk if last else 0}:{version}:{timezone.localdate().isoformat()}:{changed}"


def cached(scope: Scope, block: str, compute: Callable[[], Any], when: str | None = None) -> Any:
    """``compute()`` for ``scope``, kept ten minutes (not in DEBUG); ``when`` is :func:`stamp`, read
    once per request by the caller. A scope that drills into the reviews is never kept: a review
    saved a minute ago must count at once, and no refresh marks it."""
    if settings.DEBUG or any(key == "review" for key, _value in scope.drill):
        return compute()
    key = f"fmm:v5:{scope.hash()}:{when if when is not None else stamp()}:{block}"
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
    return cache.get(f"fmm:v5:{scope.hash()}:{when if when is not None else stamp()}:{block}")


# The aggregates the average quality is computed from, so a block that aggregates the records anyway
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


def avg_quality(records_qs: QuerySet) -> Decimal | None:
    """The average quality score of the scored records of ``records_qs`` (``Scope.records``; half up,
    one decimal), or None when none is scored. The one definition of the KPI, the highlights, the AI
    facts and the chat tools: as in FMS, the mean over records, each scored on its own."""
    return mean_quality(**records_qs.aggregate(**QUALITY_AGGREGATES))


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


def urgency_counts(records_qs: QuerySet, limits: dict[str, int] | None = None) -> dict[str, int]:
    """``{"red": n, "amber": n}``: records at or above the red threshold, and between amber and red."""
    limits = limits or thresholds()
    return records_qs.aggregate(
        red=Count("pk", filter=Q(urgency__gte=limits["red"])),
        amber=Count("pk", filter=Q(urgency__gte=limits["amber"], urgency__lt=limits["red"])),
    )


COUNTED_KINDS = ("pd", "cp_output", "partner")  # the kinds counted by name; the rest is "other"


def kpis(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """The four key figures: the monitoring visits (distinct visits, with the status breakdown); the
    records (rated, by rating, and Not monitored: planned, not conducted, a count apart; not rated yet on
    a planned or in-progress visit); the average quality per record (with the scored records and the
    rules version); and the records of high urgency (with the amber ones; a record without a score has
    no urgency). Two queries: the visits, then their records."""

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        visits = scope.visits().aggregate(
            total=Count("pk"),
            scored=Count("pk", filter=Q(quality_score__isnull=False)),
            version=Max("rules_version"),
            **{group: Count("pk", filter=Q(status_group=group)) for group in STATUS_ORDER},
        )
        records = scope.records().aggregate(
            total=Count("pk"),
            red=Count("pk", filter=Q(urgency__gte=limits["red"])),
            amber=Count("pk", filter=Q(urgency__gte=limits["amber"], urgency__lt=limits["red"])),
            rated=Count("pk", filter=Q(rating__in=RATED)),
            # Not monitored (planned, not conducted) on a reported visit only; on a planned or
            # in-progress visit a blank rating is "not rated yet" (``counted_rating``)
            not_monitored=Count("pk", filter=Q(rating="not_monitored", visit__status_group="reported")),
            not_rated_yet=Count(
                "pk",
                filter=Q(rating="not_monitored", visit__status_group__in=("planned", "in_progress")),
            ),
            **QUALITY_AGGREGATES,
            **{f"rating_{code}": Count("pk", filter=Q(rating=code)) for code in RATED},
            **{f"kind_{kind}": Count("pk", filter=Q(kind=kind)) for kind in COUNTED_KINDS},
        )
        by_kind = {kind: records[f"kind_{kind}"] for kind in COUNTED_KINDS}
        by_kind["other"] = records["total"] - sum(by_kind.values())  # "other", or no kind at all
        breakdown = [
            {"group": g, "n": visits[g], "label": STATUS_WORDS[g]}
            for g in STATUS_ORDER
            if visits[g] or g != "unknown"
        ]
        return {
            "visits": visits["total"],
            "by_status": breakdown,
            "records": records["total"],
            "records_rated": records["rated"],
            "records_not_monitored": records["not_monitored"],
            # the rated records by rating: every share of ratings is over the rated ones only
            "record_ratings": {code: records[f"rating_{code}"] for code in RATED},
            "records_not_rated_yet": records["not_rated_yet"],
            "records_other": records["total"]
            - records["rated"]
            - records["not_monitored"]
            - records["not_rated_yet"],
            "record_kinds": {kind: n for kind, n in by_kind.items() if n},
            "avg_quality": mean_quality(records["quality_sum"], records["quality_n"]),
            "scored": records["quality_n"],  # scored records
            "scored_visits": visits["scored"],
            "rules_version": visits["version"] or 0,
            "high_urgency": records["red"],
            "amber": records["amber"],
            "red_at": limits["red"],
            "amber_at": limits["amber"],
        }

    return cached(scope, "kpis", compute, when)


def data_window(scope: Scope, when: str | None = None) -> dict[str, Any]:
    """The first and last visit dates (start, else end) of the visits every filter but the period
    keeps ("Data available from X to Y", shown for all time and for custom dates)."""

    def compute() -> dict[str, Any]:
        found = (
            scope.filtered()
            .exclude(visit_date=None)
            .aggregate(first=Min("visit_date"), last=Max("visit_date"))
        )
        return {"first": found["first"], "last": found["last"]}

    return cached(scope, "window", compute, when)


def notes(scope: Scope, when: str | None = None) -> list[dict[str, Any]]:
    """The data notes of the scope: where FMM counts differently from the overview, each line only
    when it applies, with its count (``{"key", "n", "text"}``)."""

    def compute() -> list[dict[str, Any]]:
        within = Q(visit_date__gte=scope.start, visit_date__lte=scope.end)
        counts = scope.filtered().aggregate(
            via_site=Count("pk", filter=within & Q(location=None, site__isnull=False)),
            no_reference=Count("pk", filter=within & Q(reference="")),
            no_date=Count("pk", filter=Q(visit_date=None)),
            dated_by_end=Count("pk", filter=within & Q(start_date=None, end_date__isnull=False)),
        )
        lines: list[dict[str, Any]] = []
        if scope.sections:
            lines.append({"key": "section", "n": None})
        if scope.governorate and scope.governorate != "none" and counts["via_site"]:
            lines.append({"key": "via_site", "n": counts["via_site"]})
        for key in ("no_reference", "no_date", "dated_by_end"):
            if counts[key]:
                lines.append({"key": key, "n": counts[key]})
        if scope.record_filtered:
            lines.append({"key": "record_filter", "n": None})
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
    (records of urgency at or above red), the average quality per record, the low-quality records (below
    the Medium band), the critical partners (the partners of a record of urgency at or above red; the
    top five with their critical records), the monitoring visits (distinct visits, with their records),
    the visits in review, submitted, in data collection, assigned and completed (a status is the visit's),
    and the average quality and records of each governorate."""
    from neurodb.partnerships.models import PartnerOrganization

    year = briefing_scope(scope)

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        records = year.records()
        counts = records.aggregate(
            records=Count("pk"),
            critical=Count("pk", filter=Q(urgency__gte=limits["red"])),
            low=Count("pk", filter=Q(quality_score__lt=limits["band_medium"])),
            **QUALITY_AGGREGATES,
        )
        visits = year.visits().aggregate(
            visits=Count("pk"),
            **{f"status_{code}": Count("pk", filter=Q(status=code)) for code in BRIEFING_STATUSES},
        )
        partners: Counter = Counter(
            records.filter(urgency__gte=limits["red"], partner_id__isnull=False)
            .order_by()
            .values_list("partner_id", flat=True)
        )
        names = {
            p.pk: p.name or p.short_name
            for p in PartnerOrganization.objects.filter(pk__in=list(partners)).only(
                "pk", "name", "short_name"
            )
        }
        top = sorted(partners.items(), key=lambda kv: (-kv[1], str(names.get(kv[0], kv[0])).casefold()))
        # each governorate's records and their visits (the distinct visits of its records)
        governorates = (
            records.exclude(visit__governorate_key="")
            .order_by()
            .values("visit__governorate_key")
            .annotate(
                n=Count("pk"),
                visits=Count("visit_id", distinct=True),
                name=Max("visit__governorate_name"),
                **QUALITY_AGGREGATES,
            )
            .order_by("-n", "visit__governorate_key")
        )
        return {
            "start": year.start,
            "end": year.end,
            "visits": visits["visits"],
            "records": counts["records"],
            "critical": counts["critical"],
            "avg_quality": mean_quality(counts["quality_sum"], counts["quality_n"]),
            "scored": counts["quality_n"],
            "low": counts["low"],
            "critical_partners": len(partners),
            "top_partners": [
                {"id": pk, "name": names.get(pk, str(pk)), "records": n} for pk, n in top[:TOP_PARTNERS]
            ],
            "statuses": {code: visits[f"status_{code}"] for code in BRIEFING_STATUSES},
            "governorates": [
                {
                    "key": row["visit__governorate_key"],
                    "name": row["name"],
                    "records": row["n"],
                    "visits": row["visits"],
                    "avg_quality": mean_quality(row["quality_sum"], row["quality_n"]),
                }
                for row in governorates
            ],
            "red_at": limits["red"],
            "low_below": limits["band_medium"],
        }

    return cached(year, "briefing", compute, when)


# ------------------------------------------------------------------------------------------ critical items
CRITICAL_ITEMS = 10  # red records listed
CRITICAL_AMBER = 5  # amber records listed when no record is red
_OFFICE_IN_FLAG = re.compile(r"field office '([^']+)'")
_RECORD_IN_FLAG = re.compile(r"\(([^()]{1,40})\) ")  # a visit's derived flag opens with the record's type
ENTITY_TYPE_LABELS = {"pd": "PD/SSFA", "cp_output": "CP output", "partner": "Partner", "other": "Other"}


def record_name(
    kind: str, entity: str, cp_output: str = "", pd_number: str = "", partner_name: str = ""
) -> str:
    """A record's entity in words: the CP output, the partner's full name or the PD number, else the
    entity as eTools wrote it ("" when it has none)."""
    own = {"cp_output": cp_output, "partner": partner_name, "pd": pd_number}.get(kind)
    return (own or entity or "").strip()


def record_names(ids) -> dict[int, str]:
    """The entity of each record of ``ids`` (``datamart_id``), in words (:func:`record_name`); a
    record without one is left out. One query, for the records a list shows (the section lines)."""
    from .models import VisitEntity

    ids = list(ids)
    if not ids:
        return {}
    out: dict[int, str] = {}
    rows = VisitEntity.objects.filter(datamart_id__in=ids).values_list(
        "datamart_id", "kind", "entity", "cp_output", "pd__number", "partner__name"
    )
    for datamart_id, kind, entity, cp_output, pd_number, partner_name in rows:
        text = record_name(kind, entity, cp_output, pd_number or "", partner_name or "")
        if text:
            out[datamart_id] = text
    return out


def critical_items(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Critical items requiring attention (FMS §7.3), one card per record: the scored records of urgency
    at or above red (the morning briefing's critical flags), most urgent first, at most
    ``CRITICAL_ITEMS``; when no record is red, the ``CRITICAL_AMBER`` most urgent amber records. Each
    with its visit, entity and type, its rating, urgency and one line per rule it failed: the rule's
    stored flag (for an AI check, the flag and the AI's explanation), except R19, which says how many
    monitors are not on the field office's staff list (never who). ``band`` is "red", "amber" or ""
    (nothing to list)."""
    from .models import RecordRuleResult
    from .rules import code_order

    limits = limits or thresholds()

    def compute() -> dict[str, Any]:
        records = scope.records().order_by("-urgency", "-visit__visit_date", "visit__key", "datamart_id")
        fields = ("pk", "datamart_id", "kind", "entity", "cp_output", "pd__number", "partner__name", "rating")
        fields += ("urgency", "visit__key", "visit__label", "visit__activity_id", "visit__status_group")
        # one query: the most urgent records from amber up, the red ones first (most urgent first)
        found = list(records.filter(urgency__gte=limits["amber"]).values_list(*fields)[:CRITICAL_ITEMS])
        red = [r for r in found if r[8] >= limits["red"]]
        band, found = ("red", red) if red else ("amber", found[:CRITICAL_AMBER])
        if not found:
            return {"band": "", "items": []}
        lines: dict[int, list[dict[str, Any]]] = {r[0]: [] for r in found}
        results = RecordRuleResult.objects.filter(entity_id__in=list(lines), status="fail").values_list(
            "entity_id", "rule", "detail", "measure"
        )
        for entity_id, rule, detail, measure in results:
            lines[entity_id].append({"rule": rule, **_flag_line(rule, detail, measure)})
        items = []
        for (
            pk,
            datamart_id,
            kind,
            entity,
            cp_output,
            pd_number,
            partner_name,
            rating,
            urgency,
            *visit,
        ) in found:
            key, label, activity_id, status_group = visit
            items.append(
                {
                    "key": key,
                    "record": datamart_id,
                    "name": _visit_name(key, label, activity_id),
                    "entity": record_name(kind, entity, cp_output, pd_number or "", partner_name or ""),
                    "type": ENTITY_TYPE_LABELS.get(kind or "other", ENTITY_TYPE_LABELS["other"]),
                    "rating": rating,
                    "status_group": status_group,
                    "urgency": urgency,
                    "level": "high" if urgency >= limits["red"] else "medium",
                    "lines": sorted(lines[pk], key=lambda line: code_order(line["rule"])),
                }
            )
        return {"band": band, "items": items}

    return cached(scope, f"critical_items:{limits['red']}:{limits['amber']}", compute, when)


def _flag_line(rule: str, detail: str, measure: float | None) -> dict[str, Any]:
    """One line of a critical item: the stored flag without the type of the record that failed the rule
    ("(PD/SSFA) ", kept as ``record``) and its "R3: " prefix, split at its first dash (an AI check's
    flag holds the AI's explanation after it); for R19, the number of monitors not on the staff list and
    the field office (``r19``), never an address."""
    if rule == "R19":
        office = _OFFICE_IN_FLAG.search(detail or "")
        return {"r19": {"n": int(measure or 1), "office": office.group(1) if office else ""}}
    text = (detail or "").strip()
    record = ""
    found = _RECORD_IN_FLAG.match(text)
    if found and text[found.end() :].startswith(f"{rule}:"):
        record, text = found.group(1), text[found.end() :]
    if text.startswith(f"{rule}:"):
        text = text[len(rule) + 1 :].strip()
    message, _dash, explanation = text.partition(" — ")
    return {"message": message, "explanation": explanation, "record": record}


# ------------------------------------------------------------------------------------------ shared
BANDS = ("high", "medium", "low")
BAND_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
BAND_COLORS = {"high": "--nd-success", "medium": "--nd-warning", "low": "--nd-danger"}
RATINGS = ("on_track", "constrained", "off_track", "not_monitored")
RATING_COLORS = {  # FMS's rating colours (app.css), the same in the HACT Q1 and the rating charts
    "on_track": "--nd-rating-on-track",
    "constrained": "--nd-rating-constrained",
    "off_track": "--nd-rating-off-track",
    "not_monitored": "--nd-rating-not-monitored",
}
FLAG_ROWS = (  # (drill, label, meter colour), as FMS's flag count distribution writes them
    ("0", "0 flags", "success"),
    ("1", "1 flag", "info"),
    ("2", "2 flags", "warning"),
    ("3+", "3+ flags", "danger"),
)
MONTHS_MAX = 36  # the latest months a monthly chart draws
SECTION_LINES = 50  # record lines listed under a section
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
    """The rating a record (or a visit) counts under: its own, but "Not monitored" (planned, not
    conducted: an eTools rating, not a monitoring gap) only on a reported visit; on a planned or
    in-progress visit nothing rated is "not rated yet", and an unrated record of another status counts
    in no rating ("")."""
    if rating == "not_monitored" and status_group != "reported":
        return ""
    return rating


class Tally:
    """Records counted with their quality scores, score bands and ratings, and the distinct visits they
    belong to. A visit's records are added one after the other (``visit`` given): a visit counts once
    however many of its records are added."""

    __slots__ = ("bands", "last", "q_n", "q_sum", "ratings", "records", "visits")

    def __init__(self) -> None:
        self.records = 0
        self.visits = 0
        self.last: Any = None  # the visit of the last record added
        self.q_sum = Decimal(0)
        self.q_n = 0
        self.bands = dict.fromkeys(BANDS, 0)
        self.ratings: dict[str, int] = {}

    def add(self, quality: Decimal | None, band: str, rating: str, visit: Any = None) -> None:
        self.records += 1
        if visit is None or visit != self.last:
            self.visits += 1
            self.last = visit
        if quality is not None:
            self.q_sum += quality
            self.q_n += 1
        if band in self.bands:
            self.bands[band] += 1
        ratings = self.ratings
        ratings[rating] = ratings.get(rating, 0) + 1

    def merge(self, part: tuple) -> None:
        """One visit's records at once (``part``: their count, score sum, scores, bands and ratings, as
        :func:`summary` adds them up): the visit counts once."""
        n, q_sum, q_n, bands, ratings = part
        self.records += n
        self.visits += 1
        self.q_sum += q_sum
        self.q_n += q_n
        own = self.bands
        for band, count in bands.items():
            if band in own:
                own[band] += count
        own = self.ratings
        for rating, count in ratings.items():
            own[rating] = own.get(rating, 0) + count

    def out(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "visits": self.visits,
            "avg": mean_quality(self.q_sum, self.q_n),
            "scored": self.q_n,
            "bands": dict(self.bands),
            "ratings": {rating: self.ratings.get(rating, 0) for rating in RATINGS},
        }


def _parts_out(parts: list[tuple]) -> dict[str, Any]:
    """:meth:`Tally.out` of a group from the parts of its visits, as :func:`summary` adds them up: each
    part is one visit's records (their count, score sum, scored ones, High / Medium / Low bands, and On
    track / Constrained / Off track / Not monitored ratings), and the visits are the parts. Summed column
    by column, not visit by visit."""
    if parts:
        n, q_sum, q_n, high, medium, low, on_track, constrained, off_track, not_monitored = map(
            sum, zip(*parts, strict=True)
        )
    else:
        n = q_sum = q_n = high = medium = low = on_track = constrained = off_track = not_monitored = 0
    return {
        "records": n,
        "visits": len(parts),
        "avg": mean_quality(q_sum, q_n),
        "scored": q_n,
        "bands": {"high": high, "medium": medium, "low": low},
        "ratings": {
            "on_track": on_track,
            "constrained": constrained,
            "off_track": off_track,
            "not_monitored": not_monitored,
        },
    }


def _rated_out(code: str, records: list[tuple]) -> dict[str, Any]:
    """:meth:`Tally.out` of the records that count under the rating ``code`` (``(visit, score, band)``
    each, as :func:`summary` lists them)."""
    scores = [quality for _visit, quality, _band in records if quality is not None]
    bands = Counter(band for _visit, _quality, band in records)
    return {
        "records": len(records),
        "visits": len({visit for visit, _quality, _band in records}),
        "avg": mean_quality(sum(scores), len(scores)),
        "scored": len(scores),
        "bands": {band: bands.get(band, 0) for band in BANDS},
        "ratings": {rating: len(records) if rating == code else 0 for rating in RATINGS},
    }


_FLAG_KEYS = ("0", "1", "2")  # the flags-per-record chart's keys below "3+"
_BUCKET_KEYS = tuple(CHART_BUCKETS)  # in order: bucket n holds the scores from 10n to below 10(n + 1)


def _bucket(quality: Decimal) -> str:
    """The score bucket of a score (0 to 100), as the ``bucket`` drill-down reads it (90-100 holds
    100): worked out, not searched, as the one pass over the records calls it for every scored one."""
    if quality < 0 or quality > 100:
        return ""
    return _BUCKET_KEYS[min(int(quality // 10), len(_BUCKET_KEYS) - 1)]


# The columns of the one pass over the scope's visits most blocks are computed from, and of their
# records (read apart and joined here: a visit's columns are not read again for each of its records)
SUMMARY_COLUMNS = (
    "pk",
    "key",
    "label",
    "activity_id",
    "visit_date",
    "status_group",
    "rating",
    "hact_q1",
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
    "entities_rated",
    "action_points",
)
RECORD_COLUMNS = (
    "visit_id",
    "datamart_id",
    "quality_score",
    "score_band",
    "rating",
    "hact_q1",
    "flags",
    "flag_count",
)


def summary(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """One pass over the scope's visits and their records (two queries), kept with the other blocks: the
    figures by month, score bucket, flag count, rating, field office, section and place, the
    governorates visited, the partners, and the visits without follow-up. Quality, bands, ratings,
    flags and the section lines count records; each group gives its distinct visits too. The reported
    visits, PSEA, the places without a location, the monitoring gaps and the follow-up count visits."""

    def compute() -> dict[str, Any]:
        nonlocal limits
        limits = limits or thresholds()
        high_flag = limits.get("high_flag", 3)
        months: dict[str, dict[str, Any]] = {}
        month_counts: dict[str, Counter] = {}  # per month, the records by their Q1 and their rating
        everyone: list[tuple] = []  # the part of each visit with records (see _parts_out)
        # per rating, its records (visit, score, band): each record under the rating it counts under
        by_rating: dict[str, list[tuple]] = {r: [] for r in RATINGS}
        visit_ratings: Counter = Counter()  # each visit under its own rating (its worst record's)
        scores: list[Decimal] = []  # the scored records' scores
        flag_counts: list[int] = []  # the scored records' numbers of flags
        flag_lists: list[list[str]] = []  # the flags of each flagged record
        offices: dict[str, dict[str, Any]] = {}
        sections: dict[str, dict[str, Any]] = {}
        places: dict[tuple, dict[str, Any]] = {}
        office_sources: Counter = Counter()
        governorates: set[str] = set()
        partners: set[int] = set()
        no_follow_up: list[tuple[str, str]] = []
        from .action_points import is_follow_up
        from .models import LocalActionPoint

        # the NeuroDB action points of visits, read once: the visits they follow up, the open ones
        followed: set[str] = set()
        local_open: dict[str, list] = {}
        for local_key, source, local_status, due in LocalActionPoint.objects.exclude(
            visit_key=""
        ).values_list("visit_key", "source", "status", "due_date"):
            if is_follow_up(source, local_status):
                followed.add(local_key)
            if local_status == LocalActionPoint.Status.OPEN:
                local_open.setdefault(local_key, []).append(due)
        today = _today()
        counts: Counter = Counter()
        # the records of the scope by visit (every filter applied: a record filter keeps its records)
        by_visit: dict[int, list[tuple]] = {}
        for record in scope.records().order_by().values_list(*RECORD_COLUMNS):
            found = by_visit.get(record[0])
            if found is None:
                by_visit[record[0]] = [record]
            else:
                found.append(record)
        # read at once: a year of visits is a few thousand rows, and a server-side cursor's round
        # trips cost more than the rows
        rows = scope.visits().order_by("visit_date", "key").values_list(*SUMMARY_COLUMNS)
        last_day, month, m, m_counts = None, "", None, None
        n_visits = 0
        for row in rows:
            (pk, key, label, activity_id, visit_date, group, visit_rating, visit_q1, visit_offices,
             offices_from, section_names, governorate_key, location_id, location_name, site_id, place_name,
             governorate_name, partner_ids, psea_flag, entities_rated, action_points) = row  # fmt: skip
            n_visits += 1
            visit_rating = visit_rating or "not_monitored"
            reported = group == "reported"
            visit_ratings["" if visit_rating == "not_monitored" and not reported else visit_rating] += 1
            counts["reported"] += reported
            counts["gaps"] += reported and not entities_rated
            counts["psea_asked"] += psea_flag is not None
            counts["psea_flagged"] += psea_flag is True
            counts["no_place"] += location_id is None and site_id is None
            counts["unlocated"] += not governorate_key
            # by month (start, else end date: every visit here has one; the rows come in date order)
            if visit_date != last_day:
                last_day = visit_date
                month = f"{visit_date.year:04d}-{visit_date.month:02d}"
                m = months.get(month)
                if m is None:
                    m = months[month] = {
                        "visits": 0,
                        "records": 0,
                        "reported": 0,
                        "q_sum": Decimal(0),
                        "q_n": 0,
                    }
                    month_counts[month] = Counter()
                m_counts = month_counts[month]
            m["visits"] += 1
            # the groups the visit counts in, each once: field offices ("none": not known), sections
            # ("none": no section) and its place (the gazetteer location, else the place eTools wrote)
            office_from = offices_from or "none"
            office_sources[office_from] += 1
            office_groups = []
            for name in visit_offices or ["none"]:
                o = offices.get(name)
                if o is None:  # (not setdefault: its default would be built for every visit)
                    o = offices[name] = {"parts": [], "from": Counter(), "flags": []}
                o["from"][office_from] += 1
                office_groups.append(o)
            section_groups = []
            for name in section_names or ["none"]:
                sec = sections.get(name)
                if sec is None:
                    sec = sections[name] = {"parts": [], "lines": [], "flagged": 0}
                section_groups.append(sec)
            if location_id is not None:
                place_key: tuple | None = ("location", location_id)
                name = location_name or place_name
            elif place_name:
                place_key, name = ("place", place_name), place_name
            else:
                place_key = None
            place = None
            if place_key is not None:
                place = places.get(place_key)
                if place is None:
                    place = places[place_key] = {
                        "name": name,
                        "location_id": location_id,
                        "governorate": governorate_name,
                        "parts": [],
                        "last": None,
                        "rated": 0,
                    }
                place["last"] = max(place["last"] or visit_date, visit_date)
                place["governorate"] = place["governorate"] or governorate_name
            # its records, each counted on its own: added up for the visit first (its part), then the
            # visit's part given to each group it counts in (the visit once)
            records = by_visit.get(pk)
            if records:
                q_sum = Decimal(0)
                q_n = high = medium = low = on_track = constrained = off_track = not_monitored = 0
                flagged = rated = 0
                lines_v: list[tuple] = []
                office_flags: list[str] = []
                for _visit, datamart_id, quality, band, rating, q1, flags, flag_count in records:
                    if not rating or rating == "not_monitored":
                        # counted_rating, written out (it runs for every record of the scope): Not
                        # monitored on a reported visit only, else not rated yet (in no rating)
                        rating = "not_monitored"
                        counted = rating if reported else ""
                    else:
                        counted = rating
                    if counted == "on_track":
                        on_track += 1
                        rated += 1
                    elif counted == "off_track":
                        off_track += 1
                        rated += 1
                    elif counted == "constrained":
                        constrained += 1
                        rated += 1
                    elif counted == "not_monitored":
                        not_monitored += 1
                    if counted:
                        m_counts[counted] += 1
                        if counted in by_rating:
                            by_rating[counted].append((pk, quality, band))
                    if band == "high":
                        high += 1
                    elif band == "medium":
                        medium += 1
                    elif band == "low":
                        low += 1
                    if quality is not None:
                        q_sum += quality
                        q_n += 1
                        scores.append(quality)
                        flag_counts.append(flag_count or 0)
                    if flags:
                        flagged += 1
                        flag_lists.append(flags)
                        if quality is not None:
                            office_flags.extend(flags)
                    if q1:
                        m_counts["q1:" + q1] += 1
                    # HACT Q1's Not monitored: its Q1 reads "not monitored", or nothing rated on a
                    # reported visit
                    if q1 == "not_monitored" or counted == "not_monitored":
                        counts["q1_not_monitored"] += 1
                    # a record's line (a tuple: only the first SECTION_LINES of a section are written out)
                    lines_v.append(
                        (
                            quality is None,
                            quality or 0,
                            visit_date,
                            key,
                            datamart_id,
                            quality,
                            activity_id,
                            label,
                            band,
                            rating,
                            group,
                        )
                    )
                n = len(records)
                part = (n, q_sum, q_n, high, medium, low, on_track, constrained, off_track, not_monitored)
                everyone.append(part)
                counts["off_track"] += off_track
                m["records"] += n
                if reported:
                    m["reported"] += n
                m["q_sum"] += q_sum
                m["q_n"] += q_n
                for o in office_groups:
                    o["parts"].append(part)
                    if office_flags:
                        o["flags"].extend(office_flags)  # counted once at the end
                for sec in section_groups:
                    sec["parts"].append(part)
                    sec["lines"].extend(lines_v)
                    sec["flagged"] += flagged
                if place is not None:
                    place["parts"].append(part)
                    place["rated"] += rated
            if governorate_key:
                governorates.add(governorate_key)
            if partner_ids:
                partners.update(partner_ids)
            if local_open and key in local_open:
                counts["local_open"] += len(local_open[key])
                counts["local_overdue"] += sum(1 for due in local_open[key] if due and due < today)
            if reported and not action_points and key not in followed:
                worse = max([visit_rating, visit_q1 or "not_monitored"], key=_rating_rank)
                if worse in ("off_track", "constrained"):
                    no_follow_up.append((key, _visit_name(key, label, activity_id)))

        # the month's records by Q1 and by rating, as the monthly charts read them ("q1:on_track")
        for month, found in month_counts.items():
            m = months[month]
            for code, n in found.items():
                m[code if code.startswith("q1:") else f"rating:{code}"] = n
        # the scored records by score bucket (each distinct score's bucket worked out once) and by
        # number of flags; the records not scored
        buckets: Counter = Counter()
        for quality, n in Counter(scores).items():
            buckets[_bucket(quality)] += n
        unscored = sum(part[0] for part in everyone) - len(scores)
        if unscored:
            buckets["none"] = unscored
        flags_dist: Counter = Counter()
        for count, n in Counter(flag_counts).items():
            flags_dist[_FLAG_KEYS[count] if count < 3 else "3+"] += n
            if count >= high_flag:
                counts["high_flag"] += n
        rule_flags = Counter(chain.from_iterable(flag_lists))
        out_sections = {}
        for name, sec in sections.items():
            lines = sec["lines"]
            # lowest score first, unscored last (a record's id ends the sort key): the first only
            first = heapq.nsmallest(SECTION_LINES, lines)
            out_sections[name] = {
                **_parts_out(sec["parts"]),
                "flagged": sec["flagged"],
                "lines": [
                    {
                        "key": key,
                        "record": datamart_id,
                        "name": f"#{activity_id}" if activity_id else label,  # _visit_name
                        "quality": quality,
                        "band": band,
                        "rating": rating,
                        "not_rated_yet": _not_rated_yet(rating, group),
                        "date": visit_date,
                    }
                    for visit_date, key, datamart_id, quality, activity_id, label, band, rating, group in (
                        ln[2:] for ln in first
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
                    "rated": p["rated"],
                    **_parts_out(p["parts"]),
                }
                for p in places.values()
            ),
            key=lambda p: (-p["visits"], -(p["last"].toordinal() if p["last"] else 0), p["name"].casefold()),
        )
        return {
            **_parts_out(everyone),
            "visits": n_visits,
            "reported": counts["reported"],
            "off_track": counts["off_track"],
            "gaps": counts["gaps"],
            "q1_not_monitored": counts["q1_not_monitored"],
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
            "by_rating": {code: _rated_out(code, found) for code, found in by_rating.items()},
            "visit_ratings": {code: visit_ratings[code] for code in RATINGS},
            "offices": {
                name: {**_parts_out(o["parts"]), "from": dict(o["from"]), "flags": dict(Counter(o["flags"]))}
                for name, o in offices.items()
            },
            "office_sources": dict(office_sources),
            "sections": out_sections,
            "places": out_places[:PLACES_MAX],
            "places_total": len(out_places),
            "governorates": sorted(governorates),
            "partners": sorted(partners),
            "no_follow_up": {"n": len(no_follow_up), "visits": no_follow_up[-FOLLOW_UP_LIST:][::-1]},
            "local_open": counts["local_open"],
            "local_overdue": counts["local_overdue"],
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
    """Block 4, quality score trends: the average quality of the records of the visits that started each
    month and the reports (the records of reported visits; two lines, each on its own axis); ``{}`` when
    no record of the scope is scored. A point opens the records of its month."""
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
                "label": "Average quality score",
                "unit": "%",
                "values": values,
                "reports": [months.get(k, {}).get("reported", 0) for k in keys],
            }
        ],
        "bar_name": "Average quality score",
        "line_name": "Total reports",
        "line_unit": "reports",
        "drill": {"labels": keys, "series": {}},
    }


def monthly_volume(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 5, monitoring volume over time: the records of the visits that started each month (bars,
    whatever their status; FMS counts records), with the distinct visits in the tooltip, and their
    average quality (line); ``{}`` when the scope has no visit."""
    axis, months = _months(scope, when, limits)
    if not months:
        return {}
    keys = [d.strftime("%Y-%m") for d in axis]
    return {
        "months": [_month_label(d) for d in axis],
        "indicators": [
            {
                "id": "records",
                "label": "Record count",
                "unit": "records",
                "values": [months.get(k, {}).get("records", 0) for k in keys],
                "extra": [months.get(k, {}).get("visits", 0) for k in keys],
                "reports": [
                    _float(mean_quality(months[k]["q_sum"], months[k]["q_n"])) if k in months else None
                    for k in keys
                ],
            }
        ],
        "bar_name": "Record count",
        "extra_label": "visits",
        "line_name": "Avg quality %",
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
    """Block 6: the records of each month by their own HACT Q1 (the record's answer, else its partner's,
    else the visit's): On track, Constrained, Off track. None when no record of the scope has a Q1
    rating."""
    return _by_month(scope, when, limits, "q1", ("on_track", "constrained", "off_track"))


def rating_by_month(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict | None:
    """Block 6 without Q1 answers: the records of each month by their overall rating."""
    return _by_month(scope, when, limits, "rating", RATINGS)


def q1_question(when: str | None = None) -> str:
    """The HACT Q1 question as eTools writes it (the most frequent text of the answers whose question
    is Q1, in the whole data), "" when no Q1 question was found. Kept ten minutes per refresh."""
    from .models import QuestionAnswer

    key = f"fmm:v5:q1:{when if when is not None else stamp()}"
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
SCORE_BUCKETS = (  # FMS's five buckets of 20 points (80-100 holds 100), each with its own colour
    ("0-20", "--fmm-score-0"),
    ("20-40", "--fmm-score-1"),
    ("40-60", "--fmm-score-2"),
    ("60-80", "--fmm-score-3"),
    ("80-100", "--fmm-score-4"),
)


def score_buckets(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 7: scored records in FMS's five score buckets of 20 points (80–100 holds 100), each with its
    drill value, its colour (red, orange, amber, light green, green, as FMS draws them) and the band of
    its lowest score (High from 80, Medium from 50, else Low, as Score settings set them); and the
    records not scored. Summed from the pass's buckets of 10 points: two of them make one of 20."""
    limits = limits or thresholds()
    data = summary(scope, when, limits)
    found = data["buckets"]
    items = []
    for drill, color in SCORE_BUCKETS:
        low, high = BUCKETS[drill]
        items.append(
            {
                "label": f"{low}–{high}",
                "value": found.get(f"{low}-{low + 10}", 0) + found.get(f"{low + 10}-{high}", 0),
                "color": color,
                "band": band_of(low, limits),
                "drill": drill,
            }
        )
    return {"items": items if data["scored"] else [], "not_scored": found.get("none", 0)}


def band_of(score: float, limits: dict[str, int]) -> str:
    """The band of a score under the thresholds set now: high, medium or low."""
    return "high" if score >= limits["band_high"] else "medium" if score >= limits["band_medium"] else "low"


def issue_label(rule: str, key: str, measures: list[float], setting: Any) -> str:
    """A recurring issue in words, written by NeuroDB from the rule's flag template and the detail code
    (the fields missing, the band, or "ai" for an AI check: its explanation differs from record to
    record, so the issue is the rule's flag without it)."""
    from .rules import COLUMNS, number, param, render

    template = getattr(setting, "flag_template", "") or ""
    label = getattr(setting, "label", "") or rule
    if not template:
        return f"{rule} {label}: flagged"
    if key.startswith("missing:") and setting is not None:
        specs = param(setting, "fields") or []
        names = []
        for part in key.split(":", 1)[1].split(","):
            if part.isdigit() and int(part) < len(specs):
                spec = specs[int(part)]
                names.append(spec.get("label") or COLUMNS.get(spec.get("name", ""), spec.get("name", "")))
        return render(template, missing_fields=", ".join(names))
    if key == "missing":
        name = COLUMNS.get(param(setting, "field", ""), "") if setting is not None else ""
        return f"{rule}: {name or label} not in the eTools data"
    value = ""
    if key == "band" and measures:
        low, high = min(measures), max(measures)
        value = number(low) if low == high else f"{number(low)}–{number(high)}"
    return render(template, value=value)


def top_issues(
    scope: Scope, limit: int = 10, when: str | None = None, rules: list | None = None
) -> list[dict[str, Any]]:
    """Block 8: the flags of the scope's records grouped by rule and detail (``R2:below_threshold``), most
    frequent first: the issue in words, its records, their mean urgency and the first visits (the
    visits of its most urgent records, each once)."""
    from .models import RecordRuleResult, RuleSetting, Visit
    from .rules import half_up
    from .scope import _ISSUE

    def compute() -> list[dict[str, Any]]:
        settings_ = {r.code: r for r in (rules if rules is not None else RuleSetting.objects.all())}
        # the failed checks of the scope's records, added up per issue and visit by the database (a
        # visit's records often share an issue)
        rows = (
            scope.record_results(RecordRuleResult.objects.filter(status="fail"))
            .order_by()
            .values("rule", "detail_key", "entity__visit_id")
            .annotate(
                n=Count("pk"),
                u_sum=Sum("entity__urgency"),
                u_n=Count("entity__urgency"),
                u_max=Max("entity__urgency"),
                low=Min("measure"),
                high=Max("measure"),
            )
            .values_list(
                "rule", "detail_key", "entity__visit_id", "n", "u_sum", "u_n", "u_max", "low", "high"
            )
        )
        groups: dict[tuple[str, str], list] = {}
        for rule, key, visit_id, n, u_sum, u_n, u_max, low, high in rows:
            g = groups.get((rule, key))
            if g is None:  # records, urgency sum, urgent records, lowest and highest measure, visits
                g = groups[(rule, key)] = [0, 0, 0, None, None, {}]
            g[0] += n
            if u_n:  # a record without a score has no urgency: left out of the mean
                g[1] += u_sum
                g[2] += u_n
            if low is not None and (g[3] is None or low < g[3]):
                g[3] = low
            if high is not None and (g[4] is None or high > g[4]):
                g[4] = high
            # each visit with its most urgent record's urgency
            g[5][visit_id] = u_max or 0
        # the chips: the visits of the most urgent records (each once), then the latest, then by key;
        # only the visits that can be among the first are read
        candidates: dict[tuple[str, str], list[int]] = {}
        for found, g in groups.items():
            visits = g[5]
            if len(visits) <= CHIP_VISITS:
                candidates[found] = list(visits)
            else:
                floor = heapq.nlargest(CHIP_VISITS, visits.values())[-1]
                candidates[found] = [pk for pk, urgency in visits.items() if urgency >= floor]
        named = {
            pk: (key, label, activity_id, day)
            for pk, key, label, activity_id, day in Visit.objects.filter(
                pk__in={pk for pks in candidates.values() for pk in pks}
            ).values_list("pk", "key", "label", "activity_id", "visit_date")
        }
        out = []
        for (rule, key), (n, urgency_sum, urgent_n, low, high, visits) in groups.items():
            drill = f"{rule}:{key}"
            first = heapq.nsmallest(
                CHIP_VISITS,
                (
                    (-visits[pk], -(named[pk][3].toordinal() if named[pk][3] else 0), named[pk][0], pk)
                    for pk in candidates[(rule, key)]
                    if pk in named
                ),
            )
            chips = [
                {"key": visit_key, "name": _visit_name(visit_key, named[pk][1], named[pk][2])}
                for _urgency, _day, visit_key, pk in first
            ]
            measures = [m for m in (low, high) if m is not None]
            out.append(
                {
                    "rule": rule,
                    "label": issue_label(rule, key, measures, settings_.get(rule)),
                    "records": n,
                    "visits": len(visits),
                    "urgency": int(half_up(Decimal(urgency_sum) / urgent_n, 0)) if urgent_n else None,
                    "lowest": low,
                    "chips": chips,
                    "more": max(len(visits) - CHIP_VISITS, 0),
                    "drill": drill if _ISSUE.match(drill) else "",
                }
            )
        out.sort(key=lambda r: (-r["records"], -(r["urgency"] or 0), r["rule"], r["label"]))
        return out

    return cached(scope, "issues", compute, when)[:limit]


def locations(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Blocks 9 and 19: the places of the scope's visits (the gazetteer location, else the site or
    place eTools wrote), most visited first: visits and their records, last visit, average quality per
    record ("—" when no record is scored), and coverage (rated records ÷ records); plus the visits with
    no place at all."""
    data = summary(scope, when, limits)
    rows = []
    for p in data["places"]:
        rows.append(
            {
                **p,
                "coverage": _pct(p["rated"], p["records"]),
                "drill": str(p["location_id"]) if p["location_id"] is not None else "",
            }
        )
    return {"rows": rows, "total": data["places_total"], "unlinked": data["no_place"]}


def _rule_rows(scope: Scope, by_month: bool) -> list[dict[str, Any]]:
    """Per rule (and month of the visits' date, start else end, ``by_month``), over the scope's
    records: the records in each result state, and the points earned (each capped at its maximum)
    over the points evaluated. One query."""
    from django.db.models import DecimalField
    from django.db.models.functions import Cast, Least, TruncMonth

    from .models import RecordRuleResult
    from .rules import STATES

    # grouped by rule and state, the points only where they count (each result is in one group): one
    # aggregate pass over the checks of the records, not a filtered count per state for every check
    with_points = Q(max_points__gt=0)
    rows = scope.record_results(RecordRuleResult.objects.all()).order_by()
    keys = ("rule", "status")
    if by_month:
        rows = rows.annotate(month=TruncMonth("entity__visit__visit_date"))
        keys = ("rule", "month", "status")
    found = rows.values(*keys).annotate(
        n=Count("pk"),
        earned=Sum(
            Least("points", Cast("max_points", DecimalField(max_digits=5, decimal_places=1))),
            filter=with_points,
        ),
        points_max=Sum("max_points", filter=with_points),
        points_n=Count("pk", filter=with_points),
    )
    out: dict[tuple, dict[str, Any]] = {}
    for row in found:
        group = (row["rule"], row["month"]) if by_month else (row["rule"],)
        r = out.get(group)
        if r is None:
            r = out[group] = {
                "rule": row["rule"],
                **({"month": row["month"]} if by_month else {}),
                **dict.fromkeys(STATES, 0),
                "earned": None,
                "points_max": None,
                "points_n": 0,
            }
        status = row["status"]
        if status in r:
            r[status] += row["n"]
        if status in ("pass", "fail") and row["points_n"]:  # the points evaluated: passed or flagged
            r["earned"] = (r["earned"] or 0) + (row["earned"] or 0)
            r["points_max"] = (r["points_max"] or 0) + (row["points_max"] or 0)
            r["points_n"] += row["points_n"]
    return list(out.values())


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
    """Per rule over the scope's records: the records in each result state, and the points earned (each
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


ANSWER_COLUMNS = ("fmq_answered_pct", "fmq_answered_categories", "method_count", "red_flag_count")


def _rule_settings(rules: list | None) -> list:
    """The rules as administrators set them (given by the page, which reads them once, or read here)."""
    from .models import RuleSetting

    return list(rules) if rules is not None else list(RuleSetting.objects.order_by("code"))


def rule_tone(share: Decimal | None) -> str:
    """FMS's colour of a rule by the share of its checked records it flagged: green under 25 %, amber
    from 25 % to 50 %, red over 50 %."""
    if share is None or share < 25:
        return "success"
    return "warning" if share <= 50 else "danger"


def rule_analysis(scope: Scope, rules: list | None = None, when: str | None = None) -> list[dict[str, Any]]:
    """Block 10: per rule, the records flagged out of the records evaluated (passed or flagged), the
    records where it was not available and those whose AI check is pending; "off" for a rule switched
    off and "flag only" for a rule without a deduction. In the order of the rule ids."""
    from .rules import TYPE_LABELS, code_order, nominal, param

    rules = sorted(_rule_settings(rules), key=lambda r: code_order(r.code))
    stats = rule_stats(scope, when)
    out = []
    for rule in rules:
        s = stats.get(rule.code, {})
        flagged, evaluated = s.get("fail", 0), s.get("fail", 0) + s.get("pass", 0)
        share = _pct(flagged, evaluated)
        if not rule.enabled:
            state = "off"
        elif not evaluated and s.get("pending", 0):
            state = "pending"
        elif not evaluated and s.get("na", 0):
            state = "na"
        elif not evaluated and s.get("off", 0):
            state = "ai_off"  # an AI check while the AI checks are switched off
        elif not evaluated:
            state = "none"
        else:
            state = "ok"
        deduction = nominal(rule)
        out.append(
            {
                "code": rule.code,
                "label": rule.label,
                "description": rule.description,
                "category": rule.category,
                "type": TYPE_LABELS.get(rule.type, rule.type),
                "points": deduction,
                "flag_only": not deduction,
                "state": state,
                "flagged": flagged,
                "evaluated": evaluated,
                "na": s.get("na", 0),
                "pending": s.get("pending", 0),
                "share": share,
                "fill": float(share or 0),
                "level": "danger"
                if share is not None and share >= 50
                else "warning"
                if flagged
                else "success",
                # Quality rule analysis (FMS): the bar is the share of the checked records not flagged,
                # green under 25 % flagged, amber up to 50 %, red over 50 %
                "clean_fill": float(100 - share) if share is not None else 0.0,
                "tone": rule_tone(share),
                # a rule read from the checklist answers (fm_questions), "not available" without them
                "needs_answers": param(rule, "field", "") in ANSWER_COLUMNS,
            }
        )
    return out


def issues_summary(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 11: the records R6 flagged and the high-flag records (each with its share of the scored
    records), and the monitoring gaps: reported visits none of whose records is rated (§0.3)."""
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
    """Block 12: the scored records by their number of flags (0, 1, 2, 3 or more)."""
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
    """Block 15: per field office, its records and visits, their average quality and where the office
    came from (per visit); the records whose visit's office is not known in one row. A record of a visit
    to a PD with two offices counts in both. Most records first."""
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
    rows.sort(key=lambda r: (-r["records"], -r["visits"], r["name"].casefold()))
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
    """Block 17: per field office, its scored records and, for each rule with flags, the scored records
    it flagged ("R1: 6/16")."""
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
        out.append(
            {
                "name": name,
                "drill": name,
                "scored": scored,
                "records": o["records"],
                "visits": o["visits"],
                "badges": badges,
            }
        )
    out.sort(key=lambda r: (r["name"] == "none", -r["scored"], r["name"].casefold()))
    return out


def sections(scope: Scope, when: str | None = None, limits: dict | None = None) -> list[dict[str, Any]]:
    """Block 18: per eTools section, its records and visits, their average quality, ratings and bands,
    and a line per record (worst score first, unscored last). A record of a visit with two sections
    counts in both."""
    data = summary(scope, when, limits)
    out = [
        {"name": name, "drill": name, **s}
        for name, s in data["sections"].items()
        if _within(scope.sections, name)
    ]
    out.sort(key=lambda r: (r["name"] == "none", -r["records"], r["name"].casefold()))
    return out


BREAKDOWN_KINDS = ("partner", "office", "section")
BREAKDOWN_COLUMNS = (
    "partner_id",
    "visit_id",
    "visit__offices",
    "visit__section_names",
    "quality_score",
    "score_band",
    "rating",
    "visit__status_group",
    "flags",
    "visit__action_points_open",
)


def breakdown(
    scope: Scope, by: str, when: str | None = None, limits: dict | None = None
) -> list[dict[str, Any]]:
    """The exports' aggregates (Excel workbook, PDF report, Power BI package) per partner, field office
    or section (``by``), over the scope's records: the records and their distinct visits, the rated
    records (On track, Constrained, Off track) and each rating's share of them, the average quality with
    the bands, the records flagged (and their visits) and their flags, and the open FM action points of
    the visits (each visit once). A record counts under its own partner, and in each of its visit's
    offices or sections ("none": without one), as the page's field office and section blocks count it,
    with the same ratings and average (:class:`Tally`, :func:`counted_rating`); only the rows the filter
    keeps are given. Most records first."""
    from neurodb.partnerships.models import PartnerOrganization

    if by not in BREAKDOWN_KINDS:
        raise ValueError(f"breakdown by {by!r}")

    def compute() -> list[dict[str, Any]]:
        groups: dict[Any, dict[str, Any]] = {}
        rows = scope.records().order_by("visit_id").values_list(*BREAKDOWN_COLUMNS)
        for partner_id, visit, offices, sections, quality, band, rating, group, flags, open_points in rows:
            keys = {"partner": [partner_id] if partner_id else [], "office": offices, "section": sections}[
                by
            ] or ["none"]
            counted = counted_rating(rating or "not_monitored", group)
            for key in keys:
                g = groups.get(key)
                if g is None:
                    g = groups[key] = {
                        "tally": Tally(),
                        "flagged": 0,
                        "flagged_visits": 0,
                        "flags": 0,
                        "open": 0,
                        "flagged_last": None,
                    }
                tally = g["tally"]
                if tally.last != visit:  # the visit's action points, once per group
                    g["open"] += open_points or 0
                tally.add(quality, band, counted, visit)
                if flags:
                    g["flagged"] += 1
                    if g["flagged_last"] != visit:  # the visits with a flagged record, each once
                        g["flagged_visits"] += 1
                        g["flagged_last"] = visit
                g["flags"] += len(flags or ())
        names: dict[Any, tuple[str, str, str]] = {}
        if by == "partner":
            names = {
                p.pk: (p.short_name or p.name, p.name, p.vendor_number or "")
                for p in PartnerOrganization.objects.filter(pk__in=[k for k in groups if k != "none"]).only(
                    "pk", "name", "short_name", "vendor_number"
                )
            }
        kept = {"partner": scope.partners, "office": scope.offices, "section": scope.sections}[by]
        out = []
        for key, g in groups.items():
            if kept and key not in kept:  # a multi-valued row the filter does not keep (_within)
                continue
            tally = g["tally"].out()
            rated = sum(tally["ratings"][code] for code in RATED)
            short, full, vendor = names.get(key, (str(key), str(key), ""))
            out.append(
                {
                    "key": key,
                    "name": "" if key == "none" else short,
                    "full_name": "" if key == "none" else full,
                    "vendor_number": vendor,
                    **tally,
                    "rated": rated,
                    "shares": {code: _pct(tally["ratings"][code], rated) for code in RATED},
                    "flagged": g["flagged"],
                    "flagged_visits": g["flagged_visits"],
                    "flags": g["flags"],
                    "open_action_points": g["open"],
                }
            )
        out.sort(key=lambda r: (r["key"] == "none", -r["records"], str(r["name"]).casefold()))
        return out

    return cached(scope, f"breakdown:{by}", compute, when)


def quality_by_rating(
    scope: Scope, when: str | None = None, limits: dict | None = None
) -> list[dict[str, Any]]:
    """Block 20: per finding rating, its records (each under its own rating), their visits, average
    quality and bands; Not monitored (a record of a reported visit with nothing rated: planned, not
    conducted) always has its own bar."""
    from .scope import RATING_LABELS

    data = summary(scope, when, limits)
    out = []
    for code in RATINGS:
        row = data["by_rating"][code]
        if (row["records"] or code == "not_monitored") and _within(scope.ratings, code):
            avg = row["avg"]
            out.append({"code": code, "label": RATING_LABELS[code], "fill": float(avg or 0), **row})
    return out


def flag_frequency(scope: Scope, rules: list | None = None, when: str | None = None) -> dict[str, Any]:
    """Block 21: the records each rule flagged, most first, as ``[label, records, drill]`` triples for the
    chart, with each rule's share of the records it evaluated."""
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
                "fill": float(_pct(flagged, evaluated) or 0),  # the bar: the share of the records it checked
            }
        )
    rows.sort(key=lambda r: (-r["n"], r["code"]))
    return {"rows": rows, "pairs": [[r["label"], r["n"], r["code"]] for r in rows if r["n"]]}


def dimension_breakdown(
    scope: Scope, rules: list | None = None, when: str | None = None, setting=None
) -> dict[str, Any]:
    """Block 22, points by category: per score category, its weight and the points the scored records
    kept of it on average (the weight less the category's deductions, each at most the weight), weakest
    first; the categories no rule switched on uses are listed apart."""
    from .models import ScoreSetting
    from .rules import half_up
    from .score import categories_of, category_labels

    rules = _rule_settings(rules)
    setting = setting or ScoreSetting.load()
    weights = categories_of(setting)
    labels = category_labels(setting)

    def compute() -> dict[str, Any]:
        # one aggregate: each record's deduction per category is stored already capped at its weight
        sums = {
            f"c{i}": Sum(
                Cast(
                    KeyTextTransform(key, "category_deductions"),
                    DecimalField(max_digits=12, decimal_places=2),
                )
            )
            for i, key in enumerate(weights)
        }
        found = scope.records().exclude(quality_score=None).aggregate(n=Count("id"), **sums)
        totals = {key: str(found[f"c{i}"] or 0) for i, key in enumerate(weights)}
        return {"n": found["n"], "totals": totals}

    data = cached(scope, "categories", compute, when)
    used = {r.category for r in rules if r.enabled}
    rows, unavailable = [], []
    for key, weight in weights.items():
        if key not in used or not weight:
            unavailable.append({"code": key, "label": labels.get(key, key), "points": weight})
            continue
        if not data["n"]:
            continue
        lost = Decimal(data["totals"].get(key, "0")) / data["n"]
        kept = half_up(max(Decimal(0), weight - lost), 1)
        pct = min(Decimal(100), half_up(Decimal(100) * kept / weight, 1))
        rows.append(
            {
                "code": key,
                "label": labels.get(key, key),
                "earned": kept,
                "max": half_up(weight, 1),
                "pct": pct,
                "fill": float(pct),
                "records": data["n"],
            }
        )
    rows.sort(key=lambda r: (r["pct"], r["code"]))
    flag_only = [r.code for r in rules if r.enabled and not r.deduction]
    return {"rows": rows, "unavailable": unavailable, "flag_only": flag_only}


def rule_trends(scope: Scope, rules: list | None = None, when: str | None = None) -> dict[str, Any]:
    """Rule score trends over time: per rule with points, the share of its maximum points earned by the
    records of the visits that started each month (the results that passed or failed; each capped at its
    maximum); a month where the rule checked no record has no value. ``{}`` when no rule checked one."""
    from .rules import code_order, half_up, max_deduction

    rules = [r for r in _rule_settings(rules) if r.enabled and max_deduction(r)]
    found: dict[tuple[str, str], float] = {}
    for row in rule_months(scope, when):
        if row["month"] and row["points_max"]:
            share = half_up(Decimal(100) * Decimal(row["earned"] or 0) / Decimal(row["points_max"]), 1)
            found[(row["rule"], row["month"])] = float(min(Decimal(100), share))
    if not found:
        return {}
    axis = _month_axis(scope, {month for _rule, month in found})
    keys = [d.strftime("%Y-%m") for d in axis]
    shown = [
        r for r in sorted(rules, key=lambda r: code_order(r.code)) if any((r.code, k) in found for k in keys)
    ]
    if not shown:
        return {}
    return {
        "labels": [_month_label(d) for d in axis],
        "series": {f"{r.code} {r.label}": [found.get((r.code, k)) for k in keys] for r in shown},
        "drill": {"labels": keys, "series": {f"{r.code} {r.label}": r.code for r in shown}},
    }


def highlights(scope: Scope, when: str | None = None, limits: dict | None = None) -> dict[str, Any]:
    """Block 13: visits and their records, reported visits, governorates covered, the average quality
    per record (the KPI's own definition), off-track records, PSEA-flagged visits out of those with a
    PSEA question, the scored records by High / Medium / Low band and the records by kind."""
    data = summary(scope, when, limits)
    kinds = entity_kinds(scope, when)
    from .scope import KIND_LABELS

    return {
        "visits": data["visits"],
        "records": data["records"],
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
    "visit__visit_date",
    "quality_score",
    "score_band",
    "flags",
    "visit__status_group",
    "urgency",
    "datamart_id",
    "partner__partner_type",
)
# The type badge of a partner in the entity table, from the start of its eTools partner type ("Civil Society
# Organization" or "CSO" -> "CSO partner", as FMS writes it)
PARTNER_TYPES = (
    (("civil", "cso"), "CSO partner"),
    (("government", "gov"), "Government partner"),
    (("un agency", "un "), "UN agency"),
    (("bilateral",), "Bilateral partner"),
)


def partner_type_label(partner_type: str | None) -> str:
    """A partner's type badge: "CSO partner", "Government partner"…, else "Partner"."""
    text = (partner_type or "").strip().casefold()
    for starts, label in PARTNER_TYPES:
        if text and (text.startswith(starts) or text in {s.strip() for s in starts}):
            return label
    return ENTITY_TYPE_LABELS["partner"]


def _year(scope: Scope) -> int:
    """The calendar year a block of the scope reads plans for: the year chosen, else the period's last."""
    return scope.year if scope.preset == "year" and scope.year else scope.end.year


def entity_kinds(scope: Scope, when: str | None = None) -> dict[str, int]:
    """The records of the scope (a record filter keeps the matching records only) counted by the kind of
    their entity: the key figures' own count (:func:`kpis`), so no query of its own."""
    return kpis(scope, when)["record_kinds"]


def entity_rows(scope: Scope, when: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """The entities of the scope's records (a record filter keeps the matching records only): the records
    by kind, and per kind each entity with its records and their visits, its own average quality and
    bands (each record is scored on its own), its most frequent flag and its latest rating; PD rows add
    the visits planned for the year. With ``kind``, only the entities of that kind are worked out (the
    table shows one kind at a time)."""
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
        # the records in the entities' own order (most urgent first), sorted here rather than by the
        # database, which sorted the whole joined rows (a third of the block at 15,000 rows)
        found = scope.records().order_by()
        if kind == "other":
            found = found.filter(Q(kind="other") | Q(kind=""))
        elif kind:
            found = found.filter(kind=kind)
        rows = list(found.values_list(*ENTITY_COLUMNS))
        # most urgent first; a record without urgency (not scored) after every one that has it
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
            cp_output, rating, visit_id, visit_date, quality, band, flags, status_group = row[7:15]
            partner_type = row[17]
            row_kind = row_kind or "other"
            text = (entity or "").strip()
            if row_kind == "pd" and pd_id:
                key, name, link = ("pd", pd_id), pd_number or text, ("pd", pd_id)
            elif row_kind == "partner" and partner_id:
                name = partner_name or partner_short or text  # the full name, as FMS writes it
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
                g = of_kind[key] = {
                    "name": name,
                    "link": link,
                    "tally": Tally(),
                    "visits": set(),
                    "flags": Counter(),
                    "last": None,
                    "planned": planned.get(pd_id),
                    "type": partner_type_label(partner_type)
                    if row_kind == "partner"
                    else ENTITY_TYPE_LABELS.get(row_kind, ENTITY_TYPE_LABELS["other"]),
                }
            g["tally"].add(quality, band, counted_rating(rating, status_group))
            g["visits"].add(visit_id)
            if flags:
                g["flags"].update(flags)
            last = g["last"]
            if last is None or (visit_date or datetime.date.min) > (last[0] or datetime.date.min):
                g["last"] = (visit_date, rating, status_group)
        out: dict[str, list[dict[str, Any]]] = {}
        for group_kind, entities in groups.items():
            rows_out = []
            for g in entities.values():
                flags = g["flags"]
                last = g["last"]
                top = min(flags.items(), key=lambda kv: (-kv[1], kv[0])) if flags else None
                rows_out.append(
                    {
                        "name": g["name"],
                        "kind": group_kind,
                        "type": g["type"],
                        "link": g["link"],
                        "planned": g["planned"],
                        "top_issue": {"rule": top[0], "n": top[1]} if top else None,
                        "last": {
                            "date": last[0],
                            "rating": last[1],
                            "not_rated_yet": _not_rated_yet(last[1], last[2]),
                        },
                        **g["tally"].out(),
                        "visits": len(g["visits"]),
                    }
                )
            rows_out.sort(key=_worst_first)
            out[group_kind] = rows_out
        return {"kinds": entity_kinds(scope, when), "entities": out, "year": year}

    return cached(scope, f"entities:{kind or 'all'}", compute, when)


def _worst_first(row: dict[str, Any]) -> tuple:
    """Worst average quality first, unscored last; then the most records, then the name."""
    return (row["avg"] is None, row["avg"] or 0, -row["records"], row["name"].casefold())


def entities_performance(scope: Scope, kind: str = "all", when: str | None = None) -> dict[str, Any]:
    """Block 16: the entities of one kind, or of every kind (``"all"``, FMS's default: partners, CP
    outputs, PD/SSFAs and the rest in one table, each with its type), worst average quality first,
    unscored last."""
    if kind == "all":
        data = entity_rows(scope, when, None)
        rows = sorted((row for found in data["entities"].values() for row in found), key=_worst_first)
    else:
        data = entity_rows(scope, when, kind)
        rows = data["entities"].get(kind, [])
    return {
        "kind": kind,
        "rows": rows,
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
    open), the NeuroDB action points on them (open, and overdue), the reported off-track or constrained
    visits without either, and the overdue FM ones, oldest due date first, with their visits."""
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
        found = summary(scope, when, limits)
        return {
            "local_open": found["local_open"],
            "local_overdue": found["local_overdue"],
            "linked": len(points),
            "open": len(open_),
            "overdue": len(overdue),
            "high_open": sum(p["high"] for p in open_),
            "overdue_list": overdue[:10],
            "without": found["no_follow_up"],
        }

    return cached(scope, "action_points", compute, when)
