"""Power BI's "Applied filters" line (Compare with Power BI): what an export's visual was filtered by, read
clause by clause into a :class:`Filter` that NeuroDB can apply to its own pages.

Power BI writes the line in the export's last row (or its first, on a chart's export): "Applied filters:"
followed by one clause per line, each in Power BI's own field names ("status is active", "reporting_year is
2026", "actual_value is less than or equal to 0 or is blank"). Each clause is read alone and reading never
raises.

* **The field** is the text before the first condition word; Power BI's fields are model names. The
  condition is tried in a fixed order, longest first, so "less than or equal to … or is blank" is never read
  as "less than".
* **Applied** are only the shapes the exports show, on the fields of :data:`FIELDS`: "field is value(s)" for
  every one of them, a comparison (with "or is blank") for ``actual_value``, a comparison for
  ``location_value``. Every other shape is one NeuroDB cannot apply.
* **What is kept.** A known field's values only when they are in its vocabulary (longest first, so a value
  holding ", " is never split wrongly); a field NeuroDB does not know keeps its name and condition words,
  never its value; a clause NeuroDB could not read keeps no text at all. The raw line is never kept: only the
  parsed filter and the clauses' descriptions, rebuilt from what was understood.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings

from neurodb.partnerships import statuses as pd_statuses

PREFIX = "Applied filters:"
# Power BI's fields NeuroDB knows, with the kind of value each takes
FIELDS = {
    "country_name": "country",
    "status": "pd_status",
    "reporting_year": "year",
    "section_name": "section",
    "report_status_label": "report_status",
    "is_period_latest": "boolean",
    "is_blueprint_canonical": "boolean",
    "actual_value": "number",
    "location_value": "number",
}
# Power BI's own fields, which NeuroDB's records may carry (read at run time: Raw eTools data's fields)
RECORD_FIELDS = ("is_period_latest", "is_blueprint_canonical")
# Power BI's report status labels -> NeuroDB's statuses (reached.status_of); Power BI's Due covers both
REPORT_STATUSES = {
    "Accepted": ("accepted",),
    "Submitted": ("submitted",),
    "Sent back": ("sent_back",),
    "Overdue": ("overdue",),
    "Due": ("due", "not_yet_due"),
    "Not Yet Due": ("not_yet_due",),
}
PD_STATUSES = (*pd_statuses.RUNNING, *pd_statuses.ENDED, *pd_statuses.NOT_A_PARTNERSHIP)
CONDITIONS = (" does not contain ", " does not start with ", " contains ", " starts with ", " is ")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
BLANK_TAIL = re.compile(r"^(?P<rest>.+?) (?P<join>or|and) is (?P<not>not )?blank$", re.I)
COMPARISON = re.compile(
    r"^is (?P<op>greater than or equal to|less than or equal to|greater than|less than) "
    r"(?P<n>-?\d+(?:\.\d+)?)$",
    re.I,
)
OPS = {
    "greater than or equal to": ">=",
    "less than or equal to": "<=",
    "greater than": ">",
    "less than": "<",
}
DATE_CONDITION = re.compile(r"^is (?P<words>on or after|on or before|after|before) .+$", re.I)
EMPTINESS = re.compile(r"^is (?P<words>not blank|blank|not empty|empty)$", re.I)
TEXT_CONDITION = re.compile(
    r"^(?P<words>does not contain|does not start with|contains|starts with) .+$", re.I
)
TWO_CONDITIONS = re.compile(r"^.+? and is .+$", re.I)
IS_NOT = re.compile(r"^is not .+$", re.I)
IS = re.compile(r"^is (?P<values>.+)$", re.I)
SEPARATORS = (", or ", ", and ", " or ", " and ", ", ")
BLANK_VALUE = "(Blank)"


@dataclass
class Clause:
    """One clause as NeuroDB understood it: its description (rebuilt, never the raw text), how NeuroDB applies
    it (``powerbi_words.CLAUSE_STATES``), and for a known field its values in NeuroDB's words."""

    words: str
    state: str
    field: str = ""
    values: list[Any] = dataclasses.field(default_factory=list)


