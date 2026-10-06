"""The action points page (``/action-points/``) and what opens from it: the eTools action points with
their filters, charts, CSV and Excel exports and printable report; one action point's details (every
field, the action taken, the AI's verdict and the PME verifications); the AI review and the AI content
summary; and the NeuroDB action points (FMS §10).

What comes from Monitoring insights (the visit of a field monitoring action point and its link
confidence, the AI's verdicts, the verifications, the NeuroDB action points) is read through
``neurodb.fmm.action_points``, imported lazily: with that app switched off the page shows the eTools
action points as before.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.datamart import monitoring as pd_monitoring_service
from neurodb.datamart import services as datamart
from neurodb.datamart.models import ActionPoint

from . import exports

PAGE_SIZE = 50
EXPORT_KEYS = ("export", "all", "page")


def _crumb(label: str, url: str | None = None) -> dict[str, str | None]:
    return {"label": label, "url": url}


def _fmm():
    """``neurodb.fmm.action_points`` when Monitoring insights is on, else None."""
    return datamart._fmm_points()


def _need_fmm():
    found = _fmm()
    if found is None:
        raise Http404
    return found


def _params(request: HttpRequest) -> tuple[QueryDict, list[str]]:
    """The page's filter: the query, or on the first load (no query at all) the user's own section,
    as the other pages choose it."""
    if request.GET:
        return request.GET, []
    own = pd_monitoring_service.default_sections(request.user, datamart._distinct_ap("section"))
    params = QueryDict(mutable=True)
    if own:
        params.setlist("section", own)
    return params, own


def _query(params: QueryDict, **changes: Any) -> str:
    """``params`` without the export and page keys, with ``changes`` (None removes a key)."""
    out = params.copy()
    for key in EXPORT_KEYS:
        out.pop(key, None)
    for key, value in changes.items():
        out.pop(key, None)
        if value is not None:
            out[key] = value
    return out.urlencode()


# ------------------------------------------------------------------------------------------ the page
# The Excel export's columns: the CSV's, without who an action point is assigned to (the CSV keeps its
# established column)
XLSX_COLUMNS = tuple(c for c in datamart.AP_EXPORT_COLUMNS if c != "assigned_to")
CLEANED = ("description", "action_taken")  # texts written by people: cleaned in the Excel file


def _xlsx_rows(points):
    """The CSV's rows without the assignee, the description and the action taken cleaned of names,
    e-mail addresses, links and phone numbers (``fmm.privacy.clean``)."""
    from neurodb.fmm import privacy
    from neurodb.watch import people

    names = people.known_names(refresh=True)  # read afresh: the file holds whole descriptions
    for row in datamart.action_point_rows(points):
        row.pop("assigned_to", None)
        for key in CLEANED:
            row[key] = privacy.clean(row.get(key), 32_000, names)[0] if row.get(key) else ""
        yield row


@require_GET
def action_points(request: HttpRequest) -> HttpResponse:
    params, own_section = _params(request)
    export = request.GET.get("export")
    if export in ("csv", "xlsx"):
        everything = request.GET.get("all") == "1"
        if everything:
            points = ActionPoint.objects.select_related("partner", "intervention")
        else:
            points = datamart.filtered_action_points(params)[0]
        label = "action-points" if everything else "action-points-filtered"
        if export == "xlsx":
            return exports.xlsx_response(
                exports.export_filename(label, "xlsx"),
                [("Action points", XLSX_COLUMNS, _xlsx_rows(points))],
                typed=True,
            )
        return exports.stream_csv(
            exports.export_filename(label, "csv"),
            datamart.AP_EXPORT_COLUMNS,
            datamart.action_point_rows(points),
        )
    data = datamart.action_points(params)
    from django.core.paginator import Paginator

    page_obj = Paginator(data["points"], PAGE_SIZE).get_page(request.GET.get("page"))
    rows = list(page_obj.object_list)
    fmm_points = _fmm()
    extras = _extras(request, rows, fmm_points)
    charts = datamart.action_point_charts(data["points"])
    query = _query(params)
    base = reverse("reports:action_points")
    selected = {key: params.getlist(key) for key in ("status", "module", "office", "section", "partner")}
    context = {
        "page_title": _("Action points"),
        "page_subtitle": _(
            "Follow-up actions from audits, spot checks, visits and monitoring, from the eTools Datamart, "
            "with NeuroDB's own action points"
        ),
        "breadcrumbs": [_crumb(_("Action points"))],
        "data": data,
        "page_obj": page_obj,
        "rows": rows,
        "selected": selected,
        "own_section": own_section,
        "f": data["filters"],
        "q": params.get("q", ""),
        "overdue": data["filters"]["overdue"],
        "priority": data["filters"]["priority"],
        "chart_data": charts,
        "drills": _drills(params, data["filters"]),
        "query": query,
        "chart_hrefs": _chart_hrefs(params),
        "downloads": [
            {"label": _("CSV of the filter"), "url": f"{base}?{_query(params, export='csv')}"},
            {"label": _("CSV of every action point"), "url": f"{base}?export=csv&all=1"},
            {"label": _("Excel of the filter"), "url": f"{base}?{_query(params, export='xlsx')}"},
            {"label": _("Excel of every action point"), "url": f"{base}?export=xlsx&all=1"},
            {
                "label": _("PDF report"),
                "url": f"{reverse('reports:action_points_report')}?{query}",
                "icon": "printer",
                "new_tab": True,
            },
        ],
        **extras,
    }
    template = "reports/partials/action_point_table.html" if request.htmx else "reports/action_points.html"
    return render(request, template, context)


def _extras(request: HttpRequest, rows: list[ActionPoint], fmm_points) -> dict[str, Any]:
    """What Monitoring insights adds to the rows on screen and to the page's AI panel."""
    if fmm_points is None:
        return {"fmm_on": False}
    from neurodb.fmm.access import is_admin
    from neurodb.fmm.ai import ap_review, ap_summary
    from neurodb.fmm.models import ActionPointReview, ActionPointVerification

    links = fmm_points.visit_links([p.pk for p in rows])
    reviews = fmm_points.current_reviews(rows)
    checks = fmm_points.latest_verifications([p.datamart_id for p in rows])
    for p in rows:
        p.visit_link = links.get(p.pk)
        p.is_fm = (p.related_module or "").lower() == "fm"
        p.confidence = p.visit_link["confidence"] if p.visit_link else ("unmatched" if p.is_fm else "")
        p.confidence_label = fmm_points.CONFIDENCE_LABELS.get(p.confidence, "")
        p.confidence_help = fmm_points.CONFIDENCE_HELP.get(p.confidence, "")
        p.review = reviews.get(p.datamart_id)
        p.check = checks.get(p.datamart_id)
        p.completed = fmm_points.is_completed(p.status)
    used, allowed = ap_summary.quota(request.user)
    return {
        "fmm_on": True,
        "can_run_review": is_admin(request.user),
        "review_on": ap_review.switched_on()[0],
        "review_status": ap_review.last_run(),
        "batch_sizes": ap_review.BATCH_SIZES,
        "default_batch": ap_review.DEFAULT_BATCH,
        "summary_on": ap_summary.available(),
        "summary_quota": {"used": used, "allowed": allowed},
        "verdicts": ActionPointReview.Verdict.choices,
        "verdict_counts": ap_review.counts(),
        "pme_states": [*ActionPointVerification.State.choices, ("none", _("Not verified yet"))],
        "link_levels": list(fmm_points.CONFIDENCE_LABELS.items()),
        "local": fmm_points.local_points(request.GET, request.user),
        "can_add_local": fmm_points.can_add_local(request.user),
    }


