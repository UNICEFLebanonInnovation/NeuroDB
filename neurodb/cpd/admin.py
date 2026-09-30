"""The Country Programme in the admin: cycles and their documents, the results framework (typed,
imported from Excel, or proposed from the CPD by AI and reviewed), yearly values and the sources
that measure each indicator."""

from __future__ import annotations

import re

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from neurodb.web.admin_helpers import badge

from . import excel, extraction, sources
from .models import (
    CountryProgramme,
    CPDocument,
    FrameworkProposal,
    Indicator,
    Link,
    Milestone,
    Outcome,
    Output,
    Value,
)


def _user(request) -> str:
    return request.user.get_username()


# ------------------------------------------------------------------------------------ cycles
class DocumentInline(TabularInline):
    model = CPDocument
    fields = ("title", "kind", "file", "note")
    extra = 1
    tab = True


class OutcomeInline(TabularInline):
    model = Outcome
    fields = ("code", "title", "sections", "origin")
    readonly_fields = ("origin",)
    extra = 0
    tab = True
    show_change_link = True


class ImportForm(forms.Form):
    file = forms.FileField(label=_("Results framework (.xlsx)"))


@admin.register(CountryProgramme)
class CountryProgrammeAdmin(ModelAdmin):
    list_display = (
        "name",
        "start_year",
        "end_year",
        "current",
        "outcome_count",
        "indicator_count",
        "dashboard",
    )
    fields = ("name", ("start_year", "end_year"), "current", "etools_name", "summary", "framework_tools")
    readonly_fields = ("framework_tools",)
    inlines = (DocumentInline, OutcomeInline)
    actions_detail = ("download_template", "import_framework")

    @admin.display(description=_("Outcomes"))
    def outcome_count(self, obj):
        return obj.outcomes.count()

    @admin.display(description=_("Indicators"))
    def indicator_count(self, obj):
        return obj.indicators.count()

    @admin.display(description=_("Dashboard"))
    def dashboard(self, obj):
        return format_html('<a href="{}?cycle={}">{}</a>', reverse("cpd:dashboard"), obj.pk, _("Open"))

    @admin.display(description=_("Results framework"))
    def framework_tools(self, obj):
        if not obj.pk:
            return _("Save the cycle first, then fill its framework.")
        return format_html(
            '{} · <a href="{}">{}</a> · <a href="{}">{}</a> · {}',
            _("Fill it with"),
            reverse("admin:cpd_countryprogramme_template", args=[obj.pk]),
            _("the Excel template (with what is already entered)"),
            reverse("admin:cpd_countryprogramme_import", args=[obj.pk]),
            _("import a filled sheet"),
            _(
                "or propose it from an uploaded CPD PDF: Documents list, action "
                "“Propose the results framework”."
            ),
        )

    def save_model(self, request, obj, form, change):
        obj.updated_by = _user(request)
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            if isinstance(item, CPDocument) and not item.uploaded_by:
                item.uploaded_by = _user(request)
            item.save()
        for item in formset.deleted_objects:
            item.delete()
        formset.save_m2m()

    def get_urls(self):
        return [
            path(
                "<int:pk>/template/",
                self.admin_site.admin_view(self.template_view),
                name="cpd_countryprogramme_template",
            ),
            path(
                "<int:pk>/import/",
                self.admin_site.admin_view(self.import_view),
                name="cpd_countryprogramme_import",
            ),
            *super().get_urls(),
        ]

    def template_view(self, request, pk):
        programme = get_object_or_404(CountryProgramme, pk=pk)
        response = HttpResponse(
            excel.template(programme),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        name = re.sub(r"[^A-Za-z0-9-]+", "-", programme.name).strip("-") or "cpd"
        response["Content-Disposition"] = f'attachment; filename="{name}-results-framework.xlsx"'
        return response

    def import_view(self, request, pk):
        programme = get_object_or_404(CountryProgramme, pk=pk)
        if not self.has_change_permission(request, programme):
            return redirect("admin:cpd_countryprogramme_change", pk)
        form = ImportForm(request.POST or None, request.FILES or None)
        errors = []
        if request.method == "POST" and form.is_valid():
            result = excel.import_framework(programme, form.cleaned_data["file"].read())
            errors = result.errors
            if not errors:
                messages.success(
                    request,
                    _("Imported: %(created)s items added, %(updated)s updated.")
                    % {"created": result.created, "updated": result.updated},
                )
                return redirect("admin:cpd_countryprogramme_change", pk)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Import the results framework"),
            "programme": programme,
            "form": form,
            "errors": errors,
            "opts": self.model._meta,
        }
        return render(request, "cpd/admin/import.html", context)

    @action(description=_("Excel template"), url_path="excel-template", icon="download")
    def download_template(self, request, object_id):
        return redirect("admin:cpd_countryprogramme_template", object_id)

    @action(description=_("Import from Excel"), url_path="excel-import", icon="upload")
    def import_framework(self, request, object_id):
        return redirect("admin:cpd_countryprogramme_import", object_id)


