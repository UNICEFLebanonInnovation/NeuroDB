"""Records (Release 2 step 5, stage F2): each entity row of a visit is scored, flagged and given an
urgency on its own, as FMS does, and a visit's figures are worked out from its records'. The build's
row columns (the derived columns over the answers that apply to a row, its place), the per-record rules
(the entity type filter row by row, R19 once per visit), the visit aggregates (mean, lowest, most
urgent, union of flags, provisional), the visit rule results derived from the records', the record
rule results written by both kinds of refresh, the rules version and refresh request of the migration,
and the stable record id."""

from __future__ import annotations

import datetime
import importlib
from decimal import Decimal

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction
from django.test import override_settings

from neurodb.datamart import models as dm
from neurodb.fmm import metrics, refresh, score, versions
from neurodb.fmm.ai import checks
from neurodb.fmm.models import (
    AICheckAnswer,
    FieldOfficeStaff,
    RecordRuleResult,
    RefreshRequest,
    RuleSetVersion,
    ScoreSetting,
    Visit,
    VisitEntity,
    VisitRuleResult,
)
from neurodb.fmm.rules import Outcome

from .conftest import CP_OUTPUT, PD_LEBA

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


def _refresh(**kw):
    run = refresh.run(triggered_by="test", today=TODAY, **kw)
    assert run.status == "succeeded", run.error
    return run


def _records(key: str) -> dict[int, VisitEntity]:
    return {e.datamart_id: e for e in VisitEntity.objects.filter(visit__key=key)}


def _results(datamart_id: int) -> dict[str, RecordRuleResult]:
    return {r.rule: r for r in RecordRuleResult.objects.filter(entity__datamart_id=datamart_id)}


# ------------------------------------------------------------------------------------------ the build
def test_each_record_has_its_own_derived_columns_and_place(fm_world):
    categories = {PD_LEBA: "HACT", CP_OUTPUT: "Reach"}
    for doc in dm.DatamartDocument.objects.filter(dataset="fm_questions", data__monitoring_activity_id=1722):
        text = doc.data["question_text"]
        doc.data["category"] = categories.get(doc.data["entity"]) or (
            "PSEA" if "PSEA" in text else "Programme"
        )
        doc.save()
    _refresh()
    rows = {e.datamart_id: e for e in VisitEntity.objects.all()}
    visit = Visit.objects.get(key="1722")
    # the visit: every answer; a record: its own, its partner's and the visit's
    assert visit.fmq_answered_categories == "HACT; Programme; PSEA; Reach"
    assert rows[101].fmq_answered_categories == "HACT; Programme; PSEA"
    assert rows[102].fmq_answered_categories == "Programme; PSEA; Reach"
    assert rows[103].fmq_answered_categories == "Programme; PSEA"  # the partner's row: the visit's only
    assert (visit.questions_asked, rows[101].questions_asked, rows[103].questions_asked) == (5, 4, 3)
    assert rows[101].method_count == rows[103].method_count == 1  # "Interview"
    # 1723: its SSFA answered its own Q1; the other two questions were the visit's, left unanswered
    assert Visit.objects.get(key="1723").fmq_answered_pct == Decimal("33.3")
    assert (rows[111].questions_asked, rows[111].questions_answered) == (3, 1)
    assert (rows[111].fmq_answered_pct, rows[112].fmq_answered_pct) == (Decimal("33.3"), Decimal("0.0"))
    # answers that name no record (1726: one for the partner, one for the visit): every record has the
    # visit's figures, as FMS repeats them on each row
    v1726 = Visit.objects.get(key="1726")
    for n in (121, 122):
        assert (rows[n].questions_asked, rows[n].fmq_answered_pct) == (
            v1726.questions_asked,
            v1726.fmq_answered_pct,
        )
    assert rows[141].questions_asked is None and rows[141].fmq_answered_pct is None  # no question data
    # each record keeps its row's own place (rules R20 and R23 read it before the visit's)
    assert rows[101].location_id == fm_world.cadasters["zahle_town"].pk
    assert rows[121].location_id == fm_world.cadasters["mina"].pk


