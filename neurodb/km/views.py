"""The KM Database page (``/km/``): the findings of the version shown now (the default tab), its documents and
its assistant (the Ask tab, :mod:`neurodb.km.ask`), with one search box, the filters, sorting, a page for each
finding (also opened as a quick view from the table) and each document, and the downloads of the filter.
Every signed-in UNICEF role opens it (sign-in and the donor lock-out are the middleware's); the menu shows it
to superusers, and to the roles a superuser ticks on the Menu page.

It reads the KM tables only (and, while a version waits to load or loads, the load's lock: a version whose
load stopped is marked not loaded). An HTMX request (a filter, a page link) gets the table only.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Value
from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import formats
from django.utils.html import format_html
from django.utils.safestring import SafeData
from django.views.decorators.http import require_GET, require_POST

from neurodb.accounts.roles import can_set_up
from neurodb.assistant import llm
from neurodb.reports import exports as report_exports

from . import ask, authors, exports, loading, privacy, search, words
from . import columns as cols
from .filters import FLAG_PARAMS, NONE, ORIGINS, PER_PAGE, QUALITY, Filters, facets
from .models import Flag, KMDocument, KMFinding, KMQuestion, KMVersion, day, live
from .words import counted, say

KEY_FINDING_SHOWN = 220  # characters of a key finding the table shows before "Show all"
ENTITY_SHOWN = 120
CHIP_TITLE = 60  # characters of a document's title on the document filter's chip
CONFIDENCE_HEADER = next(c.header for c in cols.FINDING_COLUMNS if c.kind == "confidence")
TONES = {"A": "success", "B": "info", "C": "warning", "D": "danger"}  # once Level A is confirmed strongest
FINDING_FIELDS = (
    "finding_id",
    "record_id",
    "document_id",
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
    "position",
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
HEADERS = {c.field: c.header for c in cols.FINDING_COLUMNS}
DOCUMENT_HEADERS = {c.field: c.header for c in cols.DOCUMENT_COLUMNS}
FLAG_WORDS = dict(Flag.choices)


def _url(path: str, query: QueryDict) -> str:
    encoded = query.urlencode()
    return path + (f"?{encoded}" if encoded else "")


def version_line(version: KMVersion) -> str:
    """ "Prepared on 22 Oct 2026 · loaded on 24 Oct 2026 · 537 documents · 14,869 findings"."""
    figures = {
        "loaded": day(version.finished_at or version.uploaded_at),
        "documents": counted(version.documents_count, words.DOCUMENTS_N),
        "findings": counted(version.findings_count, words.FINDINGS_N),
    }
    if version.prepared_on:
        line = say(words.PREPARED_LINE, prepared=day(version.prepared_on), **figures)
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


def _loading() -> bool:
    """Whether a new version waits to load or loads. Only then is the load's lock looked at: a version whose
    load stopped (a restart) or never started is marked not loaded here, so the page does not say a new
    version is loading for good. With no version waiting, this is one query."""
    busy = KMVersion.objects.filter(status__in=(KMVersion.Status.QUEUED, KMVersion.Status.LOADING))
    if not busy.exists():
        return False
    return not loading.close_stale() or busy.exists()


def fold(text: str, size: int = KEY_FINDING_SHOWN) -> tuple[str, str]:
    """A long text cut after a word near ``size`` characters: what the row shows, and the rest (Show all)."""
    if len(text) <= size:
        return text, ""
    cut = text.rfind(" ", 0, size + 1)
    if cut < size * 0.6:  # no space near the end: cut where it is
        cut = size
    return text[:cut].rstrip() + "…", text[cut:].lstrip()


def cut(text: str, size: int) -> str:
    return text if len(text) <= size else text[:size].rstrip() + "…"


# ------------------------------------------------------------------------------------------- printouts
# A printout follows the download rule: a text the download writes otherwise (a name withheld) is shown as
# the workbook gives it on screen, and as the download writes it on paper (d-print-none / d-print-*).
def printed(text: str, people: privacy.People, document: str | None) -> str:
    """``text`` as a printout shows it (as a download writes it, with the own authors of ``document``, its
    Document ID), or "" when that is ``text`` itself."""
    if not text or not isinstance(text, str) or isinstance(text, SafeData):
        return ""
    own = people.own.get(document, frozenset()) if document is not None else frozenset()
    paper = privacy.for_download(text, people, own)
    return paper if paper != text else ""


def screen_and_paper(text: str, paper: str):
    """``text`` on screen and ``paper`` on paper, for a place a template writes as it is (a title)."""
    if not paper:
        return text
    return format_html(
        '<span class="d-print-none">{}</span><span class="d-none d-print-inline">{}</span>', text, paper
    )


def _paper_rows(rows: list[dict[str, Any]], people: privacy.People, document: str) -> None:
    """Each text line of a detail page with its printed copy (``printed``, and ``differs_printed`` for its
    note), when the download writes it otherwise. The authors' lines have their own (screen, paper)."""
    for r in rows:
        if r.get("screen") or r.get("paper"):
            continue
        r["printed"] = printed(r["value"], people, document)
        if r.get("differs"):
            r["differs_printed"] = printed(str(r["differs"]), people, document)


