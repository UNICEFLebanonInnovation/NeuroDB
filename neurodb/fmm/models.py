"""Monitoring insights' own tables, all rebuilt by the refresh (``fmm_refresh``) except the reviews:

- what the eTools field monitoring records hold (:class:`KeyProbe`) and which key each logical field
  is read from (:class:`FieldMapping`), shown in the admin as "Fields found";
- the visits (:class:`Visit`, one per eTools monitoring activity), the finding rows of each
  (:class:`VisitEntity`), its checklist answers (:class:`QuestionAnswer`) and the action points raised
  from it (:class:`VisitActionPoint`);
- the reviews sections put on visits (:class:`VisitReview`, kept across rebuilds) and the refreshes
  someone asked for that have not run yet (:class:`RefreshRequest`).

No table here holds a narrative, an answer, a summary or a comment from eTools: those texts are read
from their source when a page needs them. A probe keeps at most three short redacted examples per
key, and "(withheld)" for a key that holds a person. ``Visit.team`` holds display names only, never an
e-mail address. Every link to a table of another app carries no database constraint, so a Datamart
sync that replaces those rows never waits on these.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.urls import reverse


def _fk(to, related_name="+", **kw):
    return models.ForeignKey(
        to,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name=related_name,
        **kw,
    )


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


class Visit(models.Model):
    """One eTools field monitoring activity, page-ready: its finding rows grouped by
    ``datamart.fm.visit_key`` and linked to partners, programme documents, places, sections, offices,
    answers and action points (``fmm.build``). Rebuilt by the refresh; its pk stays the same while its
    key does. The quality score, HACT Q1, PSEA flag and urgency are filled by the scoring step."""

    key = models.SlugField(max_length=40, unique=True)
    activity_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    reference = models.CharField(max_length=100, blank=True, db_index=True)  # monitoring_activity
    reference_number = models.CharField(max_length=100, blank=True)
    label = models.CharField(max_length=110)  # "Visit 1722" | the reference | "Finding 88"
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)  # THE visit date
    last_modified = models.DateTimeField(null=True, blank=True)  # latest change in eTools: "status as of"
    status = models.CharField(max_length=24, blank=True)  # normalised, the most advanced of its rows
    status_raw = models.CharField(max_length=40, blank=True)
    status_group = models.CharField(max_length=12, db_index=True, default="unknown")
    rating = models.CharField(max_length=16, db_index=True, default="not_monitored")  # worst rated entity
    rating_counts = models.JSONField(default=dict)  # {"on_track": 2, "not_monitored": 1, ...}
    hact_q1 = models.CharField(max_length=16, blank=True, db_index=True)  # set at scoring; "" = not known
    psea_flag = models.BooleanField(null=True)  # set at scoring; None = no PSEA question
    is_programmatic = models.BooleanField(default=False)
    is_remote = models.BooleanField(default=False)
    entities = models.PositiveSmallIntegerField(default=0)
    entities_rated = models.PositiveSmallIntegerField(default=0)
    entity_kinds = ArrayField(models.CharField(max_length=12), default=list)
    partner = _fk("etools.PartnerOrganization", related_name="fmm_visits")  # the most frequent one
    partner_ids = ArrayField(models.IntegerField(), default=list)
    pd = _fk("etools.PCA", related_name="fmm_visits")  # the first programme document resolved
    pd_ids = ArrayField(models.BigIntegerField(), default=list)
    pd_numbers = ArrayField(models.CharField(max_length=64), default=list)
    cp_outputs = ArrayField(models.CharField(max_length=300), default=list)
    programme_activities = ArrayField(models.CharField(max_length=300), default=list)
    location = _fk("locations.Location")
    site = _fk("datamart.MonitoringSite")
    place_name = models.CharField(max_length=254, blank=True)  # site, else location, else the raw name
    place_pcode = models.CharField(max_length=32, blank=True)
    governorate = _fk("locations.Location")
    governorate_name = models.CharField(max_length=254, blank=True)
    governorate_key = models.CharField(max_length=40, blank=True, db_index=True)  # overview.governorate_key
    district = _fk("locations.Location")
    district_name = models.CharField(max_length=254, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    located_by = models.CharField(max_length=10, blank=True)  # site | location | ancestor | ""
    # the admin level of the location whose point is used; None for a site
    located_level = models.PositiveSmallIntegerField(null=True, blank=True)
    # a site, or the location's own point at the gazetteer's lowest admin level: only these match a
    # planned location by coordinates
    point_precise = models.BooleanField(default=False)
    approximate = models.BooleanField(default=False)
    approximate_from = models.CharField(max_length=254, blank=True)  # the ancestor's name
    section_names = ArrayField(models.CharField(max_length=200), default=list)  # eTools spelling
    section_ids = ArrayField(models.IntegerField(), default=list)  # users.Section, confirmed matches only
    sections_from = models.CharField(max_length=14, blank=True)  # activity | pd | action_point | partner | ""
    offices = ArrayField(models.CharField(max_length=200), default=list)
    offices_from = models.CharField(max_length=14, blank=True)  # activity | pd | action_point | ""
    team = ArrayField(models.CharField(max_length=120), default=list)  # display names only, never emails
    team_unnamed = models.PositiveSmallIntegerField(default=0)  # members known by e-mail address only
    questions_asked = models.PositiveSmallIntegerField(null=True, blank=True)  # NULL = no question data
    questions_answered = models.PositiveSmallIntegerField(null=True, blank=True)
    quality_score = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True, db_index=True)
    quality_points = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    quality_max = models.PositiveSmallIntegerField(default=0)  # points of the rules evaluated
    evaluated_rules = ArrayField(models.CharField(max_length=4), default=list)  # ["R1", "R4"]
    not_scored_reason = models.CharField(max_length=80, blank=True)
    score_band = models.CharField(max_length=8, blank=True)  # high | medium | low | ""
    flags = ArrayField(models.CharField(max_length=4), default=list)  # the rules failed
    flag_count = models.PositiveSmallIntegerField(default=0, db_index=True)
    urgency = models.PositiveSmallIntegerField(default=0, db_index=True)
    urgency_band = models.CharField(max_length=6, blank=True)  # red | amber | ""
    urgency_parts = models.JSONField(default=dict)  # {"rating": 40, "quality": 8, ...}
    action_points = models.PositiveSmallIntegerField(default=0)
    action_points_open = models.PositiveSmallIntegerField(default=0)
    action_points_overdue = models.PositiveSmallIntegerField(default=0)
    action_points_high_open = models.PositiveSmallIntegerField(default=0)
    # folded: label, references, partner names, PD numbers, place, governorate (never a person)
    search = models.TextField(blank=True)
    issues = models.JSONField(default=dict)  # data problems, counts and codes only (no free text)
    rules_version = models.PositiveIntegerField(default=0)
    refreshed_at = models.DateTimeField()

    class Meta:
        ordering = ("-urgency", "-end_date")
        indexes = [
            models.Index(fields=["end_date", "status_group"]),
            GinIndex(fields=["section_names"]),
            GinIndex(fields=["partner_ids"]),
            GinIndex(fields=["pd_ids"]),
            GinIndex(fields=["flags"]),
            GinIndex(fields=["offices"]),
        ]
        verbose_name = "visit"
        verbose_name_plural = "visits"

    def __str__(self):
        return self.label or self.key

    def get_absolute_url(self) -> str:
        return reverse("fmm:visit", args=[self.key])

    def findings(self) -> models.QuerySet:
        """The eTools field monitoring finding rows of this visit."""
        from neurodb.datamart.models import MonitoringFinding

        ids = self.entity_rows.exclude(finding_id=None).values_list("finding_id", flat=True)
        return MonitoringFinding.objects.filter(pk__in=list(ids))


class VisitEntity(models.Model):
    """One finding row of a visit (a monitored programme document, CP output or partner), with its
    links and the measures of its narrative; never the narrative itself."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="entity_rows")
    finding = _fk("datamart.MonitoringFinding")
    datamart_id = models.BigIntegerField(db_index=True)
    entity = models.CharField(max_length=255, blank=True)  # as eTools wrote it (a PD, partner or output)
    entity_type_raw = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=12)  # datamart.fm.KINDS
    pd = _fk("etools.PCA")
    pd_match = models.CharField(max_length=8, blank=True)
    partner = _fk("etools.PartnerOrganization")
    cp_output = models.CharField(max_length=300, blank=True)
    rating = models.CharField(max_length=16)  # normalised
    rating_raw = models.CharField(max_length=50, blank=True)
    hact_q1 = models.CharField(max_length=16, blank=True)  # own, partner-level or visit-level; at scoring
    hact_q1_from = models.CharField(max_length=8, blank=True)  # entity | partner | visit | ""
    narrative_words = models.PositiveIntegerField(default=0)
    narrative_hash = models.CharField(max_length=40, blank=True)  # sha1 of the folded text, long ones only
    narrative_placeholder = models.BooleanField(default=False)

    class Meta:
        ordering = ("visit", "datamart_id")
        verbose_name = "monitored entity"
        verbose_name_plural = "monitored entities"

    def __str__(self):
        return self.entity or f"Finding {self.datamart_id}"


