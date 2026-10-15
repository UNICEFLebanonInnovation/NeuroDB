"""What makes a value this app keeps per worker stale on the worker that changed its input.

Connected by :meth:`neurodb.indicators.apps.IndicatorsConfig.ready`.

- **The ActivityInfo state** (:mod:`neurodb.indicators.services.activityinfo`): a reporting year or a database
  saved or deleted, and a scheduled job switched on or off, edited or deleted, drop the state at once on the
  worker that made the change (the others follow within 30 seconds). The scheduler's own saves
  (``update_fields`` holding only its bookkeeping: the next run, the last start, the outcome) change nothing
  the state reads and are ignored. The bulk "Switch off the selected jobs" action sends no signal: the action
  drops the state itself, as do the setting's admin page and the end of an ActivityInfo import.
- **The menu's sources** (:func:`neurodb.indicators.services.navigation.sources`): the inputs an administrator
  edits by hand, the document review's settings (switched on or off) and the country programme cycles
  (uploaded, ticked current, changed or deleted), drop them at once on the worker that made the change (the
  others follow within two minutes). What the jobs store (Compiler figures, document review findings,
  forecasts) is written by the job's own process, whose cache no web worker reads: those follow within two
  minutes.
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


def _drop_menu_sources(**kwargs) -> None:
    from neurodb.indicators.services import navigation

    # only the sources: the menu's other parts do not read these rows (and a page that makes the document
    # review's settings row on first read pays no query for it)
    navigation.invalidate_sources()


def connect() -> None:
    from neurodb.core.models import ScheduledJob
    from neurodb.cpd.models import CountryProgramme
    from neurodb.indicators.models import Database, ReportingYear
    from neurodb.knowledge.models import DocumentReviewSettings

    post_save.connect(_scheduled_job_saved, sender=ScheduledJob, dispatch_uid="activityinfo-state-job-saved")
    post_delete.connect(_invalidate, sender=ScheduledJob, dispatch_uid="activityinfo-state-job-deleted")
    for model in (ReportingYear, Database):
        name = model._meta.model_name
        post_save.connect(_invalidate, sender=model, dispatch_uid=f"activityinfo-state-{name}-saved")
        post_delete.connect(_invalidate, sender=model, dispatch_uid=f"activityinfo-state-{name}-deleted")
    for model in (DocumentReviewSettings, CountryProgramme):
        name = model._meta.model_name
        post_save.connect(_drop_menu_sources, sender=model, dispatch_uid=f"nav-sources-{name}-saved")
        post_delete.connect(_drop_menu_sources, sender=model, dispatch_uid=f"nav-sources-{name}-deleted")
