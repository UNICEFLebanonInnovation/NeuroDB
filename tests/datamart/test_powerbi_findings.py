"""What the first real Power BI comparison found, fixed in the comparison (SPEC_pbi_findings, stage 2): reports
still owed get a group of their own ("NeuroDB cannot show this yet"), owed only for a period that has ended,
the disaggregation rows tied to their PD through the location export, Power BI's own rules labelled (one report
per period end, the PD's last year, a copy of a title, Accepted only), two indicators sharing a title added up,
rounding at the 6th decimal, the section rule counted in a note, and the diagnostics (inactive indicators, funds
on another PD, budgets). Small generated fake data shaped like the real exports only."""

from __future__ import annotations

import datetime

import pytest

from neurodb.datamart import powerbi_match as pm
from neurodb.datamart import powerbi_results, reached
from neurodb.datamart import powerbi_words as words
from neurodb.datamart.models import PowerBIExport
from neurodb.integrations import background
from tests.datamart import powerbi_factory as pf
from tests.datamart.test_powerbi_compare import (
    ACTIVE,
    LOCATION_FILTERS,
    PARTNER,
    PD,
    REPORTS,
    SECTIONS,
    YEAR,
    _indicator,
    _lines,
    _one,
    _with,
)

pytestmark = pytest.mark.django_db
D = datetime.date
AFTER = datetime.datetime(2026, 10, 15, 8, 0)  # NeuroDB's sync, after Power BI's refresh
REFRESHED = datetime.datetime(2026, 10, 14, 6, 0)
OTHER_PD = "FAKE/PCA2026009/PD2026009-1"  # a PD NeuroDB does not hold
FOLLOW_FILTERS = (*ACTIVE, "report_status_label is Due, Overdue, Sent back, or Submitted")


@pytest.fixture
def started(monkeypatch):
    found = []
    monkeypatch.setattr(background, "start_command", lambda *args: found.append(args) or 4242)
    return found


@pytest.fixture
def checker(client, roles, started):
    client.force_login(pf.user("checker"))
    return client


@pytest.fixture
def world(db, monkeypatch):
    """NeuroDB holds partner reports from 1 Jan 2026: the factory's world (``powerbi_factory.world``)."""
    monkeypatch.setattr(
        reached, "history_from", lambda today=None: reached.HistoryFrom(D(2026, 1, 1), "read_from")
    )
    return pf.world()


def _report(**values):
    return _with(REPORTS[0], **values)


def _owed(n=1, **values):
    """A report Power BI's list has as Overdue, for a period that has ended (August), that NeuroDB lacks."""
    base = {"report": f"HR{n}", "report_status": "Overdue", "period_start": D(2026, 8, 1),
            "period_end": D(2026, 8, 31), "due_date": D(2026, 9, 15)}  # fmt: skip
    return _report(**{**base, **values})


def _follow(**values):
    return pf.row("follow_up", **{"pd": PD, "partner": PARTNER, "section": SECTIONS, **values})


def _only(lines, *match):
    """The one line whose match values start with ``match`` (dates as ISO text)."""
    wanted = [m.isoformat() if isinstance(m, datetime.date) else m for m in match]
    (found,) = [x for x in lines if x.match[: len(wanted)] == wanted]
    return found


def _compared(client, *files, sync=None):
    """Upload ``files`` (Power BI refreshed on 14 Oct at 06:00) and compare them; ``sync``: when NeuroDB's
    partner reports sync finished."""
    if sync is not None:
        pf.sync(sync)
    return pf.compared(client, *files, refreshed=REFRESHED)


# ============================================================================== group 5: cannot show yet
def test_an_owed_report_for_an_ended_period_is_cannot_show_yet(checker, world):
    upload = _compared(checker, _one("report_list", [*REPORTS, _owed()]))
    export, lines = _lines(upload, "report_list")
    line = _only(lines, PD, "HR1")
    assert (line.kind, line.reason, line.group) == ("only_powerbi", "owed", 5)
    assert line.detail == words.DETAIL_OWED
    assert export.verdict == "not_shown" and not powerbi_results.settled(export)


def test_a_not_yet_due_report_power_bi_lists_and_neurodb_lacks_needs_a_look(checker, world):
    """The feed carries reports not yet due (NeuroDB holds most of them): one NeuroDB lacks was not read."""
    not_yet = _report(report="QPR4", report_status="Due", period_start=D(2026, 10, 1), period_end=D(2026, 12, 31),
                      due_date=D(2027, 1, 15))  # fmt: skip
    upload = _compared(checker, _one("report_list", [*REPORTS, not_yet]), sync=AFTER)
    _export, lines = _lines(upload, "report_list")
    line = _only(lines, PD, "QPR4")
    assert (line.reason, line.group) == ("not_in_read", 4)


@pytest.mark.parametrize("kind", ["report_list", "follow_up", "locations"])
def test_an_overdue_report_of_a_pd_neurodb_lacks_reads_no_pd(checker, world, kind):
    rows = {
        "report_list": [*REPORTS, _owed(pd=OTHER_PD)],
        "follow_up": [_follow(pd=OTHER_PD, status="Overdue", due_date=D(2026, 9, 15))],
        "locations": [_location(pd=OTHER_PD, report="HR1", p_code="LB9_9_1", reached=4, report_status="Overdue",
                                period_end=D(2026, 8, 31))],
    }[kind]  # fmt: skip
    files = [_one(kind, rows, filters=FOLLOW_FILTERS if kind == "follow_up" else ACTIVE)]
    if kind == "locations":
        files = [_one(kind, rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP)]
    upload = _compared(checker, *files, sync=AFTER)
    _export, lines = _lines(upload, kind)
    (line,) = [x for x in lines if x.match[0] == OTHER_PD]
    assert (line.reason, line.group) == ("not_in_read", 4)
    assert line.detail.startswith(words.DETAIL_NO_PD)


def test_owed_lines_are_group_5_never_expected_and_not_settled(checker, world):
    """Reports still owed (and their draft values) on the report list, the follow-up list, the locations, the
    disaggregations and the counted visuals: group 5, never expected, never settled."""
    owed_location = _location(report="HR1", p_code="LB9_9_1", reached=4, report_status="Overdue",
                              period_start=D(2026, 8, 1), period_end=D(2026, 8, 31))  # fmt: skip
    value = _value(report="HR1", report_type="HR", report_status="Overdue", period_start=D(2026, 8, 1),
                   period_end=D(2026, 8, 31), disaggregation="Female", reached=4)  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _owed()]),
        _one("follow_up", [_follow(status="Overdue", due_date=D(2026, 9, 15))], filters=FOLLOW_FILTERS),
        _one("locations", [owed_location], filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("disaggregations", [value], filters=YEAR, drop=DISAGGREGATION_DROP),
        _one(
            "by_month",
            [pf.row("by_month", month="Aug 2026", overdue=1, accepted=0, submitted=0, due=0)],
            filters=YEAR,
        ),
    )
    for kind in ("report_list", "follow_up", "locations", "disaggregations", "by_month"):
        export, lines = _lines(upload, kind)
        owed = [x for x in lines if x.reason == "owed"]
        assert owed and {x.group for x in owed} == {5}, kind
        assert not powerbi_results.settled(export), kind


def test_pick_ranks_needs_a_look_above_cannot_show(world):
    from neurodb.datamart import powerbi_facts, powerbi_kinds

    context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    options = [("owed", "", ""), ("unknown", "", ""), ("rule", "", "")]
    assert powerbi_kinds.pick(options, context)[0] == "unknown"
    assert powerbi_kinds.pick([("owed", "", ""), ("rule", "", "")], context)[0] == "owed"
    assert pm.GROUP_ORDER == (4, 3, 2, 5, 1)


def test_verdict_not_shown_and_its_todo_line(checker, world):
    upload = _compared(checker, _one("report_list", [*REPORTS, *(_owed(n) for n in (1, 2, 3))]))
    export = PowerBIExport.objects.get(upload=upload, kind="report_list")
    verdict, action = powerbi_results.verdict_words(export)
    assert verdict == "NeuroDB cannot show 3 rows yet" and "{" not in verdict
    assert action == words.GROUP_ACTIONS[5]
    exports = list(PowerBIExport.objects.filter(upload=upload))
    line, *_expected = powerbi_results.todo(upload, exports, [])
    assert line["text"] == words.TODO_NOT_SHOWN.format(n=3)
    assert line["url"] == powerbi_results.export_url(upload, export, group=5)
    one = PowerBIExport(state="compared", counts={"groups": {"5": 1}, "reasons": {"owed": 1}})
    assert powerbi_results.verdict_words(one)[0] == "NeuroDB cannot show 1 row yet"


def test_four_more_pressing_lines_never_hide_what_neurodb_cannot_show_yet(checker, world):
    """The browser check's upload of the real exports: needs a look, PDs to check, an export cut and the visuals
    not uploaded made four lines, and the cap cut the fifth, the reports still owed (772 differences with the gate
    closed): no Power BI page's verdict named them either. It is now always said, after the four; the expected
    line still waits for room."""
    upload = _compared(checker, _one("report_list", [*REPORTS, *(_owed(n) for n in (1, 2, 3))]))
    exports = list(PowerBIExport.objects.filter(upload=upload))
    export = next(e for e in exports if e.kind == "report_list")
    pressing = [
        "1 file could not be read.",
        "3 visuals are not uploaded.",
        "Clear a slicer.",
        "Clear another one.",
    ]
    lines = powerbi_results.todo(upload, exports, pressing)
    assert [x["text"] for x in lines[:4]] == pressing
    assert lines[4:] == [
        {"text": words.TODO_NOT_SHOWN.format(n=3), "url": powerbi_results.export_url(upload, export, group=5)}
    ]
    # with room, the expected line follows it as before
    export.counts = {
        **export.counts,
        "groups": {"5": 3, "1": 2},
        "reasons": {"owed": 3, "before_history": 2},
        "reason_groups": {"owed": 5, "before_history": 1},
    }
    lines = powerbi_results.todo(upload, [export], pressing[:2])
    assert [x["text"] for x in lines][2] == words.TODO_NOT_SHOWN.format(n=3)
    assert len(lines) == 4 and lines[3]["text"].startswith("Expected, nothing to fix: 2 differences")


def test_an_earlier_upload_s_owed_lines_keep_group_1(checker, world):
    """An upload compared before reports still owed had their own group wrote them as expected: its verdict
    and What to do now still say so, from what it stored (never today's groups)."""
    upload = _compared(checker, _one("report_list", REPORTS))
    export = PowerBIExport.objects.get(upload=upload, kind="report_list")
    export.counts = {"groups": {"1": 3}, "reasons": {"owed": 3}, "written": 3}
    export.verdict = "owed"
    export.save(update_fields=["counts", "verdict"])
    assert powerbi_results.verdict_words(export)[0] == "Same, apart from reports still owed: 3"
    (line,) = powerbi_results.todo(upload, [export], [])
    assert line["text"] == "Expected, nothing to fix: 3 differences (reports still owed 3)."
    # a new upload stores each reason's group: its expected parts never name reports still owed
    new = PowerBIExport(
        state="compared",
        counts={"groups": {"5": 2, "1": 1}, "reasons": {"owed": 2, "before_history": 1},
                "reason_groups": {"owed": 5, "before_history": 1}},
    )  # fmt: skip
    texts = [x["text"] for x in powerbi_results.todo(upload, [new], [])]
    assert texts[-1] == "Expected, nothing to fix: 1 difference (before NeuroDB's history 1)."


