"""Running NeuroDB Watch: the morning pass, the quick pass after new data, and running again safely.

**The morning pass** (:func:`daily`; ``run_watch --daily``: the scheduled job "watch" at 07:45 Beirut
time, after the daily review at 06:00, the knowledge hub at 07:00 and What's new at 07:30; also "Run
NeuroDB Watch now" in the admin). One step after the other:

1. **sections**: eTools section names not in the map yet are added (:func:`neurodb.watch.sections.seed`);
2. **inputs**: while today's daily review or a knowledge hub build is still running, it waits, looking
   every minute, for at most 20 minutes; then it goes ahead and records what was not ready
   (``inputs_not_ready``). That input is read by the quick pass that follows when it lands;
3. **usefulness**: checks that people found unhelpful go back to trial
   (:func:`neurodb.watch.precision.demote`), so today's announcements already follow it;
4. **checks**: every check runs and the memory is updated (:func:`neurodb.watch.memory.run`);
5. **connect**: the open items are connected through the knowledge hub, and the notable What's new
   changes since the watermark are added to their stories (:mod:`neurodb.watch.connect`);
6. **announce**: who is told what (:func:`neurodb.watch.routing.announce`);
7. **notes**: the morning note of each audience (:func:`neurodb.watch.explain.write_all`), before the
   look-ups, so that they never take the notes' share of the day's AI budget;
8. **email**: the morning email (:func:`neurodb.watch.delivery.send_daily`), once per person and day,
   dormant until email is set up (``EMAIL_URL``);
9. **look_ups**: the AI looks into a few open critical items (:func:`neurodb.watch.investigate.run`),
   within what is left of the budget, and only while the time left leaves room for a whole look-up;
10. **watermark**: what was read is kept (``WatchState.last_change_id``) and, when the checks and the
    announcements succeeded, the day of the pass (``WatchState.last_daily_on``: a pass whose checks or
    announcements failed is caught up later the same day);
11. **retention**: receipts last told over 12 months ago, items closed over 24 months ago and the
    requests this pass answered are deleted.

A pass that ends without its email (it failed, was stopped or ran out of time first) still sends
today's What's new note to the people who asked for the email
(:func:`neurodb.watch.delivery.send_whats_new_instead`): the What's new email stepped aside for the
morning email. Requests for a quick pass that arrived while it ran (new data that landed meanwhile) get
a trailing quick pass under the same lock, unless an administrator stopped it. When another run holds
the lock, the morning pass waits for it (every minute, at most 20 minutes); it then runs, unless a
morning pass finished meanwhile.

**The quick pass** (:func:`when_requested`; ``run_watch --when-requested``, started by
:mod:`neurodb.watch.signals` when a knowledge hub build finishes, a job fails or a finding's
assignment is edited). It first waits ``WATCH_SETTLE_SECONDS`` (a burst of new data makes one pass),
then, holding the lock:

- after the morning pass's time, when no morning pass finished today (and none was stopped by an
  administrator today), it runs the morning pass instead (a catch-up, once per process);
- when the morning pass is due within 45 minutes, it leaves the requests to it;
- after ``WATCH_QUICK_PASSES_PER_DAY`` passes in a day (catch-ups included), later requests wait for
  the next morning;
- otherwise it runs the checks, the memory (no misses counted), the connections and the announcements
  (critical items, items due within 3 days and system items only), keeps the watermark and deletes the
  requests it answered. It never calls the AI and never sends email;
- requests that arrived meanwhile get a trailing pass, at most 3 passes per process.

**Running again safely.** One run at a time (:mod:`neurodb.watch.lock`): a second start does nothing.
Every run is a ``SyncRun`` (job "watch", target "daily" or "quick") whose details say what each step
did. Each step commits its own writes as it goes (an AI call is recorded as soon as it returns) and is
safe to repeat: items are keyed on lasting identifiers, one receipt per person and item holds the last
step told, one note per audience and day, and a What's new change goes into a story once. A step that
fails is recorded and the next steps still run (the run ends "Succeeded with errors"). Between steps
the run stops when an administrator stopped it, or when ``WATCH_TIME_LIMIT_SECONDS`` have passed since
its inputs were ready. A run left "running" by a process that died is closed by the next one. All
dates are Beirut dates (``timezone.localdate``).

The watch writes only its own tables, the AI use ledger and its own ``SyncRun``.
"""

