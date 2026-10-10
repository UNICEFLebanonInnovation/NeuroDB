"""Monitoring insights counted per record (Release 2 step 5, stage F3a): as FMS does, quality, ratings,
flags, rules and urgency count records, each scored on its own; "Monitoring visits" and the status tiles
count distinct visits. A filter gives the same numbers in both units (the visits are the distinct visits
of the records counted), every block's drill-down window lists the records it counted, and the pages say
which unit each figure counts.

On ``fm_world`` built on 5 October 2026 (conftest ``built``): 8 visits holding 12 records; 1722 holds
three (a PD 95, a CP output 95, its partner 93), 1723 two (48 each, amber), 1726 two (88 each).
"""

from __future__ import annotations

import datetime
import importlib
import re
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.http import QueryDict
from django.urls import reverse

from neurodb.fmm import metrics, views
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import RecordRuleResult, RuleSetVersion, Visit, VisitEntity
from neurodb.fmm.scope import Scope
from neurodb.fmm.score import average_quality

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
DRILL = reverse("fmm:drill")
SCOPES = (
    "year=2026&section=",
    "year=2026&section=&entity_type=partner",
    "year=2026&section=&entity_type=pd&governorate=north",
    "year=2026&section=&rating=on_track",
    "year=2026&section=&quality=high",
    "year=2026&section=&urgency_level=medium",
    "year=2026&section=&flag=R7",
    "year=2026&section=&hact_q1=constrained",
    "year=2026&section=&status=reported&bucket=80-100",
    "year=2026&section=&office=Tripoli&rule=R1&rule_state=fail",
)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)
    cache.clear()


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _drill(client, url: str) -> tuple[int, int, list[int]]:
    """(records, visits, the records listed) of a drill-down window."""
    html = client.get(url.replace("&amp;", "&"), HTTP_HX_REQUEST="true").content.decode()
    found = re.search(r'id="modal-title">(\d+) records? <span[^>]*>· (\d+) visits?</span>', html)
    return (
        int(found.group(1)),
        int(found.group(2)),
        [int(n) for n in re.findall(r'data-record="(\d+)"', html)],
    )


# ------------------------------------------------------------------------------------------ the scope
@pytest.mark.parametrize("query", SCOPES)
def test_the_visits_are_the_distinct_visits_of_the_records_counted(built, query):
    scope = _scope(query)
    records = scope.records()
    visits = set(scope.visits().values_list("pk", flat=True))
    assert visits == set(records.values_list("visit_id", flat=True)), query
    kpis = metrics.kpis(scope)
    assert (kpis["visits"], kpis["records"]) == (len(visits), records.count()), query
    # the one pass counts the same records and visits
    data = metrics.summary(scope)
    assert (data["visits"], data["records"]) == (len(visits), records.count()), query
    # the average is the records' own mean
    assert kpis["avg_quality"] == average_quality(records.values_list("quality_score", flat=True))


@pytest.mark.parametrize("query", (*SCOPES, "issue", "year=2026&section=&review=none&month=2026-05"))
def test_the_checks_of_the_scope_are_those_of_its_records(built, query):
    # the rule figures and the recurring issues read the checks through their record (one join): the
    # same checks as those of the records of the scope
    if query == "issue":  # the most frequent issue's drill-down
        query = f"year=2026&section=&issue={metrics.top_issues(_scope())[0]['drill']}"
    scope = _scope(query)
    expected = set(RecordRuleResult.objects.filter(entity__in=scope.records()).values_list("pk", flat=True))
    found = scope.record_results(RecordRuleResult.objects.all()).values_list("pk", flat=True)
    assert set(found) == expected, query
    assert expected or query.endswith("2026-05"), query


