"""Every page, partial and internal API endpoint renders for a signed-in viewer (smoke + contract tests)."""

import datetime

import pytest
from django.urls import reverse

from neurodb.core.models import PopulationFigure, SavedView, SyncRun
from neurodb.library.models import Map, Resource, ResourceType
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def partnerships(db):
    partner = PartnerOrganization.objects.create(
        etl_id="11", name="Partner A", partner_type="Civil Society Organization", vendor_number="V11"
    )
    today = datetime.date.today()
    pd = PCA.objects.create(
        etl_id="21",
        partner=partner,
        partner_name="Partner A",
        number="LEB/PCA2026001-1",
        title="Protection services",
        status="active",
        document_type="PD",
        start=today - datetime.timedelta(days=200),
        end=today + datetime.timedelta(days=30),
        section_names=["Child Protection"],
        offices_set=["Beirut"],
        donors=["Donor X"],
        grants=["SC001"],
        donors_set=[{"donor": "Donor X", "grant": "SC001", "value": "1000.5"}],
        location_p_codes=["LB1"],
        total_budget="5000",
    )
    return {"partner": partner, "pd": pd}


@pytest.fixture
def library(db):
    rtype = ResourceType.objects.create(name="Evaluation")
    resource = Resource.objects.create(
        title="Learning study",
        publication_year="2025",
        section="Education",
        type=rtype,
        resource_file=b"%PDF-1.4",
        resource_file_name="study.pdf",
    )
    Map.objects.create(name="Schools map", status="Completed", link="https://example.org/map")
    return resource


@pytest.fixture
def population(db):
    PopulationFigure.objects.create(
        year=2026, nationality="LEB", level="national", value=1000, category="total"
    )
    PopulationFigure.objects.create(
        year=2026,
        nationality="SYR",
        level="governorate",
        area_code="BEI",
        area_name="Beirut",
        value=400,
        category="total",
    )


def _get(client, url, **headers):
    response = client.get(url, **headers)
    assert response.status_code == 200, f"{url} -> {response.status_code}"
    return response


def test_anonymous_users_are_sent_to_login(client, hierarchy):
    response = client.get(reverse("reports:database_dashboard", args=[hierarchy["database"].id]))
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


def test_login_page_renders(client):
    response = _get(client, reverse("account_login"))
    assert b"Sign in" in response.content


def test_healthz_is_public(client):
    assert client.get("/healthz/").status_code == 200


def test_overview_and_database_pages(client_viewer, hierarchy):
    db = hierarchy["database"]
    master = hierarchy["master"]
    html = _get(client_viewer, reverse("reports:overview")).content.decode()
    assert "Child Protection" in html
    html = _get(client_viewer, reverse("reports:database_dashboard", args=[db.id])).content.decode()
    assert "Children reached (total)" in html and 'id="chart-data"' in html
    for name in ("database_analytical", "database_snapshot", "database_map"):
        _get(client_viewer, reverse(f"reports:{name}", args=[db.id]))
    _get(client_viewer, reverse("reports:database_map", args=[db.id]) + "?level=site&partner=Partner+A")
    _get(client_viewer, reverse("reports:indicator_detail", args=[db.id, master.id]))


def test_htmx_requests_get_partials(client_viewer, hierarchy):
    db = hierarchy["database"]
    html = _get(
        client_viewer,
        reverse("reports:database_dashboard", args=[db.id]) + "?status=off_track",
        HTTP_HX_REQUEST="true",
    ).content.decode()
    assert "<html" not in html
    modal = _get(
        client_viewer,
        reverse("reports:indicator_detail", args=[db.id, hierarchy["master"].id]),
        HTTP_HX_REQUEST="true",
    ).content.decode()
    assert 'class="modal-header"' in modal


def test_raw_data_exports(client_viewer, hierarchy):
    db = hierarchy["database"]
    csv = client_viewer.get(reverse("reports:database_raw_data", args=[db.id, "csv"]))
    assert csv.status_code == 200 and csv["Content-Type"].startswith("text/csv")
    body = b"".join(csv.streaming_content).decode("utf-8-sig")
    assert body.splitlines()[0].startswith("master_indicator,target")
    xlsx = client_viewer.get(reverse("reports:database_raw_data", args=[db.id, "xlsx"]))
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    assert client_viewer.get(reverse("reports:database_raw_data", args=[db.id, "pdf"])).status_code == 404


def test_report_pages(client_viewer, hierarchy):
    report = hierarchy["report"]
    _get(client_viewer, reverse("reports:report_dashboard", args=[report.id]) + "?month=2")
    _get(client_viewer, reverse("reports:report_analytical", args=[report.id]))
    html = _get(
        client_viewer, reverse("reports:report_hpm", args=[report.id]) + "?quarter=Q1"
    ).content.decode()
    assert "Children reached" in html
    _get(client_viewer, reverse("reports:report_hpm", args=[report.id]) + "?month=3", HTTP_HX_REQUEST="true")


