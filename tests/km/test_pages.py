"""The KM Database page as stage 1 draws it (SPEC_km §3.1-3.5, §3.8): the header, the version line, the two
tabs with their counts, the empty and loading states, and the plain tables, 25 rows a page, with their phone
columns. Fake generated workbooks only."""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from neurodb.km import columns as cols
from neurodb.km import views
from neurodb.km.models import KMFinding, KMVersion
from tests.km import factory

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}


def _html(client, name="km:index", **params) -> str:
    response = client.get(reverse(name), params)
    assert response.status_code == 200
    return response.content.decode()


def test_the_empty_page_says_who_uploads_it(client, client_viewer, admin_user):
    html = _html(client_viewer)
    assert "The KM Database is empty." in html and "An administrator uploads it." in html
    assert reverse("admin:km_kmversion_upload") not in html
    client.force_login(admin_user)  # an Administrator with staff status: the admin's link
    html = _html(client)
    assert "Upload the workbook in admin → KM Database → Versions." in html
    assert f'href="{reverse("admin:km_kmversion_upload")}"' in html
    assert "<table" not in html


def test_an_htmx_request_with_nothing_shown_reloads_the_page(client_viewer):
    response = client_viewer.get(reverse("km:index"), {"page": 2}, **HX)
    assert response.status_code == 204 and response["HX-Refresh"] == "true"


def test_the_header_the_version_line_and_the_tabs(client_viewer):
    edits = {("findings", 2, "Data Quality Status"): "Review required: fake reason"}
    factory.load(factory.workbook(documents=3, findings=30, edits=edits))
    html = _html(client_viewer)
    assert "<title>KM Database · " in html
    assert '<h1 class="page-title">KM Database</h1>' in html
    assert "Findings from research, evaluations and assessments" in html
    line = re.search(r'<p class="km-version" data-km-version>([^<]+)</p>', html).group(1)
    assert line.startswith("Prepared on 1 Oct 2026 · loaded on ")
    assert line.endswith(" · 3 documents · 30 findings · 1 marked for review")
    tabs = re.findall(
        r'<a href="([^"]+)"( aria-current="page")?>(\w+) <span class="km-tabs__count">\((\d+)\)', html
    )
    assert tabs == [
        (reverse("km:index"), ' aria-current="page"', "Findings", "30"),
        (reverse("km:documents"), "", "Documents", "3"),
    ]
    assert "Upload a new version" not in html  # a Viewer sets nothing up


def test_without_a_prepared_on_date_the_line_says_loaded_on(client_viewer):
    factory.load(factory.workbook(read_me=False))
    assert re.search(
        r"data-km-version>Loaded on \d+ \w+ \d{4} · 3 documents · 4 findings<", _html(client_viewer)
    )


def test_a_version_loading_keeps_the_one_shown(client_viewer):
    factory.load(factory.workbook())
    factory.version(factory.workbook(findings=5))
    html = _html(client_viewer)
    assert "A new version is loading. This one stays until it is ready." in html and "<table" in html


def test_the_findings_table_its_phone_columns_and_pills(client_viewer, monkeypatch):
    long = "Fake long key finding. " * 15
    edits = {
        ("findings", 2, "Key Finding"): long,
        ("findings", 3, "Evidence Confidence Level"): "Fake level",
        ("findings", 4, "Data Quality Status"): "Review required: fake reason",
    }
    factory.load(factory.workbook(edits=edits))
    html = _html(client_viewer)
    table = html.split('id="km-findings"', 1)[1].split("</table>", 1)[0]
    heads = re.findall(r'<th scope="col"( class="[^"]*")?>([^<]+)</th>', table)
    assert heads == [
        ("", "Finding"),
        (' class="d-none d-md-table-cell"', "Pillar"),
        (' class="d-none d-md-table-cell"', "Question"),
        (' class="d-none d-md-table-cell"', "Document"),
        (' class="d-none d-md-table-cell"', "Confidence"),
    ]
    assert table.count("<tr>") == 5  # the header and four findings
    shown, rest = views.fold(long)
    assert shown.endswith("key…") and len(shown) <= 221 and (shown[:-1] + " " + rest).strip() == long.strip()
    assert shown in table and "<summary>Show all</summary>" in table and rest in table
    assert views.fold("short") == ("short", "") and views.fold("x" * 300) == ("x" * 220 + "…", "x" * 80)
    assert '<span class="cell-sub d-md-none">' in table  # the phone's line: document, pillar, question, level
    assert '<span class="has-tip" title="P1 – Fake pillar one">P1</span>' in table
    assert '<span class="has-tip" title="RQ1 - Fake question one">RQ1</span>' in table
    assert "Fake document title 1" in table
    assert (
        '<span class="pill pill--sm pill--neutral" title="Levels A to D, as the workbook grades the evidence.">Level A</span>'
        in table
    )
    assert 'pill--warning" title="Fake level">Check this value</span>' in table
    assert ">Marked for review</span>" in table
    assert "pill--success" not in table
    assert "data-table-copy" not in html and "data-table-csv" not in html
    monkeypatch.setattr(cols, "CONFIDENCE_A_STRONGEST", True)
    assert 'pill--success" title="Level A: the strongest evidence' in _html(client_viewer)


