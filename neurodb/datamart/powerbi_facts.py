"""What Compare with Power BI knows about NeuroDB's records, beside the pages' own figures (``powerbi_page``):
the simple facts a difference's reason is decided from, and Power BI's possible rules counted from NeuroDB's
records where Power BI's own rule is not known.

* :class:`Context`: one comparison's day (the statuses are read for it), where NeuroDB's history starts, when
  Power BI refreshed and when the partner reports sync NeuroDB reads finished (their moments: a sync later the
  same day ran after the refresh), the people cleaner, and what one export tells another: the report list's
  gaps explain the counted visuals, its rows (:class:`Listed`) give the periods the follow-up list and the
  location export lack and its reasons tie disaggregation rows and Data Quality's indicators to reports, the
  location export ties disaggregation rows to their PDs (it has PD #, the disaggregation export none). Each
  is built in the comparer's own variables and set at its end; one that fails leaves them None
  (:meth:`Context.reset`), and every reader then keeps the reason it gives without them.
* :class:`Pds` and :class:`Reports`: every programme document and every progress report NeuroDB holds,
  whatever its status, section, year or PD status (one query each): is the PD there, what is its status, is
  the report there, under which status words, in how many records.
* :func:`indicator_facts`: each PD indicator's reports of both types (quarterly and monthly), placed on the
  indicator as Partner monitoring places them (``identity.Idents``) and read as ``reached`` reads them, for
  Program Performance's and PD/SPD Indicator's Reached and Target candidates (:data:`REACHED_CANDIDATES`,
  :data:`TARGET_CANDIDATES`) and the Data Quality readings.
* :func:`money`: the Outstanding DCT sums and the financial contribution candidates.

Nothing here reads a person: the records are read with explicit field lists that never include
``submitted_by``, ``narrative``, ``narrative_assessment``, the focal points or a record's whole ``data``.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Count, Max, Sum
from django.db.models.fields.json import KT

from neurodb.partnerships.models import PCA

from . import identity
from . import models as dm
from . import powerbi_match as pm
from . import powerbi_words as words
from . import reached as rc

# the partner report fields the facts read: Reached's, each row's disaggregation read as its total alone,
# without the partner's narrative (``reached.with_totals``)
ROW_FIELDS = rc.TOTAL_FIELDS
FACT_PDS = 25  # PDs whose report rows :func:`indicator_facts` reads at once
REACHED_CANDIDATES = ("qpr_sum", "every_sum", "latest", "prp_cumulative", "accepted")
TARGET_CANDIDATES = ("etools", "prp_sum", "prp_one")


# ------------------------------------------------------------------------------------------- the context
@dataclass
class Context:
    """One comparison: the day the statuses are read for, the first period NeuroDB holds, Power BI's refresh
    and NeuroDB's partner reports sync (dates), the names the people cleaner withholds, the section names
    NeuroDB knows, and what an export compared earlier tells a later one."""

    day: datetime.date
    history: datetime.date
    refreshed: datetime.date | None = None
    synced: datetime.date | None = None
    refreshed_at: datetime.datetime | None = None  # the moments (aware): which came first
    synced_at: datetime.datetime | None = None
    names: frozenset[str] = frozenset()
    sections: tuple[str, ...] = ()
    gaps: Counter | None = None  # the report list's gaps (powerbi_kinds.report_list); None: not uploaded
    gaps_cut: int = 0  # the report list's rows, when Power BI may have cut it there
    dq: dict[str, Any] | None = None  # the Data Quality list's NeuroDB count and gaps
    # the report list's rows (set aside ones too), their count per period-end year, and the reason each was
    # given: {(PD #, report, period end): "held" when paired, else its line's reason}
    listed: Listed | None = None
    listed_years: Counter | None = None
    listed_reasons: dict[tuple[str, str, str], str] | None = None
    # the location export's rows (``powerbi_kinds.locations``, its copies joined): the PDs of each (title,
    # report, period start or "", period end, P-code), with and without the period start (``location_any``);
    # the reports of each (PD base, title, period end); the reports of each (PD base, title) as (PD #,
    # report, period end); each report's Report Status by (PD #, report, period end); and NeuroDB's own
    # location rows of that comparison as (PD base, title, report, period end, P-code)
    location_pds: dict[tuple, set[str]] | None = None
    location_any: dict[tuple, set[str]] | None = None
    location_start: bool = False  # the location export had a Period Start column
    location_reports: dict[tuple[str, str, str], set[str]] | None = None
    location_titles: dict[tuple[str, str], set[tuple[str, str, str]]] | None = None
    location_status: dict[tuple[str, str, str], str] | None = None
    held_locations: set[tuple] | None = None
    # NeuroDB shows PRP's own reports still owed (datamart.owed: the gate open), and when the last read of
    # PRP's progress reports finished (a report found only there is as recent as that read, not as the partner
    # reports' sync)
    owed_shown: bool = False
    prp_synced_at: datetime.datetime | None = None
    cache: dict[Any, Any] = field(default_factory=dict)
    _memo: dict[str, str] = field(default_factory=dict)

    @property
    def history_words(self) -> str:
        return f"{self.history.day} {self.history:%b %Y}"

    @property
    def sync_after_refresh(self) -> bool:
        """NeuroDB's last partner reports sync finished after Power BI's refresh (later the same day too): a
        row Power BI has and the sync did not bring is not on its way. False when either moment is not
        known."""
        return bool(self.synced_at and self.refreshed_at and self.synced_at > self.refreshed_at)

    @property
    def sync_before_refresh(self) -> bool:
        """The sync finished before Power BI's refresh. False when either moment is not known."""
        return bool(self.synced_at and self.refreshed_at and self.synced_at < self.refreshed_at)

    def before_refresh(self, report: Any) -> bool:
        """The read a held report comes from finished before Power BI's refresh: the partner reports' sync, or
        the last read of PRP's progress reports for a report found only there (``source == "prp"``)."""
        if getattr(report, "source", "") != "prp":
            return self.sync_before_refresh
        return bool(self.prp_synced_at and self.refreshed_at and self.prp_synced_at < self.refreshed_at)

    def after_refresh(self, report: Any) -> bool:
        """The read a held report comes from finished after Power BI's refresh (:meth:`before_refresh`)."""
        if getattr(report, "source", "") != "prp":
            return self.sync_after_refresh
        return bool(self.prp_synced_at and self.refreshed_at and self.prp_synced_at > self.refreshed_at)

    def group(self, reason: str) -> int:
        if reason == "not_in_read":
            return 4 if self.sync_after_refresh else 2
        return words.REASON_GROUPS[reason]

    def not_in_read_detail(self) -> str:
        """Why a row may be missing from the Datamart read: said only when NeuroDB knows which came first."""
        if self.sync_after_refresh:
            return words.DETAIL_SYNC_AFTER
        if self.sync_before_refresh:
            return words.DETAIL_SYNC_BEFORE
        return words.DETAIL_SYNC_UNKNOWN

    def clean(self, text: Any) -> str:
        """A NeuroDB text as Power BI's are kept: without the names NeuroDB knows, e-mails and phones."""
        from . import powerbi_read

        return powerbi_read.clean(str(text or ""), self.names, self._memo) if text else ""

    def once(self, key: Any, build):
        """``build()`` once per export (a page's figures the export reads more than once), until
        :meth:`release`."""
        if key not in self.cache:
            self.cache[key] = build()
        return self.cache[key]

    def release(self) -> None:
        """Let go of every figure :meth:`once` kept: the job calls it after each export, so no export's data
        stays alive into the next (each reads its own)."""
        self.cache.clear()

    def reset(self, kind: str) -> None:
        """What an export of this kind tells the others, set back to "not uploaded": its comparison failed,
        and a later export never reads half of it."""
        for name, empty in RESET.get(kind, {}).items():
            setattr(self, name, empty)


