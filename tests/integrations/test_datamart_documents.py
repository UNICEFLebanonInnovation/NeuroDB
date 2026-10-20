import json

import pytest
import responses

from neurodb.core.models import DatamartSetting, SyncRun
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
    written = {
        "locations",
        "location_sites",
        "partners",
        "interventions",
        "intervention_budgets",
        "agreements",
        *sync.ENRICHMENTS,
    }
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


# ------------------------------------------------------------------- PRP's indicator reports: the business area
def prp(pk: int, area="2490", **extra) -> dict:
    """One record of PRP's indicator reports (``prp/indicator-report-v2``) as the sync tests assume it."""
    record = {
        "id": pk,
        "business_area": area,
        "pd_reference_number": "LEB/PD1",
        "performance_indicator": "Children reached",
        "time_period_end": "2026-03-31",
        "partner": "V1",
    }
    return {k: v for k, v in {**record, **extra}.items() if v is not ...}


def v2(results: list[dict], count: int | None = None, **match) -> None:
    payload = {"results": results, "next": None, **({"count": count} if count is not None else {})}
    kwargs = {"match": [responses.matchers.query_param_matcher(match)]} if match else {}
    responses.get(f"{API}/prp/indicator-report-v2/", json=payload, **kwargs)


@pytest.mark.parametrize("value", ["2490", 2490, " 2490 ", "2490.0", 2490.0, {"code": "2490"}])
@responses.activate
def test_a_business_area_code_is_compared_whatever_its_form(linked, settings, value):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    v2([prp(5, value)])
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED and result.details["documents"] == 1
    assert "other_country_skipped" not in result.details and "empty_response" not in result.details
    assert result.details["business_areas_seen"] == {"2490": 1}


@responses.activate
def test_a_read_naming_only_another_business_area_says_so_and_keeps_what_is_held(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="held")
    v2([prp(5, "2340"), prp(6, " 2340")], count=2)
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED and result.rows_in == 0
    details = result.details
    assert details["other_country_skipped"] == 2 and details["empty_response"] is True
    assert (details["read"], details["documents"], details["business_areas_seen"]) == (2, 0, {"2340": 2})
    assert list(DatamartDocument.objects.values_list("record_key", flat=True)) == ["held"]


