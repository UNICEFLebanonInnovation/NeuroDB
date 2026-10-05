"""Scoring the visits (step 6 of the refresh): the question roles, HACT Q1 and the PSEA flag, the quality
rules, the score with its band and flags, and urgency.

**Question roles.** Each checklist question gets a role (Q1, Q2, Q3, PSEA or none) from its text and
the patterns of the score settings (``rules.assign_roles``). Everything that depends on a role is
worked out here, not when the visits are built, so a pattern change needs only a scores-only refresh:

- the **HACT Q1 of an entity**: its own Q1 answer, else the one given for its partner (on a row of its
  own or for the partner as a whole), else the one given for the whole visit (:func:`effective_q1`);
- the **HACT Q1 of a visit**: the worst of its entities' and of its visit-level Q1 answers;
- the **PSEA flag**: an answer to the PSEA question whose code the settings list ("yes", or a
  Constrained or Off track rating) flags the visit; ``None`` when no PSEA question was asked, or when
  the answers of the checklist records were not found (Fields found).

**Score** (:func:`score_visit`): the points earned over the points of the rules evaluated (those that
passed or failed), as a percentage rounded half up to one decimal. A rule never gives more than its
points. A visit that is not reported yet (or cancelled) gets no score, and neither does one with fewer
evaluated points than the settings' minimum (30). Bands: High from 80, Medium from 50, else Low. The
flags are the rules failed, R6 included even at 0 points.

**Urgency** (:func:`urgency`), 0 to 100: the worse of the visit's rating and its HACT Q1 (Off track 40,
Constrained 20), the quality gap (or 10 for a reported visit that could not be scored), 5 per flag (at
most 15), follow-up (20 when an Off track or Constrained reported visit has no action point after 14
days; else 12 for an overdue action point, 8 more when it is high priority, 5 for an open high
priority one, at most 20) and 15 for a report still not in 30 days after the visit. Red from 70, amber
from 40. Each part is kept (``urgency_parts``) to explain it.

The step reads the narratives from the findings in batches of 1,000 visits (``narrative_finding``
only, never the records), twice: first to find the narratives copied between visits (R4; only their
hashes are kept), then to apply the rules. The texts stay in memory while a batch is scored and are
never stored. Q3 answers are read back from their records (through ``parse``) for R5's placeholder
list. A full refresh scores the visits it has just built (:class:`BuiltSource`); a scores-only one the
visits stored (:class:`StoredSource`), which is also what the admin's *Preview effect* scores in
memory without writing anything.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError

from neurodb.datamart.fm import RATING_ORDER

from . import parse, rules
from .models import (
    QuestionAnswer,
    RuleSetting,
    ScoreSetting,
    Visit,
    VisitEntity,
    VisitRuleResult,
    default_question_patterns,
    default_role_flag_answers,
    default_urgency_weights,
)
from .rules import AnswerFacts, Context, EntityFacts, RuleOutcome, VisitFacts, half_up, q1_value, worst

logger = logging.getLogger(__name__)

NARRATIVE_BATCH = 1000  # visits whose narratives are read at once
ANSWER_CODES = ("on_track", "constrained", "off_track", "yes", "no")
MAX_PATTERNS = 20
NOT_REPORTED = "not reported yet"
CANCELLED = "cancelled"
NOT_SCORED_ERROR = "could not be scored"


# ------------------------------------------------------------------------------------------ settings
@dataclass
class Rulebook:
    """The quality rules and score settings one scoring reads, loaded once."""

    rules: dict[str, RuleSetting]
    setting: ScoreSetting

    @classmethod
    def load(cls) -> Rulebook:
        """The rules as administrators set them (a rule missing from the table is written back with its
        defaults) and the score settings."""
        found = {rule.code: rule for rule in RuleSetting.objects.all()}
        for code in rules.CODES:
            if code not in found:
                default = rules.DEFAULTS[code]
                found[code] = RuleSetting.objects.get_or_create(
                    code=code,
                    defaults={
                        "label": default["label"],
                        "points": default["points"],
                        "threshold": default["threshold"],
                        "params": copy.deepcopy(default["params"]),
                        "description": default["description"],
                    },
                )[0]
        return cls({code: found[code] for code in rules.CODES}, ScoreSetting.load())

    def total_points(self) -> int:
        """The points of the rules switched on (the reference's 85)."""
        return sum(rule.points for rule in self.rules.values() if rule.enabled)

    def with_changes(self, rule_changes: Mapping[str, Mapping], score_changes: Mapping) -> Rulebook:
        """A copy with some settings changed (for a preview); nothing is saved."""
        changed = {code: copy.copy(rule) for code, rule in self.rules.items()}
        for code, values in (rule_changes or {}).items():
            for name, value in values.items():
                setattr(changed[code], name, value)
        setting = copy.copy(self.setting)
        for name, value in (score_changes or {}).items():
            setattr(setting, name, value)
        return Rulebook(changed, setting)

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
    def weights(self) -> dict[str, int]:
        weights = self.setting.urgency_weights
        return {**default_urgency_weights(), **(weights if isinstance(weights, dict) else {})}

    def narrative_minimum(self) -> int:
        return round(rules.threshold(self.rules["R4"]))

    def narrative_placeholders(self) -> frozenset[str]:
        return frozenset(rules.param(self.rules["R4"], "placeholders") or ())

    def q3_placeholders(self) -> frozenset[str]:
        return frozenset(rules.param(self.rules["R5"], "placeholders") or ())


def validate_settings(setting: ScoreSetting) -> None:
    """The score settings checked (``ScoreSetting.clean``); the question patterns are stored folded."""
    errors: dict[str, list[str]] = {}

    def error(name: str, message: str) -> None:
        errors.setdefault(name, []).append(message)

    if not setting.urgency_amber < setting.urgency_red <= 100:
        error("urgency_red", "Amber must be below red, and red at most 100.")
    if not setting.band_medium < setting.band_high <= 100:
        error("band_high", "The Medium band must start below the High band, and High at most 100.")
    if not 0 <= setting.min_evaluated_points <= 100:
        error("min_evaluated_points", "The fewest evaluated points go from 0 to 100.")
    weights = setting.urgency_weights
    known = default_urgency_weights()
    if not isinstance(weights, dict):
        error("urgency_weights", 'The weights must be an object, e.g. {"off_track": 40, ...}.')
    else:
        unknown = sorted(set(weights) - set(known))
        missing = sorted(set(known) - set(weights))
        if unknown:
            error("urgency_weights", f"Unknown weights: {', '.join(unknown)}.")
        if missing:
            error("urgency_weights", f"Missing weights: {', '.join(missing)}.")
        bad = [
            k
            for k, v in weights.items()
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100
        ]
        if bad:
            error(
                "urgency_weights",
                f"Each weight is a whole number from 0 to 100 (not: {', '.join(sorted(bad))}).",
            )
    patterns = setting.question_patterns
    if not isinstance(patterns, dict):
        error("question_patterns", 'The patterns must be an object, e.g. {"q1": ["implemented as planned"]}.')
    else:
        cleaned: dict[str, list[str]] = {}
        for role, values in patterns.items():
            if role not in rules.ROLES:
                error("question_patterns", f"Unknown role “{role}”: the roles are {', '.join(rules.ROLES)}.")
                continue
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                error("question_patterns", f"The patterns of {role} must be a list of texts.")
                continue
            if len(values) > MAX_PATTERNS:
                error("question_patterns", f"{role} has {len(values)} patterns; at most {MAX_PATTERNS}.")
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
                    continue
                if prefix + body not in out:
                    out.append(prefix + body)
            cleaned[role] = out
        if "question_patterns" not in errors:
            setting.question_patterns = cleaned
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


# ------------------------------------------------------------------------------------------ Q1 and PSEA
def scorable(status_group: str, entities_rated: int, answered: bool) -> bool:
    """A reported visit, or one of unknown status with a rated entity or an answered question."""
    if status_group == "reported":
        return True
    return status_group == "unknown" and (entities_rated > 0 or answered)


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
    points: Decimal | None
    max_points: int
    evaluated: tuple[str, ...]
    band: str
    flags: tuple[str, ...]
    not_scored_reason: str


def score_visit(
    facts: VisitFacts, outcomes: list[RuleOutcome], s: ScoreSetting, total: int | None = None
) -> ScoreOutcome:
    """The visit's score from its rule outcomes (see the module's description). ``total`` is the points
    of the rules switched on, for the reason given when too few could be evaluated."""
    evaluated = [o for o in outcomes if o.status in ("pass", "fail") and o.max_points > 0]
    max_points = sum(o.max_points for o in evaluated)
    flags = tuple(o.rule for o in outcomes if o.status == "fail")
    codes = tuple(o.rule for o in evaluated)
    if not facts.scorable:
        reason = CANCELLED if facts.status_group == "cancelled" else NOT_REPORTED
        return ScoreOutcome(None, None, 0, (), "", (), reason)
    if total is None:
        total = sum(o.max_points for o in outcomes if o.status != "off")
    if max_points < s.min_evaluated_points or max_points == 0:
        reason = f"too few rules ({max_points} of {total} points)"
        return ScoreOutcome(None, None, max_points, codes, "", flags, reason)
    earned = sum((min(Decimal(str(o.points)), Decimal(o.max_points)) for o in evaluated), Decimal(0))
    score = half_up(Decimal(100) * earned / Decimal(max_points), 1)
    band = "high" if score >= s.band_high else "medium" if score >= s.band_medium else "low"
    return ScoreOutcome(score, half_up(earned, 1), max_points, codes, band, flags, "")


def high_flag(flag_count: int, s: ScoreSetting) -> bool:
    """A visit with many flags: at least the settings' ``high_flag_count`` (3) rules failed."""
    return flag_count >= s.high_flag_count


def urgency(
    visit: Visit, score: ScoreOutcome, links: Sequence[Any], s: ScoreSetting, today: date
) -> tuple[int, str, dict[str, int]]:
    """The visit's urgency, 0-100, its band (red, amber or "") and its parts. ``links`` are the visit's
    action points (``build.ActionPointFacts``)."""
    from neurodb.datamart.models import ActionPoint

    w = {**default_urgency_weights(), **(s.urgency_weights if isinstance(s.urgency_weights, dict) else {})}
    worse = max([visit.rating, visit.hact_q1 or "not_monitored"], key=lambda v: RATING_ORDER.get(v, 0))
    rating = w["off_track"] if worse == "off_track" else w["constrained"] if worse == "constrained" else 0
    if score.score is not None:
        quality = int(half_up(Decimal(w["quality_gap"]) * (Decimal(100) - score.score) / Decimal(100), 0))
    elif visit.status_group == "reported":
        quality = w["unscored_reported"]
    else:
        quality = 0
    flags = min(w["flags_max"], w["per_flag"] * len(score.flags))
    open_ = [a for a in links if a.status in ActionPoint.OPEN_STATUSES]
    overdue = [a for a in open_ if a.due_date and a.due_date < today]
    if (
        worse in ("off_track", "constrained")
        and visit.status_group == "reported"
        and not links
        and visit.end_date
        and (today - visit.end_date).days > s.follow_up_days
    ):
        follow = w["no_follow_up"]
    else:
        follow = (
            (w["ap_overdue"] if overdue else 0)
            + (w["ap_high_overdue"] if any(a.high_priority for a in overdue) else 0)
            + (w["ap_high_open"] if not overdue and any(a.high_priority for a in open_) else 0)
        )
    follow = min(w["follow_up_max"], follow)
    late = (
        w["report_late"]
        if visit.status_group in ("planned", "in_progress")
        and visit.end_date
        and visit.end_date < today - timedelta(days=s.report_late_days)
        else 0
    )
    total = min(100, rating + quality + flags + follow + late)
    band = "red" if total >= s.urgency_red else "amber" if total >= s.urgency_amber else ""
    return (
        total,
        band,
        {"rating": rating, "quality": quality, "flags": flags, "follow_up": follow, "report_late": late},
    )


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
    at a time) and what the checklist records hold as a whole."""

    visits: list[Visit]
    entities: dict[str, list[VisitEntity]]  # by visit key, in the order the answers point into
    links: dict[str, list[Any]]  # by visit key: build.ActionPointFacts
    answers_available: bool
    unanswered_seen: bool
    question_records: int
    answer_keys: Mapping[str, str | None]  # the answer, label and summary keys (R5 reads Q3 answers)

    def answers(self, visits: list[Visit]) -> dict[str, list[AnswerIn]]:
        raise NotImplementedError

    def question_texts(self) -> Iterable[tuple[str, bool | None]]:
        raise NotImplementedError

    def copy_scope(self) -> list[tuple[Visit, list[VisitEntity]]]:
        """The visits whose narratives may be copies of the scored ones' (all of them, by default)."""
        return [(visit, self.entities.get(visit.key, [])) for visit in self.visits]


