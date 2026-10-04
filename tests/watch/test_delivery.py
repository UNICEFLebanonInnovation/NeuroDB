"""NeuroDB Watch's morning email: one plain-text email per person and day once email is set up, what it
holds and never holds, sent once, never by the quick pass, and the What's new email stepping aside."""

import datetime
import re
import smtplib

import pytest
from django.contrib.auth.models import Group
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, VIEWER
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.graph import digest
from neurodb.graph.management.commands.whats_new_digest import run as write_notes
from neurodb.graph.models import Digest, DigestSubscription
from neurodb.review.models import FindingAssignment
from neurodb.watch import delivery, services
from neurodb.watch.models import (
    DetectorSetting,
    WatchDelivery,
    WatchItem,
    WatchReceipt,
    WatchRequest,
    WatchState,
)

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut, after the 07:45 morning pass
TODAY = datetime.date(2026, 10, 5)
SITE = "https://n.example"
WRITE = re.compile(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"(\w+)"', re.I)


@pytest.fixture
def email_on(settings, roles):
    """Email set up (EMAIL_URL given), the watch and its morning email on, the locmem backend."""
    settings.DIGEST_EMAIL_ENABLED = True
    settings.WATCH_ENABLED = True
    settings.WATCH_EMAIL = True
    settings.SITE_URL = SITE
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    return settings


@pytest.fixture
def cp(db):
    return Section.objects.create(name="Child Protection", code="CP")


def person(username, role=VIEWER, section=None, *, subscribed=True, email=None):
    """A user with a role; ``subscribed`` True or False: the What's new email switch, None: never set."""
    address = f"{username}@example.org" if email is None else email
    user = User.objects.create_user(username=username, email=address, section=section)
    user.groups.add(Group.objects.get(name=role))
    if subscribed is not None:
        DigestSubscription.objects.create(user=user, email=subscribed)
    return user


def item(key, title, *, due=None, severity="warning", sections=(), detector="report_due_soon", **extra):
    return WatchItem.objects.create(
        key=key,
        detector=detector,
        kind=WatchItem.Kind.DEADLINE,
        severity=severity,
        title=title,
        due_date=due,
        section_ids=list(sections),
        first_seen_on=TODAY,
        last_seen_on=TODAY,
        changed_on=TODAY,
        evidence={"source": "eTools", "records": [{"label": key, "date": "", "value": "", "url": ""}]},
        **extra,
    )


def told(user, point, step="new", level=WatchReceipt.Level.NEEDS_YOU, on=TODAY, **extra):
    return WatchReceipt.objects.create(
        user=user, item=point, first_told_on=on, last_told_on=on, told_step=step, level=level, **extra
    )


def note(section=None, text="Two programme documents ended.", written_by=digest.TEMPLATE, on=TODAY):
    return Digest.objects.create(
        date=on,
        section_id=section.pk if section else None,
        section_name=section.name if section else "",
        text=text,
        written_by=written_by,
    )


def in_days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


def sent_to(address: str):
    found = [m for m in mail.outbox if m.to == [address]]
    assert len(found) == 1, [m.to for m in mail.outbox]
    return found[0]


# ---------------------------------------------------------------------------- what it says
def test_the_email_lists_what_needs_you_what_is_coming_and_the_note_of_the_section(email_on, cp):
    DetectorSetting.objects.create(detector="report_due_soon", mode=DetectorSetting.Mode.ON)
    val = person("val", section=cp)
    critical = item("review:late", "Reports overdue for 3 PDs", severity="critical", sections=[cp.pk])
    dated = item("due:report:A:1", "Progress report due 7 Oct 2026: PD A", due=in_days(2), sections=[cp.pk])
    undated_title = item("due:fr:F1", "Funds reservation F1 expires with money left", due=in_days(5))
    receipts = [told(val, critical), told(val, dated, step="3"), told(val, undated_title)]
    receipts += [
        told(val, item(f"due:pd_end:P{n}", f"PD P{n} ends", due=in_days(20 + n), sections=[cp.pk]))
        for n in range(3)
    ]  # 6 things: the 6th is left for the page
    item("due:grant:G1", "Grant G1 expires", due=in_days(10), sections=[cp.pk])  # coming up
    item("due:grant:G2", "Grant G2 expires", due=in_days(30), sections=[cp.pk])  # coming up, the last day
    item("due:grant:G3", "Grant G3 expires", due=in_days(31), sections=[cp.pk])  # too far ahead
    other = Section.objects.create(name="Education", code="EDU")
    item("due:grant:G4", "Grant G4 expires", due=in_days(9), sections=[other.pk])  # not their section
    done = item("due:grant:G5", "Grant G5 expires", due=in_days(9), sections=[cp.pk])
    told(
        val, done, level=WatchReceipt.Level.GOOD_TO_KNOW, reaction=WatchReceipt.Reaction.DONE, reacted_at=NOW
    )
    note(text="The note for everyone.")
    note(cp, "One PD of Child Protection ended.", written_by="the-model")

    counts = delivery.send_daily(TODAY)

    assert counts == {
        "sent": 1,
        "failed": 0,
        "already_sent": 0,
        "nothing_to_say": 0,
        "recipients": 1,
    }
    message = sent_to("val@example.org")
    assert message.subject == "NeuroDB: 6 things for you today, 5 Oct 2026"
    body = message.body
    lines = body.splitlines()
    assert lines[0] == "Needs you today (6):"
    assert lines[1] == "- Reports overdue for 3 PDs · New"  # critical first
    assert lines[2] == "- Progress report due 7 Oct 2026: PD A · Due in 3 days"  # the date given once
    assert lines[3] == "- Funds reservation F1 expires with money left (due 10 Oct 2026) · New"
    assert "PD P2 ends" not in body and "And 1 more on For you." in body
    assert "Also due in the next 30 days: 2 things (Coming up on For you)." in body
    assert (
        "What's new for Child Protection (written by AI from the changes NeuroDB noticed):\n"
        "One PD of Child Protection ended." in body
    )
    assert "The note for everyone." not in body
    assert f"on For you: {delivery.for_you_url()}" in body and delivery.for_you_url().startswith(SITE)
    assert delivery.for_you_url().endswith("/for-you/")
    assert f"choose “Stop the email” there: {SITE}/whats-new/" in body
    emailed = set(WatchReceipt.objects.filter(emailed_on=TODAY).values_list("item__key", flat=True))
    assert emailed == {r.item.key for r in receipts[:5]}  # the five listed


def test_with_no_section_note_it_carries_the_note_for_everyone_even_when_nothing_needs_them(email_on, cp):
    nosection = person("nosection")
    other = person("other", section=cp)
    note(text="Three partners joined.")
    delivery.send_daily(TODAY)
    for user in (nosection, other):
        message = sent_to(user.email)
        assert message.subject == "NeuroDB: what's new today, 5 Oct 2026"
        assert message.body.startswith("Nothing needs you today.\n\nWhat's new for every section:\n")
        assert "Three partners joined." in message.body and "written by AI" not in message.body


def test_nothing_needs_them_and_no_note_no_email(email_on, cp):
    val = person("val", section=cp)
    told(val, item("due:fr:F2", "FR F2 expires", due=in_days(3)), on=TODAY - datetime.timedelta(days=1))
    note(text="Yesterday's note.", on=TODAY - datetime.timedelta(days=1))
    counts = delivery.send_daily(TODAY)
    assert counts["sent"] == 0 and counts["nothing_to_say"] == 1
    assert mail.outbox == [] and not WatchDelivery.objects.exists()


def test_things_put_aside_or_closed_are_not_in_the_email(email_on, cp):
    val = person("val", section=cp)
    Reaction = WatchReceipt.Reaction
    told(val, item("due:fr:A", "FR A expires"), reaction=Reaction.DONE, reacted_at=NOW)
    told(val, item("due:fr:B", "FR B expires"), reaction=Reaction.NOT_MINE, reacted_at=NOW)
    told(val, item("due:fr:C", "FR C expires"), snoozed_until=in_days(3))
    told(val, item("due:fr:D", "FR D expires", state=WatchItem.State.CLOSED))
    told(val, item("due:fr:E", "FR E expires"))
    a_week_ago = NOW - datetime.timedelta(days=7)  # marked done, and told again today: still open
    told(
        val,
        item("due:fr:F", "FR F expires"),
        step="still_open",
        reaction=Reaction.DONE,
        reacted_at=a_week_ago,
    )
    delivery.send_daily(TODAY)
    body = sent_to("val@example.org").body
    assert "Needs you today (2):\n" in body
    assert "- FR E expires · New" in body and "- FR F expires · Still open after you marked it done" in body
    assert not any(f"FR {x} expires" in body for x in "ABCD")


def test_a_trial_check_is_marked_in_the_country_view(email_on):
    boss = person("boss", ADMIN)
    DetectorSetting.objects.create(detector="hact_gap", mode=DetectorSetting.Mode.TRIAL)
    point = item("hact:2026:7", "HACT assurance due by 15 Dec 2026 for Partner X", detector="hact_gap")
    point.scope = WatchItem.Scope.COUNTRY
    point.save()
    told(boss, point)
    delivery.send_daily(TODAY)
    assert "- HACT assurance due by 15 Dec 2026 for Partner X · New · trial check" in sent_to(boss.email).body


def test_the_body_never_holds_an_assignment_note_a_comment_or_a_figure_for_internal_use(email_on, cp):
    val = person("val", section=cp)
    FindingAssignment.objects.create(
        key="abc", title="Reports overdue", owner="Ms Rania Planted", note="planted assignment note"
    )
    point = item(
        "review:abc",
        "Reports overdue for 3 PDs",
        severity="critical",
        sections=[cp.pk],
        detail="planted detail text",
        has_owner=True,
        assignment_status="acknowledged",
        # a look-up that was kept: shown on the card as AI text, never emailed
        looked_up={
            "text": "planted look-up",
            "numbers": [],
            "tools": ["programme_details"],
            "at": "2026-10-05T07:50:00+03:00",
            "on": "2026-10-05",
            "kept": True,
            "reason": "",
        },
    )
    point.evidence = {**point.evidence, "numbers": {"internal_use_figure": 987654321}}
    point.save()
    told(val, point, comment="planted reviewer comment")
    from neurodb.watch import investigate

    assert investigate.shown(point)["text"] == "planted look-up"  # the page would show it
    delivery.send_daily(TODAY)
    message = sent_to("val@example.org")
    text = message.subject + message.body
    assert "Reports overdue for 3 PDs" in text
    for planted in (
        "Rania",
        "planted assignment note",
        "planted reviewer comment",
        "planted detail",
        "planted look-up",
        "987654321",
        "987,654,321",
    ):
        assert planted not in text


# ---------------------------------------------------------------------------- who gets it
def test_only_active_people_with_an_address_who_asked_get_it(email_on, cp):
    asked = person("asked", section=cp)
    person("stopped", section=cp, subscribed=False)
    person("never", section=cp, subscribed=None)
    person("noaddress", section=cp, email="")
    gone = person("gone", section=cp)
    gone.is_active = False
    gone.save()
    note(cp, "One PD ended.")
    assert list(delivery.recipients()) == [asked]
    delivery.send_daily(TODAY)
    assert [m.to for m in mail.outbox] == [["asked@example.org"]]


def test_donors_never_get_it(email_on, cp):
    from neurodb.donors.models import DonorAccount

    donor = person("donor", section=cp)
    DonorAccount.objects.create(user=donor, name="EU", donors=["EU"], must_change_password=False)
    told(donor, item("due:fr:F3", "FR F3 expires", sections=[cp.pk]))
    note(cp, "One PD ended.")
    note(text="Everyone's note.")
    counts = delivery.send_daily(TODAY)
    assert counts["recipients"] == 0 and mail.outbox == [] and not WatchDelivery.objects.exists()


# ---------------------------------------------------------------------------- dormant, once, morning only
def test_with_email_not_set_up_nothing_is_sent_and_nothing_fails(settings, roles, cp):
    settings.DIGEST_EMAIL_ENABLED = False  # EMAIL_URL empty
    settings.WATCH_ENABLED = True
    person("val", section=cp)
    note(text="Everyone's note.")
    assert delivery.send_daily(TODAY) == {"sent": 0, "note": delivery.NO_EMAIL}
    run = services.daily(now=NOW).runs[0]
    assert run.status == SyncRun.Status.SUCCEEDED and "email" not in run.details["step_errors"]
    assert run.details["emails"] == {"sent": 0, "note": delivery.NO_EMAIL}
    assert mail.outbox == [] and not WatchDelivery.objects.exists()
    assert not delivery.carries_whats_new()


def test_switched_off_it_says_why(email_on, cp):
    person("val", section=cp)
    note(text="Everyone's note.")
    email_on.WATCH_EMAIL = False
    assert delivery.send_daily(TODAY) == {"sent": 0, "note": delivery.EMAIL_OFF}
    email_on.WATCH_EMAIL, email_on.WATCH_ENABLED = True, False
    assert delivery.send_daily(TODAY) == {"sent": 0, "note": delivery.WATCH_OFF}
    assert mail.outbox == []


def test_two_morning_passes_send_one_email_and_write_only_the_watchs_tables(email_on, cp):
    val = person("val", section=cp)
    note(text="Three partners joined.")
    with CaptureQueriesContext(connection) as queries:
        first = services.daily(now=NOW).runs[0]
    written = {found[1] for query in queries.captured_queries if (found := WRITE.match(query["sql"]))}
    assert "watch_watchdelivery" in written
    assert all(t.startswith("watch_") or t in {"assistant_aiusage", "core_syncrun"} for t in written), written
    second = services.daily(now=NOW).runs[0]
    assert first.status == second.status == SyncRun.Status.SUCCEEDED
    assert first.details["emails"]["sent"] == 1
    assert second.details["emails"]["sent"] == 0 and second.details["emails"]["already_sent"] == 1
    assert [m.to for m in mail.outbox] == [["val@example.org"]]
    delivery_row = WatchDelivery.objects.get()
    assert (delivery_row.user, delivery_row.date, delivery_row.channel) == (val, TODAY, delivery.CHANNEL)
    assert delivery_row.sent_at is not None and delivery_row.error == ""


def test_a_quick_pass_sends_nothing(email_on, cp):
    val = person("val", section=cp)
    told(val, item("due:fr:F4", "FR F4 expires", due=in_days(1), severity="critical", sections=[cp.pk]))
    note(text="Three partners joined.")
    state = WatchState.get()
    state.last_daily_on = TODAY  # the morning pass ran: no catch-up
    state.save()
    WatchRequest.objects.create(reason="Knowledge hub")
    done = services.when_requested(now=NOW, settle=0)
    assert [run.target for run in done.runs] == ["quick"] and "emails" not in done.runs[0].details
    assert mail.outbox == [] and not WatchDelivery.objects.exists()


def test_a_failed_email_is_kept_and_sent_by_the_next_morning_pass_then_never_again(email_on, cp, monkeypatch):
    person("val", section=cp)
    note(text="Three partners joined.")

    def down(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(delivery, "send_mail", down)
    counts = delivery.send_daily(TODAY)
    assert counts["failed"] == 1 and counts["sent"] == 0
    row = WatchDelivery.objects.get()
    assert row.sent_at is None and row.error.startswith("SMTPServerDisconnected")
    monkeypatch.undo()
    assert delivery.send_daily(TODAY)["sent"] == 1
    assert delivery.send_daily(TODAY)["already_sent"] == 1
    row.refresh_from_db()
    assert len(mail.outbox) == 1 and row.sent_at is not None and row.error == ""


def test_an_email_cut_off_while_sending_is_not_sent_again(email_on, cp):
    val = person("val", section=cp)
    note(text="Three partners joined.")
    WatchDelivery.objects.create(user=val, date=TODAY)  # claimed, then the process died
    assert delivery.send_daily(TODAY)["already_sent"] == 1 and mail.outbox == []


# ---------------------------------------------------------------------------- What's new steps aside
@pytest.fixture
def todays_note(cp, monkeypatch):
    """Today's What's new notes, as whats_new_digest writes them."""
    notes = [note(text="Three partners joined."), note(cp, "One PD of Child Protection ended.")]
    monkeypatch.setattr(digest, "write", lambda *args, **kwargs: notes)
    return notes


def test_a_subscriber_gets_one_email_and_whats_new_sends_none_that_day(email_on, cp, todays_note):
    person("val", section=cp)
    assert delivery.carries_whats_new() and digest.carried_by_morning_email()
    run = write_notes("test")
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.details["emailed"] == 0 and run.details["email"] == digest.CARRIED
    assert mail.outbox == [] and all(d.emailed_to == 0 for d in Digest.objects.all())
    services.daily(now=NOW)
    message = sent_to("val@example.org")
    assert "One PD of Child Protection ended." in message.body and len(mail.outbox) == 1


@pytest.mark.parametrize("change", ["watch_email_off", "morning_pass_paused", "watch_off"])
def test_the_whats_new_email_goes_out_when_the_morning_email_does_not(email_on, cp, todays_note, change):
    person("val", section=cp)
    if change == "watch_email_off":
        email_on.WATCH_EMAIL = False
    elif change == "watch_off":
        email_on.WATCH_ENABLED = False
    else:
        ScheduledJob.objects.filter(command=delivery.MORNING_COMMAND).update(enabled=False)
    assert not delivery.carries_whats_new()
    run = write_notes("test")
    assert run.details["emailed"] == 1 and "email" not in run.details
    assert "what's new for Child Protection" in sent_to("val@example.org").subject


def test_the_morning_pass_is_the_job_whose_schedule_is_checked():
    assert delivery.MORNING_COMMAND == services.MORNING_COMMAND


# ---------------------------------------------------------------------------- decided per day
def test_a_morning_pass_that_ends_before_its_email_sends_the_whats_new_note_instead(
    email_on, cp, todays_note
):
    person("val", section=cp)
    assert write_notes("test").details["emailed"] == 0  # stepped aside for the morning email
    email_on.WATCH_TIME_LIMIT_SECONDS = 0  # the morning pass stops before its steps
    run = services.daily(now=NOW).runs[0]
    assert "email" in run.details["not_run"]
    message = sent_to("val@example.org")
    assert "what's new for Child Protection" in message.subject
    assert "One PD of Child Protection ended." in message.body
    assert run.details["emails"]["whats_new"] == 1
    # caught up later the same day: the morning email does not repeat the note (nothing else to say)
    email_on.WATCH_TIME_LIMIT_SECONDS = 900
    services.daily(now=NOW)
    assert len(mail.outbox) == 1
    assert WatchDelivery.objects.filter(channel=WatchDelivery.Channel.WHATS_NEW).count() == 1


def test_a_morning_pass_that_fails_still_sends_the_whats_new_note(email_on, cp, todays_note, monkeypatch):
    person("val", section=cp)
    write_notes("test")

    def broken(*args, **kwargs):
        raise RuntimeError("the runner broke")

    monkeypatch.setattr(services.Context, "make", broken)
    run = services.daily(now=NOW).runs[0]
    assert run.status == SyncRun.Status.FAILED
    assert "One PD of Child Protection ended." in sent_to("val@example.org").body


def test_a_whats_new_note_written_after_the_morning_email_goes_out_by_itself(email_on, cp, todays_note):
    person("val", section=cp)
    Digest.objects.all().delete()  # no note yet when the morning email goes
    services.daily(now=NOW)
    assert mail.outbox == []  # nothing to say: no point, no note
    assert delivery.carries_whats_new() and not delivery.carries_whats_new(TODAY)
    for found in todays_note:  # the What's new job ran late
        found.pk = None
        found.save()
    run = write_notes("test")
    assert run.details["emailed"] == 1 and "email" not in run.details
    assert "what's new for Child Protection" in sent_to("val@example.org").subject
