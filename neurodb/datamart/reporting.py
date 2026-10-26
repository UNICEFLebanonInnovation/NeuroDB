"""Partner reporting: the Progress reports page (Reporting Progress, Partner Performance, the report table,
Report/Location and Report/Disaggregation, as the eTools Power BI report shows them) and its exports.

* **What is counted.** Reporting Progress counts progress reports, as Power BI's Reporting Progress does (the
  user's decision of 26 Oct 2026): its tiles, its table by section and its chart by month. An *indicator
  report* is one indicator in one progress report (PRP's ``indicator_report``; the Datamart sends it once per
  location): each tile gives the indicator reports of its progress reports under it. Every indicator report of
  a progress report has the report's status.
* **To follow up.** The reports to follow up (the Partners tab, Home) are those Overdue, Sent back, Due and
  Submitted, a report not yet due among the Due ones, as Power BI's follow-up list has them (the user's
  decision of 26 Oct 2026): :data:`FOLLOW_UP_STATUSES`, :func:`follow_up_status`.
* **Emergency indicator**: the programme document is an SPD (``reached.is_spd``), as Power BI's (the user's
  decision of 26 Oct 2026), never the indicator's high frequency.
* **Statuses.** The six report statuses of :func:`neurodb.datamart.reached.status_of` (Accepted,
  Submitted, Sent back, Due, Overdue, Not yet due): one status per report, the same on every page, so
  the counts add up to the total; Overdue follows ``monitoring.pending_q`` as every page does.
* **Values per location** are read by :mod:`neurodb.datamart.reached` (the disaggregation total "()",
  never a sum of repeated or duplicated rows); nothing here adds values up, except the values of one
  disaggregation into its subtotal (:mod:`neurodb.datamart.disaggregation`: never across
  disaggregations).
* **People.** UNICEF focal points are shown by name only; an entry that holds an e-mail address is
  left out, and partner focal points are never shown.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    Func,
    Max,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Concat, ExtractYear, Lower, NullIf, Trim, Upper

from neurodb.core import compute
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import identity, keys
from . import models as dm
from . import reached as rc

REPORT_FIELDS = (
    "pd_reference_number",
    "intervention_id",
    "partner_id",
    "partner_name",
    "report_number",
    "report_type",
    "report_status",
    "report_accepted_status",
    "period_start",
    "period_end",
    "due_date",
    "submission_date",
    "acceptance_date",
)
STATUS_KEYS = tuple(rc.STATUS_LABELS)
# reports someone has to act on, the most pressing first (Power BI's "Partners require follow up")
FOLLOW_UP = (rc.OVERDUE, rc.SENT_BACK, rc.DUE, rc.SUBMITTED)  # the most pressing first
# the statuses of the reports to follow up: a report not yet due is one of the Due ones there, as Power BI's
# "Partners require follow up" lists it (the user's decision of 26 Oct 2026)
FOLLOW_UP_STATUSES = frozenset({*FOLLOW_UP, rc.NOT_YET_DUE})
# one colour per status in the charts (dark-mode tokens of app.css)
STATUS_COLORS = {
    rc.ACCEPTED: "--nd-cat-6",
    rc.SUBMITTED: "--nd-cat-1",
    rc.SENT_BACK: "--nd-cat-7",
    rc.DUE: "--nd-cat-4",
    rc.OVERDUE: "--nd-cat-8",
    rc.NOT_YET_DUE: "--nd-cat-10",
}
# report rows the Report/Location tab and its export read at once (a web worker holds them all: about
# 8 KB of memory each), and the Report/Disaggregation tab and its export (each row can give many lines)
LOCATION_LIMIT = 20_000
DISAGGREGATION_LIMIT = 10_000
# what the Progress reports page keeps in the shared cache (compute.once): its Report/Location and
# Report/Disaggregation tables two minutes, its filter options ten (they only change with a sync)
STORED_SECONDS = 120
OPTIONS_SECONDS = 600
# 2: the stored tables carry no chart data (by_month); 3: their rows carry the report key; 4: an indicator
# report's type and unit are its placed indicator's (neurodb.datamart.identity); 5: placed from the whole
# report, whatever the filter keeps
CACHE_VERSION = 5
NO_SECTION = "—"
EMAIL = re.compile(r"[^\s@]+@[^\s@]+")

# one indicator in one progress report: PRP's indicator report id, else the eTools id, title and PD output
# (reached.indicator_report_key)
INDICATOR_REPORT = Coalesce(
    NullIf("indicator_report", Value("")),
    Concat(
        Value("t|"),
        "etools_indicator_id",
        Value("|"),
        "indicator",
        Value("|"),
        "pd_output",
        output_field=CharField(),
    ),
    output_field=CharField(),
)
# a report's programme document as reached.report_key names it: NeuroDB's link, else the reference number
REPORT_PD = Case(
    When(intervention__isnull=False, then=Concat(Value("pd"), Cast("intervention_id", CharField()))),
    default=Concat(Value("ref:"), "pd_reference_number"),
    output_field=CharField(),
)
# a progress report as reached.report_key tells it apart: its programme document (NeuroDB's link, else the
# reference number), type, number and period; PRP's progress report id only on a row naming no programme
# document at all, or no number and no period
REPORT_KEY = Concat(
    REPORT_PD,
    Value("|"),
    Upper(Trim("report_type")),
    Value("|"),
    "report_number",
    Value("|"),
    Coalesce(Cast("period_start", CharField()), Value("")),
    Value("|"),
    Coalesce(Cast("period_end", CharField()), Value("")),
    Case(
        When(
            (
                Q(intervention__isnull=True, pd_reference_number="")
                | Q(report_number="", period_start=None, period_end=None)
            )
            & ~Q(progress_report=""),
            then=Concat(Value("|pr:"), "progress_report"),
        ),
        default=Value(""),
        output_field=CharField(),
    ),
    output_field=CharField(),
)
# one indicator report among all: its progress report and the indicator in it, as progress_reports counts them
INDICATOR_REPORT_KEY = Concat(REPORT_KEY, Value("#"), INDICATOR_REPORT, output_field=CharField())
# a progress report by its programme document, type and number, whatever its period; and its period
REPORT_NUMBER = Concat(
    REPORT_PD, Value("|"), Upper(Trim("report_type")), Value("|"), "report_number", output_field=CharField()
)
REPORT_PERIOD = Concat(
    Coalesce(Cast("period_start", CharField()), Value("")),
    Value("|"),
    Coalesce(Cast("period_end", CharField()), Value("")),
    output_field=CharField(),
)
# report rows that name no programme document at all (no link, no PD reference number, no eTools PD id):
# PRP reports outside programme documents, which no PD status filter keeps: Progress reports, the partner
# page, the home page and the management brief never count them
NO_PD = Q(intervention__isnull=True, pd_reference_number="") & (
    Q(data__etools_intervention_id__isnull=True)
    | Q(data__etools_intervention_id=None)
    | Q(data__etools_intervention_id="")
)


def sent_for_another_period(rows: QuerySet) -> QuerySet:
    """The rows of ``rows`` eTools no longer sends (``not_sent_since``, kept and counted) whose programme
    document, type and report number a row still sent holds under another period (start or end): mostly
    reports PRP made again with new dates after a PD amendment, the first to review before removing them
    (Raw eTools data, ``datamart_sync.keep_unsent``). One ``Exists``; a row with no report number is never
    one."""
    sent = (
        dm.ReportedIndicator.objects.filter(not_sent_since=None)
        .exclude(report_number="")
        .annotate(_number=REPORT_NUMBER, _period=REPORT_PERIOD)
        .filter(_number=OuterRef("_number"))
        .exclude(_period=OuterRef("_period"))
    )
    return (
        rows.exclude(not_sent_since=None)
        .exclude(report_number="")
        .annotate(_number=REPORT_NUMBER, _period=REPORT_PERIOD)
        .filter(Exists(sent))
    )


def _base_sql(column: str) -> Func:
    """``reached.pd_base`` in the database: the number without its spaces, in lower case, its amendment suffix
    ("-2") dropped."""
    bare = Func(
        F(column), Value(r"\s"), Value(""), Value("g"), function="REGEXP_REPLACE", output_field=CharField()
    )
    return Func(Lower(bare), Value(r"-\d+$"), Value(""), function="REGEXP_REPLACE", output_field=CharField())


def report_numbers(pd_numbers: Iterable[str] | None = None, *fields: str) -> Iterator[tuple]:
    """The progress reports the partner report rows hold, by number, in one query over every row (never a
    filtered queryset: a section, a search or the emergency filter leaves out rows of a report the rows still
    hold; a row eTools no longer sends still holds its report), limited to the rows of ``pd_numbers`` (by
    ``reached.pd_base``) when given: for each distinct row, ``(bases, TYPE, number key, *fields)``. ``bases``:
    the base of the row's own reference number and of its linked programme document's number (both, when they
    differ: a row the eTools id linked to another amendment, or to another PD, still holds its report); TYPE
    upper case; the number key ``reached.report_number_key`` ("3" of a QPR is "QPR3"). A row with no number
    holds none."""
    rows = dm.ReportedIndicator.objects.order_by()
    if pd_numbers is not None:
        bases = sorted({rc.pd_base(n) for n in pd_numbers} - {""})
        if not bases:
            return
        rows = rows.annotate(_own=_base_sql("pd_reference_number"), _linked=_base_sql("intervention__number"))
        rows = rows.filter(Q(_own__in=bases) | Q(_linked__in=bases))
    found = rows.values_list(
        "pd_reference_number", "intervention__number", "report_type", "report_number", *fields
    ).distinct()
    for own, linked, kind, number, *rest in found.iterator(chunk_size=2000):
        key = rc.report_number_key(number, kind)
        bases = {rc.pd_base(own), rc.pd_base(linked)} - {""}
        if key and bases:
            yield (bases, (kind or "").strip().upper(), key, *rest)


def held_report_numbers(pd_numbers: Iterable[str] | None = None) -> set[tuple[str, str, str]]:
    """The progress reports the partner report rows hold (:func:`report_numbers`), as ``(base of a PD number,
    TYPE, number key)``; a row with no type gives ``(base, "", key)``, which holds the number under any type
    (:func:`holds`). The one rule that says whether the rows hold a report of PRP's own progress reports (the
    proof of their link, ``datamart_sync.PartnerNumbers``, reads the same rows by the same rule)."""
    return {(base, kind, key) for bases, kind, key in report_numbers(pd_numbers) for base in bases}


def holds(found: Any, base: str, kind: str, key: str) -> bool:
    """Whether ``found`` (:func:`held_report_numbers`, or a dict keyed alike) holds the report ``key`` of type
    ``kind`` of the PD whose base number is ``base``: under its type, or under a row with no type."""
    return (base, kind, key) in found or (base, "", key) in found


def _date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _getlist(params, key: str) -> list[str]:
    values = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values if str(v or "").strip()]


def status_key(text: str) -> str:
    """A status as the filter carries it: one of the six keys, or PRP's word for it ("Sent back")."""
    word = " ".join(str(text or "").lower().replace("_", " ").split())
    key = word.replace(" ", "_")
    if key in rc.STATUS_LABELS:
        return key
    return {"acc": rc.ACCEPTED, "sub": rc.SUBMITTED, "sen": rc.SENT_BACK, "ove": rc.OVERDUE}.get(word, "")


