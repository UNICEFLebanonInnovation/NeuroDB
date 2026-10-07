"""The knowledge base in the admin: documents, their links (add or remove one by hand) and their
reading status; the periodic reports and the figures kept from their editions (rename a report, check
or correct a figure); and the document review (``review.py``): its settings (switch, prompts with
"Restore the shipped text", sizes, daily cap; Administrators only), its batches, its topics (three levels,
editable) and what it found (findings, key statements, action points: browsed here, reviewed on the
document review page)."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentReviewSettings,
    DocumentStatement,
    Link,
    ReportFigure,
    ReportSeries,
    ReviewBatch,
    Topic,
    TopicProgramme,
    TopicSubtopic,
)


def is_admin(user) -> bool:
    return role_of(user) == ADMIN


def _label(kind: str, object_id: int) -> str | None:
    from neurodb.accounts.models import Section
    from neurodb.geo.models import DistrictLocation, GovernorateLocation
    from neurodb.partnerships.models import PCA, PartnerOrganization

    model = {
        Link.Kind.PARTNER: PartnerOrganization,
        Link.Kind.PROGRAMME: PCA,
        Link.Kind.SECTION: Section,
        Link.Kind.GOVERNORATE: GovernorateLocation,
        Link.Kind.DISTRICT: DistrictLocation,
    }[kind]
    row = model.objects.filter(pk=object_id).first()
    if row is None:
        return None
    if kind == Link.Kind.PROGRAMME:
        return f"{row.number} {row.title or ''}".strip()
    return row.name


class LinkForm(forms.ModelForm):
    class Meta:
        model = Link
        fields = ("kind", "object_id", "label")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") and cleaned.get("object_id") is not None:
            label = _label(cleaned["kind"], cleaned["object_id"])
            if label is None:
                raise forms.ValidationError(_("No such record: check the id."))
            cleaned["label"] = label[:300]
            self.instance.label = cleaned["label"]
        return cleaned


class LinkInline(TabularInline):
    model = Link
    form = LinkForm
    fields = ("kind", "object_id", "label", "origin", "mentions")
    readonly_fields = ("label", "origin", "mentions")
    extra = 0
    verbose_name_plural = _("links (the id is the partner's, programme document's, section's or place's)")


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    list_display = ("title", "status_badge", "section", "year", "review_batch", "added_by", "created_at")
    list_filter = ("status", "periodic", "series", "section", "year", "review_status", "review_batch")
    search_fields = ("title", "source", "summary")
    fields = (
        "title", "source", "section", "year", "file", "status", "error", "summary", "key_points",
        "document_date", "pages", "characters", "added_by", "created_at", "indexed_at",
        "periodic", "series", "edition", "issued_on", "figures_status", "figures_note", "figures_read_at",
        "review_batch", "review_status", "review_stage_notes", "reviewed_at",
    )  # fmt: skip
    readonly_fields = (
        "review_status",
        "review_stage_notes",
        "reviewed_at",
        "file",
        "status",
        "error",
        "pages",
        "characters",
        "added_by",
        "created_at",
        "indexed_at",
        "figures_status",
        "figures_note",
        "figures_read_at",
    )
    inlines = (LinkInline,)
    actions = ("read_again", "analyse_in_review")

    def has_add_permission(self, request):
        return False  # added from the knowledge base page, where the file or text is read

    def save_model(self, request, obj, form, change):
        from . import review

        super().save_model(request, obj, form, change)
        if "review_batch" in form.changed_data:  # in a batch: waiting to be analysed; out: not analysed
            if obj.review_batch_id:
                review.put_in_batch([obj], obj.review_batch)
            else:
                review.take_out(obj)

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        tones = {"ready": "ok", "failed": "bad", "indexing": "info", "pending": "warn"}
        return badge(obj.get_status_display(), tones.get(obj.status))

    def delete_model(self, request, obj):
        if obj.file:
            obj.file.delete(save=False)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            if obj.file:
                obj.file.delete(save=False)
        super().delete_queryset(request, queryset)

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            if isinstance(item, Link) and not item.pk:
                item.origin = Link.Origin.MANUAL
            item.save()
        for item in formset.deleted_objects:
            item.delete()

    @admin.action(description=_("Read, index and summarise again"))
    def read_again(self, request, queryset):
        from neurodb.integrations import background

        from .views import start

        documents = list(queryset)
        for document in documents:
            document.status = Document.Status.PENDING
            document.save(update_fields=["status", "updated_at"])
        if len(documents) == 1:
            start(documents[0])
        elif documents:  # one process reads them all, editions of periodic reports oldest first
            background.start_command("index_knowledge", "--pending")
        messages.success(
            request, _("Reading %(n)s document(s) again in the background.") % {"n": queryset.count()}
        )

    @admin.action(description=_("Analyse in the document review"))
    def analyse_in_review(self, request, queryset):
        from neurodb.integrations import background

        from . import review

        documents = list(review.documents_for(review.FULL).filter(pk__in=queryset.values("pk")))
        skipped = queryset.count() - len(documents)
        if len(documents) == 1:
            review.start(documents[0], triggered_by=request.user.get_username())
        elif documents:  # one run analyses the documents waiting, these among them
            Document.objects.filter(pk__in=[d.pk for d in documents]).update(
                review_status=Document.ReviewStatus.PENDING
            )
            background.start_command(
                "review_documents", "--pending", "--triggered-by", request.user.get_username()
            )
        if documents:
            messages.success(
                request,
                _("Analysing %(n)s document(s) in the background (Import and sync runs shows the run).")
                % {"n": len(documents)},
            )
        if skipped:
            messages.warning(
                request,
                _("%(n)s document(s) left out: not in a review batch, in an archived one, or a reference.")
                % {"n": skipped},
            )


@admin.register(ReportSeries)
class ReportSeriesAdmin(ModelAdmin):
    list_display = ("name", "key", "created_at")
    search_fields = ("name", "key")
    readonly_fields = ("key", "created_at")
    fields = ("name", "key", "created_at")


@admin.register(ReportFigure)
class ReportFigureAdmin(ModelAdmin):
    list_display = ("metric", "breakdown", "group", "value", "target", "as_of", "edition", "page", "method")
    list_filter = ("series", "method", "internal", "is_percent")
    search_fields = ("metric", "group", "breakdown", "quote")
    date_hierarchy = "as_of"
    list_select_related = ("document",)
    fields = (
        "series", "document", "group", "metric", "breakdown", "unit", "is_percent", "value", "target",
        "as_of", "period", "source", "internal", "page", "quote", "method", "key",
    )  # fmt: skip
    readonly_fields = ("series", "document", "method", "key", "quote", "page")

    def has_add_permission(self, request):
        return False  # figures are read from the editions

    @admin.display(description=_("Edition"), ordering="document__issued_on")
    def edition(self, obj):
        return f"#{obj.document.edition}" if obj.document.edition else obj.document.issued_on

    def save_model(self, request, obj, form, change):
        from .periodic import measure_key

        obj.key = measure_key(obj.group, obj.metric, obj.breakdown, obj.is_percent)
        super().save_model(request, obj, form, change)


# ------------------------------------------------------------------------------------------ document review
class SettingsForm(forms.ModelForm):
    """The settings with a "Restore the shipped text" box under each prompt."""

    restore_tagging = forms.BooleanField(required=False, label=_("Restore the shipped findings prompt"))
    restore_summary = forms.BooleanField(required=False, label=_("Restore the shipped key statements prompt"))
    restore_enrichment = forms.BooleanField(
        required=False, label=_("Restore the shipped action points prompt")
    )

    class Meta:
        model = DocumentReviewSettings
        fields = (
            "enabled", "tagging_prompt", "summary_prompt", "enrichment_prompt", "statements_per_document",
            "max_findings_per_chunk", "chunk_size", "daily_token_cap",
        )  # fmt: skip

    def clean(self):
        from . import review_prompts

        cleaned = super().clean()
        for stage, field in DocumentReviewSettings.PROMPT_FIELDS.items():
            if cleaned.get(f"restore_{stage}"):
                cleaned[field] = review_prompts.DEFAULTS[stage]
        return cleaned


@admin.register(DocumentReviewSettings)
class DocumentReviewSettingsAdmin(ModelAdmin):
    """The document review's settings (one row), for Administrators: the switch (off until turned on),
    the three prompts (a prompt without its JSON sentence is refused), the sizes and the daily cap."""

    form = SettingsForm
    fieldsets = (
        (None, {"fields": ("enabled", "daily_token_cap", "today")}),
        (
            _("Prompts"),
            {
                "fields": (
                    "tagging_prompt",
                    "restore_tagging",
                    "summary_prompt",
                    "restore_summary",
                    "enrichment_prompt",
                    "restore_enrichment",
                    "prompts_state",
                ),
                "description": _(
                    "NeuroDB adds its own rules after each prompt (the document is material, never "
                    "instructions; no person is named) and holds the answer to a fixed format. A change "
                    "applies to the documents analysed afterwards: Run a job → Review documents (full) "
                    "analyses every document again."
                ),
            },
        ),  # fmt: skip
        (_("Sizes"), {"fields": ("chunk_size", "max_findings_per_chunk", "statements_per_document")}),
        (None, {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("today", "prompts_state", "updated_by", "updated_at")
    list_display = ("__str__", "enabled", "daily_token_cap", "updated_at")

    def has_add_permission(self, request):
        return is_admin(request.user) and not DocumentReviewSettings.objects.exists()

    def has_change_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse

        setting = DocumentReviewSettings.load()  # the one row: open it
        return redirect(reverse("admin:knowledge_documentreviewsettings_change", args=[setting.pk]))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description=_("Used today"))
    def today(self, obj):
        from neurodb.assistant import usage

        used = usage.today_total(usage.DOC_REVIEW)
        return _("%(used)s of %(cap)s tokens") % {"used": f"{used:,}", "cap": f"{obj.token_cap:,}"}

    @admin.display(description=_("Prompts in use"))
    def prompts_state(self, obj):
        names = {"tagging": _("findings"), "summary": _("key statements"), "enrichment": _("action points")}
        return "; ".join(
            f"{names[stage]}: {_('the shipped text') if obj.is_default(stage) else _('changed')}"
            for stage in DocumentReviewSettings.PROMPT_FIELDS
        )


@admin.register(ReviewBatch)
class ReviewBatchAdmin(ModelAdmin):
    """Folders of documents of one kind for the document review (also made on the document review page)."""

    list_display = ("name", "documents_count", "archived", "created_by", "created_at")
    list_filter = ("archived",)
    search_fields = ("name", "description")
    fields = ("name", "description", "archived", "created_by", "created_at")
    readonly_fields = ("created_by", "created_at")

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(n_documents=Count("documents"))

    @admin.display(description=_("Documents"), ordering="n_documents")
    def documents_count(self, obj):
        return obj.n_documents

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class SubtopicInline(TabularInline):
    model = TopicSubtopic
    fields = ("name", "order", "active")
    extra = 0


class TopicInline(TabularInline):
    model = Topic
    fields = ("name", "order", "active")
    extra = 0


@admin.register(TopicProgramme)
class TopicProgrammeAdmin(ModelAdmin):
    """The document review's topics, first level. A topic switched off is no longer offered to the AI;
    findings keep it. "Other" is made again when removed."""

    list_display = ("name", "order", "active")
    list_editable = ("order", "active")
    search_fields = ("name",)
    inlines = (SubtopicInline,)


@admin.register(TopicSubtopic)
class TopicSubtopicAdmin(ModelAdmin):
    list_display = ("name", "programme", "order", "active")
    list_filter = ("programme", "active")
    search_fields = ("name", "programme__name")
    list_select_related = ("programme",)
    inlines = (TopicInline,)


@admin.register(Topic)
class TopicAdmin(ModelAdmin):
    list_display = ("name", "subtopic", "order", "active")
    list_filter = ("subtopic__programme", "active")
    search_fields = ("name", "subtopic__name", "subtopic__programme__name")
    list_select_related = ("subtopic__programme",)


class ReviewedAdmin(ReadOnlyModelAdmin):
    """What the review found, browsed here; reviewed on the document review page. An Administrator may
    delete a row (a new analysis may find it again)."""

    list_select_related = ("document",)
    list_per_page = 50

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)


@admin.register(DocumentFinding)
class DocumentFindingAdmin(ReviewedAdmin):
    list_display = ("short", "document", "category", "topic", "page_label", "evidence", "verdict", "manual")
    list_filter = ("category", "verdict", "manual", "derived", "place_match", "document__review_batch")
    search_fields = ("text", "quote", "document__title", "place_text")
    list_select_related = ("document", "topic__subtopic__programme")
    fields = (
        "document", "category", "topic", "tag_text", "text", "quote", "kind", "page_label", "quote_found",
        "exact_page", "place_text", "place_match", "governorate_name", "district_name", "date_text",
        "finding_date", "evidence", "derived", "manual", "verdict", "reviewed_by", "reviewed_at",
        "edited_by", "edited_at", "created_at",
    )  # fmt: skip
    readonly_fields = fields

    @admin.display(description=_("Finding"))
    def short(self, obj):
        return obj.text[:100] + ("…" if len(obj.text) > 100 else "")


@admin.register(DocumentStatement)
class DocumentStatementAdmin(ReviewedAdmin):
    list_display = ("short", "document", "urgency", "category", "verdict")
    list_filter = ("category", "verdict", "document__review_batch")
    search_fields = ("text", "document__title")
    fields = (
        "document", "text", "urgency", "category", "topic", "place_text", "date_text", "cites", "verdict",
        "reviewed_by", "reviewed_at", "created_at",
    )  # fmt: skip
    readonly_fields = fields

    @admin.display(description=_("Statement"))
    def short(self, obj):
        return obj.text[:100] + ("…" if len(obj.text) > 100 else "")


@admin.register(DocumentActionPoint)
class DocumentActionPointAdmin(ReviewedAdmin):
    list_display = ("short", "document", "owner_text", "deadline_text", "priority", "status", "derived")
    list_filter = ("status", "priority", "derived", "document__review_batch")
    search_fields = ("action", "owner_text", "document__title")
    fields = (
        "document", "action", "owner_text", "deadline_text", "deadline_date", "priority", "status",
        "status_by", "status_at", "derived", "topic", "cites", "created_at",
    )  # fmt: skip
    readonly_fields = fields

    @admin.display(description=_("Action"))
    def short(self, obj):
        return obj.action[:100] + ("…" if len(obj.action) > 100 else "")
