"""The Help assistant's look-ups over a world of visits with named teams (stage D2.3): why a visit scored
what it scored, with every rule result, the deductions per category and the urgency parts, but never a
person, a narrative or an AI check's explanation; a visit not found is said so; with Monitoring insights
off nothing is read."""

import json

import pytest
from django.test import override_settings

from neurodb.assistant import tools as assistant_tools
from neurodb.fmm.models import Visit
from neurodb.help import assistant, tools
from tests.fmm.conftest import BUILT_VERDICTS, CANARIES, CANARY_TEXT, LEAD, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db


def _explain(user, visit):
    with tools.bind(user):
        return assistant_tools.run("explain_visit_score", {"visit": visit}, registry=assistant.REGISTRY)


def test_a_visit_score_is_explained_without_people_or_texts(built, viewer):
    out = _explain(viewer, "1723")
    visit = Visit.objects.get(activity_id=1723)
    assert out["key"] == visit.key and out["url"] == visit.get_absolute_url()
    assert out["quality_score"] == float(visit.quality_score) and out["band"] == "low"
    assert out["urgency"] == visit.urgency and out["urgency_parts"] == visit.urgency_parts
    # each record scored on its own: R3 failed 1723's partner record, R6 both of its records
    records = {r["record"]: r for r in out["records"]}
    assert set(records) == {111, 112} and out["lowest_score"] == min(
        r["quality_score"] for r in out["records"]
    )
    lost = {
        (n, line["rule"]): line["points_lost"]
        for n, record in records.items()
        for line in record["rule_results"]
        if line["result"] == "flagged"
    }
    assert (lost[(112, "R3")], lost[(111, "R6")], lost[(112, "R6")]) == (20, 15, 15) and (
        111,
        "R3",
    ) not in lost
    for record in records.values():
        deducted = sum(d["deducted"] for d in record["deductions_by_category"])
        assert round(100 - deducted, 1) == record["quality_score"]
    # the visit's deductions are its records' means, and its quality the mean of its records
    assert round(100 - sum(d["deducted"] for d in out["deductions_by_category"]), 1) == out["quality_score"]
    r3 = next(line for line in records[112]["rule_results"] if line["rule"] == "R3")
    assert r3["ai_check"] and "explanation is on the visit page" in r3["detail"]

    blob = json.dumps(out, ensure_ascii=False)
    for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
        assert canary not in blob, canary
    for (_label, _rule), (_passed, explanation) in BUILT_VERDICTS.items():
        assert explanation not in blob  # the AI's own words never come back
    assert CANARY_TEXT[:30] not in blob and "@" not in blob and '"team' not in blob
    assert "narrative" not in json.dumps(list(out))  # no narrative field at all


def test_a_visit_is_found_by_its_id_key_or_reference(built, viewer):
    visit = Visit.objects.get(activity_id=1722)
    assert _explain(viewer, "Visit 1722")["key"] == visit.key
    assert _explain(viewer, "#1722")["key"] == visit.key
    assert _explain(viewer, visit.key)["key"] == visit.key
    with pytest.raises(assistant_tools.ToolInputError, match="No visit 'nope'"):
        _explain(viewer, "nope")


def test_nothing_is_read_while_monitoring_insights_is_off(built, viewer):
    with override_settings(FMM_ENABLED=False):
        assert "switched off" in _explain(viewer, "1723")["error"]


def test_the_rules_look_up_names_no_monitor_over_the_built_world(built, viewer, admin_user):
    for user in (viewer, admin_user):
        with tools.bind(user):
            listed = assistant_tools.run("list_quality_rules", {}, registry=assistant.REGISTRY)
            r19 = assistant_tools.run("get_quality_rule", {"rule_id": "R19"}, registry=assistant.REGISTRY)
        blob = json.dumps([listed, r19], ensure_ascii=False)
        for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL):
            assert canary not in blob, canary
        assert "@" not in blob
