"""Monitoring insights drawn as FMS draws it (stage E1): the Insights tab's morning briefing (tinted
tiles, top critical partners, governorate chips by band), the AI brief's generation settings, the
critical visits requiring attention; the Quality tab's order and charts; a PDF button on every chart
card; and an action point in progress counting as open."""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.fmm import metrics, refresh
from neurodb.fmm import scope as scope_module
from neurodb.fmm.ai import profiles
from neurodb.fmm.models import ModelCapability, PromptVersion, ScoreSetting, Visit, VisitRuleResult
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _settings(**changes) -> None:
    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting(pk=1)
    for name, value in changes.items():
        setattr(setting, name, value)
    setting.save()
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.status == "succeeded", run.error


def _tab(client, tab: str, **params) -> str:
    query = {"year": "2026", "section": "", "tab": tab, **params}
    return client.get(PAGE, query, HTTP_HX_REQUEST="true").content.decode()


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _section(html: str, element_id: str) -> str:
    return html.split(f'id="{element_id}"', 1)[1].split("</section>", 1)[0]


# ------------------------------------------------------------------------------------------ Insights tab
def test_the_insights_tab_follows_fms_order(built, client_viewer):
    html = _tab(client_viewer, "insights")
    order = [html.find(f'id="{i}"') for i in ("fmm-briefing", "fmm-insights", "fmm-critical", "fmm-chat")]
    assert -1 not in order and order == sorted(order), order


def test_the_briefing_tiles_are_tinted_as_fms_shows_them(built, client_viewer):
    briefing = _section(_tab(client_viewer, "insights"), "fmm-briefing")
    for key in ("critical", "avg_quality", "low", "critical_partners", "visits", "review", "completed"):
        assert f'class="kpi fmm-tile fmm-tile--{key}" data-tile="{key}"' in briefing, key
    assert "This year (&lt; 50%)" in briefing  # the low quality visits' threshold, as FMS writes it
    css = (STATIC / "css" / "app.css").read_text()
    for token in ("--fmm-tint-red", "--fmm-tint-yellow", "--fmm-tint-purple", "--fmm-ink-amber"):
        assert css.count(f"{token}:") == 2, token  # the light and the dark theme


def test_top_critical_partners_count_the_critical_visits_and_open_them(built, client_viewer):
    _settings(urgency_red=1, urgency_amber=0)  # every scored visit is critical
    data = metrics.briefing(_scope())
    critical = Visit.objects.filter(end_date__year=2026, urgency__gte=1)
    assert data["top_partners"], "a critical visit has a partner"
    assert len(data["top_partners"]) <= metrics.TOP_PARTNERS
    for partner in data["top_partners"]:
        assert partner["visits"] == critical.filter(partner_ids__contains=[partner["id"]]).count()
    counts = [p["visits"] for p in data["top_partners"]]
    assert counts == sorted(counts, reverse=True)  # most critical visits first
    html = _section(_tab(client_viewer, "insights"), "fmm-briefing")
    chips = html.split("Top critical partners (this year)", 1)[1].split("</div>", 1)[0]
    assert "Amel Association" in chips and "@" not in chips  # partner names only
    amel = next(p for p in data["top_partners"] if p["name"] == "Amel Association")
    urls = re.findall(r'class="fmm-chip fmm-chip--danger" href="([^"]+)"', chips)
    assert len(urls) == len(data["top_partners"])
    url = next(u for u in urls if f"partner={amel['id']}&" in u + "&").replace("&amp;", "&")
    assert "urgency_level=high" in url
    opened = client_viewer.get(url, HTTP_HX_REQUEST="true").content.decode()
    keys = set(re.findall(r"/fmm/visits/([\w-]+)/", opened))
    expected = {v.key for v in critical.filter(partner_ids__contains=[amel["id"]])}
    assert keys == expected


