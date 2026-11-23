"""Is the data ready for machine learning? Before any model is chosen, this measures what each source
really holds (how much history, how complete, how places and indicators are coded, what outcomes
people recorded) and says, for each programme decision a model could support, whether the data is
ready, partly ready or not yet, and why.

Everything here is counted in the database; nothing leaves NeuroDB and nothing is predicted. The
thresholds are written below so the verdicts can be discussed and changed. ActivityInfo is not measured:
it is an archive of past monthly results, no longer a source. A check stored before (with its ActivityInfo
sources and the verdicts that rested on them) is shown without them (:func:`shown`).

Two more sources say how far the data could carry the analyses the programme may want later: how well
the sources link up (:func:`links`: the share of each source's rows that carry a link, and how it was found
where NeuroDB keeps that) and what arrives each year (:func:`arrivals`: reports, audits, visits, action
points and PME verifications per calendar year, each counted by the rule of the page that counts it, the
accepted values a typical running PD indicator has, and the action point history kept). Each analysis for
later gets a verdict (group ``"later"``) with a plain reason when it is not ready and, where the data's pace
allows it, about when it could be (:func:`verdicts`). Counts, dates and statuses only: no person, no free
text. The job runs monthly (core 0034); :func:`since` says what changed between the last two checks.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from django.db.models import Count, Max, Min, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------- thresholds
PD_LOCATIONS = 0.50
POPULATION_AGE_YEARS = 3
REPORTS_WITH_DATES = 30  # eTools progress reports with a due and a submission date
REVIEW_DAYS = 30
OUTCOMES = 50  # findings people resolved or took on: the examples a review model would learn from

# The analyses the programme may want later (the verdicts of group "later"). Each threshold is a floor to
# start the discussion from, not a proof that a method would work; a year is the last full calendar year.
PDS_PER_GROUP = 10  # running PDs a comparable group needs (the PDs one master indicator brings together)
COMPARABLE_GROUPS = 5  # comparable groups of that size
MASTER_LINKED = 0.50  # running PD indicators in a master indicator (a confirmed link)
VALUES_PER_INDICATOR = 4  # accepted values a typical running PD indicator has, to compare it with others
THEME_FM_FINDINGS = 100  # field monitoring findings a year, to group into themes
THEME_AUDIT_FINDINGS = 30  # audit findings a year, likewise
CHANGE_VALUES = 6  # accepted values a typical running PD indicator has, to see a trend
CHANGE_QUARTERS = 6  # quarters those values cover
FOLLOW_UP_VERIFIED = 100  # action points PME verified or rejected: the examples to learn from
FOLLOW_UP_HISTORY_DAYS = 365  # days of action point history kept (each action point's status, day by day)
FOLLOW_UP_COMPLETED = 100  # action points completed a year
BEFORE_AFTER_EVENTS = 20  # visits and audits a year on a PD with accepted values before and after them
BEFORE_AFTER_DAYS = 180  # how far before and after a visit or an audit its values are looked for
BEFORE_AFTER_VALUES = 5  # accepted values a typical running PD indicator has: about 3 before and 2 after
PACE_YEAR_SHARE = 0.25  # this year's pace says nothing about the whole year before a quarter of it has passed


@dataclass
class Metric:
    key: str
    label: str
    value: float | int | str | None
    display: str
    note: str = ""


@dataclass
class Section:
    key: str
    label: str
    metrics: list[Metric] = field(default_factory=list)
    error: str = ""

    def add(self, key: str, label: str, value: Any, display: str | None = None, note: str = "") -> None:
        self.metrics.append(Metric(key, label, value, display if display is not None else _show(value), note))


def _show(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.0%}" if 0 <= value <= 1 else f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def _day(value: datetime.date) -> str:
    """15 Oct 2026."""
    return f"{value.day} {value:%b %Y}"


def _years(rows: Any, today: datetime.date) -> dict[str, int]:
    """``(year, n)`` pairs as ``{"2024": n, ...}``: every year from the first present to this one (0 where
    none). JSON keys are text: the check is stored as JSON."""
    found: dict[int, int] = defaultdict(int)
    for year, n in rows:
        if year and int(year) >= 2000:  # an empty or mistyped year (0, 25) is no year
            found[int(year)] += n or 0
    if not found:
        return {}
    return {str(y): found.get(y, 0) for y in range(min(found), max(today.year, max(found)) + 1)}


def _years_shown(found: dict[str, int], today: datetime.date) -> str:
    """2025: 41 · 2026 so far: 9"""
    if not found:
        return "None yet"
    return " · ".join(f"{y}{' so far' if y == str(today.year) else ''}: {n:,}" for y, n in found.items())


def _median(values: list[int]) -> float | int | None:
    if not values:
        return None
    middle = statistics.median(values)
    return int(middle) if float(middle).is_integer() else round(middle, 1)


def _settled_regex() -> str:
    """The report statuses of a settled value: the words PRP writes for Accepted (``reached.status_of``)."""
    from neurodb.datamart import reached

    words = sorted(w for w, status in reached._STATUS_WORDS.items() if status == reached.ACCEPTED)
    return r"^\s*(" + "|".join(re.escape(w) for w in words) + r")\s*$"


# ---------------------------------------------------------------------------------- sources
def etools(s: Section, today: datetime.date) -> None:
    from neurodb.datamart.models import PDIndicator, ReportedIndicator
    from neurodb.partnerships.models import PCA

    running = PCA.objects.filter(status__in=("active", "signed", "ended", "closed", "suspended"))
    n = running.count()
    s.add("pds", "Programme documents (signed, active or ended)", n)
    if n:
        located = running.filter(pk__in=PCA.locations.through.objects.values("pca_id")).count()
        s.add("pd_locations", "PDs with locations", _share(located, n))
        s.add(
            "pd_outputs",
            "PDs with country programme outputs",
            _share(running.exclude(cp_outputs=None).exclude(cp_outputs=[]).count(), n),
        )
        s.add(
            "pd_sections",
            "PDs with a section",
            _share(running.exclude(section_names=None).exclude(section_names=[]).count(), n),
        )
    indicators_n = PDIndicator.objects.count()
    s.add("pd_indicators", "PD indicators", indicators_n)
    if indicators_n:
        s.add(
            "pd_targets",
            "PD indicators with a target",
            _share(PDIndicator.objects.filter(target_numerator__gt=0).count(), indicators_n),
        )
    report = ("intervention_id", "pd_reference_number", "report_number", "report_type", "period_end")
    s.add(
        "progress_reports",
        "Partner progress reports",
        ReportedIndicator.objects.values(*report).distinct().count(),
    )
    dated = list(
        ReportedIndicator.objects.exclude(due_date=None)
        .exclude(submission_date=None)
        .values(*report)
        .annotate(due=Max("due_date"), sent=Max("submission_date"))
    )
    s.add("reports_dated", "Progress reports with a due and a submission date", len(dated))
    if dated:
        on_time = sum(r["sent"] <= r["due"] for r in dated)
        s.add(
            "reports_on_time",
            "Progress reports submitted by their due date",
            _share(on_time, len(dated)),
            note="by the latest submission date: a report sent back and submitted again counts as late",
        )
    span = ReportedIndicator.objects.filter(period_end__lte=today).aggregate(
        first=Min("period_end"), last=Max("period_end")
    )
    if span["first"]:
        s.add("reporting_span", "Partner reporting covers", f"{span['first']:%b %Y} – {span['last']:%b %Y}")
    future = ReportedIndicator.objects.filter(period_end__gt=today).values(*report).distinct().count()
    if future:
        s.add(
            "reports_future",
            "Progress reports for periods not ended yet",
            future,
            note="eTools creates the reporting periods of a PD in advance",
        )


def assurance(s: Section, today: datetime.date) -> None:
    from neurodb.datamart.models import ActionPoint, AuditEngagement, PartnerAssessment, ProgrammaticVisit
    from neurodb.partnerships.models import PartnerOrganization

    partners = PartnerOrganization.objects.count()
    if partners:
        rated = PartnerOrganization.objects.exclude(rating=None).exclude(rating="").count()
        s.add("partners_rated", "Partners with a HACT risk rating", _share(rated, partners))
    s.add("assessments", "Partner assessments", PartnerAssessment.objects.count())
    s.add("audits", "Audits and spot checks", AuditEngagement.objects.count())
    points = ActionPoint.objects.count()
    s.add("action_points", "Action points", points)
    if points:
        statuses = dict(ActionPoint.objects.values_list("status").annotate(n=Count("id")))
        closed = sum(n for st, n in statuses.items() if st not in ActionPoint.OPEN_STATUSES)
        s.add(
            "action_points_closed",
            "Action points no longer open",
            _share(closed, points),
            note=", ".join(
                f"{st or 'no status'}: {n:,}"
                for st, n in sorted(statuses.items(), key=lambda x: (-x[1], x[0] or ""))
            ),
        )
    visits = ProgrammaticVisit.objects.aggregate(n=Count("id"), first=Min("date"), last=Max("date"))
    s.add("visits", "Programmatic visits", visits["n"])
    if visits["first"]:
        s.add("visits_span", "Visits cover", f"{visits['first']:%b %Y} – {visits['last']:%b %Y}")


def review(s: Section, today: datetime.date) -> None:
    from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding

    days = DailyReview.objects.filter(status="succeeded")
    s.add("review_days", "Days the daily review ran", days.count())
    first = days.aggregate(first=Min("date"))["first"]
    if first:
        s.add("review_since", "Daily review since", f"{first:%d %b %Y}")
    resolved = ReviewFinding.objects.filter(state="resolved").values("key").distinct().count()
    assigned = FindingAssignment.objects.count()
    s.add("findings_resolved", "Findings that were resolved", resolved)
    s.add("findings_assigned", "Findings people took on (assigned)", assigned)


def context(s: Section, today: datetime.date) -> None:
    from neurodb.core.models import PopulationFigure
    from neurodb.education.power_bi import EDUCATION, page_periods
    from neurodb.graph.models import Change
    from neurodb.wellbeing.models import CenterSummary
    from neurodb.youth.models import YouthFigures

    pop = PopulationFigure.objects.aggregate(latest=Max("year"), n=Count("id"))
    s.add("population_latest", "Latest population figures", str(pop["latest"]) if pop["latest"] else None)
    if pop["n"]:
        s.add(
            "population_levels",
            "Population figures by level",
            ", ".join(
                f"{r['level']}: {r['n']:,}"
                for r in PopulationFigure.objects.values("level").annotate(n=Count("id")).order_by("level")
            ),
        )
    s.add("youth_years", "Compiler youth years", YouthFigures.objects.yearly().count())
    # the periods the pages list (Dirasa's rounds stored before it was counted by year are kept, not listed)
    listed = page_periods(EDUCATION)
    s.add("education_periods", "Compiler education periods", sum(len(v) for v in listed.values()))
    s.add(
        "wellbeing_months",
        "Makani wellbeing months",
        CenterSummary.objects.values("month").distinct().count(),
    )
    first = Change.objects.aggregate(first=Min("detected_at"))["first"]
    s.add(
        "change_log_days",
        "Days of the change log (What's new)",
        (timezone.now() - first).days if first else 0,
    )


# how a field monitoring finding's programme document was found (``MonitoringFinding.pd_match``, the same on a
# Monitoring insights record: ``datamart.fm.PDResolver.resolve``), in plain words
LINK_METHODS = {
    "exact": "the same reference",
    "token": "the same PCA and PD numbers",
    "base": "the same number without its amendment",
    "title": "the same title, among the partner's PDs",
}
# the engagements about programme documents (audits, special audits, spot checks); a micro-assessment is a
# partner's
AUDIT_TYPES = ("audit", "sa", "sc")


def _linked(
    s: Section, key: str, label: str, rows: Any, link: Q, *, methods: bool = False, note: str = ""
) -> None:
    """The share of ``rows`` that ``link`` holds (none when there are no rows), "x of y" and, with
    ``methods``, how the programme document was found (``pd_match``): one query."""
    by_method = {how: Count("pk", distinct=True, filter=link & Q(pd_match=how)) for how in LINK_METHODS}
    found = rows.order_by().aggregate(
        n=Count("pk", distinct=True),
        linked=Count("pk", distinct=True, filter=link),
        **(by_method if methods else {}),
    )
    if not found["n"]:
        return
    parts = [f"{found['linked']:,} of {found['n']:,}"]
    if methods:
        parts += [f"{words}: {found[how]:,}" for how, words in LINK_METHODS.items() if found[how]]
        other = found["linked"] - sum(found[how] for how in LINK_METHODS)
        if other:
            parts.append(f"another way: {other:,}")
        parts.append(f"not linked: {found['n'] - found['linked']:,}")
    if note:
        parts.append(note)
    s.add(key, label, _share(found["linked"], found["n"]), note=" · ".join(parts))


def _running_pd_ids() -> list[int]:
    from neurodb.partnerships import statuses
    from neurodb.partnerships.models import PCA

    return list(PCA.objects.filter(status__in=statuses.RUNNING).values_list("pk", flat=True))


def links(s: Section, today: datetime.date) -> None:
    """How well the sources the later analyses would join link up: for each, the share of its rows that carry
    the link, and how a field monitoring finding's programme document was found."""
    from django.apps import apps

    from neurodb.datamart import identity, masters
    from neurodb.datamart.models import (
        ActionPoint,
        AuditEngagement,
        EtoolsMaster,
        EtoolsMasterLink,
        FundsReservation,
        MonitoringFinding,
        PDIndicator,
    )

    findings = MonitoringFinding.objects.all()
    has_pd = Q(intervention__isnull=False)
    _linked(
        s,
        "fm_findings_pd",
        "Field monitoring findings linked to a programme document",
        findings,
        has_pd,
        methods=True,
    )
    _linked(
        s,
        "fm_findings_partner",
        "Field monitoring findings linked to a partner",
        findings,
        Q(partner__isnull=False),
    )
    if apps.is_installed("neurodb.fmm"):
        from neurodb.fmm.models import Visit, VisitEntity

        _linked(
            s,
            "fm_records_pd",
            "Monitoring insights records about a programme document, linked to it",
            VisitEntity.objects.filter(kind="pd"),
            Q(pd__isnull=False),
            methods=True,
        )
        _linked(
            s,
            "fm_visits_pd",
            "Monitoring insights visits linked to a programme document",
            Visit.objects.all(),
            Q(pd__isnull=False),
        )
    points = ActionPoint.objects.all()
    _linked(s, "ap_pd", "Action points linked to a programme document", points, has_pd)
    _linked(s, "ap_partner", "Action points linked to a partner", points, Q(partner__isnull=False))
    _linked(
        s,
        "ap_engagement",
        "Action points raised from an audit or spot check, linked to it",
        points.filter(Q(engagement_source_id__isnull=False) | Q(related_module__iexact="audit")),
        Q(engagement__isnull=False),
    )
    _linked(
        s,
        "audits_pd",
        "Audits, special audits and spot checks linked to a programme document",
        AuditEngagement.objects.filter(engagement_type__in=AUDIT_TYPES),
        Q(interventions__isnull=False),
        note="micro-assessments left out: they assess a partner, not a programme document",
    )
    _linked(
        s,
        "funds_pd",
        "Funds reservation lines linked to a programme document",
        FundsReservation.objects.all(),
        has_pd,
    )
    # the running PD indicators (each once, whatever its places) in a master indicator this year: the master
    # indicators' own rule (masters.homes: a confirmed link to an active master not switched off this year)
    running = _running_pd_ids()
    indicators = {
        (pd_id, masters.ident_code(identity.ident_of(pd_id, source_id, title)))
        for pd_id, source_id, title in PDIndicator.objects.filter(intervention_id__in=running, listed=True)
        .order_by()
        .values_list("intervention_id", "source_id", "title")
        .distinct()
    }
    if indicators:
        linked = len(indicators & masters.homes(today.year, running))
        s.add(
            "indicators_master",
            "Running PD indicators in a master indicator (a confirmed link)",
            _share(linked, len(indicators)),
            note=f"{linked:,} of {len(indicators):,}",
        )
    groups = (
        EtoolsMasterLink.objects.filter(
            status=EtoolsMasterLink.Status.CONFIRMED,
            master__status=EtoolsMaster.Status.ACTIVE,
            pd_id__in=running,
        )
        .exclude(master_id__in=masters.switched_off(today.year))
        .order_by()
        .values("master_id")
        .annotate(n=Count("pd_id", distinct=True))
        .filter(n__gte=PDS_PER_GROUP)
        .count()
    )
    s.add(
        "master_groups",
        f"Master indicators bringing together {PDS_PER_GROUP} or more running PDs",
        groups,
        note="each a group of comparable programme documents",
    )