def test_r2_reads_each_records_share_answered(fm_world):
    _refresh()
    ssfa, partner = _results(111)["R2"], _results(112)["R2"]
    assert (ssfa.status, ssfa.measure, partner.status, partner.measure) == ("fail", 33.3, "fail", 0.0)
    assert "Only 0% of monitoring questions answered" in partner.detail
    assert _results(101)["R2"].status == "pass" and _results(101)["R2"].measure == 100.0


def test_the_record_id_is_unique(fm_world):
    _refresh()
    entity = VisitEntity.objects.get(datamart_id=101)
    with pytest.raises(IntegrityError), transaction.atomic():
        VisitEntity.objects.create(visit=entity.visit, datamart_id=101, kind="pd", rating="on_track")
    assert entity.anchor == "record-101"
    assert entity.get_absolute_url() == f"/fmm/visits/{entity.visit.key}/#record-101"


# ------------------------------------------------------------------------------------------ scoring
def _bad_cp_output_and_a_staff_list():
    """1722's CP output row loses its narrative and rating, and Zahle's staff list leaves its monitor
    out (R19 fails, once for the visit); built, then rescored (the team's key is read from the keys the
    first refresh wrote)."""
    dm.MonitoringFinding.objects.filter(datamart_id=102).update(
        narrative_finding="", overall_finding_rating=""
    )
    FieldOfficeStaff.objects.filter(office="Zahle").update(emails="someone.else@unicef.example")
    _refresh()
    _refresh(scores_only=True)


def test_each_record_is_scored_on_its_own_and_the_visit_from_its_records(fm_world):
    _bad_cp_output_and_a_staff_list()
    records = _records("1722")
    pd, cp, partner = records[101], records[102], records[103]
    # R19 (3) on every record; the CP output also lacks its narrative (3) and rating (2), the partner's
    # row its Q1 (2): the programme document is the best record
    assert (pd.quality_score, cp.quality_score, partner.quality_score) == (
        Decimal("97.0"),
        Decimal("92.0"),
        Decimal("95.0"),
    )
    assert (pd.score_band, cp.score_band, pd.flag_count) == ("high", "high", 1)
    assert cp.flags == ["R1", "R19", "R23"] and pd.flags == ["R19"]
    visit = Visit.objects.get(key="1722")
    assert visit.quality_score == score.average_quality(e.quality_score for e in records.values())
    assert visit.quality_score == Decimal("94.7") and visit.score_band == "high"
    assert (visit.lowest_score, visit.records_scored, visit.records_low) == (Decimal("92.0"), 3, 0)
    most = max(records.values(), key=lambda e: e.urgency)
    assert (visit.urgency, visit.urgency_band, visit.urgency_parts) == (
        most.urgency,
        most.urgency_band,
        most.urgency_parts,
    )
    assert visit.flags == ["R1", "R19", "R23"] and visit.flag_count == 3  # the union, in code order
    assert visit.category_deductions == {"completeness": 5.3}  # the records' mean: (3 + 8 + 5) / 3
    # the entity type filter applies record by record
    assert _results(102)["R20"].status == "nap" and _results(102)["R20"].detail == (
        "Applies to PD/SSFA records only."
    )
    assert _results(101)["R21"].status == "nap" and _results(101)["R21"].detail == (
        "Applies to CP Output records only."
    )
    # R19 is the visit's: checked once, the same on every record
    r19 = {
        (r.status, r.points, r.max_points, r.detail_key, r.detail)
        for r in RecordRuleResult.objects.filter(entity__visit=visit, rule="R19")
    }
    assert len(r19) == 1 and next(iter(r19))[0] == "fail"


