"""The Monitoring insights refresh (``manage.py fmm_refresh``), one run at a time.

Each pass is one ``SyncRun`` (job "Monitoring insights refresh") under its own database lock
(``LOCK_ID``), so a second start while one runs does nothing. A **full** pass (target "full"):

1. **Relinks programme documents**: the findings not linked to a programme document yet are matched
   by their entity's reference (``datamart.fm.relink_findings``), as the Datamart sync does.
2. **Probes the keys**: every record of the six field monitoring datasets is read (``fields.probe``);
   each key is counted with its value types and up to three redacted examples (``KeyProbe``).
3. **Chooses the keys**: each logical field gets the key that fills it (``fields.resolve_all``,
   ``FieldMapping``); an administrator's override is kept.
4. and 5. **Builds the visits** (``fmm.build``): the findings grouped into visits and linked to
   partners, programme documents, places, sections, offices, teams, action points and checklist
   answers. Records are read one at a time, and only small parsed values are kept.
6. **Scores** them: the quality rules, HACT Q1, the PSEA flag and urgency (``_score``). They need the
   question roles of the quality rules, which arrive with the rules; until then nothing is scored.
7. **Swaps** the new visits in, in one transaction: readers see the old ones until it commits, a
   visit keeps its pk while its key stays, and the reviews (``VisitReview``) are never touched.
8. **Finishes** the run with its counts: ``rows_in`` the finding rows and answer records read,
   ``rows_written`` the visits written, ``rows_failed`` the records or visits skipped by an error
   (the run then *Succeeded with errors*). A step that fails as a whole fails the run and keeps the
   previous visits.

A **scores-only** pass (target "scores", ``--scores-only``) recomputes what changes with the day or
the rules (the action point counts, then the scores) from the stored visits, without reading the
records. ``--probe-only`` (target "probe") runs steps 1-3 alone.

**Requests are never lost.** A saved rule asks for a scores-only pass and a pinned key for a full one
(:func:`request`): it stamps ``RefreshRequest`` and starts the command in the background, which does
nothing when another refresh holds the lock. So every run looks at the requests before it releases
the lock (and once more after): one newer than its last pass, or a rules version that changed while it
ran, gets another pass, up to ``FMM_REFRESH_MAX_PASSES``. The pass that serves a request clears it.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Literal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.integrations import background
from neurodb.integrations.background import FMM_REFRESH_LOCK_ID
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.watch import people

from . import build, fields, privacy
from .models import FieldMapping, QuestionAnswer, RefreshRequest, Visit, VisitActionPoint, VisitEntity

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
BATCH = 2000
VISIT_BATCH = 500  # a visit has some 70 columns: smaller batches keep the upsert's memory low
# What a scores-only pass writes on each visit
SCORE_FIELDS = (
    "hact_q1",
    "psea_flag",
    "quality_score",
    "quality_points",
    "quality_max",
    "evaluated_rules",
    "not_scored_reason",
    "score_band",
    "flags",
    "flag_count",
    "urgency",
    "urgency_band",
    "urgency_parts",
    "action_points",
    "action_points_open",
    "action_points_overdue",
    "action_points_high_open",
    "rules_version",
    "refreshed_at",
)
# What a full pass writes on a visit that exists already (everything but its pk and key)
UPSERT_FIELDS = [f.name for f in Visit._meta.concrete_fields if f.name not in ("id", "key")]
Kind = Literal["full", "scores", "probe"]


def wanted_after(runs: Iterable[SyncRun]) -> bool:
    """A Datamart sync wrote one of the datasets the refresh reads (a failed run wrote nothing)."""
    return any(run.target in SOURCE_TARGETS and run.status != SyncRun.Status.FAILED for run in runs)


def current_rules_version() -> int:
    """The version of the quality rules the scores are computed with; 0 until the rules exist."""
    return 0


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
    """One refresh: a full pass, a scores-only one or the key probe, then the passes the requests made
    meanwhile ask for (up to ``FMM_REFRESH_MAX_PASSES``); its last pass. ``None`` when Monitoring
    insights is switched off (``FMM_ENABLED``) or another run holds the lock: a request stays for
    that run."""
    if not settings.FMM_ENABLED:
        logger.info("Monitoring insights refresh: switched off (FMM_ENABLED)")
        return None
    limit = max(1, settings.FMM_REFRESH_MAX_PASSES)
    kind: Kind | None = "probe" if probe_only else "scores" if scores_only else "full"
    who = triggered_by
    passes, last, last_start = 0, None, timezone.now()
    while kind and passes < limit:
        with _locked() as got:
            if not got:
                logger.info("Monitoring insights refresh: already running, nothing done")
                return last
            while kind and passes < limit:
                started = timezone.now()
                last = _pass(kind, who, today)
                passes, last_start = passes + 1, started
                if last.status != SyncRun.Status.FAILED:
                    _served(kind, started)
                kind, who = _wanted(started, last, triggered_by)  # before the lock is released
        if passes < limit:  # a request written between that look and the release
            kind, who = _wanted(last_start, last, triggered_by)
    if kind:
        logger.warning(
            "Monitoring insights refresh: %s passes made; the request left waits for the next run", passes
        )
    return last


def run_safely(**kw) -> SyncRun | None:
    """:func:`run` that never raises: an error is recorded as a failed run instead."""
    try:
        return run(**kw)
    except Exception as exc:
        target = "probe" if kw.get("probe_only") else "scores" if kw.get("scores_only") else "full"
        return fail(new_run(SyncRun.Job.FMM_REFRESH, target, kw.get("triggered_by", "schedule")), exc)


def request(kind: Literal["scores", "full"], who: str) -> None:
    """Ask for a refresh (a saved rule: ``"scores"``; a pinned key: ``"full"``): stamp the request,
    then, once the caller's transaction commits, start ``fmm_refresh`` in the background. When another
    refresh holds the lock, that command does nothing and the running refresh serves the request."""
    if kind not in ("scores", "full"):
        raise ValueError(f"unknown refresh kind {kind!r}")
    RefreshRequest.load()
    RefreshRequest.objects.filter(pk=1).update(
        **{f"{kind}_requested_at": timezone.now()}, requested_by=(who or "")[:150]
    )
    args = ("--scores-only",) if kind == "scores" else ()
    transaction.on_commit(lambda: background.start_command("fmm_refresh", *args, "--triggered-by", who))


def _wanted(since: datetime, last: SyncRun, triggered_by: str) -> tuple[Kind | None, str]:
    """The pass the requests ask for after a pass that started at ``since``: a full one, a scores-only
    one, or none; and who asked. A rules version saved while that pass ran asks for a scores-only one."""
    row = RefreshRequest.objects.filter(pk=1).first()
    who = (row.requested_by if row else "") or triggered_by
    if row and row.full_requested_at and row.full_requested_at > since:
        return "full", who
    if row and row.scores_requested_at and row.scores_requested_at > since:
        return "scores", who
    version = (last.details or {}).get("rules_version")
    if last.status != SyncRun.Status.FAILED and version is not None and version != current_rules_version():
        return "scores", who
    return None, ""


def _served(kind: Kind, started: datetime) -> None:
    """Clear the requests made before a pass that served them started (a full pass serves both)."""
    if kind == "probe":  # reads the keys only: serves nothing
        return
    requests = RefreshRequest.objects.filter(pk=1)
    requests.filter(scores_requested_at__lte=started).update(scores_requested_at=None)
    if kind == "full":
        requests.filter(full_requested_at__lte=started).update(full_requested_at=None)


def _pass(kind: Kind, triggered_by: str, today: date | None) -> SyncRun:
    if kind == "probe":
        return _probe(triggered_by)
    today = today or timezone.localdate()
    return _full(triggered_by, today) if kind == "full" else _scores(triggered_by, today)


class _Failures:
    """The records (or visits) a run could not read, each counted once whichever step met it."""

    def __init__(self, sync_run: SyncRun) -> None:
        self.run = sync_run
        self.labels: set[str] = set()

    def __call__(self, label: str, exc: BaseException) -> None:
        note_error(self.run, label, exc)
        logger.warning("fmm_refresh: %s could not be read: %s", label, exc)
        if label not in self.labels:
            self.labels.add(label)
            self.run.rows_failed = len(self.labels)


def _probe(triggered_by: str) -> SyncRun:
    """Steps 1-3: relink, probe the keys, choose them."""
    sync_run = new_run(SyncRun.Job.FMM_REFRESH, "probe", triggered_by)
    clock = time.monotonic()
    failed = _Failures(sync_run)
    try:
        relinked, probes = _read_keys(failed)
        with transaction.atomic():
            sync_run.rows_written, mappings = _write_keys(probes)
        sync_run.rows_in = sum(p.total for p in probes.values())
        details = _details(probes, mappings, relinked)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    return finish_by_counts(sync_run, **details, duration_ms=_ms(clock))


def _read_keys(failed: _Failures) -> tuple[dict[str, int], dict[str, fields.Probe]]:
    """Steps 1 and 2: relink the findings to their programme documents; read every record's keys."""
    relinked = fm.relink_findings()
    return relinked, {dataset: fields.probe(dataset, on_error=failed) for dataset in fields.DATASETS}


