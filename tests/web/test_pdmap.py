"""The partner monitoring map's script (static/js/pdmap.js): each point is filled with its worst status and
ringed and sized by its level (a governorate, a district or a smaller place), in design tokens for the light
and dark themes; the side list and the place's details name the level; and a place's details list its
partners, programme documents and indicators without any programme document budget line, and without the
location's own Reached (the "Reached here" columns): each indicator's target, Reached over every location,
status and latest period."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")
FUNDING_WORDS = ("Total budget", "UNICEF cash", "partner contribution", "donors", "agreement", "USD")

# pdmap.js run in node: the design tokens as the light theme gives them, a place at each level, and a place's
# details for a programme document that still carries budget fields and an indicator row that still carries the
# location's own Reached (reached_here, reached_here_in_year, prp_cumulative_here), as a payload stored by an
# older release
PDMAP_SCRIPT = r"""
const tokens = {
  "--nd-danger": "#c63b3b", "--nd-success": "#16865a", "--nd-warning": "#b76e00", "--nd-neutral": "#6b7686",
  "--nd-muted": "#5b6778", "--nd-level-country": "#111111", "--nd-level-governorate": "#222222",
  "--nd-level-district": "#333333", "--nd-level-place": "#444444",
};
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => tokens[name] || "" });
const pdmap = await import(process.argv[1]);
const levels = { country: "Whole country", governorate: "Governorate", district: "District", place: "Smaller place" };
const config = { partnerUrl: "/partners/0/", levels };
const place = (key, level, indicators, worst, extra = {}) => ({
  key, name: key, p_code: key, level, indicators, partners: 1, pds: 1, worst, latitude: 34, longitude: 36,
  governorate: "Akkar", district: "", approximate: false, rows: [], ...extra,
});
const points = [
  place("Gov", "governorate", 3, "off_track"),
  place("Dist", "district", 3, "on_track"),
  place("Cad", "place", 3, "not_reported", { level_name: "Cadastral" }),
  place("Unknown", undefined, 3, "over_target"),
];
const features = pdmap.features(points);
const row = {
  pd_id: 7, pd: "LEB/PD7", partner: "Example Partner", partner_id: 5, indicator: "# of children reached",
  key: "100", url: "/partner-monitoring/indicator/7/100/", output: "1.1 Services", target: 100, reached_here: 41,
  reached_here_in_year: 39, prp_cumulative_here: 43, reached: 80, achieved_percent: 80, tracking: "on_track",
  tracking_label: "On track", period: "2026-06-30", report: "QPR1", planned: true, reported: true,
};
const data = {
  year: 2026, report_type: "QPR",
  pds: { 7: {
    id: 7, number: "LEB/PD7", title: "Child protection", url: "/programme-documents/7/", total_budget: 987654,
    unicef_cash: 876543, partner_contribution: 765432, currency: "USD", donors: ["Example Donor Fund"],
    agreement: "LEB/PCA2026901",
  } },
};
const detail = pdmap.detailHTML(place("Dist", "district", 1, "on_track", { rows: [row] }), data, config);
console.log(JSON.stringify({
  properties: features.map((f) => f.properties),
  paint: pdmap.paint(),
  layout: pdmap.layout(),
  detail,
  table: pdmap.tableHTML(points, "", config),
  districts: pdmap.tableHTML(points, "district", config),
}));
"""


@pytest.fixture(scope="module")
def drawn():
    if NODE is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", PDMAP_SCRIPT, (STATIC / "js" / "pdmap.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


def test_points_keep_their_status_fill_and_get_a_ring_per_level(drawn):
    paint = drawn["paint"]
    # the fill: the worst status at the place, in the status tokens
    fill = paint["circle-color"]
    assert fill[:2] == ["match", ["get", "worst"]]
    pairs = dict(zip(fill[2:-1:2], fill[3:-1:2], strict=True))
    assert pairs == {
        "off_track": "#c63b3b",
        "on_track": "#16865a",
        "over_target": "#b76e00",
        "no_target": "#6b7686",
        "not_reported": "#5b6778",
    }
    # the ring: the place's level, in the level tokens
    ring = paint["circle-stroke-color"]
    assert ring[:2] == ["match", ["get", "level"]]
    assert dict(zip(ring[2:-1:2], ring[3:-1:2], strict=True)) == {
        "country": "#111111",
        "governorate": "#222222",
        "district": "#333333",
        "place": "#444444",
    }
    assert paint["circle-radius"] == ["get", "radius"] and paint["circle-stroke-width"] == ["get", "ring"]
    # the smaller places are drawn over the larger ones
    assert drawn["layout"] == {"circle-sort-key": ["get", "order"]}


def test_a_governorate_a_district_and_a_smaller_place_differ_in_size_and_ring(drawn):
    props = {p["key"]: p for p in drawn["properties"]}
    gov, dist, cad = props["Gov"], props["Dist"], props["Cad"]
    # as many indicators each: the size tells the level
    assert gov["radius"] > dist["radius"] > cad["radius"]
    assert gov["ring"] > dist["ring"] > cad["ring"]
    assert gov["order"] < dist["order"] < cad["order"]
    assert (gov["level"], dist["level"], cad["level"]) == ("governorate", "district", "place")
    # a place with no level given is drawn as a smaller place
    assert props["Unknown"]["level"] == "place" and props["Unknown"]["radius"] == cad["radius"]
    assert {p["worst"] for p in props.values()} == {"off_track", "on_track", "not_reported", "over_target"}


def test_the_side_list_names_each_place_s_level(drawn):
    table = drawn["table"]
    assert table.count("<tr ") == 4
    for word in ("Governorate", "District", "Smaller place"):
        assert word in table
    assert 'pdmap-ring pdmap-ring--district" aria-hidden="true"' in table
    # "district" in the search box finds the districts
    assert drawn["districts"].count("<tr ") == 1 and 'data-pdmap-row="Dist"' in drawn["districts"]


def test_a_place_s_details_name_its_level_and_carry_no_budget_line(drawn):
    detail = drawn["detail"]
    assert "District" in detail["meta"]
    body = detail["body"]
    for word in FUNDING_WORDS + ("987", "876", "765", "Example Donor Fund", "LEB/PCA2026901"):
        assert word not in body, word
    # the partner and programme document links stay, and the indicator opens its own pop-up
    assert '<a href="/partners/5/">Example Partner</a>' in body
    assert '<a href="/programme-documents/7/">LEB/PD7</a>' in body
    assert 'hx-get="/partner-monitoring/indicator/7/100/?report_type=QPR&year=2026"' in body


def test_a_place_s_details_have_no_reached_here_columns(drawn):
    """A place's indicator table has no "Reached here" nor "Reached here <year>" column (nor the PRP running
    total under the first): an older payload's location figures are simply not drawn. Every other column
    stays, with its values."""
    body = drawn["detail"]["body"]
    heads = re.findall(r"<th(?: [^>]*)?>(.*?)</th>", body)
    assert heads == ["Indicator", "Target (PD)", "Reached (all locations)", "Status", "Latest period"]
    assert "Reached here" not in body and " in 2026" not in body and "in the year chosen" not in body
    cells = re.findall(r"<td(?: [^>]*)?>(.*?)</td>", body, re.S)
    assert len(cells) == len(heads)
    # the location's own Reached (41), in the year (39) and PRP's running total there (43) are not shown
    for value in ("41", "39", "43", "PRP"):
        assert value not in body, value
    indicator, target, reached, status, period = cells
    assert "# of children reached" in indicator and "1.1 Services" in indicator
    assert target == "100"
    assert reached.startswith("80") and "80% of target" in reached
    assert 'data-status="on_track"' in status and "On track" in status
    assert "2026-06-30" in period and "QPR1" in period


def test_the_script_has_no_budget_words_and_reads_the_level_tokens():
    """The JS-free check: no budget, UNICEF cash, partner contribution, donor or agreement words in the map's
    script, and the level ring colours are design tokens of both themes."""
    script = (STATIC / "js" / "pdmap.js").read_text()
    for word in ("Total budget", "UNICEF cash", "partner contribution", "donors", "agreement", "money("):
        assert word not in script, word
    css = (STATIC / "css" / "app.css").read_text()
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    dark = css.split('[data-bs-theme="dark"] {', 1)[1].split("}", 1)[0]
    for level in ("country", "governorate", "district", "place"):
        token = f"--nd-level-{level}"
        assert f'"{token}"' in script
        assert re.search(rf"{token}: #[0-9a-f]{{6}};", root), token
        assert re.search(rf"{token}: #[0-9a-f]{{6}};", dark), token
        assert f".pdmap-ring--{level} {{" in css
    # the theme switch repaints the points and dims the map in the dark theme, as the other maps do
    assert 'document.addEventListener("nd:themechange", recolour)' in script
    assert "raster-brightness-max" in script


# Two maps on one page, as a quick view of the map opened over the map page puts them (quickview.js): one in the
# page's content, one in the window. Each fills its own tiles, list, totals and unlocated block, never the other's.
TWO_MAPS_SCRIPT = r"""
const dom = await import(process.argv[2]);
const env = dom.installPage({ href: "http://localhost/partner-monitoring/map/" });
const { doc, main, clock } = env;
const h = doc.h.bind(doc);
doc.body.append(h("script", { id: "asset-urls", type: "application/json" },
  JSON.stringify({ maplibreCss: "/static/m.css", maplibre: "/static/m.js", maplibreWorker: "/static/w.js" })));
