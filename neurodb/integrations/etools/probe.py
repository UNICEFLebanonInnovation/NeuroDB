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
NO_FILTER, BY_BUSINESS_AREA, BY_COUNTRY = "no filter", "business area", "country"
CODE_SETTING = "Admin → Data and sync → eTools Datamart: PRP business area code"  # as checks.CODE_SETTING


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


# the business area code of the country's workspace as the last sync of ``workspaces`` stored it
held_business_area = sync.held_business_area


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


def _refused(answer: dict[str, Any], filter_key: str, code: str, name: str) -> str:
    """The verdict when the business area request was refused: the sync asks with that same filter."""
    status = answer.get("status")
    said = f" (HTTP {status})" if status else ""
    start = (
        f"The eTools Datamart refused the business area request ({filter_key}={code}){said}, and the sync of "
        f"{name} asks with that same filter, so it fails the same way"
    )
    if status in (401, 403):
        return (
            f"{start}: ask the team that runs NeuroDB's hosting to have the eTools Datamart team give "
            "NeuroDB's account access to it."
        )
    if status and 400 <= status < 500:
        return (
            f"{start}: check the code in {CODE_SETTING} against the codes the records name with no filter "
            "(listed below); if it is right, tell the eTools Datamart team that this dataset refuses it."
        )
    return f"{start}: probe it again later; if it is refused again, tell the eTools Datamart team."


def verdict(
    name: str,
    path: str,
    filter_key: str,
    code: str,
    answers: dict[str, dict[str, Any]],
    choice: sync.AreaCode | None = None,
) -> str:
    """What the answers mean, in plain words, and what to do from the admin. For a dataset the sync reads by
    business area, the business area answer is judged first: refused, ignored (its page names other codes,
    or it finds as many as no filter while that page names other codes) or kept to the code. ``choice``, the
    code as the sync resolves it: a value set aside (a country's name) is said first."""
    from neurodb.reports.services import BUSINESS_AREA_FROM

    choice = choice or sync.AreaCode(code)
    aside = sync.set_aside_words(choice.set_aside) if filter_key else ""
    if aside and code:
        where = BUSINESS_AREA_FROM.get(choice.source, "")
        first = (
            f"{aside[0].upper()}{aside[1:]}: the probe and the sync set it aside and use {code}"
            + (f", {where}" if where else "")
            + ". "
        )
    else:
        first = ""
    return first + _verdict(name, path, filter_key, code, answers, aside)


def _verdict(
    name: str, path: str, filter_key: str, code: str, answers: dict[str, dict[str, Any]], aside: str
) -> str:
    """:func:`verdict` after its first sentence; ``aside``, the values set aside in words."""
    country = (settings.ETOOLS_DATAMART_COUNTRY or "").strip()
    everything = _n(answers.get(NO_FILTER))
    area = answers.get(BY_BUSINESS_AREA) or {}
    by_area, by_country = _n(answers.get(BY_BUSINESS_AREA)), _n(answers.get(BY_COUNTRY))
    if all(_n(a) is None for a in answers.values()):
        statuses = {a.get("status") for a in answers.values()}
        if statuses & {401, 403}:
            return (
                f"The eTools Datamart refused NeuroDB's account for {path}: ask the team that runs NeuroDB's "
                "hosting to have the eTools Datamart team give that account access to it."
            )
        return f"The eTools Datamart did not answer for {path} (see each request's error): try again later."
    if everything == 0 and "error" not in area and not by_area and not by_country:
        return (
            f"The eTools Datamart holds no record at all in {path}, for any country: ask the eTools Datamart "
            "team whether this dataset is loaded."
        )
    if filter_key and not code:
        return (
            "No business area code is set"
            + (f" ({aside} (a code is digits, as 2490))" if aside else "")
            + f" and NeuroDB finds none for {country} in the workspace it holds or on the partner report "
            f"rows: enter it in {CODE_SETTING} (the codes the records name with no filter are listed below), "
            "then sync the dataset again."
        )
    if filter_key and "error" in area:
        return _refused(area, filter_key, code, name)
    if filter_key and by_area:
        codes = area.get("business_areas") or {}
        mine, unnamed = codes.get(code, 0), codes.get(sync.NO_CODE, 0)
        other = sum(n for c, n in codes.items() if c not in (code, sync.NO_CODE))
        elsewhere = any(
            c not in (code, sync.NO_CODE) for c in (answers.get(NO_FILTER) or {}).get("business_areas") or {}
        )
        if other or (by_area == everything and elsewhere):
            return (
                f"The eTools Datamart seems to ignore the business area filter ({filter_key}={code}) for "
                f"{path}: with it, it announces {by_area:,} records"
                + (f" ({everything:,} with no filter)" if everything is not None else "")
                + f", and of the {area.get('page', 0):,} records on its page, {mine:,} "
                f"{'names' if mine == 1 else 'name'} business area {code}, {other:,} another and {unnamed:,} "
                "none (listed below). The sync leaves out every "
                f"record that names another business area, so it keeps {country}'s only, but it reads every "
                f"country's: tell the eTools Datamart team that {path} ignores {filter_key}."
            )
        if not mine:
            return (
                f"The eTools Datamart announces {by_area:,} records with the business area filter "
                f"({filter_key}={code}), but none of the {area.get('page', 0):,} on its page names a "
                "business area, so whether the filter works cannot be told"
                + (
                    ": as many as with no filter, so it may ignore it. Tell the eTools Datamart team, and "
                    "read Data health for the records the sync kept that name no business area."
                    if by_area == everything
                    else f" ({everything:,} with no filter)."
                    if everything is not None
                    else "."
                )
            )
        return (
            f"The eTools Datamart has {by_area:,} records for business area {code}: the filter works (every "
            f"record on its page that names a business area names {code}). Sync {name} again (Sync eTools "
            "Datamart now → Only the datasets ticked below); if its run still keeps none, read its details "
            "for the records skipped as another business area."
        )
    if filter_key and by_area == 0:
        if by_country and (everything is None or by_country < everything):
            return (
                f"The business area filter ({filter_key}={code}) finds nothing, but the country filter "
                f"finds {by_country:,} records: check the code in {CODE_SETTING} against the codes listed "
                "below, and tell the team that runs NeuroDB that this dataset answers the country filter."
            )
        held = held_business_area(country)
        named = f"; the workspace NeuroDB holds names {held}" if held and held != code else ""
        return (
            f"The eTools Datamart holds {everything:,} records in {path}, none for business area {code}: "
            f"compare {code} with the codes the records name with no filter (listed below){named}. If "
            f"{country}'s code is among them under another form, enter it in {CODE_SETTING}; if none is "
            f"{country}'s, ask the eTools Datamart team whether {country}'s data is loaded."
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
        # the code as the sync resolves it, without its request to the workspaces: the probe asks nothing more
        choice = sync.resolve_business_area() if filter_key else sync.AreaCode()
        code = choice.code
        answers = {NO_FILTER: ask(client, path, NO_FILTER, {}, filter_key)}
        if filter_key and code:
            answers[BY_BUSINESS_AREA] = ask(client, path, BY_BUSINESS_AREA, {filter_key: code}, filter_key)
        answers[BY_COUNTRY] = ask(client, path, BY_COUNTRY, {"country_name": country}, filter_key)
    except Exception as exc:
        return fail(run, exc)
    run.rows_in = sum(a.get("page", 0) for a in answers.values())
    details: dict[str, Any] = {"dataset": name, "path": path, "requests": list(answers.values())}
    if filter_key:
        details.update(filter_key=filter_key, **choice.details())
    details["verdict"] = verdict(name, path, filter_key, code, answers, choice)
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
