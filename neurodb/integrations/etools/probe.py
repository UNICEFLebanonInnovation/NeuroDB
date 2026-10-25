"""Probe one eTools Datamart dataset from the admin (*Run a job → Probe a Datamart dataset*): what the
Datamart answers with no filter, with the business area filter and with the country filter.

When a dataset's sync reads nothing, its run cannot tell a country office with no records from a filter
the Datamart does not take or a code it does not know. The probe asks the questions side by side, two or
three requests of one small page each (:data:`PAGE` records; the sync's own query too for the partner
reports), then the window requests below, and writes nothing but its own run
(``SyncRun`` job ``datamart_probe``, target the dataset). For each request it keeps the filter sent, the
``count`` the Datamart announces, the records on the page, how many of them carry each key (key names and
counts), and the business area codes and country names they name (counts): never another value of a
record, no title, name or text. Its verdict (``details.verdict``) says in plain words what the answers mean
and what to do from the admin.

Only when the sync would window the read (``windows.suspected``: a round count of
``windows.WINDOW_RECORDS`` records or more, or a cut sign in the dataset's last sync), it also asks what a
read in windows needs (``details.windows``, one small page each, with the sync's own filter): whether the
Datamart applies the window filters (a request that should find nothing finds nothing), whether its count or
its paging stops at a limit, and, for the partner reports, how many reports end before any NeuroDB holds. Per
request it keeps the count, the page length, the page number and query keys of its ``next`` link and the
records per start year of the page: counts, dates and key names only. A large read with no sign of a cut asks
none: the verdict says the sync reads it in one answer.

For a dataset whose business area filter the sync proves before using it (``catalogue.AREA_FILTERS``: the
partner reports, PRP's progress reports), it asks each filter key on one page of the sync's own query
(``details.area_filters``) and judges it as the sync does (``datamart_sync.judge_area_key``): the verdict says
which filter the Datamart applies, or that it applies none.

For a dataset whose last sync had records twice, or asked a stable order (``windows.order_wanted``), it asks
the ordering requests the sync asks before its read (``windows.prove_ordering``: two pages of ``ordering=id``
and the reverse on another, :data:`windows.ORDER_PAGE` records each, then the dataset's record identity) and
keeps, per key, its verdict, the page lengths and the records shared (``details.ordering_requests``): the
verdict says whether the Datamart takes the order, which the sync then asks for.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.models import Min
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue, runfacts
from neurodb.datamart import models as dm
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools import windows
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import parse_iso_date
from neurodb.integrations.http import IntegrationError, get_json
from neurodb.integrations.runs import describe_error, fail, new_run

logger = logging.getLogger(__name__)

PAGE = 50  # the records asked for per request
VALUES = 10  # the business area codes and country names listed per request, the most frequent first
KEYS = 100  # the key names listed per request at most
LOCK_ID = 7140439  # one probe at a time; neurodb.integrations.background.DATAMART_PROBE_LOCK_ID
BUSY = "A probe of the eTools Datamart is already running."
DEFAULT = "prp_indicator_reports"
NO_FILTER, BY_BUSINESS_AREA, BY_COUNTRY = "no filter", "business area", "country"
AS_SYNC = "as the sync asks"  # the partner reports' own query: the country and the date they are read from
FAR_FUTURE, FAR_PAST, FAR_PAST_LTE = "far future", "far past", "far past (lte)"
BEFORE_YEAR, FROM_YEAR = "before this year", "from this year"
IDS_BELOW, IDS_ABOVE, IDS_BELOW_LTE = "ids below 1", "ids from 10^15", "ids below 1 (lte)"
PAST_COUNT, END_PROOF, OLDER = "past the count", "end date far past", "older than held"
END_KEY = "reporting_period_end_date"  # the partner reports' period end, for the reports older than any held
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
    client: DatamartClient,
    path: str,
    label: str,
    query: dict[str, Any],
    filter_key: str,
    keep: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One request of one page: the filter sent, the count announced, the records on the page and their
    :func:`summary`; on a refusal, its HTTP status and error instead. ``keep``: the page's records are added
    to it (compared, never stored)."""
    answer: dict[str, Any] = {"request": label, "filter": query}
    try:
        count, results = client.first_page(path, {**query, "page_size": PAGE}, country=False)
    except IntegrationError as exc:
        return {**answer, "status": exc.status, "error": describe_error(exc)[:300]}
    if keep is not None:
        keep.extend(results)
    return {**answer, "count": count, "page": len(results), **summary(results, filter_key)}


