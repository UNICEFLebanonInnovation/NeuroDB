"""System health for the administrators: the admin home's "Needs attention" lines, followed by the watch.

Each line of :func:`neurodb.web.health.warnings` (the same list the admin home shows) becomes the item
``system:<its key>``: told to the administrators only, on from the start (no trial). The scheduler not
checking in and a missing daily review are critical; the other lines are warnings. When a line is no
longer listed the problem was fixed, and its item closes at once.

"NeuroDB Watch has not run for 26 hours" is left out of the morning pass: that pass is the run it
asks for (a quick pass still raises it, as the morning run is still missing).

These items are never sent to the AI: they carry error text and server details (see
:func:`neurodb.watch.redact.refused`).
"""

from __future__ import annotations

from neurodb.watch.models import fit_key
from neurodb.web import health

from . import ADMINS, CRITICAL, ON, SYSTEM, WARNING, Candidate, Close, Context, Detector, record, register

ID = "system"
PREFIX = "system:"  # the stale-source items use "system:stale:"; no health key starts with "stale:"
SOURCE = "NeuroDB system health"
DETAIL = (
    "Listed under Needs attention on the admin home. It closes by itself once the problem is fixed and the "
    "line is gone."
)
FIXED = "fixed: no longer listed under Needs attention"
FIXED_BY_THE_MORNING_PASS = frozenset({"watch_not_run"})


def key_of(line: health.Warning) -> str:
    """The watch item's key for one line: ``system:<its key>``."""
    return fit_key(f"{PREFIX}{line.key}")


def lines(ctx: Context) -> dict[str, health.Warning]:
    """The lines of this pass by item key, read once (``run`` and ``resolved`` share them)."""

    def read() -> dict[str, health.Warning]:
        found = health.warnings(ctx.now)
        if ctx.daily:
            found = [line for line in found if line.key not in FIXED_BY_THE_MORNING_PASS]
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
    """The items whose line is gone: fixed, closed at once."""
    current = lines(ctx)
    return {item.key: Close(FIXED) for item in open_items if item.key not in current}


SYSTEM_HEALTH = register(
    Detector(
        id=ID,
        label="System health (administrators)",
        run=run,
        resolved=resolved,
        default_mode=ON,
    )
)