def history_model() -> Any:
    """The action point history (one status per action point and day: ``datamart.ActionPointStatus``), or None
    while NeuroDB does not keep it."""
    from django.apps import apps

    try:
        return apps.get_model("datamart", "ActionPointStatus")
    except LookupError:
        return None


def _settled(s: Section, today: datetime.date) -> None:
    """How many accepted (settled) values a typical running PD indicator has, and over how many quarters:
    the middle of the running PD indicators, those with none included. A report row belongs to the indicator
    whose eTools id it carries, else to the one indicator of its PD with its title (``identity``'s first two
    rules); a value is one progress report (the code's report key), whatever its places."""
    from django.db.models.functions import TruncQuarter

    from neurodb.datamart import identity, reporting
    from neurodb.datamart.models import PDIndicator, ReportedIndicator
    from neurodb.datamart.reached import norm

    running = _running_pd_ids()
    found: dict[tuple[int, str], list] = {}
    by_title: dict[int, dict[str, tuple[int, str] | None]] = defaultdict(dict)
    for pd_id, source_id, title in (
        PDIndicator.objects.filter(intervention_id__in=running, listed=True)
        .order_by()
        .values_list("intervention_id", "source_id", "title")
        .distinct()
    ):
        ident = identity.ident_of(pd_id, source_id, title)
        found.setdefault(ident, [0, set()])
        seen = by_title[pd_id].get(norm(title), ident)
        by_title[pd_id][norm(title)] = ident if seen == ident else None  # a title two indicators share: none
    if not found:
        return
    rows = (
        ReportedIndicator.objects.filter(
            intervention_id__in=running, period_end__lte=today, report_status__iregex=_settled_regex()
        )
        .order_by()
        .annotate(quarter=TruncQuarter("period_end"))
        .values_list("intervention_id", "etools_indicator_id", "indicator", "quarter")
        .annotate(n=Count(reporting.REPORT_KEY, distinct=True))
    )
    for pd_id, etools_id, title, quarter, n in rows:
        ident = (pd_id, f"s{etools_id}") if etools_id else None
        if ident not in found:
            ident = by_title[pd_id].get(norm(title))
        if ident in found:
            found[ident][0] += n
            found[ident][1].add(quarter)
    values = _median([v for v, _ in found.values()])
    quarters = _median([len(q) for _, q in found.values()])
    note = f"the middle of {len(found):,} running PD indicators, those with none included"
    s.add(
        "settled_values",
        "Accepted values of a typical running PD indicator",
        values,
        f"{values:g}",
        note=note + "; a value is one report UNICEF accepted",
    )
    s.add("settled_quarters", "Quarters those values cover", quarters, f"{quarters:g}", note=note)


