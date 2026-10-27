"""Compare with Power BI's comparison (datamart.powerbi_kinds, powerbi_match, powerbi_facts, powerbi_page): every
kind of export against the NeuroDB page that shows the same thing, for Power BI's own filter; two values per row
and a reason, with what to do, for every difference. Small generated fake data only."""

from __future__ import annotations

import datetime

import pytest

from neurodb.datamart import powerbi_exports, powerbi_match, reached
from neurodb.datamart.models import PowerBIDifference, PowerBIExport, PowerBIUpload
from neurodb.integrations import background
from tests.datamart import powerbi_factory as pf

pytestmark = pytest.mark.django_db
D = datetime.date
ACTIVE = ("country_name is Lebanon", "status is active")
YEAR = (*ACTIVE, "reporting_year is 2026")
PD, ENDED, PARTNER = pf.WORLD_PD, pf.WORLD_ENDED, "FAKEORG PARTNER 1"
SECTIONS = "Section Alpha, Section Beta"


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
def history(monkeypatch):
    """NeuroDB holds partner reports from 1 Jan 2026."""
    monkeypatch.setattr(
        reached, "history_from", lambda today=None: reached.HistoryFrom(D(2026, 1, 1), "read_from")
    )


@pytest.fixture
def world(db, history):
    return pf.world()


def _lines(upload, kind):
    export = PowerBIExport.objects.get(upload=upload, kind=kind)
    return export, list(PowerBIDifference.objects.filter(export=export).order_by("n"))


def _one(kind, rows, filters=ACTIVE, **options):
    return (f"{kind}.xlsx", pf.export(kind, rows, filters=filters, **options))


def test_every_kind_is_compared_on_an_empty_neurodb(checker):
    upload = pf.compared(checker, *pf.every_export())
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert upload.status == PowerBIUpload.Status.DONE, upload.error
    assert set(states.values()) == {"compared"}, states


REPORTS = [
    pf.row("report_list", pd=PD, report="QPR1", report_status="Accepted", period_end=D(2026, 3, 31),
           pd_status="Active", partner=PARTNER, section=SECTIONS, document_type="PD", due_date=D(2026, 4, 15),
           period_start=D(2026, 1, 1)),
    pf.row("report_list", pd=PD, report="QPR2", report_status="Submitted", period_end=D(2026, 6, 30),
           pd_status="Active", partner=PARTNER, section=SECTIONS, document_type="PD", due_date=D(2026, 7, 15),
           period_start=D(2026, 4, 1)),
    pf.row("report_list", pd=PD, report="QPR3", report_status="Overdue", period_end=D(2026, 9, 30),
           pd_status="Active", partner=PARTNER, section=SECTIONS, document_type="PD", due_date=D(2026, 10, 14),
           period_start=D(2026, 7, 1)),
]  # fmt: skip


def test_the_same_data_gives_no_difference(checker, world):
    files = [
        _one("report_list", REPORTS),
        _one(
            "follow_up",
            [
                pf.row(
                    "follow_up",
                    pd=PD,
                    partner=PARTNER,
                    section=SECTIONS,
                    status="Submitted",
                    due_date=D(2026, 7, 15),
                ),
                pf.row(
                    "follow_up",
                    pd=PD,
                    partner=PARTNER,
                    section=SECTIONS,
                    status="Overdue",
                    due_date=D(2026, 10, 14),
                ),
            ],
            filters=(*ACTIVE, "report_status_label is Due, Overdue, Sent back, or Submitted"),
        ),
        _one("partner_cards", [pf.row("partner_cards", accepted=1, submitted=1, due=0, overdue=1)]),
        _one(
            "by_sector",
            [
                pf.row("by_sector", section=SECTIONS, accepted=1, submitted=1, due=0, overdue=1),
                pf.row("by_sector", section="Total", accepted=1, submitted=1, due=0, overdue=1),
            ],
            filters=YEAR,
        ),  # fmt: skip
        _one(
            "by_month",
            [
                pf.row(
                    "by_month", month="Mar 2026", accepted=1, submitted=0, overdue=0, due=0, not_yet_due=0
                ),
                pf.row(
                    "by_month", month="Jun 2026", accepted=0, submitted=1, overdue=0, due=0, not_yet_due=0
                ),
                pf.row(
                    "by_month", month="Sep 2026", accepted=0, submitted=0, overdue=1, due=0, not_yet_due=0
                ),
            ],
            filters=YEAR,
        ),  # fmt: skip
        _one(
            "locations",
            [
                pf.row(
                    "locations",
                    pd=PD,
                    indicator_title="Fake indicator 1",
                    report="QPR1",
                    p_code="LB9_9_1",
                    reached=10,
                    report_status="Accepted",
                    period_end=D(2026, 3, 31),
                    target=100,
                ),
                pf.row(
                    "locations",
                    pd=PD,
                    indicator_title="Fake indicator 1",
                    report="QPR1",
                    p_code="LB9_9_2",
                    reached=5,
                    report_status="Accepted",
                    period_end=D(2026, 3, 31),
                    target=100,
                ),
                pf.row(
                    "locations",
                    pd=PD,
                    indicator_title="Fake percentage 2",
                    report="QPR1",
                    p_code="LB9_9_1",
                    reached=0.25,
                    report_status="Accepted",
                    period_end=D(2026, 3, 31),
                    target=0.4,
                ),
                pf.row(
                    "locations",
                    pd=PD,
                    indicator_title="Fake indicator 1",
                    report="QPR2",
                    p_code="LB9_9_1",
                    reached=20,
                    report_status="Submitted",
                    period_end=D(2026, 6, 30),
                    target=100,
                ),
            ],
            filters=(*YEAR, "location_value is greater than 0"),
            drop=(
                "disaggregations",
                "refreshed",
                "submitted_on",
                "location",
                "period_start",
                "pd_status",
                "partner",
                "section",
                "cp_output",
                "emergency",
            ),
        ),  # fmt: skip
        _one(
            "fund_monitoring",
            [
                pf.row(
                    "fund_monitoring",
                    pd=PD,
                    unicef_planned=9000.0,
                    cash=10000.000000001,
                    partner_contribution=1000,
                    partner=PARTNER,
                    partner_type="Civil Society Organization",
                    currency="USD",
                    partner_share=0.1,
                )
            ],
            drop=("financial_contribution",),
        ),  # fmt: skip
        _one(
            "funded_programmes",
            [
                pf.row(
                    "funded_programmes",
                    pd=PD,
                    grant="SC000001",
                    fund_reservation=5000.1,
                    donor="Fake Donor",
                    unicef_planned=9000,
                    cash=10000,
                    disbursed=2500,
                    partner_contribution=1000,
                    partner_share=0.1,
                    currency="USD",
                    partner=PARTNER,
                    start=D(2026, 1, 1),
                    end=D(2026, 12, 31),
                    pd_status="active",
                    outstanding_dct=300,
                )
            ],
            drop=("days_to_end",),
        ),  # fmt: skip
        _one(
            "program_performance",
            [
                pf.row(
                    "program_performance",
                    section=SECTIONS,
                    cp_output="Output One, Output, Two",
                    emergency="No",
                    indicator_title="Fake indicator 1",
                    target=100,
                    reached=35,
                    achieved=0.35,
                    partner=PARTNER,
                ),
                pf.row(
                    "program_performance",
                    section=SECTIONS,
                    cp_output="Output One, Output, Two",
                    emergency="No",
                    indicator_title="Fake percentage 2",
                    target=40,
                    reached=25,
                    achieved=0.625,
                    partner=PARTNER,
                ),
            ],
            filters=(*ACTIVE, "is_period_latest is True", "report_status_label is Accepted"),
        ),  # fmt: skip
        _one(
            "pd_indicators",
            [
                pf.row(
                    "pd_indicators",
                    pd=PD,
                    pd_status="Active",
                    partner=PARTNER,
                    document_type="PD",
                    indicator_title="Fake indicator 1",
                    target=100,
                    pd_title="Fake programme 1",
                    start=D(2026, 1, 1),
                    end=D(2026, 12, 31),
                    offices="Office North",
                    section=SECTIONS,
                    cp_output="Output One",
                    reached=35,
                ),
                pf.row(
                    "pd_indicators",
                    pd=PD,
                    pd_status="Active",
                    partner=PARTNER,
                    document_type="PD",
                    indicator_title="Fake percentage 2",
                    target=40,
                    pd_title="Fake programme 1",
                    start=D(2026, 1, 1),
                    end=D(2026, 12, 31),
                    offices="Office North",
                    section="Section Alpha",
                    cp_output="Output One",
                    reached=25,
                ),
            ],
            filters=(*ACTIVE, "is_blueprint_canonical is True"),
        ),  # fmt: skip
        _one("dq_card", [pf.row("dq_card", indicators=0)], filters=YEAR),
        _one(
            "disaggregations",
            [
                pf.row(
                    "disaggregations",
                    indicator_title="Fake indicator 1",
                    p_code="LB9_9_1",
                    report="QPR1",
                    disaggregation="Female",
                    reached=4,
                    period_end=D(2026, 3, 31),
                    period_start=D(2026, 1, 1),
                    report_status="Accepted",
                    report_type="QPR",
                ),
                pf.row(
                    "disaggregations",
                    indicator_title="Fake indicator 1",
                    p_code="LB9_9_1",
                    report="QPR1",
                    disaggregation="Male",
                    reached=6,
                    period_end=D(2026, 3, 31),
                    period_start=D(2026, 1, 1),
                    report_status="Accepted",
                    report_type="QPR",
                ),
                pf.row(
                    "disaggregations",
                    indicator_title="Fake indicator 1",
                    p_code="LB9_9_1",
                    report="QPR1",
                    disaggregation="Other",
                    reached=0,
                    period_end=D(2026, 3, 31),
                    period_start=D(2026, 1, 1),
                    report_status="Accepted",
                    report_type="QPR",
                ),
            ],
            filters=(*ACTIVE, "is_period_latest is True"),
            drop=("section", "location", "due_date", "refreshed"),
        ),  # fmt: skip
    ]
    upload = pf.compared(checker, *files)
    assert upload.status == PowerBIUpload.Status.DONE, upload.error
    for export in PowerBIExport.objects.filter(upload=upload):
        lines = list(
            PowerBIDifference.objects.filter(export=export).values(
                "kind", "reason", "rule", "detail", "match", "found"
            )
        )
        assert export.state == "compared", (export.kind, export.error)
        assert not lines, (export.kind, lines)
        assert export.verdict == "same", export.kind


def _with(row, **changes):
    return pf.Row({**row, **changes})


def _by_reason(lines):
    return {(line.kind, line.reason, line.group): line for line in lines}


def test_the_report_list_gives_each_difference_its_reason(checker, world):
    pf.sync(datetime.datetime(2026, 10, 13, 20, 30))  # NeuroDB's sync ran the day before Power BI's refresh
    rows = [
        REPORTS[0],
        _with(REPORTS[1], report_status="Accepted"),  # NeuroDB still has it Submitted
        REPORTS[2],
        _with(REPORTS[2], report_status="Due"),  # one report listed twice, with two statuses
        _with(
            REPORTS[0], report="QPR4", period_start=D(2025, 10, 1), period_end=D(2025, 12, 31)
        ),  # set aside
        _with(
            REPORTS[0],
            report="HR1",
            report_status="Overdue",
            period_start=D(2026, 8, 1),
            period_end=D(2026, 8, 31),
        ),
        _with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30)),
        _with(REPORTS[0], pd=ENDED),
    ]
    upload = pf.compared(checker, _one("report_list", rows), refreshed=datetime.datetime(2026, 10, 14, 6, 0))
    export, lines = _lines(upload, "report_list")
    found = _by_reason(lines)
    assert export.set_aside == 1 and export.counts["same"] == 2  # QPR1, QPR3
    assert ("different", "older_version", 2) in found
    assert found[("different", "older_version", 2)].found["report_status"] == ["Accepted", "Submitted", None]
    assert (
        found[("only_powerbi", "status", 1)].detail == "Power BI lists this report twice, with two statuses."
    )
    assert ("only_powerbi", "owed", 5) in found  # NeuroDB cannot show it yet
    assert ("only_powerbi", "not_in_read", 2) in found  # the sync ran before the refresh: wait for the next
    assert ("only_powerbi", "pd_not_running", 3) in found and export.counts["check_pds"] == [ENDED]
    order = powerbi_match.GROUP_ORDER
    assert [line.group for line in lines] == sorted((line.group for line in lines), key=order.index)
    assert export.verdict == "check"
    assert "1 row is about a period before 1 Jan 2026" in " ".join(export.counts["notes"])


def test_a_report_the_sync_after_the_refresh_did_not_bring_needs_a_look(checker, world):
    pf.sync(datetime.datetime(2026, 10, 15, 8, 0))  # NeuroDB's sync ran after Power BI's refresh
    rows = [
        _with(REPORTS[1], report_status="Accepted"),
        _with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30)),
    ]
    upload = pf.compared(checker, _one("report_list", rows), refreshed=datetime.datetime(2026, 10, 14, 6, 0))
    export, lines = _lines(upload, "report_list")
    found = _by_reason(lines)
    assert ("only_powerbi", "not_in_read", 4) in found
    assert found[("only_powerbi", "not_in_read", 4)].detail == "The Datamart did not send this row."
    assert not any(line.reason == "older_version" for line in lines)
    assert export.verdict == "look"


