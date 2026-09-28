"""The management brief: the country overview read for decisions.

One call to :func:`build` reads the overview of the year and of the year before (same service, same
programme documents, same rules), and adds what senior management asked for on top: comparisons,
the year-end at the current pace, how sure the figures are (reporting, verification, partner
links, the two sources side by side, timeliness), who and where the children are, a scorecard per
partner, the money from donor to section to child, the grants about to expire, funded against
required, and whether the daily review's findings are being acted on.

Three figures are entered by hand in the admin because eTools does not hold them: the Country
Programme target and the funds required per section (``reports.SectionPlan``) and who owns a
finding (``review.FindingAssignment``). Everything else is read from the synced tables. The two
reporting sources stay apart, as on the overview: a total is the larger of the two, never the sum.
"""

from __future__ import annotations

import datetime
import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.cache import cache as django_cache
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart.children import overrides as children_overrides
from neurodb.datamart.monitoring import SUBMITTED
from neurodb.facts import queries as fact_queries
from neurodb.facts.services.dashboard import fact_filter
from neurodb.partnerships.models import PCA, PartnerLink
from neurodb.reports import overview
from neurodb.reports.models import SectionPlan
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.web.templatetags.ui import half_up, percent
from neurodb.web.templatetags.ui import money as money_text

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120
CACHE_VERSION = 2
RULES_VERSION = "v1"
MONTH_LABELS = overview.MONTH_LABELS
PROJECTION_MONTHS = 3  # the pace is the average of the last three reported months
CONFIDENCE = {"reported": 80, "verified": 30, "linked": 90, "sync_days": 2}
GAP_TOLERANCE = 10  # percent: the two sources agree within this
GRANT_HORIZON_DAYS = 90
MAX_DECISIONS = 5
MAX_RECONCILE = 15
MAX_TIMELINESS = 25
MAX_DISTRICTS = 12
MAX_GRANTS = 12
LIFECYCLE_DAYS = 30
DIGEST_DAYS = 7
HIGH_RISK = overview.HIGH_RISK
AGE_ORDER = ("Under 5", "Under 18", "Children", "Adolescents", "Youth")
NATIONALITY_OF_POPULATION = {
    "LEB": "Lebanese",
    "SYR": "Syrian",
    "PRS": "Palestinian",
    "PRL": "Palestinian",
    "PAL": "Palestinian",
}
NATIONALITY_OF_TAG = {"Lebanese": "Lebanese", "Syrian": "Syrian", "Palestinian": "Palestinian"}


@dataclass
class Scope:
    year: int
    reporting_year: Any = None  # this year's ReportingYear (ActivityInfo), None when none exists
    previous_reporting_year: Any = None
    sections: list[str] = field(default_factory=list)
    today: datetime.date = field(default_factory=datetime.date.today)


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return half_up(part * 100 / whole, 1)


def _money(value: Any) -> float:
    return float(value or 0)


def _link(name: str, **params: Any) -> str:
    from urllib.parse import urlencode

    url = reverse(name)
    query = [
        (k, v) for k, values in params.items() for v in (values if isinstance(values, list) else [values])
    ]
    query = [(k, v) for k, v in query if v not in (None, "")]
    return f"{url}?{urlencode(query)}" if query else url


def _same_day_last_year(day: datetime.date) -> datetime.date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


# --------------------------------------------------------------------------------- build
def _cache_key(scope: Scope) -> str:
    """Everything the result depends on: the overview's inputs plus the hand-entered tables, the
    latest review and the last finished sync (a new assignment, plan or sync shows at once)."""
    from neurodb.datamart.models import IndicatorFlag

    flags = IndicatorFlag.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    plans = SectionPlan.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    owners = FindingAssignment.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    review = DailyReview.objects.aggregate(last=Max("id"))
    import hashlib

    sections = "|".join(sorted(scope.sections))
    ryear = getattr(scope.reporting_year, "pk", "")
    fingerprint = (
        f"{flags['n']}:{flags['last']}:{plans['n']}:{plans['last']}:{owners['n']}:{owners['last']}:"
        f"{review['last']}:{overview.latest_sync_run()}"
    )
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]  # no spaces: memcached-safe
    return f"brief:v{CACHE_VERSION}:{scope.year}:{ryear}:{sections}:{scope.today}:{digest}"


def build(scope: Scope, *, cache: bool = True) -> dict[str, Any]:
    """Every block of the management brief for ``scope`` (see the module docstring)."""
    use_cache = cache and not settings.DEBUG
    key = _cache_key(scope)
    if use_cache:
        found = django_cache.get(key)
        if found is not None:
            return found
    now = overview.build(
        overview.Scope(
            year=scope.year, reporting_year=scope.reporting_year, sections=scope.sections, today=scope.today
        ),
        cache=cache,
    )
    previous = overview.build(
        overview.Scope(
            year=scope.year - 1,
            reporting_year=scope.previous_reporting_year,
            sections=scope.sections,
            today=_same_day_last_year(scope.today),
        ),
        cache=cache,
    )
    data = _Builder(scope, now, previous).build()
    if use_cache:
        django_cache.set(key, data, CACHE_SECONDS)
    return data