# ------------------------------------------------------------------------------------------ filters
@dataclass
class ReportFilters:
    q: str = ""
    year: int | None = None  # the year the reporting period ends in
    statuses: list[str] = field(default_factory=list)  # six status keys
    report_types: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    report_numbers: list[str] = field(default_factory=list)
    # programme documents by number, exactly: the report's own reference or its PD's number (Home's rows)
    pds: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=list)  # partner ids as text
    partner_types: list[str] = field(default_factory=list)  # partner type or CSO type
    # eTools PD statuses chosen: None (no choice made) the running PDs, as on Partner monitoring and in
    # Power BI; an empty list every status. Draft and cancelled PDs are never read
    pd_statuses: list[str] | None = None
    emergency: str = ""  # yes: the reports of SPDs (the Emergency indicator, as Power BI's), no: the others
    start_from: datetime.date | None = None  # period start between
    start_to: datetime.date | None = None
    end_from: datetime.date | None = None  # period end between
    end_to: datetime.date | None = None
    overdue: bool = False  # only reports not submitted by their due date

    @classmethod
    def from_params(cls, params) -> ReportFilters:
        from .monitoring import year_param

        return cls(
            q=(params.get("q") or "").strip(),
            year=year_param(params.get("year")),
            statuses=[k for k in dict.fromkeys(status_key(x) for x in _getlist(params, "status")) if k],
            report_types=[x.upper() for x in _getlist(params, "report_type")],
            sections=_getlist(params, "section"),
            report_numbers=_getlist(params, "report_number"),
            pds=_getlist(params, "pd"),
            partners=[x for x in _getlist(params, "partner") if re.fullmatch(r"[0-9]{1,18}", x)],
            partner_types=_getlist(params, "partner_type"),
            # "pd_status=" alone (nothing chosen) is every status; no key at all, the running PDs
            pd_statuses=_getlist(params, "pd_status") if "pd_status" in params else None,
            emergency=emergency
            if (emergency := str(params.get("emergency") or "").strip().lower()) in ("yes", "no")
            else "",
            start_from=_date(params.get("start_from")),
            start_to=_date(params.get("start_to")),
            end_from=_date(params.get("end_from")),
            end_to=_date(params.get("end_to")),
            overdue=params.get("overdue") == "1",
        )

    @property
    def status_choice(self) -> list[str]:
        """The PD statuses the filter shows as chosen (none: every status)."""
        return list(pd_statuses.RUNNING) if self.pd_statuses is None else list(self.pd_statuses)

    def cache_key(self) -> str:
        """Every field of the filter, as JSON, for the keys of what is stored for it (see
        ``monitoring.Filters.cache_key``): each list in one order, the dates as written, and the PD statuses
        as :meth:`queryset` reads them (:attr:`status_choice`): no ``pd_status`` in the address (the page's
        own links) and the running statuses the filter bar sends for the same choice share a key, while
        ``pd_status=`` alone (every status) keeps its own."""
        return _json_key(dataclasses.replace(self, pd_statuses=sorted(set(self.status_choice))))

    def queryset(
        self,
        today: datetime.date,
        *,
        by_pd_status: bool = True,
        steps: list[tuple[str, QuerySet[dm.ReportedIndicator]]] | None = None,
    ) -> QuerySet[dm.ReportedIndicator]:
        """The report rows (one per indicator, report and location) every filter but the status keeps
        (a report's status is read from all its rows, in :func:`progress_reports`); with
        ``by_pd_status=False``, also those the PD status filter leaves out (:func:`left_out`). The PD status
        filter comes last, so :func:`left_out` reads the rows every other filter keeps.

        ``steps``: the figure trail's list (:mod:`neurodb.datamart.trail`), which receives ``("start", every
        row)`` and then ``(the filter's name, the rows it keeps)`` after each filter that applies, in their
        order; None on the pages. Every filter is ANDed: the rows and the SQL are the same either way."""
        from .monitoring import pending_q

        def kept(name: str, rows: QuerySet[dm.ReportedIndicator]) -> QuerySet[dm.ReportedIndicator]:
            if steps is not None:
                steps.append((name, rows))
            return rows

        rows = kept("start", dm.ReportedIndicator.objects.all())
        if self.report_types:
            rows = kept("report_type", rows.filter(report_type__in=self.report_types))
        if self.year:
            rows = kept("year", rows.filter(period_end__year=self.year))
        if self.q:
            rows = kept(
                "q",
                rows.filter(
                    Q(partner_name__icontains=self.q)
                    | Q(vendor_number__icontains=self.q)
                    | Q(pd_reference_number__icontains=self.q)
                    | Q(indicator__icontains=self.q)
                    | Q(partner__name__icontains=self.q)
                    | Q(partner__short_name__icontains=self.q)
                    | Q(partner__vendor_number__icontains=self.q)
                ),
            )
        if self.sections:
            rows = kept("section", rows.filter(section_q(self.sections)))
        if self.report_numbers:
            rows = kept("report_number", rows.filter(report_number__in=self.report_numbers))
        if self.pds:
            # the report's own reference, or the number of the PD the sync linked it to (by its eTools id
            # first: the reference can differ from that number, or be blank)
            rows = kept(
                "pds", rows.filter(Q(pd_reference_number__in=self.pds) | Q(intervention__number__in=self.pds))
            )
        if self.partners:
            rows = kept("partner", rows.filter(partner_id__in=[int(p) for p in self.partners]))
        if self.partner_types:
            rows = kept(
                "partner_type",
                rows.filter(
                    Q(partner__partner_type__in=self.partner_types)
                    | Q(partner__cso_type__in=self.partner_types)
                ),
            )
        # never draft or cancelled PDs (not, or no longer, a partnership)
        rows = kept("not_a_partnership", rows.exclude(intervention__status__in=pd_statuses.NOT_A_PARTNERSHIP))
        if self.emergency:  # the programme document is an SPD (reached.is_spd), as Power BI's
            spd = rc.spd_q("intervention__")
            rows = kept("emergency", rows.filter(spd) if self.emergency == "yes" else rows.exclude(spd))
        for name, lookup, value in (
            ("start_from", "period_start__gte", self.start_from),
            ("start_to", "period_start__lte", self.start_to),
            ("end_from", "period_end__gte", self.end_from),
            ("end_to", "period_end__lte", self.end_to),
        ):
            if value is not None:
                rows = kept(name, rows.filter(**{lookup: value}))
        if self.overdue:
            rows = kept("overdue", rows.filter(pending_q(), due_date__lt=today))
        # a report of a PD NeuroDB does not hold is read only when every status is chosen, and a report that
        # names no PD never
        if by_pd_status and self.status_choice:
            rows = kept("pd_status", rows.filter(intervention__status__in=self.status_choice))
        elif by_pd_status:
            rows = kept("pd_status", rows.exclude(NO_PD))
        return rows


