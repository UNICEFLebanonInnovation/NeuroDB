"""ActivityInfo: in use, or an archive of the years partners reported there. One state for every page.

Partners reported their monthly results in ActivityInfo until 2026 and report in eTools from then on. The
ActivityInfo pages of every year stay open; what changes is how prominent ActivityInfo is and whether its
import raises alarms. Three rules, read by the menu, Home, the alarms, the landing page and Ask:

- **Rule 1, figures** (:attr:`State.years`): a year *Y* is an ActivityInfo year when a reporting year whose
  calendar year is *Y* (:func:`neurodb.core.years.number_of`) has a database shown on the dashboard with at
  least one ActivityInfo record. Several such reporting years: the one ticked current, else the highest
  name. The mode never changes this rule: a year with records was an ActivityInfo year, and its figures are
  history.
- **Rule 2, in use** (:attr:`State.in_use`): the administrator's mode (admin, "ActivityInfo: in use or
  archive") sets it; Automatic (the default, also when nobody ever opened the page) means in use while the
  import is switched on in Scheduled jobs (any switched-on job that runs the ActivityInfo data import,
  whatever its key) **and** the year the import serves (:attr:`State.serving`: this year, or last year
  during January, when the import still brings in December's results) has records no older than
  :data:`RECENT_MONTHS` months. An import left on over a year partners stopped reporting in archives by
  itself.
- **Rule 3, watched** (:attr:`State.watched`): the import's own alarms are on when the mode is "In use", or
  on Automatic while the import is switched on and either ActivityInfo is in use or the databases of a
  returning year wait for their first records (:attr:`State.setup_pending`), so a failing comeback is
  reported from day one. Never when "Archived".

The state is plain data, computed in four queries and kept 30 seconds per worker (the default cache, under
a key holding this year and month), and read once per request. Every input invalidates it on the worker
that changed it (the setting's admin page, Scheduled jobs, reporting years and databases, the end of an
import); the other workers follow within 30 seconds.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.db.models import Exists, OuterRef
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from neurodb.core import compute, years
from neurodb.core.models import ActivityInfoSetting, ScheduledJob, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear

AUTO, IN_USE, ARCHIVED = "auto", "in_use", "archived"
AI_JOBS = (SyncRun.Job.ACTIVITYINFO_STRUCTURE, SyncRun.Job.ACTIVITYINFO_DATA)
RECENT_MONTHS = 3  # records of a month no more than this many months before today's keep it in use
CACHE_KEY = "activityinfo:state:v1:{year}:{month}"  # the default (per-worker) cache
CACHE_SECONDS = 30
_MONTH = re.compile(r"^(\d{4})-(\d{2})")
# Why Rule 2 gives what it gives (State.reason)
REASONS = (
    "auto_in_use",
    "auto_january",
    "auto_no_records",
    "auto_not_recent",
    "auto_schedule_off",
    "set_in_use",
    "set_archived",
)


@dataclass(frozen=True)
class State:
    """ActivityInfo now: plain values only (kept in the cache as a dict)."""

    this_year: int
    mode: str  # AUTO, IN_USE or ARCHIVED
    schedule_on: bool  # a switched-on scheduled job runs the ActivityInfo data import
    years: tuple[int, ...]  # the ActivityInfo years (Rule 1), newest first
    names: dict[int, str]  # year -> the name of its reporting year
    pks: dict[int, int]  # year -> the pk of its reporting year
    first: int | None
    last: int | None
    span: str  # "2019–2026", "2026" or ""
    serving: int | None  # the ActivityInfo year the import still serves (this year; last year in January)
    latest_month: str  # "YYYY-MM": the newest month of the serving year's records, or ""
    recent: bool  # latest_month is no more than RECENT_MONTHS months before today's month
    setup_pending: bool  # the year ticked current is this year and its databases wait for their first records
    in_use: bool  # Rule 2
    watched: bool  # Rule 3
    reason: str  # one of REASONS
    current_name: str | None  # the reporting year ticked current: what the import reads


# ------------------------------------------------------------------------------------------- reading
def state() -> State:
    """ActivityInfo now: read once per request, kept 30 seconds per worker."""
    return compute.per_request("activityinfo-state", _read)


def in_use() -> bool:
    """Rule 2: the menu label and place, forecasts, the landing page's tense, Ask."""
    return state().in_use