# ------------------------------------------------------------------------------------- the filter form
def _options(pairs, chosen) -> list[dict[str, Any]]:
    return [{"value": value, "label": label, "selected": value in chosen} for value, label in pairs]


def _form(filters: Filters, found: dict[str, Any]) -> dict[str, Any]:
    """The filter bar's choices, each with whether it is chosen."""
    pillars = dict(found.get("pillars", ()))
    questions: dict[str, list[dict[str, Any]]] = {}
    for q in found.get("questions", ()):
        questions.setdefault(q["pillar"], []).append(
            {"value": q["key"], "label": q["label"] or q["code"], "selected": q["key"] in filters.rq}
        )
    letters = [(c, f"Level {c}") for c in found.get("letters", ())] + [(NONE, str(words.NOT_GIVEN))]
    years = [(str(y), str(y)) for y in found.get("years", ())]
    if found.get("no_year"):
        years.append((NONE, str(words.NOT_REPORTED)))
    chips = [
        {
            "name": key,
            "title": title,
            "options": _options(
                [(f.field, f.label) for f in cols.FLAGS if f.field in FLAG_PARAMS[key]], getattr(filters, key)
            ),
        }
        for key, title in (("age", cols.AGE), ("sex", cols.SEX), ("sem", cols.SEM), ("theme", cols.THEMES))
    ]
    chips.append(
        {
            "name": "origin",
            "title": words.ORIGIN_TITLE,
            "options": _options([(o, str(words.ORIGIN_LABELS[o])) for o in ORIGINS], filters.origin),
        }
    )
    return {
        "pillars": _options(found.get("pillars", ()), filters.pillar),
        "questions": [
            {"label": cut(pillars.get(code) or code, 60), "options": options}
            for code, options in questions.items()
        ],
        "confidence": _options(letters, filters.confidence),
        "years": _options(years, filters.year),
        "types": _options([(t, t) for t in found.get("types", ())], filters.type),
        "domains": _options(found.get("domains", ()), filters.domain),
        "chips": chips,
        "quality": [
            {"value": q, "label": str(words.QUALITY_LABELS[q]), "selected": filters.quality == q}
            for q in ("", *QUALITY)
        ],
        "legend": words.CONFIDENCE_STRONGEST if cols.CONFIDENCE_A_STRONGEST else words.CONFIDENCE_NEUTRAL,
    }


