"""The eTools Datamart pages and the Datamart sections on the programme, partner and donor pages."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def datamart(db):
    partner = PartnerOrganization.objects.create(
        etl_id="11", name="Partner A", partner_type="Civil Society Organization", vendor_number="V11"
    )
    pd = PCA.objects.create(
        etl_id="21",
        partner=partner,
        partner_name="Partner A",
        number="LEB/PD1",
        title="Protection",
        status="active",
    )
    today = datetime.date.today()
    dm.FundsReservation.objects.create(
        datamart_id=1,
        intervention=pd,
        fr_number="0400001",
        line_item=1,
        donor="Donor X",
        grant_number="SC001",
        overall_amount=Decimal("600"),
        total_amt=Decimal("1000"),
        actual_amt=Decimal("400"),
        outstanding_amt=Decimal("600"),
    )
    dm.FundsReservation.objects.create(
        datamart_id=2,
        intervention=pd,
        fr_number="0400001",
        line_item=2,
        donor="Donor X",
        grant_number="SC001",
        overall_amount=Decimal("400"),
        total_amt=Decimal("1000"),
        actual_amt=Decimal("400"),
        outstanding_amt=Decimal("600"),
    )
    dm.Grant.objects.create(
        datamart_id=1, name="SC001", donor="Donor X", expiry=today + datetime.timedelta(days=30)
    )
    for n, location in enumerate(("Akkar", "Beirut"), start=1):
        dm.PDIndicator.objects.create(
            datamart_id=n,
            source_id=77,
            intervention=pd,
            title="Children reached with services",
            target_numerator=Decimal("100"),
            display_type="number",
            location_name=location,
        )
    engagement = dm.AuditEngagement.objects.create(
        datamart_id=1,
        partner=partner,
        partner_name="Partner A",
        reference_number="LEB/2024/SC/01",
        engagement_type="sc",
        status="final",
        start_date=today,
        total_value=Decimal("25000"),
        financial_findings=Decimal("150"),
    )
    engagement.interventions.add(pd)
    dm.ActionPoint.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        reference_number="LEB/2024/1/APD",
        status="open",
        description="Submit the missing receipts",
        due_date=today - datetime.timedelta(days=3),
        high_priority=True,
        related_module="audit",
    )
    dm.PartnerAssessment.objects.create(datamart_id=1, partner=partner, type="Micro Assessment", rating="Low")
    dm.PSEAAssessment.objects.create(datamart_id=1, partner=partner, status="final", overall_rating=3)
    dm.MonitoringFinding.objects.create(
        datamart_id=1,
        partner=partner,
        entity="Partner A",
        entity_type="partner",
        overall_finding_rating="On Track",
        monitoring_activity="MA-1",
        end_date=today,
    )
    dm.TPMVisit.objects.create(
        datamart_id=1, partner=partner, reference_number="TPM/1", status="unicef_approved"
    )
    dm.HACTAggregate.objects.create(datamart_id=1, year=today.year, completed_spotcheck=12)
    return {"partner": partner, "pd": pd}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("reports:assurance", "LEB/2024/SC/01"),
        ("reports:monitoring", "MA-1"),
        ("reports:action_points", "Submit the missing receipts"),
    ],
)
def test_datamart_pages_render_with_their_partials(client_viewer, datamart, name, expected):
    response = client_viewer.get(reverse(name))
    assert response.status_code == 200
    assert expected in response.text
    partial = client_viewer.get(reverse(name), headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert expected in partial.text and "<html" not in partial.text


@pytest.mark.parametrize("name", ["reports:assurance", "reports:monitoring", "reports:action_points"])
def test_datamart_pages_render_empty(client_viewer, name):
    assert client_viewer.get(reverse(name)).status_code == 200


def test_datamart_pages_need_sign_in(client):
    response = client.get(reverse("reports:assurance"))
    assert response.status_code == 302


def test_filters(client_viewer, datamart):
    assert "LEB/2024/SC/01" not in client_viewer.get(reverse("reports:assurance"), {"type": "audit"}).text
    assert (
        "LEB/2024/SC/01" in client_viewer.get(reverse("reports:assurance"), {"type": "sc", "q": "V11"}).text
    )
    overdue = client_viewer.get(reverse("reports:action_points"), {"overdue": "1"}).text
    assert "LEB/2024/1/APD" in overdue
    assert (
        "LEB/2024/1/APD"
        not in client_viewer.get(reverse("reports:action_points"), {"status": "completed"}).text
    )
    assert "MA-1" not in client_viewer.get(reverse("reports:monitoring"), {"rating": "Off Track"}).text


def test_programme_detail_shows_funds_indicators_and_follow_up(client_viewer, datamart):
    text = client_viewer.get(reverse("reports:programme_detail", args=[datamart["pd"].id])).text
    assert "0400001" in text and "Funds reservations" in text
    # The FR total counts once although the FR has two lines.
    from neurodb.datamart.services import programme_datamart

    detail = programme_datamart(datamart["pd"])
    assert (detail["fr_count"], detail["fr_total"], detail["fr_outstanding"]) == (1, 1000, 600)
    assert "Children reached with services" in text and "Akkar, Beirut" in text
    assert "LEB/2024/SC/01" in text and "LEB/2024/1/APD" in text


def test_partner_profile_shows_datamart_assurance(client_viewer, datamart):
    text = client_viewer.get(reverse("reports:partner_profile", args=[datamart["partner"].id])).text
    for expected in (
        "LEB/2024/SC/01",
        "Micro Assessment",
        "PSEA assessment",
        "Submit the missing receipts",
        "MA-1",
        "TPM/1",
    ):
        assert expected in text


def test_donors_page_lists_grants_with_expiry(client_viewer, datamart):
    text = client_viewer.get(reverse("reports:donors")).text
    assert "SC001" in text and "Expires within 6 months" in text


def test_sidebar_links_the_new_pages(client_viewer):
    text = client_viewer.get(reverse("reports:partners")).text
    for name in ("reports:assurance", "reports:monitoring", "reports:action_points"):
        assert f'href="{reverse(name)}"' in text


def test_assistant_tools_read_the_datamart(datamart):
    overview = tools.run("assurance_overview", {"partner": "Partner A"})
    assert overview["engagements"] == 1
    assert overview["action_points"] == {
        "open": 1,
        "overdue": 1,
        "open_high_priority": 1,
        "by_module": {"audit": 1},
    }
    assert overview["field_monitoring"]["by_rating"] == {"On Track": 1}
    pd = tools.run("programme_details", {"number": "LEB/PD1"})
    assert pd["funds_reservations"] == {
        "count": 1,
        "reserved": 1000.0,
        "disbursed": 400.0,
        "outstanding": 600.0,
    }
    assert pd["indicators"][0]["locations"] == ["Akkar", "Beirut"]
    partner = tools.run("partner_details", {"partner_id": datamart["partner"].id})
    assert partner["open_action_points"] == 1 and partner["psea_assessment"]["overall_rating"] == 3