from __future__ import annotations

import datetime
import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils import timezone

from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.core.models.sync import STOPPED
from neurodb.graph import refresh
from neurodb.integrations import background
from neurodb.integrations.runs import describe_error, new_run

from . import budget, connect, explain, investigate, lock, memory, routing, sections, signals
from .detectors import DAILY, QUICK, Context
from .models import WatchItem, WatchRequest, WatchState
from .signals import quick_passes_used  # the day's count, also read when new data asks for a pass

logger = logging.getLogger(__name__)

INPUTS = (SyncRun.Job.DAILY_REVIEW, SyncRun.Job.KNOWLEDGE_HUB)  # what the morning pass waits for
INPUT_WAIT_SECONDS = 20 * 60
INPUT_POLL_SECONDS = 60
LOCK_WAIT_SECONDS = 20 * 60  # the morning pass waits this long for another run to finish
LOCK_POLL_SECONDS = 60
RUNNING, NOT_TODAY = "running", "not_today"  # why an input was not ready (details["inputs_not_ready"])
DUE_SOON = datetime.timedelta(minutes=45)  # the morning pass is this close: the quick pass leaves it the data
MAX_PASSES = 3  # per quick process: data that keeps arriving waits for the next request
MORNING_COMMAND = "watch"  # the ScheduledJob command of the morning pass
ITEM_RETENTION_DAYS = 730  # items closed or gone this long ago are deleted (receipts: routing.prune)
AFTER_ROOM_SECONDS = 30  # a look-up starts only while this much time is left after it (watermark...)
ERROR_CHARS = 500

# Why a run stopped between steps (details["stopped"])
BY_ADMIN, TIME_UP = "stopped", "time_limit"

# What run_watch says when it ran nothing (more)
BUSY = "Another run of NeuroDB Watch is running; nothing to do."
SWITCHED_OFF = "NeuroDB Watch is switched off (WATCH_ENABLED); nothing was checked."
NOTHING_WAITING = "No new data is waiting for NeuroDB Watch; nothing to do."
DEFERRED = "The morning run of NeuroDB Watch starts within 45 minutes; the new data waits for it."
CAPPED = "NeuroDB Watch made its quick checks for today; the new data waits for the morning run."
ALREADY_RAN = "A morning run of NeuroDB Watch finished while this one waited; nothing more to do."
CATCH_UP = "catch-up"  # added to triggered_by when a quick pass runs the missed morning pass

_sleep = time.sleep  # the tests replace it


@dataclass
class Passes:
    """What one start of ``run_watch`` did: the runs it recorded (one ``SyncRun`` per pass) and, in
    plain words, why nothing (more) ran (empty when every request was answered)."""

    runs: list[SyncRun] = field(default_factory=list)
    note: str = ""


def run(
    mode: str = DAILY,
    triggered_by: str = "command",
    *,
    today: datetime.date | None = None,
    now: datetime.datetime | None = None,
    settle: float | None = None,
) -> Passes:
    """The morning pass (``daily``) or the quick pass after new data (``quick``). ``today`` is the day
    the checks reason from (``run_watch --date``); ``now`` the moment of the pass (the tests fix it;
    default: the clock)."""
    if mode == DAILY:
        return daily(triggered_by, today=today, now=now)
    if mode == QUICK:
        return when_requested(triggered_by, today=today, now=now, settle=settle)
    raise ValueError(f"unknown pass {mode!r}: {DAILY} or {QUICK}")


# ---------------------------------------------------------------------------- the morning pass
def daily(
    triggered_by: str = "command",
    *,
    today: datetime.date | None = None,
    now: datetime.datetime | None = None,
) -> Passes:
    """Run the morning pass (see the module's notes). When another run holds the lock, wait for it
    (every minute, at most 20 minutes; then nothing is done), and run unless a morning pass finished
    meanwhile. Requests for a quick pass that arrived while it ran get a trailing quick pass."""
    waited = 0
    while True:
        with lock.hold() as got:
            if got:
                if waited and _morning_done(today or _local_day(now)):
                    return Passes(note=ALREADY_RAN)  # e.g. the quick pass caught it up meanwhile
                _close_cut_off()
                if not settings.WATCH_ENABLED:
                    return Passes([_switched_off(DAILY, triggered_by, today, now)], SWITCHED_OFF)
                done = Passes([_daily(triggered_by, today, now)])
                if _latest_request() is not None and not done.runs[-1].stopped():
                    # new data landed while it ran: a trailing quick pass, as its own process would
                    _answer_requests(done, signals.TRIGGERED_BY, today, now, catch_up=False, defer=False)
                return done
        if waited >= LOCK_WAIT_SECONDS:
            return Passes(note=BUSY)
        _sleep(LOCK_POLL_SECONDS)
        waited += LOCK_POLL_SECONDS


