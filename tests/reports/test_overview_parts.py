"""The country overview in parts (SPEC7 §3.3-3.4 for the overview; STAGE0 D1, D2, D9).

The overview answers its frame at once (purpose, filter bar, subtitle, freshness strip) with a placeholder per
data block: ``kpis`` and ``figures`` (one overview build), ``for-you`` (the NeuroDB Watch card, from its own
address), ``review`` (the daily review card; its folded findings are ``review-more``, inside it) and ``news``
(What's new and the forecast risk card). These tests check the frame without any figure work, each part, the
part queries, and that the page put together from its parts is the page drawn at once (``_eager=1``).

On ``overview_fixture``: one programme document (Amel, Child Protection) with 500 children reached in 2026, plus
the ActivityInfo hierarchy; a daily review is run on it where a test needs the card.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from neurodb.accounts.models import User
from neurodb.donors.models import DonorAccount
from neurodb.graph import news
from neurodb.indicators.models import ReportingYear
from neurodb.insights import services as insight_services
from neurodb.reports import overview as overview_service
from neurodb.reports import views
from neurodb.review import services as review_services
from neurodb.watch import page as watch_page
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.reports.test_headline import _second
from tests.web.parts import (
    HX,
    assemble,
    eager,
    get_part,
    normalise,
    placeholder_attributes,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

PAGE = "/"  # reports:overview, the site root once signed in
PARTS = ("kpis", "for-you", "figures", "review", "news")  # the frame's placeholders, in page order
OWN_PARTS = ("kpis", "figures", "review", "review-more", "news")  # the parts the view itself answers
FIGURES_JSON = "overview-figures-data"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm (its filter
# choices, freshness strip and navigation stored, as after the first visit of a page) is 10: the request's floor
# (session, user, donor check, user role) plus the year, the cache stamp (last sync run, flags), the own section
# and whether a daily review exists. Cold it also reads the filter choices (2), the freshness strip (4) and the
# navigation (4). Each part is measured with nothing stored: kpis and figures compute the whole build.
SHELL_QUERIES_WARM = 12
SHELL_QUERIES_COLD = 22
PART_QUERIES = {"kpis": 39, "figures": 39, "review": 9, "review-more": 9, "news": 8}


@pytest.fixture
def world(hierarchy, reporting_year, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


@pytest.fixture
def reviewed(world, monkeypatch):
    """The world with a daily review: the card's place is kept in the frame. The card shows one finding and
    folds the others (``review-more``), as the real card does past its first findings."""
    review_services.run(date=TODAY, today=TODAY)
    monkeypatch.setattr(review_services, "PAGE_FINDINGS", 1)
    return world


@pytest.fixture
def own_section(viewer, section):
    """The viewer works in Child Protection: a first load shows that section."""
    viewer.section = section
    viewer.save()
    return viewer


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the overview raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the shell computed figures")

    monkeypatch.setattr(overview_service, "build", refuse)
    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    monkeypatch.setattr(views, "_overview_chart_data", refuse)
    monkeypatch.setattr(views, "_whats_new", refuse)
    monkeypatch.setattr(views, "_forecast_risk", refuse)
    monkeypatch.setattr(review_services, "for_page", refuse)
    monkeypatch.setattr(news, "recent", refuse)
    monkeypatch.setattr(insight_services, "at_risk", refuse)
    monkeypatch.setattr(watch_page, "card", refuse)


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a chart JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _without_for_you(assembled) -> str:
    """The assembled page without the NeuroDB Watch card, which eager mode leaves to the browser (a ``url=``
    part): the comparison with the eager page is about the overview's own parts."""
    card = assembled.parts.get("for-you")
    return assembled.html.replace(text(card), "", 1) if card is not None else assembled.html


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, reviewed, monkeypatch):
    from django.core.cache import caches

    caches["shared"].clear()  # nothing stored: nothing may be drawn inline either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Country overview" in html and 'class="page-purpose"' in html
    assert 'aria-label="Overview filters"' in html and 'name="governorate"' in html  # the filter bar
    assert "Reporting year 2026 · every section · all governorates" in html  # the subtitle
    assert "Data freshness" in html and "Daily AI review" in html  # the freshness strip
    assert "ActivityInfo databases of the year" in html
    assert 'id="daily-review"' in html  # the review card's anchor stays in the frame
    assert list(placeholders(html)) == list(PARTS)
    for data_id in (FIGURES_JSON, "overview-chart-data"):  # no figures, so no chart data
        assert f'id="{data_id}"' not in html
    for figure in ("Children reached", 'id="gov-title"', 'id="review-title"', 'id="whats-new-title"'):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_filter(client_viewer, reviewed):
    html = text(client_viewer.get(PAGE))
    found = placeholder_attributes(html)
    assert list(found) == list(PARTS)
    for name in ("kpis", "figures", "review", "news"):
        url = found[name]["hx-get"]
        assert urlsplit(url).path == PAGE
        # every section written out (never a first load again), the year always
        assert _query(url) == {"section": [""], "year": ["2026"], "_part": [name]}, url
    assert found["for-you"]["hx-get"] == reverse("watch:card") and found["for-you"]["hx-swap"] == "outerHTML"
    assert (found["kpis"]["data-part-priority"], found["kpis"]["data-part-group"]) == ("high", "overview")
    assert (found["figures"]["data-part-priority"], found["figures"]["data-part-group"]) == (
        "normal",
        "overview",
    )
    assert found["for-you"]["data-part-priority"] == "high" and found["news"]["data-part-priority"] == "low"
    assert "data-part-group" not in found["review"] and "data-part-lazy" not in found["review"]
    assert "gov-title" in found["figures"]["data-part-anchors"].split()  # its blocks' ids stay anchors
    # the room of the four blocks, on a desktop and on a phone (where they stack)
    assert found["figures"]["class"] == "part part--charts"
    assert found["figures"]["style"] == "--part-h: 4300px; --part-h-sm: 7600px"
    # the governorate and the review tab travel too
    html = text(
        client_viewer.get(PAGE, {"section": "Child Protection", "governorate": "Akkar", "review": "week"})
    )
    assert _query(placeholders(html)["review"]) == {
        "section": ["Child Protection"],
        "year": ["2026"],
        "governorate": ["Akkar"],
        "review": ["week"],
        "_part": ["review"],
    }


