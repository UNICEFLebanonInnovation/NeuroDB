"""Read-only admin over the eTools Datamart tables (refreshed by ``sync_etools_datamart``), the eTools
reporting checks page (admin/datamart/reporting-checks/, Administrators; validators read it), Raw eTools data
(admin/datamart/raw/, validators: ``raw_admin``), and the tables people edit: the indicator flags, the HACT
visit plan's comments and the eTools master indicators with their PD indicators."""

import time
from urllib.parse import urlencode

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.text import Truncator, get_text_list
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from unfold.forms import BaseDialogForm
from unfold.widgets import (
    UnfoldAdminCheckboxSelectMultipleWidget,
    UnfoldAdminRadioSelectWidget,
    UnfoldAdminSelectWidget,
)

from neurodb.accounts.roles import ADMIN, can_validate, role_of
from neurodb.core import years
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import checks, curation, master_links, masters
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
        """Raw eTools data, at admin/datamart/raw/ (``NeuroDBAdminSite.get_urls``; ``raw_admin``)."""
        from . import raw_admin

        view = self.admin_site.admin_view
        return [
            path("datamart/raw/", view(raw_admin.index_view), name="datamart_raw"),
            path("datamart/raw/rest/read/", view(raw_admin.rest_read_view), name="datamart_raw_rest_read"),
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


# ------------------------------------------------------------------------------- eTools master indicators
Method, Role, Status = dm.EtoolsMaster.Method, dm.EtoolsMasterLink.Role, dm.EtoolsMasterLink.Status
MASTER = dm.EtoolsMaster.Status
STATUS_TONES = {"proposed": "info", "active": "ok", "dismissed": "muted", "archived": "muted"}
LINK_TONES = {"suggested": "info", "confirmed": "ok", "rejected": "muted"}
SHOWN_IN_MESSAGES = 3  # reasons spelled out in a message, then "and N more"


def _left(left) -> str:
    """The links or indicators an action left as they were, with why: the first few, then how many more."""
    shown = "; ".join(f"{what}: {why}" for what, why in left[:SHOWN_IN_MESSAGES])
    return f"{shown}; and {len(left) - SHOWN_IN_MESSAGES} more" if len(left) > SHOWN_IN_MESSAGES else shown


def _link_name(link) -> str:
    return f"{link.pd_number or 'PD'} {Truncator(link.title or link.ident).chars(60)}"


class YearTargetFilter(admin.SimpleListFilter):
    """Whether the master has this year's target, which its status is measured against (the idea of the
    ActivityInfo masters' AWP target filter): a target above 0, as :func:`neurodb.datamart.masters.has_target`
    reads it. "Listed this year, no target" are the masters Data health's warning counts
    (:func:`neurodb.datamart.masters.without_target`), the list its line opens."""

    title = _("target this year")
    parameter_name = "target"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Has a target")),
            ("no", _("No target")),
            ("listed", _("Listed this year, no target")),
        )

    def queryset(self, request, queryset):
        year = years.this_year()
        typed = dm.EtoolsMasterYear.objects.filter(master=OuterRef("pk"), year=year, target__gt=0)
        if self.value() == "yes":
            return queryset.filter(Exists(typed))
        if self.value() == "no":
            return queryset.exclude(Exists(typed))
        if self.value() == "listed":
            return queryset.filter(pk__in=[m.pk for m in masters.without_target(year)])
        return queryset


class MasterYearForm(forms.ModelForm):
    """A master's settings for one year, refusing a target of 0 or less (the status reads it as none, as the
    Master indicators tab refuses it)."""

    class Meta:
        model = dm.EtoolsMasterYear
        fields = ("year", "code", "target", "is_active", "note")

    def clean_target(self):
        target = self.cleaned_data.get("target")
        if target is not None and not target > 0:
            raise forms.ValidationError(_("The target is a number above 0 (leave it blank for no target)."))
        return target


class MasterYearInline(TabularInline):
    """A master's settings for each calendar year: its AWP/RWP code and the target staff type."""

    model = dm.EtoolsMasterYear
    form = MasterYearForm
    extra = 0
    fields = ("year", "code", "target", "is_active", "note")


