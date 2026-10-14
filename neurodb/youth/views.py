"""The youth dashboard (/youth/): Compiler's counts per youth indicator, next to eTools reporting."""

from __future__ import annotations

import datetime
import functools
from typing import Any

from django.http import HttpRequest, HttpResponse, QueryDict
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.web.parts import Part, render_page

from . import compiler, services
from .models import YouthFigures

ALL = YouthFigures.ALL


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """The year's youth figures (``?year=``, the latest by default) under the filters (``master``,
    ``partner``, ``donor``, ``governorate``).

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (the filter bar,
    whose choices are read in the database without decoding the year's payload, and the line saying when
    Compiler's figures were read) with a placeholder for each of its two parts, asked for at this address plus
    ``_part``, with the year and the filters written out and ``as_of``, the fingerprint of the figures the
    frame was drawn with:

    - ``summary`` (the key figures and the youth indicators with their eTools indicators), the group's leader;
    - ``details`` (the breakdowns, Compiler and eTools side by side, the programme documents in Compiler).

    Both read one build (:func:`neurodb.youth.services.build`, shared by every worker): when it is already
    stored, the frame draws both inline, so a warm page is one request; otherwise ``details`` reads the build
    ``summary`` read for ``as_of``, even when a link was confirmed or a sync came before or between them.
    Without figures: the empty state, and no part at all.
    """
    years, every_year = services.stored_years()  # one query: the years, and whether "All years" is stored
    filters = services.YouthFilters.from_params(request.GET, years + ([ALL] if every_year else []))
    record = services.stored_row(filters.year) if filters.year else None
    today = datetime.date.today()

    def shell() -> dict[str, Any]:
        context: dict[str, Any] = {
            "page_title": _("Youth programmes"),
            "page_subtitle": _(
                "Young people reached under each youth indicator, counted in Compiler from the partners' "
                "registrations, next to what partners reported in eTools"
            ),
            "breadcrumbs": [{"label": _("Youth programmes"), "url": None}],
            "years": years,
            "every_year": every_year,  # the figures of all years together: "All years" in the year select
            "filters": filters,
            "configured": compiler.configured(),
            "record": record,
        }
        if record is None:
            context["data"] = None  # no figures: the empty state
        else:
            context["options"] = services.filter_options(record)
        return context

    if record is None:  # nothing to compute: the empty state, no parts
        return render_page(request, "youth/dashboard.html", scope={}, shell=shell, parts={}, query="")

    # The figures the frame is drawn with (services.fingerprint): a part request reads them from its address,
    # so ``details`` reads the build ``summary`` read even when a link was confirmed or a sync came before or
    # between them, while it is stored (compute.once_as_of). Any other request reads them now.
    as_of = (
        request.GET.get("as_of", "") if getattr(request, "part", None) else services.fingerprint(record.year)
    )
    resolved = QueryDict(mutable=True)  # the year written out (the latest on a first load), and each filter
    resolved["year"] = filters.year
    for name in ("master", "partner", "donor", "governorate"):
        if getattr(filters, name) is not None:
            resolved[name] = str(getattr(filters, name))
    if as_of:
        resolved["as_of"] = as_of
    data = functools.cache(lambda: services.build(record, filters, today, as_of=as_of))
    stored = functools.cache(lambda: services.stored(record, filters, today))
    parts = {
        "summary": Part("youth/parts/dashboard_summary.html", lambda: {"data": data()}, ready=stored),
        "details": Part("youth/parts/dashboard_details.html", lambda: {"data": data()}, ready=stored),
    }
    return render_page(
        request, "youth/dashboard.html", scope={}, shell=shell, parts=parts, query=resolved.urlencode()
    )
