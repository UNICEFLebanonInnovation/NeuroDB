"""How the things NeuroDB Watch follows connect, through the knowledge hub's real links.

Each open item names the hub thing it is about (``entity_kind``, ``entity_key``): a PD, a partner, a
grant, a master indicator. An item imported from the daily review names its finding, and the hub
already links the finding to the PDs and partners it is about ("about"). From there the watch goes
**one link** through the hub, by (kind, key) because the hub's row ids change at every build:

- a PD: its partner (implemented by), its grants (has grant) and its donors (funded by);
- a grant: the donors that fund through it (funds through);
- a master indicator: its ActivityInfo database (part of);
- a PD or a partner: the documents that mention it, read in the last 90 days (mentions).

Open items that share a partner or a grant form a **situation**, keyed ``partner:<pk>`` or
``grant:<name>``: "three open points meet on Partner X". A situation needs two open items or more, or
one item with a notable What's new change around it in the last 7 days or a recent document. An item
that could join several takes the largest (a partner before a grant when as large).

:func:`link` writes on each open item what it connects to (``related``, at most 8) and its situation
(``situation_key``), and returns the situations. :func:`changes_since` adds the notable What's new
changes around the open items to their stories, each once. :func:`situations_of` groups items into the
situations already stored, without reading the hub (for the page and the notes).

Pure code, no AI: only links the hub holds are followed, and nothing is written to the hub, the
knowledge base or Ask NeuroDB's data. Items never sent to the AI (system items and the administrators'
own) are not connected.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from neurodb.graph import news
from neurodb.graph.models import Change, Edge, Entity
from neurodb.knowledge.models import Document

from .models import RELATED_MAX, WatchItem
from .redact import refused

K = Entity.Kind
Ref = tuple[str, str]  # a hub thing: (kind, key)
OPEN = WatchItem.State.OPEN

DOCUMENT_DAYS = 90  # a document counts when it was read into the knowledge base this recently
CHANGE_DAYS = 7  # a notable What's new change counts around a situation for this many days
STORY_CHANGES_MAX = 5  # What's new lines added to one item's story in one pass (then "and N more")
SITUATION_CHANGES_MAX = 15  # changes kept on a situation (the note's writer reads at most 15)
SITUATION_KEY_MAX = 160  # WatchItem.situation_key
ITEM = "watch_item"  # the kind of a related entry that is another open item (not a hub kind)
ANCHORS = (K.PARTNER, K.GRANT)  # what a situation is keyed on, the first preferred when as large
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# One link through the hub: (relation, direction seen from the item's thing) -> the kinds kept at the
# other end. A partner's PDs and a grant's PDs are not followed: they are what the situation gathers.
OUT, IN = "out", "in"
HOPS: dict[tuple[str, str], frozenset[str]] = {
    ("implemented_by", OUT): frozenset({K.PARTNER}),  # a PD's partner
    ("has_grant", OUT): frozenset({K.GRANT}),
    ("funded_by", OUT): frozenset({K.DONOR}),
    ("funds_through", IN): frozenset({K.DONOR}),  # the donors funding through a grant
    ("funds_through", OUT): frozenset({K.GRANT}),
    ("mentions", IN): frozenset({K.DOCUMENT}),  # the documents mentioning a PD or a partner
    ("part_of", OUT): frozenset({K.DATABASE}),  # a master indicator's database
}
MENTIONS = {("mentions", IN): HOPS[("mentions", IN)]}
# The connected things whose changes go into an item's story, besides its own (a donor or a database
# is behind too many things for its news to belong to each of them)
STORY_KINDS = frozenset({K.PARTNER, K.PROGRAMME, K.GRANT, K.DOCUMENT})


# ---------------------------------------------------------------------------- a situation
@dataclass
class Situation:
    """Open items meeting on one partner or grant (the anchor), what they connect to and the notable
    What's new changes around them. ``related`` holds hub things as ``{kind, key, name, url}`` (with
    ``date``: a grant's expiry, a document's reading day): the anchor first, then the PDs and other
    things the items are about, the grants expiring soonest, the newest documents and the donors."""

    key: str  # partner:<pk> or grant:<name>
    kind: str  # partner or grant
    entity_key: str
    name: str
    url: str = ""
    items: list[WatchItem] = field(default_factory=list)
    related: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)  # read in the last 90 days
    changes: list[Change] = field(default_factory=list)  # notable, the last 7 days, newest first

    @property
    def item_keys(self) -> list[str]:
        return [item.key for item in self.items]

    @property
    def label(self) -> str:
        """The anchor in words: a partner's name, or "grant <name>"."""
        return f"grant {self.name}" if self.kind == K.GRANT else self.name

    @property
    def headline(self) -> str:
        """One line for the page: "3 open points meet on Partner X"."""
        count = len(self.items)
        if count >= 2:
            return f"{count} open points meet on {self.label}"
        extra = []
        if self.documents:
            extra.append("a recent document" if len(self.documents) == 1 else "recent documents")
        if self.changes:
            extra.append("a recent change" if len(self.changes) == 1 else "recent changes")
        return f"An open point on {self.label}" + (f", with {' and '.join(extra)}" if extra else "")

    @property
    def change_lines(self) -> list[str]:
        """The changes as What's new tells them."""
        return [news.sentence(change) for change in self.changes]

    def qualifies(self) -> bool:
        """Two open items or more, or one with a recent document or a notable change around it."""
        return len(self.items) >= 2 or (len(self.items) == 1 and bool(self.documents or self.changes))

    def restricted(self, keys: Iterable[str]) -> Situation | None:
        """The situation as an audience sees it: only its items in ``keys`` (the items one person or
        note may read), or None when what is left is not a situation any more."""
        keys = set(keys)
        kept = replace(self, items=[item for item in self.items if item.key in keys])
        return kept if kept.qualifies() else None


