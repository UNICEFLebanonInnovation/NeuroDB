"""Five-field cron schedules (minute hour day-of-month month day-of-week), in the site's time zone.

Enough of cron for the scheduled jobs page: ``*``, numbers, lists (``1,15``), ranges (``1-22``) and
steps (``*/15``, ``0-30/10``); day of week 0-7 with 0 and 7 both Sunday. As in cron, when both the
day of month and the day of week are restricted, a day matching either one runs.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.conf import settings

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool  # day of month is *
    any_weekday: bool  # day of week is *

    def day_matches(self, d: datetime.date) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if base == "*":
                start, end = low, high
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(base)
                if step_text:
                    end = high
        except ValueError:
            raise CronError(f"{name}: '{part}' is not a number, a range or *") from None
        if step < 1 or start < low or end > high or start > end:
            raise CronError(f"{name}: '{part}' is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse(expression: str) -> Cron:
    parts = (expression or "").split()
    if len(parts) != 5:
        raise CronError("a schedule has five fields: minute hour day-of-month month day-of-week")
    sets = [_field(p, name, low, high) for p, (name, low, high) in zip(parts, FIELDS, strict=True)]
    weekdays = frozenset(d % 7 for d in sets[4])
    return Cron(sets[0], sets[1], sets[2], sets[3], weekdays, parts[2] == "*", parts[4] == "*")


def site_zone() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def next_after(expression: str, after: datetime.datetime) -> datetime.datetime:
    """The first time strictly after ``after`` that the schedule matches, as an aware datetime.
    The schedule is read in the site's time zone (Asia/Beirut), so it follows summer time."""
    cron = parse(expression)
    zone = site_zone()
    t = after.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0) + datetime.timedelta(minutes=1)
    limit = t + datetime.timedelta(days=366 * 5)
    while t < limit:
        if t.month not in cron.months:
            t = (t.replace(day=1) + datetime.timedelta(days=32)).replace(day=1, hour=0, minute=0)
            continue
        if not cron.day_matches(t.date()):
            t = (t + datetime.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in cron.hours:
            t = (t + datetime.timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in cron.minutes:
            t += datetime.timedelta(minutes=1)
            continue
        return t.replace(tzinfo=zone)
    raise CronError("the schedule never matches (for example 30 February)")


def describe(expression: str) -> str:
    """A short reading of common schedules; the expression itself for anything else."""
    try:
        cron = parse(expression)
    except CronError:
        return expression
    minute, hour, dom, month, dow = expression.split()
    if month != "*" or not (minute.isdigit()):
        return expression
    if hour == "*" and dom == "*" and dow == "*":
        return f"every hour at :{int(minute):02d}"
    if not hour.isdigit():
        return expression
    at = f"{int(hour):02d}:{int(minute):02d}"
    if dom == "*" and dow == "*":
        return f"daily at {at}"
    if dom == "*" and cron.any_day and len(cron.weekdays) == 1:
        return f"{DAY_NAMES[next(iter(cron.weekdays))]}s at {at}"
    if dow == "*":
        return f"at {at} on days {dom} of the month"
    return expression
