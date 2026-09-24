from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.forms import BaseDialogForm

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import PopulationFigure, SavedView, SyncRun


class DatamartSyncForm(BaseDialogForm):
    scope = forms.ChoiceField(
        label=_("What to sync"),
        choices=(
            ("core", _("Partners and programme documents (a few minutes)")),
            (
                "all",
                _("Everything: funds, audits, reporting, monitoring and every other dataset (up to 2 hours)"),
            ),
        ),
        initial="core",
        widget=forms.RadioSelect,
    )


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyModelAdmin):
    actions_list = ["sync_etools_datamart"]

    list_display = (
        "job",
        "target",
        "status_badge",
        "started_at",
        "duration_display",
        "rows_written",
        "rows_failed",
        "triggered_by",
        "error_short",
    )
    list_filter = ("job", "status", "started_at", "triggered_by")
    search_fields = ("target", "error", "triggered_by")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    readonly_fields = tuple(f.name for f in SyncRun._meta.fields) + ("duration", "errors_display")
    list_per_page = 50

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(
            obj.get_status_display(),
            {"succeeded": "ok", "partial": "warn", "failed": "bad", "running": "info"}.get(obj.status),
        )

    @admin.display(description=_("Duration"))
    def duration_display(self, obj):
        d = obj.duration
        if not d:
            return "—"
        seconds = int(d.total_seconds())
        return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"

    @admin.display(description=_("Why rows failed"))
    def errors_display(self, obj):
        errors = (obj.details or {}).get("errors") or []
        if not errors:
            return "—"
        items = format_html_join(
            "",
            "<li><strong>{} ×</strong> {}<br><small>{}: {}</small></li>",
            (
                (e["count"], e["error"], _("examples"), ", ".join(str(x) for x in e["examples"]))
                for e in sorted(errors, key=lambda e: -e["count"])
            ),
        )
        other = obj.details.get("other_errors")
        tail = format_html("<li>{}</li>", _("%(n)s more with other messages") % {"n": other}) if other else ""
        return format_html('<ul class="nd-error-list">{}{}</ul>', items, tail)

    @admin.display(description=_("Error"))
    def error_short(self, obj):
        return (obj.error[:90] + "…") if len(obj.error) > 90 else (obj.error or "")

    def has_run_sync_permission(self, request):
        from neurodb.accounts.roles import ADMIN, role_of

        return request.user.is_superuser or role_of(request.user) == ADMIN

    @action(
        description=_("Sync eTools now"),
        url_path="sync-etools-datamart",
        permissions=["run_sync"],
        icon="sync",
        dialog={
            "title": _("Sync from the eTools Datamart"),
            "description": _(
                "Reads the eTools Datamart in the background. Each dataset appears in this list as it "
                "runs; refresh the page to follow it."
            ),
            "form_class": DatamartSyncForm,
            "form_submit_text": _("Start"),
        },
    )
    def sync_etools_datamart(self, request, form):
        from neurodb.integrations import background
        from neurodb.integrations.etools.datamart import configured
        from neurodb.integrations.management.commands.sync_etools_datamart import CORE

        if not configured():
            messages.error(
                request,
                _(
                    "The eTools Datamart credentials are not set: add ETOOLS_USERNAME and ETOOLS_PASSWORD "
                    "to the application settings (Key Vault secrets etools-username and etools-password)."
                ),
            )
        elif background.is_running(SyncRun.Job.ETOOLS_DATAMART):
            messages.warning(request, _("An eTools Datamart sync is already running."))
        else:
            args = ["sync_etools_datamart", "--triggered-by", request.user.get_username()]
            if form.cleaned_data["scope"] == "core":
                args += ["--only", ",".join(CORE)]
            background.start_command(*args)
            messages.success(request, _("eTools Datamart sync started. Refresh this page to follow it."))
        url = reverse("admin:core_syncrun_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


@admin.register(SavedView)
class SavedViewAdmin(ModelAdmin):
    list_display = ("name", "owner", "page", "object_id", "is_shared", "updated_at")
    list_filter = ("page", "is_shared")
    search_fields = ("name", "owner__username")
    list_select_related = ("owner",)
    autocomplete_fields = ("owner",)


@admin.register(PopulationFigure)
class PopulationFigureAdmin(ModelAdmin):
    list_display = ("year", "category", "nationality", "level", "area_name", "age_group", "sex", "value")
    list_filter = ("year", "category", "nationality", "level")
    search_fields = ("area_name", "area_code")
    list_per_page = 100


@admin.register(LogEntry)
class AuditTrailAdmin(ReadOnlyModelAdmin):
    """Every change made in the admin: who, when, what (Django records it; this makes it browsable)."""

    list_display = (
        "action_time",
        "user",
        "action_badge",
        "content_type",
        "object_link",
        "change_message_text",
    )
    list_filter = ("action_flag", "user")
    search_fields = ("object_repr", "change_message", "user__username")
    date_hierarchy = "action_time"
    list_select_related = ("user", "content_type")
    ordering = ("-action_time",)
    list_per_page = 50

    def changelist_view(self, request, extra_context=None):
        extra_context = {"title": _("Audit trail: who changed what in the admin"), **(extra_context or {})}
        return super().changelist_view(request, extra_context)

    def has_view_permission(self, request, obj=None):
        return (
            request.user.is_active and request.user.is_superuser or super().has_view_permission(request, obj)
        )

    @admin.display(description=_("Action"), ordering="action_flag")
    def action_badge(self, obj):
        text, tone = {
            ADDITION: (_("Added"), "ok"),
            CHANGE: (_("Changed"), "info"),
            DELETION: (_("Deleted"), "bad"),
        }[obj.action_flag]
        return badge(text, tone)

    @admin.display(description=_("Object"))
    def object_link(self, obj):
        if obj.action_flag != DELETION:
            try:
                url = obj.get_admin_url()
            except NoReverseMatch:
                url = None
            if url:
                return format_html('<a href="{}">{}</a>', url, obj.object_repr)
        return obj.object_repr

    @admin.display(description=_("Change"))
    def change_message_text(self, obj):
        return obj.get_change_message()
