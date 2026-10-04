"""What's new: everything NeuroDB noticed changing, whatever the source, for every signed-in user."""

from __future__ import annotations

import datetime
from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from neurodb.accounts.models import Section

from . import digest, news
from .models import Change, Digest, DigestSubscription, Entity

PERIODS = {"1": 1, "7": 7, "30": 30}
SHOWN = 300


@require_GET
def whats_new(request: HttpRequest) -> HttpResponse:
    days = PERIODS.get(request.GET.get("days", ""), 7)
    if "section" in request.GET:  # a choice was made ("" = every section)
        raw = request.GET.get("section", "")
        section_id = int(raw) if raw.isdigit() else None
    else:  # first visit: the user's own section
        section_id = getattr(request.user, "section_id", None)
    kind = request.GET.get("kind", "")
    kind = kind if kind in dict(Entity.Kind.choices) else ""
    minor = request.GET.get("all") == "1"
    since = timezone.now() - datetime.timedelta(days=days)
    qs = news.recent(
        since,
        sections=[section_id] if section_id else None,
        kinds=[kind] if kind else None,
        notable_only=not minor,
    )
    total = qs.count()
    rows = [(c, news.sentence(c)) for c in qs[:SHOWN]]
    days_list = [
        (day, list(items))
        for day, items in groupby(rows, key=lambda r: timezone.localtime(r[0].detected_at).date())
    ]
    note = Digest.objects.filter(section_id=section_id).first() if section_id else None
    note = note or Digest.objects.filter(section_id__isnull=True).first()
    sections = Section.objects.order_by("name")
    section_name = next((s.name for s in sections if s.pk == section_id), "")
    subscription = DigestSubscription.objects.filter(user=request.user).first()
    last = news.last_build()
    context = {
        "page_title": _("What's new"),
        "page_subtitle": _(
            "Everything NeuroDB noticed changing, whatever the source: new partners, programme documents, "
            "donors, documents and indicators, statuses and figures that moved, new links"
        ),
        "breadcrumbs": [{"label": _("What's new"), "url": None}],
        "days": days,
        "periods": PERIODS,
        "section_id": section_id,
        "section_name": section_name,
        "sections": sections,
        "kind": kind,
        "kinds": Entity.Kind.choices,
        "minor": minor,
        "days_list": days_list,
        "total": total,
        "shown": min(total, SHOWN),
        "note": note,
        "email_enabled": settings.DIGEST_EMAIL_ENABLED,
        "morning_email": digest.carried_by_morning_email(),  # the email is NeuroDB Watch's morning note
        "subscribed": bool(subscription and subscription.email),
        "last_build": last,
        "ops": Change.Op,
    }
    return render(request, "graph/whats_new.html", context)


@require_POST
def email(request: HttpRequest) -> HttpResponse:
    wanted = request.POST.get("email") == "1"
    DigestSubscription.objects.update_or_create(user=request.user, defaults={"email": wanted})
    if wanted and not request.user.email:
        messages.warning(request, _("Your account has no email address: ask an administrator to add one."))
    elif wanted:
        messages.success(
            request,
            _("You will get the morning note by email (For you and What's new).")
            if digest.carried_by_morning_email()
            else _("You will get the daily note by email."),
        )
    else:
        messages.success(request, _("You will no longer get the email."))
    return redirect("graph:whats_new")