def test_record_filters_read_each_record_and_visit_filters_its_visit(built, fm_world):
    amel = fm_world.partners["amel"]
    scope = _scope(f"year=2026&section=&partner={amel.pk}")
    assert sorted(scope.records().values_list("datamart_id", flat=True)) == [101, 102, 103, 131, 151]
    # a rating filter keeps the records of that rating, not the visits of that worst rating
    off = _scope("year=2026&section=&rating=off_track")
    assert sorted(off.records().values_list("datamart_id", flat=True)) == [102, 161]
    # a visit filter keeps every record of the visits it keeps
    north = _scope("year=2026&section=&governorate=north")
    assert sorted(north.records().values_list("datamart_id", flat=True)) == [121, 122, 141, 161]
    # one record must match every record filter: 1722's partner record is on track, its CP output off
    both = _scope("year=2026&section=&entity_type=partner&rating=off_track")
    assert both.records().count() == 0 and both.visits().count() == 0
    assert _scope("year=2026&section=&visit_rating=off_track").records().count() == 4  # 1722's 3, ref's 1
    assert _scope("year=2026&section=").record_filtered is False
    assert _scope("year=2026&section=&month=2026-05").record_filtered is False  # a visit drill
    assert _scope("year=2026&section=&bucket=none").record_filtered is True


def test_the_pd_filter_reads_the_records_programme_document(built, fm_world):
    education = fm_world.pds["education"]
    scope = _scope(f"year=2026&section=&pd={education.pk}")
    assert sorted(scope.records().values_list("datamart_id", flat=True)) == [122, 141, 161]
    # 1726's other record (its SSFA) is not the programme document's: it is not counted
    assert metrics.kpis(scope)["avg_quality"] == Decimal("81.5")  # 88 and 75


# ------------------------------------------------------------------------------------------ key figures
def test_the_tiles_say_which_unit_they_count(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    text = _text(html)
    for label in ("Monitoring visits 8", "Records 12", "Average quality score (per record) 80.3%"):
        assert label in text, label
    assert "High urgency (records) 0" in text and "on 10 scored records · rules v" in text
    # the Monitoring visits tile opens the list grouped by visit, the Records tile the records list
    kpis = html.split('class="kpi-grid fmm-kpis"', 1)[1]
    found = re.findall(r'kpi__label">([^<]+)</div>\s*<div class="kpi__value"><a href="([^"]+)"', kpis)
    by_label = {label.strip(): url for label, url in found}
    assert "view=visits" in by_label["Monitoring visits"] and "view=" not in by_label["Records"]


def test_the_records_list_grouped_by_visit_counts_the_monitoring_visits(built, client_viewer):
    for query in SCOPES:
        params = dict(QueryDict(query).items())
        kpis = metrics.kpis(_scope(query))
        records = client_viewer.get(PAGE, {**params, "tab": "visits"}, HTTP_HX_REQUEST="true")
        grouped = client_viewer.get(
            PAGE, {**params, "tab": "visits", "view": "visits"}, HTTP_HX_REQUEST="true"
        )
        shown = re.search(r"Showing (\d+) records?", records.content.decode())
        visits = re.search(r"Showing (\d+) visits?", grouped.content.decode())
        n_records = int(shown.group(1)) if shown else 0
        n_visits = int(visits.group(1)) if visits else 0
        assert (n_records, n_visits) == (kpis["records"], kpis["visits"]), query


# ------------------------------------------------------------------------------------------ the briefing
def test_the_briefing_counts_records_and_its_tiles_open_them(built, client_viewer):
    data = metrics.briefing(_scope())
    records = VisitEntity.objects.filter(visit__visit_date__year=2026)
    assert (data["visits"], data["records"]) == (8, 12)
    assert data["avg_quality"] == average_quality(records.values_list("quality_score", flat=True))
    assert data["low"] == records.filter(quality_score__lt=50).count() == 2
    html = client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "insights"}).content.decode()
    briefing = html.split('id="fmm-briefing"', 1)[1].split("</section>", 1)[0]
    found = re.findall(
        r'data-tile="(\w+)".*?<div class="kpi__value">(?:<a href="([^"]*)"[^>]*>)?', briefing, re.S
    )
    tiles = dict(found)
    assert _drill(client_viewer, tiles["low"])[:2] == (2, 1)  # 1723's two records
    assert _drill(client_viewer, tiles["visits"])[:2] == (12, 8)  # the visits' records
    assert _drill(client_viewer, tiles["completed"])[1] == data["statuses"]["completed"] == 6
    # the tiles that count records say so; the glossary sits under the visits tile's ⓘ
    assert "Low quality records" in briefing and "Records · this year" in briefing
    assert str(views.GLOSSARY)[:60] in briefing.replace("&#x27;", "'")


