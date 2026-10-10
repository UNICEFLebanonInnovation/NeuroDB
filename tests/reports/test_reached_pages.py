"""eTools Reached on every page: the same filter gives the same numbers on Partner monitoring (grid,
indicator detail, map), the programme document and partner pages and Ask NeuroDB; plus the views that
read it (All years, the progress report pop-up), Ask's refusal to add partner report values, and the
eTools reporting checks page."""

# ruff: noqa: F401, F811 (the partner monitoring fixture "data" comes from test_pd_monitoring)
from __future__ import annotations

import datetime

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import checks, monitoring, services
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA
from tests.reports.test_pd_monitoring import TODAY, _FrozenDatetime, data, report_rows

pytestmark = pytest.mark.django_db
HX = {"HX-Request": "true"}


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(monitoring, "datetime", _FrozenDatetime)


@pytest.fixture
def submitted(data):
    """A third girls report, submitted and not yet accepted: Akkar 30 more."""
    report_rows(
        data["pd"],
        data["partner"],
        data["girls"],
        "PR-2s",
        "QPR2s",
        "QPR",
        datetime.date(2026, 6, 15),
        {"Akkar": 30},
        580,
        status="Submitted",
        before={"Akkar": 350},
    )
    return data


def test_same_filter_same_numbers_on_every_page(client_viewer, submitted, frozen_today):
    data = submitted
    pd, partner = data["pd"], data["partner"]
    grid = {r.title: r for r in monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)}
    girls = grid[data["girls"]]
    assert (girls.reached, girls.awaiting, girls.awaiting_reports, girls.reached_in_year) == (
        580.0,
        30.0,
        1,
        580.0,
    )
    # indicator detail: the corner, the rows and the columns
    detail = monitoring.indicator_detail(pd, "100", "QPR", 2026, today=TODAY)
    assert detail["indicator"].reached == 580.0
    assert sum(loc["reached"] for loc in detail["by_location"]) == 580.0
    assert sum(p["total"] for p in detail["periods"] if p["counted"]) == 580.0
    # the programme document page: its indicators and its Reached rows
    pd_page = services.programme_datamart(pd)
    assert {r.title: r.reached for r in pd_page["monitoring"]["rows"]} == {
        t: r.reached for t, r in grid.items()
    }
    progress = {p["indicator"]: p for p in pd_page["latest_progress"]}
    assert (progress[data["girls"]]["reached"], progress[data["girls"]]["awaiting"]) == (580.0, 30.0)
    assert progress[data["boys"]]["reached"] == 20.0 and progress[data["boys"]]["report"] == "QPR1"
    # the partner page
    partner_page = services.partner_datamart(partner)
    assert {r.title: r.reached for r in partner_page["monitoring"]["rows"]} == {
        t: r.reached for t, r in grid.items()
    }
    # the map: the indicator's Reached, and its locations add up to it
    places = monitoring.map_points(monitoring.Filters(year=2026), today=TODAY)
    mine = [
        r
        for p in places["points"] + places["unlocated"]
        for r in p["rows"]
        if r["indicator"] == data["girls"]
    ]
    assert {r["reached"] for r in mine} == {580.0} and sum(r["reached_here"] for r in mine) == 580.0
    # Ask NeuroDB
    out = tools.run("pd_indicator_progress", {"partner": "amel", "year": 2026})
    ask = next(i for i in out["indicators_detail"] if i["indicator"] == data["girls"])
    assert (ask["reached"], ask["awaiting_acceptance"], ask["reached_in_year"]) == (580.0, 30.0, 580.0)
    # and the pages show it
    for text in (
        client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"}).text,
        client_viewer.get(
            reverse("reports:pd_indicator", args=[pd.id, "100"]), {"year": "2026"}, headers=HX
        ).text,
        client_viewer.get(reverse("reports:programme_detail", args=[pd.id])).text,
        client_viewer.get(reverse("reports:partner_profile", args=[partner.id])).text,
    ):
        assert "580" in text and "Cumulative" not in text


