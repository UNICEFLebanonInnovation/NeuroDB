"""The public landing page once ActivityInfo is an archive (SPEC_HOME §7): the archived copy names the years
partners reported there, the rows marked "always" change in both states, no "…in ActivityInfo this year"
figure stands next to "no longer updated" copy (even when this year has records), nothing about an archive
is said when no year has ActivityInfo records, and the figures' key holds this year, its ActivityInfo
reporting year and whether ActivityInfo is in use, so a change of state shows at once."""

import datetime
import itertools

import pytest
from django.core.cache import cache
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
    assert (
        "built from eTools data, with the ActivityInfo results of 2019–2026 kept as an archive." in html
    )  # meta description
    assert "built from eTools and ActivityInfo data" not in html
    # lead
    assert "funds, assurance, field monitoring, the country programme and population figures" in html
    assert "The ActivityInfo results of 2019–2026 stay available as an archive." in html
    assert "results from ActivityInfo," not in html
    # l.130, 160, 165
    assert "research and the ActivityInfo archive in one place" in html
    assert "Interactive maps by governorate and district show coverage and gaps" in html
    assert "the ActivityInfo archive keeps its site-level maps." in html
    assert "down to cadaster and site level" not in html
    assert (
        "Exports of every table, and the humanitarian performance and programme reports of the ActivityInfo "
        "years, kept in the archive." in html
    )
    # l.187-188 with the chip, l.192
    assert "Budgets, funds and assurance are managed in eTools. The monthly activity results partners" in html
    assert "reported in ActivityInfo until 2026 stay in the archive." in html
    assert '<span class="chip">ActivityInfo archive</span>' in html
    assert '<span class="chip">ActivityInfo</span>' not in html
    assert "Every night NeuroDB synchronises the eTools Datamart. Results roll up into indicators" in html
    assert "1st to the 22nd" not in html  # the import's schedule is no promise of an archive
    # l.214, l.222
    assert "Follow your section’s programme documents and progress reports" in html
    assert "Exports of every table instead of rebuilt spreadsheets" in html
    assert "monthly HPM" not in html and "Saved pivot views" not in html
    # FAQ l.289, 290, 291
    assert "the monthly activity results of 2019–2026 from ActivityInfo, kept as an archive;" in html
    assert "The ActivityInfo archive is no longer updated." in html
    assert "(and the share of the year for the ActivityInfo indicators of the archive)" in html
    assert "the share of the year for ActivityInfo ones" not in html
    # quick link
    assert "Where partners reported their monthly results until 2026." in html
    assert "Where partners report their monthly results." not in html


def test_whats_new_presents_home_and_the_archive(client, history):
    html = _landing(client)
    new = html.split('id="new"', 1)[1].split('id="explore"', 1)[0]
    assert new.index("<strong>Home</strong>") < new.index("<strong>Country overview</strong>")  # Home first
    assert "What needs you today, what changed and where your section stands." in new
    assert "Children reached, funds, delivery and assurance on one page." in new
    assert (
        "Fourteen checks every morning, on a page of their own; findings get an owner and a due date." in new
    )
    assert "<strong>ActivityInfo archive</strong>" in new
    assert "Every database and report from 2019–2026, kept as it was." in new
    assert "<strong>ActivityInfo databases</strong>" not in new
    assert "Saved and shared views" not in new  # pivot layouts are an ActivityInfo page's feature
    assert "Governorate and district maps, filtered in the browser." in new
    assert "Governorate to site level" not in new


def test_rows_marked_always_change_in_both_states(client, client_viewer, history, etools, settings):
    settings.AI_ASSISTANT_ENABLED = False
    for mode in (ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE):
        _mode(mode)
        html = _landing(client)
        assert "Home for your day, eTools partner reporting and the daily review" in html  # the pill
        assert "Management brief, daily review and eTools partner reporting" not in html
        assert "Illustration of NeuroDB, not real figures." in html  # l.102
        assert "Illustration of the overview" not in html
        assert "Figures for 2026" in html and "Reporting year" not in html  # l.113
        signed_in = _landing(client_viewer)
        assert "Open Home" in signed_in and "Open your overview" not in signed_in  # l.65
        assert f'href="{reverse("reports:overview")}"' in signed_in


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
    assert "built from eTools and ActivityInfo data." in html
    assert "results from ActivityInfo, funds, assurance and population figures" in html
    assert "ActivityInfo imports run from the 1st to the 22nd of each month" in html
    assert '<span class="chip">ActivityInfo</span>' in html and "Saved and shared views" in html
    assert "<strong>ActivityInfo databases</strong>" in html and "ActivityInfo archive" not in html
    assert "Where partners report their monthly results." in html
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
    assert "ActivityInfo" not in html
    assert "built from eTools data." in html
    assert "population estimates and research in one place" in html
    assert "Exports of every table, and a printable snapshot of every dashboard." in html
    assert "activityinfo.org" not in html


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
    assert "Where partners reported their monthly results until 2026." in html


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