def _page(request: HttpRequest, tab: str) -> dict[str, Any]:
    """What both tabs show: the header, the version line, the tabs (each keeping the filter), the filter bar
    and the state."""
    version = live()
    found = facets(version)
    filters = Filters.from_query(request.GET, found, for_ask=tab == "ask")
    keep = filters.query()
    keep.pop("sort", None)  # each tab sorts by its own columns
    without_document = keep.copy()
    without_document.pop("document", None)
    context: dict[str, Any] = {
        "page_title": words.PAGE_TITLE,
        "tab": tab,
        "version": version,
        "version_line": version_line(version) if version else "",
        "loading": _loading(),
        "can_set_up": can_set_up(request.user),
        "upload_url": reverse("admin:km_kmversion_upload"),
        "all_url": request.path,
        "filters": filters,
        "facets": found,
        "form": _form(filters, found) if version else {},
        "tabs": [
            {
                "key": "findings",
                "label": words.TAB_FINDINGS,
                "count": version.findings_count if version else 0,
                "url": _url(reverse("km:index"), keep),
                "drop": "",
            },
            {
                "key": "documents",
                "label": words.TAB_DOCUMENTS,
                "count": version.documents_count if version else 0,
                "url": _url(reverse("km:documents"), without_document),
                "drop": "document",
            },
            {  # the question is its own search: the search box's words, sort and page size stay behind
                "key": "ask",
                "label": words.TAB_ASK,
                "short": words.TAB_ASK_SHORT,  # on a phone, so that the three tabs fit
                "count": None,
                "url": _url(reverse("km:ask"), filters.query(ask=True)),
                "drop": "q per export",
            },
        ],
        "words": words,
    }
    if context["can_set_up"]:
        context["actions"] = [{"label": words.UPLOAD_ACTION, "url": context["upload_url"]}]
    return context


def _downloads(base: str, params: QueryDict) -> list[dict[str, str]]:
    out = []
    for label, fmt in ((words.EXCEL_OF_FILTER, "xlsx"), (words.CSV_OF_FILTER, "csv")):
        query = params.copy()
        query.pop("per", None)
        query["export"] = fmt
        out.append({"label": label, "url": _url(base, query)})
    return out


def _sorts(path: str, filters: Filters, keys: tuple[str, ...]) -> dict[str, str]:
    """The header links of the sorts ``keys``: each loads the page, so the filter bar keeps the sort."""
    out = {}
    for key in keys:
        query = filters.query()
        query.pop("sort", None)
        if key:
            query["sort"] = key
        out[key or "default"] = _url(path, query)
    if "-year" in out:  # the year's link: newest first, then oldest first
        out["year_toggle"] = out["year"] if filters.sort == "-year" else out["-year"]
    return out


def _per_links(path: str, filters: Filters) -> list[dict[str, Any]]:
    out = []
    for n in PER_PAGE:
        query = filters.query()
        query.pop("per", None)
        if n != PER_PAGE[0]:
            query["per"] = str(n)
        out.append({"n": n, "url": _url(path, query), "current": n == filters.per})
    return out


def _document_chip(filters: Filters, path: str) -> dict[str, str] | None:
    """The document filter's chip: its title (one query) and the address without it."""
    if not filters.document:
        return None
    title = KMDocument.objects.filter(record_id=filters.document).values_list("title", flat=True).first()
    query = filters.query()
    query.pop("document", None)
    return {
        "label": say(words.DOCUMENT_CHIP, title=cut(title or filters.document, CHIP_TITLE)),
        "remove": _url(path, query),
    }


def count_line(n: int, total: int | None, filters: Filters, noun) -> str:
    """ "214 findings of 14,869" (filtered), "14,869 findings"."""
    line = counted(n, noun)
    if total is not None and filters.any and n != total:
        line += " " + say(words.OF_ALL, total=formats.number_format(total, force_grouping=True))
    return line


