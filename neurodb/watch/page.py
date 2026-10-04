"""What the "For you" page, its count in the sidebar and its Overview card show (the views are thin).

**The page** (:func:`build`) is one person's view of what NeuroDB Watch follows for them, in blocks:

- **Today's note**: the morning note of their section and, for the whole-country view (Administrators
  and the Management group), the country's. Each sentence links to the points it rests on. A note the
  AI wrote says so ("AI wrote this from the facts below"); a plain one says why the AI was not used;
- **Needs you today**: the "Needs you" points they were told today (at most
  ``WATCH_NEEDS_YOU_PER_DAY``), each as a card with the reason they hear of it, how sure NeuroDB is,
  how it knows (the evidence), what it remembers (first noticed, open for, when they were told),
  what it connects to, what NeuroDB looked up (AI) when a look-up was kept, and the buttons;
- **Coming up (next 30 days)**, **How things connect** (the situations among their open points),
  **Good to know** (told in the last 7 days) and **Everything else open**, collapsed;
- **Set aside**: what they marked done, not theirs or wrong, what they asked to hear about later, and
  what a Section editor of their section or an Administrator marked done, each with Undo where it is
  theirs.

Each point is shown once on the page: "Everything else open" holds the open points not already listed
above it. Opening the page stamps ``seen_at`` on the receipts it shows unfolded ("Needs you today",
"Coming up" and "Good to know"), which clears the count in the sidebar. The count and the Overview card
list only points the page shows "Needs you today" (the same routing). Nothing else about browsing is
recorded.

**Administrators** may look at what a section's staff (or the whole country) are shown ("Show notes
for"): that audience's note and points only, never anyone's receipts or reactions, and nothing is
stamped.

**Reactions** (:func:`react`) are made on the person's own receipt only (the view returns 404
otherwise): Useful, Not useful, Done, Not mine, Something's wrong (with an optional short comment that
only administrators read, never sent to the AI or by email), Remind me later (tomorrow, next week, or 3
days before it is due) and Undo. A Section editor of one of the point's sections, or an Administrator,
marking it wrong hides it for their section until its evidence changes, and an Administrator's (or that
of an editor whose section is the point's only one) hides it for everyone (state "wrong"); their "Done"
hides it for their section (:func:`neurodb.watch.routing.hidden_sections`).

Everything shown is text written by code, except a note's sentences and a look-up's text, written by
the AI from the facts and checked; all of it is escaped by the templates. Only links into NeuroDB or
to a web address (http, https) are kept.
"""

from __future__ import annotations

import calendar
import datetime
import hashlib
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import Section
from neurodb.core.models import SyncRun

from . import connect, detectors, investigate, routing
from .detectors import CHECK, CRITICAL, DAILY, LIKELY, ON, SURE, TRIAL, WARNING
from .detectors.concerns import FORECAST_ID
from .models import DetectorSetting, WatchItem, WatchNote, WatchReceipt

Reaction = WatchReceipt.Reaction
State = WatchItem.State
NEEDS_YOU, GOOD_TO_KNOW, KNOWN = routing.NEEDS_YOU, routing.GOOD_TO_KNOW, routing.KNOWN

BADGE_SECONDS = 60  # the sidebar count is cached this long per person (and per state of their receipts)
STALE_HOURS = 26  # no successful morning check for this long: the page says so
COMING_UP_DAYS = 30
GOOD_TO_KNOW_DAYS = 7  # "Good to know" lists what was told this many days back
GOOD_TO_KNOW_SHOWN = 10
CARD_SHOWN = 3  # points on the Overview card
STORY_SHOWN = 5  # dated lines of a point's story under "How we know"
CONNECTED_SHOWN = 5
ASK_CHARS = 1000  # Ask NeuroDB's longest question (assistant.views.MAX_QUESTION_CHARS)
COMMENT_CHARS = 300  # WatchReceipt.comment
SNOOZE_BEFORE_DUE = 3  # "3 days before it is due"

# The reactions a button may send, and the snoozes ("Remind me later")
REACTIONS = (Reaction.USEFUL, Reaction.NOT_USEFUL, Reaction.DONE, Reaction.NOT_MINE, Reaction.WRONG)
UNDO = "undo"
TOMORROW, NEXT_WEEK, BEFORE_DUE = "tomorrow", "week", "before_due"
SNOOZES = (TOMORROW, NEXT_WEEK, BEFORE_DUE)
# Reactions that take a point off the person's page (until they are told about it again)
SET_ASIDE = (Reaction.DONE, Reaction.NOT_MINE, Reaction.WRONG)

