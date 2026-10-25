"""Compare with Power BI (Admin → Data and sync), for the people who check the figures
(``roles.can_validate``: superusers and the Validators group; the admin itself asks for staff status). Anyone
else gets a 404.

* ``datamart/powerbi/``: the upload form, how to export from Power BI, and the last uploads. A POST checks the
  files in the request (``powerbi_read.prepare``), keeps what NeuroDB compares and starts the job
  (``powerbi_compare``) in the background.
* ``datamart/powerbi/<pk>/``: one upload: what to do now, one line per Power BI page in Power BI's order with
  each export read, the files not read and why. Reloads itself every 5 seconds while the job runs (no script).
* ``datamart/powerbi/<pk>/<export_pk>/``: one export: its filters clause by clause, its columns, its notes.
* ``datamart/powerbi/<pk>/again/`` (POST): *Compare again*, the same rows against NeuroDB's current data.

The upload POST keeps the files in memory only (``powerbi_read.UploadHandler``): the view is CSRF-exempt so
the handler is set before anything reads the body, and its inner function checks the CSRF token itself.
Refusals are written in the response or kept on the upload, never in a message (a cookie or the session).
Nothing here shows a value from the files: counts, Power BI's captions and NeuroDB's own sentences.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from neurodb.accounts.roles import can_validate

from . import comparison, powerbi_compare, powerbi_filters, powerbi_read
from . import powerbi_exports as px
from . import powerbi_words as words
from .models import PowerBIExport, PowerBIUpload

HISTORY = 20  # uploads the first page lists
CANNOT = ("cannot", "unknown_field", "unknown_value", "unread")
TODO_CLAUSES = 4  # clauses "what to do now" names


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, list | tuple):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


class PowerBIUploadForm(forms.Form):
    files = MultipleFileField(
        required=False,
        label=words.FILES_LABEL,
        help_text=words.FILES_HELP,
        widget=MultipleFileInput(attrs={"accept": ".zip,.xlsx"}),
    )


def _guard(request) -> None:
    if not can_validate(request.user):
        raise Http404


def _context(request, title: str, **extra: Any) -> dict[str, Any]:
    return {
        **admin.site.each_context(request),
        "title": title,
        "subtitle": None,
        "index_url": reverse("admin:datamart_powerbi"),
        "words": words,
        # no "opts": the header is the page's title, never a link to the developer tables
        **extra,
    }


# ------------------------------------------------------------------------------------------- the first page
@csrf_exempt
def index_view(request):
    """The form, how to export and the last uploads; a POST is an upload (its files kept in memory only)."""
    _guard(request)
    if request.method == "POST":
        handler = powerbi_read.UploadHandler(request)
        request.upload_handlers = [handler]  # before anything reads the body
        return _upload(request, handler)
    powerbi_compare.close_stale()
    return _index(request, PowerBIUploadForm())


def _index(request, form: PowerBIUploadForm, refused: list[str] | None = None, status: int = 200):
    running = powerbi_compare.busy()
    uploads = PowerBIUpload.objects.select_related("uploaded_by").order_by("-uploaded_at", "-id")[:HISTORY]
    context = _context(
        request,
        words.PAGE_TITLE,
        form=form,
        refused=refused or [],
        running=running,
        history=[_history_row(u) for u in uploads],
        visuals=[" → ".join(k.visual) for k in px.KINDS.values()],
        report=comparison.POWERBI_REPORT,
        steps=words.HOW_STEPS,
    )
    return render(request, "admin/datamart/powerbi/index.html", context, status=status)


def _history_row(upload: PowerBIUpload) -> dict[str, Any]:
    summary = upload.summary or {}
    return {
        "upload": upload,
        "url": reverse("admin:datamart_powerbi_upload", args=[upload.pk]),
        "status": words.UPLOAD_STATUS.get(upload.status, upload.status),
        "exports": summary.get("exports"),
        "not_read": len(upload.refused or []),
    }


@csrf_protect
def _upload(request, handler: powerbi_read.UploadHandler):
    from neurodb.watch import people

    form = PowerBIUploadForm(request.POST, request.FILES)
    running = powerbi_compare.busy()
    if running is not None:
        return _index(request, PowerBIUploadForm(), [words.BUSY])
    if powerbi_compare.heavy_job_running():
        return _index(request, PowerBIUploadForm(), [words.OTHER_JOB])
    if handler.too_big_upload:
        return _index(request, PowerBIUploadForm(), [words.TOO_BIG_UPLOAD, words.NOTHING_KEPT])
    files = request.FILES.getlist("files")
    names = people.known_names()
    refused = [words.TOO_BIG.format(name=name) for name in handler.too_big]
    if not files and not refused:
        return _index(request, form, [words.NO_FILE])
    prepared = powerbi_read.prepare(files, names, powerbi_compare.known_sections())
    prepared.refused[:0] = refused
    if not prepared.exports:
        hidden = [words.plural(prepared.hidden, words.HIDDEN_ONE, words.HIDDEN)] if prepared.hidden else []
        return _index(request, PowerBIUploadForm(), [*prepared.refused, *hidden, words.NOTHING_KEPT])
    upload = powerbi_compare.create(prepared, request.user)
    powerbi_compare.start(upload, request.user)
    return redirect("admin:datamart_powerbi_upload", upload.pk)


# ------------------------------------------------------------------------------------------------ one upload
def _covers(parsed: powerbi_filters.Filter) -> str:
    """What an export's filter covers, in plain words: "2026", "Every year", "Education only"."""
    parts = [", ".join(str(y) for y in parsed.years) if parsed.years else words.COVERS_EVERY_YEAR]
    if parsed.sections:
        parts.append(words.COVERS_ONLY.format(sections=", ".join(parsed.sections)))
    return " · ".join(parts)