def _findings_page(request: HttpRequest, qs, filters: Filters, how: str, people: privacy.People):
    """A page of findings as the table shows them (one count and one query), each with its printed copies."""
    qs = (
        qs.only(*FINDING_FIELDS)
        .annotate(document_title_shown=F("document__title"))
        .order_by(*filters.finding_order(how == "words"))
    )
    page = Paginator(qs, filters.per).get_page(request.GET.get("page"))
    marked = search.marked_words(filters.q) if how in ("words", "letters") else []
    for row in page.object_list:
        shown, rest = fold(row.key_finding)
        row.shown = search.highlight(shown, marked)
        row.rest = search.highlight(rest, marked) if rest else ""
        row.pill = confidence_pill(row)
        row.paper = printed(row.key_finding, people, row.record_id)
        row.paper_title = printed(row.document_title_shown, people, row.record_id)
    return page


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    """The Findings tab, and its downloads (``?export=xlsx|csv``)."""
    context = _page(request, "findings")
    version, filters = context["version"], context["filters"]
    if version is not None:
        qs, how = search.find(filters.apply(KMFinding.objects.all()), filters.q)
        export = request.GET.get("export")
        if export in report_exports.FORMATS:
            ordered = qs.order_by(*filters.finding_order(how == "words"))
            return exports.findings_export(ordered, export, version, filters, context["facets"])
        page = _findings_page(request, qs, filters, how, authors.people(version))
        context.update(
            page_obj=page,
            count_label=count_line(page.paginator.count, version.findings_count, filters, words.FINDINGS_N),
            how=how,
            chip=_document_chip(filters, request.path),
            sorts=_sorts(request.path, filters, ("", "pillar", "document", "-year", "year", "confidence")),
            per_links=_per_links(request.path, filters),
            downloads=_downloads(reverse("km:index"), filters.query()),
            results_id="km-results",
        )
    return _answer(request, "km/partials/findings.html", context)


@require_GET
def documents(request: HttpRequest) -> HttpResponse:
    """The Documents tab: each document with the number of its findings, and its downloads."""
    context = _page(request, "documents")
    version, filters = context["version"], context["filters"]
    if version is not None:
        filters.document = ""  # this tab has no document filter
        qs, how = search.find_documents(filters.documents(KMDocument.objects.all()), filters.q)
        qs = qs.annotate(findings_all=Count("findings"), findings_rows=_findings_of(filters))
        qs = qs.order_by(*filters.document_order(how == "words"))
        export = request.GET.get("export")
        if export in report_exports.FORMATS:
            return exports.documents_export(qs, export, version, filters, context["facets"])
        page = Paginator(qs.only(*DOCUMENT_FIELDS), filters.per).get_page(request.GET.get("page"))
        people = authors.people(version)
        keep = filters.query()
        keep.pop("sort", None)  # the Findings tab sorts by its own columns
        for row in page.object_list:
            row.pill = confidence_pill(row)
            row.entity_shown = cut(row.entity, ENTITY_SHOWN)
            query = keep.copy()
            query["document"] = row.record_id
            row.findings_url = _url(reverse("km:index"), query)  # the Findings tab, with the same filter
            if row.findings_rows != row.findings_all:
                row.findings_of = say(
                    words.OF_ALL, total=formats.number_format(row.findings_all, force_grouping=True)
                )
            row.paper_title = printed(row.title, people, row.record_id)
            row.paper_type = printed(row.study_type, people, row.record_id)
        context.update(
            page_obj=page,
            count_label=count_line(page.paginator.count, version.documents_count, filters, words.DOCUMENTS_N),
            how=how,
            sorts=_sorts(request.path, filters, ("", "title", "-year", "year", "findings")),
            per_links=_per_links(request.path, filters),
            downloads=_downloads(reverse("km:documents"), filters.query()),
            results_id="km-results",
        )
    return _answer(request, "km/partials/documents.html", context)


def _findings_of(filters: Filters):
    """The number of a document's findings the Documents tab shows: under a filter or a search, those the
    Findings tab shows for it with the same filter (its link opens them); otherwise all of them."""
    if not filters.any:
        return Count("findings")
    found, _how = search.find(filters.apply(KMFinding.objects.all()), filters.q)
    if found.query.is_empty():
        return Value(0)
    return Count("findings", filter=Q(findings__in=found.order_by().values("pk")))


