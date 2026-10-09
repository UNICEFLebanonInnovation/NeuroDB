"""Periods read the visit's start date (stage F1): ``Visit.visit_date`` is the start date, else the end
date when eTools left the start blank. A visit from 30 December 2025 to 3 January 2026 counts in 2025
on Monitoring insights, the overview and the field monitoring page alike; its month is December; the
links it writes open the year that holds it; a visit without a start date is dated by its end and the
page says so. Urgency recency still counts from the end date."""

from __future__ import annotations

import csv
import datetime
import importlib
import io
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook

from neurodb.datamart import fm, services
from neurodb.datamart import models as dm
from neurodb.fmm import hub, metrics, powerbi, refresh, score
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


# ------------------------------------------------------------------------------------------ stage F1 check
def test_the_partner_chart_dates_a_visit_as_monitoring_insights_does(across):
    """The partner page's FM visits per year read a visit's date as ``Visit.visit_date`` does (the
    earliest start date of its rows, else the latest end date), not the date of whichever row comes
    first: a row without a start date ending in 2026 does not move a visit that started in 2025."""
    mercy = PartnerOrganization.objects.get(short_name="MCL")
    _finding(
        903,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity="CP output of 1790",
        entity_type="CP Output",
        monitoring_activity="FM-2025-090",
        monitoring_activity_id=1790,
        reference_number="FM-2025-090",
        status="completed",
        overall_finding_rating="On Track",
        start_date=None,
        end_date=datetime.date(2026, 1, 4),  # the latest end of the visit: the row read first by end date
    )
    refresh.run(triggered_by="test", today=TODAY)
    assert Visit.objects.get(key="1790").visit_date == START
    field = services.partner_datamart(mercy)["monitoring_visits_by_year"]["field"]
    for year in (2025, 2026):
        scope = _scope(year=year, partner=mercy.pk)
        assert field.get(year, 0) == metrics.kpis(scope)["visits"], year
    assert field[2025] == 1


def test_the_ai_reads_the_visit_date_with_the_start_and_the_end(across):
    from neurodb.fmm import privacy

    visit = Visit.objects.select_related("partner", "pd").get(key="1790")
    card = privacy.visit_card(visit, frozenset())
    assert (card["date"], card["start"], card["end"]) == ("2025-12-30", "2025-12-30", "2026-01-03")
    assert card["rated_on"] == "2026-01-03"  # a rating is given when the visit ends
    no_start = privacy.visit_card(Visit.objects.select_related("partner", "pd").get(key="1723"), frozenset())
    assert no_start["start"] is None and no_start["date"] == no_start["end"] == "2026-06-20"


def test_a_no_date_issue_of_an_older_build_is_not_shown_for_a_dated_visit(across):
    """Before visit dates, a build noted "no date" for a visit with a start date but no end date; the
    migration dates it by its start, so the visit page does not say it is left out of every period."""
    from neurodb.fmm.views import _data_notes

    left_out = "left out of every period"
    visit = Visit(issues={"no_date": True}, start_date=START, end_date=None, visit_date=START)
    assert not any(left_out in note for note in _data_notes(visit))
    visit.visit_date = visit.start_date = None
    assert any(left_out in note for note in _data_notes(visit))


def test_the_hact_block_names_the_year_of_the_programme_documents_quarter(across, client_viewer):
    """The partners' HACT year is that of the end date (2026), the programme documents' quarter that of
    the visit date: the visit page and the AI say Q4 2025, never a bare "Q4" read as 2026's."""
    from neurodb.fmm.ai import tools as ai_tools
    from neurodb.fmm.views import _hact

    mercy = PartnerOrganization.objects.get(short_name="MCL")
    pd = PCA.objects.get(number=PD_EDU)
    found = _hact(across, [mercy], [pd])
    assert (found["year"], found["quarter"], found["quarter_year"]) == (2026, 4, 2025)
    assert found["pds"][0]["done"] == 1  # this visit, in Q4 2025
    assert ai_tools._hact(across)["quarter_year"] == 2025
    html = client_viewer.get(reverse("fmm:visit", args=[across.key])).content.decode()
    assert "in Q4 2025" in html


def test_the_feed_and_the_workbook_read_the_visit_date(across, client_viewer):
    """The Power BI feed's ``?year=`` and ``?since=`` and the Excel workbook's period read the visit
    date, as the page does: 1790 is 2025's, and not in a feed from 31 December 2025 though it ended in
    2026."""
    _row, key = powerbi.create_key("Office workspace")
    feed = Client()

    def keys(**params) -> set[str]:
        response = feed.get(reverse("fmm_powerbi_feed", args=["visits"]), {**params, "key": key})
        text = b"".join(response.streaming_content).decode("utf-8") if response.streaming else ""
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
        return {str(r["monitoring_activity_id"]) for r in rows}

    assert keys(year="2025") == {"1790"}
    assert "1790" not in keys(year="2026")
    assert "1790" not in keys(since="2025-12-31") and "1790" in keys(since="2025-12-30")
    response = client_viewer.get(reverse("fmm:export_xlsx"), {"year": "2025", "section": ""})
    book = load_workbook(io.BytesIO(response.content))
    rows = list(book["Visits"].iter_rows(values_only=True))
    header = rows[0]
    assert [dict(zip(header, row, strict=True))["monitoring_activity_id"] for row in rows[1:]] == [1790]
    about = {row[0]: row[1] for row in book["About"].iter_rows(values_only=True)}
    assert "start date" in about["Visits dated by"] and about["Visits"] == 1


def test_a_visit_with_a_start_date_and_no_end_date_counts_and_reads_cleanly(
    across, client, admin_user, reporting_year
):
    """A planned visit eTools gives a start date but no end date yet is in the period of its start (it
    was left out of every period before) and the panels that list it show "ends —", not "ends "."""
    mercy = PartnerOrganization.objects.get(short_name="MCL")
    _finding(
        904,
        partner=mercy,
        vendor_number=mercy.vendor_number,
        entity=PD_EDU,
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-091",
        monitoring_activity_id=1791,
        reference_number="FM-2026-091",
        status="assigned",  # planned: not rated yet
        start_date=datetime.date(2026, 10, 1),
        end_date=None,
    )
    refresh.run(triggered_by="test", today=TODAY)
    visit = Visit.objects.get(key="1791")
    assert visit.visit_date == datetime.date(2026, 10, 1) and not visit.issues.get("no_date")
    assert _scope(year=2026).visits().filter(key="1791").exists()
    # counted on every page alike: Monitoring insights, the overview and the field monitoring page
    n = metrics.kpis(_scope(year=2026))["visits"]
    assert (
        n
        == 9
        == _overview_visits(reporting_year, 2026)
        == services.monitoring({"year": "2026"})["activities"]
    )
    summary = fmm_services.partner_summary(mercy.pk, 2026)
    assert summary["last"]["key"] == "1791" and summary["last"]["end_date"] is None
    assert summary["last"]["not_rated_yet"]
    client.force_login(admin_user)
    html = client.get(reverse("reports:partner_profile", args=[mercy.pk])).content.decode()
    panel = html.split('id="fmm-partner"', 1)[1].split("</section>", 1)[0]
    assert "ends —" in panel and "ends </span>" not in panel
    assert "visits starting in 2026" in panel
