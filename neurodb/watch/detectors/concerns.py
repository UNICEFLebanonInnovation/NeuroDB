"""What NeuroDB already knows is wrong, brought into the watch's memory: daily review findings and
year-end forecasts.

The daily review looks back and the forecast looks at December; the watch follows both under their
existing keys, so people hear of a concern once, with its history, next to what falls due:

- ``daily_review``: the critical and warning findings still open in the latest succeeded daily review
  (up to the day of the pass), each as ``review:<finding key>``. Left out: the review's own data checks
  (``SYSTEM_CHECKS``; the system items cover them) and its good news (``improvements``). The title,
  detail and evidence are the review's, written by code, with the day counts that age by themselves
  ("ends in 30 days", "22 days ago") written as dates. From ``FindingAssignment`` it takes only whether
  someone owns the finding now and the assignment's status, never the owner or the note. Its source
  mark is the review's date: a finding the review no longer lists counts as missed only at a newer
  review, so re-running a day's review closes nothing, and the item is gone after two newer reviews
  without it. A finding the review now lists as "to note" only closes at once. On from the start.
- ``forecast_short``: the ActivityInfo master indicators the year-end forecast says are likely to fall
  short of their target, each as ``forecast:<year>:<master id>``, only while the forecasts are shown
  (:func:`neurodb.insights.services.shown`: their test on past years passed). Always worded as an
  estimate with its range; told to the forecast's section. A forecast back on course closes the item
  at once as "recovered", and any other status closes it too, naming the status, so its story keeps
  the forecast's history (likely short, recovered, back again). The forecast runs weekly, so its source
  is fresh for 8 days. Starts in trial, like every new check.

Looking ahead belongs to the watch and looking back to the review, and one thing is never two items.
A ``pd_ending_soon`` finding is the PD ending that the ``pd_ending`` check follows
(:func:`~neurodb.watch.detectors.deadlines.twin_of`). While that check is on (told to staff) and
follows the PD, the finding is noted on its item (``review_key``) instead of making a second item, and
an item imported earlier closes ("followed from now on as ..."). While it is in trial, only the
whole-country view sees its items, so the finding is imported as usual and its section still hears
of it. A report that becomes overdue closes its ``report_due_soon`` item, which notes
``reports_overdue:<PD number>``; the review's finding about it is then imported here.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse

from neurodb.core.models import SyncRun
from neurodb.graph.models import Entity
from neurodb.insights import services as forecasts
from neurodb.insights.models import IndicatorForecast
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.review.services import SYSTEM_CHECKS

from ..models import RECORDS_MAX, DetectorSetting, WatchItem, fit_key
from . import (
    ALIVE,
    CONCERN,
    COUNTRY,
    INFO,
    LIKELY,
    ON,
    SECTION,
    Attach,
    Candidate,
    Close,
    Context,
    Detector,
    deadlines,
    evidence,
    record,
    register,
)

logger = logging.getLogger(__name__)

DAILY_REVIEW, FORECAST = SyncRun.Job.DAILY_REVIEW, SyncRun.Job.FORECAST

# ---------------------------------------------------------------------------- daily review findings
REVIEW_ID = "daily_review"
REVIEW_PREFIX = "review:"  # review:<finding key>
REVIEW_SOURCE = "the daily review"  # "No longer in the daily review"
FINDING = Entity.Kind.FINDING  # the knowledge hub kind of a finding; its key is the finding's key
HUB_KEY_MAX = 120  # the hub cuts its keys to this length
IMPORTED = frozenset({ReviewFinding.Severity.CRITICAL, ReviewFinding.Severity.WARNING})
LEFT_OUT = frozenset(SYSTEM_CHECKS | {"improvements"})  # the review's data checks and good news
TWIN_CHECK = deadlines.PD_ENDING  # the look-ahead check a finding may hand over to (deadlines.twin_of)
LOWERED = "the daily review now lists it as to note only"

# ---------------------------------------------------------------------------- year-end forecasts
FORECAST_ID = "forecast_short"
FORECAST_PREFIX = "forecast:"  # forecast:<year>:<master id>
FORECAST_SOURCE = "the year-end forecasts"
FORECAST_FRESH_HOURS = 8 * 24  # the forecast runs on Mondays: stale the day after one missed run
MASTER = Entity.Kind.MASTER_INDICATOR
SHORT, ON_COURSE = IndicatorForecast.Status.SHORT, IndicatorForecast.Status.ON_COURSE
RECOVERED = "recovered: the year-end forecast is on course again"
NOT_SHOWN = "forecasts are no longer shown: the latest test on past years did not find them reliable enough"
NAME_CHARS = 150  # an indicator's name in a title
# The only columns read (never the database's sign-in fields)
FORECAST_FIELDS = (
    "master",
    "database",
    "section_id",
    "year",
    "as_of_month",
    "value_to_date",
    "target",
    "forecast",
    "low",
    "high",
    "status",
    "master__name",
    "master__awp_code",
    "database__name",
    "database__label",
)


# ---------------------------------------------------------------------------- keys
def review_item_key(finding_key: str) -> str:
    """The watch item of a daily review finding: ``review:<finding key>``."""
    return fit_key(f"{REVIEW_PREFIX}{finding_key}")


def finding_key_of(item_key: str) -> str:
    """The daily review finding key of a ``review:`` item ('' for any other item)."""
    item_key = str(item_key or "")
    return item_key[len(REVIEW_PREFIX) :] if item_key.startswith(REVIEW_PREFIX) else ""


def forecast_item_key(year: int, master_id: int) -> str:
    """The watch item of a master indicator likely to fall short: ``forecast:<year>:<master id>``."""
    return fit_key(f"{FORECAST_PREFIX}{year}:{master_id}")


def _forecast_ids(item_key: str) -> tuple[int, int] | None:
    """(year, master id) of a ``forecast:`` item key, or None."""
    _, year, master = (str(item_key or "").split(":") + ["", "", ""])[:3]
    return (int(year), int(master)) if year.isdigit() and master.isdigit() else None


# ---------------------------------------------------------------------------- dates, not day counts
_DATE_THEN_DAYS_AGO = re.compile(r"(\b\d{4}), \d+ days? ago\b")  # "due on 12 Sep 2026, 22 days ago"
_IN_DAYS = re.compile(r"\bin (\d+) days?\b")  # "ends in 30 days"
_DAYS_AGO = re.compile(r"\b(\d+) days? ago\b")  # "visit ended 20 days ago"


def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def dated(text: str, on: datetime.date) -> str:
    """The review's ``text`` without the day counts that age by themselves, counted from ``on`` (the
    review's date): in the review of 5 Oct 2026, "ends in 30 days" becomes "ends on 4 Nov 2026" and
    "ended 20 days ago" "ended on 15 Sep 2026"; a count right after a date ("due on 12 Sep 2026, 23
    days ago") is dropped."""
    text = _DATE_THEN_DAYS_AGO.sub(r"\1", str(text or ""))
    text = _IN_DAYS.sub(lambda m: f"on {_day(on + datetime.timedelta(days=int(m[1])))}", text)
    return _DAYS_AGO.sub(lambda m: f"on {_day(on - datetime.timedelta(days=int(m[1])))}", text)


# ---------------------------------------------------------------------------- daily review findings
@dataclass
class ReviewRead:
    """What the import reads from the latest review, once per pass: the findings it imports, the keys
    of open findings now below warning, each imported finding's assignment as (status, owned now), and
    the findings handed over to a look-ahead item as finding key -> (item key, item title)."""

    review: DailyReview | None = None
    findings: list[ReviewFinding] = field(default_factory=list)
    lowered: set[str] = field(default_factory=set)
    assignments: dict[str, tuple[str, bool]] = field(default_factory=dict)
    handed_over: dict[str, tuple[str, str]] = field(default_factory=dict)


def latest_review(today: datetime.date) -> DailyReview | None:
    """The latest succeeded daily review up to ``today``."""
    return (
        DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED, date__lte=today)
        .order_by("-date")
        .first()
    )


