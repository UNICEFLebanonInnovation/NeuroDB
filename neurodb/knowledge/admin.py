"""The knowledge base in the admin: documents, their links (add or remove one by hand) and their
reading status."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline

from neurodb.web.admin_helpers import badge

from .models import Document, Link


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
    list_display = ("title", "status_badge", "section", "year", "added_by", "created_at")
    list_filter = ("status", "section", "year")
    search_fields = ("title", "source", "summary")
    fields = (
        "title", "source", "section", "year", "file", "status", "error", "summary", "key_points",
        "document_date", "pages", "characters", "added_by", "created_at", "indexed_at",
    )  # fmt: skip
    readonly_fields = (
        "file",
        "status",
        "error",
        "pages",
        "characters",
        "added_by",
        "created_at",
        "indexed_at",
    )
    inlines = (LinkInline,)
    actions = ("read_again",)

    def has_add_permission(self, request):
        return False  # added from the knowledge base page, where the file or text is read

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
        from .views import start

        for document in queryset:
            document.status = Document.Status.PENDING
            document.save(update_fields=["status", "updated_at"])
            start(document)
        messages.success(
            request, _("Reading %(n)s document(s) again in the background.") % {"n": queryset.count()}
        )
