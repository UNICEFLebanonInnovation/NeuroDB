"""Checking the figures (SPEC_validation): the Power BI comparison sheet of Progress reports and Partner
monitoring (``datamart.comparison``), for the people who check the figures (``roles.can_validate``: superusers
and the Validators group; no staff status needed). Anyone else gets a 404, and a donor never reaches it
(``DonorScopeMiddleware``).

* ``check/<page>/compare.xlsx?<the page's filter>&day=<YYYY-MM-DD>``: the sheet; ``day`` is the day the
  statuses are read for (the page's own by default, at most 14 days before). One of the two large downloads
  the site prepares at once (``raw.take_slot``).
"""

from __future__ import annotations

import datetime
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.http import require_GET

from neurodb.accounts.roles import can_validate
from neurodb.datamart import comparison, raw

from . import exports


def _plain(text: str, status: int) -> HttpResponse:
    return HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")


@require_GET
def check_compare(request: HttpRequest, page: str) -> HttpResponse:
    """The page's Power BI comparison sheet for the filter of the address: refused for a day outside the last
    14 days (400) or when two large downloads are being prepared already (503)."""
    if not can_validate(request.user) or page not in comparison.PAGES:
        raise Http404
    day = comparison.statuses_day(page, request.GET.get("day"))
    if day is None:
        return _plain(comparison.REFUSED_DAY, 400)
    slot = raw.take_slot()
    if slot is None:
        return _plain(raw.BUSY, 503)
    try:
        content = comparison.workbook(page, request.GET, day, request.build_absolute_uri("/").rstrip("/"))
        name = comparison.filename(page)
    finally:
        slot.free()
    response = HttpResponse(content, content_type=exports.XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


def compare_link(page: str, params: Any, today: datetime.date) -> dict[str, Any]:
    """What the check line needs to offer a page's comparison sheet (``reports/partials/_check_link.html``):
    the page, its filter as hidden fields (never the page shown, the sort or an export) and the days "statuses
    read for" can take (the page's day, back 14 days). No query."""
    query = comparison.filter_query(params)
    return {
        "page": page,
        "query": [(key, value) for key, values in query.lists() for value in values],
        "today": today,
        "first": today - datetime.timedelta(days=comparison.DAYS_BACK),
    }