def _morning_done(day: datetime.date) -> bool:
    """A morning pass whose checks and announcements succeeded ran on ``day``."""
    state = WatchState.objects.filter(pk=1).first()
    return state is not None and state.last_daily_on == day


def _daily(
    triggered_by: str,
    today: datetime.date | None,
    now: datetime.datetime | None,
    catch_up: bool = False,
) -> SyncRun:
    """One morning pass, the lock held."""
    run = new_run(SyncRun.Job.WATCH, DAILY, triggered_by[:150])
    answered = _latest_request()  # what this pass answers: the requests waiting when it starts
    pass_ = _Pass(run)
    run.details.update(mode=DAILY, catch_up=catch_up, model_calls=0, tokens=0)
    try:
        pass_.step("sections", lambda: _sections(run))
        pass_.step("inputs", lambda: _wait_for_inputs(pass_, now))
        pass_.start_clock()
        ctx = Context.make(DAILY, now=now, today=today, stop=pass_.stop)
        run.details["date"] = ctx.today.isoformat()
        watermark = WatchState.get().last_change_id
        pass_.step("usefulness", lambda: _usefulness(run, ctx.today))
        outcome = pass_.step("checks", lambda: memory.run(ctx, sync_run=run))
        top = pass_.step("connect", lambda: _connect(run, ctx, watermark))
        announced = pass_.step("announce", lambda: routing.announce(ctx, outcome, sync_run=run))
        pass_.step("notes", lambda: _notes(run, ctx.today, pass_.stop))
        pass_.step("email", lambda: _email(run, ctx.today))
        pass_.step("look_ups", lambda: _look_ups(run, ctx.today, pass_.no_room_for_a_look_up))
        # the day counts as done only when its checks and announcements were made (else: caught up)
        done_on = _local_day(now) if outcome is not None and announced is not None else None
        pass_.step("watermark", lambda: _keep(run, top, done_on))
        pass_.step("retention", lambda: _retention(run, ctx.today, answered))
        _whats_new_instead(pass_, ctx.today)
        return pass_.finish(outcome, announced)
    except Exception as exc:  # a fault of the runner itself (each step catches its own)
        _whats_new_instead(pass_, today or _local_day(now))
        return pass_.fail(exc)


# ---------------------------------------------------------------------------- the quick pass
def when_requested(
    triggered_by: str = "new data",
    *,
    today: datetime.date | None = None,
    now: datetime.datetime | None = None,
    settle: float | None = None,
) -> Passes:
    """Answer the requests for a quick pass (see the module's notes): wait ``settle`` seconds (default
    ``WATCH_SETTLE_SECONDS``), then, holding the lock, run quick passes while requests remain, at most
    3, or the missed morning pass instead of the first."""
    if not settings.WATCH_ENABLED:
        with lock.hold() as got:
            if not got:
                return Passes(note=BUSY)
            return Passes([_switched_off(QUICK, triggered_by, today, now)], SWITCHED_OFF)
    settle = settings.WATCH_SETTLE_SECONDS if settle is None else settle
    if settle:
        _sleep(settle)
    if _latest_request() is None:
        return Passes(note=NOTHING_WAITING)  # another process answered them
    with lock.hold() as got:
        if not got:
            # the run holding it answers the requests that arrived meanwhile (the morning pass's trailing
            # pass, or the other quick process's next pass)
            return Passes(note=BUSY)
        _close_cut_off()
        done = Passes()
        _answer_requests(done, triggered_by, today, now)
        return done


