"""The KM Database page (SPEC_km §3.1-3.6, §3.8): the header, the version line, the tabs that keep the filter,
the empty and loading states, the search box, the filters, sorting and the page size, the tables with their
phone columns and quick views, the finding and document pages, the page for a row the version does not hold,
and the queries a page reads. Fake generated workbooks only."""

from __future__ import annotations

import datetime
import re

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.km import columns as cols
from neurodb.km import loading, search, views
from neurodb.km.models import KMFinding, KMVersion, forget_live
from tests.km import factory

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}
LATE = datetime.datetime(2026, 10, 20, 22, 30, tzinfo=datetime.UTC)  # 21 Oct 2026, 01:30 in Beirut
F = factory.finding_id
# six findings over two documents: F1, F3, F5 in document 1 (P1); F2, F4, F6 in document 2 (P2)
F1, F2, F3, F4, F5, F6 = F(1, 1), F(2, 1), F(1, 2), F(2, 2), F(1, 3), F(2, 3)
VARIED = {
    ("findings", 2, "Key Finding"): "Fake child labour rose among fakepupils.",
    ("findings", 3, "Key Finding"): "Fake gender-based violence services were fakeclosed.",
    ("findings", 3, "Research Question"): "RQ2 - Fake question two",
    ("findings", 3, "Evidence Confidence Level"): "Level C",
    ("findings", 3, "Publication Year"): 2020,
    ("findings", 3, "Early Childhood 0-4"): "Yes",
    ("findings", 3, "Female"): "No",
    ("findings", 4, "Key Finding"): "<script>alert(1)</script> fake zqkmtoken",
    ("findings", 4, "Analytical Domain"): "2. Fake domain two",
    ("findings", 4, "Evidence Origin"): "Secondary: fake origin text",
    ("findings", 4, "Prenatal"): "Yes",
    ("findings", 5, "Evidence Confidence Level"): None,
    ("findings", 5, "Publication Year"): "Not reported",
    ("findings", 5, "Male"): "Yes",
    ("findings", 5, "SEM Policy / Enabling Environment"): "Yes",
    ("findings", 6, "Data Quality Status"): "Review required: fake reason",
    ("findings", 7, "Research Question"): "RQ1 - Fake question one, as pillar two words it",
    ("documents", 3, "Document / Study Type"): "Fake review",
    ("documents", 3, "Document Title"): "Fake document title two zqkmdoc",
    ("documents", 2, "Publishing Entity / Authors"): "Fakeorg Zqkmauthor Group; Doe, J.",
}


def _html(client, name="km:index", *args, **params) -> str:
    response = client.get(reverse(name, args=args), params)
    assert response.status_code == 200, response.status_code
    return response.content.decode()


def _ids(html: str) -> list[str]:
    """The Finding IDs a findings table shows, in order."""
    return re.findall(r'<span class="cell-sub">(TST-\d{4}-F\d{5})', html)


def _documents(html: str) -> list[str]:
    return re.findall(r'<span class="cell-sub">(TST-\d{4})[<\s]', html)


@pytest.fixture
def varied(db):
    return factory.load(factory.workbook(documents=2, findings=6, edits=VARIED))


# ----------------------------------------------------------------------------------- header and states
def test_the_empty_page_says_who_uploads_it(client, client_viewer, admin_user):
    html = _html(client_viewer)
    assert "The KM Database is empty." in html and "An administrator uploads it." in html
    assert reverse("admin:km_kmversion_upload") not in html
    client.force_login(admin_user)  # an Administrator with staff status: the admin's link
    html = _html(client)
    assert "Upload the workbook in admin → KM Database → Versions." in html
    assert f'href="{reverse("admin:km_kmversion_upload")}"' in html
    assert "<table" not in html and "data-filter-bar" not in html


def test_an_htmx_request_with_nothing_shown_reloads_the_page(client_viewer):
    response = client_viewer.get(reverse("km:index"), {"page": 2}, **HX)
    assert response.status_code == 204 and response["HX-Refresh"] == "true"


