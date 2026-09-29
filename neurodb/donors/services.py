"""The donor page's figures: the donor's funds, what they paid for, and the country picture.

**Your contribution.** The donor's money is the funds reservation (FR) lines that name one of the
account's donors (and grants, when the account lists some), on the FRs running in the year (the
overview's window). A line's amount is what the donor committed to the programme document (PD).
Disbursed: the FR's disbursed amount (header) times the donor's share of the FR's lines. The PD's
children, targets, girls and boys and governorates come from the overview's children rule
(``overview.children_by_pd``); the donor is attributed its share of the PD, the donor's lines over
all the lines of the PD's FRs. Cost per child: disbursed over children attributed, set against the
country's cost per child of the same section (the overview's funds block, every donor).

**UNICEF Lebanon overall.** The overview of the whole country for the year, reduced to aggregates:
children reached, by month, section and governorate; results on track; programme and partner
counts; field visits. No programme, partner or donor is named and no amount of money appears.

Nothing here reads a request: the view passes the account and the year, so a donor can never widen
the scope with a query string.
"""

from __future__ import annotations

import datetime
import hashlib
from collections import Counter, defaultdict
from typing import Any

from django.conf import settings
from django.core.cache import cache as django_cache
from django.db.models import Q, Sum

from neurodb.datamart import models as dm
from neurodb.partnerships.models.legacy import PCA
from neurodb.reports import overview

CACHE_SECONDS = 300
CACHE_VERSION = 2
SECTION_SLOTS = 5  # the categorical colours of the page (--s1 .. --s5), then "other"
GOVERNORATES = {  # overview.governorate_key -> the name and place on the page's schematic map
    "akkar": ("Akkar", 1, 0),
    "north": ("North", 0, 1),
    "baalbekhermel": ("Baalbek-Hermel", 2, 1),
    "beirut": ("Beirut", 0, 2),
    "mountlebanon": ("Mount Lebanon", 1, 2),
    "beqaa": ("Bekaa", 2, 2),
    "south": ("South", 0, 3),
    "nabatieh": ("Nabatieh", 1, 3),
}
# A programme document past its end date (or closed in eTools) is "Closed": its result is final.
CLOSED = ("none", "Closed", "dash")
ENDED_STATUSES = {"ended", "closed", "terminated", "implemented"}
# The age tags of the indicator titles (datamart.tags), as the donor reads them: "Children" only
# means the title names no age range, so it is not a group apart from "Under 5".
AGE_LABELS = {"Children": "Age not specified", "Not named": "Age not specified"}
# The syncs that feed the page: the eTools Datamart first (funds and indicators), then the others.
SOURCE_JOBS = ("etools_datamart", "etools", "ai_data")
STATUS = {  # the partner monitoring rule, as the donor reads it (icon + label, never colour alone)
    "on_track": ("good", "On track", "check"),
    "over_target": ("good", "Ahead of schedule", "up"),
    "off_track": ("crit", "Behind", "x"),
    "not_reported": ("warn", "Not reported yet", "warn"),
    "no_target": ("none", "No target", "dash"),
}


def _money(value: Any) -> float:
    return float(value or 0)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def reporting_year(year: int):
    from neurodb.indicators.models import ReportingYear

    return ReportingYear.objects.filter(name__startswith=str(year)).order_by("name").first()


def donor_names() -> list[str]:
    """Every donor name on funds reservation lines, for the account form."""
    names = dm.FundsReservation.objects.exclude(donor="").values_list("donor", flat=True).distinct()
    return sorted({n.strip() for n in names if n and n.strip()}, key=str.casefold)


def _donor_lines(account):
    """Every FR line of the account's donors (and grants), any year."""
    query = Q()
    for name in account.donors:
        if name.strip():
            query |= Q(donor__iexact=name.strip())
    if not query:
        return dm.FundsReservation.objects.none()
    lines = dm.FundsReservation.objects.filter(query).exclude(intervention=None)
    if account.grants:
        lines = lines.filter(grant_number__in=[g.strip() for g in account.grants if g.strip()])
    return lines


