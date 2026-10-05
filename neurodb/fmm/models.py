"""Monitoring insights' own tables, the data ones rebuilt by the refresh (``fmm_refresh``):

- what the eTools field monitoring records hold (:class:`KeyProbe`) and which key each logical field
  is read from (:class:`FieldMapping`), shown in the admin as "Fields found";
- the visits (:class:`Visit`, one per eTools monitoring activity), the finding rows of each
  (:class:`VisitEntity`), its checklist answers (:class:`QuestionAnswer`) and the action points raised
  from it (:class:`VisitActionPoint`);
- the reviews sections put on visits (:class:`VisitReview`, kept across rebuilds) and the refreshes
  someone asked for that have not run yet (:class:`RefreshRequest`);
- the quality rules, score settings and their versions (:class:`RuleSetting`, :class:`ScoreSetting`,
  :class:`RuleSetVersion`) and each visit's rule results (:class:`VisitRuleResult`);
- the AI's prompt versions (:class:`PromptProfile`, :class:`PromptVersion`, never changed once
  published), what each model accepted (:class:`ModelCapability`), its pause (:class:`AIState`) and its
  briefs (:class:`Insight`, which keeps the payload sent, redacted, for a limited time).

No data table here holds a narrative, an answer, a summary or a comment from eTools: those texts are
read from their source when a page needs them. Only an AI brief keeps texts derived from them: the
payload as sent (cleaned, and blanked after ``FMM_PAYLOAD_RETENTION_DAYS``) and its checked sentences.
A probe keeps at most three short redacted examples per key, and "(withheld)" for a key that holds a
person. ``Visit.team`` holds display names only, never an e-mail address. Every link to a table of
another app carries no database constraint, so a Datamart sync that replaces those rows never waits on
these.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
)
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


# ------------------------------------------------------------------------------------------ quality rules
class RuleSetting(models.Model):
    """One quality rule (R1-R6) as administrators set it: on or off, its points, its threshold and its
    parameters (``fmm.rules``). Every save is recorded as a new rules version (``RuleSetVersion``) and
    the scores are recomputed in the background."""

    code = models.CharField(max_length=4, primary_key=True)  # R1..R6
    label = models.CharField(max_length=60)  # "Completeness", ...
    enabled = models.BooleanField(default=True)
    points = models.PositiveSmallIntegerField(validators=[MaxValueValidator(50)])
    threshold = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    params = models.JSONField(default=dict, blank=True)  # checked per rule in clean()
    description = models.TextField(blank=True)  # plain words: what the rule checks
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)
        verbose_name = "quality rule"
        verbose_name_plural = "quality rules"

    def __str__(self):
        return f"{self.code} {self.label}"

    def clean(self):
        from . import rules

        self.params = rules.validate_rule(self.code, self.points, self.threshold, self.params)


def default_urgency_weights() -> dict:
    return {
        "off_track": 40,
        "constrained": 20,
        "quality_gap": 25,
        "unscored_reported": 10,
        "per_flag": 5,
        "flags_max": 15,
        "no_follow_up": 20,
        "ap_overdue": 12,
        "ap_high_overdue": 8,
        "ap_high_open": 5,
        "follow_up_max": 20,
        "report_late": 15,
    }


def default_role_flag_answers() -> dict:
    # answer codes (QuestionAnswer.answer_code) that flag a visit for a role
    return {"psea": ["yes", "constrained", "off_track"]}


def default_question_patterns() -> dict:
    return {
        "q1": ["implemented as planned", "activities been implemented"],
        "q2": ["^q2", "^q 2", "activities monitored"],
        "q3": ["^q3", "^q 3"],
        "psea": ["psea", "sexual exploitation", "sexual abuse"],
    }


class ScoreSetting(models.Model):
    """The one row (pk=1) of how scores, bands, urgency and the question roles are worked out
    (``fmm.score``). Versioned and rescored like the rules."""

    min_evaluated_points = models.PositiveSmallIntegerField(default=30)
    band_high = models.PositiveSmallIntegerField(default=80)  # High >= 80
    band_medium = models.PositiveSmallIntegerField(default=50)  # Medium 50-79, Low < 50
    high_flag_count = models.PositiveSmallIntegerField(default=3)
    urgency_red = models.PositiveSmallIntegerField(default=70)
    urgency_amber = models.PositiveSmallIntegerField(default=40)
    urgency_weights = models.JSONField(default=default_urgency_weights)  # a new dict for every row
    follow_up_days = models.PositiveSmallIntegerField(default=14)
    report_late_days = models.PositiveSmallIntegerField(default=30)
    question_patterns = models.JSONField(default=default_question_patterns)
    role_flag_answers = models.JSONField(default=default_role_flag_answers)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "score settings"
        verbose_name_plural = "score settings"

    def __str__(self):
        return "Score settings"

    @classmethod
    def load(cls) -> ScoreSetting:
        return cls.objects.get_or_create(pk=1)[0]

    def clean(self):
        from . import score

        score.validate_settings(self)


class RuleSetVersion(models.Model):
    """One saved state of the quality rules, the score settings and the keys administrators pinned:
    who saved it, when and why. A rollback writes a new version (``restored_from``); none is edited."""

    number = models.PositiveIntegerField(unique=True)
    # {"rules": [...], "score": {...}, "mappings": {"fm_questions.answer": "value", ...}}
    snapshot = models.JSONField()
    note = models.CharField(max_length=200)  # what changed and why
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by_name = models.CharField(max_length=150)  # kept when the user is deleted
    created_at = models.DateTimeField(auto_now_add=True)
    restored_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-number",)
        verbose_name = "rules version"
        verbose_name_plural = "rule versions"

    def __str__(self):
        return f"Rules v{self.number}"


class VisitRuleResult(models.Model):
    """What one quality rule found on one visit: passed, failed (a flag), not available, not
    applicable or switched off, with the points earned and a code-written sentence (never text from
    the data)."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="rule_results")
    rule = models.CharField(max_length=4)  # R1..R6
    status = models.CharField(max_length=4, db_index=True)  # pass | fail | na | nap | off
    points = models.DecimalField(max_digits=4, decimal_places=1, default=0)  # earned, never above max
    max_points = models.PositiveSmallIntegerField(default=0)
    detail_key = models.CharField(max_length=40, blank=True)  # "missing:narrative,q2", "below_threshold"
    detail = models.CharField(max_length=300, blank=True)  # code-written; never text from the data
    measure = models.FloatField(null=True, blank=True)  # 46.2 (% answered), 9 (words), 2 (cues)

    class Meta:
        ordering = ("visit", "rule")
        constraints = [models.UniqueConstraint(fields=["visit", "rule"], name="fmm_visit_rule")]
        indexes = [models.Index(fields=["rule", "status"])]
        verbose_name = "rule result"
        verbose_name_plural = "rule results"

    def __str__(self):
        return f"{self.visit_id} {self.rule} {self.status}"


