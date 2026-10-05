"""The Datamart sync links each field monitoring finding to its programme document (``Links.fm_pd``),
so Ask's eTools query can filter field monitoring by programme document."""

import datetime

import pytest
import responses

from neurodb.assistant import tools
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.integrations.test_datamart_sync import page, run_all

pytestmark = pytest.mark.django_db


@pytest.fixture
def pds(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Civil Society Organization", vendor_number="V1"
    )
    old = PCA.objects.create(
        etl_id="11",
        partner=partner,
        number="LEB/PCA2023597/PD2025123",
        title="Education services",
        start=datetime.date(2025, 1, 1),
        end=datetime.date(2025, 12, 31),
    )
    amended = PCA.objects.create(
        etl_id="12",
        partner=partner,
        number="LEB/PCA2023597/PD2025123-2",
        title="Education services",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    return {"partner": partner, "old": old, "amended": amended}


def finding(n: int, entity: str, entity_type: str, end: str = "2026-05-12", **extra) -> dict:
    """An fm-ontrack record as the Datamart sends it."""
    return {
        "id": n,
        "source_id": 900 + n,
        "vendor_number": "V1",
        "entity": entity,
        "entity_type": entity_type,
        "overall_finding_rating": "On Track",
        "monitoring_activity": "FM-2026-022",
        "monitoring_activity_id": 1722,
        "monitoring_activity_end_date": end,
        "narrative_finding": "Activities observed as planned.",
        "visit_lead": "Rania Canary",
        **extra,
    }


@responses.activate
def test_link_monitoring_sets_the_programme_document_and_how_it_was_found(pds):
    page(
        "fm-ontrack",
        [
            finding(1, "LEBA/PCA2023597/PD2025123", "PD/SSFA"),  # visit in 2026: the amendment
            finding(2, "LEB/PCA2023597/PD2025123", "PD/SSFA"),  # written exactly like the first PD
            finding(3, "Partner 1", "Partner"),
            finding(4, "LEB/PCA9999999/PD9999999", "PD/SSFA"),  # no such programme document
        ],
    )
    (run,) = run_all("field_monitoring")
    assert run.status == SyncRun.Status.SUCCEEDED
    rows = {r.datamart_id: r for r in dm.MonitoringFinding.objects.all()}
    assert (rows[1].intervention, rows[1].pd_match) == (pds["amended"], "token")
    assert (rows[2].intervention, rows[2].pd_match) == (pds["old"], "exact")
    assert (rows[3].intervention, rows[3].pd_match) == (None, "")
    assert (rows[4].intervention, rows[4].pd_match) == (None, "")
    # only the PD row that matches nothing is counted missing (a partner row is not about a PD)
    assert run.details["not_linked"] == {"programme_document_fm": 1}


@responses.activate
def test_etools_query_filters_field_monitoring_by_programme_document(pds):
    page(
        "fm-ontrack",
        [finding(1, "LEBA/PCA2023597/PD2025123", "PD/SSFA"), finding(2, "Partner 1", "Partner")],
    )
    run_all("field_monitoring")
    out = tools.run("etools_query", {"dataset": "field_monitoring", "programme_document": "PD2025123-2"})
    assert out["matching_records"] == 1
    (row,) = out["rows"]
    assert row["record"] == "1" and row["entity"] == "LEBA/PCA2023597/PD2025123"
    assert row["neurodb_programme_document"]["number"] == "LEB/PCA2023597/PD2025123-2"
    assert "visit_lead" not in row and "Rania Canary" not in str(out)
    grouped = tools.run("etools_query", {"dataset": "field_monitoring", "group_by": "programme_document"})
    assert {g["programme_document"]: g["records"] for g in grouped["groups"]} == {
        "LEB/PCA2023597/PD2025123-2": 1,
        None: 1,
    }
