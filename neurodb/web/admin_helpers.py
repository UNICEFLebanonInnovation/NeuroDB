"""Small shared pieces for ModelAdmins: status badges and a read-only base."""

from __future__ import annotations

from django.template.loader import render_to_string
from unfold.admin import ModelAdmin

TONES = {
    "succeeded": "ok", "fresh": "ok", "active": "ok", "yes": "ok",
    "partial": "warn", "stale": "warn", "running": "info",
    "failed": "bad", "never": "muted", "no": "muted", "none": "muted",
}  # fmt: skip
# Our tones mapped to Unfold's label variants (anything else renders neutral grey).
VARIANTS = {"ok": "success", "warn": "warning", "bad": "danger", "info": "info", "muted": ""}


def badge(text, tone: str | None = None):
    """A coloured label for list columns, drawn with Unfold's own label component."""
    tone = tone or TONES.get(str(text).lower().replace(" ", "_"), "muted")
    return render_to_string(
        "unfold/helpers/label.html",
        {
            "text": text,
            "variant": VARIANTS.get(tone, ""),
            "class": f"nd-badge nd-badge--{tone}",
            "size": "md",
        },
    )


class ReadOnlyModelAdmin(ModelAdmin):
    """Browse and search only (replicated or system data)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