CONFIDENCE_WORDS = {SURE: "Sure", LIKELY: "Likely", CHECK: "Please check"}
SEVERITY_WORDS = {CRITICAL: "Critical", WARNING: "Warning", "info": "To note"}
# Why the AI did not write a note, in the words of "AI not used today: ..."
SKIPPED_WORDS = {
    WatchNote.Skipped.OFF: "the AI is switched off",
    WatchNote.Skipped.BUDGET: "daily limit reached",
    WatchNote.Skipped.PAUSED: "the AI is paused for a few hours",
    WatchNote.Skipped.QUOTA: "the OpenAI credit ran out",
    WatchNote.Skipped.ERROR: "the AI did not give an answer that passed the checks",
    WatchNote.Skipped.NO_CHANGE: "nothing changed",
}
AI_WROTE = "AI wrote this from the facts below"
# The hub kinds of a point's connections, in words
KIND_WORDS = {"partner": "", "programme_document": "", "grant": "grant", "donor": "donor", "database": ""}
# The evidence numbers under "How we know", in words (any other: its name in words)
NUMBER_LABELS = {
    "days_left": "Days left",
    "days_overdue": "Days overdue",
    "days_since": "Days since",
    "days_open": "Days open",
    "days_ago": "Days ago",
    "days_since_end": "Days since it ended",
    "days_since_start": "Days since it started",
    "elapsed_percent": "Time gone (%)",
    "indicators": "Indicators",
    "action_points": "Action points",
    "high_priority": "High priority",
    "oldest_due": "Oldest due date",
    "outstanding": "Outstanding (US$)",
    "unspent": "Unspent (US$)",
    "reserved": "Reserved (US$)",
    "disbursed": "Disbursed (US$)",
    "funds_reservations": "Funds reservations",
    "pds": "Programme documents",
    "flagged_by_review": "Flagged by the daily review",
    "to_date": "Reached so far",
    "target": "Target",
    "forecast": "Likely by the end of the year",
    "low": "Lowest likely",
    "high": "Highest likely",
    "low_pct": "Lowest likely (% of target)",
    "high_pct": "Highest likely (% of target)",
    "year": "Year",
    "as_of_month": "Data up to",
    "limit_hours": "Hours allowed",
    "pv_required": "Programmatic visits required",
    "pv_completed": "Programmatic visits done",
    "sc_required": "Spot checks required",
    "sc_completed": "Spot checks done",
    "audits_required": "Audits required",
    "audits_completed": "Audits done",
    "rows_failed": "Rows failed",
    "jobs_overdue": "Jobs late",
}
NUMBER_HIDDEN = frozenset({"run"})  # record ids: nothing to read
MONTH_NUMBERS = frozenset({"as_of_month", "month"})  # a month's number: shown as its name
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------- small helpers
def safe_url(url: Any) -> str:
    """``url`` when it leads into NeuroDB (a path) or to a web address; otherwise "" (never a
    ``javascript:`` or ``data:`` link)."""
    url = str(url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        return url
    if url.lower().startswith(("https://", "http://")):
        return url
    return ""


def day_words(day: datetime.date | None, today: datetime.date) -> str:
    """ "today", "yesterday", "tomorrow", or "on 21 Sep" (with the year when it is not this year)."""
    if day is None:
        return ""
    if day == today:
        return "today"
    if day == today - datetime.timedelta(days=1):
        return "yesterday"
    if day == today + datetime.timedelta(days=1):
        return "tomorrow"
    return f"on {short_date(day, today)}"


def short_date(day: datetime.date, today: datetime.date) -> str:
    """ "21 Sep", or "21 Sep 2025" in another year."""
    return f"{day.day} {day:%b}" if day.year == today.year else f"{day.day} {day:%b %Y}"


def moment_words(moment: datetime.datetime | None, today: datetime.date) -> str:
    """ "today at 07:46", "yesterday at 20:41", "on 2 Oct at 20:41" (Beirut time)."""
    if moment is None:
        return ""
    local = timezone.localtime(moment)
    return f"{day_words(local.date(), today)} at {local:%H:%M}"


def _parse_moment(text: Any) -> datetime.datetime | None:
    try:
        moment = datetime.datetime.fromisoformat(str(text or ""))
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


def _plural(count: int, word: str, words: str | None = None) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {words or word + 's'}"


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.1f}".rstrip("0").rstrip(".")
    return str(value)


def _date_text(value: Any, today: datetime.date) -> str:
    """An ISO date ("2026-10-10", or the date of a moment) as the page writes dates ("10 Oct"); any
    other text as it is."""
    text = str(value or "")
    if ISO_DAY.match(text):
        try:
            return short_date(datetime.date.fromisoformat(text[:10]), today)
        except ValueError:
            pass
    return text


def number_label(name: str) -> str:
    """An evidence number's name in words ("Days left", "Unspent (US$)")."""
    name = str(name)
    if name in NUMBER_LABELS:
        return NUMBER_LABELS[name]
    words = name.replace("_pct", " (%)").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def number_value(name: str, value: Any, today: datetime.date) -> str:
    """An evidence number as the page shows it: a year as it is written (2026), a month by its name,
    a date as "10 Oct", yes or no, and amounts with thousands separators."""
    name = str(name)
    if isinstance(value, int) and not isinstance(value, bool):
        if name == "year" or name.endswith("_year"):
            return str(value)
        if name in MONTH_NUMBERS and 1 <= value <= 12:
            return calendar.month_name[value]
    if isinstance(value, str):
        return _date_text(value, today)
    return _number(value)


def ask_url(item: WatchItem) -> str:
    """Ask NeuroDB with a question about the point already typed (at most 1,000 characters)."""
    lead = "Look into this open point for me: "
    tail = (
        ". What is behind it, what changed recently, and what else is open on the same partner, "
        "programme document or grant?"
    )
    title = " ".join(str(item.title or "").split()).rstrip(".")
    title = title[: ASK_CHARS - len(lead) - len(tail)]
    return reverse("assistant:ask") + "?" + urlencode({"q": f"{lead}{title}{tail}"})


