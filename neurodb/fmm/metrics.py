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

Most blocks read one pass over the scope's visits, each with its records added up by the database, and
the scope's records grouped by month and by score (:func:`summary`), so a tab runs a handful of queries
whatever the number of visits; the rule results, the entity table, HACT figures and action points have
one query each. Where the filters keep whole visits (no record filter), the records and their checks
are read added up already, as the refresh keeps them per visit (``tallies``): the same figures, from a
few thousand rows instead of every record and check. Every drill value a block gives (``drill``) is a
code the scope parses, and the records a drill-down lists are exactly those the block counted (for a
block that counts visits, the records of those visits).

Every block is cached for ten minutes under ``fmm:v6:<scope hash>:<stamp>:<block>`` and the language
(:func:`cached`; :func:`stamp`: the last refresh run, its rules version, the day, the NeuroDB action
points, and the eTools and gazetteer inputs no refresh follows), so a refresh, a rescore, a new day or an
edit shows at once, and for the rest of the request that read it (:func:`request_started`). The blocks the
parts of a page and the other workers read (:func:`summary`, :func:`entity_rows`, :func:`rule_counts` and
the Map tab's ``geo.map_points``) are kept in the cache every worker shares (``shared=True``, through
:func:`neurodb.core.compute.once`); the others in each worker's own.
"""

from __future__ import annotations

import datetime
import hashlib
import heapq
import re
import threading
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import EmptyResultSet
from django.db.models import Count, DecimalField, Max, Min, Q, QuerySet, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.translation import get_language

from neurodb.core import compute as core_compute
from neurodb.datamart.fm import RATING_ORDER

from . import tallies
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


# The jobs that write what the figures read but start no refresh (the eTools sync's programme documents, their
# planned places and the partners' names; the gazetteer; the eTools update clean-up): a run of one that
# finished moves the stamp. The eTools Datamart sync starts a refresh when it wrote what the refresh reads.
INPUT_JOBS = ("etools", "locations", "datamart_backfill")


def stamp(last: Any = _READ) -> str:
    """What every cached figure depends on besides its scope: the last refresh that finished (given,
    or read), the rules version it computed the scores with, the day (rolling periods move at
    midnight), the last change to the NeuroDB action points (they count as follow-up at once), and what
    staff or a job change without a refresh: the last run of the :data:`INPUT_JOBS` that finished, and
    the gazetteer's places and their types (an administrator may move or rename one in the admin: the
    map's planned places and the places lists read them). One query. Read with them, for the rest of the
    request: whether the figures kept per visit are those of the stored records (``tallies.ready``; the
    figures are the same either way, only the reading differs)."""
    from django.db import connection

    from neurodb.core.models import SyncRun
    from neurodb.geo.models import Location, LocationType

    from . import status
    from .models import FigureState, LocalActionPoint

    last = status.last_refresh() if last is _READ else last
    version = (last.details or {}).get("rules_version", 0) if last else 0
    quote = connection.ops.quote_name

    def edited(model) -> str:  # "<rows>|<last change>" of a table staff edit in the admin
        return "(SELECT count(*)::text || '|' || coalesce(max({at})::text, '') FROM {table})".format(  # noqa: S608
            at=quote(model._meta.get_field("modified").column), table=quote(model._meta.db_table)
        )

    runs = SyncRun._meta
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), max({at}), (SELECT {valid} FROM {state} WHERE {pk} = 1), "  # noqa: S608
            "(SELECT {run_pk} FROM {runs} WHERE {finished} IS NOT NULL AND {job} IN ({jobs}) "
            "ORDER BY {finished} DESC, {run_pk} DESC LIMIT 1), {places}, {kinds} FROM {local}".format(
                at=quote(LocalActionPoint._meta.get_field("updated_at").column),
                valid=quote(FigureState._meta.get_field("valid").column),
                state=quote(FigureState._meta.db_table),
                pk=quote(FigureState._meta.pk.column),
                run_pk=quote(runs.pk.column),
                runs=quote(runs.db_table),
                finished=quote(runs.get_field("finished_at").column),
                job=quote(runs.get_field("job").column),
                jobs=", ".join(["%s"] * len(INPUT_JOBS)),
                places=edited(Location),
                kinds=edited(LocationType),
                local=quote(LocalActionPoint._meta.db_table),
            ),
            list(INPUT_JOBS),
        )
        n, at, valid, run, places, kinds = cursor.fetchone()
    tallies.remember(bool(valid))
    changed = f"{n}.{at.timestamp():.6f}" if at else "0"
    inputs = hashlib.blake2b(f"{run}|{places}|{kinds}".encode(), digest_size=6).hexdigest()
    return f"{last.pk if last else 0}:{version}:{timezone.localdate().isoformat()}:{changed}:{inputs}"


# The blocks the current request has read already, by their cache key: a request reads the figures of its
# scope many times over (the one pass some fifteen times on the Analysis tab), and each read from the cache
# unpickles them again. Kept for that request only (from ``request_started`` to ``request_finished``, each
# thread its own); outside a request (a job, a test) every read goes to the cache. The figures a block
# gives are never changed in place by its readers: they build their own rows from them.
_request = threading.local()


def request_started(**kwargs: Any) -> None:
    _request.blocks = {}
    tallies.request_started()


def request_finished(**kwargs: Any) -> None:
    _request.blocks = None
    tallies.request_finished()


def _key(scope: Scope, block: str, when: str | None) -> str:
    """A block's key in its worker's cache: the scope, the stamp, the block and the language (dates, labels
    and words are translated as a block is computed: one language never answers another)."""
    stamp_ = when if when is not None else stamp()
    return f"fmm:v6:{scope.hash()}:{stamp_}:{block}|lang={get_language() or '-'}"


def cached(
    scope: Scope, block: str, compute: Callable[[], Any], when: str | None = None, *, shared: bool = False
) -> Any:
    """``compute()`` for ``scope``, kept ten minutes (not in DEBUG), and for the rest of the request;
    ``when`` is :func:`stamp`, read once per request by the caller. A scope that drills into the reviews
    is never kept: a review saved a minute ago must count at once, and no refresh marks it.

    ``shared=True``: kept in the cache every worker shares (:func:`neurodb.core.compute.once`: one request
    at a time computes it, the others read what it stored; the language goes in its key there too), for a
    block the parts of a page or the other workers read. Only for a block of plain data (no QuerySet, no
    model instance: ``once`` refuses them in the tests) that reads nothing of the person asking (the scope
    holds the sections, a user's own default included), with every input staff edit in ``block`` or in
    the stamp."""
    if settings.DEBUG or any(key == "review" for key, _value in scope.drill):
        return compute()
    key = _key(scope, block, when)
    blocks = getattr(_request, "blocks", None)
    if blocks is not None and key in blocks:
        return blocks[key]
    if shared:  # (the language goes in the key there, as once() adds it)
        found = core_compute.once(key.split("|lang=", 1)[0], compute, CACHE_SECONDS)
    else:
        found = cache.get(key)
        if found is None:
            found = compute()
            cache.set(key, found, CACHE_SECONDS)
    if blocks is not None:
        blocks[key] = found
    return found


def kept(scope: Scope, block: str, when: str | None = None) -> Any:
    """What :func:`cached` keeps for ``scope``'s ``block`` in this worker (None when nothing is kept),
    never computed."""
    if settings.DEBUG or any(key == "review" for key, _value in scope.drill):
        return None
    key = _key(scope, block, when)
    blocks = getattr(_request, "blocks", None)
    if blocks is not None and key in blocks:
        return blocks[key]
    return cache.get(key)


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


def _rated_out(code: str, n: int, visits: int, q_sum: Any, q_n: int, bands: Counter) -> dict[str, Any]:
    """:meth:`Tally.out` of the ``n`` records that count under the rating ``code`` (of ``visits`` visits;
    ``q_sum`` and ``q_n`` their scores' sum and number, ``bands`` their bands), as :func:`summary` adds
    them up."""
    return {
        "records": n,
        "visits": visits,
        "avg": mean_quality(q_sum, q_n),
        "scored": q_n,
        "bands": {band: bands.get(band, 0) for band in BANDS},
        "ratings": {rating: n if rating == code else 0 for rating in RATINGS},
    }


_FLAG_KEYS = ("0", "1", "2")  # the flags-per-record chart's keys below "3+"
_BUCKET_KEYS = tuple(CHART_BUCKETS)  # in order: bucket n holds the scores from 10n to below 10(n + 1)


def _bucket(quality: Decimal) -> str:
    """The score bucket of a score (0 to 100), as the ``bucket`` drill-down reads it (90-100 holds
    100): worked out, not searched, as the one pass over the records calls it for every scored one."""
    if quality < 0 or quality > 100:
        return ""
    return _BUCKET_KEYS[min(int(quality // 10), len(_BUCKET_KEYS) - 1)]


# The columns of the one pass over the scope's visits most blocks are computed from (their records are
# added up by the database, or kept added up per visit: :func:`_visit_rows`; and :func:`_record_groups`)
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
_UNRATED = ("", "not_monitored")  # a record's rating that counts as Not monitored on a reported visit only
_ZERO = Decimal(0)  # the score sum of a visit without scored records


# The scope's visits (``SUMMARY_COLUMNS``, in date and key order), each followed by its records added up
# (``tallies.FIGURE_COLUMNS``; none for a visit without records the filter keeps)
VISIT_ROWS_SQL = """
    SELECT v.*, {figures} FROM ({visits}) v {records} ORDER BY v.visit_date, v.key
"""


def _visit_rows(scope: Scope) -> list[tuple]:
    """The scope's visits in date and key order (``SUMMARY_COLUMNS``), each with its records' figures
    (``tallies.FIGURE_COLUMNS``; None for a visit without records): those kept per visit when the filters
    keep whole visits (``tallies.usable``), else the records the filters keep added up by the database
    (``tallies.record_figures``). In one query read at once: a year of visits is a few thousand rows,
    and a server-side cursor's round trips cost more than the rows."""
    from django.db import connection

    from .models import VisitFigures

    visits = _sql(scope.visits().order_by().values_list(*SUMMARY_COLUMNS))
    if visits is None:  # a filter that keeps nothing
        return []
    columns = tallies.FIGURE_COLUMNS
    if tallies.usable(scope):
        records = (connection.ops.quote_name(VisitFigures._meta.db_table), ())
    else:
        found = _sql(tallies.record_figures(scope.records()))
        records = None if found is None else (f"({found[0]})", found[1])
    if records is None:  # no record kept: no visit has figures
        figures, join, params = ", ".join("NULL" for _column in columns), "", visits[1]
    else:
        figures = ", ".join(f"f.{column}" for column in columns)
        join, params = f"LEFT JOIN {records[0]} f ON f.visit_id = v.pk", (*visits[1], *records[1])
    with connection.cursor() as cursor:
        cursor.execute(VISIT_ROWS_SQL.format(figures=figures, visits=visits[0], records=join), params)
        return cursor.fetchall()


def _sql(qs: QuerySet) -> tuple[str, tuple] | None:
    """The SQL and parameters of ``qs`` (a ``values_list`` whose annotations come in the order they were
    made; None when a filter keeps nothing), to be read without Django's converter for each value of each
    row: allowed only where the database's own value is the one the converter would give (a count's
    integer, a sum's Decimal; a column without one)."""
    query = qs.query
    names = [*query.extra_select, *query.values_select, *query.annotation_select]
    if names != list(qs._fields):  # (the columns would come in another order)
        raise ValueError(f"the columns of {names} are not read in the order asked for")
    compiler = query.get_compiler(qs.db)
    try:
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        return None
    connection = compiler.connection
    for expression, _sql, alias in compiler.select:
        kind = expression.output_field.get_internal_type()
        same = kind == "DecimalField" or kind.endswith("IntegerField")
        for converter in (
            *connection.ops.get_db_converters(expression),
            *expression.get_db_converters(connection),
        ):
            if not (same and converter == expression.convert_value):
                raise ValueError(f"the column {alias} needs its converter")
    return sql, tuple(params)


def _rows(qs: QuerySet) -> list[tuple]:
    """The rows of ``qs`` as the database gives them (:func:`_sql`)."""
    from django.db import connection

    found = _sql(qs)
    if found is None:  # a filter that keeps nothing
        return []
    with connection.cursor() as cursor:
        cursor.execute(*found)
        return cursor.fetchall()


# the scope's records grouped two ways in one pass: by rating, HACT Q1, the month of their visit and
# whether their visit is reported (``GROUPING(score_band)`` 1), and by rating, whether their visit is
# reported, band, score and number of flags (0)
RECORD_GROUPS_SQL = """
    SELECT r.rating, r.hact_q1, date_trunc('month', r.visit__visit_date::timestamp)::date, r.reported,
           r.score_band, r.quality_score, r.flag_count, GROUPING(r.score_band), count(*),
           sum(r.quality_score), count(r.quality_score)
    FROM ({records}) r
    GROUP BY GROUPING SETS (
        (r.rating, r.hact_q1, date_trunc('month', r.visit__visit_date::timestamp)::date, r.reported),
        (r.rating, r.reported, r.score_band, r.quality_score, r.flag_count)
    )
"""


def _record_groups(scope: Scope) -> tuple[list[tuple], list[tuple]]:
    """The scope's records grouped by what the monthly charts read: (rating, Q1, month of their visit
    ("2026-05"), on a reported visit, records, score sum, scored); and by what the score buckets, the
    flag counts and the quality by rating read: (rating, band, score, number of flags, on a reported
    visit, records). One query."""
    from django.db import connection
    from django.db.models import BooleanField, ExpressionWrapper

    reported = ExpressionWrapper(Q(visit__status_group="reported"), output_field=BooleanField())
    records = (
        scope.records()
        .order_by()
        .annotate(reported=reported)
        .values_list(
            "rating", "hact_q1", "visit__visit_date", "score_band", "quality_score", "flag_count", "reported"
        )
    )
    try:
        sql, params = records.query.sql_with_params()
    except EmptyResultSet:  # a filter that keeps nothing
        return [], []
    with connection.cursor() as cursor:
        cursor.execute(RECORD_GROUPS_SQL.format(records=sql), params)
        rows = cursor.fetchall()
    by_month, groups = [], []
    months: dict[Any, str] = {}
    for rating, q1, day, on_reported, band, quality, flag_count, monthly, n, q_sum, q_n in rows:
        if monthly:
            month = months.get(day)
            if month is None:
                month = months[day] = f"{day.year:04d}-{day.month:02d}"
            by_month.append((rating, q1, month, on_reported, n, q_sum, q_n))
        else:
            groups.append((rating, band, quality, flag_count, on_reported, n))
    return by_month, groups


def summary(scope: Scope, when: str | None = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
    """One pass over the scope's visits, each with its records added up by the database, and the scope's
    records grouped by month, rating and Q1, and by score, band and number of flags (three queries), kept
    with the other blocks: the figures by month, score bucket, flag count, rating, field office, section
    and place, the governorates visited, the partners, and the visits without follow-up. Quality, bands,
    ratings and flags count records; each group gives its distinct visits too. The reported visits,
    PSEA, the places without a location, the monitoring gaps and the follow-up count visits. The records
    listed under each section are :func:`section_lines`'.

    Kept in the cache every worker shares (the Analysis and Quality tabs, the AI card's brief and the chat
    read it), under the thresholds administrators set (Score settings: the bands, the urgency levels and
    the number of flags that is many), which it reads: a saved setting shows at once, before the rescore it
    starts has finished."""
    limits = limits or core_compute.per_request("fmm.thresholds", thresholds)

    def build() -> dict[str, Any]:
        high_flag = limits.get("high_flag", 3)
        months: dict[str, dict[str, Any]] = {}
        everyone: list[tuple] = []  # the part of each visit with records (see _parts_out)
        visit_ratings: Counter = Counter()  # each visit under its own rating (its worst record's)
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
        counts: Counter = Counter()  # the figures counted by month and by record group below
        # every visit of the scope runs through this loop: what it counts is kept in local names and the
        # flags as their text, split once at the end
        rows = _visit_rows(scope)
        reported_n = gaps = psea_asked = psea_flagged = no_place = unlocated = off_track_n = 0
        on_track_visits = constrained_visits = off_track_visits = not_monitored_visits = 0
        local_open_n = local_overdue = 0
        office_from: Counter = Counter()  # (the visit's field offices, where they come from)
        office_lists: dict[tuple, list] = {}
        section_lists: dict[tuple, list] = {}
        flag_texts: list[str] = []  # the flags of the records flagged, as each visit's text
        last_day, m = None, None
        add_part = everyone.append
        for (_pk, key, label, activity_id, visit_date, group, visit_rating, visit_q1, visit_offices,
             offices_from, section_names, governorate_key, location_id, location_name, site_id, place_name,
             governorate_name, partner_ids, psea_flag, entities_rated, action_points,
             n, q_sum, q_n, high, medium, low, on_track, constrained, off_track, unrated, flagged,
             scored_flags, unscored_flags) in rows:  # fmt: skip
            visit_rating = visit_rating or "not_monitored"
            reported = group == "reported"
            visit_ratings["" if visit_rating == "not_monitored" and not reported else visit_rating] += 1
            if reported:
                reported_n += 1
                if not entities_rated:
                    gaps += 1
            if psea_flag is not None:
                psea_asked += 1
                if psea_flag is True:
                    psea_flagged += 1
            if location_id is None and site_id is None:
                no_place += 1
            if not governorate_key:
                unlocated += 1
            # by month (start, else end date: every visit here has one; the rows come in date order); its
            # records are counted by month below
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
            m["visits"] += 1
            # the groups the visit counts in, each once: field offices ("none": not known), sections
            # ("none": no section) and its place (the gazetteer location, else the place eTools wrote)
            # (the groups of each list of offices and of sections are looked up once)
            source = offices_from or "none"
            office_sources[source] += 1
            names = tuple(visit_offices) if visit_offices else ("none",)
            office_groups = office_lists.get(names)
            if office_groups is None:
                office_groups = office_lists[names] = []
                for name in names:
                    o = offices.get(name)
                    if o is None:
                        o = offices[name] = {"parts": [], "flags": []}
                    office_groups.append(o)
            office_from[names, source] += 1
            names = tuple(section_names) if section_names else ("none",)
            section_groups = section_lists.get(names)
            if section_groups is None:
                section_groups = section_lists[names] = []
                for name in names:
                    sec = sections.get(name)
                    if sec is None:
                        sec = sections[name] = {"parts": [], "flagged": 0}
                    section_groups.append(sec)
            place = None
            if location_id is not None:
                place = places.get(("location", location_id))
                if place is None:
                    place = places["location", location_id] = {
                        "name": location_name or place_name,
                        "location_id": location_id,
                        "governorate": governorate_name,
                        "parts": [],
                        "last": None,
                        "rated": 0,
                    }
            elif place_name:
                place = places.get(("place", place_name))
                if place is None:
                    place = places["place", place_name] = {
                        "name": place_name,
                        "location_id": location_id,
                        "governorate": governorate_name,
                        "parts": [],
                        "last": None,
                        "rated": 0,
                    }
            if place is not None:
                place["last"] = visit_date  # (the rows come in date order: the latest so far)
                if not place["governorate"]:
                    place["governorate"] = governorate_name
            # its records, added up by the database: the visit's part, given to each group it counts in
            # (the visit once). A blank rating or "not monitored" counts as Not monitored on a reported
            # visit only, else in no rating (not rated yet: counted_rating)
            if n is not None:
                not_monitored = unrated if reported else 0
                part = (
                    n,
                    _ZERO if q_sum is None else q_sum,
                    q_n,
                    high,
                    medium,
                    low,
                    on_track,
                    constrained,
                    off_track,
                    not_monitored,
                )
                add_part(part)
                if on_track:
                    on_track_visits += 1
                if constrained:
                    constrained_visits += 1
                if off_track:
                    off_track_n += off_track
                    off_track_visits += 1
                if not_monitored:
                    not_monitored_visits += 1
                if scored_flags:
                    flag_texts.append(scored_flags)
                    for o in office_groups:
                        o["parts"].append(part)
                        o["flags"].append(scored_flags)  # counted once at the end
                else:
                    for o in office_groups:
                        o["parts"].append(part)
                if unscored_flags:
                    flag_texts.append(unscored_flags)
                for sec in section_groups:
                    sec["parts"].append(part)
                    sec["flagged"] += flagged
                if place is not None:
                    place["parts"].append(part)
                    place["rated"] += on_track + constrained + off_track
            if governorate_key:
                governorates.add(governorate_key)
            if partner_ids:
                partners.update(partner_ids)
            if local_open and key in local_open:
                local_open_n += len(local_open[key])
                local_overdue += sum(1 for due in local_open[key] if due and due < today)
            if reported and not action_points and key not in followed:
                worse = visit_q1 or "not_monitored"  # the worse of the two, the visit's on a tie
                if RATING_ORDER.get(worse, 0) <= RATING_ORDER.get(visit_rating, 0):
                    worse = visit_rating
                if worse in ("off_track", "constrained"):
                    no_follow_up.append((key, _visit_name(key, label, activity_id)))
        n_visits = len(rows)
        # per rating, the visits with a record that counts under it (counted_rating)
        rating_visits = {
            "on_track": on_track_visits,
            "constrained": constrained_visits,
            "off_track": off_track_visits,
            "not_monitored": not_monitored_visits,
        }
        # the flags of the records flagged, each counted (in the order the visits first name them)
        rule_flags = Counter(",".join(flag_texts).split(",")) if flag_texts else Counter()
        # the month's records, those of its reported visits and their scores, then by Q1 and by the
        # rating each counts under, as the monthly charts read them ("q1:on_track", "rating:on_track");
        # HACT Q1's Not monitored: a Q1 that reads "not monitored", or nothing rated on a reported visit
        month_counts: dict[str, Counter] = {}
        by_month, groups = _record_groups(scope)
        for rating, q1, month, on_reported, n, q_sum, q_n in by_month:
            m = months.get(month)
            if m is None:  # a month no visit read above has: a refresh committed between the two reads
                continue
            m["records"] += n
            if on_reported:
                m["reported"] += n
            if q_sum is not None:
                m["q_sum"] += q_sum
            m["q_n"] += q_n
            if rating in _UNRATED:
                rating = "not_monitored" if on_reported else ""
            found = month_counts.get(month)
            if found is None:
                found = month_counts[month] = Counter()
            if rating:
                found["rating:" + rating] += n
            if q1:
                found["q1:" + q1] += n
            if q1 == "not_monitored" or rating == "not_monitored":
                counts["q1_not_monitored"] += n
        for month, m in months.items():  # each month's codes in the order the old pass met them
            for code, n in sorted(month_counts.get(month, {}).items()):
                m[code] = n
        # the records by score bucket (each distinct score's bucket worked out once), by number of flags
        # (the scored ones) and under the rating each counts under, with their scores and bands; the
        # records not scored
        buckets: Counter = Counter()
        flags_dist: Counter = Counter()
        by_rating = {code: [0, Decimal(0), 0, Counter()] for code in RATINGS}
        unscored = 0
        for rating, band, quality, flag_count, on_reported, n in groups:
            if quality is None:
                unscored += n
            else:
                buckets[_bucket(quality)] += n
                count = flag_count or 0
                flags_dist[_FLAG_KEYS[count] if count < 3 else "3+"] += n
                if count >= high_flag:
                    counts["high_flag"] += n
            if rating in _UNRATED:
                rating = "not_monitored" if on_reported else ""
            r = by_rating.get(rating)
            if r is not None:
                r[0] += n
                if quality is not None:
                    r[1] += quality * n
                    r[2] += n
                r[3][band] += n
        if unscored:
            buckets["none"] = unscored
        sources_of: dict[str, dict[str, int]] = {name: {} for name in offices}  # (in the order met)
        for (names, source), n in office_from.items():
            for name in names:
                found = sources_of[name]
                found[source] = found.get(source, 0) + n
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
            "reported": reported_n,
            "off_track": off_track_n,
            "gaps": gaps,
            "q1_not_monitored": counts["q1_not_monitored"],
            "psea_asked": psea_asked,
            "psea_flagged": psea_flagged,
            "no_place": no_place,
            "unlocated": unlocated,
            "high_flag": counts["high_flag"],
            "high_flag_at": high_flag,
            "buckets": dict(buckets),
            "flags": dict(flags_dist),
            "rule_flags": dict(rule_flags),
            "months": months,
            "by_rating": {
                code: _rated_out(code, n, rating_visits[code], q_sum, q_n, bands)
                for code, (n, q_sum, q_n, bands) in by_rating.items()
            },
            "visit_ratings": {code: visit_ratings[code] for code in RATINGS},
            "offices": {
                name: {
                    **_parts_out(o["parts"]),
                    "from": sources_of[name],
                    "flags": dict(Counter(",".join(o["flags"]).split(","))) if o["flags"] else {},
                }
                for name, o in offices.items()
            },
            "office_sources": dict(office_sources),
            "sections": {
                name: {**_parts_out(sec["parts"]), "flagged": sec["flagged"]}
                for name, sec in sections.items()
            },
            "places": out_places[:PLACES_MAX],
            "places_total": len(out_places),
            "governorates": sorted(governorates),
            "partners": sorted(partners),
            "no_follow_up": {"n": len(no_follow_up), "visits": no_follow_up[-FOLLOW_UP_LIST:][::-1]},
            "local_open": local_open_n,
            "local_overdue": local_overdue,
        }

    return cached(scope, f"summary:{limits_key(limits)}", build, when, shared=True)


def limits_key(limits: dict[str, int]) -> str:
    """The thresholds a block reads, in its key: ``red.amber.band_high.band_medium.high_flag``."""
    names = ("red", "amber", "band_high", "band_medium", "high_flag")
    return ".".join(str(limits.get(name, "")) for name in names)


LINE_COLUMNS = (
    "visit__key",
    "datamart_id",
    "quality_score",
    "visit__activity_id",
    "visit__label",
    "score_band",
    "rating",
    "visit__status_group",
    "visit__visit_date",
)


NAME_COLUMNS = ("kind", "entity", "cp_output", "pd__number", "partner__name")  # a record's name (record_name)


def _line(key, datamart_id, quality, activity_id, label, band, rating, group, visit_date) -> dict[str, Any]:
    """A record's line under its sections (a blank rating reads as "not monitored", as the records'
    figures count it)."""
    rating = "not_monitored" if not rating or rating == "not_monitored" else rating
    return {
        "key": key,
        "record": datamart_id,
        "name": f"#{activity_id}" if activity_id else label,  # _visit_name
        "quality": quality,
        "band": band,
        "rating": rating,
        "not_rated_yet": _not_rated_yet(rating, group),
        "date": visit_date,
    }


def section_lines(
    scope: Scope, when: str | None = None, limits: dict | None = None, top: int = SECTION_LINES
) -> dict[str, list[dict[str, Any]]]:
    """Per section of the scope (:func:`summary`'s; "none": the visits without a section), its first
    ``top`` records (at most ``SECTION_LINES``): lowest score first, unscored last, then by visit date,
    visit key and record. The scope's records are read in that order until each section has its lines
    (a section read to its end without them, its own records only); fewer lines than ``SECTION_LINES``
    are the first of the full lists when those are kept already."""
    return _section_lines(scope, when, limits, top)[0]


def section_line_names(
    scope: Scope, when: str | None = None, limits: dict | None = None, top: int = SECTION_LINES
) -> dict[int, str]:
    """The entity of each record :func:`section_lines` lists (``datamart_id``), in words
    (:func:`record_name`; a record without one is left out), read with the lines."""
    return _section_lines(scope, when, limits, top)[1]


def _section_lines(
    scope: Scope, when: str | None, limits: dict | None, top: int
) -> tuple[dict[str, list[dict[str, Any]]], dict[int, str]]:
    from django.db.models import F
    from django.db.models.functions import Collate

    top = min(top, SECTION_LINES)
    if top < SECTION_LINES:
        full = kept(scope, "section_lines", when)
        if full is not None:
            return {name: lines[:top] for name, lines in full[0].items()}, full[1]

    def compute() -> tuple[dict[str, list[dict[str, Any]]], dict[int, str]]:
        sections = summary(scope, when, limits)["sections"]
        out: dict[str, list[dict[str, Any]]] = {name: [] for name in sections}
        named: dict[int, str] = {}
        if not sections or scope.empty:
            return out, named
        order = (
            F("quality_score").asc(nulls_last=True),
            "visit__visit_date",
            Collate("visit__key", "C"),  # as Python compares the keys, whatever the database's collation
            "datamart_id",
        )
        wanted = {name: min(s["records"], top) for name, s in sections.items()}
        missing = {name for name, n in wanted.items() if n}
        width = len(LINE_COLUMNS)

        def line_of(row: tuple) -> dict[str, Any]:
            """The line of a record read with its name (``LINE_COLUMNS``, then ``NAME_COLUMNS``)."""
            kind, entity, cp_output, pd_number, partner_name = row[width : width + len(NAME_COLUMNS)]
            text = record_name(kind, entity, cp_output, pd_number or "", partner_name or "")
            if text:
                named[row[1]] = text
            return _line(*row[:width])

        def of_section(name: str) -> Q:
            return Q(visit__section_names=[]) if name == "none" else Q(visit__section_names__contains=[name])

        def read(records: QuerySet, lines: dict[str, list]) -> None:
            """The first records of ``records`` (enough, for sections of some size, to give each of
            ``lines`` its lines), each under the sections of ``lines`` its visit has, until each has its
            lines."""
            rows = _rows(
                records.order_by(*order).values_list(*LINE_COLUMNS, *NAME_COLUMNS, "visit__section_names")[
                    : top * 2 * len(lines)
                ]
            )
            for row in rows:
                line = None
                for name in row[-1] or ["none"]:
                    found = lines.get(name)
                    if found is not None and len(found) < top:
                        found.append(line if line is not None else (line := line_of(row)))
                        if len(found) == wanted[name]:
                            missing.discard(name)
                if not missing:
                    break

        # the first records of the scope; then the first records of the sections still without their
        # lines (a section's records come in the same order); a section still without them after that:
        # its own records, in the same order
        read(scope.records(), out)
        if len(missing) > 1:
            rest = {name: [] for name in missing}
            either = Q()
            for name in sorted(missing):
                either |= of_section(name)
            read(scope.records().filter(either), rest)
            out.update((name, lines) for name, lines in rest.items() if name not in missing)
        for name in sorted(missing):
            found = scope.records().filter(of_section(name)).order_by(*order)
            out[name] = [line_of(row) for row in _rows(found.values_list(*LINE_COLUMNS, *NAME_COLUMNS)[:top])]
        return out, named

    return cached(scope, "section_lines" if top == SECTION_LINES else f"section_lines:{top}", compute, when)


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


# The failed checks of a scope's records added up per issue (rule and detail) and visit (a visit's records
# often share an issue), then per issue (its rows, ``visit`` empty), and the visits that can be among each
# issue's first (fewer than CHIP_VISITS of its visits have a more urgent record: the chips are picked from
# them), each with its most urgent record's urgency
ISSUE_SQL = """
WITH g AS MATERIALIZED ({grouped})
SELECT rule, detail_key, sum(n)::bigint, sum(u_sum)::bigint, sum(u_n)::bigint, min(low), max(high), count(*),
       NULL::bigint, NULL::int
FROM g GROUP BY rule, detail_key
UNION ALL
SELECT rule, detail_key, NULL, NULL, NULL, NULL, NULL, NULL, visit_id, u_max
FROM (
    SELECT g.*, rank() OVER (PARTITION BY rule, detail_key ORDER BY coalesce(u_max, 0) DESC) AS place FROM g
) AS ranked
WHERE place <= {chips}
"""


def _issue_rows(scope: Scope) -> list[tuple]:
    """The rows of :data:`ISSUE_SQL` for ``scope``: (rule, detail, records, urgency sum, urgent records,
    lowest and highest measure, visits, visit, its most urgent record's urgency). The failed checks per
    issue and visit are those kept per visit when the filters keep whole visits (``tallies.usable``),
    else those of the records the filters keep."""
    from django.db import connection

    from .models import RecordRuleResult, VisitIssueTally

    if tallies.usable(scope):
        grouped = scope.of_visits(VisitIssueTally.objects.all()).values_list(
            "rule", "detail_key", "visit_id", *tallies.ISSUE_COLUMNS
        )
    else:
        grouped = tallies.issue_tallies(scope.record_results(RecordRuleResult.objects.all()))
    try:
        found = tallies.select(grouped)  # (summed by the database: no column is read as it stands)
    except EmptyResultSet:  # a filter that keeps nothing
        return []
    columns = "rule, detail_key, visit_id, n, u_sum, u_n, u_max, low, high"
    with connection.cursor() as cursor:
        cursor.execute(
            ISSUE_SQL.format(grouped=f"SELECT * FROM ({found[0]}) AS found ({columns})", chips=CHIP_VISITS),  # noqa: S608
            found[1],
        )
        return cursor.fetchall()


def top_issues(
    scope: Scope, limit: int = 10, when: str | None = None, rules: list | None = None
) -> list[dict[str, Any]]:
    """Block 8: the flags of the scope's records grouped by rule and detail (``R2:below_threshold``), most
    frequent first: the issue in words, its records, their mean urgency and the first visits (the
    visits of its most urgent records, each once)."""
    from .models import RuleSetting, Visit
    from .rules import half_up
    from .scope import _ISSUE

    def compute() -> list[dict[str, Any]]:
        settings_ = {r.code: r for r in (rules if rules is not None else RuleSetting.objects.all())}
        # per issue its records, their urgency (summed over the records that have one, which the mean
        # counts), lowest and highest measure and their visits; and the visits that can be among its
        # first (:func:`_issue_rows`)
        groups: dict[tuple[str, str], tuple] = {}
        candidates: dict[tuple[str, str], dict[int, int]] = {}
        for rule, key, n, u_sum, u_n, low, high, n_visits, visit_id, u_max in _issue_rows(scope):
            if visit_id is None:
                groups[(rule, key)] = (n, u_sum or 0, u_n, low, high, n_visits)
            else:  # each visit with its most urgent record's urgency
                candidates.setdefault((rule, key), {})[visit_id] = u_max or 0
        # the chips: the visits of the most urgent records (each once), then the latest, then by key
        named = {
            pk: (key, label, activity_id, day)
            for pk, key, label, activity_id, day in Visit.objects.filter(
                pk__in={pk for found in candidates.values() for pk in found}
            ).values_list("pk", "key", "label", "activity_id", "visit_date")
        }
        out = []
        for (rule, key), (n, urgency_sum, urgent_n, low, high, n_visits) in groups.items():
            drill = f"{rule}:{key}"
            visits = candidates.get((rule, key), {})
            first = heapq.nsmallest(
                CHIP_VISITS,
                (
                    (-urgency, -(named[pk][3].toordinal() if named[pk][3] else 0), named[pk][0], pk)
                    for pk, urgency in visits.items()
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
                    "visits": n_visits,
                    "urgency": int(half_up(Decimal(urgency_sum) / urgent_n, 0)) if urgent_n else None,
                    "lowest": low,
                    "chips": chips,
                    "more": max(n_visits - CHIP_VISITS, 0),
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


def _rule_tallies(scope: Scope) -> tuple[QuerySet, str]:
    """The rule figures kept for ``scope`` (``tallies.usable``): per day when no filter but the period
    applies (``tallies.by_day``), else per visit of the scope; and the date their month is read from."""
    from .models import DayRuleTally, VisitRuleTally

    if tallies.by_day(scope):
        return DayRuleTally.objects.filter(day__gte=scope.start, day__lte=scope.end).order_by(), "day"
    return scope.of_visits(VisitRuleTally.objects.all()).order_by(), "visit__visit_date"


def _rule_rows(scope: Scope, by_month: bool) -> list[dict[str, Any]]:
    """Per rule (and month of the visits' date, start else end, ``by_month``), over the scope's
    records: the records in each result state, and the points earned (each capped at its maximum)
    over the points evaluated. One query."""
    from django.db.models.functions import TruncMonth

    from .models import RecordRuleResult
    from .rules import STATES

    keys = ("rule", "month", "status") if by_month else ("rule", "status")
    if tallies.usable(scope):
        # the checks per rule and result kept per visit (``tallies.rule_tallies``), or per day when no
        # filter but the period applies, summed
        rows, day = _rule_tallies(scope)
        if by_month:
            rows = rows.annotate(month=TruncMonth(day))
        found = rows.values(*keys).annotate(
            n=Sum("records"), earned=Sum("earned"), points_max=Sum("points_max"), points_n=Sum("points_n")
        )
    else:
        # grouped by rule and state, the points only where they count (each result is in one group):
        # one aggregate pass over the checks of the records, not a filtered count per state for every
        # check. The checks are those of the scope's records (without the months, picked by their
        # record alone, not joined to it), read from their covering index (``fmm_result_points``):
        # nothing counted is a column the index lacks
        if by_month:
            rows = scope.record_results(RecordRuleResult.objects.all()).order_by()
            rows = rows.annotate(month=TruncMonth("entity__visit__visit_date"))
        else:
            rows = RecordRuleResult.objects.filter(entity__in=scope.records().values("pk")).order_by()
        found = rows.values(*keys).annotate(n=Count("*"), **tallies.rule_points())
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


def rule_counts(scope: Scope, when: str | None = None) -> dict[str, dict[str, int]]:
    """Per rule over the scope's records, the records in each result state: :func:`rule_stats` without
    the points, which the rule analysis and the flag frequency read. Taken from the rule figures or the
    rule months when they are kept already, else one count per rule and state (cheaper than the
    points: no sum over every check)."""
    from .models import RecordRuleResult
    from .rules import STATES

    def compute() -> dict[str, dict[str, int]]:
        stats = kept(scope, "rules", when)
        if stats is not None:
            return {rule: {state: s[state] for state in STATES} for rule, s in stats.items()}
        months = kept(scope, "rule_months", when)
        if months is not None:
            rows = ((row["rule"], state, row[state]) for row in months for state in STATES)
        elif tallies.usable(scope):  # the checks per rule and result kept per visit or day, summed
            rows = _rows(
                _rule_tallies(scope)[0]
                .values("rule", "status")
                .annotate(n=Sum("records"))
                .values_list("rule", "status", "n")
            )
        else:
            rows = _rows(
                RecordRuleResult.objects.filter(entity__in=scope.records().values("pk"))
                .order_by()
                .values("rule", "status")
                .annotate(n=Count("*"))
                .values_list("rule", "status", "n")
            )
        out: dict[str, dict[str, int]] = {}
        for rule, status, n in rows:
            r = out.get(rule)
            if r is None:
                r = out[rule] = dict.fromkeys(STATES, 0)
            if status in r:
                r[status] += n
        return out

    return cached(scope, "rule_counts", compute, when, shared=True)


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
    stats = rule_counts(scope, when)
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


def sections(
    scope: Scope, when: str | None = None, limits: dict | None = None, lines: int | None = SECTION_LINES
) -> list[dict[str, Any]]:
    """Block 18: per eTools section, its records and visits, their average quality, ratings and bands,
    and a line per record (worst score first, unscored last), up to ``SECTION_LINES`` (``more``: the
    records past them). ``lines``: the first lines only (the page's before "Show all"); None: the figures
    alone, without reading the lines. A record of a visit with two sections counts in both."""
    data = summary(scope, when, limits)
    found = section_lines(scope, when, limits, lines) if lines is not None else None
    out = [
        {
            "name": name,
            "drill": name,
            **s,
            **(
                {"lines": found.get(name, []), "more": max(s["records"] - SECTION_LINES, 0)}
                if found is not None
                else {}
            ),
        }
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
    stats = rule_counts(scope, when)
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

    return cached(scope, f"entities:{kind or 'all'}", compute, when, shared=True)


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
            p = points.get(pk)
            if p is None:  # (an action point of several visits comes once per visit)
                p = points[pk] = {
                    "reference": reference,
                    "category": category,
                    "due": due,
                    "high": bool(high),
                    "open": status_ in ActionPoint.OPEN_STATUSES,
                    "visits": [],
                }
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
