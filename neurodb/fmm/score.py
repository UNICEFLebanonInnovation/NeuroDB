"""Scoring the visits (step 6 of the refresh): the question roles, HACT Q1 and the PSEA flag, the quality
rules, the score with its band and flags, and urgency.

**Question roles.** Each checklist question gets a role (Q1, Q2, Q3, PSEA or none) from its text and
the patterns of the score settings (``rules.assign_roles``). Everything that depends on a role is
worked out here, not when the visits are built, so a pattern change needs only a scores-only refresh:

- the **HACT answers written on a finding row** (``hact_q1_answer``, ``hact_q2_answer``,
  ``hact_q3_answer``) come first: a row's own answer replaces the checklist answers of that question
  given for that row; a row whose answer is blank keeps them (:func:`with_row_answers`);
- the **HACT Q1 of an entity**: its own Q1 answer, else the one given for its partner (on a row of its
  own or for the partner as a whole), else the one given for the whole visit (:func:`effective_q1`);
- the **HACT Q1 of a visit**: the worst of its entities' and of its visit-level Q1 answers;
- the **PSEA flag**: an answer to the PSEA question whose code the settings list ("yes", or a
  Constrained or Off track rating) flags the visit; ``None`` when no PSEA question was asked, or when
  the answers of the checklist records were not found (Fields found).

**Each record is scored** (FMS scores, rates and counts each record: an entity row of a visit). The
rules (``fmm.rules``, FMS's model) read what one record holds (:func:`record_of`, with ``kinds`` its
own entity type, so a rule's entity type filter and its entity type bands apply row by row): whether
each field of the row is filled (its narrative, its rating, Q1, Q2 and Q3 answered for it: its own
answer, its partner's or the visit's), the columns derived at the build over the answers that apply to
it (the share of questions answered, the categories answered, the methods...), the visit's own fields
(offices, sections, modality, action points: repeated on each record, as FMS does), the reference facts
eTools holds for that row (:class:`References`: its programme document's registered locations, its CP
output's sections, its place, else the visit's) and the answers of the AI checks (``AICheckAnswer``,
used only while the record's inputs and the rule's prompt are those it was checked with:
``fmm.ai.checks``). A rule that reads only what belongs to the visit (R19: the monitors' e-mail
addresses and the visit's field offices) is evaluated once per visit and its outcome copied to every
record (its entity type filter, when it has one, still applies record by record). The monitors' e-mail
addresses R19 compares are read from the records in code and dropped at once.

**Score** (:func:`score_outcome`), FMS's, per record: 100 less the deductions of the rules that fired,
each score category's deductions at most its weight (Score settings: categories), never below 0, rounded
half up to one decimal. Only the visits whose eTools status is one of the settings' *scored statuses*
(report finalization and completed by default) are scored: the others' records are "pending" (band
``pending``, no score), a cancelled one's "cancelled". A record whose AI checks are not all done is
**provisional**: its score so far is kept apart (``provisional_score``, with ``ai_pending``) and it
counts as not scored, so it never gets full marks for checks not made. Bands: High from 80, Medium from
50, else Low. The flags are the rules that fired.

**The visit** (:func:`aggregate_visit`): its quality is the mean of its records' scores (rounded half
up), its lowest record's score is kept, its band is the mean's, its flags the union of its records'
and its urgency its most urgent record's. A visit with a provisional record is provisional too: no
score and no urgency, its records' mean so far in ``provisional_score``. A visit has no rule results of
its own: every page reads its records' (``RecordRuleResult``; a rule read once per visit, R19, is kept
on each of its records).

**Urgency** (:func:`urgency`), FMS's formula, per record, 0 to 100: 50% the gap from the maximum score
(100 − the record's score), 30% recency (100 on the day the visit ended, falling to 0 at
``recency_days``, 180) and 20% red flags (25 per rule the record failed, at most 100); the weights are
the settings' and sum to 1. A record with no score has no urgency (``None``) and is left out of every
urgency figure. Red from 70, amber from 40. Each weighted part is kept (``urgency_parts``) to explain
it.

**Signals** (:func:`signals`), shown on the visit page and never part of urgency: no follow-up action
point 14 days after an Off track or Constrained reported visit; overdue or high-priority open action
points; a planned or in-progress visit that ended more than 30 days ago (a late report).

The visits are scored in batches of 500, each with its records. A full refresh scores the visits it has
just built (:class:`BuiltSource`); a scores-only one the visits stored (:class:`StoredSource`), which is
also what the admin's *Preview effect* scores in memory without writing anything.
"""

from __future__ import annotations

import copy
import functools
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

from neurodb.datamart.fm import RATING_ORDER, STATUSES

from . import parse, rules
from .models import (
    AICheckAnswer,
    FieldOfficeStaff,
    QuestionAnswer,
    RuleSetting,
    ScoreSetting,
    Visit,
    VisitEntity,
    default_categories,
    default_question_patterns,
    default_role_flag_answers,
    default_scored_statuses,
    default_urgency_weights,
)
from .rules import AnswerFacts, Outcome, half_up, q1_value, worst

logger = logging.getLogger(__name__)

BATCH = 500  # visits scored at once (their texts are read together for the AI checks)
ANSWER_CODES = ("on_track", "constrained", "off_track", "yes", "no")
MAX_PATTERNS = 20
MAX_CATEGORIES = 20
PENDING = "pending: its status is not scored"
CANCELLED = "cancelled"
PROVISIONAL = "provisional: {n} AI checks pending"
WEIGHT_NAMES = ("quality_gap", "recency", "red_flags")
FLAG_POINTS = 25  # each red flag (a failed rule) adds this to the flags part, at most 100
NOT_SCORED_ERROR = "could not be scored"
HUNDRED = Decimal(100)
PLACE_CHECKS = ("value_in_mapped_list", "pd_reference_locations", "section_in_cp_output")
# the reference checks that read only what belongs to the visit (its team and field offices): evaluated
# once per visit, their outcome copied to every record
VISIT_CHECKS = ("member_in_mapped_list",)


# ------------------------------------------------------------------------------------------ settings
@dataclass
class Rulebook:
    """The quality rules, the score settings, the AI check prompts of the published prompt version and
    the field offices' staff lists one scoring reads, loaded once."""

    rules: dict[str, RuleSetting]
    setting: ScoreSetting
    prompts: dict[str, str] = field(default_factory=dict)
    staff: dict[str, frozenset[str]] = field(default_factory=dict)
    ai_enabled: bool = False  # Monitoring insights, its AI and the assistant are switched on

    @classmethod
    def load(cls) -> Rulebook:
        from .ai import profiles

        version = profiles.published()
        prompts = {
            key: text
            for key, text in ((version.rule_prompts if version else None) or {}).items()
            if isinstance(text, str) and text.strip()
        }
        staff = {}
        for row in FieldOfficeStaff.objects.all():
            if addresses := row.addresses():
                staff[parse.fold(row.office)] = frozenset(addresses)
        return cls(
            {rule.code: rule for rule in RuleSetting.objects.all()},
            ScoreSetting.load(),
            prompts,
            staff,
            bool(settings.FMM_ENABLED and settings.FMM_AI and settings.AI_ASSISTANT_ENABLED),
        )

    def with_changes(self, rule_changes: Mapping[str, Mapping], score_changes: Mapping) -> Rulebook:
        """A copy with some settings changed (for a preview); nothing is saved."""
        changed = {code: copy.copy(rule) for code, rule in self.rules.items()}
        for code, values in (rule_changes or {}).items():
            if code not in changed:
                continue
            for name, value in values.items():
                setattr(changed[code], name, value)
        setting = copy.copy(self.setting)
        for name, value in (score_changes or {}).items():
            setattr(setting, name, value)
        return Rulebook(changed, setting, dict(self.prompts), dict(self.staff), self.ai_enabled)

    @property
    def categories(self) -> dict[str, Decimal]:
        return categories_of(self.setting)

    def split_rules(self) -> tuple[dict[str, RuleSetting], dict[str, RuleSetting]]:
        """(the rules evaluated on each record, those evaluated once per visit: :func:`visit_level`),
        worked out once per scoring."""
        if not hasattr(self, "_split"):
            visit = {code: rule for code, rule in self.rules.items() if visit_level(rule)}
            self._split = ({c: r for c, r in self.rules.items() if c not in visit}, visit)
        return self._split

    @property
    def ai_on(self) -> bool:
        return self.ai_enabled and bool(self.setting.ai_checks)

    def ai_rules(self) -> list[RuleSetting]:
        """The AI checks that count: the narrative rules switched on whose prompt key has instructions
        (none while the AI checks are switched off in Score settings)."""
        if not self.setting.ai_checks:
            return []
        return [
            rule
            for code, rule in sorted(self.rules.items(), key=lambda kv: rules.code_order(kv[0]))
            if rule.enabled
            and rule.type == "narrative"
            and rules.param(rule, "ai_prompt_key") in self.prompts
        ]

    def context(self) -> rules.Context:
        return rules.Context(
            ai_on=self.ai_on,
            ai_checks=bool(self.setting.ai_checks),
            prompt_keys=frozenset(self.prompts),
            staff=self.staff,
        )

    def _checks(self, *names: str) -> list[RuleSetting]:
        return [
            rule
            for rule in self.rules.values()
            if rule.enabled and rule.type == "reference_check" and rules.param(rule, "check_type") in names
        ]

    def needs_people(self) -> bool:
        """A rule that compares the monitors' e-mail addresses is on and has something to compare with."""
        if self.staff and self._checks("member_in_mapped_list"):
            return True
        return any(
            rules.param(rule, "field") in rules.PERSON_COLUMNS and rules.param(rule, "reference_list")
            for rule in self._checks("value_in_list")
        )

    def needs_places(self) -> bool:
        return bool(self._checks(*PLACE_CHECKS))

    @property
    def patterns(self) -> Mapping[str, list[str]]:
        patterns = self.setting.question_patterns
        return patterns if isinstance(patterns, dict) else default_question_patterns()

    @property
    def flag_codes(self) -> frozenset[str]:
        flags = self.setting.role_flag_answers
        flags = flags if isinstance(flags, dict) else default_role_flag_answers()
        return frozenset(flags.get("psea") or ())

    @property
    def weights(self) -> dict[str, float]:
        return weights_of(self.setting)

    @property
    def scored_statuses(self) -> frozenset[str]:
        return scored_statuses_of(self.setting)


