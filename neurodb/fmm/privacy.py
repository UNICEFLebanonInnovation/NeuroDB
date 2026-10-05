"""What Monitoring insights lets out of the eTools field monitoring records, and how texts are cleaned.

Field monitoring records name people (the visit lead, the team) and hold free texts that can name more
(narratives, answers, summaries). The rules here are the same as Ask NeuroDB's for these records
(``datamart.query._fm_safe``) and NeuroDB Watch's, from one place each:

- a key that holds a person (:func:`person_like`: ``datamart.catalogue.person_key``, "visit_lead",
  "team_members", "person_responsible"..., or one of NeuroDB Watch's person fields such as
  "first_name", "username", "contacts") is never shown, never sent and never kept as an example;
- a text is cleaned before it goes anywhere (:func:`clean`): e-mail addresses, links and the person
  names NeuroDB knows (``watch.redact.text``), then phone numbers and names written after a title
  ("Mrs Layla Saab", ``watch.people``), each replaced by a placeholder;
- a team is shown by its members' names only, never their e-mail addresses (:func:`person_display`),
  and those names join the names NeuroDB removes from every text it sends (:func:`team_names`,
  registered into ``watch.people.EXTRA_SOURCES`` when the app starts);
- the AI sees a visit as a card copied from an allow-list of its fields (:func:`visit_card`): never its
  team, visit lead or narrative;
- everything about to be sent to the AI is checked one last time (:func:`assert_clean`): a known person
  name, an e-mail address, a phone number or a link anywhere in it stops the call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from neurodb.datamart import catalogue
from neurodb.watch import people, redact
from neurodb.watch.people import HONORIFIC_NAME, INTL_PHONE, PHONE, PHONE_WITHHELD

WITHHELD = "(withheld)"  # the example of a key that holds a person
EXAMPLE_CHARS = 60  # characters of a key's example (Fields found)
NAME_CHARS = 120  # characters of a team member's display name
# Person-like keys whose values are texts written by people, not their names (watch.redact.PERSON_FIELDS)
WRITTEN_BY_PEOPLE = frozenset({"note", "notes", "comment", "comments"})
PLACEHOLDERS = (people.NAME_WITHHELD, people.EMAIL_WITHHELD, redact.LINK_WITHHELD, PHONE_WITHHELD)
# A team written as one text: "Rania Haddad, Karim Saab; Nour Khalil / Ali Hassan and Zeina Fares"
_MEMBERS = re.compile(r"[,;/\n]|\s+and\s+", re.IGNORECASE)
# A Python or JSON object written as text ("{'name': 'Rania Haddad', 'email': ...}")
_OBJECT_NAME = re.compile(r"""['"](?:name|full_name|display_name)['"]\s*:\s*['"]([^'"]+)['"]""")
_OBJECT_FIRST_LAST = re.compile(
    r"""['"]first_name['"]\s*:\s*['"]([^'"]*)['"].*?['"]last_name['"]\s*:\s*['"]([^'"]*)['"]""", re.S
)


def names() -> frozenset[str]:
    """Every person name and e-mail address NeuroDB knows (``watch.people.known_names``)."""
    return people.known_names()


def person_like(key: Any) -> bool:
    """A record key (any depth, "a.b" too) that holds a person: one of its words is a person word
    (``catalogue.person_key``), or one of its parts is a field NeuroDB Watch never sends because it
    names or was written by a person (``watch.redact._person_field``)."""
    text = str(key or "")
    return catalogue.person_key(text) or any(redact._person_field(part) for part in text.split(".") if part)


def names_person(key: Any) -> bool:
    """A key that holds a person (:func:`person_like`) whose texts are names (a lead, a team, a user),
    not texts a person wrote (a note, a comment): only these teach the names to remove elsewhere, so
    that a comment's words are not taken for a name."""
    last = str(key or "").rsplit(".", 1)[-1].lower()
    return person_like(key) and last not in WRITTEN_BY_PEOPLE and not last.endswith("_note")


def _placeholders(text: str) -> int:
    return sum(text.count(placeholder) for placeholder in PLACEHOLDERS)


def clean(text: Any, limit: int, names_: frozenset[str] | None = None) -> tuple[str, int]:
    """``text`` on one line without e-mail addresses, links, known person names, phone numbers or names
    written after a title, cut to ``limit`` characters; and how many placeholders were put in."""
    if text is None:
        return "", 0
    flat = " ".join(str(text).split())
    before = _placeholders(flat)
    cleaned = redact.text(flat, limit * 2, names() if names_ is None else names_)
    cleaned = PHONE.sub(PHONE_WITHHELD, cleaned)
    cleaned = INTL_PHONE.sub(PHONE_WITHHELD, cleaned)
    cleaned = HONORIFIC_NAME.sub(people.NAME_WITHHELD, cleaned)
    return cleaned[:limit], max(0, _placeholders(cleaned) - before)


def _person_field(dataset: str, key: str) -> bool:
    """A candidate key of a field marked as a person (``fields.PERSON_FIELDS``)."""
    from . import fields  # fields reads this module

    return any(
        key in fields.CANDIDATES.get(ds, {}).get(field, ())
        for ds, field in fields.PERSON_FIELDS
        if ds == dataset
    )


def example_text(value: Any) -> str:
    """A value written out for an example: an object as its keys, a list as its first values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping):
        keys = [str(k) for k in list(value)[:8]]
        return "{" + ", ".join(keys) + (", …" if len(value) > 8 else "") + "}"
    if isinstance(value, list):
        shown = ", ".join(example_text(v) for v in value[:3])
        return f"[{shown}{f', … {len(value)} in all' if len(value) > 3 else ''}]"
    return str(value)


def example(dataset: str, key: str, value: Any, names_: frozenset[str] | None = None) -> str:
    """One example of a key's values for Fields found: "(withheld)" for a key that holds a person,
    else the value cleaned (:func:`clean`) and cut to ``EXAMPLE_CHARS`` characters."""
    if person_like(key) or _person_field(dataset, key):
        return WITHHELD
    return clean(example_text(value), EXAMPLE_CHARS, names_)[0]


# ------------------------------------------------------------------------------------------ team
def person_display(value: Any) -> tuple[list[str], int]:
    """The display names of the people in a team value (a list of objects, a text, an object written
    as text), and how many members are known by e-mail address (or id) only. E-mail addresses are
    never returned."""
    found: list[str] = []
    seen: set[str] = set()
    unnamed = 0

    def add(name: str) -> None:
        name = " ".join(people.EMAIL.sub(" ", name).split()).strip(" ,;:-<>()[]{}'\"")
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            found.append(name[:NAME_CHARS])

    def from_text(text: str) -> None:
        nonlocal unnamed
        text = text.strip()
        if not text:
            return
        if text[0] in "{[" and ":" in text:  # objects written as text
            written = _OBJECT_NAME.findall(text)
            written += [f"{first} {last}" for first, last in _OBJECT_FIRST_LAST.findall(text)]
            for name in written:
                add(name)
            unnamed += max(0, text.count("{") - len(written))
            return
        for piece in _MEMBERS.split(text):
            piece = piece.strip()
            if not piece:
                continue
            if people.EMAIL.search(piece) and not people.EMAIL.sub("", piece).strip(" <>()[],;:'\""):
                unnamed += 1  # an e-mail address and nothing else
                continue
            add(piece)

    def from_value(item: Any, depth: int = 0) -> None:
        nonlocal unnamed
        if item is None or depth > 4:
            return
        if isinstance(item, Mapping):
            name = next(
                (item[k] for k in ("name", "full_name", "display_name") if isinstance(item.get(k), str)),
                "",
            )
            if not name.strip():
                name = f"{item.get('first_name') or ''} {item.get('last_name') or ''}"
            if name.strip() and not people.EMAIL.fullmatch(name.strip()):
                add(name)
            elif any(v not in (None, "") for v in item.values()):
                unnamed += 1  # known by e-mail address or id only
            return
        if isinstance(item, list | tuple):
            for element in item:
                from_value(element, depth + 1)
            return
        if isinstance(item, str):
            from_text(item)
        elif isinstance(item, int) and not isinstance(item, bool):
            unnamed += 1  # a member's user id

    from_value(value)
    return found, unnamed


def team_names() -> list[str]:
    """Every team member's display name on the visits (``Visit.team``, which holds names only). Read by
    ``watch.people.known_names`` (``EXTRA_SOURCES``), so that NeuroDB Watch, Ask NeuroDB, the AI checks
    and Monitoring insights all remove these names from the texts they send."""
    from django.db.models import CharField, F, Func

    from .models import Visit

    names = (
        Visit.objects.annotate(name=Func(F("team"), function="unnest", output_field=CharField()))
        .order_by()
        .values_list("name", flat=True)
        .distinct()
    )
    return sorted(name for name in names if name)


def name_forms(values: Iterable[Any]) -> set[str]:
    """The name forms and e-mail addresses (``watch.people``) written in the text values given."""
    forms: set[str] = set()
    for text in values:
        forms |= people.name_forms(str(text)) | people.emails_in(str(text))
    return forms


# ------------------------------------------------------------------------------------------ the AI
CARD_TEXT_CHARS = 200  # characters of a name on a visit card (partner, place, section)
_PATH_KEY = re.compile(r"[A-Za-z0-9_:.-]{1,60}")  # a key written in a refusal's path ("visits.visit:1722")


class PrivacyRefused(Exception):
    """Something about to be sent to the AI holds a person name, an e-mail address, a phone number or a
    link. The message says what was found and where (the path of keys), never the text itself."""


def _card_text(value: Any, names_: frozenset[str]) -> str:
    return redact.text(value, CARD_TEXT_CHARS, names_)


def visit_card(visit, names_: frozenset[str] | None = None) -> dict[str, Any]:
    """A visit as the AI may read it "in full": an allow-list copy of its structured fields (dates,
    partner, programme document, place, sections, rating with its date, HACT Q1, quality, flags, urgency
    and action point counts). Never its team, visit lead, narratives or answers. Names pass through
    ``watch.redact.text`` (e-mail addresses, links and known person names removed). Reads
    ``visit.partner`` and ``visit.pd``: select them with the visits."""
    names_ = names() if names_ is None else names_
    not_rated_yet = visit.rating == "not_monitored" and visit.status_group in ("planned", "in_progress")
    end = visit.end_date.isoformat() if visit.end_date else None
    pd_number = (visit.pd.number if visit.pd_id and visit.pd else "") or next(
        iter(visit.pd_numbers or []), ""
    )
    return {
        "key": f"visit:{visit.key}",
        "label": _card_text(visit.label, names_),
        "date": end,
        "start": visit.start_date.isoformat() if visit.start_date else None,
        "status": visit.status or visit.status_group,
        "partner": _card_text(visit.partner.name if visit.partner_id and visit.partner else "", names_),
        "pd": _card_text(pd_number, names_),
        "sections": [_card_text(name, names_) for name in visit.section_names or []],
        "governorate": _card_text(visit.governorate_name, names_),
        "place": _card_text(visit.place_name, names_),
        "rating": visit.rating,
        "rated_on": None if not_rated_yet else end,
        "hact_q1": visit.hact_q1 or None,
        "quality": float(visit.quality_score) if visit.quality_score is not None else None,
        "flags": list(visit.flags or []),
        "urgency": visit.urgency,
        "action_points_open": visit.action_points_open,
        "action_points_overdue": visit.action_points_overdue,
    }


def _strings(value: Any, names_: frozenset[str], path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in ``value`` (keys included) with the path of keys that leads to it; a key that is
    not a plain field name, or that names someone, shows as "?" in the path."""
    if isinstance(value, str):
        yield path or "(root)", value
    elif isinstance(value, Mapping):
        for key, inner in value.items():
            plain = _PATH_KEY.fullmatch(str(key)) and not people.mentions(str(key), names_)
            shown = str(key) if plain else "?"
            where = f"{path}.{shown}" if path else shown
            yield where, str(key)
            yield from _strings(inner, names_, where)
    elif isinstance(value, list | tuple | set | frozenset):
        for index, inner in enumerate(value):
            yield from _strings(inner, names_, f"{path}[{index}]")


def assert_clean(payload: Any, names_: frozenset[str]) -> None:
    """The last check before anything is sent to the AI (a brief's payload, a chat look-up's result):
    raises :class:`PrivacyRefused` when any string in ``payload`` holds an e-mail address, a phone
    number, a link or a person name in ``names_`` (``watch.people.mentions``)."""
    for where, text in _strings(payload, names_):
        if people.EMAIL.search(text):
            raise PrivacyRefused(f"an e-mail address at {where}")
        if PHONE.search(text) or INTL_PHONE.search(text):
            raise PrivacyRefused(f"a phone number at {where}")
        if redact.LINK.search(text):
            raise PrivacyRefused(f"a link at {where}")
        if people.mentions(text, names_):
            raise PrivacyRefused(f"a person name at {where}")