# ------------------------------------------------------------------------------------------ AI
class PromptProfile(models.Model):
    """The prompt of the AI brief and the chat for one audience (Release 1: one, "lebanon"), and which of
    its versions is published. Its versions are never edited once published (``PromptVersion``)."""

    key = models.SlugField(max_length=20, unique=True)  # "lebanon" (later: one per section)
    label = models.CharField(max_length=80)  # "Lebanon"
    published = models.ForeignKey(
        "PromptVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "prompt profile"
        verbose_name_plural = "prompt profiles"

    def __str__(self):
        return self.label


NARR_HELP = (
    "narr: the most texts (narratives, question answers and search snippets) the AI may read per brief, "
    "and per chat answer across all of its look-ups. Each is cleaned of names, e-mail addresses, phone "
    "numbers and links first. 0 sends no text at all."
)
COMP_HELP = (
    "comp: how many visits the AI sees in full, as a card (dates, partner, programme document, place, "
    "sections, rating with its date, HACT Q1, quality, flags, urgency, action point counts; never a "
    "narrative). In the chat, the most visit cards one look-up returns. Every other visit reaches the AI "
    "as counts only; the previous period is sent as key figures only."
)


class PromptVersion(models.Model):
    """One version of a profile's prompt and parameters. A draft can be edited; once published (and later
    retired) it never changes, so every brief and chat answer says exactly which version it ran with.
    A rollback publishes a new copy (``restored_from``); an old row is never published again."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        RETIRED = "retired", "Retired"

    profile = models.ForeignKey(PromptProfile, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # the AI brief
    insights_enabled = models.BooleanField(default=True)
    instructions = models.TextField(  # the editable part only; the fixed safety text follows it
        validators=[MinLengthValidator(200), MaxLengthValidator(8000)]
    )
    max_output_tokens = models.PositiveIntegerField(  # INCLUDES the reasoning tokens
        default=4000, validators=[MinValueValidator(1000), MaxValueValidator(32000)]
    )
    narratives_sampled = models.PositiveSmallIntegerField(  # "narr"
        default=20, validators=[MaxValueValidator(50)], help_text=NARR_HELP
    )
    comparison_visits = models.PositiveSmallIntegerField(  # "comp"
        default=15, validators=[MaxValueValidator(40)], help_text=COMP_HELP
    )
    insights_per_user_per_day = models.PositiveSmallIntegerField(
        default=5, validators=[MaxValueValidator(50)]
    )
    # the chat
    chat_enabled = models.BooleanField(default=True)
    chat_instructions = models.TextField(validators=[MinLengthValidator(100), MaxLengthValidator(6000)])
    chat_max_output_tokens = models.PositiveIntegerField(  # per model call
        default=6000, validators=[MinValueValidator(1000), MaxValueValidator(32000)]
    )
    chat_per_user_per_day = models.PositiveSmallIntegerField(default=20, validators=[MaxValueValidator(200)])
    chat_max_rounds = models.PositiveSmallIntegerField(
        default=4, validators=[MinValueValidator(1), MaxValueValidator(8)]
    )
    chat_time_limit = models.PositiveSmallIntegerField(  # seconds
        default=120, validators=[MinValueValidator(30), MaxValueValidator(180)]
    )
    # both
    model = models.CharField(max_length=64, blank=True)  # blank: settings.FMM_MODEL
    effort = models.CharField(max_length=8, default="low")  # one of settings.AI_ASSISTANT_EFFORTS
    temperature = models.DecimalField(  # None: not set (not sent)
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("2"))],
    )
    top_p = models.DecimalField(  # None: not set (the API's default, 1.00)
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
    )
    # history
    note = models.CharField(max_length=300)  # required: what changed and why
    based_on = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    restored_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    content_hash = models.CharField(max_length=64)  # sha256 of the content, the safety text and the schema
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by_name = models.CharField(max_length=150)  # kept when the user is deleted
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    published_by_name = models.CharField(max_length=150, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    CONTENT_FIELDS = (
        "insights_enabled",
        "instructions",
        "max_output_tokens",
        "narratives_sampled",
        "comparison_visits",
        "insights_per_user_per_day",
        "chat_enabled",
        "chat_instructions",
        "chat_max_output_tokens",
        "chat_per_user_per_day",
        "chat_max_rounds",
        "chat_time_limit",
        "model",
        "effort",
        "temperature",
        "top_p",
    )

    class Meta:
        ordering = ("profile", "-number")
        constraints = [
            models.UniqueConstraint(fields=["profile", "number"], name="fmm_prompt_number"),
            models.UniqueConstraint(
                fields=["profile"], condition=models.Q(status="published"), name="fmm_one_published_version"
            ),
        ]
        verbose_name = "prompt version"
        verbose_name_plural = "prompt versions"

    def __str__(self):
        return f"Prompt v{self.number}"

    def save(self, *args, **kwargs):
        """Refuses (ValueError) any change to the content of a version that is not a draft, and a
        published or retired version turned back into a draft."""
        stored = None
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values("status", *self.CONTENT_FIELDS).first()
        if stored is not None and stored["status"] != self.Status.DRAFT:
            changed = [
                n
                for n in self.CONTENT_FIELDS
                if self._plain(n, stored[n]) != self._plain(n, getattr(self, n))
            ]
            if changed:
                raise ValueError(
                    f"Prompt v{self.number} is {stored['status']} and cannot change ({', '.join(changed)}); "
                    "make a new draft from it instead."
                )
            if self.status == self.Status.DRAFT:
                raise ValueError(
                    f"Prompt v{self.number} is {stored['status']} and cannot become a draft again."
                )
        else:
            self.content_hash = self.compute_hash()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "content_hash"}
        super().save(*args, **kwargs)

    SAMPLING_FIELDS = ("temperature", "top_p")

    @classmethod
    def _plain(cls, name: str, value):
        """A content value in one written form (a sampling value 0.3, "0.3" and Decimal 0.30 are all
        "0.30")."""
        if name in cls.SAMPLING_FIELDS and value not in (None, ""):
            return f"{Decimal(str(value)):.2f}"
        return value

    def content(self) -> dict:
        """The content fields as plain values, for comparisons and the hash."""
        return {name: self._plain(name, getattr(self, name)) for name in self.CONTENT_FIELDS}

    def compute_hash(self) -> str:
        """sha256 of the content, the version of the fixed safety text and the version of the brief's
        schema: a change to any of them gives another hash."""
        from .ai.insights import SCHEMA_VERSION
        from .ai.prompts import SAFETY_VERSION

        blob = json.dumps(
            {"content": self.content(), "safety": SAFETY_VERSION, "schema": SCHEMA_VERSION},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()


class ModelCapability(models.Model):
    """Whether a model, at one reasoning effort, accepted a sampling parameter (temperature or top_p) the
    last time it was sent: kept in the database so that every process knows it. Deleting a row means
    "check again on the next call"."""

    model = models.CharField(max_length=64)
    effort = models.CharField(max_length=8)
    parameter = models.CharField(max_length=16)  # temperature | top_p
    accepted = models.BooleanField(null=True)
    checked_at = models.DateTimeField()
    detail = models.CharField(max_length=300, blank=True)  # the API's message, cut

    class Meta:
        ordering = ("model", "effort", "parameter")
        constraints = [
            models.UniqueConstraint(fields=["model", "effort", "parameter"], name="fmm_model_capability")
        ]
        verbose_name = "sampling check"
        verbose_name_plural = "sampling checks"

    def __str__(self):
        return f"{self.model} · {self.effort} · {self.parameter}"


class AIState(models.Model):
    """The one row (pk=1) of Monitoring insights' AI pause: after the OpenAI credit ran out, its AI is
    not used until ``paused_until``."""

    paused_until = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI pause"
        verbose_name_plural = "AI pause"

    def __str__(self):
        return "AI pause"

    @classmethod
    def load(cls) -> AIState:
        return cls.objects.get_or_create(pk=1)[0]


class Insight(models.Model):
    """One AI brief of a filter (or why none was written): the sentences kept, the priority actions, what
    was sent and what was used (version, model, tokens, sampling). A row in ``running`` is a brief being
    written in a background process; only one may run per filter and version."""

    class Trigger(models.TextChoices):
        NIGHTLY = "nightly", "Nightly"
        USER = "user", "Regenerate"
        TEST = "test", "Test run"

    class Status(models.TextChoices):
        RUNNING = "running", "Writing"
        OK = "ok", "Written"
        PARTIAL = "partial", "Partly written"
        FALLBACK = "fallback", "Written by NeuroDB"
        FAILED = "failed", "Failed"
        LIMITED = "limited", "Limit reached"
        SKIPPED = "skipped", "Not written"

    scope_hash = models.CharField(max_length=64, db_index=True)
    scope = models.JSONField()  # Scope.canonical()
    scope_label = models.CharField(max_length=300)
    trigger = models.CharField(max_length=8, choices=Trigger.choices)
    version = models.ForeignKey(PromptVersion, on_delete=models.PROTECT, related_name="insights")
    rules_version = models.PositiveIntegerField()
    input_hash = models.CharField(max_length=64, db_index=True)
    data_as_of = models.DateTimeField(null=True)  # the last fmm_refresh's finished_at
    called = models.BooleanField(default=False)  # an AI call was made (the quota counts these)
    status = models.CharField(max_length=8, choices=Status.choices)
    reason = models.CharField(max_length=200, blank=True)
    sections = models.JSONField(default=dict)  # {"coverage_quality": [{"text", "keys"}], ...}
    actions = models.JSONField(default=list)  # [{"priority", "section", "action", "owner_role", ...}]
    dropped = models.JSONField(default=dict)  # {"number": 3, "person": 1}
    cited_keys = ArrayField(models.CharField(max_length=40), default=list)  # the visit keys cited
    sent = models.JSONField(default=dict)  # narr and comp sent and allowed
    # the payload as sent (redacted); blanked after FMM_PAYLOAD_RETENTION_DAYS
    sent_payload = models.JSONField(null=True, blank=True)
    model = models.CharField(max_length=64, blank=True)
    effort = models.CharField(max_length=8, blank=True)
    max_output_tokens = models.PositiveIntegerField(default=0)
    # {"temperature": {"asked": 0.3, "state": "not_applied", "why": "..."}, "top_p": {...}}
    sampling = models.JSONField(default=dict)
    input_tokens = models.PositiveIntegerField(default=0)
    cached_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["scope_hash", "version"],
                condition=models.Q(status="running"),
                name="fmm_one_running_insight",
            )
        ]
        verbose_name = "AI brief"
        verbose_name_plural = "AI briefs"

    def __str__(self):
        return f"{self.scope_label} ({self.get_status_display()})"
