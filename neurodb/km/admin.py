"""The KM Database in the admin, for the people who set NeuroDB up (``roles.can_set_up``: superusers and
Administrators with staff status): its versions, with the page that uploads a new one and the Excel template,
and its documents (read only). The findings have no admin list: the KM Database page shows them.

Administrators hold every model permission (``roles.ensure_groups``), so each admin here checks
``can_set_up`` itself.
"""

from __future__ import annotations

import hashlib
import re

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import formats
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from neurodb.accounts.roles import can_set_up
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import columns as cols
from . import loading, words, workbook
from .forms import UploadForm
from .models import KMDocument, KMVersion
from .words import quoted, say

Status = KMVersion.Status
TONES = {Status.LIVE: "ok", Status.QUEUED: "info", Status.LOADING: "info", Status.FAILED: "bad"}
RECENT = 5  # versions the upload page lists
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
VERSION_FIELDS = (
    "status_shown",
    "version_label",
    "file_name",
    "file_size",
    "uploaded_by",
    "uploaded_at",
    "started_at",
    "finished_at",
    "prepared_on",
    "source_name",
    "documents_count",
    "findings_count",
    "stated_documents",
    "stated_findings",
    "review_count",
    "check_count",
    "fewer_ok",
    "peak_memory_mb",
    "load_seconds",
    "error",
)


def _review_url() -> str:
    return f"{reverse('km:index')}?quality=review"


def lines(sentences) -> list:
    """The version's problems or warnings as the page shows them: a sentence that ends with "See them on
    the page" links the findings marked for review or with a value to check."""
    shown = []
    for sentence in sentences or ():
        text = str(sentence)
        tail = next((str(t) for t in words.SEE_ON_PAGE if text.endswith(str(t))), "")
        if tail:
            shown.append(
                format_html('{} <a href="{}">{}</a>', text[: -len(tail)].rstrip(), _review_url(), tail)
            )
        else:
            shown.append(text)
    return shown


