"""What's new: changes noticed between builds of the knowledge hub, the rebuild after new data, the
assistant lookup, the page, the overview card and the daily note."""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.assistant import tools
from neurodb.assistant.tools import ToolInputError
from neurodb.core.models import SyncRun
from neurodb.geo.models import DistrictLocation
from neurodb.graph import build, builders, digest, refresh
from neurodb.graph.management.commands.whats_new_digest import run as write_notes
from neurodb.graph.models import Change, Digest, DigestSubscription, Edge, Entity, RefreshRequest
from neurodb.graph.news import sentence
from neurodb.integrations.runs import new_run
from neurodb.library.models import Map
from neurodb.partnerships.models import PartnerOrganization


def _build():
    return build.run(triggered_by="test", documents=False)


@pytest.fixture
def changed(world):
    """A first build, then the news: a PD ends, its budget rises, a new donor funds it, a partner joins,
    a map goes."""
    _build()
    pd = world.pd
    pd.status, pd.total_budget = "ended", "140000"
    pd.donors = [*pd.donors, "Japan"]
    pd.save()
    PartnerOrganization.objects.create(etl_id="3", name="Lebanese Red Cross", short_name="LRC")
    Map.objects.all().delete()
    world.pd_before = Decimal("100000")
    return world


def _changes(**filters):
    return {(c.op, c.kind, c.key): c for c in Change.objects.filter(**filters)}


# ---------------------------------------------------------------------------------- detection
def test_the_first_build_is_the_starting_point(world):
    run = _build()
    assert run.details["changes"] == 0 and not Change.objects.exists()


def test_a_later_build_records_what_is_new_changed_linked_and_gone(changed):
    world = changed
    world.pd.total_budget = "140000"
    run = _build()
    found = _changes()
    pd = found[("changed", "programme_document", str(world.pd.pk))]
    assert pd.fields["status"] == ["active", "ended"] and pd.notable
    assert pd.sections == [world.education.pk] and pd.run_id == run.pk
    linked = found[("linked", "programme_document", str(world.pd.pk))]
    assert linked.link["name"] == "Japan" and linked.notable
    assert sentence(linked) == f"{world.pd.number} Education support in the north is now funded by Japan"
    assert found[("added", "donor", "japan")].notable
    partner = next(c for (op, kind, _), c in found.items() if op == "added" and kind == "partner")
    assert partner.notable and partner.sections == []  # no programme document yet: no section
    gone = next(c for (op, kind, _), c in found.items() if op == "removed" and kind == "map")
    assert not gone.notable  # kept and searchable, not in the daily note
    assert run.details["notable_changes"] == sum(1 for c in found.values() if c.notable)


def test_small_moves_and_minor_things_are_kept_but_not_notable(world):
    world.pd.total_budget = "100000"
    world.pd.save()
    _build()
    world.pd.total_budget = "105000"  # 5%: not notable
    world.pd.save()
    DistrictLocation.objects.create(code="LB99", gov_code="LB1", name="Qobayat")
    _build()
    found = _changes()
    budget = found[("changed", "programme_document", str(world.pd.pk))]
    assert budget.fields == {"budget": [100000.0, 105000.0]} and not budget.notable
    assert not found[("added", "district", "qobayat")].notable


def test_figures_recorded_for_the_first_time_are_not_changes(world):
    _build()
    Entity.objects.update(snapshot={})  # as before the upgrade that started recording them
    run = _build()
    assert run.details["changes"] == 0


def test_a_source_that_fails_keeps_its_things_and_reports_nothing_gone(world, monkeypatch):
    _build()
    partners = Entity.objects.filter(kind="partner").count()

    def broken(c, names):
        raise RuntimeError("eTools table unreadable")

    monkeypatch.setattr(
        builders,
        "SOURCES",
        [(label, broken if label == "partners" else add) for label, add in builders.SOURCES],
    )
    run = _build()
    assert run.status == SyncRun.Status.PARTIAL and "partners" in run.details["failed_sources"]
    assert Entity.objects.filter(kind="partner").count() == partners
    assert Edge.objects.filter(relation="implemented_by").exists()
    assert not Change.objects.exists()


# ---------------------------------------------------------------------------- refresh on new data
@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr("neurodb.integrations.background.start_command", lambda *a: calls.append(a))
    return calls