def _answer_requests(
    done: Passes,
    triggered_by: str,
    today: datetime.date | None,
    now: datetime.datetime | None,
    *,
    catch_up: bool = True,
    defer: bool = True,
) -> Passes:
    """The lock held: quick passes while requests remain, at most 3 per process, within the day's
    quick passes; with ``catch_up``, the missed morning pass instead of the first; with ``defer``, the
    requests are left to a morning pass due within 45 minutes. Adds the runs (and why it stopped) to
    ``done``."""
    caught_up = not catch_up
    started = len(done.runs)
    while len(done.runs) - started < MAX_PASSES:
        moment = now or timezone.now()
        if quick_passes_used(moment) >= settings.WATCH_QUICK_PASSES_PER_DAY:
            done.note = CAPPED
            break
        if not caught_up and catch_up_due(moment):
            caught_up = True
            _count_quick_pass(moment)
            done.runs.append(_daily(f"{triggered_by} ({CATCH_UP})", today, now, catch_up=True))
        elif _latest_request() is None:
            break
        elif defer and morning_due_soon(moment):
            done.note = DEFERRED
            break
        else:
            _count_quick_pass(moment)
            done.runs.append(_quick(triggered_by, today, now))
        if done.runs[-1].status == SyncRun.Status.FAILED:
            break  # the next request or the morning pass tries again
    return done


def _quick(triggered_by: str, today: datetime.date | None, now: datetime.datetime | None) -> SyncRun:
    """One quick pass, the lock held: the checks, the memory, the connections and the announcements,
    no AI and no email."""
    answered = _latest_request()
    reasons = sorted(set(WatchRequest.objects.filter(pk__lte=answered or 0).values_list("reason", flat=True)))
    who = f"{triggered_by}: {', '.join(reasons)}" if reasons else triggered_by
    run = new_run(SyncRun.Job.WATCH, QUICK, who[:150])
    pass_ = _Pass(run)
    run.details.update(mode=QUICK)
    try:
        pass_.start_clock()
        ctx = Context.make(QUICK, now=now, today=today, stop=pass_.stop)
        run.details["date"] = ctx.today.isoformat()
        watermark = WatchState.get().last_change_id
        outcome = pass_.step("checks", lambda: memory.run(ctx, sync_run=run))
        top = pass_.step("connect", lambda: _connect(run, ctx, watermark))
        announced = pass_.step("announce", lambda: routing.announce(ctx, outcome, sync_run=run))
        pass_.step("watermark", lambda: _keep(run, top, None))
        pass_.step("requests", lambda: _answer(run, answered))
        return pass_.finish(outcome, announced)
    except Exception as exc:
        return pass_.fail(exc)


# ---------------------------------------------------------------------------- when the morning pass runs
def _morning_job() -> ScheduledJob | None:
    """The enabled scheduled job of the morning pass (command "watch"), if any."""
    return ScheduledJob.objects.filter(command=MORNING_COMMAND, enabled=True).order_by("pk").first()


def _next_time(job: ScheduledJob, after: datetime.datetime) -> datetime.datetime | None:
    from neurodb.core.cron import CronError, next_after

    try:
        return next_after(job.schedule, after)
    except CronError:
        return None


def morning_time(day: datetime.date) -> datetime.datetime | None:
    """When the morning pass is scheduled on ``day`` (Beirut time), or None (no enabled job, or none
    that day)."""
    job = _morning_job()
    if job is None:
        return None
    first = _next_time(job, _start_of(day) - datetime.timedelta(minutes=1))
    return first if first is not None and timezone.localdate(first) == day else None


def catch_up_due(moment: datetime.datetime) -> bool:
    """The morning pass's time today has passed and no morning pass finished today: it was missed (the
    site was down, its process was cut off, or a quick pass held the lock at that minute). A morning
    pass an administrator stopped today is not run again by itself."""
    day = timezone.localdate(moment)
    scheduled = morning_time(day)
    if scheduled is None or moment < scheduled:
        return False
    state = WatchState.objects.filter(pk=1).first()
    if state is not None and state.last_daily_on == day:
        return False
    stopped_today = SyncRun.objects.filter(
        job=SyncRun.Job.WATCH,
        target=DAILY,
        started_at__gte=_start_of(day),
        error__startswith=STOPPED,
    )
    return not stopped_today.exists()


