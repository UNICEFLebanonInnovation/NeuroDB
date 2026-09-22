"""Shared bits for the integration commands: summaries via ``self.stdout``, exit codes."""

from __future__ import annotations

from collections.abc import Iterable

from django.core.management.base import BaseCommand, CommandError

from neurodb.core.models import SyncRun


def triggered_by(options: dict) -> str:
    return options.get("triggered_by") or "command"


def write_summary(command: BaseCommand, runs: Iterable[SyncRun]) -> list[SyncRun]:
    runs = list(runs)
    for run in runs:
        line = (
            f"{run.get_job_display()} [{run.target}] {run.status}: in={run.rows_in} "
            f"written={run.rows_written} failed={run.rows_failed}"
        )
        style = command.style.ERROR if run.status == SyncRun.Status.FAILED else command.style.SUCCESS
        command.stdout.write(style(line))
        if run.error:
            command.stdout.write(f"  error: {run.error[:300]}")
    return runs


def exit_on_failure(runs: Iterable[SyncRun]) -> None:
    failed = [run for run in runs if run.status == SyncRun.Status.FAILED]
    if failed:
        names = ", ".join(f"{run.job}[{run.target}]" for run in failed)
        raise CommandError(f"{len(failed)} run(s) failed: {names}")


def add_triggered_by(parser) -> None:
    parser.add_argument("--triggered-by", dest="triggered_by", default="command", help="recorded on the SyncRun")
