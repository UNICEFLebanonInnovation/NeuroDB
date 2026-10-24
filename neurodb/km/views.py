"""The KM Database page (``/km/``): the findings of the version shown now (the default tab) and its documents,
25 rows a page. Every signed-in UNICEF role opens it (sign-in and the donor lock-out are the middleware's);
the menu shows it to superusers, and to the roles a superuser ticks on the Menu page.

It reads the KM tables only. An HTMX request (a page link) gets the table only.
"""

from __future__ import annotations

from typing import Any

from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils import formats
from django.views.decorators.http import require_GET

from neurodb.accounts.roles import can_set_up

from . import columns as cols
from . import words
from .models import KMDocument, KMFinding, KMVersion, live
from .words import say

PER_PAGE = 25
KEY_FINDING_SHOWN = 220  # characters of a key finding the table shows before "Show all"
ENTITY_SHOWN = 120
CONFIDENCE_HEADER = next(c.header for c in cols.FINDING_COLUMNS if c.kind == "confidence")
TONES = {"A": "success", "B": "info", "C": "warning", "D": "danger"}  # once Level A is confirmed strongest
FINDING_FIELDS = (
    "finding_id",
    "record_id",
    "key_finding",
    "pillar_code",
    "pillar",
    "rq_code",
    "research_question",
    "confidence",
    "confidence_text",
    "year",
    "year_text",
    "needs_review",
    "checks",
)
DOCUMENT_FIELDS = (
    "record_id",
    "title",
    "entity",
    "year",
    "year_text",
    "study_type",
    "confidence",
    "confidence_text",
    "needs_review",
    "checks",
)
REVIEW = Q(needs_review=True) | ~Q(checks={})


def _query_with(params: QueryDict, drop: tuple[str, ...] = (), **changes: Any) -> str:
    """``params`` as a query string without ``drop`` (and the page), with ``changes``."""
    out = params.copy()
    for key in (*drop, "page"):
        out.pop(key, None)
    for key, value in changes.items():
        out.pop(key, None)
        if value is not None:
            out[key] = value
    return out.urlencode()


def _date(value) -> str:
    return formats.date_format(value, "j M Y") if value else "—"


def version_line(version: KMVersion) -> str:
    """ "Prepared on 22 Oct 2026 · loaded on 24 Oct 2026 · 537 documents · 14,869 findings"."""
    figures = {
        "loaded": _date(version.finished_at or version.uploaded_at),
        "documents": formats.number_format(version.documents_count, force_grouping=True),
        "findings": formats.number_format(version.findings_count, force_grouping=True),
    }
    if version.prepared_on:
        line = say(words.PREPARED_LINE, prepared=_date(version.prepared_on), **figures)
    else:
        line = say(words.LOADED_LINE, **figures)
    if version.review_count:
        line += say(
            words.REVIEW_PART, review=formats.number_format(version.review_count, force_grouping=True)
        )
    return line


def confidence_pill(row) -> dict[str, str]:
    """The confidence level as the table shows it: the level (one neutral colour until Level A is confirmed
    the strongest), "Check this value" with the value as given, or "Not given"."""
    as_given = row.checks.get(CONFIDENCE_HEADER) if row.checks else None
    if as_given is not None:
        return {"label": str(words.CHECK_PILL), "tone": "warning", "title": as_given}
    if row.confidence:
        strongest = cols.CONFIDENCE_A_STRONGEST
        return {
            "label": row.get_confidence_display(),
            "tone": TONES[row.confidence] if strongest else "neutral",
            "title": str(words.CONFIDENCE_STRONGEST if strongest else words.CONFIDENCE_NEUTRAL),
        }
    return {"label": str(words.NOT_GIVEN), "tone": "neutral", "title": ""}


def _page(request: HttpRequest, tab: str) -> dict[str, Any]:
    """What both tabs show: the header, the version line, the tabs (each keeping the filter) and the state."""
    version = live()
    params = request.GET
    review = params.get("quality") == "review"
    keep = _query_with(params)
    context: dict[str, Any] = {
        "page_title": words.PAGE_TITLE,
        "tab": tab,
        "version": version,
        "version_line": version_line(version) if version else "",
        "loading": KMVersion.objects.filter(
            status__in=(KMVersion.Status.QUEUED, KMVersion.Status.LOADING)
        ).exists(),
        "can_set_up": can_set_up(request.user),
        "upload_url": reverse("admin:km_kmversion_upload"),
        "review": review,
        "all_url": request.path,
        "tabs": [
            {
                "key": "findings",
                "label": words.TAB_FINDINGS,
                "count": version.findings_count if version else 0,
                "url": reverse("km:index") + (f"?{keep}" if keep else ""),
            },
            {
                "key": "documents",
                "label": words.TAB_DOCUMENTS,
                "count": version.documents_count if version else 0,
                "url": reverse("km:documents") + (f"?{keep}" if keep else ""),
            },
        ],
        "words": words,
    }
    if context["can_set_up"]:
        context["actions"] = [{"label": words.UPLOAD_ACTION, "url": context["upload_url"]}]
    return context


def fold(text: str, size: int = KEY_FINDING_SHOWN) -> tuple[str, str]:
    """A long text cut after a word near ``size`` characters: what the row shows, and the rest (Show all)."""
    if len(text) <= size:
        return text, ""
    cut = text.rfind(" ", 0, size + 1)
    if cut < size * 0.6:  # no space near the end: cut where it is
        cut = size
    return text[:cut].rstrip() + "…", text[cut:].lstrip()


def _paginate(request: HttpRequest, queryset):
    return Paginator(queryset, PER_PAGE).get_page(request.GET.get("page"))


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    """The Findings tab."""
    context = _page(request, "findings")
    if context["version"] is not None:
        findings = KMFinding.objects.only(*FINDING_FIELDS).annotate(document_title_shown=F("document__title"))
        if context["review"]:
            findings = findings.filter(REVIEW)
        page = _paginate(request, findings.order_by("position"))
        for row in page.object_list:
            row.shown, row.rest = fold(row.key_finding)
            row.pill = confidence_pill(row)
        context["page_obj"] = page
    return _answer(request, "km/partials/findings.html", context)


@require_GET
def documents(request: HttpRequest) -> HttpResponse:
    """The Documents tab: each document with the number of its findings."""
    context = _page(request, "documents")
    if context["version"] is not None:
        rows = KMDocument.objects.only(*DOCUMENT_FIELDS).annotate(findings_rows=Count("findings"))
        if context["review"]:
            rows = rows.filter(REVIEW)
        page = _paginate(request, rows.order_by("position"))
        for row in page.object_list:
            row.pill = confidence_pill(row)
            row.entity_shown = row.entity[:ENTITY_SHOWN] + ("…" if len(row.entity) > ENTITY_SHOWN else "")
        context["page_obj"] = page
    return _answer(request, "km/partials/documents.html", context)


def _answer(request: HttpRequest, partial: str, context: dict[str, Any]) -> HttpResponse:
    """The table alone for an HTMX request (a page link), the whole page otherwise. With no version shown
    (it cannot be removed, so only before the first load) an HTMX request reloads the page instead."""
    if not request.htmx:
        return render(request, "km/index.html", context)
    if context["version"] is None:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return render(request, partial, context)
