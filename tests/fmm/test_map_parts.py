"""The Monitoring insights Map tab in parts (SPEC7 §3.3-3.4; STAGE3 group C).

The Map tab is the one tab of ``/fmm/`` over 300 ms cold on large data (step 3's triage). Its page answers the
frame at once (the header and Export menu, the filter bar, the reference line, the chips, the data notes, the key
figures and the tab bar) with one placeholder, ``map``: the map, its legend and its three tables, with the map's
configuration (``fmm-map-config``), read from ``geo.map_points``, which every worker shares. An HTMX swap of the
results (the tab bar, the filter bar, the map's own links) still answers the results with the map drawn in them.
These tests check the frame without any figure work, the part, its address, that the page put together from its
part is the page drawn at once (``_eager=1``), that the stored map never shows one person's view to another nor
one language's words in another, and that an edit shows at once.

On ``fm_world`` built on 5 October 2026 (``built``), with the amended Bekaa PD planning Zahle town, the Baalbek
district and Douris (``planned`` of tests/fmm/test_map.py): Douris is the one planned place no visit reached.
"""

from __future__ import annotations

import datetime
import html as html_module
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.cache import cache, caches
from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.dateformat import format as date_format

from neurodb.accounts.models import User
from neurodb.core.models import SyncRun
from neurodb.donors.models import DonorAccount
from neurodb.fmm import geo, metrics, views
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit
from neurodb.geo.models import Location
from neurodb.partnerships.models import PCA
from tests.fmm.test_map import planned  # noqa: F401 - the amended Bekaa PD's planned places (a fixture)
from tests.fmm.test_pages import _section_editor
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

TODAY = datetime.date(2026, 10, 5)
PAGE = "/fmm/"  # fmm:dashboard
MAP_JSON = "fmm-map-config"  # the map's configuration (edumap.js), in the part
STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# The map part's height as it arrived on step 3's world (5,075 visits, the 1,000 most urgent drawn and listed), the
# Map tab's first load: (window width, window height, the loaded #part-map in px). Its placeholder's room (app.css,
# stage C) must stay within 80 px of each.
LOADED_MAP = [
    (360, 780, 2867),
    (390, 844, 2904),
    (414, 896, 2956),
    (600, 900, 2707),
    (767, 1000, 2760),
    (768, 1024, 2796),
    (800, 1000, 2720),
    (860, 900, 2531),
    (900, 900, 2531),
    (991, 900, 2468),
    (992, 900, 2610),
    (1024, 768, 2353),
    (1200, 800, 2341),
    (1366, 768, 2211),
    (1366, 1000, 2618),
    (1440, 900, 2468),
    (1600, 900, 2375),
    (1920, 1080, 2606),
]
# Query ceilings (SPEC7 §3.3 6, §3.4): measured on this world plus 2. The frame with nothing stored (the request's
# floor, the reference line's runs and counts, the cache stamp, the score and rule settings, the key figures, the
# data notes, the chips, the filter bar's options and the Export menu): 21; again, its blocks kept: 13. The part
# with nothing stored (the floor, the reference counts and stamp it reads to key the map, and the map's own reads:
# the visits, their programme documents and planned places, the gazetteer, the records of the popups): 11, 12 for
# every active programme document; stored: 6. The frame cold is 27 since the menu also reads the ActivityInfo
# state and the sources (kept per worker, SPEC_HOME §4).
FRAME_QUERIES_COLD = 29
FRAME_QUERIES_WARM = 15
PART_QUERIES_COLD = 14
PART_QUERIES_STORED = 8


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


@pytest.fixture
def educator(planned):  # noqa: F811 - the fixture imported above
    """A section editor of Education: a first load shows that section (3 visits of 8)."""
    return _section_editor(planned.section)


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _forget() -> None:
    """Every stored figure gone: each worker's cache and the one every worker shares."""
    cache.clear()
    caches["shared"].clear()


def _json_blocks(html: str) -> dict[str, object]:
    found = JSON_SCRIPT.findall(html)
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a JSON id twice: {ids}"
    return {name: json.loads(html_module.unescape(body)) for name, body in found}


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the map, and the blocks the other tabs share, raises: the frame needs none."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed the map's figures")

    monkeypatch.setattr(geo, "map_points", refuse)
    monkeypatch.setattr(geo, "_compute", refuse)
    monkeypatch.setattr(views, "_map_tab", refuse)
    for name in ("summary", "entity_rows", "rule_counts"):
        monkeypatch.setattr(metrics, name, refuse)


