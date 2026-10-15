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

The page loads in parts (``reports.views.brief``): both of its parts read one build, kept two minutes in the
cache every worker shares (:func:`neurodb.core.compute.once`) under the brief's own key (:func:`_cache_key`:
the hand-entered tables, the ActivityInfo partner links staff correct in the admin, the latest review, the
indicator flags and the last finished sync). Nothing in a
build depends on who asks (the staff-only Assign links are the page's, from the person asking); its labels
are translated as it is computed, so a build is kept per language. The page's frame draws the "Figures as
of" line and the Reference block from :func:`lineage`, kept the same way, never from a build.
"""

from __future__ import annotations

import datetime
import json
import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Count, Max, Q, Value
from django.urls import reverse
from django.utils import timezone

from neurodb.core import compute, years
from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.datamart import headline as main_indicators
from neurodb.datamart import models as dm
from neurodb.datamart.children import overrides as children_overrides
from neurodb.datamart.monitoring import is_submitted
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
CACHE_VERSION = 9  # 7: the sections written as JSON in the key (6: in the shared cache, compute.once); 8: the
# calendar year, its ActivityInfo reporting year (or none), like with like, the lineage's jobs; 9: last year's
# reporting year in the key, the children tile's "Not yet" and same ActivityInfo months
RULES_VERSION = "v2"
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
MAX_REASON = 280  # characters: a decision's reason is one or two sentences, not the review's summary
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
    year: int  # the calendar year
    # the reporting year whose ActivityInfo records count for the year (activityinfo.reporting_year_for), None
    # when the year has none
    reporting_year: Any = None
    previous_reporting_year: Any = None  # None: looked up when a build is computed (_build)
    sections: list[str] = field(default_factory=list)
    today: datetime.date = field(default_factory=lambda: years.today())


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


# --------------------------------------------------------------------------------- money, shared
# The brief's money block and NeuroDB Watch's expiring-grant check read the funds through these two
# functions, so that a grant's unspent balance is the same figure in both places.
def fr_lines(pd_ids: list[int], year: int) -> tuple[dict[int, dict[str, float]], list[dict[str, Any]]]:
    """The funds reservations (FR headers) of these PDs that overlap ``year``: per PD, the amounts
    reserved, disbursed and outstanding; and the FR lines (donor, grant) of those headers, each with
    its PD (``pd_id``: the line's own, else its header's) and its header's outstanding and total
    amounts (``fr_outstanding``, ``fr_total``)."""
    first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    headers = list(
        dm.FundsReservationHeader.objects.filter(intervention_id__in=pd_ids)
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
    return dict(per_pd), lines


def grant_balances(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per grant number, from the FR lines of :func:`fr_lines`, in the order of its first line:

    - ``donor``: the donor of its first line;
    - ``reserved``: what its lines reserve;
    - ``unspent``: its unspent balance, each FR's outstanding amount in the grant's share of the FR's
      lines;
    - ``frs``: that balance per FR number;
    - ``pd_ids``: the PDs it funds, in the order of their first line.

    Lines without a grant number are left out."""
    fr_total: Counter[str] = Counter()
    for line in lines:
        fr_total[line["fr_number"]] += _money(line["overall_amount"])
    grants: dict[str, dict[str, Any]] = {}
    for line in lines:
        grant = line["grant_number"] or ""
        if not grant:
            continue
        amount = _money(line["overall_amount"])
        entry = grants.setdefault(
            grant, {"donor": line["donor"] or "", "reserved": 0.0, "unspent": 0.0, "frs": {}, "pd_ids": []}
        )
        entry["reserved"] += amount
        share = amount / fr_total[line["fr_number"]] if fr_total[line["fr_number"]] else 0
        unspent = line["fr_outstanding"] * share
        entry["unspent"] += unspent
        entry["frs"][line["fr_number"]] = entry["frs"].get(line["fr_number"], 0.0) + unspent
        if line["pd_id"] and line["pd_id"] not in entry["pd_ids"]:
            entry["pd_ids"].append(line["pd_id"])
    return grants


# --------------------------------------------------------------------------------- build
def previous_reporting_year(year: int) -> Any:
    """The reporting year whose ActivityInfo records count for the year before ``year``, None when that year
    has none (:func:`neurodb.indicators.services.activityinfo.reporting_year_for`)."""
    from neurodb.indicators.services import activityinfo

    return activityinfo.reporting_year_for(year - 1)


def _latest_review() -> int | None:
    """The id of the latest daily review, in the build's key and the lineage's: read once per request."""
    return compute.per_request("brief:review", lambda: DailyReview.objects.aggregate(last=Max("id"))["last"])


# what staff fill in by hand that the build reads (section plans, finding assignments, and the ActivityInfo
# partner links of the confidence and partners blocks), in the order of the key's text
HAND_ENTERED = {"plans": SectionPlan, "owners": FindingAssignment, "links": PartnerLink}


def _hand_entered() -> str:
    """The tables staff fill in by hand (:data:`HAND_ENTERED`: section plans, finding assignments,
    ActivityInfo partner links) as a short text that changes with every edit: how many rows and the last
    change of each, in one query. Read once per request, so the frame's look at the stored build and the
    build it then draws share one reading."""

    def read() -> str:
        tables = [
            model.objects.annotate(table=Value(name))
            .values("table")  # a constant: one row per table, an empty one included
            .annotate(n=Count("pk"), last=Max("updated_at"))
            .values_list("table", "n", "last")
            .order_by()
            for name, model in HAND_ENTERED.items()
        ]
        found = {name: (n, last) for name, n, last in tables[0].union(*tables[1:], all=True)}
        return ":".join(f"{found[name][0]}:{found[name][1]}" for name in HAND_ENTERED)

    return compute.per_request("brief:hand-entered", read)


def fingerprint() -> str:
    """What a build depends on besides its scope as 16 hex digits (no spaces: memcached-safe): the
    hand-entered tables (section plans, finding assignments, ActivityInfo partner links), the latest review
    and :func:`neurodb.core.compute.data_stamp` (the indicator flags and the last finished sync). In the
    build's key, and in the address of each part of the page (``as_of``, see :func:`build`). Read once per
    request."""
    return compute.fingerprint(f"{_hand_entered()}:{_latest_review()}:{compute.data_stamp()}")


def _cache_key(scope: Scope, stamp: str = "") -> str:
    """Everything the result depends on: the overview's inputs plus the hand-entered tables (section plans,
    finding assignments, ActivityInfo partner links), the latest review, and
    :func:`neurodb.core.compute.data_stamp` (the indicator flags and the last finished sync), so a new
    assignment, plan, partner link, flag or sync shows at once (``stamp``,
    their :func:`fingerprint`, is read now unless given). Nothing about the person asking: a build depends
    on its scope only. The previous year's reporting year (the one whose ActivityInfo records count for
    it) is in the key by its pk, read from the ActivityInfo state without a query: hiding last year's only
    database ("Show on dashboard") changes it, and the build compared with its figures is not served again.
    The sections come from the address as typed: written as JSON, two spellings never share a key (as the
    overview's)."""
    from neurodb.indicators.services import activityinfo

    sections = json.dumps(sorted(scope.sections))
    ryear = getattr(scope.reporting_year, "pk", "")
    before = activityinfo.state().pks.get(scope.year - 1, "")
    return (
        f"brief:v{CACHE_VERSION}:{scope.year}:{ryear}:{before}:{sections}:{scope.today}:"
        f"{stamp or fingerprint()}"
    )


def build(scope: Scope, *, cache: bool = True, as_of: str = "") -> dict[str, Any]:
    """Every block of the management brief for ``scope`` (see the module docstring): computed once across
    every worker and kept two minutes (per language: its labels are translated). ``cache=False`` (and
    DEBUG) computes it afresh. ``as_of``, in a part request, is the :func:`fingerprint` the page's frame
    read: the page's two parts show one build even when a sync, a review or an edit came before or between
    them (:func:`neurodb.core.compute.once_as_of`)."""
    if not cache or settings.DEBUG:
        return _build(scope, cache=cache)
    return compute.once_as_of(
        lambda stamp: _cache_key(scope, stamp), as_of, fingerprint(), lambda: _build(scope), CACHE_SECONDS
    )


def stored(scope: Scope) -> bool:
    """Whether :func:`build` would answer ``scope`` at once, from what is already stored: the page's frame
    then draws the brief inline instead of asking for it (it never computes it itself)."""
    if settings.DEBUG:
        return False
    return compute.peek(_cache_key(scope))[0]


def _build(scope: Scope, *, cache: bool = True) -> dict[str, Any]:
    if scope.previous_reporting_year is None:  # only a build computed needs it, never a stored one
        scope = replace(scope, previous_reporting_year=previous_reporting_year(scope.year))
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
    return _Builder(scope, now, previous).build()


# --------------------------------------------------------------------------------- lineage
LINEAGE_JOBS = (SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Job.POPULATION)


def lineage_jobs(year: int) -> tuple[str, ...]:
    """The sources the brief of ``year`` comes from: :data:`LINEAGE_JOBS`, the ActivityInfo data import only
    in a year with ActivityInfo records (:func:`neurodb.indicators.services.activityinfo.is_year`)."""
    from neurodb.indicators.services import activityinfo

    if activityinfo.is_year(year):
        return tuple(LINEAGE_JOBS)
    return tuple(job for job in LINEAGE_JOBS if job != SyncRun.Job.ACTIVITYINFO_DATA)


def lineage(year: int) -> dict[str, Any]:
    """Where the brief's figures come from (the "Figures as of" line and the Reference block of the page's
    frame): the last successful run of each source (:func:`lineage_jobs`), the year of the population figures,
    the rules and the latest daily review. Plain values, kept two minutes in the shared cache, the same for
    every language (nothing in it is translated), under a key that holds the sources and that a finished sync
    or a new review changes. Never waited for: a frame computes it itself (a few small queries) when another
    request is at it."""
    jobs = lineage_jobs(year)
    if settings.DEBUG:
        return _lineage(year, jobs)
    key = f"brief-lineage:v{CACHE_VERSION}:{year}:{','.join(jobs)}:{_latest_review()}:{compute.data_stamp()}"
    return compute.once(key, lambda: _lineage(year, jobs), CACHE_SECONDS, wait=0, lang_neutral=True)


def _lineage(year: int, jobs: tuple[str, ...] | None = None) -> dict[str, Any]:
    review = DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date").first()
    return lineage_of(
        overview.children_population(year)[1], review, lineage_jobs(year) if jobs is None else jobs
    )


def lineage_of(
    population_year: int | None, review: DailyReview | None, jobs: tuple[str, ...] | list[str] = LINEAGE_JOBS
) -> dict[str, Any]:
    """:func:`lineage` from the population figures' year (the overview's), the latest succeeded review and the
    sources (:func:`lineage_jobs`)."""
    labels = dict(SyncRun.Job.choices)
    sources = []
    for job in jobs:
        last = SyncRun.last_success(job)
        sources.append(
            {
                "job": str(job),
                "label": labels.get(job, job),
                "run": last.id if last else None,
                "when": last.finished_at.isoformat() if last and last.finished_at else "",
                "finished": last.finished_at if last else None,
                "rows": last.rows_written if last else None,
                "status": last.status if last else "never",
            }
        )
    return {
        "sources": sources,
        "population_year": population_year,
        "rules": RULES_VERSION,
        "rules_text": (
            "children rule (title tag plus flags), one main indicator per programme document (its "
            "unique beneficiaries indicator, else its largest children indicator), Reached from "
            "Accepted and Submitted reports, tracking rule ±10 points against the elapsed PD "
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


def decisions(sections: list[str], year: int, review: DailyReview | None = None) -> dict[str, Any]:
    """What management should decide in ``sections``: the AI daily review's decisions (each resting on
    findings of the review), or, when no model chose them, the review's five most important open findings.
    ``review``: the latest succeeded review, looked up when not given. The brief's "Things to decide" and
    Home's "Needs a decision" (which shows neither the owner nor the Assign link)."""
    if review is None:
        review = DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date").first()
    if review is None:
        return {"mode": "none", "items": [], "review_date": None, "decided_by": ""}
    from neurodb.review.services import DECISION_SEVERITIES, RULES, in_sections

    open_findings = [
        f
        for f in review.findings.all()
        if f.state != ReviewFinding.State.RESOLVED and f.severity in DECISION_SEVERITIES
    ]
    by_key = {f.key: f for f in open_findings}
    owners = {a.key: a for a in FindingAssignment.objects.filter(key__in=list(by_key))}
    items = []
    if review.decisions and review.decided_by not in ("", RULES):
        whys = Counter((d.get("why") or "").strip() for d in review.decisions)
        for d in review.decisions:
            cited = [by_key[k] for k in d.get("finding_keys", []) if k in by_key]
            if not cited or not any(in_sections(f.section, sections) for f in cited):
                continue
            why = (d.get("why") or "").strip()
            if whys[why] > 1 or len(why) > MAX_REASON:  # a summary repeated on every card is no reason
                why = cited[0].detail
            items.append(_decision_item(d["decision"], why, cited, owners, d))
        mode = "ai"
    else:
        mine = [f for f in open_findings if in_sections(f.section, sections)]
        for f in mine[:MAX_DECISIONS]:
            items.append(_decision_item(f.title, f.detail, [f], owners, None))
        mode = "rules"
    return {
        "mode": mode,
        "items": items,
        "review_date": review.date.isoformat(),
        "decided_by": review.decided_by if mode == "ai" else "",
        "other_year": review.date.year != year,
    }


def _decision_item(title, detail, cited, owners, ai: dict[str, Any] | None) -> dict[str, Any]:
    severity = (
        ReviewFinding.Severity.CRITICAL
        if any(f.severity == ReviewFinding.Severity.CRITICAL for f in cited)
        else ReviewFinding.Severity.WARNING
    )
    lead = cited[0]
    owner = owners.get(lead.key)
    return {
        "key": lead.key,
        "severity": severity,
        "state": lead.state,
        "section": next((f.section for f in cited if f.section), ""),  # one section per card
        "title": title,
        "detail": detail,
        "who": ai["who"] if ai else "",
        "urgency": ai["urgency"] if ai else "",
        "children": sum(f.children or 0 for f in cited),
        "url": lead.url,
        "findings": [{"title": f.title, "url": f.url} for f in cited] if ai else [],
        "owner": owner.owner if owner else "",
        "due_date": owner.due_date.isoformat() if owner and owner.due_date else "",
        "status": owner.status if owner else FindingAssignment.Status.RAISED,
        "status_label": (
            str(owner.get_status_display()) if owner else str(FindingAssignment.Status.RAISED.label)
        ),
        "assign_url": _assign_url(lead, owner),
    }


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
        (donor, grant) of those headers (:func:`fr_lines`, read once)."""
        if not hasattr(self, "_fund_rows"):
            self._fund_rows = fr_lines(self.pd_ids, self.year)
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
            "scorecard": self._scorecard(confidence),
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
                # ActivityInfo counts this year (Rule 1, and children of this scope): its words show only then
                "activityinfo_used": bool(self.now["impact"].get("activityinfo_used")),
            },
        }
        data["text"] = brief_text(data)
        return data

    # ------------------------------------------------------------------ 1 headline
    def _month_to_date(self, block: dict[str, Any], *, with_ai: bool = True, ai_months: int = 0) -> int:
        """Children reached from January to the current month: the larger of the two sources, or eTools alone
        (``with_ai=False``). eTools: each programme document's main indicator from January to that month (its
        months combined by its method: a sum adds them, a max or the latest does not), summed over the PDs.
        ActivityInfo: its months to the current month, or to ``ai_months`` when given (the same months of
        both years when its records end earlier)."""
        monthly = block.get("monthly") or {}
        to_date = monthly.get("etools_to_date") or []
        if len(to_date) >= self.months_shown:
            etools = to_date[self.months_shown - 1]
        else:  # an overview cached before the to-date series existed
            etools = sum((monthly.get("etools") or [])[: self.months_shown])
        ai = sum((monthly.get("activityinfo") or [])[: ai_months or self.months_shown]) if with_ai else 0
        return half_up(max(etools, ai))

    def _not_used(self, year: int, reporting_year: Any) -> str:
        """Why ActivityInfo does not count in ``year`` for this scope: a year with ActivityInfo records
        (Rule 1, its reporting year) whose records count no children here, or a year without them."""
        if reporting_year is not None:
            return f"ActivityInfo's {year} records count no children for this scope."
        return f"ActivityInfo was not used in {year}."

    def _children_tile(self) -> dict[str, Any]:
        """Children reached to date against the same months of last year, like with like: the larger of the
        two sources only when ActivityInfo counted in both years over the same months (from the same first
        month: ActivityInfo resumed in July is not compared with a full ActivityInfo year; to this year's last
        ActivityInfo month on both sides, since its records arrive about two months late), else eTools against
        eTools. No comparison before any partner report of the year ("Not yet", the overview's January rule),
        for a year that has not started, or when either year has no eTools partner reports; an eTools-only
        comparison is made only when last year's programme documents adding a value are at least half of this
        year's (no "+900 %" against a pilot year). The hint says which comparison is made, or why none is."""
        impact, before = self.now["impact"], self.previous["impact"]
        if impact.get("not_yet") or (self.year > self.today.year and not impact.get("children_reached")):
            # before the first partner report of the year: "Not yet", never a 0 against last year's figure
            hint = (
                f"No partner report for {self.year} yet: the first ones come after the first quarter."
                if self.year <= self.today.year
                else f"{self.year} has not started: no partner report yet."
            )
            # last year's same months, for the brief's source line (never compared on this tile)
            before_to_date = self._month_to_date(before, with_ai=False) if self.year <= self.today.year else 0
            return {
                "value": None,
                "not_yet": True,
                "delta": None,
                "hint": hint,
                "compared_before": before_to_date,
            }
        ai_now = bool(impact.get("activityinfo_used"))
        ai_before = bool(before.get("activityinfo_used"))
        last_year = self.year - 1
        both = (
            ai_now
            and ai_before
            and impact.get("activityinfo_first_month") == before.get("activityinfo_first_month")
        )
        # ActivityInfo's months compared: from January to this year's last ActivityInfo month (when before the
        # current month), the same on both sides; last year's records must reach it
        ai_last = int(impact.get("activityinfo_last_month") or 0)
        ai_months = ai_last if both and 0 < ai_last < self.months_shown else 0
        if both and int(before.get("activityinfo_last_month") or 0) < (ai_months or self.months_shown):
            both = False
        value = self._month_to_date(impact, with_ai=ai_now)
        compared_now = self._month_to_date(impact, with_ai=both, ai_months=ai_months)
        compared_before = self._month_to_date(before, with_ai=both, ai_months=ai_months)
        etools = (
            f"eTools {impact['children_etools']:,} ({main_indicators.TOTAL_RULE}"
            + (
                f"; {impact['children_awaiting']:,} from reports awaiting acceptance"
                if impact.get("children_awaiting")
                else ""
            )
            + ")"
        )
        if ai_now:
            hint = (
                f"{etools} · ActivityInfo {impact['children_activityinfo']:,} in the year; "
                "the larger of the two, never added."
            )
        else:
            hint = f"{etools} · {self._not_used(self.year, self.scope.reporting_year)}"
        counted = impact.get("main_indicators") or {}
        counted_before = before.get("main_indicators") or {}
        pds = (counted.get("flagged") or 0) + (counted.get("largest") or 0)
        pds_before = (counted_before.get("flagged") or 0) + (counted_before.get("largest") or 0)
        delta = None
        if self.year > self.today.year:
            hint += f" Not compared: {self.year} has not started."
        elif both:
            delta = _delta(compared_now, compared_before, "vs same period last year")
            if ai_months:
                first = int(impact.get("activityinfo_first_month") or 1)
                hint += (
                    " ActivityInfo compared over the same months of both years "
                    f"({MONTH_LABELS[first - 1]}–{MONTH_LABELS[ai_months - 1]}): its {self.year} records end "
                    f"in {MONTH_LABELS[ai_months - 1]}."
                )
        elif not pds or not compared_now:
            hint += f" Not compared: {self.year} has no eTools partner reports yet."
        elif not pds_before or not compared_before:
            hint += f" Not compared: {last_year} has no eTools partner reports" + (
                "." if not pds_before else " for the same months."
            )
        elif pds_before * 2 < pds:  # a pilot year: too few documents to compare with
            hint += (
                f" Not compared: {last_year} has eTools partner reports for only {pds_before} programme "
                f"document{'' if pds_before == 1 else 's'}."
            )
        else:
            delta = _delta(compared_now, compared_before, "vs same period last year, eTools only")
            if ai_now:
                hint += " Compared on eTools only: " + (
                    f"ActivityInfo did not cover the same months in {last_year}."
                    if ai_before
                    else self._not_used(last_year, self.scope.previous_reporting_year)
                )
        return {"value": value, "delta": delta, "hint": hint, "compared_before": compared_before}

    def _headline(self) -> dict[str, Any]:
        now, prev = self.now, self.previous
        impact, money, delivery = now["impact"], now["money"], now["delivery"]
        children = self._children_tile()
        children_now = children["value"]
        children_prev = children["compared_before"]
        achievement = impact.get("achievement") or {}
        percent, elapsed = achievement.get("percent"), achievement.get("elapsed")
        counts = delivery["status_counts"]
        tracked = counts["on_track"] + counts["off_track"] + counts["over_target"]
        on_track = _pct(counts["on_track"] + counts["over_target"], tracked)
        month_ago = (
            self._on_track_month_ago() if not self.scope.sections and self.year == self.today.year else None
        )
        tiles = [
            {
                "key": "children",
                "label": "Children reached",
                "value": children_now,  # None before any partner report of the year: "Not yet"
                "not_yet": bool(children.get("not_yet")),
                "format": "number",
                "delta": children["delta"],
                "hint": children["hint"],
                "url": _link(
                    "reports:pd_monitoring", section=self.scope.sections, year=self.year, scope="year"
                ),
            },
            {
                "key": "achievement",
                "label": "Achievement of PD targets (median)",
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
                "hint": (
                    "median share of the target reached by the children indicators (each capped at 100 %), "
                    "against the median share of their PD period elapsed"
                ),
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
        last_year = self.year - 1
        has_last_year = bool(children_prev)  # no children reported then: no comparison on any tile
        if self.year > self.today.year:
            source = (
                f"The overview's figures for {self.year}, which has not started: no change against "
                f"{last_year}."
            )
        elif has_last_year:
            source = (
                f"The overview's figures for this year, against the same months of {last_year} (same "
                "programme documents rule, same children rule)."
            )
        else:
            source = (
                f"The overview's figures for this year. No {last_year} data for the same months to "
                "compare with, so no change against last year is shown."
            )
        if month_ago is not None:
            source += " On track: change against the daily review of 30 days ago."
        return {
            "tiles": tiles,
            "decide": self._decide(),
            "period": (
                f"January {self.year}"
                if self.months_shown == 1
                else f"January to {MONTH_LABELS[self.months_shown - 1]} {self.year}"
            ),
            "last_year": {"year": last_year, "has_data": has_last_year},
            "source": source,
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

    def _decide(self) -> dict[str, Any]:
        """What management should decide (:func:`decisions`), from the review the build read."""
        review = self._latest_review()
        if review is None:
            return {"mode": "none", "items": [], "review_date": None, "decided_by": ""}
        return decisions(self.scope.sections, self.year, review)

    # ------------------------------------------------------------------ 1b scorecard
    def _scorecard(self, confidence: dict[str, Any]) -> dict[str, Any]:
        """Each section in five status cells (good / watch / act), worst sections first: pace,
        indicators on track, spending against results, reporting and confidence. The same figures
        as the blocks below, read as one picture; each cell says what it compares."""
        pace = {s["section"]: s for s in self.now["impact"]["by_section"]}
        delivery = {s["section"]: s for s in self.now["delivery"]["by_section"]}
        money = {s["section"]: s for s in self.now["money"]["by_section"]}
        trust = {r["section"]: r for r in confidence["rows"]}
        rows = []
        for section in self.sections:
            cells = [
                _pace_cell(pace.get(section)),
                _on_track_cell(delivery.get(section)),
                _spending_cell(money.get(section)),
                _reporting_cell(delivery.get(section)),
                _confidence_cell(trust.get(section)),
            ]
            weight = sum({"act": 3, "watch": 1}.get(c["status"], 0) for c in cells)
            rows.append({"section": section, "cells": cells, "weight": weight, "url": _brief_url(section)})
        rows.sort(key=lambda r: (-r["weight"], r["section"]))
        return {
            "columns": [c["column"] for c in rows[0]["cells"]] if rows else [],
            "rows": rows,
            "counts": dict(Counter(c["status"] for r in rows for c in r["cells"])),
            "source": (
                "Pace: share of the target reached against the share of the PD period elapsed "
                "(on pace within 10 points, watch within 20). On track: indicators on track or over "
                "target among those tracked (good from 70 %, watch from 50 %). Spending: points "
                "disbursed ahead of achieved (good up to 10, watch up to 25, as the daily review's "
                "check). Reporting: indicators with a progress report "
                f"(good from {CONFIDENCE['reported']} %, watch from 50 %). Confidence: the How sure block."
            ),
        }

    # ------------------------------------------------------------------ 2 pace
    def _pace(self) -> dict[str, Any]:
        """Per section, the programme documents' main indicators (one per PD, the headline rule):
        Reached to date against their targets and what was expected by now, with the Country Programme
        target; and the children reached this year, projected to December."""
        plans = self._plans()
        medians = {s["section"]: s for s in self.now["impact"]["by_section"]}
        bullets = []
        mains = [s for s in self.now["impact"].get("main_by_section") or [] if s.get("target")]
        for s in mains:
            plan = plans.get(s["section"])
            bullets.append(
                {
                    "section": s["section"],
                    "achieved": s["reached"],
                    "target": s["target"],
                    "expected": s["expected"],
                    "cp_target": plan.children_target if plan and plan.children_target else None,
                    "percent": s["percent"],
                    "elapsed": (medians.get(s["section"]) or {}).get("elapsed"),
                    "programme_documents": s["programme_documents"],
                }
            )
        impact = self.now["impact"]
        monthly = impact["monthly"]
        target = sum(s["target"] for s in mains)
        series = {
            "eTools": _project_to_date(monthly.get("etools_to_date"), monthly["etools"], self.months_shown)
        }
        colors = {"eTools": "--nd-primary"}
        if impact.get("activityinfo_used"):  # ActivityInfo counts this year: reported to its last month
            last = int(impact.get("activityinfo_last_month") or self.months_shown)
            series["ActivityInfo"] = _project(monthly["activityinfo"], self.months_shown, last)
            colors["ActivityInfo"] = "--nd-series-2"
        return {
            "bullets": bullets,
            "has_cp_targets": any(b["cp_target"] for b in bullets),
            "projection": {
                "labels": list(MONTH_LABELS),
                "actual_months": self.months_shown,
                "series": series,
                "target": target,
                "colors": colors,
            },
            "source": (
                "Each programme document's main indicator (its unique beneficiaries indicator, else its "
                "largest children indicator), summed over the section's PDs: Reached to date against the "
                "sum of those indicators' targets; expected = target × share of the PD period elapsed; "
                "Country Programme targets from the section plans entered in the admin; projection = the "
                f"last {PROJECTION_MONTHS} reported months' average, to December. Partners may reach the "
                "same children"
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
        linked_used = self.scope.reporting_year is not None
        implausible = self.now["impact"].get("implausible") or []
        set_aside = Counter(x["section"] for x in implausible)
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
            # What keeps the section from high confidence, in the reader's words (shown by the badge)
            reasons = [
                reason
                for short_of, reason in (
                    (
                        reported is None or reported < CONFIDENCE["reported"],
                        "few indicators have a progress report",
                    ),
                    (
                        verified_pct is None or verified_pct < CONFIDENCE["verified"],
                        "few programme documents were verified by a visit",
                    ),
                    (
                        # only in a year with ActivityInfo records: an eTools-only year has nothing to link to
                        linked_used and (linked_pct is None or linked_pct < CONFIDENCE["linked"]),
                        "partners are not linked to ActivityInfo",
                    ),
                    (sync_days is None, "eTools is not yet synced on this server"),
                    (
                        sync_days is not None and sync_days > CONFIDENCE["sync_days"],
                        f"eTools was last synced {sync_days} days ago",
                    ),
                )
                if short_of
            ]
            short = len(reasons)
            if set_aside.get(section):
                reasons.insert(0, f"{set_aside[section]} reported value(s) left out as errors")
            rows.append(
                {
                    "section": section,
                    "indicators": delivery.get("total", 0),
                    "reported_percent": reported,
                    "verified_percent": verified_pct,
                    "linked_percent": linked_pct,
                    "sync_days": sync_days,
                    "set_aside": set_aside.get(section, 0),
                    "reasons": reasons,
                    # a value set aside as an error makes the section's figures low, whatever else
                    "level": "low"
                    if set_aside.get(section)
                    else ("high" if short == 0 else ("medium" if short == 1 else "low")),
                }
            )
        return {
            "rows": rows,
            "implausible": implausible,
            "linked_used": linked_used,
            "thresholds": CONFIDENCE,
            "reconcile": self._reconcile(),
            "timeliness": self._timeliness(),
            # the partner-link criterion counts only in a year with ActivityInfo records (linked_used)
            "source": (
                "Indicators with a progress report this year; PDs with a completed TPM activity or a field "
                "monitoring finding on their partner this year; "
                + ("partners with an ActivityInfo partner link; " if linked_used else "")
                + "days since the last eTools Datamart sync"
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
            submitted = is_submitted(r)
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
        districts = self._district_gaps()
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
            "districts": districts,
            # No district reached at all: say why rather than show a column of zeros. Either no report
            # is placed in a district, or the districts the reports name are not those of the
            # population figures (the names differ).
            "districts_matched": any(d["reached"] for d in districts),
            "districts_reported": [d["district"] for d in self.now["impact"]["by_district"][:3]],
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
            submitted = is_submitted(r)
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
        for line in lines:
            amount = _money(line["overall_amount"])
            section = pds.get(line["pd_id"], {}).get("section") or self.section_of_pd.get(
                line["pd_id"], "Other"
            )
            donor_section[(line["donor"] or "Unknown", section or "Other")] += amount
        balances = grant_balances(lines)
        children = {s["section"]: s["children"] for s in self.now["impact"].get("main_by_section") or []}
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
        expiry = {g.name: g.expiry for g in dm.Grant.objects.filter(name__in=list(balances))}
        grants = []
        for grant, balance in balances.items():
            when = expiry.get(grant)
            days = (when - self.today).days if when else None
            unspent = half_up(balance["unspent"], 2)
            if not unspent:
                continue
            grants.append(
                {
                    "grant": grant,
                    "donor": balance["donor"],
                    "reserved": half_up(balance["reserved"], 2),
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
                "section from its indicators, children reached this year by the main indicator of each "
                "programme document of the section (summed over the PDs); a grant's unspent "
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
        from neurodb.review.services import DELTA_CHECKS, _trend

        latest = self._latest_review()
        if latest is None:
            return {}
        first = latest.date - datetime.timedelta(days=DIGEST_DAYS - 1)
        reviews = list(
            DailyReview.objects.filter(
                status=DailyReview.Status.SUCCEEDED, date__gte=first, date__lte=latest.date
            ).order_by("-date")
        )
        # The oldest review of the week is the baseline: "new" counts what appeared after it (its
        # own findings are new against a review outside the week, or all new on the very first run).
        later = reviews[:-1]
        new_critical = ReviewFinding.objects.filter(
            review__in=later, state=ReviewFinding.State.NEW, severity=ReviewFinding.Severity.CRITICAL
        ).count()
        resolved = ReviewFinding.objects.filter(review__in=later, state=ReviewFinding.State.RESOLVED).count()
        lines = [str(t) for t in _trend(reviews)]
        # Findings about a change ("newly off track") show on one day only: counts can then fall
        # with nothing resolved, which the digest says when it happened this week.
        one_day = ReviewFinding.objects.filter(
            review__in=reviews[1:], check_id__in=DELTA_CHECKS, severity=ReviewFinding.Severity.CRITICAL
        ).exists()
        return {
            "first": first.isoformat(),
            "last": latest.date.isoformat(),
            "since": reviews[-1].date.isoformat(),
            "reviews": len(reviews),
            "new_critical": new_critical,
            "resolved": resolved,
            "trend": lines,
            "one_day_findings": one_day,
            "summary": latest.summary,
        }

    # ------------------------------------------------------------------ 8 lineage
    def _lineage(self) -> dict[str, Any]:
        return lineage_of(
            self.now["impact"]["population_year"], self._latest_review(), lineage_jobs(self.year)
        )


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


def _project(monthly: list[float], shown: int, last: int | None = None) -> list[float | None]:
    """Cumulative reach by month: reported months, then the last months' average to December. ``last``, the
    source's last reported month when it comes before ``shown`` (ActivityInfo's records arrive about two
    months late): the months after it, to ``shown``, hold the total to date (they are drawn as reported, so
    never a projected value), the projection starts after ``shown``, and its pace is that of the months up
    to ``last``, never of the empty ones after it."""
    values = [float(v or 0) for v in (monthly or [])[:12]] + [0.0] * (12 - len(monthly or []))
    reported = shown if last is None else max(min(last, shown), 0)
    if shown <= 0 or not any(values[:reported]):
        return []
    out: list[float | None] = []
    total = 0.0
    for v in values[:reported]:
        total += v
        out.append(half_up(total))
    out.extend(half_up(total) for _ in range(reported, shown))  # not reported yet: the total to date
    recent = values[max(reported - PROJECTION_MONTHS, 0) : reported]
    pace = sum(recent) / len(recent) if recent else 0.0
    for _ in range(shown, 12):
        total += pace
        out.append(half_up(total))
    return out


def _project_to_date(to_date: list[float] | None, monthly: list[float], shown: int) -> list[float | None]:
    """:func:`_project` from Reached from January to each month (what a max or the latest does not
    add up month by month); the monthly values when the series is missing."""
    if not to_date:
        return _project(monthly, shown)
    values = [float(v or 0) for v in to_date[:12]]
    steps = [values[0]] + [values[i] - values[i - 1] for i in range(1, len(values))]
    return _project(steps, shown)


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
        "Children reached",
        "The larger of the eTools PRP and ActivityInfo figures. The two are never added because the same "
        "children are often reported in both. eTools: each programme document counts once, with its main "
        "indicator (its unique beneficiaries indicator, else its largest children indicator), summed over "
        "the programme documents; partners may reach the same children.",
    ),
    (
        "Achievement of PD targets (median)",
        "The median share of the target reached by the children indicators, each capped at 100 %. Targets "
        "of different indicators are never added.",
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
        "Disbursed to date by the PDs with a children result, over the children they reached this year "
        "(each PD's main indicator). Compare sections, not absolute values.",
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


# --------------------------------------------------------------------------------- scorecard cells
def _cell(column: str, status: str, value: str, detail: str) -> dict[str, str]:
    return {"column": column, "status": status, "value": value, "detail": detail}


def _band(value: float, good: float, watch: float) -> str:
    return "good" if value >= good else ("watch" if value >= watch else "act")


def _pace_cell(row: dict[str, Any] | None) -> dict[str, str]:
    if not row or row.get("percent") is None or row.get("elapsed") is None:
        return _cell("Pace", "none", "—", "No children indicator with a target")
    gap = row["percent"] - row["elapsed"]
    return _cell(
        "Pace",
        _band(gap, -10, -20),
        percent(row["percent"], 0),
        f"median {percent(row['percent'], 0)} of the target reached by the children indicators, "
        f"{percent(row['elapsed'], 0)} of the time elapsed",
    )


def _on_track_cell(row: dict[str, Any] | None) -> dict[str, str]:
    row = row or {}
    tracked = row.get("on_track", 0) + row.get("over_target", 0) + row.get("off_track", 0)
    if not tracked:
        return _cell("On track", "none", "—", "No indicator tracked yet")
    share = 100 * ((row["on_track"] + row["over_target"]) / tracked)
    return _cell(
        "On track",
        _band(share, 70, 50),
        percent(share, 0),
        f"{row['on_track'] + row['over_target']:,} of {tracked:,} tracked indicators on track or ahead of "
        "schedule",
    )


def _spending_cell(row: dict[str, Any] | None) -> dict[str, str]:
    if not row or row.get("disbursed_percent") is None or row.get("achieved_percent") is None:
        return _cell("Spending", "none", "—", "No funds or no achievement to compare")
    ahead = row["disbursed_percent"] - row["achieved_percent"]
    status = "good" if ahead <= 10 else ("watch" if ahead <= 25 else "act")
    return _cell(
        "Spending",
        status,
        percent(row["disbursed_percent"], 0),
        f"{percent(row['disbursed_percent'], 0)} of reserved funds disbursed, "
        f"{percent(row['achieved_percent'], 0)} of the target achieved",
    )


def _reporting_cell(row: dict[str, Any] | None) -> dict[str, str]:
    total = (row or {}).get("total", 0)
    if not total:
        return _cell("Reporting", "none", "—", "No indicator")
    share = 100 * row.get("reported", 0) / total
    return _cell(
        "Reporting",
        _band(share, CONFIDENCE["reported"], 50),
        percent(share, 0),
        f"{row.get('reported', 0):,} of {total:,} indicators have a progress report",
    )


def _confidence_cell(row: dict[str, Any] | None) -> dict[str, str]:
    if not row:
        return _cell("Confidence", "none", "—", "Not assessed")
    level = row["level"]
    status = {"high": "good", "medium": "watch"}.get(level, "act")
    reasons = row.get("reasons") or []
    detail = f"{level.capitalize()} because {'; '.join(reasons)}" if reasons else "See How sure are we?"
    return _cell("Confidence", status, level.capitalize(), detail)


def _brief_url(section: str) -> str:
    return reverse("reports:brief") + "?" + urlencode({"section": section})


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
    if children["value"] is None:  # no partner report of the year yet: never a 0
        lines.append("Children reached: not yet.")
    else:
        lines.append(
            f"Children reached: {children['value']:,}"
            + (f" ({_signed(delta['value'])}% {delta['label']})" if delta else "")
            + "."
        )
    ach = tiles["achievement"]
    if ach["value"] is not None:
        lines.append(
            f"Achievement of PD targets (median): {percent(ach['value'], 0)}"
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
    decide = data["headline"]["decide"]
    if decide["items"]:
        how = (
            f"chosen by the AI daily review of {decide['review_date']}"
            if decide["mode"] == "ai"
            else f"the daily review of {decide['review_date']}, most important findings first"
        )
        lines += ["", f"To decide ({how}):"]
        for i, d in enumerate(decide["items"], start=1):
            who = f" Who: {d['who']}" if d["who"] else ""
            when = f", {d['urgency']}" if d["urgency"] else ""
            owner = f" Owner: {d['owner']}" if d["owner"] else ""
            due = f", due {d['due_date']}" if d["due_date"] else ""
            lines.append(f"{i}. {d['title'].rstrip('.')}.{who}{when}.{owner}{due}".replace("..", "."))
    low = [r["section"] for r in data["confidence"]["rows"] if r["level"] == "low"]
    if low:
        lines += ["", f"Low confidence in the figures of: {', '.join(low)}."]
    aside = data["confidence"].get("implausible") or []
    if aside:
        lines.append(
            f"Left out of children reached as reporting errors: {len(aside)} indicator"
            + ("s" if len(aside) != 1 else "")
            + " ("
            + "; ".join(f"{x['pd']} {x['title'][:60]}: {x['value']:,}, {x['reason']}" for x in aside[:3])
            + ("; …" if len(aside) > 3 else "")
            + "). Check them in eTools."
        )
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