def test_hpm_comment_requires_own_section(client_viewer, hierarchy):
    report = hierarchy["report"]
    link = report.neuroreportmasterindicator_set.first()
    response = client_viewer.post(
        reverse("reports:report_hpm", args=[report.id]),
        {"master": link.id, "comment": "On track", "related_month": "02"},
    )
    assert response.status_code == 403


def test_partnership_pages(client_viewer, partnerships, hierarchy):
    pd, partner = partnerships["pd"], partnerships["partner"]
    html = _get(client_viewer, reverse("reports:programmes") + "?scope=active&donor=Donor+X").content.decode()
    assert "LEB/PCA2026001-1" in html
    _get(client_viewer, reverse("reports:programmes") + "?page=1", HTTP_HX_REQUEST="true")
    _get(client_viewer, reverse("reports:programme_summary"))
    _get(client_viewer, reverse("reports:programme_detail", args=[pd.id]))
    _get(client_viewer, reverse("reports:programme_detail", args=[pd.id]), HTTP_HX_REQUEST="true")
    _get(client_viewer, reverse("reports:donors") + "?donor=Donor+X")
    _get(client_viewer, reverse("reports:partners") + "?q=Partner")
    html = _get(client_viewer, reverse("reports:partner_profile", args=[partner.id])).content.decode()
    assert "Partner A" in html
    xlsx = client_viewer.get(reverse("reports:export_etools_locations"))
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


def test_resource_pages(client_viewer, library, population, reporting_year):
    _get(client_viewer, reverse("reports:library") + "?q=Learning")
    download = client_viewer.get(reverse("reports:library_download", args=[library.id]))
    assert download.status_code == 200 and b"".join(download.streaming_content) == b"%PDF-1.4"
    _get(client_viewer, reverse("reports:maps"))
    html = _get(client_viewer, reverse("reports:population") + "?year=2026").content.decode()
    assert "Beirut" in html
    assert client_viewer.get(reverse("reports:population") + "?view=bogus").status_code == 400


def test_health_and_search(client_viewer, hierarchy):
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS, status=SyncRun.Status.FAILED, error="timeout")
    _get(client_viewer, reverse("reports:data_health"))
    html = _get(client_viewer, reverse("reports:search") + "?q=children").content.decode()
    assert "Children reached (total)" in html
    partial = _get(
        client_viewer, reverse("reports:search") + "?q=zz-nothing", HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "No results" in partial


def test_internal_api(client_viewer, hierarchy, partnerships):
    db, report = hierarchy["database"], hierarchy["report"]
    dash = _get(client_viewer, reverse("api:dashboard", args=[db.id])).json()
    assert dash["indicators"][0]["label"] == "Children reached (total)"
    rows = _get(client_viewer, reverse("api:analytical", args=[db.id])).json()
    assert rows and "indicator_value" in rows[0]
    assert client_viewer.get(reverse("api:analytical", args=[db.id]) + "?emergency=maybe").status_code == 400
    geo = _get(client_viewer, reverse("api:map", args=[db.id]) + "?level=governorate").json()
    assert geo["totals"]["interventions"] == 4
    assert _get(client_viewer, reverse("api:hpm", args=[report.id]) + "?month=2").json()["month"] == 2
    assert client_viewer.get(reverse("api:hpm", args=[report.id]) + "?month=13").status_code == 400
    _get(client_viewer, reverse("api:report_analytical", args=[report.id]))
    assert _get(client_viewer, reverse("api:programmes")).json()["count"] == 1
    _get(client_viewer, reverse("api:donors"))
    _get(client_viewer, reverse("api:health"))


def test_saved_views_round_trip(client_viewer, viewer, admin_user, hierarchy):
    db = hierarchy["database"]
    api = reverse("api:saved_views")
    payload = {
        "name": "By month",
        "page": "reports:database_analytical",
        "object_id": db.id,
        "layout": {"rows": ["partner"]},
        "is_shared": True,
    }
    created = client_viewer.post(api, payload, content_type="application/json")
    assert created.status_code == 201, created.content
    view_id = created.json()["id"]
    listed = client_viewer.get(api, {"page": "reports:database_analytical", "object_id": db.id}).json()
    assert [v["name"] for v in listed] == ["By month"]
    assert (
        client_viewer.post(
            api, {**payload, "page": "admin:index"}, content_type="application/json"
        ).status_code
        == 400
    )
    # Someone else's view cannot be deleted by a viewer; an administrator can.
    other = SavedView.objects.create(
        owner=admin_user, name="Admin view", page="reports:database_analytical", object_id=db.id
    )
    assert client_viewer.delete(reverse("api:saved_view_detail", args=[other.id])).status_code == 403
    assert client_viewer.delete(reverse("api:saved_view_detail", args=[view_id])).status_code == 204


def test_api_requires_authentication(client, hierarchy):
    response = client.get(reverse("api:dashboard", args=[hierarchy["database"].id]))
    assert response.status_code in (302, 401, 403)


def test_csp_header_and_nonce(client_viewer, hierarchy):
    response = _get(client_viewer, reverse("reports:overview"))
    policy = response["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in policy
    nonce = policy.split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"'.encode() in response.content
