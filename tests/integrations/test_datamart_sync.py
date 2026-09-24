from decimal import Decimal

import pytest
import responses
from django.core.management import CommandError, call_command

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.geo.models import Location
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, Agreement, PartnerOrganization

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
API = f"{BASE}/api/latest/datamart"


def make_client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def page(dataset: str, results: list[dict]) -> None:
    responses.get(f"{API}/{dataset}/", json={"count": len(results), "next": None, "results": results})


def partner(source_id: int, **extra) -> dict:
    return {
        "id": 900 + source_id,
        "source_id": source_id,
        "name": f"Partner {source_id}",
        "short_name": f"P{source_id}",
        "partner_type": "Civil Society Organization",
        "cso_type": "National",
        "vendor_number": f"V{source_id}",
        "rating": "Low",
        "hidden": False,
        "deleted_flag": False,
        "total_ct_cy": "1500.00",
        "hact_values": {"programmatic_visits": {"completed": {"total": 2}}},
        **extra,
    }


def intervention(source_id: int, **extra) -> dict:
    return {
        "id": 700 + source_id,
        "source_id": source_id,
        "intervention_id": source_id,
        "number": f"LEB/PCA2023{source_id}/PD2024{source_id}",
        "title": f"Programme {source_id}",
        "document_type": "PD",
        "status": "active",
        "partner_name": "Partner 1",
        "partner_source_id": 1,
        "partner_vendor_number": "V1",
        "agreement_id": 55,
        "agreement_reference_number": "LEB/PCA2023123",
        "start_date": "2024-01-01",
        "end_date": "2025-06-30",
        "planned_programmatic_visits": 4,
        "sections_data": [{"name": "Education", "source_id": 3}],
        "offices_data": [{"name": "Beirut"}, {"name": "Zahle"}],
        "unicef_focal_points_data": [{"first_name": "Rana", "last_name": "K", "email": "r@example.org"}],
        "cp_outputs_data": [{"name": "Output 1"}],
        "locations_data": [{"name": "Akkar", "pcode": "LB-AK"}, {"name": "Unknown", "pcode": "LB-XX"}],
        "donors": ["European Union"],
        "grants": ["SC220001"],
        **extra,
    }


def run_all(only: str) -> list[SyncRun]:
    return sync.sync_all(only=only.split(","), triggered_by="test", client=make_client())


@responses.activate
def test_partners_and_interventions_feed_the_etools_tables():
    Location.objects.create(id=10, name="Akkar", p_code="LB-AK", lft=1, rght=2, level=0, tree_id=10)
    page("partners", [partner(1), partner(2, name="Second")])
    page("interventions", [intervention(11)])
    page(
        "interventions-budget",
        [
            {
                "id": 1,
                "source_id": 11,
                "budget_total": "1000.50",
                "budget_unicef_cash": "800",
                "budget_unicef_supply": "100",
                "budget_cso_contribution": "100.50",
                "budget_currency": "USD",
            }
        ],
    )
    page(
        "partners/agreements",
        [
            {
                "reference_number": "LEB/PCA2023123",
                "agreement_type": "PCA",
                "start": "2023-01-01",
                "end": "2025-12-31",
            }
        ],
    )
    runs = run_all("partners,interventions,intervention_budgets,agreements")
    assert [r.status for r in runs] == [SyncRun.Status.SUCCEEDED] * 4
    assert all(r.job == SyncRun.Job.ETOOLS_DATAMART for r in runs)

    p1 = PartnerOrganization.objects.get(etl_id="1")
    assert (p1.name, p1.vendor_number, p1.total_ct_cy) == ("Partner 1", "V1", "1500.00")
    pd = PCA.objects.get(etl_id="11")
    assert pd.partner == p1
    assert pd.number == "LEB/PCA202311/PD202411"
    assert (str(pd.start), str(pd.end), pd.planned_visits) == ("2024-01-01", "2025-06-30", 4)
    assert pd.section_names == ["Education"]
    assert pd.offices_set == ["Beirut", "Zahle"] and pd.offices_names == "Beirut, Zahle"
    assert pd.unicef_focal_points == ["Rana K"]
    assert pd.donors == ["European Union"] and pd.grants == ["SC220001"]
    assert pd.location_p_codes == ["LB-AK", "LB-XX"]
    assert list(pd.locations.values_list("p_code", flat=True)) == ["LB-AK"]
    assert (pd.total_budget, pd.unicef_cash, pd.total_unicef_budget, pd.budget_currency) == (
        "1000.50",
        "800",
        "900",
        "USD",
    )
    agreement = Agreement.objects.get(etl_id="55")
    assert (agreement.agreement_number, agreement.agreement_type, str(agreement.end)) == (
        "LEB/PCA2023123",
        "PCA",
        "2025-12-31",
    )
    assert pd.agreement == agreement


