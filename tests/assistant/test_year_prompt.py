"""Ask NeuroDB and ActivityInfo as an archive of past monthly results (SPEC_HOME §7; the owner's rule of November
2026): the line after the date says this year, that NeuroDB's figures come from eTools, and the years the archive
holds, whatever the mode or its records; the instructions add one archive line. The instructions start from
eTools, the examples ask about eTools, and the hub tools read the calendar year and link the daily review's own
page."""

import datetime
import itertools
from types import SimpleNamespace

import pytest
from django.urls import reverse

from neurodb.assistant import agent, hub_tools, tools
from neurodb.assistant import views as assistant_views
from neurodb.core.models import ActivityInfoSetting
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import (
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportMasterIndicator,
    ReportingYear,
    SubIndicator,
)
from neurodb.indicators.services import activityinfo

pytestmark = pytest.mark.django_db

_ids = itertools.count(920001)


def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def _ai_year(
    number: int,
    last_month: int,
    current: bool = False,
    name: str | None = None,
    children: bool = False,
    funded_by: str = "",
) -> ReportingYear:
    """A reporting year of ``number`` with one displayed database reported up to ``last_month``. With
    ``children``, the records are behind an additive HPM master indicator that counts children (the masters
    the Country overview adds up into its ActivityInfo children reached); without, the year has records but
    no ActivityInfo children figure. ``funded_by``: the database counts UNICEF-funded records only, and its
    records are funded by ``funded_by``."""
    year = ReportingYear.objects.create(name=name or str(number), year=str(number), current=current)
    n = next(_ids)
    database = Database.objects.create(
        ai_id=n, db_id=f"db{n}", name=f"Education {number}", label="Education", username="", password="",
        reporting_year=year, is_funded_by_unicef=bool(funded_by),
    )  # fmt: skip
    for month in range(1, last_month + 1):
        ActivityReportNew.objects.create(
            dbase=database, database_ai_id=str(n), indicator_id=f"i{n}", indicator_value=10,
            month_name=f"{number}-{month:02d}-01", partner_label="Partner Z", funded_by=funded_by or None,
        )  # fmt: skip
    if children:
        _children_master(year, database, ai_indicator=f"i{n}")
    activityinfo.invalidate()
    return year


def _children_master(year: ReportingYear, database: Database, ai_indicator: str) -> MasterIndicator:
    """An additive master indicator of ``database`` named for children, on the year's HPM report, whose
    TOTAL sub-indicator adds up the records of the ActivityInfo indicator ``ai_indicator``."""
    leaf = IndicatorNew.objects.create(database=database, ai_indicator=ai_indicator, name="Children reached")
    sub = SubIndicator.objects.create(database=database, name="Children reached", aggregation_method="SUM")
    sub.indicators.set([leaf])
    master = MasterIndicator.objects.create(
        database=database, name="Children reached (total)", aggregation_method="SUM", sequence=1
    )
    MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=1)
    report = NeuroReport.objects.get_or_create(name=f"HPM {year.name}", is_hpm=True, ryear=year)[0]
    NeuroReportMasterIndicator.objects.create(report=report, master=master, label="Children reached")
    return master


def _line(span: str, stop: str = "", year: int = 2026) -> str:
    return (
        f"This year is {year}. NeuroDB's figures, children reached included, come from eTools. ActivityInfo is an "
        f"archive of {span}{stop}: use the ActivityInfo tools only when the person asks about it."
    )


# ----------------------------------------------------------------------------------------- the year line
def test_in_use(db):
    """In use (records up to August, Automatic on 15 October): still an archive to Ask."""
    _ai_year(2026, 8, current=True)
    assert activityinfo.state().reason == "auto_in_use"
    assert agent.year_line() == _line("2026", " (its 2026 records stop at Aug 2026)")


def test_in_use_in_january_names_the_years_the_archive_holds(db, site_today):
    _ai_year(2026, 12, current=True)
    site_today.set(datetime.date(2027, 1, 5))  # January still brings in the December results
    assert activityinfo.state().reason == "auto_january"
    assert agent.year_line() == _line("2026", year=2027)


