"""Monitoring insights' own tables. For now: what the eTools field monitoring records hold
(:class:`KeyProbe`) and which key each logical field is read from (:class:`FieldMapping`), both
rewritten by every refresh (``fmm_refresh``) and shown in the admin as "Fields found".

Neither table holds a narrative, an answer or a person: a probe keeps at most three short examples per
key, redacted (``privacy.example``), and "(withheld)" for a key that holds a person.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class KeyProbe(models.Model):
    """One key of one dataset's records, as the last refresh found it: how many records hold it, the
    types of its values and up to three redacted examples."""

    dataset = models.CharField(max_length=64)  # field_monitoring | fm_questions | fm_options | ...
    key = models.CharField(max_length=120)  # top level, or "parent.child" one level down
    records = models.PositiveIntegerField()
    total = models.PositiveIntegerField()
    types = models.JSONField(default=dict)  # {"str": 120, "dict": 4, "null": 2}
    examples = models.JSONField(default=list)  # <= 3, privacy.example(); "(withheld)" for person keys
    refreshed_at = models.DateTimeField()

    class Meta:
        ordering = ("dataset", "key")
        constraints = [models.UniqueConstraint(fields=["dataset", "key"], name="fmm_key_probe")]
        verbose_name = "key found in the data"
        verbose_name_plural = "keys found in the data"

    def __str__(self):
        return f"{self.dataset}.{self.key}"


class FieldMapping(models.Model):
    """Which key of a dataset's records a logical field (the answer, the activity id...) is read from,
    chosen by the refresh from the candidates of ``fields.CANDIDATES`` (``fields.resolve``), unless an
    administrator pinned another key the data shows (``override_key``)."""

    class State(models.TextChoices):
        FOUND = "found", "Found"
        OVERRIDE = "override", "Set by an administrator"
        OVERRIDE_MISSING = "override_missing", "Set key not in the data"
        AMBIGUOUS = "ambiguous", "Found, another key is fuller"
        MISSING = "missing", "Not found"

    dataset = models.CharField(max_length=64)
    field = models.CharField(max_length=40)  # logical field (fields.CANDIDATES)
    chosen_key = models.CharField(max_length=120, blank=True)
    coverage = models.FloatField(default=0)  # share of records with a usable value under chosen_key
    candidates = models.JSONField(default=list)  # [{"key", "coverage"}] for every candidate present
    state = models.CharField(max_length=16, choices=State.choices, default=State.MISSING)
    override_key = models.CharField(max_length=120, blank=True, help_text="pin a key the data shows")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("dataset", "field")
        constraints = [models.UniqueConstraint(fields=["dataset", "field"], name="fmm_field_mapping")]
        verbose_name = "field found"
        verbose_name_plural = "fields found"

    def __str__(self):
        return f"{self.dataset}.{self.field}"