def test_the_governorate_chips_open_their_records(built, client_viewer):
    data = metrics.briefing(_scope())
    for g in data["governorates"]:
        scope = _scope(f"year=2026&section=&governorate={g['key']}")
        assert g["records"] == scope.records().count() and g["visits"] == scope.visits().count()
        assert g["avg_quality"] == metrics.kpis(scope)["avg_quality"]


# ------------------------------------------------------------------------------------------ the Quality tab
def test_the_hact_chips_count_records_by_their_q1_and_not_monitored_apart(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "quality"}).content.decode()
    pills = html.split('class="fmm-drillbox__pills"', 1)[1].split("</div>", 1)[0]
    found = re.findall(
        r'<a class="fmm-rating-pill[^"]*" href="([^"]+)".*?</span>([^<]+)<span class="fmm-rating-pill__count">(\d+)',
        pills,
        re.S,
    )
    chips = {label.strip(): (url, int(n)) for url, label, n in found}
    records = VisitEntity.objects.filter(visit__visit_date__year=2026)
    for code, label in (("on_track", "On track"), ("off_track", "Off track"), ("constrained", "Constrained")):
        assert chips[label][1] == records.filter(hact_q1=code).count(), label
        assert sorted(_drill(client_viewer, chips[label][0])[2]) == sorted(
            records.filter(hact_q1=code).values_list("datamart_id", flat=True)
        )
    not_monitored = records.filter(hact_q1="not_monitored") | records.filter(
        rating="not_monitored", visit__status_group="reported"
    )
    assert chips["Not Monitored"][1] == not_monitored.count() == 2
    assert sorted(_drill(client_viewer, chips["Not Monitored"][0])[2]) == [111, 112]
    # the chips total the records with each Q1 plus the records not monitored
    with_q1 = records.exclude(hact_q1="").exclude(hact_q1="not_monitored").count()
    assert sum(n for _url, n in chips.values()) == with_q1 + not_monitored.count()


def test_the_monitoring_gaps_count_visits_and_open_their_records(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "quality"}).content.decode()
    card = re.search(r'<a class="mini mini--danger" href="([^"]+)"[^>]*><div class="mini__value">(\d+)', html)
    assert card and int(card.group(2)) == metrics.issues_summary(_scope())["gaps"]["n"] == 1
    assert _drill(client_viewer, card.group(1)) == (2, 1, [111, 112])  # 1723 and its two records


def test_the_volume_chart_counts_records_and_names_the_visits(built):
    data = metrics.monthly_volume(_scope())
    indicator = data["indicators"][0]
    assert sum(indicator["values"]) == 12 and sum(indicator["extra"]) == 8
    may = data["drill"]["labels"].index("2026-05")
    assert (indicator["values"][may], indicator["extra"][may]) == (3, 1)


# ------------------------------------------------------------------------------------------ the Analysis tab
def test_rule_analysis_counts_records_and_r19_on_each_of_them(built):
    from neurodb.fmm.models import RecordRuleResult

    rows = {r["code"]: r for r in metrics.rule_analysis(_scope())}
    in_scope = RecordRuleResult.objects.filter(entity__visit__visit_date__year=2026)
    for code, row in rows.items():
        mine = in_scope.filter(rule=code)
        assert row["flagged"] == mine.filter(status="fail").count(), code
        assert row["evaluated"] == mine.filter(status__in=("pass", "fail")).count(), code
    # R19 reads the whole visit, once, and counts on each of its 12 records, as FMS counts it
    assert in_scope.filter(rule="R19").count() == 12


