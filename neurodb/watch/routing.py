"""Who is told what, and when: NeuroDB Watch's routing and its record of what each person was told.

Pure code: the AI never picks who hears what.

**Who.** The people are the active users with a role (or superusers) and no donor account
(:func:`people`). Each person follows their own section; Administrators and members of the
Management group also get the **whole-country view** (:func:`audiences`). An item goes
(:func:`item_users`, :meth:`Routing.recipients`):

- to the staff of its sections (``WatchItem.section_ids``, from the confirmed eTools section names);
- also to the whole-country view when its check says so (``scope`` "country": grants, HACT
  assurance, agreed dates, PDs long ended with money outstanding);
- to the Administrators only when it is a system item or an administrators' item (``scope``
  "admins"), and also to them when one of its eTools section names has no confirmed NeuroDB section
  (never to every section, and not to the Management group: they get the country items, not every
  section's);
- to the whole-country view only while its check is in **trial**; nowhere while it is **off**;
- to the whole-country view too when a daily review finding is critical, open for 7 days and no one
  is assigned to it (only the Administrators can assign): "Open 7 days, no one assigned".

**When.** One ``WatchReceipt`` per person and item holds the last step told. :func:`announce` tells a
person again only when the step moves on:

- **new** to them. First sight is silent: an item that already existed before the person could first
  hear about it (the first run of a check, the day a check went on for staff or into trial, the
  person's first day, or an item first seen a week ago or more) is recorded as **known** and not
  announced, unless it is critical or due within 3 days;
- a **milestone** of its due date is crossed (14, 7, 3, 1, 0 days...), at most 3 per item and person;
- its due date **passed** while it is still open (once);
- it got **worse** (its severity rose above the one they were last told, kept on their receipt);
- the week-old **escalation** above, for the whole-country view;
- their **snooze** ended while it is still open ("Remind me later");
- 7 days after they marked it **Done**, it is still open ("still open after you marked it done"),
  once, then never again;
- it **closed** or went, and they had been told about it (good to know), as it ended: "resolved" (done,
  or no longer seen), "missed" (its date passed and it was not done: never told as resolved) or
  "closed" (no longer followed here: its date moved, another point follows it).

**Reactions.** "Not mine" and the person's own "Something's wrong" hide the item from them unless it
becomes critical. "Done" hides it from them, and for the whole section when a Section editor of that
section or an Administrator marked it. A Section editor's "Something's wrong" hides it for their own
section until its evidence changes (:func:`hidden_sections`); an Administrator's, or that of an editor
whose section is the item's only one, marks it wrong for everyone (state "wrong"), and it is then told
to no one. A snooze hides it until its day. Three "Not useful" on the same check within 30 days mute
that check's info and warning items for the person (critical items are still told).

**How much.** Warning and critical items are "Needs you"; info items and "resolved" are "Good to
know". Each person gets at most ``WATCH_NEEDS_YOU_PER_DAY`` (5) and ``WATCH_GOOD_TO_KNOW_PER_DAY``
(10) a day, ranked critical first, then the soonest due, then got worse, then new warnings. The rest
waits for the next pass and stays on the page meanwhile. The quick pass after new data tells only
critical items, items due within 3 days and system items, and only "Needs you".

Receipts older than 12 months are deleted (:func:`prune`). Nothing outside ``WatchReceipt`` is
written here, and of a receipt only what was told: never the reaction, comment or reminder people
write (a reminder that has done its work is cleared, and a point told again is marked unseen, by
their own updates, and a person who answered while a pass ran is not told over their answer).
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from neurodb.accounts.roles import ADMIN, ALL_ROLES, MANAGEMENT, SECTION_EDITOR
from neurodb.core.models import SyncRun
from neurodb.review.models import FindingAssignment

from . import detectors, sections
from .detectors import (
    ADMINS,
    COUNTRY,
    CRITICAL,
    INFO,
    OFF,
    ON,
    QUICK,
    SECTION,
    SEVERITY_RANK,
    SYSTEM,
    TRIAL,
    Context,
    milestone,
    milestones_of,
)
from .detectors.calendar import ASSIGNMENT_PREFIX
from .detectors.concerns import REVIEW_PREFIX
from .memory import SEVERITY_WORDS, Outcome, fingerprint
from .models import DetectorSetting, WatchItem, WatchNote, WatchReceipt

# What a receipt says was told (WatchReceipt.told_step; a milestone is its number of days, "7")
KNOWN, NEW, OVERDUE, WORSE = WatchReceipt.KNOWN, WatchReceipt.NEW, WatchReceipt.OVERDUE, WatchReceipt.WORSE
STILL_OPEN, RESOLVED = WatchReceipt.STILL_OPEN, WatchReceipt.RESOLVED
MISSED, CLOSED = WatchReceipt.MISSED, WatchReceipt.CLOSED  # its date passed, not done; no longer followed
ENDED = (RESOLVED, MISSED, CLOSED)  # the steps that tell an item closed or went
ESCALATED = "escalated"  # critical, open 7 days, no one assigned: the whole-country view
REMINDER = "reminder"  # the person's snooze ended while it is still open
NEEDS_YOU, GOOD_TO_KNOW = WatchReceipt.Level.NEEDS_YOU, WatchReceipt.Level.GOOD_TO_KNOW
Reaction = WatchReceipt.Reaction
State = WatchItem.State

# How a person gets an item (Routing.recipients); the first that applies is kept
STAFF = "section"  # they work in one of its sections
COUNTRY_VIEW = "country"  # the whole-country view: Administrators and the Management group
ADMINS_ONLY = "admins"  # system and administrators' items, and section names not confirmed
ESCALATION = "escalated"  # only because it is critical, a week old and no one is assigned

MAX_MILESTONES = 3  # milestone announcements per item and person
URGENT_DAYS = 3  # due within this many days: told at first sight, and in the quick pass
OLD_NEWS_DAYS = 7  # an item first seen this long ago is known to someone who first hears of it now
ESCALATE_AFTER_DAYS = 7
DONE_CHECK_DAYS = 7  # "Done", and still open this many days later: one reminder
MUTE_AFTER = 3  # "Not useful" on one check within MUTE_DAYS mutes its info and warning items
MUTE_DAYS = 30
RESOLVED_WITHIN_DAYS = 14  # items closed this recently are told as resolved to those told before
RETENTION_DAYS = 365  # receipts last told longer ago are deleted
OPEN_ASSIGNMENT = (FindingAssignment.Status.ACKNOWLEDGED, FindingAssignment.Status.ASSIGNED)
# The receipt fields a telling writes (never the reaction, comment, reminder, seen stamp or email date:
# those are people's; an ended reminder and the unseen mark are cleared by filtered updates, see _write)
TOLD_FIELDS = (
    "first_told_on",
    "last_told_on",
    "told_step",
    "told_severity",
    "milestone_count",
    "level",
)

# Ranks of the steps among tellings on the same day (lower first) when several apply at once
_PRECEDENCE = {OVERDUE: 0, "milestone": 1, WORSE: 2, ESCALATED: 3, REMINDER: 4}
# The story lines memory writes when an item's severity changes
_SEVERITY_CHANGE = re.compile(r"^(?:Got worse|Less urgent): was (?P<was>[a-z ]+), now (?P<now>[a-z ]+)$")
_SEVERITY_OF = {word: severity for severity, word in SEVERITY_WORDS.items()}


# ---------------------------------------------------------------------------- the people
@dataclass(frozen=True)
class Person:
    """Someone NeuroDB Watch may tell things to: their section, the day they joined and what their
    role and groups make them (never their name or email)."""

    id: int
    section_id: int | None
    joined: datetime.date
    admin: bool = False
    editor: bool = False
    management: bool = False

    @property
    def country(self) -> bool:
        """They get the whole-country view (Administrators and the Management group)."""
        return self.admin or self.management

    @property
    def audiences(self) -> list[str]:
        """The morning notes they read: their section's, and the whole country's."""
        keys = [WatchNote.section_audience(self.section_id)] if self.section_id else []
        return keys + ([WatchNote.COUNTRY] if self.country else [])

    @classmethod
    def of(cls, user) -> Person | None:
        """The user as a person the watch tells things to; None for a donor account, an inactive
        user or a user without a role."""
        if user is None or not getattr(user, "pk", None):
            return None
        return people([user.pk]).get(user.pk)


def eligible_users() -> QuerySet:
    """The users who may be told anything: active, with a role or superuser, never a donor account."""
    return (
        get_user_model()
        .objects.filter(is_active=True, donor_account__isnull=True)
        .filter(Q(is_superuser=True) | Q(groups__name__in=ALL_ROLES))
        .distinct()
    )


def people(user_ids: Iterable[int] | None = None) -> dict[int, Person]:
    """Every person who may be told anything (see :func:`eligible_users`), by user id; with
    ``user_ids``, only those of them."""
    users = eligible_users()
    if user_ids is not None:
        users = users.filter(pk__in=list(user_ids))
    rows = list(users.order_by("pk").values_list("pk", "section_id", "date_joined", "is_superuser"))
    groups: dict[int, set[str]] = defaultdict(set)
    membership = get_user_model().groups.through.objects.filter(
        user_id__in=[row[0] for row in rows], group__name__in=(ADMIN, SECTION_EDITOR, MANAGEMENT)
    )
    for user_id, name in membership.values_list("user_id", "group__name"):
        groups[user_id].add(name)
    found = {}
    for pk, section_id, joined, superuser in rows:
        admin = bool(superuser) or ADMIN in groups[pk]
        found[pk] = Person(
            id=pk,
            section_id=section_id,
            joined=timezone.localdate(joined) if joined else datetime.date.min,
            admin=admin,
            editor=not admin and SECTION_EDITOR in groups[pk],
            management=MANAGEMENT in groups[pk],
        )
    return found


def section_of(audience: str) -> int | None:
    """The section id of a section audience ("section:12"), else None."""
    prefix = WatchNote.section_audience("")
    if audience.startswith(prefix) and audience[len(prefix) :].isdigit():
        return int(audience[len(prefix) :])
    return None


# ---------------------------------------------------------------------------- who gets an item
@dataclass
class Routing:
    """Who hears what in one pass: the people, the checks' settings and the confirmed section map,
    read once (:meth:`load`)."""

    today: datetime.date
    people: dict[int, Person]
    checks: dict[str, DetectorSetting]
    mapping: dict[str, int]
    staff: dict[int, list[int]] = field(init=False, repr=False)
    country: list[int] = field(init=False, repr=False)
    admins: list[int] = field(init=False, repr=False)

    def __post_init__(self):
        staff: dict[int, list[int]] = defaultdict(list)
        for person in self.people.values():
            if person.section_id:
                staff[person.section_id].append(person.id)
        self.staff = dict(staff)
        self.country = [p.id for p in self.people.values() if p.country]
        self.admins = [p.id for p in self.people.values() if p.admin]

    @classmethod
    def load(cls, today: datetime.date | None = None) -> Routing:
        return cls(
            today=today or timezone.localdate(),
            people=people(),
            checks={s.detector: s for s in DetectorSetting.objects.all()},
            mapping=sections.confirmed_map(),
        )

    # ------------------------------------------------------------------ the item's check
    def mode(self, item: WatchItem) -> str:
        """Its check's setting: trial, on or off (a check never run yet: its default)."""
        setting = self.checks.get(item.detector)
        if setting is not None:
            return setting.mode
        check = detectors.get(item.detector)
        return check.default_mode if check else TRIAL

    def since(self, item: WatchItem) -> datetime.date:
        """The day the people who get the item now could first hear of its check's items: the day it
        went on for staff (``on_since``), or the day it went into trial (``trial_since``: its first
        run, an administrator's change or going back by itself; never a save that keeps it in trial).
        Items found by then were there already."""
        setting = self.checks.get(item.detector)
        if setting is None:
            return self.today
        if setting.mode == ON:
            return setting.on_since or self.today
        return setting.trial_since or self.today

    # ------------------------------------------------------------------ the rules
    @staticmethod
    def admin_only(item: WatchItem) -> bool:
        """A system item, or one of the administrators' own (donor accounts, the year rollover)."""
        return item.kind == SYSTEM or item.scope == ADMINS

    def unmapped(self, item: WatchItem) -> bool:
        """One of its eTools section names has no confirmed NeuroDB section, or a section item has no
        section at all: its section staff cannot be found, so the Administrators get it."""
        if self.admin_only(item):
            return False
        if sections.resolve_names(item.etools_sections, self.mapping)[1]:
            return True
        return item.scope == SECTION and not item.section_ids

    @staticmethod
    def escalation_day(item: WatchItem) -> datetime.date:
        return item.first_seen_on + datetime.timedelta(days=ESCALATE_AFTER_DAYS)

    def escalated(self, item: WatchItem) -> bool:
        """A daily review finding (only those can be assigned) that is critical, open for 7 days and
        has no open assignment: the whole-country view is told, since only Administrators assign."""
        return (
            item.state == State.OPEN
            and item.severity == CRITICAL
            and item.key.startswith(REVIEW_PREFIX)
            and item.assignment_status not in OPEN_ASSIGNMENT
            and self.today >= self.escalation_day(item)
        )

    def recipients(self, item: WatchItem) -> dict[int, str]:
        """user id -> how they get the item (``STAFF``, ``COUNTRY_VIEW``, ``ADMINS_ONLY`` or
        ``ESCALATION``; the first that applies). Empty while its check is off."""
        mode = self.mode(item)
        if mode == OFF:
            return {}
        found: dict[int, str] = {}

        def add(ids: Iterable[int], via: str) -> None:
            for pk in ids:
                found.setdefault(pk, via)

        if self.admin_only(item):
            add(self.admins, ADMINS_ONLY)
            return found
        if mode == TRIAL:
            add(self.country, COUNTRY_VIEW)  # a check on trial: the whole-country view only
            return found
        for section_id in item.section_ids:
            add(self.staff.get(section_id, ()), STAFF)
        if item.scope == COUNTRY:
            add(self.country, COUNTRY_VIEW)
        if self.unmapped(item):
            add(self.admins, ADMINS_ONLY)
        if self.escalated(item):
            add(self.country, ESCALATION)
        return found

    def visible(self, person: Person, item: WatchItem) -> bool:
        """The person gets the item (whatever its state)."""
        return person.id in self.recipients(item)

    def items_for(self, person: Person, items: Iterable[WatchItem]) -> list[WatchItem]:
        """The items, in their order, that go to the person and are not marked wrong."""
        return [i for i in items if i.state != State.WRONG and self.visible(person, i)]

    # ------------------------------------------------------------------ the morning notes
    def in_audience(self, item: WatchItem, audience: str) -> bool:
        """The item belongs in the audience's morning note: a section's items for its staff (checks
        that are on), or the whole-country view's own items (country items, checks on trial and
        escalations; not the administrators' own items, nor every section's)."""
        mode = self.mode(item)
        if mode == OFF or self.admin_only(item) or item.state == State.WRONG:
            return False
        if audience == WatchNote.COUNTRY:
            return mode == TRIAL or item.scope == COUNTRY or self.escalated(item)
        section_id = section_of(audience)
        return mode == ON and section_id is not None and section_id in item.section_ids

    def audience_items(self, audience: str, items: Iterable[WatchItem]) -> list[WatchItem]:
        """The items of one morning note's audience ('section:<id>' or 'country'), in their order."""
        return [item for item in items if self.in_audience(item, audience)]

    def section_counts(self, items: Iterable[WatchItem]) -> dict[int, dict[str, int]]:
        """Counts per NeuroDB section of the open items its staff get (checks that are on), for the
        whole-country view: open, critical, warning, due within 7 days and new today."""
        counts: dict[int, Counter] = defaultdict(Counter)
        for item in items:
            if item.state != State.OPEN or self.mode(item) != ON or self.admin_only(item):
                continue
            left = (item.due_date - self.today).days if item.due_date else None
            for section_id in item.section_ids:
                bucket = counts[section_id]
                bucket["open"] += 1
                bucket["critical"] += item.severity == CRITICAL
                bucket["warning"] += item.severity == detectors.WARNING
                bucket["due_within_7_days"] += left is not None and 0 <= left <= 7
                bucket["new_today"] += item.first_seen_on == self.today
        return {section_id: dict(bucket) for section_id, bucket in sorted(counts.items())}