def _answer(request: HttpRequest, partial: str, context: dict[str, Any]) -> HttpResponse:
    """The table alone for an HTMX request (a filter, a page link), the whole page otherwise. With no version
    shown (it cannot be removed, so only before the first load) an HTMX request reloads the page instead."""
    if not request.htmx:
        return render(request, "km/index.html", context)
    if context["version"] is None:
        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response
    return render(request, partial, context)


# --------------------------------------------------------------------------------------- the detail pages
def _missing(request: HttpRequest, version: KMVersion | None, sentence) -> HttpResponse:
    """A finding or document that the version shown does not hold (a link from an older version)."""
    message = say(sentence, label=version.label()) if version else str(words.MISSING_EMPTY)
    context = {"page_title": words.PAGE_TITLE, "message": message, "words": words}
    return render(request, "km/missing.html", context, status=404)


def _row(label, value, **extra) -> dict[str, Any] | None:
    """A line of a detail page, or None when the workbook leaves it empty (left out)."""
    if value in (None, ""):
        return None
    return {"label": label, "value": value, **extra}


def _checked(row, field: str, headers: dict[str, str]) -> bool:
    return bool(row.checks) and headers.get(field) in row.checks


def _given(row, field: str, headers: dict[str, str], shown: Any) -> Any:
    """A value to check as the workbook gives it, else ``shown``."""
    if _checked(row, field, headers):
        return row.checks[headers[field]]
    return shown


def _year(row) -> str:
    return str(row.year) if row.year is not None else row.year_text


def _link(url: str):
    """A link of the workbook: an ``http(s)://`` address opens in a new tab; anything else is text."""
    if url.lower().startswith(("http://", "https://")):
        return format_html('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', url, url)
    return url


def _marks(row: KMFinding) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """The marks of a finding: those marked Yes by group, the others by answer, and those to check."""
    marks = {flag.field: getattr(row, flag.field) for flag in cols.FLAGS}
    yes = []
    for group in cols.FLAG_GROUPS:
        chips = [f.label for f in cols.FLAGS if f.group == group and marks[f.field] == "Y"]
        if chips:
            yes.append({"title": group, "chips": chips})
    others = []
    for code in ("N", "R", "A", ""):
        found = [f.label for f in cols.FLAGS if marks[f.field] == code]
        if found:
            others.append({"label": FLAG_WORDS[code] if code else words.NOT_GIVEN, "flags": found})
    to_check = [f.label for f in cols.FLAGS if row.checks and f.header in row.checks]
    return yes, others, to_check


