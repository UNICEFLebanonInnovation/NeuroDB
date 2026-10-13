"""The HACT programmatic visit plan per programme document: the Field monitoring page's "HACT visit plan"
tab, its strip on the default tab, the Watch's link and the Ask NeuroDB tool.

One row per programme document (PD) of a HACT year (the calendar year), with its plan, the programmatic
visits that count against it and what is still needed (:func:`build`):

* **Required** = the PD's planned programmatic visits for the year, q1 + q2 + q3 + q4 of eTools'
  ``interventions-planned-visits`` (``PlannedVisits``); 0 when eTools plans none. eTools' partner HACT
  minimum (``PartnerHACTYear.pv_required``) is a different, partner-level figure, shown beside it.
* **Completed** = programmatic visits to the PD whose report UNICEF approved in the year, each visit
  once per source: field monitoring visits completed and marked programmatic, by their end date (the
  rows the Assurance page counts per partner, ``fm.completed_programmatic_rows``), plus third-party
  monitoring visits marked programmatic and ``unicef_approved``, by the activity date.
* **Counted** = min(required, completed); **still needed** = required - counted, never negative. A
  surplus on one PD never makes up for a gap on another (no netting), as in the team's workbook.
* **Due by the end of Q<n>** = the visits planned up to the end of quarter n less those completed,
  never negative; n is today's quarter for this year, 4 for a past year and 0 for a future one.
* **Not counted, shown so that a zero is explained**: reports awaiting approval (FM ``submitted``, TPM
  ``tpm_reported``), FM visits completed but not marked programmatic, UNICEF staff trip activities
  (eTools sends them with no status: a planned or cancelled trip looks like a done one), and the
  partner's completed programmatic FM visits whose findings name none of its PDs (only the partner or
  a CP output: the Assurance page counts them for the partner).
* eTools' planned-visit rows that match no PD (:func:`unlinked_plans`) are in no figure: the page
  says how many there are.

The rows are plain data (ints, strs, dates, Decimals, bools): no staff name ever enters them, so they
feed the files, the Watch and the AI as they are. The UNICEF focal points are a count there; their names
are read for the page only (:func:`focal_names`, :func:`for_page`). No cache: a sync or a comment shows
at once (:func:`build` takes about 30 ms for the 252 PDs of the x20 test world).
"""

from __future__ import annotations

import datetime
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import CharField, Count, Q, Sum, Value
from django.db.models.expressions import Case, When
from django.db.models.functions import Cast, Coalesce, Concat, Lower, NullIf, Trim
from django.utils.translation import gettext_lazy as _

from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import fm
from . import models as dm
from .funds import amount
from .reached import norm
from .reporting import focal_point_names

REMARKS = {
    "needed": _("HACT FMM needed"),
    "ended": _("PD ended, visits missing"),
    "completed": _("Completed"),
    "no_plan": _("No visit planned"),
}  # the codes are stable: filters, files and the AI use them
REMARK_ORDER = ("needed", "ended", "completed", "no_plan")
TPM_APPROVED = ("unicef_approved",)
TPM_AWAITING = ("tpm_reported",)
TPM_NOT_PLANNED = ("draft", "cancelled")
MAX_ROWS = 1000  # above: "narrow the filter" (a country has a few hundred)
# the Datamart datasets the plan's figures come from (the page's "as of" line)
SYNC_TARGETS = ("planned_visits", "field_monitoring", "tpm_activities")
COMMENT_MAX = 500
QUARTERS = (1, 2, 3, 4)


# ------------------------------------------------------------------------------------ filters
def _values(params: Any, key: str) -> list[str]:
    """Every non-blank value of ``key`` in a QueryDict or a plain dict, stripped, once each, in order."""
    raw = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
    if isinstance(raw, (str, int)):
        raw = [raw]
    seen: dict[str, None] = {}
    for value in raw or []:
        text = " ".join(str(value or "").split())
        if text:
            seen.setdefault(text, None)
    return list(seen)


def parse_year(value: Any, today: datetime.date) -> int:
    """A HACT year from a parameter; today's year when it is blank, not a number or out of range."""
    text = str(value or "").strip()
    if text.isdecimal() and 1990 <= int(text) <= today.year + 10:
        return int(text)
    return today.year