class OneTotalForm(forms.ModelForm):
    """A link's form that keeps a PD indicator in one total: making it count in the total while another
    master's total holds it (confirmed or suggested) is a form error. The database refuses it too
    (``one_total_home``), but only with a server error once the indicator's fields are fixed (read-only)."""

    def clean(self):
        data = super().clean()
        link = self.instance
        pd = data["pd"].pk if data.get("pd") else link.pd_id
        ident = data.get("ident") or link.ident
        status = data.get("status") or link.status
        if (
            data.get("role") == Role.TOTAL
            and (link.pk is None or "role" in self.changed_data)
            and status != Status.REJECTED
            and pd
        ):
            other = (
                dm.EtoolsMasterLink.objects.filter(
                    pd_id=pd,
                    ident=ident,
                    role=Role.TOTAL,
                    status__in=[Status.SUGGESTED, Status.CONFIRMED],
                )
                .exclude(pk=link.pk)
                .select_related("master")
                .first()
            )
            if other is not None:
                self.add_error(
                    "role",
                    _(
                        "This PD indicator is in the total of %(name)s already (%(status)s): it can be in "
                        "one total only."
                    )
                    % {"name": other.master.name, "status": other.get_status_display().lower()},
                )
        return data


def _role_set_by(link, form, username: str) -> None:
    """A person changed a suggestion's role: keep who (``confirmed_by``), so the nightly job keeps the role
    (:func:`neurodb.datamart.master_links.set_by_person`)."""
    if link.status == Status.SUGGESTED and "role" in form.changed_data:
        link.confirmed_by = username[:150]


class MasterLinkForm(OneTotalForm):
    """A link on its master's page: what it counts as, and its place. Its status changes through the
    actions, which read the indicator again (Confirm and Reject, on the links' list)."""

    class Meta:
        model = dm.EtoolsMasterLink
        fields = ("role", "sequence")

    def _post_clean(self):
        # an unchanged link is not checked again: the check reads its programme document's indicators, with
        # their Reached, which would make saving a master with many links slow
        if self.instance.pk is not None and not self.has_changed():
            return
        super()._post_clean()


class EtoolsMasterLinkForm(OneTotalForm):
    """A link on its own page. A new one is a person's decision, so it starts confirmed (it counts, and the
    nightly job, which rewrites suggestions, never touches it) or is rejected; it cannot be added as a
    suggestion."""

    class Meta:
        model = dm.EtoolsMasterLink
        fields = ("master", "pd", "ident", "role", "status", "sequence")  # the admin's own list wins

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        status = self.fields.get("status")
        if status is not None and self.instance.pk is None:
            status.choices = [(s.value, s.label) for s in (Status.CONFIRMED, Status.REJECTED)]
            status.initial = Status.CONFIRMED


class MasterLinkInline(TabularInline):
    """A master's PD indicators. They are added with Add PD indicators (or by the nightly suggestions)."""

    model = dm.EtoolsMasterLink
    form = MasterLinkForm
    extra = 0
    fields = ("pd_number", "title", "unit_class", "role", "status", "score", "reason", "sequence")
    readonly_fields = ("pd_number", "title", "unit_class", "status", "score", "reason")
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class AddPDIndicatorsForm(forms.Form):
    """The *Add PD indicators* wizard: the section's PD indicators of this year that can join the master
    (:func:`neurodb.datamart.curation.offered`), and the one role they all take. ``named``: whether a PD
    indicator of this year names the master's section at all (when none does, the wizard says to check it)."""

    role = forms.ChoiceField(label=_("Counts as"), widget=UnfoldAdminSelectWidget)
    indicators = forms.MultipleChoiceField(widget=UnfoldAdminCheckboxSelectMultipleWidget)

    def __init__(self, master, offers, year, *args, named=True, **kwargs):
        super().__init__(*args, **kwargs)
        roles = [Role.TOTAL, Role.SHOWN]
        if master.method in masters.WITH_PARTS:
            roles = [Role.TOTAL, Role.NUMERATOR, Role.DENOMINATOR, Role.SHOWN]
        self.fields["role"].choices = [(role.value, role.label) for role in roles]
        self.fields["role"].help_text = _(
            "Every indicator ticked counts this way. They count at once: a person chose them."
        )
        self.fields["indicators"].label = _(
            "PD indicators of %(section)s in the programme documents running in %(year)s"
        ) % {"section": master.section, "year": year}
        self.fields["indicators"].choices = [(offer.value, _offer_label(offer)) for offer in offers]
        if offers:
            help_text = _(
                "Indicators that already count in another master's total, of a type this master cannot "
                "combine, or that it holds already are not listed."
            )
        elif not named:
            help_text = _(
                "No PD indicator of a programme document running in %(year)s names the section %(section)s. "
                "Check the master's section as eTools spells it on the PD indicators (after an eTools "
                "rename, use Change section on the master indicators list)."
            ) % {"year": year, "section": master.section}
        else:
            help_text = _(
                "No PD indicator of this section can be added: each one counts in a master already, is held "
                "by this one, or is of a type it cannot combine."
            )
        self.fields["indicators"].help_text = help_text


