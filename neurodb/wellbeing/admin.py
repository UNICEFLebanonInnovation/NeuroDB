from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import CenterSummary, Flag, SyncState


@admin.register(Flag)
class FlagAdmin(ModelAdmin):
    list_display = (
        "registration",
        "kind_label",
        "status",
        "urgent",
        "center_name",
        "opened_on",
        "followed_up_on",
    )
    list_filter = ("status", "kind", "urgent", "priority")
    search_fields = ("registration", "center_name", "partner_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # a copy of BMA's flags: follow-ups are recorded on the Makani wellbeing page


@admin.register(CenterSummary)
class CenterSummaryAdmin(ModelAdmin):
    list_display = ("center_name", "partner_name", "round_name", "month", "computed_at")
    list_filter = ("month",)
    search_fields = ("center_name", "partner_name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SyncState)
class SyncStateAdmin(ModelAdmin):
    list_display = ("synced_at", "flags_modified_since")
    readonly_fields = ("flags_modified_since", "settings", "kinds", "results", "synced_at")

    def has_add_permission(self, request):
        return False
