"""Ask NeuroDB and ActivityInfo as an archive (SPEC_HOME §7): the line after the date says this year and
whether ActivityInfo is in use or an archive, in four sentences; whether this year has ActivityInfo figures
follows its records (as the Home and Country overview tiles do), the mode only the tense. The instructions
start from eTools, the examples ask about eTools, and the hub tools read the calendar year and link the
daily review's own page."""

import datetime
import itertools
from types import SimpleNamespace

import pytest
from django.urls import reverse

from neurodb.assistant import agent, hub_tools, tools
from neurodb.assistant import views as assistant_views
from neurodb.core.models import ActivityInfoSetting
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo

pytestmark = pytest.mark.django_db

_ids = itertools.count(920001)


def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def _ai_year(number: int, last_month: int, current: bool = False, name: str | None = None) -> ReportingYear:
    """A reporting year of ``number`` with one displayed database reported up to ``last_month``."""
    year = ReportingYear.objects.create(name=name or str(number), year=str(number), current=current)
    n = next(_ids)
    database = Database.objects.create(
        ai_id=n, db_id=f"db{n}", name=f"Education {number}", label="Education", username="", password="",
        reporting_year=year, is_funded_by_unicef=False,
    )  # fmt: skip
    for month in range(1, last_month + 1):
        ActivityReportNew.objects.create(
            dbase=database, database_ai_id=str(n), indicator_id=f"i{n}", indicator_value=10,
            month_name=f"{number}-{month:02d}-01", partner_label="Partner Z",
        )  # fmt: skip
    activityinfo.invalidate()
    return year


ARCHIVED_WITH_RECORDS = (
    "This year is 2026. ActivityInfo is no longer updated: its 2026 records stop at {month}, and NeuroDB's "
    "children reached for 2026 still counts them (the larger of the two sources). For progress since then "
    "use the eTools tools; the ActivityInfo tools cover {span}."
)


# ------------------------------------------------------------------------------- the four sentences
def test_in_use(db):
    _ai_year(2026, 8, current=True)  # records up to August: in use on 15 October (Automatic)
    assert activityinfo.state().reason == "auto_in_use"
    assert agent.year_line() == "This year is 2026. ActivityInfo is in use (reporting year 2026)."


def test_in_use_in_january_names_the_year_the_import_serves(db, site_today):
    _ai_year(2026, 12, current=True)
    site_today.set(datetime.date(2027, 1, 5))  # January still brings in the December results
    assert activityinfo.state().reason == "auto_january"
    assert agent.year_line() == "This year is 2027. ActivityInfo is in use (reporting year 2026)."


def test_archived_with_records_this_year(db):
    """The import left on over a year whose records stop in April: an archive, but 2026 still has
    ActivityInfo figures, so Ask says they stop and still count, as the Home tile does."""
    _ai_year(2019, 12)
    _ai_year(2026, 4, current=True)
    assert activityinfo.state().reason == "auto_not_recent" and activityinfo.is_year(2026)
    assert agent.year_line() == ARCHIVED_WITH_RECORDS.format(month="Apr 2026", span="2019–2026")


def test_set_archived_with_recent_records_still_counts_them(db):
    _ai_year(2026, 8, current=True)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    assert agent.year_line() == ARCHIVED_WITH_RECORDS.format(month="Aug 2026", span="2026")


def test_archived_without_records_this_year(db):
    _ai_year(2019, 12)
    _ai_year(2025, 12, current=True)
    assert not activityinfo.is_year(2026) and not activityinfo.in_use()
    assert agent.year_line() == (
        "This year is 2026. ActivityInfo is an archive of 2019–2025: partners no longer report there. For "
        "this year use the eTools tools; use the ActivityInfo tools only for 2019–2025."
    )


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
        "list_databases or find_indicators. Several lookups can run in parallel."
    ) in agent.SYSTEM_PROMPT
    assert (
        "When no ActivityInfo year is given, use this year if ActivityInfo is in use (see the line after "
        "these instructions), else say which year you used."
    ) in agent.SYSTEM_PROMPT
    assert "The current reporting year is used when none is given." not in agent.SYSTEM_PROMPT
    assert "Start from list_databases or find_indicators" not in agent.SYSTEM_PROMPT
    for name in ("programme_details", "partner_details", "pd_indicator_progress", "list_databases"):
        assert name in tools.TOOLS, name  # the instructions name tools that exist
    assert "find_indicators" in tools.TOOLS


# ------------------------------------------------------------------------------------------ examples
def test_the_examples_ask_about_etools(client_viewer, settings):
    assert assistant_views.EXAMPLES == [
        "Which Child Protection programme documents are off track this year?",
        "Which partners have progress reports overdue?",
        "What is the total budget of active programme documents funded by the European Union?",
        "How many Syrian children live in Akkar?",
        "Is the eTools data up to date?",
    ]
    settings.AI_ASSISTANT_ENABLED = True
    settings.OPENAI_API_KEY = "test-not-a-key"
    page = client_viewer.get(reverse("assistant:ask")).content.decode()
    assert "Which partners have progress reports overdue?" in page
    assert "Is the ActivityInfo data up to date?" not in page


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
    assert scopes[-1].year == 2026 and scopes[-1].reporting_year is None
    assert scopes[-1].today == datetime.date(2026, 10, 15)
    assert hub_tools.management_brief(year=1990)["year"] == 2026  # not a year NeuroDB can show: this year


def test_the_management_brief_counts_activityinfo_only_in_its_years(db, scopes):
    """The 2025 reporting year is ticked current (the import's year): this year's brief has no ActivityInfo
    reporting year, last year's has it."""
    last_year = _ai_year(2025, 12, current=True)
    hub_tools.management_brief()
    assert scopes[-1].year == 2026 and scopes[-1].reporting_year is None
    result = hub_tools.management_brief(year=2025, section="Education")
    assert result["year"] == 2025 and scopes[-1].reporting_year == last_year
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
