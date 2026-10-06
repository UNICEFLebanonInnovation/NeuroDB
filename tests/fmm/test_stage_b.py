"""Release 2, stage B, on the built world: FMS Lebanon's rule set as seeded (B1), the derived columns of a
visit (B2), the reference checks from eTools data and the field offices' staff lists (B4), the rule
admin (Rebalance, the staff lists) and the pages that follow the new engine (B5). The AI checks
themselves are in test_ai_checks."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal

import pytest
from django.http import QueryDict
from django.urls import reverse

from neurodb.datamart import models as dm
from neurodb.fmm import lebanon, metrics, refresh, rules, versions
from neurodb.fmm import scope as scope_module
from neurodb.fmm.ai import profiles
from neurodb.fmm.models import (
    FieldOfficeStaff,
    RefreshRequest,
    RuleSetting,
    RuleSetVersion,
    ScoreSetting,
    Visit,
    VisitRuleResult,
)
from neurodb.fmm.scope import Scope
from neurodb.integrations import background

from .conftest import MEMBER_EMAIL

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _rescore() -> None:
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.status == "succeeded", run.error


def _results(key: str) -> dict[str, VisitRuleResult]:
    return {r.rule: r for r in VisitRuleResult.objects.filter(visit__key=key)}


# ------------------------------------------------------------------------------------------ B1 seeded
def test_the_lebanon_rule_set_is_seeded():
    found = {r.code: r for r in RuleSetting.objects.all()}
    assert len(found) == 32 and not ({"R4", "R9", "R22", "R31"} & {c for c, r in found.items() if r.enabled})
    assert {c for c, r in found.items() if r.enabled} == {
        "R1", "R2", "R3", "R5", "R6", "R7", "R8", "R19", "R20", "R21", "R23", "R32"
    }  # fmt: skip
    r1 = found["R1"]
    assert (r1.type, r1.category, r1.group, r1.deduction) == ("completeness", "completeness", "core", 11)
    assert r1.hact_spec == "Rule 1 - Completeness"
    assert found["R3"].params["ai_prompt_key"] == "evidence_sufficiency"
    assert found["R20"].params["reference_map"]["LEBA/PCA2023646/PD20251522-2"] == ["MU-35277"]
    assert all(not v for v in found["R19"].params["reference_map"].values())
    for rule in found.values():
        assert "@" not in str(rule.params) + rule.description + rule.flag_template, rule.code
    setting = ScoreSetting.load()
    assert setting.categories == lebanon.categories() and (setting.band_high, setting.band_medium) == (80, 50)
    assert {o.office: o.emails for o in FieldOfficeStaff.objects.all()} == dict.fromkeys(
        lebanon.STAFF_OFFICES, ""
    )
    version = profiles.published()
    assert set(version.rule_prompts) == set(lebanon.RULE_PROMPTS)
    assert RuleSetVersion.objects.order_by("-number").first().note.startswith("FMS Lebanon rule set")


def test_the_built_visits_are_scored_with_the_fms_rules(built):
    visit = Visit.objects.get(key="1723")  # Q1 and Q2 on the rows: the narrative, a rating, 1 of 3 answered
    results = _results("1723")
    assert results["R1"].status == "fail" and results["R1"].deducted == 9
    assert results["R2"].detail == "R2: Only 33.3% of monitoring questions answered (target: 80%+)"
    assert visit.category_deductions == {"completeness": 19.0, "evidence": 20.0, "coherence": 15.0}
    assert (visit.quality_score, visit.score_band, visit.urgency) == (Decimal("46.0"), "low", 59)
    # the AI checks the world was built with (conftest.BUILT_VERDICTS) still count with the AI off
    assert {c: results[c].status for c in ("R3", "R5", "R6", "R7", "R8", "R32")} == {
        "R3": "fail", "R5": "pass", "R6": "fail", "R7": "pass", "R8": "pass", "R32": "pass"
    }  # fmt: skip
    assert results["R3"].detail.endswith("— Q2 lists no activity the monitor verified.")
    assert Visit.objects.get(key="1726").quality_score == Decimal("88.0")


# ------------------------------------------------------------------------------------------ B2
def test_the_derived_columns_of_a_visit(built):
    visit = Visit.objects.get(key="1723")
    assert visit.fmq_answered_pct == Decimal("33.3")  # 1 of the 3 questions asked
    assert visit.method_count == 1  # "Interview"
    assert visit.fmq_answered_categories is None and visit.red_flag_count is None  # not in the data
    assert visit.attachments_count is None
    assert Visit.objects.get(key="1724").fmq_answered_pct is None  # no question data


def test_categories_likert_red_flags_and_assigned_action_points(fm_world):
    docs = dm.DatamartDocument.objects.filter(dataset="fm_questions", data__monitoring_activity_id=1722)
    for doc in docs:
        doc.data["category"] = "PSEA" if "PSEA" in doc.data["question_text"] else "HACT"
        doc.save()
    for n, value in enumerate(("1", "4")):  # two answers on a 5-point scale: one red flag
        dm.DatamartDocument.objects.create(
            dataset="fm_questions",
            record_key=str(59000 + n),
            title="x",
            data={
                "id": 59000 + n,
                "monitoring_activity_id": 1722,
                "question_id": 40 + n,
                "question_text": f"How satisfied are the families ({n})?",
                "answer": value,
                "method": "Focus group" if n else "Observation",
                "category": "Acceptability",
            },
        )
        for option in range(1, 6):
            dm.DatamartDocument.objects.create(
                dataset="fm_options",
                record_key=f"o{n}{option}",
                title="x",
                data={
                    "id": 79000 + 10 * n + option,
                    "question_id": 40 + n,
                    "value": str(option),
                    "label": str(option),
                },
            )
    point = dm.ActionPoint.objects.get(datamart_id=8001)
    point.assigned_to_name = "Someone"
    point.save()
    refresh.run(triggered_by="test", today=TODAY)
    visit = Visit.objects.get(key="1722")
    assert visit.fmq_answered_categories == "Acceptability; HACT; PSEA"
    assert visit.red_flag_count == 1 and visit.method_count == 3
    assert visit.action_points_assigned == 1
    assert "Someone" not in str(Visit.objects.filter(key="1722").values().first())  # a count, never the name
    assert _results("1722")["R1"].status in ("pass", "fail") and "Data collection method records" not in (
        _results("1722")["R1"].detail
    )


# ------------------------------------------------------------------------------------------ B4
def test_r19_reads_the_staff_list_and_never_shows_an_address(built, client_viewer):
    assert _results("1722")["R19"].status == "nap"  # the lists are empty: skipped silently
    zahle = FieldOfficeStaff.objects.get(office="Zahle")
    zahle.emails = "someone.else@unicef.example"
    zahle.save()
    _rescore()
    r19 = _results("1722")["R19"]
    assert (r19.status, r19.deducted) == ("fail", 3)
    assert r19.detail == "R19: Monitor is not listed as staff for field office 'Zahle' — verify assignment"
    html = client_viewer.get("/fmm/visits/1722/").content.decode()
    assert "Monitor is not listed as staff for field office" in html
    assert "someone.else@unicef.example" not in html and MEMBER_EMAIL not in html
    zahle.emails = f"someone.else@unicef.example\n{MEMBER_EMAIL.upper()}"
    zahle.save()
    _rescore()
    assert _results("1722")["R19"].status == "pass"


def test_r20_reads_the_programme_documents_registered_locations(built):
    # 1726 is at the Tripoli community centre, in Tripoli Mina, a location of its PD: registered
    assert _results("1726")["R20"].status == "pass"
    from neurodb.partnerships.models import PCA

    edu = PCA.objects.get(number="LEB/PCA2024100/PD2026010")
    edu.locations.clear()
    edu.location_p_codes = ["LB35"]  # Qalamoun only
    edu.save()
    _rescore()
    r20 = _results("1726")["R20"]
    assert r20.status == "fail" and "is not a registered PD/SSFA site" in r20.detail
    rule = RuleSetting.objects.get(code="R20")
    rule.params = {**rule.params, "reference_map": {"LEB/PCA2024100/PD2026010": ["LB34"]}}
    rule.save()
    _rescore()
    assert _results("1726")["R20"].status == "pass"  # the administrator's entry adds to eTools'


def test_r21_reads_the_sections_of_the_programme_documents_of_a_cp_output(fm_world):
    from neurodb.partnerships.models import PCA

    row = dm.MonitoringFinding.objects.get(datamart_id=102)  # 1722's CP output row
    row.data["sections_names"] = "Education"
    row.save()
    refresh.run(triggered_by="test", today=TODAY)
    # "2.2 INCREASED ACCESS TO EDUCATION": FMS's map has no such code, no programme document names it
    assert _results("1722")["R21"].status == "nap"
    PCA.objects.filter(number="LEB/PCA2024100/PD2026010").update(
        cp_outputs=["2.2 INCREASED ACCESS TO EDUCATION"], section_names=["Child Protection"]
    )
    _rescore()
    r21 = _results("1722")["R21"]
    assert r21.status == "fail" and "Section 'Education' does not match expected sections" in r21.detail
    PCA.objects.filter(number="LEB/PCA2024100/PD2026010").update(section_names=["Education"])
    _rescore()
    assert _results("1722")["R21"].status == "pass"


# ------------------------------------------------------------------------------------------ admin
def test_the_rules_list_shows_the_category_sums_and_rebalance_fixes_a_mismatch(admin_client, built, started):
    page = admin_client.get(reverse("admin:fmm_rulesetting_changelist")).content.decode()
    assert "Sum of deductions per category" in page and "Completeness: 30.0 / 30" in page and "⚠" not in page
    rule = RuleSetting.objects.get(code="R3")
    rule.deduction = Decimal(10)
    rule.save()
    page = admin_client.get(reverse("admin:fmm_rulesetting_changelist")).content.decode()
    assert "Evidence: 10.0 / 20 ⚠" in page and "use Rebalance" in page.replace(
        "Use Rebalance", "use Rebalance"
    )
    before = versions.current_rules_version()
    url = reverse("admin:fmm_rulesetting_rebalance_rules")
    response = admin_client.post(url, {"note": "after the R3 edit", "_form_submitted": "on"})
    assert response.status_code in (200, 302)
    assert RuleSetting.objects.get(code="R3").deduction == 20
    version = RuleSetVersion.objects.get(number=before + 1)
    assert version.note == "after the R3 edit"
    assert RefreshRequest.objects.get(pk=1).scores_requested_at is not None  # rescored in the background


def test_a_rule_is_edited_with_its_parameters_checked(admin_client, built, started):
    url = reverse("admin:fmm_rulesetting_change", args=["R19"])
    form = admin_client.get(url).content.decode()
    assert "Staff e-mail addresses never go here" in form
    data = {
        "label": "Field Office - Team Member Validation",
        "enabled": "on",
        "category": "completeness",
        "group": "additional",
        "hact_spec": "",
        "deduction": "3",
        "flag_template": "R19: Monitor not on the staff list of '{key_value}'",
        "params": '{"check_type": "member_in_mapped_list", "reference_map": {"Zahle": ["x@unicef.example"]}}',
        "description": "",
        "change_note": "test",
    }
    page = admin_client.post(url, data).content.decode()
    assert "Field office staff lists, never in a rule" in page
    data["params"] = (
        '{"check_type": "member_in_mapped_list", "field": "team_members", "key_field": "field_offices"}'
    )
    assert admin_client.post(url, data).status_code == 302
    assert RuleSetting.objects.get(code="R19").flag_template.startswith("R19: Monitor not on")


def test_the_staff_lists_are_for_administrators_and_show_counts_only(admin_client, viewer, built):
    zahle = FieldOfficeStaff.objects.get(office="Zahle")
    url = reverse("admin:fmm_fieldofficestaff_change", args=[zahle.pk])
    response = admin_client.post(url, {"office": "Zahle", "emails": "B@Unicef.example\na@unicef.example\n"})
    assert response.status_code == 302
    assert RefreshRequest.objects.get(pk=1).scores_requested_at is not None
    zahle.refresh_from_db()
    assert zahle.emails == "a@unicef.example\nb@unicef.example"
    listing = admin_client.get(reverse("admin:fmm_fieldofficestaff_changelist")).content.decode()
    assert "a@unicef.example" not in listing and re.search(r">\s*2\s*<", listing)
    bad = admin_client.post(url, {"office": "Zahle", "emails": "not an address"}).content.decode()
    assert "One e-mail address per line" in bad
    from django.test import Client

    other = Client()
    other.force_login(viewer)
    assert other.get(reverse("admin:fmm_fieldofficestaff_changelist")).status_code in (302, 403)


def test_a_snapshot_keeps_the_fms_rule_fields_and_restores_them(admin_user, built, started):
    rule = RuleSetting.objects.get(code="R2")
    rule.enabled, rule.deduction = False, Decimal("6.5")
    rule.save()
    changed = versions.record_rules(admin_user, "R2 off")
    row = next(r for r in changed.snapshot["rules"] if r["code"] == "R2")
    assert (row["enabled"], row["deduction"], row["type"]) == (False, 6.5, "deterministic")
    seeded = RuleSetVersion.objects.get(note__startswith="FMS Lebanon rule set")
    versions.restore_rules(seeded, admin_user, "back")
    rule.refresh_from_db()
    assert (rule.enabled, rule.deduction) == (True, 8)
    assert any(
        r["name"] == "deduction" and r["group"] == "R2" for r in versions.differences(changed.snapshot)
    )


def test_the_preview_counts_any_rule(built):
    result = versions.preview({"R2": {"enabled": False}}, {})
    assert result["rules"]["R2"] == {"now": 1, "then": 0}
    assert "R32" in result["rules"]


# ------------------------------------------------------------------------------------------ B5
def test_what_quality_means_lists_the_thresholds_and_the_core_and_additional_rules(built, client_viewer):
    html = client_viewer.get(PAGE).content.decode()
    how = html.split('id="fmm-how"', 1)[1]
    assert "What does “quality” mean for Lebanon?" in how
    assert "High ≥ 80" in how and "Medium 50–79" in how and "Low &lt; 50" in how
    core = how.split("Core rules", 1)[1].split("Additional rules", 1)[0]
    assert (
        "R1 Report Completeness" in core and "HACT: Rule 1 - Completeness" in core and "R3 Narrative" in core
    )
    additional = how.split("Additional rules", 1)[1]
    assert (
        "R32 Challenge-to-Action Alignment" in additional and "R2 Question Answer Completeness" in additional
    )
    assert "R9 Programme Coverage" not in how and "20 rules are switched off and not listed." in how


def test_points_by_category_rule_analysis_and_flag_frequency(built, client_viewer):
    rows = {r["code"]: r for r in metrics.dimension_breakdown(_scope())["rows"]}
    assert rows["completeness"]["max"] == 30 and rows["completeness"]["earned"] == Decimal("23.3")
    analysis = {r["code"]: r for r in metrics.rule_analysis(_scope())}
    assert [r["code"] for r in metrics.rule_analysis(_scope())][:4] == ["R1", "R2", "R3", "R4"]
    assert analysis["R23"]["flag_only"] and analysis["R1"]["flagged"] == 6
    frequency = {r["code"] for r in metrics.flag_frequency(_scope())["rows"]}
    assert {"R1", "R2", "R19", "R20", "R23", "R32", "R7", "R8"} <= frequency
    html = client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "analysis"}, HTTP_HX_REQUEST="true")
    text = html.content.decode()
    assert "Points by category" in text and "Completeness" in text and "weight 30" in text


def test_the_visit_page_lists_each_rules_result_and_its_points_off(built, client_viewer):
    html = client_viewer.get("/fmm/visits/1723/").content.decode()
    quality = html.split("Quality checks", 1)[1].split("Questions and answers", 1)[0]
    assert 'data-rule="R1"' in quality and 'data-rule="R2"' in quality
    assert "R2: Only 33.3% of monitoring questions answered" in quality
    assert "Switched off:" in quality and "R3" in quality.split("Switched off:", 1)[1]
    assert "100 less Evidence 20, Completeness 19, Coherence 15" in html


def test_a_recurring_issue_is_named_from_the_rules_flag(built):
    labels = {row["drill"]: row["label"] for row in metrics.top_issues(_scope(), 30)}
    assert labels["R2:band"] == "R2: Only 33.3% of monitoring questions answered (target: 80%+)"
    assert any(label.startswith("R1: Incomplete monitoring report — missing:") for label in labels.values())
    assert rules.render("x {value}") == "x"
