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
def test_version_1_holds_release_1s_defaults_and_version_2_fms_rules():
    v1, v2 = RuleSetVersion.objects.order_by("number")
    assert (v1.number, v1.note, v1.created_by, v1.created_by_name) == (
        1,
        "Defaults",
        None,
        "NeuroDB (default)",
    )
    assert {r["code"] for r in v1.snapshot["rules"]} == {"R1", "R2", "R3", "R4", "R5", "R6"}  # Release 1's
    assert (v2.number, v2.created_by, v2.created_by_name) == (2, None, "NeuroDB (default)")
    assert v2.note == "FMS Lebanon rule set (Release 2): 32 rules, six score categories"
    now = versions.snapshot_rules()
    assert v2.snapshot == now and versions.current_rules_version() == refresh.current_rules_version() == 2
    seeded, fresh = ScoreSetting.load(), ScoreSetting()
    for name in versions.SCORE_FIELDS:
        assert getattr(seeded, name) == getattr(fresh, name), name
    assert sum(r.deduction for r in RuleSetting.objects.filter(enabled=True)) == 100  # FMS's six weights


def test_the_default_settings_are_new_objects_for_every_row():
    a, b = ScoreSetting(), ScoreSetting()
    a.urgency_weights["quality_gap"] = 1
    a.scored_statuses.append("submitted")
    a.question_patterns["q1"].append("x")
    a.role_flag_answers["psea"].append("no")
    assert b.urgency_weights == default_urgency_weights() and b.urgency_weights["quality_gap"] == 0.5
    assert b.scored_statuses == ["report_finalization", "completed"]
    assert b.question_patterns == default_question_patterns()
    assert b.role_flag_answers == default_role_flag_answers()


# ------------------------------------------------------------------------------------------ saving
def test_a_rule_save_records_a_version_and_asks_for_a_rescore_on_commit(
    admin_user, started, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        RuleSetting.objects.filter(code="R2").update(deduction=6, updated_by=admin_user)
        version = versions.record_rules(admin_user, "Target lowered for the first quarter")
        versions.start_rescore(admin_user)
        assert started == []  # not before the save is committed
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    assert (version.number, version.note, version.created_by, version.created_by_name) == (
        3,
        "Target lowered for the first quarter",
        admin_user,
        "admin",
    )
    assert version.created_at is not None and version.restored_from is None
    assert {r["code"]: r["deduction"] for r in version.snapshot["rules"]}["R2"] == 6
    assert RefreshRequest.load().scores_requested_at is not None and RefreshRequest.load().requested_by == (
        f"admin:{admin_user.pk}"
    )
    assert versions.current_rules_version() == 3
    assert versions.saved_message(version) == (
        "Saved as rules v3. Scores will be recomputed in the background; the page shows "
        "'recomputing with rules v3' until they are."
    )


def test_the_visits_carry_the_rules_version_they_were_scored_with(fm_world, admin_user, started):
    refresh.run(triggered_by="test", today=TODAY)
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2} and not status.rescore_pending()
    RuleSetting.objects.filter(code="R1").update(enabled=False)
    versions.record_rules(admin_user, "Completeness off for a test")
    assert status.rescore_pending()  # until the rescore has run
    run = refresh.run(triggered_by=f"admin:{admin_user.pk}", scores_only=True, today=TODAY)
    assert run.details["rules_version"] == 3
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {3} and not status.rescore_pending()
    assert not VisitRuleResult.objects.filter(rule="R1").exists()  # a rule switched off keeps no row


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
                RuleSetting.objects.filter(code="R1").update(enabled=False)
                saved.append(versions.record_rules(admin_user, "Completeness off for a test"))
                versions.start_rescore(admin_user)
        return book

    monkeypatch.setattr(score.Rulebook, "load", staticmethod(load_then_save))
    last = refresh.run(triggered_by="test", today=TODAY)
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    assert (last.target, last.details["rules_version"]) == ("scores", 3)
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {3} and not status.rescore_pending()
    assert not VisitRuleResult.objects.filter(rule="R1").exists()  # a rule switched off keeps no row