# Why the PD status filter leaves an indicator report out (:func:`left_out`), given the rows every other
# filter keeps and a PD status not chosen: Raw eTools data's "Programme document link" filter reads the same
LEFT_OUT_REASONS: dict[str, Q] = {
    "other_status": Q(intervention__isnull=False),
    "not_held": Q(intervention__isnull=True) & ~NO_PD,
    "no_pd": NO_PD,
}


def left_out(filters: ReportFilters, today: datetime.date) -> dict[str, int]:
    """The indicator reports every other filter keeps and the page does not count, of any report status
    (a report's status is not read here), by why: ``other_status``, of a programme document whose status
    the PD status filter leaves out; ``not_held``, of a programme document NeuroDB does not hold (read
    only when every PD status is chosen); ``no_pd``, naming no programme document (never read). One
    query, over the left-out rows only."""
    chosen = filters.status_choice
    rows = filters.queryset(today, by_pd_status=False)
    if chosen:
        rows = rows.filter(Q(intervention__isnull=True) | ~Q(intervention__status__in=chosen))
    else:
        rows = rows.filter(NO_PD)
    key = INDICATOR_REPORT_KEY
    found = rows.aggregate(
        **{name: Count(key, distinct=True, filter=reason) for name, reason in LEFT_OUT_REASONS.items()}
    )
    out = {name: int(found[name] or 0) for name in ("other_status", "not_held", "no_pd")}
    out["by_pd_status"] = out["other_status"] + out["not_held"]
    return out


@dataclass(frozen=True)
class Units:
    """Report rows, and the indicator reports and progress reports they make (:func:`units`)."""

    rows: int = 0
    indicator_reports: int = 0
    progress_reports: int = 0


# what tells a row's progress report and indicator report apart, as progress_reports groups them
UNIT_FIELDS = ("pk", "datamart_id", "progress_report", *rc.IDENTITY_FIELDS)


def unit_rows(queryset: QuerySet[dm.ReportedIndicator]):
    """One dict per report row: :data:`UNIT_FIELDS` and its indicator report (``_ir``,
    :data:`INDICATOR_REPORT`), what :func:`unit_key` reads. No payload."""
    return queryset.annotate(_ir=INDICATOR_REPORT).values(*UNIT_FIELDS, "_ir").order_by()


def unit_key(row: dict[str, Any]) -> tuple[str, str]:
    """A row's progress report and indicator report, by the keys :func:`progress_reports` groups with."""
    return report_key(row), row["_ir"]


def units(queryset: QuerySet[dm.ReportedIndicator]) -> Units:
    """The rows of ``queryset``, and the indicator reports and progress reports they make, by the keys
    :func:`progress_reports` groups with: ``units(rows).indicator_reports`` is the sum of the reports'
    ``indicators``, whatever their status."""
    keys = [unit_key(row) for row in unit_rows(queryset)]
    return Units(len(keys), len(set(keys)), len({k for k, _ in keys}))


def report_count(queryset: QuerySet[dm.ReportedIndicator]) -> int:
    """The progress reports of these rows (``reached.report_key``, :data:`REPORT_KEY` in the database),
    counted by the database."""
    return queryset.annotate(_report=REPORT_KEY).values("_report").distinct().count()


# --------------------------------------------------------------------------------- progress reports
def submitted_late(report: dict[str, Any]) -> bool:
    """A progress report submitted after its due date, whatever its status (the page's "submitted after their
    due date")."""
    return bool(
        report["submission_date"] and report["due_date"] and report["submission_date"] > report["due_date"]
    )


def report_status(report: dict[str, Any], today: datetime.date) -> str:
    """One of the six report statuses (``reached.status_of``) of a progress report row."""
    return rc.status_of(
        report.get("report_status"),
        period_end=report.get("period_end"),
        due_date=report.get("due_date"),
        submission_date=report.get("submission_date"),
        today=today,
    )


def report_key(row: dict[str, Any]) -> str:
    """The progress report of a grouped row (:func:`neurodb.datamart.reached.report_key`: its programme
    document, type, number and period), whatever its status, partner link or dates say."""
    return rc.report_key(row)


