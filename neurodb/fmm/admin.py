"""Monitoring insights in the admin. Administrators change the settings; other staff read them.

- **Fields found**: which keys the eTools field monitoring records hold and which key each field is read
  from, as the last refresh found them, with the rates that tell whether the data can be trusted. An
  administrator can pin another key the data shows (an override), with a note;
- **Questions found**: the checklist questions the answers hold, the role each one has (Q1, Q2, Q3,
  PSEA) and buttons that give a question its role;
- **Quality rules** and **Score settings**: R1-R6, the score bands, urgency and the question roles,
  each save with a note, with a preview of its effect before it is saved;
- **Rule versions**: every saved state of the rules, the score settings and the pinned keys, with who,
  when and why, and "Restore this version";
- **Visits**: the visits the refresh built, with their entity rows, action points, rule results and
  data problems, for checking the data;
- **Visit reviews**: the marks sections put on visits.

Every save of a rule, a score setting, a question's role, a pinned key or a restore records a new rules
version (``fmm.versions``) and asks for the scores (or, for a key, the visits) to be recomputed in the
background; the admin request never waits for it."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action

from neurodb.core.models import SyncRun
from neurodb.integrations import background
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import access, fields, privacy, rules, status, versions
from .ai import profiles
from .models import (
    FieldMapping,
    KeyProbe,
    ModelCapability,
    PromptProfile,
    PromptVersion,
    QuestionAnswer,
    RuleSetting,
    RuleSetVersion,
    ScoreSetting,
    Visit,
    VisitActionPoint,
    VisitEntity,
    VisitReview,
    VisitRuleResult,
    default_urgency_weights,
)

STATE_TONES = {
    FieldMapping.State.FOUND: "ok",
    FieldMapping.State.OVERRIDE: "info",
    FieldMapping.State.AMBIGUOUS: "warn",
    FieldMapping.State.OVERRIDE_MISSING: "bad",
    FieldMapping.State.MISSING: "muted",
}
PD_TARGET = 0.9  # the share of programme document rows that should be linked (go-live checklist)
R2_MIN_RECORDS = 200  # below this, no answers left blank says nothing about the export
MATCH_TARGET = 0.5  # below this share of records joined to their visit, Fields found warns


def _pct(share: float | None) -> str:
    return "—" if share is None else f"{share * 100:.0f}%"


def _rates(run: SyncRun | None) -> list[dict[str, Any]]:
    """The last key reading's rates, in plain words, each with a warning when it is low."""
    if run is None:
        return []
    details = run.details or {}
    lines: list[dict[str, Any]] = []
    ids = details.get("activity_ids") or {}
    if ids.get("rows"):
        share = ids.get("share") or 0
        lines.append(
            {
                "text": _(
                    "%(pct)s of field monitoring finding rows carry an eTools activity id "
                    "(%(n)s of %(total)s)."
                )
                % {"pct": _pct(share), "n": f"{ids['with_id']:,}", "total": f"{ids['rows']:,}"},
                "hint": _("Below 95%, a visit is told apart by its activity reference instead.")
                if share < 0.95
                else "",
                "warn": share < 0.95,
            }
        )
    pd = details.get("pd_resolved") or {}
    if pd.get("pd_kind_rows"):
        linked = pd["pd_kind_rows"] - pd.get("unresolved", 0)
        share = linked / pd["pd_kind_rows"]
        lines.append(
            {
                "text": _(
                    "%(pct)s of the rows about a programme document are linked to it (%(n)s of %(total)s: "
                    "%(exact)s by the full reference, %(token)s by the PCA/PD pair, %(base)s without the "
                    "amendment, %(title)s by title)."
                )
                % {
                    "pct": _pct(share),
                    "n": f"{linked:,}",
                    "total": f"{pd['pd_kind_rows']:,}",
                    "exact": f"{pd.get('exact', 0):,}",
                    "token": f"{pd.get('token', 0):,}",
                    "base": f"{pd.get('base', 0):,}",
                    "title": f"{pd.get('title', 0):,}",
                },
                "hint": _("Aim for 90% or more: check the references the unlinked rows give.")
                if share < PD_TARGET
                else "",
                "warn": share < PD_TARGET,
            }
        )
    questions = details.get("questions") or {}
    if questions.get("records") and questions.get("answers_found"):
        lines.append(
            {
                "text": _("%(pct)s of the %(total)s checklist answer records hold an answer.")
                % {"pct": _pct(questions.get("answered_share")), "total": f"{questions['records']:,}"},
                "hint": "",
                "warn": False,
            }
        )
    elif questions.get("records"):
        lines.append(
            {
                "text": _(
                    "The answers of the %(total)s checklist records were not found under any known key."
                )
                % {"total": f"{questions['records']:,}"},
                "hint": _("Rules R2, R3 and R5 and the HACT Q1 and PSEA figures are not available."),
                "warn": True,
            }
        )
    return lines


def _match_rates(run: SyncRun | None) -> list[dict[str, Any]]:
    """How well the last build joined the records to visits: the checklist answer records that found
    their visit, and how the FM action points were matched (few by the activity id means eTools'
    related module id may not be the activity id)."""
    details = (run.details or {}) if run else {}
    lines: list[dict[str, Any]] = []
    questions = details.get("questions") or {}
    linked = questions.get("linked") or 0
    read = linked + (questions.get("unlinked") or 0)
    if read:
        share = linked / read
        lines.append(
            {
                "text": _("%(pct)s of the checklist answer records matched a visit (%(n)s of %(total)s).")
                % {"pct": _pct(share), "n": f"{linked:,}", "total": f"{read:,}"},
                "hint": _("Check the keys of the activity id and reference of the checklist answers.")
                if share < MATCH_TARGET
                else "",
                "warn": share < MATCH_TARGET,
            }
        )
    points = details.get("action_points") or {}
    total = points.get("fm_total") or 0
    if total:
        share = (points.get("related_id") or 0) / total
        lines.append(
            {
                "text": _(
                    "FM action points: %(id)s matched to their visit by the activity id, %(ref)s by the "
                    "activity reference, %(number)s by the reference number, %(none)s not matched "
                    "(%(total)s in all)."
                )
                % {
                    "id": _pct(share),
                    "ref": _pct((points.get("reference") or 0) / total),
                    "number": _pct((points.get("reference_number") or 0) / total),
                    "none": _pct((points.get("unlinked") or 0) / total),
                    "total": f"{total:,}",
                },
                "hint": _(
                    "Under half match by the activity id: check whether eTools' related module id of "
                    "an action point is the activity id."
                )
                if share < MATCH_TARGET
                else "",
                "warn": share < MATCH_TARGET,
            }
        )
    return lines


