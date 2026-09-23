"""Read-only admin over the eTools replica tables (synced data is never edited by hand)."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import PCA, ActionPoint, Agreement, Engagement, PartnerOrganization, Travel, TravelActivity


class ReadOnlyAdmin(ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PartnerOrganization)
class PartnerAdmin(ReadOnlyAdmin):
    list_display = (
        "name",
        "short_name",
        "vendor_number",
        "partner_type",
        "cso_type",
        "rating",
        "hidden",
        "deleted_flag",
    )
    list_filter = ("partner_type", "cso_type", "rating", "hidden", "deleted_flag")
    search_fields = ("name", "short_name", "vendor_number")
    exclude = ("staff_members",)


@admin.register(Agreement)
class AgreementAdmin(ReadOnlyAdmin):
    list_display = ("agreement_number", "partner_name", "agreement_type", "start", "end")
    search_fields = ("agreement_number", "partner_name")


@admin.register(PCA)
class PCAAdmin(ReadOnlyAdmin):
    list_display = ("number", "title", "partner_name", "document_type", "status", "start", "end")
    list_filter = ("status", "document_type")
    search_fields = ("number", "title", "partner_name")


@admin.register(Engagement)
class EngagementAdmin(ReadOnlyAdmin):
    list_display = ("unique_id", "partner", "engagement_type", "status", "start_date", "end_date")
    list_filter = ("engagement_type", "status")
    search_fields = ("unique_id", "partner__name")


@admin.register(Travel)
class TravelAdmin(ReadOnlyAdmin):
    list_display = (
        "reference_number",
        "status",
        "section",
        "office",
        "start_date",
        "end_date",
        "travel_type",
    )
    list_filter = ("status", "travel_type", "section")
    search_fields = ("reference_number",)
    exclude = ("traveler_name", "supervisor_name")


@admin.register(TravelActivity)
class TravelActivityAdmin(ReadOnlyAdmin):
    list_display = ("travel", "travel_type", "partner", "partnership", "date")
    list_filter = ("travel_type",)


@admin.register(ActionPoint)
class ActionPointAdmin(ReadOnlyAdmin):
    list_display = ("reference_number", "status", "partner", "due_date", "high_priority")
    list_filter = ("status", "high_priority")
