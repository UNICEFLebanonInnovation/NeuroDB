"""The quality rules of a monitoring visit's report, as FMS defines them, each applied by code to what the
visit holds.

**The rule model** (``RuleSetting``, seeded from FMS Lebanon's rules file, ``fmm.lebanon``): an id (R1,
R2, ... R32, never renamed), a name and description, a **type**, a score **category**, a group (core or
additional), on or off, a **deduction** (the points it takes off its category when it fires), a flag
template and its parameters (``params``), by type:

- ``completeness``: ``fields``, each with a label and its own deduction: every field missing from the
  report takes its deduction off ("R1: Incomplete monitoring report — missing: Q2 – Activities
  monitored");
- ``deterministic``: one ``field`` (``field_type`` numeric, or a list split at ``list_separator``) scored
  by bands (``scoring``: the first band that matches, in the order listed, gives the deduction; a band
  matches a value from its ``min`` and up to its ``max_inclusive``; for a list, from ``min_categories``,
  and with ``required_met`` only when the categories ``required`` for the entity type are there
  (``entity_type_scoring``)). A value not in the data takes ``missing_value_deduction`` off, or the rule
  is "not available" when that is 0;
- ``narrative``: an AI check (``fmm.ai.checks``) of the ``fields`` listed, with the instructions of its
  ``ai_prompt_key`` in the published prompt version; it fails when the AI says the report is not
  coherent, with the AI's explanation in its flag. A check not done yet is "pending";
- ``reference_check``: a ``check_type`` against reference data: ``member_in_mapped_list`` (R19: the
  monitor is on the staff list of the visit's field office, kept by administrators),
  ``value_in_mapped_list`` (R20: the visited place is one of the programme document's registered
  locations), ``section_in_cp_output`` (R21: the visit's sections are those of its CP output),
  ``pd_reference_locations`` (R23: the visited place is among the planned locations of the visit's or
  the partner's programme documents), ``value_in_list`` and ``string_contains``. R20, R21 and R23 read
  what eTools holds (``score.References``); the rule's ``reference_map`` adds entries to it.

``entity_type_filter`` limits a rule to the visits with a finding row of those types ("Partner", "CP
Output", "PD/SSFA" or "PD").

Each rule returns an :class:`Outcome`: ``pass``, ``fail`` (a flag, with its deduction), ``na`` (its input
is not in the data), ``nap`` (it does not apply to this visit, or the visit is not scored), ``off`` or
``pending`` (an AI check not done yet). The **score** (``score.score_visit``) is 100 less the deductions,
each category's at most its weight, never below 0.

Texts are compared folded (:func:`neurodb.fmm.parse.fold`). The question roles (Q1, Q2, Q3, PSEA) are
given by :func:`assign_roles` from the patterns of the score settings. Nothing a rule writes holds a
person's name or e-mail address: R19 compares the addresses in code and says only that the monitor is not
on the list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.exceptions import ValidationError

from . import parse

ROLES = ("q1", "q2", "q3", "psea")
RATED = ("on_track", "constrained", "off_track")
STATES = ("pass", "fail", "na", "nap", "off", "pending")
TYPES = ("completeness", "deterministic", "narrative", "reference_check")
TYPE_LABELS = {
    "completeness": "Completeness",
    "deterministic": "Deterministic",
    "narrative": "AI check",
    "reference_check": "Reference check",
}
CHECK_TYPES = (
    "member_in_mapped_list",
    "value_in_mapped_list",
    "section_in_cp_output",
    "value_in_list",
    "pd_reference_locations",
    "string_contains",
)
RATING_LABELS = {
    "on_track": "On track",
    "constrained": "Constrained",
    "off_track": "Off track",
    "not_monitored": "Not monitored",
    "other": "not a rating",
}
# The columns of FMS's processed FMM record a rule may read (the eTools export's names, §13.2)
COLUMNS: dict[str, str] = {
    "narrative_finding": "General Observation (narrative)",
    "overall_finding_rating": "Overall finding rating",
    "hact_q1_answer": "Q1 – Implementation status",
    "hact_q2_answer": "Q2 – Activities monitored",
    "hact_q3_answer": "Q3 – Key observations and findings",
    "fmq_answered_categories": "Checklist categories answered",
    "fmq_answered_pct": "Share of checklist questions answered",
    "method_count": "Data collection methods",
    "red_flag_count": "Red-flag answers",
    "attachments_count": "Attachments",
    "attachment_count": "Attachments",
    "action_points_count": "Action points",
    "action_points_text": "Action points",
    "action_points_assigned_to": "Action points assigned",
    "action_points_due_dates": "Action points' due dates",
    "visit_goals": "Visit goals",
    "objective": "Objective",
    "entity": "Entity",
    "entity_type": "Entity type",
    "field_offices": "Field offices",
    "sections_names": "Sections",
    "location_pcode": "Location P-code",
    "monitoring_modality": "Monitoring modality",
    "programme_areas": "Programme areas",
    "dim_supplies": "Supplies",
    "dim_psea": "PSEA",
    "team_members": "Team members",
    "person_responsible_email": "Person responsible",
    "partner": "Partner",
    "output": "CP output",
}
ROW_COLUMNS = frozenset(
    {
        "narrative_finding",
        "overall_finding_rating",
        "hact_q1_answer",
        "hact_q2_answer",
        "hact_q3_answer",
        "entity",
        "entity_type",
    }
)
# Columns that name people: never shown, never sent (the AI gets a count instead)
PERSON_COLUMNS = frozenset({"team_members", "person_responsible_email", "action_points_assigned_to"})
ENTITY_TYPES = {"partner": "partner", "cp output": "cp_output", "pd ssfa": "pd", "pd": "pd", "ssfa": "pd"}
KIND_NAMES = {"partner": "Partner", "cp_output": "CP Output", "pd": "PD/SSFA"}
PARAMS: dict[str, tuple[str, ...]] = {
    "completeness": ("fields", "entity_type_filter"),
    "deterministic": (
        "field",
        "field_type",
        "scoring",
        "missing_value_deduction",
        "list_separator",
        "entity_type_field",
        "entity_type_scoring",
        "entity_type_filter",
    ),
    "narrative": ("fields", "ai_prompt_key", "entity_type_filter"),
    "reference_check": (
        "check_type",
        "field",
        "key_field",
        "reference_map",
        "reference_list",
        "contains",
        "list_separator",
        "entity_type_filter",
    ),
}
MAX_LIST = 400  # entries of a reference list or map
MAX_ENTRY = 300  # characters of one entry
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
CANCELLED_DETAIL = "The visit was cancelled."
PENDING_DETAIL = "Pending: the visit's status is not one of the scored statuses (Score settings)."


# ------------------------------------------------------------------------------------------ numbers
def half_up(value: float | Decimal, digits: int = 1) -> Decimal:
    """``value`` rounded with halves away from zero (12.25 -> 12.3), as the pages show figures."""
    if not isinstance(value, Decimal):
        value = Decimal(f"{value:.9f}")  # no binary noise (84.94999...) decides a half
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def number(value: float | Decimal) -> str:
    """46.2 -> "46.2"; 80.0 -> "80"."""
    rounded = half_up(value, 1)
    return str(rounded.to_integral_value()) if rounded == rounded.to_integral_value() else str(rounded)


def dec(value: Any) -> Decimal:
    """A deduction as a Decimal (None, a yes/no and anything unreadable: 0)."""
    if value is None or isinstance(value, bool) or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return Decimal(0)


def q1_value(answer: AnswerFacts) -> str:
    """The normalised Q1 of an answered Q1 answer: its rating code, else "other" (not a rating)."""
    return answer.rating or "other"


def worst(values: Iterable[str]) -> str:
    """The worst of rating codes (off track, constrained, on track, other, not monitored); "" if none."""
    from neurodb.datamart.fm import RATING_ORDER

    values = [v for v in values if v]
    return max(values, key=lambda v: RATING_ORDER.get(v, 0)) if values else ""


def code_order(code: str) -> tuple[int, str]:
    """R2 before R10: rule ids in their number's order."""
    digits = "".join(ch for ch in code if ch.isdigit())
    return (int(digits) if digits else 0, code)