def _computed(monkeypatch) -> Counter:
    """How many times the map was computed, by language."""
    calls: Counter = Counter()
    real = geo._compute

    def counted(scope, pd_scope, km):
        calls[translation.get_language()] += 1
        return real(scope, pd_scope, km)

    monkeypatch.setattr(geo, "_compute", counted)
    return calls


def test_the_page_is_where_the_tests_ask_for_it():
    assert reverse("fmm:dashboard") == PAGE


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, planned, monkeypatch):  # noqa: F811
    _forget()  # nothing stored: nothing may be drawn from it either
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE, {"tab": "map"})
    assert response.status_code == 200
    html = text(response)
    assert "<title>Monitoring insights ·" in html and 'class="page-purpose"' in html
    assert 'id="fmm-filters"' in html and "Showing 1 Jan – 31 Dec 2026" in html  # filter bar, reference line
    assert 'class="kpi-grid fmm-kpis"' in html and "Monitoring visits" in html  # the key figures stay
    nav = html.split('class="segmented segmented--scroll fmm-tabs"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-current="page">Map</a>' in nav  # the tab bar, the Map tab current
    assert 'name="tab" value="map" form="fmm-filters"' in html  # the filter bar keeps the tab
    assert list(placeholders(html)) == ["map"]
    for figure in (
        f'id="{MAP_JSON}"',
        "Visit locations map",
        "Visits on the map",
        "PD locations not yet visited",
    ):
        assert figure not in html, figure
    # the results container receives a part: it is not a live region (parts.js speaks once, in #parts-status, and says
    # the area's words after a tab or filter swap of it: review R2-critic-2)
    assert '<div id="fmm-results" data-results-updated="The results were updated.">' in html
    assert 'id="fmm-results" aria-live' not in html


@pytest.mark.shell
def test_every_tab_gives_the_words_said_after_a_swap_of_its_results(client_viewer, planned):  # noqa: F811
    """The tab bar, the filter bar and the view links swap #fmm-results, which is not a live region (the Map tab's map
    comes in it as a part), so the swaps of the tabs drawn whole were no longer announced (review R2-critic-2). Every
    tab's frame gives the words parts.js says after such a swap, whichever tab the page was opened on (a frame of the
    Map tab included), and a swap answers the area's content, never a second area."""
    for tab in ("insights", "quality", "analysis", "visits", "map"):
        html = text(client_viewer.get(PAGE, {"tab": tab}))
        areas = re.findall(r'<div id="fmm-results"[^>]*>', html)
        assert areas == ['<div id="fmm-results" data-results-updated="The results were updated.">'], tab
        swapped = client_viewer.get(PAGE, {"tab": tab}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="fmm-results")
        assert swapped.status_code == 200 and 'id="fmm-results"' not in text(swapped), tab


@pytest.mark.shell
def test_the_placeholder_carries_the_resolved_filter(educator, client_viewer):
    client = _signed_in(educator)
    found = placeholder_attributes(text(client.get(PAGE, {"tab": "map"})))
    assert list(found) == ["map"]
    attrs = found["map"]
    # a first load: the editor's own section written out, the tab always
    assert urlsplit(attrs["hx-get"]).path == PAGE
    assert _query(attrs["hx-get"]) == {"section": ["Education"], "tab": ["map"], "_part": ["map"]}
    assert attrs["data-part-priority"] == "high" and attrs["class"] == "part part--map"
    assert attrs["data-part-needs"] == "maplibre"  # parts.js loads the map library while the server works
    assert "data-part-group" not in attrs and "data-part-lazy" not in attrs
    assert "style" not in attrs  # its room comes from app.css (stage C), which follows the window's height
    # every section, the planned places of every active programme document, a visit centred, a filter: all travel
    html = text(
        client.get(
            PAGE, {"tab": "map", "section": "", "pd_scope": "active", "visit": "1722", "rating": "on_track"}
        )
    )
    assert _query(placeholders(html)["map"]) == {
        "section": [""],
        "rating": ["on_track"],
        "tab": ["map"],
        "pd_scope": ["active"],
        "visit": ["1722"],
        "_part": ["map"],
    }
    # the viewer without a section: every section written out
    assert _query(placeholders(text(client_viewer.get(PAGE, {"tab": "map"})))["map"])["section"] == [""]


@pytest.mark.shell
def test_the_planned_places_shown_stay_with_the_filter_bar_before_the_map_arrives(client_viewer, planned):  # noqa: F811
    """The filter bar sends the hidden ``pd_scope`` of the results: it is in the frame, so a filter changed
    before the map arrived keeps every active programme document."""
    html = text(client_viewer.get(PAGE, {"tab": "map", "section": "", "pd_scope": "active"}))
    frame = html.split('id="part-map"', 1)[0]
    assert '<input type="hidden" name="pd_scope" value="active" form="fmm-filters">' in frame
    assert 'name="pd_scope"' not in text(client_viewer.get(PAGE, {"tab": "map", "section": ""}))


@pytest.mark.shell
def test_a_filter_without_visits_shows_the_empty_tab_and_no_part(client_viewer, planned):  # noqa: F811
    html = text(client_viewer.get(PAGE, {"tab": "map", "year": "2025"}))
    assert "No visits in this filter — widen the period" in html and placeholders(html) == {}


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(educator, django_assert_max_num_queries):
    client = _signed_in(educator)
    _forget()
    with django_assert_max_num_queries(FRAME_QUERIES_COLD):
        response = client.get(PAGE, {"tab": "map"})
    assert response.status_code == 200 and list(placeholders(text(response))) == ["map"]
    with django_assert_max_num_queries(FRAME_QUERIES_WARM):
        response = client.get(PAGE, {"tab": "map"})
    assert response.status_code == 200 and list(placeholders(text(response))) == ["map"]


# ------------------------------------------------------------------------------------------------- the part
def test_the_part_answers_its_wrapper_with_the_map(client_viewer, planned):  # noqa: F811
    response = get_part(client_viewer, PAGE + "?tab=map&section=", "map")  # 200 and the #part-map wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == "map"
    assert "<html" not in html and 'id="sidebar"' not in html and 'id="fmm-filters"' not in html
    for figure in ("Visit locations map", "Visits on the map", "PD locations not yet visited", "Douris"):
        assert figure in html, figure
    assert 'data-module="edumap" data-config="fmm-map-config"' in html
    assert list(_json_blocks(html)) == [MAP_JSON]  # the map's configuration travels with its map
    # nothing that would run once swapped in, and no link that carries _part
    assert executable(html) == []
    assert all("_part" not in link for link in LINK.findall(html))
    # the planned places switch pushes the page's own address, never a part's
    switch = re.search(r'<a href="([^"]+)" hx-get="\1" hx-target="#fmm-results" hx-push-url="\1">', html)
    assert switch and "pd_scope=active" in switch.group(1) and "_part" not in switch.group(1)


def test_the_part_reads_the_figures_of_its_address(client_viewer, planned):  # noqa: F811
    """The part's figures are the map of its address: the planned places of the programme documents visited,
    or of every active one, and the visit it centres."""
    visited = get_part(client_viewer, PAGE + "?tab=map&section=", "map").context
    assert visited["pd_scope"] == "visited" and [r["place"] for r in visited["map"]["planned_rows"]] == [
        "Douris"
    ]
    PCA.objects.filter(pk=planned.pds["education"].pk).update(
        status="active"
    )  # it plans Tripoli Mina, Qalamoun
    _forget()
    active = get_part(client_viewer, PAGE + "?tab=map&section=&pd_scope=active", "map").context
    assert active["pd_scope"] == "active" and active["map"]["counts"]["planned"] == 2
    centred = get_part(client_viewer, PAGE + "?tab=map&section=&visit=1722", "map")
    assert centred.context["map_focus"]["key"] == "1722" and '"focus": "Visit 1722"' in text(centred)
    unknown = get_part(client_viewer, PAGE + "?tab=map&section=&visit=no-such-visit", "map")
    assert "That visit is not in NeuroDB." in text(unknown)


def test_the_part_stays_within_its_query_ceiling(client_viewer, planned, django_assert_max_num_queries):  # noqa: F811
    for query in ("section=&tab=map", "section=&tab=map&pd_scope=active"):
        _forget()
        with django_assert_max_num_queries(PART_QUERIES_COLD):
            response = client_viewer.get(f"{PAGE}?{query}&_part=map", **HX)
        assert response.status_code == 200 and 'id="part-map"' in text(response)
        with django_assert_max_num_queries(PART_QUERIES_STORED):  # the map stored by the request before
            response = client_viewer.get(f"{PAGE}?{query}&_part=map", **HX)
        assert response.status_code == 200


def test_a_donor_is_refused_the_part(client, planned):  # noqa: F811
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    assert client.get(f"{PAGE}?section=&tab=map&_part=map", **HX).status_code == 403


def test_a_visitor_asking_for_the_part_is_sent_to_sign_in(client, planned):  # noqa: F811
    response = client.get(f"{PAGE}?section=&tab=map&_part=map", **HX)
    assert response.status_code == 200 and "Visit locations map" not in text(response)
    assert response["HX-Redirect"].startswith(reverse("account_login") + "?next=")


def test_another_tab_has_no_part(client_viewer, planned):  # noqa: F811
    for tab in ("insights", "quality", "analysis", "visits"):
        assert client_viewer.get(f"{PAGE}?section=&tab={tab}&_part=map", **HX).status_code == 404, tab
    assert client_viewer.get(f"{PAGE}?section=&tab=map&_part=other", **HX).status_code == 404


def test_switched_off_the_part_is_404(client_viewer, planned, settings):  # noqa: F811
    settings.FMM_ENABLED = False
    assert client_viewer.get(f"{PAGE}?section=&tab=map&_part=map", **HX).status_code == 404


# ------------------------------------------------------------------------------------ the HTMX swaps of the results
def test_a_swap_of_the_results_brings_the_map_drawn_in_one_request(client_viewer, planned):  # noqa: F811
    """The tab bar, the filter bar and the map's own links swap the results (#fmm-results): the answer has the
    map in it, as before (well under a second: SPEC7_review 3c), and the same map as the part."""
    headers = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "fmm-results"}
    for query in ("section=&tab=map", "section=&tab=map&pd_scope=active", "section=Education&tab=map"):
        swapped = text(raw_get(client_viewer, f"{PAGE}?{query}", **headers))
        assert placeholders(swapped) == {}, query
        assert "<html" not in swapped and 'id="fmm-filters"' not in swapped
        assert 'name="tab" value="map" form="fmm-filters"' in swapped
        part = text(raw_get(client_viewer, f"{PAGE}?{query}&_part=map", **HX))
        start = swapped.index('<div id="part-map"')
        assert normalise(swapped[start : _element_end(swapped, start)]) == normalise(part), query
    # the other tabs swap as before: no placeholder
    for tab in ("insights", "quality", "analysis", "visits"):
        assert placeholders(text(raw_get(client_viewer, f"{PAGE}?section=&tab={tab}", **headers))) == {}, tab


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    "?tab=map",  # first load: the editor's own section, the viewer's every section
    "?tab=map&section=",
    "?tab=map&section=Education",
    "?tab=map&section=&governorate=Bekaa",
    "?tab=map&year=2025",  # the previous year: no visit, no part
    "?tab=map&section=No+such+section",  # a filter with no result
    "?tab=map&section=&pd_scope=active",
    "?tab=map&section=&visit=1722",
    "?tab=map&section=&visit=no-such-visit",
]