def item_users(item: WatchItem, routing: Routing | None = None) -> dict[int, str]:
    """Who gets the item: user id -> how (see :meth:`Routing.recipients`)."""
    return (routing or Routing.load()).recipients(item)


def audiences(routing: Routing | None = None) -> dict[str, list[int]]:
    """Who reads each morning note: 'section:<id>' -> the staff of that section, and 'country' ->
    the Administrators and the Management group. A person without a section who is not in the
    whole-country view reads none."""
    routing = routing or Routing.load()
    found: dict[str, list[int]] = {}
    for section_id, ids in sorted(routing.staff.items()):
        found[WatchNote.section_audience(section_id)] = sorted(ids)
    if routing.country:
        found[WatchNote.COUNTRY] = sorted(routing.country)
    return found


def items_for(user, today: datetime.date | None = None) -> list[WatchItem]:
    """The open items that go to the user (the "Everything open" list); none for someone the watch
    tells nothing to (a donor account, no role)."""
    person = Person.of(user)
    if person is None:
        return []
    routing = Routing.load(today)
    return routing.items_for(person, WatchItem.objects.filter(state=State.OPEN).order_by("due_date", "pk"))


# ---------------------------------------------------------------------------- what to say
def urgent(item: WatchItem, today: datetime.date) -> bool:
    """Critical, or due within 3 days (not past): told even at first sight."""
    if item.severity == CRITICAL:
        return True
    return item.due_date is not None and 0 <= (item.due_date - today).days <= URGENT_DAYS


