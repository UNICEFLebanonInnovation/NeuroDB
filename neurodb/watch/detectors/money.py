"""Money and assurance checks: grants and funds reservations ending with money left, and the HACT
assurance still to do before the year ends.

- ``grant_expiring``: a grant expiring in the next 90 days with at least ``WATCH_GRANT_MIN_UNSPENT``
  USD unspent. The unspent balance is the management brief's own figure, read through the same two
  functions (:func:`neurodb.reports.brief.fr_lines` over the PDs running this year, then
  :func:`~neurodb.reports.brief.grant_balances`), so the brief and the watch never disagree. To note,
  warning from 30 days before, critical from 14. Told to the sections of the PDs it funds and to the
  whole-country view. It closes once the grant expired, its expiry moved past 90 days or its unspent
  money fell below the amount followed.
- ``fr_expiring``: a funds reservation (FR) ending in the next 30 days with money outstanding and not
  completed. To note, warning from 7 days before. Told to the sections of its PD. It closes once the
  FR is completed, nothing is outstanding, its end date moved or passed.
- ``hact_assurance_gap``: from 1 October to 31 December, a partner whose programmatic visits, spot
  checks or audits completed this year are below what HACT requires, before the year-end deadline.
  To note in October, warning from 15 November, critical from 15 December. Told to the sections of the
  partner's active PDs and to the whole-country view. It closes once the assurance is complete, or
  when the year ends (saying what was still missing).

All three read only tables the eTools Datamart sync refreshes, and run only while that sync is fresh.
None of them reads a field that holds a person. Titles carry dates and amounts, never day counts.
Every check starts in trial: only the whole-country view sees it until an administrator switches it
on. Keys come from lasting identifiers: :func:`grant_key`, :func:`fr_key` and :func:`hact_key`.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Q

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart.monitoring import ACTIVE_PD_STATUSES
from neurodb.partnerships.models import PCA
from neurodb.reports.brief import fr_lines, grant_balances
from neurodb.review.checks import link, pd_label, plural
from neurodb.web.templatetags.ui import half_up

from ..models import RECORDS_MAX, WatchItem, fit_key
from . import (
    CHANGED,
    CONCERN,
    COUNTRY,
    CRITICAL,
    DEADLINE,
    INFO,
    MISSED,
    RESOLVED,
    WARNING,
    Candidate,
    Close,
    Context,
    Detector,
    evidence,
    record,
    register,
)

DATAMART = SyncRun.Job.ETOOLS_DATAMART
PROGRAMME = "programme_document"  # the knowledge hub kinds the items are about
PARTNER = "partner"
GRANT = "grant"
NOT_RUNNING = ("draft", "cancelled")  # PD statuses that never count as running in a year

GRANT_DAYS = 90  # grants expiring within this many days
GRANT_WARNING_DAYS = 30
GRANT_CRITICAL_DAYS = 14
GRANT_MILESTONES = (90, 60, 30, 14)
FR_DAYS = 30  # funds reservations ending within this many days
FR_WARNING_DAYS = 7
FR_MILESTONES = (30, 14, 7)
HACT_FROM = (10, 1)  # (month, day): the assurance gaps are raised from 1 October...
HACT_WARNING_FROM = (11, 15)  # ...a warning from 15 November...
HACT_CRITICAL_FROM = (12, 15)  # ...critical from 15 December, until the year ends

GRANT_SOURCE = "eTools grants and funds reservations"
FR_SOURCE = "eTools funds reservations"
HACT_SOURCE = "eTools HACT assurance per partner"

# The only PD columns read; none holds a person
PD_FIELDS = (
    "id",
    "number",
    "partner",
    "partner__name",
    "partner_name",
    "status",
    "section_names",
    "sections",
)
# What HACT requires and what was done, per kind of assurance: (label, plural, required, completed)
ASSURANCE = (
    ("programmatic visit", "programmatic visits", "pv_required", "pv_completed"),
    ("spot check", "spot checks", "sc_required", "sc_completed"),
    ("audit", "audits", "audits_required", "audits_completed"),
)


# ---------------------------------------------------------------------------- keys
def grant_key(name: str) -> str:
    """A grant, by its number: ``due:grant:<grant>``."""
    return fit_key(f"due:grant:{str(name).strip()}")


def fr_key(fr_number: str, datamart_id: int | None = None) -> str:
    """A funds reservation, by its FR number (else its Datamart id): ``due:fr:<FR number>``."""
    number = str(fr_number or "").strip()
    return fit_key(f"due:fr:{number}" if number else f"due:fr:id:{datamart_id}")


def hact_key(year: int, partner_id: int | None, vendor_number: str = "") -> str:
    """A partner's HACT year: ``hact:<year>:<partner pk>`` (``vendor:<vendor number>`` when the row is
    not linked to a partner). Empty when it has neither."""
    if partner_id:
        return fit_key(f"hact:{year}:{partner_id}")
    vendor = str(vendor_number or "").strip()
    return fit_key(f"hact:{year}:vendor:{vendor}") if vendor else ""


# ---------------------------------------------------------------------------- shared lookups
def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def _usd(value: float | Decimal | None) -> str:
    return f"{float(value or 0):,.0f} USD"


def _known(**numbers: Any) -> dict[str, Any]:
    """The evidence numbers that are known (a missing one is left out, never written as None)."""
    return {name: value for name, value in numbers.items() if value is not None}


def _sections(pd: PCA) -> list[str]:
    """The PD's eTools section names (the names the review falls back on when one is missing)."""
    return [name for name in (pd.section_names or pd.sections or []) if str(name or "").strip()]


