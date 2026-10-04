"""The morning email of NeuroDB Watch: one plain-text email per person and day, once email is set up.

**Dormant** until NeuroDB can send email (``EMAIL_URL`` set, which turns on ``DIGEST_EMAIL_ENABLED``),
with the watch on (``WATCH_ENABLED``) and its email on (``WATCH_EMAIL``, on by default): :func:`enabled`.
Until then nothing is sent and nothing fails; the morning pass records why (``details["emails"]``).

**Who** (:func:`recipients`): the people who switched the email on, on the What's new page
(``DigestSubscription.email``): active users with an email address, never a donor account. It takes
the place of the What's new email: while it is on and the morning pass is scheduled,
``neurodb.graph.digest.send`` steps aside (:func:`carries_whats_new`), so people get one email, not two.

**What** (:func:`compose`), plain text written by code only:

- "Needs you today": up to 5 of the points the person was told today ("Needs you", still open and not
  put aside by them since: Done, Not mine, Something's wrong or Remind me later), critical first, then
  the soonest due, each with its due date and why it is there;
- how many other things going to them are due in the next 30 days ("Coming up" on the page);
- today's What's new note of their section, or the note for everyone when their section has none;
- the link to For you (``SITE_URL`` + /for-you/) and how to stop the email.

It is sent even when nothing needs the person, as long as there is a What's new note; with neither,
there is no email that day. It never holds a point's detail or evidence, an assignment's owner or
note, a comment left with a reaction, child-level data or a figure marked for internal use: only the
points' titles, due dates and counts, and the What's new note.

**Once.** A ``WatchDelivery`` row (person, day, "email_daily") is written just before the email is
sent and stamped when it went, so a morning pass run again the same day sends nothing new. An email
that failed (its error is kept on the row) is tried again by the next morning pass of the same day;
one whose sending was cut off is not sent again (at most once). The receipts listed get
``emailed_on``.

Called only by the morning pass (:func:`send_daily`, the "email" step of
:mod:`neurodb.watch.services`); the quick pass after new data never emails. Writes only
``WatchDelivery`` and ``WatchReceipt.emailed_on``.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.core.models import ScheduledJob
from neurodb.graph import digest as whats_new
from neurodb.graph.models import Digest
from neurodb.integrations.runs import describe_error

from . import routing
from .detectors import CRITICAL, TRIAL
from .models import WatchDelivery, WatchItem, WatchReceipt

logger = logging.getLogger(__name__)

CHANNEL = WatchDelivery.Channel.EMAIL_DAILY
LISTED = 5  # "Needs you" titles in one email; the rest is on the page
COMING_UP_DAYS = 30
FOR_YOU_PATH = "/for-you/"  # the page's address while its URL is not installed
MORNING_COMMAND = "watch"  # the scheduled job of the morning pass (the same as services.MORNING_COMMAND)
# A person's own reactions that put a point aside for them
PUT_ASIDE = (WatchReceipt.Reaction.DONE, WatchReceipt.Reaction.NOT_MINE, WatchReceipt.Reaction.WRONG)

# Why no email went out at all (details["emails"]["note"])
NO_EMAIL = "email is not set up (EMAIL_URL)"
WATCH_OFF = "NeuroDB Watch is switched off (WATCH_ENABLED)"
EMAIL_OFF = "the morning email is switched off (WATCH_EMAIL)"


# ---------------------------------------------------------------------------- switched on
def off_reason() -> str:
    """Why the morning email is not sent (empty when it is on)."""
    if not getattr(settings, "DIGEST_EMAIL_ENABLED", False):
        return NO_EMAIL
    if not getattr(settings, "WATCH_ENABLED", False):
        return WATCH_OFF
    if not getattr(settings, "WATCH_EMAIL", False):
        return EMAIL_OFF
    return ""


def enabled() -> bool:
    """NeuroDB can send email, the watch is on and its morning email is on."""
    return not off_reason()


def carries_whats_new() -> bool:
    """The morning email carries the What's new note: it is on and the morning pass is scheduled (an
    enabled scheduled job "watch"). The What's new email then steps aside; when an administrator
    pauses the morning pass, the What's new email goes out again, so the note is never lost."""
    return enabled() and ScheduledJob.objects.filter(command=MORNING_COMMAND, enabled=True).exists()


