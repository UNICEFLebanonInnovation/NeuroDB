import datetime as dt

import pytest
import responses
from django.utils import timezone

from neurodb.accounts.models import Section
from neurodb.core.models import SyncRun
from neurodb.geo.models import Location, LocationType
from neurodb.integrations.etools import locations, sync
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, Agreement, PartnerOrganization, Travel, TravelActivity

pytestmark = pytest.mark.django_db

BASE = "https://etools.test"


def make_client() -> EToolsClient:
    return EToolsClient(BASE, "abc", session=make_session("Token abc", backoff=0))


def make_run(target: str, job: str = SyncRun.Job.ETOOLS) -> SyncRun:
    return SyncRun.objects.create(job=job, target=target, triggered_by="test")


def partner_payload(pk: int, **extra) -> dict:
    item = {
        "id": pk,
        "name": f"Partner {pk}",
        "short_name": f"P{pk}",
        "partner_type": "Civil Society Organization",
        "cso_type": "National",
        "rating": "Low",
        "last_assessment_date": "2024-03-01",
        "vendor_number": f"V{pk}",
        "hidden": False,
        "deleted_flag": False,
        "shared_with": ["UNDP"],
        "total_ct_cp": 1234.5,
        "email": None,
        "postal_code": "ignored",
    }
    item.update(extra)
    return item


@responses.activate
def test_sync_partners_upserts_and_isolates_bad_items():
    responses.get(
        f"{BASE}/api/v2/partners/",
        json=[partner_payload(1), partner_payload(2, name=None, vendor_number=None), partner_payload(3)],
    )
    run = sync.sync_partners(make_run("partners"), client=make_client())
    # A missing name is coerced to '' (NOT NULL column) rather than failing the row.
    assert run.status == SyncRun.Status.SUCCEEDED
    assert (run.rows_in, run.rows_written, run.rows_failed) == (3, 3, 0)
    assert PartnerOrganization.objects.get(etl_id="2").name == ""
    partner = PartnerOrganization.objects.get(etl_id="1")
    assert partner.name == "Partner 1"
    assert partner.last_assessment_date == dt.date(2024, 3, 1)
    assert partner.total_ct_cp == "1234.5"
    assert partner.email is None  # nullable column keeps None
    assert partner.shared_with == ["UNDP"]


@responses.activate
def test_sync_partners_counts_failures_and_continues():
    good = partner_payload(1)
    broken = {"id": 2, "name": "x" * 300, "partner_type": "Government", "vendor_number": "V2"}  # truncated to 255
    unique_clash = partner_payload(3, name="Partner 1", vendor_number="V1")  # (name, vendor_number) unique in v2
    responses.get(f"{BASE}/api/v2/partners/", json=[good, broken, unique_clash])
    run = sync.sync_partners(make_run("partners"), client=make_client())
    assert run.rows_in == 3
    assert run.rows_failed == 1
    assert run.status == SyncRun.Status.PARTIAL
    assert PartnerOrganization.objects.count() == 2
    assert len(PartnerOrganization.objects.get(etl_id="2").name) == 255


@responses.activate
def test_sync_agreements_with_missing_partner_still_writes():
    PartnerOrganization.objects.create(etl_id="1", name="P1", partner_type="Government", vendor_number="V1")
    responses.get(
        f"{BASE}/api/v2/agreements/",
        json=[
            {"id": 10, "partner": 1, "agreement_number": "LEBA/PCA1", "agreement_type": "PCA", "start": "2024-01-01",
             "end": None, "partner_name": "P1", "signed_by_unicef_date": "", "signed_by_partner_date": "2024-01-02"},
            {"id": 11, "partner": 999, "agreement_number": "LEBA/PCA2", "agreement_type": "SSFA"},
        ],
    )  # fmt: skip
    run = sync.sync_agreements(make_run("agreements"), client=make_client())
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["missing_refs"] == {"partnerorganization": 1}
    a10 = Agreement.objects.get(etl_id="10")
    assert (a10.partner.etl_id, a10.start, a10.end, a10.signed_by_unicef_date) == ("1", dt.date(2024, 1, 1), None, None)
    assert Agreement.objects.get(etl_id="11").partner is None


@responses.activate
def test_sync_intervention_details_accumulates_all_frs():
    partner = PartnerOrganization.objects.create(etl_id="1", name="P1", partner_type="Government", vendor_number="V1")
    agreement = Agreement.objects.create(etl_id="10", agreement_type="PCA")
    PCA.objects.create(etl_id="100", title="t", donors=["D1"])
    PCA.objects.create(etl_id="101", title="no donors", donors=[])
    responses.get(
        f"{BASE}/api/v2/interventions/100/",
        json={
            "partner_id": 1, "agreement": 10, "number": "PD1", "document_type": "PD", "status": "active",
            "title": "T", "start": "2024-01-01", "end": "2024-12-31",
            "frs_details": {"frs": [{"line_item_details": [{"donor": "A"}]}, {"line_item_details": [{"donor": "B"}]}]},
        },
    )  # fmt: skip
    run = sync.sync_intervention_details(make_run("intervention_details"), client=make_client())
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.rows_in == 1
    pca = PCA.objects.get(etl_id="100")
    assert pca.donors_set == [{"donor": "A"}, {"donor": "B"}]
    assert (pca.partner, pca.agreement, pca.end_date) == (partner, agreement, dt.date(2024, 12, 31))


