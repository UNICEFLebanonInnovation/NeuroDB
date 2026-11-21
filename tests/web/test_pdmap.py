"""The partner monitoring map's script (static/js/pdmap.js): each point is filled with its worst status and
ringed and sized by its level (a governorate, a district or a smaller place), in design tokens for the light
and dark themes; the side list and the place's details name the level; and a place's details list its
partners, programme documents and indicators without any programme document budget line."""

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
# details for a programme document that still carries budget fields (as a payload stored by an older release)
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
  key: "100", url: "/partner-monitoring/indicator/7/100/", output: "1.1 Services", target: 100, reached_here: 40,
  reached_here_in_year: 40, reached: 80, achieved_percent: 80, tracking: "on_track", tracking_label: "On track",
  period: "2026-06-30", report: "QPR1", planned: true, reported: true,
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
