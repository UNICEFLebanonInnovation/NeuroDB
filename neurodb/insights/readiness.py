"""Is the data ready for machine learning? Before any model is chosen, this measures what each source
really holds (how much history, how complete, how places and indicators are coded, what outcomes
people recorded) and says, for each programme decision a model could support, whether the data is
ready, partly ready or not yet, and why.

Everything here is counted in the database; nothing leaves NeuroDB and nothing is predicted. The
thresholds are written below so the verdicts can be discussed and changed.
"""

from __future__ import annotations

import datetime
import logging
import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from django.db.models import Count, Max, Min, Q
from django.db.models.functions import Substr
from django.utils import timezone

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------- thresholds
YEARS_FOR_FORECAST = 2  # complete years of monthly reports to learn each indicator's pattern
MONTHS_FOR_PATTERNS = 18
CONTINUITY = 0.60  # share of a year's master indicators found again the year before
TARGETS = 0.70  # share of master indicators with a target
COMPLETENESS = 0.70  # share of the months a partner reported in, between its first and last month
GOVERNORATE_CODED = 0.90
DISTRICT_CODED = 0.70
PLACES_MATCHED = 0.90  # governorates written as NeuroDB knows them
PARTNER_LINKED = 0.70  # ActivityInfo records whose partner is linked to an eTools partner
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


def _months_between(first: str, last: str) -> int | None:
    try:
        a, b = (datetime.date.fromisoformat(f"{m[:7]}-01") for m in (first, last))
    except (TypeError, ValueError):
        return None
    return (b.year - a.year) * 12 + b.month - a.month + 1


def _norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def _compact(text: str | None) -> str:
    """ "Baalbek-El Hermel", "Baalbek Hermel" and "Baalbek-Hermel" alike."""
    return "".join(w for w in _norm(text).split() if w not in ("el", "al", "governorate", "mohafaza"))


