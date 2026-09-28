"""Aggregation methods, ratio records, tracking of ratios and Neuro Report periods (integrity fixes)."""

import datetime
import zoneinfo

import pytest

from neurodb.facts import queries
from neurodb.facts.models import ActivityReportNew
from neurodb.facts.queries import FactFilter
from neurodb.facts.services import dashboard as svc
from neurodb.indicators.models import MasterIndicator, SubIndicator
from neurodb.indicators.services.tracking import tracking

pytestmark = pytest.mark.django_db

BEIRUT = zoneinfo.ZoneInfo("Asia/Beirut")


def _values(db) -> dict[int, dict]:
    return {r["id"]: r for r in queries.master_indicator_values(FactFilter(database_id=db.id))}


def test_ratio_master_counts_the_records_of_its_denominator(hierarchy):
    ratio = _values(hierarchy["database"])[hierarchy["ratio"].id]
    # Numerator (girls) and denominator (all) share leaf i_b: the denominator's 4 records, not 6.
    assert ratio["reports"] == 4
    dash = svc.database_dashboard(hierarchy["database"])
    assert all(i.reports for i in dash.indicators)


@pytest.mark.parametrize(
    ("method", "expected"),
    [("MINIMUM", 150.0), ("MAXIMUM", 350.0), ("AVERAGE", 250.0), ("", 500.0), (None, 500.0)],
)
def test_master_methods_on_monthly_sums(hierarchy, method, expected):
    MasterIndicator.objects.filter(pk=hierarchy["master"].pk).update(aggregation_method=method)
    # Monthly sums are 150 (Jan) and 350 (Feb); a blank method is SUM, as its chip says.
    assert float(_values(hierarchy["database"])[hierarchy["master"].id]["value"]) == expected
    row = next(
        i for i in svc.database_dashboard(hierarchy["database"]).indicators if i.id == hierarchy["master"].id
    )
    assert row.value == expected and row.aggregation_method == (method or "SUM")


def test_count_sub_indicator_counts_records(hierarchy):
    SubIndicator.objects.filter(pk=hierarchy["sub"].pk).update(aggregation_method="COUNT")
    subs = queries.sub_indicator_values(
        hierarchy["master"].id, FactFilter(database_id=hierarchy["database"].id)
    )
    assert [float(s["value"]) for s in subs] == [4.0]


def test_analytical_rows_carry_master_id_and_sub_name(hierarchy):
    rows = queries.analytical_rows(FactFilter(database_id=hierarchy["database"].id))
    assert {r["master_id"] for r in rows} == {hierarchy["master"].id, hierarchy["ratio"].id}
    # The links have no label: the sub-indicator's own name is used, as on the detail page.
    assert {r["sub_indicator"] for r in rows} == {"Children reached", "Girls", "All"}


def test_ratio_tracking_is_not_prorated():
    today = datetime.date(2026, 9, 27)
    assert tracking(48.8, 50, 2026, today).status == "over_target"  # 97.6 % against 73.7 % elapsed
    assert tracking(48.8, 50, 2026, today, prorate=False).status == "on_track"
    assert tracking(30, 50, 2026, datetime.date(2026, 2, 1), prorate=False).status == "off_track"


def test_dashboard_ratio_status_uses_the_whole_target(hierarchy):
    row = next(
        i for i in svc.database_dashboard(hierarchy["database"]).indicators if i.id == hierarchy["ratio"].id
    )
    assert row.value == 40.0 and row.achieved == 80.0 and row.tracking == "off_track"


def test_overview_counts_distinct_partners(hierarchy, reporting_year):
    assert svc.overview(reporting_year)["totals"]["partners"] == 2


def test_only_ended_periods_are_offered():
    today = datetime.date(2026, 9, 28)
    assert svc.last_ended_month(2026, today) == 8
    assert svc.last_ended_month(2025, today) == 12
    assert svc.last_ended_month(2026, datetime.date(2026, 1, 10)) == 1
    assert svc.resolve_period(12, None, today, 8) == (8, None)
    assert svc.resolve_period(None, "Q3", today, 8) == (8, None)
    assert svc.resolve_period(None, "Q2", today, 8) == (6, "Q2")
    assert svc.resolve_period(None, None, today, 8) == (8, None)
    assert svc.resolve_period(None, None, today, 12) == (12, None)  # a past year opens on December


def test_neuroreport_clamps_future_months_and_lists_ended_quarters(hierarchy):
    data = svc.neuroreport(hierarchy["report"], month=12, today=datetime.date(2026, 9, 28))
    assert data["month"] == 8 and data["month_label"] == "August" and data["last_month"] == 8
    assert data["cutoff"] == datetime.date(2026, 9, 17)
    assert data["quarters"] == ["Q1", "Q2"]


def test_neuroreport_status_is_measured_at_the_end_of_the_period(hierarchy):
    # 500 of 1,200 is 41.7 %: over target at 28 Feb (15.9 % of the year), off track at 28 Sep.
    item = svc.neuroreport(hierarchy["report"], month=2, today=datetime.date(2026, 9, 28))["sections"][0][
        "items"
    ][0]
    assert item["value"] == 500.0 and item["tracking"] == "over_target"


def test_edits_made_on_the_cutoff_day_count(hierarchy):
    db = hierarchy["database"]
    for value, edited in [
        (1000, datetime.datetime(2026, 3, 17, 23, 30, tzinfo=BEIRUT)),  # on the 17th: counts
        (7, datetime.datetime(2026, 3, 18, 0, 30, tzinfo=BEIRUT)),  # the 18th in Beirut (17th UTC): does not
    ]:
        ActivityReportNew.objects.create(
            dbase=db, database_ai_id=str(db.ai_id), indicator_id="i_a", indicator_value=value,
            month_name="2026-02-01", month="2026-02", last_edited_time=edited,
        )  # fmt: skip
    data = svc.neuroreport(hierarchy["report"], month=2, today=datetime.date(2026, 9, 28))
    assert data["cutoff"] == datetime.date(2026, 3, 17)
    assert data["sections"][0]["items"][0]["value"] == 1500.0
