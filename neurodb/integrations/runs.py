"""Helpers around ``SyncRun`` shared by every job (new in v3: v2 kept no record of its jobs)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from django.db import transaction

from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

T = TypeVar("T")
ERROR_CHARS = 2000


def new_run(job: str, target: str = "", triggered_by: str = "schedule") -> SyncRun:
    return SyncRun.objects.create(job=job, target=target, triggered_by=triggered_by)


def describe_error(exc: BaseException) -> str:
    """``"TypeName: message"`` capped for the ``error`` column (never a token: clients strip them)."""
    return f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]


def finish_by_counts(run: SyncRun, **details: Any) -> SyncRun:
    """SUCCEEDED when nothing failed, PARTIAL otherwise."""
    status = SyncRun.Status.PARTIAL if run.rows_failed else SyncRun.Status.SUCCEEDED
    run.finish(status, **details)
    return run


def fail(run: SyncRun, exc: BaseException, **details: Any) -> SyncRun:
    logger.exception("%s %s failed", run.job, run.target)
    run.finish(SyncRun.Status.FAILED, error=describe_error(exc), **details)
    return run


def process_items(
    run: SyncRun,
    items: Iterable[T],
    handler: Callable[[T], None],
    label: Callable[[T], str],
) -> None:
    """Apply ``handler`` to each item inside its own savepoint, capturing per-item failures.

    v2 wrapped whole loops in ``try/except: print`` (or nothing), so one bad item aborted or
    silently skipped the rest. Here each failure is logged with the item id, counted in
    ``rows_failed`` and the loop continues; the run ends PARTIAL.
    """
    for item in items:
        run.rows_in += 1
        try:
            with transaction.atomic():
                handler(item)
        except Exception as exc:
            run.rows_failed += 1
            logger.warning("%s %s: item %s failed: %s", run.job, run.target, label(item), describe_error(exc))
        else:
            run.rows_written += 1
