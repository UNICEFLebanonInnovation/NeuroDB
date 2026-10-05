"""Scoring a visit (``fmm.score``): the score over the rules evaluated, its floor, bands and flags;
urgency and its worked examples; HACT Q1 (own, partner-level, visit-level) and the PSEA flag."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from neurodb.fmm import score
from neurodb.fmm.build import ActionPointFacts
from neurodb.fmm.models import ScoreSetting, Visit, default_urgency_weights
from neurodb.fmm.rules import AnswerFacts, RuleOutcome
from neurodb.fmm.score import ScoreOutcome

from .test_rules import answer, entity, facts

TODAY = datetime.date(2026, 10, 5)


def setting(**changes) -> ScoreSetting:
    return ScoreSetting(**changes)  # the defaults, not saved


def outcome(rule: str, status: str, points: float, max_points: int) -> RuleOutcome:
    return RuleOutcome(rule, status, points, max_points)


# ------------------------------------------------------------------------------------------ score
def test_the_score_is_earned_over_the_points_evaluated():
    outcomes = [
        outcome("R1", "pass", 15, 15),
        outcome("R2", "fail", 8.3, 20),
        outcome("R3", "na", 0, 20),  # not available: out of the denominator
        outcome("R4", "fail", 7.5, 15),
        outcome("R5", "nap", 0, 15),
        outcome("R6", "pass", 0, 0),
    ]
    result = score.score_visit(facts([entity()]), outcomes, setting(), total=85)
    assert result.score == Decimal("61.6")  # 30.8 of 50, half up
    assert (result.points, result.max_points, result.evaluated) == (Decimal("30.8"), 50, ("R1", "R2", "R4"))
    assert (result.flags, result.band, result.not_scored_reason) == (("R2", "R4"), "medium", "")


def test_r6_at_0_points_still_flags_and_a_disabled_rule_is_out():
    outcomes = [
        outcome("R1", "pass", 15, 15),
        outcome("R2", "pass", 20, 20),
        outcome("R3", "off", 0, 20),
        outcome("R6", "fail", 0, 0),
    ]
    result = score.score_visit(facts([entity()]), outcomes, setting())
    assert (result.score, result.max_points, result.flags) == (Decimal("100.0"), 35, ("R6",))
    assert result.evaluated == ("R1", "R2")


def test_points_above_a_rules_maximum_never_count():
    result = score.score_visit(
        facts([entity()]), [outcome("R2", "pass", 40, 20), outcome("R1", "pass", 15, 15)], setting()
    )
    assert result.score == Decimal("100.0")


def test_too_few_points_evaluated_gives_no_score():
    outcomes = [outcome("R1", "fail", 7.5, 15), *[outcome(r, "na", 0, 20) for r in ("R2", "R3")]]
    result = score.score_visit(facts([entity()]), outcomes, setting(), total=85)
    assert (result.score, result.band, result.not_scored_reason) == (
        None,
        "",
        "too few rules (15 of 85 points)",
    )
    assert result.flags == ("R1",)  # the flag stays
    assert score.score_visit(facts([entity()]), outcomes, setting(min_evaluated_points=15)).score == 50


def test_visits_not_reported_are_not_scored():
    planned = score.score_visit(facts([entity()], status_group="planned", scorable=False), [], setting())
    assert (planned.score, planned.not_scored_reason) == (None, "not reported yet")
    cancelled = score.score_visit(facts([entity()], status_group="cancelled", scorable=False), [], setting())
    assert cancelled.not_scored_reason == "cancelled"


def test_scorable_visits():
    assert score.scorable("reported", 0, False)  # a not-monitored reported visit is scored
    assert score.scorable("unknown", 1, False) and score.scorable("unknown", 0, True)
    assert not score.scorable("unknown", 0, False)
    assert not score.scorable("in_progress", 3, True) and not score.scorable("planned", 0, False)


@pytest.mark.parametrize(
    ("earned", "band"),
    [(Decimal(80), "high"), (Decimal("79.9"), "medium"), (Decimal(50), "medium"), (Decimal("49.9"), "low")],
)
def test_bands_at_their_edges(earned, band):
    result = score.score_visit(
        facts([entity()]), [outcome("R1", "pass", float(earned), 100)], setting(min_evaluated_points=0)
    )
    assert result.band == band


def test_high_flag_from_three_flags():
    assert score.high_flag(3, setting()) and not score.high_flag(2, setting())
    assert score.high_flag(2, setting(high_flag_count=2))


def test_average_quality_is_half_up():
    assert score.average_quality([Decimal("90.0"), Decimal("85.5"), None]) == Decimal("87.8")
    assert score.average_quality([None]) is None


# ------------------------------------------------------------------------------------------ urgency
def visit(rating="on_track", q1="", group="reported", end=None) -> Visit:
    return Visit(rating=rating, hact_q1=q1, status_group=group, end_date=end)


def scored(value, flags=0) -> ScoreOutcome:
    value = None if value is None else Decimal(value)
    return ScoreOutcome(value, value, 85, (), "", tuple(f"R{n}" for n in range(1, flags + 1)), "")


def days_ago(n: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=n)


def open_point(due=None, high=False) -> ActionPointFacts:
    return ActionPointFacts(1, "open", due, high)


@pytest.mark.parametrize(
    ("case", "args", "total", "band", "parts"),
    [
        (
            "off track, reported 20 days ago, no action point, score 70, 1 flag",
            (visit("off_track", end=days_ago(20)), scored(70, 1), []),
            73,
            "red",
            {"rating": 40, "quality": 8, "flags": 5, "follow_up": 20, "report_late": 0},
        ),
        (
            "off track, an open action point not overdue, score 90, no flag",
            (visit("off_track", end=days_ago(20)), scored(90), [open_point(days_ago(-10))]),
            43,
            "amber",
            {"rating": 40, "quality": 3, "flags": 0, "follow_up": 0, "report_late": 0},
        ),
        (
            "on track but HACT Q1 constrained, reported 30 days ago, no action point, score 85, 1 flag",
            (visit("on_track", "constrained", end=days_ago(30)), scored(85, 1), []),
            49,
            "amber",
            {"rating": 20, "quality": 4, "flags": 5, "follow_up": 20, "report_late": 0},
        ),
        (
            "on track, score 90, 3 flags (the reference's typical visit)",
            (visit("on_track", "on_track", end=days_ago(30)), scored(90, 3), []),
            18,
            "",
            {"rating": 0, "quality": 3, "flags": 15, "follow_up": 0, "report_late": 0},
        ),
        (
            "in progress, end date 45 days ago",
            (visit("not_monitored", group="in_progress", end=days_ago(45)), scored(None), []),
            15,
            "",
            {"rating": 0, "quality": 0, "flags": 0, "follow_up": 0, "report_late": 15},
        ),
    ],
)
def test_urgency_worked_examples(case, args, total, band, parts):
    assert score.urgency(*args, setting(), TODAY) == (total, band, parts), case


def test_urgency_follow_up_parts():
    off = visit("off_track", end=days_ago(20))
    overdue_high = [open_point(days_ago(3), high=True)]
    assert score.urgency(off, scored(100), overdue_high, setting(), TODAY)[2]["follow_up"] == 20  # 12 + 8
    assert score.urgency(off, scored(100), [open_point(days_ago(3))], setting(), TODAY)[2]["follow_up"] == 12
    high_open = [open_point(days_ago(-3), high=True)]
    assert score.urgency(off, scored(100), high_open, setting(), TODAY)[2]["follow_up"] == 5
    done = [ActionPointFacts(1, "completed", days_ago(3), True)]  # closed: a follow-up, nothing open
    assert score.urgency(off, scored(100), done, setting(), TODAY)[2]["follow_up"] == 0
    # within the 14 days, no follow-up is expected yet
    assert (
        score.urgency(visit("off_track", end=days_ago(14)), scored(100), [], setting(), TODAY)[2]["follow_up"]
        == 0
    )
    # a reported visit that could not be scored
    assert score.urgency(visit(), scored(None), [], setting(), TODAY)[2]["quality"] == 10


def test_urgency_is_capped_and_its_bands_follow_the_settings():
    heavy = setting(urgency_weights={**default_urgency_weights(), "off_track": 100})
    assert score.urgency(visit("off_track", end=days_ago(20)), scored(0, 3), [], heavy, TODAY)[0] == 100

    def late(points: int) -> tuple[int, str]:
        weights = dict.fromkeys(default_urgency_weights(), 0) | {"report_late": points}
        total, band, _parts = score.urgency(
            visit(group="planned", end=days_ago(45)),
            scored(None),
            [],
            setting(urgency_weights=weights),
            TODAY,
        )
        return total, band

    assert [late(n) for n in (39, 40, 69, 70)] == [(39, ""), (40, "amber"), (69, "amber"), (70, "red")]


# ------------------------------------------------------------------------------------------ Q1 and PSEA
def q1(value: str, **kw) -> AnswerFacts:
    return answer("q1", rating=value if value in ("on_track", "constrained", "off_track") else "", **kw)


def test_the_q1_of_an_entity_is_its_own_then_its_partners_then_the_visits():
    entities = [
        entity(partner=1),
        entity(partner=1),
        entity(partner=2),
        entity("on_track", kind="partner", partner=3),
    ]
    answers = [
        q1("off_track", applies_to="entity", entity=0),
        q1("constrained", applies_to="partner", partner_id=1),
        q1("on_track", applies_to="visit"),
        q1("on_track", applies_to="visit", answered=False),  # unanswered: ignored
    ]
    assert score.effective_q1(entities, answers) == [
        ("off_track", "entity"),
        ("constrained", "partner"),
        ("on_track", "visit"),
        ("on_track", "visit"),
    ]
    assert score.effective_q1(entities, []) == [("", "")] * 4


def test_an_answer_on_a_partners_own_row_applies_to_its_other_rows():
    entities = [entity("on_track", kind="partner", partner=1), entity(partner=1)]
    answers = [q1("constrained", applies_to="entity", entity=0)]
    assert score.effective_q1(entities, answers) == [("constrained", "entity"), ("constrained", "partner")]


def test_the_q1_of_a_visit_is_the_worst_of_its_entities_and_visit_level_answers():
    assert (
        score.visit_q1(["on_track", "constrained", ""], [q1("on_track", applies_to="visit")]) == "constrained"
    )
    assert score.visit_q1(["on_track"], [q1("off_track", applies_to="visit")]) == "off_track"
    assert score.visit_q1(["", ""], []) == ""
    # a Q1 answered with something that is not a rating
    assert score.visit_q1([], [answer("q1", applies_to="visit")]) == "other"


def test_the_psea_flag():
    yes, no = answer("psea", answer_code="yes"), answer("psea", answer_code="no")
    flagging = ["yes", "constrained", "off_track"]
    assert score.psea_flag([yes, no], flagging) is True
    assert score.psea_flag([no], flagging) is False
    assert score.psea_flag([answer("psea", answered=False, answer_code="")], flagging) is False
    assert score.psea_flag([answer("q1", answer_code="yes")], flagging) is None  # no PSEA question
    assert score.psea_flag([no], ["no"]) is True  # follows the settings' flagging answers


@pytest.mark.django_db
def test_psea_follows_an_edit_of_the_flagging_answers(fm_world):
    from neurodb.fmm import refresh

    refresh.run(triggered_by="test", today=TODAY)
    flags = dict(Visit.objects.values_list("key", "psea_flag"))
    assert (flags["1726"], flags["1722"], flags["1727"]) == (True, False, None)  # Yes, No, not asked
    setting = ScoreSetting.load()
    setting.role_flag_answers = {"psea": ["no"]}
    setting.save()
    refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    flags = dict(Visit.objects.values_list("key", "psea_flag"))
    assert (flags["1726"], flags["1722"], flags["1727"]) == (False, True, None)


@pytest.mark.django_db
def test_the_built_visits_carry_their_effective_q1(fm_world):
    from neurodb.fmm import refresh
    from neurodb.fmm.models import VisitEntity

    refresh.run(triggered_by="test", today=TODAY)
    rows = {e.datamart_id: (e.hact_q1, e.hact_q1_from) for e in VisitEntity.objects.all()}
    assert rows[101] == ("on_track", "entity") and rows[102] == ("off_track", "entity")
    assert rows[103] == ("", "")  # the partner row of 1722: no Q1 of its own, of its partner or the visit
    assert rows[121] == rows[122] == ("on_track", "partner")  # 1726: once for the partner
    assert rows[131] == ("constrained", "visit")  # 1727: once for the visit, as an option code
    visits = dict(Visit.objects.values_list("key", "hact_q1"))
    assert (visits["1722"], visits["1723"], visits["1726"], visits["1727"]) == (
        "off_track",
        "constrained",
        "on_track",
        "constrained",
    )
    assert visits["1724"] == visits["1725"] == ""


@pytest.mark.django_db
def test_without_the_answer_keys_psea_is_not_known_and_the_answer_rules_are_not_available(
    fm_world, monkeypatch
):
    from neurodb.fmm import fields, refresh
    from neurodb.fmm.models import VisitRuleResult

    refresh.run(triggered_by="test", today=TODAY)
    assert dict(Visit.objects.values_list("key", "psea_flag"))["1722"] is False
    monkeypatch.setattr(fields, "available", lambda dataset, field: False)
    refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    flags = dict(Visit.objects.values_list("key", "psea_flag"))
    assert flags["1722"] is None and flags["1726"] is None  # not "not flagged"
    statuses = set(
        VisitRuleResult.objects.filter(
            rule__in=("R2", "R3", "R5"), visit__status_group="reported"
        ).values_list("status", flat=True)
    )
    assert statuses == {"na"}
