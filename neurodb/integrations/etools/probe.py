"""Probe one eTools Datamart dataset from the admin (*Run a job → Probe a Datamart dataset*): what the
Datamart answers with no filter, with the business area filter and with the country filter.

When a dataset's sync reads nothing, its run cannot tell a country office with no records from a filter
the Datamart does not take or a code it does not know. The probe asks the three questions side by side, at
most three requests of one small page each (:data:`PAGE` records), and writes nothing but its own run
(``SyncRun`` job ``datamart_probe``, target the dataset). For each request it keeps the filter sent, the
``count`` the Datamart announces, the records on the page, how many of them carry each key (key names and
counts), and the business area codes and country names they name (counts): never another value of a
record, no title, name or text. Its verdict (``details.verdict``) says in plain words what the answers mean
and what to do from the admin.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue
from neurodb.datamart.models import DatamartDocument
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import IntegrationError
from neurodb.integrations.runs import describe_error, fail, new_run

logger = logging.getLogger(__name__)

PAGE = 50  # the records asked for per request
VALUES = 10  # the business area codes and country names listed per request, the most frequent first
KEYS = 100  # the key names listed per request at most
LOCK_ID = 7140439  # one probe at a time; neurodb.integrations.background.DATAMART_PROBE_LOCK_ID
BUSY = "A probe of the eTools Datamart is already running."
DEFAULT = "prp_indicator_reports"
# where the business area code of the probe came from: the setting's sources, or the workspaces dataset held
FROM_HELD_WORKSPACES = "workspaces (held)"
NO_FILTER, BY_BUSINESS_AREA, BY_COUNTRY = "no filter", "business area", "country"


def datasets() -> list[str]:
    """The datasets a probe can ask about: every dataset the eTools Datamart sync reads, in its order."""
    return list(sync.ENTITY_SYNCS)


def endpoint(name: str) -> tuple[str, str]:
    """(Datamart path, business area filter key or "") of a dataset the sync reads."""
    from neurodb.integrations.etools.samples import LEGACY_PATHS

    spec = catalogue.DOCUMENTS.get(name)
    if spec is not None and spec.scope != "written_by":
        return spec.path, spec.filter_key if spec.scope == "business_area" else ""
    if name in sync.DATASETS:
        return sync.DATASETS[name][0], ""
    if name in sync.ENRICHMENTS:
        return sync.ENRICHMENTS[name][0], ""
    if name in LEGACY_PATHS:
        return LEGACY_PATHS[name], ""
    raise ValueError(f"unknown eTools Datamart dataset {name!r}")


def held_business_area(country: str) -> str:
    """The business area code of the country's workspace as the last sync of ``workspaces`` stored it: the
    probe asks the Datamart nothing more to find it."""
    wanted = sync.country_name(country)
    for data in DatamartDocument.objects.filter(dataset="workspaces").values_list("data", flat=True):
        if isinstance(data, dict) and sync.country_name(data.get("name")) == wanted:
            code = sync.area_code(data.get("business_area_code"))
            if code:
                return code
    return ""


def _short(value: Any, size: int) -> str:
    return " ".join(str(value).split())[:size]


def summary(results: list[dict[str, Any]], filter_key: str) -> dict[str, Any]:
    """What one page holds, as counts: how many records carry each key, and the business area codes and
    country names they name. No other value of a record."""
    keys: Counter[str] = Counter()
    for item in results:
        keys.update(str(k)[:60] for k, v in item.items() if v not in (None, "", [], {}))
    found: dict[str, Any] = {"keys": dict(sorted(keys.most_common(KEYS)))}
    if filter_key:
        codes = Counter(sync.area_code(item.get(filter_key))[:20] or sync.NO_CODE for item in results)
        found["business_areas"] = dict(codes.most_common(VALUES))
    countries = Counter(_short(item.get("country_name"), 40) for item in results if item.get("country_name"))
    if countries:
        found["countries"] = dict(countries.most_common(VALUES))
    return found


def ask(
    client: DatamartClient, path: str, label: str, query: dict[str, Any], filter_key: str
) -> dict[str, Any]:
    """One request of one page: the filter sent, the count announced, the records on the page and their
    :func:`summary`; on a refusal, its HTTP status and error instead."""
    answer: dict[str, Any] = {"request": label, "filter": query}
    try:
        count, results = client.first_page(path, {**query, "page_size": PAGE}, country=False)
    except IntegrationError as exc:
        return {**answer, "status": exc.status, "error": describe_error(exc)[:300]}
    return {**answer, "count": count, "page": len(results), **summary(results, filter_key)}


def _n(answer: dict[str, Any] | None) -> int | None:
    """The records a request found: the count announced, else those on its page; None when it failed."""
    if not answer or "error" in answer:
        return None
    return answer["count"] if answer.get("count") is not None else answer["page"]


def verdict(name: str, path: str, filter_key: str, code: str, answers: dict[str, dict[str, Any]]) -> str:
    """What the answers mean, in plain words, and what to do from the admin."""
    country = (settings.ETOOLS_DATAMART_COUNTRY or "").strip()
    everything = _n(answers.get(NO_FILTER))
    by_area, by_country = _n(answers.get(BY_BUSINESS_AREA)), _n(answers.get(BY_COUNTRY))
    if all(_n(a) is None for a in answers.values()):
        statuses = {a.get("status") for a in answers.values()}
        if statuses & {401, 403}:
            return (
                f"The eTools Datamart refused NeuroDB's account for {path}: ask the team that runs NeuroDB's "
                "hosting to have the eTools Datamart team give that account access to it."
            )
        return f"The eTools Datamart did not answer for {path} (see each request's error): try again later."
    if everything == 0:
        return (
            f"The eTools Datamart holds no record at all in {path}, for any country: ask the eTools Datamart "
            "team whether this dataset is loaded."
        )
    if filter_key and not code:
        return (
            f"No business area code is set and the workspaces dataset holds none for {country}: enter it in "
            "Admin → eTools Datamart: PRP business area code (the codes the records name with no filter are "
            "listed below), then sync the dataset again."
        )
    if filter_key and by_area:
        return (
            f"The eTools Datamart has {by_area:,} records for business area {code}: the filter works. "
            f"Sync {name} again (Sync eTools Datamart now → Only the datasets ticked below); if its run "
            "still keeps none, read its details for the records skipped as another business area."
        )
    if filter_key and by_area == 0:
        if by_country and (everything is None or by_country < everything):
            return (
                f"The business area filter ({filter_key}={code}) finds nothing, but the country filter "
                f"finds {by_country:,} records: check the code in Admin → eTools Datamart: PRP business area "
                "code against the codes listed below, and tell the team that runs NeuroDB that this dataset "
                "answers the country filter."
            )
        return (
            f"The eTools Datamart holds {everything:,} records in {path}, none for business area {code}: "
            f"compare {code} with the codes the records name with no filter (listed below). If {country}'s "
            "code is among them under another form, enter it in Admin → eTools Datamart: PRP business area "
            f"code; if none is {country}'s, ask the eTools Datamart team whether {country}'s data is loaded."
            if everything is not None
            else f"The business area filter ({filter_key}={code}) finds nothing: see each request below."
        )
    if by_country:
        ignored = everything is not None and by_country == everything
        return f"The eTools Datamart has {by_country:,} records for {country}" + (
            ": as many as with no filter, so it may ignore the country filter for this dataset (the sync "
            "keeps only the records that name this country or none)."
            if ignored
            else f" (of {everything:,} with no filter): the filter works."
            if everything is not None
            else "."
        )
    if by_country == 0:
        return (
            f"The eTools Datamart holds records in {path}, none for {country}: ask the eTools Datamart team "
            f"whether {country}'s data is loaded."
        )
    return "Some requests failed: see each request's error below."


def probe(name: str, run: SyncRun, *, client: DatamartClient | None = None) -> SyncRun:
    """Ask the Datamart about one dataset (:data:`NO_FILTER`, :data:`BY_BUSINESS_AREA` for a dataset filtered
    by business area, :data:`BY_COUNTRY`) and finish ``run`` with what it answered: SUCCEEDED when every
    request was answered, PARTIAL when one was refused (its error is kept), FAILED when the probe could not
    ask at all (no credentials, an unknown dataset)."""
    try:
        path, filter_key = endpoint(name)
        client = client or DatamartClient()
        country = (settings.ETOOLS_DATAMART_COUNTRY or "").strip()
        code, source = sync.business_area_setting() if filter_key else ("", "")
        if filter_key and not code:
            code = held_business_area(country)
            source = FROM_HELD_WORKSPACES if code else ""
        answers = {NO_FILTER: ask(client, path, NO_FILTER, {}, filter_key)}
        if filter_key and code:
            answers[BY_BUSINESS_AREA] = ask(client, path, BY_BUSINESS_AREA, {filter_key: code}, filter_key)
        answers[BY_COUNTRY] = ask(client, path, BY_COUNTRY, {"country_name": country}, filter_key)
    except Exception as exc:
        return fail(run, exc)
    run.rows_in = sum(a.get("page", 0) for a in answers.values())
    details: dict[str, Any] = {"dataset": name, "path": path, "requests": list(answers.values())}
    if filter_key:
        details.update(filter_key=filter_key, business_area=code, business_area_from=source)
    details["verdict"] = verdict(name, path, filter_key, code, answers)
    refused = any("error" in a for a in answers.values())
    run.finish(SyncRun.Status.PARTIAL if refused else SyncRun.Status.SUCCEEDED, **details)
    return run


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
        logger.warning("eTools Datamart probe: the lock could not be released", exc_info=True)


def run_job(name: str = DEFAULT, triggered_by: str = "command", *, client: DatamartClient | None = None):
    """Probe ``name`` under the probe's lock and return its run, or :data:`BUSY` when another probe holds the
    lock (nothing is recorded)."""
    if not _lock():
        return BUSY
    try:
        return probe(name, new_run(SyncRun.Job.DATAMART_PROBE, name[:64], triggered_by[:150]), client=client)
    finally:
        _unlock()
