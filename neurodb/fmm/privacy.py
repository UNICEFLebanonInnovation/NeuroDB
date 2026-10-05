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
- a team is shown by its members' names only, never their e-mail addresses (:func:`person_display`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


def name_forms(values: Iterable[Any]) -> set[str]:
    """The name forms and e-mail addresses (``watch.people``) written in the text values given."""
    forms: set[str] = set()
    for text in values:
        forms |= people.name_forms(str(text)) | people.emails_in(str(text))
    return forms
