"""What changed between two builds of the hub: new and gone things, links that appeared or went, and
facts or headline figures that moved. Whatever the source (today's or one added later), anything that
reaches the hub is noticed here; nothing is source-specific.

A change is *notable* when people would want to hear about it: a new partner, programme document,
donor or document, a status that changed, a figure that moved by 10% or more, a programme document
newly funded by a donor. The rest (a new district in the gazetteer, a document newly mentioning a
place) is kept and searchable, but stays out of the daily note.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .models import Change, Entity
from .query import RELATION_LABELS

K = Entity.Kind
Ref = tuple[str, str]
Edge = tuple[Ref, str, Ref]

NOTABLE_ADDED = {
    K.PARTNER,
    K.PROGRAMME,
    K.DONOR,
    K.GRANT,
    K.DATABASE,
    K.REPORT,
    K.CPD_CYCLE,
    K.CPD_OUTCOME,
    K.CPD_OUTPUT,
    K.CPD_INDICATOR,
    K.YOUTH_INDICATOR,
    K.EDUCATION_PROGRAMME,
    K.CENTRE,
    K.DOCUMENT,
    K.MAP,
}
NOTABLE_REMOVED = {K.PARTNER, K.PROGRAMME, K.GRANT, K.DATABASE, K.CPD_INDICATOR, K.CENTRE, K.DOCUMENT}
NOTABLE_LINKS = {
    "implemented_by",
    "funded_by",
    "has_grant",
    "contributes_to",
    "reports_in",
    "linked_to",
    "run_by",
}
IGNORED_FIELDS = {K.FINDING: {"date"}}  # a finding carries the date of the review that raised it
RELATIVE = 0.10  # a figure "moved" when it changed by at least 10%…
MINIMUM = {"budget": 1000, "disbursed": 1000, "outstanding": 1000, "reports": 5}  # …and at least this much


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def field_moved(field: str, before: Any, after: Any) -> bool:
    """Is this change worth telling people? Numbers: by 10% and a minimum; anything else: always."""
    b, a = _number(before), _number(after)
    if b is not None and a is not None:
        return abs(a - b) >= max(abs(b) * RELATIVE, MINIMUM.get(field, 1))
    return before != after


def _is_critical(data: dict[str, Any]) -> bool:
    return (data.get("attrs") or {}).get("severity") == "critical"


class _Sections:
    """The sections a thing concerns: its own section links, else those of what it is linked to (a
    partner through its programme documents)."""

    def __init__(self, edges: set[Edge]) -> None:
        self.direct: dict[Ref, set[int]] = defaultdict(set)
        self.near: dict[Ref, set[Ref]] = defaultdict(set)
        for source, relation, target in edges:
            if relation == "in_section" and target[0] == K.SECTION and target[1].isdigit():
                self.direct[source].add(int(target[1]))
            else:
                self.near[source].add(target)
                self.near[target].add(source)

    def of(self, ref: Ref) -> list[int]:
        if ref[0] == K.SECTION:
            return [int(ref[1])] if ref[1].isdigit() else []
        own = self.direct.get(ref)
        if own:
            return sorted(own)
        found: set[int] = set()
        for other in self.near.get(ref, ()):
            found |= self.direct.get(other, set())
        return sorted(found)[:20]


def detect(
    *,
    before: dict[Ref, dict[str, Any]],
    gone: set[Ref],
    collector: Any,
    old_edges: set[Edge],
    new_edges: set[Edge],
    ids: dict[Ref, int],
    now: datetime,
    run: Any = None,
) -> list[Change]:
    sections = _Sections(old_edges | new_edges)
    out: list[Change] = []

    def change(ref: Ref, name: str, url: str, op: str, notable: bool, **extra: Any) -> None:
        out.append(
            Change(
                detected_at=now,
                run=run,
                entity_id=ids.get(ref),
                kind=ref[0],
                key=ref[1],
                name=name[:500],
                url=url[:300],
                op=op,
                sections=sections.of(ref),
                notable=notable,
                **extra,
            )
        )

    for ref, data in collector.entities.items():
        old = before.get(ref)
        if old is None:
            notable = ref[0] in NOTABLE_ADDED or (ref[0] == K.FINDING and _is_critical(data))
            change(ref, data["name"], data["url"], Change.Op.ADDED, notable)
            continue
        ignored = IGNORED_FIELDS.get(ref[0], set())
        then = {**(old["attrs"] or {}), **(old["snapshot"] or {})}
        now_ = {**data["attrs"], **data["snapshot"]}
        if not old["snapshot"]:  # its figures are recorded for the first time: nothing to compare yet
            ignored = ignored | set(data["snapshot"])
        fields = {
            f: [then.get(f), now_.get(f)]
            for f in sorted(set(then) | set(now_))
            if f not in ignored and then.get(f) != now_.get(f)
        }
        if old["name"] != data["name"]:
            fields["name"] = [old["name"], data["name"]]
        if fields:
            notable = any(field_moved(f, b, a) for f, (b, a) in fields.items())
            change(ref, data["name"], data["url"], Change.Op.CHANGED, notable, fields=fields)
    for ref in gone:
        old = before[ref]
        notable = ref[0] in NOTABLE_REMOVED or (ref[0] == K.FINDING and _is_critical(old))
        change(ref, old["name"], "", Change.Op.REMOVED, notable)

    names = {ref: d["name"] for ref, d in collector.entities.items()}
    names.update({ref: d["name"] for ref, d in before.items() if ref not in names})
    urls = {ref: d["url"] for ref, d in collector.entities.items()}
    for edges, op in ((new_edges - old_edges, Change.Op.LINKED), (old_edges - new_edges, Change.Op.UNLINKED)):
        for source, relation, target in edges:
            if source not in before or source in gone or (op == Change.Op.UNLINKED and target in gone):
                continue  # told with the news that it is new or gone (a link to a new donor is told)
            label = RELATION_LABELS.get(relation, (relation, relation))[0]
            change(
                source,
                names.get(source, source[1]),
                urls.get(source, ""),
                op,
                relation in NOTABLE_LINKS,
                link={
                    "relation": label,
                    "kind": target[0],
                    "key": target[1],
                    "name": names.get(target, target[1]),
                    "url": urls.get(target, ""),
                },
            )
    return out