def _lines(account, year: int):
    """The account's FR lines on FRs running in ``year``, and the numbers of those FRs."""
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    frs = dm.FundsReservationHeader.objects.filter(
        Q(start_date__isnull=True) | Q(start_date__lte=end),
        Q(end_date__isnull=True) | Q(end_date__gte=start),
    ).values_list("fr_number", flat=True)
    return _donor_lines(account).filter(fr_number__in=frs), frs


def years(account, today: datetime.date) -> list[int]:
    """The years the donor has funds in (FR windows, five years back at most), the current year always."""
    numbers = set(_donor_lines(account).values_list("fr_number", flat=True))
    found = {today.year}
    for start, end in dm.FundsReservationHeader.objects.filter(fr_number__in=numbers).values_list(
        "start_date", "end_date"
    ):
        first = (start or end or today).year
        last = min((end or today).year, today.year)
        found.update(range(max(first, today.year - 5), last + 1))
    return sorted(found, reverse=True)


# ------------------------------------------------------------------------------ your contribution
def contribution(account, year: int, today: datetime.date) -> dict[str, Any]:
    lines, frs = _lines(account, year)
    rows = list(lines.values("fr_number", "grant_number", "intervention_id", "overall_amount"))
    fr_numbers = {r["fr_number"] for r in rows}
    fr_total = {
        r["fr_number"]: _money(r["amount"])
        for r in dm.FundsReservation.objects.filter(fr_number__in=fr_numbers)
        .values("fr_number")
        .annotate(amount=Sum("overall_amount"))
    }
    fr_paid = {
        r["fr_number"]: _money(r["paid"])
        for r in dm.FundsReservationHeader.objects.filter(fr_number__in=fr_numbers)
        .values("fr_number")
        .annotate(paid=Sum("actual_amt"))
    }
    pd_ids = {r["intervention_id"] for r in rows}
    # All the money of each PD (every donor), on its FRs running in the year: the donor's share.
    pd_total = defaultdict(float)
    for r in (
        dm.FundsReservation.objects.filter(intervention_id__in=pd_ids, fr_number__in=frs)
        .values("intervention_id")
        .annotate(amount=Sum("overall_amount"))
    ):
        pd_total[r["intervention_id"]] += _money(r["amount"])

    per_pd: dict[int, dict[str, Any]] = {}
    for r in rows:
        amount = _money(r["overall_amount"])
        if amount <= 0:
            continue
        total = fr_total.get(r["fr_number"]) or amount
        paid = fr_paid.get(r["fr_number"], 0.0) * amount / total
        entry = per_pd.setdefault(
            r["intervention_id"], {"grants": defaultdict(float), "paid": defaultdict(float)}
        )
        grant = r["grant_number"] or "No grant number"
        entry["grants"][grant] += amount
        entry["paid"][grant] += paid

    ryear = reporting_year(year)
    children = overview.children_by_pd(overview.Scope(year=year, reporting_year=ryear, today=today))
    pds = {pd.id: pd for pd in PCA.objects.select_related("partner").filter(id__in=per_pd)}
    partner_alias: dict[str, str] = {}
    out_pds: list[dict[str, Any]] = []
    for pd_id, entry in per_pd.items():
        pd = pds.get(pd_id)
        if pd is None:
            continue
        committed = sum(entry["grants"].values())
        share = min(1.0, committed / pd_total[pd_id]) if pd_total.get(pd_id) else 1.0
        kids = children.get(pd_id) or {}
        whole = kids.get("children", 0.0)
        by_gov = kids.get("by_governorate") or {}
        located = sum(by_gov.values())
        gov = {k: v / located for k, v in by_gov.items() if k in GOVERNORATES and v > 0} if located else {}
        sex = kids.get("sex") or {}
        age = kids.get("age") or {}
        main = kids.get("main") or {}
        tracking = main.get("tracking") or _most_common(kids.get("statuses")) or "not_reported"
        partner = _partner_name(pd, account.show_partner_names, partner_alias)
        end = pd.end or pd.end_date
        ages: dict[str, float] = defaultdict(float)
        for label, value in age.items():
            ages[AGE_LABELS.get(label, label)] += value
        out_pds.append(
            {
                "id": pd.number or f"PD {pd.id}",
                "title": pd.title or "",
                "section": kids.get("section") or _section_of(pd) or "Other",
                "partner": partner,
                "grants": {k: round(v, 2) for k, v in entry["grants"].items()},
                "paid": {k: round(v, 2) for k, v in entry["paid"].items()},
                "committed": round(committed, 2),
                "disbursed": round(sum(entry["paid"].values()), 2),
                "share": round(share, 4),
                "start": pd.start.isoformat() if pd.start else None,
                "end": end.isoformat() if end else None,
                "closed": bool(end and end < today) or (pd.status or "").lower() in ENDED_STATUSES,
                "elapsed": round(kids.get("elapsed") or _elapsed(pd, today), 1),
                "children": round(whole * share, 1),
                "children_whole": round(whole),
                "achieved": round((kids.get("achieved") or 0) * share, 1),
                "target": round((kids.get("target") or 0) * share, 1),
                "gov": {k: round(v, 4) for k, v in gov.items()},
                "sex": _fractions(sex, whole),
                "age": _fractions(ages, whole),
                "indicator": main.get("title") or "",
                "indicator_cumulative": main.get("cumulative"),
                "indicator_target": main.get("target"),
                "status": tracking,
            }
        )
    out_pds.sort(key=lambda p: -p["committed"])

    grant_keys = sorted({g for p in out_pds for g in p["grants"]})
    expiry: dict[str, datetime.date | None] = {}
    descriptions: dict[str, str] = {}
    for name, ends, description in dm.Grant.objects.filter(name__in=grant_keys).values_list(
        "name", "expiry", "description"
    ):
        expiry[name] = ends
        descriptions[name] = description
    grants = []
    for key in grant_keys:
        ends = expiry.get(key)
        grants.append(
            {
                "key": key,
                "label": f"{key} · {descriptions[key]}" if descriptions.get(key) else key,
                "name": descriptions.get(key) or "",
                "expires": ends.isoformat() if ends else None,
                "days_left": (ends - today).days if ends else None,
            }
        )
    return {"pds": out_pds, "grants": grants}