@pytest.mark.shell
def test_a_first_load_writes_the_own_section_and_every_section_writes_section(
    client_viewer, own_section, world, monkeypatch
):
    seen = []
    real_build = overview_service.build

    def build(scope, **kwargs):
        seen.append(list(scope.sections))
        return real_build(scope, **kwargs)

    monkeypatch.setattr(overview_service, "build", build)
    first = text(client_viewer.get(PAGE))
    assert "Your section, Child Protection, by default" in first
    for name in ("kpis", "figures", "news"):
        assert _query(placeholders(first)[name])["section"] == ["Child Protection"]
    # the part request is never a first load: it computes what the frame wrote, the own section
    get_part(client_viewer, PAGE, "kpis")
    assert seen == [["Child Protection"]]
    every = text(client_viewer.get(PAGE + "?section="))
    assert "by default" not in every
    for name in ("kpis", "figures", "news"):
        assert _query(placeholders(every)[name])["section"] == [""]
    get_part(client_viewer, PAGE + "?section=", "kpis")  # "every section", not the own section again
    assert seen[-1] == []


@pytest.mark.shell
def test_without_a_reporting_year_there_is_no_part(client_viewer, world, reporting_year):
    reporting_year.delete()
    html = text(client_viewer.get(PAGE))
    assert "No reporting year is configured" in html and placeholders(html) == {}
    response = client_viewer.get(PAGE + "?_part=kpis", **HX)
    assert response.status_code == 404  # no part to ask for


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, own_section, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "kpis" in placeholders(text(response))
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and "kpis" in placeholders(text(response))


def test_the_frame_reads_no_watch_table(client_viewer, reviewed):
    with CaptureQueriesContext(connection) as queries:
        response = raw_get(client_viewer, PAGE)
    assert response.status_code == 200
    assert not [q["sql"] for q in queries.captured_queries if '"watch_' in q["sql"]]