@dataclass(frozen=True)
class PlanFilters:
    """The plan tab's filter. ``remarks`` and ``zero`` are the presets (the quick views): the headline
    leaves them out (``select(..., presets=False)``)."""

    year: int
    q: str = ""
    sections: tuple[str, ...] = ()
    partners: tuple[int, ...] = ()
    risks: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    remarks: tuple[str, ...] = ()
    zero: bool = False

    @classmethod
    def from_params(cls, params: Any, today: datetime.date) -> PlanFilters:
        """From the request's parameters (``year``, ``q``, ``section``, ``partner``, ``risk``,
        ``status``, ``type``, ``remark``, ``zero``); the year defaults to today's, bad values are
        dropped."""
        get = params.get if hasattr(params, "get") else (lambda key, default=None: default)
        partners = tuple(
            dict.fromkeys(int(v) for v in _values(params, "partner") if v.isdecimal() and int(v))
        )
        return cls(
            year=parse_year(get("year"), today),
            q=" ".join(str(get("q") or "").split())[:200],
            sections=tuple(_values(params, "section")),
            partners=partners,
            risks=tuple(_values(params, "risk")),
            statuses=tuple(v.lower() for v in _values(params, "status")),
            types=tuple(_values(params, "type")),
            remarks=tuple(v for v in _values(params, "remark") if v in REMARKS),
            zero=str(get("zero") or "").strip().lower() in ("1", "true", "yes", "on"),
        )


def select(rows: Iterable[dict], f: PlanFilters, presets: bool = True) -> list[dict]:
    """The rows the filter keeps (the year is :func:`build`'s). ``presets`` False leaves the remark
    and "not visited" presets out, as the headline does. Search: partner name, vendor number or PD
    reference, any case; sections, risks and types in any case."""
    q = f.q.casefold()
    sections = {norm(s) for s in f.sections}
    risks = {norm(r) for r in f.risks}
    types = {norm(t) for t in f.types}
    partners, statuses = set(f.partners), set(f.statuses)
    kept = []
    for row in rows:
        if q and not any(q in (row[k] or "").casefold() for k in ("partner", "vendor_number", "reference")):
            continue
        if sections and not sections.intersection(norm(s) for s in row["sections"]):
            continue
        if partners and row["partner_id"] not in partners:
            continue
        if risks and norm(row["risk_rating"]) not in risks:
            continue
        if statuses and row["status"] not in statuses:
            continue
        if types and norm(row["document_type"]) not in types:
            continue
        if presets and f.remarks and row["remark"] not in f.remarks:
            continue
        if presets and f.zero and not row["zero"]:
            continue
        kept.append(row)
    return kept


# ------------------------------------------------------------------------------------ the rows
def years(today: datetime.date) -> list[int]:
    """The HACT years the plan can show: those eTools planned visits or a partner HACT year for, and
    this year, newest first."""
    found = {today.year}
    found.update(dm.PlannedVisits.objects.exclude(year=None).values_list("year", flat=True).distinct())
    found.update(dm.PartnerHACTYear.objects.exclude(year=None).values_list("year", flat=True).distinct())
    return sorted(found, reverse=True)


def due_quarter(year: int, today: datetime.date) -> int:
    """The n of "Due by the end of Q<n>": today's quarter for this year, 4 for a past year, 0 for a
    future one."""
    if year == today.year:
        return (today.month - 1) // 3 + 1
    return 4 if year < today.year else 0


def _plans(year: int, pd_ids: list[int] | None) -> dict[int, dict[str, int]]:
    """{pd_id: {"q1".."q4": n}}: the PD's planned visits per quarter (its rows of the year added)."""
    rows = dm.PlannedVisits.objects.filter(year=year).exclude(intervention_id=None)
    if pd_ids is not None:
        rows = rows.filter(intervention_id__in=pd_ids)
    rows = rows.order_by().values("intervention_id").annotate(**{f"q{n}": Sum(f"q{n}") for n in QUARTERS})
    return {r["intervention_id"]: {f"q{n}": r[f"q{n}"] or 0 for n in QUARTERS} for r in rows}


def _tpm_visit_key():
    """A TPM visit, in SQL: its reference number, else the activity's task reference, else the row."""
    return Case(
        When(visit_reference_number__regex=r"\S", then=Concat(Value("v:"), Trim("visit_reference_number"))),
        When(task_reference_number__regex=r"\S", then=Concat(Value("t:"), Trim("task_reference_number"))),
        default=Concat(Value("a-"), Cast("id", CharField())),
        output_field=CharField(),
    )