# ---------------------------------------------------------------------------- the last check
def last_check(target: str = "") -> SyncRun | None:
    """The last run of NeuroDB Watch that succeeded (``target`` "daily": the morning pass only)."""
    return SyncRun.last_success(SyncRun.Job.WATCH, target)


def banner(now: datetime.datetime | None = None) -> str:
    """The warning at the top of the page when the morning check has not succeeded for 26 hours
    (or NeuroDB Watch is switched off); empty otherwise."""
    now = now or timezone.now()
    if not settings.WATCH_ENABLED:
        return "NeuroDB is not checking at the moment: an administrator switched these checks off."
    last = last_check(DAILY)
    if last is None or last.finished_at is None:
        return (
            "NeuroDB has not finished a morning check yet. If nothing appears by tomorrow, ask an "
            "administrator to look at Scheduled jobs."
        )
    if now - last.finished_at <= datetime.timedelta(hours=STALE_HOURS):
        return ""
    local = timezone.localtime(last.finished_at)
    today = timezone.localdate(now)
    since = f"{local:%A}" if (today - local.date()).days < 7 else short_date(local.date(), today)
    return f"NeuroDB has not checked since {since}. Ask an administrator to look at Scheduled jobs."


# ---------------------------------------------------------------------------- the count and the card
def _badge_key(user_id: int, today: datetime.date) -> str:
    """The cache key of the person's count: their receipts of the day as they stand now (how many,
    the latest told, seen, answered and reminder), so that seeing or answering a point changes the
    key for every web worker at once, whichever worker cached the count."""
    state = WatchReceipt.objects.filter(user_id=user_id, last_told_on=today).aggregate(
        n=Count("pk"), seen=Max("seen_at"), reacted=Max("reacted_at"), snoozed=Max("snoozed_until")
    )
    stamp = "|".join(str(state[name]) for name in ("n", "seen", "reacted", "snoozed"))
    digest = hashlib.sha1(stamp.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"watch-badge:{user_id}:{today.isoformat()}:{digest}"


def forget_badge(user, today: datetime.date | None = None) -> None:
    """Drop the cached count, so the sidebar shows the change at once (the key also moves with the
    person's receipts, for the other web workers)."""
    cache.delete(_badge_key(user.pk, today or timezone.localdate()))


def needs_you(user, today: datetime.date, rules: routing.Routing | None = None) -> list[WatchReceipt]:
    """The person's "Needs you" receipts told today that the page lists under "Needs you today", best
    first: points they still get (the same routing as the page: a check switched off, an escalation
    no longer escalated or a change of section takes them off), without what they set aside, what they
    asked to hear about later and what their section marked done or wrong."""
    person = routing.Person.of(user)
    if person is None:
        return []
    receipts = [
        receipt
        for receipt in routing.needs_you_today(user, today).filter(item__state=State.OPEN)
        if not set_aside_reason(receipt, today)
    ]
    if not receipts:
        return []
    rules = rules or routing.Routing.load(today)
    visible = {item.pk for item in rules.items_for(person, [r.item for r in receipts])}
    receipts = [r for r in receipts if r.item_id in visible]
    hidden = section_hidden([r.item for r in receipts], person)
    kept = [r for r in receipts if r.item_id not in hidden]
    return sorted(kept, key=lambda r: _rank(r.item))[: settings.WATCH_NEEDS_YOU_PER_DAY]


def badge_count(user, today: datetime.date | None = None) -> int:
    """The number in the sidebar: "Needs you" points told today that the person has not seen, has not
    reacted to and has not put off. 0 for anyone NeuroDB Watch tells nothing (a donor account, no
    role). Cached for a minute per person and state of their receipts."""
    today = today or timezone.localdate()
    key = _badge_key(user.pk, today)
    count = cache.get(key)
    if count is None:
        count = sum(1 for receipt in needs_you(user, today) if receipt.seen_at is None)
        cache.set(key, count, BADGE_SECONDS)
    return count


def card(user, today: datetime.date | None = None) -> dict[str, Any]:
    """The Overview card: how many points need the person today, the first three, and whether NeuroDB
    tells them anything at all (``shown`` false: no card)."""
    today = today or timezone.localdate()
    if routing.Person.of(user) is None:
        return {"shown": False}
    receipts = needs_you(user, today)
    unseen = sum(1 for receipt in receipts if receipt.seen_at is None)
    return {
        "shown": True,
        "count": len(receipts),
        "unseen": unseen,
        "top": [
            {
                "title": r.item.title,
                "reason": routing.reason(r),
                "severity": r.item.severity,
                "severity_label": SEVERITY_WORDS.get(r.item.severity, ""),
                "anchor": f"watch-{r.item_id}",
            }
            for r in receipts[:CARD_SHOWN]
        ],
    }


# ---------------------------------------------------------------------------- what is set aside
def _reacted_on(receipt: WatchReceipt) -> datetime.date | None:
    return timezone.localdate(receipt.reacted_at) if receipt.reacted_at else None


def set_aside_reason(receipt: WatchReceipt, today: datetime.date) -> str:
    """Why the person took the point off their page ("" when they did not): a reaction since they
    were last told about it, or a reminder still to come."""
    if receipt.snoozed_until and receipt.snoozed_until > today:
        return f"You asked to hear about it again {day_words(receipt.snoozed_until, today)}"
    reacted = _reacted_on(receipt)
    if receipt.reaction in SET_ASIDE and reacted is not None and reacted >= receipt.last_told_on:
        return {
            Reaction.DONE: "You marked it done",
            Reaction.NOT_MINE: "You said it is not yours",
            Reaction.WRONG: "You said something is wrong",
        }[receipt.reaction]
    return ""


SECTION_HIDDEN_WORDS = {
    Reaction.DONE: "Marked done for your section",
    Reaction.WRONG: "Marked wrong for your section, until its data changes",
}


def section_hidden(items: Iterable[WatchItem], person: routing.Person) -> dict[int, str]:
    """The points hidden for the person's section, with why: marked done since they last (re)appeared
    by a Section editor of the section or an Administrator, or marked wrong by a Section editor of the
    section while their evidence is the same (:func:`neurodb.watch.routing.hidden_sections`)."""
    by_id = {item.pk: item for item in items}
    if person.section_id is None or not by_id:
        return {}
    rows = list(
        WatchReceipt.objects.filter(item_id__in=list(by_id), reaction__in=(Reaction.DONE, Reaction.WRONG))
        .exclude(user_id=person.id)
        .order_by("pk")
    )
    if not rows:
        return {}
    deciders = routing.people({row.user_id for row in rows})
    hidden: dict[int, str] = {}
    for row in rows:
        decider, item = deciders.get(row.user_id), by_id[row.item_id]
        if decider is not None and person.section_id in routing.hidden_sections(decider, item, row):
            hidden.setdefault(item.pk, SECTION_HIDDEN_WORDS[row.reaction])
    return hidden


# ---------------------------------------------------------------------------- one point, as shown
def _rank(item: WatchItem) -> tuple:
    """Critical first, then the soonest due."""
    return (item.severity != CRITICAL, item.due_date is None, item.due_date or datetime.date.max, item.pk)


def _confidence(item: WatchItem) -> str:
    """ "Sure", "Likely", "Please check"; a forecast says its range: "Estimate: between 62% and 78% of
    target"."""
    numbers = ((item.evidence or {}).get("numbers") or {}) if isinstance(item.evidence, dict) else {}
    low, high = numbers.get("low_pct"), numbers.get("high_pct")
    if item.detector == FORECAST_ID and isinstance(low, int | float) and isinstance(high, int | float):
        return f"Estimate: between {low:.0f}% and {high:.0f}% of target"
    return CONFIDENCE_WORDS.get(item.confidence, "")


def _evidence(item: WatchItem, today: datetime.date) -> dict[str, Any]:
    """How NeuroDB knows: the source and when it was last synced, the records, the numbers and the
    latest lines of the point's story."""
    evidence = item.evidence if isinstance(item.evidence, dict) else {}
    synced = _parse_moment(evidence.get("synced_at"))
    records = []
    for row in evidence.get("records") or []:
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "label": str(row.get("label") or ""),
                "date": _date_text(row.get("date"), today),
                "value": _number(row["value"]) if row.get("value") not in (None, "") else "",
                "url": safe_url(row.get("url")),
            }
        )
    numbers = [
        {"name": number_label(name), "value": number_value(name, value, today)}
        for name, value in (evidence.get("numbers") or {}).items()
        if value not in (None, "")
        and not isinstance(value, dict | list)
        and str(name) not in NUMBER_HIDDEN
        and not str(name).endswith("_id")
    ]
    story = [
        {"on": _date_text(line.get("on"), today), "text": str(line.get("text") or "")}
        for line in (item.story or [])[-STORY_SHOWN:][::-1]
        if isinstance(line, dict)
    ]
    return {
        "source": str(evidence.get("source") or ""),
        "updated": moment_words(synced, today) if synced else "",
        "records": records,
        "numbers": numbers,
        "story": story,
    }


