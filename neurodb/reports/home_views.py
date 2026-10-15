"""Home: the site root once signed in (``reports.views.home`` dispatches to :func:`home_page`).

What needs the person today, what changed since the last working day, and where their section (or the
country) stands this year, in that order (SPEC_HOME §3.1). The page loads in parts
(:mod:`neurodb.web.parts`): this view answers the frame at once (title, date and scope line, purpose, the
freshness strip; no figure work and no ``watch_`` table) with a placeholder for each block:

- ``for-you``: the NeuroDB Watch card, from its own address (``watch:card``); not drawn while Watch is off;
- ``tasks``: "Needs a decision" (Administrators and Management) and the daily review's digest (everyone);
- ``since``: what changed since the previous working day, then "Your tasks" (counts only);
- ``standing``: the Country overview's own five key figures for this year (the same build, key and template,
  :func:`neurodb.reports.overview.build`), drawn inline when that build is already stored, and the "Also"
  line;
- ``programmes``: the country programme, the Compiler figures and ActivityInfo, one line each.

The first load shows the person's own section by the rule the Country overview and the daily review share
(:func:`neurodb.reports.home.default_sections`); every link Home draws to those pages and the brief carries
Home's scope written out, so they show the same figures.
"""

from __future__ import annotations

import functools
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, QueryDict
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, in_management, role_of
from neurodb.core import years
from neurodb.datamart import monitoring as pd_monitoring_service
from neurodb.indicators.services import activityinfo
from neurodb.reports import home
from neurodb.reports import overview as overview_service
from neurodb.web.parts import Part, first_load, render_page


def _query(sections: list[str], year: int | None = None, **extra: str) -> str:
    query = QueryDict(mutable=True)
    if sections:
        query.setlist("section", sections)
    elif year is not None:
        query["year"] = str(year)
    for key, value in extra.items():
        query[key] = value
    return query.urlencode()


