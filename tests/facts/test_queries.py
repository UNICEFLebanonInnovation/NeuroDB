import datetime

import pytest

from neurodb.facts import queries
from neurodb.facts.queries import FactFilter
from neurodb.facts.services import dashboard as svc

pytestmark = pytest.mark.django_db


def test_master_values_sum_and_ratio(hierarchy):
    db = hierarchy["database"]
    rows = {r["id"]: r for r in queries.master_indicator_values(FactFilter(database_id=db.id))}
    assert float(rows[hierarchy["master"].id]["value"]) == 500.0
    assert rows[hierarchy["master"].id]["reports"] == 4
    ratio = rows[hierarchy["ratio"].id]
    assert round(float(ratio["value"]), 2) == 40.0  # (50+150) / (500) * 100


def test_month_and_cutoff_filters(hierarchy):
    db = hierarchy["database"]
    jan = queries.master_indicator_values(FactFilter(database_id=db.id, month_to=1))
    assert float(next(r for r in jan if r["id"] == hierarchy["master"].id)["value"]) == 150.0
    cut = queries.master_indicator_values(FactFilter(database_id=db.id, edited_before=datetime.date(2026, 2, 1)))
    assert next(r for r in cut if r["id"] == hierarchy["master"].id)["value"] is None


def test_report_overrides(hierarchy):
    db = hierarchy["database"]
    rows = queries.master_indicator_values(FactFilter(database_id=db.id), report_id=hierarchy["report"].id)
    assert len(rows) == 1
    assert rows[0]["label"] == "Children reached"
    assert rows[0]["target"] == 1200


def test_sub_values_and_analytical(hierarchy):
    db = hierarchy["database"]
    subs = queries.sub_indicator_values(hierarchy["master"].id, FactFilter(database_id=db.id))
    assert [float(s["value"]) for s in subs] == [500.0]
    rows = queries.analytical_rows(FactFilter(database_id=db.id))
    assert sum(float(r["indicator_value"]) for r in rows if r["sub_indicator"] is None or True) > 0
    assert {r["month"] for r in rows} == {"Jan", "Feb"}
    assert queries.analytical_rows(FactFilter(database_id=db.id), emergency="yes") == []


def test_area_and_site_queries(hierarchy):
    db = hierarchy["database"]
    govs = queries.interventions_by_area(FactFilter(database_id=db.id), "governorate")
    assert {g["code"] for g in govs} == {"BEI", "AKK"}
    filtered = queries.interventions_by_area(FactFilter(database_id=db.id), "district", partner="Partner A")
    assert len(filtered) == 1
    sites = queries.sites(FactFilter(database_id=db.id))
    assert all(isinstance(s["latitude"], float) for s in sites)
    with pytest.raises(KeyError):
        queries.interventions_by_area(FactFilter(database_id=db.id), "not-a-level")


def test_dashboard_service_tracking(hierarchy):
    dash = svc.database_dashboard(hierarchy["database"])
    row = next(i for i in dash.indicators if i.id == hierarchy["master"].id)
    assert row.value == 500.0 and row.target == 1000.0
    assert row.tracking in {"on_track", "off_track", "over_target"}
    assert dash.status_counts["no_target"] == 0
    assert dash.totals["reports"] == 4


def test_neuroreport_service(hierarchy):
    data = svc.neuroreport(hierarchy["report"], month=2)
    assert data["totals"]["indicators"] == 1
    item = data["sections"][0]["items"][0]
    assert item["value"] == 500.0 and item["previous"] == 150.0 and item["delta"] == 350.0
