"""Read-only queries over every eTools Datamart dataset, for the AI assistant.

Each dataset of ``catalogue`` is either a table of its own or ``DatamartDocument`` rows; both keep the
whole record in ``data``, so one query language covers all of them: filters on record fields,
full-text search, date range, partner and programme document (through the links made at sync time),
grouping with counts and sums, and a field selection. Contact details are removed from every
output and long values are shortened.

The answers of PSEA assessments (``catalogue.SAFEGUARDING``) may describe a case of sexual exploitation
or abuse: only the keys of ``catalogue.SAFEGUARDING_KEYS`` are sent (the standard, its rating, the
partner, the date), and those records are never searched by text nor queried on another key.

Every dataset is sent without a person (the project's rule: team members, visit leads, monitors, focal
points, assignees, authors and travellers never go to the AI): the keys that hold a person are dropped and
cannot be filtered, grouped, summed, sorted or selected on, a key that is itself a name, an address or a
phone is dropped, every text is sent without e-mail addresses, links, phone numbers, the person names
NeuroDB knows (every name the eTools records hold under their person keys among them), the names the
record holds or names written after a title (:func:`_safe`), a number with a phone's shape is withheld
outside a key that holds an amount, and a search, a filter, a group, a sum, a comparison or a sort only
ever looks at what Ask is sent (:func:`_confirm`, :func:`_sent_groups`, :func:`_numeric`,
:func:`_ordered`): it finds no record by a person's name, and tells nothing of where one falls; nor does
whether a query is refused (too many records to check, a field that does not hold dates only), which is
never decided on the records a text found.

The field monitoring datasets (``catalogue.FM_PRIVATE``) name the visit lead and team and hold long
free texts. For them the same rules hold, with Monitoring insights' cleaning of texts (any name after a
title, any 7 or 8 digits taken for a phone, under any key: an answer's "value" is what a monitor wrote,
never an amount), and texts longer than ``catalogue.FM_TEXT_MAX`` are withheld (a search still looks in
them, cleaned). Monitoring insights shows those texts to staff.
"""

from __future__ import annotations

import ast
import bisect
import dataclasses
import datetime
import functools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.apps import apps
from django.db.models import Count, F, Model, Q, QuerySet, Sum, TextField, Value
from django.db.models.expressions import RawSQL
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, ExtractMonth, ExtractYear, NullIf
from django.urls import reverse

from neurodb.partnerships.models import PCA, PartnerOrganization

from . import catalogue
from .models import DatamartDocument

MAX_ROWS = 50
MAX_GROUPS = 50
MAX_TEXT = 400
MAX_LIST = 15
MAX_OUTPUT_CHARS = 40_000  # one answer's worth of records; the model is told to narrow or pick fields
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
OPERATORS = ("eq", "ne", "contains", "gt", "gte", "lt", "lte", "isnull")
NOISE = {"id", "seen", "last_modify_date", "country_name", "schema_name", "area_code"}
# A number written alone (PostgreSQL's pattern; ``PLAIN_NUMBER`` is the same in Python)
NUMBER = r"^\s*-?(0|[1-9][0-9]*)(\.[0-9]+)?\s*$"
# Texts are compared with __gt/__lt (and sorted by the database) only as whole dates, and only in a
# field that holds nothing else: ISO dates and times, sent as they are stored
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_VALUE = r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?)?$"
# An amount or a count written alone as text ("2500000.00", "1500000") or as a number is sent as it is,
# unless it has a phone's shape (a 961 prefix, the leading 0, or 70, 71, 76, 78, 79 or 81 and six
# digits, a Syrian mobile's 093 to 099; in field monitoring, any 7 or 8 digits); under a key that holds
# an amount (``catalogue.holds_amount``: "local_amount": "70000000") always, outside field monitoring
# (:func:`_measure_key`)
PLAIN_NUMBER = re.compile(r"\s*-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\s*")
WHOLE_PHONE = re.compile(
    r"\s*(?:(?:\+|00)?961[\s.-]?0?[1-9]\d{6,7}|0?(?:7[016-9]|81)\d{6}|0[1-9]\d{6}|09[3-9]\d{7})\s*"
)
FM_WHOLE_PHONE = r"\s*-?\d{7,8}(?:\.\d+)?\s*"
# An e-mail address, whole or cut short ("someone@exam", "someone(@)exam", "someone[at]exam",
# "someone<at>exam", "someone @exam": a space before the sign when a letter follows it, and only where an
# address can be, before a domain in lower case or after a word that looks like an address's own part,
# in lower case or with a full stop, an underscore or a digit, "r.haddad @Unicef"; "meeting @ 10am",
# "trainers @50", "Workshop @Beirut" and "Follow @UNICEFLebanon" stay)
AT_WORD = re.compile(
    r"(?<![\w.+'-])(?:[\w.+'-]+(?:[@＠﹫]|\s*[\[(]\s*[@＠﹫]\s*[\])]\s*|[\[({<](?i:at)[\])}>])"
    r"|(?:[a-z0-9.+'_-]+|[\w+'-]*[._\d][\w.+'-]*)\s+[@＠﹫](?=[^\W\d_])|[\w.+'-]+\s+[@＠﹫](?=[a-z]))[\w.-]*"
)
UNCUT = 10**9
_SPELLED_HINT = re.compile(r"[@＠﹫]|\bat\b|at[\])}>]", re.IGNORECASE)
_AT_HINT = re.compile(r"[@＠﹫]|at[\])}>]", re.IGNORECASE)
_DIGIT = re.compile(r"\d")
# Records a search or a filter may have to be checked in, as Ask is sent them: counted before the text
# narrows them (by the partner, the programme document, the dates and the filters on numbers, dates and
# single words), so that the count never depends on what is withheld; above that Ask is asked to narrow
CONFIRM_MAX = 20_000  # for a text (a search, a __contains); the different values a sort or a group cleans
EQUAL_MAX = 100_000  # for an equality (eq, ne): each value is cleaned once
# When every dataset is searched: the latest SEARCH_WINDOW records of each are looked in, and a dataset is
# counted "at least SEARCH_SAMPLE" once that many of them hold the text as Ask is sent it
SEARCH_SAMPLE = 50
SEARCH_WINDOW = 1000
GROUP_NAMES_MAX = 5000  # grouped records whose own names are taken out of the group labels
# Places and organisations: a name after Sheikh or Dr ("Sheikh Taba", "Dr. Mohamad Khaled Social
# Foundation") is kept under a key that holds the name of a place or an organisation ("location",
# "partner_name", "name" inside "location" or "offices_data"; a partner's own name; any name key of the
# place datasets), never under a key that holds a text, whatever it is about ("site_feedback",
# "other_partners_involved"). A name after Mr, Mrs, Ms... is never kept.
PLACE_DATASETS = frozenset({"locations", "location_sites", "intervention_locations", "offices"})
PLACE_WORDS = frozenset(
    {"location", "locations", "site", "sites", "place", "places", "district", "governorate", "caza"}
    | {"cadaster", "village", "town", "office", "offices"}
)
ORG_WORDS = frozenset(
    {"partner", "partners", "vendor", "organization", "organisation", "agency", "ministry", "firm"}
    | {"auditor", "tpm", "donor", "donors", "company", "supplier", "ngo", "association", "foundation"}
)
ORG_KEYS = {"partners": frozenset({"name", "short_name", "alternate_name"})}
NAMING_WORDS = frozenset(
    {"name", "names", "title", "short", "alternate", "legal", "official", "display", "common", "local"}
    | {"english", "arabic", "french", "implementing", "parent", "main"}
)
# Keys that wrap a list or an object of things: "sections_data" holds sections, "offices_data" offices
CONTAINER_WORDS = frozenset({"data", "info", "details", "list", "set", "items", "records"})
# A name inside an object ("approval": {"display_name": ..}), read with its parent's key
NAME_LIKE = frozenset({"name", "names", "label", "labels", "fullname", "displayname"})
# "<word>_name" names a person unless the word is a thing ("partner_name", "cp_output_name", "tpm_name");
# a name inside an object is read with its parent's key ("location": {"name": ..} and "sections_data":
# [{"name": ..}] are things, "approval": {"name": ..} a person). A record may name a person by a role
# catalogue.person_key does not know ("visitor_name", "reporter_name", "pme": {"name": ..}): what is not
# a known thing (or its plural) is taken for a person.
THING_WORDS = (
    PLACE_WORDS
    | ORG_WORDS
    | frozenset(
        {"partner", "vendor", "location", "site", "office", "section", "donor", "grant", "fund", "indicator"}
        | {"activity", "activities", "programme", "program", "project", "country", "countries", "output"}
        | {"outcome", "result", "cluster", "ministry", "organization", "organisation", "agency", "school"}
        | {"sector", "district", "governorate", "admin", "level", "area", "file", "document", "attachment"}
        | {"dataset", "table", "task", "status", "type", "category", "categories", "currency", "business"}
        | {"workspace", "assessment", "engagement", "audit", "firm", "intervention", "pd", "agreement"}
        | {"report", "question", "method", "entity", "entities", "facility", "facilities", "centre", "center"}
        | {"region", "caza", "cadaster", "village", "town", "city", "cities", "municipality", "unit"}
        | {"disaggregation", "label", "blueprint", "reportable", "frequency", "frequencies", "modality"}
        | {"modalities", "source", "display", "short", "long", "code", "official", "legal", "common", "local"}
        | {"english", "arabic", "french", "zone", "kind", "group", "classification", "rating", "risk", "mode"}
        | {"sex", "gender", "age", "nationality", "nationalities", "disability", "disabilities", "item"}
        | {"supply", "supplies", "product", "service", "trip", "travel", "purpose", "bank", "module"}
        | {"checklist", "option", "tool", "plan", "objective", "event", "training", "account", "cp", "tpm"}
        | {"ll", "alternate", "spd", "ssfa", "ip", "cso", "org", "company", "companies", "supplier", "parent"}
        | {"month", "visit", "link", "budget", "goal", "target", "category", "sub", "subsector", "branch"}
        | {"premise", "premises", "building", "camp", "shelter", "district", "street", "area", "geo"}
        | {"reservation", "choice", "answer", "field", "column", "theme", "tag", "standard", "subject"}
        | {"line", "header", "series", "year", "quarter", "period", "value", "values", "domain", "criteria"}
        | {"hospital", "clinic", "university", "institution", "department", "phase", "session", "round"}
        | {"stage", "course", "campaign", "workplan", "funder", "grantee", "space", "kit", "package"}
    )
)
# PRP's {"v": .., "d": .., "c": ..}: its parts are numbers (summed as such by :func:`_numeric`)
PRP_PARTS = frozenset({"v", "d", "c"})
# PRP's {"v": .., "d": .., "c": ..} written as text (Python's quotes or JSON's): one part, as a number
PART = r"['\"]%s['\"]\s*:\s*['\"]?(-?(0|[1-9][0-9]*)(\.[0-9]+)?)(?![0-9])"
OBJECT = r"^\s*\{.*\}\s*$"  # an object written as text, read back as one (:func:`_parsed`)
# Partner report values repeat on every location row, report and indicator: adding them is never right
NO_SUM = {
    "partner_reports": "Partner report values repeat on every location, report and indicator row",
    "prp_indicator_reports": "PRP indicator report values repeat across reports and indicators",
    # one row per indicator, location and disaggregation: a sum of targets counts each one many times
    "pd_indicators": "PD indicator rows repeat the indicator's target on every location and disaggregation",
}
NO_SUM_HINT = (
    "{why}, so they are never added up. Use pd_indicator_progress for each indicator's target and what "
    "a partner reached (Reached, per indicator, by PRP's own rules), or list the records without sum."
)