def morning_due_soon(moment: datetime.datetime) -> bool:
    """The morning pass is scheduled within 45 minutes after ``moment``."""
    job = _morning_job()
    upcoming = _next_time(job, moment) if job else None
    return upcoming is not None and upcoming <= moment + DUE_SOON


def _count_quick_pass(moment: datetime.datetime) -> None:
    day = timezone.localdate(moment)
    state = WatchState.get()
    if state.quick_passes_on != day:
        state.quick_passes_on, state.quick_passes_count = day, 0
    state.quick_passes_count += 1
    state.save(update_fields=["quick_passes_on", "quick_passes_count"])


# ---------------------------------------------------------------------------- the steps
def _sections(run: SyncRun) -> dict[str, int]:
    counts = sections.seed()
    run.details["sections"] = counts
    return counts


def _running_inputs() -> list[str]:
    """The inputs of the morning pass still being made: today's daily review (its run holds its lock)
    and a knowledge hub build (it holds the hub's lock)."""
    busy = []
    if background.is_running(SyncRun.Job.DAILY_REVIEW):
        busy.append(SyncRun.Job.DAILY_REVIEW)
    if background.lock_is_held(refresh.LOCK_ID):
        busy.append(SyncRun.Job.KNOWLEDGE_HUB)
    return busy


def _wait_for_inputs(pass_: _Pass, now: datetime.datetime | None) -> dict[str, str]:
    """Wait while the daily review or a hub build is running (every minute, at most 20 minutes, or
    until an administrator stops the run), then record in ``inputs_not_ready`` what is still running
    or did not succeed today."""
    waited = 0
    while True:
        busy = _running_inputs()
        if not busy or waited >= INPUT_WAIT_SECONDS or pass_.run.stopped():
            break
        _sleep(INPUT_POLL_SECONDS)
        waited += INPUT_POLL_SECONDS
    day = _local_day(now)
    not_ready = {job: RUNNING for job in busy}
    for job in INPUTS:
        last = SyncRun.last_success(job)
        if job not in not_ready and (
            last is None or last.finished_at is None or timezone.localdate(last.finished_at) != day
        ):
            not_ready[job] = NOT_TODAY
    pass_.run.details.update(inputs_not_ready=not_ready, waited_seconds=waited)
    return not_ready


def _hook(module: str, name: str) -> Callable[..., Any] | None:
    """A step another part of the watch provides (the morning email, the usefulness scores), or None
    while that part is not installed."""
    path = f"{__package__}.{module}"
    try:
        found = importlib.import_module(path)
    except ModuleNotFoundError as exc:
        if exc.name != path:
            raise  # the module is there but something it needs is not: a real fault
        return None
    return getattr(found, name, None)


def _usefulness(run: SyncRun, today: datetime.date) -> Any:
    """Checks people found unhelpful go back to trial (``precision.demote(today)``)."""
    demote = _hook("precision", "demote")
    if demote is None:
        return None
    result = demote(today)
    run.details["usefulness"] = result if isinstance(result, dict | list) else str(result)
    return result


def _connect(run: SyncRun, ctx: Context, watermark: int) -> int:
    """Connect the open items through the hub, then add the What's new changes read after
    ``watermark`` to their stories; the highest change id read (the next watermark)."""
    situations = connect.link(today=ctx.today)
    stats: dict[str, int] = {}
    top = connect.changes_since(watermark, today=ctx.today, stats=stats)
    run.details["connect"] = {
        "situations": len(situations),
        "changes_read": stats.get("read", 0),
        "changes_added": stats.get("added", 0),
        "items_with_changes": stats.get("items", 0),
    }
    run.details["changes_until"] = top
    return top


def _look_ups(run: SyncRun, today: datetime.date, stop: Callable[[], bool]) -> investigate.Summary:
    """The look-ups, after the notes: they use what the notes left of the day's AI budget."""
    summary = investigate.run(today, stop=stop)
    run.details["look_ups"] = summary.details()
    _add_ai_use(run, summary.calls, summary.tokens)
    run.details["ai_use_today"] = budget.status()  # the watch's tokens and calls against its caps
    return summary


