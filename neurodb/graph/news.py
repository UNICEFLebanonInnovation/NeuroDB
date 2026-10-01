"""Reading what changed: for the assistant (``whats_new``), the What's new page, the overview card and
the daily note. Every change is told the same way everywhere."""

from __future__ import annotations

import datetime
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from .models import Change, Edge, Entity

FIELD_LABELS = {
    "status": "status",
    "end": "end date",
    "start": "start date",
    "budget": "budget",
    "disbursed": "disbursed",
    "outstanding": "outstanding",
    "risk_rating": "risk rating",
    "reports": "activity reports",
    "latest_month": "latest month reported",
    "value": "value",
    "achieved_pct": "achieved (%)",
    "severity": "severity",
    "state": "state",
    "expiry": "expiry",
    "month": "latest month",
    "name": "name",
}


def label(field: str) -> str:
    return FIELD_LABELS.get(field, field.replace("_", " "))


def _value(value: Any) -> str:
    if value is None or value == "":
        return "none"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def sentence(change: Change) -> str:
    """One line: "New programme document: …", "X: status active → ended", "X is now funded by Y"."""
    kind = change.get_kind_display()
    if change.op == Change.Op.ADDED:
        return f"New {kind.lower()}: {change.name}"
    if change.op == Change.Op.REMOVED:
        return f"{kind} no longer in NeuroDB: {change.name}"
    if change.op in (Change.Op.LINKED, Change.Op.UNLINKED):
        link = change.link or {}
        verb = "now" if change.op == Change.Op.LINKED else "no longer"
        return f"{change.name} is {verb} {link.get('relation', 'linked to')} {link.get('name', '')}".strip()
    parts = [
        f"{label(f)} {_value(before)} → {_value(after)}"
        for f, (before, after) in (change.fields or {}).items()
    ]
    return f"{change.name}: {'; '.join(parts[:6])}"


def recent(
    since: datetime.datetime,
    *,
    sections: list[int] | None = None,
    kinds: list[str] | None = None,
    notable_only: bool = True,
    entity_ids: list[int] | None = None,
) -> QuerySet[Change]:
    qs = Change.objects.filter(detected_at__gte=since)
    if notable_only:
        qs = qs.filter(notable=True)
    if sections:
        qs = qs.filter(sections__overlap=sections)
    if kinds:
        qs = qs.filter(kind__in=kinds)
    if entity_ids is not None:
        qs = qs.filter(entity_id__in=entity_ids)
    return qs


def around(entity_ids: list[int], limit: int = 500) -> list[int]:
    """The things and everything linked to them (a partner: its PDs, centres, documents…)."""
    linked = set(entity_ids)
    for s, t in Edge.objects.filter(Q(source_id__in=entity_ids) | Q(target_id__in=entity_ids)).values_list(
        "source_id", "target_id"
    )[:limit]:
        linked.update((s, t))
    return sorted(linked)


def as_dict(change: Change) -> dict[str, Any]:
    out: dict[str, Any] = {
        "when": timezone.localtime(change.detected_at).strftime("%Y-%m-%d %H:%M"),
        "what": change.get_op_display(),
        "kind": change.get_kind_display(),
        "key": change.key,
        "name": change.name,
        "says": sentence(change),
    }
    if change.fields:
        out["fields"] = {label(f): {"from": b, "to": a} for f, (b, a) in change.fields.items()}
    if change.link:
        out["link"] = change.link
    if change.url:
        out["url"] = change.url
    if change.entity_id and change.entity and change.entity.lookup:
        out["lookup"] = change.entity.lookup
    if not change.notable:
        out["minor"] = True
    return out


def totals(qs: QuerySet[Change]) -> dict[str, dict[str, int]]:
    kinds, ops = dict(Entity.Kind.choices), dict(Change.Op.choices)
    out: dict[str, dict[str, int]] = {}
    for kind, op in qs.values_list("kind", "op"):
        group = out.setdefault(kinds.get(kind, kind), {})
        group[ops.get(op, op)] = group.get(ops.get(op, op), 0) + 1
    return out


def last_build() -> datetime.datetime | None:
    from neurodb.core.models import SyncRun

    run = SyncRun.last_success(SyncRun.Job.KNOWLEDGE_HUB)
    return run.finished_at if run else None
