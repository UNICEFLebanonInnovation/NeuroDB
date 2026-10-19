"""The Funds page's filter and its Power BI views: **Partner Fund Monitoring** (one row per programme
document: its budget, the partner's contribution and its share, and the financial contribution the
partner reported in PRP) and **Funded Programs** (one row per programme document and grant).

* **One filter for every tab** (:class:`FundFilters`): search, donor, grant, FR number, FR start year
  (they choose funds reservation lines, and so the programme documents funded by them), PD status,
  partner and partner type. With no PD status chosen, every programme document but drafts and
  cancelled ones.
* **Amounts.** The budget fields are the programme document's, in its budget currency (eTools'
  ``budget_currency``); funds reservation amounts are in US dollars. Amounts in different currencies
  are never added: totals are given per currency.
* **Partner contribution share** = the partner's contribution over the total budget (61,260 of 644,148.9
  is 9.5 %).
* **Financial contribution during reporting periods**: PRP's ``financial_contribution_to_date`` (the
  ``prp_indicator_reports`` documents), which PRP repeats on every indicator of a progress report: it is
  read once per progress report, from the latest one that counts (Accepted or Submitted, by
  ``reached.status_of``) and carries it, never added across indicators or reports.
* **Funded Programs.** The funds reserved per programme document and grant are the funds reservation
  lines of that grant (``FundsReservation.overall_amount``). The budget, the disbursement (the FRs'
  ``actual_amt``, for the whole programme document) and the partner's contribution belong to the
  programme document: they are shown once per programme document, never repeated on each of its
  grants, so a total counts them once. Days to end: the PD's end date minus today, −1 once it has
  ended.
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.db.models.fields.json import KT

from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import models as dm
from . import reached as rc

# Power BI's status colours: active green, signed grey, ended (and the like) amber, in review red
STATUS_TONES = {
    "active": "on_track",
    "signed": "planned",
    "signature": "planned",
    "pending_approval": "planned",
    "ended": "stale",
    "closed": "stale",
    "suspended": "stale",
    "terminated": "stale",
    "cancelled": "stale",
    "expired": "stale",
    "review": "off_track",
}


def _getlist(params, key: str) -> list[str]:
    values = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values if str(v or "").strip()]


def amount(value: Any) -> Decimal | None:
    """A budget amount as eTools stores it (text): ``"1,585,122.10"`` -> Decimal; None when empty or
    not a number."""
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).replace(",", "").strip()
    try:
        found = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return found if found.is_finite() else None


# ------------------------------------------------------------------------------------------ filters
@dataclass
class FundFilters:
    q: str = ""
    donors: list[str] = field(default_factory=list)
    grants: list[str] = field(default_factory=list)
    fr: str = ""  # words of an FR number
    year: int | None = None  # the FR's start year
    pd_statuses: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=list)  # partner ids as text
    partner_types: list[str] = field(default_factory=list)  # partner type or CSO type

    @classmethod
    def from_params(cls, params) -> FundFilters:
        from .monitoring import year_param

        return cls(
            q=(params.get("q") or "").strip()[:200],
            donors=_getlist(params, "donor"),
            grants=_getlist(params, "grant"),
            fr=" ".join(str(params.get("fr") or "").split())[:50],
            year=year_param(params.get("year")),
            pd_statuses=_getlist(params, "pd_status"),
            partners=[x for x in _getlist(params, "partner") if re.fullmatch(r"[0-9]{1,18}", x)],
            partner_types=_getlist(params, "partner_type"),
        )

    @property
    def by_fr(self) -> bool:
        """Whether the filter chooses funds reservation lines (and so the PDs they fund)."""
        return bool(self.donors or self.grants or self.fr or self.year)

    def pd_q(self, prefix: str = "") -> Q:
        """PD status, partner and partner type, on a programme document reached by ``prefix``."""
        found = Q()
        if self.pd_statuses:
            found &= Q(**{f"{prefix}status__in": self.pd_statuses})
        if self.partners:
            found &= Q(**{f"{prefix}partner_id__in": [int(p) for p in self.partners]})
        if self.partner_types:
            found &= Q(**{f"{prefix}partner__partner_type__in": self.partner_types}) | Q(
                **{f"{prefix}partner__cso_type__in": self.partner_types}
            )
        return found

    def lines(self) -> QuerySet[dm.FundsReservation]:
        """The funds reservation lines of the FR filters (donor, grant, FR number, start year)."""
        lines = dm.FundsReservation.objects.all()
        if self.donors:
            lines = lines.filter(donor__in=self.donors)
        if self.grants:
            lines = lines.filter(grant_number__in=self.grants)
        if self.fr:
            lines = lines.filter(fr_number__icontains=self.fr)
        if self.year:
            lines = lines.filter(start_date__year=self.year)
        return lines

    def pds(self) -> QuerySet[PCA]:
        """The programme documents of the filter."""
        pds = PCA.objects.select_related("partner").filter(self.pd_q())
        if not self.pd_statuses:
            pds = pds.exclude(status__in=pd_statuses.NOT_A_PARTNERSHIP)
        if self.q:
            pds = pds.filter(
                Q(number__icontains=self.q)
                | Q(title__icontains=self.q)
                | Q(partner_name__icontains=self.q)
                | Q(partner__name__icontains=self.q)
                | Q(partner__short_name__icontains=self.q)
                | Q(partner__vendor_number__icontains=self.q)
                | Q(
                    pk__in=dm.FundsReservation.objects.filter(fr_number__icontains=self.q).values(
                        "intervention_id"
                    )
                )
            )
        if self.by_fr:
            pds = pds.filter(pk__in=self.lines().exclude(intervention=None).values("intervention_id"))
        return pds


def filter_options() -> dict[str, Any]:
    pds = PCA.objects.exclude(status__in=pd_statuses.NOT_A_PARTNERSHIP).order_by()
    partners = PartnerOrganization.objects.filter(pk__in=pds.exclude(partner=None).values("partner_id"))
    return {
        "pd_statuses": sorted({x for x in pds.values_list("status", flat=True).distinct() if x}),
        "partners": [
            {"value": str(pk), "label": name}
            for pk, name in partners.order_by("name", "id").values_list("id", "name")
        ],
        "partner_types": sorted(
            {x for pair in partners.order_by().values_list("partner_type", "cso_type") for x in pair if x}
        ),
    }


# ------------------------------------------------------------------ PRP financial contribution
def financial_contributions(pd_ids: set[int], today: datetime.date) -> dict[int, dict[str, Any]]:
    """Per programme document: the financial contribution to date its partner reported in PRP, read once
    per progress report from the latest one that counts and carries it (see the module docstring)."""
    ids = sorted(i for i in pd_ids if i)
    reports: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for start in range(0, len(ids), 5000):
        docs = (
            dm.DatamartDocument.objects.filter(
                dataset="prp_indicator_reports", intervention_id__in=ids[start : start + 5000]
            )
            .annotate(
                _type=KT("data__report_type"),
                _number=KT("data__report_number"),
                _pr_status=KT("data__pr_status"),
                _status=KT("data__report_status"),
                _pr_end=KT("data__pr_end_date"),
                _end=KT("data__time_period_end"),
                _pr_due=KT("data__pr_due_date"),
                _due=KT("data__due_date"),
                _pr_sub=KT("data__pr_submission_date"),
                _sub=KT("data__submission_date"),
                _value=KT("data__financial_contribution_to_date"),
                _currency=KT("data__financial_contribution_currency"),
            )
            .order_by("intervention_id", "record_key")
            .values_list(
                "intervention_id",
                "_type",
                "_number",
                "_pr_status",
                "_status",
                "_pr_end",
                "_end",
                "_pr_due",
                "_due",
                "_pr_sub",
                "_sub",
                "_value",
                "_currency",
            )
        )
        for (
            pd_id,
            kind,
            number,
            pr_status,
            status,
            pr_end,
            end,
            pr_due,
            due,
            pr_sub,
            sub,
            value,
            currency,
        ) in docs:
            report_status, end_date = rc.prp_document_status(
                pr_status=pr_status,
                status=status,
                pr_end=pr_end,
                end=end,
                pr_due=pr_due,
                due=due,
                pr_sub=pr_sub,
                sub=sub,
                today=today,
            )
            key = (pd_id, (kind or "").upper(), str(number or ""), end_date.isoformat() if end_date else "")
            report = reports.setdefault(
                key,
                {
                    "report_type": (kind or "").upper(),
                    "report_number": str(number or ""),
                    "period_end": end_date,
                    "status": report_status,
                    "values": set(),
                    "currency": "",
                },
            )
            found = amount(value)
            if found is not None:
                report["values"].add(found)
            report["currency"] = report["currency"] or (currency or "").strip()
    out: dict[int, dict[str, Any]] = {}
    for (pd_id, _kind, _number, _end), report in sorted(
        reports.items(), key=lambda kv: (kv[1]["period_end"] or datetime.date.min, kv[0])
    ):
        if report["status"] not in rc.COUNTED or not report["values"]:
            continue
        number = report["report_number"]
        name = (
            number if number.upper().startswith(report["report_type"]) else f"{report['report_type']}{number}"
        )
        out[pd_id] = {  # the latest counted report with a value wins (reports are read oldest first)
            "value": max(report["values"]),
            "varies": len(report["values"]) > 1,
            "currency": report["currency"],
            "as_of": report["period_end"],
            "report": name,
            "status": report["status"],
            "status_label": rc.STATUS_LABELS[report["status"]],
        }
    return out


# ------------------------------------------------------------------ Partner Fund Monitoring
def _partner_type(pd: PCA) -> tuple[str, str]:
    partner = pd.partner if pd.partner_id else None
    return (partner.partner_type or "", partner.cso_type or "") if partner else ("", "")


def _currency_totals(rows: list[dict[str, Any]], fields: tuple[str, ...], currency_key: str = "currency"):
    """Sums of ``fields`` per currency (amounts in different currencies are never added)."""
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(
            row[currency_key] or "",
            {"currency": row[currency_key] or "", **{f: Decimal(0) for f in fields}, "pds": 0},
        )
        entry["pds"] += 1
        for f in fields:
            if row.get(f) is not None:
                entry[f] += row[f]
    return sorted(totals.values(), key=lambda t: (-t["pds"], t["currency"]))


def partner_contributions(filters: FundFilters, today: datetime.date) -> dict[str, Any]:
    """Partner Fund Monitoring: one row per programme document of the filter, with totals per
    currency."""
    pds = list(filters.pds().order_by("partner_name", "number", "pk"))
    reported = financial_contributions({pd.pk for pd in pds}, today)
    rows = []
    for pd in pds:
        unicef, total, partner = (
            amount(pd.total_unicef_budget),
            amount(pd.total_budget),
            amount(pd.cso_contribution),
        )
        kind, cso = _partner_type(pd)
        rows.append(
            {
                "pd": pd,
                "pd_id": pd.pk,
                "programme_document": pd.number or "",
                "pd_status": pd_statuses.label(pd.status),
                "partner": pd.partner.name if pd.partner_id and pd.partner else (pd.partner_name or ""),
                "partner_id": pd.partner_id,
                "partner_type": kind,
                "cso_type": cso,
                "currency": (pd.budget_currency or "").strip(),
                "unicef_planned": unicef,
                "total": total,
                "partner_contribution": partner,
                "partner_share": (partner / total * 100) if partner is not None and total else None,
                "reported": reported.get(pd.pk),
            }
        )
    totals = _currency_totals(rows, ("unicef_planned", "total", "partner_contribution"))
    # the share of a currency: its partner contributions over its total budgets, of the programme
    # documents that give both (a contribution without its budget is never set against another's)
    shares: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in rows:
        if r["partner_contribution"] is not None and r["total"]:
            shares[r["currency"] or ""][0] += r["partner_contribution"]
            shares[r["currency"] or ""][1] += r["total"]
    for t in totals:
        part, whole = shares.get(t["currency"], (Decimal(0), Decimal(0)))
        t["partner_share"] = part / whole * 100 if whole else None
    reported_rows = [
        {"currency": r["reported"]["currency"], "value": r["reported"]["value"]}
        for r in rows
        if r["reported"]
    ]
    return {
        "rows": rows,
        "totals": totals,
        "reported_totals": _currency_totals(reported_rows, ("value",)),
        "reported": len(reported_rows),
    }


# ----------------------------------------------------------------------------- Funded Programs
def funded_programmes(filters: FundFilters, today: datetime.date) -> dict[str, Any]:
    """Funded Programs: one row per programme document and grant of the filter (the funds reservation
    lines of the grant), grouped per programme document (``groups``), whose own amounts are given once
    (``rows``: the first row of each PD carries them, ``pd_amounts``)."""
    pds = {pd.pk: pd for pd in filters.pds()}
    lines = filters.lines()
    per_grant: dict[tuple[int, str], dict[str, Any]] = {}
    grants_of: dict[int, list[str]] = defaultdict(list)
    ids = sorted(pds)
    for start in range(0, len(ids), 5000):
        for pd_id, grant, donor, reserved in (
            lines.filter(intervention_id__in=ids[start : start + 5000])
            .values("intervention_id", "grant_number", "donor")
            .annotate(reserved=Sum("overall_amount"))
            .order_by("intervention_id", "grant_number", "donor")
            .values_list("intervention_id", "grant_number", "donor", "reserved")
        ):
            if (pd_id, grant or "") not in per_grant:
                grants_of[pd_id].append(grant or "")
            entry = per_grant.setdefault((pd_id, grant or ""), {"donors": [], "reserved": Decimal(0)})
            if donor and donor not in entry["donors"]:
                entry["donors"].append(donor)
            entry["reserved"] += reserved or Decimal(0)
    funded = sorted(grants_of)
    disbursed: dict[int, Decimal] = {}
    for start in range(0, len(funded), 5000):
        for pd_id, actual in (
            dm.FundsReservationHeader.objects.filter(intervention_id__in=funded[start : start + 5000])
            .values("intervention_id")
            .annotate(actual=Sum("actual_amt"))
            .order_by()
            .values_list("intervention_id", "actual")
        ):
            disbursed[pd_id] = actual
    groups = []
    for pd_id in sorted(funded, key=lambda i: ((pds[i].number or "").lower(), i)):
        pd = pds[pd_id]
        if pd.end is None:
            days = None
        else:
            days = -1 if pd.end < today else (pd.end - today).days
        kind, cso = _partner_type(pd)
        shared = {
            "pd": pd,
            "pd_id": pd.pk,
            "programme_document": pd.number or "",
            "pd_status": pd_statuses.label(pd.status),
            "tone": STATUS_TONES.get((pd.status or "").lower(), "unknown"),
            "start": pd.start,
            "end": pd.end,
            "days_to_end": days,
            "partner": pd.partner.name if pd.partner_id and pd.partner else (pd.partner_name or ""),
            "partner_id": pd.partner_id,
            "partner_type": kind,
            "cso_type": cso,
            "currency": (pd.budget_currency or "").strip(),
            "unicef_planned": amount(pd.total_unicef_budget),
            "total": amount(pd.total_budget),
            "disbursed": disbursed.get(pd_id),
            "partner_contribution": amount(pd.cso_contribution),
        }
        grants = sorted(grants_of[pd_id], key=lambda g: (g == "", g))
        rows = [
            {
                "grant": grant,
                "donor": ", ".join(per_grant[(pd_id, grant)]["donors"]),
                "reserved": per_grant[(pd_id, grant)]["reserved"],
            }
            for grant in grants
        ]
        groups.append({**shared, "rows": rows})
    flat = []
    for group in groups:
        for n, row in enumerate(group["rows"]):
            flat.append({**group, **row, "pd_amounts": n == 0})
    return {
        "groups": groups,
        "rows": flat,
        "pds": len(groups),
        "grants": len({row["grant"] for row in flat if row["grant"]}),
        "reserved": sum((row["reserved"] for row in flat), Decimal(0)),
        "disbursed": sum((g["disbursed"] or Decimal(0) for g in groups), Decimal(0)),
        "totals": _currency_totals(groups, ("unicef_planned", "total", "partner_contribution")),
    }
