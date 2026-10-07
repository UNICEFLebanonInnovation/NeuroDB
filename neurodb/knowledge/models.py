"""The knowledge base of Ask NeuroDB: documents and texts people add, cut into passages with a
full-text index, and linked to the partners, programme documents, sections and places they mention.

Periodic reports (a snapshot or situation report issued again and again under the same name, with
its number and date) are grouped in a series, and the figures of each edition are kept as data
(``ReportFigure``), so that they can be followed over time (see ``periodic.py``).
"""

from __future__ import annotations

import os
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
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


class ReportSeries(models.Model):
    """A report issued again and again under the same name (e.g. "Escalation of hostilities - UNICEF
    snapshot"); each edition is a document with its number and issue date."""

    key = models.CharField(max_length=200, unique=True, help_text=_("the name without number and date"))
    name = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)
        verbose_name = _("periodic report")
        verbose_name_plural = _("periodic reports")

    def __str__(self):
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("knowledge:series", args=[self.pk])


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

    class Origin(models.TextChoices):
        ADDED = "added", _("Added to the knowledge base")
        LIBRARY = "library", _("Library publication")
        CPD = "cpd", _("Country programme document")

    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.ADDED)
    origin_id = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("the publication or CPD document")
    )
    origin_signature = models.CharField(
        max_length=300, blank=True, help_text=_("tells when the source changed")
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    indexed_at = models.DateTimeField(null=True, blank=True)

    # an edition of a periodic report: its figures are kept as data
    class FiguresStatus(models.TextChoices):
        NONE = "", _("Not a periodic report")
        PENDING = "pending", _("Figures waiting to be read")
        READ = "read", _("Figures read")
        FAILED = "failed", _("Figures could not be read")

    periodic = models.BooleanField(
        default=False, help_text=_("an edition of a report issued periodically: its figures are kept by date")
    )
    series = models.ForeignKey(
        ReportSeries, null=True, blank=True, on_delete=models.SET_NULL, related_name="editions"
    )
    edition = models.PositiveIntegerField(null=True, blank=True, help_text=_("its number, e.g. 37"))
    issued_on = models.DateField(null=True, blank=True)
    figures_status = models.CharField(max_length=10, choices=FiguresStatus.choices, default="", blank=True)
    figures_note = models.TextField(blank=True)
    figures_read_at = models.DateTimeField(null=True, blank=True)

    # the document review (``review.py``): only a document in a batch, and not a reference, is analysed
    class ReviewStatus(models.TextChoices):
        NOT_IN_REVIEW = "not_in_review", _("Not in the review")
        PENDING = "pending", _("Waiting to be analysed")
        RUNNING = "running", _("Being analysed")
        DONE = "done", _("Analysed")
        PARTLY = "partly", _("Partly analysed")
        FAILED = "failed", _("Analysis failed")
        REFERENCE = "reference", _("Reference only")

    review_batch = models.ForeignKey(
        "ReviewBatch", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents"
    )
    review_status = models.CharField(
        max_length=14, choices=ReviewStatus.choices, default=ReviewStatus.NOT_IN_REVIEW, db_index=True
    )
    review_stage_notes = models.JSONField(
        default=dict, blank=True, help_text=_("per stage: yes, partly or failed, and why")
    )
    reviewed_text_hash = models.CharField(max_length=64, blank=True)
    review_progress_at = models.DateTimeField(
        null=True, blank=True, help_text=_("the last step of the analysis (none for 30 minutes: given up)")
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, help_text=_("when the last analysis ended"))

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("knowledge document")
        constraints = [
            models.UniqueConstraint(
                fields=["origin", "origin_id"],
                condition=~models.Q(origin="added"),
                name="knowledge_one_per_source",
            )
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("knowledge:detail", args=[self.pk])

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name or "")

    @property
    def origin_url(self) -> str:
        if self.origin == self.Origin.LIBRARY and self.origin_id:
            return reverse("reports:library_item", args=[self.origin_id])
        if self.origin == self.Origin.CPD and self.origin_id:
            return reverse("cpd:document", args=[self.origin_id])
        return ""


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


class ReportFigure(models.Model):
    """One figure of an edition of a periodic report: what is counted, the value, the date it is
    about, and where it was read (page, words around it). The same measure in every edition shares
    ``key``, so the figures line up over time; when two editions give a value for the same date,
    the newer edition's counts (see ``periodic.timeline``)."""

    class Method(models.TextChoices):
        CHART = "chart", _("Read from a chart")
        TEXT = "text", _("Read from the text (AI)")

    series = models.ForeignKey(ReportSeries, on_delete=models.CASCADE, related_name="figures")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="figures")
    key = models.CharField(max_length=300, help_text=_("the same measure in every edition"))
    group = models.CharField(max_length=200, blank=True, help_text=_("heading or sector, e.g. Health"))
    metric = models.CharField(max_length=300)
    breakdown = models.CharField(
        max_length=150, blank=True, help_text=_("e.g. children; empty for the total")
    )
    unit = models.CharField(max_length=40, blank=True)
    is_percent = models.BooleanField(default=False)
    value = models.DecimalField(max_digits=20, decimal_places=4)
    target = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    as_of = models.DateField(help_text=_("the date the figure is about"))
    period = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=200, blank=True)
    internal = models.BooleanField(default=False, help_text=_("marked for internal use in the report"))
    page = models.PositiveIntegerField(null=True, blank=True)
    quote = models.CharField(max_length=500, blank=True)
    method = models.CharField(max_length=10, choices=Method.choices)

    class Meta:
        ordering = ("series", "group", "metric", "breakdown", "as_of")
        verbose_name = _("report figure")
        indexes = [models.Index(fields=["series", "key", "as_of"])]
        constraints = [
            models.UniqueConstraint(fields=["document", "key", "as_of"], name="knowledge_figure_once")
        ]

    def __str__(self):
        return f"{self.label}: {self.value} ({self.as_of})"

    @property
    def label(self) -> str:
        return " — ".join(p for p in (self.metric, self.breakdown) if p)


