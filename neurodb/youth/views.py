"""The youth dashboard (/youth/): Compiler's counts per youth indicator, next to eTools reporting."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from . import compiler, services


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    years = services.years()
    filters = services.YouthFilters.from_params(request.GET, years)
    record = services.load(filters.year) if filters.year else None
    context = {
        "page_title": _("Youth programmes"),
        "page_subtitle": _(
            "Young people reached under each youth indicator, counted in Compiler from the partners' "
            "registrations, next to what partners reported in eTools"
        ),
        "breadcrumbs": [{"label": _("Youth programmes"), "url": None}],
        "years": years,
        "filters": filters,
        "configured": compiler.configured(),
        "data": services.dashboard(record, filters) if record else None,
    }
    return render(request, "youth/dashboard.html", context)