def _groups(row: KMFinding, doc: KMDocument, print_authors: str) -> list[dict[str, Any]]:
    """The finding page's groups, each with its non-empty lines."""
    h = HEADERS
    yes, others, to_check = _marks(row)
    differs = ""
    if doc.study_type and doc.study_type != row.study_type:
        differs = say(words.TYPE_DIFFERS, type=doc.study_type)
    groups = [
        {
            "title": words.GROUP_FINDING,
            "rows": [
                _row(words.ANALYSIS, row.analysis, long=True),
                _row(words.MEASURE, row.measure, long=True),
                _row(words.LIMITATIONS, row.limitations, long=True),
            ],
        },
        {
            "title": words.GROUP_FITS,
            "rows": [
                _row(words.PILLAR, row.pillar, check=_checked(row, "pillar", h)),
                _row(words.DOMAIN, _given(row, "domain", h, row.domain), check=_checked(row, "domain", h)),
                _row(
                    words.QUESTION,
                    _given(row, "research_question", h, row.research_question),
                    check=_checked(row, "research_question", h),
                ),
                _row(words.SUB_QUESTION, row.sub_question, long=True),
                _row(words.SUB_SUB_QUESTION, row.sub_sub_question, long=True),
                _row(words.INDICATOR, row.indicator, long=True),
                _row(words.INDICATOR_PART, row.indicator_component, long=True),
                _row(words.ALSO_RELEVANT, row.also_relevant, long=True),
            ],
        },
        {
            "title": words.GROUP_WHO,
            "rows": [_row(words.AGE_GIVEN, row.age_text, long=True)],
            "yes": yes,
            "others": others,
            "to_check": to_check,
            "after": [_row(words.POPULATION, row.population, long=True)],
        },
        {
            "title": words.GROUP_EVIDENCE,
            "rows": [
                _row(words.ORIGIN, row.evidence_origin, long=True),
                _row(words.CONFIDENCE, confidence_pill(row), pill=True),
                _row(words.METHODS, row.methods, long=True),
                _row(words.PERIOD, row.period),
                _row(
                    words.QUALITY, row.quality or str(words.NOT_GIVEN), quality=True, review=row.needs_review
                ),
            ],
        },
        {
            "title": words.GROUP_DOCUMENT,
            "rows": [
                _row(words.DOCUMENT_TITLE_LABEL, doc.title or row.document_title, document=True),
                _row(words.DOCUMENT_ID, doc.record_id),
                _row(words.YEAR, _given(row, "year", h, _year(row)), check=_checked(row, "year", h)),
                _row(
                    words.TYPE,
                    _given(row, "study_type", h, row.study_type),
                    check=_checked(row, "study_type", h),
                    differs=differs,
                ),
                _row(words.TEXT_USED, doc.text_used),
                _row(words.LINK, _link(row.link or doc.link)),
                _row(words.AUTHORS_SCREEN, row.entity or doc.entity, screen=True, long=True),
                _row(words.PRINT_AUTHORS, print_authors, paper=True),
            ],
        },
    ]
    for group in groups:
        group["rows"] = [r for r in group["rows"] if r]
        group["after"] = [r for r in group.get("after", ()) if r]
    return groups


@require_GET
def finding(request: HttpRequest, finding_id: str) -> HttpResponse:
    """One finding: its page, or its quick view (an HTMX request, from the table's Open button)."""
    version = live()
    row = None
    if version is not None:
        row = (
            KMFinding.objects.select_related("document")
            .defer("search_vector", "own_vector", "document__search_vector")
            .filter(finding_id=finding_id)
            .first()
        )
    if row is None:
        return _missing(request, version, words.MISSING_FINDING)
    doc = row.document
    older = None
    asked = request.GET.get("v", "")
    if asked.isascii() and asked.isdigit() and int(asked) != version.pk:  # never "²", which int() refuses
        older = KMVersion.objects.defer("content").filter(pk=int(asked)).first()
    known, people = authors.context(version)
    print_authors = authors.for_downloads(row.entity or doc.entity, known, people)
    document_url = reverse("km:document", args=[doc.record_id])
    all_url = f"{reverse('km:index')}?document={doc.record_id}"
    groups = _groups(row, doc, print_authors)
    for group in groups:
        _paper_rows([*group["rows"], *group["after"]], people, doc.record_id)
    crumb = cut(doc.title or doc.record_id, 60)
    paper_title = printed(doc.title, people, doc.record_id)
    pill = confidence_pill(row)  # next to the key finding, as in the table
    context = {
        "page_title": words.FINDING_HEADING,
        "page_subtitle": row.finding_id,
        "breadcrumbs": [
            {"label": words.PAGE_TITLE, "url": reverse("km:index")},
            {
                "label": screen_and_paper(crumb, cut(paper_title, 60) if paper_title else ""),
                "url": document_url,
            },
            {"label": words.FINDING_HEADING},
        ],
        "actions": [
            {"label": words.OPEN_DOCUMENT, "url": document_url},
            {"label": words.ALL_OF_DOCUMENT, "url": all_url},
            {"label": words.ASK_ABOUT_DOCUMENT, "url": f"{reverse('km:ask')}?document={doc.record_id}"},
        ],
        "header_include": "km/_print_button.html",
        "finding": row,
        "pill": pill,
        # "Check this value" once: the confidence level's pill says it already when the level is the value
        "check_pill": bool(row.checks) and pill["label"] != str(words.CHECK_PILL),
        "document": doc,
        "groups": groups,
        "lead_paper": printed(row.key_finding, people, doc.record_id),
        "older": say(words.OLDER_VERSION, label=older.label()) if older else "",
        "document_url": document_url,
        "all_url": all_url,
        "words": words,
    }
    if request.htmx:
        return render(request, "km/partials/finding.html", context)
    return render(request, "km/finding.html", context)