def _chart_hrefs(params: QueryDict) -> dict[str, str]:
    """What a click on each chart's bar opens: the filter with the bar's value in place of the chart's
    own key ({drill}: the value the bar carries; {series_drill}: raised or completed)."""
    base = reverse("reports:action_points")

    def href(drop: tuple[str, ...], tail: str) -> str:
        query = _query(params, **dict.fromkeys(drop))
        return f"{base}?{query}&{tail}" if query else f"{base}?{tail}"

    return {
        "status": href(("status",), "status={drill}"),
        "due": href(("due",), "due={drill}"),
        "timeliness": href(("timeliness",), "timeliness={drill}"),
        "office": href(("office", "status"), "office={drill}&status=open"),
        "section": href(("section", "status"), "section={drill}&status=open"),
        "monthly": href(("raised", "completed"), "{series_drill}={drill}"),
    }


def _drills(params: QueryDict, f: dict[str, Any]) -> list[dict[str, str]]:
    """The filters a chart click or a link set that the filter bar does not show, as removable chips."""
    base = reverse("reports:action_points")
    labels = {
        "due": lambda v: datamart.AP_DUE.get(v, v),
        "timeliness": lambda v: datamart.AP_TIMELINESS.get(v, v),
        "raised": lambda v: _("Raised in %(month)s") % {"month": v},
        "completed": lambda v: _("Completed in %(month)s") % {"month": v},
    }
    out = []
    for key, label in labels.items():
        if f.get(key):
            out.append({"label": label(f[key]), "url": f"{base}?{_query(params, **{key: None})}"})
    return out