# ------------------------------------------------------------------------------------------ roles
def matches(folded_text: str, pattern: str) -> bool:
    """A question pattern: "=x" the folded text is x; "^x" it is x or starts with x and a space;
    anything else, it contains x (folded). Patterns are plain words, never regular expressions."""
    pattern = str(pattern or "").strip()
    if pattern[:1] in ("=", "^"):
        body = parse.fold(pattern[1:])
        if not body:
            return False
        if pattern[0] == "=":
            return folded_text == body
        return folded_text == body or folded_text.startswith(body + " ")
    body = parse.fold(pattern)
    return bool(body) and body in folded_text


def assign_roles(question_text: str, is_hact: bool | None, patterns: Mapping[str, list[str]]) -> str:
    """The role of a checklist question: the first of q1, q2, q3 and psea one of whose patterns
    matches its text; "" when none does. A whole text pinned to a role ("=x", as Questions found
    writes it) wins over the other patterns, so a question pinned to PSEA is PSEA even when a broader
    Q1 pattern matches it too. With no Q1 pattern at all, the question eTools flags as HACT is Q1 (an
    administrator who clears the Q1 patterns chooses the HACT flag instead)."""
    folded = parse.fold(question_text)
    for exact in (True, False):
        for role in ROLES:
            for pattern in patterns.get(role) or ():
                if str(pattern or "").strip().startswith("=") == exact and matches(folded, pattern):
                    return role
    if is_hact and not (patterns.get("q1") or ()):
        return "q1"
    return ""


@dataclass(frozen=True)
class AnswerFacts:
    """One checklist answer of a visit: never its text."""

    question_key: str
    role: str  # q1 | q2 | q3 | psea | ""
    answered: bool
    placeholder: bool
    words: int  # answer and summary together
    rating: str = ""  # a rating code when the answer is a rating word
    answer_code: str = ""  # on_track | constrained | off_track | yes | no | ""
    is_hact: bool | None = None
    applies_to: str = "visit"  # entity | partner | visit
    entity: int | None = None  # index in the visit's entity rows (applies_to "entity")
    partner_id: int | None = None  # the partner (applies_to "partner")
    from_row: bool = False  # the HACT answer written on the finding row (hact_q1_answer...)