def _tpm(year: int, pd_ids: list[int] | None) -> dict[int, dict[str, int]]:
    """{pd_id: {"approved", "awaiting", "planned"}}: distinct TPM visits of the PD dated in the year,
    programmatic with an approved report, programmatic awaiting approval, and any not draft or
    cancelled (the "TPM yes / no" column). The status is the visit's (``visit_status``), else the
    linked TPM visit's."""
    rows = dm.TPMActivity.objects.filter(date__year=year).exclude(intervention_id=None)
    if pd_ids is not None:
        rows = rows.filter(intervention_id__in=pd_ids)
    status = Lower(Trim(Coalesce(NullIf(Trim("visit_status"), Value("")), "visit__status", Value(""))))
    counted = (
        rows.order_by()
        .annotate(vk=_tpm_visit_key(), vstatus=status)
        .values("intervention_id")
        .annotate(
            approved=Count(
                "vk", distinct=True, filter=Q(is_programmatic_visit=True, vstatus__in=TPM_APPROVED)
            ),
            awaiting=Count(
                "vk", distinct=True, filter=Q(is_programmatic_visit=True, vstatus__in=TPM_AWAITING)
            ),
            planned=Count("vk", distinct=True, filter=~Q(vstatus__in=TPM_NOT_PLANNED)),
        )
    )
    return {r["intervention_id"]: r for r in counted}


def _staff_trips(year: int, pd_ids: list[int] | None) -> dict[int, int]:
    """{pd_id: distinct trips}: UNICEF staff trip activities of the PD typed programmatic, dated in the
    year (shown, never counted), by trip reference (else the row)."""
    rows = dm.ProgrammaticVisit.objects.filter(
        date__year=year, travel_type__icontains="programmatic"
    ).exclude(intervention_id=None)
    if pd_ids is not None:
        rows = rows.filter(intervention_id__in=pd_ids)
    key = Case(
        When(travel_reference_number__regex=r"\S", then=Trim("travel_reference_number")),
        default=Concat(Value("t-"), Cast("id", CharField())),
        output_field=CharField(),
    )
    counted = (
        rows.order_by().annotate(trip=key).values("intervention_id").annotate(n=Count("trip", distinct=True))
    )
    return {r["intervention_id"]: r["n"] for r in counted}


def _hact_years(year: int) -> dict[int, dict[str, Any]]:
    """{partner_id: its HACT row of the year}: the latest synced one when eTools sends two (as the
    Watch reads them: ``-last_modify_date, -datamart_id``)."""
    rows = (
        dm.PartnerHACTYear.objects.filter(year=year)
        .exclude(partner_id=None)
        .order_by("-last_modify_date", "-datamart_id")
        .values("partner_id", "risk_rating", "pv_required", "pv_completed")
    )
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        found.setdefault(row["partner_id"], row)
    return found


def _pds(year: int, wanted: set[int], pd_ids: list[int] | None) -> list[dict[str, Any]]:
    """The programme documents of the year: not draft or cancelled, and running at some point in the
    year (start on or before 31 December or blank, end on or after 1 January or blank, not both blank),
    or with a plan or a counted visit in the year (``wanted``)."""
    first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    runs = (Q(start__lte=last) | Q(start=None)) & (Q(end__gte=first) | Q(end=None)) & ~Q(start=None, end=None)
    pds = PCA.objects.annotate(status_code=Lower(Trim("status"))).exclude(
        status_code__in=pd_statuses.NOT_A_PARTNERSHIP
    )
    pds = pds.filter(runs | Q(pk__in=sorted(wanted)))
    if pd_ids is not None:
        pds = pds.filter(pk__in=pd_ids)
    return list(
        pds.order_by().values(
            "pk",
            "number",
            "status_code",
            "document_type",
            "section_names",
            "start",
            "end",
            "total_budget",
            "budget_currency",
            "partner_id",
            "partner__name",
            "partner__vendor_number",
            "partner__rating",
            "unicef_focal_points",
        )
    )


def _remark(required: int, still_needed: int, ended: bool) -> str:
    if required == 0:
        return "no_plan"
    if still_needed == 0:
        return "completed"
    return "ended" if ended else "needed"


def _sections(names: Iterable[str | None] | None) -> list[str]:
    """A PD's eTools section names, spaces tidied, once each (any case), in their order."""
    found: dict[str, str] = {}
    for name in names or []:
        text = " ".join(str(name or "").split())
        if text:
            found.setdefault(norm(text), text)
    return list(found.values())


