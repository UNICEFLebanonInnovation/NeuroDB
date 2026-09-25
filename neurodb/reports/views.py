"""HTML pages of NeuroDB. Small function views: fetch the object, call a service, render.

Every page is login-required through ``LoginRequiredMiddleware``; HTMX requests receive a partial
template (``request.htmx``), plain requests the full page.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
import mimetypes
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from neurodb.accounts.roles import can_edit_section
from neurodb.core.models import PopulationFigure, SavedView
from neurodb.core.services import population as population_service
from neurodb.datamart import monitoring as pd_monitoring_service
from neurodb.datamart import services as datamart
from neurodb.datamart.models import AuditEngagement, AuditFinding
from neurodb.facts.services import dashboard as facts
from neurodb.facts.services import partners as partner_facts
from neurodb.indicators.models import Database, MasterIndicator, NeuroReport
from neurodb.indicators.services.tracking import LABELS
from neurodb.library.models import Resource
from neurodb.library.services import completed_maps, published_resource, resource_filters, search_resources
from neurodb.partnerships import services as partnerships
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import exports, services
from .forms import HPMCommentForm, SavedViewForm

PAGE_SIZE = 50


# ------------------------------------------------------------------------- helpers


def _database(pk: int) -> Database:
    return get_object_or_404(Database.objects.select_related("section", "reporting_year"), pk=pk)


def _report(pk: int) -> NeuroReport:
    return get_object_or_404(NeuroReport.objects.select_related("ryear"), pk=pk, is_active=True)


def _crumb(label: str, url: str | None = None) -> dict[str, str | None]:
    return {"label": label, "url": url}


def _database_crumbs(database: Database, *extra: dict[str, str | None]) -> list[dict[str, str | None]]:
    crumbs = [_crumb(_("Overview"), reverse("reports:overview"))]
    if database.section:
        crumbs.append(_crumb(database.section.name))
    crumbs.append(
        _crumb(database.label or database.name, reverse("reports:database_dashboard", args=[database.id]))
    )
    crumbs.extend(extra)
    return crumbs


def _database_actions(database: Database, current: str) -> list[dict[str, Any]]:
    """The dashboard header buttons of v2: analytical, map, snapshot, raw data."""
    items = [
        ("database_dashboard", _("Dashboard"), "grid"),
        ("database_analytical", _("Analytical view"), "table"),
        ("database_map", _("Intervention map"), "map"),
        ("database_snapshot", _("Snapshot"), "printer"),
    ]
    actions = [
        {
            "label": label,
            "url": reverse(f"reports:{name}", args=[database.id]),
            "icon": icon,
            "current": name == current,
        }
        for name, label, icon in items
    ]
    actions.append(
        {
            "label": _("Raw data"),
            "icon": "download",
            "menu": [
                {"label": "CSV", "url": reverse("reports:database_raw_data", args=[database.id, "csv"])},
                {"label": "Excel", "url": reverse("reports:database_raw_data", args=[database.id, "xlsx"])},
            ],
        }
    )
    return actions


def _paginate(request: HttpRequest, queryset, per_page: int = PAGE_SIZE):
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


# ------------------------------------------------------------------------- overview


@login_not_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    """Site root: the public landing page for visitors, the programme overview once signed in."""
    if not request.user.is_authenticated:
        from neurodb.web.views import landing

        return landing(request)
    return overview(request)


@require_GET
def overview(request: HttpRequest) -> HttpResponse:
    year = services.resolve_year(request.GET.get("year"))
    data = facts.overview(year)
    context = {
        "page_title": _("Programme overview"),
        "page_subtitle": _("Reporting year %(year)s") % {"year": year.name} if year else "",
        "breadcrumbs": [_crumb(_("Overview"))],
        "year": year,
        "data": data,
        "labels": LABELS,
        "chart_data": {"status_counts": data["status_counts"], "labels": LABELS},
    }
    return render(request, "reports/overview.html", context)


# ------------------------------------------------------------------------- databases


@require_GET
def database_dashboard(request: HttpRequest, pk: int) -> HttpResponse:
    database = _database(pk)
    dash = facts.database_dashboard(database)
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip().lower()
    indicators = dash.indicators
    if status in LABELS:
        indicators = [i for i in indicators if i.tracking == status]
    if q:
        indicators = [i for i in indicators if q in i.label.lower() or q in (i.awp_code or "").lower()]
    reported = sum(1 for i in dash.indicators if i.reports)
    context = {
        "page_title": database.label or database.name,
        "page_subtitle": database.section.name if database.section else "",
        "breadcrumbs": _database_crumbs(database),
        "actions": _database_actions(database, "database_dashboard"),
        "database": database,
        "dashboard": dash,
        "indicators": indicators,
        "reported": reported,
        "reporting_progress": round(reported * 100 / len(dash.indicators)) if dash.indicators else 0,
        "labels": LABELS,
        "status": status,
        "q": q,
        "chart_data": {"status_counts": dash.status_counts, "labels": LABELS, "monthly": dash.monthly},
    }
    template = "reports/partials/indicator_table.html" if request.htmx else "reports/database_dashboard.html"
    return render(request, template, context)


@require_GET
def database_analytical(request: HttpRequest, pk: int) -> HttpResponse:
    database = _database(pk)
    emergency = request.GET.get("emergency", "")
    if emergency and emergency not in services.EMERGENCY_VALUES:
        return HttpResponse(_("Invalid emergency filter."), status=400)
    page = "reports:database_analytical"
    context = {
        "page_title": _("%(name)s · Analytical view") % {"name": database.label or database.name},
        "page_subtitle": _("Pivot table over every ActivityInfo record of the year"),
        "breadcrumbs": _database_crumbs(database, _crumb(_("Analytical view"))),
        "actions": _database_actions(database, "database_analytical"),
        "database": database,
        "emergency": emergency,
        "saved_views": [
            services.saved_view_as_dict(v, request.user)
            for v in services.saved_views_for(request.user, page, database.id)
        ],
        "pivot_config": {
            "api": reverse("api:analytical", args=[database.id])
            + (f"?emergency={emergency}" if emergency else ""),
            "savedViewsApi": reverse("api:saved_views"),
            "page": page,
            "objectId": database.id,
            "exportName": f"{database.label or database.name}_indicators",
            "defaultPreset": "month",
        },
    }
    return render(request, "reports/database_analytical.html", context)


@require_GET
def database_snapshot(request: HttpRequest, pk: int) -> HttpResponse:
    database = _database(pk)
    snap = facts.snapshot(database)
    context = {
        "page_title": _("%(name)s · Snapshot") % {"name": database.label or database.name},
        "page_subtitle": _("Printable summary"),
        "breadcrumbs": _database_crumbs(database, _crumb(_("Snapshot"))),
        "actions": _database_actions(database, "database_snapshot"),
        "database": database,
        "snapshot": snap,
        "dashboard": snap["dashboard"],
        "labels": LABELS,
        "chart_data": {
            "status_counts": snap["dashboard"].status_counts,
            "labels": LABELS,
            "monthly": snap["dashboard"].monthly,
            "by_governorate": snap["by_governorate"],
        },
    }
    return render(request, "reports/database_snapshot.html", context)


@require_GET
def database_map(request: HttpRequest, pk: int) -> HttpResponse:
    database = _database(pk)
    level = request.GET.get("level", "governorate")
    if level not in services.MAP_LEVELS:
        return HttpResponse(_("Invalid map level."), status=400)
    filters = services.map_filters(request.GET)
    context = {
        "page_title": _("%(name)s · Intervention map") % {"name": database.label or database.name},
        "page_subtitle": _("Interventions by admin area and site"),
        "breadcrumbs": _database_crumbs(database, _crumb(_("Intervention map"))),
        "actions": _database_actions(database, "database_map"),
        "database": database,
        "level": level,
        "levels": services.MAP_LEVELS,
        "filters": filters,
        "options": facts.analytical_filters(database),
        "filter_fields": [
            ("partner", _("Partner")),
            ("pd", _("Programme document")),
            ("governorate", _("Governorate")),
            ("district", _("District")),
            ("month", _("Month")),
        ],
        "map_config": {"api": reverse("api:map", args=[database.id]), "level": level, "filters": filters},
    }
    return render(request, "reports/database_map.html", context)


@require_GET
def database_raw_data(request: HttpRequest, pk: int, fmt: str) -> HttpResponse:
    if fmt not in exports.FORMATS:
        raise Http404
    database = _database(pk)
    emergency = request.GET.get("emergency", "")
    if emergency and emergency not in services.EMERGENCY_VALUES:
        return HttpResponse(_("Invalid emergency filter."), status=400)
    return exports.analytical_export(database, fmt, emergency or None)


@require_GET
def indicator_detail(request: HttpRequest, pk: int, master_id: int) -> HttpResponse:
    database = _database(pk)
    master = get_object_or_404(MasterIndicator, pk=master_id, database=database)
    rows = facts.master_detail(database, master.id)
    context = {
        "page_title": master.name,
        "page_subtitle": _("Sub-indicators of %(code)s") % {"code": master.awp_code},
        "breadcrumbs": _database_crumbs(database, _crumb(master.name)),
        "database": database,
        "master": master,
        "rows": rows,
        "analytical_url": reverse("reports:database_analytical", args=[database.id]),
    }
    template = "reports/partials/indicator_detail.html" if request.htmx else "reports/indicator_detail.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- Neuro Reports / HPM


def _report_context(request: HttpRequest, report: NeuroReport) -> dict[str, Any]:
    month = services.parse_month(request.GET.get("month"))
    quarter = services.parse_quarter(request.GET.get("quarter"))
    data = facts.neuroreport(report, month=month, quarter=quarter)
    months = [{"value": m, "label": f"{m:02d}"} for m in range(1, 13)]
    return {
        "report": report,
        "data": data,
        "months": months,
        "quarters": services.QUARTERS,
        "labels": LABELS,
        "breadcrumbs": [_crumb(_("Overview"), reverse("reports:overview")), _crumb(report.name)],
    }


def _report_actions(report: NeuroReport, current: str) -> list[dict[str, Any]]:
    items = [
        ("report_dashboard", _("Dashboard"), "grid"),
        ("report_analytical", _("Analytical view"), "table"),
    ]
    if report.is_hpm:
        items.insert(1, ("report_hpm", _("HPM view"), "list"))
    return [
        {
            "label": label,
            "url": reverse(f"reports:{name}", args=[report.id]),
            "icon": icon,
            "current": name == current,
        }
        for name, label, icon in items
    ]


@require_GET
def report_dashboard(request: HttpRequest, pk: int) -> HttpResponse:
    report = _report(pk)
    context = _report_context(request, report)
    context.update(
        {
            "page_title": report.name,
            "page_subtitle": _("Values to the end of %(month)s %(year)s")
            % {"month": context["data"]["month_label"], "year": context["data"]["year"]},
            "actions": _report_actions(report, "report_dashboard"),
        }
    )
    template = "reports/partials/report_sections.html" if request.htmx else "reports/report_dashboard.html"
    return render(request, template, context)


@require_GET
def report_analytical(request: HttpRequest, pk: int) -> HttpResponse:
    report = _report(pk)
    page = "reports:report_analytical"
    context = {
        "page_title": _("%(name)s · Analytical view") % {"name": report.name},
        "page_subtitle": _("Pivot table over the ActivityInfo records behind this report"),
        "breadcrumbs": [
            _crumb(_("Overview"), reverse("reports:overview")),
            _crumb(report.name, reverse("reports:report_dashboard", args=[report.id])),
            _crumb(_("Analytical view")),
        ],
        "actions": _report_actions(report, "report_analytical"),
        "report": report,
        "saved_views": [
            services.saved_view_as_dict(v, request.user)
            for v in services.saved_views_for(request.user, page, report.id)
        ],
        "pivot_config": {
            "api": reverse("api:report_analytical", args=[report.id]),
            "savedViewsApi": reverse("api:saved_views"),
            "page": page,
            "objectId": report.id,
            "exportName": f"{report.name}_indicators",
            "defaultPreset": "month",
        },
    }
    return render(request, "reports/report_analytical.html", context)


def report_hpm(request: HttpRequest, pk: int) -> HttpResponse:
    report = _report(pk)
    if request.method == "POST":
        form = HPMCommentForm(report, request.POST)
        if not form.is_valid():
            messages.error(
                request, _("The comment could not be saved: %(errors)s") % {"errors": form.errors.as_text()}
            )
        else:
            link = form.cleaned_data["master"]
            section_id = link.master.database.section_id if link.master and link.master.database else None
            if not can_edit_section(request.user, section_id):
                raise PermissionDenied(_("You may only comment on indicators of your own section."))
            form.save()
            messages.success(request, _("Comment saved."))
        query = request.META.get("QUERY_STRING", "")
        return redirect(reverse("reports:report_hpm", args=[report.id]) + (f"?{query}" if query else ""))
    if request.method != "GET":
        return HttpResponse(status=405)
    context = _report_context(request, report)
    editable_sections = {
        s["database"].section_id
        for s in context["data"]["sections"]
        if can_edit_section(request.user, s["database"].section_id)
    }
    context.update(
        {
            "page_title": _("%(name)s · HPM") % {"name": report.name},
            "page_subtitle": _("Cut-off %(cutoff)s · values to the end of %(month)s")
            % {"cutoff": context["data"]["cutoff"].isoformat(), "month": context["data"]["month_label"]},
            "actions": _report_actions(report, "report_hpm"),
            "can_comment": bool(editable_sections),
            "editable_sections": editable_sections,
            "form": HPMCommentForm(report, initial={"related_month": f"{context['data']['month']:02d}"}),
        }
    )
    template = "reports/partials/hpm_sections.html" if request.htmx else "reports/report_hpm.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- programmes / donors / partners


@require_GET
def programmes(request: HttpRequest) -> HttpResponse:
    scope = request.GET.get("scope", "all")
    if scope not in services.PD_SCOPES:
        scope = "all"
    filters = partnerships.PDFilters.from_params(request.GET)
    page_obj = _paginate(request, partnerships.programme_documents(filters, scope=scope))
    counts = partnerships.pd_intervention_counts([pd.number for pd in page_obj if pd.number])
    rows = [services.pd_as_dict(pd, counts) for pd in page_obj]
    context = {
        "page_title": _("Programme documents"),
        "page_subtitle": _("eTools partnerships with their donors, grants and ActivityInfo interventions"),
        "breadcrumbs": [_crumb(_("Programmes"))],
        "actions": [
            {"label": _("Summary"), "url": reverse("reports:programme_summary"), "icon": "chart"},
            {
                "label": _("Planned locations (Excel)"),
                "url": reverse("reports:export_etools_locations"),
                "icon": "download",
            },
        ],
        "scope": scope,
        "page_obj": page_obj,
        "rows": rows,
        "options": partnerships.pd_filter_options(),
        "selected": {
            key: request.GET.getlist(key)
            for key in (
                "partner",
                "section",
                "office",
                "status",
                "donor",
                "grant",
                "document_type",
                "cso_type",
            )
        },
    }
    template = "reports/partials/programme_table.html" if request.htmx else "reports/programmes.html"
    return render(request, template, context)


@require_GET
def programme_summary(request: HttpRequest) -> HttpResponse:
    scope = request.GET.get("scope", "active")
    if scope not in services.PD_SCOPES:
        return HttpResponse(_("Invalid scope."), status=400)
    summary = partnerships.pd_summary(scope)
    context = {
        "page_title": _("Programme documents · Summary"),
        "page_subtitle": _("Active partnerships") if scope == "active" else _("All partnerships"),
        "breadcrumbs": [_crumb(_("Programmes"), reverse("reports:programmes")), _crumb(_("Summary"))],
        "scope": scope,
        "summary": summary,
        "chart_data": {"by_section": summary["by_section"][:20], "by_type": summary["by_type"]},
    }
    return render(request, "reports/programme_summary.html", context)


@require_GET
def programme_detail(request: HttpRequest, pk: int) -> HttpResponse:
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    detail = partnerships.pd_detail(pd)
    context = {
        "page_title": pd.number or pd.title,
        "page_subtitle": pd.title,
        "breadcrumbs": [
            _crumb(_("Programmes"), reverse("reports:programmes")),
            _crumb(pd.number or pd.title),
        ],
        "detail": detail,
        "pd": pd,
        "datamart": datamart.programme_datamart(pd),
    }
    template = "reports/partials/programme_detail.html" if request.htmx else "reports/programme_detail.html"
    return render(request, template, context)


@require_GET
def donors(request: HttpRequest) -> HttpResponse:
    filters = partnerships.PDFilters.from_params(request.GET)
    data = partnerships.donor_mapping(filters)
    context = {
        "page_title": _("Donors"),
        "page_subtitle": _("Funds by donor and year, planned versus actual locations"),
        "breadcrumbs": [_crumb(_("Donors"))],
        "data": data,
        "grants": datamart.grants_for_donors(filters.donors),
        "options": partnerships.pd_filter_options(),
        "selected": {
            key: request.GET.getlist(key) for key in ("donor", "grant", "partner", "section", "status")
        },
        "chart_data": {
            "funds_by_donor": data["funds_by_donor"],
            "funds_by_year": data["funds_by_year"],
            "interventions_by_governorate": data["interventions_by_governorate"],
        },
    }
    template = "reports/partials/donor_results.html" if request.htmx else "reports/donors.html"
    return render(request, template, context)


@require_GET
def partners(request: HttpRequest) -> HttpResponse:
    page_obj = _paginate(request, partnerships.partners(request.GET))
    context = {
        "page_title": _("Partners"),
        "page_subtitle": _("Implementing partners from eTools"),
        "breadcrumbs": [_crumb(_("Partners"))],
        "page_obj": page_obj,
        "options": partnerships.partner_filter_options(),
        "selected": {key: request.GET.getlist(key) for key in ("partner_type", "cso_type")},
        "q": request.GET.get("q", ""),
    }
    template = "reports/partials/partner_table.html" if request.htmx else "reports/partners.html"
    return render(request, template, context)


@require_GET
def partner_profile(request: HttpRequest, pk: int) -> HttpResponse:
    partner = get_object_or_404(PartnerOrganization, pk=pk, deleted_flag=False)
    profile = partnerships.partner_profile(partner)
    extra = datamart.partner_datamart(partner)
    activityinfo = partner_facts.partner_activityinfo(partner)
    context = {
        "page_title": partner.name,
        "page_subtitle": " · ".join(x for x in (partner.partner_type, partner.cso_type) if x),
        "breadcrumbs": [
            _crumb(_("Partners"), reverse("reports:partners")),
            _crumb(partner.short_name or partner.name),
        ],
        "partner": partner,
        "profile": profile,
        "datamart": extra,
        "activityinfo": activityinfo,
        "labels": LABELS,
        "chart_data": {
            # eTools Trips from the Datamart when synced, else the v2 travel tables
            "visits_by_year": extra["staff_visits_by_year"] or profile["visits_by_year"],
            "engagement_counts": profile["engagement_counts"],
            "activityinfo_by_year": activityinfo["by_year"],
            "etools_reports_by_year": extra["reports_by_year"],
        },
    }
    return render(request, "reports/partner_profile.html", context)


@require_GET
def partner_activityinfo(request: HttpRequest, pk: int, database_id: int) -> HttpResponse:
    """What one partner reported in one ActivityInfo database: master indicators by month (modal/page)."""
    partner = get_object_or_404(PartnerOrganization, pk=pk, deleted_flag=False)
    database = _database(database_id)
    data = partner_facts.partner_database_indicators(partner, database)
    context = {
        "page_title": _("%(partner)s in %(database)s")
        % {"partner": partner.short_name or partner.name, "database": database.label or database.name},
        "page_subtitle": _("ActivityInfo indicators reported by the partner, by month"),
        "breadcrumbs": [
            _crumb(_("Partners"), reverse("reports:partners")),
            _crumb(partner.short_name or partner.name, reverse("reports:partner_profile", args=[partner.id])),
            _crumb(database.label or database.name),
        ],
        "partner": partner,
        "database": database,
        "data": data,
    }
    template = (
        "reports/partials/partner_activityinfo.html" if request.htmx else "reports/partner_activityinfo.html"
    )
    return render(request, template, context)


# ------------------------------------------------------------------------- eTools Datamart pages


@require_GET
def assurance(request: HttpRequest) -> HttpResponse:
    data = datamart.assurance(request.GET)
    page_obj = _paginate(request, data["engagements"])
    context = {
        "page_title": _("Assurance"),
        "page_subtitle": _(
            "HACT audits, spot checks, micro-assessments and PSEA assessments from the eTools Datamart"
        ),
        "breadcrumbs": [_crumb(_("Assurance"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {key: request.GET.getlist(key) for key in ("type", "status")},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "type_labels": datamart.ENGAGEMENT_TYPES,
        "chart_data": {"by_type": data["by_type"]},
    }
    if not request.htmx:
        raw_year = request.GET.get("hact_year", "")
        context["hact"] = datamart.hact_compliance(int(raw_year) if raw_year.isdigit() else None)
        context["recent_findings"] = list(
            AuditFinding.objects.select_related("partner", "engagement").order_by("-created", "-datamart_id")[
                :15
            ]
        )
    template = "reports/partials/assurance_table.html" if request.htmx else "reports/assurance.html"
    return render(request, template, context)


@require_GET
def engagement_detail(request: HttpRequest, pk: int) -> HttpResponse:
    engagement = get_object_or_404(AuditEngagement.objects.select_related("partner"), pk=pk)
    detail = datamart.engagement_detail(engagement)
    title = engagement.reference_number or detail["engagement"].type_label
    context = {
        "page_title": title,
        "page_subtitle": " · ".join(
            x for x in (detail["engagement"].type_label, engagement.partner_name, engagement.auditor) if x
        ),
        "breadcrumbs": [_crumb(_("Assurance"), reverse("reports:assurance")), _crumb(title)],
        **detail,
    }
    return render(request, "reports/engagement_detail.html", context)


@require_GET
def funds(request: HttpRequest) -> HttpResponse:
    data = datamart.funds(request.GET)
    page_obj = _paginate(request, data["headers"])
    context = {
        "page_title": _("Funds"),
        "page_subtitle": _("Funds reservations, disbursements and grants by donor, from the eTools Datamart"),
        "breadcrumbs": [_crumb(_("Funds"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {key: request.GET.getlist(key) for key in ("donor", "grant")},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "chart_data": {
            "by_donor": data["by_donor"],
            "by_year": data["by_year"],
            "disbursed_by_year": data["disbursed_by_year"],
        },
    }
    template = "reports/partials/funds_table.html" if request.htmx else "reports/funds.html"
    return render(request, template, context)


@require_GET
def partner_reporting(request: HttpRequest) -> HttpResponse:
    data = datamart.partner_reporting(request.GET)
    page_obj = _paginate(request, data["reports"])
    context = {
        "page_title": _("Partner reporting"),
        "page_subtitle": _(
            "Progress reports submitted by partners in the Partner Reporting Portal, from the eTools Datamart"
        ),
        "breadcrumbs": [_crumb(_("Partner reporting"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {key: request.GET.getlist(key) for key in ("status", "report_type")},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "overdue": request.GET.get("overdue") == "1",
        "chart_data": {"by_status": data["by_status"]},
    }
    template = "reports/partials/reporting_table.html" if request.htmx else "reports/partner_reporting.html"
    return render(request, template, context)


@require_GET
def progress_report(request: HttpRequest) -> HttpResponse:
    """Quick view: the indicators of one progress report (``?report=<progress report id>``)."""
    rows = datamart.report_indicators(request.GET.get("report", "")[:300])
    if not rows:
        raise Http404
    report = rows[0]
    title = f"{report.pd_reference_number} · {report.report_number}"
    context = {
        "rows": rows,
        "report": report,
        "page_title": title,
        "page_subtitle": report.partner_name,
        "breadcrumbs": [_crumb(_("Partner reporting"), reverse("reports:partner_reporting")), _crumb(title)],
    }
    template = "reports/partials/progress_report.html" if request.htmx else "reports/progress_report.html"
    return render(request, template, context)


@require_GET
def monitoring(request: HttpRequest) -> HttpResponse:
    data = datamart.monitoring(request.GET)
    page_obj = _paginate(request, data["findings"])
    context = {
        "page_title": _("Field monitoring"),
        "page_subtitle": _(
            "Field monitoring findings and third-party monitoring visits from the eTools Datamart"
        ),
        "breadcrumbs": [_crumb(_("Field monitoring"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {"rating": request.GET.getlist("rating")},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "chart_data": {"by_rating": data["by_rating"], "by_month": data["by_month"]},
    }
    template = "reports/partials/monitoring_table.html" if request.htmx else "reports/monitoring.html"
    return render(request, template, context)


@require_GET
def action_points(request: HttpRequest) -> HttpResponse:
    data = datamart.action_points(request.GET)
    page_obj = _paginate(request, data["points"])
    context = {
        "page_title": _("Action points"),
        "page_subtitle": _(
            "Follow-up actions from audits, spot checks, visits and monitoring, from the eTools Datamart"
        ),
        "breadcrumbs": [_crumb(_("Action points"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {key: request.GET.getlist(key) for key in ("status", "module")},
        "q": request.GET.get("q", ""),
        "overdue": request.GET.get("overdue") == "1",
        "priority": request.GET.get("priority") == "1",
    }
    template = "reports/partials/action_point_table.html" if request.htmx else "reports/action_points.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- partner monitoring (eTools)


@require_GET
def pd_monitoring(request: HttpRequest) -> HttpResponse:
    filters = pd_monitoring_service.Filters.from_params(request.GET)
    options = pd_monitoring_service.filter_options(filters)
    own_section: list[str] = []
    if not request.GET:  # first load, no choice made yet: the user's own section
        own_section = pd_monitoring_service.default_sections(request.user, options["sections"])
        if own_section:
            filters = dataclasses.replace(filters, sections=own_section)
    rows = pd_monitoring_service.indicators(filters)
    data = pd_monitoring_service.summary(rows, filters)
    page_obj = _paginate(request, rows, pd_monitoring_service.PAGE_SIZE)
    context = {
        "page_title": _("Partner monitoring"),
        "page_subtitle": _(
            "What partners reported on each programme document indicator, by reporting period and location, "
            "against the target in the PD"
        ),
        "breadcrumbs": [_crumb(_("Partner monitoring"))],
        "filters": filters,
        "rows": rows,
        "page_obj": page_obj,
        "groups": pd_monitoring_service.grouped(list(page_obj.object_list)),
        "data": data,
        "months": pd_monitoring_service.MONTHS,
        "labels": LABELS,
        "options": options,
        "selected": {
            key: request.GET.getlist(key)
            for key in ("section", "partner", "pd", "location", *pd_monitoring_service.TAG_FIELDS)
        }
        | ({"section": own_section} if own_section else {}),
        "own_section": own_section,
        "chart_data": {"status_counts": data["status_counts"], "by_section": data["by_section"]},
        "truncated": len(rows) >= pd_monitoring_service.MAX_INDICATORS,
    }
    template = "reports/partials/monitoring_grid.html" if request.htmx else "reports/pd_monitoring.html"
    return render(request, template, context)


@require_GET
def pd_indicator(request: HttpRequest, pk: int, key: str) -> HttpResponse:
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    raw_year = request.GET.get("year", "")
    year = int(raw_year) if raw_year.isdigit() else datetime.date.today().year
    detail = pd_monitoring_service.indicator_detail(
        pd, key, (request.GET.get("report_type") or "").upper(), year
    )
    if detail is None:
        raise Http404
    title = detail["definition"].title
    context = {
        "page_title": title,
        "page_subtitle": " · ".join(
            x for x in (pd.number, pd.partner_name, detail["definition"].section_name) if x
        ),
        "breadcrumbs": [
            _crumb(_("Partner monitoring"), reverse("reports:pd_monitoring")),
            _crumb(pd.number or pd.title),
            _crumb(title),
        ],
        "labels": LABELS,
        "report_types": pd_monitoring_service.REPORT_TYPES,
        **detail,
    }
    template = "reports/partials/pd_indicator.html" if request.htmx else "reports/pd_indicator.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- population / library / maps


@require_GET
def population(request: HttpRequest) -> HttpResponse:
    years = population_service.available_years()
    raw_year = request.GET.get("year")
    year = int(raw_year) if raw_year and raw_year.isdigit() else (years[0] if years else None)
    view = request.GET.get("view", "total")
    if view not in services.POPULATION_VIEWS:
        return HttpResponse(_("Invalid population view."), status=400)
    data = population_service.population_view(year, view) if year else None
    nationality_labels = dict(PopulationFigure.Nationality.choices)
    context = {
        "page_title": _("Population figures"),
        "nationality_labels": nationality_labels,
        "national": (
            ([(_("All nationalities"), data["grand_total"])] if data and data["grand_total"] else [])
            + [
                (nationality_labels.get(code, code), value)
                for code, value in (data["totals_by_nationality"].items() if data else [])
            ]
        ),
        "page_subtitle": _("Estimates by nationality, governorate, district and age group"),
        "breadcrumbs": [_crumb(_("Population"))],
        "years": years,
        "population_year": year,
        "view": view,
        "views": [
            ("total", _("Total population")),
            ("children", _("Children")),
            ("vulnerable", _("Vulnerable population")),
        ],
        "data": data,
        "chart_data": {
            "totals_by_nationality": data["totals_by_nationality"],
            "by_governorate": data["by_governorate"],
        }
        if data
        else None,
    }
    return render(request, "reports/population.html", context)


@require_GET
def library(request: HttpRequest) -> HttpResponse:
    page_obj = search_resources(request.GET, request.GET.get("page", 1))
    context = {
        "page_title": _("Library"),
        "page_subtitle": _("Research, evaluations and knowledge products"),
        "breadcrumbs": [_crumb(_("Library"))],
        "page_obj": page_obj,
        "filters": resource_filters(),
        "selected": {key: request.GET.getlist(key) for key in ("year", "type", "topic", "section", "tag")},
        "q": request.GET.get("q", ""),
    }
    template = "reports/partials/library_results.html" if request.htmx else "reports/library.html"
    return render(request, template, context)


@require_GET
def library_download(request: HttpRequest, pk: int) -> HttpResponse:
    resource = get_object_or_404(Resource, pk=pk, published=True)
    if not resource.resource_file:
        raise Http404
    filename = resource.resource_file_name or f"resource-{resource.pk}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        io.BytesIO(bytes(resource.resource_file)),
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )


# Files shown inside the quick view. Anything else is offered as a download only: serving an
# uploaded HTML or SVG file inline would let it run script on this site.
INLINE_DOCUMENT_TYPES = {"application/pdf"}
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _guess_type(filename: str | None) -> str:
    return mimetypes.guess_type(filename or "")[0] or "application/octet-stream"


@require_GET
def library_item(request: HttpRequest, pk: int) -> HttpResponse:
    """Quick view of a resource: cover, full abstract, details and an inline PDF preview."""
    resource = published_resource(pk)
    kind = " · ".join(str(x.name) for x in (resource.type, resource.topic) if x)
    context = {
        "page_title": resource.title,
        "page_subtitle": kind,
        "breadcrumbs": [_crumb(_("Library"), reverse("reports:library")), _crumb(resource.title)],
        "resource": resource,
        "kind": kind or _("Resource"),
        "tags": list(resource.tags.all()),
        "pdf_preview": _guess_type(resource.resource_file_name) in INLINE_DOCUMENT_TYPES,
        "has_cover": _guess_type(resource.resource_image_name) in INLINE_IMAGE_TYPES,
    }
    template = "reports/partials/library_item.html" if request.htmx else "reports/library_item.html"
    return render(request, template, context)


def _resource_bytes(pk: int, field: str) -> tuple[Resource, bytes]:
    resource = get_object_or_404(Resource.objects.only("id", f"{field}_name", field), pk=pk, published=True)
    data = getattr(resource, field)
    if not data:
        raise Http404
    return resource, bytes(data)


@require_GET
@xframe_options_sameorigin
def library_file(request: HttpRequest, pk: int) -> HttpResponse:
    """The document inline, for the quick view's preview frame (PDF only)."""
    resource, data = _resource_bytes(pk, "resource_file")
    content_type = _guess_type(resource.resource_file_name)
    if content_type not in INLINE_DOCUMENT_TYPES:
        raise Http404
    response = FileResponse(
        io.BytesIO(data),
        as_attachment=False,
        filename=resource.resource_file_name or f"resource-{resource.pk}.pdf",
        content_type=content_type,
    )
    # Framed by our own pages only; the document itself may load nothing else.
    response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@require_GET
