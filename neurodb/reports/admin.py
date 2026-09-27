"""The section plans people enter by hand for the management brief."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import SectionPlan


@admin.register(SectionPlan)
class SectionPlanAdmin(ModelAdmin):
    list_display = ("year", "section", "children_target", "required_usd", "note", "updated_by", "updated_at")
    list_filter = ("year",)
    search_fields = ("section", "note")
    readonly_fields = ("updated_by", "updated_at")
    list_per_page = 50

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)