# ------------------------------------------------------------------------------- document review
# FMS §9 "Other Reports", built on the knowledge base: documents gathered in batches are read by the AI
# into findings with evidence, key statements and action points (``review.py``), which people accept or
# reject. Only documents in a batch are analysed, so the AI's cost stays with what was chosen.


class ReviewBatch(models.Model):
    """A folder of documents of one kind (annual reports, donor reports, evaluations…): the Coverage
    view tells when a theme rests on one kind of source only."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    archived = models.BooleanField(default=False, help_text=_("kept, but no longer analysed or listed first"))

    class Meta:
        ordering = ("archived", "name")
        verbose_name = _("document review batch")
        verbose_name_plural = _("document review batches")

    def __str__(self):
        return self.name


class TopicProgramme(models.Model):
    """The first level of the document review's topics (e.g. Education)."""

    name = models.CharField(max_length=120, unique=True)
    order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("order", "name")
        verbose_name = _("topic programme")

    def __str__(self):
        return self.name


class TopicSubtopic(models.Model):
    """The second level (e.g. Education → Access to learning)."""

    programme = models.ForeignKey(TopicProgramme, on_delete=models.CASCADE, related_name="subtopics")
    name = models.CharField(max_length=120)
    order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("programme__order", "programme__name", "order", "name")
        verbose_name = _("topic subtopic")
        constraints = [models.UniqueConstraint(fields=["programme", "name"], name="knowledge_subtopic_once")]

    def __str__(self):
        return f"{self.programme} › {self.name}"


class Topic(models.Model):
    """A tag the AI gives a finding (e.g. Education → Access to learning → Out-of-school children). A tag
    the AI invents becomes "Other", which always exists (``other``)."""

    OTHER = "Other"

    subtopic = models.ForeignKey(TopicSubtopic, on_delete=models.CASCADE, related_name="topics")
    name = models.CharField(max_length=120)
    order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = (
            "subtopic__programme__order",
            "subtopic__programme__name",
            "subtopic__order",
            "order",
            "name",
        )
        verbose_name = _("topic")
        constraints = [models.UniqueConstraint(fields=["subtopic", "name"], name="knowledge_topic_once")]

    def __str__(self):
        return f"{self.subtopic} › {self.name}"

    @property
    def path(self) -> str:
        return f"{self.subtopic.programme.name} › {self.subtopic.name} › {self.name}"

    @property
    def is_other(self) -> bool:
        return self.name == self.OTHER and self.subtopic.name == self.OTHER

    @classmethod
    def other(cls) -> Topic:
        """The "Other" tag (under the "Other" programme and subtopic), made again if it was removed."""
        programme, _created = TopicProgramme.objects.get_or_create(
            name=cls.OTHER, defaults={"order": 999, "active": True}
        )
        subtopic, _created = TopicSubtopic.objects.get_or_create(programme=programme, name=cls.OTHER)
        return cls.objects.get_or_create(subtopic=subtopic, name=cls.OTHER)[0]


class Verdict(models.TextChoices):
    UNREVIEWED = "unreviewed", _("Not reviewed")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")


