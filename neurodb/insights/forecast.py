"""Year-end forecast of each ActivityInfo master indicator: where it will likely stand in December,
with a range, compared with its target.

How it works (no black box; everything is in this file):

1. **History.** Every additive (SUM) master indicator of every year is read month by month (the same
   monthly sums as the dashboards). The same indicator is recognised across years by its section and
   its AWP code or name.
2. **Pattern.** For each past year, the share of the year's total reached by the end of each month
   (by March, 18%; by June, 41%…). An indicator's pattern is the average of its own past years,
   blended with the pattern of its section (and, without either, of all indicators).
3. **Forecast.** Value to date ÷ the share usually reached by now = the likely year-end value.
   Only settled months count: a month is used once 30 days have passed since it ended (most late
   reports and corrections arrive by then).
4. **Range.** How far such forecasts landed from the real year-end in past years, at the same
   month, gives the range (5th to 95th percentile of the past errors).
5. **Back-test.** Every past year is forecast again, month by month, using only what was known then,
   and compared with what happened and with the simple straight-line projection (value to date ×
   12 ÷ months). Forecasts are shown only when the method beats the straight line and its ranges
   held the real result often enough.

Status against the target: *on course* when even the low end reaches it, *likely to fall short* when
even the high end does not, *uncertain* in between.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SETTLE_DAYS = 30  # a month is used once this many days have passed since it ended
MIN_SHARE = 0.05  # too early to forecast before the usual share of the year reaches 5%
LOW_Q, HIGH_Q = 0.05, 0.95
POOL_WEIGHT = 1.0  # the section's pattern counts as this many years of the indicator's own history
MIN_ERRORS = 30  # past errors needed at a month to give a range from them
MAX_DEPTH = 3  # years of own history: 3 or more are alike
GATE_MONTH = 6  # the back-test month that decides whether forecasts are shown
GATE_COVERAGE = 0.70  # ranges must hold the real result this often
CHECK_MONTHS = (3, 6, 9)

ON_COURSE, UNCERTAIN, SHORT, NO_REPORTS, TOO_EARLY, NO_TARGET = (
    "on_course",
    "uncertain",
    "likely_short",
    "no_reports",
    "too_early",
    "no_target",
)
STATUS_LABELS = {
    ON_COURSE: "On course",
    UNCERTAIN: "Uncertain",
    SHORT: "Likely to fall short",
    NO_REPORTS: "Nothing reported yet",
    TOO_EARLY: "Too early to tell",
    NO_TARGET: "No target",
}


def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").lower()).split())


@dataclass
class Instance:
    """One master indicator in one year, with its 12 monthly values."""

    master_id: int
    year: int
    section: int | None
    name: str
    code: str
    target: float | None
    months: list[float]  # index 0 = January
    database_id: int | None = None

    @property
    def total(self) -> float:
        return sum(self.months)

    def to_date(self, month: int) -> float:
        return sum(self.months[:month])

    def keys(self) -> set[tuple]:
        out = {("name", self.section, _norm(self.name))}
        if _norm(self.code):
            out.add(("code", self.section, _norm(self.code)))
        return out


def curve(months: list[float]) -> list[float] | None:
    """Share of the year's total reached by the end of each month (None when the year is empty)."""
    total = sum(months)
    if total <= 0:
        return None
    out, running = [], 0.0
    for v in months:
        running += v
        out.append(running / total)
    return out


def _mean_curve(curves: list[list[float]]) -> list[float] | None:
    return [statistics.fmean(c[i] for c in curves) for i in range(12)] if curves else None


LINEAR = [(i + 1) / 12 for i in range(12)]


@dataclass
class History:
    """The past patterns: per indicator (by its keys), per section and overall, before a given year."""

    instances: list[Instance]

    def __post_init__(self) -> None:
        self._pooled: dict[tuple, tuple[list[float] | None, str]] = {}
        self.by_key: dict[tuple, list[Instance]] = defaultdict(list)
        for inst in self.instances:
            for key in inst.keys():
                self.by_key[key].append(inst)

    def own(self, inst: Instance, before: int) -> list[list[float]]:
        seen, curves = set(), []
        for key in inst.keys():
            for past in self.by_key.get(key, []):
                if past.year < before and (past.master_id, past.year) not in seen:
                    seen.add((past.master_id, past.year))
                    c = curve(past.months)
                    if c:
                        curves.append(c)
        return curves

    def pooled(self, section: int | None, before: int) -> tuple[list[float] | None, str]:
        key = (section, before)
        if key not in self._pooled:
            self._pooled[key] = self._pool(section, before)
        return self._pooled[key]

    def _pool(self, section: int | None, before: int) -> tuple[list[float] | None, str]:
        same = [
            c
            for p in self.instances
            if p.year < before and p.section == section and section is not None
            for c in [curve(p.months)]
            if c
        ]
        if len(same) >= 5:
            return _median_curve(same), "its section"
        every = [c for p in self.instances if p.year < before for c in [curve(p.months)] if c]
        if every:
            return _median_curve(every), "all indicators"
        return None, ""

    def profile(self, inst: Instance, before: int) -> tuple[list[float], str, int]:
        """(pattern, what it is based on, years of the indicator's own history)."""
        own = self.own(inst, before)
        pool, pool_label = self.pooled(inst.section, before)
        if not own and pool is None:
            return LINEAR, "straight line (no history)", 0
        if not own:
            return pool, f"the pattern of {pool_label}", 0
        mean = _mean_curve(own)
        if pool is None:
            return mean, f"its own {len(own)} past year(s)", len(own)
        k = len(own)
        blended = [(k * mean[i] + POOL_WEIGHT * pool[i]) / (k + POOL_WEIGHT) for i in range(12)]
        return blended, f"its own {k} past year(s) and {pool_label}", k