def build(year: int, today: datetime.date, *, pd_ids: Iterable[int] | None = None) -> list[dict]:
    """Every programme document of the HACT year ``year`` with its plan and visits (see the module), as
    of ``today``, sorted by partner name then reference. Seven queries, whatever the number of PDs.
    ``pd_ids`` keeps those PDs only (one row redrawn after a comment).

    Each row: ``pd_id``, ``reference``, ``status`` (the eTools code, lower case), ``document_type``,
    ``sections`` (eTools names), ``start``, ``end``, ``total_budget`` (Decimal or None), ``currency``,
    ``partner_id``, ``partner``, ``vendor_number``, ``risk_rating`` and ``risk_source`` ("hact": the
    partner's HACT year, "partner": its rating, "" none), ``etools_pv_required`` and
    ``etools_pv_completed`` (the partner's eTools HACT figures, None without a HACT row), ``plan_row``
    (eTools sent a plan for the year), ``q1``-``q4``, ``required``, ``completed_fm``, ``completed_tpm``,
    ``completed``, ``counted``, ``still_needed``, ``due_quarter``, ``due_now``, ``zero`` (planned and
    not visited), ``ended``, ``remark`` (a code of :data:`REMARKS`), ``awaiting`` (``awaiting_fm`` of
    them field monitoring reports), ``fm_not_programmatic``, ``staff_trips``, ``fm_partner_level`` (the
    partner's FM visits naming none of its PDs, the same on each of its PDs), ``tpm`` (bool) and
    ``focal_points_count``."""
    ids = sorted(set(pd_ids)) if pd_ids is not None else None
    plans = _plans(year, ids)
    fm_visits = fm.visits_by_pd_for_hact(year, ids)
    tpm = _tpm(year, ids)
    trips = _staff_trips(year, ids)
    hact = _hact_years(year)
    wanted = (
        set(plans)
        | {k for k, v in fm_visits.items() if v["completed"]}
        | {k for k, v in tpm.items() if v["approved"]}
    )
    quarter = due_quarter(year, today)
    pds = _pds(year, wanted, ids)
    # a redrawn row reads its own partner's only
    partners = None if ids is None else {pd["partner_id"] for pd in pds if pd["partner_id"]}
    partner_level = fm.partner_visits_naming_no_pd(year, partners) if pds else {}
    rows = []
    for pd in pds:
        pk = pd["pk"]
        plan = plans.get(pk) or dict.fromkeys(("q1", "q2", "q3", "q4"), 0)
        quarters = [plan[f"q{n}"] for n in QUARTERS]
        required = sum(quarters)
        visits = fm_visits.get(pk) or {}
        third_party = tpm.get(pk) or {}
        completed_fm = visits.get("completed", 0)
        completed_tpm = third_party.get("approved", 0)
        completed = completed_fm + completed_tpm
        counted = min(required, completed)
        still_needed = required - counted
        status = pd["status_code"] or ""
        ended = status in pd_statuses.ENDED or bool(pd["end"] and pd["end"] < today)
        hact_year = hact.get(pd["partner_id"]) if pd["partner_id"] else None
        hact_risk = " ".join(str((hact_year or {}).get("risk_rating") or "").split())
        partner_risk = " ".join(str(pd["partner__rating"] or "").split())
        rows.append(
            {
                "pd_id": pk,
                "reference": pd["number"] or "",
                "status": status,
                "document_type": pd["document_type"] or "",
                "sections": _sections(pd["section_names"]),
                "start": pd["start"],
                "end": pd["end"],
                "total_budget": amount(pd["total_budget"]),
                "currency": (pd["budget_currency"] or "").strip(),
                "partner_id": pd["partner_id"],
                "partner": pd["partner__name"] or "",
                "vendor_number": pd["partner__vendor_number"] or "",
                "risk_rating": hact_risk or partner_risk,
                "risk_source": "hact" if hact_risk else ("partner" if partner_risk else ""),
                "etools_pv_required": (hact_year or {}).get("pv_required"),
                "etools_pv_completed": (hact_year or {}).get("pv_completed"),
                "plan_row": pk in plans,
                **plan,
                "required": required,
                "completed_fm": completed_fm,
                "completed_tpm": completed_tpm,
                "completed": completed,
                "counted": counted,
                "still_needed": still_needed,
                "due_quarter": quarter,
                "due_now": max(0, sum(quarters[:quarter]) - completed),
                "zero": required > 0 and completed == 0,
                "ended": ended,
                "remark": _remark(required, still_needed, ended),
                "awaiting": visits.get("submitted", 0) + third_party.get("awaiting", 0),
                "awaiting_fm": visits.get("submitted", 0),
                "fm_not_programmatic": visits.get("not_programmatic", 0),
                "staff_trips": trips.get(pk, 0),
                "fm_partner_level": partner_level.get(pd["partner_id"], 0),
                "tpm": bool(third_party.get("planned")),
                # names are never kept in the row: a count only (exports, Watch and the AI read the rows)
                "focal_points_count": len(focal_point_names(pd["unicef_focal_points"])),
            }
        )
    rows.sort(key=lambda r: (r["partner"].casefold(), r["reference"].casefold(), r["pd_id"]))
    return rows


