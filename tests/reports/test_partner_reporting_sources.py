"""The partner page brings the eTools and ActivityInfo reporting of one partner together."""

import datetime

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.facts import queries
from neurodb.facts.models import ActivityReportNew
from neurodb.facts.queries import FactFilter
from neurodb.partnerships import linking
from neurodb.partnerships.models import PCA, PartnerLink, PartnerOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def linked(hierarchy):
    """The conftest facts: Partner A (Beirut, Jan) and Partner B (Akkar, Feb) under LEB/PCA2026001."""
    a = PartnerOrganization.objects.create(
        etl_id="1", name="Partner A", partner_type="CSO", vendor_number="V1"
    )
    b = PartnerOrganization.objects.create(
        etl_id="2", name="Partner B", partner_type="CSO", vendor_number="V2"
    )
    PCA.objects.create(
        etl_id="11", number="LEB/PCA2026001-1", title="PD", partner=a, partner_name=a.name, status="active"
    )
    linking.link_activityinfo_partners()
    return {"a": a, "b": b, "database": hierarchy["database"], "master": hierarchy["master"]}


def test_partner_filter_in_the_fact_queries(linked):
    db = linked["database"]
    f = FactFilter(database_id=db.id, partner_labels=("Partner A",))
    values = {r["name"]: r["value"] for r in queries.master_indicator_values(f)}
    assert float(values["Children reached (total)"]) == 150.0  # 100 + 50 in January
    assert queries.master_monthly_values(f) == {linked["master"].id: {"01": 150.0}}
    assert [(m["month"], float(m["value"])) for m in queries.monthly_totals(f)] == [("Jan", 150.0)]
    assert [s["name"] for s in queries.sites(f)] == ["Site Beirut"]
    assert queries.partner_activity(["Partner A", "Partner B"])[0]["records"] == 4
    assert queries.partner_activity([]) == []


def test_partner_page_shows_both_sources(client_viewer, linked):
    a = linked["a"]
    response = client_viewer.get(reverse("reports:partner_profile", args=[a.id]))
    text = response.text
    assert "eTools · implementation monitoring" in text and "ActivityInfo · reporting history" in text
    assert "Child Protection 2026" in text or "Child Protection" in text
    assert reverse("reports:partner_activityinfo", args=[a.id, linked["database"].id]) in text
    assert response.context["activityinfo"]["labels"] == ["Partner A"]
    assert response.context["activityinfo"]["databases"][0]["records"] == 2
    assert response.context["chart_data"]["activityinfo_by_year"] == [("2026", 2)]
    assert 'href="' + reverse("reports:pd_monitoring") + "?partner=" in text


def test_partner_activityinfo_modal_and_page(client_viewer, linked):
    url = reverse("reports:partner_activityinfo", args=[linked["a"].id, linked["database"].id])
    modal = client_viewer.get(url, headers={"HX-Request": "true"})
    assert modal.status_code == 200 and "modal-header" in modal.text
    assert "Children reached (total)" in modal.text and "Jan" in modal.text and "Site Beirut" in modal.text
    row = next(i for i in modal.context["data"]["indicators"] if i["label"] == "Children reached (total)")
    assert (row["value"], row["total"], row["share"], row["months"]) == (150.0, 500.0, 30.0, {"01": 150.0})
    page = client_viewer.get(url)
    assert page.status_code == 200 and "<html" in page.text
    nothing = client_viewer.get(
        reverse("reports:partner_activityinfo", args=[linked["b"].id, linked["database"].id])
    )
    assert nothing.status_code == 200 and "Children reached (total)" in nothing.text  # Partner B: 200 + 150


def test_unlinked_partner_page_and_snapshot_links(client_viewer, linked):
    PartnerLink.objects.filter(label="Partner B").update(partner=None, method="")
    text = client_viewer.get(reverse("reports:partner_profile", args=[linked["b"].id])).text
    assert "No ActivityInfo record is linked to this partner" in text
    snapshot = client_viewer.get(reverse("reports:database_snapshot", args=[linked["database"].id])).text
    assert reverse("reports:partner_profile", args=[linked["a"].id]) in snapshot  # Partner A is linked
    assert reverse("reports:partner_profile", args=[linked["b"].id]) not in snapshot


def test_partner_list_shows_the_reporting_sources(client_viewer, linked):
    text = client_viewer.get(reverse("reports:partners")).text
    assert "ActivityInfo · 2" in text and "Reporting" in text


def test_sidebar_separates_the_two_reporting_systems(client_viewer, linked):
    text = client_viewer.get(reverse("reports:partners")).text
    assert "ActivityInfo reporting" in text and "eTools partner reporting" in text
    assert "PD indicators" in text and "Progress reports" in text


def test_assistant_partner_details_and_activityinfo_tool(linked):
    details = tools.run("partner_details", {"partner_id": linked["a"].id})
    assert details["activityinfo_names"] == ["Partner A"]
    assert (
        details["activityinfo_reporting"][0]["records"] == 2
        and details["activityinfo_reporting"][0]["year"] == "2026"
    )
    out = tools.run("partner_activityinfo", {"partner": "partner a", "year": 2026})
    assert out["partner"] == "Partner A" and out["databases"][0]["indicators"][0]["value"] == 150.0
    assert out["databases"][0]["indicators"][0]["by_month"] == {"01": 150.0}
    with pytest.raises(tools.ToolInputError):
        tools.run("partner_activityinfo", {"partner": "nobody"})
    empty = tools.run("partner_activityinfo", {"partner": "Partner B", "year": 2019})
    assert empty["databases"] == []


def test_activityinfo_partner_rows_fold_the_amendment_suffix(linked):
    ActivityReportNew.objects.filter(partner_label="Partner A").update(project_label="LEB/PCA2026001-2")
    rows = {(r["label"], r["pd_number"]): r["records"] for r in queries.activityinfo_partner_rows()}
    assert rows[("Partner A", "LEB/PCA2026001")] == 2
    assert datetime.date.today().year >= 2026
