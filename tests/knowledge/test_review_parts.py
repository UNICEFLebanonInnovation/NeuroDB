"""The document review in parts (STAGE3 group D2; SPEC7 §3.3-3.4 for the review).

On a large review three views take more than 300 ms even after their quick wins: findings by document (the template
of ten documents with thirty findings each), the synthesis themes (the themes and their findings) and the repeated
findings (thousands of findings compared word by word: seconds). The page answers their frame at once (the tab bar,
the view's links and filter, the repeated findings' heading) with one placeholder, ``findings`` or ``synthesis``, and
the figures come in that part. A tab or filter change (HTMX) answers the results area with the figures in it, as
before (under a second), except the repeated findings: their placeholder, unless they are already stored. The
repeated findings are kept for every worker (``review_data.repeated_groups``, plain data) under a key that every
change to the review moves (``review_data.review_stamp``) and that holds the person's Verified only switch. Every
other tab and view stays one request.

These tests check the frame without any figure work, each part, the query ceilings, that the page put together from
its parts is the page drawn at once (``_eager=1``) under a matrix of filters and three users, that a stored value
never shows one person's view to another, that every edit shows at once, and the quick wins of the review
(``document_rows`` counts by subqueries, the themes' findings in one query) with the same figures as before.

The world (:func:`world`): two batches, twelve analysed documents with three findings each (one sentence repeated in
six documents, another in three), a key statement and an action point each, and a few verdicts.
"""

from __future__ import annotations

