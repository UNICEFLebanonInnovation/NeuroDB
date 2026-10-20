"""The eTools master indicators on Partner monitoring's Master indicators tab
(:mod:`neurodb.datamart.masters_tab`): a master's detail (the modal), and what a section changes there
without the administration, each a button: accept or dismiss a proposed master, confirm or reject a
suggestion, confirm all of a master's suggestions or a section's strong ones, change what a sub counts as,
take one out, attach PD indicators that are in no master, rename a master and set its year's code and target.

Each change is a POST modelled on :func:`neurodb.reports.views.pd_main_indicator`: an Administrator, or a
Section editor of the master's section (:func:`neurodb.datamart.masters_tab.curator`); anyone else gets 403 (a
donor account never reaches these pages). Each says what it did and what it left as it was, and goes back to
the tab, where the figures show the change at once (nothing is stored). Who did it is kept for the
administration only (``updated_by``, ``confirmed_by``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of
from neurodb.datamart import curation, master_links, masters_tab
from neurodb.datamart import monitoring as pd_monitoring_service
from neurodb.datamart.models import EtoolsMaster, EtoolsMasterLink

MASTER = EtoolsMaster.Status
Role, Status = EtoolsMasterLink.Role, EtoolsMasterLink.Status


def _crumb(label: str, url: str | None = None) -> dict[str, str | None]:
    return {"label": label, "url": url}


def _tab_url(section: str = "", year: int | None = None) -> str:
    query = {"view": "masters", "section": section}
    if year:
        query["year"] = year
    return reverse("reports:pd_monitoring") + "?" + urlencode(query)


def _back(request: HttpRequest, master: EtoolsMaster | None = None) -> str:
    """Where a change goes back to: the tab as it was (``next``, on this site only), else the tab on the
    master's section."""
    after = request.POST.get("next") or ""
    if after and url_has_allowed_host_and_scheme(
        after, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return after
    return _tab_url(master.section if master is not None else "")


def _number(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 18 else None


def _numbers(values: list[str]) -> list[int]:
    return [n for n in map(_number, values) if n is not None]


def _may_curate_anything(user) -> bool:
    return role_of(user) in (ADMIN, SECTION_EDITOR)


def _left(items: list[tuple[str, str]]) -> str:
    """What was left as it was, for a message: "PD/2026/001: why, ..." (the first three, then how many
    more)."""
    return curation.listed([f"{name}: {why}" for name, why in items])


def _link_name(link: EtoolsMasterLink) -> str:
    return f"{link.pd_number or 'PD'}: {link.title or link.ident}"


def _said_confirmed(request: HttpRequest, done: master_links.Confirmed) -> None:
    if done.confirmed:
        messages.success(
            request,
            _("%(n)s PD indicator(s) confirmed: they count from now on.") % {"n": done.confirmed},
        )
    if done.left:
        messages.warning(
            request,
            _("%(n)s left as they were: %(why)s")
            % {"n": len(done.left), "why": _left([(_link_name(link), why) for link, why in done.left])},
        )


# ------------------------------------------------------------------------------------------------- the detail
@require_GET
def etools_master(request: HttpRequest, pk: int) -> HttpResponse:
    """One master indicator in the year (``?year=``, this year by default) as the tab reads it, with its
    report type and places: its tiles, how it is worked out, its quarters, its sub table and what to know
    before reading it. A modal from the tab, a page of its own otherwise."""
    filters = pd_monitoring_service.Filters.from_params(request.GET)
    if filters.year is None:
        filters = dataclasses.replace(filters, year=pd_monitoring_service.today().year)
    line = masters_tab.detail(pk, filters, pd_monitoring_service.today())
    if line is None:
        raise Http404
    master, figure = line.master, line.figure
    tab_url = _tab_url(master.section, filters.year)
    context = {
        "page_title": master.name,
        "page_subtitle": " · ".join(x for x in (figure.code, master.section, figure.method_label) if x),
        "breadcrumbs": [
            _crumb(_("Partner monitoring"), reverse("reports:pd_monitoring")),
            _crumb(_("Master indicators"), tab_url),
            _crumb(master.name),
        ],
        "line": line,
        "figure": figure,
        "master": master,
        "year": filters.year,
        "report_type": filters.report_type,
        "report_types": pd_monitoring_service.REPORT_TYPES,
        "labels": pd_monitoring_service.LABELS,
        "quarter_labels": masters_tab.QUARTER_LABELS,
        "tab_url": tab_url,
    }
    template = "reports/partials/etools_master.html" if request.htmx else "reports/etools_master.html"
    return render(request, template, context)


# ------------------------------------------------------------------------------------------------ the changes
@require_POST
def etools_master_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """One change to master indicator ``pk`` (``action``): accept it (a proposal, with the PD indicators its
    row showed, one ``link`` each) or dismiss it, confirm the suggestions its row showed (confirm_all, one
    ``link`` each), confirm or reject one (``link``), change what one counts as (``role``), take one out
    (remove), or rename it and set the year's code and target (settings: ``name``, ``year``, ``code``,
    ``target``). A proposal's links are confirmed by accepting it."""
    master = get_object_or_404(EtoolsMaster, pk=pk)
    if not masters_tab.can_curate(request.user, master.section):
        raise PermissionDenied
    action = request.POST.get("action") or ""
    by = request.user.get_username()
    back = _back(request, master)
    if master.status not in (MASTER.ACTIVE, MASTER.PROPOSED):
        messages.error(
            request,
            _("This master indicator is %(status)s: it can only be changed in the administration.")
            % {"status": master.get_status_display().lower()},
        )
        return redirect(back)
    if action == "accept":
        # the PD indicators the row showed (``link``), never one the night added since
        shown = set(_numbers(request.POST.getlist("link")))
        waiting = set(master.links.filter(status=Status.SUGGESTED).values_list("pk", flat=True))
        if master.status == MASTER.PROPOSED and waiting and not waiting & shown:
            messages.warning(
                request,
                _(
                    "None of the PD indicators shown is in the proposal any more: open the tab again to see "
                    "what it holds now."
                ),
            )
            return redirect(back)
        done = master_links.accept([master], by, only=shown)
        if done.accepted:
            messages.success(
                request,
                _("“%(name)s” is active: %(n)s PD indicator(s) confirmed, they count from now on.")
                % {"name": master.name, "n": done.links.confirmed},
            )
        for _master, why in done.refused:
            messages.warning(request, why)
        if done.unseen:
            messages.warning(
                request,
                _(
                    "%(n)s PD indicator(s) added to the proposal after the page was shown are left as "
                    "suggestions: confirm them under the master indicator."
                )
                % {"n": done.unseen},
            )
        if done.links.left:
            messages.warning(
                request,
                _("%(n)s left as suggestions: %(why)s")
                % {
                    "n": len(done.links.left),
                    "why": _left([(_link_name(x), why) for x, why in done.links.left]),
                },
            )
    elif action == "dismiss":
        if master_links.dismiss([master], by):
            messages.success(
                request, _("“%(name)s” dismissed: NeuroDB will not propose it again.") % {"name": master.name}
            )
        else:
            messages.warning(request, master_links.NOT_PROPOSED)
    elif action == "confirm_all":
        # the suggestions the row showed (``link``), never one it did not: another partner's, a PD's of
        # another year
        links = list(
            master.links.filter(
                status=Status.SUGGESTED, pk__in=_numbers(request.POST.getlist("link"))
            ).order_by("sequence", "id")
        )
        if not links:
            messages.warning(request, _("None of the suggestions shown is waiting any more."))
            return redirect(back)
        for link in links:
            link.master = master
        _said_confirmed(request, master_links.confirm(links, by))
    elif action in ("confirm", "reject", "role", "remove"):
        link = master.links.filter(pk=_number(request.POST.get("link"))).first()
        if link is None:
            messages.error(request, _("Choose one of this master indicator's PD indicators."))
            return redirect(back)
        link.master = master
        _change_link(request, link, action, by)
    elif action == "settings":
        _settings(request, master, by)
    else:
        messages.error(request, _("Choose what to change."))
    return redirect(back)


def _change_link(request: HttpRequest, link: EtoolsMasterLink, action: str, by: str) -> None:
    name = _link_name(link)
    if action == "confirm":
        _said_confirmed(request, master_links.confirm([link], by))
    elif action == "reject":
        master_links.reject([link], by)
        messages.success(
            request,
            _("%(name)s rejected: it never counts here and is not suggested here again.") % {"name": name},
        )
    elif action == "remove":
        curation.remove(link)
        messages.success(request, _("%(name)s taken out of this master indicator.") % {"name": name})
    else:
        role = request.POST.get("role") or ""
        problem = curation.set_role(link, role, by)
        if problem:
            messages.error(
                request, _("%(name)s was left as it was: %(why)s") % {"name": name, "why": problem}
            )
        else:
            messages.success(
                request,
                _("%(name)s now counts as: %(role)s.") % {"name": name, "role": Role(link.role).label},
            )


def _settings(request: HttpRequest, master: EtoolsMaster, by: str) -> None:
    year = _number(request.POST.get("year"))
    if year is None or not 2000 <= year <= 2100:
        messages.error(request, _("Choose the year these settings are for."))
        return
    errors = curation.save_settings(
        master,
        year,
        name=request.POST.get("name", master.name),
        code=request.POST.get("code", ""),
        target=request.POST.get("target", ""),
        by=by,
    )
    if errors:
        messages.error(request, _("Nothing was saved: %(why)s") % {"why": " ".join(errors.values())})
    else:
        messages.success(
            request,
            _("“%(name)s”: the settings of %(year)s are saved.") % {"name": master.name, "year": year},
        )


@require_POST
def etools_master_attach(request: HttpRequest) -> HttpResponse:
    """Attach the PD indicators ticked in "Not in a master" (``key``: PD id|ident) to one active master
    (``master``), all with one role, confirmed at once (:func:`neurodb.datamart.curation.add`): one of another
    section, of a type the role or the method does not take, or counted in another master's total is left out
    and said why."""
    if not _may_curate_anything(request.user):
        raise PermissionDenied
    master = EtoolsMaster.objects.filter(pk=_number(request.POST.get("master")), status=MASTER.ACTIVE).first()
    if master is None:
        messages.error(request, _("Choose the master indicator to attach them to."))
        return redirect(_back(request))
    if not masters_tab.can_curate(request.user, master.section):
        raise PermissionDenied
    back = _back(request, master)
    role = request.POST.get("role") or Role.TOTAL
    if role not in Role.values:
        messages.error(request, _("Choose what they count as."))
        return redirect(back)
    keys = [key for key in map(curation.parse_key, request.POST.getlist("key")) if key]
    if not keys:
        messages.error(request, _("Tick the PD indicators to attach."))
        return redirect(back)
    done = curation.add(master, keys, role, request.user.get_username())
    if done.added:
        messages.success(
            request,
            _("%(n)s PD indicator(s) attached to “%(name)s”: they count from now on.")
            % {"n": done.added, "name": master.name},
        )
    if done.left:
        messages.warning(
            request, _("%(n)s left out: %(why)s") % {"n": len(done.left), "why": _left(done.left)}
        )
    return redirect(back)


@require_POST
def etools_masters_confirm(request: HttpRequest) -> HttpResponse:
    """Confirm a section's strong matches in one click: the suggestions scoring 0.9 or more (the same
    structure as a confirmed sub, or a replacement) of its active masters that the tab showed (``link``), as
    the administration's "Confirm suggestions scoring 0.9 or more" does. One the tab did not show (another
    partner's, a master hidden by the search) is never confirmed by it."""
    section = (request.POST.get("section") or "").strip()
    if not section or not masters_tab.can_curate(request.user, section):
        raise PermissionDenied
    links = list(
        EtoolsMasterLink.objects.filter(
            pk__in=_numbers(request.POST.getlist("link")),
            master__section__iexact=section,
            master__status=MASTER.ACTIVE,
            status=Status.SUGGESTED,
            score__gte=master_links.STRONG,
        )
        .select_related("master")
        .order_by("master__name", "sequence", "id")
    )
    if not links:
        messages.warning(request, _("None of the strong matches shown is waiting any more."))
        return redirect(_back(request))
    _said_confirmed(request, master_links.confirm(links, request.user.get_username()))
    return redirect(_back(request))