def _connected(item: WatchItem, today: datetime.date) -> list[dict[str, str]]:
    """What the point connects to, in words: its partner, PDs and grants ("grant SC1 expires 30
    Nov"), "2 documents", "3 other open points"."""
    shown: list[dict[str, str]] = []
    documents = others = 0
    for entry in item.related or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        if kind == connect.ITEM:
            others += 1
            continue
        if kind == "document":
            documents += 1
            continue
        if kind == item.entity_kind and str(entry.get("key")) == item.entity_key:
            continue  # the point's own thing
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        label = f"{KIND_WORDS[kind]} {name}".strip() if kind in KIND_WORDS else name
        if kind == "grant" and entry.get("date"):
            try:
                expiry = datetime.date.fromisoformat(str(entry["date"]))
            except ValueError:
                expiry = None
            if expiry is not None:
                verb = "expires" if expiry >= today else "expired"
                label = f"{label} {verb} {short_date(expiry, today)}"
        shown.append({"label": label, "url": safe_url(entry.get("url"))})
    shown = shown[:CONNECTED_SHOWN]
    if documents:
        shown.append({"label": _plural(documents, "document"), "url": ""})
    if others:
        shown.append({"label": _plural(others, "other open point"), "url": ""})
    return shown


def _memory(item: WatchItem, receipt: WatchReceipt | None, today: datetime.date) -> str:
    """ "First noticed 20 Sep · open 14 days · told you first on 21 Sep, again today"."""
    parts = [f"First noticed {short_date(item.first_seen_on, today)}"]
    if item.state == State.OPEN:
        days = (today - item.first_seen_on).days
        parts.append("open since today" if days <= 0 else f"open {_plural(days, 'day')}")
    elif item.closed_on:
        ended = "closed" if item.state == State.CLOSED else "no longer seen"
        why = " ".join(str(item.close_reason or "").split()).rstrip(".")
        parts.append(f"{ended} {day_words(item.closed_on, today)}" + (f" ({why})" if why else ""))
    if receipt is not None and receipt.told_step != KNOWN:
        first, last = receipt.first_told_on, receipt.last_told_on
        if first == last:
            parts.append(f"told you {day_words(last, today)}")
        else:
            parts.append(f"told you first {day_words(first, today)}, again {day_words(last, today)}")
    return " · ".join(parts)


