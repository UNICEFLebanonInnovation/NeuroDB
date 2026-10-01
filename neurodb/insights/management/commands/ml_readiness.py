"""``ml_readiness``: measure whether each source is ready for machine learning and record the result
(shown on the Data health page)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.core.models import SyncRun
from neurodb.insights.readiness import measure
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import fail, new_run


def run(triggered_by: str = "schedule") -> SyncRun:
    sync_run = new_run(SyncRun.Job.ML_READINESS, "", triggered_by)
    try:
        result = measure()
    except Exception as exc:
        return fail(sync_run, exc)
    failed = [s["key"] for s in result["sections"] if s["error"]]
    sync_run.rows_written = sum(len(s["metrics"]) for s in result["sections"])
    sync_run.rows_failed = len(failed)
    status = SyncRun.Status.PARTIAL if failed else SyncRun.Status.SUCCEEDED
    sync_run.finish(status, readiness=result)
    return sync_run


class Command(BaseCommand):
    help = "Measure whether the data is ready for machine learning, per programme decision"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = run(options["triggered_by"])
        for v in result.details.get("readiness", {}).get("verdicts", []):
            self.stdout.write(f"{v['status_label']:>13}  {v['question']}")
            for c in v["checks"]:
                mark = "ok " if c["ok"] else ("?  " if c["ok"] is None else "no ")
                self.stdout.write(f"{'':>15}{mark}{c['text']}")
        exit_on_failure(write_summary(self, [result]))