# what each kind of export tells the exports compared after it, and its value when it is not known
RESET: dict[str, dict[str, Any]] = {
    "report_list": {
        "gaps": None,
        "gaps_cut": 0,
        "listed": None,
        "listed_years": None,
        "listed_reasons": None,
    },
    "locations": {
        "location_pds": None,
        "location_any": None,
        "location_start": False,
        "location_reports": None,
        "location_titles": None,
        "location_status": None,
        "held_locations": None,
    },
    "dq_list": {"dq": None},
}


@dataclass
class Listed:
    """The PD/SPD Report list's rows of one upload (those set aside before NeuroDB's history too), each as
    ``pd``, ``report``, ``label`` (its Report Status as compared), ``period_start``, ``period_end``, ``due``,
    ``partner`` and ``sections``, by PD # and by PD # without its amendment suffix."""

    by_number: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    by_base: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, row: dict[str, Any]) -> None:
        self.by_number[pm.pd_number(row["pd"])].append(row)
        self.by_base[pm.pd_base(row["pd"])].append(row)

    def of(self, pd: Any) -> list[dict[str, Any]]:
        """The rows of a PD: by its number, else by its number without the amendment suffix."""
        return self.by_number.get(pm.pd_number(pd)) or self.by_base.get(pm.pd_base(pd), [])

    def group(self, pd: Any, label: Any, due: Any) -> list[dict[str, Any]]:
        """The rows of a PD with this status and due date (Power BI's follow-up list has no report number)."""
        label, due = pm.text(label), pm.iso(due)
        return [r for r in self.of(pd) if r["label"] == label and pm.iso(r["due"]) == due]

    def report(self, pd: Any, report: Any) -> list[dict[str, Any]]:
        """The rows of a PD's report (any period)."""
        key = pm.text(pm.report_number(report))
        return [r for r in self.of(pd) if pm.text(r["report"]) == key]


