from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.urls import NoReverseMatch
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import PopulationFigure, SavedView, SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyModelAdmin):
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
    readonly_fields = tuple(f.name for f in SyncRun._meta.fields) + ("duration",)
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

    @admin.display(description=_("Error"))
    def error_short(self, obj):
        return (obj.error[:90] + "…") if len(obj.error) > 90 else (obj.error or "")


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