def _due(item: WatchItem, today: datetime.date) -> str:
    """ "Due today", "Due 10 Oct (in 6 days)", "Was due 2 Oct (3 days ago)"."""
    if item.due_date is None:
        return ""
    left = (item.due_date - today).days
    when = short_date(item.due_date, today)
    if left == 0:
        return "Due today"
    if left > 0:
        return f"Due {when} (in {_plural(left, 'day')})"
    return f"Was due {when} ({_plural(-left, 'day')} ago)"


def _snoozes(item: WatchItem, today: datetime.date) -> list[dict[str, str]]:
    """The "Remind me later" choices: tomorrow, next week, and 3 days before it is due when that is
    after tomorrow."""
    options = [
        {"value": TOMORROW, "label": "Tomorrow"},
        {"value": NEXT_WEEK, "label": "Next week"},
    ]
    until = snooze_until(BEFORE_DUE, item, today)
    if until is not None and until > today + datetime.timedelta(days=1):
        options.append(
            {"value": BEFORE_DUE, "label": f"3 days before it is due ({short_date(until, today)})"}
        )
    return options


def snooze_until(choice: str, item: WatchItem, today: datetime.date) -> datetime.date | None:
    """The day a "Remind me later" choice means, or None when it means nothing (no due date, or 3
    days before it is due is already past)."""
    if choice == TOMORROW:
        return today + datetime.timedelta(days=1)
    if choice == NEXT_WEEK:
        return today + datetime.timedelta(days=7)
    if choice == BEFORE_DUE and item.due_date is not None:
        until = item.due_date - datetime.timedelta(days=SNOOZE_BEFORE_DUE)
        return until if until > today else None
    return None


def reason_chip(receipt: WatchReceipt, item: WatchItem, today: datetime.date) -> str:
    """Why the person hears of the point, as its chip says it, for as long as it is true: as it was
    told when that was today ("Due in 3 days", "New"); a step told in the last 7 days with when ("Got
    worse · told yesterday"), except a due-date milestone (its count of days is out of date: the due
    chip says it); how a closed point ended ("Resolved", "Date passed, not done"); else nothing."""
    words = routing.reason(receipt, item)
    if not words or receipt.last_told_on == today:
        return words
    if receipt.told_step in routing.ENDED:
        return words  # how it ended stays true
    age = (today - receipt.last_told_on).days
    if receipt.told_step.isdigit() or not 0 < age < GOOD_TO_KNOW_DAYS:
        return ""
    return f"{words} · told {day_words(receipt.last_told_on, today).removeprefix('on ')}"


def point(
    item: WatchItem,
    receipt: WatchReceipt | None,
    today: datetime.date,
    *,
    modes: dict[str, str] | None = None,
    actions: bool = True,
) -> dict[str, Any]:
    """One point as a card shows it. ``receipt`` is the person's own (None in an administrator's
    preview, or when they were never told): without it there are no buttons and no reason."""
    looked = investigate.shown(item)
    return {
        "item": item,
        "pk": item.pk,
        "title": item.title,
        "detail": item.detail,
        "url": safe_url(item.url),
        "severity": item.severity,
        "severity_label": SEVERITY_WORDS.get(item.severity, ""),
        "state": item.state,
        "due": _due(item, today),
        "due_date": item.due_date,
        "reason": reason_chip(receipt, item, today) if receipt is not None else "",
        "confidence": _confidence(item),
        "trial": mode_of(item, modes) == TRIAL,
        "evidence": _evidence(item, today),
        "memory": _memory(item, receipt, today),
        "connected": _connected(item, today),
        "looked_up": looked,
        "ask_url": ask_url(item) if settings.AI_ASSISTANT_ENABLED else "",
        "receipt": receipt if actions else None,
        "reaction": receipt.reaction if receipt is not None else "",
        "snoozes": _snoozes(item, today) if receipt is not None and actions else [],
        "anchor": "",
        "why": "",  # why it is set aside, when it is
        "undo": False,  # the person's own answer can be taken back
    }


def check_modes() -> dict[str, str]:
    """Each check's setting (trial, on or off), read once for a page."""
    return dict(DetectorSetting.objects.values_list("detector", "mode"))