# ---------------------------------------------------------------------------------------- programme documents
@dataclass(frozen=True)
class Pd:
    """A programme document as the facts read it."""

    id: int
    number: str
    status: str
    document_type: str
    sections: tuple[str, ...]
    cp_outputs: tuple[str, ...]
    partner: str
    start: datetime.date | None
    end: datetime.date | None
    offices: tuple[str, ...]
    title: str

    @property
    def status_label(self) -> str:
        from neurodb.partnerships import statuses as pd_statuses

        return pd_statuses.label(self.status)


class Pds:
    """Every programme document NeuroDB holds (draft and cancelled ones too), by its number as Power BI writes
    it (any case), and by its number without the amendment suffix."""

    def __init__(self) -> None:
        self.by_id: dict[int, Pd] = {}
        self.by_number: dict[str, list[Pd]] = defaultdict(list)
        self.by_base: dict[str, list[Pd]] = defaultdict(list)
        fields = (
            "id",
            "number",
            "status",
            "document_type",
            "section_names",
            "cp_outputs",
            "partner_name",
            "partner__name",
            "start",
            "end",
            "offices_set",
            "title",
        )
        for row in PCA.objects.order_by("id").values(*fields):
            pd = Pd(
                id=row["id"],
                number=row["number"] or "",
                status=row["status"] or "",
                document_type=row["document_type"] or "",
                sections=tuple(" ".join(s.split()) for s in row["section_names"] or [] if s and s.strip()),
                cp_outputs=tuple(
                    dict.fromkeys(" ".join(str(c).split()) for c in row["cp_outputs"] or [] if str(c).strip())
                ),
                partner=row["partner__name"] or row["partner_name"] or "",
                start=row["start"],
                end=row["end"],
                offices=tuple(o for o in row["offices_set"] or [] if o),
                title=row["title"] or "",
            )
            self.by_id[pd.id] = pd
            if pd.number:
                self.by_number[pm.pd_number(pd.number)].append(pd)
                self.by_base[pm.pd_base(pd.number)].append(pd)

    def find(self, number: Any) -> tuple[Pd | None, bool]:
        """The PD of a number (None when NeuroDB holds none, or two share it), and whether only its number
        without the amendment suffix matched."""
        found = self.by_number.get(pm.pd_number(number), [])
        if len(found) == 1:
            return found[0], False
        if found:
            return None, False
        found = self.by_base.get(pm.pd_base(number), [])
        return (found[0], True) if len(found) == 1 else (None, False)


def pds(context: Context) -> Pds:
    return context.once("pds", Pds)


# ------------------------------------------------------------------------------------------- progress reports
@dataclass
class HeldReport:
    """A progress report NeuroDB holds, whatever its status: its status (read for the day, from the newest
    word of its rows), every status word its rows carry, and its records."""

    pd_id: int | None
    report_type: str
    number: str  # as Power BI writes it ("QPR3")
    period_start: datetime.date | None
    period_end: datetime.date | None
    due_date: datetime.date | None
    status: str = ""
    words: set[str] = field(default_factory=set)
    records: int = 0
    stamps: dict[str, rc.Stamp] = field(default_factory=dict)
    submission_date: datetime.date | None = None
    source: str = ""  # "prp": a report still owed found only in PRP's own list (records 0, no value)
    stale: bool = False  # a record of PRP's own list no read returned in the last 7 days (PrpReports)


