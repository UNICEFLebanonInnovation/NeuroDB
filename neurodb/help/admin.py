from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import HelpQuestion

TONES = {"answered": "ok", "refused": "warn", "failed": "bad", "limited": "muted", "in_progress": "muted"}


@admin.register(HelpQuestion)
class HelpQuestionAdmin(ReadOnlyModelAdmin):
    """The questions put to the Help assistant (read-only, kept 90 days): who asked, from which page,
    whether it was answered or declined (a declined question costs no quota), the look-ups and the tokens.
    The question is kept as it was sent: names, e-mail addresses, phone numbers and links removed."""

    list_display = (
        "created_at",
        "user",
        "question_short",
        "page",
        "status_badge",
        "refusal_reason",
        "lookups",
        "tokens_shown",
        "duration",
    )
    list_filter = ("status", "refused", "refusal_reason", "created_at")
    search_fields = ("question", "answer", "page", "user__username")
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    list_per_page = 50

    @admin.display(description=_("Question"))
    def question_short(self, obj):
        return obj.question[:100] + ("…" if len(obj.question) > 100 else "")

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), TONES.get(obj.status))

    @admin.display(description=_("Look-ups"))
    def lookups(self, obj):
        return len(obj.tools or [])

    @admin.display(description=_("Tokens in / out"))
    def tokens_shown(self, obj):
        return f"{obj.input_tokens + obj.cache_read_tokens:,} / {obj.output_tokens:,}"

    @admin.display(description=_("Time"), ordering="duration_ms")
    def duration(self, obj):
        return f"{obj.duration_ms / 1000:.1f} s"
