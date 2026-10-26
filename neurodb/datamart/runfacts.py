"""What each eTools Datamart sync received, and what became of it: the header of every source on Raw eTools
data (Admin → Data and sync), in the words of Data health's datasets table (``reports.services.quality_row``,
the same row of the same run).

The run the records come from is the latest that succeeded, or succeeded with errors, and received something
(no ``empty_response``), as ``datamart_sync.last_details`` chooses; a newer run that failed is named beside
it. Counted on distinct records, the same for the datasets in their own table and those kept whole:

    received − of another country − named no country − could not be saved = updated by the sync
    in NeuroDB = updated by the sync + updated since (a later sync under way) + kept from an earlier sync

The partner report rows eTools no longer sends (a whole read keeps them, counted:
``datamart_sync.keep_unsent``) are kept from an earlier sync: counted there (``no_longer_sent``), never as
unexplained.

A record a later run rewrote (one running, or one that failed after writing) is counted updated since, not by
the sync: up to the remainder, those are explained too. The engagements are saved again by their detail
datasets (audits, spot checks...) right after their own run, in the same sync: those saves count as the
sync's (``raw.written_until``), never more than the engagements run kept itself; the others they saved, kept
from an earlier sync (a read not whole removes nothing), count as kept from an earlier sync.

``other_country_skipped`` already holds the records that named no country (``unnamed_skipped``) and those of
another business area a filter let through (``area_strays``): "of another country" is shown without the first,
the second as an "of which". Records sent twice are a note, each already counted once in what NeuroDB
received; those the read proved the Datamart's own copies (``copies_proven``: every copy came identical in one
answer) count with what it received against what eTools said it had, and those it could not prove, when
they are as many as the records that did not come, are said to be why ("may stand for others that did not
come"). A remainder the counts do not explain is shown, never hidden. For a source written by another sync
(``WRITTEN_BY``) the equation counts its raw copies; the records that could not be saved into the partners,
programme documents, locations... the pages read (the run's ``rows_failed``) are a line of their own, and make
"Complete?" No. An error is shown by its kind only (``runs._error_key``): its text can hold a value of the
record. Counts only: no record's value is read here.

An eTools REST entity's records come from the latest run that kept its raw copy: of the job "Read the eTools
REST API into Raw eTools data" (one run per entity), or of the eTools REST sync (its ``details.raw_copies``);
for a list, the latest that received something (a list eTools answered empty removes nothing from the copy:
a newer read that received nothing is named beside it, ``newer_empty``). The same equation holds, with nothing
of another country: received − could not be saved = updated by the sync.

A run that failed is said by its cause in a few plain words (:func:`failure_words`: the status eTools
answered, no answer in time, no connection...), never by its error's text.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
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
    copies_proven: int  # of sent_twice, the Datamart's own copies (details.copies_proven.twice)
    never_came: int  # said_it_had - received - copies_proven, when > 0
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
    updated_by_sync: int  # started_at <= synced_at <= finished_at (raw.written_until; at most what it kept)
    updated_since: int  # synced_at > finished_at: a later run under way
    from_earlier: int  # last updated before run.started_at
    unexplained: int  # received - other_country_skipped - not_saved - updated_by_sync, less updated_since
    removed: int
    removal_skipped: bool
    removal_held: int
    sent_nothing: bool = False  # an eTools REST list whose read received nothing
    not_linked: dict[str, int] = field(default_factory=dict)
    complete: tuple[bool, str] = (False, "")
    # the last sync failed because the eTools Datamart does not let NeuroDB's account read this dataset (403)
    not_allowed: bool = False
    # an eTools REST entity: a newer read than ``run`` that ended without receiving anything (a list eTools
    # answered empty; the action points with no final engagement to ask for), and what it got, in words
    newer_empty: SyncRun | None = None
    empty_words: str = ""
    # the partner reports (:func:`facts`: their page and the trails): the rows eTools no longer sends, kept
    # and counted inside ``from_earlier`` (raw.no_longer_sent), their progress reports, those of them eTools
    # sends again for another period, and when they were last sent
    no_longer_sent: int = 0
    no_longer_sent_reports: int = 0
    same_number_other_period: int = 0
    last_sent_first: datetime.datetime | None = None
    last_sent_last: datetime.datetime | None = None

    @property
    def twice_unproven(self) -> int:
        """The records sent twice the read could not prove the Datamart's own copies."""
        return max(self.sent_twice - self.copies_proven, 0)

    @property
    def gap_by_repeats(self) -> bool:
        """The records that did not come are no more than those sent twice and not proven: these may stand
        for them (rows that moved between pages), or the Datamart holds them twice."""
        return bool(self.never_came) and self.twice_unproven >= self.never_came


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