def test_grid_columns_and_the_all_years_view(client_viewer, submitted, frozen_today):
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"})
    for heading in ("Reached", "% Reached", "Reached in 2026", "Awaiting acceptance"):
        assert f">{heading}<" in page.text
    assert "never adding a value the reports repeat" in page.text
    report_rows(
        submitted["pd"],
        submitted["partner"],
        submitted["girls"],
        "PR-0",
        "QPR0",
        "QPR",
        datetime.date(2025, 12, 31),
        {"Bekaa": 10},
        10,
    )
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "all"})
    assert page.status_code == 200 and page.context["filters"].all_years
    assert page.context["year_columns"] == [2025, 2026] and "Reached in 2026" not in page.text
    girls = next(r for r in page.context["rows"] if r.title == submitted["girls"])
    assert girls.years == {2025: 10.0, 2026: 580.0} and sum(girls.years.values()) == girls.reached == 590.0
    assert 'value="all" selected' in page.text and "year=all" in page.text
    detail = client_viewer.get(girls.url, {"year": "all"}, headers=HX)
    assert detail.status_code == 200 and detail.context["year"] is None and "590" in detail.text


def test_an_indicator_without_quarterly_reports_is_read_from_its_monthly_reports(
    client_viewer, data, frozen_today
):
    pd, partner = data["pd"], data["partner"]
    kits = "# of emergency kits distributed"
    dm.PDIndicator.objects.create(
        datamart_id=40,
        source_id=400,
        intervention=pd,
        title=kits,
        section_name="Child Protection",
        target_numerator=100,
        is_high_frequency=True,
        location_name="Akkar",
    )
    report_rows(pd, partner, kits, "HR-1", "HR1", "HR", datetime.date(2026, 4, 30), {"Akkar": 30}, 30)
    report_rows(
        pd,
        partner,
        kits,
        "HR-2",
        "HR2",
        "HR",
        datetime.date(2026, 5, 31),
        {"Akkar": 20},
        50,
        before={"Akkar": 30},
    )
    rows = {r.title: r for r in monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)}
    assert (rows[kits].reached, rows[kits].report_type) == (50.0, "HR")
    # an indicator with quarterly reports never takes its monthly ones (HR4's 100 is not added)
    assert (rows[data["girls"]].reached, rows[data["girls"]].report_type) == (550.0, "QPR")
    assert (
        "From monthly reports" in client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"}).text
    )


def test_two_indicators_with_the_same_title_are_never_merged(data):
    pd, partner = data["pd"], data["partner"]
    dm.PDIndicator.objects.create(
        datamart_id=41,
        source_id=101,
        intervention=pd,
        title=data["girls"],
        section_name="Child Protection",
        target_numerator=500,
        location_name="Tyre",
    )
    report_rows(
        pd,
        partner,
        data["girls"],
        "PR-1",
        "QPR1",
        "QPR",
        datetime.date(2026, 3, 31),
        {"Tyre": 40},
        40,
        etools_indicator_id="101",
    )
    # the title no longer says which indicator a row without an id belongs to: those rows are left out
    rows = [
        r
        for r in monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)
        if r.title == data["girls"]
    ]
    assert sorted((r.key, r.reached) for r in rows) == [("100", None), ("101", 40.0)]
    # with their ids, each indicator reads its own reports: never A + B, never max(A, B)
    dm.ReportedIndicator.objects.filter(indicator=data["girls"], etools_indicator_id="").update(
        etools_indicator_id="100"
    )
    rows = [
        r
        for r in monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)
        if r.title == data["girls"]
    ]
    assert sorted((r.key, r.reached, r.target) for r in rows) == [
        ("100", 550.0, 1000.0),
        ("101", 40.0, 500.0),
    ]
    assert {r.key: r.months for r in rows} == {"100": {3: 300.0, 6: 250.0}, "101": {3: 40.0}}


