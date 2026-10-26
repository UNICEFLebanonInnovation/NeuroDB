"""Compare with Power BI (Admin → Data and sync), for the people who check the figures
(``roles.can_validate``: superusers and the Validators group; the admin itself asks for staff status). Anyone
else gets a 404.

* ``datamart/powerbi/``: the upload form, how to export from Power BI, and the last uploads. A POST checks the
  files in the request (``powerbi_read.prepare``), keeps what NeuroDB compares and starts the job
  (``powerbi_compare``) in the background.
* ``datamart/powerbi/<pk>/``: one upload: what to do now (at most four lines), one line per Power BI page in
  Power BI's order with its verdict, what to do and each export, the choices for the user, the files not read
  and why. Reloads itself every 5 seconds while the job runs (no script).
* ``datamart/powerbi/<pk>/<export_pk>/``: one export: its filters clause by clause, its notes, its fit tables,
  its lines by group and reason, and the lines themselves (Power BI's value and NeuroDB's, the reason and what
  to do, "Check in NeuroDB"), needs-a-look first, 100 a page and at most 5,000.
* ``datamart/powerbi/<pk>/differences.xlsx``: every difference of the upload (``powerbi_results.workbook``),
  behind one of Raw eTools data's download slots.
* ``datamart/powerbi/<pk>/again/`` (POST): *Compare again*, the same rows against NeuroDB's current data.

The upload POST keeps the files in memory only (``powerbi_read.UploadHandler``): the view is CSRF-exempt so
the handler is set before anything reads the body, and its inner function checks the CSRF token itself.
Refusals are written in the response or kept on the upload, never in a message (a cookie or the session).
The pages show counts, Power BI's captions, NeuroDB's own sentences and the values of the lines: those of
the columns NeuroDB compares, never a person (the files' people columns were dropped as they were read, free
texts cleaned; NeuroDB's side passed ``powerbi_page.shown``).
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.contrib import admin
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from neurodb.accounts.roles import can_validate

from . import comparison, powerbi_compare, powerbi_filters, powerbi_read, powerbi_results
from . import powerbi_exports as px
from . import powerbi_words as words
from .models import PowerBIDifference, PowerBIExport, PowerBIUpload

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
    uploads = list(
        PowerBIUpload.objects.select_related("uploaded_by").order_by("-uploaded_at", "-id")[:HISTORY]
    )
    states: dict[int, Counter] = {u.pk: Counter() for u in uploads}
    found = (
        PowerBIExport.objects.filter(upload__in=uploads)
        .order_by()
        .values("upload_id", "state")
        .annotate(n=Count("id"))
    )
    for row in found:
        states[row["upload_id"]][row["state"]] = row["n"]
    context = _context(
        request,
        words.PAGE_TITLE,
        form=form,
        refused=refused or [],
        running=running,
        history=[_history_row(u, states[u.pk]) for u in uploads],
        visuals=[" → ".join(k.visual) for k in px.KINDS.values()],
        report=comparison.POWERBI_REPORT,
        steps=words.HOW_STEPS,
    )
    return render(request, "admin/datamart/powerbi/index.html", context, status=status)


def _history_row(upload: PowerBIUpload, states: Counter) -> dict[str, Any]:
    """An upload of the history: its exports read (none counted while it runs) and its files not read (refused
    in the request, or failed in the job)."""
    busy = upload.status in powerbi_compare.BUSY_STATUSES
    return {
        "upload": upload,
        "url": reverse("admin:datamart_powerbi_upload", args=[upload.pk]),
        "status": words.UPLOAD_STATUS.get(upload.status, upload.status),
        "exports": None if busy else sum(states[s] for s in powerbi_compare.READ_STATES),
        "not_read": len(upload.refused or []) + states["failed"],
        # the lines that need a look (a comparison done)
        "look": (upload.summary or {}).get("look") if upload.status == PowerBIUpload.Status.DONE else None,
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


def _to_clear(clause: powerbi_filters.Clause) -> str:
    """A filter NeuroDB cannot apply, as the user finds it on the report: a slicer by its name, another field
    as the Filters pane shows it (Power BI's field names stay on the export's page)."""
    slicer = comparison.POWERBI_FIELD_SLICERS.get(clause.field)
    if slicer:
        return words.TODO_SLICER.format(slicer=slicer)
    return words.TODO_PANE.format(field=clause.field) if clause.field else words.TODO_UNREAD


def _export_row(upload: PowerBIUpload, export: PowerBIExport) -> dict[str, Any]:
    kind = px.KINDS.get(export.kind)
    parsed = powerbi_filters.Filter.from_json(export.filters)
    # a failed upload's copies are emptied: an export still waiting will never be read
    not_read = export.state == "staged" and upload.status == PowerBIUpload.Status.FAILED
    if not_read:
        state, action = words.NOT_READ_STATE, words.NOT_READ_ACTION
    elif export.state in ("compared", "empty", "not_compared", "failed"):
        state, action = powerbi_results.verdict_words(export)
    else:
        state = words.STATE_WORDS.get(export.state, export.state)
        action = export.error or words.STATE_ACTIONS.get(export.state, "")
    counts = export.counts or {}
    look = powerbi_results.groups(export).get(4, 0)
    return {
        "export": export,
        "visual": kind.visual[1] if kind else export.kind,
        "state": state,
        "action": action,
        "covers": _covers(parsed) if export.state not in ("failed", "staged") else "",  # its filter read
        "cannot": [_to_clear(c) for c in parsed.clauses if c.state in CANNOT],
        "url": reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk]),
        "failed": export.state == "failed",
        "warn": export.state == "failed" or not_read or bool(look),
        "counts": _counts_words(export) if export.state == "compared" else "",
        "set_aside": words.EXPORT_SET_ASIDE.format(n=export.set_aside) if export.set_aside else "",
        "look": look,
        "look_url": powerbi_results.export_url(upload, export, group=4)
        if powerbi_results.written_groups(export).get(4)
        else "",
        "same": counts.get("same", 0) + counts.get("same_zero", 0),
    }


def _counts_words(export: PowerBIExport) -> str:
    counts = export.counts or {}
    return words.EXPORT_COUNTS.format(
        rows=counts.get("rows", 0),
        same=counts.get("same", 0) + counts.get("same_zero", 0),
        different=counts.get("different", 0),
        only_powerbi=counts.get("only_powerbi", 0),
        only_neurodb=counts.get("only_neurodb", 0),
    )


def _pages(upload: PowerBIUpload, exports: list[PowerBIExport]) -> tuple[list[dict[str, Any]], int]:
    """One line per Power BI page, in Power BI's order: its verdict (its worst export's) and what to do, its
    needs-a-look lines, and its exports (or its visuals not uploaded)."""
    pages, missing = [], 0
    for page in powerbi_results.page_lines(upload, exports):
        lines = []
        for kind in px.KINDS.values():
            if kind.visual[0] != page["name"]:
                continue
            found = [e for e in page["exports"] if e.kind == kind.key]
            if not found:
                missing += 1
                lines.append({"visual": kind.visual[1], "missing": True})
            lines.extend(_export_row(upload, e) for e in found)
        pages.append({**page, "lines": lines})
    return pages, missing


def _before(upload: PowerBIUpload, pages: list[dict[str, Any]], missing: int) -> list[str]:
    """What to do now about the files themselves: those not read, the visuals not uploaded, the filters to
    clear."""
    lines = []
    not_read = len(upload.refused or []) + sum(1 for p in pages for line in p["lines"] if line.get("failed"))
    if not_read:
        lines.append(words.plural(not_read, words.TODO_NOT_READ_ONE, words.TODO_NOT_READ))
    if missing:
        lines.append(words.plural(missing, words.TODO_MISSING_ONE, words.TODO_MISSING))
    cannot = list(dict.fromkeys(c for p in pages for line in p["lines"] for c in line.get("cannot", ())))
    if cannot:
        lines.append(words.TODO_CANNOT.format(clauses="; ".join(cannot[:TODO_CLAUSES])))
    return lines


def upload_view(request, pk: int):
    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload.objects.select_related("uploaded_by"), pk=pk)
    return _upload_page(request, upload)


def _status_words(upload: PowerBIUpload, exports: list[PowerBIExport]) -> str:
    words_ = words.UPLOAD_STATUS.get(upload.status, upload.status)
    if upload.status == PowerBIUpload.Status.COMPARING:
        done = (upload.summary or {}).get("compared", 0)
        words_ = words.COMPARING.format(n=done, of=len(exports))
    return words_


def _upload_page(request, upload: PowerBIUpload, refused: list[str] | None = None, status: int = 200):
    exports = list(powerbi_compare.exports_of(upload))
    pages, missing = _pages(upload, exports)
    summary = upload.summary or {}
    hidden = summary.get("hidden") or 0
    busy = upload.status in powerbi_compare.BUSY_STATUSES
    done = upload.status == PowerBIUpload.Status.DONE
    written = any((e.counts or {}).get("written") for e in exports)
    context = _context(
        request,
        words.PAGE_TITLE,
        upload=upload,
        busy=busy,
        status_words=_status_words(upload, exports),
        pages=pages,
        todo=[] if busy else powerbi_results.todo(upload, exports, _before(upload, pages, missing)),
        choices=upload.choices if done else [],
        # the Reached sentence alone when no choice carries it (no rule fits)
        reached=summary.get("reached", "")
        if done and not any(c.get("code") == "reached" for c in upload.choices or [])
        else "",
        refused=refused or [],
        not_read=upload.refused or [],
        hidden=words.plural(hidden, words.HIDDEN_ONE, words.HIDDEN) if hidden else "",
        dropped=list(dict.fromkeys(s for e in exports for s in e.dropped)),
        notes=summary.get("notes") or [],
        sync_moved=words.SYNC_MOVED if upload.sync_moved else "",
        trimmed=words.DETAILS_REMOVED if upload.trimmed else "",
        again=powerbi_compare.can_compare_again(upload),
        again_url=reverse("admin:datamart_powerbi_again", args=[upload.pk]),
        xlsx_url=reverse("admin:datamart_powerbi_xlsx", args=[upload.pk]) if done and written else "",
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
def _history(upload: PowerBIUpload) -> str:
    from . import reached

    day = upload.day or timezone.localdate()
    found = reached.history_from(day).date
    return f"{found.day} {found:%b %Y}"


def export_view(request, pk: int, export_pk: int):
    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload, pk=pk)
    export = get_object_or_404(PowerBIExport.objects.defer("content", "rows"), pk=export_pk, upload_id=pk)
    kind = px.KINDS.get(export.kind)
    captions = {c.name: c.caption for c in kind.columns} if kind else {}
    row = _export_row(upload, export)
    history = _history(upload)
    counts = export.counts or {}
    chosen = {
        "reason": request.GET.get("reason", ""),
        "kind": request.GET.get("kind", ""),
        "group": request.GET.get("group", ""),
    }
    lines = PowerBIDifference.objects.filter(export=export).order_by("n")
    if chosen["reason"] in words.REASON_WORDS:
        lines = lines.filter(reason=chosen["reason"])
    if chosen["kind"] in words.LINE_KINDS:
        lines = lines.filter(kind=chosen["kind"])
    if chosen["group"] in {str(g) for g in words.GROUP_NAMES}:
        lines = lines.filter(group=int(chosen["group"]))
    on_screen = lines.only("n", "group", "kind", "reason", "rule", "detail", "match", "found", "link")
    paginator = Paginator(on_screen[: powerbi_results.ON_SCREEN], powerbi_results.PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    query = {k: v for k, v in chosen.items() if v}
    context = _context(
        request,
        words.PAGE_TITLE,
        upload=upload,
        export=export,
        row=row,
        page_name=kind.visual[0] if kind else "",
        neurodb_page=words.NEURODB_PAGES.get(export.kind, ""),
        neurodb_url=powerbi_results.neurodb_url(export),
        upload_url=reverse("admin:datamart_powerbi_upload", args=[upload.pk]),
        export_url=reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk]),
        day=comparison._day(upload.day) if upload.day else "",
        clauses=[
            {"words": c["words"], "state": words.CLAUSE_STATES.get(c["state"], c["state"])}
            for c in export.clauses
        ],
        kept=[captions[name] for name in export.columns if name in captions],
        not_compared=words.NOT_COMPARED_COLUMNS.format(columns=", ".join(_not_compared(export)))
        if export.not_compared or export.unknown_columns
        else "",
        notes=[*export.notes, *(counts.get("notes") or [])],
        fits=powerbi_results.fit_tables(export, history),
        by_reason=_by_reason(upload, export),
        chosen=chosen,
        lines=[powerbi_results.line_view(export, line, history) for line in page.object_list],
        page=page,
        query=urlencode(query),
        match=powerbi_results.match_captions(export),
        on_screen=words.NOTE_ON_SCREEN.format(n=powerbi_results.ON_SCREEN)
        if counts.get("written", 0) > powerbi_results.ON_SCREEN
        else "",
        empty=words.EXPORT_EMPTY.format(n=counts.get("neurodb_rows", 0)) if export.state == "empty" else "",
        kinds=words.LINE_KINDS,
        reasons={r: powerbi_results.reason_words(r, history) for r in words.REASON_ORDER},
        group_names=words.GROUP_NAMES,
    )
    return render(request, "admin/datamart/powerbi/export.html", context)


def _by_reason(upload: PowerBIUpload, export: PowerBIExport) -> dict[str, Any]:
    """The lines by group and reason, needs a look first; the expected ones on one line of counts."""
    history = _history(upload)
    found = (
        PowerBIDifference.objects.filter(export=export)
        .order_by()
        .values("group", "reason")
        .annotate(n=Count("id"))
    )
    rows = sorted(found, key=lambda r: (-r["group"], -r["n"], r["reason"]))
    needs = []
    expected = []
    for r in rows:
        item = {
            "group": words.GROUP_NAMES.get(r["group"], ""),
            "reason": powerbi_results.reason_words(r["reason"], history),
            "n": r["n"],
            "url": powerbi_results.export_url(upload, export, reason=r["reason"], group=r["group"]),
            "todo": words.GROUP_ACTIONS.get(r["group"], ""),
        }
        (expected if r["group"] == 1 else needs).append(item)
    return {"needs": needs, "expected": expected}


def _not_compared(export: PowerBIExport) -> list[str]:
    found = list(export.not_compared)
    if export.unknown_columns:
        found.append(
            words.plural(export.unknown_columns, words.NOT_COMPARED_OTHER, words.NOT_COMPARED_OTHERS)
        )
    return found


# ---------------------------------------------------------------------------------------------- the Excel
def xlsx_view(request, pk: int):
    """Every difference of an upload as an Excel file (``powerbi_results.workbook``), behind one of Raw eTools
    data's download slots (503 while every one is taken), freed when the file is written."""
    from neurodb.reports import exports as export_files

    from . import raw

    _guard(request)
    powerbi_compare.close_stale()
    upload = get_object_or_404(PowerBIUpload, pk=pk)
    found = list(powerbi_compare.exports_of(upload))
    if upload.status != PowerBIUpload.Status.DONE or not found:
        return HttpResponse(words.XLSX_NONE, status=404, content_type="text/plain; charset=utf-8")
    slot = raw.take_slot()
    if slot is None:
        return HttpResponse(words.XLSX_BUSY, status=503, content_type="text/plain; charset=utf-8")
    try:
        data = powerbi_results.workbook(upload, found, _history(upload), request.build_absolute_uri("/")[:-1])
    finally:
        slot.free()
    name = export_files.export_filename("powerbi-comparison", "xlsx", moment=upload.compared_at)
    response = HttpResponse(data, content_type=export_files.XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
