"""Read-only admin for ``SyncRun`` (the job ledger new in v3)."""

from django.contrib import admin

from neurodb.core.models import SyncRun


class SyncRunAdmin(admin.ModelAdmin):
    list_display = (
        "job",
        "target",
        "status",
        "started_at",
        "finished_at",
        "rows_in",
        "rows_written",
        "rows_failed",
        "triggered_by",
    )
    list_filter = ("job", "status", "started_at")
    search_fields = ("target", "error", "triggered_by")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    readonly_fields = tuple(field.name for field in SyncRun._meta.fields) + ("duration",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


if not admin.site.is_registered(SyncRun):
    admin.site.register(SyncRun, SyncRunAdmin)
