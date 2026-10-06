"""The quality rules (``fmm.rules``), FMS's model: completeness, deterministic (scoring bands),
AI checks and reference checks; the entity type filter; the flags they write; the checks of a rule's
settings; Rebalance; and the question roles (Q1, Q2, Q3, PSEA)."""

from __future__ import annotations

import copy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from neurodb.fmm import lebanon, rules, score
from neurodb.fmm.models import RuleSetting
from neurodb.fmm.rules import AnswerFacts, Context, Record

from .conftest import Q1_TEXT, Q2_TEXT, Q3_TEXT

FMS = {rule["id"]: rule for rule in lebanon.RULE_SET["rules"]}
CTX = Context()


def rule(code: str, **changes) -> RuleSetting:
    """A rule of FMS Lebanon's set as seeded (not saved), with ``changes`` (``params`` merged)."""
    values = copy.deepcopy(lebanon.model_values(FMS[code]))
    params = changes.pop("params", None)
    if params is not None:
        values["params"] = {**values["params"], **params}
    values["deduction"] = Decimal(str(values["deduction"]))
    return RuleSetting(code=code, **{**values, **changes})


def entity(rating="on_track", *, kind="pd", partner=1, **kw) -> SimpleNamespace:
    """A finding row as the scoring reads it (Q1, roles)."""
    return SimpleNamespace(rating=rating, kind=kind, partner_id=partner, **kw)


def answer(role="", answered=True, **kw) -> AnswerFacts:
    values = {
        "question_key": role or "other",
        "role": role,
        "answered": answered,
        "placeholder": False,
        "words": 0,
    }
    return AnswerFacts(**{**values, **kw})


def record(**kw) -> Record:
    values = {"key": "1722", "scorable": True, "kinds": frozenset({"pd"})}
    return Record(**{**values, **kw})


def run(code: str, visit: Record, ctx: Context = CTX, **changes) -> rules.Outcome:
    return rules.evaluate(visit, {code: rule(code, **changes)}, ctx)[0]


# ------------------------------------------------------------------------------------------ the set
def test_the_lebanon_set_is_fms_file_with_no_staff_address():
    assert len(FMS) == 32
    enabled = {code for code, r in FMS.items() if r["enabled"]}
    assert enabled == {"R1", "R2", "R3", "R5", "R6", "R7", "R8", "R32", "R19", "R20", "R21", "R23"}
    text = str(lebanon.RULE_SET) + str(lebanon.RULE_PROMPTS)
    assert "@" not in text and "staff email" not in text
    assert all(v == [] for v in FMS["R19"]["reference_map"].values()) and FMS["R22"]["reference_list"] == []
    assert lebanon.STAFF_OFFICES == ("Beirut", "Zahle", "Tripoli", "Beirut/Mount Lebanon")
    weights = {c["key"]: c["weight"] for c in lebanon.categories()}
    assert weights == {
        "completeness": 30,
        "evidence": 20,
        "alignment": 20,
        "coherence": 15,
        "q3_quality": 10,
        "actionability": 5,
    }
    assert set(lebanon.RULE_PROMPTS) == {
        rules.param(rule(code), "ai_prompt_key") for code in enabled if FMS[code]["type"] == "narrative"
    }


def test_the_lebanon_deductions_add_up_to_each_categorys_weight():
    seeded = [rule(code) for code in FMS]
    weights = {c["key"]: Decimal(c["weight"]) for c in lebanon.categories()}
    sums = {row["key"]: row for row in rules.category_sums(seeded, weights)}
    assert {key: row["sum"] for key, row in sums.items() if key in weights} == weights
    assert all(sums[key]["matches"] for key in weights)
    assert (
        rule("R1").deduction == 11
        and rules.nominal(rule("R2")) == 8
        and rules.max_deduction(rule("R2")) == 10
    )
    changes, categories = rules.rebalance(seeded, lebanon.categories())
    assert changes == {} and categories == lebanon.categories()  # nothing to rebalance