def _median_curve(curves: list[list[float]]) -> list[float]:
    return [statistics.median(c[i] for c in curves) for i in range(12)]


def point(value_to_date: float, share: float) -> float | None:
    return value_to_date / share if share >= MIN_SHARE else None


def linear(value_to_date: float, month: int) -> float:
    return value_to_date * 12 / month


@dataclass
class Errors:
    """Past forecast errors, ln(actual ÷ forecast), by month and by years of the indicator's own history
    (0, 1, 2, 3 or more): five years of history forecast better than one."""

    by_month: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    by_month_kind: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))

    def add(self, month: int, depth: int, error: float) -> None:
        self.by_month[month].append(error)
        self.by_month_kind[(month, depth)].append(error)

    def band(self, month: int, depth: int) -> tuple[float, float] | None:
        errors = self.by_month_kind.get((month, depth)) or []
        if len(errors) < MIN_ERRORS:
            errors = self.by_month.get(month) or []
        if len(errors) < MIN_ERRORS:
            return None
        ordered = sorted(errors)
        return _quantile(ordered, LOW_Q), _quantile(ordered, HIGH_Q)


def _quantile(ordered: list[float], q: float) -> float:
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def status(low: float | None, high: float | None, target: float | None) -> str:
    if not target:
        return NO_TARGET
    if low is None or high is None:
        return UNCERTAIN
    if low >= target:
        return ON_COURSE
    if high < target:
        return SHORT
    return UNCERTAIN


# ------------------------------------------------------------------------------------ back-test
@dataclass
class Case:
    year: int
    month: int
    depth: int  # years of own history, capped at MAX_DEPTH
    actual: float
    forecast: float
    linear: float
    target: float | None


def cases(instances: list[Instance], history: History) -> list[Case]:
    """Every past indicator, forecast again at the end of each month with what was known then."""
    out = []
    for inst in instances:
        actual = inst.total
        if actual <= 0:
            continue
        pattern, _, own_years = history.profile(inst, before=inst.year)
        for month in range(1, 12):
            to_date = inst.to_date(month)
            if to_date <= 0:
                continue
            f = point(to_date, pattern[month - 1])
            if f is None or f <= 0:
                continue
            out.append(
                Case(
                    inst.year,
                    month,
                    min(own_years, MAX_DEPTH),
                    actual,
                    f,
                    linear(to_date, month),
                    inst.target,
                )
            )
    return out


def backtest(all_cases: list[Case]) -> dict[str, Any]:
    """Accuracy at months 3, 6 and 9, with ranges taken from the *other* years' errors."""
    years = sorted({c.year for c in all_cases})
    errors_without: dict[int, Errors] = {}
    for year in years:
        e = Errors()
        for c in all_cases:
            if c.year != year:
                e.add(c.month, c.depth, math.log(c.actual / c.forecast))
        errors_without[year] = e
    by_month = {}
    for month in CHECK_MONTHS:
        rows = [c for c in all_cases if c.month == month]
        if not rows:
            continue
        model_err = [abs(c.forecast - c.actual) / c.actual for c in rows]
        linear_err = [abs(c.linear - c.actual) / c.actual for c in rows]
        held, ranged = 0, 0
        missed = flagged = correct_flags = 0
        linear_flagged = linear_correct = 0
        for c in rows:
            band = errors_without[c.year].band(month, c.depth)
            low = high = None
            if band:
                low, high = c.forecast * math.exp(band[0]), c.forecast * math.exp(band[1])
                ranged += 1
                held += low <= c.actual <= high
            if c.target:
                fell_short = c.actual < c.target
                said_short = status(low, high, c.target) == SHORT
                missed += fell_short
                flagged += said_short
                correct_flags += said_short and fell_short
                linear_says = c.linear < c.target
                linear_flagged += linear_says
                linear_correct += linear_says and fell_short
        by_month[month] = {
            "cases": len(rows),
            "error": round(statistics.median(model_err), 3),
            "linear_error": round(statistics.median(linear_err), 3),
            "range_held": round(held / ranged, 3) if ranged else None,
            "short_flagged_right": round(correct_flags / flagged, 3) if flagged else None,
            "short_caught": round(correct_flags / missed, 3) if missed else None,
            "linear_short_flagged_right": round(linear_correct / linear_flagged, 3)
            if linear_flagged
            else None,
            "linear_short_caught": round(linear_correct / missed, 3) if missed else None,
        }
    gate = by_month.get(GATE_MONTH)
    shown = bool(
        gate
        and gate["error"] <= gate["linear_error"]
        and gate["range_held"] is not None
        and gate["range_held"] >= GATE_COVERAGE
    )
    return {"years": years, "by_month": by_month, "shown": shown}


