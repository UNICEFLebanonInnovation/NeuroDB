"""The assistant's tools over every eTools Datamart dataset."""

import datetime
from decimal import Decimal

import pytest

from neurodb.assistant import tools
from neurodb.datamart import catalogue
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def data(db):
    p1 = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="CSO", vendor_number="V1"
    )
    p2 = PartnerOrganization.objects.create(etl_id="2", name="Himaya", partner_type="CSO", vendor_number="V2")
    pd1 = PCA.objects.create(etl_id="11", number="LEB/PCA2024/PD1", title="PD 1", partner=p1)
    pd2 = PCA.objects.create(etl_id="12", number="LEB/PCA2024/PD2", title="PD 2", partner=p2)
    for n, (pd, rating, cost) in enumerate(
        [(pd1, "High", "1200.50"), (pd1, "Medium", "300"), (pd2, "High", "not a number")], start=1
    ):
        dm.DatamartDocument.objects.create(
            dataset="intervention_epd",
            record_key=str(n),
            intervention=pd,
            partner=pd.partner,
            title=f"{pd.number} · ePD",
            date=datetime.date(2024, n, 1),
            data={
                "pd_number": pd.number,
                "gender_rating": rating,
                "hq_support_cost": cost,
                "context": "Children in Akkar need protection services." if n == 1 else "Other context",
                "id": n,
            },
        )
    dm.DatamartDocument.objects.create(
        dataset="prp_progress_reports",
        record_key="abc",
        title="QPR · 2",
        data={
            "overall_satisfaction": "Very satisfied",
            "report_number": 2,
            "narrative": "x" * 1000,
            "contact_email": "someone@example.org",
        },
    )
    dm.FundsReservationHeader.objects.create(
        datamart_id=5,
        intervention=pd1,
        fr_number="0400001",
        total_amt=Decimal("1000"),
        data={"fr_number": "0400001", "total_amt": "1000.00", "vendor_code": "V1"},
    )
    return {"p1": p1, "pd1": pd1}


def test_datasets_lists_every_catalogue_dataset_with_counts(data):
    listing = tools.run("etools_datasets", {})
    names = {d["dataset"]: d for d in listing["datasets"]}
    assert set(names) == set(catalogue.dataset_names())
    assert names["intervention_epd"]["records"] == 3
    assert names["intervention_epd"]["linked_to"] == ["partner", "programme_document"]
    assert "datamart/users" in listing["not_read"]


def test_dataset_fields_are_described_without_contact_details(data):
    info = tools.run("etools_datasets", {"dataset": "prp_progress_reports"})
    assert "overall_satisfaction" in info["fields_with_example_values"]
    assert "contact_email" not in info["fields_with_example_values"]
    with pytest.raises(tools.ToolInputError, match="Unknown dataset"):
        tools.run("etools_datasets", {"dataset": "nope"})


def test_query_filters_by_partner_pd_field_and_number(data):
    by_partner = tools.run("etools_query", {"dataset": "intervention_epd", "partner": "amel"})
    assert by_partner["matching_records"] == 2
    assert by_partner["rows"][0]["neurodb_programme_document"]["number"] == "LEB/PCA2024/PD1"
    high = tools.run("etools_query", {"dataset": "intervention_epd", "filters": {"gender_rating": "high"}})
    assert high["matching_records"] == 2
    costly = tools.run(
        "etools_query", {"dataset": "intervention_epd", "filters": {"hq_support_cost__gte": 500}}
    )
    assert costly["matching_records"] == 1  # "not a number" is ignored, not an error
    by_pd = tools.run(
        "etools_query",
        {"dataset": "intervention_epd", "programme_document": "PD2", "fields": ["gender_rating"]},
    )
    assert by_pd["rows"][0]["gender_rating"] == "High" and "context" not in by_pd["rows"][0]
    dated = tools.run("etools_query", {"dataset": "intervention_epd", "date_from": "2024-02-01"})
    assert dated["matching_records"] == 2
    found = tools.run("etools_query", {"dataset": "intervention_epd", "search": "akkar"})
    assert found["matching_records"] == 1


def test_query_groups_and_sums(data):
    grouped = tools.run(
        "etools_query", {"dataset": "intervention_epd", "group_by": "gender_rating", "sum": "hq_support_cost"}
    )
    assert {g["gender_rating"]: (g["records"], g["sum_hq_support_cost"]) for g in grouped["groups"]} == {
        "High": (2, 1200.5),
        "Medium": (1, 300.0),
    }
    by_partner = tools.run("etools_query", {"dataset": "intervention_epd", "group_by": "partner"})
    assert {g["partner"]: g["records"] for g in by_partner["groups"]} == {"Amel Association": 2, "Himaya": 1}
    by_year = tools.run("etools_query", {"dataset": "intervention_epd", "group_by": "year"})
    assert by_year["groups"] == [{"year": 2024, "records": 3}]
    typed = tools.run("etools_query", {"dataset": "funds_reservation_headers", "sum": "total_amt"})
    assert typed["sum"] == {"total_amt": 1000.0} and typed["rows"][0]["record"] == "5"


def test_query_rejects_bad_input(data):
    for args, message in [
        ({"dataset": "prp_progress_reports", "partner": "amel"}, "not linked to partners"),
        ({"dataset": "intervention_epd", "filters": {"x;drop": 1}}, "Invalid field"),
        ({"dataset": "intervention_epd", "filters": {"a__like": 1}}, "Unknown operator"),
        ({"dataset": "intervention_epd", "partner": "nobody at all"}, "No partner"),
        ({"dataset": "offices", "date_from": "2024-01-01"}, "no main date"),
    ]:
        with pytest.raises(tools.ToolInputError, match=message):
            tools.run("etools_query", args)
    with pytest.raises(tools.ToolInputError, match="single values"):
        tools.run("etools_query", {"dataset": "intervention_epd", "filters": {"a": {"nested": 1}}})


def test_search_and_record_hide_contact_details_and_shorten(data):
    hits = tools.run("etools_search", {"text": "satisfied"})
    assert hits["datasets"][0]["dataset"] == "prp_progress_reports"
    record = tools.run("etools_record", {"dataset": "prp_progress_reports", "record": "abc"})
    assert "contact_email" not in record["data"] and len(record["data"]["narrative"]) < 500
    assert "someone@example.org" not in str(tools.run("etools_query", {"dataset": "prp_progress_reports"}))


def test_programme_and_partner_tools_count_linked_records(data):
    pd = tools.run("programme_details", {"number": "LEB/PCA2024/PD1"})
    assert pd["linked_etools_records"] == {"funds_reservation_headers": 1, "intervention_epd": 2}
    partner = tools.run("partner_details", {"partner_id": data["p1"].id})
    assert partner["linked_etools_records"] == {"intervention_epd": 2}


def test_tool_definitions_are_valid_for_the_model():
    names = {d["name"] for d in tools.definitions()}
    assert {"etools_datasets", "etools_query", "etools_search", "etools_record"} <= names


def test_large_results_are_cut_to_a_size_budget(db):
    for n in range(30):
        dm.DatamartDocument.objects.create(
            dataset="offices",
            record_key=str(n),
            title=f"Office {n}",
            data={"name": f"Office {n}", "text": "y" * 390, **{f"f{i}": "z" * 300 for i in range(10)}},
        )
    out = tools.run("etools_query", {"dataset": "offices", "limit": 50})
    assert out["matching_records"] == 30 and 1 <= len(out["rows"]) < 30 and "choose fields" in out["note"]
    slim = tools.run("etools_query", {"dataset": "offices", "limit": 50, "fields": ["name"]})
    assert len(slim["rows"]) == 30