class QueryError(ValueError):
    """A question the data cannot answer as asked (unknown dataset or field, bad filter)."""


@dataclass
class Dataset:
    name: str
    description: str
    model: type[Model]
    date_field: str
    base: QuerySet
    spec: catalogue.Source | None = None  # for DatamartDocument datasets: which links the sync makes

    @property
    def partner_link(self) -> bool:
        if self.spec is not None:  # the partner comes from the record or from its programme document
            return any(self.spec.partner) or any(self.spec.intervention)
        return _has_field(self.model, "partner")

    @property
    def pd_link(self) -> str:
        if self.spec is not None:
            return "intervention" if any(self.spec.intervention) else ""
        if _has_field(self.model, "intervention"):
            return "intervention"
        if _has_field(self.model, "interventions"):
            return "interventions"
        return ""


def _has_field(model: type[Model], name: str) -> bool:
    return any(f.name == name for f in model._meta.get_fields())


def dataset(name: str) -> Dataset:
    if name in catalogue.TYPED:
        model_name, description, date_field = catalogue.TYPED[name]
        model = apps.get_model("datamart", model_name)
        return Dataset(name, description, model, date_field, model.objects.all())
    if name in catalogue.DOCUMENTS:
        spec = catalogue.DOCUMENTS[name]
        base = DatamartDocument.objects.filter(dataset=name)
        return Dataset(name, spec.description, DatamartDocument, "date" if spec.date else "", base, spec)
    raise QueryError(f"Unknown dataset '{name}'. Use etools_datasets to list them.")