def _events_before_after(s: Section, today: datetime.date) -> None:
    """Of last year's visits and audits on a programme document (field monitoring, TPM activities, audits,
    special audits and spot checks; one per PD and day), those with an accepted value of that PD in the
    ``BEFORE_AFTER_DAYS`` before and in the ``BEFORE_AFTER_DAYS`` after."""
    from django.db.models.functions import Coalesce

    from neurodb.datamart import fm
    from neurodb.datamart.models import AuditEngagement, MonitoringFinding, ReportedIndicator, TPMActivity

    year = today.year - 1
    events: set[tuple[int, datetime.date]] = set()
    events |= set(
        MonitoringFinding.objects.filter(intervention__isnull=False)
        .annotate(on=fm.finding_date())
        .filter(on__year=year)
        .order_by()
        .values_list("intervention_id", "on")
        .distinct()
    )
    events |= set(
        TPMActivity.objects.filter(intervention__isnull=False, date__year=year)
        .order_by()
        .values_list("intervention_id", "date")
        .distinct()
    )
    events |= set(
        AuditEngagement.interventions.through.objects.filter(auditengagement__engagement_type__in=AUDIT_TYPES)
        .annotate(
            on=Coalesce(
                "auditengagement__date_of_field_visit",
                "auditengagement__end_date",
                "auditengagement__start_date",
            )
        )
        .filter(on__year=year)
        .order_by()
        .values_list("pca_id", "on")
        .distinct()
    )
    ends: dict[int, list[datetime.date]] = defaultdict(list)
    for pd_id, end in (
        ReportedIndicator.objects.filter(
            intervention_id__in={pd for pd, _ in events},
            period_end__isnull=False,
            report_status__iregex=_settled_regex(),
        )
        .order_by()
        .values_list("intervention_id", "period_end")
        .distinct()
    ):
        ends[pd_id].append(end)
    window = datetime.timedelta(days=BEFORE_AFTER_DAYS)
    both = sum(
        1
        for pd_id, on in events
        if any(on - window <= end < on for end in ends[pd_id])
        and any(on < end <= on + window for end in ends[pd_id])
    )
    s.add(
        "events_before_after",
        f"Visits and audits in {year} with accepted values before and after",
        both,
        note=f"of {len(events):,} on a programme document (field monitoring, TPM, audits and spot checks; "
        f"one per PD and day); {BEFORE_AFTER_DAYS} days each side",
    )