def test_the_governorate_chips_are_tinted_by_band(built, client_viewer):
    briefing = _section(_tab(client_viewer, "insights"), "fmm-briefing")
    chips = re.findall(
        r'class="fmm-chip fmm-chip--mono fmm-chip--(\w+)"[^>]*><span data-synced>([^<]+)</span> · ([\d.]+)%',
        briefing,
    )
    assert chips
    limits = metrics.thresholds()
    for band, _name, avg in chips:
        assert band == metrics.band_of(float(avg), limits), (band, avg)


# ------------------------------------------------------------------------------------------ critical visits
def test_critical_visits_come_worst_first_with_their_flags_and_no_address(built, client_viewer):
    _settings(urgency_red=40, urgency_amber=20)
    worst = Visit.objects.filter(end_date__year=2026, urgency__gte=40).order_by("-urgency").first()
    VisitRuleResult.objects.update_or_create(
        visit=worst,
        rule="R19",
        defaults={
            "status": "fail",
            # what a careless template might keep: the page must still never show an address
            "detail": "R19: Monitor monitor@example.org is not listed as staff for field office 'Beirut' — verify assignment",
            "measure": 2,
        },
    )
    cache.clear()
    data = metrics.critical_items(_scope())
    assert data["band"] == "red" and 0 < len(data["items"]) <= metrics.CRITICAL_ITEMS
    urgencies = [i["urgency"] for i in data["items"]]
    assert urgencies == sorted(urgencies, reverse=True) and min(urgencies) >= 40
    assert data["items"][0]["key"] == worst.key
    html = _section(_tab(client_viewer, "insights"), "fmm-critical")
    text = unescape(_text(html))
    assert "Critical visits requiring attention" in text
    assert "2 monitors not on the staff list of Beirut — verify the assignment" in text
    assert "@" not in html and "monitor@" not in text
    shown = [int(n) for n in re.findall(r"Urgency: (\d+)", text)]
    assert shown == urgencies
    # each flag line: the rule id and the stored flag (an AI check's with the AI's explanation)
    failed = VisitRuleResult.objects.filter(visit=worst, status="fail").exclude(rule="R19")
    for result in failed:
        message = result.detail.split(":", 1)[1].split(" — ", 1)[0].strip()
        assert message in text, result.rule
    assert "HIGH" in text


def test_without_a_red_visit_the_most_urgent_amber_ones_are_listed(built, client_viewer):
    limits = metrics.thresholds()
    assert not Visit.objects.filter(end_date__year=2026, urgency__gte=limits["red"]).exists()
    data = metrics.critical_items(_scope())
    amber = Visit.objects.filter(end_date__year=2026, urgency__gte=limits["amber"]).order_by("-urgency")
    assert data["band"] == ("amber" if amber.exists() else "")
    assert [i["key"] for i in data["items"]] == [v.key for v in amber[: metrics.CRITICAL_AMBER]]
    text = _text(_section(_tab(client_viewer, "insights"), "fmm-critical"))
    if amber.exists():
        assert "the most urgent amber visits" in text and "MEDIUM" in text


def test_at_most_ten_critical_visits_and_a_link_to_all(built, client_viewer, monkeypatch):
    _settings(urgency_red=1, urgency_amber=0)
    monkeypatch.setattr(metrics, "CRITICAL_ITEMS", 2)
    cache.clear()
    total = Visit.objects.filter(end_date__year=2026, urgency__gte=1).count()
    assert total > 2
    html = _section(_tab(client_viewer, "insights"), "fmm-critical")
    assert len(re.findall(r'class="fmm-critical-item ', html)) == 2
    link = re.search(r'href="([^"]+)"[^>]*>Show all (\d+)', html)
    assert link and int(link.group(2)) == total
    assert "tab=visits" in link.group(1) and "urgency=red" in link.group(1)
    # the Visits tab it opens lists exactly those visits
    opened = client_viewer.get(unescape(link.group(1)), HTTP_HX_REQUEST="true").content.decode()
    assert f"Showing {total} visits" in _text(opened)


