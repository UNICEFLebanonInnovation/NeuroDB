"""The knowledge base pages (/knowledge/): search and list, add a document or a text, a document's
page (summary, links, text) and its file."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from neurodb.accounts.models import Section

from . import search
from .access import can_add, can_manage
from .forms import DocumentForm
from .models import Document, Link

TEXT_PREVIEW = 20000


def start(document: Document) -> None:
    from neurodb.integrations import background

    background.start_command("index_knowledge", "--document", str(document.pk))


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
    if request.method == "POST" and form.is_valid():
        document = form.save(commit=False)
        document.added_by = request.user
        document.save()
        start(document)
        messages.success(
            request,
            _(
                "Added. NeuroDB is reading it (a few seconds to a few minutes); "
                "it can be asked about once ready."
            ),
        )
        return redirect(document)
    context = {
        "page_title": _("Add to the knowledge base"),
        "breadcrumbs": [
            {"label": _("Knowledge base"), "url": reverse("knowledge:index")},
            {"label": _("Add"), "url": None},
        ],
        "form": form,
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
    }
    return render(request, "knowledge/detail.html", context)


@require_GET
def download(request: HttpRequest, pk: int) -> FileResponse:
    document = get_object_or_404(Document, pk=pk)
    if not document.file:
        raise Http404
    try:
        handle = document.file.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404(_("The file is missing from storage.")) from exc
    return FileResponse(handle, as_attachment=True, filename=document.filename)


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
    if not can_manage(request.user, document):
        raise PermissionDenied
    title = document.title
    if document.file:
        document.file.delete(save=False)
    document.delete()
    messages.success(request, _("“%(title)s” was removed from the knowledge base.") % {"title": title})
    return redirect("knowledge:index")
