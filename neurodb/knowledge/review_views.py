"""The document review page (``/knowledge/review/``, FMS §9 "Other Reports"): its tabs (Documents,
Findings, Dashboard, Synthesis, Actions, Report), the reviewers' actions (accept or reject, edit, add,
delete a finding; a statement's verdict; an action point's status), the batches and their documents
(create, rename, archive; add, analyse, mark as reference, take out), the Verified only switch, the theme
paragraph, the exports (CSV of the Findings and Actions tabs, Excel of the Actions tab) and the Word desk
review.

Everyone signed in (donor accounts apart: ``donors.middleware``) reads it; Viewers read only. Reviewing,
editing, the batches and starting an analysis need ``access.can_add`` (Administrators and Section
editors); the prompts and settings are the admin's (Administrators). The Verified only switch (each
person's, kept in the session) and "Write a paragraph" (each person's daily quota) are open to every
reader. What each tab counts is :mod:`review_data`'s.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Case, F, Max, Prefetch, When
from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.reports import exports

from . import review, review_data, review_docx, review_paragraph
from . import review_locate as where
from .access import can_add
from .models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentStatement,
    FindingCategory,
    ReviewBatch,
    Topic,
    Verdict,
)

VERIFIED_KEY = "doc_review_verified_only"
POLL_KEY, POLL_SECONDS = "doc_review_poll_until", 300
PAGE_SIZE = 50
TABS = [
    ("documents", gettext_lazy("Documents")),
    ("findings", gettext_lazy("Findings")),
    ("dashboard", gettext_lazy("Dashboard")),
    ("synthesis", gettext_lazy("Synthesis")),
    ("actions", gettext_lazy("Actions")),
    ("report", gettext_lazy("Report")),
]
FINDING_VIEWS = [
    ("all", gettext_lazy("All findings")),
    ("document", gettext_lazy("By document")),
    ("statements", gettext_lazy("Key statements")),
    ("index", gettext_lazy("Index")),
]
SYNTHESIS_VIEWS = [
    ("themes", gettext_lazy("Themes")),
    ("time", gettext_lazy("Over time")),
    ("coverage", gettext_lazy("Coverage")),
    ("repeated", gettext_lazy("Repeated findings")),
]
FINDING_COLUMNS = (
    "Batch", "Document", "Page", "Link", "Date", "Programme", "Subtopic", "Tag", "Category", "Place",
    "Place recognised", "Evidence", "Text", "Quote", "Reported or interpreted", "Verdict",
    "Added by a person", "Derived",
)  # fmt: skip
STATEMENT_COLUMNS = ("Batch", "Document", "Urgency", "Category", "Text", "Place", "Date", "Cites", "Verdict")
INDEX_COLUMNS = (
    "Batch", "Document", "Status", "Analysed on", "Text", "Findings stage", "Locate", "Summary",
    "Action points stage", "Findings", "Statements", "Action points", "Reviewed", "Notes",
)  # fmt: skip
ACTION_COLUMNS = (
    "Batch", "Document", "Where", "Action", "Owner", "Deadline", "Deadline date", "Priority", "Status",
    "Tag", "Programme", "Derived",
)  # fmt: skip
DRILL_KEYS = (
    "counted",
    "document",
    "subtopic",
    "year",
    "place",
    "gap",
    "band",
)  # set by links, kept by the filter
# open first, the nearest deadline first, then the highest priority
ACTION_ORDER = (
    Case(When(status="open", then=0), When(status="done", then=1), default=2),
    F("deadline_date").asc(nulls_last=True),
    Case(
        When(priority="high", then=0),
        When(priority="medium", then=1),
        When(priority="low", then=2),
        default=3,
    ),
    "pk",
)
CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")
MAX_TEXT = {"text": 1200, "quote": 1000, "place_text": 200, "date_text": 100, "page_label": 80}


# ------------------------------------------------------------------------------------------ helpers
def _verified(request: HttpRequest) -> bool:
    return bool(request.session.get(VERIFIED_KEY))


def _need_editor(request: HttpRequest) -> None:
    if not can_add(request.user):
        raise PermissionDenied


def _tab(request: HttpRequest) -> str:
    tab = request.GET.get("tab", "")
    return tab if tab in dict(TABS) else "documents"


def _page_url(tab: str = "documents", **params: Any) -> str:
    query = QueryDict(mutable=True)
    query["tab"] = tab
    for key, value in params.items():
        if value not in (None, "", False):
            query[key] = value
    return f"{reverse('knowledge:review')}?{query.urlencode()}"


def _back(request: HttpRequest, default: str) -> str:
    """Where a form goes back to: its ``next`` when it is an address of this site, else ``default``."""
    back = request.POST.get("next") or request.GET.get("next") or ""
    if back and url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure()):
        return back
    return default


def _query(params: QueryDict, **changes: Any) -> str:
    out = params.copy()
    for key in ("export", "page"):
        out.pop(key, None)
    for key, value in changes.items():
        out.pop(key, None)
        if value not in (None, ""):
            out[key] = value
    return out.urlencode()


def _safe_csv(value: Any) -> Any:
    """A text that a spreadsheet would read as a formula, kept as text (the documents are outside text)."""
    if isinstance(value, str) and value.startswith(CSV_RISKY):
        return "'" + value
    return value


def _csv(label: str, columns: tuple[str, ...], rows) -> HttpResponse:
    return exports.stream_csv(
        exports.export_filename(label, "csv"),
        columns,
        ({key: _safe_csv(value) for key, value in row.items()} for row in rows),
    )


def _review_on() -> tuple[bool, str]:
    on, why = review.switched_on()
    if on:
        return True, ""
    if why == review.OFF:
        return False, _(
            "The document review is switched off: an administrator turns it on in Document review settings."
        )
    return False, _("AI is switched off, so documents cannot be analysed.")


# ------------------------------------------------------------------------------------------ the page
@require_GET
def page(request: HttpRequest) -> HttpResponse:
    """The page and its tabs; an HTMX request (tab bar, filters, a chart's bar) gets the results only.
    ``export``: the CSV of the Findings tab's view, the CSV or Excel file of the Actions tab."""
    tab = _tab(request)
    verified = _verified(request)
    export = request.GET.get("export")
    if export and tab == "findings":
        return _findings_export(request, verified)
    if export and tab == "actions":
        return _actions_export(request, verified, export)
    context = {
        "tab": tab,
        "tabs": [{"key": k, "label": label, "query": _tab_query(request, k)} for k, label in TABS],
        "verified": verified,
        "can_edit": can_add(request.user),
        "batch_choices": list(ReviewBatch.objects.order_by("archived", "name")),
        "review_on": review.switched_on()[0],
    }
    context.update(TAB_CONTEXT[tab](request, verified))
    if request.htmx:
        return render(request, "knowledge/review/_results.html", context)
    context.update(
        {
            "page_title": _("Document review"),
            "page_subtitle": _(
                "Findings with evidence read from the documents put in a review batch: review them, count "
                "them, compare them across documents and download the desk review"
            ),
            "breadcrumbs": [
                {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
                {"label": _("Document review"), "url": None},
            ],
            "header_include": "knowledge/review/_verified_switch.html",
            "show_verified": verified or review_data.any_verdict(),
        }
    )
    return render(request, "knowledge/review/page.html", context)


def _tab_query(request: HttpRequest, tab: str) -> str:
    query = QueryDict(mutable=True)
    query["tab"] = tab
    if batch := review_data.batch_id(request.GET.get("batch")):
        query["batch"] = batch
    return query.urlencode()


def _documents(request: HttpRequest, verified: bool) -> dict[str, Any]:
    review.left_behind()  # a document left "being analysed" by a stopped run shows as failed
    batches = review_data.batches()
    batch = review_data.batch_id(request.GET.get("batch"))
    chosen = next((b for b in batches if b.pk == batch), None)
    rows = review_data.document_rows(chosen.pk if chosen else None)
    on, why = _review_on()
    can_edit = can_add(request.user)
    choices = []
    if can_edit and chosen:
        choices = list(
            Document.objects.filter(review_batch__isnull=True)
            .order_by("-created_at")
            .values_list("pk", "title")[:500]
        )
    # the list refreshes while a document is analysed, and for a few minutes after this person's Analyse
    # (the background run takes a moment to start); documents waiting for the night do not keep it going
    started = request.session.get(POLL_KEY, 0)
    working = any(d.review_status == Document.ReviewStatus.RUNNING for d in rows) or (
        started > timezone.now().timestamp()
        and any(d.review_status == Document.ReviewStatus.PENDING for d in rows)
    )
    return {
        "batches": batches,
        "batch": chosen,
        "rows": rows,
        "review_on": on,
        "review_off_reason": why,
        "addable": choices,
        "working": working and on,
        "documents_total": sum(b.n_documents for b in batches),
        "settings_url": reverse("admin:knowledge_documentreviewsettings_changelist")
        if request.user.is_superuser or _is_admin(request.user)
        else "",
        "upload_url": f"{reverse('knowledge:add')}?batch={chosen.pk}" if chosen else "",
        "next": _page_url("documents", batch=chosen.pk if chosen else None),
    }


def _is_admin(user) -> bool:
    from neurodb.accounts.roles import ADMIN, role_of

    return bool(user.is_authenticated and (user.is_superuser or role_of(user) == ADMIN))


def _findings(request: HttpRequest, verified: bool) -> dict[str, Any]:
    view = request.GET.get("view", "all")
    view = view if view in dict(FINDING_VIEWS) else "all"
    params = request.GET
    base = reverse("knowledge:review")
    context: dict[str, Any] = {
        "view": view,
        "views": [{"key": k, "label": label, "query": _query(params, view=k)} for k, label in FINDING_VIEWS],
        "categories": FindingCategory.choices,
        "verdicts": Verdict.choices,
        **review_data.topic_options(),
    }
    if view == "index":
        batch = review_data.batch_id(params.get("batch"))
        context.update(
            {
                "f": {"batch": batch},
                "rows": review_data.document_rows(batch),
                "export_url": f"{base}?{_query(params, export='csv')}",
            }
        )
        return context
    drill = ("counted", "document", "gap") if view == "statements" else DRILL_KEYS
    context["hidden"] = [(key, params[key]) for key in drill if params.get(key)]
    if view == "statements":
        sf = review_data.StatementFilter.from_params(params)
        qs = (
            sf.apply(verified)
            .select_related("document__review_batch", "reviewed_by")
            .prefetch_related(Prefetch("cites", queryset=DocumentFinding.objects.select_related("document")))
            .order_by("-urgency", "document__title", "position", "pk")
        )
        page_obj = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        context.update(
            {
                "f": sf,
                "page_obj": page_obj,
                "statements": list(page_obj.object_list),
                "chips": [
                    {"label": label, "url": f"{base}?{_query(params, **{key: None})}"}
                    for key, label in _statement_chips(sf)
                ],
                "query": _query(params),
                "export_url": f"{base}?{_query(params, export='csv')}",
                "high_urgency": review_data.HIGH_URGENCY,
            }
        )
        return context
    ff = review_data.FindingFilter.from_params(params)
    qs = ff.apply(verified).select_related(
        "document__review_batch", "topic__subtopic__programme", "reviewed_by", "edited_by"
    )
    context.update(
        {
            "f": ff,
            "chips": [
                {"label": label, "url": f"{base}?{_query(params, **{key: None})}"}
                for key, label in ff.chips()
            ],
            "query": _query(params),
            "export_url": f"{base}?{_query(params, export='csv')}",
        }
    )
    if view == "document":
        return {**context, **_by_document(request, ff, qs, verified)}
    page_obj = Paginator(qs.order_by("document__title", "position", "pk"), PAGE_SIZE).get_page(
        params.get("page")
    )
    context.update({"page_obj": page_obj, "findings": list(page_obj.object_list)})
    return context


def _statement_chips(sf: review_data.StatementFilter) -> list[tuple[str, str]]:
    """The statements' drill-downs the filter bar does not show: (key, words)."""
    out = []
    if sf.counted:
        out.append(("counted", _("As counted on the figures (rejected left out)")))
    if sf.document:
        title = Document.objects.filter(pk=sf.document).values_list("title", flat=True).first()
        out.append(("document", _("Document: %(title)s") % {"title": title or sf.document}))
    if sf.urgent:
        out.append(("urgent", _("Urgency %(n)s or more") % {"n": review_data.HIGH_URGENCY}))
    if sf.gap:
        out.append(("gap", review_data.STATEMENT_GAPS[sf.gap]))
    return out


def _by_document(request: HttpRequest, ff: review_data.FindingFilter, qs, verified: bool) -> dict[str, Any]:
    """The documents of the filter, newest analysis first, each with its key statements and findings;
    ``document``: that one only."""
    documents = review_data.in_review_documents(ff.batch).select_related("review_batch")
    if ff.document:
        documents = documents.filter(pk=ff.document)
    else:
        documents = documents.filter(pk__in=qs.values("document_id"))
    page_obj = Paginator(
        documents.order_by(F("reviewed_at").desc(nulls_last=True), "title", "pk"), 10
    ).get_page(request.GET.get("page"))
    shown = list(page_obj.object_list)
    ids = [d.pk for d in shown]
    by_doc: dict[int, list[DocumentFinding]] = {pk: [] for pk in ids}
    for finding in qs.filter(document_id__in=ids).order_by("position", "pk"):
        by_doc[finding.document_id].append(finding)
    said: dict[int, list[DocumentStatement]] = {pk: [] for pk in ids}
    for statement in (
        review_data.all_statements()
        .filter(document_id__in=ids)
        .select_related("reviewed_by")
        .prefetch_related("cites")
        .order_by("-urgency", "position")
    ):
        said[statement.document_id].append(statement)
    for document in shown:
        document.shown_findings = by_doc[document.pk]
        document.key_statements = said[document.pk]
        document.unreviewed = sum(f.verdict == Verdict.UNREVIEWED for f in document.shown_findings)
    return {"documents_page": page_obj, "documents": shown, "high_urgency": review_data.HIGH_URGENCY}


def _dashboard(request: HttpRequest, verified: bool) -> dict[str, Any]:
    batch = review_data.batch_id(request.GET.get("batch"))
    figures = review_data.dashboard(batch, verified)
    base = reverse("knowledge:review")

    def to(tab: str = "findings", **params: Any) -> str:
        return _page_url(tab, batch=batch, **params)

    tiles = figures["tiles"]
    links = {
        "documents": to("findings", view="index"),
        "findings": to(counted="1"),
        "statements": to(view="statements", counted="1"),
        "urgent": to(view="statements", counted="1", urgent="1"),
        "open_actions": to("actions", status="open", period="all"),
    }
    for tile in figures["quality"]:
        if tile["key"] == "uncited":
            tile["href"] = to(view="statements", counted="1", gap="uncited")
        else:
            tile["href"] = to(counted="1", gap=tile["key"])
    query = f"tab=findings&counted=1{f'&batch={batch}' if batch else ''}"
    hrefs = {
        name: f"{base}?{query}&{key}={{drill}}"
        for name, key in (
            ("category", "category"),
            ("programme", "programme"),
            ("tags", "topic"),
            ("evidence", "band"),
            ("years", "year"),
            ("places", "place"),
        )
    }
    return {
        "batch": batch,
        "figures": figures,
        "tiles": tiles,
        "tile_links": links,
        "chart_data": figures["charts"],
        "chart_hrefs": hrefs,
        "high_urgency": review_data.HIGH_URGENCY,
    }


def _min_documents(params: QueryDict) -> int:
    value = review_data._int(params.get("min"), 2, 5)
    return value if value in review_data.MIN_DOCUMENTS else 2


def _synthesis(request: HttpRequest, verified: bool) -> dict[str, Any]:
    params = request.GET
    batch = review_data.batch_id(params.get("batch"))
    view = params.get("view", "themes")
    view = view if view in dict(SYNTHESIS_VIEWS) else "themes"
    min_documents = _min_documents(params)
    challenges = params.get("challenges") == "1"
    q = " ".join(params.get("q", "").split())[:100]
    context: dict[str, Any] = {
        "view": view,
        "views": [
            {"key": k, "label": label, "query": _query(params, view=k)} for k, label in SYNTHESIS_VIEWS
        ],
        "batch": batch,
        "min_documents": min_documents,
        "min_choices": review_data.MIN_DOCUMENTS,
        "challenges": challenges,
        "q": q,
        "paragraph_on": review_paragraph.available(),
    }
    used, allowed = review_paragraph.quota(request.user)
    context["quota"] = {"used": used, "allowed": allowed}
    if view == "repeated":
        context["groups"] = review_data.repeated(batch, verified)
        context["similar"] = round(review_data.SIMILAR * 100)
        return context
    found = review_data.synthesis(batch, verified, min_documents, q, challenges)
    context.update({"synthesis": found, "trends": review_data.TRENDS})
    if view == "time":
        context["over_time"] = [
            {"key": key, "label": label, "themes": themes}
            for key, label in review_data.TRENDS.items()
            if (themes := found.over_time()[key])
        ]
    elif view == "coverage":
        single = found.coverage()
        by_batch: dict[str, list] = {}
        for theme in single:
            by_batch.setdefault(next(iter(theme.batches.values())), []).append(theme)
        context.update({"single": single, "by_batch": sorted(by_batch.items())})
    else:
        for theme in found.themes[:60]:
            theme.top = review_data.theme_findings(theme.topic.pk, batch, verified, challenges, limit=5)
            theme.findings_url = _page_url(
                "findings", batch=batch, topic=theme.topic.pk, counted="1",
                category="challenge" if challenges else None,
            )  # fmt: skip
        context["shown_themes"] = found.themes[:60]
    return context


def _actions(request: HttpRequest, verified: bool) -> dict[str, Any]:
    params = request.GET
    af = review_data.ActionFilter.from_params(params)
    base_qs = af.base(verified)
    figures = review_data.action_figures(base_qs)
    qs = (
        af.apply(verified)
        .select_related("document__review_batch", "topic__subtopic__programme")
        .prefetch_related(Prefetch("cites", queryset=DocumentFinding.objects.select_related("document")))
        .order_by(*ACTION_ORDER)
    )
    page_obj = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
    rows = list(page_obj.object_list)
    today = timezone.localdate()
    for point in rows:
        point.overdue = point.status == DocumentActionPoint.Status.OPEN and bool(
            point.deadline_date and point.deadline_date < today
        )
        cited = [f for f in point.cites.all() if f.verdict != Verdict.REJECTED]
        point.where = cited[0] if cited else None
    base = reverse("knowledge:review")
    plain = QueryDict(mutable=True)
    plain.update({"tab": "actions", "period": af.period})
    if af.batch:
        plain["batch"] = af.batch

    def card(**changes: Any) -> str:
        return f"{base}?{_query(plain, **changes)}"

    return {
        "f": af,
        "figures": figures,
        "cards": {
            "open": card(status="open"),
            "overdue": card(status="open", overdue="1"),
            "high": card(status="open", priority="high"),
            "done": card(status="done"),
        },
        "page_obj": page_obj,
        "points": rows,
        "owners": review_data.owners(base_qs),
        "statuses": DocumentActionPoint.Status.choices,
        "priorities": DocumentActionPoint.Priority.choices,
        "query": _query(params),
        "chart_data": {"by_owner": figures["by_owner"]},
        "owner_href": f"{base}?{_query(params, owner=None, status=None)}&status=open&owner={{drill}}",
        "downloads": [
            {"label": _("CSV of the filter"), "url": f"{base}?{_query(params, export='csv')}"},
            {"label": _("Excel of the filter"), "url": f"{base}?{_query(params, export='xlsx')}"},
        ],
        "today": today,
    }


def _report(request: HttpRequest, verified: bool) -> dict[str, Any]:
    params = request.GET
    batch = review_data.batch_id(params.get("batch"))
    min_documents = _min_documents(params)
    figures = review_data.dashboard(batch, verified)
    found = review_data.synthesis(batch, verified, min_documents)
    query = QueryDict(mutable=True)
    query["min"] = min_documents
    if batch:
        query["batch"] = batch
    return {
        "batch": batch,
        "min_documents": min_documents,
        "min_choices": review_data.MIN_DOCUMENTS,
        "tiles": figures["tiles"],
        "themes": len(found.themes),
        "single": len(found.coverage()),
        "download_url": f"{reverse('knowledge:review_docx')}?{query.urlencode()}",
        "filename": review_docx.filename(),
    }


TAB_CONTEXT = {
    "documents": _documents,
    "findings": _findings,
    "dashboard": _dashboard,
    "synthesis": _synthesis,
    "actions": _actions,
    "report": _report,
}


# ------------------------------------------------------------------------------------------ exports
def _findings_export(request: HttpRequest, verified: bool) -> HttpResponse:
    view = request.GET.get("view", "all")
    if view == "index":
        rows = review_data.document_rows(review_data.batch_id(request.GET.get("batch")))
        return _csv("document-review-index", INDEX_COLUMNS, (_index_row(d) for d in rows))
    if view == "statements":
        qs = (
            review_data.StatementFilter.from_params(request.GET)
            .apply(verified)
            .select_related("document__review_batch")
            .prefetch_related(Prefetch("cites", queryset=DocumentFinding.objects.select_related("document")))
            .order_by("-urgency", "document__title", "pk")
        )
        return _csv(
            "document-statements", STATEMENT_COLUMNS, (_statement_row(s) for s in qs.iterator(chunk_size=500))
        )
    qs = (
        review_data.FindingFilter.from_params(request.GET)
        .apply(verified)
        .select_related("document__review_batch", "topic__subtopic__programme")
        .order_by("document__review_batch__name", "document__title", "position", "pk")
    )
    host = request.build_absolute_uri("/").rstrip("/")
    return _csv(
        "document-findings", FINDING_COLUMNS, (_finding_row(f, host) for f in qs.iterator(chunk_size=500))
    )


def _finding_row(f: DocumentFinding, host: str) -> dict[str, Any]:
    return {
        "Batch": f.document.review_batch.name if f.document.review_batch else "",
        "Document": f.document.title,
        "Page": f.page_label,
        "Link": host + f.url,
        "Date": f.finding_date.isoformat() if f.finding_date else f.date_text,
        "Programme": f.topic.subtopic.programme.name,
        "Subtopic": f.topic.subtopic.name,
        "Tag": f.topic.name,
        "Category": f.get_category_display(),
        "Place": f.place_text,
        "Place recognised": f.get_place_match_display() if f.place_match else "",
        "Evidence": f.evidence,
        "Text": f.text,
        "Quote": f.quote,
        "Reported or interpreted": f.get_kind_display(),
        "Verdict": f.get_verdict_display(),
        "Added by a person": "yes" if f.manual else "no",
        "Derived": "yes" if f.derived else "no",
    }


def _statement_row(s: DocumentStatement) -> dict[str, Any]:
    return {
        "Batch": s.document.review_batch.name if s.document.review_batch else "",
        "Document": s.document.title,
        "Urgency": s.urgency,
        "Category": s.get_category_display() if s.category else "",
        "Text": s.text,
        "Place": s.place_text,
        "Date": s.date_text,
        "Cites": "; ".join(review_data.cite(f) for f in s.cites.all()),
        "Verdict": s.get_verdict_display(),
    }


def _index_row(d: Document) -> dict[str, Any]:
    states = {chip["stage"]: chip["state"] for chip in d.stage_chips}
    return {
        "Batch": d.review_batch.name if d.review_batch else "",
        "Document": d.title,
        "Status": d.get_review_status_display(),
        "Analysed on": timezone.localtime(d.reviewed_at).date().isoformat() if d.reviewed_at else "",
        "Text": states[review.TEXT],
        "Findings stage": states[review.FINDINGS],
        "Locate": states[review.PLACES],
        "Summary": states[review.SUMMARY],
        "Action points stage": states[review.ENRICHMENT],
        "Findings": d.n_findings,
        "Statements": d.n_statements,
        "Action points": d.n_actions,
        "Reviewed": d.n_reviewed,
        "Notes": " | ".join(
            f"{chip['label']}: {chip['note']}"
            for chip in d.stage_chips
            if chip["key"] in ("partial", "failed")
        )
        or d.stopped,
    }


def _action_row(p: DocumentActionPoint, typed: bool) -> dict[str, Any]:
    cited = [f for f in p.cites.all() if f.verdict != Verdict.REJECTED]
    return {
        "Batch": p.document.review_batch.name if p.document.review_batch else "",
        "Document": p.document.title,
        "Where": cited[0].page_label if cited else "",
        "Action": p.action,
        "Owner": p.owner_text,
        "Deadline": p.deadline_text,
        "Deadline date": p.deadline_date
        if typed
        else (p.deadline_date.isoformat() if p.deadline_date else ""),
        "Priority": p.get_priority_display(),
        "Status": p.get_status_display(),
        "Tag": p.topic.name if p.topic else "",
        "Programme": p.topic.subtopic.programme.name if p.topic else "",
        "Derived": "yes" if p.derived else "no",
    }


def _actions_export(request: HttpRequest, verified: bool, export: str) -> HttpResponse:
    qs = (
        review_data.ActionFilter.from_params(request.GET)
        .apply(verified)
        .select_related("document__review_batch", "topic__subtopic__programme")
        .prefetch_related("cites")
        .order_by("document__review_batch__name", "document__title", "position", "pk")
    )
    if export == "xlsx":
        return exports.xlsx_response(
            exports.export_filename("document-action-points", "xlsx"),
            [("Action points", ACTION_COLUMNS, (_action_row(p, True) for p in qs))],
            typed=True,
        )
    if export != "csv":
        raise Http404
    return _csv("document-action-points", ACTION_COLUMNS, (_action_row(p, False) for p in qs))


@require_GET
def desk_review(request: HttpRequest) -> HttpResponse:
    """The Word desk review of the batch (every batch when none) as the Report tab sets it."""
    batch = review_data.batch_id(request.GET.get("batch"))
    content = review_docx.build(batch, _verified(request), _min_documents(request.GET))
    response = HttpResponse(
        content, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    response["Content-Disposition"] = f'attachment; filename="{review_docx.filename()}"'
    return response


# ------------------------------------------------------------------------------------------ the switch
@require_POST
def verified_only(request: HttpRequest) -> HttpResponse:
    """Turn Verified only on or off for this person (kept in the session); every reader may."""
    request.session[VERIFIED_KEY] = request.POST.get("on") == "1"
    # back to the tab shown: the address bar (the referrer) follows the tabs, the form's next does not
    referrer = request.META.get("HTTP_REFERER", "")
    if referrer and url_has_allowed_host_and_scheme(referrer, {request.get_host()}, request.is_secure()):
        return redirect(referrer)
    return redirect(_back(request, reverse("knowledge:review")))


# ------------------------------------------------------------------------------------------ batches
def _batch_form(request: HttpRequest) -> tuple[str, str]:
    name = " ".join(request.POST.get("name", "").split())[:200]
    description = request.POST.get("description", "").strip()[:2000]
    return name, description


@require_POST
def batch_new(request: HttpRequest) -> HttpResponse:
    _need_editor(request)
    name, description = _batch_form(request)
    if not name:
        messages.error(request, _("Give the batch a name."))
        return redirect(_back(request, _page_url()))
    batch = ReviewBatch.objects.create(name=name, description=description, created_by=request.user)
    messages.success(request, _("Batch “%(name)s” created: add documents to it.") % {"name": batch.name})
    return redirect(_page_url("documents", batch=batch.pk))


@require_POST
def batch_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Rename a batch, or archive it (kept, no longer analysed) or bring it back."""
    _need_editor(request)
    batch = get_object_or_404(ReviewBatch, pk=pk)
    action = request.POST.get("action", "rename")
    if action == "archive":
        batch.archived = True
        batch.save(update_fields=["archived"])
        messages.success(
            request, _("Batch archived: its documents are no longer analysed; what was found stays.")
        )
    elif action == "restore":
        batch.archived = False
        batch.save(update_fields=["archived"])
        messages.success(request, _("Batch brought back: its documents are analysed again."))
    else:
        name, description = _batch_form(request)
        if not name:
            messages.error(request, _("Give the batch a name."))
        else:
            batch.name, batch.description = name, description
            batch.save(update_fields=["name", "description"])
            messages.success(request, _("Batch renamed."))
    return redirect(_back(request, _page_url("documents", batch=batch.pk)))


@require_POST
def batch_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Put chosen knowledge base documents (not in a batch yet) in this batch: they wait to be analysed."""
    _need_editor(request)
    batch = get_object_or_404(ReviewBatch, pk=pk)
    ids = [int(v) for v in request.POST.getlist("documents") if v.isdigit()][:500]
    documents = Document.objects.filter(pk__in=ids, review_batch__isnull=True)
    added = review.put_in_batch(documents, batch)
    if added:
        messages.success(
            request,
            _("%(n)s document(s) added; they are analysed at the next nightly run, or now with Analyse.")
            % {"n": added},
        )
    else:
        messages.info(request, _("No document added: choose documents that are not in a batch yet."))
    return redirect(_back(request, _page_url("documents", batch=batch.pk)))


# ------------------------------------------------------------------------------------------ documents
def _in_batch(pk: int) -> Document:
    document = get_object_or_404(Document, pk=pk)
    if not document.review_batch_id:
        raise Http404("This document is not in a review batch.")
    return document


@require_POST
def analyse(request: HttpRequest, pk: int) -> HttpResponse:
    """Analyse (or re-analyse) one document now, in the background."""
    _need_editor(request)
    document = _in_batch(pk)
    back = _back(request, _page_url("documents", batch=document.review_batch_id))
    on, why = _review_on()
    if not on:
        messages.error(request, why)
    elif document.review_status == Document.ReviewStatus.REFERENCE:
        messages.error(request, _("A reference document is not analysed: unmark it first."))
    elif document.review_batch and document.review_batch.archived:
        messages.error(request, _("The batch is archived: bring it back to analyse its documents."))
    else:
        review.start(document, triggered_by="page")
        request.session[POLL_KEY] = timezone.now().timestamp() + POLL_SECONDS
        messages.success(
            request,
            _("Analysing “%(title)s” in the background: a few minutes for a long report.")
            % {"title": document.title},
        )
    return redirect(back)


@require_POST
def reference(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a document as a reference (in its batch, never analysed, out of every count) or unmark it."""
    _need_editor(request)
    document = _in_batch(pk)
    review.set_reference(document, request.POST.get("reference") == "1")
    return redirect(_back(request, _page_url("documents", batch=document.review_batch_id)))


@require_POST
def take_out(request: HttpRequest, pk: int) -> HttpResponse:
    """Take a document out of its batch: no longer analysed; what was found stays with it, out of view."""
    _need_editor(request)
    document = _in_batch(pk)
    batch = document.review_batch_id
    review.take_out(document)
    messages.success(request, _("“%(title)s” was taken out of the review.") % {"title": document.title})
    return redirect(_back(request, _page_url("documents", batch=batch)))


@require_POST
def bulk_verdict(request: HttpRequest, pk: int) -> HttpResponse:
    """Accept all or Reject all: the findings of the document still not reviewed (the others stay)."""
    _need_editor(request)
    document = _in_batch(pk)
    verdict = request.POST.get("verdict", "")
    if verdict not in (Verdict.ACCEPTED, Verdict.REJECTED):
        raise Http404
    count = document.findings.filter(verdict=Verdict.UNREVIEWED).update(
        verdict=verdict, reviewed_by=request.user, reviewed_at=timezone.now()
    )
    messages.success(
        request,
        _("%(n)s finding(s) marked %(verdict)s.") % {"n": count, "verdict": Verdict(verdict).label.lower()},
    )
    return redirect(_back(request, _page_url("findings", view="document", document=document.pk)))


# ------------------------------------------------------------------------------------------ findings
def _row_context(request: HttpRequest, **extra: Any) -> dict[str, Any]:
    return {"can_edit": can_add(request.user), **extra}


@require_POST
def finding_verdict(request: HttpRequest, pk: int) -> HttpResponse:
    _need_editor(request)
    finding = get_object_or_404(DocumentFinding.objects.select_related("document__review_batch"), pk=pk)
    verdict = request.POST.get("verdict", "")
    if verdict not in Verdict.values:
        raise Http404
    finding.verdict = verdict
    finding.reviewed_by = request.user if verdict != Verdict.UNREVIEWED else None
    finding.reviewed_at = timezone.now() if verdict != Verdict.UNREVIEWED else None
    finding.save(update_fields=["verdict", "reviewed_by", "reviewed_at"])
    if request.htmx:
        finding = DocumentFinding.objects.select_related(
            "document__review_batch", "topic__subtopic__programme", "reviewed_by", "edited_by"
        ).get(pk=pk)
        return render(
            request,
            "knowledge/review/_finding_row.html",
            _row_context(request, finding=finding, compact=request.POST.get("compact") == "1"),
        )
    return redirect(_back(request, _page_url("findings")))


@require_POST
def statement_verdict(request: HttpRequest, pk: int) -> HttpResponse:
    _need_editor(request)
    statement = get_object_or_404(DocumentStatement, pk=pk)
    verdict = request.POST.get("verdict", "")
    if verdict not in Verdict.values:
        raise Http404
    statement.verdict = verdict
    statement.reviewed_by = request.user if verdict != Verdict.UNREVIEWED else None
    statement.reviewed_at = timezone.now() if verdict != Verdict.UNREVIEWED else None
    statement.save(update_fields=["verdict", "reviewed_by", "reviewed_at"])
    if request.htmx:
        statement = (
            DocumentStatement.objects.select_related("document__review_batch", "reviewed_by")
            .prefetch_related(Prefetch("cites", queryset=DocumentFinding.objects.select_related("document")))
            .get(pk=pk)
        )
        return render(
            request,
            "knowledge/review/_statement_row.html",
            _row_context(
                request,
                s=statement,
                compact=request.POST.get("compact") == "1",
                high_urgency=review_data.HIGH_URGENCY,
            ),
        )
    return redirect(_back(request, _page_url("findings", view="statements")))


def _finding_values(request: HttpRequest, finding: DocumentFinding | None) -> dict[str, str]:
    if request.method == "POST":
        values = {
            key: " ".join(request.POST.get(key, "").split())[: MAX_TEXT.get(key, 200)]
            for key in ("category", "topic", "text", "quote", "page", "place_text", "date_text", "kind")
        }
        return values
    if finding is None:
        return {
            "category": FindingCategory.OBSERVATION, "topic": "", "text": "", "quote": "", "page": "",
            "place_text": "", "date_text": "", "kind": DocumentFinding.Kind.REPORTED,
        }  # fmt: skip
    return {
        "category": finding.category,
        "topic": str(finding.topic_id),
        "text": finding.text,
        "quote": finding.quote,
        "page": str(finding.page_from or ""),
        "place_text": finding.place_text,
        "date_text": finding.date_text,
        "kind": finding.kind,
    }


def _check(values: dict[str, str], document: Document) -> tuple[dict[str, str], Topic | None, int | None]:
    errors: dict[str, str] = {}
    if not values["text"]:
        errors["text"] = _("Write the finding in one to three sentences.")
    if values["category"] not in FindingCategory.values:
        errors["category"] = _("Choose a category.")
    if values["kind"] not in DocumentFinding.Kind.values:
        values["kind"] = DocumentFinding.Kind.REPORTED
    topic = Topic.objects.filter(pk=values["topic"]).first() if values["topic"].isdigit() else None
    if topic is None:
        errors["topic"] = _("Choose a topic (Other when none fits).")
    page = None
    if values["page"]:
        if not values["page"].isdigit() or not (1 <= int(values["page"]) <= max(document.pages, 1) + 1000):
            errors["page"] = _("Write the page as a number.")
        else:
            page = int(values["page"])
    return errors, topic, page


def _place(finding: DocumentFinding, document: Document, page: int | None) -> None:
    """Where the finding is, its place, date and evidence, as the analysis works them out (no AI); the
    page a person gave stays when the quote is not found in the text."""
    index, sheets = where.TextIndex(document), where.sheet_names(document)
    where.locate(finding, index, where.Gazetteer(), sheets, Topic.other().pk)
    if page is not None and not finding.exact_page:
        finding.page_from = finding.page_to = page
        finding.page_label = where.label(index.kind, page, page, sheets)


def _form_response(request: HttpRequest, template_context: dict[str, Any], title: str) -> HttpResponse:
    if request.htmx:
        return render(request, "knowledge/review/_finding_form.html", {**template_context, "modal": True})
    template_context.update(
        {
            "page_title": title,
            "breadcrumbs": [
                {"label": _("Document review"), "url": reverse("knowledge:review")},
                {"label": title, "url": None},
            ],
        }
    )
    return render(request, "knowledge/review/finding_form.html", template_context)


def _done(request: HttpRequest, back: str) -> HttpResponse:
    if request.htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = back
        return response
    return redirect(back)


@require_http_methods(["GET", "POST"])
def finding_new(request: HttpRequest, pk: int) -> HttpResponse:
    """Add a finding the AI missed (a person's: accepted by them, kept by every new analysis)."""
    _need_editor(request)
    document = _in_batch(pk)
    values = _finding_values(request, None)
    errors: dict[str, str] = {}
    back = _back(request, _page_url("findings", view="document", document=document.pk))
    if request.method == "POST":
        errors, topic, page = _check(values, document)
        if not errors:
            now = timezone.now()
            last = document.findings.aggregate(last=Max("position"))["last"]
            finding = DocumentFinding(
                document=document,
                position=(last or 0) + 1,
                category=values["category"],
                topic=topic,
                text=values["text"],
                quote=values["quote"],
                kind=values["kind"],
                place_text=values["place_text"],
                date_text=values["date_text"],
                manual=True,
                verdict=Verdict.ACCEPTED,
                reviewed_by=request.user,
                reviewed_at=now,
                edited_by=request.user,
                edited_at=now,
            )
            _place(finding, document, page)
            finding.save()
            messages.success(request, _("Finding added (accepted; a new analysis keeps it)."))
            return _done(request, back)
    context = {
        "document": document,
        "values": values,
        "errors": errors,
        "action": reverse("knowledge:review_finding_new", args=[document.pk]),
        "next": back,
        "categories": FindingCategory.choices,
        "kinds": DocumentFinding.Kind.choices,
        "title": _("Add a finding"),
        **review_data.topic_options(),
    }
    return _form_response(request, context, _("Add a finding"))


@require_http_methods(["GET", "POST"])
def finding_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Correct a finding; a new analysis keeps a person's edit (with the verdict)."""
    _need_editor(request)
    finding = get_object_or_404(DocumentFinding.objects.select_related("document"), pk=pk)
    document = finding.document
    values = _finding_values(request, finding)
    errors: dict[str, str] = {}
    back = _back(request, _page_url("findings", view="document", document=document.pk))
    if request.method == "POST":
        errors, topic, page = _check(values, document)
        if not errors:
            finding.category, finding.topic = values["category"], topic
            finding.text, finding.quote, finding.kind = values["text"], values["quote"], values["kind"]
            finding.place_text, finding.date_text = values["place_text"], values["date_text"]
            finding.edited_by, finding.edited_at = request.user, timezone.now()
            _place(finding, document, page)
            finding.save()
            messages.success(request, _("Finding saved; a new analysis keeps your changes."))
            return _done(request, back)
    context = {
        "document": document,
        "finding": finding,
        "values": values,
        "errors": errors,
        "action": reverse("knowledge:review_finding_edit", args=[finding.pk]),
        "next": back,
        "categories": FindingCategory.choices,
        "kinds": DocumentFinding.Kind.choices,
        "title": _("Edit a finding"),
        **review_data.topic_options(),
    }
    return _form_response(request, context, _("Edit a finding"))


@require_POST
def finding_delete(request: HttpRequest, pk: int) -> HttpResponse:
    _need_editor(request)
    finding = get_object_or_404(DocumentFinding, pk=pk)
    document = finding.document_id
    finding.delete()
    if request.htmx:
        return HttpResponse("")
    messages.success(request, _("Finding deleted."))
    return redirect(_back(request, _page_url("findings", view="document", document=document)))


# ------------------------------------------------------------------------------------------ actions
@require_POST
def action_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Set an action point open, done or dropped: people only; a new analysis keeps it."""
    _need_editor(request)
    point = get_object_or_404(DocumentActionPoint, pk=pk)
    status = request.POST.get("status", "")
    if status not in DocumentActionPoint.Status.values:
        raise Http404
    if status != point.status:
        point.status, point.status_by, point.status_at = status, request.user, timezone.now()
        point.save(update_fields=["status", "status_by", "status_at"])
    if request.htmx:
        point = (
            DocumentActionPoint.objects.select_related("document__review_batch", "topic", "status_by")
            .prefetch_related(Prefetch("cites", queryset=DocumentFinding.objects.select_related("document")))
            .get(pk=pk)
        )
        today = timezone.localdate()
        point.overdue = point.status == "open" and bool(point.deadline_date and point.deadline_date < today)
        cited = [f for f in point.cites.all() if f.verdict != Verdict.REJECTED]
        point.where = cited[0] if cited else None
        return render(
            request,
            "knowledge/review/_action_row.html",
            _row_context(request, p=point, statuses=DocumentActionPoint.Status.choices),
        )
    return redirect(_back(request, _page_url("actions")))


# ------------------------------------------------------------------------------------------ theme paragraph
@require_POST
def theme_paragraph(request: HttpRequest, pk: int) -> HttpResponse:
    """Write a paragraph on one theme (one AI call on its findings only; each person's daily quota)."""
    topic = get_object_or_404(Topic.objects.select_related("subtopic__programme"), pk=pk)
    result = review_paragraph.write(
        topic,
        request.user,
        review_data.batch_id(request.POST.get("batch")),
        _verified(request),
        request.POST.get("challenges") == "1",
    )
    used, allowed = review_paragraph.quota(request.user)
    context = {"result": result, "topic": topic, "quota": {"used": used, "allowed": allowed}}
    if request.htmx:
        return render(request, "knowledge/review/_paragraph.html", context)
    if result.ok:
        messages.success(request, result.text)
    else:
        messages.error(request, result.message)
    return redirect(_back(request, _page_url("synthesis")))
