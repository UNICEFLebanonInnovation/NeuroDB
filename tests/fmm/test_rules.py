"""The quality rules R1-R6 (``fmm.rules``), each a pure function of a visit's facts and its settings:
pass, fail (with its detail and measure), not available, does not apply, off; partial points, never
above the rule's maximum; and the question roles."""

from __future__ import annotations

import copy
import datetime
from decimal import Decimal

import pytest

from neurodb.fmm import refresh, rules, score
from neurodb.fmm.models import RuleSetting, Visit, VisitRuleResult
from neurodb.fmm.rules import AnswerFacts, Context, EntityFacts, VisitFacts

from .conftest import Q1_TEXT, Q2_TEXT, Q3_TEXT

TODAY = datetime.date(2026, 10, 5)
CTX = Context(today=TODAY)
LONG = (
    "Activities at the Saadnayel centre were implemented as planned. Two hundred children attended the four "
    "sessions observed and the attendance registers matched the figures reported."
)  # 26 words


def rule(code: str, **changes) -> RuleSetting:
    """A rule as seeded (not saved), with ``changes``."""
    default = rules.DEFAULTS[code]
    values = {
        "label": default["label"],
        "points": default["points"],
        "threshold": default["threshold"],
        "params": copy.deepcopy(default["params"]),
        "enabled": True,
    }
    return RuleSetting(code=code, **{**values, **changes})


def entity(rating="on_track", narrative="", *, kind="pd", partner=1, q1="", raw=None) -> EntityFacts:
    placeholders = frozenset(rules.PLACEHOLDERS)
    words, placeholder, digest = score.narrative_measures(narrative, 25, placeholders)
    written = {"on_track": "On Track", "off_track": "Off Track", "not_monitored": ""}.get(rating, rating)
    written = written if raw is None else raw
    return EntityFacts(kind, rating, written, partner, q1, narrative, words, placeholder, digest)


def answer(role="", answered=True, **kw) -> AnswerFacts:
    values = {
        "question_key": role or "other",
        "role": role,
        "answered": answered,
        "placeholder": False,
        "words": 0,
    }
    return AnswerFacts(**{**values, **kw})


def facts(entities=(), answers=(), **kw) -> VisitFacts:
    values = {
        "key": "1722",
        "status_group": "reported",
        "scorable": True,
        "is_programmatic": True,
        "end_date": datetime.date(2026, 5, 12),
        "has_place": True,
        "entities": tuple(entities),
        "answers": tuple(answers),
        "questions_in_dataset": frozenset({"q1", "q2", "q3", "psea", "hact"}),
        "answers_available": True,
        "unanswered_seen": True,
        "question_records": 1000,
    }
    return VisitFacts(**{**values, **kw})


def run(code: str, visit: VisitFacts, ctx: Context = CTX, **changes) -> rules.RuleOutcome:
    return rules.RULES[code](visit, rule(code, **changes), ctx)


# ------------------------------------------------------------------------------------------ R1
def test_r1_passes_with_a_narrative_on_every_rated_entity_and_q2_answered():
    out = run("R1", facts([entity(narrative="Seen."), entity("not_monitored")], [answer("q2")]))
    assert (out.status, out.points, out.max_points, out.detail_key, out.measure) == (
        "pass",
        15,
        15,
        "complete",
        100,
    )


def test_r1_fails_with_what_is_missing():
    out = run("R1", facts([entity(), entity(narrative="Seen.")], [answer("q2", answered=False)]))
    assert (out.status, out.points, out.detail_key) == ("fail", 0, "missing:narrative,q2")
    assert out.detail == (
        "Incomplete monitoring report — missing: General observation (narrative), Q2 – Activities monitored"
    )


def test_r1_gives_points_for_the_elements_present():
    out = run("R1", facts([entity(narrative="Seen.")], [answer("q2", answered=False)]))
    assert (out.status, out.points, out.detail_key, out.measure) == ("fail", 7.5, "missing:q2", 50.0)
    # q2 is evaluated only when the visit has a Q2 question
    assert run("R1", facts([entity(narrative="Seen.")], [answer("q3")])).status == "pass"


