"""User review of the eTools pages: one definition per figure, overdue flags, empty states, codes explained."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.core.models import PopulationFigure
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.partnerships.services import past_end_date

pytestmark = pytest.mark.django_db
TODAY = datetime.date.today()
DAY = datetime.timedelta(days=1)


@pytest.fixture
def partner(db):
    return PartnerOrganization.objects.create(
        etl_id="31", name="Partner B", partner_type="Civil Society Organization", total_ct_cy="1000"
    )


@pytest.fixture
def pd(partner):
    return PCA.objects.create(
        etl_id="41",
        partner=partner,
        partner_name="Partner B",
        number="LEB/PD9",
        title="Education",
        status="active",
        start=TODAY - 400 * DAY,
        end=TODAY - 10 * DAY,
        document_type="SPD",
    )


def _point(n, partner, pd, status, due):
    return dm.ActionPoint.objects.create(
        datamart_id=n,
        partner=partner,
        intervention=pd,
        reference_number=f"AP/{n}",
        status=status,
        due_date=due,
        description=f"Point {n}",
    )


def test_open_action_points_past_due_are_flagged_overdue_on_partner_and_programme(client_viewer, partner, pd):
    _point(1, partner, pd, "open", TODAY - DAY)
    _point(2, partner, pd, "open", TODAY + DAY)
    _point(3, partner, pd, "completed", TODAY - DAY)
    points = {p.reference_number: p.overdue for p in datamart.programme_datamart(pd)["action_points"]}
    assert points == {"AP/1": True, "AP/2": False, "AP/3": False}
    extra = datamart.partner_datamart(partner)
    assert extra["overdue_action_points"] == 1
    assert [p.overdue for p in extra["action_points"]] == [True, False]
    assert "Overdue" in client_viewer.get(reverse("reports:programme_detail", args=[pd.id])).text
    text = client_viewer.get(reverse("reports:partner_profile", args=[partner.id])).text
    assert "Overdue" in text and "1 overdue" in text


def test_partner_visits_card_counts_field_monitoring_and_planned_tpm_visits(client_viewer, partner):
    for n in (1, 2):  # two findings of one monitoring activity: one visit
        dm.MonitoringFinding.objects.create(
            datamart_id=n, partner=partner, monitoring_activity="FM-1", end_date=TODAY
        )
    for n, status in enumerate(("assigned", "draft", "cancelled", "unicef_approved"), start=1):
        dm.TPMVisit.objects.create(datamart_id=n, partner=partner, status=status, start_date=TODAY)
    chart = client_viewer.get(reverse("reports:partner_profile", args=[partner.id])).context["chart_data"]
    assert chart["visits_by_year"] == {
        "labels": [str(TODAY.year)],
        "series": {"UNICEF staff trips": [0], "Field monitoring": [1], "Third-party monitoring": [2]},
    }


def test_field_monitoring_kpi_counts_planned_tpm_visits_and_prints_the_location_once(client_viewer, partner):
    for n, status in enumerate(("assigned", "draft", "cancelled", "tpm_reported"), start=1):
        dm.TPMVisit.objects.create(datamart_id=n, partner=partner, status=status, start_date=TODAY)
    dm.MonitoringFinding.objects.create(
        datamart_id=1, partner=partner, location_name="Akkar West", site="Akkar West", end_date=TODAY
    )
    response = client_viewer.get(reverse("reports:monitoring"))
    assert (response.context["data"]["tpm_planned"], response.context["data"]["tpm_completed"]) == (2, 1)
    assert "Third-party visits planned" in response.text and "1 completed" in response.text
    assert response.text.count("Akkar West") == 1


def test_past_end_date_marks_running_programmes_only(client_viewer, pd):
    assert past_end_date("active", TODAY - DAY) and past_end_date("signed", TODAY - DAY)
    assert not past_end_date("ended", TODAY - DAY) and not past_end_date("active", TODAY + DAY)
    assert "past end date" in client_viewer.get(reverse("reports:programmes")).text
    assert "past end date" in client_viewer.get(reverse("reports:programme_detail", args=[pd.id])).text


def test_programmes_list_explains_codes_and_names_the_currency(client_viewer, pd):
    text = client_viewer.get(reverse("reports:programmes")).text
    assert "Budget (USD)" in text and "Simplified programme document" in text
    assert "Civil society organisation type" in text


def test_partners_list_and_profile_show_the_same_cash_transfers(client_viewer, partner):
    partner.total_ct_cp = "5000"
    partner.save()
    listing = client_viewer.get(reverse("reports:partners")).text
    profile = client_viewer.get(reverse("reports:partner_profile", args=[partner.id])).text
    assert "Cash transfers this year (USD)" in listing and "Cash transfers this year (USD)" in profile
    assert "1,000" in listing and "Cash transfers (CP)" not in listing
    assert "country programme (CP)" in profile and "5,000" in profile


def test_programme_detail_shows_one_donor_table_from_the_funds_lines(client_viewer, pd):
    pd.donors_set = [{"donor": "Donor X", "grant": "SC009", "value": 900}]
    pd.save()
    dm.FundsReservation.objects.create(
        datamart_id=1,
        intervention=pd,
        fr_number="0500001",
        line_item=1,
        donor="Donor X",
        grant_number="SC001",
        overall_amount=Decimal("700"),
    )
    text = client_viewer.get(reverse("reports:programme_detail", args=[pd.id])).text
    assert "Donors and grants" in text and "Reserved (USD)" in text
    assert "Donor contributions listed on the programme document" in text


def test_donors_page_totals_grants_by_donor_and_links_to_the_lists(client_viewer, pd):
    dm.Grant.objects.create(datamart_id=1, name="SC001", donor="Donor X")
    dm.Grant.objects.create(datamart_id=2, name="SC002", donor="Donor X")
    for n, (grant, amount) in enumerate((("SC001", 700), ("SC002", 300)), start=1):
        dm.FundsReservation.objects.create(
            datamart_id=n,
            intervention=pd,
            fr_number=f"05{n}",
            donor="Donor X",
            grant_number=grant,
            overall_amount=Decimal(amount),
        )
    rows = datamart.grants_by_donor([])
    assert [(r["donor"], r["reserved"], r["pds"], len(r["grants"])) for r in rows] == [
        ("Donor X", Decimal(1000), 1, 2)
    ]
    response = client_viewer.get(reverse("reports:donors"), {"donor": "Donor X"})
    assert response.context["funds_url"] == reverse("reports:funds") + "?donor=Donor+X"
    assert "Grants by donor" in response.text and "Records by governorate" not in response.text


def test_empty_assurance_page_does_not_ask_viewers_to_run_the_sync(client_viewer, client, admin_user):
    text = client_viewer.get(reverse("reports:assurance")).text
    assert "No audits or spot checks synced yet" in text
    assert "run the eTools Datamart sync" not in text.lower() and "by partner, —" not in text
    client.force_login(admin_user)
    assert "Run the eTools Datamart sync in the admin" in client.get(reverse("reports:assurance")).text


def test_sign_in_page_says_why_a_resource_page_needs_an_account(client):
    response = client.get(reverse("reports:population"), follow=True)
    assert "Sign in to see the population figures" in response.text
    assert "library of reports" in client.get(reverse("reports:library"), follow=True).text


def test_population_explains_the_column_codes(client_viewer):
    for code in ("LEB", "PRS"):
        PopulationFigure.objects.create(
            year=2026,
            category="total",
            level="governorate",
            area_name="Akkar",
            nationality=code,
            value=10,
        )
    response = client_viewer.get(reverse("reports:population"))
    assert response.context["column_codes"] == [
        ("LEB", "Lebanese"),
        ("PRS", "Palestinian refugees from Syria"),
    ]
