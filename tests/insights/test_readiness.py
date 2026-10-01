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
