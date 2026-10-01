"""Makani wellbeing pages (/makani/wellbeing/): the centre summaries (counts, every signed-in user)
and the flags on children who may need a follow-up (by BMA registration number; UNICEF
administrators and section editors, who record the follow-ups, saved in BMA)."""

from __future__ import annotations

import datetime
from collections import Counter

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of
from neurodb.integrations.http import IntegrationError
from neurodb.youth import compiler

from .forms import FollowUpForm
from .models import CenterSummary, Flag, SyncState
from .sync import store_flag


def can_follow_up(user) -> bool:
    return bool(user.is_authenticated and (user.is_superuser or role_of(user) in (ADMIN, SECTION_EDITOR)))


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


@require_GET
def summaries(request: HttpRequest) -> HttpResponse:
    state = SyncState.current()
    months = list(CenterSummary.objects.values_list("month", flat=True).distinct().order_by("-month"))
    try:
        month = datetime.date.fromisoformat(request.GET.get("month", ""))
    except ValueError:
        month = months[0] if months else None
    rows = list(CenterSummary.objects.filter(month=month)) if month else []
    partner = _int(request.GET.get("partner"))
    partners = sorted({(r.partner_id, r.partner_name) for r in rows if r.partner_id}, key=lambda p: p[1])
    if partner:
        rows = [r for r in rows if r.partner_id == partner]
    total = Counter()
    by_kind = Counter()
    for r in rows:
        f, flags = r.figures, r.figures.get("flags", {})
        total["children"] += f.get("children", 0)
        total["dropouts"] += f.get("dropouts", 0)
        total["flagged"] += flags.get("children_flagged", 0)
        total["opened"] += flags.get("opened_this_month", 0)
        total["on_time"] += flags.get("followed_up_on_time", 0)
        total["urgent"] += flags.get("urgent_open", 0)
        total["late"] += flags.get("open_longer_than_target", 0)
        total["sheets"] += f.get("attendance", {}).get("sheets", 0)
        total["sheets_all_present"] += f.get("attendance", {}).get("sheets_all_present", 0)
        by_kind.update(flags.get("open_by_kind", {}))
    kinds = state.kinds or {}
    context = {
        "page_title": _("Makani wellbeing"),
        "page_subtitle": _(
            "Children who may need a follow-up, worked out every night in BMA from attendance, services, "
            "screenings, referrals and tests, and how quickly centres follow up"
        ),
        "breadcrumbs": [{"label": _("Makani wellbeing"), "url": None}],
        "rows": rows,
        "months": months,
        "month": month,
        "partner": partner,
        "partners": partners,
        "total": total,
        "state": state,
        "configured": compiler.configured(),
        "can_follow_up": can_follow_up(request.user),
        "chart_data": {"open_by_kind": [[kinds.get(k, k), n] for k, n in by_kind.most_common()]},
    }
    return render(request, "wellbeing/summaries.html", context)


@require_GET
def flags(request: HttpRequest) -> HttpResponse:
    if not can_follow_up(request.user):
        raise PermissionDenied
    status = request.GET.get("status", Flag.OPEN)
    kind = request.GET.get("kind", "")
    partner, center = _int(request.GET.get("partner")), _int(request.GET.get("center"))
    qs = Flag.objects.all()
    if status in dict(Flag.STATUSES):
        qs = qs.filter(status=status)
    if kind:
        qs = qs.filter(kind=kind)
    if partner:
        qs = qs.filter(partner_id=partner)
    if center:
        qs = qs.filter(center_id=center)
    if request.GET.get("urgent") == "1":
        qs = qs.filter(urgent=True)
    open_flags = Flag.objects.filter(status=Flag.OPEN)
    state = SyncState.current()
    target = (state.settings or {}).get("followup_days", 7)
    late_before = timezone.localdate() - datetime.timedelta(days=target)
    context = {
        "page_title": _("Children to follow up"),
        "page_subtitle": _(
            "Makani flags by BMA registration number; open the child in BMA for the details. A flag means "
            "“check on this child”, not a judgement."
        ),
        "breadcrumbs": [
            {"label": _("Makani wellbeing"), "url": reverse("wellbeing:summaries")},
            {"label": _("Children to follow up"), "url": None},
        ],
        "page": Paginator(qs, 50).get_page(request.GET.get("page")),
        "status": status,
        "kind": kind,
        "partner": partner,
        "center": center,
        "statuses": Flag.STATUSES,
        "kinds": sorted((state.kinds or {}).items()),
        "partners": open_flags.values_list("partner_id", "partner_name").distinct().order_by("partner_name"),
        "centers": open_flags.filter(**({"partner_id": partner} if partner else {}))
        .values_list("center_id", "center_name")
        .distinct()
        .order_by("center_name"),
        "counts": {
            "open": open_flags.count(),
            "urgent": open_flags.filter(urgent=True).count(),
            "priority": open_flags.filter(priority=True).count(),
            "late": open_flags.filter(opened_on__lt=late_before).count(),
            "children": open_flags.values("registration").distinct().count(),
        },
        "by_kind": dict(open_flags.values_list("kind").annotate(n=Count("id")).order_by()),
        "target": target,
        "state": state,
    }
    return render(request, "wellbeing/flags.html", context)


@require_http_methods(["GET", "POST"])
def follow_up(request: HttpRequest, pk: int) -> HttpResponse:
    if not can_follow_up(request.user):
        raise PermissionDenied
    flag = get_object_or_404(Flag, pk=pk)
    form = FollowUpForm(request.POST or None, flag=flag)
    if request.method == "POST" and flag.status == Flag.OPEN and form.is_valid():
        values = {
            **form.cleaned_data,
            "followed_up_on": form.cleaned_data["followed_up_on"].isoformat(),
            "by": request.user.get_full_name() or request.user.get_username(),
        }
        try:
            status, body = compiler.CompilerClient().wellbeing_follow_up(flag.bma_id, values)
        except IntegrationError:
            messages.error(
                request, _("BMA could not be reached: the follow-up was not saved. Try again later.")
            )
        else:
            if status == 400:
                for field, errors in body.items():
                    form.add_error(field if field in form.fields else None, "; ".join(map(str, errors)))
            else:
                updated = store_flag(body["flag"])
                if status == 200:
                    updated.followed_up_by = request.user
                    updated.save(update_fields=["followed_up_by", "synced_at"])
                    messages.success(request, _("Follow-up saved in BMA."))
                else:
                    messages.info(
                        request, _("This flag was already closed in BMA; it is now up to date here.")
                    )
                return redirect("wellbeing:flags")
    context = {
        "page_title": _("Record the follow-up"),
        "breadcrumbs": [
            {"label": _("Makani wellbeing"), "url": reverse("wellbeing:summaries")},
            {"label": _("Children to follow up"), "url": reverse("wellbeing:flags")},
            {"label": str(flag.registration), "url": None},
        ],
        "flag": flag,
        "form": form,
    }
    return render(request, "wellbeing/follow_up.html", context)