def test_entity_performance_reads_each_entitys_own_records(built, fm_world):
    rows = {r["name"]: r for r in metrics.entities_performance(_scope(), "partner")["rows"]}
    # Amel's partner record of 1722 (93), not 1722's mean; Mercy's partner record of 1723 (48)
    assert (rows["Amel Association"]["avg"], rows["Amel Association"]["records"]) == (Decimal("93.0"), 1)
    assert (rows["Mercy Corps Lebanon"]["avg"], rows["Mercy Corps Lebanon"]["records"]) == (
        Decimal("48.0"),
        1,
    )
    cp = metrics.entities_performance(_scope(), "cp_output")["rows"]
    assert [(r["avg"], r["top_issue"]["rule"]) for r in cp] == [(Decimal("95.0"), "R23")]


def test_section_lines_are_one_per_record(built, client_viewer):
    html = client_viewer.get(
        PAGE, {"year": "2026", "section": "", "tab": "analysis", "sections": "all"}
    ).content.decode()
    panel = html.split('id="fmm-sections-title"', 1)[1].split("</section>", 1)[0]
    anchors = re.findall(r'<li><a href="/fmm/visits/[\w-]+/#record-(\d+)"', panel)
    assert sorted(int(a) for a in anchors) == sorted(
        VisitEntity.objects.values_list("datamart_id", flat=True)
    )
    assert "#1722" in panel and "Amel Association" in panel


def test_the_breakdown_counts_each_record_under_its_own_partner(built, fm_world):
    rows = {r["key"]: r for r in metrics.breakdown(_scope(), "partner")}
    amel, mercy = fm_world.partners["amel"].pk, fm_world.partners["mercy"].pk
    assert (rows[amel]["records"], rows[amel]["visits"]) == (5, 3)  # 1722's three, 1727's, 1725's
    assert (rows[mercy]["records"], rows[mercy]["visits"]) == (7, 5)
    assert rows[amel]["avg"] == metrics.kpis(_scope(f"year=2026&section=&partner={amel}"))["avg_quality"]
    # a visit's open action points count once per group, however many of its records it holds
    assert rows[mercy]["open_action_points"] == sum(
        Visit.objects.filter(partner_ids__contains=[mercy]).values_list("action_points_open", flat=True)
    )


# ------------------------------------------------------------------------------------------ the drill window
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"flag": "R7"}, [101, 102, 103]),
        ({"urgency_level": "medium"}, [111, 112]),
        ({"entity_type": "cp_output"}, [102]),
        ({"bucket": "none"}, [141, 151]),
        ({"issue": "R2:band"}, [111, 112]),
        ({"rule": "R6", "rule_state": "fail"}, [111, 112, 131]),
        ({"location": "30"}, [101, 102, 103, 151]),
    ],
)
def test_the_drill_window_lists_exactly_the_records(built, client_viewer, params, expected):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    records, visits, listed = _drill(client_viewer, f"{DRILL}?year=2026&section=&{query}")
    assert sorted(listed) == expected and records == len(expected)
    assert (
        visits == VisitEntity.objects.filter(datamart_id__in=expected).values("visit_id").distinct().count()
    )


# ------------------------------------------------------------------------------------------ the visit page
def test_a_record_link_opens_its_card_on_the_visit_page(built, client_viewer):
    record = VisitEntity.objects.select_related("visit").get(datamart_id=112)
    assert record.get_absolute_url() == "/fmm/visits/1723/#record-112"
    html = client_viewer.get(reverse("fmm:visit", args=["1723"])).content.decode()
    card = html.split('id="record-112"', 1)[1].split("</article>", 1)[0]
    text = _text(card)
    assert "Mercy Corps Lebanon" in text and "48.0% (Low)" in text and "Urgency 58" in text
    for rule in ("R1", "R2", "R3", "R6", "R23"):
        assert f'<span class="pill pill--sm pill--danger">{rule}</span>' in card, rule
    assert "R3: Q2 lacks specific or disaggregated activity evidence" in card  # its own AI check


