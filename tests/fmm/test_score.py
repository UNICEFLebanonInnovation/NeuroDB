"""Scoring a visit (``fmm.score``): FMS's score (100 less the deductions, each category's at most its
weight), provisional visits, bands and flags; urgency and its worked examples; HACT Q1 (own,
partner-level, visit-level) and the PSEA flag."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from neurodb.fmm import score
from neurodb.fmm.build import ActionPointFacts
from neurodb.fmm.models import ScoreSetting, Visit, default_urgency_weights
from neurodb.fmm.rules import AnswerFacts, Outcome
from neurodb.fmm.score import ScoreOutcome

from .test_rules import answer, entity

TODAY = datetime.date(2026, 10, 5)


def setting(**changes) -> ScoreSetting:
    return ScoreSetting(**changes)  # the defaults, not saved


def outcome(
    rule: str, status: str, deduction: float = 0, category: str = "completeness", top: float = 20
) -> Outcome:
    return Outcome(rule, status, Decimal(str(deduction)), Decimal(str(top)), category=category)


# ------------------------------------------------------------------------------------------ score
def test_the_score_is_100_less_the_deductions_each_category_at_most_its_weight():
    outcomes = [
        outcome("R1", "fail", 11, "completeness"),
        outcome("R2", "fail", 10, "completeness"),
        outcome("R20", "fail", 15, "completeness"),  # completeness: 36 taken, at most 30
        outcome("R3", "fail", 20, "evidence"),
        outcome("R5", "pass", 0, "alignment"),
        outcome("R6", "na", 0, "coherence"),
        outcome("R7", "off", 0, "q3_quality"),
    ]
    result = score.score_visit(outcomes, setting())
    assert result.score == Decimal("50.0")  # 100 - 30 - 20
    assert result.deductions == {"completeness": 30.0, "evidence": 20.0}
    assert (result.flags, result.evaluated) == (("R1", "R2", "R20", "R3"), ("R1", "R2", "R20", "R3", "R5"))
    assert (result.band, result.not_scored_reason, result.pending) == ("medium", "", 0)


def test_the_score_never_goes_below_zero_and_a_flag_without_points_still_flags():
    every = [outcome(f"R{n}", "fail", 40, f"c{n}") for n in range(1, 5)]  # categories without a weight
    assert score.score_visit(every, setting()).score == Decimal("0.0")
    result = score.score_visit([outcome("R23", "fail", 0, "completeness", 0)], setting())
    assert (result.score, result.flags) == (Decimal("100.0"), ("R23",))


def test_a_visit_with_ai_checks_pending_is_provisional_and_not_scored():
    outcomes = [outcome("R1", "fail", 2), outcome("R3", "pending", 0, "evidence"), outcome("R5", "pending")]
    result = score.score_visit(outcomes, setting())
    assert (result.score, result.provisional, result.pending) == (None, Decimal("98.0"), 2)
    assert (result.band, result.not_scored_reason) == ("", "provisional: 2 AI checks pending")
    assert result.flags == ("R1",)  # the flags found so far stay


def test_visits_in_other_statuses_are_pending():
    planned = score.score_visit([], setting(), False, "planned")
    assert (planned.score, planned.band, planned.not_scored_reason) == (None, "pending", score.PENDING)
    cancelled = score.score_visit([], setting(), False, "cancelled")
    assert (cancelled.band, cancelled.not_scored_reason) == ("", "cancelled")


def test_scorable_visits_follow_the_scored_statuses():
    scored_statuses = score.scored_statuses_of(setting())
    assert scored_statuses == {"report_finalization", "completed"}  # FMS's default
    assert score.scorable("completed", scored_statuses) and score.scorable(
        "report_finalization", scored_statuses
    )
    for status in ("submitted", "review", "data_collection", "assigned", "cancelled", ""):
        assert not score.scorable(status, scored_statuses), status
    assert score.scorable("submitted", score.scored_statuses_of(setting(scored_statuses=["submitted"])))
    # a setting that holds nothing usable falls back to the defaults
    assert score.scored_statuses_of(setting(scored_statuses=[])) == scored_statuses


@pytest.mark.parametrize(
    ("taken", "band"),
    [(Decimal(20), "high"), (Decimal("20.1"), "medium"), (Decimal(50), "medium"), (Decimal("50.1"), "low")],
)
def test_bands_at_their_edges(taken, band):
    result = score.score_visit([outcome("R1", "fail", taken, "x", 100)], setting())
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
    return ScoreOutcome(value, None, 0, {}, (), "", tuple(f"R{n}" for n in range(1, flags + 1)), "")


def days_ago(n: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=n)


def open_point(due=None, high=False) -> ActionPointFacts:
    return ActionPointFacts(1, "open", due, high)


@pytest.mark.parametrize(
    ("case", "args", "total", "band", "parts"),
    [
        (
            "score 70, ended today, 1 flag: 0.5 × 30 + 0.3 × 100 + 0.2 × 25",
            (visit(end=TODAY), scored(70, 1)),
            50,
            "amber",
            {"quality_gap": 15.0, "recency": 30.0, "red_flags": 5.0},
        ),
        (
            "score 40, ended 90 days ago (recency 50), 3 flags",
            (visit(end=days_ago(90)), scored(40, 3)),
            60,
            "amber",
            {"quality_gap": 30.0, "recency": 15.0, "red_flags": 15.0},
        ),
        (
            "score 20, ended 10 days ago, 4 flags (flags at most 100)",
            (visit(end=days_ago(10)), scored(20, 4)),
            88,
            "red",
            {"quality_gap": 40.0, "recency": 28.3, "red_flags": 20.0},
        ),
        (
            "score 100, ended 200 days ago, no flag",
            (visit(end=days_ago(200)), scored(100)),
            0,
            "",
            {"quality_gap": 0.0, "recency": 0.0, "red_flags": 0.0},
        ),
        (
            "a visit that ends later counts as recent",
            (visit(end=days_ago(-10)), scored(100)),
            30,
            "",
            {"quality_gap": 0.0, "recency": 30.0, "red_flags": 0.0},
        ),
        ("no score: no urgency", (visit(end=days_ago(5)), scored(None, 2)), None, "", {}),
    ],
)
def test_urgency_worked_examples(case, args, total, band, parts):
    assert score.urgency(*args, setting(), TODAY) == (total, band, parts), case


def test_recency_falls_from_100_to_0_over_the_window():
    assert score.recency(TODAY, TODAY, 180) == 100
    assert score.recency(days_ago(45), TODAY, 180) == 75
    assert score.recency(days_ago(180), TODAY, 180) == 0 and score.recency(days_ago(400), TODAY, 180) == 0
    assert score.recency(days_ago(45), TODAY, 90) == 50
    assert score.recency(None, TODAY, 180) == 0


def test_urgency_weights_are_editable_and_bands_follow_the_settings():
    weights = {"quality_gap": 0.6, "recency": 0.2, "red_flags": 0.2}
    assert score.urgency(visit(end=TODAY), scored(70, 1), setting(urgency_weights=weights), TODAY)[0] == 43

    def by_gap(value: str) -> tuple[int, str]:
        only_gap = {"quality_gap": 1, "recency": 0, "red_flags": 0}
        total, band, _parts = score.urgency(
            visit(end=days_ago(400)), scored(value), setting(urgency_weights=only_gap), TODAY
        )
        return total, band

    assert [by_gap(s) for s in ("61", "60", "31", "30")] == [
        (39, ""),
        (40, "amber"),
        (69, "amber"),
        (70, "red"),
    ]
    assert by_gap("30.5") == (70, "red")  # 69.5, half up
    stricter = setting(urgency_weights={"quality_gap": 1, "recency": 0, "red_flags": 0}, urgency_red=80)
    assert score.urgency(visit(end=days_ago(400)), scored(25), stricter, TODAY)[1] == "amber"


def test_invalid_weights_fall_back_to_fms_defaults():
    release_1 = {"off_track": 40, "quality_gap": 25}
    assert score.weights_of(setting(urgency_weights=release_1)) == default_urgency_weights()
    assert score.weights_of(
        setting(urgency_weights={"quality_gap": 0.5, "recency": 0.5, "red_flags": 0.5})
    ) == (default_urgency_weights())
    assert score.valid_weights({"quality_gap": 0.2, "recency": 0.3, "red_flags": 0.5})
    assert not score.valid_weights({"quality_gap": True, "recency": 0, "red_flags": 0})


def test_signals_are_kept_apart_from_urgency():
    off = visit("off_track", end=days_ago(20))
    assert score.signals(off, [], setting(), TODAY) == {"no_follow_up": True}
    overdue_high = [open_point(days_ago(3), high=True)]
    assert score.signals(off, overdue_high, setting(), TODAY) == {"ap_overdue": 1, "ap_high_overdue": 1}
    high_open = [open_point(days_ago(-3), high=True)]
    assert score.signals(off, high_open, setting(), TODAY) == {"ap_high_open": 1}
    done = [ActionPointFacts(1, "completed", days_ago(3), True)]  # closed: a follow-up, nothing open
    assert score.signals(off, done, setting(), TODAY) == {}
    # within the 14 days, no follow-up is expected yet; HACT Q1 constrained counts like the rating
    assert score.signals(visit("off_track", end=days_ago(14)), [], setting(), TODAY) == {}
    assert score.signals(visit("on_track", "constrained", end=days_ago(30)), [], setting(), TODAY) == {
        "no_follow_up": True
    }
    late = visit("not_monitored", group="in_progress", end=days_ago(45))
    assert score.signals(late, [], setting(), TODAY) == {"report_late_days": 45}
    assert score.signals(late, [], setting(report_late_days=60), TODAY) == {}
    # none of it moves urgency
    assert score.urgency(off, scored(70, 1), setting(), TODAY) == score.urgency(
        visit(end=days_ago(20)), scored(70, 1), setting(), TODAY
    )


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
def test_without_the_answer_keys_psea_is_not_known_and_q1_q2_are_not_counted_missing(fm_world, monkeypatch):
    from neurodb.fmm import fields, refresh
    from neurodb.fmm.models import RecordRuleResult

    refresh.run(triggered_by="test", today=TODAY)
    assert dict(Visit.objects.values_list("key", "psea_flag"))["1722"] is False
    monkeypatch.setattr(fields, "available", lambda dataset, field: False)
    refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    flags = dict(Visit.objects.values_list("key", "psea_flag"))
    assert flags["1722"] is None and flags["1726"] is None  # not "not flagged"
    # Q1 and Q2 cannot be read: R1 does not count them missing
    details = RecordRuleResult.objects.filter(rule="R1").values_list("detail", flat=True)
    assert details and not any("Q1 –" in d or "Q2 –" in d for d in details)
