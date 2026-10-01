"""A build of the knowledge hub: read new or changed library and CPD documents into the knowledge
base (the morning run), then rebuild the hub from every source and record what changed."""

from __future__ import annotations

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, new_run

from . import builders, refresh, store
from .models import RefreshRequest


def run(triggered_by: str = "schedule", documents: bool = True) -> SyncRun:
    with refresh.lock(wait=True):
        if triggered_by == "new data":
            triggered_by = f"new data: {', '.join(refresh.reasons())}"[:150]
        sync_run = new_run(SyncRun.Job.KNOWLEDGE_HUB, "", triggered_by)
        try:
            details = {}
            if documents:
                from neurodb.knowledge.management.commands.index_documents import run as index_documents

                details["documents"] = index_documents()
            answered = refresh.latest()  # the requests this build answers
            collector, errors = builders.collect()
            counts = store.write(collector, run=sync_run, failed=errors)
            if answered is not None:
                RefreshRequest.objects.filter(pk__lte=answered).delete()
            sync_run.rows_in = len(collector.entities)
            sync_run.rows_written = counts["entities"]
            sync_run.rows_failed = len(errors) + details.get("documents", {}).get("failed", 0)
            details.update(counts, failed_sources=errors)
        except Exception as exc:
            return fail(sync_run, exc)
        status = SyncRun.Status.PARTIAL if sync_run.rows_failed else SyncRun.Status.SUCCEEDED
        sync_run.finish(status, **details)
        return sync_run