# ------------------------------------------------------------------------------------------ types
def test_completeness_takes_each_missing_fields_deduction():
    present = {
        "narrative_finding": False,
        "overall_finding_rating": True,
        "hact_q1_answer": True,
        "hact_q2_answer": False,
        "fmq_answered_categories": None,  # not in the data: not checked
    }
    out = run("R1", record(present=present))
    assert (out.status, out.deduction, out.max_deduction) == ("fail", Decimal(5), Decimal(11))
    assert out.detail == (
        "R1: Incomplete monitoring report — missing: General Observation (narrative), Q2 – Activities monitored"
    )
    assert (out.detail_key, out.category) == ("missing:0,3", "completeness")
    full = run("R1", record(present={k: True for k in present}))
    assert (full.status, full.deduction) == ("pass", 0)
    assert run("R1", record(present={})).status == "na"


@pytest.mark.parametrize(
    ("value", "status", "deduction"),
    [(90, "pass", 0), (80, "pass", 0), (79.9, "fail", 5), (50, "fail", 5), (12.5, "fail", 10)],
)
def test_r2_scores_the_share_answered_by_its_bands(value, status, deduction):
    out = run("R2", record(values={"fmq_answered_pct": Decimal(str(value))}))
    assert (out.status, out.deduction) == (status, Decimal(deduction))
    if status == "fail":
        assert (
            out.detail == f"R2: Only {rules.number(value)}% of monitoring questions answered (target: 80%+)"
        )


def test_a_missing_value_takes_the_missing_value_deduction_or_skips():
    out = run("R2", record(values={"fmq_answered_pct": None}))
    assert (out.status, out.deduction, out.detail_key) == ("fail", Decimal(3), "missing")
    out = run("R14", record(values={"red_flag_count": None}), enabled=True)
    assert out.status == "na"  # missing_value_deduction 0: the rule skips
    # a column NeuroDB cannot read at all (the checklist answers not found): not available, no deduction
    out = run(
        "R2", record(values={"fmq_answered_pct": None}, unreadable=score.unreadable(False, True, True, True))
    )
    assert (out.status, out.deduction, out.detail_key) == ("na", Decimal(0), "unreadable")


def test_the_columns_that_cannot_be_read_follow_the_keys_found():
    assert score.unreadable(True, True, True, True) == frozenset()
    assert score.unreadable(True, False, True, False) == {
        "fmq_answered_categories",
        "attachments_count",
        "attachment_count",
    }
    assert {"fmq_answered_pct", "method_count", "red_flag_count"} <= score.unreadable(False, True, True, True)


def test_bands_with_maximums_and_lists_with_required_categories():
    r14 = [run("R14", record(values={"red_flag_count": n}), enabled=True).deduction for n in (0, 2, 3, 6)]
    assert r14 == [0, 3, 6, 10]
    r31 = [run("R31", record(values={"method_count": n}), enabled=True).deduction for n in (0, 1, 2)]
    assert r31 == [8, 5, 0]
    cp = record(kinds=frozenset({"cp_output"}), values={"fmq_answered_categories": "Reach; Equity"})
    out = run("R10", cp, enabled=True)
    assert (out.status, out.deduction) == ("fail", 8)  # 2 categories but not Reach and Quality: the 1+ band
    met = record(kinds=frozenset({"cp_output"}), values={"fmq_answered_categories": "Reach; Quality"})
    assert run("R10", met, enabled=True).deduction == 5
    assert "for CP Output — categories covered: Reach; Quality" in run("R10", met, enabled=True).detail


def test_an_ai_check_is_off_pending_passed_or_flagged_with_its_explanation():
    prompts = frozenset(lebanon.RULE_PROMPTS)
    assert run("R3", record()).status == "off"  # AI checks off
    assert run("R3", record(), Context(ai_on=True)).detail_key == "no_prompt"
    on = Context(ai_on=True, prompt_keys=prompts)
    assert run("R3", record(), on).status == "pending"
    passed = run("R3", record(checks={"R3": (True, "Q2 names what was observed.")}), on)
    assert (passed.status, passed.deduction) == ("pass", 0)
    flagged = run("R3", record(checks={"R3": (False, "Q2 restates the partner's report.")}), on)
    assert (flagged.status, flagged.deduction, flagged.category) == ("fail", Decimal(20), "evidence")
    assert flagged.detail == (
        "R3: Q2 lacks specific or disaggregated activity evidence — Q2 restates the partner's report."
    )
    assert run("R3", record(checks={"R3": (False, "")}), on).detail == (
        "R3: Q2 lacks specific or disaggregated activity evidence"
    )
    # the AI switched off: an answer kept still counts, one never made is not pending
    off = Context(ai_on=False, prompt_keys=prompts)
    assert run("R3", record(checks={"R3": (False, "")}), off).status == "fail"
    assert run("R3", record(), off).status == "off"
    # the AI checks switched off in Score settings: none counts
    unticked = Context(ai_on=True, ai_checks=False, prompt_keys=prompts)
    assert run("R3", record(checks={"R3": (False, "")}), unticked).status == "off"


