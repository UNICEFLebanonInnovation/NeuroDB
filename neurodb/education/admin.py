"""The education figures read from Compiler (read-only)."""

from __future__ import annotations

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from neurodb.web.admin_helpers import ReadOnlyModelAdmin

from .figures import Figures
from .models import EducationFigures


@admin.register(EducationFigures)
class EducationFiguresAdmin(ReadOnlyModelAdmin):
    list_display = ("programme_label", "year", "children", "counted_at", "fetched_at", "problems", "page")
    list_filter = ("programme",)
    fields = ("programme_label", "year", "children", "counted_at", "fetched_at", "problems", "page")
    readonly_fields = fields

    @admin.display(description=_("Programme"), ordering="programme")
    def programme_label(self, obj):
        return obj.payload.get("label") or obj.programme

    @admin.display(description=_("Children registered"))
    def children(self, obj):
        return Figures(obj.payload).block("registrations").total({}).get("people", 0)

    @admin.display(description=_("Parts not counted"))
    def problems(self, obj):
        failed = [n for n, b in obj.payload.get("blocks", {}).items() if b.get("error")]
        return ", ".join(failed) or "—"

    @admin.display(description=_("Page"))
    def page(self, obj):
        url = reverse("education:dashboard")
        return format_html(
            '<a href="{}?programme={}&year={}">{}</a>', url, obj.programme, obj.year, _("Open")
        )
