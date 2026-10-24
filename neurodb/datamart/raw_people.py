"""The people rule of Raw eTools data (Admin → Data and sync): a raw eTools record as a validator may see it
on screen or download it in a CSV, and what was withheld, field by field, as counts. Nothing else writes a
raw record out.

People (team members, visit leads, monitors, focal points, assignees, submitters, authors, travellers, anyone)
are never shown or written: a field that names people shows how many (a text counts the names between its
commas, semicolons and new lines, a list its items). A text a person wrote (comments, notes, remarks) is
withheld whole. Every other text is cleaned by Ask NeuroDB's own rule (``query._safe``: e-mail addresses
whole, cut or spelled out, links, phone numbers, the names NeuroDB knows and those the record holds, names
after a title), so the rule is the same on every screen, every file and in what the AI is sent. In order:

1. contact fields (e-mail, phone, mobile) are dropped at any depth (``catalogue.scrub``);
2. the answers of PSEA assessments (``catalogue.SAFEGUARDING``) keep only ``catalogue.SAFEGUARDING_KEYS``: the
   others are one count, never named;
3. a field whose name is not an identifier (a space or an ``@``, longer than 64 characters, shaped like an
   e-mail, a link or a phone, or a person's name NeuroDB knows or the record holds) is never a column nor a
   row: it adds to ``(fields naming a person)``. Inside a field, at any depth, so does a name with a letter
   that is not an identifier ("Zorba Quintel": 4); a name without a letter stays (PRP's "(12, 31)");
4. a top-level field nobody reviewed (``raw.REVIEWED_KEYS``, the fields the pages read, or a superuser's
   ``RawFieldReview``) is shown only when every value in it is a number, yes/no, empty, a date or a time, and
   every name inside it passes step 3; otherwise it reads "N values (not reviewed)";
5. the values are cleaned by Ask's rule (``query._safe``, with the record's own names learnt from the whole
   record before it is narrowed to the fields shown);
6. what was dropped is counted: people, texts people wrote, and the fields dropped inside a field shown, under
   their dotted path ("location.focal_point"), whose people add up in a CSV's ``people_inside``.

NeuroDB's own columns are read through pinned lists (:data:`PERSON_COLUMNS`, :data:`NEVER_COLUMNS`,
:data:`TEXT_COLUMNS`, :data:`JSON_COLUMNS`): a test fails on a new JSON or person column until it is listed.
Every other text column is cleaned by the payload's rule under its own name, so a reference, a code or an
amount keeps its value and a name, an e-mail address or a phone in a description does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import catalogue
from . import query as dmq

PEOPLE, TEXT, NOT_REVIEWED, SAFEGUARDING, FIELD_NAMES = (
    "people",
    "text",
    "not_reviewed",
    "safeguarding",
    "fields",
)
FIELDS_NAMING = "(fields naming a person)"  # the fields whose names are withheld, counted
WITHHELD_KEYS = "withheld_keys"  # a PSEA answer's fields outside SAFEGUARDING_KEYS, counted
KEY_MAX = 64  # a field name longer than this is never a column
_PIECES = re.compile(r"[,;\n]")

# NeuroDB's own columns: (source, column) for the datamart tables (the catalogue's dataset names), (model
# name, column) for the partners, programme documents, agreements, trips and engagements the records link to.
PERSON_COLUMNS = frozenset(  # shown and downloaded as "<column> (people)": a count
    {
        ("partner_reports", "submitted_by"),
        ("field_monitoring", "visit_lead"),
        ("action_points", "assigned_to_name"),
        ("tpm_visits", "author_name"),
        ("staff_visits", "primary_traveler"),
        ("pca", "unicef_focal_points"),
        ("pca", "partner_focal_point"),
        ("pca", "partner_manager"),
        ("pca", "unicef_manager"),
        ("pca", "unicef_managers"),
        ("agreement", "signed_by"),
        ("agreement", "partner_manager"),
        ("partnerorganization", "staff_members"),
        ("travel", "traveler"),
        ("travel", "traveler_name"),
        ("travel", "supervisor"),
        ("travel", "supervisor_name"),
        ("travelactivity", "primary_traveler"),
        ("travelactivity", "is_primary_traveler"),
        ("engagement", "staff_members"),
        ("engagement", "authorized_officers"),
        ("actionpoint", "author"),
        ("actionpoint", "author_name"),
        ("actionpoint", "assigned_by"),
        ("actionpoint", "assigned_by_name"),
        ("actionpoint", "assigned_to"),
        ("actionpoint", "assigned_to_name"),
    }
)
NEVER_COLUMNS = frozenset(  # never shown, never downloaded: contacts, and the texts people wrote in eTools
    {
        ("partnerorganization", "email"),
        ("partnerorganization", "phone_number"),
        ("partnerorganization", "comments"),
        ("travel", "rejection_note"),
        ("travel", "cancellation_note"),
        ("travel", "certification_note"),
        ("travel", "report_note"),
        ("travel", "additional_note"),
        ("actionpoint", "comments"),
    }
)
TEXT_COLUMNS = frozenset(  # the free texts: never searched, cleaned like a text of the record (as every text)
    {
        ("partner_reports", "narrative"),
        ("partner_reports", "narrative_assessment"),
        ("field_monitoring", "narrative_finding"),
        ("action_points", "description"),
        ("audit_findings", "description"),
        ("audit_findings", "recommendation"),
        ("audit_findings", "ip_comments"),
        ("*", "title"),  # DatamartDocument.title and the typed titles are built from raw values
    }
)
JSON_COLUMNS = frozenset(  # JSON columns other than ``data``: written out through public_record
    {
        ("engagements", "details"),  # AuditEngagement.details: the engagement's audit, spot check... records
        ("partner_reports", "disaggregation"),
        ("tpm_activities", "location_pcodes"),
    }
)
# the JSON columns holding whole eTools records by the source each came from (``enrich_engagement``): each
# record read by its own source's rule, its reviewed fields among it; the others have a fixed shape (EVERY)
RECORD_COLUMNS = frozenset({("engagements", "details")})


@dataclass(frozen=True)
class Withheld:
    """What a field (or a field inside one, ``inside``) holds that is not shown, as a count: people named,
    texts withheld, values not shown until reviewed, fields withheld (a PSEA answer's, or fields whose names
    name a person)."""

    kind: str  # PEOPLE, TEXT, NOT_REVIEWED, SAFEGUARDING or FIELD_NAMES
    count: int
    inside: bool = False

    @property
    def words(self) -> str:
        n = self.count
        if self.kind == PEOPLE:
            return f"{n:,} {'person' if n == 1 else 'people'} (not shown)"
        if self.kind == TEXT:
            return "text withheld (written by a person)"
        if self.kind == NOT_REVIEWED:
            return f"{n:,} {'value' if n == 1 else 'values'} (not reviewed)"
        if self.kind == SAFEGUARDING:
            return f"{n:,} {'field' if n == 1 else 'fields'} withheld (safeguarding)"
        if n == 1:
            return "1 field not shown: its name names a person"
        return f"{n:,} fields not shown: their names name a person"

    @property
    def cell(self) -> Any:
        """What a CSV cell holds for it: a count, or "N values (not reviewed)"."""
        return f"{self.count:,} values (not reviewed)" if self.kind == NOT_REVIEWED else self.count


class _Every(frozenset):
    """Every field counts as reviewed: the JSON columns of NeuroDB's own tables whose shape is fixed (a
    disaggregation's values, a list of P-codes), never one holding eTools records (:data:`RECORD_COLUMNS`)."""

    def __contains__(self, item: object) -> bool:
        return True