def valid_weights(weights: Any) -> bool:
    """Urgency's three weights (quality_gap, recency, red_flags), numbers from 0 to 1 that sum to 1."""
    if not isinstance(weights, dict) or set(weights) != set(WEIGHT_NAMES):
        return False
    values = list(weights.values())
    if not all(isinstance(v, int | float) and not isinstance(v, bool) and 0 <= v <= 1 for v in values):
        return False
    return abs(sum(values) - 1) < 0.001


def weights_of(setting: ScoreSetting) -> dict[str, float]:
    """Urgency's weights as the settings hold them; the defaults (0.5, 0.3, 0.2) when they are not
    valid (:func:`valid_weights`: a rules version saved before Release 2 held others)."""
    weights = setting.urgency_weights
    if valid_weights(weights):
        return {k: float(weights[k]) for k in WEIGHT_NAMES}
    return default_urgency_weights()


def scored_statuses_of(setting: ScoreSetting) -> frozenset[str]:
    """The eTools statuses whose visits are scored; the defaults when the setting holds none."""
    found = setting.scored_statuses
    if isinstance(found, list) and found and all(isinstance(v, str) for v in found):
        return frozenset(found)
    return frozenset(default_scored_statuses())


def categories_of(setting: ScoreSetting) -> dict[str, Decimal]:
    """The score categories and their weights, in their order; FMS Lebanon's when the settings hold
    none that can be read."""
    found = setting.categories if isinstance(setting.categories, list) else []
    out = {
        str(c["key"]): rules.dec(c.get("weight"))
        for c in found
        if isinstance(c, dict) and c.get("key") and isinstance(c.get("weight"), int | float)
    }
    return out or {c["key"]: Decimal(c["weight"]) for c in default_categories()}


def category_labels(setting: ScoreSetting) -> dict[str, str]:
    found = setting.categories if isinstance(setting.categories, list) else default_categories()
    return {str(c.get("key")): str(c.get("label") or c.get("key")) for c in found if isinstance(c, dict)}


def _validate_categories(value: Any, error: Callable[[str, str], None]) -> list[dict] | None:
    if not isinstance(value, list) or not value or len(value) > MAX_CATEGORIES:
        error(
            "categories",
            f'List 1 to {MAX_CATEGORIES} categories, e.g. [{{"key": "completeness", "weight": 30}}].',
        )
        return None
    out, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"key", "label", "weight"}:
            error("categories", "Each category has a key, a label and a weight.")
            return None
        key = str(entry.get("key") or "")
        weight = entry.get("weight")
        if not rules._KEY.match(key) or key in seen:
            error("categories", f"Each key is written once, in lower case (not “{key}”).")
            return None
        if isinstance(weight, bool) or not isinstance(weight, int | float) or not 0 <= weight <= 100:
            error("categories", f"The weight of {key} is a number from 0 to 100.")
            return None
        seen.add(key)
        label = str(entry.get("label") or key.replace("_", " ").capitalize())[:60]
        out.append({"key": key, "label": label, "weight": weight})
    total = sum(entry["weight"] for entry in out)
    if abs(total - 100) > 0.05:
        error("categories", f"The weights add up to 100 (they add up to {round(total, 1)}): use Rebalance.")
        return None
    return out


def validate_settings(setting: ScoreSetting) -> None:
    """The score settings checked (``ScoreSetting.clean``); the question patterns are stored folded."""
    errors: dict[str, list[str]] = {}

    def error(name: str, message: str) -> None:
        errors.setdefault(name, []).append(message)

    if not setting.urgency_amber < setting.urgency_red <= 100:
        error("urgency_red", "Amber must be below red, and red at most 100.")
    if not setting.band_medium < setting.band_high <= 100:
        error("band_high", "The Medium band must start below the High band, and High at most 100.")
    categories = _validate_categories(setting.categories, error)
    if categories is not None:
        setting.categories = categories
        enabled = RuleSetting.objects.filter(enabled=True).exclude(
            category__in=[c["key"] for c in categories]
        )
        if enabled.exists():
            codes = ", ".join(sorted(enabled.values_list("code", flat=True), key=rules.code_order))
            error("categories", f"Rules that are on use a category left out ({codes}): keep it.")
    weights = setting.urgency_weights
    if not isinstance(weights, dict):
        error(
            "urgency_weights",
            'The weights must be an object, e.g. {"quality_gap": 0.5, "recency": 0.3, ...}.',
        )
    else:
        unknown = sorted(set(weights) - set(WEIGHT_NAMES))
        missing = sorted(set(WEIGHT_NAMES) - set(weights))
        if unknown:
            error("urgency_weights", f"Unknown weights: {', '.join(unknown)}.")
        if missing:
            error("urgency_weights", f"Missing weights: {', '.join(missing)}.")
        bad = [
            k
            for k, v in weights.items()
            if isinstance(v, bool) or not isinstance(v, int | float) or not 0 <= v <= 1
        ]
        if bad:
            error("urgency_weights", f"Each weight is a number from 0 to 1 (not: {', '.join(sorted(bad))}).")
        elif not unknown and not missing and abs(sum(weights.values()) - 1) >= 0.001:
            total = round(sum(weights.values()), 3)
            error("urgency_weights", f"The three weights must add up to 1 (they add up to {total}).")
    if setting.recency_days is None or not 1 <= setting.recency_days <= 3650:
        error("recency_days", "The recency window goes from 1 to 3,650 days.")
    statuses = setting.scored_statuses
    allowed = [code for code in STATUSES if code != "cancelled"]
    if not isinstance(statuses, list) or not statuses or not all(isinstance(v, str) for v in statuses):
        error("scored_statuses", "Choose at least one status whose visits are scored.")
    elif unknown_statuses := sorted(set(statuses) - set(allowed)):
        error(
            "scored_statuses",
            f"Unknown or unscorable statuses: {', '.join(unknown_statuses)} (they are {', '.join(allowed)}).",
        )
    else:
        setting.scored_statuses = [code for code in allowed if code in statuses]
    _validate_patterns(setting, error)
    flags = setting.role_flag_answers
    if not isinstance(flags, dict):
        error("role_flag_answers", 'The flagging answers must be an object, e.g. {"psea": ["yes"]}.')
    else:
        for role, values in flags.items():
            if role not in rules.ROLES:
                error("role_flag_answers", f"Unknown role “{role}”: the roles are {', '.join(rules.ROLES)}.")
            elif not isinstance(values, list) or not all(v in ANSWER_CODES for v in values):
                error("role_flag_answers", f"The answers of {role} can only be {', '.join(ANSWER_CODES)}.")
    if errors:
        raise ValidationError(errors)


