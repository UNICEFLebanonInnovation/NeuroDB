"""Ask Compiler to count the education programmes, wait until it is done, then read the counts and keep
them (counts only)."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.youth.compiler import CompilerClient, calculate, calculation_problem

from .models import EducationFigures


def wanted_years(entry: dict) -> list[str]:
    """The current year (Compiler counts it if it has not yet) and the counted years before it, up to
    COMPILER_EDUCATION_YEARS in all."""
    limit = max(settings.COMPILER_EDUCATION_YEARS, 1)
    current = entry.get("current_year")
    years = [current] if current else []
    years += [y["year"] for y in entry.get("years", []) if y.get("counted") and y["year"] != current]
    return years[:limit]


def sync(
    triggered_by: str = "schedule", client: CompilerClient | None = None, *, calculate_first: bool = True
) -> SyncRun:
    """``calculate_first``: ask Compiler to count every programme's current year first (it keeps no
    schedule of its own); a count that fails or takes too long is noted and the stored counts are
    read all the same."""
    run = new_run(SyncRun.Job.COMPILER_EDUCATION, "compiler", triggered_by)
    calculation = None
    try:
        client = client or CompilerClient()
        if calculate_first:
            calculation = calculate(client, "education")
            run.details["calculation"] = calculation
        index = client.education_index()
    except Exception as exc:
        return fail(run, exc)
    stored, pending = [], []
    for entry in index:
        programme = entry["programme"]
        for year in wanted_years(entry):
            run.rows_in += 1
            label = f"{programme} {year}"
            try:
                payload = client.education(programme, year)
                if payload is None:  # not counted yet in Compiler: next time
                    pending.append(label)
                    continue
                EducationFigures.objects.update_or_create(
                    programme=programme,
                    year=str(payload["year"]),
                    defaults={
                        "payload": payload,
                        "fetched_at": timezone.now(),
                        "counted_at": parse_datetime(payload.get("counted_at") or ""),
                    },
                )
            except Exception as exc:
                run.rows_failed += 1
                note_error(run, label, exc)
            else:
                run.rows_written += 1
                stored.append(label)
    problem = calculation_problem(calculation)
    return finish_by_counts(run, partial=bool(problem), error=problem, stored=stored, pending=pending)
