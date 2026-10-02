"""Machine learning readiness: what each source holds, the verdict per decision, the job and the
Data health section."""

import datetime

from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
from neurodb.insights import readiness
from neurodb.insights.management.commands.ml_readiness import run


def _metrics(result):
    return {f"{s['key']}.{m['key']}": m["value"] for s in result["sections"] for m in s["metrics"]}


def _verdict(result, key):
    return next(v for v in result["verdicts"] if v["key"] == key)


def test_an_empty_platform_is_measured_and_nothing_is_ready(db):
    result = readiness.measure()
    assert not any(s["error"] for s in result["sections"])
    assert {v["status"] for v in result["verdicts"]} == {"not_yet"}
    assert _metrics(result)["activityinfo.reports"] == 0


def test_activityinfo_reports_are_measured(hierarchy):
    m = _metrics(readiness.measure(datetime.date(2026, 10, 1)))
    assert m["activityinfo.reports"] == 4 and m["activityinfo.months"] == 2
    assert m["activityinfo.governorate_coded"] == 1.0 and m["activityinfo.district_coded"] == 1.0
    assert m["activityinfo.completeness"] == 1.0  # each partner reported its one month
    assert m["indicators.targets"] == 1.0


def test_indicators_found_again_the_year_before(hierarchy, section):
    previous = ReportingYear.objects.create(name="2025", year="2025")
    old = Database.objects.create(
        ai_id=202518, db_id="old", name="CP 2025", label="CP", username="", password="",
        section=section, reporting_year=previous, is_funded_by_unicef=False,
    )  # fmt: skip
    MasterIndicator.objects.create(database=old, name="Children reached (total)", awp_code="9", sequence=1)
    m = _metrics(readiness.measure())
    assert m["indicators.continuity"] == 0.5  # one of the two 2026 masters existed in 2025 (same name)


def test_checks_and_statuses():
    assert readiness._check(0.8, 0.7, "x").ok and not readiness._check(0.5, 0.7, "x").ok
    assert readiness._check(None, 0.7, "x").ok is None
    ok, no, unknown = (readiness.Check(v, "") for v in (True, False, None))
    assert readiness._status([ok, ok]) == "ready"
    assert readiness._status([ok, ok, no]) == "partly"
    assert readiness._status([no, ok, ok]) == "not_yet"  # the first check is required
    assert readiness._status([ok, unknown, no]) == "not_yet"  # less than half pass


def test_the_job_records_the_result_and_a_failing_source_marks_it_partial(db, monkeypatch):
    result = run("test")
    assert result.status == SyncRun.Status.SUCCEEDED and len(result.details["readiness"]["verdicts"]) == 4

    def broken(section, today):
        raise RuntimeError("table missing")

    monkeypatch.setattr(readiness, "SOURCES", [("broken", "Broken", broken), *readiness.SOURCES])
    result = run("test")
    assert result.status == SyncRun.Status.PARTIAL
    assert result.details["readiness"]["sections"][0]["error"] == "RuntimeError: table missing"


def test_data_health_shows_the_verdicts_not_a_stale_sync(client_viewer, db):
    page = client_viewer.get(reverse("reports:data_health")).content.decode()
    assert "Ready for machine learning?" in page and "Not measured yet" in page
    run("test")
    page = client_viewer.get(reverse("reports:data_health")).content.decode()
    assert "Which indicators or activities may be falling behind their targets?" in page
    assert 'panel__title">Machine learning readiness check<' not in page  # no stale/fresh card


def test_months_come_from_month_name_whatever_the_month_column_holds(hierarchy):
    from neurodb.facts.models import ActivityReportNew

    ActivityReportNew.objects.update(month="1")  # production keeps something else in this column
    m = _metrics(readiness.measure(datetime.date(2026, 10, 1)))
    assert m["activityinfo.months"] == 2 and m["activityinfo.dated"] == 1.0


def test_governorates_match_by_name_spelling_or_code_and_the_rest_is_named(hierarchy):
    from neurodb.facts.models import ActivityReportNew
    from neurodb.geo.models import GovernorateLocation

    GovernorateLocation.objects.create(code="BEI", name="Beirut", ai_id=1)
    GovernorateLocation.objects.create(code="LB8", name="Baalbek-Hermel", ai_id=2)
    ActivityReportNew.objects.filter(location_adminlevel_governorate="Akkar").update(
        location_adminlevel_governorate="Baalbek-El Hermel", location_adminlevel_governorate_code="X"
    )
    ActivityReportNew.objects.create(
        dbase=hierarchy["database"], month_name="2026-02-01", indicator_value=1,
        location_adminlevel_governorate="Mont Liban", location_adminlevel_governorate_code="5",
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=hierarchy["database"], month_name="2026-02-01", indicator_value=1,
        location_adminlevel_governorate="Atlantis", location_adminlevel_governorate_code="99",
    )  # fmt: skip
    GovernorateLocation.objects.create(code="MOU", name="Mount Lebanon", ai_id=3)
    result = readiness.measure()
    metric = next(m for s in result["sections"] for m in s["metrics"] if m["key"] == "governorate_matched")
    # Beirut by code, Baalbek-El Hermel by spelling, Mont Liban in French; Atlantis is unknown
    assert metric["value"] == round(5 / 6, 3)
    assert metric["note"] == "not matched: Atlantis (99): 1"


def test_progress_reports_are_counted_per_report_and_future_periods_flagged(db):
    from neurodb.datamart.models import ReportedIndicator

    def row(pk, number, period_end, due=None, sent=None):
        ReportedIndicator.objects.create(
            datamart_id=pk, pd_reference_number="PD1", report_number=number, report_type="QPR",
            period_end=period_end, due_date=due, submission_date=sent,
        )  # fmt: skip

    day = datetime.date
    row(1, "QPR1", day(2025, 3, 31), day(2025, 4, 15), day(2025, 4, 10))
    row(2, "QPR1", day(2025, 3, 31), day(2025, 4, 15), day(2025, 4, 10))  # a second indicator, same report
    row(3, "QPR2", day(2025, 6, 30), day(2025, 7, 15), day(2025, 7, 20))
    row(4, "QPR9", day(2029, 7, 31))
    m = _metrics(readiness.measure(day(2026, 10, 1)))
    assert m["etools.progress_reports"] == 3 and m["etools.reports_dated"] == 2
    assert m["etools.reports_on_time"] == 0.5
    assert m["etools.reporting_span"] == "Mar 2025 – Jun 2025" and m["etools.reports_future"] == 1


def test_action_points_are_closed_by_status(db):
    from neurodb.datamart.models import ActionPoint

    for pk, status in enumerate(("open", "completed", "completed", "cancelled"), start=1):
        ActionPoint.objects.create(datamart_id=pk, status=status)
    result = readiness.measure()
    metric = next(m for s in result["sections"] for m in s["metrics"] if m["key"] == "action_points_closed")
    assert metric["value"] == 0.75 and metric["note"] == "completed: 2, cancelled: 1, open: 1"


def test_the_population_year_is_a_year(db):
    from neurodb.core.models import PopulationFigure

    PopulationFigure.objects.create(year=2026, level="national", area_code="LB", area_name="Lebanon", value=1)
    assert _metrics(readiness.measure())["context.population_latest"] == "2026"
