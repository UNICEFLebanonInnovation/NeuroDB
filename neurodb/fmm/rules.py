"""The quality rules R1-R6 of a monitoring visit's report, each a pure function of the visit's facts.

Each rule (``RULES``) reads a :class:`VisitFacts` (what the visit holds: its entities with their
ratings and narratives, its checklist answers with the role of their question, its place) and the
rule's own settings (``RuleSetting``: on or off, points, threshold, parameters), and returns a
:class:`RuleOutcome`:

- ``pass``: no flag, full points;
- ``fail``: one flag; the points may be partial (R1, R2, R4, R5);
- ``na``: the inputs are not in the data; left out of the score and shown as "not available";
- ``nap``: the rule does not apply to this visit ("n/a");
- ``off``: the rule is switched off.

Only the visits whose eTools status is one of the scored statuses (Score settings: report
finalization and completed by default) are scored (``score.scorable``); for the others ("pending",
or cancelled) every rule is ``nap``. The points earned are never above the
rule's maximum. A detail sentence is written by the code, never copied from the data: a cue named by
R6 is a word of the rule's own list.

Texts are compared folded (:func:`neurodb.fmm.parse.fold`): lower case, no accents, every character
other than a letter, a digit or a space made a space. The question roles (Q1, Q2, Q3, PSEA) are
given by :func:`assign_roles` from the patterns of the score settings.

The default settings (:data:`DEFAULTS`) are NeuroDB's proposal, apart from the points and R2's 80%,
which follow the reference dashboard; administrators change them in the admin, and every change is
versioned (``fmm.versions``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.exceptions import ValidationError

from . import parse

CODES = ("R1", "R2", "R3", "R4", "R5", "R6")
ROLES = ("q1", "q2", "q3", "psea")
RATED = ("on_track", "constrained", "off_track")
STATES = ("pass", "fail", "na", "nap", "off")
R1_ELEMENTS = ("narrative", "q2", "rating", "location")
R1_LABELS = {
    "narrative": "General observation (narrative)",
    "q2": "Q2 – Activities monitored",
    "rating": "a rating for every entity",
    "location": "the place of the visit",
}
RATING_LABELS = {
    "on_track": "On track",
    "constrained": "Constrained",
    "off_track": "Off track",
    "not_monitored": "Not monitored",
    "other": "not a rating",
}
R2_MIN_RECORDS = 200  # fewer checklist records than this say nothing about whether eTools sends blanks
MAX_LIST = 60  # entries of a list setting
MAX_ENTRY = 80  # characters of one entry

PLACEHOLDERS = [
    "n a",
    "na",
    "none",
    "nil",
    "no comment",
    "no comments",
    "same as above",
    "see above",
    "test",
    "tbd",
    "not applicable",
]
NEGATIVE_CUES = [
    "not implemented",
    "not started",
    "delayed",
    "delay",
    "suspended",
    "stopped",
    "halted",
    "did not take place",
    "not conducted",
    "postponed",
    "cancelled",
    "canceled",
    "shortage",
    "not delivered",
    "behind schedule",
    "low attendance",
    "no activities",
    "closed",
    "unsafe",
    "not functional",
]
POSITIVE_CUES = [
    "implemented as planned",
    "as planned",
    "on track",
    "completed as planned",
    "good progress",
    "well implemented",
    "successfully",
    "fully functional",
    "no major issues",
]
NEGATIONS = ["no", "not", "without", "never", "nor"]
ACCESS_CUES = ["could not", "no access", "not accessible", "postponed", "not monitored", "security", "closed"]

# The rules as seeded (fmm/0004_rule_defaults writes the same values as literals)
DEFAULTS: dict[str, dict[str, Any]] = {
    "R1": {
        "label": "Completeness",
        "points": 15,
        "threshold": None,
        "params": {"required": ["narrative", "q2"]},
        "description": (
            "The report has a general observation (narrative), on every rated entity, and an answer to "
            "Q2 (activities monitored). A rating for every entity and the place of the visit can be "
            "required too. Points in proportion to the elements present."
        ),
    },
    "R2": {
        "label": "Evidence sufficiency",
        "points": 20,
        "threshold": 80,
        "params": {"require_unanswered_seen": True},
        "description": (
            "The share of the checklist questions answered, counted once per question and entity: full "
            "points at the target share (80%) or above, fewer below it. Not available when eTools sends "
            "no unanswered question at all, since every visit would then pass."
        ),
    },
    "R3": {
        "label": "HACT alignment",
        "points": 20,
        "threshold": None,
        "params": {"strict": False},
        "description": (
            "Every rated entity's overall finding agrees with its HACT Q1 answer (its own, else the one "
            "given for its partner, else the one given for the whole visit). Only On track against Off "
            "track is a conflict: Constrained agrees with either, unless the strict setting is on."
        ),
    },
    "R4": {
        "label": "Narrative coherence",
        "points": 15,
        "threshold": 25,
        "params": {"placeholders": PLACEHOLDERS, "check_copies": True, "copy_window_days": 365},
        "description": (
            "Each narrative has at least 25 words, is not a placeholder such as “n/a”, and is not word "
            "for word the narrative of another visit within a year. Points in proportion to the "
            "narratives that pass."
        ),
    },
    "R5": {
        "label": "Q3 quality",
        "points": 15,
        "threshold": 15,
        "params": {"placeholders": PLACEHOLDERS},
        "description": (
            "Q3 (key observations and findings) is answered with at least 15 words, answer and summary "
            "together, and not with a placeholder. Points in proportion to the words, up to 15."
        ),
    },
    "R6": {
        "label": "Rating quality",
        "points": 0,
        "threshold": 2,
        "params": {
            "negative_cues": NEGATIVE_CUES,
            "positive_cues": POSITIVE_CUES,
            "negations": NEGATIONS,
            "negation_window": 3,
            "access_cues": ACCESS_CUES,
            "check_not_monitored": False,
        },
        "description": (
            "The narrative does not contradict the rating: an entity rated On track whose narrative "
            "names two or more problems (delayed, suspended...) and nothing good, or one rated Off track "
            "that names only good points. A word preceded by “no” or “not” within three words does not "
            "count. A flag only: no points unless an administrator gives it some."
        ),
    },
}


# ------------------------------------------------------------------------------------------ facts
@dataclass(frozen=True)
class EntityFacts:
    """One monitored entity of a visit, as the rules see it."""

    kind: str
    rating: str  # normalised: on_track | constrained | off_track | not_monitored | other
    rating_raw: str
    partner_id: int | None
    hact_q1: str  # effective Q1 (own, else its partner's, else the visit's; score.effective_q1)
    narrative: str  # the source text, in memory only while scoring; never stored
    words: int = 0
    placeholder: bool = False  # the narrative is one of R4's placeholders
    narrative_hash: str = ""  # sha1 of the folded text, for narratives of R4's minimum words or more


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
    entity: int | None = None  # index in VisitFacts.entities (applies_to "entity")
    partner_id: int | None = None  # the partner (applies_to "partner")
    from_row: bool = (
        False  # the HACT answer written on the finding row (hact_q1_answer...), not a checklist one
    )


@dataclass(frozen=True)
class VisitFacts:
    key: str
    status_group: str
    scorable: bool
    is_programmatic: bool
    end_date: date | None
    has_place: bool
    entities: tuple[EntityFacts, ...]
    answers: tuple[AnswerFacts, ...]
    questions_in_dataset: frozenset[str]  # roles found anywhere, and "hact" when a question is flagged so
    answers_available: bool  # the answer (or its label) key of the checklist records was found
    unanswered_seen: bool  # the records hold at least one unanswered or placeholder answer (R2)
    question_records: int = 0  # checklist records in the data (R2: "cannot be measured" from 200)


@dataclass(frozen=True)
class RuleOutcome:
    rule: str
    status: str
    points: float
    max_points: int
    detail_key: str = ""
    detail: str = ""
    measure: float | None = None


@dataclass(frozen=True)
class Context:
    today: date
    copies: Mapping[str, list[tuple[str, date | None]]] = field(default_factory=dict)  # hash -> visits
    labels: Mapping[str, str] = field(default_factory=dict)  # visit key -> "Visit 1588"
    narrative_min_words: int = 25  # R4's threshold (R6's not-monitored check reads it too)


# ------------------------------------------------------------------------------------------ helpers
def half_up(value: float | Decimal, digits: int = 1) -> Decimal:
    """``value`` rounded with halves away from zero (12.25 -> 12.3), as the pages show figures."""
    if not isinstance(value, Decimal):
        value = Decimal(f"{value:.9f}")  # no binary noise (84.94999...) decides a half
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def number(value: float | Decimal) -> str:
    """46.2 -> "46.2"; 80.0 -> "80"."""
    rounded = half_up(value, 1)
    return str(rounded.to_integral_value()) if rounded == rounded.to_integral_value() else str(rounded)


def words_text(n: int) -> str:
    return f"{n} word" if n == 1 else f"{n} words"


def param(rule, name: str) -> Any:
    """A parameter of ``rule``: its own value, else the default."""
    params = rule.params if isinstance(rule.params, dict) else {}
    if name in params:
        return params[name]
    return DEFAULTS[rule.code]["params"][name]


def threshold(rule) -> float:
    """The rule's threshold, else its default."""
    if rule.threshold is not None:
        return float(rule.threshold)
    return float(DEFAULTS[rule.code]["threshold"] or 0)


