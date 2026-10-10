"""Release 2 step 5, stage F3b: what reads Monitoring insights outside its pages counts records too.

The exports' records dataset (one row per record, as FMS's export) beside the visits at visit grain; the
AI brief's facts and the code-written brief per record; the chat's look-ups (groups of records, visits
with their records, one visit record by record); the Help assistant's explanation record by record; the
partner and PD panels' records; the automatic action point's link to its record; and the clean-up of
the visits' own rule results and of the AI checks made per visit (dropped once none is left)."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse

from neurodb.assistant import tools as assistant_tools
from neurodb.fmm import exports, metrics
from neurodb.fmm.ai import facts, fallback, prompts, tools
from neurodb.fmm.models import LocalActionPoint, RecordRuleResult, Visit, VisitEntity
from neurodb.fmm.scope import Scope
from neurodb.fmm.score import average_quality

from .conftest import rule_states

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
YEAR = {"year": "2026", "section": ""}


def _scope(**params) -> Scope:
    return Scope.from_params({**YEAR, **{k: str(v) for k, v in params.items()}})


@pytest.fixture
def in_2026(monkeypatch):
    from neurodb.fmm import scope as scope_module

    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


# ------------------------------------------------------------------------------------------ exports
def test_the_records_dataset_has_one_row_per_record_and_the_visits_keep_their_columns(built):
    ctx = exports.Context.read()
    scope = _scope()
    records = list(exports.record_rows(scope.records(), ctx))
    visits = list(exports.visit_rows(scope.visits(), ctx))
    assert len(records) == VisitEntity.objects.filter(visit__in=scope.visits()).count() == 12
    assert len({r["id"] for r in records}) == len(records) and all(r["id"].startswith("e") for r in records)
    assert len(visits) == len({r["visit_id"] for r in records}) == 8
    # the visits keep every column a report built before records types (it would stop refreshing
    # without one), and gain the visit's figures from its records
    before = [*exports.VISIT_HEAD, *exports.VISIT_TAIL]
    columns = exports.visit_columns(ctx.weights)
    assert set(before) <= set(columns) and set(exports.VISIT_RECORDS) <= set(columns)
    for v in visits:
        own = [r for r in records if r["visit_id"] == v["id"]]
        assert v["records"] == len(own)
        scores = [r["quality_score"] for r in own if r["quality_score"] is not None]
        if v["quality_score"] is not None:
            assert v["quality_score"] == average_quality(scores)  # the visit's quality: the mean
            assert v["lowest_score"] == min(scores)
    assert exports.DATASETS == ("records", "visits", "rule_results", "action_points", "partners")


def test_a_record_filter_keeps_its_records_and_their_visits(built):
    ctx = exports.Context.read()
    scope = _scope(entity_type="partner")
    records = list(exports.record_rows(scope.records(), ctx))
    assert records and {r["entity_type"] for r in records} == {"Partner"}
    assert len(records) == metrics.kpis(scope)["records"]
    visit_ids = {r["id"] for r in exports.visit_rows(scope.visits(), ctx)}
    assert visit_ids == {r["visit_id"] for r in records}
    results = list(exports.rule_rows(scope.records(), ctx))
    assert {r["record_id"] for r in results} == {r["id"] for r in records}


# ------------------------------------------------------------------------------------------ AI facts
def test_the_brief_sends_the_record_figures_under_their_own_names(built):
    scope = _scope()
    found = facts.build(scope, narratives=False)
    kpi = found.payload["kpi"]
    k = metrics.kpis(scope)
    assert (kpi["records"], kpi["records_rated"], kpi["scored_records"]) == (
        k["records"],
        k["records_rated"],
        k["scored"],
    )
    assert kpi["high_urgency_records"] == k["high_urgency"] and kpi["amber_urgency_records"] == k["amber"]
    assert kpi["avg_quality"] == float(metrics.avg_quality(scope.records()))
    low = scope.records().filter(quality_score__lt=metrics.thresholds()["band_medium"]).count()
    assert kpi["low_quality_records"] == low
    # the rules count the records they flagged, as the page's rule analysis
    stats = metrics.rule_stats(scope)
    for key, entry in found.payload["rules"].items():
        assert entry["flagged"] == stats[key.split(":", 1)[1]].get("fail", 0), key


def test_the_facts_version_is_part_of_the_input_hash(built, monkeypatch):
    found = facts.build(_scope(), narratives=False)
    monkeypatch.setattr(facts, "FACTS_VERSION", facts.FACTS_VERSION + 1)
    assert facts.input_hash(found.payload) != found.input_hash


def test_the_cards_pick_visits_by_their_most_urgent_record(built):
    scope = _scope()
    order = facts.card_order(scope, 15)
    # the visit of the most urgent record comes first
    top = VisitEntity.objects.filter(visit__in=scope.visits()).exclude(urgency=None).order_by("-urgency")[0]
    assert order[0] == top.visit_id
    # a provisional visit (another of its records waits for its AI checks) has no urgency of its own:
    # its scored record's urgency still picks it
    Visit.objects.filter(pk=top.visit_id).update(urgency=None, urgency_band="", quality_score=None)
    assert facts.card_order(_scope(), 15)[0] == top.visit_id


def test_the_notes_are_sampled_by_the_records_own_urgency(built):
    """A visit's calm record no longer rides on its urgent record: among the off-track and constrained
    records, the order is the records' own urgency."""
    rows = facts.narrative_order(_scope())
    first = [r for r in rows if r["rating"] in ("off_track", "constrained")]
    urgencies = [-1 if r["urgency"] is None else r["urgency"] for r in first]
    assert urgencies == sorted(urgencies, reverse=True)