def _unexplained(rest: int, since: int) -> int:
    """What the counts leave unexplained: the remainder (received less what was left out and what the sync
    updated), less the records a later run rewrote since, at most the remainder: a negative remainder, or one
    larger than what the later run rewrote, stays shown."""
    return rest - min(max(rest, 0), since)


def _verdict(facts: dict[str, Any]) -> tuple[bool, str]:
    """ "Complete?": yes when the last sync succeeded, NeuroDB received at least what eTools said it had (an
    eTools REST list, at least one record), no record failed (nor, for a source written by another sync, its
    partner, programme document, location...) and none is unexplained; otherwise the first reason, in plain
    words."""
    run = facts["run"]
    failed = (
        "No: not allowed to read it (ask the eTools Datamart team)"
        if facts.get("not_allowed")
        else "No: the last sync failed"
    )
    if run is None:
        if facts["newer_failed"] is not None:
            return False, failed
        if facts.get("newer_empty") is not None:
            return False, f"No: {facts['empty_words']}"
        return False, "No: never synced"
    if facts["newer_failed"] is not None:
        return False, failed
    if facts.get("sent_nothing"):
        return False, "No: eTools sent nothing"
    unproven = max(facts["sent_twice"] - facts.get("copies_proven", 0), 0)
    if facts["never_came"] and unproven >= facts["never_came"]:
        words = "were sent twice and may stand for others that did not come; nothing was removed"
        return False, f"No: {unproven:,} {words}"
    if facts["never_came"]:
        return False, f"No: eTools said it had {facts['never_came']:,} more"
    if facts["not_saved"]:
        return False, f"No: {facts['not_saved']:,} could not be saved"
    if facts["not_written"]:
        return (
            False,
            f"No: {facts['not_written']:,} could not be saved into NeuroDB's {facts['written_into']}",
        )
    lines = sum(line.count for line in facts["error_lines"])
    if lines and run.status != SyncRun.Status.SUCCEEDED:  # errors the counts above do not hold
        return False, f"No: {lines:,} could not be saved"
    if facts["unexplained"]:
        return False, f"No: {facts['unexplained']:,} records are not explained by the sync's counts"
    return True, "Yes"


def build(
    name: str,
    run: SyncRun | None,
    newer_failed: SyncRun | None,
    counts: tuple[int, int, int],
    *,
    last: SyncRun | None = None,
) -> RunFacts:
    """The facts of ``name`` from its run and ``counts`` (in NeuroDB, updated by the sync, updated since);
    for an eTools REST entity, ``last`` is its newest run that ended (:func:`rest_reads`)."""
    from neurodb.integrations.etools.datamart_sync import shift_words
    from neurodb.reports.services import quality_row

    from . import raw

    if raw.rest(name):
        return _rest_build(name, run, newer_failed, counts, last)
    held, by_sync, since = counts
    row = quality_row(name, run, held)
    details = (run.details if isinstance(run.details, dict) else {}) if run else {}
    said = _given(row["datamart_count"])
    received = records_read(run) if run else None
    skipped, unnamed = _count(row["other_country_skipped"]), _count(row["unnamed_skipped"])
    failed = run.rows_failed if run else 0
    not_saved = _count(details.get("documents_failed")) if name in WRITTEN_BY else failed
    proven = _count(row["copies_proven"])
    if received is not None and raw.rewritten_by(name):
        # the detail datasets' saves count as the sync's only up to what its own run kept: one it did not
        # receive (a read not whole keeps it) was kept from an earlier sync, and saved again by them
        by_sync = min(by_sync, max(received - skipped - not_saved, 0))
    facts: dict[str, Any] = {
        "source": name,
        "run": run,
        "newer_failed": newer_failed,
        "said_it_had": said,
        "received": received,
        "sent_twice": _count(row["duplicates_read"]),
        "copies_proven": proven,
        "never_came": max(said - received - proven, 0) if said is not None and received is not None else 0,
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
        "unexplained": _unexplained(received - skipped - not_saved - by_sync, since)
        if received is not None
        else 0,
        "removed": _count(details.get("removed")) or _count(details.get("documents_removed")),
        "removal_skipped": row["removal_skipped"],
        "removal_held": _count(row["removal_held"]),
        "not_linked": {str(k): _count(v) for k, v in row["not_linked"].items()},
        "not_allowed": not_allowed(newer_failed),
    }
    facts["complete"] = _verdict(facts)
    return RunFacts(**facts)


