"""PRP disaggregations: the labels of the ids the partner reports key their values by, and one location's
values in one report broken down by disaggregation, with a subtotal per disaggregation and the location
total.

The partner reports (``ReportedIndicator.disaggregation``) carry, per location, PRP's values keyed by
disaggregation-value ids: ``"()"`` is the location total, ``"(12,)"`` one value of one disaggregation
("female", of "Sex"), ``"(12, 31)"`` a cell of a cross-tab ("female × Lebanese"). The labels live in
PRP's own tables; the eTools Datamart sync reads those of the ids it sees (``DisaggregationLabel``).

The rule (the synthesis' "per disaggregation"):

* Values add up only within one disaggregation (female + male = the location total; Lebanese + Syrian
  + … = the location total) or within one cross-tab. They are never added across disaggregations:
  female 5 + Male 15 + Lebanese 20 is 40, for a location total of 20.
* Each disaggregation gets its own subtotal (percentages and ratios: numerators over denominators),
  and the location total ``"()"`` is shown apart. A subtotal that differs from the total is marked.
* A value whose label (or whose disaggregation) has not been read yet is not shown; it is counted, and
  the location total still is. A value PRP names by its text, without an id, is shown without a
  subtotal (its disaggregation is not known).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from . import reached as rc

TOTAL_KEY = "()"
CROSS = " × "
_QUOTES = "'\" "


def parse_key(key: Any) -> tuple | None:
    """A disaggregation key as its parts: ``"()"`` -> ``()`` (the location total), ``"(12,)"`` ->
    ``(12,)``, ``"(12, 31)"`` -> ``(12, 31)``; a key written in words (``"female"``, ``"('female',)"``)
    -> its words; None for an empty key."""
    text = str(key or "").strip()
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        inner = [part.strip(_QUOTES) for part in text[1:-1].split(",")]
        parts = [part for part in inner if part]
        if not parts:
            return ()
        if all(re.fullmatch(r"[0-9]{1,18}", part) for part in parts):
            return tuple(int(part) for part in parts)
        return tuple(parts)
    return (text,)


def ids_in(keys: Iterable[Any]) -> set[int]:
    """The disaggregation-value ids these keys name."""
    out: set[int] = set()
    for key in keys:
        parts = parse_key(key)
        if parts and all(isinstance(p, int) for p in parts):
            out.update(parts)
    return out


@dataclass
class Labels:
    """``values``: value id -> (label, its disaggregation's id); ``types``: disaggregation id -> name."""

    values: dict[int, tuple[str, int | None]] = field(default_factory=dict)
    types: dict[int, str] = field(default_factory=dict)

    def value(self, pk: int) -> tuple[str, int] | None:
        """(label, disaggregation id) of a value whose label and disaggregation are both known."""
        found = self.values.get(pk)
        if found is None or not found[0] or found[1] is None:
            return None
        return found[0], found[1]

    def type_name(self, pk: int) -> str:
        return self.types.get(pk) or "Disaggregation not named yet"


def load_labels(ids: Iterable[int] | None = None) -> Labels:
    """The labels held (of these value ids, else all), with the names of their disaggregations."""
    from .models import DisaggregationLabel

    values = DisaggregationLabel.objects.filter(kind=DisaggregationLabel.Kind.VALUE).exclude(label="")
    if ids is not None:
        wanted = sorted(set(ids))
        if not wanted:
            return Labels()
        values = values.filter(prp_id__in=wanted)
    out = Labels()
    for pk, label, type_id in values.values_list("prp_id", "label", "disaggregation_id"):
        out.values[pk] = (label, type_id)
    type_ids = {t for _, t in out.values.values() if t is not None}
    if type_ids:
        for pk, label in (
            DisaggregationLabel.objects.filter(
                kind=DisaggregationLabel.Kind.TYPE, prp_id__in=sorted(type_ids)
            )
            .exclude(label="")
            .values_list("prp_id", "label")
        ):
            out.types[pk] = label
    return out


@dataclass
class Line:
    key: str
    label: str
    value: float | None
    v: float | None = None
    d: float | None = None


@dataclass
class Group:
    """One disaggregation (or one cross-tab) at one location in one report."""

    name: str  # "Sex", or "Sex × Nationality" for a cross-tab
    cross: bool
    lines: list[Line]
    subtotal: float | None = None
    matches: bool | None = None  # the subtotal equals the location total (None: nothing to compare)
    hidden: int = 0  # values a filter leaves out: the subtotal is still that of every value


@dataclass
class Breakdown:
    total: float | None  # the location total "()" (None when PRP gives none)
    groups: list[Group] = field(default_factory=list)
    untyped: list[Line] = field(default_factory=list)  # named by PRP in words: no disaggregation known
    unlabelled: int = 0  # values whose label is not read yet: not shown

    @property
    def keys(self) -> int:
        return sum(len(g.lines) for g in self.groups) + len(self.untyped)


def _value(raw: Any, ratio: bool, percentage: bool) -> tuple[float | None, float | None, float | None]:
    p = rc.parts(raw)
    if p is None:
        return None, None, None
    return p.value(ratio, percentage), p.v, p.d


def _subtotal(lines: list[Line], ratio: bool, percentage: bool) -> float | None:
    """The lines of one disaggregation added up (percentages and ratios: numerators over denominators,
    None when a numerator or denominator is missing: such values are never added)."""
    present = [line for line in lines if line.value is not None]
    if not present:
        return None
    if ratio and not all(line.v is not None and line.d is not None for line in present):
        return None
    return rc.combine_items(
        [(line.value, line.v, line.d) for line in present], "sum", ratio=ratio, percentage=percentage
    )[0]


def breakdown(raw: Any, *, ratio: bool, percentage: bool, labels: Labels) -> Breakdown:
    """One location's disaggregation in one report (``raw``: PRP's ``{key: value}``), by the rule of the
    module docstring."""
    raw = raw if isinstance(raw, dict) else {}
    total = _value(raw[TOTAL_KEY], ratio, percentage)[0] if TOTAL_KEY in raw else None
    out = Breakdown(total=total)
    groups: dict[tuple[int, ...], list[Line]] = defaultdict(list)
    for key, value in raw.items():
        parts = parse_key(key)
        if not parts:
            continue
        number, v, d = _value(value, ratio, percentage)
        if not all(isinstance(p, int) for p in parts):
            out.untyped.append(Line(str(key), CROSS.join(str(p) for p in parts), number, v, d))
            continue
        known = [labels.value(pk) for pk in parts]
        if any(k is None for k in known):
            out.unlabelled += 1
            continue
        groups[tuple(k[1] for k in known)].append(
            Line(str(key), CROSS.join(k[0] for k in known), number, v, d)
        )
    for type_ids, lines in groups.items():
        lines.sort(key=lambda line: line.label.casefold())
        subtotal = _subtotal(lines, ratio, percentage)
        out.groups.append(
            Group(
                name=CROSS.join(labels.type_name(t) for t in type_ids),
                cross=len(type_ids) > 1,
                lines=lines,
                subtotal=subtotal,
                matches=(
                    None
                    if subtotal is None or total is None
                    else rc.close(subtotal, total, ratio=ratio, percentage=percentage)
                ),
            )
        )
    out.groups.sort(key=lambda g: (g.cross, g.name.casefold()))
    out.untyped.sort(key=lambda line: line.label.casefold())
    return out


def reached_by_disaggregation(result: Any, labels: Labels | None = None) -> list[dict[str, Any]]:
    """Per location of an indicator's Reached (``reached.Reached``): its Reached per labelled value,
    grouped by disaggregation (by PRP's id, never by name, so two disaggregations whose names are not
    read yet are never pooled), each with a subtotal when the indicator adds up (summed across
    locations and periods, not a percentage) — never one sum across disaggregations. A value whose
    label (or disaggregation) is not read yet is counted, not listed; a value PRP names in words has no
    disaggregation known, so no subtotal."""
    if result is None or not result.disaggregation:
        return []
    keys = {k for values in result.disaggregation.values() for k in values}
    labels = labels if labels is not None else load_labels(ids_in(keys))
    out = []
    for place, values in result.disaggregation.items():
        meta = result.by_location.get(place) or {}
        groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        unlabelled = 0
        for key, value in values.items():
            parts = parse_key(key)
            if not parts:
                continue
            if not all(isinstance(p, int) for p in parts):
                groups[("words",)].append({"label": CROSS.join(str(p) for p in parts), "reached": value})
                continue
            known = [labels.value(pk) for pk in parts]
            if any(k is None for k in known):
                unlabelled += 1
                continue
            groups[tuple(k[1] for k in known)].append(
                {"label": CROSS.join(k[0] for k in known), "reached": value}
            )
        if not groups and not unlabelled:
            continue
        reached = meta.get("reached")
        listed = []
        for type_ids, lines in groups.items():
            words = type_ids == ("words",)
            lines.sort(key=lambda line: line["label"].casefold())
            subtotal = None
            if result.additive and not words:
                subtotal = sum(line["reached"] or 0.0 for line in lines)
            listed.append(
                {
                    "name": "—" if words else CROSS.join(labels.type_name(t) for t in type_ids),
                    "cross": not words and len(type_ids) > 1,
                    "lines": lines,
                    "subtotal": subtotal,
                    "matches": None if subtotal is None or reached is None else rc.close(subtotal, reached),
                }
            )
        listed.sort(key=lambda g: (g["name"] == "—", g["cross"], g["name"].casefold()))
        out.append(
            {
                "location": meta.get("name") or "—",
                "p_code": meta.get("p_code") or "",
                "reached": reached,
                "groups": listed,
                "unlabelled": unlabelled,
            }
        )
    return out
