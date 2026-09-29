"""Read the youth figures from Compiler, keep them, and suggest links to eTools indicators."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error

from . import linking
from .compiler import CompilerClient
from .figures import Figures
from .models import YouthFigures


def sync(triggered_by: str = "schedule", client: CompilerClient | None = None) -> SyncRun:
    """This year's figures and the ``COMPILER_YOUTH_YEARS - 1`` years before it."""
    run = new_run(SyncRun.Job.COMPILER_YOUTH, "compiler", triggered_by)
    try:
        client = client or CompilerClient()
        current = client.figures()
        payloads = [current]
        head = str(current["year"])[:4]
        if head.isdigit():
            for back in range(1, max(settings.COMPILER_YOUTH_YEARS, 1)):
                older = client.figures(str(int(head) - back))
                if older is not None:
                    payloads.append(older)
    except Exception as exc:
        return fail(run, exc)
    years, links = [], {}
    for payload in payloads:
        run.rows_in += 1
        try:
            YouthFigures.objects.update_or_create(
                year=str(payload["year"]), defaults={"fetched_at": timezone.now(), "payload": payload}
            )
            links[str(payload["year"])] = linking.suggest(Figures(payload))
        except Exception as exc:
            run.rows_failed += 1
            note_error(run, str(payload.get("year")), exc)
        else:
            run.rows_written += 1
            years.append(str(payload["year"]))
    return finish_by_counts(run, years=years, links=links)