def quick_worthy(item: WatchItem, today: datetime.date) -> bool:
    """What the quick pass after new data looks at: critical items, items due within 3 days and
    system items."""
    return item.kind == SYSTEM or urgent(item, today)


def level_of(item: WatchItem, step: str) -> str:
    """Warning and critical items are "Needs you"; info items, known ones and those that closed or
    went are "Good to know"."""
    if step == KNOWN or step in ENDED or item.severity == INFO:
        return GOOD_TO_KNOW
    return NEEDS_YOU


def closing_step(item: WatchItem) -> str:
    """How a closed or gone item is told: missed (its date passed and it was not done), closed (no
    longer followed here) or resolved (done, or no longer seen)."""
    if item.state == State.CLOSED and item.close_kind == WatchItem.CloseKind.MISSED:
        return MISSED
    if item.state == State.CLOSED and item.close_kind == WatchItem.CloseKind.CHANGED:
        return CLOSED
    return RESOLVED


def told_severity(item: WatchItem, receipt: WatchReceipt) -> str:
    """The severity the person was last told (kept on the receipt; for a receipt written before it
    was kept, read from the item's story)."""
    return receipt.told_severity or severity_told(item, receipt.last_told_on)


def severity_told(item: WatchItem, since: datetime.date) -> str:
    """The item's severity on ``since`` (the day someone was last told): what its first severity
    change after that day says it was, else its severity now. (Only for receipts without
    ``told_severity``.)"""
    after = since.isoformat()
    for line in item.story or []:
        if not isinstance(line, dict) or str(line.get("on") or "") <= after:
            continue
        found = _SEVERITY_CHANGE.match(str(line.get("text") or ""))
        if found:
            return _SEVERITY_OF.get(found["was"], item.severity)
    return item.severity