# ------------------------------------------------------------------------------------------ the report
def _filters_words(params: QueryDict, f: dict[str, Any], visit: dict | None) -> list[str]:
    """The filter of the page in words, for the printable report; a name typed in *Assigned to* is not
    repeated (the report names no one)."""
    from neurodb.partnerships.models import PartnerOrganization

    out = []
    for key, label in (
        ("statuses", _("Status")),
        ("modules", _("Raised from")),
        ("offices", _("Office")),
        ("sections", _("Section")),
    ):
        if f.get(key):
            out.append(f"{label}: {', '.join(f[key])}")
    if f.get("partners"):
        names = (
            PartnerOrganization.objects.filter(pk__in=f["partners"])
            .order_by("name")
            .values_list("name", flat=True)
        )
        out.append(f"{_('Partner')}: {', '.join(names)}")
    if f.get("q"):  # a search may be a person's name: cleaned like the exports
        from neurodb.fmm import privacy

        out.append(f"{_('Search')}: {privacy.clean(f['q'], 200)[0]}")
    if f.get("assignee"):
        out.append(_("Assigned to: filtered by a name"))
    if f.get("changed_from") or f.get("changed_to"):
        out.append(
            _("Changed in eTools: %(start)s – %(end)s")
            % {"start": f.get("changed_from") or "…", "end": f.get("changed_to") or "…"}
        )
    for key, label in (
        ("overdue", _("Overdue only")),
        ("priority", _("High priority only")),
        ("fm", _("Field monitoring only")),
    ):
        if f.get(key):
            out.append(str(label))
    if f.get("due"):
        out.append(f"{_('Due')}: {datamart.AP_DUE.get(f['due'], f['due'])}")
    if f.get("timeliness"):
        out.append(f"{_('Completed')}: {datamart.AP_TIMELINESS.get(f['timeliness'], f['timeliness'])}")
    if f.get("raised"):
        out.append(_("Raised in %(month)s") % {"month": f["raised"]})
    if f.get("completed"):
        out.append(_("Completed in %(month)s") % {"month": f["completed"]})
    for key, label in (
        ("verdict", _("AI verdict")),
        ("pme", _("PME verification")),
        ("link", _("Visit link")),
    ):
        if f.get(key):
            out.append(f"{label}: {f[key].replace('_', ' ')}")
    if visit:
        out.append(_("From %(label)s") % {"label": visit["label"]})
    return out