# ------------------------------------------------------------------------------------------ AI brief settings
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


def test_the_generation_settings_show_what_the_published_version_holds(
    built, client_viewer, admin_user, client
):
    with override_settings(**AI_ON):
        version = profiles.published()
        PromptVersion.objects.filter(pk=version.pk).update(temperature="0.30", top_p=None)
        url = f"{reverse('fmm:insights')}?year=2026&section="
        html = client_viewer.get(url).content.decode()
        strip = html.split('class="fmm-gen"', 1)[1].split('<div class="chips', 1)[0]
        text = _text(strip)
        assert "Generation settings" in text
        version.refresh_from_db()
        for label, value in (
            ("Model", profiles.model_of(version)),
            ("Max output tokens", f"{version.max_output_tokens:,}"),
            ("Narrative samples", str(version.narratives_sampled)),
            ("Compliance depth", str(version.comparison_visits)),
            ("Temperature", "0.30"),
        ):
            assert f"{label} {value}" in text, label
        assert "Top-p" not in text  # not set: never shown as a setting
        for part in ("Coverage and Quality Summary", "Recommendations"):
            assert part in text
        assert "Edit in admin" not in text  # a viewer only reads them
        # a temperature the model refused is not one it uses: not shown
        ModelCapability.objects.update_or_create(
            model=profiles.model_of(version),
            effort=version.effort,
            parameter="temperature",
            defaults={"accepted": False, "checked_at": timezone.now()},
        )
        text = _text(
            client_viewer.get(url)
            .content.decode()
            .split('class="fmm-gen"', 1)[1]
            .split('<div class="chips', 1)[0]
        )
        assert "Temperature" not in text
        client.force_login(admin_user)
        admin_html = client.get(url).content.decode()
        assert (
            reverse("admin:fmm_promptversion_change", args=[version.pk])
            in admin_html.split('class="fmm-gen"', 1)[1]
        )


# ------------------------------------------------------------------------------------------ PDF on every chart card
def test_every_chart_card_has_a_png_and_a_pdf_button(built, client_viewer):
    for tab, extra in (("quality", {}), ("analysis", {}), ("analysis", {"rule_trends": "1"})):
        html = _tab(client_viewer, tab, **extra)
        sections = re.findall(r"<section\b.*?</section>", html, re.S)
        charted = [s for s in sections if re.search(r'id="fmm-chart-[\w-]+" class="chart', s)]
        assert charted, tab
        for section in charted:
            assert "data-chart-png=" in section and "data-card-pdf" in section, section[:120]
    points = client_viewer.get(reverse("reports:action_points"), HTTP_HX_REQUEST="true").content.decode()
    assert points.count("data-chart-png=") == points.count("data-card-pdf") > 0
    app = (STATIC / "js" / "app.js").read_text()
    assert "[data-card-pdf]" in app and '"afterprint"' in app and "window.print()" in app
    css = (STATIC / "css" / "app.css").read_text()
    assert "body.printing-card .print-path > :not(.print-path):not(.print-card)" in css


# ------------------------------------------------------------------------------------------ help guide
def test_the_help_guide_names_the_panels_as_the_page_does():
    guide = (Path(settings.BASE_DIR) / "neurodb" / "help" / "guide" / "monitoring-insights.md").read_text()
    for name in (
        "Top critical partners",
        "Generation settings",
        "Critical visits requiring attention",
        "Quality score trends",
        "Monitoring volume over time",
        "HACT Q1 — Finding rating distribution",
        "PDF",
    ):
        assert name in guide, name