def done_in_episode(item: WatchItem, receipt: WatchReceipt) -> bool:
    """The receipt's "Done" was given since the item last (re)appeared."""
    return (
        receipt.reaction == Reaction.DONE
        and receipt.reacted_at is not None
        and timezone.localdate(receipt.reacted_at) >= item.first_seen_on
    )


def reason(receipt: WatchReceipt, item: WatchItem | None = None) -> str:
    """The step told, in plain words for the page ("Due in 3 days", "Got worse"); empty when it was
    only recorded as known."""
    item = item or receipt.item
    step = receipt.told_step
    if step.isdigit():
        days = int(step)
        return "Due today" if days == 0 else "Due tomorrow" if days == 1 else f"Due in {days} days"
    if step == OVERDUE:
        return "Agreed date passed" if item.key.startswith(ASSIGNMENT_PREFIX) else "Date passed"
    if step == RESOLVED:
        return "No longer seen" if item.state == State.GONE else "Resolved"
    if step == MISSED:
        return "Date passed, not done"
    if step == CLOSED:
        return "No longer followed here"
    return {
        NEW: "New",
        WORSE: "Got worse",
        ESCALATED: f"Open {ESCALATE_AFTER_DAYS} days, no one assigned",
        STILL_OPEN: "Still open after you marked it done",
        REMINDER: "Reminder",
    }.get(step, "")


