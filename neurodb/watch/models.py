"""What NeuroDB Watch remembers from one day to the next.

The other tables it reads are rebuilt (review findings, hub links, forecasts), so the watch keeps its
own memory keyed on lasting identifiers: PD numbers, review finding keys, grant and FR numbers, the
hub's (kind, key). Everything here is written by the watch only:

- ``WatchItem``: one thing it follows (a report due, a finding still open), with its evidence and a
  dated story of what happened to it;
- ``WatchReceipt``: what one person was told about one item, and how they reacted;
- ``WatchNote``: the morning note of one audience (a section, or the whole country);
- ``WatchDelivery``: the morning email of one person on one day, so it goes out once;
- ``WatchRequest``: "new data arrived", waiting for the next quick pass;
- ``WatchState``: one row of run state (the What's new watermark, the AI pause);
- ``DetectorSetting``: whether a check is in trial, on or off;
- ``SectionMatch``: which NeuroDB section an eTools section name means.
"""

from __future__ import annotations

import datetime
import hashlib
import json

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

KEY_MAX = 320  # WatchItem.key; a longer key is cut and keeps a hash of its whole text (fit_key)
STORY_MAX = 30  # dated lines kept in an item's story, the latest ones
RELATED_MAX = 8  # connected things kept on an item
RECORDS_MAX = 10  # evidence records kept on an item


def fit_key(key: str, limit: int = KEY_MAX) -> str:
    """``key`` within ``limit`` characters. A longer key keeps its start and a hash of its whole
    text, so the same long key always gives the same short one and two long keys stay apart (the
    daily review cuts its keys the same way)."""
    key = str(key)
    if len(key) <= limit:
        return key
    digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{key[: limit - 17]}#{digest}"