class BuiltSource(Source):
    """The visits a full refresh has just built, before they are written."""

    def __init__(self, result, keys: Mapping[tuple[str, str], str | None], questions: Mapping[str, Any]):
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
        self.unanswered_seen = bool(questions.get("unanswered_seen"))
        self.question_records = int(questions.get("records") or 0)
        self.answer_keys = {f: keys.get(("fm_questions", f)) for f in ("answer", "answer_label", "summary")}

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

    def __init__(self, visits: list[Visit] | None = None, copy_visits: Iterable[Visit] | None = None):
        from . import fields, status
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
        self._copy_visits = list(copy_visits) if copy_visits is not None else None
        self.answers_available = fields.available("fm_questions", "answer") or fields.available(
            "fm_questions", "answer_label"
        )
        probe = status.last_probe()
        questions = ((probe.details or {}) if probe else {}).get("questions") or {}
        if "unanswered_seen" in questions:
            self.unanswered_seen = bool(questions.get("unanswered_seen"))
            self.question_records = int(questions.get("records") or 0)
        else:  # no reading of the keys recorded: what the stored answers say
            self.unanswered_seen = QuestionAnswer.objects.filter(answered=False).exists()
            self.question_records = QuestionAnswer.objects.count()
        self.answer_keys = {
            f: fields.key_for("fm_questions", f) for f in ("answer", "answer_label", "summary")
        }

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

    def copy_scope(self) -> list[tuple[Visit, list[VisitEntity]]]:
        if self._copy_visits is None:
            return super().copy_scope()
        scored = {visit.pk for visit in self.visits}
        extra = [v for v in self._copy_visits if v.pk not in scored]
        rows: dict[int, list[VisitEntity]] = defaultdict(list)
        for entity in VisitEntity.objects.filter(visit_id__in=[v.pk for v in extra]).only(
            "pk", "visit_id", "finding_id"
        ):
            rows[entity.visit_id].append(entity)
        return super().copy_scope() + [(visit, rows.get(visit.pk, [])) for visit in extra]


