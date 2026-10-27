"""The figure trail of Partner monitoring, its map and Home's Indicator progress block (datamart.trail_monitoring,
SPEC_validation §3.4.3-3.4.6, stage 4): under every filter, each figure the page draws is the page's own (its parts'
context and their ``data-figure``), rebuilt from the indicators the steps keep it is the same, and the Kept
indicators CSV and its own CSV give it again; the chain of steps adds up, in programme documents, PD indicator rows
and indicators, and the Reached fold in report rows and indicator reports; the hooks change no SQL and no figure; a
cold trail is the page's one build."""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the partner monitoring tests)
from __future__ import annotations

import csv
import datetime
import io
import re
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.http import QueryDict
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.core import compute
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring, trail, trail_rows
from neurodb.datamart import trail_monitoring as tm
from neurodb.partnerships.models import PCA
from tests.reports.test_figure_trail import drawn
from tests.reports.test_pd_monitoring import TODAY, data, report_rows
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import get_part, text
from tests.web.test_plain_words import NEVER

pytestmark = pytest.mark.django_db
PM = "partner-monitoring"
TRAIL_GRID = "reports:pd_monitoring"
PARTNER = "{partner}"  # the partner's id, filled in by the test
CAREGIVERS = "# of caregivers trained"
FILTERS = [
    {"year": "2026"},  # the grid's default, as its check line writes it
    {"view": "table", "year": "all"},
    {"view": "quality", "year": "2026"},
    {"view": "quality", "year": "all"},
    {"year": "2026", "status": "on_track"},
    {"year": "2026", "report_type": "HR"},
    {"year": "2026", "pd_status": ""},
    {"year": "2026", "scope": "year"},
    {"year": "2026", "partner": PARTNER},
    {"year": "2026", "q": "girls"},
    {"year": "2026", "cp_output": "psychosocial"},
    {"year": "2026", "location": "Bekaa"},
    {"view": "table", "year": "all", "section": "Education"},
    {"year": "2026", "scope": "year", "section": ""},  # Home's filter
]
PARTS = {  # each view's parts, and the blocks of the trail they draw
    "grid": ("summary", "grid"),
    "table": ("sections", "table"),
    "quality": ("quality-tiles", "quality-lists"),
}


@pytest.fixture
def world(more, frozen):
    """The partner monitoring world (``more``), plus: a running programme document with no PD indicator row,
    one with no linked partner whose only indicator a search for girls leaves out, two indicators of one
    programme document sharing a title (a row placed by its PD output, one whose output matches neither), a row
    whose title no indicator has, an overdue report, PRP's own total of one report, and the syncs."""
    from neurodb.core.models import SyncRun
    from neurodb.datamart import keys

    pd, partner, d = more["pd"], more["partner"], datetime.date
    PCA.objects.create(
        etl_id="14", partner=partner, number="LEB/PD4", title="Empty", status="active", start=d(2026, 1, 1)
    )
    shelter = PCA.objects.create(
        etl_id="15", partner_name="Unlinked", number="LEB/PD5", title="Shelter", status="signed"
    )
    dm.PDIndicator.objects.create(
        datamart_id=40, source_id=800, intervention=shelter, title="# of tents", section_name="Shelter"
    )
    for n, output in ((41, "3.1 Parenting"), (42, "3.2 Caregiving")):
        dm.PDIndicator.objects.create(
            datamart_id=n,
            source_id=900 + n,
            intervention=pd,
            title=CAREGIVERS,
            section_name="Child Protection",
            lower_result_name=output,
            target_numerator=100,
            location_name="Akkar",
        )
    end = d(2026, 3, 31)
    report_rows(
        pd, partner, CAREGIVERS, "PR-1", "QPR1", "QPR", end, {"Akkar": 7}, 7, pd_output="3.1 Parenting"
    )
    report_rows(
        pd, partner, CAREGIVERS, "PR-1", "QPR1", "QPR", end, {"Zahle": 2}, 2, pd_output="9.9 Elsewhere"
    )
    report_rows(pd, partner, "# of something no indicator has", "PR-1", "QPR1", "QPR", end, {"Akkar": 5}, 5)
    overdue = report_rows(pd, partner, more["boys"], "PR-6", "QPR6", "QPR", d(2026, 5, 31), {"Akkar": 1}, 1)
    dm.ReportedIndicator.objects.filter(pk__in=[r.pk for r in overdue]).update(
        report_status="", submission_date=None
    )
    boys_q1 = dm.ReportedIndicator.objects.filter(indicator=more["boys"], progress_report="PR-1")
    boys_q1.update(indicator_report="5001")
    dm.DatamartDocument.objects.create(
        dataset="prp_indicator_reports",
        record_key="5001",
        source_id=5001,
        intervention=pd,
        data={"progress_value": "20", "performance_indicator": more["boys"]},
    )
    for target in ("partner_reports", "pd_indicators"):
        run = SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_DATAMART, target=target, status=SyncRun.Status.RUNNING
        )
        run.finish(SyncRun.Status.SUCCEEDED)
    compute.clear()
    return {**more, "shelter": shelter}


@pytest.fixture
def validator(client, roles):
    user = User.objects.create_user(username="checker", password="pass-123456-x")
    user.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    client.force_login(user)
    return client