class QuestionAnswer(models.Model):
    """One checklist answer record (``fm_questions``), parsed: whether it was answered and its code
    (a rating, yes or no), never its text, which stays in the Datamart store."""

    document_id = models.BigIntegerField(db_index=True)  # DatamartDocument.pk (that store is replaced)
    visit = models.ForeignKey(Visit, null=True, blank=True, on_delete=models.CASCADE, related_name="answers")
    visit_key = models.CharField(max_length=40, blank=True, db_index=True)  # "" = joins no visit
    entity = models.ForeignKey(
        VisitEntity, null=True, blank=True, on_delete=models.SET_NULL, related_name="answers"
    )
    # the partner a partner-level answer applies to (applies_to "partner"): every row of that partner
    partner = _fk("etools.PartnerOrganization")
    question_key = models.CharField(max_length=64)  # the question id, else sha1 of the folded text
    question_text = models.CharField(max_length=500, blank=True)  # the checklist question, a template
    question_order = models.IntegerField(null=True, blank=True)
    is_hact = models.BooleanField(null=True)
    role = models.CharField(max_length=6, blank=True, db_index=True)  # q1 | q2 | q3 | psea | "" (scoring)
    applies_to = models.CharField(max_length=8, blank=True)  # entity | partner | visit
    answer_code = models.CharField(max_length=12, blank=True)  # on_track | ... | yes | no | "": never text
    answered = models.BooleanField(default=False)
    placeholder = models.BooleanField(default=False)
    answer_words = models.PositiveIntegerField(default=0)
    summary_words = models.PositiveIntegerField(default=0)
    rating = models.CharField(max_length=16, blank=True)  # when the answer is a rating word
    method = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ("visit", "question_order", "question_key")
        verbose_name = "checklist answer"
        verbose_name_plural = "checklist answers"

    def __str__(self):
        return f"{self.visit_key or '—'} {self.question_key}"


