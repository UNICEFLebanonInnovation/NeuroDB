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


def test_raw_etools_data_prints_every_column_wrapping_on_a_landscape_page_of_its_own():
    """The print of 24 Oct: every source table cut after "eTools said it had", rows ten lines high (the hidden
    columns' texts wrapping in narrow cells), the header printed, the cards held to unfold's 640 px."""
    css = (STATIC / "admin.css").read_text()
    block = _print_block(css, "Raw eTools data printed or saved as PDF")
    assert "@page nd-raw { size: A4 landscape; margin: 10mm; }" in block
    assert "body:has(.nd-raw) { page: nd-raw; }" in block
    assert "body:has(.nd-raw) #content { max-width: none !important; }" in block
    assert "body:has(.nd-raw) #page > :not(#main), body:has(.nd-raw) #main > :has(#header-inner)" in block
    assert ".nd-raw .nd-scroll { overflow: visible !important; margin: 0; }" in block
    assert re.search(
        r"\.nd-raw \.nd-jobs th, \.nd-raw \.nd-jobs td \{ white-space: normal !important; "
        r"overflow-wrap: break-word;",
        block,
    )
    # a date, a code and a record's one-word cell never break; only a record id made from its content may
    assert (
        ".nd-raw .nd-jobs code, .nd-raw .nd-raw__when, .nd-raw .nd-raw__records td.nd-raw__atom "
        "{ white-space: nowrap !important; }"
    ) in block
    assert block.count("anywhere") == 1 and ".nd-raw .nd-raw__records th.nd-raw__long" in block
    assert '.nd-raw .nd-jobs th[scope="row"] { min-width: 0; max-width: none; }' in block
    # a long text (an indicator) keeps a column to read it in: with none, partner reports with real indicator
    # titles printed 12 to 14 lines a row, three rows a sheet
    assert ".nd-raw .nd-raw__records td.nd-raw__long { min-width: 7rem; max-width: none; }" in block
    # a field's key in a heading breaks after its "_" in print only (raw_admin._heading puts a <wbr> there)
    assert ".nd-raw .nd-raw__records th wbr { display: inline; }" in block
    assert (
        ".nd-raw__records th wbr { display: none; }"
        in css.split("Raw eTools data printed or saved as PDF")[0]
    )
    assert ".nd-raw form, .nd-raw .nd-raw__review { display: none !important; }" in block
    assert ".nd-raw details.nd-check-group::details-content { content-visibility: visible; }" in block
    assert not re.search(r"@page\s*\{", css)  # every other admin page keeps the browser's sheet
    raw = Path(settings.BASE_DIR) / "neurodb" / "datamart" / "templates" / "admin" / "datamart" / "raw"
    for name in ("index.html", "source.html", "record.html"):
        assert '<div id="content-main" class="nd-found nd-raw">' in (raw / name).read_text(), name
    index, source = (raw / "index.html").read_text(), (raw / "source.html").read_text()
    assert index.count('<span class="nd-raw__when">') == 4  # each last sync's moment, linked or not
    assert source.count('class="nd-raw__review"') == 2  # the review column's heading and its cells


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