def _export_row(upload: PowerBIUpload, export: PowerBIExport) -> dict[str, Any]:
    kind = px.KINDS.get(export.kind)
    parsed = powerbi_filters.Filter.from_json(export.filters)
    return {
        "export": export,
        "visual": kind.visual[1] if kind else export.kind,
        "state": words.STATE_WORDS.get(export.state, export.state),
        "action": export.error or words.STATE_ACTIONS.get(export.state, ""),
        "covers": _covers(parsed) if export.state != "failed" else "",
        "cannot": [
            c["words"] or words.CLAUSE_STATES["unread"] for c in export.clauses if c["state"] in CANNOT
        ],
        "url": reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk]),
        "failed": export.state == "failed",
    }


def _pages(upload: PowerBIUpload, exports: list[PowerBIExport]) -> tuple[list[dict[str, Any]], int]:
    """One line per Power BI page, in Power BI's order, with its exports (or its visuals not uploaded)."""
    by_kind: dict[str, list[PowerBIExport]] = {}
    for export in exports:
        by_kind.setdefault(export.kind, []).append(export)
    pages, missing = [], 0
    for name in comparison.POWERBI_PAGE_ORDER:
        lines = []
        for kind in px.KINDS.values():
            if kind.visual[0] != name:
                continue
            found = by_kind.get(kind.key, [])
            if not found:
                missing += 1
                lines.append({"visual": kind.visual[1], "missing": True})
            lines.extend(_export_row(upload, e) for e in found)
        pages.append({"name": name, "lines": lines})
    return pages, missing


def _todo(upload: PowerBIUpload, pages: list[dict[str, Any]], missing: int) -> list[str]:
    """What to do now (at most four lines)."""
    lines = []
    not_read = len(upload.refused or []) + sum(1 for p in pages for line in p["lines"] if line.get("failed"))
    if not_read:
        lines.append(words.plural(not_read, words.TODO_NOT_READ_ONE, words.TODO_NOT_READ))
    if missing:
        lines.append(words.plural(missing, words.TODO_MISSING_ONE, words.TODO_MISSING))
    cannot = list(dict.fromkeys(c for p in pages for line in p["lines"] for c in line.get("cannot", ())))
    if cannot:
        lines.append(words.TODO_CANNOT.format(clauses="; ".join(cannot[:TODO_CLAUSES])))
    return lines[:4] or [words.TODO_NOTHING]