@admin.register(KMVersion)
class KMVersionAdmin(ModelAdmin):
    """The versions of the KM Database: the one shown now, the ones it replaced (their summary only), the
    ones that did not load (with why). A version is never changed here; a new workbook is uploaded instead."""

    change_form_template = "km/admin/version_change_form.html"
    list_display = (
        "status_shown",
        "prepared_on",
        "findings_count",
        "documents_count",
        "problems_shown",
        "uploaded_by",
        "uploaded_at",
    )
    list_select_related = ("uploaded_by",)
    list_filter = ("status",)
    actions_list = ("upload",)
    fields = VERSION_FIELDS
    readonly_fields = VERSION_FIELDS

    def get_queryset(self, request):
        return super().get_queryset(request).defer("content")

    def has_module_permission(self, request):
        return can_set_up(request.user)

    def has_view_permission(self, request, obj=None):
        return can_set_up(request.user)

    def has_add_permission(self, request):
        return False  # the upload page replaces the add form

    def has_change_permission(self, request, obj=None):
        return False  # read only: a version is replaced by uploading another

    def has_delete_permission(self, request, obj=None):
        # a superuser removes a version that is not shown and not loading, one at a time from its page
        return bool(
            request.user.is_superuser and obj is not None and obj.status in (Status.FAILED, Status.REPLACED)
        )

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # one at a time, from the version's page
        return actions

    @admin.display(description=_("Status"), ordering="status")
    def status_shown(self, obj):
        return badge(obj.get_status_display(), TONES.get(obj.status, "muted"))

    @admin.display(description=_("Problems"))
    def problems_shown(self, obj):
        return len(obj.problems or []) or "—"

    @admin.display(description=_("Version"))
    def version_label(self, obj):
        return obj.label()

    def get_urls(self):
        return [
            path(
                "upload/template/",
                self.admin_site.admin_view(self.template_view),
                name="km_kmversion_template",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        loading.close_stale()
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        loading.close_stale()
        version = KMVersion.objects.defer("content").filter(pk=object_id).first()
        extra = dict(extra_context or {})
        if version is not None:
            shown = (
                KMVersion.objects.defer("content").filter(status=Status.LIVE).exclude(pk=version.pk).first()
            )
            failed_line = ""
            if version.status == Status.FAILED:
                failed_line = (
                    say(words.FAILED_LIVE, label=shown.label(), date=_date(shown.finished_at))
                    if shown
                    else str(words.FAILED_EMPTY)
                )
            extra.update(
                version=version,
                failed_line=failed_line,
                problems=lines(version.problems),
                warnings=lines(version.warnings),
                upload_url=reverse("admin:km_kmversion_upload"),
                can_upload=can_set_up(request.user),
                words=words,
            )
        return super().change_view(request, object_id, form_url, extra)

    def template_view(self, request):
        if not can_set_up(request.user):
            raise PermissionDenied
        response = HttpResponse(workbook.template(), content_type=XLSX)
        response["Content-Disposition"] = 'attachment; filename="km-database-template.xlsx"'
        return response

    @action(description=words.UPLOAD_ACTION, url_path="upload", icon="upload_file")
    def upload(self, request):
        """The upload page (the list's "Upload a new version"): quick checks in the request, then the load in
        the background."""
        if not can_set_up(request.user):
            raise PermissionDenied
        loading.close_stale()
        form = UploadForm(request.POST or None, request.FILES or None)
        problems: list[str] = []
        shown = (
            KMVersion.objects.defer("content")
            .filter(status=Status.LIVE)
            .select_related("uploaded_by")
            .first()
        )
        running = loading.busy()
        if request.method == "POST" and running is not None:
            messages.warning(
                request,
                format_html(
                    '{} <a href="{}">{}</a>.',
                    words.BUSY,
                    reverse("admin:km_kmversion_change", args=[running.pk]),
                    words.BUSY_LINK,
                ),
            )
        elif request.method == "POST" and form.is_valid():
            upload = form.cleaned_data["workbook"]
            data = upload.read() if upload.size <= workbook.MAX_UPLOAD_BYTES else b""  # too big: not read
            name = re.split(r"[/\\]", upload.name or "")[-1][:255]
            checked = workbook.check_upload(data, name, size=upload.size)
            digest = hashlib.sha256(data).hexdigest()
            if checked.problems:
                problems = [
                    *checked.problems,
                    say(words.NOTHING_LOADED, label=shown.label())
                    if shown
                    else str(words.NOTHING_LOADED_EMPTY),
                ]
            elif shown is not None and shown.file_sha256 == digest and not form.cleaned_data["again"]:
                messages.warning(request, say(words.SAME_FILE, again=words.AGAIN_LABEL))
            else:
                return self._start(request, data, name, digest, form.cleaned_data["fewer"])
        context = {
            **self.admin_site.each_context(request),
            "title": words.UPLOAD_ACTION,
            "opts": self.model._meta,
            "form": form,
            "problems": problems,
            "shown": shown,
            "running": running,
            "recent": KMVersion.objects.defer("content").select_related("uploaded_by")[:RECENT],
            "asks": [
                say(
                    sentence,
                    finding_id=quoted([cols.FINDING_ID.header]),
                    record_id=quoted([cols.RECORD_ID.header]),
                )
                for sentence in words.CHECK_ASKS
            ],
            "template_url": reverse("admin:km_kmversion_template"),
            "template_link": words.TEMPLATE_LINK,
            "words": words,
        }
        return render(request, "km/admin/upload.html", context)

    def _start(self, request, data: bytes, name: str, digest: str, fewer: bool):
        from neurodb.integrations import background

        version = KMVersion.objects.create(
            status=Status.QUEUED,
            content=data,
            file_name=name or "workbook.xlsx",
            file_size=len(data),
            file_sha256=digest,
            fewer_ok=fewer,
            uploaded_by=request.user,
        )
        background.start_command(
            "load_km_database",
            "--version-id",
            str(version.pk),
            "--triggered-by",
            request.user.get_username(),
            *(["--fewer-ok"] if fewer else []),
        )
        messages.success(request, str(words.STARTED))
        return redirect("admin:km_kmversion_change", version.pk)


def _date(value) -> str:
    return formats.date_format(value, "j M Y") if value else "—"


@admin.register(KMDocument)
class KMDocumentAdmin(ReadOnlyModelAdmin):
    """The documents of the version shown now, read only (the workbook is changed and uploaded again)."""

    list_display = (
        "record_id",
        "title_short",
        "year_shown",
        "study_type",
        "confidence_shown",
        "findings_shown",
        "needs_review",
    )
    search_fields = ("record_id", "title")
    list_filter = ("study_type", "confidence", "needs_review")

    def get_queryset(self, request):
        return super().get_queryset(request).defer("search_vector").annotate(findings_rows=Count("findings"))

    def has_module_permission(self, request):
        return can_set_up(request.user)

    def has_view_permission(self, request, obj=None):
        return can_set_up(request.user)

    @admin.display(description=_("Title"), ordering="title")
    def title_short(self, obj):
        return obj.title[:80] + ("…" if len(obj.title) > 80 else "")

    @admin.display(description=_("Year"), ordering="year")
    def year_shown(self, obj):
        return obj.year or obj.year_text or "—"

    @admin.display(description=_("Confidence level"), ordering="confidence")
    def confidence_shown(self, obj):
        return obj.get_confidence_display() or obj.confidence_text or "—"

    @admin.display(description=_("Findings"), ordering="findings_rows")
    def findings_shown(self, obj):
        return obj.findings_rows