@pytest.mark.parametrize("role", ["editor", "staff"])
@pytest.mark.parametrize("query", MATRIX)
def test_the_assembled_page_is_the_eager_page(educator, admin_user, role, query):
    client = _signed_in(educator if role == "editor" else admin_user)
    assembled = assemble(client, PAGE + query)
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, PAGE + query)
    assert drawn.status_code == 200 and placeholders(text(drawn)) == {}
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert "_part=" not in "".join(LINK.findall(assembled.html))
    assert all("_part" not in link and "_eager" not in link for link in LINK.findall(text(drawn)))
    assert normalise(assembled.html) == normalise(text(drawn))
    together, alone = _json_blocks(assembled.html), _json_blocks(text(drawn))
    assert together == alone
    if assembled.parts:  # a filter with visits: the map's one JSON, in its part, the same as the eager page's
        assert set(assembled.parts) == {"map"}
        assert _json_blocks(text(assembled.parts["map"]))[MAP_JSON] == alone[MAP_JSON]
    else:
        assert MAP_JSON not in alone


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, planned):  # noqa: F811
    frame = text(client_viewer.get(PAGE, {"tab": "map", "section": ""}))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert len(shown) == 1 and "_eager=1" in shown[0]
    page = text(client_viewer.get(PAGE + html_module.unescape(shown[0])))
    assert placeholders(page) == {}
    for figure in (
        "Visit locations map",
        "Visits on the map",
        "PD locations not yet visited",
        f'id="{MAP_JSON}"',
    ):
        assert figure in page, figure
    assert "part__skeleton" not in page and "Show the figures" not in page
    assert "_eager=1" not in "".join(LINK.findall(page))