# ================================================================================ item 2: the follow-up
def test_a_due_follow_up_row_whose_list_periods_have_not_ended_is_not_in_read(checker, world):
    """A Due row of a report not yet due (the report list of the same upload gives its period): NeuroDB's list
    calls such a report Due too (the user's decision), and the feed carries them, so one NeuroDB lacks was not
    in the read. No "Due covers" label, no choice."""
    listed = _report(report="QPR4", report_status="Due", period_start=D(2026, 10, 1), period_end=D(2026, 12, 31),
                     due_date=D(2027, 1, 15))  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, listed]),
        _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS),
    )
    export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    assert line.reason == "not_in_read" and line.group in (2, 4)
    assert "due_not_yet" not in export.counts["extra"] and "due_rows" not in export.counts["extra"]
    assert upload.choices == []
    assert not hasattr(words, "DETAIL_DUE_COVERS")


def _two_listed(second_end):
    return [
        *REPORTS,
        _report(report="QPR4", report_status="Due", period_start=D(2026, 10, 1), period_end=D(2026, 12, 31),
                due_date=D(2027, 1, 15)),
        _report(report="HR9", report_status="Due", period_start=D(2026, 9, 1), period_end=second_end,
                due_date=D(2027, 1, 15)),
    ]  # fmt: skip


def test_a_due_follow_up_row_with_one_period_ended_stays_owed(checker, world):
    """Every row of the group must agree (risk 1): one period ended, still owed."""
    upload = _compared(
        checker,
        _one("report_list", _two_listed(D(2026, 9, 30))),
        _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS),
    )
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    assert (line.reason, line.group) == ("owed", 5)


def test_a_due_follow_up_row_without_a_report_list_stays_owed(checker, world):
    upload = _compared(
        checker, _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS)
    )
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    assert (line.reason, line.group) == ("owed", 5)


def test_a_due_follow_up_row_with_a_list_row_without_period_end_stays_owed(checker, world):
    """None-safe: a list row without a Period End is compared, and the follow-up row stays owed."""
    upload = _compared(
        checker,
        _one("report_list", _two_listed(None)),
        _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS),
    )
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {"report_list": "compared", "follow_up": "compared"}
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    assert (line.reason, line.group) == ("owed", 5)


# ============================================================================ item 9: history by period start
def _early(start):
    return _report(report="QPR9", report_status="Overdue", period_start=start, period_end=D(2026, 1, 31),
                   due_date=D(2026, 2, 15))  # fmt: skip


def test_a_follow_up_row_whose_list_periods_start_before_history_is_before_history(checker, world):
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _early(D(2025, 11, 1))]),
        _one("follow_up", [_follow(status="Overdue", due_date=D(2026, 2, 15))], filters=FOLLOW_FILTERS),
    )
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Overdue", D(2026, 2, 15))
    assert (line.reason, line.group) == ("before_history", 1)


def test_a_follow_up_row_with_one_row_starting_after_history_stays_owed(checker, world):
    second = _report(report="HR9", report_status="Overdue", period_start=D(2026, 1, 1), period_end=D(2026, 1, 31),
                     due_date=D(2026, 2, 15))  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _early(D(2025, 11, 1)), second]),
        _one("follow_up", [_follow(status="Overdue", due_date=D(2026, 2, 15))], filters=FOLLOW_FILTERS),
    )
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Overdue", D(2026, 2, 15))
    assert (line.reason, line.group) == ("owed", 5)


def test_a_follow_up_row_with_a_row_without_period_start_stays_owed(checker, world):
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _early(None)]),
        _one("follow_up", [_follow(status="Overdue", due_date=D(2026, 2, 15))], filters=FOLLOW_FILTERS),
    )
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Overdue", D(2026, 2, 15))
    assert (line.reason, line.group) == ("owed", 5)


# ================================================================== item 10: the charts and the set-aside rows
def test_set_aside_reports_explain_chart_gaps_as_before_history(checker, world):
    early = _report(
        report="QPR9", report_status="Accepted", period_start=D(2025, 12, 1), period_end=D(2026, 2, 28)
    )
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, early]),
        _one(
            "by_month",
            [
                pf.row("by_month", month="Feb 2026", accepted=1, submitted=0, overdue=0, due=0),
                pf.row("by_month", month="Mar 2026", accepted=1, submitted=0, overdue=0, due=0),
                pf.row("by_month", month="Jun 2026", accepted=0, submitted=1, overdue=0, due=0),
                pf.row("by_month", month="Sep 2026", accepted=0, submitted=0, overdue=1, due=0),
            ],
            filters=YEAR,
        ),  # fmt: skip
    )
    _export, lines = _lines(upload, "by_month")
    line = _only(lines, "Feb 2026")
    assert (line.reason, line.group) == ("before_history", 1)
    assert line.detail == "Of the 1 report behind this gap: 1 is before NeuroDB's history."


def test_list_cut_only_when_the_list_holds_fewer_rows_than_the_chart(checker, world):
    """A report list of 1,000 rows (Power BI may have cut it) that holds more rows of the year than the table
    counts: a gap it does not explain is the chart and its list differing, never the list's cut."""
    others = [_report(pd=f"FAKE/PCA9{n:03}/PD9{n:03}", section="Section Gamma") for n in range(999)]
    upload = _compared(
        checker,
        _one("report_list", [REPORTS[0], *others]),
        _one("by_sector", [pf.row("by_sector", section=SECTIONS, accepted=1, submitted=0, due=0, overdue=0)],
             filters=YEAR),
    )  # fmt: skip
    _sector, lines = _lines(upload, "by_sector")
    assert {(x.reason, x.group, x.detail) for x in lines} == {("unknown", 4, words.DETAIL_NO_GAP_LINES)}


def test_a_chart_gap_mixing_set_aside_and_missing_reports_needs_a_look(checker, world):
    """3 reports before the history and 1 not in the Datamart read: the gap needs a look (never the most
    common reason)."""
    early = [_report(report=f"HR{n}", report_status="Accepted", period_start=D(2025, 12, 1),
                     period_end=D(2026, 2, 28)) for n in (1, 2, 3)]  # fmt: skip
    missing = _report(
        report="HR4", report_status="Accepted", period_start=D(2026, 2, 1), period_end=D(2026, 2, 28)
    )
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, *early, missing]),
        _one("by_month", [pf.row("by_month", month="Feb 2026", accepted=4, submitted=0, overdue=0, due=0)],
             filters=YEAR),
        sync=AFTER,
    )  # fmt: skip
    _export, lines = _lines(upload, "by_month")
    (line,) = [x for x in lines if x.match[0] == "Feb 2026"]
    assert (line.reason, line.group) == ("not_in_read", 4)
    assert "3 are before NeuroDB's history" in line.detail and "1 was not in the Datamart read" in line.detail


def test_a_chart_gap_mixing_owed_and_missing_reports_needs_a_look(checker, world):
    missing = _report(report="HR5", report_status="Overdue", period_start=D(2026, 9, 1), period_end=D(2026, 9, 30),
                      pd=OTHER_PD)  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _owed(1), _owed(2), missing]),
        _one("by_month", [pf.row("by_month", month="Aug 2026", overdue=2, accepted=0, submitted=0, due=0),
                          pf.row("by_month", month="Sep 2026", overdue=2, accepted=0, submitted=0, due=0)],
             filters=YEAR),
        sync=AFTER,
    )  # fmt: skip
    _export, lines = _lines(upload, "by_month")
    found = {x.match[0]: (x.reason, x.group) for x in lines if x.found.get("overdue")}
    assert found["Aug 2026"] == ("owed", 5)  # only reports still owed
    assert found["Sep 2026"] == ("not_in_read", 4)  # one owed, one of a PD NeuroDB does not hold


# ======================================================================================= context safety
def test_a_report_list_that_fails_after_its_rows_leaves_listed_none(checker, world, monkeypatch):
    """The report list fails once compared (while its result is saved): what it told the later exports is
    set back, so the follow-up list's Due row stays owed (its list would have said "not yet due"), and the
    report list says it could not be compared."""
    real = powerbi_results.verdict_code

    def broken(export):
        if export.kind == "report_list":
            raise RuntimeError("boom")
        return real(export)

    monkeypatch.setattr(powerbi_results, "verdict_code", broken)
    listed = _report(report="QPR4", report_status="Due", period_start=D(2026, 10, 1), period_end=D(2026, 12, 31),
                     due_date=D(2027, 1, 15))  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, listed]),
        _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS),
    )
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {"report_list": "failed", "follow_up": "compared"}
    _export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    assert (line.reason, line.group) == ("owed", 5)


def test_a_location_export_that_fails_leaves_its_fields_none(checker, world, monkeypatch):
    """The location export fails once compared (while its result is saved): the disaggregations read none of
    what it told."""
    from neurodb.datamart import powerbi_facts, powerbi_kinds

    real = powerbi_results.verdict_code

    def broken(export):
        if export.kind == "locations":
            raise RuntimeError("boom")
        return real(export)

    monkeypatch.setattr(powerbi_results, "verdict_code", broken)
    seen = []
    compare = powerbi_kinds.disaggregations

    def spy(job):
        seen.append({name: getattr(job.context, name) for name in powerbi_facts.RESET["locations"]})
        return compare(job)

    monkeypatch.setitem(powerbi_kinds.COMPARERS, "disaggregations", spy)
    rows = [_location(report="QPR9", p_code="LB9_9_1", reached=7, period_end=D(2026, 4, 30))]
    upload = _compared(
        checker,
        _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("disaggregations", [_value()], filters=YEAR, drop=DISAGGREGATION_DROP),
    )
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert states == {"locations": "failed", "disaggregations": "compared"}
    assert seen == [powerbi_facts.RESET["locations"]]


# --------------------------------------------------------------------------------------------- helpers
LOCATION_DROP = ("disaggregations", "refreshed", "submitted_on", "location", "pd_status", "partner", "section",
                 "emergency", "cp_output")  # fmt: skip
DISAGGREGATION_DROP = ("section", "location", "due_date", "refreshed")


def _location(**values):
    base = {"pd": PD, "indicator_title": "Fake indicator 1", "report_status": "Accepted", "target": 100,
            "period_start": D(2026, 1, 1)}  # fmt: skip
    return pf.row("locations", **{**base, **values})


def _value(**values):
    base = {"indicator_title": "Fake indicator 1", "p_code": "LB9_9_1", "report": "QPR1", "report_type": "QPR",
            "report_status": "Accepted", "period_start": D(2026, 1, 1), "period_end": D(2026, 3, 31),
            "disaggregation": "Female", "reached": 4}  # fmt: skip
    return pf.row("disaggregations", **{**base, **values})


