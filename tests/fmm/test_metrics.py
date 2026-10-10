"""The figures of the Quality and Analysis tabs (``fmm.metrics``), on ``fm_world`` built and scored on 5
October 2026: 8 visits dated in 2026 (6 reported, 1 in progress, 1 cancelled) holding 12 records, 10 of
them scored (48% to 95%, average 80.3%). As in FMS, quality, ratings, flags, rules and urgency count
records; visits, statuses, places, PSEA, the gaps and follow-up count visits. Drill values are codes the
scope reads."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db.models import F

from neurodb.datamart import fm
from neurodb.fmm import metrics
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import RecordRuleResult, RuleSetting, Visit, VisitEntity
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
REFERENCE_KEY = fm.visit_key(None, "FM/2026/9", 0)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)
    cache.clear()


def _scope(**params) -> Scope:
    return Scope.from_params({"section": "", **params})


def _rules() -> list[RuleSetting]:
    return list(RuleSetting.objects.order_by("code"))


def _keys(scope: Scope) -> set[str]:
    return set(scope.visits().values_list("key", flat=True))


def _records(scope: Scope) -> int:
    return scope.records().count()


# ------------------------------------------------------------------------------------------ key figures
def test_key_figures_and_the_status_breakdown(built):
    k = metrics.kpis(_scope())
    assert (k["visits"], k["records"], k["records_rated"], k["records_not_monitored"]) == (8, 12, 8, 2)
    assert {row["group"]: row["n"] for row in k["by_status"]} == {
        "reported": 6,
        "in_progress": 1,
        "planned": 0,
        "cancelled": 1,
    }
    # per record: 10 scored (80.3 on average), 1723's two records amber; 6 scored visits
    assert (k["avg_quality"], k["scored"], k["high_urgency"], k["amber"]) == (Decimal("80.3"), 10, 0, 2)
    assert k["scored_visits"] == 6 and (k["records_not_rated_yet"], k["records_other"]) == (1, 1)
    # the rated records by rating: shares of ratings are over these only
    assert k["record_ratings"] == {"on_track": 6, "constrained": 0, "off_track": 2}


def test_one_average_quality_in_the_key_figure_and_the_highlights(built):
    for params in ({}, {"governorate": "north"}, {"rating": "on_track"}, {"partner": "1"}):
        scope = _scope(**params)
        kpi = metrics.kpis(scope)["avg_quality"]
        assert metrics.highlights(scope)["avg_quality"] == kpi == metrics.avg_quality(scope.records())
    assert metrics.highlights(_scope())["avg_quality"] == Decimal("80.3")


# ------------------------------------------------------------------------------------------ Quality tab
def test_quality_and_visits_by_month(built):
    quality = metrics.monthly_quality(_scope())
    assert quality["months"][0] == "Jan 2026" and quality["months"][-1] == "Oct 2026"  # up to this month
    values = dict(zip(quality["drill"]["labels"], quality["indicators"][0]["values"], strict=True))
    assert values["2026-05"] == 94.3 and values["2026-01"] is None and values["2026-09"] is None
    reports = dict(zip(quality["drill"]["labels"], quality["indicators"][0]["reports"], strict=True))
    assert reports["2026-09"] == 0 and sum(reports.values()) == 10  # the records of reported visits
    assert quality["bar_name"] == "Average quality score" and quality["line_name"] == "Total reports"
    volume = metrics.monthly_volume(_scope())
    records = dict(zip(volume["drill"]["labels"], volume["indicators"][0]["values"], strict=True))
    assert sum(records.values()) == 12 and records["2026-05"] == 3  # whatever their status
    visits = dict(zip(volume["drill"]["labels"], volume["indicators"][0]["extra"], strict=True))
    assert sum(visits.values()) == 8 and visits["2026-05"] == 1  # the tooltip's distinct visits
    assert volume["line_unit"] == "%" and (volume["bar_name"], volume["line_name"]) == (
        "Record count",
        "Avg quality %",
    )
    assert volume["extra_label"] == "visits"
    assert metrics.monthly_volume(_scope(year="2025")) == {} == metrics.monthly_quality(_scope(year="2025"))


def test_hact_q1_by_month_counts_records_by_their_own_q1(built):
    data = metrics.hact_q1_by_month(_scope())
    assert {t["code"]: t["n"] for t in data["totals"]} == {"on_track": 3, "constrained": 2, "off_track": 2}
    months = data["drill"]["labels"]
    assert data["series"]["Off track"][months.index("2026-05")] == 1  # 1722's CP output
    assert data["series"]["On track"][months.index("2026-05")] == 1  # 1722's PD (its partner: no Q1)
    assert data["series"]["Constrained"][months.index("2026-06")] == 1  # 1723's SSFA
    assert data["series"]["Constrained"][months.index("2026-08")] == 1  # 1727, its visit-level Q1
    assert data["drill"]["series"] == {
        "On track": "on_track",
        "Constrained": "constrained",
        "Off track": "off_track",
    }
    assert (
        sum(sum(v) for v in data["series"].values()) == VisitEntity.objects.exclude(hact_q1="").count() == 7
    )
    assert data["colors"]["Off track"] == "--nd-rating-off-track"  # FMS's rating colours


def test_without_q1_answers_the_chart_counts_the_overall_rating(built):
    VisitEntity.objects.update(hact_q1="")
    assert metrics.hact_q1_by_month(_scope()) is None
    data = metrics.rating_by_month(_scope())
    assert {t["code"]: t["n"] for t in data["totals"]} == {
        "on_track": 6,
        "constrained": 0,
        "off_track": 2,
        "not_monitored": 2,  # Not monitored: records of reported visits with nothing rated (1723's two)
    }
    assert data["key"] == "rating"


def test_score_buckets_are_fms_five_and_hold_100_in_the_top_one(built):
    data = metrics.score_buckets(_scope())
    assert [(i["drill"], i["value"]) for i in data["items"] if i["value"]] == [
        ("40-60", 2),  # 1723's two records
        ("60-80", 1),
        ("80-100", 7),
    ]
    assert len(data["items"]) == 5 and data["not_scored"] == 2
    bands = {i["drill"]: (i["band"], i["color"]) for i in data["items"]}
    assert bands["40-60"] == ("low", "--fmm-score-2") and bands["60-80"] == ("medium", "--fmm-score-3")
    assert bands["80-100"] == ("high", "--fmm-score-4")
    VisitEntity.objects.filter(visit__key="1722").update(quality_score=Decimal("100.0"))
    VisitEntity.objects.filter(visit__key="1723").update(quality_score=Decimal("90.0"))
    cache.clear()
    top = metrics.score_buckets(_scope())["items"][-1]
    assert top["value"] == 9 == _records(_scope(bucket="80-100"))  # 100 and 90 are in 80-100
    assert _records(_scope(bucket="90-100")) == 6  # a bucket of 10 of an older link still opens its records
    # the bands follow Score settings (the colours are FMS's, fixed)
    limits = {**metrics.thresholds(), "band_medium": 40}
    assert metrics.score_buckets(_scope(), limits=limits)["items"][2]["band"] == "medium"


def test_top_issues_are_grouped_by_rule_and_detail(built):
    issues = metrics.top_issues(_scope(), 50)
    first = issues[0]
    assert first["label"] == "R23: Visit location not among registered PD locations for partner"
    # four records (1722's CP output, 1723's two, 1728's) of three visits
    assert (first["records"], first["visits"], first["drill"]) == (4, 3, "R23:not_registered")
    assert {c["key"] for c in first["chips"]} == {"1722", "1723", "1728"}
    assert [c["key"] for c in first["chips"]][0] == "1723"  # the visit of the most urgent record first
    by_drill = {row["drill"]: row for row in issues}
    # 1723's SSFA answered 33.3% of its questions, its partner none
    assert by_drill["R2:band"]["label"] == "R2: Only 0–33.3% of monitoring questions answered (target: 80%+)"
    assert by_drill["R1:missing:0,3"]["records"] == 2  # the same fields missing
    # an AI check's issue is its rule's flag without the explanation, which differs from visit to visit
    assert by_drill["R6:ai"]["label"] == (
        "R6: General Observation is incoherent, duplicates Q2, or does not address visit objective"
    )
    # one row per (rule, detail): every failed result of a record in the scope is in exactly one of them
    in_scope = RecordRuleResult.objects.filter(status="fail", entity__in=_scope().records())
    assert sum(row["records"] for row in issues) == in_scope.count()
    # mean urgency of the records behind the issue: 1722's CP output 18, 1723's two 58, 1728's 14
    assert first["urgency"] == round((18 + 58 + 58 + 14) / 4) == 37
    assert metrics.top_issues(_scope(year="2025"), 10) == []


def test_rule_analysis_flagged_out_of_evaluated(built):
    rows = {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}
    assert (rows["R1"]["flagged"], rows["R1"]["evaluated"], rows["R1"]["state"]) == (8, 10, "ok")
    assert rows["R1"]["level"] == "danger"  # 80% of the records flagged
    assert (rows["R6"]["flagged"], rows["R6"]["level"], rows["R6"]["category"]) == (3, "warning", "coherence")
    # R19 reads the whole visit and counts on each of its records, as FMS counts it
    assert rows["R19"]["state"] == "none" and rows["R19"]["evaluated"] == 0
    assert rows["R23"]["flag_only"] and rows["R23"]["points"] == 0
    assert rows["R4"]["state"] == "off"  # FMS Lebanon's rule set has it switched off
    assert [r["code"] for r in rows.values()][:3] == ["R1", "R2", "R3"]  # in the order of the ids
    RuleSetting.objects.filter(code="R5").update(enabled=False)
    cache.clear()
    assert {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}["R5"]["state"] == "off"


def test_rules_needing_answers_are_not_available_without_them(built):
    RecordRuleResult.objects.filter(rule__in=("R2", "R3", "R5")).update(status="na", points=0)
    rows = {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}
    assert rows["R2"]["state"] == "na" and rows["R2"]["needs_answers"]
    assert rows["R1"]["state"] == "ok"


def test_issues_summary_monitoring_gaps_are_reported_visits_with_nothing_rated(built):
    data = metrics.issues_summary(_scope())
    # 1723 is reported with no rated entity; 1724 (in progress) and 1725 (cancelled) are not gaps
    assert data["gaps"]["n"] == 1
    assert _keys(_scope(rating="not_monitored", status="reported")) == {"1723"}
    # records with 3 flags or more: 1723's two (5 each) and 1726's two (R1, R8, R32)
    assert data["high_flag"]["n"] == 4 and data["high_flag"]["at"] == 3
    assert data["r6"]["n"] == _records(_scope(flag="R6")) == 3  # 1723's two records and 1727's
    assert data["high_flag"]["pct"] == Decimal("40.0")  # of the 10 scored records


def test_flag_distribution_counts_scored_records(built):
    data = metrics.flag_distribution(_scope())
    counts = {r["drill"]: r["n"] for r in data["rows"]}
    assert sum(counts.values()) == data["scored"] == 10
    assert counts == {"0": 0, "1": 1, "2": 5, "3+": 4}
    for drill, n in counts.items():
        assert _records(_scope(flags=drill)) == n


def test_places_show_a_dash_without_a_scored_visit_and_coverage(built):
    places = metrics.locations(_scope())
    by_name = {p["name"]: p for p in places["rows"]}
    assert by_name["Zahle town"]["visits"] == 2 and by_name["Zahle town"]["last"] == datetime.date(
        2026, 5, 11
    )  # 1722's start date
    assert by_name["Zahle town"]["records"] == 4  # 1722's three and 1725's one
    assert by_name["Zahle town"]["coverage"] == Decimal("75.0")  # 3 rated of 4
    assert by_name["Douris"]["coverage"] == Decimal("0.0")  # 1723: nothing rated
    assert places["unlinked"] == 0
    only_planned = metrics.locations(_scope(status="in_progress"))["rows"]
    assert [(p["name"], p["avg"]) for p in only_planned] == [("Qalamoun", None)]
    assert sum(p["visits"] for p in places["rows"]) == 8 and sum(p["records"] for p in places["rows"]) == 12


# ------------------------------------------------------------------------------------------ Analysis tab
def test_governorate_gaps(built):
    assert metrics.governorate_gaps(_scope())["names"] == []
    may_june = _scope(preset="custom", **{"from": "2026-05-01", "to": "2026-06-30"})
    assert metrics.governorate_gaps(may_june)["names"] == ["North"]
    assert metrics.coverage(may_june)["covered"] == 1 and metrics.coverage(may_june)["total"] == 2
    assert metrics.governorate_gaps(_scope(governorate="bekaa"))["names"] == []


def test_entity_performance_worst_first_unscored_last(built, fm_world):
    rows = metrics.entities_performance(_scope(), "pd")["rows"]
    assert [r["name"] for r in rows] == [
        "LEB/SSFA2024001",
        "LEB/PCA2024100/PD2026010",
        "LEB/PCA2023597/PD2025123-2",
        "LEB/PCA2023597/PD2025123",
    ]
    # each entity's own records: the amended PD's 95 (1722) and 80 (1727)
    assert [r["avg"] for r in rows] == [Decimal("76.3"), Decimal("81.5"), Decimal("87.5"), None]
    assert [r["records"] for r in rows] == [3, 3, 2, 1] and [r["visits"] for r in rows] == [3, 3, 2, 1]
    amended = rows[2]
    assert amended["link"] == ("pd", fm_world.pds["amended"].pk) and amended["planned"] == 2
    partners = metrics.entities_performance(_scope(), "partner")["rows"]
    assert {r["name"] for r in partners} == {"Amel Association", "Mercy Corps Lebanon"}
    assert metrics.entity_rows(_scope())["kinds"] == {"pd": 9, "cp_output": 1, "partner": 2}


def test_offices_sections_and_ratings_count_each_record_in_each_of_its_groups(built):
    offices = metrics.offices(_scope())
    assert [(o["name"], o["records"], o["visits"]) for o in offices["rows"]] == [
        ("Tripoli", 4, 3),
        ("Zahle", 3, 1),
    ]
    assert (offices["unknown"]["records"], offices["unknown"]["visits"]) == (5, 4)
    assert offices["sources"] == {"activity": 1, "pd": 3, "action_point": 0}  # visits
    for row in offices["rows"]:
        scope = _scope(office=row["name"])
        assert (_records(scope), len(_keys(scope))) == (row["records"], row["visits"])
    sections = {s["name"]: s for s in metrics.sections(_scope())}
    assert (sections["Education"]["records"], sections["Education"]["visits"]) == (4, 3)
    assert (sections["none"]["records"], sections["none"]["visits"]) == (8, 5)
    lines = sections["Education"]["lines"]
    # one line per record, worst first, unscored last
    assert [(ln["key"], ln["record"]) for ln in lines] == [
        (REFERENCE_KEY, 161),
        ("1726", 121),
        ("1726", 122),
        ("1724", 141),
    ]
    ratings = {r["code"]: (r["records"], r["visits"]) for r in metrics.quality_by_rating(_scope())}
    assert ratings == {"on_track": (6, 4), "off_track": (2, 2), "not_monitored": (2, 1)}
    # a filter keeps its own rows only
    assert [s["name"] for s in metrics.sections(_scope(section="Education"))] == ["Education"]


def test_office_rule_badges(built):
    badges = {o["name"]: o for o in metrics.office_rule_badges(_scope(), _rules())}
    assert badges["Zahle"]["scored"] == 3  # 1722's three records
    assert [(b["rule"], b["flagged"], b["total"], b["level"]) for b in badges["Zahle"]["badges"]] == [
        ("R1", 1, 3, "warning"),
        ("R23", 1, 3, "warning"),
        ("R7", 3, 3, "danger"),
    ]


def test_points_by_category_keep_between_0_and_the_weight(built):
    data = metrics.dimension_breakdown(_scope(), _rules())
    rows = {r["code"]: r for r in data["rows"]}
    assert set(rows) == {"completeness", "evidence", "alignment", "coherence", "q3_quality", "actionability"}
    assert all(0 <= r["pct"] <= 100 for r in rows.values()) and data["flag_only"] == ["R23"]
    # the points kept on average: 1723's two records and 1727's lost 15 of coherence's 15 -> 10.5 of 15
    assert (rows["coherence"]["earned"], rows["coherence"]["max"], rows["coherence"]["records"]) == (
        Decimal("10.5"),
        Decimal("15.0"),
        10,
    )
    VisitEntity.objects.update(category_deductions={"completeness": 45, "evidence": 0})  # more than its 30
    cache.clear()
    rows = {r["code"]: r for r in metrics.dimension_breakdown(_scope(), _rules())["rows"]}
    assert rows["completeness"]["pct"] == Decimal("0.0") and rows["completeness"]["earned"] == 0
    assert (
        rows["evidence"]["pct"] == Decimal("100.0") and rows["evidence"]["earned"] == rows["evidence"]["max"]
    )
    # weakest first
    ordered = metrics.dimension_breakdown(_scope(), _rules())["rows"]
    assert [r["pct"] for r in ordered] == sorted(r["pct"] for r in ordered)


def test_flag_frequency_triples_carry_the_rule_as_drill(built):
    data = metrics.flag_frequency(_scope(), _rules())
    assert data["pairs"][0] == ["R1 Report Completeness", 8, "R1"]
    assert all(len(p) == 3 and p[2].startswith("R") for p in data["pairs"])


def test_hact_programmatic_visits(built, fm_world):
    data = metrics.hact_programmatic(_scope())
    assert data["year"] == 2026
    assert data["rows"] == [
        {
            "partner_id": fm_world.partners["amel"].pk,
            "name": "AMEL",
            "required": 2,
            "planned": 2,
            "completed": 1,
            "fm": fm.programmatic_visits_by_partner(2026)[fm_world.partners["amel"].pk],
            "gap": 1,
        }
    ]
    assert metrics.hact_programmatic(_scope(partner=str(fm_world.partners["mercy"].pk)))["rows"] == []


def test_follow_up_action_points(built):
    data = metrics.action_points(_scope())
    # 8001 open (due 1 Dec), 8002 completed, 8004 open, high priority and overdue since 1 Sep
    assert (data["linked"], data["open"], data["overdue"], data["high_open"]) == (3, 2, 1, 1)
    assert [p["visits"][0]["key"] for p in data["overdue_list"]] == ["1726"]
    without = data["without"]
    assert without["n"] == len(
        [
            v
            for v in Visit.objects.filter(status_group="reported", action_points=0)
            if "off_track" in (v.rating, v.hact_q1) or "constrained" in (v.rating, v.hact_q1)
        ]
    )


# ------------------------------------------------------------------------------------------ periods, notes
def test_the_previous_period_has_its_own_figures(built):
    scope = _scope()
    previous = scope.previous()
    assert (previous.start, previous.end) == (datetime.date(2025, 1, 1), datetime.date(2025, 10, 5))
    assert metrics.kpis(previous)["visits"] == 0 and metrics.kpis(scope)["visits"] == 8
    assert previous.hash() != scope.hash()


def test_the_data_notes_appear_only_when_they_apply(built):
    # every visit of fm_world but 1722 has no start date: dated by its end, and counted
    assert metrics.notes(_scope()) == [{"key": "dated_by_end", "n": 7}]
    Visit.objects.exclude(start_date__isnull=False).update(start_date=F("end_date"))
    cache.clear()
    assert metrics.notes(_scope()) == []
    assert [n["key"] for n in metrics.notes(_scope(section="Education"))] == ["section"]
    assert [n["key"] for n in metrics.notes(_scope(entity_type="pd"))] == ["record_filter"]
    assert [n["key"] for n in metrics.notes(_scope(flag="R1"))] == ["record_filter"]  # a record drill too
    Visit.objects.filter(key="1728").update(location=None)  # placed by its site only
    cache.clear()
    assert {"key": "via_site", "n": 1} in metrics.notes(_scope(governorate="bekaa"))


def test_blocks_are_kept_per_scope_and_refresh(built, settings):
    settings.DEBUG = False
    scope = _scope()
    first = metrics.summary(scope)
    Visit.objects.filter(key="1722").delete()
    assert metrics.summary(scope)["visits"] == first["visits"] == 8  # kept ten minutes
    assert metrics.summary(scope, when="another-refresh")["visits"] == 7


def test_the_rule_blocks_read_the_rules_themselves_when_not_given(built):
    scope = _scope()
    assert metrics.rule_analysis(scope) == metrics.rule_analysis(scope, _rules())
    assert metrics.flag_frequency(scope) == metrics.flag_frequency(scope, _rules())
    assert metrics.dimension_breakdown(scope) == metrics.dimension_breakdown(scope, _rules())
    assert metrics.office_rule_badges(scope) == metrics.office_rule_badges(scope, _rules())
