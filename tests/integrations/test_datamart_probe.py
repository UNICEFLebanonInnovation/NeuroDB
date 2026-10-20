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
    assert "Admin → Data and sync → eTools Datamart: PRP business area code" in run.details["verdict"]


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


# ------------------------------------------------------------------- the business area answer, judged first
def _verdict(answers: dict) -> str:
    return probe.verdict("prp_indicator_reports", "prp/indicator-report-v2", "business_area", "2490", answers)


def _answer(label: str, count: int | None = None, codes: dict | None = None, **failed) -> dict:
    if failed:
        return {"request": label, "filter": {}, **failed}
    return {"request": label, "filter": {}, "count": count, "page": sum((codes or {}).values()),
            "business_areas": codes or {}}  # fmt: skip


@responses.activate
def test_an_ignored_business_area_filter_is_never_said_to_work(settings):
    """The Datamart answers 9,000 records of 2340, 6810 and 2490 with the filter as without it: the verdict
    says it seems to ignore the filter, never that the filter works."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"
    everyone = [record(1, "2340"), record(2, "6810"), record(3, "2490")]
    answer(V2, 9000, everyone)
    answer(V2, 9000, everyone, business_area="2490")
    answer(V2, 9000, everyone, country_name="Lebanon")
    run = probe.run_job("prp_indicator_reports", "tester", client=client())
    verdict = run.details["verdict"]
    assert run.details["requests"][1]["business_areas"] == {"2340": 1, "6810": 1, "2490": 1}
    assert "the filter works" not in verdict and "seems to ignore the business area filter" in verdict
    assert "of the 3 records on its page, 1 names business area 2490, 2 another and 0 none" in verdict
    assert "tell the eTools Datamart team" in verdict


@pytest.mark.parametrize(
    ("answers", "said", "never"),
    [
        (  # (a) the same count and page with the filter as without: ignored
            {
                probe.NO_FILTER: _answer("no filter", 9000, {"2340": 2, "2490": 1}),
                probe.BY_BUSINESS_AREA: _answer("business area", 9000, {"2340": 2, "2490": 1}),
                probe.BY_COUNTRY: _answer("country", 0),
            },
            "seems to ignore the business area filter (business_area=2490)",
            "the filter works",
        ),
        (  # (a') as many as with no filter, and the no-filter page names other codes: ignored
            {
                probe.NO_FILTER: _answer("no filter", 9000, {"2340": 2, "2490": 1}),
                probe.BY_BUSINESS_AREA: _answer("business area", 9000, {"2490": 3}),
                probe.BY_COUNTRY: _answer("country", 0),
            },
            "seems to ignore the business area filter",
            "the filter works",
        ),
        (  # (b) nothing with no filter, 120 with the business area: never "no record at all"
            {
                probe.NO_FILTER: _answer("no filter", 0),
                probe.BY_BUSINESS_AREA: _answer("business area", 120, {"2490": 50}),
                probe.BY_COUNTRY: _answer("country", 0),
            },
            "has 120 records for business area 2490: the filter works",
            "no record at all",
        ),
        (  # (c) the business area request refused: said with its status, whatever the country filter finds
            {
                probe.NO_FILTER: _answer("no filter", 9000, {"2340": 2}),
                probe.BY_BUSINESS_AREA: _answer("business area", status=400, error="IntegrationError: 400"),
                probe.BY_COUNTRY: _answer("country", 120),
            },
            "refused the business area request (business_area=2490) (HTTP 400), and the sync of "
            "prp_indicator_reports asks with that same filter",
            "the filter works",
        ),
        (  # every answer 0: the dataset holds nothing
            {
                probe.NO_FILTER: _answer("no filter", 0),
                probe.BY_BUSINESS_AREA: _answer("business area", 0),
                probe.BY_COUNTRY: _answer("country", 0),
            },
            "holds no record at all",
            "the filter works",
        ),
    ],
)
def test_the_business_area_answer_is_judged_first(settings, answers, said, never):
    settings.ETOOLS_DATAMART_COUNTRY = "Lebanon"
    verdict = _verdict(answers)
    assert said in verdict and never not in verdict


def test_a_refused_business_area_request_says_what_to_do_from_the_admin(settings):
    found = _verdict(
        {
            probe.NO_FILTER: _answer("no filter", 9000, {"2340": 2}),
            probe.BY_BUSINESS_AREA: _answer("business area", status=400, error="IntegrationError: 400"),
            probe.BY_COUNTRY: _answer("country", 120),
        }
    )
    assert "check the code in Admin → Data and sync → eTools Datamart: PRP business area code" in found


# ------------------------------------------------------------------------- what the probe found, in the admin
def test_the_verdict_is_on_the_runs_list_and_above_the_runs_details(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    run = SyncRun.objects.create(job=SyncRun.Job.DATAMART_PROBE, target="prp_indicator_reports")
    run.finish(SyncRun.Status.SUCCEEDED, verdict="The Datamart answered every request & the filter works.")
    shown = "The Datamart answered every request &amp; the filter works."
    listed = client.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert shown in listed  # the Error cell of the run, in plain words
    page = client.get(reverse("admin:core_syncrun_change", args=[run.pk])).content.decode()
    assert "What the probe found" in page
    found, details = page.index("What the probe found"), page.index(">Details<")
    assert found < details and shown in page[found:details]


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