def test_the_page_never_titles_itself_with_a_workbook_title(client_viewer):
    factory.load(factory.workbook())
    for name in ("km:index", "km:documents"):
        title = re.search(r"<title>([^<]*)</title>", _html(client_viewer, name)).group(1)
        assert title.startswith("KM Database · ") and "Fake" not in title


def test_texts_are_escaped(client_viewer):
    edits = {("findings", 2, "Key Finding"): "<script>alert(1)</script> fake"}
    factory.load(factory.workbook(edits=edits))
    html = _html(client_viewer)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; fake" in html


def test_25_rows_a_page_and_the_htmx_partial(client_viewer):
    factory.load(factory.workbook(documents=3, findings=30))
    html = _html(client_viewer)
    assert html.count('<td class="km-finding">') == 25
    assert "Showing 1–25 of 30" in html and 'hx-target="#km-results"' in html
    response = client_viewer.get(reverse("km:index"), {"page": 2}, **HX)
    partial = response.content.decode()
    assert partial.count('<td class="km-finding">') == 5 and "<html" not in partial
    assert "HX-Request" in response["Vary"]


def test_the_documents_tab(client_viewer):
    factory.load(factory.workbook())
    html = _html(client_viewer, "km:documents")
    table = html.split('id="km-documents"', 1)[1].split("</table>", 1)[0]
    assert (
        '<th scope="col" class="d-none d-md-table-cell d-print-none" data-export="no">Publishing entity / authors</th>'
        in table
    )
    assert 'data-export="no"' in table.split("<tbody>", 1)[1]  # the authors' cells too: screen only
    assert factory.ENTITY in table  # shown on screen as the workbook gives it
    assert table.count("<tr>") == 4 and "Fake document title 2" in table
    assert re.search(r'<td class="num">2</td>', table)  # the first document's findings, counted
    assert 'aria-current="page">Documents' in html


def test_the_review_link_of_the_version_page_lists_what_to_review(client_viewer):
    edits = {
        ("findings", 2, "Data Quality Status"): "Review required: fake reason",
        ("findings", 3, "Prenatal"): "Fake mark",
    }
    factory.load(factory.workbook(findings=6, edits=edits))
    html = _html(client_viewer, quality="review")
    assert html.count('<td class="km-finding">') == 2
    assert "Showing the findings and documents marked for review or with a value to check." in html
    assert re.search(
        r'<a href="/km/\?quality=review"( aria-current="page")?>Findings', html
    )  # the tabs keep it
    assert "No documents match." in _html(client_viewer, "km:documents", quality="review")  # none to review


def test_the_page_reads_a_fixed_number_of_queries(client_viewer, django_assert_max_num_queries):
    factory.load(factory.workbook(documents=3, findings=30))
    _html(client_viewer)  # warm: the menu and the session
    with django_assert_max_num_queries(12) as small:
        _html(client_viewer)
    KMFinding.objects.all().delete()
    KMVersion.objects.all().delete()
    factory.load(factory.workbook(documents=10, findings=200))
    _html(client_viewer)
    with django_assert_max_num_queries(12) as large:
        _html(client_viewer)
    assert len(small.captured_queries) == len(large.captured_queries)