def _params(params: dict, world) -> dict:
    out = {k: str(world["partner"].pk) if v == PARTNER else v for k, v in params.items()}
    out.setdefault("section", "")  # the check line writes every section out
    return out


def _query(params: dict) -> str:
    query = QueryDict(mutable=True)
    for key, value in params.items():
        query.setlist(key, value if isinstance(value, list) else [value])
    return query.urlencode()


def _csv(client, params: dict, what: str, page: str = PM, **extra) -> list[dict[str, str]]:
    response = client.get(reverse("reports:check_rows", args=[page]), {**params, "what": what, **extra})
    assert response.status_code == 200, (what, response.content[:300])
    return list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))


def _view(params: dict) -> str:
    return params.get("view", "grid")


def _page_figures(client, params: dict) -> tuple[dict[str, int | None], dict[str, object]]:
    """The figures the view's parts draw (their data-figure) and their context."""
    url = reverse("reports:pd_monitoring") + "?" + _query(params)
    shown: dict[str, int | None] = {}
    context: dict[str, object] = {}
    for name in PARTS[_view(params)]:
        part = get_part(client, url, name)
        shown.update(drawn(text(part)))
        context[name] = part.context
    return shown, context


def _drawn_keys(blocks, view: str, listed: set[str]) -> set[str]:
    keys = set()
    for block in blocks:
        if block.tab != view:
            continue
        for f in block.figures:
            if f.listed:
                if f.key in listed:
                    keys.add(f.key)
            elif f.drawn:
                keys.add(f.key)
    return keys


def _number(value) -> int | None:
    """A figure as the page writes it (``number:0``): its first whole number."""
    if value is None:
        return None
    return int(round(value)) if isinstance(value, float) else value


# --------------------------------------------------------------------------------------------- every figure
@pytest.mark.parametrize("params", FILTERS)
def test_each_figure_is_the_page_s_rebuilt_and_in_its_csv(validator, world, params):
    params = _params(params, world)
    view = _view(params)
    found = tm.monitoring_trail(params, TODAY)
    shown, context = _page_figures(validator, params)
    # (c) every figure rebuilt from the indicators kept is the page's
    assert not found.differences, [(f.key, f.page, f.rebuilt) for f in found.differences]
    figures = {f.key: f for f in found.figures}
    if view == "table":
        table = tm.performance_trail(params, TODAY)
        assert not [f for f in table.block.figures if f.page != f.rebuilt]
        figures.update({f.key: f for f in table.block.figures})
        blocks = [table.block]
        listed = {f"pp.reached.{r.pd.id}.{r.key}" for r in context["table"]["page_obj"]}
        tiles = context["sections"]["section_tiles"]
        assert {t["name"]: t["count"] for t in tiles} == {
            k.removeprefix("pp.section_tile."): f.page
            for k, f in figures.items()
            if k.startswith("pp.section_tile.")
        }
        assert figures["pp.rows"].page == len(context["table"]["rows"])
    else:
        blocks = found.blocks
        listed = set()
        if view == "quality":
            q = context["quality-tiles"]["quality"]
            listed = {
                f"dq.same_title.{g['figure']}.{n}" for g in q["same_title"] for n in ("count", "left_out")
            }
            for key, name in (
                ("dq.pds", "programme_documents"),
                ("dq.running", "running_pds"),
                ("dq.without_reached", "without_reached"),
                ("dq.zero_reached", "zero_reached"),
                ("dq.list.no_reached", "no_reached_count"),
                ("dq.list.same_title", "same_title_count"),
            ):
                assert figures[key].page == q[name], key  # (a) the page's own figure
        else:
            summary = context["summary"]["data"]
            assert figures["pm.indicators"].page == summary["indicators"]
            assert figures["pm.overdue_reports"].page == summary["overdue_reports"]
            for key, n in summary["status_counts"].items():
                assert figures[f"pm.status.{key}"].page == n, key
            assert figures["pm.listed"].page == len(context["grid"]["rows"])
    # (b) the figures the view draws: every one is the trail's, and the view draws every figure of its blocks
    expected = _drawn_keys(blocks, view, listed)
    assert set(shown) == expected, set(shown) ^ expected
    for key, value in shown.items():
        assert value == _number(figures[key].page), key
    # (d) the Kept indicators CSV gives each indicator figure again, by its yes/no column
    of = {"of": "performance"} if view == "table" else {}
    kept = _csv(validator, params, "kept", **of)
    assert sum(int(r["counts_once"]) for r in kept) == len(kept)
    if view != "table":
        assert len(kept) == figures["pm.indicators"].page
        for f in found.figures:
            if f.select and f.select[0][0] == "test":
                column = trail_rows.TEST_HEADERS[f.select[0][1]]
                assert sum(r[column] == "yes" for r in kept) == f.page, f.key
        # ... and each figure's own CSV, by counts_once (its unit: indicators, programme documents, partners)
        for f in found.figures:
            if not f.what:
                continue
            rows = _csv(validator, params, f.what)
            once = (
                "counts_once_partner"
                if f.unit == trail.PARTNER
                else ("counts_once_progress_report" if f.unit == trail.PR else "counts_once")
            )
            assert sum(int(r[once]) for r in rows) == f.page, f.key
    else:
        assert len(kept) == figures["pp.indicators"].page
    # (e) the chain adds up
    _adds_up(found.chain_steps)
    # the trail page answers, and every figure is the same
    page = validator.get(reverse("reports:check_figures", args=[PM]), params)
    assert page.status_code == 200
    part = get_part(validator, reverse("reports:check_figures", args=[PM]) + "?" + _query(params), "figures")
    assert "rebuilt from the indicators kept: the same." in text(part)


