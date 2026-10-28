"""eTools master indicators: a section's master indicator combines the partners' own PD indicators (its subs,
:class:`~neurodb.datamart.models.EtoolsMasterLink`) into one figure a year, like an ActivityInfo master
indicator, with each indicator type's rule.

This module holds the one rule for what the nightly job, the admin and the page share:

* **Identity** (:func:`ident_code`): a PD indicator is named in its PD by its eTools id, else by a hash of
  its title (:func:`neurodb.datamart.monitoring.ident_of`), never by the page's key (``r<datamart id>`` for
  an indicator without an eTools id, which changes when the sync rewrites the rows).
* **Type** (:func:`unit_class`): number, percentage, ratio, or other (which never counts). It is read from
  the indicator's own fields, so it works on a stored :class:`~neurodb.datamart.monitoring.Row`, which has no
  ``result`` (``headline.is_number`` reads one and would fail there).
* **Grouping** (:func:`base_title`, :func:`cp_output_key`, :func:`structure_key`, :func:`clusters`): the
  structure key NeuroDB proposes masters from: section, CP output, type and the title without its counting
  words, gender, nationality, disability and age range.
* **Figures** (:func:`figures`): a master's value, quarters, coverage, status and the reason each sub is
  counted or not. A master never re-reads report rows: each sub's figure is its own Reached in the year
  (:mod:`neurodb.datamart.reached`, through the partner monitoring rows), which already reads each value
  once, follows the sub's location and period methods and never adds quarterly and monthly reports.

Only confirmed links count; a suggestion is listed, never added. Numbers are added (up to N, at least the
largest programme document's figure: partners may reach the same people), percentages and ratios are
numerators added up over denominators added up (a sub reported without them is left out and said so, never
averaged), and the status is measured against the year's target staff typed alone ("No target" until it is
typed). Masters are never added together, nor to the children headline, the overview, the CPD or forecasts.
No AI is used: grouping and suggestions are deterministic.
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Prefetch

from neurodb.cpd.services import _norm as output_norm
from neurodb.indicators.services.tracking import NO_TARGET, label_for, tracking

from . import headline, identity, monitoring, tags
from . import reached as rc
from .children import NUMBER_TYPES
from .models import EtoolsMaster, EtoolsMasterLink, EtoolsMasterYear, PDIndicator
from .reached import norm

Row = Any  # neurodb.datamart.monitoring.Row (a stored row) or Indicator: the same fields

Method, Role, Status = EtoolsMaster.Method, EtoolsMasterLink.Role, EtoolsMasterLink.Status
NUMBER, PERCENTAGE, RATIO, OTHER = (
    EtoolsMasterLink.UnitClass.NUMBER,
    EtoolsMasterLink.UnitClass.PERCENTAGE,
    EtoolsMasterLink.UnitClass.RATIO,
    EtoolsMasterLink.UnitClass.OTHER,
)
QUARTERS = (1, 2, 3, 4)  # Reached from 1 January to the end of each quarter: the fourth is the year
NO_CP_OUTPUT = "?"  # the CP output key of an indicator whose CP output is not known

# How a master combines its subs: a short name for the tables and a sentence for their tooltip (in the
# style of the ActivityInfo pages' methods, neurodb.facts.services.dashboard.METHODS)
METHODS = {
    Method.SUM: {
        "label": "Sum",
        "help": "Adds up the programme documents' Reached in the year; partners may reach the same people.",
    },
    Method.PERCENT: {
        "label": "Percentage",
        "help": "The numerators added up over the denominators added up, × 100.",
    },
    Method.RATIO: {"label": "Ratio", "help": "The numerators added up over the denominators added up."},
    Method.AVERAGE: {
        "label": "Average",
        "help": "The unweighted average of the programme documents' figures.",
    },
    Method.MAXIMUM: {"label": "Maximum", "help": "The largest programme document's figure."},
    Method.MINIMUM: {"label": "Minimum", "help": "The smallest programme document's figure."},
}
# a sum master's figure: up to N (partners may reach the same people), at least the largest PD's
SUM_RULE = (
    "Up to {up_to}: the sum over the programme documents; partners may reach the same people. "
    "At least {at_least} (the largest programme document)."
)

# what each method takes as its totals (a numerator or a denominator is a number; a shown link anything)
TOTALS = {
    Method.SUM: frozenset({NUMBER}),
    Method.PERCENT: frozenset({PERCENTAGE}),
    Method.RATIO: frozenset({RATIO}),
    Method.AVERAGE: frozenset({NUMBER, PERCENTAGE, RATIO}),
    Method.MAXIMUM: frozenset({NUMBER, PERCENTAGE, RATIO}),
    Method.MINIMUM: frozenset({NUMBER, PERCENTAGE, RATIO}),
}
WITH_PARTS = frozenset({Method.PERCENT, Method.RATIO})  # numerators over denominators
ONE_CLASS = frozenset({Method.AVERAGE, Method.MAXIMUM, Method.MINIMUM})  # every total of one type

# Why a sub is not counted (the page's sub rows, the exports)
SUGGESTED = "Suggested, not confirmed"
GONE = "No longer in eTools"
# still in eTools, but left out of the pages (identity.listed): never "no longer in eTools"
LEFT_OUT = "Left out of the pages: eTools marks it inactive and no partner report names it"
TYPE_CHANGED = "Type changed in eTools"
WRONG_TYPE = "Its type does not fit this master's method"
SHOWN = "Shown only"
NO_VALUE = "Not reported in the year"
SAME_PD = "Same programme document: may count the same people; not added"
SAME_PD_PARTS = "Same programme document: counted through its numerator and denominator"
NO_PARTS = "Reported without numerator and denominator"
# its PD combines its periods, or its locations, by an average or the largest (reached.combine_items): the
# year has no numerator and denominator, whatever its reports carry
PERIODS_COMBINED = {
    "avg": "Its PD averages its reporting periods: no numerator and denominator for the year",
    "max": "Its PD takes its largest reporting period: no numerator and denominator for the year",
}
LOCATIONS_COMBINED = {
    "avg": "Its PD averages its locations: no numerator and denominator for the year",
    "max": "Its PD takes its largest location: no numerator and denominator for the year",
}
NUMERATOR_ALONE = "Numerator without its denominator"
DENOMINATOR_ALONE = "Denominator without its numerator"
SET_ASIDE = "Set aside: {why}"
MOVED = "Now in section {section}"


# ------------------------------------------------------------------------------------------------ identity
def ident_code(ident: tuple[int, str] | str) -> str:
    """A PD indicator's identity in its PD as a link stores it: ``s<eTools id>``, else ``t`` and the sha1 of
    its normalised title (:func:`neurodb.datamart.monitoring.ident_of`). Never the page's key: ``r<datamart
    id>`` (or a bare id) is refused, since it changes when the sync rewrites the rows."""
    code = ident[1] if isinstance(ident, tuple) else ident
    if code.startswith("s") and code[1:].isdigit():
        return code
    if code.startswith("t"):
        return "t" + hashlib.sha1(norm(code[1:]).encode(), usedforsecurity=False).hexdigest()
    raise ValueError(f"not an indicator identity: {code!r}")


def key_of(row: Row) -> tuple[int, str]:
    """``(PD id, ident code)``: how a link finds its row."""
    return (row.pd.id, ident_code(row.ident))


def left_out(pd_ids: Iterable[int]) -> set[tuple[int, str]]:
    """The PD indicators of these programme documents the pages leave out (``identity.listed``: eTools marks
    them inactive in every row and no partner report row names them), by key (:func:`key_of`). They are still
    in eTools: a link to one is never "no longer in eTools" (:data:`GONE`). One query."""
    ids = sorted({i for i in pd_ids if i})
    if not ids:
        return set()
    rows = identity.left_out(PDIndicator.objects.filter(intervention_id__in=ids)).order_by()
    return {
        (pd_id, ident_code(monitoring.ident_of(pd_id, source_id, title)))
        for pd_id, source_id, title in rows.values_list("intervention_id", "source_id", "title").distinct()
    }


# ---------------------------------------------------------------------------------------------------- type
def unit_class(row: Row) -> str:
    """What a PD indicator counts in: "percentage" or "ratio" (numerators over denominators, as Reached reads
    it: :func:`neurodb.datamart.reached.is_ratio` over its display type, unit and methods), "number" when its
    display type and unit are both a number, else "other", which never counts. Read from the row's fields,
    so it works on a stored Row and on an indicator whose Reached was not computed (its methods then
    unknown: a ratio only by its display type or unit)."""
    display, unit = (row.display_type or "").lower(), (row.unit or "").lower()
    ratio = bool(getattr(row, "ratio", False)) or rc.is_ratio(
        display, unit, getattr(row, "method", "") or "", getattr(row, "period_method", "") or ""
    )
    if ratio:
        percentage = bool(getattr(row, "percentage", False)) or "percentage" in (display, unit)
        return PERCENTAGE if percentage else RATIO
    if display in NUMBER_TYPES and unit in NUMBER_TYPES:
        return NUMBER
    return OTHER


def misfit(method: str, role: str, unit: str) -> str:
    """Why a PD indicator of type ``unit`` cannot count as ``role`` in a master of ``method`` ("" when it
    can): a sum adds numbers; a percentage master takes percentages, a ratio master ratios, and numbers as
    numerators or denominators; an average, a maximum or a minimum takes any one type. A shown link takes
    anything, and a percentage never mixes with a ratio."""
    if role == Role.SHOWN:
        return ""
    if role in (Role.NUMERATOR, Role.DENOMINATOR):
        if method not in WITH_PARTS:
            return "A numerator or a denominator counts only in a percentage or a ratio master."
        if unit != NUMBER:
            return "A numerator or a denominator must be a number (a count), not a percentage or a ratio."
        return ""
    if unit in TOTALS.get(method, ()):
        return ""
    if method == Method.SUM:
        return "A sum adds numbers only: a percentage or a ratio can only be shown in it."
    if method == Method.PERCENT:
        return "A percentage master's total takes percentages (numbers count as numerators or denominators)."
    if method == Method.RATIO:
        return "A ratio master's total takes ratios (numbers count as numerators or denominators)."
    return "Only numbers, percentages or ratios can be combined."


# ------------------------------------------------------------------------------------------------ grouping
_AGE_RANGE = re.compile(r"\(\s*\d{1,2}\s*(?:[-–]|to)\s*\d{1,2}\s*(?:years?|yrs?|y|months?)?(?:\s*old)?\s*\)")
_COUNTING = re.compile(r"(?:#|%|\bnumber|\bno\.?|\bnb\.?|\bpercentage|\bpercent|\bproportion)\s*of\b")
_PUNCTUATION = re.compile(r"[^\w\s]|_")
_YOUNG = re.compile(r"\byoung (?:men|women)\b")
_DISABILITY = re.compile(
    r"\b(?:(?:with|having|living with) )?(?:disabilit(?:y|ies)|special needs)\b|\bpwds?\b|\bdisabled\b"
)
_NATIONALITY = re.compile("|".join(pattern for _label, pattern in tags.NATIONALITY))
# the gender nouns of the title tags (neurodb.datamart.tags.GENDER) as the population they belong to: girls
# and boys are children, women and men adults, female and male say nothing more. Mothers, fathers, pregnant
# and lactating women name a population of their own (caregivers), so they stay.
_GENDER = {
    **dict.fromkeys(("girl", "girls", "boy", "boys"), "children"),
    **dict.fromkeys(("woman", "women", "man", "men"), "adults"),
    **dict.fromkeys(("female", "females", "male", "males"), "~"),
}
_DROPPED = "~"
_JOINS = frozenset({"and", "or"})


def base_title(title: str | None) -> str:
    """A PD indicator's title as its family: lower case, without punctuation, its counting words ("#",
    "number of", "% of", "percentage of", "proportion of"), a bracketed age range ("(6-14)"), its nationality
    and disability words; girls and boys read as children, women and men as adults, female and male as
    nothing, and repeats said once ("girls and boys" is "children"). The age-group nouns stay: children,
    adolescents, youth and caregivers are different families."""
    text = (title or "").lower()
    text = _AGE_RANGE.sub(" ", text)
    text = _COUNTING.sub(" ", text)
    text = " ".join(_PUNCTUATION.sub(" ", text).split())
    text = _YOUNG.sub("young people", text)
    text = _DISABILITY.sub(f" {_DROPPED} ", text)
    text = _NATIONALITY.sub(f" {_DROPPED} ", text)
    words = [_GENDER.get(word, word) for word in text.split()]
    kept = []
    for i, word in enumerate(words):
        # "and" between two dropped words ("Syrian and Lebanese", "female and male") goes with them
        left = words[i - 1] if i > 0 else _DROPPED
        right = words[i + 1] if i + 1 < len(words) else _DROPPED
        if word in _JOINS and left == _DROPPED and right == _DROPPED:
            continue
        if word != _DROPPED:
            kept.append(word)
    out: list[str] = []
    for word in kept:
        if out and word == out[-1]:  # "children children"
            continue
        if len(out) >= 2 and out[-1] in _JOINS and out[-2] == word:  # "children and children"
            out.pop()
            continue
        out.append(word)
    while out and out[0] in _JOINS:
        out.pop(0)
    while out and out[-1] in _JOINS:
        out.pop()
    return " ".join(out)


def _outputs(row: Row, outputs: Sequence[str] | None) -> list[str] | None:
    found = outputs if outputs is not None else getattr(row.pd, "cp_outputs", None)
    if found is None:
        return None
    return list(dict.fromkeys(str(x).strip() for x in found if str(x or "").strip()))


def cp_output_key(row: Row, outputs: Sequence[str] | None = None) -> str:
    """The CP output a PD indicator is grouped by: its own (from its reports), normalised as the CPD links
    read it (:func:`neurodb.cpd.services._norm`); else its PD's only CP output; else "?". The PD's outputs
    joined, which partner monitoring shows for an indicator no report names yet
    (:func:`neurodb.datamart.monitoring.pd_cp_outputs`), are never a key. ``outputs``: the PD's CP outputs,
    for a stored row (an indicator's PD carries them)."""
    pd_outputs = _outputs(row, outputs)
    own = (row.cp_output or "").strip()
    if own and (pd_outputs is None or own != ", ".join(pd_outputs)):
        return output_norm(own) or NO_CP_OUTPUT
    if pd_outputs and len(pd_outputs) == 1:
        return output_norm(pd_outputs[0]) or NO_CP_OUTPUT
    return NO_CP_OUTPUT


def _parts(row: Row, outputs: Sequence[str] | None = None) -> tuple[str, str, str, str] | None:
    section = norm(row.section)
    unit = unit_class(row)
    if not section or row.section == monitoring.NO_SECTION or unit == OTHER:
        return None
    return (section, cp_output_key(row, outputs), unit, base_title(row.title))


def _digest(parts: tuple[str, str, str, str]) -> str:
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()


def structure_key(row: Row, outputs: Sequence[str] | None = None) -> str:
    """The sha1 of section | CP output | type | base title (:func:`cp_output_key`, :func:`unit_class`,
    :func:`base_title`): the family NeuroDB proposes a master from. "" for an indicator that names no
    section, or whose type never counts: it is never proposed."""
    parts = _parts(row, outputs)
    return _digest(parts) if parts else ""


def clusters(rows: Iterable[Row], outputs: dict[int, Sequence[str]] | None = None) -> dict[str, list[Row]]:
    """``rows`` by :func:`structure_key`. An indicator whose CP output is not known ("?") joins the one
    family of its section, type and base title whose CP output is, when there is exactly one; else it stays
    in the "?" family. Indicators without a key are left out. ``outputs``: PD id -> its CP outputs (for
    stored rows)."""
    found: dict[str, list[Row]] = defaultdict(list)
    families: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    unknown = []
    for row in rows:
        parts = _parts(row, (outputs or {}).get(row.pd.id) if outputs is not None else None)
        if parts is None:
            continue
        section, cp_output, unit, base = parts
        if cp_output == NO_CP_OUTPUT:
            unknown.append((row, parts))
            continue
        key = _digest(parts)
        found[key].append(row)
        families[(section, unit, base)].add(key)
    for row, parts in unknown:
        family = families.get((parts[0], parts[2], parts[3]), set())
        found[next(iter(family)) if len(family) == 1 else _digest(parts)].append(row)
    return dict(found)


# ---------------------------------------------------------------------------------------- checks (clean)
def pd_indicator(pd_id: int, ident: str) -> Row | None:
    """The PD indicator ``ident`` (:func:`ident_code`) of programme document ``pd_id``, read again from eTools
    with its Reached (so its type is the one the pages read), or None when the PD has no such indicator."""
    rows = monitoring.indicators(monitoring.Filters(pd_ids=[pd_id], scope="all"))
    return next((row for row in rows if ident_code(row.ident) == ident), None)


def _one_class(master: EtoolsMaster, unit: str, exclude: int | None = None) -> str:
    """Why a total of type ``unit`` cannot join an average, maximum or minimum master whose other totals are
    of another type ("" when it can)."""
    if master.pk is None or master.method not in ONE_CLASS:
        return ""
    others = (
        master.links.filter(role=Role.TOTAL)
        .exclude(status=Status.REJECTED)
        .exclude(pk=exclude)
        .exclude(unit_class="")
        .values_list("unit_class", flat=True)
        .distinct()
    )
    found = set(others) - {unit}
    if found:
        return _one_type(master, found)
    return ""


def _one_type(master: EtoolsMaster, found: set[str]) -> str:
    kind = master.get_method_display().lower()
    kind = f"an {kind}" if kind[:1] in "aeiou" else f"a {kind}"
    return f"Every total of {kind} has one type: this master's are {', '.join(sorted(found))}."


def not_active(master: EtoolsMaster) -> str:
    """Why a PD indicator cannot be confirmed in ``master`` ("" when it is active): a master that is not
    active is listed nowhere, so what its total holds would count nowhere and keep the indicator from every
    other master's total."""
    if master.status == EtoolsMaster.Status.ACTIVE:
        return ""
    if master.status == EtoolsMaster.Status.PROPOSED:
        return "Accept this proposed master indicator first: its PD indicators count once it is active."
    return (
        f"This master indicator is {master.get_status_display().lower()}: make it active first, or choose "
        "another one."
    )


def check_link(link: EtoolsMasterLink) -> None:
    """:meth:`EtoolsMasterLink.clean`: a new link's indicator is one of its PD's (read again, as the indicator
    flags check theirs) and in the master's section, and a new confirmed link is only in an active master
    (:func:`not_active`); every link is of a type its role and the master's method take. A saved link's PD and
    indicator are read-only on its forms: one gone from eTools (:data:`GONE`) is checked by the type it was
    linked with, so staff can make it shown only. Fills the title, PD number and type it was linked with when
    they are empty."""
    errors: dict[str, str] = {}
    if not link.master_id:
        raise ValidationError({"master": "Choose the master indicator."})
    master = link.master
    adding = link._state.adding
    if adding and link.status == Status.CONFIRMED and not_active(master):
        errors["master"] = not_active(master)
    row = None
    if not link.pd_id:
        if adding:
            errors["pd"] = "Choose the programme document."
    elif link.ident:
        row = pd_indicator(link.pd_id, link.ident)
        if row is None and adding:
            errors["ident"] = (
                "This is not one of the programme document's indicators. Choosing the indicator on the "
                "master indicators tab or with Add PD indicators avoids this."
            )
    if row is not None:
        link.title = link.title or row.title[:1024]
        link.pd_number = link.pd_number or (row.pd.number or "")[:64]
        link.unit_class = link.unit_class or unit_class(row)
        if adding and norm(row.section) != norm(master.section):
            errors["ident"] = (
                f"This indicator is in section {row.section or 'none'}, not in this master's section "
                f"({master.section})."
            )
    if link.status != Status.REJECTED:
        unit = unit_class(row) if row is not None else link.unit_class
        problem = misfit(master.method, link.role, unit) if unit else ""
        if not problem and link.role == Role.TOTAL and unit:
            problem = _one_class(master, unit, exclude=link.pk)
        if problem:
            errors["role"] = problem
    if errors:
        raise ValidationError(errors)


def check_master(master: EtoolsMaster) -> None:
    """:meth:`EtoolsMaster.clean`: a section is named (never the tile of the indicators without one), a
    new method is refused while any link (rejected ones aside) would not fit it, by the type it was linked
    with, and an active master is not archived or dismissed while it counts PD indicators in its total: they
    would count nowhere and no other master could take them (merging moves them first)."""
    errors: dict[str, str] = {}
    if not (master.section or "").strip() or master.section.strip() == monitoring.NO_SECTION:
        errors["section"] = "Name the eTools section, as on the PD indicators."
    if master.pk is not None:
        stored = EtoolsMaster.objects.filter(pk=master.pk).values_list("method", "status").first()
        before, was = stored if stored is not None else (None, None)
        changed = was is not None and was != master.status and not_active(master)
        totals = master.links.filter(role=Role.TOTAL, status=Status.CONFIRMED).count() if changed else 0
        if totals:
            errors["status"] = (
                f"It counts {totals} PD indicator(s) in its total: make them “Shown only” or take them out "
                "first (or merge it into another master indicator, which moves them), so they can count "
                "elsewhere."
            )
        if before is not None and before != master.method:
            links = list(master.links.exclude(status=Status.REJECTED).exclude(unit_class=""))
            wrong = [link for link in links if misfit(master.method, link.role, link.unit_class)]
            totals = {link.unit_class for link in links if link.role == Role.TOTAL}
            if wrong:
                example = wrong[0]
                why = misfit(master.method, example.role, example.unit_class)
                errors["method"] = (
                    f"{len(wrong)} linked indicator(s) would not fit this method (for example "
                    f"{example.pd_number or 'a PD'}: {why}) Change their role first, or show them only."
                )
            elif master.method in ONE_CLASS and len(totals) > 1:
                errors["method"] = _one_type(master, totals)
    if errors:
        raise ValidationError(errors)


# ------------------------------------------------------------------------------------------------- figures
@dataclass
class Sub:
    """One link of a master as the year reads it: its PD indicator's figures and whether it counts."""

    link: EtoolsMasterLink
    row: Row | None  # None: not found among the year's indicators
    role: str
    status: str
    unit_class: str = ""  # its type now
    value: float | None = None  # Reached in the year
    numerator: float | None = None  # of Reached in the year, when its period method keeps them
    denominator: float | None = None
    quarters: dict[int, float | None] = field(default_factory=dict)  # Reached to the end of each quarter
    eligible: bool = False  # confirmed, found, of its type, and in the total or a numerator or a denominator
    counted: bool = False
    reason: str = ""  # why it is not counted ("" when it is)
    note: str = ""  # e.g. "Now in section X": still read

    @property
    def counts_as(self) -> str:
        return Role(self.role).label

    @property
    def pd_id(self) -> int | None:
        return self.row.pd.id if self.row is not None else self.link.pd_id


@dataclass
class Figure:
    """A master indicator's figures for one year (:func:`figures`)."""

    master: EtoolsMaster
    year: int
    code: str = ""  # the year's AWP/RWP code
    target: float | None = None  # the year's target, typed by staff: the status is measured against it alone
    value: float | None = None
    numerator: float | None = None  # percentage and ratio masters: Σ numerators
    denominator: float | None = None  # and Σ denominators
    at_least: float | None = None  # sum masters: the largest single programme document's figure
    # Reached from 1 January to the end of each quarter (1 to 4): the fourth is the year
    quarters: dict[int, float | None] = field(default_factory=dict)
    status: str = NO_TARGET
    achieved: float | None = None  # % of the target
    as_of: datetime.date | None = None  # the status's date: the latest counted report's period end
    prorate: bool = False  # the status expects the year's share of the target (an all-additive sum)
    counted_pds: int = 0  # programme documents counted...
    pds: int = 0  # ...of those that reported a figure in the year (coverage)
    reporting: int = 0  # subs with a value in the year...
    eligible: int = 0  # ...of the subs that count (confirmed, found, of their type, not shown only)
    partners: int = 0  # partners counted
    pd_target: float | None = None  # for reference only: the programme documents' own targets (whole periods)
    one_class: str = ""  # an average, a maximum or a minimum: the type of its totals (its first confirmed's)
    subs: list[Sub] = field(default_factory=list)

    @property
    def method(self) -> str:
        return self.master.method

    @property
    def method_label(self) -> str:
        return METHODS[self.master.method]["label"]

    @property
    def method_help(self) -> str:
        return METHODS[self.master.method]["help"]

    @property
    def status_label(self) -> str:
        return (
            monitoring.LABELS[self.status]
            if self.status == monitoring.NOT_REPORTED
            else label_for(self.status, self.achieved)
        )

    @property
    def coverage(self) -> str:
        """The programme documents counted of those that reported in the year: "counted 4 of 6 PDs"."""
        return f"counted {self.counted_pds} of {self.pds} PD{'' if self.pds == 1 else 's'}"

    @property
    def how(self) -> str:
        """How the figure is worked out, in one sentence (a sum's "up to / at least" pair)."""
        if self.master.method == Method.SUM and self.value is not None:
            return SUM_RULE.format(up_to=_number(self.value), at_least=_number(self.at_least))
        text = self.method_help
        if self.pds and self.counted_pds < self.pds:
            text = f"{text[:-1]} ({self.coverage})."
        return text


def _number(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


_SEX = {"Girls": "F", "Women": "F", "Female": "F", "Boys": "M", "Men": "M", "Male": "M"}
# refugees may be Syrian or Palestinian: never apart from them
_NATIONS = frozenset({"Syrian", "Palestinian", "Lebanese", "Migrants"})
_NATION_WORDS = "|".join(f"(?:{pattern})" for label, pattern in tags.NATIONALITY if label in _NATIONS)
_REFUGEE_WORDS = dict(tags.NATIONALITY)["Refugees"]
# "non-Lebanese" names every nationality but one: never that one ("non-" is read before the nationality
# words, which would read it as Lebanese), and apart from it alone
_NOT_OF = [
    (f"non-{label}", re.compile(rf"\bnon[-\u2010\u2011\s]*(?:{pattern})"))
    for label, pattern in tags.NATIONALITY
    if label in _NATIONS
]
# "Palestine refugees" (UNRWA's words, as PRS and PRL) are Palestinian
_PALESTINE = re.compile(r"\bpalestine\b")
# refugees a nationality names ("Syrian refugees", "displaced Lebanese") are of that nationality
_THEIR_REFUGEES = re.compile(
    rf"(?P<before>{_NATION_WORDS})\s+(?:{_REFUGEE_WORDS})|(?:{_REFUGEE_WORDS})\s+(?P<after>{_NATION_WORDS})"
)


def _sexes(row: Row) -> set[str]:
    """Every sex a title names ("girls and boys": both); the stored tag for a row without a title."""
    text = (row.title or "").lower()
    if not text:
        return {_SEX[label] for label in [row.tags.get("gender") or ""] if label in _SEX}
    return {_SEX[label] for label, pattern in tags.GENDER if re.search(pattern, text)}


def _lands(row: Row) -> set[str]:
    """Every nationality a title names, refugees among them unless a nationality names them, and "non-X" for
    every nationality but X ("non-Lebanese", never Lebanese); the stored tag for a row without a title."""
    text = (row.title or "").lower()
    if not text:
        return {row.tags["nationality"]} if row.tags.get("nationality") else set()
    found = set()
    for label, pattern in _NOT_OF:
        text, n = pattern.subn(" ", text)
        if n:
            found.add(label)
    text = _PALESTINE.sub("palestinian", text)
    text = _THEIR_REFUGEES.sub(lambda m: m.group("before") or m.group("after"), text)
    return found | {label for label, pattern in tags.NATIONALITY if re.search(pattern, text)}


def _nations_apart(a: str, b: str) -> bool:
    """Two nationalities, or a nationality and "non-" another, that no one has both of."""
    if a in _NATIONS and b in _NATIONS:
        return a != b
    return a == f"non-{b}" or b == f"non-{a}"


def disjoint(a: Row, b: Row) -> bool:
    """Two indicators that cannot count the same person, by every gender and nationality word of their
    titles: one names girls and the other boys (women and men, female and male), or each names one
    nationality and they differ, or one names Lebanese and the other non-Lebanese. A title naming both sexes
    or two nationalities ("girls and boys", "Syrian and Lebanese", "Lebanese and non-Lebanese") is apart from
    neither, nor are refugees a nationality does not name (they may be Syrian or Palestinian), and
    "non-Lebanese" is apart from the Lebanese alone (the Syrian may be among them). Disability and age subsets
    never are."""
    sex_a, sex_b = _sexes(a), _sexes(b)
    if len(sex_a) == len(sex_b) == 1 and sex_a != sex_b:
        return True
    land_a, land_b = _lands(a), _lands(b)
    return len(land_a) == len(land_b) == 1 and _nations_apart(next(iter(land_a)), next(iter(land_b)))


def _apart(rows: Sequence[Row]) -> bool:
    return all(disjoint(a, b) for a, b in itertools.combinations(rows, 2))


def appears(master: EtoolsMaster, year: int, running: Iterable[int]) -> bool:
    """Whether a master is listed in ``year``: it is active, its year is not switched off, and it has that
    year's settings or a confirmed link to a programme document running in the year (``running``)."""
    if master.status != EtoolsMaster.Status.ACTIVE:
        return False
    year_row = next((y for y in master.years.all() if y.year == year), None)
    if year_row is not None:
        return year_row.is_active
    running = set(running)
    return any(link.status == Status.CONFIRMED and link.pd_id in running for link in master.links.all())


def counted_running(master: EtoolsMaster, year: int) -> int:
    """The PD indicators ``master`` counts (confirmed, not shown only) of programme documents running in
    ``year``: what switching its year off leaves counting nowhere in it (links have no year, so no other
    master can take one in its total while this one holds it)."""
    counting = master.links.filter(status=Status.CONFIRMED).exclude(role=Role.SHOWN)
    pd_ids = sorted(set(counting.values_list("pd_id", flat=True)))
    if not pd_ids:
        return 0
    running = monitoring._pd_queryset(monitoring.Filters(year=year, scope="year", pd_ids=pd_ids))
    return counting.filter(pd_id__in=running.values("id")).count()


def switched_off(year: int):
    """The ids of the masters whose ``year`` is switched off (*shown this year* unticked): they are not listed
    in it and count nothing in it (a values query, for ``master_id__in``)."""
    return EtoolsMasterYear.objects.filter(year=year, is_active=False).values_list("master_id", flat=True)


def homes(year: int, pd_ids: Iterable[int] | None = None) -> set[tuple[int, str]]:
    """The PD indicators, by key, in a master in ``year``: a confirmed link to an active master whose year
    is not switched off (:func:`switched_off`). One confirmed only in a master switched off for the year is in
    no master that year: it counts nowhere, the Master indicators tab lists it under Not in a master and says
    why, and reporting check 12 counts it apart. ``pd_ids``: only these programme documents' (default all)."""
    found = EtoolsMasterLink.objects.filter(
        status=Status.CONFIRMED, master__status=EtoolsMaster.Status.ACTIVE
    ).exclude(master_id__in=switched_off(year))
    if pd_ids is not None:
        found = found.filter(pd_id__in=sorted(set(pd_ids)))
    return set(found.values_list("pd_id", "ident"))


def has_target(master: EtoolsMaster, year: int) -> bool:
    """Whether staff typed ``year``'s target, which its status is measured against (a target of 0 or less is
    none: the status reads "No target", as :func:`_status` reads it)."""
    year_row = next((y for y in master.years.all() if y.year == year), None)
    return year_row is not None and year_row.target is not None and year_row.target > 0


def listed(year: int) -> list[EtoolsMaster]:
    """The masters listed in ``year`` (:func:`appears`), with their years and confirmed links: four reads
    (the masters, their years, their confirmed links, and which of the linked programme documents run in
    the year, as the Master indicators tab reads them). Data health and reporting check 12 count them."""
    found = list(
        EtoolsMaster.objects.filter(status=EtoolsMaster.Status.ACTIVE)
        .order_by("section", "sequence", "name", "pk")
        .prefetch_related(
            "years",
            Prefetch(
                "links",
                queryset=EtoolsMasterLink.objects.filter(status=Status.CONFIRMED).only(
                    "pk", "master_id", "pd_id", "status"
                ),
            ),
        )
    )
    pd_ids = sorted({link.pd_id for m in found for link in m.links.all() if link.pd_id})
    running = (
        set(
            monitoring._pd_queryset(monitoring.Filters(year=year, scope="year", pd_ids=pd_ids)).values_list(
                "id", flat=True
            )
        )
        if pd_ids
        else set()
    )
    return [m for m in found if appears(m, year, running)]


def without_target(year: int) -> list[EtoolsMaster]:
    """The masters listed in ``year`` whose target for it is not typed: their status reads "No target" until
    it is (Data health's warning, reporting check 12)."""
    return [m for m in listed(year) if not has_target(m, year)]


def figures(
    masters: Iterable[EtoolsMaster],
    rows: Iterable[Row],
    year: int,
    today: datetime.date,
    *,
    running: Iterable[int] | None = None,
    national: int | None = None,
    is_children: Callable[[Row], bool] | None = None,
) -> list[Figure]:
    """Each master's figures in ``year`` from the year's PD indicator rows (``rows``: partner monitoring's
    rows of the year, :func:`neurodb.datamart.monitoring.stored_indicators`), with no database read beyond the
    masters' links and years (prefetch them).

    ``running``: the programme documents whose indicators of the year ``rows`` holds in full (default: every
    PD with a row). A confirmed link of one of them whose indicator is not among the rows is "No longer in
    eTools"; a link of any other PD is not listed (its PD does not run in the year, or the filter leaves it
    out). ``national`` and ``is_children``: the country's child population and the children rule, for the
    set-aside guard of a sum (:func:`neurodb.datamart.headline.implausible`): a value above the child
    population is set aside only for a sub that counts children; any value above 20 times its own target
    is."""
    by_key = {key_of(row): row for row in rows}
    running = {pd_id for pd_id, _ident in by_key} if running is None else set(running)
    return [_figure(master, by_key, running, year, today, national, is_children) for master in masters]


def _figure(
    master: EtoolsMaster,
    by_key: dict[tuple[int, str], Row],
    running: set[int],
    year: int,
    today: datetime.date,
    national: int | None,
    is_children: Callable[[Row], bool] | None,
) -> Figure:
    year_row = next((y for y in master.years.all() if y.year == year), None)
    out = Figure(master=master, year=year, code=year_row.code if year_row else "")
    out.target = year_row.target if year_row else None
    links = sorted(master.links.all(), key=lambda link: (link.sequence, link.pk or 0))
    # an average, maximum or minimum is of one type: its first confirmed total's
    first = next(
        (link for link in links if link.status == Status.CONFIRMED and link.role == Role.TOTAL), None
    )
    one_class = first.unit_class if first is not None and master.method in ONE_CLASS else ""
    out.one_class = one_class
    for link in links:
        sub = _sub(master, link, by_key, running, one_class)
        if sub is not None:
            out.subs.append(sub)
    candidates = [s for s in out.subs if s.eligible and s.value is not None]
    if master.method == Method.SUM:
        _sum(out, candidates, year, national, is_children)
    elif master.method in WITH_PARTS:
        _with_parts(out, candidates)
    else:
        _combined(out, candidates)
    counted = [s for s in out.subs if s.counted]
    out.eligible = sum(1 for s in out.subs if s.eligible)
    out.reporting = len(candidates)
    out.pds = len({s.pd_id for s in candidates})
    out.counted_pds = len({s.pd_id for s in counted})
    out.partners = len({s.row.pd.partner_id or s.row.pd.partner_name for s in counted})
    out.pd_target = _pd_target(master, [s for s in out.subs if s.eligible])
    _status(out, counted, year, today)
    return out


def _sub(
    master: EtoolsMaster,
    link: EtoolsMasterLink,
    by_key: dict[tuple[int, str], Row],
    running: set[int],
    one_class: str,
) -> Sub | None:
    """One link read in the year, with why it would not count (the method's own rules come after)."""
    if link.status == Status.REJECTED or link.pd_id not in running:
        return None
    row = by_key.get((link.pd_id, link.ident))
    sub = Sub(link=link, row=row, role=link.role, status=link.status)
    if row is None:
        if link.status != Status.CONFIRMED:
            return None  # a suggestion for an indicator gone: the job drops it
        sub.reason = GONE
        return sub
    sub.unit_class = unit_class(row)
    sub.value = row.reached_in_year
    item = row.in_year_item or (None, None, None)
    sub.numerator, sub.denominator = item[1], item[2]
    sub.quarters = {q: (row.to_quarter.get(q) or (None, None, None))[0] for q in QUARTERS}
    if norm(row.section) != norm(master.section):
        sub.note = MOVED.format(section=row.section or monitoring.NO_SECTION)
    if link.status != Status.CONFIRMED:
        sub.reason = SUGGESTED
    elif link.role == Role.SHOWN:
        sub.reason = SHOWN
    elif sub.unit_class != (link.unit_class or sub.unit_class):
        sub.reason = TYPE_CHANGED
    elif misfit(master.method, link.role, sub.unit_class) or (
        link.role == Role.TOTAL and one_class and sub.unit_class != one_class
    ):
        sub.reason = WRONG_TYPE
    else:
        sub.eligible = True
        if sub.value is None:
            sub.reason = NO_VALUE
    return sub


def _largest(subs: Sequence[Sub]) -> Sub:
    return max(subs, key=lambda s: (s.value or 0.0, s.row.target or 0.0, s.row.key or ""))


def _sum(
    out: Figure,
    candidates: list[Sub],
    year: int,
    national: int | None,
    is_children: Callable[[Row], bool] | None,
) -> None:
    """Numbers: values too large to be true set aside; within one programme document, only its largest sub
    unless they cannot count the same people (girls and boys, two nationalities) or the section said its
    parts add up; then the sum (up to), the largest programme document's figure (at least) and the
    quarters."""
    kept = []
    for sub in candidates:
        children = national if is_children is not None and is_children(sub.row) else None
        aside = headline.implausible([sub.row], children, year)
        if aside:
            sub.reason = SET_ASIDE.format(why=aside[0][1])
        else:
            kept.append(sub)
    by_pd: dict[int, list[Sub]] = defaultdict(list)
    for sub in kept:
        by_pd[sub.pd_id].append(sub)
    figures_by_pd = []
    for subs in by_pd.values():
        if len(subs) > 1 and not out.master.pd_parts_add_up and not _apart([s.row for s in subs]):
            largest = _largest(subs)
            for sub in subs:
                if sub is not largest:
                    sub.reason = SAME_PD
            subs = [largest]
        for sub in subs:
            sub.counted = True
        figures_by_pd.append(sum(s.value for s in subs))
    counted = [s for s in kept if s.counted]
    if not counted:
        return
    out.value = sum(figures_by_pd)
    out.at_least = max(figures_by_pd)
    out.quarters = {q: _total(s.quarters.get(q) for s in counted) for q in QUARTERS}
    out.prorate = all(s.row.additive for s in counted)


def _total(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def combined(row: Row) -> str:
    """Why a PD indicator's year has no numerator and denominator whatever its reports carry, when its period
    method averages its reporting periods or takes the largest (:data:`PERIODS_COMBINED`), or else its
    location method averages its locations or takes the largest (:data:`LOCATIONS_COMBINED`; PRP's methods,
    :func:`neurodb.datamart.reached.combine_items`). "" for any other method."""
    method = rc.method_of(getattr(row, "period_method", "") or "")
    if method in PERIODS_COMBINED:
        return PERIODS_COMBINED[method]
    method = rc.method_of(getattr(row, "method", "") or "")
    return LOCATIONS_COMBINED.get(method, "")


def _with_parts(out: Figure, candidates: list[Sub]) -> None:
    """Percentages and ratios: Σ numerators ÷ Σ denominators (× 100 for a percentage). A total brings its
    own numerator and denominator (left out without them, never averaged: the reason says whether its
    reports lack them or its PD averages its periods or its locations, or takes the largest,
    :func:`combined`); numerator and denominator subs of one programme document are added up, and take part
    only when it has both. Each programme document counts once: one counted through its numerator and
    denominator adds none of its totals, and of its several totals only the one with the largest
    denominator, unless they cannot count the same people or the section said its parts add up (a sum's
    rule). The same for each quarter."""
    scale = 100.0 if out.master.method == Method.PERCENT else 1.0
    totals: dict[int, list[Sub]] = defaultdict(list)
    for sub in candidates:
        if sub.role != Role.TOTAL:
            continue
        if sub.numerator is None or sub.denominator is None:
            sub.reason = combined(sub.row) or NO_PARTS
        else:
            totals[sub.pd_id].append(sub)
    pairs: dict[int, dict[str, list[Sub]]] = defaultdict(lambda: {Role.NUMERATOR: [], Role.DENOMINATOR: []})
    for sub in candidates:
        if sub.role in (Role.NUMERATOR, Role.DENOMINATOR):
            pairs[sub.pd_id][sub.role].append(sub)
    paired = []
    for pd_id, found in pairs.items():
        numerators, denominators = found[Role.NUMERATOR], found[Role.DENOMINATOR]
        if numerators and denominators:
            for sub in numerators + denominators:
                sub.counted = True
            paired.append((numerators, denominators))
            for sub in totals.pop(pd_id, []):  # the programme document is counted through its parts
                sub.reason = SAME_PD_PARTS
        else:
            for sub in numerators:
                sub.reason = NUMERATOR_ALONE
            for sub in denominators:
                sub.reason = DENOMINATOR_ALONE
    taking: list[Sub] = []
    for subs in totals.values():
        if len(subs) > 1 and not out.master.pd_parts_add_up and not _apart([s.row for s in subs]):
            largest = max(subs, key=lambda s: (s.denominator, s.numerator, s.row.key or ""))
            for sub in subs:
                if sub is not largest:
                    sub.reason = SAME_PD
            subs = [largest]
        for sub in subs:
            sub.counted = True
        taking.extend(subs)
    if not taking and not paired:
        return

    def ratio(numerator: float, denominator: float) -> float | None:
        return numerator / denominator * scale if denominator else None

    out.numerator = sum(s.numerator for s in taking) + sum(sum(s.value for s in n) for n, _d in paired)
    out.denominator = sum(s.denominator for s in taking) + sum(sum(s.value for s in d) for _n, d in paired)
    out.value = ratio(out.numerator, out.denominator)
    for q in QUARTERS:
        numerator = denominator = 0.0
        found = False
        for sub in taking:
            value, v, d = sub.row.to_quarter.get(q) or (None, None, None)
            if value is not None and v is not None and d is not None:
                numerator, denominator, found = numerator + v, denominator + d, True
        for numerators, denominators in paired:
            top = _total(s.quarters.get(q) for s in numerators)
            bottom = _total(s.quarters.get(q) for s in denominators)
            if top is not None and bottom is not None:
                numerator, denominator, found = numerator + top, denominator + bottom, True
        out.quarters[q] = ratio(numerator, denominator) if found else None


def _combined(out: Figure, candidates: list[Sub]) -> None:
    """Average, maximum or minimum of the subs' Reached in the year (one type), and of each quarter."""
    totals = [s for s in candidates if s.role == Role.TOTAL]
    if not totals:
        return
    for sub in totals:
        sub.counted = True
    out.value = _combine([s.value for s in totals], out.master.method)
    out.quarters = {q: _combine([s.quarters.get(q) for s in totals], out.master.method) for q in QUARTERS}


def _combine(values: list[float | None], method: str) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    if method == Method.MINIMUM:
        return min(present)
    return rc.combine(present, "avg" if method == Method.AVERAGE else "max")


def _pd_target(master: EtoolsMaster, eligible: list[Sub]) -> float | None:
    """For reference only (never the status): the programme documents' own targets, for their whole
    periods. A sum: Σ of the subs' targets, a PD's subs as its figure reads them (added when they cannot
    count the same people or its parts add up, else its largest). A percentage or a ratio: Σ target
    numerators ÷ Σ target denominators, each programme document once as its figure reads it (its numerator
    and denominator targets when it has both, else its totals', the largest denominator's alone unless they
    cannot count the same people or its parts add up). None for an average, a maximum or a minimum."""
    if master.method == Method.SUM:
        by_pd: dict[int, list[Sub]] = defaultdict(list)
        for sub in eligible:
            if sub.row.target is not None:
                by_pd[sub.pd_id].append(sub)
        if not by_pd:
            return None
        total = 0.0
        for subs in by_pd.values():
            together = master.pd_parts_add_up or _apart([s.row for s in subs])
            targets = [s.row.target for s in subs]
            total += sum(targets) if together else max(targets)
        return total
    if master.method not in WITH_PARTS:
        return None
    numerator = denominator = 0.0
    sides: dict[int, dict[str, float]] = defaultdict(dict)
    totals: dict[int, list[tuple[float, float, Sub]]] = defaultdict(list)
    for sub in eligible:
        top, bottom = sub.row.target_parts
        if sub.role == Role.TOTAL and top is not None and bottom is not None:
            totals[sub.pd_id].append((bottom, top, sub))
        elif sub.role in (Role.NUMERATOR, Role.DENOMINATOR) and sub.row.target is not None:
            side = sides[sub.pd_id]
            side[sub.role] = side.get(sub.role, 0.0) + sub.row.target
    for pd_id, side in sides.items():
        if Role.NUMERATOR in side and Role.DENOMINATOR in side:
            numerator, denominator = numerator + side[Role.NUMERATOR], denominator + side[Role.DENOMINATOR]
            totals.pop(pd_id, None)
    for found in totals.values():
        if len(found) > 1 and not master.pd_parts_add_up and not _apart([s.row for _b, _t, s in found]):
            found = [max(found, key=lambda item: (item[0], item[1], item[2].row.key or ""))]
        numerator += sum(top for _bottom, top, _sub in found)
        denominator += sum(bottom for bottom, _top, _sub in found)
    if not denominator:
        return None
    return numerator / denominator * (100.0 if master.method == Method.PERCENT else 1.0)


def _status(out: Figure, counted: list[Sub], year: int, today: datetime.date) -> None:
    """Against the year's typed target alone: "No target" without one, "Not reported" without a value, else
    on or off track as of the latest counted report (its period end, at most 31 December and today), so a
    quarter's lag does not read off track. A sum of additive subs expects the year's share of the target;
    a level (a percentage, an average, a maximum) the whole target."""
    year_end = datetime.date(year, 12, 31)
    ends = [min(s.row.as_of, year_end) for s in counted if s.row.as_of is not None]
    out.as_of = min(max(ends), today) if ends else None
    if not out.target or out.target <= 0:
        out.status, out.achieved = NO_TARGET, None
        return
    if out.value is None:
        out.status, out.achieved = monitoring.NOT_REPORTED, None
        return
    status = tracking(out.value, out.target, year, out.as_of or min(today, year_end), prorate=out.prorate)
    out.status, out.achieved = status.status, status.achieved