# ------------------------------------------------------------------------------------------- the stored map
def test_the_stored_map_is_the_same_for_every_user_and_never_holds_who_asked(
    educator, admin_user, monkeypatch
):
    """``geo.map_points`` is stored for every worker and every person: it reads the address only. A section
    editor's request stores it, an administrator's reads it, and both see the same map."""
    calls = _computed(monkeypatch)
    editor, staff = _signed_in(educator), _signed_in(admin_user)
    address = f"{PAGE}?section=Education&tab=map&_part=map"
    first = raw_get(editor, address, **HX)
    cache.clear()  # this worker's own cache gone: the second request reads the shared one
    second = raw_get(staff, address, **HX)
    assert first.status_code == second.status_code == 200 and calls == {"en": 1}
    assert normalise(text(first)) == normalise(text(second))
    assert first.context["map"] == second.context["map"]
    for person in (educator, admin_user):
        assert person.email not in text(first)


def test_the_map_drawn_in_french_after_english_has_french_words(client_viewer, planned, monkeypatch):  # noqa: F811
    """The popups' dates are written in the language of the request (Django's own month names): the stored map
    is kept per language (SPEC7_review item 10), so French never reads the English map, nor the other way."""
    calls = _computed(monkeypatch)
    address = f"{PAGE}?section=&tab=map&_part=map"
    english = raw_get(client_viewer, address, HTTP_ACCEPT_LANGUAGE="en", **HX)
    french = raw_get(client_viewer, address, HTTP_ACCEPT_LANGUAGE="fr", **HX)
    again = raw_get(client_viewer, address, HTTP_ACCEPT_LANGUAGE="en", **HX)
    assert calls == {"en": 1, "fr": 1}  # each language computed once, English read back after French
    day = Visit.objects.get(key="1722").end_date
    with translation.override("en"):
        in_english = date_format(day, "j M Y")
    with translation.override("fr"):
        in_french = date_format(day, "j M Y")
    assert in_english != in_french
    config = {lang: _json_blocks(text(r))[MAP_JSON] for lang, r in (("en", english), ("fr", french))}
    assert in_english in json.dumps(config["en"]) and in_english not in json.dumps(config["fr"])
    assert in_french in json.dumps(config["fr"])
    assert text(again) == text(english)