def empty_words(name: str, run: SyncRun) -> str:
    """What an eTools REST read that received nothing got, in words."""
    from neurodb.integrations.etools import rest_raw

    from . import raw

    if raw.run_details(run, name).get("removal_skipped_why") == rest_raw.NO_FINAL:
        return "no final engagement to ask for"
    return "eTools sent nothing"


def _newer_empty(name: str, run: SyncRun | None, last: SyncRun | None) -> SyncRun | None:
    """``last`` when it is a newer read than ``run`` that ended without failing and received nothing."""
    from . import raw

    if last is None or last.status == SyncRun.Status.FAILED or (run is not None and last.pk == run.pk):
        return None
    if run is not None and last.started_at <= run.started_at:
        return None
    return last if _given(raw.run_details(last, name).get("read")) == 0 else None


def _rest_build(
    name: str,
    run: SyncRun | None,
    newer_failed: SyncRun | None,
    counts: tuple[int, int, int],
    last: SyncRun | None = None,
) -> RunFacts:
    """The facts of an eTools REST entity: what the read that kept its copy received and kept
    (``rest_raw.RestCopy.finish``), and ``counts``."""
    from neurodb.integrations.etools import rest_raw

    from . import raw

    held, by_sync, since = counts
    empty = _newer_empty(name, run, last)
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
        "copies_proven": 0,
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
        "unexplained": _unexplained(received - not_saved - by_sync, since) if received is not None else 0,
        "removed": _count(details.get("removed")),
        "removal_skipped": bool(details.get("removal_skipped")),
        "removal_held": 0,
        "sent_nothing": name in rest_raw.LISTS and received == 0,
        "newer_empty": empty,
        "empty_words": empty_words(name, empty) if empty is not None else "",
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


@dataclass(frozen=True)
class RestReads:
    """The runs of an eTools REST entity the pages read."""

    run: SyncRun | None  # the run its records come from
    newer_failed: SyncRun | None  # a newer run that failed
    last: SyncRun | None  # its newest run that ended, of either job


def rest_runs(names: list[str]) -> dict[str, tuple[SyncRun | None, SyncRun | None]]:
    """For each eTools REST entity of ``names``: the run its records come from, and a newer run that failed
    (:func:`rest_reads`)."""
    return {name: (found.run, found.newer_failed) for name, found in rest_reads(names).items()}


def rest_reads(names: list[str]) -> dict[str, RestReads]:
    """For each eTools REST entity of ``names``: the latest run that kept its copy (succeeded, or succeeded
    with errors: of the read into Raw eTools data, or of the eTools REST sync when its details hold the copy)
    and, for a list, received something (a list eTools answered empty, when it is down or the token's user
    lost the country, removed nothing: the records come from the read before), a newer run of either that
    failed, and the newest run of either that ended; in two queries, each the latest run of every (job,
    target) on the index ``core_syncrun_job_target``."""
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
    last: dict[str, SyncRun] = {}
    for run in SyncRun.objects.raw(finished, [*columns[:3], SyncRun.Status.RUNNING.value]):
        if run.rest_entity not in last or run.started_at > last[run.rest_entity].started_at:
            last[run.rest_entity] = run
        before = used.get(run.rest_entity)
        if run.status == SyncRun.Status.FAILED and (before is None or run.started_at > before.started_at):
            if run.rest_entity not in failed or run.started_at > failed[run.rest_entity].started_at:
                failed[run.rest_entity] = run
    return {name: RestReads(used.get(name), failed.get(name), last.get(name)) for name in names}