@admin.register(CPDocument)
class CPDocumentAdmin(ModelAdmin):
    list_display = ("title", "programme", "kind", "filename", "uploaded_by", "uploaded_at")
    list_filter = ("programme", "kind")
    search_fields = ("title", "note")
    fields = ("programme", "title", "kind", "file", "note", "uploaded_by", "uploaded_at")
    readonly_fields = ("uploaded_by", "uploaded_at")
    actions = ("propose_framework",)

    def save_model(self, request, obj, form, change):
        if not obj.uploaded_by:
            obj.uploaded_by = _user(request)
        super().save_model(request, obj, form, change)

    @admin.action(
        description=_("Propose the results framework from this CPD (AI-suggested, reviewed before use)")
    )
    def propose_framework(self, request, queryset):
        from neurodb.integrations import background

        for document in queryset:
            if not document.filename.lower().endswith(".pdf"):
                messages.warning(
                    request, _("%(doc)s is not a PDF: use the Excel import.") % {"doc": document}
                )
                continue
            proposal = FrameworkProposal.objects.create(document=document, requested_by=_user(request))
            background.start_command("propose_cpd_framework", "--proposal", str(proposal.pk))
            messages.success(
                request,
                _(
                    "Reading %(doc)s in the background (a minute or two): the proposal appears in "
                    "AI-suggested frameworks."
                )
                % {"doc": document},
            )


# ------------------------------------------------------------------------------ framework
class OutputInline(TabularInline):
    model = Output
    fields = ("code", "title", "etools_output", "origin")
    readonly_fields = ("origin",)
    extra = 0
    show_change_link = True


class IndicatorInline(TabularInline):
    model = Indicator
    fk_name = None
    fields = ("code", "title", "unit", "baseline", "target", "origin")
    readonly_fields = ("origin",)
    extra = 0
    show_change_link = True


class OutcomeIndicatorInline(IndicatorInline):
    fk_name = "outcome"
    verbose_name_plural = _("indicators of the outcome itself")


class OutputIndicatorInline(IndicatorInline):
    fk_name = "output"


@admin.register(Outcome)
class OutcomeAdmin(ModelAdmin):
    list_display = ("code", "short_title", "programme", "origin_badge")
    list_filter = ("programme", "origin")
    search_fields = ("code", "title")
    fields = ("programme", "code", "title", "sections")
    inlines = (OutputInline, OutcomeIndicatorInline)

    @admin.display(description=_("Title"))
    def short_title(self, obj):
        return obj.title[:120]

    @admin.display(description=_("Origin"), ordering="origin")
    def origin_badge(self, obj):
        return badge(obj.get_origin_display(), "warn" if obj.origin == "ai_suggested" else "muted")

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            if isinstance(item, Indicator) and not item.programme_id:
                item.programme = form.instance.programme
            item.save()
        for item in formset.deleted_objects:
            item.delete()


@admin.register(Output)
class OutputAdmin(ModelAdmin):
    list_display = ("code", "short_title", "outcome", "etools_output")
    list_filter = ("outcome__programme",)
    search_fields = ("code", "title", "etools_output")
    fields = ("outcome", "code", "title", "etools_output")
    inlines = (OutputIndicatorInline,)

    @admin.display(description=_("Title"))
    def short_title(self, obj):
        return obj.title[:120]

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            if isinstance(item, Indicator) and not item.programme_id:
                item.programme = form.instance.outcome.programme
            item.save()
        for item in formset.deleted_objects:
            item.delete()


class MilestoneInline(TabularInline):
    model = Milestone
    fields = ("year", "value")
    extra = 0


class ValueInline(TabularInline):
    model = Value
    fields = ("year", "value", "source", "note")
    extra = 0
    verbose_name_plural = _("values typed here (they replace the linked sources for their year)")