def _r2_line(run: SyncRun | None) -> dict[str, Any] | None:
    """Whether eTools sends unanswered questions at all: without any, R2 cannot be measured."""
    questions = ((run.details or {}) if run else {}).get("questions") or {}
    if not questions.get("records") or not questions.get("answers_found"):
        return None
    unanswered, total = questions.get("unanswered") or 0, questions["records"]
    text = _("Unanswered questions seen: %(n)s of %(total)s records") % {
        "n": f"{unanswered:,}",
        "total": f"{total:,}",
    }
    r2 = RuleSetting.objects.filter(code="R2").first()
    checked = r2 is None or (r2.enabled and rules.param(r2, "require_unanswered_seen") is not False)
    if unanswered == 0 and total >= R2_MIN_RECORDS and checked:  # an administrator may turn the check off
        return {"text": text + _(" — R2 cannot be measured"), "warn": True}
    return {"text": text, "warn": False}


def _types(counts: dict[str, int]) -> str:
    return " · ".join(f"{name} {n:,}" for name, n in counts.items())


def _failed_after(latest: SyncRun | None, shown: SyncRun | None) -> SyncRun | None:
    """The last refresh, when it failed to read the keys again after the reading shown (a refresh that
    only recomputes the scores reads no key, and a running one is said so apart)."""
    if latest is None or latest.status != SyncRun.Status.FAILED or latest.target not in ("full", "probe"):
        return None
    if shown is not None and (latest.pk == shown.pk or latest.started_at < shown.started_at):
        return None
    return latest


def fields_found() -> dict[str, Any]:
    """Everything the Fields found page shows."""
    probe_run = status.last_probe()
    latest = status.last_run()
    mappings = {(m.dataset, m.field): m for m in FieldMapping.objects.all()}
    probes: dict[str, list[KeyProbe]] = defaultdict(list)
    for row in KeyProbe.objects.order_by("dataset", "key"):
        probes[row.dataset].append(row)
    datasets_details = ((probe_run.details or {}) if probe_run else {}).get("datasets") or {}
    datasets = []
    for name in fields.DATASETS:
        rows = []
        for field in fields.CANDIDATES[name]:
            mapping = mappings.get((name, field))
            state = mapping.state if mapping else FieldMapping.State.MISSING
            rows.append(
                {
                    "field": field,
                    "kind": fields.kind_of(field),
                    "person": (name, field) in fields.PERSON_FIELDS,
                    "key": mapping.chosen_key if mapping else "",
                    "state": badge(FieldMapping.State(state).label, STATE_TONES.get(state)),
                    "coverage": _pct(mapping.coverage) if mapping and mapping.chosen_key else "—",
                    "candidates": [
                        {"key": c["key"], "pct": _pct(c.get("coverage"))}
                        for c in (mapping.candidates if mapping else [])
                    ],
                    "listed": fields.CANDIDATES[name][field],
                    "needed_by": fields.NEEDED_BY.get((name, field), ""),
                    "override": mapping.override_key if mapping else "",
                    "pk": mapping.pk if mapping else None,
                }
            )
        keys = [
            {
                "key": row.key,
                "records": row.records,
                "total": row.total,
                "types": _types(row.types or {}),
                "examples": row.examples or [],
            }
            for row in probes.get(name, [])
        ]
        total = (datasets_details.get(name) or {}).get("records")
        if total is None and keys:
            total = keys[0]["total"]
        datasets.append(
            {
                "name": name,
                "label": fields.DATASET_LABELS[name],
                "total": total,
                "fields": rows,
                "keys": keys,
                "r2": _r2_line(probe_run) if name == "fm_questions" else None,
            }
        )
    details = (probe_run.details or {}) if probe_run else {}
    return {
        "run": probe_run,
        "failed": _failed_after(latest, probe_run),
        "running": background.is_running(SyncRun.Job.FMM_REFRESH),
        "rates": _rates(probe_run) + _match_rates(status.last_build()),
        "not_found": details.get("fields_not_found") or [],
        "ambiguous": details.get("fields_ambiguous") or [],
        "overrides_missing": details.get("overrides_missing") or [],
        "datasets": datasets,
        "values": [
            (_("Ratings"), details.get("ratings_seen") or []),
            (_("Statuses"), details.get("statuses_seen") or []),
            (_("Entity types"), details.get("entity_types_seen") or []),
        ],
        "min_coverage": _pct(settings.FMM_KEY_MIN_COVERAGE),
    }


# ------------------------------------------------------------------------------------------ editing
class JSONTextField(forms.JSONField):
    """A JSON setting in a textarea, laid out on several lines so that it can be read and edited."""

    widget = forms.Textarea(attrs={"rows": 10, "class": "font-mono", "spellcheck": "false"})

    def __init__(self, *args, order: tuple[str, ...] = (), **kwargs):
        self.order = order  # the keys of an object shown first, in this order (the database keeps none)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, str):  # what was typed, shown back as it was when it is not valid
            return value
        if isinstance(value, dict) and self.order:
            first = {key: value[key] for key in self.order if key in value}
            value = {**first, **{key: v for key, v in value.items() if key not in first}}
        return json.dumps(value, ensure_ascii=False, indent=2)


class _NoteForm(forms.ModelForm):
    """A change form that asks why: the note is kept with the rules version the save records."""

    change_note = forms.CharField(
        label=_("Change note"),
        max_length=versions.NOTE_CHARS,
        help_text=_(
            "Required: what changed and why. It is kept with the new rules version, with your name and the "
            "time, and the change can be rolled back from Rule versions."
        ),
    )