def read_review(ctx: Context) -> ReviewRead:
    """The latest review as the import reads it (see :class:`ReviewRead`), read once per pass."""

    def load() -> ReviewRead:
        review = latest_review(ctx.today)
        if review is None:
            return ReviewRead()
        still_open = list(
            review.findings.exclude(state=ReviewFinding.State.RESOLVED)
            .exclude(check_id__in=LEFT_OUT)
            .order_by("rank", "id")
        )
        findings = [f for f in still_open if f.severity in IMPORTED]
        return ReviewRead(
            review=review,
            findings=findings,
            lowered={f.key for f in still_open if f.severity not in IMPORTED},
            assignments=assignments([f.key for f in findings]),
            handed_over=handed_over(ctx, findings),
        )

    return ctx.memo("concerns:review", load)


def assignments(keys: list[str]) -> dict[str, tuple[str, bool]]:
    """finding key -> (assignment status, owned now) for the findings that have an assignment. "Owned
    now" means an owner is written and the assignment is not closed. The owner and note are never
    read: the database answers whether an owner is written."""
    rows = FindingAssignment.objects.filter(key__in=keys)
    statuses = dict(rows.values_list("key", "status"))
    owned = set(rows.exclude(owner__regex=r"^\s*$").values_list("key", flat=True))
    closed = FindingAssignment.Status.CLOSED
    return {key: (status, key in owned and status != closed) for key, status in statuses.items()}


