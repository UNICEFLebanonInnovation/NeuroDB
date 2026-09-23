"""Small shared pieces for ModelAdmins: status badges and a read-only base."""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

TONES = {
    "succeeded": "ok", "fresh": "ok", "active": "ok", "yes": "ok",
    "partial": "warn", "stale": "warn", "running": "info",
    "failed": "bad", "never": "muted", "no": "muted", "none": "muted",
}  # fmt: skip


def badge(text, tone: str | None = None):
    """A coloured pill for list columns (styles in static/css/admin.css)."""
    tone = tone or TONES.get(str(text).lower().replace(" ", "_"), "muted")
    return format_html('<span class="nd-badge nd-badge--{}">{}</span>', tone, text)


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Browse and search only (replicated or system data)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