# ---------------------------------------------------------------------------------- forecasts
def settled_month(year: int, today: datetime.date) -> int:
    """The last month of ``year`` that ended at least SETTLE_DAYS ago (0: none yet, 12: year over)."""
    cutoff = today - datetime.timedelta(days=SETTLE_DAYS)
    if cutoff.year > year:
        return 12
    if cutoff.year < year:
        return 0
    first_of_month = cutoff.replace(day=1)
    last_day = (first_of_month + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
    return cutoff.month if cutoff == last_day else cutoff.month - 1


@dataclass
class Forecast:
    inst: Instance
    as_of: int
    to_date: float
    forecast: float | None
    low: float | None
    high: float | None
    linear: float | None
    status: str
    basis: str
    own_years: int


def forecast_year(current: list[Instance], history: History, errors: Errors, as_of: int) -> list[Forecast]:
    out = []
    for inst in current:
        to_date = inst.to_date(as_of)
        pattern, basis, own_years = history.profile(inst, before=inst.year)
        share = pattern[as_of - 1] if as_of else 0
        if as_of == 0 or share < MIN_SHARE:
            out.append(Forecast(inst, as_of, to_date, None, None, None, None, TOO_EARLY, basis, own_years))
            continue
        if to_date <= 0:
            out.append(Forecast(inst, as_of, 0, None, None, None, None, NO_REPORTS, basis, own_years))
            continue
        f = point(to_date, share)
        band = errors.band(as_of, min(own_years, MAX_DEPTH))
        low = high = None
        if f is not None and band:
            low, high = max(f * math.exp(band[0]), to_date), f * math.exp(band[1])
        out.append(
            Forecast(
                inst,
                as_of,
                to_date,
                f,
                low,
                high,
                linear(to_date, as_of),
                status(low, high, inst.target),
                basis,
                own_years,
            )
        )
    return out


# ---------------------------------------------------------------------------------- the data
def load(years: Iterable[int] | None = None) -> list[Instance]:
    """Every active additive master indicator with its monthly sums, one query per database."""
    from django.db.models import Q

    from neurodb.facts.queries import master_monthly_values
    from neurodb.facts.services.dashboard import fact_filter
    from neurodb.indicators.models import Database, MasterIndicator

    wanted = {str(y) for y in years} if years else None
    databases = Database.objects.exclude(reporting_year=None).select_related("reporting_year")
    out = []
    for db in databases:
        year = str(db.reporting_year.year or db.reporting_year.name or "")[:4]
        if not year.isdigit() or (wanted and year not in wanted):
            continue
        masters = list(
            MasterIndicator.objects.filter(database=db, is_active=True)
            .filter(Q(aggregation_method__in=("SUM", "")) | Q(aggregation_method__isnull=True))
            .values("id", "name", "awp_code", "awp_target")
        )  # an average, a maximum or a ratio does not add up month by month
        if not masters:
            continue
        try:
            monthly = master_monthly_values(fact_filter(db))
        except Exception:  # one database that cannot be read does not stop the others
            logger.exception("forecast: monthly values of database %s failed", db.pk)
            continue
        for m in masters:
            by_month = monthly.get(m["id"], {})
            out.append(
                Instance(
                    master_id=m["id"],
                    year=int(year),
                    section=db.section_id,
                    name=m["name"] or "",
                    code=m["awp_code"] or "",
                    target=float(m["awp_target"]) if m["awp_target"] else None,
                    months=[float(by_month.get(f"{i:02d}", 0) or 0) for i in range(1, 13)],
                    database_id=db.pk,
                )
            )
    return out


def run(today: datetime.date | None = None) -> dict[str, Any]:
    """Back-test on the past years, then forecast the current reporting year."""
    from neurodb.indicators.services.navigation import current_year

    today = today or datetime.date.today()
    reporting_year = current_year()
    year = int(str(reporting_year.year or reporting_year.name)[:4]) if reporting_year else today.year
    instances = load()
    past = [i for i in instances if i.year < year]
    current = [i for i in instances if i.year == year]
    history = History(past)
    all_cases = cases(past, history)
    result = backtest(all_cases)
    errors = Errors()
    for c in all_cases:
        errors.add(c.month, c.depth, math.log(c.actual / c.forecast))
    as_of = settled_month(year, today)
    return {
        "year": year,
        "as_of": as_of,
        "backtest": result,
        "forecasts": forecast_year(current, history, errors, as_of) if as_of < 12 else [],
        "indicators_history": len(past),
    }