const shell = (api) => {
  const map = h("section", { "data-module": "pdmap", "data-config": "map-config" }, h("div", { "data-map-canvas": "" }));
  const box = h("div", {},
    h("div", { class: "kpi-grid", "data-pdmap-kpis": "" }, ...Array.from({ length: 7 }, () => h("div", { class: "kpi__value" }, "…"))),
    map,
    h("span", { "data-map-totals": "" }),
    h("input", { type: "search", "data-pdmap-search": "" }),
    h("table", {}, h("tbody", { "data-pdmap-table": "" }, h("tr", {}, h("td", {}, "loading")))),
    h("section", { "data-pdmap-detail": "", hidden: true }, h("h2", { "data-pdmap-detail-title": "" }), h("span", { "data-pdmap-detail-sub": "" }), h("div", { "data-pdmap-detail-body": "" })),
    h("section", { "data-pdmap-unlocated": "", hidden: true }, h("div", { "data-pdmap-unlocated-list": "" })),
    h("script", { id: "map-config", type: "application/json" }, JSON.stringify({ api: api, levels: {} })),
  );
  return { box, map };
};
const page = shell("/api/page");
main.append(page.box);
const view = h("div", { class: "qview", "data-quick-view": "", "data-quick-url": "/partner-monitoring/map/?pd=8" });
const quick = shell("/api/quick");
view.append(quick.box);
doc.body.append(h("div", { id: "modal" }, h("div", { id: "modal-content" }, view)));
const point = (key) => ({ key, name: key, p_code: key, level: "district", indicators: 2, partners: 1, pds: 1, worst: "on_track",
  latitude: 34, longitude: 36, governorate: "", district: "", rows: [] });