def questions_answered(answers: Iterable[AnswerFacts]) -> tuple[int, int]:
    """(asked, answered): one per checklist question and entity (or partner, or the visit), answered
    when one of its records is. The HACT answers written on the finding rows are not checklist
    questions and are not counted."""
    asked: dict[tuple[str, str], bool] = {}
    for answer in answers:
        if answer.from_row:
            continue
        if answer.applies_to == "entity" and answer.entity is not None:
            unit = f"entity:{answer.entity}"
        elif answer.applies_to == "partner" and answer.partner_id is not None:
            unit = f"partner:{answer.partner_id}"
        else:
            unit = "visit"
        pair = (answer.question_key, unit)
        asked[pair] = asked.get(pair, False) or answer.answered
    return len(asked), sum(asked.values())


# ------------------------------------------------------------------------------------------ the record
@dataclass(frozen=True)
class Record:
    """What the rules see of one visit (``score`` builds it): whether it is scored, the kinds of its
    finding rows, whether each column of the report is present (``present``: True, False, or None when
    the data cannot tell), the values of the other columns (``values``; None: not in the data), the
    columns NeuroDB cannot read at all (``unreadable``: a rule on one is "not available", never a missing
    value), the reference facts (``refs``) and the answers of its AI checks (``checks``: rule ->
    (passed, detail))."""

    key: str
    scorable: bool
    status_group: str = "reported"
    kinds: frozenset[str] = frozenset()
    present: Mapping[str, bool | None] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)
    refs: Mapping[str, Any] = field(default_factory=dict)
    checks: Mapping[str, tuple[bool, str]] = field(default_factory=dict)
    unreadable: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Context:
    """What every visit's rules share: whether the AI checks count (``ai_checks``, Score settings) and
    whether they are made now (``ai_on``: the AI is switched on too; a check not made yet is then
    pending), the prompt keys the published version has instructions for, and the field offices' staff
    lists (R19: folded office -> addresses)."""

    ai_on: bool = False
    ai_checks: bool = True
    prompt_keys: frozenset[str] = frozenset()
    staff: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    rule: str
    status: str
    deduction: Decimal
    max_deduction: Decimal
    detail_key: str = ""
    detail: str = ""
    measure: float | None = None
    category: str = ""


# ------------------------------------------------------------------------------------------ helpers
def param(rule, name: str, default: Any = None) -> Any:
    params = rule.params if isinstance(rule.params, dict) else {}
    return params.get(name, default)


def bands_of(rule) -> list[dict]:
    """Every scoring band of a deterministic rule (its own and its entity types')."""
    out = list(param(rule, "scoring") or [])
    for spec in (param(rule, "entity_type_scoring") or {}).values():
        if isinstance(spec, dict):
            out += list(spec.get("scoring") or [])
    return [b for b in out if isinstance(b, dict)]


def max_deduction(rule) -> Decimal:
    """The most a rule can take off a visit."""
    if rule.type == "completeness":
        fields = [f for f in param(rule, "fields") or [] if isinstance(f, dict)]
        return sum((dec(f.get("deduction")) for f in fields), Decimal(0))
    if rule.type == "deterministic":
        found = [dec(b.get("deduction")) for b in bands_of(rule)]
        return max([*found, dec(param(rule, "missing_value_deduction")), Decimal(0)])
    return dec(rule.deduction)


def nominal(rule) -> Decimal:
    """A rule's weight in its category (what the category's sum and Rebalance read): a completeness
    rule's fields together; a rule with bands its deduction (else its largest band); else its deduction."""
    if rule.type == "completeness":
        return max_deduction(rule)
    if rule.type == "deterministic" and not dec(rule.deduction):
        return max_deduction(rule)
    return dec(rule.deduction)


def entity_types(rule) -> frozenset[str]:
    """The finding row kinds a rule applies to (empty: every visit)."""
    found = param(rule, "entity_type_filter")
    if not found:
        return frozenset()
    names = [found] if isinstance(found, str) else list(found)
    return frozenset(ENTITY_TYPES.get(parse.fold(name), "") for name in names) - {""}


def render(template: str, **values: Any) -> str:
    """A flag template with its placeholders filled ({value}, {key_value}, {missing_fields},
    {entity_type}, {ai_detail}); one with no value is left out, with a dash left hanging at the end."""

    def fill(match: re.Match) -> str:
        value = values.get(match.group(1))
        return "" if value in (None, "") else str(value)

    text = " ".join(_PLACEHOLDER.sub(fill, template or "").split())
    text = text.replace(" ''", "").replace("''", "")
    text = re.sub(r"\s*[—-]\s*$", "", text)
    return " ".join(text.split())


def _outcome(rule, status: str, deduction: Any = 0, key: str = "", detail: str = "", measure=None) -> Outcome:
    top = max_deduction(rule)
    taken = min(max(half_up(dec(deduction), 1), Decimal(0)), top) if status == "fail" else Decimal(0)
    return Outcome(rule.code, status, taken, top, key[:40], detail[:600], measure, rule.category)


def _flag(rule, deduction: Any, key: str, measure=None, **values) -> Outcome:
    return _outcome(rule, "fail", deduction, key, render(rule.flag_template, **values), measure)


