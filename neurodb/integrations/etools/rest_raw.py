"""The eTools REST API into Raw eTools data: a raw copy of the records the eTools REST sync reads, list and
detail apart (``datamart.RestRecord``, one entity per endpoint: ``ENTITIES``), so that a validator checks the
partners, programme documents, trips, engagements and action points eTools sent against what NeuroDB made
of them (Admin → Data and sync → Raw eTools data).

* :class:`RestCopy` keeps the copy of one entity as a read goes: each record as it came (``catalogue.scrub``:
  no contact field), what the read received (each record once, by its eTools id), what eTools said it had
  (the first page's ``count``), the fields of the records kept (``raw.KeyCount``: ``details.keys_kept``);
  and, after a list read that ended, it removes the records eTools no longer lists and the details of those.
  A read that failed, received nothing, or fewer records than eTools said it had, removes nothing. A detail
  entity is never removed on its own (the details of trips started more than a year ago stay).
* :func:`read_all` is the job "Read the eTools REST API into Raw eTools data" (``read_etools_rest_raw``:
  the button on Raw eTools data, and Run a job). It reads the list endpoints and the detail GETs into the
  copy only: it writes no other table, so it moves no figure. One ``SyncRun`` per entity, one run at a time
  (:data:`LOCK_ID`).
* The eTools REST sync (``sync.py``) keeps the same copy as it reads, in the savepoint of each record, under
  the same removal rule; it writes the partners, programme documents (whose status decides "running" on
  every partner reporting page), trips, engagements and action points as before.

At the end of either job the names the copies hold are added to those NeuroDB takes out of texts
(``query.remember_people``). Ask NeuroDB never reads the copy.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.models.fields.json import KT
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue
from neurodb.datamart.models import RestRecord
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.runs import describe_error, fail, finish_by_counts, new_run, process_items

logger = logging.getLogger(__name__)

LOCK_ID = 7140441  # one read at a time; neurodb.integrations.background.ETOOLS_REST_RAW_LOCK_ID
COMMAND = "read_etools_rest_raw"  # the command the button on Raw eTools data and Run a job start
BUSY = "The eTools REST API is already being read into Raw eTools data."
NO_TOKEN = "The eTools REST API token is not set on this server: ask the NeuroDB team."  # noqa: S105
DETAIL_DAYS = 365  # the trips whose details are read: those started in the last year (as the REST sync)
ENGAGEMENT_FINAL = "final"  # the engagements whose action points are listed (as the REST sync lists them)
KEY_MAX = 64  # RestRecord.record_key
TEXT_MAX = 255  # RestRecord.reference and title

# entity -> the endpoint it is read from, and for a detail the list entity whose records it details. The
# engagement details are read from the endpoint of each engagement's type (sync.ENGAGEMENT_DETAIL_PATHS).
ENTITIES: dict[str, tuple[str, str]] = {
    "rest_partners": ("/api/v2/partners/", ""),
    "rest_partner_details": ("/api/v2/partners/{id}/", "rest_partners"),
    "rest_agreements": ("/api/v2/agreements/", ""),
    "rest_interventions": ("/api/v2/interventions/", ""),
    "rest_intervention_details": ("/api/v2/interventions/{id}/", "rest_interventions"),
    "rest_travels": ("/api/t2f/travels/", ""),
    "rest_travel_details": ("/api/t2f/travels/{id}/", "rest_travels"),
    "rest_engagements": ("/api/audit/engagements/", ""),
    "rest_engagement_details": ("/api/audit/<type>/{id}/", "rest_engagements"),
    "rest_action_points": ("/api/action-points/action-points/", ""),
}
DETAILS = {parent: entity for entity, (_path, parent) in ENTITIES.items() if parent}  # list -> its details
LISTS = frozenset(entity for entity, (_path, parent) in ENTITIES.items() if not parent)  # the lists
# entity -> the run of the eTools REST sync that keeps its copy too (its target: sync.ENTITY_SYNCS)
SYNC_TARGETS = {
    "rest_partners": "partners",
    "rest_partner_details": "partner_details",
    "rest_agreements": "agreements",
    "rest_interventions": "interventions",
    "rest_intervention_details": "intervention_details",
    "rest_travels": "travels",
    "rest_travel_details": "travels",
    "rest_engagements": "engagements",
    "rest_engagement_details": "engagements",
    "rest_action_points": "action_points",
}
# The field a record's reference and title are read from: a number or a name, never a person; a trip's title
# is its reference number, never its purpose (which can name the traveller)
REFERENCE = {
    "rest_partners": "vendor_number",
    "rest_partner_details": "vendor_number",
    "rest_agreements": "agreement_number",
    "rest_interventions": "number",
    "rest_intervention_details": "number",
    "rest_travels": "reference_number",
    "rest_travel_details": "reference_number",
    "rest_engagements": "reference_number",
    "rest_engagement_details": "reference_number",
    "rest_action_points": "reference_number",
}
TITLE = {
    "rest_partners": "name",
    "rest_partner_details": "name",
    "rest_interventions": "title",
    "rest_intervention_details": "title",
    "rest_travels": "reference_number",
    "rest_travel_details": "reference_number",
}


def record_key(item: dict[str, Any], asked: str = "") -> str:
    """A record's key in the copy: its eTools id (for a detail that names none, the id it was asked by),
    else a hash of the record."""
    from neurodb.integrations.etools.datamart_sync import content_key, id_value  # lazy: the Datamart sync

    found = id_value(item.get("id")) or asked
    return found if found and len(found) <= KEY_MAX else content_key(item)


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key) if key else None
    if value in (None, "") or isinstance(value, dict | list | bool):
        return ""
    return " ".join(str(value).split())[:TEXT_MAX]


class RestCopy:
    """The raw copy of one eTools REST entity, kept as a read goes (see the module's notes). ``offered``
    counts what the read received, ``keep`` writes one record (call it last in the record's savepoint: a
    record whose write fails is not kept, and its previous copy stays), ``finish`` removes what a list no
    longer holds and gives the run's details."""

    def __init__(self, entity: str) -> None:
        from neurodb.datamart import raw  # lazy: Raw eTools data reads what this copy writes

        if entity not in ENTITIES:
            raise ValueError(f"unknown eTools REST entity: {entity}")
        self.entity = entity
        self.received: set[str] = set()  # the records a list received, or the ids a detail was asked by
        self.repeated = 0  # records a list returned again (each counted once)
        self.written: set[str] = set()
        self.seen: dict[str, Any] = {}  # what the list's first page said it had (EToolsClient.list's seen)
        self.fields = raw.KeyCount(entity)

    @property
    def is_list(self) -> bool:
        return not ENTITIES[self.entity][1]

    def offered(self, items: Iterable[Any], key: Callable[[Any], str] | None = None) -> Iterator[Any]:
        """``items`` as they come, each counted once: a list's by its record key, a detail's parents by
        ``key`` (the id it is asked by)."""
        for item in items:
            found = key(item) if key else record_key(item if isinstance(item, dict) else {})
            if found in self.received:
                self.repeated += 1
            else:
                self.received.add(found)
            yield item

    def keep(self, item: Any, *, asked: Any = "", parent: Any = "") -> None:
        """Write one record as it came (without contact fields) under its key."""
        from neurodb.integrations.etools.datamart_sync import id_value  # lazy: the Datamart sync

        if not isinstance(item, dict):
            raise ValueError("record is not an object")
        asked = str(asked or "")
        key = record_key(item, asked)
        data = catalogue.scrub(item)
        RestRecord.objects.update_or_create(
            entity=self.entity,
            record_key=key,
            defaults={
                "source_id": (id_value(item.get("id")) or asked)[:KEY_MAX],
                "reference": _text(data, REFERENCE.get(self.entity, "")),
                "title": _text(data, TITLE.get(self.entity, "")),
                "parent_key": str(parent or "")[:KEY_MAX],
                "data": data,
            },
        )
        if key not in self.written:
            self.written.add(key)
            self.fields.add(data)

    def finish(self, *, whole: bool) -> dict[str, Any]:
        """The run's details for this copy (what was received and kept, what eTools said it had, the
        fields), after the removal of a list read that ended (``whole``): the records of the entity it did not
        receive, and their details. A read that did not end, received nothing, or received fewer records than
        eTools said it had, removes nothing; a detail entity is never removed on its own."""
        details: dict[str, Any] = {"read": len(self.received), "kept": len(self.written)}
        if self.repeated:
            details["duplicates_read"] = self.repeated
        announced = self.seen.get("count")
        if isinstance(announced, int):
            details["announced"] = announced
        if self.is_list:
            why = ""
            if not whole:
                why = "the read did not end"
            elif not self.received:
                why = "eTools listed nothing"
            elif isinstance(announced, int) and len(self.received) < announced:
                why = f"eTools said it had {announced - len(self.received):,} more"
            if why:
                details.update(removed=0, removal_skipped=True, removal_skipped_why=why)
            else:
                details.update(self._remove())
        details["keys_kept"] = self.fields.details()
        return details

    def _remove(self) -> dict[str, int]:
        held = RestRecord.objects.filter(entity=self.entity)
        stale = list(held.exclude(record_key__in=self.received).values_list("record_key", flat=True))
        if not stale:
            return {"removed": 0}
        removed, _ = held.filter(record_key__in=stale).delete()
        found = {"removed": removed}
        child = DETAILS.get(self.entity)
        if child:
            gone, _ = RestRecord.objects.filter(entity=child, parent_key__in=stale).delete()
            found["details_removed"] = gone
        return found

    def into(self, details: dict[str, Any], *, whole: bool = True) -> dict[str, Any]:
        """``details`` (a run of the eTools REST sync's) with this copy's under ``raw_copies``: one run of
        that sync reads a list and its details."""
        details.setdefault("raw_copies", {})[self.entity] = self.finish(whole=whole)
        return details


# ------------------------------------------------------------------------------------------------- the job
def _label(item: Any) -> str:
    return str(item.get("id", "?")) if isinstance(item, dict) else str(item)


def _run(run: SyncRun, copy: RestCopy, read: Callable[[], None]) -> SyncRun:
    """Read one entity into its copy and finish its run by its counts; a read that fails is recorded FAILED
    and removes nothing."""
    try:
        read()
    except Exception as exc:
        return fail(run, exc, **copy.finish(whole=False))
    return finish_by_counts(run, **copy.finish(whole=True))


def read_list(run: SyncRun, entity: str, client: EToolsClient) -> SyncRun:
    """A list endpoint into its copy (as the eTools REST sync lists it)."""
    copy, path = RestCopy(entity), ENTITIES[entity][0]
    page_size = None if entity in ("rest_partners", "rest_agreements", "rest_interventions") else 1000

    def read() -> None:
        found = client.list(path, page_size=page_size, seen=copy.seen)
        process_items(run, copy.offered(found), copy.keep, _label)

    return _run(run, copy, read)


def read_action_points(run: SyncRun, client: EToolsClient) -> SyncRun:
    """The action points of every final engagement in the copy (as the eTools REST sync lists them), under
    their engagement."""
    copy, path = RestCopy("rest_action_points"), ENTITIES["rest_action_points"][0]
    final = (
        RestRecord.objects.filter(entity="rest_engagements")
        .annotate(_status=KT("data__status"))
        .filter(_status=ENGAGEMENT_FINAL)
        .order_by("pk")
        .values_list("record_key", flat=True)
    )

    def items() -> Iterator[tuple[str, dict[str, Any]]]:
        for engagement in list(final):
            said: dict[str, Any] = {}
            for item in client.list(path, {"engagement": engagement}, seen=said):
                yield engagement, item
            if isinstance(said.get("count"), int):
                copy.seen["count"] = copy.seen.get("count", 0) + said["count"]

    def read() -> None:
        found = copy.offered(items(), key=lambda pair: record_key(pair[1]))
        process_items(run, found, lambda pair: copy.keep(pair[1], parent=pair[0]), lambda p: _label(p[1]))

    return _run(run, copy, read)


def _details_of(entity: str) -> list[tuple[str, str]]:
    """The records whose details ``entity`` reads: (id, path), from the copy of their list."""
    from neurodb.integrations.etools.sync import ENGAGEMENT_DETAIL_PATHS

    template, parent = ENTITIES[entity]
    rows = RestRecord.objects.filter(entity=parent).order_by("pk")
    if entity == "rest_travel_details":  # the trips started in the last year
        since = (timezone.localdate() - dt.timedelta(days=DETAIL_DAYS)).isoformat()
        rows = rows.annotate(_start=KT("data__start_date")).filter(_start__gte=since)
    found = []
    for key, data in rows.values_list("source_id", "data"):
        if not key:
            continue  # a record without an eTools id: no detail to ask for
        if entity == "rest_engagement_details":
            kind = data.get("engagement_type") if isinstance(data, dict) else None
            if kind not in ENGAGEMENT_DETAIL_PATHS:
                continue  # an engagement type with no endpoint (the eTools REST sync skips it too)
            found.append((key, ENGAGEMENT_DETAIL_PATHS[kind].format(id=key)))
        else:
            found.append((key, template.format(id=key)))
    return found


def read_details(run: SyncRun, entity: str, client: EToolsClient) -> SyncRun:
    """The detail GET of each record of the list's copy into the detail's copy."""
    copy = RestCopy(entity)

    def read() -> None:
        def handle(pair: tuple[str, str]) -> None:
            key, path = pair
            copy.keep(client.get(path), asked=key, parent=key)

        asked = copy.offered(_details_of(entity), key=lambda pair: pair[0])
        process_items(run, asked, handle, lambda pair: pair[0])

    return _run(run, copy, read)


READS: dict[str, Callable[[SyncRun, EToolsClient], SyncRun]] = {
    "rest_partners": lambda run, client: read_list(run, "rest_partners", client),
    "rest_partner_details": lambda run, client: read_details(run, "rest_partner_details", client),
    "rest_agreements": lambda run, client: read_list(run, "rest_agreements", client),
    "rest_interventions": lambda run, client: read_list(run, "rest_interventions", client),
    "rest_intervention_details": lambda run, client: read_details(run, "rest_intervention_details", client),
    "rest_travels": lambda run, client: read_list(run, "rest_travels", client),
    "rest_travel_details": lambda run, client: read_details(run, "rest_travel_details", client),
    "rest_engagements": lambda run, client: read_list(run, "rest_engagements", client),
    "rest_engagement_details": lambda run, client: read_details(run, "rest_engagement_details", client),
    "rest_action_points": read_action_points,
}


def token_set() -> bool:
    return bool((settings.ETOOLS_TOKEN or "").strip())


def remember_names() -> None:
    """The names the copies hold, added to those NeuroDB takes out of texts (``query.remember_people`` reads
    them with the Datamart's records)."""
    from neurodb.datamart import query
    from neurodb.watch import people

    try:
        query.remember_people()
        people.forget()
    except Exception:
        logger.exception("eTools REST raw copy: the people the records name could not be read")


def read_all(
    *,
    only: Iterable[str] | None = None,
    triggered_by: str = "command",
    client: EToolsClient | None = None,
) -> list[SyncRun] | str:
    """Read every entity (or ``only`` these) into Raw eTools data, in :data:`ENTITIES`' order, one run each: a
    failed entity is recorded FAILED and the next one still reads. Returns the runs, or :data:`BUSY` when
    another read holds the lock (nothing is recorded). Without the eTools token, one failed run says so."""
    selected = set(only) if only else set(READS)
    unknown = selected - set(READS)
    if unknown:
        raise ValueError(f"unknown eTools REST entities: {', '.join(sorted(unknown))}")
    if not _lock():
        return BUSY
    try:
        if client is None and not token_set():
            run = new_run(SyncRun.Job.ETOOLS_REST_RAW, target="", triggered_by=triggered_by[:150])
            run.finish(SyncRun.Status.FAILED, error=NO_TOKEN)
            return [run]
        client = client or EToolsClient()
        runs: list[SyncRun] = []
        for entity, read in READS.items():
            if entity not in selected:
                continue
            run = new_run(SyncRun.Job.ETOOLS_REST_RAW, target=entity, triggered_by=triggered_by[:150])
            try:
                read(run, client)
            except Exception as exc:  # a failure outside the read itself: recorded, the next entity reads
                logger.exception("eTools REST raw copy: %s stopped", entity)
                if run.status == SyncRun.Status.RUNNING:
                    run.finish(SyncRun.Status.FAILED, error=describe_error(exc))
            runs.append(run)
        remember_names()
        return runs
    finally:
        _unlock()


def _lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    if connection.vendor != "postgresql":
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
    except DatabaseError:  # the session is gone, and its lock with it
        logger.warning("eTools REST raw copy: the lock could not be released", exc_info=True)
