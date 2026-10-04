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
    source: str = ""  # the builder running now: what it adds is kept as is when it fails next time

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
        snapshot: dict[str, Any] | None = None,
    ) -> Ref:
        ref = (kind, str(key)[:120])
        name = norm(name)[:500] or str(key)
        current = self.entities.get(ref)
        alias_set = {norm(a) for a in aliases if norm(a) and norm(a) != name}
        if current:  # the same thing seen by two sources: keep the first name, merge the rest
            current["aliases"] |= alias_set
            current["attrs"].update(attrs or {})
            current["snapshot"].update(snapshot or {})
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
                "snapshot": {k: v for k, v in (snapshot or {}).items() if v is not None},
                "builder": self.source,
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
            self.edges[key] = {"origin": origin, "weight": weight, "builder": self.source}


def write(collector: Collector, run: Any = None, failed: Iterable[str] = ()) -> dict[str, Any]:
    """Replace the hub with the collected entities and edges (edges whose ends are missing are dropped)
    and record what changed since the previous build.

    What a ``failed`` builder added last time is kept as it was: a source that could not be read is
    not "gone", and nothing of it is reported as changed. When a kind loses more than a fifth of its
    things with no error, those removals are recorded but not notable, and counted in ``suspect_removals``
    ({kind: how many went})."""
    from . import changes

    now = timezone.now()
    failed = set(failed)
    with transaction.atomic():
        existing = {(e.kind, e.key): e for e in Entity.objects.all()}
        before = {
            ref: {"name": e.name, "attrs": e.attrs, "snapshot": e.snapshot} for ref, e in existing.items()
        }
        old_edges = {
            ((sk, skey), rel, (tk, tkey)): b
            for sk, skey, rel, tk, tkey, b in Edge.objects.values_list(
                "source__kind", "source__key", "relation", "target__kind", "target__key", "builder"
            )
        }
        new, changed = [], []
        for (kind, key), data in collector.entities.items():
            values = {
                "name": data["name"],
                "aliases": "\n".join(sorted(data["aliases"]))[:20000],
                "description": data["description"],
                "url": data["url"][:300],
                "attrs": data["attrs"],
                "lookup": data["lookup"],
                "snapshot": data["snapshot"],
                "builder": data["builder"],
            }
            entity = existing.pop((kind, key), None)
            if entity is None:
                new.append(Entity(kind=kind, key=key, built_at=now, **values))
            else:
                for name, value in values.items():
                    setattr(entity, name, value)
                entity.built_at = now
                changed.append(entity)
        kept = {ref: e for ref, e in existing.items() if e.builder and e.builder in failed}
        gone = {ref: e for ref, e in existing.items() if ref not in kept}
        Entity.objects.filter(pk__in=[e.pk for e in gone.values()]).delete()
        Entity.objects.bulk_create(new, batch_size=2000)
        Entity.objects.bulk_update(
            changed,
            ["name", "aliases", "description", "url", "attrs", "lookup", "snapshot", "builder", "built_at"],
            batch_size=2000,
        )
        ids = {(k, key): pk for pk, k, key in Entity.objects.values_list("pk", "kind", "key")}
        edges_now = dict(collector.edges)
        kept_edges = set()  # the links of a failed source, and the links to what it added, stay
        for edge, builder in old_edges.items():
            source, _, target = edge
            if edge not in edges_now and (
                (builder and builder in failed) or source in kept or target in kept
            ):
                edges_now[edge] = {"origin": "", "weight": 1, "builder": builder}
                kept_edges.add(edge)
        Edge.objects.all().delete()
        edges = [
            Edge(
                source_id=ids[s],
                target_id=ids[t],
                relation=rel,
                origin=d["origin"],
                weight=d["weight"],
                builder=d.get("builder", ""),
            )
            for (s, rel, t), d in edges_now.items()
            if s in ids and t in ids
        ]
        Edge.objects.bulk_create(edges, batch_size=5000)
        Entity.objects.update(
            search_vector=SearchVector("name", weight="A", config="simple")
            + SearchVector("name", weight="A", config="english")
            + SearchVector("aliases", weight="B", config="simple")
            + SearchVector("description", weight="C", config="english")
        )
        found, suspect = [], {}
        if before:  # the first build is the starting point: nothing is "new" yet
            suspect = changes.suspect_removals(before, set(gone))
            found = changes.detect(
                before=before,
                gone=set(gone),
                collector=collector,
                old_edges=set(old_edges) - kept_edges,
                new_edges={e for e in collector.edges if e[0] in ids and e[2] in ids},
                ids=ids,
                now=now,
                run=run,
                suspect=suspect,
            )
            changes.Change.objects.bulk_create(found, batch_size=2000)
    return {
        "entities": len(ids),
        "edges": len(edges),
        "removed": len(gone),
        "kept_from_failed_sources": len(kept),
        "changes": len(found),
        "notable_changes": sum(1 for c in found if c.notable),
        "suspect_removals": suspect,
    }