def _validate_patterns(setting: ScoreSetting, error: Callable[[str, str], None]) -> None:
    patterns = setting.question_patterns
    if not isinstance(patterns, dict):
        error("question_patterns", 'The patterns must be an object, e.g. {"q1": ["implemented as planned"]}.')
        return
    cleaned: dict[str, list[str]] = {}
    failed = False
    for role, values in patterns.items():
        if role not in rules.ROLES:
            error("question_patterns", f"Unknown role “{role}”: the roles are {', '.join(rules.ROLES)}.")
            failed = True
            continue
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            error("question_patterns", f"The patterns of {role} must be a list of texts.")
            failed = True
            continue
        if len(values) > MAX_PATTERNS:
            error("question_patterns", f"{role} has {len(values)} patterns; at most {MAX_PATTERNS}.")
            failed = True
            continue
        out: list[str] = []
        for value in values:
            text = value.strip()
            prefix = text[0] if text[:1] in ("=", "^") else ""
            body = parse.fold(text[len(prefix) :])
            if not 2 <= len(text) <= 200 or not body:
                error(
                    "question_patterns",
                    f"Each pattern has 2 to 200 characters with a letter or digit (not “{value}”).",
                )
                failed = True
                continue
            if prefix + body not in out:
                out.append(prefix + body)
        cleaned[role] = out
    if not failed:
        setting.question_patterns = cleaned


# ------------------------------------------------------------------------------------------ Q1 and PSEA
def scorable(status: str, scored: Iterable[str]) -> bool:
    """A visit whose eTools status is one of the scored statuses (Score settings); the others are
    "pending" (a cancelled one: "cancelled")."""
    return bool(status) and status in set(scored)


def effective_q1(entities: Sequence[Any], answers: Sequence[AnswerFacts]) -> list[tuple[str, str]]:
    """(Q1, where from) of each entity (objects with ``kind`` and ``partner_id``), in their order: its
    own answered Q1 ("entity"); else the one given for its partner, as a whole or on the partner's own
    row ("partner"); else the visit-level one ("visit"); ("", "") when there is none. Several answers
    give their worst."""
    own: dict[int, list[str]] = defaultdict(list)
    by_partner: dict[int, list[str]] = defaultdict(list)
    visit_level: list[str] = []
    for answer in answers:
        if answer.role != "q1" or not answer.answered:
            continue
        if answer.applies_to == "entity" and answer.entity is not None:
            own[answer.entity].append(q1_value(answer))
        elif answer.applies_to == "partner" and answer.partner_id is not None:
            by_partner[answer.partner_id].append(q1_value(answer))
        else:
            visit_level.append(q1_value(answer))
    partner_rows: dict[int, list[tuple[int, str]]] = defaultdict(list)  # answers on a partner's own row
    for index, entity in enumerate(entities):
        if entity.kind == "partner" and entity.partner_id:
            partner_rows[entity.partner_id] += [(index, v) for v in own.get(index, ())]
    out = []
    for index, entity in enumerate(entities):
        if own.get(index):
            out.append((worst(own[index]), "entity"))
            continue
        partner = by_partner.get(entity.partner_id, []) if entity.partner_id else []
        partner = partner + [v for i, v in partner_rows.get(entity.partner_id, ()) if i != index]
        if partner:
            out.append((worst(partner), "partner"))
        elif visit_level:
            out.append((worst(visit_level), "visit"))
        else:
            out.append(("", ""))
    return out


def visit_q1(entity_q1: Iterable[str], answers: Sequence[AnswerFacts]) -> str:
    """The HACT Q1 of a visit: the worst of its entities' Q1 and its visit-level Q1 answers ("" when
    neither is in the data)."""
    visit_level = [
        q1_value(a)
        for a in answers
        if a.role == "q1" and a.answered and a.applies_to not in ("entity", "partner")
    ]
    return worst(list(entity_q1) + visit_level)


ROW_ROLES = ("q1", "q2", "q3")


def with_row_answers(entities: Sequence[Any], answers: Sequence[AnswerFacts]) -> list[AnswerFacts]:
    """A visit's checklist answers with the HACT answers written on its finding rows
    (``VisitEntity.row_answers``, measured at the build). A row's own answer to Q1, Q2 or Q3, when it
    is written (a placeholder too), replaces the checklist answers of that question given for that row;
    a blank one adds an unanswered answer and keeps them, as the fallback."""
    added: list[AnswerFacts] = []
    replaced: set[tuple[int, str]] = set()
    for index, entity in enumerate(entities):
        for role, measured in (getattr(entity, "row_answers", None) or {}).items():
            if role not in ROW_ROLES or not isinstance(measured, dict):
                continue
            answered = bool(measured.get("answered"))
            placeholder = bool(measured.get("placeholder"))
            if answered or placeholder:
                replaced.add((index, role))
            added.append(
                AnswerFacts(
                    question_key=f"row:{role}",
                    role=role,
                    answered=answered,
                    placeholder=placeholder,
                    words=int(measured.get("words") or 0),
                    rating=str(measured.get("rating") or ""),
                    answer_code=str(measured.get("code") or ""),
                    applies_to="entity",
                    entity=index,
                    from_row=True,
                )
            )
    kept = [a for a in answers if not (a.applies_to == "entity" and (a.entity, a.role) in replaced)]
    return kept + added


def row_roles(entities: Iterable[Any]) -> set[str]:
    """The questions (q1, q2, q3) whose answers the finding rows carry."""
    return {
        role
        for entity in entities
        for role in (getattr(entity, "row_answers", None) or {})
        if role in ROW_ROLES
    }


def psea_flag(answers: Sequence[AnswerFacts], flagging: Iterable[str]) -> bool | None:
    """True when an answer to a PSEA question has a flagging code, False when PSEA was asked and not
    flagged, None when the visit has no PSEA question."""
    psea = [a for a in answers if a.role == "psea"]
    if not psea:
        return None
    codes = set(flagging)
    return any(a.answered and a.answer_code in codes for a in psea)


# ------------------------------------------------------------------------------------------ score
@dataclass(frozen=True)
class ScoreOutcome:
    score: Decimal | None
    provisional: Decimal | None
    pending: int
    deductions: dict[str, float]  # per category, each at most its weight
    evaluated: tuple[str, ...]
    band: str
    flags: tuple[str, ...]
    not_scored_reason: str


def score_outcome(
    outcomes: Sequence[Outcome], s: ScoreSetting, scorable_: bool = True, status_group: str = ""
) -> ScoreOutcome:
    """A record's score from its rules' outcomes (see the module's description)."""
    flags = tuple(o.rule for o in outcomes if o.status == "fail")
    evaluated = tuple(o.rule for o in outcomes if o.status in ("pass", "fail"))
    if not scorable_:
        if status_group == "cancelled":
            return ScoreOutcome(None, None, 0, {}, (), "", (), CANCELLED)
        return ScoreOutcome(None, None, 0, {}, (), "pending", (), PENDING)
    weights = categories_of(s)
    taken: dict[str, Decimal] = defaultdict(Decimal)
    for outcome in outcomes:
        if outcome.status == "fail":
            taken[outcome.category] += outcome.deduction
    capped = {
        category: min(total, weights[category]) if category in weights else total
        for category, total in taken.items()
    }
    value = half_up(max(Decimal(0), HUNDRED - sum(capped.values(), Decimal(0))), 1)
    deductions = {category: float(half_up(total, 1)) for category, total in capped.items() if total}
    pending = sum(1 for o in outcomes if o.status == "pending")
    if pending:
        reason = PROVISIONAL.format(n=pending)
        return ScoreOutcome(None, value, pending, deductions, evaluated, "", flags, reason)
    return ScoreOutcome(value, None, 0, deductions, evaluated, band_of(value, s), flags, "")


