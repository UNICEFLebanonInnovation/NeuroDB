"""Read the education programmes' counts from Compiler and keep them (counts only)."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.youth.compiler import CompilerClient

from .models import EducationFigures


def wanted_years(entry: dict) -> list[str]:
    """The current year (Compiler counts it if it has not yet) and the counted years before it, up to
    COMPILER_EDUCATION_YEARS in all."""
    limit = max(settings.COMPILER_EDUCATION_YEARS, 1)
    current = entry.get("current_year")
    years = [current] if current else []
    years += [y["year"] for y in entry.get("years", []) if y.get("counted") and y["year"] != current]
    return years[:limit]


def sync(triggered_by: str = "schedule", client: CompilerClient | None = None) -> SyncRun:
    run = new_run(SyncRun.Job.COMPILER_EDUCATION, "compiler", triggered_by)
    try:
        client = client or CompilerClient()
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
                if payload is None:  # Compiler is counting it in the background: next time
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
    return finish_by_counts(run, stored=stored, pending=pending)
