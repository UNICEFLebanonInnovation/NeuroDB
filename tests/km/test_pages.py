"""The KM Database page as stage 1 draws it (SPEC_km §3.1-3.5, §3.8): the header, the version line, the two
tabs with their counts, the empty and loading states, and the plain tables, 25 rows a page, with their phone
columns. Fake generated workbooks only."""

from __future__ import annotations

import datetime
import re

import pytest
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from neurodb.km import columns as cols
from neurodb.km import loading, views
from neurodb.km.models import KMFinding, KMVersion, forget_live
from tests.km import factory

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}
LATE = datetime.datetime(2026, 10, 20, 22, 30, tzinfo=datetime.UTC)  # 21 Oct 2026, 01:30 in Beirut


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
        r'<a href="([^"]+)"( aria-current="page")?>(\w+) <span class="km-tabs__count">\(([\d,]+)\)', html
    )
    assert tabs == [
        (reverse("km:index"), ' aria-current="page"', "Findings", "30"),
        (reverse("km:documents"), "", "Documents", "3"),
    ]
    assert "Upload a new version" not in html  # a Viewer sets nothing up
    KMVersion.objects.filter(status="live").update(documents_count=1, findings_count=14_869)
    forget_live()
    html = _html(client_viewer)
    assert " · 1 document · 14,869 findings · 1 marked for review<" in html  # grouped as the tabs are
    assert re.findall(r'<span class="km-tabs__count">\(([\d,]+)\)', html) == ["14,869", "1"]


def test_without_a_prepared_on_date_the_line_says_loaded_on(client_viewer):
    factory.load(factory.workbook(read_me=False))
    assert re.search(
        r"data-km-version>Loaded on \d+ \w+ \d{4} · 3 documents · 4 findings<", _html(client_viewer)
    )


def test_the_dates_are_local_not_utc(client_viewer, settings):
    settings.TIME_ZONE = "Asia/Beirut"
    shown = factory.load(factory.workbook(read_me=False))
    KMVersion.objects.filter(pk=shown.pk).update(finished_at=LATE)
    shown.refresh_from_db()
    assert shown.label() == "loaded on 21 Oct 2026"
    assert "data-km-version>Loaded on 21 Oct 2026 · 3 documents · 4 findings<" in _html(client_viewer)
    KMVersion.objects.filter(pk=shown.pk).update(prepared_on=datetime.date(2026, 10, 1))
    shown.refresh_from_db()
    assert shown.label(loaded=True) == "prepared on 1 Oct 2026 (loaded on 21 Oct 2026)"


def test_a_version_loading_keeps_the_one_shown(client_viewer):
    factory.load(factory.workbook())
    factory.version(factory.workbook(findings=5))
    html = _html(client_viewer)
    assert "A new version is loading. This one stays until it is ready." in html and "<table" in html


def test_the_first_load_says_so_and_nothing_else(client_viewer, admin_user, client):
    factory.version(factory.workbook())  # the first upload, seconds old: nothing is shown yet
    client.force_login(admin_user)  # an Administrator: no upload button while it loads
    for html in (_html(client_viewer), _html(client)):
        assert "The KM Database is being loaded for the first time." in html
        assert "It shows here when it is ready." in html
        assert "A new version is loading" not in html and "This one stays" not in html
        assert "The KM Database is empty." not in html and "An administrator uploads it." not in html
        assert "Upload the workbook in admin" not in html


@pytest.fixture
def other_session():
    """A second database session, as the load's own process is."""
    other = connections.create_connection("default")
    yield other
    other.close()


def test_a_load_that_stopped_does_not_say_it_is_loading_for_good(client_viewer, other_session):
    factory.load(factory.workbook())
    stopped = factory.version(b"fake", status="loading")
    with other_session.cursor() as cursor:  # the load runs: its lock is held
        cursor.execute("SELECT pg_advisory_lock(%s)", [loading.LOCK_ID])
    try:
        assert "A new version is loading." in _html(client_viewer)
    finally:
        with other_session.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [loading.LOCK_ID])
    # the container restarted: nobody holds the lock, so the version can never finish
    html = _html(client_viewer)
    assert "A new version is loading." not in html and "<table" in html
    stopped.refresh_from_db()
    assert stopped.status == "failed"
    never = factory.version(b"fake")  # queued, and its load never started
    KMVersion.objects.filter(pk=never.pk).update(uploaded_at=timezone.now() - datetime.timedelta(minutes=3))
    assert "A new version is loading." not in _html(client_viewer)
    assert KMVersion.objects.get(pk=never.pk).status == "failed"


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
