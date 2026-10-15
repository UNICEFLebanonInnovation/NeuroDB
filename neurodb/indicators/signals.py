"""What makes the ActivityInfo state (:mod:`neurodb.indicators.services.activityinfo`) stale on this worker.

Connected by :meth:`neurodb.indicators.apps.IndicatorsConfig.ready`. A reporting year or a database saved or
deleted, and a scheduled job switched on or off, edited or deleted, drop the state at once on the worker that
made the change (the others follow within 30 seconds). The scheduler's own saves (``update_fields`` holding
only its bookkeeping: the next run, the last start, the outcome) change nothing the state reads and are
ignored. The bulk "Switch off the selected jobs" action sends no signal: the action drops the state itself,
as do the setting's admin page and the end of an ActivityInfo import.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save

STATE_FIELDS = frozenset({"enabled", "command"})  # what the state reads from a scheduled job


def _invalidate(**kwargs) -> None:
    from neurodb.indicators.services import activityinfo

    activityinfo.invalidate()


def _scheduled_job_saved(sender, instance, update_fields=None, **kwargs) -> None:
    if update_fields is not None and not STATE_FIELDS & set(update_fields):
        return  # the scheduler's bookkeeping
    _invalidate()


def connect() -> None:
    from neurodb.core.models import ScheduledJob
    from neurodb.indicators.models import Database, ReportingYear

    post_save.connect(_scheduled_job_saved, sender=ScheduledJob, dispatch_uid="activityinfo-state-job-saved")
    post_delete.connect(_invalidate, sender=ScheduledJob, dispatch_uid="activityinfo-state-job-deleted")
    for model in (ReportingYear, Database):
        name = model._meta.model_name
        post_save.connect(_invalidate, sender=model, dispatch_uid=f"activityinfo-state-{name}-saved")
        post_delete.connect(_invalidate, sender=model, dispatch_uid=f"activityinfo-state-{name}-deleted")
