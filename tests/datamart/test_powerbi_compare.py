"""Compare with Power BI's comparison (datamart.powerbi_kinds, powerbi_match, powerbi_facts, powerbi_page): every
kind of export against the NeuroDB page that shows the same thing, for Power BI's own filter; two values per row
and a reason, with what to do, for every difference. Small generated fake data only."""

from __future__ import annotations

import datetime

import pytest

from neurodb.datamart import reached
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
    assert ("only_powerbi", "owed", 1) in found
    assert ("only_powerbi", "not_in_read", 2) in found  # the sync ran before the refresh: wait for the next
    assert ("only_powerbi", "pd_not_running", 3) in found and export.counts["check_pds"] == [ENDED]
    assert [line.group for line in lines] == sorted(
        (line.group for line in lines), reverse=True
    )  # look first
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
        _follow(status="Due", due_date=D(2027, 1, 15)),  # Power BI's Due covers reports not yet due
        _follow(status="Overdue", due_date=D(2025, 12, 15)),  # before NeuroDB's history
        _follow(pd=ENDED, status="Submitted", due_date=D(2026, 4, 17)),
        _follow(status="Sent back", due_date=D(2026, 8, 15)),
    ]
    upload = pf.compared(checker, _one("follow_up", rows, filters=FOLLOW_FILTERS))
    export, lines = _lines(upload, "follow_up")
    found = _by_reason(lines)
    assert export.counts["same"] == 2
    assert found[("only_powerbi", "status", 1)].detail == "Power BI's Due also covers reports not yet due."
    assert ("only_powerbi", "before_history", 1) in found
    assert ("only_powerbi", "pd_not_running", 3) in found
    assert ("only_powerbi", "not_in_read", 2) in found
    assert export.counts["extra"]["due_not_yet"] == 1
    assert {c["code"] for c in upload.choices} == {"due"}
    due = next(c for c in upload.choices if c["code"] == "due")
    assert "1 of Power BI's 'Due' rows are not yet due" in due["evidence"] and due["question"]


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
    assert "Same unit as Power BI: progress reports" in notes


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
    assert (line.reason, line.group) == ("owed", 1)
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
    assert {c["code"] for c in upload.choices} == {"progress_reports"}


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
    assert "NeuroDB's records give 35." in line.detail and "Also fits:" in line.detail
    assert line.link.startswith("/partner-monitoring/check/") or "figure=pp.reached." in line.link
    (reached, target) = export.fits
    assert reached["title"] == "reached" and target["title"] == "target"
    best = next(r for r in reached["rows"] if r["chosen"])
    assert (best["candidate"], best["fits"], best["of"], best["above"]) == ("qpr_sum", 2, 2, 2)
    assert export.verdict == "counted"
    notes = " ".join(export.counts["notes"])
    assert (
        "Emergency indicator is Yes for an SPD" in notes
        and "Power BI's Section is the PD's sections" in notes
    )
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
    assert found["Fake percentage 2"].found == {"emergency": ["Yes", "No", None]}
    assert found["Fake ended indicator"].reason == "pd_not_running"
    assert found["Fake indicator never held"].reason == "not_in_read"
    assert export.fits[0]["set_aside"] == 2  # the PD started before NeuroDB's history
    assert export.counts["extra"]["emergency_fit"] == [1, 2]


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
    assert line.detail == "Of the 2 indicators behind this gap: 2 are counted another way."
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
    older = found[("QPR2", "Fake indicator 1", "LB9_9_2")]
    assert (older.reason, older.found["reached"][:2]) == ("older_version", [6.0, 8.0])
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