def _pds(ids: Iterable[int]) -> dict[int, PCA]:
    """Programme documents by id, with only the fields the checks use (never the focal points)."""
    ids = {int(pk) for pk in ids if pk}
    if not ids:
        return {}
    return {pd.pk: pd for pd in PCA.objects.select_related("partner").only(*PD_FIELDS).filter(pk__in=ids)}


def _union(lists: Iterable[list[str]]) -> list[str]:
    """Section names once each, in the order they first come."""
    return list(dict.fromkeys(name for names in lists for name in names))


# ---------------------------------------------------------------------------- grants
def running_pd_ids(year: int) -> list[int]:
    """The PDs running in ``year`` (dates overlapping it, not draft or cancelled): the PDs whose funds
    reservations make up a grant's unspent balance. The brief reads the same year's PDs (those with
    indicators in the year)."""
    first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    return list(
        PCA.objects.exclude(status__in=NOT_RUNNING)
        .filter(Q(start__isnull=True) | Q(start__lte=last), Q(end__isnull=True) | Q(end__gte=first))
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def balances(ctx: Context) -> dict[str, dict[str, Any]]:
    """Every grant's balance this year (:func:`~neurodb.reports.brief.grant_balances` of the FR lines
    of the PDs running in the year), read once per pass."""

    def load() -> dict[str, dict[str, Any]]:
        year = ctx.today.year
        _per_pd, lines = fr_lines(running_pd_ids(year), year)
        return grant_balances(lines)

    return ctx.memo("money:grant_balances", load)


def unspent_of(balance: dict[str, Any]) -> float:
    """A grant's unspent balance as the brief shows it (to the cent, rounded half up)."""
    return half_up(balance["unspent"], 2)


def _grants(names: Iterable[str]) -> dict[str, dm.Grant]:
    """The grants table rows by name (the brief reads the expiry the same way)."""
    return {g.name: g for g in dm.Grant.objects.filter(name__in=list(names)).order_by("datamart_id")}


def _fr_ends(ctx: Context) -> dict[str, datetime.date | None]:
    """The end date of each funds reservation behind a grant, read once per pass."""

    def load() -> dict[str, datetime.date | None]:
        numbers = {fr for balance in balances(ctx).values() for fr in balance["frs"]}
        rows = dm.FundsReservationHeader.objects.filter(fr_number__in=numbers).order_by("end_date")
        return dict(rows.values_list("fr_number", "end_date"))

    return ctx.memo("money:fr_ends", load)


def grants_expiring(ctx: Context) -> Iterator[Candidate]:
    """Grants expiring in the next 90 days with at least ``WATCH_GRANT_MIN_UNSPENT`` USD unspent."""
    found = balances(ctx)
    grants = _grants(found)
    until = ctx.today + datetime.timedelta(days=GRANT_DAYS)
    minimum = settings.WATCH_GRANT_MIN_UNSPENT
    expiring = {
        name: balance
        for name, balance in found.items()
        if (grant := grants.get(name)) is not None
        and grant.expiry is not None
        and ctx.today <= grant.expiry <= until
        and unspent_of(balance) >= minimum
    }
    pds = _pds(pk for balance in expiring.values() for pk in balance["pd_ids"])
    for name, balance in expiring.items():
        yield _grant(ctx, name, grants[name], balance, pds)


def _grant(
    ctx: Context, name: str, grant: dm.Grant, balance: dict[str, Any], pds: dict[int, PCA]
) -> Candidate:
    expiry = grant.expiry
    left = (expiry - ctx.today).days
    unspent = unspent_of(balance)
    reserved = half_up(balance["reserved"], 2)
    donor = (balance["donor"] or grant.donor or "").strip()
    funded = [pds[pk] for pk in balance["pd_ids"] if pk in pds]
    url = link("reports:funds", grant=name)
    if left <= GRANT_CRITICAL_DAYS:
        severity = CRITICAL
    elif left <= GRANT_WARNING_DAYS:
        severity = WARNING
    else:
        severity = INFO
    detail = (
        f"{_usd(unspent)} of the {_usd(reserved)} its funds reservations hold this year is not disbursed yet."
    )
    if funded:
        shown = ", ".join(pd_label(pd) for pd in funded[:3])
        more = f" and {len(funded) - 3} more" if len(funded) > 3 else ""
        detail += f" It funds {len(funded)} {plural(len(funded), 'PD')}: {shown}{more}."
    detail += (
        " Plan how it will be spent in time, or ask the donor early for an extension or a reprogramming."
    )
    ends = _fr_ends(ctx)
    records = [
        record(
            f"Grant {name} expiry" + (f" ({donor})" if donor else ""), expiry, _usd(unspent) + " unspent", url
        )
    ]
    for fr, amount in sorted(balance["frs"].items(), key=lambda kv: (-kv[1], kv[0]))[: RECORDS_MAX - 1]:
        records.append(
            record(
                f"FR {fr}", ends.get(fr), f"{_usd(half_up(amount, 2))} unspent", link("reports:funds", q=fr)
            )
        )
    return Candidate(
        key=grant_key(name),
        kind=DEADLINE,
        severity=severity,
        title=f"Grant {name}"
        + (f" ({donor})" if donor else "")
        + f" expires {_day(expiry)} with {_usd(unspent)} unspent",
        detail=detail,
        due_date=expiry,
        etools_sections=_union(_sections(pd) for pd in funded),
        scope=COUNTRY,
        entity_kind=GRANT,
        entity_key=str(name).strip(),
        url=url,
        evidence=evidence(
            ctx,
            GRANT_SOURCE,
            DATAMART,
            records,
            unspent=unspent,
            reserved=reserved,
            pds=len(funded),
            days_left=left,
        ),
    )


def grants_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Grants no longer expiring soon with money left: expired, given a later expiry, or with less
    unspent than the amount followed. A grant gone from eTools gets no reason: it is missed, then gone."""
    names = {item.key: item.key.removeprefix("due:grant:") for item in items}
    found = balances(ctx)
    grants = _grants(names.values())
    until = ctx.today + datetime.timedelta(days=GRANT_DAYS)
    minimum = settings.WATCH_GRANT_MIN_UNSPENT
    closes: dict[str, Close] = {}
    for key, name in names.items():
        grant = grants.get(name)
        if grant is None or grant.expiry is None:
            continue
        unspent = unspent_of(found[name]) if name in found else 0.0
        if grant.expiry < ctx.today:
            left = f" with {_usd(unspent)} unspent" if unspent else ""
            closes[key] = Close(
                f"expired on {_day(grant.expiry)}{left}", kind=MISSED if unspent else RESOLVED
            )
        elif grant.expiry > until:
            closes[key] = Close(f"expiry moved to {_day(grant.expiry)}", kind=CHANGED)
        elif unspent < minimum:
            closes[key] = Close(
                f"{_usd(unspent)} left unspent on this year's funds reservations, below the {_usd(minimum)} "
                "followed"
                if unspent
                else "nothing left unspent on this year's funds reservations"
            )
    return closes


# ---------------------------------------------------------------------------- funds reservations
FR_FIELDS = (
    "datamart_id",
    "fr_number",
    "intervention_id",
    "total_amt",
    "actual_amt",
    "outstanding_amt",
    "end_date",
    "completed_flag",
)


def frs_expiring(ctx: Context) -> Iterator[Candidate]:
    """Funds reservations ending in the next 30 days, not completed, with money outstanding."""
    rows = list(
        dm.FundsReservationHeader.objects.filter(
            completed_flag=False,
            outstanding_amt__gt=0,
            end_date__gte=ctx.today,
            end_date__lte=ctx.today + datetime.timedelta(days=FR_DAYS),
        )
        .order_by("end_date", "fr_number", "datamart_id")
        .values(*FR_FIELDS)
    )
    pds = _pds(row["intervention_id"] for row in rows)
    for row in rows:
        yield _fr(ctx, row, pds.get(row["intervention_id"]))


def _fr(ctx: Context, row: dict[str, Any], pd: PCA | None) -> Candidate:
    end = row["end_date"]
    left = (end - ctx.today).days
    number = (row["fr_number"] or "").strip()
    name = f"Funds reservation {number}" if number else "A funds reservation"
    outstanding, total = row["outstanding_amt"] or Decimal(0), row["total_amt"] or Decimal(0)
    url = link("reports:funds", q=number) if number else link("reports:funds")
    return Candidate(
        key=fr_key(number, row["datamart_id"]),
        kind=DEADLINE,
        severity=WARNING if left <= FR_WARNING_DAYS else INFO,
        title=f"{name} ends {_day(end)} with {_usd(outstanding)} outstanding"
        + (f": {pd_label(pd)}" if pd else ""),
        detail=(
            f"{_usd(outstanding)} of the {_usd(total)} it reserves is not disbursed yet. Disburse or "
            "liquidate what is due, or extend the reservation before it ends."
        ),
        due_date=end,
        etools_sections=_sections(pd) if pd else [],
        entity_kind=PROGRAMME if pd else "",
        entity_key=str(pd.pk) if pd else "",
        url=url,
        evidence=evidence(
            ctx,
            FR_SOURCE,
            DATAMART,
            [
                record(
                    f"FR {number}" if number else "Funds reservation",
                    end,
                    f"{_usd(outstanding)} outstanding",
                    url,
                )
            ],
            outstanding=round(outstanding),
            reserved=round(total),
            disbursed=round(row["actual_amt"] or 0),
            days_left=left,
        ),
    )


def frs_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Funds reservations no longer ending soon with money outstanding: completed, nothing outstanding,
    given a later end date, or ended. One gone from eTools gets no reason: it is missed, then gone."""
    wanted = {item.key for item in items}
    numbers = [key.removeprefix("due:fr:") for key in wanted if not key.startswith("due:fr:id:")]
    ids = [
        int(key.removeprefix("due:fr:id:"))
        for key in wanted
        if key.startswith("due:fr:id:") and key.removeprefix("due:fr:id:").isdigit()
    ]
    rows = dm.FundsReservationHeader.objects.filter(Q(fr_number__in=numbers) | Q(datamart_id__in=ids))
    found = {
        fr_key(row["fr_number"], row["datamart_id"]): row
        for row in rows.order_by("datamart_id").values(*FR_FIELDS)
    }
    until = ctx.today + datetime.timedelta(days=FR_DAYS)
    closes: dict[str, Close] = {}
    for key in wanted:
        row = found.get(key)
        if row is None:
            continue
        outstanding = row["outstanding_amt"] or Decimal(0)
        end = row["end_date"]
        if row["completed_flag"]:
            closes[key] = Close("completed in eTools")
        elif outstanding <= 0:
            closes[key] = Close("nothing outstanding any more: disbursed or liquidated")
        elif end is not None and end < ctx.today:
            closes[key] = Close(
                f"ended on {_day(end)} with {_usd(outstanding)} still outstanding", kind=MISSED
            )
        elif end is not None and end > until:
            closes[key] = Close(f"end date moved to {_day(end)}", kind=CHANGED)
    return closes


# ---------------------------------------------------------------------------- HACT assurance
def hact_season(today: datetime.date) -> bool:
    """The assurance gaps are raised from 1 October to 31 December."""
    return (today.month, today.day) >= HACT_FROM


def _hact_severity(today: datetime.date) -> str:
    if (today.month, today.day) >= HACT_CRITICAL_FROM:
        return CRITICAL
    if (today.month, today.day) >= HACT_WARNING_FROM:
        return WARNING
    return INFO


def gaps(row: dm.PartnerHACTYear) -> list[tuple[str, int, int]]:
    """What HACT still requires of a partner's year: (what, required, completed) for each kind of
    assurance completed below what is required."""
    found = []
    for single, many, required_field, completed_field in ASSURANCE:
        required = getattr(row, required_field) or 0
        completed = getattr(row, completed_field) or 0
        if required > completed:
            missing = required - completed
            found.append((f"{missing} {single if missing == 1 else many}", required, completed))
    return found


def _hact_rows(year: int) -> dict[str, dm.PartnerHACTYear]:
    """The partners' HACT rows of ``year`` by key (the latest synced row when eTools sends two)."""
    rows = dm.PartnerHACTYear.objects.filter(year=year).select_related("partner")
    found: dict[str, dm.PartnerHACTYear] = {}
    for row in rows.order_by("-last_modify_date", "-datamart_id"):
        key = hact_key(year, row.partner_id, row.vendor_number)
        if key:
            found.setdefault(key, row)
    return found


def _partner_sections(ctx: Context) -> dict[int, list[str]]:
    """The eTools section names of each partner's active PDs, read once per pass."""

    def load() -> dict[int, list[str]]:
        sections: dict[int, list[list[str]]] = defaultdict(list)
        pds = PCA.objects.filter(status__in=ACTIVE_PD_STATUSES, partner_id__isnull=False).only(
            "id", "partner", "section_names", "sections"
        )
        for pd in pds.order_by("pk"):
            sections[pd.partner_id].append(_sections(pd))
        return {partner: _union(names) for partner, names in sections.items()}

    return ctx.memo("money:partner_sections", load)


def hact_assurance_gaps(ctx: Context) -> Iterator[Candidate]:
    """From 1 October, partners whose HACT assurance this year is below what is required."""
    if not hact_season(ctx.today):
        return
    year = ctx.today.year
    sections = _partner_sections(ctx)
    for key, row in _hact_rows(year).items():
        missing = gaps(row)
        if missing:
            yield _hact(ctx, key, row, missing, sections.get(row.partner_id, []) if row.partner_id else [])


def _hact(
    ctx: Context, key: str, row: dm.PartnerHACTYear, missing: list[tuple[str, int, int]], sections: list[str]
) -> Candidate:
    year = row.year
    deadline = datetime.date(year, 12, 31)
    partner = (row.partner.name if row.partner_id and row.partner else row.partner_name) or "a partner"
    url = (
        link("reports:partner_profile", row.partner_id)
        if row.partner_id
        else link("reports:assurance", hact_year=year)
    )
    todo = ", ".join(what for what, _required, _completed in missing)
    records = []
    for _single, many, required_field, completed_field in ASSURANCE:
        required = getattr(row, required_field)
        if required:
            completed = getattr(row, completed_field) or 0
            records.append(
                record(f"{many.capitalize()} in {year}", None, f"{completed} of {required} completed", url)
            )
    numbers = {
        field: getattr(row, field)
        for _single, _many, required_field, completed_field in ASSURANCE
        for field in (required_field, completed_field)
    }
    return Candidate(
        key=key,
        kind=CONCERN,
        severity=_hact_severity(ctx.today),
        title=f"HACT assurance due by {_day(deadline)} for {partner}: {todo} to go",
        detail=(
            f"eTools shows {todo} still to be completed for {partner} this year. The HACT assurance plan "
            f"must be completed by {_day(deadline)}: plan the visits, spot checks or audits now."
        ),
        due_date=deadline,
        etools_sections=sections,
        scope=COUNTRY,
        entity_kind=PARTNER if row.partner_id else "",
        entity_key=str(row.partner_id) if row.partner_id else "",
        url=url,
        evidence=evidence(
            ctx,
            HACT_SOURCE,
            DATAMART,
            records,
            **_known(**numbers),
            days_left=(deadline - ctx.today).days,
        ),
    )


def hact_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Assurance gaps closed: everything required done, or the year over (saying what was still to
    do). A partner gone from eTools gets no reason: it is missed, then gone."""
    years: dict[int, list[str]] = defaultdict(list)
    for item in items:
        _prefix, year, _rest = (item.key.split(":", 2) + ["", ""])[:3]
        if year.isdigit():
            years[int(year)].append(item.key)
    closes: dict[str, Close] = {}
    for year, keys in years.items():
        rows = _hact_rows(year)
        for key in keys:
            row = rows.get(key)
            if row is None:
                continue
            missing = gaps(row)
            if not missing:
                closes[key] = Close("assurance complete: every required visit, spot check and audit is done")
            elif year < ctx.today.year:
                todo = ", ".join(what for what, _required, _completed in missing)
                closes[key] = Close(
                    f"the {year} HACT year ended on 31 Dec {year} with {todo} not done", kind=MISSED
                )
    return closes


# ---------------------------------------------------------------------------- the registry
GRANT_EXPIRING = register(
    Detector(
        id="grant_expiring",
        label="Grants expiring with money unspent",
        run=grants_expiring,
        resolved=grants_resolved,
        source_jobs=(DATAMART,),
        milestones=GRANT_MILESTONES,
    )
)
FR_EXPIRING = register(
    Detector(
        id="fr_expiring",
        label="Funds reservations ending with money outstanding",
        run=frs_expiring,
        resolved=frs_resolved,
        source_jobs=(DATAMART,),
        milestones=FR_MILESTONES,
    )
)
HACT_ASSURANCE_GAP = register(
    Detector(
        id="hact_assurance_gap",
        label="HACT assurance still to do this year",
        run=hact_assurance_gaps,
        resolved=hact_resolved,
        source_jobs=(DATAMART,),
    )
)