def _adds_up(stages) -> None:
    """Each step keeps what the step before it kept, less what it leaves out (in its unit)."""
    before = None
    for stage in stages:
        if before is not None and stage.unit == before.unit and stage.out is not None:
            assert stage.kept == before.kept - stage.out, stage.key
        before = stage


def test_the_steps_add_up_from_the_pd_indicator_rows_in_neurodb(validator, world):
    found = tm.monitoring_trail({"year": "2026", "q": "girls", "section": ""}, TODAY)
    steps = {s.key: s for s in found.steps}
    rows = found.rows
    # D4 - D5 - the programme documents' steps' rows = M11
    pd_rows = sum(s.rows_out for s in found.chain_steps if s.unit == trail.PD and s.out is not None)
    assert rows.held - rows.orphans - pd_rows == steps["M11"].kept
    assert steps["M1"].rows_kept == rows.held - rows.orphans
    assert not any(line.alert for s in found.steps for line in s.lines), [
        line.words for s in found.steps for line in s.lines if line.alert
    ]
    # K2: a running programme document with no PD indicator row, and one whose indicators the search leaves out
    k2 = steps["K2"]
    assert (
        k2.kept == 2
        and "No PD indicator row in NeuroDB: 1; every indicator left out by a later step: 1"
        in (k2.lines[0].words)
    )
    rows_k2 = _csv(validator, {"year": "2026", "q": "girls", "section": ""}, "step:K2")
    assert {r["number"] for r in rows_k2} == {"LEB/PD4", "LEB/PD5"}
    kept_pds = {f.page for f in found.figures if f.key == "pm.pds"}
    assert kept_pds == {1}  # LEB/PD1 alone: the two are in no programme documents' figure
    # the search: rows left out entirely and partly
    assert steps["q"].out > 0 and "left out entirely" in steps["q"].lines[0].words


def test_the_reached_fold_adds_up_and_says_why_rows_are_left_out(validator, world):
    found = tm.monitoring_trail({"year": "2026", "section": ""}, TODAY)
    fold = {s.key: s for s in found.reached}
    held = dm.ReportedIndicator.objects.count()
    # R0 to R3: every partner report row in NeuroDB, less what each step leaves out
    assert fold["R0"].kept == held - fold["R0"].out
    assert fold["R3"].kept == fold["R2"].kept == held - fold["R0"].out - fold["R1"].out - fold["R2"].out
    # R3 = placed to an indicator of the filter + placed to one it leaves out + left out
    assert fold["R3"].kept == fold["R4"].kept + fold["R5"].out + fold["R6"].out
    assert fold["R6"].title.startswith("Quarterly pass: left out, and why (")
    reasons = " ".join(line.words for line in fold["R6"].lines)
    assert "left out: no indicator of the PD has the title: 1 report row" in reasons
    assert (
        "left out: a title several indicators of the PD share, and a PD output none of them has: 1" in reasons
    )
    assert "by PD output 1" in fold["R4"].lines[0].words
    # the monthly pass: the girls' HR rows are read for the indicator with no quarterly report, and not used
    assert "read, not used) 2" in fold["R7"].lines[0].words and fold["R7"].added == 0
    # R10: the overdue report and the due one do not count; R12: PRP's own total found and kept
    assert "Overdue 1" in fold["R10"].lines[0].words and "Not yet due 1" in fold["R10"].lines[0].words
    assert "Found with a value for the rows these indicators read: 1; kept 1" in fold["R12"].lines[0].words
    assert fold["R14"].kept == found.figure("pm.with_reached").page
    # the rows of the used reports add up: left out by a report's status, with no value, or counted
    used = sum(1 for p in found.rows.fold.placed.values() if p[3])
    outcome = [p[4] for p in found.rows.fold.placed.values() if p[3]]
    assert used == len(outcome) and "unexplained" not in outcome
    # each step's CSV holds its rows, with where each went
    r6 = _csv(validator, {"year": "2026", "section": ""}, "reached:R6")
    assert len(r6) == fold["R6"].out and all(r["how"].startswith("left out:") for r in r6)
    r13 = _csv(validator, {"year": "2026", "section": ""}, "reached:R13")
    assert r13 and all(r["counted"] == "yes" and r["placed_to"] for r in r13)


REACHED_FILTERS = [
    {"year": "2026"},
    {"year": "2026", "status": "not_reported"},
    {"year": "2026", "status": "on_track"},
    {"year": "2026", "location": "Bekaa"},
    {"year": "2026", "report_type": "HR"},
]


