"""The CSVs of the figure trail (SPEC_validation §3.7): the rows behind a step or a figure of
:mod:`neurodb.datamart.trail`, so a person can rebuild any figure of the page in Excel.

* **Report rows** (``what``): ``kept`` (the rows the filter keeps: one pivot table rebuilds the tiles, the
  section table and the month chart), ``step:<filter>`` (the rows a filter leaves out), ``step:start`` (every
  row in NeuroDB), ``step:history`` (rows starting before the date NeuroDB holds partner reports from),
  ``step:S`` (rows of a report status not chosen), ``reason:<why>`` (left out by the PD status filter),
  ``figure:<key>`` (the rows a figure counts), ``no_partner`` and ``status`` (every row of the progress
  reports kept, which set their status). Each starts with ``figures_as_of``, ``step`` and ``reason``; then
  the units (``unit_key`` and one column per unit, so a distinct count in Excel gives the figure) and
  ``counts_once_*`` (1 on the first row of each unit, by the unit, never by the rows' order; in a step's CSV
  only on a unit that has no row left after the step, ``unit_partly_kept`` marking the others, so the column
  adds up to what the step leaves out entirely); what NeuroDB read of the row's report; then the record as
  Raw eTools data writes it (``raw.csv_record``: the people rule, never a person).
* **One row per progress report** (``reports``, ``follow_up``): the export's columns (``_report_row``, focal
  points as a count) with the vendor number and the match key of the Power BI comparison sheet.
* **By location's lines** (``lines``) and **By disaggregation's location blocks** (``blocks``): the tab's
  export columns with the Datamart ids of each location's rows and the one read.
* **PRP's own reports still owed** (``owed``, once their link is proven: :mod:`neurodb.datamart.owed`): one
  row per report the page could add, with whether the filter keeps it and why not, its status as the page
  reads it, then its record as Raw eTools data writes it (field by field, the people rule).

Every CSV streams (``exports.stream_csv``) inside one read-only snapshot, 2,000 records at a time, takes one
of the site's two large download slots and holds at most ``raw.csv_limit("partner_reports")`` records. A query
that takes too long ends a file of records with one last row saying so (``raw.STOPPED``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.db import DatabaseError

from neurodb.partnerships.models import PCA

from . import catalogue, monitoring, owed, raw, raw_people, reporting, runfacts, trail
from . import models as dm
from . import query as dmq
from . import reached as rc
from . import trail_monitoring as tm

logger = logging.getLogger(__name__)

SOURCE = trail.SOURCE
ROW_HEAD = (
    "figures_as_of",
    "step",
    "reason",
    "record_id",
    "progress_report_key",
    "indicator_report_key",
    "unit_key",
    "indicator_report_unit",
    "progress_report_unit",
    "partner_unit",
    "pd_unit",
    "counts_once",
    "counts_once_indicator_report",
    "counts_once_progress_report",
    "counts_once_partner",
    "unit_partly_kept",
    "progress_report_partly_kept",
    "kept_by_the_filter",
    "report_status_neurodb",
    "report_status_chosen",
    "status_word_used",
    "status_word_from_record_id",
    "status_word_of_this_row",
    "status_rows_outside_filter",
    "section_counted",
    "period_end_month",
    "submitted_late",
    "partner_has_reported",
    "pd_number_in_neurodb",
    "pd_status_in_neurodb",
    "pd_link",
    "partner_in_neurodb",
    "vendor_number",
    "synced_at",
    "updated_by_last_sync",
    "no_longer_sent_since",
)
REPORT_COLUMNS = (
    "figures_as_of",
    "step",
    "reason",
    "progress_report_key",
    "programme_document",
    "pd_status",
    "partner",
    "vendor_number",
    "section",
    "document_type",
    "report",
    "report_type",
    "report_status",
    "due_date",
    "period_start",
    "period_end",
    "submission_date",
    "indicator_reports",
    "unicef_focal_points_count",
    "source",
    "match_key",
)
LINE_TAIL = ("progress_report_key", "record_ids", "chosen_record_id")


def _yes(value: bool) -> str:
    return "yes" if value else "no"


# ----------------------------------------------------------------------------------------- what a CSV holds
def kind_of(what: str) -> str:
    """``rows`` (report rows), ``reports`` (one row per progress report), ``lines`` (a tab's lines or blocks)
    or ``documents`` (PRP's own reports still owed); "" for an address the trail does not know."""
    if what == "owed":
        return "documents"
    if what in ("kept", "no_partner", "status") or what.startswith(("step:", "reason:", "figure:")):
        return "rows"
    if what in ("reports", "follow_up"):
        return "reports"
    if what in ("lines", "blocks"):
        return "lines"
    return ""


def selection(found: trail.Trail, what: str) -> tuple[tuple, str, str, str] | None:
    """A report rows CSV's rows (a :func:`trail.selected` selection), its unit, its step and its reason in
    words; None for an address the trail does not know."""
    rows = found.rows
    if what == "kept":
        return (("set", "kept"),), trail.IR, "Kept by the filter", ""
    if what == "status":
        return (("set", "all"),), trail.IR, "The rows that set the reports' status", ""
    if what == "no_partner":
        words = "Progress reports with neither a partner id nor a partner name"
        return (("set", "no_partner"),), trail.PR, words, "left out of every Partners figure"
    if what.startswith("figure:"):
        figure = found.figure(what[7:])
        if figure is None or not figure.select:
            return None
        return figure.select, figure.unit, f"Figure: {figure.label}", figure.how
    if what.startswith("reason:"):
        why = what[7:]
        if why not in reporting.LEFT_OUT_REASONS:
            return None
        step = next(s for s in found.steps if s.key == "pd_status") if "pd_status" in rows.stages else None
        line = next((x.words for x in step.lines if x.what == what), "") if step else ""
        return (("set", what),), trail.IR, "Left out: the PD status filter", line
    if what.startswith("step:"):
        name = what[5:]
        key = {"start": "D4", "history": "D4b"}.get(name, name)  # the dataset steps' rows, by their step
        step = next((s for s in found.steps if s.key == key), None)
        if step is None:
            return None
        base = {"start": "all", "history": "history", "S": "status_out"}.get(name, f"stage:{name}")
        title = step.title if step.kind != "dataset" else step.lines[0].words
        return (
            (("set", base),),
            trail.IR,
            f"Left out: {title}" if step.kind in ("filter", "status") else title,
            "",
        )
    return None


# ---------------------------------------------------------------------------------------------- report rows
def row_pks(found: trail.Trail, what: str) -> list[int]:
    """The pks of a report rows CSV, in the order it writes them."""
    select = selection(found, what)
    if select is None:
        return []
    if what == "status":  # every row of the progress reports kept, whatever the filter
        return [pk for pk, r in sorted(found.rows.rows.items()) if r[1] in found.rows.reports]
    return trail.selected(found.rows, select[0])


def row_columns(names: frozenset[str], pks: list[int]) -> tuple[list[str], list[str]]:
    """The columns of a report rows CSV, and the record fields among them (``raw.header_keys``)."""
    counted, _left_out = raw.field_counts(SOURCE, names)
    if pks:
        keys = raw.header_keys(SOURCE, dm.ReportedIndicator.objects.filter(pk__in=pks), counted)
    else:  # no row: the fields NeuroDB counted
        keys = [f.key for f in counted]
    head = [*ROW_HEAD, *(raw.header_name(k) for k in keys), *raw.TAIL_COLUMNS]
    return head, keys


def _first_out(rows: trail.TrailRows) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """For each indicator report and progress report: the last stage any of its rows reaches (``rows[3]``): a
    unit is left out entirely by the stage it equals."""
    irs: dict[tuple[str, str], int] = {}
    prs: dict[str, int] = {}
    for r in rows.rows.values():
        irs[(r[1], r[2])] = max(irs.get((r[1], r[2]), 0), r[3])
        prs[r[1]] = max(prs.get(r[1], 0), r[3])
    return irs, prs


def stream_rows(
    found: trail.Trail, what: str, pks: list[int], keys: list[str], names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    """The rows of a report rows CSV, read 2,000 at a time inside one snapshot, each written as Raw eTools
    data writes it (``raw.csv_record``) with what the trail read of it."""
    rows = found.rows
    _select, unit, step_words, reason_words = selection(found, what)
    # a figure's words can name a partner as the report rows give it: cleaned as that column is
    step_words = raw_people.text(step_words, SOURCE, "partner_name", names, frozenset())
    reason_words = raw_people.text(reason_words, SOURCE, "partner_name", names, frozenset())
    run = _run()
    as_of = raw.local_moment(found.as_of)
    reviewed = raw.reviewed_keys(SOURCE)
    wanted = set(keys)
    of = trail.unit_of(rows, unit)
    stage = None
    if what.startswith("step:") and what[5:] in rows.stages:
        stage = rows.stages.index(what[5:])
    elif what.startswith("reason:"):
        stage = rows.stages.index("pd_status")
    last_irs, last_prs = _first_out(rows) if stage is not None else ({}, {})
    seen: dict[str, set] = {"unit": set(), "ir": set(), "pr": set(), "partner": set()}
    stages = {i: name for i, name in enumerate(rows.stages)}
    reasons: dict[tuple[int, str], str] = {}  # each step's words, cleaned once (a search can name anyone)
    try:
        with raw.snapshot(raw.STREAM_TIMEOUT_MS):
            for start in range(0, len(pks), raw.CSV_CHUNK):
                chunk = pks[start : start + raw.CSV_CHUNK]
                records = raw.related(SOURCE, dm.ReportedIndicator.objects.filter(pk__in=chunk))
                by_pk = {row.pk: row for row in records}
                for pk in chunk:
                    row = by_pk.get(pk)
                    if row is None:  # removed since the trail was read
                        continue
                    r = rows.rows[pk]
                    out = raw.csv_record(SOURCE, row, wanted, run, names, reviewed, as_of)
                    report = rows.reports.get(r[1]) if r[3] == rows.kept_at or what == "status" else None
                    ir, pr = (r[1], r[2]), r[1]
                    partner = report.partner if report is not None else None
                    left = ""
                    if r[3] != rows.kept_at:
                        left = reasons.get((r[3], r[4]))
                        if left is None:
                            left = reasons[(r[3], r[4])] = raw_people.text(
                                trail_reason(found, stages.get(r[3], ""), r[4]),
                                SOURCE,
                                "partner_name",
                                names,
                                frozenset(),
                            )
                    out.update(
                        step=step_words,
                        reason=reason_words or left,
                        indicator_report_unit=f"{out['progress_report_key']}#{out['indicator_report_key']}",
                        progress_report_unit=out["progress_report_key"],
                        partner_unit=_partner_unit(partner, names),
                        pd_unit=_pd_unit(report.pd if report is not None else "", names),
                        kept_by_the_filter=_yes(r[3] == rows.kept_at),
                    )
                    out["unit_key"] = {
                        trail.IR: out["indicator_report_unit"],
                        trail.PR: out["progress_report_unit"],
                        trail.PARTNER: out["partner_unit"],
                        trail.PD: out["pd_unit"],
                    }.get(unit, out["indicator_report_unit"])
                    unit_value = of(r)
                    if stage is not None:  # a step's CSV: once per unit it leaves out entirely
                        whole_ir, whole_pr = last_irs[ir] == stage, last_prs[pr] == stage
                        out["counts_once_indicator_report"] = int(whole_ir and _first(seen["ir"], ir))
                        out["counts_once_progress_report"] = int(whole_pr and _first(seen["pr"], pr))
                        out["counts_once"] = out["counts_once_indicator_report"]
                        out["unit_partly_kept"] = _yes(not whole_ir)
                        out["progress_report_partly_kept"] = _yes(not whole_pr)
                    else:
                        out["counts_once"] = int(unit_value is not None and _first(seen["unit"], unit_value))
                        out["counts_once_indicator_report"] = int(_first(seen["ir"], ir))
                        out["counts_once_progress_report"] = int(_first(seen["pr"], pr))
                        out["unit_partly_kept"] = out["progress_report_partly_kept"] = ""
                    # once per partner among the reports of the statuses chosen, and once among the others: a
                    # sum over the rows of report_status_chosen "yes" is the page's Partners
                    out["counts_once_partner"] = int(
                        partner is not None and _first(seen["partner"], (report.chosen, partner))
                    )
                    if report is not None:
                        out.update(_report_columns(report, r, names))
                    if what == "status":
                        own = dmq._own_names(catalogue.scrub(row.data if isinstance(row.data, dict) else {}))
                        text = raw_people.column(SOURCE, "report_status", row.report_status, names, own)
                        out["status_word_of_this_row"] = text
                    yield out
    except DatabaseError:
        logger.warning("figure trail: the download of %s stopped (took too long)", what, exc_info=True)
        yield {"figures_as_of": raw.STOPPED}


def _run():
    """The run the partner reports in NeuroDB come from (``updated_by_last_sync``)."""
    from . import runfacts

    return runfacts.runs([SOURCE])[SOURCE][0]


def trail_reason(found: trail.Trail, stage: str, why: str) -> str:
    """Why a row is left out, in the words of its step."""
    step = next((s for s in found.steps if s.key == stage), None)
    if step is None:
        return ""
    if stage == "pd_status" and why:
        return next((x.words.split(":")[0] for x in step.lines if x.what == f"reason:{why}"), step.title)
    return step.title


def _first(seen: set, value: Any) -> bool:
    """True the first time ``value`` is seen (a seen-set, never the rows' order)."""
    if value in seen:
        return False
    seen.add(value)
    return True


def _partner_unit(partner: Any, names: frozenset[str]) -> str:
    if partner is None:
        return ""
    if isinstance(partner, int):
        return f"partner:{partner}"
    return "name:" + str(raw_people.text(partner, SOURCE, "partner_name", names, frozenset()) or "")


def _pd_unit(key: str, names: frozenset[str]) -> str:
    if not key.startswith("ref:"):
        return key
    return "ref:" + str(raw_people.text(key[4:], SOURCE, "pd_reference_number", names, frozenset()) or "")


def _report_columns(report: trail.Report, r: tuple, names: frozenset[str]) -> dict[str, Any]:
    clean = lambda column, value: raw_people.text(value, SOURCE, column, names, frozenset())  # noqa: E731
    return {
        "report_status_neurodb": rc.STATUS_LABELS.get(report.status, report.status),
        "report_status_chosen": _yes(report.chosen),
        "status_word_used": clean("report_status", report.word),
        "status_word_from_record_id": report.word_from if report.word_from is not None else "",
        "status_rows_outside_filter": report.rows_outside,
        # the section its progress report is counted under in the table by section (reporting.report_section)
        "section_counted": clean("section", report.section),
        "period_end_month": report.month,
        "submitted_late": _yes(report.late),
        "partner_has_reported": _yes(report.reported) if report.partner is not None else "",
    }


# ---------------------------------------------------------------------------- PRP's own reports still owed
OWED_HEAD = (
    "figures_as_of",
    "step",
    "reason",
    "progress_report_key",
    "unit_key",
    "counts_once",
    "kept_by_the_filter",
    "report_status_neurodb",
    "report_status_chosen",
    "section_counted",
    "period_end_month",
    "partner_unit",
    "pd_unit",
)


def document_pks(found: trail.Trail) -> list[int]:
    """The records of PRP's reports still owed the trail read (every one before the filter), in order."""
    return sorted(found.rows.documents)


def document_columns(names: frozenset[str], pks: list[int]) -> tuple[list[str], list[str]]:
    """The columns of the owed reports' CSV, and the record fields among them (``raw.header_keys``)."""
    counted, _left_out = raw.field_counts(owed.SOURCE, names)
    if pks:
        keys = raw.header_keys(owed.SOURCE, dm.DatamartDocument.objects.filter(pk__in=pks), counted)
    else:
        keys = [f.key for f in counted]
    return [*OWED_HEAD, *raw.csv_columns(owed.SOURCE, keys)[1:]], keys


def document_rows(
    found: trail.Trail, pks: list[int], keys: list[str], names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    """One row per report still owed from PRP's own list the trail read: whether the filter keeps it (and the
    step that left it out), its status and section as the page counts them, then its record through the people
    rule (``raw.csv_record``), read 2,000 at a time inside one snapshot."""
    rows = found.rows
    source = owed.SOURCE
    run = runfacts.runs([source])[source][0]
    reviewed = raw.reviewed_keys(source)
    wanted = set(keys)
    as_of = raw.local_moment(found.as_of)
    stages = dict(enumerate(rows.stages))
    seen: set = set()
    try:
        with raw.snapshot(raw.STREAM_TIMEOUT_MS):
            for start in range(0, len(pks), raw.CSV_CHUNK):
                chunk = pks[start : start + raw.CSV_CHUNK]
                by_pk = {
                    d.pk: d for d in raw.related(source, dm.DatamartDocument.objects.filter(pk__in=chunk))
                }
                for pk in chunk:
                    record = by_pk.get(pk)
                    if record is None:  # removed since the trail was read
                        continue
                    r = rows.documents[pk]
                    kept = r[3] == rows.kept_at
                    report = rows.reports.get(r[1]) if kept else None
                    out = raw.csv_record(source, record, wanted, run, names, reviewed, as_of)
                    out.update(
                        step="PRP's own reports still owed",
                        reason="" if kept else trail_reason(found, stages.get(r[3], ""), ""),
                        progress_report_key=r[1],
                        unit_key=r[1],
                        counts_once=int(kept and report is not None and report.chosen and _first(seen, r[1])),
                        kept_by_the_filter=_yes(kept),
                        report_status_neurodb=rc.STATUS_LABELS.get(report.status, "") if report else "",
                        report_status_chosen=_yes(report.chosen) if report else "",
                        section_counted=report.section if report else "",
                        period_end_month=report.month if report else "",
                        partner_unit=_partner_unit(report.partner, names) if report else "",
                        pd_unit=report.pd if report else "",
                    )
                    yield out
    except DatabaseError:
        logger.warning("figure trail: the download of PRP's reports still owed stopped", exc_info=True)
        yield {"figures_as_of": raw.STOPPED}


# ------------------------------------------------------------------------------------------ progress reports
# the texts of a progress report's row and of a tab's line (the exports' rows: views._report_row and
# _location_row), each cleaned as the partner reports' column it comes from, as the kept rows' CSV cleans them
REPORT_TEXTS = {
    "programme_document": "pd_reference_number",
    "partner": "partner_name",
    "section": "section",
    "cp_output": "cp_output",
    "indicator": "indicator",
    "report": "report_number",
    "location": "location",
    "progress_report_key": "progress_report_key",
}


def cleaned(row: dict[str, Any], texts: dict[str, str], source: str, names: frozenset[str]) -> dict[str, Any]:
    """``row`` with each of its ``texts`` (its column -> the record's column it comes from) cleaned by the
    people rule (``raw_people.text``): a name, an e-mail address or a phone in it is withheld."""
    out = dict(row)
    for column, name in texts.items():
        if column in out:
            out[column] = raw_people.text(out[column], source, name, names, frozenset())
    return out


def report_rows(
    found: trail.Trail, what: str, params: Any, names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    """One row per progress report of the table (``reports``) or of the reports to follow up
    (``follow_up``), as the page lists them, with the comparison sheet's match key; people never written."""
    from neurodb.reports.views import _report_row

    from . import services
    from .comparison import _vendor_numbers

    with raw.snapshot(raw.STREAM_TIMEOUT_MS):
        data = services.partner_reporting(params, found.day, options=False)
        reports = data["reports"] if what == "reports" else data["partners"]["follow_up"]
        vendors = _vendor_numbers({r["partner_id"] for r in reports})
        rows = []
        for r in reports:
            row = cleaned({**_report_row(r), "progress_report_key": r["key"]}, REPORT_TEXTS, SOURCE, names)
            rows.append(
                {
                    **row,
                    "figures_as_of": raw.local_moment(found.as_of),
                    "step": "The table's progress reports" if what == "reports" else "Reports to follow up",
                    "reason": "",
                    "vendor_number": vendors.get(r["partner_id"], ""),
                    "match_key": f"{row['programme_document']} {row['report']}",
                }
            )
    yield from rows


# ------------------------------------------------------------------------------------------------ the lines
def line_columns(tab: str) -> list[str]:
    from neurodb.reports.views import DISAGGREGATION_BLOCK_COLUMNS, LOCATION_COLUMNS

    head = ["figures_as_of", "step", "reason"]
    if tab == "locations":
        return [*head, *LOCATION_COLUMNS, *LINE_TAIL]
    return [*head, *DISAGGREGATION_BLOCK_COLUMNS, "location_total", *LINE_TAIL]


def line_rows(tab: str, params: Any, day, as_of, names: frozenset[str]) -> Iterator[dict[str, Any]]:
    """By location's lines or By disaggregation's location blocks, computed as the tab computes them, with
    their records (``record``); people never written."""
    from neurodb.reports.views import DISAGGREGATION_BLOCK_COLUMNS, _location_row

    with raw.snapshot(raw.STREAM_TIMEOUT_MS):
        found, _recorder = trail.table_lines(tab, params, day)
    head = {
        "figures_as_of": raw.local_moment(as_of),
        "step": "By location: lines" if tab == "locations" else "By disaggregation: location blocks",
        "reason": "",
    }
    for item in found["rows"] if tab == "locations" else found["blocks"]:
        tail = {
            "progress_report_key": item["report_key"],
            "record_ids": " ".join(str(i) for i in item.get("record_ids", [])),
            "chosen_record_id": item.get("chosen_record_id"),
        }
        if tab == "locations":
            line = {**head, **_location_row(item), **tail}
        else:
            line = {
                **head,
                **{k: item.get(k) for k in DISAGGREGATION_BLOCK_COLUMNS},
                "location_total": item["total"],
                **tail,
            }
        yield cleaned(line, REPORT_TEXTS, SOURCE, names)


# ----------------------------------------------------------------------------------------- Partner monitoring
# the CSVs of the Partner monitoring trail (datamart.trail_monitoring): indicators, programme documents, PD
# indicator rows, partner report rows and the map's places, each starting with figures_as_of, step and reason
TEST_HEADERS = {name: name.replace("tracking.", "status_") for name in tm.TEST_COLUMNS}
INDICATOR_HEAD = (
    "figures_as_of",
    "step",
    "reason",
    "indicator_key",
    "unit_key",
    "counts_once",
    "kept_by_the_filter",
    *TEST_HEADERS.values(),
)
PD_COLUMNS = (
    "figures_as_of",
    "step",
    "reason",
    "pd_id",
    "unit_key",
    "counts_once",
    "partner_unit",
    "counts_once_partner",
    "number",
    "title",
    "partner_in_neurodb",
    "vendor_number",
    "status",
    "document_type",
    "start",
    "end",
    "sections",
    "offices",
    "unicef_focal_points (people)",
    "pd_indicator_rows",
    "indicators_kept",
)
PD_ROW_HEAD = ("figures_as_of", "step", "reason", "indicator_key", "unit_key", "counts_once")
DOCUMENT_HEAD = ("figures_as_of", "step", "reason")
REPORT_ROW_HEAD = (
    "figures_as_of",
    "step",
    "reason",
    "progress_report_unit",
    "indicator_report_unit",
    "counts_once_progress_report",
    "counts_once_indicator_report",
    "placed_to",
    "how",
    "counted",
)
PLACE_COLUMNS = (
    "figures_as_of",
    "step",
    "reason",
    "location_key",
    "unit_key",
    "counts_once",
    "location",
    "p_code",
    "on_the_map",
    "approximate",
    "pd_id",
    "programme_document",
    "indicator_key",
    "tracking",
    "planned",
    "reported",
)


@dataclass(frozen=True)
class Selection:
    """What a Partner monitoring CSV holds: its ``kind`` (``indicators``, ``pds``, ``pd_rows``,
    ``report_rows``, ``places`` or ``documents``: every record of a source NeuroDB keeps whole), its records
    (idents, PD ids, pks, the source), its step and its reason in words."""

    kind: str
    items: tuple
    step: str
    reason: str = ""
    unit: str = (
        ""  # what counts_once counts, when not the record (``title``: a title of one programme document)
    )


def _stage_index(rows: tm.Chain, key: str) -> tuple[int, str] | None:
    return next(((i, kind) for i, (kind, name) in enumerate(rows.stages) if name == key), None)


def kept_of(found: Any) -> tuple[tuple[int, str], ...]:
    """The indicators a trail keeps: Program Performance's are those of the sections chosen."""
    kept = getattr(found, "kept", None)
    return found.rows.kept if kept is None else kept


def indicator_key(rows: tm.Chain, ident: tuple[int, str]) -> str:
    """An indicator as its CSVs name it: its programme document and its key (its page's address: its eTools
    id, else ``r`` and a Datamart id); an indicator the chain does not hold, by its eTools id (``s``) or
    title (``t``)."""
    found = rows.indicators.get(ident)
    return f"{ident[0]}|{found.key if found is not None else ident[1]}"


def pm_selection(found: Any, what: str) -> Selection | None:
    """The records of a CSV of a Partner monitoring trail (``found``: the page's trail, or Program
    Performance's or the map's) for ``what``; None for an address the trail does not know."""
    rows = found.rows
    if what == "kept":
        return Selection("indicators", kept_of(found), "Kept: the page's indicators")
    if what == "step:sections" and getattr(found, "kept", None) is not None:
        chosen = set(found.kept)
        idents = tuple(i for i in rows.kept if i not in chosen)
        return Selection("indicators", idents, "Left out: not in the sections chosen")
    if what == "no_partner":
        pds = sorted({i.pd for i in rows.kept_indicators if not rows.pds[i.pd][2]})
        return Selection(
            "pds", tuple(pds), "Programme documents with no linked partner", "they add no partner"
        )
    if what.startswith("step:"):
        return _step_selection(found, what[5:])
    if what.startswith("figure:"):
        figure = found.figure(what[7:])
        if figure is None or not figure.select:
            return None
        return _figure_selection(found, figure)
    if what == "reached:all" and isinstance(found, tm.MonitoringTrail):
        return Selection(
            "report_rows", tuple(tm.reached_rows(rows.fold, "all")), "Partner report rows in NeuroDB"
        )
    if what == "reached:totals" and isinstance(found, tm.MonitoringTrail):
        return Selection("documents", (tm.TOTALS,), "PRP indicator reports in NeuroDB")
    if what.startswith("reached:") and isinstance(found, tm.MonitoringTrail):
        step = found.step(what[8:])
        if step is None:
            return None
        return Selection("report_rows", tuple(tm.reached_rows(rows.fold, step.key)), step.title)
    if what.startswith("overdue:") and isinstance(found, tm.MonitoringTrail):
        step = found.step(what[8:])
        if step is None:
            return None
        stage = int(step.key[1:]) - 1
        pks = sorted(pk for pk, s in rows.overdue.items() if (s >= 0 if stage == 0 else s == stage))
        return Selection("report_rows", tuple(pks), step.title)
    return None


def _step_selection(found: Any, key: str) -> Selection | None:
    rows = found.rows
    step = next((s for s in found.steps if s.key == key), None)
    if step is None:
        return None
    title = step.title
    if key == "M1":
        return Selection("pds", tuple(sorted(rows.pds)), title)
    if key == "K2":
        none = sorted(rows.pds_kept - {i.pd for i in rows.kept_indicators})
        return Selection("pds", tuple(none), title)
    if key == "M11":
        return Selection("pd_rows", tuple(sorted(rows.rows)), title)
    if key == "D4":
        return Selection("pd_rows", ("all",), title)
    if key == "D5":
        return Selection("pd_rows", ("orphans",), title)
    found_at = _stage_index(rows, key)
    if found_at is None:
        return None
    index, kind = found_at
    if kind == "pd":
        pds = sorted(pk for pk, f in rows.pds.items() if f[0] == index)
        return Selection("pds", tuple(pds), f"Left out: {title}")
    if kind == "rows":
        pks = sorted(pk for pk, r in rows.rows.items() if r[1] == index)
        return Selection("pd_rows", tuple(pks), f"Left out: {title}")
    idents = tuple(i for i, k in rows.indicators.items() if k.stage == index)
    return Selection("indicators", idents, f"Left out: {title}")


def _figure_selection(found: Any, figure: Any) -> Selection | None:
    rows = found.rows
    every = getattr(found, "kept", None) is not None and figure.select[0][0] == "section"  # the tiles' own
    by_ident = {i: rows.indicators[i] for i in (rows.kept if every else kept_of(found))}
    name, value = figure.select[0]
    step, reason = f"Figure: {figure.label}", figure.how

    def indicators(test: Callable[[Any], bool]) -> Selection:
        return Selection("indicators", tuple(i for i, k in by_ident.items() if test(k)), step, reason)

    def pds(test: Callable[[Any], bool]) -> Selection:
        return Selection("pds", tuple(sorted({k.pd for k in by_ident.values() if test(k)})), step, reason)

    groups: dict[tuple[int, str], list[Any]] = defaultdict(list)
    for k in by_ident.values():
        groups[(k.pd, rc.norm(k.title))].append(k)
    shared = {key for key, group in groups.items() if len(group) > 1}
    found_by = {
        ("set", "kept"): lambda: indicators(lambda k: True),
        ("set", "same_title"): lambda: dataclasses.replace(
            indicators(lambda k: (k.pd, rc.norm(k.title)) in shared), unit="title"
        ),
        ("set", "same_title_left_out"): lambda: indicators(
            lambda k: (k.pd, rc.norm(k.title)) in shared and not k.rows_placed and k.title_rows_left_out
        ),
        ("set", "pds"): lambda: pds(lambda k: True),
        ("set", "partners"): lambda: pds(lambda k: k.partner is not None),
        ("set", "overdue"): lambda: Selection(
            "report_rows", tuple(sorted(pk for pk, s in rows.overdue.items() if s == 3)), step, reason
        ),
        ("set", "map"): lambda: Selection("places", (), step, reason),
    }
    if (name, value) in found_by:
        return found_by[(name, value)]()
    if name == "test":
        return indicators(lambda k: value in k.tests)
    if name == "not_test":
        return indicators(lambda k: value not in k.tests)
    if name == "section":
        return indicators(lambda k: (k.section or monitoring.NO_SECTION) == value)
    if name == "group":
        return indicators(lambda k: (k.pd, rc.norm(k.title)) == tuple(value))
    if name == "pd_test":
        return pds(lambda k: value in k.tests)
    if name == "indicator":
        pks = sorted(pk for pk, p in rows.fold.placed.items() if p[1] == value)
        return Selection("report_rows", tuple(pks), step, reason)
    return None


def pm_count(selection: Selection) -> int:
    """The records a CSV would write (the PD indicator rows in NeuroDB, those naming no PD, or the records of
    a source, counted)."""
    if selection.kind == "documents":
        return raw.base(selection.items[0]).count()
    if selection.items in (("all",), ("orphans",)):
        rows = dm.PDIndicator.objects.all()
        return (rows if selection.items == ("all",) else rows.filter(intervention=None)).count()
    return len(selection.items)


def _source(selection: Selection) -> str:
    if selection.kind == "documents":
        return selection.items[0]
    return tm.REPORTS if selection.kind == "report_rows" else tm.SOURCE


def pm_limit(selection: Selection) -> tuple[str, int]:
    """The source a CSV's records come from, and how many a download holds (``raw.csv_limit``)."""
    source = _source(selection)
    return source, raw.csv_limit(source)


def pm_columns(selection: Selection, names: frozenset[str]) -> tuple[list[str], list[str]]:
    """The columns of a CSV, and the record fields among them (``raw.header_keys``)."""
    if selection.kind == "indicators":
        return [*INDICATOR_HEAD, *monitoring.TABLE_COLUMNS], []
    if selection.kind == "pds":
        return list(PD_COLUMNS), []
    if selection.kind == "places":
        return list(PLACE_COLUMNS), []
    source = _source(selection)
    counted, _left_out = raw.field_counts(source, names)
    if selection.kind == "documents":
        keys = raw.header_keys(source, raw.base(source), counted)
        return [*DOCUMENT_HEAD, *raw.csv_columns(source, keys)[1:]], keys
    model = dm.ReportedIndicator if selection.kind == "report_rows" else dm.PDIndicator
    if selection.items:
        keys = raw.header_keys(source, _pd_rows(selection, model), counted)
    else:  # no record: the fields NeuroDB counted
        keys = [f.key for f in counted]
    head = REPORT_ROW_HEAD if selection.kind == "report_rows" else PD_ROW_HEAD
    return [*head, *raw.csv_columns(source, keys)[1:]], keys


def _pd_rows(selection: Selection, model: Any) -> Any:
    if selection.items == ("all",):
        return model.objects.all()
    if selection.items == ("orphans",):
        return model.objects.filter(intervention=None)
    return model.objects.filter(pk__in=list(selection.items[: raw.HEADER_RECORDS]))


@dataclass(frozen=True)
class PageReads:
    """What a CSV of indicators or of the map's places reads of the page (each called only when needed): its
    indicators as stored (``rows``: their Program Performance columns, for ``filters``) and the map's places
    (``points``)."""

    filters: Any
    rows: Callable[[], list[Any]]
    points: Callable[[], dict[str, Any]]
    base_url: str = ""


def pm_rows(
    found: Any, selection: Selection, keys: list[str], names: frozenset[str], page: PageReads
) -> Iterator[dict[str, Any]]:
    """The rows of a Partner monitoring CSV, people never written (``raw.csv_record``; a programme document's
    focal points as a count)."""
    clean = lambda value: raw_people.text(value, tm.REPORTS, "partner_name", names, frozenset())  # noqa: E731
    head = {
        "figures_as_of": raw.local_moment(found_moment(found)),
        "step": clean(selection.step),
        "reason": clean(selection.reason),
    }
    if selection.kind == "indicators":
        yield from _indicator_rows(found.rows, selection, head, page, names)
    elif selection.kind == "pds":
        yield from _pd_csv_rows(found.rows, selection, head, names)
    elif selection.kind == "places":
        yield from _place_rows(page.points(), head)
    elif selection.kind == "pd_rows":
        yield from _pd_indicator_rows(found.rows, selection, head, keys, names)
    elif selection.kind == "documents":
        yield from _document_rows(selection.items[0], head, keys, names)
    else:
        yield from _report_rows(found.rows, selection, head, keys, names)


def found_moment(found: Any) -> Any:
    return getattr(found, "as_of", None) or tm.moment()


# the texts of an indicators CSV, each cleaned as the PD indicators' column it comes from (as the PD indicator
# rows' CSVs clean them)
INDICATOR_TEXTS = {
    "indicator": "title",
    "pd_output": "lower_result_name",
    "cp_output": "cp_output",
    "section": "section_name",
    "partner": "partner_name",
}


def _title_unit(pd: int, title: str) -> str:
    """A title of one programme document as a CSV's unit: the programme document and a digest of the title as
    Data quality groups it (``reached.norm``), never its words (a title can name a person or a contact)."""
    return f"{pd}|title-{hashlib.blake2b(rc.norm(title).encode(), digest_size=6).hexdigest()}"


def _indicator_rows(
    rows, selection, head, page: PageReads, names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    by_key = {(r.pd.id, r.key): r for r in page.rows()}
    seen: set = set()
    for ident in selection.items:
        k = rows.indicators[ident]
        row = by_key.get((k.pd, k.key)) if k.stage == rows.kept_at else None
        unit = _title_unit(k.pd, k.title) if selection.unit == "title" else indicator_key(rows, ident)
        out = {
            **head,
            "indicator_key": indicator_key(rows, ident),
            "unit_key": unit,
            "counts_once": int(_first(seen, unit)),
            "kept_by_the_filter": _yes(k.stage == rows.kept_at),
            **{column: _yes(name in k.tests) for name, column in TEST_HEADERS.items()},
        }
        if row is not None:
            out.update(next(monitoring.table_rows([row], page.filters, page.base_url)))
        else:
            out.update(
                section=k.section,
                cp_output=k.cp_output,
                pd_output=k.output,
                indicator=k.title,
                programme_document=k.pd_number,
            )
        yield cleaned(out, INDICATOR_TEXTS, tm.SOURCE, names)


def _pd_csv_rows(rows, selection, head, names) -> Iterator[dict[str, Any]]:
    kept = Counter(i.pd for i in rows.kept_indicators)
    seen: set = set()
    found = PCA.objects.filter(pk__in=list(selection.items)).select_related("partner").order_by("pk")
    for pd in found.iterator(chunk_size=raw.CSV_CHUNK):
        partner = pd.partner
        unit = f"partner:{pd.partner_id}" if pd.partner_id else ""
        yield {
            **head,
            "pd_id": pd.pk,
            "unit_key": f"pd:{pd.pk}",
            "counts_once": 1,
            "partner_unit": unit,
            "counts_once_partner": int(bool(unit) and _first(seen, unit)),
            "number": pd.number or "",
            "title": raw_people.text(pd.title or "", "interventions", "title", names, frozenset()),
            "partner_in_neurodb": partner.name if partner is not None else "",
            "vendor_number": partner.vendor_number if partner is not None else "",
            "status": pd.status or "",
            "document_type": pd.document_type or "",
            "start": pd.start,
            "end": pd.end,
            "sections": ", ".join(pd.section_names or []),
            "offices": ", ".join(pd.offices_set or []),
            "unicef_focal_points (people)": raw_people.people_count(pd.unicef_focal_points or []),
            "pd_indicator_rows": rows.pds.get(pd.pk, (0, "", None, 0))[3],
            "indicators_kept": kept.get(pd.pk, 0),
        }


def _place_rows(points: dict[str, Any], head: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for located, places in ((True, points.get("points", [])), (False, points.get("unlocated", []))):
        for place in places:
            for n, r in enumerate(place["rows"]):
                yield {
                    **head,
                    "location_key": place["key"],
                    "unit_key": place["key"],
                    "counts_once": int(n == 0),
                    "location": place.get("name") or "",
                    "p_code": place.get("p_code") or "",
                    "on_the_map": _yes(located),
                    "approximate": _yes(bool(place.get("approximate"))),
                    "pd_id": r["pd_id"],
                    "programme_document": r["pd"] or "",
                    "indicator_key": f"{r['pd_id']}|{r['key']}",  # as the indicators' CSV names it
                    "tracking": monitoring.LABELS.get(r["tracking"], r["tracking"]),
                    "planned": _yes(r["planned"]),
                    "reported": _yes(r["reported"]),
                }


def _chunks(selection: Selection, model: Any) -> Iterator[list[Any]]:
    """The records of a CSV, :data:`raw.CSV_CHUNK` at a time, with their partner and programme document."""
    source = tm.REPORTS if model is dm.ReportedIndicator else tm.SOURCE
    if selection.items in (("all",), ("orphans",)):
        rows = raw.related(source, _pd_rows(selection, model)).order_by("pk")
        chunk: list[Any] = []
        for row in rows.iterator(chunk_size=raw.CSV_CHUNK):
            chunk.append(row)
            if len(chunk) == raw.CSV_CHUNK:
                yield chunk
                chunk = []
        if chunk:
            yield chunk
        return
    pks = list(selection.items)
    for start in range(0, len(pks), raw.CSV_CHUNK):
        part = pks[start : start + raw.CSV_CHUNK]
        by_pk = {row.pk: row for row in raw.related(source, model.objects.filter(pk__in=part))}
        yield [by_pk[pk] for pk in part if pk in by_pk]  # a record removed since the trail was read: skipped


def _pd_indicator_rows(rows, selection, head, keys, names) -> Iterator[dict[str, Any]]:
    run = runfacts.runs([tm.SOURCE])[tm.SOURCE][0]
    reviewed = raw.reviewed_keys(tm.SOURCE)
    wanted = set(keys)
    seen: set = set()
    try:
        with raw.snapshot(raw.STREAM_TIMEOUT_MS):
            for chunk in _chunks(selection, dm.PDIndicator):
                for row in chunk:
                    ident = monitoring.ident_of(row.intervention_id, row.source_id, row.title)
                    key = indicator_key(rows, ident) if row.intervention_id else ""
                    if ident not in rows.indicators and ident[1].startswith("t"):  # by its title
                        key = raw_people.text(key, tm.SOURCE, "title", names, frozenset())
                    record = raw.csv_record(
                        tm.SOURCE, row, wanted, run, names, reviewed, head["figures_as_of"]
                    )
                    yield {
                        **record,
                        **head,
                        "indicator_key": key,
                        "unit_key": key,
                        "counts_once": int(bool(key) and _first(seen, key)),
                    }
    except DatabaseError:
        logger.warning(
            "figure trail: the download of %s stopped (took too long)", selection.step, exc_info=True
        )
        yield {"figures_as_of": raw.STOPPED}


def _document_rows(source: str, head, keys, names) -> Iterator[dict[str, Any]]:
    """Every record of a source NeuroDB keeps whole (PRP's report totals), as Raw eTools data writes it."""
    run = runfacts.runs([source])[source][0]
    reviewed = raw.reviewed_keys(source)
    wanted = set(keys)
    try:
        with raw.snapshot(raw.STREAM_TIMEOUT_MS):
            rows = raw.related(source, raw.base(source)).order_by("pk")
            for row in rows.iterator(chunk_size=raw.CSV_CHUNK):
                yield {
                    **raw.csv_record(source, row, wanted, run, names, reviewed, head["figures_as_of"]),
                    **head,
                }
    except DatabaseError:
        logger.warning("figure trail: the download of %s stopped (took too long)", source, exc_info=True)
        yield {"figures_as_of": raw.STOPPED}


def _report_rows(rows, selection, head, keys, names) -> Iterator[dict[str, Any]]:
    run = runfacts.runs([tm.REPORTS])[tm.REPORTS][0]
    reviewed = raw.reviewed_keys(tm.REPORTS)
    wanted = set(keys)
    fold = rows.fold
    seen: dict[str, set] = {"pr": set(), "ir": set()}
    try:
        with raw.snapshot(raw.STREAM_TIMEOUT_MS):
            for chunk in _chunks(selection, dm.ReportedIndicator):
                for row in chunk:
                    record = raw.csv_record(
                        tm.REPORTS, row, wanted, run, names, reviewed, head["figures_as_of"]
                    )
                    report, ir = record["progress_report_key"], record["indicator_report_key"]
                    placed = fold.placed.get(row.pk)
                    to, how, counted = "", "", ""
                    if placed is not None:
                        if placed[1] >= 0:
                            to = indicator_key(rows, rows.kept[placed[1]])
                        how = tm._way_words(placed[2])
                        if placed[3]:
                            how += "; " + tm.outcome_words(placed[4])
                        counted = _yes(placed[4].startswith("counted:"))
                    yield {
                        **record,
                        **head,
                        "progress_report_unit": report,
                        "indicator_report_unit": f"{report}#{ir}",
                        "counts_once_progress_report": int(_first(seen["pr"], report)),
                        "counts_once_indicator_report": int(_first(seen["ir"], (report, ir))),
                        "placed_to": to,
                        "how": how,
                        "counted": counted,
                    }
    except DatabaseError:
        logger.warning(
            "figure trail: the download of %s stopped (took too long)", selection.step, exc_info=True
        )
        yield {"figures_as_of": raw.STOPPED}
