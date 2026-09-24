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
