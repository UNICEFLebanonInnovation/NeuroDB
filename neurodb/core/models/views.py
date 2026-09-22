from django.conf import settings
from django.db import models
from django.utils import timezone


class SavedView(models.Model):
    """A bookmarked page state (filters, pivot layout) that a user can name and share (new in v3)."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=120)
    page = models.CharField(
        max_length=64, db_index=True, help_text="route name, e.g. reports:database_analytical"
    )
    object_id = models.PositiveIntegerField(null=True, blank=True, help_text="e.g. the database id")
    query = models.JSONField(default=dict, blank=True, help_text="query-string parameters")
    layout = models.JSONField(default=dict, blank=True, help_text="pivot rows/cols/aggregator/renderer")
    is_shared = models.BooleanField(default=False, help_text="visible to every signed-in user")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        unique_together = (("owner", "page", "object_id", "name"),)

    def __str__(self):
        return self.name
