from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AssistantQuestion(models.Model):
    """One question put to the AI assistant: who asked, what was answered, which data was read and
    what it cost. Used for the hourly per-user limit, for cost monitoring and for review."""

    class Status(models.TextChoices):
        ANSWERED = "answered", "Answered"
        REFUSED = "refused", "Declined by the model"
        FAILED = "failed", "Failed"
        LIMITED = "limited", "Over the hourly limit"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    conversation = models.UUIDField(null=True, blank=True, help_text="groups a question with its follow-ups")
    question = models.TextField()
    answer = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ANSWERED, db_index=True)
    tools = models.JSONField(default=list, blank=True, help_text="data lookups made, in order")
    model = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "conversation", "created_at"]),
        ]
        verbose_name = "AI question"

    def __str__(self):
        return self.question[:80]


class AIUsage(models.Model):
    """The AI use of one feature on one day with one model: the calls made to the AI provider (Anthropic
    or OpenAI, told apart by the model's name) and the tokens they used. Every AI feature adds to it after
    each call (``usage.record``): Ask NeuroDB, the daily review, the What's new note, document summaries,
    periodic report figures, the country programme reading, NeuroDB Watch, Monitoring insights, the Help
    assistant, the document review and the connection test of AI settings. The day's total across
    features is read from it, and so are the watch's and Monitoring insights' own limits."""

    day = models.DateField(help_text="local date of the calls")
    feature = models.CharField(
        max_length=20, help_text="ask, review, digest, knowledge, periodic, cpd, watch or fmm"
    )
    model = models.CharField(max_length=64, blank=True, help_text="the model that answered the calls")
    calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(
        default=0, help_text="prompt tokens not read from the cache"
    )
    cached_tokens = models.PositiveBigIntegerField(default=0, help_text="prompt tokens read from the cache")
    # db_default too: while a release starts, the revision before it still writes rows without this column
    cache_write_tokens = models.PositiveBigIntegerField(
        default=0,
        db_default=0,
        help_text=(
            "of the prompt tokens not read from the cache, those written to it (Claude; priced at 1.25 "
            "times the input)"
        ),
    )
    output_tokens = models.PositiveBigIntegerField(
        default=0, help_text="includes the reasoning or thinking tokens"
    )

    class Meta:
        ordering = ("-day", "feature", "model")
        constraints = [
            models.UniqueConstraint(fields=["day", "feature", "model"], name="assistant_aiusage_one_per_day")
        ]
        verbose_name = "AI use"
        verbose_name_plural = "AI use"

    def __str__(self):
        return f"{self.day} {self.feature} {self.model}"

    @property
    def total_tokens(self) -> int:
        """The whole prompt (cached or not) and the output."""
        return self.input_tokens + self.cached_tokens + self.output_tokens


class AISettings(models.Model):
    """The one row (pk=1) of "AI settings": which AI provider the AI features run on (Automatic, Claude or
    OpenAI), the API keys entered here, the models, the default effort, whether a declined answer is retried
    on another Claude model, and the last connection test. No row means Automatic with nothing entered: the
    row is made when an administrator first opens the page (:meth:`load`). The keys are sealed with the
    site's secret key (:mod:`neurodb.assistant.llm.keys`) and never shown again, only their last 4
    characters. A provider or key the hosting team sets in the environment comes first. Readers go
    through :func:`neurodb.assistant.llm.snapshot` (``filter(pk=1).first()``, cached) and never write. The
    choices are the form's, so a new model or effort needs no migration."""

    provider = models.CharField(
        _("AI provider"),
        max_length=10,
        default="auto",
        help_text=_(
            "Which AI the AI features run on. Automatic: Claude when an Anthropic API key is entered here "
            "or set by the hosting team, else OpenAI. A provider the hosting team set (AI_PROVIDER) comes "
            "before the choice here. A change applies within seconds, with no restart."
        ),
    )
    anthropic_key_sealed = models.TextField(blank=True, editable=False)
    anthropic_key_last4 = models.CharField(max_length=4, blank=True, editable=False)
    anthropic_key_set_at = models.DateTimeField(null=True, blank=True, editable=False)
    openai_key_sealed = models.TextField(blank=True, editable=False)
    openai_key_last4 = models.CharField(max_length=4, blank=True, editable=False)
    openai_key_set_at = models.DateTimeField(null=True, blank=True, editable=False)
    claude_model = models.CharField(
        _("Claude model"),
        max_length=64,
        blank=True,
        help_text=_("Empty: the default model (CLAUDE_MODEL). It applies to every AI feature on Claude."),
    )
    claude_effort = models.CharField(
        _("Claude's default effort"),
        max_length=8,
        blank=True,
        help_text=_(
            "How much Claude thinks before it answers, where a feature does not set its own: low is quicker "
            "and cheaper, high and above more thorough. Empty: the default (CLAUDE_EFFORT)."
        ),
    )
    claude_fallbacks = models.BooleanField(
        _("Retry a declined answer on another Claude model (recommended)"),
        default=True,
        help_text=_(
            "When Claude declines to answer for a safety reason, Anthropic answers the same request with "
            "another Claude model it chooses for that reason. The question log and AI use then name the "
            "model that answered."
        ),
    )
    openai_model = models.CharField(
        _("OpenAI model"),
        max_length=64,
        blank=True,
        help_text=_(
            "Empty: the default model (AI_ASSISTANT_MODEL). It applies to every AI feature on OpenAI."
        ),
    )
    openai_effort = models.CharField(
        _("OpenAI's default reasoning effort"),
        max_length=8,
        blank=True,
        help_text=_("Where a feature does not set its own. Empty: the default (AI_ASSISTANT_EFFORT)."),
    )
    checked_at = models.DateTimeField(_("Tested on"), null=True, blank=True, editable=False)
    check_result = models.CharField(_("Result"), max_length=300, blank=True, editable=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("AI settings")
        verbose_name_plural = _("AI settings")

    def __str__(self):
        # the provider in use: the page's crumbs read "AI settings › Claude (Anthropic)"
        from . import llm

        return llm.names().label

    @classmethod
    def load(cls) -> "AISettings":
        """The one row, made (Automatic, nothing entered) when missing: for the admin page only."""
        return cls.objects.get_or_create(pk=1)[0]