def test_a_visit_rule_limited_to_an_entity_type_skips_the_other_records(fm_world):
    """R19 is evaluated once for the visit; with an entity type filter it still applies record by
    record, as every rule's filter does: only the programme document's record loses its points."""
    from neurodb.fmm.models import RuleSetting

    rule = RuleSetting.objects.get(code="R19")
    rule.params = {**rule.params, "entity_type_filter": "PD"}
    rule.save()
    _bad_cp_output_and_a_staff_list()
    assert _results(101)["R19"].status == "fail"
    for n in (102, 103):
        assert (_results(n)["R19"].status, _results(n)["R19"].detail) == (
            "nap",
            "Applies to PD/SSFA records only.",
        )
    records = _records("1722")
    assert "R19" in records[101].flags and "R19" not in records[103].flags


def test_the_visit_rule_results_are_derived_from_the_records(fm_world):
    _bad_cp_output_and_a_staff_list()
    visit = {r.rule: r for r in VisitRuleResult.objects.filter(visit__key="1722")}
    r1 = visit["R1"]
    assert r1.status == "fail" and r1.detail.startswith("(CP Output) R1: Incomplete monitoring report")
    assert (r1.max_points, r1.points) == (Decimal("11.0"), Decimal("8.7"))  # the mean of 11, 6 and 9
    assert visit["R19"].detail.startswith("R19: Monitor is not listed")  # the visit's own: no record named
    assert visit["R20"].status == "nap" and visit["R2"].status == "pass"
    assert set(visit) == set(_results(101))  # one per rule switched on


def test_visit_results_take_a_fail_then_a_pass_then_the_most_common_state():
    pd, cp, partner = (
        VisitEntity(kind=k, datamart_id=n) for n, k in enumerate(("pd", "cp_output", "partner"))
    )

    def outcome(status, taken=0, detail=""):
        return Outcome("R9", status, Decimal(taken), Decimal(10), "k", detail or status)

    visit = Visit(key="1")
    failed = score.visit_results(visit, [(pd, [outcome("pass")]), (cp, [outcome("fail", 10, "R9: no")])])
    assert (failed[0].status, failed[0].detail, failed[0].points) == (
        "fail",
        "(CP Output) R9: no",
        Decimal("5.0"),
    )
    passed = score.visit_results(visit, [(pd, [outcome("na")]), (cp, [outcome("pass")])])
    assert passed[0].status == "pass"
    other = score.visit_results(
        visit, [(pd, [outcome("nap")]), (cp, [outcome("na")]), (partner, [outcome("na")])]
    )
    assert other[0].status == "na"
    copied = score.visit_results(visit, [(pd, [outcome("fail", 3, "R9: x")])], frozenset({"R9"}))
    assert copied[0].detail == "R9: x"


def test_a_critical_item_line_keeps_the_record_type_apart_from_the_flag():
    line = metrics._flag_line("R3", "(PD/SSFA) R3: Narrative too short — fewer than 50 words", None)
    assert line == {
        "message": "Narrative too short",
        "explanation": "fewer than 50 words",
        "record": "PD/SSFA",
    }
    assert metrics._flag_line("R3", "R3: Narrative too short", None)["record"] == ""
    # brackets that open a flag of another rule's wording stay part of the message
    assert metrics._flag_line("R3", "(see notes) missing", None)["message"] == "(see notes) missing"


def test_a_visit_with_a_provisional_record_is_provisional(built):
    with override_settings(**AI_ON):
        book = score.Rulebook.load()
        visit = Visit.objects.get(key="1722")
        item = checks._items([visit])[0]
        inputs = checks.collect([item])[visit.key]
        index = [row.datamart_id for row in item.rows].index(102)
        found = checks.input_hash(checks.payload(inputs, index, book.rules["R7"], book.setting.ai_text_chars))
        assert AICheckAnswer.objects.filter(rule="R7", input_hash=found).delete()[0] == 1
        _refresh(scores_only=True)
    records = _records("1722")
    cp = records[102]
    assert (cp.quality_score, cp.provisional_score, cp.ai_pending) == (None, Decimal("100.0"), 1)
    assert cp.urgency is None and records[101].quality_score == Decimal("95.0")  # the others stay scored
    visit = Visit.objects.get(key="1722")
    assert (visit.quality_score, visit.urgency, visit.ai_pending, visit.score_band) == (None, None, 1, "")
    assert visit.provisional_score == Decimal("96.0")  # the mean of 95, 100 so far (R7 pending) and 93
    assert visit.not_scored_reason == "provisional: 1 AI checks pending"
    assert (visit.records_scored, visit.lowest_score) == (2, Decimal("93.0"))