score_visit = score_outcome  # its name before records were scored (Release 2 step 5)


def band_of(value: Decimal, s: ScoreSetting) -> str:
    """The band of a score: High from ``band_high``, Medium from ``band_medium``, else Low."""
    return "high" if value >= s.band_high else "medium" if value >= s.band_medium else "low"


def high_flag(flag_count: int, s: ScoreSetting) -> bool:
    """Many flags: at least the settings' ``high_flag_count`` (3) rules failed."""
    return flag_count >= s.high_flag_count


def recency(end_date: date | None, today: date, days: int) -> float:
    """100 on the day a visit ended (and for a visit that ends later), falling to 0 at ``days`` days
    after it; 0 for a visit without an end date."""
    if end_date is None or days <= 0:
        return 0.0
    since = (today - end_date).days
    return max(0.0, min(100.0, 100.0 * (1 - since / days)))


def urgency(visit: Visit, score: ScoreOutcome, s: ScoreSetting, today: date) -> tuple[int | None, str, dict]:
    """The urgency of a record of ``visit`` scored ``score`` (FMS's formula, see the module's
    description; recency counts from the visit's end), its band (red, amber or "") and its weighted
    parts; ``(None, "", {})`` without a score."""
    if score.score is None:
        return None, "", {}
    w = weights_of(s)
    gap = 100.0 - float(score.score)
    recent = recency(visit.end_date, today, s.recency_days)
    flags = float(min(100, FLAG_POINTS * len(score.flags)))
    parts = {
        "quality_gap": w["quality_gap"] * gap,
        "recency": w["recency"] * recent,
        "red_flags": w["red_flags"] * flags,
    }
    total = int(half_up(Decimal(str(sum(parts.values()))), 0))
    total = max(0, min(100, total))
    band = "red" if total >= s.urgency_red else "amber" if total >= s.urgency_amber else ""
    return total, band, {name: float(half_up(Decimal(str(value)), 1)) for name, value in parts.items()}


def signals(visit: Visit, links: Sequence[Any], s: ScoreSetting, today: date) -> dict[str, Any]:
    """The visit's follow-up and late-report signals (shown on the visit page, never part of urgency):
    ``no_follow_up`` (an Off track or Constrained reported visit with no action point more than
    ``follow_up_days`` after it ended), its overdue, high-priority overdue and high-priority open action
    points, and ``report_late_days`` (a planned or in-progress visit that ended more than
    ``report_late_days`` ago). Only what applies is kept. ``links``: ``build.ActionPointFacts``."""
    from neurodb.datamart.models import ActionPoint

    out: dict[str, Any] = {}
    worse = max([visit.rating, visit.hact_q1 or "not_monitored"], key=lambda v: RATING_ORDER.get(v, 0))
    if (
        worse in ("off_track", "constrained")
        and visit.status_group == "reported"
        and not links
        and visit.end_date
        and (today - visit.end_date).days > s.follow_up_days
    ):
        out["no_follow_up"] = True
    open_ = [a for a in links if a.status in ActionPoint.OPEN_STATUSES]
    overdue = [a for a in open_ if a.due_date and a.due_date < today]
    if overdue:
        out["ap_overdue"] = len(overdue)
    if high := sum(1 for a in overdue if a.high_priority):
        out["ap_high_overdue"] = high
    if high_open := sum(1 for a in open_ if a.high_priority and not (a.due_date and a.due_date < today)):
        out["ap_high_open"] = high_open
    if (
        visit.status_group in ("planned", "in_progress")
        and visit.end_date
        and visit.end_date < today - timedelta(days=s.report_late_days)
    ):
        out["report_late_days"] = (today - visit.end_date).days
    return out


# ------------------------------------------------------------------------------------------ references
class References:
    """What eTools holds that the reference checks compare a record with (R20, R21, R23), read once per
    scoring: the registered locations of every programme document (``PCA.location_p_codes`` and its
    locations' P-codes), the programme documents of each partner, the sections of each CP output (the
    programme documents that name it), the partners' names and the P-codes of the places holding each
    place (a visit at a cadaster lies in its district)."""

    def __init__(self, load: bool = True) -> None:
        self.pd_codes: dict[int, frozenset[str]] = {}
        self.pd_numbers: dict[int, str] = {}
        self.partner_pds: dict[int, list[tuple[int, date | None, date | None]]] = defaultdict(list)
        self.output_sections: dict[str, set[str]] = defaultdict(set)
        self.partner_names: dict[int, list[str]] = {}
        self.parents: dict[int, tuple[int | None, str]] = {}
        self.site_parents: dict[int, int | None] = {}
        if load:
            self._load()

    def _load(self) -> None:
        from neurodb.datamart.models import MonitoringSite
        from neurodb.geo.models import Location
        from neurodb.partnerships.models import PCA, PartnerOrganization

        codes: dict[int, set[str]] = defaultdict(set)
        rows = PCA.objects.values_list(
            "pk", "number", "partner_id", "start", "end", "location_p_codes", "cp_outputs", "section_names"
        )
        for pk, number, partner, start, end, pcodes, outputs, sections in rows:
            self.pd_numbers[pk] = number or ""
            codes[pk] |= {str(c).strip().casefold() for c in pcodes or () if str(c).strip()}
            if partner:
                self.partner_pds[partner].append((pk, start, end))
            for output in outputs or ():
                self.output_sections[parse.fold(output)] |= {s for s in sections or () if s}
        through = PCA.locations.through.objects.values_list("pca_id", "location__p_code")
        for pk, pcode in through:
            if pcode:
                codes[pk].add(pcode.strip().casefold())
        self.pd_codes = {pk: frozenset(found) for pk, found in codes.items() if found}
        self.partner_names = {
            pk: [n for n in (name, short) if n]
            for pk, name, short in PartnerOrganization.objects.values_list("pk", "name", "short_name")
        }
        self.parents = {
            pk: (parent, (pcode or "").strip().casefold())
            for pk, parent, pcode in Location.objects.values_list("pk", "parent_id", "p_code")
        }
        self.site_parents = dict(MonitoringSite.objects.values_list("pk", "parent_id"))

    def place_codes(self, visit: Visit, entity: VisitEntity | None = None) -> list[str]:
        """The P-code of a record's place and those of the places holding it, nearest first: the place
        its row names (its P-code, its location), else the visit's."""
        own_pcode = ((getattr(entity, "location_pcode", "") or "").strip()) if entity is not None else ""
        own_node = getattr(entity, "location_id", None) if entity is not None else None
        if own_pcode or own_node:
            out = [own_pcode.casefold()] if own_pcode else []
            node = own_node
        else:
            out = [visit.place_pcode.strip().casefold()] if visit.place_pcode.strip() else []
            node = visit.location_id or (self.site_parents.get(visit.site_id) if visit.site_id else None)
        hops = 0
        while node and hops < 10:
            parent, pcode = self.parents.get(node, (None, ""))
            if pcode and pcode not in out:
                out.append(pcode)
            node, hops = parent, hops + 1
        return out

    def facts(self, visit: Visit, entity: VisitEntity | None = None) -> dict[str, Any]:
        """The reference facts of one record of ``visit`` (``entity``), for the rules
        (``rules.Record.refs``): its own programme document, partner and CP output, and its place (else
        the visit's); a visit's as a whole when ``entity`` is None. Dictionary look-ups only."""
        if entity is None:
            pd_ids = list(visit.pd_ids or ())
            pd_numbers = list(visit.pd_numbers or [])
            partner_ids = list(visit.partner_ids or ())
            outputs = list(visit.cp_outputs or [])
        else:
            pd_ids = [entity.pd_id] if entity.pd_id else []
            pd_numbers = [self.pd_numbers[pk] for pk in pd_ids if self.pd_numbers.get(pk)]
            partner_ids = [entity.partner_id] if entity.partner_id else []
            outputs = [entity.cp_output] if (entity.cp_output or "").strip() else []
        pd_locations = {
            self.pd_numbers.get(pk) or str(pk): self.pd_codes[pk] for pk in pd_ids if pk in self.pd_codes
        }
        partner_pd_locations = {}
        day = visit.visit_date  # the partner's documents running when the visit started (else ended)
        for partner in partner_ids:
            for pk, start, end in self.partner_pds.get(partner, ()):
                running = day is None or ((start is None or start <= day) and (end is None or day <= end))
                if running and pk in self.pd_codes:
                    partner_pd_locations[self.pd_numbers.get(pk) or str(pk)] = self.pd_codes[pk]
        return {
            "pd_locations": pd_locations,
            "partner_pd_locations": partner_pd_locations,
            "pd_numbers": pd_numbers,
            "partner_names": [n for pid in partner_ids for n in self.partner_names.get(pid, ())],
            "cp_outputs": outputs,
            "cp_output_sections": {o: self.output_sections.get(rules.folded(o), set()) for o in outputs},
            "place_pcodes": self.place_codes(visit, entity),
        }