def handed_over(ctx: Context, findings: list[ReviewFinding]) -> dict[str, tuple[str, str]]:
    """The findings that are a look-ahead item's twin (a PD ending soon), as finding key -> (item key,
    item title), when the twin's check is on and follows that item in this pass. A check in trial
    hands nothing over: only the whole-country view sees its items."""
    twins = {f.key: deadlines.twin_of(f.key) for f in findings}
    twins = {key: twin for key, twin in twins.items() if twin}
    if not twins or _mode(TWIN_CHECK) != ON:
        return {}
    followed = _followed(ctx, TWIN_CHECK)
    return {key: (twin, followed[twin]) for key, twin in twins.items() if twin in followed}


def _mode(check: Detector) -> str:
    """The check's setting (its default before its first run)."""
    mode = DetectorSetting.objects.filter(detector=check.id).values_list("mode", flat=True).first()
    return mode or check.default_mode


def _followed(ctx: Context, check: Detector) -> dict[str, str]:
    """The items ``check`` follows in this pass, as key -> title: what it finds when its sources are
    fresh; otherwise (it is skipped) its items carried forward."""

    def load() -> dict[str, str]:
        if all(ctx.fresh(job, check.max_age_hours) for job in check.source_jobs):
            try:
                return {fit_key(c.key): c.title for c in check.run(ctx) or () if isinstance(c, Candidate)}
            except Exception:  # the check records its own error when it runs: keep what it followed
                logger.exception("NeuroDB Watch: could not read what %s follows", check.id)
        alive = WatchItem.objects.filter(detector=check.id, state__in=ALIVE)
        return dict(alive.values_list("key", "title"))

    return ctx.memo(f"concerns:followed:{check.id}", load)


def review_mark(ctx: Context) -> str:
    """The date of the review read: a finding it no longer lists is missed only at a newer review."""
    review = read_review(ctx).review
    return review.date.isoformat() if review else ""


def review_findings(ctx: Context) -> Iterator[Candidate | Attach]:
    """The open critical and warning findings of the latest review, each an item, or noted on the
    look-ahead item it hands over to."""
    read = read_review(ctx)
    for finding in read.findings:
        twin = read.handed_over.get(finding.key)
        if twin:
            yield Attach(key=twin[0], review_key=finding.key)
        else:
            yield finding_candidate(read, finding)


