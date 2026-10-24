"""The KM Database: one evidence workbook (its documents and their findings), uploaded in the admin and shown
on its own page. Kept apart from the rest of NeuroDB: its only relations are to its own tables and to the
person who uploaded a version, and nothing outside this app reads its tables.

One version is shown at a time (``KMVersion.Status.LIVE``): a new upload is checked, then loaded by a
background job (:mod:`neurodb.km.loading`) that replaces every document and finding in one transaction.
Texts are kept as the workbook gives them (stripped, line breaks as ``\\n``); codes are worked out beside
them. A value that is not one of the expected ones is kept as given in ``checks`` and marked on the page.
"""

from __future__ import annotations

import time

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q
from django.utils import formats
from django.utils.translation import gettext_lazy as _


class KMVersion(models.Model):
    """One uploaded workbook. The uploaded file is kept only until its load ends; a version that is no
    longer shown keeps its summary only (its rows are replaced by the next version's)."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Waiting to load")
        LOADING = "loading", _("Loading")
        LIVE = "live", _("Shown now")
        REPLACED = "replaced", _("Replaced by a newer version")
        FAILED = "failed", _("Not loaded")

    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    file_name = models.CharField(_("file"), max_length=255)
    file_size = models.PositiveIntegerField(_("size (bytes)"), default=0)
    file_sha256 = models.CharField(_("file fingerprint (SHA-256)"), max_length=64)
    # the uploaded bytes, until the load ends (loaded or not): then emptied in the same save
    content = models.BinaryField(null=True, editable=False)
    fewer_ok = models.BooleanField(_("fewer findings on purpose"), default=False)
    prepared_on = models.DateField(_("prepared on"), null=True, blank=True)
    source_name = models.CharField(_("source workbook (its name only)"), max_length=255, blank=True)
    stated_documents = models.PositiveIntegerField(_("documents the Read Me states"), null=True, blank=True)
    stated_findings = models.PositiveIntegerField(_("findings the Read Me states"), null=True, blank=True)
    documents_count = models.PositiveIntegerField(_("documents"), default=0)
    findings_count = models.PositiveIntegerField(_("findings"), default=0)
    review_count = models.PositiveIntegerField(_("marked for review"), default=0)
    check_count = models.PositiveIntegerField(_("rows with a value to check"), default=0)
    # plain sentences, at most 100 each: never a cell's text other than IDs, column names and sheet names
    problems = models.JSONField(_("problems"), default=list, blank=True)
    warnings = models.JSONField(_("warnings"), default=list, blank=True)
    download_summary = models.JSONField(_("downloads summary"), default=dict, blank=True)
    peak_memory_mb = models.PositiveIntegerField(_("peak memory of the load (MB)"), null=True, blank=True)
    load_seconds = models.PositiveIntegerField(_("load time (seconds)"), null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("uploaded by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    uploaded_at = models.DateTimeField(_("uploaded at"), auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(_("load started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("load finished at"), null=True, blank=True)
    error = models.TextField(_("error"), blank=True)  # our own sentence and the exception's class name only

    class Meta:
        db_table = "km_version"
        ordering = ("-uploaded_at",)
        verbose_name = _("version")
        verbose_name_plural = _("versions")
        constraints = [
            models.UniqueConstraint(fields=["status"], condition=Q(status="live"), name="km_one_live_version")
        ]

    def __str__(self) -> str:
        return f"{self.label()} ({self.get_status_display()})"

    def label(self) -> str:
        """The version's date, as every sentence that names a version says it."""
        if self.prepared_on:
            return _("prepared on %(date)s") % {"date": formats.date_format(self.prepared_on, "j M Y")}
        when = self.finished_at or self.uploaded_at
        return _("loaded on %(date)s") % {"date": formats.date_format(when, "j M Y") if when else "—"}

    @property
    def busy(self) -> bool:
        return self.status in (self.Status.QUEUED, self.Status.LOADING)


class Flag(models.TextChoices):
    """A finding's mark for an age group, girls or boys, a level of the social-ecological model or a theme;
    "" when the workbook leaves it empty (or gives a value to check)."""

    YES = "Y", _("Yes")
    NO = "N", _("No")
    NOT_REPORTED = "R", _("Not reported")
    NOT_APPLICABLE = "A", _("Not applicable")


