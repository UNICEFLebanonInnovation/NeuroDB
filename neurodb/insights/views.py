"""The year-end forecasts page: how good the method proved on past years, then every indicator."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.accounts.models import Section

from . import services
from .models import IndicatorForecast


@require_GET
def forecasts(request: HttpRequest) -> HttpResponse:
    run = services.latest_run()
    shown = services.shown(run)
    if "section" in request.GET:
        raw = request.GET.get("section", "")
        section_id = int(raw) if raw.isdigit() else None
    else:  # first visit: the user's own section
        section_id = getattr(request.user, "section_id", None)
    status = request.GET.get("status", "")
    filters = {}
    if section_id:
        filters["section_id"] = section_id
    if status in IndicatorForecast.Status.values:
        filters["status"] = status
    rows = list(services.forecasts(**filters)) if (shown or request.user.is_staff) else []
    counts = {}
    for f in IndicatorForecast.objects.filter(**{k: v for k, v in filters.items() if k == "section_id"}):
        counts[f.status] = counts.get(f.status, 0) + 1
    backtest = (run.details.get("backtest") if run else None) or {}
    as_of = run.details.get("as_of") if run else None
    context = {
        "page_title": _("Year-end forecasts"),
        "page_subtitle": _(
            "Where each ActivityInfo indicator will likely stand in December, learned from its own "
            "monthly pattern in past years. Estimates, with a range: check them against what you know."
        ),
        "breadcrumbs": [{"label": _("Year-end forecasts"), "url": None}],
        "run": run,
        "shown": shown,
        "backtest": backtest,
        "checks": sorted((int(k), v) for k, v in (backtest.get("by_month") or {}).items()),
        "year": run.details.get("year") if run else None,
        "as_of": services.MONTHS[as_of] if as_of else "",
        "rows": rows,
        "counts": counts,
        "statuses": IndicatorForecast.Status.choices,
        "status": status,
        "sections": Section.objects.order_by("name"),
        "section_id": section_id,
        "pills": services.STATUS_PILLS,
    }
    return render(request, "insights/forecasts.html", context)
