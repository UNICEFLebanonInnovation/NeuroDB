"""The public landing page once ActivityInfo is an archive (SPEC_HOME §7): the archived copy names the years
partners reported there ("ActivityInfo archive 2019–2026"), while it is in use it is a current source and the
word "archive" is not used, the rows marked "always" change in both states, no "…in ActivityInfo this year"
figure stands next to archive copy (even when this year has records), nothing about ActivityInfo is said
when no year has its records, and the figures' key holds this year, its ActivityInfo reporting year and
whether ActivityInfo is in use, so a change of state shows at once."""

import datetime
import itertools

import pytest
from django.core.cache import cache
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


# ------------------------------------------------------------------------------------------ the archive
def test_archived_copy_names_the_years_of_the_archive(client, history):
    assert not activityinfo.in_use() and activityinfo.state().span == "2019–2026"
    html = _landing(client)
    # the three places the design names ActivityInfo: the sources of the data scene, the coverage card and
    # the sources belt
    assert (
        "<b>Historical results — ActivityInfo</b><span>Past-year evidence for comparison, 2019–2026</span>"
        in html
    )
    assert "<b>Historical results</b><span>ActivityInfo archive 2019–2026 and year comparisons</span>" in html
    assert "<span>ActivityInfo archive 2019–2026</span>" in html
    assert "Monthly results — ActivityInfo" not in html and "ActivityInfo results of the year" not in html
    # the meta description is the design's, the same in every state
    assert (
        '<meta name="description" content="See how UNICEF Lebanon’s NeuroDB connects programme evidence, flags '
        'what needs attention and supports decisions for children.">' in html
    )
    assert "ActivityInfo imported" not in html  # no ActivityInfo figure


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
    """Visitors enter through NeuroDB's own sign-in; signed in, the closing band opens NeuroDB (Home), in
    both states."""
    visitor = Client()  # ``client_viewer`` signs ``client`` in
    for mode in (ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE):
        _mode(mode)
        band = _landing(visitor).split('<section class="ending">', 1)[1].split("</section>", 1)[0]
        assert f'<a class="endingbtn" href="{reverse("account_login")}"><span>Enter NeuroDB' in band
        signed_in = _landing(client_viewer).split('<section class="ending">', 1)[1].split("</section>", 1)[0]
        assert f'<a class="endingbtn" href="{reverse("reports:overview")}"><span>Open NeuroDB' in signed_in


def test_no_activityinfo_tiles_when_archived_even_with_records_this_year(client, history, etools):
    """2026 has ActivityInfo records (its figures stay on the Country overview), but an archive shows no
    "…reporting in ActivityInfo this year" tile next to copy saying it is no longer updated."""
    assert activityinfo.is_year(2026)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    html = _landing(client)
    for label in (
        "ActivityInfo results tracked against targets",
        "governorates in ActivityInfo reports this year",
        "activity records this year",
        "partners reporting in ActivityInfo this year",
        "programme sections in ActivityInfo",
        "ActivityInfo imported",
    ):
        assert label not in html, label
    assert "programme documents running this year" in html  # the eTools figures stay
    assert "Placeholder Partner" not in html and "LEB/PD1" not in html
    data = services.public_highlights()
    assert data["records"] == data["partners"] == data["governorates"] == data["indicators"] == 0
    assert data["last_update"] is None and data["year"] == 2026


def test_in_use_keeps_todays_copy_and_its_figures(client, history):
    Database.objects.filter(pk=history["database"].pk).update(
        last_monthly_update_date=timezone.now() - datetime.timedelta(days=2)
    )
    _mode(ActivityInfoSetting.Mode.IN_USE)
    html = _landing(client)
    # a current source, in the same three places, and never called an archive
    assert "<b>Monthly results — ActivityInfo</b>" in html
    assert "<b>Monthly results</b><span>ActivityInfo results of the year and year comparisons</span>" in html
    assert "<span>ActivityInfo</span>" in html
    assert "ActivityInfo archive" not in html and "Historical results" not in html and "2019–2026" not in html
    # this year's ActivityInfo figures (hierarchy: 4 records, 2 partners, 2 governorates, 2 masters)
    assert "partners reporting in ActivityInfo this year" in html
    assert "governorates in ActivityInfo reports this year" in html
    assert 'data-count="4"' in html and "ActivityInfo imported 2\xa0days ago" in html
    assert "Partner A" not in html and "Partner B" not in html  # counts only


def test_nothing_about_an_archive_without_activityinfo_years(client, db):
    """No year has ActivityInfo records: every sentence naming the archive is left out, and so is the link
    to ActivityInfo."""
    assert activityinfo.state().span == "" and not activityinfo.in_use()
    html = _landing(client)
    assert "ActivityInfo" not in html and "Historical results" not in html and "Monthly results" not in html
    assert "activityinfo.org" not in html
    assert "<span>Knowledge & evidence</span></div>" in html  # the sources belt ends with the other three


def test_the_figures_are_this_years_and_the_ai_counts_come_from_its_reporting_year(client, etools):
    """The reporting year ticked current is 2025 (records to December): on 15 October 2026 the eTools
    figures are 2026's and no 2025 ActivityInfo count is shown as "this year"."""
    _ai_year(2025, 12, current=True)
    _mode(ActivityInfoSetting.Mode.IN_USE)  # in use, but 2026 has no ActivityInfo records
    data = services.public_highlights()
    assert data["year"] == 2026 and data["calendar_year"] == 2026
    assert data["records"] == 0 and data["partners"] == 0
    html = _landing(client)
    assert "Figures for 2026" in html and "partners reporting in ActivityInfo this year" not in html


def test_the_key_holds_the_year_its_reporting_year_and_the_state(hierarchy, site_today):
    ry = hierarchy["database"].reporting_year
    _mode(ActivityInfoSetting.Mode.IN_USE)
    assert services.cache_key() == f"landing:highlights:v4:2026:{ry.pk}:1"
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    assert services.cache_key() == f"landing:highlights:v4:2026:{ry.pk}:0"
    site_today.set(datetime.date(2027, 1, 5))  # 1 January starts a year without ActivityInfo records
    assert services.cache_key() == "landing:highlights:v4:2027::0"


def test_a_change_of_state_shows_at_once_without_clearing_the_cache(client, hierarchy):
    _mode(ActivityInfoSetting.Mode.IN_USE)
    assert "partners reporting in ActivityInfo this year" in _landing(client)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)  # the admin page's save: the state is dropped, the figures kept
    assert cache.get(services.cache_key()) is None  # a key of its own
    html = _landing(client)
    assert "partners reporting in ActivityInfo this year" not in html
    assert "<span>ActivityInfo archive 2026</span>" in html  # hierarchy's records: 2026 only


def test_a_year_getting_its_first_records_starts_new_figures(client, db):
    """A 2026 reporting year whose databases wait for records, then the import brings the first ones: the
    figures' key moves to that reporting year (no figures of the empty year are served for an hour)."""
    _mode(ActivityInfoSetting.Mode.IN_USE)
    year = ReportingYear.objects.create(name="2026", year="2026", current=True)
    before = services.cache_key()
    assert before == "landing:highlights:v4:2026::1"
    services.public_highlights()
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
    assert services.cache_key() == f"landing:highlights:v4:2026:{year.pk}:1" != before
    assert services.public_highlights()["partners"] == 1
    assert "partners reporting in ActivityInfo this year" in _landing(client)
