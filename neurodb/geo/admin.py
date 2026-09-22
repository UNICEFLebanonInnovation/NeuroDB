from django.contrib import admin

from .models import CadasterLocation, DistrictLocation, GovernorateLocation, Location, LocationType


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "p_code", "type", "parent", "is_active")
    list_filter = ("type", "is_active")
    search_fields = ("name", "p_code")


admin.site.register(LocationType)
for model in (GovernorateLocation, DistrictLocation, CadasterLocation):
    admin.site.register(model, list_display=("name", "code"), search_fields=("name", "code"))