@pytest.mark.parametrize("mode", [None, ActivityInfoSetting.Mode.ARCHIVED, ActivityInfoSetting.Mode.IN_USE])
@pytest.mark.parametrize("children", [True, False])
def test_records_this_year_never_count_whatever_the_mode_or_the_children_indicators(db, mode, children):
    """Records of 2026 (up to April), with or without an HPM children master behind them, Automatic, set
    Archived or set In use: never "still counts them" (R1 time_roles-F2 no longer applies: nothing counts)."""
    _ai_year(2019, 12)
    _ai_year(2026, 4, current=True, children=children)
    if mode:
        _mode(mode)
    line = agent.year_line()
    assert line == _line("2019–2026", " (its 2026 records stop at Apr 2026)")
    assert "still counts" not in line and "larger of the two" not in line


def test_archived_without_records_this_year(db):
    _ai_year(2019, 12)
    _ai_year(2025, 12, current=True)
    assert not activityinfo.is_year(2026) and not activityinfo.in_use()
    assert agent.year_line() == _line("2019–2025")


def test_no_activityinfo_years(db):
    ReportingYear.objects.create(name="2026", year="2026", current=True)  # no database, no record
    assert agent.year_line() == "This year is 2026. NeuroDB holds no ActivityInfo data."


def test_the_line_follows_the_date_and_never_names_the_current_reporting_year(db):
    _ai_year(2026, 8, current=True)
    text = agent._instructions()
    assert text.startswith(agent.SYSTEM_PROMPT)
    assert text.endswith(f"Today is Thursday 15 October 2026. {agent.year_line()}")
    assert "current reporting year" not in text
    # a run with its own prompt has no year line (it is about ActivityInfo)
    own = agent.effective_instructions(agent.RunOptions(base_prompt="X"))
    assert own == "X\nToday is Thursday 15 October 2026." and "ActivityInfo" not in own


# ---------------------------------------------------------------------------------- the instructions
def test_the_sources_start_with_etools_and_end_with_the_archive():
    first = agent.SYSTEM_PROMPT.split("\n\n", 1)[0]
    holds = first.split("monitoring data: ", 1)[1]
    assert holds.startswith("eTools programme documents, partners and donor funding")
    assert (
        "and the ActivityInfo archive (databases per section and reporting year with master indicators, "
        "targets and activity reports, Neuro and HPM reports) for the years partners reported there."
    ) in first
    assert "ActivityInfo databases per section" not in first


def test_the_start_and_year_rules_point_to_etools_for_this_year():
    assert (
        "For this year's programme documents, partners and indicators start from the eTools tools "
        "(programme_details, partner_details, pd_indicator_progress); for ActivityInfo years start from "
        "list_databases or find_indicators. Run together, in the same round, every lookup that does not "
        "need another lookup's result; wait for a result only when the next lookup needs it."
    ) in agent.SYSTEM_PROMPT
    assert (
        "ActivityInfo is an archive of past monthly results: do not bring it up or use its data unless the person "
        "asks about the archive or those past results."
    ) in agent.SYSTEM_PROMPT
    assert "use this year if ActivityInfo is in use" not in agent.SYSTEM_PROMPT
    assert "The current reporting year is used when none is given." not in agent.SYSTEM_PROMPT
    assert "Start from list_databases or find_indicators" not in agent.SYSTEM_PROMPT
    for name in ("programme_details", "partner_details", "pd_indicator_progress", "list_databases"):
        assert name in tools.TOOLS, name  # the instructions name tools that exist
    assert "find_indicators" in tools.TOOLS


# ------------------------------------------------------------------------------------------ examples
def test_the_examples_ask_about_etools(client_viewer, settings):
    assert [q for topic in assistant_views.EXAMPLES for q in topic["questions"]] == [
        "Which partners have progress reports overdue?",
        "Which Child Protection programme documents are off track this year?",
        "What is the total budget of active programme documents funded by the European Union?",
        "Which grants expire in the next three months?",
        "How many HACT programmatic visits are still needed this year?",
        "How many action points are overdue, and how many of them are high priority?",
        "What do the documents people added say about school dropout?",
        "How many Syrian children live in Akkar?",
        "Is the eTools data up to date?",
    ]
    settings.AI_ASSISTANT_ENABLED = True
    settings.OPENAI_API_KEY = "test-not-a-key"
    page = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert "Which partners have progress reports overdue?" in page
    assert "Is the ActivityInfo data up to date?" not in page