# ---------------------------------------------------------------------------- one pass
@dataclass
class Telling:
    """One thing to tell (or, for ``KNOWN``, to record) to one person about one item."""

    person: Person
    item: WatchItem
    step: str
    receipt: WatchReceipt | None
    fresh: bool  # no receipt yet for this episode of the item

    @property
    def level(self) -> str:
        return level_of(self.item, self.step)

    def rank(self) -> tuple:
        """Critical first, then the soonest due, then got worse, then new warnings."""
        item = self.item
        return (
            item.severity != CRITICAL,
            item.due_date is None,
            item.due_date or datetime.date.max,
            self.step != WORSE,
            not (self.step == NEW and item.severity == detectors.WARNING),
            -SEVERITY_RANK.get(item.severity, 0),
            item.pk,
        )


@dataclass
class Announced:
    """What one :func:`announce` did (for ``SyncRun.details["receipts"]``)."""

    mode: str
    people: int = 0
    steps: Counter = field(default_factory=Counter)
    needs_you: int = 0
    good_to_know: int = 0
    known: int = 0
    deferred: int = 0  # over the day's limits: left for a later pass
    created: int = 0
    updated: int = 0

    def details(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "people": self.people,
            "needs_you": self.needs_you,
            "good_to_know": self.good_to_know,
            "known": self.known,
            "deferred": self.deferred,
            "created": self.created,
            "updated": self.updated,
            "steps": dict(sorted(self.steps.items())),
        }


