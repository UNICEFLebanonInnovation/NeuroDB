"""What NeuroDB Watch lets the AI read: an allow-list.

Everything the watch sends to the AI is built here and nowhere else. Each piece is copied field by field
from a fixed list, so a field added to an item later is not sent until someone adds it here:

- an item (:func:`for_model`): its key, its check, severity and confidence, the title written by code,
  its state and what changed today, its due date, days left and days open, the dates and numbers of its
  evidence (dates and numbers only, never the records' labels or values), the hub thing it is about
  (kind and name), its section names, whether someone owns it (yes or no, never who), the assignment
  status, how many times it was told and whether its section marked it done;
- a situation (:func:`situation_for_model`): its key and name, the keys of its items and the names of
  the partner, PD, grant, donor and documents it connects;
- a What's new line (:func:`changes_for_model`), written by code (``graph.news.sentence``);
- a section's counts for the whole-country note (:func:`count_for_model`), citable as
  ``count:<section id>``;
- an Ask NeuroDB tool result, for the background look-up (:func:`for_tool`): numbers, dates and yes/no
  under any field, and texts only under the fields of :data:`TOOL_TEXT_FIELDS` (names, codes, statuses,
  periods: a text under any other field is left out, so a field a tool returns later is not sent until
  someone adds it there); never the fields that hold a person, free text (a finding's ``detail``) or a
  link, whatever they hold; never a Makani centre or a daily review finding of the knowledge hub.

Never sent:

- system items (they carry sync errors), Makani items and the administrators' own items (donor
  accounts, the year rollover): :func:`refused` says why, and :func:`for_model` raises :class:`Refused`;
  the daily review's own data checks (sync failures and the like) are left out of the look-up's
  ``daily_review`` result too (:func:`neurodb.watch.investigate.review_for_look_up`);
- an item's detail, and its evidence records' labels and values;
- the owner and note of a finding assignment, and people's reactions and comments;
- any name of a person NeuroDB knows, any email address and any link: every text that goes out is
  scrubbed (:mod:`neurodb.watch.people`), and an item whose key names a person is refused;
- document summaries, earlier notes, the daily review's summary and anything else the AI wrote.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.review.models import FindingAssignment

from . import people
from .models import WatchItem

logger = logging.getLogger(__name__)

MAX_ITEMS = 25  # items in one note's input
MAX_SITUATIONS = 8
MAX_CHANGES = 15  # What's new lines in one note's input
MAX_NUMBERS = 15  # evidence numbers of one item
MAX_DATES = 5  # evidence dates of one item
MAX_CONNECTED = 8  # names a situation connects
MAX_SECTIONS = 5
TITLE_CHARS = 300
NAME_CHARS = 200
LINE_CHARS = 300

LINK_WITHHELD = "[link withheld]"
LINK = re.compile(
    r"\b(?:https?|ftp|mailto|javascript):\S*|\bwww\.\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|int|gov|edu|io|info|biz|co|lb|uk|fr|ch|us|me|app|ai|ly|gl|to)"
    r"\b(?:/\S*)?",
    re.IGNORECASE,
)
ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ][\d:.+\-Z]*)?$")
NUMBER_TEXT = re.compile(r"^-?\d[\d,]*(?:\.\d+)?%?$")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# What changed for an item today, as the note's writer may be told it (anything else is left out)
CHANGES = frozenset(
    {
        "new",
        "worse",
        "milestone",
        "overdue",
        "due_moved",
        "still_open",
        "closed",  # ended today: done or fixed
        "missed",  # ended today: its date passed and it was not done
        "no_longer_followed",  # ended today for another reason (its date moved, another point follows it)
        "gone",
    }
)
# The hub kinds a situation may name
CONNECTED_KINDS = frozenset({"partner", "programme_document", "grant", "donor", "document"})
# The hub kinds a What's new line is never sent for
CHANGES_REFUSED = frozenset({"makani_centre", "review_finding"})
ASSIGNMENT_STATUSES = frozenset(FindingAssignment.Status.values)

# Why an item is not sent (refused)
SYSTEM, MAKANI, ADMINS_ONLY, PERSON = "system", "makani", "admins_only", "person"
REFUSED_KEYS = {"system:": SYSTEM, "makani:": MAKANI, "donor_account:": ADMINS_ONLY}

# Tool results (the background look-up): fields never sent, by name
PERSON_FIELDS = frozenset(
    {
        "assigned_to",
        "assigned_to_name",
        "author",
        "author_name",
        "visit_lead",
        "submitted_by",
        "primary_traveler",
        "traveler",
        "traveller",
        "focal_point",
        "focal_points",
        "unicef_focal_points",
        "unicef_manager",
        "unicef_managers",
        "partner_focal_point",
        "partner_manager",
        "owner",
        "note",
        "notes",
        "comment",
        "comments",
        "email",
        "emails",
        "phone",
        "mobile",
        "user",
        "users",
        "username",
        "first_name",
        "last_name",
        "full_name",
        "person",
        "people",
        "staff",
        "contact",
        "contacts",
        "reviewer",
        "reviewers",
    }
)
FREE_TEXT_FIELDS = frozenset(
    {
        "detail",
        "details",
        "summary",
        "key_points",
        "description",
        "narrative",
        "narrative_finding",
        "narrative_assessment",
        "excerpt",
        "passage",
        "passages",
        "quote",
        "quotes",
        "content",
        "text",
        "answer",
    }
)
LINK_FIELDS = frozenset({"url", "urls", "link", "links", "href", "file", "download"})
DROPPED_SUFFIXES = (
    "_by",
    "_email",
    "_phone",
    "_url",
    "_traveler",
    "_lead",
    "_focal_point",
    "_owner",
    "_note",
)
# Tool results: the only fields whose text may be sent (a name, code, status, kind or period, never a
# free text). Numbers, dates and yes/no pass under any field that is not dropped.
TOOL_TEXT_FIELDS = frozenset(
    {
        # what a thing is
        "name",
        "short_name",
        "title",
        "kind",
        "kind_label",
        "key",
        "type",
        "cso_type",
        "number",
        "vendor_number",
        "reference",
        "engagement",
        "awp_code",
        "aliases",
        "activityinfo_names",
        "connection",
        "through",
        "to_kind",
        "tool",
        # what it belongs to
        "partner",
        "programme_document",
        "pd",
        "section",
        "sections",
        "donor",
        "donors",
        "matched_donors",
        "grant",
        "database",
        "indicator",
        "output",
        "outcome",
        "governorate",
        "district",
        "office",
        "offices",
        "module",
        "country_programme",
        "about",
        # its state
        "status",
        "state",
        "severity",
        "rating",
        "risk_rating",
        "overall_rating",
        "priority",
        "report",
        "report_type",
        "based_on",
        "unit",
        "currency",
        "category",
        "label",
        "level",
        "job",
        "what",
        "says",
        "from",
        "to",
        "rule",
        "how_to_continue",
        "error",
        "received",
        # when, written as text
        "start",
        "end",
        "date",
        "due",
        "submitted",
        "completed",
        "period",
        "period_start",
        "period_end",
        "since",
        "when",
        "made",
        "expiry",
        "last_success",
        "last_import",
        "hub_built",
        "hub_last_rebuilt",
        "year",
        "month",
        "months",
        "months_used",
        "first_month",
        "last_month",
    }
)
TOOL_LIST_MAX = 30
TOOL_DEPTH_MAX = 6
KEY_CHARS = 80  # a field name (or a label used as one) in a tool result


class Refused(ValueError):
    """An item that is never sent to the AI (see :func:`refused`)."""


# ---------------------------------------------------------------------------- texts
def text(value: Any, limit: int = NAME_CHARS, names: Iterable[str] | None = None) -> str:
    """A text as it may be sent: on one line, without links, email addresses or known person names,
    and at most ``limit`` characters."""
    clean = " ".join(str(value or "").split())
    clean = people.EMAIL.sub(people.EMAIL_WITHHELD, clean)  # before links: an address holds a domain
    clean = people.scrub(LINK.sub(LINK_WITHHELD, clean), names)
    return clean[:limit]


def _number(value: Any) -> int | float | str | bool | None:
    """``value`` when it is a number, a date or a written number; None for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float | decimal.Decimal):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else round(number, 4)
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        match = ISO_DATE.match(value)
        if match:
            return match.group(1)
        if NUMBER_TEXT.match(value):
            return value
    return None