def test_switched_off_ask_describes_the_search_as_it_is(client_viewer, settings):
    """The search finds pages, partners, programme documents and indicators (SPEC_HOME §5), no longer only
    "any indicator, database or report" (R1 spec_shell-4)."""
    settings.AI_ASSISTANT_ENABLED = False
    page = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert (
        "Until it is, Search finds partners, programme documents, indicators and pages by name, and the "
        "Management brief answers the common questions about results, money and partners."
    ) in page
    assert "any indicator, database or report" not in page


# ------------------------------------------------------------------------------------------ hub tools
def test_the_daily_review_links_its_own_page(db):
    from neurodb.review.models import DailyReview

    DailyReview.objects.create(date=datetime.date(2026, 10, 15), status="succeeded", summary="Calm.")
    result = hub_tools.daily_review()
    assert result["url"] == reverse("reports:daily_review") == "/daily-review/"


@pytest.fixture
def scopes(monkeypatch):
    """The scopes the management brief tool builds, without computing a brief."""
    from neurodb.reports import brief

    seen = []

    def build(scope, **kwargs):
        seen.append(scope)
        return {"headline": {}, "text": "A brief."}

    monkeypatch.setattr(brief, "build", build)
    return seen


def test_the_management_brief_reads_the_calendar_year(db, scopes):
    """No reporting year at all: the brief of this year, eTools alone, never "No reporting year."."""
    result = hub_tools.management_brief()
    assert "note" not in result and result["year"] == 2026
    assert result["url"] == f"{reverse('reports:brief')}?year=2026"
    assert scopes[-1].year == 2026 and not hasattr(scopes[-1], "reporting_year")
    assert scopes[-1].today == datetime.date(2026, 10, 15)
    assert hub_tools.management_brief(year=1990)["year"] == 2026  # not a year NeuroDB can show: this year


def test_the_management_brief_never_reads_activityinfo(db, scopes):
    """The 2025 reporting year is ticked current: neither this year's brief nor last year's reads it (the brief
    is eTools alone, the owner's rule of November 2026)."""
    _ai_year(2025, 12, current=True)
    hub_tools.management_brief()
    assert scopes[-1].year == 2026 and not hasattr(scopes[-1], "reporting_year")
    result = hub_tools.management_brief(year=2025, section="Education")
    assert result["year"] == 2025 and not hasattr(scopes[-1], "reporting_year")
    assert scopes[-1].sections == ["Education"]
    assert tools.run("management_brief", {"year": 2025})["year"] == 2025  # through the registry


@pytest.fixture
def forecast_run(monkeypatch):
    """A forecast run of 2026 that showed its forecasts (none listed)."""
    from neurodb.insights import services
    from neurodb.insights.models import IndicatorForecast

    run = SimpleNamespace(details={"year": 2026, "as_of": 8, "backtest": {"shown": True}}, finished_at=None)
    monkeypatch.setattr(services, "latest_run", lambda: run)
    monkeypatch.setattr(services, "forecasts", lambda **filters: IndicatorForecast.objects.none())
    return run


ARCHIVE_NOTE = "These forecasts were made for 2026 while ActivityInfo was in use; they are no longer updated."


def test_forecasts_say_they_are_no_longer_updated_once_archived(db, forecast_run):
    _mode(ActivityInfoSetting.Mode.IN_USE)
    assert ARCHIVE_NOTE not in hub_tools.indicator_forecasts()["note"]
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    result = hub_tools.indicator_forecasts()
    assert result["note"].startswith(ARCHIVE_NOTE) and "Say they are estimates" in result["note"]
    forecast_run.details["backtest"]["shown"] = False  # not shown yet: the note says both
    note = hub_tools.indicator_forecasts()["note"]
    assert note.startswith(ARCHIVE_NOTE) and "Forecasts are not shown yet" in note