@pytest.mark.parametrize("params", REACHED_FILTERS)
def test_every_step_of_the_reached_fold_is_the_one_before_less_what_it_leaves_out(validator, world, params):
    """SPEC_validation §3.4.4 and §3.8: one chain, in report rows and in indicator reports, from every row in
    NeuroDB to the reports that count, which are those of the indicators kept (the ones behind R14)."""
    params = _params(params, world)
    found = tm.monitoring_trail(params, TODAY)
    stages = [s for s in found.reached if isinstance(s, tm.Stage)]
    assert [s.key for s in stages if s.unit == trail.ROW][:4] == ["R0", "R1", "R2", "R3"]
    before = None
    for stage in stages:
        if stage.unit == trail.ROW:
            assert stage.irs_kept is not None, stage.key
        if before is not None and stage.unit == before.unit:
            assert stage.kept == before.kept - (stage.out or 0) + (stage.added or 0), stage.key
            assert stage.irs_kept == before.irs_kept - stage.irs_out + stage.irs_added, stage.key
        before = stage
    assert not [line.words for s in found.reached for line in s.lines if line.alert]
    fold = {s.key: s for s in found.reached}
    placed = found.rows.fold.placed.values()
    behind = {found.rows.kept[p[1]] for p in placed if p[1] >= 0 and p[4].startswith("counted:")}
    assert len(behind) == fold["R14"].kept == found.figure("pm.with_reached").page
    if params.get("status") == "not_reported":  # every indicator kept is without Reached
        assert fold["R4"].kept == fold["R13"].kept == fold["R13"].irs_kept == fold["R14"].kept == 0
    # a step's CSV holds the rows it leaves out, and their indicator reports, entirely or partly left out
    for stage in stages:
        if stage.what and stage.out and stage.added is None:
            rows = _csv(validator, params, stage.what)
            assert len(rows) == stage.out, stage.key
            units = {r["indicator_report_unit"] for r in rows}
            assert len(units) == stage.irs_out + stage.irs_partly, stage.key


def test_a_step_that_does_not_add_up_shows_a_red_line():
    def stage(key, kept, irs, out=None, irs_out=0):
        return tm.Stage(key, key, trail.ROW, kept=kept, out=out, irs_kept=irs, irs_out=irs_out)

    chained = tm.adds_up([stage("a", 10, 5), stage("b", 8, 4, out=2, irs_out=1), stage("c", 8, 4)])
    assert not [line for s in chained for line in s.lines]
    broken = tm.adds_up([stage("a", 10, 5), stage("b", 8, 5, out=1), stage("c", 8, 3), stage("d", 2, 1)])
    assert [s.key for s in broken if any(line.alert for line in s.lines)] == ["b", "c", "d"]
    assert trail.DIFFERS in broken[1].lines[0].words


def test_prp_s_report_totals_and_every_partner_report_row_have_their_dataset_steps(validator, world):
    params = {"year": "2026", "section": ""}
    found = tm.monitoring_trail(params, TODAY)
    keys = [s.key for s in found.reached]
    assert keys.index("R12-D1") < keys.index("R12-D4") < keys.index("R12") < keys.index("R13")
    d4 = found.step("R12-D4")
    assert d4.title == "PRP's report totals: what is in NeuroDB" and d4.what == "reached:totals"
    totals = _csv(validator, params, "reached:totals")
    assert [r["record_id"] for r in totals] == ["5001"] and totals[0]["progress_value"] == "20"
    # the partner reports' D4: every row in NeuroDB, as CSV
    (reports,) = [s for s in found.reached if s.key == "D4"]
    assert reports.what == "reached:all"
    rows = _csv(validator, params, "reached:all")
    assert len(rows) == dm.ReportedIndicator.objects.count() == found.step("R0").kept + found.step("R0").out


def test_a_step_holding_more_than_a_download_links_to_raw_etools_data(validator, world, monkeypatch):
    """SPEC_validation §1.3: above the limit, the trail says so instead of a link, with Raw eTools data."""
    from neurodb.datamart import raw

    monkeypatch.setitem(raw.CSV_LIMIT, "other", 3)
    monkeypatch.setitem(raw.CSV_LIMIT, "long_texts", 3)
    params = {"year": "2026", "section": "", "pd_status": ""}
    found = tm.monitoring_trail(params, TODAY)
    m11 = found.step("M11")
    assert m11.kept > 3 and not m11.what
    (line,) = [x for x in m11.lines if x.raw]
    assert line.raw == "pd_indicators" and line.words == raw.too_many("pd_indicators", m11.kept)
    r3 = found.step("R3")
    assert r3.kept > 3 and not r3.what and [x.raw for x in r3.lines] == ["partner_reports"]
    whats = []
    for step in (*found.steps, *found.reached, *found.overdue):
        whats += [step.what, *(x.what for x in step.lines)]
    links = [w for w in dict.fromkeys(whats) if w]
    assert links and "step:M11" not in links and "reached:R3" not in links
    for what in links:  # every link a step shows is a download that answers, of at most 3 records
        assert len(_csv(validator, params, what)) <= 3, what
    part = get_part(validator, reverse("reports:check_figures", args=[PM]) + "?" + _query(params), "figures")
    assert "what=step%3AM11" not in text(part) and line.words in text(part)
    assert "Raw data: ask for staff status" in text(part)  # the validator has no staff status


def test_the_same_title_rows_left_out_are_data_quality_s(validator, world):
    found = tm.monitoring_trail({"view": "quality", "year": "2026", "section": ""}, TODAY)
    unmatched = found.figure("dq.same_title_rows_unmatched")
    assert unmatched.page == unmatched.rebuilt == 1 and unmatched.drawn
    (group,) = [f for f in found.figures if f.key.startswith("dq.same_title.") and f.key.endswith(".count")]
    assert group.page == group.rebuilt == 2
    rows = _csv(validator, {"view": "quality", "year": "2026", "section": ""}, group.what)
    assert len(rows) == 2