def arrivals(s: Section, today: datetime.date) -> None:
    """What arrives each year, per calendar year from the first present to this one, each counted by the rule
    of the page that counts it; the accepted values of a typical running PD indicator; the first partner
    report data; the action point history kept; and last year's visits and audits with values before and
    after."""
    from django.apps import apps
    from django.db.models.functions import Coalesce, ExtractYear, Substr, Trim, Upper

    from neurodb.datamart import fm, reporting
    from neurodb.datamart import services as dms
    from neurodb.datamart.models import (
        ActionPoint,
        AuditEngagement,
        AuditFinding,
        MonitoringFinding,
        ReportedIndicator,
        TPMVisit,
    )
    from neurodb.datamart.monitoring import REPORT_TYPES, pending_q

    def years(key: str, label: str, rows: Any, note: str = "") -> None:
        found = _years(rows, today)
        s.add(key, label, found, _years_shown(found, today), note=note)

    # partner progress reports sent (submitted or accepted: monitoring.pending_q), each once by the code's own
    # report key (reporting.REPORT_KEY), by the year their period ended, per report type
    by_type: dict[str, list] = defaultdict(list)
    for kind, year, n in (
        ReportedIndicator.objects.exclude(pending_q())
        .exclude(period_end=None)
        .order_by()
        .annotate(kind=Upper(Trim("report_type")), year=ExtractYear("period_end"))
        .values_list("kind", "year")
        .annotate(n=Count(reporting.REPORT_KEY, distinct=True))
    ):
        by_type[kind].append((year, n))
    order = list(REPORT_TYPES)  # quarterly, then humanitarian, then any other type
    for kind in sorted(by_type, key=lambda k: (order.index(k) if k in order else len(order), k)):
        slug = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_") or "no_type"
        what = REPORT_TYPES.get(kind) or (f"{kind} reports" if kind else "Reports with no type")
        years(
            f"reports_{slug}",
            f"Partner reports sent: {what}",
            by_type[kind],
            "by the year their period ended; each report once, whatever its indicators and places",
        )
    first = ReportedIndicator.objects.aggregate(first=Min(Coalesce("period_start", "period_end")))["first"]
    s.add(
        "reports_first",
        "Partner report data from",
        first.isoformat() if first else None,
        _day(first) if first else "None yet",
        note="the first reporting period NeuroDB holds",
    )

    # audits, special audits, spot checks and micro-assessments: the Assurance page's year (the year audited,
    # else the start date's)
    by_kind: dict[str, list] = defaultdict(list)
    for kind, year, n in (
        AuditEngagement.objects.order_by()
        .annotate(year=Coalesce("year_of_audit", ExtractYear("start_date")))
        .values_list("engagement_type", "year")
        .annotate(n=Count("pk"))
    ):
        by_kind[kind].append((year, n))
    for kind, key, label in (
        ("audit", "audits", "Audits"),
        ("sa", "special_audits", "Special audits"),
        ("sc", "spot_checks", "Spot checks"),
        ("ma", "micro_assessments", "Micro-assessments"),
    ):
        years(
            key, label, by_kind[kind], "by the year audited, else the start date's (as on the Assurance page)"
        )
    years(
        "audit_findings",
        "Audit findings",
        AuditFinding.objects.order_by()
        .annotate(year=ExtractYear("created"))
        .values_list("year")
        .annotate(n=Count("pk")),
        "financial findings of audits and spot checks, by the date they were made",
    )

    # field monitoring: visits (datamart.fm.visits_by_year, as every page counts them by year) and findings
    # (the field monitoring page's year: the start date, else the end date); TPM visits done (reported or
    # approved)
    years(
        "fm_visits",
        "Field monitoring visits",
        fm.visits_by_year(MonitoringFinding.objects.all()).items(),
        "by their date: the start date, else the end date",
    )
    years(
        "fm_findings",
        "Field monitoring findings",
        MonitoringFinding.objects.order_by()
        .annotate(year=ExtractYear(fm.finding_date()))
        .values_list("year")
        .annotate(n=Count("pk")),
        "one per entity monitored in a visit (a programme document, output or partner)",
    )
    years(
        "tpm_visits",
        "TPM visits done",
        TPMVisit.objects.exclude(status__in=dms.TPM_NOT_PLANNED)
        .filter(status__in=dms.TPM_COMPLETED)
        .order_by()
        .annotate(year=ExtractYear("start_date"))
        .values_list("year")
        .annotate(n=Count("pk")),
        "reported or approved, by their start date",
    )

    # action points raised (the month eTools' record says) and completed, as the action points page counts
    # them
    years(
        "ap_raised",
        "Action points raised",
        ActionPoint.objects.order_by()
        .annotate(raised=dms._raised_month())
        .filter(raised__regex=r"^\d{4}-(0[1-9]|1[0-2])$")
        .annotate(year=Substr("raised", 1, 4))
        .values_list("year")
        .annotate(n=Count("pk")),
        "by the date eTools says they were raised",
    )
    years(
        "ap_completed",
        "Action points completed",
        ActionPoint.objects.filter(dms.ap_completed_q())
        .exclude(date_of_completion=None)
        .order_by()
        .annotate(year=ExtractYear("date_of_completion", tzinfo=datetime.UTC))
        .values_list("year")
        .annotate(n=Count("pk")),
        "completed, closed or resolved, by their completion date",
    )

    # PME's verifications of action points (Monitoring insights), every decision kept
    if apps.is_installed("neurodb.fmm"):
        from neurodb.fmm.models import ActionPointVerification

        decisions = ActionPointVerification.objects.order_by()
        years(
            "pme_verifications",
            "PME verifications of action points",
            decisions.annotate(year=ExtractYear("created_at")).values_list("year").annotate(n=Count("pk")),
            "every decision recorded: verified, rejected or pending",
        )
        first_decision = decisions.aggregate(first=Min("created_at"))["first"]
        first_day = timezone.localtime(first_decision).date() if first_decision else None
        s.add(
            "pme_first",
            "First PME verification",
            first_day.isoformat() if first_day else None,
            _day(first_day) if first_day else "None yet",
        )
        decided = decisions.filter(state__in=("verified", "rejected"))
        s.add(
            "pme_decided",
            "Action points PME verified or rejected",
            decided.values("datamart_id").distinct().count(),
        )
        recent = decided.filter(created_at__gte=timezone.now() - datetime.timedelta(days=365))
        s.add(
            "pme_recent",
            "Of them, decided in the last 12 months",
            recent.values("datamart_id").distinct().count(),
        )

    # the action point history (one status per action point and day), once NeuroDB keeps it
    model = history_model()
    kept = model.objects.aggregate(first=Min("day"), days=Count("day", distinct=True)) if model else {}
    days, since_day = kept.get("days") or 0, kept.get("first")
    s.add(
        "ap_history_days",
        "Action point history kept",
        days,
        f"{days:,} {'day' if days == 1 else 'days'}, since {_day(since_day)}" if days else "Not kept yet",
        note="each action point's status, once a day: no text, no names",
    )
    s.add(
        "ap_history_first",
        "Action point history kept since",
        since_day.isoformat() if days else None,
        _day(since_day) if days else "Not kept yet",
    )

    _settled(s, today)
    _events_before_after(s, today)


