"""Read-only admin over the eTools Datamart tables (refreshed by ``sync_etools_datamart``), the eTools
reporting checks page (admin/datamart/reporting-checks/, Administrators; validators read it), Raw eTools data
(admin/datamart/raw/, validators: ``raw_admin``), and the tables people edit: the indicator flags and the HACT
visit plan's comments."""

import time

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.text import get_text_list
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from neurodb.accounts.roles import ADMIN, can_validate, role_of
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
    list_display = ("title", "pd_reference_number", "section_name", "location_name", "is_active", "listed")
    list_filter = ("section_name", "is_active", "listed", "is_high_frequency")
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


@admin.register(dm.ActionPointStatus)
class ActionPointStatusAdmin(ReadOnlyModelAdmin):
    """The action point history (``datamart.ap_history``), read-only: a row for an action point on each day
    its status, priority, due date or completion date changed, or it left the Datamart or came back. No text,
    no names: the action point by its eTools id."""

    list_display = (
        "source_id",
        "day",
        "in_datamart",
        "status",
        "high_priority",
        "due_date",
        "date_of_completion",
        "related_module",
        "section",
    )
    list_filter = ("day", "status", "related_module", "in_datamart", "high_priority")
    date_hierarchy = "day"
    search_fields = ("=source_id",)
    search_help_text = _("The action point's eTools id")
    raw_id_fields = ("intervention", "partner")
    list_per_page = 50
    show_full_result_count = False  # a table that grows every day: no second count


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


STARTING_S = 120  # how long a run started counts as starting while its process has written no row yet


