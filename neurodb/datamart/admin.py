"""Read-only admin over the eTools Datamart tables (refreshed by ``sync_etools_datamart``)."""

from django.contrib import admin

from neurodb.web.admin_helpers import ReadOnlyModelAdmin

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