SOURCES: list[tuple[str, str, Callable[[Section, datetime.date], None]]] = [
    ("etools", "eTools programme documents and partner reporting", etools),
    ("assurance", "Assurance and field monitoring", assurance),
    ("review", "Daily review and what people did with it", review),
    ("context", "Population, Compiler and change history", context),
    ("links", "How the sources link up", links),
    ("arrivals", "What arrives each year", arrivals),
]


# -------------------------------------------------------------------------------- verdicts
@dataclass
class Check:
    ok: bool | None  # None: not measured
    text: str


@dataclass
class Verdict:
    key: str
    question: str
    method: str
    status: str
    checks: list[Check]
    group: str = (
        "decision"  # "decision": a decision a model could support now; "later": an analysis for later
    )
    reason: str = ""  # why it is not ready, in plain words
    when: str = ""  # about when it could be ready ("about July 2027"), only where the data's pace tells


STATUS_LABELS = {"ready": "Ready", "partly": "Partly ready", "not_yet": "Not yet"}


def _count(value: float) -> str:
    """1,234 or 2.5: a count or a median, never a share."""
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.1f}"


def _check(value: Any, minimum: float, label: str) -> Check:
    if value is None:
        return Check(None, f"{label}: not measured (no data)")
    share = isinstance(minimum, float) and minimum <= 1
    want = _show(minimum) if share else f"{minimum:,}"
    return Check(value >= minimum, f"{label}: {_show(value) if share else _count(value)} (needs {want})")