def test_r1_optional_elements_and_not_available():
    visit = facts([entity(narrative="Seen.", raw="")], has_place=False)
    out = run("R1", visit, params={"required": ["narrative", "rating", "location"]})
    assert (out.status, out.points, out.detail_key) == ("fail", 5.0, "missing:rating,location")
    assert "a rating for every entity, the place of the visit" in out.detail
    assert run("R1", facts([entity()]), params={"required": ["q2"]}).status == "na"


# ------------------------------------------------------------------------------------------ R2
def test_r2_counts_question_and_entity_pairs():
    """A question asked for 3 entities and answered for 2 gives 2 of 3."""
    answers = [answer(question_key="12", applies_to="entity", entity=i, answered=i < 2) for i in range(3)]
    out = run("R2", facts([entity()] * 3, answers))
    assert (out.status, out.detail_key, out.measure) == ("fail", "below_threshold", 66.7)
    assert out.points == 16.7  # 20 x 66.7 / 80
    assert out.detail == "Only 66.7% of monitoring questions answered (target: 80%+)"
    assert rules.questions_answered(answers) == (3, 2)


def test_r2_partner_and_visit_level_answers_count_once_each():
    answers = [
        answer(question_key="12", applies_to="visit", answered=False),
        answer(question_key="12", applies_to="visit"),  # the same question for the visit, answered once
        answer(question_key="12", applies_to="partner", partner_id=7, answered=False),
        answer(question_key="12", applies_to="partner", partner_id=7),
        answer(question_key="13", applies_to="partner", partner_id=7, answered=False),
    ]
    assert rules.questions_answered(answers) == (3, 2)


def test_r2_passes_at_the_threshold_and_never_gives_more_than_its_points():
    answers = [answer(question_key=str(n), answered=n < 4) for n in range(5)]  # 80%
    out = run("R2", facts([entity()], answers))
    assert (out.status, out.points, out.detail_key) == ("pass", 20, "answered")
    out = run("R2", facts([entity()], answers), threshold=Decimal(50))
    assert out.points == 20 == out.max_points  # 80% against a 50% target: still 20


def test_r2_is_not_available_without_answers_or_when_it_cannot_be_measured():
    assert run("R2", facts([entity()])).detail_key == "no_answers"
    answers = [answer(question_key="12")]
    assert run("R2", facts([entity()], answers, answers_available=False)).detail_key == "answers_not_found"
    # >= 200 records and none unanswered: eTools sends answered questions only
    out = run("R2", facts([entity()], answers, unanswered_seen=False, question_records=200))
    assert (out.status, out.detail_key) == ("na", "cannot_be_measured")
    assert "none of the 200 checklist records" in out.detail
    # under 200 records, or with the check switched off, it is measured
    assert run("R2", facts([entity()], answers, unanswered_seen=False, question_records=199)).status == "pass"
    out = run(
        "R2",
        facts([entity()], answers, unanswered_seen=False, question_records=500),
        params={"require_unanswered_seen": False},
    )
    assert out.status == "pass"


# ------------------------------------------------------------------------------------------ R3
def q1(value="on_track", **kw) -> AnswerFacts:
    return answer("q1", rating=value if value in rules.RATED else "", answer_code=value, is_hact=True, **kw)


@pytest.mark.parametrize(
    ("rating", "said", "status"),
    [
        ("on_track", "constrained", "pass"),
        ("off_track", "constrained", "pass"),
        ("constrained", "on_track", "pass"),
        ("on_track", "on_track", "pass"),
        ("on_track", "off_track", "fail"),
        ("off_track", "on_track", "fail"),
    ],
)
def test_r3_only_on_track_against_off_track_is_a_conflict(rating, said, status):
    out = run("R3", facts([entity(rating, q1=said)], [q1(said)]))
    assert out.status == status
    if status == "fail":
        assert out.detail_key == "conflict" and out.points == 0
    else:
        assert out.points == 20


def test_r3_conflict_detail_and_strict_mode():
    out = run("R3", facts([entity("on_track", q1="off_track")], [q1("off_track")]))
    assert out.detail == "HACT Q1 says Off track but the overall finding is On track"
    strict = run(
        "R3", facts([entity("on_track", q1="constrained")], [q1("constrained")]), params={"strict": True}
    )
    assert (strict.status, strict.detail_key) == ("fail", "conflict")
    assert "says Constrained but the overall finding is On track" in strict.detail