# ---------------------------------------------------------------------------------- sources
def activityinfo(s: Section, today: datetime.date) -> None:
    from neurodb.facts.models import ActivityReportNew as A
    from neurodb.geo.models import GovernorateLocation

    total = A.objects.count()
    s.add("reports", "Activity reports", total)
    if not total:
        return
    # the month of a report is in month_name ("2025-03-01"), as everywhere else in NeuroDB
    dated = A.objects.filter(month_name__regex=r"^[0-9]{4}-[0-9]{2}").annotate(ym=Substr("month_name", 1, 7))
    s.add("dated", "Reports with a month", _share(dated.count(), total))
    by_year = list(
        dated.annotate(y=Substr("month_name", 1, 4))
        .values("y")
        .annotate(n=Count("id"), months=Count("ym", distinct=True))
        .order_by("y")
    )
    full_years = [r for r in by_year if r["months"] >= 10]
    s.add(
        "years",
        "Years with reports",
        len(by_year),
        note=", ".join(f"{r['y']}: {r['months']} months" for r in by_year),
    )
    s.add("full_years", "Years with at least 10 months reported", len(full_years))
    s.add("months", "Months with reports, all years", dated.values("ym").distinct().count())

    pairs = list(
        dated.values("dbase_id", "partner_label").annotate(
            months=Count("ym", distinct=True), first=Min("ym"), last=Max("ym")
        )
    )
    reported = sum(p["months"] for p in pairs)
    expected = sum(_months_between(p["first"], p["last"]) or p["months"] for p in pairs)
    s.add(
        "completeness",
        "Months reported, between a partner's first and last month",
        _share(reported, expected),
        note="per partner and database; a missing month is not a zero",
    )
    s.add("zeros", "Values that are 0", _share(A.objects.filter(indicator_value=0).count(), total))
    s.add("blank", "Values that are empty", _share(A.objects.filter(indicator_value=None).count(), total))

    gov = A.objects.exclude(
        Q(location_adminlevel_governorate=None) | Q(location_adminlevel_governorate="")
    ).count()
    dist = A.objects.exclude(
        Q(location_adminlevel_caza_code=None) | Q(location_adminlevel_caza_code="")
    ).count()
    cad = A.objects.exclude(
        Q(location_adminlevel_cadastral_area_code=None) | Q(location_adminlevel_cadastral_area_code="")
    ).count()
    s.add("governorate_coded", "Reports with a governorate", _share(gov, total))
    s.add("district_coded", "Reports with a district (caza) code", _share(dist, total))
    s.add("cadastral_coded", "Reports with a cadastral area code", _share(cad, total))
    known = set()
    for name, code in GovernorateLocation.objects.values_list("name", "code"):
        known |= {_norm(name), _compact(name), _norm(code)}
    matched, unmatched = 0, []
    rows = (
        A.objects.exclude(location_adminlevel_governorate=None)
        .exclude(location_adminlevel_governorate="")
        .values("location_adminlevel_governorate", "location_adminlevel_governorate_code")
        .annotate(n=Count("id"))
    )
    for row in rows:
        name, code = row["location_adminlevel_governorate"], row["location_adminlevel_governorate_code"]
        if {_norm(name), _compact(name), _norm(code)} & known:
            matched += row["n"]
        else:
            unmatched.append((row["n"], name, code))
    unmatched.sort(reverse=True)
    s.add(
        "governorate_matched",
        "Governorates matching NeuroDB's (name or code)",
        _share(matched, gov),
        note="not matched: " + "; ".join(f"{n} ({c or 'no code'}): {k:,}" for k, n, c in unmatched[:6])
        if unmatched
        else "",
    )

    # how long after the month a report was last edited in ActivityInfo (late reports and corrections)
    start = f"{today.year - 1}-01"
    delays = []
    for row in (
        dated.filter(month_name__gte=start)
        .exclude(last_edited_time=None)
        .values("report_id")
        .annotate(month=Max("ym"), edited=Max("last_edited_time"))[:200_000]
    ):
        try:
            first_day = datetime.date.fromisoformat(f"{(row['month'] or '')[:7]}-01")
        except ValueError:
            continue
        month_end = (first_day + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        delays.append((row["edited"].date() - month_end).days)
    if delays:
        s.add(
            "edit_delay",
            "Median days from the end of the month to the last edit",
            int(statistics.median(delays)),
            note="since last year; includes later corrections",
        )
        s.add(
            "edited_within_30",
            "Reports last edited within 30 days of the month",
            _share(sum(d <= 30 for d in delays), len(delays)),
        )


def indicators(s: Section, today: datetime.date) -> None:
    from neurodb.indicators.models import MasterIndicator

    masters = list(
        MasterIndicator.objects.filter(is_active=True)
        .exclude(database__reporting_year__year=None)
        .values(
            "pk", "name", "awp_code", "awp_target", "database__reporting_year__year", "database__section_id"
        )
    )
    s.add("masters", "Active master indicators, all years", len(masters))
    if not masters:
        return
    years = sorted({str(m["database__reporting_year__year"]) for m in masters})
    by_year: dict[str, list[dict]] = defaultdict(list)
    for m in masters:
        by_year[str(m["database__reporting_year__year"])].append(m)
    latest = years[-1]
    with_target = sum(1 for m in by_year[latest] if (m["awp_target"] or 0) > 0)
    s.add(
        "targets", f"Master indicators of {latest} with a target", _share(with_target, len(by_year[latest]))
    )

    def keys(m: dict) -> set[tuple]:
        out = {("name", m["database__section_id"], _norm(m["name"]))}
        if (m["awp_code"] or "").strip():
            out.add(("code", m["database__section_id"], _norm(m["awp_code"])))
        return out

    continuity = []
    for previous, year in zip(years, years[1:], strict=False):
        known = set().union(*(keys(m) for m in by_year[previous]))
        found = sum(1 for m in by_year[year] if keys(m) & known)
        continuity.append((year, _share(found, len(by_year[year]))))
    if continuity:
        year, share = continuity[-1]
        s.add(
            "continuity",
            f"Master indicators of {year} found again the year before",
            share,
            note="same section and the same AWP code or name; "
            + ", ".join(f"{y}: {_show(v)}" for y, v in continuity),
        )


def etools(s: Section, today: datetime.date) -> None:
    from neurodb.datamart.models import PDIndicator, ReportedIndicator
    from neurodb.partnerships.models import PCA, PartnerLink

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
        s.add("reports_on_time", "Progress reports submitted by their due date", _share(on_time, len(dated)))
    span = ReportedIndicator.objects.filter(period_end__lte=today).aggregate(
        first=Min("period_end"), last=Max("period_end")
    )
    if span["first"]:
        s.add("reporting_span", "Partner reporting covers", f"{span['first']:%b %Y} – {span['last']:%b %Y}")
    future = ReportedIndicator.objects.filter(period_end__gt=today).values(*report).distinct().count()
    if future:
        s.add(
            "reports_future",
            "Progress reports with a period ending after today",
            future,
            note="to check in eTools",
        )
    links = PartnerLink.objects.aggregate(
        all=Count("id"), linked=Count("id", filter=Q(partner__isnull=False))
    )
    records = PartnerLink.objects.values_list("records", "partner_id")
    total_records = sum(r or 0 for r, _ in records)
    linked_records = sum(r or 0 for r, p in records if p)
    s.add(
        "partner_labels_linked",
        "ActivityInfo partner names linked to an eTools partner",
        _share(links["linked"], links["all"]),
    )
    s.add(
        "partner_records_linked",
        "ActivityInfo records whose partner is linked",
        _share(linked_records, total_records),
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
    from neurodb.education.models import EducationFigures
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
    s.add("youth_years", "Compiler youth years", YouthFigures.objects.count())
    s.add("education_periods", "Compiler education periods", EducationFigures.objects.count())
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
    ("activityinfo", "ActivityInfo monthly reports", activityinfo),
    ("indicators", "Master indicators across years", indicators),
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

    falling = [
        _check(get("activityinfo.full_years"), YEARS_FOR_FORECAST, "Years with monthly reports"),
        _check(get("indicators.continuity"), CONTINUITY, "Indicators found again the year before"),
        _check(get("indicators.targets"), TARGETS, "Indicators with a target"),
        _check(get("activityinfo.completeness"), COMPLETENESS, "Months reported by partners"),
    ]
    patterns = [
        _check(get("activityinfo.months"), MONTHS_FOR_PATTERNS, "Months of reports"),
        _check(get("activityinfo.governorate_coded"), GOVERNORATE_CODED, "Reports with a governorate"),
        _check(get("activityinfo.district_coded"), DISTRICT_CODED, "Reports with a district"),
        _check(get("activityinfo.completeness"), COMPLETENESS, "Months reported by partners"),
    ]
    coverage = [
        _check(get("activityinfo.governorate_matched"), PLACES_MATCHED, "Governorates matching NeuroDB's"),
        _check(get("activityinfo.district_coded"), DISTRICT_CODED, "Reports with a district"),
        _check(
            get("etools.partner_records_linked"),
            PARTNER_LINKED,
            "ActivityInfo records linked to eTools partners",
        ),
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
            "falling_behind",
            "Which indicators or activities may be falling behind their targets?",
            "Year-end forecast per indicator from its own monthly pattern",
            _status(falling, must=1),
            falling,
        ),
        Verdict(
            "patterns",
            "What patterns appear across locations, partners and reporting periods?",
            "Unusual reports and similar profiles, explained",
            _status(patterns, must=1),
            patterns,
        ),
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