def test_records_of_a_visit_not_scored_follow_its_status(built):
    pending = _records("1724")[141]
    assert (pending.quality_score, pending.score_band, pending.not_scored_reason) == (
        None,
        "pending",
        score.PENDING,
    )
    cancelled = _records("1725")[151]
    assert (cancelled.score_band, cancelled.not_scored_reason) == ("", "cancelled")
    assert {r.status for r in RecordRuleResult.objects.filter(entity=pending)} == {"nap"}
    visit = Visit.objects.get(key="1724")
    assert (visit.score_band, visit.not_scored_reason, visit.records_scored) == ("pending", score.PENDING, 0)


# ------------------------------------------------------------------------------------------ the refresh
def _record_rows() -> list[tuple]:
    return sorted(
        RecordRuleResult.objects.values_list(
            "entity__datamart_id", "rule", "status", "points", "max_points", "detail_key", "detail", "measure"
        )
    )


def test_both_refreshes_write_the_record_results_and_replace_them(built):
    enabled = sum(1 for rule in score.Rulebook.load().rules.values() if rule.enabled)
    rows = _record_rows()
    assert len(rows) == enabled * VisitEntity.objects.count() == 12 * 12
    scores = sorted(VisitEntity.objects.values_list("datamart_id", *score.RECORD_SCORE_FIELDS))
    _refresh()  # a full pass re-creates the records (new pks) and their results
    assert _record_rows() == rows
    assert sorted(VisitEntity.objects.values_list("datamart_id", *score.RECORD_SCORE_FIELDS)) == scores
    VisitEntity.objects.filter(datamart_id=101).update(quality_score=None, flags=[])
    RecordRuleResult.objects.filter(entity__datamart_id=101).delete()
    _refresh(scores_only=True)  # a scores-only pass writes them again
    assert _record_rows() == rows
    assert sorted(VisitEntity.objects.values_list("datamart_id", *score.RECORD_SCORE_FIELDS)) == scores


def test_a_scores_only_pass_writes_only_the_results_that_changed(built):
    _refresh(scores_only=True)
    kept = dict(RecordRuleResult.objects.values_list("pk", "detail"))
    visit_kept = dict(VisitRuleResult.objects.values_list("pk", "detail"))
    rows = _record_rows()
    _refresh(scores_only=True)  # nothing changed: every row stays as it was
    assert dict(RecordRuleResult.objects.values_list("pk", "detail")) == kept
    assert dict(VisitRuleResult.objects.values_list("pk", "detail")) == visit_kept
    # one record's flag differs from the scoring's, another result is missing: those two are written
    RecordRuleResult.objects.filter(entity__datamart_id=101, rule="R2").update(detail="an older flag")
    entity = VisitEntity.objects.exclude(datamart_id=101).order_by("datamart_id").first()
    RecordRuleResult.objects.filter(entity=entity, rule="R1").delete()
    _refresh(scores_only=True)
    now = dict(RecordRuleResult.objects.values_list("pk", "detail"))
    assert len(set(now) - set(kept)) == 2 and len(set(kept) - set(now)) == 2
    assert _record_rows() == rows
    # without any result, none is kept
    with transaction.atomic():
        assert refresh.update_results(RecordRuleResult, []) == 0
        assert not RecordRuleResult.objects.exists()
        transaction.set_rollback(True)


def test_the_visits_keep_their_pks_with_or_without_a_temporary_table(built, monkeypatch):
    rows, keys = _record_rows(), dict(Visit.objects.values_list("key", "pk"))
    _refresh()  # through a temporary table
    assert dict(Visit.objects.values_list("key", "pk")) == keys and _record_rows() == rows
    # where the database user may not create one, the visits and results are written as before
    monkeypatch.setattr(refresh, "_scratch", lambda cursor, table, names: False)
    _refresh()
    assert dict(Visit.objects.values_list("key", "pk")) == keys and _record_rows() == rows
    _refresh(scores_only=True)
    assert _record_rows() == rows