def _outcome(rule, status: str, points: float = 0.0, key: str = "", detail: str = "", measure=None):
    points = float(min(max(half_up(points, 1), Decimal(0)), Decimal(rule.points)))
    return RuleOutcome(rule.code, status, points, rule.points, key, detail, measure)


def _share(rule, share: float) -> float:
    """``share`` of the rule's points (never above them)."""
    return rule.points * max(0.0, min(1.0, share))


def label_of(key: str) -> str:
    """How a visit is named in a detail when its label is not at hand: "Visit 1588" for an id."""
    return f"Visit {key}" if key.isdecimal() else key


def q1_value(answer: AnswerFacts) -> str:
    """The normalised Q1 of an answered Q1 answer: its rating code, else "other" (not a rating)."""
    return answer.rating or "other"


def worst(values: Iterable[str]) -> str:
    """The worst of rating codes (off track, constrained, on track, other, not monitored); "" if none."""
    from neurodb.datamart.fm import RATING_ORDER

    values = [v for v in values if v]
    return max(values, key=lambda v: RATING_ORDER.get(v, 0)) if values else ""


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


# ------------------------------------------------------------------------------------------ the rules
def _has_narrative(entity: EntityFacts) -> bool:
    return bool(entity.narrative and entity.narrative.strip())


