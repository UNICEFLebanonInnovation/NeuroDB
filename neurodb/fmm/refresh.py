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
6. **Scores** them (``fmm.score``): the question roles, HACT Q1 and the PSEA flag, then each record
   (an entity row: FMS's model scores each one) with the quality rules and the AI checks' answers kept
   for its inputs, its score, band, flags and urgency, and each visit from its records (the mean, the
   lowest, the most urgent), with the rules as they are when the pass starts (the rules version is
   read first and stamped on every visit). The AI checks made per visit before records are carried
   over to the records first (``ai.checks.carry_over``, while any is left).
7. **Swaps** the new visits in, in one transaction: readers see the old ones until it commits, a
   visit keeps its pk while its key stays, its records, answers, action point links and rule results
   (the records' and the visits') are replaced, and the reviews (``VisitReview``) are never touched.
   The AI check answers no scoring has read for 120 days are deleted.
8. **Action points**: a NeuroDB action point is made for each visit with a scored record of Low
   quality that the AI flagged for its action points (R7, R8 or R32,
   ``action_points.create_automatic``), and the AI reviews of eTools action points that changed are
   deleted (``ai.ap_review.forget_stale``).
9. **Finishes** the run with its counts: ``rows_in`` the finding rows and answer records read,
   ``rows_written`` the visits written, ``rows_failed`` the records or visits skipped by an error
   (the run then *Succeeded with errors*). A step that fails as a whole fails the run and keeps the
   previous visits.

A **scores-only** pass (target "scores", ``--scores-only``) recomputes what changes with the day or
the rules (the action point counts, the question roles, HACT Q1, PSEA, the rule results, the scores
and urgency of the records and the visits) from the stored visits and the narratives, without reading
the eTools records; it writes them in one transaction, then makes the NeuroDB action points of step 8.
``--probe-only`` (target "probe") runs steps 1-3 alone.

**Requests are never lost.** A saved rule asks for a scores-only pass and a pinned key for a full one
(:func:`request`): it stamps ``RefreshRequest`` and starts the command in the background, which does
nothing when another refresh holds the lock. So every run looks at the requests before it releases
the lock (and once more after): one newer than its last pass or that pass did not serve (a full
request after a scores-only pass), or a rules version that changed while it ran, gets another pass, up
to ``FMM_REFRESH_MAX_PASSES``. The pass that serves a request clears it; a request is stamped again
when the save that made it is committed, so a pass that read the settings before the commit never
clears it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Literal

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.integrations import background
from neurodb.integrations.background import FMM_REFRESH_LOCK_ID
from neurodb.integrations.runs import fail, finish_by_counts, new_run, note_error
from neurodb.watch import people

from . import build, fields, privacy, score, versions
from .models import (
    AICheckAnswer,
    FieldMapping,
    QuestionAnswer,
    RecordRuleResult,
    RefreshRequest,
    ScoreSetting,
    Visit,
    VisitActionPoint,
    VisitEntity,
    VisitRuleResult,
)

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
ANSWER_DAYS = 120  # an AI check answer no scoring has read for this long is deleted
# What a scores-only pass writes on each visit
SCORE_FIELDS = (
    "hact_q1",
    "psea_flag",
    "quality_score",
    "provisional_score",
    "ai_pending",
    "category_deductions",
    "quality_points",
    "quality_max",
    "lowest_score",
    "records_scored",
    "records_low",
    "evaluated_rules",
    "not_scored_reason",
    "score_band",
    "flags",
    "flag_count",
    "urgency",
    "urgency_band",
    "urgency_parts",
    "signals",
    "action_points",
    "action_points_open",
    "action_points_overdue",
    "action_points_high_open",
    "rules_version",
    "refreshed_at",
)
# What a scores-only pass writes on each record: the effective HACT Q1 and the record's score fields
ENTITY_SCORE_FIELDS = ("hact_q1", "hact_q1_from", *score.RECORD_SCORE_FIELDS)
# What a full pass writes on a visit that exists already (everything but its pk and key)
UPSERT_FIELDS = [f.name for f in Visit._meta.concrete_fields if f.name not in ("id", "key")]
Kind = Literal["full", "scores", "probe"]


def wanted_after(runs: Iterable[SyncRun]) -> bool:
    """A Datamart sync wrote one of the datasets the refresh reads (a failed run wrote nothing)."""
    return any(run.target in SOURCE_TARGETS and run.status != SyncRun.Status.FAILED for run in runs)


def current_rules_version() -> int:
    """The version of the quality rules the scores are computed with (``versions``)."""
    return versions.current_rules_version()


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
    stamped = f"{kind}_requested_at"
    RefreshRequest.load()
    RefreshRequest.objects.filter(pk=1).update(**{stamped: timezone.now()}, requested_by=(who or "")[:150])
    args = ("--scores-only",) if kind == "scores" else ()

    def start() -> None:
        # Stamped again once the save is committed: a pass that started before the commit read the
        # old settings, and may already have cleared the first stamp as served. This one is newer than
        # that pass's start, so it is served after it.
        RefreshRequest.objects.filter(pk=1).update(**{stamped: timezone.now()})
        background.start_command("fmm_refresh", *args, "--triggered-by", who)

    transaction.on_commit(start)


def _wanted(since: datetime, last: SyncRun, triggered_by: str) -> tuple[Kind | None, str]:
    """The pass the requests ask for after a pass that started at ``since``: a full one, a scores-only
    one, or none; and who asked. A request is waiting when it is newer than that pass's start, or when
    that pass did not fail and did not serve it (a scores-only pass or the key probe leaves a full
    request, made before it started, whose own command found the lock held). A rules version saved
    while that pass ran asks for a scores-only one."""
    row = RefreshRequest.objects.filter(pk=1).first()
    who = (row.requested_by if row else "") or triggered_by
    # a pass that did not fail cleared what it served: what is left of the requests is waiting
    finished = last.status != SyncRun.Status.FAILED

    def waiting(at: datetime | None) -> bool:
        return at is not None and (at > since or finished)

    if row and waiting(row.full_requested_at):
        return "full", who
    if row and waiting(row.scores_requested_at):
        return "scores", who
    version = (last.details or {}).get("rules_version")
    if finished and version is not None and version != current_rules_version():
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
        # read before any setting, the keys pinned with the rules included: a save landing later in the
        # pass is then newer than this version, and gets a pass of its own
        version_used = current_rules_version()
        relinked, probes = _read_keys(failed)
        keys = _chosen_keys(probes)
        result = build.build_visits(build.Context(today=today, keys=keys, on_error=failed))
        if result.findings and not result.visits:  # every row failed: never swap in an empty set
            raise RuntimeError(f"none of the {result.findings} finding rows could be read")
        source = score.BuiltSource(result, keys)
        scored = _score(result.visits, today, version_used, source=source, on_error=failed)
        with transaction.atomic():
            _written, mappings = _write_keys(probes)
            sync_run.rows_written = _swap(result, version_used, scored)
            forgotten = _forget_checks(today)
        details = _details(probes, mappings, relinked)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    followed = _action_points(sync_run, today, full=True)
    sync_run.rows_in = result.findings + result.questions
    built = result.details
    scoring = dict(scored.details)
    questions = {**details.pop("questions"), **built.pop("questions"), **scoring.pop("questions", {})}
    return finish_by_counts(
        sync_run,
        **details,
        **built,
        **scoring,
        questions=questions,
        rules_version=version_used,
        scored=sum(1 for v in result.visits if v.quality_score is not None),
        ai_answers_forgotten=forgotten,
        **followed,
        duration_ms=_ms(clock),
    )


def _scores(triggered_by: str, today: date) -> SyncRun:
    """A scores-only pass: the action point counts (they change with the day) and the scores of the
    stored visits, without reading the records."""
    sync_run = new_run(SyncRun.Job.FMM_REFRESH, "scores", triggered_by)
    clock = time.monotonic()
    failed = _Failures(sync_run)
    try:
        version_used = current_rules_version()  # before any setting is read
        source = score.StoredSource()
        visits = source.visits
        for visit in visits:
            for name, n in build.ap_counts(source.links.get(visit.key, ()), today).items():
                setattr(visit, name, min(n, 32767))
        before = {entity.pk: _entity_scores(entity) for rows in source.entities.values() for entity in rows}
        scored = _score(visits, today, version_used, source=source, on_error=failed)
        now = timezone.now()
        for visit in visits:
            visit.rules_version, visit.refreshed_at = version_used, now
        with transaction.atomic():
            fm.update_rows(Visit, visits, SCORE_FIELDS)
            changed = [
                entity
                for rows in source.entities.values()
                for entity in rows
                if _entity_scores(entity) != before.get(entity.pk)
            ]
            fm.update_rows(VisitEntity, changed, ENTITY_SCORE_FIELDS)
            _write_roles(scored.roles)
            update_results(RecordRuleResult, scored.record_results)
            update_results(VisitRuleResult, scored.results)
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=_ms(clock))
    followed = _action_points(sync_run, today, full=False)
    sync_run.rows_in = sync_run.rows_written = len(visits)
    scoring = dict(scored.details)
    return finish_by_counts(
        sync_run,
        visits=len(visits),
        **scoring,
        rules_version=version_used,
        scored=sum(1 for v in visits if v.quality_score is not None),
        **followed,
        duration_ms=_ms(clock),
    )