# ------------------------------------------------------------------------------------ summaries
def _partner_ids(rows: Iterable[dict]) -> set[int]:
    """The partners of ``rows``; a PD whose partner the sync could not link counts for none."""
    return {r["partner_id"] for r in rows if r["partner_id"] is not None}


def headline(rows: Iterable[dict], year: int, every: Iterable[dict] | None = None) -> dict[str, Any]:
    """The tab's four figures over ``rows`` (counts only: also the strip and the AI tool):

    * ``not_visited``: PDs planned and not visited (``zero``), their partners, how many of those
      partners have no completed visit on any of their PDs of the year, and how many of the PDs ended;
    * ``still_needed``: visits still needed by 31 December, summed over the PDs with a gap (no
      netting), with those PDs, their partners, and the visits and PDs among them that ended;
    * ``completed``: visits counted out of the visits required, the visits completed and those
      awaiting approval;
    * ``etools_behind``: partners of ``rows`` whose eTools HACT year has fewer programmatic visits
      completed than required, of those with a HACT row (the partner-level figure).

    ``every`` is every row of the year (:func:`build`) when ``rows`` is a filter of them: "no visit on
    any of their PDs" reads it, so a visit on a PD the filter leaves out still counts for its partner.
    PDs with no plan never enter a figure; PDs without a linked partner count for no partner."""
    rows = list(rows)
    zero = [r for r in rows if r["zero"]]
    gaps = [r for r in rows if r["still_needed"] > 0]
    visited_partners = _partner_ids(r for r in (rows if every is None else every) if r["completed"] > 0)
    zero_partners = _partner_ids(zero)
    hact: dict[Any, tuple[Any, Any]] = {}
    for r in rows:
        if r["partner_id"] is not None and r["etools_pv_required"] is not None:
            hact.setdefault(r["partner_id"], (r["etools_pv_required"], r["etools_pv_completed"]))
    return {
        "year": year,
        "pds": len(rows),
        "pds_with_plan": sum(1 for r in rows if r["required"] > 0),
        "not_visited": {
            "pds": len(zero),
            "partners": len(zero_partners),
            "partners_no_visit": len(zero_partners - visited_partners),
            "ended": sum(1 for r in zero if r["ended"]),
        },
        "still_needed": {
            "visits": sum(r["still_needed"] for r in gaps),
            "pds": len(gaps),
            "partners": len(_partner_ids(gaps)),
            "visits_ended": sum(r["still_needed"] for r in gaps if r["ended"]),
            "pds_ended": sum(1 for r in gaps if r["ended"]),
        },
        "completed": {
            "counted": sum(r["counted"] for r in rows),
            "required": sum(r["required"] for r in rows),
            "completed": sum(r["completed"] for r in rows),
            "awaiting": sum(r["awaiting"] for r in rows),
        },
        "etools_behind": {
            "partners": sum(1 for required, done in hact.values() if (done or 0) < (required or 0)),
            "with_row": len(hact),
        },
        "by_remark": {code: sum(1 for r in rows if r["remark"] == code) for code in REMARK_ORDER},
    }