def look(
    client: DatamartClient,
    path: str,
    label: str,
    query: dict[str, Any],
    date_key: str,
    keep: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One request of one page for the windows: the count announced, the records on the page, whether ``next``
    is given (its page number and the query keys it carries: does it keep the filters?) and, for ``date_key``,
    the records of the page per start year; on a refusal, its HTTP status and error."""
    answer: dict[str, Any] = {"request": label, "filter": query}
    try:
        payload = get_json(client.session, client.url(path), **{**query, "page_size": PAGE})
    except IntegrationError as exc:
        return {**answer, "status": exc.status, "error": describe_error(exc)[:300]}
    if isinstance(payload, list):
        count, results, following = None, payload, None
    elif isinstance(payload, dict) and "results" in payload:
        count, results, following = (
            sync._count(payload.get("count")),
            payload["results"] or [],
            payload.get("next"),
        )
    else:
        return {**answer, "error": "unrecognised list payload"}
    results = [item for item in results if isinstance(item, dict)]
    answer.update(count=count, page=len(results))
    if following:
        keys = dict(parse_qsl(urlsplit(str(following)).query))
        answer["next"] = {"page": keys.get("page", ""), "keys": sorted(k[:60] for k in keys)[:KEYS]}
    if date_key:
        years: Counter[str] = Counter()
        for item in results:
            try:
                day = parse_iso_date(item.get(date_key))
            except ValueError:
                day = None
            years[str(day.year) if day else "none"] += 1
        answer["years"] = dict(sorted(years.items()))
    if keep is not None:
        keep.extend(results)
    return answer


def _n(answer: dict[str, Any] | None) -> int | None:
    """The records a request found: the count announced, else those on its page; None when it failed."""
    if not answer or "error" in answer:
        return None
    return answer["count"] if answer.get("count") is not None else answer["page"]


def _refused(
    answer: dict[str, Any],
    filter_key: str,
    code: str,
    name: str,
    everything: int | None = None,
    by_country: int | None = None,
) -> str:
    """The verdict when the business area request was refused: the sync asks with that same filter. Another
    request of the probe answered (``everything``, with no filter; ``by_country``, with the country filter:
    None when refused), so a 403 refuses the account this business area, not the dataset."""
    status = answer.get("status")
    said = f" (HTTP {status})" if status else ""
    start = (
        f"The eTools Datamart refused the business area request ({filter_key}={code}){said}, and the sync of "
        f"{name} asks with that same filter, so it fails the same way"
    )
    if status == 403:  # signed in: the same account read the dataset with another request
        read = (
            f" with no filter ({everything:,} records)"
            if everything is not None
            else f" with the country filter ({by_country:,} records)"
            if by_country is not None
            else ""
        )
        listed = " with no filter (listed below)" if everything is not None else ""
        return (
            f"{start}: the eTools Datamart does not let the account NeuroDB uses ask this dataset for "
            f"business area {code}"
            + (f", though it reads it{read}" if read else "")
            + f". Check the code in {CODE_SETTING} against the codes the records name{listed}; if it is "
            f"right, ask the eTools Datamart team to give the account business area {code}."
        )
    if status == 401:
        return (
            f"{start}: the eTools Datamart refused NeuroDB's sign-in, whose credentials may have expired; "
            "ask the team that runs NeuroDB's hosting to renew them."
        )
    if status and 400 <= status < 500:
        return (
            f"{start}: check the code in {CODE_SETTING} against the codes the records name with no filter "
            "(listed below); if it is right, tell the eTools Datamart team that this dataset refuses it."
        )
    return f"{start}: probe it again later; if it is refused again, tell the eTools Datamart team."


def _refused_why(answers: dict[str, dict[str, Any]], name: str) -> str:
    """Why every request of a probe answered 403 refused it (``runfacts.refused``): the body names the
    sign-in, or the account signed in and not let read the dataset (the body says so, or the latest run of
    another eTools Datamart dataset read); else either."""
    errors = " ".join(str(a.get("error") or "") for a in answers.values())
    if runfacts.SIGN_IN_REFUSED.search(errors):
        return runfacts.SIGN_IN
    if runfacts.NO_PERMISSION.search(errors) or runfacts.signed_in_near(timezone.now(), name):
        return runfacts.DATASET
    return runfacts.EITHER


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
        why = runfacts.SIGN_IN if 401 in statuses else _refused_why(answers, name) if 403 in statuses else ""
        if why == runfacts.SIGN_IN:
            return (
                f"The eTools Datamart refused NeuroDB's sign-in for {path}: its credentials may have "
                "expired; ask the team that runs NeuroDB's hosting to renew them."
            )
        if why == runfacts.DATASET:  # signed in, not allowed this dataset
            return (
                f"The account NeuroDB uses for the eTools Datamart is not allowed to read {path}: ask the "
                "eTools Datamart team to grant it."
            )
        if why == runfacts.EITHER:
            return (
                f"The eTools Datamart refused NeuroDB's sign-in or {path} (HTTP 403): if every dataset fails "
                "this way, ask the team that runs NeuroDB's hosting to renew the credentials; if only this "
                "one, ask the eTools Datamart team to grant the account access to it."
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
        return _refused(area, filter_key, code, name, everything, by_country)
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


def _ids(results: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id")) for item in results if item.get("id") not in (None, "")]


def window_requests(
    client: DatamartClient,
    name: str,
    path: str,
    base: dict[str, Any],
    count: int | None,
    first: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """What a read of ``name`` in windows needs (:func:`look`, each with the sync's own query ``base``, whose
    first page announced ``count`` and held ``first``), asked only when the sync would window the read
    (``windows.suspected``): the date filters (partner reports: a far future and a far past start, ``__lte``
    when ``__lt`` is ignored, the reports before and from this year), the id filters, the page past the count
    whenever the count is known, and for the partner reports the reports that end before any NeuroDB holds,
    once the end-date filter is proven."""
    date_key = sync.DATASETS[name][1].windows if name in sync.DATASETS else ""
    asked: list[dict[str, Any]] = []

    def one(label: str, extra: dict[str, Any], keep: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        found = look(client, path, label, {**base, **extra}, date_key, keep)
        asked.append(found)
        return found

    if date_key:
        one(FAR_FUTURE, {f"{date_key}__gte": windows.FAR_FUTURE})
        if _n(one(FAR_PAST, {f"{date_key}__lt": windows.FAR_PAST})) != 0:
            one(FAR_PAST_LTE, {f"{date_key}__lte": windows.FAR_PAST})
        january = timezone.localdate().replace(month=1, day=1).isoformat()
        since = base.get(f"{date_key}__gte")
        one(BEFORE_YEAR, {f"{date_key}__lt": january, **({f"{date_key}__gte": since} if since else {})})
        one(FROM_YEAR, {f"{date_key}__gte": january})
    one(IDS_ABOVE, {"id__gte": windows.ID_TOP})
    if _n(one(IDS_BELOW, {"id__lt": 1})) != 0:
        one(IDS_BELOW_LTE, {"id__lte": 0})
    if count is not None:
        page: list[dict[str, Any]] = []
        past = one(PAST_COUNT, {"page": -(-count // PAGE) + 1}, page)  # the first page wholly past it
        past["same_as_first_page"] = bool(page) and _ids(page) == _ids(first)
    if name == "partner_reports":
        earliest = dm.ReportedIndicator.objects.aggregate(first=Min("period_end"))["first"]
        if earliest and _n(one(END_PROOF, {f"{END_KEY}__lt": windows.FAR_PAST})) == 0:
            one(OLDER, {f"{END_KEY}__lt": earliest.isoformat()})
    return asked


def _day(text: Any) -> str:
    day = parse_iso_date(text)
    return f"{day.day} {day:%b %Y}" if day else str(text)


def window_verdict(name: str, path: str, count: int | None, asked: list[dict[str, Any]]) -> str:
    """What the window requests mean, in plain words (:func:`window_requests`): which window filters the
    Datamart applies and so how NeuroDB reads the dataset, whether its count or its paging stops at a limit,
    and for the partner reports how many reports end before any NeuroDB holds."""
    by = {a["request"]: a for a in asked}
    date_key = sync.DATASETS[name][1].windows if name in sync.DATASETS else ""
    zero = {label: _n(a) == 0 for label, a in by.items()}
    ids = zero.get(IDS_ABOVE) and (zero.get(IDS_BELOW) or zero.get(IDS_BELOW_LTE))
    dates = date_key and zero.get(FAR_FUTURE) and (zero.get(FAR_PAST) or zero.get(FAR_PAST_LTE))
    said = []
    if dates:
        end = "__lt" if zero.get(FAR_PAST) else "__lte"
        said.append(
            f"The Datamart applies {date_key}{end} and __gte (requests that should find nothing found "
            "nothing): NeuroDB reads the partner reports in date windows."
        )
    elif date_key:
        ignored = f"{date_key}__gte" if not zero.get(FAR_FUTURE) else f"{date_key}__lt"
        found = _n(by.get(FAR_FUTURE)) if not zero.get(FAR_FUTURE) else _n(by.get(FAR_PAST))
        said.append(
            f"The Datamart ignores {ignored} (a request that should find nothing found {found or 0:,})"
            + (
                ": NeuroDB reads in id windows."
                if ids
                else f": NeuroDB cannot read more than one answer gives: tell the eTools Datamart team that "
                f"{path} ignores {ignored} and id__lt."
            )
        )
    elif ids:
        said.append(
            "The Datamart applies id__lt and id__gte (requests that should find nothing found nothing): "
            f"NeuroDB reads {path} in id windows."
        )
    else:
        said.append(
            f"The Datamart ignores id__lt or id__gte for {path}: NeuroDB cannot read more than one answer "
            f"gives; tell the eTools Datamart team that {path} ignores id__lt."
        )
    how = "NeuroDB reads it in windows" if dates or ids else "NeuroDB cannot read it whole"
    past = by.get(PAST_COUNT)
    # every page past the last one is refused (DRF): that says something of a limit only with a round count;
    # records found past the count are what proves it cut
    if past and past.get("status") in (400, 404) and count is not None and sync.capped(count):
        said.append(
            f"It answers no page past record {count:,}: it returns at most {count:,} records to one request; "
            f"{how}."
        )
    elif past and past.get("same_as_first_page"):
        said.append(
            f"The Datamart ignores the page number (page {past['filter'].get('page')} gives the first page's "
            "records): its paging says nothing of a limit."
        )
    elif past and past.get("page"):
        said.append(
            f"Page {past['filter'].get('page')}, past the {count:,} records announced, still holds records: "
            f"the count is cut, the Datamart holds more; {how}."
        )
    elif count is not None and sync.capped(count):
        said.append(
            f"It announces exactly {count:,} records: it returns at most {count:,} records to one request; "
            f"{how}."
        )
    before, after = _n(by.get(BEFORE_YEAR)), _n(by.get(FROM_YEAR))
    if dates and None not in (before, after, count) and before + after > count:
        said.append(
            f"The reports starting before this year ({before:,}) and from this year ({after:,}) add up to "
            f"more than the {count:,} announced: the count is cut."
        )
    proof, older = by.get(END_PROOF), by.get(OLDER)
    if proof and _n(proof):
        said.append(
            f"The Datamart ignores {END_KEY}__lt (a request that should find nothing found {_n(proof):,}): "
            "how many reports end before those NeuroDB holds cannot be told."
        )
    elif older and _n(older) is not None:
        day = _day(older["filter"].get(f"{END_KEY}__lt"))
        n = _n(older)
        said.append(
            f"It holds {n:,} reports that end before any NeuroDB holds ({day}): the last read did not get "
            "them."
            if n
            else f"It holds no report that ends before those NeuroDB holds ({day})."
        )
    return " ".join(said)


def no_window_verdict(name: str, count: int) -> str:
    """The verdict of a large read the sync does not window (no round count, no sign of a cut last night)."""
    return (
        f"It announces {count:,} records, not a round count, and the last sync of {name} saw no sign of a "
        "cut: NeuroDB reads it in one answer, as the sync does (no window request asked)."
    )


def _held(name: str) -> int:
    """The rows NeuroDB holds in the scope the sync of ``name`` replaces (the partner reports of the window it
    reads; a dataset's records kept whole)."""
    if name in sync.DATASETS:
        spec = sync.DATASETS[name][1]
        rows = spec.model.objects.all()
        return (spec.scope(rows, spec.since()) if spec.scope and spec.since else rows).count()
    return dm.DatamartDocument.objects.filter(dataset=name).count()


def area_requests(
    client: DatamartClient, name: str, path: str, base: dict[str, Any], code: str, unfiltered: int | None
) -> list[dict[str, Any]]:
    """Each business area filter key of ``catalogue.AREA_FILTERS[name]`` on one page of the sync's own query
    ``base`` (:func:`ask`, its codes counted under the key the records name their code by), judged as the
    sync judges it (``datamart_sync.judge_area_key``, against ``unfiltered``, the count with no filter, and
    the rows NeuroDB holds): each answer with its ``key`` and ``verdict``. Counts and codes only."""
    spec, held, asked = catalogue.AREA_FILTERS[name], _held(name), []
    for key in spec.keys:
        records: list[dict[str, Any]] = []
        found = ask(client, path, f"business area: {key}", {**base, key: code}, spec.named_by, records)
        if "error" in found:
            status = found.get("status")
            verdict = "refused" if status and 400 <= status < 500 else "failed"
        else:
            verdict = sync.judge_area_key(
                found.get("count"),
                records,
                unfiltered=unfiltered,
                held=held,
                code=code,
                named_by=spec.named_by,
            )
        asked.append({**found, "key": key, "verdict": verdict})
    return asked


def area_verdict(path: str, code: str, asked: list[dict[str, Any]], unfiltered: int | None) -> str:
    """What the business area requests mean (:func:`area_requests`): the filter the Datamart applies, which
    the sync reads by, or that it applies none of those tried, and what each did."""
    proven = next((a for a in asked if a["verdict"] == "proven"), None)
    if proven is not None:
        without = f", {unfiltered:,} without" if isinstance(unfiltered, int) else ""
        return (
            f"The Datamart applies {proven['key']} to {path} ({proven['count']:,} records with it{without}; "
            f"every record on its page names {code}): the sync reads by it."
        )
    tried = ", ".join(sync.tried_words(a) for a in asked)
    return (
        f"The Datamart applies none of the business area filters tried for {path}: {tried}. The sync reads "
        f"every country's records and keeps {code}'s."
    )


def order_requests(
    client: DatamartClient, name: str, path: str, base: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The ordering requests the sync of ``name`` asks before its read (``windows.prove_ordering``, with the
    sync's own query ``base``), only when its last finished run had records twice or asked an order: the
    order proven or why none, and each key tried (counts only). None asked otherwise."""
    before = sync.last_details(name)
    if not windows.order_wanted(before):
        return {}, []
    keys = ("id", str(before.get("record_identity") or ""))
    known = before.get("datamart_count")
    return windows.prove_ordering(client, path, base, country=False, keys=keys, known=known)


def order_verdict(path: str, tried: list[dict[str, Any]]) -> str:
    """What the ordering requests mean (:func:`order_requests`): the stable order the Datamart takes, which
    the sync asks for after a read whose records came twice, or that it takes none of those tried."""
    said = []
    for t in tried:
        key, verdict = t.get("key"), t.get("verdict")
        if verdict == "proven":
            how = (
                "two pages in order, the reverse on another"
                if t.get("carried")
                else "two pages that share no record, the reverse on another"
            )
            held = t.get("shared_held")
            if held:  # the two pages shared only records one answer of both lists as often: held twice
                how += f"; the {held:,} {'record' if held == 1 else 'records'} they share it holds twice"
            said.append(
                f"The Datamart takes ordering={key} for {path} ({how}): the sync asks for it after a read "
                "whose records came twice."
            )
        elif verdict in ("refused", "failed"):
            status = f" (HTTP {t['status']})" if t.get("status") else ""
            word = "refuses" if verdict == "refused" else "did not answer"
            said.append(f"The Datamart {word} ordering={key}{status}: the sync does not ask it.")
        elif verdict == "too_small":
            said.append(
                f"{path} holds {2 * windows.ORDER_PAGE:,} records or fewer: no stable order is asked (too "
                "few to shift)."
            )
        else:
            how = (
                "the reverse order gives the same records"
                if t.get("reverse_shared")
                else "its two pages share records"
                if t.get("shared")
                else "a page came back empty"
                if not all(t.get("pages") or [0])
                else "its pages are not in order"
            )
            said.append(
                f"The Datamart ignores ordering={key} ({how}): the sync reads in its own order, and a read "
                "whose records come twice removes nothing, unless every copy of each came in one answer."
            )
    return " ".join(said)


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
        by_area = name in catalogue.AREA_FILTERS  # a business area filter the sync proves before it uses it
        choice = sync.resolve_business_area() if filter_key or by_area else sync.AreaCode()
        code = choice.code
        first: dict[str, list[dict[str, Any]]] = {NO_FILTER: [], BY_BUSINESS_AREA: [], BY_COUNTRY: []}
        answers = {NO_FILTER: ask(client, path, NO_FILTER, {}, filter_key, first[NO_FILTER])}
        if filter_key and code:
            answers[BY_BUSINESS_AREA] = ask(
                client, path, BY_BUSINESS_AREA, {filter_key: code}, filter_key, first[BY_BUSINESS_AREA]
            )
        answers[BY_COUNTRY] = ask(
            client, path, BY_COUNTRY, {"country_name": country}, filter_key, first[BY_COUNTRY]
        )
        # the sync's own query, whose count decides whether it reads in windows
        if name == "partner_reports":
            base = {"country_name": country, **sync._reporting_window(sync._reporting_since())}
            first[AS_SYNC] = []
            answers[AS_SYNC] = ask(client, path, AS_SYNC, base, filter_key, first[AS_SYNC])
            own = AS_SYNC
        elif by_area or (name in catalogue.DOCUMENTS and catalogue.DOCUMENTS[name].scope == "lookup"):
            base, own = {}, NO_FILTER  # PRP's progress reports: read with no filter until one is proven
        elif filter_key:
            base, own = {filter_key: code}, BY_BUSINESS_AREA
        else:
            base, own = {"country_name": country}, BY_COUNTRY
        count = (answers.get(own) or {}).get("count")
        answered = own in answers and "error" not in answers[own]
        why = windows.suspected(name, count) if answered and isinstance(count, int) else ""
        looked = window_requests(client, name, path, base, count, first.get(own, [])) if why else []
        areas = area_requests(client, name, path, base, code, count) if by_area and code and answered else []
        _, ordered = order_requests(client, name, path, base)
    except Exception as exc:
        return fail(run, exc)
    run.rows_in = sum(a.get("page", 0) for a in answers.values())
    details: dict[str, Any] = {"dataset": name, "path": path, "requests": list(answers.values())}
    if filter_key:
        details.update(filter_key=filter_key, **choice.details())
    details["verdict"] = verdict(name, path, filter_key, code, answers, choice)
    if looked:
        details.update(windows=looked, windows_why=why)
        details["verdict"] += " " + window_verdict(name, path, count, looked)
    elif isinstance(count, int) and count >= windows.WINDOW_RECORDS:
        details["verdict"] += " " + no_window_verdict(name, count)
    if areas:
        details["area_filters"] = areas
        details["verdict"] += " " + area_verdict(path, code, areas, count)
    if ordered:
        details["ordering_requests"] = ordered
        details["verdict"] += " " + order_verdict(path, ordered)
    # a page past the count that is refused is what the probe asks (a paging limit), and a business area key
    # refused is an answer too: neither is a refusal of the probe
    refused = (
        any("error" in a for a in answers.values())
        or any("error" in a for a in looked if a["request"] != PAST_COUNT)
        or any(a.get("verdict") == "failed" for a in [*areas, *ordered])
    )
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