def test_under_a_location_filter_the_rows_at_other_places_are_a_step(validator, world):
    found = tm.monitoring_trail({"year": "2026", "location": "Bekaa", "section": ""}, TODAY)
    r9 = found.step("R9")
    assert r9 is not None and r9.out > 0
    rows = _csv(validator, {"year": "2026", "location": "Bekaa", "section": ""}, "reached:R9")
    assert len(rows) == r9.out and all("location filter" in r["how"] for r in rows)
    assert found.step("R9") and tm.monitoring_trail({"year": "2026", "section": ""}, TODAY).step("R9") is None


def test_data_quality_with_a_year_reads_reached_in_that_year(validator, world):
    params = {"view": "quality", "year": "2025", "section": ""}
    found = tm.monitoring_trail(params, TODAY)
    tiles = get_part(validator, reverse("reports:pd_monitoring") + "?" + _query(params), "quality-tiles")
    q = tiles.context["quality"]
    assert (
        found.figure("dq.zero_reached").page == found.figure("dq.zero_reached").rebuilt == q["zero_reached"]
    )
    assert (
        found.figure("dq.without_reached").page == q["without_reached"] == q["indicators"]
    )  # nothing in 2025


def test_overdue_reports_step_by_step(validator, world):
    params = {"year": "2026", "section": ""}
    found = tm.monitoring_trail(params, TODAY)
    steps = {s.key: s for s in found.overdue}
    assert steps["O4"].kept == found.figure("pm.overdue_reports").page == 1
    assert steps["O3"].kept == steps["O2"].kept - steps["O3"].out
    rows = _csv(validator, params, "figure:pm.overdue_reports")
    assert sum(int(r["counts_once_progress_report"]) for r in rows) == 1
    assert {r["progress_report_unit"] for r in rows} == {
        next(iter({r["progress_report_unit"] for r in rows}))
    }


def test_gate_open_overdue_reports_add_prp_s_reports_still_owed_once(validator, world):
    """With the link of PRP's progress reports proven (the gate open), Partner monitoring's Overdue reports add
    the Overdue reports still owed in PRP's own list that the partner reports do not have: the rows' QPR6 and
    PRP's QPR8 of the same PD, each once. O5 says so with its CSV, and the figure's CSV lists both."""
    from tests.datamart.test_owed_reports import EMAIL, PERSON, _record, _verdict

    params = {"year": "2026", "section": ""}
    pd, d = world["pd"], datetime.date
    _record(pd, "QPR8", d(2026, 4, 1), d(2026, 4, 30), "Overdue", due=d(2026, 5, 15))
    _record(pd, "QPR6", d(2026, 3, 1), d(2026, 5, 31), "Overdue", due=d(2026, 6, 15))  # the rows have it
    filters = tm.filters_of(QueryDict(_query(params)))
    rows = monitoring.indicators(filters, TODAY)
    assert monitoring.summary(rows, filters, TODAY)["overdue_reports"] == 1  # gate closed: the rows' QPR6
    _verdict()
    compute.clear()
    assert monitoring.summary(rows, filters, TODAY)["overdue_reports"] == 2
    found = tm.monitoring_trail(params, TODAY)
    figure = found.figure("pm.overdue_reports")
    assert (figure.page, figure.rebuilt) == (2, 2) and not found.differences
    steps = {s.key: s for s in found.overdue}
    assert (steps["O4"].kept, steps["O5"].kept) == (1, 1)
    assert steps["O5"].what == "overdue:O5"
    shown, _context = _page_figures(validator, params)
    assert shown["pm.overdue_reports"] == 2
    figure_rows = _csv(validator, params, figure.what)
    assert sum(int(r["counts_once_progress_report"]) for r in figure_rows) == 2
    o5 = _csv(validator, params, "overdue:O5")
    assert [(r["source"], r["counts_once_progress_report"]) for r in o5] == [("PRP progress reports", "1")]
    assert o5[0]["progress_report_unit"] in {r["progress_report_unit"] for r in figure_rows}
    said = repr(figure_rows + o5)
    assert PERSON not in said and EMAIL not in said


# ------------------------------------------------------------------------------------- Program Performance
@pytest.mark.parametrize(("section", "builds"), [("", 1), ("Education", 2)])
def test_program_performance_without_a_section_reads_the_trail_s_one_build(
    validator, world, monkeypatch, section, builds
):
    """SPEC_validation §3.5 and §3.9: with no section chosen, Program Performance's filter is the trail's, so
    its part reads the trail's one recorded build (and the page's indicators that build stored); with one, it
    reads the filter without it, a build of its own."""
    found = []
    real = monitoring.indicators

    def counting(*args, **kwargs):
        found.append(kwargs.get("steps") is not None)
        return real(*args, **kwargs)

    monkeypatch.setattr(monitoring, "indicators", counting)
    compute.clear()
    url = (
        reverse("reports:check_figures", args=[PM])
        + "?"
        + _query({"view": "table", "year": "all", "section": section})
    )
    get_part(validator, url, "figures")
    table = get_part(validator, url, "performance")
    assert found == [True] * builds
    figures = table.context["table"].block.figures
    assert figures and all(f.page == f.rebuilt for f in figures)