class FindingCategory(models.TextChoices):
    CHALLENGE = "challenge", _("Challenge")
    RECOMMENDATION = "recommendation", _("Recommendation")
    OBSERVATION = "observation", _("Observation")
    ACTION_POINT = "action_point", _("Action point")


class Reviewed(models.Model):
    """What a person decides about a finding or statement: accepted, rejected or not reviewed yet."""

    verdict = models.CharField(
        max_length=10, choices=Verdict.choices, default=Verdict.UNREVIEWED, db_index=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class DocumentFinding(Reviewed):
    """One point a document makes, with the words that support it and where they are. The evidence score
    (0-100) is worked out by NeuroDB from what can be checked, never by the model: the quote found in
    the text 45, reports rather than interprets 25, dated 10, placed 10, tagged (not Other) 10."""

    class Kind(models.TextChoices):
        REPORTED = "reported", _("Reported by the document")
        INTERPRETED = "interpreted", _("Interpreted from it")

    class PlaceMatch(models.TextChoices):
        NONE = "", _("No place")
        GENERAL = "general", _("Lebanon (country-wide)")
        GOVERNORATE = "governorate", _("Governorate")
        DISTRICT = "district", _("District")
        UNMATCHED = "unmatched", _("Not recognised")

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="findings")
    position = models.PositiveIntegerField(default=0)
    category = models.CharField(max_length=14, choices=FindingCategory.choices, db_index=True)
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="findings")
    tag_text = models.CharField(max_length=200, blank=True, help_text=_("the tag as the AI wrote it"))
    text = models.TextField()
    quote = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.REPORTED)
    # where: the exact page when the quote is found in the text, else the range of the part it came from
    page_label = models.CharField(max_length=80, blank=True)
    page_from = models.PositiveIntegerField(null=True, blank=True)
    page_to = models.PositiveIntegerField(null=True, blank=True)
    chunk_from = models.PositiveIntegerField(null=True, blank=True, help_text=_("the part's first page"))
    chunk_to = models.PositiveIntegerField(null=True, blank=True)
    quote_found = models.BooleanField(default=False)
    exact_page = models.BooleanField(default=False)
    place_text = models.CharField(max_length=200, blank=True)
    place_match = models.CharField(max_length=12, choices=PlaceMatch.choices, default="", blank=True)
    governorate_id = models.BigIntegerField(null=True, blank=True)
    governorate_name = models.CharField(max_length=100, blank=True)
    district_id = models.BigIntegerField(null=True, blank=True)
    district_name = models.CharField(max_length=100, blank=True)
    finding_date = models.DateField(null=True, blank=True)
    date_text = models.CharField(max_length=100, blank=True)
    evidence = models.PositiveSmallIntegerField(default=0, db_index=True)
    derived = models.BooleanField(
        default=False, help_text=_("made by the enrichment for an action point no finding states")
    )
    manual = models.BooleanField(default=False, help_text=_("added by a person; re-analysis keeps it"))
    model_key = models.CharField(
        max_length=64, blank=True, db_index=True, help_text=_("the text and quote as the AI wrote them")
    )
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("document", "position", "pk")
        verbose_name = _("document finding")
        indexes = [models.Index(fields=["document", "verdict"]), models.Index(fields=["topic", "category"])]

    def __str__(self):
        return self.text[:80]

    @property
    def placed(self) -> bool:
        return self.place_match in (
            self.PlaceMatch.GENERAL,
            self.PlaceMatch.GOVERNORATE,
            self.PlaceMatch.DISTRICT,
        )

    @property
    def url(self) -> str:
        """The document's file at the finding's page (a PDF opens there), else its knowledge base page."""
        if self.document.file and self.page_from:
            return f"{reverse('knowledge:file', args=[self.document_id])}#page={self.page_from}"
        return reverse("knowledge:detail", args=[self.document_id])