# ================================================================== item 1: disaggregations tied to their PD
def _tie_files(locations, values, reports=None, drop=DISAGGREGATION_DROP):
    files = [
        _one("locations", locations, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("disaggregations", values, filters=YEAR, drop=drop),
    ]
    if reports is not None:
        files.insert(0, _one("report_list", reports))
    return files


APRIL = {"period_start": D(2026, 4, 1), "period_end": D(2026, 4, 30)}


def _of(lines, report, label="Female", p_code="LB9_9_1"):
    """The one disaggregation line of a report, a value's name and a place."""
    (found,) = [
        x
        for x in lines
        if x.match[5] == report and pm.text(x.match[6]) == pm.text(label) and x.match[1] == p_code
    ]
    return found


def test_a_disaggregation_row_tied_to_a_pd_through_the_location_export_is_not_in_read(checker, world):
    """The disaggregation export has no PD #: its row is tied to its PD through the location export (title,
    report, period and P-code), and the report list says that PD's report was not in the Datamart read."""
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR9", p_code="LB9_9_1", reached=7, **APRIL)],
            [_value(report="QPR9", reached=7, **APRIL)],
            [*REPORTS, _report(report="QPR9", **APRIL)],
        ),
        sync=AFTER,
    )
    _export, lines = _lines(upload, "disaggregations")
    line = _of(lines, "QPR9")
    assert (line.kind, line.reason, line.group) == ("only_powerbi", "not_in_read", 4)


def test_a_disaggregation_row_whose_only_values_are_overdue_rows_is_owed(checker, world):
    """Power BI adds up two PDs' rows of one title, place and report: the value comes from the Overdue row
    (a draft), the Submitted row of the other PD is 0."""
    rows = [
        _value(report="HR2", report_type="HR", report_status="Overdue", reached=5, period_start=D(2026, 8, 1),
               period_end=D(2026, 8, 31)),
        _value(report="HR2", report_type="HR", report_status="Submitted", reached=0, period_start=D(2026, 8, 1),
               period_end=D(2026, 8, 31)),
    ]  # fmt: skip
    upload = _compared(checker, _one("disaggregations", rows, filters=YEAR, drop=DISAGGREGATION_DROP))
    _export, lines = _lines(upload, "disaggregations")
    line = _of(lines, "HR2")
    assert (line.reason, line.group, line.detail) == ("owed", 5, words.DETAIL_OWED_DRAFT)


def test_a_disaggregation_row_tied_to_an_owed_report_is_owed_not_not_in_read(checker, world):
    """No Report Status in the disaggregation export: the tied PD's report is still owed (the report list),
    so its value is a draft NeuroDB cannot show, never a row the read missed."""
    august = {"period_start": D(2026, 8, 1), "period_end": D(2026, 8, 31)}
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="HR1", p_code="LB9_9_1", reached=4, report_status="Overdue", **august)],
            [_value(report="HR1", report_type="HR", reached=4, **august)],
            [*REPORTS, _owed()],
            drop=(*DISAGGREGATION_DROP, "report_status"),
        ),
        sync=AFTER,
    )
    _export, lines = _lines(upload, "disaggregations")
    line = _of(lines, "HR1")
    assert (line.reason, line.group) == ("owed", 5)


def test_a_disaggregation_row_with_no_location_row_stays_not_explained(checker, world):
    """The location export has no row there (it keeps only values above 0): no tie, not explained yet."""
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR9", p_code="LB9_9_2", reached=7, **APRIL)],
            [_value(report="QPR9", reached=7, **APRIL)],
            [*REPORTS, _report(report="QPR9", **APRIL)],
        ),
        sync=AFTER,
    )
    _export, lines = _lines(upload, "disaggregations")
    assert (_of(lines, "QPR9").reason, _of(lines, "QPR9").group) == ("unknown", 4)


def test_a_disaggregation_row_is_never_tied_by_title_alone(checker, world):
    """Two PDs share a standard title: the location export's rows of that title at other places tie
    nothing."""
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR9", p_code="LB9_9_2", reached=7, **APRIL),
             _location(pd=OTHER_PD, report="QPR9", p_code="LB9_9_3", reached=7, **APRIL)],
            [_value(report="QPR9", reached=7, **APRIL)],
            [*REPORTS, _report(report="QPR9", **APRIL), _report(pd=OTHER_PD, report="QPR9", **APRIL)],
        ),
        sync=AFTER,
    )  # fmt: skip
    _export, lines = _lines(upload, "disaggregations")
    assert _of(lines, "QPR9").reason == "unknown"


def test_a_disaggregation_row_tied_to_two_pds_one_held_with_its_place_stays_not_explained(checker, world):
    """Tied to two PDs, one whose report NeuroDB has with a location row of its own there: the value may be
    that report's, a disaggregation NeuroDB lacks: not explained yet. Tied to the missing PD's alone: not in
    the read."""
    march = {"period_start": D(2026, 1, 1), "period_end": D(2026, 3, 31)}
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR1", p_code="LB9_9_1", reached=10, **march),
             _location(pd=OTHER_PD, report="QPR1", p_code="LB9_9_1", reached=3, **march),
             _location(pd=OTHER_PD, report="QPR1", p_code="LB9_9_3", reached=3, **march)],
            [_value(disaggregation="Female", reached=4), _value(disaggregation="Other", reached=3),
             _value(p_code="LB9_9_3", disaggregation="Other", reached=3)],
            [*REPORTS, _report(pd=OTHER_PD)],
        ),
        sync=AFTER,
    )  # fmt: skip
    _export, lines = _lines(upload, "disaggregations")
    assert _of(lines, "QPR1", "Other").reason == "unknown"
    assert _of(lines, "QPR1", "Other", "LB9_9_3").reason == "not_in_read"


def test_a_different_disaggregation_row_reads_the_status_of_its_valued_rows(checker, world):
    """Power BI's Female value comes from an Overdue row (another PD's draft) beside an Accepted row of 0:
    its status is the valued row's, and the value above NeuroDB's is a draft NeuroDB cannot show."""
    rows = [_value(report_status="Overdue", reached=7), _value(report_status="Accepted", reached=0)]
    upload = _compared(checker, _one("disaggregations", rows, filters=YEAR, drop=DISAGGREGATION_DROP))
    _export, lines = _lines(upload, "disaggregations")
    line = _of(lines, "QPR1")
    assert (line.kind, line.reason, line.group) == ("different", "owed", 5)
    assert set(line.found) == {"reached", "report_status"}


def test_a_different_row_with_neurodb_above_power_bi_keeps_its_reason(checker, world):
    """NeuroDB above Power BI: neither a report not read nor a draft explains it."""
    rows = [_value(report_status="Overdue", reached=2)]
    upload = _compared(checker, _one("disaggregations", rows, filters=YEAR, drop=DISAGGREGATION_DROP))
    _export, lines = _lines(upload, "disaggregations")
    line = _of(lines, "QPR1")
    assert line.kind == "different" and line.reason not in ("owed", "not_in_read")


# ===================================================================== item 3: one report per period end
def _september(pd, *, hr=True, second=None):
    """NeuroDB's QPR3 at the first place given a value (Submitted), and (``hr``) an Accepted HR3 ending on the
    same day; ``second``: another PD with the same title's QPR3 there."""
    from neurodb.datamart import models as dm

    dm.ReportedIndicator.objects.filter(intervention=pd, report_number="QPR3").update(
        report_status="Submitted", disaggregation={"()": 9, "(1,)": 4, "(2,)": 5},
        submission_date=D(2026, 10, 10),
    )  # fmt: skip
    if hr:
        pf.report_row(pd, "HR3", "Accepted", D(2026, 9, 1), D(2026, 9, 30), value=4, submitted=D(2026, 10, 2))
    if second is not None:
        row = pf.report_row(second, "QPR3", "Submitted", D(2026, 7, 1), D(2026, 9, 30), value=3,
                            submitted=D(2026, 10, 10))  # fmt: skip
        row.disaggregation = {"()": 3, "(1,)": 3}
        row.save(update_fields=["disaggregation"])


SEPTEMBER_HR = {"report": "HR3", "period_start": D(2026, 9, 1), "period_end": D(2026, 9, 30)}


def _qpr3(lines):
    (found,) = [x for x in lines if x.match[1] == "QPR3" and x.match[3] == "LB9_9_1"]
    return found


def test_neurodb_s_other_report_of_the_same_period_end_is_period_latest(checker, world):
    pd, _ended = world
    _september(pd)
    upload = _compared(
        checker, _one("locations", [_location(p_code="LB9_9_1", reached=4, **SEPTEMBER_HR)],
                      filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )  # fmt: skip
    _export, lines = _lines(upload, "locations")
    line = _qpr3(lines)
    assert (line.kind, line.reason, line.rule, line.group) == ("only_neurodb", "rule", "period_latest", 1)
    assert line.detail == words.DETAIL_PERIOD_LATEST


def test_period_latest_not_when_neurodb_lacks_the_other_report(checker, world):
    pd, _ended = world
    _september(pd, hr=False)
    upload = _compared(
        checker, _one("locations", [_location(p_code="LB9_9_1", reached=4, **SEPTEMBER_HR)],
                      filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )  # fmt: skip
    _export, lines = _lines(upload, "locations")
    assert _qpr3(lines).rule != "period_latest"


def test_period_latest_not_when_power_bi_shows_the_row_s_own_report_for_that_indicator(checker, world):
    pd, _ended = world
    _september(pd)
    rows = [_location(p_code="LB9_9_1", reached=4, **SEPTEMBER_HR),
            _location(p_code="LB9_9_2", reached=1, report="QPR3", period_start=D(2026, 7, 1),
                      period_end=D(2026, 9, 30), report_status="Submitted")]  # fmt: skip
    upload = _compared(checker, _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP))
    _export, lines = _lines(upload, "locations")
    assert _qpr3(lines).rule != "period_latest"


def test_period_latest_not_when_neurodb_s_disaggregation_key_spans_two_pds(checker, world):
    """NeuroDB adds up two PDs' QPR3 Female under one key (the export has no PD #): never Power BI's rule;
    Male, of one PD, is."""
    from neurodb.partnerships.models import PCA

    pd, _ended = world
    second = PCA.objects.create(etl_id="fake-pd3", partner=pd.partner, partner_name=pf.WORLD_PARTNER,
                                number="FAKE/PCA2026003/PD2026003", title="Fake programme 3", status="active",
                                document_type="PD", start=D(2026, 1, 1), end=D(2026, 12, 31),
                                section_names=["Section Alpha"])  # fmt: skip
    _september(pd, second=second)
    pf.report_row(second, "HR3", "Accepted", D(2026, 9, 1), D(2026, 9, 30), value=1, submitted=D(2026, 10, 2))
    locations = [_location(p_code="LB9_9_1", reached=4, **SEPTEMBER_HR),
                 _location(pd=second.number, p_code="LB9_9_1", reached=1, **SEPTEMBER_HR)]  # fmt: skip
    upload = _compared(checker, *_tie_files(locations, [_value(report_type="HR", reached=4, **SEPTEMBER_HR)]))
    _export, lines = _lines(upload, "disaggregations")
    female = _of(lines, "QPR3", "Female")
    male = _of(lines, "QPR3", "Male")
    assert female.kind == "only_neurodb" and female.rule != "period_latest"
    assert (male.kind, male.reason, male.rule) == ("only_neurodb", "rule", "period_latest")


# =========================================================== item 5: two indicators sharing a title added up
def _twins(pd, *, second_place="LB9_9_1", second_title="Fake twin", second_report="QPR1"):
    from neurodb.datamart import models as dm

    for source, title, output, target in ((201, "Fake twin", "Fake output 1", 80),
                                          (202, second_title, "Fake output 2", 125)):  # fmt: skip
        dm.PDIndicator.objects.create(
            datamart_id=source + 5000, source_id=source, intervention=pd, pd_reference_number=pd.number,
            title=title, section_name="Section Alpha", lower_result_name=output, target_numerator=target,
        )  # fmt: skip
    pf.report_row(pd, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), title="Fake twin", source=201, value=55,
                  submitted=D(2026, 4, 10), target="80", pd_output="Fake output 1")  # fmt: skip
    end = D(2026, 3, 31) if second_report == "QPR1" else D(2026, 6, 30)
    pf.report_row(pd, second_report, "Accepted", end.replace(day=1), end, title=second_title, source=202, value=1,
                  submitted=D(2026, 7, 10), target="125", pd_output="Fake output 2",
                  place=second_place)  # fmt: skip


