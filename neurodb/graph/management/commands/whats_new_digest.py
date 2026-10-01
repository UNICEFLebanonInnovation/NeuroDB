"""``whats_new_digest``: write today's what's new notes (everyone and per section) and email them to
the people who asked for them."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.core.models import SyncRun
from neurodb.graph import digest
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import fail, new_run


def run(triggered_by: str = "schedule") -> SyncRun:
    sync_run = new_run(SyncRun.Job.WHATS_NEW, "", triggered_by)
    try:
        notes = digest.write()
        emailed = digest.send(notes)
    except Exception as exc:
        return fail(sync_run, exc)
    sync_run.rows_written = len(notes)
    sync_run.finish(
        SyncRun.Status.SUCCEEDED,
        notes=[d.section_name or "all sections" for d in notes],
        emailed=emailed,
        written_by=sorted({d.written_by for d in notes}),
    )
    return sync_run


class Command(BaseCommand):
    help = "Write today's what's new notes and email them to the people who asked"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        exit_on_failure(write_summary(self, [run(options["triggered_by"])]))