def mode_of(item: WatchItem, settings_: dict[str, str] | None) -> str:
    """The setting of the point's check; a check never run yet has its default (as routing reads it)."""
    mode = (settings_ or {}).get(item.detector)
    if mode is None:
        check = detectors.get(item.detector)
        mode = check.default_mode if check else TRIAL
    return mode


# ---------------------------------------------------------------------------- the notes
def _note(audience: str, today: datetime.date, title: str, cards: dict[str, dict[str, Any]]) -> dict | None:
    """The latest morning note of ``audience`` up to today, with each sentence's links to the points
    on the page it rests on, and its label."""
    note = WatchNote.objects.filter(audience_key=audience, date__lte=today).order_by("-date").first()
    if note is None:
        return None
    sentences = []
    for sentence in note.sentences or []:
        if not isinstance(sentence, dict) or not str(sentence.get("text") or "").strip():
            continue
        links = []
        for key in sentence.get("keys") or []:
            found = cards.get(str(key))
            if found is not None:
                links.append({"label": found["title"], "anchor": f"watch-{found['pk']}"})
        sentences.append({"text": str(sentence["text"]), "links": links})
    if note.written_by == WatchNote.TEMPLATE:
        why = SKIPPED_WORDS.get(note.ai_skipped_reason, "")
        label = "Listed by NeuroDB." + (f" AI not used today: {why}" if why else "")
    else:
        label = AI_WROTE
    return {
        "title": title,
        "sentences": sentences,
        "label": label,
        "by_ai": note.written_by != WatchNote.TEMPLATE,
        "date": note.date,
        "old": note.date != today,
        "when": day_words(note.date, today),
    }


# ---------------------------------------------------------------------------- the page
def audiences_for_picker() -> list[dict[str, str]]:
    """The audiences an administrator may look at: the whole country, then each section."""
    choices = [{"value": WatchNote.COUNTRY, "label": "Whole country"}]
    for pk, name in Section.objects.order_by("name").values_list("pk", "name"):
        choices.append({"value": WatchNote.section_audience(pk), "label": name})
    return choices


def _situations(items: list[WatchItem], cards: dict[str, dict[str, Any]], today: datetime.date) -> list[dict]:
    out = []
    for situation in connect.situations_of(items, today=today):
        out.append(
            {
                "headline": situation.headline,
                "url": safe_url(situation.url),
                "points": [
                    {"title": item.title, "anchor": f"watch-{item.pk}" if item.key in cards else ""}
                    for item in situation.items
                ],
                "changes": situation.change_lines[:3],
            }
        )
    return out


def _anchor(blocks: Iterable[list[dict[str, Any]]]) -> None:
    """Give each point one anchor on the page: where it is first shown."""
    seen: set[int] = set()
    for block in blocks:
        for shown in block:
            if shown["pk"] not in seen:
                seen.add(shown["pk"])
                shown["anchor"] = f"watch-{shown['pk']}"


