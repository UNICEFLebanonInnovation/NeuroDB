from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Change, Digest, DigestSubscription, Edge, Entity


class ReadOnly(ModelAdmin):
    """The hub is rebuilt every night from the other data: nothing is edited here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Entity)
class EntityAdmin(ReadOnly):
    list_display = ("name", "kind", "key", "built_at")
    list_filter = ("kind",)
    search_fields = ("name", "aliases", "key")
    exclude = ("search_vector",)


@admin.register(Edge)
class EdgeAdmin(ReadOnly):
    list_display = ("source", "relation", "target", "origin")
    list_filter = ("relation", "origin")
    search_fields = ("source__name", "target__name")
    list_select_related = ("source", "target")
    raw_id_fields = ("source", "target")


@admin.register(Change)
class ChangeAdmin(ReadOnly):
    list_display = ("detected_at", "op", "kind", "name", "notable")
    list_filter = ("op", "kind", "notable")
    search_fields = ("name", "key")
    date_hierarchy = "detected_at"
    raw_id_fields = ("entity", "run")


@admin.register(Digest)
class DigestAdmin(ReadOnly):
    list_display = ("date", "section_name", "changes", "emailed_to", "written_by")
    list_filter = ("date",)


@admin.register(DigestSubscription)
class DigestSubscriptionAdmin(ModelAdmin):
    list_display = ("user", "email", "updated_at")
    list_filter = ("email",)
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)
