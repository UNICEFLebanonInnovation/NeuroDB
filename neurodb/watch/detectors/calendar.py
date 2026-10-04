"""Calendar checks: dates people agreed or set in NeuroDB itself, rather than in eTools.

- ``assignment_due``: a finding of the daily review was given an agreed date (``FindingAssignment``,
  set by the administrators in the management brief) that falls in the next 3 days or has passed,
  and the assignment is not closed. The title is the finding's own (written by code); the owner and
  the note are never read, only whether someone owns it and the assignment's status. Warning. Told to
  the staff of the finding's section and to the whole-country view. It closes at once when the
  assignment is closed or deleted, or its date is removed or moved past 3 days.
- ``donor_account_expiring``: a donor's sign-in that stops working within 14 days (warning from 3
  days before). Administrators only. It closes once the account is extended, switched off or expired.
- ``reporting_year_rollover``: from 15 December to 15 January, the new year is not the reporting year
  marked as current yet: the yearly rollover is due (to note in December, warning from 1 January).
  Administrators only. It closes once the new year is current, or on 16 January saying it still is not.

They read NeuroDB's own tables, which are always up to date, so they have no source job to wait for.
Titles carry dates, never day counts. Every check starts in trial: only the whole-country view (for
the last two, the administrators) sees it until an administrator switches it on. Keys:
:func:`assignment_key`, :func:`donor_account_key` and :func:`rollover_key`.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

from django.db.models import BooleanField, Case, Value, When
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.donors.models import DonorAccount
from neurodb.indicators.models import ReportingYear
from neurodb.review.checks import link
from neurodb.review.models import FindingAssignment, ReviewFinding

from ..models import WatchItem, fit_key
from . import (
    ADMINS,
    COUNTRY,
    DEADLINE,
    INFO,
    WARNING,
    Candidate,
    Close,
    Context,
    Detector,
    evidence,
    record,
    register,
)

FINDING = "review_finding"  # the knowledge hub kind of a daily review finding (keyed by its key)
HUB_KEY_MAX = 120  # WatchItem.entity_key: a longer finding key is not linked to the hub

ASSIGNMENT_DAYS = 3  # agreed dates within this many days, or passed
ASSIGNMENT_MILESTONES = (3, 0)
DONOR_ACCOUNT_DAYS = 14
DONOR_ACCOUNT_WARNING_DAYS = 3
DONOR_ACCOUNT_MILESTONES = (14, 7, 1)
ROLLOVER_FROM = (12, 15)  # (month, day): the rollover is raised from 15 December...
ROLLOVER_UNTIL = (1, 15)  # ...to 15 January
ROLLOVER_MILESTONES = (14, 1)

ASSIGNMENT_SOURCE = "Agreed dates on daily review findings"
DONOR_SOURCE = "NeuroDB donor accounts"
ROLLOVER_SOURCE = "NeuroDB reporting years"

ASSIGNMENT_PREFIX = "assignment:"
DONOR_ACCOUNT_PREFIX = "donor_account:"
ROLLOVER_PREFIX = "rollover:"
CLOSED = FindingAssignment.Status.CLOSED


# ---------------------------------------------------------------------------- keys
def assignment_key(finding_key: str) -> str:
    """The agreed date of a daily review finding: ``assignment:<finding key>``."""
    return fit_key(f"{ASSIGNMENT_PREFIX}{finding_key}")


def donor_account_key(pk: int) -> str:
    return f"{DONOR_ACCOUNT_PREFIX}{pk}"


def rollover_key(year: int) -> str:
    """The yearly rollover to ``year``: ``rollover:<year>``."""
    return f"{ROLLOVER_PREFIX}{year}"


# ---------------------------------------------------------------------------- shared
def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def _admin(name: str, *args: Any) -> str:
    try:
        return reverse(f"admin:{name}", args=args)
    except NoReverseMatch:
        return ""


def _timing(due: datetime.date, today: datetime.date) -> dict[str, int]:
    """Days left, or days overdue once the date passed (both move by themselves: not evidence)."""
    left = (due - today).days
    return {"days_left": left} if left >= 0 else {"days_overdue": -left}


# ---------------------------------------------------------------------------- agreed dates
def _titles(keys: list[str]) -> dict[str, tuple[str, str]]:
    """The title and link of each finding in its latest review (both written by code)."""
    rows = (
        ReviewFinding.objects.filter(key__in=keys)
        .order_by("key", "-review__date", "-pk")
        .distinct("key")
        .values_list("key", "title", "url")
    )
    return {key: (title, url) for key, title, url in rows}


def _assignments(**filters: Any):
    """Finding assignments with what the checks may read: never the owner nor the note, only whether
    an owner is written (``owned``)."""
    owned = Case(
        When(owner__regex=r"\S", then=Value(True)), default=Value(False), output_field=BooleanField()
    )
    return (
        FindingAssignment.objects.filter(**filters)
        .annotate(owned=owned)
        .values("pk", "key", "title", "section", "status", "due_date", "closed_at", "owned")
    )


def assignments_due(ctx: Context) -> Iterator[Candidate]:
    """Agreed dates on daily review findings within the next 3 days or passed, not closed."""
    until = ctx.today + datetime.timedelta(days=ASSIGNMENT_DAYS)
    rows = list(
        _assignments(due_date__isnull=False, due_date__lte=until)
        .exclude(status=CLOSED)
        .order_by("due_date", "key")
    )
    findings = _titles([row["key"] for row in rows])
    for row in rows:
        yield _assignment(ctx, row, findings.get(row["key"]))


def _assignment(ctx: Context, row: dict[str, Any], finding: tuple[str, str] | None) -> Candidate:
    due = row["due_date"]
    title, finding_url = finding or ((row["title"] or "").strip() or row["key"], "")
    status = (
        FindingAssignment.Status(row["status"]).label
        if row["status"] in FindingAssignment.Status.values
        else row["status"]
    )
    passed = due < ctx.today
    url = finding_url or link("reports:brief")
    if passed:
        heading = f"Agreed date {_day(due)} passed"
        detail = f"The date agreed for this daily review finding was {_day(due)}, and it is not closed yet."
    else:
        heading = f"Agreed date {_day(due)}"
        detail = f"The date agreed for this daily review finding is {_day(due)}."
    detail += f" Status: {str(status).lower()}."
    if not row["owned"]:
        detail += " No one is named to act on it yet."
    return Candidate(
        key=assignment_key(row["key"]),
        kind=DEADLINE,
        severity=WARNING,
        title=f"{heading}: {title}",
        detail=detail,
        due_date=due,
        etools_sections=[row["section"]] if (row["section"] or "").strip() else [],
        scope=COUNTRY,
        entity_kind=FINDING if len(row["key"]) <= HUB_KEY_MAX else "",
        entity_key=row["key"] if len(row["key"]) <= HUB_KEY_MAX else "",
        url=url,
        has_owner=bool(row["owned"]),
        assignment_status=row["status"],
        evidence=evidence(
            ctx,
            ASSIGNMENT_SOURCE,
            "",
            [record("Agreed date of the finding", due, str(status), url)],
            **_timing(due, ctx.today),
        ),
    )


def assignments_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Agreed dates no longer due: the assignment closed, its date removed or moved past 3 days, or the
    assignment deleted (NeuroDB's own table: a missing assignment is gone for sure)."""
    wanted = {item.key.removeprefix(ASSIGNMENT_PREFIX): item.key for item in items}
    until = ctx.today + datetime.timedelta(days=ASSIGNMENT_DAYS)
    closes: dict[str, Close] = {}
    rows = {row["key"]: row for row in _assignments(key__in=list(wanted))}
    for finding_key, key in wanted.items():
        row = rows.get(finding_key)
        if row is None:
            closes[key] = Close("the assignment was deleted")
        elif row["status"] == CLOSED:
            when = timezone.localdate(row["closed_at"]) if row["closed_at"] else None
            closes[key] = Close(f"closed on {_day(when)}" if when else "closed")
        elif row["due_date"] is None:
            closes[key] = Close("agreed date removed")
        elif row["due_date"] > until:
            closes[key] = Close(f"agreed date moved to {_day(row['due_date'])}")
    return closes