def test_a_finished_sync_asks_for_a_rebuild_once_committed(
    db, started, settings, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        new_run(SyncRun.Job.ETOOLS_DATAMART, "", "test").finish(SyncRun.Status.SUCCEEDED)
        new_run(SyncRun.Job.ETOOLS, "", "test").finish(SyncRun.Status.FAILED)  # nothing new
        new_run(SyncRun.Job.KNOWLEDGE_HUB, "", "test").finish(SyncRun.Status.SUCCEEDED)  # its own run
        new_run(SyncRun.Job.WHATS_NEW, "", "test").finish(SyncRun.Status.SUCCEEDED)
    assert list(RefreshRequest.objects.values_list("reason", flat=True)) == ["eTools Datamart sync"]
    assert started == [("build_knowledge_hub", "--when-requested")]
    with django_capture_on_commit_callbacks(execute=True):  # a process is already waiting: no second one
        new_run(SyncRun.Job.LOCATIONS, "", "test").finish(SyncRun.Status.SUCCEEDED)
    assert RefreshRequest.objects.count() == 2 and len(started) == 1
    RefreshRequest.objects.update(requested_at=timezone.now() - datetime.timedelta(minutes=20))
    with django_capture_on_commit_callbacks(execute=True):  # ...unless it has waited too long
        new_run(SyncRun.Job.LOCATIONS, "", "test").finish(SyncRun.Status.SUCCEEDED)
    assert len(started) == 2
    RefreshRequest.objects.all().delete()
    settings.KNOWLEDGE_HUB_ON_NEW_DATA = False
    with django_capture_on_commit_callbacks(execute=True):
        new_run(SyncRun.Job.ETOOLS_DATAMART, "", "test").finish(SyncRun.Status.SUCCEEDED)
    assert not RefreshRequest.objects.exists() and len(started) == 2


def test_a_burst_of_syncs_makes_one_rebuild_naming_them(world):
    for reason in ("eTools Datamart sync", "Locations sync", "eTools Datamart sync"):
        RefreshRequest.objects.create(reason=reason)
    runs = refresh.drain(settle=0)
    assert len(runs) == 1 and runs[0].triggered_by == "new data: Locations sync, eTools Datamart sync"
    assert not RefreshRequest.objects.exists()
    assert refresh.drain(settle=0) == []


def test_a_build_running_elsewhere_takes_the_requests(world):
    RefreshRequest.objects.create(reason="Locations sync")
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [refresh.LOCK_ID])
        assert refresh.drain(settle=0) == []
        assert RefreshRequest.objects.count() == 1
    finally:
        other.close()  # ends its session: the lock goes with it


# ---------------------------------------------------------------------------------- assistant
def test_the_assistant_tells_what_is_new_about_something_or_a_section(changed):
    _build()
    about = tools.run("whats_new", tools.validate("whats_new", {"about": "AMEL"}))
    says = [c["says"] for c in about["changes"]]
    assert any("now funded by Japan" in s for s in says) and not any("Red Cross" in s for s in says)
    pd_change = next(c for c in about["changes"] if c["what"] == "Changed")
    assert pd_change["fields"]["status"] == {"from": "active", "to": "ended"}
    assert pd_change["lookup"]["tool"] == "programme_details"
    section = tools.run("whats_new", {"section": "Education"})
    assert {c["kind"] for c in section["changes"]} == {"Programme document", "Donor"}  # through the PD
    everything = tools.run("whats_new", {"include_minor": True})
    assert any(c.get("minor") for c in everything["changes"])
    with pytest.raises(ToolInputError):
        tools.run("whats_new", {"since": "last week"})
    with pytest.raises(ToolInputError):
        tools.run("whats_new", {"section": "Astronomy"})


# ------------------------------------------------------------------------------- page and card
def test_the_page_shows_the_changes_of_the_users_section_first(client, changed, roles):
    _build()
    user = User.objects.create_user(username="edu", email="edu@example.org", password="edu-pass-12345678")
    user.section = changed.education
    user.save()
    client.force_login(user)
    page = client.get(reverse("graph:whats_new")).content.decode()
    assert "now funded by Japan" in page and "Lebanese Red Cross" not in page
    every = client.get(reverse("graph:whats_new"), {"section": "", "all": "1"}).content.decode()
    assert "Lebanese Red Cross" in every and "Schools map" in every
    assert "Email me the daily note" not in every  # no email configured