def _action_points(sync_run: SyncRun, today: date, *, full: bool) -> dict[str, int]:
    """After the scores: the NeuroDB action points of the Low visits the AI flagged for their action
    points (``action_points.create_automatic``) and, after a full pass (a Datamart sync brings changed
    action points), the AI reviews of action points that are out of date deleted
    (``ai.ap_review.forget_stale``). A failure here is noted and never fails the refresh."""
    from . import action_points
    from .ai import ap_review

    out = {}
    try:
        out["local_action_points_made"] = action_points.create_automatic(today)
        if full:
            out["ap_reviews_out_of_date"] = ap_review.forget_stale()
    except Exception as exc:  # the visits are written: the next pass makes them
        note_error(sync_run, "action points", exc)
        logger.warning("fmm_refresh: the action points step failed: %s", exc)
    return out


def _score(
    visits: list[Visit],
    today: date,
    version: int,
    *,
    source: score.Source | None = None,
    on_error=None,
) -> score.Scored:
    """Step 6: the question roles, HACT Q1, the PSEA flag, the quality rules, the score and urgency of
    each record and visit (``score.score_visits``), with the rules and settings as they are now
    (``version`` was read before them). The visits and their records are updated in place; the rule
    results and the roles are returned to be written. The AI checks made per visit before records are
    carried over first (:func:`_carry_over`)."""
    source = source if source is not None else score.StoredSource(visits)
    book = score.Rulebook.load()
    carried = _carry_over(book, on_error)
    scored = score.score_visits(source, book, today, on_error=on_error)
    if carried:
        scored.details["ai_checks_carried"] = carried
    if isinstance(source, score.BuiltSource):
        source.set_roles(scored.roles)
    return scored


