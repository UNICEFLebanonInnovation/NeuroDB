import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue
from neurodb.datamart.models import DatamartDocument
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
API = f"{BASE}/api/latest"


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def page(path: str, results: list[dict], **match) -> None:
    kwargs = {"match": [responses.matchers.query_param_matcher(match)]} if match else {}
    responses.get(f"{API}/{path}/", json={"results": results, "next": None}, **kwargs)


def run(only: str) -> list[SyncRun]:
    return sync.sync_all(only=only.split(","), triggered_by="test", client=client())


@pytest.fixture
def linked(db):
    p = PartnerOrganization.objects.create(
        etl_id="1", name="Partner 1", partner_type="Government", vendor_number="V1"
    )
    pd = PCA.objects.create(etl_id="11", number="LEB/PD1", title="PD", partner=p)
    return p, pd


def test_every_catalogue_dataset_has_a_sync_or_a_table():
    for name, spec in catalogue.DOCUMENTS.items():
        assert spec.scope == "written_by" or name in sync.ENTITY_SYNCS
    written = {"partners", "interventions", "intervention_budgets", "agreements", *sync.ENRICHMENTS}
    assert {n for n, s in catalogue.DOCUMENTS.items() if s.scope == "written_by"} == written
    assert set(catalogue.TYPED) <= set(sync.ENTITY_SYNCS)


@responses.activate
def test_raw_copies_of_partners_and_programme_documents_are_linked_and_scrubbed():
    page(
        "datamart/partners",
        [
            {
                "id": 9,
                "source_id": 1,
                "name": "Partner 1",
                "vendor_number": "V1",
                "email": "office@partner.example",
                "phone_number": "+961 1",
                "rating": "Low",
            }
        ],
    )
    page(
        "datamart/interventions",
        [
            {
                "id": 70,
                "source_id": 11,
                "number": "LEB/PD1",
                "title": "PD",
                "partner_source_id": 1,
                "start_date": "2024-01-01",
                "partner_focal_points_data": [{"name": "A", "email": "a@x.example"}],
            }
        ],
    )
    runs = run("partners,interventions")
    assert [r.details["documents"] for r in runs] == [1, 1]
    partner_doc = DatamartDocument.objects.get(dataset="partners")
    assert partner_doc.partner.etl_id == "1" and partner_doc.title == "Partner 1"
    assert "email" not in partner_doc.data and "phone_number" not in partner_doc.data
    pd_doc = DatamartDocument.objects.get(dataset="interventions")
    assert pd_doc.intervention.etl_id == "11" and pd_doc.partner.etl_id == "1"
    assert str(pd_doc.date) == "2024-01-01"
    assert pd_doc.data["partner_focal_points_data"] == [{"name": "A"}]


@responses.activate
def test_country_datasets_link_by_reference_number_and_replace_their_rows(linked):
    p, pd = linked
    DatamartDocument.objects.create(dataset="intervention_epd", record_key="old")
    page(
        "datamart/interventions-epd",
        [
            {
                "id": 1,
                "pd_number": "LEB/PD1",
                "pd_title": "PD",
                "partner_vendor_number": "V1",
                "context": "Context",
                "country_name": "Lebanon",
            },
            {
                "id": 2,
                "pd_number": "SYR/PD9",
                "country_name": "Syria",
            },  # an ignored filter must not let it in
        ],
        country_name="Lebanon",
        page_size="500",
    )
    (result,) = run("intervention_epd")
    assert result.status == SyncRun.Status.SUCCEEDED
    assert result.details["other_country_skipped"] == 1 and result.details["documents_removed"] == 1
    doc = DatamartDocument.objects.get(dataset="intervention_epd")
    assert (doc.intervention, doc.partner, doc.title) == (pd, p, "LEB/PD1 · PD")


