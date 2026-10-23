"""Printed or saved as PDF, the eTools reporting checks and Data health print every table whole: on screen
their wide tables scroll sideways (and Data health's inside 560 px), so a PDF copy cut off their numbers
(check 5's "what they share" and check 7's tie-breakers came out empty). The checks print on A4 landscape,
a named page of their own, so every other admin page keeps the browser's sheet, across the whole sheet (the
admin's container would hold them to 640 px); check 10's folded lists, and Data health's Power BI details,
are opened before printing by a script that carries the page's nonce. A cell breaks inside a word or a
figure only when it cannot fit at all, so reference numbers and counts print whole (check 10's dates hold
together by their non-breaking spaces: tests/reports/test_reporting_checks_feeds.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css"
TEMPLATES = Path(settings.BASE_DIR) / "neurodb" / "reports" / "templates" / "reports"


def _print_block(css: str, after: str) -> str:
    """The ``@media print`` block that follows the comment holding ``after``."""
    rest = css.split(after, 1)[1]
    found = re.search(r"@media print \{\n(.*?)\n\}", rest, re.S)
    assert found is not None, after
    return found.group(1)


def test_the_checks_print_every_column_wrapping_on_a_landscape_page_of_their_own():
    css = (STATIC / "admin.css").read_text()
    block = _print_block(css, "eTools reporting checks printed or saved as PDF")
    assert "@page nd-checks { size: A4 landscape; margin: 10mm; }" in block
    assert "body:has(.nd-checks) { page: nd-checks; }" in block
    assert ".nd-checks .nd-scroll { overflow: visible !important; margin: 0; }" in block
    assert re.search(
        r"\.nd-checks \.nd-jobs th, \.nd-checks \.nd-jobs td \{ white-space: normal !important; "
        r"overflow-wrap: break-word;",
        block,
    )
    assert "anywhere" not in block  # a reference number or a count never splits to fit a column
    # the whole landscape sheet: unfold's container holds the page to 40rem at the print dialog's portrait width
    assert "body:has(.nd-checks) #content { max-width: none !important; }" in block
    assert '.nd-checks .nd-jobs th[scope="row"] { min-width: 0; }' in block
    assert ".nd-checks .nd-found__actions form { display: none; }" in block  # the run button
    assert "#page > :not(#main)" in block and "#main > :has(#header-inner)" in block  # sidebar, header
    assert ".nd-checks details.nd-check-group::details-content { content-visibility: visible; }" in block
    # admin.css is loaded on every admin page: no sheet size of its own outside the checks' named page
    assert not re.search(r"@page\s*\{", css)


def test_data_health_prints_each_table_whole():
    css = (STATIC / "app.css").read_text()
    block = _print_block(css, "Data health printed: each table whole")
    for part in ("databases", "runs", "datasets", "power-bi"):
        assert f"#part-{part} .table-wrap" in block and f"#part-{part} .table :is(th, td)" in block
    assert "overflow: visible !important; max-height: none !important; background: none;" in block
    assert "white-space: normal !important; overflow-wrap: break-word; position: static;" in block
    assert "anywhere" not in block  # a figure of Recent runs never splits ("781,0" over "14")
    assert ".health-tables { grid-template-columns: 1fr; }" in block  # the imports, then the runs, each whole
    # Compared with Power BI: each period's details print open (the page opens them before printing)
    assert (
        "#part-power-bi details.power-bi-details::details-content { content-visibility: visible; }" in block
    )
    # the parts the selectors name, and the inline heights they lift
    page = (TEMPLATES / "data_health.html").read_text()
    for part in ("databases", "runs", "datasets", "power-bi"):
        assert f'{{% part "{part}"' in page, part
    for name in ("databases", "runs", "datasets"):
        assert (
            'class="table-wrap" style="max-height: 560px"'
            in (TEMPLATES / "parts" / f"health_{name}.html").read_text()
        )


@pytest.mark.django_db
def test_the_checks_page_marks_itself_for_print_and_opens_its_groups_with_the_page_s_nonce(
    client, admin_user
):
    client.force_login(admin_user)
    response = client.get(reverse("admin:datamart_reporting_checks"))
    page = response.text
    assert '<div id="content-main" class="nd-found nd-checks">' in page
    nonce = re.search(r"'nonce-([^']+)'", response["Content-Security-Policy"]).group(1)
    script = page.split(f'<script nonce="{nonce}">', 1)[1].split("</script>", 1)[0]
    assert 'addEventListener("beforeprint"' in script and 'addEventListener("afterprint"' in script
    assert "details.nd-check-group:not([open])" in script


@pytest.mark.django_db
def test_data_health_opens_the_power_bi_details_to_print_with_the_page_s_nonce(client, admin_user):
    client.force_login(admin_user)
    response = client.get(reverse("reports:data_health"))
    nonce = re.search(r"'nonce-([^']+)'", response["Content-Security-Policy"]).group(1)
    scripts = response.text.split(f'<script nonce="{nonce}">')[1:]
    (script,) = [s.split("</script>", 1)[0] for s in scripts if "beforeprint" in s]
    assert 'addEventListener("beforeprint"' in script and 'addEventListener("afterprint"' in script
    assert "#part-power-bi details.power-bi-details:not([open])" in script