def test_program_performance_s_indicators_are_the_sum_of_its_section_tiles(validator, world, monkeypatch):
    monkeypatch.setattr(monitoring, "MAX_INDICATORS", 2)  # Program Performance lists 2, counts them all
    params = {"view": "table", "year": "all", "section": ""}
    found = tm.performance_trail(params, TODAY)
    figures = {f.key: f for f in found.block.figures}
    tiles = sum(f.page for k, f in figures.items() if k.startswith("pp.section_tile."))
    assert figures["pp.indicators"].page == figures["pp.indicators"].rebuilt == tiles > 2
    assert figures["pp.rows"].page == 2
    # each indicator's Reached opens its fold: the rows placed to it, used or not, and why
    girls = next(f for f in found.block.figures if f.label.endswith(world["girls"]))
    rows = _csv(validator, params, girls.what, of="performance")
    assert rows and all(r["placed_to"] == rows[0]["placed_to"] for r in rows if r["placed_to"])
    assert {r["counted"] for r in rows} >= {"yes"}


def test_the_trail_writes_a_reached_as_the_table_draws_it(validator, world):
    """A Reached that is not a whole number (a percentage at 1 of 3): the trail's On the page and Rebuilt cells
    read as Program Performance draws it (number:0), the exact value in their title."""
    pd, partner, title = world["pd"], world["partner"], "% of sessions held"
    dm.PDIndicator.objects.create(
        datamart_id=50,
        source_id=950,
        intervention=pd,
        title=title,
        display_type="percentage",
        section_name="Education",
        target_numerator=50,
        location_name="Akkar",
    )
    rows = report_rows(
        pd, partner, title, "PR-1", "QPR1", "QPR", datetime.date(2026, 3, 31), {"Akkar": 1, "Zahle": 1}, 1
    )
    dm.ReportedIndicator.objects.filter(pk__in=[r.pk for r in rows]).update(
        disaggregation={"()": {"v": 1, "d": 3}}, calculation_across_locations="ratio"
    )
    compute.clear()
    params = {"view": "table", "year": "all", "section": ""}
    (figure,) = [f for f in tm.performance_trail(params, TODAY).block.figures if f.label.endswith(title)]
    assert isinstance(figure.page, float) and not figure.page.is_integer() and figure.page == figure.rebuilt
    table = text(get_part(validator, reverse("reports:pd_monitoring") + "?" + _query(params), "table"))
    shown = re.search(rf'data-figure="{re.escape(figure.key)}"[^>]*>([^<]*)<', table).group(1).strip()
    trail_url = reverse("reports:check_figures", args=[PM]) + "?" + _query(params)
    part = text(get_part(validator, trail_url, "performance"))
    row = re.search(rf'<tr id="{figure.anchor}".*?</tr>', part, re.S).group(0)
    cells = re.findall(r'<td class="num"([^>]*)>([^<]*)</td>', row)
    assert [value for _attrs, value in cells] == [shown, shown], (shown, cells)
    assert all(f'title="{figure.page}"' in attrs for attrs, _value in cells)