def hash_evidence(evidence: dict) -> str:
    """A short fingerprint of an item's evidence: it changes when the records or numbers change."""
    text = json.dumps(evidence or {}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


class WatchItem(models.Model):
    """One thing the watch follows: a deadline coming up, a concern still open or a system problem.

    Written by code only (never by the AI): the key, title, dates, severity, routing and evidence.
    The key is built from lasting identifiers, e.g. ``due:report:<PD number>:<progress report>``,
    ``review:<review finding key>`` or ``system:<warning key>``. An item closes at once on positive
    evidence (a report submitted); otherwise it is "gone" after two fresh daily runs without it,
    never "done".
    """

    class Kind(models.TextChoices):
        DEADLINE = "deadline", _("Deadline")
        CONCERN = "concern", _("Concern")
        SYSTEM = "system", _("System")

    class Severity(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        WARNING = "warning", _("Warning")
        INFO = "info", _("To note")

    class Confidence(models.TextChoices):
        SURE = "sure", _("Sure")
        LIKELY = "likely", _("Likely")
        CHECK = "check", _("Please check")

    class Scope(models.TextChoices):
        SECTION = "section", _("Its sections")
        COUNTRY = "country", _("Whole country")
        ADMINS = "admins", _("Administrators")

    class State(models.TextChoices):
        OPEN = "open", _("Open")
        CLOSED = "closed", _("Closed")
        GONE = "gone", _("No longer seen")
        WRONG = "wrong", _("Marked wrong")

    class CloseKind(models.TextChoices):
        """How a closed item ended: done, its date passed without it being done, or no longer
        followed here for another reason (its date moved, another point follows it...)."""

        RESOLVED = "resolved", _("Resolved")
        MISSED = "missed", _("Date passed, not done")
        CHANGED = "changed", _("No longer followed here")

    # Identity and kind
    key = models.CharField(max_length=KEY_MAX, unique=True, help_text="built from lasting identifiers")
    detector = models.CharField("check", max_length=40, db_index=True, help_text="the check that found it")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    confidence = models.CharField(max_length=16, choices=Confidence.choices, default=Confidence.SURE)

    # Text, written by code only
    title = models.CharField(max_length=300, help_text="carries dates, never day counts")
    detail = models.TextField(blank=True)
    url = models.CharField(max_length=500, blank=True)

    # Dates and routing
    due_date = models.DateField(null=True, blank=True, db_index=True)
    etools_sections = ArrayField(
        models.CharField(max_length=300), default=list, blank=True, help_text="the eTools section names"
    )
    section_ids = ArrayField(
        models.IntegerField(),
        default=list,
        blank=True,
        help_text="NeuroDB sections, from the confirmed eTools section names",
    )
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.SECTION)

    # Connections
    entity_kind = models.CharField(max_length=24, blank=True, help_text="the knowledge hub kind it is about")
    entity_key = models.CharField(max_length=120, blank=True, help_text="the knowledge hub key it is about")
    related = models.JSONField(
        default=list, blank=True, help_text=f"at most {RELATED_MAX} of {{kind, key, name, url}}"
    )
    situation_key = models.CharField(
        max_length=160, blank=True, db_index=True, help_text="the situation it belongs to, e.g. partner:1234"
    )
    review_key = models.CharField(
        max_length=300, blank=True, help_text="the daily review finding it hands over to"
    )

    # Evidence (never empty)
    evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            '{"source", "source_job", "synced_at", "records": [{label, date, value, url}], "numbers": {}}'
        ),
    )
    evidence_hash = models.CharField(max_length=40, blank=True)

    # State and lifecycle
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN, db_index=True)
    close_reason = models.CharField(max_length=300, blank=True)
    close_kind = models.CharField(
        max_length=16, choices=CloseKind.choices, blank=True, help_text="how it closed (closed items only)"
    )
    first_seen_on = models.DateField()
    last_seen_on = models.DateField()
    changed_on = models.DateField(help_text="when its severity, due date or state last changed")
    closed_on = models.DateField(null=True, blank=True)
    missed_runs = models.PositiveSmallIntegerField(
        default=0, help_text="fresh daily runs in a row that did not find it"
    )
    source_mark = models.CharField(
        max_length=40,
        blank=True,
        help_text="how fresh its source was when last seen; compared as text (an ISO date or time)",
    )
    story = models.JSONField(default=list, blank=True, help_text=f"at most {STORY_MAX} of {{on, text}}")

    # Assignment: whether someone owns it and the assignment's status, never the owner or note text
    has_owner = models.BooleanField(default=False)
    assignment_status = models.CharField(max_length=16, blank=True)

    # The background look-up, written by AI and kept only when its numbers are grounded
    looked_up = models.JSONField(
        default=dict, blank=True, help_text="what NeuroDB looked up (AI): {text, numbers, tools, at}"
    )

    class Meta:
        ordering = ("due_date", "key")
        indexes = [GinIndex(fields=["section_ids"], name="watch_item_sections")]
        verbose_name = _("thing followed")
        verbose_name_plural = _("NeuroDB Watch: things followed")

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        self.key = fit_key(self.key)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        records = (self.evidence or {}).get("records") if isinstance(self.evidence, dict) else None
        if not records:
            raise ValidationError(
                {"evidence": _("An item needs its evidence: at least one record it was found in.")}
            )

    def add_story(self, text: str, on: datetime.date | None = None) -> None:
        """Add a dated line to the story (not saved), keeping the latest ``STORY_MAX`` lines."""
        on = on or timezone.localdate()
        self.story = [*(self.story or []), {"on": on.isoformat(), "text": str(text)[:300]}][-STORY_MAX:]