def _split(value: Any, separator: str) -> list[str]:
    """A list column's entries: a list as it is, a text split at ``separator``; blanks left out."""
    if value is None:
        return []
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
    else:
        items = str(value).split(separator or ";")
    return [str(item).strip() for item in items if str(item).strip()]


def _band(bands: list[dict], value: float, required_met: bool = True, count: bool = False) -> dict | None:
    """The first band matching ``value`` (a number, or a count of categories), in the order listed."""
    for band in bands:
        low = band.get("min_categories") if count else band.get("min")
        high = band.get("max_inclusive")
        if low is not None and value < float(low):
            continue
        if high is not None and value > float(high):
            continue
        if band.get("required_met") and not required_met:
            continue
        return band
    return None


# ------------------------------------------------------------------------------------------ the types
def completeness(record: Record, rule, ctx: Context) -> Outcome:
    missing, evaluated, total = [], 0, Decimal(0)
    for index, spec in enumerate(param(rule, "fields") or []):
        name = spec.get("name", "")
        present = record.present.get(name)
        if present is None:
            continue
        evaluated += 1
        if not present:
            missing.append((index, spec.get("label") or COLUMNS.get(name, name)))
            total += dec(spec.get("deduction"))
    if not evaluated:
        return _outcome(
            rule, "na", key="no_fields", detail="None of the fields this rule checks is in the data."
        )
    if missing:
        key = "missing:" + ",".join(str(i) for i, _label in missing)
        labels = ", ".join(label for _i, label in missing)
        return _flag(rule, total, key, float(len(missing)), missing_fields=labels)
    return _outcome(rule, "pass", key="complete", detail="Every field this rule checks is filled.")


def _type_spec(specs: Mapping[str, Any], kind: str) -> dict | None:
    for name, spec in specs.items():
        if name != "default" and ENTITY_TYPES.get(parse.fold(name)) == ENTITY_TYPES.get(parse.fold(kind)):
            return spec
    return specs.get("default")


def _list_outcome(record: Record, rule, value: Any) -> Outcome:
    entries = _split(value, param(rule, "list_separator", ";"))
    specs = param(rule, "entity_type_scoring") or {}
    found: list[tuple[Decimal, str]] = []
    kinds = [KIND_NAMES[k] for k in sorted(record.kinds) if k in KIND_NAMES]
    for kind in kinds or ["default"]:
        spec = _type_spec(specs, kind) if specs else {"scoring": param(rule, "scoring") or []}
        if spec is None:
            continue
        required = {parse.fold(r) for r in spec.get("required") or []}
        met = required <= {parse.fold(e) for e in entries}
        band = _band(spec.get("scoring") or [], len(entries), met, count=True)
        if band is not None:
            found.append((dec(band.get("deduction")), kind))
    if not found:
        return _outcome(rule, "nap", key="no_band", detail="No scoring band applies to this visit.")
    deduction, kind = max(found, key=lambda f: f[0])
    text = "; ".join(entries) or "none"
    if deduction:
        return _flag(rule, deduction, "band", float(len(entries)), value=text, entity_type=kind)
    label = COLUMNS.get(param(rule, "field", ""), "")
    return _outcome(rule, "pass", key="band", detail=f"{label}: {text}.", measure=float(len(entries)))


def deterministic(record: Record, rule, ctx: Context) -> Outcome:
    name = param(rule, "field", "")
    value = record.values.get(name)
    label = COLUMNS.get(name, name)
    is_list = param(rule, "field_type") == "list"
    if value is None and name in record.unreadable:
        return _outcome(
            rule, "na", key="unreadable", detail=f"{label}: not in the eTools data NeuroDB reads."
        )
    if value is None or (not is_list and isinstance(value, str) and not value.strip()):
        missing = dec(param(rule, "missing_value_deduction"))
        if not missing:
            return _outcome(rule, "na", key="missing", detail=f"{label}: not in the eTools data.")
        return _outcome(rule, "fail", missing, "missing", f"{rule.code}: {label} not in the eTools data")
    if is_list:
        return _list_outcome(record, rule, value)
    try:
        measure = float(value)
    except (TypeError, ValueError):
        return _outcome(rule, "na", key="not_a_number", detail=f"{label}: not a number in the data.")
    band = _band(param(rule, "scoring") or [], measure)
    shown = number(measure)
    unit = "%" if name.endswith("_pct") else ""
    if band is None:
        return _outcome(
            rule, "nap", key="no_band", detail="No scoring band applies to this value.", measure=measure
        )
    deduction = dec(band.get("deduction"))
    if deduction:
        return _flag(rule, deduction, "band", measure, value=shown)
    return _outcome(rule, "pass", key="band", detail=f"{label}: {shown}{unit}.", measure=measure)


