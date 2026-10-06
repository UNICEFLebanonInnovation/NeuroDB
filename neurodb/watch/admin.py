"""NeuroDB Watch in the admin ("Data and sync"): what it follows and who was told about it, and the
morning notes, all read-only; the checks, switched between trial, on and off here, each with its
usefulness over the last 30 days (:mod:`neurodb.watch.precision`); and the eTools section names, each
matched to a NeuroDB section and confirmed by an administrator, with the "Not mine" its staff gave."""

from __future__ import annotations

import json

from django.contrib import admin, messages
from django.db.models import Case, CharField, FloatField, IntegerField, Value, When
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import detectors, precision, sections
from .models import DetectorSetting, SectionMatch, WatchItem, WatchNote, WatchReceipt

SEVERITY_TONES = {"critical": "bad", "warning": "warn", "info": "info"}
STATE_TONES = {"open": "info", "closed": "ok", "gone": "muted", "wrong": "warn"}
MODE_TONES = {"on": "ok", "trial": "info", "off": "muted"}
SCORE_FIELDS = ("told", "useful", "not_useful", "wrong", "not_mine")  # precision.Score's counts


def _per_row(field: str, values: dict, output_field, default=None):
    """An annotation giving each row the value computed for it in Python (``values``: the row's
    ``field`` -> value), so a list column can show and sort by it."""
    whens = [When(**{field: key}, then=Value(value)) for key, value in values.items()]
    fallback = Value(default, output_field=output_field)
    return Case(*whens, default=fallback, output_field=output_field) if whens else fallback


def _pretty(value) -> str:
    if not value:
        return "—"
    return format_html(
        '<pre class="nd-json" style="white-space: pre-wrap">{}</pre>',
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
    )


@admin.register(WatchItem)
class WatchItemAdmin(ReadOnlyModelAdmin):
    list_display = (
        "title",
        "detector",
        "severity_badge",
        "state_badge",
        "due_date",
        "scope",
        "first_seen_on",
        "last_seen_on",
    )
    list_filter = ("state", "severity", "kind", "scope", "detector")
    search_fields = ("title", "key", "situation_key", "review_key")
    list_per_page = 50
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "title",
                    "detail",
                    "key",
                    "detector",
                    "kind",
                    "severity",
                    "confidence",
                    "state",
                    "close_reason",
                    "due_date",
                    "url",
                )
            },
        ),
        (
            _("Who is told"),
            {"fields": ("scope", "etools_sections", "section_ids", "has_owner", "assignment_status")},
        ),
        (
            _("Memory"),
            {
                "fields": (
                    "story_lines",
                    "first_seen_on",
                    "last_seen_on",
                    "changed_on",
                    "closed_on",
                    "missed_runs",
                    "source_mark",
                )
            },
        ),
        (
            _("Evidence and connections"),
            {
                "fields": (
                    "evidence_pretty",
                    "evidence_hash",
                    "entity_kind",
                    "entity_key",
                    "situation_key",
                    "related_pretty",
                    "review_key",
                )
            },
        ),
        (_("What NeuroDB looked up (AI)"), {"fields": ("looked_up_pretty",)}),
    )
    readonly_fields = ("story_lines", "evidence_pretty", "related_pretty", "looked_up_pretty")

    @admin.display(description=_("Severity"), ordering="severity")
    def severity_badge(self, obj):
        return badge(obj.get_severity_display(), SEVERITY_TONES.get(obj.severity))

    @admin.display(description=_("State"), ordering="state")
    def state_badge(self, obj):
        return badge(obj.get_state_display(), STATE_TONES.get(obj.state))

    @admin.display(description=_("Story"))
    def story_lines(self, obj):
        lines = [line for line in obj.story or [] if isinstance(line, dict)]
        if not lines:
            return "—"
        return format_html(
            "<ul>{}</ul>",
            format_html_join("", "<li>{}: {}</li>", ((x.get("on", ""), x.get("text", "")) for x in lines)),
        )

    @admin.display(description=_("Evidence"))
    def evidence_pretty(self, obj):
        return _pretty(obj.evidence)

    @admin.display(description=_("Connected to"))
    def related_pretty(self, obj):
        return _pretty(obj.related)

    @admin.display(description=_("Looked up"))
    def looked_up_pretty(self, obj):
        return _pretty(obj.looked_up)