def data_as_of() -> datetime.datetime | None:
    """When the page's data last came in: the last successful eTools Datamart sync (the funds and the
    indicators); without one, the latest successful sync of the other sources (eTools, ActivityInfo)."""
    from neurodb.core.models import SyncRun

    runs = SyncRun.objects.filter(
        job__in=SOURCE_JOBS, status__in=[SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL]
    ).exclude(finished_at=None)
    datamart = runs.filter(job=SyncRun.Job.ETOOLS_DATAMART).order_by("-finished_at").first()
    last = datamart or runs.order_by("-finished_at").first()
    return last.finished_at if last else None


def _most_common(counter: Counter | None) -> str:
    return counter.most_common(1)[0][0] if counter else ""


def _fractions(parts: dict[str, float], whole: float) -> dict[str, float]:
    return {k: round(v / whole, 4) for k, v in parts.items() if whole and v} if whole else {}


def _elapsed(pd, today: datetime.date) -> float:
    start, end = pd.start, pd.end or pd.end_date
    if not start or not end or end <= start:
        return 0.0
    return max(0.0, min(100.0, 100 * (today - start).days / (end - start).days))


def _section_of(pd) -> str:
    return (pd.sectors or "").split(",")[0].strip()


def _partner_name(pd, show: bool, alias: dict[str, str]) -> str:
    name = (pd.partner.name if pd.partner_id and pd.partner else "") or pd.partner_name or "Partner"
    if show:
        return name
    if name not in alias:
        kind = (pd.partner.partner_type if pd.partner_id and pd.partner else "") or ""
        kind = (
            "civil society"
            if "civil" in kind.lower()
            else "government"
            if "government" in kind.lower()
            else ""
        )
        alias[name] = f"Partner {len(alias) + 1}" + (f" ({kind})" if kind else "")
    return alias[name]