class LinkForm(forms.ModelForm):
    source = forms.ChoiceField(label=_("Source"), required=False)

    class Meta:
        model = Link
        fields = ("source", "year", "confirmed")

    def __init__(self, *args, indicator=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = sources.choices(indicator) if indicator is not None else []
        current = sources.value_of(self.instance) if self.instance.pk else ""
        if current and current not in {value for _group, options in choices for value, _label in options}:
            choices = [(_("Current"), [(current, self.instance.label)]), *choices]
        self.fields["source"].choices = [("", "---------"), *choices]
        self.fields["source"].initial = current
        self.fields["year"].required = False

    def clean(self):
        cleaned = super().clean()
        value = cleaned.get("source")
        if not value:
            if not self.instance.pk:
                raise forms.ValidationError(_("Choose the source."))
            return cleaned
        try:
            sources.apply(self.instance, value)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        if not cleaned.get("year"):
            cleaned["year"] = self.instance.year or sources.default_year(value)
        if not cleaned.get("year"):
            raise forms.ValidationError(_("Set the CPD year this source counts for."))
        return cleaned


class LinkInline(TabularInline):
    model = Link
    form = LinkForm
    fields = ("source", "year", "confirmed")
    extra = 1
    verbose_name_plural = _("linked sources (summed for their year)")

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)

        class Bound(formset):
            def get_form_kwargs(self, index):
                return {**super().get_form_kwargs(index), "indicator": obj}

        return Bound


@admin.register(Indicator)
class IndicatorAdmin(ModelAdmin):
    list_display = ("code", "short_title", "programme", "unit", "baseline", "target", "origin_badge")
    list_filter = ("programme", "origin", "unit")
    search_fields = ("code", "title")
    fieldsets = (
        (None, {"fields": ("programme", ("outcome", "output"), "code", "title")}),
        (
            _("Measure"),
            {
                "fields": (
                    ("unit", "direction", "accumulate"),
                    ("baseline", "baseline_year", "target"),
                    "means_of_verification",
                    "origin",
                )
            },
        ),
    )
    inlines = (MilestoneInline, ValueInline, LinkInline)
    actions = ("mark_reviewed",)

    @admin.display(description=_("Title"))
    def short_title(self, obj):
        return obj.title[:120]

    @admin.display(description=_("Origin"), ordering="origin")
    def origin_badge(self, obj):
        return badge(obj.get_origin_display(), "warn" if obj.origin == "ai_suggested" else "muted")

    @admin.action(description=_("Mark the selected AI-suggested indicators as reviewed"))
    def mark_reviewed(self, request, queryset):
        count = queryset.filter(origin="ai_suggested").update(origin="manual")
        messages.success(request, _("%(n)s indicator(s) marked as reviewed.") % {"n": count})

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            if hasattr(item, "updated_by"):
                item.updated_by = _user(request)
            item.save()
        for item in formset.deleted_objects:
            item.delete()


# --------------------------------------------------------------------------- AI proposals
@admin.register(FrameworkProposal)
class FrameworkProposalAdmin(ModelAdmin):
    list_display = ("document", "status_badge", "item_count", "requested_by", "created_at", "review")
    list_filter = ("status",)
    fields = ("document", "status", "error", "requested_by", "created_at", "applied_at", "review")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        tones = {"ready": "info", "applied": "ok", "failed": "bad", "running": "warn"}
        return badge(obj.get_status_display(), tones.get(obj.status))

    @admin.display(description=_("Items"))
    def item_count(self, obj):
        return len(extraction.flatten(obj.items or {}))

    @admin.display(description=_("Review"))
    def review(self, obj):
        if obj.status != FrameworkProposal.Status.READY:
            return "—"
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:cpd_frameworkproposal_review", args=[obj.pk]),
            _("Review and apply"),
        )

    def get_urls(self):
        return [
            path(
                "<int:pk>/review/",
                self.admin_site.admin_view(self.review_view),
                name="cpd_frameworkproposal_review",
            ),
            *super().get_urls(),
        ]

    def review_view(self, request, pk):
        proposal = get_object_or_404(FrameworkProposal.objects.select_related("document__programme"), pk=pk)
        rows = extraction.flatten(proposal.items or {})
        if request.method == "POST" and proposal.status == FrameworkProposal.Status.READY:
            if not self.has_change_permission(request, proposal):
                return redirect("admin:cpd_frameworkproposal_changelist")
            made = extraction.apply(proposal, set(request.POST.getlist("keep")))
            messages.success(
                request,
                _(
                    "Applied: %(created)s items added, %(skipped)s already there. "
                    "They are marked AI-suggested "
                    "until reviewed."
                )
                % made,
            )
            return redirect("admin:cpd_countryprogramme_change", proposal.document.programme_id)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Review the AI-suggested framework"),
            "proposal": proposal,
            "rows": rows,
            "opts": self.model._meta,
        }
        return render(request, "cpd/admin/review.html", context)