class _VersionedAdmin(ModelAdmin):
    """Settings that are versioned with the quality rules (rules, score settings, pinned keys).
    Administrators change them, never add or delete them; every save records a rules version and asks
    for the scores (``full_refresh``: the visits) to be recomputed in the background. A *Preview effect*
    button (``preview_url``) scores this year's visits in memory with the form's values, unsaved."""

    full_refresh = False
    preview = True  # the Preview effect button
    change_form_template = "admin/fmm/versioned_change_form.html"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return access.is_admin(request.user)

    def version_note(self, obj, form) -> str:
        return form.cleaned_data["change_note"]

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        version = versions.record_rules(request.user, self.version_note(obj, form))
        versions.start_rescore(request.user, full=self.full_refresh)
        request._fmm_saved = True
        messages.success(request, versions.saved_message(version, full=self.full_refresh))

    def message_user(self, request, message, level=messages.INFO, *args, **kwargs):
        if getattr(request, "_fmm_saved", False) and level == messages.SUCCESS:
            return  # the rules version's message says it already
        super().message_user(request, message, level, *args, **kwargs)

    # the preview of a change, before it is saved
    def preview_changes(self, obj) -> tuple[dict, dict]:
        """(rule changes, score changes) of ``obj`` as the form left it, for ``versions.preview``."""
        return {}, {}

    def get_urls(self):
        if not self.preview:
            return super().get_urls()
        return [
            path(
                "<path:object_id>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name=f"{self.opts.app_label}_{self.opts.model_name}_preview",
            ),
            *super().get_urls(),
        ]

    def preview_view(self, request, object_id):
        """The effect of the form's values (posted by the *Preview effect* button), scored in memory over
        this year's visits; nothing is saved. Administrators only."""
        if not access.is_admin(request.user) or request.method != "POST":
            raise Http404
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        data = request.POST.copy()
        data["change_note"] = data.get("change_note") or "preview"  # the note is asked for at the save
        form = self.get_form(request, obj, change=True)(data, request.FILES, instance=obj)
        context: dict[str, Any] = {"errors": [], "sentence": "", "result": None}
        if form.is_valid():
            rule_changes, score_changes = self.preview_changes(form.instance)
            result = versions.preview(rule_changes, score_changes)
            codes = list(rule_changes) or None
            context.update(result=result, sentence=versions.describe(result, codes))
        else:
            context["errors"] = [
                f"{form.fields[name].label if name in form.fields else ''}: {message}".lstrip(": ")
                for name, errors in form.errors.items()
                for message in errors
            ]
        return HttpResponse(render_to_string("admin/fmm/_preview.html", context, request=request))

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if self.preview and obj is not None and access.is_admin(request.user):
            name = f"admin:{self.opts.app_label}_{self.opts.model_name}_preview"
            context["preview_url"] = reverse(name, args=[obj.pk])
        return super().render_change_form(request, context, add, change, form_url, obj)


def _last_results(code: str) -> str:
    """What the last refresh found for one rule, in plain words."""
    run = status.last_refresh()
    counts = (((run.details or {}) if run else {}).get("rule_results") or {}).get(code)
    if run is None or counts is None:
        return _("Not computed yet: shown after the next refresh of Monitoring insights.")
    evaluated = counts.get("pass", 0) + counts.get("fail", 0)
    return _(
        "Last refresh (%(when)s, rules v%(version)s): evaluated on %(evaluated)s visits, %(flagged)s of them "
        "flagged; not available on %(na)s; does not apply to %(nap)s; switched off on %(off)s."
    ) % {
        "when": date_format(timezone.localtime(run.finished_at), "j M Y, H:i") if run.finished_at else "—",
        "version": (run.details or {}).get("rules_version", "—"),
        "evaluated": f"{evaluated:,}",
        "flagged": f"{counts.get('fail', 0):,}",
        "na": f"{counts.get('na', 0):,}",
        "nap": f"{counts.get('nap', 0):,}",
        "off": f"{counts.get('off', 0):,}",
    }


# ------------------------------------------------------------------------------------------ fields found
class FieldMappingForm(_NoteForm):
    """Pin a key the data shows to a field, or leave the choice to the refresh ("auto")."""

    override_key = forms.ChoiceField(
        label=_("Pinned key"),
        required=False,
        help_text=_(
            "Auto: the refresh chooses the key (the first listed key that fills enough records). A pinned "
            "key is used as long as the data shows it. The visits are rebuilt in the background after the "
            "save."
        ),
    )

    class Meta:
        model = FieldMapping
        fields = ("override_key",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mapping = self.instance
        person_field = (mapping.dataset, mapping.field) in fields.PERSON_FIELDS
        choices = [("", _("auto (the refresh chooses)"))]
        shown = set()
        for key, records, total in KeyProbe.objects.filter(dataset=mapping.dataset).values_list(
            "key", "records", "total"
        ):
            if privacy.person_like(key) and not person_field:
                continue  # a key that holds a person is never read as another field
            shown.add(key)
            label = _("%(key)s (%(n)s of %(total)s records)") % {
                "key": key,
                "n": f"{records:,}",
                "total": f"{total:,}",
            }
            choices.append((key, label))
        if mapping.override_key and mapping.override_key not in shown:
            choices.append(
                (mapping.override_key, _("%(key)s (not in the data)") % {"key": mapping.override_key})
            )
        self.fields["override_key"].choices = choices


@admin.register(FieldMapping)
class FieldMappingAdmin(_VersionedAdmin):
    """Fields found: the keys the eTools field monitoring records hold, the key each field is read
    from and why, and the rates of the last reading. Administrators pin another key (an override),
    versioned with the quality rules; a pinned key rebuilds the visits in the background."""

    full_refresh = True
    preview = False  # a key changes the visits, not only their scores: nothing to score in memory
    form = FieldMappingForm
    change_list_template = "admin/fmm/fieldmapping/change_list.html"
    change_form_template = None
    list_display = ("dataset", "field", "chosen_key", "state", "coverage")
    list_filter = ("dataset", "state")
    search_fields = ("field", "chosen_key")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "dataset",
                    "field",
                    "chosen_key",
                    "state_shown",
                    "coverage_shown",
                    "candidates_shown",
                )
            },
        ),
        (_("Pinned key"), {"fields": ("override_key", "change_note")}),
        (None, {"fields": ("needed_by", "updated_by", "updated_at")}),
    )
    readonly_fields = (
        "dataset",
        "field",
        "chosen_key",
        "state_shown",
        "coverage_shown",
        "candidates_shown",
        "needed_by",
        "updated_by",
        "updated_at",
    )

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "found": fields_found(),
            "can_pin": access.is_admin(request.user),
        }
        return super().changelist_view(request, extra_context)

    def version_note(self, obj, form) -> str:
        key = obj.override_key or "auto"
        return f"Field override: {obj.dataset}.{obj.field} → {key}: {form.cleaned_data['change_note']}"

    def save_model(self, request, obj, form, change):
        if "override_key" not in form.changed_data:
            request._fmm_saved = True
            messages.info(request, _("The pinned key did not change: nothing was recorded."))
            return
        super().save_model(request, obj, form, change)

    def site_urls(self):
        """Questions found, at admin/fmm/questions/ (``NeuroDBAdminSite.get_urls``)."""
        return [path("fmm/questions/", self.admin_site.admin_view(questions_view), name="fmm_questions")]

    @admin.display(description=_("state"))
    def state_shown(self, obj):
        return badge(FieldMapping.State(obj.state).label, STATE_TONES.get(obj.state))

    @admin.display(description=_("records filled"))
    def coverage_shown(self, obj):
        return _pct(obj.coverage) if obj.chosen_key else "—"

    @admin.display(description=_("keys listed, as found"))
    def candidates_shown(self, obj):
        found = [f"{c['key']} {_pct(c.get('coverage'))}" for c in obj.candidates or []]
        return " · ".join(found) or _("none of %(keys)s") % {
            "keys": ", ".join(fields.CANDIDATES.get(obj.dataset, {}).get(obj.field, ()))
        }

    @admin.display(description=_("needed for"))
    def needed_by(self, obj):
        return fields.NEEDED_BY.get((obj.dataset, obj.field), "")