@responses.activate
def test_a_bad_record_is_counted_and_the_rest_are_written():
    page("partners", [partner(1), {"id": 5, "name": "no source id"}, partner(3)])
    (run,) = run_all("partners")
    assert run.status == SyncRun.Status.PARTIAL
    assert (run.rows_in, run.rows_written, run.rows_failed) == (3, 2, 1)
    assert set(PartnerOrganization.objects.values_list("etl_id", flat=True)) == {"1", "3"}


@responses.activate
def test_funds_reservations_link_to_the_pd_and_rebuild_its_donors():
    p = PartnerOrganization.objects.create(etl_id="1", name="Partner 1", partner_type="Government")
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    line = {
        "fr_number": "0400001",
        "donor": "European Union",
        "donor_code": "I49901",
        "grant_number": "SC220001",
        "total_amt": "1000",
        "actual_amt": "400",
        "outstanding_amt": "600",
        "source_intervention_id": 11,
        "pd_reference_number": "LEB/PD1",
        "start_date": "2024-01-01",
    }
    page(
        "funds-reservation",
        [
            {"id": 1, "line_item": 1, "overall_amount": "600", **line},
            {"id": 2, "line_item": 2, "overall_amount": "400", **line},
            {
                "id": 3,
                "line_item": 1,
                "overall_amount": "50",
                **line,
                "source_intervention_id": 999,
                "pd_reference_number": "UNKNOWN",
            },
        ],
    )
    (run,) = run_all("funds_reservations")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["not_linked"] == {"programme_document": 1}
    assert dm.FundsReservation.objects.filter(intervention=pd).count() == 2
    pd.refresh_from_db()
    assert pd.donors_set == [
        {"donor": "European Union", "donor_code": "I49901", "grant_number": "SC220001", "value": 1000.0}
    ]

    # The next full read replaces the table: a line no longer returned is removed.
    responses.replace(
        responses.GET,
        f"{API}/funds-reservation/",
        json={"results": [{"id": 1, "line_item": 1, "overall_amount": "600", **line}], "next": None},
    )
    (run,) = run_all("funds_reservations")
    assert run.details["removed"] == 2
    assert list(dm.FundsReservation.objects.values_list("datamart_id", flat=True)) == [1]
    pd.refresh_from_db()
    assert pd.donors_set[0]["value"] == 600.0

    # A PD whose last FR line disappears loses its FR donors.
    responses.replace(
        responses.GET,
        f"{API}/funds-reservation/",
        json={
            "results": [
                {
                    "id": 9,
                    "line_item": 1,
                    "overall_amount": "5",
                    **line,
                    "source_intervention_id": 999,
                    "pd_reference_number": "X",
                }
            ],
            "next": None,
        },
    )
    run_all("funds_reservations")
    pd.refresh_from_db()
    assert pd.donors_set == []


@responses.activate
def test_an_empty_answer_never_empties_a_table():
    dm.Grant.objects.create(datamart_id=1, name="SC1", donor="EU")
    page("funds/grants", [])
    (run,) = run_all("grants")
    assert run.details == {"removed": 0, "empty_response": True}
    assert dm.Grant.objects.count() == 1