def numbers(raw: Any) -> dict[str, Any]:
    """The evidence numbers that may be sent: plain field names, with numbers, dates or yes/no only (a
    text that is not a number is left out, whatever its field)."""
    out: dict[str, Any] = {}
    for key, value in raw.items() if isinstance(raw, Mapping) else ():
        name = str(key)
        if not FIELD_NAME.match(name) or _person_field(name):
            continue
        value = _number(value)
        if value is not None:
            out[name] = value
        if len(out) >= MAX_NUMBERS:
            break
    return out


def _dates(records: Any) -> list[str]:
    found: set[str] = set()
    for record in records if isinstance(records, list | tuple) else ():
        date = record.get("date") if isinstance(record, Mapping) else None
        value = _number(date) if date else None
        if isinstance(value, str) and ISO_DATE.match(value):
            found.add(value)
    return sorted(found)[:MAX_DATES]


# ---------------------------------------------------------------------------- items
def refused(item: WatchItem) -> str:
    """Why ``item`` is never sent to the AI ("" when it may be): a system item, a Makani item or an
    administrators' own item."""
    key = str(item.key or "")
    for prefix, why in REFUSED_KEYS.items():
        if key.startswith(prefix):
            return why
    if item.kind == WatchItem.Kind.SYSTEM:
        return SYSTEM
    source_job = (item.evidence or {}).get("source_job") if isinstance(item.evidence, dict) else ""
    if (
        str(item.detector or "").startswith("makani")
        or item.entity_kind == "makani_centre"
        or source_job == SyncRun.Job.COMPILER_WELLBEING
    ):
        return MAKANI
    if item.scope == WatchItem.Scope.ADMINS:
        return ADMINS_ONLY
    return ""