FOLLOW_FILTERS = (*ACTIVE, "report_status_label is Due, Overdue, Sent back, or Submitted")


def _follow(**values):
    return pf.row("follow_up", **{"pd": PD, "partner": PARTNER, "section": SECTIONS, **values})


def test_the_follow_up_list_reads_not_yet_due_owed_ended_and_earlier_reports(checker, world):
    pd, ended = world
    pf.report_row(
        pd, "QPR4", "", D(2026, 10, 1), D(2026, 12, 31), due=D(2027, 1, 15)
    )  # not yet due on 15 Oct
    rows = [
        _follow(status="Submitted", due_date=D(2026, 7, 15)),
        _follow(status="Overdue", due_date=D(2026, 10, 14)),
        _follow(status="Due", due_date=D(2027, 1, 15)),  # not yet due: NeuroDB's list calls it Due too
        _follow(status="Overdue", due_date=D(2025, 12, 15)),  # before NeuroDB's history
        _follow(pd=ENDED, status="Submitted", due_date=D(2026, 4, 17)),
        _follow(status="Sent back", due_date=D(2026, 8, 15)),
    ]
    upload = pf.compared(checker, _one("follow_up", rows, filters=FOLLOW_FILTERS))
    export, lines = _lines(upload, "follow_up")
    found = _by_reason(lines)
    # NeuroDB's follow-up list counts the report not yet due as Due, as Power BI's (the user's decision): the
    # rows pair, no "Due covers" label and no choice is left
    assert export.counts["same"] == 3
    assert not any(line.reason == "status" for line in lines)
    assert ("only_powerbi", "before_history", 1) in found
    assert ("only_powerbi", "pd_not_running", 3) in found
    assert ("only_powerbi", "not_in_read", 2) in found
    assert "due_not_yet" not in export.counts["extra"] and upload.choices == []


SECTOR = [
    pf.row("by_sector", section=SECTIONS, accepted=1, submitted=1, due=0, overdue=2),
    pf.row("by_sector", section="Section beta ,Section ALPHA ", accepted=0, submitted=0, due=0, overdue=0),
]


def test_a_counted_visual_without_the_report_list_says_upload_it(checker, world):
    upload = pf.compared(checker, _one("by_sector", SECTOR, filters=YEAR))
    export, lines = _lines(upload, "by_sector")
    assert [(line.reason, line.found) for line in lines] == [("see_reports", {"overdue": [2, 1, None]})]
    notes = " ".join(export.counts["notes"])
    assert "2 Power BI rows join the same sections written differently" in notes
    assert "counted under its PD's sections, as Power BI's Section is" in notes
    assert "Same unit" not in notes  # NeuroDB's table counts progress reports too


def test_a_counted_visual_is_explained_from_the_report_list(checker, world):
    owed = _with(
        REPORTS[0],
        report="HR1",
        report_status="Overdue",
        period_start=D(2026, 8, 1),
        period_end=D(2026, 8, 31),
    )
    upload = pf.compared(
        checker,
        _one("by_sector", SECTOR, filters=YEAR),
        _one(
            "by_month",
            [pf.row("by_month", month="Aug 2026", overdue=1, accepted=0, submitted=0, due=0)],
            filters=YEAR,
        ),
        _one("partner_cards", [pf.row("partner_cards", accepted=1, submitted=1, due=0, overdue=1)]),
        _one("report_list", [*REPORTS, owed]),
    )
    _export, lines = _lines(upload, "by_sector")
    (line,) = lines
    assert (line.reason, line.group) == ("owed", 5)
    assert line.detail == "Of the 1 report behind this gap: 1 is a report still owed."
    _export, lines = _lines(upload, "by_month")
    assert {(line.match[0], line.reason) for line in lines} == {
        ("Aug 2026", "owed"),  # Power BI's chart counts the report still owed
        ("Mar 2026", "unknown"),  # its chart has no row for March: NeuroDB counts one Accepted report there
        ("Jun 2026", "unknown"),
        ("Sep 2026", "unknown"),
    }
    march = next(line for line in lines if line.match[0] == "Mar 2026")
    assert (march.kind, march.found) == ("only_neurodb", {"accepted": [None, 1, None]})
    assert march.detail.startswith("The report list shows no report behind this gap")
    _export, lines = _lines(upload, "partner_cards")
    assert lines == []  # the same partners, whatever the reports


def test_accepted_reports_in_another_month_with_the_same_total_are_a_rule(checker, world):
    rows = [
        pf.row("by_month", month="Apr 2026", accepted=1, submitted=0, overdue=0),  # NeuroDB: March
        pf.row("by_month", month="Jun 2026", accepted=0, submitted=1, overdue=0),
        pf.row("by_month", month="Sep 2026", accepted=0, submitted=0, overdue=1),
    ]
    upload = pf.compared(
        checker, _one("by_month", rows, filters=YEAR, drop=("due", "not_yet_due", "sent_back"))
    )
    export, lines = _lines(upload, "by_month")
    assert {(line.match[0], line.reason, line.rule) for line in lines} == {
        ("Apr 2026", "rule", "month"),
        ("Mar 2026", "rule", "month"),
    }
    assert export.verdict == "counted"
    assert upload.choices == []  # NeuroDB counts progress reports too: no choice is left


PP_FILTERS = (*ACTIVE, "is_period_latest is True", "report_status_label is Accepted")


def _pp(**values):
    base = {
        "section": SECTIONS,
        "cp_output": "Output One, Output, Two",
        "emergency": "No",
        "partner": PARTNER,
    }
    return pf.row("program_performance", **{**base, **values})


def _indicator(pd, source, title, target=100, **extra):
    from neurodb.datamart import models as dm

    return dm.PDIndicator.objects.create(
        datamart_id=source + 5000, source_id=source, intervention=pd, pd_reference_number=pd.number, title=title,
        section_name=extra.pop("section", "Section Alpha"), lower_result_name="Fake output 1", target_numerator=target,
        **extra,
    )  # fmt: skip


def test_program_performance_counts_power_bis_possible_reached_and_says_which_fits(checker, world):
    rows = [
        _pp(
            indicator_title="Fake indicator 1", target=100, reached=15, achieved=0.15
        ),  # Accepted reports only
        _pp(indicator_title="Fake percentage 2", target=40, reached=25, achieved=0.625),
    ]
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    export, lines = _lines(upload, "program_performance")
    (line,) = lines
    assert (line.kind, line.reason, line.rule, line.group) == ("different", "rule", "reached", 1)
    assert line.found == {"reached": [15, 35, 15]}  # Power BI, NeuroDB, counted Power BI's way
    assert line.detail.startswith("Counted Power BI's way (adds up the Accepted quarterly reports")
    # each value with its side: 15 counted Power BI's way (as the Alternative column shows), 35 NeuroDB's own
    assert (
        "monthly reports only)): 15. NeuroDB's own rule gives 35." in line.detail
        and "Also fits:" in line.detail
    )
    assert line.link.startswith("/partner-monitoring/check/") or "figure=pp.reached." in line.link
    (reached, target) = export.fits
    assert reached["title"] == "reached" and target["title"] == "target"
    best = next(r for r in reached["rows"] if r["chosen"])
    assert (best["candidate"], best["fits"], best["of"], best["above"]) == ("qpr_sum", 2, 2, 2)
    assert export.verdict == "counted"
    notes = " ".join(export.counts["notes"])
    # NeuroDB's Emergency indicator is Power BI's (the PD is an SPD): no rule to state about it
    assert "Emergency indicator" not in notes and "Power BI's Section is the PD's sections" in notes
    clauses = {c["words"]: c["state"] for c in export.clauses}
    assert clauses["report_status_label is Accepted"] == "by_comparison"
    choice = next(c for c in upload.choices if c["code"] == "reached")
    assert (
        "matches 'adds up the Accepted quarterly reports" in choice["evidence"]
        and "on 2 of 2 rows" in choice["evidence"]
    )
    assert upload.summary["reached"] == choice["evidence"]


def test_program_performance_joins_rows_and_finds_partners_written_differently(checker, world):
    from neurodb.partnerships.models import PCA

    pd, _ended = world
    third = PCA.objects.create(
        etl_id="fake-pd3", partner=pd.partner, partner_name=pd.partner_name, number="FAKE/PCA2026003/PD2026003",
        title="Fake programme 3", status="active", start=D(2026, 1, 1), end=D(2026, 12, 31),
        section_names=["Section Alpha", "Section Beta"], cp_outputs=["Output One", "Output, Two"],
    )  # fmt: skip
    _indicator(third, 301, "Fake indicator 1", target=50)
    pf.report_row(
        third, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), source=301, value=7, submitted=D(2026, 4, 9)
    )
    rows = [
        _pp(indicator_title="Fake indicator 1", target=150, reached=42, achieved=0.28),  # the two rows joined
        _pp(
            indicator_title="Fake percentage 2",
            target=40,
            reached=25,
            achieved=0.625,
            partner="FAKEORG PARTNER ONE",
        ),
    ]
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    export, lines = _lines(upload, "program_performance")
    assert lines == [] and export.counts["same"] == 2  # joined, and a partner written differently paired


def test_program_performance_partial_history_emergency_and_rows_power_bi_alone_has(
    checker, world, monkeypatch
):
    from neurodb.datamart import models as dm

    pd, ended = world
    _indicator(ended, 201, "Fake ended indicator")
    rows = [
        _pp(indicator_title="Fake indicator 1", target=100, reached=35, achieved=0.35),
        _pp(indicator_title="Fake percentage 2", target=40, reached=25, achieved=0.625, emergency="Yes"),
        _pp(indicator_title="Fake ended indicator", target=100, reached=3, achieved=0.03),
        _pp(indicator_title="Fake indicator never held", target=10, reached=1, achieved=0.1),
    ]
    dm.ReportedIndicator.objects.filter(intervention=pd, indicator="Fake indicator 1").update(
        total_cumulative_progress="90"
    )
    pd.start = D(2025, 6, 1)  # before NeuroDB's history: Reached is PRP's cumulative
    pd.save(update_fields=["start"])
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    export, lines = _lines(upload, "program_performance")
    found = {line.match[1]: line for line in lines}
    first = found["Fake indicator 1"]
    assert first.found["reached"][:2] == [35.0, 90.0] and (first.reason, first.rule) == ("rule", "partial")
    # NeuroDB's Emergency indicator is Power BI's (the PD is an SPD, the user's decision): a plain difference,
    # no rule stated, no evidence kept for a choice
    assert found["Fake percentage 2"].found == {"emergency": ["Yes", "No", None]}
    assert found["Fake percentage 2"].reason != "rule"
    assert found["Fake ended indicator"].reason == "pd_not_running"
    assert found["Fake indicator never held"].reason == "not_in_read"
    assert export.fits[0]["set_aside"] == 2  # the PD started before NeuroDB's history
    assert "emergency_fit" not in export.counts["extra"]
    assert "Emergency indicator" not in " ".join(export.counts["notes"])
    pd.document_type = "SPD"
    pd.save(update_fields=["document_type"])
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    _export, lines = _lines(upload, "program_performance")
    assert "Fake percentage 2" not in {line.match[1] for line in lines}  # Yes on both sides
    assert {line.match[1]: line.found for line in lines}["Fake indicator 1"]["emergency"] == [
        "No",
        "Yes",
        None,
    ]


def test_pd_indicators_read_the_section_filter_and_the_pd_status(checker, world):
    pd, ended = world
    _indicator(ended, 201, "Fake ended indicator")
    _indicator(pd, 103, "Fake beta indicator", section="Section Beta")
    base = {"pd_status": "Active", "partner": PARTNER, "document_type": "PD", "pd_title": "Fake programme 1",
            "start": D(2026, 1, 1), "end": D(2026, 12, 31), "offices": "Office North", "cp_output": "Output One"}  # fmt: skip
    rows = [
        pf.row("pd_indicators", pd=PD, indicator_title="Fake indicator 1", target=100, reached=35, section="Section Alpha", **base),
        pf.row("pd_indicators", pd=PD, indicator_title="Fake percentage 2", target=999, reached=25, section="Section Alpha", **base),
        pf.row("pd_indicators", pd=ENDED, indicator_title="Fake ended indicator", target=100, section="Section Alpha", **base),
    ]  # fmt: skip
    filters = (*ACTIVE, "is_blueprint_canonical is True", "section_name is Section Alpha")
    upload = pf.compared(checker, _one("pd_indicators", rows, filters=filters))
    export, lines = _lines(upload, "pd_indicators")
    found = {line.match[1]: line for line in lines}
    assert set(found) == {"Fake percentage 2", "Fake ended indicator"}  # Section Beta's: out of both
    assert (
        found["Fake percentage 2"].found["target"][:2] == [999, 40.0]
        and found["Fake percentage 2"].group == 4
    )
    assert (
        found["Fake ended indicator"].reason == "pd_not_running" and found["Fake ended indicator"].group == 3
    )
    assert found["Fake ended indicator"].link == ""