@admin.register(WatchReceipt)
class WatchReceiptAdmin(ReadOnlyModelAdmin):
    """What each person was told and how they reacted. The comment is for administrators only."""

    list_display = (
        "item",
        "user",
        "level",
        "told_step",
        "last_told_on",
        "reaction",
        "seen_at",
        "snoozed_until",
    )
    list_filter = ("level", "reaction", "told_step", "item__detector")
    search_fields = ("item__title", "item__key", "user__username")
    list_select_related = ("item", "user")
    list_per_page = 50


@admin.register(WatchNote)
class WatchNoteAdmin(ReadOnlyModelAdmin):
    list_display = ("date", "audience_name", "text_preview", "written_by_display", "tokens")
    list_filter = ("ai_skipped_reason", "written_by")
    search_fields = ("audience_name", "text")
    date_hierarchy = "date"
    list_per_page = 50
    readonly_fields = ("sentences_pretty",)
    fields = (
        "date",
        "audience_key",
        "audience_name",
        "text",
        "sentences_pretty",
        "written_by",
        "ai_skipped_reason",
        "item_keys",
        "input_hash",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "created_at",
    )

    @admin.display(description=_("Note"))
    def text_preview(self, obj):
        return Truncator(obj.text).chars(160) or "—"

    @admin.display(description=_("Written by"), ordering="written_by")
    def written_by_display(self, obj):
        if obj.written_by == WatchNote.TEMPLATE:
            reason = obj.get_ai_skipped_reason_display() if obj.ai_skipped_reason else _("AI not used")
            return format_html("{} ({})", _("Listed by NeuroDB"), reason)
        return obj.written_by or "—"

    @admin.display(description=_("Tokens"))
    def tokens(self, obj):
        total = obj.input_tokens + obj.output_tokens
        return f"{total:,}" if total else "—"

    @admin.display(description=_("Sentences and the items they cite"))
    def sentences_pretty(self, obj):
        return _pretty(obj.sentences)