def monitor_emails(finding_ids: Iterable[int | None]) -> dict[int, set[str]]:
    """{finding pk: the e-mail addresses of its visit lead and team members}, read from the records for
    rule R19 and compared in code: never kept, shown or sent."""
    from neurodb.datamart.models import MonitoringFinding
    from neurodb.watch.people import EMAIL

    from . import fields

    ids = [pk for pk in set(finding_ids) if pk]
    key = fields.key_for("field_monitoring", "team")
    out: dict[int, set[str]] = defaultdict(set)
    if not ids:
        return out
    for pk, lead, data in MonitoringFinding.objects.filter(pk__in=ids).values_list(
        "pk", "visit_lead", "data"
    ):
        found = [lead or ""]
        if key and isinstance(data, dict):
            found.append(str(parse.walk(data, key) or ""))
        for text in found:
            out[pk] |= {match.strip("'.-").casefold() for match in EMAIL.findall(text)}
    return out


# ------------------------------------------------------------------------------------------ the step
@dataclass(frozen=True)
class AnswerIn:
    """One checklist answer as a source gives it to the scoring (no text)."""

    document_id: int
    question_key: str
    question_text: str
    is_hact: bool | None
    applies_to: str
    entity: int | None  # index in the visit's entity list
    partner_id: int | None
    answered: bool
    placeholder: bool
    answer_words: int
    summary_words: int
    rating: str
    answer_code: str


class Source:
    """What one scoring reads: the visits, their entity rows and action points, their answers (a batch
    at a time) and whether the answers of the checklist records were found."""

    visits: list[Visit]
    entities: dict[str, list[VisitEntity]]  # by visit key, in the order the answers point into
    links: dict[str, list[Any]]  # by visit key: build.ActionPointFacts
    answers_available: bool
    unreadable: frozenset[str] = frozenset()  # the derived columns NeuroDB cannot read (unreadable())

    def answers(self, visits: list[Visit]) -> dict[str, list[AnswerIn]]:
        raise NotImplementedError

    def question_texts(self) -> Iterable[tuple[str, bool | None]]:
        raise NotImplementedError


def unreadable(answers: bool, category: bool, method: bool, attachments: bool) -> frozenset[str]:
    """The derived columns NeuroDB cannot read at all, by the keys found: a rule on one is "not
    available" on every visit, never a missing value deducted (a visit without a value, where the data
    has the key, still takes the rule's missing-value deduction)."""
    out: set[str] = set()
    if not answers:
        out |= {"fmq_answered_pct", "fmq_answered_categories", "method_count", "red_flag_count"}
    if not category:
        out.add("fmq_answered_categories")
    if not method:
        out.add("method_count")
    if not attachments:
        out |= {"attachments_count", "attachment_count"}
    return frozenset(out)


class BuiltSource(Source):
    """The visits a full refresh has just built, before they are written."""

    def __init__(self, result, keys: Mapping[tuple[str, str], str | None]):
        self.result = result
        self.visits = result.visits
        built = {visit.key for visit in self.visits}
        self.entities = defaultdict(list)
        for entity in result.entities:
            self.entities[entity.visit.key].append(entity)
        self._index = {id(e): i for rows in self.entities.values() for i, e in enumerate(rows)}
        self.links = dict(result.action_point_facts)
        # the parsed answers of each visit built, turned into AnswerIn a batch at a time (answers()):
        # a copy of the 100,000 answers of a large refresh is never held at once
        self._answers: dict[str, list[Any]] = defaultdict(list)
        for answer in result.answers:
            if answer.visit_key in built:
                self._answers[answer.visit_key].append(answer)
        self.answers_available = bool(
            keys.get(("fm_questions", "answer")) or keys.get(("fm_questions", "answer_label"))
        )
        self.unreadable = unreadable(
            self.answers_available,
            bool(keys.get(("fm_questions", "category"))),
            bool(keys.get(("fm_questions", "method"))),
            bool(keys.get(("field_monitoring", "attachments"))),
        )

    def answers(self, visits: list[Visit]) -> dict[str, list[AnswerIn]]:
        return {visit.key: [self._answer_in(a) for a in self._answers.get(visit.key, ())] for visit in visits}

    def _answer_in(self, answer) -> AnswerIn:
        return AnswerIn(
            answer.document_id,
            answer.question_key,
            answer.question_text,
            answer.is_hact,
            answer.applies_to,
            self._index.get(id(answer.entity)) if answer.entity is not None else None,
            answer.partner_id,
            answer.parsed.answered,
            answer.parsed.placeholder,
            answer.parsed.answer_words,
            answer.parsed.summary_words,
            answer.parsed.rating,
            answer.parsed.answer_code,
        )

    def question_texts(self) -> Iterable[tuple[str, bool | None]]:
        return {(a.question_text, a.is_hact) for a in self.result.answers}

    def set_roles(self, roles: Mapping[tuple[str, bool | None], str]) -> None:
        for answer in self.result.answers:
            answer.role = roles.get((answer.question_text, answer.is_hact), "")


ANSWER_COLUMNS = (
    "document_id",
    "visit_id",
    "question_key",
    "question_text",
    "is_hact",
    "applies_to",
    "entity_id",
    "partner_id",
    "answered",
    "placeholder",
    "answer_words",
    "summary_words",
    "rating",
    "answer_code",
)


class StoredSource(Source):
    """The visits as stored (a scores-only refresh, or a preview), without reading any record."""

    def __init__(self, visits: list[Visit] | None = None):
        from . import fields
        from .build import ActionPointFacts
        from .models import VisitActionPoint

        self.visits = list(Visit.objects.order_by("pk")) if visits is None else list(visits)
        by_pk = {visit.pk: visit for visit in self.visits}
        self.entities = defaultdict(list)
        self._index: dict[int, int] = {}
        rows = VisitEntity.objects.filter(visit_id__in=list(by_pk)).order_by("visit_id", "datamart_id", "pk")
        for entity in rows:
            visit = by_pk[entity.visit_id]
            self._index[entity.pk] = len(self.entities[visit.key])
            self.entities[visit.key].append(entity)
        self.links = defaultdict(list)
        links = VisitActionPoint.objects.filter(visit_id__in=list(by_pk)).values_list(
            "visit_id",
            "action_point_id",
            "action_point__status",
            "action_point__due_date",
            "action_point__high_priority",
        )
        for visit_id, pk, state, due, high in links:
            self.links[by_pk[visit_id].key].append(ActionPointFacts(pk, state or "", due, bool(high)))
        self.answers_available = fields.available("fm_questions", "answer") or fields.available(
            "fm_questions", "answer_label"
        )
        self.unreadable = unreadable(
            self.answers_available,
            fields.available("fm_questions", "category"),
            fields.available("fm_questions", "method"),
            fields.available("field_monitoring", "attachments"),
        )

    def answers(self, visits: list[Visit]) -> dict[str, list[AnswerIn]]:
        keys = {visit.pk: visit.key for visit in visits}
        out: dict[str, list[AnswerIn]] = {visit.key: [] for visit in visits}
        rows = (
            QuestionAnswer.objects.filter(visit_id__in=list(keys)).order_by("pk").values_list(*ANSWER_COLUMNS)
        )
        for row in rows.iterator(chunk_size=5000):
            values = dict(zip(ANSWER_COLUMNS, row, strict=True))
            out[keys[values["visit_id"]]].append(
                AnswerIn(
                    values["document_id"],
                    values["question_key"],
                    values["question_text"],
                    values["is_hact"],
                    values["applies_to"],
                    self._index.get(values["entity_id"]) if values["entity_id"] else None,
                    values["partner_id"],
                    values["answered"],
                    values["placeholder"],
                    values["answer_words"],
                    values["summary_words"],
                    values["rating"],
                    values["answer_code"],
                )
            )
        return out

    def question_texts(self) -> Iterable[tuple[str, bool | None]]:
        return QuestionAnswer.objects.order_by().values_list("question_text", "is_hact").distinct()