# ---------------------------------------------------------------------------- donor accounts
def donor_accounts_expiring(ctx: Context) -> Iterator[Candidate]:
    """Active donor accounts that stop working within 14 days."""
    rows = (
        DonorAccount.objects.filter(
            active=True,
            user__is_active=True,
            expires_on__gte=ctx.today,
            expires_on__lte=ctx.today + datetime.timedelta(days=DONOR_ACCOUNT_DAYS),
        )
        .order_by("expires_on", "pk")
        .values("pk", "name", "expires_on")  # never the contact, the user or the email
    )
    for row in rows:
        expires = row["expires_on"]
        left = (expires - ctx.today).days
        url = _admin("donors_donoraccount_change", row["pk"])
        yield Candidate(
            key=donor_account_key(row["pk"]),
            kind=DEADLINE,
            severity=WARNING if left <= DONOR_ACCOUNT_WARNING_DAYS else INFO,
            title=f"Donor account {row['name']} expires on {_day(expires)}",
            detail=(
                "The donor's sign-in stops working after that day. Extend it in the admin if the donor "
                "should keep access, or let it end."
            ),
            due_date=expires,
            scope=ADMINS,
            url=url,
            milestones=DONOR_ACCOUNT_MILESTONES,
            evidence=evidence(
                ctx,
                DONOR_SOURCE,
                "",
                [record("Donor account end date", expires, "active", url)],
                days_left=left,
            ),
        )


