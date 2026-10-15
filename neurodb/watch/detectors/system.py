"""System health for the administrators: the admin home's "Needs attention" lines, followed by the watch.

Each line of :func:`neurodb.web.health.warnings` (the same list the admin home shows) becomes the item
``system:<its key>``: told to the administrators only, on from the start (no trial). The scheduler not
checking in and a missing daily review are critical; the other lines are warnings. When a line is no
longer listed the problem was fixed, and its item closes at once: resolved. The exception is a line
about the ActivityInfo import (its set-up lines, its failed or partial runs:
:func:`neurodb.web.health.about_activityinfo`) gone because the import is no longer followed (ActivityInfo
is an archive): its item closes as changed, "no longer followed", never as fixed, since the problem may
well still be there.

"NeuroDB Watch has not run for 26 hours" is not raised by the morning pass: that pass is the run it
asks for (a quick pass still raises it, as the morning run is still missing). Nor does the morning pass
close it: while the line is listed, an item already open for it is carried as it is, since the pass
that would fix it has not succeeded yet. It closes in the first pass after a morning pass succeeded
(the line is then gone).

These items are never sent to the AI: they carry error text and server details (see
:func:`neurodb.watch.redact.refused`).
"""

from __future__ import annotations

from neurodb.watch.models import WatchItem, fit_key
from neurodb.web import health

from . import (
    ADMINS,
    ALIVE,
    CHANGED,
    CRITICAL,
    ON,
    RESOLVED,
    SYSTEM,
    WARNING,
    Candidate,
    Close,
    Context,
    Detector,
    record,
    register,
)

ID = "system"
PREFIX = "system:"  # the stale-source items use "system:stale:"; no health key starts with "stale:"
SOURCE = "NeuroDB system health"
DETAIL = (
    "Listed under Needs attention on the admin home. It closes by itself once the problem is fixed and the "
    "line is gone."
)
FIXED = "fixed: no longer listed under Needs attention"
NOT_FOLLOWED = "ActivityInfo is an archive: its import is no longer followed"
FIXED_BY_THE_MORNING_PASS = frozenset({"watch_not_run"})


def key_of(line: health.Warning) -> str:
    """The watch item's key for one line: ``system:<its key>``."""
    return fit_key(f"{PREFIX}{line.key}")


def lines(ctx: Context) -> dict[str, health.Warning]:
    """The lines of this pass by item key, read once (``run`` and ``resolved`` share them)."""

    def read() -> dict[str, health.Warning]:
        found = health.warnings(ctx.now)
        if ctx.daily:
            # not raised by the pass it asks for; an item already open for it stays as it is
            waiting = [key_of(line) for line in found if line.key in FIXED_BY_THE_MORNING_PASS]
            carried = set(
                WatchItem.objects.filter(key__in=waiting, state__in=ALIVE).values_list("key", flat=True)
                if waiting
                else ()
            )
            found = [
                line for line in found if line.key not in FIXED_BY_THE_MORNING_PASS or key_of(line) in carried
            ]
        return {key_of(line): line for line in found}

    return ctx.memo("health_warnings", read)


def candidate(ctx: Context, line: health.Warning) -> Candidate:
    """One line as a system item for the administrators. Its evidence is what the line rests on (the
    last run of a job, the scheduler's last check-in...), in values that stay the same while the
    problem does."""
    return Candidate(
        key=key_of(line),
        detector=ID,
        kind=SYSTEM,
        severity=CRITICAL if line.critical else WARNING,
        scope=ADMINS,
        title=(line.title or line.text)[:300],
        detail=DETAIL,
        url=line.url,
        evidence={
            "source": SOURCE,
            "source_job": "",
            "synced_at": "",
            "records": [record(line.read or line.text, line.on, line.value, line.url)],
            "numbers": dict(line.numbers),
        },
    )


def run(ctx: Context) -> list[Candidate]:
    return [candidate(ctx, line) for line in lines(ctx).values()]


def resolved(ctx: Context, open_items) -> dict[str, Close]:
    """The items whose line is gone, closed at once: fixed; or, for a line about the ActivityInfo import
    while it is no longer followed, no longer followed (it may still fail: nobody is told any more)."""
    from neurodb.indicators.services import activityinfo

    current = lines(ctx)
    closes: dict[str, Close] = {}
    for item in open_items:
        if item.key in current:
            continue
        line_key = item.key.removeprefix(PREFIX)
        if health.about_activityinfo(line_key) and not ctx.memo("activityinfo_watched", activityinfo.watched):
            closes[item.key] = Close(NOT_FOLLOWED, kind=CHANGED)
        else:
            closes[item.key] = Close(FIXED, kind=RESOLVED)
    return closes


SYSTEM_HEALTH = register(
    Detector(
        id=ID,
        label="System health (administrators)",
        run=run,
        resolved=resolved,
        default_mode=ON,
    )
)
