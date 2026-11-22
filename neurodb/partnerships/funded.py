"""Programme documents' two funded tabs: the old NeuroDB's *Summary Info (Active PDs / All PDs)* and
*Funded Programs* pages, on v3 data.

* **Amounts.** eTools writes a programme document's budget as text (``"1,585,122.10"``): it is read as a
  number in the database (:func:`amount`; a text that is not a number counts as no amount). *Funds
  reserved* is the sum of the programme document's funds reservation lines
  (``FundsReservation.overall_amount``, US dollars: what ``PCA.donors_set`` is built from) and *Disbursed*
  the sum of its funds reservations' ``actual_amt`` (US dollars), as on the Funds page. Budgets are in the
  programme document's currency, and amounts in different currencies are never added: the sums are given
  per budget currency.
* **Which programme documents** (:func:`programme_documents`): every one but drafts and cancelled ones
  (``statuses.NOT_A_PARTNERSHIP``: not, or no longer, a partnership), as Funds > Funded Programs reads them,
  so that the two pages' figures for the same programme documents agree.
* **Summary info** (:func:`summary`): the programme documents of the scope (Active: the eTools status
  active; All: every one of :func:`programme_documents`) summed by partner and by section, each in its budget
  currency, in the database. A programme document counts once for its partner and once under each of its
  sections (so the section rows add up to more than the total); the active ones ending within
  :data:`ENDING_DAYS` days; totals per currency. A partner is one partner however its programme documents
  spell its name, and also when one of them has no partner link but its name (:func:`_by_partner`).
* **Overall** (:func:`overall_rows`, :func:`overall_totals`): one row per programme document of the
  filter, with its budgets, UNICEF cash, funds reserved and disbursed.
* **Speed.** The sums are read with PostgreSQL's just-in-time compiler off (:func:`_grouped`): its row
  guesses for these queries are far too high, and once there are a few thousand programme documents it
  spends half a second compiling a query that runs in a few hundredths.

No person is ever read here: neither the UNICEF focal points nor the partner's (the rows name their
columns).
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from django.db import connection, transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    F,
    OuterRef,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Replace, Trim
from django.utils.translation import gettext as _

from neurodb.datamart import models as dm
from neurodb.datamart.funds import STATUS_TONES

from . import statuses
from .models import PCA, PartnerOrganization

ENDING_DAYS = 90  # the summary's list of active programme documents ending soon
ENDING_SOON_DAYS = 30  # the old page's window ("PDs ending within 1 month"), counted in the tile's hint
MONEY = DecimalField(max_digits=30, decimal_places=2)
# an amount as eTools writes it: digits with thousands commas and decimals; at most 27 characters before the
# point, so that it always fits MONEY
NUMBER = r"^\s*-?[0-9][0-9,]{0,26}(\.[0-9]*)?\s*$"


def programme_documents(filters: Any = None, *, scope: str = "all") -> QuerySet[PCA]:
    """The programme documents of the two tabs: those of the Programme documents tab
    (:func:`neurodb.partnerships.services.programme_documents`) without the cancelled ones, as Funds leaves
    out ``statuses.NOT_A_PARTNERSHIP``."""
    from .services import programme_documents as listed

    return listed(filters, scope=scope).exclude(status__in=statuses.NOT_A_PARTNERSHIP)


def filter_options() -> dict[str, Any]:
    """The overall tab's filter lists: the Programme documents tab's, without the statuses it never lists."""
    from .services import pd_filter_options

    options = pd_filter_options()
    options["statuses"] = [s for s in options["statuses"] if s not in statuses.NOT_A_PARTNERSHIP]
    return options


def amount(field: str) -> Case:
    """The text column ``field`` as a number, in the database; None when it is not one."""
    cleaned = Replace(Trim(F(field)), Value(","), Value(""))
    return Case(
        When(**{f"{field}__regex": NUMBER}, then=Cast(cleaned, MONEY)),
        default=Value(None, output_field=MONEY),
        output_field=MONEY,
    )


def _sum_per_pd(model, field: str) -> Subquery:
    return Subquery(
        model.objects.filter(intervention=OuterRef("pk"))
        .order_by()
        .values("intervention")
        .annotate(total=Sum(field))
        .values("total"),
        output_field=MONEY,
    )


def with_amounts(qs: QuerySet[PCA]) -> QuerySet[PCA]:
    """``qs`` with each programme document's amounts: its budget currency (``f_currency``), UNICEF
    planned budget (``f_unicef``), total budget (``f_total``), UNICEF cash (``f_cash``), funds reserved
    (``f_reserved``) and disbursed (``f_disbursed``)."""
    return qs.annotate(
        f_currency=Coalesce(Trim("budget_currency"), Value(""), output_field=CharField()),
        f_unicef=amount("total_unicef_budget"),
        f_total=amount("total_budget"),
        f_cash=amount("unicef_cash"),
        f_reserved=_sum_per_pd(dm.FundsReservation, "overall_amount"),
        f_disbursed=_sum_per_pd(dm.FundsReservationHeader, "actual_amt"),
    )


def days_to_end(end: datetime.date | None, today: datetime.date) -> int | None:
    """Days from today to the end date, −1 once it has ended (the Funds page's rule); None without one."""
    if end is None:
        return None
    return -1 if end < today else (end - today).days


def days_tone(days: int | None) -> str:
    """The old page's colours of the days left: green over 120, amber over 60, red below."""
    if days is None or days < 0:
        return ""
    return "on_track" if days > 120 else "stale" if days > 60 else "off_track"


# ------------------------------------------------------------------------------- sums in the database
def _grouped(qs: QuerySet[PCA], select: str) -> list[dict[str, Any]]:
    """Run ``select`` over ``amounts``: one row per programme document of ``qs`` with its partner,
    currency, sections, budgets and its funds reserved and disbursed (the funds reservation tables read
    once, joined)."""
    inner = (
        qs.select_related(None)
        .order_by()
        .annotate(
            f_currency=Coalesce(Trim("budget_currency"), Value(""), output_field=CharField()),
            f_unicef=amount("total_unicef_budget"),
            f_total=amount("total_budget"),
        )
        .values("id", "partner_id", "partner_name", "section_names", "f_currency", "f_unicef", "f_total")
    )
    sql, params = inner.query.sql_with_params()
    lines, headers = dm.FundsReservation._meta.db_table, dm.FundsReservationHeader._meta.db_table
    query = (
        f"WITH pd AS ({sql}), "  # noqa: S608 - the tables are the models' own, the values are parameters
        f"fr AS (SELECT f.intervention_id AS id, SUM(f.overall_amount) AS reserved FROM {lines} f "
        "JOIN pd ON pd.id = f.intervention_id GROUP BY 1), "
        f"hd AS (SELECT h.intervention_id AS id, SUM(h.actual_amt) AS disbursed FROM {headers} h "
        "JOIN pd ON pd.id = h.intervention_id GROUP BY 1), "
        "amounts AS (SELECT pd.*, fr.reserved, hd.disbursed FROM pd LEFT JOIN fr ON fr.id = pd.id "
        "LEFT JOIN hd ON hd.id = pd.id) " + select
    )
    # the just-in-time compiler off for this query alone (SET LOCAL ends with the transaction): the planner's
    # guesses over the CTEs are millions of rows, so with a few thousand programme documents it compiles for
    # half a second a query that runs in a few hundredths
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL jit = off")
        cursor.execute(query, params)
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# the queries over ``amounts`` (:func:`_grouped`): constant texts, no value of a request in them
SUMS = (
    "COUNT(*) AS pds, SUM(a.f_unicef) AS unicef_planned, SUM(a.f_total) AS total, "
    "SUM(a.reserved) AS reserved, SUM(a.disbursed) AS disbursed"
)
# by partner link and name as written, put together by :func:`_by_partner`
BY_PARTNER = (
    "SELECT a.partner_id, COALESCE(NULLIF(btrim(a.partner_name), ''), '') AS partner, "  # noqa: S608
    f"a.f_currency AS currency, {SUMS} FROM amounts a GROUP BY 1, 2, 3"
)
# each programme document under each of its sections (a section named twice once), or under none
BY_SECTION = (
    f"SELECT COALESCE(s.section, '') AS section, a.f_currency AS currency, {SUMS} "  # noqa: S608
    "FROM amounts a LEFT JOIN LATERAL (SELECT DISTINCT btrim(x) AS section FROM unnest(a.section_names) AS x "
    "WHERE btrim(x) <> '') s ON true GROUP BY 1, 2"
)
BY_CURRENCY = f"SELECT a.f_currency AS currency, {SUMS} FROM amounts a GROUP BY 1"  # noqa: S608


def _by_partner(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The :data:`BY_PARTNER` rows, one per partner and currency. A partner is its eTools partner link, shown
    under the partner's current name, whatever each programme document's spelling of it; a programme document
    without a link joins the partner whose name it bears (case and spaces aside), else counts under its
    name; one with neither (``partner_id`` None, ``partner`` "") stays apart, as "Unknown partner"."""
    ids = sorted({r["partner_id"] for r in rows if r["partner_id"] is not None})
    names = dict(PartnerOrganization.objects.filter(pk__in=ids).values_list("id", "name")) if ids else {}
    by_name: dict[str, set[int]] = {}
    for row in rows:
        if row["partner_id"] is not None:
            for name in (row["partner"], names.get(row["partner_id"]) or ""):
                if name.strip():
                    by_name.setdefault(" ".join(name.split()).casefold(), set()).add(row["partner_id"])
    merged: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        folded = " ".join(row["partner"].split()).casefold()
        partner_id = row["partner_id"]
        if partner_id is None and len(by_name.get(folded, ())) == 1:
            partner_id = next(iter(by_name[folded]))
        key = (partner_id if partner_id is not None else ("name", folded), row["currency"])
        entry = merged.get(key)
        if entry is None:
            name = (names.get(partner_id) or "").strip() if partner_id is not None else ""
            merged[key] = {**row, "partner_id": partner_id, "partner": name or row["partner"]}
            continue
        entry["pds"] += row["pds"]
        for field in ("unicef_planned", "total", "reserved", "disbursed"):
            if row[field] is not None:
                entry[field] = (entry[field] or Decimal(0)) + row[field]
    return list(merged.values())


def _partner_count(rows: list[dict[str, Any]]) -> int:
    """The partners of :func:`_by_partner`'s rows: each once whatever its currencies, the blank one not."""
    return len(
        {
            r["partner_id"] if r["partner_id"] is not None else " ".join(r["partner"].split()).casefold()
            for r in rows
            if r["partner_id"] is not None or r["partner"].strip()
        }
    )


def _order(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The most programme documents first (the old pages' order), then by name and currency."""
    return sorted(rows, key=lambda r: (-r["pds"], (r[name] == ""), r[name].casefold(), r["currency"]))


def _funds(totals: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """The funds reserved and disbursed of every currency's programme documents (both in US dollars)."""
    reserved = sum((t["reserved"] or Decimal(0) for t in totals), Decimal(0))
    disbursed = sum((t["disbursed"] or Decimal(0) for t in totals), Decimal(0))
    return reserved, disbursed


def _totals_of(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per currency, the sums of rows that hold each programme document once (the partner rows)."""
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(
            row["currency"],
            {"currency": row["currency"], "pds": 0, "unicef_planned": None, "total": None},
        )
        entry["pds"] += row["pds"]
        for key in ("unicef_planned", "total", "reserved", "disbursed"):
            if row.get(key) is not None:
                entry[key] = (entry.get(key) or Decimal(0)) + row[key]
            else:
                entry.setdefault(key, None)
    return sorted(totals.values(), key=lambda t: (-t["pds"], t["currency"]))


# ----------------------------------------------------------------------------------------- summary info
def summary(scope: str, today: datetime.date) -> dict[str, Any]:
    """*Funded programme summary info*: the programme documents of ``scope`` (``active`` or ``all``) by
    partner and by section with their totals, and the active ones ending within :data:`ENDING_DAYS`
    days."""
    base = programme_documents(scope=scope)
    by_partner = _order(_by_partner(_grouped(base, BY_PARTNER)), "partner")
    by_section = _order(_grouped(base, BY_SECTION), "section")
    totals = _totals_of(by_partner)
    reserved, disbursed = _funds(totals)
    ending = [
        {
            "id": row["id"],
            "number": row["number"] or "",
            "title": row["title"] or "",
            "sections": row["section_names"] or [],
            "start": row["start"],
            "end": row["end"],
            "days_to_end": days_to_end(row["end"], today),
            "partner": row["partner_name"] or "",
            "partner_id": row["partner_id"],
        }
        for row in PCA.objects.filter(
            status__in=statuses.ACTIVE,
            end__gte=today,
            end__lte=today + datetime.timedelta(days=ENDING_DAYS),
        )
        .order_by("end", "number", "id")
        .values("id", "number", "title", "section_names", "start", "end", "partner_id", "partner_name")
    ]
    for row in ending:
        row["tone"] = days_tone(row["days_to_end"])
    per_section: Counter[str] = Counter()
    for row in by_section:
        per_section[row["section"]] += row["pds"]  # a programme document has one currency: its rows add up
    by_type = Counter(
        dict(
            base.select_related(None)
            .order_by()
            .values("document_type")
            .annotate(n=Count("id"))
            .values_list("document_type", "n")
        )
    )
    return {
        "scope": scope,
        "count": sum(t["pds"] for t in totals),
        "partners": _partner_count(by_partner),
        "by_partner": by_partner,
        "by_section": by_section,
        "totals": totals,
        "reserved": reserved,
        "disbursed": disbursed,
        "ending_soon": ending,
        "ending_days": ENDING_DAYS,
        "ending_soon_days": ENDING_SOON_DAYS,
        "ending_30": sum(1 for r in ending if r["days_to_end"] <= ENDING_SOON_DAYS),
        "charts": {
            "by_section": sorted(
                ((name or _("No section"), n) for name, n in per_section.items()), key=lambda x: (-x[1], x[0])
            )[:20],
            "by_type": sorted(
                ((name or _("Unknown"), n) for name, n in _merge_blank(by_type).items()),
                key=lambda x: (-x[1], x[0]),
            ),
        },
        "last_sync": base.select_related(None)
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first(),
    }


def _merge_blank(counts: Counter) -> Counter:
    """Counts keyed by a text, None and "" counted together under ""."""
    merged: Counter[str] = Counter()
    for key, n in counts.items():
        merged[(key or "").strip()] += n
    return merged


SUMMARY_COLUMNS = {
    "ending": (
        "programme_document",
        "title",
        "sections",
        "start",
        "end",
        "days_to_end",
        "partner",
    ),
    "section": (
        "section",
        "currency",
        "programme_documents",
        "unicef_planned_budget",
        "total_budget",
        "funds_reserved_usd",
        "disbursed_usd",
    ),
    "partner": (
        "partner",
        "currency",
        "programme_documents",
        "unicef_planned_budget",
        "total_budget",
        "funds_reserved_usd",
        "disbursed_usd",
    ),
}


def summary_sheets(data: dict[str, Any]) -> list[tuple[str, tuple[str, ...], list[dict[str, Any]]]]:
    """The summary's three tables as sheets of a workbook (no person in any)."""

    def sums(row: dict[str, Any], name: str, blank: str) -> dict[str, Any]:
        return {
            name: row[name] or blank,
            "currency": row["currency"],
            "programme_documents": row["pds"],
            "unicef_planned_budget": row["unicef_planned"],
            "total_budget": row["total"],
            "funds_reserved_usd": row["reserved"],
            "disbursed_usd": row["disbursed"],
        }

    ending = [
        {
            "programme_document": r["number"],
            "title": r["title"],
            "sections": r["sections"],
            "start": r["start"],
            "end": r["end"],
            "days_to_end": r["days_to_end"],
            "partner": r["partner"],
        }
        for r in data["ending_soon"]
    ]
    return [
        (f"Ending within {data['ending_days']} days", SUMMARY_COLUMNS["ending"], ending),
        (
            "Summary by section",
            SUMMARY_COLUMNS["section"],
            [sums(r, "section", "No section") for r in data["by_section"]],
        ),
        (
            "Summary by partner",
            SUMMARY_COLUMNS["partner"],
            [sums(r, "partner", "Unknown partner") for r in data["by_partner"]],
        ),
    ]


# ---------------------------------------------------------------------------------------------- overall
OVERALL_FIELDS = (
    "id",
    "number",
    "title",
    "partner_id",
    "partner_name",
    "document_type",
    "status",
    "section_names",
    "start",
    "end",
    "donors",
    "grants",
    "f_currency",
    "f_unicef",
    "f_total",
    "f_cash",
    "f_reserved",
    "f_disbursed",
)
OVERALL_COLUMNS = (
    "programme_document",
    "title",
    "partner",
    "document_type",
    "pd_status",
    "sections",
    "start",
    "end",
    "days_to_end",
    "donors",
    "grants",
    "currency",
    "unicef_planned_budget",
    "total_budget",
    "unicef_cash",
    "funds_reserved_usd",
    "disbursed_usd",
)


def overall_values(qs: QuerySet[PCA]) -> QuerySet:
    """The programme documents of the filter as the overall list reads them (its columns only)."""
    return with_amounts(qs.select_related(None)).values(*OVERALL_FIELDS)


def overall_row(row: dict[str, Any], today: datetime.date) -> dict[str, Any]:
    """One programme document of :func:`overall_values` as the list shows it."""
    days = days_to_end(row["end"], today)
    status = row["status"] or ""
    return {
        "id": row["id"],
        "number": row["number"] or "",
        "title": row["title"] or "",
        "partner": row["partner_name"] or "",
        "partner_id": row["partner_id"],
        "document_type": row["document_type"] or "",
        "status": status,
        "status_label": statuses.label(status),
        "tone": STATUS_TONES.get(status.lower(), "unknown"),
        "sections": row["section_names"] or [],
        "start": row["start"],
        "end": row["end"],
        "days_to_end": days,
        "days_tone": days_tone(days),
        "donors": row["donors"] or [],
        "grants": row["grants"] or [],
        "currency": row["f_currency"],
        "unicef_planned": row["f_unicef"],
        "total": row["f_total"],
        "unicef_cash": row["f_cash"],
        "reserved": row["f_reserved"],
        "disbursed": row["f_disbursed"],
    }


def overall_page(page: Any, today: datetime.date) -> list[dict[str, Any]]:
    """The rows of one page of programme documents (a page of a paginated ``PCA`` query), in its order."""
    ids = [pd.pk for pd in page]
    found = {row["id"]: row for row in overall_values(PCA.objects.filter(pk__in=ids))}
    return [overall_row(found[pk], today) for pk in ids if pk in found]


def overall_rows(qs: QuerySet[PCA], today: datetime.date) -> Iterator[dict[str, Any]]:
    for row in overall_values(qs).iterator(chunk_size=2000):
        yield overall_row(row, today)


def export_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row of the overall list as its CSV and Excel files write it (:data:`OVERALL_COLUMNS`)."""
    return {
        "programme_document": row["number"],
        "title": row["title"],
        "partner": row["partner"],
        "document_type": row["document_type"],
        "pd_status": row["status_label"],
        "sections": row["sections"],
        "start": row["start"],
        "end": row["end"],
        "days_to_end": row["days_to_end"],
        "donors": row["donors"],
        "grants": row["grants"],
        "currency": row["currency"],
        "unicef_planned_budget": row["unicef_planned"],
        "total_budget": row["total"],
        "unicef_cash": row["unicef_cash"],
        "funds_reserved_usd": row["reserved"],
        "disbursed_usd": row["disbursed"],
    }


def overall_totals(qs: QuerySet[PCA]) -> dict[str, Any]:
    """The tiles of the overall list: its programme documents, their budgets per currency, funds reserved and
    disbursed."""
    totals = sorted(_grouped(qs, BY_CURRENCY), key=lambda t: (-t["pds"], t["currency"]))
    reserved, disbursed = _funds(totals)
    return {
        "count": sum(t["pds"] for t in totals),
        "totals": totals,
        "reserved": reserved,
        "disbursed": disbursed,
    }