def test_the_overview_card_lists_the_latest_notable_changes(client_viewer, changed):
    _build()
    page = client_viewer.get(reverse("reports:overview")).content.decode()
    assert 'id="whats-new-title"' in page and "now funded by Japan" in page


# ---------------------------------------------------------------------------------- daily note
@pytest.fixture
def people(db, changed):
    def person(name, section=None, email=True):
        user = User.objects.create_user(username=name, email=f"{name}@example.org", password="x-pass-1234567")
        user.section = section
        user.save()
        if email is not None:
            DigestSubscription.objects.create(user=user, email=email)
        return user

    return SimpleNamespace(
        edu=person("edu", changed.education),
        nosection=person("nosection"),
        stopped=person("stopped", changed.education, email=False),
        never=person("never", changed.education, email=None),
    )


def test_the_daily_note_is_written_per_section_and_emailed_to_who_asked(
    people, changed, settings, mailoutbox
):
    settings.DIGEST_EMAIL_ENABLED, settings.SITE_URL, settings.AI_ASSISTANT_ENABLED = (
        True,
        "https://n.example",
        False,
    )
    _build()
    run = write_notes("test")
    assert run.status == SyncRun.Status.SUCCEEDED
    notes = {d.section_name: d for d in Digest.objects.all()}
    assert set(notes) == {"", "Education"} and notes["Education"].written_by == digest.TEMPLATE
    assert "now funded by Japan" in notes["Education"].text and "Red Cross" not in notes["Education"].text
    assert "Red Cross" in notes[""].text
    sent = {m.to[0]: m for m in mailoutbox}
    assert set(sent) == {"edu@example.org", "nosection@example.org"}
    assert (
        "Education" in sent["edu@example.org"].subject
        and "https://n.example/whats-new/" in sent["edu@example.org"].body
    )
    assert notes["Education"].emailed_to == 1
    assert (
        write_notes("test").status == SyncRun.Status.SUCCEEDED and Digest.objects.count() == 2
    )  # same day: rewritten


def test_no_notable_change_no_note(world):
    _build()
    run = write_notes("test")
    assert run.rows_written == 0 and not Digest.objects.exists()


class FakeResponses:
    def __init__(self, text="", error=None):
        self.text, self.error, self.calls = text, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.text)


def test_the_assistant_writes_the_note_from_the_changes_only(changed, settings, monkeypatch):
    settings.AI_ASSISTANT_ENABLED = True
    fake = FakeResponses("One programme document ended and is now funded by Japan.")
    monkeypatch.setattr("neurodb.assistant.agent.client", lambda: SimpleNamespace(responses=fake))
    _build()
    notes = digest.write()
    assert (
        notes[0].text.startswith("One programme document")
        and notes[0].written_by == settings.AI_ASSISTANT_MODEL
    )
    call = fake.calls[0]
    assert call["store"] is False and "now funded by Japan" in call["input"]
    fake.error = RuntimeError("down")
    notes = digest.write()
    assert all(n.written_by == digest.TEMPLATE for n in notes)


def test_people_ask_for_the_email_and_stop_it(client, viewer, settings):
    settings.DIGEST_EMAIL_ENABLED = True
    client.force_login(viewer)
    assert "Email me the daily note" in client.get(reverse("graph:whats_new")).content.decode()
    client.post(reverse("graph:email"), {"email": "1"})
    assert DigestSubscription.objects.get(user=viewer).email
    assert "Stop the email" in client.get(reverse("graph:whats_new")).content.decode()
    client.post(reverse("graph:email"), {"email": "0"})
    assert not DigestSubscription.objects.get(user=viewer).email


def test_donor_accounts_never_get_the_note(people, changed, settings, mailoutbox):
    from neurodb.donors.models import DonorAccount

    DonorAccount.objects.create(user=people.edu, name="EU", donors=["EU"], must_change_password=False)
    settings.DIGEST_EMAIL_ENABLED, settings.AI_ASSISTANT_ENABLED = True, False
    _build()
    write_notes("test")
    assert "edu@example.org" not in {m.to[0] for m in mailoutbox}


def test_notes_and_changes_are_dated(changed):
    _build()
    assert Change.objects.filter(detected_at__date=datetime.date.today()).exists()
