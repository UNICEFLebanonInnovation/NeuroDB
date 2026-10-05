"""The Monitoring insights refresh (``manage.py fmm_refresh``), one run at a time.

Each run is one ``SyncRun`` (job "Monitoring insights refresh") under its own database lock
(``LOCK_ID``), so a second start while one runs does nothing. So far the refresh has its first three
steps, run with ``--probe-only`` (target "probe"):

1. **Relink programme documents**: the findings not linked to a programme document yet are matched
   by their entity's reference (``datamart.fm.relink_findings``), as the Datamart sync does.
2. **Probe the keys**: every record of the six field monitoring datasets is read (``fields.probe``);
   each key is counted with its value types and up to three redacted examples (``KeyProbe``).
3. **Choose the keys**: each logical field gets the key that fills it (``fields.resolve_all``,
   ``FieldMapping``); an administrator's override is kept.

The probe's keys and choices are written together in one transaction: a failure keeps the previous
ones and marks the run *Failed*. A record that cannot be read is counted in ``rows_failed`` (the run
then *Succeeded with errors*), and the others are read. The run's details hold what the admin's
"Fields found" shows above the keys: the activity id coverage, the programme document link rate,
the share of checklist answers given, the fields not found, and the rating, status and entity type
values seen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.integrations.background import FMM_REFRESH_LOCK_ID
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error

from . import fields, privacy
from .models import FieldMapping

logger = logging.getLogger(__name__)

LOCK_ID = FMM_REFRESH_LOCK_ID  # 7_140_432
SOURCE_TARGETS = frozenset(
    {
        "locations",
        "location_sites",
        "partners",
        "interventions",
        "action_points",
        "field_monitoring",
        "fm_questions",
        "fm_options",
        "fm_programme_activities",
        "hact_history",
        "planned_visits",
        "offices",
        "sections",
    }
)  # keys of datamart_sync.ENTITY_SYNCS / catalogue.DOCUMENTS
ACTIVITY_ID_TARGET = 0.95  # below this share of rows with an activity id, Fields found warns
VALUES_SEEN = 50  # distinct rating, status and entity type values kept in the run details
VALUE_CHARS = 60


def wanted_after(runs: Iterable[SyncRun]) -> bool:
    """A Datamart sync wrote one of the datasets the refresh reads (a failed run wrote nothing)."""
    return any(run.target in SOURCE_TARGETS and run.status != SyncRun.Status.FAILED for run in runs)


@contextmanager
def _locked() -> Iterator[bool]:
    """Take the refresh's lock without waiting: False when another run holds it."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def run(
    *,
    triggered_by: str = "schedule",
    scores_only: bool = False,
    probe_only: bool = False,
    today: date | None = None,
) -> SyncRun | None:
    """One refresh. ``None`` when Monitoring insights is switched off (``FMM_ENABLED``) or another run
    holds the lock. Only the key probe (``probe_only``) exists so far."""
    if not settings.FMM_ENABLED:
        logger.info("Monitoring insights refresh: switched off (FMM_ENABLED)")
        return None
    if not probe_only:
        raise NotImplementedError(
            "Only the key probe of the Monitoring insights refresh exists (probe_only)."
        )
    with _locked() as got:
        if not got:
            logger.info("Monitoring insights refresh: already running, nothing done")
            return None
        return _probe(triggered_by)


def run_safely(**kw) -> SyncRun | None:
    """:func:`run` that never raises: an error is recorded as a failed run instead."""
    try:
        return run(**kw)
    except Exception as exc:
        target = "probe" if kw.get("probe_only") else "scores" if kw.get("scores_only") else "full"
        return fail(new_run(SyncRun.Job.FMM_REFRESH, target, kw.get("triggered_by", "schedule")), exc)


def _probe(triggered_by: str) -> SyncRun:
    """Steps 1-3: relink, probe the keys, choose them."""
    sync_run = new_run(SyncRun.Job.FMM_REFRESH, "probe", triggered_by)
    clock = time.monotonic()

    def failed(label: str, exc: BaseException) -> None:
        sync_run.rows_failed += 1
        note_error(sync_run, label, exc)
        logger.warning("fmm_refresh: %s could not be read: %s", label, exc)

    try:
        relinked = fm.relink_findings()
        probes = {dataset: fields.probe(dataset, on_error=failed) for dataset in fields.DATASETS}
        sync_run.rows_in = sum(p.total for p in probes.values())
        names = privacy.names() | privacy.name_forms(v for p in probes.values() for v in p.person_values)
        with transaction.atomic():
            sync_run.rows_written = sum(fields.write_probe(p, names) for p in probes.values())
            mappings = fields.resolve_all(probes)
        details = _details(probes, mappings, relinked)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    return finish_by_counts(sync_run, **details, duration_ms=_ms(clock))