def test_data_quality_readings_and_the_card(checker, world):
    from neurodb.datamart import models as dm

    pd, _ended = world
    _indicator(pd, 103, "Fake never reported")  # no report in 2026
    _indicator(pd, 104, "Fake draft value")  # a value only in a report not yet submitted
    pf.report_row(pd, "QPR3", "", D(2026, 7, 1), D(2026, 9, 30), title="Fake draft value", source=104, value=9,
                  due=D(2026, 10, 14))  # fmt: skip
    filters = (*YEAR, "is_blueprint_canonical is True", "section_name is Section Alpha",
               "actual_value is less than or equal to 0 or is blank")  # fmt: skip
    rows = [
        pf.row("dq_list", section="Section Alpha", pd=PD, cp_output="Output One", pd_output="Fake output 1",
               indicator_title="Fake indicator 1", reached=0),  # one of its rows has no value
    ]  # fmt: skip
    upload = pf.compared(
        checker,
        _one("dq_list", rows, filters=filters),
        _one(
            "dq_card",
            [pf.row("dq_card", indicators=1)],
            filters=YEAR[:3] + ("section_name is Section Alpha",),
        ),
    )
    export, lines = _lines(upload, "dq_list")
    found = {line.match[1]: (line.kind, line.reason, line.rule) for line in lines}
    assert found == {
        "Fake indicator 1": ("only_powerbi", "rule", "per_row"),
        "Fake never reported": ("only_neurodb", "rule", "no_report_in_year"),
        "Fake draft value": ("only_neurodb", "rule", "draft_values"),
    }
    (table,) = export.fits
    rows = {r["candidate"]: r for r in table["rows"]}
    assert rows["dq_per_row"]["fits"] >= rows["dq_neurodb"]["fits"]
    _card, (line,) = _lines(upload, "dq_card")
    assert line.found == {"indicators": [1, 2, None]} and (line.reason, line.group) == ("rule", 1)
    # both sides of the gap: two only NeuroDB lists, one only Power BI does
    assert line.detail == "2 more in NeuroDB, 1 more in Power BI, net 1: 3 are counted another way."
    del dm


def test_the_data_quality_card_alone_says_upload_its_list(checker, world):
    upload = pf.compared(checker, _one("dq_card", [pf.row("dq_card", indicators=4)], filters=YEAR))
    _card, (line,) = _lines(upload, "dq_card")
    assert (line.reason, line.group) == ("see_dq_list", 4)


LOCATION_DROP = ("disaggregations", "refreshed", "submitted_on", "location", "period_start", "pd_status", "partner",
                 "section", "emergency")  # fmt: skip
LOCATION_FILTERS = (*YEAR, "is_period_latest is True", "location_value is greater than 0")


def _location(**values):
    base = {"pd": PD, "indicator_title": "Fake indicator 1", "report_status": "Accepted", "target": 100,
            "cp_output": "Output One"}  # fmt: skip
    return pf.row("locations", **{**base, **values})


def test_locations_join_power_bis_copies_and_give_each_difference_its_reason(checker, world):
    from django.utils import timezone

    pd, _ended = world
    # PRP's total repeated on every location of an HR report: no value of its own per location
    for place in ("LB9_9_1", "LB9_9_2"):
        pf.report_row(pd, "HR1", "Accepted", D(2026, 8, 1), D(2026, 8, 31), title="Fake indicator 5", source=105,
                      place=place, achievement_in_period="7", submitted=D(2026, 9, 5))  # fmt: skip
    # two records of QPR2 at the second place: the page reads the larger, the newer says 6
    for value, changed in ((8, datetime.datetime(2026, 7, 1)), (6, datetime.datetime(2026, 7, 20))):
        pf.report_row(pd, "QPR2", "Submitted", D(2026, 4, 1), D(2026, 6, 30), place="LB9_9_2", value=value,
                      submitted=D(2026, 7, 10), last_modify_date=timezone.make_aware(changed))  # fmt: skip
    rows = [
        _location(report="QPR1", p_code="LB9_9_1", reached=10, period_end=D(2026, 3, 31)),
        _location(report="QPR1", p_code="LB9_9_1", reached=10, period_end=D(2026, 3, 31), cp_output="Output, Two"),
        _location(report="QPR1", p_code="LB9_9_1", indicator_title="Fake percentage 2", reached=25, target=0.4,
                  period_end=D(2026, 3, 31)),  # a percentage shown x100 here: a factor of 100 is left
        _location(report="QPR2", p_code="LB9_9_1", reached=20, report_status="Submitted", period_end=D(2026, 6, 30)),
        _location(report="QPR2", p_code="LB9_9_2", reached=6, report_status="Submitted", period_end=D(2026, 6, 30)),
        _location(report="QPR3", p_code="LB9_9_1", reached=3, report_status="Overdue", period_end=D(2026, 9, 30)),
        _location(report="HR1", p_code="LB9_9_1", indicator_title="Fake indicator 5", reached=7,
                  period_end=D(2026, 8, 31)),
        _location(report="HR1", p_code="LB9_9_2", indicator_title="Fake indicator 5", reached=7,
                  period_end=D(2026, 8, 31)),
        _location(report="HR9", p_code="LB9_9_1", reached=1, period_end=D(2026, 9, 30)),
    ]  # fmt: skip
    upload = pf.compared(checker, _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP))
    export, lines = _lines(upload, "locations")
    found = {(line.match[1], line.match[2], line.match[3]): line for line in lines}
    assert (
        found[("QPR1", "Fake percentage 2", "LB9_9_1")].reason,
        found[("QPR1", "Fake percentage 2", "LB9_9_1")].rule,
    ) == ("rule", "units")
    older = found[("QPR2", "Fake indicator 1", "LB9_9_2")]  # NeuroDB holds the newer one too: a rule
    assert (older.reason, older.rule, older.found["reached"][:2]) == ("rule", "several_records", [6.0, 8.0])
    assert (
        older.detail.startswith("NeuroDB holds 2 records of this row")
        and "/datamart/raw/partner_reports/" in older.link
    )
    assert found[("QPR3", "Fake indicator 1", "LB9_9_1")].reason == "owed"
    assert found[("HR1", "Fake indicator 5", "LB9_9_1")].reason == "repeated"
    assert found[("HR9", "Fake indicator 1", "LB9_9_1")].reason == "not_in_read"
    only = found[("QPR1", "Fake indicator 1", "LB9_9_2")]  # NeuroDB's 5 there: not in Power BI's export
    assert only.kind == "only_neurodb"
    notes = " ".join(export.counts["notes"])
    assert (
        "Copies of another location row (Power BI repeats it once per CP output of the PD), joined: 1."
        in notes
    )


DISAGGREGATION_DROP = ("section", "location", "due_date", "refreshed")


def _value(**values):
    base = {"indicator_title": "Fake indicator 1", "p_code": "LB9_9_1", "report": "QPR1", "report_type": "QPR",
            "report_status": "Accepted", "period_start": D(2026, 1, 1), "period_end": D(2026, 3, 31)}  # fmt: skip
    return pf.row("disaggregations", **{**base, **values})


def test_disaggregations_add_up_compare_zeros_and_read_cross_tabs(checker, world):
    from neurodb.datamart import models as dm

    Label = dm.DisaggregationLabel
    Label.objects.create(kind=Label.Kind.TYPE, prp_id=20, label="Nationality")
    Label.objects.create(kind=Label.Kind.VALUE, prp_id=11, label="Lebanese", disaggregation_id=20)
    # the second place's 5 only as a cross-tab: female x Lebanese 2, male x Lebanese 3
    dm.ReportedIndicator.objects.filter(
        p_code="LB9_9_2", report_number="QPR1", indicator="Fake indicator 1"
    ).update(disaggregation={"()": 5, "(1, 11)": 2, "(2, 11)": 3})
    # QPR2 at the first place: a value whose name NeuroDB has not read yet
    dm.ReportedIndicator.objects.filter(report_number="QPR2", p_code="LB9_9_1").update(
        disaggregation={"()": 20, "(3,)": 20}
    )
    rows = [
        _value(disaggregation="Female", reached=3),  # Power BI's two rows of one value add up: 3 + 1
        _value(disaggregation="Female", reached=1, report_status="Accepted"),
        _value(disaggregation="Male", reached=6),
        _value(disaggregation="Other", reached=0),  # NeuroDB has none: 0 against nothing is the same
        _value(p_code="LB9_9_2", disaggregation="Female", reached=2),  # NeuroDB's only in a cross-tab
        _value(report="QPR2", disaggregation="Not read yet", reached=20, report_status="Submitted",
               period_start=D(2026, 4, 1), period_end=D(2026, 6, 30)),
        _value(disaggregation="Female", reached=9, period_start=D(2025, 10, 1), period_end=D(2025, 12, 31)),
    ]  # fmt: skip
    upload = pf.compared(
        checker,
        _one(
            "disaggregations", rows, filters=(*ACTIVE, "is_period_latest is True"), drop=DISAGGREGATION_DROP
        ),
    )
    export, lines = _lines(upload, "disaggregations")
    found = {(line.match[1], line.match[-1]): line for line in lines}
    assert export.set_aside == 1
    assert ("LB9_9_1", "Female") not in found and ("LB9_9_1", "Male") not in found  # 3 + 1 = 4, and 6
    assert export.counts["same_zero"] >= 1
    cross = found[("LB9_9_2", "Female")]
    assert (cross.reason, cross.rule, cross.found["reached"]) == ("rule", "combined", [2.0, None, 2.0])
    unread = found[("LB9_9_1", "Not read yet")]
    assert (unread.kind, unread.reason, unread.detail) == (
        "only_powerbi",
        "not_in_read",
        "NeuroDB has not read this value's name yet.",
    )
    notes = " ".join(export.counts["notes"])
    assert "1 row is about a period before 1 Jan 2026" in notes and "combined table" in notes


def test_funds_compare_amounts_once_per_pd_and_count_outstanding_dct_candidates(checker, world):
    upload = pf.compared(
        checker,
        _one(
            "fund_monitoring",
            [pf.row("fund_monitoring", pd=PD, unicef_planned=9000, cash=12000, partner_contribution=1000,
                    partner=PARTNER, currency="USD", partner_share=0.1),
             pf.row("fund_monitoring", pd=ENDED, unicef_planned=1, cash=1, partner_contribution=0),
             pf.row("fund_monitoring", pd="FAKE/PCA2026099/PD2026099", unicef_planned=1, cash=1, partner_contribution=0)],
            drop=("financial_contribution", "partner_type"),
        ),
        _one(
            "funded_programmes",
            [pf.row("funded_programmes", pd=PD, grant="SC000001", fund_reservation=5000.1, donor="Fake Donor",
                    cash=10000, disbursed=2500, outstanding_dct=300),
             pf.row("funded_programmes", pd=PD, grant="NON-GRANT", fund_reservation=10, cash=10000, disbursed=2500,
                    outstanding_dct=300),
             pf.row("funded_programmes", pd=ENDED, grant="SC000002", fund_reservation=1, cash=1, disbursed=0,
                    outstanding_dct=0)],
            filters=("country_name is Lebanon",),
            drop=("days_to_end", "start", "end", "pd_status", "partner", "currency", "unicef_planned",
                  "partner_contribution", "partner_share"),
        ),
    )  # fmt: skip
    _export, lines = _lines(upload, "fund_monitoring")
    found = {line.match[0]: line for line in lines}
    assert found[PD].found == {
        "cash": [12000.0, 10000.0, None]
    }  # the share follows the amounts: not compared
    assert found[ENDED].reason == "pd_not_running"
    assert found["FAKE/PCA2026099/PD2026099"].reason == "not_in_read"
    export, lines = _lines(upload, "funded_programmes")
    found = {(line.match[0], line.match[1]): line for line in lines}
    assert set(found) == {(PD, "NON-GRANT"), (ENDED, "SC000002")}
    assert (found[(ENDED, "SC000002")].reason, found[(ENDED, "SC000002")].rule) == ("rule", "fr_only")
    (table,) = export.fits
    assert table["title"] == "outstanding_dct" and {r["candidate"]: r["fits"] for r in table["rows"]} == {
        "dct_fr": 1,
        "dct_pd": 1,
    }


def test_power_bis_own_filter_is_read_exactly_and_an_unknown_one_is_named(checker, world):
    upload = pf.compared(
        checker,
        _one("report_list", [_with(REPORTS[0], pd=ENDED, pd_status="Ended", section="Section Alpha")], filters=("country_name is Lebanon", "status is ended")),
        _one("by_sector", [pf.row("by_sector", section="Section Alpha", accepted=0, submitted=0, due=0, overdue=0)],
             filters=(*ACTIVE, "reporting_year is 2025")),
        _one("follow_up", [_follow(status="Submitted", due_date=D(2026, 7, 15))], filters=(*FOLLOW_FILTERS, "fake_field_name is Something")),
    )  # fmt: skip
    export, lines = _lines(upload, "report_list")
    assert [(x.found, x.reason) for x in lines] == [] and export.counts["same"] == 1  # the ended PD
    export, lines = _lines(upload, "by_sector")
    assert lines == [] and export.verdict == "same"
    export, lines = _lines(upload, "follow_up")
    (line,) = (
        lines  # NeuroDB's Overdue report Power BI does not list: the filter NeuroDB cannot apply may say why
    )
    assert (line.kind, line.reason, line.group) == ("only_neurodb", "filter", 4)
    assert line.detail == "Power BI's filter: fake_field_name is …."


