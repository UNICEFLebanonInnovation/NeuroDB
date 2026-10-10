"""Read-only admin over the eTools Datamart tables (refreshed by ``sync_etools_datamart``), and the
eTools reporting checks page (admin/datamart/reporting-checks/, Administrators only)."""

from django import forms
from django.contrib import admin
from django.http import Http404
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from neurodb.accounts.roles import ADMIN, role_of
from neurodb.web.admin_helpers import ReadOnlyModelAdmin

from . import checks
from . import models as dm


class DatamartAdmin(ReadOnlyModelAdmin):
    readonly_fields = ("data",)
    list_per_page = 50


@admin.register(dm.FundsReservation)
class FundsReservationAdmin(DatamartAdmin):
    list_display = (
        "fr_number",
        "line_item",
        "pd_reference_number",
        "donor",
        "grant_number",
        "overall_amount",
        "end_date",
    )
    list_filter = ("donor", "completed_flag")
    search_fields = ("fr_number", "pd_reference_number", "donor", "grant_number")
    raw_id_fields = ("intervention",)


@admin.register(dm.Grant)
class GrantAdmin(DatamartAdmin):
    list_display = ("name", "donor", "expiry", "description")
    list_filter = ("donor",)
    search_fields = ("name", "donor", "description")


@admin.register(dm.PDIndicator)
class PDIndicatorAdmin(DatamartAdmin):
    list_display = ("title", "pd_reference_number", "section_name", "location_name", "is_active")
    list_filter = ("section_name", "is_active", "is_high_frequency")
    search_fields = ("title", "pd_reference_number", "lower_result_name")
    raw_id_fields = ("intervention",)


@admin.register(dm.PartnerAssessment)
class PartnerAssessmentAdmin(DatamartAdmin):
    list_display = ("partner_name", "vendor_number", "type", "rating", "completed_date", "current")
    list_filter = ("type", "rating", "current")
    search_fields = ("partner_name", "vendor_number")
    raw_id_fields = ("partner",)


@admin.register(dm.PSEAAssessment)
class PSEAAssessmentAdmin(DatamartAdmin):
    list_display = (
        "partner_name",
        "vendor_number",
        "reference_number",
        "overall_rating",
        "assessment_date",
        "status",
    )
    list_filter = ("status",)
    search_fields = ("partner_name", "vendor_number", "reference_number")
    raw_id_fields = ("partner",)


@admin.register(dm.AuditEngagement)
class AuditEngagementAdmin(DatamartAdmin):
    list_display = (
        "reference_number",
        "partner_name",
        "engagement_type",
        "status",
        "start_date",
        "financial_findings",
    )
    list_filter = ("engagement_type", "status", "year_of_audit")
    search_fields = ("reference_number", "partner_name", "vendor_number", "auditor")
    raw_id_fields = ("partner", "interventions")


@admin.register(dm.ActionPoint)
class ActionPointAdmin(DatamartAdmin):
    list_display = (
        "reference_number",
        "partner_name",
        "intervention_number",
        "status",
        "high_priority",
        "due_date",
    )
    list_filter = ("status", "high_priority", "related_module")
    search_fields = (
        "reference_number",
        "description",
        "partner_name",
        "intervention_number",
        "assigned_to_name",
    )
    raw_id_fields = ("partner", "intervention")


@admin.register(dm.TPMVisit)
class TPMVisitAdmin(DatamartAdmin):
    list_display = ("reference_number", "partner_name", "tpm_name", "status", "start_date", "end_date")
    list_filter = ("status",)
    search_fields = ("reference_number", "partner_name", "vendor_number", "tpm_name")
    raw_id_fields = ("partner",)


@admin.register(dm.MonitoringFinding)
class MonitoringFindingAdmin(DatamartAdmin):
    list_display = ("monitoring_activity", "entity", "entity_type", "overall_finding_rating", "end_date")
    list_filter = ("overall_finding_rating", "entity_type", "is_programmatic_visit")
    search_fields = ("monitoring_activity", "entity", "vendor_number", "reference_number")
    raw_id_fields = ("partner",)


@admin.register(dm.HACTAggregate)
class HACTAggregateAdmin(DatamartAdmin):
    list_display = (
        "year",
        "microassessments_total",
        "programmaticvisits_total",
        "completed_spotcheck",
        "completed_hact_audits",
        "completed_special_audits",
    )


@admin.register(dm.FundsReservationHeader)
class FundsReservationHeaderAdmin(DatamartAdmin):
    list_display = (
        "fr_number",
        "pd_reference_number",
        "total_amt",
        "actual_amt",
        "outstanding_amt",
        "end_date",
    )
    list_filter = ("completed_flag", "fr_type")
    search_fields = ("fr_number", "pd_reference_number", "vendor_code")
    raw_id_fields = ("intervention",)


@admin.register(dm.AuditFinding)
class AuditFindingAdmin(DatamartAdmin):
    list_display = ("reference_number", "finding_number", "partner_name", "title", "amount")
    list_filter = ("engagement_type",)
    search_fields = ("reference_number", "partner_name", "vendor_number", "title")
    raw_id_fields = ("partner", "engagement")