@dataclass
class Scored:
    """What a scoring produced besides the visits and entity rows it updated in place."""

    results: list[VisitRuleResult] = field(default_factory=list)
    roles: dict[tuple[str, bool | None], str] = field(default_factory=dict)
    scored: int = 0
    failed: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def narratives(finding_ids: Iterable[int | None]) -> dict[int, str]:
    """{finding pk: narrative} of the findings given (the narrative column only)."""
    from neurodb.datamart.models import MonitoringFinding

    ids = [pk for pk in set(finding_ids) if pk]
    if not ids:
        return {}
    rows = MonitoringFinding.objects.filter(pk__in=ids).values_list("pk", "narrative_finding")
    return {pk: text or "" for pk, text in rows}


def narrative_measures(text: str, minimum: int, placeholders: frozenset[str]) -> tuple[int, bool, str]:
    """(words, placeholder, hash) of a narrative under R4's settings: the hash (sha1 of the folded text)
    only for a narrative of ``minimum`` words or more that is not a placeholder."""
    if not text or not text.strip():
        return 0, False, ""
    folded = parse.fold(text)
    words = parse.word_count(text)
    placeholder = folded in placeholders
    digest = ""
    if words >= minimum and not placeholder:
        digest = hashlib.sha1(folded.encode(), usedforsecurity=False).hexdigest()
    return words, placeholder, digest