@require_GET
def action_points_report(request: HttpRequest) -> HttpResponse:
    """The printable report of the action points of the filter: the key figures, the page's charts (drawn
    once, at a width that fits A4), the action points by module and, with Monitoring insights, the AI
    verdicts. The browser's print dialog opens on it; staff save it as PDF. No person is named."""
    from django.db.models import Max

    params, _own = _params(request)
    data = datamart.action_points(params)
    charts = datamart.action_point_charts(data["points"])
    fmm_points = _fmm()
    verdicts = []
    if fmm_points is not None:
        from neurodb.fmm.ai import ap_review
        from neurodb.fmm.models import ActionPointReview

        found = ap_review.counts()
        verdicts = [(label, found.get(value, 0)) for value, label in ActionPointReview.Verdict.choices]
    as_of = ActionPoint.objects.aggregate(at=Max("synced_at"))["at"]
    context = {
        "page_title": _("Action points report"),
        "page_subtitle": _("eTools action points of the filter"),
        "breadcrumbs": [
            _crumb(_("Action points"), reverse("reports:action_points")),
            _crumb(_("Report")),
        ],
        "data": data,
        "total": data["points"].count(),
        "chart_data": charts,
        "filters": _filters_words(params, data["filters"], data["visit"]),
        "as_of": as_of,
        "verdicts": verdicts,
        "fmm_on": fmm_points is not None,
        "back_url": f"{reverse('reports:action_points')}?{_query(params)}",
    }
    return render(request, "reports/action_points_report.html", context)


# ------------------------------------------------------------------------------------------ one point
def _point(pk: int) -> ActionPoint:
    return get_object_or_404(ActionPoint.objects.select_related("partner", "intervention"), pk=pk)


def _detail_context(request: HttpRequest, point: ActionPoint, **extra: Any) -> dict[str, Any]:
    fmm_points = _fmm()
    context: dict[str, Any] = {
        "a": point,
        "action_taken": datamart.ap_action_taken(point.data),
        "today": datetime.date.today(),
        "fmm_on": fmm_points is not None,
        **extra,
    }
    if fmm_points is not None:
        from neurodb.fmm.models import ActionPointVerification

        link = fmm_points.visit_links([point.pk]).get(point.pk)
        is_fm = (point.related_module or "").lower() == "fm"
        confidence = link["confidence"] if link else ("unmatched" if is_fm else "")
        history = fmm_points.verification_history(point.datamart_id)
        context.update(
            {
                "visit_link": link,
                "confidence": confidence,
                "confidence_label": fmm_points.CONFIDENCE_LABELS.get(confidence, ""),
                "confidence_help": fmm_points.CONFIDENCE_HELP.get(confidence, ""),
                "completed": fmm_points.is_completed(point.status),
                "review": fmm_points.current_reviews([point]).get(point.datamart_id),
                "history": history,
                "check": history[0] if history else None,
                "can_verify": fmm_points.can_verify(request.user, point),
                "pme_choices": ActionPointVerification.State.choices,
            }
        )
    return context


@require_GET
def action_point(request: HttpRequest, pk: int) -> HttpResponse:
    """One eTools action point: every field, the action taken, the AI's verdict and its explanation,
    and the PME verifications (with the form, for who may verify)."""
    point = _point(pk)
    context = _detail_context(request, point)
    if request.htmx:
        return render(request, "reports/partials/action_point_detail.html", {**context, "modal": True})
    title = point.reference_number or _("Action point")
    context.update(
        {
            "page_title": title,
            "page_subtitle": _("eTools action point"),
            "breadcrumbs": [_crumb(_("Action points"), reverse("reports:action_points")), _crumb(title)],
        }
    )
    return render(request, "reports/action_point.html", context)


