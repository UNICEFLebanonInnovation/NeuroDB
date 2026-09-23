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
