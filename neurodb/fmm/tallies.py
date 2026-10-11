"""The figures kept per visit for the pages (``metrics``): each visit's records added up
(:class:`~neurodb.fmm.models.VisitFigures`), its failed checks per issue
(:class:`~neurodb.fmm.models.VisitIssueTally`) and its checks per rule and result
(:class:`~neurodb.fmm.models.VisitRuleTally`).

A page whose filters keep whole visits (no record filter: :func:`usable`) adds up these rows of its
visits instead of reading every record and every check of the period: some 5,000 and 10,000 rows
instead of 15,000 records and 180,000 checks at production size. The figures are the same exactly:
each row is what the pages' own queries give (:func:`record_figures`, :func:`issue_tallies`,
:func:`rule_tallies`, which the pages run on the records themselves for a record filter), grouped by
visit as well, and every page groups them further by summing (counts and sums, the lowest and highest).

The rows are written by the refresh, in the transaction that writes the records and their checks
(:func:`rebuild`: every visit after a full refresh, the visits whose records or checks changed after a
scores-only one), and read only while they are those of the stored records and checks (:func:`ready`).
Any change to the records or their checks, whatever makes it (the refresh, an administrator, a test),
marks them out of date at once: a trigger of the database clears :class:`~neurodb.fmm.models.FigureState`
(migration 0021), and the pages then add up the records and checks themselves until the next refresh
writes the rows again.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.db.models import Count, DecimalField, Max, Min, Q, QuerySet, Sum
from django.db.models.functions import Cast, Least
from django.utils import timezone

if TYPE_CHECKING:
    from .scope import Scope

# the columns of a visit's records added up, in the order :func:`record_figures` gives them (after the
# visit), as :class:`VisitFigures` keeps them
FIGURE_COLUMNS = (
    "records",
    "q_sum",
    "q_n",
    "high",
    "medium",
    "low",
    "on_track",
    "constrained",
    "off_track",
    "unrated",
    "flagged",
    "scored_flags",
    "unscored_flags",
)
ISSUE_COLUMNS = ("records", "urgency_sum", "urgency_n", "urgency_max", "low", "high")
RULE_COLUMNS = ("records", "earned", "points_max", "points_n")
UNRATED = ("", "not_monitored")  # a record's rating that counts as Not monitored on a reported visit only

# Whether the rows are those of the stored records and checks, read once per request (``metrics.stamp``
# reads it with the other things every figure depends on); outside a request, at each use
_request = threading.local()


def _flags_text(scored: bool):
    """The flags of the records flagged, scored or not, comma-separated (a rule's code holds no comma)."""
    from django.contrib.postgres.aggregates import StringAgg
    from django.db.models import CharField, F, Func, Value

    return StringAgg(
        Func(F("flags"), Value(","), function="array_to_string", output_field=CharField()),
        delimiter=",",
        filter=Q(quality_score__isnull=not scored) & ~Q(flags=[]),
    )


def record_figures(records: QuerySet) -> QuerySet:
    """Per visit of ``records`` (records), those records added up (``visit_id``, then
    :data:`FIGURE_COLUMNS`): their number, the sum and number of their scores, their High / Medium / Low
    bands, their ratings (on track, constrained, off track; blank or "not monitored": ``unrated``, Not
    monitored on a reported visit only), the records flagged, and the flags of the scored and of the
    unscored records flagged."""
    return (
        records.order_by()
        .values("visit_id")
        .annotate(
            records=Count("*"),
            q_sum=Sum("quality_score"),
            q_n=Count("quality_score"),
            high=Count("pk", filter=Q(score_band="high")),
            medium=Count("pk", filter=Q(score_band="medium")),
            low=Count("pk", filter=Q(score_band="low")),
            on_track=Count("pk", filter=Q(rating="on_track")),
            constrained=Count("pk", filter=Q(rating="constrained")),
            off_track=Count("pk", filter=Q(rating="off_track")),
            unrated=Count("pk", filter=Q(rating__in=UNRATED)),
            flagged=Count("pk", filter=~Q(flags=[])),
            scored_flags=_flags_text(True),
            unscored_flags=_flags_text(False),
        )
        .values_list("visit_id", *FIGURE_COLUMNS)
    )


def issue_tallies(results: QuerySet) -> QuerySet:
    """The failed checks of ``results`` (rule results) per issue (rule and detail) and visit: ``rule``,
    ``detail_key``, the visit, then :data:`ISSUE_COLUMNS` (the records, the sum, number and highest of
    their urgencies, the lowest and highest measure)."""
    return (
        results.filter(status="fail")
        .order_by()
        .values("rule", "detail_key", "entity__visit_id")
        .annotate(
            records=Count("*"),
            urgency_sum=Sum("entity__urgency"),
            urgency_n=Count("entity__urgency"),
            urgency_max=Max("entity__urgency"),
            low=Min("measure"),
            high=Max("measure"),
        )
        .values_list("rule", "detail_key", "entity__visit_id", *ISSUE_COLUMNS)
    )


def rule_points() -> dict:
    """The aggregates of the points of a rule's checks (:data:`RULE_COLUMNS` but the records), over those
    whose rule takes points off: the points kept, each capped at its maximum, the most it takes off,
    and their number."""
    with_points = Q(max_points__gt=0)
    return {
        "earned": Sum(
            Least("points", Cast("max_points", DecimalField(max_digits=5, decimal_places=1))),
            filter=with_points,
        ),
        "points_max": Sum("max_points", filter=with_points),
        "points_n": Count("max_points", filter=with_points),
    }


def rule_tallies(results: QuerySet) -> QuerySet:
    """The checks of ``results`` (rule results) per visit, rule and result: the visit, ``rule``,
    ``status``, then :data:`RULE_COLUMNS` (the records, and the points: :func:`rule_points`)."""
    return (
        results.order_by()
        .values("entity__visit_id", "rule", "status")
        .annotate(records=Count("*"), **rule_points())
        .values_list("entity__visit_id", "rule", "status", *RULE_COLUMNS)
    )


# ------------------------------------------------------------------------------------------ reading
def request_started() -> None:
    _request.active, _request.valid = True, None


def request_finished() -> None:
    _request.active, _request.valid = False, None


def remember(valid: bool | None) -> None:
    """Note, for the rest of the request, whether the rows are those of the stored records and checks
    (``metrics.stamp`` reads it at the start of the request); None: read it again when asked."""
    if getattr(_request, "active", False):
        _request.valid = None if valid is None else bool(valid)


def valid_now() -> bool:
    """Whether the rows kept are those of the stored records and checks, read now."""
    from .models import FigureState

    return FigureState.objects.filter(pk=1, valid=True).exists()


def ready() -> bool:
    """Whether the rows kept are those of the stored records and checks (read once per request)."""
    if getattr(_request, "active", False) and _request.valid is not None:
        return _request.valid
    valid = valid_now()
    remember(valid)
    return valid


def usable(scope: Scope) -> bool:
    """Whether ``scope``'s figures can be added up from the rows kept per visit: its filters keep whole
    visits (no record filter or drill-down) and the rows are those of the stored records and checks."""
    return not scope.empty and not scope.record_filtered and ready()


def by_day(scope: Scope) -> bool:
    """Whether ``scope``'s rule figures can be added up from the rows kept per day: no filter but the
    period applies (every visit of the period counts, whole) and the rows are up to date."""
    return usable(scope) and not scope.visit_filtered


# ------------------------------------------------------------------------------------------ writing
def _sources(visit_ids: list[int] | None) -> list[tuple[type, list[str], QuerySet]]:
    """Each table kept per visit, its columns, and the query that gives its rows (of ``visit_ids``)."""
    from .models import RecordRuleResult, VisitEntity, VisitFigures, VisitIssueTally, VisitRuleTally

    records = VisitEntity.objects.all()
    results = RecordRuleResult.objects.all()
    if visit_ids is not None:
        records = records.filter(visit_id__in=visit_ids)
        results = results.filter(entity__visit_id__in=visit_ids)
    return [
        (VisitFigures, ["visit_id", *FIGURE_COLUMNS], record_figures(records)),
        (VisitIssueTally, ["rule", "detail_key", "visit_id", *ISSUE_COLUMNS], issue_tallies(results)),
        (VisitRuleTally, ["visit_id", "rule", "status", *RULE_COLUMNS], rule_tallies(results)),
    ]


def day_rule_tallies(rows: QuerySet) -> QuerySet:
    """``rows`` (:class:`VisitRuleTally` rows) summed per visit date (the undated visits' left out), rule
    and result: the day, ``rule``, ``status``, then :data:`RULE_COLUMNS`."""
    return (
        rows.filter(visit__visit_date__isnull=False)
        .order_by()
        .values("visit__visit_date", "rule", "status")
        .annotate(
            records=Sum("records"),
            earned=Sum("earned"),
            points_max=Sum("points_max"),
            points_n=Sum("points_n"),
        )
        .values_list("visit__visit_date", "rule", "status", *RULE_COLUMNS)
    )


def _insert(cursor, model: type, columns: list[str], source: QuerySet) -> None:
    quote = connection.ops.quote_name
    sql, params = select(source)
    names = ", ".join(quote(model._meta.get_field(c.removesuffix("_id")).column) for c in columns)
    cursor.execute(f"INSERT INTO {quote(model._meta.db_table)} ({names}) {sql}", params)  # noqa: S608


def hold() -> bool:
    """The figure state locked for the rest of the current transaction (its row made when missing), to
    be called before the transaction writes any record or check: a change to the records or checks
    another transaction makes meanwhile then waits (its trigger updates this row) and marks the figures
    out of date once they are written, and a change committed before shows here. Returns whether the
    figures were up to date."""
    from .models import FigureState

    table = connection.ops.quote_name(FigureState._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"INSERT INTO {table} (id, valid) VALUES (1, false) ON CONFLICT (id) DO NOTHING")  # noqa: S608
        cursor.execute(f"SELECT valid FROM {table} WHERE id = 1 FOR UPDATE")  # noqa: S608
        return bool(cursor.fetchone()[0])


def rebuild(visit_ids: Iterable[int] | None = None) -> None:
    """The rows of every visit (``visit_ids`` None), or of the visits given (their records or checks
    changed: the others' rows are theirs already), written again from the stored records and checks,
    then those of their days, and marked as theirs. Run in the transaction that wrote the records and
    checks, after its last write to them: a later one marks the rows out of date again."""
    from .models import DayRuleTally, FigureState, Visit, VisitRuleTally

    ids = None if visit_ids is None else sorted(set(visit_ids))
    quote = connection.ops.quote_name
    with transaction.atomic(), connection.cursor() as cursor:
        for model, columns, source in _sources(ids):
            table = quote(model._meta.db_table)
            if ids is None:
                cursor.execute(f"DELETE FROM {table}")  # noqa: S608 - a model's table
            elif ids:
                cursor.execute(f"DELETE FROM {table} WHERE visit_id = ANY(%s)", [ids])  # noqa: S608
            if ids != []:
                _insert(cursor, model, columns, source)
        days = None
        if ids:
            days = list(
                Visit.objects.filter(pk__in=ids, visit_date__isnull=False)
                .values_list("visit_date", flat=True)
                .distinct()
            )
        if ids is None or days:
            rows = VisitRuleTally.objects.all()
            table = quote(DayRuleTally._meta.db_table)
            if days is None:
                cursor.execute(f"DELETE FROM {table}")  # noqa: S608
            else:
                cursor.execute(f"DELETE FROM {table} WHERE day = ANY(%s)", [days])  # noqa: S608
                rows = rows.filter(visit__visit_date__in=days)
            _insert(cursor, DayRuleTally, ["day", "rule", "status", *RULE_COLUMNS], day_rule_tallies(rows))
        FigureState.objects.update_or_create(pk=1, defaults={"valid": True, "written_at": timezone.now()})
    remember(None)


def select(qs: QuerySet) -> tuple[str, tuple]:
    """The SQL and parameters of ``qs`` (a ``values_list``), its columns in the order asked for (raises
    ``EmptyResultSet`` for a filter that keeps nothing)."""
    query = qs.query
    if [*query.extra_select, *query.values_select, *query.annotation_select] != list(qs._fields):
        raise ValueError(f"the columns of {qs._fields} are not read in the order asked for")
    sql, params = query.sql_with_params()
    return sql, tuple(params)