class _Builder:
    def __init__(self, scope: Scope, now: dict[str, Any], previous: dict[str, Any]):
        self.scope = scope
        self.now = now
        self.previous = previous
        self.today = scope.today
        self.year = scope.year
        self.flags = children_overrides()
        self.pd_ids: list[int] = list(now["scope"].get("pd_ids") or [])
        self.section_of_pd: dict[int, str] = {
            int(k): v for k, v in (now["scope"].get("section_of_pd") or {}).items()
        }
        self.months_shown = self.today.month if self.year == self.today.year else 12
        self.sections = sorted(
            {r["section"] for r in now["delivery"]["by_section"]}
            | {r["section"] for r in now["impact"]["by_section"]}
        )

    # ------------------------------------------------------------------ shared reads
    def _pds(self) -> dict[int, dict[str, Any]]:
        if not hasattr(self, "_pd_rows"):
            rows = PCA.objects.filter(id__in=self.pd_ids).select_related("partner")
            self._pd_rows = {
                pd.id: {
                    "id": pd.id,
                    "number": pd.number or f"PD {pd.id}",
                    "partner_id": pd.partner_id,
                    "partner": (pd.partner.name if pd.partner_id and pd.partner else pd.partner_name) or "",
                    "rating": (pd.partner.rating or "") if pd.partner_id and pd.partner else "",
                    "status": (pd.status or "").lower(),
                    "end": pd.end,
                    "section": self.section_of_pd.get(pd.id, ""),
                }
                for pd in rows
            }
        return self._pd_rows

    def _partner_ids(self) -> set[int]:
        return {p["partner_id"] for p in self._pds().values() if p["partner_id"]}

    def _funds(self) -> tuple[dict[int, dict[str, float]], list[dict[str, Any]]]:
        """Per PD: reserved, disbursed, outstanding (FR headers overlapping the year); and the FR lines
        (donor, grant) of those headers."""
        if hasattr(self, "_fund_rows"):
            return self._fund_rows
        first, last = datetime.date(self.year, 1, 1), datetime.date(self.year, 12, 31)
        headers = list(
            dm.FundsReservationHeader.objects.filter(intervention_id__in=self.pd_ids)
            .filter(
                Q(start_date__isnull=True) | Q(start_date__lte=last),
                Q(end_date__isnull=True) | Q(end_date__gte=first),
            )
            .values("intervention_id", "fr_number", "total_amt", "actual_amt", "outstanding_amt")
        )
        per_pd: dict[int, dict[str, float]] = defaultdict(
            lambda: {"reserved": 0.0, "disbursed": 0.0, "outstanding": 0.0}
        )
        fr_of: dict[str, dict[str, Any]] = {}
        for h in headers:
            per_pd[h["intervention_id"]]["reserved"] += _money(h["total_amt"])
            per_pd[h["intervention_id"]]["disbursed"] += _money(h["actual_amt"])
            per_pd[h["intervention_id"]]["outstanding"] += _money(h["outstanding_amt"])
            if h["fr_number"]:
                fr_of[h["fr_number"]] = h
        lines = list(
            dm.FundsReservation.objects.filter(fr_number__in=list(fr_of)).values(
                "fr_number", "intervention_id", "donor", "grant_number", "overall_amount"
            )
        )
        for line in lines:
            header = fr_of[line["fr_number"]]
            line["pd_id"] = line["intervention_id"] or header["intervention_id"]
            line["fr_outstanding"] = _money(header["outstanding_amt"])
            line["fr_total"] = _money(header["total_amt"])
        self._fund_rows = (dict(per_pd), lines)
        return self._fund_rows

    def _reports(self) -> list[dict[str, Any]]:
        """One row per progress report of the year's partners: due, submitted, status, period end."""
        if hasattr(self, "_report_rows"):
            return self._report_rows
        rows = (
            dm.ReportedIndicator.objects.filter(
                partner_id__in=self._partner_ids(), period_end__year=self.year, report_type="QPR"
            )
            .values(
                "partner_id", "progress_report", "due_date", "submission_date", "report_status", "period_end"
            )
            .distinct()
        )
        seen: dict[tuple[int, str], dict[str, Any]] = {}
        for r in rows:
            seen.setdefault((r["partner_id"], r["progress_report"]), r)
        self._report_rows = list(seen.values())
        return self._report_rows

    def _plans(self) -> dict[str, SectionPlan]:
        if not hasattr(self, "_plan_rows"):
            self._plan_rows = {p.section: p for p in SectionPlan.objects.filter(year=self.year)}
        return self._plan_rows

    def _sync_age_days(self) -> int | None:
        last = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)
        if last is None or last.finished_at is None:
            return None
        return max((timezone.now() - last.finished_at).days, 0)

    # ------------------------------------------------------------------ build
    def build(self) -> dict[str, Any]:
        headline = self._headline()
        pace = self._pace()
        confidence = self._confidence()
        partners = self._partners()
        money = self._money()
        action = self._action()
        data = {
            "headline": headline,
            "pace": pace,
            "confidence": confidence,
            "who": self._who(),
            "partners": partners,
            "money": money,
            "action": action,
            "lineage": self._lineage(),
            "dictionary": DICTIONARY,
            "scope": {
                "year": self.year,
                "sections": list(self.scope.sections),
                "today": self.today.isoformat(),
                "months_shown": self.months_shown,
                "all_sections": self.sections,
            },
        }
        data["text"] = brief_text(data)
        return data

    # ------------------------------------------------------------------ 1 headline
    def _month_to_date(self, block: dict[str, Any]) -> int:
        """Children reached from January to the current month, the larger of the two sources."""
        monthly = block.get("monthly") or {}
        etools = sum((monthly.get("etools") or [])[: self.months_shown])
        ai = sum((monthly.get("activityinfo") or [])[: self.months_shown])
        return half_up(max(etools, ai))

    def _headline(self) -> dict[str, Any]:
        now, prev = self.now, self.previous
        impact, money, delivery = now["impact"], now["money"], now["delivery"]
        children_now = self._month_to_date(impact)
        children_prev = self._month_to_date(prev["impact"])
        achieved = sum(s["achieved"] for s in impact["by_section"])
        target = sum(s["target"] for s in impact["by_section"])
        elapsed = (
            sum((s["elapsed"] or 0) * s["target"] for s in impact["by_section"]) / target if target else None
        )
        percent = _pct(achieved, target)
        counts = delivery["status_counts"]
        tracked = counts["on_track"] + counts["off_track"] + counts["over_target"]
        on_track = _pct(counts["on_track"] + counts["over_target"], tracked)
        month_ago = (
            self._on_track_month_ago() if not self.scope.sections and self.year == self.today.year else None
        )
        tiles = [
            {
                "key": "children",
                "label": "Children reached, at least",
                "value": children_now,
                "format": "number",
                "delta": _delta(children_now, children_prev, "vs same period last year"),
                "hint": (
                    f"eTools {impact['children_etools']:,} · ActivityInfo "
                    f"{impact['children_activityinfo']:,} in the year; the two are never added"
                ),
                "url": _link(
                    "reports:pd_monitoring", section=self.scope.sections, year=self.year, scope="year"
                ),
            },
            {
                "key": "achievement",
                "label": "Achievement of PD targets",
                "value": percent,
                "format": "percent",
                "delta": (
                    {
                        "value": half_up(percent - elapsed, 1),
                        "label": f"vs {half_up(elapsed)}% of the period elapsed",
                        "good": percent >= elapsed - 10,
                        "unit": "pts",
                    }
                    if percent is not None and elapsed is not None
                    else None
                ),
                "hint": "cumulative children against the sum of the PD targets",
                "url": _link(
                    "reports:pd_monitoring", section=self.scope.sections, year=self.year, scope="year"
                ),
            },
            {
                "key": "disbursed",
                "label": "Funds disbursed",
                "value": money["disbursed"],
                "format": "money",
                "delta": (
                    {
                        "value": money["disbursed_percent"],
                        "label": f"of {money_text(money['reserved'])} reserved",
                        "good": True,
                        "unit": "% ",
                        "plain": True,
                    }
                    if money["disbursed_percent"] is not None
                    else None
                ),
                "hint": "reserved and disbursed to date on the PDs running this year",
                "url": reverse("reports:funds"),
            },
            {
                "key": "cost",
                "label": "Cost per child",
                "value": money["cost_per_child"],
                "format": "money",
                "delta": _delta(
                    money["cost_per_child"],
                    prev["money"]["cost_per_child"],
                    "vs last year's PDs",
                    lower_is_good=True,
                ),
                "hint": money["cost_caveat"],
                "url": "",
            },
            {
                "key": "on_track",
                "label": "Indicators on track",
                "value": on_track,
                "format": "percent",
                "delta": (
                    {
                        "value": half_up(on_track - month_ago, 1),
                        "label": "pts vs 30 days ago",
                        "good": on_track >= month_ago,
                        "unit": "pts",
                    }
                    if on_track is not None and month_ago is not None
                    else None
                ),
                "hint": f"{counts['off_track']:,} off track · {counts['not_reported']:,} not reported",
                "url": _link(
                    "reports:pd_monitoring",
                    section=self.scope.sections,
                    year=self.year,
                    scope="year",
                    status="off_track",
                ),
            },
        ]
        return {
            "tiles": tiles,
            "decide": self._decide(),
            "period": f"January to {MONTH_LABELS[self.months_shown - 1]} {self.year}",
            "source": (
                "The overview's blocks for this year and for the same months of the year before (same "
                "programme documents rule, same children rule); on-track share of the daily review 30 days "
                "ago"
            ),
        }

    def _on_track_month_ago(self) -> float | None:
        """The tile's share ((on track + over target) of the tracked) in the review of 30 days ago, of
        the same year: from its counts of the PDs running in the year (older reviews kept only the
        active PDs' counts)."""
        cutoff = self.today - datetime.timedelta(days=30)
        review = (
            DailyReview.objects.filter(
                status=DailyReview.Status.SUCCEEDED, date__lte=cutoff, date__year=self.year
            )
            .order_by("-date")
            .first()
        )
        if review is None:
            return None
        stats = review.stats or {}
        counts = stats.get("status_counts_year") or stats.get("status_counts") or {}
        on = (counts.get("on_track") or 0) + (counts.get("over_target") or 0)
        return _pct(on, on + (counts.get("off_track") or 0))

    def _latest_review(self) -> DailyReview | None:
        if not hasattr(self, "_review"):
            self._review = (
                DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date").first()
            )
        return self._review

    def _decide(self) -> list[dict[str, Any]]:
        """The five findings of the latest review that matter most, with who owns them."""
        review = self._latest_review()
        if review is None:
            return []
        from neurodb.review.services import in_sections

        findings = [
            f
            for f in review.findings.all()
            if f.state != ReviewFinding.State.RESOLVED
            and f.severity in (ReviewFinding.Severity.CRITICAL, ReviewFinding.Severity.WARNING)
            and in_sections(f.section, self.scope.sections)
        ][:MAX_DECISIONS]
        owners = {a.key: a for a in FindingAssignment.objects.filter(key__in=[f.key for f in findings])}
        out = []
        for f in findings:
            owner = owners.get(f.key)
            out.append(
                {
                    "key": f.key,
                    "severity": f.severity,
                    "state": f.state,
                    "section": f.section,
                    "title": f.title,
                    "detail": f.detail,
                    "children": f.children,
                    "url": f.url,
                    "owner": owner.owner if owner else "",
                    "due_date": owner.due_date.isoformat() if owner and owner.due_date else "",
                    "status": owner.status if owner else FindingAssignment.Status.RAISED,
                    "status_label": (
                        str(owner.get_status_display())
                        if owner
                        else str(FindingAssignment.Status.RAISED.label)
                    ),
                    "assign_url": _assign_url(f, owner),
                }
            )
        return out

    # ------------------------------------------------------------------ 2 pace
    def _pace(self) -> dict[str, Any]:
        plans = self._plans()
        bullets = []
        for s in self.now["impact"]["by_section"]:
            plan = plans.get(s["section"])
            bullets.append(
                {
                    "section": s["section"],
                    "achieved": s["achieved"],
                    "target": s["target"],
                    "expected": half_up(s["target"] * (s["elapsed"] or 0) / 100),
                    "cp_target": plan.children_target if plan and plan.children_target else None,
                    "percent": s["percent"],
                    "elapsed": s["elapsed"],
                }
            )
        monthly = self.now["impact"]["monthly"]
        target = sum(s["target"] for s in self.now["impact"]["by_section"])
        return {
            "bullets": bullets,
            "has_cp_targets": any(b["cp_target"] for b in bullets),
            "projection": {
                "labels": list(MONTH_LABELS),
                "actual_months": self.months_shown,
                "series": {
                    "eTools": _project(monthly["etools"], self.months_shown),
                    "ActivityInfo": _project(monthly["activityinfo"], self.months_shown),
                },
                "target": target,
                "colors": {"eTools": "--nd-primary", "ActivityInfo": "--nd-series-2"},
            },
            "source": (
                "PD indicators and PRP reports (children indicators with a target); expected = target × "
                "share of the PD period elapsed; Country Programme targets from the section plans entered "
                f"in the admin; projection = the last {PROJECTION_MONTHS} reported months' average, to "
                "December"
            ),
        }

    # ------------------------------------------------------------------ 3 confidence
    def _confidence(self) -> dict[str, Any]:
        pds = self._pds()
        first = datetime.date(self.year, 1, 1)
        verified_pds = set(
            dm.TPMActivity.objects.filter(
                intervention_id__in=self.pd_ids,
                visit__status__in=overview.TPM_COMPLETED,
                visit__start_date__gte=first,
            ).values_list("intervention_id", flat=True)
        )
        verified_partners = set(
            dm.MonitoringFinding.objects.filter(
                partner_id__in=self._partner_ids(), end_date__gte=first
            ).values_list("partner_id", flat=True)
        )
        linked = set(
            PartnerLink.objects.filter(partner_id__in=self._partner_ids()).values_list(
                "partner_id", flat=True
            )
        )
        sync_days = self._sync_age_days()
        by_section = {r["section"]: r for r in self.now["delivery"]["by_section"]}
        rows = []
        for section in self.sections:
            section_pds = [p for p in pds.values() if p["section"] == section]
            partners = {p["partner_id"] for p in section_pds if p["partner_id"]}
            verified = [
                p for p in section_pds if p["id"] in verified_pds or p["partner_id"] in verified_partners
            ]
            delivery = by_section.get(section) or {}
            reported = _pct(delivery.get("reported"), delivery.get("total"))
            verified_pct = _pct(len(verified), len(section_pds))
            linked_pct = _pct(len(partners & linked), len(partners))
            short = sum(
                [
                    reported is None or reported < CONFIDENCE["reported"],
                    verified_pct is None or verified_pct < CONFIDENCE["verified"],
                    linked_pct is None or linked_pct < CONFIDENCE["linked"],
                    sync_days is None or sync_days > CONFIDENCE["sync_days"],
                ]
            )
            rows.append(
                {
                    "section": section,
                    "indicators": delivery.get("total", 0),
                    "reported_percent": reported,
                    "verified_percent": verified_pct,
                    "linked_percent": linked_pct,
                    "sync_days": sync_days,
                    "level": "high" if short == 0 else ("medium" if short == 1 else "low"),
                }
            )
        return {
            "rows": rows,
            "thresholds": CONFIDENCE,
            "reconcile": self._reconcile(),
            "timeliness": self._timeliness(),
            "source": (
                "Indicators with a progress report this year; PDs with a completed TPM activity or a field "
                "monitoring finding on their partner this year; partners with an ActivityInfo partner link; "
                "days since the last eTools Datamart sync"
            ),
        }

    def _reconcile(self) -> list[dict[str, Any]]:
        """Children reached this year per partner, ActivityInfo against eTools, for partners linked
        across the two sources; sorted by the gap."""
        ryear = self.scope.reporting_year
        etools = {
            r["partner_id"]: r["children"]
            for r in self.now["delivery"]["by_partner"]
            if r["partner_id"] and r["children"]
        }
        names = {r["partner_id"]: r["partner"] for r in self.now["delivery"]["by_partner"] if r["partner_id"]}
        if ryear is None:
            return []
        links = defaultdict(list)
        for label, partner_id in PartnerLink.objects.exclude(partner=None).values_list("label", "partner_id"):
            links[label].append(partner_id)
        ai: dict[int, float] = defaultdict(float)
        for database, master_ids in overview.children_masters(
            ryear, self.scope.sections, self.flags
        ).values():
            try:
                rows = fact_queries.master_values_by_partner(fact_filter(database), sorted(master_ids))
            except Exception:  # one database's facts must not break the page
                logger.exception("brief: ActivityInfo values by partner of database %s failed", database.pk)
                continue
            for r in rows:
                for partner_id in links.get(r["partner"] or "", []):
                    ai[partner_id] += float(r["value"] or 0)
        out = []
        for partner_id in set(etools) | set(ai):
            if partner_id not in names:
                continue  # an ActivityInfo partner without a PD running this year
            e, a = half_up(etools.get(partner_id, 0)), half_up(ai.get(partner_id, 0))
            if not e and not a:
                continue
            gap = _pct(e - a, max(e, a))
            out.append(
                {
                    "partner_id": partner_id,
                    "partner": names[partner_id],
                    "activityinfo": a,
                    "etools": e,
                    "gap_percent": gap,
                    "consistent": gap is not None and abs(gap) <= GAP_TOLERANCE,
                    "url": reverse("reports:partner_profile", args=[partner_id]),
                }
            )
        out.sort(key=lambda r: (-abs(r["gap_percent"] or 0), r["partner"]))
        return out[:MAX_RECONCILE]

    def _timeliness(self) -> dict[str, Any]:
        """Quarterly progress reports per partner: on time, late, missing or not yet due."""
        pds = self._pds()
        names = {p["partner_id"]: p["partner"] for p in pds.values() if p["partner_id"]}
        cells: dict[int, dict[int, str]] = defaultdict(dict)
        rank = {"on_time": 0, "late": 1, "missing": 2, "not_due": 3}
        for r in self._reports():
            if not r["period_end"]:
                continue
            quarter = (r["period_end"].month - 1) // 3
            submitted = r["submission_date"] is not None or (r["report_status"] or "").lower() in SUBMITTED
            due = r["due_date"]
            if submitted:
                state = "late" if due and r["submission_date"] and r["submission_date"] > due else "on_time"
            elif due and due < self.today:
                state = "missing"
            else:
                state = "not_due"
            current = cells[r["partner_id"]].get(quarter)
            if current is None or rank[state] > rank[current]:
                cells[r["partner_id"]][quarter] = state  # the worst report of the quarter
        counts = Counter(state for by_quarter in cells.values() for state in by_quarter.values())
        rows = []
        for partner_id, by_quarter in cells.items():
            if partner_id not in names:
                continue
            rows.append(
                {
                    "partner_id": partner_id,
                    "partner": names[partner_id],
                    "cells": [by_quarter.get(q, "") for q in range(4)],
                    "late": sum(1 for s in by_quarter.values() if s in ("late", "missing")),
                    "url": reverse("reports:partner_profile", args=[partner_id]),
                }
            )
        rows.sort(key=lambda r: (-r["late"], r["partner"]))
        return {
            "quarters": [f"Q{q + 1} {self.year}" for q in range(4)],
            "rows": rows[:MAX_TIMELINESS],
            "more": max(len(rows) - MAX_TIMELINESS, 0),
            "counts": {k: counts.get(k, 0) for k in rank},
        }

    # ------------------------------------------------------------------ 4 who and where
    def _who(self) -> dict[str, Any]:
        rows = self._children_rows_with_tags()
        by_age: dict[str, Counter[str]] = defaultdict(Counter)
        by_nationality: Counter[str] = Counter()
        disability = 0.0
        total = 0.0
        for row in rows:
            value = row["value"]
            total += value
            age = row["age_group"] or "Age not named"
            by_age[age][row["sex"]] += value
            if row["nationality"]:
                by_nationality[row["nationality"]] += value
            if row["disability"]:
                disability += value
        ages = sorted(by_age, key=lambda a: (AGE_ORDER.index(a) if a in AGE_ORDER else 99, a))
        population, population_year = self._population_by_nationality()
        nationality = []
        for name in ("Lebanese", "Syrian", "Palestinian"):
            reached = by_nationality.get(name, 0)
            nationality.append(
                {
                    "name": name,
                    "reached": half_up(reached),
                    "reached_share": _pct(reached, sum(by_nationality.values())),
                    "population_share": population.get(name),
                }
            )
        return {
            "sex_age": {
                "labels": ages,
                "series": {
                    "Girls": [half_up(by_age[a]["Girls"]) for a in ages],
                    "Boys": [half_up(by_age[a]["Boys"]) for a in ages],
                    "Not named": [half_up(by_age[a][""]) for a in ages],
                },
                "colors": {"Girls": "--nd-series-4", "Boys": "--nd-primary", "Not named": "--nd-neutral"},
            }
            if ages
            else {},
            "children_tagged": half_up(total),
            "nationality": nationality,
            "nationality_named": half_up(sum(by_nationality.values())),
            "disability": {"reached": half_up(disability), "share": _pct(disability, total)},
            "governorates": self.now["impact"]["by_governorate"],
            "districts": self._district_gaps(),
            "population_year": population_year,  # of the nationality shares (the caption under them)
            "source": (
                "The children indicators' titles (girls or boys, age band, nationality, disability), PRP "
                "reports of the year; population shares from the population figures; districts from the "
                "locations reported in eTools (ActivityInfo records are kept at governorate level)"
            ),
        }

    def _children_rows_with_tags(self) -> list[dict[str, Any]]:
        """The eTools children indicators of the year with their values and what their titles name
        (computed by the overview from the same rows, so nothing is read twice)."""
        return list(self.now["impact"].get("children_by_tag") or [])

    def _population_by_nationality(self) -> tuple[dict[str, float | None], int | None]:
        """Each nationality's share of the child population, and the year of the figures used."""
        base = PopulationFigure.objects.filter(
            category="children", age_group="", sex="", vulnerability_level="", year__lte=self.year
        ).exclude(nationality="ALL")
        year = base.order_by("-year").values_list("year", flat=True).first()
        if year is None:
            return {}, None
        level = "national" if base.filter(year=year, level="national").exists() else "governorate"
        totals: Counter[str] = Counter()
        for r in base.filter(year=year, level=level).values("nationality", "value"):
            name = NATIONALITY_OF_POPULATION.get(r["nationality"], "Other")
            totals[name] += r["value"] or 0
        whole = sum(totals.values())
        return {name: _pct(value, whole) for name, value in totals.items()}, year

    def _district_gaps(self) -> list[dict[str, Any]]:
        base = PopulationFigure.objects.filter(
            category="children",
            level="district",
            age_group="",
            sex="",
            vulnerability_level="",
            year__lte=self.year,
        )
        year = base.order_by("-year").values_list("year", flat=True).first()
        if year is None:
            return []
        rows = list(base.filter(year=year).values("area_name", "parent_name", "nationality", "value"))
        with_all = {r["area_name"] for r in rows if r["nationality"] == "ALL"}
        population: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["area_name"] in with_all and r["nationality"] != "ALL":
                continue
            entry = population.setdefault(
                overview.governorate_key(r["area_name"]),
                {"district": r["area_name"], "governorate": r["parent_name"], "children": 0},
            )
            entry["children"] += r["value"] or 0
        reached = {
            overview.governorate_key(d["district"]): d["etools"] for d in self.now["impact"]["by_district"]
        }
        out = []
        for key, entry in population.items():
            got = reached.get(key, 0)
            out.append(
                {
                    **entry,
                    "reached": got,
                    "coverage": _pct(got, entry["children"]),
                    "not_reached": max(entry["children"] - got, 0),
                }
            )
        out.sort(key=lambda d: (-d["not_reached"], d["district"]))
        return out[:MAX_DISTRICTS]

    # ------------------------------------------------------------------ 5 partners
    def _partners(self) -> dict[str, Any]:
        pds = self._pds()
        per_pd, _lines = self._funds()
        reports_by_partner: dict[int, Counter[str]] = defaultdict(Counter)
        for r in self._reports():
            due = r["due_date"]
            submitted = r["submission_date"] is not None or (r["report_status"] or "").lower() in SUBMITTED
            if not due or (not submitted and due >= self.today):
                continue  # not yet due
            reports_by_partner[r["partner_id"]]["due"] += 1
            if submitted and (not r["submission_date"] or r["submission_date"] <= due):
                reports_by_partner[r["partner_id"]]["on_time"] += 1
        points_by_partner: dict[int, Counter[str]] = defaultdict(Counter)
        for r in dm.ActionPoint.objects.filter(partner_id__in=self._partner_ids()).values(
            "partner_id", "status", "due_date", "date_of_completion"
        ):
            due = r["due_date"]
            if not due or due.year < self.year - 1:
                continue
            is_open = r["status"] in dm.ActionPoint.OPEN_STATUSES
            if is_open and due >= self.today:
                continue  # not yet due
            done = r["date_of_completion"].date() if r["date_of_completion"] else None
            points_by_partner[r["partner_id"]]["due"] += 1
            if not is_open and done and done <= due:
                points_by_partner[r["partner_id"]]["on_time"] += 1
        findings = {}
        for r in (
            dm.MonitoringFinding.objects.filter(partner_id__in=self._partner_ids(), end_date__year=self.year)
            .exclude(overall_finding_rating="")
            .order_by("partner_id", "-end_date")
            .values("partner_id", "overall_finding_rating")
        ):
            findings.setdefault(r["partner_id"], r["overall_finding_rating"])  # the latest per partner
        rows = []
        for g in self.now["delivery"]["by_partner"]:
            partner_id = g["partner_id"]
            money = {"reserved": 0.0, "disbursed": 0.0}
            for pd_id in g["pd_ids"]:
                amounts = per_pd.get(pd_id)
                if amounts:
                    money["reserved"] += amounts["reserved"]
                    money["disbursed"] += amounts["disbursed"]
            reports = reports_by_partner.get(partner_id, Counter())
            points = points_by_partner.get(partner_id, Counter())
            rating = next((pds[i]["rating"] for i in g["pd_ids"] if i in pds), "")
            disbursed = _pct(money["disbursed"], money["reserved"])
            rows.append(
                {
                    "partner_id": partner_id,
                    "partner": g["partner"],
                    "pds": g["pds"],
                    "indicators": g["indicators"],
                    "reserved": half_up(money["reserved"], 2),
                    "on_track_percent": g["on_track_percent"],
                    "reports_on_time_percent": _pct(reports["on_time"], reports["due"]),
                    "points_on_time_percent": _pct(points["on_time"], points["due"]),
                    "risk": rating,
                    "high_risk": (rating or "").strip().lower() in HIGH_RISK,
                    "finding": findings.get(partner_id, ""),
                    "disbursed_percent": disbursed,
                    "achieved_percent": g["achieved_percent"],
                    "children": g["children"],
                    "quadrant": _quadrant(disbursed, g["achieved_percent"]),
                    "url": reverse("reports:partner_profile", args=[partner_id]) if partner_id else "",
                }
            )
        rows.sort(
            key=lambda r: (r["on_track_percent"] if r["on_track_percent"] is not None else 101, r["partner"])
        )
        return {
            "scorecard": rows,
            "quadrant": [
                {
                    "name": r["partner"],
                    "x": r["disbursed_percent"],
                    "y": r["achieved_percent"],
                    "size": r["reserved"],
                    "quadrant": r["quadrant"],
                }
                for r in rows
                if r["disbursed_percent"] is not None and r["achieved_percent"] is not None
            ],
            "decisions": self.now["money"]["decisions"],
            "source": (
                "PD indicators (tracking rule), progress reports due and submitted, action points due and "
                "closed, partner risk rating, the latest field monitoring finding, funds reservations of the "
                "partner's PDs running this year"
            ),
        }

    # ------------------------------------------------------------------ 6 money
    def _money(self) -> dict[str, Any]:
        _per_pd, lines = self._funds()
        pds = self._pds()
        donor_section: Counter[tuple[str, str]] = Counter()
        grant_amount: Counter[str] = Counter()
        grant_unspent: Counter[str] = Counter()
        grant_donor: dict[str, str] = {}
        fr_lines_total: Counter[str] = Counter()
        for line in lines:
            fr_lines_total[line["fr_number"]] += _money(line["overall_amount"])
        for line in lines:
            amount = _money(line["overall_amount"])
            section = pds.get(line["pd_id"], {}).get("section") or self.section_of_pd.get(
                line["pd_id"], "Other"
            )
            donor_section[(line["donor"] or "Unknown", section or "Other")] += amount
            grant = line["grant_number"] or ""
            if grant:
                grant_amount[grant] += amount
                grant_donor.setdefault(grant, line["donor"] or "")
                share = amount / fr_lines_total[line["fr_number"]] if fr_lines_total[line["fr_number"]] else 0
                grant_unspent[grant] += line["fr_outstanding"] * share
        children = {s["section"]: s["achieved"] for s in self.now["impact"]["by_section"]}
        donors = Counter()
        for (donor, _section), amount in donor_section.items():
            donors[donor] += amount
        top = [d for d, _ in donors.most_common(overview.MAX_DONORS)]
        flows = []
        for (donor, section), amount in sorted(donor_section.items(), key=lambda kv: -kv[1]):
            flows.append(
                {
                    "donor": donor if donor in top else "Other",
                    "section": section,
                    "amount": half_up(amount, 2),
                }
            )
        merged: Counter[tuple[str, str]] = Counter()
        for f in flows:
            merged[(f["donor"], f["section"])] += f["amount"]
        expiry = {g.name: g.expiry for g in dm.Grant.objects.filter(name__in=list(grant_amount))}
        grants = []
        for grant, amount in grant_amount.items():
            when = expiry.get(grant)
            days = (when - self.today).days if when else None
            unspent = half_up(grant_unspent[grant], 2)
            if not unspent:
                continue
            grants.append(
                {
                    "grant": grant,
                    "donor": grant_donor.get(grant, ""),
                    "reserved": half_up(amount, 2),
                    "unspent": unspent,
                    "expiry": when.isoformat() if when else "",
                    "days": days,
                    "at_risk": days is not None and days <= GRANT_HORIZON_DAYS,
                }
            )
        grants.sort(key=lambda g: (g["days"] if g["days"] is not None else 10**6, -g["unspent"]))
        plans = self._plans()
        reserved = {s["section"]: s["reserved"] for s in self.now["money"]["by_section"]}
        funded = []
        for section in self.sections:
            plan = plans.get(section)
            required = _money(plan.required_usd) if plan and plan.required_usd else None
            funded.append(
                {
                    "section": section,
                    "required": required,
                    "reserved": half_up(reserved.get(section, 0.0), 2),
                    "percent": _pct(reserved.get(section, 0.0), required) if required else None,
                }
            )
        return {
            "flows": {
                "donors": [{"name": d, "amount": half_up(donors[d], 2)} for d in top]
                + (
                    [
                        {
                            "name": "Other",
                            "amount": half_up(sum(v for d, v in donors.items() if d not in top), 2),
                        }
                    ]
                    if len(donors) > len(top)
                    else []
                ),
                "links": [
                    {"donor": d, "section": s, "amount": half_up(a, 2)} for (d, s), a in merged.items()
                ],
                "sections": [
                    {"name": s, "children": children.get(s, 0), "reserved": half_up(reserved.get(s, 0.0), 2)}
                    for s in self.sections
                ],
            },
            "grants": grants[:MAX_GRANTS],
            "funded": funded,
            "has_requirements": any(f["required"] for f in funded),
            "reserved": self.now["money"]["reserved"],
            "disbursed": self.now["money"]["disbursed"],
            "source": (
                "Funds reservation lines (donor, grant) of the FRs of the PDs running this year, each PD's "
                "section from its indicators, children from the section's indicators; a grant's unspent "
                "balance = each FR's outstanding amount, in the grant's share of the FR's lines; grant "
                "expiry from the grants table; requirements from the section plans entered in the admin"
            ),
        }

    # ------------------------------------------------------------------ 7 action
    def _action(self) -> dict[str, Any]:
        from neurodb.review.services import in_sections

        window = self.today - datetime.timedelta(days=LIFECYCLE_DAYS)
        reviews = list(
            DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED, date__gte=window).order_by("date")
        )
        raised: dict[str, datetime.date] = {}
        resolved: dict[str, datetime.date] = {}
        for review in reviews:
            for f in review.findings.all():
                if not in_sections(f.section, self.scope.sections):
                    continue
                if f.state == ReviewFinding.State.NEW:
                    raised.setdefault(f.key, review.date)
                elif f.state == ReviewFinding.State.RESOLVED:
                    resolved.setdefault(f.key, review.date)
        assignments = {
            a.key: a for a in FindingAssignment.objects.filter(key__in=list(raised)) if a.key in raised
        }
        steps = {"acknowledged": [], "assigned": [], "closed": []}
        counts = Counter()
        for key, day in raised.items():
            a = assignments.get(key)
            start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
            if a and a.acknowledged_at:
                counts["acknowledged"] += 1
                steps["acknowledged"].append(max((a.acknowledged_at - start).days, 0))
            if a and a.assigned_at:
                counts["assigned"] += 1
                steps["assigned"].append(max((a.assigned_at - (a.acknowledged_at or start)).days, 0))
            closed_at = a.closed_at if a and a.closed_at else None
            if closed_at is None and key in resolved:
                closed_at = datetime.datetime.combine(resolved[key], datetime.time.min, tzinfo=datetime.UTC)
            if closed_at is not None:
                counts["closed"] += 1
                steps["closed"].append(
                    max((closed_at - (a.assigned_at if a and a.assigned_at else start)).days, 0)
                )
        open_assignments = FindingAssignment.objects.exclude(status=FindingAssignment.Status.CLOSED)
        owners: dict[str, dict[str, Any]] = {}
        for a in open_assignments:
            if not in_sections(a.section, self.scope.sections):
                continue
            owner = a.owner or "Unassigned"
            entry = owners.setdefault(owner, {"owner": owner, "open": 0, "oldest_days": 0, "past_due": 0})
            entry["open"] += 1
            entry["oldest_days"] = max(entry["oldest_days"], (timezone.now() - a.created_at).days)
            if a.due_date and a.due_date < self.today:
                entry["past_due"] += 1
        return {
            "lifecycle": {
                "days": LIFECYCLE_DAYS,
                "raised": len(raised),
                "acknowledged": counts["acknowledged"],
                "assigned": counts["assigned"],
                "closed": counts["closed"],
                "median_days": {
                    k: (half_up(statistics.median(v), 1) if v else None) for k, v in steps.items()
                },
            },
            "owners": sorted(owners.values(), key=lambda o: (-o["past_due"], -o["oldest_days"], o["owner"])),
            "digest": self._digest(),
            "source": (
                f"Daily review findings first seen in the last {LIFECYCLE_DAYS} days, their assignments "
                "(acknowledged, assigned, closed dates) and the findings the review itself saw resolved"
            ),
        }

    def _digest(self) -> dict[str, Any]:
        from neurodb.review.services import _trend

        latest = self._latest_review()
        if latest is None:
            return {}
        first = latest.date - datetime.timedelta(days=DIGEST_DAYS - 1)
        reviews = list(
            DailyReview.objects.filter(
                status=DailyReview.Status.SUCCEEDED, date__gte=first, date__lte=latest.date
            ).order_by("-date")
        )
        new_critical = ReviewFinding.objects.filter(
            review__in=reviews, state=ReviewFinding.State.NEW, severity=ReviewFinding.Severity.CRITICAL
        ).count()
        resolved = ReviewFinding.objects.filter(
            review__in=reviews, state=ReviewFinding.State.RESOLVED
        ).count()
        lines = [str(t) for t in _trend(reviews)]
        return {
            "first": first.isoformat(),
            "last": latest.date.isoformat(),
            "reviews": len(reviews),
            "new_critical": new_critical,
            "resolved": resolved,
            "trend": lines,
            "summary": latest.summary,
        }

    # ------------------------------------------------------------------ 8 lineage
    def _lineage(self) -> dict[str, Any]:
        labels = dict(SyncRun.Job.choices)
        sources = []
        for job in (SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Job.POPULATION):
            last = SyncRun.last_success(job)
            sources.append(
                {
                    "job": str(job),
                    "label": labels.get(job, job),
                    "run": last.id if last else None,
                    "when": last.finished_at.isoformat() if last and last.finished_at else "",
                    "rows": last.rows_written if last else None,
                    "status": last.status if last else "never",
                }
            )
        review = self._latest_review()
        return {
            "sources": sources,
            "population_year": self.now["impact"]["population_year"],
            "rules": RULES_VERSION,
            "rules_text": (
                "children rule (title tag plus flags), tracking rule ±10 points against the elapsed PD "
                "period, two sources never added, cost per child on the same PDs, confidence thresholds "
                f"{CONFIDENCE['reported']}/{CONFIDENCE['verified']}/{CONFIDENCE['linked']} % and "
                f"{CONFIDENCE['sync_days']} days"
            ),
            "review": {
                "date": review.date.isoformat(),
                "checks": review.checks_run,
                "findings": review.findings.count(),
                "narrated_by": review.narrated_by,
            }
            if review
            else None,
        }