def _notes(run: SyncRun, today: datetime.date, stop: Callable[[], bool]) -> explain.Summary:
    summary = explain.write_all(today, use_ai=True, stop=stop)
    details = summary.details()
    run.details["notes"] = details["notes"]
    run.details["ai_skipped_reason"] = details["ai_skipped_reason"]
    _add_ai_use(run, details["model_calls"], details["tokens"])
    run.details["ai_use_today"] = budget.status()  # the watch's tokens and calls against its caps
    return summary


def _add_ai_use(run: SyncRun, calls: int, tokens: int) -> None:
    """The run's AI calls and tokens, the morning notes and the look-ups together."""
    run.details["model_calls"] = run.details.get("model_calls", 0) + int(calls or 0)
    run.details["tokens"] = run.details.get("tokens", 0) + int(tokens or 0)


def _email(run: SyncRun, today: datetime.date) -> Any:
    """The morning email (``delivery.send_daily(today)``), sent once per person and day."""
    send_daily = _hook("delivery", "send_daily")
    if send_daily is None:
        run.details["emails"] = {"sent": 0, "note": "the morning email is not set up"}
        return None
    sent = send_daily(today)
    run.details["emails"] = sent if isinstance(sent, dict) else {"sent": int(sent or 0)}
    return sent


def _whats_new_instead(pass_: _Pass, today: datetime.date) -> None:
    """A morning pass that ended without its email step (it failed, was stopped or ran out of time
    first): today's What's new note goes to the people who asked for the email all the same
    (``delivery.send_whats_new_instead``), since the What's new email stepped aside for this one. Never
    fails the run."""
    if "email" in pass_.done:
        return
    send = _hook("delivery", "send_whats_new_instead")
    if send is None:
        return
    try:
        pass_.run.details["emails"] = send(today)
        pass_.run.details.setdefault("date", today.isoformat())  # the day it was for
        pass_.run.save(update_fields=["details"])
    except Exception:
        logger.exception("NeuroDB Watch: the What's new note could not be emailed instead")


def _keep(run: SyncRun, top: int | None, morning_of: datetime.date | None) -> None:
    """Keep the watermark (it only moves forward, and only to a change id read in this pass) and, for
    the morning pass, the day it ran (``morning_of``)."""
    state = WatchState.get()
    changed = []
    if top is not None and top > state.last_change_id:
        state.last_change_id = top
        changed.append("last_change_id")
    if morning_of is not None:
        state.last_daily_on = morning_of
        changed.append("last_daily_on")
    if changed:
        state.save(update_fields=changed)
    run.details["changes_until"] = state.last_change_id


def _answer(run: SyncRun, answered: int | None) -> int:
    """Delete the requests the pass answered (those waiting when it started)."""
    deleted = WatchRequest.objects.filter(pk__lte=answered).delete()[0] if answered else 0
    run.details["requests"] = deleted
    return deleted


def _retention(run: SyncRun, today: datetime.date, answered: int | None) -> dict[str, int]:
    """Delete receipts last told over 12 months ago, items closed or gone over 24 months ago (with
    their receipts) and the requests this pass answered."""
    receipts = routing.prune(today)
    cutoff = today - datetime.timedelta(days=ITEM_RETENTION_DAYS)
    old = WatchItem.objects.filter(
        state__in=(WatchItem.State.CLOSED, WatchItem.State.GONE), closed_on__lt=cutoff
    )
    items = old.delete()[1].get(WatchItem._meta.label, 0)
    counts = {"receipts": receipts, "items": items, "requests": _answer(run, answered)}
    run.details["retention"] = counts
    return counts


# ---------------------------------------------------------------------------- helpers
def _local_day(now: datetime.datetime | None) -> datetime.date:
    return timezone.localdate(now or timezone.now())


def _start_of(day: datetime.date) -> datetime.datetime:
    """Midnight at the start of ``day``, Beirut time."""
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=timezone.get_current_timezone())


def _latest_request() -> int | None:
    return WatchRequest.objects.order_by("-pk").values_list("pk", flat=True).first()


def _close_cut_off() -> int:
    """Close the watch's runs left "running" by a process that died (a restart): the lock is held
    here, so no other run is in progress."""
    closed = SyncRun.objects.filter(job=SyncRun.Job.WATCH, status=SyncRun.Status.RUNNING).update(
        status=SyncRun.Status.FAILED, finished_at=timezone.now(), error=background.CUT_OFF
    )
    if closed:
        logger.warning("NeuroDB Watch: closed %s run(s) cut off by a stopped process", closed)
    return closed


