"""Versions of the quality rules (``fmm.versions``): every save is a new version with who, when and why,
and asks for a background rescore (varargs, on commit); a restore writes a version back as a new one;
the settings are checked with readable errors; the preview scores in memory and writes nothing."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from neurodb.fmm import refresh, rules, status, versions
from neurodb.fmm.models import (
    FieldMapping,
    RefreshRequest,
    RuleSetting,
    RuleSetVersion,
    ScoreSetting,
    Visit,
    VisitRuleResult,
    default_question_patterns,
    default_role_flag_answers,
    default_urgency_weights,
)
from neurodb.integrations import background

from .conftest import OTHER_TEXT, PSEA_TEXT, Q1_TEXT

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
YEAR = {"start": datetime.date(2026, 1, 1), "end": datetime.date(2026, 12, 31)}


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


# ------------------------------------------------------------------------------------------ the seed
def test_version_1_holds_the_defaults():
    v1 = RuleSetVersion.objects.get()
    assert (v1.number, v1.note, v1.created_by, v1.created_by_name) == (
        1,
        "Defaults",
        None,
        "NeuroDB (default)",
    )
    assert v1.snapshot == versions.snapshot_rules()
    assert (
        v1.snapshot["mappings"] == {}
        and versions.current_rules_version() == refresh.current_rules_version() == 1
    )
    for row in RuleSetting.objects.all():  # the migration's literals are the code's defaults
        default = rules.DEFAULTS[row.code]
        assert (row.label, row.points, row.params, row.description, row.enabled) == (
            default["label"],
            default["points"],
            default["params"],
            default["description"],
            True,
        )
        assert row.threshold == (None if default["threshold"] is None else Decimal(default["threshold"]))
    assert sum(r.points for r in RuleSetting.objects.all()) == 85  # R6 is a flag only (0 points)
    seeded, fresh = ScoreSetting.load(), ScoreSetting()
    for name in versions.SCORE_FIELDS:
        assert getattr(seeded, name) == getattr(fresh, name), name


def test_the_default_settings_are_new_objects_for_every_row():
    a, b = ScoreSetting(), ScoreSetting()
    a.urgency_weights["off_track"] = 1
    a.question_patterns["q1"].append("x")
    a.role_flag_answers["psea"].append("no")
    assert b.urgency_weights == default_urgency_weights() and b.urgency_weights["off_track"] == 40
    assert b.question_patterns == default_question_patterns()
    assert b.role_flag_answers == default_role_flag_answers()


# ------------------------------------------------------------------------------------------ saving
def test_a_rule_save_records_a_version_and_asks_for_a_rescore_on_commit(
    admin_user, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        RuleSetting.objects.filter(code="R2").update(threshold=60, updated_by=admin_user)
        version = versions.record_rules(admin_user, "Target lowered for the first quarter")
        versions.start_rescore(admin_user)
        assert started == []  # not before the save is committed
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    assert (version.number, version.note, version.created_by, version.created_by_name) == (
        2,
        "Target lowered for the first quarter",
        admin_user,
        "admin",
    )
    assert version.created_at is not None and version.restored_from is None
    assert {r["code"]: r["threshold"] for r in version.snapshot["rules"]}["R2"] == 60
    assert RefreshRequest.load().scores_requested_at is not None and RefreshRequest.load().requested_by == (
        f"admin:{admin_user.pk}"
    )
    assert versions.current_rules_version() == 2
    assert versions.saved_message(version) == (
        "Saved as rules v2. Scores will be recomputed in the background; the page shows "
        "'recomputing with rules v2' until they are."
    )


def test_the_visits_carry_the_rules_version_they_were_scored_with(fm_world, admin_user, started):
    refresh.run(triggered_by="test", today=TODAY)
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {1} and not status.rescore_pending()
    RuleSetting.objects.filter(code="R4").update(threshold=5)
    versions.record_rules(admin_user, "Shorter narratives accepted")
    assert status.rescore_pending()  # until the rescore has run
    run = refresh.run(triggered_by=f"admin:{admin_user.pk}", scores_only=True, today=TODAY)
    assert run.details["rules_version"] == 2
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2} and not status.rescore_pending()
    assert VisitRuleResult.objects.get(visit__key="1726", rule="R4").status == "pass"  # 8 words, minimum 5


def test_a_rule_saved_while_a_refresh_holds_the_lock_ends_on_the_new_version(
    fm_world, admin_user, monkeypatch, started, django_capture_on_commit_callbacks
):
    """The save's own background command finds the lock held and stops; the refresh that holds it sees
    the request before it lets go and recomputes the scores: every visit ends on the new version."""
    from neurodb.fmm import score

    load, saved = score.Rulebook.load, []

    def load_then_save():
        book = load()
        if not saved:  # an administrator saves while this pass scores
            with django_capture_on_commit_callbacks(execute=True):
                RuleSetting.objects.filter(code="R4").update(threshold=5)
                saved.append(versions.record_rules(admin_user, "Shorter narratives accepted"))
                versions.start_rescore(admin_user)
        return book

    monkeypatch.setattr(score.Rulebook, "load", staticmethod(load_then_save))
    last = refresh.run(triggered_by="test", today=TODAY)
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    assert (last.target, last.details["rules_version"]) == ("scores", 2)
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2} and not status.rescore_pending()
    assert VisitRuleResult.objects.get(visit__key="1726", rule="R4").status == "pass"


# ------------------------------------------------------------------------------------------ restoring
def test_restore_writes_a_version_back_as_a_new_one(admin_user, started, django_capture_on_commit_callbacks):
    v1 = RuleSetVersion.objects.get(number=1)
    RuleSetting.objects.filter(code="R2").update(threshold=60, enabled=False)
    ScoreSetting.objects.filter(pk=1).update(urgency_red=90)
    v2 = versions.record_rules(admin_user, "Experiment")
    assert [r["changed"] for r in versions.differences(v1.snapshot) if r["changed"]]
    with django_capture_on_commit_callbacks(execute=True):
        v3 = versions.restore_rules(v1, admin_user, "Back to the defaults")
    assert (v3.number, v3.note, v3.restored_from) == (3, "Restored v1: Back to the defaults", v1)
    rule = RuleSetting.objects.get(code="R2")
    assert (rule.threshold, rule.enabled, rule.updated_by) == (Decimal(80), True, admin_user)
    assert ScoreSetting.load().urgency_red == 70
    assert v3.snapshot == v1.snapshot and not [r for r in versions.differences(v1.snapshot) if r["changed"]]
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    # the history only grows: v2 is still there, and can be restored in turn
    assert list(RuleSetVersion.objects.values_list("number", flat=True)) == [3, 2, 1]
    assert versions.restore_rules(v2, admin_user).note == "Restored v2"


def test_restoring_writes_the_pinned_keys_back_and_rebuilds_only_when_they_differ(
    fm_world, admin_user, started, django_capture_on_commit_callbacks
):
    refresh.run(triggered_by="test", probe_only=True)
    v1 = RuleSetVersion.objects.get(number=1)
    FieldMapping.objects.filter(dataset="fm_questions", field="answer").update(override_key="summary")
    v2 = versions.record_rules(admin_user, "Field override: fm_questions.answer → summary: test")
    assert v2.snapshot["mappings"] == {"fm_questions.answer": "summary"}
    rows = {(r["group"], r["name"]): r for r in versions.differences(v1.snapshot)}
    assert rows[("Pinned keys", "fm_questions.answer")] == {
        "group": "Pinned keys",
        "name": "fm_questions.answer",
        "then": "auto",
        "now": "summary",
        "changed": True,
    }
    with django_capture_on_commit_callbacks(execute=True):
        versions.restore_rules(v1, admin_user, "unpin")
    assert FieldMapping.objects.get(dataset="fm_questions", field="answer").override_key == ""
    assert started[-1] == ("fmm_refresh", "--triggered-by", f"admin:{admin_user.pk}")  # a full refresh
    assert RefreshRequest.load().full_requested_at is not None
    with django_capture_on_commit_callbacks(execute=True):
        versions.restore_rules(v1, admin_user, "again")  # the keys are already those of v1
    assert started[-1] == ("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")
    with django_capture_on_commit_callbacks(execute=True):
        versions.restore_rules(v2, admin_user, "pin again")
    assert FieldMapping.objects.get(dataset="fm_questions", field="answer").override_key == "summary"
    assert started[-1] == ("fmm_refresh", "--triggered-by", f"admin:{admin_user.pk}")


# ------------------------------------------------------------------------------------------ question roles
def test_a_question_given_a_role_from_questions_found(
    admin_user, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        version = versions.pin_question("psea", OTHER_TEXT, admin_user)
    patterns = ScoreSetting.load().question_patterns
    assert patterns["psea"][-1] == "=are attendance registers kept up to date"
    assert rules.assign_roles(OTHER_TEXT, False, patterns) == "psea"
    assert (version.number, version.note) == (2, "PSEA pattern set from Questions found")
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    # given another role, it leaves the first one
    versions.pin_question("q2", OTHER_TEXT, admin_user)
    patterns = ScoreSetting.load().question_patterns
    assert "=are attendance registers kept up to date" not in patterns["psea"]
    assert rules.assign_roles(OTHER_TEXT, False, patterns) == "q2"
    # a whole text pinned to a role wins over a broader pattern of another role
    versions.pin_question("q3", Q1_TEXT, admin_user)
    assert rules.assign_roles(Q1_TEXT, True, ScoreSetting.load().question_patterns) == "q3"


def test_a_long_question_is_pinned_by_its_first_words_and_the_role_limit_holds(admin_user, started):
    long_text = PSEA_TEXT + " " + "Please describe what was observed in detail. " * 6
    pattern = versions.question_pattern(long_text)
    assert pattern.startswith("^psea were any protection") and len(pattern) <= 200
    assert rules.matches(rules.parse.fold(long_text), pattern)
    ScoreSetting.objects.filter(pk=1).update(question_patterns={"q1": [f"question {n}" for n in range(20)]})
    with pytest.raises(ValidationError, match="at most 20"):
        versions.pin_question("q1", OTHER_TEXT, admin_user)
    assert RuleSetVersion.objects.count() == 1  # nothing was recorded
    with pytest.raises(ValueError):
        versions.pin_question("q9", OTHER_TEXT, admin_user)


# ------------------------------------------------------------------------------------------ preview
def test_the_preview_shows_the_effect_and_writes_nothing(fm_world):
    refresh.run(triggered_by="test", today=TODAY)
    before = (
        list(Visit.objects.order_by("key").values_list("key", "quality_score", "flags", "urgency")),
        sorted(VisitRuleResult.objects.values_list("visit__key", "rule", "status", "points")),
    )
    rule = RuleSetting.objects.get(code="R4")
    result = versions.preview(
        {"R4": {"threshold": Decimal(5), "enabled": True, "points": 15, "params": rule.params}}, {}, **YEAR
    )
    assert result["rules"]["R4"] == {"now": 2, "then": 0}  # 1722 and 1726 have narratives under 25 words
    assert result["rules"]["R2"] == {"now": 1, "then": 1} and result["visits"] == 8
    assert result["scored"] == {"now": 6, "then": 6}
    assert result["avg_quality"]["then"] > result["avg_quality"]["now"]
    sentence = versions.describe(result, ["R4"])
    assert sentence.startswith("R4 would flag 0 visits (now 2); average quality ")
    assert sentence.endswith("; scored visits 6 (now 6)")
    after = (
        list(Visit.objects.order_by("key").values_list("key", "quality_score", "flags", "urgency")),
        sorted(VisitRuleResult.objects.values_list("visit__key", "rule", "status", "points")),
    )
    assert after == before and RuleSetVersion.objects.count() == 1
    assert RuleSetting.objects.get(code="R4").threshold == 25
    # a score setting change: no band, no flag moves with the urgency bands
    result = versions.preview({}, {"min_evaluated_points": 100}, **YEAR)
    assert result["scored"] == {"now": 6, "then": 0}


# ------------------------------------------------------------------------------------------ validation
def _rule_errors(code: str, **values) -> dict[str, list[str]]:
    rule = RuleSetting.objects.get(code=code)
    for name, value in values.items():
        setattr(rule, name, value)
    with pytest.raises(ValidationError) as caught:
        rule.clean()
    return caught.value.message_dict


@pytest.mark.parametrize(
    ("code", "values", "field", "words"),
    [
        ("R1", {"params": {"required": []}}, "params", "at least one of"),
        ("R1", {"params": {"required": ["narrative", "photos"]}}, "params", "not photos"),
        ("R1", {"threshold": Decimal(5)}, "threshold", "uses no threshold"),
        ("R2", {"threshold": Decimal(0)}, "threshold", "from 1 to 100"),
        ("R2", {"threshold": Decimal(101)}, "threshold", "from 1 to 100"),
        ("R2", {"threshold": None}, "threshold", "needs a threshold"),
        ("R2", {"params": {"require_unanswered_seen": "yes"}}, "params", "true or false"),
        ("R3", {"params": {"strict": "no"}}, "params", "true or false"),
        ("R4", {"threshold": Decimal(501)}, "threshold", "from 1 to 500"),
        ("R4", {"threshold": Decimal("2.5")}, "threshold", "whole number"),
        ("R4", {"params": {"placeholders": "n/a"}}, "params", "must be a list"),
        ("R4", {"params": {"check_copies": 1}}, "params", "true or false"),
        ("R4", {"params": {"copy_window_days": 1096}}, "params", "from 1 to 1095"),
        ("R4", {"params": {"copy_window_days": 0}}, "params", "from 1 to 1095"),
        ("R5", {"params": {"placeholders": ["x" * 81]}}, "params", "1 to 80 characters"),
        ("R5", {"params": {"placeholders": ["ok", 3]}}, "params", "must be a list"),
        ("R6", {"threshold": Decimal(11)}, "threshold", "from 1 to 10"),
        ("R6", {"params": {"negative_cues": [f"cue {n}" for n in range(61)]}}, "params", "at most 60"),
        ("R6", {"params": {"negation_window": 6}}, "params", "from 0 to 5"),
        ("R6", {"params": {"check_not_monitored": "true"}}, "params", "true or false"),
        ("R6", {"params": {"positive_cues": ["..."]}}, "params", "no letter or digit"),
        ("R6", {"params": {"tone": "harsh"}}, "params", "has no setting"),
        ("R6", {"params": ["delayed"]}, "params", "must be an object"),
        ("R3", {"points": 51}, "points", "from 0 to 50"),
    ],
)
def test_each_rule_setting_is_checked_with_a_readable_error(code, values, field, words):
    errors = _rule_errors(code, **values)
    assert any(words in message for message in errors[field]), errors


def test_lists_are_stored_folded_and_without_repeats():
    rule = RuleSetting.objects.get(code="R4")
    rule.params = {"placeholders": ["N/A", "n/a", "See Above!", "Très bien"], "check_copies": False}
    rule.clean()
    assert rule.params == {"placeholders": ["n a", "see above", "tres bien"], "check_copies": False}
    r1 = RuleSetting.objects.get(code="R1")
    r1.params = {"required": ["location", "narrative"]}
    r1.clean()
    assert r1.params == {"required": ["narrative", "location"]}


def _score_errors(**values) -> dict[str, list[str]]:
    setting = ScoreSetting.load()
    for name, value in values.items():
        setattr(setting, name, value)
    with pytest.raises(ValidationError) as caught:
        setting.clean()
    return caught.value.message_dict


@pytest.mark.parametrize(
    ("values", "field", "words"),
    [
        ({"urgency_amber": 70}, "urgency_red", "Amber must be below red"),
        ({"urgency_red": 101}, "urgency_red", "at most 100"),
        ({"band_medium": 80}, "band_high", "must start below the High band"),
        ({"band_high": 101}, "band_high", "at most 100"),
        ({"min_evaluated_points": 101}, "min_evaluated_points", "from 0 to 100"),
        (
            {"urgency_weights": {**default_urgency_weights(), "mood": 3}},
            "urgency_weights",
            "Unknown weights: mood",
        ),
        ({"urgency_weights": {"off_track": 40}}, "urgency_weights", "Missing weights"),
        ({"urgency_weights": {**default_urgency_weights(), "per_flag": 101}}, "urgency_weights", "0 to 100"),
        ({"urgency_weights": {**default_urgency_weights(), "per_flag": "5"}}, "urgency_weights", "0 to 100"),
        ({"question_patterns": {"q7": ["x"]}}, "question_patterns", "Unknown role"),
        ({"question_patterns": {"q1": "implemented"}}, "question_patterns", "must be a list"),
        ({"question_patterns": {"q1": ["x"]}}, "question_patterns", "2 to 200 characters"),
        ({"question_patterns": {"q1": ["y" * 201]}}, "question_patterns", "2 to 200 characters"),
        ({"question_patterns": {"q1": [f"p{n}" for n in range(21)]}}, "question_patterns", "at most 20"),
        ({"role_flag_answers": {"psea": ["maybe"]}}, "role_flag_answers", "can only be"),
        ({"role_flag_answers": {"q9": ["yes"]}}, "role_flag_answers", "Unknown role"),
    ],
)
def test_the_score_settings_are_checked_with_a_readable_error(values, field, words):
    errors = _score_errors(**values)
    assert any(words in message for message in errors[field]), errors


def test_question_patterns_are_stored_folded_with_their_prefix():
    setting = ScoreSetting.load()
    setting.question_patterns = {
        "q2": ["^Q2 –", "Activités monitored", "^Q2 –"],
        "q1": ["=Have the Activities?"],
    }
    setting.clean()
    assert setting.question_patterns == {"q2": ["^q2", "activites monitored"], "q1": ["=have the activities"]}
