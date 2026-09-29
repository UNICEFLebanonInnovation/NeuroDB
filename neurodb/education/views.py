"""The education dashboards: Makani (/education/makani/) and Dirasa (/education/dirasa/), one tab per page
of the programmes' Power BI dashboards; /education/ opens Makani."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.youth import compiler

from . import dirasa, makani, services

RESULTS = "edu-results"  # the element the slicer bar's HTMX requests replace


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    return redirect("education:makani")


@require_GET
def makani_page(request: HttpRequest) -> HttpResponse:
    return _page(request, makani.PAGE)


@require_GET
def dirasa_page(request: HttpRequest) -> HttpResponse:
    return _page(request, dirasa.PAGE)


def _page(request: HttpRequest, page: services.Page) -> HttpResponse:
    periods = services.periods(page.programme)
    period = request.GET.get(page.period_param, "")
    if period not in periods:
        period = periods[0] if periods else ""
    stored = services.record(page.programme, period) if period else None
    tab = page.tab(request.GET.get("tab"))
    context = {
        "page": page,
        "page_title": f"{page.title} {period}".strip(),
        "breadcrumbs": [{"label": _("Education programmes"), "url": None}, {"label": page.name, "url": None}],
        "periods": periods,
        "period": period,
        "tab": tab,
        "tab_template": f"education/tabs/{page.programme}_{tab}.html",
        "record": stored,
        "configured": compiler.configured(),
    }
    if stored is not None:
        context.update(services.page_data(stored, page, tab, request.GET))
    partial = request.htmx and request.htmx.target == RESULTS
    return render(request, "education/partials/results.html" if partial else "education/page.html", context)