class _ByNumber:
    """Held reports by programme document number (with and without the amendment suffix), report number and
    period end; and by programme document and due date (Power BI's follow-up list has no report number)."""

    def __init__(self) -> None:
        self.by_report: dict[tuple[str, str, str], list[HeldReport]] = defaultdict(list)
        self.by_due: dict[tuple[str, str], list[HeldReport]] = defaultdict(list)
        self.by_pd: dict[str, list[HeldReport]] = defaultdict(list)

    def add(self, report: HeldReport, numbers: set[str]) -> None:
        end = report.period_end.isoformat() if report.period_end else ""
        due = report.due_date.isoformat() if report.due_date else ""
        for number in numbers:
            self.by_report[(number, pm.text(report.number), end)].append(report)
            self.by_due[(number, due)].append(report)
            self.by_pd[number].append(report)

    def find(self, pd: Any, number: Any, end: Any, report_type: Any = "") -> list[HeldReport]:
        end = pm.iso(end)
        key = pm.text(pm.report_number(number, report_type))
        return self.by_report.get((pm.pd_number(pd), key, end)) or self.by_report.get(
            (pm.pd_base(pd), key, end), []
        )

    def due(self, pd: Any, due: Any) -> list[HeldReport]:
        due = pm.iso(due)
        return self.by_due.get((pm.pd_number(pd), due)) or self.by_due.get((pm.pd_base(pd), due), [])

    def of_pd(self, pd: Any) -> list[HeldReport]:
        return self.by_pd.get(pm.pd_number(pd)) or self.by_pd.get(pm.pd_base(pd), [])


class Reports(_ByNumber):
    """Every progress report NeuroDB holds (one query, grouped by the database): by programme document number
    (the PD's, else the report's own reference; with and without the amendment suffix), report number and
    period end; and by programme document and due date (Power BI's follow-up list has no report number)."""

    def __init__(self, context: Context, held: Pds) -> None:
        super().__init__()
        found: dict[str, HeldReport] = {}
        rows = (
            dm.ReportedIndicator.objects.values(*rc.IDENTITY_FIELDS, "progress_report", "report_status")
            .annotate(
                n=Count("pk"),
                due=Max("due_date"),
                sub=Max("submission_date"),
                changed=Max("last_modify_date"),
                synced=Max("synced_at"),
                last=Max("datamart_id"),
            )
            .order_by()
        )
        numbers: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            key = rc.report_key(row)
            report = found.get(key)
            if report is None:
                report = found[key] = HeldReport(
                    pd_id=row["intervention_id"],
                    report_type=(row["report_type"] or "").strip().upper(),
                    number=pm.report_number(row["report_number"], row["report_type"]),
                    period_start=row["period_start"],
                    period_end=row["period_end"],
                    due_date=row["due"],
                )
            word = (row["report_status"] or "").strip()
            report.words.add(word)
            report.records += row["n"]
            report.stamps[word] = rc.stamp(
                report.stamps.get(word), (row["changed"], row["synced"], row["last"])
            )
            report.due_date = report.due_date or row["due"]
            report.submission_date = report.submission_date or row["sub"]
            pd = held.by_id.get(row["intervention_id"]) if row["intervention_id"] else None
            for number in (pd.number if pd else "", row["pd_reference_number"] or ""):
                if number:
                    numbers[key].add(pm.pd_number(number))
                    numbers[key].add(pm.pd_base(number))
        for report in found.values():
            report.status = rc.status_of(
                rc.newest_status(report.stamps),
                period_end=report.period_end,
                due_date=report.due_date,
                submission_date=report.submission_date,
                today=context.day,
            )
        if context.owed_shown:  # PRP's own reports still owed the pages show (none the rows hold)
            from . import owed

            for r in owed.every_owed(context.day):
                key = f"prp:{r['record']}"
                found[key] = HeldReport(
                    pd_id=r["intervention_id"],
                    report_type=r["report_type"],
                    number=pm.report_number(r["report_number"], r["report_type"]),
                    period_start=r["period_start"],
                    period_end=r["period_end"],
                    due_date=r["due_date"],
                    status=r["status"],
                    words={r["report_status"]},
                    submission_date=r["submission_date"],
                    source="prp",
                )
                if r["pd_reference_number"]:  # its PD's number, as NeuroDB holds it
                    numbers[key].add(pm.pd_number(r["pd_reference_number"]))
                    numbers[key].add(pm.pd_base(r["pd_reference_number"]))
        for key, report in found.items():
            self.add(report, numbers[key])


def reports(context: Context) -> Reports:
    return context.once("reports", lambda: Reports(context, pds(context)))


