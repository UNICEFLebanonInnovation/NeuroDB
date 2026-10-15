"""Home in parts (SPEC_HOME §2, §3.1): the frame, each part, the old addresses of the site root, and the page put
together from its parts against the page drawn at once.

Home answers its frame at once (title, date and scope line, purpose, freshness strip; no figure work and no
``watch_`` table) with a placeholder per block: ``for-you`` (the NeuroDB Watch card, from its own address),
``tasks`` (decisions and the daily review's digest), ``since`` (what changed since the previous working day, and
"Your tasks"), ``standing`` (the Country overview's own key figures) and ``programmes``.

On ``overview_fixture``: one programme document (Amel, Child Protection) with 500 children reached in 2026, plus the
ActivityInfo hierarchy (2026 records of January and February: archived on the suite's day, 15 October 2026); a daily
review is run on it where a test needs one.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.donors.models import DonorAccount
from neurodb.graph import news
from neurodb.reports import brief as brief_service
from neurodb.reports import home as home_service
from neurodb.reports import overview as overview_service
from neurodb.reports import views
from neurodb.review import services as review_services
from neurodb.watch import page as watch_page
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.web.parts import (
    HX,
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

PAGE = "/"  # reports:overview: Home once signed in
PARTS = ("for-you", "tasks", "since", "standing", "programmes")  # the frame's placeholders, in page order
OWN_PARTS = ("tasks", "since", "standing", "programmes")  # the parts Home itself answers
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (the codebase rule; SPEC_HOME §3.1). The frame warm
# (filter sections, freshness strip, ActivityInfo state and navigation stored) is 11: the request's floor (session,
# user, donor check), the cache stamp (last sync run, flags), the person's role and Management membership, their
# section, the reporting year whose ActivityInfo records count for this year (the stored build's key) and the
# navigation's year and role check. Cold (23) it also reads the ActivityInfo state (4: SPEC_HOME §1.3), the filter
# sections (1), the freshness strip (3) and the navigation (4). Each part is measured with nothing stored: tasks
# 10, since 8, standing 42 (the whole overview build), programmes 16 (no country programme cycle in this world).
SHELL_QUERIES_WARM = 13
SHELL_QUERIES_COLD = 25
PART_QUERIES = {"tasks": 12, "since": 10, "standing": 44, "programmes": 18}
WATCH_CARD_QUERIES = 11  # the for-you part, at watch:card: measured 9 (SPEC_HOME §3.1 allows 12)


@pytest.fixture
def world(hierarchy, reporting_year, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


@pytest.fixture
def reviewed(world):
    """The world with a daily review."""
    review_services.run(date=TODAY, today=TODAY)
    return world


@pytest.fixture
def own_section(viewer, section):
    """The viewer works in Child Protection: a first load shows that section."""
    viewer.section = section
    viewer.save()
    return viewer


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _figure_services_refuse(monkeypatch):
    """Every service that computes figures raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    monkeypatch.setattr(overview_service, "build", refuse)
    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    monkeypatch.setattr(brief_service, "decisions", refuse)
    monkeypatch.setattr(review_services, "digest", refuse)
    monkeypatch.setattr(review_services, "for_page", refuse)
    monkeypatch.setattr(home_service, "since", refuse)
    monkeypatch.setattr(home_service, "your_tasks", refuse)
    monkeypatch.setattr(home_service, "programmes", refuse)
    monkeypatch.setattr(news, "recent", refuse)
    monkeypatch.setattr(watch_page, "card", refuse)


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, reviewed, monkeypatch):
    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _figure_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Home" in html and '<h1 class="page-title">Home</h1>' in html
    assert "Thursday 15 October 2026 · Every section" in html  # the date and the scope line
    assert 'class="page-purpose"' in html and "where the country stands in 2026" in html
    assert 'aria-labelledby="home-today-title"' in html and "Needs you today" in html
    assert "Data freshness" in html  # the freshness strip, in the frame
    assert list(placeholders(html)) == list(PARTS)
    for figure in ("Children reached", "Needs a decision", "Daily review ·", "Since "):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filter(client_viewer, reviewed):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    stamp = overview_service.fingerprint()
    for name in OWN_PARTS:
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # every section written out (never a first load again) and the figures' fingerprint
        assert _query(url) == {"section": [""], "as_of": [stamp], "_part": [name]}, url
    assert found["for-you"]["hx-get"] == reverse("watch:card")
    assert {name: found[name]["data-part-priority"] for name in PARTS} == {
        "for-you": "high",
        "tasks": "high",
        "since": "normal",
        "standing": "high",
        "programmes": "low",
    }
    assert found["standing"]["class"] == "part part--tiles"
    standing = html[html.index('id="part-standing"') : html.index('id="part-programmes"')]
    assert standing.count("skeleton__block--tile") == 5  # the five key figure tiles


