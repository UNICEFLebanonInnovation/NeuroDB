"""The action point history: a small record of each eTools action point's status, kept from now on.

``ActionPointStatus`` gets one row for an action point, by its eTools id (which lasts: its Datamart id may
change), on each day its recorded state changed:

* a first row the first time NeuroDB sees the point;
* a row on a day its status, priority, due date or completion date differs from its latest row (a change to
  its section, module or links alone writes none);
* a row on the day it is no longer in the Datamart (``in_datamart`` false, with its last known values; the
  sync removes a point only once a whole read no longer returns it), and one on the day it comes back.

Each read is filed under the day the action points were read (:func:`read_day`): after a sync, the day that
sync started (the nightly sync starts at 20:30 and can end after midnight, so two nightly reads never share a
day); from the job, the day of the latest Datamart read of the action points that did not fail, else today. A
second change the same day (a daytime sync and that evening's) updates that day's row, and a change undone the
same day removes that day's row (the day then holds no change), so running it twice on one day writes nothing
new; a row of an earlier day is never deleted. An empty action point table never marks every point gone: an
empty answer of the Datamart never empties the table, so that is not a real day.

It reads the action points and the latest row of each point as two streams in eTools id order and walks them
together (``values_list`` and ``iterator``), and writes in batches (``bulk_create``): neither the number of
queries nor the memory grows with the points. It reads no text and no name: no description, assignee, partner
name or place name.

It runs right after each eTools Datamart sync that refreshed the action points (``sync_etools_datamart``),
and as the job ``ap_history`` (*Run a job → Record action point status*), each filed under its read's day.
Each run is a ``SyncRun`` with counts only.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Iterator
from typing import Any

from django.db import connection, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.integrations.background import AP_HISTORY_LOCK_ID as LOCK_ID
from neurodb.integrations.runs import fail, finish_by_counts, new_run

from .models import ActionPoint, ActionPointStatus

DATASET = "action_points"  # the Datamart sync's dataset (its run's target)
TRACKED = (
    "status",
    "high_priority",
    "due_date",
    "date_of_completion",
)  # a change to one of these is recorded
KEPT = (
    *TRACKED,
    "related_module",
    "section",
    "engagement_source_id",
    "tpm_activity_source_id",
    "intervention_id",
    "partner_id",
)
CHUNK = 2000  # rows read at a time
BATCH = 1000  # rows written at a time
ANOTHER_RUN = "Another record of the action point status is running; nothing to do."


def _counts(today: datetime.date) -> dict[str, Any]:
    return {
        "day": today.isoformat(),
        "points": 0,  # the action points held, as the action points page lists them with no filter
        "without_etools_id": 0,  # not kept: nothing would find them again
        "repeated": 0,  # rows that share an eTools id with another: the point is kept once
        "first_seen": 0,
        "changed": 0,
        "gone": 0,
        "returned": 0,
        "unchanged": 0,
        "added": 0,  # rows written
        "updated": 0,  # today's rows, changed again the same day
        "removed": 0,  # today's rows, the point back where it was the day before
    }


def _points() -> Iterator[tuple]:
    """(eTools id, *KEPT) of each action point with an eTools id, in eTools id order; of two rows with one
    eTools id, the one changed last comes first."""
    return (
        ActionPoint.objects.exclude(source_id=None)
        .order_by("source_id", F("last_modify_date").desc(nulls_last=True), "-datamart_id")
        .values_list("source_id", *KEPT)
        .iterator(chunk_size=CHUNK)
    )


def _latest() -> Iterator[tuple]:
    """(eTools id, pk, day, in_datamart, *KEPT) of each point's latest row, in eTools id order."""
    return (
        ActionPointStatus.objects.order_by("source_id", "-day")
        .distinct("source_id")
        .values_list("source_id", "pk", "day", "in_datamart", *KEPT)
        .iterator(chunk_size=CHUNK)
    )


def _once(rows: Iterator[tuple], counts: dict[str, Any]) -> Iterator[tuple]:
    """The first row of each eTools id (the rows come in eTools id order); the others are counted."""
    last = None
    for row in rows:
        if row[0] == last:
            counts["repeated"] += 1
            continue
        last = row[0]
        yield row


def _pairs(
    points: Iterator[tuple], latest: Iterator[tuple]
) -> Iterator[tuple[int, tuple | None, tuple | None]]:
    """(eTools id, the point's values or None, its latest row or None), walking both streams in eTools id
    order."""
    point, row = next(points, None), next(latest, None)
    while point is not None or row is not None:
        if row is None or (point is not None and point[0] < row[0]):
            yield point[0], point[1:], None
            point = next(points, None)
        elif point is None or row[0] < point[0]:
            yield row[0], None, row
            row = next(latest, None)
        else:
            yield point[0], point[1:], row
            point, row = next(points, None), next(latest, None)


def record(today: datetime.date | None = None) -> dict[str, Any]:
    """Record today's state of every action point whose state changed since its latest row (and of every point
    seen for the first time, gone or back). Returns the counts; writes nothing twice the same day."""
    today = today or timezone.localdate()
    counts = _counts(today)
    held = ActionPoint.objects.aggregate(points=Count("pk"), without=Count("pk", filter=Q(source_id=None)))
    counts["points"], counts["without_etools_id"] = held["points"], held["without"]
    if held["points"] == held["without"]:
        counts["nothing_held"] = True  # never every point gone at once
        return counts
    with transaction.atomic():
        _walk(today, counts)
    return counts


