"""The site's calendar: today in Beirut, this year, the years a page offers, and Home's "since" window.

One seam for the date: every page and job that asks "what is today" reads :func:`today` (or
``timezone.localdate()``, which it wraps), never ``date.today()``. The tests travel the clock once for the
whole suite (``tests/conftest.py``, ``site_today``), so ``timezone.now()``, ``timezone.localdate()``,
``date.today()`` and :func:`today` always agree there.

The site year is the calendar year in Beirut. It no longer depends on which ActivityInfo reporting year is
ticked current: :func:`number_of` reads a reporting year's calendar year where a page still needs one.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.core.cache import cache
from django.utils import timezone

FIRST_YEAR = 2000  # a year before this one is not a year NeuroDB has data for
YEARS_SHOWN = 10  # years a year menu lists at most
YEARS_KEY = "years:v1:{}"  # + this year: the list follows the calendar (per worker, locmem)
YEARS_SECONDS = 600


def today() -> datetime.date:
    """Today in Beirut (``TIME_ZONE``): the one date every page reads."""
    return timezone.localdate()


def this_year() -> int:
    """This calendar year in Beirut."""
    return today().year


def parse(raw: Any) -> int | None:
    """A year written in a query (``?year=2026``): its first four characters are digits and it lies between
    2000 and next year. None otherwise (empty, ``all``, a typo, a year far away)."""
    text = str(raw or "").strip()[:4]
    if len(text) != 4 or not (text.isascii() and text.isdigit()):
        return None
    number = int(text)
    return number if FIRST_YEAR <= number <= this_year() + 1 else None


def resolve(raw: Any) -> int:
    """The year a query asks for (:func:`parse`), else this year."""
    return parse(raw) or this_year()


def number_of(reporting_year: Any) -> int | None:
    """The calendar year of an ActivityInfo reporting year: the first four digits of its ``year``, else of
    its ``name``; None when neither starts with four digits (or there is no reporting year)."""
    if reporting_year is None:
        return None
    for raw in (getattr(reporting_year, "year", None), getattr(reporting_year, "name", None)):
        text = str(raw or "").strip()[:4]
        if len(text) == 4 and text.isascii() and text.isdigit():
            return int(text)
    return None


def available() -> list[int]:
    """The years a year menu offers, newest first: this year down to the first year NeuroDB has data for
    (the first ActivityInfo year with records, or the year the oldest programme document started), at
    most ten. Kept 10 minutes per worker; the key holds this year, so 1 January starts a new list."""
    year = this_year()
    key = YEARS_KEY.format(year)
    cached = cache.get(key)
    if cached is not None:
        return list(cached)
    from django.db.models import Min

    from neurodb.indicators.services import activityinfo
    from neurodb.partnerships.models import PCA

    starts = [activityinfo.state().first, PCA.objects.aggregate(first=Min("start"))["first"]]
    known = [s if isinstance(s, int) else s.year for s in starts if s is not None]
    first = max(min([*known, year]), year - YEARS_SHOWN + 1)
    years = list(range(year, first - 1, -1))
    cache.set(key, years, YEARS_SECONDS)
    return years


def since_working_day() -> datetime.datetime:
    """00:00 in Beirut of the previous working day (Monday to Friday; no holiday calendar): Tuesday to
    Friday look back to the day before, Saturday, Sunday and Monday to Friday. Home's "Since {weekday}"
    window, so a Monday morning still sees what Friday's syncs brought."""
    day = today() - datetime.timedelta(days=1)
    while day.weekday() >= 5:  # Saturday or Sunday
        day -= datetime.timedelta(days=1)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
