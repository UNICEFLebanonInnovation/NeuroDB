"""HTML pages of NeuroDB. Small function views: fetch the object, call a service, render.

Every page is login-required through ``LoginRequiredMiddleware``; HTMX requests receive a partial
template (``request.htmx``), plain requests the full page.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import io
import mimetypes
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.accounts.roles import SECTION_EDITOR, can_edit_section, role_of
from neurodb.assistant import llm
from neurodb.core import years
from neurodb.core.models import PopulationFigure, SavedView, SyncRun
from neurodb.core.services import population as population_service
from neurodb.datamart import hact_plan, headline, identity, owed, timeline
from neurodb.datamart import keys as etools_keys
from neurodb.datamart import monitoring as pd_monitoring_service
from neurodb.datamart import reporting as reporting_service
from neurodb.datamart import services as datamart
from neurodb.datamart.models import AuditEngagement, AuditFinding, PDIndicator
from neurodb.facts.services import dashboard as facts
from neurodb.facts.services import partners as partner_facts
from neurodb.indicators.models import Database, MasterIndicator, NeuroReport
from neurodb.indicators.services import navigation
from neurodb.indicators.services.tracking import LABELS
from neurodb.knowledge.search import linked as linked_documents
from neurodb.library.models import Resource
from neurodb.library.services import completed_maps, published_resource, resource_filters, search_resources
from neurodb.partnerships import funded
from neurodb.partnerships import services as partnerships
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import brief as brief_service
from neurodb.reports import home as home_service
from neurodb.reports import overview as overview_service
from neurodb.web.parts import EAGER_KEY, Part, first_load, render_page

from . import check_views, exports, old_filters, services
from .forms import HPMCommentForm, SavedViewForm

PAGE_SIZE = 50


# ------------------------------------------------------------------------- helpers


def _database(pk: int) -> Database:
    return get_object_or_404(Database.objects.select_related("section", "reporting_year"), pk=pk)


def _report(pk: int) -> NeuroReport:
    return get_object_or_404(NeuroReport.objects.select_related("ryear"), pk=pk, is_active=True)


def _crumb(label: str, url: str | None = None) -> dict[str, str | None]:
    return {"label": label, "url": url}


def _section_label(database: Database) -> str:
    """The database's section name, or "" when it only repeats the database's name."""
    name = database.section.name if database.section else ""
    same = name.strip().casefold() in {
        (database.label or "").strip().casefold(),
        database.name.strip().casefold(),
    }
    return "" if same else name


def _database_crumbs(database: Database, *extra: dict[str, str | None]) -> list[dict[str, str | None]]:
    crumbs = [_crumb(_("Home"), reverse("reports:overview"))]
    if _section_label(database):
        crumbs.append(_crumb(database.section.name))
    crumbs.append(
        _crumb(database.label or database.name, reverse("reports:database_dashboard", args=[database.id]))
    )
    crumbs.extend(extra)
    return crumbs


def _database_actions(database: Database, current: str) -> list[dict[str, Any]]:
    """The dashboard header buttons of v2: analytical, map, snapshot, raw data."""
    items = [
        ("database_dashboard", _("Dashboard"), "grid", _("Status of every master indicator")),
        ("database_analytical", _("Analytical view"), "table", _("Pivot table to cut the records your way")),
        ("database_map", _("Intervention map"), "map", _("Records by governorate, district and site")),
        ("database_snapshot", _("Snapshot"), "printer", _("Print-ready one-page summary")),
    ]
    actions = [
        {
            "label": label,
            "url": reverse(f"reports:{name}", args=[database.id]),
            "icon": icon,
            "hint": hint,
            "current": name == current,
        }
        for name, label, icon, hint in items
    ]
    actions.append(
        {
            "label": _("Raw data"),
            "hint": _("Every record of the year as a spreadsheet"),
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


# ------------------------------------------------------------------------- Home, country overview, daily
# review The parts the site root answered when it was the country overview: an open tab, or a link kept from
# then, still asks for them at "/" and gets them from their page now.
COUNTRY_PARTS = ("kpis", "figures", "news")
REVIEW_PARTS = ("review", "review-more")
REVIEW_KEYS = ("review", "review_latest", "review_id")  # the daily review's tabs and its folded findings


def _with_query(url: str, query: str) -> str:
    return f"{url}?{query}" if query else url


@login_not_required
@require_GET
def root(request: HttpRequest) -> HttpResponse:
    """The site root: the public landing page for visitors; signed in, the page the person opens first
    (:func:`neurodb.indicators.services.navigation.start_route`: Ask NeuroDB while the AI is on and their menu
    shows it, else Home). Signing in (``LOGIN_REDIRECT_URL``) and the logo lead here.

    The site root was Home, and before it the country overview and its daily review card: what was asked of it
    then still answers. A part of the country overview (``kpis``, ``figures``, ``news``) or of the review
    (``review``, ``review-more``) is answered by that page, any other part by Home (a tab opened when Home was
    here); a page asked with the overview's filters (``year``, ``governorate``) or a review tab (``review``,
    ``review_latest``, ``review_id``) is sent there with the same query, and any other query (Home's
    ``section``) to Home with it (``_eager=1`` included: an old print or no-JavaScript link lands on the page
    drawn at once). Neither page sends anyone back here. Donors never get here (``DonorScopeMiddleware``)."""
    if not request.user.is_authenticated:
        from neurodb.web.views import landing

        return landing(request)
    part = getattr(request, "part", None)
    if part in COUNTRY_PARTS:
        return country_overview(request)
    if part in REVIEW_PARTS:
        return daily_review(request)
    if part is not None:
        return home(request)
    query = request.GET.urlencode()
    if getattr(request, "parts_eager", False):  # web.parts took it out of GET: the page asked keeps it
        query = f"{query}&{EAGER_KEY}=1" if query else f"{EAGER_KEY}=1"
    if "year" in request.GET or "governorate" in request.GET:
        return redirect(_with_query(reverse("reports:country_overview"), query))
    if any(key in request.GET for key in REVIEW_KEYS):
        return redirect(_with_query(reverse("reports:daily_review"), query))
    if query:
        return redirect(_with_query(reverse("reports:overview"), query))
    return redirect(navigation.start_route(request.user))


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    """Home (``reports/home_views.py``), at its own address since Ask NeuroDB opens first (:func:`root`); its
    menu row opens it. Donors never get here (``DonorScopeMiddleware``)."""
    from .home_views import home_page

    return home_page(request)


def _iso_date(text: str) -> datetime.date | None:
    """A date written ``2026-10-11`` in the query, or None when there is none or it is not a date."""
    try:
        return datetime.date.fromisoformat(text.strip())
    except ValueError:
        return None


def _row_id(text: str) -> int | None:
    """A row's id written in the query (a whole number above zero), or None when there is none."""
    text = text.strip()
    if not (text.isascii() and text.isdigit()) or int(text) == 0:
        return None
    return int(text)


def _overview_chart_data(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The JSON the overview charts read (static/js/charts.js), shaped for each builder, per part: the
    ``figures`` part holds every chart of the page (impact, money, delivery, progress), in its own
    ``json_script`` (``overview-figures-data``), with the status labels the builders read.

    Twelve-month blocks become ``{labels, series: {name: [...]}, colors}`` for the "lines" and
    "grouped" builders, and an empty dict when every value is zero so the chart shows its empty state.
    Children reached by month has one series, eTools.
    """
    impact, money, delivery, progress = (
        data.get(key) or {} for key in ("impact", "money", "delivery", "progress")
    )

    scope = data.get("scope") or {}
    today = str(scope.get("today") or "")
    # In the current year the months still to come are left off, not drawn as zeros.
    shown = int(today[5:7]) if today[:4] == str(scope.get("year")) and today[5:7].isdigit() else 12

    def series(
        block: dict[str, Any],
        names: dict[str, str],
        colors: dict[str, str],
        planned: tuple[str, ...] = (),
        last: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """``planned`` series (visits planned, points due) keep all twelve months; the others stop at
        the current month, the months to come shown empty rather than as zeros; ``last`` stops a series
        earlier, after the last month its source reported."""
        last = last or {}
        values = {
            label: [
                float(v or 0) if key in planned or (i < shown and i < last.get(key, 12)) else None
                for i, v in enumerate(block.get(key) or [])
            ]
            for key, label in names.items()
        }
        if not any(any(v for v in vals if v) for vals in values.values()):
            return {}
        if not planned:  # nothing planned ahead: the months to come are left off
            values = {label: vals[:shown] for label, vals in values.items()}
            return {"labels": list(block.get("labels") or [])[:shown], "series": values, "colors": colors}
        return {"labels": list(block.get("labels") or []), "series": values, "colors": colors}

    by_section_money = money.get("by_section") or []
    monthly = series(impact.get("monthly") or {}, {"etools": "eTools"}, {"eTools": "--nd-primary"})
    figures = {
        "impact": {
            "by_section": [r for r in impact.get("by_section") or [] if r.get("target") or r.get("achieved")],
            "monthly": monthly,
        },
        "money": {
            "by_section": [
                [r.get("section"), r.get("cost_per_child")]
                for r in by_section_money
                if r.get("cost_per_child") is not None
            ],
            "scatter": [
                {
                    "name": r.get("section"),
                    "x": r.get("disbursed_percent"),
                    "y": r.get("achieved_percent"),
                    "ahead": bool(r.get("ahead")),
                }
                for r in by_section_money
                if r.get("disbursed_percent") is not None and r.get("achieved_percent") is not None
            ],
            "by_donor": money.get("by_donor") or [],
        },
        "delivery": {
            "by_section": [r for r in delivery.get("by_section") or [] if r.get("total")],
            "findings_by_rating": delivery.get("findings_by_rating") or [],
        },
        "progress": {
            "tpm": series(
                progress.get("tpm") or {},
                {"planned": "Planned", "completed": "Completed", "overdue": "Overdue"},
                {"Planned": "--nd-neutral", "Completed": "--nd-success", "Overdue": "--nd-danger"},
                planned=("planned",),
            ),
            "action_points": series(
                progress.get("action_points") or {},
                {"due": "Due", "closed": "Closed", "past_due": "Past due"},
                {"Due": "--nd-neutral", "Closed": "--nd-success", "Past due": "--nd-danger"},
                planned=("due",),
            ),
        },
        "status_counts": delivery.get("status_counts") or {},
        "labels": pd_monitoring_service.LABELS,
    }
    return {"figures": figures}


def _whats_new(sections: list[str]) -> list[tuple[Any, str]]:
    """The notable changes of the last two days in the chosen sections, for the Country overview's card (in
    the ``news`` part only), sections matched to NeuroDB's as Home matches them (names that match no NeuroDB
    section: nothing, never the whole country's under their label)."""
    from neurodb.graph import news

    ids, matched = home_service.section_ids(sections)
    if not matched:
        return []
    since = timezone.now() - datetime.timedelta(days=2)
    return [(c, news.sentence(c)) for c in news.recent(since, sections=ids)[:6]]


def _review_services():
    """The daily review's services, or None where the review app is not installed."""
    try:
        from neurodb.review import services as review_services
    except ImportError:
        return None
    return review_services


def _review_exists() -> bool:
    """Whether a daily review has ever succeeded: the review page keeps its card's place only then (one
    cheap query; the card itself is a part)."""
    try:
        from neurodb.review.models import DailyReview
    except ImportError:
        return False
    return DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).exists()


def _month_names() -> dict[int, str]:
    """``{1: "Jan", ... 12: "Dec"}`` in the active language: the months the key figures name ("Jan–Sep")."""
    from django.utils.formats import date_format

    return {month: date_format(datetime.date(2000, month, 1), "M") for month in range(1, 13)}


def _year_choices(year: int) -> list[dict[str, Any]]:
    """The Year select of a filter bar: the calendar years a page offers (``years.available()``), this year
    marked, and the year shown even when it is older than the list."""
    this_year = years.this_year()
    numbers = years.available()
    if year not in numbers:
        numbers = sorted({*numbers, year}, reverse=True)
    return [{"value": y, "this_year": y == this_year} for y in numbers]


def _scope_query(sections: list[str], year: int | None = None) -> str:
    """A filter written out for a link, so the page it opens never takes a first-load default: the sections
    (repeated), or ``year`` alone for every section."""
    query = QueryDict(mutable=True)
    if sections:
        query.setlist("section", sections)
    elif year is not None:
        query["year"] = str(year)
    return query.urlencode()


@require_GET
def country_overview(request: HttpRequest) -> HttpResponse:
    """The country dashboard: impact on children, value for money, delivery and assurance, progress.

    Filters: ``year`` (a calendar year, :func:`neurodb.core.years.resolve`: this year by default), ``section``
    (repeated) and ``governorate``. A first load with no query string at all shows the user's own section, as
    Home does (:func:`neurodb.reports.home.default_sections`: every section for Management and for people
    without a section). Its figures are eTools'.

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (purpose, filter
    bar, subtitle, freshness strip) with a placeholder per data block, and each block is asked for at this
    address plus ``_part``, with the resolved filter (the own section written out) and ``as_of``, the
    fingerprint of the figures the frame was drawn with:

    - ``kpis`` and ``figures`` (impact, money, delivery, TPM and action point progress) read one
      overview build (:func:`neurodb.reports.overview.build`, shared by every worker); when that build
      is already stored, the frame draws both inline, so a warm overview asks only for ``news``, which is
      never stored; otherwise ``figures`` reads the build ``kpis`` read for ``as_of``, even when a sync
      finished before or between them;
    - ``news`` (What's new).

    Home shows the key figures of this year (the same ``kpis`` build), and today's findings are on the daily
    review's page (:func:`daily_review`).
    """
    year = years.resolve(request.GET.get("year"))
    today = years.today()
    options = functools.cache(lambda: overview_service.options(year))  # the frame's filter bar only
    sections = [s for s in request.GET.getlist("section") if s]
    governorate = request.GET.get("governorate", "").strip()
    own_section: list[str] = []
    if first_load(request):  # first load, no choice made yet: the user's own section (Home's rule)
        own_section = home_service.default_sections(request.user, options()["sections"])
        if own_section:
            sections = own_section
    keep = QueryDict(mutable=True)  # the section, year and PD scope every drill-down link carries
    keep.setlist("section", sections)
    keep["year"] = str(year)  # the eTools pages read a calendar year
    keep["scope"] = "year"  # every PD running in the year, as the overview counts them
    base = QueryDict(mutable=True)  # links back to this page: section and year
    base.setlist("section", sections)
    base["year"] = str(year)
    # the programmes page filters by section, not by year; every PD status written out (``status=``), as the
    # tile counts every PD running in the year whatever its status now (the page opens on Active)
    programmes = QueryDict(mutable=True)
    programmes.setlist("section", sections)
    programmes["status"] = ""
    here = base.copy()  # ... and the governorate
    if governorate:
        here["governorate"] = governorate
    previous = QueryDict(mutable=True)  # January's "See {last year}": the same sections, last year
    previous.setlist("section", sections)
    previous["year"] = str(year - 1)
    scope = {  # what the parts read besides their own figures
        "selected": {"section": sections, "governorate": governorate, "year": str(year)},
        "labels": pd_monitoring_service.LABELS,
        "keep_query": keep.urlencode(),
        "programmes_query": "?" + programmes.urlencode(),
        "base_query": base.urlencode(),
        "page_query": here.urlencode(),
        "previous_query": previous.urlencode(),
        "month_names": _month_names(),
    }

    def shell() -> dict[str, Any]:
        jobs = overview_service.freshness(overview_service.freshness_jobs())
        return {
            "page_title": _("Country overview"),
            "page_subtitle": " · ".join(
                [
                    str(year),
                    ", ".join(sections) if sections else _("every section"),
                    governorate or _("all governorates"),
                ]
            ),
            "breadcrumbs": [_crumb(_("Home"), reverse("reports:overview")), _crumb(_("Country overview"))],
            "year": year,
            "today": today,
            "own_section": own_section,
            "options": options(),
            "year_choices": _year_choices(year),
            "freshness": jobs,
            "freshness_note": overview_service.freshness_note(jobs),
            "review_query": _scope_query(sections, year),
        }

    # The figures the frame is drawn with (their fingerprint: overview_service.fingerprint): a part request
    # reads them from its address, so ``figures`` reads the build ``kpis`` read even when a sync finished
    # before or between them, while it is stored (compute.once_as_of). Any other request reads them now.
    as_of = request.GET.get("as_of", "") if getattr(request, "part", None) else overview_service.fingerprint()
    # The resolved filter of every part request: the sections written out (``section=`` for every
    # section, so a part never takes the user's own section again), the year always, and the figures'
    # fingerprint.
    resolved = QueryDict(mutable=True)
    resolved.setlist("section", sections or [""])
    resolved["year"] = str(year)
    if governorate:
        resolved["governorate"] = governorate
    if as_of:
        resolved["as_of"] = as_of
    build_scope = overview_service.Scope(year=year, sections=sections, governorate=governorate, today=today)
    data = functools.cache(lambda: overview_service.build(build_scope, as_of=as_of))
    stored = functools.cache(lambda: overview_service.stored(build_scope))
    parts = {
        "kpis": Part("reports/parts/overview_kpis.html", lambda: {"data": data()}, ready=stored),
        "figures": Part(
            "reports/parts/overview_figures.html",
            lambda: {"data": data(), "chart": _overview_chart_data(data())["figures"]},
            ready=stored,
        ),
        "news": Part(
            "reports/parts/overview_news.html",
            lambda: {"whats_new": _whats_new(sections)},
        ),
    }
    return render_page(
        request, "reports/overview.html", scope=scope, shell=shell, parts=parts, query=resolved.urlencode()
    )