def progress_reports(
    queryset: QuerySet[dm.ReportedIndicator], today: datetime.date | None = None, *, record: Any = None
) -> list[dict[str, Any]]:
    """One row per progress report (the Datamart has one row per indicator and location), with its
    status, its indicator reports (``indicators``) and their sections (``sections``: section ->
    indicator reports; ``indicator_sections``: each indicator report and the section it counts under).

    A report is its programme document, type, number and period (:func:`report_key`), whatever PRP's ids
    hold. A report whose rows disagree on a field (a partial re-sync, a partner link set on some rows
    only) is still listed and counted once: its status is the word of its rows changed last
    (:func:`neurodb.datamart.reached.newest_status`, as every page reads it), read from every row of
    the report, also those a filter leaves out (a section, a search, the emergency indicators: Partner
    monitoring and the pop-up read the whole report); every other field is the first non-empty value
    of its rows, in a fixed order. ``key``: the report's key, which opens its pop-up. ``record``: the figure
    trail's (:func:`_status_words`); None on the pages."""
    today = today or datetime.date.today()
    grouped = (
        queryset.annotate(_ir=INDICATOR_REPORT)
        .values("progress_report", *REPORT_FIELDS, "section", "_ir")
        .annotate(
            locations=Count("pk"),
            _changed=Max("last_modify_date"),
            _synced=Max("synced_at"),
            _id=Max("datamart_id"),
        )
        .order_by("progress_report", *REPORT_FIELDS, "section", "_ir")
    )
    reports: dict[Any, dict[str, Any]] = {}
    sections: dict[Any, dict[str, str]] = defaultdict(dict)  # report -> indicator report -> section
    words: dict[Any, dict[str, rc.Stamp]] = defaultdict(dict)  # report -> status word -> latest stamp
    for row in grouped:
        key = report_key(row)
        report = reports.get(key)
        if report is None:
            report = reports[key] = {f: row[f] for f in ("progress_report", *REPORT_FIELDS)}
            report["_group"] = key  # filling empty fields below must not change the report's key
        else:
            for f in ("progress_report", *REPORT_FIELDS):
                if report[f] in (None, "") and row[f] not in (None, ""):
                    report[f] = row[f]
        text = row["report_status"] or ""
        words[key][text] = rc.stamp(words[key].get(text), (row["_changed"], row["_synced"], row["_id"]))
        section = (row["section"] or "").strip()
        known = sections[key].get(row["_ir"])
        if known is None or (not known and section):
            sections[key][row["_ir"]] = section
    words.update(_status_words(queryset, set(reports), record))
    out = sorted(reports.values(), key=lambda r: (r["pd_reference_number"], r["report_number"]))
    # latest period first, reports without a period end at the top (as the database sorts them)
    out.sort(key=lambda r: r["period_end"] or datetime.date.max, reverse=True)
    for report in out:  # one of the six statuses, the same on every page that lists the report
        key = report.pop("_group")
        report["report_status"] = rc.newest_status(words[key])
        report["indicator_sections"] = {ir: s or NO_SECTION for ir, s in sections[key].items()}
        by_section = Counter(report["indicator_sections"].values())
        report["indicators"] = sum(by_section.values())
        report["sections"] = dict(by_section)
        report["section"] = ", ".join(s for s in by_section if s != NO_SECTION)
        report["status"] = report_status(report, today)
        report["status_label"] = rc.STATUS_LABELS[report["status"]]
        report["key"] = key
    return out


def _status_words(
    queryset: QuerySet[dm.ReportedIndicator], keys: set[str], record: Any = None
) -> dict[str, dict[str, rc.Stamp]]:
    """Each status word of every row of the progress reports ``keys`` of ``queryset`` (all their rows, not
    only the filter's: the rows of the same programme documents and period ends) with the latest stamp of
    its rows (:func:`neurodb.datamart.reached.newest_status`). ``record``: the figure trail's, which receives
    what was read (``record.status_words``) and the same rows one by one (``record.status_rows``: which row
    gave each report its word); None on the pages."""
    held = queryset.exclude(intervention=None)
    named = queryset.filter(intervention=None)
    rows = dm.ReportedIndicator.objects.filter(
        Q(intervention_id__in=held.values("intervention_id"))
        | Q(intervention=None, pd_reference_number__in=named.values("pd_reference_number")),
        Q(period_end__in=queryset.values("period_end")) | Q(period_end=None),
    )
    every = (
        rows.values(*rc.IDENTITY_FIELDS, "progress_report", "report_status")
        .annotate(_changed=Max("last_modify_date"), _synced=Max("synced_at"), _id=Max("datamart_id"))
        .order_by()
    )
    out: dict[str, dict[str, rc.Stamp]] = defaultdict(dict)
    for row in every:
        key = rc.report_key(row)
        if key not in keys:
            continue
        mine, text = out[key], row["report_status"] or ""
        mine[text] = rc.stamp(mine.get(text), (row["_changed"], row["_synced"], row["_id"]))
    if record is not None:
        record.status_words(out)
        stamps = ("last_modify_date", "synced_at", "datamart_id")  # reached.row_stamp
        record.status_rows(
            rows.values(*rc.IDENTITY_FIELDS, "progress_report", "report_status", *stamps).order_by(), keys
        )
    return out