import datetime
import gettext
import html as html_module
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches
from django.db import connection
from django.db.models import Count, Q
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.knowledge import review, review_data, review_views
from neurodb.knowledge.models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentStatement,
    ReviewBatch,
    Topic,
    Verdict,
)
from tests.web.parts import (
    HX,
    _element_end,
    assemble,
    eager,
    executable,
    get_part,
    normalise,
    placeholder_attributes,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

PAGE = "/knowledge/review/"  # knowledge:review
BY_DOCUMENT = PAGE + "?tab=findings&view=document"
THEMES = PAGE + "?tab=synthesis"
REPEATED = PAGE + "?tab=synthesis&view=repeated"
VIEWS = {BY_DOCUMENT: "findings", THEMES: "synthesis", REPEATED: "synthesis"}  # each view's one part
RESULTS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "review-results"}  # the tab bar, a filter, a chip
WATER = "Water trucking costs rose sharply in the northern districts during the dry season."
TEACHERS = "Teachers lack training in the rural schools of the north and leave early."
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
NEXT = re.compile(r'name="next" value="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). A frame of a viewer (the navigation
# stored, the shared cache empty: 12, 11, 12): the request's floor (session, user, donor check, role), the batches
# of the filter bar, the review settings, whether a verdict was ever given (the Verified only switch) and the view's
# own frame: the tags and programmes of the Findings filter (By document); the person's paragraph quota (Synthesis);
# and for the repeated findings the review's stamp, to see whether they are stored.
FRAME_QUERIES = {BY_DOCUMENT: 14, THEMES: 13, REPEATED: 14}
# Each part with nothing stored (9, 11, 6): By document reads the page of documents (count and rows), their findings
# and their key statements with the findings they cite; Themes the counted findings, their topics and the shown
# themes' findings (one query for all of them: one per theme before), and the quota; Repeated the stamp and the
# best findings. The repeated findings again, stored: 5.
PART_QUERIES = {BY_DOCUMENT: 11, THEMES: 13, REPEATED: 8}
REPEATED_STORED_QUERIES = 7
# an HTMX tab or filter change (10, 14, 8): the frame's results area and the figures (By document and Repeated: their
# placeholder)
RESULTS_QUERIES = {BY_DOCUMENT: 12, THEMES: 16, REPEATED: 10}


# ------------------------------------------------------------------------------------------------- the world
def _topics(n: int) -> list[Topic]:
    return list(
        Topic.objects.exclude(name=Topic.OTHER).select_related("subtopic__programme").order_by("pk")[:n]
    )


# words of each document's own findings: no two documents share them, so only WATER and TEACHERS repeat
TREES = "amber birch cedar delta ember fjord glade heron islet jasper kelp lotus".split()
STONES = "anvil basil cobalt dune elm flint garnet hazel iris juniper kiwi larch".split()
BEASTS = "acorn bison coral daisy eagle falcon gecko hyena ibis jackal koala llama".split()


@pytest.fixture
def world(db, roles):
    """Twelve analysed documents in two batches (eight annual reports, four donor reports), analysed one after
    the other. Each has three findings: a sentence of its own on the first topic, and either WATER (documents 0-5,
    second topic) or TEACHERS (documents 6-8, second topic) or one of its own (third topic), and a recommendation
    of its own (third topic); a key statement citing its first finding and an action point citing its second. A
    reviewer accepted the water finding of document 0 and rejected the first finding of document 11."""
    annual = ReviewBatch.objects.create(name="Annual reports")
    donors = ReviewBatch.objects.create(name="Donor reports")
    reviewer = User.objects.create_user(
        username="reviewer", email="r@example.org", password="reviewer-pass-123"
    )
    first, second, third = _topics(3)
    start = timezone.make_aware(datetime.datetime(2026, 9, 1, 9, 0))
    documents = []
    for n in range(12):
        document = Document.objects.create(
            title=f"Report {n:02d}",
            text="x",
            status=Document.Status.READY,
            year=2022 + n % 3,
            review_batch=annual if n < 8 else donors,
            review_status=Document.ReviewStatus.DONE,
            reviewed_at=start + datetime.timedelta(hours=n),
        )
        tree, stone, beast = TREES[n], STONES[n], BEASTS[n]
        own = DocumentFinding.objects.create(
            document=document, topic=first, position=1, category="challenge", evidence=40 + n,
            text=f"The {tree} {stone} {beast} survey counted pupils in {TREES[(n + 5) % 12]} villages.",
            page_label="p. 1", page_from=1, finding_date=datetime.date(2022 + n % 3, 1, 1),
        )  # fmt: skip
        shared = WATER if n < 6 else TEACHERS if n < 9 else f"A {stone} {beast} remark about {tree} only."
        topic = second if n < 9 else third
        other = DocumentFinding.objects.create(
            document=document, topic=topic, position=2, category="observation", evidence=90 - n,
            text=shared, page_label="p. 2", page_from=2,
        )  # fmt: skip
        DocumentFinding.objects.create(
            document=document, topic=third, position=3, category="recommendation", evidence=30,
            text=f"Recommend {beast} {tree} {stone} measures soon.", page_label="p. 3", page_from=3,
        )  # fmt: skip
        statement = DocumentStatement.objects.create(
            document=document, position=1, text=f"Report {n} says the first topic is urgent.", urgency=50 + n
        )
        statement.cites.add(own)
        point = DocumentActionPoint.objects.create(
            document=document, position=1, action=f"Follow up report {n}.", owner_text="Education section"
        )
        point.cites.add(other)
        documents.append(document)
    DocumentFinding.objects.filter(document=documents[0], text=WATER).update(
        verdict=Verdict.ACCEPTED, reviewed_by=reviewer, reviewed_at=start
    )
    DocumentFinding.objects.filter(document=documents[11], position=1).update(
        verdict=Verdict.REJECTED, reviewed_by=reviewer, reviewed_at=start
    )
    return {"annual": annual, "donors": donors, "documents": documents, "topics": (first, second, third)}


@pytest.fixture
def editor(db, roles):
    user = User.objects.create_user(username="editor", email="e@example.org", password="editor-pass-123456")
    from django.contrib.auth.models import Group

    user.groups.add(Group.objects.get(name=SECTION_EDITOR))
    return user


def _signed_in(user, *, verified: bool = False) -> Client:
    client = Client()
    client.force_login(user)
    if verified:  # the person's Verified only switch, kept in their session
        assert client.post(reverse("knowledge:review_verified"), {"on": "1"}).status_code == 302
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _json_blocks(html: str) -> dict[str, str]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a JSON id twice: {ids}"
    return dict(found)


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the converted views raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    monkeypatch.setattr(review_views, "_by_document", refuse)
    monkeypatch.setattr(review_views, "_themes_part", refuse)
    for name in ("synthesis", "themes_findings", "theme_findings", "repeated", "repeated_groups"):
        monkeypatch.setattr(review_data, name, refuse)


def _groups(response) -> list[dict]:
    return response.context["groups"]


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("knowledge:review") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
@pytest.mark.parametrize("url", list(VIEWS))
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch, url):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(url)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Document review" in html and 'class="page-purpose"' in html
    assert re.search(r'<nav class="segmented segmented--scroll review-tabs[^"]*"', html)  # the tab bar
    assert 'aria-current="page">Findings<' in html or 'aria-current="page">Synthesis<' in html
    assert list(placeholders(html)) == [VIEWS[url]]
    assert 'id="review-results" aria-live' not in html
    assert '<div id="review-results" data-results-updated="The results were updated.">' in html  # R2-critic-2
    if url == BY_DOCUMENT:
        assert 'aria-label="Finding filters"' in html and 'name="programme"' in html  # the filter bar
        assert 'aria-current="page">By document<' in html
        for figure in (
            "Report 00",
            'id="review-doc-',
            '<h3 class="h6 mt-2">Key statements</h3>',
            "Accept all (",
        ):
            assert figure not in html, figure
    else:
        assert 'aria-label="Synthesis filters"' in html
        assert "Raised in" not in html and "theme raised by" not in html and "themes raised by" not in html
    if url == REPEATED:  # the heading of the repeated findings stays in the frame, around the placeholder
        assert (
            '<h2 class="panel__title" id="review-repeated-title">Findings repeated across documents</h2>'
            in html
        )
        assert "words at least 60% alike" in html


@pytest.mark.shell
def test_the_placeholders_carry_the_address_filter(client_viewer, world):
    for url, name in VIEWS.items():
        found = placeholder_attributes(text(client_viewer.get(url)))[name]
        assert urlsplit(found["hx-get"]).path == PAGE
        expected = {key: values for key, values in _query(url).items()}
        assert _query(found["hx-get"]) == {**expected, "_part": [name]}, url
        assert found["data-part-priority"] == "high" and found["class"] == "part part--list"
        assert "data-part-group" not in found and "data-part-needs" not in found
    # the keys that decide the view travel in their order, a key the page does not read does not
    url = PAGE + "?tab=synthesis&view=repeated&batch=" + str(world["annual"].pk) + "&utm=x&min=3"
    found = placeholders(text(client_viewer.get(url)))["synthesis"]
    assert (
        urlsplit(found).query
        == f"tab=synthesis&view=repeated&batch={world['annual'].pk}&min=3&_part=synthesis"
    )
    url = BY_DOCUMENT + "&q=water&page=2&verdict=&category=challenge"
    assert _query(placeholders(text(client_viewer.get(url)))["findings"]) == {
        "tab": ["findings"],
        "view": ["document"],
        "q": ["water"],
        "page": ["2"],
        "verdict": [""],
        "category": ["challenge"],
        "_part": ["findings"],
    }


