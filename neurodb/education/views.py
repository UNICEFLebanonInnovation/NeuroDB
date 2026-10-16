"""The education dashboards: Makani (/education/makani/) and Dirasa (/education/dirasa/), one tab per page
of the programmes' Power BI dashboards; /education/ opens Makani."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from neurodb.youth import compiler

from . import dirasa, makani, power_bi, services
from .models import EducationFigures

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
    stored_periods = power_bi.stored_periods([page.programme])
    periods = stored_periods.listed.get(page.programme, [])
    kind = stored_periods.kinds.get(page.programme, "year")
    param, label = services.period_field(page, kind)
    params = request.GET
    period = params.get(param, "")
    unlisted = ""  # an old link's round that no year shown lists
    if period not in periods:
        period = periods[0] if periods else ""
        old = params.get("round", "")
        if page.programme == "bridging" and param != "round" and old and not old.strip().isdigit():
            # an old link names a Dirasa round (?round=Bridging 2025-2026): the year holding it, round chosen
            found = services.year_of_round(page.programme, periods, old)
            if found is not None:
                period, round_id = found
                params = params.copy()
                params["round"] = str(round_id)
            else:
                unlisted = old.strip()
    stored = services.record(page.programme, period) if period else None
    tab = page.tab(params.get("tab"))
    context = {
        "page": page,
        "page_title": f"{page.title} {period}".strip(),
        "breadcrumbs": [{"label": _("Education and youth"), "url": None}, {"label": page.name, "url": None}],
        "periods": periods,
        "period": period,
        "period_param": param,
        "period_label": label,
        "tab": tab,
        "tab_template": f"education/tabs/{page.programme}_{tab}.html",
        "record": stored,
        "configured": compiler.configured(),
    }
    if stored is not None:
        context.update(services.page_data(stored, page, tab, params))
        if page.programme == "bridging" and param != "round" and not context.get("old_format"):
            shown = (context.get("filters") or {}, (context.get("data") or {}).get("errors") or [])
            line = context.get("rounds_line")
            context["round_note"] = _round_note(stored, page, tab, params, shown, unlisted, line)
    partial = request.htmx and request.htmx.target == RESULTS
    return render(request, "education/partials/results.html" if partial else "education/page.html", context)


def _round_note(
    stored: EducationFigures,
    page: services.Page,
    tab: str,
    params: QueryDict,
    shown: tuple[dict, list[str]],
    unlisted: str,
    line: dict | None = None,
) -> str:
    """Why the Dirasa page (shown by year) shows every round of the year when a round was asked for: an old
    link names a round no year shown lists (``unlisted``), Compiler could not count the registrations (no
    round can be chosen), the round has no registration nor teacher in the year (it is no option of the
    Round filter), or it is not a round of the year (kept from the year chosen before). When the round shown
    is a round of another year (or of no year) whose registrations Power BI's view counts in this one
    (``line``: :func:`neurodb.education.dirasa.rounds_line`, its ``other_year``), it says so: those are only
    the registrations counted in this year. "" when a round of the year is shown, none was asked for, or the
    tab has no Round filter (Outreach: Compiler's Kobo outreach has no round). ``shown``: the filters applied
    and the parts Compiler could not count."""
    year = stored.year
    filters, errors = shown
    if unlisted:
        text = _(
            "No year shown here lists the round “%(name)s”: the figures are those of every round of %(year)s."
        )
        return text % {"name": unlisted, "year": year}
    if "round" in filters:
        others = (line or {}).get("other_year") or []
        other = next((r for r in others if r.get("id") == filters["round"]), None)
        if other is None:
            return ""
        if other["year"]:
            text = _(
                "%(name)s is a round of %(round_year)s: shown here are only its registrations that Power "
                "BI's view counts in %(year)s."
            )
            return text % {"name": other["name"], "round_year": other["year"], "year": year}
        text = _(
            "%(name)s is a round in no year: shown here are only its registrations that Power BI's view "
            "counts in %(year)s."
        )
        return text % {"name": other["name"], "year": year}
    raw = params.get("round", "").strip()
    if not raw or not any(s.name == "round" for s in page.slicers_for(tab)):
        return ""
    if "enrolment" in errors:
        text = _(
            "Compiler could not count the registrations this time, so no round of %(year)s can be shown on "
            "its own."
        )
        return text % {"year": year}
    rounds = EducationFigures.objects.filter(pk=stored.pk).values_list("payload__rounds", flat=True).first()
    named = [r for r in rounds or [] if isinstance(r, dict)]
    entry = next((r for r in named if raw in (str(r.get("id")), str(r.get("name")))), None)
    if entry is not None:
        text = _(
            "%(name)s has no registrations or teachers in %(year)s: the figures are those of every round "
            "of the year."
        )
        return text % {"name": entry.get("name") or f"#{entry.get('id')}", "year": year}
    text = _("The round chosen is not a round of %(year)s: the figures are those of every round of the year.")
    return text % {"year": year}