def test_the_programme_documents_line_credits_each_sync_with_what_it_saved_after(db):
    """Each sync saves its programme documents twice: the Datamart's again with their budgets
    (``intervention_budgets``), the eTools REST sync's with their details (``intervention_details``), straight
    after its ``interventions``. The line credits the sync with all of them, never "earlier"."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations.etools import datamart_sync

    pds = [
        PCA.objects.create(etl_id=str(n), number=f"LEB/PD{n}", title="A", status="active") for n in (1, 2, 3)
    ]
    assert "earlier: 3" in tm.pd_writers()  # no sync yet: each reads "never ran"

    def sync(job, then, write):
        first = SyncRun.objects.create(job=job, target="interventions", status=SyncRun.Status.RUNNING)
        for pd in PCA.objects.filter(pk__in=[p.pk for p in pds]):
            pd.save()
        first.finish(SyncRun.Status.SUCCEEDED)
        second = SyncRun.objects.create(job=job, target=then, status=SyncRun.Status.RUNNING)
        write()
        second.finish(SyncRun.Status.SUCCEEDED)

    links = datamart_sync.Links()

    def budgets() -> None:  # as the dataset's sync does: each programme document saved with its budget
        for p in pds:
            datamart_sync.update_budget({"source_id": p.etl_id, "budget_total": "10"}, links)

    def details() -> None:  # as the REST sync's details do
        for p in pds:
            PCA.objects.get(pk=p.pk).save()

    sync(SyncRun.Job.ETOOLS_DATAMART, "intervention_budgets", budgets)
    line = tm.pd_writers()
    assert "Datamart sync of programme documents" in line and "earlier: 0" in line, line
    assert re.search(r"Datamart sync of programme documents \([^)]*\): 3", line), line
    sync(SyncRun.Job.ETOOLS, "intervention_details", details)
    line = tm.pd_writers()
    assert re.search(r"REST sync of programme documents \([^)]*\): 3", line) and "earlier: 0" in line, line


def test_a_csv_cut_by_a_slow_query_says_so_in_its_last_row(validator, world, monkeypatch):
    """The PD indicator rows, the partner report rows and a source kept whole: a query that takes too long ends
    each file with one row saying so."""
    from neurodb.datamart import raw

    original = raw.related  # only the CSVs' records are slow: the trail itself reads on in its time
    slow = ["pg_sleep(1) IS NOT NULL"]
    monkeypatch.setattr(raw, "related", lambda name, rows: original(name, rows).extra(where=slow))
    monkeypatch.setattr(raw, "STREAM_TIMEOUT_MS", 500)
    params = {"year": "2026", "section": ""}
    for what in ("step:D4", "reached:all", "reached:totals"):
        rows = _csv(validator, params, what)
        assert rows and rows[-1]["figures_as_of"] == raw.STOPPED, what


# ------------------------------------------------------------------------------------------------- the map
def test_the_map_s_figures_are_its_stored_places_and_the_trail_s(validator, world):
    params = {"year": "2026", "section": ""}
    found = tm.map_trail(params, TODAY)
    api = validator.get(reverse("api:pd_map"), params).json()
    totals = api["totals"]
    figures = {f.key: f for f in found.block.figures}
    assert all(f.page == f.rebuilt for f in figures.values()), [
        (f.key, f.page, f.rebuilt) for f in figures.values()
    ]
    assert figures["map.locations"].page == totals["locations"]
    assert figures["map.indicators"].page == totals["indicators"]
    assert figures["map.unlocated"].page == len(api["unlocated"])
    assert figures["map.status.on_track"].page == totals["status_counts"].get("on_track", 0)
    places = _csv(validator, params, "figure:map.locations", of="map")
    assert sum(int(r["counts_once"]) for r in places) == totals["locations"]
    # the page carries every key: its tiles in the template, its totals line written by pdmap.js
    html = text(validator.get(reverse("reports:pd_monitoring_map"), params))
    tiles = set(re.findall(r'data-figure="(map\.[a-z_.]+)"', html))
    script = (Path(__file__).parents[2] / "neurodb/web/static/js/pdmap.js").read_text()
    lines = set(re.findall(r'figure\("(map\.[a-z_.]+)"', script))
    assert tiles | lines == set(figures)


def test_the_map_s_trail_opens_on_the_map_and_goes_back_to_it(validator, world):
    """ "Check these figures" on the map: its trail opens its Map part at once, and "Open the page" is the map."""
    from tests.web.parts import raw_get

    link = validator.get(reverse("reports:pd_monitoring_map"), {"year": "2026"}).context["check_trail"]
    assert "view=map" in link
    shell = raw_get(validator, link)
    assert shell.status_code == 200 and shell.context["trail_view"] == "map"
    page_url = shell.context["page_url"]
    assert page_url.startswith(reverse("reports:pd_monitoring_map")) and "view=" not in page_url
    assert re.search(r'<details class="panel mt-3" id="trail-map" open>', text(shell))
    assert not re.search(r'<details class="panel mt-3" id="trail-performance" open>', text(shell))
    # the grid's own trail: the grid, the Map folded
    shell = raw_get(validator, reverse("reports:check_figures", args=[PM]), {"year": "2026"})
    assert shell.context["trail_view"] == "grid" and shell.context["page_url"].startswith(reverse(TRAIL_GRID))
    assert not re.search(r'id="trail-map" open', text(shell))


# ------------------------------------------------------------------------------------------------ Home's block
def test_home_s_indicator_progress_is_home_s_tiles(validator, world):
    part = get_part(validator, "/?section=", "indicators")
    shown = drawn(text(part))
    link = re.search(r'<a href="([^"]+)" data-check-trail>Check these figures</a>', text(part))
    assert link and "block=indicators" in link.group(1)
    url = link.group(1).replace("&amp;", "&")
    page = validator.get(url)
    assert page.status_code == 200
    params = dict(QueryDict(url.split("?", 1)[1]).lists())
    found = tm.monitoring_trail(params, TODAY, home=True)
    assert not found.differences
    keys = {f.key for f in found.figures}
    assert keys == {
        "pm.indicators",
        "pm.with_target",
        "pm.status.on_track",
        "pm.status.over_target",
        "pm.status.off_track",
        "pm.status.not_reported",
        "pm.with_reached",
        "pm.pds",
        "pm.partners",
    }
    assert set(shown) == keys
    summary = part.context["indicators"]
    for f in found.figures:
        assert shown[f.key] == f.page, f.key
    assert found.figure("pm.indicators").page == summary["indicators"]
    rows = _csv(validator, {**params, "block": "indicators"}, "kept", page="home")
    assert len(rows) == summary["indicators"]


# ------------------------------------------------------------------------------------------------ the hooks
@pytest.mark.parametrize("params", FILTERS)
def test_the_hooks_change_no_sql_and_no_figure(world, params):
    filters = tm.filters_of(_params(params, world))
    assert str(monitoring._pd_queryset(filters).query) == str(monitoring._pd_queryset(filters, []).query)
    rows = monitoring._indicator_rows(filters, [world["pd"].pk])
    assert str(rows.query) == str(monitoring._indicator_rows(filters, [world["pd"].pk], []).query)
    plain = monitoring.as_rows(monitoring.indicators(filters, TODAY))
    recorded = monitoring.as_rows(monitoring.indicators(filters, TODAY, steps=[], record=trail.Recorder()))
    assert plain == recorded
    overdue = monitoring.overdue_rows({world["pd"].pk}, filters.report_type, TODAY)
    assert str(overdue.query) == str(
        monitoring.overdue_rows({world["pd"].pk}, filters.report_type, TODAY, []).query
    )
    every = monitoring.stored_indicators(dataclasses_replace(filters), TODAY)
    assert monitoring.program_performance_rows(filters, TODAY) == [
        r for r in every if not filters.sections or (r.section or monitoring.NO_SECTION) in filters.sections
    ]


def dataclasses_replace(filters):
    import dataclasses

    return dataclasses.replace(filters, sections=[])


def test_the_page_never_passes_a_hook(validator, world, monkeypatch):
    import inspect

    from neurodb.datamart import reached as rc

    seen = []
    for module, name, hook in (
        (monitoring, "indicators", "steps"),
        (monitoring, "indicators", "record"),
        (monitoring, "_pd_queryset", "steps"),
        (monitoring, "overdue_rows", "steps"),
        (rc, "compute", "record"),
        (rc, "progress_values", "record"),
    ):
        function = getattr(module, name)

        def wrapper(*args, _function=function, _name=name, _hook=hook, **kwargs):
            seen.append((_name, inspect.signature(_function).bind(*args, **kwargs).arguments.get(_hook)))
            return _function(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)
    for view in ("grid", "table", "quality"):
        for name in PARTS[view]:
            get_part(validator, f"/partner-monitoring/?view={view}&year=2026", name)
    assert validator.get(reverse("api:pd_map"), {"year": "2026"}).status_code == 200
    get_part(validator, "/?section=", "indicators")
    assert {"indicators", "_pd_queryset", "overdue_rows", "compute", "progress_values"} <= {
        n for n, _ in seen
    }
    assert all(hook is None for _, hook in seen)


def test_a_cold_trail_is_the_page_s_one_build(validator, world):
    params = {"year": "2026", "section": ""}
    compute.clear()
    found = tm.monitoring_trail(params, TODAY)
    filters = tm.filters_of(params)
    with CaptureQueriesContext(connection) as stored:
        rows = monitoring.stored_indicators(filters, TODAY)
    assert len(stored) <= 3  # the moment of the eTools figures and the stored value: nothing computed
    assert monitoring.summary(rows, filters, TODAY)["indicators"] == found.figure("pm.indicators").page
    compute.reset_memo()


def test_a_trail_runs_as_many_queries_whatever_the_number_of_indicators(world, settings):
    settings.DEBUG = True  # computed at each call: nothing stored between the two counts
    params = {"year": "2026", "section": ""}
    with CaptureQueriesContext(connection) as few:
        small = tm.monitoring_trail(params, TODAY)
    pd = world["pd"]
    for n in range(40):
        dm.PDIndicator.objects.create(
            datamart_id=1000 + n,
            source_id=5000 + n,
            intervention=pd,
            title=f"# of extra {n}",
            location_name="Akkar",
        )
        report_rows(
            pd,
            world["partner"],
            f"# of extra {n}",
            "PR-1",
            "QPR1",
            "QPR",
            datetime.date(2026, 3, 31),
            {"Akkar": 1},
            1,
        )
    with CaptureQueriesContext(connection) as many:
        large = tm.monitoring_trail(params, TODAY)
    assert len(large.rows.kept) == len(small.rows.kept) + 40
    assert len(many) == len(few)


def test_the_trail_says_when_an_etools_sync_is_running(validator, world, monkeypatch):
    from neurodb.datamart import keys
    from neurodb.integrations import background

    url = reverse("reports:check_figures", args=[PM])
    assert 'id="trail-sync-running"' not in text(validator.get(url, {"year": "2026"}))
    for job in keys.ETOOLS_JOBS:
        monkeypatch.setattr(background, "is_running", lambda name, job=job: name == job)
        assert 'id="trail-sync-running"' in text(validator.get(url, {"year": "2026"})), job


def test_the_master_indicators_have_no_trail(validator, world):
    url = reverse("reports:check_figures", args=[PM])
    assert validator.get(url, {"view": "masters", "year": "2026"}).status_code == 404
    rows = reverse("reports:check_rows", args=[PM])
    assert validator.get(rows, {"year": "2026", "what": "nothing"}).status_code == 404
    assert validator.get(rows, {"year": "2026", "what": "kept", "of": "elsewhere"}).status_code == 404


# ------------------------------------------------------------------------------------------------- the words
def test_the_trail_s_sentences_say_none_of_the_words_of_the_code(world):
    found = tm.monitoring_trail({"year": "2026", "q": "girls", "location": "Bekaa", "section": ""}, TODAY)
    table = tm.performance_trail({"view": "table", "year": "all", "section": "Education"}, TODAY)
    sentences = [*found.notes, *found.filter_words]
    for step in (*found.steps, *found.reached, *found.overdue, *table.steps):
        sentences += [step.title, step.kept_words, step.out_words or "", *(line.words for line in step.lines)]
    for block in (*found.blocks, table.block):
        sentences.append(block.title)
        sentences += [line.words for line in block.notes]
        sentences += [x for f in block.figures for x in (f.label, f.how, f.note)]
    for sentence in sentences:
        assert not NEVER.findall(sentence), sentence