@pytest.mark.shell
def test_a_first_load_writes_the_own_section(client_viewer, own_section, world):
    first = text(client_viewer.get(PAGE))
    assert "Your section, Child Protection, by default" in first and 'href="?section="' in first
    for name in OWN_PARTS:
        assert _query(placeholders(first)[name])["section"] == ["Child Protection"]
    every = text(client_viewer.get(PAGE + "?section="))
    assert "by default" not in every
    assert "Every section · " in every and "only Child Protection" in every  # back to the own section
    for name in OWN_PARTS:
        assert _query(placeholders(every)[name])["section"] == [""]


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, own_section, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "standing" in placeholders(text(response))
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "standing" in placeholders(text(response))


def test_the_frame_reads_no_watch_table(client_viewer, reviewed):
    with CaptureQueriesContext(connection) as queries:
        response = raw_get(client_viewer, PAGE)
    assert response.status_code == 200
    assert not [q["sql"] for q in queries.captured_queries if '"watch_' in q["sql"]]


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_answers_its_wrapper(client, admin_user, reviewed, name):
    client.force_login(admin_user)
    response = get_part(client, PAGE, name)
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "tasks": "Needs a decision",
        "since": 'id="home-since-title"',
        "standing": "Children reached",
        "programmes": "Programmes and sources",
    }[name]
    assert expected in html
    assert executable(html) == []  # nothing that would run once swapped in