# ------------------------------------------------------------------------------ overall
def country(data: dict[str, Any]) -> dict[str, Any]:
    """The overview of the whole country (``overview.build``), as aggregates (see the module docstring)."""
    impact, delivery = data["impact"], data["delivery"]
    counts = delivery.get("status_counts") or {}
    measured = sum(counts.get(k, 0) for k in ("on_track", "over_target", "off_track"))
    on_track = counts.get("on_track", 0) + counts.get("over_target", 0)
    monthly = impact.get("monthly") or {}
    uses_activityinfo = impact.get("children_reached") == impact.get("children_activityinfo") and any(
        monthly.get("activityinfo") or []
    )
    months = monthly.get("activityinfo" if uses_activityinfo else "etools") or []
    governorates = []
    for g in impact.get("by_governorate") or []:
        key = overview.governorate_key(g.get("name"))
        if key in GOVERNORATES:
            governorates.append(
                {
                    "key": key,
                    "reached": round(g.get("reached") or 0),
                    "population": round(g.get("population") or 0) or None,
                    "coverage": g.get("coverage"),
                }
            )
    sex = Counter()
    nationality = Counter()
    disability = 0.0
    for t in impact.get("children_by_tag") or []:
        value = t.get("value") or 0
        if t.get("sex"):
            sex[t["sex"]] += value
        if t.get("nationality"):
            nationality[t["nationality"]] += value
        if t.get("disability"):
            disability += value
    assurance = delivery.get("assurance") or {}
    tpm = (data.get("progress") or {}).get("tpm") or {}
    return {
        "children": round(impact.get("children_reached") or 0),
        "programmes": delivery.get("programme_documents") or 0,
        "partners": delivery.get("partners") or 0,
        "governorates_reached": sum(1 for g in governorates if g["reached"]),
        "on_track_percent": round(100 * on_track / measured) if measured else None,
        "results_measured": measured,
        "status_counts": {k: counts.get(k, 0) for k in STATUS},
        "months": [round(v) for v in months],
        "month_labels": overview.MONTH_LABELS,
        "sections": [
            {
                "section": s["section"],
                "percent": s.get("percent"),
                "elapsed": s.get("elapsed"),
            }
            for s in impact.get("by_section") or []
            if s.get("target")
        ],
        "governorates": governorates,
        "sex": {k: round(v) for k, v in sex.items()},
        "nationality": {k: round(v) for k, v in nationality.most_common()},
        "disability": round(disability),
        "visits": (assurance.get("field_monitoring_visits") or 0) + (tpm.get("completed_total") or 0),
        "sites_visited": tpm.get("sites_visited") or 0,
    }


# ------------------------------------------------------------------------------ page
def build(account, year: int, today: datetime.date | None = None, *, cache: bool = True) -> dict[str, Any]:
    today = today or datetime.date.today()
    use_cache = cache and not settings.DEBUG
    settings_seen = f"{account.show_partner_names}:{sorted(account.donors)}:{sorted(account.grants)}"
    digest = hashlib.sha256(settings_seen.encode()).hexdigest()[:16]  # memcached-safe
    key = f"donor:v{CACHE_VERSION}:{account.pk}:{year}:{today}:{overview.latest_sync_run() or ''}:{digest}"
    if use_cache:
        found = django_cache.get(key)
        if found is not None:
            return found
    mine = contribution(account, year, today)
    everything = overview.build(overview.Scope(year=year, reporting_year=reporting_year(year), today=today))
    whole = country(everything)
    average = {s["section"]: s.get("cost_per_child") for s in everything["money"].get("by_section") or []}
    funded = Counter()
    for p in mine["pds"]:
        funded[p["section"]] += p["committed"]
    order = [name for name, _amount in funded.most_common()]  # the donor's areas first, largest first
    order += [s["section"] for s in whole["sections"] if s["section"] not in order]
    sections = [
        {"key": name, "slot": i + 1 if i < SECTION_SLOTS else 0, "avg": average.get(name)}
        for i, name in enumerate(order)
    ]
    data = {
        "year": year,
        "today": today.isoformat(),
        "sections": sections,
        "governorates": [{"key": k, "name": n, "c": c, "r": r} for k, (n, c, r) in GOVERNORATES.items()],
        "statuses": {
            k: {"cls": c, "label": label, "icon": icon}
            for k, (c, label, icon) in {**STATUS, "closed": CLOSED}.items()
        },
        **mine,
        "overall": whole,
    }
    if use_cache:
        django_cache.set(key, data, CACHE_SECONDS)
    return data


__all__ = ["build", "contribution", "country", "data_as_of", "donor_names", "years"]
