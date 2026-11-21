"""A partner's reporting: eTools on the partner page and the partners list; ActivityInfo, an archive of past
monthly results (the owner's rule, November 2026), on the partner's ActivityInfo page alone, opened by its
address, and in the archive's own pages."""

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
    plain = FactFilter(database_id=db.id)
    assert [s["name"] for s in queries.sites(plain, partner="Partner B")] == ["Site Akkar"]
    assert [a["name"] for a in queries.interventions_by_area(plain, "governorate", partner="Partner B")] == [
        "Akkar"
    ]
    from neurodb.facts.services import dashboard

    assert [s["name"] for s in dashboard.map_data(db, "site", partner="Partner A")["sites"]] == [
        "Site Beirut"
    ]
    assert queries.partner_activity(["Partner A", "Partner B"])[0]["records"] == 4
    assert queries.partner_activity([]) == []


def test_partner_page_shows_etools_alone(client_viewer, linked):
    """Partner A has ActivityInfo records linked: its page shows eTools only, no panel, tile, chart or link of
    ActivityInfo; the partner's ActivityInfo page still opens by its address."""
    a = linked["a"]
    response = client_viewer.get(reverse("reports:partner_profile", args=[a.id]))
    text = response.text
    page = text.split('<details class="sidebar__archive"')[0]
    assert "eTools · implementation monitoring" in text and "ActivityInfo" not in page
    activityinfo_page = reverse("reports:partner_activityinfo", args=[a.id, linked["database"].id])
    assert activityinfo_page not in text and "activityinfo_by_year" not in text
    assert 'href="' + reverse("reports:pd_monitoring") + "?partner=" in text
    assert client_viewer.get(activityinfo_page).status_code == 200


def test_partner_activityinfo_modal_and_page(client_viewer, linked):
    url = reverse("reports:partner_activityinfo", args=[linked["a"].id, linked["database"].id])
    modal = client_viewer.get(url, headers={"HX-Request": "true"})
    assert modal.status_code == 200 and "modal-header" in modal.text
    assert "Children reached (total)" in modal.text and "Jan" in modal.text and "Site Beirut" in modal.text
    row = next(i for i in modal.context["data"]["indicators"] if i["label"] == "Children reached (total)")
    assert (row["value"], row["total"], row["share"], row["months"]) == (150.0, 500.0, 30.0, {"01": 150.0})
    ratio = next(i for i in modal.context["data"]["indicators"] if i["label"] == "Share of girls")
    assert ratio["share"] is None and round(ratio["value"], 1) == 33.3  # a ratio is not a part of a whole
    page = client_viewer.get(url)
    assert page.status_code == 200 and "<html" in page.text
    nothing = client_viewer.get(
        reverse("reports:partner_activityinfo", args=[linked["b"].id, linked["database"].id])
    )
    assert nothing.status_code == 200 and "Children reached (total)" in nothing.text  # Partner B: 200 + 150


def test_unlinked_partner_page_and_snapshot_links(client_viewer, linked):
    PartnerLink.objects.filter(label="Partner B").update(partner=None, method="")
    text = client_viewer.get(reverse("reports:partner_profile", args=[linked["b"].id])).text
    assert "No ActivityInfo record is linked to this partner" not in text and "Edit the link" not in text
    # the archive's own snapshot still links the partners its records are linked to
    snapshot = client_viewer.get(reverse("reports:database_snapshot", args=[linked["database"].id])).text
    assert reverse("reports:partner_profile", args=[linked["a"].id]) in snapshot  # Partner A is linked
    assert reverse("reports:partner_profile", args=[linked["b"].id]) not in snapshot


def test_partner_list_shows_the_reporting_sources(client_viewer, linked):
    from neurodb.datamart.models import ReportedIndicator

    text = client_viewer.get(reverse("reports:partners")).text
    assert "Reporting" in text and ">eTools<" not in text
    assert (
        "ActivityInfo" not in text.split('<details class="sidebar__archive"')[0]
    )  # its records never counted
    ReportedIndicator.objects.create(
        datamart_id=1, partner=linked["a"], progress_report="PR-1", indicator="x"
    )
    text = client_viewer.get(reverse("reports:partners")).text
    assert ">eTools<" in text


def test_sidebar_separates_the_two_reporting_systems(client_viewer, linked, activityinfo_in_use, full_menu):
    # "Monthly results (ActivityInfo)" while ActivityInfo is in use; its archive otherwise (tests/web/test_menu.py)
    text = client_viewer.get(reverse("reports:partners")).text
    assert "Monthly results (ActivityInfo)" in text and ">Knowledge base<" in text
    assert "Partner progress (eTools)" not in text
    assert "Partner monitoring" in text and "Progress reports" in text


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
    assert tools.run("partner_activityinfo", {"partner": "Partner A", "database": "child"})["databases"]
    assert tools.run("partner_activityinfo", {"partner": "Partner A", "database": "wash"})["databases"] == []


def test_activityinfo_partner_rows_fold_the_amendment_suffix(linked):
    ActivityReportNew.objects.filter(partner_label="Partner A").update(project_label="LEB/PCA2026001-2")
    rows = {(r["label"], r["pd_number"]): r["records"] for r in queries.activityinfo_partner_rows()}
    assert rows[("Partner A", "LEB/PCA2026001")] == 2
    assert datetime.date.today().year >= 2026
