"""Year-end forecast: the pattern, the forecast and its range, the back-test, the job and where it
shows."""

import datetime
import math
import random

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    ReportingYear,
    SubIndicator,
)
from neurodb.insights import forecast as fc
from neurodb.insights import services
from neurodb.insights.management.commands.forecast_indicators import run
from neurodb.insights.models import IndicatorForecast

SEASON = [0.3, 0.5, 0.8, 1, 1, 1.1, 0.7, 0.6, 1.3, 1.4, 1.5, 1.8]  # slow start, push at the end


def simulated(indicators=120, years=range(2020, 2027), seed=1):
    rnd = random.Random(seed)
    out = []
    for k in range(indicators):
        scale = rnd.uniform(500, 50000)
        own = [s * rnd.uniform(0.6, 1.4) for s in SEASON]
        for year in years:
            growth = rnd.uniform(0.7, 1.3)
            months = [scale * growth * o * rnd.uniform(0.7, 1.3) for o in own]
            target = scale * sum(own) * rnd.uniform(0.8, 1.25)
            out.append(
                fc.Instance(k * 10_000 + year, year, k % 4, f"ind {k}", f"{k % 4}.{k}", target, months, 1)
            )
    return out


# ------------------------------------------------------------------------------------ the maths
def test_the_curve_is_the_share_reached_by_each_month():
    c = fc.curve([1] * 12)
    assert c[0] == pytest.approx(1 / 12) and c[5] == pytest.approx(0.5) and c[11] == 1
    assert fc.curve([0] * 12) is None


def test_an_indicator_is_recognised_across_years_by_section_and_code_or_name():
    a = fc.Instance(1, 2024, 3, "Children reached", "CP.1", 10, [1] * 12)
    b = fc.Instance(2, 2025, 3, "Children reached (total)", "cp 1", 10, [2] * 12)
    c = fc.Instance(3, 2025, 4, "Children reached", "CP.1", 10, [2] * 12)  # another section
    assert a.keys() & b.keys() and not a.keys() & c.keys()


def test_the_pattern_blends_own_history_with_the_section_and_falls_back():
    past = [fc.Instance(i, 2024, 1, f"other {i}", "", None, [1] * 12) for i in range(6)]
    own = fc.Instance(99, 2024, 1, "mine", "M1", None, [0] * 11 + [12])  # everything in December
    history = fc.History([*past, own])
    now = fc.Instance(100, 2025, 1, "mine", "M1", None, [0] * 12)
    pattern, basis, years = history.profile(now, before=2025)
    assert years == 1 and "its own 1 past year(s) and its section" in basis
    assert pattern[5] == pytest.approx((0 + 0.5) / 2)  # half own (0 by June), half section (0.5)
    newcomer = fc.Instance(101, 2025, 1, "new", "N1", None, [0] * 12)
    assert history.profile(newcomer, before=2025)[1] == "the pattern of its section"
    assert fc.History([]).profile(newcomer, before=2025)[1] == "straight line (no history)"


def test_status_against_the_target():
    assert fc.status(110, 130, 100) == fc.ON_COURSE
    assert fc.status(70, 95, 100) == fc.SHORT
    assert fc.status(90, 110, 100) == fc.UNCERTAIN
    assert fc.status(90, 110, None) == fc.NO_TARGET


@pytest.mark.parametrize(
    ("today", "month"),
    [
        (datetime.date(2026, 10, 2), 8),  # September ended 2 days ago: not settled
        (datetime.date(2026, 10, 30), 9),
        (datetime.date(2026, 1, 15), 0),
        (datetime.date(2027, 2, 1), 12),
    ],
)
def test_only_settled_months_count(today, month):
    assert fc.settled_month(2026, today) == month


def test_on_seasonal_history_it_beats_the_straight_line_and_its_ranges_hold():
    data = simulated()
    past = [i for i in data if i.year < 2026]
    history = fc.History(past)
    result = fc.backtest(fc.cases(past, history))
    june = result["by_month"][6]
    assert june["error"] < june["linear_error"] / 2
    assert june["range_held"] >= 0.7 and result["shown"]
    assert june["short_flagged_right"] > june["linear_short_flagged_right"]


def test_the_current_year_is_forecast_with_ranges_and_statuses():
    data = simulated()
    past, current = [i for i in data if i.year < 2026], [i for i in data if i.year == 2026]
    history = fc.History(past)
    errors = fc.Errors()
    for c in fc.cases(past, history):
        errors.add(c.month, c.depth, math.log(c.actual / c.forecast))
    found = fc.forecast_year(current, history, errors, as_of=8)
    decided = [f for f in found if f.status in (fc.ON_COURSE, fc.SHORT)]
    assert decided and all((f.status == fc.SHORT) == (f.inst.total < f.inst.target) for f in decided)
    held = sum(f.low <= f.inst.total <= f.high for f in found)
    assert held / len(found) >= 0.8
    assert all(f.low >= f.to_date for f in found)  # never below what is already reported


