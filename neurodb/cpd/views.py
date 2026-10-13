"""The Country Programme page (/country-programme/): where each CPD outcome, output and indicator
stands, with the interventions and partner reporting behind each output, and the cycle's documents."""

from __future__ import annotations

import datetime
import functools
from typing import Any

from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.web.parts import Part, render_page

from . import services
from .models import CountryProgramme, CPDocument, Indicator, Origin


def _cycle(request: HttpRequest, programmes: list[CountryProgramme]) -> CountryProgramme | None:
    wanted = request.GET.get("cycle", "")
    if wanted.isdigit():
        for programme in programmes:
            if programme.pk == int(wanted):
                return programme
    return next((p for p in programmes if p.current), programmes[0] if programmes else None)


def _chart_data(data: dict[str, Any]) -> dict[str, Any]:
    """The JSON of the summary part's status donut (``cpd-summary-data``), with the labels the charts read."""
    return {
        "status_counts": data["status_chart"],
        "labels": {k: _(v) for k, v in services.LABELS.items()},
    }


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """The cycle (``?cycle=<id>``, the current one by default): where each outcome, output and indicator
    stands.

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (the cycle
    selector, the cycle's panel with the share of the cycle elapsed and its documents, the note on
    AI-suggested indicators, the staff-only admin link) with a placeholder for each of its two parts, asked
    for at this address plus ``_part``, with the cycle written out and ``as_of``, the fingerprint of the
    figures the frame was drawn with:

    - ``summary`` (the key figures, where the indicators stand, the outcomes at a glance), the group's leader;
    - ``outcomes`` (each outcome with its outputs, indicators, programme documents and partner reporting);
      the ``#outcome-<id>`` links of the summary and of NeuroDB's knowledge graph live there.

    Both read one build (:func:`neurodb.cpd.services.build`, shared by every worker): when it is already
    stored, the frame draws both inline, so a warm page is one request; otherwise ``outcomes`` reads the build
    ``summary`` read for ``as_of``, even when an edit or a sync came before or between them. Without a cycle,
    or a cycle without indicators: the empty state, and no part at all.
    """
    programmes = list(CountryProgramme.objects.all())
    programme = _cycle(request, programmes)
    today = datetime.date.today()
    totals = (
        Indicator.objects.filter(programme=programme).aggregate(
            total=Count("pk"), unreviewed=Count("pk", filter=Q(origin=Origin.AI))
        )
        if programme
        else {"total": 0, "unreviewed": 0}
    )

    def shell() -> dict[str, Any]:
        return {
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
            # where the cycle stands in time, the indicators and those AI-suggested: no figure work
            "cycle": {
                **(services.cycle_facts(programme, today) if programme else {}),
                "indicators": totals["total"],
                "unreviewed": totals["unreviewed"],
            },
        }

    if programme is None or not totals["total"]:  # nothing to compute: the empty state, no parts
        return render_page(request, "cpd/dashboard.html", scope={}, shell=shell, parts={}, query="")

    # The figures the frame is drawn with (services.fingerprint): a part request reads them from its
    # address, so ``outcomes`` reads the build ``summary`` read even when an edit or a sync came before or
    # between them, while it is stored (compute.once_as_of). Any other request reads them now.
    as_of = (
        request.GET.get("as_of", "") if getattr(request, "part", None) else services.fingerprint(programme)
    )
    resolved = QueryDict(mutable=True)  # the cycle written out (the current one on a first load)
    resolved["cycle"] = str(programme.pk)
    if as_of:
        resolved["as_of"] = as_of
    data = functools.cache(lambda: services.build(programme, today, as_of=as_of))
    stored = functools.cache(lambda: services.stored(programme, today))
    parts = {
        "summary": Part(
            "cpd/parts/dashboard_summary.html",
            lambda: {"data": data(), "chart_data": _chart_data(data())},
            ready=stored,
        ),
        "outcomes": Part("cpd/parts/dashboard_outcomes.html", lambda: {"data": data()}, ready=stored),
    }
    return render_page(
        request, "cpd/dashboard.html", scope={}, shell=shell, parts=parts, query=resolved.urlencode()
    )


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