# ---------------------------------------------------------------------------- who and where
def recipients() -> QuerySet:
    """The people who asked for the email: active, with an email address, never a donor account."""
    return (
        get_user_model()
        .objects.filter(is_active=True, donor_account__isnull=True, digest__email=True)
        .exclude(email="")
        .order_by("pk")
    )


def for_you_url() -> str:
    """The For you page's full address (``SITE_URL`` + its path), or empty without ``SITE_URL``."""
    if not settings.SITE_URL:
        return ""
    try:
        path = reverse("watch:for_you")
    except NoReverseMatch:
        path = FOR_YOU_PATH
    return f"{settings.SITE_URL}{path}"


def whats_new_url() -> str:
    """The What's new page's full address, where the email is stopped; empty without ``SITE_URL``."""
    return f"{settings.SITE_URL}{reverse('graph:whats_new')}" if settings.SITE_URL else ""


# ---------------------------------------------------------------------------- what it says
@dataclass
class Morning:
    """One person's morning email, and the receipts it lists."""

    subject: str
    body: str
    receipt_ids: list[int] = field(default_factory=list)


@dataclass
class _Today:
    """What every email of one morning reads once: who gets which open point due soon, the checks'
    settings and today's What's new notes."""

    day: datetime.date
    rules: routing.Routing
    due_soon: list[tuple[WatchItem, set[int]]]  # open points due in the next 30 days, with who gets them
    notes: dict[int | None, Digest]  # section id (None: everyone) -> today's What's new note

    @classmethod
    def load(cls, day: datetime.date) -> _Today:
        rules = routing.Routing.load(day)
        due = WatchItem.objects.filter(
            state=WatchItem.State.OPEN,
            due_date__gte=day,
            due_date__lte=day + datetime.timedelta(days=COMING_UP_DAYS),
        ).order_by("due_date", "pk")
        return cls(
            day=day,
            rules=rules,
            due_soon=[(item, set(rules.recipients(item))) for item in due],
            notes={note.section_id: note for note in Digest.objects.filter(date=day)},
        )

    def note_for(self, user) -> Digest | None:
        """Their section's What's new note today, else the note for everyone."""
        return self.notes.get(user.section_id) or self.notes.get(None)


def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def _rank(receipt: WatchReceipt) -> tuple:
    """Critical first, then the soonest due."""
    item = receipt.item
    return (item.severity != CRITICAL, item.due_date is None, item.due_date or datetime.date.max, item.pk)


def put_aside(receipt: WatchReceipt, day: datetime.date) -> bool:
    """The person took the point off their list: Done, Not mine or Something's wrong since they were
    last told about it (a point told again after that, e.g. "still open after you marked it done",
    is back), or Remind me later for a day after ``day``. The same rule as the For you page."""
    if receipt.snoozed_until and receipt.snoozed_until > day:
        return True
    return (
        receipt.reaction in PUT_ASIDE
        and receipt.reacted_at is not None
        and timezone.localdate(receipt.reacted_at) >= receipt.last_told_on
    )


def needs_you(user, day: datetime.date) -> list[WatchReceipt]:
    """The person's "Needs you" receipts told on ``day`` whose points are still open and that they did
    not put aside since, best first."""
    receipts = routing.needs_you_today(user, day).filter(item__state=WatchItem.State.OPEN)
    return sorted((r for r in receipts if not put_aside(r, day)), key=_rank)


def coming_up(user, today: _Today, leave_out: Collection[int] = ()) -> list[WatchItem]:
    """The open points going to the person that are due in the next 30 days, soonest first, without
    those in ``leave_out`` (item ids) and those the person put aside."""
    mine = [item for item, people in today.due_soon if user.pk in people and item.pk not in leave_out]
    if not mine:
        return []
    receipts = WatchReceipt.objects.filter(user=user, item__in=mine).exclude(
        reaction=WatchReceipt.Reaction.NONE, snoozed_until__isnull=True
    )
    aside = {r.item_id for r in receipts if put_aside(r, today.day)}
    return [item for item in mine if item.pk not in aside]


def _line(receipt: WatchReceipt, today: _Today) -> str:
    """One "Needs you" line: the title, its due date when the title does not give it, why it is there
    and whether its check is still on trial."""
    item = receipt.item
    text = f"- {item.title}"
    if item.due_date and _day(item.due_date) not in item.title:
        text += f" (due {_day(item.due_date)})"
    why = routing.reason(receipt, item)
    if why:
        text += f" · {why}"
    if today.rules.mode(item) == TRIAL:
        text += " · trial check"
    return text