EVERY = _Every()


def identifier(key: Any, fm: bool = False) -> bool:
    """A field name that may be a column: no space or ``@``, at most :data:`KEY_MAX` characters, and not
    shaped
    like an e-mail address, a link or a phone (``query._key_shape_withheld``)."""
    text = str(key)
    if not text or " " in text or "@" in text or len(text) > KEY_MAX:
        return False
    return not dmq._key_shape_withheld(text, fm)


def written(key: Any) -> bool:
    """A field that holds a text a person wrote: withheld whole, never counted as people."""
    from neurodb.watch import redact

    name = str(key).lower()
    return (
        catalogue.written_by_people(key)
        or name in redact.FREE_TEXT_FIELDS
        or name.endswith("_note")
        or name in ("comments", "notes")
    )


def names_people(key: Any) -> bool:
    """A top-level field the people rule leaves out (``query._person``): shown as a count, or withheld whole
    when a person wrote it (:func:`written`)."""
    return dmq._person(str(key))


def people_count(value: Any) -> int:
    """How many people a value names: a text, its names between commas, semicolons and new lines; a list, its
    items; anything else that holds something, one."""
    if value in (None, "", [], {}):
        return 0
    if isinstance(value, str):
        return sum(1 for piece in _PIECES.split(value) if piece.strip())
    if isinstance(value, list):
        return sum(1 for item in value if item not in (None, "", [], {}))
    return 1