def _write_keys(probes: dict[str, fields.Probe]) -> tuple[int, list[FieldMapping]]:
    """Step 3, written: the keys found (examples cleaned of every known name and of the names the
    records themselves hold) and the key chosen for each field."""
    names = privacy.names() | privacy.name_forms(v for p in probes.values() for v in p.person_values)
    written = sum(fields.write_probe(p, names) for p in probes.values())
    return written, fields.resolve_all(probes)


def _chosen_keys(probes: dict[str, fields.Probe]) -> dict[tuple[str, str], str]:
    """Step 3, in memory: the key each field would be read from (as ``fields.resolve_all`` chooses it),
    for the build; written with the visits only, so that a failed build keeps the previous keys."""
    current = {(m.dataset, m.field): m for m in FieldMapping.objects.all()}
    keys = {}
    for dataset, result in probes.items():
        figures = result.as_mapping()
        for name in fields.CANDIDATES[dataset]:
            mapping = fields.resolve(dataset, name, figures, current.get((dataset, name)))
            if mapping.state != FieldMapping.State.MISSING and mapping.chosen_key:
                keys[(dataset, name)] = mapping.chosen_key
    return keys


def _full(triggered_by: str, today: date) -> SyncRun:
    """Steps 1-8. The keys found and chosen are written with the visits, in one transaction."""
    sync_run = new_run(SyncRun.Job.FMM_REFRESH, "full", triggered_by)
    clock = time.monotonic()
    failed = _Failures(sync_run)
    try:
        relinked, probes = _read_keys(failed)
        keys = _chosen_keys(probes)
        version_used = current_rules_version()  # before any setting is read
        result = build.build_visits(build.Context(today=today, keys=keys, on_error=failed))
        if result.findings and not result.visits:  # every row failed: never swap in an empty set
            raise RuntimeError(f"none of the {result.findings} finding rows could be read")
        _score(result.visits, today, version_used)
        with transaction.atomic():
            _written, mappings = _write_keys(probes)
            sync_run.rows_written = _swap(result, version_used)
        details = _details(probes, mappings, relinked)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    sync_run.rows_in = result.findings + result.questions
    built = result.details
    questions = {**details.pop("questions"), **built.pop("questions")}
    return finish_by_counts(
        sync_run,
        **details,
        **built,
        questions=questions,
        rules_version=version_used,
        scored=sum(1 for v in result.visits if v.quality_score is not None),
        duration_ms=_ms(clock),
    )