def test_a_place_renamed_in_the_admin_shows_at_once(client, admin_user, planned):  # noqa: F811
    """Fresh after edit: an administrator renames a planned place in the admin (no refresh, no sync): the map
    shows the new name at once, though it was stored a moment before (the gazetteer is in the stamp)."""
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)

    def not_visited() -> list[str]:
        return [
            r["place"]
            for r in get_part(client, PAGE + "?tab=map&section=", "map").context["map"]["planned_rows"]
        ]

    assert not_visited() == ["Douris"]  # stored now
    douris = planned.cadasters["douris"]
    url = reverse("admin:locations_location_change", args=[douris.pk])
    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {
            "name": "Douris (Baalbek)",
            "type": douris.type_id,
            "latitude": douris.latitude or "",
            "longitude": douris.longitude or "",
            "p_code": douris.p_code,
            "cas_code": douris.cas_code,
            "parent": douris.parent_id or "",
            "is_active": "on",
        },
    )
    assert response.status_code == 302, text(response)[:2000]
    assert Location.objects.get(pk=douris.pk).name == "Douris (Baalbek)"
    assert not_visited() == ["Douris (Baalbek)"]


def test_a_finished_etools_sync_shows_its_planned_places_at_once(client_viewer, planned):  # noqa: F811
    """The eTools sync writes the programme documents' planned places and starts no refresh: when its run
    finishes, the map shows what it wrote at once (its run is in the stamp)."""

    def not_visited() -> list[str]:
        response = get_part(client_viewer, PAGE + "?tab=map&section=", "map")
        return sorted(r["place"] for r in response.context["map"]["planned_rows"])

    assert not_visited() == ["Douris"]  # stored now
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS, target="interventions", status=SyncRun.Status.RUNNING
    )
    planned.pds["amended"].locations.add(planned.cadasters["qalamoun"])  # what the run writes
    run.finish(SyncRun.Status.SUCCEEDED)
    assert not_visited() == ["Douris", "Qalamoun"]


