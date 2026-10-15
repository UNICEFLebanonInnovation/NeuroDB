"""Ask Compiler to count the youth figures, read them, keep them, and suggest links to eTools indicators.

Compiler counts the youth figures at night like the education ones (payload format 2): NeuroDB asks for its
current year, the years before it and all years together (POST /api/figures/runs/, programme ``youth``),
waits, then reads them from the youth API, which answers the stored count when it is fresh and counts on the
spot otherwise (a year; all years together are only ever read from a count). An older Compiler does not count
youth there: it answers that the programme is unknown, and the youth API counts on the spot, as before.
Compiler runs one count at a time and answers a request made during another one (the education count) with
that one: the sync waits for it, then asks again once. Payloads of format 1 and 2 are kept alike (format 2
only adds keys, among them the comparison with Power BI's view, kept apart by ``education.power_bi.store``).

The figures of all years together (the year ``YouthFigures.ALL``) are kept in a table of their own,
``YouthAllYears``, never among the years: an earlier release reads every ``YouthFigures`` row as a year, so
going back to it must not make "all" its latest year. They have no targets and no links to eTools (both are
per year).
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.education import power_bi
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error

from . import linking
from .compiler import BUSY, CompilerClient, calculate_own, calculation_problem, counted_for
from .figures import Figures
from .models import YouthFigures, figures_model

PROGRAMME = "youth"  # Compiler's programme key for the youth count


def _years_back(head: str) -> list[str]:
    """The ``COMPILER_YOUTH_YEARS - 1`` years before ``head`` (a year such as "2026")."""
    head = str(head)[:4]
    if not head.isdigit():
        return []
    return [str(int(head) - back) for back in range(1, max(settings.COMPILER_YOUTH_YEARS, 1))]


def targets() -> list[list[str | None]]:
    """The youth count NeuroDB asks for: Compiler's current year (None), the years before it that NeuroDB
    already keeps (so Compiler has them: a year it does not have would refuse the whole count), and all years
    together. The latest year kept is taken for the current one: on the first night of a new year, the year
    before it is read as Compiler counts it on the spot."""
    known = list(YouthFigures.objects.yearly().order_by("-year").values_list("year", flat=True))
    past = [y for y in _years_back(known[0]) if y in known] if known else []
    return [[PROGRAMME, None], *([PROGRAMME, y] for y in past), [PROGRAMME, YouthFigures.ALL]]


def sync(
    triggered_by: str = "schedule", client: CompilerClient | None = None, *, calculate_first: bool = True
) -> SyncRun:
    """This year's figures, the ``COMPILER_YOUTH_YEARS - 1`` years before it and all years together.
    ``calculate_first``: ask Compiler to count them first; a count that fails or takes too long is noted
    and the figures are read all the same."""
    run = new_run(SyncRun.Job.COMPILER_YOUTH, "compiler", triggered_by)
    calculation = None
    try:
        client = client or CompilerClient()
        if calculate_first:
            asked = {"programme": PROGRAMME, "targets": targets()}
            calculation, other = calculate_own(client, asked, (PROGRAMME,), stop=run.stopped)
            if other is not None:  # the education count was going: waited for, then asked again
                run.details["calculation_other"] = other
            run.details["calculation"] = calculation
            if calculation["status"] == "stopped":  # stopped from the admin: nothing is read
                run.finish(SyncRun.Status.FAILED)
                return run
            if not counted_for(calculation, (PROGRAMME,)):  # still another one: the youth API counts a year
                run.details["calculation_note"] = BUSY  # on the spot; all years keep their last count
                calculation = None
        current = client.figures()
        payloads = [(str(current["year"]), current)]
        for year in _years_back(current["year"]):
            older = client.figures(year)
            if older is not None:
                payloads.append((str(older["year"]), older))
    except Exception as exc:
        return fail(run, exc)
    every_year_failed = False
    try:
        every_year = client.figures(YouthFigures.ALL)  # None from an older Compiler (no such year)
    except Exception as exc:
        every_year_failed = True
        note_error(run, YouthFigures.ALL, exc)
    else:
        if every_year is not None:
            payloads.append((YouthFigures.ALL, every_year))
    years, links, changed = [], {}, []
    comparison_failed = False
    for year, payload in payloads:
        run.rows_in += 1
        try:
            if year == YouthFigures.ALL and str(payload.get("year")) != YouthFigures.ALL:
                raise ValueError(f"Compiler answered the year {payload.get('year')!r} for all years")
            figures_model(year).objects.update_or_create(  # "all" apart from the years (YouthAllYears)
                year=year, defaults={"fetched_at": timezone.now(), "payload": payload}
            )
            if year != YouthFigures.ALL:  # links are per year (programme documents and targets)
                links[year] = linking.suggest(Figures(payload))
        except Exception as exc:
            run.rows_failed += 1
            note_error(run, year, exc)
            continue
        run.rows_written += 1
        years.append(year)
        try:
            changed += [v for v in power_bi.store(PROGRAMME, year, payload) if v not in changed]
        except Exception as exc:  # the figures are kept; only their comparison with Power BI is missing
            comparison_failed = True
            note_error(run, f"{year}: comparison with Power BI", exc)
    if changed:  # a view Power BI reads was changed in Compiler's database (Data health says when)
        run.details["power_bi_changed"] = changed
    problem = calculation_problem(calculation)
    partial = bool(problem) or comparison_failed or every_year_failed
    return finish_by_counts(run, partial=partial, error=problem, years=years, links=links)
