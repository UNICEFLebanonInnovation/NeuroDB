"""Programme, partner and assurance pages: the same figure under the same label on every page."""

import datetime

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.facts.models import ActivityReportNew
from neurodb.facts.services import partners as partner_facts
from neurodb.partnerships import services as partnerships
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def partner(db):
    partner = PartnerOrganization.objects.create(
        etl_id="31",
        name="Himaya",
        partner_type="Civil Society Organization",
        cso_type="National",
        vendor_number="2500003",
    )
    for n, status in enumerate(("active", "signed", "signed", "expired", "draft"), start=1):
        PCA.objects.create(
            etl_id=f"4{n}",
            partner=partner,
            partner_name="Himaya",
            number=f"LEB/PD{n}",
            title=f"PD {n}",
            status=status,
            document_type="PD",
            total_budget="1000",
        )
    return partner


def test_programme_summary_all_counts_every_non_draft_pd(client_viewer, partner):
    assert (
        partnerships.pd_summary("all")["count"] == partnerships.programme_documents(scope="all").count() == 4
    )
    assert partnerships.pd_summary("all")["budget_total"] == 4000
    assert partnerships.pd_summary("active")["count"] == 1
    text = client_viewer.get(reverse("reports:programme_summary"), {"scope": "all"}).text
    assert "All signed" not in text


def test_partner_list_all_documents_matches_the_profile(partner):
    row = partnerships.partners({}).get(pk=partner.pk)
    assert row.pd_count == len(partnerships.partner_profile(partner)["programme_documents"]) == 4
    assert row.active_pd_count == partnerships.partner_profile(partner)["active_count"] == 1


def test_partner_without_pds_counts_zero(partner):
    other = PartnerOrganization.objects.create(
        etl_id="32", name="Other", partner_type="Civil Society Organization"
    )
    assert partnerships.partners({}).get(pk=other.pk).pd_count == 0


def test_activityinfo_tile_is_the_linked_records_total(client_viewer, partner, monkeypatch):
    fake = {
        "labels": ["Himaya AI"],
        "databases": [],
        "records": 126,
        "years": [],
        "by_year": [],
        "link_run": None,
    }
    monkeypatch.setattr(partner_facts, "partner_activityinfo", lambda p: fake)
    text = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk])).text
    assert '<div class="kpi__label">ActivityInfo records</div>\n  <div class="kpi__value">126</div>' in text
    assert "126 records" in text
    assert tools.run("partner_details", {"partner_id": partner.pk})["activity_reports"] == 126


def test_open_action_points_tile_links_to_the_partner_s_action_points(client_viewer, partner):
    for n in (1, 2):
        dm.ActionPoint.objects.create(
            datamart_id=n, partner=partner, reference_number=f"AP/{n}", status="open"
        )
    text = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk])).text
    url = reverse("reports:action_points") + "?q=2500003"
    assert f'<a href="{url}">2</a>' in text
    assert f'<a href="{url}">All action points of this partner</a>' in text


def test_monitoring_sentence_does_not_call_signed_pds_active(client_viewer, partner):
    pd = PCA.objects.get(number="LEB/PD2")  # signed
    dm.PDIndicator.objects.create(datamart_id=1, source_id=5, intervention=pd, title="Children reached")
    text = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk])).text
    assert "indicator of the running (signed, active or suspended) programme documents" in text
    assert "of the active programme documents" not in text