class _Pass:
    """The reading behind one announce: who gets what, what each was told, their reactions."""

    def __init__(self, ctx: Context, routing: Routing, items: list[WatchItem], worse: Iterable[str]):
        self.ctx = ctx
        self.today = ctx.today
        self.routing = routing
        self.worse_now = set(worse)
        ids = [item.pk for item in items]
        self.receipts: dict[tuple[int, int], WatchReceipt] = {}
        self.by_item: dict[int, list[WatchReceipt]] = defaultdict(list)
        for receipt in WatchReceipt.objects.filter(item_id__in=ids, user_id__in=list(routing.people)):
            self.receipts[(receipt.user_id, receipt.item_id)] = receipt
            self.by_item[receipt.item_id].append(receipt)
        self.muted = muted(routing.people, ctx.now)
        # item id -> the sections it is hidden for: marked done, or wrong, for the section
        self.done: dict[int, set[int]] = defaultdict(set)
        by_id = {item.pk: item for item in items}
        for (user_id, item_id), receipt in self.receipts.items():
            self.done[item_id] |= hidden_sections(routing.people[user_id], by_id[item_id], receipt)

    # ------------------------------------------------------------------ open items
    def decide(self, item: WatchItem, person: Person) -> Telling | None:
        """What to tell the person about an open item now, if anything (see the module's rules)."""
        receipt = self.receipts.get((person.id, item.pk))
        fresh = receipt is None or item.first_seen_on > receipt.first_told_on
        critical = item.severity == CRITICAL

        def tell(step: str | None) -> Telling | None:
            return Telling(person, item, step, receipt, fresh) if step else None

        if receipt is not None:
            if receipt.snoozed_until and receipt.snoozed_until > self.today:
                return None
            if receipt.reaction in (Reaction.NOT_MINE, Reaction.WRONG):
                if not critical:
                    return None  # theirs to disown: told again only once it is critical
                if fresh:
                    return tell(NEW)
                return tell(WORSE) if told_severity(item, receipt) != CRITICAL else None
            if not fresh and done_in_episode(item, receipt):
                checked = timezone.localdate(receipt.reacted_at) + datetime.timedelta(days=DONE_CHECK_DAYS)
                return tell(STILL_OPEN if self.today >= checked and receipt.told_step != STILL_OPEN else None)
        if person.section_id is not None and person.section_id in self.done.get(item.pk, ()):
            return None  # their section marked it done, or wrong
        quiet = (person.id, item.detector) in self.muted and not critical
        if fresh:
            if person.country and self.routing.escalated(item):
                return tell(ESCALATED)
            if quiet or (self.pre_existing(person, item) and not urgent(item, self.today)):
                return tell(KNOWN)
            return tell(NEW)
        if quiet:
            return None
        return tell(self.event(item, person, receipt))

    def pre_existing(self, person: Person, item: WatchItem) -> bool:
        """The item was there before the person could first hear of it: found by the day its check
        went on (or into trial), or by their first day, or a week ago or more."""
        start = max(person.joined, self.routing.since(item))
        return item.first_seen_on <= start or (self.today - item.first_seen_on).days >= OLD_NEWS_DAYS

    def event(self, item: WatchItem, person: Person, receipt: WatchReceipt) -> str | None:
        """The step that moved on since the person was last told, if any (the most telling one)."""
        told = receipt.last_told_on
        steps: dict[str, str] = {}
        if item.due_date is not None and item.due_date < self.today and told <= item.due_date:
            steps[OVERDUE] = OVERDUE
        reached = milestone(item.due_date, milestones_of(item), self.today)
        if (
            reached is not None
            and receipt.milestone_count < MAX_MILESTONES
            and told < item.due_date - datetime.timedelta(days=reached)
        ):
            steps["milestone"] = str(reached)
        # worse than the severity they were last told (kept on the receipt, so a rise later the same
        # day, or a telling put off by the day's limits, is still told)
        if item.key in self.worse_now or SEVERITY_RANK.get(item.severity, 0) > SEVERITY_RANK.get(
            told_severity(item, receipt), 0
        ):
            steps[WORSE] = WORSE
        if person.country and self.routing.escalated(item) and told < self.routing.escalation_day(item):
            steps[ESCALATED] = ESCALATED
        if receipt.snoozed_until and receipt.snoozed_until <= self.today:
            steps[REMINDER] = REMINDER
        if not steps:
            return None
        return steps[min(steps, key=_PRECEDENCE.__getitem__)]

    # ------------------------------------------------------------------ closed items
    def resolved(self, item: WatchItem) -> list[Telling]:
        """Tell a closed or gone item, as it ended (:func:`closing_step`: resolved, missed or closed),
        to the people who had been told about it (not only recorded as known) and did not set it
        aside."""
        step = closing_step(item)
        tellings = []
        for receipt in self.by_item.get(item.pk, ()):
            if receipt.told_step == KNOWN or receipt.told_step in ENDED:
                continue
            person = self.routing.people[receipt.user_id]
            if (
                item.first_seen_on > receipt.first_told_on  # told about an earlier time it was open
                or receipt.reaction in (Reaction.DONE, Reaction.NOT_MINE, Reaction.WRONG)
                or (receipt.snoozed_until and receipt.snoozed_until > self.today)
                or (person.id, item.detector) in self.muted
                or (person.section_id is not None and person.section_id in self.done.get(item.pk, ()))
            ):
                continue
            tellings.append(Telling(person, item, step, receipt, False))
        return tellings


def muted(people_by_id: Iterable[int], now: datetime.datetime | None = None) -> set[tuple[int, str]]:
    """(user id, check) pairs muted by three "Not useful" on the check within 30 days."""
    since = (now or timezone.now()) - datetime.timedelta(days=MUTE_DAYS)
    rows = (
        WatchReceipt.objects.filter(
            user_id__in=list(people_by_id), reaction=Reaction.NOT_USEFUL, reacted_at__gte=since
        )
        .values("user_id", "item__detector")
        .annotate(n=Count("pk"))
        .filter(n__gte=MUTE_AFTER)
    )
    return {(row["user_id"], row["item__detector"]) for row in rows}


def done_sections(person: Person, item: WatchItem) -> set[int]:
    """The sections for which the person's "Done" hides the item: all of its sections for an
    Administrator, their own for a Section editor of one of them, none for anyone else."""
    if person.admin:
        return set(item.section_ids)
    if person.editor and person.section_id in item.section_ids:
        return {person.section_id}
    return set()


def wrong_for_everyone(person: Person | None, item: WatchItem) -> bool:
    """The person's "Something's wrong" marks the item wrong for everyone (state "wrong"): an
    Administrator's, or a Section editor's whose section is the only one of a section item."""
    if person is None:
        return False
    if person.admin:
        return True
    return (
        person.editor
        and item.scope == SECTION
        and bool(item.section_ids)
        and set(item.section_ids) <= {person.section_id}
    )


def evidence_mark(item: WatchItem) -> str:
    """What an answer of "Something's wrong" is kept against: the fingerprint of the item's evidence
    (it changes when its records or numbers do)."""
    return item.evidence_hash or fingerprint(item.evidence)


def wrong_sections(person: Person, item: WatchItem, receipt: WatchReceipt) -> set[int]:
    """The section for which a Section editor's "Something's wrong" hides the item: their own, while
    the item's evidence is what it was when they said so. (An Administrator's marks it wrong for
    everyone instead: :func:`wrong_for_everyone`.)"""
    if (
        receipt.reaction == Reaction.WRONG
        and receipt.wrong_hash
        and receipt.wrong_hash == evidence_mark(item)
        and person.editor
        and person.section_id in item.section_ids
    ):
        return {person.section_id}
    return set()