def _carry_over(book: score.Rulebook, on_error=None) -> dict[str, int]:
    """The AI checks made per visit before records (``VisitAICheck``) carried over to the records'
    answers before a scoring (``ai.checks.carry_over``; nothing to do once none is left). The refresh
    does it too, not only the AI checks job: the first refresh after the change to records then scores
    the single-record visits with the verdicts they had, and those verdicts keep counting while the AI
    is switched off or paused (the job does not run then), as the answers kept always do. A failure is
    noted and never stops the refresh (the next pass or the job tries again)."""
    from .models import VisitAICheck

    if not VisitAICheck.objects.exists():
        return {}
    from .ai import checks

    try:
        return checks.carry_over(book)
    except Exception as exc:
        if on_error is not None:
            on_error("AI checks carry-over", exc)
        logger.warning("fmm_refresh: the AI checks made per visit were not carried over: %s", exc)
        return {}


def _entity_scores(entity: VisitEntity) -> tuple:
    return tuple(getattr(entity, name) for name in ENTITY_SCORE_FIELDS)


def _write_roles(roles: dict[tuple[str, bool | None], str]) -> None:
    """The role of every stored answer, one update per question whose role changed."""
    current = QuestionAnswer.objects.order_by().values_list("question_text", "is_hact", "role").distinct()
    for text, is_hact, role in list(current):
        wanted = roles.get((text, is_hact), "")
        if role != wanted:
            QuestionAnswer.objects.filter(question_text=text, is_hact=is_hact, role=role).update(role=wanted)


def copy_rows(model, rows: Iterable, *, with_pk: bool = False, table: str = "") -> int:
    """Insert new ``rows`` (unsaved model instances whose pk is not needed, or set already: ``with_pk``)
    with PostgreSQL's COPY, much faster than ``bulk_create`` for the tens of thousands of checklist
    answers (into ``table`` when given, a table of the same columns); ``bulk_create`` elsewhere."""
    if connection.vendor != "postgresql":
        rows = list(rows)
        model.objects.bulk_create(rows, batch_size=BATCH)
        return len(rows)
    fields = [f for f in model._meta.concrete_fields if with_pk or not f.primary_key]
    quote = connection.ops.quote_name
    sql = "COPY {} ({}) FROM STDIN".format(
        quote(table or model._meta.db_table), ", ".join(quote(f.column) for f in fields)
    )
    written = 0
    with connection.cursor() as cursor, cursor.cursor.copy(sql) as copy:
        for row in rows:
            row._prepare_related_fields_for_save(operation_name="copy_rows")
            copy.write_row([f.get_db_prep_save(getattr(row, f.attname), connection) for f in fields])
            written += 1
    return written