class Confidence(models.TextChoices):
    A = "A", _("Level A")
    B = "B", _("Level B")
    C = "C", _("Level C")
    D = "D", _("Level D")


def _flag(label: str):
    return models.CharField(label, max_length=1, blank=True, choices=Flag.choices)


class KMDocument(models.Model):
    """One row of the documents sheet. ``entity`` (Publishing Entity / Authors) is shown on screen only:
    never in a download or printout as written, never to the AI."""

    position = models.PositiveIntegerField(_("row"))  # the Excel row number
    record_id = models.CharField(_("Document ID"), max_length=40, unique=True)
    item_title = models.TextField(_("short title"), blank=True)
    title = models.TextField(_("title"), blank=True)
    entity = models.TextField(_("publishing entity / authors"), blank=True)
    year = models.PositiveSmallIntegerField(_("year"), null=True, blank=True, db_index=True)
    year_text = models.CharField(_("year (as given)"), max_length=40, blank=True)
    study_type = models.CharField(_("document or study type"), max_length=200, blank=True, db_index=True)
    text_used = models.CharField(_("text used"), max_length=100, blank=True)
    confidence = models.CharField(
        _("confidence level"), max_length=1, blank=True, choices=Confidence.choices, db_index=True
    )
    confidence_text = models.CharField(_("confidence level (as given)"), max_length=200, blank=True)
    findings_stated = models.PositiveIntegerField(_("number of findings (as given)"), null=True, blank=True)
    link = models.TextField(_("link"), blank=True)
    quality = models.TextField(_("data quality"), blank=True)
    needs_review = models.BooleanField(_("marked for review"), default=False, db_index=True)
    checks = models.JSONField(_("values to check"), default=dict, blank=True)  # {column: value as given}
    search_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        db_table = "km_document"
        ordering = ("position",)
        verbose_name = _("document")
        verbose_name_plural = _("documents")
        indexes = [GinIndex(fields=["search_vector"], name="km_document_search")]

    def __str__(self) -> str:
        return self.record_id