def counts(name: str, run: SyncRun | None) -> tuple[int, int, int]:
    """For one source, in one query: the records in NeuroDB, those the run updated, and those updated
    since it ended."""
    from . import raw

    return raw._counts(raw.base(name), run, name)


def facts(name: str) -> RunFacts:
    """The facts of one source (its page): its runs (two queries) and its counts (one); for the partner
    reports, the rows eTools no longer sends (one more, two when some are marked)."""
    from . import raw

    if raw.rest(name):
        found = rest_reads([name])[name]
        return build(name, found.run, found.newer_failed, counts(name, found.run), last=found.last)
    run, newer = runs([name])[name]
    found = build(name, run, newer, counts(name, run))
    if name == "partner_reports":  # its own page and the trails: the rows eTools no longer sends
        unsent = raw.no_longer_sent()
        found = dataclasses.replace(
            found,
            no_longer_sent=unsent["rows"],
            no_longer_sent_reports=unsent["reports"],
            same_number_other_period=unsent["other_period"],
            last_sent_first=unsent["first"],
            last_sent_last=unsent["last"],
        )
    return found


# ------------------------------------------------------------------------------------- why a run failed
# The words of an HTTP status eTools or the Datamart answered (none: the status alone, "eTools answered 500")
STATUS_WORDS = {
    400: "the request was refused",
    401: "sign-in refused",
    403: "not allowed",
    404: "not found",
    408: "no answer in time",
    429: "too many requests",
    502: "unavailable",
    503: "unavailable",
    504: "no answer in time",
}
UNREADABLE = re.compile(r"response is not JSON|unrecognised (list|record) payload|unexpected list payload")
NOT_READ = "Not read: "  # the start of rest_raw.NOT_READ and NEVER_READ


# What the body of a 403 kept in a run's error says (Django REST framework's words): the sign-in refused, or
# the account signed in and refused the dataset (a permission is checked only once signed in)
SIGN_IN_REFUSED = re.compile(
    r"Invalid username/password|Authentication credentials were not provided|User inactive or deleted|"
    r"Invalid token",
    re.IGNORECASE,
)
NO_PERMISSION = re.compile(r"You do not have permission to perform this action", re.IGNORECASE)
DATASET, SIGN_IN, EITHER = "dataset", "sign_in", "either"  # :func:`refused`


def refused(run: SyncRun | None) -> str:
    """Why the eTools Datamart answered a run 403: :data:`DATASET`, the account signed in but not let read the
    dataset (the body says so, or the run of another dataset nearest in time read: :func:`signed_in_near`);
    :data:`SIGN_IN`, the body names the sign-in refused; :data:`EITHER`, nothing tells the two apart (every
    dataset failing so is the sign-in). "" for a run of another job, or with no 403. A 401 is the sign-in,
    said by its status. One query, for a 403 whose body tells nothing; kept on the run."""
    from neurodb.core.admin import HTTP_STATUS

    if run is None or run.job != SyncRun.Job.ETOOLS_DATAMART:
        return ""
    error = run.error or ""
    found = HTTP_STATUS.search(error)
    if found is None or found.group(1) != "403":
        return ""
    why = getattr(run, "_refused", None)
    if why is None:
        if SIGN_IN_REFUSED.search(error):
            why = SIGN_IN
        elif NO_PERMISSION.search(error) or signed_in_near(run.started_at, run.target, exclude=run.pk):
            why = DATASET
        else:
            why = EITHER
        run._refused = why
    return why