def test_r3_missing_unrecognised_not_applicable_and_not_available():
    other_question = [answer(question_key="16")]
    # a programmatic visit without any Q1 fails; a non-programmatic one without a Q1 question: n/a
    out = run("R3", facts([entity()], other_question))
    assert (out.status, out.detail_key, out.detail) == ("fail", "q1_missing", "HACT Q1 not answered")
    assert run("R3", facts([entity()], other_question, is_programmatic=False)).status == "nap"
    # a Q1 answered with something that is not a rating
    out = run("R3", facts([entity(q1="other")], [q1("yes")]))
    assert (out.status, out.detail_key) == ("fail", "q1_unrecognised")
    # no question data for the visit, or no Q1 / HACT question anywhere in the data
    assert run("R3", facts([entity()])).status == "na"
    out = run("R3", facts([entity()], other_question, questions_in_dataset=frozenset({"q2"})))
    assert (out.status, out.detail_key) == ("na", "no_q1_question")


def _with_effective_q1(entities: list[EntityFacts], answers: list[AnswerFacts]) -> list[EntityFacts]:
    """The entities with their effective Q1 (``score.effective_q1``), as the scoring gives them."""
    effective = score.effective_q1(entities, answers)
    return [
        EntityFacts(e.kind, e.rating, e.rating_raw, e.partner_id, value, e.narrative)
        for e, (value, _from) in zip(entities, effective, strict=True)
    ]


def test_r3_uses_the_effective_q1_of_each_entity():
    """A Q1 answered once for the visit, or once for the partner, satisfies every entity."""
    entities = [entity("on_track", partner=1), entity("on_track", partner=1)]
    for given in (
        q1("on_track", applies_to="visit"),
        q1("on_track", applies_to="partner", partner_id=1),
    ):
        assert run("R3", facts(_with_effective_q1(entities, [given]), [given])).status == "pass"
    # another partner's Q1 does not count
    other = q1("on_track", applies_to="partner", partner_id=2)
    assert run("R3", facts(_with_effective_q1(entities, [other]), [other])).detail_key == "q1_missing"


def test_r3_on_the_built_visits(fm_world):
    """1726: Q1 answered once for the partner; 1727: once for the visit. Both satisfy every entity."""
    refresh.run(triggered_by="test", today=TODAY)
    results = dict(VisitRuleResult.objects.filter(rule="R3").values_list("visit__key", "status"))
    assert results["1726"] == results["1727"] == "pass"
    assert dict(Visit.objects.values_list("key", "hact_q1"))["1727"] == "constrained"
    # 1722's partner row has no Q1 of its own, for its partner or for the visit
    assert VisitRuleResult.objects.get(visit__key="1722", rule="R3").detail_key == "q1_missing"
    assert results["1728"] == "nap"  # not programmatic, no Q1 asked


# ------------------------------------------------------------------------------------------ R4
def test_r4_passes_long_narratives_and_fails_short_ones():
    assert run("R4", facts([entity(narrative=LONG)])).status == "pass"
    out = run("R4", facts([entity(narrative="Classes were held in two rooms with nine children present.")]))
    assert (out.status, out.detail_key, out.measure, out.points) == ("fail", "too_short", 10.0, 0)
    assert out.detail == "Narrative has 10 words (minimum 25)"


def test_r4_partial_points_per_narrated_entity_and_placeholders():
    out = run("R4", facts([entity(narrative=LONG), entity(narrative="n/a"), entity()]))  # 1 of 2 narrated
    assert (out.status, out.points, out.detail_key, out.detail) == (
        "fail",
        7.5,
        "placeholder",
        "Narrative is a placeholder",
    )