def _scores(triggered_by: str, today: date) -> SyncRun:
    """A scores-only pass: the action point counts (they change with the day) and the scores of the
    stored visits, without reading the records."""
    sync_run = new_run(SyncRun.Job.FMM_REFRESH, "scores", triggered_by)
    clock = time.monotonic()
    try:
        version_used = current_rules_version()  # before any setting is read
        visits = list(Visit.objects.order_by("pk"))
        links: dict[int, list[build.ActionPointFacts]] = defaultdict(list)
        rows = VisitActionPoint.objects.values_list(
            "visit_id",
            "action_point_id",
            "action_point__status",
            "action_point__due_date",
            "action_point__high_priority",
        )
        for visit_id, pk, status, due, high in rows:
            links[visit_id].append(build.ActionPointFacts(pk, status or "", due, bool(high)))
        for visit in visits:
            for name, n in build.ap_counts(links.get(visit.pk, ()), today).items():
                setattr(visit, name, min(n, 32767))
        _score(visits, today, version_used)
        now = timezone.now()
        for visit in visits:
            visit.rules_version, visit.refreshed_at = version_used, now
        with transaction.atomic():
            fm.update_rows(Visit, visits, SCORE_FIELDS)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    sync_run.rows_in = sync_run.rows_written = len(visits)
    return finish_by_counts(
        sync_run,
        visits=len(visits),
        rules_version=version_used,
        scored=sum(1 for v in visits if v.quality_score is not None),
        duration_ms=_ms(clock),
    )


