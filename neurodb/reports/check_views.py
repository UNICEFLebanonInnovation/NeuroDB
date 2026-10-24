"""Checking the figures (SPEC_validation): the figure trails (``datamart.trail``) and the Power BI
comparison sheet (``datamart.comparison``) of Progress reports, Partner monitoring and Home, for the people
who check the figures (``roles.can_validate``: superusers and the Validators group; no staff status
needed). Anyone else gets a 404, and a donor never reaches them (``DonorScopeMiddleware``).

* ``check/<page>/?<the page's filter>``: the figure trail of Progress reports (``progress-reports``) or of
  Home's Progress reports block (``home``, ``block=reporting``), and of Partner monitoring
  (``partner-monitoring``, ``view=`` the view it was opened from: grid, table, quality or map) or of Home's
  Indicator progress block (``home``, ``block=indicators``; ``datamart.trail_monitoring``). The page loads in
  parts: the frame (the filter in words, the day, the moment of the eTools figures, the sync running note) at
  once, then ``figures`` (the page's tiles, the steps, every figure on the page and rebuilt from the rows)
  and, folded until opened, Progress reports' ``locations`` and ``disaggregations`` (By location and By
  disaggregation) or Partner monitoring's ``performance`` and ``map`` (Program Performance and the map; open
  when the trail was opened from them). ``day``: the day the statuses are read for (the page's own by default,
  at most 14 days before); ``figure``: a figure to show open (the comparison sheet's Trail links); ``tab``:
  the page's tab the trail was opened from.
* ``check/<page>/rows.csv?<the filter>&what=<rows>``: the rows of a step or a figure
  (``datamart.trail_rows``; ``of=performance`` or ``of=map`` for those of Partner monitoring's folded parts),
  streamed, people never written; one of the two large downloads the site prepares at once
  (``raw.take_slot``).
* ``check/<page>/compare.xlsx?<the page's filter>&day=<YYYY-MM-DD>``: the comparison sheet; ``day`` is the day
  the statuses are read for (the page's own by default, at most 14 days before).
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.accounts.roles import can_validate
from neurodb.datamart import comparison, monitoring, raw, runfacts, trail, trail_rows
from neurodb.datamart import trail_monitoring as tm
from neurodb.web.parts import Part, render_page

from . import exports

TRAIL_PAGES = {
    "progress-reports": "reports:partner_reporting",
    "partner-monitoring": "reports:pd_monitoring",
    "home": "reports:overview",
}


def _plain(text: str, status: int) -> HttpResponse:
    return HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")


def _guard(request: HttpRequest, page: str) -> str:
    """The trail's Home block ("" for a page), or a 404: validators only, a page with a trail, Home with one
    of its blocks."""
    if not can_validate(request.user) or page not in trail.PAGES:
        raise Http404
    block = request.GET.get("block", "") if page == "home" else ""
    if page == "home" and block not in trail.HOME_BLOCKS:
        raise Http404
    return block


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


def trail_url(page: str, params: Any, **extra: str) -> str:
    """The figure trail of ``page`` for the filter of ``params`` (never the page shown, the sort or an
    export), with ``extra`` (Home's ``block``, the ``tab`` it was opened from, a ``figure``, a ``day``). No
    query."""
    query = trail.filter_query(params)
    for key, value in extra.items():
        if value:
            query[key] = value
    encoded = query.urlencode()
    return reverse("reports:check_figures", args=[page]) + (f"?{encoded}" if encoded else "")


# --------------------------------------------------------------------------------------------- the trail page
def _monitoring(page: str, block: str) -> bool:
    """Whether the trail is Partner monitoring's family: its page, or Home's Indicator progress block."""
    return page == "partner-monitoring" or block == "indicators"


@require_GET
def check_figures(request: HttpRequest, page: str) -> HttpResponse:
    """The figure trail of Progress reports, Partner monitoring or one of Home's blocks (see the module
    docstring)."""
    block = _guard(request, page)
    if _monitoring(page, block):
        return _monitoring_figures(request, page, block)
    day = comparison.statuses_day("progress-reports", request.GET.get("day"))
    if day is None:
        return _plain(comparison.REFUSED_DAY, 400)
    params = trail.filter_query(request.GET)
    tab = request.GET.get("tab", "") if page == "progress-reports" else ""
    tab = tab if tab in ("reports", "partners", *trail.TABLES) else ""
    figure = request.GET.get("figure", "")[:1000]
    today = comparison.page_today("progress-reports")
    query = params.copy()
    for key, value in (("block", block), ("tab", tab), ("figure", figure)):
        if value:
            query[key] = value
    if day != today:
        query["day"] = day.isoformat()
    rows_query = params.copy()
    if block:
        rows_query["block"] = block
    if day != today:
        rows_query["day"] = day.isoformat()
    scope = {
        "trail_page": page,
        "trail_block": block,
        "trail_tab": tab,
        "trail_figure": figure,
        "trail_day": day,
        "rows_url": reverse("reports:check_rows", args=[page]),
        "rows_query": rows_query.urlencode(),
    }

    def shell() -> dict[str, Any]:
        from neurodb.datamart import reporting

        name = trail.PAGES[page] if page != "home" else f"Home: {trail.HOME_BLOCKS[block]}"
        title = _("Check the figures of %(page)s") % {"page": name}
        run = runfacts.runs([trail.SOURCE])[trail.SOURCE][0]
        page_query = params.copy()
        if tab and tab != "reports":
            page_query["tab"] = tab
        encoded = page_query.urlencode()
        return {
            "page_title": title,
            "page_subtitle": _("Where each figure comes from, step by step, from what eTools said it had"),
            "breadcrumbs": [{"label": title, "url": None}],
            "filter_words": trail.filter_words(reporting.ReportFilters.from_params(params)),
            "as_of": runfacts.moment(run),
            "today": today,
            "running_since": trail.sync_running(),
            "page_url": reverse(TRAIL_PAGES[page]) + (f"?{encoded}" if encoded else ""),
            "check_compare": compare_link("progress-reports", params, today) if page != "home" else None,
        }

    def figures() -> dict[str, Any]:
        found = trail.progress_trail(page, params, day)
        return {
            "trail": found,
            "figure_count": len({f.key for f in found.figures}),
            "open_block": found.block_of(figure),
        }

    parts = {"figures": Part("reports/parts/check_figures.html", figures)}
    if page == "progress-reports":
        for name in trail.TABLES:
            parts[name] = Part(
                "reports/parts/check_table.html",
                lambda name=name: {"table": trail.table_trail(name, params, day)},
            )
    return render_page(
        request, "reports/check_figures.html", scope=scope, shell=shell, parts=parts, query=query.urlencode()
    )


def _monitoring_params(request: HttpRequest, page: str) -> tuple[QueryDict, str]:
    """The filter of a Partner monitoring trail (the page's resolved filter, as ``views.monitoring_filters``
    reads it; Home's block: the filter of Home's links) and the view it was opened from; a 404 for the master
    indicators (they have no trail)."""
    from neurodb.reports.views import monitoring_filters

    if page == "home":
        return trail.filter_query(request.GET), ""
    found = monitoring_filters(request)
    if found.view == "masters":
        raise Http404
    params = trail.filter_query(found.resolved)
    if found.view != "grid":
        params["view"] = found.view
    return params, found.view


def _monitoring_figures(request: HttpRequest, page: str, block: str) -> HttpResponse:
    day = comparison.statuses_day("partner-monitoring", request.GET.get("day"))
    if day is None:
        return _plain(comparison.REFUSED_DAY, 400)
    params, view = _monitoring_params(request, page)
    figure = request.GET.get("figure", "")[:1000]
    today = comparison.page_today("partner-monitoring")
    query = params.copy()
    for key, value in (("block", block), ("figure", figure)):
        if value:
            query[key] = value
    if day != today:
        query["day"] = day.isoformat()
    rows_query = params.copy()
    if block:
        rows_query["block"] = block
    if day != today:
        rows_query["day"] = day.isoformat()
    scope = {
        "trail_page": page,
        "trail_block": block,
        "trail_family": "monitoring",
        "trail_view": view,
        "trail_figure": figure,
        "trail_day": day,
        "rows_url": reverse("reports:check_rows", args=[page]),
        "rows_query": rows_query.urlencode(),
    }

    def shell() -> dict[str, Any]:
        name = trail.PAGES[page] if page != "home" else f"Home: {trail.HOME_BLOCKS[block]}"
        title = _("Check the figures of %(page)s") % {"page": name}
        page_query = params.copy()
        if view == "map":
            page_query.pop("view", None)
            path = reverse("reports:pd_monitoring_map")
        else:
            path = reverse(TRAIL_PAGES[page])
        encoded = page_query.urlencode()
        return {
            "page_title": title,
            "page_subtitle": _("Where each figure comes from, step by step, from what eTools said it had"),
            "breadcrumbs": [{"label": title, "url": None}],
            "filter_words": tm.filter_words(tm.filters_of(params)),
            "as_of": tm.moment(),
            "today": today,
            "running_since": trail.sync_running(),
            "page_url": path + (f"?{encoded}" if encoded else ""),
            "check_compare": compare_link("partner-monitoring", params, today) if page != "home" else None,
        }

    def figures() -> dict[str, Any]:
        found = tm.monitoring_trail(params, day, home=bool(block))
        return {
            "trail": found,
            "figure_count": len({f.key for f in found.figures}),
            "open_block": found.block_of(figure),
        }

    parts = {"figures": Part("reports/parts/check_monitoring.html", figures)}
    if page == "partner-monitoring":
        parts["performance"] = Part(
            "reports/parts/check_monitoring_table.html", lambda: {"table": tm.performance_trail(params, day)}
        )
        parts["map"] = Part(
            "reports/parts/check_monitoring_table.html", lambda: {"table": tm.map_trail(params, day)}
        )
    return render_page(
        request, "reports/check_figures.html", scope=scope, shell=shell, parts=parts, query=query.urlencode()
    )


# ------------------------------------------------------------------------------------------------- the rows
@require_GET
def check_rows(request: HttpRequest, page: str) -> HttpResponse:
    """The rows of a step or a figure of the trail (``what``): refused above the download limit (400), when
    two large downloads are being prepared already (503), or for rows the trail does not know (404)."""
    from neurodb.watch import people

    block = _guard(request, page)
    if _monitoring(page, block):
        return _monitoring_rows(request, page, block)
    day = comparison.statuses_day("progress-reports", request.GET.get("day"))
    if day is None:
        return _plain(comparison.REFUSED_DAY, 400)
    params = trail.filter_query(request.GET)
    what = request.GET.get("what", "")[:1000]
    kind = trail_rows.kind_of(what)
    if not kind or (kind == "lines" and page != "progress-reports"):
        raise Http404
    found = trail.progress_trail(page, params, day)
    pks: list[int] = []
    if kind == "rows":
        if trail_rows.selection(found, what) is None:
            raise Http404
        pks = trail_rows.row_pks(found, what)
        if len(pks) > raw.csv_limit(trail.SOURCE):
            return _plain(raw.too_many(trail.SOURCE, len(pks)), 400)
    slot = raw.take_slot()
    if slot is None:
        return _plain(raw.BUSY, 503)
    try:
        if kind == "rows":
            names = people.known_names()
            columns, keys = trail_rows.row_columns(names, pks)
            rows = trail_rows.stream_rows(found, what, pks, keys, names)
        elif kind == "reports":
            columns, rows = list(trail_rows.REPORT_COLUMNS), trail_rows.report_rows(found, what, params)
        else:
            tab = "locations" if what == "lines" else "disaggregations"
            columns = trail_rows.line_columns(tab)
            rows = trail_rows.line_rows(tab, params, day, found.as_of)
    except Exception:
        slot.free()
        raise

    def stream():
        try:
            yield from rows
        finally:
            slot.free()

    # a figure of one partner, report or programme document is named by its row on the trail, never by the
    # names its key holds
    named = what.startswith("figure:") and what[7:].startswith(trail.NAMED_KEYS)
    label = "trail_{}_{}".format(
        page, trail.anchor(what[7:]) if named else what.replace(":", "-").replace(".", "-")[:80]
    )
    response = exports.stream_csv(
        exports.export_filename(label, "csv", moment=found.as_of), columns, stream()
    )
    response._resource_closers.append(slot.free)  # a response closed before it was read frees it too
    return response


def _monitoring_rows(request: HttpRequest, page: str, block: str) -> HttpResponse:
    """The rows of a step or a figure of a Partner monitoring trail (``of``: Program Performance's or the
    map's part), refused as :func:`check_rows` refuses."""
    from neurodb.watch import people

    day = comparison.statuses_day("partner-monitoring", request.GET.get("day"))
    if day is None:
        return _plain(comparison.REFUSED_DAY, 400)
    params, _view = _monitoring_params(request, page)
    what = request.GET.get("what", "")[:1000]
    part = request.GET.get("of", "") if page == "partner-monitoring" else ""
    if part not in ("", *tm.TABLES):
        raise Http404
    if part == "performance":
        found: Any = tm.performance_trail(params, day)
        filters = dataclasses.replace(tm.filters_of(params), sections=[])
    elif part == "map":
        found = tm.map_trail(params, day)
        on_map = params.copy()
        on_map["view"] = "map"
        filters = tm.filters_of(on_map)
    else:
        found = tm.monitoring_trail(params, day, home=bool(block))
        filters = tm.filters_of(params)
    selection = trail_rows.pm_selection(found, what)
    if selection is None:
        raise Http404
    source, limit = trail_rows.pm_limit(selection)
    count = trail_rows.pm_count(selection)
    if count > limit:
        return _plain(raw.too_many(source, count), 400)
    slot = raw.take_slot()
    if slot is None:
        return _plain(raw.BUSY, 503)
    try:
        names = people.known_names()
        columns, keys = trail_rows.pm_columns(selection, names)
        reads = trail_rows.PageReads(
            filters=filters,
            rows=lambda: monitoring.stored_indicators(filters, day),
            points=lambda: monitoring.stored_map_points(filters, day),
            base_url=request.build_absolute_uri("/").rstrip("/"),
        )
        rows = trail_rows.pm_rows(found, selection, keys, names, reads)
    except Exception:
        slot.free()
        raise

    def stream():
        try:
            yield from rows
        finally:
            slot.free()

    named = what.startswith("figure:") and what[7:].startswith(trail.NAMED_KEYS)
    label = "trail_{}_{}".format(
        page, trail.anchor(what[7:]) if named else what.replace(":", "-").replace(".", "-")[:80]
    )
    response = exports.stream_csv(
        exports.export_filename(label, "csv", moment=trail_rows.found_moment(found)), columns, stream()
    )
    response._resource_closers.append(slot.free)
    return response