def _label(item: WatchItem) -> str:
    """The check's name, from the checks registered in this run (else its id, in words)."""
    from .detectors import REGISTRY

    detector = REGISTRY.get(item.detector)
    return detector.label if detector else str(item.detector or "").replace("_", " ")


def _entity_name(item: WatchItem) -> str:
    for ref in item.related or ():
        if (
            isinstance(ref, Mapping)
            and ref.get("kind") == item.entity_kind
            and str(ref.get("key")) == str(item.entity_key)
        ):
            return str(ref.get("name") or "")
    return ""


def for_model(
    item: WatchItem, stats: Mapping[str, Any] | None = None, names: Iterable[str] | None = None
) -> dict[str, Any]:
    """One item as the AI may read it (the allow-list in the module's notes). Raises :class:`Refused`
    for an item that is never sent.

    ``stats`` adds what the run knows about the item, all optional: ``today`` (the day days are
    counted from), ``label`` (the check's name), ``sections`` (section names; default the item's eTools
    section names), ``entity_name``, ``change`` (one of :data:`CHANGES`), ``times_told`` and
    ``done_by_section``."""
    why = refused(item)
    if why:
        raise Refused(f"{item.key}: not sent to the AI ({why})")
    names = people.known_names() if names is None else frozenset(names)
    if people.mentions(item.key, names):
        raise Refused(f"item {item.pk}: its key names a person ({PERSON})")
    stats = stats or {}
    today = stats.get("today") or timezone.localdate()
    evidence = item.evidence if isinstance(item.evidence, dict) else {}
    change = str(stats.get("change") or "")
    entity_name = stats.get("entity_name") or _entity_name(item)
    sections = stats.get("sections")
    if sections is None:
        sections = item.etools_sections or []
    return {
        "key": item.key,
        "check": text(stats.get("label") or _label(item), names=names),
        "severity": item.severity,
        "confidence": item.confidence,
        "title": text(item.title, TITLE_CHARS, names),
        "state": item.state,
        "change": change if change in CHANGES else "",
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "days_left": (item.due_date - today).days if item.due_date else None,
        "days_open": max(0, (today - item.first_seen_on).days) if item.first_seen_on else None,
        "closed_on": item.closed_on.isoformat() if item.closed_on and item.state != item.State.OPEN else None,
        "evidence_dates": _dates(evidence.get("records")),
        "numbers": numbers(evidence.get("numbers")),
        "about": (
            {"kind": item.entity_kind, "name": text(entity_name, names=names)} if item.entity_kind else None
        ),
        "sections": [text(s, names=names) for s in list(sections)[:MAX_SECTIONS] if s],
        "has_owner": bool(item.has_owner),
        "assignment_status": item.assignment_status if item.assignment_status in ASSIGNMENT_STATUSES else "",
        "times_told": int(stats.get("times_told") or 0),
        "done_by_section": bool(stats.get("done_by_section")),
    }


