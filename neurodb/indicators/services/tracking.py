"""Tracking status of an indicator against its target (ported from v2 ``setTrackingStatus``).

Rule from the SDD: compare the percentage achieved with the percentage of the year elapsed;
within ±10 points is on track, below is off track, above is over target; no target means no status.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

ON_TRACK, OFF_TRACK, OVER_TARGET, NO_TARGET = "on_track", "off_track", "over_target", "no_target"
LABELS = {ON_TRACK: "On track", OFF_TRACK: "Off track", OVER_TARGET: "Over target", NO_TARGET: "No target"}
TOLERANCE = 10


def percentage_of_year_elapsed(year: int, today: datetime.date | None = None) -> float:
    today = today or datetime.date.today()
    if today.year > year:
        return 100.0
    if today.year < year:
        return 0.0
    days_in_year = (datetime.date(year + 1, 1, 1) - datetime.date(year, 1, 1)).days
    return min(100.0, (today - datetime.date(year, 1, 1)).days / days_in_year * 100)


@dataclass(frozen=True)
class Tracking:
    status: str
    achieved: float | None  # percentage of target

    @property
    def label(self) -> str:
        return LABELS[self.status]


def tracking(
    value: float | None, target: float | None, year: int, today: datetime.date | None = None
) -> Tracking:
    if not target or target <= 0:
        return Tracking(NO_TARGET, None)
    achieved = (value or 0) * 100 / target
    elapsed = percentage_of_year_elapsed(year, today)
    if achieved - elapsed >= TOLERANCE:
        return Tracking(OVER_TARGET, achieved)
    if elapsed - achieved >= TOLERANCE:
        return Tracking(OFF_TRACK, achieved)
    return Tracking(ON_TRACK, achieved)


def percentage_elapsed(
    start: datetime.date | None, end: datetime.date | None, today: datetime.date | None = None
) -> float:
    """Share of the period ``start``..``end`` elapsed at ``today``: 0 before it starts, 100 once it
    has ended; without dates, the share of the current calendar year (the ActivityInfo rule)."""
    today = today or datetime.date.today()
    if not start or not end or end <= start:
        return percentage_of_year_elapsed(today.year, today)
    if today <= start:
        return 0.0
    if today >= end:
        return 100.0
    return (today - start).days / (end - start).days * 100


def _status(achieved: float, elapsed: float) -> str:
    if achieved - elapsed >= TOLERANCE:
        return OVER_TARGET
    if elapsed - achieved >= TOLERANCE:
        return OFF_TRACK
    return ON_TRACK


def tracking_between(
    value: float | None,
    target: float | None,
    start: datetime.date | None,
    end: datetime.date | None,
    today: datetime.date | None = None,
) -> Tracking:
    """The same rule as ``tracking`` with the expected share taken from a programme document's own
    period (eTools PD indicators run from the PD start to its end, not by calendar year)."""
    if not target or target <= 0:
        return Tracking(NO_TARGET, None)
    achieved = (value or 0) * 100 / target
    return Tracking(_status(achieved, percentage_elapsed(start, end, today)), achieved)


def year_of(reporting_year) -> int:
    """v2 stores the year as text; fall back to the current year if it does not parse."""
    try:
        return int(str(reporting_year.year or reporting_year.name)[:4])
    except (TypeError, ValueError, AttributeError):
        return datetime.date.today().year
