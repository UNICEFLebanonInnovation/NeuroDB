"""Machine learning readiness: what each source holds, the verdict per decision, the job and the
Data health section. ActivityInfo, an archive of past monthly results, is no longer a source (the owner's rule,
November 2026)."""

import datetime

from django.urls import reverse

from neurodb.core.models import SyncRun
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


def test_activityinfo_is_not_measured(hierarchy):
    """ActivityInfo records and their master indicators are there: neither is a source any more, nor are the
    partner links made from its records."""
    result = readiness.measure(datetime.date(2026, 10, 1))
    assert {s["key"] for s in result["sections"]}.isdisjoint(readiness.ARCHIVE_SECTIONS)
    assert set(_metrics(result)).isdisjoint(readiness.ARCHIVE_METRICS)
    assert {v["key"] for v in result["verdicts"]} == {
        "coverage", "review_list",  # the decisions a model could support
        "unusual", "themes", "change", "follow_up", "before_after", "value_for_money",  # analyses for later
    }  # fmt: skip


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
    assert result.status == SyncRun.Status.SUCCEEDED and len(result.details["readiness"]["verdicts"]) == 8

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
    assert "Where might teams want to review implementation or data quality?" in page
    assert "Which indicators or activities may be falling behind their targets?" not in page  # ActivityInfo's
    assert 'panel__title">Machine learning readiness check<' not in page  # no stale/fresh card


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
