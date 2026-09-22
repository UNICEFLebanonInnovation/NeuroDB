from django.contrib import admin

from .models import PopulationFigure, SavedView, SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("job", "target", "status", "started_at", "finished_at", "rows_in", "rows_written", "rows_failed", "triggered_by")
    list_filter = ("job", "status")
    search_fields = ("target", "error")
    readonly_fields = [f.name for f in SyncRun._meta.fields]
    date_hierarchy = "started_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SavedView)
class SavedViewAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "page", "object_id", "is_shared", "updated_at")
    list_filter = ("page", "is_shared")
    search_fields = ("name",)


@admin.register(PopulationFigure)
class PopulationFigureAdmin(admin.ModelAdmin):
    list_display = ("year", "category", "nationality", "level", "area_name", "age_group", "sex", "value")
    list_filter = ("year", "category", "nationality", "level")
    search_fields = ("area_name",)
