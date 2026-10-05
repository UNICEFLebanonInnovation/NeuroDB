"""The Map tab (``fmm.geo``): visits set against the places their programme documents planned.

On ``fm_world`` built on 5 October 2026, the amended Bekaa PD (visited at 1722, Zahle town's own point,
and at 1727, Baalbek district's own centre) is given three planned places here: Zahle town, the
Baalbek district and Douris. The education PD plans Tripoli Mina (1726 was at a site 0.14 km away) and
Qalamoun (the reference-only visit and 1724 were at its own point).
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from neurodb.fmm import geo
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit
from neurodb.fmm.place import haversine_km
from neurodb.fmm.scope import Scope
from neurodb.partnerships.models import PCA
from neurodb.web.templatetags.ui import ASSETS

from .conftest import CANARIES, CANARY_TEXT, LEAD, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
KM_PER_DEGREE = 111.195  # one degree of latitude on the mean Earth radius
STATIC = Path(settings.BASE_DIR) / "neurodb" / "web" / "static"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


@pytest.fixture
def planned(fm_world, built):
    """The amended Bekaa PD plans Zahle town, the Baalbek district and Douris."""
    c, d = fm_world.cadasters, fm_world.districts
    fm_world.pds["amended"].locations.set([c["zahle_town"], d["baalbek"], c["douris"]])
    return fm_world


def _scope(**params) -> Scope:
    return Scope.from_params({"section": "", "year": "2026", **params})


def _pd_location(pk=500, *, lat=33.9, lon=35.9, precise=True, name="Somewhere", p_code="LBX1", ancestors=()):
    return geo.PdLocation(
        pd_id=1,
        pd_number="LEB/PCA1/PD1",
        partner_id=None,
        location_id=pk,
        name=name,
        p_code=p_code,
        governorate="Bekaa",
        governorate_key="bekaa",
        district="",
        latitude=lat,
        longitude=lon,
        level=3 if precise else 2,
        own_point=True,
        precise=precise,
        approximate_from="",
        ancestors=frozenset({pk, *ancestors}),
    )


def _visit(lat=33.9, lon=35.9, precise=True, **fields) -> Visit:
    return Visit(key="v", latitude=lat, longitude=lon, point_precise=precise, **fields)


# ------------------------------------------------------------------------------------------ matching
def test_haversine_beirut_tripoli_is_about_69_km():
    assert abs(haversine_km(33.8938, 35.5018, 34.4367, 35.8497) - 69) <= 1
    assert haversine_km(33.9, 35.9, 33.9, 35.9) == 0


def test_two_precise_points_match_below_the_distance_only():
    near = _pd_location(lat=33.9 + 1.9 / KM_PER_DEGREE, name="Near")
    far = _pd_location(lat=33.9 + 2.1 / KM_PER_DEGREE, name="Far")
    kind, loc, distance = geo.match(_visit(), [near], 2.0, ancestors=frozenset())
    assert (kind, loc) == ("coords", near) and abs(distance - 1.9) < 0.01
    assert geo.match(_visit(), [far], 2.0, ancestors=frozenset()) == ("none", None, None)
    # the nearer of two precise places wins
    assert geo.match(_visit(), [far, near], 2.5, ancestors=frozenset())[1] == near


def test_a_point_that_is_not_precise_never_matches_by_coordinates_but_can_by_place():
    district = _pd_location(pk=21, precise=False, name="Baalbek")  # a planned "location" that is a district
    cadaster = _pd_location(pk=32, name="Baalbek town")
    # the visit at an ancestor's point or a district's own centre, on the very same coordinates
    assert geo.match(_visit(precise=False), [cadaster], 2.0, ancestors=frozenset()) == ("none", None, None)
    # a precise visit and a planned district at the same point: not by coordinates either
    assert geo.match(_visit(), [district], 2.0, ancestors=frozenset())[0] == "none"
    # ... but by place when the visit lies in that district
    kind, loc, _distance = geo.match(
        _visit(location_id=32), [district], 2.0, ancestors=frozenset({32, 21, 10})
    )
    assert (kind, loc) == ("place", district)


def test_place_matches_by_location_p_code_area_and_name():
    loc = _pd_location(pk=31, name="Saadnayel", p_code="LB31", lat=None, lon=None)
    nowhere = {"lat": None, "lon": None}
    assert geo.match(_visit(**nowhere, location_id=31), [loc], 2.0, ancestors=frozenset({31}))[0] == "place"
    assert geo.match(_visit(**nowhere, place_pcode="lb31"), [loc], 2.0, ancestors=frozenset())[0] == "place"
    # a planned district holds the visit's cadaster
    district = _pd_location(pk=20, name="Zahle", p_code="LB20")
    found = geo.match(_visit(**nowhere, location_id=31), [district], 2.0, ancestors=frozenset({31, 20, 10}))
    assert found[:2] == ("place", district) and found[2] is None
    # the same name, folded (case and accents)
    assert (
        geo.match(_visit(**nowhere, place_name="SAADNÂYEL "), [loc], 2.0, ancestors=frozenset())[0] == "place"
    )
    assert geo.match(_visit(**nowhere, place_name="Zahle"), [loc], 2.0, ancestors=frozenset())[0] == "none"
    assert geo.match(_visit(), [], 2.0) == ("none", None, None)


def test_match_reads_the_visit_place_when_not_given(planned, django_assert_max_num_queries):
    from neurodb.datamart.monitoring import _gazetteer
    from neurodb.fmm.place import lowest_admin_level

    baalbek = planned.districts["baalbek"].pk
    # one query gives what the per-level reading gives, and the lowest admin level
    gazetteer, lowest = geo.gazetteer_with_lowest({baalbek, planned.cadasters["douris"].pk})
    assert gazetteer == _gazetteer({baalbek, planned.cadasters["douris"].pk})
    assert lowest == lowest_admin_level() == 3 and geo.gazetteer_with_lowest(set()) == ({}, None)
    district = geo.pd_location(1, "PD", None, baalbek, gazetteer, lowest)
    assert district is not None and not district.precise  # a district's own centre is never precise
    visit = Visit.objects.get(key="1727")  # placed at that centre
    with django_assert_max_num_queries(1):  # its place and the places above it, in one query
        kind, loc, distance = geo.match(visit, [district], 2.0)
    assert (kind, loc) == ("place", district) and distance == 0


# ------------------------------------------------------------------------------------------ map_points
def _points(data, group=None):
    return [p for p in data["config"]["points"] if group is None or p["group"] == group]


def test_the_visits_are_matched_to_their_own_programme_documents(planned):
    data = geo.map_points(_scope())
    by_name = {p["name"]: p for p in _points(data) if p.get("shape") != "ring"}
    # 1722 at Zahle town's own point, a planned place of its PD: by coordinates, 0 km
    assert by_name["Visit 1722"]["group"] == "coords"
    # 1726 at the Tripoli community centre (a site), 0.14 km from planned Tripoli Mina
    assert by_name["Visit 1726"]["group"] == "coords"
    # 1727 at the Baalbek district's own centre: never by coordinates, but by place (the planned district)
    assert by_name["Visit 1727"]["group"] == "place"
    # 1723 (Douris) went to an SSFA that plans nothing: not linked, although Douris is planned by another PD
    assert by_name["Visit 1723"]["group"] == "none"
    rows = {r["key"]: r for r in data["visit_rows"]}
    assert rows["1722"]["distance"] == "0.0 km" and rows["1722"]["pd_location"] == "Zahle town"
    assert (
        rows["1726"]["distance"] == "0.1 km" and rows["1726"]["pd_number"] == planned.pds["education"].number
    )
    c = data["counts"]
    assert c["mapped"] == c["coords"] + c["place"] + c["none"] == len(rows)
    assert c["place"] == 1 and c["unlocated"] == 0


def test_planned_places_not_visited_are_counted_and_drawn_as_rings(planned):
    data = geo.map_points(_scope())
    rings = _points(data, geo.PLANNED)
    assert [r["name"] for r in rings] == ["Douris"]
    ring = rings[0]
    assert ring["shape"] == "ring" and ring["color"] == "var(--nd-muted)"
    assert ring["href"] == reverse("reports:programme_detail", args=[planned.pds["amended"].pk])
    assert ring["open"] == "page"  # a programme document opens as a page, not in the visit window
    assert data["counts"]["not_visited"] == 1 and data["counts"]["rings"] == 1
    assert data["counts"]["planned"] == 5  # 3 planned by the amended PD, 2 by the education PD
    assert [r["place"] for r in data["planned_rows"]] == ["Douris"]
    legend = {e["group"]: e for e in data["config"]["legend"]}
    assert legend[geo.PLANNED]["shape"] == "ring" and all(e["toggle"] for e in legend.values())
    assert legend["coords"]["label"].startswith("Matched by coordinates (")
    # a governorate filter keeps that governorate's planned places only
    from django.core.cache import cache

    planned.pds["amended"].locations.add(planned.cadasters["qalamoun"])  # in the North
    cache.clear()
    assert {r["place"] for r in geo.map_points(_scope())["planned_rows"]} == {"Douris", "Qalamoun"}
    bekaa = geo.map_points(_scope(governorate="Bekaa"))
    assert [r["place"] for r in bekaa["planned_rows"]] == ["Douris"] and bekaa["counts"]["planned"] == 3


def test_a_place_two_of_the_visit_programme_documents_planned_is_visited_for_both(planned):
    """1722 (at Zahle town) also counts for the education PD when that PD planned Zahle town too:
    no ring is drawn under the visit for the second PD."""
    education = planned.pds["education"]
    education.locations.add(planned.cadasters["zahle_town"])
    visit = Visit.objects.get(key="1722")
    Visit.objects.filter(pk=visit.pk).update(pd_ids=[*visit.pd_ids, education.pk])
    data = geo.map_points(_scope())
    assert "Zahle town" not in {r["place"] for r in data["planned_rows"]}
    assert "Zahle town" not in {p["name"] for p in _points(data, geo.PLANNED)}
    assert data["counts"]["planned"] == 6 and data["counts"]["not_visited"] == 1  # Douris only


def test_an_empty_scope_has_no_active_programme_document(planned):
    from dataclasses import replace

    PCA.objects.filter(pk=planned.pds["education"].pk).update(status="active")
    assert geo._active_pds(_scope()).exists()
    assert not geo._active_pds(replace(_scope(), empty=True)).exists()


def test_points_that_are_not_precise_are_fainter(planned):
    data = geo.map_points(_scope())
    by_name = {p["name"]: p for p in _points(data)}
    assert by_name["Visit 1727"]["opacity"] == geo.APPROXIMATE_OPACITY == 0.6
    assert ["Where", "approximate: placed at Baalbek"] in by_name["Visit 1727"]["lines"]
    assert "opacity" not in by_name["Visit 1722"] and "opacity" not in by_name["Visit 1726"]


def test_every_active_programme_document_of_the_filter(planned):
    other = PCA.objects.create(
        etl_id="99",
        partner=planned.partners["mercy"],
        number="LEB/PCA2026999/PD2026001",
        status="active",
        section_names=["Education"],
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    other.locations.set([planned.cadasters["saadnayel"]])
    visited = geo.map_points(_scope())
    assert "LEB/PCA2026999/PD2026001" not in {r["pd_number"] for r in visited["planned_rows"]}
    active = geo.map_points(_scope(), "active")
    assert active["pd_scope"] == "active"
    assert {r["pd_number"] for r in active["planned_rows"]} == {"LEB/PCA2026999/PD2026001"}
    # a section the PD is not in, or another partner, leaves it out
    assert not geo.map_points(_scope(section="Health"), "active")["planned_rows"]
    assert not geo.map_points(_scope(partner=str(planned.partners["amel"].pk)), "active")["planned_rows"]
    # the visits are still matched to their own programme documents
    assert active["counts"]["coords"] == visited["counts"]["coords"]
    # an unknown choice falls back to the default
    assert geo.map_points(_scope(), "everything")["pd_scope"] == "visited"


def test_at_most_a_thousand_points_with_a_note(planned, monkeypatch, client_viewer):
    monkeypatch.setattr(geo, "MAX_POINTS", 3)
    data = geo.map_points(_scope())
    assert len(data["config"]["points"]) == 3 and data["counts"]["capped"]
    assert len(data["visit_rows"]) == 3  # the table lists the points drawn
    assert data["counts"]["mapped"] > 3  # ... while the counts cover every visit
    response = client_viewer.get(PAGE, {"tab": "map", "section": "", "year": "2026"}, HTTP_HX_REQUEST="true")
    assert "The map shows the first 3 points" in response.content.decode()
    monkeypatch.undo()
    assert geo.MAX_POINTS == 1000


def test_the_configuration_carries_only_the_allowed_keys_and_no_person(planned):
    data = geo.map_points(_scope())
    config = data["config"]
    assert set(config) <= geo.CONFIG_KEYS
    for point in config["points"]:
        assert set(point) <= geo.POINT_KEYS, point
    text = json.dumps(data, default=str)
    for canary in (LEAD, MEMBER, MEMBER_EMAIL, CANARY_TEXT, *CANARIES, "Classes held as planned"):
        assert canary not in text


def test_edumap_loads_only_openstreetmap_tiles_and_is_registered():
    source = (STATIC / "js" / "edumap.js").read_text()
    hosts = set(re.findall(r"https://([^/\"'`]+)/", source))
    assert hosts == {"tile.openstreetmap.org"}
    assert ASSETS["eduMapModule"] == "js/edumap.js"
    assert 'edumap: "eduMapModule"' in (STATIC / "js" / "app.js").read_text()


# ------------------------------------------------------------------------------------------ the tab
def _map_tab(client, **params):
    response = client.get(
        PAGE, {"tab": "map", "section": "", "year": "2026", **params}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    return response.content.decode()


def _config(html: str) -> dict:
    found = re.search(r'<script id="fmm-map-config" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(found.group(1))


def test_the_map_tab_lists_every_point_below_the_map(planned, client_viewer):
    html = _map_tab(client_viewer)
    assert 'data-module="edumap" data-config="fmm-map-config"' in html
    assert "The table below lists every point on this map" in html
    config = _config(html)
    assert config["mode"] == "points" and config["open"] == "modal"
    # the keyboard route: every point of the map is in the table with its link
    for point in config["points"]:
        assert f'href="{point["href"]}"' in html, point["name"]
    assert html.index("map-shell") < html.index("Visits on the map")
    # the legend chips and the toggle to every active programme document
    assert "matched by coordinates" in html and "PD location not yet visited" in html
    assert "pd_scope=active" in html and "tab=map" in html


def test_the_tab_keeps_the_choice_of_planned_places(planned, client_viewer):
    html = _map_tab(client_viewer, pd_scope="active")
    assert '<input type="hidden" name="pd_scope" value="active" form="fmm-filters">' in html
    assert "pd_scope=active" not in html.split("Every active programme document")[0].rsplit("<a", 1)[-1]


def test_a_visit_named_in_the_address_is_centred_and_opened(planned, client_viewer):
    html = _map_tab(client_viewer, visit="1722")
    assert _config(html)["focus"] == "Visit 1722"
    assert 'class="table-active"' in html
    # a visit of another year is not in this filter; an unknown key is said so
    Visit.objects.filter(key="1723").update(end_date=datetime.date(2025, 6, 20))
    from django.core.cache import cache

    cache.clear()
    html = _map_tab(client_viewer, visit="1723")
    assert "focus" not in _config(html) and "Visit 1723 is not in this filter." in html
    assert "That visit is not in NeuroDB." in _map_tab(client_viewer, visit="nope")


def test_a_visit_past_the_points_drawn_is_said_so(planned, client_viewer, monkeypatch):
    monkeypatch.setattr(geo, "MAX_POINTS", 1)  # the most urgent visit only
    drawn = geo.map_points(_scope())["visit_rows"][0]["key"]
    other = Visit.objects.exclude(key=drawn).filter(latitude__isnull=False, end_date__year=2026).first()
    html = _map_tab(client_viewer, visit=other.key)
    assert f"{other.label} is in this filter but past the points the map draws" in html
    assert "is not in this filter" not in html and "focus" not in _config(html)


def test_the_choice_of_planned_places_survives_a_filter_without_visits(planned, client_viewer):
    html = _map_tab(client_viewer, pd_scope="active", year="2019")
    assert '<input type="hidden" name="pd_scope" value="active" form="fmm-filters">' in html


def test_a_visit_without_coordinates_is_listed_below_the_map(planned, client_viewer):
    Visit.objects.filter(key="1725").update(latitude=None, longitude=None, located_by="", point_precise=False)
    html = _map_tab(client_viewer, visit="1725")
    assert "Visits without coordinates" in html
    assert "has no coordinates, so it is listed below the map only" in html
    assert reverse("fmm:visit", args=["1725"]) in html
    assert "focus" not in _config(html)


def test_no_coordinates_at_all_shows_the_empty_state(planned, client_viewer):
    Visit.objects.update(latitude=None, longitude=None, point_precise=False)
    html = _map_tab(client_viewer)
    # the planned places are still drawn
    assert "No visit in this filter has coordinates · the map shows the planned places only" in html
    assert "map-shell" in html and {p["group"] for p in _config(html)["points"]} == {geo.PLANNED}
    PCA.locations.through.objects.all().delete()
    from django.core.cache import cache

    cache.clear()
    html = _map_tab(client_viewer)
    assert "No visit in this filter has coordinates" in html and "map-shell" not in html
    assert "Nothing to map in this filter" in html


def test_the_visit_page_links_to_the_map_centred_on_it(planned, client_viewer):
    html = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert "On the map" in html
    link = re.search(r'href="([^"]*tab=map[^"]*)"', html).group(1).replace("&amp;", "&")
    assert "visit=1722" in link and "year=2026" in link and "section=" in link
    # the link opens the tab with the visit in its filter
    response = client_viewer.get(link)
    assert response.status_code == 200 and '"focus": "Visit 1722"' in response.content.decode()
    # a visit without a point has no link
    Visit.objects.filter(key="1725").update(latitude=None, longitude=None)
    assert "On the map" not in client_viewer.get(reverse("fmm:visit", args=["1725"])).content.decode()


def test_the_map_tab_keeps_to_the_query_budget(planned, client_viewer, django_assert_max_num_queries):
    from django.core.cache import cache

    cache.clear()
    with django_assert_max_num_queries(20):
        client_viewer.get(PAGE, {"tab": "map"}, HTTP_HX_REQUEST="true")
    cache.clear()
    with django_assert_max_num_queries(30):
        client_viewer.get(PAGE, {"tab": "map"})