def _status(checks: list[Check], must: int = 1) -> str:
    """Ready when every check passes; partly when the first ``must`` pass and at least half overall."""
    passed = [c.ok for c in checks]
    if all(passed):
        return "ready"
    if all(passed[:must]) and sum(1 for p in passed if p) >= len(passed) / 2:
        return "partly"
    return "not_yet"


def verdicts(m: dict[str, Any], today: datetime.date | None = None) -> list[Verdict]:
    """The verdicts of the measures ``m`` (``"source.measure"``: value): the decisions a model could support,
    then the analyses for later (:func:`later`), as of ``today`` (the day of the check)."""

    def get(key: str) -> Any:
        return m.get(key)

    population = get("context.population_latest")
    try:
        population_age = datetime.date.today().year - int(str(population)[:4]) if population else None
    except ValueError:
        population_age = None

    coverage = [
        _check(get("etools.pd_locations"), PD_LOCATIONS, "PDs with locations"),
        Check(
            None if population_age is None else population_age <= POPULATION_AGE_YEARS,
            f"Population figures of {population or '—'} (needs {POPULATION_AGE_YEARS} years old at most)",
        ),
    ]
    outcomes = (get("review.findings_resolved") or 0) + (get("review.findings_assigned") or 0)
    review_list = [
        _check(get("review.review_days"), REVIEW_DAYS, "Days of daily review"),
        Check(
            bool((get("assurance.assessments") or 0) + (get("assurance.audits") or 0)),
            "Assessments, audits or spot checks recorded",
        ),
        _check(
            get("etools.reports_dated"), REPORTS_WITH_DATES, "Progress reports with due and submission dates"
        ),
        _check(outcomes, OUTCOMES, "Findings people resolved or took on (examples to learn from)"),
    ]
    return [
        Verdict(
            "coverage",
            "Where do several partners work on related activities, and where are the gaps?",
            "Counts by place against need; grouping related activities",
            _status(coverage, must=1),
            coverage,
        ),
        Verdict(
            "review_list",
            "Where might teams want to review implementation or data quality?",
            "A ranked list with its reasons: rules first, learning from outcomes later",
            _status(review_list, must=2),
            review_list,
        ),
        *later(m, today or timezone.localdate()),
    ]


# ------------------------------------------------------------------------------ analyses for later
def _about(day: datetime.date) -> str:
    return f"about {day:%B %Y}"


def _months_on(today: datetime.date, months: int) -> datetime.date:
    month = today.month - 1 + months
    year, month = today.year + month // 12, month % 12 + 1
    return datetime.date(year, month, min(today.day, 28))


def _in_year(m: dict[str, Any], key: str, year: int) -> int | None:
    """A yearly measure's count for ``year`` (0 for a year it holds none of), or None when not measured."""
    found = m.get(key)
    return None if found is None else int((found or {}).get(str(year), 0))


def _year_pace(m: dict[str, Any], key: str, minimum: int, today: datetime.date) -> datetime.date | None:
    """When a yearly count short of ``minimum`` last year would reach it: next January when this year, at its
    pace so far, gets there (said only once a quarter of the year has passed); else None."""
    count = _in_year(m, key, today.year)
    passed = (today - datetime.date(today.year, 1, 1)).days / 365
    if count is None or passed < PACE_YEAR_SHARE or count / passed < minimum:
        return None
    return datetime.date(today.year + 1, 1, 1)


def _values_pace(m: dict[str, Any], minimum: int, today: datetime.date) -> datetime.date | None:
    """When a typical indicator would have ``minimum`` accepted values, at the pace it has had them (its
    values over the quarters they cover); None before it has any."""
    values, quarters = m.get("arrivals.settled_values"), m.get("arrivals.settled_quarters")
    if not values or not quarters:
        return None
    return _months_on(today, 3 * math.ceil((minimum - values) / (values / quarters)))


