"""The knowledge base pages (/knowledge/): search and list, add a document or a text, a document's
page (summary, links, text) and its file."""

from __future__ import annotations

import logging
import os

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.accounts.models import Section

from . import search
from .access import can_add, can_manage
from .forms import DocumentForm
from .models import Document, Link, ReportSeries, ReviewBatch

logger = logging.getLogger(__name__)

TEXT_PREVIEW = 20000
STORAGE_MESSAGE = gettext_lazy(
    "The file could not be stored: NeuroDB's file storage is not set up. An administrator sets the "
    "storage account (AZURE_STORAGE_ACCOUNT, see the deployment guide); nothing was added."
)


def _storage_errors() -> tuple[type[BaseException], ...]:
    try:
        from azure.core.exceptions import AzureError
    except ImportError:  # pragma: no cover - the Azure SDK is installed with django-storages[azure]
        return (OSError,)
    return (OSError, AzureError)


STORAGE_ERRORS = _storage_errors()


def start(document: Document) -> None:
    from neurodb.integrations import background

    background.start_command("index_knowledge", "--document", str(document.pk))


def save_added(form: DocumentForm, user) -> list[Document]:
    """One document per chosen file (named after it when several are chosen), or the pasted text.
    An edition of a periodic report gets its series, number and date from its name at once, so that
    the editions are read oldest first."""
    from . import periodic

    data = form.cleaned_data
    files = data.get("files") or [None]
    title = (data.get("title") or "").strip()
    out = []
    for upload in files:
        document = Document(
            title=title,
            text=data.get("text") or "",
            source=data.get("source") or "",
            section=data.get("section"),
            year=data.get("year"),
            periodic=bool(data.get("periodic")),
            added_by=user,
        )
        if upload is not None:
            document.text = ""
            document.file = upload
            stem = os.path.splitext(os.path.basename(upload.name))[0].replace("_", " ").strip()
            document.title = (title if len(files) == 1 and title else stem)[:300]
        if not document.periodic and periodic.known_series(document):
            document.periodic = True  # another edition of a periodic report already here
        if document.periodic:
            periodic.assign(document)
        document.save()
        out.append(document)
    return out


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()[:200]
    filters = search.Filters(section_id=_int(request.GET.get("section")), year=_int(request.GET.get("year")))
    results = search.documents_matching(q, filters) if q else None
    documents = None
    if results is None:
        qs = filters.documents() if (filters.section_id or filters.year) else Document.objects.all()
        documents = Paginator(qs.select_related("added_by").prefetch_related("links"), 25).get_page(
            request.GET.get("page")
        )
    context = {
        "page_title": _("Knowledge base"),
        "page_subtitle": _(
            "Documents and notes added for Ask NeuroDB: searchable, linked to the partners, programme "
            "documents, sections and places they mention"
        ),
        "breadcrumbs": [{"label": _("Knowledge base"), "url": None}],
        "q": q,
        "results": results,
        "documents": documents,
        "sections": Section.objects.order_by("name"),
        "filters": filters,
        "can_add": can_add(request.user),
        "add_url": reverse("knowledge:add"),
    }
    return render(request, "knowledge/index.html", context)