@responses.activate
def test_records_without_an_id_get_a_stable_key(linked):
    rows = [{"pd_sffa_reference_number": "LEB/PD1", "label": "Children reached", "target_numerator": "100"}]
    page("datamart/reports/indicators", rows)
    run("cp_indicators")
    first = DatamartDocument.objects.get(dataset="cp_indicators")
    run("cp_indicators")
    assert DatamartDocument.objects.get(dataset="cp_indicators").pk == first.pk
    assert first.intervention == linked[1] and len(first.record_key) == 64


@responses.activate
def test_prp_datasets_use_the_business_area_found_from_the_workspaces(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    page(
        "datamart/workspaces",
        [
            {"id": 1, "name": "Syria", "business_area_code": "2340"},
            {"id": 2, "name": "Lebanon", "business_area_code": "2490"},
        ],
        page_size="500",
    )
    page(
        "prp/indicator-report-v2",
        [
            {
                "id": 5,
                "business_area": "2490",
                "pd_reference_number": "LEB/PD1",
                "performance_indicator": "Children",
                "time_period_end": "2024-03-31",
                "partner": "V1",
            },
            {"id": 6, "business_area": "2340", "pd_reference_number": "SYR/PD9"},
        ],
        business_area="2490",
        page_size="500",
    )
    page(
        "sources/prp/unicefprogressreport",
        [
            {
                "business_area_code": "2490",
                "report_number": 1,
                "report_type": "QPR",
                "overall_satisfaction": "Satisfied",
            },
        ],
        business_area_code="2490",
        page_size="500",
    )
    runs = run("prp_indicator_reports,prp_progress_reports,workspaces")
    assert [r.status for r in runs] == [SyncRun.Status.SUCCEEDED] * 3
    report = DatamartDocument.objects.get(dataset="prp_indicator_reports")
    assert report.intervention == linked[1] and report.partner == linked[0]
    assert runs[0].details["other_country_skipped"] == 1
    assert (
        DatamartDocument.objects.get(dataset="prp_progress_reports").data["overall_satisfaction"]
        == "Satisfied"
    )
    assert list(DatamartDocument.objects.filter(dataset="workspaces").values_list("title", flat=True)) == [
        "Lebanon · 2490"
    ]


@responses.activate
def test_a_missing_business_area_fails_that_dataset_clearly(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    page("datamart/workspaces", [{"id": 1, "name": "Syria", "business_area_code": "2340"}])
    page(
        "system/monitor",
        [{"table_name": "interventions", "status": "SUCCESS", "last_success": "2024-05-01T02:00:00Z"}],
    )
    reports, monitor = run("prp_indicator_reports,datamart_etl_status")
    assert reports.status == SyncRun.Status.FAILED and "ETOOLS_DATAMART_BUSINESS_AREA" in reports.error
    assert monitor.status == SyncRun.Status.SUCCEEDED
    assert str(DatamartDocument.objects.get(dataset="datamart_etl_status").date) == "2024-05-01"


def test_scrub_removes_contact_details_at_any_depth():
    assert sync.scrub(
        {"a": 1, "Email_Address": "x", "nested": [{"phone": 1, "name": "n"}], "t": "a\x00b"}
    ) == {"a": 1, "nested": [{"name": "n"}], "t": "ab"}


@responses.activate
def test_a_full_run_reads_every_dataset_once(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    seen = []

    def reply(request):
        seen.append(request.url.split("/api/latest/")[1].split("?")[0].rstrip("/"))
        return 200, {}, '{"results": [], "next": null}'

    responses.add_callback(responses.GET, __import__("re").compile(rf"{API}/.*"), callback=reply)
    runs = sync.sync_all(triggered_by="test", client=client())
    assert {r.status for r in runs} == {SyncRun.Status.SUCCEEDED}
    assert len(runs) == len(sync.ENTITY_SYNCS)
    assert len(seen) == len(set(seen)) == len(sync.ENTITY_SYNCS)
    assert "prp/indicator-report-v2" in seen and "datamart/audit/financial-findings-all" in seen
