"""The Education page (/education/): Makani and Bridging children, by partner and place."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.youth import compiler

from . import services
from .models import EducationFigures


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    available = services.available()
    filters = services.EducationFilters.from_params(request.GET, available)
    record = (
        EducationFigures.objects.filter(programme=filters.programme, year=filters.year).first()
        if filters.programme
        else None
    )
    context = {
        "page_title": _("Education programmes"),
        "page_subtitle": _(
            "Children registered by the education partners in Compiler (Makani and Bridging), counted in "
            "Compiler: by partner, place, sex, age and the programmes' own categories"
        ),
        "breadcrumbs": [{"label": _("Education programmes"), "url": None}],
        "available": available,
        "programme_labels": services.labels(),
        "filters": filters,
        "configured": compiler.configured(),
        "data": services.dashboard(record, filters) if record else None,
    }
    return render(request, "education/dashboard.html", context)