TABS = re.compile(
    r'<a href="([^"]+)" data-current-query=""(?: data-drop-query="(\w+)")?( aria-current="page")?>(\w+) '
    r'<span class="km-tabs__count">\(([\d,]+)\)'
)


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
    assert TABS.findall(html) == [
        (reverse("km:index"), "", ' aria-current="page"', "Findings", "30"),
        (reverse("km:documents"), "document", "", "Documents", "3"),
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


# ------------------------------------------------------------------------------------------- the tables
def test_the_findings_table_its_phone_columns_quick_views_and_pills(client_viewer, monkeypatch):
    long = "Fake long key finding. " * 15
    edits = {
        ("findings", 2, "Key Finding"): long,
        ("findings", 3, "Evidence Confidence Level"): "Fake level",
        ("findings", 4, "Data Quality Status"): "Review required: fake reason",
    }
    factory.load(factory.workbook(edits=edits))
    html = _html(client_viewer)
    table = html.split('id="km-findings"', 1)[1].split("</table>", 1)[0]
    heads = re.findall(r'<th scope="col"( class="[^"]*")?[^>]*>(.*?)</th>', table)
    assert [(cls, re.sub(r"<[^>]+>", "", text)) for cls, text in heads] == [
        ("", "Finding"),
        (' class="d-none d-md-table-cell"', "Pillar"),
        (' class="d-none d-md-table-cell"', "Question"),
        (' class="d-none d-md-table-cell"', "Document · year"),
        (' class="d-none d-md-table-cell"', "Confidence"),
    ]
    assert table.count("<tr>") == 5  # the header and four findings
    shown, rest = views.fold(long)
    assert shown.endswith("key…") and len(shown) <= 221 and (shown[:-1] + " " + rest).strip() == long.strip()
    assert shown in table and "<summary>Show all</summary>" in table and rest in table
    assert views.fold("short") == ("short", "") and views.fold("x" * 300) == ("x" * 220 + "…", "x" * 80)
    # the phone's line: pillar, question, level, document
    assert '<span class="cell-sub d-md-none km-clamp-1">P1 · RQ1 · ' in table
    assert '<span class="has-tip" title="P1 – Fake pillar one">P1</span>' in table
    assert '<span class="has-tip" title="RQ1 - Fake question one">RQ1</span>' in table
    document_url = reverse("km:document", args=["TST-0001"])
    assert f'<a class="km-clamp-2" href="{document_url}">Fake document title 1</a>' in table
    for finding in (F(1, 1), F(2, 1), F(3, 1), F(1, 2)):  # an Open button each: the quick view
        url = reverse("km:finding", args=[finding])
        assert (
            f'<a class="btn btn-outline-secondary btn-sm km-open" href="{url}" hx-get="{url}" '
            'hx-target="#modal-content" hx-push-url="false">Open</a>'
        ) in table
    assert (
        '<span class="pill pill--sm pill--neutral" title="Levels A to D, as the workbook grades the '
        'evidence.">Level A</span>'
    ) in table
    assert 'pill--warning" title="Fake level">Check this value</span>' in table
    assert ">Marked for review</span>" in table
    assert "pill--success" not in table and "<tr data-href" not in table
    assert "data-table-copy" not in html and "data-table-csv" not in html  # the downloads follow the rule
    monkeypatch.setattr(cols, "CONFIDENCE_A_STRONGEST", True)
    html = _html(client_viewer)
    assert 'pill--success" title="Level A: the strongest evidence' in html
    assert "Level A: the strongest evidence" in html.split('id="km-confidence-help"', 1)[1][:200]


def test_the_page_never_titles_itself_with_a_workbook_title(client_viewer):
    factory.load(factory.workbook())
    pages = [
        ("km:index", ()),
        ("km:documents", ()),
        ("km:finding", (F(1, 1),)),
        ("km:document", ("TST-0001",)),
    ]
    titles = [re.search(r"<title>([^<]*)</title>", _html(client_viewer, n, *a)).group(1) for n, a in pages]
    assert titles == [
        "KM Database · NeuroDB",
        "KM Database · NeuroDB",
        "Finding · KM Database · NeuroDB",
        "Document · KM Database · NeuroDB",
    ]


def test_texts_are_escaped_and_searched_words_marked(client_viewer, varied):
    html = _html(client_viewer, q="zqkmtoken")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; fake <mark>zqkmtoken</mark>" in html
    assert search.highlight("a <b> & labour", ["labour", "amp"]) == "a &lt;b&gt; &amp; <mark>labour</mark>"


def test_25_rows_a_page_the_page_size_and_the_htmx_partial(client_viewer):
    factory.load(factory.workbook(documents=3, findings=60))
    html = _html(client_viewer)
    assert html.count('<td class="km-finding">') == 25
    assert "Showing 1–25 of 60" in html and 'hx-target="#km-results"' in html
    assert re.search(
        r'Rows a page:</span> <strong aria-current="true">25</strong> <a href="/km/\?per=50">50</a>', html
    )
    response = client_viewer.get(reverse("km:index"), {"page": 2}, **HX)
    partial = response.content.decode()
    assert partial.count('<td class="km-finding">') == 25 and "<html" not in partial
    assert "HX-Request" in response["Vary"]
    assert _html(client_viewer, per=50).count('<td class="km-finding">') == 50
    html = _html(client_viewer, per=50, pillar="P1")
    assert '<input type="hidden" name="per" value="50">' in html  # the filter bar keeps it
    pages = _html(client_viewer, pillar="P1").split('class="pagination-bar"', 1)[1]
    assert "Showing 1–25 of 30" in pages and "page=2&amp;pillar=P1" in pages.replace(
        "pillar=P1&amp;page=2", "page=2&amp;pillar=P1"
    )
    assert _html(client_viewer, per=7).count('<td class="km-finding">') == 25  # not a page size: 25


def test_the_documents_tab(client_viewer):
    factory.load(factory.workbook())
    html = _html(client_viewer, "km:documents")
    table = html.split('id="km-documents"', 1)[1].split("</table>", 1)[0]
    assert (
        '<th scope="col" class="d-none d-md-table-cell d-print-none" data-export="no">Publishing entity / '
        "authors</th>"
    ) in table
    assert 'data-export="no"' in table.split("<tbody>", 1)[1]  # the authors' cells too: screen only
    assert factory.ENTITY in table  # shown on screen as the workbook gives it
    assert table.count("<tr>") == 4 and "Fake document title 2" in table
    assert f'<a href="{reverse("km:index")}?document=TST-0001">2</a>' in table  # its findings, in the table
    assert 'aria-current="page">Documents' in html
    assert "data-table-copy" not in html and "data-table-csv" not in html


# -------------------------------------------------------------------------------------------- filters
@pytest.mark.parametrize(
    ("params", "shown"),
    [
        ({}, [F1, F3, F5, F6, F2, F4]),  # newest year first (F2: 2020, F4: none), then the workbook's order
        ({"pillar": "P2"}, [F6, F2, F4]),
        ({"confidence": "C"}, [F2]),
        ({"confidence": ["C", "none"]}, [F2, F4]),
        ({"year": "2020"}, [F2]),
        ({"year": "none"}, [F4]),
        ({"year": ["2020", "none"]}, [F2, F4]),
        ({"type": "Fake review"}, [F6, F2, F4]),  # the document's type
        ({"document": "TST-0001"}, [F1, F3, F5]),
        ({"domain": "D2"}, [F3]),
        ({"age": "early_childhood"}, [F2]),
        ({"age": ["early_childhood", "prenatal"]}, [F3, F2]),  # any of them
        ({"age": "early_childhood", "pillar": "P1"}, []),  # and every filter
        ({"sex": "male"}, [F4]),
        ({"sem": "sem_policy"}, [F4]),
        ({"theme": "mhpss"}, [F1, F3, F5, F6, F2, F4]),
        ({"origin": "O"}, [F3]),
        ({"origin": ["P", "none"]}, [F1, F5, F6, F2, F4]),
        ({"quality": "review"}, [F5]),
        ({"quality": "ready"}, [F1, F3, F6, F2, F4]),
        (
            {"pillar": "P9", "year": "abc", "confidence": "Z", "sort": "pk", "age": "fake", "quality": "x"},
            [F1, F3, F5, F6, F2, F4],
        ),  # values the version does not hold are left out
    ],
)
def test_each_filter_alone_and_combined(client_viewer, varied, params, shown):
    assert _ids(_html(client_viewer, **params)) == shown


def test_a_research_question_is_its_pillar_and_its_wording(client_viewer, varied):
    html = _html(client_viewer)
    select = html.split('id="km-rq"', 1)[1].split("</select>", 1)[0]
    groups = re.findall(r'<optgroup label="([^"]+)">(.*?)</optgroup>', select)
    assert [label for label, _ in groups] == ["P1 – Fake pillar one", "P2 – Fake pillar two"]
    p1, p2 = (re.findall(r'<option value="(\w+)">([^<]+)</option>', body) for _, body in groups)
    assert [text for _, text in p1] == ["RQ1 - Fake question one"]
    assert [text for _, text in p2] == [
        "RQ1 - Fake question one",
        "RQ1 - Fake question one, as pillar two words it",
        "RQ2 - Fake question two",
    ]
    p1_rq1, p2_rq1 = p1[0][0], p2[0][0]
    assert p1_rq1 != p2_rq1  # the same code and wording in two pillars: two questions
    assert _ids(_html(client_viewer, rq=p2_rq1)) == [F4]
    assert _ids(_html(client_viewer, rq=[p1_rq1, p2_rq1])) == [F1, F3, F5, F4]
    html = _html(client_viewer, rq=p2_rq1, pillar="P1")  # a question of another pillar is left out
    assert _ids(html) == [F1, F3, F5]
    assert "The question filter was left out: the questions you picked are in another pillar." in html


def test_the_tabs_keep_the_filter_and_the_documents_tab_drops_the_document(client_viewer, varied):
    html = _html(client_viewer, pillar="P2", document="TST-0002", q="fake", sort="pillar", page=2)
    tabs = {name: url for url, _, _, name, _ in TABS.findall(html)}
    assert tabs["Findings"] == f"{reverse('km:index')}?q=fake&amp;pillar=P2&amp;document=TST-0002"
    assert tabs["Documents"] == f"{reverse('km:documents')}?q=fake&amp;pillar=P2"
    assert '<input type="hidden" name="document" value="TST-0002">' in html
    assert '<input type="hidden" name="sort" value="pillar">' in html
    documents = _html(client_viewer, "km:documents", pillar="P2", document="TST-0002")
    assert _documents(documents) == ["TST-0002"]
    assert 'name="document"' not in documents  # no document filter on that tab


def test_the_document_filter_shows_the_document_title_on_a_chip(client_viewer, varied):
    html = _html(client_viewer, document="TST-0002", pillar="P2")
    assert (
        '<span class="chip chip--removable">Document: Fake document title two zqkmdoc <a class="chip__remove" '
        'href="/km/?pillar=P2"'
    ) in html
    assert re.search(r">3 findings of 6<", html)  # the count line says it is filtered


def test_the_filter_bar(client_viewer, varied):
    html = _html(client_viewer)
    form = html.split("data-filter-bar", 1)[1].split("</form>", 1)[0]
    search_box = form.split('class="filter-bar__field km-search"', 1)[1].split("</div>", 1)[0]
    assert 'name="q"' in search_box and 'placeholder="Words, a phrase in quotes, or an ID"' in search_box
    assert 'data-hx-target="#km-results"' in html and "data-filters-collapse" in html
    assert '<details class="km-more">' in form  # More filters, folded
    assert '<details class="km-more" open>' in _html(client_viewer, sex="male")
    chips = re.findall(
        r'<label class="chip km-chip"><input type="checkbox" name="(\w+)" value="(\w+)"> ([^<]+)</label>',
        form,
    )
    labels = [label for _, _, label in chips]
    assert len([c for c in chips if c[0] != "origin"]) == len(cols.FLAGS)
    assert "Ages 10-14" in labels and "Household and family" in labels
    assert "Mental health and psychosocial support (MHPSS)" in labels
    assert not any(re.search(r"\bSEM\b", label) or label == "MHPSS" for label in labels)
    assert re.findall(r'<legend class="form-label">([^<]+)</legend>', form) == [
        "Age groups",
        "Girls and boys",
        "Level of the social-ecological model",
        "Cross-cutting themes",
        "Evidence",
        "Data quality",
    ]
    assert "Levels A to D, as the workbook grades the evidence." in form  # the levels' legend
    assert "Several choices in one filter show findings that match any of them" in html
    year = form.split('id="km-year"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="(\w+)">', year) == ["2022", "2020", "none"]


# --------------------------------------------------------------------------------------------- search
@pytest.mark.parametrize(
    ("q", "shown", "how"),
    [
        (F3, [F3], "id"),
        ("tst-0002", [F2, F4, F6], "id"),  # a Document ID: its findings
        ("child labor", [F1], "words"),  # US spelling finds the UK one
        ("GBV", [F2], "words"),  # an acronym finds its expansion
        ('"labour rose"', [F1], "words"),  # a phrase
        ("fakeclosed or fakepupils", [F1, F2], "words"),
        ("zqkmtoken -february", [F3], "words"),  # a word left out
        ("zqkmtok", [F3, F4, F5, F6], "letters"),  # the fallback: the letters typed
    ],
)
def test_the_search_box(client_viewer, varied, q, shown, how):
    html = _html(client_viewer, q=q)
    assert sorted(_ids(html)) == sorted(shown)
    note = "No exact word matches; showing findings that contain the letters you typed."
    assert (note in html) == (how == "letters")


def test_a_search_ranks_the_best_match_first(client_viewer):
    edits = {
        ("findings", 2, "Measure"): "fakeunique",  # a finding's measure weighs less than its key finding
        ("findings", 6, "Key Finding"): "Fake fakeunique fakeunique fakeunique words.",
    }
    factory.load(factory.workbook(documents=2, findings=6, edits=edits))
    assert _ids(_html(client_viewer, q="fakeunique")) == [F5, F1]


def test_the_documents_tab_searches_titles_authors_and_findings(client_viewer, varied):
    assert _documents(_html(client_viewer, "km:documents", q="GBV")) == ["TST-0002"]  # one of its findings
    assert _documents(_html(client_viewer, "km:documents", q="zqkmdoc")) == ["TST-0002"]  # its title
    assert _documents(_html(client_viewer, "km:documents", q="Zqkmauthor")) == ["TST-0001"]  # its authors
    assert _documents(_html(client_viewer, "km:documents", q="TST-0002")) == ["TST-0002"]
    html = _html(client_viewer, "km:documents", q="docum")
    assert "showing documents that contain the letters you typed" in html


@pytest.mark.parametrize(
    ("sort", "shown"),
    [
        ("year", [F2, F1, F3, F5, F6, F4]),
        ("-year", [F1, F3, F5, F6, F2, F4]),
        ("confidence", [F1, F3, F5, F6, F2, F4]),  # Level A to D, not given last
        ("pillar", [F1, F3, F5, F4, F6, F2]),
        ("document", [F1, F3, F5, F2, F4, F6]),
    ],
)
def test_sorting(client_viewer, varied, sort, shown):
    html = _html(client_viewer, sort=sort)
    assert _ids(html) == shown
    assert f'<input type="hidden" name="sort" value="{sort}">' in html


def test_the_sort_links_load_the_page_with_the_filter(client_viewer, varied):
    html = _html(client_viewer, pillar="P2")
    table = html.split('id="km-findings"', 1)[1].split("</thead>", 1)[0]
    assert '<a class="sort-link" href="/km/?pillar=P2&amp;sort=pillar">Pillar</a>' in table
    assert '<a class="km-sort-year" href="/km/?pillar=P2&amp;sort=-year">year</a>' in table
    assert "hx-get" not in table  # a sort is a page of its own: the filter bar keeps it
    head = (
        _html(client_viewer, pillar="P2", sort="-year")
        .split('id="km-findings"', 1)[1]
        .split("</thead>", 1)[0]
    )
    assert 'aria-sort="descending" data-km-sorted="year"' in head
    assert '<a class="km-sort-year" href="/km/?pillar=P2&amp;sort=year">year</a>' in head
    documents = _html(client_viewer, "km:documents", sort="findings")
    assert (
        'aria-sort="descending"><a class="sort-link" href="/km/documents/?sort=findings">Findings</a>'
        in documents
    )
    assert _documents(_html(client_viewer, "km:documents", sort="title")) == ["TST-0001", "TST-0002"]


def test_nothing_matches(client_viewer, varied):
    html = _html(client_viewer, q="zzfakenothing")
    assert "No findings match. Try fewer words or clear a filter." in html
    assert 'href="/km/">Clear filters</a>' in html


# ---------------------------------------------------------------------------------- finding and document
def test_the_finding_page(client_viewer, varied):
    html = _html(client_viewer, "km:finding", F2)
    assert '<h1 class="page-title">Finding</h1>' in html and f'<p class="page-subtitle">{F2}</p>' in html
    crumbs = re.findall(r'<li class="breadcrumb-item[^"]*"[^>]*>\s*(?:<a href="([^"]+)">)?([^<]+)', html)
    assert [(url, label.strip()) for url, label in crumbs] == [
        ("/km/", "KM Database"),
        ("/km/documents/TST-0002/", "Fake document title two zqkmdoc"),
        ("", "Finding"),
    ]
    assert "Fake gender-based violence services were fakeclosed." in html.split('class="km-lead"', 1)[1][:200]
    assert "Fields the workbook leaves empty are not shown." in html
    titles = re.findall(r'<h2 class="km-group__title">([^<]+)</h2>', html)
    assert titles == ["The finding", "Where it fits", "Who it is about", "The evidence", "Its document"]
    labels = re.findall(r"<dt>([^<]+)</dt>", html)
    assert "Also relevant to" not in labels and "Link" not in labels  # the workbook leaves them empty
    assert "Finding analysis" in labels and "Research question" in labels and "Methods and sample" in labels
    marked = html.split("<dt>Marked Yes</dt>", 1)[1].split("</dd>", 1)[0]
    assert re.findall(r'<span class="km-marks__title">([^<]+)</span>', marked) == [
        "Age groups",
        "Cross-cutting themes",
    ]
    assert '<span class="chip">Ages 0-4</span><span class="chip">Ages 10-14</span>' in marked
    assert "<summary>Not marked Yes</summary>" in marked and "<strong>No:</strong> Before birth" in marked
    assert ">Level C</span>" in html and ">Ready</span>" in html
    assert 'href="/km/?document=TST-0002"' in html and 'href="/km/documents/TST-0002/"' in html
    assert "data-autoprint" in html


def test_the_finding_page_shows_the_authors_on_screen_and_the_organisations_on_paper(client_viewer, varied):
    html = _html(client_viewer, "km:finding", F1)
    screen = html.split('<div class="km-dl__row km-dl__row--long d-print-none">', 1)[1].split("</div>", 1)[0]
    assert "<dt>Publishing entity / authors (as the workbook gives it)</dt>" in screen
    assert factory.ENTITY in screen
    paper = html.split('<div class="km-dl__row d-none d-print-block">', 1)[1].split("</div>", 1)[0]
    assert re.search(r"<dt>Publishing organisations</dt>\s*<dd>Fakeorg Institute \+ 2 authors", paper)
    assert "Doe" not in paper and "Roe" not in paper


def test_the_quick_view_and_the_values_to_check(client_viewer):
    edits = {
        ("findings", 2, "Evidence Confidence Level"): "Fake level",
        ("findings", 2, "Prenatal"): "Fake mark",
        ("findings", 2, "Data Quality Status"): "Review required: fake reason",
        ("findings", 2, "Document / Study Type"): "Fake other type",
    }
    factory.load(factory.workbook(edits=edits))
    response = client_viewer.get(reverse("km:finding", args=[F(1, 1)]), **HX)
    html = response.content.decode()
    assert response.status_code == 200 and "<html" not in html
    assert '<div class="modal-header">' in html and 'id="modal-title">Finding</h2>' in html
    assert f'href="{reverse("km:finding", args=[F(1, 1)])}">Open as page</a>' in html
    assert 'title="Fake level">Check this value</span>' in html
    assert re.search(r"Check this value</span> Before birth \(pregnancy\)", html)  # the mark to check
    assert '>Marked for review</span> <span class="km-text">Review required: fake reason</span>' in html
    assert "The documents sheet gives “Fake study”." in html


def test_a_link_from_an_older_version_says_the_finding_may_have_changed(client_viewer):
    old = factory.load(factory.workbook())
    factory.load(factory.workbook(findings=5))
    html = _html(client_viewer, "km:finding", F(1, 1), v=old.pk)
    assert (
        f"This answer was written from the version {old.label()}; this finding may have changed since."
        in html
    )
    for v in ("abc", "999999", str(KMVersion.objects.get(status="live").pk)):
        assert "may have changed since" not in _html(client_viewer, "km:finding", F(1, 1), v=v)


def test_the_document_page(client_viewer, varied):
    html = _html(client_viewer, "km:document", "TST-0001")
    assert '<h1 class="page-title">Fake document title 1</h1>' in html
    assert "<title>Document · KM Database · NeuroDB</title>" in html
    labels = re.findall(r"<dt>([^<]+)</dt>", html)
    assert labels[:3] == [
        "Document ID",
        "Publishing entity / authors (as the workbook gives it)",
        "Publishing organisations",
    ]
    assert "Link" not in labels  # empty in the workbook
    assert "Its findings" in html and sorted(_ids(html)) == sorted([F1, F3, F5])
    assert 'id="km-document-findings"' in html and f'href="{reverse("km:index")}?document=TST-0001"' in html
    assert "/km/?document=TST-0001&amp;export=xlsx" in html  # its findings' downloads


def test_a_document_page_lists_its_findings_25_a_page(client_viewer):
    factory.load(factory.workbook(documents=1, findings=30))
    html = _html(client_viewer, "km:document", "TST-0001")
    assert html.count('<td class="km-finding">') == 25 and 'hx-target="#km-document-findings"' in html
    response = client_viewer.get(reverse("km:document", args=["TST-0001"]), {"page": 2}, **HX)
    assert response.content.decode().count('<td class="km-finding">') == 5


def test_a_row_the_version_does_not_hold_answers_404(client_viewer):
    factory.load(factory.workbook())
    for name, key, sentence in (
        ("km:finding", "TST-9999-F00001", "This finding is not in the version of the KM Database shown now"),
        ("km:document", "TST-9999", "This document is not in the version of the KM Database shown now"),
    ):
        response = client_viewer.get(reverse(name, args=[key]))
        html = response.content.decode()
        assert response.status_code == 404 and sentence in html
        assert "It may have been removed by a newer version." in html
        assert 'href="/km/">Back to the KM Database' in html
    assert client_viewer.get("/km/findings/bad id/").status_code == 404


def test_the_scripts_carry_the_page_nonce(client_viewer, varied):
    for name, args in (("km:index", ()), ("km:finding", (F1,)), ("km:document", ("TST-0001",))):
        response = client_viewer.get(reverse(name, args=args))
        policy = response["Content-Security-Policy"]
        nonce = policy.split("'nonce-")[1].split("'")[0]
        scripts = re.findall(r"<script\b[^>]*>", response.content.decode())
        run = [tag for tag in scripts if 'type="application/json"' not in tag]  # data, not run
        assert run and all(f'nonce="{nonce}"' in tag for tag in run), (name, run)


# ------------------------------------------------------------------------------------------- queries
def _queries(client, name, *args, headers=None, **params) -> int:
    url = reverse(name, args=args)
    client.get(url, params, **(headers or {}))  # warm: the session, the menu, the facets
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url, params, **(headers or {}))
        if getattr(response, "streaming", False):
            b"".join(response.streaming_content)
    assert response.status_code == 200
    return len(captured.captured_queries)