def _records(found: Mapping[str, Any], title: str, url: str) -> list[dict]:
    """The review's evidence records (lines of text) as watch records; the title when it has none."""
    records = []
    for line in list(found.get("records") or [])[:RECORDS_MAX]:
        if isinstance(line, Mapping):
            records.append(record(line.get("label") or "", line.get("date"), line.get("value", ""), url))
        elif str(line or "").strip():
            records.append(record(str(line)[:300], url=url))
    return records or [record(title, url=url)]


def finding_candidate(read: ReviewRead, finding: ReviewFinding) -> Candidate:
    """One review finding as a watch item (its text written by code, dated, never the assignment's
    owner or note)."""
    review = read.review
    found = finding.evidence if isinstance(finding.evidence, dict) else {}
    title = dated(finding.title, review.date)
    status, owned = read.assignments.get(finding.key, ("", False))
    return Candidate(
        key=review_item_key(finding.key),
        kind=CONCERN,
        severity=finding.severity,
        title=title,
        detail=dated(finding.detail, review.date),
        etools_sections=[finding.section] if finding.section else [],
        scope=SECTION if finding.section else COUNTRY,  # country-wide: the whole-country view
        entity_kind=FINDING,
        entity_key=finding.key[:HUB_KEY_MAX],
        url=finding.url,
        has_owner=owned,
        assignment_status=status,
        evidence={
            "source": REVIEW_SOURCE,
            "source_job": DAILY_REVIEW,
            "synced_at": review.finished_at.isoformat() if review.finished_at else "",
            "records": _records(found, title, finding.url),
            "numbers": {k: v for k, v in (found.get("numbers") or {}).items() if v is not None},
            "read": str(found.get("read") or "")[:500],  # what the review says to look at
            "review_date": review.date.isoformat(),
        },
    )