@responses.activate
def test_a_record_naming_no_business_area_is_kept_unless_it_names_another_country(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    v2([prp(5, None), prp(6, "", country_name="Syria"), prp(7, ..., country_name=" lebanon ")])
    (result,) = run("prp_indicator_reports")
    # no record of the read named a code: whether the filter works is unconfirmed, so the run says so
    assert result.status == SyncRun.Status.PARTIAL and result.rows_failed == 0
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["5", "7"]
    details = result.details
    assert (details["other_country_skipped"], details["country_unnamed"]) == (1, 1)
    assert details["business_areas_seen"] == {"(none)": 3}
    v2([prp(5, None), prp(8)])  # one record names the code: the filter answered
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED and result.details["country_unnamed"] == 1


@responses.activate
def test_an_empty_prp_read_says_so_with_the_code_and_where_it_came_from(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="held")
    v2([], count=0, business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED and (result.rows_in, result.rows_written) == (0, 0)
    details = result.details
    assert details["empty_response"] is True and details["datamart_count"] == 0 and details["read"] == 0
    assert (details["business_area"], details["business_area_from"]) == ("2490", "environment")
    assert (
        details["documents"] == 0 and DatamartDocument.objects.count() == 1
    )  # an empty read removes nothing


@responses.activate
def test_a_prp_read_records_what_it_saw_as_counts_only(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    v2([prp(5), prp(6, time_period_end=...), prp(7, "2340"), prp(8, time_period_end="2019-12-31")], count=4)
    (result,) = run("prp_indicator_reports")
    details = result.details
    assert (details["read"], result.rows_in, details["datamart_count"]) == (4, 3, 4)
    assert details["business_areas_seen"] == {"2490": 3, "2340": 1}
    keys = details["keys_seen"]
    assert (keys["time_period_end"], keys["pd_reference_number"], keys["id"]) == (3, 4, 4)
    assert keys["source_id"] == 0 and keys["progress_value"] == 0 and keys["country_name"] == 0
    assert details["dated"] == {
        "2026": 1,
        "none": 1,
        "other": 1,
    }  # the records kept, by the year of their end
    assert DatamartDocument.objects.get(record_key="6").date is None
    # counts, key names and business area codes only: no value of a record
    stored = json.dumps(details)
    assert "Children reached" not in stored and "LEB/PD1" not in stored and "V1" not in stored


@responses.activate
def test_the_code_entered_in_the_admin_comes_after_the_environment_and_before_the_workspaces(settings):
    DatamartSetting.objects.create(pk=1, prp_business_area="2490")
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    # only the indicator reports answer: a request to the workspaces would fail the run
    v2([prp(5)], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == ("2490", "admin")
    assert [call.request.url.split("/api/latest/")[1].split("?")[0] for call in responses.calls] == [
        "prp/indicator-report-v2/"
    ]

    responses.reset()
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2491"  # the hosting team's code comes first
    v2([], business_area="2491", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == ("2491", "environment")


@responses.activate
def test_the_workspaces_give_the_code_of_the_first_workspace_of_that_name_that_has_one(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    page(
        "datamart/workspaces",
        [
            {"id": 1, "name": "Lebanon", "business_area_code": None},
            {"id": 2, "name": " LEBANON ", "business_area_code": 2490.0},
        ],
    )
    v2([], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == ("2490", "workspaces")


@responses.activate
def test_a_refused_prp_read_fails_with_its_http_status(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    responses.get(f"{API}/prp/indicator-report-v2/", status=403)
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.FAILED and "HTTP 403" in result.error


def test_the_missing_code_error_says_where_to_enter_it(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    scope = sync.CountryScope(type("Client", (), {"list": lambda self, *a, **k: iter(())})())
    with pytest.raises(sync.IntegrationError, match="Admin|admin") as raised:
        scope.business_area()
    assert "eTools Datamart: PRP business area code" in str(raised.value)


# --------------------------------------------------------------------------- the source id of a v2 record
@responses.activate
def test_a_v2_record_without_a_source_id_key_takes_its_id_and_no_other_dataset_does(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    v2([prp(5), prp(6, source_id=None), prp(7, source_id=70)])
    page("datamart/interventions-epd", [{"id": 1, "pd_number": "LEB/PD1", "country_name": "Lebanon"}])
    run("prp_indicator_reports,intervention_epd")
    by_key = dict(
        DatamartDocument.objects.filter(dataset="prp_indicator_reports").values_list(
            "record_key", "source_id"
        )
    )
    # a source_id key, even empty, is the record's own word; only a record without one takes its id
    assert by_key == {"5": 5, "6": None, "7": 70}
    assert DatamartDocument.objects.get(dataset="intervention_epd").source_id is None


# ------------------------------------------------------------- what the clean-ups of other datasets never touch
@responses.activate
def test_the_partner_reports_sync_never_removes_a_prp_record_and_an_empty_v2_read_keeps_them(
    linked, settings
):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    kept = DatamartDocument.objects.create(
        dataset="prp_indicator_reports", record_key="9", data={"country_name": "Syria"}
    )
    report = {
        "id": 1,
        "partner_vendor_number": "V1",
        "intervention_reference_number": "LEB/PD1",
        "report_type": "QPR",
        "reporting_period_start_date": "2026-01-01",
        "performance_indicator": "Children reached",
        "country_name": "Lebanon",
    }
    responses.get(
        __import__("re").compile(rf"{API}/prp/datareport/.*"), json={"results": [report], "next": None}
    )
    (reports,) = run("partner_reports")
    assert reports.status != SyncRun.Status.FAILED, reports.error
    assert DatamartDocument.objects.filter(pk=kept.pk).exists()  # the typed tables' clean-up only

    v2([])
    (result,) = run("prp_indicator_reports")
    assert result.details["documents"] == 0 and "documents_removed" not in result.details
    assert DatamartDocument.objects.filter(pk=kept.pk).exists()


@responses.activate
def test_a_document_read_shorter_than_announced_removes_nothing_and_says_so(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="old")
    v2([prp(5), prp(6)], count=4)
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.PARTIAL and result.rows_failed == 0
    details = result.details
    assert details["removal_skipped"] is True and (details["read"], details["datamart_count"]) == (2, 4)
    assert details["documents_removed"] == 0
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["5", "6", "old"]

    v2([prp(5), prp(6)], count=2)  # a whole read removes it
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED and result.details["documents_removed"] == 1


@responses.activate
def test_a_document_returned_twice_removes_nothing_and_says_so(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    for key in ("5", "6", "7"):
        DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key=key)
    url = f"{API}/prp/indicator-report-v2/"
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "500", "business_area": "2490"})],
        json={"count": 3, "next": f"{url}?page=2", "results": [prp(5), prp(6)]},
    )
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page": "2"})],
        json={"count": 3, "next": None, "results": [prp(6)]},
    )
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.PARTIAL and result.rows_failed == 0
    details = result.details
    assert details["removal_skipped"] is True and details["documents_removed"] == 0
    assert (details["read"], details["duplicates_read"], details["business_areas_seen"]) == (
        2,
        1,
        {"2490": 2},
    )
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["5", "6", "7"]


@responses.activate
def test_raw_copies_from_a_read_shorter_than_announced_are_not_removed():
    DatamartDocument.objects.create(dataset="partners", record_key="old")
    responses.get(
        f"{API}/datamart/partners/",
        json={"count": 3, "next": None, "results": [{"id": 9, "source_id": 1, "name": "Partner 1"}]},
    )
    (result,) = run("partners")
    assert result.status == SyncRun.Status.PARTIAL and result.rows_failed == 0
    assert result.details["removal_skipped"] is True and result.details["documents_removed"] == 0
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["9", "old"]


# ------------------------------------------------------------- PRP's progress reports: their programme document
@responses.activate
def test_prp_progress_reports_are_linked_to_their_pd_through_prp_programme_documents(linked, settings):
    """A PRP progress report names its PRP programme document by id; that record links to the eTools PD by
    its external id: the progress report gets the PD and its partner, after either dataset's sync. Counts
    only in the run's details."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    partner, pd = linked
    page(
        "sources/prp/unicefprogrammedocument",
        [
            {"id": 77, "external_business_area_code": "2490", "external_id": "11", "reference_number": "X"},
            {"id": 78, "external_business_area_code": "2490", "external_id": "999"},  # a PD NeuroDB lacks
        ],
        external_business_area_code="2490",
        page_size="500",
    )
    page(
        "sources/prp/unicefprogressreport",
        [
            {"id": 1, "business_area_code": "2490", "programme_document_id": 77, "status": "Ove"},
            {"id": 2, "business_area_code": "2490", "programme_document": {"id": "77"}, "status": "Due"},
            {"id": 3, "business_area_code": "2490", "programme_document_id": 78},
            {"id": 4, "business_area_code": "2490", "programme_document_id": 55},  # not held
            {"id": 5, "business_area_code": "2490", "report_type": "QPR"},  # names none
        ],
        business_area_code="2490",
        page_size="500",
    )
    documents, reports = run("prp_programme_documents,prp_progress_reports")
    assert reports.status == SyncRun.Status.SUCCEEDED
    linked_to = dict(
        DatamartDocument.objects.filter(dataset="prp_progress_reports").values_list(
            "record_key", "intervention"
        )
    )
    assert linked_to == {"1": pd.pk, "2": pd.pk, "3": None, "4": None, "5": None}
    assert set(
        DatamartDocument.objects.filter(dataset="prp_progress_reports", intervention=pd).values_list(
            "partner", flat=True
        )
    ) == {partner.pk}
    assert reports.details["progress_reports_pd_link"] == {
        "linked": 2,
        "pd_not_linked": 1,
        "pd_not_held": 1,
        "pd_not_named": 1,
    }
    assert documents.details["progress_reports_pd_link"] == {}  # no progress report held yet
    # the PRP programme document now links to another PD: the next sync of either dataset follows it
    other = PCA.objects.create(etl_id="999", number="LEB/PD9", title="PD 9", partner=partner)
    sync.link_progress_reports()
    assert DatamartDocument.objects.get(dataset="prp_progress_reports", record_key="3").intervention is None
    DatamartDocument.objects.filter(dataset="prp_programme_documents", record_key="78").update(
        intervention=other
    )
    assert sync.link_progress_reports()["linked"] == 3
    assert DatamartDocument.objects.get(dataset="prp_progress_reports", record_key="3").intervention == other


PROGRAMME_DOCUMENTS = "sources/prp/unicefprogrammedocument"
PROGRESS_REPORTS = "sources/prp/unicefprogressreport"


@responses.activate
def test_a_progress_report_read_that_fails_part_way_unlinks_none(linked, settings):
    """Two progress reports linked to the PD through PRP programme document 77; the next read returns report 1,
    then HTTP 500 on its second page: the run fails, and report 1 keeps its link."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    _, pd = linked
    page(PROGRAMME_DOCUMENTS, [{"id": 77, "external_business_area_code": "2490", "external_id": "11"}])
    reports = [{"id": n, "business_area_code": "2490", "programme_document_id": 77} for n in (1, 2)]
    page(PROGRESS_REPORTS, reports)
    run("prp_programme_documents,prp_progress_reports")
    responses.reset()
    url = f"{API}/{PROGRESS_REPORTS}/"
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "500", "business_area_code": "2490"})],
        json={"count": 2, "next": f"{url}?page=2", "results": reports[:1]},
    )
    responses.get(url, match=[responses.matchers.query_param_matcher({"page": "2"})], status=500)
    (result,) = run("prp_progress_reports")
    assert result.status == SyncRun.Status.FAILED and "HTTP 500" in result.error
    links = DatamartDocument.objects.filter(dataset="prp_progress_reports").values_list(
        "record_key", "intervention"
    )
    assert dict(links) == {"1": pd.pk, "2": pd.pk}


@responses.activate
def test_a_progress_report_naming_no_business_area_is_kept_only_through_a_held_programme_document(
    linked, settings
):
    """The progress reports endpoint may ignore business_area_code, and its records name neither a code nor a
    country: one naming a PRP programme document NeuroDB holds (read by business area) is kept, the others
    are left out as another country's, and counted apart."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    _, pd = linked
    page(PROGRAMME_DOCUMENTS, [{"id": 77, "external_business_area_code": "2490", "external_id": "11"}])
    page(
        PROGRESS_REPORTS, [{"id": n, "programme_document_id": pk} for n, pk in ((1, 77), (2, 901), (3, 902))]
    )
    documents, reports = run("prp_programme_documents,prp_progress_reports")
    assert list(
        DatamartDocument.objects.filter(dataset="prp_progress_reports").values_list(
            "record_key", "intervention"
        )
    ) == [("1", pd.pk)]
    details = reports.details
    assert (details["kept_by_programme_document"], details["unnamed_skipped"]) == (1, 2)
    assert details["other_country_skipped"] == 2 and "country_unnamed" not in details
    assert reports.status == SyncRun.Status.SUCCEEDED