def test_nothing_reported_and_too_early_are_said_so():
    history = fc.History([fc.Instance(1, 2025, 1, "a", "A", 10, SEASON)])
    blank = fc.Instance(2, 2026, 1, "a", "A", 10, [0] * 12)
    assert fc.forecast_year([blank], history, fc.Errors(), as_of=6)[0].status == fc.NO_REPORTS
    assert fc.forecast_year([blank], history, fc.Errors(), as_of=0)[0].status == fc.TOO_EARLY


# ----------------------------------------------------------------------- the data and the job
@pytest.fixture
def years_of_data(db, section):
    """One database a year (2023–2026), the same master indicator in each, reported monthly."""
    current = None
    for year in (2023, 2024, 2025, 2026):
        ryear = ReportingYear.objects.create(name=str(year), year=str(year), current=year == 2026)
        database = Database.objects.create(
            ai_id=year, db_id=f"db{year}", name=f"CP {year}", label=f"Child Protection {year}", username="",
            password="", section=section, reporting_year=ryear, is_funded_by_unicef=False,
        )  # fmt: skip
        activity = Activity.objects.create(
            database=database, name="Case", label="Case", ai_form_id=f"f{year}"
        )
        leaf = IndicatorNew.objects.create(
            database=database, activity=activity, ai_indicator=f"i{year}", name="Children", awp_code="1.1"
        )
        sub = SubIndicator.objects.create(
            database=database, name="Children", awp_code="1.1", aggregation_method="SUM"
        )
        sub.indicators.set([leaf])
        master = MasterIndicator.objects.create(
            database=database, name="Children in case management", awp_code="CP.1",
            aggregation_method="SUM", awp_target=1000, sequence=1,
        )  # fmt: skip
        MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=1)
        MasterIndicator.objects.create(
            database=database,
            name="Share of girls",
            awp_code="CP.2",
            aggregation_method="SUM_OVER_SUM",
            sequence=2,
        )  # not additive: not forecast
        last = 8 if year == 2026 else 12
        for month in range(1, last + 1):
            ActivityReportNew.objects.create(
                dbase=database, database_ai_id=str(year), indicator_id=f"i{year}", indicator_value=SEASON[month - 1] * 40,
                month_name=f"{year}-{month:02d}-01", partner_label="Partner A",
            )  # fmt: skip
        current = database
    return current


def test_the_job_reads_every_year_and_forecasts_the_current_one(years_of_data, settings):
    result = run("test", today=datetime.date(2026, 10, 2))
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert result.details["year"] == 2026 and result.details["as_of"] == 8
    f = IndicatorForecast.objects.get()
    assert f.database_id == years_of_data.pk and f.master.awp_code == "CP.1"
    assert f.basis == "its own 3 past year(s) and all indicators"  # too few for a section pattern
    assert f.value_to_date == pytest.approx(sum(SEASON[:8]) * 40)
    assert f.forecast == pytest.approx(sum(SEASON) * 40)  # the same pattern every year: exact
    assert f.status == IndicatorForecast.Status.UNCERTAIN  # too few past errors for a range


def test_forecasts_show_only_once_the_back_test_passed(client_viewer, years_of_data, admin_user, client):
    run("test", today=datetime.date(2026, 10, 2))
    page = client_viewer.get(reverse("insights:forecasts")).content.decode()
    assert "Forecasts are not shown yet" in page and "Children in case management" not in page
    assert services.at_risk() == []
    client.force_login(admin_user)
    page = client.get(reverse("insights:forecasts"), {"section": ""}).content.decode()
    assert "Shown to administrators only" in page and "Children in case management" in page
    answer = tools.run("indicator_forecasts", {})
    assert "not shown yet" in answer["note"]


def test_shown_forecasts_reach_the_pages_and_the_assistant(client_viewer, years_of_data):
    run("test", today=datetime.date(2026, 10, 2))
    latest = services.latest_run()
    latest.details["backtest"]["shown"] = True
    latest.save()
    IndicatorForecast.objects.update(status="likely_short", low=500, high=700)
    page = client_viewer.get(reverse("reports:database_dashboard", args=[years_of_data.pk])).content.decode()
    assert "Likely to fall short by December" in page and "likely 50–70% of target" in page
    overview = client_viewer.get(reverse("reports:overview")).content.decode()
    assert "Likely to fall short by December" in overview
    answer = tools.run(
        "indicator_forecasts", tools.validate("indicator_forecasts", {"status": "likely_short"})
    )
    assert answer["forecasts"][0]["range_pct_of_target"] == [50.0, 70.0]
    assert answer["forecasts"][0]["status"] == "Likely to fall short"