def by_partner(rows: Iterable[dict], year: int, every: Iterable[dict] | None = None) -> list[dict]:
    """One row per partner of ``rows``, sorted by name: its PDs, those not visited, and the sums of the
    PD figures (still needed adds each PD's gap: no netting), beside the partner's eTools HACT
    minimum (``etools_required``, ``etools_completed``, ``etools_gap``; None without a HACT row of
    ``year``), and its FM visits naming none of its PDs (``fm_partner_level``, not counted). Two flags
    make the PD-level and partner-level difference visible:
    ``minimum_met_pd_needed`` (the eTools minimum is met while PDs still need visits) and
    ``plans_below_minimum`` (the PDs plan fewer visits than the partner minimum).

    ``every`` is every row of the year (:func:`build`) when ``rows`` is a filter of them: the partner
    minimum is the partner's, so ``plans_below_minimum`` adds the plans of all its PDs of the year,
    whatever the filter leaves out (a section's own PDs, or a quick view, never plan the whole
    minimum)."""
    groups: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["partner_id"]].append(r)
    planned: dict[Any, int] = defaultdict(int)  # every PD of the partner in the year
    for r in rows if every is None else every:
        planned[r["partner_id"]] += r["required"]
    out = []
    for partner_id, pds in groups.items():
        first = pds[0]
        required = sum(r["required"] for r in pds)
        still_needed = sum(r["still_needed"] for r in pds)
        etools_required, etools_completed = first["etools_pv_required"], first["etools_pv_completed"]
        has_row = etools_required is not None
        out.append(
            {
                "partner_id": partner_id,
                "partner": first["partner"],
                "vendor_number": first["vendor_number"],
                "risk_rating": first["risk_rating"],
                "year": year,
                "pds": len(pds),
                "pds_not_visited": sum(1 for r in pds if r["zero"]),
                "required": required,
                "completed": sum(r["completed"] for r in pds),
                "counted": sum(r["counted"] for r in pds),
                "still_needed": still_needed,
                "fm_partner_level": first["fm_partner_level"],
                "etools_required": etools_required,
                "etools_completed": etools_completed,
                "etools_gap": max(0, (etools_required or 0) - (etools_completed or 0)) if has_row else None,
                "minimum_met_pd_needed": has_row
                and (etools_completed or 0) >= (etools_required or 0)
                and still_needed > 0,
                "plans_below_minimum": has_row and planned[partner_id] < (etools_required or 0),
            }
        )
    out.sort(key=lambda p: (p["partner"].casefold(), p["partner_id"] or 0))
    return out


def by_section(rows: Iterable[dict], sections: Iterable[str] = ()) -> list[dict]:
    """Per eTools section, sorted by name: PDs, PDs with a plan, PDs not visited and visits still needed.
    A PD with two sections counts under each; a PD with none under "" (no section). The last row is the
    total (``total`` True), which counts each PD once.

    ``sections`` is the filter's sections when ``rows`` is filtered on them: only their lines are kept.
    The other sections of a PD shared with a chosen one would hold that PD alone, a part of the
    section's figures read as the whole."""
    rows = list(rows)
    chosen = {norm(s) for s in sections}
    groups: dict[str, list[dict]] = defaultdict(list)
    names: dict[str, str] = {}
    for r in rows:
        for name in r["sections"] or [""]:
            if chosen and norm(name) not in chosen:
                continue
            groups[norm(name)].append(r)
            names.setdefault(norm(name), name)

    def figures(pds: list[dict]) -> dict[str, int]:
        return {
            "pds": len(pds),
            "pds_with_plan": sum(1 for r in pds if r["required"] > 0),
            "not_visited": sum(1 for r in pds if r["zero"]),
            "still_needed": sum(r["still_needed"] for r in pds),
        }

    out = [
        {"section": names[key], "total": False, **figures(pds)}
        for key, pds in sorted(groups.items(), key=lambda item: (item[0] == "", item[0]))
    ]
    out.append({"section": "", "total": True, **figures(rows)})
    return out


def options(rows: Iterable[dict], f: PlanFilters | None = None) -> dict[str, Any]:
    """The filter's lists, from the rows of the year: sections, partners ([id, name], by name), risk
    ratings, statuses, document types and the remarks ([code, label], in :data:`REMARK_ORDER`).

    ``f`` is the filter the bar shows: every value it narrows the list on stays in the lists, and
    ``chosen`` gives each as its list spells it (the values the bar marks). A value the year's rows lack
    (a partner or a section of another year, after a change of year, or an address typed by hand) is
    added, so the bar and the list always agree and each value can be taken off; such a partner is named
    from its record (one query, only then)."""
    rows = list(rows)
    sections: dict[str, str] = {}
    partners: dict[int, str] = {}
    risks: dict[str, str] = {}
    types: dict[str, str] = {}
    statuses: set[str] = set()
    for r in rows:
        for name in r["sections"]:
            sections.setdefault(norm(name), name)
        if r["partner_id"] is not None:
            partners.setdefault(r["partner_id"], r["partner"])
        if r["risk_rating"]:
            risks.setdefault(norm(r["risk_rating"]), r["risk_rating"])
        if r["document_type"]:
            types.setdefault(norm(r["document_type"]), r["document_type"])
        if r["status"]:
            statuses.add(r["status"])
    chosen: dict[str, list] = {"sections": [], "partners": [], "risks": [], "statuses": [], "types": []}
    if f is not None:
        # select() reads these in any case: the list's own spelling is the one marked
        for key, found in (("sections", sections), ("risks", risks), ("types", types)):
            chosen[key] = [found.setdefault(norm(value), value) for value in getattr(f, key)]
        statuses.update(f.statuses)
        chosen["statuses"] = list(f.statuses)
        missing = [pk for pk in f.partners if pk not in partners]
        if missing:
            names = dict(PartnerOrganization.objects.filter(pk__in=missing).values_list("pk", "name"))
            partners.update({pk: names.get(pk) or f"#{pk}" for pk in missing})
        chosen["partners"] = list(f.partners)

    def ordered(found: dict[str, str]) -> list[str]:
        return [found[k] for k in sorted(found)]

    return {
        "sections": ordered(sections),
        "partners": [
            [pk, name] for pk, name in sorted(partners.items(), key=lambda p: (p[1].casefold(), p[0]))
        ],
        "risks": ordered(risks),
        "statuses": sorted(statuses),
        "types": ordered(types),
        "remarks": [[code, str(REMARKS[code])] for code in REMARK_ORDER],
        "chosen": chosen,
    }


