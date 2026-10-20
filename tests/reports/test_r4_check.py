"""eTools reporting, stage R4 check (SPEC6 decision 4 and the funds views): a partner report's
``submitted_by`` holding an e-mail address is kept neither in its column nor in its record (migration
datamart 0013 empties both), Ask NeuroDB never reads an e-mail address, the eTools REST sync never
stores UNICEF focal points as ids or e-mail addresses, the report table never shows an id as a focal
point's name, a currency's partner share is set only against the budgets of the programme documents
that give both, and the By PD and grant tab says its disbursement is each programme document's
whole."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import responses
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, query, reporting
from neurodb.datamart import funds as fund_views
from neurodb.datamart import models as dm
from neurodb.integrations.etools import sync as rest_sync
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 9)


def test_ask_never_reads_an_email_address_under_another_key():
    """The records Ask NeuroDB reads lose the contact keys (``catalogue.scrub``) and the keys that hold a
    person (a partner report's ``submitted_by`` stored by an earlier sync, ``submitted_by_name``,
    ``people``), and, whatever the key, every e-mail address and link in a text."""
    record = {
        "submitted_by": "staff@example.org",
        "submitted_by_name": "Partner Staff",
        "people": ["Nadia Madeup", "focal.two@example.org"],
        "narrative": "Ask staff@example.org about the second session.",
        "nested": {"phone_number": "+961 1", "website": "https://example.org/x"},
        "count": 3,
    }
    found = query.public(record, dataset="partner_reports")
    assert found == {
        "narrative": "Ask [email withheld] about the second session.",
        "nested": {"website": "[link withheld]"},
        "count": 3,
    }
    # the records kept are cleaned by key only, as every reader of them (field monitoring too) expects
    assert catalogue.scrub(record)["narrative"] == record["narrative"]


def test_the_clean_up_also_empties_an_address_in_the_record_of_a_partner_report():
    from neurodb.datamart import backfill

    both = dm.ReportedIndicator.objects.create(
        datamart_id=1, submitted_by="staff@example.org", data={"submitted_by": "staff@example.org", "a": 1}
    )
    record_only = dm.ReportedIndicator.objects.create(
        datamart_id=2, submitted_by="", data={"submitted_by": "Staff (staff@example.org)"}
    )
    named = dm.ReportedIndicator.objects.create(
        datamart_id=3, submitted_by="Partner Staff", data={"submitted_by": "Partner Staff"}
    )
    listed = dm.ReportedIndicator.objects.create(datamart_id=4, data=["staff@example.org"])

    for _ in range(2):  # running it twice changes nothing more
        backfill.scrub_contact_details()
    for row in (both, record_only, named, listed):
        row.refresh_from_db()
    assert (both.submitted_by, both.data) == ("", {"submitted_by": "", "a": 1})
    assert (record_only.submitted_by, record_only.data) == ("", {"submitted_by": ""})
    assert (named.submitted_by, named.data) == ("Partner Staff", {"submitted_by": "Partner Staff"})
    assert listed.data == ["staff@example.org"]  # not a record: left as it is


# ------------------------------------------------------------------------------- UNICEF focal points
BASE = "https://etools.test"


def _rest_client() -> EToolsClient:
    return EToolsClient(BASE, "abc", session=make_session("Token abc", backoff=0))


@responses.activate
def test_the_rest_sync_never_stores_focal_points_as_ids_or_addresses():
    """The eTools REST list may give the UNICEF focal points as user ids: the names the Datamart sync
    wrote are kept. Given as records, they are kept by name, never by e-mail address."""
    PCA.objects.create(etl_id="7", number="LEB/PCA1", title="A", unicef_focal_points=["Nadia Madeup"])
    responses.get(
        f"{BASE}/api/v2/interventions/",
        json=[
            {"id": 7, "number": "LEB/PCA1", "title": "A", "unicef_focal_points": [101, 102]},
            {
                "id": 8,
                "number": "LEB/PCA2",
                "title": "B",
                "unicef_focal_points": [
                    {"id": 103, "first_name": "Karim", "last_name": "Invented", "email": "k@example.org"},
                    {"id": 104, "email": "only.an.address@example.org"},
                ],
            },
        ],
    )
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS, target="interventions", triggered_by="test")
    run = rest_sync.sync_interventions(run, client=_rest_client())
    assert run.status == SyncRun.Status.SUCCEEDED, run.details
    assert PCA.objects.get(etl_id="7").unicef_focal_points == ["Nadia Madeup"]
    assert PCA.objects.get(etl_id="8").unicef_focal_points == ["Karim Invented"]


def test_the_report_table_never_shows_an_id_or_a_record_as_a_focal_point():
    assert reporting.focal_point_names(
        [
            "101",
            "Nadia Madeup",
            '{"id": 103, "first_name": "Karim", "last_name": "Invented", "email": "k@example.org"}',
            '{"id": 104, "name": "Rana K"}',
            '{"id": 105}',
            "focal.two@example.org",
            "Nadia Madeup",
        ]
    ) == ["Nadia Madeup", "Karim Invented", "Rana K"]


# ------------------------------------------------------------------------------------------- funds
def test_a_currency_share_counts_only_the_programme_documents_with_a_budget():
    """A partner contribution whose programme document gives no total budget is never set against the
    budgets of the others: 61,260 of 644,148.9 is 9.5 %, whatever a PD without a budget contributes."""
    partner = PartnerOrganization.objects.create(etl_id="1", name="Alpha Association", vendor_number="V1")
    for etl_id, number, total, contribution in (
        ("1", "LEB/PD1", "644,148.90", "61260"),
        ("2", "LEB/PD2", "", "500000"),
    ):
        PCA.objects.create(
            etl_id=etl_id,
            partner=partner,
            number=number,
            title=number,
            status="active",
            total_budget=total,
            cso_contribution=contribution,
            budget_currency="USD",
        )
    found = fund_views.partner_contributions(fund_views.FundFilters(), TODAY)
    (usd,) = found["totals"]
    assert usd["partner_contribution"] == Decimal("561260")  # the sum keeps every contribution
    assert round(usd["partner_share"], 1) == Decimal("9.5")  # 61,260 / 644,148.9, never 561,260 / 644,148.9


def test_the_by_pd_and_grant_tab_says_its_disbursement_is_each_whole_programme_document(client_viewer):
    partner = PartnerOrganization.objects.create(etl_id="1", name="Alpha Association", vendor_number="V1")
    pd = PCA.objects.create(etl_id="1", partner=partner, number="LEB/PD1", title="A", status="active")
    for n, (fr, donor) in enumerate((("0400001", "Netherlands"), ("0400002", "EU")), start=1):
        dm.FundsReservation.objects.create(
            datamart_id=n, intervention=pd, fr_number=fr, line_item=1, donor=donor, grant_number=f"G{n}",
            overall_amount=Decimal("100"),
        )  # fmt: skip
        dm.FundsReservationHeader.objects.create(
            datamart_id=n, intervention=pd, fr_number=fr, total_amt=Decimal("100"), actual_amt=Decimal("40")
        )
    url = reverse("reports:funds")
    reservations = client_viewer.get(url, {"donor": "Netherlands"}).context["data"]["totals"]
    programmes = client_viewer.get(url, {"tab": "programmes", "donor": "Netherlands"})
    assert reservations["disbursed"] == Decimal("40")  # the FRs of the filter
    assert programmes.context["programmes"]["disbursed"] == Decimal("80")  # the whole PD
    assert "Disbursed to these PDs (USD)" in programmes.text
