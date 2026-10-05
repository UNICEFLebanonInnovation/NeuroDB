"""The checks of NeuroDB Watch and what they share.

A check asks one fixed question of NeuroDB's own tables (no AI) and answers with **candidates**: the
things it finds today, each with a lasting key, a severity and due date set by rules, and its
evidence. The memory (:mod:`neurodb.watch.memory`) turns candidates into ``WatchItem`` rows and
decides what opened, changed, closed or went.

Each check is registered once with :func:`register`, as a :class:`Detector`:

- ``id`` and ``label``;
- ``source_jobs``: the ``SyncRun`` jobs whose data it reads. It runs only when each had a success
  within ``SYNC_STALENESS_HOURS`` (or its own ``max_age_hours``); otherwise it is skipped, its items
  are left as they are, and one ``system:stale:<job>`` item tells the administrators;
- ``run(ctx)``: the candidates (and :class:`Attach` hand-overs to the daily review);
- ``resolved(ctx, open_items)``: evidence that some of its open items are over, as ``{key: Close}``
  (a report submitted, an FR spent). These close at once. A :class:`Close` says how: ``RESOLVED``
  (done: the default), ``MISSED`` (its date passed and it was not done: a report now overdue, a grant
  expired with money unspent) or ``CHANGED`` (no longer followed here: its date moved, another point
  follows it). People are told the three differently, never "resolved" for a missed date;
- ``source_mark(ctx)``: how fresh the data it read is (by default the time of the oldest last
  success of its source jobs). An item it no longer finds counts as missed only when this mark is
  newer than the item's own, so an item is never closed by data older than what showed it;
- ``default_mode``: new checks start in trial (seen only by the whole-country view); the daily
  review import and the system checks start on;
- ``milestones``: days before the due date at which people are told again (14, 7, 3, 1, 0).

The check modules of this package (``deadlines.py``, ``money.py``...) register their checks when
imported; :func:`registered` imports them all. :func:`collect` runs the checks one by one: a check
that raises is recorded and skipped, and the others still run.
"""

from __future__ import annotations

import datetime
import importlib
import logging
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.watch.models import KEY_MAX, RECORDS_MAX, DetectorSetting, WatchItem

logger = logging.getLogger(__name__)

DAILY, QUICK = "daily", "quick"  # the morning pass, and the quick pass after new data

# The choices of WatchItem, for the check modules
DEADLINE, CONCERN, SYSTEM = WatchItem.Kind.DEADLINE, WatchItem.Kind.CONCERN, WatchItem.Kind.SYSTEM
CRITICAL, WARNING, INFO = WatchItem.Severity.CRITICAL, WatchItem.Severity.WARNING, WatchItem.Severity.INFO
SURE, LIKELY, CHECK = WatchItem.Confidence.SURE, WatchItem.Confidence.LIKELY, WatchItem.Confidence.CHECK
SECTION, COUNTRY, ADMINS = WatchItem.Scope.SECTION, WatchItem.Scope.COUNTRY, WatchItem.Scope.ADMINS
TRIAL, ON, OFF = DetectorSetting.Mode.TRIAL, DetectorSetting.Mode.ON, DetectorSetting.Mode.OFF

SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}
# How an item closed (Close.kind, WatchItem.close_kind)
RESOLVED = WatchItem.CloseKind.RESOLVED
MISSED, CHANGED = WatchItem.CloseKind.MISSED, WatchItem.CloseKind.CHANGED
ALIVE = (WatchItem.State.OPEN, WatchItem.State.WRONG)  # items a check still follows

STALE_DETECTOR = "stale_source"  # the check behind the "system:stale:<job>" items
STALE_KEY = "system:stale:"

# Why a check did not run in a pass (Result.skipped)
SKIPPED_OFF, SKIPPED_STALE, SKIPPED_ERROR, SKIPPED_STOPPED = "off", "stale", "error", "stopped"