# ------------------------------------------------------------------------------------ outputs
def shorten(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, list):
        items = [shorten(v) for v in value[:MAX_LIST]]
        return items + [f"… {len(value) - MAX_LIST} more"] if len(value) > MAX_LIST else items
    if isinstance(value, dict):
        return {k: shorten(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


def _thing(word: str) -> bool:
    """A word of ``THING_WORDS`` or its plural ("offices", "businesses", "agencies"; never a role named
    after a thing: "visitor", "reporter", "agent")."""
    if word in THING_WORDS:
        return True
    if not word.endswith("s"):
        return False
    singular = {
        word[:-1],
        word[:-2] if word.endswith("es") else "",
        word[:-3] + "y" if word.endswith("ies") else "",
    }
    return bool(singular & THING_WORDS)


@functools.lru_cache(maxsize=8192)
def _words(key: Any) -> tuple[str, ...]:
    """The words of a key's last part ("a.b": "b"), camelCase and acronyms split (``catalogue.key_words``)."""
    return tuple(catalogue.key_words(key))


def _bare(words: tuple[str, ...]) -> tuple[str, ...]:
    """A key's words without the words of a container at its end ("sections_data": "sections")."""
    while len(words) > 1 and words[-1] in CONTAINER_WORDS:
        words = words[:-1]
    return words


@functools.lru_cache(maxsize=8192)
def _thing_key(key: Any) -> bool:
    """A key whose objects are things, not people: "location", "sections_data", "cp_outputs_data",
    "budget" (not "approval", "review", "sent_back")."""
    words = _bare(_words(key))
    return bool(words) and (_thing(words[-1]) or catalogue._not_a_person(list(words)))


@functools.lru_cache(maxsize=8192)
def _named(key: Any) -> bool:
    """A key that holds a person's name by a role :func:`_fm_person` does not know: "<word>_name" where the
    word is not a thing (``THING_WORDS``: "partner_name" is a partner, "tpm_name" a firm)."""
    words = list(_words(key))
    if len(words) < 2 or words[-1] not in ("name", "names") or catalogue._not_a_person(words):
        return False
    return not _thing(words[-2])


def _fm_person(key: Any) -> bool:
    """A key that holds a person, by the rule Monitoring insights also uses (``catalogue.holds_person``)."""
    return catalogue.holds_person(str(key or ""))


def _name_like(key: Any) -> bool:
    """A name key ("name", "display_name", "full_name", "label"): of a person, or of a thing."""
    words = set(_words(key))
    return bool(words & NAME_LIKE) and words <= NAME_LIKE | {"display", "full"}


@functools.lru_cache(maxsize=8192)
def _person(key: Any, parent: Any = None) -> bool:
    """A key that holds a person, for Ask: :func:`_fm_person`, :func:`_named`, or a name inside an object
    whose own key is not a thing (:func:`_thing_key`: "approval": {"display_name": ..};
    "location": {"name": ..} and "offices_data": [{"name": ..}] stay)."""
    if _fm_person(key) or _named(key):
        return True
    return parent is not None and _name_like(key) and not _thing_key(parent)


@functools.lru_cache(maxsize=8192)
def _names_person(key: Any, parent: Any = None) -> bool:
    """A key that names a person (:func:`_person`), not one that holds a text a person wrote ("comments",
    "remarks", "report_note": ``catalogue.written_by_people``, as Monitoring insights'
    ``privacy.names_person``): only these teach the names taken out of texts."""
    return _person(key, parent) and not catalogue.written_by_people(key)


# The parts of a name split over several keys ("signatory_first_name", "firstName", "surname")
NAME_PART_WORDS = {
    **{part: part for part in catalogue.NAME_PARTS},
    "firstname": "first",
    "lastname": "last",
    "surname": "last",
    "givenname": "given",
    "familyname": "family",
    "fullname": "full",
}
NAME_ORDER = ("first", "given", "middle", "last", "family")


@functools.lru_cache(maxsize=8192)
def _name_part(key: Any) -> tuple[str, str] | None:
    """For a key that holds a part of a name: the part ("first", "last"...) and the person's prefix
    ("unicef_signatory")."""
    words = _words(key)
    part = next((NAME_PART_WORDS[w] for w in words if w in NAME_PART_WORDS), None)
    if part is None:
        return None
    prefix = "_".join(w for w in words if w not in NAME_PART_WORDS and w not in ("name", "names"))
    return part, prefix


@functools.lru_cache(maxsize=8192)
def _name_key(key: Any) -> bool:
    """Inside an object that holds a person: a key that holds its name (not its title, section or office)."""
    words = set(_words(key))
    return bool(words & (set(NAME_PART_WORDS) | NAME_LIKE | {"display"}))


def _own_names(value: Any, subtract: bool = True) -> frozenset[str]:
    """The person names (and e-mail addresses) a record holds under its keys that name a person
    (:func:`_names_person`), at any depth and in objects written as text, so that the same names are also
    taken out of its texts when NeuroDB does not know them. A split name is put together per person
    ("unicef_signatory_first_name" with "unicef_signatory_last_name", "Layla" with "Abi Saab"); inside an
    object that names a person only its name keys count (not its title, section or office); the texts
    people wrote (comments, notes, remarks) teach no name; the section and donor names NeuroDB never takes
    for people are left out (``people.not_people``)."""
    from neurodb.watch import people

    found: set[str] = set()
    _learn(value, found, False, None)
    if found and subtract:
        found -= people.not_people()
    return frozenset(found)


def _learn(value: Any, found: set[str], holds: bool, parent: Any) -> None:
    if isinstance(value, dict):
        split: dict[str, dict[str, str]] = {}
        for key, item in value.items():
            naming = _names_person(key, parent) or (holds and _name_key(key))
            if not naming and not isinstance(item, dict | list):
                if not isinstance(item, str) or item.lstrip()[:1] not in ("{", "["):
                    continue  # a number, or a text no person's key holds: no name to learn
            part = _name_part(key) if naming and isinstance(item, str) else None
            if part and item.strip():
                split.setdefault(part[1], {})[part[0]] = item
            _learn(item, found, naming, key)
        for parts in split.values():
            ordered = [parts[p] for p in NAME_ORDER if p in parts]
            if len(ordered) >= 2:
                found |= _forms(" ".join(ordered))
    elif isinstance(value, list):
        for item in value:
            _learn(item, found, holds, parent)
    elif isinstance(value, str) and value.strip():
        parsed = _parsed(value)
        if parsed is OBJECT_WITHHELD:  # never sent: still, the names its pairs hold are learnt
            for _, key, _, item in _PAIR.findall(value):
                if _names_person(key, parent) or (holds and _name_key(key)):
                    found |= _forms(item)
        elif parsed is not value:
            _learn(parsed, found, holds, parent)
        elif holds:
            found |= _forms(value)


@functools.lru_cache(maxsize=65536)
def _forms(value: str) -> frozenset[str]:
    """The forms a name (and the e-mail addresses) written in ``value`` is looked for in: the same
    people are named in many records, read once."""
    from neurodb.watch import people

    return frozenset(people.name_forms(value) | people.emails_in(value))


def _parsed(value: Any) -> Any:
    """An object or a list written as text, read back (JSON, or Python's own quotes as the Datamart
    sometimes writes them, values such as ``Decimal('120.00')`` and ``datetime.date(2024, 1, 1)`` too:
    :func:`_python`), so that the keys holding a person inside it are dropped as in a record; an object
    that cannot be read is ``OBJECT_WITHHELD``; any other value as it is."""
    if not isinstance(value, str) or value.lstrip()[:1] not in ("{", "["):
        return value
    try:
        found = json.loads(value)
    except ValueError:
        found = _python(value)
    return found if isinstance(found, dict | list) or found is OBJECT_WITHHELD else value


OBJECT_WITHHELD = "[object withheld]"
# A text that starts as an object with a quoted key does ("{'name': ..", "[{'primary_traveler': ..",
# '{"a": ..'): one that cannot be read back is withheld whole, never sent with the people it may name
# ("[Draft] report" and "[1, 2" are texts)
OBJECT_SHAPE = re.compile(r"""\s*[\[{(]\s*[\[{(]?\s*['"][^'"\n]{1,80}['"]\s*:""")
# What Python writes for a value it has no literal for ("tzinfo=<UTC>", "<DstTzInfo 'Asia/Beirut' ...>")
_REPR = re.compile(r"<[^<>\n]{1,200}>")
# The key and the text of each pair of an object that cannot be read ("'primary_traveler': 'Rami ..'")
_PAIR = re.compile(r"""(['"])([^'"\n]{1,80})\1\s*:\s*(['"])([^'"\n]{1,200})\3""")


def _python(text: str) -> Any:
    """An object written with Python's quotes, read back: its literals as they are, and a value written
    as a call (``Decimal('120.00')``, ``datetime.date(2024, 1, 1)``, ``UUID('..')``) as its text
    ("120.00", "2024-01-01"); what Python writes for a value it has no literal for (``<UTC>``) as
    nothing. One that still cannot be read is ``OBJECT_WITHHELD`` when it has an object's keys
    (``OBJECT_SHAPE``), else the text as it is."""
    source = text.strip()
    if len(source) <= 100_000:
        for attempt in (source, _REPR.sub("None", source)):
            try:
                return _literal(ast.parse(attempt, mode="eval").body)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
    return OBJECT_WITHHELD if OBJECT_SHAPE.match(text) else text


def _literal(node: ast.AST, called: bool = False) -> Any:
    """A literal of Python's written text, as JSON would hold it: tuples and sets as lists, a call as its
    text (:func:`_called`), and a name only among a call's values ("tzinfo=UTC"; "[Draft]" is a text)."""
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        raise ValueError("not a literal")  # "[...]", a complex number
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):  # "{**other}"
            raise ValueError("not a literal")
        out = {}
        for key, item in zip(node.keys, node.values, strict=True):
            found = _literal(key)
            out[found if isinstance(found, str | int | float | bool) or found is None else str(found)] = (
                _literal(item)
            )
        return out
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [_literal(item) for item in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        number = _literal(node.operand)
        if isinstance(number, int | float) and not isinstance(number, bool):
            return -number if isinstance(node.op, ast.USub) else number
    if isinstance(node, ast.Call):
        return _called(node)
    if called and isinstance(node, ast.Name | ast.Attribute):
        return _dotted(node)
    raise ValueError("not a literal")


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    raise ValueError("not a name")


def _called(node: ast.Call) -> str:
    """A value written as a call, as its text: a date or a time as ISO ("2024-01-01"), a call of one
    value as that value ("Decimal('120.00')": "120.00"), any other as its name and values."""
    name = _dotted(node.func).rsplit(".", 1)[-1]
    values = [_literal(item, True) for item in node.args]
    if name in ("date", "datetime") and values and all(type(v) is int for v in values):
        try:
            return (datetime.date if name == "date" else datetime.datetime)(*values).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    if len(values) == 1 and not node.keywords and isinstance(values[0], str | int | float):
        return str(values[0])
    named = [f"{kw.arg}={_literal(kw.value, True)}" for kw in node.keywords if kw.arg]
    return f"{name}({', '.join([*map(str, values), *named])})"


@dataclass(frozen=True)
class Rules:
    """How a call cleans what Ask is sent: the names NeuroDB knows (one set for every record of the call,
    so that ``people``' caches hold), the dataset (field monitoring's rules for ``catalogue.FM_PRIVATE``),
    and ``whole``: field monitoring's long texts cleaned, not withheld (a search looks in them; they are
    never sent)."""

    names: frozenset[str]
    dataset: str = ""
    whole: bool = False
    # the keys looked at (:func:`_key_withheld`): the same keys in every record of a call
    keys: dict[str, bool] = dataclasses.field(default_factory=dict, compare=False, repr=False)

    @property
    def fm(self) -> bool:
        return self.dataset in catalogue.FM_PRIVATE


def _phone_number(text: str, fm: bool) -> bool:
    """A number written alone with a phone's shape (``WHOLE_PHONE``; in field monitoring, any 7 or 8
    digits, as before)."""
    if WHOLE_PHONE.fullmatch(text):
        return True
    return fm and bool(re.fullmatch(FM_WHOLE_PHONE, text))


def _phones(text: str, fm: bool) -> str:
    """``text`` without phone numbers: in field monitoring any Lebanese number (``people.PHONE``),
    elsewhere those with a phone's prefix (``people.PREFIXED_PHONE``: "USD 1250000" and "1 500 000" are
    amounts), and every international number. A number with a phone's prefix is never taken for an
    amount, next to a currency or not ("LBP 71.500.000" is withheld), unless its last six digits are 0
    ("LBP 70.000.000", "81 000 000"): no phone ends so."""
    from neurodb.watch import people

    def withheld(match: re.Match) -> str:
        found = match.group(0)
        if not fm and re.sub(r"\D", "", found).endswith("000000"):
            return found
        return people.PHONE_WITHHELD

    text = (people.PHONE if fm else people.PREFIXED_PHONE).sub(withheld, text)
    return people.INTL_PHONE.sub(people.PHONE_WITHHELD, text)


def _clean_text(
    value: str, rules: Rules, own: frozenset[str], keeps: bool = False, amount: bool = False
) -> str:
    """A text on one line without e-mail addresses (whole, cut short, spaced or spelled out), links,
    known person names and the record's own (NeuroDB Watch's ``redact.text``), phone numbers
    (:func:`_phones`) and names written after a title ("Mrs Layla Saab", "السيدة ..."; after Sheikh or Dr
    only outside a key that holds a place or an organisation: ``keeps``, and always in field monitoring);
    field monitoring's texts then cut at ``FM_TEXT_MAX`` (after: a marker can be longer than what it
    replaces). A number written alone stays unless it has a phone's shape (:func:`_phone_number`), and
    always under a key that holds an amount (``amount``)."""
    from neurodb.watch import people, redact  # lazy: watch already reads the datamart lazily

    value = people.visible(value)
    if PLAIN_NUMBER.fullmatch(value):
        return value if amount or not _phone_number(value, rules.fm) else people.PHONE_WITHHELD
    value = " ".join(value.split())  # on one line, as NeuroDB Watch's redact.text reads it
    # each pattern only where what it needs is written (an at sign, "at", a digit, an Arabic letter): the
    # same text, much faster on the thousands of records a search checks
    text = people.SPELLED_EMAIL.sub(people.EMAIL_WITHHELD, value) if _SPELLED_HINT.search(value) else value
    text = redact.text(text, UNCUT, rules.names, own)
    if _AT_HINT.search(text):
        text = AT_WORD.sub(people.EMAIL_WITHHELD, text)
    if _DIGIT.search(text):
        text = _phones(text, rules.fm)
    text = people.PERSON_TITLE_NAME.sub(people.NAME_WITHHELD, text)
    arabic = not text.isascii()
    if arabic:
        text = people.ARABIC_PERSON_TITLE_NAME.sub(people.NAME_WITHHELD, text)
    if rules.fm or not keeps:
        text = people.HONORIFIC_NAME.sub(people.NAME_WITHHELD, text)
        text = people.ARABIC_TITLE_NAME.sub(people.NAME_WITHHELD, text) if arabic else text
    return text[: catalogue.FM_TEXT_MAX] if rules.fm and not rules.whole else text


# The language a name is written in, after its key ("partner_name_ar")
LANGUAGE_WORDS = frozenset({"ar", "en", "fr"})


def _place_or_org(words: tuple[str, ...]) -> bool:
    """Words that name a place or an organisation ("location", "partner_name", "locations_data"), and
    nothing else ("site_feedback" and "other_partners_involved" hold texts), or the name of one with a
    word that says which before it, or its language after it ("assessment_partner_name": the partner a
    PSEA answer is about; "partner_name_ar")."""
    kinds = PLACE_WORDS | ORG_WORDS
    known = [w in kinds or w.rstrip("s") in kinds for w in words]
    if any(known) and all(k or w in NAMING_WORDS for k, w in zip(known, words, strict=True)):
        return True
    named = [w for w in words if w not in LANGUAGE_WORDS]
    return any(known) and named[-1:] in (["name"], ["names"])


@functools.lru_cache(maxsize=8192)
def _keeps_titles(key: Any, parent: Any = None, dataset: str = "") -> bool:
    """A key that holds the name of a place or an organisation, where a name after Sheikh or Dr is kept:
    its words name one (:func:`_place_or_org`), or it is a name key ("name", "short_name", "title")
    inside an object of one ("location": {"name": ..}), of a partner's record (``ORG_KEYS``) or of the
    place datasets."""
    words = _bare(_words(key))
    if not words:
        return False
    if set(words) <= NAMING_WORDS:
        if dataset in PLACE_DATASETS:
            return True
        if parent is None:
            return key in ORG_KEYS.get(dataset, ())
        return _place_or_org(_bare(_words(parent)))
    return _place_or_org(words)


@functools.lru_cache(maxsize=8192)
def _holds_amount(key: Any) -> bool:
    return catalogue.holds_amount(key)


def _measure_key(key: Any, fm: bool) -> bool:
    """A key whose number is sent as it is, never taken for a phone: PRP's parts ("v", "d", "c"), and
    outside field monitoring a key that holds an amount (:func:`_holds_amount`). Field monitoring holds
    answers, not amounts: its "value", "answer_value" or "values" is what a monitor wrote, where any 7
    or 8 digits are a phone. One rule for what is sent, searched, compared, grouped and summed."""
    return key in PRP_PARTS or (not fm and _holds_amount(key))


MARKERS = ("[name withheld]", "[email withheld]", "[link withheld]", "[phone withheld]", OBJECT_WITHHELD)


@functools.lru_cache(maxsize=8192)
def _key_shape_withheld(key: str, fm: bool) -> bool:
    """A key cleaned as a text of its dataset would be, with every title: a phone by the dataset's rule
    (a "1234567" key holds a location's figures outside field monitoring)."""
    cleaned = _clean_text(key, Rules(frozenset(), "field_monitoring" if fm else "", whole=True), frozenset())
    return any(marker in cleaned and marker not in key for marker in MARKERS)


def _key_withheld(key: Any, rules: Rules, own: frozenset[str]) -> bool:
    """A key that is itself an e-mail address, a link, a phone number, a name after a title or a person
    name NeuroDB knows or the record holds ("approvals": {"layla.saab@...": "approved"}): left out with
    its value."""
    from neurodb.watch import people

    text = str(key)
    known = rules.keys.get(text)
    if known is None:
        known = rules.keys[text] = _key_shape_withheld(text, rules.fm) or bool(
            people.mentions(text, rules.names)
        )
    if known or not own or _key_letters(text).isdisjoint(people._shape(own)[1]):
        return known  # no name of the record's own starts in the key (its addresses are any address)
    return bool(people.mentions(text, frozenset(), own))


@functools.lru_cache(maxsize=8192)
def _key_letters(key: str) -> frozenset[str]:
    """The words of a key as names are compared (``people.words``)."""
    from neurodb.watch import people

    return frozenset(people.words(key))


def _safe(
    value: Any,
    rules: Rules,
    own: frozenset[str] = frozenset(),
    parent: Any = None,
    keeps: bool = False,
    amount: bool = False,
) -> Any:
    """A record's value as Ask may send it: dicts without the keys that hold a person (:func:`_person`)
    or that are themselves a person's name, address or phone (:func:`_key_withheld`), lists element by
    element, objects written as text read back first (:func:`_parsed`), texts cleaned by
    :func:`_clean_text` (with the record's ``own`` names; their length is left to :func:`shorten`,
    field monitoring's longer than ``FM_TEXT_MAX`` withheld), a number with a phone's shape withheld
    outside a key that holds an amount. Dates and yes/no pass as they are."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _person(key, parent) or _key_withheld(key, rules, own):
                continue
            titled = _keeps_titles(key, parent, rules.dataset)
            measure = amount or _measure_key(key, rules.fm)
            out[key] = _safe(item, rules, own, key, titled, measure)
        return out
    if isinstance(value, list):
        return [_safe(item, rules, own, parent, keeps, amount) for item in value]
    if isinstance(value, str):
        parsed = _parsed(value)
        if parsed is OBJECT_WITHHELD:
            return OBJECT_WITHHELD
        if parsed is not value:
            return _safe(parsed, rules, own, parent, keeps, amount)
        if rules.fm and not rules.whole and len(value) > catalogue.FM_TEXT_MAX:
            return catalogue.FM_TEXT_WITHHELD
        return _clean_text(value, rules, own, keeps, amount)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not amount and _phone_number(str(value), rules.fm):
            from neurodb.watch import people

            return people.PHONE_WITHHELD
    return value


def _cleaned(
    data: dict[str, Any], dataset: str, names: frozenset[str] | None = None, fields: list[str] | None = None
) -> dict[str, Any]:
    """A record as Ask may be sent it, before any shortening: contact keys and the keys that hold a person
    dropped, every text cleaned with the names NeuroDB knows and the names the record holds itself;
    field monitoring texts longer than ``FM_TEXT_MAX`` withheld; only the safeguarding keys of a PSEA
    answer."""
    data = catalogue.scrub(data or {})
    if dataset in catalogue.SAFEGUARDING:  # never a comment: only the keys that hold no text of a case
        data = {k: v for k, v in data.items() if k in catalogue.SAFEGUARDING_KEYS}
    own = _own_names(data)
    if fields:  # only what is sent is cleaned
        data = {f: data[f] for f in fields if f in data}
    return _safe(data, Rules(_known_names() if names is None else names, dataset), own)


def public(
    data: dict[str, Any],
    fields: list[str] | None = None,
    dataset: str = "",
    names: frozenset[str] | None = None,
) -> dict[str, Any]:
    """A record as Ask may send it (see the module's notes and :func:`_cleaned`), shortened; ``names``,
    the person names NeuroDB knows (read once by the caller for many records)."""
    data = _cleaned(data, dataset, names, fields)
    if fields:
        return {f: shorten(data.get(f)) for f in fields}
    return {k: shorten(v) for k, v in data.items() if k not in NOISE and v not in (None, "", [], {})}


def _refuse_personal(ds: Dataset, keys: list[str], texts: list[Any]) -> None:
    """No query may filter, group, sum, sort or select on a key that holds a person (or that is a
    person's name), nor look for a person's name (or an e-mail address) in the records. A PSEA answers
    query may use only ``catalogue.SAFEGUARDING_KEYS`` (and the partner, PD, year and month groups), and
    never a text search: a search would tell which records' comments hold a word."""
    if ds.name in catalogue.SAFEGUARDING:
        for key in keys:
            field = str(key or "").lstrip("-").partition("__")[0]
            if field and field not in catalogue.SAFEGUARDING_KEYS | GROUPS | {"date"}:
                raise QueryError(
                    f"'{field}' is not read for PSEA answers: only "
                    f"{', '.join(sorted(catalogue.SAFEGUARDING_KEYS))} can be queried."
                )
        if any(isinstance(t, str) and t.strip() for t in texts[:1]):
            raise QueryError("PSEA answers cannot be searched by text: filter on their standard or rating.")
        return
    from neurodb.watch import people

    fm = ds.name in catalogue.FM_PRIVATE
    where = "; use the Monitoring insights page" if fm else ""
    for key in keys:
        field = str(key or "").lstrip("-").partition("__")[0]
        if field and (_person(field) or _contact_key(field) or people.mentions(field)):
            raise QueryError(f"'{field}' holds personal data and cannot be queried{where}.")
    if any(isinstance(t, str) and t.strip() and people.mentions(t) for t in texts):
        kind = "Field monitoring records" if fm else "eTools records"
        raise QueryError(f"{kind} cannot be searched for a person{where}.")


PERSON_SEARCH_NOTE = (
    "Nothing is searched for a person's name or an e-mail address: people are never sent to the assistant."
)


# ------------------------------------------------------------------------------------ checking a match
def _text(value: Any) -> str:
    """A value as text to look in: a text as it is, anything else as its JSON, in lower case."""
    if isinstance(value, str):
        return value.casefold()
    return json.dumps(value, ensure_ascii=False, default=str).casefold()


def _contact_key(key: Any) -> bool:
    """A key ``catalogue.scrub`` drops (it names an e-mail address, a phone or a mobile)."""
    return any(part in str(key).lower() for part in catalogue.PERSONAL_KEYS)


NOT_SENT = object()  # a field Ask is not sent (a person's key, a contact key, a key that is a name)


def _sent_field(rules: Rules, record: dict[str, Any], own: frozenset[str], field: str) -> Any:
    """One field of a stored record as Ask is sent it (``NOT_SENT`` when it is not sent at all)."""
    if field not in record or _contact_key(field):
        return NOT_SENT
    return _safe({field: catalogue.scrub(record[field])}, rules, own).get(field, NOT_SENT)


def _same(sent: Any, value: Any) -> bool:
    """Whether ``value`` (a filter's) is what Ask is sent: a text compared without case, a number as
    written, an object or a list as read back from its text."""
    if isinstance(sent, str):
        return sent.casefold() == str(value).casefold()
    if isinstance(sent, bool) or sent is None or sent is NOT_SENT:
        return False
    if isinstance(sent, int | float):
        return str(sent) == str(value).strip()
    return _parsed(str(value)) == sent


# A text longer than this is checked around the searched words only, ``AROUND`` words on each side and
# more until a plain word on either side (a name, a title, an address or a phone reaches no further);
# the searched words then hold only letters, digits, spaces and plain punctuation
LONG_TEXT = 400
AROUND = 12
AROUND_NEEDLE = re.compile(r"[\w .,;!?'()/&%+#-]+")
_CHUNK = re.compile(r"\S+")
MEMO_MAX = 20_000
# What any cleaning rule needs written near a place the searched words are (:func:`_untouched`): a letter
# outside ASCII (an Arabic title, an accent, an invisible character, a full-width sign), an at sign, a
# full stop inside a word or between spaces ("x.org", "M.Sc", "x . y"), an "at" or a "dot" in brackets,
# "-at-", "dot", a link's scheme; and ``PHONE_DIGITS`` digits (no phone has fewer), a title's word, the
# first word of a known name or of one the record holds
_TOUCHED = re.compile(
    r"[^\x00-\x7f]|[@＠﹫]|\w\.\w|\s\.\s|[\[({<]\s*(?:at|dot|\.)\s*[\])}>]|-at-|\bdot\b"
    r"|\b(?:https?|ftp|mailto|javascript):",
    re.IGNORECASE,
)
PHONE_DIGITS = 7
_LETTERS = re.compile(r"[^\W\d_]+")


class Sent:
    """What Ask is sent of the records one search, filter or sort reads, field by field: the names
    NeuroDB knows read once (``rules``), each value cleaned once per set of the record's own names (a
    status repeats on thousands of records), and a long text cleaned only around the searched words."""

    def __init__(self, rules: Rules) -> None:
        self.rules = rules
        self.memo: dict[tuple[str, str, frozenset[str]], Any] = {}
        self.unsent_keys: dict[str, bool] = {}

    def unsent(self, field: str) -> bool:
        """A key never sent whatever it holds (a person's, a contact's): no need to look in it."""
        found = self.unsent_keys.get(field)
        if found is None:
            found = self.unsent_keys[field] = bool(_person(field) or _contact_key(field))
        return found

    def field(self, record: dict[str, Any], own: frozenset[str], field: str) -> Any:
        """``_sent_field``, once per distinct value and own names (none when no name of the record's own
        can start in the value or its key: it is then cleaned as without them, once for all records)."""
        if field not in record or _contact_key(field):
            return NOT_SENT
        item = record[field]
        text = item if isinstance(item, str) else json.dumps(item, sort_keys=True, default=str)
        if own and _without(own, item, field):
            own = frozenset()
        key = (field, text, own)
        found = self.memo.get(key, NOT_SENT)
        if found is NOT_SENT:
            if len(self.memo) >= MEMO_MAX:
                self.memo.clear()
            found = self.memo[key] = _sent_field(self.rules, record, own, field)
        return found

    def holds(
        self,
        record: dict[str, Any],
        own: frozenset[str] | _OwnNames,
        field: str,
        needle: str,
        key: bool,
    ) -> bool:
        """Whether a field, as Ask is sent it, holds ``needle`` (in lower case; with ``key``, its key
        included, as the database looks in a record's text): never a field that is not sent. ``own``,
        the record's own names, or how to read them: read only when a check needs them."""
        item = record.get(field)
        text = isinstance(item, str) and not (key and needle in field.casefold())
        if text and AROUND_NEEDLE.fullmatch(needle) and _parsed(item) is item:
            if _person(field) or _contact_key(field) or _key_withheld(field, self.rules, frozenset()):
                return False
            if _untouched(item, needle, self.rules, own, field):
                return True
            names = own() if isinstance(own, _OwnNames) else own
            if names and _key_withheld(field, self.rules, names):
                return False
            if len(item) > LONG_TEXT:
                keeps = _keeps_titles(field, None, self.rules.dataset)
                amount = _measure_key(field, self.rules.fm)
                return _holds_around(item, needle, self.rules, names, keeps, amount)
        names = own() if isinstance(own, _OwnNames) else own
        sent = self.field(record, names, field)
        return sent is not NOT_SENT and needle in _text({field: sent} if key else sent)


def _without(own: frozenset[str], value: Any, field: str) -> bool:
    """Whether no name of ``own`` can be found in ``value`` (its texts and keys, at any depth) nor in its
    key ``field``: the first word of none of them is written there (as ``people`` finds names, word by
    word)."""
    from neurodb.watch import people

    firsts = people._shape(own)[1]
    if not firsts.isdisjoint(_key_letters(field)):
        return False
    for text in _texts(value):
        found = (
            people.WORD.findall(text.lower())
            if text.isascii()
            else map(people.fold, people.WORD.findall(text))
        )
        if not firsts.isdisjoint(found):
            return False
    return True


def _texts(value: Any) -> Iterator[str]:
    """The texts of a value: itself, or its keys and texts at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _untouched(item: str, needle: str, rules: Rules, own: frozenset[str] | _OwnNames, field: str) -> bool:
    """Whether a text surely holds ``needle`` as Ask is sent it, without cleaning it: a place the needle
    is written where no cleaning rule can reach (``AROUND`` words on each side, and more until a word of
    letters only, so that no phone runs past) holds nothing any rule needs (``_TOUCHED``, fewer than
    ``PHONE_DIGITS`` digits, no title's word, no first word of a known name nor of the record's own,
    read only then; nor is the key the record's own name). Most records a common word finds are told so
    at once. False says only that the text must be cleaned to tell."""
    from neurodb.watch import people

    if not needle.isascii() or not _LETTERS.search(needle) or _in_markers(needle):
        return False
    if rules.fm and not rules.whole:  # cut and withheld at FM_TEXT_MAX: only the cleaned text tells
        return False
    chunks = item.split()
    text = " ".join(chunks)
    folded = text.casefold()
    if len(folded) != len(text):
        return False
    firsts = people._shape(rules.names)[1]
    at, seen, first = folded.find(needle), 0, 0
    while at >= 0:
        first += folded.count(" ", seen, at)  # the words before it, counted once along the text
        seen = at
        last = first + needle.count(" ")
        lo, hi = max(0, first - AROUND), min(len(chunks) - 1, last + AROUND)
        while lo > 0 and not _LETTERS.fullmatch(chunks[lo]):
            lo -= 1
        while hi < len(chunks) - 1 and not _LETTERS.fullmatch(chunks[hi]):
            hi += 1
        window = " ".join(chunks[lo : hi + 1]).casefold()
        if not _TOUCHED.search(window) and len(_DIGIT.findall(window)) < PHONE_DIGITS:
            words = set(_LETTERS.findall(window))
            if words.isdisjoint(people.TITLE_WORDS) and words.isdisjoint(firsts):
                if isinstance(own, _OwnNames):
                    names, own_firsts = own(), own.firsts()
                else:
                    names, own_firsts = own, people._shape(own)[1]
                if words.isdisjoint(own_firsts) and not (names and _key_withheld(field, rules, names)):
                    return True
        at = folded.find(needle, at + 1)
    return False


def _ordinary(chunks: list[str], i: int) -> bool:
    """A plain word with plain words on either side: no name, title, address or phone runs through it."""
    from neurodb.watch import people

    return all(
        people.WORD.fullmatch(chunk) and chunk.casefold() not in ("at", "dot")
        for chunk in chunks[max(0, i - 1) : i + 2]
    )


def _holds_around(
    text: str, needle: str, rules: Rules, own: frozenset[str], keeps: bool, amount: bool
) -> bool:
    """Whether a long text, cleaned as Ask is sent it, holds ``needle``: each place it is written is
    cleaned with ``AROUND`` words on each side (more until a plain word, :func:`_ordinary`), places
    close together as one part, so that every place is cleaned as the whole text would clean it, much
    faster than cleaning it all. A part's own edges hold no place: they may be cleaned otherwise."""
    from neurodb.watch import people

    text = " ".join(text.split())  # as the cleaning reads it
    folded = text.casefold()
    if len(folded) != len(text) or people.INVISIBLE.search(text) or "\x00" in text or _in_markers(needle):
        return needle in _clean_text(text, rules, own, keeps, amount).casefold()
    spans = [m.span() for m in _CHUNK.finditer(text)]
    chunks = [text[a:b] for a, b in spans]
    starts = [a for a, _ in spans]
    parts: list[list[int]] = []
    at = folded.find(needle)
    while at >= 0:
        lo = max(0, bisect.bisect_right(starts, at) - 1 - AROUND)
        hi = min(len(spans) - 1, bisect.bisect_right(starts, at + len(needle) - 1) - 1 + AROUND)
        while lo > 0 and not _ordinary(chunks, lo):
            lo -= 1
        while hi < len(spans) - 1 and not _ordinary(chunks, hi):
            hi += 1
        if parts and lo <= parts[-1][1]:  # overlaps the part before: one part
            parts[-1] = [min(parts[-1][0], lo), max(parts[-1][1], hi)]
        else:
            parts.append([lo, hi])
        at = folded.find(needle, at + 1)
    return any(
        needle in _clean_text(text[spans[lo][0] : spans[hi][1]], rules, own, keeps, amount).casefold()
        for lo, hi in parts
    )


def _in_markers(needle: str) -> bool:
    """A needle a marker could hold ("withheld", "name"): only the whole text tells."""
    return any(needle in marker for marker in MARKERS)


class _OwnNames:
    """A record's own names (:func:`_own_names`), read the first time a check needs them."""

    __slots__ = ("data", "first_words", "names")

    def __init__(self, data: dict[str, Any]) -> None:
        self.data, self.names, self.first_words = data, None, None

    def __call__(self) -> frozenset[str]:
        if self.names is None:
            self.names = _own_names(self.data)
        return self.names

    def firsts(self) -> frozenset[str]:
        """The first word of each of them (a name is only found where one is written)."""
        from neurodb.watch import people

        if self.first_words is None:
            self.first_words = people._shape(self())[1]
        return self.first_words


def _holds(sent: Sent, data: Any, checks: list[tuple[str, str | None, str]]) -> bool:
    """Whether a record, as Ask is sent it, holds the searched text and each ``__contains`` value (both
    in lower case), and is sent equal to each ``eq`` value. Only the fields that can hold them are
    checked: the filtered field, or for a search the fields sent whose stored text holds it; the record's
    own names are read from it whole (contact keys too: more names, never fewer), once, and only when a
    check needs them."""
    if not isinstance(data, dict):
        cleaned = _safe(catalogue.scrub(data), sent.rules)
        return all(kind == "search" and value in _text(cleaned) for kind, _, value in checks)
    own = _OwnNames(data)
    for kind, field, value in checks:
        if kind == "search":
            found = any(
                not sent.unsent(key)
                and (value in key.casefold() or value in _text(item))
                and sent.holds(data, own, key, value, True)
                for key, item in data.items()
            )
        elif kind == "contains":
            found = sent.holds(data, own, str(field), value, False)
        else:  # eq
            found = _same(sent.field(data, own(), str(field)), value)
        if not found:
            return False
    return True


def _stream(qs: QuerySet, most: int, what: str) -> Iterator[tuple[int, Any]]:
    """The records a check reads, a few thousand at a time (:func:`_rows`); more than ``most`` (counted
    on records Ask is told match: never on what is withheld): Ask is asked to narrow."""
    if qs.order_by().count() > most:
        raise QueryError(f"More than {most:,} records {what}.")
    return _rows(qs)


def _rows(qs: QuerySet) -> Iterator[tuple[int, Any]]:
    return qs.order_by().values_list("pk", "data").iterator(chunk_size=2000)


def _plain(rules: Rules, field: str, value: Any) -> bool:
    """A filter's value that no cleaning can change in any field it equals: a single word or number
    (a name has two words or more, a title one more) that is the same once cleaned however it is written
    ("open", "Halba", "2024"; not "71123456", "x@y.org", "amel.lb"): an ``eq`` or ``ne`` on it needs no
    check against what Ask is sent."""
    from neurodb.watch import people

    text = str(value)
    if len(people.WORD.findall(text)) > 1 or text.lstrip()[:1] in ("{", "["):
        return False
    if rules.fm and len(text) > catalogue.FM_TEXT_MAX:
        return False
    amount = _measure_key(field, rules.fm)
    return all(
        _clean_text(form, rules, frozenset(), False, amount) == form
        for form in {text, text.lower(), text.upper(), text.title()}
    )


def _confirm(
    ds: Dataset, before: QuerySet, qs: QuerySet, checks: list[tuple[str, str | None, Any]]
) -> QuerySet:
    """Only the records whose text, as Ask is sent it, holds the searched words and each ``__contains``
    value and is sent equal to each ``eq`` value, without those sent equal to an ``ne`` value: a search
    or a filter never finds what is withheld (a person under any key, at any depth, a name, an e-mail
    address or a phone number in a text), so it can never tell who a record names. The database narrows
    first (``qs``); whether there are too many records to check is told by ``before``, the records before
    any text or equality narrowed them (what is withheld plays no part in it): at most ``CONFIRM_MAX``
    for a text, ``EQUAL_MAX`` for an equality (each value cleaned once)."""
    if not checks:
        return qs
    most = EQUAL_MAX if all(kind in ("eq", "ne") for kind, _, _ in checks) else CONFIRM_MAX
    if before.order_by().count() > most:
        raise QueryError(
            f"More than {most:,} records to look in for this text: add a partner, a programme document, "
            "dates or a filter on a number, a date or a single word first."
        )
    sent = Sent(Rules(_known_names(), ds.name, whole=True))
    for _, field, value in [c for c in checks if c[0] == "ne"]:
        equal = _rows(_raw_equal(qs, str(field), value))
        sent_equal = [pk for pk, data in equal if _holds(sent, data, [("eq", field, value)])]
        qs = qs.exclude(pk__in=sent_equal)
    found = [
        (kind, field, value if kind == "eq" else str(value).strip().casefold())
        for kind, field, value in checks
        if kind != "ne"
    ]
    if not found:
        return qs
    kept = [pk for pk, data in _rows(qs) if _holds(sent, data, found)]
    return ds.base.filter(pk__in=kept)  # each kept record meets every filter: no text is read again


def _raw_equal(qs: QuerySet, field: str, value: Any) -> QuerySet:
    """The records whose stored ``field`` equals ``value`` (text without case, or yes/no)."""
    if isinstance(value, bool):
        return qs.filter(**{f"data__{field}": value})
    return qs.annotate(**{f"_t_{field}": KT(f"data__{field}")}).filter(**{f"_t_{field}__iexact": str(value)})


# ------------------------------------------------------------------------------------ people
PEOPLE_CACHE_KEY = "datamart-people-latest-v3"  # the names of the latest records, while none are stored
PEOPLE_LATEST = 500  # records per dataset read while no names are stored (before the first sync)
PEOPLE_LATEST_SECONDS = 3600


def datamart_people() -> list[str]:
    """The person names the eTools records hold under their keys that name a person, in every dataset
    (one of NeuroDB Watch's ``people.EXTRA_SOURCES``), so that a name held in one dataset (a trip's
    supervisor, an action point's location focal point, an intervention's split signatory) is also taken
    out of the texts of another (an audit's remarks), and in the eTools REST records kept for Raw eTools
    data. Read from every record at the end of each eTools Datamart sync, eTools REST sync and read of the
    eTools REST API into Raw eTools data (:func:`remember_people`) and stored in a table of their own
    (``RecordPeople``: no cull of the page-figure cache, no emptied figure and no restart loses them); while
    none are stored (before the first sync after a release), read from the latest ``PEOPLE_LATEST`` records
    of each dataset (kept an hour), so that no request waits long."""
    from django.core.cache import caches
    from django.db import DatabaseError, transaction

    from .models import RecordPeople

    try:
        with transaction.atomic():
            stored = RecordPeople.objects.filter(pk=1).values_list("names", flat=True).first()
    except DatabaseError:  # the table not made yet (a release before its migration)
        stored = None
    if stored is not None:
        return stored
    found = caches["shared"].get(PEOPLE_CACHE_KEY)
    if found is None:
        found = sorted(_read_datamart_people(PEOPLE_LATEST))
        caches["shared"].set(PEOPLE_CACHE_KEY, found, PEOPLE_LATEST_SECONDS)
    return found


def remember_people() -> list[str]:
    """Read the names :func:`datamart_people` gives from every record, and store them."""
    from .models import RecordPeople

    found = sorted(_read_datamart_people())
    RecordPeople.objects.update_or_create(pk=1, defaults={"names": found})
    return found


def _read_datamart_people(latest: int | None = None) -> set[str]:
    """The names of every dataset's records, and of the eTools REST records kept for Raw eTools data
    (``RestRecord``: a partner's staff named in its details is then also taken out of an engagement's texts;
    Ask never reads those records)."""
    from .models import RestRecord

    found: set[str] = set()
    queries = [
        dataset(name).base.order_by("-pk").values_list("data", flat=True)
        for name in catalogue.dataset_names()
        if name not in catalogue.SAFEGUARDING
    ]
    queries.append(RestRecord.objects.order_by("-pk").values_list("data", flat=True))
    for rows in queries:
        for data in (rows[:latest] if latest else rows).iterator(chunk_size=2000):
            found |= _own_names(catalogue.scrub(data or {}), subtract=False)
    return found


def _links(row: Model) -> dict[str, Any]:
    out: dict[str, Any] = {}
    partner = getattr(row, "partner", None)
    if partner is not None:
        out["neurodb_partner"] = {
            "id": partner.pk,
            "name": partner.name,
            "url": reverse("reports:partner_profile", args=[partner.pk]),
        }
    pd = getattr(row, "intervention", None)
    if pd is not None:
        out["neurodb_programme_document"] = {
            "number": pd.number,
            "url": reverse("reports:programme_detail", args=[pd.pk]),
        }
    return out


# ------------------------------------------------------------------------------------ catalogue
def describe(name: str | None = None) -> dict[str, Any]:
    """Without a name, every dataset with its description and size; with one, its fields and examples."""
    if not name:
        rows = []
        for key in catalogue.dataset_names():
            ds = dataset(key)
            rows.append(
                {
                    "dataset": key,
                    "description": ds.description,
                    "records": ds.base.count(),
                    "linked_to": [
                        x
                        for x, ok in (("partner", ds.partner_link), ("programme_document", ds.pd_link))
                        if ok
                    ],
                }
            )
        return {"datasets": rows, "not_read": catalogue.EXCLUDED}
    ds = dataset(name)
    fields: dict[str, Any] = {}
    private = name in catalogue.FM_PRIVATE
    rules = Rules(_known_names(), name)
    for data in ds.base.order_by("-pk").values_list("data", flat=True)[:25]:
        record = catalogue.scrub(data or {})
        if not isinstance(record, dict):
            continue
        own = _own_names(record)
        for key, value in record.items():
            if key in NOISE or key in fields and fields[key] not in (None, "", [], {}):
                continue
            if name in catalogue.SAFEGUARDING and key not in catalogue.SAFEGUARDING_KEYS:
                continue
            sent = _safe({key: value}, rules, own)  # no person's key at all; values as public() sends them
            if key not in sent:
                continue
            example = sent[key]
            if private:
                fields[key] = shorten(example)
            else:  # cleaned whole, then cut
                fields[key] = example[:80] if isinstance(example, str) else shorten(example)
    return {
        "dataset": name,
        "description": ds.description,
        "records": ds.base.count(),
        "date_field": ds.date_field or None,
        "filter_by_partner": ds.partner_link,
        "filter_by_programme_document": bool(ds.pd_link),
        "fields_with_example_values": fields,
    }


# ------------------------------------------------------------------------------------ filtering
def _known_names() -> frozenset[str]:
    from neurodb.watch import people

    return people.known_names()


def _field(name: str) -> str:
    if not FIELD.match(name or ""):
        raise QueryError(f"Invalid field name '{name}'.")
    return name


def _numeric(ds: Dataset, field: str) -> RawSQL:
    """The field as a number where it holds one and Ask is sent it: the Datamart sends decimals as
    strings, and PRP its values as ``{"v": .., "d": .., "c": ..}`` (an object, or its text): ``c``, else
    ``v``/``d`` when the denominator is a real one, else ``v``. A number Ask is not sent (with a phone's
    shape outside a key that holds an amount, :func:`_safe`) is left out, so that a sum or a comparison
    never tells it."""
    column = f'"{ds.model._meta.db_table}"."data"'
    value = f"({column}->%s)"
    text = f"({column}->>%s)"

    def part(key: str) -> tuple[str, list[str]]:
        """``key`` of the value: from the object, else from its text (``'c': 12.0``)."""
        sql = (
            f"CASE WHEN jsonb_typeof({value}) = 'object' AND ({value}->>%s) ~ %s "
            f"THEN ({value}->>%s)::numeric "
            f"WHEN jsonb_typeof({value}) = 'string' AND {text} ~ %s AND {text} ~ %s "
            f"THEN (substring({text} from %s))::numeric END"
        )
        pattern = PART % key
        params = [field, field, key, NUMBER, field, key, field, field, OBJECT, field, pattern, field, pattern]
        return sql, params

    c_sql, c_params = part("c")
    v_sql, v_params = part("v")
    d_sql, d_params = part("d")
    plain, plain_params = f"{text} ~ %s", [field, NUMBER]
    fm = ds.name in catalogue.FM_PRIVATE
    if not _measure_key(field, fm):
        phone = WHOLE_PHONE.pattern + (f"|{FM_WHOLE_PHONE}" if fm else "")
        plain, plain_params = f"{plain} AND {text} !~ %s", [*plain_params, field, f"^(?:{phone})$"]
    # The table name comes from the model; the field name, the keys and the patterns are bound parameters.
    return RawSQL(  # noqa: S611
        f"CASE WHEN {plain} THEN {text}::numeric "
        f"WHEN ({c_sql}) IS NOT NULL THEN ({c_sql}) "
        f"WHEN ({v_sql}) IS NOT NULL AND coalesce(({d_sql}), 0) NOT IN (0, 1) THEN ({v_sql}) / ({d_sql}) "
        f"ELSE ({v_sql}) END",
        (*plain_params, field, *c_params, *c_params, *v_params, *d_params, *v_params, *d_params, *v_params),
    )


def _filter(
    ds: Dataset, qs: QuerySet, key: str, value: Any, rules: Rules, scope: QuerySet
) -> tuple[QuerySet, tuple[str, str, Any] | None]:
    """The records a filter keeps, as far as the database can tell, and the check :func:`_confirm` makes
    against what Ask is sent (None when the database's answer is already that: a number, a date, a
    yes/no, a single plain word). ``scope``: the records of the partner, programme document and dates
    asked, where a field is judged to hold dates only (never narrowed by a text)."""
    field, _, op = key.partition("__")
    field, op = _field(field), op or "eq"
    if op not in OPERATORS:
        raise QueryError(f"Unknown operator '{op}' in '{key}'. Use one of {', '.join(OPERATORS)}.")
    text = KT(f"data__{field}")
    if op == "isnull":
        empty = Q(**{f"data__{field}__isnull": True}) | Q(**{f"data__{field}": ""})
        return (qs.filter(empty) if value else qs.exclude(empty)), None
    if op in ("gt", "gte", "lt", "lte"):
        if isinstance(value, int | float) and not isinstance(value, bool):
            alias = f"_n_{field}"
            return qs.annotate(**{alias: _numeric(ds, field)}).filter(**{f"{alias}__{op}": value}), None
        # comparing texts would spell out a withheld one, letter by letter: only whole dates, in a field
        # that holds nothing but dates (sent as they are stored) in the records of the partner, PD and
        # dates asked: never judged on records a search or another filter found, so that whether it is
        # refused tells nothing of what they hold
        if not DAY.match(str(value)):
            raise QueryError(f"'{key}': __gt, __gte, __lt and __lte take a number or a date (YYYY-MM-DD).")
        held = scope.annotate(**{f"_t_{field}": text})
        held = held.exclude(**{f"_t_{field}__isnull": True}).exclude(**{f"_t_{field}": ""})
        if held.exclude(**{f"_t_{field}__regex": DATE_VALUE}).exists():
            raise QueryError(f"'{key}': '{field}' does not hold dates only; compare dates or numbers.")
        return qs.annotate(**{f"_t_{field}": text}).filter(**{f"_t_{field}__{op}": str(value)}), None
    if op == "contains":
        found = qs.annotate(**{f"_t_{field}": text}).filter(**{f"_t_{field}__icontains": str(value)})
        return found, ("contains", field, str(value))
    if isinstance(value, bool) or _plain(rules, field, value):
        match = _raw_equal(qs, field, value)
        return (qs.exclude(pk__in=match.values("pk")) if op == "ne" else match), None
    if op == "ne":  # the records sent equal to the value are left out by _confirm
        return qs, ("ne", field, value)
    return _raw_equal(qs, field, value), ("eq", field, value)


def partners_matching(text: str) -> list[int]:
    text = text.strip()
    return list(
        PartnerOrganization.objects.filter(
            Q(name__icontains=text) | Q(short_name__icontains=text) | Q(vendor_number__iexact=text)
        ).values_list("pk", flat=True)[:50]
    )


def pds_matching(text: str) -> list[int]:
    return list(PCA.objects.filter(number__icontains=text.strip()).values_list("pk", flat=True)[:50])


def filtered(
    ds: Dataset,
    *,
    partner: str | None = None,
    programme_document: str | None = None,
    search: str | None = None,
    filters: dict[str, Any] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> QuerySet:
    qs = ds.base
    if partner:
        if not ds.partner_link:
            raise QueryError(f"'{ds.name}' is not linked to partners; filter on a partner field instead.")
        ids = partners_matching(partner)
        if not ids:
            raise QueryError(f"No partner matches '{partner}'.")
        qs = qs.filter(partner_id__in=ids)
    if programme_document:
        if not ds.pd_link:
            raise QueryError(
                f"'{ds.name}' is not linked to programme documents; filter on a PD field instead."
            )
        ids = pds_matching(programme_document)
        if not ids:
            raise QueryError(f"No programme document numbered like '{programme_document}'.")
        qs = qs.filter(**{f"{ds.pd_link}__in": ids}).distinct()
    if date_from or date_to:
        if not ds.date_field:
            raise QueryError(f"'{ds.name}' has no main date; filter on a date field with gte/lte instead.")
        if date_from:
            qs = qs.filter(**{f"{ds.date_field}__gte": date_from})
        if date_to:
            qs = qs.filter(**{f"{ds.date_field}__lte": date_to})
    scope, rules = qs, Rules(_known_names(), ds.name, whole=True)
    checks: list[tuple[str, str | None, Any]] = []
    texts: list[tuple[str, Any]] = []  # the filters the database narrows by the stored text
    for key, value in (filters or {}).items():
        narrowed, check = _filter(ds, qs, key, value, rules, scope)
        if check:
            checks.append(check)
            texts.append((key, value))
        else:
            qs = narrowed
    before = qs  # what is withheld plays no part in it: the count of records to check is taken here
    if search:
        qs = qs.annotate(_text=Cast("data", TextField())).filter(_text__icontains=search.strip())
        checks.insert(0, ("search", None, search))
    for key, value in texts:
        qs = _filter(ds, qs, key, value, rules, scope)[0]
    return _confirm(ds, before, qs, checks)


# ------------------------------------------------------------------------------------ queries
GROUPS = {"partner", "programme_document", "year", "month"}


def query(
    name: str,
    *,
    fields: list[str] | None = None,
    group_by: str | None = None,
    sum_field: str | None = None,
    order_by: str | None = None,
    limit: int = 20,
    **criteria: Any,
) -> dict[str, Any]:
    ds = dataset(name)
    filters = criteria.get("filters") or {}
    for key in (group_by, sum_field, (order_by or "").lstrip("-"), *(fields or [])):
        if key and "__" in key:  # a key inside an object: it may hold a person the object's key hides
            raise QueryError(f"'{key}': group, sum, sort and select on whole fields only.")
    _refuse_personal(
        ds,
        [*filters, *(fields or []), group_by or "", sum_field or "", order_by or ""],
        [criteria.get("search"), *filters.values()],
    )
    if sum_field and name in NO_SUM:
        raise QueryError(NO_SUM_HINT.format(why=NO_SUM[name]))
    qs = filtered(ds, **criteria)
    total = qs.count()
    if group_by:
        return {"dataset": name, "matching_records": total, **_groups(ds, qs, group_by, sum_field, total)}
    related = [f for f in ("partner", "intervention") if _has_field(ds.model, f)]
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    if order_by:
        descending, key = order_by.startswith("-"), order_by.lstrip("-")
        shown = _ordered(ds, qs.select_related(*related), key, descending, limit)
    else:
        shown = qs.select_related(*related).order_by(f"-{ds.date_field}" if ds.date_field else "-pk", "-pk")
    rows, size = [], 0
    selected = [_field(f) for f in fields or []] or None
    names = _known_names()
    for row in shown[:limit]:
        item = {"record": _key(row), **_links(row), **public(row.data, selected, dataset=name, names=names)}
        size += len(json.dumps(item, default=str))
        if rows and size > MAX_OUTPUT_CHARS:
            break
        rows.append(item)
    out = {"dataset": name, "matching_records": total, "rows": rows}
    if total > len(rows):
        out["note"] = (
            f"Showing {len(rows)} of {total}; narrow the query, choose fields, or group the records."
        )
    if sum_field:
        out["sum"] = {sum_field: _sum(ds, qs, sum_field)}
    return out


def _ordered(ds: Dataset, qs: QuerySet, key: str, descending: bool, limit: int) -> QuerySet | list[Model]:
    """The records in the order asked, as far as what Ask is sent tells it: by the main date, by a
    field's numbers as Ask is sent them (:func:`_numeric`: a withheld number sorts with the empty
    values, last), by a field that holds nothing but dates; any other field (a text, an object) in the
    order of its values as Ask is sent them (:func:`_sorted_as_sent`). A sort on the stored text would
    tell where a withheld name falls among the others, letter by letter, as comparing texts would."""
    if key == "date" and ds.date_field:
        return qs.order_by(f"-{ds.date_field}" if descending else ds.date_field, "-pk")
    field = _field(key)
    numbers = qs.annotate(_order=_numeric(ds, field))
    texts = qs.annotate(_order=KT(f"data__{field}"))
    held = texts.exclude(_order__isnull=True).exclude(_order="")
    if numbers.filter(_order__isnull=False).exists():
        ordered = numbers
    elif not held.exclude(_order__regex=DATE_VALUE).exists():  # dates only: sent as they are stored
        ordered = qs.annotate(_order=NullIf(KT(f"data__{field}"), Value("", output_field=TextField())))
    else:
        return _sorted_as_sent(ds, qs, field, descending, limit)
    order = F("_order").desc(nulls_last=True) if descending else F("_order").asc(nulls_last=True)
    return ordered.order_by(order, "-pk")


def _few_values(ds: Dataset, records: int, field: str) -> bool:
    """Whether a sort or a group of ``records`` records (as many as Ask is told match) cleans at most
    ``CONFIRM_MAX`` different values of ``field``: always when there are no more records than that,
    else when the whole dataset holds no more different values (a status repeats on thousands of
    records). Never counted on the records a query found, whose stored values also differ by what is
    withheld (an object's focal point, a name in a text)."""
    if records <= CONFIRM_MAX:
        return True
    values = ds.base.annotate(_value=KT(f"data__{field}")).order_by().values("_value").distinct()
    return values.count() <= CONFIRM_MAX


def _sorted_as_sent(ds: Dataset, qs: QuerySet, field: str, descending: bool, limit: int) -> list[Model]:
    """The first ``limit`` records by a field's value as Ask is sent it (a text without case, an object
    as its JSON; a value not sent last): at most ``EQUAL_MAX`` records, of at most ``CONFIRM_MAX``
    different values (:func:`_few_values`; each cleaned once)."""
    sent = Sent(Rules(_known_names(), ds.name))
    narrow = f"to sort by '{field}': sort by a date or a number, or filter first"
    if not _few_values(ds, qs.order_by().count(), field):
        raise QueryError(f"More than {CONFIRM_MAX:,} different values {narrow}.")
    keyed = []
    for pk, data in _stream(qs, EQUAL_MAX, narrow):
        value = sent.field(data, _own_names(data), field) if isinstance(data, dict) else NOT_SENT
        keyed.append((pk, _sort_key(value)))
    keyed.sort(key=lambda item: -item[0])  # the latest first among equal values
    present = sorted(
        (item for item in keyed if item[1] is not None), key=lambda item: item[1], reverse=descending
    )
    chosen = [pk for pk, _ in present][:limit]
    chosen += [pk for pk, value in keyed if value is None][: limit - len(chosen)]
    found = {row.pk: row for row in qs.filter(pk__in=chosen)}
    return [found[pk] for pk in chosen if pk in found]


def _sort_key(value: Any) -> tuple[int, Any] | None:
    if value is NOT_SENT or value in (None, "", [], {}):
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return 0, float(value)
    if isinstance(value, str):
        return 1, value.casefold()
    return 2, json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _key(row: Model) -> str:
    return row.record_key if isinstance(row, DatamartDocument) else str(row.datamart_id)


def _sum(ds: Dataset, qs: QuerySet, field: str) -> float | None:
    value = qs.order_by().aggregate(total=Sum(_numeric(ds, _field(field))))["total"]
    return float(value) if value is not None else None


def _groups(ds: Dataset, qs: QuerySet, group_by: str, sum_field: str | None, total: int) -> dict[str, Any]:
    """The records counted (and a field summed) per value of ``group_by``, the largest groups first. A
    record field's groups are its values as Ask is sent them (:func:`_sent_groups`)."""
    if group_by == "partner":
        if not ds.partner_link:
            raise QueryError(f"'{ds.name}' is not linked to partners; group by a partner field instead.")
        qs, label = qs.annotate(_g=Cast("partner__name", TextField())), "partner"
    elif group_by == "programme_document":
        if not ds.pd_link:
            raise QueryError(f"'{ds.name}' is not linked to programme documents.")
        qs, label = qs.annotate(_g=Cast(f"{ds.pd_link}__number", TextField())), "programme_document"
    elif group_by in ("year", "month"):
        if not ds.date_field:
            raise QueryError(f"'{ds.name}' has no main date to group by.")
        extract = ExtractYear(ds.date_field) if group_by == "year" else ExtractMonth(ds.date_field)
        qs, label = qs.annotate(_g=extract), group_by
    else:
        qs, label = qs.annotate(_g=KT(f"data__{_field(group_by)}")), ""
    annotations: dict[str, Any] = {"records": Count("pk", distinct=True)}
    if sum_field:
        annotations["total"] = Sum(_numeric(ds, _field(sum_field)))
    values = qs.order_by().values("_g").annotate(**annotations)
    if not label:  # a record field's values are the records' own words: sent as public() sends them
        label, groups = group_by, _sent_groups(ds, qs, values, group_by, total)
    else:
        largest = values.order_by("-records", "_g")[: MAX_GROUPS + 1]
        groups = [(g["_g"], g["records"], g.get("total")) for g in largest]
    rows = [
        {label: value, "records": records, **({f"sum_{sum_field}": shorten(summed)} if sum_field else {})}
        for value, records, summed in groups[:MAX_GROUPS]
    ]
    out: dict[str, Any] = {"groups": rows}
    if len(groups) > MAX_GROUPS:
        out["note"] = f"Only the {MAX_GROUPS} largest groups are listed."
    return out


def _sent_groups(
    ds: Dataset, qs: QuerySet, values: QuerySet, field: str, total: int
) -> list[tuple[Any, int, Any]]:
    """A record field's groups as Ask is sent its values: each stored value cleaned as public() sends it
    (with the names the grouped records hold under their person keys, read once for the call), the
    values sent alike counted (and summed) as one group, the largest first, then by the value sent. The
    stored values are never what orders or splits the groups: they also differ by what is withheld (an
    object's focal point, a name in a text). At most ``CONFIRM_MAX`` different values
    (:func:`_few_values`)."""
    if not _few_values(ds, total, field):
        raise QueryError(
            f"More than {CONFIRM_MAX:,} different values to group by '{field}': filter first, or group by "
            "partner, programme document, year or month."
        )
    rules = Rules(_known_names(), ds.name)
    held = qs.order_by("-pk").values_list("data", flat=True)[:GROUP_NAMES_MAX]
    own = frozenset().union(*(_own_names(catalogue.scrub(data or {})) for data in held))
    merged: dict[str, list[Any]] = {}
    for group in values.iterator():
        sent = _safe({field: group["_g"]}, rules, own).get(field)
        entry = merged.setdefault(json.dumps(sent, sort_keys=True, default=str), [sent, 0, None])
        entry[1] += group["records"]
        if group.get("total") is not None:
            entry[2] = group["total"] if entry[2] is None else entry[2] + group["total"]

    def order(item: tuple[str, list[Any]]) -> tuple[int, bool, Any, str]:
        key = _sort_key(item[1][0])
        return -item[1][1], key is None, key or (0, 0), item[0]  # last, the value sent exactly as written

    return [tuple(entry) for _, entry in sorted(merged.items(), key=order)]


def search_all(text: str) -> dict[str, Any]:
    """Which datasets mention a word or number (a place, a partner, a reference), with a few titles each.
    Only what Ask is sent is looked in, and only the latest ``SEARCH_WINDOW`` records of each dataset:
    the database finds those whose stored text holds it, latest first, and each is checked as Ask is
    sent it until ``SEARCH_SAMPLE`` hold it. A dataset is counted "at least SEARCH_SAMPLE" then, else by
    the records that hold it as sent ("at least" when the dataset has older records): what is said
    depends only on what Ask is sent and on the dataset's size, never on how many records hold the text
    where it is withheld (a person's name, a key never sent)."""
    text = (text or "").strip()
    if len(text) < 3:
        raise QueryError("Search for at least 3 characters.")
    from neurodb.watch import people

    hits, partly = [], False
    names = people.known_names()
    # a person's name (or an e-mail address) finds no record at all: it would tell what names them
    if people.mentions(text, names):
        return {"text": text, "datasets": [], "note": PERSON_SEARCH_NOTE}
    needle = text.casefold()
    for name in catalogue.dataset_names():
        if name in catalogue.SAFEGUARDING:  # a hit would tell which comments hold the word
            continue
        ds = dataset(name)
        sent = Sent(Rules(names, name, whole=True))
        window = ds.base
        oldest = list(ds.base.order_by("-pk").values_list("pk", flat=True)[SEARCH_WINDOW - 1 : SEARCH_WINDOW])
        older = bool(oldest) and ds.base.filter(pk__lt=oldest[0]).exists()
        if older:
            window, partly = ds.base.filter(pk__gte=oldest[0]), True
        found = window.annotate(_text=Cast("data", TextField())).filter(_text__icontains=text)
        kept: list[int] = []
        for pk, data in found.order_by("-pk").values_list("pk", "data").iterator(chunk_size=SEARCH_SAMPLE):
            if _holds(sent, data, [("search", None, needle)]):
                kept.append(pk)
                if len(kept) == SEARCH_SAMPLE:
                    break
        if not kept:
            continue
        shown = ds.base.filter(pk__in=kept).order_by("-pk")[:3]
        if name in catalogue.FM_PRIVATE:
            examples = [_fm_example(row, Rules(names, name)) for row in shown]
        else:
            examples = [_example(row, Rules(names, name)) for row in shown]
        hit: dict[str, Any] = {"dataset": name, "records": len(kept), "examples": examples}
        if len(kept) == SEARCH_SAMPLE:
            hit["note"] = f"At least {SEARCH_SAMPLE}; etools_query with this search counts them all."
        elif older:
            hit["note"] = (
                f"At least {len(kept)}: only the latest {SEARCH_WINDOW:,} records of the dataset were "
                "looked in; etools_query with this search counts them all."
            )
        hits.append(hit)
    hits.sort(key=lambda h: -h["records"])
    out: dict[str, Any] = {"text": text, "datasets": hits}
    if partly:
        out["note"] = (
            f"Datasets of more than {SEARCH_WINDOW:,} records were looked in for their latest "
            f"{SEARCH_WINDOW:,} only: etools_query with this search looks in all of a dataset's records."
        )
    return out


def _title_parts(row: Model) -> list[tuple[Any, int, bool]]:
    """What a record's title is made of (``__str__``, or the title keys a document was synced with):
    each part's value, where it is cut (0: whole) and whether it holds an organisation's name."""
    kind = type(row).__name__
    if isinstance(row, DatamartDocument):
        spec = catalogue.DOCUMENTS.get(row.dataset)
        data = row.data if isinstance(row.data, dict) else {}
        keys = [k for k in (spec.title if spec else ()) if not _person(k) and data.get(k) not in ("", None)]
        return [
            (data[k], 0, _keeps_titles(k, None, row.dataset)) for k in keys if isinstance(data[k], str | int)
        ]
    if kind == "ActionPoint":
        return [(row.reference_number, 0, False)] if row.reference_number else [(row.description, 80, False)]
    if kind == "AuditEngagement" and not row.reference_number:
        return [(row.engagement_type, 0, False), (row.partner_name, 0, True)]
    if kind == "PartnerAssessment":
        return [(row.type, 0, False), (row.partner_name, 0, True)]
    if kind == "PSEAAssessment":
        return [("PSEA", 0, False), (row.partner_name, 0, True)]
    if kind == "PartnerHACTYear":
        return [(row.partner_name, 0, True), (row.year, 0, False)]
    if kind == "PDIndicator":
        return [(row.title, 80, False)]
    if kind == "ReportedIndicator":
        return [
            (row.pd_reference_number, 0, False),
            (row.report_number, 0, False),
            (row.indicator, 60, False),
        ]
    if kind == "PDActivity":
        return [(row.code, 0, False), (row.name, 60, False)]
    return [(str(row), 0, False)]


def _example(row: Model, rules: Rules) -> str:
    """A record outside field monitoring as a search example: its title, each part cleaned whole (with
    the names the record itself holds under its person keys too) and then cut; a partner's name keeps
    its title as under ``partner_name``."""
    own = _own_names(catalogue.scrub(row.data or {}))
    parts = []
    for value, cut, keeps in _title_parts(row):
        if value in (None, ""):
            continue
        cleaned = _clean_text(str(value), rules, own, keeps)
        parts.append(cleaned[:cut] if cut else cleaned)
    if isinstance(row, DatamartDocument):
        return f"{row.dataset}: {' · '.join(parts) or row.record_key}"
    return " ".join(parts) or f"record {_key(row)}"


def _fm_example(row: Model, rules: Rules) -> str:
    """A field monitoring record as a search example: its visit reference, else its document title,
    never the record's text."""
    if isinstance(row, DatamartDocument):
        reference = (row.data or {}).get("monitoring_activity")
        text = reference if isinstance(reference, str | int) and str(reference).strip() else row.title
    else:
        text = getattr(row, "monitoring_activity", "") or getattr(row, "reference_number", "")
    return _safe(str(text or "").strip(), rules) or f"record {_key(row)}"


def record(name: str, key: str) -> dict[str, Any]:
    ds = dataset(name)
    related = [f for f in ("partner", "intervention") if _has_field(ds.model, f)]
    qs = ds.base.select_related(*related)
    if ds.model is DatamartDocument:
        row = qs.filter(record_key=str(key)).first()
    else:
        row = qs.filter(datamart_id=int(key)).first() if str(key).isdigit() else None
    if row is None:
        raise QueryError(f"No record '{key}' in '{name}'.")
    return {"dataset": name, "record": _key(row), **_links(row), "data": public(row.data, dataset=name)}


def linked_counts(*, partner: PartnerOrganization | None = None, pd: PCA | None = None) -> dict[str, int]:
    """How many records of each dataset are linked to a partner or a programme document."""
    counts: dict[str, int] = {}
    for name in catalogue.dataset_names():
        ds = dataset(name)
        if partner is not None and ds.partner_link:
            n = ds.base.filter(partner=partner).count()
        elif pd is not None and ds.pd_link:
            n = ds.base.filter(**{ds.pd_link: pd}).count()
        else:
            continue
        if n:
            counts[name] = n
    return counts