def _twin_row(**values):
    base = {"indicator_title": "Fake twin", "report": "QPR1", "p_code": "LB9_9_1", "reached": 56, "target": 205,
            "period_start": D(2026, 1, 1), "period_end": D(2026, 3, 31)}  # fmt: skip
    return _location(**{**base, **values})


def test_two_indicators_sharing_a_title_add_up_at_a_location(checker, world):
    pd, _ended = world
    _twins(pd)
    upload = _compared(
        checker, _one("locations", [_twin_row()], filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )
    export, lines = _lines(upload, "locations")
    assert not [x for x in lines if x.match[2] == "Fake twin"]
    assert words.NOTE_ADDED_UP_ONE in export.counts["notes"]


def test_two_indicators_sharing_a_title_never_add_up_across_reports_or_p_codes(checker, world):
    pd, _ended = world
    _twins(pd, second_place="LB9_9_2")
    upload = _compared(
        checker, _one("locations", [_twin_row()], filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )
    _export, lines = _lines(upload, "locations")
    (line,) = [x for x in lines if x.match[2] == "Fake twin" and x.match[3] == "LB9_9_1"]
    assert line.kind == "different" and line.found["reached"][:2] == [56, 55]


def test_two_indicators_never_add_up_when_titles_differ_only_by_case_or_spaces(checker, world):
    pd, _ended = world
    _twins(pd, second_title="fake  Twin")
    upload = _compared(
        checker, _one("locations", [_twin_row()], filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )
    _export, lines = _lines(upload, "locations")
    assert any(x.match[2] == "Fake twin" and x.kind == "different" for x in lines)


def test_a_sum_that_differs_stays_different_and_says_it_adds_up(checker, world):
    pd, _ended = world
    _twins(pd)
    upload = _compared(
        checker, _one("locations", [_twin_row(reached=60)], filters=LOCATION_FILTERS, drop=LOCATION_DROP)
    )
    _export, lines = _lines(upload, "locations")
    (line,) = [x for x in lines if x.match[2] == "Fake twin"]
    assert line.kind == "different" and line.found["reached"][:2] == [60, 56]
    assert words.DETAIL_ADDED_UP.format(n=2) in line.detail


# ============================================================================== item 7: rounding
def test_values_rounded_to_six_decimals_are_the_same():
    assert pm.same_number(0.905263, 0.9052631578947368)
    assert pm.same_number(0.166667, 1 / 6)
    assert pm.same_number(0.1666665, 0.166666)  # a value at exactly the half
    assert pm.same_number(0.166667, 0.1666665)
    assert not pm.same_number(0.166668, 1 / 6)


def test_whole_values_one_apart_differ():
    assert not pm.same_number(56, 55)
    assert not pm.same_number(56.0, 55.0)
    assert pm.same_number(56, 56.0000000001)


# ========================================================================= item 8: the twin in the section
def test_the_twin_in_the_filtered_section_pairs_first(checker, world):
    """NeuroDB has a title twice, one copy outside the section Power BI's filter names: Power BI's row pairs
    with the copy in the section (the same), never with the other (left out by the page's section)."""
    pd, _ended = world
    _indicator(pd, 301, "Fake twin", section="Section Alpha")
    _indicator(pd, 302, "Fake twin", section="Section Beta")
    base = {"pd_status": "Active", "partner": PARTNER, "document_type": "PD", "section": SECTIONS}
    rows = [pf.row("pd_indicators", pd=PD, indicator_title="Fake twin", target=100, **base)]
    filters = (*ACTIVE, "section_name is Section Beta")
    drop = ("pd_title", "start", "end", "offices", "cp_output", "reached")
    upload = _compared(checker, _one("pd_indicators", rows, filters=filters, drop=drop))
    export, lines = _lines(upload, "pd_indicators")
    assert not [x for x in lines if x.match[1] == "Fake twin"]
    assert export.counts["same"] == 1


# ========================================================================== item 4: Data Quality's year rule
DQ_FILTERS = (*YEAR, "is_blueprint_canonical is True", "section_name is Section Alpha",
              "actual_value is less than or equal to 0 or is blank")  # fmt: skip


def _dq(title, pd=PD, **values):
    base = {"section": "Section Alpha", "pd": pd, "cp_output": "Output One", "pd_output": "Fake output 1",
            "indicator_title": title, "reached": 0}  # fmt: skip
    return pf.row("dq_list", **{**base, **values})


LATER_END = D(2027, 6, 30)


def _later(end=LATER_END, number="FAKE/PCA2026004/PD2026004"):
    """An active PD that ends after 2026 (``end``), with an indicator whose 2026 report has no value yet."""
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.get(name=pf.WORLD_PARTNER)
    later = PCA.objects.create(etl_id=f"fake-{number}", partner=partner, partner_name=pf.WORLD_PARTNER,
                               number=number, title="Fake programme 4", status="active", document_type="PD",
                               start=D(2026, 1, 1), end=end, section_names=["Section Alpha"],
                               cp_outputs=["Output One"])  # fmt: skip
    _indicator(later, 401, "Fake later indicator", cp_output="Output One")
    pf.report_row(later, "QPR2", "", D(2026, 4, 1), D(2026, 6, 30), title="Fake later indicator", source=401)
    return later


def _dq_line(upload, title):
    _export, lines = _lines(upload, "dq_list")
    (found,) = [x for x in lines if x.match[1] == title]
    return found


def test_dq_pd_year_reading_explains_indicators_of_pds_ending_later(checker, world):
    pd, _ended = world
    _indicator(pd, 103, "Fake never reported", cp_output="Output One")
    _later()
    upload = _compared(checker, _one("dq_list", [_dq("Fake never reported")], filters=DQ_FILTERS))
    line = _dq_line(upload, "Fake later indicator")
    assert (line.kind, line.reason, line.rule, line.group) == ("only_neurodb", "rule", "pd_year", 1)
    assert line.detail == words.DETAIL_PD_YEAR
    export = PowerBIExport.objects.get(upload=upload, kind="dq_list")
    (table,) = export.fits
    assert "dq_pd_year" in {r["candidate"] for r in table["rows"]}


def test_dq_pd_year_off_when_power_bi_lists_an_indicator_of_a_pd_ending_after_the_year(checker, world):
    pd, _ended = world
    later = _later()
    _indicator(later, 402, "Fake later listed", cp_output="Output One")
    rows = [_dq("Fake later listed", pd=later.number)]
    upload = _compared(checker, _one("dq_list", rows, filters=DQ_FILTERS))
    assert _dq_line(upload, "Fake later indicator").rule != "pd_year"


def test_dq_pd_year_off_for_a_dq_upload_of_two_years(checker, world):
    _later()
    filters = (*DQ_FILTERS[:2], "reporting_year is 2026 or 2027", *DQ_FILTERS[3:])
    upload = _compared(checker, _one("dq_list", [_dq("Fake indicator 1")], filters=filters))
    assert PowerBIExport.objects.get(upload=upload, kind="dq_list").state == "compared"
    assert _dq_line(upload, "Fake later indicator").rule != "pd_year"


def test_dq_pd_year_off_for_a_pd_without_an_end_date(checker, world):
    _later(end=None)
    upload = _compared(checker, _one("dq_list", [_dq("Fake indicator 1")], filters=DQ_FILTERS))
    assert PowerBIExport.objects.get(upload=upload, kind="dq_list").state == "compared"
    assert _dq_line(upload, "Fake later indicator").rule != "pd_year"


def test_dq_pd_year_off_when_a_power_bi_pd_is_unresolved(checker, world):
    """A Power BI row of a PD NeuroDB cannot tell (none of that number): it may end after the year."""
    _later()
    upload = _compared(checker, _one("dq_list", [_dq("Fake other", pd=OTHER_PD)], filters=DQ_FILTERS))
    assert PowerBIExport.objects.get(upload=upload, kind="dq_list").state == "compared"
    assert _dq_line(upload, "Fake later indicator").rule != "pd_year"


def test_dq_readings_rank_by_rows_agreed(checker, world):
    """Power BI lists a (no Reached) and b, c (a Reached, one location row at 0): the reading "any row without
    a value or 0" finds b and c, but also d, e and Fake indicator 1, which Power BI does not list. Agreement
    over every indicator (in on both sides, out on both) keeps NeuroDB's own reading."""
    pd, _ended = world
    _indicator(pd, 110, "Fake a", cp_output="Output One")
    for source, title in ((111, "Fake b"), (112, "Fake c"), (113, "Fake d"), (114, "Fake e")):
        _indicator(pd, source, title, cp_output="Output One")
        for place, value in (("LB9_9_1", 5), ("LB9_9_2", 0)):
            pf.report_row(pd, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), title=title, source=source,
                          value=value, place=place, submitted=D(2026, 4, 10))  # fmt: skip
    rows = [_dq("Fake a"), _dq("Fake b", reached=5), _dq("Fake c", reached=5)]
    upload = _compared(checker, _one("dq_list", rows, filters=DQ_FILTERS))
    export = PowerBIExport.objects.get(upload=upload, kind="dq_list")
    (table,) = export.fits
    chosen = next(r["candidate"] for r in table["rows"] if r["chosen"])
    assert chosen == "dq_neurodb"


# ===================================================================================== item 6: the labels
PDI_BASE = {"pd_status": "Active", "partner": PARTNER, "document_type": "PD", "section": SECTIONS}
PDI_DROP = ("pd_title", "start", "end", "offices", "cp_output")


def _pdi(title, **values):
    return pf.row(
        "pd_indicators", **{"pd": PD, "indicator_title": title, "target": 100, **PDI_BASE, **values}
    )


def _pdi_line(upload, title):
    _export, lines = _lines(upload, "pd_indicators")
    found = [x for x in lines if x.match[1] == title]
    return found


def test_reached_accepted_only_label(checker, world):
    """No rule fits Power BI's Reached (it shows 0), NeuroDB's Accepted-only figure fits it, and NeuroDB adds a
    Submitted report: Power BI's Reached counts Accepted reports only."""
    pd, _ended = world
    _indicator(pd, 103, "Fake submitted only")
    pf.report_row(pd, "QPR2", "Submitted", D(2026, 4, 1), D(2026, 6, 30), title="Fake submitted only", source=103,
                  value=8, submitted=D(2026, 7, 10))  # fmt: skip
    upload = _compared(
        checker, _one("pd_indicators", [_pdi("Fake submitted only", reached=0)], drop=PDI_DROP)
    )
    (line,) = _pdi_line(upload, "Fake submitted only")
    assert (line.kind, line.reason, line.rule, line.group) == ("different", "rule", "accepted_only", 1)


def test_reached_never_most_fits_overall(checker, world):
    """Power BI's Reached is not 0 and no rule fits it: never the Accepted-only rule (risk 8)."""
    pd, _ended = world
    _indicator(pd, 103, "Fake submitted only")
    pf.report_row(pd, "QPR2", "Submitted", D(2026, 4, 1), D(2026, 6, 30), title="Fake submitted only", source=103,
                  value=8, submitted=D(2026, 7, 10))  # fmt: skip
    upload = _compared(
        checker, _one("pd_indicators", [_pdi("Fake submitted only", reached=3)], drop=PDI_DROP)
    )
    (line,) = _pdi_line(upload, "Fake submitted only")
    assert line.rule != "accepted_only"


def _shared(pd, second_output="Fake output 2", active=True):
    from neurodb.datamart import models as dm

    for source, output in ((201, "Fake output 1"), (202, second_output)):
        dm.PDIndicator.objects.create(
            datamart_id=source + 5000, source_id=source, intervention=pd, pd_reference_number=pd.number,
            title="Fake twin", section_name="Section Alpha", lower_result_name=output, target_numerator=100,
            is_active=active or source == 201,
        )  # fmt: skip


def test_shared_output_twin(checker, world):
    pd, _ended = world
    _shared(pd)
    upload = _compared(checker, _one("pd_indicators", [_pdi("Fake twin")], drop=PDI_DROP))
    (line,) = _pdi_line(upload, "Fake twin")
    assert (line.kind, line.reason, line.rule, line.group) == ("only_neurodb", "rule", "shared_output", 1)


def test_a_twin_with_the_same_output_is_not_shared_output(checker, world):
    pd, _ended = world
    _shared(pd, second_output="Fake output 1")
    upload = _compared(checker, _one("pd_indicators", [_pdi("Fake twin")], drop=PDI_DROP))
    (line,) = _pdi_line(upload, "Fake twin")
    assert line.rule != "shared_output"


def _copies(pd, *, second_title="Fake copy", active=True):
    from neurodb.datamart import models as dm

    for source, title in ((501, "Fake copy"), (502, second_title)):
        dm.PDIndicator.objects.create(
            datamart_id=source + 5000, source_id=source, intervention=pd, pd_reference_number=pd.number,
            title=title, section_name="Section Alpha", lower_result_name="Fake output 1", target_numerator=100,
            cp_output="Output One", is_active=active or source == 501,
        )  # fmt: skip


def test_dq_canonical_copy(checker, world):
    """Two copies of one indicator (the same PD, title and PD output): Power BI lists one."""
    pd, _ended = world
    _copies(pd)
    upload = _compared(checker, _one("dq_list", [_dq("Fake copy")], filters=DQ_FILTERS))
    line = _dq_line(upload, "Fake copy")
    assert (line.kind, line.reason, line.rule) == ("only_neurodb", "rule", "canonical")


def test_dq_canonical_copy_not_by_target_alone(checker, world):
    """Another title under the same output with the same target is never a copy (risk 6)."""
    pd, _ended = world
    _copies(pd, second_title="Fake other title")
    upload = _compared(checker, _one("dq_list", [_dq("Fake copy")], filters=DQ_FILTERS))
    assert _dq_line(upload, "Fake other title").rule != "canonical"


def test_an_inactive_copy_without_rows_reads_inactive_not_shared_output_or_canonical(checker, world):
    pd, _ended = world
    _shared(pd, active=False)
    _copies(pd, active=False)
    upload = _compared(
        checker,
        _one("pd_indicators", [_pdi("Fake twin"), _pdi("Fake copy")], drop=PDI_DROP),
        _one("dq_list", [_dq("Fake copy")], filters=DQ_FILTERS),
    )
    (line,) = _pdi_line(upload, "Fake twin")
    assert (line.kind, line.reason, line.group) == ("only_neurodb", "inactive", 4)
    assert line.detail == words.DETAIL_INACTIVE
    export = PowerBIExport.objects.get(upload=upload, kind="pd_indicators")
    assert words.NOTE_INACTIVE.format(n=2) in export.counts["notes"]
    line = _dq_line(upload, "Fake copy")
    assert (line.reason, line.group) == ("inactive", 4)
    assert powerbi_results.what_to_do(line, "1 Jan 2026").endswith(words.REASON_ACTIONS["inactive"])


# ============================================================================ item 11: reasons made right
def test_dq_indicator_of_a_report_not_in_read_needs_a_look(checker, world):
    """A PD with one missing report and two indicators only NeuroDB lists, one of which the location export
    names under that report: only that one is not in the read; the other has no report in the year; a copy
    in the same PD stays a copy."""
    pd, _ended = world
    _indicator(pd, 120, "Fake missing a", cp_output="Output One")
    _indicator(pd, 121, "Fake missing b", cp_output="Output One")
    _copies(pd)
    locations = [_location(indicator_title=title, report="QPR9", p_code="LB9_9_1", reached=7, **APRIL)
                 for title in ("Fake missing a", "Fake copy")]  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _report(report="QPR9", **APRIL)]),
        _one("locations", locations, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("dq_list", [_dq("Fake copy")], filters=DQ_FILTERS),
        sync=AFTER,
    )
    assert (_dq_line(upload, "Fake missing a").reason, _dq_line(upload, "Fake missing a").group) == (
        "not_in_read",
        4,
    )
    assert _dq_line(upload, "Fake missing b").rule == "no_report_in_year"
    assert _dq_line(upload, "Fake copy").rule == "canonical"


