"""Is the data ready for machine learning? Before any model is chosen, this measures what each source
really holds (how much history, how complete, how places and indicators are coded, what outcomes
people recorded) and says, for each programme decision a model could support, whether the data is
ready, partly ready or not yet, and why.

Everything here is counted in the database; nothing leaves NeuroDB and nothing is predicted. The
thresholds are written below so the verdicts can be discussed and changed. ActivityInfo is not measured:
it is an archive of past monthly results, no longer a source. A check stored before (with its ActivityInfo
sources and the verdicts that rested on them) is shown without them (:func:`shown`).
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from django.db.models import Count, Max, Min
from django.utils import timezone

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------- thresholds
PD_LOCATIONS = 0.50
POPULATION_AGE_YEARS = 3
REPORTS_WITH_DATES = 30  # eTools progress reports with a due and a submission date
REVIEW_DAYS = 30
OUTCOMES = 50  # findings people resolved or took on: the examples a review model would learn from


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


SOURCES: list[tuple[str, str, Callable[[Section, datetime.date], None]]] = [
    ("etools", "eTools programme documents and partner reporting", etools),
    ("assurance", "Assurance and field monitoring", assurance),
    ("review", "Daily review and what people did with it", review),
    ("context", "Population, Compiler and change history", context),
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


STATUS_LABELS = {"ready": "Ready", "partly": "Partly ready", "not_yet": "Not yet"}


def _check(value: Any, minimum: float, label: str) -> Check:
    if value is None:
        return Check(None, f"{label}: not measured (no data)")
    want = _show(minimum) if isinstance(minimum, float) and minimum <= 1 else f"{minimum:,}"
    return Check(value >= minimum, f"{label}: {_show(value)} (needs {want})")


def _status(checks: list[Check], must: int = 1) -> str:
    """Ready when every check passes; partly when the first ``must`` pass and at least half overall."""
    passed = [c.ok for c in checks]
    if all(passed):
        return "ready"
    if all(passed[:must]) and sum(1 for p in passed if p) >= len(passed) / 2:
        return "partly"
    return "not_yet"


def verdicts(m: dict[str, Any]) -> list[Verdict]:
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
    ]


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
    found = verdicts(flat)
    return {
        "measured_at": timezone.now().isoformat(),
        "sections": [asdict(s) for s in sections],
        "verdicts": [asdict(v) | {"status_label": STATUS_LABELS[v.status]} for v in found],
    }


# What a check stored before ActivityInfo became an archive held of it: its sources, and the measures of the
# partner links made from its records. Left out when the check is shown (:func:`shown`).
ARCHIVE_SECTIONS = frozenset({"activityinfo", "indicators"})
ARCHIVE_METRICS = frozenset({"etools.partner_labels_linked", "etools.partner_records_linked"})


def shown(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored check (``SyncRun.details["readiness"]``) as Data health shows it: without the ActivityInfo
    archive's sources and measures, its verdicts worked out again from the measures left (so none rests on
    them)."""
    if not result:
        return result
    sections, flat = [], {}
    for section in result.get("sections") or []:
        key = section.get("key")
        if key in ARCHIVE_SECTIONS:
            continue
        metrics = [m for m in section.get("metrics") or [] if f"{key}.{m.get('key')}" not in ARCHIVE_METRICS]
        sections.append({**section, "metrics": metrics})
        flat.update({f"{key}.{m.get('key')}": m.get("value") for m in metrics})
    found = verdicts(flat)
    return {
        **result,
        "sections": sections,
        "verdicts": [asdict(v) | {"status_label": STATUS_LABELS[v.status]} for v in found],
    }