@admin.register(dm.ReportedIndicator)
class ReportedIndicatorAdmin(DatamartAdmin):
    list_display = (
        "pd_reference_number",
        "report_number",
        "report_status",
        "period_end",
        "indicator",
        "location",
    )
    list_filter = ("report_type", "report_status")
    search_fields = ("pd_reference_number", "partner_name", "vendor_number", "indicator")
    raw_id_fields = ("partner", "intervention")

    def site_urls(self):
        """The eTools reporting checks page, at admin/datamart/reporting-checks/
        (``NeuroDBAdminSite.get_urls``)."""
        return [
            path(
                "datamart/reporting-checks/",
                self.admin_site.admin_view(reporting_checks_view),
                name="datamart_reporting_checks",
            )
        ]


def reporting_checks_view(request):
    """eTools reporting checks (Administrators only): how the partner report data answers the questions
    the "Reached" rule depends on, with a verdict and what NeuroDB does in each case. Read-only."""
    if role_of(request.user) != ADMIN:
        raise Http404
    context = {
        **admin.site.each_context(request),
        "title": _("eTools reporting checks"),
        "subtitle": None,
        "checks": checks.run_all(),
        "checked_at": timezone.now(),
        "rows_url": reverse("admin:datamart_reportedindicator_changelist"),
        "health_url": reverse("reports:data_health"),
        "opts": dm.ReportedIndicator._meta,
    }
    return render(request, "admin/datamart/reporting_checks.html", context)


@admin.register(dm.TPMActivity)
class TPMActivityAdmin(DatamartAdmin):
    list_display = (
        "task_reference_number",
        "partner_name",
        "pd_reference_number",
        "tpm_name",
        "status",
        "date",
    )
    list_filter = ("status", "is_programmatic_visit")
    search_fields = ("task_reference_number", "visit_reference_number", "partner_name", "pd_reference_number")
    raw_id_fields = ("partner", "intervention")


@admin.register(dm.ProgrammaticVisit)
class ProgrammaticVisitAdmin(DatamartAdmin):
    list_display = ("travel_reference_number", "travel_type", "partner_name", "partnership_number", "date")
    list_filter = ("travel_type",)
    search_fields = ("travel_reference_number", "partner_name", "partnership_number", "primary_traveler")
    raw_id_fields = ("partner", "intervention")


@admin.register(dm.PlannedVisits)
class PlannedVisitsAdmin(DatamartAdmin):
    list_display = ("pd_reference_number", "year", "q1", "q2", "q3", "q4")
    list_filter = ("year",)
    search_fields = ("pd_reference_number",)
    raw_id_fields = ("partner", "intervention")


@admin.register(dm.PartnerHACTYear)
class PartnerHACTYearAdmin(DatamartAdmin):
    list_display = (
        "partner_name",
        "year",
        "risk_rating",
        "cash_transfers",
        "pv_completed",
        "pv_required",
        "sc_completed",
        "sc_required",
    )
    list_filter = ("year", "risk_rating")
    search_fields = ("partner_name", "vendor_number")
    raw_id_fields = ("partner",)


@admin.register(dm.PDActivity)
class PDActivityAdmin(DatamartAdmin):
    list_display = ("pd_reference_number", "code", "name", "unicef_cash", "cso_cash")
    search_fields = ("pd_reference_number", "name", "code")
    raw_id_fields = ("intervention",)


@admin.register(dm.DatamartDocument)
class DatamartDocumentAdmin(ReadOnlyModelAdmin):
    list_display = ("dataset", "title", "date", "partner", "intervention", "synced_at")
    list_filter = ("dataset",)
    search_fields = ("title", "record_key")
    raw_id_fields = ("partner", "intervention")
    readonly_fields = ("data",)
    list_per_page = 50


class IndicatorFlagForm(forms.ModelForm):
    """The two roles of a flag, in plain words: whether the indicator counts children (or the title
    decides), and whether it is its programme document's unique beneficiaries indicator."""

    counts_children = forms.TypedChoiceField(
        label=_("Counts children"),
        choices=(
            ("", _("The title decides")),
            ("true", _("Counts children")),
            ("false", _("Does not count children")),
        ),
        coerce=lambda v: v == "true",
        empty_value=None,
        required=False,
    )

    class Meta:
        model = dm.IndicatorFlag
        fields = ("source", "key", "label", "counts_children", "unique_beneficiaries", "intervention", "note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        value = self.initial.get("counts_children")
        self.initial["counts_children"] = "" if value is None else ("true" if value else "false")

    def clean(self):
        data = super().clean()
        if data.get("unique_beneficiaries"):
            if data.get("source") != dm.IndicatorFlag.Source.ETOOLS:
                self.add_error(
                    "unique_beneficiaries",
                    _("Only an eTools PD indicator can be a unique beneficiaries indicator."),
                )
            if not data.get("intervention"):
                self.add_error("intervention", _("Choose the programme document this indicator belongs to."))
        return data


@admin.register(dm.IndicatorFlag)
class IndicatorFlagAdmin(ModelAdmin):
    """The one Datamart table people edit: which indicators count children on the overview, and each
    programme document's unique beneficiaries indicator (its headline children reached)."""

    form = IndicatorFlagForm
    list_display = (
        "source",
        "key",
        "label",
        "counts_children",
        "unique_beneficiaries",
        "intervention",
        "updated_by",
        "updated_at",
    )
    list_filter = ("source", "counts_children", "unique_beneficiaries")
    search_fields = ("key", "label", "note")
    readonly_fields = ("updated_by", "updated_at")
    raw_id_fields = ("intervention",)
    list_per_page = 50

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)