# --------------------------------------------------------------------------------- helpers
def _delta(now: float | None, before: float | None, label: str, *, lower_is_good: bool = False):
    if now is None or before in (None, 0):
        return None
    change = half_up((now - before) * 100 / before, 1)
    return {
        "value": change,
        "label": label,
        "good": (change <= 0) if lower_is_good else (change >= 0),
        "unit": "%",
    }


def _project(monthly: list[float], shown: int) -> list[float | None]:
    """Cumulative reach by month: reported months, then the last months' average to December."""
    values = [float(v or 0) for v in (monthly or [])[:12]] + [0.0] * (12 - len(monthly or []))
    if shown <= 0 or not any(values[:shown]):
        return []
    out: list[float | None] = []
    total = 0.0
    for v in values[:shown]:
        total += v
        out.append(half_up(total))
    recent = values[max(shown - PROJECTION_MONTHS, 0) : shown]
    pace = sum(recent) / len(recent) if recent else 0.0
    for _ in range(shown, 12):
        total += pace
        out.append(half_up(total))
    return out


def _quadrant(disbursed: float | None, achieved: float | None) -> str:
    if disbursed is None or achieved is None:
        return ""
    if achieved >= disbursed:
        return "ahead"
    return "behind" if achieved < 50 and disbursed >= 50 else "spending_ahead"


