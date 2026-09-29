"""The youth figures (read-only) and the youth indicator links (suggested, confirmed here)."""

from __future__ import annotations

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .figures import Figures
from .models import YouthFigures, YouthIndicatorLink


@admin.register(YouthFigures)
class YouthFiguresAdmin(ReadOnlyModelAdmin):
    list_display = ("year", "fetched_at", "young_people", "indicators", "programme_documents", "dashboard")
    fields = ("year", "fetched_at", "young_people", "indicators", "programme_documents", "dashboard")
    readonly_fields = fields

    @admin.display(description=_("Young people reached"))
    def young_people(self, obj):
        return Figures(obj.payload).count({})

    @admin.display(description=_("Indicators"))
    def indicators(self, obj):
        return len(obj.payload.get("indicators", []))

    @admin.display(description=_("Programme documents"))
    def programme_documents(self, obj):
        return len(obj.payload.get("program_documents", []))

    @admin.display(description=_("Dashboard"))
    def dashboard(self, obj):
        return format_html('<a href="{}?year={}">{}</a>', reverse("youth:dashboard"), obj.year, _("Open"))


@admin.register(YouthIndicatorLink)
class YouthIndicatorLinkAdmin(ModelAdmin):
    """A suggested link is replaced at every sync; confirming it (or saving it here) keeps it."""

    list_display = (
        "youth_indicator",
        "compiler_pd_code",
        "pd",
        "etools_title",
        "source_badge",
        "score",
        "year",
    )
    list_filter = ("year", "source", "level")
    search_fields = ("youth_indicator", "compiler_pd_code", "etools_title", "pd__number")
    raw_id_fields = ("pd",)
    readonly_fields = ("score", "confirmed_by", "updated_at")
    fields = (
        "year",
        "level",
        "youth_indicator_id",
        "youth_indicator",
        "compiler_pd_id",
        "compiler_pd_code",
        "pd",
        "etools_key",
        "etools_title",
        "note",
        "score",
        "confirmed_by",
        "updated_at",
    )
    actions = ("confirm",)
    list_per_page = 50

    @admin.display(description=_("Source"), ordering="source")
    def source_badge(self, obj):
        tone = "ok" if obj.source == YouthIndicatorLink.Source.CONFIRMED else "warn"
        return badge(obj.get_source_display(), tone)

    def save_model(self, request, obj, form, change):
        obj.source = YouthIndicatorLink.Source.CONFIRMED  # a link saved by a person is kept
        obj.confirmed_by = request.user
        super().save_model(request, obj, form, change)

    @action(description=_("Confirm the selected links"))
    def confirm(self, request, queryset):
        count = queryset.exclude(source=YouthIndicatorLink.Source.CONFIRMED).update(
            source=YouthIndicatorLink.Source.CONFIRMED, confirmed_by=request.user
        )
        self.message_user(request, _("%(count)d link(s) confirmed.") % {"count": count}, messages.SUCCESS)
