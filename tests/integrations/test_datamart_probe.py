"""The probe of one eTools Datamart dataset (*Run a job → Probe a Datamart dataset*): at most three small
requests, counts and key names only in its run, nothing else written."""

import json

import pytest
import responses
from django.core.management import call_command
from django.urls import reverse

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.datamart.models import DatamartDocument
from neurodb.integrations import background
from neurodb.integrations.etools import probe
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
V2 = f"{BASE}/api/latest/prp/indicator-report-v2/"


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def record(pk: int, area: str, **extra) -> dict:
    return {
        "id": pk,
        "business_area": area,
        "pd_reference_number": f"LEB/PD{pk}",
        "performance_indicator": "Children reached",
        "narrative_assessment": "A partner's own words",
        **extra,
    }


def answer(url: str, count: int, results: list[dict], **query) -> None:
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "50", **query})],
        json={"count": count, "next": f"{url}?page=2", "results": results},
    )


def _query(call) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return dict(parse_qsl(urlsplit(call.request.url).query))


@responses.activate
def test_three_small_requests_and_only_counts_key_names_and_codes(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    held = DatamartDocument.objects.create(dataset="prp_indicator_reports", record_key="1")
    answer(V2, 9000, [record(1, "2340"), record(2, "2490"), record(3, " 2490.0", country_name="Lebanon")])
    answer(V2, 0, [], business_area="2490")
    answer(V2, 0, [], country_name="Lebanon")
    run = probe.run_job("prp_indicator_reports", "tester", client=client())
    assert [_query(call) for call in responses.calls] == [
        {"page_size": "50"},
        {"page_size": "50", "business_area": "2490"},
        {"page_size": "50", "country_name": "Lebanon"},
    ]
    assert (run.job, run.target, run.status, run.triggered_by) == (
        SyncRun.Job.DATAMART_PROBE,
        "prp_indicator_reports",
        SyncRun.Status.SUCCEEDED,
        "tester",
    )
    assert (run.rows_in, run.rows_written) == (3, 0)
    everything, by_area, by_country = run.details["requests"]
    assert (everything["count"], everything["page"], by_area["count"], by_country["count"]) == (9000, 3, 0, 0)
    assert everything["business_areas"] == {"2490": 2, "2340": 1}
    assert everything["countries"] == {"Lebanon": 1}
    assert everything["keys"]["narrative_assessment"] == 3 and everything["keys"]["country_name"] == 1
    assert (run.details["business_area"], run.details["business_area_from"]) == ("2490", "environment")
    # nothing written but the run, and no value of a record in it
    assert list(DatamartDocument.objects.all()) == [held]
    stored = json.dumps(run.details)
    for text in ("Children reached", "A partner's own words", "LEB/PD1"):
        assert text not in stored
    # the verdict says what the answers mean and what to do from the admin
    assert "none for business area 2490" in run.details["verdict"]
    assert "Admin → eTools Datamart: PRP business area code" in run.details["verdict"]


@responses.activate
def test_the_verdict_names_a_country_filter_that_finds_what_the_code_does_not():
    DatamartSetting.objects.create(pk=1, prp_business_area="2490")
    answer(V2, 9000, [record(1, "2340")])
    answer(V2, 0, [], business_area="2490")
    answer(V2, 120, [record(2, "LBN")], country_name="Lebanon")
    run = probe.run_job("prp_indicator_reports", client=client())
    assert run.details["business_area_from"] == "admin"
    assert "the country filter finds 120 records" in run.details["verdict"]


@responses.activate
def test_without_a_code_set_the_probe_takes_the_workspace_held_and_asks_nothing_more(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    DatamartDocument.objects.create(
        dataset="workspaces", record_key="2", data={"name": "Lebanon", "business_area_code": "2490"}
    )
    answer(V2, 10, [record(1, "2490")])
    answer(V2, 10, [record(1, "2490")], business_area="2490")
    answer(V2, 10, [record(1, "2490")], country_name="Lebanon")
    run = probe.run_job("prp_indicator_reports", client=client())
    assert len(responses.calls) == 3  # no request to the workspaces
    assert (run.details["business_area"], run.details["business_area_from"]) == ("2490", "workspaces (held)")
    assert "has 10 records for business area 2490: the filter works" in run.details["verdict"]


@responses.activate
def test_a_country_dataset_takes_two_requests_and_a_refusal_is_kept_as_its_status(settings):
    url = f"{BASE}/api/latest/datamart/funds/grants/"
    answer(url, 40, [{"id": 1, "name": "SC1", "country_name": "Syria"}])
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "50", "country_name": "Lebanon"})],
        status=403,
    )
    run = probe.run_job("grants", client=client())
    assert len(responses.calls) == 2
    assert run.status == SyncRun.Status.PARTIAL  # a refusal is what the probe found, not its failure
    everything, by_country = run.details["requests"]
    assert everything["countries"] == {"Syria": 1} and "business_areas" not in everything
    assert by_country["status"] == 403 and "HTTP 403" in by_country["error"]
    assert "filter_key" not in run.details


@responses.activate
def test_a_refused_account_is_said_in_plain_words():
    for query in ({}, {"business_area": "2490"}, {"country_name": "Lebanon"}):
        responses.get(
            V2, match=[responses.matchers.query_param_matcher({"page_size": "50", **query})], status=403
        )
    DatamartSetting.objects.create(pk=1, prp_business_area="2490")
    run = probe.run_job("prp_indicator_reports", client=client())
    assert run.status == SyncRun.Status.PARTIAL
    assert "refused NeuroDB's account" in run.details["verdict"]


def test_without_credentials_the_probe_fails_clearly(settings):
    settings.ETOOLS_USERNAME = settings.ETOOLS_PASSWORD = ""
    run = probe.run_job("prp_indicator_reports")
    assert run.status == SyncRun.Status.FAILED and "credentials" in run.error


def test_the_command_refuses_an_unknown_dataset():
    from django.core.management import CommandError

    with pytest.raises(CommandError, match="unknown eTools Datamart dataset"):
        call_command("probe_datamart", "--dataset", "nope")


def test_a_probe_is_never_an_alarm(db):
    from neurodb.core.jobs import NEVER_AN_ALARM
    from neurodb.reports import services

    assert SyncRun.Job.DATAMART_PROBE in NEVER_AN_ALARM
    assert SyncRun.Job.DATAMART_PROBE in services.HEALTH_SKIPPED_JOBS
    assert background.LOCK_IDS[SyncRun.Job.DATAMART_PROBE] == probe.LOCK_ID


# ------------------------------------------------------------------------------------------- the button
@pytest.fixture
def started(monkeypatch):
    from neurodb.integrations.etools import datamart

    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    monkeypatch.setattr(datamart, "configured", lambda: True)
    return calls


URL = "admin:core_syncrun_run_probe_datamart"


def test_administrators_probe_a_dataset_from_run_a_job(client, admin_user, started):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    html = client.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert "Probe a Datamart dataset" in html
    assert client.get(reverse(URL)).status_code == 200 and started == []  # a GET only shows the dialog
    response = client.post(reverse(URL), {"_form_submitted": "on", "dataset": "prp_progress_reports"})
    assert response.status_code == 302
    assert started == [
        ("probe_datamart", "--dataset", "prp_progress_reports", "--triggered-by", admin_user.username)
    ]


def test_viewers_cannot_probe(client, viewer, started):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    client.post(reverse(URL), {"_form_submitted": "on", "dataset": "prp_indicator_reports"})
    assert started == []
