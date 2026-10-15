"""The donor page: one page, no menu, the donor's own results and the country picture."""

from __future__ import annotations

import datetime
import functools
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.web.parts import Part, render_page

from . import services
from .middleware import donor_account
from .models import DonorAccount


@require_GET
def page(request: HttpRequest) -> HttpResponse:
    """A donor sees its own account's page. An administrator previews any account with
    ``?account=<id>``; every other user gets 404 (the page does not exist for them).

    The page answers its frame at once (the bar with the year, the tabs, the headings and the cards, the date
    of the data) and ``donor.js`` then fetches its figures: one JSON part, ``data``
    (:func:`neurodb.donors.services.build`, shared by every worker), at this address plus ``_part=data``, with
    the year written out (and, in an administrator's preview, the account and the tab). The account is always
    the one this view resolves: a donor's own, whatever the address says, so a donor can never fetch another
    account's figures. When the figures are already stored, or the page is asked for whole (``_eager=1``), the
    frame carries them itself (``donor-data``) and nothing is fetched."""
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
    tab = "overall" if request.GET.get("tab") == "overall" else "mine"
    data = functools.cache(lambda: services.build(account, year, today))

    resolved = QueryDict(mutable=True)  # the year written out; the account and the tab of a preview
    resolved["year"] = str(year)
    if preview:
        resolved["account"] = str(account.pk)
    if tab == "overall":
        resolved["tab"] = tab

    def shell() -> dict[str, Any]:
        inline = getattr(request, "parts_eager", False) or services.stored(account, year, today)
        return {
            "account": account,
            "preview": preview,
            "year": year,
            "years": choices,
            "as_of": services.data_as_of(year),
            "tab": tab,
            # the figures, when they are ready (stored) or the page is asked for whole; otherwise donor.js
            # fetches them from the part's address
            "data": data() if inline else None,
            "data_url": f"{request.path}?{resolved.urlencode()}&_part=data",
        }

    return render_page(
        request,
        "donors/page.html",
        scope={},
        shell=shell,
        parts={"data": Part("", data, json=True)},
        query=resolved.urlencode(),
    )