def focal_point_names(values: Any) -> list[str]:
    """UNICEF focal points by name: an entry that is (or holds) an e-mail address is never shown, nor
    one that is a bare user id or a record's text (as an older eTools REST sync could store them)."""
    names = []
    for value in values or []:
        text = " ".join(str(value or "").split())
        if text.startswith("{"):
            try:
                record = json.loads(text)
            except ValueError:
                record = None
            record = record if isinstance(record, dict) else {}
            full = " ".join(str(record.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
            text = " ".join((full or str(record.get("name") or "")).split())
        if text and not text.isdigit() and not EMAIL.search(text) and text not in names:
            names.append(text)
    return names


def pd_details(pd_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Number, status, document type, sections and UNICEF focal points (names only) of these PDs."""
    out: dict[int, dict[str, Any]] = {}
    ids = sorted(i for i in pd_ids if i)
    for start in range(0, len(ids), 5000):
        for pk, number, status, kind, names, focal in PCA.objects.filter(
            pk__in=ids[start : start + 5000]
        ).values_list("pk", "number", "status", "document_type", "section_names", "unicef_focal_points"):
            out[pk] = {
                "number": number or "",
                "status": status or "",
                "status_label": pd_statuses.label(status),
                "document_type": kind or "",
                "sections": [s for s in names or [] if s],
                "focal_points": focal_point_names(focal),
            }
    return out


def with_pd_details(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reports with their PD's number (``pd_number``), status, document type and UNICEF focal points
    (names). An indicator report whose rows name no section is in its PD's sections (joined, one entry:
    never counted twice), in the table and in the counts by section alike."""
    details = pd_details({r["intervention_id"] for r in reports if r["intervention_id"]})
    for report in reports:
        pd = details.get(report["intervention_id"]) or {}
        report["pd_number"] = pd.get("number", "")
        report["pd_status"] = pd.get("status", "")
        report["pd_status_label"] = pd.get("status_label", "") if pd else ""
        report["document_type"] = pd.get("document_type", "")
        report["focal_points"] = pd.get("focal_points", [])
        if pd.get("sections") and NO_SECTION in report["sections"]:
            joined = ", ".join(pd["sections"])
            sections = dict(report["sections"])
            sections[joined] = sections.get(joined, 0) + sections.pop(NO_SECTION)
            report["sections"] = sections
            report["section"] = ", ".join(sections)
            report["indicator_sections"] = {
                ir: joined if s == NO_SECTION else s for ir, s in report["indicator_sections"].items()
            }
    return reports


def section_q(names: list[str]) -> Q:
    """Report rows in these sections: the row's own section (alone or in a list such as "C4D,
    Education" or "C4D; Education", as :func:`section_names` reads it), else, for a row that names none,
    its PD's sections: the rule of the counts by section."""
    found = Q()
    for name in names:
        found |= (
            Q(section__iexact=name)
            | Q(section__iregex=r"(^|[,;])\s*" + re.escape(name) + r"\s*([,;]|$)")
            | (Q(section="") & Q(intervention__section_names__contains=[name]))
        )
    return found


def _spaced(text: Any) -> str:
    """A section name with its spaces as one (a no-break space or a tab too), and one space after a comma
    or a semicolon, none before."""
    return re.sub(r"\s*([,;])\s*", r"\1 ", " ".join(str(text or "").split())).strip()


def section_names(text: Any, known: Any = ()) -> list[str]:
    """The section names in a partner report row's section field, which holds one name or a list ("Child
    Protection, Adolescent & Youth"): split at commas and semicolons, the spaces inside a name as one, each
    name once whatever its case (its first spelling kept). ``known``: section names as eTools spells them
    (the PDs' sections); one of them that holds a comma itself ("WASH / Water, Sanitation and Hygiene") is
    kept whole. Check 10 of the eTools reporting checks and the Section filter of Progress reports read
    the field this way, and :func:`section_q` finds a name inside such a list."""
    whole = {_spaced(k).casefold() for k in known or () if _spaced(k)}
    pieces = re.split(r"([,;])", str(text or ""))
    words, separators = pieces[0::2], pieces[1::2]

    def joined(first: int, last: int) -> str:
        return _spaced(
            "".join(words[k] + (separators[k] if k < last else "") for k in range(first, last + 1))
        )

    out: dict[str, str] = {}
    i = 0
    while i < len(words):
        # the longest run of pieces from here that a known name spells whole, else this piece alone
        last = next((j for j in range(len(words) - 1, i, -1) if joined(i, j).casefold() in whole), i)
        name = joined(i, last)
        if name:
            out.setdefault(name.casefold(), name)
        i = last + 1
    return list(out.values())


# ------------------------------------------------------------------------------- Reporting Progress
def counts(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Progress reports per status (what the tiles show, as Power BI's Reporting Progress counts them: the
    status keys) and the indicator reports in them (each tile's hint), each adding up to its total, and the
    progress reports submitted after their due date."""
    indicator_reports: Counter[str] = Counter()
    progress: Counter[str] = Counter()
    for r in reports:
        indicator_reports[r["status"]] += r["indicators"]
        progress[r["status"]] += 1
    return {
        "indicator_reports": sum(indicator_reports.values()),
        "progress_reports": len(reports),
        "statuses": [
            {
                "key": key,
                "label": rc.STATUS_LABELS[key],
                "indicator_reports": indicator_reports.get(key, 0),
                "progress_reports": progress.get(key, 0),
            }
            for key in STATUS_KEYS
        ],
        **{key: progress.get(key, 0) for key in STATUS_KEYS},
        "late": sum(1 for r in reports if submitted_late(r)),
    }


def report_section(report: dict[str, Any]) -> str:
    """The section a progress report is counted under by :func:`by_section`: its Section as the reports' table
    shows it (its indicator reports' own sections, joined; for those that name none, their PD's sections), or
    :data:`NO_SECTION`."""
    return report["section"] or NO_SECTION


def by_section(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Progress reports by section and status, with a total row and column (Power BI's "Reporting Progress
    by Sector", which counts progress reports): each report once, under its Section (:func:`report_section`),
    so the rows add up to the total and to the tiles."""
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for r in reports:
        table[report_section(r)][r["status"]] += 1
    rows = [
        {"section": section, "counts": [c.get(k, 0) for k in STATUS_KEYS], "total": sum(c.values())}
        for section, c in sorted(table.items(), key=lambda kv: (kv[0] == NO_SECTION, kv[0].lower()))
    ]
    totals = [sum(row["counts"][i] for row in rows) for i in range(len(STATUS_KEYS))]
    return {"rows": rows, "totals": totals, "total": sum(totals)}


def report_month(report: dict[str, Any]) -> tuple[int, int] | None:
    """The month a progress report is about (:func:`by_month`): the year and month its reporting period ends
    in; None without a period end."""
    end = report["period_end"]
    return (end.year, end.month) if end else None


def by_month(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Progress reports by month of the end of their reporting period and status, Not yet due included (a
    chart), as Power BI's "# of Reports by Type over Reporting Date" counts them. The month a report is about,
    as Power BI places it (a report still Due on the day of the refresh sits in the month its period ended,
    before its due date), and as the Reporting year filter and Partner monitoring's months read it."""
    months: dict[tuple[int, int] | None, Counter[str]] = defaultdict(Counter)
    for r in reports:
        months[report_month(r)][r["status"]] += 1
    order = sorted(m for m in months if m is not None) + ([None] if None in months else [])
    labels = [datetime.date(m[0], m[1], 1).strftime("%b %Y") if m else "No period end" for m in order]
    present = [k for k in STATUS_KEYS if any(months[m].get(k) for m in order)]
    return {
        "labels": labels,
        "series": {rc.STATUS_LABELS[k]: [months[m].get(k, 0) for m in order] for k in present},
        "colors": {rc.STATUS_LABELS[k]: STATUS_COLORS[k] for k in present},
    }


# ------------------------------------------------------------------------------ Partner Performance
def partner_key(report: dict[str, Any]) -> Any:
    """A progress report's partner, as the Partners tab counts them: its eTools partner, else its name (any
    case); None when it names neither."""
    return report["partner_id"] or (report["partner_name"] or "").strip().lower() or None


def follow_up_status(status: str) -> str:
    """A report's status in the lists of reports to follow up: a report not yet due is Due there, as Power
    BI's follow-up list calls it (the user's decision of 26 Oct 2026); any other keeps its own."""
    return rc.DUE if status == rc.NOT_YET_DUE else status


def follow_up_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reports to follow up (:data:`FOLLOW_UP_STATUSES`), the most pressing first: Overdue, Sent back,
    Due (a report not yet due among them) and Submitted, each by due date. A report not yet due is listed as
    Due (:func:`follow_up_status`: a copy of its row, its status Due), so the table, its export and the
    trail's CSV say Due as Power BI's list does; the reports' own list keeps Not yet due."""
    severity = {key: n for n, key in enumerate(FOLLOW_UP)}
    out = []
    for r in reports:
        if r["status"] not in FOLLOW_UP_STATUSES:
            continue
        status = follow_up_status(r["status"])
        if status != r["status"]:
            r = {**r, "status": status, "status_label": rc.STATUS_LABELS[status]}
        out.append(r)
    out.sort(
        key=lambda r: (severity[r["status"]], r["due_date"] or datetime.date.max, r["pd_reference_number"])
    )
    return out


def partner_performance(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Partners with reports in the filter (every status), those with at least one Submitted or
    Accepted report and those without, the number of partners with a report in each status, and the
    reports to follow up (:func:`follow_up_reports`: Overdue, Sent back, Due, a report not yet due among
    them, and Submitted), the most pressing first."""
    names: dict[Any, str] = {}
    ids: dict[Any, int | None] = {}
    by_status: dict[str, set[Any]] = defaultdict(set)
    reports_of: dict[Any, Counter[str]] = defaultdict(Counter)
    pds_of: dict[Any, set[str]] = defaultdict(set)
    for r in reports:
        key = partner_key(r)
        if key is None:
            continue
        names.setdefault(key, r["partner_name"] or "")
        ids.setdefault(key, r["partner_id"])
        by_status[r["status"]].add(key)
        reports_of[key][r["status"]] += 1
        if r["pd_reference_number"]:
            pds_of[key].add(r["pd_reference_number"])
    reported = set().union(*(by_status[k] for k in rc.COUNTED))
    not_reported = sorted(set(names) - reported, key=lambda k: names[k].lower())
    follow_up = follow_up_reports(reports)
    return {
        "partners": len(names),
        "reported": len(reported),
        "not_reported": len(not_reported),
        "by_status": [
            {"key": key, "label": rc.STATUS_LABELS[key], "partners": len(by_status.get(key, ()))}
            for key in STATUS_KEYS
        ],
        "not_reported_list": [
            {
                "key": k,
                "name": names[k] or "—",
                "id": ids[k],
                "pds": sorted(pds_of[k]),
                "statuses": {rc.STATUS_LABELS[s]: n for s, n in sorted(reports_of[k].items())},
            }
            for k in not_reported
        ],
        "follow_up": follow_up,
    }


def pd_follow_up_key(report: dict[str, Any]) -> str:
    """The programme document a progress report is followed up under on Home: NeuroDB's link to it
    (``pd:<id>``), else the report's own reference number (``ref:<reference>``)."""
    held = report["intervention_id"]
    return f"pd:{held}" if held else f"ref:{report['pd_reference_number'] or ''}"


def pd_follow_up(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Home's "Programme documents to follow up": one row per programme document of ``reports`` (those of
    :func:`neurodb.datamart.services.partner_reporting`, with their PD's details), by NeuroDB's link to it,
    else by the reports' own reference number. Each row: ``pd_reference_number`` (the PD's number when
    NeuroDB holds it; else the reports' reference; "" for neither), ``pd_number`` (the PD's number alone, as
    Partner monitoring's ``pd`` reads it: "" for a PD NeuroDB does not hold), ``intervention_id``,
    ``partner_id``, ``partner_name``, ``sections`` (its reports' sections, joined), ``counts`` (its progress
    reports per status as the follow-up lists name it, :func:`follow_up_status`: a report not yet due is
    counted under Due, as Power BI's follow-up list has it; Not yet due is then 0), ``follow_up`` (Overdue,
    Sent back, Due and Submitted) and ``oldest_due`` (the oldest due date of its Due or Overdue reports: a
    past one as soon as one is Overdue) and ``key`` (:func:`pd_follow_up_key`).
    Only the programme documents with a report to follow up, the most pressing first: the most Overdue, then
    Sent back, Due and Submitted, then the oldest due date. Counts are progress reports, as the follow-up
    table of the Partners tab lists them."""
    found: dict[str, dict[str, Any]] = {}
    for r in reports:
        held = r["intervention_id"]
        key = pd_follow_up_key(r)
        row = found.get(key)
        if row is None:
            row = found[key] = {
                "key": key,
                "pd_reference_number": "",
                "pd_number": (r.get("pd_number") or "") if held else "",
                "intervention_id": r["intervention_id"],
                "partner_id": None,
                "partner_name": "",
                "_sections": [],
                "counts": dict.fromkeys(STATUS_KEYS, 0),
                "oldest_due": None,
            }
        reference = (r.get("pd_number") if r["intervention_id"] else "") or r["pd_reference_number"] or ""
        if not row["pd_reference_number"] and reference:
            row["pd_reference_number"] = reference
        if row["partner_id"] is None and r["partner_id"]:
            row["partner_id"] = r["partner_id"]
        if not row["partner_name"] and r["partner_name"]:
            row["partner_name"] = r["partner_name"]
        for name in r["sections"]:  # as the page's Section column names them
            if name != NO_SECTION and name not in row["_sections"]:
                row["_sections"].append(name)
        status = follow_up_status(r["status"])
        row["counts"][status] += 1
        if status in (rc.DUE, rc.OVERDUE) and r["due_date"]:
            if row["oldest_due"] is None or r["due_date"] < row["oldest_due"]:
                row["oldest_due"] = r["due_date"]
    rows = []
    for row in found.values():
        row["sections"] = ", ".join(row.pop("_sections"))
        row["follow_up"] = sum(row["counts"][key] for key in FOLLOW_UP)
        if row["follow_up"]:
            rows.append(row)
    rows.sort(
        key=lambda row: (
            *(-row["counts"][key] for key in FOLLOW_UP),
            row["oldest_due"] or datetime.date.max,
            row["pd_reference_number"],
        )
    )
    return {"rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------------- Report/Location
LOCATION_FIELDS = (*rc.ROW_FIELDS, "partner_id", "partner_name", "section", "cp_output")


def _location_cells(
    rows: list[dict[str, Any]], statuses: dict[Any, str], today: datetime.date, wanted, record: Any = None
):
    """Each indicator report's locations, read as :mod:`reached` reads them (one value per location:
    the disaggregation total "()", never a sum of repeated or duplicated rows): ``(fields, cell)`` per
    location, ``fields`` being what the Report/Location and Report/Disaggregation tabs show of the
    report. A report keeps its progress report's own status (``statuses``), also under a status filter
    (``wanted``: the statuses to keep, None: all). Each indicator report reads the type and unit of the PD
    indicator its rows belong to (:func:`identity.definitions`).

    ``record``: the figure trail's (:mod:`neurodb.datamart.trail`), which receives each indicator report the
    status filter leaves out (``record.dropped``) and each location (``record.line``: the report, the
    indicator report, the place, the Datamart ids of its rows and the one read), whose ``fields`` then also
    carry them (``indicator_report_key``, ``place``, ``record_ids``, ``chosen_record_id``); None on the
    pages."""
    definition = identity.definitions(rows)
    details = pd_details({r["intervention_id"] for r in rows if r["intervention_id"]})
    # one group per indicator report (one indicator in one progress report, as the tiles count them):
    # two indicators that share an eTools id or a title are never read as one, nor two programme documents'
    # reports of one number and period
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(rc.report_key(row), rc.indicator_report_key(row))].append(row)
    for (key, ir), group in groups.items():
        first = group[0]
        status = statuses.get(key)
        if status is None:
            status = rc.status_of(
                first["report_status"],
                period_end=first["period_end"],
                due_date=first["due_date"],
                submission_date=first["submission_date"],
                today=today,
            )
        if wanted is not None and status not in wanted:
            if record is not None:
                record.dropped("status", key, ir, len(group))
            continue
        pd_id = first["intervention_id"]
        display_type, unit, _ = definition(group) or ("", "", None)
        ratio = rc.is_ratio(
            display_type, unit, first["calculation_across_locations"], first["calculation_across_periods"]
        )
        percentage = display_type == "percentage" or unit == "percentage"
        (report,) = rc.build_reports(group, ratio=ratio, percentage=percentage, today=today)
        pd = details.get(pd_id) or {}
        for place, cell in report.cells.items():
            meta = report.places.get(place, {})
            fields = {
                "programme_document": first["pd_reference_number"],
                "pd_id": pd_id,
                "pd_status": pd.get("status_label", ""),
                "partner": first["partner_name"],
                "partner_id": first["partner_id"],
                "section": (first["section"] or "").strip() or ", ".join(pd.get("sections", [])),
                "cp_output": first["cp_output"],
                "indicator": first["indicator"],
                "emergency": "Yes" if rc.is_spd(pd.get("document_type")) else "No",  # the PD is an SPD
                "report": report.number,
                "report_type": report.report_type,
                "progress_report": first["progress_report"],
                "report_key": key,
                "status": status,
                "report_status": rc.STATUS_LABELS[status],
                "counted": status in rc.COUNTED,
                "period_start": report.period_start,
                "period_end": report.period_end,
                "due_date": report.due_date,
                "location": meta.get("name") or "",
                "p_code": meta.get("p_code") or "",
                "percentage": percentage,
                "ratio": ratio,
                "repeated": cell.repeated,
                "rows": cell.rows,
            }
            if record is not None:
                fields["indicator_report_key"], fields["place"] = ir, place
                fields["record_ids"], fields["chosen_record_id"] = list(cell.records), cell.record
                record.line(key, ir, place, cell.records, cell.record)
            yield fields, cell


def _newest_first(item: dict[str, Any]) -> tuple:
    return (
        -(item["period_end"] or datetime.date.min).toordinal(),
        item["programme_document"] or "",
        item["report"] or "",
        rc.norm(item["indicator"]),
        rc.norm(item["location"]),
    )


def location_rows(
    queryset: QuerySet[dm.ReportedIndicator],
    statuses: dict[Any, str],
    today: datetime.date,
    wanted: set[str] | None = None,
    *,
    record: Any = None,
) -> dict[str, Any]:
    """Report/Location: one row per indicator report and location, with the value of the location in
    the period (the disaggregation total "()", read as :mod:`reached` reads it: a report total repeated
    on every location gives no value of its own, two rows of one location are never added) and PRP's
    figure to date there. ``statuses``: each progress report's status by its key (``progress_reports``);
    ``wanted``: the statuses to keep (None: all). Read only below :data:`LOCATION_LIMIT` rows. ``record``: the
    figure trail's (:func:`_location_cells`; ``record.count`` receives the rows counted against the limit);
    None on the pages."""
    total = queryset.count()
    if record is not None:
        record.count("limit", total, LOCATION_LIMIT)
    if total > LOCATION_LIMIT:
        return {"rows": [], "too_many": total, "limit": LOCATION_LIMIT}
    # only the disaggregation total "()" of each row (the value the tab shows), never every value of
    # every disaggregation, nor the partner's narrative: a web worker holds every row at once
    fields = [f for f in LOCATION_FIELDS if f not in ("disaggregation", "narrative_assessment")]
    rows = list(rc.with_totals(queryset.order_by(*rc.ROW_ORDER), fields))
    out = [
        {
            **fields,
            "value_in_period": None if cell.repeated else cell.value,
            "prp_to_date": cell.prp_cumulative,
        }
        for fields, cell in _location_cells(rows, statuses, today, wanted, record)
    ]
    out.sort(key=_newest_first)
    return {"rows": out, "too_many": 0, "limit": LOCATION_LIMIT}


# ---------------------------------------------------------------------------- Report/Disaggregation
@dataclass
class DisaggregationFilters:
    """What the Report/Disaggregation tab adds to the page's filter: words in the indicator, the location,
    its P-code, and in the disaggregation or its value ("Sex", "female")."""

    indicator: str = ""
    location: str = ""
    p_code: str = ""
    disaggregation: str = ""

    @classmethod
    def from_params(cls, params) -> DisaggregationFilters:
        def text(key: str) -> str:
            return " ".join(str(params.get(key) or "").split())[:200]

        return cls(
            indicator=text("indicator"),
            location=text("location"),
            p_code=text("p_code"),
            disaggregation=text("disaggregation"),
        )

    @property
    def any(self) -> bool:
        return bool(self.indicator or self.location or self.p_code or self.disaggregation)

    def cache_key(self) -> str:
        return _json_key(self)


def _json_key(filters: Any) -> str:
    """A filter dataclass as JSON for a key: lists sorted (no figure depends on the order of the choices),
    None kept apart from an empty list, dates as ISO text."""
    out: dict[str, Any] = {}
    for item in dataclasses.fields(filters):
        value = getattr(filters, item.name)
        if isinstance(value, list | tuple):
            value = sorted(str(v) for v in value)
        elif isinstance(value, datetime.date):
            value = value.isoformat()
        out[item.name] = value
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _words(needle: str, *haystack: str) -> bool:
    """Every word of ``needle`` is in one of the texts (case and spacing ignored)."""
    text = " ".join(rc.norm(h) for h in haystack)
    return all(word in text for word in rc.norm(needle).split())


def disaggregation_rows(
    queryset: QuerySet[dm.ReportedIndicator],
    statuses: dict[Any, str],
    today: datetime.date,
    wanted: set[str] | None = None,
    params=None,
    *,
    record: Any = None,
) -> dict[str, Any]:
    """Report/Disaggregation: per indicator report and location (a *block*), PRP's values by
    disaggregation with their labels, a subtotal per disaggregation and the location total "()"
    (:mod:`neurodb.datamart.disaggregation`: values add up only within one disaggregation, never across).
    The location is read as :mod:`reached` reads it: two rows of one location are never added (the one
    whose value is used gives the disaggregation), and a report total repeated on every location gives
    the location no total of its own. ``statuses`` and ``wanted`` as for :func:`location_rows`;
    ``params``: :class:`DisaggregationFilters`. Read only below :data:`DISAGGREGATION_LIMIT` rows.

    Each block carries its location total; its lines (``groups``, ``untyped``, ``unlabelled``,
    ``table_lines``) are made by :func:`fill_block` for the blocks a page shows or an export writes, never
    for every block of the filter at once (``labels`` and ``filters`` in the result are what it needs).

    ``record``: the figure trail's (:func:`_location_cells`), which also receives the rows before the tab's
    indicator words and those counted against the limit (``record.count``) and each location block the tab's
    location, P-code and disaggregation filters leave out (``record.dropped``); None on the pages."""
    from . import disaggregation as dg

    tab = DisaggregationFilters.from_params(params or {})
    if record is not None and tab.indicator:
        record.count("before_indicator", queryset.count(), None)
    if tab.indicator:
        for word in tab.indicator.split():
            queryset = queryset.filter(indicator__icontains=word)
    total = queryset.count()
    if record is not None:
        record.count("limit", total, DISAGGREGATION_LIMIT)
    if total > DISAGGREGATION_LIMIT:
        return {"blocks": [], "too_many": total, "limit": DISAGGREGATION_LIMIT, "filters": tab}
    # every value of every disaggregation (the tab shows them), never the partner's narrative
    fields = [f for f in LOCATION_FIELDS if f != "narrative_assessment"]
    found = queryset.order_by(*rc.ROW_ORDER).values(*fields).iterator(chunk_size=rc.ROW_CHUNK)
    rows = list(rc.texts_once(found))
    keys = {k for r in rows if isinstance(r["disaggregation"], dict) for k in r["disaggregation"]}
    labels = dg.load_labels(dg.ids_in(keys))
    blocks: list[dict[str, Any]] = []
    for fields, cell in _location_cells(rows, statuses, today, wanted, record):
        if tab.location and not _words(tab.location, fields["location"]):
            if record is not None:
                record.dropped("location", fields["report_key"], fields["indicator_report_key"], 1)
            continue
        if tab.p_code and rc.norm(tab.p_code) not in rc.norm(fields["p_code"]):
            if record is not None:
                record.dropped("p_code", fields["report_key"], fields["indicator_report_key"], 1)
            continue
        prp_total = (
            dg.breakdown(
                {dg.TOTAL_KEY: cell.disaggregation[dg.TOTAL_KEY]},
                ratio=fields["ratio"],
                percentage=fields["percentage"],
                labels=labels,
            ).total
            if isinstance(cell.disaggregation, dict) and dg.TOTAL_KEY in cell.disaggregation
            else None
        )
        block = {
            **fields,
            "modified": cell.modified,
            # the location total: PRP's "()", else the location's value in the period as Reached reads it
            "total": None if cell.repeated else (prp_total if prp_total is not None else cell.value),
            "total_is_prp": prp_total is not None and not cell.repeated,
            "_cell": cell,
        }
        # the disaggregation filter needs the lines to choose: made now (within the row limit)
        if tab.disaggregation and not fill_block(block, labels, tab):
            if record is not None:
                record.dropped("disaggregation", fields["report_key"], fields["indicator_report_key"], 1)
            continue
        blocks.append(block)
    del rows
    blocks.sort(key=_newest_first)
    return {
        "blocks": blocks,
        "too_many": 0,
        "limit": DISAGGREGATION_LIMIT,
        "filters": tab,
        "labels": labels,
    }


def fill_block(block: dict[str, Any], labels, tab: DisaggregationFilters | None = None) -> bool:
    """A Report/Disaggregation block's lines (once): PRP's values by disaggregation with their labels, a
    subtotal per disaggregation (:mod:`neurodb.datamart.disaggregation`: never across them) and the
    location total. False when the tab's disaggregation filter leaves none of its values."""
    from . import disaggregation as dg

    if "table_lines" in block:
        return True
    cell = block["_cell"]
    found = dg.breakdown(
        cell.disaggregation, ratio=block["ratio"], percentage=block["percentage"], labels=labels
    )
    groups_shown, untyped = found.groups, found.untyped
    if tab is not None and tab.disaggregation:
        kept = [
            (g, [ln for ln in g.lines if _words(tab.disaggregation, g.name, ln.label)]) for g in groups_shown
        ]
        groups_shown = [
            dg.Group(g.name, g.cross, lines, g.subtotal, g.matches, hidden=len(g.lines) - len(lines))
            for g, lines in kept
            if lines
        ]
        untyped = [ln for ln in untyped if _words(tab.disaggregation, ln.label)]
        if not groups_shown and not untyped:
            return False
    block.update({"groups": groups_shown, "untyped": untyped, "unlabelled": found.unlabelled})
    block["table_lines"] = disaggregation_lines(block)
    return True


def filled_blocks(found: dict[str, Any], blocks: list[dict[str, Any]] | None = None):
    """``blocks`` (every block of ``found`` when None), each with its lines (:func:`fill_block`)."""
    for block in found["blocks"] if blocks is None else blocks:
        fill_block(block, found["labels"], found["filters"])
        yield block


def disaggregation_lines(block: dict[str, Any]) -> list[dict[str, Any]]:
    """A block as the lines the table and the exports show: each value of each disaggregation, the
    disaggregation's subtotal, the values PRP names without a disaggregation, then the location total.
    A value, a subtotal and a total are in columns of their own, so no column adds them together."""
    lines: list[dict[str, Any]] = []
    for group in block["groups"]:
        for line in group.lines:
            lines.append({"line": "value", "type": group.name, "label": line.label, "value": line.value})
        lines.append(
            {
                "line": "subtotal",
                "type": group.name,
                "label": "Subtotal" if not group.cross else "Subtotal of the cross-tab",
                "subtotal": group.subtotal,
                "matches": group.matches,
                "hidden": group.hidden,
            }
        )
    for line in block["untyped"]:
        lines.append({"line": "value", "type": "—", "label": line.label, "value": line.value})
    lines.append({"line": "total", "type": "", "label": "Location total", "total": block["total"]})
    return lines


# ------------------------------------------------------------------------------------------ options
def stored_options() -> dict[str, Any]:
    """:func:`filter_options` for the Progress reports page: stored ten minutes for every worker under a key
    the eTools jobs' runs change (:func:`neurodb.datamart.keys.fingerprint`), the same for every user and
    language (the status labels are English constants, translated by the templates). A frame that finds
    another request computing them computes them itself rather than wait (17 ms on a large database); no part
    reads them (a part request would answer "pending" instead: :func:`neurodb.core.compute.once`). In DEBUG
    they are read at each call."""
    if settings.DEBUG:
        return filter_options()
    key = f"pr-options:v{CACHE_VERSION}:{keys.fingerprint()}"
    return compute.once(key, filter_options, OPTIONS_SECONDS, wait=0, lang_neutral=True)


def filter_options() -> dict[str, Any]:
    rows = dm.ReportedIndicator.objects.order_by()
    pds = PCA.objects.filter(pk__in=rows.exclude(intervention=None).values("intervention_id")).order_by()
    known = {_spaced(x) for names in pds.values_list("section_names", flat=True) for x in names or [] if x}
    # one option per section name, the PDs' spelling first: a row's list ("Child Protection, Adolescent &
    # Youth") gives each of its names, which section_q finds inside the list
    found = {name.casefold(): name for name in sorted(known - {""})}
    for text in rows.values_list("section", flat=True).distinct():
        for name in section_names(text, known):
            found.setdefault(name.casefold(), name)
    sections = set(found.values())
    reporting = PartnerOrganization.objects.filter(pk__in=rows.exclude(partner=None).values("partner_id"))
    partners = reporting.order_by("name", "id").values_list("id", "name")
    partner_types = reporting.order_by()
    numbers = {x for x in rows.values_list("report_number", flat=True).distinct() if x}
    return {
        "statuses": [{"value": k, "label": v} for k, v in rc.STATUS_LABELS.items()],
        "types": sorted(x for x in rows.values_list("report_type", flat=True).distinct() if x),
        "years": sorted(
            {
                y
                for y in rows.exclude(period_end=None)
                .annotate(y=ExtractYear("period_end"))
                .values_list("y", flat=True)
                .distinct()
                if y
            },
            reverse=True,
        ),
        "sections": sorted(sections - {""}, key=str.lower),
        # QPR1, QPR2 ... HR10: the type, then the number
        "report_numbers": sorted(
            numbers,
            key=lambda x: (re.sub(r"[0-9]+", "", x), int(n) if (n := re.sub(r"[^0-9]", "", x)) else 0, x),
        ),
        "partners": [{"value": str(pk), "label": name} for pk, name in partners],
        "partner_types": sorted(
            {x for pair in partner_types.values_list("partner_type", "cso_type") for x in pair if x}
        ),
        # the running statuses first (the filter's default); draft and cancelled PDs are never read
        "pd_statuses": sorted(
            {x for x in pds.values_list("status", flat=True).distinct() if x}
            - set(pd_statuses.NOT_A_PARTNERSHIP)
            | set(pd_statuses.RUNNING),
            key=lambda x: (x not in pd_statuses.RUNNING, x),
        ),
    }


# ------------------------------------------------------------------------------ PD/SPD Indicators
INDICATOR_COLUMNS = (
    "programme_document",
    "pd_status",
    "partner",
    "document_type",
    "pd_title",
    "start",
    "end",
    "office",
    "section",
    "cp_output",
    "pd_output",
    "indicator",
    "unit",
    "baseline",
    "target",
    "emergency",
)


def pd_indicators(pds: QuerySet[PCA], params) -> list[dict[str, Any]]:
    """PD/SPD Indicators: one row per indicator of these programme documents (the Datamart lists an
    indicator once per location and disaggregation; it is identified by its eTools id, else its title,
    as on Partner monitoring), with its PD's attributes, its target and baseline. ``params``: ``emergency``
    (yes/no: the indicators of SPDs, NeuroDB's Emergency indicator as Power BI's), ``section`` (the
    indicator's own section), ``indicator`` (words in its title) and ``pd_q`` (words in the PD reference)."""
    from .monitoring import _target, ident_of, pd_cp_outputs

    pd_q = (params.get("pd_q") or "").strip()
    if pd_q:
        pds = pds.filter(number__icontains=pd_q)
    by_id = {pd.id: pd for pd in pds}
    if not by_id:
        return []
    rows = dm.PDIndicator.objects.filter(intervention_id__in=list(by_id))
    emergency = str(params.get("emergency") or "").strip().lower()
    if emergency in ("yes", "no"):  # the programme document is an SPD (reached.is_spd)
        spd = rc.spd_q("intervention__")
        rows = rows.filter(spd) if emergency == "yes" else rows.exclude(spd)
    words = (params.get("indicator") or "").strip()
    if words:
        rows = rows.filter(title__icontains=words)
    sections = {s.strip() for s in _getlist(params, "section")}
    found: dict[tuple[int, str], dict[str, Any]] = {}
    fields = (
        "intervention_id",
        "source_id",
        "title",
        "section_name",
        "cp_output",
        "lower_result_name",
        "unit",
        "display_type",
        "baseline_numerator",
        "target_numerator",
        "target_denominator",
    )
    for row in rows.order_by("lower_result_name", "title", "datamart_id").values(*fields):
        ident = ident_of(row["intervention_id"], row["source_id"], row["title"])
        entry = found.get(ident)
        if entry is not None:
            entry["cp_output"] = entry["cp_output"] or row["cp_output"] or ""
            continue
        pd = by_id[row["intervention_id"]]
        offices = [x for x in pd.offices_set or [] if x]
        found[ident] = {
            "pd": pd,
            "programme_document": pd.number or "",
            "pd_status": pd_statuses.label(pd.status),
            "partner": pd.partner.name if pd.partner_id and pd.partner else (pd.partner_name or ""),
            "document_type": pd.document_type or "",
            "pd_title": pd.title,
            "start": pd.start,
            "end": pd.end,
            "office": ", ".join(offices),
            "section": (row["section_name"] or "").strip(),
            "cp_output": row["cp_output"] or "",
            "pd_output": row["lower_result_name"] or "",
            "indicator": row["title"],
            "unit": row["unit"] or "",
            "display_type": row["display_type"] or "",
            "baseline": rc.number(row["baseline_numerator"]),
            "target": _target(row),
            "emergency": "Yes" if rc.is_spd(pd.document_type) else "No",  # the PD is an SPD
            "ident": ident,  # its identity in its PD, as Partner monitoring's (Compare with Power BI's)
        }
    out = []
    for entry in found.values():
        if not entry["cp_output"]:  # not reported yet: the PD's own CP outputs, joined (as Power BI)
            entry["cp_output"] = pd_cp_outputs(entry["pd"])
        if sections and entry["section"] not in sections:
            continue
        out.append(entry)
    out.sort(
        key=lambda e: (e["programme_document"].lower(), rc.norm(e["pd_output"]), rc.norm(e["indicator"]))
    )
    return out