def home_page(request: HttpRequest) -> HttpResponse:
    """Home: see the module docstring. ``section`` (repeated) narrows it; a first load shows the person's own
    section (every section for Management and for people without one)."""
    user = request.user
    today = years.today()
    year = today.year
    options = functools.cache(lambda: {"sections": overview_service.section_options(year)})
    sections = [s for s in request.GET.getlist("section") if s]
    own_default: list[str] = []
    if first_load(request):
        own_default = home.default_sections(user, options()["sections"])
        sections = own_default
    role = role_of(user)
    # Home's scope, written out on every link to the pages that would otherwise take a first-load default
    scope_query = _query(sections, year)
    keep = QueryDict(mutable=True)  # the eTools pages: the sections, this year, every PD running in it
    keep.setlist("section", sections)
    keep["year"] = str(year)
    keep["scope"] = "year"
    programmes = QueryDict(mutable=True)
    programmes.setlist("section", sections)
    previous = QueryDict(mutable=True)
    previous.setlist("section", sections)
    previous["year"] = str(year - 1)
    # the "Also" line's pages: the action points with Home's sections (every section: none named, which the
    # page reads as the whole country), field monitoring (FMM) with them written out, even empty
    points = QueryDict(mutable=True)
    points.setlist("section", sections)
    points["overdue"], points["priority"] = "1", "1"
    field = QueryDict(mutable=True)
    field.setlist("section", sections or [""])
    field["year"] = str(year)
    # "your section" whenever the sections shown are the person's own (a part request is never a first load)
    own = functools.cache(lambda: home.own_names(user, options))
    if not sections:
        who = _("the country")
    elif own_default or sorted(sections) == sorted(own()):
        who = _("your section")
    else:
        who = ", ".join(sections)
    scope = {
        "selected": {"section": sections, "governorate": "", "year": str(year)},
        "year": year,
        "labels": pd_monitoring_service.LABELS,
        "keep_query": keep.urlencode(),
        "programmes_query": ("?" + programmes.urlencode()) if sections else "",
        "previous_query": previous.urlencode(),
        "points_query": points.urlencode(),
        "fmm_query": field.urlencode(),
        "scope_query": scope_query,
        "month_names": _month_names(),
        "who": who,
        "is_editor": role in (ADMIN, SECTION_EDITOR),
    }

    def shell() -> dict[str, Any]:
        jobs = overview_service.freshness(overview_service.freshness_jobs())
        return {
            "page_title": _("Home"),
            "breadcrumbs": [{"label": _("Home"), "url": None}],
            "date_line": date_format(today, "l j F Y"),
            "own_default": own_default,
            "scope_sections": sections,
            "own_names": own() if not sections else [],
            "own_query": _query(own()) if not sections else "",
            "today": today,
            "watch_enabled": settings.WATCH_ENABLED,
            "watch_off": _(
                "NeuroDB is not checking at the moment: an administrator switched these checks off."
            ),
            "freshness": jobs,
            "freshness_note": overview_service.freshness_note(jobs),
        }

    as_of = request.GET.get("as_of", "") if getattr(request, "part", None) else overview_service.fingerprint()
    resolved = QueryDict(mutable=True)
    resolved.setlist("section", sections or [""])
    if as_of:
        resolved["as_of"] = as_of
    # the Country overview's own scope for this year (its key holds the reporting year of this year's
    # ActivityInfo records): read only by the frame's look at the stored build and by ``standing``
    build_scope = functools.cache(
        lambda: overview_service.Scope(
            year=year,
            reporting_year=activityinfo.reporting_year_for(year),
            sections=sections,
            governorate="",
            today=today,
        )
    )
    data = functools.cache(lambda: overview_service.build(build_scope(), as_of=as_of))
    stored = functools.cache(lambda: overview_service.stored(build_scope()))

    def tasks() -> dict[str, Any]:
        deciding = role == ADMIN or in_management(user)
        decisions = home.decisions(sections, year) if deciding else None
        shown_keys = [row["key"] for row in decisions["rows"]] if decisions else []
        from neurodb.review import services as review_services

        digest = review_services.digest(sections, limit=home.REVIEW_ROWS, exclude_keys=shown_keys)
        return {
            "deciding": deciding,
            "decisions": decisions,
            "digest": digest,
            "stale": bool(digest and (today - digest["date"]).days > home.REVIEW_STALE_DAYS),
            "schedule": home.review_schedule() if digest is None else None,
            "staff": bool(user.is_staff or user.is_superuser),
            "brief_query": scope_query,
            "review_query": scope_query,
        }

    def since() -> dict[str, Any]:
        start = years.since_working_day()
        # the person's own section as it is, when Home shows it (the first load wrote its names in the
        # address)
        mine = bool(sections) and (bool(own_default) or sorted(sections) == sorted(own()))
        own_id = getattr(user, "section_id", None) if mine else None
        ids, matched = home.section_ids(sections, own=own_id)
        return {
            "since_label": home.since_label(start, today),
            "since_yesterday": home.since_yesterday(start, today),
            "since_weekday": date_format(start, "l"),
            "changes": home.since(ids, start),
            "matched": matched,
            "your_tasks": home.your_tasks(user, year),
        }

    def standing() -> dict[str, Any]:
        built = data()
        return {"data": built, "overview_query": scope_query, "no_etools": _no_etools(built)}

    parts = {
        "tasks": Part("reports/parts/home_tasks.html", tasks),
        "since": Part("reports/parts/home_since.html", since),
        "standing": Part("reports/parts/home_standing.html", standing, ready=stored),
        "programmes": Part("reports/parts/home_programmes.html", lambda: home.programmes(user, today)),
    }
    return render_page(
        request, "reports/home.html", scope=scope, shell=shell, parts=parts, query=resolved.urlencode()
    )


def _no_etools(data: dict[str, Any]) -> bool:
    """Whether the server has no eTools data at all (no programme document of any year, and the eTools
    Datamart sync never succeeded): Home then says so in one line instead of five tiles of 0. A scope with
    true zeros (another section, a year whose documents have not started, a sync that ran) keeps its tiles.
    Looked up only when this year's figures are all 0."""
    if data["impact"].get("children_reached") or data["delivery"].get("programme_documents"):
        return False
    from neurodb.core.models import SyncRun
    from neurodb.partnerships.models import PCA

    return not PCA.objects.exists() and SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART) is None


def _month_names() -> dict[int, str]:
    from neurodb.reports.views import _month_names as names

    return names()


__all__ = ["home_page"]
