"""The syncs' raw copies for Raw eTools data: the fields every sync counts as it keeps its records
(``details.keys_kept``), the job "Read the eTools REST API into Raw eTools data" (``rest_raw``: each entity kept
as it came, removed when eTools no longer lists it, nothing else written), its button, and the copy the eTools
REST sync keeps as it reads. Synthetic people only."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import responses
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VALIDATORS, VIEWER
from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, checks, raw, runfacts
from neurodb.datamart import models as dm
from neurodb.integrations import background
from neurodb.integrations.etools import datamart_sync, rest_raw, sync
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session
from neurodb.partnerships.models import PCA, ActionPoint, Agreement, Engagement, PartnerOrganization, Travel
from neurodb.watch import people

pytestmark = pytest.mark.django_db

NAME, EMAIL, UNKNOWN = "Person One", "person.one@example.org", "Zorba Quintel"
BASE = "https://etools.test"
DATAMART = "https://datamart.test"
INDEX = reverse("admin:datamart_raw")


def rest_client() -> EToolsClient:
    return EToolsClient(BASE, "abc", session=make_session("Token abc", backoff=0))


def datamart_client() -> DatamartClient:
    return DatamartClient(
        DATAMART, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


# ------------------------------------------------------------------------------------- the fields counted
ODD = {  # field names never stored: an address, a person's name, too long
    EMAIL: "approved",
    UNKNOWN: "approved",
    "x" * 70: 1,
}


def test_the_count_keeps_field_names_and_counts_never_odd_names_or_contacts():
    fields = raw.KeyCount("grants")
    fields.add(catalogue.scrub({"id": 1, "name": "SC1", "donor": "", "contact_email": EMAIL, **ODD}))
    fields.add({"id": 2, "name": "SC2", "notes": [], "budget": {}})
    kept = fields.details()
    assert kept["id"] == [2, 2] and kept["name"] == [2, 2]
    assert kept["donor"] == [1, 0] and kept["notes"] == [1, 0] and kept["budget"] == [1, 0]
    assert kept[raw.OTHER_FIELDS] == [1, 1]  # one record carries them, with a value
    text = json.dumps(kept)
    for planted in (EMAIL, UNKNOWN, "contact_email", "x" * 70, "Zorba"):
        assert planted not in text


def test_the_count_keeps_the_most_carried_fields_and_sums_the_others(monkeypatch):
    monkeypatch.setattr(raw, "KEYS_KEPT", 2)
    fields = raw.KeyCount("grants")
    for record in ({"a": 1, "b": 1, "c": 1, "d": None}, {"a": 1, "b": 2}, {"a": 3}):
        fields.add(record)
    assert fields.details() == {"a": [3, 3], "b": [2, 2], raw.MORE_FIELDS: [2, 1]}


def test_a_psea_answer_keeps_only_its_safeguarding_fields_by_name():
    fields = raw.KeyCount("psea_answers")
    fields.add({"id": 1, "comments": "a case", "assessment": 3})
    kept = fields.details()
    assert set(kept) - {raw.OTHER_FIELDS} <= catalogue.SAFEGUARDING_KEYS
    assert "comments" not in kept and kept[raw.OTHER_FIELDS][0] == 1


@responses.activate
def test_a_typed_dataset_counts_the_fields_of_the_records_it_keeps():
    responses.get(
        f"{DATAMART}/api/latest/datamart/funds/grants/",
        json={
            "count": 3,
            "next": None,
            "results": [
                {"id": 1, "name": "SC1", "donor": "EU", "contact_email": EMAIL, **ODD},
                {"id": 2, "name": "SC2", "donor": "", "expiry": None},
                {"id": 1, "name": "SC1", "donor": "EU"},  # returned again: counted once
            ],
        },
    )
    (run,) = datamart_sync.sync_all(only=["grants"], triggered_by="test", client=datamart_client())
    kept = run.details["keys_kept"]
    assert kept["id"] == [2, 2] and kept["donor"] == [2, 1] and kept["expiry"] == [1, 0]
    assert kept[raw.OTHER_FIELDS] == [1, 1]
    text = json.dumps(run.details)
    for planted in (EMAIL, UNKNOWN, "contact_email", "Zorba"):
        assert planted not in text
    # the source's page counts its fields from the run: every record it kept
    assert {f.key: (f.carrying, f.with_value) for f in raw.field_counts("grants", frozenset(), run)[0]}[
        "donor"
    ] == (2, 1)


@responses.activate
def test_a_dataset_kept_whole_counts_the_fields_of_the_records_it_keeps():
    responses.get(
        f"{DATAMART}/api/latest/datamart/interventions-epd/",
        json={
            "next": None,
            "results": [
                {"id": 1, "pd_number": "LEB/PD1", "context": "Context", "country_name": "Lebanon", **ODD},
                {"id": 2, "pd_number": "SYR/PD9", "country_name": "Syria"},  # another country: not kept
            ],
        },
    )
    (run,) = datamart_sync.sync_all(only=["intervention_epd"], triggered_by="test", client=datamart_client())
    kept = run.details["keys_kept"]
    assert kept["pd_number"] == [1, 1] and kept["context"] == [1, 1] and kept[raw.OTHER_FIELDS] == [1, 1]
    assert EMAIL not in json.dumps(kept) and UNKNOWN not in json.dumps(kept)


# ---------------------------------------------------------------------------------------- the eTools REST API
RECENT = (timezone.localdate() - dt.timedelta(days=10)).isoformat()


def partner(pk: int) -> dict:
    return {"id": pk, "name": f"Partner {pk}", "vendor_number": f"V{pk}", "email": EMAIL, "phone_number": "1"}


def register(partners=(1, 2), *, agreements_said: int | None = None, trips=((1, RECENT),)) -> None:
    """The eTools REST API: lists and details of every entity."""
    responses.get(f"{BASE}/api/v2/partners/", json=[partner(pk) for pk in partners])
    for pk in partners:
        responses.get(
            f"{BASE}/api/v2/partners/{pk}/",
            json={**partner(pk), "staff_members": [{"first_name": "Zorba", "last_name": "Quintel"}]},
        )
    agreements = [{"id": 10, "partner": 1, "agreement_number": "LEBA/PCA1"}]
    responses.get(
        f"{BASE}/api/v2/agreements/",
        json={"count": agreements_said or len(agreements), "next": None, "results": agreements},
    )
    responses.get(f"{BASE}/api/v2/interventions/", json=[{"id": 100, "number": "LEB/PD1", "title": "PD one"}])
    responses.get(f"{BASE}/api/v2/interventions/100/", json={"number": "LEB/PD1", "status": "active"})
    responses.get(
        f"{BASE}/api/t2f/travels/?page_size=1000",
        json={
            "data": [
                {
                    "id": pk,
                    "reference_number": f"T{pk}",
                    "traveler": NAME,
                    "purpose": f"Visit with {NAME}",
                    "start_date": start,
                }
                for pk, start in trips
            ]
        },
    )
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000&page=2", status=404)
    for pk, _start in trips:
        responses.get(f"{BASE}/api/t2f/travels/{pk}/", json={"id": pk, "reference_number": f"T{pk}"})
    responses.get(
        f"{BASE}/api/audit/engagements/?page_size=1000",
        json={
            "count": 1,
            "next": None,
            "results": [{"id": 7, "engagement_type": "sc", "status": "final", "reference_number": "SC/7"}],
        },
    )
    responses.get(
        f"{BASE}/api/audit/spot-checks/7/",
        json={
            "id": 7,
            "reference_number": "SC/7",
            "explanation_for_additional_information": f"Met {UNKNOWN}",
        },
    )
    responses.get(
        f"{BASE}/api/action-points/action-points/?page_size=1000&engagement=7",
        json={
            "count": 1,
            "next": None,
            "results": [{"id": 70, "reference_number": "AP/70", "status": "open"}],
        },
    )


def held(entity: str) -> set[str]:
    return set(dm.RestRecord.objects.filter(entity=entity).values_list("record_key", flat=True))


@responses.activate
def test_the_read_keeps_every_entity_as_it_came_and_writes_no_other_table():
    register()
    runs = rest_raw.read_all(triggered_by="test", client=rest_client())
    assert [r.target for r in runs] == list(rest_raw.ENTITIES) == list(raw.REST_ENTITIES)
    assert all(r.status == SyncRun.Status.SUCCEEDED for r in runs), [(r.target, r.error) for r in runs]
    assert all(r.job == SyncRun.Job.ETOOLS_REST_RAW for r in runs)
    assert held("rest_partners") == held("rest_partner_details") == {"1", "2"}
    assert held("rest_intervention_details") == {"100"}  # the detail names no id: the one it was asked by
    assert held("rest_engagement_details") == {"7"} and held("rest_action_points") == {"70"}
    assert dm.RestRecord.objects.get(entity="rest_action_points").parent_key == "7"
    for row in dm.RestRecord.objects.all():
        assert "email" not in row.data and "phone_number" not in row.data, row.entity
    trip = dm.RestRecord.objects.get(entity="rest_travels")
    assert (trip.reference, trip.title) == ("T1", "T1")  # never its purpose
    partner_row = dm.RestRecord.objects.get(entity="rest_partners", record_key="1")
    assert (partner_row.reference, partner_row.title, partner_row.source_id) == ("V1", "Partner 1", "1")
    by = {r.target: r for r in runs}
    assert by["rest_agreements"].details["announced"] == 1  # the DRF count
    assert "announced" not in by["rest_travels"].details  # the t2f list has no count
    for model in (PartnerOrganization, Agreement, PCA, Travel, Engagement, ActionPoint):
        assert not model.objects.exists(), model
    kept = by["rest_partner_details"].details["keys_kept"]
    assert kept["staff_members"] == [2, 2] and "email" not in kept


@responses.activate
def test_a_second_read_removes_what_etools_no_longer_lists_with_its_details():
    register()
    rest_raw.read_all(triggered_by="test", client=rest_client())
    responses.reset()
    register(partners=(1,))
    runs = {r.target: r for r in rest_raw.read_all(triggered_by="test", client=rest_client())}
    assert held("rest_partners") == held("rest_partner_details") == {"1"}
    assert runs["rest_partners"].details["removed"] == 1
    assert runs["rest_partners"].details["details_removed"] == 1


@responses.activate
def test_a_detail_older_than_the_detail_window_stays():
    old = (timezone.localdate() - dt.timedelta(days=rest_raw.DETAIL_DAYS + 30)).isoformat()
    dm.RestRecord.objects.create(entity="rest_travel_details", record_key="2", source_id="2", parent_key="2")
    register(trips=((1, RECENT), (2, old)))
    runs = {r.target: r for r in rest_raw.read_all(triggered_by="test", client=rest_client())}
    assert held("rest_travels") == {"1", "2"} and held("rest_travel_details") == {"1", "2"}
    assert runs["rest_travel_details"].details["read"] == 1  # the trip of the last year only


@responses.activate
def test_a_failed_or_short_read_removes_nothing():
    dm.RestRecord.objects.create(entity="rest_partners", record_key="9", source_id="9")
    dm.RestRecord.objects.create(entity="rest_agreements", record_key="99", source_id="99")
    register(agreements_said=3)
    responses.replace(responses.GET, f"{BASE}/api/v2/partners/", status=500)
    runs = {r.target: r for r in rest_raw.read_all(triggered_by="test", client=rest_client())}
    assert runs["rest_partners"].status == SyncRun.Status.FAILED and "9" in held("rest_partners")
    agreements = runs["rest_agreements"].details
    assert agreements["removal_skipped"] is True and "99" in held("rest_agreements")
    assert agreements["removal_skipped_why"] == "eTools said it had 2 more"
    facts = runfacts.facts("rest_agreements")
    assert (facts.said_it_had, facts.received, facts.never_came) == (3, 1, 2)
    assert facts.complete == (False, "No: eTools said it had 2 more")
    assert runfacts.facts("rest_partners").newer_failed is not None


@responses.activate
def test_a_list_that_came_back_empty_is_not_the_read_the_records_come_from():
    """eTools answers a list with nothing (it is down, or the token's user lost the country): the copy keeps
    the records of the read before, and so does what the page says of them, whichever job read the list."""
    responses.get(f"{BASE}/api/v2/partners/", json=[partner(1), partner(2)])
    (full,) = rest_raw.read_all(only=["rest_partners"], triggered_by="test", client=rest_client())
    responses.replace(responses.GET, f"{BASE}/api/v2/partners/", json=[])
    (empty,) = rest_raw.read_all(only=["rest_partners"], triggered_by="test", client=rest_client())
    assert empty.status == SyncRun.Status.SUCCEEDED and empty.details["read"] == 0
    assert empty.details["removal_skipped_why"] == "eTools listed nothing" and held("rest_partners") == {
        "1",
        "2",
    }
    facts = runfacts.facts("rest_partners")
    assert facts.run == full
    assert (facts.received, facts.held, facts.updated_by_sync, facts.from_earlier) == (2, 2, 2, 0)
    assert facts.complete == (True, "Yes")
    # the eTools REST sync's copy: the same
    responses.replace(responses.GET, f"{BASE}/api/v2/partners/", json=[partner(1), partner(2)])
    (synced,) = sync.sync_all(only=["partners"], client=rest_client(), triggered_by="test")
    responses.replace(responses.GET, f"{BASE}/api/v2/partners/", json=[])
    (after,) = sync.sync_all(only=["partners"], client=rest_client(), triggered_by="test")
    assert after.details["raw_copies"]["rest_partners"]["read"] == 0
    assert runfacts.rest_runs(["rest_partners"])["rest_partners"] == (synced, None)
    # a list read that received nothing is never complete, wherever it comes from
    assert runfacts.build("rest_partners", empty, None, (2, 0, 0)).complete == (
        False,
        "No: eTools sent nothing",
    )
    # a detail asks for the records of its list's copy: none asked is no outage
    details = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_REST_RAW,
        target="rest_travel_details",
        status=SyncRun.Status.SUCCEEDED,
        details={"read": 0, "kept": 0},
    )
    details.finish(SyncRun.Status.SUCCEEDED)
    assert runfacts.facts("rest_travel_details").run == details
    assert runfacts.facts("rest_travel_details").complete == (True, "Yes")


def test_without_the_token_the_read_records_why(settings):
    settings.ETOOLS_TOKEN = ""
    (run,) = rest_raw.read_all(triggered_by="test")
    assert run.status == SyncRun.Status.FAILED and run.error == rest_raw.NO_TOKEN
    assert not dm.RestRecord.objects.exists()


def test_a_second_read_waits_for_the_first(monkeypatch):
    monkeypatch.setattr(rest_raw, "_lock", lambda: False)
    assert rest_raw.read_all(triggered_by="test", client=rest_client()) == rest_raw.BUSY
    assert not SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_REST_RAW).exists()


def test_the_read_has_its_lock_and_its_button_in_run_a_job():
    from neurodb.core.admin_jobs import BACKGROUND_JOBS

    assert background.LOCK_IDS[SyncRun.Job.ETOOLS_REST_RAW] == rest_raw.LOCK_ID
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)
    (spec,) = [s for s in BACKGROUND_JOBS if s.command == (rest_raw.COMMAND,)]
    assert spec.job == SyncRun.Job.ETOOLS_REST_RAW


# ------------------------------------------------------------------------------------------------ the button
def _staff(username: str, groups: tuple[str, ...], *, superuser: bool = False) -> User:
    user = User.objects.create_user(
        username=username, password="pass-123456-x", is_staff=True, is_superuser=superuser
    )
    user.groups.set(Group.objects.filter(name__in=groups))
    return user


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    monkeypatch.setattr(background, "is_running", lambda job: False)
    return calls


def test_an_administrator_validator_starts_the_read_from_raw_etools_data(client, roles, settings, started):
    settings.ETOOLS_TOKEN = "abc"
    user = _staff("checker", (ADMIN, VALIDATORS))
    client.force_login(user)
    page = client.get(INDEX).content.decode()
    assert "Read the eTools REST API now" in page and reverse("admin:datamart_raw_rest_read") in page
    response = client.post(reverse("admin:datamart_raw_rest_read"))
    assert response.status_code == 302
    assert started == [(rest_raw.COMMAND, "--triggered-by", "checker")]


def test_a_read_that_already_ended_is_no_longer_shown_as_starting(client, roles, settings, monkeypatch):
    """Pressed, the read is "starting" until its process writes a run; once a run started after the press is
    there (here, already ended), the page follows the runs and shows the button again."""
    settings.ETOOLS_TOKEN = "abc"
    monkeypatch.setattr(background, "start_command", lambda *args: background._note_started(args[0]) or 1)
    client.force_login(_staff("checker", (ADMIN, VALIDATORS)))
    client.post(reverse("admin:datamart_raw_rest_read"))
    page = client.get(INDEX).content.decode()
    assert "is starting" in page and "Read the eTools REST API now" not in page
    for entity in rest_raw.ENTITIES:
        run = SyncRun.objects.create(
            job=SyncRun.Job.ETOOLS_REST_RAW, target=entity, details={"read": 0, "kept": 0}
        )
        run.finish(SyncRun.Status.SUCCEEDED)
    page = client.get(INDEX).content.decode()
    assert "is starting" not in page and "Read the eTools REST API now" in page
    assert background.started_at(rest_raw.COMMAND) is None  # the note is forgotten


def test_without_the_token_the_page_says_so_and_shows_no_button(client, roles, settings, started):
    settings.ETOOLS_TOKEN = ""
    client.force_login(_staff("root", (), superuser=True))
    page = client.get(INDEX).content.decode()
    assert "The eTools REST API token is not set on this server: ask the NeuroDB team." in page
    assert "Read the eTools REST API now" not in page
    client.post(reverse("admin:datamart_raw_rest_read"))
    assert started == []


def test_a_validator_who_is_not_an_administrator_sees_the_reads_and_no_button(
    client, roles, settings, started
):
    settings.ETOOLS_TOKEN = "abc"
    client.force_login(_staff("validator", (VIEWER, VALIDATORS)))
    page = client.get(INDEX).content.decode()
    assert "Read the eTools REST API now" not in page and "Not read yet." in page
    assert client.post(reverse("admin:datamart_raw_rest_read")).status_code == 403
    assert started == []
    # an Administrator who is not a validator opens no Raw eTools data (the job is in Run a job for them)
    client.force_login(_staff("admin-only", (ADMIN,)))
    assert client.post(reverse("admin:datamart_raw_rest_read")).status_code == 404
    assert started == []


# ------------------------------------------------------------------------------------------ the REST sync
@responses.activate
def test_the_rest_sync_keeps_the_copy_too_and_its_page_reads_it(client, roles):
    register()
    runs = {r.target: r for r in sync.sync_all(client=rest_client(), triggered_by="test")}
    assert held("rest_partners") == {"1", "2"} and PartnerOrganization.objects.count() == 2
    assert held("rest_travels") == {"1"} and held("rest_travel_details") == {"1"}
    assert held("rest_engagements") == held("rest_engagement_details") == {"7"}
    assert held("rest_action_points") == {"70"}
    copies = runs["travels"].details["raw_copies"]
    assert copies["rest_travels"]["read"] == 1 and copies["rest_travel_details"]["kept"] == 1
    assert runs["agreements"].details["raw_copies"]["rest_agreements"]["announced"] == 1
    run, newer = runfacts.rest_runs(["rest_travel_details"])["rest_travel_details"]
    assert run == runs["travels"] and newer is None
    facts = runfacts.facts("rest_travel_details")
    assert (facts.received, facts.not_saved, facts.updated_by_sync, facts.unexplained) == (1, 0, 1, 0)
    assert facts.complete == (True, "Yes")
    # a list no longer listing a partner removes its copy, as the read into Raw eTools data does
    responses.reset()
    register(partners=(2,))
    (again,) = sync.sync_all(only=["partners"], client=rest_client(), triggered_by="test")
    assert again.details["raw_copies"]["rest_partners"]["removed"] == 1 and held("rest_partners") == {"2"}
    client.force_login(_staff("validator", (VIEWER, VALIDATORS)))
    page = client.get(reverse("admin:datamart_raw_source", args=["rest_partners"])).content.decode()
    assert "Read from the eTools REST API" in page and "Counted by the sync of" in page


@responses.activate
def test_a_partner_s_staff_named_in_its_details_is_taken_out_of_an_engagement_s_texts(client, roles):
    register()
    rest_raw.read_all(triggered_by="test", client=rest_client())
    people.forget()
    assert UNKNOWN.lower() in people.known_names()
    client.force_login(_staff("validator", (VIEWER, VALIDATORS)))
    url = reverse("admin:datamart_raw_csv", args=["rest_engagement_details"])
    text = b"".join(client.get(url).streaming_content).decode("utf-8-sig")
    assert "Met" in text and UNKNOWN not in text and "Quintel" not in text


# ------------------------------------------------------------------------------------------- checks 10, 11
def test_checks_10_and_11_count_what_a_sync_received_by_one_rule():
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        rows_in=6,
        details={"other_country_skipped": 4},
    )
    assert runfacts.records_read(run) == 10
    assert "NeuroDB read 10" in checks._partner_read(run)[0]
    run.details["read"] = 12  # the read recorded the distinct records it received
    assert runfacts.records_read(run) == 12 and "NeuroDB read 12" in checks._partner_read(run)[0]