class PrpReports(_ByNumber):
    """Every record of PRP's own list of progress reports linked to a programme document NeuroDB holds
    (``datamart_sync.linked_progress_reports``: every version, any status, ``stale`` when no read returned it
    in the last 7 days), as held reports of source "prp", the latest version first: why a report still owed
    that NeuroDB does not show is missing once it shows them (``powerbi_kinds.owed_reason``). Read field by
    field: no person."""

    def __init__(self, context: Context, held: Pds) -> None:
        from neurodb.integrations.etools import datamart_sync as sync

        super().__init__()
        since = sync.fresh_since()
        found = sync.linked_progress_reports(context.day)
        found.sort(key=lambda r: (r["read"], r["end"] or datetime.date.min, r["pk"]), reverse=True)
        for r in found:
            pd = held.by_id.get(r["pd"])
            if pd is None or not pd.number:
                continue
            report = HeldReport(
                pd_id=r["pd"],
                report_type=r["kind"],
                number=r["key"],
                period_start=r["start"],
                period_end=r["end"],
                due_date=r["due"],
                status=r["status"],
                words={r["word"]},
                submission_date=r["sub"],
                source="prp",
                stale=r["synced_at"] < since,
            )
            self.add(report, {pm.pd_number(pd.number), pm.pd_base(pd.number)})


def prp_reports(context: Context) -> PrpReports:
    return context.once("prp-reports", lambda: PrpReports(context, pds(context)))


# ---------------------------------------------------------------------------------- each indicator's reports
@dataclass
class IndicatorFact:
    """One PD indicator's reports of both types (oldest first), read as ``reached`` reads them, with what
    Power BI's possible rules need."""

    ident: tuple[int, str]
    ratio: bool = False
    percentage: bool = False
    period_method: str = ""
    by_type: dict[str, list[rc.Report]] = field(default_factory=dict)
    # the latest Accepted report's PRP target at each location, as the values are read
    targets: dict[str, float] = field(default_factory=dict)
    # per report key: the values of each location cell (for "any one row without a value or 0")
    cells: dict[str, list[float | None]] = field(default_factory=dict)

    @property
    def every(self) -> list[rc.Report]:
        return sorted((r for found in self.by_type.values() for r in found), key=lambda r: r.order)

    @property
    def accepted(self) -> list[rc.Report]:
        return [r for r in self.every if r.status == rc.ACCEPTED and r.value is not None]

    @property
    def any_accepted(self) -> bool:
        return any(r.status == rc.ACCEPTED for r in self.every)

    def _over(self, found: list[rc.Report]) -> float | None:
        return rc.combine_items(
            [r.item for r in found],
            self.period_method or "sum",
            ratio=self.ratio,
            percentage=self.percentage,
            fallback="latest",
        )[0]

    def reached(self, accepted: float | None = None, year: int | None = None) -> dict[str, float | None]:
        """Power BI's possible Reached (:data:`REACHED_CANDIDATES`), to date or in ``year`` (the reports whose
        period ends in it); ``accepted``: the page's own Accepted-only figure, for the same."""
        found = self.accepted if year is None else [r for r in self.accepted if _ends_in(r, year)]
        quarterly = [r for r in found if r.report_type == "QPR"]
        latest = found[-1] if found else None
        qpr = self._over(quarterly) if quarterly else (0.0 if found else None)
        return {
            "qpr_sum": qpr,
            "every_sum": self._over(found) if found else None,
            "latest": latest.value if latest else None,
            "prp_cumulative": latest.prp_cumulative if latest else None,
            "accepted": accepted,
        }

    def target(self, etools: float | None) -> dict[str, Any]:
        """Power BI's possible Target (:data:`TARGET_CANDIDATES`): ``prp_one`` holds every location's."""
        return {
            "etools": etools,
            "prp_sum": sum(self.targets.values()) if self.targets else None,
            "prp_one": sorted(set(self.targets.values())) if self.targets else None,
        }

    def accepted_in(self, year: int, report_type: str = "") -> float | None:
        """The Accepted reports of ``year`` combined as NeuroDB combines them (those of ``report_type``, the
        type the page reads, when given): the page's Accepted-only figure for that year."""
        found = [r for r in self.accepted if _ends_in(r, year)]
        if report_type:
            found = [r for r in found if r.report_type == report_type]
        return self._over(found) if found else None

    def in_year(self, year: int | None) -> list[rc.Report]:
        if year is None:
            return self.every
        return [r for r in self.every if _ends_in(r, year)]

    def value(self, year: int | None, statuses: Iterable[str] | None = None) -> float | None:
        """The indicator's value in ``year`` from the reports of ``statuses`` (None: every status)."""
        wanted = set(statuses) if statuses is not None else None
        found = [
            r for r in self.in_year(year) if r.value is not None and (wanted is None or r.status in wanted)
        ]
        return self._over(found) if found else None

    def empty_row(self, year: int | None) -> bool:
        """One location row of a report of ``year`` (of any status) has no value or 0."""
        return any(v is None or v == 0 for r in self.in_year(year) for v in self.cells.get(r.key, []))