def test_an_empty_export_says_how_many_rows_neurodbs_page_has(checker, world):
    from neurodb.datamart import powerbi_results

    upload = pf.compared(checker, _one("report_list", []))
    export = PowerBIExport.objects.get(upload=upload)
    assert export.state == "empty" and export.counts == {"neurodb_rows": 3}
    assert (
        powerbi_results.verdict_words(export)[0]
        == "Empty in Power BI: NeuroDB's page has 3 rows for this filter"
    )


# ----------------------------------------------------------------------------------------- when the same
def test_two_values_are_the_same_on_raw_values_only():
    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_match as pm

    assert pm.same(px.MONEY, 1000.1 + 1e-9, "1000.10")  # float noise is the same amount
    assert not pm.same(px.NUMBER, 12.4, 12)  # no rounding: 12.4 is not 12
    assert pm.same(px.NUMBER, 0.1 + 0.2, 0.3)
    assert pm.same(px.NUMBER, 0, None, zero_is_none=True) and not pm.same(px.NUMBER, 0, None)
    assert pm.same(px.DATE, "2026-03-31", datetime.datetime(2026, 3, 31, 23, 59))
    assert pm.same(px.TEXT, "Fake  indicator", "fake indicator")
    assert pm.same(px.TEXT, "Fake indicator x", "Fake indicator xyz", cut=True)
    assert not pm.same(px.TEXT, "Fake indicator xyz", "Fake indicator x", cut=True)
    known = ("WASH / Water, Sanitation and Hygiene", "Section Alpha")
    assert pm.sections("WASH / Water, Sanitation and Hygiene ,section alpha ", known) == pm.sections(
        ["Section Alpha", "WASH / Water, Sanitation and Hygiene"], known
    )
    assert pm.cp_outputs_fit("Output One, Output, Two", ["Output, Two", "Output One"])
    assert not pm.cp_outputs_fit("Output One, Output, Two, Output Three", ["Output, Two", "Output One"])
    assert pm.pd_base("FAKE/PCA2026001/PD2026001-2") == pm.pd_base("fake/pca2026001/pd2026001")
    assert pm.partner("FAKEORG PARTNER 1") == pm.partner("Fakeorg Partner 1 (eTools 12)")
    assert pm.document_type("SPD") == pm.document_type("SHPD") != pm.document_type("PD")
    assert pm.grant("NON-GRANT") == pm.grant("") == pm.grant("Non Grant")
    assert pm.report_number("3", "QPR") == "QPR3" == pm.report_number("qpr3", "QPR")


def test_pairing_puts_the_most_similar_rows_together():
    from neurodb.datamart import powerbi_match as pm

    powerbi = [{"key": "a", "target": 10}, {"key": "a", "target": 40}, {"key": "b", "target": 1}]
    neurodb = [{"key": "a", "target": 41}, {"key": "a", "target": 9}, {"key": "c", "target": 1}]
    pairs, left_pb, left_nd = pm.pair(
        powerbi,
        neurodb,
        lambda r: r["key"],
        lambda r: r["key"],
        lambda p, n: (pm.closeness(p["target"], n["target"]),),
    )
    assert {(p["target"], n["target"]) for p, n in pairs} == {(10, 9), (40, 41)}
    assert left_pb == [{"key": "b", "target": 1}] and left_nd == [{"key": "c", "target": 1}]


# ------------------------------------------------------------------------------------ nothing else changes
def test_a_comparison_changes_no_figure_of_neurodbs_pages_and_writes_nothing_else(checker, world):
    from django.apps import apps
    from django.db import connection

    from neurodb.core.models import SyncRun
    from neurodb.datamart import funds, monitoring, reporting, services
    from neurodb.datamart import powerbi_kinds as kinds

    day = D(2026, 10, 15)

    def figures():
        page = services.partner_reporting({"pd_status": ["active"]}, day, options=False)
        filters = monitoring.Filters.from_params(
            {"pd_status": ["active"], "year": "all"}, day, default_year=False
        )
        rows = monitoring.as_rows(monitoring.indicators(filters, day))
        found = reporting.location_rows(page["rows"], page["statuses"], day)["rows"]
        recorded = reporting.location_rows(page["rows"], page["statuses"], day, record=kinds.Recorder())[
            "rows"
        ]
        return repr(
            (
                page["counts"],
                page["summary"],
                page["by_section"],
                page["by_month"],
                page["partners"]["by_status"],
                [(r.ident, r.reached, r.target, r.achieved) for r in rows],
                monitoring.data_quality(rows, filters, day)["no_reached_count"],
                [(r["programme_document"], r["value_in_period"]) for r in found],
                [(r["programme_document"], r["value_in_period"]) for r in recorded],
                [
                    (r["pd_id"], r["total"], r["partner_share"])
                    for r in funds.partner_contributions(funds.FundFilters(), day)["rows"]
                ],
                [
                    (r["pd_id"], r["grant"], r["reserved"])
                    for r in funds.funded_programmes(funds.FundFilters(), day)["rows"]
                ],
            )
        )

    def tables():
        out = {}
        with connection.cursor() as cursor:
            for model in apps.get_models():
                table = model._meta.db_table
                if table.startswith("datamart_powerbi") or model._meta.proxy or not model._meta.managed:
                    continue
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608 - the models' own tables
                out[table] = cursor.fetchone()[0]
        return out

    before, counted = figures(), tables()
    runs = SyncRun.objects.count()
    upload = pf.compared(checker, *[_one(k, rows) for k, rows in (("report_list", REPORTS),)])
    assert upload.status == PowerBIUpload.Status.DONE
    assert figures() == before  # the page figures are the same before and after a comparison
    after = tables()
    changed = {t for t in counted if after.get(t) != counted[t]}
    assert changed <= {"accounts_user", "auth_group", "django_session"}, changed  # the checker's sign-in only
    assert SyncRun.objects.count() == runs


@pytest.mark.parametrize("year", ["2026", "all"])
def test_reading_each_rows_total_alone_gives_the_pages_figures(world, year):
    """The comparison reads each partner report row's disaggregation as its total alone, never its narrative
    (``monitoring.indicators(totals=True)``): every figure Partner monitoring shows is the same; only Reached by
    disaggregation, which the comparison never reads, is left out."""
    from neurodb.datamart import monitoring

    day = D(2026, 10, 15)
    filters = monitoring.Filters.from_params({"pd_status": [], "year": year}, day, default_year=False)
    page = {e.ident: e for e in monitoring.indicators(filters, day)}
    light = {e.ident: e for e in monitoring.indicators(filters, day, totals=True)}
    assert page and page.keys() == light.keys()
    names = (
        "reached", "reached_in_year", "in_year_item", "to_quarter", "months", "years", "by_location", "reports",
        "awaiting", "awaiting_reports", "as_of", "prp_cumulative", "mismatch", "value_status", "partial",
        "report_type", "partner_status", "report_status", "tracking", "achieved", "method", "period_method",
        "ratio", "percentage", "rows_placed", "placed_by",
    )  # fmt: skip
    for ident, entry in page.items():
        assert [getattr(light[ident], n) for n in names] == [getattr(entry, n) for n in names], ident
    keys = [{k for found in e.result.disaggregation.values() for k in found} for e in page.values()]
    assert {"()", "(1,)", "(2,)"} in keys  # Akkar's 10 by sex, on the page alone
    assert all(set(found) <= {"()"} for e in light.values() for found in e.result.disaggregation.values())


def test_why_a_power_bi_row_has_no_indicator_on_the_page_never_reads_a_report(world):
    """``_titles`` reads the indicators of the PDs holding the titles alone, never their partner reports: their
    Reached is never needed (production's renewed PDs share most titles, and their reports read whole took the
    job past its memory)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from neurodb.datamart import powerbi_facts, powerbi_filters, powerbi_kinds

    context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    with CaptureQueriesContext(connection) as queries:
        found = powerbi_kinds._titles(
            context, powerbi_filters.Filter(), ["Fake indicator 1", "Fake percentage 2"]
        )
    assert {n["pd"] for rows in found.values() for n in rows} == {PD}
    assert all(n["reached"] is None and n["partial"] is False for rows in found.values() for n in rows)
    assert not [q for q in queries.captured_queries if "datamart_reportedindicator" in q["sql"]]


def test_compare_again_compares_the_same_rows_with_neurodbs_newest_data(checker, world):
    from django.urls import reverse

    from neurodb.datamart import powerbi_compare as pc

    pd, _ended = world
    rows = [*REPORTS, _with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30))]
    upload = pf.compared(checker, _one("report_list", rows))
    export, lines = _lines(upload, "report_list")
    assert [line.match[1] for line in lines] == ["QPR9"]
    # the next sync brings the report: Compare again finds it
    pf.report_row(pd, "QPR9", "Accepted", D(2026, 4, 1), D(2026, 4, 30), value=1, submitted=D(2026, 5, 2),
                  due=D(2026, 4, 15))  # fmt: skip
    response = checker.post(reverse("admin:datamart_powerbi_again", args=[upload.pk]))
    assert response.status_code == 302
    pc.run(upload.pk)
    export, lines = _lines(upload, "report_list")
    assert lines == [] and export.counts["same"] == 4 and export.verdict == "same"
    page = checker.get(reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk])).content.decode()
    link = page.split("data-pbi-neurodb-page", 1)[1].split("</p>", 1)[0]
    assert 'href="/partner-reporting/?pd_status=active"' in link  # Power BI's filter, on NeuroDB's own page


def test_a_month_above_the_tabs_limit_is_read_pd_by_pd_with_the_same_result(world, monkeypatch):
    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_facts, powerbi_filters, powerbi_kinds, powerbi_match, reporting

    def read():
        context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
        parsed = powerbi_filters.parse("Applied filters:\nstatus is active")
        job = powerbi_kinds.Job(
            px.KINDS["disaggregations"], [], None, parsed, context, powerbi_match.Result(10)
        )
        data = powerbi_kinds.page.reporting(parsed, context)
        return powerbi_kinds._neurodb_disaggregations(job, data, "2026-03", True, True)

    from neurodb.partnerships.models import PCA

    pd, _ended = world
    other = PCA.objects.create(
        etl_id="fake-pd4", partner=pd.partner, partner_name=pd.partner_name, number="FAKE/PCA2026004/PD2026004",
        title="Fake programme 4", status="active", start=D(2026, 1, 1), end=D(2026, 12, 31),
    )  # fmt: skip
    pf.report_row(other, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), source=401,
                  disaggregation={"()": 2, "(1,)": 2}, submitted=D(2026, 4, 3))  # fmt: skip
    whole = read()
    female = next(v for k, v in whole[0].items() if k[-1] == "female")
    assert female["reached"] == 6.0  # Power BI's export has no PD #: two PDs' values of one label add up
    monkeypatch.setattr(
        reporting, "DISAGGREGATION_LIMIT", 3
    )  # March's 4 rows are too many: PD by PD (3 and 1)
    assert read() == whole  # the same values, read in parts
    monkeypatch.setattr(reporting, "DISAGGREGATION_LIMIT", 0)  # even one PD is too large: said, never guessed
    found, _shared, _unlabelled = read()
    assert found == {}


# ------------------------------------------------------------------------------- stage 2 check: reasons
def test_a_program_performance_row_of_a_pd_older_than_neurodbs_history_is_expected(checker, world):
    """Power BI keeps the indicators with an Accepted report: a PD that started before NeuroDB's history, whose
    Accepted reports are all older than it, is expected; a PD that started after it, with no Accepted report in
    NeuroDB, still needs a look and says so truly."""
    from neurodb.partnerships.models import PCA

    pd, _ended = world
    pf.sync(datetime.datetime(2026, 10, 15, 8, 0))  # after Power BI's refresh
    for number, start, source, title in (
        ("FAKE/PCA2025009/PD2025009", D(2025, 6, 1), 909, "Fake old indicator"),
        ("FAKE/PCA2026008/PD2026008", D(2026, 2, 1), 908, "Fake new indicator"),
    ):
        found = PCA.objects.create(
            etl_id=f"fake-{source}", partner=pd.partner, partner_name=pd.partner_name, number=number,
            title=f"Fake programme {source}", status="active", document_type="PD", start=start,
            end=D(2026, 12, 31), section_names=["Section Alpha"], cp_outputs=["Output One"],
        )  # fmt: skip
        _indicator(found, source, title)
        # its 2026 report is still Submitted (the old PD's Accepted ones are from 2025)
        pf.report_row(found, "QPR3", "Submitted", D(2026, 1, 1), D(2026, 3, 31), title=title, source=source,
                      value=5, submitted=D(2026, 4, 10))  # fmt: skip
    rows = [
        _pp(section="Section Alpha", cp_output="Output One", indicator_title=title, target=100, reached=50,
            achieved=0.5)
        for title in ("Fake old indicator", "Fake new indicator")
    ]  # fmt: skip
    upload = pf.compared(
        checker,
        _one("program_performance", rows, filters=PP_FILTERS),
        refreshed=datetime.datetime(2026, 10, 14, 6, 0),
    )
    _export, lines = _lines(upload, "program_performance")
    found = {line.match[1]: line for line in lines if line.kind == "only_powerbi"}
    old = found["Fake old indicator"]
    assert (old.reason, old.group, old.detail) == (
        "before_history",
        1,
        "The Datamart holds no earlier partner report.",
    )
    new = found["Fake new indicator"]
    assert (new.reason, new.group) == ("not_in_read", 4)
    assert new.detail == "NeuroDB has this indicator, but none of its reports is Accepted there."


def test_a_report_further_along_in_neurodb_after_power_bis_refresh_has_moved_on(checker, world):
    """NeuroDB's sync ran after Power BI's refresh and NeuroDB has a report further along: it moved on since
    (expected). A report Power BI counts as Overdue on its due date, the day the statuses are read for, is
    said so; "a different day" only when NeuroDB reads the statuses for another day than Power BI's refresh."""
    from neurodb.datamart import powerbi_words as words

    pf.sync(datetime.datetime(2026, 10, 15, 8, 0))
    rows = [
        _with(REPORTS[0], report_status="Submitted"),  # NeuroDB has it Accepted
        REPORTS[1],
        REPORTS[2],  # Overdue on 14 Oct, its due date: NeuroDB's Due until the day after
    ]
    upload = pf.compared(checker, _one("report_list", rows), refreshed=datetime.datetime(2026, 10, 14, 6, 0))
    export, lines = _lines(upload, "report_list")
    assert upload.day == D(2026, 10, 14)
    found = {line.match[1]: line for line in lines}
    moved = found["QPR1"]
    assert (moved.kind, moved.reason, moved.group) == ("different", "moved_on", 1)
    assert moved.detail == "NeuroDB's sync ran after Power BI's refresh: the report moved on since."
    owed = found["QPR3"]
    assert (owed.reason, owed.group, owed.found["report_status"][:2]) == ("status", 1, ["Overdue", "Due"])
    assert owed.detail == (
        "Power BI counts a report as Overdue on its due date; NeuroDB counts it as Due until the day after."
    )
    assert export.verdict != "look"
    # Power BI refreshed more than 14 days ago: NeuroDB reads the statuses for today, another day
    rows = [REPORTS[0], REPORTS[1], _with(REPORTS[2], report_status="Due")]
    upload = pf.compared(checker, _one("report_list", rows), refreshed=datetime.datetime(2026, 9, 20, 6, 0))
    _export, (line,) = _lines(upload, "report_list")
    assert upload.day == D(2026, 10, 15) and line.found["report_status"][:2] == ["Due", "Overdue"]
    assert (line.reason, line.detail) == ("status", words.DETAIL_OTHER_DAY)