# ------------------------------------------------------------------------------------------ action points in progress
def test_an_action_point_in_progress_is_open_and_can_be_overdue(fm_world):
    dm.ActionPoint.objects.filter(datamart_id=8001).update(
        status="in_progress", due_date=datetime.date(2026, 9, 1)
    )
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded", run.error
    visit = Visit.objects.get(key="1722")
    assert visit.action_points_open == 1 and visit.action_points_overdue == 1
    point = dm.ActionPoint.objects.get(datamart_id=8001)
    assert point.is_open
    for status in ("in progress", "in-progress", "open"):
        point.status = status
        assert point.is_open, status
    every = datamart.action_points(QueryDict(""))
    assert every["open"] == dm.ActionPoint.objects.exclude(status="completed").count()
    # the "open" filter of the action points page keeps the points in progress
    opened = datamart.action_points(QueryDict("status=open"))
    assert 8001 in set(opened["points"].values_list("datamart_id", flat=True))
    # but "in progress" chosen alone keeps only the points in progress
    working = datamart.action_points(QueryDict("status=in_progress"))
    assert set(working["points"].values_list("status", flat=True)) == {"in_progress"}


# ------------------------------------------------------------------------------------------ charts
CHART_SCRIPT = """
globalThis.document = { documentElement: { getAttribute: () => "light" } };
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => (name.startsWith("--nd-") ? "#446ab3" : "") });
const charts = await import(process.argv[1]);
const B = charts.BUILDERS;
const el = (dataset = {}) => ({ dataset, clientWidth: 900 });
const data = {
  months: ["May 2026", "Jun 2026"], indicators: [{ id: "q", values: [64.7, null], reports: [1, 0] }],
  bar_name: "Average quality score", line_name: "Total reports", line_unit: "reports",
  drill: { labels: ["2026-05", "2026-06"], series: {} },
};
const area = B["dual-axis"](el({ primary: "area", yTitle: "Quality score (%)", y2Title: "Report count" }), data);
const bars = B["dual-axis"](el({ primary: "bar" }), { ...data, line_unit: "%" });
const q1 = B.grouped(el({ barmode: "stack", legend: "top", yTitle: "Visit count" }), {
  labels: ["May 2026"], series: { "On track": [1], "Off track": [2] }, drill: { labels: ["2026-05"], series: { "On track": "on_track", "Off track": "off_track" } },
});
console.log(JSON.stringify({
  area: area.traces.map((t) => [t.type, t.line?.shape ?? null, t.fill ?? null, t.yaxis ?? "y", t.name]),
  areaAxis: [area.layout.yaxis.range, area.layout.yaxis.title.text, area.layout.yaxis2.title.text],
  bars: bars.traces.map((t) => [t.type, t.line?.shape ?? null]),
  barsAxis2: [bars.layout.yaxis2.range, bars.layout.yaxis2.ticksuffix],
  meta: area.traces[0].meta,
  legend: [area.layout.legend.y, area.layout.legend.xanchor, q1.layout.legend.y, q1.layout.legend.xanchor],
  q1Title: q1.layout.yaxis.title.text,
  click: charts.drillUrl("/fmm/drill/?month={drill}", { pointIndex: 1, data: area.traces[1] }),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_monthly_charts_draw_smooth_lines_on_two_axes():
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", CHART_SCRIPT, (STATIC / "js" / "charts.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    data = json.loads(out.stdout)
    assert data["area"] == [
        ["scatter", "spline", "tozeroy", "y", "Average quality score"],
        ["scatter", "spline", None, "y2", "Total reports"],
    ]
    assert data["areaAxis"] == [[0, 100], "Quality score (%)", "Report count"]
    assert data["bars"] == [["bar", None], ["scatter", "spline"]]
    assert data["barsAxis2"] == [[0, 100], "%"]
    assert data["meta"] == {"drill": ["2026-05", "2026-06"], "series": None}
    assert data["legend"] == [1, "center", 1, "center"]  # on top, centred, as FMS draws them
    assert data["q1Title"] == "Visit count"
    assert data["click"] == "/fmm/drill/?month=2026-06"
