"""The figures of the Quality and Analysis tabs (``fmm.metrics``), on ``fm_world`` built and scored on 5
October 2026: 8 visits ending in 2026 (6 reported, 1 in progress, 1 cancelled), 6 of them scored
(40.4% to 78.6%, average 64.4%). Each block counts visits; its drill values are codes the scope reads."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache

from neurodb.datamart import fm
from neurodb.fmm import metrics
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import RuleSetting, Visit, VisitRuleResult
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


# ------------------------------------------------------------------------------------------ key figures
def test_key_figures_and_the_status_breakdown(built):
    k = metrics.kpis(_scope())
    assert (k["visits"], k["entities"], k["entities_rated"], k["entities_not_monitored"]) == (8, 12, 8, 4)
    assert {row["group"]: row["n"] for row in k["by_status"]} == {
        "reported": 6,
        "in_progress": 1,
        "planned": 0,
        "cancelled": 1,
    }
    assert (k["avg_quality"], k["scored"], k["high_urgency"], k["amber"]) == (Decimal("64.4"), 6, 0, 1)
    # the rated entities by rating: shares of ratings are over these only
    assert k["entity_ratings"] == {"on_track": 6, "constrained": 0, "off_track": 2}


def test_one_average_quality_in_the_key_figure_and_the_highlights(built):
    for params in ({}, {"governorate": "north"}, {"rating": "on_track"}, {"partner": "1"}):
        scope = _scope(**params)
        kpi = metrics.kpis(scope)["avg_quality"]
        assert metrics.highlights(scope)["avg_quality"] == kpi == metrics.avg_quality(scope.visits())
    assert metrics.highlights(_scope())["avg_quality"] == Decimal("64.4")


# ------------------------------------------------------------------------------------------ Quality tab
def test_quality_and_visits_by_month(built):
    quality = metrics.monthly_quality(_scope())
    assert quality["months"][0] == "Jan 2026" and quality["months"][-1] == "Oct 2026"  # up to this month
    values = dict(zip(quality["drill"]["labels"], quality["indicators"][0]["values"], strict=True))
    assert values["2026-05"] == 64.7 and values["2026-01"] is None and values["2026-09"] is None
    reports = dict(zip(quality["drill"]["labels"], quality["indicators"][0]["reports"], strict=True))
    assert reports["2026-09"] == 0 and sum(reports.values()) == 6  # reported visits only
    assert quality["bar_name"] == "Average quality" and quality["line_name"] == "Reports"
    volume = metrics.monthly_volume(_scope())
    visits = dict(zip(volume["drill"]["labels"], volume["indicators"][0]["values"], strict=True))
    assert sum(visits.values()) == 8 and visits["2026-09"] == 1  # whatever their status
    assert volume["line_unit"] == "%"
    assert metrics.monthly_volume(_scope(year="2025")) == {} == metrics.monthly_quality(_scope(year="2025"))


def test_hact_q1_by_month_counts_visits_by_their_worst_q1(built):
    data = metrics.hact_q1_by_month(_scope())
    assert {t["code"]: t["n"] for t in data["totals"]} == {"on_track": 1, "constrained": 2, "off_track": 2}
    months = data["drill"]["labels"]
    assert data["series"]["Off track"][months.index("2026-05")] == 1  # 1722, one visit for three entities
    assert data["series"]["Constrained"][months.index("2026-06")] == 1  # 1723
    assert data["series"]["Constrained"][months.index("2026-08")] == 1  # 1727, its visit-level Q1
    assert data["drill"]["series"] == {
        "On track": "on_track",
        "Constrained": "constrained",
        "Off track": "off_track",
    }
    assert sum(sum(v) for v in data["series"].values()) == Visit.objects.exclude(hact_q1="").count() == 5
    assert data["colors"]["Off track"] == "--nd-danger"


def test_without_q1_answers_the_chart_counts_the_overall_rating(built):
    Visit.objects.update(hact_q1="")
    assert metrics.hact_q1_by_month(_scope()) is None
    data = metrics.rating_by_month(_scope())
    assert {t["code"]: t["n"] for t in data["totals"]} == {
        "on_track": 3,
        "constrained": 0,
        "off_track": 2,
        "not_monitored": 1,  # Not monitored: reported visits with nothing rated (planned, not conducted)
    }
    assert data["key"] == "rating"


def test_score_buckets_hold_100_in_the_top_bucket_and_take_their_band_colour(built):
    data = metrics.score_buckets(_scope())
    assert [(i["drill"], i["value"]) for i in data["items"] if i["value"]] == [
        ("40-50", 1),
        ("50-60", 1),
        ("60-70", 1),
        ("70-80", 3),
    ]
    assert len(data["items"]) == 10 and data["not_scored"] == 2
    bands = {i["drill"]: (i["band"], i["color"]) for i in data["items"]}
    assert bands["40-50"] == ("low", "--nd-danger") and bands["50-60"] == ("medium", "--nd-warning")
    assert bands["70-80"] == ("medium", "--nd-warning") and bands["80-90"] == ("high", "--nd-success")
    Visit.objects.filter(key="1722").update(quality_score=Decimal("100.0"))
    Visit.objects.filter(key="1723").update(quality_score=Decimal("90.0"))
    cache.clear()
    top = metrics.score_buckets(_scope())["items"][-1]
    assert top["value"] == 2 == len(_keys(_scope(bucket="90-100")))
    assert len(_keys(_scope(bucket="80-100"))) == 2  # a bucket of Release 1's links still opens its visits
    # the colours follow the bands as Score settings set them
    limits = {**metrics.thresholds(), "band_medium": 40}
    assert metrics.score_buckets(_scope(), limits=limits)["items"][4]["band"] == "medium"


def test_top_issues_are_grouped_by_rule_and_detail(built):
    issues = metrics.top_issues(_scope(), 10)
    first = issues[0]
    assert first["label"] == "R1: Incomplete monitoring report — missing: General observation (narrative)"
    assert first["visits"] == 4 and first["drill"] == "R1:missing:narrative"
    assert {c["key"] for c in first["chips"]} == {REFERENCE_KEY, "1727", "1723", "1728"}
    by_drill = {row["drill"]: row for row in issues}
    assert by_drill["R2:below_threshold"]["label"] == (
        "R2: fewer than 80% of questions answered (lowest 33.3%)"
    )
    assert by_drill["R4:too_short"]["visits"] == 2
    # one row per (rule, detail): every failed result is in exactly one of them
    assert sum(row["visits"] for row in issues) == VisitRuleResult.objects.filter(status="fail").count()
    # mean urgency of the visits behind the issue
    urgencies = Visit.objects.filter(key__in=[c["key"] for c in first["chips"]]).values_list(
        "urgency", flat=True
    )
    assert first["urgency"] == round(sum(urgencies) / 4)
    assert metrics.top_issues(_scope(year="2025"), 10) == []


def test_rule_analysis_flagged_out_of_evaluated(built):
    rows = {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}
    assert (rows["R1"]["flagged"], rows["R1"]["evaluated"], rows["R1"]["state"]) == (4, 6, "ok")
    assert rows["R1"]["level"] == "danger"  # 66.7% flagged
    assert rows["R6"]["flag_only"] and rows["R6"]["points"] == 0
    RuleSetting.objects.filter(code="R5").update(enabled=False)
    cache.clear()
    assert {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}["R5"]["state"] == "off"


def test_rules_needing_answers_are_not_available_without_them(built):
    VisitRuleResult.objects.filter(rule__in=("R2", "R3", "R5")).update(status="na", points=0)
    rows = {r["code"]: r for r in metrics.rule_analysis(_scope(), _rules())}
    assert rows["R2"]["state"] == "na" and rows["R2"]["needs_answers"]
    assert rows["R1"]["state"] == "ok"


def test_issues_summary_monitoring_gaps_are_reported_visits_with_nothing_rated(built):
    data = metrics.issues_summary(_scope())
    # 1723 is reported with no rated entity; 1724 (in progress) and 1725 (cancelled) are not gaps
    assert data["gaps"]["n"] == 1
    assert _keys(_scope(rating="not_monitored", status="reported")) == {"1723"}
    assert data["high_flag"]["n"] == 1 and data["high_flag"]["at"] == 3  # 1723: R1, R2, R5
    assert data["r6"]["n"] == len(_keys(_scope(flag="R6")))
    assert data["high_flag"]["pct"] == Decimal("16.7")


def test_flag_distribution_counts_scored_visits(built):
    data = metrics.flag_distribution(_scope())
    counts = {r["drill"]: r["n"] for r in data["rows"]}
    assert sum(counts.values()) == data["scored"] == 6
    for drill, n in counts.items():
        assert len(_keys(_scope(flags=drill))) == n


def test_places_show_a_dash_without_a_scored_visit_and_coverage(built):
    places = metrics.locations(_scope())
    by_name = {p["name"]: p for p in places["rows"]}
    assert by_name["Zahle town"]["visits"] == 2 and by_name["Zahle town"]["last"] == datetime.date(
        2026, 5, 12
    )
    assert by_name["Douris"]["coverage"] == Decimal("0.0")  # 1723: nothing rated
    assert places["unlinked"] == 0
    only_planned = metrics.locations(_scope(status="in_progress"))["rows"]
    assert [(p["name"], p["avg"]) for p in only_planned] == [("Qalamoun", None)]
    assert sum(p["visits"] for p in places["rows"]) == 8


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
        "LEB/PCA2023597/PD2025123-2",
        "LEB/PCA2024100/PD2026010",
        "LEB/PCA2023597/PD2025123",
    ]
    assert [r["avg"] for r in rows] == [Decimal("58.7"), Decimal("68.7"), Decimal("75.7"), None]
    amended = rows[1]
    assert amended["link"] == ("pd", fm_world.pds["amended"].pk) and amended["planned"] == 2
    partners = metrics.entities_performance(_scope(), "partner")["rows"]
    assert {r["name"] for r in partners} == {"AMEL", "MCL"}
    assert metrics.entity_rows(_scope())["kinds"] == {"pd": 9, "cp_output": 1, "partner": 2}


def test_offices_sections_and_ratings_count_each_visit_in_each_of_its_groups(built):
    offices = metrics.offices(_scope())
    assert [(o["name"], o["visits"]) for o in offices["rows"]] == [("Tripoli", 3), ("Zahle", 1)]
    assert offices["unknown"]["visits"] == 4
    assert offices["sources"] == {"activity": 1, "pd": 3, "action_point": 0}
    for row in offices["rows"]:
        assert len(_keys(_scope(office=row["name"]))) == row["visits"]
    sections = {s["name"]: s for s in metrics.sections(_scope())}
    assert sections["Education"]["visits"] == 3 and sections["none"]["visits"] == 5
    lines = sections["Education"]["lines"]
    assert [ln["key"] for ln in lines] == [REFERENCE_KEY, "1726", "1724"]  # worst first, unscored last
    ratings = {r["code"]: r["visits"] for r in metrics.quality_by_rating(_scope())}
    assert ratings == {"on_track": 3, "off_track": 2, "not_monitored": 1}
    # a filter keeps its own rows only
    assert [s["name"] for s in metrics.sections(_scope(section="Education"))] == ["Education"]


def test_office_rule_badges(built):
    badges = {o["name"]: o for o in metrics.office_rule_badges(_scope(), _rules())}
    assert badges["Zahle"]["scored"] == 1
    assert [(b["rule"], b["flagged"], b["total"], b["level"]) for b in badges["Zahle"]["badges"]] == [
        ("R3", 1, 1, "danger"),
        ("R4", 1, 1, "danger"),
    ]


def test_points_per_rule_are_capped_at_100(built):
    rows = {r["code"]: r for r in metrics.dimension_breakdown(_scope(), _rules())["rows"]}
    assert all(r["pct"] <= 100 for r in rows.values())
    assert metrics.dimension_breakdown(_scope(), _rules())["flag_only"] == ["R6"]
    result = VisitRuleResult.objects.filter(rule="R2", status="pass").first()
    VisitRuleResult.objects.filter(rule="R2").update(points=Decimal("40.0"))  # above its 20 points
    cache.clear()
    r2 = {r["code"]: r for r in metrics.dimension_breakdown(_scope(), _rules())["rows"]}["R2"]
    assert result is not None and r2["pct"] == Decimal("100.0") and r2["earned"] == r2["max"]
    # weakest first
    ordered = metrics.dimension_breakdown(_scope(), _rules())["rows"]
    assert [r["pct"] for r in ordered] == sorted(r["pct"] for r in ordered)


def test_flag_frequency_triples_carry_the_rule_as_drill(built):
    data = metrics.flag_frequency(_scope(), _rules())
    assert data["pairs"][0] == ["R1 Completeness", 4, "R1"]
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
    assert metrics.notes(_scope()) == []
    assert [n["key"] for n in metrics.notes(_scope(section="Education"))] == ["section"]
    assert [n["key"] for n in metrics.notes(_scope(entity_type="pd"))] == ["entity_filter"]
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