# One rule's result on one record as a scoring keeps it until it is written: (record, rule, status,
# points, max points, detail key, detail, measure). A tuple, not a model instance: a refresh holds one
# per record and rule switched on (some 180,000 at 15,000 records), within its memory budget.
ResultRow = tuple[Any, str, str, Decimal, Decimal, str, str, float | None]


def result_models(model, owner: str, rows: Iterable[ResultRow]) -> Iterable:
    """The rows as ``model`` instances (``RecordRuleResult``, ``owner`` "entity"), one at a time: what
    ``refresh.copy_results`` writes where COPY is not available."""
    for target, rule, status, points, max_points, detail_key, detail, measure in rows:
        yield model(
            **{owner: target},
            rule=rule,
            status=status,
            points=points,
            max_points=max_points,
            detail_key=detail_key,
            detail=detail,
            measure=measure,
        )


@functools.lru_cache(maxsize=4096)  # a few dozen distinct deductions, for 180,000 results
def _points(top: Decimal, taken: Decimal) -> tuple[Decimal, Decimal]:
    return half_up(top - taken, 1), half_up(top, 1)


def _row(owner, result: Outcome, keep: Callable[[Any], Any]) -> ResultRow:
    """A record's result (an ``Outcome``) as a row, its repeated values shared (the points are shared
    already: ``_points``)."""
    points, top = _points(result.max_deduction, result.deduction)
    return (
        owner,
        result.rule,
        result.status,
        points,
        top,
        keep(result.detail_key[:40]),
        keep(result.detail[:600]),
        result.measure,
    )