@responses.activate
def test_engagements_link_partner_by_vendor_and_pds_by_number():
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    page(
        "audit/engagements",
        [
            {
                "id": 40,
                "partner_name": "Partner 1",
                "partner_code": "V1",
                "partner": {"name": "Partner 1"},
                "engagement_type": "sc",
                "status": "final",
                "reference_number": "LEB/2024/SC/01",
                "start_date": "2024-02-01",
                "total_value": "25000",
                "spotcheck_total_amount_tested": "12000",
                "financial_findings": "150.5",
                "active_pd_data": [{"number": "LEB/PD1"}],
            }
        ],
    )
    (run,) = run_all("engagements")
    assert run.status == SyncRun.Status.SUCCEEDED
    engagement = dm.AuditEngagement.objects.get(datamart_id=40)
    assert engagement.partner == p and engagement.vendor_number == "V1"
    assert engagement.amount_tested == Decimal("12000") and engagement.financial_findings == Decimal("150.5")
    assert list(engagement.interventions.all()) == [pd]
    assert engagement.data["reference_number"] == "LEB/2024/SC/01"


@responses.activate
def test_action_points_indicators_and_monitoring_link_to_existing_rows():
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    page(
        "actionpoints",
        [
            {
                "id": 1,
                "source_id": 501,
                "reference_number": "LEB/2024/1/APD",
                "status": "open",
                "description": "Fix it",
                "due_date": "2024-01-31",
                "partner_source_id": 1,
                "intervention_source_id": 11,
                "related_module": "audit",
                "high_priority": True,
            }
        ],
    )
    page(
        "pd-indicators",
        [
            {
                "id": 1,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Akkar",
                "target_numerator": "100",
                "display_type": "number",
            },
            {
                "id": 2,
                "source_id": 77,
                "title": "Children reached",
                "pd_reference_number": "LEB/PD1",
                "location_name": "Beirut",
                "target_numerator": "100",
                "display_type": "number",
            },
        ],
    )
    page(
        "fm-ontrack",
        [
            {
                "id": 1,
                "source_id": 9,
                "vendor_number": "V1",
                "entity": "Partner 1",
                "entity_type": "partner",
                "overall_finding_rating": "On Track",
                "monitoring_activity": "MA-1",
                "location": {"name": "Akkar"},
                "monitoring_activity_end_date": "2024-05-01",
            }
        ],
    )
    page(
        "tpm-visits",
        [
            {
                "id": 1,
                "source_id": 3,
                "source_partner_id": 1,
                "visit_reference_number": "TPM/1",
                "visit_status": "unicef_approved",
                "visit_start_date": "2024-04-01",
            }
        ],
    )
    page(
        "hact/aggregate", [{"id": 1, "year": 2024, "completed_spotcheck": 12, "microassessments_total": None}]
    )
    runs = run_all("action_points,pd_indicators,field_monitoring,tpm_visits,hact")
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}

    point = dm.ActionPoint.objects.get()
    assert (point.partner, point.intervention, point.high_priority) == (p, pd, True)
    assert dm.PDIndicator.objects.filter(intervention=pd).count() == 2
    finding = dm.MonitoringFinding.objects.get()
    assert (finding.partner, finding.location_name) == (p, "Akkar")
    assert dm.TPMVisit.objects.get().partner == p
    hact = dm.HACTAggregate.objects.get()
    assert (hact.year, hact.completed_spotcheck, hact.microassessments_total) == (2024, 12, 0)


@responses.activate
def test_a_failed_dataset_does_not_stop_the_next_one():
    responses.get(f"{API}/funds/grants/", status=500)
    page("hact/aggregate", [{"id": 1, "year": 2024}])
    grants, hact = run_all("grants,hact")
    assert grants.status == SyncRun.Status.FAILED and "HTTP 500" in grants.error
    assert hact.status == SyncRun.Status.SUCCEEDED


def test_unknown_dataset_is_rejected():
    with pytest.raises(ValueError, match="unknown eTools Datamart datasets"):
        sync.sync_all(only=["nope"], client=make_client())


def test_command_without_credentials_fails_clearly(settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = "", ""
    with pytest.raises(CommandError, match="ETOOLS_USERNAME and ETOOLS_PASSWORD"):
        call_command("sync_etools_datamart", "--only", "grants")
    assert not SyncRun.objects.exists()


def test_names_accepts_the_shapes_the_datamart_uses():
    assert sync.names([{"name": "A"}, {"name": "A"}, "B", None]) == ["A", "B"]
    assert sync.names("A, B") == ["A", "B"]
    assert sync.names({"name": "A"}) == ["A"]
    assert sync.names([{"pcode": "LB-1", "name": "x"}], "pcode", "p_code") == ["LB-1"]
    assert sync.names(None) == []