def test_a_pd_older_than_the_reports_read_shows_prp_cumulative(data, settings):
    old = PCA.objects.create(
        etl_id="15",
        partner=data["partner"],
        partner_name=data["partner"].name,
        number="LEB/PD5",
        title="Older",
        status="closed",
        start=datetime.date(2020, 1, 1),
        end=datetime.date(2025, 12, 31),
    )
    dm.PDIndicator.objects.create(
        datamart_id=42, source_id=600, intervention=old, title="# of boys", target_numerator=1000
    )
    report_rows(
        old,
        data["partner"],
        "# of boys",
        "OLD-1",
        "QPR9",
        "QPR",
        datetime.date(2025, 9, 30),
        {"Akkar": 10},
        900,
    )
    (row,) = monitoring.indicators(monitoring.Filters(pds=["LEB/PD5"], scope="all"), today=TODAY)
    assert (row.reached, row.partial, row.prp_cumulative) == (900.0, True, 900.0)
    # a running PD is read from its start: its history is whole, its Reached the sum of its reports
    PCA.objects.filter(pk=old.pk).update(status="active", end=datetime.date(2027, 12, 31))
    (row,) = monitoring.indicators(monitoring.Filters(pds=["LEB/PD5"], scope="all"), today=TODAY)
    assert (row.reached, row.partial, row.mismatch) == (10.0, False, True)
    settings.ETOOLS_DATAMART_REPORTING_FROM_PD_START = False
    (row,) = monitoring.indicators(monitoring.Filters(pds=["LEB/PD5"], scope="all"), today=TODAY)
    assert (row.reached, row.partial) == (900.0, True)


def test_progress_report_pop_up_shows_parsed_numbers_and_totals(client_viewer, data):
    dm.ReportedIndicator.objects.filter(progress_report="PR-1", location="Bekaa").update(
        disaggregation={},
        achievement_in_period="{'c': 100.0, 'd': 1, 'v': 100}",
        total_cumulative_progress_in_location="{'c': 100.0, 'd': 1, 'v': 100}",
        previous_location_progress="",
    )
    detail = services.report_detail("PR-1", today=TODAY)
    girls = next(i for i in detail["indicators"] if i["indicator"] == data["girls"])
    assert [(loc["name"], loc["value"]) for loc in girls["locations"]] == [("Akkar", 200.0), ("Bekaa", 100.0)]
    assert (girls["total"], girls["method"], girls["prp_cumulative"]) == (300.0, "sum of locations", 300.0)
    assert detail["status"] == "accepted" and detail["counted"]
    page = client_viewer.get(reverse("reports:progress_report"), {"report": "PR-1"}, headers=HX)
    assert page.status_code == 200 and "{&#x27;c&#x27;" not in page.text and "{'c'" not in page.text
    assert "All locations" in page.text and "Counts in Reached" in page.text
    due = client_viewer.get(reverse("reports:progress_report"), {"report": "PR-3"}, headers=HX)
    assert "Does not count in Reached" in due.text
    assert client_viewer.get(reverse("reports:progress_report"), {"report": "nope"}).status_code == 404


def test_progress_reports_page_counts_six_statuses(client_viewer, submitted, monkeypatch):
    monkeypatch.setattr(services, "datetime", _FrozenDatetime)
    dm.ReportedIndicator.objects.filter(progress_report="HR-4").update(report_status="Sent back")
    data = services.partner_reporting({})
    summary = data["summary"]
    assert summary["statuses"] == {
        "accepted": 2,  # PR-1, PR-2
        "submitted": 1,  # PR-2s
        "sent_back": 1,  # HR-4
        "due": 0,
        "overdue": 0,
        "not_yet_due": 1,  # PR-3 (period ends 30 Sep, today 1 Jul)
    }
    assert sum(summary["statuses"].values()) == summary["reports"] == 5
    page = client_viewer.get(reverse("reports:partner_reporting"))
    for label in ("Accepted", "Submitted", "Sent back", "Due", "Overdue", "Not yet due"):
        assert f">{label}<" in page.text


def _girls(data):
    rows = monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)
    return next(r for r in rows if r.title == data["girls"])