def test_dq_copies_power_bi_lists_neither_go_on_to_the_next_reasons(checker, world):
    """Two copies of one indicator (the same PD, title and PD output), each with 2026 rows of Reached 0, and
    Power BI's list holds neither: Power BI lists none of them (its copy has a Reached, from QPR9, which the
    Datamart read did not bring: the location export names it), so neither is "a copy Power BI lists once";
    both are not in the read, needing a look."""
    pd, _ended = world
    _copies(pd)
    for source, number, start, end in (
        (501, "QPR1", D(2026, 1, 1), D(2026, 3, 31)),
        (502, "QPR2", D(2026, 4, 1), D(2026, 6, 30)),
    ):
        pf.report_row(pd, number, "Accepted", start, end, title="Fake copy", source=source, value=0,
                      submitted=end + datetime.timedelta(days=5))  # fmt: skip
    locations = [_location(indicator_title="Fake copy", report="QPR9", p_code="LB9_9_1", reached=7, **APRIL)]
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, _report(report="QPR9", **APRIL)]),
        _one("locations", locations, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("dq_list", [_dq("Fake other", pd=OTHER_PD)], filters=DQ_FILTERS),
        sync=AFTER,
    )
    _export, lines = _lines(upload, "dq_list")
    copies = [x for x in lines if x.match[1] == "Fake copy"]
    assert len(copies) == 2
    assert {(x.kind, x.reason, x.group) for x in copies} == {("only_neurodb", "not_in_read", 4)}
    assert not [x for x in copies if x.rule == "canonical"]


def test_dq_a_copy_whose_other_copy_has_a_reached_is_a_copy_power_bi_lists_once(checker, world):
    """One copy has a 2026 report with a Reached, the other none: Power BI lists neither (its one copy has a
    Reached), and the copy without rows is the copy Power BI lists once."""
    pd, _ended = world
    _copies(pd)
    pf.report_row(pd, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), title="Fake copy", source=501, value=8,
                  submitted=D(2026, 4, 5))  # fmt: skip
    upload = _compared(checker, _one("dq_list", [_dq("Fake other", pd=OTHER_PD)], filters=DQ_FILTERS))
    line = _dq_line(upload, "Fake copy")
    assert (line.kind, line.reason, line.rule, line.group) == ("only_neurodb", "rule", "canonical", 1)


def _funds_pd(number="FAKE/PCA2026005/PD2026005"):
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.get(name=pf.WORLD_PARTNER)
    return PCA.objects.create(etl_id=f"fake-{number}", partner=partner, partner_name=pf.WORLD_PARTNER,
                              number=number, title="Fake programme 5", status="active", document_type="PD",
                              start=D(2026, 1, 1), end=D(2026, 12, 31), total_unicef_budget="100",
                              total_budget="200", cso_contribution="10")  # fmt: skip


FUNDED_DROP = ("days_to_end", "start", "end", "pd_status", "partner", "currency", "unicef_planned",
               "partner_contribution", "partner_share", "outstanding_dct", "donor", "disbursed")  # fmt: skip


def _funded(upload, number):
    _export, lines = _lines(upload, "funded_programmes")
    (found,) = [x for x in lines if x.match[0] == number]
    return found


def test_funds_named_for_a_pd_but_linked_elsewhere_need_a_look(checker, world):
    from decimal import Decimal

    from neurodb.datamart import models as dm

    pd, _ended = world
    other = _funds_pd()
    dm.FundsReservation.objects.create(datamart_id=710, intervention=pd, pd_reference_number=other.number,
                                       fr_number="FR-FAKE-2", grant_number="SC000009",
                                       overall_amount=Decimal("10"))  # fmt: skip
    rows = [pf.row("funded_programmes", pd=other.number, grant="SC000009", fund_reservation=10, cash=200)]
    upload = _compared(checker, _one("funded_programmes", rows, filters=ACTIVE, drop=FUNDED_DROP))
    line = _funded(upload, other.number)
    assert (line.reason, line.group) == ("funds_elsewhere", 4)
    assert line.detail == words.DETAIL_FUNDS_ELSEWHERE.format(pd=PD)