def _assign_url(finding: ReviewFinding, owner: FindingAssignment | None) -> str:
    from urllib.parse import urlencode

    if owner is not None:
        return reverse("admin:review_findingassignment_change", args=[owner.pk])
    return (
        reverse("admin:review_findingassignment_add")
        + "?"
        + urlencode({"key": finding.key, "title": finding.title, "section": finding.section})
    )


DICTIONARY = [
    (
        "Children reached, at least",
        "The larger of the eTools PRP and ActivityInfo figures. The two are never added because the same "
        "children are often reported in both.",
    ),
    (
        "Expected at this point",
        "The PD target multiplied by the share of the PD period elapsed. On track means within 10 points "
        "of it.",
    ),
    (
        "Verified by a visit",
        "A programme document with a completed TPM activity this year, or whose partner had a field "
        "monitoring finding this year.",
    ),
    (
        "Cost per child",
        "Disbursed to date by the PDs with a children result, over the children they reached this year. "
        "Compare sections, not absolute values.",
    ),
    (
        "Confidence",
        f"High when reported ≥ {CONFIDENCE['reported']} %, verified ≥ {CONFIDENCE['verified']} %, linked ≥ "
        f"{CONFIDENCE['linked']} % and the sync is at most {CONFIDENCE['sync_days']} days old; medium when "
        "one falls short; low when two or more do.",
    ),
    (
        "Gap",
        "eTools children minus ActivityInfo children for the same partner this year, as a share of the "
        f"larger; consistent within {GAP_TOLERANCE} %.",
    ),
    (
        "Unspent balance of a grant",
        "Each funds reservation's outstanding amount, in the grant's share of that reservation's lines; an "
        "approximation, as eTools carries the outstanding amount per reservation, not per grant.",
    ),
    (
        "Owner",
        "The role or team that owns a finding of the daily review, entered in the admin; never a person's "
        "name.",
    ),
]