# ------------------------------------------------------------------------------------------ restoring
def test_restore_writes_a_version_back_as_a_new_one(admin_user, started, django_capture_on_commit_callbacks):
    v1, v2 = RuleSetVersion.objects.order_by("number")
    RuleSetting.objects.filter(code="R2").update(deduction=6, enabled=False)
    ScoreSetting.objects.filter(pk=1).update(urgency_red=90)
    v3 = versions.record_rules(admin_user, "Experiment")
    assert [r["changed"] for r in versions.differences(v2.snapshot) if r["changed"]]
    with django_capture_on_commit_callbacks(execute=True):
        v4 = versions.restore_rules(v2, admin_user, "Back to the defaults")
    assert (v4.number, v4.note, v4.restored_from) == (4, "Restored v2: Back to the defaults", v2)
    rule = RuleSetting.objects.get(code="R2")
    assert (rule.deduction, rule.enabled, rule.updated_by) == (Decimal(8), True, admin_user)
    assert ScoreSetting.load().urgency_red == 70
    assert not [r for r in versions.differences(v2.snapshot) if r["changed"]]
    assert v4.snapshot["rules"] == v2.snapshot["rules"]
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    # the history only grows: v3 is still there, and can be restored in turn
    assert list(RuleSetVersion.objects.values_list("number", flat=True)) == [4, 3, 2, 1]
    assert versions.restore_rules(v3, admin_user).note == "Restored v3"
    # version 1 holds Release 1's rules (R1-R6, points and thresholds): restoring it keeps FMS's rules and
    # the settings it does not hold
    before = versions.snapshot_rules()["rules"]
    versions.restore_rules(v1, admin_user, "Release 1")
    assert versions.snapshot_rules()["rules"] == before
    assert ScoreSetting.load().urgency_weights == default_urgency_weights()


def test_restoring_writes_the_pinned_keys_back_and_rebuilds_only_when_they_differ(
    fm_world, admin_user, started, django_capture_on_commit_callbacks
):
    refresh.run(triggered_by="test", probe_only=True)
    v1 = RuleSetVersion.objects.get(number=2)
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
    assert (version.number, version.note) == (3, "PSEA pattern set from Questions found")
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
    assert RuleSetVersion.objects.count() == 2  # nothing was recorded
    with pytest.raises(ValueError):
        versions.pin_question("q9", OTHER_TEXT, admin_user)


# ------------------------------------------------------------------------------------------ preview
def test_the_preview_shows_the_effect_and_writes_nothing(fm_world):
    refresh.run(triggered_by="test", today=TODAY)
    before = (
        list(Visit.objects.order_by("key").values_list("key", "quality_score", "flags", "urgency")),
        sorted(VisitRuleResult.objects.values_list("visit__key", "rule", "status", "points")),
    )
    rule = RuleSetting.objects.get(code="R2")
    scoring = [{"min": 30, "deduction": 0}, {"min": 0, "deduction": 10}]
    result = versions.preview({"R2": {"params": {**rule.params, "scoring": scoring}}}, {}, **YEAR)
    # counted per record: 1723's SSFA answered 33.3% (no longer flagged), its partner's record 0%
    assert result["rules"]["R2"] == {"now": 2, "then": 1}
    assert result["rules"]["R1"] == {"now": 8, "then": 8}
    assert (result["visits"], result["records"]) == (8, 12)
    assert result["scored"] == {"now": 10, "then": 10}
    assert result["avg_quality"]["then"] > result["avg_quality"]["now"]
    sentence = versions.describe(result, ["R2"])
    assert sentence.startswith("R2 would flag 1 record (now 2); average quality ")
    assert sentence.endswith("; scored records 10 (now 10)")
    after = (
        list(Visit.objects.order_by("key").values_list("key", "quality_score", "flags", "urgency")),
        sorted(VisitRuleResult.objects.values_list("visit__key", "rule", "status", "points")),
    )
    assert after == before and RuleSetVersion.objects.count() == 2
    assert RuleSetting.objects.get(code="R2").params["scoring"][0] == {"min": 80, "deduction": 0}
    # a score setting change: a category's weight caps its deductions
    categories = [
        {**c, "weight": 5 if c["key"] == "completeness" else c["weight"]}
        for c in ScoreSetting.load().categories
    ]
    result = versions.preview({}, {"categories": categories}, **YEAR)
    assert result["avg_quality"]["then"] > result["avg_quality"]["now"]


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
        ("R1", {"params": {"fields": []}}, "params", "lists its fields"),
        (
            "R1",
            {"params": {"fields": [{"name": "photos", "deduction": 2}]}},
            "params",
            "a name of the report",
        ),
        ("R1", {"params": {"fields": [{"name": "entity", "deduction": 200}]}}, "params", "0 to 100"),
        ("R2", {"params": {"field": "fmq_answered_pct", "scoring": []}}, "params", "at least one band"),
        (
            "R2",
            {"params": {"field": "nothing", "scoring": [{"min": 1, "deduction": 1}]}},
            "params",
            "names a column",
        ),
        (
            "R2",
            {"params": {"field": "method_count", "scoring": [{"min": 1, "points": 1}]}},
            "params",
            "Each band",
        ),
        (
            "R2",
            {"params": {"field": "method_count", "field_type": "text", "scoring": [{"deduction": 1}]}},
            "params",
            "numeric or list",
        ),
        (
            "R2",
            {
                "params": {
                    "field": "method_count",
                    "scoring": [{"deduction": 1}],
                    "missing_value_deduction": -1,
                }
            },
            "params",
            "0 to 100",
        ),
        ("R3", {"params": {"fields": ["narrative_finding"], "strict": True}}, "params", "has no"),
        ("R3", {"params": {"fields": []}}, "params", "lists the fields"),
        (
            "R3",
            {"params": {"fields": ["narrative_finding"], "ai_prompt_key": "Not A Key"}},
            "params",
            "lower case",
        ),
        ("R20", {"params": {"check_type": "guess"}}, "params", "check_type"),
        (
            "R20",
            {"params": {"check_type": "value_in_mapped_list", "reference_map": ["x"]}},
            "params",
            "maps each key",
        ),
        ("R24", {"params": {"check_type": "string_contains"}}, "params", "needs the text"),
        (
            "R20",
            {"params": {"check_type": "value_in_mapped_list", "entity_type_filter": ["Donor"]}},
            "params",
            "Partner, CP Output",
        ),
        ("R20", {"params": ["x"]}, "params", "must be an object"),
        ("R20", {"category": "Not a key"}, "category", "lower case"),
        ("R20", {"category": "red_flags"}, "category", "not one of the score categories"),
        ("R20", {"type": "guess"}, "type", "one of"),
    ],
)
def test_each_rule_setting_is_checked_with_a_readable_error(code, values, field, words):
    errors = _rule_errors(code, **values)
    assert any(words in message for message in errors[field]), errors