class DocumentStatement(Reviewed):
    """One key statement of a document, citing the findings it rests on, with its urgency (0-100)."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="statements")
    position = models.PositiveIntegerField(default=0)
    text = models.TextField()
    urgency = models.PositiveSmallIntegerField(default=0)
    category = models.CharField(max_length=14, choices=FindingCategory.choices, blank=True)
    topic = models.ForeignKey(
        Topic, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text=_("the topic most of its findings have"),
    )  # fmt: skip
    place_text = models.CharField(max_length=200, blank=True)
    date_text = models.CharField(max_length=100, blank=True)
    cites = models.ManyToManyField(DocumentFinding, blank=True, related_name="statements")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("document", "-urgency", "position")
        verbose_name = _("document statement")

    def __str__(self):
        return self.text[:80]


class DocumentActionPoint(models.Model):
    """A commitment a document states: who, what, by when. Its status is set by people only; a new
    analysis keeps it (matched on the action's words)."""

    class Priority(models.TextChoices):
        HIGH = "high", _("High")
        MEDIUM = "medium", _("Medium")
        LOW = "low", _("Low")
        UNRATED = "unrated", _("Not rated")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        DONE = "done", _("Done")
        DROPPED = "dropped", _("Dropped")

    UNASSIGNED = "Unassigned"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="review_action_points")
    position = models.PositiveIntegerField(default=0)
    action = models.TextField()
    action_key = models.CharField(max_length=64, db_index=True, help_text=_("the action's words, normalised"))
    owner_text = models.CharField(max_length=200, default=UNASSIGNED)
    deadline_text = models.CharField(max_length=100, blank=True)
    deadline_date = models.DateField(null=True, blank=True, help_text=_("a quarter or year: its last day"))
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.UNRATED)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    status_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status_at = models.DateTimeField(null=True, blank=True)
    derived = models.BooleanField(
        default=False, help_text=_("drawn from challenges or recommendations: no finding states it as such")
    )
    topic = models.ForeignKey(Topic, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cites = models.ManyToManyField(DocumentFinding, blank=True, related_name="action_points")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("document", "position")
        verbose_name = _("document action point")

    def __str__(self):
        return self.action[:80]


def _default_prompt(stage: str):
    from . import review_prompts

    return review_prompts.DEFAULTS[stage]


class DocumentReviewSettings(models.Model):
    """The one row (pk=1) of the document review's settings, for Administrators (admin): the switch,
    the three prompts (shipped text in ``review_prompts``; "Restore default" puts it back), the sizes
    and the daily token cap. A prompt without the sentence naming its JSON is refused."""

    enabled = models.BooleanField(
        default=False, help_text=_("off until an administrator turns it on: nothing is analysed meanwhile")
    )
    tagging_prompt = models.TextField(
        help_text=_("stage 1: how the findings are found and tagged"),
        default="",
    )
    summary_prompt = models.TextField(help_text=_("stage 2: how the key statements are written"), default="")
    enrichment_prompt = models.TextField(
        help_text=_("stage 3: how the action points are drawn from the findings"), default=""
    )
    statements_per_document = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(100)],
        help_text=_("at most (fewer for short documents, never under 5); 0 = the default, 20"),
    )
    max_findings_per_chunk = models.PositiveSmallIntegerField(
        default=25, validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    chunk_size = models.PositiveIntegerField(
        default=12000,
        validators=[MinValueValidator(2000), MaxValueValidator(60000)],
        help_text=_("characters of the document read in one AI call"),
    )
    daily_token_cap = models.PositiveIntegerField(
        default=0, help_text=_("tokens a day for the document review; 0 = DOC_REVIEW_DAILY_TOKEN_CAP")
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    PROMPT_FIELDS = {
        "tagging": "tagging_prompt",
        "summary": "summary_prompt",
        "enrichment": "enrichment_prompt",
    }
    DEFAULT_STATEMENTS = 20
    MIN_STATEMENTS = 5

    class Meta:
        verbose_name = _("document review settings")
        verbose_name_plural = _("document review settings")

    def __str__(self):
        return "Document review settings"

    def save(self, *args, **kwargs):
        for stage, field in self.PROMPT_FIELDS.items():  # an empty prompt is the shipped one
            if not (getattr(self, field) or "").strip():
                setattr(self, field, _default_prompt(stage))
        super().save(*args, **kwargs)

    def clean(self):
        from . import review_prompts

        errors = {}
        for stage, field in self.PROMPT_FIELDS.items():
            text = getattr(self, field) or ""
            sentence = review_prompts.REQUIRED_SENTENCES[stage]
            if text.strip() and sentence not in text:
                errors[field] = _(
                    "Keep the sentence %(sentence)s: without it this stage runs and finds nothing."
                ) % {"sentence": f"“{sentence}”"}
        if errors:
            raise ValidationError(errors)

    @classmethod
    def load(cls) -> DocumentReviewSettings:
        found = cls.objects.filter(pk=1).first()
        if found is None:
            found = cls(pk=1)
            found.save()
        return found

    def prompt(self, stage: str) -> str:
        return (getattr(self, self.PROMPT_FIELDS[stage]) or "").strip() or _default_prompt(stage)

    def is_default(self, stage: str) -> bool:
        return self.prompt(stage) == _default_prompt(stage).strip()

    @property
    def statements_limit(self) -> int:
        return self.statements_per_document or self.DEFAULT_STATEMENTS

    @property
    def token_cap(self) -> int:
        return self.daily_token_cap or settings.DOC_REVIEW_DAILY_TOKEN_CAP