def _signed(value: float) -> str:
    """A delta as the tile pill shows it: whole, halves rounded up, "+" when above zero."""
    return f"{'+' if value > 0 else ''}{half_up(value)}"


def brief_text(data: dict[str, Any]) -> str:
    """The brief as plain text for the minutes: the same numbers as on screen."""
    scope = data["scope"]
    tiles = {t["key"]: t for t in data["headline"]["tiles"]}
    sections = ", ".join(scope["sections"]) if scope["sections"] else "all sections"
    lines = [
        f"NeuroDB management brief · {sections} · {data['headline']['period']} · "
        f"figures as of {scope['today']}",
        "",
    ]
    children = tiles["children"]
    delta = children["delta"]
    lines.append(
        f"Children reached, at least: {children['value']:,}"
        + (f" ({_signed(delta['value'])}% {delta['label']})" if delta else "")
        + "."
    )
    ach = tiles["achievement"]
    if ach["value"] is not None:
        lines.append(
            f"Achievement of PD targets: {percent(ach['value'], 0)}"
            + (f" ({ach['delta']['label']})" if ach["delta"] else "")
            + "."
        )
    dis = tiles["disbursed"]
    lines.append(
        f"Funds: {money_text(dis['value'])} disbursed"
        + (f" ({half_up(dis['delta']['value'])}% {dis['delta']['label']})" if dis["delta"] else "")
        + "."
    )
    cost = tiles["cost"]
    if cost["value"] is not None:
        lines.append(
            f"Cost per child: {money_text(cost['value'])}"
            + (f" ({_signed(cost['delta']['value'])}% {cost['delta']['label']})" if cost["delta"] else "")
            + "."
        )
    track = tiles["on_track"]
    if track["value"] is not None:
        lines.append(f"Indicators on track: {percent(track['value'], 0)} ({track['hint']}).")
    if data["headline"]["decide"]:
        lines += ["", "To decide this month:"]
        for i, d in enumerate(data["headline"]["decide"], start=1):
            owner = f" Owner: {d['owner']}" if d["owner"] else ""
            due = f", due {d['due_date']}" if d["due_date"] else ""
            lines.append(f"{i}. {d['title']}.{owner}{due}")
    low = [r["section"] for r in data["confidence"]["rows"] if r["level"] == "low"]
    if low:
        lines += ["", f"Low confidence in the figures of: {', '.join(low)}."]
    risk = [g for g in data["money"]["grants"] if g["at_risk"]]
    if risk:
        lines.append(
            f"Grants expiring within {GRANT_HORIZON_DAYS} days with an unspent balance: "
            + ", ".join(f"{g['grant']} ({g['donor']}, {money_text(g['unspent'])})" for g in risk[:5])
            + "."
        )
    lineage = data["lineage"]
    runs = ", ".join(f"{s['label']} run {s['run']}" for s in lineage["sources"] if s["run"])
    lines += ["", f"Source: NeuroDB, {runs or 'no sync yet'}, rules {lineage['rules']}."]
    return "\n".join(lines)


__all__ = ["Scope", "build", "brief_text"]