def _ms(clock: float) -> int:
    return round((time.monotonic() - clock) * 1000)


# ------------------------------------------------------------------------------------------ details
def _details(probes: dict[str, fields.Probe], mappings: list[FieldMapping], relinked: dict) -> dict:
    keys = {
        (m.dataset, m.field): (m.chosen_key or None)
        for m in mappings
        if m.state != FieldMapping.State.MISSING
    }
    answer_keys = {field: keys.get(("fm_questions", field)) for field in fields.ANSWER_FIELDS}
    return {
        "datasets": {
            name: {
                "records": p.total,
                "keys": len(p.records),
                "keys_not_kept": p.keys_dropped,
                "failed": p.failed,
            }
            for name, p in probes.items()
        },
        "pd_relinked": relinked,
        "pd_resolved": pd_resolved(),
        "activity_ids": activity_ids(),
        "questions": probes["fm_questions"].answer_counts(answer_keys),
        "fields_not_found": [
            f"{m.dataset}.{m.field}" for m in mappings if m.state == FieldMapping.State.MISSING
        ],
        "fields_ambiguous": [
            f"{m.dataset}.{m.field}" for m in mappings if m.state == FieldMapping.State.AMBIGUOUS
        ],
        "overrides_missing": [
            f"{m.dataset}.{m.field}" for m in mappings if m.state == FieldMapping.State.OVERRIDE_MISSING
        ],
        **values_seen(),
    }


def pd_resolved() -> dict[str, int]:
    """How the findings about a programme document were linked to one: exact, token, base, title or
    unresolved (``pd_kind_rows`` in all), over every finding."""
    from neurodb.datamart.models import MonitoringFinding

    counts = dict.fromkeys(("exact", "token", "base", "title", "unresolved", "pd_kind_rows"), 0)
    rows = (
        MonitoringFinding.objects.order_by()
        .values_list("entity_type", "entity", "pd_match")
        .annotate(n=Count("pk"))
    )
    for entity_type, entity, how, n in rows:
        if fm.entity_kind(entity_type, entity) != "pd":
            continue
        counts["pd_kind_rows"] += n
        counts[how if how in counts else "unresolved"] += n
    return counts


def activity_ids() -> dict[str, float | int]:
    """The share of findings that carry an eTools activity id (visits otherwise fall back to their
    activity reference)."""
    from neurodb.datamart.models import MonitoringFinding

    found = MonitoringFinding.objects.aggregate(
        rows=Count("pk"), with_id=Count("pk", filter=Q(monitoring_activity_id__gt=0))
    )
    rows, with_id = found["rows"], found["with_id"]
    return {"rows": rows, "with_id": with_id, "share": round(with_id / rows, 4) if rows else None}


def values_seen() -> dict:
    """The rating, status and entity type values the findings hold, each with the code NeuroDB reads it
    as and its rows; and the ratings and statuses it does not recognise. Vocabulary, never free text."""
    from neurodb.datamart.models import MonitoringFinding

    def seen(field: str, code) -> list[list]:
        rows = (
            MonitoringFinding.objects.order_by()
            .values_list(field)
            .annotate(n=Count("pk"))
            .order_by("-n", field)[:VALUES_SEEN]
        )
        return [[str(raw or "")[:VALUE_CHARS], code(raw), n] for raw, n in rows]

    ratings = seen("overall_finding_rating", fm.normalize_rating)
    statuses = seen("status", fm.normalize_status)
    kinds = seen("entity_type", lambda raw: fm.entity_kind(raw))
    return {
        "ratings_seen": ratings,
        "statuses_seen": statuses,
        "entity_types_seen": kinds,
        "ratings_unknown": {raw: n for raw, code, n in ratings if code == "other"},
        "statuses_unknown": {raw: n for raw, code, n in statuses if raw and not code},
    }