RESULT_FIELDS = ("rule", "status", "points", "max_points", "detail_key", "detail", "measure")


def _result_columns(model) -> tuple[str, list[str]]:
    """(the owner field, "entity" or "visit"; the columns of a rule result, the owner's first)."""
    owner = "entity" if model is RecordRuleResult else "visit"
    meta = model._meta
    columns = [meta.get_field(owner).column, *(meta.get_field(name).column for name in RESULT_FIELDS)]
    if {f.column for f in meta.concrete_fields if not f.primary_key} != set(columns):
        raise RuntimeError(f"{meta.label}: its columns are not those of a rule result")
    return owner, columns


def copy_results(model, rows: Iterable[score.ResultRow], table: str = "") -> int:
    """Insert rule results (``RecordRuleResult`` or ``VisitRuleResult``; into ``table`` when given, a
    table of the same columns) from the scoring's rows (``score.ResultRow``: the record or visit first,
    saved already) with PostgreSQL's COPY, straight from the rows: no model instance is made for the
    180,000 results of a large refresh."""
    owner, columns = _result_columns(model)
    if connection.vendor != "postgresql":
        return copy_rows(model, score.result_models(model, owner, rows))
    meta = model._meta
    quote = connection.ops.quote_name
    sql = "COPY {} ({}) FROM STDIN".format(
        quote(table or meta.db_table), ", ".join(quote(c) for c in columns)
    )
    written = 0
    with connection.cursor() as cursor, cursor.cursor.copy(sql) as copy:
        for target, *values in rows:
            if target.pk is None:
                raise ValueError(f"{meta.label}: a result of an unsaved {owner}")
            copy.write_row([target.pk, *values])
            written += 1
    return written


SCRATCH = "fmm_scratch"  # the temporary table a write goes through


def _scratch(cursor, table: str, names: str) -> bool:
    """A temporary table of the columns ``names`` of ``table`` (both quoted), named :data:`SCRATCH` and
    dropped at the commit. False where the database user may not create one: the caller then takes
    the slower way."""
    try:
        with transaction.atomic():
            cursor.execute(f"DROP TABLE IF EXISTS {SCRATCH}")
            cursor.execute(
                f"CREATE TEMPORARY TABLE {SCRATCH} ON COMMIT DROP AS SELECT {names} FROM {table} WITH NO DATA"  # noqa: S608
            )
    except DatabaseError as exc:
        logger.warning("fmm_refresh: no temporary table (%s); the slower way is taken", exc)
        return False
    return True


def update_results(model, rows: Iterable[score.ResultRow]) -> int:
    """A scores-only pass: the stored rule results made those of the scoring by writing only what
    changed (most results of a day are those of the day before): the new rows are copied into a
    temporary table, the stored rows that differ from theirs (or have none) are deleted and the new
    rows missing then are inserted. Returns the rows inserted."""
    _owner, columns = _result_columns(model)
    quote = connection.ops.quote_name
    table = quote(model._meta.db_table)
    names = ", ".join(quote(c) for c in columns)
    key = " AND ".join(f"n.{quote(c)} = r.{quote(c)}" for c in columns[:2])
    same = " AND ".join(f"n.{quote(c)} IS NOT DISTINCT FROM r.{quote(c)}" for c in columns[2:])
    with transaction.atomic(), connection.cursor() as cursor:
        if connection.vendor != "postgresql" or not _scratch(cursor, table, names):
            model.objects.all().delete()
            return copy_results(model, rows)
        copy_results(model, rows, table=SCRATCH)
        cursor.execute(f"ANALYZE {SCRATCH}")
        cursor.execute(
            f"DELETE FROM {table} AS r WHERE NOT EXISTS "  # noqa: S608
            f"(SELECT 1 FROM {SCRATCH} AS n WHERE {key} AND {same})"
        )
        cursor.execute(
            f"INSERT INTO {table} ({names}) SELECT {names} FROM {SCRATCH} AS n "  # noqa: S608
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS r WHERE {key})"
        )
        inserted = cursor.rowcount
        cursor.execute(f"DROP TABLE {SCRATCH}")
    return inserted