class WatchReceipt(models.Model):
    """What one person was told about one item, at which step, and how they reacted. One row per
    person and item, holding the last step told: a person is told again only when the step moves on
    (a milestone, worse, resolved...)."""

    class Level(models.TextChoices):
        NEEDS_YOU = "needs_you", _("Needs you")
        GOOD_TO_KNOW = "good_to_know", _("Good to know")

    class Reaction(models.TextChoices):
        NONE = "", _("No reaction")
        USEFUL = "useful", _("Useful")
        NOT_USEFUL = "not_useful", _("Not useful")
        DONE = "done", _("Done")
        NOT_MINE = "not_mine", _("Not mine")
        WRONG = "wrong", _("Something's wrong")

    # told_step values besides a milestone's number of days ("14", "7"...). An item that closed is told
    # as resolved, missed (its date passed and it was not done) or closed (no longer followed here)
    KNOWN, NEW, OVERDUE, WORSE = "known", "new", "overdue", "worse"
    STILL_OPEN, RESOLVED, MISSED, CLOSED = "still_open", "resolved", "missed", "closed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="watch_receipts"
    )
    item = models.ForeignKey(WatchItem, on_delete=models.CASCADE, related_name="receipts")
    first_told_on = models.DateField()
    last_told_on = models.DateField()
    told_step = models.CharField(
        max_length=16,
        help_text=(
            "known, new, a milestone (days left), overdue, worse, still_open, resolved, missed or closed"
        ),
    )
    told_severity = models.CharField(
        max_length=16, blank=True, help_text="the item's severity when the person was last told"
    )
    milestone_count = models.PositiveSmallIntegerField(default=0, help_text="milestones told, at most 3")
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.GOOD_TO_KNOW)
    seen_at = models.DateTimeField(null=True, blank=True)
    reaction = models.CharField(max_length=16, choices=Reaction.choices, blank=True, default=Reaction.NONE)
    reacted_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(
        max_length=300, blank=True, help_text="seen by administrators only; never sent to the AI or by email"
    )
    wrong_hash = models.CharField(
        max_length=40,
        blank=True,
        help_text="the item's evidence when marked wrong: a Section editor's answer hides it for their "
        "section until the evidence changes",
    )
    snoozed_until = models.DateField(null=True, blank=True)
    emailed_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ("-last_told_on", "id")
        constraints = [models.UniqueConstraint(fields=["user", "item"], name="watch_receipt_one_per_person")]
        indexes = [models.Index(fields=["user", "-last_told_on"], name="watch_receipt_user_told")]
        verbose_name = _("what a person was told")
        verbose_name_plural = _("NeuroDB Watch: what people were told")

    def __str__(self):
        return f"{self.user} · {self.item} ({self.told_step})"


class WatchNote(models.Model):
    """The morning note of one audience on one day: written by the AI from the facts, every sentence
    citing the items it rests on, or listed by a plain template when the AI is not used."""

    TEMPLATE = "template"  # written_by when the note was listed without the AI
    COUNTRY = "country"  # the audience_key of the whole-country note; a section's is "section:<id>"

    class Skipped(models.TextChoices):
        NONE = "", _("AI used")
        OFF = "off", _("AI switched off")
        BUDGET = "budget", _("Daily limit reached")
        PAUSED = "paused", _("AI paused")
        QUOTA = "quota", _("OpenAI credit ran out")
        ERROR = "error", _("AI did not answer")
        NO_CHANGE = "no_change", _("Nothing changed")

    date = models.DateField()
    audience_key = models.CharField(max_length=40, help_text="section:<id> or country")
    audience_name = models.CharField(max_length=200, blank=True)
    sentences = models.JSONField(default=list, blank=True, help_text="[{text, keys}]")
    text = models.TextField(blank=True)
    written_by = models.CharField(max_length=100, help_text="the AI model, or template")
    ai_skipped_reason = models.CharField(max_length=16, choices=Skipped.choices, blank=True, default="")
    input_hash = models.CharField(max_length=64, blank=True)
    item_keys = models.JSONField(default=list, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    cached_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date", "audience_name")
        constraints = [
            models.UniqueConstraint(fields=["date", "audience_key"], name="watch_note_one_per_audience")
        ]
        verbose_name = _("morning note")
        verbose_name_plural = _("Morning notes (For you)")

    def __str__(self):
        return f"{self.date} {self.audience_name or self.audience_key}"

    @staticmethod
    def section_audience(section_id: int) -> str:
        return f"section:{section_id}"


class WatchDelivery(models.Model):
    """The morning email of one person on one day: sent once, even when the run is repeated."""

    class Channel(models.TextChoices):
        EMAIL_DAILY = "email_daily", _("Morning email")
        WHATS_NEW = "whats_new", _("What's new email (the morning run did not send its email)")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="watch_deliveries"
    )
    date = models.DateField()
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.EMAIL_DAILY)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-date", "id")
        constraints = [
            models.UniqueConstraint(fields=["user", "date", "channel"], name="watch_delivery_once_a_day")
        ]
        verbose_name = _("morning email sent")

    def __str__(self):
        return f"{self.date} {self.user} {self.channel}"