def signed_in_near(moment: datetime.datetime | None, target: str, *, exclude: int | None = None) -> bool:
    """Whether the run of another eTools Datamart dataset nearest in time to ``moment`` (the one that started
    just before it, or just after it, whichever is nearer) read, or read with errors: NeuroDB's sign-in
    worked then. A run still under way is left out. One query."""
    if moment is None:
        return False
    others = SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART).exclude(target=target)
    others = others.exclude(status=SyncRun.Status.RUNNING)
    if exclude is not None:
        others = others.exclude(pk=exclude)
    fields = ("started_at", "status")
    after = others.filter(started_at__gte=moment).order_by("started_at", "pk").values_list(*fields)[:1]
    before = others.filter(started_at__lt=moment).order_by("-started_at", "-pk").values_list(*fields)[:1]
    near = sorted(after.union(before, all=True), key=lambda found: abs(found[0] - moment))
    return bool(near) and near[0][1] in (SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL)


def not_allowed(run: SyncRun | None) -> bool:
    """An eTools Datamart run that failed because the Datamart does not let NeuroDB's account read its
    dataset: it answered 403, and the account was signed in (:func:`refused`)."""
    return refused(run) == DATASET


def not_read(run: SyncRun | None) -> bool:
    """A run of an eTools REST entity that asked eTools for nothing: the list it asks for failed."""
    return run is not None and run.status == SyncRun.Status.FAILED and (run.error or "").startswith(NOT_READ)


def failure_words(run: SyncRun | None) -> str:
    """Why a run failed, in a few plain words read from its error: the status the service answered ("eTools
    answered 403: not allowed"; for the eTools Datamart, that the account NeuroDB uses is not allowed to read
    the dataset and whom to ask, the sign-in refused, or either, :func:`refused`), no answer in time, could
    not connect, NeuroDB restarted during the run, the list an entity asks for that could not be read, the
    token not set, no PRP business area code set; "an unexpected error" for any other.
    Never the error's text: an answer's body comes from eTools or a proxy, and can hold a record's value."""
    from neurodb.core.admin import HTTP_STATUS, NOT_IN_TIME, SOURCES, unanswered
    from neurodb.core.models.sync import STOPPED
    from neurodb.integrations.background import CUT_OFF
    from neurodb.integrations.etools import datamart_sync, rest_raw

    if run is None:
        return ""
    error = run.error or ""
    source = SOURCES.get(run.job, "the data source")
    if error == CUT_OFF:
        return "NeuroDB was restarted during the run"
    if error.startswith(STOPPED):
        return "stopped from Import and sync runs"
    if error == rest_raw.NO_TOKEN:
        return "the eTools REST API token is not set"
    if error.startswith(NOT_READ):
        return error[len(NOT_READ) :].rstrip(".")
    if error.startswith("DatamartNotConfigured"):
        return "the eTools Datamart sign-in is not set"
    if error.removeprefix("IntegrationError: ").startswith(datamart_sync.ENTER_CODE) or (
        datamart_sync.NEVER_SENT in error
    ):  # never the value set aside: the error names it
        return "no PRP business area code is set: enter it in Admin → Data and sync → eTools Datamart"
    why = refused(run)
    if why == DATASET:
        return (
            "eTools Datamart answered 403: the account NeuroDB uses is not allowed to read it; ask the "
            "eTools Datamart team to grant it"
        )
    if why == SIGN_IN:
        return "eTools Datamart answered 403: sign-in refused"
    if why == EITHER:
        return "eTools Datamart answered 403: it refused NeuroDB's sign-in or this dataset"
    found = HTTP_STATUS.search(error)
    if found:
        status = int(found.group(1))
        words = STATUS_WORDS.get(status)
        return f"{source} answered {status}: {words}" if words else f"{source} answered {status}"
    how = unanswered(error)  # as the run page says it (core.admin.error_hint)
    if how:
        return "no answer in time" if how == NOT_IN_TIME else "could not connect"
    if UNREADABLE.search(error):
        return f"{source} sent an answer NeuroDB cannot read"
    return "an unexpected error"


def moment(run: SyncRun | None) -> datetime.datetime | None:
    """The moment of the eTools figures a run gives: its end."""
    return run.finished_at if run is not None else None
