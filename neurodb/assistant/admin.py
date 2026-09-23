from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import AssistantQuestion

TONES = {"answered": "ok", "refused": "warn", "failed": "bad", "limited": "muted"}


@admin.register(AssistantQuestion)
class AssistantQuestionAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "user", "question_short", "status_badge", "lookups", "tokens", "duration")
    list_filter = ("status", "created_at", "model")
    search_fields = ("question", "answer", "user__username")
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    list_per_page = 50

    @admin.display(description=_("Question"))
    def question_short(self, obj):
        return obj.question[:100] + ("…" if len(obj.question) > 100 else "")

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), TONES.get(obj.status))

    @admin.display(description=_("Lookups"))
    def lookups(self, obj):
        return len(obj.tools or [])

    @admin.display(description=_("Tokens in / out"))
    def tokens(self, obj):
        return f"{obj.input_tokens + obj.cache_read_tokens:,} / {obj.output_tokens:,}"

    @admin.display(description=_("Time"), ordering="duration_ms")
    def duration(self, obj):
        return f"{obj.duration_ms / 1000:.1f} s"