def _quarters_pace(m: dict[str, Any], minimum: int, today: datetime.date) -> datetime.date | None:
    """When the values would cover ``minimum`` quarters, one more each quarter; None before they cover any."""
    quarters = m.get("arrivals.settled_quarters")
    return _months_on(today, 3 * math.ceil(minimum - quarters)) if quarters else None


def _later(
    key: str, question: str, method: str, items: list[tuple[Check, datetime.date | None, str]], must: int
) -> Verdict:
    """An analysis for later from its checks, each with the day it could pass (None: no telling) and why it
    matters when it does not: the reason is the first failing check's; about when is said only when every
    failing check has a day (the latest of them)."""
    checks = [check for check, _, _ in items]
    status = _status(checks, must=must)
    failing = [(day, why) for check, day, why in items if not check.ok]
    reason = failing[0][1] if failing else ""
    days = [day for day, _ in failing]
    when = _about(max(days)) if days and all(days) else ""
    return Verdict(key, question, method, status, checks, group="later", reason=reason, when=when)


def later(m: dict[str, Any], today: datetime.date) -> list[Verdict]:
    """A verdict for each analysis the programme may want later, from the measures ``m`` as of ``today``: each
    with its checks (thresholds above), a plain reason when it is not ready and about when it could be, where
    the data's pace allows it."""
    last = today.year - 1
    values, quarters = m.get("arrivals.settled_values"), m.get("arrivals.settled_quarters")

    def values_check(minimum: int, why: str) -> tuple[Check, datetime.date | None, str]:
        check = _check(values, minimum, "Accepted values of a typical running PD indicator")
        return check, _values_pace(m, minimum, today), why

    unusual = [
        (
            _check(
                m.get("links.master_groups"),
                COMPARABLE_GROUPS,
                f"Master indicators with {PDS_PER_GROUP} or more running PDs",
            ),
            None,
            f"Few master indicators bring together {PDS_PER_GROUP} or more running PDs, so there are few "
            "groups of similar PDs to compare. Confirming more PD indicators in master indicators would "
            "help.",
        ),
        (
            _check(
                m.get("links.indicators_master"), MASTER_LINKED, "Running PD indicators in a master indicator"
            ),
            None,
            "Most running PD indicators are in no master indicator, so they cannot be compared with similar "
            "ones.",
        ),
        values_check(
            VALUES_PER_INDICATOR,
            "A typical running PD indicator has too few accepted values to tell an unusual result from an "
            "ordinary one.",
        ),
    ]
    themes = [
        (
            _check(
                _in_year(m, "arrivals.fm_findings", last),
                THEME_FM_FINDINGS,
                f"Field monitoring findings in {last}",
            ),
            _year_pace(m, "arrivals.fm_findings", THEME_FM_FINDINGS, today),
            "Too few field monitoring findings arrive in a year to group them into themes.",
        ),
        (
            _check(
                _in_year(m, "arrivals.audit_findings", last),
                THEME_AUDIT_FINDINGS,
                f"Audit findings in {last}",
            ),
            _year_pace(m, "arrivals.audit_findings", THEME_AUDIT_FINDINGS, today),
            "Too few audit findings arrive in a year to group them into themes.",
        ),
    ]
    change = [
        values_check(
            CHANGE_VALUES, "A typical running PD indicator has too few accepted values to show a trend."
        ),
        (
            _check(quarters, CHANGE_QUARTERS, "Quarters those values cover"),
            _quarters_pace(m, CHANGE_QUARTERS, today),
            "The accepted values cover too few quarters to show a trend.",
        ),
    ]
    decided, recent = m.get("arrivals.pme_decided"), m.get("arrivals.pme_recent")
    history = m.get("arrivals.ap_history_days")
    follow_up = [
        (
            _check(decided, FOLLOW_UP_VERIFIED, "Action points PME verified or rejected"),
            today + datetime.timedelta(days=math.ceil((FOLLOW_UP_VERIFIED - decided) / recent * 365))
            if decided is not None and recent
            else None,
            "Too few action points have been verified or rejected by PME: they are the examples to learn "
            "from.",
        ),
        (
            Check(False, f"Action point history: not kept yet (needs {FOLLOW_UP_HISTORY_DAYS:,} days)")
            if history == 0
            else _check(history, FOLLOW_UP_HISTORY_DAYS, "Days of action point history kept"),
            today + datetime.timedelta(days=FOLLOW_UP_HISTORY_DAYS - history) if history else None,
            "NeuroDB does not keep the action point history yet."
            if history == 0
            else "The action point history is too short to see how action points move over time.",
        ),
        (
            _check(
                _in_year(m, "arrivals.ap_completed", last),
                FOLLOW_UP_COMPLETED,
                f"Action points completed in {last}",
            ),
            _year_pace(m, "arrivals.ap_completed", FOLLOW_UP_COMPLETED, today),
            "Too few action points are completed in a year.",
        ),
    ]
    before_after = [
        (
            _check(
                m.get("arrivals.events_before_after"),
                BEFORE_AFTER_EVENTS,
                f"Visits and audits in {last} with accepted values before and after",
            ),
            None,
            "Too few visits and audits have accepted values of their programme document both before and "
            "after them.",
        ),
        values_check(
            BEFORE_AFTER_VALUES,
            "A typical running PD indicator has too few accepted values to compare before and after.",
        ),
    ]
    money = [(Check(False, "Spending per programme document: not in NeuroDB"), None, NO_SPENDING)]
    return [
        _later(
            "unusual",
            "Which programme documents show unusual results compared with similar ones?",
            "Comparing PDs whose indicators share a master indicator: an unusual result is something to look "
            "at, not a judgement",
            unusual,
            must=1,
        ),
        _later(
            "themes",
            "Which problems keep coming back in field monitoring and audits?",
            "Grouping recurring findings into themes; people check every theme before it is used",
            themes,
            must=0,  # either source alone is a start
        ),
        _later(
            "change",
            "How are indicator results changing over time?",
            "Trends in accepted values, quarter by quarter, for each indicator",
            change,
            must=1,
        ),
        _later(
            "follow_up",
            "Which follow-ups to findings get done and stay done?",
            "Learning from the action points PME verified, and from how each one moved over time",
            follow_up,
            must=1,
        ),
        _later(
            "before_after",
            "Do results change after a visit or an audit?",
            "Accepted values before and after each visit or audit, compared with similar PDs without one: "
            "a link, not proof of cause",
            before_after,
            must=1,
        ),
        _later(
            "value_for_money",
            "What does a result cost, compared with similar programme documents?",
            "Spending per PD against comparable results",
            money,
            must=1,
        ),
    ]