def test_the_run_details_count_records(built):
    run = _refresh(scores_only=True)
    details = run.details
    assert (details["records"], details["records_scored"], details["records_provisional"]) == (12, 10, 0)
    assert details["rule_results"]["R2"] == {"pass": 8, "fail": 2, "nap": 2}  # per record


def test_answers_no_scoring_read_for_120_days_are_deleted(built):
    kept = AICheckAnswer.objects.count()
    old = AICheckAnswer.objects.order_by("pk").first()
    AICheckAnswer.objects.filter(pk=old.pk).update(last_used=TODAY - datetime.timedelta(days=121))
    unused = AICheckAnswer.objects.create(
        rule="R3",
        input_hash="x" * 64,
        prompt_hash="y" * 64,
        passed=True,
        checked_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        last_used=TODAY - datetime.timedelta(days=200),
    )
    run = _refresh()
    # the old answer still serves a record: the scoring marked it used again before the clean-up
    assert AICheckAnswer.objects.filter(pk=old.pk).exists()
    assert not AICheckAnswer.objects.filter(pk=unused.pk).exists()
    assert run.details["ai_answers_forgotten"] == 1 and AICheckAnswer.objects.count() == kept
    # switched off, the AI checks keep every answer for when they are back
    AICheckAnswer.objects.filter(pk=old.pk).update(last_used=TODAY - datetime.timedelta(days=300))
    ScoreSetting.objects.update(ai_checks=False)
    _refresh()
    assert AICheckAnswer.objects.filter(pk=old.pk).exists()


# ------------------------------------------------------------------------------------------ preview
def test_the_preview_counts_records_and_writes_nothing(built):
    AICheckAnswer.objects.update(last_used=TODAY - datetime.timedelta(days=10))
    r2 = score.Rulebook.load().rules["R2"]
    lower = {**r2.params, "scoring": [{"min": 30, "deduction": 0}, {"min": 0, "deduction": 10}]}
    result = versions.preview(
        {"R2": {"params": lower}}, {}, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
    )
    assert (result["visits"], result["records"]) == (8, 12)
    assert result["rules"]["R2"] == {"now": 2, "then": 1}  # the records at 33.3% and 0%; then 0% only
    assert versions.describe(result, ["R2"]).startswith("R2 would flag 1 record (now 2); average quality ")
    assert versions.describe(result, ["R2"]).endswith("; scored records 10 (now 10)")
    assert set(AICheckAnswer.objects.values_list("last_used", flat=True)) == {
        TODAY - datetime.timedelta(days=10)
    }


# ------------------------------------------------------------------------------------------ migration
def test_the_migration_records_a_rules_version_and_asks_for_a_full_refresh(built):
    migration = importlib.import_module("neurodb.fmm.migrations.0019_records")
    before = versions.current_rules_version()
    RefreshRequest.objects.all().delete()
    migration.new_rules_version(apps, None)
    migration.ask_full_refresh(apps, None)
    latest = RuleSetVersion.objects.get(number=before + 1)
    assert latest.note == migration.NOTE and latest.created_by_name == "NeuroDB"
    assert latest.snapshot == RuleSetVersion.objects.get(number=before).snapshot
    assert RefreshRequest.load().full_requested_at is not None
    migration.new_rules_version(apps, None)  # once only
    assert versions.current_rules_version() == before + 1
    # every visit reads "recomputing" until a pass rescores it: the full pass the request asks for
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {before}
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.target == "full" and set(Visit.objects.values_list("rules_version", flat=True)) == {before + 1}


def test_without_visits_the_migration_changes_nothing(db):
    migration = importlib.import_module("neurodb.fmm.migrations.0019_records")
    before = versions.current_rules_version()
    migration.new_rules_version(apps, None)
    migration.ask_full_refresh(apps, None)
    assert versions.current_rules_version() == before
    assert RefreshRequest.objects.filter(full_requested_at__isnull=False).count() == 0