@require_GET
def document(request: HttpRequest, record_id: str) -> HttpResponse:
    """One document, with its findings (25 a page; a page link reloads the findings only)."""
    version = live()
    doc = None
    if version is not None:
        doc = (
            KMDocument.objects.defer("search_vector")
            .annotate(findings_rows=Count("findings"))
            .filter(record_id=record_id)
            .first()
        )
    if doc is None:
        return _missing(request, version, words.MISSING_DOCUMENT)
    filters = Filters()
    known, people = authors.context(version)
    page = _findings_page(request, KMFinding.objects.filter(document=doc), filters, "", people)
    table_url = f"{reverse('km:index')}?document={doc.record_id}"
    rows_differ = ""
    if doc.findings_stated is not None and doc.findings_stated != doc.findings_rows:
        rows_differ = say(words.ROWS_DIFFER, n=doc.findings_rows)
    rows = [
        _row(words.DOCUMENT_ID, doc.record_id),
        _row(words.AUTHORS_SCREEN, doc.entity, screen=True, long=True),
        _row(words.PRINT_AUTHORS, authors.for_downloads(doc.entity, known, people), paper=True),
        _row(
            words.YEAR,
            _given(doc, "year", DOCUMENT_HEADERS, _year(doc)),
            check=_checked(doc, "year", DOCUMENT_HEADERS),
        ),
        _row(
            words.TYPE,
            _given(doc, "study_type", DOCUMENT_HEADERS, doc.study_type),
            check=_checked(doc, "study_type", DOCUMENT_HEADERS),
        ),
        _row(words.TEXT_USED, doc.text_used),
        _row(words.CONFIDENCE, confidence_pill(doc), pill=True),
        _row(words.QUALITY, doc.quality or str(words.NOT_GIVEN), quality=True, review=doc.needs_review),
        _row(
            words.NUMBER_OF_FINDINGS,
            doc.findings_stated if doc.findings_stated is not None else doc.findings_rows,
            differs=rows_differ,
        ),
        _row(words.LINK, _link(doc.link)),
    ]
    rows = [r for r in rows if r]
    _paper_rows(rows, people, doc.record_id)
    context = {
        "page_title": screen_and_paper(doc.title or doc.record_id, printed(doc.title, people, doc.record_id)),
        "page_subtitle": doc.record_id,
        "breadcrumbs": [
            {"label": words.PAGE_TITLE, "url": reverse("km:index")},
            {"label": words.DOCUMENT_HEADING},
        ],
        "actions": [
            {"label": words.OPEN_IN_TABLE, "url": table_url},
            {"label": words.ASK_ABOUT_THIS_DOCUMENT, "url": f"{reverse('km:ask')}?document={doc.record_id}"},
        ],
        "header_include": "km/_print_button.html",
        "document": doc,
        "rows": rows,
        "page_obj": page,
        "count_label": count_line(page.paginator.count, None, filters, words.FINDINGS_N),
        "empty_title": words.NO_FINDINGS_FOR_DOCUMENT,  # a document page has no search box or filter
        "empty_icon": "evidence",
        "table_url": table_url,
        "results_id": "km-document-findings",
        "downloads": _downloads(reverse("km:index"), QueryDict(f"document={doc.record_id}")),
        "filters": filters,
        "words": words,
    }
    if request.htmx:
        return render(request, "km/partials/findings.html", context)
    return render(request, "km/document.html", context)


