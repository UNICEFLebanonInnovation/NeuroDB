"""Monitoring insights drawn as FMS draws it (stage E2): the Analysis tab in FMS's order (the five-bucket
quality score distribution, the rule score trends, the quality rule analysis, the quality flag frequency,
the flag count distribution, entity performance with All, quality by field office, section performance);
the Map tab's legend wording; the drill-down window's columns; the visits table's page size; and the help
guide."""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
from decimal import Decimal
from html import unescape
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.http import QueryDict
from django.urls import reverse

from neurodb.fmm import metrics, views
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
DRILL = reverse("fmm:drill")
STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _tab(client, tab: str, **params) -> str:
    query = {"year": "2026", "section": "", "tab": tab, **params}
    return client.get(PAGE, query, HTTP_HX_REQUEST="true").content.decode()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def _panel(html: str, title_id: str) -> str:
    return html.split(f'id="{title_id}"', 1)[1].split("</section>", 1)[0]


def _drill_count(client, url: str) -> int:
    html = client.get(unescape(url), HTTP_HX_REQUEST="true").content.decode()
    return int(re.search(r'id="modal-title">\s*(\d+) visits?', html).group(1))


ANALYSIS_ORDER = (  # FMS's order, then NeuroDB's own blocks
    "Quality score distribution",
    "Rule score trends over time",
    "Quality rule analysis",
    "Quality flag frequency",
    "Flag count distribution",
    "Entity performance",
    "Quality by field office",
    "Section performance",
    "Highlights",
    "Governorates not visited",
    "Field offices",
    "Visit frequency by location",
    "Quality by finding rating",
    "Points by category",
    "Programmatic visits and HACT",
    "Follow-up",
)


def test_the_analysis_tab_follows_fms_order(built, client_viewer):
    html = _tab(client_viewer, "analysis")
    titles = [_text(t).strip() for t in re.findall(r'<h2 class="panel__title"[^>]*>(.*?)</h2>', html, re.S)]
    order = [next(i for i, t in enumerate(titles) if t.startswith(name)) for name in ANALYSIS_ORDER]
    assert order == sorted(order), titles
    for gone in ("Flags by rule", "Quality rules<", ">Sections<", 'data-chart="hbars"'):
        assert gone not in html, gone
    quality = _text(_tab(client_viewer, "quality"))
    assert "Flag count distribution" not in quality and "Flags per visit" not in quality


# ------------------------------------------------------------------------------------------ s07
def test_the_score_distribution_has_fms_five_buckets_that_sum_to_the_scored_visits(built, client_viewer):
    scope = _scope()
    data = metrics.score_buckets(scope)
    assert [i["drill"] for i in data["items"]] == ["0-20", "20-40", "40-60", "60-80", "80-100"]
    assert [i["label"] for i in data["items"]] == ["0–20", "20–40", "40–60", "60–80", "80–100"]
    assert [i["color"] for i in data["items"]] == [f"--fmm-score-{n}" for n in range(5)]
    assert sum(i["value"] for i in data["items"]) == metrics.kpis(scope)["scored"]
    for item in data["items"]:  # each bar opens exactly its visits
        assert _scope(f"year=2026&section=&bucket={item['drill']}").visits().count() == item["value"], item
    # 1723 scores 46, 1727 80 (in the top bucket), 1722 and 1728 93
    values = {i["drill"]: i["value"] for i in data["items"]}
    assert values["40-60"] == 1 and values["80-100"] >= 3
    Visit.objects.filter(key="1722").update(quality_score=Decimal("100.0"))
    cache.clear()
    assert metrics.score_buckets(_scope())["items"][-1]["value"] == values["80-100"]  # 100 is in 80-100
    html = _tab(client_viewer, "analysis")
    panel = _panel(html, "fmm-dist-title")
    assert 'data-value-title="Visit count"' in panel and "&amp;bucket={drill}" in panel
    assert "Not scored: 2 visits" in _text(panel)
    css = (STATIC / "css" / "app.css").read_text()
    for n in range(5):
        assert css.count(f"--fmm-score-{n}:") == 2, n  # a light and a dark colour


