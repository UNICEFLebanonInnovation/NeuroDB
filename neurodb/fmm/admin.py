"""Monitoring insights in the admin. For now **Fields found**: which keys the eTools field monitoring
records hold and which key each field is read from, as the last refresh found them, with the rates
that tell whether the data can be trusted. Read-only: pinning a key (an override, versioned with the
quality rules) arrives with the rules."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext as _
from unfold.admin import ModelAdmin

from neurodb.core.models import SyncRun
from neurodb.integrations import background
from neurodb.web.admin_helpers import badge

from . import fields, status
from .models import FieldMapping, KeyProbe

STATE_TONES = {
    FieldMapping.State.FOUND: "ok",
    FieldMapping.State.OVERRIDE: "info",
    FieldMapping.State.AMBIGUOUS: "warn",
    FieldMapping.State.OVERRIDE_MISSING: "bad",
    FieldMapping.State.MISSING: "muted",
}
PD_TARGET = 0.9  # the share of programme document rows that should be linked (go-live checklist)
R2_MIN_RECORDS = 200  # below this, no answers left blank says nothing about the export


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
        "rates": _rates(probe_run),
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