@require_http_methods(["GET", "POST"])
def add(request: HttpRequest) -> HttpResponse:
    if not can_add(request.user):
        raise PermissionDenied
    form = DocumentForm(request.POST or None, request.FILES or None)
    # uploaded from the document review: the documents go in that review batch
    batch = ReviewBatch.objects.filter(pk=_int(request.POST.get("batch") or request.GET.get("batch"))).first()
    documents = []
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():  # all the files or none
                documents = save_added(form, request.user)
                if batch is not None:
                    from . import review

                    review.put_in_batch(documents, batch)
        except STORAGE_ERRORS:
            logger.exception("knowledge base: the uploaded file(s) could not be stored")
            form.add_error(None, STORAGE_MESSAGE)
    if documents and batch is not None:
        if len(documents) == 1:
            start(documents[0])
        else:
            from neurodb.integrations import background

            background.start_command("index_knowledge", "--pending")
        messages.success(
            request,
            _(
                "%(n)s document(s) added to the batch “%(batch)s”. NeuroDB reads them first (a few minutes "
                "each); they are analysed at the next nightly run, or now with Analyse once read."
            )
            % {"n": len(documents), "batch": batch.name},
        )
        return redirect(f"{reverse('knowledge:review')}?tab=documents&batch={batch.pk}")
    if documents:
        if len(documents) == 1:
            start(documents[0])
            messages.success(
                request,
                _(
                    "Added. NeuroDB is reading it (a few seconds to a few minutes); "
                    "it can be asked about once ready."
                ),
            )
            return redirect(documents[0])
        from neurodb.integrations import background

        background.start_command("index_knowledge", "--pending")
        messages.success(
            request,
            _(
                "%(n)s documents added. NeuroDB reads them one after the other, editions of a periodic "
                "report oldest first; this can take a few minutes per document."
            )
            % {"n": len(documents)},
        )
        series = {d.series_id for d in documents}
        if len(series) == 1 and None not in series:
            return redirect("knowledge:series", pk=series.pop())
        return redirect("knowledge:index")
    context = {
        "page_title": _("Add to the knowledge base"),
        "breadcrumbs": [
            {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
            {"label": _("Add"), "url": None},
        ],
        "form": form,
        "batch": batch,
    }
    return render(request, "knowledge/add.html", context)


@require_GET
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document.objects.select_related("added_by", "section"), pk=pk)
    links = list(document.links.all())
    groups: dict[str, list[Link]] = {}
    for link in links:
        groups.setdefault(link.get_kind_display(), []).append(link)
    text = document.text.replace("\f", "\n\n")
    context = {
        "page_title": document.title,
        "breadcrumbs": [
            {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
            {"label": document.title[:60], "url": None},
        ],
        "document": document,
        "link_groups": groups,
        "text": text[:TEXT_PREVIEW],
        "text_cut": len(text) > TEXT_PREVIEW,
        "chunks": document.chunks.count(),
        "can_manage": can_manage(request.user, document),
        "working": document.status in (Document.Status.PENDING, Document.Status.INDEXING),
        "figures": document.figures.count() if document.periodic else 0,
    }
    return render(request, "knowledge/detail.html", context)


@require_GET
def series_index(request: HttpRequest) -> HttpResponse:
    from django.db.models import Count, Max, Min, Q

    ready = Q(editions__status=Document.Status.READY)
    rows = ReportSeries.objects.annotate(
        n_editions=Count("editions", filter=ready, distinct=True),
        first_issue=Min("editions__issued_on", filter=ready),
        last_issue=Max("editions__issued_on", filter=ready),
        n_figures=Count("figures", distinct=True),
    ).order_by("-last_issue", "name")
    context = {
        "page_title": _("Periodic reports"),
        "page_subtitle": _(
            "Reports issued again and again (snapshots, situation reports): their figures are kept by date, "
            "so Ask NeuroDB can compare editions, follow trends and draw charts"
        ),
        "breadcrumbs": [
            {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
            {"label": _("Periodic reports"), "url": None},
        ],
        "rows": rows,
        "can_add": can_add(request.user),
    }
    return render(request, "knowledge/series_index.html", context)


@require_GET
def series_detail(request: HttpRequest, pk: int) -> HttpResponse:
    from django.db.models import Count

    from . import periodic

    series = get_object_or_404(ReportSeries, pk=pk)
    found = periodic.overview(series)
    editions = list(
        series.editions.order_by("-issued_on", "-edition", "-pk").annotate(n_figures=Count("figures"))
    )
    trend = [
        {
            "id": m["key"],
            "label": " · ".join(
                p
                for p in (
                    "" if m["group"].lower() in m["metric"].lower() else m["group"],
                    m["metric"],
                    m["breakdown"],
                )
                if p
            ),
            "unit": "%" if m["is_percent"] else m["unit"],
            "dates": [p["as_of"].isoformat() for p in m["timeline"]],
            "values": [float(p["value"]) for p in m["timeline"]],
        }
        for m in found
        if len(m["timeline"]) >= 2
    ]
    default = max(trend, key=lambda t: len(t["dates"]))["id"] if trend else None
    context = {
        "page_title": series.name,
        "page_subtitle": _("Periodic report: its figures by date, the newest edition counting for each date"),
        "breadcrumbs": [
            {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
            {"label": _("Periodic reports"), "url": reverse("knowledge:series_index")},
            {"label": series.name[:60], "url": None},
        ],
        "series": series,
        "measures": found,
        "editions": editions,
        "working": any(e.status in (Document.Status.PENDING, Document.Status.INDEXING) for e in editions),
        "chart_data": {"trend": {"series": trend, "default": default}},
        "can_add": can_add(request.user),
    }
    return render(request, "knowledge/series_detail.html", context)


@require_GET
def download(request: HttpRequest, pk: int) -> FileResponse:
    document = get_object_or_404(Document, pk=pk)
    if not document.file:
        raise Http404
    try:
        handle = document.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404(_("The file is missing from storage.")) from exc
    # a PDF opens in the browser, so that a finding's link (#page=n) opens it at its page
    inline = document.filename.lower().endswith(".pdf")
    return FileResponse(handle, as_attachment=not inline, filename=document.filename)


@require_POST
def reindex(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document, pk=pk)
    if not can_manage(request.user, document):
        raise PermissionDenied
    document.status, document.error = Document.Status.PENDING, ""
    document.save(update_fields=["status", "error", "updated_at"])
    start(document)
    messages.success(request, _("Reading it again."))
    return redirect(document)


@require_POST
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    document = get_object_or_404(Document, pk=pk)
    if not can_manage(request.user, document) or document.origin != Document.Origin.ADDED:
        raise PermissionDenied  # a publication or CPD document goes with its source
    title = document.title
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, _("“%(title)s” was removed from the knowledge base.") % {"title": title})
    return redirect("knowledge:index")