def narrative(record: Record, rule, ctx: Context) -> Outcome:
    """An AI check: its answer while it is up to date; else pending while the AI checks are made, or
    switched off while the AI is (an answer kept still counts, so a pause never moves the scores)."""
    if not ctx.ai_checks:
        return _outcome(rule, "off", key="ai_off", detail="AI checks are switched off (Score settings).")
    if param(rule, "ai_prompt_key", "") not in ctx.prompt_keys:
        detail = "The published prompt version has no instructions for this AI check."
        return _outcome(rule, "off", key="no_prompt", detail=detail)
    found = record.checks.get(rule.code)
    if found is None and not ctx.ai_on:
        detail = "Not checked: the AI is switched off."
        return _outcome(rule, "off", key="ai_off", detail=detail)
    if found is None:
        return _outcome(rule, "pending", key="pending", detail="AI check not done yet.")
    passed, detail = found
    if passed:
        return _outcome(rule, "pass", key="ai", detail="AI check passed" + (f": {detail}" if detail else "."))
    return _flag(rule, rule.deduction, "ai", ai_detail=detail)


def _folded_map(rule) -> dict[str, list]:
    found = param(rule, "reference_map") or {}
    if not isinstance(found, dict):
        return {}
    return {parse.fold(key): list(values or []) for key, values in found.items()}


def _pcodes(entries: Iterable[Any]) -> set[str]:
    out = set()
    for entry in entries:
        code = entry.get("pcode") if isinstance(entry, dict) else entry
        if code:
            out.add(str(code).strip().casefold())
    return out


def reference(record: Record, rule, ctx: Context) -> Outcome:
    check = param(rule, "check_type", "")
    if check == "member_in_mapped_list":
        return _members(record, rule, ctx)
    if check in ("value_in_mapped_list", "pd_reference_locations"):
        return _locations(record, rule, check)
    if check == "section_in_cp_output":
        return _sections(record, rule)
    if check == "string_contains":
        return _contains(record, rule)
    if check == "value_in_list":
        return _in_list(record, rule)
    return _outcome(rule, "off", key="unknown_check", detail="Unknown check type.")


def _contains(record: Record, rule) -> Outcome:
    value = record.values.get(param(rule, "field", ""))
    if value is None:
        return _outcome(rule, "na", key="missing", detail="The field this rule reads is not in the data.")
    wanted = parse.fold(param(rule, "contains", ""))
    if wanted and wanted in parse.fold(value):
        return _outcome(rule, "pass", key="contains", detail=f"{param(rule, 'contains')} is covered.")
    return _flag(rule, rule.deduction, "absent", value=value)


def _in_list(record: Record, rule) -> Outcome:
    name = param(rule, "field", "")
    listed = {str(v).strip().casefold() for v in param(rule, "reference_list") or [] if str(v).strip()}
    if not listed:
        return _outcome(rule, "nap", key="no_list", detail="Its reference list is empty.")
    found = (record.refs.get("people") or {}).get(name) if name in PERSON_COLUMNS else record.values.get(name)
    if not found:
        return _outcome(rule, "na", key="missing", detail="The field this rule reads is not in the data.")
    found = found if isinstance(found, set | list | tuple | frozenset) else [found]
    values = {str(v).strip().casefold() for v in found}
    if values & listed:
        return _outcome(rule, "pass", key="listed", detail="On the reference list.")
    shown = "" if name in PERSON_COLUMNS else ", ".join(sorted(values))  # a person is never written
    return _flag(rule, rule.deduction, "not_listed", value=shown)


def _members(record: Record, rule, ctx: Context) -> Outcome:
    """R19: every monitor e-mail address of the visit is on the staff list of one of its field offices.
    Skipped while no office of the visit has a list; the addresses are never written anywhere."""
    offices = [o for o in record.values.get("field_offices") or [] if parse.fold(o) in ctx.staff]
    if not offices:
        return _outcome(rule, "nap", key="no_list", detail="No staff list for the visit's field office.")
    emails = set((record.refs.get("people") or {}).get("team_members") or ())
    if not emails:
        return _outcome(rule, "na", key="no_email", detail="No monitor e-mail address in the eTools data.")
    allowed = set().union(*(ctx.staff[parse.fold(o)] for o in offices))
    if emails <= allowed:
        return _outcome(rule, "pass", key="listed", detail="The monitor is on the field office's staff list.")
    office = ", ".join(offices)
    return _flag(rule, rule.deduction, "not_listed", float(len(emails - allowed)), value="", key_value=office)


def _locations(record: Record, rule, check: str) -> Outcome:
    """R20 (``value_in_mapped_list``): the visited place (its P-code, or a place holding it) is one of
    the registered locations of the visit's programme documents; R23 (``pd_reference_locations``): the
    same, else against the planned locations of the partner's programme documents running then."""
    extra = _folded_map(rule)
    allowed: set[str] = set()
    keys: list[str] = []
    for number_, codes in (record.refs.get("pd_locations") or {}).items():
        allowed |= set(codes)
        keys.append(number_)
    names = list(record.refs.get("pd_numbers") or [])
    if check == "pd_reference_locations":
        names += list(record.refs.get("partner_names") or []) + list(record.refs.get("cp_outputs") or [])
        if not allowed:
            for number_, codes in (record.refs.get("partner_pd_locations") or {}).items():
                allowed |= set(codes)
                keys.append(number_)
    for name in names:
        entries = extra.get(parse.fold(name))
        if entries:
            allowed |= _pcodes(entries)
            keys.append(name)
    if not allowed:
        detail = "No registered location for its programme document."
        return _outcome(rule, "nap", key="no_reference", detail=detail)
    visited = {c.casefold() for c in record.refs.get("place_pcodes") or () if c}
    if not visited:
        return _outcome(rule, "na", key="no_place", detail="The visit's place has no P-code in the data.")
    if visited & allowed:
        return _outcome(rule, "pass", key="registered", detail="The visited place is a registered location.")
    place = record.values.get("location_pcode") or ""
    shown = ", ".join(dict.fromkeys(keys))
    return _flag(rule, rule.deduction, "not_registered", value=place, key_value=shown)