# ---------------------------------------------------------------------------- one pass
@dataclass
class Context:
    """What every check of one pass shares: the day it reasons from (Beirut time), the moment it
    started, whether it is the morning pass or a quick pass, and lookups read once per pass."""

    today: datetime.date
    now: datetime.datetime
    mode: str = DAILY
    stop: Callable[[], bool] | None = None  # true when the run must stop (an admin stopped it, time is up)
    cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def make(
        cls,
        mode: str = DAILY,
        now: datetime.datetime | None = None,
        today: datetime.date | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> Context:
        """A pass starting ``now`` (default: this moment). ``today`` is the local date of ``now``
        unless given (``run_watch --date``)."""
        if mode not in (DAILY, QUICK):
            raise ValueError(f"unknown pass {mode!r}: {DAILY} or {QUICK}")
        now = now or timezone.now()
        return cls(today=today or timezone.localdate(now), now=now, mode=mode, stop=stop)

    @property
    def daily(self) -> bool:
        return self.mode == DAILY

    def memo(self, name: str, compute: Callable[[], Any]) -> Any:
        """``compute()`` once per pass, kept under ``name`` (the checks share their lookups)."""
        if name not in self.cache:
            self.cache[name] = compute()
        return self.cache[name]

    def last_success(self, job: str) -> SyncRun | None:
        """The job's last succeeded (or partly succeeded) run, read once per pass."""
        return self.memo(f"last_success:{job}", lambda: SyncRun.last_success(job))

    def fresh(self, job: str, max_age_hours: int | None = None) -> bool:
        """The job succeeded within ``max_age_hours`` (default ``SYNC_STALENESS_HOURS``) of the pass."""
        last = self.last_success(job)
        hours = max_age_hours or settings.SYNC_STALENESS_HOURS
        return bool(
            last and last.finished_at and last.finished_at >= self.now - datetime.timedelta(hours=hours)
        )


def mark_of(moment: datetime.datetime) -> str:
    """A freshness mark for a moment: UTC ISO text, so that marks compare correctly as text."""
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds")


def newer(mark: str, than: str) -> bool:
    """``mark`` is a later version of the source than ``than`` (an empty mark is never newer)."""
    return bool(mark) and mark > (than or "")


# ---------------------------------------------------------------------------- what a check gives
@dataclass(kw_only=True)
class Candidate:
    """One thing a check found today, before it is remembered. Everything here is written by code;
    ``evidence`` must hold at least one record it was found in. Its records and numbers say what
    the item rests on: when they change, an item people marked wrong comes back, so day counts that
    move by themselves go under the names in ``memory.COUNTING_NUMBERS`` (``days_left``...)."""

    key: str  # from lasting identifiers, e.g. "due:report:<PD number>:<progress report>"
    detector: str = ""  # filled with the check's id when blank
    kind: str
    severity: str
    confidence: str = SURE
    title: str  # dates, never day counts
    detail: str = ""
    due_date: datetime.date | None = None
    etools_sections: list[str] = field(default_factory=list)
    section_ids: list[int] = field(default_factory=list)  # empty: resolved from etools_sections
    scope: str = SECTION
    entity_kind: str = ""  # the knowledge hub (kind, key) it is about
    entity_key: str = ""
    evidence: dict[str, Any]  # {source, source_job, synced_at, records: [{label, date, value, url}], numbers}
    url: str = ""
    review_key: str = ""  # the daily review finding it hands over to
    milestones: tuple[int, ...] = ()  # days before due_date at which people are told again
    has_owner: bool = False  # someone is assigned to it (never who)
    assignment_status: str = ""

    def __post_init__(self):
        if not str(self.key or "").strip():
            raise ValueError("a watch item needs a key")
        if not str(self.title or "").strip():
            raise ValueError(f"{self.key}: a watch item needs a title")
        for name, value, allowed in (
            ("kind", self.kind, WatchItem.Kind.values),
            ("severity", self.severity, WatchItem.Severity.values),
            ("confidence", self.confidence, WatchItem.Confidence.values),
            ("scope", self.scope, WatchItem.Scope.values),
        ):
            if value not in allowed:
                raise ValueError(f"{self.key}: {name} {value!r} is not one of {', '.join(allowed)}")
        records = self.evidence.get("records") if isinstance(self.evidence, dict) else None
        if not records or not isinstance(records, list | tuple):
            raise ValueError(
                f"{self.key}: a watch item needs its evidence: at least one record it was found in"
            )
        self.milestones = tuple(sorted({int(m) for m in self.milestones}, reverse=True))


@dataclass(frozen=True)
class Attach:
    """The daily review now flags something the watch already follows (a due report become
    overdue): hand the item over by noting the review's key on it, instead of a second item."""

    key: str  # the watch item's key
    review_key: str


@dataclass(frozen=True)
class Close:
    """Evidence that an item is over, said in plain words ("report submitted on 3 Oct"), how it ended
    (``kind``: ``RESOLVED``, ``MISSED`` or ``CHANGED``) and the daily review finding that takes it
    over, if any."""

    reason: str
    review_key: str = ""
    kind: str = RESOLVED

    def __post_init__(self):
        if self.kind not in WatchItem.CloseKind.values:
            raise ValueError(f"a close is {', '.join(WatchItem.CloseKind.values)}, not {self.kind!r}")


def record(label: str, date: datetime.date | str | None = None, value: Any = "", url: str = "") -> dict:
    """One evidence record: what was read, its date, its value and where to see it."""
    if isinstance(date, datetime.date):
        date = date.isoformat()
    return {
        "label": str(label),
        "date": date or "",
        "value": value if value is not None else "",
        "url": url or "",
    }


def evidence(ctx: Context, source: str, job: str, records: Iterable[dict], **numbers: Any) -> dict:
    """An item's evidence: the source in plain words ("eTools progress reports"), the job that
    syncs it with the time of its last success, the records (at most 10) and the numbers."""
    last = ctx.last_success(job) if job else None
    return {
        "source": source,
        "source_job": job,
        "synced_at": last.finished_at.isoformat() if last and last.finished_at else "",
        "records": list(records)[:RECORDS_MAX],
        "numbers": numbers,
    }


# ---------------------------------------------------------------------------- the checks
@dataclass(frozen=True)
class Detector:
    """One registered check (see the module's notes)."""

    id: str
    label: str
    run: Callable[[Context], Iterable[Candidate | Attach]]
    source_jobs: tuple[str, ...] = ()
    resolved: Callable[[Context, list[WatchItem]], dict[str, str | Close]] | None = None
    source_mark: Callable[[Context], str] | None = None
    default_mode: str = TRIAL
    milestones: tuple[int, ...] = ()
    max_age_hours: int | None = None  # a weekly source (the forecast) is fresh for longer

    def __post_init__(self):
        if not self.id or len(self.id) > 40:
            raise ValueError(f"a check id has 1 to 40 characters: {self.id!r}")
        unknown = [job for job in self.source_jobs if job not in SyncRun.Job.values]
        if unknown:
            raise ValueError(f"{self.id}: unknown source jobs {unknown}")
        object.__setattr__(self, "milestones", tuple(sorted({int(m) for m in self.milestones}, reverse=True)))

    def mark(self, ctx: Context) -> str:
        """How fresh the data it reads is: its own ``source_mark``, else the oldest last success of
        its source jobs, else the moment of the pass (it reads live tables)."""
        if self.source_mark is not None:
            return str(self.source_mark(ctx) or "")
        marks = [ctx.last_success(job) for job in self.source_jobs]
        if not marks:
            return mark_of(ctx.now)
        if any(last is None or last.finished_at is None for last in marks):
            return ""
        return min(mark_of(last.finished_at) for last in marks)


REGISTRY: dict[str, Detector] = {}
_discovered = False


def register(detector: Detector) -> Detector:
    """Add a check to the registry (a check module calls it when imported). Ids are unique: the
    same id registered again replaces the earlier entry."""
    REGISTRY[detector.id] = detector
    return detector


def _discover() -> None:
    """Import every check module of this package once, so that each registers its checks."""
    global _discovered
    if _discovered:
        return
    for module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
    _discovered = True


def registered() -> list[Detector]:
    """Every registered check, in the order the modules registered them."""
    _discover()
    return list(REGISTRY.values())


def ensure_settings() -> int:
    """Give every check its row, in its starting mode, before its first run, so an administrator
    can switch it on or off from the start. Rows already there are not touched. Returns how many
    were added."""
    from neurodb.watch.budget import QUOTA_CHECK  # budget reads the models only: no cycle, but kept lazy

    wanted = {detector.id: detector.default_mode for detector in registered()}
    wanted.setdefault(STALE_DETECTOR, ON)
    wanted.setdefault(QUOTA_CHECK, ON)
    known = set(DetectorSetting.objects.filter(detector__in=wanted).values_list("detector", flat=True))
    for detector_id, mode in wanted.items():
        if detector_id not in known:
            DetectorSetting.ensure(detector_id, mode)
    return len(wanted.keys() - known)


def get(detector_id: str) -> Detector | None:
    _discover()
    return REGISTRY.get(detector_id)


def milestones_of(item: WatchItem) -> tuple[int, ...]:
    """The item's milestones (kept in its evidence), else its check's."""
    stored = (item.evidence or {}).get("milestones") if isinstance(item.evidence, dict) else None
    if stored:
        return tuple(sorted({int(m) for m in stored}, reverse=True))
    detector = REGISTRY.get(item.detector)
    return detector.milestones if detector else ()


def milestone(due_date: datetime.date | None, milestones: Iterable[int], today: datetime.date) -> int | None:
    """The milestone reached on ``today``: the nearest one at or above the days left (with 14, 7, 3,
    1, 0 and 5 days left: 7). None before the first milestone, once past due, or without a due date."""
    if due_date is None:
        return None
    left = (due_date - today).days
    reached = [m for m in milestones if m >= left >= 0]
    return min(reached) if reached else None


# ---------------------------------------------------------------------------- running the checks
@dataclass
class Result:
    """What one check gave in one pass. ``skipped`` says why it did not run: off, stale (the
    jobs in ``stale_jobs``), error or stopped; a check that did not run changes nothing."""

    detector: Detector
    mode: str = TRIAL  # its setting: trial, on or off
    candidates: list[Candidate] = field(default_factory=list)
    attach: list[Attach] = field(default_factory=list)
    closes: dict[str, Close] = field(default_factory=dict)
    mark: str = ""
    skipped: str = ""
    stale_jobs: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ran(self) -> bool:
        return not self.skipped


def collect(ctx: Context, detectors: Iterable[Detector] | None = None) -> list[Result]:
    """Run the checks (default: every registered one), each on its own: a check that is off, reads
    a stale source or raises is recorded and skipped. A last result holds one ``system:stale:<job>``
    candidate per stale job, and closes the earlier ones whose job ran again since."""
    detectors = registered() if detectors is None else list(detectors)
    results = [_run_one(ctx, detector) for detector in detectors]
    results.append(_stale_result(ctx, detectors, results))
    return results


def _run_one(ctx: Context, detector: Detector) -> Result:
    setting = DetectorSetting.ensure(detector.id, detector.default_mode)
    result = Result(detector=detector, mode=setting.mode)
    if setting.mode == OFF:
        result.skipped = SKIPPED_OFF
        return result
    if ctx.stop is not None and ctx.stop():
        result.skipped = SKIPPED_STOPPED
        return result
    result.stale_jobs = [job for job in detector.source_jobs if not ctx.fresh(job, detector.max_age_hours)]
    if result.stale_jobs:
        result.skipped = SKIPPED_STALE
        return result
    try:
        result.mark = detector.mark(ctx)
        for found in detector.run(ctx) or ():
            if isinstance(found, Attach):
                result.attach.append(found)
                continue
            if not isinstance(found, Candidate):
                raise TypeError(f"a check gives candidates, not {type(found).__name__}")
            if not found.detector:
                found.detector = detector.id
            elif found.detector != detector.id:
                raise ValueError(f"{found.key}: found by {detector.id}, not {found.detector}")
            if not found.milestones:
                found.milestones = detector.milestones
            result.candidates.append(found)
        if detector.resolved is not None:
            alive = list(WatchItem.objects.filter(detector=detector.id, state__in=ALIVE))
            for key, why in (detector.resolved(ctx, alive) or {}).items():
                result.closes[key] = why if isinstance(why, Close) else Close(str(why))
    except Exception as exc:
        logger.exception("NeuroDB Watch: the check %s failed", detector.id)
        result.candidates, result.attach, result.closes = [], [], {}
        result.skipped = SKIPPED_ERROR
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    return result


def _stale_result(ctx: Context, detectors: list[Detector], results: list[Result]) -> Result:
    """The data sources that were too old for a check: one system candidate per job, and a close
    for the earlier ones whose job has succeeded again within its limit."""
    stale_check = Detector(
        id=STALE_DETECTOR, label="Data sources not refreshed", run=lambda ctx: (), default_mode=ON
    )
    setting = DetectorSetting.ensure(STALE_DETECTOR, ON)
    result = Result(detector=stale_check, mode=setting.mode, mark=mark_of(ctx.now))
    if setting.mode == OFF:
        result.skipped = SKIPPED_OFF
        return result
    limits: dict[str, int] = {}
    for detector in detectors:
        for job in detector.source_jobs:
            limits[job] = max(limits.get(job, 0), detector.max_age_hours or settings.SYNC_STALENESS_HOURS)
    stale: list[str] = []
    for each in results:
        stale += [job for job in each.stale_jobs if job not in stale]
    result.candidates = [stale_candidate(ctx, job, limits.get(job)) for job in stale]
    for item in WatchItem.objects.filter(detector=STALE_DETECTOR, state__in=ALIVE):
        job = (item.evidence or {}).get("source_job") or item.key.removeprefix(STALE_KEY)
        if job not in stale and ctx.fresh(job, limits.get(job)):
            last = ctx.last_success(job)
            result.closes[item.key] = Close(
                f"{_job_label(job)} ran again on {timezone.localtime(last.finished_at):%d %b %Y %H:%M}",
                kind=RESOLVED,
            )
    return result


def _job_label(job: str) -> str:
    return SyncRun.Job(job).label if job in SyncRun.Job.values else job


def stale_candidate(ctx: Context, job: str, limit_hours: int | None = None) -> Candidate:
    """The system item for one job that has not succeeded recently enough for the checks reading it."""
    limit_hours = limit_hours or settings.SYNC_STALENESS_HOURS
    label = _job_label(job)
    last = ctx.last_success(job)
    try:
        url = reverse("admin:core_syncrun_changelist") + f"?job__exact={job}"
    except NoReverseMatch:
        url = ""
    if last and last.finished_at:
        when = timezone.localtime(last.finished_at)
        title = f"{label}: no successful run since {when:%d %b %Y %H:%M}"
        found = record(f"Last successful run of the {label}", when.date(), last.get_status_display(), url)
    else:
        title = f"{label}: no successful run yet"
        found = record(f"Last successful run of the {label}", None, "none", url)
    return Candidate(
        key=f"{STALE_KEY}{job}"[:KEY_MAX],
        detector=STALE_DETECTOR,
        kind=SYSTEM,
        severity=WARNING,
        scope=ADMINS,
        title=title,
        detail=(
            f"The checks that read its data need a successful run within {limit_hours} hours. They were "
            "skipped: nothing they follow was added or closed until it runs again."
        ),
        url=url,
        evidence={
            "source": "NeuroDB jobs",
            "source_job": job,
            "synced_at": last.finished_at.isoformat() if last and last.finished_at else "",
            "records": [found],
            "numbers": {"limit_hours": limit_hours},
        },
    )