def test_r19_compares_the_monitors_addresses_and_never_writes_one():
    staff = Context(staff={"zahle": frozenset({"lead@unicef.example"})})
    visit = record(
        values={"field_offices": ["Zahle"]}, refs={"people": {"team_members": {"x@partner.example"}}}
    )
    assert run("R19", visit).status == "nap"  # no list yet: skipped silently
    out = run("R19", visit, staff)
    assert (out.status, out.deduction) == ("fail", 3)
    assert "@" not in out.detail
    assert out.detail == "R19: Monitor is not listed as staff for field office 'Zahle' — verify assignment"
    listed = record(
        values={"field_offices": ["Zahle"]}, refs={"people": {"team_members": {"lead@unicef.example"}}}
    )
    assert run("R19", listed, staff).status == "pass"
    nobody = record(values={"field_offices": ["Zahle"]}, refs={"people": {"team_members": set()}})
    assert run("R19", nobody, staff).status == "na"
    elsewhere = record(
        values={"field_offices": ["Tripoli"]}, refs={"people": {"team_members": {"x@y.example"}}}
    )
    assert run("R19", elsewhere, staff).status == "nap"


def test_r20_the_visited_place_among_the_programme_documents_locations():
    refs = {"pd_locations": {"LEB/PCA1/PD2": frozenset({"lb30"})}, "pd_numbers": ["LEB/PCA1/PD2"]}
    there = record(refs={**refs, "place_pcodes": ["lbs9", "lb30", "lb20"]}, values={"location_pcode": "LBS9"})
    assert run("R20", there).status == "pass"  # the site lies in the registered cadaster
    away = record(refs={**refs, "place_pcodes": ["lb31", "lb20"]}, values={"location_pcode": "LB31"})
    out = run("R20", away)
    assert (out.status, out.deduction) == ("fail", 5)
    assert (
        out.detail == "R20: Location pcode 'LB31' is not a registered PD/SSFA site for partner 'LEB/PCA1/PD2'"
    )
    added = run("R20", away, params={"reference_map": {"LEB/PCA1/PD2": ["LB31"]}})
    assert added.status == "pass"  # the rule's own map adds to what eTools holds
    assert run("R20", record(refs={"place_pcodes": ["lb30"]})).status == "nap"  # nothing registered
    assert run("R20", record(kinds=frozenset({"partner"}), refs=refs)).detail_key == "entity_type"


def test_r23_falls_back_to_the_partners_running_documents_and_flags_without_points():
    refs = {"partner_pd_locations": {"LEB/PCA1/PD9": frozenset({"lb30"})}, "place_pcodes": ["lb31"]}
    out = run("R23", record(kinds=frozenset({"partner"}), refs=refs))
    assert (out.status, out.deduction, out.max_deduction) == ("fail", 0, 0)


def test_r21_sections_of_a_cp_output_visit():
    refs = {"cp_outputs": ["2.2 EDUCATION"], "cp_output_sections": {"2.2 EDUCATION": {"Education"}}}
    good = record(kinds=frozenset({"cp_output"}), refs=refs, values={"sections_names": ["Education"]})
    assert run("R21", good).status == "pass"
    bad = record(kinds=frozenset({"cp_output"}), refs=refs, values={"sections_names": ["Education", "WASH"]})
    out = run("R21", bad)
    assert out.status == "fail" and out.detail == (
        "R21: Section 'WASH' does not match expected sections for CP output '2.2 EDUCATION'"
    )
    coded = {"cp_outputs": ["2490/A0/08/302/002 Access"], "cp_output_sections": {}}
    mapped = record(kinds=frozenset({"cp_output"}), refs=coded, values={"sections_names": ["Education"]})
    assert run("R21", mapped).status == "pass"  # FMS's map, keyed by the output's code


def test_string_contains_and_the_entity_type_filter():
    pd = record(values={"fmq_answered_categories": "Reach; Supplies"})
    assert run("R29", pd, enabled=True).status == "pass"
    assert run("R29", record(values={"fmq_answered_categories": "Reach"}), enabled=True).deduction == 5
    assert run("R26", pd, enabled=True).status == "nap"  # partner visits only


