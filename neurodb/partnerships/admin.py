"""Read-only admin over the eTools replica tables (synced data is never edited by hand)."""

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from neurodb.web.admin_helpers import badge

from .linking import link_activityinfo_partners
from .models import (
    PCA,
    ActionPoint,
    Agreement,
    Engagement,
    PartnerLink,
    PartnerOrganization,
    Travel,
    TravelActivity,
)


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


@admin.register(PartnerLink)
class PartnerLinkAdmin(ModelAdmin):
    """The bridge between the ActivityInfo partner names and the eTools partners (editable)."""

    actions_list = ["relink"]
    list_display = ("label", "partner", "method_badge", "records", "first_month", "last_month", "updated_at")
    list_filter = ("method", ("partner", admin.EmptyFieldListFilter))
    search_fields = ("label", "partner__name", "partner__short_name", "partner__vendor_number")
    autocomplete_fields = ("partner",)
    readonly_fields = ("method", "records", "first_month", "last_month", "database_ids", "updated_at")
    list_per_page = 100
    ordering = ("partner", "label")

    @admin.display(description=_("Linked by"), ordering="method")
    def method_badge(self, obj):
        tones = {"manual": "info", "name": "ok", "pd": "ok", "": "warn"}
        return badge(obj.get_method_display(), tones.get(obj.method, "muted"))

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if "partner" in form.changed_data:  # a hand-picked partner (or none) sticks across re-runs
            obj.method = PartnerLink.Method.MANUAL if obj.partner_id else PartnerLink.Method.NONE
        super().save_model(request, obj, form, change)

    def has_relink_permission(self, request):
        return self.has_change_permission(request)

    @action(
        description=_("Match ActivityInfo partners now"),
        url_path="relink",
        permissions=["relink"],
        icon="link",
    )
    def relink(self, request):
        run = link_activityinfo_partners(triggered_by=request.user.get_username())
        d = run.details
        messages.success(
            request,
            _(
                "%(labels)s ActivityInfo partner names: %(name)s linked by name, %(pd)s by programme "
                "document, %(manual)s set by hand, %(unlinked)s not linked."
            )
            % {
                "labels": d["labels"],
                "name": d["linked_by_name"],
                "pd": d["linked_by_pd"],
                "manual": d["set_by_hand"],
                "unlinked": d["unlinked"],
            },
        )
        url = reverse("admin:etools_partnerlink_changelist")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)