class KMFinding(models.Model):
    """One row of the findings sheet, with the codes worked out from its labels (pillar, domain, research
    question) and its 27 marks (:data:`neurodb.km.columns.FLAGS`). ``entity`` is shown on screen only."""

    position = models.PositiveIntegerField(_("row"))
    finding_id = models.CharField(_("Finding ID"), max_length=40, unique=True)
    document = models.ForeignKey(KMDocument, on_delete=models.CASCADE, related_name="findings")
    record_id = models.CharField(_("Document ID"), max_length=40)  # as given
    item_title = models.TextField(_("short title"), blank=True)
    document_title = models.TextField(_("document title"), blank=True)
    pillar_code = models.CharField(_("pillar code"), max_length=8, blank=True, db_index=True)
    pillar = models.TextField(_("pillar"), blank=True)
    domain_code = models.CharField(_("analytical domain code"), max_length=8, blank=True, db_index=True)
    domain = models.TextField(_("analytical domain"), blank=True)
    rq_code = models.CharField(_("research question code"), max_length=8, blank=True)
    research_question = models.TextField(_("research question"), blank=True)
    # the pillar and the wording together (columns.question_key): one code is worded differently per pillar
    question_key = models.CharField(_("research question key"), max_length=12, blank=True, db_index=True)
    sub_question = models.TextField(_("sub-question"), blank=True)
    sub_sub_question = models.TextField(_("sub-sub-question"), blank=True)
    indicator = models.TextField(_("indicator or evidence asked for"), blank=True)
    indicator_component = models.TextField(_("part of the indicator answered"), blank=True)
    also_relevant = models.TextField(_("also relevant to"), blank=True)
    measure = models.TextField(_("measure"), blank=True)
    key_finding = models.TextField(_("key finding"), blank=True)
    analysis = models.TextField(_("finding analysis"), blank=True)
    age_text = models.TextField(_("age group (as given)"), blank=True)
    # the 27 marks, in the order of columns.FLAGS
    prenatal = _flag(_("before birth (pregnancy)"))
    early_childhood = _flag(_("ages 0-4"))
    middle_childhood = _flag(_("ages 5-9"))
    early_adolescence = _flag(_("ages 10-14"))
    late_adolescence = _flag(_("ages 15-19"))
    youth = _flag(_("youth (15-24)"))
    young_people = _flag(_("young people (10-24)"))
    adult_caregiver = _flag(_("adults and caregivers (20+)"))
    female = _flag(_("girls and women"))
    male = _flag(_("boys and men"))
    sem_individual = _flag(_("individual"))
    sem_household = _flag(_("household and family"))
    sem_community = _flag(_("community"))
    sem_institutional = _flag(_("institutions and services"))
    sem_policy = _flag(_("policies and laws"))
    disability = _flag(_("disability inclusion"))
    poverty = _flag(_("poverty and economic vulnerability"))
    geographic = _flag(_("differences between areas"))
    nationality = _flag(_("nationality and legal status"))
    mhpss = _flag(_("mental health and psychosocial support"))
    sbc = _flag(_("social and behaviour change"))
    climate = _flag(_("climate and the environment"))
    systems = _flag(_("strengthening systems"))
    foresight = _flag(_("looking ahead (future scenarios)"))
    information_access = _flag(_("access to information, digital rights and accountability"))
    youth_participation = _flag(_("youth participation"))
    palestinian_programme = _flag(_("Palestinian programme in Lebanon"))
    limitations = models.TextField(_("limitations"), blank=True)
    evidence_origin = models.TextField(_("evidence origin"), blank=True)
    # P: collected by the document itself ("Primary ..."), O: any other origin, "": not given
    origin_kind = models.CharField(_("evidence origin (kind)"), max_length=1, blank=True, db_index=True)
    confidence = models.CharField(
        _("confidence level"), max_length=1, blank=True, choices=Confidence.choices, db_index=True
    )
    confidence_text = models.CharField(_("confidence level (as given)"), max_length=200, blank=True)
    entity = models.TextField(_("publishing entity / author"), blank=True)
    study_type = models.TextField(_("document or study type"), blank=True)  # as the finding gives it
    # the document's own type, copied at the load: the type filter reads it (fewer, cleaner values)
    document_type = models.CharField(_("document type"), max_length=200, blank=True, db_index=True)
    year = models.PositiveSmallIntegerField(_("year"), null=True, blank=True, db_index=True)
    year_text = models.CharField(_("year (as given)"), max_length=40, blank=True)
    period = models.TextField(_("study or data collection period"), blank=True)
    population = models.TextField(_("population"), blank=True)
    methods = models.TextField(_("methods and sample"), blank=True)
    link = models.TextField(_("link"), blank=True)
    quality = models.TextField(_("data quality"), blank=True)
    needs_review = models.BooleanField(_("marked for review"), default=False, db_index=True)
    checks = models.JSONField(_("values to check"), default=dict, blank=True)
    # every searchable text, for the page's search box
    search_vector = SearchVectorField(null=True, editable=False)
    # only the finding's own words (not its document's methods, population or titles), for its assistant
    own_vector = SearchVectorField(null=True, editable=False)

    class Meta:
        db_table = "km_finding"
        ordering = ("position",)
        verbose_name = _("finding")
        verbose_name_plural = _("findings")
        indexes = [
            GinIndex(fields=["search_vector"], name="km_finding_search"),
            GinIndex(fields=["own_vector"], name="km_finding_own"),
            models.Index(fields=["pillar_code", "question_key"], name="km_finding_question"),
        ]

    def __str__(self) -> str:
        return self.finding_id


# ------------------------------------------------------------------------------------- the version shown
LIVE_MEMO_SECONDS = 10.0
_live_memo: tuple[float, KMVersion | None] = (0.0, None)


def live() -> KMVersion | None:
    """The version shown now, or None. Kept 10 seconds in this process (then one indexed query): a version
    made live by the load, which runs in another process, shows within 10 seconds on every worker. No shared
    cache key is involved, so nothing has to be cleared when a version changes."""
    global _live_memo
    now = time.monotonic()
    expires, version = _live_memo
    if now < expires:
        return version
    version = KMVersion.objects.defer("content").filter(status=KMVersion.Status.LIVE).first()
    _live_memo = (now + LIVE_MEMO_SECONDS, version)
    return version


def forget_live() -> None:
    """Read the version shown afresh at the next :func:`live` (this process only)."""
    global _live_memo
    _live_memo = (0.0, None)