@require_POST
def action_point_verify(request: HttpRequest, pk: int) -> HttpResponse:
    """Record a PME verification (Verified, Rejected or Pending, with an optional note): Administrators
    and Section editors of the point's section."""
    fmm_points = _need_fmm()
    point = _point(pk)
    if not fmm_points.can_verify(request.user, point):
        raise PermissionDenied
    from neurodb.fmm.models import ActionPointVerification

    state = request.POST.get("state", "")
    note = " ".join(request.POST.get("note", "").split())
    error = ""
    if state not in ActionPointVerification.State.values:
        error = _("Choose Verified, Rejected or Pending.")
    elif len(note) > fmm_points.NOTE_CHARS:
        error = _("The note can be at most 500 characters.")
    else:
        fmm_points.verify(point, request.user, state, note)
    if not request.htmx:
        if error:
            messages.error(request, error)
        return redirect("reports:action_point", pk=point.pk)
    context = _detail_context(
        request, point, verify_error=error, verify_saved=not error, note=note if error else ""
    )
    return render(request, "reports/partials/_ap_verification.html", context)


# ------------------------------------------------------------------------------------------ the AI
@require_POST
def action_points_review(request: HttpRequest) -> HttpResponse:
    """Start the AI review of completed action points in the background with the batch size chosen
    (Administrators only); the page's status line follows it."""
    from neurodb.core.models import SyncRun
    from neurodb.fmm.access import is_admin
    from neurodb.fmm.ai import ap_review
    from neurodb.integrations import background

    _need_fmm()
    if not is_admin(request.user):
        raise PermissionDenied
    try:
        size = int(request.POST.get("batch", ap_review.DEFAULT_BATCH))
    except ValueError:
        size = ap_review.DEFAULT_BATCH
    if size not in ap_review.BATCH_SIZES:
        size = ap_review.DEFAULT_BATCH
    on, why = ap_review.switched_on()
    if not on:
        messages.warning(request, _("The AI review cannot run: %(why)s.") % {"why": why})
    elif background.is_running(SyncRun.Job.FMM_AP_REVIEW):
        messages.warning(request, _("The AI review is already running."))
    else:
        background.start_command(
            "fmm_ap_review", "--limit", str(size), "--triggered-by", request.user.get_username()
        )
        messages.success(
            request,
            _("The AI review of up to %(n)s completed action points started. Refresh this page to follow it.")
            % {"n": size},
        )
    return redirect(f"{reverse('reports:action_points')}?{request.POST.get('query', '')}")


@require_POST
def action_points_summary(request: HttpRequest) -> HttpResponse:
    """The AI content summary of the action points of the page's filter (the query posted), as a card."""
    from neurodb.fmm.ai import ap_summary

    _need_fmm()
    params = QueryDict(request.POST.get("query", ""))
    points = datamart.filtered_action_points(params)[0]
    summary = ap_summary.summarise(points, request.user)
    used, allowed = ap_summary.quota(request.user)
    return render(
        request,
        "reports/partials/_ap_summary.html",
        {
            "summary": summary,
            "quota": {"used": used, "allowed": allowed},
            "base": reverse("reports:action_points"),
        },
    )


# ------------------------------------------------------------------------------------------ NeuroDB points
@require_GET
def local_action_points(request: HttpRequest) -> HttpResponse:
    """The NeuroDB action points with their own filters (search, status, priority, programme)."""
    fmm_points = _need_fmm()
    if not request.htmx:
        return redirect(f"{reverse('reports:action_points')}?{request.GET.urlencode()}#neurodb-action-points")
    return render(
        request,
        "reports/partials/_local_action_points.html",
        {
            "local": fmm_points.local_points(request.GET, request.user),
            "can_add_local": fmm_points.can_add_local(request.user),
        },
    )