def upload_view(request, pk: int):
    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload.objects.select_related("uploaded_by"), pk=pk)
    return _upload_page(request, upload)


def _upload_page(request, upload: PowerBIUpload, refused: list[str] | None = None, status: int = 200):
    exports = list(powerbi_compare.exports_of(upload))
    pages, missing = _pages(upload, exports)
    summary = upload.summary or {}
    hidden = summary.get("hidden") or 0
    busy = upload.status in powerbi_compare.BUSY_STATUSES
    context = _context(
        request,
        words.PAGE_TITLE,
        upload=upload,
        busy=busy,
        status_words=words.UPLOAD_STATUS.get(upload.status, upload.status),
        pages=pages,
        todo=[] if busy else _todo(upload, pages, missing),
        refused=refused or [],
        not_read=upload.refused or [],
        hidden=words.plural(hidden, words.HIDDEN_ONE, words.HIDDEN) if hidden else "",
        dropped=list(dict.fromkeys(s for e in exports for s in e.dropped)),
        notes=summary.get("notes") or [],
        sync_moved=words.SYNC_MOVED if upload.sync_moved else "",
        trimmed=words.DETAILS_REMOVED if upload.trimmed else "",
        again=powerbi_compare.can_compare_again(upload),
        again_url=reverse("admin:datamart_powerbi_again", args=[upload.pk]),
        day=comparison._day(upload.day) if upload.day else "",
        refreshed=comparison._day(upload.powerbi_as_of) if upload.powerbi_as_of else "",
        synced=comparison._day(upload.synced_at) if upload.synced_at else "",
    )
    return render(request, "admin/datamart/powerbi/upload.html", context, status=status)


@require_POST
def again_view(request, pk: int):
    """*Compare again*: refused while a comparison, a Datamart sync or a KM Database load runs."""
    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload.objects.select_related("uploaded_by"), pk=pk)
    if powerbi_compare.busy() is not None:
        return _upload_page(request, upload, [words.BUSY])
    if powerbi_compare.heavy_job_running():
        return _upload_page(request, upload, [words.OTHER_JOB])
    if not powerbi_compare.again(upload):
        return _upload_page(request, upload, [words.NOT_AGAIN])
    upload.refresh_from_db()
    powerbi_compare.start(upload, request.user)
    return redirect("admin:datamart_powerbi_upload", upload.pk)


# ------------------------------------------------------------------------------------------------ one export
def export_view(request, pk: int, export_pk: int):
    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload, pk=pk)
    export = get_object_or_404(PowerBIExport.objects.defer("content", "rows"), pk=export_pk, upload_id=pk)
    kind = px.KINDS.get(export.kind)
    captions = {c.name: c.caption for c in kind.columns} if kind else {}
    row = _export_row(upload, export)
    context = _context(
        request,
        words.PAGE_TITLE,
        upload=upload,
        export=export,
        row=row,
        page_name=kind.visual[0] if kind else "",
        upload_url=reverse("admin:datamart_powerbi_upload", args=[upload.pk]),
        clauses=[
            {"words": c["words"], "state": words.CLAUSE_STATES.get(c["state"], c["state"])}
            for c in export.clauses
        ],
        kept=[captions[name] for name in export.columns if name in captions],
        not_compared=words.NOT_COMPARED_COLUMNS.format(columns=", ".join(_not_compared(export)))
        if export.not_compared or export.unknown_columns
        else "",
    )
    return render(request, "admin/datamart/powerbi/export.html", context)


def _not_compared(export: PowerBIExport) -> list[str]:
    found = list(export.not_compared)
    if export.unknown_columns:
        found.append(
            words.plural(export.unknown_columns, words.NOT_COMPARED_OTHER, words.NOT_COMPARED_OTHERS)
        )
    return found