def build(
    user,
    *,
    audience: str = "",
    now: datetime.datetime | None = None,
    stamp: bool = True,
) -> dict[str, Any]:
    """Everything the page shows for ``user`` (see the module's notes). ``audience`` ("country" or
    "section:<id>") is an administrator's preview of what that audience is shown; anyone else's is
    ignored. ``stamp`` marks the receipts shown as seen."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    person = routing.Person.of(user)
    is_admin = bool(person and person.admin)
    rules = routing.Routing.load(today)
    checks = {key: setting.mode for key, setting in rules.checks.items()}
    last = last_check()
    context: dict[str, Any] = {
        "today": today,
        "banner": banner(now),
        "checked": moment_words(last.finished_at, today) if last and last.finished_at else "",
        "is_admin": is_admin,
        "picker": audiences_for_picker() if is_admin else [],
        "audience": "",
        "preview": False,
        "notes": [],
        "needs_you": [],
        "coming_up": [],
        "situations": [],
        "good_to_know": [],
        "everything": [],
        "set_aside": [],
        "no_section": False,
        "no_role": False,
        "subtitle_for": "",
        "needs_you_limit": settings.WATCH_NEEDS_YOU_PER_DAY,
    }
    open_items = list(WatchItem.objects.filter(state=State.OPEN).order_by("due_date", "pk"))
    if is_admin and audience and _valid_audience(audience):
        return _preview(context, audience, open_items, rules, checks, today)

    if person is not None:
        visible = rules.items_for(person, open_items)
    else:
        visible = []
    if person is None or (person.section_id is None and not person.country):
        # Not told anything: everything open that section staff are told, to read only. Without a
        # role (person None) they are told to ask for one; without a section, for a section.
        context["no_section"] = True
        context["no_role"] = person is None
        context["subtitle_for"] = ""
        shared = [i for i in open_items if rules.mode(i) == ON and not rules.admin_only(i)]
        context["everything"] = [point(i, None, today, modes=checks, actions=False) for i in shared]
        _anchor([context["everything"]])
        return context

    section_name = ""
    if person.section_id:
        section_name = (
            Section.objects.filter(pk=person.section_id).values_list("name", flat=True).first() or ""
        )
    context["subtitle_for"] = section_name or ("the whole country" if person.country else "")

    visible_ids = [item.pk for item in visible]
    receipts = {
        receipt.item_id: receipt
        for receipt in WatchReceipt.objects.filter(user=user)
        .filter(
            Q(item_id__in=visible_ids)
            | Q(last_told_on__gte=today - datetime.timedelta(days=GOOD_TO_KNOW_DAYS - 1))
        )
        .select_related("item")
    }
    hidden = section_hidden(visible, person)
    shown_items, set_aside = [], []
    for item in visible:
        receipt = receipts.get(item.pk)
        why = set_aside_reason(receipt, today) if receipt is not None else ""
        if not why and item.pk in hidden:
            why = hidden[item.pk]
        if why:
            card_ = point(item, receipt, today, modes=checks)
            card_["why"] = why
            card_["undo"] = receipt is not None and bool(set_aside_reason(receipt, today))
            set_aside.append(card_)
        else:
            shown_items.append(item)

    cards = {item.key: point(item, receipts.get(item.pk), today, modes=checks) for item in shown_items}
    needs = [
        cards[r.item.key]
        for r in sorted(receipts.values(), key=lambda r: _rank(r.item))
        if r.level == NEEDS_YOU and r.told_step != KNOWN and r.last_told_on == today and r.item.key in cards
    ][: settings.WATCH_NEEDS_YOU_PER_DAY]
    in_needs = {shown["pk"] for shown in needs}
    horizon = today + datetime.timedelta(days=COMING_UP_DAYS)
    coming = [
        dict(cards[item.key])  # a copy per block: each point gets its anchor once (_anchor)
        for item in sorted(shown_items, key=lambda i: (i.due_date or datetime.date.max, i.pk))
        if item.due_date is not None and today <= item.due_date <= horizon and item.pk not in in_needs
    ]
    listed = in_needs | {shown["pk"] for shown in coming}  # shown above already: not again
    good = []
    for receipt in sorted(receipts.values(), key=lambda r: (r.last_told_on, r.pk), reverse=True):
        if receipt.level != GOOD_TO_KNOW or receipt.told_step == KNOWN or receipt.item_id in listed:
            continue
        if receipt.last_told_on < today - datetime.timedelta(days=GOOD_TO_KNOW_DAYS - 1):
            continue
        if receipt.item.state == State.WRONG or set_aside_reason(receipt, today):
            continue
        if receipt.item.state == State.OPEN and receipt.item.key not in cards:
            continue  # open but no longer theirs (or set aside for the section)
        key = receipt.item.key
        good.append(dict(cards[key]) if key in cards else point(receipt.item, receipt, today, modes=checks))
        if len(good) >= GOOD_TO_KNOW_SHOWN:
            break

    listed |= {shown["pk"] for shown in good}
    context.update(
        needs_you=needs,
        coming_up=coming,
        situations=_situations(shown_items, cards, today),
        good_to_know=good,
        # each point once on the page: the open points not listed above
        everything=[dict(cards[item.key]) for item in shown_items if item.pk not in listed],
        everything_else=bool(listed),
        set_aside=set_aside,
    )
    _anchor([needs, coming, good, context["everything"], set_aside])
    notes = []
    if person.section_id:
        found = _note(WatchNote.section_audience(person.section_id), today, "Today's note", cards)
        if found:
            notes.append(found)
    if person.country:
        found = _note(WatchNote.COUNTRY, today, "Across the country", cards)
        if found:
            notes.append(found)
    context["notes"] = notes
    if stamp:
        shown_now = needs + coming + good  # not "Everything else open": it is folded away
        _stamp(user, [shown["receipt"] for shown in shown_now if shown["receipt"] is not None], now)
    return context


def _valid_audience(audience: str) -> bool:
    if audience == WatchNote.COUNTRY:
        return True
    section_id = routing.section_of(audience)
    return section_id is not None and Section.objects.filter(pk=section_id).exists()


def _preview(
    context: dict[str, Any],
    audience: str,
    open_items: list[WatchItem],
    rules: routing.Routing,
    checks: dict[str, str],
    today: datetime.date,
) -> dict[str, Any]:
    """An administrator's look at what one audience is shown: its note and its open points, never
    anyone's receipts or reactions."""
    items = rules.audience_items(audience, open_items)
    cards = {item.key: point(item, None, today, modes=checks, actions=False) for item in items}
    name = "the whole country"
    section_id = routing.section_of(audience)
    if section_id is not None:
        name = Section.objects.filter(pk=section_id).values_list("name", flat=True).first() or ""
    horizon = today + datetime.timedelta(days=COMING_UP_DAYS)
    context.update(
        audience=audience,
        preview=True,
        subtitle_for=name,
        coming_up=[
            dict(cards[i.key])
            for i in sorted(items, key=lambda i: (i.due_date or datetime.date.max, i.pk))
            if i.due_date is not None and today <= i.due_date <= horizon
        ],
        situations=_situations(items, cards, today),
        everything=[dict(shown) for shown in cards.values()],
    )
    _anchor([context["coming_up"], context["everything"]])
    title = "Across the country" if audience == WatchNote.COUNTRY else "Today's note"
    found = _note(audience, today, title, cards)
    context["notes"] = [found] if found else []
    return context