def test_the_rule_score_trends_are_smooth_lines_with_the_legend_on_top(built, client_viewer):
    panel = _panel(_tab(client_viewer, "analysis", rule_trends="1"), "fmm-rule-trends-title")
    assert 'data-chart="share-lines"' in panel and 'data-y-title="% of max score"' in panel
    assert "data-chart-png=" in panel and "data-card-pdf" in panel


# ------------------------------------------------------------------------------------------ s08
def test_rule_analysis_flag_frequency_and_the_rule_list_agree(built, client_viewer):
    scope = _scope()
    analysis = {r["code"]: r for r in metrics.rule_analysis(scope)}
    frequency = {r["code"]: r for r in metrics.flag_frequency(scope)["rows"]}
    for code, row in frequency.items():
        assert (row["n"], row["evaluated"]) == (analysis[code]["flagged"], analysis[code]["evaluated"]), code
    html = _tab(client_viewer, "analysis")
    rules = _panel(html, "fmm-rules-title")
    checked = rules.split('<details class="fmm-rules-all', 1)[0]
    shown = [
        re.search(r'fmm-bar__label">(?:<a [^>]*>)?(R\d+) .*?(\d+) / (\d+) visits flagged', row, re.S).groups()
        for row in checked.split("data-png-row>")[1:]
    ]
    assert shown and [c for c, *_ in shown] == [
        r["code"] for r in metrics.rule_analysis(scope) if r["state"] == "ok"
    ]
    flagged = _panel(html, "fmm-flag-rule-title")
    rows = re.findall(
        r'<a class="fmm-bar" data-png-row href="([^"]+)".*?mono">(R\d+)</span>.*?fmm-bar__value">(\d+)',
        flagged,
        re.S,
    )
    assert [code for _url, code, _n in rows] == [
        r["code"] for r in metrics.flag_frequency(scope)["rows"] if r["n"]
    ]
    for code, n, total in shown:
        assert (int(n), int(total)) == (analysis[code]["flagged"], analysis[code]["evaluated"])
    for url, code, n in rows:
        assert int(n) == frequency[code]["n"] == _drill_count(client_viewer, url), code
    # every rule is still listed, with what it checks, under the rule analysis: loaded when it is opened
    assert "Every quality rule (" in rules and "rules=all" in rules and "What it checks" not in rules
    every = _panel(_tab(client_viewer, "analysis", rules="all"), "fmm-rules-title")
    listed = every.split('id="fmm-rules-all"', 1)[1]
    assert "What it checks" in listed and "6 / 6 visits flagged" in _text(listed)
    assert listed.count('class="fmm-meter-row"') == len(analysis)


@pytest.mark.parametrize(
    ("share", "tone"),
    [(None, "success"), (Decimal("0"), "success"), (Decimal("24.9"), "success"), (Decimal("25"), "warning"),
     (Decimal("50"), "warning"), (Decimal("50.1"), "danger"), (Decimal("100"), "danger")],
)  # fmt: skip
def test_a_rule_is_green_amber_or_red_by_its_share_flagged(share, tone):
    assert metrics.rule_tone(share) == tone


def test_the_rule_bar_is_the_share_not_flagged(built):
    for row in metrics.rule_analysis(_scope()):
        if row["state"] == "ok":
            assert row["clean_fill"] == pytest.approx(100 - float(row["share"])), row["code"]
            assert row["tone"] == metrics.rule_tone(row["share"])


# ------------------------------------------------------------------------------------------ s09
def test_the_flag_count_distribution_reads_as_fms(built, client_viewer):
    scope = _scope()
    data = metrics.flag_distribution(scope)
    assert [r["label"] for r in data["rows"]] == ["0 flags", "1 flag", "2 flags", "3+ flags"]
    assert [r["color"] for r in data["rows"]] == ["success", "info", "warning", "danger"]
    assert sum(r["n"] for r in data["rows"]) == data["scored"] == metrics.kpis(scope)["scored"]
    panel = _panel(_tab(client_viewer, "analysis"), "fmm-flags-title")
    text = _text(panel)
    for row in data["rows"]:
        assert row["label"] in text
    assert "Green = no issues · Blue = minor · Amber = moderate · Red = critical attention needed" in text
    links = re.findall(r'<a href="([^"]+)" hx-get="[^"]+" hx-target="#modal-content">([^<]+)</a>', panel)
    counts = {r["label"]: r["n"] for r in data["rows"]}
    assert links and all(_drill_count(client_viewer, url) == counts[label] for url, label in links)
    assert re.search(r'fmm-bar__pct">\d+%<', panel)  # the share inside the bar