@dataclass
class Scored:
    """What a scoring produced besides the visits and records it updated in place: the records' rule
    results, as rows (:data:`ResultRow`), and the question roles."""

    record_results: list[ResultRow] = field(default_factory=list)
    roles: dict[tuple[str, bool | None], str] = field(default_factory=dict)
    scored: int = 0
    failed: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def _batches(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class _Batch:
    """What the visits of one batch share: their records' AI checks' answers (fresh ones only: their
    inputs and prompts are those they were checked with), by record (``datamart_id``), and their
    monitors' e-mail addresses."""

    checks: dict[int, dict[str, tuple[bool, str]]] = field(default_factory=dict)
    emails: dict[int, set[str]] = field(default_factory=dict)
    # one object per distinct detail and number of the records' results (they repeat a few hundred)
    values: dict[Any, Any] = field(default_factory=dict)

    def shared(self, value: Any) -> Any:
        return self.values.setdefault(value, value)


def _batch_facts(
    batch: list[Visit],
    source: Source,
    answers: Mapping[str, list[AnswerIn]],
    roles: Mapping[tuple[str, bool | None], str],
    book: Rulebook,
    touch: bool = True,
    today: date | None = None,
) -> _Batch:
    out = _Batch()
    ai_rules = book.ai_rules()
    # the texts are read only when a check may count: the AI is on, or answers are kept from before
    if ai_rules and (book.ai_on or AICheckAnswer.objects.exists()):
        from .ai import checks

        items = [
            checks.item_of(
                visit,
                source.entities.get(visit.key, []),
                [
                    (
                        a.document_id,
                        roles.get((a.question_text, a.is_hact), ""),
                        a.applies_to,
                        a.entity,
                        a.partner_id,
                    )
                    for a in answers.get(visit.key, ())
                    if a.answered
                ],
                [link.id for link in source.links.get(visit.key, ())],
            )
            for visit in batch
            if scorable(visit.status, book.scored_statuses)
        ]
        out.checks = checks.fresh(items, ai_rules, book, touch=touch, today=today)
    if book.needs_people():
        out.emails = monitor_emails(
            e.finding_id for visit in batch for e in source.entities.get(visit.key, ())
        )
    return out


def score_visits(
    source: Source,
    book: Rulebook,
    today: date,
    on_error: Callable[[str, BaseException], None] | None = None,
    *,
    touch: bool = True,
) -> Scored:
    """Score every record of every visit of ``source`` with ``book``, then each visit from its records:
    the visits and their records are updated in place (Q1, PSEA, score, band, flags, urgency), and the
    rule results and the question roles are returned to be written. ``touch``: mark the AI check
    answers read as used today (a preview, which writes nothing, does not)."""
    out = Scored()
    roles = {
        (text, is_hact): rules.assign_roles(text, is_hact, book.patterns)
        for text, is_hact in source.question_texts()
    }
    out.roles = roles
    in_dataset = {role for role in roles.values() if role}
    if any(is_hact for _text, is_hact in roles):
        in_dataset.add("hact")
    in_dataset |= row_roles(e for rows in source.entities.values() for e in rows)
    refs = References(load=book.needs_places())
    ctx = book.context()
    roles_seen: Counter[str] = Counter()
    q1_applies: Counter[str] = Counter()
    statuses: dict[str, Counter[str]] = defaultdict(Counter)
    for batch in _batches(source.visits, BATCH):
        answers = source.answers(batch)
        shared = _batch_facts(batch, source, answers, roles, book, touch, today)
        for visit in batch:
            visit_answers = answers.get(visit.key, [])
            entities = source.entities.get(visit.key, [])
            try:
                record_results = _score_one(
                    visit,
                    entities,
                    visit_answers,
                    source.links.get(visit.key, []),
                    roles,
                    frozenset(in_dataset),
                    source,
                    book,
                    ctx,
                    refs,
                    shared,
                    today,
                )
            except Exception as exc:
                out.failed += 1
                logger.warning("fmm scoring: visit %s could not be scored: %s", visit.key, exc)
                if on_error is not None:
                    on_error(f"visit {visit.key}", exc)
                _not_scored(visit, entities)
                continue
            out.record_results += record_results
            out.scored += int(visit.quality_score is not None)
            # what each rule found, counted per record
            for row in record_results:
                statuses[row[1]][row[2]] += 1
            for answer in visit_answers:
                role = roles.get((answer.question_text, answer.is_hact), "")
                if role:
                    roles_seen[role] += 1
                if role == "q1":
                    q1_applies[answer.applies_to or "visit"] += 1
    records = [e for rows in source.entities.values() for e in rows]
    out.details = {
        "questions": {
            "roles": {role: roles_seen.get(role, 0) for role in rules.ROLES},
            "q1_applies_to": {k: q1_applies.get(k, 0) for k in ("entity", "partner", "visit")},
        },
        "rule_results": {code: dict(counts) for code, counts in statuses.items()},
        "provisional": sum(1 for v in source.visits if v.ai_pending),
        "records": len(records),
        "records_scored": sum(1 for e in records if e.quality_score is not None),
        "records_provisional": sum(1 for e in records if e.ai_pending),
    }
    return out


RECORD_SCORE_FIELDS = (
    "quality_score",
    "provisional_score",
    "ai_pending",
    "score_band",
    "category_deductions",
    "evaluated_rules",
    "not_scored_reason",
    "flags",
    "flag_count",
    "urgency",
    "urgency_band",
    "urgency_parts",
)


def _not_scored(visit: Visit, entities: Iterable[VisitEntity] = ()) -> None:
    """A visit whose scoring raised: nothing worked out for it or its records is kept (not even an
    older result)."""
    visit.hact_q1, visit.psea_flag = "", None
    visit.quality_score = visit.quality_points = visit.provisional_score = visit.lowest_score = None
    visit.quality_max, visit.evaluated_rules, visit.flags, visit.flag_count = 0, [], [], 0
    visit.records_scored = visit.records_low = 0
    visit.score_band, visit.not_scored_reason = "", NOT_SCORED_ERROR
    visit.ai_pending, visit.category_deductions = 0, {}
    visit.urgency, visit.urgency_band, visit.urgency_parts, visit.signals = None, "", {}, {}
    for entity in entities:
        entity.hact_q1 = entity.hact_q1_from = ""
        _set_record(entity, ScoreOutcome(None, None, 0, {}, (), "", (), NOT_SCORED_ERROR), (None, "", {}))


def _answered_for(answers: Sequence[AnswerFacts], role: str, index: int, partner_id: int | None) -> bool:
    """A row has an answer to ``role``: its own, its partner's, or the visit's."""
    for answer in answers:
        if answer.role != role or not answer.answered:
            continue
        if answer.applies_to == "entity" and answer.entity == index:
            return True
        if answer.applies_to == "partner" and partner_id and answer.partner_id == partner_id:
            return True
        if answer.applies_to not in ("entity", "partner"):
            return True
    return False


DERIVED_COLUMNS = ("fmq_answered_pct", "fmq_answered_categories", "method_count", "red_flag_count")


def row_values(visit: Visit, entity: VisitEntity) -> dict[str, Any]:
    """A record's derived columns (the share answered, the categories answered, the methods, the red
    flags), worked out at the build over the answers that apply to it. A record the build has not
    measured yet (stored before Release 2 step 5: no question count while its visit has one) reads its
    visit's, until the next full refresh."""
    if getattr(entity, "questions_asked", None) is None and visit.questions_asked is not None:
        return {name: getattr(visit, name) for name in DERIVED_COLUMNS}
    return {name: getattr(entity, name, None) for name in DERIVED_COLUMNS}


def _visit_values(visit: Visit) -> dict[str, Any]:
    """The columns that belong to the visit, repeated on each of its records as FMS does."""
    return {
        "action_points_count": visit.action_points,
        "field_offices": list(visit.offices or []),
        "sections_names": list(visit.section_names or []),
        "monitoring_modality": visit.modality or "",
        "programme_areas": list(visit.programme_areas or []),
    }


def _present(values: Mapping[str, Any], visit: Visit, present: dict[str, bool | None]) -> None:
    """Whether each column of ``values`` (and the visit's action points and team) is filled."""
    categories = values["fmq_answered_categories"]
    present["fmq_answered_categories"] = None if categories is None else bool(categories.strip())
    for name in (
        "field_offices",
        "sections_names",
        "location_pcode",
        "monitoring_modality",
        "programme_areas",
    ):
        present[name] = bool(values[name])
    present["action_points_count"] = present["action_points_text"] = visit.action_points > 0
    present["action_points_due_dates"] = visit.action_points > 0
    present["action_points_assigned_to"] = visit.action_points_assigned > 0
    present["team_members"] = bool(visit.team or visit.team_unnamed)
    for name in ("fmq_answered_pct", "method_count", "red_flag_count", "attachments_count"):
        present[name] = None if values[name] is None else values[name] > 0
    present["attachment_count"] = present["attachments_count"]


def record_of(
    visit: Visit,
    entity: VisitEntity,
    index: int,
    answers: Sequence[AnswerFacts],
    in_dataset: frozenset[str],
    answers_available: bool,
    scorable_: bool,
    refs: dict[str, Any] | None = None,
    checks: dict[str, tuple[bool, str]] | None = None,
    unreadable: frozenset[str] = frozenset(),
) -> rules.Record:
    """What the rules see of one record (``rules.Record``, ``kinds`` its own entity type): whether each
    field of its row is filled (its narrative, its rating, Q1, Q2 and Q3 answered for it: its own
    answer, its partner's or the visit's), its derived columns, its place (else the visit's) and the
    visit's own columns. ``index``: the row's place in the visit's records (the answers point into
    it)."""
    present: dict[str, bool | None] = {
        "narrative_finding": entity.narrative_words > 0 and not entity.narrative_placeholder,
        "overall_finding_rating": bool((entity.rating_raw or "").strip()),
        "entity": bool((entity.entity or "").strip()),
        "entity_type": bool((entity.entity_type_raw or "").strip()),
    }
    for role in ROW_ROLES:
        readable = role in in_dataset and (
            answers_available or any(a.from_row and a.role == role for a in answers)
        )
        present[f"hact_{role}_answer"] = (
            _answered_for(answers, role, index, entity.partner_id) if readable else None
        )
    attachments = getattr(entity, "attachments_count", None)
    values: dict[str, Any] = {
        **row_values(visit, entity),
        "attachments_count": attachments,
        "attachment_count": attachments,
        **_visit_values(visit),
        "location_pcode": (getattr(entity, "location_pcode", "") or "").strip() or visit.place_pcode or "",
        "output": [entity.cp_output] if (getattr(entity, "cp_output", "") or "").strip() else [],
    }
    _present(values, visit, present)
    return rules.Record(
        key=f"{visit.key}#{entity.datamart_id}",
        scorable=scorable_,
        status_group=visit.status_group,
        kinds=frozenset({entity.kind}),
        present=present,
        values=values,
        refs=refs or {},
        checks=checks or {},
        unreadable=unreadable,
    )


def record(
    visit: Visit,
    entities: Sequence[VisitEntity],
    answers: Sequence[AnswerFacts],
    in_dataset: frozenset[str],
    answers_available: bool,
    scorable_: bool,
    refs: dict[str, Any] | None = None,
    checks: dict[str, tuple[bool, str]] | None = None,
    unreadable: frozenset[str] = frozenset(),
) -> rules.Record:
    """What the rules see of a visit as a whole: only for a visit without any record (none is built
    without one); a field of the report is filled when every row that counts has it."""
    rated = [i for i, e in enumerate(entities) if e.rating in rules.RATED]
    considered = rated or list(range(len(entities)))
    present: dict[str, bool | None] = {}
    if entities:
        present["narrative_finding"] = all(
            entities[i].narrative_words > 0 and not entities[i].narrative_placeholder for i in considered
        )
        present["overall_finding_rating"] = all((e.rating_raw or "").strip() for e in entities)
        present["entity"] = all((e.entity or "").strip() for e in entities)
        present["entity_type"] = all((e.entity_type_raw or "").strip() for e in entities)
    for role in ROW_ROLES:
        column = f"hact_{role}_answer"
        readable = role in in_dataset and (
            answers_available or any(a.from_row and a.role == role for a in answers)
        )
        if not entities or not readable:
            present[column] = None
            continue
        present[column] = all(_answered_for(answers, role, i, entities[i].partner_id) for i in considered)
    values: dict[str, Any] = {
        **{name: getattr(visit, name) for name in DERIVED_COLUMNS},
        "attachments_count": visit.attachments_count,
        "attachment_count": visit.attachments_count,
        **_visit_values(visit),
        "location_pcode": visit.place_pcode or "",
        "output": list(visit.cp_outputs or []),
    }
    _present(values, visit, present)
    return rules.Record(
        key=visit.key,
        scorable=scorable_,
        status_group=visit.status_group,
        kinds=frozenset(e.kind for e in entities),
        present=present,
        values=values,
        refs=refs or {},
        checks=checks or {},
        unreadable=unreadable,
    )


def visit_level(rule: RuleSetting) -> bool:
    """A rule that reads only what belongs to the visit (its monitors and field offices): evaluated once
    per visit, its outcome copied to every record (R19)."""
    if rule.type != "reference_check":
        return False
    check = rules.param(rule, "check_type", "")
    if check in VISIT_CHECKS:
        return True
    return check == "value_in_list" and rules.param(rule, "field", "") in rules.PERSON_COLUMNS


def _set_record(entity: VisitEntity, outcome: ScoreOutcome, urgent: tuple[int | None, str, dict]) -> None:
    entity.quality_score, entity.provisional_score = outcome.score, outcome.provisional
    entity.ai_pending = min(outcome.pending, 32767)
    entity.category_deductions = outcome.deductions
    entity.evaluated_rules = list(outcome.evaluated)
    entity.not_scored_reason, entity.score_band = outcome.not_scored_reason, outcome.band
    entity.flags, entity.flag_count = list(outcome.flags), len(outcome.flags)
    entity.urgency, entity.urgency_band, entity.urgency_parts = urgent


def _score_one(
    visit: Visit,
    entities: list[VisitEntity],
    answers: list[AnswerIn],
    links: list[Any],
    roles: Mapping[tuple[str, bool | None], str],
    in_dataset: frozenset[str],
    source: Source,
    book: Rulebook,
    ctx: rules.Context,
    refs: References,
    shared: _Batch,
    today: date,
) -> list[ResultRow]:
    """Score each record of one visit, then the visit from its records; its records' rule results, as
    rows."""
    facts_answers = [
        AnswerFacts(
            question_key=a.question_key,
            role=roles.get((a.question_text, a.is_hact), ""),
            answered=a.answered,
            placeholder=a.placeholder,
            words=a.answer_words + a.summary_words,
            rating=a.rating,
            answer_code=a.answer_code,
            is_hact=a.is_hact,
            applies_to=a.applies_to or "visit",
            entity=a.entity,
            partner_id=a.partner_id,
        )
        for a in answers
    ]
    facts_answers = with_row_answers(entities, facts_answers)
    q1 = effective_q1(entities, facts_answers)
    for entity, (entity_q1, q1_from) in zip(entities, q1, strict=True):
        entity.hact_q1, entity.hact_q1_from = entity_q1, q1_from
    visit.hact_q1 = visit_q1([q for q, _ in q1], facts_answers)
    # without the answers' key every answer reads as blank: the flag is not known, not "not flagged"
    visit.psea_flag = psea_flag(facts_answers, book.flag_codes) if source.answers_available else None
    is_scorable = scorable(visit.status, book.scored_statuses)
    people: dict[str, Any] | None = None
    if book.needs_people():
        emails = (
            set().union(*(shared.emails.get(e.finding_id, set()) for e in entities)) if entities else set()
        )
        people = {"team_members": emails}  # the visit's monitors: the same for every record
    if not entities:
        return _score_visit_alone(
            visit, facts_answers, in_dataset, source, book, ctx, refs, people, today, links
        )
    row_rules, visit_rules = book.split_rules()
    # the visit's rules, evaluated once per visit and kind of record: the same outcome on every record,
    # but a rule limited to some entity types (its entity type filter) still skips the other records
    copied: dict[str, list[Outcome]] = {}
    scored: list[tuple[VisitEntity, list[Outcome]]] = []
    for index, entity in enumerate(entities):
        references = refs.facts(visit, entity) if book.needs_places() else {}
        if people is not None:
            references["people"] = people
        facts = record_of(
            visit,
            entity,
            index,
            facts_answers,
            in_dataset,
            source.answers_available,
            is_scorable,
            references,
            shared.checks.get(entity.datamart_id, {}),
            source.unreadable,
        )
        outcomes = rules.evaluate(facts, row_rules, ctx)
        if visit_rules:
            if entity.kind not in copied:  # on what every record of the visit shares
                copied[entity.kind] = rules.evaluate(facts, visit_rules, ctx)
            outcomes = sorted(outcomes + copied[entity.kind], key=lambda o: rules.code_order(o.rule))
        outcome = score_outcome(outcomes, book.setting, is_scorable, visit.status_group)
        _set_record(entity, outcome, urgency(visit, outcome, book.setting, today))
        scored.append((entity, outcomes))
    aggregate_visit(visit, entities, book.setting)
    visit.signals = signals(visit, links, book.setting, today)
    keep = shared.shared
    return [_row(entity, o, keep) for entity, outcomes in scored for o in outcomes]


def _score_visit_alone(
    visit: Visit,
    facts_answers: list[AnswerFacts],
    in_dataset: frozenset[str],
    source: Source,
    book: Rulebook,
    ctx: rules.Context,
    refs: References,
    people: dict[str, Any] | None,
    today: date,
    links: list[Any],
) -> list[ResultRow]:
    """A visit without any record (the build never makes one; kept for safety): scored on its own
    fields, as a visit was before its records were. It has no record to keep rule results on: none is
    returned."""
    is_scorable = scorable(visit.status, book.scored_statuses)
    references = refs.facts(visit) if book.needs_places() else {}
    if people is not None:
        references["people"] = people
    facts = record(
        visit,
        [],
        facts_answers,
        in_dataset,
        source.answers_available,
        is_scorable,
        references,
        {},
        source.unreadable,
    )
    outcomes = rules.evaluate(facts, book.rules, ctx)
    outcome = score_outcome(outcomes, book.setting, is_scorable, visit.status_group)
    visit.quality_score, visit.provisional_score = outcome.score, outcome.provisional
    visit.quality_points = outcome.score
    visit.quality_max = 100 if outcome.score is not None else 0
    visit.lowest_score = outcome.score
    visit.records_scored = visit.records_low = 0
    visit.evaluated_rules = list(outcome.evaluated)
    visit.ai_pending, visit.category_deductions = min(outcome.pending, 32767), outcome.deductions
    visit.not_scored_reason, visit.score_band = outcome.not_scored_reason, outcome.band
    visit.flags, visit.flag_count = list(outcome.flags), len(outcome.flags)
    visit.urgency, visit.urgency_band, visit.urgency_parts = urgency(visit, outcome, book.setting, today)
    visit.signals = signals(visit, links, book.setting, today)
    return []


def aggregate_visit(visit: Visit, records: Sequence[VisitEntity], s: ScoreSetting) -> None:
    """A visit's figures from its records' (see the module's description): its quality is the mean of
    its records' scores (rounded half up), with its lowest one, the number scored and below the Medium
    band; its band is the mean's; its flags (and the rules evaluated) the union of its records', in code
    order; its urgency its most urgent record's; its pending AI checks the sum of its records'. A visit
    with a provisional record is provisional: no score and no urgency, the mean of its records' scores so
    far in ``provisional_score``. Its status decides alone whether it is scored ("pending", "cancelled")."""
    records = list(records)
    scored = [r for r in records if r.quality_score is not None]
    pending = sum(r.ai_pending or 0 for r in records)
    flags = {code for r in records for code in r.flags or ()}
    visit.flags = sorted(flags, key=rules.code_order)
    visit.flag_count = len(visit.flags)
    evaluated = {code for r in records for code in r.evaluated_rules or ()}
    visit.evaluated_rules = sorted(evaluated, key=rules.code_order)
    visit.ai_pending = min(pending, 32767)
    visit.records_scored = min(len(scored), 32767)
    visit.records_low = min(sum(1 for r in scored if r.quality_score < s.band_medium), 32767)
    visit.lowest_score = min((r.quality_score for r in scored), default=None)
    counted = [r for r in records if r.quality_score is not None or r.provisional_score is not None]
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for r in counted:
        for category, value in (r.category_deductions or {}).items():
            totals[category] += Decimal(str(value))
    visit.category_deductions = {
        category: float(half_up(total / len(counted), 1)) for category, total in totals.items() if total
    }
    visit.urgency, visit.urgency_band, visit.urgency_parts = None, "", {}
    if pending:
        visit.quality_score = None
        visit.provisional_score = average_quality(
            r.quality_score if r.quality_score is not None else r.provisional_score for r in records
        )
        visit.score_band, visit.not_scored_reason = "", PROVISIONAL.format(n=pending)
    elif scored:
        visit.quality_score = average_quality(r.quality_score for r in scored)
        visit.provisional_score = None
        visit.score_band, visit.not_scored_reason = band_of(visit.quality_score, s), ""
        urgent = [r for r in scored if r.urgency is not None]
        if urgent:
            most = max(urgent, key=lambda r: r.urgency)
            visit.urgency, visit.urgency_band, visit.urgency_parts = (
                most.urgency,
                most.urgency_band,
                dict(most.urgency_parts or {}),
            )
    else:  # not scored: its status is not, it was cancelled, or a record could not be
        visit.quality_score = visit.provisional_score = None
        first = records[0] if records else None
        visit.score_band = first.score_band if first else ""
        visit.not_scored_reason = first.not_scored_reason if first else ""
    visit.quality_points = visit.quality_score
    visit.quality_max = 100 if visit.quality_score is not None else 0


def average_quality(scores: Iterable[Decimal | float | None]) -> Decimal | None:
    """The mean of the scores given (the unscored left out), rounded half up to one decimal."""
    values = [Decimal(str(s)) for s in scores if s is not None]
    if not values:
        return None
    return half_up(sum(values, Decimal(0)) / len(values), 1)