def test_power_bis_total_row_is_checked_as_a_note_never_as_a_line(checker, world):
    from neurodb.datamart import powerbi_exports as px

    rows = [
        pf.row("by_sector", section=SECTIONS, accepted=2, submitted=1, due=0, overdue=1),
        pf.row("by_sector", section="Total", accepted=2, submitted=1, due=0, overdue=1),
    ]
    upload = pf.compared(checker, _one("by_sector", rows, filters=YEAR))
    export, lines = _lines(upload, "by_sector")
    assert [(line.match, line.found) for line in lines] == [([SECTIONS], {"accepted": [2.0, 1, None]})]
    assert export.counts["groups"] == {"4": 1} and upload.summary["look"] == 1
    caption = px.KINDS["by_sector"].column("accepted").caption
    assert (
        f"Power BI's Total differs from NeuroDB's in {caption}: see the sections' lines."
        in (export.counts["notes"])
    )


def test_two_records_of_one_location_neurodb_holds_both_of_are_a_rule_not_a_wait(checker, world, monkeypatch):
    """The record Power BI shows is already in NeuroDB: waiting for the next sync changes nothing. Power BI's
    own field in the records is said to be unused by NeuroDB's pages, never "applied"."""
    from django.utils import timezone

    from neurodb.datamart import models as dm
    from neurodb.datamart import powerbi_compare as pc

    pd, _ended = world
    for value, changed, latest in (
        (8, datetime.datetime(2026, 7, 1), False),
        (6, datetime.datetime(2026, 7, 20), True),
    ):
        pf.report_row(pd, "QPR2", "Submitted", D(2026, 4, 1), D(2026, 6, 30), place="LB9_9_2", value=value,
                      submitted=D(2026, 7, 10), last_modify_date=timezone.make_aware(changed),
                      data={"is_period_latest": latest})  # fmt: skip
    dm.ReportedIndicator.objects.exclude(data__has_key="is_period_latest").update(
        data={"is_period_latest": True}
    )
    monkeypatch.setattr(pc, "fields_in_records", lambda run: {"is_period_latest"})
    rows = [_location(report="QPR2", p_code="LB9_9_2", reached=6, report_status="Submitted",
                      period_end=D(2026, 6, 30))]  # fmt: skip
    upload = pf.compared(checker, _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP))
    export, lines = _lines(upload, "locations")
    clauses = {c["words"]: c["state"] for c in export.clauses}
    assert clauses["is_period_latest is True"] == "as_read"
    assert (
        "Power BI's field is_period_latest is in the records NeuroDB reads, but NeuroDB's pages do not use "
        "it: NeuroDB reads it as within one report, the newest row of each location; Power BI also keeps one "
        "report per period end." in export.notes
    )
    (line,) = [line for line in lines if line.match[1] == "QPR2" and line.match[3] == "LB9_9_2"]
    assert (line.reason, line.rule, line.group, line.found["reached"][:2]) == (
        "rule",
        "several_records",
        1,
        [6.0, 8.0],
    )
    assert line.detail == "NeuroDB holds 2 records of this row; Power BI's value is the newest one's."
    # NeuroDB's sync ran before Power BI's refresh: the next one may bring the change
    pf.sync(datetime.datetime(2026, 10, 13, 20, 30))
    upload = pf.compared(
        checker,
        _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        refreshed=datetime.datetime(2026, 10, 14, 6, 0),
    )
    _export, lines = _lines(upload, "locations")
    (line,) = [line for line in lines if line.match[1] == "QPR2" and line.match[3] == "LB9_9_2"]
    assert (line.reason, line.group) == ("older_version", 2)
    assert line.detail == "NeuroDB holds 2 records of this row; the page used one that is not the newest."


# ---------------------------------------------------------------------------- stage 2 check: what it says
def test_an_export_with_only_reports_still_owed_says_so_not_counted_another_way(checker, world):
    from neurodb.datamart import powerbi_results, powerbi_words

    owed = [
        _with(REPORTS[0], report=f"HR{n}", report_status="Overdue", period_start=D(2026, 8, 1),
              period_end=D(2026, 8, 31))
        for n in (1, 2, 3)
    ]  # fmt: skip
    upload = pf.compared(checker, _one("report_list", [*REPORTS, *owed]))
    export = PowerBIExport.objects.get(upload=upload)
    assert export.verdict == "not_shown"  # reports still owed: NeuroDB cannot show them yet, never expected
    assert powerbi_results.verdict_words(export) == (
        "NeuroDB cannot show 3 rows yet",
        powerbi_words.GROUP_ACTIONS[5],
    )
    page = next(p for p in upload.summary["pages"] if p["name"] == "PD/SPD Report")
    assert page["verdict"] == "NeuroDB cannot show 3 rows yet"
    assert not powerbi_results.settled(export)
    # an earlier upload wrote its reports still owed as expected: its verdict still reads so
    earlier = PowerBIExport(state="compared", counts={"groups": {"1": 3}, "reasons": {"owed": 3}})
    assert powerbi_results.verdict_words(earlier) == (
        "Same, apart from reports still owed: 3",
        "Nothing to fix. The partners have not sent these reports yet.",
    )
    mixed = PowerBIExport(
        state="compared",
        counts={"groups": {"1": 6}, "reasons": {"owed": 3, "rule": 2, "before_history": 1}},
    )
    assert powerbi_results.verdict_code(mixed) == "counted"
    assert powerbi_results.verdict_words(mixed) == (
        "Same data, counted another way; also reports still owed: 3; rows before NeuroDB's history: 1",
        "Nothing to fix. See Choices for you if NeuroDB should count Power BI's way. The partners have not "
        "sent these reports yet. NeuroDB holds no partner report before its history.",
    )


def test_a_line_s_detail_stays_whole_when_every_rule_fits():
    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_facts, powerbi_filters, powerbi_kinds
    from neurodb.datamart import powerbi_match as pm
    from neurodb.datamart import powerbi_words as words

    context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    job = powerbi_kinds.Job(
        px.KINDS["program_performance"], [], None, powerbi_filters.Filter(), context, pm.Result(10)
    )
    candidates = dict.fromkeys(powerbi_facts.REACHED_CANDIDATES, 15.0)
    reason, rule, detail = powerbi_kinds.candidate_reason(
        job, "reached", 15.0, {"reached": 35.0, "report_type": "QPR"}, candidates, "qpr_sum"
    )
    assert (reason, rule) == ("rule", "reached") and len(detail) <= 300
    assert detail.startswith("Counted Power BI's way (adds up the Accepted quarterly reports")
    others = [c for c in powerbi_facts.REACHED_CANDIDATES if c != "qpr_sum"]
    assert all(words.CANDIDATE_SHORT[c] in detail for c in others), detail
    assert detail.endswith(".")
    # a detail too long is cut on a word, never inside one
    kept = pm.Line("different", "unknown", 4, detail="Fakeword " * 60).as_json()[4]
    assert len(kept) <= 300 and kept.endswith("…") and kept[:-1].split()[-1] == "Fakeword"


def test_the_upload_s_line_limit_drops_expected_lines_first_and_never_links_an_empty_list(
    checker, world, monkeypatch
):
    """The report list is compared first, but its expected lines give way to a later export's lines that need a
    look; a needs-a-look line left unwritten is said so, and no link opens an empty list."""
    from neurodb.datamart import powerbi_compare as pc
    from neurodb.datamart import powerbi_results

    owed = [
        _with(REPORTS[0], report=f"HR{n}", report_status="Overdue", period_start=D(2026, 8, 1),
              period_end=D(2026, 8, 31))
        for n in (1, 2, 3)
    ]  # fmt: skip
    funds = [pf.row("fund_monitoring", pd=PD, unicef_planned=1.0, cash=2.0, partner_contribution=3.0)]
    monkeypatch.setattr(pc, "UPLOAD_LINE_LIMIT", 3)
    upload = pf.compared(checker, _one("report_list", [*REPORTS, *owed]), _one("fund_monitoring", funds))
    reports, report_lines = _lines(upload, "report_list")
    fund, fund_lines = _lines(upload, "fund_monitoring")
    assert [line.group for line in fund_lines] == [4]
    assert [line.group for line in report_lines] == [5, 5] and reports.counts["written"] == 2
    assert "Differences counted but not listed: 1." in reports.counts["notes"]
    exports = list(pc.exports_of(upload))
    (look, *_rest) = powerbi_results.todo(upload, exports, [])
    assert look["text"].startswith("Needs a look: 1 difference.") and "not listed" not in look["text"]
    assert look["url"] == powerbi_results.export_url(upload, fund, group=4)
    # one line of room: the report list's line that needs a look is written, the fund's is said not listed
    pf.sync(datetime.datetime(2026, 10, 15, 8, 0))  # after Power BI's refresh: a row not read needs a look
    monkeypatch.setattr(pc, "UPLOAD_LINE_LIMIT", 1)
    missing = _with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30))
    upload = pf.compared(
        checker,
        _one("report_list", [*REPORTS, missing, *owed]),
        _one("fund_monitoring", funds),
        refreshed=datetime.datetime(2026, 10, 14, 6, 0),
    )
    reports, report_lines = _lines(upload, "report_list")
    fund, fund_lines = _lines(upload, "fund_monitoring")
    assert [(line.group, line.match[1]) for line in report_lines] == [(4, "QPR9")] and fund_lines == []
    exports = list(pc.exports_of(upload))
    (look, *_rest) = powerbi_results.todo(upload, exports, [])
    assert look["text"] == (
        "Needs a look: 2 differences. Download the Excel file and send it to the NeuroDB team. 1 of them is "
        "counted but not listed (too many differences at once)."
    )
    assert look["url"] == powerbi_results.export_url(upload, reports, group=4)
    funds_page = next(
        p for p in powerbi_results.page_lines(upload, exports) if p["name"] == "Partner Fund Monitoring"
    )
    assert funds_page["look"] == 1 and funds_page["look_url"] == ""


QUERY_ROWS = {"indicator_title": "Fake indicator 1", "partner": "FAKEORG PARTNER 1", "target": 100, "reached": 10,
              "grant": "SC1", "report": "QPR1", "period_end": "2026-03-31", "report_status": "Accepted",
              "p_code": "LB9_9_1", "pd_output": "Fake output 1", "status": "Overdue", "due_date": "2026-04-15",
              "disaggregation": "Female", "period_start": "2026-01-01", "unicef_planned": 9000, "cash": 10000,
              "section": "Section Alpha", "month": "Mar 2026", "accepted": 1, "indicators": 1}  # fmt: skip