def watched() -> bool:
    """Rule 3: the ActivityInfo import's own alarms are on."""
    return state().watched


def is_year(number: Any) -> bool:
    """Rule 1: ``number`` is an ActivityInfo year (a year with records)."""
    year = _as_int(number)
    return year is not None and year in state().years


def reporting_year_for(number: Any) -> ReportingYear | None:
    """The reporting year whose ActivityInfo figures count for the calendar year ``number`` (Rule 1), or
    None when that year has no ActivityInfo records. Read once per request."""
    year = _as_int(number)
    pk = state().pks.get(year) if year is not None else None
    if pk is None:
        return None
    return compute.per_request(f"ai-ry:{year}", lambda: ReportingYear.objects.filter(pk=pk).first())


def invalidate() -> None:
    """Forget the state on this worker (the others follow within 30 seconds)."""
    cache.delete(_key())


def _key() -> str:
    today = years.today()
    return CACHE_KEY.format(year=today.year, month=today.month)


def _read() -> State:
    key = _key()
    cached = cache.get(key)
    if cached is not None:
        return State(**cached)
    st = compute_state()
    cache.set(key, dataclasses.asdict(st), CACHE_SECONDS)
    return st


def _as_int(number: Any) -> int | None:
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------------------- computing
def compute_state() -> State:
    """The state from the database, in four queries (three when no year is served)."""
    from neurodb.core.jobs import COMMANDS

    today = years.today()
    this_year = today.year
    # 1. the administrator's mode (no row: Automatic)
    mode = ActivityInfoSetting.objects.filter(pk=1).values_list("mode", flat=True).first() or AUTO
    if mode not in (AUTO, IN_USE, ARCHIVED):
        mode = AUTO
    # 2. a switched-on job runs the data import, whatever its key
    commands = ScheduledJob.objects.filter(enabled=True).values_list("command", flat=True)
    schedule_on = any(
        command in COMMANDS and COMMANDS[command].sync_job == SyncRun.Job.ACTIVITYINFO_DATA
        for command in commands
    )
    # 3. every reporting year: shown databases, and whether one of them has a record
    shown = Database.objects.filter(reporting_year=OuterRef("pk"), display=True)
    with_records = shown.filter(Exists(ActivityReportNew.objects.filter(dbase=OuterRef("pk"))))
    rows = list(
        ReportingYear.objects.annotate(shown=Exists(shown), records=Exists(with_records))
        .order_by("-current", "-name", "-pk")
        .values_list("pk", "name", "year", "current", "shown", "records")
    )
    chosen: dict[int, tuple[int, str]] = {}  # year -> (pk, name): the current row, else the highest name
    current_row = None
    for pk, name, year_text, current, has_shown, has_records in rows:
        number = years.number_of(_Named(name, year_text))
        if current and current_row is None:
            current_row = (number, name, has_shown, has_records)
        if number is not None and has_records and number not in chosen:
            chosen[number] = (pk, name)
    ai_years = tuple(sorted(chosen, reverse=True))
    first = ai_years[-1] if ai_years else None
    last = ai_years[0] if ai_years else None
    span = "" if first is None else str(first) if first == last else f"{first}–{last}"
    serving = None
    if this_year in chosen:
        serving = this_year
    elif today.month == 1 and this_year - 1 in chosen:
        serving = this_year - 1  # January still reports December (rows.extraction_month)
    # 4. the newest month of the serving year's records
    latest_month = ""
    if serving is not None:
        newest = (
            ActivityReportNew.objects.filter(
                dbase__in=Database.objects.filter(reporting_year_id=chosen[serving][0], display=True),
                month_name__regex=r"^[0-9]{4}-[0-9]{2}",
            )
            .order_by("-month_name")
            .values_list("month_name", flat=True)
            .first()
        )
        latest_month = (newest or "")[:7]
    recent = False
    if match := _MONTH.match(latest_month):
        months_back = (today.year * 12 + today.month) - (int(match[1]) * 12 + int(match[2]))
        recent = months_back <= RECENT_MONTHS
    setup_pending = bool(
        current_row
        and current_row[0] == this_year
        and current_row[2]  # a database shown on the dashboard
        and not current_row[3]  # none of them has a record yet
    )
    if mode == IN_USE:
        used, reason = True, "set_in_use"
    elif mode == ARCHIVED:
        used, reason = False, "set_archived"
    elif not schedule_on:
        used, reason = False, "auto_schedule_off"
    elif serving is None:
        used, reason = False, "auto_no_records"
    elif not recent:
        used, reason = False, "auto_not_recent"
    else:
        used, reason = True, "auto_january" if serving == this_year - 1 else "auto_in_use"
    watched = mode == IN_USE or (mode == AUTO and schedule_on and (used or setup_pending))
    return State(
        this_year=this_year,
        mode=mode,
        schedule_on=schedule_on,
        years=ai_years,
        names={year: name for year, (_pk, name) in chosen.items()},
        pks={year: pk for year, (pk, _name) in chosen.items()},
        first=first,
        last=last,
        span=span,
        serving=serving,
        latest_month=latest_month,
        recent=recent,
        setup_pending=setup_pending,
        in_use=used,
        watched=watched,
        reason=reason,
        current_name=current_row[1] if current_row else None,
    )