def donor_accounts_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Donor accounts no longer ending soon: extended, without an end date, switched off, expired or
    deleted (NeuroDB's own table: a missing account is gone for sure)."""
    wanted = {
        int(pk): item.key for item in items if (pk := item.key.removeprefix(DONOR_ACCOUNT_PREFIX)).isdigit()
    }
    accounts = DonorAccount.objects.filter(pk__in=list(wanted)).values(
        "pk", "active", "user__is_active", "expires_on"
    )
    found = {row["pk"]: row for row in accounts}
    until = ctx.today + datetime.timedelta(days=DONOR_ACCOUNT_DAYS)
    closes: dict[str, Close] = {}
    for pk, key in wanted.items():
        row = found.get(pk)
        if row is None:
            closes[key] = Close("the donor account was deleted")
        elif not row["active"] or not row["user__is_active"]:
            closes[key] = Close("the donor account was switched off")
        elif row["expires_on"] is None:
            closes[key] = Close("the donor account no longer has an end date")
        elif row["expires_on"] < ctx.today:
            closes[key] = Close(f"the donor account ended on {_day(row['expires_on'])}")
        elif row["expires_on"] > until:
            closes[key] = Close(f"extended to {_day(row['expires_on'])}")
    return closes


# ---------------------------------------------------------------------------- the yearly rollover
def rollover_year(today: datetime.date) -> int | None:
    """The year being rolled over to, from 15 December to 15 January (else None)."""
    if (today.month, today.day) >= ROLLOVER_FROM:
        return today.year + 1
    if (today.month, today.day) <= ROLLOVER_UNTIL:
        return today.year
    return None


def year_number(year: ReportingYear) -> int | None:
    """The calendar year of a reporting year (its ``year`` or ``name`` starts with four digits)."""
    for raw in (year.year, year.name):
        text = str(raw or "").strip()
        if text[:4].isdigit():
            return int(text[:4])
    return None


def _years() -> tuple[set[int], set[int]]:
    """The calendar years of the reporting years marked as current, and of every reporting year."""
    current: set[int] = set()
    every: set[int] = set()
    for year in ReportingYear.objects.order_by("pk"):
        number = year_number(year)
        if number is not None:
            every.add(number)
            if year.current:
                current.add(number)
    return current, every


def rollovers(ctx: Context) -> Iterator[Candidate]:
    """From 15 December to 15 January: the new year is not the current reporting year yet."""
    new = rollover_year(ctx.today)
    if new is None:
        return
    current, every = _years()
    if new in current:
        return
    starts = datetime.date(new, 1, 1)
    url = _admin("pivoting_reportingyear_changelist")
    shown = ", ".join(str(n) for n in sorted(current)) or "none"
    records = [
        record("Reporting year marked as current", None, shown, url),
        record(
            f"Reporting year {new}",
            None,
            "created, not marked current" if new in every else "not created yet",
            url,
        ),
    ]
    yield Candidate(
        key=rollover_key(new),
        kind=DEADLINE,
        severity=WARNING if ctx.today >= starts else INFO,
        title=f"Yearly rollover: make {new} the current reporting year from {_day(starts)}",
        detail=(
            "NeuroDB's pages and ActivityInfo imports follow the reporting year marked as current. Create "
            f"{new} in the admin (Reporting years) and mark it current, then add its ActivityInfo databases "
            "and population figures: see the yearly rollover steps in the operations guide."
        ),
        due_date=starts,
        scope=ADMINS,
        url=url,
        milestones=ROLLOVER_MILESTONES,
        evidence=evidence(ctx, ROLLOVER_SOURCE, "", records, **_timing(starts, ctx.today)),
    )


def rollovers_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """The new year is now current; or 15 January passed and it still is not (said so)."""
    current, _every = _years()
    closes: dict[str, Close] = {}
    for item in items:
        year = item.key.removeprefix(ROLLOVER_PREFIX)
        if not year.isdigit():
            continue
        year = int(year)
        if year in current:
            closes[item.key] = Close(f"{year} is now the current reporting year")
        elif ctx.today > datetime.date(year, *ROLLOVER_UNTIL):
            closes[item.key] = Close(
                f"the rollover period ended on {_day(datetime.date(year, *ROLLOVER_UNTIL))} with {year} "
                "still not marked current"
            )
    return closes


# ---------------------------------------------------------------------------- the registry
ASSIGNMENT_DUE = register(
    Detector(
        id="assignment_due",
        label="Agreed dates on findings",
        run=assignments_due,
        resolved=assignments_resolved,
        milestones=ASSIGNMENT_MILESTONES,
    )
)
DONOR_ACCOUNT_EXPIRING = register(
    Detector(
        id="donor_account_expiring",
        label="Donor accounts ending soon",
        run=donor_accounts_expiring,
        resolved=donor_accounts_resolved,
        milestones=DONOR_ACCOUNT_MILESTONES,
    )
)
REPORTING_YEAR_ROLLOVER = register(
    Detector(
        id="reporting_year_rollover",
        label="Yearly rollover of the reporting year",
        run=rollovers,
        resolved=rollovers_resolved,
        milestones=ROLLOVER_MILESTONES,
    )
)