def _batches(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def score_visits(
    source: Source,
    book: Rulebook,
    today: date,
    on_error: Callable[[str, BaseException], None] | None = None,
) -> Scored:
    """Score every visit of ``source`` with ``book``: the visits and their entity rows are updated in
    place (Q1, PSEA, score, band, flags, urgency; the narrative measures under R4's settings), and the
    rule results and the question roles are returned to be written."""
    out = Scored()
    roles = {
        (text, is_hact): rules.assign_roles(text, is_hact, book.patterns)
        for text, is_hact in source.question_texts()
    }
    out.roles = roles
    in_dataset = {role for role in roles.values() if role}
    if any(is_hact for _text, is_hact in roles):
        in_dataset.add("hact")
    minimum = book.narrative_minimum()
    placeholders = book.narrative_placeholders()

    # 1. the narratives copied between visits (their hashes only)
    copies: dict[str, list[tuple[str, date | None]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for batch in _batches(source.copy_scope(), NARRATIVE_BATCH):
        texts = narratives(e.finding_id for _visit, rows in batch for e in rows)
        for visit, rows in batch:
            labels[visit.key] = visit.label
            for digest in {
                narrative_measures(texts.get(e.finding_id, ""), minimum, placeholders)[2] for e in rows
            }:
                if digest:
                    copies[digest].append((visit.key, visit.end_date))
    ctx = Context(today=today, copies=copies, labels=labels, narrative_min_words=minimum)

    # 2. the rules, the score and urgency, a batch of visits at a time
    roles_seen: Counter[str] = Counter()
    q1_applies: Counter[str] = Counter()
    statuses: dict[str, Counter[str]] = {code: Counter() for code in rules.CODES}
    total = book.total_points()
    for batch in _batches(source.visits, NARRATIVE_BATCH):
        answers = source.answers(batch)
        texts = narratives(e.finding_id for visit in batch for e in source.entities.get(visit.key, ()))
        q3_ids = [
            a.document_id
            for visit in batch
            for a in answers.get(visit.key, ())
            if roles.get((a.question_text, a.is_hact)) == "q3"
        ]
        q3_texts = parse.answer_texts(q3_ids, source.answer_keys) if q3_ids else {}
        for visit in batch:
            visit_answers = answers.get(visit.key, [])
            try:
                results = _score_one(
                    visit,
                    source.entities.get(visit.key, []),
                    visit_answers,
                    source.links.get(visit.key, []),
                    texts,
                    q3_texts,
                    roles,
                    frozenset(in_dataset),
                    source,
                    book,
                    ctx,
                    total,
                )
            except Exception as exc:
                out.failed += 1
                logger.warning("fmm scoring: visit %s could not be scored: %s", visit.key, exc)
                if on_error is not None:
                    on_error(f"visit {visit.key}", exc)
                _not_scored(visit)
                continue
            out.results += results
            out.scored += int(visit.quality_score is not None)
            for result in results:
                statuses[result.rule][result.status] += 1
            for answer in visit_answers:
                role = roles.get((answer.question_text, answer.is_hact), "")
                if role:
                    roles_seen[role] += 1
                if role == "q1":
                    q1_applies[answer.applies_to or "visit"] += 1
    out.details = {
        "questions": {
            "roles": {role: roles_seen.get(role, 0) for role in rules.ROLES},
            "q1_applies_to": {k: q1_applies.get(k, 0) for k in ("entity", "partner", "visit")},
        },
        "rule_results": {code: dict(counts) for code, counts in statuses.items()},
    }
    return out


def _not_scored(visit: Visit) -> None:
    """A visit whose scoring raised: nothing worked out for it is kept (not even an older result)."""
    visit.hact_q1, visit.psea_flag = "", None
    visit.quality_score = visit.quality_points = None
    visit.quality_max, visit.evaluated_rules, visit.flags, visit.flag_count = 0, [], [], 0
    visit.score_band, visit.not_scored_reason = "", NOT_SCORED_ERROR
    visit.urgency, visit.urgency_band, visit.urgency_parts = 0, "", {}


def _score_one(
    visit: Visit,
    entities: list[VisitEntity],
    answers: list[AnswerIn],
    links: list[Any],
    texts: Mapping[int, str],
    q3_texts: Mapping[int, str],
    roles: Mapping[tuple[str, bool | None], str],
    in_dataset: frozenset[str],
    source: Source,
    book: Rulebook,
    ctx: Context,
    total: int,
) -> list[VisitRuleResult]:
    q3_placeholders = book.q3_placeholders()
    facts_answers = []
    for a in answers:
        role = roles.get((a.question_text, a.is_hact), "")
        placeholder = a.placeholder
        if role == "q3" and not placeholder and (text := q3_texts.get(a.document_id)):
            placeholder = parse.fold(text) in q3_placeholders
        facts_answers.append(
            AnswerFacts(
                question_key=a.question_key,
                role=role,
                answered=a.answered,
                placeholder=placeholder,
                words=a.answer_words + a.summary_words,
                rating=a.rating,
                answer_code=a.answer_code,
                is_hact=a.is_hact,
                applies_to=a.applies_to or "visit",
                entity=a.entity,
                partner_id=a.partner_id,
            )
        )
    q1 = effective_q1(entities, facts_answers)
    minimum = book.narrative_minimum()
    placeholders = book.narrative_placeholders()
    facts_entities = []
    for entity, (entity_q1, q1_from) in zip(entities, q1, strict=True):
        text = texts.get(entity.finding_id, "") if entity.finding_id else ""
        words, placeholder, digest = narrative_measures(text, minimum, placeholders)
        entity.hact_q1, entity.hact_q1_from = entity_q1, q1_from
        entity.narrative_placeholder, entity.narrative_hash = placeholder, digest
        facts_entities.append(
            EntityFacts(
                kind=entity.kind,
                rating=entity.rating,
                rating_raw=entity.rating_raw,
                partner_id=entity.partner_id,
                hact_q1=entity_q1,
                narrative=text,
                words=words,
                placeholder=placeholder,
                narrative_hash=digest,
            )
        )
    rated = sum(1 for e in entities if e.rating in rules.RATED)
    answered = any(a.answered for a in facts_answers)
    facts = VisitFacts(
        key=visit.key,
        status_group=visit.status_group,
        scorable=scorable(visit.status_group, rated, answered),
        is_programmatic=visit.is_programmatic,
        end_date=visit.end_date,
        has_place=bool(visit.location_id or visit.site_id),
        entities=tuple(facts_entities),
        answers=tuple(facts_answers),
        questions_in_dataset=in_dataset,
        answers_available=source.answers_available,
        unanswered_seen=source.unanswered_seen,
        question_records=source.question_records,
    )
    visit.hact_q1 = visit_q1([q for q, _ in q1], facts_answers)
    # without the answers' key every answer reads as blank: the flag is not known, not "not flagged"
    visit.psea_flag = psea_flag(facts_answers, book.flag_codes) if source.answers_available else None
    outcomes = rules.evaluate(facts, book.rules, ctx)
    outcome = score_visit(facts, outcomes, book.setting, total)
    visit.quality_score, visit.quality_points = outcome.score, outcome.points
    visit.quality_max, visit.evaluated_rules = min(outcome.max_points, 32767), list(outcome.evaluated)
    visit.not_scored_reason, visit.score_band = outcome.not_scored_reason, outcome.band
    visit.flags, visit.flag_count = list(outcome.flags), len(outcome.flags)
    visit.urgency, visit.urgency_band, visit.urgency_parts = urgency(
        visit, outcome, links, book.setting, ctx.today
    )
    return [
        VisitRuleResult(
            visit=visit,
            rule=o.rule,
            status=o.status,
            points=half_up(o.points, 1),
            max_points=o.max_points,
            detail_key=o.detail_key[:40],
            detail=o.detail[:300],
            measure=o.measure,
        )
        for o in outcomes
    ]


def average_quality(scores: Iterable[Decimal | float | None]) -> Decimal | None:
    """The mean of the scores given (the unscored left out), rounded half up to one decimal."""
    values = [Decimal(str(s)) for s in scores if s is not None]
    if not values:
        return None
    return half_up(sum(values, Decimal(0)) / len(values), 1)