PAGES = [
    ("km:index", (), {}),
    (
        "km:index",
        (),
        {
            "q": "zqkmtoken",
            "pillar": "P1",
            "confidence": "A",
            "year": "2022",
            "age": "female",
            "sort": "pillar",
        },
    ),
    ("km:documents", (), {}),
    ("km:documents", (), {"q": "fake", "pillar": "P1"}),
    ("km:finding", (F(1, 1),), {}),
    ("km:finding", (F(1, 1),), {"v": "0"}),
    ("km:document", ("TST-0001",), {}),
    ("km:index", (), {"export": "xlsx"}),
    ("km:index", (), {"export": "csv"}),
    ("km:documents", (), {"export": "xlsx"}),
]


def test_each_page_reads_as_many_queries_for_20_or_200_findings(client_viewer):
    counts = {}
    for size, documents in ((20, 3), (200, 10)):
        KMFinding.objects.all().delete()
        KMVersion.objects.all().delete()
        forget_live()
        factory.load(factory.workbook(documents=documents, findings=size))
        counts[size] = [_queries(client_viewer, n, *a, **p) for n, a, p in PAGES]
        counts[size].append(_queries(client_viewer, "km:index", headers=HX, pillar="P1", q="fake"))
    assert counts[20] == counts[200], counts
    assert max(counts[20]) <= 16, counts