class WatchRequest(models.Model):
    """New data arrived (a hub build, a failed job, an edited assignment): a quick pass follows."""

    requested_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=120)

    class Meta:
        ordering = ("pk",)

    def __str__(self):
        return f"{self.requested_at:%Y-%m-%d %H:%M} {self.reason}"


class WatchState(models.Model):
    """One row (pk 1) of state that survives restarts: what the watch has read up to, how many quick
    passes ran today, and whether its AI is paused."""

    last_change_id = models.BigIntegerField(
        default=0, help_text="the last What's new change read; moves only to the highest id read"
    )
    last_daily_on = models.DateField(null=True, blank=True)
    quick_passes_on = models.DateField(null=True, blank=True)
    quick_passes_count = models.PositiveSmallIntegerField(default=0)
    ai_paused_until = models.DateTimeField(null=True, blank=True)
    ai_pause_reason = models.CharField(max_length=200, blank=True)
    consecutive_ai_errors = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = _("NeuroDB Watch state")

    def __str__(self):
        return "NeuroDB Watch state"

    def save(self, *args, **kwargs):
        self.pk = 1  # one row only
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> WatchState:
        """The one row, created on first use."""
        return cls.objects.get_or_create(pk=1)[0]


class DetectorSetting(models.Model):
    """Whether one check is in trial (seen only by the whole-country view), on (told to staff) or off.
    New checks start in trial; the daily review import and the system checks start on. A check that
    people find unhelpful goes back to trial by itself (``demoted_at``)."""

    class Mode(models.TextChoices):
        TRIAL = "trial", _("Trial: whole-country view only")
        ON = "on", _("On: told to staff")
        OFF = "off", _("Off")

    detector = models.CharField("check", max_length=40, unique=True)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.TRIAL)
    on_since = models.DateField(
        null=True, blank=True, help_text="items found before this day were known already: not announced"
    )
    trial_since = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "the day it went into trial: items found before were known already to the whole-country view"
        ),
    )
    demoted_at = models.DateTimeField(null=True, blank=True, help_text="when it went back to trial by itself")
    demoted_reason = models.CharField(max_length=300, blank=True)
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("detector",)
        verbose_name = _("NeuroDB Watch check")
        verbose_name_plural = _("NeuroDB Watch: checks")

    def __str__(self):
        return f"{self.detector} ({self.mode})"

    def save(self, *args, **kwargs):
        if self.mode == self.Mode.ON and self.on_since is None:
            self.on_since = timezone.localdate()
        if self.mode == self.Mode.TRIAL and self.trial_since is None:
            self.trial_since = timezone.localdate()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "trial_since"}
        super().save(*args, **kwargs)

    @classmethod
    def ensure(cls, detector: str, mode: str = Mode.TRIAL) -> DetectorSetting:
        """The check's setting, created in ``mode`` the first time the check is seen."""
        return cls.objects.get_or_create(detector=detector[:40], defaults={"mode": mode})[0]


class SectionMatch(models.Model):
    """Which NeuroDB section an eTools section name means. An exact name or code match is confirmed
    automatically; any other match waits for an administrator. A name with no confirmed section is
    told to the administrators only, never to every section."""

    class How(models.TextChoices):
        EXACT = "exact", _("Same name")
        CODE = "code", _("Same code")
        CONTAINS = "contains", _("Name contains the other")
        MANUAL = "manual", _("Set by an administrator")
        NONE = "none", _("No match")

    etools_name = models.CharField("eTools section name", max_length=300, unique=True)
    section = models.ForeignKey(
        "users.Section",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="watch_matches",
        verbose_name="NeuroDB section",
    )
    how = models.CharField(max_length=16, choices=How.choices, default=How.NONE)
    confirmed = models.BooleanField(default=False)
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("etools_name",)
        verbose_name = _("eTools section name")
        verbose_name_plural = _("eTools section names")

    def __str__(self):
        return self.etools_name