# ---------------------------------------------------------------------------- reading the hub
def _ref(kind: Any, key: Any) -> Ref:
    return (str(kind), str(key))


def _start_of(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


class _Hub:
    """The hub things around a set of items, read in a few queries: the things themselves, a
    finding's "about" links, one link out of each (:data:`HOPS`), and when documents were read."""

    FIELDS = ("pk", "kind", "key", "name", "url", "attrs")

    def __init__(self, today: datetime.date) -> None:
        self.today = today
        self.things: dict[Ref, dict[str, Any]] = {}
        self.refs: dict[int, Ref] = {}  # hub row id -> (kind, key), for this read only
        self.about: dict[Ref, list[Ref]] = defaultdict(list)
        self.near: dict[Ref, set[Ref]] = defaultdict(set)
        self.read_on: dict[str, datetime.date] = {}  # document key -> the day it was read
        self._hopped: set[tuple[Ref, str]] = set()

    def _keep(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            ref = _ref(row["kind"], row["key"])
            self.things[ref] = row
            self.refs[row["pk"]] = ref

    def _ids(self, refs: Iterable[Ref]) -> set[int]:
        return {self.things[ref]["pk"] for ref in refs if ref in self.things}

    def _load_ids(self, ids: Iterable[int]) -> None:
        missing = set(ids) - set(self.refs)
        if missing:
            self._keep(Entity.objects.filter(pk__in=missing).values(*self.FIELDS))

    def load(self, refs: Iterable[Ref]) -> None:
        """The hub rows of these things (a thing the hub does not hold is simply absent)."""
        keys: dict[str, set[str]] = defaultdict(set)
        for kind, key in refs:
            keys[kind].add(key)
        if not keys:
            return
        condition = Q()
        for kind, wanted in keys.items():
            condition |= Q(kind=kind, key__in=wanted)
        self._keep(Entity.objects.filter(condition).values(*self.FIELDS))

    def read_about(self, findings: Iterable[Ref]) -> None:
        """What each daily review finding is about (its PDs and partners)."""
        ids = self._ids(findings)
        if not ids:
            return
        pairs = list(
            Edge.objects.filter(source_id__in=ids, relation="about")
            .order_by("target_id")
            .values_list("source_id", "target_id")
        )
        self._load_ids(target for _, target in pairs)
        for source, target in pairs:
            if target in self.refs:
                self.about[self.refs[source]].append(self.refs[target])

    def hop(self, refs: Iterable[Ref], hops: dict[tuple[str, str], frozenset[str]] = HOPS) -> None:
        """One link out of each thing, through the links in ``hops`` only."""
        tag = ",".join(sorted(f"{relation}:{way}" for relation, way in hops))
        todo = [ref for ref in refs if (ref, tag) not in self._hopped]
        self._hopped.update((ref, tag) for ref in todo)
        ids = self._ids(todo)
        if not ids:
            return
        out = {relation for relation, way in hops if way == OUT}
        into = {relation for relation, way in hops if way == IN}
        edges = list(
            Edge.objects.filter(
                Q(source_id__in=ids, relation__in=out) | Q(target_id__in=ids, relation__in=into)
            ).values_list("source_id", "target_id", "relation")
        )
        self._load_ids({end for source, target, _ in edges for end in (source, target)})
        for source, target, relation in edges:
            if source not in self.refs or target not in self.refs:
                continue
            there, here = self.refs[target], self.refs[source]
            if source in ids and there[0] in hops.get((relation, OUT), ()):
                self.near[here].add(there)
            if target in ids and here[0] in hops.get((relation, IN), ()):
                self.near[there].add(here)

    def read_documents(self) -> None:
        """The day each document of the knowledge base among the things read was read."""
        keys = [int(key) for kind, key in self.things if kind == K.DOCUMENT and key.isdigit()]
        wanted = [key for key in keys if str(key) not in self.read_on]
        for pk, indexed_at, created_at in Document.objects.filter(pk__in=wanted).values_list(
            "pk", "indexed_at", "created_at"
        ):
            when = indexed_at or created_at
            if when:
                self.read_on[str(pk)] = timezone.localdate(when)

    def recent(self, ref: Ref) -> bool:
        """A thing that may be connected: any but a document read more than 90 days ago (or never)."""
        if ref[0] != K.DOCUMENT:
            return True
        read = self.read_on.get(ref[1])
        return bool(read and read >= self.today - datetime.timedelta(days=DOCUMENT_DAYS))

    def entry(self, ref: Ref) -> dict[str, Any]:
        """A hub thing as ``related`` keeps it: kind, key, name, url, and its date when it has one."""
        row = self.things[ref]
        out: dict[str, Any] = {"kind": ref[0], "key": ref[1], "name": row["name"], "url": row["url"] or ""}
        if ref[0] == K.GRANT and (row["attrs"] or {}).get("expiry"):
            out["date"] = str(row["attrs"]["expiry"])
        elif ref[0] == K.DOCUMENT and ref[1] in self.read_on:
            out["date"] = self.read_on[ref[1]].isoformat()
        return out


def _days(n: int) -> datetime.timedelta:
    return datetime.timedelta(days=n)


def _about(refs: set[Ref]) -> Q:
    """Changes to these things, or to a link to one of them (matched exactly afterwards)."""
    kinds, keys = sorted({kind for kind, _ in refs}), sorted({key for _, key in refs})
    return Q(kind__in=kinds, key__in=keys) | Q(link__kind__in=kinds, link__key__in=keys)


def recent_changes(refs: set[Ref], today: datetime.date) -> dict[Ref, list[Change]]:
    """The notable What's new changes of the last 7 days to these hub things or to their links, newest
    first, by thing (a daily review finding's are left to the review)."""
    if not refs:
        return {}
    found = (
        Change.objects.filter(notable=True, detected_at__gte=_start_of(today - _days(CHANGE_DAYS)))
        .exclude(kind=K.FINDING)
        .filter(_about(refs))
        .order_by("-detected_at", "-id")
    )
    out: dict[Ref, list[Change]] = defaultdict(list)
    for change in found[:2000]:
        for ref in _change_refs(change) & refs:
            out[ref].append(change)
    return out


def _change_refs(change: Change) -> set[Ref]:
    """The hub things a change is about: its own, and the other end of a link that appeared or went."""
    refs = {_ref(change.kind, change.key)}
    link = change.link if isinstance(change.link, dict) else {}
    if link.get("kind") and link.get("key") not in (None, ""):
        refs.add(_ref(link["kind"], link["key"]))
    return refs


def linkable(item: WatchItem) -> bool:
    """An open item that may be connected: not a system item nor one for the administrators only."""
    return item.state == OPEN and not refused(item)


# ---------------------------------------------------------------------------- ranking
def _grant_order(entry: dict[str, Any], today: datetime.date) -> tuple:
    """Grants expiring soonest first: those still to expire, then those expired (the latest first),
    then those without an expiry."""
    try:
        expiry = datetime.date.fromisoformat(str(entry.get("date") or ""))
    except ValueError:
        return (2, 0, entry["name"])
    if expiry >= today:
        return (0, expiry.toordinal(), entry["name"])
    return (1, -expiry.toordinal(), entry["name"])


def _ranked(
    hub: _Hub,
    refs: Iterable[Ref],
    *,
    lead: Iterable[dict[str, Any]] = (),
    middle: Iterable[dict[str, Any]] = (),
    limit: int = RELATED_MAX,
) -> list[dict[str, Any]]:
    """Connected things in the order they are kept: ``lead`` (as given), the partners and PDs among
    ``refs``, ``middle`` (as given), then the grants expiring soonest, the newest documents, the donors
    and the rest; each once, at most ``limit``."""
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in sorted(set(refs)):
        if ref in hub.things:
            by_kind[ref[0]].append(hub.entry(ref))
    by_name = {kind: sorted(entries, key=lambda e: e["name"]) for kind, entries in by_kind.items()}
    ordered = [
        *lead,
        *by_name.pop(K.PARTNER, []),
        *by_name.pop(K.PROGRAMME, []),
        *middle,
        *sorted(by_name.pop(K.GRANT, []), key=lambda e: _grant_order(e, hub.today)),
        *sorted(by_name.pop(K.DOCUMENT, []), key=lambda e: e.get("date") or "", reverse=True),
        *by_name.pop(K.DONOR, []),
        *[entry for kind in sorted(by_name) for entry in by_name[kind]],
    ]
    out, seen = [], set()
    for entry in ordered:
        mark = (entry["kind"], str(entry["key"]))
        if mark not in seen:
            seen.add(mark)
            out.append(entry)
    return out[:limit]


def _item_entry(item: WatchItem) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": ITEM, "key": item.key, "name": item.title, "url": item.url or ""}
    if item.due_date:
        entry["date"] = item.due_date.isoformat()
    return entry


def _item_order(item: WatchItem) -> tuple:
    """Worst first, then the soonest due, then by key."""
    return (-SEVERITY_RANK.get(item.severity, 0), item.due_date or datetime.date.max, item.key)


def _situation_order(situation: Situation) -> tuple:
    worst = max((SEVERITY_RANK.get(i.severity, 0) for i in situation.items), default=0)
    due = min((i.due_date for i in situation.items if i.due_date), default=datetime.date.max)
    return (-len(situation.items), -worst, due, situation.key)


# ---------------------------------------------------------------------------- linking
@dataclass
class _Links:
    """What one item reaches: the things it is about (``own``), one link further (``near``)."""

    own: list[Ref] = field(default_factory=list)
    near: set[Ref] = field(default_factory=set)

    def anchors(self) -> list[Ref]:
        return sorted({ref for ref in (*self.own, *self.near) if ref[0] in ANCHORS})

    def documents(self) -> set[Ref]:
        return {ref for ref in self.near if ref[0] == K.DOCUMENT}


def link(items: Iterable[WatchItem] | None = None, *, today: datetime.date | None = None) -> list[Situation]:
    """Connect the open items (default: every one) through the hub: write on each what it connects to
    (``related``) and the situation it is in (``situation_key``, empty when it stands alone), and
    return the situations, the largest first. An item whose hub thing is missing stands alone with
    nothing related. Run it before :func:`changes_since`, so that the news of a newly connected
    document reaches the items it concerns. Writes the two fields only, and only when they moved."""
    today = today or timezone.localdate()
    if items is None:  # every open item, and those that closed since the last pass (to leave their situation)
        items = [
            *WatchItem.objects.filter(state=OPEN).order_by("key"),
            *WatchItem.objects.exclude(state=OPEN).exclude(situation_key="").order_by("key"),
        ]
    items = list(items)
    linked = {item.key: item for item in sorted(items, key=lambda item: item.key) if linkable(item)}

    hub = _Hub(today)
    own_refs = {_ref(i.entity_kind, i.entity_key) for i in linked.values() if i.entity_kind and i.entity_key}
    hub.load(own_refs)
    hub.read_about(ref for ref in own_refs if ref[0] == K.FINDING)
    links: dict[str, _Links] = {}
    for item in linked.values():
        ref = _ref(item.entity_kind, item.entity_key)
        if item.entity_kind and ref in hub.things:
            links[item.key] = _Links(own=[ref, *hub.about.get(ref, [])])
    hub.hop({ref for each in links.values() for ref in each.own})
    for each in links.values():
        each.near = {ref for own in each.own for ref in hub.near.get(own, ())} - set(each.own)
    # the documents mentioning a partner the items meet on, one link beyond a PD's partner
    hub.hop({ref for each in links.values() for ref in each.anchors() if ref[0] == K.PARTNER}, MENTIONS)
    hub.read_documents()
    for each in links.values():
        each.near = {ref for ref in each.near if hub.recent(ref)}

    members: dict[Ref, set[str]] = defaultdict(set)  # partner or grant -> the items meeting on it
    about: dict[Ref, set[str]] = defaultdict(set)  # a hub thing -> the items about it
    for key, each in links.items():
        for anchor in each.anchors():
            members[anchor].add(key)
        for ref in each.own:
            about[ref].add(key)
    around = recent_changes({ref for each in links.values() for ref in each.own} | set(members), today)
    situations = _gather(hub, links, linked, members, around)
    placed: dict[str, str] = {}
    for situation in situations:
        documents = {_ref(entry["kind"], entry["key"]) for entry in situation.documents}
        for key in situation.item_keys:
            placed[key] = situation.key
            links[key].near |= documents  # the recent documents about its partner are connected too

    changed: list[WatchItem] = []
    for item in items:
        if item.key in links:
            related = _related(hub, links[item.key], item.key, linked, members, about)
            situation_key = placed.get(item.key, "")
        elif item.key in linked:
            related, situation_key = [], ""  # its hub thing is missing: it stands alone
        elif item.situation_key:
            related, situation_key = item.related, ""  # closed, gone or hidden: in no situation now
        else:
            continue
        if related != item.related or situation_key != item.situation_key:
            item.related, item.situation_key = related, situation_key
            changed.append(item)
    if changed:
        with transaction.atomic():
            WatchItem.objects.bulk_update(changed, ["related", "situation_key"], batch_size=500)
    return situations


def _related(
    hub: _Hub,
    each: _Links,
    key: str,
    linked: dict[str, WatchItem],
    members: dict[Ref, set[str]],
    about: dict[Ref, set[str]],
) -> list[dict[str, Any]]:
    """What one item connects to, at most 8: the things it is about, its partner, the other open items
    it meets (on a partner, a grant or the same thing; the worst and soonest first), then the grants
    expiring soonest, the newest documents and the donors."""
    others: set[str] = set()
    for anchor in each.anchors():
        others |= members.get(anchor, set())
    for ref in each.own:
        others |= about.get(ref, set())
    others.discard(key)
    meeting = [_item_entry(linked[k]) for k in sorted(others, key=lambda k: _item_order(linked[k]))]
    return _ranked(hub, each.near, lead=[hub.entry(ref) for ref in each.own], middle=meeting)


def _gather(
    hub: _Hub,
    links: dict[str, _Links],
    by_key: dict[str, WatchItem],
    members: dict[Ref, set[str]],
    around: dict[Ref, list[Change]],
) -> list[Situation]:
    """The situations: the anchor with the most items not placed yet first (a partner before a grant
    when as large); an anchor whose items do not make a situation leaves them free for the next."""
    free = {anchor: set(keys) for anchor, keys in members.items()}
    situations: list[Situation] = []
    while free:
        anchor = min(free, key=lambda a: (-len(free[a]), ANCHORS.index(a[0]), a[1]))
        keys = free.pop(anchor)
        if not keys:
            continue
        situation = _situation(hub, anchor, [by_key[k] for k in sorted(keys)], links, around)
        if situation.qualifies():
            situations.append(situation)
            for others in free.values():
                others -= keys
    return sorted(situations, key=_situation_order)


def _situation(
    hub: _Hub, anchor: Ref, items: list[WatchItem], links: dict[str, _Links], around: dict[Ref, list[Change]]
) -> Situation:
    own = {ref for item in items for ref in links[item.key].own if ref[0] != K.FINDING}
    near = {ref for item in items for ref in links[item.key].near}
    documents = {ref for ref in hub.near.get(anchor, ()) if ref[0] == K.DOCUMENT and hub.recent(ref)}
    documents |= {ref for item in items for ref in links[item.key].documents()}
    changes: dict[int, Change] = {}
    for ref in sorted({anchor, *(r for item in items for r in links[item.key].own)}):
        for change in around.get(ref, ()):
            changes[change.pk] = change
    newest = sorted(changes.values(), key=lambda c: (c.detected_at, c.pk), reverse=True)
    row = hub.things[anchor]
    return Situation(
        key=f"{anchor[0]}:{anchor[1]}"[:SITUATION_KEY_MAX],
        kind=anchor[0],
        entity_key=anchor[1],
        name=row["name"],
        url=row["url"] or "",
        items=sorted(items, key=_item_order),
        related=_ranked(hub, (own | near | documents) - {anchor}, lead=[hub.entry(anchor)]),
        documents=_ranked(hub, documents),
        changes=newest[:SITUATION_CHANGES_MAX],
    )


# ---------------------------------------------------------------------------- the news around items
def story_refs(item: WatchItem) -> set[Ref]:
    """The hub things whose news belongs in an item's story: its own and its connected partners,
    PDs, grants and documents."""
    refs = {_ref(item.entity_kind, item.entity_key)} if item.entity_kind and item.entity_key else set()
    for entry in item.related or ():
        if (
            isinstance(entry, dict)
            and entry.get("kind") in STORY_KINDS
            and entry.get("key") not in (None, "")
        ):
            refs.add(_ref(entry["kind"], entry["key"]))
    return refs


def told_changes(item: WatchItem) -> set[int]:
    """The What's new changes already in an item's story."""
    return {
        int(pk)
        for line in item.story or ()
        if isinstance(line, dict)
        for pk in line.get("changes") or ()
        if str(pk).isdigit()
    }


def changes_since(
    watermark: int,
    items: Iterable[WatchItem] | None = None,
    *,
    today: datetime.date | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    """Add to the stories of the open items (default: every one) the notable What's new changes read
    after ``watermark`` (a ``graph.Change`` id) about them or what they connect to, matched by (kind,
    key), each once, as What's new tells it. A daily review finding's changes are left out (the review
    tells its own), and so are changes older than 7 days. Returns the highest change id read: the next
    watermark. ``stats`` (when given) gets ``read``, ``added`` and ``items``."""
    today = today or timezone.localdate()
    watermark = int(watermark or 0)
    top = Change.objects.filter(pk__gt=watermark).aggregate(top=Max("pk"))["top"]
    counts = {"read": 0, "added": 0, "items": 0}
    if top is not None:
        counts["read"] = Change.objects.filter(pk__gt=watermark, pk__lte=top).count()
        items = WatchItem.objects.filter(state=OPEN).order_by("key") if items is None else items
        concerned: dict[Ref, list[WatchItem]] = defaultdict(list)
        for item in items:
            if linkable(item):
                for ref in story_refs(item):
                    concerned[ref].append(item)
        if concerned:
            _tell(watermark, top, today, concerned, counts)
    if stats is not None:
        stats.update(counts)
    return top if top is not None else watermark


def _tell(
    watermark: int, top: int, today: datetime.date, concerned: dict[Ref, list[WatchItem]], counts: dict
) -> None:
    found = (
        Change.objects.filter(
            pk__gt=watermark,
            pk__lte=top,
            notable=True,
            detected_at__gte=_start_of(today - _days(CHANGE_DAYS)),
        )
        .exclude(kind=K.FINDING)
        .filter(_about(set(concerned)))
        .order_by("pk")
    )
    news_of: dict[str, list[Change]] = defaultdict(list)
    items: dict[str, WatchItem] = {}
    for change in found.iterator():
        touched = {item.key: item for ref in _change_refs(change) for item in concerned.get(ref, ())}
        for key, item in touched.items():
            items[key] = item
            news_of[key].append(change)
    changed = 0
    with transaction.atomic():
        # each story read anew under a lock: a line added meanwhile (an answer on the page) is kept
        fresh_rows = WatchItem.objects.select_for_update().in_bulk([items[key].pk for key in news_of])
        for key, changes in news_of.items():
            item = fresh_rows.get(items[key].pk)
            if item is None:
                continue
            told = told_changes(item)
            fresh = [change for change in changes if change.pk not in told]
            if not fresh:
                continue
            for change in fresh[:STORY_CHANGES_MAX]:
                _add_line(item, f"What's new: {news.sentence(change)}", change.detected_at, [change.pk])
            rest = fresh[STORY_CHANGES_MAX:]
            if rest:
                _add_line(
                    item,
                    f"What's new: and {len(rest)} more change{'s' if len(rest) > 1 else ''} around it",
                    rest[-1].detected_at,
                    [change.pk for change in rest],
                )
            item.save(update_fields=["story"])
            items[key].story = item.story
            counts["added"] += len(fresh)
            changed += 1
    counts["items"] = changed


def _add_line(item: WatchItem, text: str, when: datetime.datetime, pks: list[int]) -> None:
    item.add_story(text, timezone.localdate(when))
    item.story[-1]["changes"] = pks


# ---------------------------------------------------------------------------- situations already stored
def situations_of(items: Iterable[WatchItem], *, today: datetime.date | None = None) -> list[Situation]:
    """The situations among ``items`` (e.g. the open items one person may see), from what :func:`link`
    stored on them, without reading the hub's things and links: the items are grouped by situation,
    and a group is a situation when it has two items or more, or one with a recent document or a
    notable change in the last 7 days (to the partner or grant, or to the PDs and things the items
    are about). The largest first."""
    today = today or timezone.localdate()
    groups: dict[str, list[WatchItem]] = defaultdict(list)
    for item in items:
        if item.state == OPEN and item.situation_key:
            groups[item.situation_key].append(item)
    watched: dict[str, set[Ref]] = {}
    for key, members in groups.items():
        kind, _, entity_key = key.partition(":")
        refs = {_ref(kind, entity_key)}
        for item in members:
            if item.entity_kind and item.entity_key:
                refs.add(_ref(item.entity_kind, item.entity_key))
            refs |= {
                _ref(e["kind"], e["key"])
                for e in item.related or ()
                if isinstance(e, dict) and e.get("kind") == K.PROGRAMME and e.get("key") not in (None, "")
            }
        watched[key] = refs
    around = recent_changes({ref for refs in watched.values() for ref in refs}, today)
    out = []
    for key, members in groups.items():
        kind, _, entity_key = key.partition(":")
        entries = [e for item in members for e in item.related or () if isinstance(e, dict)]
        anchor = next((e for e in entries if e.get("kind") == kind and str(e.get("key")) == entity_key), {})
        hub_entries, seen = [], set()
        for entry in [anchor, *entries]:
            mark = (entry.get("kind"), str(entry.get("key")))
            if entry and entry.get("kind") not in (ITEM, K.FINDING) and mark not in seen:
                seen.add(mark)
                hub_entries.append(entry)
        changes = {change.pk: change for ref in watched[key] for change in around.get(ref, ())}
        situation = Situation(
            key=key,
            kind=kind,
            entity_key=entity_key,
            name=str(anchor.get("name") or entity_key),
            url=str(anchor.get("url") or ""),
            items=sorted(members, key=_item_order),
            related=hub_entries[:RELATED_MAX],
            documents=[e for e in hub_entries if e.get("kind") == K.DOCUMENT],
            changes=sorted(changes.values(), key=lambda c: (c.detected_at, c.pk), reverse=True)[
                :SITUATION_CHANGES_MAX
            ],
        )
        if situation.qualifies():
            out.append(situation)
    return sorted(out, key=_situation_order)
