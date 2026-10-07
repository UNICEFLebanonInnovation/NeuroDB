"""Periods read the visit's start date (stage F1): ``Visit.visit_date`` is the start date, else the end
date when eTools left the start blank. A visit from 30 December 2025 to 3 January 2026 counts in 2025
on Monitoring insights, the overview and the field monitoring page alike; its month is December; the
links it writes open the year that holds it; a visit without a start date is dated by its end and the
page says so. Urgency recency still counts from the end date."""

from __future__ import annotations

import datetime
import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.urls import reverse

from neurodb.datamart import fm, services
from neurodb.datamart import models as dm
from neurodb.fmm import hub, metrics, refresh, score
from neurodb.fmm import scope as scope_module
from neurodb.fmm import services as fmm_services
from neurodb.fmm.models import ScoreSetting, Visit
from neurodb.fmm.scope import Scope, options
from neurodb.fmm.views import SORTS, _map_url
from neurodb.geo.models import Location
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import overview

from .conftest import PD_EDU, _finding

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
START = datetime.date(2025, 12, 30)
END = datetime.date(2026, 1, 3)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


@pytest.fixture
def across(built):
    """``built`` plus visit 1790, from 30 December 2025 to 3 January 2026 (two rows, one PD), placed
    at a cadaster with a point; refreshed."""
    mercy = PartnerOrganization.objects.get(short_name="MCL")
    for n, entity, entity_type in ((901, PD_EDU, "PD/SSFA"), (902, mercy.name, "Partner")):
        _finding(
            n,
            partner=mercy,
            vendor_number=mercy.vendor_number,
            entity=entity,
            entity_type=entity_type,
            monitoring_activity="FM-2025-090",
            monitoring_activity_id=1790,
            reference_number="FM-2025-090",
            status="completed",
            overall_finding_rating="On Track",
            start_date=START,
            end_date=END,
            location=Location.objects.get(pk=34),
        )
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded", run.error
    return Visit.objects.get(key="1790")


def _scope(**params) -> Scope:
    return Scope.from_params({"section": "", **{k: str(v) for k, v in params.items()}})


def _overview_visits(reporting_year, year: int) -> int:
    data = overview.build(overview.Scope(year=year, reporting_year=reporting_year, today=TODAY), cache=False)
    return data["delivery"]["assurance"]["field_monitoring_visits"]


# ------------------------------------------------------------------------------------------ the date
def test_the_visit_date_is_the_start_date_else_the_end_date(across):
    assert (across.start_date, across.end_date, across.visit_date) == (START, END, START)
    no_start = Visit.objects.get(key="1723")
    assert no_start.start_date is None and no_start.visit_date == no_start.end_date
    assert Visit.objects.get(key="1722").visit_date == datetime.date(2026, 5, 11)  # its start


def test_a_visit_across_the_new_year_counts_in_the_year_it_started(across, reporting_year):
    assert metrics.kpis(_scope(year=2025))["visits"] == 1
    assert metrics.kpis(_scope(year=2026))["visits"] == 8  # the visits of fm_world, as before
    assert across.key in set(_scope(year=2025).visits().values_list("key", flat=True))
    assert across.key not in set(_scope(year=2026).visits().values_list("key", flat=True))
    # the overview and the field monitoring page count it in the same year
    for year, n in ((2025, 1), (2026, 8)):
        page = services.monitoring({"year": str(year)})
        assert metrics.kpis(_scope(year=year))["visits"] == _overview_visits(reporting_year, year) == n
        assert page["activities"] == n
    assert services.monitoring({"year": "2025"})["findings"].count() == 2
    assert 2025 in services.monitoring({})["options"]["years"]
    assert 2025 in options()["years"]


def test_finding_year_q_reads_the_start_date_else_the_end_date(across):
    rows = dm.MonitoringFinding.objects
    assert set(rows.filter(fm.finding_year_q(2025)).values_list("datamart_id", flat=True)) == {901, 902}
    ended_2026 = set(rows.filter(end_date__year=2026).values_list("datamart_id", flat=True))
    assert {901, 902} <= ended_2026  # by the end date they would be 2026's
    assert not {901, 902} & set(rows.filter(fm.finding_year_q(2026)).values_list("datamart_id", flat=True))
    # a row with neither date is in no year
    assert not rows.filter(fm.finding_year_q(2025), start_date=None, end_date=None).exists()


def test_the_month_drill_reads_the_start_month(across):
    december = _scope(year=2025, month="2025-12").visits()
    assert list(december.values_list("key", flat=True)) == ["1790"]
    assert not _scope(year=2026, month="2026-01").visits().filter(key="1790").exists()
    months = metrics.summary(_scope(year=2025))["months"]
    assert set(months) == {"2025-12"} and months["2025-12"]["visits"] == 1