def test_an_unlinked_line_naming_a_held_pd_is_never_fr_only(checker, world):
    from decimal import Decimal

    from neurodb.datamart import models as dm

    other = _funds_pd()
    dm.FundsReservation.objects.create(datamart_id=711, intervention=None, pd_reference_number=other.number,
                                       fr_number="FR-FAKE-3", grant_number="SC000009",
                                       overall_amount=Decimal("10"))  # fmt: skip
    rows = [pf.row("funded_programmes", pd=other.number, grant="SC000009", fund_reservation=10, cash=200)]
    upload = _compared(checker, _one("funded_programmes", rows, filters=ACTIVE, drop=FUNDED_DROP))
    line = _funded(upload, other.number)
    assert (line.reason, line.detail) == ("funds_elsewhere", words.DETAIL_FUNDS_UNLINKED)


def test_partner_cards_detail_counts_a_partner_only_before_history(checker, world):
    """Partner Performance has no year: Power BI counts a partner whose only Submitted report is from before
    NeuroDB's history. A count, never a name."""
    early = _report(report="QPR8", report_status="Submitted", partner="FAKEORG PARTNER 2",
                    period_start=D(2025, 10, 1), period_end=D(2025, 12, 31))  # fmt: skip
    upload = _compared(
        checker,
        _one("report_list", [*REPORTS, early]),
        _one("partner_cards", [pf.row("partner_cards", accepted=1, submitted=2, due=1, overdue=0)]),
    )
    _export, lines = _lines(upload, "partner_cards")
    (line,) = [x for x in lines if "submitted" in x.found]
    assert words.DETAIL_PARTNERS_BEFORE_ONE in line.detail
    assert "FAKEORG PARTNER 2" not in line.detail.upper()


# ============================================================================ Q4: the section rule noted
def test_section_rule_lines_are_counted_in_one_note_not_listed(checker, world):
    """Power BI lists, under its Section filter, an indicator whose own section is another (the PD's sections
    hold both): equal in every other column, it is counted exactly as before, in one note, not listed."""
    pd, _ended = world
    _indicator(pd, 103, "Fake beta indicator", section="Section Beta")
    filters = (*ACTIVE, "section_name is Section Alpha")
    rows = [_pdi("Fake beta indicator"), _pdi("Fake indicator 1"), _pdi("Fake percentage 2", target=40)]
    upload = _compared(checker, _one("pd_indicators", rows, filters=filters, drop=(*PDI_DROP, "reached")))
    export, lines = _lines(upload, "pd_indicators")
    assert not _pdi_line(upload, "Fake beta indicator")
    counts = export.counts
    assert (
        counts["only_powerbi"] == 1 and counts["reasons"] == {"left_out": 1} and counts["groups"] == {"1": 1}
    )
    assert counts["noted"] == {"section": 1} and counts["written"] == 0
    assert words.NOTE_PD_SECTIONS_ONE in counts["notes"]
    assert not any(n.startswith("Differences counted but not listed") for n in counts["notes"])
    assert export.verdict == "counted"
    from django.urls import reverse

    html = checker.get(reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk])).content.decode()
    assert words.NOTED_COUNT.format(n=1) in html


def test_a_paired_out_of_section_indicator_whose_target_differs_is_listed(checker, world):
    pd, _ended = world
    _indicator(pd, 103, "Fake beta indicator", section="Section Beta")
    filters = (*ACTIVE, "section_name is Section Alpha")
    rows = [_pdi("Fake beta indicator", target=90)]
    upload = _compared(checker, _one("pd_indicators", rows, filters=filters, drop=(*PDI_DROP, "reached")))
    (line,) = _pdi_line(upload, "Fake beta indicator")
    assert line.kind == "different" and line.found["target"][:2] == [90, 100.0]
    assert words.DETAIL_SECTION in line.detail


def test_a_dq_section_line_with_a_neurodb_reached_is_listed(checker, world):
    """Data Quality under Section Alpha: Power BI lists two indicators of Section Beta (its PD's sections hold
    Alpha); the one NeuroDB has no Reached for differs only by section (noted), the other has a Reached in
    NeuroDB (listed)."""
    pd, _ended = world
    _indicator(pd, 103, "Fake beta none", section="Section Beta", cp_output="Output One")
    _indicator(pd, 104, "Fake beta reached", section="Section Beta", cp_output="Output One")
    pf.report_row(pd, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), title="Fake beta reached", source=104,
                  value=6, submitted=D(2026, 4, 10))  # fmt: skip
    rows = [_dq("Fake beta none"), _dq("Fake beta reached")]
    upload = _compared(checker, _one("dq_list", rows, filters=DQ_FILTERS))
    export, lines = _lines(upload, "dq_list")
    assert not [x for x in lines if x.match[1] == "Fake beta none"]
    line = _dq_line(upload, "Fake beta reached")
    assert (line.kind, line.reason, line.rule) == ("only_powerbi", "left_out", "section")
    assert export.counts["noted"] == {"section": 1}


# =============================================================================================== diagnostics
def test_an_inactive_indicator_without_rows_is_reason_inactive_never_no_report_in_year(checker, world):
    pd, _ended = world
    _indicator(pd, 601, "Fake lapsed", cp_output="Output One", is_active=False)
    upload = _compared(checker, _one("dq_list", [_dq("Fake indicator 1", reached=35)], filters=DQ_FILTERS))
    line = _dq_line(upload, "Fake lapsed")
    assert (line.kind, line.reason, line.group) == ("only_neurodb", "inactive", 4)


def test_a_gap_of_inactive_indicators_says_so(checker, world):
    pd, _ended = world
    _indicator(pd, 103, "Fake never reported", cp_output="Output One")
    _indicator(pd, 601, "Fake lapsed", cp_output="Output One", is_active=False)
    upload = _compared(
        checker,
        _one("dq_list", [_dq("Fake never reported")], filters=DQ_FILTERS),
        _one("dq_card", [pf.row("dq_card", indicators=1)], filters=DQ_FILTERS[:4]),
    )
    _card, (line,) = _lines(upload, "dq_card")
    assert words.DETAIL_GAP_PART_ONE["inactive"] in line.detail
    assert (line.reason, line.group) == ("inactive", 4)


def test_budget_lines_carry_neurodb_s_budget_fields(checker, world):
    rows = [pf.row("fund_monitoring", pd=PD, unicef_planned=8000, cash=10000, partner_contribution=1000)]
    drop = ("financial_contribution", "partner_type", "partner", "currency", "partner_share")
    upload = _compared(checker, _one("fund_monitoring", rows, drop=drop))
    _export, (line,) = _lines(upload, "fund_monitoring")
    assert line.found["unicef_planned"][:2] == [8000, 9000]
    assert words.DETAIL_BUDGET.format(unicef="9,000", total="10,000", partner="1,000") in line.detail


def test_canonical_clause_says_neurodb_does_not_read_it(checker, world):
    filters = (*ACTIVE, "is_blueprint_canonical is True")
    upload = _compared(
        checker, _one("pd_indicators", [_pdi("Fake indicator 1")], filters=filters, drop=PDI_DROP)
    )
    export, lines = _lines(upload, "pd_indicators")
    clause = next(c for c in export.clauses if c["words"].startswith("is_blueprint_canonical"))
    assert clause["state"] == "all_listed"
    assert (
        words.CLAUSE_STATES["all_listed"]
        == "NeuroDB does not read this field: its pages list every PD indicator eTools sends"
    )
    assert not any(x.reason == "filter" for x in lines)
    assert any("every PD indicator eTools sends" in n for n in export.notes)


# ========================================================== the cards and Indicators with Missing Targets
def test_each_card_is_recognised_by_its_name():
    """A card's export holds one column, the card's own name (powerbi_names, the one place for them)."""
    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_names

    for page, name in powerbi_names.POWERBI_CARDS.values():
        kind = px.recognise([name], [0]).kind
        assert kind is not None and kind.visual == (page, name), name
    table = px.recognise(["Section", "PD #", "Indicator Title", "Target"], [0, 1, 2, 3]).kind
    assert table is not None and table.key == "missing_targets"
    assert table.visual == (powerbi_names.DATA_QUALITY, "Indicators with Missing Targets")


def _cards(values, filters):
    return [
        (f"{key}.xlsx", pf.export(key, [pf.row(key, count=n)], filters=filters)) for key, n in values.items()
    ]


def test_the_cards_compare_with_neurodb_s_figures(checker, world):
    """Partner Performance's, Reporting Progress's and Data Quality's cards against NeuroDB's figures for the
    same filter (progress reports, partners, programme documents and indicators); a card that differs is
    explained as its table is (the report list of the same upload)."""
    dq = (*YEAR, "section_name is Section Alpha")
    files = [
        *_cards({"partners_count": 1, "partners_reported": 1, "partners_not_reported": 0}, ACTIVE),
        *_cards({"accepted_report": 1, "submitted_report": 1, "due_report": 1, "overdue_report": 1}, YEAR),
        *_cards({"pd_count": 1, "active_pds": 1, "ended_pds": 0, "dq_total": 2, "dq_with_reached": 2,
                 "dq_with_target": 2, "dq_without_target": 0}, dq),
        _one("report_list", [REPORTS[0], REPORTS[1], _report(report="QPR3", report_status="Due",
                                                             period_start=D(2026, 7, 1), period_end=D(2026, 9, 30),
                                                             due_date=D(2026, 10, 14)), _owed()]),
    ]  # fmt: skip
    upload = _compared(checker, *files)
    exports = {e.kind: e for e in PowerBIExport.objects.filter(upload=upload)}
    same = {k for k, e in exports.items() if e.verdict == "same"}
    assert same >= {"partners_count", "partners_reported", "partners_not_reported", "accepted_report",
                    "submitted_report", "due_report", "pd_count", "active_pds", "ended_pds", "dq_total",
                    "dq_with_reached", "dq_with_target", "dq_without_target"}  # fmt: skip
    _export, (line,) = _lines(upload, "overdue_report")
    assert line.found == {"count": [1, 0, None]}
    assert (line.reason, line.group) == ("owed", 5)  # the report list's Overdue HR1: still owed
    # the tiles count progress reports as the cards do: no note on another unit
    assert "Same unit" not in " ".join(exports["overdue_report"].counts["notes"])


def test_indicators_with_missing_targets_compare_with_neurodb_s(checker, world):
    pd, _ended = world
    _indicator(pd, 103, "Fake no target", target=None)
    dq = (*YEAR, "section_name is Section Alpha")
    rows = [
        pf.row("missing_targets", section="Section Alpha", pd=PD, indicator_title="Fake no target"),
        pf.row(
            "missing_targets", section="Section Alpha", pd=PD, indicator_title="Fake indicator 1", target=0
        ),
        pf.row("missing_targets", section="Section Alpha", pd=OTHER_PD, indicator_title="Fake other"),
    ]
    upload = _compared(checker, _one("missing_targets", rows, filters=dq))
    export, lines = _lines(upload, "missing_targets")
    found = {x.match[1]: x for x in lines}
    assert set(found) == {"Fake indicator 1", "Fake other"} and export.counts["same"] == 1
    assert found["Fake indicator 1"].kind == "different"  # NeuroDB has its target
    assert found["Fake indicator 1"].found["target"][:2] == [0, 100.0]
    assert found["Fake other"].reason == "not_in_read"
    assert found["Fake other"].detail.startswith(words.DETAIL_NO_PD)