# ------------------------------------------------------------------------------------------------- the parts
@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_answers_its_wrapper(client_viewer, reviewed, name):
    response = get_part(client_viewer, PAGE, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    expected = {
        "kpis": "Children reached",
        "figures": "Impact on children",
        "review": 'id="review-title"',
        "review-more": "review-finding",
        "news": 'id="whats-new-title"',
    }[name]
    assert expected in html
    assert (f'id="{FIGURES_JSON}"' in html) == (name == "figures")  # the one chart JSON, in its own part


@pytest.mark.parametrize("name", OWN_PARTS)
def test_each_part_stays_within_its_query_ceiling(
    client_viewer, reviewed, name, django_assert_max_num_queries
):
    from django.core.cache import caches

    address = f"{PAGE}?section=&year=2026&_part={name}"
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(address, **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


@pytest.mark.parametrize("name", OWN_PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.get(f"{PAGE}?section=&year=2026&_part={name}", **HX)
    assert response.status_code == 403


def test_a_visitor_asking_for_a_part_of_the_site_root_is_sent_to_sign_in(client, world):
    response = client.get(f"{PAGE}?section=&year=2026&_part=kpis", **HX)
    assert response.status_code == 200 and "in one place." not in text(response)  # never the landing page
    assert response["HX-Redirect"] == reverse("account_login") + "?next=/"


def test_no_link_inside_a_part_carries_part(client_viewer, own_section, reviewed):
    assembled = assemble(client_viewer, PAGE)
    assert set(OWN_PARTS) <= set(assembled.parts)
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)  # (a placeholder's noscript: _eager)
    assert "_part=" not in "".join(LINK.findall(assembled.html))


def test_the_review_more_part_holds_only_the_folded_findings(client_viewer, reviewed):
    page = review_services.for_page("today", [])
    shown = escape(page["findings"][0].title)
    folded = [escape(f.title) for f in page["more_findings"]]
    assert len(page["findings"]) == 1 and folded and shown not in folded
    item = '<li class="review-finding">'
    card = text(get_part(client_viewer, PAGE, "review"))
    # the card lists its first finding only (its summary may name the others) ...
    assert card.count(item) == 1 and shown in card.split(item, 1)[1].split("</li>", 1)[0]
    assert f"Show {len(folded)} more finding" in card
    inner = placeholder_attributes(card)
    assert list(inner) == ["review-more"] and inner["review-more"]["data-part-lazy"] == "strict"
    assert _query(inner["review-more"]["hx-get"])["_part"] == ["review-more"]
    more = text(get_part(client_viewer, PAGE, "review-more"))
    # ... and the folded ones come in their own part, alone
    assert more.count(item) == len(folded) and all(title in more for title in folded)
    assert shown not in more and 'id="review-title"' not in more and "review-summary" not in more


def test_a_warm_overview_draws_kpis_and_figures_in_the_frame(client_viewer, reviewed, monkeypatch):
    get_part(client_viewer, PAGE, "kpis")  # the group leader computes and stores the build

    def refuse(self):
        raise AssertionError("the stored build was computed again")

    monkeypatch.setattr(overview_service._Builder, "build", refuse)
    html = text(raw_get(client_viewer, PAGE))
    assert list(placeholders(html)) == ["for-you", "review", "news"]  # kpis and figures are already in
    for name in ("kpis", "figures"):
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in html
    assert "Children reached" in html and "Impact on children" in html and f'id="{FIGURES_JSON}"' in html
    # another filter is not stored: its figures are asked for
    other = text(raw_get(client_viewer, PAGE + "?section=Child+Protection"))
    assert list(placeholders(other)) == list(PARTS)


def test_an_edit_shows_at_once_in_the_part(client, admin_user, reviewed):
    """Fresh after edit (STAGE0 D9): the main indicator chosen on the programme document page changes the
    children reached in the key figures, though the build of that filter was stored a moment before."""
    _second(reviewed)  # a second children indicator of the PD: 700, the larger, stands for the PD
    client.force_login(admin_user)
    before = text(get_part(client, PAGE, "kpis"))
    assert ">700</a>" in before
    response = client.post(reverse("reports:pd_main_indicator", args=[reviewed["pd"].pk]), {"key": "100"})
    assert response.status_code == 302
    after = text(get_part(client, PAGE, "kpis"))
    assert ">500</a>" in after and ">700</a>" not in after  # the unique beneficiaries indicator now


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "",  # first load: the own section
    "?section=",  # every section
    "?section=Child+Protection",
    "?section=&governorate=Akkar",
    "?section=Education",  # a filter with no result
    "?year=2025",  # the previous year
    "?section=&review=week",
    "?section=&review=yesterday",
]


@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(client_viewer, own_section, reviewed, query):
    ReportingYear.objects.get_or_create(name="2025", defaults={"year": "2025", "current": False})
    assembled = assemble(client_viewer, PAGE + query)
    assert {"kpis", "figures", "review", "news"} <= set(assembled.parts)  # review-more: when findings fold
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client_viewer, PAGE + query)
    assert drawn.status_code == 200
    assert placeholders(text(drawn)) == {"for-you": reverse("watch:card")}  # only the Watch card is left
    assert normalise(_without_for_you(assembled)) == normalise(text(drawn), exclude=("part-for-you",))
    # one chart JSON per part, unique in the page, the same as the eager page's
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone and FIGURES_JSON in together
    figures = _json_blocks(text(assembled.parts["figures"]))
    assert list(figures) == [FIGURES_JSON] and figures[FIGURES_JSON] == alone[FIGURES_JSON]
    assert (
        figures[FIGURES_JSON]["labels"]["not_reported"] == "Not reported"
    )  # the labels travel with the charts


# ------------------------------------------------------------------------------------- templates left to load
LOAD_TRIGGERS_ALLOWED = {
    "web/templates/base.html",  # the sidebar's For you count
    "fmm/templates/fmm/tabs/insights.html",  # the Monitoring insights AI card, which polls
}


def test_every_load_trigger_left_is_on_the_allow_list():
    """A block that loads after the page is a part now (SPEC7 §3.2); only these keep ``hx-trigger="load"``."""
    from pathlib import Path

    root = Path(views.__file__).resolve().parents[1]
    found = {
        str(path.relative_to(root))
        for path in root.glob("*/templates/**/*.html")
        if re.search(r'hx-trigger="load\b', path.read_text(encoding="utf-8"))
    }
    assert found <= LOAD_TRIGGERS_ALLOWED, found - LOAD_TRIGGERS_ALLOWED
    overview = (root / "reports/templates/reports/overview.html").read_text(encoding="utf-8")
    assert "watch-overview-slot" not in overview and '{% part "for-you"' in overview