def test_switched_off_and_not_scored_visits():
    # a rule switched off has no outcome (no row kept); the others keep theirs
    assert rules.evaluate(record(), {"R1": rule("R1", enabled=False)}, CTX) == []
    both = rules.evaluate(record(), {"R2": rule("R2"), "R1": rule("R1", enabled=False)}, CTX)
    assert [o.rule for o in both] == ["R2"]
    out = run("R1", record(scorable=False, status_group="cancelled"))
    assert (out.status, out.detail) == ("nap", rules.CANCELLED_DETAIL)
    assert run("R1", record(scorable=False)).detail == rules.PENDING_DETAIL


def test_rules_run_in_the_order_of_their_ids():
    found = {c: rule(c, enabled=True) for c in ("R10", "R2", "R32", "R1")}
    codes = [o.rule for o in rules.evaluate(record(), found, CTX)]
    assert codes == ["R1", "R2", "R10", "R32"]


# ------------------------------------------------------------------------------------------ settings
@pytest.mark.django_db
def test_a_rules_settings_are_checked():
    params, deduction = rules.validate_rule(rule("R2"))
    assert deduction == 8 and params["field"] == "fmq_answered_pct"
    with pytest.raises(ValidationError) as staff:
        rules.validate_rule(rule("R19", params={"reference_map": {"Zahle": ["someone@unicef.example"]}}))
    assert "Field office staff lists" in str(staff.value)
    with pytest.raises(ValidationError):
        rules.validate_rule(rule("R2", params={"strict": True}))  # not a setting of its type
    with pytest.raises(ValidationError) as category:
        rules.validate_rule(rule("R12", enabled=True))  # its category has no weight
    assert "compliance" in str(category.value)
    with pytest.raises(ValidationError):
        rules.validate_rule(rule("R3", params={"ai_prompt_key": ""}))
    fields = [{"name": "entity", "label": "Entity", "deduction": 4}]
    assert rules.validate_rule(rule("R1", params={"fields": fields}))[1] == 4  # the sum of its fields


def _category_total(seeded: list, changes: dict, category: str) -> Decimal:
    total = Decimal(0)
    for r in seeded:
        if r.enabled and r.category == category:
            params, deduction = changes.get(r.code, (r.params, r.deduction))
            total += rules.nominal(rules.SimpleRule(params, r.type, deduction))
    return total


def test_rebalance_scales_deductions_to_the_weights():
    seeded = [rule(code) for code in FMS]
    by_code = {r.code: r for r in seeded}
    by_code["R3"].deduction = Decimal(10)  # evidence: 10 of 20
    by_code["R7"].deduction = Decimal(15)  # q3_quality: R7 15 + R8 5 of 10
    categories = [{**c, "weight": c["weight"] * 2} for c in lebanon.categories()]  # sum 200
    changes, new = rules.rebalance(seeded, categories)
    assert [c["weight"] for c in new] == [30, 20, 20, 15, 10, 5]
    assert changes["R3"][1] == 20
    assert _category_total(seeded, changes, "q3_quality") == 10
    assert set(changes) == {"R3", "R7", "R8"}  # the categories already at their weight are left
    assert "R12" not in changes  # a rule switched off is never touched


def test_rebalance_scales_a_completeness_rules_fields():
    seeded = [rule(code) for code in FMS]
    r1 = next(r for r in seeded if r.code == "R1")
    r1.params["fields"][0]["deduction"] = 14  # completeness: 22 + 8 + 3 + 5 + 3 = 41 of 30
    changes, _new = rules.rebalance(seeded, lebanon.categories())
    assert _category_total(seeded, changes, "completeness") == 30
    assert sum(Decimal(str(f["deduction"])) for f in changes["R1"][0]["fields"]) == changes["R1"][1]


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
    assert rules.matches(rules.parse.fold("Is (a+)+ here?"), "(a+)+") is True  # plain words
    assert rules.matches(rules.parse.fold("Écoles visitées"), "ecoles") is True
    assert rules.matches("anything", "=") is False


def test_render_leaves_out_what_has_no_value():
    assert rules.render("R3: Q2 lacks evidence — {ai_detail}") == "R3: Q2 lacks evidence"
    assert rules.render("Monitor {value} is not listed for '{key_value}'", key_value="Zahle") == (
        "Monitor is not listed for 'Zahle'"
    )