@responses.activate
def test_sync_travels_paginates_from_page_one_and_uses_activity_date():
    Section.objects.create(id=5, name="CP")
    partner = PartnerOrganization.objects.create(etl_id="1", name="P1", partner_type="Government", vendor_number="V1")
    recent = (timezone.now().date() - dt.timedelta(days=10)).isoformat()
    responses.get(
        f"{BASE}/api/t2f/travels/?page_size=1000",
        json={"data": [
            {"id": 1, "reference_number": "T1", "traveler": "Ann", "purpose": "visit", "status": "COMPLETED",
             "start_date": recent, "end_date": None, "supervisor_name": "Bob", "section": 5, "office": 77},
        ]},
    )  # fmt: skip
    responses.get(
        f"{BASE}/api/t2f/travels/?page_size=1000&page=2",
        json={"data": [
            {"id": 2, "reference_number": "T2", "traveler": "Cy", "purpose": "", "status": "planned",
             "start_date": "2020-01-01", "end_date": "2020-01-02", "supervisor_name": "", "section": None, "office": None},
        ]},
    )  # fmt: skip
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000&page=3", status=404)
    responses.get(
        f"{BASE}/api/t2f/travels/1/",
        json={
            "international_travel": False, "ta_required": True, "itinerary": [{"a": 1}],
            "activities": [
                {"id": 50, "travel_type": "programmatic visit", "date": "2025-05-05", "is_primary_traveler": True,
                 "partner": 1, "partnership": None, "locations": []},
                {"id": 51, "travel_type": "meeting", "date": None, "partner": None, "partnership": None},
            ],
            "attachments": [{"name": "HACT report.docx"}, {"name": "other.pdf"}],
            "mode_of_travel": ["Car"], "estimated_travel_cost": "12.5", "completed_at": "2025-05-06T10:00:00Z",
            "canceled_at": None, "rejection_note": None, "cancellation_note": "", "certification_note": "",
            "report": "done", "additional_note": "", "misc_expenses": "", "first_submission_date": None,
        },
    )  # fmt: skip
    run = sync.sync_travels(make_run("travels"), client=make_client(), days=365)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["travels_listed"] == 2
    assert run.details["details_fetched"] == 1
    assert run.details["missing_refs"] == {"office": 1}
    t1 = Travel.objects.get(id=1)
    assert (t1.status, t1.end_date, t1.section_id, t1.office_id) == ("completed", None, 5, None)
    assert (t1.have_hact, t1.report_note, t1.travel_type, t1.itinerary_set) == (1, "done", "Programmatic Visit", ['{"a": 1}'])
    activity = TravelActivity.objects.get(id=50)
    assert (activity.date, activity.partner, activity.travel_id) == (dt.date(2025, 5, 5), partner, 1)
    assert not TravelActivity.objects.filter(id=51).exists()  # no partner/partnership -> skipped like v2
    assert Travel.objects.get(id=2).start_date == dt.date(2020, 1, 1)


@responses.activate
def test_sync_all_runs_every_entity_in_order_and_survives_failures():
    responses.get(f"{BASE}/api/v2/partners/", json=[partner_payload(1)])
    responses.get(f"{BASE}/api/v2/partners/1/", json=partner_payload(1, staff_members=[{"n": 1}]))
    responses.get(f"{BASE}/api/v2/agreements/", status=500)
    responses.get(f"{BASE}/api/v2/interventions/", json=[])
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000", json={"data": []})
    responses.get(f"{BASE}/api/audit/engagements/?page_size=1000", json={"results": [], "next": None})
    runs = sync.sync_all(client=make_client(), triggered_by="test")
    assert [r.target for r in runs] == list(sync.ENTITY_SYNCS)
    statuses = {r.target: r.status for r in runs}
    assert statuses["agreements"] == SyncRun.Status.FAILED
    assert statuses["partners"] == SyncRun.Status.SUCCEEDED
    assert statuses["action_points"] == SyncRun.Status.SUCCEEDED
    assert PartnerOrganization.objects.get(etl_id="1").staff_members == [{"n": 1}]
    assert SyncRun.objects.filter(job=SyncRun.Job.ETOOLS, triggered_by="test").count() == 8


def test_sync_all_rejects_unknown_entity():
    with pytest.raises(ValueError, match="unknown eTools entities: nope"):
        sync.sync_all(only=["nope"], client=make_client())


@responses.activate
def test_sync_locations_sets_type_parent_and_coordinates():
    responses.get(f"{BASE}/api/locations-types/", json=[{"id": 1, "name": "Governorate", "admin_level": 1},
                                                          {"id": 2, "name": "Cadastral", "admin_level": 3}])  # fmt: skip
    responses.get(
        f"{BASE}/api/locations/",
        json=[
            {"id": 100, "name": "Beirut", "p_code": "LB_GOV_1", "gateway": {"id": 1}, "parent": None,
             "geo_point": "POINT (35.5 33.9)"},
            {"id": 200, "name": "Ain", "p_code": "LB_CAS_10110", "gateway": {"id": 2}, "parent": 100,
             "geo_point": None},
            {"id": 300, "name": "Old", "p_code": "1-2-3", "gateway": {"id": 9}, "parent": 999, "geo_point": "bad"},
        ],
    )  # fmt: skip
    runs = locations.sync_all_locations(client=make_client(), triggered_by="test")
    assert [r.status for r in runs] == [SyncRun.Status.SUCCEEDED, SyncRun.Status.SUCCEEDED]
    assert (runs[0].rows_written, runs[1].rows_written, runs[1].details["parents_linked"]) == (2, 3, 1)
    assert LocationType.objects.get(id=2).admin_level == 3
    beirut = Location.objects.get(id=100)
    assert (beirut.type_id, beirut.longitude, beirut.latitude, beirut.cas_code) == (1, 35.5, 33.9, "")
    ain = Location.objects.get(id=200)
    assert (ain.type_id, ain.parent_id, ain.cas_code, ain.longitude) == (2, 100, "10110", None)
    old = Location.objects.get(id=300)
    assert (old.type_id, old.parent_id, old.cas_code) == (None, None, "1")