def partner_counts(year: int, today: datetime.date) -> dict[int, dict[str, int]]:
    """{partner_id: {"pds": n, "visits": m}}: the partner's PDs that still need visits by 31 December
    and how many (no netting), for the Watch's HACT item. Partners without a gap are left out."""
    counts: dict[int, dict[str, int]] = {}
    for r in build(year, today):
        if r["still_needed"] > 0 and r["partner_id"] is not None:
            entry = counts.setdefault(r["partner_id"], {"pds": 0, "visits": 0})
            entry["pds"] += 1
            entry["visits"] += r["still_needed"]
    return counts


def unlinked_plans(year: int) -> dict[str, int]:
    """The planned-visit rows eTools sent for ``year`` that match no programme document (``rows``: the
    sync links a row by its PD reference alone, ``details.not_linked`` of the ``planned_visits`` run)
    and the visits they plan (``visits``, q1 + q2 + q3 + q4). They are in no figure of the plan, which
    says so. One query."""
    found = dm.PlannedVisits.objects.filter(year=year, intervention_id=None).aggregate(
        rows=Count("id"), **{f"q{n}": Sum(f"q{n}") for n in QUARTERS}
    )
    return {"rows": found["rows"], "visits": sum(found[f"q{n}"] or 0 for n in QUARTERS)}