def _sections(record: Record, rule) -> Outcome:
    """R21: each section of a CP output visit is one of the sections the CP output works with (the
    programme documents that name it, and the rule's own map)."""
    extra = _folded_map(rule)
    expected: set[str] = set()
    outputs = list(record.refs.get("cp_outputs") or [])
    for output in outputs:
        expected |= {parse.fold(s) for s in (record.refs.get("cp_output_sections") or {}).get(output, ())}
        folded = parse.fold(output)
        for key, sections in extra.items():
            if key and (key == folded or key in folded):
                expected |= {parse.fold(s) for s in sections}
    if not expected:
        return _outcome(rule, "nap", key="no_reference", detail="No sections known for its CP output.")
    sections = list(record.values.get("sections_names") or [])
    if not sections:
        return _outcome(rule, "na", key="no_section", detail="The visit names no section.")
    wrong = [s for s in sections if parse.fold(s) not in expected]
    if not wrong:
        return _outcome(rule, "pass", key="aligned", detail="Its sections are those of its CP output.")
    return _flag(rule, rule.deduction, "not_aligned", float(len(wrong)), value=wrong[0], key_value=outputs[0])


EVALUATE = {
    "completeness": completeness,
    "deterministic": deterministic,
    "narrative": narrative,
    "reference_check": reference,
}


def evaluate(record: Record, rules: Mapping[str, Any], ctx: Context) -> list[Outcome]:
    """The outcome of each rule switched on for one visit, in the order of their ids (a rule switched off
    has none: with FMS's 32 rules, most of them off, a row each would multiply the rows kept for
    nothing): ``nap`` for all of them when the visit is not scored, and for a rule whose entity types
    the visit has no row of."""
    out = []
    for code in sorted(rules, key=code_order):
        rule = rules[code]
        if not rule.enabled:
            continue
        if not record.scorable:
            detail = CANCELLED_DETAIL if record.status_group == "cancelled" else PENDING_DETAIL
            out.append(_outcome(rule, "nap", key="not_scorable", detail=detail))
        elif (kinds := entity_types(rule)) and not (kinds & record.kinds):
            names = ", ".join(sorted(KIND_NAMES[k] for k in kinds))
            out.append(_outcome(rule, "nap", key="entity_type", detail=f"Applies to {names} visits only."))
        else:
            out.append(EVALUATE.get(rule.type, reference)(record, rule, ctx))
    return out


# ------------------------------------------------------------------------------------------ settings
def _number_ok(value: Any, low: float = 0, high: float = 100) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high


def _bands_errors(bands: Any, name: str, count: bool = False) -> list[str]:
    if not isinstance(bands, list) or not bands:
        return [f'“{name}” must list at least one band, e.g. [{{"min": 80, "deduction": 0}}].']
    limits = {"min_categories", "required_met", "max_inclusive"} if count else {"min", "max_inclusive"}
    for band in bands:
        if not isinstance(band, dict) or not set(band) <= limits | {"deduction"} or "deduction" not in band:
            return [f"Each band of “{name}” has a deduction and only {', '.join(sorted(limits))}."]
        numbers = [band[k] for k in band if k not in ("deduction", "required_met")]
        if not _number_ok(band["deduction"]) or not all(_number_ok(v, -1e9, 1e9) for v in numbers):
            return [f"The deductions and limits of “{name}” are numbers (deductions from 0 to 100)."]
    return []


def _check_fields(cleaned: dict, error) -> Decimal | None:
    fields = cleaned.get("fields")
    if not isinstance(fields, list) or not fields:
        error("params", "A completeness rule lists its fields, each with its name, label and deduction.")
        return None
    for spec in fields:
        if (
            not isinstance(spec, dict)
            or spec.get("name") not in COLUMNS
            or not _number_ok(spec.get("deduction"))
            or not isinstance(spec.get("label", ""), str)
        ):
            names = ", ".join(sorted(COLUMNS))
            error(
                "params",
                f"Each field has a name of the report ({names}), a label and a deduction (0 to 100).",
            )
            return None
    return sum((dec(s["deduction"]) for s in fields), Decimal(0))


def _check_deterministic(cleaned: dict, error) -> None:
    if cleaned.get("field") not in COLUMNS:
        error("params", "“field” names a column of the report, e.g. fmq_answered_pct or method_count.")
    kind = cleaned.get("field_type", "numeric")
    if kind not in ("numeric", "list"):
        error("params", "“field_type” is numeric or list.")
    if "missing_value_deduction" in cleaned and not _number_ok(cleaned["missing_value_deduction"]):
        error("params", "“missing_value_deduction” is a number from 0 to 100.")
    specs = cleaned.get("entity_type_scoring")
    if not specs:
        for message in _bands_errors(cleaned.get("scoring"), "scoring", count=kind == "list"):
            error("params", message)
        return
    if not isinstance(specs, dict):
        error("params", "“entity_type_scoring” maps entity types to their required categories and bands.")
        return
    for name, spec in specs.items():
        if not isinstance(spec, dict):
            error("params", f"“entity_type_scoring” → {name} needs “required” and “scoring”.")
            continue
        for message in _bands_errors(spec.get("scoring"), f"{name} scoring", count=True):
            error("params", message)