def test_r4_copies_within_the_window_only():
    copied = entity(narrative=LONG)
    visit = facts([copied], key="1722", end_date=datetime.date(2026, 5, 12))
    near = Context(
        TODAY, copies={copied.narrative_hash: [("1722", visit.end_date), ("1588", datetime.date(2025, 6, 1))]}
    )
    out = run("R4", visit, near)
    assert (out.status, out.detail_key, out.detail) == ("fail", "copied", "Narrative identical to Visit 1588")
    labelled = Context(TODAY, copies=near.copies, labels={"1588": "FM-2025-588"})
    assert run("R4", visit, labelled).detail == "Narrative identical to FM-2025-588"
    far = Context(
        TODAY,
        copies={copied.narrative_hash: [("1722", visit.end_date), ("1588", datetime.date(2025, 5, 11))]},
    )
    assert run("R4", visit, far).status == "pass"  # 366 days apart
    assert run("R4", visit, near, params={"check_copies": False}).status == "pass"
    assert run("R4", visit, near, params={"check_copies": True, "copy_window_days": 300}).status == "pass"


def test_r4_reads_its_threshold_and_is_not_available_without_a_narrative():
    short = facts([entity(narrative="Classes were held in two rooms with nine children present.")])
    assert run("R4", short, threshold=Decimal(10)).status == "pass"
    out = run("R4", facts([entity(), entity("not_monitored")]))
    assert (out.status, out.detail_key) == ("na", "no_narrative")


# ------------------------------------------------------------------------------------------ R5
def test_r5_points_by_words():
    out = run("R5", facts([entity()], [answer("q3", words=6)]))
    assert (out.status, out.detail_key, out.points, out.measure) == ("fail", "q3_short", 6.0, 6.0)
    assert out.detail == "Q3 answer has 6 words (minimum 15)"
    out = run("R5", facts([entity()], [answer("q3", words=40)]))
    assert (out.status, out.points, out.max_points) == ("pass", 15, 15)  # never above its points


def test_r5_missing_placeholder_not_applicable_and_not_available():
    out = run("R5", facts([entity()], [answer("q3", answered=False)]))
    assert (out.status, out.detail_key, out.detail, out.points) == (
        "fail",
        "q3_missing",
        "Q3 not answered",
        0,
    )
    out = run("R5", facts([entity()], [answer("q3", answered=True, placeholder=True, words=1)]))
    assert (out.detail_key, out.detail, out.points) == ("q3_placeholder", "Q3 answer is a placeholder", 0)
    assert run("R5", facts([entity()], [answer("q2")])).status == "nap"  # question data, no Q3 asked
    assert run("R5", facts([entity()])).status == "na"  # no question data
    out = run("R5", facts([entity()], [answer("q2")], questions_in_dataset=frozenset({"q1"})))
    assert (out.status, out.detail_key) == ("na", "no_q3_question")
    assert run("R5", facts([entity()], [answer("q3", words=8)]), threshold=Decimal(8)).status == "pass"


# ------------------------------------------------------------------------------------------ R6
DELAYED = "The sessions were delayed and then suspended for two weeks."


def test_r6_on_track_with_two_problem_words_fails_with_one_passes():
    out = run("R6", facts([entity("on_track", DELAYED)]))
    assert (out.status, out.detail_key, out.points, out.max_points, out.measure) == (
        "fail",
        "contradiction",
        0,
        0,
        2.0,
    )
    assert out.detail == "Narrative contradicts the rating (rated On track; it mentions: delayed, suspended)"
    assert run("R6", facts([entity("on_track", "The sessions were delayed by a week.")])).status == "pass"
    # a good word as well: no contradiction
    assert (
        run("R6", facts([entity("on_track", DELAYED + " The partner made good progress.")])).status == "pass"
    )


def test_r6_negated_words_do_not_count():
    assert (
        run("R6", facts([entity("on_track", "There was no delay and nothing was not suspended.")])).status
        == "pass"
    )
    assert (
        rules.cues_found(
            "Sessions were not delayed, without shortage.", rules.NEGATIVE_CUES, rules.NEGATIONS, 3
        )
        == []
    )
    assert rules.cues_found("Not on track at all.", rules.POSITIVE_CUES, rules.NEGATIONS, 3) == []


def test_r6_off_track_with_only_good_words_fails_and_constrained_never_does():
    out = run("R6", facts([entity("off_track", "The partner successfully reached every child.")]))
    assert (out.status, out.detail) == (
        "fail",
        "Narrative contradicts the rating (rated Off track; it mentions: successfully)",
    )
    mixed = "The partner successfully reached some children but sessions were delayed."
    assert run("R6", facts([entity("off_track", mixed)])).status == "pass"
    assert run("R6", facts([entity("constrained", DELAYED)])).status == "pass"