# ------------------------------------------------------------------------------------------ questions found
def questions_found() -> list[dict[str, Any]]:
    """Every checklist question the answers hold, with its records, whether eTools flags it as HACT,
    the role the score settings give it now and the share of its records answered."""
    patterns = ScoreSetting.load().question_patterns
    patterns = patterns if isinstance(patterns, dict) else {}
    rows = (
        QuestionAnswer.objects.order_by()
        .values("question_text", "is_hact")
        .annotate(
            records=Count("pk"),
            answered=Count("pk", filter=Q(answered=True)),
            visits=Count("visit_id", distinct=True),
        )
        .order_by("-records", "question_text")
    )
    out = []
    for row in rows:
        role = rules.assign_roles(row["question_text"], row["is_hact"], patterns)
        out.append(
            {
                **row,
                "role": role,
                "role_label": versions.ROLE_LABELS.get(role, ""),
                "share": _pct(row["answered"] / row["records"] if row["records"] else None),
                "pinned": versions.question_pattern(row["question_text"]) in (patterns.get(role) or ()),
            }
        )
    return out


def questions_view(request):
    """Questions found (Administrators only): the checklist questions, their roles, and buttons that
    give a question a role (Q1, Q2, Q3 or PSEA), recorded as a rules version."""
    if not access.is_admin(request.user):
        raise Http404
    if request.method == "POST":
        role, text = request.POST.get("role", ""), request.POST.get("question_text", "")
        if role not in versions.ROLE_LABELS or not text.strip():
            messages.error(request, _("Choose a question and a role."))
        else:
            try:
                version = versions.pin_question(role, text, request.user)
            except (ValidationError, ValueError) as exc:
                problems = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
                messages.error(request, _("Not saved: %(why)s") % {"why": " ".join(problems)})
            else:
                messages.success(request, versions.saved_message(version))
        return redirect("admin:fmm_questions")
    run = status.last_build()
    context = {
        **admin.site.each_context(request),
        "title": _("Questions found"),
        "subtitle": None,
        "questions": questions_found(),
        "roles": list(versions.ROLE_LABELS.items()),
        "run": run,
        "patterns": ScoreSetting.load().question_patterns,
        "opts": FieldMapping._meta,
        "fields_url": reverse("admin:fmm_fieldmapping_changelist"),
        "settings_url": reverse("admin:fmm_scoresetting_changelist"),
    }
    return render(request, "admin/fmm/questions.html", context)


# ------------------------------------------------------------------------------------------ quality rules
class RuleSettingForm(_NoteForm):
    params = JSONTextField(
        label=_("Parameters"),
        required=False,
        help_text=_(
            'The rule\'s settings as JSON, e.g. {"strict": false}. Lists of words are compared in lower '
            "case, without accents or punctuation; each list holds at most 60 entries of 1 to 80 characters."
        ),
    )

    class Meta:
        model = RuleSetting
        fields = ("enabled", "points", "threshold", "params", "description")

    def clean_params(self):
        return self.cleaned_data.get("params") or {}


