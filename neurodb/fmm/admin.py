"""Monitoring insights in the admin, read-only for now:

- **Fields found**: which keys the eTools field monitoring records hold and which key each field is read
  from, as the last refresh found them, with the rates that tell whether the data can be trusted.
  Pinning a key (an override, versioned with the quality rules) arrives with the rules;
- **Visits**: the visits the refresh built, with their entity rows, action points and data problems,
  for checking the data;
- **Visit reviews**: the marks sections put on visits."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext as _
from unfold.admin import ModelAdmin, TabularInline

from neurodb.core.models import SyncRun
from neurodb.integrations import background
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import fields, status
from .models import FieldMapping, KeyProbe, Visit, VisitActionPoint, VisitEntity, VisitReview

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
    if unanswered == 0 and total >= R2_MIN_RECORDS:
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


@admin.register(FieldMapping)
class FieldMappingAdmin(ModelAdmin):
    """Fields found: the keys the eTools field monitoring records hold, the key each field is read
    from and why, and the rates of the last reading. Read-only for now."""

    change_list_template = "admin/fmm/fieldmapping/change_list.html"
    list_display = ("dataset", "field", "chosen_key", "state", "coverage")
    list_filter = ("dataset", "state")
    search_fields = ("field", "chosen_key")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "found": fields_found()}
        return super().changelist_view(request, extra_context)


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
    inlines = (VisitEntityInline, VisitActionPointInline)
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