def test_the_code_written_brief_counts_the_records_a_flag_was_raised_on(built):
    found = facts.build(_scope(), narratives=False)
    sentences = [line["text"] for line in fallback._challenges(found)]
    issues = found.payload["issues"]
    assert issues and any(" records in " in s or " record in " in s for s in sentences)
    for line in fallback._challenges(found):
        for key in line["keys"]:
            if key in issues:
                n = issues[key]["records"]
                assert f"{n} record" in line["text"]


def test_the_fixed_prompt_speaks_of_records():
    assert prompts.SAFETY_VERSION == 4
    for text in (prompts.SAFETY_COMMON, prompts.DATA_GUIDE_INSIGHTS, prompts.DATA_GUIDE_CHAT):
        assert "record" in text
        assert '"entities"' not in text and "entities rated" not in text
    assert "average quality score per record" in prompts.DATA_GUIDE_INSIGHTS


# ------------------------------------------------------------------------------------------ chat
def test_summary_groups_count_records_their_visits_and_their_average(built, in_2026):
    scope = tools.current().scope
    k = metrics.kpis(scope)
    for group in ("partner", "rating", "entity_type", "status", "month"):
        out = assistant_tools.run("fm_summary", {"group_by": group})
        assert sum(g["records"] for g in out["groups"]) == k["records"] == out["records"], group
        assert all(g["visits"] <= g["records"] for g in out["groups"]), group
    # each record under its own partner: the exports' partner rows, the same figures
    by_partner = {g["code"]: g for g in assistant_tools.run("fm_summary", {"group_by": "partner"})["groups"]}
    for row in metrics.breakdown(scope, "partner"):
        group = by_partner[str(row["key"])]
        assert (group["records"], group["visits"]) == (row["records"], row["visits"])
        assert group["avg_quality"] == (float(row["avg"]) if row["avg"] is not None else None)
    rules = assistant_tools.run("fm_summary", {"group_by": "rule"})["groups"]
    stats = metrics.rule_stats(scope)
    for g in rules:
        assert g["records_flagged"] == (stats.get(g["code"]) or {}).get("fail", 0), g["code"]


def test_listed_visits_carry_their_records(built, in_2026):
    listed = assistant_tools.run("fm_visits", {"limit": 30})
    for card in listed["visits"]:
        visit = Visit.objects.get(key=card["key"].split(":", 1)[1])
        assert len(card["records"]) == visit.entity_rows.count()
        scores = [r["score"] for r in card["records"] if r["score"] is not None]
        if card["quality"] is not None:
            assert Decimal(str(card["quality"])) == average_quality(scores)
    one = next(c for c in listed["visits"] if c["key"] == "visit:1722")
    assert {r["type"] for r in one["records"]} == {"PD/SSFA", "CP output", "Partner"}
    assert "Amel Association" in {r["entity"] for r in one["records"]}


def test_one_visit_is_read_record_by_record(built, in_2026):
    one = assistant_tools.run("fm_visit", {"visit": "1722"})
    assert "entities" not in one and len(one["records"]) == 3
    for record in one["records"]:
        entity = VisitEntity.objects.get(datamart_id=record["record"])
        assert record["url"] == f"/fmm/visits/1722/#record-{entity.datamart_id}"
        assert record["quality"] == float(entity.quality_score) and record["urgency"] == entity.urgency
        stored = {
            r.rule: r.status for r in RecordRuleResult.objects.filter(entity=entity) if r.status != "off"
        }
        listed = {line["rule"]: line["status"] for line in record["rules"]}
        assert listed.items() <= stored.items()
    # a rule read once for the whole visit is given once, not on each record
    once = {line["rule"] for line in one["visit_checks"]}
    assert not once & {line["rule"] for r in one["records"] for line in r["rules"]}
    assert one["records_count"] == 3 and one["lowest_score"] == float(
        Visit.objects.get(key="1722").lowest_score
    )


def test_the_look_up_descriptions_define_a_record():
    described = " ".join(assistant_tools.TOOLS[name][1] for name in ("fm_summary", "fm_visits", "fm_visit"))
    assert "one record per entity assessed" in described and "per record" in described