@admin.register(DetectorSetting)
class DetectorSettingAdmin(ModelAdmin):
    """Each check starts in trial (seen only by the whole-country view); switch it on for staff once
    it proves useful. Beside each check, its last 30 days: people told about its points, their
    reactions and the usefulness score. A check people find unhelpful goes back to trial by itself."""

    list_display = (
        "check_name",
        "mode_badge",
        "score_display",
        "told_n",
        "useful_n",
        "not_useful_n",
        "wrong_n",
        "not_mine_n",
        "on_since",
        "demoted_display",
    )
    list_filter = ("mode",)
    search_fields = ("detector",)
    list_before_template = "admin/watch/detectorsetting/usefulness.html"
    fields = (
        "detector",
        "check_name",
        "mode",
        "usefulness",
        "on_since",
        "trial_since",
        "demoted_at",
        "demoted_reason",
        "updated_by",
        "updated_at",
    )
    readonly_fields = (
        "detector",
        "check_name",
        "usefulness",
        "on_since",
        "trial_since",
        "demoted_at",
        "demoted_reason",
        "updated_by",
        "updated_at",
    )
    actions = ("keep_in_trial",)

    def has_add_permission(self, request):
        return False  # a check gets its row the first time it runs

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        """Each check with its numbers of the last 30 days (``precision.scores``), as columns."""
        scores = precision.scores()
        queryset = super().get_queryset(request)
        for name in SCORE_FIELDS:
            counts = {check: getattr(score, name) for check, score in scores.items()}
            queryset = queryset.annotate(**{name: _per_row("detector", counts, IntegerField(), 0)})
        rated = {check: score.score for check, score in scores.items() if score.score is not None}
        return queryset.annotate(score=_per_row("detector", rated, FloatField()))

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            detectors.ensure_settings()  # every check listed from the start, not after its first run
        extra_context = {
            "usefulness": {
                "days": precision.WINDOW_DAYS,
                "min_ratings": precision.MIN_RATINGS,
                "demote_percent": round(precision.DEMOTE_SHARE * 100),
                "gate": precision.gate(),
            },
            **(extra_context or {}),
        }
        return super().changelist_view(request, extra_context)

    @admin.display(description=_("Check"), ordering="detector")
    def check_name(self, obj):
        return format_html(
            '{}<br><small class="text-font-subtle-light">{}</small>',
            precision.label(obj.detector),
            obj.detector,
        )

    @admin.display(description=_("Mode"), ordering="mode")
    def mode_badge(self, obj):
        return badge(obj.get_mode_display(), MODE_TONES.get(obj.mode))

    @admin.display(description=_("Useful (30 days)"), ordering="score")
    def score_display(self, obj):
        score = getattr(obj, "score", None)
        if score is None:
            return "—"
        tone = "ok" if score >= 1 - precision.DEMOTE_SHARE else "warn" if score >= 0.5 else "bad"
        return badge(f"{round(score * 100)}%", tone)

    @admin.display(description=_("Told"), ordering="told")
    def told_n(self, obj):
        return getattr(obj, "told", 0)

    @admin.display(description=_("Useful"), ordering="useful")
    def useful_n(self, obj):
        return getattr(obj, "useful", 0)

    @admin.display(description=_("Not useful"), ordering="not_useful")
    def not_useful_n(self, obj):
        return getattr(obj, "not_useful", 0)

    @admin.display(description=_("Something's wrong"), ordering="wrong")
    def wrong_n(self, obj):
        return getattr(obj, "wrong", 0)

    @admin.display(description=_("Not mine"), ordering="not_mine")
    def not_mine_n(self, obj):
        return getattr(obj, "not_mine", 0)

    @admin.display(description=_("Went back to trial"), ordering="demoted_at")
    def demoted_display(self, obj):
        if obj.demoted_at is None:
            return "—"
        return format_html(
            "{}<br><small>{}</small>",
            timezone.localtime(obj.demoted_at).strftime("%d %b %Y"),
            obj.demoted_reason,
        )

    @admin.display(description=_("Last 30 days"))
    def usefulness(self, obj):
        score = precision.scores().get(obj.detector) or precision.Score(detector=obj.detector)
        values = {
            "told": score.told,
            "useful": score.useful,
            "not_useful": score.not_useful,
            "wrong": score.wrong,
            "not_mine": score.not_mine,
            "score": f"{round(score.score * 100)}%" if score.score is not None else "—",
        }
        return format_html(
            "{} people told · {} useful · {} not useful · {} something's wrong · {} not mine · usefulness {}",
            *values.values(),
        )

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        if "mode" in form.changed_data:
            obj.demoted_at, obj.demoted_reason = None, ""  # an administrator decided
            if obj.mode == DetectorSetting.Mode.ON:
                obj.on_since = timezone.localdate()  # what exists today is known: not announced
            elif obj.mode == DetectorSetting.Mode.TRIAL:
                obj.trial_since = timezone.localdate()  # the same, for the whole-country view
        super().save_model(request, obj, form, change)

    @admin.action(
        description=_("Keep in trial (clears the notice that it went back)"), permissions=("change",)
    )
    def keep_in_trial(self, request, queryset):
        """An administrator saw that the check went back to trial and keeps it there: the admin home
        stops listing it."""
        done = queryset.filter(mode=DetectorSetting.Mode.TRIAL, demoted_at__isnull=False).update(
            demoted_at=None,
            demoted_reason="",
            updated_by=request.user.get_username(),
            updated_at=timezone.now(),
        )
        self.message_user(request, _("%(n)s check(s) kept in trial.") % {"n": done}, messages.SUCCESS)