def _ends_in(report: rc.Report, year: int) -> bool:
    return bool(report.period_end and report.period_end.year == year)


def indicator_facts(
    pd_ids: Iterable[int], day: datetime.date, context: Context | None = None
) -> dict[tuple[int, str], IndicatorFact]:
    """Each indicator of these PDs, with its quarterly and monthly reports (placed as Partner monitoring
    places them; each report's status from all its rows); read once per export for the same PDs, and
    :data:`FACT_PDS` PDs at a time (an indicator's facts are its PD's rows alone)."""
    ids = sorted({i for i in pd_ids if i})
    if not ids:
        return {}
    if context is not None:
        return context.once(("indicator_facts", tuple(ids)), lambda: indicator_facts(ids, day))
    out: dict[tuple[int, str], IndicatorFact] = {}
    for start in range(0, len(ids), FACT_PDS):
        out.update(_facts(ids[start : start + FACT_PDS], day))
    return out


def _facts(ids: list[int], day: datetime.date) -> dict[tuple[int, str], IndicatorFact]:
    """:func:`indicator_facts` of a few PDs. Each report is kept without its locations (its value, status and
    period are what the readings read; ``cells`` keeps each location's value)."""
    rows = list(
        rc.with_totals(
            dm.ReportedIndicator.objects.filter(
                intervention_id__in=ids, report_type__in=["QPR", "HR"]
            ).order_by(*rc.ROW_ORDER),
            ROW_FIELDS,
        )
    )
    idents = identity.Idents(ids)
    statuses = rc.report_status_texts(rows)
    per: dict[tuple[tuple[int, str], str], list[dict[str, Any]]] = defaultdict(list)
    for row, spot in zip(rows, idents.place(rows), strict=True):
        if spot.ident is not None:
            per[(spot.ident, (row["report_type"] or "").strip().upper())].append(row)
    out: dict[tuple[int, str], IndicatorFact] = {}
    for (ident, kind), group in per.items():
        display_type, unit, _target = idents.definition(ident) or ("", "", None)
        methods = {r.get("calculation_across_locations") or "" for r in group} | {
            r.get("calculation_across_periods") or "" for r in group
        }
        ratio = rc.is_ratio(display_type, unit, *methods)
        percentage = (display_type or "").lower() == "percentage" or (unit or "").lower() == "percentage"
        fact = out.setdefault(ident, IndicatorFact(ident, ratio=ratio, percentage=percentage))
        found = rc.build_reports(group, ratio=ratio, percentage=percentage, today=day, report_status=statuses)
        fact.by_type[kind] = found
        fact.period_method = fact.period_method or next(
            (r.period_method for r in found if r.period_method), ""
        )
        for report in found:
            fact.cells[report.key] = [None if c.repeated else c.value for c in report.cells.values()]
    for fact in out.values():
        latest = fact.accepted[-1] if fact.accepted else None
        if latest is None:
            continue
        targets: dict[str, float] = {}
        for row in per[(fact.ident, latest.report_type)]:
            if rc.report_key(row) != latest.key:
                continue
            parsed = rc.parts(row.get("target"))
            value = parsed.value(fact.ratio, fact.percentage) if parsed else None
            if value is not None:
                place = rc.location_key(row.get("p_code"), row.get("location"))
                targets[place] = max(targets.get(place, value), value)
        fact.targets = targets
    for fact in out.values():
        for report in fact.every:
            report.cells, report.places = {}, {}
    return out


def fit(rows: list[tuple[float | None, dict[str, Any]]], names: Iterable[str]) -> list[dict[str, Any]]:
    """How many rows each candidate fits: ``rows`` are (Power BI's value, the candidates' values); a candidate
    whose value is a list fits when Power BI is one of them. The chosen one fits most rows where Power BI is
    above 0 (zeros fit by chance), the order of ``names`` breaking a tie. ``positive``: the rows where Power
    BI is above 0 (a choice is offered only when the chosen one fits most of them)."""
    table = []
    positive = sum(1 for powerbi, _candidates in rows if powerbi)
    for name in names:
        fits = above = 0
        for powerbi, candidates in rows:
            if fits_value(powerbi, candidates.get(name)):
                fits += 1
                above += bool(powerbi)
        table.append(
            {
                "candidate": name,
                "fits": fits,
                "of": len(rows),
                "above": above,
                "positive": positive,
                "chosen": False,
            }
        )
    if table and any(t["above"] for t in table):
        best = max(table, key=lambda t: (t["above"], t["fits"], -list(names).index(t["candidate"])))
        best["chosen"] = True
    return table


