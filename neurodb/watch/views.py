"""The "For you" page of NeuroDB Watch, its count in the sidebar, its Overview card and its buttons.

What is shown and what a button does is decided in :mod:`neurodb.watch.page`; these views only check
who asks. Signing in is required (the site's login middleware). Donor accounts never reach them: the
donor middleware sends a donor's page request to the donor page and refuses the HTMX count and card
(403), and the views refuse a donor account again. A button acts on the person's own receipt only:
any other returns 404. Administrators also get "Show notes for" and "Check now".
"""

from __future__ import annotations

import logging
from functools import wraps

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.donors.middleware import donor_account

from . import page
from .models import WatchReceipt

logger = logging.getLogger(__name__)


def staff_only(view):
    """Refuse a donor account (the donor middleware already does; this keeps it so)."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if donor_account(request) is not None:
            return HttpResponseForbidden()
        return view(request, *args, **kwargs)

    return wrapped


@require_GET
@staff_only
def for_you(request: HttpRequest) -> HttpResponse:
    """The page: what NeuroDB noticed for the person (or, for an Administrator, what an audience is
    shown: ``?audience=country`` or ``?audience=section:<id>``)."""
    data = page.build(request.user, audience=request.GET.get("audience", "").strip())
    subtitle = _("What NeuroDB noticed")
    if data["subtitle_for"]:
        subtitle += " " + _("for %(who)s") % {"who": data["subtitle_for"]}
    if data["checked"]:
        subtitle += " · " + _("checked %(when)s") % {"when": data["checked"]}
    context = {
        **data,
        "page_title": _("For you"),
        "page_subtitle": subtitle,
        "breadcrumbs": [{"label": _("For you"), "url": None}],
    }
    return render(request, "watch/for_you.html", context)


@require_GET
@staff_only
def badge(request: HttpRequest) -> HttpResponse:
    """The count beside "For you" in the sidebar (loaded after the page, cached a minute)."""
    return render(request, "watch/partials/_badge.html", {"count": page.badge_count(request.user)})


@require_GET
@staff_only
def card(request: HttpRequest) -> HttpResponse:
    """The Overview card (loaded after the page)."""
    return render(request, "watch/partials/_card.html", page.card(request.user))


@require_POST
@staff_only
def react(request: HttpRequest, pk: int) -> HttpResponse:
    """A button on a card: on the person's own receipt only (anything else: 404)."""
    receipt = get_object_or_404(WatchReceipt.objects.select_related("item", "user"), pk=pk, user=request.user)
    try:
        said = page.react(
            receipt,
            reaction=request.POST.get("reaction", "").strip(),
            snooze=request.POST.get("snooze", "").strip(),
            comment=request.POST.get("comment", ""),
        )
    except page.Refused as exc:
        if request.htmx:  # the card again, saying why in plain words (htmx does not show a 4xx)
            receipt.refresh_from_db()
            return _card(request, receipt, str(exc), refused=True)
        messages.error(request, str(exc))
        return redirect("watch:for_you")
    if not request.htmx:
        messages.success(request, said)
        return redirect(reverse("watch:for_you") + f"#watch-{receipt.item_id}")
    return _card(request, receipt, said)


def _card(request: HttpRequest, receipt: WatchReceipt, said: str, refused: bool = False) -> HttpResponse:
    """The card after a button, in place of the one pressed (each point is on the page once, so it
    keeps its anchor), with what was done (or why not) and the same words for the page's live region."""
    today = timezone.localdate()
    shown = page.point(receipt.item, receipt, today, modes=page.check_modes())
    shown["anchor"] = f"watch-{receipt.item_id}"
    shown["said"] = said
    shown["refused"] = refused
    shown["why"] = page.set_aside_reason(receipt, today)
    shown["undo"] = bool(shown["why"])
    return render(request, "watch/partials/_item.html", {"card": shown, "open": True, "live": True})


@require_POST
@staff_only
def check_now(request: HttpRequest) -> HttpResponse:
    """Administrators: run the morning check now (the job "watch")."""
    if role_of(request.user) != ADMIN:
        raise Http404
    from neurodb.core import jobs

    try:
        started = jobs.start("watch", triggered_by=request.user.get_username())
    except Exception:
        logger.exception("NeuroDB Watch: Check now could not start the job")
        messages.error(
            request, _("The check could not be started. Look at Scheduled jobs in the administration.")
        )
    else:
        if started == "running":
            messages.info(request, _("NeuroDB is already checking. Refresh this page in a few minutes."))
        else:
            messages.success(request, _("NeuroDB is checking now. Refresh this page in a few minutes."))
    return redirect("watch:for_you")
