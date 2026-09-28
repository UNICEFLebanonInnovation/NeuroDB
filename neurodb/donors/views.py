"""The donor page: one page, no menu, the donor's own results and the country picture."""

from __future__ import annotations

import datetime

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.core.models import SyncRun

from . import services
from .middleware import donor_account
from .models import DonorAccount


@require_GET
def page(request: HttpRequest) -> HttpResponse:
    """A donor sees its own account's page. An administrator previews any account with
    ``?account=<id>``; every other user gets 404 (the page does not exist for them)."""
    account = donor_account(request)
    preview = account is None
    if preview:
        if role_of(request.user) != ADMIN or not request.GET.get("account", "").isdigit():
            raise Http404
        account = get_object_or_404(DonorAccount, pk=int(request.GET["account"]))
    today = datetime.date.today()
    choices = services.years(account, today)
    raw = request.GET.get("year", "")
    year = int(raw) if raw.isdigit() and int(raw) in choices else choices[0]
    data = services.build(account, year, today)
    synced = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)
    return render(
        request,
        "donors/page.html",
        {
            "account": account,
            "preview": preview,
            "year": year,
            "years": choices,
            "data": data,
            "as_of": synced.finished_at if synced else None,
            "tab": "overall" if request.GET.get("tab") == "overall" else "mine",
        },
    )