def test_entity_performance_shows_all_entities_by_default(built, client_viewer):
    html = _tab(client_viewer, "analysis")
    entities = html.split('id="fmm-entities"', 1)[1]
    assert "entity_kind=all" in entities.split("</section>", 1)[0]  # the lazy table asks for All
    chips = re.findall(r'class="fmm-chip fmm-chip--choice"[^>]*>([^<]+)<', entities)
    assert [c.strip() for c in chips][:4] == ["Partner", "CP output", "PD/SSFA", "All"]
    html = _tab(client_viewer, "analysis", entity_kind="all")
    table = html.split('id="fmm-entities"', 1)[1].split("</table>", 1)[0]
    current = re.search(r'aria-current="page">([^<]+)<span class="fmm-chip__count">(\d+)', html)
    kinds = metrics.entity_kinds(_scope())
    assert current.group(1).strip() == "All" and int(current.group(2)) == sum(kinds.values())
    for badge in ("CSO partner", "PD/SSFA", "CP output"):
        assert f">{badge}</span>" in table, badge
    assert "Amel Association" in table and "Mercy Corps Lebanon" in table  # full partner names
    rows = metrics.entities_performance(_scope(), "all")["rows"]
    assert {r["kind"] for r in rows} == set(metrics.entity_rows(_scope())["entities"])
    scored = [r["avg"] for r in rows if r["avg"] is not None]
    assert scored == sorted(scored) and all(r["avg"] is None for r in rows[len(scored) :])
    assert len(rows) == sum(len(v) for v in metrics.entity_rows(_scope())["entities"].values())
    for header in ("Entity", "Type", "Visits", "Avg quality", "High / Med / Low", "Top issue"):
        assert f">{header}</th>" in table, header
    assert 'class="fmm-rule-badge"' in table and re.search(
        r'class="fmm-avg fmm-avg--(high|medium|low)"', table
    )
    # one kind still works, and an unknown kind is All
    assert (
        "Amel Association"
        not in _tab(client_viewer, "analysis", entity_kind="pd")
        .split('id="fmm-entities"', 1)[1]
        .split("</table>", 1)[0]
    )
    assert 'aria-current="page">All' in _tab(client_viewer, "analysis", entity_kind="nonsense")


@pytest.mark.parametrize(
    ("raw", "label"),
    [("Civil Society Organization", "CSO partner"), ("CSO", "CSO partner"), ("Government", "Government partner"),
     ("UN Agency", "UN agency"), ("Bilateral / Multilateral", "Bilateral partner"), ("", "Partner"), (None, "Partner")],
)  # fmt: skip
def test_a_partner_type_reads_as_fms_badge(raw, label):
    assert metrics.partner_type_label(raw) == label


# ------------------------------------------------------------------------------------------ s10, s11
def test_quality_by_field_office_is_one_row_per_office(built, client_viewer):
    panel = _panel(_tab(client_viewer, "analysis"), "fmm-office-rules-title")
    offices = metrics.office_rule_badges(_scope())
    assert panel.count('class="fmm-office-row"') == len(offices)
    for office in offices:
        for badge in office["badges"]:
            chip = f'fmm-office-chip fmm-office-chip--{badge["level"]}" title="{badge["label"]}">{badge["rule"]}: {badge["flagged"]}/{badge["total"]}<'
            assert chip in unescape(panel), chip
            assert badge["level"] == ("danger" if 2 * badge["flagged"] >= badge["total"] else "warning")
    assert re.search(r"\d+ visits? scored", _text(panel))