def test_a_prp_report_total_about_another_indicator_is_never_used(client_viewer, data):
    """PRP's report totals (prp_indicator_reports) are found by the indicator report id; that the two
    datasets share their ids is not confirmed, so a total about another programme document or
    indicator never stands in for this one's, even where the locations cannot say."""
    pd = data["pd"]
    # QPR1 of the girls: each location row repeats the report total (300), with no value of its own
    dm.ReportedIndicator.objects.filter(progress_report="PR-1", indicator=data["girls"]).update(
        indicator_report="900",
        disaggregation={},
        achievement_in_period="300",
        total_cumulative_progress_in_location="",
        previous_location_progress="",
    )
    doc = dm.DatamartDocument.objects.create(
        dataset="prp_indicator_reports",
        record_key="900",
        source_id=900,
        data={"progress_value": "5000", "performance_indicator": "# of teachers trained"},
    )
    girls = _girls(data)
    assert (girls.reached, girls.result.reports[0].source) == (550.0, "report_total")
    # the record of this indicator in this PD: PRP's own total is used (the locations cannot say)
    doc.intervention = pd
    doc.data = {"progress_value": "310", "performance_indicator": data["girls"].upper()}
    doc.save()
    girls = _girls(data)
    assert (girls.reached, girls.result.reports[0].source) == (560.0, "prp")
    pop_up = services.report_detail("PR-1", today=TODAY)
    assert next(i for i in pop_up["indicators"] if i["indicator"] == data["girls"])["total"] == 310.0
    # the same id about another indicator of the PD, or about another PD: never used
    doc.data = {"progress_value": "310", "performance_indicator": data["boys"]}
    doc.save()
    assert _girls(data).reached == 550.0
    doc.intervention = data["closed"]
    doc.data = {"progress_value": "310", "performance_indicator": data["girls"]}
    doc.save()
    assert _girls(data).reached == 550.0
    # the eTools reporting checks page counts such records
    check = next(c for c in checks.run_all() if c["key"] == "8")
    assert dict(check["figures"])["… about another programme document"] == 1
    assert "another programme document" in check["verdict"] and check["tone"] == "warning"
    # nothing to confirm it (no PD link, no title): never used either
    doc.intervention = None
    doc.data = {"progress_value": "310"}
    doc.save()
    assert _girls(data).reached == 550.0


def test_a_progress_report_without_an_id_is_not_found(client_viewer, data):
    """Rows without a progress report id belong to no one report: ``?report=`` (empty) is not found,
    never a page that pools them (or fails on them)."""
    for number, end, place in (
        ("QPR7", datetime.date(2025, 3, 31), "Akkar"),
        ("QPR8", datetime.date(2025, 6, 30), "Bekaa"),
    ):
        report_rows(data["pd"], data["partner"], data["girls"], "", number, "QPR", end, {place: 5}, 5)
    assert services.report_detail("") is None and services.report_detail("  ") is None
    page = client_viewer.get(reverse("reports:progress_report"), {"report": ""}, headers=HX)
    assert page.status_code == 404


def test_report_lists_show_the_six_statuses_on_every_page(client_viewer, submitted, monkeypatch):
    """The programme document and partner pages list progress reports with the six statuses of the
    Progress reports page (a report whose period still runs is "Not yet due", whatever PRP's word),
    and the partner page counts accepted reports apart from those awaiting acceptance."""
    monkeypatch.setattr(services, "datetime", _FrozenDatetime)
    data = submitted
    dm.ReportedIndicator.objects.filter(progress_report="PR-1").update(submitted_by="someone@example.org")
    pd_page = services.programme_datamart(data["pd"])
    statuses = {r["report_number"]: r["status_label"] for r in pd_page["reports"]}
    assert statuses == {
        "QPR1": "Accepted",
        "QPR2": "Accepted",
        "QPR2s": "Submitted",
        "HR4": "Accepted",
        "QPR3": "Not yet due",
    }
    summary = services.partner_datamart(data["partner"])["reporting"]
    assert (summary["accepted"], summary["submitted"], summary["not_yet_due"]) == (3, 1, 1)
    monkeypatch.undo()
    page = client_viewer.get(reverse("reports:partner_profile", args=[data["partner"].id]))
    assert "3 accepted · 1 awaiting acceptance" in page.text
    # the pop-up never shows an e-mail address
    pop_up = client_viewer.get(reverse("reports:progress_report"), {"report": "PR-1"}, headers=HX)
    assert pop_up.status_code == 200 and "@example.org" not in pop_up.text
    # a report PRP accepted without its submission date is Accepted, never "Overdue" in the table
    dm.ReportedIndicator.objects.filter(progress_report="PR-1").update(submission_date=None)
    table = client_viewer.get(reverse("reports:partner_reporting"), headers=HX)
    assert table.status_code == 200 and '<span class="text-danger">Overdue</span>' not in table.text
    dm.ReportedIndicator.objects.filter(progress_report="PR-1").update(report_status="Due")
    table = client_viewer.get(reverse("reports:partner_reporting"), headers=HX)
    assert '<span class="text-danger">Overdue</span>' in table.text