class VisitActionPoint(models.Model):
    """An eTools action point raised from a visit, and how it was matched to it."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="action_point_links")
    action_point = models.ForeignKey(
        "datamart.ActionPoint", on_delete=models.CASCADE, db_constraint=False, related_name="+"
    )
    matched_by = models.CharField(max_length=16)  # related_id | reference | reference_number

    class Meta:
        constraints = [models.UniqueConstraint(fields=["visit", "action_point"], name="fmm_visit_ap")]
        verbose_name = "action point of a visit"
        verbose_name_plural = "action points of visits"

    def __str__(self):
        return f"{self.visit_id} {self.action_point_id}"


class VisitReview(models.Model):
    """A section's mark on a visit (reviewed, needs follow-up, data issue). Keyed by the visit's key, so
    it survives the rebuilds of the visits."""

    class Status(models.TextChoices):
        REVIEWED = "reviewed", "Reviewed"
        FOLLOW_UP = "follow_up", "Needs follow-up"
        DATA_ISSUE = "data_issue", "Data issue"

    visit_key = models.SlugField(max_length=40, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices)
    note = models.CharField(max_length=500, blank=True)  # typed by staff; never sent to the AI
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "visit review"
        verbose_name_plural = "visit reviews"

    def __str__(self):
        return f"{self.visit_key} {self.status}"


class RefreshRequest(models.Model):
    """The one row (pk=1) of the refreshes someone asked for that have not run yet: a saved rule or
    score setting asks for the scores to be recomputed, a pinned key for a full refresh. Every refresh
    looks at it before and after it releases its lock, so a request made while another refresh runs is
    never lost; the pass that serves a request clears it."""

    scores_requested_at = models.DateTimeField(null=True, blank=True)
    full_requested_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.CharField(max_length=150, blank=True)  # "admin:<pk>", the run's triggered_by

    class Meta:
        verbose_name = "refresh request"
        verbose_name_plural = "refresh requests"

    def __str__(self):
        return "Refresh request"

    @classmethod
    def load(cls) -> RefreshRequest:
        return cls.objects.get_or_create(pk=1)[0]
