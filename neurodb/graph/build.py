"""The nightly knowledge run: read new or changed library and CPD documents into the knowledge base,
then rebuild the hub from every source."""

from __future__ import annotations

from neurodb.core.models import SyncRun
from neurodb.integrations.runs import fail, new_run

from . import builders, store


def run(triggered_by: str = "schedule", documents: bool = True) -> SyncRun:
    sync_run = new_run(SyncRun.Job.KNOWLEDGE_HUB, "", triggered_by)
    try:
        details = {}
        if documents:
            from neurodb.knowledge.management.commands.index_documents import run as index_documents

            details["documents"] = index_documents()
        collector, errors = builders.collect()
        counts = store.write(collector)
        sync_run.rows_in = len(collector.entities)
        sync_run.rows_written = counts["entities"]
        sync_run.rows_failed = len(errors) + details.get("documents", {}).get("failed", 0)
        details.update(counts, failed_sources=errors)
    except Exception as exc:
        return fail(sync_run, exc)
    status = SyncRun.Status.PARTIAL if sync_run.rows_failed else SyncRun.Status.SUCCEEDED
    sync_run.finish(status, **details)
    return sync_run
