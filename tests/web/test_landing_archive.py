"""The public landing page never names ActivityInfo nor shows a figure taken from it (the owner's rule,
November 2026: NeuroDB no longer uses ActivityInfo, an archive kept in the staff pages' own archive): in use or
archived, with records this year or none, its sources, coverage cards, live note and sources belt say nothing of
it and its figures are eTools' alone; the rows marked "always" change in both states, and the figures' key
holds this year alone."""

import datetime
import itertools

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import ActivityInfoSetting
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.web import services

pytestmark = pytest.mark.django_db

_ids = itertools.count(910001)


def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def _ai_year(number: int, month: int = 12, current: bool = False) -> ReportingYear:
    """A reporting year of ``number`` with one displayed database holding one record of ``month``."""
    year = ReportingYear.objects.create(name=str(number), year=str(number), current=current)
    n = next(_ids)
    database = Database.objects.create(
        ai_id=n, db_id=f"db{n}", name=f"Education {number}", label="Education", username="", password="",
        reporting_year=year, is_funded_by_unicef=False,
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=database, database_ai_id=str(n), indicator_id=f"i{n}", indicator_value=10,
        month_name=f"{number}-{month:02d}-01", partner_label="Partner Z",
        location_adminlevel_governorate_code="NOR",
    )  # fmt: skip
    activityinfo.invalidate()  # records reach the state at the end of an import, which drops it
    return year


@pytest.fixture
def etools(db):
    """One programme document running this year with two indicators and two progress reports: eTools
    figures, so the landing's figures and their note show in both states."""
    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.create(etl_id="1", name="Placeholder Partner", vendor_number="V1")
    pd = PCA.objects.create(etl_id="11", partner=partner, number="LEB/PD1", title="PSS", status="active")
    for n, location in enumerate(["Akkar", "Bekaa"], start=1):
        dm.PDIndicator.objects.create(
            datamart_id=n, source_id=100 + n, intervention=pd, title=f"# {n}", location_name=location
        )
    for n, report in enumerate(["QPR1", "QPR2"], start=1):
        dm.ReportedIndicator.objects.create(
            datamart_id=n, intervention=pd, progress_report=report, period_end=datetime.date(2026, 3 * n, 28)
        )
    return pd


@pytest.fixture
def history(hierarchy):
    """ActivityInfo years 2019 and 2026 (hierarchy's, records up to February: on Automatic an archive on
    15 October 2026, "2019–2026")."""
    _ai_year(2019)
    return hierarchy


def _landing(client) -> str:
    return client.get(reverse("landing")).content.decode()


# ------------------------------------------------------------------------------------------ no ActivityInfo
@pytest.mark.parametrize("mode", [ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE])
def test_the_landing_page_names_no_activityinfo_in_either_state(client, history, etools, mode):
    Database.objects.filter(pk=history["database"].pk).update(
        last_monthly_update_date=timezone.now() - datetime.timedelta(days=2)
    )
    _mode(mode)
    html = _landing(client)
    assert "ActivityInfo" not in html and "activityinfo" not in html
    for words in ("Monthly results", "Historical results", "activity records this year", "2019–2026"):
        assert words not in html, words
    assert "programme documents running this year" in html  # the eTools figures stay
    assert "<span>Knowledge & evidence</span></div>" in html  # the sources belt ends with the other three
    # the meta description is the design's, the same in every state
    assert (
        '<meta name="description" content="See how UNICEF Lebanon’s NeuroDB connects programme evidence, flags '
        'what needs attention and supports decisions for children.">' in html
    )
    assert "Placeholder Partner" not in html and "LEB/PD1" not in html  # counts only


def test_the_figures_are_etools_alone(client, history, etools):
    """2026 has ActivityInfo records and ActivityInfo is set in use: the figures still count eTools alone."""
    _mode(ActivityInfoSetting.Mode.IN_USE)
    data = services.public_highlights()
    assert data["year"] == data["calendar_year"] == 2026 and data["running_programmes"] == 1
    assert not {"records", "partners", "governorates", "indicators", "sections", "databases"} & set(data)
    assert "last_update" not in data and "years" not in data


def test_rows_marked_always_change_in_both_states(client, client_viewer, history, etools, settings):
    settings.AI_ASSISTANT_ENABLED = False
    for mode in (ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE):
        _mode(mode)
        html = _landing(client)
        assert "Figures for 2026" in html and "Reporting year" not in html  # the live highlights' note
        assert "SIMULATED DATA · NOT LIVE" in html and "Illustrative UI · no actual internal data" in html
        signed_in = _landing(client_viewer)
        assert "Open Home" in signed_in and "Open your overview" not in signed_in  # the hero's second button
        assert f'href="{reverse("reports:overview")}"' in signed_in


def test_the_closing_band_opens_sign_in_or_neurodb(client_viewer, history):
    """Visitors enter through NeuroDB's own sign-in; signed in, the closing band opens NeuroDB (the site root:
    the page the person opens first), in both states."""
    visitor = Client()  # ``client_viewer`` signs ``client`` in
    for mode in (ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE):
        _mode(mode)
        band = _landing(visitor).split('<section class="ending">', 1)[1].split("</section>", 1)[0]
        assert f'<a class="endingbtn" href="{reverse("account_login")}"><span>Enter NeuroDB' in band
        signed_in = _landing(client_viewer).split('<section class="ending">', 1)[1].split("</section>", 1)[0]
        assert f'<a class="endingbtn" href="{reverse("reports:root")}"><span>Open NeuroDB' in signed_in


def test_the_key_holds_this_year_alone(hierarchy, site_today):
    _mode(ActivityInfoSetting.Mode.IN_USE)
    assert services.cache_key() == "landing:highlights:v5:2026"
    _mode(ActivityInfoSetting.Mode.ARCHIVED)  # the ActivityInfo state changes nothing
    assert services.cache_key() == "landing:highlights:v5:2026"
    site_today.set(datetime.date(2027, 1, 5))  # 1 January starts new figures
    assert services.cache_key() == "landing:highlights:v5:2027"


def test_a_year_getting_its_first_activityinfo_records_changes_nothing(client, db):
    _mode(ActivityInfoSetting.Mode.IN_USE)
    year = ReportingYear.objects.create(name="2026", year="2026", current=True)
    before = services.public_highlights()
    n = next(_ids)
    database = Database.objects.create(
        ai_id=n, db_id=f"db{n}", name="Health 2026", label="Health", username="", password="",
        reporting_year=year, is_funded_by_unicef=False,
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=database, database_ai_id=str(n), indicator_id="i1", indicator_value=5, month_name="2026-09-01",
        partner_label="Partner Y",
    )  # fmt: skip
    activityinfo.invalidate()  # the end of the import
    assert services.public_highlights() == before
    assert "ActivityInfo" not in _landing(client)
