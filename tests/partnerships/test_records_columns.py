"""The "Records" columns and figures (ActivityInfo records reported against a programme document) are never shown
on the programmes, programme and donors pages, whatever the counts: ActivityInfo is an archive of past monthly
results (the owner's rule, November 2026). ``has_records`` still reads the counts for those who ask (Ask)."""

import pytest
from django.urls import reverse

from neurodb.facts.models import ActivityReportNew
from neurodb.partnerships import services
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

RECORDS_TIP = "ActivityInfo records reported against this programme document"


@pytest.fixture
def pd(db):
    partner = PartnerOrganization.objects.create(
        etl_id="31", name="Partner R", partner_type="Civil Society Organization", vendor_number="V31"
    )
    return PCA.objects.create(
        etl_id="41",
        partner=partner,
        partner_name="Partner R",
        number="LEB/PCA2026031/PD2026041",
        title="Protection services",
        status="active",
        donors=["Donor X"],
    )


@pytest.fixture
def recorded(database):
    def record(pd: PCA, n: int = 3) -> None:
        for _ in range(n):
            ActivityReportNew.objects.create(
                dbase=database, project_label=pd.number.split("-")[0], indicator_value=1
            )

    return record


def test_has_records_reads_counts_rows_or_one_count():
    assert not services.has_records({}) and not services.has_records({"LEB/PD1": 0})
    assert services.has_records({"LEB/PD1": 0, "LEB/PD2": 4})
    assert not services.has_records([{"interventions": 0}, {"interventions": None}])
    assert services.has_records([{"interventions": 0}, {"interventions": 2}])
    assert not services.has_records(0) and not services.has_records(None) and services.has_records(1)


def test_the_programmes_table_never_shows_records(client_viewer, pd, recorded):
    recorded(pd)
    page = client_viewer.get(reverse("reports:programmes")).content.decode()
    assert pd.number in page and RECORDS_TIP not in page and '<td class="num">3</td>' not in page


def test_the_programme_page_never_shows_activityinfo_records(client_viewer, pd, recorded):
    recorded(pd, 2)
    page = client_viewer.get(reverse("reports:programme_detail", args=[pd.pk])).content.decode()
    assert "ActivityInfo records" not in page


def test_the_donors_page_never_shows_the_records_tile(client_viewer, pd, recorded):
    recorded(pd)
    page = client_viewer.get(reverse("reports:donors")).content.decode()
    assert "ActivityInfo records" not in page and "reported against these programme documents" not in page