def synced_on() -> datetime.date | None:
    """The day of the eTools Datamart sync the figures are as of: the latest successful run of each of
    the plan's datasets (planned visits, field monitoring, TPM activities), the oldest of those (a
    figure is only as recent as its oldest source). None before any."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    runs = (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target__in=SYNC_TARGETS,
            status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
            finished_at__isnull=False,
        )
        .order_by("target", "-finished_at")
        .distinct("target")
        .values_list("finished_at", flat=True)
    )
    days = [timezone.localtime(at).date() if timezone.is_aware(at) else at.date() for at in runs]
    return min(days) if days else None


# ------------------------------------------------------------------------------------ the page only
def focal_names(pd_ids: Iterable[int]) -> dict[int, list[str]]:
    """{pd_id: UNICEF focal points by name} (no e-mail address, ``reporting.focal_point_names``): for
    the page's render only, never in :func:`build`'s rows, a file, the Watch or the AI. PDs without a
    name are left out."""
    ids = sorted({pk for pk in pd_ids if pk})
    found: dict[int, list[str]] = {}
    for start in range(0, len(ids), 5000):
        for pk, values in PCA.objects.filter(pk__in=ids[start : start + 5000]).values_list(
            "pk", "unicef_focal_points"
        ):
            names = focal_point_names(values)
            if names:
                found[pk] = names
    return found


def comments(year: int, pd_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """{pd_id: {"text": str, "updated_at": datetime}}: the sections' comments of the year on these PDs
    (never who wrote them)."""
    ids = sorted({pk for pk in pd_ids if pk})
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 5000):
        rows = dm.VisitPlanComment.objects.filter(year=year, intervention_id__in=ids[start : start + 5000])
        for pk, text, updated_at in rows.values_list("intervention_id", "text", "updated_at"):
            found[pk] = {"text": text, "updated_at": updated_at}
    return found


def clean_comment(text: Any) -> str:
    """A comment as stored: control characters out, runs of spaces and line breaks made one space,
    stripped, at most :data:`COMMENT_MAX` characters."""
    kept = "".join(ch for ch in str(text or "") if ch.isspace() or unicodedata.category(ch) != "Cc")
    return " ".join(kept.split())[:COMMENT_MAX].strip()


def set_comment(pd: PCA, year: int, text: Any, user) -> dm.VisitPlanComment | None:
    """Write the section comment of ``pd`` for ``year`` (:func:`clean_comment`); an empty text removes
    it. Who wrote it is kept for the admin only (``updated_by``, and an audit trail entry of who and
    when for each change, without the text). Returns the comment, None when there is none any more. The
    caller checks :func:`can_comment` first."""
    from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry

    text = clean_comment(text)
    fields = {"text": text, "pd_reference": (pd.number or "")[:64], "updated_by": user.get_username()[:150]}
    held = dm.VisitPlanComment.objects.select_for_update()
    with transaction.atomic():
        if not text:
            current = held.filter(intervention=pd, year=year).first()
            if current is not None:
                message = "Removed on the HACT visit plan."
                LogEntry.objects.log_actions(user.pk, [current], DELETION, message, single_object=True)
                current.delete()
            return None
        # get_or_create: of two first comments written at once, the second edits the first
        comment, created = held.get_or_create(intervention=pd, year=year, defaults=fields)
        if not created:
            if comment.text == text:
                return comment
            for name, value in fields.items():
                setattr(comment, name, value)
            comment.save()
        # who and when, never the text: a name typed by mistake goes with the comment, and the audit
        # trail is read-only (no administrator could erase it there)
        message = f"Written on the HACT visit plan ({len(text)} characters)."
        LogEntry.objects.log_actions(
            user.pk, [comment], ADDITION if created else CHANGE, message, single_object=True
        )
    return comment


# ------------------------------------------------------------------------------------ who may comment
def can_comment(
    user, section_names: Iterable[str | None] | None, mapping: Mapping[str, int] | None = None
) -> bool:
    """May write the section comment of a PD with these eTools sections: an Administrator, or a Section
    editor whose section is among the confirmed NeuroDB sections of those names
    (``watch.sections.resolve_names``; ``mapping`` is its confirmed map, read once by a caller that asks
    for many PDs). Never a Viewer or a donor account."""
    from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of

    if user is None or not user.is_authenticated:
        return False
    role = role_of(user)
    if role == ADMIN:
        return True
    if role != SECTION_EDITOR or not getattr(user, "section_id", None):
        return False
    from neurodb.watch.sections import resolve_names

    ids, _unmapped = resolve_names(list(section_names or []), dict(mapping) if mapping is not None else None)
    return user.section_id in ids


def editable_names(user) -> set[str] | None:
    """The eTools section names (normalised) whose PDs the user may comment on, read once for a whole
    list: None for an Administrator (every PD), an empty set for anyone who may comment on none. With
    :func:`may_comment` it answers as :func:`can_comment` does, without a query per row."""
    from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of

    if user is None or not user.is_authenticated:
        return set()
    role = role_of(user)
    if role == ADMIN:
        return None
    if role != SECTION_EDITOR or not getattr(user, "section_id", None):
        return set()
    from neurodb.watch.sections import confirmed_map

    return {name for name, section_id in confirmed_map().items() if section_id == user.section_id}


def may_comment(editable: set[str] | None, section_names: Iterable[str | None] | None) -> bool:
    """Whether a PD with these sections is among :func:`editable_names` (no query)."""
    if editable is None:
        return True
    return any(norm(name) in editable for name in section_names or [] if name)


def for_page(rows: Iterable[dict], year: int, user) -> list[dict]:
    """The rows as the page shows them, each a copy with ``focal_points`` (names), ``comment`` (its
    :func:`comments` entry or None), ``can_comment`` (for ``user``) and ``search_reference`` (what the
    findings tab is searched by for its visits: ``fm.search_reference``). Two to four queries whatever
    the number of rows. For the page's render only: never for a file, the Watch or the AI."""
    rows = list(rows)
    ids = [r["pd_id"] for r in rows]
    names, notes, editable = focal_names(ids), comments(year, ids), editable_names(user)
    return [
        {
            **r,
            "focal_points": names.get(r["pd_id"], []),
            "comment": notes.get(r["pd_id"]),
            "can_comment": may_comment(editable, r["sections"]),
            "search_reference": fm.search_reference(r["reference"]),
        }
        for r in rows
    ]
