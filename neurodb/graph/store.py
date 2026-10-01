"""Writing the hub: entities and edges collected from every source replace the previous build."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.utils import timezone

from .models import Edge, Entity

Ref = tuple[str, str]  # (kind, key)


def norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def key_of(text: Any) -> str:
    """A key for things known only by name (donors): lower case, punctuation as spaces."""
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").lower()).split())[:120]


@dataclass
class Collector:
    entities: dict[Ref, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[Ref, str, Ref], dict[str, Any]] = field(default_factory=dict)

    def entity(
        self,
        kind: str,
        key: Any,
        name: Any,
        *,
        aliases: Iterable[Any] = (),
        description: Any = "",
        url: str = "",
        attrs: dict[str, Any] | None = None,
        lookup: dict[str, Any] | None = None,
    ) -> Ref:
        ref = (kind, str(key)[:120])
        name = norm(name)[:500] or str(key)
        current = self.entities.get(ref)
        alias_set = {norm(a) for a in aliases if norm(a) and norm(a) != name}
        if current:  # the same thing seen by two sources: keep the first name, merge the rest
            current["aliases"] |= alias_set
            current["attrs"].update(attrs or {})
            current["description"] = current["description"] or norm(description)
            current["url"] = current["url"] or url
            current["lookup"] = current["lookup"] or (lookup or {})
        else:
            self.entities[ref] = {
                "name": name,
                "aliases": alias_set,
                "description": norm(description)[:2000],
                "url": url,
                "attrs": dict(attrs or {}),
                "lookup": lookup or {},
            }
        return ref

    def edge(
        self, source: Ref | None, relation: str, target: Ref | None, origin: str, weight: float = 1
    ) -> None:
        if not source or not target or source == target:
            return
        key = (source, relation, target)
        if key in self.edges:
            self.edges[key]["weight"] += weight
        else:
            self.edges[key] = {"origin": origin, "weight": weight}


def write(collector: Collector) -> dict[str, int]:
    """Replace the hub with the collected entities and edges (edges whose ends are missing are dropped)."""
    now = timezone.now()
    with transaction.atomic():
        existing = {(e.kind, e.key): e for e in Entity.objects.all()}
        new, changed = [], []
        for (kind, key), data in collector.entities.items():
            values = {
                "name": data["name"],
                "aliases": "\n".join(sorted(data["aliases"]))[:20000],
                "description": data["description"],
                "url": data["url"][:300],
                "attrs": data["attrs"],
                "lookup": data["lookup"],
            }
            entity = existing.pop((kind, key), None)
            if entity is None:
                new.append(Entity(kind=kind, key=key, built_at=now, **values))
            else:
                for name, value in values.items():
                    setattr(entity, name, value)
                entity.built_at = now
                changed.append(entity)
        Entity.objects.filter(pk__in=[e.pk for e in existing.values()]).delete()
        Entity.objects.bulk_create(new, batch_size=2000)
        Entity.objects.bulk_update(
            changed, ["name", "aliases", "description", "url", "attrs", "lookup", "built_at"], batch_size=2000
        )
        ids = {(k, key): pk for pk, k, key in Entity.objects.values_list("pk", "kind", "key")}
        Edge.objects.all().delete()
        edges = [
            Edge(source_id=ids[s], target_id=ids[t], relation=rel, origin=d["origin"], weight=d["weight"])
            for (s, rel, t), d in collector.edges.items()
            if s in ids and t in ids
        ]
        Edge.objects.bulk_create(edges, batch_size=5000)
        Entity.objects.update(
            search_vector=SearchVector("name", weight="A", config="simple")
            + SearchVector("name", weight="A", config="english")
            + SearchVector("aliases", weight="B", config="simple")
            + SearchVector("description", weight="C", config="english")
        )
    return {"entities": len(ids), "edges": len(edges), "removed": len(existing)}