const data = {
  "/api/page": { points: [point("A"), point("B")], unlocated: [],
    totals: { locations: 21, indicators: 33, programme_documents: 11, partners: 5, located: 21, approximate: 0,
      status_counts: { on_track: 4, off_track: 18, not_reported: 6 } } },
  "/api/quick": { points: [point("C")], unlocated: [{ name: "Nowhere", p_code: "X1", indicators: 1 }],
    totals: { locations: 4, indicators: 3, programme_documents: 1, partners: 1, located: 4, approximate: 0,
      status_counts: { on_track: 0, off_track: 3, not_reported: 0 } } },
};
globalThis.fetch = async (url) => ({ ok: true, status: 200, json: async () => data[url] });
const pdmap = await import(process.argv[1]);
const state = (s) => ({
  tiles: s.box.querySelectorAll(".kpi__value").map((el) => el.textContent),
  table: s.box.querySelector("[data-pdmap-table]").innerHTML,
  totals: s.box.querySelector("[data-map-totals]").innerHTML,
  unlocated: !s.box.querySelector("[data-pdmap-unlocated]").hidden,
});
pdmap.init(page.map).catch(() => {});
await clock.tick(0);
const afterPage = { page: state(page), quick: state(quick) };
pdmap.init(quick.map).catch(() => {});
await clock.tick(0);
console.log(JSON.stringify({ afterPage, afterQuick: { page: state(page), quick: state(quick) } }));
"""


def test_two_maps_on_one_page_each_fill_only_their_own_tiles_and_list():
    if NODE is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            TWO_MAPS_SCRIPT,
            (STATIC / "js" / "pdmap.js").as_uri(),
            (Path(__file__).parent / "parts_dom.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    drawn = json.loads(out.stdout)
    page, quick = drawn["afterPage"]["page"], drawn["afterPage"]["quick"]
    assert page["tiles"] == ["21", "33", "11", "5", "4", "18", "6"]
    assert 'data-pdmap-row="A"' in page["table"] and "21" in page["totals"]
    assert quick["tiles"] == ["…"] * 7 and quick["table"] == ""
    page, quick = drawn["afterQuick"]["page"], drawn["afterQuick"]["quick"]
    # the window's map fills the window's tiles, list, totals and unlocated block; the page keeps its own
    assert quick["tiles"] == ["4", "3", "1", "1", "0", "3", "0"]
    assert 'data-pdmap-row="C"' in quick["table"] and 'data-pdmap-row="A"' not in quick["table"]
    assert "without coordinates" in quick["totals"] and quick["unlocated"]
    assert page["tiles"] == ["21", "33", "11", "5", "4", "18", "6"]
    assert 'data-pdmap-row="A"' in page["table"] and "without coordinates" not in page["totals"]
    assert not page["unlocated"]


def test_the_map_and_the_chart_downloads_read_their_own_quick_view():
    """Every element the map fills, and the chart a Download PNG button saves, is looked up from the map's or
    the button's own place (the quick view it is in), never across the whole page."""
    pdmap = (STATIC / "js" / "pdmap.js").read_text()
    assert "document.querySelector(" not in pdmap
    charts = (STATIC / "js" / "charts.js").read_text()
    assert "document.querySelector(button.dataset" not in charts
    assert "scopedQuery(button.dataset.chartPng, button)" in charts
    assert "scopedQuery(button.dataset.rowsPng, button)" in charts


def test_the_help_names_the_whole_country_level_and_the_dark_mode_rings():
    """The map draws a place at the whole-country level larger than a governorate, with a near-black ring
    (white in dark mode), and the other rings lighter in dark mode: the help says so, and never that a
    governorate has the largest circle."""
    guide = (Path(settings.BASE_DIR) / "neurodb" / "help" / "guide" / "partner-progress.md").read_text()
    flat = " ".join(guide.split())
    assert "a governorate has the largest circle" not in flat
    assert (
        "The whole country, when an indicator is planned for it, has the largest circle and a dark ring"
        in flat
    )
    assert "In dark mode the rings are lighter, and the whole country's is white." in flat
    script = (STATIC / "js" / "pdmap.js").read_text()
    assert 'country: { token: "--nd-level-country"' in script and "radius: [20, 26]" in script