def _pds(n):
    """``n`` running PDs, each with an indicator, an Accepted report, a funds reservation and its header."""
    from decimal import Decimal

    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.create(
        etl_id=f"fake-q{n}", name="Fakeorg Partner 1", vendor_number=f"V-Q{n}",
        partner_type="Civil Society Organization", cso_type="National",
    )  # fmt: skip
    out = []
    for i in range(n):
        pd = PCA.objects.create(
            etl_id=f"fake-q{n}-{i}", partner=partner, partner_name="Fakeorg Partner 1",
            number=f"FAKE/PCA2026{n:02}{i:03}/PD", title=f"Fake programme {i}", status="active", document_type="PD",
            start=D(2026, 1, 1), end=D(2026, 12, 31), section_names=["Section Alpha"], cp_outputs=["Output One"],
            offices_set=["Office North"], total_unicef_budget="9000", total_budget="10000", cso_contribution="1000",
            budget_currency="USD", frs_total_outstanding_amt="300",
        )  # fmt: skip
        _indicator(pd, 500 + i, "Fake indicator 1", cp_output="Output One", unit="number")
        pf.report_row(pd, "QPR1", "Accepted", D(2026, 1, 1), D(2026, 3, 31), source=500 + i, value=10,
                      submitted=D(2026, 4, 2))  # fmt: skip
        dm.FundsReservation.objects.create(
            datamart_id=20_000 * n + i, intervention=pd, fr_number=f"FR-Q{n}-{i}", grant_number="SC1",
            donor="Fake Donor", overall_amount=Decimal("5000"),
        )  # fmt: skip
        dm.FundsReservationHeader.objects.create(
            datamart_id=30_000 * n + i, intervention=pd, fr_number=f"FR-Q{n}-{i}", actual_amt=Decimal("2500"),
            outstanding_amt=Decimal("300"),
        )  # fmt: skip
        out.append(pd)
    return out


def _queries(kind, pds):
    import gzip
    import io
    import json

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_facts, powerbi_filters, powerbi_kinds
    from neurodb.datamart import powerbi_match as pm

    names = [c.name for c in px.KINDS[kind].columns]
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as stream:
        for pd in pds:
            row = {**QUERY_ROWS, "pd": pd.number}
            stream.write((json.dumps([row.get(n) for n in names]) + "\n").encode())
    parsed = powerbi_filters.parse("Applied filters:\ncountry_name is Lebanon\nstatus is active")
    context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    job = powerbi_kinds.Job(px.KINDS[kind], names, buffer.getvalue(), parsed, context, pm.Result(1000),
                            rows_read=len(pds))  # fmt: skip
    with CaptureQueriesContext(connection) as found:
        powerbi_kinds.COMPARERS[kind](job)
    return len(found.captured_queries)


@pytest.mark.parametrize("kind", sorted(powerbi_exports.KINDS))
def test_each_comparer_reads_as_many_queries_for_12_pds_as_for_3(kind, history):
    """No query per row or per PD: a comparer's queries do not grow with NeuroDB's PDs."""
    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    counted = []
    for n in (3, 12):
        for model in (dm.ReportedIndicator, dm.PDIndicator, dm.FundsReservation, dm.FundsReservationHeader, PCA,
                      PartnerOrganization):  # fmt: skip
            model.objects.all().delete()
        counted.append(_queries(kind, _pds(n)))
    assert counted[0] == counted[1], counted


# ----------------------------------------------------------------- the year, the filter by kind, cut exports
PD_INDICATOR_BASE = {"pd_status": "Active", "partner": PARTNER, "document_type": "PD", "pd_title": "Fake programme 1",
                     "end": D(2026, 12, 31), "offices": "Office North", "cp_output": "Output One",
                     "section": "Section Alpha"}  # fmt: skip


def test_a_reporting_year_compares_reached_in_that_year(checker, world):
    """Power BI's Reporting Year reads Reached in that year: a PD older than NeuroDB's history has its whole
    2026 in NeuroDB, so Power BI's 2026 Reached is the same as NeuroDB's (to date it would be PRP's
    cumulative)."""
    from neurodb.datamart import models as dm

    pd, _ended = world
    pd.start = D(2025, 6, 1)
    pd.save(update_fields=["start"])
    dm.ReportedIndicator.objects.filter(intervention=pd, indicator="Fake indicator 1").update(
        total_cumulative_progress="90"
    )
    performance = [
        _pp(indicator_title="Fake indicator 1", target=100, reached=35, achieved=0.35),  # 10 + 5 + 20 in 2026
        _pp(indicator_title="Fake percentage 2", target=40, reached=25, achieved=0.625),
    ]
    indicators = [
        pf.row("pd_indicators", pd=PD, indicator_title="Fake indicator 1", target=100, reached=35,
               start=D(2025, 6, 1), **PD_INDICATOR_BASE),
    ]  # fmt: skip
    upload = pf.compared(
        checker, _one("program_performance", performance, filters=YEAR), _one("pd_indicators", indicators, filters=YEAR)
    )  # fmt: skip
    export, lines = _lines(upload, "program_performance")
    assert lines == [] and export.counts["same"] == 2, [(x.reason, x.rule, x.found) for x in lines]
    assert {c["words"]: c["state"] for c in export.clauses}["reporting_year is 2026"] == "applied"
    export, lines = _lines(upload, "pd_indicators")
    assert not [x for x in lines if x.match[1] == "Fake indicator 1"], [(x.reason, x.found) for x in lines]
    # Programmes → Indicators has no year: the comparison applies it to Reached itself
    assert {c["words"]: c["state"] for c in export.clauses}["reporting_year is 2026"] == "by_comparison"


def test_a_clause_the_kind_s_comparer_does_not_apply_is_one_neurodb_cannot_apply(checker, world):
    """The report list cut to Accepted reports is compared with NeuroDB's Accepted reports; a year and a
    section on the funds pages (which have neither), or several years where one year's Reached is read, are
    filters NeuroDB cannot apply: a row they leave out is said so, and What to do now asks to clear them."""
    from django.urls import reverse

    funds = [
        pf.row("fund_monitoring", pd="FAKE/PCA9/PD9", unicef_planned=1.0, cash=2.0, partner_contribution=3.0)
    ]
    performance = [_pp(indicator_title="Fake indicator 1", target=100, reached=35, achieved=0.35)]
    upload = pf.compared(
        checker,
        _one("report_list", [REPORTS[0]], filters=(*ACTIVE, "report_status_label is Accepted")),
        _one("fund_monitoring", funds, filters=(*YEAR, "section_name is Section Alpha")),
        _one("program_performance", performance, filters=(*ACTIVE, "reporting_year is 2025 or 2026")),
    )  # fmt: skip
    export, lines = _lines(upload, "report_list")
    assert lines == [] and export.verdict == "same"  # NeuroDB's QPR2 and QPR3 are not Accepted
    assert {c["words"]: c["state"] for c in export.clauses}["report_status_label is Accepted"] == "applied"
    export, lines = _lines(upload, "fund_monitoring")
    states = {c["words"]: c["state"] for c in export.clauses}
    assert states["reporting_year is 2026"] == states["section_name is Section Alpha"] == "cannot"
    only = next(x for x in lines if x.kind == "only_neurodb")
    assert only.reason == "filter" and only.detail == "Power BI's filter: reporting_year is 2026."
    from neurodb.datamart import powerbi_admin, powerbi_filters

    assert powerbi_admin._covers(powerbi_filters.Filter.from_json(export.filters)) == "Every year"
    export, _lines_ = _lines(upload, "program_performance")
    assert {c["words"]: c["state"] for c in export.clauses}["reporting_year is 2025 or 2026"] == "cannot"
    page = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    # the funds page has no year or section slicer: its filters are in the Filters pane
    assert (
        "the Reporting Year slicer; the reporting_year filter in the Filters pane; the section_name filter in "
        "the Filters pane"
    ) in page


def test_an_export_power_bi_may_have_cut_says_so_and_explains_no_counted_visual(checker, world):
    """A report list of exactly 1,000 rows: NeuroDB's reports beyond it may be beyond Power BI's cut (export
    it again in parts, nothing to send), and a counted visual's gap is never said to be under a filter."""
    others = [_with(REPORTS[0], pd=f"FAKE/PCA9{n:03}/PD9{n:03}", section="Section Gamma") for n in range(999)]
    # the table counts more reports of the year than the list holds (1,500 Overdue): it may be beyond the cut
    upload = pf.compared(
        checker,
        _one("report_list", [REPORTS[0], *others]),
        _one("by_sector", [pf.row("by_sector", section=SECTIONS, accepted=1, submitted=0, due=0, overdue=1500)],
             filters=YEAR),
    )  # fmt: skip
    export, lines = _lines(upload, "report_list")
    assert export.rows_read == 1_000
    cut = [x for x in lines if x.kind == "only_neurodb"]
    assert {x.match[1] for x in cut} == {"QPR2", "QPR3"}
    assert all((x.reason, x.group) == ("cut", 4) for x in cut)
    from neurodb.datamart import powerbi_results, powerbi_words

    todo = powerbi_results.what_to_do(cut[0], "1 Jan 2026")
    assert todo == powerbi_words.REASON_ACTIONS["cut"] and "send it to the NeuroDB team" not in todo
    assert "clear that slicer" not in todo
    assert export.verdict == "cut"
    # the table by sector is not cut itself (one row): its gap waits for the report list exported in parts
    sector, lines = _lines(upload, "by_sector")
    assert {(x.reason, x.group, x.detail) for x in lines} == {
        ("list_cut", 4, powerbi_words.DETAIL_LIST_CUT.format(n=1_000))
    }
    todo = powerbi_results.what_to_do(lines[0], "1 Jan 2026")
    assert todo == powerbi_words.REASON_ACTIONS["list_cut"] and "PD/SPD Report" in todo
    assert sector.verdict == "list_cut"
    verdict, action = powerbi_results.verdict_words(sector)
    assert "this export" not in verdict and "1 rows" not in verdict and "PD/SPD Report" in verdict
    assert action == powerbi_words.REASON_ACTIONS["list_cut"]
    # What to do now: one export may be cut (the report list), and nothing to send for the table by sector
    exports = list(PowerBIExport.objects.filter(upload=upload).order_by("pk"))
    texts = [x["text"] for x in powerbi_results.todo(upload, exports, [])]
    assert powerbi_words.TODO_CUT_ONE in texts, texts
    assert not any(t.startswith("Needs a look") or "2 exports" in t for t in texts), texts


def test_neurodbs_values_in_a_month_or_a_status_power_bi_has_no_column_for_are_compared(checker, world):
    """NeuroDB's disaggregation values of a month Power BI's export has no row for, and its reports in a status
    Power BI's month chart has no column for, are only in NeuroDB: never "Same"."""
    from neurodb.datamart import models as dm

    dm.ReportedIndicator.objects.filter(report_number="QPR2", p_code="LB9_9_1").update(
        disaggregation={"()": 20, "(1,)": 8, "(2,)": 12}
    )
    march = [_value(disaggregation="Female", reached=4), _value(disaggregation="Male", reached=6)]
    months = [
        pf.row("by_month", month="Mar 2026", accepted=1, submitted=0),
        pf.row("by_month", month="Jun 2026", accepted=0, submitted=1),
    ]
    upload = pf.compared(
        checker,
        _one("disaggregations", march, filters=(*ACTIVE, "is_period_latest is True"), drop=DISAGGREGATION_DROP),
        _one("by_month", months, filters=YEAR, drop=("overdue", "due", "not_yet_due", "sent_back")),
    )  # fmt: skip
    export, lines = _lines(upload, "disaggregations")
    june = {x.match[-1]: x for x in lines if x.match[3] == "2026-06-30"}
    assert june["male"].kind == "only_neurodb" and june["male"].found == {"reached": [None, 12.0, None]}
    assert export.verdict != "same"
    export, lines = _lines(upload, "by_month")
    (line,) = lines  # Sep: NeuroDB's Overdue report against Power BI's 0
    assert (line.match, line.found) == (["Sep 2026"], {"overdue": [None, 1, None]})
    assert any("no Overdue column" in note for note in export.counts["notes"])


@pytest.mark.parametrize(
    ("synced", "refreshed", "group", "detail"),
    [
        # the sync ran later the same day: after the refresh, and the row is missing from it
        (
            datetime.datetime(2026, 10, 14, 20, 30),
            datetime.datetime(2026, 10, 14, 6, 0),
            4,
            "DETAIL_SYNC_AFTER",
        ),
        (
            datetime.datetime(2026, 10, 14, 5, 0),
            datetime.datetime(2026, 10, 14, 6, 0),
            2,
            "DETAIL_SYNC_BEFORE",
        ),
        (datetime.datetime(2026, 10, 14, 20, 30), None, 2, "DETAIL_SYNC_UNKNOWN"),  # no refresh stamp
        (None, datetime.datetime(2026, 10, 14, 6, 0), 2, "DETAIL_SYNC_UNKNOWN"),  # no sync recorded
    ],
)
def test_a_row_not_in_the_datamart_read_compares_the_sync_and_the_refresh_by_their_moments(
    checker, world, synced, refreshed, group, detail
):
    from neurodb.datamart import powerbi_words

    if synced:
        pf.sync(synced)
    rows = [_with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30))]
    upload = pf.compared(checker, _one("report_list", rows), refreshed=refreshed)
    _export, lines = _lines(upload, "report_list")
    missing = next(x for x in lines if x.kind == "only_powerbi")
    assert (missing.reason, missing.group, missing.detail) == (
        "not_in_read",
        group,
        getattr(powerbi_words, detail),
    )
    assert (powerbi_words.SYNC_BEFORE in upload.summary["notes"]) == (detail == "DETAIL_SYNC_BEFORE")


