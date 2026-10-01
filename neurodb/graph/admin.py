from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Edge, Entity


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