# ------------------------------------------------------------------------------------------ help
def test_the_help_assistant_explains_a_visit_record_by_record(built, viewer):
    from neurodb.help import assistant
    from neurodb.help import tools as help_tools

    with help_tools.bind(viewer):
        out = assistant_tools.run("explain_visit_score", {"visit": "1722"}, registry=assistant.REGISTRY)
    visit = Visit.objects.get(key="1722")
    assert "scored on its own" in out["how"] and "mean of its scored records" in out["how"]
    assert len(out["records"]) == 3 and out["records_scored"] == visit.records_scored
    scores = [r["quality_score"] for r in out["records"] if r["quality_score"] is not None]
    assert out["quality_score"] == float(average_quality(scores)) and out["lowest_score"] == min(scores)
    for record in out["records"]:
        assert (
            round(100 - sum(d["deducted"] for d in record["deductions_by_category"]), 1)
            == (record["quality_score"])
        )
        assert record["url"].endswith(f"#record-{record['record']}")
    with help_tools.bind(viewer):
        rules = assistant_tools.run("list_quality_rules", {}, registry=assistant.REGISTRY)
    assert (
        "for each record" in rules["score"]["formula"]
        and "scored records only" in rules["urgency"]["formula"]
    )


# ------------------------------------------------------------------------------------------ panels
def test_the_partner_and_pd_panels_count_records_as_the_page_they_open(built, fm_world, monkeypatch):
    from neurodb.fmm import services

    for partner in fm_world.partners.values():
        panel = services.partner_summary(partner.pk, 2026)
        k = metrics.kpis(_scope(partner=partner.pk))
        assert (panel["visits"], panel["records"]) == (k["visits"], k["records"])
    for pd in fm_world.pds.values():
        panel = services.pd_summary(pd.pk, 2026)
        if panel is None:
            continue
        k = metrics.kpis(_scope(pd=pd.pk))
        assert (panel["visits"], panel["records"], panel["avg_quality"]) == (
            k["visits"],
            k["records"],
            k["avg_quality"],
        )


def test_the_partner_page_shows_its_records(built, fm_world, client_viewer, monkeypatch):
    monkeypatch.setattr("neurodb.reports.views._this_year", lambda: 2026)
    mercy = fm_world.partners["mercy"]
    html = client_viewer.get(reverse("reports:partner_profile", args=[mercy.pk])).content.decode()
    k = metrics.kpis(_scope(partner=mercy.pk))
    assert f"FM visits in 2026 · {k['records']} records" in html


# ------------------------------------------------------------------------------------------ action points
def test_an_automatic_action_point_links_to_its_record(built, client_viewer):
    point = LocalActionPoint.objects.create(
        title="Follow up on R8 — FM-2026-023 · LEB/SSFA2024001",
        visit_key="1723",
        record=111,
        source="auto",
        created_by_name="NeuroDB",
    )
    page = client_viewer.get(reverse("reports:action_points")).content.decode()
    assert f"{reverse('fmm:visit', args=['1723'])}#record-111" in page
    visit_page = client_viewer.get(reverse("fmm:visit", args=["1723"])).content.decode()
    assert 'href="#record-111"' in visit_page and point.title in visit_page


# ------------------------------------------------------------------------------------------ clean-up
def test_the_visits_keep_no_rule_results_of_their_own(built):
    from neurodb.fmm import models

    assert not hasattr(models, "VisitRuleResult")
    tables = connection.introspection.table_names()
    assert "fmm_visitruleresult" not in tables and "fmm_recordruleresult" in tables
    assert rule_states("1722")  # a rule over a visit is read from its records


def test_the_clean_up_keeps_the_legacy_checks_until_none_is_left(db, legacy_checks_table):
    """Migration 0020 drops the table of the checks made per visit only when it is empty; a database that
    still has some keeps it for the carry-over (a later migration drops it)."""
    import importlib

    from neurodb.fmm.ai import checks

    cleanup = importlib.import_module("neurodb.fmm.migrations.0020_records_cleanup")
    legacy_checks_table.objects.create(
        visit_key="1727",
        rule="R3",
        input_hash="x",
        prompt_hash="y",
        passed=True,
        checked_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
    )
    with connection.schema_editor() as editor:
        cleanup.drop_legacy_checks_when_done(None, editor)
    assert checks.legacy_table() and checks.legacy_left() == 1  # kept: one is left to carry over
    legacy_checks_table.objects.all().delete()
    with connection.schema_editor() as editor:
        cleanup.drop_legacy_checks_when_done(None, editor)
    assert not checks.legacy_table() and checks.legacy_left() == 0  # dropped once empty
    with connection.schema_editor() as editor:
        cleanup.drop_legacy_checks_when_done(None, editor)  # and again: nothing to do