# ------------------------------------------------------------------------------------------ the map
def test_one_pin_per_visit_lists_its_records(built):
    from neurodb.fmm import geo

    data = geo.map_points(_scope())
    mapped = VisitEntity.objects.filter(visit__visit_date__year=2026, visit__latitude__isnull=False)
    assert data["counts"]["records_mapped"] == mapped.count() > data["counts"]["mapped"]
    point = next(p for p in data["config"]["points"] if p["name"] == "Visit 1722")
    lines = dict(map(tuple, point["lines"]))
    assert lines["Quality"] == "94.3% (mean of its records)"
    texts = [value for label, value in point["lines"] if label.startswith("Record · ")]
    assert len(texts) == 3 and "Amel Association · 93.0%" in texts
    assert set(point) <= geo.POINT_KEYS


# ------------------------------------------------------------------------------------------ the release banner
def test_the_release_banner_shows_while_records_are_scored_or_checked(built, client_viewer):
    def banner() -> str:
        html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
        found = re.search(r'<div class="alert alert-info[^"]*fmm-release"[^>]*>(.*?)</div>', html, re.S)
        return _text(found.group(1)) if found else ""

    assert banner() == ""  # a site that scored per record from the start
    current = RuleSetVersion.objects.order_by("-number").first()
    RuleSetVersion.objects.create(
        number=current.number + 1,
        snapshot=current.snapshot,
        note=views.RECORDS_VERSION_NOTE,
        created_by_name="x",
    )
    text = banner()  # no refresh has scored the records with it yet
    assert f"Scores are now per record, as in FMS (rules v{current.number + 1})." in text
    assert "They are being recomputed" in text
    from neurodb.fmm import refresh

    assert refresh.run(triggered_by="test", scores_only=True, today=TODAY).status == "succeeded"
    assert banner() == ""  # scored, and no AI check waiting
    VisitEntity.objects.filter(datamart_id__in=(111, 112)).update(ai_pending=2)
    from neurodb.fmm import status

    cache.clear()
    assert "AI checks for 2 records are still running." in banner()
    assert status.snapshot().rules_version == current.number + 1


def test_the_release_note_is_the_records_migrations_own():
    migration = importlib.import_module("neurodb.fmm.migrations.0019_records")
    assert migration.NOTE == views.RECORDS_VERSION_NOTE


# ------------------------------------------------------------------------------------------ wording
def test_the_glossary_is_one_sentence_everywhere(built, client_viewer):
    from pathlib import Path

    from django.conf import settings

    glossary = str(views.GLOSSARY)
    assert glossary.startswith("A visit is one eTools monitoring activity.")
    guide = (Path(settings.BASE_DIR) / "neurodb" / "help" / "guide" / "monitoring-insights.md").read_text()
    assert " ".join(glossary.split()) in " ".join(guide.replace("**", "").split())
    listing = client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "visits"}).content.decode()
    assert glossary in listing.replace("&#x27;", "'")
    page = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert f'title="{glossary}"' in page.replace("&#x27;", "'")


def test_no_entities_or_findings_wording_on_the_page(built, client_viewer):
    from tests.fmm.test_pages import visible

    for params in (
        {},
        {"tab": "quality"},
        {"tab": "analysis"},
        {"tab": "visits"},
        {"tab": "visits", "view": "visits"},
        {"entity_type": "partner"},
    ):
        html = client_viewer.get(PAGE, {"year": "2026", "section": "", **params}).content.decode()
        main = html.split('<main id="main"', 1)[-1]
        text = " ".join(visible(main).split())
        for word in ("Monitored entities", "finding rows", "finding row "):
            assert word not in text, (params, word)