@dataclass
class Filter:
    """An export's filter: the values NeuroDB applies, and every clause with how it is applied."""

    country_ok: bool | None = None  # None: no country clause
    pd_statuses: list[str] = dataclasses.field(default_factory=list)
    years: list[int] = dataclasses.field(default_factory=list)
    sections: list[str] = dataclasses.field(default_factory=list)
    report_statuses: list[str] = dataclasses.field(default_factory=list)  # Power BI's labels
    period_latest: bool | None = None
    canonical: bool | None = None
    actual_value: dict[str, Any] | None = None  # {"op": "<=", "value": 0.0, "or_blank": True}
    location_value: dict[str, Any] | None = None
    clauses: list[Clause] = dataclasses.field(default_factory=list)

    @property
    def others(self) -> list[Clause]:
        """The clauses NeuroDB does not apply: they can explain a difference."""
        return [c for c in self.clauses if c.state in ("cannot", "unknown_field", "unknown_value", "unread")]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Filter:
        data = dict(data or {})
        clauses = [Clause(**c) for c in data.pop("clauses", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known}, clauses=clauses)


# ----------------------------------------------------------------------------------------------- vocabulary
def _vocabulary(kind: str, sections: Iterable[str]) -> list[tuple[str, Any]]:
    """(the value as it may be written, NeuroDB's value) for a field's kind, longest first."""
    if kind == "country":
        country = settings.ETOOLS_DATAMART_COUNTRY
        found = [(country, country)]
    elif kind == "pd_status":
        found = [(s, s) for s in PD_STATUSES] + [(pd_statuses.label(s), s) for s in PD_STATUSES]
    elif kind == "section":
        found = [(" ".join(s.split()), " ".join(s.split())) for s in sections if s and s.strip()]
    elif kind == "report_status":
        found = [(label, label) for label in REPORT_STATUSES]
    elif kind == "boolean":
        found = [("True", True), ("False", False)]
    else:
        return []
    return sorted(set(found), key=lambda pair: (-len(pair[0]), pair[0]))


def _values(text: str, kind: str, sections: Iterable[str]) -> list[Any] | None:
    """The values of "is a, b, or c" in NeuroDB's words, or None when one is not in the field's vocabulary."""
    if kind == "year":
        found = []
        for piece in re.split(r", or |, and | or | and |, ", text):
            if not re.fullmatch(r"\d{4}", piece.strip()):
                return None
            found.append(int(piece.strip()))
        return found
    words = _vocabulary(kind, sections)
    out: list[Any] = []
    rest = text
    while rest:
        if rest[: len(BLANK_VALUE)].casefold() == BLANK_VALUE.casefold():
            out.append(None)
            rest = rest[len(BLANK_VALUE) :]
        else:
            hit = next(
                (pair for pair in words if rest[: len(pair[0])].casefold() == pair[0].casefold()), None
            )
            if hit is None:
                return None
            out.append(hit[1])
            rest = rest[len(hit[0]) :]
        if not rest:
            break
        separator = next((s for s in SEPARATORS if rest.startswith(s)), None)
        if separator is None:
            return None
        rest = rest[len(separator) :]
    return out or None


def _shown(values: list[Any]) -> str:
    """Values as Power BI lists them: "a", "a or b", "a, b or c"."""
    words = [BLANK_VALUE if v is None else str(v) for v in values]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


# --------------------------------------------------------------------------------------------- the clauses
def parse(text: Any, sections: Iterable[str] = ()) -> Filter:
    """The filter of an "Applied filters" line (None, an empty text or a text without the prefix give an
    empty filter). ``sections``: the section names NeuroDB knows (the vocabulary of ``section_name``)."""
    found = Filter()
    text = str(text or "")
    if not text.strip().startswith(PREFIX):
        return found
    sections = tuple(sections)
    for line in text.strip()[len(PREFIX) :].splitlines():
        line = " ".join(line.split())
        if line:
            try:
                _clause(found, line, sections)
            except Exception:  # noqa: BLE001 - a clause never stops the reading: it is one NeuroDB could not read
                found.clauses.append(Clause(words="", state="unread"))
    return found


def _split(line: str) -> tuple[str, str] | None:
    """(field, condition) at the first condition word."""
    hits = [(line.find(word), word) for word in CONDITIONS if line.find(word) > 0]
    if not hits:
        return None
    at, _word = min(hits)
    return line[:at], line[at + 1 :]


