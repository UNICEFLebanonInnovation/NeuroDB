from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import usage
from .models import AIUsage, AssistantQuestion

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


class FeatureFilter(admin.SimpleListFilter):
    title = _("Feature")
    parameter_name = "feature"

    def lookups(self, request, model_admin):
        return list(usage.FEATURES.items())

    def queryset(self, request, queryset):
        return queryset.filter(feature=self.value()) if self.value() else queryset


@admin.register(AIUsage)
class AIUsageAdmin(ReadOnlyModelAdmin):
    """The AI use of every feature on the shared OpenAI key, per day, in tokens; in US dollars too
    when the optional AI_PRICE_* settings give the prices."""

    list_display = (
        "day", "feature_name", "model", "calls", "prompt_tokens", "cached", "output", "total",
    )  # fmt: skip
    list_filter = (FeatureFilter, "day", "model")
    date_hierarchy = "day"
    list_per_page = 100

    def get_list_display(self, request):
        shown = super().get_list_display(request)
        return (*shown, "cost") if usage.prices() else shown

    @admin.display(description=_("Feature"), ordering="feature")
    def feature_name(self, obj):
        return usage.FEATURES.get(obj.feature, obj.feature)

    @admin.display(description=_("Prompt tokens (not cached)"), ordering="input_tokens")
    def prompt_tokens(self, obj):
        return f"{obj.input_tokens:,}"

    @admin.display(description=_("Cached prompt tokens"), ordering="cached_tokens")
    def cached(self, obj):
        return f"{obj.cached_tokens:,}"

    @admin.display(description=_("Output tokens"), ordering="output_tokens")
    def output(self, obj):
        return f"{obj.output_tokens:,}"

    @admin.display(description=_("Total tokens"))
    def total(self, obj):
        return f"{obj.total_tokens:,}"

    @admin.display(description=_("Cost (USD)"))
    def cost(self, obj):
        price = usage.prices()
        if not price:
            return "-"
        dollars = usage.cost(obj, price)
        return "< $0.01" if 0 < dollars < usage.CENT else f"${dollars:,.2f}"