def items_for_model(
    items: Iterable[WatchItem],
    stats: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = MAX_ITEMS,
    names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """The items that may be sent, in their order, at most ``limit``; the refused ones are left out.
    ``stats`` maps an item's key to its :func:`for_model` stats."""
    names = people.known_names() if names is None else frozenset(names)
    out: list[dict[str, Any]] = []
    for item in items:
        if len(out) >= limit:
            break
        try:
            out.append(for_model(item, (stats or {}).get(item.key), names))
        except Refused as why:
            logger.info("NeuroDB Watch: %s", why)
    return out


# ---------------------------------------------------------------------------- situations, changes, counts
def _get(obj: Any, *fields: str, default: Any = None) -> Any:
    for field in fields:
        value = obj.get(field) if isinstance(obj, Mapping) else getattr(obj, field, None)
        if value is not None:
            return value
    return default


def situation_for_model(situation: Any, names: Iterable[str] | None = None) -> dict[str, Any]:
    """A situation (several open items meeting on one partner or grant) as the AI may read it: its key
    and name, its items' keys and the names of the partners, PDs, grants, donors and documents it
    connects. Takes a mapping or an object with ``key``, ``name``, ``item_keys`` (or ``items``) and
    ``related`` (or ``connected``: ``{kind, key, name}`` entries)."""
    names = people.known_names() if names is None else frozenset(names)
    key = str(_get(situation, "key", default=""))
    if not key or people.mentions(key, names):
        raise Refused("a situation without a key, or whose key names a person")
    items = _get(situation, "item_keys", "items", default=[]) or []
    item_keys = [str(getattr(i, "key", i)) for i in items][:MAX_ITEMS]
    connected = []
    for ref in _get(situation, "related", "connected", default=[]) or []:
        kind = str(_get(ref, "kind", default=""))
        name = text(_get(ref, "name", default=""), names=names)
        if kind in CONNECTED_KINDS and name:
            connected.append({"kind": kind, "name": name})
        if len(connected) >= MAX_CONNECTED:
            break
    return {
        "key": key,
        "name": text(_get(situation, "name", default=""), names=names),
        "items": [k for k in item_keys if not people.mentions(k, names)],
        "connected": connected,
    }


def change_for_model(change: Any, names: Iterable[str] | None = None) -> str | None:
    """One What's new line as the AI may read it: a ``graph.Change`` written by ``news.sentence``
    (a Makani centre or a daily review finding is never sent: None), or a line already written."""
    if isinstance(change, str):
        line = change
    else:
        if str(getattr(change, "kind", "")) in CHANGES_REFUSED:
            return None
        from neurodb.graph import news

        line = news.sentence(change)
    line = text(line, LINE_CHARS, names)
    return line or None


def changes_for_model(
    changes: Iterable[Any], limit: int = MAX_CHANGES, names: Iterable[str] | None = None
) -> list[str]:
    """The What's new lines that may be sent, at most ``limit``, each once."""
    names = people.known_names() if names is None else frozenset(names)
    lines: list[str] = []
    for change in changes:
        line = change_for_model(change, names)
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def count_key(section_id: int | str) -> str:
    """The key a section's counts are cited by in the whole-country note."""
    return f"count:{section_id}"


def count_for_model(section_id: int | str, section_name: str, counts: Mapping[str, Any]) -> dict[str, Any]:
    """One section's counts (computed by code) for the whole-country note, citable as
    ``count:<section id>``: numbers only."""
    return {"key": count_key(section_id), "section": text(section_name), "counts": numbers(counts)}


# ---------------------------------------------------------------------------- tool results
def _person_field(name: str) -> bool:
    name = name.lower()
    return name in PERSON_FIELDS or name.endswith(DROPPED_SUFFIXES)


def _dropped_field(name: str) -> bool:
    name = name.lower()
    return _person_field(name) or name in FREE_TEXT_FIELDS or name in LINK_FIELDS


def refused_kinds() -> frozenset[str]:
    """The knowledge hub kinds never passed to the AI from a tool (Makani centres, daily review
    findings), as codes and as the labels the tools also write."""
    from neurodb.graph.models import Entity

    labels = dict(Entity.Kind.choices)
    return frozenset(CHANGES_REFUSED | {str(labels[kind]) for kind in CHANGES_REFUSED if kind in labels})


def _refused_entry(value: Any, refused: frozenset[str]) -> bool:
    """A mapping about a refused kind of thing (a Makani centre, a daily review finding)."""
    return isinstance(value, Mapping) and str(value.get("kind") or "") in refused


_DROP = object()  # a value left out of a tool result


def for_tool(result: Any, names: Iterable[str] | None = None, _depth: int = 0, _field: str = "") -> Any:
    """An Ask NeuroDB tool result as the background look-up may pass it to the AI (an allow-list):
    numbers, dates and yes/no kept under any field; a text kept only under a field of
    :data:`TOOL_TEXT_FIELDS`, on one line, without known names, email addresses or links, and at most
    300 characters; the fields that hold a person, a free text or a link left out, whatever they hold;
    anything about a Makani centre or a daily review finding left out; at most 30 entries per list and
    6 levels deep."""
    names = people.known_names() if names is None else frozenset(names)
    value = _for_tool(result, names, refused_kinds(), _depth, _field)
    return None if value is _DROP else value


def _for_tool(result: Any, names: frozenset[str], refused: frozenset[str], depth: int, field: str) -> Any:
    if depth > TOOL_DEPTH_MAX:
        return _DROP
    if isinstance(result, Mapping):
        out = {}
        for k, v in result.items():
            name = str(k)
            if _dropped_field(name) or name in refused or _refused_entry(v, refused):
                continue
            clean = _for_tool(v, names, refused, depth + 1, name)
            if clean is not _DROP:
                out[text(name, KEY_CHARS, names)] = clean
        return out
    if isinstance(result, list | tuple | set | frozenset):
        kept = (
            _for_tool(v, names, refused, depth + 1, field)
            for v in list(result)
            if not _refused_entry(v, refused)
        )
        return [v for v in kept if v is not _DROP][:TOOL_LIST_MAX]
    if result is None or isinstance(result, bool | int):
        return result
    if not isinstance(result, str):
        number = _number(result)  # a decimal, a float, a date
        return number if number is not None else _DROP
    if result.strip() in refused:
        return _DROP  # a kind never passed on ("to_kind": "makani_centre")
    if field.lower() in TOOL_TEXT_FIELDS:
        return text(result, LINE_CHARS, names)
    written = result.strip()
    match = ISO_DATE.match(written)
    return match.group(1) if match else _DROP  # any other text: not on the allow-list