def hidden_sections(person: Person, item: WatchItem, receipt: WatchReceipt) -> set[int]:
    """The sections for which the person's answer hides the item: marked done since it last
    (re)appeared (:func:`done_sections`), or marked wrong while its evidence is the same
    (:func:`wrong_sections`)."""
    hidden = done_sections(person, item) if done_in_episode(item, receipt) else set()
    return hidden | wrong_sections(person, item, receipt)


def done_by_section(item: WatchItem, routing: Routing | None = None) -> bool:
    """A Section editor of one of the item's sections, or an Administrator, marked it done (for the
    morning note: yes or no, never who)."""
    return item_stats([item], routing)[item.key]["done_by_section"]


def item_stats(items: Iterable[WatchItem], routing: Routing | None = None) -> dict[str, dict[str, Any]]:
    """For the morning note, per item key: ``times_told`` (people it was announced to, not only
    recorded as known) and ``done_by_section`` (yes or no). Never who. Two queries, whatever the
    number of items."""
    routing = routing or Routing.load()
    items = list(items)
    told = Counter(
        WatchReceipt.objects.filter(item__in=items, user_id__in=list(routing.people))
        .exclude(told_step=KNOWN)
        .values_list("item_id", flat=True)
    )
    by_id = {item.pk: item for item in items}
    done: set[int] = set()
    for receipt in WatchReceipt.objects.filter(item__in=items, reaction=Reaction.DONE).order_by("pk"):
        person, item = routing.people.get(receipt.user_id), by_id[receipt.item_id]
        if person and done_in_episode(item, receipt) and (person.admin or done_sections(person, item)):
            done.add(item.pk)
    return {
        item.key: {"times_told": told.get(item.pk, 0), "done_by_section": item.pk in done} for item in items
    }


def wrong_by_section(items: Iterable[WatchItem], routing: Routing | None = None) -> dict[int, set[int]]:
    """Item id -> the sections a Section editor marked it wrong for (:func:`wrong_sections`): their
    morning notes leave it out. One query."""
    routing = routing or Routing.load()
    by_id = {item.pk: item for item in items}
    found: dict[int, set[int]] = defaultdict(set)
    rows = WatchReceipt.objects.filter(item_id__in=list(by_id), reaction=Reaction.WRONG).exclude(
        wrong_hash=""
    )
    for receipt in rows:
        person = routing.people.get(receipt.user_id)
        if person is not None:
            found[receipt.item_id] |= wrong_sections(person, by_id[receipt.item_id], receipt)
    return {pk: sections for pk, sections in found.items() if sections}


def announce(ctx: Context, outcome: Outcome | None = None, sync_run: SyncRun | None = None) -> Announced:
    """Tell each person what moved on for them (see the module's rules) and record it, one
    ``WatchReceipt`` per person and item. ``outcome`` is the memory step's of the same pass (the
    items that got worse in it); with ``sync_run``, its details get the counts under "receipts"."""
    routing = Routing.load(ctx.today)
    result = Announced(mode=ctx.mode, people=len(routing.people))
    quick = ctx.mode == QUICK
    items = [
        item
        for item in WatchItem.objects.filter(state=State.OPEN).order_by("pk")
        if not quick or quick_worthy(item, ctx.today)
    ]
    closed = []
    if not quick:
        closed = list(
            WatchItem.objects.filter(
                state__in=(State.CLOSED, State.GONE),
                closed_on__gte=ctx.today - datetime.timedelta(days=RESOLVED_WITHIN_DAYS),
            ).order_by("pk")
        )
    reading = _Pass(ctx, routing, items + closed, outcome.worse if outcome else ())
    tellings: list[Telling] = []
    for item in items:
        for user_id in routing.recipients(item):
            telling = reading.decide(item, routing.people[user_id])
            if telling is not None:
                tellings.append(telling)
    for item in closed:
        if routing.mode(item) != OFF:
            tellings += reading.resolved(item)
    if quick:  # the quick pass tells "Needs you" only, and records nothing as known
        tellings = [t for t in tellings if t.step != KNOWN and t.level == NEEDS_YOU]
    kept = _within_limits(tellings, routing, ctx.today, result)
    _write(kept, ctx.today, result)
    if sync_run is not None:
        sync_run.details["receipts"] = result.details()
        sync_run.save(update_fields=["details"])
    return result


def _within_limits(
    tellings: list[Telling], routing: Routing, today: datetime.date, result: Announced
) -> list[Telling]:
    """The tellings within each person's limits for the day, ranked; the rest is deferred (it stays
    on the page and waits for a later pass). Known items are recorded without limit, and telling
    again something already told today takes no new place."""
    limits = {NEEDS_YOU: settings.WATCH_NEEDS_YOU_PER_DAY, GOOD_TO_KNOW: settings.WATCH_GOOD_TO_KNOW_PER_DAY}
    told_today: dict[tuple[int, str], set[int]] = defaultdict(set)
    rows = (
        WatchReceipt.objects.filter(user_id__in=list(routing.people), last_told_on=today)
        .exclude(told_step=KNOWN)
        .values_list("user_id", "level", "pk")
    )
    for user_id, level, pk in rows:
        told_today[(user_id, level)].add(pk)
    used = Counter({slot: len(pks) for slot, pks in told_today.items()})
    kept = [t for t in tellings if t.step == KNOWN]
    for telling in sorted((t for t in tellings if t.step != KNOWN), key=Telling.rank):
        slot = (telling.person.id, telling.level)
        if telling.receipt is not None and telling.receipt.pk in told_today[slot]:
            kept.append(telling)  # told again the same day: no new place
        elif used[slot] < limits[telling.level]:
            used[slot] += 1
            kept.append(telling)
        else:
            result.deferred += 1
    return kept