@require_http_methods(["GET", "POST"])
def local_action_point_new(request: HttpRequest) -> HttpResponse:
    """Add a NeuroDB action point (Administrators and Section editors), optionally on a visit."""
    fmm_points = _need_fmm()
    if not fmm_points.can_add_local(request.user):
        raise PermissionDenied
    from django.contrib.auth import get_user_model

    from neurodb.fmm.models import LocalActionPoint

    users = list(
        get_user_model().objects.filter(is_active=True).order_by("first_name", "last_name", "username")[:500]
    )
    values = {
        "title": "",
        "description": "",
        "visit": request.GET.get("visit", "")[:100],
        "priority": LocalActionPoint.Priority.MEDIUM,
        "due_date": "",
        "assignee_role": "",
        "assignee": "",
    }
    errors: dict[str, str] = {}
    if request.method == "POST":
        values = {key: " ".join(request.POST.get(key, "").split()) for key in values}
        values["description"] = request.POST.get("description", "").strip()
        visit = None
        if not values["title"]:
            errors["title"] = _("Give the action point a title.")
        elif len(values["title"]) > fmm_points.TITLE_CHARS:
            errors["title"] = _("The title can be at most 200 characters.")
        if len(values["description"]) > fmm_points.DESCRIPTION_CHARS:
            errors["description"] = _("The description can be at most 2,000 characters.")
        if values["priority"] not in LocalActionPoint.Priority.values:
            errors["priority"] = _("Choose High, Medium or Low.")
        due = None
        if values["due_date"]:
            try:
                due = datetime.date.fromisoformat(values["due_date"])
            except ValueError:
                errors["due_date"] = _("Write the due date as YYYY-MM-DD.")
        if values["visit"]:
            visit = fmm_points.find_visit(values["visit"])
            if visit is None:
                errors["visit"] = _("No visit of Monitoring insights matches this.")
        assignee = None
        if values["assignee"]:
            assignee = next((u for u in users if str(u.pk) == values["assignee"]), None)
            if assignee is None:
                errors["assignee"] = _("Choose someone from the list.")
        if len(values["assignee_role"]) > 150:
            errors["assignee_role"] = _("The role can be at most 150 characters.")
        if not errors:
            LocalActionPoint.objects.create(
                title=values["title"],
                description=values["description"],
                visit_key=visit.key if visit else "",
                priority=values["priority"],
                due_date=due,
                assignee_role=values["assignee_role"],
                assignee=assignee,
                source=LocalActionPoint.Source.MANUAL,
                created_by=request.user,
                created_by_name=(request.user.get_full_name() or request.user.get_username())[:150],
            )
            messages.success(request, _("NeuroDB action point added."))
            back = request.POST.get("next") or ""
            if not back.startswith("/") or back.startswith("//"):
                back = reverse("reports:action_points") + "#neurodb-action-points"
            if request.htmx:
                response = HttpResponse(status=204)
                response["HX-Redirect"] = back
                return response
            return redirect(back)
    context = {
        "values": values,
        "errors": errors,
        "users": users,
        "priorities": LocalActionPoint.Priority.choices,
        "next": request.GET.get("next") or request.POST.get("next") or "",
    }
    if request.htmx:
        return render(request, "reports/partials/_local_action_point_form.html", {**context, "modal": True})
    context.update(
        {
            "page_title": _("New NeuroDB action point"),
            "breadcrumbs": [
                _crumb(_("Action points"), reverse("reports:action_points")),
                _crumb(_("New NeuroDB action point")),
            ],
        }
    )
    return render(request, "reports/local_action_point_new.html", context)


@require_POST
def local_action_point_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a NeuroDB action point open, done or dropped (Administrators, the person who added it, or
    a Section editor)."""
    fmm_points = _need_fmm()
    from neurodb.fmm.models import LocalActionPoint

    point = get_object_or_404(LocalActionPoint, pk=pk)
    if not fmm_points.can_change_local(request.user, point):
        raise PermissionDenied
    status = request.POST.get("status", "")
    if status in LocalActionPoint.Status.values and status != point.status:
        point.status = status
        point.closed_at = None if status == LocalActionPoint.Status.OPEN else timezone.now()
        point.save(update_fields=["status", "closed_at", "updated_at"])
    back = request.POST.get("next") or ""
    if not back.startswith("/") or back.startswith("//"):
        back = reverse("reports:action_points") + "#neurodb-action-points"
    if request.htmx:
        return render(
            request,
            "reports/partials/_local_action_points.html",
            {
                "local": fmm_points.local_points(QueryDict(request.POST.get("query", "")), request.user),
                "can_add_local": fmm_points.can_add_local(request.user),
            },
        )
    return redirect(back)