def test_a_target_of_0_against_no_target_is_the_same_and_never_a_rule(checker, world):
    from neurodb.datamart import models as dm
    from neurodb.datamart import powerbi_facts, powerbi_kinds, powerbi_match

    dm.PDIndicator.objects.filter(title="Fake indicator 1").update(target_numerator=None)
    performance = [_pp(indicator_title="Fake indicator 1", target=0, reached=35, achieved=0)]
    indicators = [
        pf.row("pd_indicators", pd=PD, indicator_title="Fake indicator 1", target=0, reached=35,
               start=D(2026, 1, 1), **PD_INDICATOR_BASE),
    ]  # fmt: skip
    upload = pf.compared(checker, _one("program_performance", performance), _one("pd_indicators", indicators))
    for kind in ("program_performance", "pd_indicators"):
        export, lines = _lines(upload, kind)
        assert not [x for x in lines if x.match[1] == "Fake indicator 1"], [
            (x.reason, x.found) for x in lines
        ]
        assert any("Power BI shows no value as 0" in note for note in export.counts["notes"]), kind
    # NeuroDB's own target, eTools', never explains a difference as "a different rule"
    job = powerbi_kinds.Job(
        kind=powerbi_exports.KINDS["program_performance"], columns=[], data=None, parsed=powerbi_kinds.Filter(),
        context=powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1)),
        result=powerbi_match.Result(limit=10),
    )  # fmt: skip
    candidates = {"etools": None, "prp_sum": 7.0, "prp_one": [7.0]}
    reason = powerbi_kinds.candidate_reason(job, "target", 0, {"target": None}, candidates, "etools")
    assert reason[0] != "rule"


def test_a_choice_is_offered_only_when_the_rule_fits_most_rows():
    """Choices for you asserts Power BI's rule only on a clear majority of the rows: the best rule fitting 1 of
    100 rows offers no choice and claims no rule. Progress reports and the Emergency indicator are no choice
    since the user decided them, whatever an earlier upload's evidence says."""
    from neurodb.datamart import powerbi_results, powerbi_words

    def performance(fits: int, above: int, positive: int, emergency: list[int]) -> PowerBIExport:
        row = {"candidate": "qpr_sum", "fits": fits, "of": 100, "above": above, "positive": positive}
        return PowerBIExport(
            kind="program_performance",
            state="compared",
            fits=[{"title": "reached", "rows": [{**row, "chosen": True}]}],
            counts={"extra": {"emergency_fit": emergency}},
        )

    sector = PowerBIExport(kind="by_sector", state="compared", counts={"extra": {"progress_fit": [0, 26]}})
    weak = [performance(1, 1, 100, [0, 40]), sector]
    assert powerbi_results.choices(weak) == []
    sentence = powerbi_results.reached_sentence(weak)
    assert (
        sentence.startswith("The rule that fits Power BI's Reached best")
        and "not missing data" not in sentence
    )
    assert sentence == powerbi_words.REACHED_WEAK.format(
        words=powerbi_words.CANDIDATE_WORDS["qpr_sum"], fits=1, of=100
    )
    sector.counts = {"extra": {"progress_fit": [20, 26]}}
    clear = [performance(90, 80, 90, [30, 40]), sector]
    assert [c["code"] for c in powerbi_results.choices(clear)] == ["reached"]


def test_a_rule_sends_the_user_to_choices_for_you_only_when_one_is_offered(checker, world):
    """Data Quality's readings are a rule no choice is offered for: neither their lines nor the card's verdict
    send the user to Choices for you, where nothing would be found. Program Performance's Reached, offered as a
    choice, does."""
    from django.urls import reverse

    from neurodb.datamart import powerbi_results

    pd, _ended = world
    _indicator(pd, 103, "Fake never reported")  # no report in 2026
    _indicator(pd, 104, "Fake draft value")  # a value only in a report not yet submitted
    pf.report_row(pd, "QPR3", "", D(2026, 7, 1), D(2026, 9, 30), title="Fake draft value", source=104, value=9,
                  due=D(2026, 10, 14))  # fmt: skip
    filters = (*YEAR, "is_blueprint_canonical is True", "section_name is Section Alpha",
               "actual_value is less than or equal to 0 or is blank")  # fmt: skip
    rows = [
        pf.row("dq_list", section="Section Alpha", pd=PD, cp_output="Output One", pd_output="Fake output 1",
               indicator_title="Fake indicator 1", reached=0),
    ]  # fmt: skip
    performance = [
        _pp(indicator_title="Fake indicator 1", target=100, reached=15, achieved=0.15),
        _pp(indicator_title="Fake percentage 2", target=40, reached=25, achieved=0.625),
    ]
    upload = pf.compared(
        checker,
        _one("dq_list", rows, filters=filters),
        _one("dq_card", [pf.row("dq_card", indicators=1)], filters=(*YEAR, "section_name is Section Alpha")),
        _one("program_performance", performance, filters=PP_FILTERS),
    )
    offered = powerbi_results.offered(upload)
    assert "reached" in offered
    card, (line,) = _lines(upload, "dq_card")
    assert (line.reason, card.verdict) == ("rule", "counted")
    assert "Choices for you" not in powerbi_results.what_to_do(line, "1 Jan 2026", offered)
    assert "Choices for you" not in powerbi_results.verdict_words(card, offered=offered)[1]
    _dq, lines = _lines(upload, "dq_list")
    assert {x.reason for x in lines} == {"rule"}
    assert not [x for x in lines if "Choices for you" in powerbi_results.what_to_do(x, "1 Jan 2026", offered)]
    for export in (card, _dq):
        page = checker.get(reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk]))
        assert "see Choices for you" not in page.content.decode()
    page = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    assert (
        "See Choices for you if NeuroDB should count Power BI's way" not in page.split("Choices for you</")[0]
    )
    performance_export, lines = _lines(upload, "program_performance")
    reached_line = next(x for x in lines if x.rule == "reached")
    assert "(see Choices for you)" in powerbi_results.what_to_do(reached_line, "1 Jan 2026", offered)
    assert "See Choices for you" in powerbi_results.verdict_words(performance_export, offered=offered)[1]
    # the verdicts the upload keeps (once its lines are removed) are written once its choices are known
    kept = {page["name"]: page for page in upload.summary["pages"]}
    assert "See Choices for you" in kept["Program Performance"]["action"], kept["Program Performance"]


def test_a_pd_neurodb_does_not_hold_is_checked_in_etools_and_a_report_in_prp(checker, world):
    """A funds row or a PD indicator of a PD NeuroDB does not hold, its sync after Power BI's refresh: check the
    PD in eTools. A report missing from the Datamart read is checked in PRP."""
    from neurodb.datamart import powerbi_results, powerbi_words

    pf.sync(datetime.datetime(2026, 10, 14, 20, 30))
    funds = [pf.row("fund_monitoring", pd="FAKE/PCA9/PD9", unicef_planned=1.0, cash=2.0, partner_contribution=3.0,
                    partner=PARTNER, currency="USD")]  # fmt: skip
    indicators = [pf.row("pd_indicators", pd="FAKE/PCA9/PD9", indicator_title="Fake indicator 9", target=10,
                         reached=0, start=D(2026, 1, 1), **PD_INDICATOR_BASE)]  # fmt: skip
    reports = [_with(REPORTS[0], report="QPR9", period_start=D(2026, 4, 1), period_end=D(2026, 4, 30))]
    upload = pf.compared(
        checker,
        _one("fund_monitoring", funds, drop=("financial_contribution",)),
        _one("pd_indicators", indicators),
        _one("report_list", reports),
        refreshed=datetime.datetime(2026, 10, 14, 6, 0),
    )
    for kind in ("fund_monitoring", "pd_indicators"):
        _export, lines = _lines(upload, kind)
        missing = next(x for x in lines if x.kind == "only_powerbi")
        assert (missing.reason, missing.group) == ("not_in_read", 4), kind
        todo = powerbi_results.what_to_do(missing, "1 Jan 2026")
        assert todo.endswith(powerbi_words.NOT_IN_READ_PD) and "PRP" not in todo, (kind, todo)
    _export, lines = _lines(upload, "report_list")
    missing = next(x for x in lines if x.kind == "only_powerbi")
    todo = powerbi_results.what_to_do(missing, "1 Jan 2026")
    assert todo.endswith(powerbi_words.REASON_ACTIONS["not_in_read"]) and "PRP" in todo


def test_neurodbs_own_rows_before_its_history_are_set_aside_like_power_bis(checker, world):
    """A report NeuroDB still holds of a period that starts before its history (kept from an earlier sync) is
    set aside like Power BI's rows of those periods, on the report list, By location and By disaggregation:
    never "only in NeuroDB, not explained yet". The note says how many of NeuroDB's own rows were set aside."""
    from neurodb.datamart import powerbi_words

    pd, _ended = world
    early = pf.report_row(
        pd, "HR1", "Accepted", D(2025, 12, 1), D(2026, 2, 28), value=7, submitted=D(2026, 3, 5)
    )
    early.disaggregation = {"()": 7, "(1,)": 3, "(2,)": 4}
    early.save(update_fields=["disaggregation"])
    places = [
        _location(report="QPR1", p_code="LB9_9_1", reached=10, period_end=D(2026, 3, 31)),
        _location(report="QPR1", p_code="LB9_9_2", reached=5, period_end=D(2026, 3, 31)),
        _location(indicator_title="Fake percentage 2", report="QPR1", p_code="LB9_9_1", reached=0.25,
                  period_end=D(2026, 3, 31), target=0.4),
        _location(report="QPR2", p_code="LB9_9_1", reached=20, report_status="Submitted", period_end=D(2026, 6, 30)),
    ]  # fmt: skip
    values = [_value(disaggregation="Female", reached=4), _value(disaggregation="Male", reached=6)]
    upload = pf.compared(
        checker,
        _one("report_list", REPORTS),
        _one("locations", places, filters=LOCATION_FILTERS, drop=LOCATION_DROP),
        _one("disaggregations", values, filters=(*ACTIVE, "is_period_latest is True"), drop=DISAGGREGATION_DROP),
    )  # fmt: skip
    # one report, one location row, two values (Female and Male)
    for kind, n in (("report_list", 1), ("locations", 1), ("disaggregations", 2)):
        export, lines = _lines(upload, kind)
        assert not [x for x in lines if "HR1" in [str(v) for v in x.match]], (kind, [x.match for x in lines])
        note = powerbi_words.either(
            n, powerbi_words.NOTE_SET_ASIDE_NEURODB_ONE, powerbi_words.NOTE_SET_ASIDE_NEURODB
        )
        assert note.format(n=n, history="1 Jan 2026") in export.counts["notes"], (
            kind,
            export.counts["notes"],
        )


# ------------------------------------------------------------------------------- production-shaped data
def _contribution(pd, number, status, end, value):
    """One PRP indicator report record of ``pd`` carrying the financial contribution to date (as PRP sends it:
    a text)."""
    from neurodb.datamart import models as dm

    return dm.DatamartDocument.objects.create(
        dataset="prp_indicator_reports", record_key=f"fake-fc-{pd.pk}-{number}", intervention=pd,
        data={"report_type": "QPR", "report_number": number, "pr_status": status, "time_period_end": end,
              "due_date": "2026-12-31", "submission_date": "2026-07-10", "financial_contribution_to_date": value,
              "financial_contribution_currency": "USD"},
    )  # fmt: skip


def _fund(**values):
    base = {"pd": PD, "unicef_planned": 9000, "cash": 10000, "partner_contribution": 1000, "partner": PARTNER,
            "partner_type": "Civil Society Organization", "currency": "USD", "partner_share": 0.1}  # fmt: skip
    return pf.row("fund_monitoring", **{**base, **values})


def test_program_performance_reads_every_pd_status_only_for_the_pds_of_the_titles_left_over(
    checker, world, monkeypatch
):
    """Why a Power BI row has no indicator on the page is read from the PDs that hold its title only, never from
    every PD indicator NeuroDB holds of every status (273,253 rows in production: past the job's memory, a
    DatabaseError), and from their indicators alone (``monitoring.collect``, never their reports). The reasons
    stay the same."""
    from neurodb.datamart import monitoring
    from neurodb.partnerships.models import PCA

    pd, ended = world
    _indicator(ended, 201, "Fake ended indicator")
    for n in range(3):  # PDs of other titles, of every status: never read
        other = PCA.objects.create(
            etl_id=f"fake-old-{n}", partner=pd.partner, partner_name=pf.WORLD_PARTNER,
            number=f"FAKE/PCA2025{n:03}/PD2025{n:03}", status=("ended", "closed", "active")[n],
            start=D(2025, 1, 1), end=D(2026, 12, 31),
        )  # fmt: skip
        _indicator(other, 300 + n, f"Fake other indicator {n}")
    calls = []
    real = monitoring.collect

    def spy(filters, steps=None):
        calls.append((filters.statuses_read, sorted(filters.pd_ids)))
        return real(filters, steps)

    monkeypatch.setattr(monitoring, "collect", spy)
    rows = [
        _pp(indicator_title="Fake indicator 1", target=100, reached=10, achieved=0.1),
        _pp(indicator_title="Fake ended indicator", target=100, reached=3, achieved=0.03),
        _pp(indicator_title="Fake indicator never held", target=10, reached=1, achieved=0.1),
    ]
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    export, lines = _lines(upload, "program_performance")
    assert export.state == "compared", export.error
    every_status = [ids for statuses, ids in calls if statuses == []]
    assert every_status == [[ended.pk]], calls  # never every PD
    found = {line.match[1]: line for line in lines if line.kind == "only_powerbi"}
    assert found["Fake ended indicator"].reason == "pd_not_running"
    assert found["Fake indicator never held"].reason == "not_in_read"
    # nothing left over: nothing of every status is read
    calls.clear()
    pf.compared(checker, _one("program_performance", rows[:1], filters=PP_FILTERS))
    assert [ids for statuses, ids in calls if statuses == []] == []


