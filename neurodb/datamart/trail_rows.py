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

Every CSV streams (``exports.stream_csv``) inside one read-only snapshot, 2,000 records at a time, takes one
of the site's two large download slots and holds at most ``raw.csv_limit("partner_reports")`` records.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from django.db import DatabaseError

from . import catalogue, raw, raw_people, reporting, trail
from . import models as dm
from . import query as dmq
from . import reached as rc

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
    "match_key",
)
LINE_TAIL = ("progress_report_key", "record_ids", "chosen_record_id")


def _yes(value: bool) -> str:
    return "yes" if value else "no"


# ----------------------------------------------------------------------------------------- what a CSV holds
def kind_of(what: str) -> str:
    """``rows`` (report rows), ``reports`` (one row per progress report) or ``lines`` (a tab's lines or
    blocks); "" for an address the trail does not know."""
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
        "section_counted": clean("section", report.sections.get(r[2], "")),
        "period_end_month": report.month,
        "submitted_late": _yes(report.late),
        "partner_has_reported": _yes(report.reported) if report.partner is not None else "",
    }


# ------------------------------------------------------------------------------------------ progress reports
def report_rows(found: trail.Trail, what: str, params: Any) -> Iterator[dict[str, Any]]:
    """One row per progress report of the table (``reports``) or of the reports to follow up
    (``follow_up``), as the page lists them, with the comparison sheet's match key."""
    from neurodb.reports.views import _report_row

    from . import services
    from .comparison import _vendor_numbers

    with raw.snapshot(raw.STREAM_TIMEOUT_MS):
        data = services.partner_reporting(params, found.day, options=False)
        reports = data["reports"] if what == "reports" else data["partners"]["follow_up"]
        vendors = _vendor_numbers({r["partner_id"] for r in reports})
        rows = []
        for r in reports:
            row = _report_row(r)
            rows.append(
                {
                    **row,
                    "figures_as_of": raw.local_moment(found.as_of),
                    "step": "The table's progress reports" if what == "reports" else "Reports to follow up",
                    "reason": "",
                    "progress_report_key": r["key"],
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


def line_rows(tab: str, params: Any, day, as_of) -> Iterator[dict[str, Any]]:
    """By location's lines or By disaggregation's location blocks, computed as the tab computes them, with
    their records (``record``)."""
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
            yield {**head, **_location_row(item), **tail}
        else:
            yield {
                **head,
                **{k: item.get(k) for k in DISAGGREGATION_BLOCK_COLUMNS},
                "location_total": item["total"],
                **tail,
            }