def _beta_pd():
    """An active PD whose only section is Section Beta."""
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.get(name=pf.WORLD_PARTNER)
    return PCA.objects.create(etl_id="fake-beta", partner=partner, partner_name=pf.WORLD_PARTNER,
                              number="FAKE/PCA2026006/PD2026006", title="Fake programme 6", status="active",
                              document_type="PD", start=D(2026, 1, 1), end=D(2026, 12, 31),
                              section_names=["Section Beta"], cp_outputs=["Output One"])  # fmt: skip


def test_missing_targets_read_power_bi_s_section_slicer_as_the_pd_s_sections(checker, world):
    """Power BI's Section slicer reads the PD's sections. Under Section Alpha it lists two indicators of the
    world's PD (sections Alpha and Beta) whose own section is Beta: NeuroDB holds one without a target (the
    section rule, counted in the export's note, never "not in the read") and one with a target (it differs in
    its target). NeuroDB lists an Alpha indicator without a target of a PD whose only section is Beta, which
    Power BI does not list: the section rule too."""
    pd, _ended = world
    _indicator(pd, 103, "Fake beta no target", target=None, section="Section Beta", cp_output="Output One")
    _indicator(pd, 104, "Fake beta target", section="Section Beta", cp_output="Output One")
    _indicator(_beta_pd(), 601, "Fake alpha of a beta PD", target=None, cp_output="Output One")
    dq = (*YEAR, "section_name is Section Alpha")
    rows = [
        pf.row("missing_targets", section="Section Alpha, Section Beta", pd=PD, indicator_title=title)
        for title in ("Fake beta no target", "Fake beta target")
    ]
    upload = _compared(checker, _one("missing_targets", rows, filters=dq), sync=AFTER)
    export, lines = _lines(upload, "missing_targets")
    found = {x.match[1]: x for x in lines}
    assert "Fake beta no target" not in found and export.counts["noted"] == {"section": 1}
    assert words.NOTE_PD_SECTIONS_ONE in export.counts["notes"]
    assert (found["Fake beta target"].kind, found["Fake beta target"].found["target"][1]) == (
        "different",
        100.0,
    )
    line = found["Fake alpha of a beta PD"]
    assert (line.kind, line.reason, line.rule, line.group) == ("only_neurodb", "rule", "section", 1)
    assert not [x for x in lines if x.reason == "not_in_read"]


def test_a_data_quality_card_power_bi_counts_by_the_pd_s_sections_is_the_section_rule(checker, world):
    """Under Section Alpha, Power BI's Data Quality counts the world's PD's Beta indicator too (the PD's
    sections): its cards of indicators and of those with a target are one above NeuroDB's, exactly NeuroDB's
    figure counted Power BI's way, so they are the section rule (expected), never "not explained yet". A card
    that differs otherwise is still not explained."""
    pd, _ended = world
    _indicator(pd, 103, "Fake beta indicator", section="Section Beta", cp_output="Output One")
    dq = (*YEAR, "section_name is Section Alpha")
    upload = _compared(checker, *_cards({"dq_total": 3, "dq_with_target": 3, "dq_without_target": 7}, dq))
    for kind in ("dq_total", "dq_with_target"):
        export, (line,) = _lines(upload, kind)
        powerbi, neurodb, by_pd = line.found["count"]
        assert powerbi == by_pd == neurodb + 1, kind
        assert (line.reason, line.rule, line.group, line.detail) == (
            "left_out", "section", 1, words.DETAIL_SECTION
        ), kind  # fmt: skip
        assert export.verdict != "look", kind
    _export, (other,) = _lines(upload, "dq_without_target")
    assert (other.reason, other.group, other.found["count"][2]) == ("unknown", 4, None)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _strings(item)


def test_the_comparison_names_power_bi_s_list_without_reached_exactly():
    """Every sentence of the comparison naming Power BI's Data Quality list names it as Power BI's report does
    (powerbi_names, the one place), never by a shortened name."""
    from neurodb.datamart import powerbi_names

    name = powerbi_names.visual("dq_list")
    assert words.DETAIL_DQ_WITH.startswith(f"{name}: ")
    for key, value in vars(words).items():
        for text in _strings(value):
            if "Indicators without Reached" in text:
                assert name in text, key


def test_how_to_export_asks_for_every_visual_and_card_by_its_exact_name():
    from neurodb.datamart import powerbi_admin
    from neurodb.datamart import powerbi_exports as px

    lines = powerbi_admin.how_visuals()
    text = " ".join(lines)
    for kind in px.KINDS.values():
        page, name = kind.visual
        assert name in text and page in text, name
    steps = " ".join(powerbi_admin.how_steps(D(2026, 10, 15)))
    assert "Reporting Year / Reporting year" in steps  # Program Performance's, and the other pages'
    assert "Partner Fund Monitoring and Funded Programs have no year slicer" in steps
    for name in ("Report status", "Report type", "Report #", "Emergency indicator", "PD Reference #"):
        assert name in steps, name


# ===================================================================================== the stage 2 check
NOT_YET = {"period_start": D(2026, 10, 1), "period_end": D(2026, 12, 31)}


def test_a_draft_value_of_a_report_not_yet_due_reads_the_same_on_location_and_disaggregation(checker, world):
    """NeuroDB holds QPR4 (October to December, its period still running) without a value; Power BI shows a
    value at a place in both exports. A report not yet due is no report still owed: neither export calls the
    value a draft (group 5); both give the same reason."""
    pd, _ended = world
    pf.report_row(pd, "QPR4", "", D(2026, 10, 1), D(2026, 12, 31), due=D(2027, 1, 15))
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR4", p_code="LB9_9_1", reached=4, report_status="Due", **NOT_YET)],
            [_value(report="QPR4", report_status="Due", reached=4, **NOT_YET)],
        ),
        sync=AFTER,
    )
    _export, locations = _lines(upload, "locations")
    (location,) = [x for x in locations if pm.text(x.match[1]) == "qpr4"]
    _export, values = _lines(upload, "disaggregations")
    value = _of(values, "QPR4")
    assert location.reason != "owed" and location.group != 5
    assert (location.reason, location.group) == (value.reason, value.group)


def test_a_draft_value_of_a_report_whose_period_has_ended_stays_cannot_show_yet(checker, world):
    """The same with the period ended (NeuroDB holds the Overdue QPR3 without a value): the draft (group 5)."""
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR3", p_code="LB9_9_1", reached=4, report_status="Overdue",
                       period_start=D(2026, 7, 1), period_end=D(2026, 9, 30))],
            [_value(report="QPR3", report_status="Overdue", reached=4, period_start=D(2026, 7, 1),
                    period_end=D(2026, 9, 30))],
        ),
        sync=AFTER,
    )  # fmt: skip
    _export, locations = _lines(upload, "locations")
    (location,) = [x for x in locations if pm.text(x.match[1]) == "qpr3" and x.kind == "different"]
    assert (location.reason, location.group, location.detail) == ("owed", 5, words.DETAIL_OWED_DRAFT)


def test_the_data_quality_cards_say_their_gap_from_their_own_side(checker, world):
    """NeuroDB lists two indicators without Reached, Power BI's list one of them: NeuroDB's card without Reached
    is 1 above Power BI's, its card with Reached 1 below. Each card's detail says the gap its own way round."""
    pd, _ended = world
    _indicator(pd, 130, "Fake no reach a", cp_output="Output One")
    _indicator(pd, 131, "Fake no reach b", cp_output="Output One")
    dq = (*YEAR, "section_name is Section Alpha")
    upload = _compared(
        checker,
        _one("dq_list", [_dq("Fake no reach a")], filters=DQ_FILTERS),
        *_cards({"dq_with_reached": 3}, dq),
        _one("dq_card", [pf.row("dq_card", indicators=1)], filters=dq),
    )
    _export, (without,) = _lines(upload, "dq_card")
    assert without.found["indicators"][:2] == [1, 2]  # NeuroDB above
    assert without.detail.startswith("1 more in NeuroDB, 0 more in Power BI")
    _export, (with_reached,) = _lines(upload, "dq_with_reached")
    assert with_reached.found["count"][:2] == [3, 2]  # NeuroDB below: one more without Reached in NeuroDB
    assert with_reached.detail.startswith(
        "Indicators without Reached or with Zero Reached: 1 more in NeuroDB's list, 0 more in Power BI's"
    )
    assert "more in NeuroDB, 0 more in Power BI, net" not in with_reached.detail


def test_the_location_facts_share_one_string_per_title_and_go_after_their_last_reader(
    checker, world, monkeypatch
):
    """What the location export tells the later exports keeps one string per distinct title (NeuroDB's own
    location rows share it), and the job lets it go once no export left reads it: Funded Programs, compared
    after By disaggregation, finds none of it."""
    from neurodb.datamart import powerbi_kinds

    seen = {}
    for kind in ("disaggregations", "funded_programmes"):
        original = powerbi_kinds.COMPARERS[kind]

        def wrapped(job, kind=kind, original=original):
            context = job.context
            seen[kind] = {
                "location_pds": context.location_pds,
                "held": set(context.held_locations or ()),
                "titles": {id(key[0]) for key in (context.location_pds or {})},
            }
            return original(job)

        monkeypatch.setitem(powerbi_kinds.COMPARERS, kind, wrapped)
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="QPR1", p_code="LB9_9_1", reached=7, period_end=D(2026, 3, 31)),
             _location(report="QPR1", p_code="LB9_9_2", reached=3, period_end=D(2026, 3, 31))],
            [_value(report="QPR1", reached=7)],
        ),
        _one("funded_programmes", [pf.row("funded_programmes", pd=PD)]),
        sync=AFTER,
    )  # fmt: skip
    assert upload.status == "done"
    before = seen["disaggregations"]
    assert before["location_pds"]  # By disaggregation reads them
    # one string per title: the two places' keys, and NeuroDB's own location rows of the title, share it
    assert len(before["titles"]) == 1
    held = {id(t[1]) for t in before["held"] if t[1] == "fake indicator 1"}
    assert held and held <= before["titles"]
    after = seen["funded_programmes"]
    assert after["location_pds"] is None and after["held"] == set()


def test_a_disaggregation_key_keeps_no_empty_set_and_no_set_of_pds():
    """Power BI's disaggregation keys whose rows carry no value keep no set of valued statuses (most keys: an
    empty set each weighed on the job's memory)."""
    import gzip
    import io
    import json

    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_facts as pfacts
    from neurodb.datamart import powerbi_kinds
    from neurodb.datamart.powerbi_filters import Filter

    names = ["indicator_title", "p_code", "period_start", "period_end", "report_type", "report", "disaggregation",
             "reached", "report_status"]  # fmt: skip
    rows = [
        ["Fake indicator 1", "LB9_9_1", "2026-01-01", "2026-03-31", "QPR", "QPR1", "Female", 0, "Accepted"],
        ["Fake indicator 1", "LB9_9_1", "2026-01-01", "2026-03-31", "QPR", "QPR1", "Male", 5, "Accepted"],
    ]
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as stream:
        for row in rows:
            stream.write(json.dumps(row).encode() + b"\n")
    context = pfacts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    job = powerbi_kinds.Job(
        px.KINDS["disaggregations"], names, buffer.getvalue(), Filter(), context, pm.Result(10)
    )
    found = powerbi_kinds._disaggregation_sums(job, buffer.getvalue(), True, True)
    by_label = {key[-1]: value for key, value in found.items()}
    assert by_label["female"]["valued"] is None and by_label["female"]["statuses"] == {"accepted"}
    assert by_label["male"]["valued"] == {"accepted"}


