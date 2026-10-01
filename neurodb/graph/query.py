"""Reading the hub: find anything by name, code or number; everything linked to one thing; what can be
reached from it within two links."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import Q

from .models import Edge, Entity

RELATION_LABELS = {  # source <relation> target, and how it reads from the target's side
    "implemented_by": ("implemented by", "implements"),
    "funded_by": ("funded by", "funds"),
    "has_grant": ("has grant", "grant of"),
    "funds_through": ("funds through grant", "grant funding"),
    "in_section": ("in section", "covers"),
    "takes_place_in": ("takes place in", "place of"),
    "part_of": ("part of", "includes"),
    "measures": ("measures", "measured by"),
    "reports_in": ("reports in", "has reports from"),
    "contributes_to": ("contributes to", "has contributions from"),
    "linked_to": ("linked to", "linked from"),
    "mentions": ("mentions", "mentioned in"),
    "located_in": ("located in", "has"),
    "run_by": ("run by", "runs"),
    "about": ("about", "subject of"),
    "same_as": ("same as", "same as"),
    "includes": ("includes", "included in"),
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[\w][\w/.\-]*", text.lower()) if len(w) >= 2][:12]


def find(text: str, kinds: list[str] | None = None, limit: int = 20) -> list[Entity]:
    """Things whose name, code, number or alias matches: exact keys and codes first, then every word,
    then any part of the name."""
    text = " ".join(text.split())[:200]
    if not text:
        return []
    qs = Entity.objects.all()
    if kinds:
        qs = qs.filter(kind__in=kinds)
    found: dict[int, Entity] = {}
    for entity in qs.filter(Q(key__iexact=text) | Q(name__iexact=text) | Q(aliases__icontains=text))[:limit]:
        found[entity.pk] = entity
    if len(found) < limit:
        query = SearchQuery(text, search_type="websearch", config="simple") | SearchQuery(
            text, search_type="websearch", config="english"
        )
        for entity in (
            qs.filter(search_vector=query)
            .exclude(pk__in=found)
            .annotate(rank=SearchRank("search_vector", query))
            .order_by("-rank", "name")[: limit - len(found)]
        ):
            found[entity.pk] = entity
    if len(found) < limit and _words(text):
        condition = Q()
        for word in _words(text):
            condition &= Q(name__icontains=word) | Q(aliases__icontains=word)
        for entity in qs.filter(condition).exclude(pk__in=found).order_by("name")[: limit - len(found)]:
            found[entity.pk] = entity
    return list(found.values())


def card(entity: Entity) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": entity.kind, "key": entity.key, "name": entity.name}
    if entity.url:
        out["url"] = entity.url
    if entity.attrs:
        out["facts"] = entity.attrs
    if entity.lookup:
        out["lookup"] = entity.lookup
    return out


def neighbours(entity: Entity, per_group: int = 25) -> list[dict[str, Any]]:
    """Everything one link away, grouped by how it is linked and what it is (largest groups first)."""
    groups: dict[tuple[str, str], list[tuple[float, Entity]]] = defaultdict(list)
    for edge in Edge.objects.filter(source=entity).select_related("target"):
        label = RELATION_LABELS.get(edge.relation, (edge.relation, edge.relation))[0]
        groups[(label, edge.target.kind)].append((edge.weight, edge.target))
    for edge in Edge.objects.filter(target=entity).select_related("source"):
        label = RELATION_LABELS.get(edge.relation, (edge.relation, edge.relation))[1]
        groups[(label, edge.source.kind)].append((edge.weight, edge.source))
    kinds = dict(Entity.Kind.choices)
    out = []
    for (label, kind), items in sorted(groups.items(), key=lambda g: -len(g[1])):
        items.sort(key=lambda i: (-i[0], i[1].name))
        out.append(
            {
                "link": label,
                "kind": kinds.get(kind, kind),
                "count": len(items),
                "items": [dict(card(e), weight=w) if w != 1 else card(e) for w, e in items[:per_group]],
                "truncated": len(items) > per_group,
            }
        )
    return out


def reach(entity: Entity, kind: str, limit: int = 40) -> list[dict[str, Any]]:
    """Things of ``kind`` one or two links away, with what connects them (e.g. the donors of the
    programme documents that take place in a governorate)."""

    def linked(ids: set[int]) -> dict[int, set[int]]:
        out: dict[int, set[int]] = defaultdict(set)
        for s, t in Edge.objects.filter(source_id__in=ids).values_list("source_id", "target_id"):
            out[s].add(t)
        for s, t in Edge.objects.filter(target_id__in=ids).values_list("source_id", "target_id"):
            out[t].add(s)
        return out

    first = linked({entity.pk}).get(entity.pk, set())
    found: dict[int, set[int]] = defaultdict(set)  # reached id -> the ids it was reached through
    kinds = dict(Entity.objects.filter(pk__in=first).values_list("pk", "kind"))
    for pk, k in kinds.items():
        if k == kind:
            found[pk].add(entity.pk)
    if len(first) <= 5000:
        second = linked(set(first))
        candidates = {t for targets in second.values() for t in targets} - {entity.pk}
        target_kinds = dict(Entity.objects.filter(pk__in=candidates, kind=kind).values_list("pk", "kind"))
        for via, targets in second.items():
            for t in targets:
                if t in target_kinds:
                    found[t].add(via)
    names = {
        e.pk: e for e in Entity.objects.filter(pk__in=set(found) | {v for vs in found.values() for v in vs})
    }
    rows = []
    for pk, via in sorted(found.items(), key=lambda f: -len(f[1])):
        entry = card(names[pk])
        direct = entity.pk in via
        entry["connection"] = "direct" if direct else f"through {len(via)} linked item(s)"
        if not direct:
            entry["through"] = [names[v].name for v in sorted(via, key=lambda v: names[v].name)[:5]]
        rows.append(entry)
    return rows[:limit]
