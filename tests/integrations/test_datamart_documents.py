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
from tests.integrations.fake_datamart import FakeDatamart
from tests.integrations.fake_datamart import v2 as fake_v2

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
        page_size="500",  # read with no business area filter: none is proven (the first read of the week)
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
def test_the_code_entered_in_the_admin_comes_before_the_environment(settings):
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
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2491"  # the admin's code comes first, even over a code
    v2([prp(6)], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == ("2490", "admin")
    assert "business_area_set_aside" not in result.details and "business_area_found" not in result.details


@responses.activate
def test_the_workspaces_give_the_code_every_workspace_of_that_name_names(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    page(
        "datamart/workspaces",
        [
            {"id": 1, "name": "Lebanon", "business_area_code": None},
            {"id": 2, "name": " LEBANON ", "business_area_code": 2490.0},
            {"id": 3, "name": "Syria", "business_area_code": "2340"},
        ],
    )
    v2([prp(5)], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == ("2490", "workspaces")
    assert result.details["business_area_found"] is True


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
    assert "the workspace it holds, the Datamart's workspaces or the partner report rows" in str(raised.value)


# ---------------------------------------------------------- a business area that is not a code is never sent
def _paths() -> list[str]:
    return [call.request.url.split("/api/latest/")[1].split("?")[0] for call in responses.calls]


def _held_workspace(code="2490", name="Lebanon", key="1"):
    DatamartDocument.objects.create(
        dataset="workspaces",
        record_key=key,
        title=f"{name} · {code}",
        data={"name": name, "business_area_code": code},
    )


@responses.activate
def test_a_hosting_name_is_never_sent_and_the_held_workspace_gives_the_code(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    _held_workspace()
    fake = FakeDatamart([fake_v2(5, "2490"), fake_v2(6, "2340")])
    fake.register(f"{API}/prp/indicator-report-v2/")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    # the one PRP request carries the code, never the name, and no request goes to the workspaces
    assert _paths() == ["prp/indicator-report-v2/"] and fake.requests == [
        {"page_size": "500", "business_area": "2490"}
    ]
    details = result.details
    assert (details["business_area"], details["business_area_from"]) == ("2490", "workspace_held")
    assert details["business_area_set_aside"] == {"environment": "Lebanon"} and details["business_area_found"]
    assert DatamartDocument.objects.filter(dataset="prp_indicator_reports").count() == 1


@responses.activate
def test_with_nothing_held_the_live_workspaces_then_the_partner_rows_give_the_code(settings):
    from neurodb.datamart.models import ReportedIndicator

    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    page("datamart/workspaces", [{"id": 1, "name": "Lebanon", "business_area_code": "2490"}])
    v2([prp(5)], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert _paths() == ["datamart/workspaces/", "prp/indicator-report-v2/"]
    assert result.details["business_area_from"] == "workspaces"

    # the workspaces name none: the code most partner report rows carry, its forms merged
    responses.reset()
    page("datamart/workspaces", [{"id": 1, "name": "Syria", "business_area_code": "2340"}])
    v2([prp(6)], business_area="2490", page_size="500")
    for pk, area in ((1, "2490"), (2, " 2490.0"), (3, {"code": "2490"}), (4, "2340"), (5, None)):
        ReportedIndicator.objects.create(datamart_id=pk, data={"business_area": area})
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    details = result.details
    assert (details["business_area"], details["business_area_from"]) == ("2490", "partner_reports")
    assert details["business_area_rows"] == [3, 5]

    # a code on half the rows or fewer, or a name, is never taken: the run fails, saying what was set aside
    ReportedIndicator.objects.filter(datamart_id=2).update(data={"business_area": "2340"})
    ReportedIndicator.objects.filter(datamart_id=3).update(data={"business_area": "Lebanon"})
    assert sync.reported_business_area() == ("", 0, 5)  # 2340, the most frequent, on 2 of 5
    ReportedIndicator.objects.filter(datamart_id__in=[1, 2]).update(data={"business_area": "Lebanon"})
    assert sync.reported_business_area() == ("", 0, 5)  # "Lebanon" on 3 of 5: a name, not a code
    responses.reset()
    page("datamart/workspaces", [])
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.FAILED
    assert _paths() == ["datamart/workspaces/"]  # nothing asked of PRP's indicator reports


@responses.activate
def test_no_code_anywhere_fails_naming_the_hosting_name(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    page("datamart/workspaces", [{"id": 1, "name": "Syria", "business_area_code": "2340"}])
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.FAILED and _paths() == ["datamart/workspaces/"]
    assert result.error == (
        "IntegrationError: enter the business area code in Admin → Data and sync → eTools Datamart: PRP business "
        "area code: the hosting team's ETOOLS_DATAMART_BUSINESS_AREA is “Lebanon”, a name, not a business area "
        "code (a code is digits, as 2490), and NeuroDB found none in the workspace it holds, the Datamart's "
        "workspaces or the partner report rows"
    )
    # what to do comes first: the pages that cut the error short (checks 8, 9 and 11 at 140 characters) keep it
    assert "Admin → Data and sync → eTools Datamart: PRP business area code" in result.error[:137]


@responses.activate
def test_a_workspaces_request_that_fails_goes_on_to_the_partner_rows(settings):
    from neurodb.datamart.models import ReportedIndicator

    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    responses.get(f"{API}/datamart/workspaces/", status=403)
    ReportedIndicator.objects.create(datamart_id=1, data={"business_area": "2490"})
    v2([prp(5)], business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert result.details["business_area_from"] == "partner_reports"


@pytest.mark.parametrize("source", ["workspace_held", "workspaces", "partner_reports"])
@responses.activate
def test_a_found_code_that_reads_nothing_removes_nothing_and_ends_partial(settings, source):
    from neurodb.datamart.models import ReportedIndicator

    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    for key in ("7", "8"):
        DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key=key)
    if source == "workspace_held":
        _held_workspace()
    page(
        "datamart/workspaces",
        [{"id": 1, "name": "Lebanon", "business_area_code": "2490"}] if source == "workspaces" else [],
    )
    if source == "partner_reports":
        ReportedIndicator.objects.create(datamart_id=1, data={"business_area": "2490"})
    v2([], count=0, business_area="2490", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.PARTIAL and result.rows_failed == 0
    details = result.details
    assert (details["business_area"], details["business_area_from"]) == ("2490", source)
    assert details["empty_response"] is True and details["business_area_found"] is True
    assert sorted(
        DatamartDocument.objects.filter(dataset="prp_indicator_reports").values_list("record_key", flat=True)
    ) == ["7", "8"]


@responses.activate
def test_a_record_naming_its_business_area_by_name_is_kept_as_the_country_s(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    # the Datamart takes the code, but its records name their business area by name
    fake = FakeDatamart(
        [fake_v2(5, "Lebanon"), fake_v2(6, " lebanon "), fake_v2(7, "Syria"), fake_v2(8, "2490")],
        ignore=("business_area",),
    )
    fake.register(f"{API}/prp/indicator-report-v2/")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["5", "6", "8"]
    assert result.details["other_country_skipped"] == 1 and "country_unnamed" not in result.details


@responses.activate
def test_a_record_with_the_code_but_another_country_is_dropped(linked, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    fake = FakeDatamart(
        [
            fake_v2(5, "2490"),
            fake_v2(6, "2490", country_name="Syria"),
            fake_v2(7, "2490", country_name=" LEBANON"),
        ]
    )
    fake.register(f"{API}/prp/indicator-report-v2/")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert sorted(DatamartDocument.objects.values_list("record_key", flat=True)) == ["5", "7"]
    assert result.details["other_country_skipped"] == 1


@responses.activate
def test_two_held_workspaces_of_the_country_with_different_codes_give_none(settings):
    from neurodb.datamart.models import ReportedIndicator

    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    _held_workspace("2490", key="1")
    _held_workspace("2491", key="2")
    assert sync.held_business_area("Lebanon") == ""
    # the live workspaces disagree too: the chain goes on to the partner rows
    page(
        "datamart/workspaces",
        [
            {"id": 1, "name": "Lebanon", "business_area_code": "2490"},
            {"id": 2, "name": "Lebanon", "business_area_code": "2491"},
        ],
    )
    ReportedIndicator.objects.create(datamart_id=1, data={"business_area": "2491"})
    v2([prp(5, "2491")], business_area="2491", page_size="500")
    (result,) = run("prp_indicator_reports")
    assert result.status == SyncRun.Status.SUCCEEDED, result.error
    assert (result.details["business_area"], result.details["business_area_from"]) == (
        "2491",
        "partner_reports",
    )


def test_a_value_that_is_not_a_code_is_never_sent(settings):
    """The guard of the request: resolve_business_area gives codes only, and a code that is not one never
    leaves NeuroDB."""
    assert [sync.is_area_code(v) for v in ("2490", 2490, " 2490 ", 2490.0, "2490.0", {"code": "2490"})] == [
        True
    ] * 6
    assert [sync.is_area_code(v) for v in ("Lebanon", "LB", "2490; 2340", "", None, "٢٤٩٠")] == [False] * 6
    scope = sync.CountryScope(object())
    scope.choice = sync.AreaCode("Lebanon", "environment")
    with pytest.raises(sync.IntegrationError, match="not a business area code: it is never sent"):
        scope.request(catalogue.DOCUMENTS["prp_indicator_reports"])


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
    its external id: the progress report gets the PD and its partner, after either dataset's sync. The records
    carry ids, so the key each programme document is held under (its id) is the pair the data proves: today's
    lookup. Key names and counts only in the run's details."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    partner, pd = linked
    year = {"start_date": "2026-01-01", "end_date": "2026-12-31"}
    page(
        "sources/prp/unicefprogrammedocument",
        [
            {
                "id": 77,
                "external_business_area_code": "2490",
                "external_id": "11",
                "reference_number": "X",
                **year,
            },
            {
                "id": 78,
                "external_business_area_code": "2490",
                "external_id": "999",
                **year,
            },  # a PD NeuroDB lacks
        ],
        external_business_area_code="2490",
        page_size="500",
    )
    ends = {"end_date": "2026-03-31"}
    page(
        "sources/prp/unicefprogressreport",
        [
            {"id": 1, "business_area_code": "2490", "programme_document_id": 77, "status": "Ove", **ends},
            {
                "id": 2,
                "business_area_code": "2490",
                "programme_document": {"id": "77"},
                "status": "Due",
                **ends,
            },
            {"id": 3, "business_area_code": "2490", "programme_document_id": 78, **ends},
            {"id": 4, "business_area_code": "2490", "programme_document_id": 55},  # not held
            {"id": 5, "business_area_code": "2490", "report_type": "QPR"},  # names none
        ],
        page_size="500",  # read with no business area filter: none is proven (the first read of the week)
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
    link = reports.details["progress_reports_pd_link"]
    assert {k: link[k] for k in ("linked", "pd_not_linked", "pd_not_held", "pd_not_named")} == {
        "linked": 2,
        "pd_not_linked": 1,
        "pd_not_held": 1,
        "pd_not_named": 1,
    }
    # "named" (both keys) links more reports than either key alone, which agree with it: today's lookup, proven
    assert (link["by"], link["proven"]) == (["named", "record_key"], True)
    assert link["pairs"][0] == {
        "report": "named", "pd": "record_key", "linked": 3, "dated": 3, "agree": 3, "verdict": "proven"
    }  # fmt: skip
    assert link["pd_keys"] == {"record_key": 2, "external_id": 2, "id": 2, "reference_number": 1}
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
    # the count with no business area filter is not answered: no filter is proven, which never fails the run
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "500"})],
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


# --------------------------------------------------- PRP's records with no id: the key pair and the record identity
def _url(path: str) -> str:
    return f"{API}/{path}/"


def _pd(prp_id, area="2490", **extra) -> dict:
    """A PRP programme document as sources/prp/unicefprogrammedocument may send it: no id, its PRP id under
    prp_id, the eTools id under external_id (eTools ids repeat across countries), the dates of 2026."""
    found = {"prp_id": prp_id, "external_business_area_code": area, "start_date": "2026-01-01",
             "end_date": "2026-12-31", **extra}  # fmt: skip
    return {k: v for k, v in found.items() if v is not ...}


def _report(pd_id, number: str, end: str = "2026-03-31", **extra) -> dict:
    """A PRP progress report as sources/prp/unicefprogressreport sends it (21 Oct 2026): no id, no source_id."""
    return {"business_area_code": "2490", "programme_document": pd_id, "report_type": "QPR",
            "report_number": number, "end_date": end, **extra}  # fmt: skip


def _held(dataset: str) -> dict[str, int | None]:
    return dict(DatamartDocument.objects.filter(dataset=dataset).values_list("record_key", "intervention"))


@responses.activate
def test_progress_reports_with_no_id_link_through_a_key_the_data_proves(linked, settings):
    """Neither dataset carries an id (21 Oct 2026: every progress report named a programme document NeuroDB did
    not hold, as the documents are held under a hash). The reports name their document's PRP id: the pair
    programme_document = prp_id links three of the four reports naming one, a decoy naming an eTools id that
    equals an external_id links one, and the dates agree: the pair is proven, the decoy left unlinked."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    partner, pd = linked
    documents = [
        _pd(77, external_id="11", reference_number="LEB/PD1"),
        _pd(78, external_id="999", reference_number="LEB/PD78"),  # a PD NeuroDB does not hold
        _pd(500, "0990", external_id="11", reference_number="SYR/PD1"),  # another country's: its eTools id 11
    ]
    FakeDatamart(documents, ignore=("external_business_area_code",)).register(
        _url("sources/prp/unicefprogrammedocument")
    )
    reports = [
        _report(77, "QPR1"),
        _report(77, "QPR2", "2026-06-30"),
        _report(78, "QPR1"),
        _report(11, "QPR3"),
    ]
    FakeDatamart(reports).register(_url("sources/prp/unicefprogressreport"))
    pds, progress = run("prp_programme_documents,prp_progress_reports")
    assert progress.status == SyncRun.Status.SUCCEEDED, progress.details
    # external_id repeats across the countries read: the documents are held under their PRP id
    assert pds.details["record_identity"] == "prp_id"
    assert sorted(_held("prp_programme_documents")) == ["prp_id:77", "prp_id:78"]
    link = progress.details["progress_reports_pd_link"]
    assert (link["by"], link["proven"]) == (["programme_document", "prp_id"], True)
    assert (link["linked"], link["pd_not_linked"], link["pd_not_held"]) == (2, 1, 1)
    assert {(p["report"], p["pd"], p["linked"]) for p in link["pairs"]} >= {
        ("programme_document", "prp_id", 3),
        ("programme_document", "external_id", 1),
    }
    held = DatamartDocument.objects.filter(dataset="prp_progress_reports").values_list(
        "data", "intervention", "partner"
    )
    assert {(d["report_number"], d["programme_document"]): (pk, p) for d, pk, p in held} == {
        ("QPR1", 77): (pd.pk, partner.pk),
        ("QPR2", 77): (pd.pk, partner.pk),
        ("QPR1", 78): (None, None),  # its document is linked to no PD NeuroDB holds
        ("QPR3", 11): (None, None),  # the decoy: no PRP programme document has id 11
    }
    # key names and counts only
    assert "LEB/PD1" not in json.dumps(link) and "QPR" not in json.dumps(link)


@responses.activate
def test_a_copy_an_earlier_read_kept_never_stops_the_link(linked, settings):
    """A programme document held under its hash changes on a night its read is not whole (its new version comes
    twice): the new version gets a row of its own (prp_id:77) and the old one stays, as nothing is removed. Both
    carry prp_id 77, external_id 11 and the same reference number, but the old row was written by an earlier
    read: the latest read's row stands for each value, and the progress reports naming 77 and 78 stay linked
    through prp_id."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    partner, pd = linked
    pd2 = PCA.objects.create(etl_id="12", number="LEB/PD2", title="PD 2", partner=partner)
    old, same = (
        _pd(77, external_id="11", reference_number="LEB/PD1", title="Old"),
        _pd(78, external_id="12", reference_number="LEB/PD2"),
    )
    for record in (old, same):
        DatamartDocument.objects.create(dataset="prp_programme_documents", record_key=sync.record_key(record),
                                        data=record)  # fmt: skip
    new = {**old, "title": "New"}
    other = _pd(500, "0990", external_id="11", reference_number="SYR/PD1")
    FakeDatamart([new, new, same, other], ignore=("external_business_area_code",)).register(
        _url("sources/prp/unicefprogrammedocument")
    )
    reports = [_report(77, "QPR1"), _report(77, "QPR2", "2026-06-30"), _report(78, "QPR1")]
    FakeDatamart(reports).register(_url("sources/prp/unicefprogressreport"))
    documents, progress = run("prp_programme_documents,prp_progress_reports")
    assert documents.status == SyncRun.Status.PARTIAL and documents.details["removal_skipped"]
    held = DatamartDocument.objects.filter(dataset="prp_programme_documents")
    assert sorted(t or "" for t in held.values_list("data__title", flat=True)) == ["", "New", "Old"]  # kept
    link = progress.details["progress_reports_pd_link"]
    assert (link["by"], link["proven"], link["linked"]) == (["programme_document", "prp_id"], True, 3)
    assert "not_unique" not in link
    assert sorted(_held("prp_progress_reports").values()) == sorted([pd.pk, pd.pk, pd2.pk])


def test_two_documents_one_read_wrote_sharing_a_value_leave_that_key_out():
    """Two rows sharing prp_id 77, written by the same read, are two documents (or one changed during that read):
    prp_id is left out. A row an earlier read wrote gives way to the latest read's."""
    pds = [("h1", {"prp_id": 77}, 1), ("h2", {"prp_id": 77}, 2), ("h3", {"prp_id": 78}, 3)]
    reports = [{"programme_document": n} for n in (77, 77, 78)]
    one_read = sync.prove_pd_link(pds, reports, written=[5, 5, 5])
    assert (one_read.proven, one_read.not_unique, one_read.why) == (False, ["prp_id"], "repeated")
    later = sync.prove_pd_link(pds, reports, written=[4, 5, 5])
    assert (later.proven, later.pd_key, later.not_unique) == (True, "prp_id", [])
    assert later.index == {"77": 2, "78": 3}  # 77: the row the latest read wrote


@responses.activate
def test_an_older_version_of_a_progress_report_is_never_linked(linked, settings):
    """PRP's progress reports carry no id: QPR1 of programme document 77 is held as "Due", then accepted on a night
    its read is not whole (another report comes twice), so its new version gets a row of its own and the old one
    stays. Only the version the latest read wrote is linked to the PD: the old one is counted apart
    (pd_superseded), so no page counts the report twice, once as overdue."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    _, pd = linked
    FakeDatamart([_pd(77, external_id="11", reference_number="LEB/PD1"), _pd(78, external_id="999")],
                 ignore=("external_business_area_code",)).register(_url("sources/prp/unicefprogrammedocument"))  # fmt: skip
    due = _report(77, "QPR1", status="Due", due_date="2026-04-15")
    accepted = {**due, "status": "Accepted", "submission_date": "2026-04-10"}
    twice = _report(78, "QPR1")
    nights = []
    for version in (due, accepted):
        FakeDatamart([version, twice, twice]).register(_url("sources/prp/unicefprogressreport"))
        nights.append(run("prp_programme_documents,prp_progress_reports")[1])
        responses.reset()
        FakeDatamart([_pd(77, external_id="11", reference_number="LEB/PD1"), _pd(78, external_id="999")],
                     ignore=("external_business_area_code",)).register(_url("sources/prp/unicefprogrammedocument"))  # fmt: skip
    first, second = nights
    assert first.details["progress_reports_pd_link"]["linked"] == 1
    assert second.status == SyncRun.Status.PARTIAL and second.details["removal_skipped"]
    held = dict(
        DatamartDocument.objects.filter(
            dataset="prp_progress_reports", data__programme_document=77
        ).values_list("data__status", "intervention")
    )
    assert held == {"Due": None, "Accepted": pd.pk}
    link = second.details["progress_reports_pd_link"]
    assert (link["linked"], link["pd_superseded"], link["pd_not_linked"]) == (1, 1, 1)


def test_a_decoy_pair_whose_dates_disagree_is_never_proven():
    """eTools ids and PRP ids are small numbers in the same range: a pair matching them by chance links more
    reports than the true pair, but to documents of 2018 while the reports end in 2026. It is out
    (dates_disagree), and the true pair is used."""
    old = {"start_date": "2018-01-01", "end_date": "2018-12-31"}
    pds = [
        ("h1", {"prp_id": 10, "external_id": 20, "start_date": "2026-01-01", "end_date": "2026-12-31"}, None),
        ("h2", {"prp_id": 11, "external_id": 10, **old}, None),
        ("h3", {"prp_id": 12, "external_id": 30, **old}, None),
        ("h4", {"prp_id": 13, "external_id": 31, **old}, None),
    ]
    reports = [{"programme_document": n, "end_date": "2026-06-30"} for n in (10, 10, 10, 30, 31)]
    link = sync.prove_pd_link(pds, reports)
    assert (link.report_key, link.pd_key, link.proven) == ("programme_document", "prp_id", True)
    decoy = next(p for p in link.pairs if p["pd"] == "external_id")
    assert (decoy["linked"], decoy["dated"], decoy["agree"], decoy["verdict"]) == (5, 5, 0, "dates_disagree")
    assert link.index == {"10": None, "11": None, "12": None, "13": None}
    # with no dates on either side, three links of the five reports naming one are too few (nine in ten asked)
    undated = sync.prove_pd_link([(k, {"prp_id": d["prp_id"]}, pk) for k, d, pk in pds], reports)
    assert (undated.proven, undated.why, undated.by if hasattr(undated, "by") else None) == (
        False,
        "few",
        None,
    )


@responses.activate
def test_no_key_pair_links_clearly_so_today_s_lookup_is_kept(db):
    """Two keys of the documents link the reports to other documents, as often each: neither is proven, and the
    reports are linked as before, by the key each document is held under (here none)."""
    for key, prp_id, legacy in (("h1", 77, 78), ("h2", 78, 77)):
        DatamartDocument.objects.create(dataset="prp_programme_documents", record_key=key,
                                        data={"prp_id": prp_id, "legacy_id": legacy})  # fmt: skip
    for n, pd_id in enumerate((77, 78)):
        DatamartDocument.objects.create(dataset="prp_progress_reports", record_key=f"r{n}",
                                        data={"programme_document": pd_id})  # fmt: skip
    link = sync.link_progress_reports()
    assert (link["by"], link["proven"], link["why"]) == (["named", "record_key"], False, "not_clear")
    assert (link["pd_not_held"], link.get("linked", 0)) == (2, 0)
    assert link["pd_keys"] == {"record_key": 2, "legacy_id": 2, "prp_id": 2}


@responses.activate
def test_a_record_without_an_id_keeps_its_row_under_a_proven_key(linked, settings):
    """A programme document the old code held under its hash is renamed in place to its PRP id (the same row,
    no second one) once the read proves prp_id unique; changed the next night, it keeps that row."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    record = _pd(77, external_id="11", reference_number="LEB/PD1")
    old = DatamartDocument.objects.create(dataset="prp_programme_documents", record_key=sync.record_key(record),
                                          data=record)  # fmt: skip
    url = _url("sources/prp/unicefprogrammedocument")
    other = _pd(500, "0990", external_id="11", reference_number="SYR/PD1")
    FakeDatamart([record, other], ignore=("external_business_area_code",)).register(url)
    (first,) = run("prp_programme_documents")
    assert first.status == SyncRun.Status.SUCCEEDED and first.details["record_identity"] == "prp_id"
    row = DatamartDocument.objects.get(dataset="prp_programme_documents")
    assert (row.pk, row.record_key, row.intervention) == (old.pk, "prp_id:77", linked[1])
    assert first.details["id_keys"] == {"prp_id": 2, "external_id": 2}

    responses.reset()
    FakeDatamart([{**record, "title": "Amended"}, other], ignore=("external_business_area_code",)).register(
        url
    )
    (second,) = run("prp_programme_documents")
    assert second.status == SyncRun.Status.SUCCEEDED and second.details["record_identity"] == "prp_id"
    row = DatamartDocument.objects.get(dataset="prp_programme_documents")
    assert (row.pk, row.record_key, row.data["title"]) == (old.pk, "prp_id:77", "Amended")
    assert second.details["documents_removed"] == 0


@responses.activate
def test_a_key_unique_only_in_one_country_is_never_an_identity(linked, settings):
    """Read country-blind, other countries' programme documents repeat Lebanon's eTools ids: external_id is
    never the record identity, the documents keep their hash, and the read counts no record twice."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    documents = [
        {"external_business_area_code": "2490", "external_id": "11", "reference_number": "LEB/PD1"},
        {"external_business_area_code": "0990", "external_id": "11", "reference_number": "SYR/PD1"},
        {"external_business_area_code": "2340", "external_id": "11", "reference_number": "JOR/PD1"},
    ]
    FakeDatamart(documents, ignore=("external_business_area_code",)).register(
        _url("sources/prp/unicefprogrammedocument")
    )
    (found,) = run("prp_programme_documents")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    assert found.details["record_identity"] == "" and "duplicates_read" not in found.details
    assert list(_held("prp_programme_documents")) == [sync.record_key(documents[0])]


@responses.activate
def test_two_records_of_different_content_sharing_a_key_are_never_merged(linked, settings):
    """Two programme documents of Lebanon name PRP id 77 with different titles: prp_id is not unique tonight, so
    it is no identity; both are kept, each under its hash, and neither overwrites the other."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    documents = [_pd(77, title="First"), _pd(77, title="Second")]
    FakeDatamart(documents, ignore=("external_business_area_code",)).register(
        _url("sources/prp/unicefprogrammedocument")
    )
    (found,) = run("prp_programme_documents")
    assert found.details["record_identity"] == "" and found.details["documents"] == 2
    held = DatamartDocument.objects.filter(dataset="prp_programme_documents")
    assert sorted(held.values_list("data__title", flat=True)) == ["First", "Second"]


@responses.activate
def test_a_programme_document_naming_no_business_area_is_kept_only_by_its_reference_number(linked, settings):
    """A PRP programme document naming no business area is kept only when its reference number names a PD
    NeuroDB holds, and linked by that number alone (its eTools id 11 is another country's, here PD1's); one
    naming none NeuroDB holds is left out; a record of another business area naming one of NeuroDB's PDs by its
    reference number is counted: the business area key may be wrong."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    partner, pd1 = linked
    pd2 = PCA.objects.create(etl_id="12", number="LEB/PD2", title="PD 2", partner=partner)
    documents = [
        {"id": 77, "external_business_area_code": "2490", "external_id": "11", "reference_number": "LEB/PD1"},
        {"id": 78, "external_id": "11", "reference_number": "LEB/PD2"},  # names no business area
        {"id": 79, "external_id": "13", "reference_number": "SYR/PD5"},  # names none, nor a PD NeuroDB holds
        {"id": 80, "external_business_area_code": "0990", "external_id": "11", "reference_number": "LEB/PD1"},
    ]
    FakeDatamart(documents, ignore=("external_business_area_code",)).register(
        _url("sources/prp/unicefprogrammedocument")
    )
    (found,) = run("prp_programme_documents")
    assert found.status == SyncRun.Status.SUCCEEDED, found.details
    details = found.details
    assert _held("prp_programme_documents") == {"77": pd1.pk, "78": pd2.pk}
    assert (details["kept_by_reference"], details["unnamed_skipped"], details["other_country_skipped"]) == (
        1,
        1,
        2,
    )
    assert (details["linked_to_etools"], details["other_code_named_here"]) == (2, 1)
    assert "country_unnamed" not in details and "record_identity" not in details


def test_id_like_values_and_keys_are_read_as_ids_never_as_a_person():
    assert [sync.id_value(v) for v in (77, "77", 77.0, " 77 ", {"id": 77}, "LEB/PD1")] == ["77"] * 5 + [
        "LEB/PD1"
    ]
    assert [sync.id_value(v) for v in (None, "", True, 7.5, [77], "x" * 41)] == [""] * 6
    record = {"prp_id": 1, "pk": 2, "external_id": "11", "programme_document": {"id": 3}, "focal_point_id": 4,
              "report_number": "QPR1", "unicef_focal_point": {"id": 5}}  # fmt: skip
    assert sync.id_keys([record]) == {"prp_id": 1, "pk": 1, "external_id": 1, "programme_document": 1}
    reports = [
        {"programme_document_id": 1, "pd": "2", "programmeDocument": 3, "pd_focal_point": 4, "pd_title": ""}
    ]
    assert sync.pd_named_keys(reports) == ["programme_document_id", "pd", "programmeDocument"]


def test_a_read_under_a_business_area_filter_chooses_no_new_record_identity():
    """Under a proven business area filter a key unique only within the country would pass: no new identity is
    chosen there, last night's is kept while it still qualifies."""
    tracker = sync.IdentityTracker()
    for n in (1, 2):
        tracker.saw({"external_id": str(n), "prp_id": n}, f"content {n}")
    assert sync.identity_key(tracker) == "external_id"  # by name, with no filter
    assert sync.identity_key(tracker, filtered=True) == ""
    assert sync.identity_key(tracker, "prp_id", filtered=True) == "prp_id"
    tracker.saw({"external_id": "1", "prp_id": 3}, "content 3")  # another record repeats external_id 1
    assert (tracker.keys, sync.identity_key(tracker)) == (["prp_id"], "prp_id")
    tracker.saw({"external_id": "4"}, "content 4")  # a record without prp_id: no key is left
    assert (tracker.keys, sync.identity_key(tracker, "prp_id")) == ([], "")
