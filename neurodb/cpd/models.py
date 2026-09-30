"""The Country Programme (CPD) results framework, its documents and what measures its progress.

A cycle (2, 3 or 5 years) has documents (the CPD, its results and resources framework, reviews),
outcomes, outputs under the outcomes, and indicators under an outcome or an output. An indicator
has a baseline, an end-of-cycle target, optional yearly milestones, and its progress comes from
values typed in the admin and from linked sources: eTools PD indicators (partner reporting),
ActivityInfo master indicators, Compiler youth indicators and Makani / Dirasa enrolment.
"""

from __future__ import annotations

import os
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

DOCUMENT_EXTENSIONS = ("pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt")
MAX_DOCUMENT_MB = 50


class CountryProgramme(models.Model):
    name = models.CharField(max_length=200, help_text=_("e.g. Lebanon Country Programme 2026-2028"))
    start_year = models.PositiveSmallIntegerField()
    end_year = models.PositiveSmallIntegerField(help_text=_("the last year of the cycle (2, 3 or 5 years)"))
    etools_name = models.CharField(
        _("country programme in eTools"),
        max_length=200,
        blank=True,
        help_text=_(
            "the country programme as eTools writes it on programme documents, to find the interventions"
        ),
    )
    current = models.BooleanField(default=False, help_text=_("the cycle the dashboard opens on"))
    summary = models.TextField(blank=True)
    updated_by = models.CharField(max_length=150, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-start_year", "name")
        verbose_name = _("country programme cycle")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.current:  # one current cycle
            CountryProgramme.objects.exclude(pk=self.pk).filter(current=True).update(current=False)

    def clean(self):
        if self.start_year and self.end_year:
            if self.end_year < self.start_year:
                raise ValidationError({"end_year": _("The cycle ends before it starts.")})
            if self.end_year - self.start_year + 1 > 7:
                raise ValidationError({"end_year": _("A cycle lasts 7 years at most.")})

    @property
    def years(self) -> list[int]:
        return list(range(self.start_year, self.end_year + 1))


def document_path(instance: CPDocument, filename: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(filename))
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)[:80] or "document"
    return f"cpd/{instance.programme_id or 'new'}/{uuid.uuid4().hex[:8]}-{safe}{ext.lower()}"


def max_size(file) -> None:
    if file.size > MAX_DOCUMENT_MB * 1024 * 1024:
        raise ValidationError(_("The file is larger than %(mb)s MB.") % {"mb": MAX_DOCUMENT_MB})


class CPDocument(models.Model):
    class Kind(models.TextChoices):
        CPD = "cpd", _("Country programme document (CPD)")
        RRF = "rrf", _("Results and resources framework")
        ANNEX = "annex", _("Annex or programme strategy note")
        REVIEW = "review", _("Review or evaluation")
        OTHER = "other", _("Other material")

    programme = models.ForeignKey(CountryProgramme, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=250)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CPD)
    file = models.FileField(
        upload_to=document_path,
        max_length=300,
        validators=[FileExtensionValidator(DOCUMENT_EXTENSIONS), max_size],
        help_text=_("PDF, Word, Excel or PowerPoint, %(mb)s MB at most") % {"mb": MAX_DOCUMENT_MB},
    )
    note = models.CharField(max_length=500, blank=True)
    uploaded_by = models.CharField(max_length=150, blank=True, editable=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("programme", "kind", "title")
        verbose_name = _("CPD document")

    def __str__(self):
        return self.title

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name or "")


class Origin(models.TextChoices):
    MANUAL = "manual", _("Entered in the admin")
    IMPORTED = "imported", _("Imported from Excel")
    AI = "ai_suggested", _("AI-suggested")


class Outcome(models.Model):
    programme = models.ForeignKey(CountryProgramme, on_delete=models.CASCADE, related_name="outcomes")
    code = models.CharField(max_length=20, help_text=_("e.g. 1"))
    title = models.TextField()
    sections = models.CharField(max_length=300, blank=True, help_text=_("the eTools sections in charge"))
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.MANUAL)

    class Meta:
        ordering = ("programme", "code")
        constraints = [models.UniqueConstraint(fields=["programme", "code"], name="cpd_outcome_code")]

    def __str__(self):
        return f"Outcome {self.code}: {self.title[:80]}"


class Output(models.Model):
    outcome = models.ForeignKey(Outcome, on_delete=models.CASCADE, related_name="outputs")
    code = models.CharField(max_length=20, help_text=_("e.g. 1.1"))
    title = models.TextField()
    etools_output = models.CharField(
        _("eTools CP output"),
        max_length=300,
        blank=True,
        help_text=_("the output as eTools names it on programme documents (blank: matched by its code)"),
    )
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.MANUAL)

    class Meta:
        ordering = ("outcome", "code")

    def __str__(self):
        return f"Output {self.code}: {self.title[:80]}"