def _things(n: int) -> str:
    return f"{n} thing{'s' if n != 1 else ''}"


def compose(user, today: _Today) -> Morning | None:
    """The person's morning email (see the module's notes), or None when there is nothing to say: no
    point needs them today and there is no What's new note."""
    receipts = needs_you(user, today.day)
    note = today.note_for(user)
    if not receipts and note is None:
        return None
    later = coming_up(user, today, {r.item_id for r in receipts})
    when = _day(today.day)
    if receipts:
        subject = f"NeuroDB: {_things(len(receipts))} for you today, {when}"
    else:
        subject = f"NeuroDB: what's new today, {when}"

    parts = []
    if receipts:
        lines = [f"Needs you today ({len(receipts)}):", *(_line(r, today) for r in receipts[:LISTED])]
        if len(receipts) > LISTED:
            lines.append(f"And {len(receipts) - LISTED} more on For you.")
        parts.append("\n".join(lines))
    else:
        parts.append("Nothing needs you today.")
    if later:
        parts.append(
            f"Also due in the next {COMING_UP_DAYS} days: {_things(len(later))} (Coming up on For you)."
        )
    if note is not None:
        audience = note.section_name or "every section"
        by_ai = note.written_by != whats_new.TEMPLATE
        label = " (written by AI from the changes NeuroDB noticed)" if by_ai else ""
        parts.append(f"What's new for {audience}{label}:\n{note.text.strip()}")
    link, stop = for_you_url(), whats_new_url()
    parts.append(
        f"See everything, and say whether it helped, on For you{f': {link}' if link else ' in NeuroDB.'}"
    )
    parts.append(
        "You get this email because you asked for NeuroDB's morning note on the What's new page. "
        f"To stop it, choose “Stop the email” there{f': {stop}' if stop else '.'}"
    )
    listed = [r.pk for r in receipts[:LISTED]]
    return Morning(subject=subject, body="\n\n".join(parts) + "\n", receipt_ids=listed)


# ---------------------------------------------------------------------------- sending, once
def _claim(user, day: datetime.date, row: WatchDelivery | None) -> WatchDelivery | None:
    """The person's delivery row for the day, claimed for this email: a new row, or the row of an
    email that failed; None when another run claimed it meanwhile."""
    if row is not None:
        row.error = ""
        row.save(update_fields=["error"])
        return row
    try:
        with transaction.atomic():
            return WatchDelivery.objects.create(user=user, date=day, channel=CHANNEL)
    except IntegrityError:
        return None


def send_daily(day: datetime.date | None = None) -> dict[str, Any]:
    """Send the morning email of ``day`` (default: today, Beirut time) to everyone who asked for it,
    once per person (see the module's notes). Returns the counts for the run's details: ``sent``,
    ``failed``, ``already_sent``, ``nothing_to_say`` and ``recipients``; with ``note`` when the email
    is off."""
    day = day or timezone.localdate()
    why_not = off_reason()
    if why_not:
        return {"sent": 0, "note": why_not}
    counts = Counter(sent=0, failed=0, already_sent=0, nothing_to_say=0)
    people = list(recipients())
    counts["recipients"] = len(people)
    if not people:
        return dict(counts)
    rows = {row.user_id: row for row in WatchDelivery.objects.filter(date=day, channel=CHANNEL)}
    today = _Today.load(day)
    for user in people:
        row = rows.get(user.pk)
        if row is not None and (row.sent_at is not None or not row.error):
            counts["already_sent"] += 1  # sent, or its sending was cut off: at most once
            continue
        morning = compose(user, today)
        if morning is None:
            counts["nothing_to_say"] += 1
            continue
        delivery = _claim(user, day, row)
        if delivery is None:
            counts["already_sent"] += 1
            continue
        try:
            send_mail(morning.subject, morning.body, settings.DEFAULT_FROM_EMAIL, [user.email])
        except Exception as exc:
            logger.exception("NeuroDB Watch: could not send the morning email to one person")
            delivery.error = describe_error(exc)[:500]
            delivery.save(update_fields=["error"])
            counts["failed"] += 1
            continue
        delivery.sent_at = timezone.now()
        delivery.save(update_fields=["sent_at"])
        WatchReceipt.objects.filter(pk__in=morning.receipt_ids).update(emailed_on=day)
        counts["sent"] += 1
    return dict(counts)