overview = country_overview  # the view's name before Home took the site root


@require_GET
def daily_review(request: HttpRequest) -> HttpResponse:
    """The daily review's page (``/daily-review/``): the latest review, the day before, the last seven days or
    a date (``review``), its findings narrowed to the sections (``section``, repeated; a first load shows the
    user's own section by Home's rule, :func:`neurodb.reports.home.default_sections`).

    The page loads in parts: the frame (purpose, scope line) at once, then ``review`` (the card) and, when its
    *Show more* opens, ``review-more`` (its folded findings, maybe hours later: its address carries
    ``review_latest``, the latest review the card read, and ``review_id``, the review it shows, so the
    findings are always the card's, even after a newer review has run, and never another run's when the card's
    review has been run again since). Home shows its summary (:func:`neurodb.review.services.digest`)."""
    today = years.today()
    year = today.year
    options = functools.cache(lambda: {"sections": overview_service.section_options(year)})
    sections = [s for s in request.GET.getlist("section") if s]
    review_day = request.GET.get("review", "")
    review_latest = _iso_date(request.GET.get("review_latest", ""))  # the folded findings: their card's
    review_id = _row_id(request.GET.get("review_id", ""))  # ... and the review it shows
    own_section: list[str] = []
    if first_load(request):
        own_section = home_service.default_sections(request.user, options()["sections"])
        if own_section:
            sections = own_section
    here = QueryDict(mutable=True)  # the card's tabs and history: the sections written out
    here.setlist("section", sections or [""])
    scope = {
        "selected": {"section": sections, "governorate": ""},
        "page_query": here.urlencode(),
        "base_query": here.urlencode(),
    }

    def shell() -> dict[str, Any]:
        own = home_service.own_names(request.user, options) if not sections else []
        return {
            "page_title": _("Daily review"),
            "breadcrumbs": [_crumb(_("Home"), reverse("reports:overview")), _crumb(_("Daily review"))],
            "today": today,
            "own_section": own_section,
            # the scope line (reports/partials/_home_scope.html)
            "own_default": own_section,
            "scope_sections": sections,
            "own_names": own,
            "own_query": _scope_query(own),
            "schedule": home_service.review_schedule(),
            "has_review": _review_exists(),
        }

    resolved = QueryDict(mutable=True)
    resolved.setlist("section", sections or [""])
    if review_day:
        resolved["review"] = review_day
    if review_latest:
        resolved["review_latest"] = review_latest.isoformat()
    if review_id:
        resolved["review_id"] = str(review_id)
    review_services = _review_services()
    review = functools.cache(
        lambda: (
            review_services.for_page(review_day or None, sections, latest=review_latest)
            if review_services
            else None
        )
    )

    def folded() -> dict[str, Any]:
        """The card's folded findings, asked for when its *Show more* opens, maybe hours after the card was
        drawn. ``review_latest`` brings back the reviews as the card read them; ``review_id`` is the review
        it shows. A run replaces the day's review with a new one (the admin's *Run the daily review now*, a
        second run of the morning's job), so when the card's review is no longer the one its tab reads,
        nothing of that card is left to show: the part says so (``replaced``), never another run's
        findings."""
        page = review()
        shown = page["review"] if page else None
        if review_id is not None and getattr(shown, "pk", None) != review_id:
            return {"review": None, "replaced": True}
        return {"review": page}

    parts = {
        "review": Part("reports/parts/overview_review.html", lambda: {"review": review()}),
        "review-more": Part("reports/parts/overview_review_more.html", folded),
    }
    return render_page(
        request,
        "reports/daily_review.html",
        scope=scope,
        shell=shell,
        parts=parts,
        query=resolved.urlencode(),
    )


def _brief_chart_data(data: dict[str, Any]) -> dict[str, Any]:
    """The JSON the management brief's charts read (static/js/charts.js): every chart of the brief is in the
    ``sections`` part, in its own ``json_script`` (``brief-sections-data``), with the status labels the
    builders read."""
    who, partners, money, action = (data.get(k) or {} for k in ("who", "partners", "money", "action"))
    lifecycle = action.get("lifecycle") or {}
    return {
        "bullets": [b for b in (data.get("pace") or {}).get("bullets") or [] if b.get("target")],
        "projection": (data.get("pace") or {}).get("projection") or {},
        "sex_age": who.get("sex_age") or {},
        "quadrant": partners.get("quadrant") or [],
        "flows": money.get("flows") if (money.get("flows") or {}).get("links") else {},
        "grants": [
            {
                "label": f"{g['donor']} · {g['grant']}",
                "value": g["unspent"],
                "flag": g["at_risk"],
                "days": g["days"],
            }
            for g in money.get("grants") or []
        ],
        "funded": [f for f in money.get("funded") or [] if f.get("required")],
        "lifecycle": [
            [str(_("Raised")), lifecycle.get("raised", 0)],
            [str(_("Acknowledged")), lifecycle.get("acknowledged", 0)],
            [str(_("Assigned")), lifecycle.get("assigned", 0)],
            [str(_("Closed")), lifecycle.get("closed", 0)],
        ]
        if lifecycle.get("raised")
        else [],
        "owners": [[o["owner"], o["open"]] for o in action.get("owners") or []],
        "labels": pd_monitoring_service.LABELS,
    }


@require_GET
def brief(request: HttpRequest) -> HttpResponse:
    """The management brief: the overview read for decisions (comparisons, confidence, equity,
    partners, money, action). Filters: ``year`` (a calendar year, this year by default) and ``section``
    (repeated); every section by default, as senior management reads the whole country. Its figures are
    eTools'.

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (purpose,
    filter bar with the "Figures as of" line, the tab bar, the Reference block, from
    :func:`neurodb.reports.brief.lineage`) with a placeholder for each of its two parts, asked for at this
    address plus ``_part``, with the resolved filter (``section=`` for every section, the year always) and
    ``as_of``, the fingerprint of the figures the frame was drawn with:

    - ``brief`` (the headline figures, the things to decide with their staff-only Assign links, the
      sections at a glance and the brief as text behind *Copy summary*), the group's leader;
    - ``sections`` (ahead or behind, how sure, who and where, partners, money, action, with their charts).

    Both read one brief build (:func:`neurodb.reports.brief.build`, shared by every worker): when it is
    already stored, the frame draws both inline, so a warm brief is one request; otherwise ``sections``
    reads the build ``brief`` read for ``as_of``, even when a sync finished before or between them.
    """
    year = years.resolve(request.GET.get("year"))
    today = years.today()
    sections = [s for s in request.GET.getlist("section") if s]
    keep = QueryDict(mutable=True)
    keep.setlist("section", sections)
    keep["year"] = str(year)
    keep["scope"] = "year"
    base = QueryDict(mutable=True)
    base.setlist("section", sections)
    base["year"] = str(year)
    scope = {  # what the parts read besides the build
        "selected": {"section": sections, "year": str(year)},
        "keep_query": keep.urlencode(),
        "base_query": base.urlencode(),
        "overview_query": "?" + base.urlencode(),
    }
    tabs = [
        ("brief", _("Brief")),
        ("pace", _("Ahead or behind")),
        ("confidence", _("How sure")),
        ("who", _("Who and where")),
        ("partners", _("Partners")),
        ("money", _("Money")),
        ("action", _("Action")),
        ("reference", _("Reference")),
    ]

    def shell() -> dict[str, Any]:
        return {
            "page_title": _("Management brief"),
            "page_subtitle": " · ".join([str(year), ", ".join(sections) if sections else _("every section")]),
            "breadcrumbs": [
                _crumb(_("Home"), reverse("reports:overview")),
                _crumb(_("Management brief")),
            ],
            "year": year,
            "today": today,
            "options": overview_service.options(year),
            "year_choices": _year_choices(year),
            "tabs": tabs,
            "lineage": brief_service.lineage(year),
            "dictionary": brief_service.DICTIONARY,
        }

    # The figures the frame is drawn with (their fingerprint: brief_service.fingerprint): a part request
    # reads them from its address, so the second part reads the build the first one read even when a
    # sync, a review or an edit came before or between them, while it is stored (compute.once_as_of). Any
    # other request reads them now.
    as_of = request.GET.get("as_of", "") if getattr(request, "part", None) else brief_service.fingerprint()
    # The resolved filter of every part request: the sections written out (``section=`` for every
    # section), the year always, and the figures' fingerprint.
    resolved = QueryDict(mutable=True)
    resolved.setlist("section", sections or [""])
    resolved["year"] = str(year)
    if as_of:
        resolved["as_of"] = as_of
    # the key's own reads (the hand-entered tables, the latest review) are made once, for the frame's look and
    # for the build it then draws
    build_scope = brief_service.Scope(year=year, sections=sections, today=today)
    data = functools.cache(lambda: brief_service.build(build_scope, as_of=as_of))
    stored = functools.cache(lambda: brief_service.stored(build_scope))
    parts = {
        "brief": Part("reports/parts/brief_brief.html", lambda: {"data": data()}, ready=stored),
        "sections": Part(
            "reports/parts/brief_sections.html",
            lambda: {"data": data(), "chart": _brief_chart_data(data())},
            ready=stored,
        ),
    }
    return render_page(
        request, "reports/brief.html", scope=scope, shell=shell, parts=parts, query=resolved.urlencode()
    )


# ------------------------------------------------------------------------- databases


@require_GET
def databases(request: HttpRequest) -> HttpResponse:
    """The ActivityInfo databases of a reporting year: one card each (indicators, status, records,
    partners, last import), the year's totals and the import runs. Was the bottom of the overview. Data
    health no longer lists the archive's imports: the imports panel links to the administration's runs of the
    two imports instead, for those who can open them. ``activityinfo_archived``: the import is no longer
    followed (``activityinfo.watched()`` false): the page then says so rather than call a database stale, and
    whether its scheduled job still runs (``activityinfo_import_on``)."""
    from neurodb.indicators.services import activityinfo

    state = activityinfo.state()
    year = services.resolve_year(request.GET.get("year"))
    data = facts.overview(year) if year else None
    context = {
        "page_title": _("ActivityInfo databases"),
        "page_subtitle": _("Reporting year %(year)s") % {"year": year.name} if year else "",
        "breadcrumbs": [
            _crumb(_("Home"), reverse("reports:overview")),
            _crumb(_("ActivityInfo databases")),
        ],
        "year": year,
        "activityinfo_archived": not state.watched,
        "activityinfo_import_on": state.schedule_on,
        "data": data,
        "labels": LABELS,
        "chart_data": {"status_counts": data["status_counts"], "labels": LABELS} if data else {},
    }
    return render(request, "reports/databases.html", context)


def _status_reference(year: int, day: datetime.date) -> dict[str, Any]:
    """What an ActivityInfo status is measured against, shown next to the statuses: the share of the
    year elapsed at ``day`` (today on the dashboards, the end of the period on a report)."""
    from neurodb.indicators.services.tracking import TOLERANCE, percentage_of_year_elapsed

    return {"date": day, "elapsed": percentage_of_year_elapsed(year, day), "tolerance": TOLERANCE}


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
        "page_subtitle": _database_subtitle(database),
        "breadcrumbs": _database_crumbs(database),
        "actions": _database_actions(database, "database_dashboard"),
        "database": database,
        "dashboard": dash,
        "indicators": indicators,
        "methods": facts.METHODS,
        "reported": reported,
        "reporting_progress": round(reported * 100 / len(dash.indicators)) if dash.indicators else 0,
        "labels": LABELS,
        "status": status,
        "q": q,
        "status_ref": _status_reference(dash.year, datetime.date.today()),
        "forecast_risk": _database_forecast_risk(database),
        "editor_reports": _editor_hpm_reports(request, database),
        "chart_data": {
            "status_counts": dash.status_counts,
            "labels": LABELS,
            "monthly": facts.monthly_by_indicator(facts.fact_filter(database), dash.indicators),
        },
    }
    template = "reports/partials/indicator_table.html" if request.htmx else "reports/database_dashboard.html"
    return render(request, template, context)


def _database_forecast_risk(database: Database) -> list[Any]:
    """The database's indicators likely to fall short by December (its dashboard's card, in the archive):
    only while ActivityInfo is in use and for a database of this year, with this year's forecasts. An
    archive's forecasts are no longer made (SPEC_HOME §8), and a past year's December has passed: no card."""
    from neurodb.indicators.services import activityinfo
    from neurodb.insights.services import at_risk

    this_year = years.this_year()
    if not activityinfo.in_use() or years.number_of(database.reporting_year) != this_year:
        return []
    return at_risk(database_id=database.pk, limit=10, year=this_year)


def _database_subtitle(database: Database) -> str:
    """The section, unless it repeats the database's name; then what the page is."""
    if section := _section_label(database):
        return section
    year = database.reporting_year.name if database.reporting_year else ""
    return _("ActivityInfo database · %(year)s") % {"year": year} if year else _("ActivityInfo database")