def test_filter_options_list_each_value_once(partner):
    PartnerOrganization.objects.create(
        etl_id="33", name="Amel", partner_type="Civil Society Organization", cso_type="National"
    )
    today = datetime.date.today()
    for n in range(3):
        dm.ActionPoint.objects.create(
            datamart_id=n, status="open", related_module="audit", due_date=today + datetime.timedelta(days=n)
        )
        dm.FundsReservation.objects.create(
            datamart_id=n, fr_number=f"04{n}", line_item=n, donor="CERF", grant_number="SC1"
        )
        dm.ReportedIndicator.objects.create(
            datamart_id=n, report_status="Submitted", report_type="QPR", pd_reference_number=f"PD{n}"
        )
        dm.AuditEngagement.objects.create(
            datamart_id=n, status="final", start_date=today - datetime.timedelta(days=n)
        )
        dm.MonitoringFinding.objects.create(
            datamart_id=n, overall_finding_rating="On Track", end_date=today - datetime.timedelta(days=n)
        )
    options = partnerships.pd_filter_options()
    assert options["statuses"] == ["active", "expired", "signed"]
    assert options["document_types"] == ["PD"]
    assert options["cso_types"] == ["National"]
    assert partnerships.partner_filter_options()["cso_types"] == ["National"]
    assert datamart.action_points({})["options"]["statuses"] == ["open"]
    assert datamart.action_points({})["options"]["modules"] == ["audit"]
    assert datamart.funds({})["options"]["donors"] == ["CERF"]
    assert datamart.funds({})["options"]["grants"] == ["SC1"]
    assert datamart.partner_reporting({})["options"]["statuses"] == ["Submitted"]
    assert datamart.partner_reporting({})["options"]["types"] == ["QPR"]
    assert datamart.assurance({})["options"]["statuses"] == ["final"]
    assert datamart.monitoring({})["options"]["ratings"] == ["On Track"]


@pytest.mark.parametrize(("count", "expected"), [(3, "3 financial findings"), (None, "1 financial findings")])
def test_engagement_detail_financial_findings_count(client_viewer, partner, count, expected):
    engagement = dm.AuditEngagement.objects.create(
        datamart_id=1, partner=partner, reference_number="LEB/2026/AUD/1", financial_findings_count=count
    )
    dm.AuditFinding.objects.create(datamart_id=1, engagement=engagement, title="Unsupported costs")
    text = client_viewer.get(reverse("reports:engagement_detail", args=[engagement.pk])).text
    assert expected in text


def test_engagement_detail_lists_action_points_linked_by_the_foreign_key(partner):
    engagement = dm.AuditEngagement.objects.create(datamart_id=1, partner=partner, reference_number="")
    dm.ActionPoint.objects.create(
        datamart_id=1, engagement=engagement, reference_number="AP/1", status="open"
    )
    dm.ActionPoint.objects.create(datamart_id=2, reference_number="AP/2", status="open")
    points = datamart.engagement_detail(engagement)["action_points"]
    assert [p.reference_number for p in points] == ["AP/1"]


def test_deleted_partner_profile_opens_with_a_notice(client_viewer, partner):
    partner.deleted_flag = True
    partner.save()
    response = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk]))
    assert response.status_code == 200
    assert "marked as deleted in eTools" in response.text


def test_pd_detail_indicator_heading_matches_the_listed_rows(client_viewer, partner):
    pd = PCA.objects.create(etl_id="49", partner=partner, number="LEB/PD9", title="Old", status="cancelled")
    dm.PDIndicator.objects.create(datamart_id=1, source_id=7, intervention=pd, title="Children reached")
    text = client_viewer.get(reverse("reports:programme_detail", args=[pd.pk])).text
    assert 'Indicators <span class="text-muted small">1</span>' in text
    assert "<td>Children reached" in text


def test_pd_without_number_has_no_activityinfo_records(partner, database):
    ActivityReportNew.objects.create(dbase=database, database_ai_id=str(database.ai_id), project_label="")
    pd = PCA.objects.create(etl_id="50", partner=partner, number="", title="No number", status="signed")
    assert partnerships.pd_detail(pd)["interventions"] == 0


def test_assurance_chips_count_every_engagement(client_viewer, partner):
    today = datetime.date.today()
    dm.AuditEngagement.objects.bulk_create(
        dm.AuditEngagement(
            datamart_id=n,
            partner=partner,
            engagement_type="sc",
            start_date=today - datetime.timedelta(days=n),
        )
        for n in range(52)
    )
    data = datamart.partner_datamart(partner)
    assert len(data["engagements"]) == 50
    assert data["engagement_counts"] == {"Spot check": 52} and data["engagements_total"] == 52
    text = client_viewer.get(reverse("reports:partner_profile", args=[partner.pk])).text
    assert "Spot check · 52" in text and "52 rows" in text and "The 50 most recent are listed." in text


def test_paginated_lists_have_a_unique_order():
    assert partnerships.programme_documents().query.order_by[-1] == "id"
    assert partnerships.partners({}).query.order_by[-1] == "id"
