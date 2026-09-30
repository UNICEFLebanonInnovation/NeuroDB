"""The Country Programme page (/country-programme/): where each CPD outcome, output and indicator
stands, with the interventions and partner reporting behind each output, and the cycle's documents."""

from __future__ import annotations

from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from . import services
from .models import CountryProgramme, CPDocument, Indicator


def _cycle(request: HttpRequest, programmes: list[CountryProgramme]) -> CountryProgramme | None:
    wanted = request.GET.get("cycle", "")
    if wanted.isdigit():
        for programme in programmes:
            if programme.pk == int(wanted):
                return programme
    return next((p for p in programmes if p.current), programmes[0] if programmes else None)


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    programmes = list(CountryProgramme.objects.all())
    programme = _cycle(request, programmes)
    data = services.dashboard(programme) if programme else None
    context = {
        "page_title": _("Country programme"),
        "page_subtitle": _(
            "Whether the country programme is on track with its outcomes and outputs: each CPD indicator "
            "against its target, from partner reporting in eTools, ActivityInfo, Compiler and the values "
            "entered by the programme team"
        ),
        "breadcrumbs": [{"label": _("Country programme"), "url": None}],
        "programmes": programmes,
        "programme": programme,
        "documents": list(programme.documents.all()) if programme else [],
        "data": data,
        "chart_data": {
            "status_counts": data["status_chart"] if data else {},
            "labels": {k: _(v) for k, v in services.LABELS.items()},
        },
    }
    return render(request, "cpd/dashboard.html", context)


@require_GET
def indicator(request: HttpRequest, pk: int) -> HttpResponse:
    ind = get_object_or_404(
        Indicator.objects.select_related("programme", "outcome", "output__outcome").prefetch_related(
            "values", "milestones", "links"
        ),
        pk=pk,
    )
    detail = services.indicator_detail(ind)
    milestones = {m.year: m.value for m in ind.milestones.all()}
    rows = [
        {
            "year": year,
            "milestone": milestones.get(year),
            **detail.years.get(year, {"value": None, "parts": []}),
        }
        for year in ind.programme.years
    ]
    context = {
        "page_title": f"{ind.code} {ind.title}".strip(),
        "breadcrumbs": [
            {"label": _("Country programme"), "url": f"{reverse('cpd:dashboard')}?cycle={ind.programme_id}"},
            {"label": ind.code or _("Indicator"), "url": None},
        ],
        "indicator": ind,
        "progress": detail,
        "rows": rows,
        "suggested": [lk for lk in ind.links.all() if not lk.confirmed],
    }
    return render(request, "cpd/indicator.html", context)


@require_GET
def document(request: HttpRequest, pk: int) -> FileResponse:
    doc = get_object_or_404(CPDocument, pk=pk)
    try:
        handle = doc.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404(_("The file is missing from storage.")) from exc
    return FileResponse(handle, as_attachment=True, filename=doc.filename)
