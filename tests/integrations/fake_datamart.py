"""A fake eTools Datamart list for the sync tests, registered on ``responses`` (no network)."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import responses

PAGING = ("page", "page_size", "ordering")  # the query keys that choose a page or its order, never a filter
RANGES = ("__gte", "__lte", "__lt")  # the lookups of a range filter, the longest first


def _point(value: Any) -> Any:
    """A value as a range filter compares it: a number, else a day (a timestamp read in UTC, as the
    Datamart's database filters it), else its text; None when missing."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.UTC)
    return moment.date()


class FakeDatamart:
    """A Datamart list over ``records``, answering as DRF does: ``count``/``next``/``results`` pages
    (``page_size`` records each, ``page`` from 1), exact filters (a record's value compared as text with the
    one asked), ``__gte``/``__lt``/``__lte`` on dates and ids, ``ordering`` by a key (``-`` for the reverse).
    ``cap``: at most this many records to one query (``cut_count``: the count is cut too); ``ignore``: filter
    keys it ignores, as DRF ignores an unknown parameter (``ignore_on_next``: dropped from the ``next`` links
    only); ``refuse``: keys answered 400; ``matches``: a filter key compared with another record key (a filter
    on the wrong field: ``{"business_area": "office_code"}``); ``page_limit``: a page past it answers 404
    "Invalid page"; ``cut_pages``: those page numbers lose their last record (a page the Datamart cut, its
    count and its ``next`` link unchanged); ``changes``: (n, records) added after the n-th request.
    ``requests``: the queries asked, for assertions."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        cap: int | None = None,
        cut_count: bool = True,
        ignore: tuple[str, ...] = (),
        ignore_on_next: tuple[str, ...] = (),
        refuse: tuple[str, ...] = (),
        matches: dict[str, str] | None = None,
        page_limit: int | None = None,
        cut_pages: tuple[int, ...] = (),
        changes: tuple[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.records, self.ignore = list(records), set(ignore)
        self.cap, self.cut_count = cap, cut_count
        self.ignore_on_next, self.refuse, self.matches = set(ignore_on_next), set(refuse), dict(matches or {})
        self.page_limit, self.cut_pages, self.changes = page_limit, set(cut_pages), changes
        self.requests: list[dict[str, str]] = []
        self.url = ""

    def register(self, url: str) -> None:
        """Answer every GET of ``url`` (the dataset's list, with any query)."""
        self.url = url
        responses.add_callback(responses.GET, re.compile(rf"{re.escape(url)}(\?.*)?$"), callback=self._answer)

    def _keeps(self, record: dict[str, Any], key: str, value: str) -> bool:
        if key in PAGING or key in self.ignore:
            return True
        for lookup in RANGES:
            if key.endswith(lookup):
                have, wanted = _point(record.get(key.removesuffix(lookup))), _point(value)
                if have is None or type(have) is not type(wanted):
                    return False
                if lookup == "__gte":
                    return have >= wanted
                return have <= wanted if lookup == "__lte" else have < wanted
        return str(record.get(self.matches.get(key, key), "")) == value

    def _answer(self, request) -> tuple[int, dict[str, str], str]:
        query = dict(parse_qsl(urlsplit(request.url).query))
        self.requests.append(query)
        if self.changes and len(self.requests) > self.changes[0]:
            self.records.extend(self.changes[1])
            self.changes = None
        if set(query) & self.refuse:
            return 400, {}, json.dumps({"detail": "Invalid filter."})
        found = [
            record for record in self.records if all(self._keeps(record, k, v) for k, v in query.items())
        ]
        order = query.get("ordering", "")
        if order:
            found.sort(key=lambda r: _point(r.get(order.lstrip("-"))) or 0, reverse=order.startswith("-"))
        count = len(found)
        if self.cap is not None:
            found = found[: self.cap]
            count = len(found) if self.cut_count else count
        size = int(query.get("page_size", 500))
        page = (
            1 if "page" in self.ignore else int(query.get("page", 1))
        )  # a list that ignores the page number
        if self.page_limit is not None and page > self.page_limit:
            return 404, {}, json.dumps({"detail": "Invalid page."})
        results = found[(page - 1) * size : page * size]
        more = page * size < len(found)
        if page in self.cut_pages:
            results = results[:-1]
        kept = {k: v for k, v in query.items() if k not in self.ignore_on_next}
        following = f"{self.url}?{urlencode({**kept, 'page': page + 1})}" if more else None
        return 200, {}, json.dumps({"count": count, "next": following, "results": results})


def report(pk: int, start: str, **extra: Any) -> dict[str, Any]:
    """A partner report row (``prp/datareport``) of Partner A's programme document, of Lebanon."""
    return {
        "id": pk,
        "partner_vendor_number": "V1",
        "partner_name": "Partner A",
        "intervention_reference_number": "LEB/PCA-1/PD1",
        "report_type": "QPR",
        "report_number": "QPR1",
        "reporting_period_start_date": start,
        "performance_indicator": "Children reached",
        "country_name": "Lebanon",
        **extra,
    }


def v2(pk: int, area: Any, **extra: Any) -> dict[str, Any]:
    """A record of PRP's indicator reports (``prp/indicator-report-v2``), naming its business area as a code
    or as a name."""
    return {
        "id": pk,
        "business_area": area,
        "pd_reference_number": "LEB/PCA-1/PD1",
        "performance_indicator": "Children reached",
        "time_period_end": "2026-03-31",
        "partner": "V1",
        **extra,
    }