def test_gate_open_draft_values_of_owed_reports_stay_cannot_show(checker, world):
    """With the gate open (NeuroDB shows PRP's own reports still owed), a value Power BI shows in an Overdue
    report whose period has ended is still the partner's draft (group 5): HR1, which PRP's own list holds and
    NeuroDB shows, and HR2, which neither feed has (the report list's line reads not in the read), on the
    location export and on the disaggregation export, whose rows (no Report Status) are tied to their PD
    through the location export."""
    from tests.datamart.test_powerbi_compare import _owed_shown

    _owed_shown("HR1", D(2026, 8, 31), D(2026, 9, 15))
    august = {"period_start": D(2026, 8, 1), "period_end": D(2026, 8, 31)}
    september = {"period_start": D(2026, 9, 1), "period_end": D(2026, 9, 30)}
    upload = _compared(
        checker,
        *_tie_files(
            [_location(report="HR1", p_code="LB9_9_1", reached=4, report_status="Overdue", **august),
             _location(report="HR2", p_code="LB9_9_1", reached=6, report_status="Overdue", **september)],
            [_value(report="HR1", report_type="HR", reached=4, **august),
             _value(report="HR2", report_type="HR", reached=6, **september)],
            [*REPORTS, _owed(), _owed(2, due_date=D(2026, 10, 10), **september)],
            drop=(*DISAGGREGATION_DROP, "report_status"),
        ),
        sync=AFTER,
    )  # fmt: skip
    _export, listed = _lines(upload, "report_list")
    assert not [x for x in listed if x.match[1] == "HR1"]  # NeuroDB shows it from PRP's own list
    assert _only(listed, PD, "HR2").reason == "not_in_read"
    _export, locations = _lines(upload, "locations")
    _export, values = _lines(upload, "disaggregations")
    for report in ("HR1", "HR2"):
        (location,) = [x for x in locations if pm.text(x.match[1]) == pm.text(report)]
        assert (location.reason, location.group, location.detail) == ("owed", 5, words.DETAIL_OWED_DRAFT), (
            report
        )
        value = _of(values, report)
        assert (value.reason, value.group, value.detail) == ("owed", 5, words.DETAIL_OWED_DRAFT), report
    # these draft values are never shown, and NeuroDB already shows PRP's reports still owed: no page promises
    # either will be
    promise = "once PRP's progress reports are tied"
    page = _page(checker, "admin:datamart_powerbi_upload", upload.pk)
    assert promise not in page and "reports still owed the eTools Datamart never sent" not in page
    assert "or values in reports not submitted yet, which the eTools Datamart never sent." in page
    for kind in ("locations", "disaggregations"):
        export = PowerBIExport.objects.get(upload=upload, kind=kind)
        page = _page(checker, "admin:datamart_powerbi_export", upload.pk, export.pk)
        assert words.GROUP_ACTIONS[5] in page and promise not in page, kind


def test_gate_closed_only_a_report_line_points_to_check_11(checker, world):
    """While the gate is closed, a report still owed says NeuroDB shows such reports once PRP's progress reports
    are tied (check 11); the group's sentence, true whatever the gate, never promises it."""
    upload = _compared(checker, _one("report_list", [*REPORTS, _owed()]))
    _export, lines = _lines(upload, "report_list")
    line = _only(lines, PD, "HR1")
    assert line.detail == words.DETAIL_OWED and "check 11" in line.detail
    assert "check 11" not in words.GROUP_ACTIONS[5] and "check 11" not in words.TODO_NOT_SHOWN


# ============================================================ stage 4: a comparison made before the decisions
# what Choices for you kept on an upload compared before the user's decision of 26 Oct 2026 (the questions as the
# comparison asked them then; the evidence is fake)
DECIDED_QUESTIONS = (
    "Should NeuroDB's Reporting Progress count progress reports, as Power BI does?",
    "Should NeuroDB's follow-up list include reports not yet due, as Power BI does?",
    "Should NeuroDB's Emergency indicator mean 'the PD is an SPD', as Power BI's does?",
)
OLD_CHOICES = [
    {"code": code, "question": question, "evidence": f"Fake evidence {n}.", "default": f"Fake default {n}."}
    for n, (code, question) in enumerate(
        zip(("progress_reports", "due", "emergency"), DECIDED_QUESTIONS, strict=True)
    )
]
# what else such a comparison stored, word for word as the code of 26 Oct 2026 wrote it: the notes of Reporting
# Progress's table by sector and chart by month, Program Performance's note on the Emergency indicator, and the
# detail of a follow-up row Power BI calls Due that NeuroDB then called Not yet due
OLD_NOTE_UNIT = "Same unit as Power BI: progress reports. NeuroDB's own table counts indicator reports."
OLD_NOTE_EMERGENCY = (
    "Stated once for every row: Power BI's Emergency indicator is Yes for an SPD; NeuroDB's is the indicator's "
    "high frequency."
)
OLD_DUE_DETAIL = "Power BI's Due also covers reports not yet due."


def _page(client, name, *args):
    from django.urls import reverse

    return client.get(reverse(name, args=args)).content.decode().replace("&#x27;", "'")


def _summary_cells(client, upload):
    import io

    import openpyxl
    from django.urls import reverse

    response = client.get(reverse("admin:datamart_powerbi_xlsx", args=[upload.pk]))
    summary = openpyxl.load_workbook(io.BytesIO(response.content)).worksheets[0]
    return [str(c).strip() for row in summary.iter_rows(values_only=True) for c in row if c]


def test_a_comparison_made_before_the_decisions_no_longer_asks_them(checker, world):
    """An upload compared before the user's decision of 26 Oct 2026 kept the three choices it asked, and its
    follow-up list's rows Power BI calls Due that NeuroDB then called Not yet due (counted another way, Expected).
    Its page and its Excel file no longer ask them: they say NeuroDB counts Power BI's way since, and to compare
    again; so does the follow-up export's verdict, never "see Choices for you"."""
    from neurodb.datamart.models import PowerBIDifference

    upload = _compared(
        checker,
        _one("follow_up", [_follow(status="Due", due_date=D(2027, 1, 15))], filters=FOLLOW_FILTERS),
    )
    export, lines = _lines(upload, "follow_up")
    line = _only(lines, PD, "Due", D(2027, 1, 15))
    # as the comparison stored it before the decision: the row's status counted another way, Expected
    PowerBIDifference.objects.filter(pk=line.pk).update(
        reason="status", rule="", group=1, detail=OLD_DUE_DETAIL
    )
    export.counts = {
        **export.counts,
        "groups": {"1": 1},
        "reasons": {"status": 1},
        "reason_groups": {"status": 1},
        "extra": {**(export.counts.get("extra") or {}), "due_not_yet": 1},
    }
    export.verdict = "counted"
    export.save(update_fields=["counts", "verdict"])
    upload.choices = OLD_CHOICES
    upload.save(update_fields=["choices"])
    page = _page(checker, "admin:datamart_powerbi_upload", upload.pk)
    for question in DECIDED_QUESTIONS:
        assert question not in page, question
    assert "data-pbi-choices" not in page and "See Choices for you" not in page
    assert words.CHOICES_DECIDED in page
    offered = powerbi_results.offered(upload)
    assert offered == frozenset()
    export.refresh_from_db()
    assert powerbi_results.verdict_words(export, offered=offered)[1] == words.COUNTED_DECIDED
    shown = _page(checker, "admin:datamart_powerbi_export", upload.pk, export.pk)
    assert OLD_DUE_DETAIL in shown and "Choices for you" not in shown.split("data-pbi-lines", 1)[1]
    line.refresh_from_db()
    assert "Choices for you" not in powerbi_results.what_to_do(line, "1 Jan 2026", offered)
    cells = _summary_cells(checker, upload)
    assert not [c for c in cells if c in DECIDED_QUESTIONS] and words.CHOICES_DECIDED in cells
    # a comparison made since asks none of them and says nothing of them
    again = pf.compared(checker, _one("by_sector", [pf.row("by_sector", section=SECTIONS, accepted=1,
                                                             submitted=1, due=0, overdue=2)], filters=YEAR))  # fmt: skip
    assert words.CHOICES_DECIDED not in _page(checker, "admin:datamart_powerbi_upload", again.pk)


def test_a_comparison_made_before_the_decisions_no_longer_states_the_old_rules_in_its_notes(checker, world):
    """The notes such a comparison stored state NeuroDB's rules of then: Reporting Progress's table by sector and
    chart by month said NeuroDB's own table counts indicator reports, Program Performance that NeuroDB's
    Emergency indicator is the indicator's high frequency. The export page and the Excel file's Summary say
    instead what NeuroDB counted then, what it counts since the decision and to compare again; the comparison's
    other notes stay as they were."""
    from tests.datamart.test_powerbi_compare import PP_FILTERS, _pp

    sector = [pf.row("by_sector", section=SECTIONS, accepted=1, submitted=1, due=0, overdue=2)]
    upload = _compared(
        checker,
        _one("by_sector", sector, filters=YEAR),
        _one(
            "program_performance", [_pp(indicator_title="Fake indicator 1", target=100)], filters=PP_FILTERS
        ),
    )
    old = {"by_sector": OLD_NOTE_UNIT, "program_performance": OLD_NOTE_EMERGENCY}
    kept = {}
    for kind, note in old.items():
        export = PowerBIExport.objects.get(upload=upload, kind=kind)
        kept[kind] = list(export.counts.get("notes") or [])
        export.counts = {**export.counts, "notes": [note, *kept[kind]]}
        export.save(update_fields=["counts"])
    upload.choices = OLD_CHOICES
    upload.save(update_fields=["choices"])
    cells = _summary_cells(checker, upload)
    for kind, note in old.items():
        export = PowerBIExport.objects.get(upload=upload, kind=kind)
        shown = _page(checker, "admin:datamart_powerbi_export", upload.pk, export.pk)
        assert note not in shown, kind
        assert words.NOTES_DECIDED[note] in shown, kind
        assert all(n in shown for n in kept[kind]), kind
        assert note not in cells and words.NOTES_DECIDED[note] in cells, kind
    # what they say now: what NeuroDB counted then, what it counts since, and to compare again
    assert "indicator reports" in words.NOTES_DECIDED[OLD_NOTE_UNIT]
    assert "high frequency" in words.NOTES_DECIDED[OLD_NOTE_EMERGENCY]
    for sentence in words.NOTES_DECIDED.values():
        assert "26 Oct 2026" in sentence and sentence.endswith(
            "Compare again to see the differences as they are now."
        )