def _switched_off(
    mode: str, triggered_by: str, today: datetime.date | None, now: datetime.datetime | None
) -> SyncRun:
    run = new_run(SyncRun.Job.WATCH, mode, triggered_by[:150])
    day = today or _local_day(now)
    run.finish(SyncRun.Status.SUCCEEDED, mode=mode, date=day.isoformat(), note=SWITCHED_OFF)
    return run


class _Pass:
    """One pass's steps: each runs unless the run must stop, a failing one is recorded and the next
    ones still run, and the run's details are saved after each (a cut-off run shows how far it got)."""

    def __init__(self, run: SyncRun):
        self.run = run
        self.deadline: float | None = None  # the time limit, from when the inputs are ready
        self.stopped = ""  # BY_ADMIN or TIME_UP
        self.errors: dict[str, str] = {}
        self.seconds: dict[str, float] = {}
        self.not_run: list[str] = []
        self.done: set[str] = set()  # the steps that ran without an error

    def start_clock(self) -> None:
        self.deadline = time.monotonic() + settings.WATCH_TIME_LIMIT_SECONDS

    def left(self) -> float:
        """Seconds left before the time limit (unlimited before the clock starts)."""
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def stop(self) -> bool:
        """The run must stop: an administrator stopped it, or its time is up."""
        if not self.stopped:
            if self.run.stopped():
                self.stopped = BY_ADMIN
            elif self.left() <= 0:
                self.stopped = TIME_UP
        return bool(self.stopped)

    def no_room_for_a_look_up(self) -> bool:
        """No (more) look-up: the run must stop, or the time left would not hold a whole look-up and
        the short steps after it."""
        return self.stop() or self.left() < investigate.TIME_LIMIT + AFTER_ROOM_SECONDS

    def step(self, name: str, work: Callable[[], Any]) -> Any:
        """Run one step; its result, or None when it failed or did not run."""
        if self.stop():
            self.not_run.append(name)
            self._save()
            return None
        started = time.monotonic()
        try:
            result = work()
        except Exception as exc:
            logger.exception("NeuroDB Watch: the step %s failed", name)
            self.errors[name] = describe_error(exc)[:ERROR_CHARS]
            return None
        else:
            self.done.add(name)
            return result
        finally:
            self.seconds[name] = round(time.monotonic() - started, 1)
            self._save()

    def _save(self) -> None:
        self.run.details.update(
            steps=dict(self.seconds), step_errors=dict(self.errors), not_run=list(self.not_run)
        )
        if self.stopped:
            self.run.details["stopped"] = self.stopped
        self.run.save(update_fields=["details"])

    def finish(self, outcome: memory.Outcome | None, announced: routing.Announced | None) -> SyncRun:
        """Record how the pass ended: succeeded, or with errors when a check or a step failed or the
        time ran out (a run an administrator stopped stays stopped)."""
        run = self.run
        check_errors = outcome.errors if outcome is not None else {}
        run.rows_in = outcome.seen if outcome is not None else 0
        run.rows_written = (announced.created + announced.updated) if announced is not None else 0
        run.rows_failed = len(check_errors) + len(self.errors)
        problems = [f"{name}: {error}" for name, error in self.errors.items()]
        problems += [f"check {check}: {error}" for check, error in check_errors.items()]
        if self.stopped == TIME_UP:
            problems.append(
                f"Stopped at the time limit ({settings.WATCH_TIME_LIMIT_SECONDS} s) before: "
                + ", ".join(self.not_run)
            )
        status = SyncRun.Status.PARTIAL if problems else SyncRun.Status.SUCCEEDED
        run.finish(status, error="; ".join(problems))
        logger.info(
            "NeuroDB Watch %s pass: %s (%s items seen, %s receipts written)",
            run.target,
            run.status,
            run.rows_in,
            run.rows_written,
        )
        return run

    def fail(self, exc: Exception) -> SyncRun:
        logger.exception("NeuroDB Watch: the %s pass failed", self.run.target)
        self.run.finish(SyncRun.Status.FAILED, error=describe_error(exc))
        return self.run