def test_r6_threshold_points_and_the_not_monitored_check():
    assert run("R6", facts([entity("on_track", DELAYED)]), threshold=Decimal(3)).status == "pass"
    out = run("R6", facts([entity("on_track", DELAYED)]), points=15)
    assert (out.status, out.points, out.max_points) == ("fail", 0, 15)
    # not monitored, described at length without saying why: flagged only when the check is on
    described = entity("not_monitored", LONG, raw="Not Monitored")
    assert run("R6", facts([described])).status == "na"  # off by default: nothing rated to compare
    params = {**rules.DEFAULTS["R6"]["params"], "check_not_monitored": True}
    out = run("R6", facts([described]), params=params)
    assert (out.status, out.detail_key) == ("fail", "not_monitored_described")
    no_access = entity("not_monitored", LONG + " The team could not reach the site.", raw="Not Monitored")
    assert run("R6", facts([no_access]), params=params).status == "pass"


# ------------------------------------------------------------------------------------------ all rules
def test_evaluate_gives_off_and_not_applicable():
    book = {code: rule(code) for code in rules.CODES}
    book["R2"] = rule("R2", enabled=False)
    out = {o.rule: o for o in rules.evaluate(facts([entity(narrative=LONG)], [answer("q2")]), book, CTX)}
    assert out["R2"].status == "off" and out["R1"].status == "pass"
    planned = rules.evaluate(facts([entity()], status_group="planned", scorable=False), book, CTX)
    assert {o.status for o in planned if o.rule != "R2"} == {"nap"}
    assert {o.detail for o in planned if o.status == "nap"} == {"The visit is not reported yet."}
    cancelled = rules.evaluate(facts([entity()], status_group="cancelled", scorable=False), book, CTX)
    assert {o.detail for o in cancelled if o.status == "nap"} == {"The visit was cancelled."}


def test_points_are_never_above_the_maximum():
    over = rules._outcome(rule("R5"), "pass", 40)
    assert over.points == 15 and rules._outcome(rule("R5"), "fail", -3).points == 0


# ------------------------------------------------------------------------------------------ roles
PATTERNS = {
    "q1": ["implemented as planned", "activities been implemented"],
    "q2": ["^q2", "^q 2", "activities monitored"],
    "q3": ["^q3", "^q 3"],
    "psea": ["psea", "sexual exploitation", "sexual abuse"],
}


def test_assign_roles_with_contains_starts_and_whole_text_patterns():
    assert rules.assign_roles(Q1_TEXT, True, PATTERNS) == "q1"
    assert rules.assign_roles(Q2_TEXT, False, PATTERNS) == "q2"  # "^q2": "Q2 – Activities..." starts with it
    assert rules.assign_roles(Q3_TEXT, False, PATTERNS) == "q3"
    assert rules.assign_roles("Q21 – Other", False, PATTERNS) == ""  # "^q2" needs a word boundary
    assert rules.assign_roles("Were any PSEA concerns observed?", None, PATTERNS) == "psea"
    assert rules.assign_roles("Anything else?", True, PATTERNS) == ""
    exact = {**PATTERNS, "q3": ["=key observations"]}
    assert rules.assign_roles("Key observations", False, exact) == "q3"
    assert rules.assign_roles("Key observations and findings", False, exact) == ""


def test_a_whole_text_pinned_to_a_role_wins_over_a_broader_pattern():
    pinned = {**PATTERNS, "psea": [*PATTERNS["psea"], "=were activities implemented as planned for psea"]}
    assert rules.assign_roles("Were activities implemented as planned for PSEA?", False, pinned) == "psea"


def test_without_q1_patterns_the_hact_question_is_q1():
    patterns = {**PATTERNS, "q1": []}
    assert rules.assign_roles("Anything else?", True, patterns) == "q1"
    assert rules.assign_roles("Anything else?", False, patterns) == ""


def test_patterns_are_folded_not_regular_expressions():
    assert (
        rules.matches(rules.parse.fold("Is (a+)+ here?"), "(a+)+") is True
    )  # plain words, punctuation dropped
    assert rules.matches(rules.parse.fold("Écoles visitées"), "ecoles") is True
    assert rules.matches("anything", "=") is False