def test_program_performance_reads_the_pds_of_a_shared_title_a_few_at_a_time(checker, world, monkeypatch):
    """A left-over title most PDs share (a renewed PD keeps its logframe's titles; sector indicators are
    standard) is read a few PDs at a time, and only its own indicators are kept: never most PD indicators of
    every status at once again (past the job's memory). The reasons stay the same."""
    from neurodb.datamart import monitoring, powerbi_kinds
    from neurodb.datamart import powerbi_match as pm
    from neurodb.partnerships.models import PCA

    pd, _ended = world
    monkeypatch.setattr(powerbi_kinds, "TITLE_PDS", 2)
    holding = []
    for n in range(5):  # PDs of other statuses holding the title, beside many indicators of other titles
        other = PCA.objects.create(
            etl_id=f"fake-renewed-{n}", partner=pd.partner, partner_name=pf.WORLD_PARTNER,
            number=f"FAKE/PCA2024{n:03}/PD2024{n:03}", status=("ended", "closed")[n % 2],
            start=D(2024, 1, 1), end=D(2025, 12, 31),
        )  # fmt: skip
        _indicator(other, 400 + 10 * n, "Fake shared indicator")
        for k in range(1, 6):
            _indicator(other, 400 + 10 * n + k, f"Fake own indicator {n}-{k}")
        holding.append(other.pk)
    calls, kept = [], []
    real, real_titles = monitoring.collect, powerbi_kinds._titles

    def spy(filters, steps=None):
        found = real(filters, steps)
        if filters.statuses_read == []:
            calls.append((sorted(filters.pd_ids), len(found)))
        return found

    def titles(*args):
        kept.append(real_titles(*args))
        return kept[-1]

    monkeypatch.setattr(monitoring, "collect", spy)
    monkeypatch.setattr(powerbi_kinds, "_titles", titles)
    rows = [_pp(indicator_title="Fake shared indicator", target=100, reached=3, achieved=0.03)]
    upload = pf.compared(checker, _one("program_performance", rows, filters=PP_FILTERS))
    export, lines = _lines(upload, "program_performance")
    assert export.state == "compared", export.error
    assert calls == [(holding[:2], 12), (holding[2:4], 12), (holding[4:], 6)]  # 2 PDs at most at once
    (found,) = kept
    assert list(found) == [pm.text("Fake shared indicator")]  # the PDs' other titles: never kept
    assert sorted(n["pd_id"] for n in found[pm.text("Fake shared indicator")]) == holding
    (line,) = [x for x in lines if x.kind == "only_powerbi"]
    assert line.reason == "pd_not_running"


def test_a_prp_value_written_as_a_json_number_beyond_any_float_stops_no_export(checker, world):
    """PRP keeps a disaggregation as received: a JSON number of 401 digits (not its text) reads as a whole
    number no float holds, which once stopped six exports at NeuroDB's page (OverflowError). It reads as its
    text does: every export is compared and its location's value is one NeuroDB cannot read."""
    from django.db import connection

    from neurodb.datamart import models as dm

    pd, _ended = world
    record = dm.ReportedIndicator.objects.get(intervention=pd, report_number="QPR2")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE datamart_reportedindicator SET disaggregation = jsonb_build_object('()', "
            "('1' || repeat('0', 400))::jsonb) WHERE id = %s",
            [record.pk],
        )
    assert dm.ReportedIndicator.objects.get(pk=record.pk).disaggregation == {"()": 10**400}
    upload = pf.compared(checker, *pf.every_export())
    states = dict(PowerBIExport.objects.filter(upload=upload).values_list("kind", "state"))
    assert upload.status == PowerBIUpload.Status.DONE, upload.error
    assert set(states.values()) == {"compared"}, states
    assert reached.number(10**400) == float("inf") and reached.number(-(10**400)) == float("-inf")
    rows = [_location(report="QPR2", p_code="LB9_9_1", reached=20, report_status="Submitted",
                      period_end=D(2026, 6, 30))]  # fmt: skip
    upload = pf.compared(checker, _one("locations", rows, filters=LOCATION_FILTERS, drop=LOCATION_DROP))
    export, lines = _lines(upload, "locations")
    assert export.state == "compared", export.error
    (line,) = [x for x in lines if x.match[1] == "QPR2"]
    assert (line.reason, line.group, line.found["reached"][:2]) == ("unreadable", 4, [20.0, None]), line.found


@pytest.mark.parametrize("powerbi, second", [(100, "Submitted"), (350, "Accepted")])
def test_a_financial_contribution_counted_another_way_is_written(checker, world, powerbi, second):
    """Two progress reports whose contributions differ: Power BI's fits its latest Accepted report (100) or their
    sum (350), NeuroDB takes the latest Accepted or Submitted (250). The line names that rule and is written
    (its code once overran its column: the export failed with DataError)."""
    from neurodb.datamart import powerbi_results

    pd, _ended = world
    _contribution(pd, "1", "Accepted", "2026-03-31", "100")
    _contribution(pd, "2", second, "2026-06-30", "250")
    upload = pf.compared(checker, _one("fund_monitoring", [_fund(financial_contribution=powerbi)]))
    export, lines = _lines(upload, "fund_monitoring")
    assert export.state == "compared", export.error
    (line,) = lines
    assert line.reason == "rule" and line.found["financial_contribution"][:2] == [float(powerbi), 250.0]
    words = powerbi_results.rule_words(line.rule, "1 Jan 2026", export)
    assert words.startswith("Financial contribution: Power BI "), words


def test_every_code_a_line_keeps_fits_its_column():
    from neurodb.datamart import powerbi_words

    width = {name: PowerBIDifference._meta.get_field(name).max_length for name in ("kind", "reason", "rule")}
    assert all(len(code) <= width["kind"] for code in powerbi_words.LINE_KINDS)
    assert all(len(code) <= width["reason"] for code in powerbi_words.REASON_WORDS)
    assert all(len(code) <= width["rule"] for code in powerbi_words.RULE_WORDS)
    assert (
        set(powerbi_words.REASON_WORDS) == set(powerbi_words.REASON_GROUPS) == set(powerbi_words.REASON_ORDER)
    )


@pytest.mark.parametrize(
    "change",
    [{"total_budget": "1e400"}, {"cso_contribution": "1e999"}, {"total_unicef_budget": "-1e400"}],
)
def test_a_neurodb_amount_beyond_any_number_is_a_value_neurodb_cannot_read(checker, world, change):
    """eTools keeps budgets as text: one too large for a number (production holds huge ones) makes its row a
    difference, "A value NeuroDB cannot read", its value left empty, never the end of the export (the line's
    Infinity once failed it with DataError)."""
    from neurodb.partnerships.models import PCA

    pd, _ended = world
    PCA.objects.filter(pk=pd.pk).update(**change)
    upload = pf.compared(checker, _one("fund_monitoring", [_fund()], drop=("financial_contribution",)))
    export, lines = _lines(upload, "fund_monitoring")
    assert export.state == "compared", export.error
    (line,) = lines
    assert (line.kind, line.reason, line.group) == ("different", "unreadable", 4)
    assert all(None in values[:2] for values in line.found.values()), line.found
    assert "unreadable" in export.counts["reasons"]


def test_a_prp_contribution_beyond_any_number_is_a_value_neurodb_cannot_read(checker, world):
    pd, _ended = world
    _contribution(pd, "1", "Accepted", "2026-03-31", "9" * 400)
    upload = pf.compared(checker, _one("fund_monitoring", [_fund(financial_contribution=100)]))
    export, lines = _lines(upload, "fund_monitoring")
    assert export.state == "compared", export.error
    assert [(x.reason, x.found["financial_contribution"][1]) for x in lines] == [("unreadable", None)]


def test_one_row_whose_comparison_raises_is_a_line_of_its_own(checker, world, monkeypatch):
    """A value NeuroDB cannot compute with (an arithmetic or value error while one row is compared) makes that
    row a difference, "A value NeuroDB cannot read", and the export goes on with the next row."""
    import decimal

    from neurodb.datamart import powerbi_kinds

    real = powerbi_kinds.pm.same_money

    def same_money(powerbi, neurodb):
        if powerbi == 7777:
            raise decimal.InvalidOperation
        return real(powerbi, neurodb)

    monkeypatch.setattr(powerbi_kinds.pm, "same_money", same_money)
    rows = [_fund(cash=7777), _fund(pd=ENDED, cash=1)]
    upload = pf.compared(checker, _one("fund_monitoring", rows, drop=("financial_contribution",)))
    export, lines = _lines(upload, "fund_monitoring")
    assert export.state == "compared", export.error
    found = {(x.match[0], x.kind): x for x in lines}
    first = found[(PD, "different")]
    assert (first.reason, first.group, first.found) == ("unreadable", 4, {})
    assert found[(ENDED, "only_powerbi")].reason == "pd_not_running"


def test_a_program_performance_link_fits_its_column_whatever_the_sections(db):
    """Power BI's filter can name many sections: the figure trail's address leaves them out when they would
    take it past its column (the indicator's Reached is the same for every section), never no address."""
    from neurodb.datamart import powerbi_exports as px
    from neurodb.datamart import powerbi_facts, powerbi_filters, powerbi_kinds
    from neurodb.datamart import powerbi_match as pm

    context = powerbi_facts.Context(day=D(2026, 10, 15), history=D(2026, 1, 1))
    width = PowerBIDifference._meta.get_field("link").max_length
    sections = [f"Fake section {n} with a long name" for n in range(8)]
    for chosen, kept in ((sections[:1], True), (sections, False)):
        parsed = powerbi_filters.Filter(pd_statuses=["active"], years=[2026], sections=chosen)
        job = powerbi_kinds.Job(px.KINDS["program_performance"], [], None, parsed, context, pm.Result(10))
        link = powerbi_kinds.trail_link(job, {"pd_id": 123456, "key": "r1234567"})
        assert 0 < len(link) <= width and "figure=pp.reached.123456.r1234567" in link, link
        assert ("section=" in link) is kept


# ------------------------------------------------- stage 3: PRP's own reports still owed, once the pages show them
def _owed_shown(number: str, end: D, due: D, word: str = "Overdue") -> None:
    """PRP's own list holds ``number`` of the world's PD (linked to it), and the sync's verdict shows PRP's
    reports still owed on the pages (datamart.owed: the gate open)."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun
    from neurodb.datamart.models import DatamartDocument
    from neurodb.partnerships.models import PCA

    pd = PCA.objects.get(number=PD)
    DatamartDocument.objects.create(
        dataset="prp_progress_reports", record_key=f"h-{number}", intervention=pd, partner=pd.partner, date=end,
        data={"report_type": number.rstrip("0123456789"), "report_number": number, "start_date":
              end.replace(day=1).isoformat(), "end_date": end.isoformat(), "due_date": due.isoformat(),
              "status": word},
    )  # fmt: skip
    started = timezone.make_aware(datetime.datetime(2026, 10, 14, 20, 30))
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="prp_progress_reports", status=SyncRun.Status.SUCCEEDED,
        started_at=started, finished_at=started + datetime.timedelta(minutes=5),
        details={"progress_reports_pd_link": {"passes": True, "shown": True, "shown_since": "2026-10-13",
                                              "shown_why": "", "days": 3, "proven": True}},
    )  # fmt: skip


def test_gate_open_an_owed_report_neurodb_shows_compares_as_held(checker, world):
    """HR1 (Overdue, ended 31 Aug) is in PRP's own list and the pages show it: Power BI's HR1 pairs with it, and
    the export says how many of NeuroDB's reports come from PRP's own list."""
    _owed_shown("HR1", D(2026, 8, 31), D(2026, 9, 15))
    hr1 = _with(REPORTS[0], report="HR1", report_status="Overdue", period_start=D(2026, 8, 1),
                period_end=D(2026, 8, 31), due_date=D(2026, 9, 15))  # fmt: skip
    upload = pf.compared(checker, _one("report_list", [*REPORTS, hr1]))
    export, lines = _lines(upload, "report_list")
    assert not [line for line in lines if line.match[1] == "HR1"]
    assert export.counts["same"] == 4
    assert "1 of NeuroDB's reports comes from PRP's own list of progress reports" in " ".join(
        export.counts["notes"]
    )


def test_gate_open_an_owed_report_prp_lacks_is_not_in_read(checker, world):
    """HR2 is in neither feed: with the gate open, NeuroDB could show it, so it is not in the Datamart read."""
    _owed_shown("HR1", D(2026, 8, 31), D(2026, 9, 15))
    hr2 = _with(REPORTS[0], report="HR2", report_status="Overdue", period_start=D(2026, 9, 1),
                period_end=D(2026, 9, 30), due_date=D(2026, 10, 10))  # fmt: skip
    upload = pf.compared(checker, _one("report_list", [*REPORTS, hr2]))
    _export, lines = _lines(upload, "report_list")
    from neurodb.datamart import powerbi_words as words

    (line,) = [line for line in lines if line.match[1] == "HR2"]
    assert (line.kind, line.reason) == ("only_powerbi", "not_in_read")
    assert line.detail.startswith(words.DETAIL_NOT_IN_PRP)