# ------------------------------------------------------------------------------------------------- layout
def _stage_c() -> str:
    css = (STATIC / "css" / "app.css").read_text()
    return css.split("stage C (Monitoring insights): part fixes", 1)[1].split("end of stage C", 1)[0]


def _room(block: str, width: int, height: int) -> float:
    """The placeholder's room (--part-h) at a window size, from the stage C rules: the last band that holds wins,
    ``calc(<text>px + min(60vh, 520px) + 150vh)``."""
    rule = re.compile(
        r"#part-map\[data-part=\"map\"\] \{ --part-h: calc\((\d+)px \+ min\(60vh, 520px\) \+ 150vh\); \}"
    )
    bands = [(10**6, int(words)) for words in rule.findall(block.split("@media", 1)[0])]
    for most, words in re.findall(r"@media \(max-width: ([\d.]+)px\) \{ " + rule.pattern + r" \}", block):
        bands.append((float(most), int(words)))
    assert len(bands) >= 6, bands
    words = min((most, value) for most, value in bands if width <= most)[1]
    vh = height / 100
    return words + min(60 * vh, 520) + 150 * vh


def test_the_map_placeholder_keeps_the_room_of_the_map_that_arrives():
    """Measured at 18 window sizes on step 3's world: the room is within 80 px of the map that arrives, whatever the
    window's width (the map panel's text wraps) and height (the map and its two tables follow it)."""
    block = _stage_c()
    misses = {
        (width, height): (round(_room(block, width, height)), loaded)
        for width, height, loaded in LOADED_MAP
        if abs(_room(block, width, height) - loaded) > 80
    }
    assert misses == {}
    # its grey shapes: the map's block, then rows for the two tables (not one block and blank space)
    assert (
        '#part-map[data-part="map"] .part__skeleton .skeleton::after { content: ""; display: block;' in block
    )
    tag = (
        Path(settings.BASE_DIR) / "neurodb" / "fmm" / "templates" / "fmm" / "tabs" / "map.html"
    ).read_text()
    assert (
        '{% part "map" kind="map" priority="high" %}' in tag
    )  # no height= of its own: the rules above set it


def test_a_failed_map_shows_its_error_box_without_the_map_room():
    """The map's placeholder keeps the room of a large map and its tables: when its request fails, the error
    box goes without it, so the page below is not pushed screens further down. Try again gives it back. The
    generic rule of every part does it (the map had a copy of its own, the same two declarations: review
    R1-cross-2): it comes after the rules that give a placeholder its room and its middle height."""
    css = (STATIC / "css" / "app.css").read_text()
    generic = css.split("pages that load in parts (parts.js)", 1)[1].split("stage A (reports core)", 1)[0]
    collapse = '.part[data-part-state="error"] { min-height: 0; }'
    no_float = '.part[data-part-state="error"]::before { content: none; }'
    for reserving in (
        ".part { min-height: var(--part-h, 8rem); }",
        ".part { min-height: var(--part-h-sm, var(--part-h-md, var(--part-h, 8rem))); }",
        '[data-part]:not(.is-loaded)::before { content: ""; float: left;',
    ):
        assert generic.index(reserving) < generic.index(collapse) < generic.index(no_float), reserving
    # the map's own rules set its room (--part-h) and its grey shapes, in no state of their own: no rule gives a
    # failed map its room back, and Try again gives it back while the part loads
    block = _stage_c()
    assert re.findall(r"#part-map\[[^{]*data-part-state[^{]*\{", block) == []
    assert "min-height" not in "".join(re.findall(r"#part-map\[[^{]*\{([^}]*)\}", block))


def test_the_printable_report_has_no_part(client_viewer, planned):  # noqa: F811
    """The printable report (data-autoprint) prints what it holds: it never loads in parts (SPEC7_review 14), and
    it still includes the key figures and the briefing, which the Map tab's conversion leaves alone."""
    html = text(raw_get(client_viewer, reverse("fmm:report"), {"section": ""}))
    assert "data-part" not in html and 'class="kpi-grid fmm-kpis"' in html
