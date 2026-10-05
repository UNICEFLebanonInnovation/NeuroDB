from django.conf import settings
from django.db import models


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
    """The AI use of one feature on one day with one model: the calls made to OpenAI and the tokens
    they used. Every feature on the shared OpenAI key adds to it after each call (``usage.record``):
    Ask NeuroDB, the daily review, the What's new note, document summaries, periodic report figures,
    the country programme reading, NeuroDB Watch and Monitoring insights. The day's total across
    features is read from it, and so are the watch's and Monitoring insights' own limits."""

    day = models.DateField(help_text="local date of the calls")
    feature = models.CharField(
        max_length=20, help_text="ask, review, digest, knowledge, periodic, cpd, watch or fmm"
    )
    model = models.CharField(max_length=64, blank=True, help_text="the model the calls asked for")
    calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(
        default=0, help_text="prompt tokens not read from the cache"
    )
    cached_tokens = models.PositiveBigIntegerField(default=0, help_text="prompt tokens read from the cache")
    output_tokens = models.PositiveBigIntegerField(default=0, help_text="includes the reasoning tokens")

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