def test_a_rules_deduction_follows_its_fields_or_bands():
    r1 = RuleSetting.objects.get(code="R1")
    r1.params = {
        "fields": [
            {"name": "entity", "label": "Entity", "deduction": 4},
            {"name": "objective", "deduction": 1.5},
        ]
    }
    r1.clean()
    assert r1.deduction == Decimal("5.5")
    r31 = RuleSetting.objects.get(code="R31")
    r31.deduction = Decimal(0)
    r31.clean()
    assert r31.deduction == 8  # no deduction given: its largest band


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
        ({"categories": []}, "categories", "List 1 to"),
        ({"categories": [{"key": "completeness", "weight": 90}]}, "categories", "add up to 100"),
        ({"categories": [{"key": "Bad Key", "weight": 100}]}, "categories", "lower case"),
        (
            {"categories": [{"key": "x", "weight": 100, "colour": "red"}]},
            "categories",
            "a key, a label and a weight",
        ),
        (
            {"categories": [{"key": "completeness", "weight": 50}, {"key": "evidence", "weight": 50}]},
            "categories",
            "Rules that are on use a category left out",
        ),
        (
            {"urgency_weights": {**default_urgency_weights(), "mood": 3}},
            "urgency_weights",
            "Unknown weights: mood",
        ),
        ({"urgency_weights": {"quality_gap": 1}}, "urgency_weights", "Missing weights"),
        ({"urgency_weights": {**default_urgency_weights(), "recency": 1.5}}, "urgency_weights", "0 to 1"),
        ({"urgency_weights": {**default_urgency_weights(), "recency": "0.3"}}, "urgency_weights", "0 to 1"),
        (
            {"urgency_weights": {**default_urgency_weights(), "recency": 0.4}},
            "urgency_weights",
            "add up to 1",
        ),
        ({"scored_statuses": []}, "scored_statuses", "at least one status"),
        ({"scored_statuses": ["completed", "cancelled"]}, "scored_statuses", "cancelled"),
        ({"scored_statuses": ["done"]}, "scored_statuses", "Unknown"),
        ({"recency_days": 0}, "recency_days", "from 1 to 3,650"),
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