def test_section_performance_lists_the_first_ten_visits(built, client_viewer, monkeypatch):
    monkeypatch.setattr(views, "SECTION_TOP", 2)
    html = _tab(client_viewer, "analysis")
    assert "Section performance" in _text(html) and ">Sections<" not in html
    panel = _panel(html, "fmm-sections-title")
    every = _panel(_tab(client_viewer, "analysis", sections="all"), "fmm-sections-title")
    sections = metrics.sections(_scope())
    blocks = panel.split('<div class="fmm-section">')[1:]
    full = every.split('<div class="fmm-section">')[1:]
    assert len(blocks) == len(full) == len(sections)
    for i, (block, whole, section) in enumerate(zip(blocks, full, sections, strict=True), start=1):
        assert block.split("<details", 1)[0].count("<li>") == min(2, len(section["lines"]))
        if len(section["lines"]) > 2:
            # "Show all" loads the rest when it is opened, as the places tables do
            assert f"Show all {section['visits']}" in block and "sections=all" in block
            assert f'hx-select="#fmm-section-rest-{i}"' in block and 'hx-trigger="toggle once"' in block
            assert whole.count("<li>") == len(section["lines"]) + (1 if section["more"] else 0)
            assert f'id="fmm-section-rest-{i}"' in whole and " open>" in whole
        # lowest score first, the unscored ones after
        scores = [line["quality"] for line in section["lines"]]
        scored = [q for q in scores if q is not None]
        assert scored == sorted(scored) and scores[len(scored) :] == [None] * (len(scores) - len(scored))
    entities = metrics.visit_entities(_scope(), [line["key"] for s in sections for line in s["lines"]])
    shown = unescape(every).upper()
    assert entities and all(e[:20].upper() in shown for e in entities.values())


# ------------------------------------------------------------------------------------------ PNG and PDF
def test_every_bar_list_has_a_png_and_a_pdf_button(built, client_viewer):
    html = _tab(client_viewer, "analysis")
    for section in re.findall(r"<section\b.*?</section>", html, re.S):
        if "data-png-row" in section:
            target = re.search(r'data-rows-png="#([\w-]+)"', section)
            assert target and f'id="{target.group(1)}"' in section, section[:120]
            assert "data-card-pdf" in section
    assert html.count("data-rows-png=") == 3  # rule analysis, flag frequency, flag count distribution
    script = (STATIC / "js" / "charts.js").read_text()
    assert "[data-rows-png]" in script and 'canvas.toDataURL("image/png")' in script


