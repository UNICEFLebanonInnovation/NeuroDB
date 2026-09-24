"""Funds, partner reporting, engagement detail and the extra programme/partner/assurance sections."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db
TODAY = datetime.date.today()


@pytest.fixture
def data(db):
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
    dm.FundsReservationHeader.objects.create(
        datamart_id=1,
        intervention=pd,
        fr_number="0400001",
        total_amt=Decimal("1000"),
        actual_amt=Decimal("400"),
        outstanding_amt=Decimal("600"),
        start_date=TODAY,
    )
    dm.FundsReservation.objects.create(
        datamart_id=1,
        intervention=pd,
        fr_number="0400001",
        line_item=1,
        donor="Donor X",
        grant_number="SC001",
        overall_amount=Decimal("1000"),
        start_date=TODAY,
    )
    dm.Grant.objects.create(
        datamart_id=1, name="SC001", donor="Donor X", expiry=TODAY - datetime.timedelta(days=1)
    )
    engagement = dm.AuditEngagement.objects.create(
        datamart_id=1,
        partner=partner,
        partner_name="Partner A",
        reference_number="LEB/2024/AUD/01",
        engagement_type="audit",
        status="final",
        risk_rating="High",
        high_priority_findings=2,
        data={"action_points": [{"id": 501}]},
    )
    dm.AuditFinding.objects.create(
        datamart_id=1,
        engagement=engagement,
        partner=partner,
        reference_number="LEB/2024/AUD/01",
        finding_number=1,
        title="Ineligible expenditure",
        amount=Decimal("250"),
        created=TODAY,
    )
    dm.ActionPoint.objects.create(
        datamart_id=1,
        source_id=501,
        reference_number="AP/501",
        status="open",
        description="Recover the ineligible amount",
    )
    for n, location in enumerate(("Akkar", "Beirut"), start=1):
        dm.ReportedIndicator.objects.create(
            datamart_id=n,
            partner=partner,
            intervention=pd,
            partner_name="Partner A",
            pd_reference_number="LEB/PD1",
            progress_report="PR-9",
            report_number="QPR2",
            report_type="QPR",
            report_status="Accepted",
            period_start=TODAY - datetime.timedelta(days=90),
            period_end=TODAY - datetime.timedelta(days=5),
            due_date=TODAY - datetime.timedelta(days=1),
            submission_date=TODAY,
            indicator="Children reached",
            target="100",
            total_cumulative_progress="60",
            total_cumulative_progress_in_location=str(20 * n),
            location=location,
        )
    dm.ReportedIndicator.objects.create(
        datamart_id=3,
        partner=partner,
        intervention=pd,
        pd_reference_number="LEB/PD1",
        progress_report="PR-10",
        report_number="QPR3",
        report_status="Due",
        period_end=TODAY - datetime.timedelta(days=40),
        due_date=TODAY - datetime.timedelta(days=10),
        indicator="Children reached",
    )
    dm.TPMActivity.objects.create(
        datamart_id=1,
        partner=partner,
        intervention=pd,
        task_reference_number="TPM/1/1",
        tpm_name="Monitor SARL",
        date=TODAY,
        locations="Akkar",
        status="completed",
    )
    dm.ProgrammaticVisit.objects.create(
        datamart_id=1, partner=partner, intervention=pd, travel_type="Programmatic Visit", date=TODAY
    )
    dm.PlannedVisits.objects.create(datamart_id=1, intervention=pd, year=TODAY.year, q1=1, q2=1)
    dm.PDActivity.objects.create(
        datamart_id=1,
        intervention=pd,
        result="Output 1",
        code="1.1",
        name="Train teachers",
        unicef_cash=Decimal("5000"),
        cso_cash=Decimal("500"),
    )
    dm.PDActivity.objects.create(
        datamart_id=2,
        intervention=pd,
        result="Output 1",
        code="1.1",
        name="Train teachers",
        unicef_cash=Decimal("5000"),
        cso_cash=Decimal("500"),
    )  # a second budget line
    dm.PartnerHACTYear.objects.create(
        datamart_id=1,
        partner=partner,
        partner_name="Partner A",
        year=TODAY.year,
        risk_rating="Moderate",
        cash_transfers=Decimal("90000"),
        pv_required=2,
        pv_completed=1,
        sc_required=1,
        sc_completed=1,
    )
    return {"partner": partner, "pd": pd, "engagement": engagement}


@pytest.mark.parametrize(
    ("name", "expected"),
    [("reports:funds", "0400001"), ("reports:partner_reporting", "QPR2")],
)
def test_new_pages_and_partials(client_viewer, data, name, expected):
    page = client_viewer.get(reverse(name))
    assert page.status_code == 200 and expected in page.text
    partial = client_viewer.get(reverse(name), headers={"HX-Request": "true"})
    assert expected in partial.text and "<html" not in partial.text


@pytest.mark.parametrize("name", ["reports:funds", "reports:partner_reporting"])
def test_new_pages_render_empty(client_viewer, name):
    assert client_viewer.get(reverse(name)).status_code == 200


def test_funds_filters_and_expired_grant(client_viewer, data):
    text = client_viewer.get(reverse("reports:funds")).text
    assert "Expired" in text and "SC001" in text
    assert "0400001" not in client_viewer.get(reverse("reports:funds"), {"donor": "Nobody"}).text
    assert "0400001" in client_viewer.get(reverse("reports:funds"), {"donor": "Donor X", "q": "V11"}).text


def test_partner_reporting_counts_and_overdue_filter(client_viewer, data):
    from neurodb.datamart.services import partner_reporting

    summary = partner_reporting({})["summary"]
    assert summary == {"reports": 2, "submitted": 1, "late": 1, "overdue": 1, "accepted": 1}
    text = client_viewer.get(reverse("reports:partner_reporting"), {"overdue": "1"}).text
    assert "QPR3" in text and "QPR2" not in text


def test_progress_report_quick_view_and_page(client_viewer, data):
    url = reverse("reports:progress_report") + "?report=PR-9"
    modal = client_viewer.get(url, headers={"HX-Request": "true"}).text
    assert "modal-header" in modal and "Akkar" in modal and "Beirut" in modal
    assert "<html" in client_viewer.get(url).text
    assert client_viewer.get(reverse("reports:progress_report") + "?report=nope").status_code == 404


def test_engagement_detail_shows_findings_and_action_points(client_viewer, data):
    text = client_viewer.get(reverse("reports:engagement_detail", args=[data["engagement"].id])).text
    assert "Ineligible expenditure" in text and "AP/501" in text and "2 high-priority" in text


def test_assurance_shows_hact_compliance_and_findings(client_viewer, data):
    text = client_viewer.get(reverse("reports:assurance")).text
    assert "HACT assurance by partner" in text and "1 / 2" in text
    assert "1 partner behind its minimum requirements" in text
    assert "Ineligible expenditure" in text
    assert reverse("reports:engagement_detail", args=[data["engagement"].id]) in text


def test_programme_page_shows_funds_workplan_reporting_and_visits(client_viewer, data):
    text = client_viewer.get(reverse("reports:programme_detail", args=[data["pd"].id])).text
    for expected in (
        "disbursed",
        "Train teachers",
        "Partner reporting",
        "QPR2",
        "Programmatic visits",
        "TPM/1/1",
    ):
        assert expected in text
    from neurodb.datamart.services import programme_datamart, workplan

    detail = programme_datamart(data["pd"])
    assert (detail["fr_count"], detail["fr_actual"]) == (1, Decimal("400"))
    assert detail["visits"] == [{"year": TODAY.year, "planned": 2, "staff": 1, "tpm": 1}]
    assert len(workplan(data["pd"].workplan_activities.all())[0]["activities"]) == 1
    assert [p["progress"] for p in detail["latest_progress"]] == ["60"]


def test_partner_page_shows_hact_years_reporting_and_findings(client_viewer, data):
    response = client_viewer.get(reverse("reports:partner_profile", args=[data["partner"].id]))
    text = response.text
    assert "HACT by year" in text and "Partner reporting" in text and "Ineligible expenditure" in text
    assert response.context["chart_data"]["visits_by_year"] == [(TODAY.year, 1)]


def test_monitoring_page_lists_tpm_activities(client_viewer, data):
    text = client_viewer.get(reverse("reports:monitoring")).text
    assert "TPM/1/1" in text and "Programmatic Visit 1" in text


def test_sidebar_links_funds_and_reporting(client_viewer):
    text = client_viewer.get(reverse("reports:partners")).text
    assert (
        f'href="{reverse("reports:funds")}"' in text
        and f'href="{reverse("reports:partner_reporting")}"' in text
    )


def test_assistant_tools(data):
    funds = tools.run("funds_overview", {"donor": "donor x"})
    assert funds["matched_donors"] == ["Donor X"] and funds["disbursed_usd"] == 400.0
    assert funds["grants"][0]["expired"] is True
    with pytest.raises(tools.ToolInputError):
        tools.run("funds_overview", {"donor": "Nobody"})
    reporting = tools.run("partner_reporting", {"partner": "Partner A", "overdue_only": True})
    assert reporting["overdue"] == 1 and reporting["progress_reports"][0]["report"] == "QPR3"
    pd = tools.run("programme_details", {"number": "LEB/PD1"})
    assert pd["workplan_outputs"][0]["unicef_cash"] == 5000.0 and pd["progress_reports"]
    partner = tools.run("partner_details", {"partner_id": data["partner"].id})
    assert partner["hact_by_year"][0]["programmatic_visits_done_required"] == [1, 2]
    assert partner["partner_reporting"]["late"] == 1
