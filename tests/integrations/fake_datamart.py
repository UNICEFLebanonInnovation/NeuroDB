"""A fake eTools Datamart list for the sync tests, registered on ``responses`` (no network)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import responses

PAGING = ("page", "page_size")  # the query keys that choose a page, never a filter


class FakeDatamart:
    """A Datamart list over ``records``, answering as DRF does: ``count``/``next``/``results`` pages
    (``page_size`` records each, ``page`` from 1) and exact filters, a record's value compared as text with
    the one asked. ``ignore``: filter keys it ignores, as DRF ignores an unknown parameter. ``requests``: the
    queries asked, for assertions."""

    def __init__(self, records: list[dict[str, Any]], *, ignore: tuple[str, ...] = ()) -> None:
        self.records, self.ignore = records, set(ignore)
        self.requests: list[dict[str, str]] = []
        self.url = ""

    def register(self, url: str) -> None:
        """Answer every GET of ``url`` (the dataset's list, with any query)."""
        self.url = url
        responses.add_callback(responses.GET, re.compile(rf"{re.escape(url)}(\?.*)?$"), callback=self._answer)

    def _matches(self, record: dict[str, Any], query: dict[str, str]) -> bool:
        return all(
            key in PAGING or key in self.ignore or str(record.get(key, "")) == value
            for key, value in query.items()
        )

    def _answer(self, request) -> tuple[int, dict[str, str], str]:
        query = dict(parse_qsl(urlsplit(request.url).query))
        self.requests.append(query)
        found = [record for record in self.records if self._matches(record, query)]
        size, page = int(query.get("page_size", 500)), int(query.get("page", 1))
        results = found[(page - 1) * size : page * size]
        more = page * size < len(found)
        following = f"{self.url}?{urlencode({**query, 'page': page + 1})}" if more else None
        return 200, {}, json.dumps({"count": len(found), "next": following, "results": results})


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