@dataclass(frozen=True)
class _Named:
    """What :func:`neurodb.core.years.number_of` reads, from a ``values_list`` row."""

    name: str
    year: str | None


# ------------------------------------------------------------------------------------------ explaining
def month_label(latest_month: str) -> str:
    """A month written "YYYY-MM" as "Aug 2026" (in the active language); "" when it is not a month."""
    match = _MONTH.match(latest_month or "")
    if not match or not 1 <= int(match[2]) <= 12:
        return ""
    return date_format(datetime.date(int(match[1]), int(match[2]), 1), "M Y")


def explain(st: State | None = None) -> list[str]:
    """Why ActivityInfo is in use or archived, in plain words (the admin page and its tile): the reason,
    the years with records, the import; and, while the databases of a returning year wait for their first
    records and the import's alarms are on, that too."""
    st = st or state()
    month = month_label(st.latest_month)
    if st.reason == "set_in_use":
        line = _("In use (set by an administrator).")
    elif st.reason == "set_archived":
        line = _("Archived (set by an administrator).")
    elif st.reason == "auto_schedule_off":
        line = _("Archived (automatic): the ActivityInfo import is switched off in Scheduled jobs.")
    elif st.reason == "auto_no_records":
        line = _(
            "Archived (automatic): %(year)s has no ActivityInfo records. It turns in use by itself when the "
            "first %(year)s records are imported."
        ) % {"year": st.this_year}
    elif st.reason == "auto_not_recent" and month:
        line = _(
            "Archived (automatic): the newest ActivityInfo records are for %(month)s, more than three months "
            "ago. It turns in use by itself when newer records are imported."
        ) % {"month": month}
    elif st.reason == "auto_not_recent":
        line = _(
            "Archived (automatic): the newest ActivityInfo records have no month NeuroDB can read. It turns "
            "in use by itself when newer records are imported."
        )
    elif st.reason == "auto_january":
        line = _(
            "In use (automatic): in January the import still brings in the December %(year)s results."
        ) % {"year": st.serving}
    else:
        line = _(
            "In use (automatic): %(year)s has ActivityInfo records up to %(month)s and the ActivityInfo "
            "import is switched on."
        ) % {"year": st.serving, "month": month}
    lines = [
        line,
        _("Years with ActivityInfo records: %(span)s.") % {"span": st.span or _("none")},
        (
            _("The ActivityInfo import is on. It imports the reporting year ticked current: %(name)s.")
            if st.schedule_on
            else _("The ActivityInfo import is off. It imports the reporting year ticked current: %(name)s.")
        )
        % {"name": st.current_name or _("none")},
    ]
    if st.setup_pending and st.watched:
        lines.append(
            _(
                "The %(year)s ActivityInfo databases wait for their first records: the import's alarms "
                "stay on."
            )
            % {"year": st.this_year}
        )
    return lines