def library_cover(request: HttpRequest, pk: int) -> HttpResponse:
    resource, data = _resource_bytes(pk, "resource_image")
    content_type = _guess_type(resource.resource_image_name)
    if content_type not in INLINE_IMAGE_TYPES:
        raise Http404
    response = HttpResponse(data, content_type=content_type)
    response["Cache-Control"] = "public, max-age=86400"
    return response


def _allow_map_frames(request: HttpRequest) -> None:
    # Map products live on other sites (ArcGIS, Power BI, ...); let this page frame https pages.
    request.csp_frame_src = " https:"


@require_GET
def maps(request: HttpRequest) -> HttpResponse:
    _allow_map_frames(request)  # the quick view opens inside this page
    context = {
        "page_title": _("Maps"),
        "page_subtitle": _("Completed map products"),
        "breadcrumbs": [_crumb(_("Maps"))],
        "maps": list(completed_maps()),
    }
    return render(request, "reports/maps.html", context)


@require_GET
def map_item(request: HttpRequest, pk: int) -> HttpResponse:
    """Quick view of a map product: description, status and the map itself when its site allows it."""
    item = get_object_or_404(completed_maps(), pk=pk)
    _allow_map_frames(request)
    context = {
        "page_title": item.name,
        "page_subtitle": _("Map"),
        "breadcrumbs": [_crumb(_("Maps"), reverse("reports:maps")), _crumb(item.name)],
        "item": item,
        "host": urlsplit(item.link).hostname if item.link else "",
        "embed": bool(item.link and item.link.startswith("https://")),
    }
    template = "reports/partials/map_item.html" if request.htmx else "reports/map_item.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- data health / search