def _editor_hpm_reports(request: HttpRequest, database: Database) -> list[NeuroReport] | None:
    """For a section editor on a database of their own section: the HPM reports where they can
    comment on its indicators (the one thing the role adds in the pages). None for other users."""
    if role_of(request.user) != SECTION_EDITOR or not can_edit_section(request.user, database.section_id):
        return None
    return list(
        NeuroReport.objects.filter(
            is_active=True, is_hpm=True, neuroreportmasterindicator__master__database=database
        )
        .distinct()
        .order_by("name")
    )


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
        "status_ref": _status_reference(snap["dashboard"].year, datetime.date.today()),
        "chart_data": {
            "status_counts": snap["dashboard"].status_counts,
            "labels": LABELS,
            "monthly": facts.monthly_by_indicator(facts.fact_filter(database), snap["dashboard"].indicators),
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
    # The value column adds up one indicator at a time: the additive (SUM) master indicators.
    indicators = MasterIndicator.objects.filter(database=database, is_active=True).filter(
        Q(aggregation_method="SUM") | Q(aggregation_method="") | Q(aggregation_method__isnull=True)
    )
    context = {
        "page_title": _("%(name)s · Intervention map") % {"name": database.label or database.name},
        "page_subtitle": _("Records by admin area and site"),
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
        "indicators": indicators.order_by("sequence", "awp_code"),
        "indicator": next((m for m in indicators if str(m.id) == filters.get("indicator")), None),
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
    summary = facts.indicator_summary(database, master.id)
    context = {
        "page_title": master.name,
        "page_subtitle": _("Sub-indicators of %(code)s") % {"code": master.awp_code},
        "breadcrumbs": _database_crumbs(database, _crumb(master.name)),
        "database": database,
        "master": master,
        "rows": rows,
        "summary": summary,
        "methods": facts.METHODS,
        "status_ref": _status_reference(summary["year"], datetime.date.today()) if summary else None,
        "analytical_url": reverse("reports:database_analytical", args=[database.id]),
    }
    template = "reports/partials/indicator_detail.html" if request.htmx else "reports/indicator_detail.html"
    return render(request, template, context)


# ------------------------------------------------------------------------- Neuro Reports / HPM


def _bad_period(request: HttpRequest) -> HttpResponse | None:
    """A 400 for a ?month= or ?quarter= that does not parse, as the HPM API answers."""
    raw_month, raw_quarter = request.GET.get("month", ""), request.GET.get("quarter", "")
    if raw_month and services.parse_month(raw_month) is None:
        return HttpResponse(_("Invalid month: use a number from 1 to 12."), status=400)
    if raw_quarter and services.parse_quarter(raw_quarter) is None:
        return HttpResponse(_("Invalid quarter: use Q1, Q2, Q3 or Q4."), status=400)
    return None


def _report_context(request: HttpRequest, report: NeuroReport, page: str) -> dict[str, Any]:
    month = services.parse_month(request.GET.get("month"))
    quarter = services.parse_quarter(request.GET.get("quarter"))
    data = facts.neuroreport(report, month=month, quarter=quarter)
    # Only periods that have ended: a future month would show year-to-date values as its total.
    months = [{"value": m, "label": f"{m:02d}"} for m in range(1, data["last_month"] + 1)]
    other_years = (
        NeuroReport.objects.filter(report_code=report.report_code, is_active=True)
        .exclude(id=report.id)
        .select_related("ryear")
        .order_by("-ryear__name")
        if report.report_code
        else []
    )
    return {
        "report": report,
        "data": data,
        "months": months,
        "quarters": data["quarters"],
        "labels": LABELS,
        "other_years": [
            {
                "year": r.ryear.name if r.ryear else "",
                "name": r.name,
                "url": reverse(f"reports:{page if r.is_hpm else 'report_dashboard'}", args=[r.id]),
            }
            for r in other_years
        ],
        "breadcrumbs": [_crumb(_("Home"), reverse("reports:overview")), _crumb(report.name)],
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
    if bad := _bad_period(request):
        return bad
    context = _report_context(request, report, "report_dashboard")
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
            _crumb(_("Home"), reverse("reports:overview")),
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
    if not report.is_hpm:
        # Only HPM reports have an HPM view (and comments); the others open on their dashboard.
        query = request.META.get("QUERY_STRING", "")
        url = reverse("reports:report_dashboard", args=[report.id])
        return redirect(url + (f"?{query}" if query else ""))
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
    if bad := _bad_period(request):
        return bad
    context = _report_context(request, report, "report_hpm")
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


# Programme documents' tabs: the list, its indicators, then the old NeuroDB's Summary Info and Funded Programs
PROGRAMME_TABS = ("documents", "indicators", "summary", "overall")
# the filters of the Funded programme overall tab: the old Funded Programs page's, without office and
# partner type (removed from every page: reports.old_filters)
OVERALL_PARAMS = (
    "pd_q",
    "donor",
    "grant",
    "partner",
    "cso_type",
    "section",
    "status",
    "start_from",
    "start_to",
)
START_YEARS = ("start_from", "start_to")  # only the overall tab has them


def _query_only(params: QueryDict, keep: tuple[str, ...], **first: Any) -> str:
    """A query string of ``first`` (in that order), then ``keep`` of ``params`` (the values set). The Status
    filter with nothing chosen (``status=`` alone: every status) is kept as it is, so the tab or file it opens
    never falls back to the page's default, Active (``PDFilters.from_page``)."""
    out = QueryDict(mutable=True)
    for key, value in first.items():
        out[key] = value
    for key in keep:
        values = [v for v in params.getlist(key) if v]
        if values:
            out.setlist(key, values)
        elif key == "status" and key in params:
            out.setlist(key, [""])
    return out.urlencode()


# the Active / All choice each tab opens on when its address names none
SCOPE_DEFAULTS = {"documents": "all", "indicators": "all", "summary": "active"}


def _programme_tab_urls(request: HttpRequest, params: QueryDict, scope: str | None = None) -> dict[str, str]:
    """The tab bar's addresses: each tab keeps the filters it has (the summary only its scope). ``scope``: the
    Active / All choice the page shows (None on the overall tab, which has none). The tabs open on different
    choices (:data:`SCOPE_DEFAULTS`), so each link names the choice it opens on when it is not that tab's own
    default, and a tab change keeps the programme documents shown. The list, Indicators and the overall tab
    read their Status filter as well (Active when the address names none, since 25 Nov 2026; the list's and
    Indicators' All is drawn "Status filter"): the summary they open is on Active when they read the active
    programme documents alone (their *Active only* included), else on All. The summary's Active opens them
    as they open (no scope, no status: their Status filter's Active), so the list as it opens, to the
    summary and back, comes back to the bare list; its All (every programme document but drafts and
    cancelled ones) opens them on every status (``status=``), never on their Status filter's default."""
    path = request.path
    params = params.copy()
    to_list, to_summary = scope, scope  # the choice the list and Indicators, and the summary, open on
    if params.get("tab") == "summary":
        if scope == "all":
            params.setlist("status", [""])
        else:
            to_list = SCOPE_DEFAULTS["documents"]
            params.pop("status", None)
    elif scope != "active":
        active_alone = partnerships.PDFilters.from_page(params).statuses == list(partnerships.ACTIVE_STATUSES)
        to_summary = "active" if active_alone else "all"
    if to_list in services.PD_SCOPES:
        params.setlist("scope", [to_list])

    def scoped(tab: str) -> dict[str, str | None]:
        """The scope the link to ``tab`` names: none when it opens on that tab's own default."""
        return {"scope": None} if to_list == SCOPE_DEFAULTS[tab] else {}

    return {
        "documents": _with_query(
            path, _query_with(params, ("tab", "export", "indicator", *START_YEARS), **scoped("documents"))
        ),
        "indicators": _with_query(
            path, _query_with(params, ("export", *START_YEARS), tab="indicators", **scoped("indicators"))
        ),
        "summary": _with_query(
            path,
            _query_only(
                params,
                (),
                tab="summary",
                **({} if to_summary == SCOPE_DEFAULTS["summary"] else {"scope": to_summary}),
            ),
        ),
        "overall": _with_query(path, _query_only(params, OVERALL_PARAMS, tab="overall")),
    }


def _programme_page(
    request: HttpRequest, tab: str, params: QueryDict, scope: str | None = None
) -> dict[str, Any]:
    """What every tab of Programme documents shows around its own content. ``params``: the page's address
    without the filters the owner removed from every page (office, emergency: ignored, never in a link).
    ``scope``: the Active / All choice the tab shows (None on the overall tab)."""
    return {
        "tab": tab,
        "tab_urls": _programme_tab_urls(request, params, scope),
        "page_title": _("Programme documents"),
        "page_subtitle": _("eTools partnerships with their donors and grants"),
        "breadcrumbs": [_crumb(_("Programme documents"))],
        "actions": [
            {
                "label": _("Planned locations (Excel)"),
                "url": reverse("reports:export_etools_locations"),
                "icon": "download",
            },
        ],
        "pd_q": params.get("pd_q", ""),
        "selected": {
            **{
                key: params.getlist(key)
                for key in ("partner", "section", "donor", "grant", "document_type", "cso_type")
            },
            # the PD statuses read: none in the address, the page's default (Active), shown chosen
            "status": partnerships.PDFilters.from_page(params).statuses,
        },
    }


@require_GET
def programmes(request: HttpRequest) -> HttpResponse:
    """Programme documents, and their indicators in one flat list (``tab=indicators``, Power BI's PD/SPD
    Indicators), with the same filter; the indicator list exports whole. ``tab=summary`` and
    ``tab=overall`` are the old NeuroDB's Summary Info and Funded Programs pages
    (:func:`_programme_funded`). The page reads its address without the filters the owner removed from every
    page (office, emergency: ignored, never in its links or downloads; :mod:`neurodb.reports.old_filters`)."""
    params = old_filters.without(request.GET)
    tab = params.get("tab") if params.get("tab") in PROGRAMME_TABS else "documents"
    if tab in ("summary", "overall"):
        return _programme_funded(request, tab, params)
    scope = params.get("scope") if params.get("scope") in services.PD_SCOPES else SCOPE_DEFAULTS[tab]
    # the start years belong to the overall tab: never a hidden filter here; the Status filter opens on Active
    filters = dataclasses.replace(partnerships.PDFilters.from_page(params), year_from=None, year_to=None)
    if tab == "indicators":
        # the section of each indicator, not of its programme document
        pds = partnerships.programme_documents(dataclasses.replace(filters, sections=[]), scope=scope)
        indicator_rows = reporting_service.pd_indicators(pds, params)
        export = params.get("export")
        if export in exports.FORMATS:
            columns = reporting_service.INDICATOR_COLUMNS
            if export == "xlsx":
                return exports.xlsx_response(
                    exports.export_filename("pd-indicators", "xlsx"),
                    [("PD indicators", columns, indicator_rows)],
                    typed=True,
                )
            return exports.stream_csv(
                exports.export_filename("pd-indicators", "csv"), columns, indicator_rows
            )
        page_obj = _paginate(request, indicator_rows)
        rows: list[dict[str, Any]] = []
    else:
        documents = partnerships.programme_documents(filters, scope=scope)
        if (params.get("pd_q") or "").strip():
            documents = documents.filter(number__icontains=params["pd_q"].strip())
        page_obj = _paginate(request, documents)
        rows = [
            {
                **services.pd_as_dict(pd),
                "budget_currency": pd.budget_currency or "",
                "past_end": partnerships.past_end_date(pd.status, pd.end),
            }
            for pd in page_obj
        ]
    context = {
        **_programme_page(request, tab, params, scope),
        # the indicator list exports whole; the PD list keeps its Copy and CSV of the page
        "downloads": [
            {
                "label": _("Excel of the filter"),
                "url": "?" + _query_with(params, (), tab="indicators", export="xlsx"),
            },
            {
                "label": _("CSV of the filter"),
                "url": "?" + _query_with(params, (), tab="indicators", export="csv"),
            },
        ]
        if tab == "indicators"
        else [],
        "indicator_q": params.get("indicator", ""),
        "scope": scope,
        "page_obj": page_obj,
        "rows": rows,
        "options": _programme_options(tab),
    }
    template = "reports/partials/programme_table.html" if request.htmx else "reports/programmes.html"
    return render(request, template, context)


def _programme_funded(request: HttpRequest, tab: str, params: QueryDict) -> HttpResponse:
    """*Funded programme summary info* (``tab=summary``: the old Summary Info (Active PDs) and (All PDs)
    pages, switched by ``scope``, Active first) and *Funded programme overall* (``tab=overall``: the old
    Funded Programs page, on its filters but office and partner type), with their downloads
    (:mod:`neurodb.partnerships.funded`). ``params``: the page's address without the removed filters."""
    today = timezone.localdate()
    scope = None
    if tab == "summary":
        scope = params.get("scope") if params.get("scope") in services.PD_SCOPES else SCOPE_DEFAULTS[tab]
    context = _programme_page(request, tab, params, scope)
    if tab == "summary":
        data = funded.summary(scope, today)
        if params.get("export") == "xlsx":
            return exports.xlsx_response(
                exports.export_filename(f"programme-summary-{scope}", "xlsx"),
                funded.summary_sheets(data),
                typed=True,
            )
        context.update(
            {
                "scope": scope,
                "summary": data,
                "summary_charts": data["charts"],
                "downloads": [
                    {
                        "label": _("Excel of the summary"),
                        "url": "?" + _query_only(params, ("scope",), tab="summary", export="xlsx"),
                    }
                ],
            }
        )
    else:
        # the filters the owner removed from every page (office, partner type) are not in ``params``; the
        # Programme documents tab's own (document type, PD numbers) are never read here, and the start years
        # are its two lists alone (never the API's ``years=``, which the page would not show)
        filters = dataclasses.replace(
            partnerships.PDFilters.from_page(params), document_types=[], numbers=[]
        ).with_start_years(params)
        pds = funded.programme_documents(filters)
        if pd_q := (params.get("pd_q") or "").strip():
            pds = pds.filter(number__icontains=pd_q)
        export = params.get("export")
        if export in exports.FORMATS:
            rows = (funded.export_row(r) for r in funded.overall_rows(pds, today))
            if export == "csv":
                return exports.stream_csv(
                    exports.export_filename("funded-programme-overall", "csv"), funded.OVERALL_COLUMNS, rows
                )
            return exports.xlsx_response(
                exports.export_filename("funded-programme-overall", "xlsx"),
                [("Funded programme overall", funded.OVERALL_COLUMNS, rows)],
                typed=True,
            )
        # the pages count the programme documents alone; the amounts are read for the page's rows only
        page_obj = _paginate(request, pds.select_related(None).only("id"))
        options = funded.filter_options()
        context.update(
            {
                "page_obj": page_obj,
                "overall": {
                    **funded.overall_totals(pds),
                    "rows": funded.overall_page(page_obj, today),
                },
                "options": options,
                "start_from": params.get("start_from", ""),
                "start_to": params.get("start_to", ""),
                "downloads": [
                    {
                        "label": _("Excel of the filter"),
                        "url": "?" + _query_only(params, OVERALL_PARAMS, tab="overall", export="xlsx"),
                    },
                    {
                        "label": _("CSV of the filter"),
                        "url": "?" + _query_only(params, OVERALL_PARAMS, tab="overall", export="csv"),
                    },
                ],
            }
        )
    template = "reports/partials/programme_table.html" if request.htmx else "reports/programmes.html"
    return render(request, template, context)


def _programme_options(tab: str) -> dict[str, Any]:
    """The Programmes filter's options. The Indicators tab filters on each indicator's own section, so
    its Section list also holds the sections the indicators name (a section only an indicator names
    can be chosen too), of the indicators the pages list (``identity.listed``)."""
    options = partnerships.pd_filter_options()
    if tab == "indicators":
        named = identity.listed(PDIndicator.objects.order_by()).values_list("section_name", flat=True)
        named = named.distinct()
        options["sections"] = sorted(set(options["sections"]) | {(x or "").strip() for x in named} - {""})
    return options


@require_GET
def programme_summary(request: HttpRequest) -> HttpResponse:
    """The old summary page's address (bookmarks): it opens the *Funded programme summary info* tab, on its
    scope."""
    scope = request.GET.get("scope")
    query = _query_only(
        QueryDict(f"scope={scope}" if scope in services.PD_SCOPES else ""), ("scope",), tab="summary"
    )
    return redirect(reverse("reports:programmes") + "?" + query)


@require_GET
def programme_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """A programme document's page (and its pop-up, an HTMX request: the HACT visit plan opens it). The page
    answers as before, with one part loaded after it (:mod:`neurodb.web.parts`): ``follow-up``, its open
    action points and its timeline (:func:`neurodb.datamart.timeline.follow_up`), at the end of the page.
    ``?timeline=all`` (its *Show older* link) lists every event: the part's address carries it."""
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    show_all = _timeline_all(request)

    def shell() -> dict[str, Any]:
        return {
            "page_title": pd.number or pd.title,
            "page_subtitle": pd.title,
            "breadcrumbs": [
                _crumb(_("Programme documents"), reverse("reports:programmes")),
                _crumb(pd.number or pd.title),
            ],
            "detail": partnerships.pd_detail(pd),
            "past_end": partnerships.past_end_date(pd.status, pd.end),
            "datamart": datamart.programme_datamart(pd),
            "knowledge_documents": linked_documents("programme_document", pd.pk),
            "fmm_pd": _fmm_panel("pd_summary", pd.pk),
            "can_set_main": headline.can_set(request.user, pd),
        }

    parts = {"follow-up": _follow_up_part(pd=pd, show_all=show_all)}
    template = "reports/partials/programme_detail.html" if request.htmx else "reports/programme_detail.html"
    return render_page(
        request, template, scope={"pd": pd}, shell=shell, parts=parts, query=_timeline_query(show_all)
    )


def _timeline_all(request: HttpRequest) -> bool:
    """``?timeline=all``: the timeline lists every event, not only the last 24 months."""
    return request.GET.get(timeline.PARAM) == timeline.ALL


def _timeline_query(show_all: bool) -> str:
    """The resolved query of the programme document and partner pages: the timeline's choice alone."""
    return urlencode({timeline.PARAM: timeline.ALL}) if show_all else ""


def _follow_up_part(**subject: Any) -> Part:
    """The part ``follow-up`` of the programme document and partner pages: open action points and the
    timeline (reports/parts/follow_up.html). Nothing is stored: it is read at each visit."""
    return Part("reports/parts/follow_up.html", lambda: {"follow_up": timeline.follow_up(**subject)})


@require_POST
def pd_main_indicator(request: HttpRequest, pk: int) -> HttpResponse:
    """Choose the programme document's unique beneficiaries indicator (``key``), or stop using one
    (``clear``): Administrators and Section editors of the PD's section. Its value is then the PD's
    children reached on the overview, the brief and the donor page."""
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    if not headline.can_set(request.user, pd):
        raise PermissionDenied
    if request.POST.get("clear"):
        if headline.clear_main(pd, request.user):
            messages.success(
                request,
                _(
                    "This programme document has no unique beneficiaries indicator any more: its largest "
                    "children indicator stands in for it."
                ),
            )
        return redirect("reports:programme_detail", pk=pd.pk)
    key = (request.POST.get("key") or "").strip()
    rows = pd_monitoring_service.indicators(pd_monitoring_service.Filters(pd_ids=[pd.id], scope="all"))
    row = next((r for r in rows if r.key == key), None) if key else None
    if row is None:
        messages.error(request, _("Choose one of this programme document's indicators."))
    elif not headline.is_number(row):
        messages.error(
            request,
            _("A percentage or a ratio cannot count beneficiaries: choose an indicator that counts people."),
        )
    else:
        headline.set_main(pd, row.key, row.title, request.user)
        messages.success(
            request,
            _("“%(title)s” is now this programme document's unique beneficiaries indicator.")
            % {"title": row.title},
        )
    return redirect("reports:programme_detail", pk=pd.pk)


def _fmm_on() -> bool:
    """Whether Monitoring insights is installed and switched on (no query)."""
    return apps.is_installed("neurodb.fmm") and bool(getattr(settings, "FMM_ENABLED", False))


def _fmm_panel(name: str, pk: int) -> dict[str, Any] | None:
    """The Monitoring insights panel of a partner or programme document page for this calendar year
    (``fmm.services.partner_summary`` or ``pd_summary``), read through a lazy import; None when that
    app is not installed or switched off, or has nothing on it."""
    if not _fmm_on():
        return None
    from neurodb.fmm import services as fmm_services

    return getattr(fmm_services, name)(pk, _this_year())


def _fmm_partner_seen(pk: int) -> bool:
    """Whether a field monitoring visit ever monitored the partner, the one case where its Monitoring
    insights panel (``fmm.services.partner_summary``, None otherwise) has anything to show: the partner
    page keeps the panel's place only then (one cheap query; the panel itself is a part)."""
    if not _fmm_on():
        return False
    from neurodb.fmm.models import Visit

    return Visit.objects.filter(partner_ids__contains=[int(pk)]).exists()


def _this_year() -> int:
    """This calendar year in Beirut, as the Monitoring insights page reads "this year"."""
    return years.this_year()


DONOR_PAGE_PDS = 10  # the Donors page lists the largest ones and links to Programmes for the rest


@require_GET
def donors(request: HttpRequest) -> HttpResponse:
    # the office the owner removed from every page is never read from the address (reports.old_filters)
    params = old_filters.without(request.GET)
    # the page has no start year: never a hidden filter (the Programme documents tab drops them too); its
    # Status filter opens on Active (PDFilters.from_page)
    filters = dataclasses.replace(partnerships.PDFilters.from_page(params), year_from=None, year_to=None)
    data = partnerships.donor_mapping(filters, activity_records=False)  # ActivityInfo's: the archive's only
    selected = {key: params.getlist(key) for key in ("donor", "grant", "partner", "section")}
    selected["status"] = filters.statuses
    # the full lists live on Programmes and Funds (each grant and what it reserves: Grants by donor is
    # hidden here): this page links there with the same filters, its PD statuses written out (``status=`` /
    # ``pd_status=`` alone for every status), so neither page falls back to its own default
    programmes_query = urlencode(
        [(k, v) for k in ("donor", "grant", "partner", "section") for v in selected[k]] + filters.status_query
    )
    funds_query = urlencode(
        [(k, v) for k in ("donor", "grant") for v in selected[k]]
        + [("pd_status", value) for _key, value in filters.status_query]
    )
    context = {
        "page_title": _("Donors"),
        "page_subtitle": _("What each donor funds and the programme documents they fund"),
        "breadcrumbs": [_crumb(_("Donors"))],
        "data": data,
        "top_programmes": sorted(data["programmes"], key=lambda p: -p["donations"])[:DONOR_PAGE_PDS],
        "programmes_url": f"{reverse('reports:programmes')}?{programmes_query}",
        "funds_url": f"{reverse('reports:funds')}?{funds_query}",
        "options": partnerships.pd_filter_options(),
        "selected": selected,
        "chart_data": {
            "funds_by_donor": data["funds_by_donor"],
            "funds_by_year": data["funds_by_year"],
        },
    }
    template = "reports/partials/donor_results.html" if request.htmx else "reports/donors.html"
    return render(request, template, context)


@require_GET
def partners(request: HttpRequest) -> HttpResponse:
    """Partners, by name, vendor number and CSO type. The partner type is no longer a filter of the page (the
    owner removed it from every page): an address that carries it is read without it
    (:mod:`neurodb.reports.old_filters`); the Type column stays."""
    params = old_filters.without(request.GET)
    page_obj = _paginate(request, partnerships.partners(params))
    context = {
        "page_title": _("Partners"),
        "page_subtitle": _("Implementing partners from eTools"),
        "breadcrumbs": [_crumb(_("Partners"))],
        "page_obj": page_obj,
        "options": partnerships.partner_filter_options(),
        "selected": {"cso_type": params.getlist("cso_type")},
        "q": params.get("q", ""),
    }
    template = "reports/partials/partner_table.html" if request.htmx else "reports/partners.html"
    return render(request, template, context)


@require_GET
def partner_profile(request: HttpRequest, pk: int) -> HttpResponse:
    """A partner's page: key figures, profile, visits and assurance, Monitoring insights, its eTools
    reporting, and its lists (programme documents, engagements, HACT, progress reports, financial findings,
    assessments, action points, monitoring visits). Its ActivityInfo records stay in the archive, on the
    partner's ActivityInfo page (:func:`partner_activityinfo`, opened by its address).

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (the header,
    the deleted notice, the knowledge base documents) with one placeholder, ``datamart``: everything from
    the key figures to the last list. Those blocks are interleaved on the page and read one computation,
    the partner's eTools Datamart figures (:func:`neurodb.datamart.services.partner_datamart`, model
    instances, never stored for other workers), with its programme documents, so they come in one request.
    Inside
    it, ``fmm``, the Monitoring insights panel (its own computation, loaded last), when Monitoring
    insights is on and a visit ever monitored the partner. After it, ``follow-up``: the partner's open action
    points and its timeline (:func:`neurodb.datamart.timeline.follow_up`). The page has no filter: the parts'
    query is empty, or ``timeline=all`` when the timeline lists every event (its *Show older* link). Nothing
    is stored, so an edit shows at the next visit.
    """
    # a partner deleted in eTools still opens (PDs and Datamart rows link to it), with a notice
    partner = get_object_or_404(PartnerOrganization, pk=pk)
    scope = {  # what the parts read besides their own figures
        "partner": partner,
        "labels": LABELS,
        # the partner's action points, the same search as the /action-points/ page
        "action_points_url": reverse("reports:action_points")
        + "?"
        + urlencode({"q": partner.vendor_number or partner.name}),
    }

    def shell() -> dict[str, Any]:
        return {
            "page_title": partner.name,
            "page_subtitle": " · ".join(x for x in (partner.partner_type, partner.cso_type) if x),
            "breadcrumbs": [
                _crumb(_("Partners"), reverse("reports:partners")),
                _crumb(partner.short_name or partner.name),
            ],
            "knowledge_documents": linked_documents("partner", partner.pk),
        }

    def figures() -> dict[str, Any]:
        profile = partnerships.partner_profile(partner)
        extra = datamart.partner_datamart(partner)
        return {
            "profile": profile,
            "datamart": extra,
            "fmm_seen": _fmm_partner_seen(partner.pk),
            "chart_data": {
                # staff trips from eTools Trips in the Datamart when synced, else the v2 travel tables,
                # next to the field monitoring and third-party visits the page lists
                "visits_by_year": datamart.visits_chart(
                    extra["staff_visits_by_year"] or profile["visits_by_year"],
                    extra["monitoring_visits_by_year"],
                ),
                "engagement_counts": profile["engagement_counts"],
                "etools_reports_by_year": extra["reports_by_year"],
            },
        }

    show_all = _timeline_all(request)
    parts = {"datamart": Part("reports/parts/partner_datamart.html", figures)}
    if _fmm_on():
        parts["fmm"] = Part(
            "reports/parts/partner_fmm.html",
            lambda: {"fmm_partner": _fmm_panel("partner_summary", partner.pk)},
        )
    parts["follow-up"] = _follow_up_part(partner=partner, show_all=show_all)
    return render_page(
        request,
        "reports/partner_profile.html",
        scope=scope,
        shell=shell,
        parts=parts,
        query=_timeline_query(show_all),
    )


@require_GET
def partner_activityinfo(request: HttpRequest, pk: int, database_id: int) -> HttpResponse:
    """What one partner reported in one ActivityInfo database: master indicators by month (modal/page)."""
    partner = get_object_or_404(PartnerOrganization, pk=pk)
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
        context["hact"] = datamart.hact_compliance(pd_monitoring_service.year_param(raw_year))
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


FUNDS_TABS = ("reservations", "contributions", "programmes")
FUNDS_LISTS = ("donor", "grant", "pd_status", "partner")
CONTRIBUTION_COLUMNS = (
    "partner",
    "partner_type",
    "cso_type",
    "programme_document",
    "pd_status",
    "currency",
    "planned_unicef_contribution",
    "total_budget",
    "partner_contribution",
    "partner_contribution_share_percent",
    "financial_contribution_during_reporting_periods",
    "financial_contribution_currency",
    "financial_contribution_as_of",
    "financial_contribution_report",
)
PROGRAMME_COLUMNS = (
    "pd_status",
    "programme_document",
    "grant",
    "donor",
    "start",
    "end",
    "days_to_end",
    "partner",
    "partner_type",
    "currency",
    "unicef_planned_budget",
    "total_budget",
    "funds_reserved_usd",
    "actual_disbursement_usd",
    "partner_contribution",
    "note",
)


def _contribution_row(r: dict[str, Any]) -> dict[str, Any]:
    reported = r["reported"] or {}
    return {
        "partner": r["partner"],
        "partner_type": r["partner_type"],
        "cso_type": r["cso_type"],
        "programme_document": r["programme_document"],
        "pd_status": r["pd_status"],
        "currency": r["currency"],
        "planned_unicef_contribution": r["unicef_planned"],
        "total_budget": r["total"],
        "partner_contribution": r["partner_contribution"],
        "partner_contribution_share_percent": (
            round(r["partner_share"], 1) if r["partner_share"] is not None else None
        ),
        "financial_contribution_during_reporting_periods": reported.get("value"),
        "financial_contribution_currency": reported.get("currency", ""),
        "financial_contribution_as_of": reported.get("as_of"),
        "financial_contribution_report": reported.get("report", ""),
    }


def _programme_row(r: dict[str, Any]) -> dict[str, Any]:
    """A Funded Programs row: the programme document's own amounts only on its first grant row."""
    own = r["pd_amounts"]
    return {
        "pd_status": r["pd_status"],
        "programme_document": r["programme_document"],
        "grant": r["grant"],
        "donor": r["donor"],
        "start": r["start"],
        "end": r["end"],
        "days_to_end": r["days_to_end"],
        "partner": r["partner"],
        "partner_type": r["partner_type"],
        "currency": r["currency"],
        "unicef_planned_budget": r["unicef_planned"] if own else None,
        "total_budget": r["total"] if own else None,
        "funds_reserved_usd": r["reserved"],
        "actual_disbursement_usd": r["disbursed"] if own else None,
        "partner_contribution": r["partner_contribution"] if own else None,
        "note": "" if own else "Programme document amounts are on its first row",
    }


@require_GET
def funds(request: HttpRequest) -> HttpResponse:
    """Funds: funds reservations and grants (``tab=reservations``), Partner Fund Monitoring
    (``tab=contributions``) and Funded Programs (``tab=programmes``), on one filter, with exports of the
    whole filtered set of the two Power BI views. The reservations tab reads the whole of
    :func:`neurodb.datamart.services.funds` (its totals, charts and table); the other two read only its filter
    choices (:func:`neurodb.datamart.services.funds_options`) and their own rows, and draw no chart. The page
    reads its address without the filters the owner removed from every page (partner type: ignored, never in
    its links or downloads; :mod:`neurodb.reports.old_filters`)."""
    from neurodb.datamart import funds as fund_views

    params = old_filters.without(request.GET)
    tab = params.get("tab") if params.get("tab") in FUNDS_TABS else "reservations"
    if tab == "reservations":
        data = datamart.funds(params)
    else:
        data = {
            "options": datamart.funds_options(),
            "filters": fund_views.FundFilters.from_params(params),
        }
    today = timezone.localdate()
    contributions = programmes = None
    if tab == "contributions":
        contributions = fund_views.partner_contributions(data["filters"], today)
    elif tab == "programmes":
        programmes = fund_views.funded_programmes(data["filters"], today)
    export = params.get("export")
    if export in exports.FORMATS and tab in ("contributions", "programmes"):
        if tab == "contributions":
            label, title, columns = "partner-fund-monitoring", "Partner fund monitoring", CONTRIBUTION_COLUMNS
            rows: Any = [_contribution_row(r) for r in contributions["rows"]]
        else:
            label, title, columns = "funded-programmes", "Funded programmes", PROGRAMME_COLUMNS
            rows = [_programme_row(r) for r in programmes["rows"]]
        if export == "csv":
            return exports.stream_csv(exports.export_filename(label, "csv"), columns, rows)
        return exports.xlsx_response(
            exports.export_filename(label, "xlsx"), [(title, columns, rows)], typed=True
        )
    if tab == "contributions":
        page_obj = _paginate(request, contributions["rows"])
    elif tab == "programmes":
        page_obj = _paginate(request, programmes["groups"])
    else:
        page_obj = _paginate(request, data["headers"])
    context = {
        "page_title": _("Funds"),
        "page_subtitle": _("Funds reservations, disbursements and grants by donor, from the eTools Datamart"),
        "breadcrumbs": [_crumb(_("Funds"))],
        "data": data,
        "f": data["filters"],
        "tab": tab,
        "contributions": contributions,
        "programmes": programmes,
        "page_obj": page_obj,
        "selected": {key: params.getlist(key) for key in FUNDS_LISTS},
        "year": params.get("year", ""),
        "year_query": params,  # the header's year menu keeps the filters the page reads (no partner type)
        "q": params.get("q", ""),
        "tab_urls": {
            name: request.path + "?" + _query_with(params, ("tab", "export"), tab=name) for name in FUNDS_TABS
        },
        "downloads": [
            {"label": _("Excel of the filter"), "url": "?" + _query_with(params, (), tab=tab, export="xlsx")},
            {"label": _("CSV of the filter"), "url": "?" + _query_with(params, (), tab=tab, export="csv")},
        ],
        # the reservations tab's charts (funds-chart-data)
        "chart_data": {
            "by_donor": data["by_donor"],
            "by_year": data["by_year"],
            "disbursed_by_year": data["disbursed_by_year"],
        }
        if tab == "reservations"
        else None,
    }
    template = "reports/partials/funds_table.html" if request.htmx else "reports/funds.html"
    return render(request, template, context)


# the Progress reports tabs the tab bar offers: Reporting progress and Partner view (tab=partners)
REPORTING_SHOWN_TABS = ("reports", "partners")
# every tab an address opens: By location and By disaggregation are hidden (in no tab bar, linked from no
# page) but their addresses still open them (old bookmarks, the figure trail, the admin's Power BI comparison)
REPORTING_TABS = (*REPORTING_SHOWN_TABS, "locations", "disaggregations")
DISAGGREGATION_BLOCKS = 25  # locations (each with its disaggregation lines) per page
REPORTING_LISTS = ("status", "report_type", "section", "report_number", "partner", "pd_status")
# the filters of an old address the page still applies (the owner removed them from its filter bar): named on
# a line with a link that removes them, as the figure trail and the Power BI comparison sheet read the same
# address
REPORTING_OLD_FILTERS = ("partner_type", "emergency")
REPORT_COLUMNS = (
    "programme_document",
    "pd_status",
    "partner",
    "section",
    "document_type",
    "report",
    "report_type",
    "report_status",
    "due_date",
    "period_start",
    "period_end",
    "submission_date",
    "indicator_reports",
    "unicef_focal_points_count",  # a count: a person is never written in an export (the page shows them)
    # the partner reports, or PRP's own list of progress reports (a report still owed: datamart.owed)
    "source",
)
FOLLOW_UP_COLUMNS = (
    "programme_document",
    "partner",
    "section",
    "report_status",
    "due_date",
    "report",
    "report_type",
    "indicator_reports",
    "source",
)
LOCATION_COLUMNS = (
    "programme_document",
    "pd_status",
    "partner",
    "section",
    "cp_output",
    "indicator",
    "emergency",
    "report",
    "report_type",
    "report_status",
    "period_start",
    "period_end",
    "location",
    "p_code",
    "value_in_period",
    "prp_to_date_in_location",
    "note",
)


def _report_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "programme_document": r["pd_reference_number"],
        "pd_status": r.get("pd_status_label", ""),
        "partner": r["partner_name"],
        "section": r["section"],
        "document_type": r.get("document_type", ""),
        "report": r["report_number"],
        "report_type": r["report_type"],
        "report_status": r["status_label"],
        "due_date": r["due_date"],
        "period_start": r["period_start"],
        "period_end": r["period_end"],
        "submission_date": r["submission_date"],
        "indicator_reports": r["indicators"],
        "unicef_focal_points_count": len(r.get("focal_points", [])),
        "source": owed.source_words(r),
    }


def _location_row(r: dict[str, Any]) -> dict[str, Any]:
    note = ""
    if r["repeated"]:
        note = "The report total is repeated on every location: no value of its own"
    elif r["rows"] > 1:
        note = f"{r['rows']} rows for this location: the largest value is used, never their sum"
    return {
        **{k: r.get(k) for k in LOCATION_COLUMNS},
        "prp_to_date_in_location": r["prp_to_date"],
        "note": note,
    }


DISAGGREGATION_BLOCK_COLUMNS = (
    "programme_document",
    "pd_status",
    "partner",
    "section",
    "cp_output",
    "indicator",
    "emergency",
    "report",
    "report_type",
    "report_status",
    "due_date",
    "period_start",
    "period_end",
    "location",
    "p_code",
)
DISAGGREGATION_COLUMNS = (
    *DISAGGREGATION_BLOCK_COLUMNS,
    "line",
    "disaggregation",
    "value",
    "value_in_period",
    "subtotal_of_the_disaggregation",
    "location_total",
    "last_modified",
    "note",
)
LINE_NAMES = {"value": "Value", "subtotal": "Subtotal", "total": "Location total"}


def _disaggregation_note(block: dict[str, Any], line: dict[str, Any]) -> str:
    notes = []
    if line["line"] == "subtotal":
        if line.get("hidden"):
            notes.append(
                f"The subtotal of every value of the disaggregation; {line['hidden']} left out by the filter"
            )
        if line.get("matches") is False:
            notes.append("This subtotal differs from the location total")
    elif line["line"] == "total":
        if block["repeated"]:
            notes.append("The report total is repeated on every location: no value of its own")
        elif not block["total_is_prp"] and block["total"] is not None:
            notes.append("No disaggregation total: the location's value in the period")
        if block["unlabelled"]:
            n = block["unlabelled"]
            notes.append(
                "1 value without its label yet is not listed"
                if n == 1
                else f"{n} values without their labels yet are not listed"
            )
    return "; ".join(notes)


def _disaggregation_export_rows(found: dict[str, Any]):
    """One row per line of each block (``reporting.disaggregation_lines``): a value, a subtotal and a
    location total each in a column of its own, so adding up one column never mixes them."""
    for block in reporting_service.filled_blocks(found):
        modified = block["modified"]
        if isinstance(modified, datetime.datetime):
            modified = timezone.localtime(modified).date() if timezone.is_aware(modified) else modified.date()
        for line in reporting_service.disaggregation_lines(block):
            yield {
                **{k: block.get(k) for k in DISAGGREGATION_BLOCK_COLUMNS},
                "line": LINE_NAMES[line["line"]],
                "disaggregation": line["type"],
                "value": line["label"] if line["line"] == "value" else "",
                "value_in_period": line.get("value"),
                "subtotal_of_the_disaggregation": line.get("subtotal"),
                "location_total": line.get("total"),
                "last_modified": modified,
                "note": _disaggregation_note(block, line),
            }
        for key in ("groups", "untyped", "table_lines"):  # written: its lines need not stay in memory
            block.pop(key, None)


def _reporting_disaggregations(data: dict[str, Any], params) -> dict[str, Any]:
    wanted = set(data["filters"].statuses) or None
    return reporting_service.disaggregation_rows(
        data["rows"], data["statuses"], data["today"], wanted, params
    )


def _reporting_locations(data: dict[str, Any]) -> dict[str, Any]:
    # every progress report's own status, read before the status filter: a row is kept by its report's
    # status, never by a status its own fields would give
    wanted = set(data["filters"].statuses) or None
    return reporting_service.location_rows(data["rows"], data["statuses"], data["today"], wanted)


@require_GET
def partner_reporting(request: HttpRequest) -> HttpResponse:
    """Progress reports: Reporting Progress (progress reports by status, with the indicators under each,
    section and month of the reporting period's end, and the report table), Partner view (Partner Performance,
    ``tab=partners``), Report/Location (``tab=locations``) and Report/Disaggregation
    (``tab=disaggregations``), with one filter for all of them and exports of the whole filtered set. The
    tab bar offers the first two only (:data:`REPORTING_SHOWN_TABS`): the other two are hidden, and open from
    their address only.

    Report/Location and Report/Disaggregation load in parts (:func:`_reporting_table_tab`); the other two tabs
    are one request, as before (they take about a tenth of a second on a large database)."""
    tab = request.GET.get("tab") if request.GET.get("tab") in REPORTING_TABS else "reports"
    if tab in datamart.REPORTING_TABLES:
        return _reporting_table_tab(request, tab)
    # the day of every tab and of Home's figures (datamart.reporting_today), never a clock of its own
    data = datamart.partner_reporting(request.GET, datamart.reporting_today())
    export = request.GET.get("export")
    if export in exports.FORMATS:
        return _reporting_export(data, tab, export, request.GET)
    if tab == "partners":
        page_obj = _paginate(request, data["partners"]["follow_up"])
    else:
        page_obj = _paginate(request, data["reports"])
    params = request.GET.copy()
    context = {
        **_reporting_frame(request, tab, data["filters"]),
        "data": data,
        "page_obj": page_obj,
        "locations": None,
        "disaggregations": None,
        "downloads": _reporting_downloads(params, tab),
        "chart_data": {"by_month": data["by_month"]},
    }
    template = "reports/partials/reporting_table.html" if request.htmx else "reports/partner_reporting.html"
    return render(request, template, context)


def _reporting_frame(request: HttpRequest, tab: str, filters) -> dict[str, Any]:
    """What the Progress reports page draws around its tables: the title, the filter bar's choices and values,
    the tabs."""
    params = request.GET.copy()
    return {
        "page_title": _("Progress reports"),
        "page_subtitle": _(
            "Progress reports submitted by partners in the Partner Reporting Portal, from the eTools Datamart"
        ),
        "breadcrumbs": [_crumb(_("Progress reports"))],
        "f": filters,
        "tab": tab,
        "selected": {key: request.GET.getlist(key) for key in REPORTING_LISTS} | {"status": filters.statuses},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "overdue": request.GET.get("overdue") == "1",
        # Home's rows open the page on one programme document (pd=): the same address without it
        "every_pd_url": request.path + "?" + _query_with(params, ("pd", "export")) if filters.pds else "",
        "old_filters": old_filters.note(request.path, params, REPORTING_OLD_FILTERS),
        # the check line's Power BI comparison sheet and figure trail, for the filter drawn (validators only
        # see them)
        "check_compare": check_views.compare_link("progress-reports", params, datamart.reporting_today()),
        "check_trail": check_views.trail_url("progress-reports", params, tab=tab),
        "status_keys": reporting_service.STATUS_KEYS,  # the section table's columns, for their figure keys
        "tab_urls": {  # the tab bar's: never a hidden tab's address
            name: request.path + "?" + _query_with(params, ("tab", "export"), tab=name)
            for name in REPORTING_SHOWN_TABS
        },
        **_all_years(request, params, not filters.year, None),
    }


def _reporting_downloads(params: QueryDict, tab: str) -> list[dict[str, str]]:
    return [
        {"label": _("Excel of the filter"), "url": "?" + _query_with(params, (), tab=tab, export="xlsx")},
        {"label": _("CSV of the filter"), "url": "?" + _query_with(params, (), tab=tab, export="csv")},
    ]


def _reporting_table_tab(request: HttpRequest, tab: str) -> HttpResponse:
    """Report/Location (``tab=locations``) and Report/Disaggregation (``tab=disaggregations``) of the
    Progress reports page, in parts (:mod:`neurodb.web.parts`): this answers the frame at once (the filter
    bar, with its options stored for every worker and the tab's own filters, and the tabs) with one
    placeholder, ``table``: the tab's table, with the note when the filter holds too many rows, its
    downloads and its pages (each reloads the part and shows the page's own address). The table is computed
    once for every worker and kept two minutes (:func:`neurodb.datamart.services.reporting_table`): its
    pages read it, and when it is already stored the frame draws it inline (one request). The page has no
    first-load default: the part's query is the page's own, without ``export``. A change of filter (HTMX)
    answers the results area with the table itself, as before: the table takes well under a second. The
    exports are answered first, whole, as before."""
    export = request.GET.get("export")
    if export in exports.FORMATS:
        data = datamart.partner_reporting(request.GET, datamart.reporting_today())
        return _reporting_export(data, tab, export, request.GET)
    filters = reporting_service.ReportFilters.from_params(request.GET)
    params = request.GET.copy()
    query = params.copy()
    query.pop("export", None)

    def table() -> dict[str, Any]:
        found = datamart.reporting_table(tab, request.GET)
        rows = found[tab]
        if tab == "locations":
            page_obj = _paginate(request, rows["rows"])
        else:
            # the lines of the blocks shown only, never of every block of the filter, made on copies: the
            # stored blocks stay as stored (once() hands the same value to every reader of the request)
            blocks = list(rows["blocks"])
            page_obj = _paginate(request, blocks, DISAGGREGATION_BLOCKS)
            shown = [dict(block) for block in page_obj.object_list]
            list(reporting_service.filled_blocks(rows, shown))
            first = page_obj.start_index() - 1 if shown else 0
            blocks[first : first + len(shown)] = shown
            page_obj.object_list = shown
            rows = {**rows, "blocks": blocks}
        # no chart data: no chart of this tab reads the page's reporting-chart-data
        return {tab: rows, "page_obj": page_obj, "downloads": _reporting_downloads(params, tab)}

    def shell() -> dict[str, Any]:
        context = _reporting_frame(request, tab, filters)
        context["data"] = {"options": reporting_service.stored_options()}  # the filter bar's choices
        if tab == "disaggregations":  # the tab's own filters, in the filter bar
            context["tab_filters"] = reporting_service.DisaggregationFilters.from_params(request.GET)
        return context

    parts = {
        "table": Part(
            "reports/parts/reporting_table.html",
            table,
            ready=lambda: datamart.reporting_table_stored(tab, request.GET),
        )
    }
    template = "reports/partner_reporting.html"
    if getattr(request, "htmx", None) and getattr(request, "part", None) is None:
        # a change of filter (or any other HTMX request of this address): the results area with its table, as
        # before the tab loaded in parts
        request.parts_eager = True
        template = "reports/partials/reporting_table.html"
    return render_page(
        request, template, scope={"tab": tab}, shell=shell, parts=parts, query=query.urlencode()
    )


def _too_many(found: dict[str, Any]) -> HttpResponse:
    return exports.too_many(
        found["too_many"], found["limit"], _("report rows"), _("a year, a section or a partner")
    )


STATUS_SHEET_COLUMNS = ("status", "progress_reports", "indicators", "indicator_reports", "note")


def _status_sheet(counts: dict[str, Any]) -> tuple[str, tuple[str, ...], list[dict[str, Any]]]:
    """The Excel file's *By status*: the tiles. Per status, its progress reports (as Power BI's Reporting
    Progress counts them), the indicators under it (each once, of active programme documents) and the
    indicator reports in them; then *Total*, the first tile, whose indicators are counted once across the
    statuses: an indicator in reports of several statuses is in each of their rows, so the indicators column
    does not add up to it, and its note says so (the progress reports and indicator reports do)."""
    rows: list[dict[str, Any]] = [
        {
            "status": s["label"],
            "progress_reports": s["progress_reports"],
            "indicators": s["indicators"],
            "indicator_reports": s["indicator_reports"],
        }
        for s in counts["statuses"]
    ]
    rows.append(
        {
            "status": "Total",
            "progress_reports": counts["progress_reports"],
            "indicators": counts["indicators"],
            "indicator_reports": counts["indicator_reports"],
            "note": f"Each indicator once, in {counts['indicator_pd_words']} programme documents: an "
            "indicator in reports of several statuses is in each of their rows, so the indicators above do "
            "not add up to this total.",
        }
    )
    return "By status", STATUS_SHEET_COLUMNS, rows


def _reporting_export(data: dict[str, Any], tab: str, fmt: str, params=None) -> HttpResponse:
    """The tab's table, every row of the filter (not only the page shown). The Excel file adds the
    counts of the tiles: progress reports per status with the indicators under each (each once, of active
    programme documents) and the indicator reports in them, then the first tile (*Total*, with a note: the
    status rows' indicators do not add up to its own, :func:`_status_sheet`), and progress reports by
    section."""
    if tab == "disaggregations":
        found = _reporting_disaggregations(data, params or {})
        if found["too_many"]:
            return _too_many(found)
        label, title, columns = "report-disaggregations", "Report by disaggregation", DISAGGREGATION_COLUMNS
        rows: Any = _disaggregation_export_rows(found)
    elif tab == "locations":
        found = _reporting_locations(data)
        if found["too_many"]:
            return _too_many(found)
        label, title, columns = "report-locations", "Report by location", LOCATION_COLUMNS
        rows = (_location_row(r) for r in found["rows"])
    elif tab == "partners":
        label, title, columns = "partners-follow-up", "Follow up", FOLLOW_UP_COLUMNS
        rows = (_report_row(r) for r in data["partners"]["follow_up"])
    else:
        label, title, columns = "progress-reports", "Progress reports", REPORT_COLUMNS
        rows = (_report_row(r) for r in data["reports"])
    if fmt == "csv":
        return exports.stream_csv(exports.export_filename(label, "csv"), columns, rows)
    sheets: list[tuple[str, Any, Any]] = [(title, columns, rows), _status_sheet(data["counts"])]
    labels = list(reporting_service.rc.STATUS_LABELS.values())
    section_rows = [
        {"section": row["section"], **dict(zip(labels, row["counts"], strict=True)), "Total": row["total"]}
        for row in data["by_section"]["rows"]
    ]
    sheets.append(("By section", ("section", *labels, "Total"), section_rows))
    if tab == "partners":
        perf = data["partners"]
        sheets.append(
            (
                "Partners by status",
                ("status", "partners"),
                [{"status": s["label"], "partners": s["partners"]} for s in perf["by_status"]],
            )
        )
        sheets.append(
            (
                "Partners not reported",
                ("partner", "programme_documents"),
                [
                    {"partner": p["name"], "programme_documents": ", ".join(p["pds"])}
                    for p in perf["not_reported_list"]
                ],
            )
        )
    return exports.xlsx_response(exports.export_filename(label, "xlsx"), sheets, typed=True)


@require_GET
def progress_report(request: HttpRequest) -> HttpResponse:
    """Quick view: the indicators of one progress report (``?report=<its key>``, as the reports' lists link
    it, or PRP's progress report id), with each location's value parsed and a total row that follows the
    indicator's method."""
    detail = datamart.report_detail(request.GET.get("report", "")[:600])
    if detail is None:
        raise Http404
    report = detail["report"]
    title = f"{report.pd_reference_number} · {report.report_number}"
    context = {
        **detail,
        "page_title": title,
        "page_subtitle": report.partner_name,
        "breadcrumbs": [_crumb(_("Progress reports"), reverse("reports:partner_reporting")), _crumb(title)],
    }
    template = "reports/partials/progress_report.html" if request.htmx else "reports/progress_report.html"
    return render(request, template, context)


@require_GET
def monitoring(request: HttpRequest) -> HttpResponse:
    """Field monitoring: findings, TPM visits and activities and staff trips (the default tab), and the
    HACT programmatic visit plan per programme document (``tab=plan``, :func:`_monitoring_plan`). The
    default tab carries a one-line strip of the plan's figures for its year."""
    if request.GET.get("tab") == "plan":
        return _monitoring_plan(request)
    data = datamart.monitoring(request.GET)
    page_obj = _paginate(request, data["findings"])
    today = timezone.localdate()
    chosen = pd_monitoring_service.year_param(request.GET.get("year"))
    context = {
        "page_title": _("Field monitoring"),
        "page_subtitle": _monitoring_subtitle(),
        "breadcrumbs": [_crumb(_("Field monitoring"))],
        "data": data,
        "page_obj": page_obj,
        "selected": {"rating": request.GET.getlist("rating")},
        "year": request.GET.get("year", ""),
        "q": request.GET.get("q", ""),
        "chart_data": {"by_rating": data["by_rating"], "by_month": data["by_month"]},
        "tab": "findings",
        "tab_urls": _monitoring_tabs(chosen),
        # the findings filter's year, else this year: the strip follows the year filter
        "hact_strip": _plan_strip(chosen or today.year, today),
    }
    template = "reports/partials/monitoring_table.html" if request.htmx else "reports/monitoring.html"
    return render(request, template, context)


def _monitoring_subtitle() -> str:
    return _(
        "Field monitoring findings, third-party monitoring visits and the HACT programmatic visit plan, "
        "from the eTools Datamart"
    )


def _monitoring_tabs(year: int | None) -> dict[str, str]:
    """The Field monitoring page's two tabs, plain links that keep the year: "Findings and visits" and
    "HACT visit plan"."""
    path = reverse("reports:monitoring")
    plan = {"tab": "plan", **({"year": year} if year else {})}
    return {
        "findings": f"{path}?{urlencode({'year': year})}" if year else path,
        "plan": f"{path}?{urlencode(plan)}",
    }


def _plan_strip(year: int, today: datetime.date) -> dict[str, Any] | None:
    """The HACT visit plan's figures for the default tab's strip (counts only) and the plan's link; None
    when eTools sent no plan for ``year`` (no strip). The figures are the plan's as its link opens it: the
    page's default filter (the active programme documents, since 25 Nov 2026), its headline read as the tab
    reads it (``hact_plan.headline`` of the filter, "no visit on any PD" of every PD of the year)."""
    rows = hact_plan.build(year, today)
    if not any(r["plan_row"] for r in rows):
        return None
    shown = hact_plan.select(rows, hact_plan.PlanFilters(year=year), presets=False)
    url = f"{reverse('reports:monitoring')}?{urlencode({'tab': 'plan', 'year': year})}"
    return {**hact_plan.headline(shown, year, rows), "url": url}


# The columns of the HACT visit plan's files: the team's workbook's, then the figures NeuroDB adds. The
# UNICEF focal points are a count; who wrote a comment is never written.
PLAN_COLUMNS = (
    "partner", "vendor_number", "reference_number", "status", "sections", "document_type", "start_date",
    "end_date", "total_budget", "budget_currency", "risk_rating", "required_pv", "completed_pv",
    "completed_pv_out_of_minimum", "still_needed_by_31_dec", "q1_planned", "q2_planned", "q3_planned",
    "q4_planned", "due_by_end_of_quarter", "remark", "not_visited", "completed_fm", "completed_tpm",
    "awaiting_approval", "fm_completed_not_programmatic", "fm_partner_visits_naming_no_pd",
    "staff_trip_activities_not_counted", "tpm", "unicef_focal_points_count", "section_comment",
    "section_comment_updated",
)  # fmt: skip
PLAN_PARTNER_COLUMNS = (
    "partner", "vendor_number", "risk_rating", "programme_documents", "pds_not_visited", "required_pv",
    "completed_pv", "completed_pv_out_of_minimum", "still_needed_by_31_dec", "fm_partner_visits_naming_no_pd",
    "etools_hact_required", "etools_hact_completed", "etools_hact_gap",
    "etools_minimum_met_pd_visits_still_needed", "pd_plans_below_etools_minimum",
)  # fmt: skip
PLAN_SECTION_COLUMNS = ("section", "pds", "pds_with_plan", "not_visited", "visits_still_needed")
PLAN_ABOUT_COLUMNS = ("name", "value")


def _monitoring_plan(request: HttpRequest) -> HttpResponse:
    """The "HACT visit plan" tab (``tab=plan``): every programme document of the HACT year with its
    planned and completed programmatic visits (``hact_plan``), narrowed by the filter bar. The four
    figures and the by-section panel leave the quick views out (remark, not visited); the list is by
    programme document, or by partner (``view=partners``); ``export`` gives its Excel or CSV file
    (every row of the filter). Computed live, so a sync or a comment shows at once: a few grouped
    queries, then Python over plain rows."""
    today = timezone.localdate()
    f = hact_plan.PlanFilters.from_params(request.GET, today)
    view = "partners" if request.GET.get("view") == "partners" else "pds"
    rows = hact_plan.build(f.year, today)
    base = hact_plan.select(rows, f, presets=False)
    shown = hact_plan.select(base, f)
    synced = hact_plan.synced_on()
    if request.GET.get("export") in exports.FORMATS:
        return _plan_export(f, view, rows, base, shown, synced, request.GET["export"])
    too_many = view == "pds" and len(shown) > hact_plan.MAX_ROWS
    # eTools' plan rows for the year that match no PD: in no figure, so the tab says how many
    unlinked = hact_plan.unlinked_plans(f.year)
    params = request.GET.copy()
    params["tab"] = "plan"
    # "no visit on any of their PDs" reads every PD of the year, whatever the filter
    figures = hact_plan.headline(base, f.year, rows)
    path = reverse("reports:monitoring")
    context = {
        "page_title": _("Field monitoring"),
        "page_subtitle": _monitoring_subtitle(),
        # no year here: the breadcrumb stays as it is when the filter bar changes the year
        "breadcrumbs": [_crumb(_("Field monitoring"), path), _crumb(_("HACT visit plan"))],
        "tab": "plan",
        "tab_urls": _monitoring_tabs(f.year),
        "f": f,
        "view": view,
        # the filter bar's lists are the year's: drawn again with the results when the year changed
        "filters_oob": bool(request.htmx) and _plan_year_changed(request, f.year, today),
        **_plan_row_context(f.year, today),
        "years": sorted({*hact_plan.years(today), f.year}, reverse=True),  # the year shown is always offered
        # the year's lists, and every value the list is filtered on, chosen: the bar and the list agree
        "options": hact_plan.options(rows, f),
        "figures": figures,
        "by_section": [
            # a section's name filters the list on it
            {**s, "url": "?" + _query_with(params, ("export",), section=s["section"])} if s["section"] else s
            for s in hact_plan.by_section(base, f.sections)
        ],
        "count": len(shown),
        "too_many": too_many,
        "rows": hact_plan.for_page(shown, f.year, request.user) if view == "pds" and not too_many else [],
        "partners": [
            # its PDs: the list by programme document of the same filter, on that partner
            {**p, "pds_url": "?" + _query_with(params, ("export", "view"), partner=str(p["partner_id"]))}
            for p in (hact_plan.by_partner(shown, f.year, rows) if view == "partners" else [])
        ],
        "has_pds": bool(rows),
        "has_plan": any(r["plan_row"] for r in rows),
        "unlinked": unlinked,
        "synced_on": synced,
        "quick": _plan_quick_views(params, f, figures, len(base)),
        "view_urls": {
            "pds": "?" + _query_with(params, ("export", "view")),
            "partners": "?" + _query_with(params, ("export",), view="partners"),
        },
        "downloads": [
            {"label": _("Excel of the filter"), "url": "?" + _query_with(params, (), export="xlsx")},
            {"label": _("CSV of the filter"), "url": "?" + _query_with(params, (), export="csv")},
        ],
        "assurance_url": f"{reverse('reports:assurance')}?{urlencode({'hact_year': f.year})}",
        "reset_url": f"{path}?{urlencode({'tab': 'plan', **({'view': view} if view == 'partners' else {})})}",
    }
    template = "reports/partials/monitoring_plan.html" if request.htmx else "reports/monitoring.html"
    return render(request, template, context)


def _plan_year_changed(request: HttpRequest, year: int, today: datetime.date) -> bool:
    """Whether an HTMX request of the plan shows another year than the page it came from (htmx sends the
    page's address, which the filter bar updates once each answer is in): the filter bar's lists, which
    are the year's, must then be drawn again. True when the address is not known. Only a change of year
    redraws the bar, so typing in its search box never loses the focus."""
    current = request.htmx.current_url if request.htmx else None
    if not current:
        return True
    return hact_plan.parse_year(QueryDict(urlsplit(current).query).get("year"), today) != year


def _plan_quick_views(params: QueryDict, f: hact_plan.PlanFilters, figures: dict, pds: int) -> list[dict]:
    """The plan's quick views, links that keep the rest of the filter, each with its count: the
    programme documents planned and not visited (``zero=1``), the end-of-year missing visits (the
    remarks "HACT FMM needed" and "PD ended, visits missing": the team's EoY sheet) and all of them."""
    eoy = ["needed", "ended"]
    return [
        {
            "label": _("Not visited"),
            "count": figures["not_visited"]["pds"],
            "url": "?" + _query_with(params, ("export", "remark"), zero="1"),
            "active": f.zero,
        },
        {
            "label": _("End-of-year missing visits"),
            "count": figures["still_needed"]["pds"],
            "url": "?" + _query_with(params, ("export", "zero"), remark=eoy),
            "active": not f.zero and set(f.remarks) == set(eoy),
        },
        {
            "label": _("All"),
            "count": pds,
            "url": "?" + _query_with(params, ("export", "zero", "remark")),
            "active": not f.zero and not f.remarks,
        },
    ]


def _plan_line(row: dict[str, Any], notes: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """One programme document as a line of the plan's files: its plain fields, the number of UNICEF
    focal points (never their names) and the section comment's text and day (never who wrote it)."""
    note = notes.get(row["pd_id"]) or {}
    updated = note.get("updated_at")
    return {
        "partner": row["partner"],
        "vendor_number": row["vendor_number"],
        "reference_number": row["reference"],
        "status": row["status"],
        "sections": ", ".join(row["sections"]),
        "document_type": row["document_type"],
        "start_date": row["start"],
        "end_date": row["end"],
        "total_budget": row["total_budget"],
        # USD when eTools gives no currency, as the programme document page reads it
        "budget_currency": row["currency"] or ("USD" if row["total_budget"] is not None else ""),
        "risk_rating": row["risk_rating"],
        "required_pv": row["required"],
        "completed_pv": row["completed"],
        "completed_pv_out_of_minimum": row["counted"],
        "still_needed_by_31_dec": row["still_needed"],
        **{f"q{n}_planned": row[f"q{n}"] for n in (1, 2, 3, 4)},
        "due_by_end_of_quarter": row["due_now"],
        "remark": str(hact_plan.REMARKS[row["remark"]]),
        "not_visited": "yes" if row["zero"] else "no",
        "completed_fm": row["completed_fm"],
        "completed_tpm": row["completed_tpm"],
        "awaiting_approval": row["awaiting"],
        "fm_completed_not_programmatic": row["fm_not_programmatic"],
        "fm_partner_visits_naming_no_pd": row["fm_partner_level"],
        "staff_trip_activities_not_counted": row["staff_trips"],
        "tpm": "yes" if row["tpm"] else "no",
        "unicef_focal_points_count": row["focal_points_count"],
        "section_comment": note.get("text", ""),
        "section_comment_updated": timezone.localtime(updated).date() if updated else None,
    }


def _plan_partner_line(p: dict[str, Any]) -> dict[str, Any]:
    """A partner of the plan's "By partner" view as a line of its files."""
    return {
        "partner": p["partner"],
        "vendor_number": p["vendor_number"],
        "risk_rating": p["risk_rating"],
        "programme_documents": p["pds"],
        "pds_not_visited": p["pds_not_visited"],
        "required_pv": p["required"],
        "completed_pv": p["completed"],
        "completed_pv_out_of_minimum": p["counted"],
        "still_needed_by_31_dec": p["still_needed"],
        "fm_partner_visits_naming_no_pd": p["fm_partner_level"],
        "etools_hact_required": p["etools_required"],
        "etools_hact_completed": p["etools_completed"],
        "etools_hact_gap": p["etools_gap"],
        "etools_minimum_met_pd_visits_still_needed": "yes" if p["minimum_met_pd_needed"] else "no",
        "pd_plans_below_etools_minimum": "yes" if p["plans_below_minimum"] else "no",
    }


def _plan_filter_text(f: hact_plan.PlanFilters, names: dict[int, str]) -> str:
    """The plan's filter in words, for the About sheet. ``names`` names the partners as the filter bar
    does (``hact_plan.options``: a partner the year's rows lack from its record)."""
    statuses = list(f.status_choice)
    if f.statuses is None:  # no status in the address: the page's default (Active), never every status
        statuses = [f"{', '.join(statuses)} (the page's default)"]
    parts = [
        ("Search", [f.q] if f.q else []),
        ("Sections", list(f.sections)),
        ("Partners", [names.get(pk) or f"#{pk}" for pk in f.partners]),
        ("Risk ratings", list(f.risks)),
        ("PD statuses", statuses),
        ("Document types", list(f.types)),
        ("Remarks", [str(hact_plan.REMARKS[code]) for code in f.remarks]),
    ]
    words = [f"{label}: {', '.join(values)}" for label, values in parts if values]
    if f.zero:
        words.append("Not visited only (visits planned, none completed)")
    return "; ".join(words) or "None: every programme document of the year"


PLAN_DEFINITIONS = (
    (
        "Programme documents",
        "Every programme document that is not a draft or cancelled and runs during the year, has visits "
        "planned for the year or a counted visit in it.",
    ),
    (
        "required_pv",
        "The programmatic visits eTools plans for the programme document in the year: Q1 + Q2 + Q3 + Q4.",
    ),
    (
        "completed_pv",
        "Programmatic visits to the programme document whose report UNICEF approved, each visit once per "
        "source: field monitoring visits marked programmatic, with the status completed and an end date in "
        "the year (completed_fm), and third-party monitoring visits marked programmatic, with the status "
        "unicef_approved and an activity date in the year (completed_tpm).",
    ),
    ("completed_pv_out_of_minimum", "The smaller of required_pv and completed_pv."),
    (
        "still_needed_by_31_dec",
        "required_pv less completed_pv_out_of_minimum, never negative. A surplus on one programme document "
        "never makes up for a gap on another.",
    ),
    (
        "due_by_end_of_quarter",
        "The visits planned up to the end of the current quarter (Q4 for a past year, none for a future "
        "year) less completed_pv, never negative.",
    ),
    (
        "remark",
        "No visit planned: required_pv is 0. Completed: nothing is still needed. PD ended, visits missing: "
        "visits are still needed and the programme document ended or is past its end date. HACT FMM needed: "
        "visits are still needed.",
    ),
    ("not_visited", "yes when visits are planned and none is completed."),
    (
        "Not counted",
        "awaiting_approval: reports not yet approved (field monitoring submitted, TPM tpm_reported). "
        "fm_completed_not_programmatic: field monitoring visits completed but not marked programmatic. "
        "fm_partner_visits_naming_no_pd: the partner's completed programmatic field monitoring visits whose "
        "findings name only the partner or a CP output, never one of its programme documents (the same on "
        "each of its programme documents); the Assurance page counts them for the partner. "
        "staff_trip_activities_not_counted: UNICEF staff trip activities typed programmatic, which eTools "
        "sends without a status.",
    ),
    ("tpm", "yes when a third-party monitoring visit of the year (not draft or cancelled) covers it."),
    ("risk_rating", "The partner's eTools HACT risk rating of the year, else the partner's rating."),
    (
        "unicef_focal_points_count",
        "The number of UNICEF focal points. Their names are shown on the page, never written into a file.",
    ),
    (
        "section_comment",
        "The section's comment for the year, written on the page. Who wrote it is not written into a file.",
    ),
    (
        "By partner",
        "The sums of the partner's programme documents of the filter, beside its eTools HACT minimum "
        "(pv_required and pv_completed of its HACT year), a partner-level requirement eTools sets from the "
        "risk rating and the cash transfers. pd_plans_below_etools_minimum adds the plans of all the "
        "partner's programme documents of the year, whatever the filter. fm_partner_visits_naming_no_pd "
        "is the partner's own figure (see Not counted).",
    ),
    (
        "By section",
        "The figures without the quick views. A programme document with two sections counts under each; "
        "the total line counts it once. Under a section filter, only the chosen sections have a line.",
    ),
)  # fmt: skip


def _plan_export(
    f: hact_plan.PlanFilters,
    view: str,
    rows: list[dict],
    base: list[dict],
    shown: list[dict],
    synced: datetime.date | None,
    fmt: str,
) -> HttpResponse:
    """The plan's file, every programme document of the filter: Excel (the plan, by partner, by section
    and About) or CSV (the table of the view). ``rows`` is every programme document of the year (the
    partner flags read them all), ``base`` the filter without its quick views and ``shown`` with them.
    Plain rows only: no staff name, e-mail or comment author."""
    year = f.year
    partners = [_plan_partner_line(p) for p in hact_plan.by_partner(shown, year, rows)]
    if fmt == "csv" and view == "partners":
        name = exports.export_filename(f"hact-visit-plan-by-partner-{year}", "csv")
        return exports.stream_csv(name, PLAN_PARTNER_COLUMNS, partners)
    notes = hact_plan.comments(year, [r["pd_id"] for r in shown])
    lines = [_plan_line(r, notes) for r in shown]
    name = exports.export_filename(f"hact-visit-plan-{year}", fmt)
    if fmt == "csv":
        return exports.stream_csv(name, PLAN_COLUMNS, lines)
    sections = [
        {
            "section": "Total (each programme document once)" if s["total"] else s["section"] or "No section",
            "pds": s["pds"],
            "pds_with_plan": s["pds_with_plan"],
            "not_visited": s["not_visited"],
            "visits_still_needed": s["still_needed"],
        }
        for s in hact_plan.by_section(base, f.sections)
    ]
    unlinked = hact_plan.unlinked_plans(year)
    unlinked_rows = f"{unlinked['rows']} planned-visit row" + ("" if unlinked["rows"] == 1 else "s")
    unlinked_visits = f"{unlinked['visits']} visit" + ("" if unlinked["visits"] == 1 else "s")
    about = [
        ("HACT visit plan", "Programmatic visits per programme document against their plan, from NeuroDB"),
        ("Year", year),
        ("Exported on", timezone.localtime().replace(tzinfo=None)),
        ("eTools figures as of the sync of", synced or "no eTools Datamart sync yet"),
        # the partners named as the filter bar names them
        ("Filter", _plan_filter_text(f, dict(hact_plan.options(rows, f)["partners"]))),
        # eTools' plan rows for the year that match no PD, as the tab says
        *(
            [
                (
                    "Planned visits matching no programme document",
                    f"eTools sent {unlinked_rows} for {year} ({unlinked_visits}) matching no programme "
                    "document: left out of every figure (Data health shows the rows not linked).",
                )
            ]
            if unlinked["rows"]
            else []
        ),
        *PLAN_DEFINITIONS,
    ]
    sheets = [
        ("HACT visit plan", PLAN_COLUMNS, lines),
        ("By partner", PLAN_PARTNER_COLUMNS, partners),
        ("By section", PLAN_SECTION_COLUMNS, sections),
        ("About", PLAN_ABOUT_COLUMNS, [{"name": k, "value": v} for k, v in about]),
    ]
    return exports.xlsx_response(name, sheets, typed=True)


def _plan_row_context(year: int, today: datetime.date) -> dict[str, Any]:
    """What a row of the HACT visit plan (``reports/partials/_plan_row.html``) reads besides ``row`` (a
    ``hact_plan.for_page`` row): the year, today, the quarter of "Due by the end of Q<n>", whether
    Monitoring insights is on and the Completed link without its PD (``fmm_completed_url``), the
    Findings and visits tab the PD's TPM activities are searched on (``findings_url``: Monitoring
    insights holds no TPM visit), and the remarks' labels."""
    fmm_on = _fmm_on()
    completed_url = ""
    if fmm_on:
        from neurodb.fmm.scope import link

        # the reported programmatic visits of the year in Monitoring insights; the row adds its pd=
        completed_url = link(programmatic=1, status="reported", year=year)
    return {
        "year": year,
        "today": today,
        "due_quarter": hact_plan.due_quarter(year, today),
        "fmm_on": fmm_on,
        "fmm_completed_url": completed_url,
        "findings_url": reverse("reports:monitoring"),
        "remarks": hact_plan.REMARKS,
    }


@require_http_methods(["GET", "POST"])
def visit_plan_comment(request: HttpRequest, pk: int) -> HttpResponse:
    """A section's comment on one programme document's HACT visit plan for a year (``year``, this year by
    default): the form (GET; in the shared modal for HTMX) and its saving (POST). Administrators and
    Section editors of one of the PD's sections (``hact_plan.can_comment``), on a PD the plan lists for
    that year (404 otherwise). An empty text, or ``remove``, removes it. HTMX gets the PD's row back (it
    swaps in place); a request without JavaScript goes back to the PD's row on the plan."""
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    if not hact_plan.can_comment(request.user, pd.section_names):
        raise PermissionDenied
    today = timezone.localdate()
    year = hact_plan.parse_year(request.POST.get("year") or request.GET.get("year"), today)
    # only a programme document the plan lists for that year: a comment anywhere else would show nowhere
    # (a draft or cancelled PD, or one of another year)
    rows = hact_plan.build(year, today, pd_ids=[pd.pk])
    if not rows:
        raise Http404
    # back to a plan that lists the PD: the plan opens on the active PDs (statuses.PAGE_DEFAULT), so for any
    # other PD the link writes every status out (``status=``)
    back_query = {"tab": "plan", "year": year}
    if (pd.status or "").lower() not in pd_statuses.PAGE_DEFAULT:
        back_query["status"] = ""
    back = f"{reverse('reports:monitoring')}?{urlencode(back_query)}#pd-{pd.pk}"
    if request.method == "POST":
        text = "" if request.POST.get("remove") else request.POST.get("text", "")
        comment = hact_plan.set_comment(pd, year, text, request.user)
        if request.htmx:
            # the row as built above, with the comment just written
            context = {
                "row": hact_plan.for_page(rows, year, request.user)[0],
                **_plan_row_context(year, today),
            }
            return render(request, "reports/partials/_plan_row.html", context)
        messages.success(request, _("Section comment saved.") if comment else _("Section comment removed."))
        return redirect(back)
    context = {
        "pd": pd,
        "year": year,
        "comment": hact_plan.comments(year, [pd.pk]).get(pd.pk),
        "action": reverse("reports:visit_plan_comment", args=[pd.pk]),
        "back": back,
        "max_length": hact_plan.COMMENT_MAX,
        "modal": bool(request.htmx),
    }
    if request.htmx:
        return render(request, "reports/partials/plan_comment_form.html", context)
    context["page_title"] = _("Section comment")
    context["breadcrumbs"] = [
        _crumb(_("Field monitoring"), reverse("reports:monitoring")),
        _crumb(_("HACT visit plan %(year)s") % {"year": year}, back),
        _crumb(pd.number or pd.title),
    ]
    return render(request, "reports/plan_comment.html", context)


# ------------------------------------------------------------------------- partner monitoring (eTools)


# The tabs of Partner monitoring's tab bar (``view=``), in its order, before the Map (a page of its own):
# Data quality, the first and the default, which holds the grid below its own lists (the Grid tab merged into
# it, by the owner's decision of 21 Nov 2026), and Program Performance
MONITORING_SHOWN_VIEWS = ("quality", "table")
MONITORING_DEFAULT_VIEW = "quality"
# views since removed: an old address (a bookmark, a download link) opens the bare page, the default view on
# the user's own section. The view of NeuroDB's own grouping of PD indicators was removed, by the owner's
# decision: eTools has no such grouping. ``view=grid``, the bare address and any other word open Data quality
# (:func:`_monitoring_view`)
REMOVED_VIEWS = ("masters",)
MONITORING_RESULTS = "monitoring-grid"  # the area a filter, sort or section change of the page reloads
MONITORING_KEYS = ("section", "partner", "pd", "location", *pd_monitoring_service.TAG_FIELDS)
MONITORING_LISTS = (*MONITORING_KEYS, "pd_status", "document_type")
# the filters of an old address the page still applies (the owner removed them from its filter bar): named on
# a line with a link that removes them, as the figure trail and the Power BI comparison sheet read the same
# address
MONITORING_OLD_FILTERS = ("cp_output", "partner_type", "office", "emergency")
# the columns sorted lowest first on the first click (the figures start with the highest)
ASCENDING_FIRST = ("section", "cp_output", "indicator", "partner", "pd")


def _all_years(request: HttpRequest, params: QueryDict, active: bool, value: str | None) -> dict[str, Any]:
    """The year menu's "All years" entry for a page that can show every year: its link (``year`` set
    to ``value``, or dropped when None) and whether the page shows every year now (the menu then says
    so instead of the current year)."""
    return {
        "year_all_url": request.path + "?" + _query_with(params, ("export",), year=value),
        "year_all_active": active,
    }


def _query_with(params: QueryDict, drop: tuple[str, ...] = (), **changes: Any) -> str:
    """``params`` as a query string without ``drop`` (and the page), with ``changes`` (None removes)."""
    out = params.copy()
    for key in (*drop, "page"):
        out.pop(key, None)
    for key, value in changes.items():
        out.pop(key, None)
        if isinstance(value, list):
            out.setlist(key, value)
        elif value is not None:
            out[key] = value
    return out.urlencode()


def _monitoring_view(request: HttpRequest) -> str:
    """The view an address opens: one of :data:`MONITORING_SHOWN_VIEWS`, else Data quality (the bare address,
    and ``view=grid`` of the old Grid tab, merged into it)."""
    view = request.GET.get("view")
    return view if view in MONITORING_SHOWN_VIEWS else MONITORING_DEFAULT_VIEW


@dataclasses.dataclass(frozen=True)
class MonitoringFilter:
    """Partner monitoring's filter as a request gives it (:func:`monitoring_filters`)."""

    view: str
    filters: pd_monitoring_service.Filters
    # the page's query without ``as_of``: the user's own section written out on a first load
    params: QueryDict
    # every part's: ``params`` with every section written as ``section=`` and the year always
    resolved: QueryDict
    own_section: list[str]


def monitoring_filters(request: HttpRequest) -> MonitoringFilter:
    """Partner monitoring's view and filter from a request, as the page and its figure trail read them: Data
    quality opens on this year, one year for its lists and the grid below them (the grid's months), as the
    page's first view opened before the Grid tab was merged into it; Program Performance on every year, as
    Power BI opens it (``monitoring.opens_on_this_year``). A first load (no choice made yet) opens on the
    user's own section. The resolved filter is the one of every part request: the page's own query, every
    section written as ``section=`` and the year always (this year by default, or every year); ``as_of`` is
    added to the placeholders' addresses only (_monitoring_results.html), so no link of a part carries it."""
    view = _monitoring_view(request)
    filters = pd_monitoring_service.Filters.from_params(
        request.GET, default_year=pd_monitoring_service.opens_on_this_year(view)
    )
    own_section: list[str] = []
    params = request.GET.copy()
    params.pop("as_of", None)  # the moment a part reads, never a filter of the links
    if first_load(request):  # first load, no choice made yet: the user's own section
        options = pd_monitoring_service.stored_options(filters)
        own_section = pd_monitoring_service.default_sections(request.user, options["sections"])
        if own_section:
            filters = dataclasses.replace(filters, sections=own_section)
            params.setlist("section", own_section)
    resolved = params.copy()
    resolved.pop("export", None)
    if "section" not in resolved:
        resolved.setlist("section", [""])
    if "year" not in resolved:
        resolved["year"] = filters.year_param
    return MonitoringFilter(view, filters, params, resolved, own_section)


@require_GET
def pd_monitoring(request: HttpRequest) -> HttpResponse:
    """Partner monitoring: Data quality (``view=quality``, the default: its counts and lists, then the grid
    below them, the indicators by section, PD and output with their months: the Grid tab merged into it, so
    ``view=grid`` and the bare address open it) and Program Performance (``view=table``: one sortable row per
    indicator, exported whole). One filter for both, so the same filter gives the same figures. The address of
    a removed view (:data:`REMOVED_VIEWS`) opens the bare page.

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (the view tabs,
    the filter bar with its options, which the eTools jobs store: ``monitoring.stored_options``, never the PD
    indicator rows; the "your section" note) with a placeholder
    for each data block of the view, in ``#monitoring-grid``, each asked for at this address plus ``_part``,
    with the resolved filter (the sections written out, ``section=`` for every section, and the year always)
    and ``as_of``, the moment of the eTools figures the frame was drawn with:

    - Data quality: ``quality-tiles`` (the counts) and ``quality-lists`` (the lists to follow up), then the
      grid's ``summary`` (the tiles, the status donut and the sections chart) and ``grid`` (the page of
      indicators, its pages, the note when the list is cut): one filter and one year, one computation;
    - Program Performance: ``sections`` (the section tiles: every section the other filters keep, read from
      the filter without its sections) and ``table`` (the page of rows in its sort, with its pages and
      downloads: the same computation, the chosen sections kept, as before the page loaded in parts).

    Every part reads the indicators of its filter computed once for every worker
    (:func:`neurodb.datamart.monitoring.stored_indicators`); the parts of a view read the computation of the
    frame's moment (``as_of``) even when a sync finishes between them, and when it is already stored the
    frame draws them inline, so a warm page is one request. A change of filter, sort or section tile (an
    HTMX request aimed at ``#monitoring-grid``) answers that area with its placeholders (the results shell);
    the pages of the grid and of Program Performance reload their own part. Any other HTMX request of this
    address answers the area with its figures, as before the page loaded in parts. The exports are answered
    first, whole.
    """
    if request.GET.get("view") in REMOVED_VIEWS:
        return redirect("reports:pd_monitoring")
    view = _monitoring_view(request)
    found = monitoring_filters(request)
    filters, params, resolved, own_section = found.filters, found.params, found.resolved, found.own_section
    options = functools.cache(lambda: pd_monitoring_service.stored_options(filters))  # the frame's
    export = request.GET.get("export")
    if export in exports.FORMATS:
        return _monitoring_export(request, filters, export)
    today = pd_monitoring_service.today()
    is_part = getattr(request, "part", None) is not None
    # The eTools figures the frame is drawn with (their fingerprint: etools_keys.fingerprint): a part request
    # reads them from its address, so the parts of one view read one computation even when a sync finished
    # before or between them, while it is stored (compute.once_as_of). Any other request reads them now.
    as_of = request.GET.get("as_of", "") if is_part else etools_keys.fingerprint()
    # the links the parts draw (section tiles, sorts, downloads) start from the resolved filter, as the links
    # a part builds from its request do: a part drawn inline, on the page drawn at once or on its own draws
    # the same links
    limit = pd_monitoring_service.MAX_INDICATORS
    every_section = dataclasses.replace(filters, sections=[])

    # every indicator of the page's filter, and of the filter without its sections (the section tiles), as
    # stored for every worker
    rows = functools.cache(lambda: pd_monitoring_service.stored_indicators(filters, today, as_of=as_of))
    every_section_rows = functools.cache(
        lambda: pd_monitoring_service.stored_indicators(every_section, today, as_of=as_of)
    )

    def table_rows() -> list[pd_monitoring_service.Row]:
        """Program Performance's rows (``monitoring.program_performance_rows``, as the comparison sheet reads
        them): the section tiles' computation with the chosen sections kept, so the view's two parts read one
        computation, never two side by side."""
        return pd_monitoring_service.program_performance_rows(filters, today, every=every_section_rows())

    def stored(of: pd_monitoring_service.Filters = filters) -> Callable[[], bool]:
        return lambda: pd_monitoring_service.indicators_stored(of, today)

    def summary() -> dict[str, Any]:
        data = pd_monitoring_service.summary(rows(), filters, today)
        return {
            "data": data,
            # the labels: the status donut names its slices by them (charts.js), as on the overview and brief
            "chart_data": {
                "status_counts": data["status_counts"],
                "by_section": data["by_section"],
                "labels": pd_monitoring_service.LABELS,
            },
        }

    def grid() -> dict[str, Any]:
        every = rows()
        shown = every[:limit]
        page_obj = _paginate(request, shown, pd_monitoring_service.PAGE_SIZE)
        return {
            "rows": shown,
            "page_obj": page_obj,
            "groups": pd_monitoring_service.grouped(list(page_obj.object_list)),
            "months": pd_monitoring_service.MONTHS,
            # "All years": one column per year with a counted value, instead of the months
            "year_columns": pd_monitoring_service.year_columns(shown) if filters.all_years else [],
            "truncated": len(every) > limit,
        }

    def sections() -> dict[str, Any]:
        # every section the other filters keep, the chosen ones or not (the filter without its sections)
        counts = pd_monitoring_service.section_counts(every_section_rows())
        return {
            "section_tiles": [
                {
                    "name": name,
                    "count": count,
                    "url": "?" + _query_with(resolved, ("section",), section=name),
                    "current": name in filters.sections,
                }
                for name, count in sorted(counts.items())
            ],
            "all_sections_url": "?" + _query_with(resolved, ("section",), section=""),
        }

    def table() -> dict[str, Any]:
        every = table_rows()
        ordered, sort = pd_monitoring_service.sorted_rows(every, request.GET.get("sort", ""), filters.year)
        shown = ordered[:limit]
        current = sort.lstrip("-")
        return {
            "rows": shown,
            "page_obj": _paginate(request, shown, pd_monitoring_service.PAGE_SIZE),
            "truncated": len(every) > limit,
            "sort": sort,
            "sort_key": current,
            "sort_desc": sort.startswith("-"),
            "table_headings": _table_headings(filters),
            "sort_urls": {
                key: "?"
                + _query_with(
                    resolved,
                    ("export",),
                    sort=(
                        ("" if sort.startswith("-") else "-") + key
                        if key == current
                        else (key if key in ASCENDING_FIRST else f"-{key}")
                    ),
                )
                for key in pd_monitoring_service.TABLE_SORTS
            },
            "downloads": [
                {
                    "label": _("Excel of the filter"),
                    "url": "?" + _query_with(resolved, ("page",), view="table", export="xlsx"),
                },
                {
                    "label": _("CSV of the filter"),
                    "url": "?" + _query_with(resolved, ("page",), view="table", export="csv"),
                },
            ],
        }

    @functools.cache
    def quality() -> dict[str, Any]:
        # Data quality lists every indicator of the filter: never cut (the other views say when they are)
        return {"quality": pd_monitoring_service.data_quality(rows(), filters, today), "truncated": False}

    if view == "table":
        parts = {
            "sections": Part("reports/parts/pm_sections.html", sections, ready=stored(every_section)),
            "table": Part("reports/parts/pm_table.html", table, ready=stored(every_section)),
        }
    else:  # Data quality, then the grid below its lists: the four read the indicators of one filter
        parts = {
            "quality-tiles": Part("reports/parts/pm_quality_tiles.html", quality, ready=stored()),
            "quality-lists": Part("reports/parts/pm_quality_lists.html", quality, ready=stored()),
            "summary": Part("reports/parts/pm_summary.html", summary, ready=stored()),
            "grid": Part("reports/parts/pm_grid.html", grid, ready=stored()),
        }
    scope = {  # what the parts read besides their own figures
        "filters": filters,
        "labels": pd_monitoring_service.LABELS,
        "report_types": pd_monitoring_service.REPORT_TYPES,
    }

    def shell() -> dict[str, Any]:
        return {
            "page_title": _("Partner monitoring"),
            "page_subtitle": _(
                "What partners reported on each programme document indicator, by reporting period and "
                "location, against the target in the PD"
            ),
            "breadcrumbs": [_crumb(_("Partner monitoring"))],
            "options": options(),
            "selected": {key: request.GET.getlist(key) for key in MONITORING_LISTS}
            | ({"section": own_section} if own_section else {}),
            "own_section": own_section,
            "view": view,
            "as_of": as_of,
            "old_filters": old_filters.note(request.path, params, MONITORING_OLD_FILTERS),
            # the check line's Power BI comparison sheet and figure trail, for the resolved filter
            "check_compare": check_views.compare_link("partner-monitoring", resolved, today),
            "check_trail": check_views.trail_url("partner-monitoring", resolved, view=view),
            # the tab bar's: the tabs keep the year shown (Data quality opens on this year, Program
            # Performance on every year: a tab never changes the year silently)
            "tab_urls": {
                name: request.path
                + "?"
                + _query_with(params, ("view", "sort", "export"), view=name, year=filters.year_param)
                for name in MONITORING_SHOWN_VIEWS
            }
            | {
                "map": reverse("reports:pd_monitoring_map")
                + "?"
                + _query_with(params, ("view", "sort", "export"), year=filters.year_param)
            },
            **_all_years(request, params, filters.year is None, pd_monitoring_service.ALL_YEARS),
        }

    template = "reports/pd_monitoring.html"
    htmx = getattr(request, "htmx", None)
    if htmx and not is_part and (htmx.target or "") != MONITORING_RESULTS:
        # any other HTMX request of this address (none of the page's own): the results area with its figures,
        # as before the page loaded in parts
        request.parts_eager = True
        template = "reports/partials/_monitoring_results.html"
    return render_page(
        request,
        template,
        scope=scope,
        shell=shell,
        parts=parts,
        query=resolved.urlencode(),
        results_template="reports/partials/_monitoring_results.html",
        results_target=MONITORING_RESULTS,
    )


def _table_headings(filters) -> list[tuple[str, str, bool]]:
    """Program Performance's columns: (sort key, heading, numeric)."""
    return [
        ("section", _("Section"), False),
        ("cp_output", _("CP output"), False),
        ("emergency", _("Emergency"), False),
        ("indicator", _("Indicator"), False),
        ("target", _("Target"), True),
        # a Reporting year scopes Reached and % Reached, as in Power BI; Reached to date stays beside them
        *(
            [
                ("reached_in_year", _("Reached in %(year)s") % {"year": filters.year}, True),
                ("achieved_in_year", _("%% Reached in %(year)s") % {"year": filters.year}, True),
                ("reached", _("Reached to date"), True),
            ]
            if filters.year
            else [("reached", _("Reached"), True), ("achieved", _("% Reached"), True)]
        ),
        ("partner", _("Partner"), False),
        ("pd", _("Programme document"), False),
    ]


def _monitoring_export(request: HttpRequest, filters, fmt: str) -> HttpResponse:
    """Program Performance, every indicator of the filter (not only the page shown, and not cut at the
    page's 2,000 rows), in its sort."""
    rows = pd_monitoring_service.indicators(filters)
    rows, _sort = pd_monitoring_service.sorted_rows(rows, request.GET.get("sort", ""), filters.year)
    columns = pd_monitoring_service.TABLE_COLUMNS
    if not filters.year:
        columns = tuple(c for c in columns if c not in ("reached_in_year", "achieved_percent_in_year"))
    out = pd_monitoring_service.table_rows(rows, filters, request.build_absolute_uri("/").rstrip("/"))
    label = "program-performance" + (f"-{filters.year}" if filters.year else "")
    if fmt == "xlsx":
        return exports.xlsx_response(
            exports.export_filename(label, "xlsx"), [("Program Performance", columns, out)], typed=True
        )
    return exports.stream_csv(exports.export_filename(label, "csv"), columns, out)


@require_GET
def pd_monitoring_map(request: HttpRequest) -> HttpResponse:
    """The partner monitoring map: where the filtered PD indicators are implemented and reported. A frame
    already: its figures come from the map's API (:class:`neurodb.reports.api.PDMapAPI`), which the page asks
    for once drawn (static/js/pdmap.js); its filter options are the page's, which the eTools jobs store."""
    filters = pd_monitoring_service.Filters.from_params(request.GET)
    options = pd_monitoring_service.stored_options(filters)
    own_section: list[str] = []
    if not request.GET:
        own_section = pd_monitoring_service.default_sections(request.user, options["sections"])
        if own_section:
            filters = dataclasses.replace(filters, sections=own_section)
    params = request.GET.copy()
    if own_section:
        params.setlist("section", own_section)
    context = {
        "page_title": _("Partner monitoring map"),
        "page_subtitle": _(
            "Where the programme documents are implemented: for each location the partner, the PD, "
            "the indicators reported there, the target, what was achieved and whether it is on track"
        ),
        "breadcrumbs": [
            _crumb(_("Partner monitoring"), reverse("reports:pd_monitoring")),
            _crumb(_("Map")),
        ],
        "filters": filters,
        "labels": pd_monitoring_service.LABELS,
        "levels": pd_monitoring_service.LEVEL_LABELS,
        "options": options,
        "selected": {key: request.GET.getlist(key) for key in MONITORING_LISTS}
        | ({"section": own_section} if own_section else {}),
        "own_section": own_section,
        "view": "map",
        "old_filters": old_filters.note(request.path, params, MONITORING_OLD_FILTERS),
        "check_compare": check_views.compare_link(
            "partner-monitoring",
            QueryDict(_query_with(params, ("view", "sort"), year=filters.year_param)),
            pd_monitoring_service.today(),
        ),
        "check_trail": check_views.trail_url(
            "partner-monitoring",
            QueryDict(_query_with(params, ("view", "sort"), year=filters.year_param)),
            view="map",
        ),
        "tab_urls": {
            name: reverse("reports:pd_monitoring")
            + "?"
            + _query_with(params, ("view", "sort"), view=name, year=filters.year_param)
            for name in MONITORING_SHOWN_VIEWS
        }
        | {"map": request.path + "?" + _query_with(params, ("view", "sort"), year=filters.year_param)},
        **_all_years(request, params, filters.year is None, pd_monitoring_service.ALL_YEARS),
        "map_config": {
            "api": reverse("api:pd_map") + ("?" + params.urlencode() if params else ""),
            "labels": pd_monitoring_service.LABELS,
            "levels": pd_monitoring_service.LEVEL_LABELS,
            "partnerUrl": reverse("reports:partner_profile", args=[0]),
            "gridUrl": reverse("reports:pd_monitoring"),
        },
    }
    return render(request, "reports/pd_monitoring_map.html", context)


@require_GET
def pd_indicator(request: HttpRequest, pk: int, key: str) -> HttpResponse:
    pd = get_object_or_404(PCA.objects.select_related("partner"), pk=pk)
    raw_year = (request.GET.get("year") or "").strip().lower()
    year = (
        None
        if raw_year == pd_monitoring_service.ALL_YEARS
        else pd_monitoring_service.year_param(raw_year) or datetime.date.today().year
    )
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
        "labels": pd_monitoring_service.LABELS,
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
    year = pd_monitoring_service.year_param(raw_year) or (years[0] if years else None)
    view = request.GET.get("view", "total")
    if view not in services.POPULATION_VIEWS:
        return HttpResponse(_("Invalid population view."), status=400)
    data = population_service.population_view(year, view) if year else None
    nationality_labels = dict(PopulationFigure.Nationality.choices)
    # the nationality codes heading the tables' columns, explained once above them
    used = {
        code
        for key in ("by_governorate", "by_district", "by_age_group", "by_vulnerability")
        for code in ((data or {}).get(key) or {}).get("columns", [])
    }
    column_codes = [(code, label) for code, label in nationality_labels.items() if code in used]
    context = {
        "page_title": _("Population figures"),
        "nationality_labels": nationality_labels,
        "column_codes": column_codes,
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
            # No loader writes vulnerable rows (only the admin form does): hide the tab until one exists.
            # Old ?view=vulnerable links still answer, with the tab selected and the empty state.
            *(
                [("vulnerable", _("Vulnerable population"))]
                if view == "vulnerable" or PopulationFigure.objects.filter(category="vulnerable").exists()
                else []
            ),
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
    """When each source was last synchronised, and what failed. Open to every signed-in member of staff, as
    before (donors are kept out by their own middleware).

    The page loads in parts (:mod:`neurodb.web.parts`): this view answers the frame at once (the page
    heading, the *Sync jobs* heading with its staleness note, the datasets panel's heading and explanation)
    with a placeholder per block, each asked for at this address plus ``_part``, each its own computation:

    - ``jobs``: one card per sync job, fresh, stale or never succeeded (:func:`services.health_jobs`), the
      ActivityInfo archive's jobs left out (:data:`services.ARCHIVE_JOBS`: the administration's Scheduled jobs
      and runs list them);
    - ``runs``: the latest 50 runs of every job but those;
    - ``datasets``: the table of the eTools Datamart datasets (:func:`services.datamart_quality`), whose row
      counts are stored for every worker (an hour at most, keyed on the jobs that write those tables); its
      panel's heading and explanation are the frame's;
    - ``readiness``: the last machine learning readiness check;
    - ``power-bi``: *Compared with Power BI*, how Compiler counted the Makani, Dirasa and youth figures
      against the views Power BI reads (:func:`neurodb.education.power_bi.health`, counts only; the frame
      holds the panel's heading, its explanation and, for administrators, the links to the card versions
      and the view definitions).

    The page has no filter: the parts' query is empty.
    """

    def shell() -> dict[str, Any]:
        from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

        return {
            "page_title": _("Data health"),
            "page_subtitle": _("Sync runs and freshness of every source"),
            "breadcrumbs": [_crumb(_("Data health"))],
            "staleness_hours": settings.SYNC_STALENESS_HOURS,
            # the jobs' placeholder draws one grey card per job card to come (no query)
            "job_count": sum(
                1
                for job in SyncRun.Job
                if job not in services.HEALTH_SKIPPED_JOBS and job not in services.ARCHIVE_JOBS
            ),
            # the datasets panel's heading counts the datasets its table lists, one per Datamart dataset
            "dataset_count": len(ENTITY_SYNCS),
        }

    parts = {
        "jobs": Part(
            "reports/parts/health_jobs.html", lambda: {"jobs": services.health_jobs(with_archive=False)}
        ),
        "runs": Part(
            "reports/parts/health_runs.html", lambda: {"runs": services.recent_runs(with_archive=False)}
        ),
        "datasets": Part(
            "reports/parts/health_datasets.html", lambda: {"datasets": services.datamart_quality()}
        ),
        "readiness": Part(
            "reports/parts/health_readiness.html",
            _readiness,
        ),
        "power-bi": Part("reports/parts/health_power_bi.html", lambda: {"programmes": _power_bi_health()}),
    }
    return render_page(request, "reports/data_health.html", scope={}, shell=shell, parts=parts, query="")


def _readiness() -> dict[str, Any]:
    """The last machine learning readiness check, as Data health shows it (without the ActivityInfo archive:
    :func:`neurodb.insights.readiness.shown`), its verdicts split into the decisions a model could support and
    the analyses for later, and what changed since the check before it (``since``:
    :func:`neurodb.insights.readiness.since`). The two latest checks come in one query, as
    ``SyncRun.last_success`` reads the latest."""
    from neurodb.insights import readiness

    runs = list(
        SyncRun.objects.filter(
            job=SyncRun.Job.ML_READINESS, status__in=[SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL]
        ).order_by("-finished_at")[:2]
    )
    checks = [readiness.shown((run.details or {}).get("readiness")) for run in runs]
    result = checks[0] if checks else None
    found = (result or {}).get("verdicts") or []
    return {
        "readiness": runs[0] if runs else None,
        "result": result,
        "decisions": [v for v in found if v.get("group", "decision") == "decision"],
        "later": [v for v in found if v.get("group") == "later"],
        "since": readiness.since(checks[1], result) if len(checks) > 1 else None,
        "schedule": _readiness_schedule(),
    }


READINESS_MONTHLY = "45 6 1 * *"  # core 0034's schedule


def _readiness_schedule() -> dict[str, Any]:
    """When the readiness check runs again, from its switched-on scheduled jobs (as Home reads the daily
    review's): ``on`` (none switched on: False), ``monthly`` (one runs on core 0034's monthly schedule), the
    first one's schedule in words (``described``) and its next run's day (``next``, in Beirut; None when the
    schedule cannot be read)."""
    from neurodb.core.cron import describe, next_after
    from neurodb.core.models import ScheduledJob

    jobs = list(
        ScheduledJob.objects.filter(command="ml_readiness", enabled=True)
        .order_by("next_run_at", "key")
        .only("schedule", "next_run_at")
    )
    if not jobs:
        return {"on": False, "monthly": False, "described": "", "next": None}
    job = next((j for j in jobs if j.schedule.strip() == READINESS_MONTHLY), jobs[0])
    when = job.next_run_at
    if when is None:
        try:
            when = next_after(job.schedule, timezone.now())
        except (ValueError, TypeError):
            when = None
    described = describe(job.schedule)
    return {
        "on": True,
        "monthly": job.schedule.strip() == READINESS_MONTHLY,
        "described": _("on its own schedule") if described == job.schedule else described,
        "next": timezone.localtime(when).date() if when else None,
    }


def _power_bi_health() -> list[dict[str, Any]]:
    from neurodb.education import power_bi

    return power_bi.health()


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
    """Pages, partners, programme documents, country programme indicators, knowledge base documents and the
    archive of every year (:func:`services.search`, its groups labelled as the archive); ``?year=`` (a
    reporting year's name) narrows the archive's groups to that year."""
    from neurodb.indicators.models import ReportingYear

    q = request.GET.get("q", "").strip()
    raw_year = request.GET.get("year", "").strip()
    year = ReportingYear.objects.filter(name=raw_year).first() if raw_year else None
    # ?group= lists one group in full (the "See all" link of a group cut at its first items).
    group = request.GET.get("group") or None
    groups = services.search(q, year, group=group, user=request.user) if q else []
    context = {
        "page_title": _("Search"),
        "page_subtitle": _("Partners, programme documents, indicators and pages"),
        "breadcrumbs": [_crumb(_("Search"))],
        "q": q,
        "group": group,
        "year_name": year.name if year else "",
        "groups": groups,
        "total": sum(len(g["items"]) for g in groups),
        "ai_enabled": llm.enabled(),
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