@pytest.mark.shell
def test_the_placeholders_keep_the_room_measured_on_the_large_world(client, viewer, editor, world):
    """The room of the first screens of each part, measured on the x20 parity world (wide, 900 px, phone): one
    document of By document (an editor's rows hold the review buttons: taller), the first ten themes, the folded
    repeated findings; a grey row for each of the first twenty lines."""
    rooms = {
        (
            "viewer",
            BY_DOCUMENT,
        ): "--part-h: 6450px; --part-h-sm: 20200px; --part-h-md: 11100px; --part-rows: 20",
        (
            "editor",
            BY_DOCUMENT,
        ): "--part-h: 7500px; --part-h-sm: 20250px; --part-h-md: 16550px; --part-rows: 20",
        ("viewer", THEMES): "--part-h: 5200px; --part-h-sm: 15300px; --part-h-md: 5950px; --part-rows: 20",
        ("viewer", REPEATED): "--part-h: 2080px; --part-h-sm: 4800px; --part-rows: 20",
    }
    for (who, url), style in rooms.items():
        client.force_login(viewer if who == "viewer" else editor)
        html = text(client.get(url))
        found = placeholder_attributes(html)[VIEWS[url]]
        assert found["style"] == style, (who, url)
        start = html.rindex("<div", 0, html.index(f'id="part-{VIEWS[url]}"'))
        part = html[start : _element_end(html, start)]
        assert part.count("skeleton__block--row") == 20


@pytest.mark.shell
@pytest.mark.parametrize("url", list(VIEWS))
def test_the_frame_stays_within_its_query_ceiling(client_viewer, world, url, django_assert_max_num_queries):
    client_viewer.get(url)  # the navigation and the sidebar stored, as after the first visit of any page
    caches["shared"].clear()
    with django_assert_max_num_queries(FRAME_QUERIES[url]):
        response = client_viewer.get(url)
    assert response.status_code == 200 and list(placeholders(text(response))) == [VIEWS[url]]


@pytest.mark.shell
def test_the_other_tabs_and_views_stay_one_request(client_viewer, world):
    """Under 300 ms on the large world after their quick wins: no part, as before (the Documents tab, the Index
    and the knowledge search among them)."""
    for params in (
        "",
        "?tab=documents",
        "?tab=findings",
        "?tab=findings&view=statements",
        "?tab=findings&view=index",
        "?tab=dashboard",
        "?tab=synthesis&view=time",
        "?tab=synthesis&view=coverage",
        "?tab=actions",
        "?tab=report",
    ):
        response = client_viewer.get(PAGE + params)
        assert response.status_code == 200 and placeholders(text(response)) == {}, params
        assert (
            client_viewer.get(PAGE + params + ("&" if params else "?") + "_part=findings", **HX).status_code
            == 404
        )
    for url in ("/knowledge/", "/knowledge/?q=water"):
        assert placeholders(text(client_viewer.get(url))) == {}


@pytest.mark.shell
def test_every_tab_gives_the_words_said_after_a_swap_of_its_results(client_viewer, world):
    """The tab bar, the view switch, the filters and the page links swap #review-results, which is not a live region
    (it holds the parts of the slow views, and the Documents tab's poll), so the swaps of the tabs and views drawn
    whole were no longer announced (review R2-critic-2). Every tab's frame gives the words parts.js says after such a
    swap, and a swap answers the area's content, never a second area."""
    for params in (
        "",
        "?tab=documents",
        "?tab=findings",
        "?tab=findings&view=document",
        "?tab=findings&view=statements",
        "?tab=findings&view=index",
        "?tab=dashboard",
        "?tab=synthesis",
        "?tab=synthesis&view=repeated",
        "?tab=synthesis&view=time",
        "?tab=synthesis&view=coverage",
        "?tab=actions",
        "?tab=report",
    ):
        html = text(client_viewer.get(PAGE + params))
        areas = re.findall(r'<div id="review-results"[^>]*>', html)
        assert areas == ['<div id="review-results" data-results-updated="The results were updated.">'], params
        swapped = client_viewer.get(PAGE + params, **RESULTS)
        assert swapped.status_code == 200 and 'id="review-results"' not in text(swapped), params


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("url", list(VIEWS))
def test_each_part_answers_its_wrapper(client_viewer, world, url):
    name = VIEWS[url]
    response = get_part(client_viewer, url, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html and 'id="review-results"' not in html
    expected = {
        BY_DOCUMENT: ('id="review-doc-', "Report 11", "Key statements", "3 findings"),
        THEMES: ("themes raised by 2 documents or more", 'id="theme-', "documents · "),
        REPEATED: ("Raised in 6 documents", "Raised in 3 documents", WATER, TEACHERS),
    }[url]
    for figure in expected:
        assert figure in html, figure
    assert _json_blocks(html) == {}  # no chart in these views: no JSON either
    assert executable(html) == []  # nothing that would run once swapped in, its included rows too


def test_the_parts_draw_the_figures_of_the_review(client_viewer, world):
    """The assembled and the eager pages draw the same part templates, so their equivalence cannot catch a wrong
    figure: the figures themselves are checked here."""
    by_document = get_part(client_viewer, BY_DOCUMENT, "findings").context
    assert [d.title for d in by_document["documents"]] == [f"Report {n:02d}" for n in range(11, 1, -1)]
    assert by_document["documents_page"].paginator.count == 12
    assert [len(d.shown_findings) for d in by_document["documents"]] == [3] * 10
    themes = get_part(client_viewer, THEMES, "synthesis").context
    first, second, third = world["topics"]
    shown = [(t.topic, t.n_documents, t.n_findings) for t in themes["shown_themes"]]
    assert shown == [(third, 12, 15), (first, 11, 11), (second, 9, 9)]  # by documents (rejected: out)
    tops = {t.topic: [f.text for f in t.top] for t in themes["shown_themes"]}
    assert tops[second][0] == WATER and len(tops[second]) == 5  # the best evidence first, five of them
    groups = _groups(get_part(client_viewer, REPEATED, "synthesis"))
    assert [(g["documents"], g["lead"]["text"]) for g in groups] == [(6, WATER), (3, TEACHERS)]
    water = groups[0]["findings"]
    assert {f["document"]["title"] for f in water} == {f"Report {n:02d}" for n in range(6)}
    found = DocumentFinding.objects.select_related("document").get(document=world["documents"][0], text=WATER)
    assert water[0] == {
        "pk": found.pk,
        "text": WATER,
        "page_label": "p. 2",
        "url": found.url,
        "evidence": 90,
        "document": {"pk": found.document_id, "title": "Report 00", "url": found.document.get_absolute_url()},
    }


@pytest.mark.parametrize("url", list(VIEWS))
def test_each_part_stays_within_its_query_ceiling(client_viewer, world, url, django_assert_max_num_queries):
    name = VIEWS[url]
    address = placeholders(text(raw_get(client_viewer, url)))[name]
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[url]):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)
    if url == REPEATED:  # stored now: the part reads it
        with django_assert_max_num_queries(REPEATED_STORED_QUERIES):
            assert client_viewer.get(address, **HX).status_code == 200


