"""Ask Compiler to count the education programmes, wait until it is done, then read the counts and keep
them (counts only).

The index (GET /api/figures/) comes first: it lists each programme's years, which are counted and, from
Compiler's payload format 3, which are stale (counted in an older format, or long ago). NeuroDB then asks for
the current years and for the past years that are stale or were never counted, so a past year is counted
again after a change of rule instead of keeping the count made while it was the current year. An older
Compiler ignores the targets and counts every current year, as before. Compiler runs one count at a time and
answers a request made during another one (the youth count) with that one: the sync waits for it, then asks
again once.

Payloads of formats 2, 3 and 4 are read alike (format 3 only adds keys, among them the comparison with Power
BI's view, kept apart by :func:`neurodb.education.power_bi.store`; format 4: Dirasa per year, CONTRACT_PBI §9,
its periods are years like Makani's, and the per-round rows stored before stay, hidden from the pages by
:func:`neurodb.education.power_bi.stored_periods`).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.youth.compiler import BUSY, CompilerClient, calculate_own, calculation_problem, counted_for

from . import power_bi
from .models import EducationFigures

EDUCATION_PROGRAMMES = ("mscc", "bridging")  # what NeuroDB keeps, whatever else Compiler's index lists
OLDEST_FORMAT = 2  # an older payload (no cubes) is kept, and the page asks for the newer one
MAX_TARGETS = 20  # Compiler counts at most 20 programme-years in one run


def _knows_staleness(entry: dict[str, Any]) -> bool:
    """Compiler says which years are stale (format 3 and later); an older one only says which are counted."""
    return any("stale" in y for y in entry.get("years", []))


def wanted_years(entry: dict[str, Any]) -> list[str]:
    """The current year (Compiler counts it if it has not yet) and the years before it, up to
    COMPILER_EDUCATION_YEARS in all: every listed past year when Compiler can be asked to count it (its index
    lists the newest first), else only the counted ones, as before."""
    limit = max(settings.COMPILER_EDUCATION_YEARS, 1)
    current = entry.get("current_year")
    listed = [y["year"] for y in entry.get("years", [])]
    can_count = _knows_staleness(entry)
    past = current not in listed  # the current year not listed: every listed year is a past one
    years = [current] if current else []
    for y in entry.get("years", []):
        if y["year"] == current:
            past = True
            continue
        if y.get("counted") or (can_count and past):
            years.append(y["year"])
    return years[:limit]


def needs_count(entry: dict[str, Any], year: str) -> bool:
    """A past year Compiler should count again: stale, never counted, or counted in an older format. An
    older Compiler (no ``stale`` in its index) is asked for its current years only."""
    newest = entry.get("format_version")
    for y in entry.get("years", []):
        if y["year"] == year and "stale" in y:
            older = bool(newest and y.get("format") is not None and y["format"] != newest)
            return bool(y["stale"] or not y.get("counted") or older)
    return False


def targets(index: list[dict[str, Any]]) -> list[list[str]]:
    """What to ask Compiler to count: each programme's current year, then the past years that need it."""
    current, past = [], []
    for entry in index:
        if entry.get("programme") not in EDUCATION_PROGRAMMES:
            continue
        for year in wanted_years(entry):
            if year == entry.get("current_year"):
                current.append([entry["programme"], year])
            elif needs_count(entry, year):
                past.append([entry["programme"], year])
    return (current + past)[:MAX_TARGETS]


def sync(
    triggered_by: str = "schedule", client: CompilerClient | None = None, *, calculate_first: bool = True
) -> SyncRun:
    """``calculate_first``: ask Compiler to count first (it keeps no schedule of its own): the current
    years and the past years that need it; a count that fails or takes too long is noted and the stored
    counts are read all the same."""
    run = new_run(SyncRun.Job.COMPILER_EDUCATION, "compiler", triggered_by)
    calculation = None
    try:
        client = client or CompilerClient()
        index = client.education_index()
        if calculate_first:
            wanted = targets(index)
            run.details["targets"] = wanted
            # without a target (no current year anywhere), Compiler counts every current year, as before
            asked = {"targets": wanted} if wanted else None
            calculation, other = calculate_own(client, asked, EDUCATION_PROGRAMMES, stop=run.stopped)
            if other is not None:  # the youth count was going: waited for, then asked again
                run.details["calculation_other"] = other
            run.details["calculation"] = calculation
            if calculation["status"] == "stopped":  # stopped from the admin: nothing is read
                run.finish(SyncRun.Status.FAILED)
                return run
            if not counted_for(calculation, EDUCATION_PROGRAMMES):  # still another: what Compiler has is read
                run.details["calculation_note"] = BUSY
                calculation = None
    except Exception as exc:
        return fail(run, exc)
    stored, pending, older, changed = [], [], [], []
    comparison_failed = False
    for entry in index:
        programme = entry["programme"]
        if programme not in EDUCATION_PROGRAMMES:
            continue
        for year in wanted_years(entry):
            run.rows_in += 1
            label = f"{programme} {year}"
            try:
                payload = client.education(programme, year)
                if payload is None:  # not counted yet in Compiler: next time
                    pending.append(label)
                    continue
                period = str(payload["year"])
                EducationFigures.objects.update_or_create(
                    programme=programme,
                    year=period,
                    defaults={
                        "payload": payload,
                        "fetched_at": timezone.now(),
                        "counted_at": parse_datetime(payload.get("counted_at") or ""),
                    },
                )
            except Exception as exc:
                run.rows_failed += 1
                note_error(run, label, exc)
                continue
            run.rows_written += 1
            stored.append(label)
            if (payload.get("format") or 1) < OLDEST_FORMAT:
                older.append(label)
            try:
                changed += [v for v in power_bi.store(programme, period, payload) if v not in changed]
            except Exception as exc:  # the figures are kept; only their comparison with Power BI is missing
                comparison_failed = True
                note_error(run, f"{label}: comparison with Power BI", exc)
    if changed:  # a view Power BI reads was changed in Compiler's database (Data health says when)
        run.details["power_bi_changed"] = changed
    if older:
        run.details["older_format"] = older
    problem = calculation_problem(calculation)
    return finish_by_counts(
        run, partial=bool(problem) or comparison_failed, error=problem, stored=stored, pending=pending
    )
