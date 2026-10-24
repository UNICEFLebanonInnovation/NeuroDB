"""What each eTools Datamart sync received, and what became of it: the header of every source on Raw eTools
data (Admin → Data and sync), in the words of Data health's datasets table (``reports.services.quality_row``,
the same row of the same run).

The run the records come from is the latest that succeeded, or succeeded with errors, and received something
(no ``empty_response``), as ``datamart_sync.last_details`` chooses; a newer run that failed is named beside
it. Counted on distinct records, the same for the datasets in their own table and those kept whole:

    received − of another country − named no country − could not be saved = updated by the sync
    in NeuroDB = updated by the sync + updated since (a later sync under way) + kept from an earlier sync

``other_country_skipped`` already holds the records that named no country (``unnamed_skipped``) and those of
another business area a filter let through (``area_strays``): "of another country" is shown without the first,
the second as an "of which". Records sent twice are a note, each already counted once in what NeuroDB
received. A remainder the counts do not explain is shown, never hidden. For a source written by another sync
(``WRITTEN_BY``) the equation counts its raw copies; the records that could not be saved into the partners,
programme documents, locations... the pages read (the run's ``rows_failed``) are a line of their own, and make
"Complete?" No. An error is shown by its kind only (``runs._error_key``): its text can hold a value of the
record. Counts only: no record's value is read here.

An eTools REST entity's records come from the latest run that kept its raw copy: of the job "Read the eTools
REST API into Raw eTools data" (one run per entity), or of the eTools REST sync (its ``details.raw_copies``);
for a list, the latest that received something (a list eTools answered empty removes nothing from the copy).
The same equation holds, with nothing of another country: received − could not be saved = updated by the sync.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.db import connection

from neurodb.core.models import SyncRun

from . import catalogue

# Sources written by another sync: their records also update the partners, programme documents, agreements,
# engagements, locations or sites, and a raw copy is kept of each one (``datamart_sync._write_documents``)
WRITTEN_BY = frozenset(name for name, spec in catalogue.DOCUMENTS.items() if spec.scope == "written_by")
# What each of them updates, which the pages read: the run's ``rows_failed`` could not be saved there
WRITTEN_INTO = {
    "partners": "partners",
    "interventions": "programme documents",
    "intervention_budgets": "programme documents",
    "agreements": "agreements",
    "audits": "engagements",
    "audit_results": "engagements",
    "spot_checks": "engagements",
    "micro_assessments": "engagements",
    "special_audits": "engagements",
    "locations": "locations",
    "location_sites": "sites",
}


def _count(value: Any) -> int:
    """A count from a run's details (0 when it is not one)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _given(value: Any) -> int | None:
    """A count the details give, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def records_read(run: SyncRun) -> int:
    """The records a run received, each once, kept or not: ``details.read`` when the read recorded it, else
    the rows it wrote and those of another country it left out. The one rule for "NeuroDB received"
    (checks 10 and 11 read it too)."""
    details = run.details if isinstance(run.details, dict) else {}
    return _count(details.get("read")) or run.rows_in + _count(details.get("other_country_skipped"))


@dataclass(frozen=True)
class ErrorLine:
    """A kind of error a run met while saving records: its words without their values, how many records met
    it, and the ids of a few of them."""

    words: str
    count: int
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunFacts:
    source: str
    run: SyncRun | None  # the read the records come from
    newer_failed: SyncRun | None  # a newer run that failed, if any
    said_it_had: int | None  # details.datamart_count
    received: int | None  # records_read(run): distinct records
    sent_twice: int  # duplicates_read: a note, each already counted once in received
    never_came: int  # said_it_had - received, when > 0
    more_than_said: int  # received - said_it_had, when > 0
    shift_words: str
    other_country: int  # other_country_skipped - unnamed_skipped
    through_area_filter: int  # area_strays: an "of which", inside other_country
    unnamed: int  # unnamed_skipped
    not_saved: int  # the records (of a source written by another sync: the raw copies) that failed
    not_written: int  # a source written by another sync: run.rows_failed, not saved into WRITTEN_INTO
    written_into: str  # WRITTEN_INTO's words, or ""
    error_lines: tuple[ErrorLine, ...]
    held: int  # in NeuroDB now
    updated_by_sync: int  # started_at <= synced_at <= finished_at
    updated_since: int  # synced_at > finished_at: a later run under way
    from_earlier: int  # last updated before run.started_at
    unexplained: int  # received - other_country_skipped - not_saved - updated_by_sync
    removed: int
    removal_skipped: bool
    removal_held: int
    sent_nothing: bool = False  # an eTools REST list whose read received nothing
    not_linked: dict[str, int] = field(default_factory=dict)
    complete: tuple[bool, str] = (False, "")


def _error_lines(details: dict[str, Any]) -> tuple[ErrorLine, ...]:
    from neurodb.integrations.runs import _error_key

    lines = []
    for entry in details.get("errors") or []:
        if not isinstance(entry, dict):
            continue
        examples = tuple(str(e)[:40] for e in (entry.get("examples") or [])[:5])
        lines.append(
            ErrorLine(_error_key(str(entry.get("error") or "")), _count(entry.get("count")), examples)
        )
    return tuple(lines)


def _verdict(facts: dict[str, Any]) -> tuple[bool, str]:
    """ "Complete?": yes when the last sync succeeded, NeuroDB received at least what eTools said it had (an
    eTools REST list, at least one record), no record failed (nor, for a source written by another sync, its
    partner, programme document, location...) and none is unexplained; otherwise the first reason, in plain
    words."""
    run = facts["run"]
    if run is None:
        return False, "No: never synced"
    if facts["newer_failed"] is not None:
        return False, "No: the last sync failed"
    if facts.get("sent_nothing"):
        return False, "No: eTools sent nothing"
    if facts["never_came"]:
        return False, f"No: eTools said it had {facts['never_came']:,} more"
    if facts["not_saved"]:
        return False, f"No: {facts['not_saved']:,} could not be saved"
    if facts["not_written"]:
        return (
            False,
            f"No: {facts['not_written']:,} could not be saved into NeuroDB's {facts['written_into']}",
        )
    failed = sum(line.count for line in facts["error_lines"])
    if failed and run.status != SyncRun.Status.SUCCEEDED:  # errors the counts above do not hold
        return False, f"No: {failed:,} could not be saved"
    if facts["unexplained"]:
        return False, f"No: {facts['unexplained']:,} records are not explained by the sync's counts"
    return True, "Yes"


def build(
    name: str, run: SyncRun | None, newer_failed: SyncRun | None, counts: tuple[int, int, int]
) -> RunFacts:
    """The facts of ``name`` from its run and ``counts`` (in NeuroDB, updated by the sync, updated since)."""
    from neurodb.integrations.etools.datamart_sync import shift_words
    from neurodb.reports.services import quality_row

    from . import raw

    if raw.rest(name):
        return _rest_build(name, run, newer_failed, counts)
    held, by_sync, since = counts
    row = quality_row(name, run, held)
    details = (run.details if isinstance(run.details, dict) else {}) if run else {}
    said = _given(row["datamart_count"])
    received = records_read(run) if run else None
    skipped, unnamed = _count(row["other_country_skipped"]), _count(row["unnamed_skipped"])
    failed = run.rows_failed if run else 0
    not_saved = _count(details.get("documents_failed")) if name in WRITTEN_BY else failed
    facts: dict[str, Any] = {
        "source": name,
        "run": run,
        "newer_failed": newer_failed,
        "said_it_had": said,
        "received": received,
        "sent_twice": _count(row["duplicates_read"]),
        "never_came": max(said - received, 0) if said is not None and received is not None else 0,
        "more_than_said": max(received - said, 0) if said is not None and received is not None else 0,
        "shift_words": shift_words(details) if run else "",
        "other_country": max(skipped - unnamed, 0),
        "through_area_filter": _count(row["area_strays"]),
        "unnamed": unnamed,
        "not_saved": not_saved,
        "not_written": failed if name in WRITTEN_BY else 0,
        "written_into": WRITTEN_INTO.get(name, ""),
        "error_lines": _error_lines(details),
        "held": held,
        "updated_by_sync": by_sync,
        "updated_since": since,
        "from_earlier": max(held - by_sync - since, 0),
        "unexplained": (received - skipped - not_saved - by_sync) if received is not None else 0,
        "removed": _count(details.get("removed")) or _count(details.get("documents_removed")),
        "removal_skipped": row["removal_skipped"],
        "removal_held": _count(row["removal_held"]),
        "not_linked": {str(k): _count(v) for k, v in row["not_linked"].items()},
    }
    facts["complete"] = _verdict(facts)
    return RunFacts(**facts)


def _rest_build(
    name: str, run: SyncRun | None, newer_failed: SyncRun | None, counts: tuple[int, int, int]
) -> RunFacts:
    """The facts of an eTools REST entity: what the read that kept its copy received and kept
    (``rest_raw.RestCopy.finish``), and ``counts``."""
    from neurodb.integrations.etools import rest_raw

    from . import raw

    held, by_sync, since = counts
    details = raw.run_details(run, name)
    said = _given(details.get("announced"))
    received = _count(details.get("read")) if run else None
    not_saved = max(received - _count(details.get("kept")), 0) if received is not None else 0
    errors = run.details if run is not None and isinstance(run.details, dict) else {}
    facts: dict[str, Any] = {
        "source": name,
        "run": run,
        "newer_failed": newer_failed,
        "said_it_had": said,
        "received": received,
        "sent_twice": _count(details.get("duplicates_read")),
        "never_came": max(said - received, 0) if said is not None and received is not None else 0,
        "more_than_said": max(received - said, 0) if said is not None and received is not None else 0,
        "shift_words": "",
        "other_country": 0,
        "through_area_filter": 0,
        "unnamed": 0,
        "not_saved": not_saved,
        "not_written": 0,
        "written_into": "",
        "error_lines": _error_lines(errors),
        "held": held,
        "updated_by_sync": by_sync,
        "updated_since": since,
        "from_earlier": max(held - by_sync - since, 0),
        "unexplained": (received - not_saved - by_sync) if received is not None else 0,
        "removed": _count(details.get("removed")),
        "removal_skipped": bool(details.get("removal_skipped")),
        "removal_held": 0,
        "sent_nothing": name in rest_raw.LISTS and received == 0,
    }
    facts["complete"] = _verdict(facts)
    return RunFacts(**facts)


# ---------------------------------------------------------------------------------------------- the runs
_READ_SOMETHING = (
    "AND s.status IN (%s, %s) AND COALESCE(s.details->>'empty_response', '') IN ('', 'false', '0', 'null') "
)


def runs(names: list[str]) -> dict[str, tuple[SyncRun | None, SyncRun | None]]:
    """For each dataset of ``names``: the run its records come from and a newer run that failed (each None
    when there is none), in two queries on the index ``core_syncrun_job_target`` (job, target, newest start
    first), as ``reports.services.latest_datamart_runs`` reads them. The eTools REST entities among them:
    :func:`rest_runs`."""
    from neurodb.reports.services import latest_datamart_runs

    from . import raw

    entities = [name for name in names if raw.rest(name)]
    if entities:
        found = rest_runs(entities)
        others = [name for name in names if not raw.rest(name)]
        return {**(runs(others) if others else {}), **found}

    table = connection.ops.quote_name(SyncRun._meta.db_table)
    params = [
        list(names),
        SyncRun.Job.ETOOLS_DATAMART.value,
        SyncRun.Status.SUCCEEDED.value,
        SyncRun.Status.PARTIAL.value,
    ]
    query = (
        "SELECT r.* FROM unnest(%s::text[]) AS t(target) CROSS JOIN LATERAL ("  # noqa: S608 - the model's table
        f"SELECT s.* FROM {table} s WHERE s.job = %s AND s.target = t.target {_READ_SOMETHING}"
        "ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r"
    )
    used = {run.target: run for run in SyncRun.objects.raw(query, params)}
    latest = latest_datamart_runs(names, finished=True)
    out: dict[str, tuple[SyncRun | None, SyncRun | None]] = {}
    for name in names:
        run, last = used.get(name), latest.get(name)
        newer = (
            last is not None
            and last.status == SyncRun.Status.FAILED
            and (run is None or last.started_at > run.started_at)
        )
        out[name] = (run, last if newer else None)
    return out


def rest_jobs(name: str) -> list[tuple[str, str]]:
    """The (job, target) of the runs that keep an eTools REST entity's copy: the read into Raw eTools data
    (target: the entity), and the eTools REST sync of that entity (``rest_raw.SYNC_TARGETS``)."""
    from neurodb.integrations.etools import rest_raw

    return [
        (SyncRun.Job.ETOOLS_REST_RAW.value, name),
        (SyncRun.Job.ETOOLS.value, rest_raw.SYNC_TARGETS[name]),
    ]


# What a run's copy of an eTools REST entity received: the read's ``details.read``, or the eTools REST sync's
# ``details.raw_copies.<entity>.read``
_REST_READ = "(COALESCE(s.details -> 'raw_copies' -> t.entity, s.details) -> 'read')"


def rest_runs(names: list[str]) -> dict[str, tuple[SyncRun | None, SyncRun | None]]:
    """For each eTools REST entity of ``names``: the latest run that kept its copy (succeeded, or succeeded
    with errors: of the read into Raw eTools data, or of the eTools REST sync when its details hold the copy)
    and, for a list, received something (a list eTools answered empty, when it is down or the token's user
    lost the country, removed nothing: the records come from the read before), and a newer run of either
    that failed; in two queries, each the latest run of every (job, target) on the index
    ``core_syncrun_job_target``."""
    from neurodb.integrations.etools import rest_raw

    table = connection.ops.quote_name(SyncRun._meta.db_table)
    pairs = [(name, job, target, name in rest_raw.LISTS) for name in names for job, target in rest_jobs(name)]
    columns = [list(column) for column in zip(*pairs, strict=True)] if pairs else [[], [], [], []]
    copied = (
        "SELECT t.entity AS rest_entity, r.* "  # noqa: S608 - the model's table
        "FROM unnest(%s::text[], %s::text[], %s::text[], %s::boolean[]) AS t(entity, job, target, is_list) "
        f"CROSS JOIN LATERAL (SELECT s.* FROM {table} s WHERE s.job = t.job AND s.target = t.target "
        "AND s.status IN (%s, %s) "
        "AND (s.job <> %s OR jsonb_exists(COALESCE(s.details -> 'raw_copies', '{}'::jsonb), t.entity)) "
        f"AND (NOT t.is_list OR (jsonb_typeof({_REST_READ}) = 'number' AND {_REST_READ} > '0'::jsonb)) "
        "ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r"
    )
    finished = (
        "SELECT t.entity AS rest_entity, r.* "  # noqa: S608 - the model's table
        "FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(entity, job, target) CROSS JOIN LATERAL ("
        f"SELECT s.* FROM {table} s WHERE s.job = t.job AND s.target = t.target AND s.status <> %s "
        "ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r"
    )
    used: dict[str, SyncRun] = {}
    for run in SyncRun.objects.raw(
        copied,
        [
            *columns,
            SyncRun.Status.SUCCEEDED.value,
            SyncRun.Status.PARTIAL.value,
            SyncRun.Job.ETOOLS.value,
        ],
    ):
        if run.rest_entity not in used or run.started_at > used[run.rest_entity].started_at:
            used[run.rest_entity] = run
    failed: dict[str, SyncRun] = {}
    for run in SyncRun.objects.raw(finished, [*columns[:3], SyncRun.Status.RUNNING.value]):
        before = used.get(run.rest_entity)
        if run.status == SyncRun.Status.FAILED and (before is None or run.started_at > before.started_at):
            if run.rest_entity not in failed or run.started_at > failed[run.rest_entity].started_at:
                failed[run.rest_entity] = run
    return {name: (used.get(name), failed.get(name)) for name in names}


def counts(name: str, run: SyncRun | None) -> tuple[int, int, int]:
    """For one source, in one query: the records in NeuroDB, those the run updated, and those updated
    since it ended."""
    from . import raw

    return raw._counts(raw.base(name), run)


def facts(name: str) -> RunFacts:
    """The facts of one source (its page): its runs (two queries) and its counts (one)."""
    run, newer = runs([name])[name]
    return build(name, run, newer, counts(name, run))


def moment(run: SyncRun | None) -> datetime.datetime | None:
    """The moment of the eTools figures a run gives: its end."""
    return run.finished_at if run is not None else None