def test_the_data_window_and_the_newest_first_sort_read_the_visit_date(across, client_viewer):
    window = metrics.data_window(_scope(preset="all_time"))
    assert window["first"] == datetime.date(2025, 12, 30)
    keys = list(Visit.objects.order_by(*SORTS["date"], "key").values_list("key", flat=True))
    assert keys[0] == "1790"  # the oldest start, though 1728 ends earlier (14 Feb 2026)
    html = client_viewer.get(reverse("fmm:visits"), {"preset": "all_time", "section": ""}).content.decode()
    assert "30 Dec 2025" in html and "Ends 3 Jan 2026" in html


# ------------------------------------------------------------------------------------------ the notes
def test_a_visit_without_a_start_date_is_dated_by_its_end_and_noted(across):
    notes = metrics.notes(_scope(year=2026))
    without_start = _scope(year=2026).visits().filter(start_date=None).count()
    assert without_start == 7  # every visit of fm_world but 1722
    assert {"key": "dated_by_end", "n": without_start} in notes
    assert not any(n["key"] == "dated_by_end" for n in metrics.notes(_scope(year=2025)))


def test_a_visit_without_any_date_is_left_out_and_noted(across):
    row = dm.MonitoringFinding.objects.get(datamart_id=131)  # visit 1727, no start date
    row.end_date = None
    row.data["monitoring_activity_end_date"] = None
    row.save()
    refresh.run(triggered_by="test", today=TODAY)
    visit = Visit.objects.get(key="1727")
    assert visit.visit_date is None and visit.issues.get("no_date") is True
    assert {"key": "no_date", "n": 1} in metrics.notes(_scope(year=2026))
    assert not _scope(preset="all_time").visits().filter(key="1727").exists()


# ------------------------------------------------------------------------------------------ links
def test_the_map_link_opens_the_year_of_the_start_date(across):
    assert across.latitude is not None
    url = _map_url(across)
    assert "year=2025" in url and "tab=map" in url and "visit=1790" in url
    across.latitude = None
    assert _map_url(across) == ""


def test_the_pd_panel_counts_the_visit_in_the_quarter_it_started(across):
    pd = PCA.objects.get(number=PD_EDU)
    assert pd.pk in across.pd_ids
    in_2025 = fmm_services.pd_summary(pd.pk, 2025)
    assert in_2025["visits"] == 1
    assert [q["visits"] for q in in_2025["quarters"]] == [0, 0, 0, 1]
    # by its end date it would be in the first quarter of 2026: it is not
    first_quarter = Visit.objects.filter(
        pd_ids__contains=[pd.pk],
        visit_date__gte=datetime.date(2026, 1, 1),
        visit_date__lte=datetime.date(2026, 3, 31),
    )
    assert not first_quarter.filter(key="1790").exists()
    assert fmm_services.pd_summary(pd.pk, 2026)["quarters"][0]["visits"] == first_quarter.count()
    # the PD page's FM visits per year agree with the panel
    by_year = fm.visits_by_year(dm.MonitoringFinding.objects.filter(intervention=pd))
    assert by_year[2025] == 1
    assert by_year[2026] == fmm_services.pd_summary(pd.pk, 2026)["visits"]


def test_the_hub_dates_a_visit_by_its_start(across):
    assert hub.attrs_of(across)["date"] == "2025-12-30"
    assert hub.entity_name("Visit 1790", "MCL", across.visit_date).endswith("30 Dec 2025")


# ------------------------------------------------------------------------------------------ unchanged
def test_urgency_recency_still_counts_from_the_end_date():
    visit = Visit(
        rating="on_track",
        status_group="reported",
        start_date=datetime.date(2026, 1, 5),
        end_date=TODAY,
        visit_date=datetime.date(2026, 1, 5),
    )
    outcome = score.ScoreOutcome(Decimal(80), None, 0, {}, (), "", (), "")
    _total, _band, parts = score.urgency(visit, outcome, ScoreSetting(), TODAY)
    assert parts["recency"] == 30.0  # 30% of 100: ended today, though it started nine months ago


def test_the_migration_fills_the_visit_date(across):
    Visit.objects.update(visit_date=None)
    migration = importlib.import_module("neurodb.fmm.migrations.0018_visit_date")
    migration.fill_visit_date(django_apps, None)
    assert Visit.objects.get(key="1790").visit_date == START
    assert Visit.objects.get(key="1723").visit_date == datetime.date(2026, 6, 20)  # no start: its end
    assert not Visit.objects.filter(visit_date=None).exists()