def test_an_indicator_read_from_monthly_reports_says_so_in_its_detail(client_viewer, data, frozen_today):
    pd, partner = data["pd"], data["partner"]
    dm.PDIndicator.objects.create(
        datamart_id=40,
        source_id=400,
        intervention=pd,
        title="# of emergency kits distributed",
        target_numerator=100,
        is_high_frequency=True,
        location_name="Akkar",
    )
    report_rows(
        pd,
        partner,
        "# of emergency kits distributed",
        "HR-1",
        "HR1",
        "HR",
        datetime.date(2026, 4, 30),
        {"Akkar": 30},
        30,
    )
    page = client_viewer.get(
        reverse("reports:pd_indicator", args=[pd.id, "400"]),
        {"report_type": "QPR", "year": "2026"},
        headers=HX,
    )
    assert page.context["read_type"] == "HR" and "read from the monthly humanitarian reports" in page.text
    assert "HR1" in page.text and page.context["indicator"].reached == 30.0


def test_ask_never_adds_partner_report_values(data):
    with pytest.raises(tools.ToolInputError, match="pd_indicator_progress"):
        tools.run("etools_query", {"dataset": "pd_indicators", "sum": "target_numerator"})
    for dataset in ("partner_reports", "prp_indicator_reports"):
        for extra in ({}, {"group_by": "programme_document"}):
            with pytest.raises(tools.ToolInputError, match="pd_indicator_progress"):
                tools.run("etools_query", {"dataset": dataset, "sum": "total_cumulative_progress", **extra})
    # PRP's {v, d, c} values are read as numbers in filters and sums
    rows = list(dm.ReportedIndicator.objects.filter(progress_report="PR-1").order_by("datamart_id"))
    for row, value in zip(
        rows, ("{'c': 250.0, 'd': 1, 'v': 250}", {"v": 30, "d": 1, "c": 30}, "12"), strict=False
    ):
        row.data = {"achievement_in_reporting_period": value}
        row.save()
    out = tools.run(
        "etools_query", {"dataset": "partner_reports", "filters": {"achievement_in_reporting_period__gt": 20}}
    )
    assert out["matching_records"] == 2
    for n, budget in enumerate(({"v": 100, "d": 1, "c": 100}, "{'v': 45, 'd': 100}", "50")):
        dm.DatamartDocument.objects.create(
            dataset="prp_programme_documents", record_key=str(n), data={"budget": budget}
        )
    out = tools.run("etools_query", {"dataset": "prp_programme_documents", "sum": "budget"})
    assert out["sum"]["budget"] == pytest.approx(150.45)


def test_reporting_checks_page_is_for_administrators(client, admin_user, viewer, submitted):
    client.force_login(admin_user)
    page = client.get(reverse("admin:datamart_reporting_checks"))
    assert page.status_code == 200
    for n in range(1, 10):
        assert f"Check {n} ·" in page.text
    assert "What NeuroDB does" in page.text and "could not finish" not in page.text
    health = client.get(reverse("reports:data_health"))
    assert reverse("admin:datamart_reporting_checks") in health.text
    found = {c["key"]: c for c in checks.run_all()}
    assert found["4"]["figures"][0][1] == 0  # the due QPR3 row carries 0: no value
    dm.ReportedIndicator.objects.filter(progress_report="PR-3").update(achievement_in_period="5")
    found = {c["key"]: c for c in checks.run_all()}
    assert found["4"]["figures"][0][1] == 1 and "1 row of reports that do not count" in found["4"]["verdict"]
    assert found["1"]["tone"] == "ok" and found["6"]["tone"] == "info"
    client.force_login(viewer)
    assert client.get(reverse("admin:datamart_reporting_checks")).status_code in (302, 404)
    assert reverse("admin:datamart_reporting_checks") not in client.get(reverse("reports:data_health")).text


def test_reporting_checks_run_on_an_empty_database(db):
    found = checks.run_all()
    assert [c["key"] for c in found] == [str(n) for n in range(1, 10)]
    assert all(c["tone"] in ("ok", "info", "warning") and c["verdict"] for c in found)