@admin.register(SectionMatch)
class SectionMatchAdmin(ModelAdmin):
    """Which NeuroDB section each eTools section name means. A name without a confirmed section is
    told to the administrators only."""

    list_display = (
        "etools_name",
        "section",
        "how",
        "confirmed_badge",
        "not_mine_display",
        "updated_by",
        "updated_at",
    )
    list_filter = ("confirmed", "how", "section")
    search_fields = ("etools_name", "section__name")
    list_select_related = ("section",)
    fields = ("etools_name", "section", "confirmed", "how", "not_mine_display", "updated_by", "updated_at")
    actions = ("confirm",)
    actions_list = ("match_again",)

    @action(description=_("Match again"), url_path="match-again", icon="sync", permissions=["change"])
    def match_again(self, request):
        """Matches again the names matched automatically and not confirmed yet (after a NeuroDB section
        was added, renamed or deleted); a name set by hand or confirmed is never changed. The admin
        button for ``map_watch_sections --rematch``."""
        counts = sections.seed(rematch=True)
        self.message_user(
            request,
            _(
                "%(added)s new name(s) added, %(rematched)s matched again; %(waiting)s still waiting for "
                "you to choose or confirm their section."
            )
            % {
                "added": counts["added"],
                "rematched": counts["rematched"],
                "waiting": sections.waiting().count(),
            },
            messages.SUCCESS,
        )
        return redirect("admin:watch_sectionmatch_changelist")

    def changelist_view(self, request, extra_context=None):
        """The names are added when the list is opened, not only by NeuroDB Watch's run, so they can
        be confirmed before its first morning."""
        if request.method == "GET":
            counts = sections.seed()
            if counts["added"]:
                self.message_user(
                    request,
                    _(
                        "%(added)s eTools section names added: %(confirmed)s matched by their name or "
                        "code, %(waiting)s waiting for you to choose or confirm their NeuroDB section."
                    )
                    % {
                        "added": counts["added"],
                        "confirmed": counts["confirmed"],
                        "waiting": counts["added"] - counts["confirmed"],
                    },
                    messages.SUCCESS,
                )
            elif not counts["names"]:
                self.message_user(
                    request,
                    _(
                        "No eTools section names in NeuroDB yet: they come from the eTools (Datamart) "
                        "data. Run the eTools sync first, then open this page again."
                    ),
                    messages.WARNING,
                )
        return super().changelist_view(request, extra_context)

    def get_readonly_fields(self, request, obj=None):
        fixed = ("how", "not_mine_display", "updated_by", "updated_at")
        return ("etools_name", *fixed) if obj else fixed

    def get_queryset(self, request):
        """Each name with the "Not mine" its section's staff gave in the last 30 days on the points
        sent to them under it (``precision.not_mine_for``): the total, and the count per check."""
        counts = precision.not_mine()
        rows = list(super().get_queryset(request).values_list("pk", "etools_name", "section_id"))
        found = {pk: precision.not_mine_for(name, section_id, counts) for pk, name, section_id in rows}
        totals = {pk: sum(by_check.values()) for pk, by_check in found.items() if by_check}
        checks = {
            pk: " · ".join(f"{precision.label(check)}: {n}" for check, n in by_check.items())
            for pk, by_check in found.items()
            if by_check
        }
        return (
            super()
            .get_queryset(request)
            .annotate(
                not_mine=_per_row("pk", totals, IntegerField(), 0),
                not_mine_checks=_per_row("pk", checks, CharField(), ""),
            )
        )

    @admin.display(description=_("Not mine (30 days)"), ordering="not_mine")
    def not_mine_display(self, obj):
        total = getattr(obj, "not_mine", None)
        if total is None:  # a new row, not read through the list
            total = sum(precision.not_mine_for(obj.etools_name, obj.section_id).values())
            checks = ""
        else:
            checks = getattr(obj, "not_mine_checks", "")
        if not total:
            return "—"
        return format_html("{}<br><small>{}</small>", badge(str(total), "warn"), checks)

    @admin.display(description=_("Confirmed"), ordering="confirmed")
    def confirmed_badge(self, obj):
        if obj.confirmed:
            return badge(_("Confirmed"), "ok")
        return badge(_("To confirm") if obj.section_id else _("No section"), "warn")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        if not change or "section" in form.changed_data:
            obj.how = SectionMatch.How.MANUAL  # never overwritten by the automatic matching
            if "confirmed" not in form.changed_data:
                obj.confirmed = obj.section_id is not None  # choosing the section confirms it
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Confirm the matched section"), permissions=("change",))
    def confirm(self, request, queryset):
        without = queryset.filter(section__isnull=True).count()
        done = queryset.filter(section__isnull=False, confirmed=False).update(
            confirmed=True, updated_by=request.user.get_username(), updated_at=timezone.now()
        )
        self.message_user(request, _("%(n)s name(s) confirmed.") % {"n": done}, messages.SUCCESS)
        if without:
            self.message_user(
                request,
                _("%(n)s name(s) have no section yet: open them to choose one.") % {"n": without},
                messages.WARNING,
            )
