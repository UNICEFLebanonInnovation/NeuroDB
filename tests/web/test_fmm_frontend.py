"""The small shared frontend pieces Monitoring insights adds: Copy and CSV leave out cells marked
``data-export="no"`` (the team names), a filter bar keeps an empty value its form marks
``data-keep-empty`` ("section=": every section), and the HACT ratings have status colours."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

from neurodb.web.templatetags.ui import STATUS_VARIANTS

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_table_rows_skip_cells_that_are_not_exported():
    """lib.js's tableRows, run in node over a table of plain objects."""
    script = """
const lib = await import(process.argv[1]);
const cell = (text, attrs = {}) => ({ innerText: text, dataset: attrs });
const row = (...cells) => ({ children: cells });
const table = { querySelectorAll: () => [
  row(cell("Visit"), cell("Team", { export: "no" }), cell("Quality")),
  row(cell("Visit 1722"), cell("Rania  Canary", { export: "no" }), cell("64.7%", { value: "64.7" })),
] };
console.log(JSON.stringify(lib.tableRows(table)));
"""
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script, (STATIC / "js" / "lib.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(out.stdout) == [["Visit", "Quality"], ["Visit 1722", "64.7"]]


def test_lib_and_app_carry_the_export_and_keep_empty_rules():
    lib = (STATIC / "js" / "lib.js").read_text()
    assert 'cell.dataset.export !== "no"' in lib
    app = (STATIC / "js" / "app.js").read_text()
    assert "[data-keep-empty]" in app and 'params.append(name, "")' in app


def test_the_hact_ratings_and_review_marks_have_status_colours():
    charts = (STATIC / "js" / "charts.js").read_text()
    assert 'constrained: "--nd-warning"' in charts and 'not_monitored: "--nd-muted"' in charts
    assert STATUS_VARIANTS["constrained"] == "warning" and STATUS_VARIANTS["not_monitored"] == "neutral"
    assert {STATUS_VARIANTS[k] for k in ("reviewed", "follow_up", "data_issue")} == {
        "success",
        "warning",
        "danger",
    }


def test_the_monitoring_insights_styles_exist_in_both_themes():
    css = (STATIC / "css" / "app.css").read_text()
    block = css.split("Monitoring insights (/fmm/)", 1)[1]
    for selector in ("tr.row--red", "tr.row--amber", ".insight-grid", ".visit-chip", ".priority-line"):
        assert selector in block, selector
    # the row colours come from theme tokens, defined for the light and the dark theme
    assert "var(--nd-danger-soft)" in block and "var(--nd-warning-soft)" in block
    assert css.count("--nd-danger-soft:") >= 2 and css.count("--nd-warning-soft:") >= 2


# ------------------------------------------------------------------------------------------ charts
CHART_SCRIPT = """
globalThis.document = { documentElement: { getAttribute: () => "light" } };
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => (name.startsWith("--nd-") ? "#446ab3" : "") });
const charts = await import(process.argv[1]);
const B = charts.BUILDERS;
const el = (dataset = {}) => ({ dataset, clientWidth: 600 });
const dist = B.dist(el({ orientation: "h", show: "value" }), [
  { label: "0–20", value: 1, drill: "0-20" },
  { label: "80–100", value: 3, drill: "80-100" },
]);
const hbars = B.hbars(el({ suffix: " visits" }), [["R1 Completeness of a very long rule label here", 4, "R1"], ["R2 Evidence", 1, "R2"]]);
const plain = B.hbars(el(), [["On Track", 4], ["Off Track", 1]]);
const grouped = B.grouped(el({ barmode: "stack" }), {
  labels: ["May 2026", "Jun 2026"],
  series: { "On track": [1, 0], "Off track": [0, 2] },
  drill: { labels: ["2026-05", "2026-06"], series: { "On track": "on_track", "Off track": "off_track" } },
});
const groupedPlain = B.grouped(el(), { labels: ["a"], series: { x: [1] } });
const monthly = B.monthly(el(), {
  months: ["May 2026"], indicators: [{ id: "q", label: "Average quality", unit: "%", values: [64.7], reports: [1] }],
  bar_name: "Average quality", line_name: "Reports", line_unit: "reports", drill: { labels: ["2026-05"], series: {} },
});
const monthlyDefault = B.monthly(el(), { months: ["May 2026"], indicators: [{ id: "x", label: "x", values: [1], reports: [2] }] });
const template = "/fmm/drill/?year=2026&section=&month={drill}&hact_q1={series_drill}";
const click = (trace, i) => charts.drillUrl(template, { pointIndex: i, data: trace, x: "Jun 2026" });
console.log(JSON.stringify({
  dist: dist.traces[0].meta,
  hbars: hbars.traces[0].meta,
  hbarsLabels: hbars.traces[0].y,
  plain: plain.traces[0].meta ?? null,
  grouped: grouped.traces.map((t) => t.meta),
  barmode: grouped.layout.barmode,
  groupedPlain: [groupedPlain.layout.barmode, groupedPlain.traces[0].meta ?? null],
  monthly: monthly.traces.map((t) => [t.name, t.meta]),
  monthlyHover: monthly.traces[1].hovertemplate,
  monthlyDefault: monthlyDefault.traces.map((t) => t.name),
  urlOff: click(grouped.traces[1], 1),
  urlBucket: charts.drillUrl("/fmm/drill/?bucket={drill}", { pointIndex: 1, data: dist.traces[0] }),
  urlFlag: charts.drillUrl("/fmm/drill/?flag={drill}", { pointIndex: 0, data: hbars.traces[0] }),
  noDrill: charts.drillUrl("/fmm/drill/?flag={drill}", { pointIndex: 0, data: plain.traces[0] }),
  fallback: charts.drillUrl("/x?label={label}&series={series}", { pointIndex: 0, label: "On Track", data: { name: "Findings & co" } }),
  noFallbackWithDrill: charts.drillUrl("/x?label={label}", { pointIndex: 0, data: hbars.traces[0] }),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_chart_builders_carry_drill_values_per_point_and_clicks_read_them():
    """charts.js run in node: dist, hbars, grouped and monthly copy the drill values into trace.meta
    (aligned with the points as drawn), and a click fills the address from them, never from a label."""
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", CHART_SCRIPT, (STATIC / "js" / "charts.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    data = json.loads(out.stdout)
    assert data["dist"] == {"drill": ["0-20", "80-100"]}
    # hbars draws the first pair at the top (reversed) and cuts long labels: the drill values follow the bars
    assert data["hbars"] == {"drill": ["R2", "R1"]}
    assert data["hbarsLabels"][1].endswith("…")
    assert data["plain"] is None  # no drill value: not clickable
    assert data["grouped"] == [
        {"drill": ["2026-05", "2026-06"], "series": "on_track"},
        {"drill": ["2026-05", "2026-06"], "series": "off_track"},
    ]
    assert data["barmode"] == "stack" and data["groupedPlain"] == ["group", None]
    assert data["monthly"][0] == ["Average quality", {"drill": ["2026-05"], "series": None}]
    assert data["monthly"][1][0] == "Reports" and "reports" in data["monthlyHover"]
    assert data["monthlyDefault"] == ["Reported in the month", "Records"]  # the defaults are unchanged
    assert data["urlOff"] == "/fmm/drill/?year=2026&section=&month=2026-06&hact_q1=off_track"
    assert data["urlBucket"] == "/fmm/drill/?bucket=80-100"
    assert data["urlFlag"] == "/fmm/drill/?flag=R2"  # bar 0 is the last pair once reversed
    assert data["noDrill"] is None
    assert data["fallback"] == "/x?label=On%20Track&series=Findings%20%26%20co"
    assert data["noFallbackWithDrill"] is None


def test_charts_bind_the_click_through_and_release_it():
    charts = (STATIC / "js" / "charts.js").read_text()
    monthly = charts.split("  monthly(el, data) {", 1)[1].split("\n  bars(el, data)", 1)[0]
    assert "data.bar_name" in monthly and "data.line_name" in monthly and "data.line_unit" in monthly
    assert 'el.dataset.barmode === "stack"' in charts
    assert "el.dataset.hrefTemplate" in charts and 'el.on("plotly_click", onClick)' in charts
    assert "point.data.meta.drill[point.pointIndex" in charts and "meta.series" in charts
    assert 'el.removeListener?.("plotly_click", onClick)' in charts
    assert 'window.htmx.ajax("GET", url, { target: el.dataset.hrefTarget || "#modal-content" })' in charts
    assert "Click a bar to list its visits" in charts and "aria-describedby" in charts
    for builder in ("dist(el, data)", "hbars(el, data)", "grouped(el, data)", "monthly(el, data)"):
        body = charts.split(f"  {builder} {{", 1)[1].split("\n  },\n", 1)[0]
        assert "meta:" in body, builder