@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_stays_within_its_query_ceiling(
    client, admin_user, reviewed, name, django_assert_max_num_queries
):
    client.force_login(admin_user)
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client.get(f"{PAGE}?section=&_part={name}", **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


def test_the_watch_card_stays_within_its_query_ceiling(
    client_viewer, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(WATCH_CARD_QUERIES):
        response = client_viewer.get(reverse("watch:card"), **HX)
    assert response.status_code == 200


@pytest.mark.parametrize("name", OWN_PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    assert client.get(f"{PAGE}?section=&_part={name}", **HX).status_code == 403
    page = client.get(PAGE)
    assert page.status_code == 302 and page["Location"] == reverse("donors:page")


def test_a_visitor_gets_the_landing_page_and_is_sent_to_sign_in_for_a_part(client, world):
    landing = client.get(PAGE)
    assert landing.status_code == 200 and 'class="page-purpose"' not in text(landing)
    response = client.get(f"{PAGE}?section=&_part=tasks", **HX)
    assert response["HX-Redirect"] == reverse("account_login") + "?next=/"


def test_a_warm_home_draws_the_key_figures_in_the_frame(client_viewer, reviewed, monkeypatch):
    get_part(client_viewer, PAGE, "standing")  # computes and stores the build

    def refuse(self):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    html = text(raw_get(client_viewer, PAGE))
    assert "standing" not in placeholders(html)
    assert '<div id="part-standing" class="part is-loaded" data-part-loaded="standing"' in html
    assert "Children reached" in html


def test_home_and_the_country_overview_read_one_build(client_viewer, reviewed, monkeypatch):
    """``standing`` is the Country overview's own build (same scope, same key): the overview's frame draws the
    key figures Home stored, without computing them again, and both show the same figure."""
    keys = []
    real = overview_service._cache_key

    def key(scope, stamp=""):
        keys.append(real(scope, stamp))
        return keys[-1]

    monkeypatch.setattr(overview_service, "_cache_key", key)
    home = get_part(client_viewer, PAGE + "?section=", "standing")
    home_key = keys[-1]
    builds = []
    monkeypatch.setattr(
        overview_service._Builder, "build", lambda self: builds.append(1) or {"never": "computed"}
    )
    overview = raw_get(client_viewer, reverse("reports:country_overview") + "?section=")
    assert builds == [] and home_key in keys[1:]
    assert 'data-part-loaded="kpis"' in text(overview)
    assert (
        overview.context["data"]["impact"]["children_reached"]
        == home.context["data"]["impact"]["children_reached"]
        == 500
    )


# ------------------------------------------------------------------------------------- the site root's old addresses
@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("?year=2026", "reports:country_overview"),
        ("?governorate=Akkar&section=Child+Protection", "reports:country_overview"),
        ("?review=week&section=", "reports:daily_review"),
        ("?review_latest=2026-07-01&review_id=3", "reports:daily_review"),
    ],
)
def test_old_links_of_the_site_root_go_to_their_page_with_their_query(client_viewer, world, query, target):
    response = client_viewer.get(PAGE + query)
    assert response.status_code == 302
    assert response["Location"] == reverse(target) + query


@pytest.mark.parametrize(("name", "page"), [("kpis", "country"), ("figures", "country"), ("news", "country")])
def test_an_open_tab_asking_the_site_root_for_an_overview_part_gets_it(client_viewer, world, name, page):
    """A tab opened on the site root when it was the country overview asks for its parts there: they still come,
    from the Country overview's view."""
    response = client_viewer.get(f"{PAGE}?section=&year=2026&_part={name}", **HX)
    assert response.status_code == 200 and response["X-NeuroDB-Part"] == name
    assert f'id="part-{name}"' in text(response)


@pytest.mark.parametrize("name", ["review", "review-more"])
def test_an_open_tab_asking_the_site_root_for_a_review_part_gets_it(client_viewer, reviewed, name):
    response = client_viewer.get(f"{PAGE}?section=&_part={name}", **HX)
    assert response.status_code == 200 and response["X-NeuroDB-Part"] == name


def test_the_addresses():
    assert reverse("reports:overview") == "/"
    assert reverse("reports:country_overview") == "/country-overview/"
    assert reverse("reports:daily_review") == "/daily-review/"
    assert views.overview is views.country_overview  # the old name still answers


@pytest.mark.parametrize("url", ["/country-overview/", "/daily-review/"])
def test_visitors_are_asked_to_sign_in(client, url):
    response = client.get(url)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_no_link_inside_a_part_carries_part(client, admin_user, reviewed):
    client.force_login(admin_user)
    assembled = assemble(client, PAGE)
    assert set(OWN_PARTS) <= set(assembled.parts)
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
    assert "_part=" not in "".join(LINK.findall(assembled.html))


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = ["", "?section=", "?section=Child+Protection", "?section=Education"]


@pytest.mark.parametrize("query", MATRIX)
@pytest.mark.parametrize("who", ["viewer", "admin"])
def test_the_assembled_page_is_the_eager_page(client, viewer, admin_user, own_section, reviewed, query, who):
    client.force_login(viewer if who == "viewer" else admin_user)
    assembled = assemble(client, PAGE + query)
    assert set(OWN_PARTS) <= set(assembled.parts)
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, PAGE + query)
    assert drawn.status_code == 200
    for name, response in assembled.parts.items():
        assert executable(text(response)) == [], name
    assert placeholders(text(drawn)) == {"for-you": reverse("watch:card")}  # only the Watch card is left
    card = assembled.parts.get("for-you")
    together = assembled.html.replace(text(card), "", 1) if card is not None else assembled.html
    assert normalise(together) == normalise(text(drawn), exclude=("part-for-you",))


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_offers_for_you_its_own_page(client_viewer, reviewed):
    """*Show the figures* (no JavaScript) leads to the page drawn at once. The For you card comes from another
    view, so that page cannot draw it: a quiet slot (no grey shapes forever, not busy), whose link is the card's
    own page, never the page already shown."""
    frame = text(client_viewer.get(PAGE))
    shown = re.search(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert shown, "the frame offers the figures without JavaScript"
    address = PAGE + shown.group(1).replace("&amp;", "&")
    assert "_eager=1" in address
    page = text(client_viewer.get(address))
    slot = placeholder_attributes(page)
    assert list(slot) == ["for-you"]  # every other part is drawn
    assert "aria-busy" not in slot["for-you"] and slot["for-you"]["hx-get"] == reverse("watch:card")
    start = page.index('id="part-for-you"')
    card = page[start : page.index('id="part-tasks"', start)]
    assert "part__skeleton" not in card and "Show the figures" not in card
    assert f'<noscript><a href="{reverse("watch:for_you")}">' in card
    assert "_eager=1" not in "".join(LINK.findall(card))


@pytest.mark.shell
def test_a_failing_part_keeps_its_error_box_with_try_again(client_viewer, viewer, reviewed, monkeypatch):
    """A part that fails answers 500; its placeholder already holds the error box parts.js shows, with Try again
    (SPEC_HOME §11.6): the first screen never stays grey."""
    from django.test import Client

    frame = text(client_viewer.get(PAGE))
    tasks = frame[frame.index('id="part-tasks"') : frame.index('id="part-since"')]
    assert "data-part-error" in tasks and "Try again" in tasks

    def broken(*args, **kwargs):
        raise RuntimeError("the decisions could not be read")

    monkeypatch.setattr(home_service, "decisions", broken)
    monkeypatch.setattr(review_services, "digest", broken)
    failing = Client(raise_request_exception=False)
    failing.force_login(viewer)
    response = failing.get(f"{PAGE}?section=&_part=tasks", **HX)
    assert response.status_code == 500 and response["X-NeuroDB-Part"] == "tasks"