def test_a_widened_window_counts_once_a_sync_has_read_it(data, settings):
    """Until a partner reports sync has read a running PD's early reports, the PD shows PRP's own
    cumulative as a partial history, never the sum of its later reports alone."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    settings.ETOOLS_DATAMART_REPORTING_YEARS = 3
    older = PCA.objects.create(
        etl_id="16",
        partner=data["partner"],
        partner_name=data["partner"].name,
        number="LEB/PD6",
        title="Running since 2020",
        status="active",
        start=datetime.date(2020, 1, 1),
        end=datetime.date(2027, 12, 31),
    )
    dm.PDIndicator.objects.create(
        datamart_id=43, source_id=700, intervention=older, title="# of boys", target_numerator=1000
    )
    report_rows(
        older,
        data["partner"],
        "# of boys",
        "OLD-2",
        "QPR22",
        "QPR",
        datetime.date(2025, 9, 30),
        {"Akkar": 10},
        900,
    )

    def boys():
        (row,) = monitoring.indicators(monitoring.Filters(pds=["LEB/PD6"]), today=TODAY)
        return row.reached, row.partial

    def sync(day, **details):
        SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target="partner_reports",
            status=SyncRun.Status.SUCCEEDED,
            started_at=timezone.make_aware(datetime.datetime(2026, 6, day, 3, 0)),
            details=details,
        )

    assert boys() == (10.0, False)  # no sync recorded: the window the sync reads (the PD's start)
    sync(1, removed=0)  # the last sync read the last three years only (2024 on)
    assert boys() == (900.0, True)
    sync(2, read_from="2020-01-01")  # the next one read from the PD's start
    assert boys() == (10.0, False)


def test_the_matrix_says_when_its_rows_do_not_add_up(client_viewer, data, frozen_today):
    url = reverse("reports:pd_indicator", args=[data["pd"].id, "100"])
    page = client_viewer.get(url, {"year": "2026"}, headers=HX)
    assert page.context["adds_up"] and "so the rows and the columns add up to it" in page.text
    # QPR1 gives only its total (300 repeated on both locations): the rows no longer add up to Reached
    dm.ReportedIndicator.objects.filter(progress_report="PR-1", indicator=data["girls"]).update(
        disaggregation={},
        achievement_in_period="300",
        total_cumulative_progress_in_location="",
        previous_location_progress="",
    )
    page = client_viewer.get(url, {"year": "2026"}, headers=HX)
    assert not page.context["adds_up"] and page.context["indicator"].reached == 550.0
    assert (
        "the rows do not add up to it" in page.text and "so the rows and the columns add up" not in page.text
    )
    assert sum(loc["reached"] for loc in page.context["by_location"]) == 250.0


def test_the_pop_up_reads_each_indicator_with_its_own_type(data):
    """A report row without an eTools id takes the type of the PD indicator with its title, never that
    of another indicator without an id (a percentage would turn 10 + 20 people into 3,000 %)."""
    pd, partner = data["pd"], data["partner"]
    for n, (title, display_type) in enumerate(
        (("% of schools assessed", "percentage"), ("# of teachers", "number"))
    ):
        dm.PDIndicator.objects.create(
            datamart_id=50 + n, intervention=pd, title=title, display_type=display_type, unit=display_type
        )
    report_rows(
        pd,
        partner,
        "# of teachers",
        "PR-9",
        "QPR9",
        "QPR",
        datetime.date(2026, 3, 31),
        {"Akkar": 10, "Bekaa": 20},
        30,
    )
    detail = services.report_detail("PR-9", today=TODAY)
    (teachers,) = detail["indicators"]
    assert (teachers["total"], teachers["method"], teachers["percentage"]) == (
        30.0,
        "sum of locations",
        False,
    )


def test_reporting_check_4_follows_the_six_statuses(admin_user, data):
    """Check 4 counts the value of every report that does not count: PRP's own Due with a submission
    date among them; a report without a word but with a submission date counts (Submitted)."""
    found = {c["key"]: c for c in checks.run_all()}
    assert found["4"]["figures"][0][1] == 0
    dm.ReportedIndicator.objects.filter(progress_report="PR-3").update(
        achievement_in_period="5", submission_date=datetime.date(2026, 10, 1)
    )
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(report_status="")
    found = {c["key"]: c for c in checks.run_all()}
    assert found["4"]["figures"][0][1] == 1
    table = {(r[0], r[1]) for r in found["4"]["table"]["rows"]}
    assert ("Due", "no") in table and ("(none)", "yes") in table
