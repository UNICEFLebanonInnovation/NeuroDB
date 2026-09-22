"""Indicator configuration admin: the v2 wizards re-implemented without mutating ModelAdmin state."""

from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse

from neurodb.indicators.services.navigation import invalidate

from .models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterIndicatorTag,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportComment,
    NeuroReportMasterIndicator,
    ReportingYear,
    SubIndicator,
)


@admin.register(ReportingYear)
class ReportingYearAdmin(admin.ModelAdmin):
    list_display = ("name", "year", "current")
    list_editable = ("current",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.current:
            ReportingYear.objects.exclude(pk=obj.pk).update(current=False)
        invalidate()


@admin.register(Database)
class DatabaseAdmin(admin.ModelAdmin):
    list_display = ("label", "name", "ai_id", "section", "reporting_year", "display", "is_funded_by_unicef", "last_monthly_update_date")
    list_filter = ("reporting_year", "section", "display", "is_funded_by_unicef")
    search_fields = ("name", "label", "ai_id", "db_id")
    exclude = ("username", "password", "mapping_extraction1", "mapping_extraction2", "mapping_extraction3")
    readonly_fields = ("last_update_date", "last_live_update_date", "last_monthly_update_date", "last_weekly_update_date")
    actions = ("queue_structure_import", "queue_data_import")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        invalidate()

    @admin.action(description="Import structure from ActivityInfo (runs as a job)")
    def queue_structure_import(self, request, queryset):
        self._run_import(request, queryset, "import_activityinfo_structure")

    @admin.action(description="Import data from ActivityInfo (runs as a job)")
    def queue_data_import(self, request, queryset):
        self._run_import(request, queryset, "import_activityinfo_data")

    def _run_import(self, request, queryset, command):
        """Runs the management command in-process (each creates its own SyncRun); jobs run the same command on a schedule."""
        from django.core.management import call_command

        for db in queryset:
            try:
                call_command(command, database=db.ai_id, triggered_by=request.user.get_username())
                self.message_user(request, f"{command} finished for {db}.", messages.SUCCESS)
            except Exception as exc:  # noqa: BLE001 - surfaced to the admin user
                self.message_user(request, f"{command} failed for {db}: {exc}", messages.ERROR)


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("name", "database", "ai_form_id", "category")
    list_filter = ("database",)
    search_fields = ("name", "label", "ai_form_id")


@admin.register(IndicatorNew)
class IndicatorAdmin(admin.ModelAdmin):
    list_display = ("name", "awp_code", "database", "activity", "gender", "nationality", "age_group", "programme", "disability")
    list_filter = ("database", "gender", "nationality", "programme", "disability")
    search_fields = ("name", "awp_code", "ai_indicator")
    autocomplete_fields = ("activity",)


class MasterSubInline(admin.TabularInline):
    model = MasterSubIndicator
    extra = 0
    autocomplete_fields = ("sub",)
    fields = ("sub", "effect", "listed", "label", "target", "sequence")


class AddSubIndicatorsForm(forms.Form):
    subs = forms.ModelMultipleChoiceField(queryset=SubIndicator.objects.none(), widget=forms.CheckboxSelectMultiple)
    effect = forms.ChoiceField(choices=[("TOTAL", "TOTAL"), ("NO_EFFECT", "NO_EFFECT"), ("NUMERATOR", "NUMERATOR"), ("DENOMINATOR", "DENOMINATOR")])

    def __init__(self, master, *args, **kwargs):
        super().__init__(*args, **kwargs)
        linked = master.subindicators.values_list("sub_id", flat=True)
        self.fields["subs"].queryset = SubIndicator.objects.filter(database=master.database).exclude(id__in=linked).order_by("awp_code")


@admin.register(SubIndicator)
class SubIndicatorAdmin(admin.ModelAdmin):
    list_display = ("name", "awp_code", "database", "aggregation_method", "target")
    list_filter = ("database", "aggregation_method")
    search_fields = ("name", "awp_code")
    filter_horizontal = ("indicators",)


@admin.register(MasterIndicator)
class MasterIndicatorAdmin(admin.ModelAdmin):
    list_display = ("name", "awp_code", "database", "aggregation_method", "awp_target", "reporting_level", "is_active", "sequence")
    list_filter = ("database", "aggregation_method", "reporting_level", "is_active", "tags")
    search_fields = ("name", "awp_code")
    filter_horizontal = ("tags",)
    inlines = (MasterSubInline,)
    change_form_template = "admin/pivoting/masterindicator/change_form.html"

    def get_urls(self):
        return [
            path("<int:pk>/add-sub-indicators/", self.admin_site.admin_view(self.add_sub_indicators), name="pivoting_masterindicator_add_subs"),
        ] + super().get_urls()

    def add_sub_indicators(self, request, pk):
        """The v2 'Add Sub Indicators' wizard: bulk-link subs of the same database with one effect."""
        master = get_object_or_404(MasterIndicator, pk=pk)
        form = AddSubIndicatorsForm(master, request.POST or None)
        if request.method == "POST" and form.is_valid():
            start = master.subindicators.count()
            for i, sub in enumerate(form.cleaned_data["subs"], start=1):
                MasterSubIndicator.objects.create(master=master, sub=sub, effect=form.cleaned_data["effect"], sequence=start + i, label=sub.name)
            self.message_user(request, f"Linked {len(form.cleaned_data['subs'])} sub-indicator(s).", messages.SUCCESS)
            return redirect(reverse("admin:pivoting_masterindicator_change", args=[pk]))
        context = {**self.admin_site.each_context(request), "form": form, "master": master, "title": f"Add sub-indicators to {master.name}"}
        return render(request, "admin/pivoting/masterindicator/add_subs.html", context)


@admin.register(MasterIndicatorTag)
class MasterIndicatorTagAdmin(admin.ModelAdmin):
    search_fields = ("name",)


class ReportMasterInline(admin.TabularInline):
    model = NeuroReportMasterIndicator
    extra = 0
    autocomplete_fields = ("master",)
    fields = ("master", "label", "category", "target", "ram_result")


class AddMasterIndicatorsForm(forms.Form):
    tags = forms.ModelMultipleChoiceField(queryset=MasterIndicatorTag.objects.all(), required=False)
    masters = forms.ModelMultipleChoiceField(queryset=MasterIndicator.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, report, *args, **kwargs):
        super().__init__(*args, **kwargs)
        linked = NeuroReportMasterIndicator.objects.filter(report=report).values_list("master_id", flat=True)
        qs = MasterIndicator.objects.filter(is_active=True, database__reporting_year=report.ryear).exclude(id__in=linked)
        self.fields["masters"].queryset = qs.select_related("database").order_by("database__name", "awp_code")


@admin.register(NeuroReport)
class NeuroReportAdmin(admin.ModelAdmin):
    list_display = ("name", "report_code", "ryear", "is_hpm", "is_active")
    list_filter = ("ryear", "is_hpm", "is_active")
    search_fields = ("name", "report_code")
    inlines = (ReportMasterInline,)
    change_form_template = "admin/pivoting/neuroreport/change_form.html"

    def get_urls(self):
        return [
            path("<int:pk>/add-master-indicators/", self.admin_site.admin_view(self.add_masters), name="pivoting_neuroreport_add_masters"),
        ] + super().get_urls()

    def add_masters(self, request, pk):
        """The v2 'Add Master Indicators' wizard: by direct selection or by tag."""
        report = get_object_or_404(NeuroReport, pk=pk)
        form = AddMasterIndicatorsForm(report, request.POST or None)
        if request.method == "POST" and form.is_valid():
            masters = set(form.cleaned_data["masters"])
            if form.cleaned_data["tags"]:
                masters.update(form.fields["masters"].queryset.filter(tags__in=form.cleaned_data["tags"]))
            for m in masters:
                NeuroReportMasterIndicator.objects.get_or_create(report=report, master=m, defaults={"label": m.name, "target": m.awp_target or 0})
            self.message_user(request, f"Added {len(masters)} master indicator(s).", messages.SUCCESS)
            invalidate()
            return redirect(reverse("admin:pivoting_neuroreport_change", args=[pk]))
        context = {**self.admin_site.each_context(request), "form": form, "report": report, "title": f"Add master indicators to {report.name}"}
        return render(request, "admin/pivoting/neuroreport/add_masters.html", context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        invalidate()


@admin.register(NeuroReportComment)
class NeuroReportCommentAdmin(admin.ModelAdmin):
    list_display = ("report", "master", "related_month", "entry_date", "is_active")
    list_filter = ("report", "related_month", "is_active")