def review_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Imported findings now followed by a look-ahead item, or listed below warning: closed at once.
    A finding the review no longer lists gets no reason: missed at each newer review, then gone."""
    read = read_review(ctx)
    closes: dict[str, Close] = {}
    for item in items:
        key = finding_key_of(item.key)
        if key in read.handed_over:
            closes[item.key] = Close(f"followed from now on as: {read.handed_over[key][1]}")
        elif key in read.lowered:
            closes[item.key] = Close(LOWERED)
    return closes


# ---------------------------------------------------------------------------- year-end forecasts
def _shown(ctx: Context) -> bool:
    return ctx.memo("concerns:forecasts_shown", lambda: forecasts.shown(ctx.last_success(FORECAST)))


def _whole(value: float | None) -> int | None:
    return round(value) if value is not None else None


def _url(name: str, *args: Any, **params: Any) -> str:
    """A page URL with these query parameters (empty ones kept: "section=" means every section)."""
    try:
        url = reverse(name, args=args)
    except NoReverseMatch:
        return ""
    return f"{url}?{urlencode(params)}" if params else url


def forecasts_short(ctx: Context) -> Iterator[Candidate]:
    """The master indicators likely to fall short by December, while the forecasts are shown."""
    if not _shown(ctx):
        return
    rows = (
        IndicatorForecast.objects.filter(status=SHORT)
        .select_related("master", "database")
        .only(*FORECAST_FIELDS)
        .order_by("database_id", "master_id")
    )
    for forecast in rows:
        yield forecast_candidate(ctx, forecast)


def forecast_candidate(ctx: Context, f: IndicatorForecast) -> Candidate:
    """One indicator likely to fall short, worded as an estimate with its range."""
    name = " ".join(str(f.master.name or f.master.awp_code or f"Indicator {f.master_id}").split())
    if len(name) > NAME_CHARS:
        name = name[: NAME_CHARS - 1].rstrip() + "…"
    database = (f.database.label or f.database.name or "").strip()
    low, high, target = _whole(f.low), _whole(f.high), _whole(f.target)
    likely, to_date = _whole(f.forecast), _whole(f.value_to_date)
    month = forecasts.MONTHS[f.as_of_month] if 1 <= (f.as_of_month or 0) <= 12 else ""
    ranged = low is not None and high is not None
    span = f"{low:,} to {high:,}" if ranged else ""
    detail = "Estimate from the indicator's monthly pattern in past years: "
    detail += f"about {likely:,} by December" if likely is not None else "below its target by December"
    if ranged:
        detail += f", likely between {low:,} and {high:,}"
    if target:
        detail += f", against a target of {target:,}"
        if f.low_pct is not None and f.high_pct is not None:
            detail += f" ({f.low_pct:.0f}–{f.high_pct:.0f}% of target)"
    detail += f". {to_date:,} reported so far" + (f", with the months up to {month}" if month else "") + "."
    detail += " Check it against what you know: new activities, funding or late reports can move it."
    dashboard = _url("reports:database_dashboard", f.database_id)
    value = (f"likely {span}" + (f" of {target:,}" if target else "")) if ranged else "likely to fall short"
    numbers = {
        "forecast": likely,
        "low": low,
        "high": high,
        "target": target,
        "to_date": to_date,
        "low_pct": f.low_pct,
        "high_pct": f.high_pct,
        "as_of_month": f.as_of_month,
        "year": f.year,
    }
    return Candidate(
        key=forecast_item_key(f.year, f.master_id),
        kind=CONCERN,
        severity=INFO,
        confidence=LIKELY,
        title=f"Likely to fall short of its {f.year} target (estimate): {name}"
        + (f" ({database})" if database else ""),
        detail=detail,
        section_ids=[f.section_id] if f.section_id else [],
        scope=SECTION if f.section_id else COUNTRY,  # no section: the whole-country view
        entity_kind=MASTER,
        entity_key=str(f.master_id),
        url=_url("insights:forecasts", section=f.section_id or "", status=SHORT),
        evidence=evidence(
            ctx,
            FORECAST_SOURCE,
            FORECAST,
            [record(name + (f" · {database}" if database else ""), None, value, dashboard)],
            **{k: v for k, v in numbers.items() if v is not None},
        ),
    )


def forecasts_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Forecasts no longer likely to fall short: back on course ("recovered") or another status, both
    closed at once; all of them when the forecasts are no longer shown. An indicator gone from the
    forecasts gets no reason: missed at each newer forecast, then gone."""
    if not _shown(ctx):
        return {item.key: Close(NOT_SHOWN) for item in items}
    wanted = {item.key: ids for item in items if (ids := _forecast_ids(item.key))}
    rows = IndicatorForecast.objects.filter(master_id__in={master for _, master in wanted.values()})
    now = {master: (year, status) for master, year, status in rows.values_list("master_id", "year", "status")}
    closes: dict[str, Close] = {}
    for key, (year, master) in wanted.items():
        if master not in now:
            continue
        current_year, status = now[master]
        if current_year != year:
            closes[key] = Close(f"the forecasts are now for {current_year}")
        elif status == ON_COURSE:
            closes[key] = Close(RECOVERED)
        elif status != SHORT:
            label = dict(IndicatorForecast.Status.choices).get(status, status)
            closes[key] = Close(f"no longer likely to fall short (forecast now: {label})")
    return closes


# ---------------------------------------------------------------------------- the registry
REVIEW_IMPORT = register(
    Detector(
        id=REVIEW_ID,
        label="Daily review findings",
        run=review_findings,
        resolved=review_resolved,
        source_mark=review_mark,
        source_jobs=(DAILY_REVIEW,),
        default_mode=ON,
    )
)
FORECAST_SHORT = register(
    Detector(
        id=FORECAST_ID,
        label="Indicators likely to fall short (year-end forecast)",
        run=forecasts_short,
        resolved=forecasts_resolved,
        source_jobs=(FORECAST,),
        max_age_hours=FORECAST_FRESH_HOURS,
    )
)