def _swap(result: build.BuildResult, version: int, scored: score.Scored | None = None) -> int:
    """Step 7: the new visits in place of the old ones, in one transaction. A visit whose key stays
    keeps its pk; its records, answers, action point links and rule results are replaced (the records
    are inserted first, under pks drawn from their sequence, which their answers and rule results then
    point at); the reviews stay."""
    visits = result.visits
    for visit in visits:
        visit.rules_version = version
    with transaction.atomic():
        RecordRuleResult.objects.all().delete()
        VisitRuleResult.objects.all().delete()
        QuestionAnswer.objects.all().delete()
        VisitActionPoint.objects.all().delete()
        # nothing points at a record any more: they are deleted in one statement, never loaded (Django
        # would read the 15,000 records of a large refresh into memory to look for what points at them)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(VisitEntity._meta.db_table)}")  # noqa: S608
        _upsert_visits(visits)
        Visit.objects.exclude(key__in=[v.key for v in visits]).delete()
        _insert_records(result.entities)
        copy_rows(QuestionAnswer, result.question_answers({v.key: v for v in visits}))
        VisitActionPoint.objects.bulk_create(result.links, batch_size=BATCH)
        if scored is not None:
            copy_results(RecordRuleResult, scored.record_results)
            copy_results(VisitRuleResult, scored.results)
        transaction.on_commit(people.forget)  # the team names, read again by the next look-up
    return len(visits)


def _upsert_visits(visits: list[Visit]) -> None:
    """The visits written over the stored ones of the same key (a visit keeps its pk; a new one is
    inserted), through a temporary table filled with COPY; their pks are read back by key. Django's
    INSERT ... ON CONFLICT of 5,000 rows of some eighty columns took 10 seconds to build and send."""
    meta = Visit._meta
    quote = connection.ops.quote_name
    table = quote(meta.db_table)
    names = ", ".join(quote(f.column) for f in meta.concrete_fields if not f.primary_key)
    key, pk = quote(meta.get_field("key").column), quote(meta.pk.column)
    columns = [quote(meta.get_field(name).column) for name in UPSERT_FIELDS]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    with transaction.atomic(), connection.cursor() as cursor:
        if connection.vendor != "postgresql" or not _scratch(cursor, table, names):
            for start in range(0, len(visits), VISIT_BATCH):
                Visit.objects.bulk_create(
                    visits[start : start + VISIT_BATCH],
                    update_conflicts=True,
                    unique_fields=["key"],
                    update_fields=UPSERT_FIELDS,
                )
            return
        copy_rows(Visit, visits, table=SCRATCH)
        cursor.execute(
            f"INSERT INTO {table} ({names}) SELECT {names} FROM {SCRATCH} "  # noqa: S608
            f"ON CONFLICT ({key}) DO UPDATE SET {updates} RETURNING {key}, {pk}"
        )
        pks = dict(cursor.fetchall())
        cursor.execute(f"DROP TABLE {SCRATCH}")
    for visit in visits:
        visit.pk = pks[visit.key]
        visit._state.adding, visit._state.db = False, connection.alias


def _insert_records(entities: list[VisitEntity]) -> None:
    """The records, inserted with COPY under pks drawn first from their table's sequence (their rule
    results point at them): an INSERT of 15,000 rows of some fifty columns took ten times as long."""
    if connection.vendor != "postgresql" or not entities:
        VisitEntity.objects.bulk_create(entities, batch_size=BATCH)
        return
    meta = VisitEntity._meta
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence(%s, %s)) FROM generate_series(1, %s)",
            [meta.db_table, meta.pk.column, len(entities)],
        )
        pks = [row[0] for row in cursor.fetchall()]
    for entity, pk in zip(entities, pks, strict=True):
        entity.pk = pk
        entity._state.adding, entity._state.db = False, connection.alias
    copy_rows(VisitEntity, entities, with_pk=True)


def _forget_checks(today: date) -> int:
    """The AI check answers no scoring has read for ``ANSWER_DAYS`` (120) days are deleted (an answer in
    use is marked used once a day by ``ai.checks.fresh``), while the AI checks are on: switched off,
    every answer is kept for when they are back. The checks made per visit before records, of visits
    gone from eTools, are deleted too. Returns the answers deleted."""
    from .models import VisitAICheck

    VisitAICheck.objects.exclude(visit_key__in=Visit.objects.values("key")).delete()
    if not ScoreSetting.load().ai_checks:
        return 0
    cutoff = today - timedelta(days=ANSWER_DAYS)
    return AICheckAnswer.objects.filter(last_used__lt=cutoff).delete()[0]


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