@require_GET
def data_health(request: HttpRequest) -> HttpResponse:
    health = services.data_health()
    context = {
        "page_title": _("Data health"),
        "page_subtitle": _("Sync runs and freshness of every source"),
        "breadcrumbs": [_crumb(_("Data health"))],
        "health": health,
    }
    return render(request, "reports/data_health.html", context)


QUESTION_WORDS = {
    "how", "what", "which", "who", "where", "when", "why", "is", "are", "do", "does", "did", "can",
    "list", "show", "compare", "give", "tell", "total", "top",
}  # fmt: skip


def _looks_like_question(q: str) -> bool:
    """Put "Ask NeuroDB AI" first for questions and longer phrases, last for short keyword searches."""
    words = q.lower().split()
    return q.endswith("?") or len(words) >= 4 or bool(words and words[0] in QUESTION_WORDS)


@require_GET
def search(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    year = services.resolve_year(None)
    groups = services.search(q, year) if q else []
    context = {
        "page_title": _("Search"),
        "page_subtitle": _("Indicators, databases and reports of %(year)s") % {"year": year.name}
        if year
        else "",
        "breadcrumbs": [_crumb(_("Search"))],
        "q": q,
        "groups": groups,
        "total": sum(len(g["items"]) for g in groups),
        "ai_enabled": settings.AI_ASSISTANT_ENABLED,
        "ask_first": _looks_like_question(q),
    }
    template = "reports/partials/search_results.html" if request.htmx else "reports/search.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- saved views / exports


@require_POST
def saved_view_save(request: HttpRequest) -> JsonResponse:
    form = SavedViewForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
    data = form.cleaned_data
    view, _created = SavedView.objects.update_or_create(
        owner=request.user,
        page=data["page"],
        object_id=data["object_id"],
        name=data["name"],
        defaults={"query": data["query"], "layout": data["layout"], "is_shared": data["is_shared"]},
    )
    return JsonResponse(services.saved_view_as_dict(view, request.user), status=201 if _created else 200)


@require_POST
def saved_view_delete(request: HttpRequest, pk: int) -> JsonResponse:
    view = get_object_or_404(SavedView, pk=pk)
    if not services.can_delete_saved_view(request.user, view):
        raise PermissionDenied
    view.delete()
    return JsonResponse({"deleted": pk})


@require_GET
def export_etools_locations(request: HttpRequest) -> HttpResponse:
    statuses = [s for s in request.GET.getlist("status") if s] or list(partnerships.ACTIVE_STATUSES)
    return exports.etools_locations_export(statuses)