def _check_reference(cleaned: dict, error) -> None:
    check = cleaned.get("check_type")
    if check not in CHECK_TYPES:
        error("params", f"“check_type” is one of {', '.join(CHECK_TYPES)}.")
    if check == "string_contains" and not str(cleaned.get("contains") or "").strip():
        error("params", "A string_contains check needs the text it looks for (“contains”).")
    found = cleaned.get("reference_map")
    if found is not None:
        if (
            not isinstance(found, dict)
            or len(found) > MAX_LIST
            or any(not isinstance(v, list) for v in found.values())
        ):
            error("params", f"“reference_map” maps each key to a list (at most {MAX_LIST} keys).")
        elif check == "member_in_mapped_list" and any(found.values()):
            error(
                "params",
                "Staff e-mail addresses are kept in Field office staff lists, never in a rule: leave each "
                "list of “reference_map” empty.",
            )
    listed = cleaned.get("reference_list")
    if listed is not None and (not isinstance(listed, list) or len(listed) > MAX_LIST):
        error("params", f"“reference_list” is a list of at most {MAX_LIST} entries.")
    elif listed and any(not isinstance(v, str) or len(v) > MAX_ENTRY for v in listed):
        error("params", f"Each entry of “reference_list” is a text of at most {MAX_ENTRY} characters.")


def validate_rule(rule) -> tuple[dict, Decimal]:
    """A rule's ``params`` checked for its type, and its deduction (a completeness rule's is the sum of its
    fields'; a rule with bands and no deduction takes its largest band's). A ``ValidationError`` naming
    each problem in plain words otherwise."""
    errors: dict[str, list[str]] = {}

    def error(name: str, message: str) -> None:
        errors.setdefault(name, []).append(message)

    if rule.type not in TYPES:
        error("type", f"The type is one of {', '.join(TYPES)}.")
    if not _KEY.match(rule.category or ""):
        error("category", "The category is a key in lower case, e.g. completeness.")
    elif rule.enabled:
        from .models import ScoreSetting

        keys = [c.get("key") for c in ScoreSetting.load().categories or [] if isinstance(c, dict)]
        if rule.category not in keys:
            error(
                "category",
                f"“{rule.category}” is not one of the score categories ({', '.join(keys)}): add it in Score "
                "settings first, or choose another.",
            )
    params = rule.params if isinstance(rule.params, dict) else None
    if params is None:
        error("params", 'The parameters must be an object, e.g. {"field": "method_count"}.')
        params = {}
    allowed = PARAMS.get(rule.type, ())
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        what = TYPE_LABELS.get(rule.type, rule.type)
        error("params", f"A {what} rule has no “{', '.join(unknown)}” (its settings: {', '.join(allowed)}).")
    cleaned = {k: v for k, v in params.items() if k in allowed}
    filter_ = cleaned.get("entity_type_filter")
    if filter_ not in (None, "", []):
        names = [filter_] if isinstance(filter_, str) else filter_
        if not isinstance(names, list) or any(ENTITY_TYPES.get(parse.fold(n)) is None for n in names):
            error("params", "entity_type_filter names Partner, CP Output, PD/SSFA or PD.")
    deduction = dec(rule.deduction)
    if rule.type == "completeness":
        total = _check_fields(cleaned, error)
        deduction = total if total is not None else deduction
    elif rule.type == "deterministic":
        _check_deterministic(cleaned, error)
        if not deduction and "params" not in errors:
            deduction = max([dec(b.get("deduction")) for b in bands_of(SimpleRule(cleaned))] + [Decimal(0)])
    elif rule.type == "narrative":
        fields = cleaned.get("fields")
        if not isinstance(fields, list) or not fields or any(f not in COLUMNS for f in fields):
            error("params", f"An AI check lists the fields it reads, among {', '.join(sorted(COLUMNS))}.")
        key = cleaned.get("ai_prompt_key", "")
        if key and not _KEY.match(str(key)):
            error("params", "“ai_prompt_key” is a key in lower case, e.g. evidence_sufficiency.")
        elif rule.enabled and not key:
            error(
                "params", "An AI check that is on needs its “ai_prompt_key” (its instructions in the prompt)."
            )
    elif rule.type == "reference_check":
        _check_reference(cleaned, error)
    if not 0 <= deduction <= 100:
        error("deduction", "A deduction goes from 0 to 100.")
    if errors:
        raise ValidationError(errors)
    return cleaned, half_up(deduction, 1)


@dataclass
class SimpleRule:
    """A rule's parameters on their own (for :func:`bands_of` while a rule is checked)."""

    params: dict
    type: str = "deterministic"
    deduction: Any = 0


