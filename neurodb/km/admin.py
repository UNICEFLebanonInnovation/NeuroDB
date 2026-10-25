"""The KM Database in the admin, for the people who set NeuroDB up (``roles.can_set_up``: superusers and
Administrators with staff status): its versions, with the page that uploads a new one and the Excel template,
its documents (read only), the corrections of its downloads' authors and the questions asked to its assistant
(read only). The findings have no admin list: the KM Database page shows them.

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
from django.utils import formats, timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from neurodb.accounts.roles import can_set_up
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import authors, loading, words, workbook
from . import columns as cols
from .forms import UploadForm
from .models import KMDocument, KMOrganisation, KMQuestion, KMVersion, day, live
from .words import counted, quoted, say

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
            path(
                "<int:pk>/review/",
                self.admin_site.admin_view(self.review_view),
                name="km_kmversion_review",
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
                    say(words.FAILED_LIVE, label=shown.label(loaded=True))
                    if shown
                    else str(words.FAILED_EMPTY)
                )
            if version.status == Status.LIVE:
                extra.update(review_list(version))
            elif version.download_summary:  # a version replaced keeps how its downloads showed the authors
                extra["download_summary"] = summary_line(version.download_summary)
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

    def review_view(self, request, pk: int):
        """The version page's review buttons (POST): **Count as a person** and **Not a name** list an author
        entry under Organisations named in downloads. The entry travels in the POST body, never in a URL."""
        if not can_set_up(request.user):
            raise PermissionDenied
        if request.method != "POST":
            return redirect("admin:km_kmversion_change", pk)
        name = " ".join((request.POST.get("name") or "").split())[:200]
        kind = request.POST.get("kind")
        if name and kind in (KMOrganisation.Kind.PERSON, KMOrganisation.Kind.WORDS):
            row = KMOrganisation.objects.filter(folded=authors.fold(name)[:200]).first() or KMOrganisation(
                name=name, added_by=request.user
            )
            row.kind = kind
            row.save()
            messages.success(
                request, say(words.REVIEW_SAVED, name=name, kind=KMOrganisation.Kind(kind).label)
            )
        return redirect("admin:km_kmversion_change", pk)

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
            "shown_line": shown_line(shown) if shown else "",
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


def summary_line(summary: dict) -> str:
    """ "Downloads name the organisations of 146 documents; 314 list only authors (counted, not named); 61
    mix both; 16 give no author."."""

    def number(key: str) -> str:
        return formats.number_format(summary.get(key, 0), force_grouping=True)

    return say(
        words.DOWNLOAD_SUMMARY,
        organisations=counted(summary.get("organisations", 0), words.DOCUMENTS_N),
        authors_only=number("authors_only"),
        mixed=number("mixed"),
        empty=number("empty"),
    )


def review_list(version: KMVersion) -> dict:
    """What the uploader reviews on the page of the version shown: how downloads show the documents'
    authors, every string they name as an organisation (each with **Count as a person**) and the author
    entries that look like ordinary words (each with **Not a name**). Computed when the page is viewed."""
    known, people = authors.context(version)
    summary = authors.summary(known, people)
    named = authors.named_organisations(known, people)
    looks = authors.look_like_words(known, people)
    return {
        "download_summary": summary_line(summary),
        "named_organisations": named,
        "named_title": say(words.NAMED_AS_ORGANISATIONS, n=len(named)),
        "look_like_words": looks,
        "words_title": say(words.LOOK_LIKE_WORDS, n=len(looks)),
        "review_url": reverse("admin:km_kmversion_review", args=[version.pk]),
        "kind_person": KMOrganisation.Kind.PERSON,
        "kind_words": KMOrganisation.Kind.WORDS,
    }


def shown_line(version: KMVersion) -> str:
    """The upload page's line on the version shown now: its dates (the load's in local time, with the hour),
    who uploaded it, and its documents and findings."""
    when = version.finished_at or version.uploaded_at
    values = {
        "loaded": formats.date_format(timezone.localtime(when), "j M Y, H:i") if when else "—",
        "who": version.uploaded_by or "—",
        "documents": counted(version.documents_count, words.DOCUMENTS_N),
        "findings": counted(version.findings_count, words.FINDINGS_N),
    }
    if version.prepared_on:
        return say(words.SHOWN_PREPARED, prepared=day(version.prepared_on), **values)
    return say(words.SHOWN_LOADED, **values)


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

    def get_list_display(self, request):
        """The list's columns, with how downloads show each document's authors (the people read once per
        page)."""
        namer = authors.Namer(*authors.context(live()))

        @admin.display(description=_("Shown in downloads as"))
        def downloads_shown(obj):
            return namer(obj.entity) or "—"

        columns = list(super().get_list_display(request))
        columns.insert(columns.index("needs_review"), downloads_shown)
        return columns

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


@admin.register(KMOrganisation)
class KMOrganisationAdmin(ModelAdmin):
    """Corrections of how the KM Database's downloads show an author entry: an organisation (named), a
    person (counted, never named) or words (left out). They change downloads at once."""

    list_display = ("name", "kind", "added_by", "added_at")
    list_filter = ("kind",)
    search_fields = ("name",)
    list_select_related = ("added_by",)
    fields = ("name", "kind")

    def has_module_permission(self, request):
        return can_set_up(request.user)

    def has_view_permission(self, request, obj=None):
        return can_set_up(request.user)

    def has_add_permission(self, request):
        return can_set_up(request.user)

    def has_change_permission(self, request, obj=None):
        return can_set_up(request.user)

    def has_delete_permission(self, request, obj=None):
        return can_set_up(request.user)

    def get_readonly_fields(self, request, obj=None):
        return ("name",) if obj is not None else ()  # a change is of its kind only

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        super().save_model(request, obj, form, change)


QUESTION_TONES = {
    "answered": "ok",
    "no_match": "muted",
    "refused": "warn",
    "failed": "bad",
    "limited": "muted",
    "in_progress": "muted",
}


@admin.register(KMQuestion)
class KMQuestionAdmin(ReadOnlyModelAdmin):
    """The questions put to the KM Database's assistant (read only, kept 90 days): who asked, whether it was
    answered, how many findings it read and cited, the sentences that cite none, the tokens and the time. The
    question is kept as it was sent: names, e-mail addresses, phone numbers and links removed."""

    list_display = (
        "created_at",
        "user",
        "question_short",
        "status_badge",
        "covered",
        "read_shown",
        "cited_shown",
        "uncited",
        "tokens_shown",
        "duration",
    )
    list_filter = ("status", "covered", "created_at")
    search_fields = ("question", "answer", "user__username")
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    list_per_page = 50

    def has_module_permission(self, request):
        return can_set_up(request.user)

    def has_view_permission(self, request, obj=None):
        return can_set_up(request.user)

    @admin.display(description=_("Question"))
    def question_short(self, obj):
        return obj.question[:100] + ("…" if len(obj.question) > 100 else "")

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), QUESTION_TONES.get(obj.status))

    @admin.display(description=_("Findings read"))
    def read_shown(self, obj):
        return len(obj.read or [])

    @admin.display(description=_("Findings cited"))
    def cited_shown(self, obj):
        return len(obj.cited or [])

    @admin.display(description=_("Tokens in / out"))
    def tokens_shown(self, obj):
        return f"{obj.input_tokens + obj.cache_read_tokens:,} / {obj.output_tokens:,}"

    @admin.display(description=_("Time"), ordering="duration_ms")
    def duration(self, obj):
        return f"{obj.duration_ms / 1000:.1f} s"