def fits_value(powerbi: float | None, candidate: Any) -> bool:
    if isinstance(candidate, list):
        return any(pm.same_number(powerbi, c, zero_is_none=True) for c in candidate)
    return pm.same_number(powerbi, candidate, zero_is_none=True)


def chosen(table: list[dict[str, Any]]) -> str:
    return next((t["candidate"] for t in table if t["chosen"]), "")


def inactive(context: Context, pd_ids: Iterable[int]) -> set[tuple[int, str]]:
    """The indicators of these PDs eTools marks inactive in every row (``is_active`` false), by their identity
    (``identity.ident_of``): mostly an older version of a PD's indicator. The pages list one only while a
    partner report row names it, or no partner report names its PD yet (``identity.listed``), beside the
    current version Power BI lists (Raw eTools data → PD indicators, Active: no). One query."""
    ids = sorted({i for i in pd_ids if i})
    if not ids:
        return set()

    def build() -> set[tuple[int, str]]:
        seen: dict[tuple[int, str], set[bool]] = defaultdict(set)
        for start in range(0, len(ids), 5000):
            for pd_id, source_id, title, active in dm.PDIndicator.objects.filter(
                intervention_id__in=ids[start : start + 5000]
            ).values_list("intervention_id", "source_id", "title", "is_active"):
                seen[identity.ident_of(pd_id, source_id, title)].add(bool(active))
        return {ident for ident, found in seen.items() if found == {False}}

    return context.once(("inactive", tuple(ids)), build)


def left_out(context: Context) -> dict[int, set[str]]:
    """The titles of the indicators the pages leave out (``identity.left_out``: eTools marks them inactive in
    every row and no partner report row names them), by PD id, as a Power BI title is compared (cleaned,
    ``pm.text``): why a Power BI row has no indicator on NeuroDB's page. One query."""

    def build() -> dict[int, set[str]]:
        out: dict[int, set[str]] = defaultdict(set)
        rows = identity.left_out(dm.PDIndicator.objects.exclude(intervention=None))
        for pd_id, title in rows.order_by().values_list("intervention_id", "title").distinct():
            out[pd_id].add(pm.text(context.clean(title)))
        return out

    return context.once("left_out", build)


def reported_pds(context: Context) -> set[int]:
    """The PDs partner report rows name: the only ones whose indicators the pages may leave out
    (``identity.listed``). One DISTINCT read."""

    def build() -> set[int]:
        rows = dm.ReportedIndicator.objects.exclude(intervention=None).order_by()
        return set(rows.values_list("intervention_id", flat=True).distinct())

    return context.once("reported_pds", build)


def listing_marked(context: Context) -> bool:
    """Whether a sync has marked the indicators the pages leave out (``identity.listing_marked``): until one
    has, after the update that brought the rule, the pages still list an indicator eTools marks inactive that
    no partner report row names."""
    return context.once("listing_marked", identity.listing_marked)


def fr_elsewhere(context: Context) -> dict[str, set[str]]:
    """Each PD base number the funds reservation lines name (their ``pd_reference_number``), with the PD
    numbers NeuroDB links those lines to ("" for a line linked to none): one DISTINCT read. A Funded
    Programs row Power BI has whose lines NeuroDB links to another PD, or to none, is never "funds
    reservation only"."""

    def build() -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        rows = (
            dm.FundsReservation.objects.exclude(pd_reference_number="")
            .exclude(pd_reference_number=None)
            .order_by()
            .values_list("pd_reference_number", "intervention__number")
            .distinct()
        )
        for named, linked in rows:
            out[pm.pd_base(named)].add(linked or "")
        return out

    return context.once("fr_elsewhere", build)