@admin.register(RuleSetting)
class RuleSettingAdmin(_VersionedAdmin):
    """The quality rules R1-R6: points, threshold and parameters, each save with a note and a rules
    version, previewed before it is saved."""

    form = RuleSettingForm
    list_display = ("code", "label", "enabled", "points", "threshold", "updated_by", "updated_at")
    list_select_related = ("updated_by",)
    ordering = ("code",)
    fieldsets = (
        (None, {"fields": ("code", "label", "last_results")}),
        (_("Settings"), {"fields": ("enabled", "points", "threshold", "params", "description")}),
        (_("Why"), {"fields": ("change_note",)}),
        (None, {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("code", "label", "last_results", "updated_by", "updated_at")

    def preview_changes(self, obj) -> tuple[dict, dict]:
        values = {name: getattr(obj, name) for name in ("enabled", "points", "threshold", "params")}
        return {obj.code: values}, {}

    @admin.display(description=_("last refresh"))
    def last_results(self, obj):
        return _last_results(obj.code)


class ScoreSettingForm(_NoteForm):
    urgency_weights = JSONTextField(
        order=tuple(default_urgency_weights()),
        label=_("Urgency weights"),
        help_text=_(
            "Points each part of urgency adds (whole numbers from 0 to 100): off_track and constrained (the "
            "worse of the rating and HACT Q1), quality_gap (times the share of the score missing), "
            "unscored_reported, per_flag up to flags_max, no_follow_up, ap_overdue, ap_high_overdue and "
            "ap_high_open up to follow_up_max, report_late."
        ),
    )
    question_patterns = JSONTextField(
        order=rules.ROLES,
        label=_("Question patterns"),
        help_text=_(
            "How Q1, Q2, Q3 and the PSEA question are found among the checklist questions: words the "
            'question contains, "^words" for the words it starts with, "=text" for its whole text '
            "(Questions found writes these). At most 20 per role, 2 to 200 characters each."
        ),
    )
    role_flag_answers = JSONTextField(
        order=rules.ROLES,
        label=_("Answers that flag"),
        help_text=_(
            'The answer codes that flag a visit for a question role, e.g. {"psea": ["yes"]}. Codes: '
            "on_track, constrained, off_track, yes, no."
        ),
    )

    class Meta:
        model = ScoreSetting
        fields = (
            "min_evaluated_points",
            "band_high",
            "band_medium",
            "high_flag_count",
            "urgency_red",
            "urgency_amber",
            "urgency_weights",
            "follow_up_days",
            "report_late_days",
            "question_patterns",
            "role_flag_answers",
        )


@admin.register(ScoreSetting)
class ScoreSettingAdmin(_VersionedAdmin):
    """The one row of score settings: bands, urgency and the question roles. Its list opens the row."""

    form = ScoreSettingForm
    fieldsets = (
        (
            _("Score and bands"),
            {"fields": ("min_evaluated_points", ("band_high", "band_medium"), "high_flag_count")},
        ),
        (
            _("Urgency"),
            {
                "fields": (
                    ("urgency_red", "urgency_amber"),
                    "urgency_weights",
                    ("follow_up_days", "report_late_days"),
                )
            },
        ),
        (_("Question roles"), {"fields": ("question_patterns", "role_flag_answers")}),
        (_("Why"), {"fields": ("change_note",)}),
        (None, {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("updated_by", "updated_at")

    def changelist_view(self, request, extra_context=None):
        return redirect("admin:fmm_scoresetting_change", ScoreSetting.load().pk)

    def preview_changes(self, obj) -> tuple[dict, dict]:
        return {}, {name: getattr(obj, name) for name in versions.SCORE_FIELDS}


# ------------------------------------------------------------------------------------------ versions
def _shown(value: Any) -> str:
    """A setting's value in a table: lists joined, objects as JSON, nothing as a dash."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return _("yes") if value else _("no")
    if isinstance(value, list):
        return ", ".join(_shown(v) for v in value) or "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _difference_rows(version: RuleSetVersion, changed_only: bool = False) -> list[dict[str, Any]]:
    rows = versions.differences(version.snapshot or {})
    return [
        {**row, "then": _shown(row["then"]), "now": _shown(row["now"])}
        for row in rows
        if row["changed"] or not changed_only
    ]


@admin.register(RuleSetVersion)
class RuleSetVersionAdmin(ReadOnlyModelAdmin):
    """Every saved state of the rules, the score settings and the pinned keys. Read-only: restoring a
    version writes it back as a new version (Administrators only)."""

    list_display = ("number", "note", "created_by_name", "created_at", "restored_from")
    list_select_related = ("restored_from",)
    search_fields = ("note", "created_by_name")
    fields = ("number", "note", "created_by_name", "created_at", "restored_from", "differences_table")
    readonly_fields = fields
    actions_detail = ("restore_version",)

    def has_restore_permission(self, request, obj=None):
        return access.is_admin(request.user)

    @admin.display(description=_("settings then and now"))
    def differences_table(self, obj):
        return render_to_string("admin/fmm/rulesetversion/_differences.html", {"rows": _difference_rows(obj)})

    def get_urls(self):
        return [
            path(
                "<int:pk>/restore/",
                self.admin_site.admin_view(self.restore_view),
                name="fmm_rulesetversion_restore",
            ),
            *super().get_urls(),
        ]

    @action(
        description=_("Restore this version"),
        url_path="restore-version",
        icon="history",
        permissions=["restore"],
    )
    def restore_version(self, request, object_id):
        return redirect("admin:fmm_rulesetversion_restore", object_id)

    def restore_view(self, request, pk):
        """Confirm a restore with a note (Administrators only), then write the version back as a new one."""
        if not access.is_admin(request.user):
            raise Http404
        version = get_object_or_404(RuleSetVersion, pk=pk)
        note = request.POST.get("note", "").strip() if request.method == "POST" else ""
        if request.method == "POST":
            new = versions.restore_rules(version, request.user, note)
            messages.success(
                request,
                _("Restored v%(old)s as rules v%(new)s. Scores will be recomputed in the background.")
                % {"old": version.number, "new": new.number},
            )
            return redirect("admin:fmm_rulesetversion_change", new.pk)
        rows = _difference_rows(version, changed_only=True)
        context = {
            **self.admin_site.each_context(request),
            "title": _("Restore rules v%(n)s") % {"n": version.number},
            "version": version,
            "rows": rows,
            "keys_change": any(row["group"] == "Pinned keys" for row in rows),
            "opts": self.model._meta,
            "note_chars": versions.NOTE_CHARS,
        }
        return render(request, "admin/fmm/rulesetversion/restore_confirm.html", context)


# ------------------------------------------------------------------------------------------ visits
class _ReadOnlyInline(TabularInline):
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class VisitEntityInline(_ReadOnlyInline):
    model = VisitEntity
    fields = readonly_fields = (
        "datamart_id",
        "kind",
        "entity",
        "entity_type_raw",
        "pd",
        "pd_match",
        "partner",
        "rating",
        "rating_raw",
        "hact_q1",
        "hact_q1_from",
        "narrative_words",
        "narrative_placeholder",
    )
    verbose_name_plural = "monitored entities (finding rows)"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("pd", "partner")


class VisitActionPointInline(_ReadOnlyInline):
    model = VisitActionPoint
    fields = readonly_fields = ("action_point", "matched_by")
    verbose_name_plural = "action points raised from the visit"


class VisitRuleResultInline(_ReadOnlyInline):
    model = VisitRuleResult
    fields = readonly_fields = ("rule", "status", "points", "max_points", "detail_key", "detail", "measure")
    verbose_name_plural = "quality rules (pass, fail, na: not available, nap: does not apply, off)"


@admin.register(Visit)
class VisitAdmin(ReadOnlyModelAdmin):
    """The visits the refresh built, for checking the data: links, place, data problems."""

    list_display = (
        "label",
        "end_date",
        "status_shown",
        "rating_shown",
        "partner",
        "entities",
        "place_name",
        "governorate_name",
    )
    list_filter = ("status_group", "rating", "located_by", "sections_from", "is_programmatic")
    search_fields = ("key", "reference", "reference_number", "partner__name", "partner__short_name")
    list_select_related = ("partner",)
    view_on_site = False
    inlines = (VisitRuleResultInline, VisitEntityInline, VisitActionPointInline)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "key",
                    "label",
                    "activity_id",
                    "reference",
                    "reference_number",
                    ("start_date", "end_date", "last_modified"),
                    ("status", "status_raw", "status_group"),
                    ("rating", "rating_counts"),
                    ("is_programmatic", "is_remote"),
                )
            },
        ),
        (
            "Links",
            {
                "fields": (
                    "partner",
                    "partner_ids",
                    "pd",
                    "pd_ids",
                    "pd_numbers",
                    "cp_outputs",
                    "programme_activities",
                    ("section_names", "sections_from", "section_ids"),
                    ("offices", "offices_from"),
                    ("team", "team_unnamed"),
                )
            },
        ),
        (
            "Place",
            {
                "fields": (
                    ("place_name", "place_pcode"),
                    ("location", "site"),
                    ("governorate_name", "governorate_key", "district_name"),
                    ("latitude", "longitude"),
                    ("located_by", "located_level", "point_precise"),
                    ("approximate", "approximate_from"),
                )
            },
        ),
        (
            "Answers, action points and scores",
            {
                "fields": (
                    ("questions_asked", "questions_answered"),
                    (
                        "action_points",
                        "action_points_open",
                        "action_points_overdue",
                        "action_points_high_open",
                    ),
                    ("hact_q1", "psea_flag"),
                    ("quality_score", "quality_points", "quality_max", "score_band"),
                    ("evaluated_rules", "flags", "not_scored_reason"),
                    ("urgency", "urgency_band", "urgency_parts"),
                    ("rules_version", "refreshed_at"),
                )
            },
        ),
        ("Data problems", {"fields": ("issues",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Visit._meta.concrete_fields]

    @admin.display(description=_("status"), ordering="status_group")
    def status_shown(self, obj):
        text = (obj.status or obj.status_raw or _("unknown")).replace("_", " ").capitalize()
        return badge(text, GROUP_TONES.get(obj.status_group, "warn"))

    @admin.display(description=_("rating"), ordering="rating")
    def rating_shown(self, obj):
        return badge(obj.rating.replace("_", " ").capitalize(), RATING_TONES.get(obj.rating))


RATING_TONES = {"on_track": "ok", "constrained": "warn", "off_track": "bad", "not_monitored": "muted"}
GROUP_TONES = {"reported": "ok", "in_progress": "info", "planned": "muted", "cancelled": "muted"}


@admin.register(VisitReview)
class VisitReviewAdmin(ReadOnlyModelAdmin):
    """The marks sections put on visits (reviewed, needs follow-up, data issue)."""

    list_display = ("visit_key", "status", "reviewed_by", "created_at")
    list_select_related = ("reviewed_by",)
    list_filter = ("status",)
    search_fields = ("visit_key",)
    readonly_fields = ("visit_key", "status", "note", "reviewed_by", "created_at")


# ------------------------------------------------------------------------------------------ prompts
STATUS_TONES = {
    PromptVersion.Status.DRAFT: "info",
    PromptVersion.Status.PUBLISHED: "ok",
    PromptVersion.Status.RETIRED: "muted",
}
PROMPT_INFO = (
    "status",
    "number",
    "based_on",
    "restored_from",
    "content_hash",
    "created_by_name",
    "created_at",
    "published_by_name",
    "published_at",
)


class PromptVersionForm(forms.ModelForm):
    """A draft's content. Blocks an effort the assistant does not know and any e-mail address, link or
    known person name in the editable texts (prompts name roles and sections, never people)."""

    effort = forms.ChoiceField(
        choices=[(e, e) for e in settings.AI_ASSISTANT_EFFORTS],
        help_text=_("How much the model reasons before it writes; higher costs more output tokens."),
    )

    class Meta:
        model = PromptVersion
        fields = (*PromptVersion.CONTENT_FIELDS, "note")
        widgets = {
            "instructions": forms.Textarea(attrs={"rows": 14}),
            "chat_instructions": forms.Textarea(attrs={"rows": 8}),
        }
        help_texts = {
            "max_output_tokens": _(
                "Output tokens of one brief. Max output tokens include reasoning tokens: below 2,500 at "
                "effort medium or higher the brief may be cut off."
            ),
            "chat_max_output_tokens": _(
                "Output tokens of each model call of a chat answer, reasoning included."
            ),
            "temperature": _(
                "Empty: not sent. Temperature/top-p are sent only when the model accepts them (Sampling "
                "checks); OpenAI advises setting one of the two only."
            ),
            "top_p": _("Empty: not sent (the API's default is 1.00)."),
            "model": _("Empty: the default model (FMM_MODEL)."),
            "note": _("Required: what changed and why. Kept with the version, with your name and the time."),
        }

    def clean(self):
        cleaned = super().clean()
        effort = cleaned.get("effort")
        if effort and effort not in settings.AI_ASSISTANT_EFFORTS:
            self.add_error(
                "effort",
                _("Choose one of %(efforts)s.") % {"efforts": ", ".join(settings.AI_ASSISTANT_EFFORTS)},
            )
        known = privacy.names()
        for name in ("instructions", "chat_instructions"):
            for problem in profiles.text_problems(cleaned.get(name) or "", known):
                self.add_error(name, problem)
        return cleaned


def _preview_scope(choice: str = "", url: str = ""):
    """The filter a Preview is shown for: a pasted ``/fmm/?…`` address, else a section, else the whole
    country this year (the scope of someone who chose "All sections")."""
    from urllib.parse import urlsplit

    from django.http import QueryDict

    from .scope import Scope

    if url.strip():
        params = QueryDict(urlsplit(url.strip()).query)
    elif choice.startswith("section:"):
        params = QueryDict(mutable=True)
        params["section"] = choice.split(":", 1)[1]
    else:
        params = QueryDict("section=")
    return Scope.from_params(params, None)


@admin.register(PromptVersion)
class PromptVersionAdmin(ModelAdmin):
    """The prompt versions of the AI brief and the chat. Administrators add drafts (prefilled from the
    published version, or ``?from=<pk>``), edit them, preview what each call sends, publish one and roll
    back to an older one; published and retired versions are read-only, and only drafts can be deleted
    (with their test runs). Other staff read."""

    form = PromptVersionForm
    change_form_template = "admin/fmm/promptversion/change_form.html"
    list_display = (
        "number",
        "status_shown",
        "note",
        "created_by_name",
        "created_at",
        "published_by_name",
        "published_at",
        "hash_shown",
    )
    list_filter = ("status",)
    search_fields = ("note", "created_by_name", "published_by_name")
    ordering = ("-number",)
    actions_detail = ("preview_version", "publish_version", "roll_back_version")
    fieldsets = (
        (_("AI monitoring insights"), {"fields": ("insights_enabled", "instructions")}),
        (_("Chat with Data"), {"fields": ("chat_enabled", "chat_instructions")}),
        (
            _("Model and parameters"),
            {
                "fields": (
                    "model",
                    "effort",
                    "max_output_tokens",
                    "chat_max_output_tokens",
                    "temperature",
                    "top_p",
                )
            },
        ),
        (_("Data sent (narr, comp)"), {"fields": ("narratives_sampled", "comparison_visits")}),
        (
            _("Limits"),
            {
                "fields": (
                    "insights_per_user_per_day",
                    "chat_per_user_per_day",
                    "chat_max_rounds",
                    "chat_time_limit",
                )
            },
        ),
        (_("Version"), {"fields": ("note", *PROMPT_INFO)}),
        (_("Fixed safety text"), {"fields": ("fixed_text",), "classes": ("nd-fixed",)}),
    )

    # permissions: Administrators change drafts only, and delete drafts only
    def has_add_permission(self, request):
        return access.is_admin(request.user)

    def _draft(self, obj) -> bool:
        if obj is not None and not isinstance(obj, PromptVersion):
            obj = PromptVersion.objects.filter(pk=obj).first()
        return obj is None or obj.status == PromptVersion.Status.DRAFT

    def has_change_permission(self, request, obj=None):
        return access.is_admin(request.user) and self._draft(obj)

    def has_delete_permission(self, request, obj=None):
        return access.is_admin(request.user) and self._draft(obj)

    def has_publish_permission(self, request, obj=None):
        return access.is_admin(request.user) and obj is not None and self._draft(obj)

    def has_roll_back_permission(self, request, obj=None):
        return access.is_admin(request.user) and obj is not None and not self._draft(obj)

    def has_preview_permission(self, request, obj=None):
        return access.is_admin(request.user)

    def get_readonly_fields(self, request, obj=None):
        return (*PROMPT_INFO, "fixed_text")

    # columns
    @admin.display(description=_("status"), ordering="status")
    def status_shown(self, obj):
        return badge(obj.get_status_display(), STATUS_TONES.get(obj.status, "muted"))

    @admin.display(description=_("content hash"))
    def hash_shown(self, obj):
        return (obj.content_hash or "")[:10]

    @admin.display(description=_("added to every prompt, after the text above"))
    def fixed_text(self, obj):
        return render_to_string("admin/fmm/promptversion/_fixed_text.html", _fixed_context())

    # adding a draft
    def _source(self, request) -> PromptVersion | None:
        pk = request.POST.get("from_version") or request.GET.get("from")
        if pk and str(pk).isdigit():
            found = PromptVersion.objects.filter(pk=int(pk)).first()
            if found is not None:
                return found
        return profiles.published() or PromptVersion.objects.order_by("-number").first()

    def get_changeform_initial_data(self, request):
        source = self._source(request)
        if source is None:
            return super().get_changeform_initial_data(request)
        return {name: getattr(source, name) for name in PromptVersion.CONTENT_FIELDS}

    def add_view(self, request, form_url="", extra_context=None):
        source = self._source(request)
        if source is None:
            messages.error(request, _("There is no prompt version to start a draft from."))
            return redirect("admin:fmm_promptversion_changelist")
        extra = {**(extra_context or {}), "from_version": source}
        return super().add_view(request, form_url, extra)

    def save_model(self, request, obj, form, change):
        if not change:
            source = self._source(request)
            with transaction.atomic():
                profile = PromptProfile.objects.select_for_update().get(pk=source.profile_id)
                obj.profile = profile
                obj.number = profiles.next_number(profile)
                obj.status = PromptVersion.Status.DRAFT
                obj.based_on = source
                obj.created_by = request.user
                obj.created_by_name = profiles.user_name(request.user)
                obj.save()
        else:
            super().save_model(request, obj, form, change)
        for warning in profiles.warnings(obj):
            messages.warning(request, warning)

    # deleting drafts (with their test runs)
    def delete_model(self, request, obj):
        profiles.delete_draft(obj, request.user)

    def delete_queryset(self, request, queryset):
        kept = 0
        for version in queryset:
            if version.status == PromptVersion.Status.DRAFT:
                profiles.delete_draft(version, request.user)
            else:
                kept += 1
        if kept:
            messages.warning(
                request, _("Published and retired versions are never deleted: %(n)s kept.") % {"n": kept}
            )

    def get_deleted_objects(self, objs, request):
        """What deleting drafts removes: each draft and its test runs (the only briefs a draft has).
        Published and retired versions are never deleted, so their briefs are never listed."""
        listed, counts = [], {}
        for version in objs:
            if version.status != PromptVersion.Status.DRAFT:  # kept by delete_queryset
                listed.append(
                    f"{version} "
                    + _("(%(status)s: kept, never deleted)")
                    % {"status": version.get_status_display().lower()}
                )
                continue
            runs = profiles.test_runs(version)
            line = str(version)
            if runs:
                line += " " + (
                    _("(this also deletes its test run)")
                    if runs == 1
                    else _("(this also deletes its %(n)s test runs)") % {"n": runs}
                )
            listed.append(line)
            counts[str(PromptVersion._meta.verbose_name_plural)] = (
                counts.get(str(PromptVersion._meta.verbose_name_plural), 0) + 1
            )
            if runs:
                counts[_("test runs")] = counts.get(_("test runs"), 0) + runs
        return listed, counts, set(), []

    # the detail actions and their pages
    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("<int:pk>/preview/", view(self.preview_view), name="fmm_promptversion_preview"),
            path("<int:pk>/publish/", view(self.publish_view), name="fmm_promptversion_publish"),
            path("<int:pk>/roll-back/", view(self.roll_back_view), name="fmm_promptversion_roll_back"),
            *super().get_urls(),
        ]

    @action(description=_("Preview"), url_path="preview-version", icon="preview", permissions=["preview"])
    def preview_version(self, request, object_id):
        return redirect("admin:fmm_promptversion_preview", object_id)

    @action(description=_("Publish"), url_path="publish-version", icon="publish", permissions=["publish"])
    def publish_version(self, request, object_id):
        return redirect("admin:fmm_promptversion_publish", object_id)

    @action(
        description=_("Roll back to this version"),
        url_path="roll-back-version",
        icon="history",
        permissions=["roll_back"],
    )
    def roll_back_version(self, request, object_id):
        return redirect("admin:fmm_promptversion_roll_back", object_id)

    def _context(self, request, version, title: str) -> dict[str, Any]:
        return {
            **self.admin_site.each_context(request),
            "title": title,
            "version": version,
            "opts": self.model._meta,
            "current": profiles.published(),
        }

    def preview_view(self, request, pk):
        """Exactly what each call of this version sends as its instructions, for a chosen filter: the
        brief's and the chat's (with the date and scope lines). No AI call; nothing is saved."""
        if not access.is_admin(request.user):
            raise Http404
        version = get_object_or_404(PromptVersion, pk=pk)
        choice = request.GET.get("scope", "")
        url = request.GET.get("url", "")
        scope = _preview_scope(choice, url)
        from .scope import options

        context = {
            **self._context(request, version, _("Preview prompt v%(n)s") % {"n": version.number}),
            "insights_text": profiles.compose(version, "insights"),
            "chat_text": profiles.chat_instructions(version, scope),
            "scope_label": scope.label(),
            "sections": options()["sections"],
            "choice": choice,
            "url": url,
            "model": profiles.model_of(version),
            "warnings": profiles.warnings(version),
        }
        return render(request, "admin/fmm/promptversion/preview.html", context)

    def publish_view(self, request, pk):
        """Confirm publishing a draft (Administrators only), then publish it."""
        if not access.is_admin(request.user):
            raise Http404
        version = get_object_or_404(PromptVersion, pk=pk)
        if version.status != PromptVersion.Status.DRAFT:
            messages.error(request, _("Only a draft can be published."))
            return redirect("admin:fmm_promptversion_change", version.pk)
        if request.method == "POST":
            try:
                profiles.publish(version, request.user)
            except ValueError:  # published or retired by someone else since the page was opened
                messages.error(request, _("Only a draft can be published."))
                return redirect("admin:fmm_promptversion_change", version.pk)
            messages.success(request, _("Prompt v%(n)s is published.") % {"n": version.number})
            return redirect("admin:fmm_promptversion_change", version.pk)
        return render(
            request,
            "admin/fmm/promptversion/publish_confirm.html",
            self._context(request, version, _("Publish prompt v%(n)s") % {"n": version.number}),
        )

    def roll_back_view(self, request, pk):
        """Confirm a rollback with a note (Administrators only), then publish a copy of the version."""
        if not access.is_admin(request.user):
            raise Http404
        version = get_object_or_404(PromptVersion, pk=pk)
        if version.status == PromptVersion.Status.DRAFT:
            messages.error(request, _("A draft is published, not rolled back to."))
            return redirect("admin:fmm_promptversion_change", version.pk)
        context = self._context(request, version, _("Roll back to prompt v%(n)s") % {"n": version.number})
        if request.method == "POST":
            note = request.POST.get("note", "").strip()
            if note:
                new = profiles.roll_back(version, request.user, note)
                messages.success(
                    request,
                    _("Prompt v%(new)s is published, a copy of v%(old)s.")
                    % {"new": new.number, "old": version.number},
                )
                return redirect("admin:fmm_promptversion_change", new.pk)
            context["error"] = _("Write why you roll back: the note is kept with the new version.")
        return render(request, "admin/fmm/promptversion/rollback_confirm.html", context)


def _fixed_context() -> dict[str, Any]:
    from .ai import insights, prompts

    return {
        "insights": prompts.fixed_text("insights"),
        "chat": prompts.fixed_text("chat"),
        "schema": json.dumps(insights.SCHEMA, indent=2, ensure_ascii=False),
    }


@admin.register(ModelCapability)
class ModelCapabilityAdmin(ModelAdmin):
    """Whether each model, at each effort, accepted temperature and top-p when last sent. Kept by the AI
    calls themselves; deleting a row (Administrators) means "check again on the next call"."""

    list_display = ("model", "effort", "parameter", "accepted_shown", "checked_at", "detail")
    list_filter = ("parameter", "accepted")
    search_fields = ("model",)
    readonly_fields = ("model", "effort", "parameter", "accepted", "checked_at", "detail")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return access.is_admin(request.user)

    @admin.display(description=_("accepted"), ordering="accepted")
    def accepted_shown(self, obj):
        if obj.accepted is None:
            return badge(_("not known"), "muted")
        return badge(_("accepted"), "ok") if obj.accepted else badge(_("refused"), "bad")