def _offer_label(offer) -> str:
    row = offer.row
    parts = [
        row.pd.number or _("No PD number"),
        offer.partner,
        dm.EtoolsMasterLink.UnitClass(offer.unit).label,
    ]
    if row.cp_output:
        parts.append(_("CP output: %(output)s") % {"output": Truncator(row.cp_output).chars(80)})
    text = f"{row.title} ({' · '.join(str(p) for p in parts if p)})"
    return f"{text}. {offer.note}" if offer.note else text


class MergeForm(forms.Form):
    """The master indicator the others are merged into."""

    keep = forms.ModelChoiceField(
        label=_("Keep"),
        queryset=dm.EtoolsMaster.objects.none(),
        widget=UnfoldAdminRadioSelectWidget,
        empty_label=None,
        help_text=_(
            "The others' PD indicators and the years' settings it lacks move to it; the others are archived "
            "(a proposal is dismissed, so NeuroDB never proposes it again)."
        ),
    )

    def __init__(self, chosen, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active = [m for m in chosen if m.status == MASTER.ACTIVE]
        self.fields["keep"].queryset = dm.EtoolsMaster.objects.filter(pk__in=[m.pk for m in active])
        if active:  # the one with the most confirmed PD indicators
            self.fields["keep"].initial = max(
                active, key=lambda m: (getattr(m, "confirmed_links", 0), -m.pk)
            ).pk


class SectionForm(forms.Form):
    """The eTools section the masters move to, as the PD indicators name it now."""

    section = forms.ChoiceField(
        label=_("New eTools section"),
        widget=UnfoldAdminSelectWidget,
        help_text=_(
            "After eTools renamed a section: its masters keep their PD indicators, codes and targets."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["section"].choices = [(name, name) for name in curation.sections()]


class SuggestNowForm(BaseDialogForm):
    """The confirmation step of "Suggest now" (it makes the action a POST)."""


@admin.register(dm.EtoolsMaster)
class EtoolsMasterAdmin(ModelAdmin):
    """The eTools master indicators of each section: one figure a year combined from the partners' PD
    indicators linked to it, by its method. Only confirmed links count; NeuroDB suggests links and proposes
    masters every night (Suggest now runs it at once), and staff accept, confirm or dismiss them here or on
    the Master indicators tab. ``updated_by`` and ``confirmed_by`` are shown here only."""

    list_display = (
        "section",
        "cp_output_short",
        "name",
        "method",
        "status_badge",
        "target_this_year",
        "confirmed_count",
        "suggested_count",
        "origin",
    )
    list_display_links = ("name",)
    list_filter = ("section", "method", "status", "origin", YearTargetFilter)
    search_fields = ("name", "keywords", "cp_output", "section")
    fields = (
        "section",
        "name",
        "unit",
        "cp_output",
        "keywords",
        "method",
        "pd_parts_add_up",
        "status",
        "sequence",
        "note",
        "origin",
        "proposal_key",
        "activityinfo_master_id",
        "updated_by",
        "updated_at",
    )
    readonly_fields = ("origin", "proposal_key", "activityinfo_master_id", "updated_by", "updated_at")
    inlines = (MasterYearInline, MasterLinkInline)
    change_form_template = "admin/datamart/etoolsmaster/change_form.html"
    actions = (
        "accept_proposals",
        "dismiss_proposals",
        "confirm_all",
        "confirm_strong",
        "merge_selected",
        "change_section",
    )
    actions_list = ("suggest_now",)
    list_per_page = 50

    def get_queryset(self, request):
        target = dm.EtoolsMasterYear.objects.filter(master=OuterRef("pk"), year=years.this_year())
        return (
            super()
            .get_queryset(request)
            .annotate(
                confirmed_links=Count("links", filter=Q(links__status=Status.CONFIRMED), distinct=True),
                suggested_links=Count("links", filter=Q(links__status=Status.SUGGESTED), distinct=True),
                year_target=Subquery(target.values("target")[:1]),
            )
        )

    @admin.display(description=_("CP output"), ordering="cp_output")
    def cp_output_short(self, obj):
        return Truncator(obj.cp_output).chars(60) if obj.cp_output else "—"

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), STATUS_TONES.get(obj.status))

    @admin.display(description=_("Target this year"), ordering="year_target")
    def target_this_year(self, obj):
        value = getattr(obj, "year_target", None)
        if not value:
            return badge(_("No target"), "warn")
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"

    @admin.display(description=_("Confirmed"), ordering="confirmed_links")
    def confirmed_count(self, obj):
        return getattr(obj, "confirmed_links", 0)

    @admin.display(description=_("Suggestions"), ordering="suggested_links")
    def suggested_count(self, obj):
        return getattr(obj, "suggested_links", 0)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        links = reverse("admin:datamart_etoolsmasterlink_changelist")
        query = urlencode({"master__id__exact": object_id})
        extra_context = {**(extra_context or {}), "links_url": f"{links}?{query}"}
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        if formset.model is dm.EtoolsMasterYear:
            for year_row in formset.save(commit=False):
                year_row.updated_by = request.user.get_username()
                year_row.save()
                left = 0 if year_row.is_active else masters.counted_running(form.instance, year_row.year)
                if left:
                    self.message_user(
                        request,
                        _(
                            "%(name)s is not shown in %(year)s: the %(n)s PD indicator(s) it counts of "
                            "programme documents running in %(year)s count nowhere in %(year)s (the Master "
                            "indicators tab lists them under Not in a master). Tick shown this year again to "
                            "count them, or take them out of it, which takes them out of its other years too."
                        )
                        % {"name": form.instance.name, "year": year_row.year, "n": left},
                        messages.WARNING,
                    )
            for gone in formset.deleted_objects:
                gone.delete()
            return
        if formset.model is dm.EtoolsMasterLink:
            for link_form in formset.forms:
                if link_form.instance.pk is not None and link_form.has_changed():
                    _role_set_by(link_form.instance, link_form, request.user.get_username())
        super().save_formset(request, form, formset, change)

    # --------------------------------------------------------------------------------- Add PD indicators
    def get_urls(self):
        return [
            path(
                "<int:pk>/add-pd-indicators/",
                self.admin_site.admin_view(self.add_pd_indicators),
                name="datamart_etoolsmaster_add_indicators",
            ),
            *super().get_urls(),
        ]

    def add_pd_indicators(self, request, pk):
        """*Add PD indicators*, as the ActivityInfo masters' Add sub-indicators: tick the section's PD
        indicators of this year that can join the master, choose what they count as, and they are linked,
        confirmed (:func:`neurodb.datamart.curation.add`)."""
        master = get_object_or_404(dm.EtoolsMaster, pk=pk)
        if not self.has_change_permission(request, master):
            raise PermissionDenied
        year = years.this_year()
        offers = curation.offered(master, year)
        # read again only when nothing is offered: to say whether the section is named at all
        named = bool(offers) or curation.section_named(master.section, year)
        form = AddPDIndicatorsForm(master, offers, year, request.POST or None, named=named)
        if request.method == "POST" and form.is_valid():
            keys = [key for key in map(curation.parse_key, form.cleaned_data["indicators"]) if key]
            done = curation.add(master, keys, form.cleaned_data["role"], request.user.get_username())
            if done.added:
                self.message_user(
                    request,
                    _("%(n)s PD indicator(s) added, confirmed: they count from now on.") % {"n": done.added},
                    messages.SUCCESS,
                )
            if done.left:
                self.message_user(
                    request,
                    _("%(n)s left out: %(why)s") % {"n": len(done.left), "why": _left(done.left)},
                    messages.WARNING,
                )
            return redirect(reverse("admin:datamart_etoolsmaster_change", args=[pk]))
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "master": master,
            "opts": self.model._meta,
            "title": _("Add PD indicators to %(name)s") % {"name": master.name},
        }
        return render(request, "admin/datamart/etoolsmaster/add_indicators.html", context)

    # ------------------------------------------------------------------------------------------- actions
    @admin.action(description=_("Accept the proposed master indicators"), permissions=("change",))
    def accept_proposals(self, request, queryset):
        """A proposal becomes active and its suggested PD indicators confirmed, in one click."""
        done = master_links.accept(queryset.order_by("section", "name", "pk"), request.user.get_username())
        if done.accepted:
            self.message_user(
                request,
                _("%(n)s master indicator(s) accepted, %(links)s PD indicator(s) confirmed.")
                % {"n": len(done.accepted), "links": done.links.confirmed},
                messages.SUCCESS,
            )
        refused = [(m.name, why) for m, why in done.refused]
        if refused:
            self.message_user(
                request,
                _("%(n)s not accepted: %(why)s") % {"n": len(refused), "why": _left(refused)},
                messages.WARNING,
            )
        if done.links.left:
            left = [(_link_name(link), why) for link, why in done.links.left]
            self.message_user(
                request,
                _("%(n)s PD indicator(s) left as suggestions: %(why)s")
                % {"n": len(left), "why": _left(left)},
                messages.WARNING,
            )

    @admin.action(description=_("Dismiss the proposed master indicators"), permissions=("change",))
    def dismiss_proposals(self, request, queryset):
        """A dismissed proposal is kept, so NeuroDB never proposes it again."""
        proposed = queryset.filter(status=MASTER.PROPOSED)
        others = queryset.count() - proposed.count()
        done = master_links.dismiss(list(proposed), request.user.get_username())
        self.message_user(
            request,
            _("%(n)s proposal(s) dismissed: NeuroDB will not propose them again.") % {"n": done},
            messages.SUCCESS,
        )
        if others:
            self.message_user(
                request,
                _("%(n)s were not proposals and were left as they were.") % {"n": others},
                messages.WARNING,
            )

    @admin.action(description=_("Confirm all suggestions"), permissions=("change",))
    def confirm_all(self, request, queryset):
        self._confirm(request, queryset, strong=False)

    @admin.action(description=_("Confirm suggestions scoring 0.9 or more"), permissions=("change",))
    def confirm_strong(self, request, queryset):
        self._confirm(request, queryset, strong=True)

    def _confirm(self, request, queryset, strong: bool) -> None:
        """Confirm the suggested PD indicators of the selected active masters (only the strong matches, the
        same structure or a replacement, with ``strong``). A proposal's are confirmed by accepting it."""
        proposed = queryset.filter(status=MASTER.PROPOSED).count()
        links = dm.EtoolsMasterLink.objects.filter(
            master__in=queryset.filter(status=MASTER.ACTIVE), status=Status.SUGGESTED
        )
        if strong:
            links = links.filter(score__gte=master_links.STRONG)
        links = links.select_related("master").order_by("master__section", "master__name", "sequence", "id")
        done = master_links.confirm(list(links), request.user.get_username())
        self.message_user(
            request,
            _("%(n)s suggestion(s) confirmed: they count from now on.") % {"n": done.confirmed},
            messages.SUCCESS,
        )
        if done.left:
            left = [(_link_name(link), why) for link, why in done.left]
            self.message_user(
                request,
                _("%(n)s left as suggestions: %(why)s") % {"n": len(left), "why": _left(left)},
                messages.WARNING,
            )
        if proposed:
            self.message_user(
                request,
                _(
                    "%(n)s proposed master indicator(s) skipped: accept a proposal to confirm its PD "
                    "indicators (Accept the proposed master indicators)."
                )
                % {"n": proposed},
                messages.WARNING,
            )

    @admin.action(description=_("Merge the selected master indicators"), permissions=("change",))
    def merge_selected(self, request, queryset):
        """Merge masters of one section and one method into the one kept (chosen on the next page)."""
        chosen = list(queryset.order_by("section", "name", "pk"))
        problem = curation.merge_problem(chosen)
        if problem:
            self.message_user(request, problem, messages.ERROR)
            return None
        form = MergeForm(chosen, request.POST if "apply" in request.POST else None)
        if form.is_valid():
            keep = form.cleaned_data["keep"]
            done = curation.merge(keep, [m for m in chosen if m.pk != keep.pk], request.user.get_username())
            if isinstance(done, str):
                self.message_user(request, done, messages.ERROR)
                return None
            self.message_user(
                request,
                _(
                    "%(n)s master indicator(s) merged into %(name)s: %(moved)s PD indicator(s) moved, "
                    "%(dropped)s it held already dropped, %(years)s year(s) of settings moved."
                )
                % {
                    "n": len(done.merged),
                    "name": keep.name,
                    "moved": done.moved,
                    "dropped": done.dropped,
                    "years": done.years,
                },
                messages.SUCCESS,
            )
            if done.shown:
                self.message_user(
                    request,
                    _(
                        "%(n)s year(s) not shown on the master indicators merged (shown this year unticked) "
                        "are shown on %(name)s, which goes on."
                    )
                    % {"n": done.shown, "name": keep.name},
                    messages.WARNING,
                )
            if done.confirmed:
                self.message_user(
                    request,
                    _("%(n)s PD indicator(s) of the proposals confirmed in %(name)s: they count from now on.")
                    % {"n": done.confirmed, "name": keep.name},
                    messages.SUCCESS,
                )
            if done.left:
                left = [(_link_name(link), why) for link, why in done.left]
                self.message_user(
                    request,
                    _("%(n)s PD indicator(s) of the proposals left as suggestions: %(why)s")
                    % {"n": len(left), "why": _left(left)},
                    messages.WARNING,
                )
            return None
        return self._action_page(
            request,
            chosen,
            form,
            "merge_selected",
            _("Merge master indicators"),
            _("These master indicators of %(section)s become one.") % {"section": chosen[0].section},
            _("Merge"),
        )

    @admin.action(description=_("Change section (after an eTools rename)"), permissions=("change",))
    def change_section(self, request, queryset):
        """Move masters to the eTools section their PD indicators are in now, after eTools renamed it."""
        chosen = list(queryset.order_by("section", "name", "pk"))
        form = SectionForm(request.POST if "apply" in request.POST else None)
        if form.is_valid():
            moved, left = curation.move(chosen, form.cleaned_data["section"], request.user.get_username())
            self.message_user(
                request,
                _("%(n)s master indicator(s) moved to %(section)s.")
                % {"n": moved, "section": form.cleaned_data["section"]},
                messages.SUCCESS,
            )
            if left:
                why = _left([(m.name, reason) for m, reason in left])
                self.message_user(
                    request, _("%(n)s not moved: %(why)s") % {"n": len(left), "why": why}, messages.WARNING
                )
            return None
        return self._action_page(
            request,
            chosen,
            form,
            "change_section",
            _("Change section"),
            _("Move these master indicators to the section eTools names their PD indicators with now."),
            _("Move"),
        )

    def _action_page(self, request, chosen, form, name, title, intro, submit) -> HttpResponse:
        """The page between choosing an action and applying it (a choice to make first)."""
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "intro": intro,
            "form": form,
            "chosen": chosen,
            "action_name": name,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "submit_label": submit,
            "opts": self.model._meta,
        }
        return render(request, "admin/datamart/etoolsmaster/action_form.html", context)

    @action(
        description=_("Suggest now"),
        url_path="suggest-now",
        icon="category",
        permissions=["change"],
        dialog={
            "title": _("Suggest PD indicators for the master indicators now"),
            "description": _(
                "Works out, in the background, the PD indicators of this year and last year that belong "
                "in each master indicator, and proposes new master indicators where programme documents "
                "share an indicator no master holds. Nothing counts until someone confirms it; confirmed and "
                "rejected links and dismissed proposals are kept. It also runs every night. No AI is used."
            ),
            "form_class": SuggestNowForm,
            "form_submit_text": _("Start"),
        },
    )
    def suggest_now(self, request, form):
        from neurodb.core.models import SyncRun
        from neurodb.integrations import background

        if background.is_running(SyncRun.Job.ETOOLS_MASTERS):
            messages.warning(request, master_links.BUSY)
        else:
            background.start_command(
                "suggest_master_indicators", "--triggered-by", request.user.get_username()
            )
            messages.success(
                request,
                _(
                    "The suggestions are being worked out in the background. Refresh this page in a few "
                    "minutes to see them."
                ),
            )
        url = reverse("admin:datamart_etoolsmaster_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: load the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


class ScoreFilter(admin.SimpleListFilter):
    """How close a suggestion is: the strong matches staff can confirm in one click, the similar titles."""

    title = _("score")
    parameter_name = "score"

    def lookups(self, request, model_admin):
        return (
            ("strong", _("0.9 or more")),
            ("similar", _("Below 0.9")),
            ("none", _("No score")),
        )

    def queryset(self, request, queryset):
        if self.value() == "strong":
            return queryset.filter(score__gte=master_links.STRONG)
        if self.value() == "similar":
            return queryset.filter(score__lt=master_links.STRONG)
        if self.value() == "none":
            return queryset.filter(score__isnull=True)
        return queryset


@admin.register(dm.EtoolsMasterLink)
class EtoolsMasterLinkAdmin(ModelAdmin):
    """Each PD indicator of an eTools master indicator, and what it counts as there. A suggestion counts
    only once confirmed (Confirm reads the indicator again and saves it as eTools has it now); a rejected
    one is never suggested for that master again."""

    list_display = (
        "pd_number",
        "title_short",
        "master",
        "section",
        "role",
        "status_badge",
        "score",
        "reason",
    )
    list_display_links = ("pd_number", "title_short")
    list_filter = (
        "status",
        "role",
        ("master__section", admin.AllValuesFieldListFilter),
        "master",
        ScoreFilter,
    )
    list_select_related = ("master",)
    search_fields = ("title", "pd_number", "ident", "master__name", "reason")
    form = EtoolsMasterLinkForm
    autocomplete_fields = ("master",)
    raw_id_fields = ("pd",)
    fields = (
        "master",
        "pd",
        "ident",
        "role",
        "status",
        "title",
        "pd_number",
        "unit_class",
        "score",
        "reason",
        "sequence",
        "confirmed_by",
        "updated_at",
    )
    actions = ("confirm_links", "reject_links", "make_shown")
    list_per_page = 50

    def get_readonly_fields(self, request, obj=None):
        fixed = ("title", "pd_number", "unit_class", "score", "reason", "confirmed_by", "updated_at")
        # a link's indicator is fixed once saved; its status changes with Confirm and Reject
        return ("master", "pd", "ident", "status", *fixed) if obj else fixed

    @admin.display(description=_("PD indicator"), ordering="title")
    def title_short(self, obj):
        return Truncator(obj.title or obj.ident).chars(90)

    @admin.display(description=_("Section"), ordering="master__section")
    def section(self, obj):
        return obj.master.section

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), LINK_TONES.get(obj.status))

    def save_model(self, request, obj, form, change):
        if not change and obj.status != Status.SUGGESTED:  # a link added by hand, confirmed or rejected
            obj.confirmed_by = request.user.get_username()
        if change:
            _role_set_by(obj, form, request.user.get_username())
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Confirm"), permissions=("change",))
    def confirm_links(self, request, queryset):
        """Confirm links: each PD indicator is read again and saved as eTools has it now; one that cannot
        count is left as it was, and said why."""
        links = list(queryset.select_related("master").order_by("master_id", "sequence", "id"))
        done = master_links.confirm(links, request.user.get_username())
        self.message_user(
            request,
            _("%(n)s link(s) confirmed: they count from now on.") % {"n": done.confirmed},
            messages.SUCCESS,
        )
        if done.left:
            left = [(_link_name(link), why) for link, why in done.left]
            self.message_user(
                request,
                _("%(n)s left as they were: %(why)s") % {"n": len(left), "why": _left(left)},
                messages.WARNING,
            )

    @admin.action(description=_("Reject"), permissions=("change",))
    def reject_links(self, request, queryset):
        done = master_links.reject(list(queryset), request.user.get_username())
        self.message_user(
            request,
            _("%(n)s link(s) rejected: never counted, and never suggested for that master again.")
            % {"n": done},
            messages.SUCCESS,
        )

    @admin.action(description=_('Make "Shown only"'), permissions=("change",))
    def make_shown(self, request, queryset):
        """Shown next to the master's figure, never counted (a breakdown, a partner's other indicator). A
        suggestion keeps who set it (the nightly job keeps a role a person gave a suggestion)."""
        changing = list(queryset.exclude(role=Role.SHOWN).values_list("pk", "status"))
        suggested = {pk for pk, status in changing if status == Status.SUGGESTED}
        now, by = timezone.now(), request.user.get_username()[:150]
        links = dm.EtoolsMasterLink.objects
        links.filter(pk__in=suggested).update(role=Role.SHOWN, confirmed_by=by, updated_at=now)
        links.filter(pk__in=[pk for pk, _status in changing if pk not in suggested]).update(
            role=Role.SHOWN, updated_at=now
        )
        done = len(changing)
        self.message_user(
            request, _('%(n)s link(s) made "Shown only": never counted.') % {"n": done}, messages.SUCCESS
        )