def budgets(context: Context, pd_ids: Iterable[int]) -> dict[int, tuple[Any, ...]]:
    """Each PD's budget fields as NeuroDB holds them (UNICEF, total, partner, UNICEF cash), for the detail of
    a budget column that differs (a diagnostic of amended PDs' budgets)."""
    ids = sorted({i for i in pd_ids if i})

    def build() -> dict[int, tuple[Any, ...]]:
        out: dict[int, tuple[Any, ...]] = {}
        for start in range(0, len(ids), 5000):
            for pk, *found in PCA.objects.filter(pk__in=ids[start : start + 5000]).values_list(
                "pk", "total_unicef_budget", "total_budget", "cso_contribution", "unicef_cash"
            ):
                out[pk] = tuple(found)
        return out

    return context.once(("budgets", tuple(ids)), build) if ids else {}


# ------------------------------------------------------------------------------------------------ the money
def outstanding(pd_ids: Iterable[int]) -> dict[int, dict[str, Decimal | None]]:
    """Per PD: the two possible Outstanding DCT (the funds reservations' outstanding amounts added up; the
    PD's own total outstanding in eTools)."""
    from .funds import amount

    ids = sorted({i for i in pd_ids if i})
    out: dict[int, dict[str, Decimal | None]] = {i: {"dct_fr": None, "dct_pd": None} for i in ids}
    for start in range(0, len(ids), 5000):
        chunk = ids[start : start + 5000]
        for pd_id, total in (
            dm.FundsReservationHeader.objects.filter(intervention_id__in=chunk)
            .values("intervention_id")
            .annotate(total=Sum("outstanding_amt"))
            .order_by()
            .values_list("intervention_id", "total")
        ):
            out[pd_id]["dct_fr"] = total
        for pd_id, total in PCA.objects.filter(pk__in=chunk).values_list("pk", "frs_total_outstanding_amt"):
            out[pd_id]["dct_pd"] = amount(total)
    return out


def contributions(pd_ids: Iterable[int], day: datetime.date) -> dict[int, dict[str, Decimal | None]]:
    """Per PD: the possible "Financial contribution during reporting periods": PRP's contribution to date on
    the latest Accepted report, on the latest Accepted or Submitted one (NeuroDB's own), and added up over
    the reports (each report read once: PRP repeats it on each of its indicators)."""
    from .funds import amount

    ids = sorted({i for i in pd_ids if i})
    reports: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for start in range(0, len(ids), 5000):
        docs = (
            dm.DatamartDocument.objects.filter(
                dataset="prp_indicator_reports", intervention_id__in=ids[start : start + 5000]
            )
            .annotate(
                _type=KT("data__report_type"),
                _number=KT("data__report_number"),
                _pr_status=KT("data__pr_status"),
                _status=KT("data__report_status"),
                _pr_end=KT("data__pr_end_date"),
                _end=KT("data__time_period_end"),
                _pr_due=KT("data__pr_due_date"),
                _due=KT("data__due_date"),
                _pr_sub=KT("data__pr_submission_date"),
                _sub=KT("data__submission_date"),
                _value=KT("data__financial_contribution_to_date"),
            )
            .order_by("intervention_id", "record_key")
            .values_list(
                "intervention_id",
                "_type",
                "_number",
                "_pr_status",
                "_status",
                "_pr_end",
                "_end",
                "_pr_due",
                "_due",
                "_pr_sub",
                "_sub",
                "_value",
            )
        )
        for pd_id, kind, number, pr_status, status, pr_end, end, pr_due, due, pr_sub, sub, value in docs:
            status_key, end_date = rc.prp_document_status(
                pr_status=pr_status,
                status=status,
                pr_end=pr_end,
                end=end,
                pr_due=pr_due,
                due=due,
                pr_sub=pr_sub,
                sub=sub,
                today=day,
            )
            key = (pd_id, (kind or "").upper(), str(number or ""), end_date.isoformat() if end_date else "")
            report = reports.setdefault(key, {"end": end_date, "status": status_key, "values": set()})
            found = amount(value)
            if found is not None:
                report["values"].add(found)
    out: dict[int, dict[str, Any]] = {
        i: {"fc_latest_accepted": None, "fc_latest_counted": None, "fc_sum": None} for i in ids
    }
    for (pd_id, _kind, _number, _end), report in sorted(
        reports.items(), key=lambda kv: (kv[1]["end"] or datetime.date.min, kv[0])
    ):
        if not report["values"]:
            continue
        value = max(report["values"])
        mine = out[pd_id]
        if report["status"] == rc.ACCEPTED:
            mine["fc_latest_accepted"] = value
        if report["status"] in rc.COUNTED:
            mine["fc_latest_counted"] = value
            mine["fc_sum"] = (mine["fc_sum"] or Decimal(0)) + value
    return out