def _shape(condition: str) -> tuple[str, dict[str, Any]]:
    """The condition's shape, tried in a fixed order: (shape, its parts)."""
    blank = None
    tail = BLANK_TAIL.match(condition)
    if tail and COMPARISON.match(tail["rest"]):
        blank = {"join": tail["join"].lower(), "not": bool(tail["not"])}
        condition = tail["rest"]
    if m := COMPARISON.match(condition):
        return "comparison", {
            "op": OPS[m["op"].lower()],
            "value": float(m["n"]),
            "words": m["op"].lower(),
            "blank": blank,
        }
    if blank is not None:
        return "unread", {}
    if m := DATE_CONDITION.match(condition):
        return "date", {"words": f"is {m['words'].lower()}"}
    if m := EMPTINESS.match(condition):
        return "emptiness", {"words": f"is {m['words'].lower()}"}
    if m := TEXT_CONDITION.match(condition):
        return "text", {"words": m["words"].lower()}
    if TWO_CONDITIONS.match(condition):
        return "two", {"words": "two conditions"}
    if IS_NOT.match(condition):
        return "is_not", {"words": "is not"}
    if m := IS.match(condition):
        return "is", {"values": m["values"]}
    return "unread", {}


def _clause(found: Filter, line: str, sections: tuple[str, ...]) -> None:
    parts = _split(line)
    if parts is None or not FIELD_NAME.match(parts[0]):
        found.clauses.append(Clause(words="", state="unread"))
        return
    name, condition = parts
    shape, bits = _shape(condition)
    if shape == "unread":
        found.clauses.append(Clause(words="", state="unread"))
        return
    known = next((f for f in FIELDS if f.casefold() == name.casefold()), None)
    if known is None:  # its name and condition words only: never its value
        words = bits.get("words") or ("is" if shape == "is" else "")
        clause = Clause(words=f"{name} {words} …".replace("  ", " "), state="unknown_field", field=name)
        found.clauses.append(clause)
        return
    kind = FIELDS[known]
    if kind == "number":
        _number(found, known, shape, bits)
        return
    if shape != "is":
        found.clauses.append(Clause(words=f"{known} {bits['words']} …", state="cannot", field=known))
        return
    values = _values(bits["values"], kind, sections)
    if values is None and kind == "country":  # another country: its name is not kept
        found.country_ok = False
        found.clauses.append(Clause(words=f"{known} is …", state="other_country", field=known))
        return
    if values is None:
        found.clauses.append(Clause(words=f"{known} is …", state="unknown_value", field=known))
        return
    _known(found, known, kind, values)


def _number(found: Filter, name: str, shape: str, bits: dict[str, Any]) -> None:
    blank = bits.get("blank")
    if shape != "comparison" or (blank and (blank["not"] or blank["join"] != "or")):
        words = bits.get("words") or ("is" if shape == "is" else "")
        found.clauses.append(Clause(words=f"{name} {words} …", state="cannot", field=name))
        return
    if name == "location_value" and blank:
        found.clauses.append(
            Clause(words=f"{name} is {bits['words']} … or is blank", state="cannot", field=name)
        )
        return
    number = bits["value"]
    shown = int(number) if number.is_integer() else number
    words = f"{name} is {bits['words']} {shown}" + (" or is blank" if blank else "")
    value = {"op": bits["op"], "value": number, "or_blank": bool(blank)}
    setattr(found, name, value)
    found.clauses.append(Clause(words=words, state="applied", field=name, values=[value]))


def _known(found: Filter, name: str, kind: str, values: list[Any]) -> None:
    shown = _shown(values)
    if kind == "country":
        ok = all(v == settings.ETOOLS_DATAMART_COUNTRY for v in values)
        found.country_ok = ok if found.country_ok is None else (found.country_ok and ok)
        found.clauses.append(Clause(words=f"{name} is {shown}", state="applied", field=name, values=values))
        return
    state = "applied"
    if kind == "pd_status":
        found.pd_statuses = sorted({*found.pd_statuses, *(v for v in values if v)})
    elif kind == "year":
        found.years = sorted({*found.years, *values})
    elif kind == "section":
        found.sections = [*found.sections, *(v for v in values if v and v not in found.sections)]
    elif kind == "report_status":
        found.report_statuses = [
            *found.report_statuses,
            *(v for v in values if v and v not in found.report_statuses),
        ]
    elif kind == "boolean":
        if name == "is_period_latest":
            found.period_latest = bool(values[0]) if len(values) == 1 else None
        else:
            found.canonical = bool(values[0]) if len(values) == 1 else None
        state = "as_read"
    found.clauses.append(Clause(words=f"{name} is {shown}", state=state, field=name, values=values))
