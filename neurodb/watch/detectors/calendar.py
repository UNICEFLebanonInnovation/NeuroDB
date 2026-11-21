"""Calendar checks: dates people agreed or set in NeuroDB itself, rather than in eTools.

- ``assignment_due``: a finding of the daily review was given an agreed date (``FindingAssignment``,
  set by the administrators in the management brief) that falls in the next 3 days or has passed,
  and the assignment is not closed. The title is the finding's own (written by code); the owner and
  the note are never read, only whether someone owns it and the assignment's status. Warning. Told to
  the staff of the finding's section and to the whole-country view. It closes at once when the
  assignment is closed or deleted, or its date is removed or moved past 3 days.
- ``donor_account_expiring``: a donor's sign-in that stops working within 14 days (warning from 3
  days before). Administrators only. It closes once the account is extended, switched off or expired.
- ``reporting_year_rollover``: from 15 December to 15 January (to note in December, warning from 1
  January), administrators only. NeuroDB's pages move to the new year by themselves; what may need a
  person is the **population figures** (``rollover:<year>:population``), while no population figures of
  the new year (or a later one) are loaded. It closes once they are, and on 16 January as missed. An item
  ``rollover:<year>`` raised before (the yearly set-up of a source NeuroDB no longer uses) closes as no
  longer followed.

They read NeuroDB's own tables, which are always up to date, so they have no source job to wait for.
Titles carry dates, never day counts. Every check starts in trial: only the whole-country view (for
the last two, the administrators) sees it until an administrator switches it on. Keys:
:func:`assignment_key`, :func:`donor_account_key`, :func:`rollover_key` and :func:`population_key`.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

from django.db.models import BooleanField, Case, Value, When
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.donors.models import DonorAccount
from neurodb.review.checks import link
from neurodb.review.models import FindingAssignment, ReviewFinding

from ..models import WatchItem, fit_key
from . import (
    ADMINS,
    CHANGED,
    COUNTRY,
    DEADLINE,
    INFO,
    MISSED,
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
POPULATION_SOURCE = "NeuroDB population figures"

ASSIGNMENT_PREFIX = "assignment:"
DONOR_ACCOUNT_PREFIX = "donor_account:"
ROLLOVER_PREFIX = "rollover:"
POPULATION_SUFFIX = ":population"
NO_LONGER_FOLLOWED = "no longer followed: the yearly rollover only asks for the population figures"
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
            closes[key] = Close("the assignment was deleted", kind=CHANGED)
        elif row["status"] == CLOSED:
            when = timezone.localdate(row["closed_at"]) if row["closed_at"] else None
            closes[key] = Close(f"closed on {_day(when)}" if when else "closed")
        elif row["due_date"] is None:
            closes[key] = Close("agreed date removed", kind=CHANGED)
        elif row["due_date"] > until:
            closes[key] = Close(f"agreed date moved to {_day(row['due_date'])}", kind=CHANGED)
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
            closes[key] = Close("the donor account was deleted", kind=CHANGED)
        elif not row["active"] or not row["user__is_active"]:
            closes[key] = Close("the donor account was switched off", kind=CHANGED)
        elif row["expires_on"] is None:
            closes[key] = Close("the donor account no longer has an end date")
        elif row["expires_on"] < ctx.today:
            closes[key] = Close(f"the donor account ended on {_day(row['expires_on'])}", kind=CHANGED)
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


def population_key(year: int) -> str:
    """The population figures for ``year``: ``rollover:<year>:population``."""
    return f"{rollover_key(year)}{POPULATION_SUFFIX}"


def _population_years(ctx: Context) -> list[int]:
    from neurodb.core.services import population

    return ctx.memo("calendar:population_years", population.available_years)


def rollovers(ctx: Context) -> Iterator[Candidate]:
    """From 15 December to 15 January: the new year's population figures, while none are loaded."""
    new = rollover_year(ctx.today)
    if new is None:
        return
    starts = datetime.date(new, 1, 1)
    severity = WARNING if ctx.today >= starts else INFO
    timing = _timing(starts, ctx.today)
    if any(year >= new for year in _population_years(ctx)):
        return
    url = link("reports:population")
    newest = max(_population_years(ctx), default=None)
    records = [record("Newest population figures", None, str(newest) if newest else "none", url)]
    yield Candidate(
        key=population_key(new),
        kind=DEADLINE,
        severity=severity,
        title=f"Population figures for {new}",
        detail=(
            # Administrators have no shell and the admin only reloads bundled files, so the text says
            # who adds a new year's file (step 5: a deploy; the start-up load picks it up).
            f"NeuroDB's pages move to {new} by themselves on 1 January. When the newest population "
            "estimates are published, ask the NeuroDB team to add the year's file; it loads by itself at "
            "the next start (step 5 of the yearly rollover in the operations guide)."
        ),
        due_date=starts,
        scope=ADMINS,
        url=url,
        milestones=ROLLOVER_MILESTONES,
        evidence=evidence(ctx, POPULATION_SOURCE, "", records, **timing),
    )


def rollovers_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Population: figures for the new year (or a later one) were added; after 15 January, the period ended
    (missed). An item of the yearly set-up NeuroDB no longer follows (``rollover:<year>``): closed at once."""
    closes: dict[str, Close] = {}
    for item in items:
        rest = item.key.removeprefix(ROLLOVER_PREFIX)
        population = rest.endswith(POPULATION_SUFFIX)
        year = rest.removesuffix(POPULATION_SUFFIX)
        if not year.isdigit():
            continue
        year = int(year)
        until = datetime.date(year, *ROLLOVER_UNTIL)
        if population:
            if any(found >= year for found in _population_years(ctx)):
                closes[item.key] = Close(f"population figures for {year} were added")
            elif ctx.today > until:
                closes[item.key] = Close(
                    f"the rollover period ended on {_day(until)} without population figures for {year}",
                    kind=MISSED,
                )
            continue
        closes[item.key] = Close(NO_LONGER_FOLLOWED, kind=CHANGED)
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
