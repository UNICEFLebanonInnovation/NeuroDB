"""Release 2 of Monitoring insights, beyond the export's field names (test_fms_export): Not monitored
counted apart from the rated visits (A2), the scored statuses (A3), the morning briefing (A5), the new
filters and All time (A6), the brief's parts and its checks (A7), the compliance depth (A8), and the
chat's starter questions, the rule trends and the chart downloads (A9).

On ``fm_world`` built on 5 October 2026 (see test_pages): 8 visits, 6 scored averaging 64.4%, 1723 the
only low one (40.4) and the most urgent (57, amber); no visit at or above 70.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import tools as assistant_tools
from neurodb.fmm import metrics, refresh, score, views
from neurodb.fmm import scope as scope_module
from neurodb.fmm.ai import facts, insights, profiles, sections
from neurodb.fmm.models import PromptVersion, ScoreSetting, Visit
from neurodb.fmm.scope import Scope

from .conftest import LEAD

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    """ "This year", the briefing and the rolling periods are read on 5 October 2026."""
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


@pytest.fixture
def ai_on():
    from neurodb.watch import people

    people.forget()
    with override_settings(**AI_ON):
        yield profiles.published()


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _settings(**changes) -> None:
    """Score settings changed as the admin saves them: the scores are worked out again."""
    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting(pk=1)
    for name, value in changes.items():
        setattr(setting, name, value)
    setting.save()
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.status == "succeeded", run.error


def _tab(client, tab: str, **params) -> str:
    query = {"year": "2026", "section": "", "tab": tab, **params}
    return client.get(PAGE, query, HTTP_HX_REQUEST="true").content.decode()


# ------------------------------------------------------------------------------------------ A2
def test_ask_gives_rating_shares_of_the_rated_visits_only(built):
    out = assistant_tools.run("fm_summary", {})
    assert out["rated_visits"] == 5 and out["not_monitored_visits"] == 1
    assert out["rated_visits_by_rating"] == {"on_track": 3, "constrained": 0, "off_track": 2}
    assert out["rating_shares_of_rated"] == {"on_track": 60.0, "constrained": 0.0, "off_track": 40.0}


def test_the_q1_chips_count_not_monitored_apart(built, client_viewer):
    html = _tab(client_viewer, "quality")
    chips = html.split('aria-label="Visits by rating"', 1)[1].split("</div>", 1)[0]
    assert "Not Monitored 1" in chips
    assert "rating=not_monitored" in chips  # it opens the planned visits not conducted


def test_quality_by_finding_rating_always_has_a_not_monitored_bar(built):
    rows = metrics.quality_by_rating(_scope())
    assert [r["code"] for r in rows] == ["on_track", "off_track", "not_monitored"]
    assert rows[-1]["visits"] == 1  # 1723: reported with nothing rated; 1724 (in progress) not counted
    narrowed = metrics.quality_by_rating(_scope("year=2026&section=&rating=off_track"))
    assert [r["code"] for r in narrowed] == ["off_track"]


# ------------------------------------------------------------------------------------------ A3
def test_a_status_left_out_of_the_scored_statuses_leaves_its_visits_pending(built):
    _settings(scored_statuses=["report_finalization"])
    for visit in Visit.objects.exclude(status="cancelled"):
        assert visit.quality_score is None and visit.urgency is None, visit.key
        assert (visit.score_band, visit.not_scored_reason) == ("pending", score.PENDING), visit.key
    # the follow-up signals do not hang on the score: they stay on the visit page
    assert Visit.objects.get(key="1727").signals == {"no_follow_up": True}
    _settings(scored_statuses=["report_finalization", "completed"])
    assert Visit.objects.filter(quality_score__isnull=False).count() == 6


# ------------------------------------------------------------------------------------------ A5
TILE = re.compile(r'data-tile="(\w+)".*?<div class="kpi__value">(?:<a href="([^"]*)"[^>]*>)?([^<]+)', re.S)


def _briefing(html: str) -> str:
    return html.split('id="fmm-briefing"', 1)[1].split("</section>", 1)[0]


def test_the_morning_briefing_tiles(built, client_viewer):
    html = _briefing(_tab(client_viewer, "insights", year="2025"))
    tiles = {key: (value.strip(), url) for key, url, value in TILE.findall(html)}
    assert list(tiles) == [key for key, _label, _info in views.BRIEFING_TILES]
    values = {key: value for key, (value, _url) in tiles.items()}
    # this year so far, whatever the page's period (2025 here)
    assert values == {
        "critical": "0",
        "avg_quality": "64.4%",
        "low": "1",
        "critical_partners": "0",
        "visits": "8",
        "review": "0",
        "submitted": "0",
        "data_collection": "1",
        "assigned": "0",
        "completed": "6",
    }
    assert "This year: 1 Jan – 5 Oct 2026" in html
    assert html.count('class="fmm-info"') == 10 and 'title="Scored visits below 50."' in html
    assert tiles["critical"][1] == "" and tiles["review"][1] == ""  # nothing to open
    assert "quality=low" in tiles["low"][1] and "visit_status=data_collection" in tiles["data_collection"][1]
    assert "tab=visits" in tiles["avg_quality"][1] and "sort=quality" in tiles["avg_quality"][1]
    assert "Top critical partners" not in html
    # the quality by governorate, each opening the visits there
    assert re.search(r"Bekaa</span> · 58\.7% · 5 visits", html)
    assert re.search(r"North</span> · 75\.7% · 3 visits", html)
    assert "governorate=beqaa" in html


def test_the_briefing_drills_open_its_visits(built, client_viewer):
    html = _briefing(_tab(client_viewer, "insights"))
    tiles = {key: url for key, url, _value in TILE.findall(html)}
    low = client_viewer.get(tiles["low"].replace("&amp;", "&"), HTTP_HX_REQUEST="true").content.decode()
    assert "/fmm/visits/1723/" in low and "/fmm/visits/1722/" not in low
    collecting = client_viewer.get(
        tiles["data_collection"].replace("&amp;", "&"), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "/fmm/visits/1724/" in collecting and "/fmm/visits/1723/" not in collecting


def test_the_briefing_names_the_critical_partners(built, client_viewer):
    _settings(urgency_red=50, urgency_amber=30)
    html = _briefing(_tab(client_viewer, "insights"))
    tiles = {key: (value.strip(), url) for key, url, value in TILE.findall(html)}
    assert tiles["critical"][0] == "1" and tiles["critical_partners"][0] == "1"
    assert 'class="kpi kpi--off_track" data-tile="critical"' in html
    critical = client_viewer.get(
        tiles["critical"][1].replace("&amp;", "&"), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "/fmm/visits/1723/" in critical and "/fmm/visits/1722/" not in critical
    partners = html.split("Top critical partners", 1)[1].split("</div>", 1)[0]
    assert re.search(r"MCL</span> <strong>1</strong>", partners)


def test_the_briefing_keeps_the_pages_other_filters(built, client_viewer):
    html = _briefing(_tab(client_viewer, "insights", governorate="north"))
    values = {key: value.strip() for key, _url, value in TILE.findall(html)}
    assert (values["visits"], values["low"], values["completed"]) == ("3", "0", "2")
    assert "This year: 1 Jan – 5 Oct 2026 · <span data-synced>Governorate: North</span>" in html


# ------------------------------------------------------------------------------------------ A6
def test_all_time_says_when_the_data_starts_and_ends(built, client_viewer):
    html = client_viewer.get(PAGE, {"preset": "all_time", "section": ""}).content.decode()
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    assert "Showing All time" in text
    assert "Data available from 14 Feb 2026 to 30 Sep 2026" in text
    assert _scope("preset=all_time&section=").previous().empty  # no earlier period to compare with
    this_year = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    assert "Data available from" not in this_year


def test_the_filter_bar_offers_the_new_filters(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    bar = html.split('id="fmm-filters"', 1)[1]
    for name in ("modality", "quality", "urgency_level"):
        assert f'name="{name}"' in bar, name
    assert '<option value="all_time"' in bar
    assert "Pending (no score)" in bar


def test_a_scope_without_the_new_filters_keeps_its_hash():
    bare = _scope("year=2026&section=")
    assert not {"modality", "quality", "urgency_level"} & set(bare.canonical())
    filtered = _scope("year=2026&section=&quality=low&urgency_level=medium&modality=none")
    assert filtered.hash() != bare.hash()
    assert insights.scope_of(filtered.canonical(), TODAY).hash() == filtered.hash()


# ------------------------------------------------------------------------------------------ A7
def test_the_schema_is_built_from_the_versions_parts():
    parts = sections.validate(
        [
            {"key": "summary", "label": "Summary", "format": "paragraph", "limit": 3},
            {"key": "action_points", "label": "Actions", "format": "bullets", "limit": 2},
        ]
    )
    schema = sections.schema(parts)
    assert schema["required"] == ["summary", "action_points"]
    assert schema["properties"]["summary"]["items"] is sections.SENTENCE
    assert schema["properties"]["action_points"]["items"]["required"] == [
        "priority",
        "section",
        "partner",
        "action",
        "owner_role",
        "timeframe",
        "keys",
    ]
    text = sections.instructions(parts)
    assert "- summary (Summary): one paragraph of at most 3 sentences." in text
    assert "- action_points (Actions): at most 2 priority action points, most urgent first." in text


def test_an_action_point_is_written_out_by_neurodb():
    action = {
        "priority": "High",
        "section": "Child Protection",
        "partner": "AMEL",
        "action": "Follow up the case management referrals",
        "owner_role": "Child Protection section lead",
        "timeframe": "within 1 week",
    }
    assert sections.action_line(action) == (
        "[PRIORITY: High] Child Protection / AMEL — Follow up the case management referrals — "
        "Child Protection section lead — within 1 week"
    )
    assert sections.action_line({**action, "partner": ""}).startswith("[PRIORITY: High] Child Protection — ")


def test_the_facts_break_the_visits_down_by_partner_modality_and_governorate(built, ai_on):
    found = facts.build(_scope(), ai_on, TODAY)
    partners = {e["name"]: e for e in found.payload["partners"].values()}
    assert set(partners) == {"AMEL", "MCL"}
    mercy = partners["MCL"]
    assert (mercy["visits"], mercy["rated"], mercy["on_track"], mercy["off_track"]) == (5, 3, 2, 1)
    assert (mercy["not_monitored"], mercy["off_track_or_constrained_share_of_rated"]) == (1, 33.3)
    assert partners["AMEL"]["off_track_or_constrained_share_of_rated"] == 50.0
    modalities = found.payload["modalities"]
    assert list(modalities) == ["modality:none"] and modalities["modality:none"]["visits"] == 8
    bekaa = found.payload["places"]["gov:beqaa"]
    assert (bekaa["rated"], bekaa["off_track"], bekaa["not_monitored"], bekaa["avg_quality"]) == (
        3,
        1,
        1,
        58.7,
    )


def test_the_notes_carry_their_q1_q2_and_q3_answers(built, ai_on):
    found = facts.build(_scope(), ai_on, TODAY)
    notes = [n for n in found.payload["narratives"].values() if n["visit"] == "visit:1722"]
    assert notes
    classes = next(n for n in notes if n["text"].startswith("Classes held"))
    assert (classes["q1"], classes["q2"], classes["q3"]) == (
        "On track",
        "BLN classes, Homework support",
        "Registers checked.",
    )


def test_partner_place_and_section_names_pass_the_checks_and_people_do_not(built, ai_on):
    found = facts.build(_scope(), ai_on, TODAY)
    card = found.payload["visits"]["visit:1722"]
    raw = {
        "coverage_summary": [],
        "key_findings": [
            {"text": f"{card['partner']} kept its learning sessions in Bekaa.", "keys": ["visit:1722"]},
            {"text": "Mercy Corps Lebanon visits in the North covered Education.", "keys": ["visit:1726"]},
            {"text": f"{LEAD} led the visit in Bekaa.", "keys": ["visit:1722"]},
        ],
        "challenges": [],
        "recommendations": [],
        "action_points": [
            {
                "priority": "High",
                "section": "Education",
                "partner": card["partner"],
                "action": "Follow up the visit's findings with the partner",
                "owner_role": "Education section lead",
                "timeframe": "within 10 working days",
                "keys": ["visit:1722"],
            },
            {
                "priority": "Medium",
                "section": "Education",
                "partner": "Some Other Partner",
                "action": "Follow up the visit's findings with the partner",
                "owner_role": "Education section lead",
                "timeframe": "within 2 weeks",
                "keys": ["visit:1722"],
            },
        ],
    }
    kept, actions, dropped = insights.validate(raw, found, TODAY)
    assert [s["text"] for s in kept["key_findings"]] == [
        f"{card['partner']} kept its learning sessions in Bekaa.",
        "Mercy Corps Lebanon visits in the North covered Education.",
    ]
    assert [a["partner"] for a in actions] == [card["partner"], ""]
    assert dropped == {"person": 1, "partner_removed": 1}


# ------------------------------------------------------------------------------------------ A8
def test_comp_is_the_number_of_quality_flags_sent(built, ai_on):
    for depth in (1, 2, 15):
        version = profiles.draft_from(ai_on, None, "Depth", comparison_visits=depth)
        found = facts.build(_scope(), version, TODAY)
        assert len(found.payload["issues"]) == found.sent["flags"] <= depth
        assert found.sent["flags_allowed"] == depth
        # the most frequent flags of the filter
        sent = sorted((e["visits"] for e in found.payload["issues"].values()), reverse=True)
        assert sent == [row["visits"] for row in metrics.top_issues(_scope(), depth)]


# ------------------------------------------------------------------------------------------ A9
def test_the_chat_starter_questions_come_from_the_published_version(built, ai_on, client_viewer):
    PromptVersion.objects.filter(pk=ai_on.pk).update(chat_examples=["Which visits in Bekaa were late?"])
    html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    assert "Which visits in Bekaa were late?" in html
    assert "What are the main programmatic issues in this period?" not in html


def test_rule_trends_give_each_rules_monthly_share_of_points(built):
    trends = metrics.rule_trends(_scope())
    assert trends["labels"][0] == "Jan 2026" and len(trends["labels"]) == 10  # to October, this month
    assert trends["drill"]["labels"][4] == "2026-05"
    r3 = next(name for name in trends["series"] if name.startswith("R3 "))
    values = trends["series"][r3]
    assert values[0] is None  # no visit ended in January
    assert all(v is None or 0 <= v <= 100 for s in trends["series"].values() for v in s)
    assert trends["drill"]["series"][r3] == "R3"
    assert metrics.rule_trends(_scope("year=2025&section=")) == {}


def test_the_rule_trends_load_when_their_panel_is_reached(built, client_viewer):
    html = _tab(client_viewer, "quality")
    panel = html.split('id="fmm-rule-trends-panel"', 1)[1].split("</section>", 1)[0]
    assert 'hx-trigger="revealed"' in panel and "rule_trends=1" in panel
    assert 'id="fmm-chart-rule-trends"' not in html and "fmm-rule-trend-data" not in html
    loaded = _tab(client_viewer, "quality", rule_trends="1")
    data = re.search(r'<script id="fmm-rule-trend-data" type="application/json">(.*?)</script>', loaded, re.S)
    assert data and '"rule_trends"' in data.group(1) and "R3 " in data.group(1)
    assert 'data-source="fmm-rule-trend-data"' in loaded


def test_a_rule_trend_point_opens_the_visits_it_flagged_that_month(built, client_viewer):
    html = _tab(client_viewer, "quality", rule_trends="1")
    template = re.search(r'id="fmm-chart-rule-trends"[^>]*data-href-template="([^"]+)"', html).group(1)
    url = template.replace("&amp;", "&").replace("{drill}", "2026-05").replace("{series_drill}", "R3")
    found = client_viewer.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert "/fmm/visits/1722/" in found and "/fmm/visits/1727/" not in found


def test_every_chart_can_be_saved_as_a_png(built, client_viewer):
    for tab, extra in (("quality", {}), ("quality", {"rule_trends": "1"}), ("analysis", {})):
        html = _tab(client_viewer, tab, **extra)
        for target in re.findall(r'data-chart-png="#([\w-]+)"', html):
            assert f'id="{target}"' in html, target
        charts = set(re.findall(r'id="(fmm-chart-[\w-]+)" class="chart', html))
        assert charts and charts <= set(re.findall(r'data-chart-png="#([\w-]+)"', html)), tab
    script = (Path(settings.BASE_DIR) / "neurodb/web/static/js/charts.js").read_text()
    assert "Plotly.downloadImage" in script and '"share-lines"(el, data)' in script


def test_the_high_urgency_tile_opens_the_urgent_visits(built, client_viewer):
    html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    link = re.search(r'<a class="kpi__more small" href="([^"]+)"[^>]*>View urgent visits →</a>', html)
    assert link and "urgency=red" in link.group(1) and "tab=visits" in link.group(1)