@pytest.mark.parametrize("url", list(VIEWS))
def test_a_donor_is_refused_every_part(client, world, url):
    from tests.donors.test_donor_access import make_donor

    client.force_login(make_donor().user)
    response = client.get(f"{url}&_part={VIEWS[url]}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_is_sent_to_sign_in(client, world):
    response = client.get(f"{REPEATED}&_part=synthesis", **HX)
    assert response.status_code == 200 and WATER not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


# ------------------------------------------------------------------------------------- HTMX: tab and filter changes
@pytest.mark.parametrize("url", [THEMES])
def test_a_tab_or_filter_change_answers_the_figures_at_once(
    client_viewer, world, url, django_assert_max_num_queries
):
    """Under a second on the large world: the results area comes with its figures (SPEC7_review item 3c)."""
    caches["shared"].clear()
    with django_assert_max_num_queries(RESULTS_QUERIES[url]):
        response = client_viewer.get(url, **RESULTS)
    html = text(response)
    assert response.status_code == 200 and "<html" not in html
    assert placeholders(html) == {}
    name = VIEWS[url]
    assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert ("Report 11" if url == BY_DOCUMENT else "themes raised by") in html


def test_a_change_into_by_document_answers_its_placeholder_then_its_part(
    client_viewer, world, django_assert_max_num_queries
):
    """By document's ten documents, drawn inside the results of a tab, view, filter or chip change (HTMX), took twice
    as long as in their own request or the old page's results on the large world (0.8-1.1 s for an editor against
    0.35-0.6 s: Python mapped and freed its frame stack thousands of times in that deeper rendering), past the second
    under which a change brings its figures (review R1-D2-1). The results area comes with the placeholder at once,
    with the change's filter, and the part draws the documents, as on the page's first visit."""
    caches["shared"].clear()
    url = f"{BY_DOCUMENT}&page=2"
    with django_assert_max_num_queries(RESULTS_QUERIES[BY_DOCUMENT]):
        response = client_viewer.get(url, **RESULTS)
    html = text(response)
    assert response.status_code == 200 and "<html" not in html
    found = placeholders(html)
    assert list(found) == ["findings"] and 'data-part-loaded="findings"' not in html
    assert _query(found["findings"]) == {
        "tab": ["findings"],
        "view": ["document"],
        "page": ["2"],
        "_part": ["findings"],
    }
    part = client_viewer.get(found["findings"], **HX)
    assert part.status_code == 200 and part.context["page_obj"].number == 2
    assert '<div id="part-findings" class="part is-loaded" data-part-loaded="findings"' in text(part)


def test_the_repeated_findings_change_answers_their_placeholder_until_they_are_stored(client_viewer, world):
    """Seconds on the large world: the results area comes with the placeholder, unless the repeated findings are
    stored; then with them."""
    caches["shared"].clear()
    with CaptureQueriesContext(connection) as queries:
        response = client_viewer.get(REPEATED, **RESULTS)
    html = text(response)
    assert len(queries) <= RESULTS_QUERIES[REPEATED]
    assert list(placeholders(html)) == ["synthesis"] and "Raised in" not in html
    assert "Findings repeated across documents" in html
    get_part(client_viewer, REPEATED, "synthesis")  # computed and stored
    html = text(client_viewer.get(REPEATED, **RESULTS))
    assert placeholders(html) == {} and "Raised in 6 documents" in html


def test_the_documents_poll_still_answers_its_list(client_viewer, world):
    """Another HTMX request of the page (the Documents tab's 20-second refresh, hx-select="#review-documents")
    keeps its answer: the results with the documents."""
    response = client_viewer.get(
        PAGE + "?tab=documents", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="review-documents"
    )
    html = text(response)
    assert response.status_code == 200 and 'id="review-documents"' in html and "Report 00" in html


def test_page_links_inside_the_documents_reload_that_part_only(client_viewer, world):
    html = text(get_part(client_viewer, BY_DOCUMENT, "findings"))
    links = re.findall(
        r'<a class="page-link" href="([^"]+)" hx-get="([^"]+)" hx-target="([^"]+)"[^>]*hx-push-url="([^"]+)"',
        html,
    )
    assert [_query(html_module.unescape(address))["page"] for _h, address, _t, _p in links] == [
        ["1"],
        ["2"],
        ["2"],
    ]
    for href, address, target, pushed in links:  # pages 1 and 2, and Next
        address, pushed = html_module.unescape(address), html_module.unescape(pushed)
        assert target == "#part-findings" and "_part=findings" in address and "_part" not in pushed + href
        page = _query(address)["page"]
        assert _query(pushed) == {"tab": ["findings"], "view": ["document"], "page": page}
        assert _query(html_module.unescape(href)) == {"tab": ["findings"], "view": ["document"], "page": page}
    page_two = client_viewer.get(address, **HX, HTTP_HX_TARGET="part-findings")
    assert [d.title for d in page_two.context["documents"]] == ["Report 01", "Report 00"]


# ------------------------------------------------------------------------------------------ inline when ready
def test_stored_repeated_findings_are_drawn_in_the_frame(client_viewer, world, monkeypatch):
    get_part(client_viewer, REPEATED, "synthesis")  # computed and stored

    def refuse(*args, **kwargs):
        raise AssertionError("the stored repeated findings were computed again")

    monkeypatch.setattr(review_data, "repeated", refuse)
    html = text(raw_get(client_viewer, REPEATED))
    assert placeholders(html) == {}
    assert '<div id="part-synthesis" class="part is-loaded" data-part-loaded="synthesis"' in html
    assert "Raised in 6 documents" in html
    # another batch is not stored: asked for
    other = text(raw_get(client_viewer, f"{REPEATED}&batch={world['annual'].pk}"))
    assert list(placeholders(other)) == ["synthesis"]


def test_the_stored_repeated_findings_hold_nothing_of_the_person(client, viewer, admin_user, world):
    """The same groups whoever stores them, staff or a viewer: plain data with no link or button that depends on
    the person (and the template adds none)."""
    staff, reader = _signed_in(admin_user), _signed_in(viewer)
    first = _groups(get_part(staff, REPEATED, "synthesis"))
    assert _groups(get_part(reader, REPEATED, "synthesis")) == first  # read, stored by staff
    caches["shared"].clear()
    compute.reset_memo()
    assert _groups(get_part(reader, REPEATED, "synthesis")) == first  # stored by the viewer this time
    assert text(get_part(staff, REPEATED, "synthesis")) == text(get_part(reader, REPEATED, "synthesis"))
    compute._check_plain(first)  # plain data (once() checks it too, in tests)


def test_verified_only_never_reads_the_groups_of_the_others(client, viewer, editor, world):
    """The switch lives in the session, not in the address: it is in the stored value's key. With it on, only
    accepted findings count, so only the water finding of document 0 is left: no group."""
    reader = _signed_in(viewer)
    assert (
        len(_groups(get_part(reader, REPEATED, "synthesis"))) == 2
    )  # stored for everyone without the switch
    verified = _signed_in(viewer, verified=True)
    assert _groups(get_part(verified, REPEATED, "synthesis")) == []
    DocumentFinding.objects.filter(document=world["documents"][1], text=WATER).update(
        verdict=Verdict.ACCEPTED, reviewed_at=timezone.now()
    )  # as Accept all does (QuerySet.update)
    assert [g["documents"] for g in _groups(get_part(verified, REPEATED, "synthesis"))] == [2]
    assert [g["documents"] for g in _groups(get_part(reader, REPEATED, "synthesis"))] == [6, 3]


class _EveryStringTranslated(gettext.NullTranslations):
    """A language in which every translatable string reads differently."""

    def gettext(self, message):
        return f"[{message.rsplit(translation.trans_real.CONTEXT_SEPARATOR, 1)[-1]}]"

    def ngettext(self, singular, plural, n):
        return f"[{singular if n == 1 else plural}]"

    def to_language(self):
        return "xx"


def test_the_stored_repeated_findings_read_the_same_in_every_language(world):
    """Stored once for every language (``lang_neutral``): nothing in them is translated."""
    with translation.override("en"):
        english = [review_data._plain_group(g) for g in review_data.repeated(None, False)]
    translation.trans_real._active.value = _EveryStringTranslated()
    try:
        assert translation.gettext("Documents") == "[Documents]"  # the language is on
        other = [review_data._plain_group(g) for g in review_data.repeated(None, False)]
    finally:
        translation.deactivate()
    assert other == english and english


def test_two_filters_never_share_a_key(world):
    keys = {
        review_data._repeated_key(batch, verified) for batch in (None, 1, 2) for verified in (False, True)
    }
    assert len(keys) == 6


# ------------------------------------------------------------------------------------------- fresh after an edit
def _water_documents(client) -> int:
    groups = _groups(get_part(client, REPEATED, "synthesis"))
    return next((g["documents"] for g in groups if g["lead"]["text"] == WATER), 0)


def test_every_edit_shows_in_the_stored_repeated_findings_at_once(client, editor, world):
    """Fresh after edit (STAGE3): the repeated findings are stored for an hour, and every change a person makes on
    the page moves their key: a verdict and its undo, an edit, a document taken out of the review."""
    client.force_login(editor)
    documents = world["documents"]
    assert _water_documents(client) == 6  # stored now

    def water(n: int) -> DocumentFinding:
        return DocumentFinding.objects.get(document=documents[n], position=2)

    verdict = reverse("knowledge:review_finding_verdict", args=[water(5).pk])
    assert client.post(verdict, {"verdict": "rejected"}).status_code == 302
    assert _water_documents(client) == 5  # rejected: out of every count
    assert client.post(verdict, {"verdict": "unreviewed"}).status_code == 302
    assert _water_documents(client) == 6  # undone
    edit = reverse("knowledge:review_finding_edit", args=[water(4).pk])
    response = client.post(
        edit,
        {"text": "The trucks were replaced by a new network in the whole governorate.", "quote": "",
         "topic": water(4).topic_id, "category": "observation", "kind": "reported", "place_text": "",
         "date_text": "", "page": "2"},
    )  # fmt: skip
    assert response.status_code == 302, text(response)[:500]
    assert _water_documents(client) == 5
    response = client.post(reverse("knowledge:review_remove", args=[documents[3].pk]))
    assert response.status_code == 302
    assert _water_documents(client) == 4
    response = client.post(reverse("knowledge:review_reference", args=[documents[2].pk]), {"reference": "1"})
    assert response.status_code == 302
    assert _water_documents(client) == 3


def test_accept_all_shows_at_once_with_verified_only(client, editor, world):
    """Accept all (a QuerySet.update(), which leaves no time stamp on the findings' rows but their verdicts)."""
    client.force_login(editor)
    client.post(reverse("knowledge:review_verified"), {"on": "1"})
    assert _groups(get_part(client, REPEATED, "synthesis")) == []  # stored now
    for n in (1, 2):
        response = client.post(
            reverse("knowledge:review_bulk_verdict", args=[world["documents"][n].pk]), {"verdict": "accepted"}
        )
        assert response.status_code == 302
    assert [g["documents"] for g in _groups(get_part(client, REPEATED, "synthesis"))] == [3]


def test_a_title_changed_in_the_admin_shows_at_once(client, admin_user, world):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    document = world["documents"][0]

    def titles() -> set[str]:
        groups = _groups(get_part(client, REPEATED, "synthesis"))
        return {f["document"]["title"] for f in groups[0]["findings"]}

    assert "Report 00" in titles()  # stored now
    response = client.post(
        reverse("admin:knowledge_document_change", args=[document.pk]),
        {
            "title": "Annual report of the north",
            "source": "",
            "section": "",
            "year": document.year,
            "summary": "",
            "key_points": "[]",
            "document_date": "",
            "series": "",
            "edition": "",
            "issued_on": "",
            "review_batch": document.review_batch_id,
            "links-TOTAL_FORMS": "0",
            "links-INITIAL_FORMS": "0",
            "links-MIN_NUM_FORMS": "0",
            "links-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        },
    )
    assert response.status_code == 302, text(response)[:3000]
    assert "Annual report of the north" in titles() and "Report 00" not in titles()


def test_a_finished_analysis_shows_at_once(client_viewer, world):
    """The review job writes a document's new findings and its analysis time in one transaction: the stored
    groups are read again from the moment it commits (as review._finish and _save_findings do)."""
    assert _water_documents(client_viewer) == 6  # stored now
    document = world["documents"][6]
    DocumentFinding.objects.filter(document=document, manual=False).delete()
    DocumentFinding.objects.create(
        document=document,
        topic=world["topics"][1],
        position=1,
        category="observation",
        evidence=80,
        text=WATER,
    )
    Document.objects.filter(pk=document.pk).update(reviewed_at=timezone.now())
    assert _water_documents(client_viewer) == 7


def test_locating_the_findings_again_shows_at_once(client_viewer, world):
    """*Locate document findings again* (the Run-a-job button: ``review.run(LOCATE)``) rewrites each finding's pages,
    place and evidence with ``bulk_update``: no document saved, no edit time. The stored groups are read again once
    the job's run ends (review R1-D2-2)."""

    def shown() -> set[tuple[int, str, int]]:
        groups = _groups(get_part(client_viewer, REPEATED, "synthesis"))
        return {(f["pk"], f["page_label"], f["evidence"]) for g in groups for f in g["findings"]}

    before = shown()  # stored now
    assert {label for _pk, label, _evidence in before} == {"p. 2"}
    run = review.run(review.LOCATE, triggered_by="test")
    assert run.status == SyncRun.Status.SUCCEEDED and run.rows_written == 12
    located = {
        (pk, label, evidence)
        for pk, label, evidence in DocumentFinding.objects.filter(
            pk__in=[pk for pk, _l, _e in before]
        ).values_list("pk", "page_label", "evidence")
    }
    assert located != before  # the job moved them: the documents' text ("x") holds none of their pages
    assert shown() == located


def test_a_verdict_shows_at_once_by_document_and_in_the_themes(client, editor, world):
    """The two other parts are never stored: what a person decides shows at the next look."""
    client.force_login(editor)
    finding = DocumentFinding.objects.get(document=world["documents"][11], position=2)
    url = BY_DOCUMENT + f"&document={world['documents'][11].pk}"
    before = text(get_part(client, url, "findings"))
    assert before.count("Not reviewed") >= 2
    client.post(reverse("knowledge:review_finding_verdict", args=[finding.pk]), {"verdict": "accepted"})
    after = get_part(client, url, "findings")
    shown = {f.pk: f.verdict for f in after.context["documents"][0].shown_findings}
    assert shown[finding.pk] == "accepted"

    def themes() -> dict:
        return {t.topic: t.n_findings for t in get_part(client, THEMES, "synthesis").context["shown_themes"]}

    first = world["topics"][0]
    assert themes()[first] == 11  # the rejected finding of document 11 is not counted
    client.post(
        reverse(
            "knowledge:review_finding_verdict",
            args=[DocumentFinding.objects.get(document=world["documents"][11], position=1).pk],
        ),
        {"verdict": "unreviewed"},
    )
    assert themes()[first] == 12


# --------------------------------------------------------------------------------------- one page, two drawings
def _matrix(world) -> list[str]:
    annual, document = world["annual"].pk, world["documents"][4].pk
    return [
        BY_DOCUMENT,
        BY_DOCUMENT + "&page=2",
        BY_DOCUMENT + f"&batch={annual}",
        BY_DOCUMENT + f"&document={document}",
        BY_DOCUMENT + "&category=challenge&verdict=unreviewed",
        BY_DOCUMENT + f"&counted=1&batch={annual}",
        BY_DOCUMENT + "&q=zzzyx",  # no result
        THEMES,
        THEMES + "&min=3",
        THEMES + "&challenges=1",
        THEMES + "&q=" + world["topics"][0].name.split()[0].lower(),
        THEMES + f"&batch={world['donors'].pk}",
        THEMES + "&q=zzzyx",  # no result
        REPEATED,
        REPEATED + f"&batch={annual}",
    ]


@pytest.mark.parametrize("role", ["viewer", "staff", "verified"])
def test_the_assembled_page_is_the_eager_page(viewer, admin_user, world, role):
    client = _signed_in(admin_user if role == "staff" else viewer, verified=role == "verified")
    for url in _matrix(world):
        compute.reset_memo()
        caches["shared"].clear()  # the repeated findings asked for, not drawn inline
        assembled = assemble(client, url)
        assert set(assembled.parts) == {"findings" if "view=document" in url else "synthesis"}, url
        assert all(response.status_code == 200 for response in assembled.parts.values()), url
        drawn = eager(client, url)
        assert drawn.status_code == 200 and placeholders(text(drawn)) == {}, url
        # under every filter: no link of a part (nor of the eager page) carries _part, nor any form's way back
        for name, response in assembled.parts.items():
            body = text(response)
            links = LINK.findall(body) + NEXT.findall(body)
            assert all("_part" not in link for link in links), (url, name, links)
            assert executable(body) == [], (url, name)
        assert all("_part" not in link for link in LINK.findall(text(drawn)) + NEXT.findall(text(drawn)))
        assert "_part=" not in "".join(LINK.findall(assembled.html))
        assert normalise(assembled.html) == normalise(text(drawn)), url
        # no JSON in these views; the page's JSON ids stay unique and the same either way
        assert _json_blocks(assembled.html) == _json_blocks(text(drawn))


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    """*Show the figures* (no JavaScript) leads to the page drawn at once: every block, no grey shapes left and no
    link to the page already shown."""
    for url, figure in (
        (BY_DOCUMENT, "Report 11"),
        (THEMES, "themes raised by"),
        (REPEATED, "Raised in 6 documents"),
    ):
        frame = text(client_viewer.get(url))
        shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
        assert len(shown) == 1, url
        page = text(client_viewer.get(PAGE + html_module.unescape(shown[0])))
        assert placeholders(page) == {} and figure in page, url
        assert "part__skeleton" not in page and "Show the figures" not in page
        assert "_eager=1" not in "".join(LINK.findall(page))


# ------------------------------------------------------------------------------------------- the quick wins
def _rows_by_join(batch=None, analysed=False) -> list[tuple]:
    """``document_rows``' counts as they were counted before (one join, distinct counts): the reference."""
    qs = Document.objects.filter(review_batch__isnull=False)
    if batch:
        qs = qs.filter(review_batch_id=batch)
    if analysed:
        qs = qs.filter(review_status__in=review_data.ANALYSED)
    return list(
        qs.annotate(
            n_findings=Count("findings", distinct=True),
            n_statements=Count("statements", distinct=True),
            n_actions=Count("review_action_points", distinct=True),
            n_reviewed=Count("findings", filter=~Q(findings__verdict=Verdict.UNREVIEWED), distinct=True),
        )
        .order_by("review_batch__archived", "review_batch__name", "title", "pk")
        .values_list("pk", "n_findings", "n_statements", "n_actions", "n_reviewed")
    )


def test_the_desk_review_reads_the_stored_repeated_findings_and_every_theme_s_evidence_at_once(
    client_viewer, world, monkeypatch
):
    """The Word desk review (a download of seconds on a large review) takes its repeated findings from the groups the
    Repeated findings view stores, and the evidence of its themes and challenges in one query each, not one per
    theme (review R1-D2-3). The file says what it said: the same groups, citations and examples."""
    from neurodb.knowledge import review_docx
    from neurodb.knowledge import text as doc_text

    found = review_data.synthesis(None, False, 2)
    challenges = review_data.synthesis(None, False, 2, challenges=True)
    expected = [  # as the file wrote them, one theme and one comparison at a time
        f"{f.text} {review_data.cite(f)}"
        for theme in found.themes[: review_docx.THEMES]
        for f in review_data.theme_findings(theme.topic.pk, None, False, limit=review_docx.EVIDENCE_PER_THEME)
    ]
    for theme in challenges.themes[: review_docx.CHALLENGES]:
        top = review_data.theme_findings(theme.topic.pk, None, False, challenges=True, limit=1)
        expected.append(f"For example: {top[0].text} {review_data.cite(top[0])}")
    for group in review_data.repeated(None, False, limit=review_docx.REPEATED):
        places = " ".join(dict.fromkeys(review_data.cite(f) for f in group.findings))
        expected.append(f"{group.lead.text} Raised in {group.documents} documents: {places}")
    assert len(expected) > 6 and any("Raised in 6 documents" in line for line in expected)
    get_part(client_viewer, REPEATED, "synthesis")  # the groups stored now
    calls: Counter = Counter()
    real = review_data.repeated

    def compared(*args, **kwargs):
        calls["repeated"] += 1
        return real(*args, **kwargs)

    def one_theme(*args, **kwargs):
        raise AssertionError("a theme's findings read alone")

    monkeypatch.setattr(review_data, "repeated", compared)
    monkeypatch.setattr(review_data, "theme_findings", one_theme)
    with CaptureQueriesContext(connection) as queries:
        response = client_viewer.get(reverse("knowledge:review_docx"), {"min": "2"})
    assert response.status_code == 200 and calls["repeated"] == 0
    words = doc_text.docx_text(response.content)
    at = [words.find(line) for line in expected]
    assert -1 not in at, [line for line, i in zip(expected, at, strict=True) if i == -1]
    assert at == sorted(at)  # in the file's order
    themed = [q["sql"] for q in queries.captured_queries if '"topic_id" IN' in q["sql"]]
    assert len(themed) == 2  # the themes' evidence, the challenges' examples


def test_document_rows_count_by_subqueries_as_the_join_did(world):
    documents = world["documents"]
    empty = Document.objects.create(title="Waiting", text="x", review_batch=world["annual"])  # no finding
    DocumentStatement.objects.create(
        document=documents[3], position=2, text="A second statement.", urgency=10
    )
    DocumentFinding.objects.filter(document=documents[5]).update(verdict=Verdict.ACCEPTED)
    for batch, analysed in (
        (None, False),
        (world["annual"].pk, False),
        (None, True),
        (world["donors"].pk, True),
    ):
        rows = review_data.document_rows(batch, analysed=analysed)
        found = [(d.pk, d.n_findings, d.n_statements, d.n_actions, d.n_reviewed) for d in rows]
        assert found == _rows_by_join(batch, analysed), (batch, analysed)
    by_pk = {d.pk: d for d in review_data.document_rows()}
    assert (by_pk[empty.pk].n_findings, by_pk[empty.pk].n_statements) == (0, 0)
    assert by_pk[documents[3].pk].n_statements == 2 and by_pk[documents[5].pk].n_reviewed == 3
    assert by_pk[documents[0].pk].n_reviewed == 1


def test_document_rows_count_in_one_query_without_the_join(world):
    with CaptureQueriesContext(connection) as queries:
        review_data.document_rows()
    (query,) = [q["sql"] for q in queries.captured_queries]
    assert "COUNT(DISTINCT" not in query.upper()


def _theme_findings_one_by_one(topic, batch, verified, challenges=False, limit=None) -> list[int]:
    """The theme's findings as they were read before: one query per theme (the reference)."""
    qs = review_data.findings(batch, verified).filter(topic_id=topic)
    if challenges:
        qs = qs.filter(category="challenge")
    rows = list(qs.order_by("-evidence", "pk"))
    seen: Counter = Counter()
    for finding in rows:
        finding.rank = seen[finding.document_id]
        seen[finding.document_id] += 1
    rows.sort(key=lambda f: (f.rank, -f.evidence, f.pk))
    return [f.pk for f in (rows[:limit] if limit else rows)]


def test_the_themes_findings_in_one_query_are_those_read_one_theme_at_a_time(world):
    topics = [t.pk for t in world["topics"]]
    for batch, verified, challenges, limit in (
        (None, False, False, 5),
        (None, False, True, 5),
        (world["annual"].pk, False, False, None),
        (None, True, False, 5),
    ):
        with CaptureQueriesContext(connection) as queries:
            together = review_data.themes_findings(topics, batch, verified, challenges, limit)
        assert len(queries) == 1
        for topic in topics:
            expected = _theme_findings_one_by_one(topic, batch, verified, challenges, limit)
            assert [f.pk for f in together[topic]] == expected, (topic, batch, verified, challenges)
            assert [
                f.pk for f in review_data.theme_findings(topic, batch, verified, challenges, limit)
            ] == expected


def test_the_themes_part_reads_the_shown_themes_findings_in_one_query(client_viewer, world):
    with CaptureQueriesContext(connection) as queries:
        get_part(client_viewer, THEMES, "synthesis")
    reads = [
        q["sql"]
        for q in queries.captured_queries
        if 'FROM "knowledge_documentfinding"' in q["sql"] and '"topic_id" IN' in q["sql"]
    ]
    assert len(reads) == 1, reads