# ------------------------------------------------------------------------------- Ask the KM Database
@require_GET
def ask_page(request: HttpRequest) -> HttpResponse:
    """The Ask tab: the box, the filters it reads within (those of the address, without the search box's
    words), the person's recent questions and today's count."""
    context = _page(request, "ask")
    version, filters = context["version"], context["filters"]
    context["page_title"] = words.PAGE_TITLE
    if version is not None:
        title = ""
        if filters.document:
            title = (
                KMDocument.objects.filter(record_id=filters.document).values_list("title", flat=True).first()
                or filters.document
            )
        parts = ask.scope_parts(filters, context["facets"], document_title=cut(title, CHIP_TITLE))
        used, allowed = ask.quota(request.user)
        context.update(
            scope=filters.query(ask=True).urlencode(),
            scope_line=say(words.ASK_SCOPE, filters="; ".join(parts)) if parts else "",
            ai_on=ask.switched_on(),
            quota=say(words.ASK_QUOTA, used=used, allowed=allowed),
            limits=say(words.ASK_LIMITS, n=allowed),
            privacy_note=say(words.ASK_PRIVACY, provider=llm.names().label),
            recent=list(
                KMQuestion.objects.filter(user=request.user)
                .exclude(question="")
                .only("question", "status", "created_at")[: ask.RECENT]
            ),
            examples=[str(e) for e in words.ASK_EXAMPLES],
        )
    return render(request, "km/ask.html", context)


@require_POST
def ask_stream(request: HttpRequest) -> HttpResponse:
    """A question to the KM Database's assistant, answered as Server-Sent Events
    (:func:`neurodb.km.ask.stream`). In order: the question (1-1,000 characters); the conversation; the
    question cleaned for sending; the filters it reads within (``scope``: known keys only, never the
    search box's words); then, the person's row locked, at most 2 questions at once (429), and the
    reason no answer may be written (switched off, the AI resting, the KM Database's share of the day's
    AI budget, the shared budget, the person's quota, as many questions declined): with a reason the
    findings that match are listed, without a model call."""
    typed = (request.POST.get("question") or "").replace("\x00", "").strip()
    if not typed:
        return JsonResponse({"error": str(words.TYPE_A_QUESTION)}, status=400)
    if len(typed) > ask.QUESTION_CHARS:
        return JsonResponse({"error": str(words.QUESTION_TOO_LONG)}, status=400)
    version = live()
    if version is None:
        return JsonResponse({"error": str(words.ASK_EMPTY)}, status=409)
    try:
        conversation = uuid.UUID(str(request.POST.get("conversation") or ""))
    except ValueError:
        conversation = uuid.uuid4()
    _known, people = authors.context(version)
    cleaned = privacy.clean_question(typed, people)  # the typed text is searched only: never kept or sent
    found = facets(version)
    filters = Filters.from_query(QueryDict(str(request.POST.get("scope") or "")[:500]), found, for_ask=True)
    fields = {
        "user": request.user,
        "conversation": conversation,
        "version": version,
        "question": cleaned,
        "scope": filters.query(ask=True).urlencode()[:500],
    }
    with transaction.atomic():
        # questions sent at the same time are checked one after the other; the row written here counts
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if ask.running(request.user) >= ask.MAX_RUNNING_PER_USER:
            return JsonResponse({"error": str(words.ASK_BUSY)}, status=429)
        why, code = ask.reason(request.user)
        status = KMQuestion.Status.LIMITED if why else KMQuestion.Status.IN_PROGRESS
        row = KMQuestion.objects.create(**fields, status=status, error=code)
    ask.prune()
    turns = ask.history(request.user, conversation, version)
    response = StreamingHttpResponse(
        ask.stream(request, row, typed, filters, found, why, turns), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: the steps reach the browser as they happen
    return response