def r1_completeness(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    required = [e for e in R1_ELEMENTS if e in (param(rule, "required") or ())]
    rated = [e for e in facts.entities if e.rating in RATED]
    evaluated: list[str] = []
    missing: list[str] = []
    for element in required:
        if element == "narrative":
            present = any(_has_narrative(e) for e in facts.entities) and all(_has_narrative(e) for e in rated)
        elif element == "q2":
            q2 = [a for a in facts.answers if a.role == "q2"]
            # only when the visit has a Q2 answer to read: on its rows, or among its checklist answers
            if not q2 or not (facts.answers_available or any(a.from_row for a in q2)):
                continue
            present = any(a.answered for a in q2)
        elif element == "rating":
            if not facts.entities:
                continue
            present = all((e.rating_raw or "").strip() for e in facts.entities)
        else:
            present = facts.has_place
        evaluated.append(element)
        if not present:
            missing.append(element)
    if not evaluated:
        return _outcome(
            rule, "na", key="no_elements", detail="None of the elements this rule checks is in the data."
        )
    share = (len(evaluated) - len(missing)) / len(evaluated)
    measure = float(half_up(100 * share, 1))
    if missing:
        detail = "Incomplete monitoring report — missing: " + ", ".join(R1_LABELS[m] for m in missing)
        return _outcome(rule, "fail", _share(rule, share), "missing:" + ",".join(missing), detail, measure)
    return _outcome(rule, "pass", rule.points, "complete", "Every element of the report is there.", measure)


def _unit(answer: AnswerFacts) -> str:
    """What one answer is about, for counting questions (R2): an entity, a partner or the visit."""
    if answer.applies_to == "entity" and answer.entity is not None:
        return f"entity:{answer.entity}"
    if answer.applies_to == "partner" and answer.partner_id is not None:
        return f"partner:{answer.partner_id}"
    return "visit"


def questions_answered(answers: Iterable[AnswerFacts]) -> tuple[int, int]:
    """(asked, answered): one per checklist question and entity (or partner, or the visit), answered
    when one of its records is. The HACT answers written on the finding rows are not checklist
    questions and are not counted."""
    asked: dict[tuple[str, str], bool] = {}
    for answer in answers:
        if answer.from_row:
            continue
        pair = (answer.question_key, _unit(answer))
        asked[pair] = asked.get(pair, False) or answer.answered
    return len(asked), sum(asked.values())


def _no_answers(facts: VisitFacts, rule, role: str = "") -> RuleOutcome | None:
    """``na`` when the visit has no checklist answer, or when the answers of the checklist records
    were not found under any key (every answer would read as blank: R2, R3 and R5 cannot be told).
    With ``role``, the answers of that question written on the finding rows are enough."""
    if role and any(a.from_row and a.role == role for a in facts.answers):
        return None
    if not any(not a.from_row for a in facts.answers):
        return _outcome(
            rule, "na", key="no_answers", detail="No checklist answers for this visit in the eTools data."
        )
    if not facts.answers_available:
        return _outcome(
            rule,
            "na",
            key="answers_not_found",
            detail="The answers of the checklist records were not found (Fields found).",
        )
    return None


def r2_evidence(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    if (missing := _no_answers(facts, rule)) is not None:
        return missing
    if (
        param(rule, "require_unanswered_seen")
        and not facts.unanswered_seen
        and facts.question_records >= R2_MIN_RECORDS
    ):
        return _outcome(
            rule,
            "na",
            key="cannot_be_measured",
            detail=(
                f"Cannot be measured: none of the {facts.question_records:,} checklist records in the eTools "
                "data is unanswered, so eTools probably sends answered questions only."
            ),
        )
    asked, answered = questions_answered(facts.answers)
    pct = half_up(Decimal(100 * answered) / Decimal(asked), 1)
    target = threshold(rule)
    share = float(pct) / target if target else 1.0
    measure = float(pct)
    if float(pct) < target:
        detail = f"Only {number(pct)}% of monitoring questions answered (target: {number(target)}%+)"
        return _outcome(rule, "fail", _share(rule, share), "below_threshold", detail, measure)
    detail = f"{number(pct)}% of monitoring questions answered (target: {number(target)}%+)"
    return _outcome(rule, "pass", rule.points, "answered", detail, measure)


def r3_hact(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    if (missing := _no_answers(facts, rule, "q1")) is not None:
        return missing
    if not ({"q1", "hact"} & facts.questions_in_dataset):
        return _outcome(
            rule,
            "na",
            key="no_q1_question",
            detail="No HACT Q1 question was found in the eTools data (Questions found).",
        )
    asked_here = any(a.role == "q1" or a.is_hact for a in facts.answers)
    if not facts.is_programmatic and not asked_here:
        return _outcome(
            rule,
            "nap",
            key="not_programmatic",
            detail="Not a programmatic visit, and no HACT Q1 question was asked.",
        )
    strict = param(rule, "strict") is True
    rated = [e for e in facts.entities if e.rating in RATED]
    failures: list[tuple[int, str, str]] = []  # (priority, key, detail)
    if rated:
        for entity in rated:
            q1 = entity.hact_q1
            if not q1:
                failures.append((1, "q1_missing", "HACT Q1 not answered"))
            elif q1 not in RATED:
                failures.append((2, "q1_unrecognised", "HACT Q1 answer is not a rating"))
            elif (strict and q1 != entity.rating) or {q1, entity.rating} == {"on_track", "off_track"}:
                said, found = RATING_LABELS[q1], RATING_LABELS[entity.rating]
                detail = f"HACT Q1 says {said} but the overall finding is {found}"
                failures.append((3, "conflict", detail))
    else:  # nothing rated to compare: the visit's own Q1 must be there and be a rating
        visit_q1 = worst(
            [e.hact_q1 for e in facts.entities]
            + [
                q1_value(a)
                for a in facts.answers
                if a.role == "q1" and a.answered and a.applies_to == "visit"
            ]
        )
        if not visit_q1:
            failures.append((1, "q1_missing", "HACT Q1 not answered"))
        elif visit_q1 not in RATED:
            failures.append((2, "q1_unrecognised", "HACT Q1 answer is not a rating"))
    if failures:
        _priority, key, detail = max(failures, key=lambda f: f[0])
        return _outcome(rule, "fail", 0, key, detail, float(len(failures)))
    detail = (
        "HACT Q1 agrees with the overall finding of every rated entity."
        if rated
        else "HACT Q1 is answered; no entity is rated to compare it with."
    )
    return _outcome(rule, "pass", rule.points, "aligned", detail)


def _copy_of(facts: VisitFacts, digest: str, window: int, ctx: Context) -> str | None:
    """The other visit, within ``window`` days of this one, whose narrative is the same text."""
    if not digest or facts.end_date is None:
        return None
    others = [
        (abs((day - facts.end_date).days), key)
        for key, day in ctx.copies.get(digest, ())
        if key != facts.key and day is not None and abs((day - facts.end_date).days) <= window
    ]
    if not others:
        return None
    return min(others)[1]


def r4_narrative(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    narrated = [e for e in facts.entities if _has_narrative(e)]
    if not narrated:
        return _outcome(
            rule,
            "na",
            key="no_narrative",
            detail="No narrative is written for this visit (rule R1 covers it).",
        )
    minimum = round(threshold(rule))
    check_copies = param(rule, "check_copies") is not False
    window = int(param(rule, "copy_window_days"))
    passing = 0
    first: tuple[str, str, int] | None = None
    for entity in narrated:
        failure = None
        if entity.placeholder:
            failure = ("placeholder", "Narrative is a placeholder", entity.words)
        elif entity.words < minimum:
            failure = (
                "too_short",
                f"Narrative has {words_text(entity.words)} (minimum {minimum})",
                entity.words,
            )
        elif check_copies and (other := _copy_of(facts, entity.narrative_hash, window, ctx)):
            name = ctx.labels.get(other) or label_of(other)
            failure = ("copied", f"Narrative identical to {name}", entity.words)
        if failure is None:
            passing += 1
        elif first is None:
            first = failure
    share = passing / len(narrated)
    if first is not None:
        key, detail, words = first
        return _outcome(rule, "fail", _share(rule, share), key, detail, float(words))
    least = min(e.words for e in narrated)
    detail = f"Every narrative has {minimum} words or more and is the visit's own."
    return _outcome(rule, "pass", rule.points, "coherent", detail, float(least))


def r5_q3(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    if (missing := _no_answers(facts, rule, "q3")) is not None:
        return missing
    if "q3" not in facts.questions_in_dataset:
        return _outcome(
            rule,
            "na",
            key="no_q3_question",
            detail="No Q3 question was found in the eTools data (Questions found).",
        )
    q3 = [a for a in facts.answers if a.role == "q3"]
    if not q3:
        return _outcome(rule, "nap", key="no_q3", detail="No Q3 question was asked on this visit.")
    minimum = round(threshold(rule))
    given = [a for a in q3 if a.answered and not a.placeholder]
    if not given:
        if any(a.placeholder for a in q3):
            return _outcome(rule, "fail", 0, "q3_placeholder", "Q3 answer is a placeholder", 0.0)
        return _outcome(rule, "fail", 0, "q3_missing", "Q3 not answered", 0.0)
    words = max(a.words for a in given)
    share = words / minimum if minimum else 1.0
    if words < minimum:
        detail = f"Q3 answer has {words_text(words)} (minimum {minimum})"
        return _outcome(rule, "fail", _share(rule, share), "q3_short", detail, float(words))
    return _outcome(
        rule, "pass", rule.points, "answered", f"Q3 answered with {words_text(words)}.", float(words)
    )


@functools.lru_cache(maxsize=4096)
def _folded_words(phrase: str) -> tuple[str, ...]:
    """A cue or a negation, folded into words (kept: the same few dozen are read for every narrative,
    and folding them again for each one took most of a rescore)."""
    return tuple(parse.fold(phrase).split())


@functools.lru_cache(maxsize=64)
def _text_words(text: str) -> tuple[str, ...]:
    """A narrative's folded words (kept for the next call: each narrative is searched for its
    negative, its positive and its access cues in turn; a tuple, so no caller can change what is kept)."""
    return tuple(parse.fold(text).split())


def cues_found(text: str, cues: Iterable[str], negations: Iterable[str], window: int) -> list[str]:
    """The cues of ``cues`` (in their list order) that ``text`` holds as whole words at least once
    without a negation in the ``window`` words before them."""
    words = _text_words(str(text or ""))
    # (``str(x or "")`` as ``parse.fold`` reads a value: a blank entry is no word, never "none")
    negating = {" ".join(folded) for n in negations if (folded := _folded_words(str(n or "")))}
    found = []
    for cue in cues:
        cue_words = _folded_words(str(cue or ""))
        if not cue_words or cue in found:
            continue
        size = len(cue_words)
        for i in range(len(words) - size + 1):
            if words[i : i + size] != cue_words:
                continue
            if not negating.intersection(words[max(0, i - window) : i]):
                found.append(cue)
                break
    return found


def r6_rating(facts: VisitFacts, rule, ctx: Context) -> RuleOutcome:
    check_not_monitored = param(rule, "check_not_monitored") is True
    rated = [e for e in facts.entities if e.rating in RATED and _has_narrative(e)]
    if not rated and not check_not_monitored:
        return _outcome(rule, "na", key="no_narrative", detail="No rated entity has a narrative to compare.")
    minimum = max(1, round(threshold(rule)))
    negatives = list(param(rule, "negative_cues") or ())
    positives = list(param(rule, "positive_cues") or ())
    negations = list(param(rule, "negations") or ())
    window = int(param(rule, "negation_window"))
    for entity in rated:
        if entity.rating == "constrained":
            continue
        bad = cues_found(entity.narrative, negatives, negations, window)
        good = cues_found(entity.narrative, positives, negations, window)
        if entity.rating == "on_track" and len(bad) >= minimum and not good:
            detail = f"Narrative contradicts the rating (rated On track; it mentions: {', '.join(bad)})"
            return _outcome(rule, "fail", 0, "contradiction", detail[:300], float(len(bad)))
        if entity.rating == "off_track" and good and not bad:
            detail = f"Narrative contradicts the rating (rated Off track; it mentions: {', '.join(good)})"
            return _outcome(rule, "fail", 0, "contradiction", detail[:300], float(len(good)))
    if check_not_monitored:
        access = list(param(rule, "access_cues") or ())
        for entity in facts.entities:
            if (
                entity.rating == "not_monitored"
                and (entity.rating_raw or "").strip()
                and _has_narrative(entity)
                and entity.words >= ctx.narrative_min_words
                and not cues_found(entity.narrative, access, negations, 0)
            ):
                detail = "Narrative describes an entity rated Not monitored without saying why it was not"
                return _outcome(rule, "fail", 0, "not_monitored_described", detail, 0.0)
    return _outcome(rule, "pass", rule.points, "coherent", "No narrative contradicts its rating.", 0.0)


RULES: dict[str, Callable[[VisitFacts, Any, Context], RuleOutcome]] = {
    "R1": r1_completeness,
    "R2": r2_evidence,
    "R3": r3_hact,
    "R4": r4_narrative,
    "R5": r5_q3,
    "R6": r6_rating,
}


def evaluate(facts: VisitFacts, rules: Mapping[str, Any], ctx: Context) -> list[RuleOutcome]:
    """Every rule's outcome for one visit, R1 to R6: ``off`` for a rule switched off, ``nap`` for all
    of them when the visit is not scorable."""
    out = []
    for code in CODES:
        rule = rules.get(code)
        if rule is None or not rule.enabled:
            points = rule.points if rule is not None else 0
            out.append(RuleOutcome(code, "off", 0.0, points, "off", "This rule is switched off."))
        elif not facts.scorable:
            detail = (
                "The visit was cancelled."
                if facts.status_group == "cancelled"
                else "Pending: the visit's status is not one of the scored statuses (Score settings)."
            )
            out.append(RuleOutcome(code, "nap", 0.0, rule.points, "not_scorable", detail))
        else:
            out.append(RULES[code](facts, rule, ctx))
    return out


# ------------------------------------------------------------------------------------------ settings
PARAMS: dict[str, dict[str, Any]] = {
    "R1": {"required": "elements"},
    "R2": {"require_unanswered_seen": "bool"},
    "R3": {"strict": "bool"},
    "R4": {"placeholders": "list", "check_copies": "bool", "copy_window_days": (1, 1095)},
    "R5": {"placeholders": "list"},
    "R6": {
        "negative_cues": "list",
        "positive_cues": "list",
        "negations": "list",
        "negation_window": (0, 5),
        "access_cues": "list",
        "check_not_monitored": "bool",
    },
}
THRESHOLDS: dict[str, tuple[int, int, str]] = {  # (lowest, highest, what it counts); whole numbers but R2
    "R2": (1, 100, "the share of questions answered, in %"),
    "R4": (1, 500, "the fewest words of a narrative"),
    "R5": (1, 500, "the fewest words of the Q3 answer and summary"),
    "R6": (1, 10, "the number of different problem words that flag an On track narrative"),
}


def fold_list(values: Any, name: str) -> list[str]:
    """A list setting checked and folded: at most 60 entries of 1-80 characters each, every entry once."""
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValidationError(f'“{name}” must be a list of words or phrases, e.g. ["n/a", "see above"].')
    if len(values) > MAX_LIST:
        raise ValidationError(f"“{name}” holds {len(values)} entries; at most {MAX_LIST} are allowed.")
    out: list[str] = []
    for value in values:
        text = value.strip()
        if not 1 <= len(text) <= MAX_ENTRY:
            raise ValidationError(f"Each entry of “{name}” must have 1 to {MAX_ENTRY} characters.")
        folded = parse.fold(text)
        if not folded:
            raise ValidationError(f"“{value}” in “{name}” holds no letter or digit.")
        if folded not in out:
            out.append(folded)
    return out


def validate_rule(code: str, points: Any, threshold_value: Any, params: Any) -> dict:
    """``params`` checked for rule ``code`` and returned with its lists folded; a ``ValidationError``
    naming each problem in plain words otherwise."""
    errors: dict[str, list[str]] = {}
    if code not in CODES:
        raise ValidationError({"code": [f"Unknown rule “{code}”: the rules are {', '.join(CODES)}."]})
    if points is None or not 0 <= int(points) <= 50:
        errors.setdefault("points", []).append("Points go from 0 to 50.")
    if code in THRESHOLDS:
        low, high, what = THRESHOLDS[code]
        if threshold_value is None:
            errors.setdefault("threshold", []).append(
                f"{code} needs a threshold: {what}, from {low} to {high}."
            )
        else:
            value = Decimal(str(threshold_value))
            if not low <= value <= high:
                errors.setdefault("threshold", []).append(
                    f"The threshold of {code} ({what}) goes from {low} to {high}."
                )
            elif code != "R2" and value != value.to_integral_value():
                errors.setdefault("threshold", []).append(
                    f"The threshold of {code} ({what}) is a whole number."
                )
    elif threshold_value is not None:
        errors.setdefault("threshold", []).append(f"{code} uses no threshold: leave it empty.")
    cleaned: dict[str, Any] = {}
    if not isinstance(params, dict):
        errors.setdefault("params", []).append('The settings must be an object, e.g. {"strict": false}.')
        params = {}
    kinds = PARAMS[code]
    for name, raw in params.items():
        kind = kinds.get(name)
        try:
            if kind is None:
                known = ", ".join(kinds) or "none"
                raise ValidationError(f"{code} has no setting “{name}” (its settings: {known}).")
            if kind == "bool":
                if not isinstance(raw, bool):
                    raise ValidationError(f"“{name}” must be true or false.")
                cleaned[name] = raw
            elif kind == "list":
                cleaned[name] = fold_list(raw, name)
            elif kind == "elements":
                if not isinstance(raw, list) or not raw or not all(isinstance(v, str) for v in raw):
                    elements = ", ".join(R1_ELEMENTS)
                    raise ValidationError(
                        f'“{name}” must list at least one of {elements}, e.g. ["narrative", "q2"].'
                    )
                unknown = [v for v in raw if v not in R1_ELEMENTS]
                if unknown:
                    raise ValidationError(
                        f"“{name}” can only hold {', '.join(R1_ELEMENTS)} (not {', '.join(unknown)})."
                    )
                cleaned[name] = [e for e in R1_ELEMENTS if e in raw]
            else:
                low, high = kind
                if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
                    raise ValidationError(f"“{name}” must be a whole number from {low} to {high}.")
                cleaned[name] = raw
        except ValidationError as exc:
            errors.setdefault("params", []).extend(exc.messages)
    if errors:
        raise ValidationError(errors)
    return cleaned