def _stamp(user, receipts: list[WatchReceipt], now: datetime.datetime) -> None:
    """Mark the receipts shown as seen (those not seen yet), and refresh the sidebar count."""
    unseen = [receipt.pk for receipt in receipts if receipt.seen_at is None]
    if unseen:
        WatchReceipt.objects.filter(pk__in=unseen, user=user, seen_at__isnull=True).update(seen_at=now)
        for receipt in receipts:
            if receipt.pk in unseen:
                receipt.seen_at = now
    forget_badge(user, timezone.localdate(now))


# ---------------------------------------------------------------------------- reactions
class Refused(ValueError):
    """A reaction that cannot be made (an unknown button, a reminder date already past)."""


def decides_for_section(person: routing.Person | None, item: WatchItem) -> bool:
    """An Administrator, or a Section editor of one of the point's sections: their Done and
    Something's wrong count for the section (for everyone: :func:`neurodb.watch.routing.
    wrong_for_everyone`)."""
    return bool(person) and (person.admin or (person.editor and person.section_id in item.section_ids))


def _still_marked_wrong(item: WatchItem, but: int) -> bool:
    """Someone else whose answer marks it wrong for everyone still says the point is wrong."""
    others = list(
        WatchReceipt.objects.filter(item=item, reaction=Reaction.WRONG)
        .exclude(user_id=but)
        .values_list("user_id", flat=True)
    )
    if not others:
        return False
    deciders = routing.people(others)
    return any(routing.wrong_for_everyone(deciders.get(pk), item) for pk in others)


def react(
    receipt: WatchReceipt,
    *,
    reaction: str = "",
    snooze: str = "",
    comment: str = "",
    now: datetime.datetime | None = None,
) -> str:
    """Record the person's reaction on their own receipt (see the module's notes); a short message
    in plain words saying what it does. Raises :class:`Refused` for an unknown choice."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    person = routing.Person.of(receipt.user)
    fields = ["seen_at"]
    receipt.seen_at = receipt.seen_at or now
    message = ""
    reopen = False
    with transaction.atomic():
        # the point read anew and locked: a pass writing it meanwhile is waited for, never undone
        item = WatchItem.objects.select_for_update().get(pk=receipt.item_id)
        receipt.item = item
        deciding = decides_for_section(person, item)
        everyone = routing.wrong_for_everyone(person, item)
        if snooze:
            until = snooze_until(snooze, item, today)
            if snooze not in SNOOZES:
                raise Refused("Unknown reminder.")
            if until is None:
                raise Refused("That reminder date has already passed.")
            receipt.snoozed_until = until
            fields.append("snoozed_until")
            message = f"NeuroDB will remind you {day_words(until, today)}."
        elif reaction == UNDO:
            reopen = receipt.reaction == Reaction.WRONG and everyone
            receipt.reaction, receipt.reacted_at, receipt.comment, receipt.snoozed_until = "", None, "", None
            receipt.wrong_hash = ""
            fields += ["reaction", "reacted_at", "comment", "snoozed_until", "wrong_hash"]
            message = "Your answer was taken back."
        elif reaction in REACTIONS:
            reopen = receipt.reaction == Reaction.WRONG and reaction != Reaction.WRONG and everyone
            receipt.reaction, receipt.reacted_at = reaction, now
            wrong = reaction == Reaction.WRONG
            receipt.comment = " ".join(str(comment or "").split())[:COMMENT_CHARS] if wrong else ""
            # the evidence it was said of: a Section editor's answer holds for their section until it changes
            receipt.wrong_hash = routing.evidence_mark(item) if wrong else ""
            fields += ["reaction", "reacted_at", "comment", "wrong_hash"]
            message = _said(reaction, deciding, person, everyone)
            if wrong and everyone and item.state == State.OPEN:
                item.state = State.WRONG
                item.add_story("Marked wrong: hidden until its data changes", today)
                item.save(update_fields=["state", "story"])
            elif wrong and deciding:
                item.add_story(
                    "Marked wrong for one of its sections: hidden there until its data changes", today
                )
                item.save(update_fields=["story"])
        else:
            raise Refused("Unknown answer.")
        if reopen and item.state == State.WRONG and not _still_marked_wrong(item, receipt.user_id):
            item.state = State.OPEN
            item.add_story("No longer marked wrong", today)
            item.save(update_fields=["state", "story"])
        receipt.save(update_fields=fields)
    forget_badge(receipt.user, today)
    return message


def _said(reaction: str, deciding: bool, person: routing.Person | None, everyone: bool = False) -> str:
    if reaction == Reaction.USEFUL:
        return "Thanks: marked useful."
    if reaction == Reaction.NOT_USEFUL:
        return (
            "Thanks: marked not useful. Three of these on the same kind of point within a month "
            "quiet its less urgent ones for you."
        )
    if reaction == Reaction.DONE:
        whose = " It is hidden for your section too." if deciding else ""
        if person is not None and person.admin:
            whose = " It is hidden for its sections too."
        return f"Marked done.{whose} If it is still open in 7 days, NeuroDB tells you once more."
    if reaction == Reaction.NOT_MINE:
        return "Noted: not yours. NeuroDB will not tell you about it again unless it becomes critical."
    if everyone:
        return "Noted: something is wrong. It is hidden for everyone until its data changes."
    if deciding:
        return (
            "Noted: something is wrong. It is hidden for your section until its data changes; "
            "administrators can read your comment."
        )
    return "Noted: something is wrong. It is hidden for you; administrators can read your comment."