NO_SPENDING = (
    "NeuroDB holds no spending per programme document. Funds reserved and disbursed are not spending: they "
    "show money set aside and paid to the partner, not what it was spent on."
)


def measure(today: datetime.date | None = None) -> dict[str, Any]:
    today = today or timezone.localdate()
    sections, flat = [], {}
    for key, label, fn in SOURCES:
        section = Section(key, label)
        try:
            fn(section, today)
        except Exception as exc:  # one source that cannot be measured does not stop the others
            logger.exception("readiness: %s could not be measured", key)
            section.error = f"{type(exc).__name__}: {exc}"[:300]
        sections.append(section)
        flat.update({f"{key}.{m.key}": m.value for m in section.metrics})
    found = verdicts(flat, today)
    return {
        "measured_at": timezone.now().isoformat(),
        "sections": [asdict(s) for s in sections],
        "verdicts": [asdict(v) | {"status_label": STATUS_LABELS[v.status]} for v in found],
    }


# What a check stored before ActivityInfo became an archive held of it: its sources, and the measures of the
# partner links made from its records. Left out when the check is shown (:func:`shown`).
ARCHIVE_SECTIONS = frozenset({"activityinfo", "indicators"})
ARCHIVE_METRICS = frozenset({"etools.partner_labels_linked", "etools.partner_records_linked"})


def _local_day(stamp: Any) -> datetime.date | None:
    """The day of a stored ``measured_at`` (ISO text), in the site's time zone."""
    try:
        moment = datetime.datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()


def shown(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored check (``SyncRun.details["readiness"]``) as Data health shows it: without the ActivityInfo
    archive's sources and measures, its verdicts worked out again from the measures left (so none rests on
    them). An analysis for later shows only when the check had it: a check made before it was measured shows
    none rather than a verdict made from nothing."""
    if not result:
        return result
    had = {v.get("key") for v in result.get("verdicts") or []}
    sections, flat = [], {}
    for section in result.get("sections") or []:
        key = section.get("key")
        if key in ARCHIVE_SECTIONS:
            continue
        metrics = [
            {**m, "yearly": isinstance(m.get("value"), dict)}  # a value per year: its cell wraps
            for m in section.get("metrics") or []
            if f"{key}.{m.get('key')}" not in ARCHIVE_METRICS
        ]
        sections.append({**section, "metrics": metrics})
        flat.update({f"{key}.{m.get('key')}": m.get("value") for m in metrics})
    day = _local_day(result.get("measured_at")) or timezone.localdate()
    found = [v for v in verdicts(flat, day) if v.group == "decision" or v.key in had]
    return {
        **result,
        "sections": sections,
        "verdicts": [asdict(v) | {"status_label": STATUS_LABELS[v.status]} for v in found],
    }


# ------------------------------------------------------------------------------- since the last check
# the headline measures "Since the last check" compares (those both checks hold, whose value changed)
HEADLINE = (
    "etools.pds",
    "etools.pd_indicators",
    "etools.progress_reports",
    "etools.reports_on_time",
    "assurance.audits",
    "assurance.action_points",
    "assurance.action_points_closed",
    "review.review_days",
    "links.fm_findings_pd",
    "links.fm_records_pd",
    "links.ap_pd",
    "links.ap_engagement",
    "links.audits_pd",
    "links.funds_pd",
    "links.indicators_master",
    "links.master_groups",
    "arrivals.settled_values",
    "arrivals.settled_quarters",
    "arrivals.pme_decided",
    "arrivals.ap_history_days",
    "arrivals.events_before_after",
)


def since(previous: dict[str, Any] | None, latest: dict[str, Any] | None) -> dict[str, Any] | None:
    """What changed from the ``previous`` check to the ``latest`` (both as :func:`shown` gives them): the
    headline measures whose value changed (``label``, ``before``, ``after`` as shown) and the verdicts whose
    status changed (``question``, ``before``, ``after``), with the ``day`` of the previous check. None without
    a previous check. A measure or verdict only one of them holds is not compared."""
    if not previous or not latest:
        return None

    def measures(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            f"{s.get('key')}.{m.get('key')}": m
            for s in result.get("sections") or []
            for m in s.get("metrics") or []
        }

    before, after = measures(previous), measures(latest)
    changed = [
        {"label": after[k].get("label"), "before": before[k].get("display"), "after": after[k].get("display")}
        for k in HEADLINE
        if k in before and k in after and before[k].get("value") != after[k].get("value")
    ]
    old = {v.get("key"): v for v in previous.get("verdicts") or []}
    statuses = [
        {
            "question": v.get("question"),
            "before": STATUS_LABELS.get(old[v.get("key")].get("status"), ""),
            "after": STATUS_LABELS.get(v.get("status"), ""),
        }
        for v in latest.get("verdicts") or []
        if v.get("key") in old and old[v.get("key")].get("status") != v.get("status")
    ]
    return {"day": _local_day(previous.get("measured_at")), "metrics": changed, "verdicts": statuses}