class Indicator(models.Model):
    class Unit(models.TextChoices):
        NUMBER = "number", _("Number")
        PERCENT = "percent", _("Percentage")

    class Direction(models.TextChoices):
        UP = "increase", _("Increase")
        DOWN = "decrease", _("Decrease")

    class Accumulate(models.TextChoices):
        LATEST = "latest", _("Latest year's value (a level, e.g. a rate)")
        SUM = "sum", _("Sum over the cycle (e.g. children reached)")

    programme = models.ForeignKey(CountryProgramme, on_delete=models.CASCADE, related_name="indicators")
    outcome = models.ForeignKey(
        Outcome, null=True, blank=True, on_delete=models.CASCADE, related_name="indicators"
    )
    output = models.ForeignKey(
        Output, null=True, blank=True, on_delete=models.CASCADE, related_name="indicators"
    )
    code = models.CharField(max_length=30, blank=True)
    title = models.TextField()
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.NUMBER)
    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.UP)
    accumulate = models.CharField(
        max_length=10,
        choices=Accumulate.choices,
        default=Accumulate.LATEST,
        help_text=_("how the yearly values make the cycle's value"),
    )
    baseline = models.FloatField(null=True, blank=True)
    baseline_year = models.PositiveSmallIntegerField(null=True, blank=True)
    target = models.FloatField(null=True, blank=True, help_text=_("end-of-cycle target"))
    means_of_verification = models.TextField(blank=True)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.MANUAL)

    class Meta:
        ordering = ("programme", "code", "id")

    def __str__(self):
        return f"{self.code} {self.title[:90]}".strip()

    def save(self, *args, **kwargs):
        if not self.programme_id:
            self.programme_id = (
                self.outcome.programme_id if self.outcome_id else self.output.outcome.programme_id
            )
        super().save(*args, **kwargs)

    def clean(self):
        if bool(self.outcome_id) == bool(self.output_id):
            raise ValidationError(_("An indicator belongs to one outcome or to one output."))
        parent_programme = self.outcome.programme_id if self.outcome_id else self.output.outcome.programme_id
        if self.programme_id and parent_programme != self.programme_id:
            raise ValidationError(_("The outcome or output belongs to another cycle."))


class Milestone(models.Model):
    indicator = models.ForeignKey(Indicator, on_delete=models.CASCADE, related_name="milestones")
    year = models.PositiveSmallIntegerField()
    value = models.FloatField()

    class Meta:
        ordering = ("indicator", "year")
        constraints = [models.UniqueConstraint(fields=["indicator", "year"], name="cpd_milestone_year")]

    def __str__(self):
        return f"{self.year}: {self.value:g}"


class Value(models.Model):
    """A value typed in the admin (a survey, ministry data, an annual report). It replaces the
    linked sources for its year."""

    indicator = models.ForeignKey(Indicator, on_delete=models.CASCADE, related_name="values")
    year = models.PositiveSmallIntegerField()
    value = models.FloatField()
    source = models.CharField(max_length=250, help_text=_("e.g. MICS 2027, MEHE data, annual report"))
    note = models.CharField(max_length=500, blank=True)
    updated_by = models.CharField(max_length=150, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("indicator", "year")
        constraints = [models.UniqueConstraint(fields=["indicator", "year"], name="cpd_value_year")]

    def __str__(self):
        return f"{self.year}: {self.value:g} ({self.source})"


class Link(models.Model):
    """A source whose figure counts toward an indicator for one year."""

    class Kind(models.TextChoices):
        ETOOLS = "etools", _("eTools PD indicator (partner reporting)")
        ACTIVITYINFO = "activityinfo", _("ActivityInfo master indicator")
        YOUTH = "youth", _("Compiler youth indicator")
        EDUCATION = "education", _("Compiler Makani / Dirasa enrolment")

    indicator = models.ForeignKey(Indicator, on_delete=models.CASCADE, related_name="links")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    year = models.PositiveSmallIntegerField(help_text=_("the CPD year this source counts for"))
    # eTools: the PD and the indicator key used by partner monitoring
    pd = models.ForeignKey(
        "etools.PCA", null=True, blank=True, on_delete=models.SET_NULL, db_constraint=False, related_name="+"
    )
    etools_key = models.CharField(max_length=32, blank=True)
    # ActivityInfo: a master indicator of that year's database
    master = models.ForeignKey(
        "pivoting.MasterIndicator",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="+",
    )
    # Compiler youth: the indicator's level and id in Compiler, and the Compiler year
    youth_level = models.CharField(max_length=8, blank=True)
    youth_id = models.PositiveIntegerField(null=True, blank=True)
    # Compiler education: the programme (mscc / bridging) and the stored year or round
    education_programme = models.CharField(max_length=16, blank=True)
    source_period = models.CharField(
        max_length=64, blank=True, help_text=_("Compiler's year or round of the figures (youth, education)")
    )
    label = models.CharField(max_length=500, help_text=_("what the source is, as shown on the dashboard"))
    confirmed = models.BooleanField(default=True, help_text=_("suggested links count only once confirmed"))
    updated_by = models.CharField(max_length=150, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("indicator", "year", "kind")

    def __str__(self):
        return f"{self.get_kind_display()} {self.year}: {self.label[:80]}"

    def clean(self):
        needed = {
            self.Kind.ETOOLS: self.pd_id and self.etools_key,
            self.Kind.ACTIVITYINFO: self.master_id,
            self.Kind.YOUTH: self.youth_level and self.youth_id and self.source_period,
            self.Kind.EDUCATION: self.education_programme and self.source_period,
        }
        if not needed.get(self.kind):
            raise ValidationError(_("This source is incomplete."))


class FrameworkProposal(models.Model):
    """What the AI extraction proposed from a CPD document, kept until an admin applies it."""

    class Status(models.TextChoices):
        RUNNING = "running", _("Reading the document")
        READY = "ready", _("Ready to review")
        APPLIED = "applied", _("Applied")
        FAILED = "failed", _("Failed")

    document = models.ForeignKey(CPDocument, on_delete=models.CASCADE, related_name="proposals")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    items = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("AI-suggested framework")
        verbose_name_plural = _("AI-suggested frameworks")

    def __str__(self):
        return f"{self.document} ({self.get_status_display()})"