def _walk(today: datetime.date, counts: dict[str, Any]) -> None:
    new: list[ActionPointStatus] = []
    again: dict[int, tuple[int, bool, tuple]] = {}  # eTools id: (today's row, in the Datamart, values)

    def write(source_id: int, in_datamart: bool, values: tuple, latest: tuple | None) -> None:
        if latest is not None and latest[2] == today:  # changed again today: that day's row
            again[source_id] = (latest[1], in_datamart, values)
            return
        if latest is not None and latest[2] > today:  # a later day is recorded already: never one before it
            return
        new.append(
            ActionPointStatus(
                source_id=source_id,
                day=today,
                in_datamart=in_datamart,
                **dict(zip(KEPT, values, strict=True)),
            )
        )
        if len(new) >= BATCH:
            _add(new, counts)

    tracked = len(TRACKED)
    for source_id, values, latest in _pairs(_once(_points(), counts), _latest()):
        if latest is None:
            counts["first_seen"] += 1
            write(source_id, True, values, None)
        elif values is None:
            if latest[3]:  # no longer in the Datamart (said once, the day it went)
                counts["gone"] += 1
                write(source_id, False, latest[4:], latest)
        elif not latest[3]:
            counts["returned"] += 1
            write(source_id, True, values, latest)
        elif values[:tracked] != latest[4 : 4 + tracked]:
            counts["changed"] += 1
            write(source_id, True, values, latest)
        else:
            counts["unchanged"] += 1
    _add(new, counts)
    _settle(today, again, counts)


def _add(new: list[ActionPointStatus], counts: dict[str, Any]) -> None:
    if new:
        ActionPointStatus.objects.bulk_create(new, batch_size=BATCH)
        counts["added"] += len(new)
        new.clear()


def _settle(today: datetime.date, again: dict[int, tuple[int, bool, tuple]], counts: dict[str, Any]) -> None:
    """Today's rows a later change of the same day touched: each holds the day's last state, or goes when the
    point is back where it was the day before (the day then holds no change)."""
    if not again:
        return
    ids = sorted(again)
    before: dict[int, tuple] = {}
    for start in range(0, len(ids), BATCH):
        for source_id, *state in (
            ActionPointStatus.objects.filter(source_id__in=ids[start : start + BATCH], day__lt=today)
            .order_by("source_id", "-day")
            .distinct("source_id")
            .values_list("source_id", "in_datamart", *TRACKED)
        ):
            before[source_id] = tuple(state)
    undone, changed = [], []
    for source_id, (pk, in_datamart, values) in again.items():
        if before.get(source_id) == (in_datamart, *values[: len(TRACKED)]):
            undone.append(pk)
        else:
            changed.append(
                ActionPointStatus(pk=pk, in_datamart=in_datamart, **dict(zip(KEPT, values, strict=True)))
            )
    for start in range(0, len(undone), BATCH):
        ActionPointStatus.objects.filter(pk__in=undone[start : start + BATCH]).delete()
    ActionPointStatus.objects.bulk_update(changed, ["in_datamart", *KEPT], batch_size=BATCH)
    counts["updated"] += len(changed)
    counts["removed"] += len(undone)


# ------------------------------------------------------------------------------------------------ its runs
def _try_lock() -> bool:
    """One record at a time: False when another process holds the lock."""
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def run(triggered_by: str = "schedule", today: datetime.date | None = None) -> SyncRun | str:
    """Record the action point status as a run (job ``ap_history``): ``rows_in`` the points held,
    ``rows_written`` the rows written, updated or removed, ``details`` the counts. :data:`ANOTHER_RUN` (and no
    run) while another record is going."""
    if not _try_lock():
        return ANOTHER_RUN
    try:
        sync_run = new_run(SyncRun.Job.AP_HISTORY, "", triggered_by)
        try:
            counts = record(today)
        except Exception as exc:
            return fail(sync_run, exc)
        sync_run.rows_in = counts["points"]
        sync_run.rows_written = counts["added"] + counts["updated"] + counts["removed"]
        return finish_by_counts(sync_run, **counts)
    finally:
        _unlock()


def run_safely(triggered_by: str = "schedule", today: datetime.date | None = None) -> SyncRun | str:
    """:func:`run` that never raises: an error is recorded as a failed run instead."""
    try:
        return run(triggered_by, today)
    except Exception as exc:
        return fail(new_run(SyncRun.Job.AP_HISTORY, "", triggered_by), exc)


def read_day(runs: Iterable[SyncRun] | None = None) -> datetime.date:
    """The day the action points were read, which the record files them under (in Beirut): with the ``runs``
    of a sync, the day that sync started (its earliest run), so that a nightly sync ending after midnight
    files its read under its night; without, the day the latest Datamart read of the action points that did
    not fail started; else today."""
    if runs is not None:
        start = min((r.started_at for r in runs if r.started_at), default=None)
    else:
        start = (
            SyncRun.objects.filter(
                job=SyncRun.Job.ETOOLS_DATAMART,
                target=DATASET,
                status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
            )
            .order_by("-started_at", "-pk")
            .values_list("started_at", flat=True)
            .first()
        )
    return timezone.localdate(start) if start else timezone.localdate()


def wanted_after(runs: Iterable[SyncRun]) -> bool:
    """A Datamart sync refreshed the action points: their dataset's run did not fail (one that succeeded with
    errors wrote the other records)."""
    return any(
        run.target == DATASET and run.status in (SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL)
        for run in runs
    )