BARS_SCRIPT = """
const charts = await import(process.argv[1]);
const node = (text, extra = {}) => ({ textContent: text, style: {}, ...extra });
const row = (label, value, width, inside) => ({
  querySelector: (sel) => ({
    ".fmm-bar__label": node(label), ".fmm-bar__value": node(value),
    ".fmm-bar__fill": node("", { style: { width } }), ".fmm-bar__pct": inside ? node(inside) : null,
  })[sel] ?? null,
});
const list = { querySelectorAll: () => [row(" R1  Report ", "15 / 55 visits flagged", "73%", ""), row("3+ flags", "17 visits", "31%", "31%")] };
const B = charts.BUILDERS;
const trend = B["share-lines"]({ dataset: { yTitle: "% of max score" }, clientWidth: 900 }, {
  labels: ["Jan 2026", "Feb 2026"], series: { "R1 A": [100, 90], "R2 B": [80, null] },
});
console.log(JSON.stringify({
  rows: charts.barRows(list).map((r) => [r.label, r.value, r.share, r.inside]),
  trend: trend.traces.map((t) => [t.mode, t.line.shape]),
  legend: [trend.layout.legend.y, trend.layout.legend.xanchor], yTitle: trend.layout.yaxis.title.text,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_bar_lists_and_the_rule_trends_draw_as_fms():
    script = "globalThis.document = { documentElement: { getAttribute: () => 'light' } };\n"
    script += "globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#446ab3' });\n" + BARS_SCRIPT
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script, (STATIC / "js" / "charts.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    data = json.loads(out.stdout)
    assert data["rows"] == [
        ["R1 Report", "15 / 55 visits flagged", 73, ""],
        ["3+ flags", "17 visits", 31, "31%"],
    ]
    assert data["trend"] == [["lines+markers", "spline"], ["lines+markers", "spline"]]
    assert data["legend"] == [1, "center"] and data["yTitle"] == "% of max score"


# ------------------------------------------------------------------------------------------ s12
def test_the_map_reads_as_fms_legend(built, client_viewer):
    html = _tab(client_viewer, "map")
    text = _text(html)
    assert "Visit locations map" in text and "Actual monitoring visits vs PD reference locations" in text
    for words in (
        "Actual visit",
        "Matched by coordinates",
        "Matched by name only",
        "PD locations not visited",
    ):
        assert words in text, words
    assert "matched by location name" in text and "Matched by place" not in text
    config = json.loads(
        re.search(r'<script id="fmm-map-config" type="application/json">(.*?)</script>', html, re.S).group(1)
    )
    labels = [entry["label"].rsplit(" (", 1)[0] for entry in config["legend"]]
    order = ["Actual visit", "Matched by coordinates", "Matched by name only", "PD locations not visited"]
    assert labels == [label for label in order if label in labels]
    Visit.objects.filter(key="1722").update(latitude=None, longitude=None)
    cache.clear()
    text = _text(_tab(client_viewer, "map"))
    assert re.search(r"\d+ visits? not shown — no location coordinates recorded\.", text)


# ------------------------------------------------------------------------------------------ Also 2-3
def test_the_drill_window_shows_fms_columns_and_no_people(built, client_viewer):
    html = client_viewer.get(DRILL, {"year": "2026", "section": ""}, HTTP_HX_REQUEST="true").content.decode()
    heads = re.findall(r'<th scope="col">([^<]+)</th>', html)
    assert heads == [
        "Visit",
        "Date",
        "Entity",
        "Partner",
        "PD number",
        "Location",
        "Section",
        "Rating",
        "Quality",
        "Urgency",
        "Flags",
    ]
    assert "Amel Association" in html  # the partner's full name
    assert "@" not in _text(html.split("<tbody>", 1)[1])
    # with an entity type filter, the entity is the row that was counted
    narrowed = client_viewer.get(
        DRILL, {"year": "2026", "section": "", "entity_type": "partner"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    rows = re.findall(r"<tr data-key=\"(\w+)\".*?</tr>", narrowed, re.S)
    entities = metrics.visit_entities(_scope("year=2026&section=&entity_type=partner"), rows)
    assert entities and set(entities.values()) <= {"Amel Association", "Mercy Corps Lebanon"}


def test_the_visits_table_takes_25_50_or_100_a_page(built, client_viewer, monkeypatch):
    html = _tab(client_viewer, "visits")
    assert re.search(r'class="fmm-page-size__opt" aria-current="true">50<', html)
    small = client_viewer.get(
        reverse("fmm:visits"), {"year": "2026", "section": "", "page_size": "25"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert re.search(r'aria-current="true">25<', small)
    assert "page_size=25" in small.split("<thead>", 1)[1].split("</thead>", 1)[0]  # the sort links keep it
    for wrong in ("7", "abc", "1000"):
        page = client_viewer.get(
            reverse("fmm:visits"), {"year": "2026", "section": "", "page_size": wrong}, HTTP_HX_REQUEST="true"
        ).content.decode()
        assert re.search(r'aria-current="true">50<', page), wrong
    monkeypatch.setattr(views, "PAGE_SIZES", (2, 50, 100))
    two = client_viewer.get(
        reverse("fmm:visits"), {"year": "2026", "section": "", "page_size": "2"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert two.count("<tr data-href=") == 2
    assert "page_size=2&amp;page=2" in two  # the page links keep it


# ------------------------------------------------------------------------------------------ help guide
def test_the_help_guide_names_the_analysis_panels_as_the_page_does():
    guide = (Path(settings.BASE_DIR) / "neurodb" / "help" / "guide" / "monitoring-insights.md").read_text()
    for name in ANALYSIS_ORDER[:8] + (
        "Visit locations map",
        "Matched by name only",
        "Per page",
        "25, 50 or 100",
    ):
        assert name.lower() in guide.lower(), name
    for old in ("Flags per visit", "| Sections |", "| Flags by rule |", "ten bars", "| Quality rules |"):
        assert old not in guide, old