def _texts(value: Any) -> int:
    if value in (None, "", [], {}):
        return 0
    if isinstance(value, list):
        return sum(1 for item in value if item not in (None, "", [], {}))
    return 1


def _values(value: Any) -> int:
    """The values a field holds, at any depth (empty ones left out)."""
    if isinstance(value, dict):
        return sum(_values(v) for v in value.values())
    if isinstance(value, list):
        return sum(_values(v) for v in value)
    return 0 if value in (None, "") else 1


_DATE = re.compile(dmq.DATE_VALUE)
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def inner_key(key: Any, fm: bool = False) -> bool:
    """A field name inside a field that may be shown: an identifier, or a name without a letter (PRP's
    disaggregation ids "(12, 31)", a date). A name with a letter that is no identifier ("Zorba Quintel")
    never is."""
    return identifier(key, fm) or not any(c.isalpha() for c in str(key))


def _inner(value: Any, fm: bool) -> Any:
    """A cleaned value without the field names inside it that :func:`inner_key` refuses, at any depth."""
    if isinstance(value, dict):
        return {k: _inner(v, fm) for k, v in value.items() if inner_key(k, fm)}
    if isinstance(value, list):
        return [_inner(v, fm) for v in value]
    return value


def plain(value: Any, rules: dmq.Rules, own: frozenset[str] = frozenset()) -> bool:
    """Every value inside is a number, yes/no, empty, a date or a time, and every field name inside it may be
    shown (:func:`inner_key`, and no person's name, address or phone: ``query._key_withheld``): no text that
    could name anyone."""
    if isinstance(value, dict):
        return all(
            inner_key(k, rules.fm) and not dmq._key_withheld(k, rules, own) and plain(v, rules, own)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return all(plain(v, rules, own) for v in value)
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or bool(_DATE.match(text) or _TIME.match(text) or dmq.PLAIN_NUMBER.fullmatch(text))
    return False


def _add(withheld: dict[str, Withheld], where: str, kind: str, n: int, inside: bool) -> None:
    found = withheld.get(where)
    withheld[where] = Withheld(kind, (found.count if found else 0) + n, inside)


def _inside(
    original: Any,
    cleaned: Any,
    path: str,
    withheld: dict[str, Withheld],
    rules: dmq.Rules,
    own: frozenset[str],
) -> None:
    """The fields dropped inside a field shown, counted under their dotted path."""
    if isinstance(original, str):
        parsed = dmq._parsed(original)
        if parsed is original or parsed is dmq.OBJECT_WITHHELD:
            return
        original = parsed
    if isinstance(original, dict) and isinstance(cleaned, dict):
        for key, item in original.items():
            where = f"{path}.{key}"
            if key in cleaned:
                _inside(item, cleaned[key], where, withheld, rules, own)
            elif dmq._contact_key(key):
                continue  # a contact field: never shown, never counted
            elif not identifier(key, rules.fm) or dmq._key_withheld(key, rules, own):
                _add(withheld, FIELDS_NAMING, FIELD_NAMES, 1, False)
            elif written(key):
                _add(withheld, where, TEXT, _texts(item), True)
            else:
                _add(withheld, where, PEOPLE, people_count(item), True)
    elif isinstance(original, list) and isinstance(cleaned, list):
        for item, kept in zip(original, cleaned, strict=False):
            _inside(item, kept, path, withheld, rules, own)


def public_record(
    data: dict,
    source: str,
    names: frozenset[str],
    *,
    fields: list[str] | None = None,
    reviewed: frozenset[str] | None = None,
) -> tuple[dict, dict[str, Withheld]]:
    """A record as a validator may see or download it, and what was withheld, per field (see the module's
    notes). ``fields``: only these top-level fields are cleaned and returned (the record's own names are still
    learnt from all of it); ``reviewed``: the source's reviewed fields (``raw.reviewed_keys``, read once by
    the caller for many records)."""
    if reviewed is None:
        from . import raw

        reviewed = raw.reviewed_keys(source)
    data = catalogue.scrub(data if isinstance(data, dict) else {})
    withheld: dict[str, Withheld] = {}
    if source in catalogue.SAFEGUARDING:  # never a comment: only the fields that hold no text of a case
        kept = {k: v for k, v in data.items() if k in catalogue.SAFEGUARDING_KEYS}
        if len(kept) < len(data):
            withheld[WITHHELD_KEYS] = Withheld(SAFEGUARDING, len(data) - len(kept))
        data = kept
    rules = dmq.Rules(names, source)
    own = dmq._own_names(data)
    named = 0
    found: dict[str, Any] = {}
    for key, value in data.items():
        if not identifier(key, rules.fm) or dmq._key_withheld(key, rules, own):
            named += 1
        else:
            found[key] = value
    if named:
        withheld[FIELDS_NAMING] = Withheld(FIELD_NAMES, named)
    if fields is not None:
        found = {f: found[f] for f in fields if f in found}
    cleaned = dmq._safe(found, rules, own)
    shown: dict[str, Any] = {}
    for key, value in found.items():
        if key not in cleaned:  # a person, or a text a person wrote
            if written(key):
                withheld[key] = Withheld(TEXT, _texts(value))
            else:
                withheld[key] = Withheld(PEOPLE, people_count(value))
        elif key not in reviewed and not plain(value, rules, own):
            withheld[key] = Withheld(NOT_REVIEWED, _values(value))
        else:
            shown[key] = _inner(cleaned[key], rules.fm)
            _inside(value, shown[key], str(key), withheld, rules, own)
    return shown, withheld


def people_inside(withheld: dict[str, Withheld]) -> int:
    """The people named inside the fields shown (a CSV's ``people_inside``)."""
    return sum(w.count for w in withheld.values() if w.inside and w.kind == PEOPLE)


def records(value: Any, names: frozenset[str]) -> Any:
    """A column of :data:`RECORD_COLUMNS` as shown and downloaded: each record under the name of the source it
    came from, read as that source's own page reads it (``public_record`` with its reviewed fields: a field
    nobody reviewed reads "N values (not reviewed)", a person as a count), what was withheld in words after
    the fields shown; anything else, a value under a name that is no source, as "N values (not reviewed)"."""
    from . import raw

    if not isinstance(value, dict):
        return Withheld(NOT_REVIEWED, _values(value)).words if _values(value) else value
    out: dict[str, Any] = {}
    for key, record in value.items():
        if not (isinstance(record, dict) and raw.known(str(key))):
            out[str(key)] = Withheld(NOT_REVIEWED, _values(record)).words
            continue
        shown, withheld = public_record(record, str(key), names, reviewed=raw.reviewed_keys(str(key)))
        out[str(key)] = {**shown, **{field: w.words for field, w in withheld.items()}}
    return out


def text(value: Any, source: str, column: str, names: frozenset[str], own: frozenset[str]) -> Any:
    """A text of NeuroDB's own columns, cleaned as the same text in the record would be (``query._safe``: a
    field monitoring text over 80 characters is withheld whole)."""
    if value in (None, ""):
        return value
    cleaned = dmq._safe({column: value}, dmq.Rules(names, source), own)
    return cleaned.get(column, "text withheld (written by a person)")


def column(source: str, name: str, value: Any, names: frozenset[str], own: frozenset[str]) -> Any:
    """One of NeuroDB's own columns as shown and downloaded: a count of the people a person column names, a
    JSON column through :func:`public_record` (one holding eTools records, each by its own source's rule:
    :func:`records`), a text cleaned under its own name (a free text of
    :data:`TEXT_COLUMNS`, and every other text: a reference, a code or an amount keeps its value), anything
    else as it is. A column of :data:`NEVER_COLUMNS` is never asked for."""
    if (source, name) in NEVER_COLUMNS:
        raise ValueError(f"{source}.{name} is never shown")
    if (source, name) in PERSON_COLUMNS:
        return people_count(value)
    if (source, name) in RECORD_COLUMNS:
        return records(value, names)
    if (source, name) in JSON_COLUMNS:
        if isinstance(value, dict):
            return public_record(value, source, names, reviewed=EVERY)[0]
        return dmq._safe({name: value}, dmq.Rules(names, source), own).get(name)
    if (source, name) in TEXT_COLUMNS or ("*", name) in TEXT_COLUMNS or isinstance(value, str):
        return text(value, source, name, names, own)
    return value