def reporting_checks_view(request):
    """eTools reporting checks (Administrators, and read-only for validators: ``roles.can_validate``): how the
    partner report data answers the questions the "Reached" rule depends on, with a verdict and what NeuroDB
    does in each case. Read-only; only an Administrator runs them (*Run the checks now*).

    The page shows the result the last run of the job stored (``checks.run_job``: every night, or *Run
    the checks now*, which posts here and starts it in the background). Only while no run has stored one,
    and none is running, does the page run the checks itself, within its own time. A run started a moment
    ago (the button, Run a job or the schedule: ``background.started_at``) counts as starting until its
    process has written its row (it needs a few seconds), so no page runs the checks itself beside it.
    A result an earlier release stored is read against this release's checks (``checks.read_stored``): the
    page says so above the cards, and a check added since reads "Not run yet", until a run replaces it."""
    from neurodb.core.models import ScheduledJob, SyncRun
    from neurodb.integrations import background

    administrator = role_of(request.user) == ADMIN
    if not (administrator or can_validate(request.user)):
        raise Http404
    job = SyncRun.Job.REPORTING_CHECKS
    here = reverse("admin:datamart_reporting_checks")
    if request.method == "POST":
        if not administrator:  # validators read the checks; only Administrators run them
            raise PermissionDenied
        if background.is_running(job):
            messages.warning(request, _("The eTools reporting checks are already running."))
        else:
            background.start_command("reporting_checks", "--triggered-by", request.user.get_username())
            messages.success(
                request,
                _(
                    "The eTools reporting checks started in the background. Reload this page in a few "
                    "minutes to see their result."
                ),
            )
        return redirect(here)
    running = None
    if background.is_running(job):  # a row left running by a stopped process is closed there
        running = SyncRun.objects.filter(job=job, status=SyncRun.Status.RUNNING).order_by("-started_at")
        running = running.first()
    stored = checks.latest_run()
    last = SyncRun.objects.filter(job=job).exclude(finished_at=None).order_by("-finished_at", "-id").first()
    failed = last if last and last.status == SyncRun.Status.FAILED and last != stored else None
    asked = background.started_at("reporting_checks")
    if asked is not None and (
        running  # its row is there: the page follows the run itself
        or time.time() - asked > STARTING_S
        or (last and last.started_at.timestamp() >= asked)  # it already ended
    ):
        background.forget_started("reporting_checks")
        asked = None
    starting = asked is not None and not running
    outdated = None
    if stored:
        read = checks.read_stored(stored.details["checks"])
        found, checked_at = read.cards, stored.finished_at
        outdated = read if read.outdated else None
    elif running or starting:
        found, checked_at = [], None
    else:
        found, checked_at = checks.run_all(), timezone.now()
    context = {
        **admin.site.each_context(request),
        "title": _("eTools reporting checks"),
        "subtitle": None,
        "checks": found,
        "checked_at": checked_at,
        "stored": stored,
        "running": running,
        "starting": starting,
        "nightly": ScheduledJob.objects.filter(command="reporting_checks", enabled=True).exists(),
        "failed": failed,
        "outdated": outdated,
        "outdated_missing": get_text_list(outdated.missing, _("and")) if outdated else "",
        "outdated_changed": get_text_list(outdated.changed, _("and")) if outdated else "",
        # the run and the rows' table open for Administrators only: a validator gets no link that refuses them
        "failed_url": reverse("admin:core_syncrun_change", args=[failed.pk])
        if failed and administrator
        else None,
        "run_url": here,
        "can_run": administrator,
        "page_seconds": int(checks.PAGE_BUDGET_S),
        "job_minutes": int(checks.JOB_BUDGET_S // 60),
        "rows_url": reverse("admin:datamart_reportedindicator_changelist") if administrator else "",
        "health_url": reverse("reports:data_health"),
    }
    if administrator:  # the header's links to the Datamart's tables
        context["opts"] = dm.ReportedIndicator._meta
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


@admin.register(dm.DisaggregationLabel)
class DisaggregationLabelAdmin(ReadOnlyModelAdmin):
    """The PRP disaggregation labels the partner reports sync read (read-only: the sync keeps them)."""

    list_display = ("label", "kind", "prp_id", "disaggregation_id", "active", "not_found", "synced_at")
    list_filter = ("kind", "active", "not_found")
    search_fields = ("label",)
    list_per_page = 50


@admin.register(dm.DatamartDocument)
class DatamartDocumentAdmin(ReadOnlyModelAdmin):
    list_display = ("dataset", "title", "date", "partner", "intervention", "synced_at")
    list_filter = ("dataset",)
    search_fields = ("title", "record_key")
    raw_id_fields = ("partner", "intervention")
    readonly_fields = ("data",)
    list_per_page = 50

    def site_urls(self):
        """Raw eTools data, at admin/datamart/raw/ (``NeuroDBAdminSite.get_urls``; ``raw_admin``), and Compare
        with Power BI, at admin/datamart/powerbi/ (``powerbi_admin``)."""
        from . import powerbi_admin, raw_admin

        view = self.admin_site.admin_view
        return [
            path("datamart/powerbi/", view(powerbi_admin.index_view), name="datamart_powerbi"),
            path(
                "datamart/powerbi/<int:pk>/", view(powerbi_admin.upload_view), name="datamart_powerbi_upload"
            ),
            path(
                "datamart/powerbi/<int:pk>/again/",
                view(powerbi_admin.again_view),
                name="datamart_powerbi_again",
            ),
            path(
                "datamart/powerbi/<int:pk>/differences.xlsx",
                view(powerbi_admin.xlsx_view),
                name="datamart_powerbi_xlsx",
            ),
            path(
                "datamart/powerbi/<int:pk>/<int:export_pk>/",
                view(powerbi_admin.export_view),
                name="datamart_powerbi_export",
            ),
            path("datamart/raw/", view(raw_admin.index_view), name="datamart_raw"),
            path("datamart/raw/rest/read/", view(raw_admin.rest_read_view), name="datamart_raw_rest_read"),
            path(
                "datamart/raw/partner_reports/no-longer-sent/",
                view(raw_admin.unsent_view),
                name="datamart_raw_unsent",
            ),
            path("datamart/raw/<str:source>/", view(raw_admin.source_view), name="datamart_raw_source"),
            path(
                "datamart/raw/<str:source>/<int:pk>/", view(raw_admin.record_view), name="datamart_raw_record"
            ),
            path("datamart/raw/<str:source>/rows.csv", view(raw_admin.csv_view), name="datamart_raw_csv"),
            path("datamart/raw/<str:source>/fields/", view(raw_admin.field_view), name="datamart_raw_field"),
        ]


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
            elif not data.get("intervention"):
                self.add_error("intervention", _("Choose the programme document this indicator belongs to."))
            elif data.get("key"):
                self._check_indicator(data)
        return data

    def _check_indicator(self, data) -> None:
        """The key must name one of the programme document's indicators, and one that counts people (as
        on the programme document page): otherwise the flag would change nothing."""
        from . import headline
        from . import monitoring as pd_monitoring

        pd = data["intervention"]
        key = str(data["key"]).strip()
        rows = pd_monitoring.indicators(pd_monitoring.Filters(pd_ids=[pd.pk], scope="all"))
        row = next((r for r in rows if r.key == key), None)
        if row is None:
            self.add_error(
                "key",
                _(
                    "This is not the eTools id of one of this programme document's indicators. Choosing "
                    "the indicator on the programme document page avoids this."
                ),
            )
        elif not headline.is_number(row):
            self.add_error(
                "unique_beneficiaries",
                _(
                    "A percentage or a ratio cannot count beneficiaries: choose an indicator that counts "
                    "people."
                ),
            )
        elif not data.get("label"):
            data["label"] = row.title[:1024]


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


@admin.register(dm.VisitPlanComment)
class VisitPlanCommentAdmin(ModelAdmin):
    """The sections' comments on the HACT visit plan (Field monitoring page), one per programme document
    and year. Written on the page by Administrators and Section editors of the PD's sections; corrected
    or removed here. Every change is in the audit trail, page edits included: who and when (and, for a
    change made here, which field), never the text, which only the comment itself holds."""

    list_display = ("pd_reference", "year", "updated_at")
    list_filter = ("year",)
    search_fields = ("pd_reference",)
    fields = ("intervention", "pd_reference", "year", "text", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")
    raw_id_fields = ("intervention",)
    list_per_page = 50

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        if obj.intervention_id and not obj.pd_reference:
            obj.pd_reference = (obj.intervention.number or "")[:64]
        super().save_model(request, obj, form, change)