# ------------------------------------------------------------------------------------------ the rule set
def category_sums(rules_: Iterable[Any], categories: Mapping[str, Decimal]) -> list[dict[str, Any]]:
    """Per score category (in the settings' order, then any other a rule switched on names): its weight,
    the deductions of its rules switched on, and whether they match (FMS's "Sum of deductions" chips)."""
    sums: dict[str, Decimal] = {key: Decimal(0) for key in categories}
    for rule in rules_:
        if rule.enabled:
            sums[rule.category] = sums.get(rule.category, Decimal(0)) + nominal(rule)
    return [
        {
            "key": key,
            "weight": categories.get(key),
            "sum": half_up(total, 1),
            "matches": key in categories and abs(total - categories[key]) < Decimal("0.05"),
        }
        for key, total in sums.items()
    ]


def _scaled(value: Any, factor: Decimal) -> float | int:
    scaled = half_up(dec(value) * factor, 1)
    return int(scaled) if scaled == scaled.to_integral_value() else float(scaled)


def scale_rule(rule, factor: Decimal) -> tuple[dict, Decimal]:
    """A rule's parameters and deduction with every deduction multiplied by ``factor`` (one decimal)."""
    params = dict(rule.params or {})
    if rule.type == "completeness":
        params["fields"] = [
            {**f, "deduction": _scaled(f.get("deduction"), factor)} for f in params.get("fields") or []
        ]
    if rule.type == "deterministic":
        params["scoring"] = [
            {**b, "deduction": _scaled(b.get("deduction"), factor)} for b in params.get("scoring") or []
        ]
        if not params["scoring"]:
            params.pop("scoring")
        if "missing_value_deduction" in params:
            params["missing_value_deduction"] = _scaled(params["missing_value_deduction"], factor)
        if params.get("entity_type_scoring"):
            params["entity_type_scoring"] = {
                name: {
                    **spec,
                    "scoring": [
                        {**b, "deduction": _scaled(b.get("deduction"), factor)}
                        for b in spec.get("scoring") or []
                    ],
                }
                for name, spec in params["entity_type_scoring"].items()
            }
    if rule.type == "completeness":
        deduction = sum((dec(f["deduction"]) for f in params["fields"]), Decimal(0))
    else:
        deduction = half_up(dec(rule.deduction) * factor, 1)
    return params, deduction


def rebalance(
    rules_: Sequence[Any], categories: list[dict]
) -> tuple[dict[str, tuple[dict, Decimal]], list[dict]]:
    """FMS's Rebalance: the category weights scaled to sum 100, then each category's rules switched on
    scaled so that their deductions add up to its weight (a category without deductions is left). No
    rule is added, removed or switched on or off. Returns the rules' new (params, deduction) by code
    (only those that change) and the new categories."""
    total = sum((dec(c.get("weight")) for c in categories), Decimal(0))
    new_categories = [dict(c) for c in categories]
    if total and abs(total - 100) >= Decimal("0.05"):
        for entry in new_categories:
            entry["weight"] = _scaled(entry.get("weight"), Decimal(100) / total)
        _fix_total(new_categories, "weight", Decimal(100))
    weights = {c["key"]: dec(c["weight"]) for c in new_categories}
    out: dict[str, tuple[dict, Decimal]] = {}
    for key, weight in weights.items():
        members = [r for r in rules_ if r.enabled and r.category == key and nominal(r) > 0]
        current = sum((nominal(r) for r in members), Decimal(0))
        if not members or not current or abs(current - weight) < Decimal("0.05"):
            continue
        factor = weight / current
        scaled = {r.code: scale_rule(r, factor) for r in members}
        got = sum((_nominal_of(r, *scaled[r.code]) for r in members), Decimal(0))
        if got != weight:  # rounding: the largest rule takes the difference
            largest = max(members, key=lambda r: (_nominal_of(r, *scaled[r.code]), r.code))
            params, deduction = scaled[largest.code]
            scaled[largest.code] = _nudge(largest, params, deduction, weight - got)
        out.update(scaled)
    return out, new_categories


def _nominal_of(rule, params: dict, deduction: Decimal) -> Decimal:
    return nominal(SimpleRule(params, rule.type, deduction))


def _nudge(rule, params: dict, deduction: Decimal, difference: Decimal) -> tuple[dict, Decimal]:
    """A rule's deduction moved by ``difference`` (a completeness rule: its largest field)."""
    if rule.type == "completeness" and params.get("fields"):
        fields = [dict(f) for f in params["fields"]]
        largest = max(range(len(fields)), key=lambda i: dec(fields[i]["deduction"]))
        fields[largest]["deduction"] = _scaled(dec(fields[largest]["deduction"]) + difference, Decimal(1))
        return {**params, "fields": fields}, sum((dec(f["deduction"]) for f in fields), Decimal(0))
    if rule.type == "deterministic" and not dec(deduction):
        return params, deduction
    return params, half_up(deduction + difference, 1)


def _fix_total(entries: list[dict], name: str, total: Decimal) -> None:
    got = sum((dec(e[name]) for e in entries), Decimal(0))
    if got != total and entries:
        largest = max(entries, key=lambda e: dec(e[name]))
        largest[name] = _scaled(dec(largest[name]) + total - got, Decimal(1))