def _score(visits: list[Visit], today: date, version: int) -> None:
    """Step 6: the quality rules, HACT Q1, the PSEA flag and urgency of each visit. They need the
    question roles that come with the quality rules; until those exist, the visits are not scored."""


def copy_rows(model, rows: Iterable) -> int:
    """Insert new ``rows`` (unsaved model instances whose pk is not needed) with PostgreSQL's COPY, much
    faster than ``bulk_create`` for the tens of thousands of checklist answers; ``bulk_create``
    elsewhere."""
    if connection.vendor != "postgresql":
        rows = list(rows)
        model.objects.bulk_create(rows, batch_size=BATCH)
        return len(rows)
    fields = [f for f in model._meta.concrete_fields if not f.primary_key]
    quote = connection.ops.quote_name
    sql = "COPY {} ({}) FROM STDIN".format(
        quote(model._meta.db_table), ", ".join(quote(f.column) for f in fields)
    )
    written = 0
    with connection.cursor() as cursor, cursor.cursor.copy(sql) as copy:
        for row in rows:
            row._prepare_related_fields_for_save(operation_name="copy_rows")
            copy.write_row([f.get_db_prep_save(getattr(row, f.attname), connection) for f in fields])
            written += 1
    return written


def _swap(result: build.BuildResult, version: int) -> int:
    """Step 7: the new visits in place of the old ones, in one transaction. A visit whose key stays
    keeps its pk; its rows, answers and action point links are replaced; the reviews stay."""
    visits = result.visits
    for visit in visits:
        visit.rules_version = version
    with transaction.atomic():
        QuestionAnswer.objects.all().delete()
        VisitActionPoint.objects.all().delete()
        VisitEntity.objects.all().delete()
        for start in range(0, len(visits), VISIT_BATCH):
            Visit.objects.bulk_create(
                visits[start : start + VISIT_BATCH],
                update_conflicts=True,
                unique_fields=["key"],
                update_fields=UPSERT_FIELDS,
            )
        Visit.objects.exclude(key__in=[v.key for v in visits]).delete()
        VisitEntity.objects.bulk_create(result.entities, batch_size=BATCH)
        copy_rows(QuestionAnswer, result.question_answers({v.key: v for v in visits}))
        VisitActionPoint.objects.bulk_create(result.links, batch_size=BATCH)
        transaction.on_commit(people.forget)  # the team names, read again by the next look-up
    return len(visits)


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
