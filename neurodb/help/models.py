from django.conf import settings
from django.db import models


class HelpQuestion(models.Model):
    """One question put to the Help assistant: who asked, from which page, what was answered, what it
    cost and whether it was declined. Used for the daily per-person quota (declined questions and those
    over a limit do not count) and for review (admin, read-only). Deleted after 90 days by the daily
    review job's clean-up (``prune``)."""

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "Being answered"
        ANSWERED = "answered", "Answered"
        REFUSED = "refused", "Declined"
        FAILED = "failed", "Failed"
        LIMITED = "limited", "Over a limit"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    conversation = models.UUIDField(null=True, blank=True, help_text="groups a question with its follow-ups")
    page = models.CharField(max_length=200, blank=True, help_text="the address the question was asked from")
    page_title = models.CharField(max_length=150, blank=True)
    question = models.TextField(help_text="as sent: names, e-mail addresses and phone numbers removed")
    answer = models.TextField(blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.IN_PROGRESS, db_index=True
    )
    refused = models.BooleanField(default=False, help_text="declined: does not count in the quota")
    refusal_reason = models.CharField(max_length=20, blank=True)  # secrets | access | data | other
    tools = models.JSONField(default=list, blank=True, help_text="look-ups made, in order")
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
        verbose_name = "help question"

    def __str__(self):
        return self.question[:80]

    @property
    def tokens(self) -> int:
        """The whole prompt (cached or not) and the output."""
        return self.input_tokens + self.cache_read_tokens + self.output_tokens