def _still_as_read(tellings: list[Telling], result: Announced) -> list[Telling]:
    """The tellings whose receipt is still as the pass read it, locked until the pass commits. A
    person who answered meanwhile (a reaction, "Remind me later") is left for the next pass, which
    reads their answer: never told over it."""
    read = {t.receipt.pk: t.receipt for t in tellings if t.receipt is not None}
    if not read:
        return tellings
    now = {
        pk: rest
        for pk, *rest in WatchReceipt.objects.select_for_update()
        .filter(pk__in=list(read))
        .values_list("pk", "reaction", "reacted_at", "snoozed_until")
    }
    kept = []
    for telling in tellings:
        receipt = telling.receipt
        if receipt is not None and now.get(receipt.pk) != [
            receipt.reaction,
            receipt.reacted_at,
            receipt.snoozed_until,
        ]:
            result.deferred += 1
            continue
        kept.append(telling)
    return kept


def _write(tellings: list[Telling], today: datetime.date, result: Announced) -> None:
    """Create or update the receipts of the tellings, in one transaction. Only what was told is
    written (``TOLD_FIELDS``); a point told again is marked unseen, and a reminder that has done its
    work is cleared only while it is still that reminder, by their own updates. A person who answered
    or asked for a reminder in the meantime is not told over it (:func:`_still_as_read`), and a seen
    stamp of a point only recorded as known is left as it is."""
    with transaction.atomic():
        tellings = _still_as_read(tellings, result)
        created: list[WatchReceipt] = []
        updated: list[WatchReceipt] = []
        unseen: list[int] = []  # announced again: unseen until they open the page
        reminded: list[int] = []  # the reminder has done its work
        for telling in tellings:
            step, level = telling.step, telling.level
            is_milestone = step.isdigit()
            receipt = telling.receipt
            if receipt is None:
                receipt = WatchReceipt(user_id=telling.person.id, item=telling.item, first_told_on=today)
                created.append(receipt)
            else:
                updated.append(receipt)
                if telling.fresh:  # a new episode of the item: told afresh
                    receipt.first_told_on = today
                    receipt.milestone_count = 0
                if step != KNOWN:
                    unseen.append(receipt.pk)
                    receipt.seen_at = None
                if receipt.snoozed_until and receipt.snoozed_until <= today:
                    reminded.append(receipt.pk)
            receipt.told_step = step
            receipt.told_severity = telling.item.severity
            receipt.level = level
            receipt.last_told_on = today
            receipt.milestone_count += is_milestone
            result.steps["milestone" if is_milestone else step] += 1
            if step == KNOWN:
                result.known += 1
            elif level == NEEDS_YOU:
                result.needs_you += 1
            else:
                result.good_to_know += 1
        WatchReceipt.objects.bulk_create(created)
        WatchReceipt.objects.bulk_update(updated, TOLD_FIELDS)
        if unseen:
            WatchReceipt.objects.filter(pk__in=unseen).update(seen_at=None)
        if reminded:
            WatchReceipt.objects.filter(pk__in=reminded, snoozed_until__lte=today).update(snoozed_until=None)
    result.created, result.updated = len(created), len(updated)


# ---------------------------------------------------------------------------- the page and retention
def announced() -> Q:
    """Receipts of things actually told (not only recorded as known)."""
    return ~Q(told_step=KNOWN)


def needs_you_today(user, today: datetime.date | None = None) -> QuerySet:
    """The person's "Needs you" receipts told today, with their items, best first."""
    today = today or timezone.localdate()
    return (
        WatchReceipt.objects.filter(announced(), user=user, level=NEEDS_YOU, last_told_on=today)
        .exclude(item__state=State.WRONG)
        .select_related("item")
        .order_by("seen_at", "item__due_date", "pk")
    )


def unseen_count(user, today: datetime.date | None = None) -> int:
    """The number for the badge: "Needs you" items told today that the person has not seen yet.
    Donor accounts and users the watch tells nothing get 0."""
    if Person.of(user) is None:
        return 0
    return needs_you_today(user, today).filter(seen_at__isnull=True).count()


def prune(today: datetime.date | None = None) -> int:
    """Delete the receipts last told more than 12 months ago; the number deleted. (An item still open
    is then known to the person: first seen long ago, it is not announced again.)"""
    today = today or timezone.localdate()
    cutoff = today - datetime.timedelta(days=RETENTION_DAYS)
    return WatchReceipt.objects.filter(last_told_on__lt=cutoff).delete()[0]
