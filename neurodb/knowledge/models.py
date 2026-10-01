"""The knowledge base of Ask NeuroDB: documents and texts people add, cut into passages with a
full-text index, and linked to the partners, programme documents, sections and places they mention.
"""

from __future__ import annotations

import os
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

EXTENSIONS = ("pdf", "docx", "txt", "md", "csv", "xlsx", "pptx")
MAX_FILE_MB = 50


def document_path(instance: Document, filename: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(filename))
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)[:80] or "document"
    return f"knowledge/{uuid.uuid4().hex[:12]}-{safe}{ext.lower()}"


def max_size(file) -> None:
    if file.size > MAX_FILE_MB * 1024 * 1024:
        raise ValidationError(_("The file is larger than %(mb)s MB.") % {"mb": MAX_FILE_MB})


class Document(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Waiting to be read")
        INDEXING = "indexing", _("Being read")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    title = models.CharField(max_length=300)
    file = models.FileField(
        upload_to=document_path,
        max_length=300,
        blank=True,
        validators=[FileExtensionValidator(EXTENSIONS), max_size],
    )
    text = models.TextField(blank=True, help_text=_("the text typed or pasted, or read from the file"))
    source = models.CharField(
        max_length=300, blank=True, help_text=_("where it comes from, e.g. 'Mid-term review, MEHE, 2026'")
    )
    section = models.ForeignKey(
        "users.Section",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="+",
    )
    year = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("the year it is about"))
    # filled when the document is read (the summary and key points are AI-written)
    summary = models.TextField(blank=True)
    key_points = models.JSONField(default=list, blank=True)
    document_date = models.DateField(null=True, blank=True)
    pages = models.PositiveIntegerField(default=0)
    characters = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    error = models.TextField(blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("knowledge document")

    def __str__(self):
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("knowledge:detail", args=[self.pk])

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name or "")


class Chunk(models.Model):
    """A passage of a document (about 1,500 characters), the unit that is searched and quoted."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    page = models.PositiveIntegerField(null=True, blank=True)
    text = models.TextField()
    search_vector = SearchVectorField(null=True)

    class Meta:
        ordering = ("document", "position")
        constraints = [
            models.UniqueConstraint(fields=["document", "position"], name="knowledge_chunk_position")
        ]
        indexes = [GinIndex(fields=["search_vector"], name="knowledge_chunk_search")]

    def __str__(self):
        return f"{self.document_id}#{self.position}"


class Link(models.Model):
    """Something in NeuroDB a document mentions."""

    class Kind(models.TextChoices):
        PARTNER = "partner", _("Partner")
        PROGRAMME = "programme_document", _("Programme document")
        SECTION = "section", _("Section")
        GOVERNORATE = "governorate", _("Governorate")
        DISTRICT = "district", _("District")

    class Origin(models.TextChoices):
        DETECTED = "detected", _("Found in the text")
        AI = "ai", _("AI-suggested")
        MANUAL = "manual", _("Added by hand")

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="links")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    object_id = models.BigIntegerField(
        help_text=_("the id of the partner, programme document, section or place")
    )
    label = models.CharField(max_length=300, blank=True)
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.MANUAL)
    mentions = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("kind", "-mentions", "label")
        constraints = [
            models.UniqueConstraint(fields=["document", "kind", "object_id"], name="knowledge_link_unique")
        ]
        indexes = [models.Index(fields=["kind", "object_id"])]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.label}"

    @property
    def url(self) -> str | None:
        if self.kind == self.Kind.PARTNER:
            return reverse("reports:partner_profile", args=[self.object_id])
        if self.kind == self.Kind.PROGRAMME:
            return reverse("reports:programme_detail", args=[self.object_id])
        return None
