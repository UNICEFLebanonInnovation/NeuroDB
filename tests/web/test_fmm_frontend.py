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
