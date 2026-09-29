"""The Makani and Dirasa dashboards: the cube reader, labels and crosswalk, every tab, filters, the HTMX
partial, caching and the older payload format."""

import datetime
import re

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from neurodb.education import services
from neurodb.education.cube import Cube
from neurodb.education.figures import Figures
from neurodb.education.models import EducationFigures
from neurodb.geo.models import GovernorateLocation

from .payloads import SCHOOLS, dirasa_payload, format_one, makani_payload, text_key

pytestmark = pytest.mark.django_db

# one entry per registration; child 1 is registered at two centers
REGISTRATIONS = [
    {
        "child": 1, "center": 100, "sex": "Female", "nationality": 1, "disability": 1, "caregiver": "Mother",
        "working": "No", "programme": "BLN Level 1", "package": "Core-Package", "id_type": 1,
        "education_status": "Never registered in any formal school before",
        "malnutrition": "No malnutrition screening", "delay": "No",
        "counselling": 1, "immunized": 1, "minimum_meals": 1, "vaccinated": 1,
    },
    {
        "child": 1, "center": 101, "sex": "Female", "nationality": 1, "disability": 1, "caregiver": "Mother",
        "working": "Yes", "programme": "BLN Level 2", "package": "Core-Package", "id_type": 1,
    },
    {
        "child": 2, "center": 100, "sex": "Male", "nationality": 5, "disability": 3, "caregiver": "Father",
        "working": "Yes", "programme": "YFS Level 1 - RS Grade 9", "package": "Walk-in", "id_type": 5,
        "education_status": "Currently registered in Formal Education school",
        "malnutrition": "MAM (MUAC >11.5 and <12.5 cm)", "delay": "Language/Communication", "married": 1, "idp": 1,
    },
    {
        "child": 3, "center": 102, "sex": "Female", "nationality": 4, "disability": None, "caregiver": "Other",
        "programme": "Summer RS Grade 3", "package": "Core-Package", "delay": "Social/Emotional", "idp": 1,
    },
    {
        "child": 4, "center": 101, "sex": "Male", "nationality": 1, "disability": 6, "caregiver": "Mother",
        "working": "No", "package": "Walk-in", "id_type": 7,
        "malnutrition": "SAM with Bilateral pitting oedema  (both feet puffy)", "vaccinated": 1,
    },
]  # fmt: skip
STAFF = [
    {"center": 100, "sex": "Female", "active": "Yes"},
    {"center": 100, "sex": "Male", "active": "No"},
    {"center": 101, "sex": "Female", "active": "Yes"},
    {"center": 102, "sex": "Female"},
]
BRIDGING = [  # the governorate is the child's; child 14 lives in Akkar and attends a Beirut school
    {"child": 11, "school": 300, "partner": 7, "governorate": 20, "sex": "Female", "nationality": 1, "disability": 1,
     "level": "level_one", "barrier": "family_moved", "pre_tested": 1},
    {"child": 12, "school": 300, "partner": 7, "governorate": 20, "sex": "Male", "nationality": 5, "disability": 3,
     "level": "level_two", "barrier": "sickness", "pre_tested": 1, "post_tested": 1},
    {"child": 13, "school": 301, "partner": 6, "governorate": 22, "sex": "Female", "nationality": 4,
     "barrier": "sickness"},
    {"child": 14, "school": 301, "partner": 7, "governorate": 20, "sex": "Female", "nationality": 1, "disability": 6},
    {"child": 15, "school": 302, "partner": 6, "governorate": 24, "sex": "Male", "nationality": 2, "disability": 1,
     "barrier": "seasonal_work"},
]  # fmt: skip
TEACHERS = [
    {"school": 300, "sex": "Female", "trainings": [1, 2]},
    {"school": 300, "sex": "Male", "trainings": [2]},
    {"school": 301, "sex": "Female", "trainings": []},
    {"school": 302, "sex": "Female", "trainings": [3]},
]
OUTREACH = [
    {"year": "2025", "partner": " Dirasa Partner ", "governorate": "Akkar",
     "education_status": "Never been engaged in any type of learning", "referral": "Referred_to_Dirasa",
     "dropout_reason": "recently_moved_from_syria_to_lebanon", "id_type": "unhcr_registered"},
    {"year": "2025", "partner": "Dirasa Partner", "governorate": "Beirut",
     "education_status": "never_been_engaged_in_any_type_of_learni", "referral": "Multi_Service_Community_Centers",
     "id_type": "UNHCR registered"},
    {"year": "2024", "partner": "Other", "governorate": "Akkar",
     "education_status": "Already_Enrolled_in_Formal_Education_(RS)", "dropout_reason": "Other reasons",
     "id_type": "No_papers"},
    {"year": None},
]  # fmt: skip
RING = ["[35.0, 33.0]", "[36.0, 33.0]", "[36.0, 34.0]", "[35.0, 34.0]"]


def store(payload, programme=None, period=None, **extra):
    return EducationFigures.objects.create(
        programme=programme or payload["programme"],
        year=period or payload["year"],
        counted_at=datetime.datetime(2026, 9, 29, 6, 4, 37, tzinfo=datetime.UTC),
        fetched_at=timezone.now(),
        payload=payload,
        **extra,
    )


@pytest.fixture
def polygons(db):
    for i, (code, name) in enumerate(
        (("AKK", "Akkar"), ("BEI", "Beirut"), ("BEK", "Baalbek-Hermel"), ("NAB", "Nabatieh")), start=1
    ):
        GovernorateLocation.objects.create(code=code, name=name, polygon_coordinates=RING, ai_id=i)


@pytest.fixture
def makani(db):
    return store(makani_payload(REGISTRATIONS, STAFF))


@pytest.fixture
def dirasa(db):
    return store(dirasa_payload(BRIDGING, TEACHERS, OUTREACH))


def page(client, name, **params):
    response = client.get(reverse(f"education:{name}"), params)
    assert response.status_code == 200
    return response


# ------------------------------------------------------------------------------------ cube reader
def test_cube_filters_and_adds_up():
    cube = Cube(
        {
            "dims": ["center", "sex"],
            "measures": ["registrations", "married"],
            "rows": [[1, "Female", 3, 1], [1, "Male", 2, 0], [2, "Female", 4, 0], [None, "Male", 1, 1]],
        }
    )
    assert cube.sum("registrations") == 10 and cube.sum("married") == 2
    assert cube.filter(sex="Female").sum("registrations") == 7
    assert cube.filter(center={1, 2}, sex="Male").sum("registrations") == 2
    assert cube.filter(center=None).sum("registrations") == 1  # None: the unknown value
    assert cube.by("center", "registrations") == {1: 5, 2: 4, None: 1}
    assert cube.by("center", "married") == {1: 1, None: 1}  # a value adding up to 0 is left out
    assert cube.distinct("center", "registrations") == {1, 2}
    assert cube.distinct("center", "married") == {1}
    assert cube.values("center") == {1, 2, None}
    assert len(cube.filter(school=1)) == 0 and cube.sum("unknown") == 0 and cube.by("school", "married") == {}
    assert len(Cube(None)) == 0 and Cube({"error": "OperationalError"}).error == "OperationalError"


def test_generated_cubes_add_up_to_the_registrations():
    payload = makani_payload(REGISTRATIONS, STAFF)
    enrolment = Figures(payload).cube("enrolment")
    assert enrolment.sum("registrations") == len(REGISTRATIONS)
    assert sum(enrolment.by("sex", "registrations").values()) == len(REGISTRATIONS)
    assert enrolment.sum("screened") == 3  # a malnutrition result recorded
    assert Figures(payload).block("registrations").total({}) == {"people": 4}  # child 1 once


# ------------------------------------------------------------------------------ labels and places
@pytest.mark.parametrize(
    ("value", "family"),
    [
        ("BLN Level 1", "BLN"),
        ("BLN Catch-up", "BLN"),
        ("CBECE Level 3", "CBECE"),
        ("YFS Level 1 - RS Grade 9", "YFS - RS"),
        ("YFS Level 2 - RS Grade 9", "YFS - RS"),
        ("RS-YFS", "YFS - RS"),
        ("Summer RS Grade 3", "Summer RS"),
        ("RS Grade 4", "RS"),
        ("YFNL Level 1", "YFNL"),
        ("YBLN", "YBLN"),
        ("ECD", "ECD"),
        ("", None),
        (None, None),
    ],
)
def test_programme_family(value, family):
    assert services.programme_family(value) == family


def test_labels():
    assert text_key("Never been engaged in any type of learning") == text_key(
        "never_been_engaged_in_any_type_of_learni"
    )
    assert services.outreach_label("never_been_engaged_in_any_type_of_learni") == (
        "Never engaged in any type of learning"
    )
    assert services.outreach_label("referred_to_dirasa") == "Referred to Dirasa"
    assert services.outreach_label("unhcr_registered") == "UNHCR registered"
    assert services.outreach_label("no_papers") == "No papers"
    assert services.outreach_label("family_has_no_or_expired_doc") == "Family has no or expired documents"
    assert services.outreach_label("some_new_answer") == "Some new answer"
    assert services.barrier_label("family_moved") == "Family moved"
    assert services.barrier_label("seasonal_work") == "Seasonal work"
    assert services.barrier_label("Moved back to Syria") == "Moved back to Syria"
    names = ("Syrian", "Lebanese", "Palestinian from Syria", "Iraqi")
    groups = [services.nationality_group(n) for n in names]
    assert groups == ["Syrian", "Lebanese", "Non-Lebanese", "Non-Lebanese"]
    assert services.nationality_group(None) == "Not specified"


def test_governorate_crosswalk(polygons):
    assert services.governorate_key("Baalbeck-Hermel") == services.governorate_key("بعلبك-الهرمل")
    assert services.governorate_key("El Nabatieh") == services.governorate_key("النبطية") == "nabatieh"
    names = {20: "Akkar", 23: "بعلبك-الهرمل", 24: "El Nabatieh", 25: "Keserwan-Jbeil"}
    figures = Figures({"locations": [{"id": k, "name": v} for k, v in names.items()]})
    assert services.governorate_codes(figures, [20, 23, 24, 25, 99]) == {20: "AKK", 23: "BEK", 24: "NAB"}
    config, rows = services.choropleth(figures, {20: 5, 23: 2, 25: 1}, "children")
    assert config["values"] == {
        "AKK": {"value": 5, "label": "5 children"},
        "BEK": {"value": 2, "label": "2 children"},
    }
    assert [(r["label"], r["value"], r["mapped"]) for r in rows] == [
        ("Akkar", 5, True),
        ("بعلبك-الهرمل", 2, True),
        ("Keserwan-Jbeil", 1, False),
    ]


# ----------------------------------------------------------------------------------------- Makani
def test_education_opens_makani(client_viewer):
    response = client_viewer.get(reverse("education:dashboard"))
    assert response.status_code == 302 and response.url == reverse("education:makani")


def test_makani_overview(client_viewer, makani):
    response = page(client_viewer, "makani")
    data = response.context["data"]
    assert data["kpis"] == {
        "centers": 3,
        "children": 5,
        "cwd": 2,  # a disability other than "No"; unknown left out
        "unique": 4,
        "working": 2,
        "married": 1,
        "counselling": 1,
        "idp": 2,
        "staff": 4,
    }
    charts = data["charts"]
    assert charts["gender"] == [["Female", 3], ["Male", 2]]
    assert charts["governorate"] == [["Akkar", 2], ["Beirut", 2], ["بعلبك-الهرمل", 1]]
    assert charts["nationality"] == [["Syrian", 3], ["Lebanese", 1], ["Palestinian from Lebanon", 1]]
    assert charts["programme"] == [["BLN", 2], ["Summer RS", 1], ["YFS - RS", 1]]
    assert charts["package"] == [["Core-Package", 3], ["Walk-in", 2]]
    html = response.content.decode()
    assert "Makani Programme Dashboard 2026" in html and "Last data update" in html
    assert 'data-chart="dist"' in html and 'data-chart="donut"' in html and 'data-orientation="h"' in html
    assert 'icon="child"' not in html and "#i-child" in html  # the KPI tiles carry their icons
    assert 'aria-current="page"' in html and "?tab=health" in html


def test_makani_slicers_offer_the_values_counted(client_viewer, makani):
    slicers = {s["slicer"].name: s["options"] for s in page(client_viewer, "makani").context["slicers"]}
    assert list(slicers) == [
        "disability", "service", "nationality", "gender", "caregiver", "working", "partner", "governorate",
        "center", "emergency",
    ]  # fmt: skip
    assert slicers["service"] == [
        ("BLN", "BLN"),
        ("Summer RS", "Summer RS"),
        ("YFS - RS", "YFS - RS"),
        ("none", "Not specified"),
    ]
    assert slicers["disability"] == [
        ("3", "Difficulty hearing"),
        ("6", "Difficulty seeing"),
        ("1", "No"),
        ("none", "Not specified"),
    ]
    assert slicers["caregiver"] == [("Mother", "Mother"), ("Father", "Father"), ("Other", "Other")]
    assert slicers["working"] == [("Yes", "Yes"), ("No", "No"), ("Not specified", "Not specified")]
    assert slicers["emergency"] == [("Yes", "Yes"), ("No", "No"), ("Not specified", "Not specified")]
    assert slicers["partner"] == [("6", "Another NGO"), ("5", "MP")]


@pytest.mark.parametrize(
    ("params", "children", "unique", "staff"),
    [
        ({"partner": "5"}, 3, 3, 3),
        ({"center": "101"}, 2, 2, 1),
        ({"partner": "5", "governorate": "20"}, 2, 2, 2),
        ({"emergency": "Yes"}, 2, None, 2),  # the block has no emergency grouping
        ({"gender": "Female"}, 3, None, 4),  # staff: place filters only
        ({"service": "BLN"}, 2, None, 4),
        ({"disability": "none"}, 1, None, 4),  # "Not specified": unknown disability
        ({"partner": "999"}, 5, 4, 4),  # not an option: ignored
    ],
)
def test_makani_kpis_under_filters(client_viewer, makani, params, children, unique, staff):
    response = page(client_viewer, "makani", **params)
    kpis = response.context["data"]["kpis"]
    assert (kpis["children"], kpis["unique"], kpis["staff"]) == (children, unique, staff)
    if unique is None:
        assert "Shown with partner, governorate or center filters only" in response.content.decode()


def test_makani_education_and_health(client_viewer, makani):
    charts = page(client_viewer, "makani", tab="education").context["data"]["charts"]
    assert charts["programme_level"] == [["BLN Level 1", 1], ["BLN Level 2", 1], ["Summer RS Grade 3", 1],
                                         ["YFS Level 1 - RS Grade 9", 1]]  # fmt: skip
    assert charts["id_type"] == [
        ["UNHCR Registered", 2],
        ["Caregiver has no ID", 1],
        ["Lebanese national ID", 1],
        ["Not specified", 1],
    ]
    assert [label for label, _n in charts["education_status"]] == [
        "Currently registered in Formal Education school",
        "Never registered in any formal school before",
    ]
    data = page(client_viewer, "makani", tab="health").context["data"]
    assert {k: data["kpis"][k] for k in ("immunized", "screened", "minimum_meals", "vaccinated")} == {
        "immunized": 1,
        "screened": 3,
        "minimum_meals": 1,
        "vaccinated": 2,
    }
    assert data["charts"]["malnutrition"] == [
        ["MAM (MUAC >11.5 and <12.5 cm)", 1],
        ["No malnutrition", 1],
        ["SAM with Bilateral pitting oedema (both feet puffy)", 1],
    ]
    assert data["charts"]["delays"] == [
        ["Language/Communication", 1],
        ["Social/Emotional", 1],
    ]  # "No" left out


def test_makani_maps(client_viewer, makani, polygons):
    response = page(client_viewer, "makani", tab="maps")
    data = response.context["data"]
    areas = {f["id"]: f["properties"] for f in data["choropleth"]["geojson"]["features"]}
    assert areas["AKK"]["value"] == 2 and areas["BEK"]["value"] == 1 and "value" not in areas["NAB"]
    assert "values" not in data["choropleth"]
    points = data["points"]["points"]
    assert [p["name"] for p in points] == ["Beirut center", "Halba center"]  # Hermel has no GPS point
    assert points[1]["lines"][0] == ["Partner", "MP"] and points[1]["lines"][2] == ["Children", "2"]
    assert {e["label"] for e in data["points"]["legend"]} == {"Akkar", "Beirut"}
    assert data["unlocated"] == 1 and len(data["center_rows"]) == 3
    html = response.content.decode()
    assert html.count('data-module="edumap"') == 2 and 'id="edu-map-governorates"' in html
    assert "Hermel center" in html and "no GPS point" in html


# ----------------------------------------------------------------------------------------- Dirasa
def test_dirasa_overview(client_viewer, dirasa):
    response = page(client_viewer, "dirasa")
    data = response.context["data"]
    assert data["kpis"] == {
        "schools": 3,
        "children": 5,
        "in_school": 650,  # a school that did not report is left out
        "cwd": 2,
        "in_school_cwd": 12,
        "teachers": 4,
    }
    charts = data["charts"]
    assert charts["nationality"] == [["Syrian", 2], ["Lebanese", 1], ["Non-Lebanese", 2]]
    assert charts["gender"] == [["Female", 3], ["Male", 2]]
    assert charts["school_types"] == [["Private Free School", 1], ["Private School", 1], ["Not specified", 1]]
    assert charts["in_school_nationality"] == [["Lebanese children", 450], ["Non-Lebanese children", 200]]
    assert charts["governorate"] == [["Akkar", 3], ["Beirut", 1], ["El Nabatieh", 1]]
    assert charts["trainings"] == [["SEL", 2], ["Inclusion", 1], ["PSEA", 1]]
    html = response.content.decode()
    assert "Dirasa Programme Dashboard Bridging 2025-2026" in html and 'data-chart="share-bar"' in html
    assert 'name="outreach_year"' not in html  # the outreach tab has its own slicers


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"type": "Private School"}, {"schools": 1, "children": 2, "in_school": 400, "teachers": 2}),
        # a school's governorate chooses the schools: child 14 lives in Akkar but attends a Beirut school
        ({"governorate": "20"}, {"schools": 1, "children": 2, "in_school": 400, "teachers": 2}),
        ({"partner": "6"}, {"schools": 2, "children": 2, "in_school": 250, "teachers": 2}),
        ({"gender": "Female"}, {"schools": 2, "children": 3, "in_school": 650, "teachers": 4}),
        ({"emergency": "Not specified"}, {"schools": 1, "children": 1, "in_school": None, "teachers": 1}),
    ],
)
def test_dirasa_school_and_child_filters(client_viewer, dirasa, params, expected):
    kpis = page(client_viewer, "dirasa", **params).context["data"]["kpis"]
    assert {k: kpis[k] for k in expected} == expected


def test_dirasa_barriers(client_viewer, dirasa):
    data = page(client_viewer, "dirasa", tab="barriers").context["data"]
    assert data["kpis"] == {"children": 5, "with_barrier": 4}
    assert data["charts"]["barriers"] == [["Sickness", 2], ["Family moved", 1], ["Seasonal work", 1]]


def test_dirasa_outreach(client_viewer, dirasa):
    response = page(client_viewer, "dirasa", tab="outreach")
    data = response.context["data"]
    assert data["kpis"] == {"outreached": 2, "year": "2025"}  # the latest year by default
    charts = data["charts"]
    assert charts["education_status"] == [["Never engaged in any type of learning", 2]]
    assert charts["referral"] == [["Multi-service community center (Makani)", 1], ["Referred to Dirasa", 1]]
    assert charts["id_type"] == [["UNHCR registered", 2]]
    assert charts["dropout_reason"] == [["Recently moved from Syria", 1]]
    slicers = {s["slicer"].name: s for s in response.context["slicers"]}
    assert list(slicers) == ["outreach_year", "outreach_partner", "outreach_governorate"]
    assert slicers["outreach_year"]["options"] == [("2025", "2025"), ("2024", "2024"), ("all", "All years")]
    assert slicers["outreach_partner"]["options"] == [
        ("Dirasa Partner", "Dirasa Partner"),
        ("Other", "Other"),
        ("none", "Not specified"),
    ]
    every_year = page(client_viewer, "dirasa", tab="outreach", outreach_year="all")
    assert every_year.context["data"]["kpis"]["outreached"] == 4
    other = page(client_viewer, "dirasa", tab="outreach", outreach_year="2024", outreach_partner="Other")
    assert other.context["data"]["charts"]["id_type"] == [["No papers", 1]]
    assert "shared by every programme" in response.content.decode()


def test_dirasa_map(client_viewer, dirasa):
    data = page(client_viewer, "dirasa", tab="map").context["data"]
    assert [p["name"] for p in data["points"]["points"]] == ["Akkar private school", "Beirut free school"]
    assert data["points"]["legend"] == [
        {"label": "Private Free School", "color": "var(--nd-cat-1)"},
        {"label": "Private School", "color": "var(--nd-cat-2)"},
    ]
    assert [(r["name"], r["children"], r["in_school"]) for r in data["school_rows"]] == [
        ("Akkar private school", 2, 400),
        ("Beirut free school", 2, 250),
        ("Nabatieh school", 1, None),
    ]
    beirut = page(client_viewer, "dirasa", tab="map", partner="6").context["data"]
    assert [r["name"] for r in beirut["school_rows"]] == ["Beirut free school", "Nabatieh school"]
    assert beirut["school_rows"][0]["children"] == 1  # the partner's own registrations there


def test_child_filters_are_kept_but_not_applied_on_the_map(client_viewer, dirasa):
    response = page(client_viewer, "dirasa", tab="map", gender="Male")
    assert response.context["filters"] == {} and ("gender", "Male") in response.context["kept"]
    html = response.content.decode()
    assert 'name="gender" value="Male" data-keep' in html
    assert "gender=Male" in html  # the tab links carry it back
    assert "Not applied on this tab (kept for the other tabs): Gender." in html  # and the page says so


def test_the_page_names_the_filters_it_does_not_apply(client_viewer, dirasa):
    outreach = page(client_viewer, "dirasa", tab="outreach", school="300", disability="none", partner="999")
    assert outreach.context["not_applied"] == ["CWD type", "School"]  # an unknown partner is no filter
    # the outreach slicers filter data of their own: they are not missing from the other tabs
    overview = page(client_viewer, "dirasa", outreach_year="2024", outreach_partner="Other")
    assert overview.context["not_applied"] == []
    assert "Not applied on this tab" not in overview.content.decode()
    assert ("outreach_year", "2024") in overview.context["kept"]


def test_a_partner_not_specified_chooses_the_schools_without_partners(client_viewer):
    unlisted = {"child": 16, "school": 303, "partner": None, "governorate": 22, "sex": "Male"}
    registrations = BRIDGING + [unlisted]
    schools = SCHOOLS + [dict(SCHOOLS[1], id=303, name="Unlisted school", listed=[])]
    store(dirasa_payload(registrations, TEACHERS + [{"school": 303, "sex": "Male"}], schools=schools))
    kpis = page(client_viewer, "dirasa", partner="none").context["data"]["kpis"]
    assert {k: kpis[k] for k in ("schools", "children", "in_school", "teachers")} == {
        "schools": 1,
        "children": 1,
        "in_school": 250,
        "teachers": 1,
    }


# ------------------------------------------------------------------------ partial, cache, formats
def test_the_slicer_bar_swaps_the_results_only(client_viewer, makani):
    url = reverse("education:makani")
    response = client_viewer.get(
        url, {"tab": "overview", "gender": "Male"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="edu-results"
    )
    html = response.content.decode()
    assert [t.name for t in response.templates][0] == "education/partials/results.html"
    assert "<html" not in html and 'id="edu-filters"' not in html
    assert 'id="edu-tabs" aria-label="Dashboard pages" hx-swap-oob="true"' in html and "gender=Male" in html
    assert 'id="edu-chart-data"' in html and response.context["data"]["kpis"]["children"] == 2
    # a new year replaces the whole page (the period form targets #edu-page)
    full = client_viewer.get(url, {"year": "2026"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="edu-page")
    assert [t.name for t in full.templates][0] == "education/page.html"


@pytest.mark.parametrize(
    ("name", "tab", "params"),
    [
        ("makani", "overview", {"partner": "5"}),
        ("makani", "education", {"service": "BLN"}),
        ("makani", "health", {"working": "Yes"}),
        ("makani", "maps", {"emergency": "Yes"}),
        ("dirasa", "overview", {"type": "Private School"}),
        ("dirasa", "outreach", {"outreach_year": "2024"}),
        ("dirasa", "barriers", {"gender": "Female"}),
        ("dirasa", "map", {"partner": "7"}),
    ],
)
def test_the_partial_counts_as_the_full_page(client_viewer, makani, dirasa, polygons, name, tab, params):
    full = page(client_viewer, name, tab=tab, **params)
    cache.clear()  # each counts on its own
    url, headers = reverse(f"education:{name}"), {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "edu-results"}
    partial = client_viewer.get(url, {"tab": tab, **params}, **headers)
    assert [t.name for t in partial.templates][0] == "education/partials/results.html"
    assert partial.context["data"] == full.context["data"]
    assert partial.context["filters"] == full.context["filters"] != {}  # the filter was applied


def test_a_cached_tab_does_not_read_the_payload_again(client_viewer, makani, django_assert_max_num_queries):
    page(client_viewer, "makani", gender="Female")
    with django_assert_max_num_queries(12) as queries:
        page(client_viewer, "makani", gender="Female")
    whole_payload = re.compile(r'"payload"\s*(,|FROM)')  # the stored years' rounds are read, not the rest
    assert not any(whole_payload.search(q["sql"]) for q in queries.captured_queries)


def test_results_are_cached_per_fetch(client_viewer, makani):
    assert page(client_viewer, "makani").context["data"]["kpis"]["children"] == 5
    makani.payload = makani_payload(REGISTRATIONS[:2], STAFF)
    makani.save(update_fields=["payload"])
    assert page(client_viewer, "makani").context["data"]["kpis"]["children"] == 5  # from the cache
    makani.fetched_at = timezone.now() + datetime.timedelta(seconds=1)
    makani.save(update_fields=["fetched_at"])
    assert page(client_viewer, "makani").context["data"]["kpis"]["children"] == 2
    cache.clear()


def test_years_and_rounds(client_viewer, makani):
    store(makani_payload(REGISTRATIONS[:1], year="2025"))
    response = page(client_viewer, "makani")
    assert response.context["periods"] == ["2026", "2025"] and response.context["period"] == "2026"
    older = page(client_viewer, "makani", year="2025")
    assert older.context["data"]["kpis"]["children"] == 1
    assert "Makani Programme Dashboard 2025" in older.content.decode()
    store(dirasa_payload(BRIDGING[:1], round_name="Bridging 2024-2025", start_date="2024-10-01"))
    store(dirasa_payload(BRIDGING, round_name="Bridging 2025-2026"))
    assert page(client_viewer, "dirasa").context["periods"] == ["Bridging 2025-2026", "Bridging 2024-2025"]
    assert page(client_viewer, "dirasa", round="Bridging 2024-2025").context["data"]["kpis"]["children"] == 1


def test_a_part_compiler_could_not_count_is_named(client_viewer):
    payload = makani_payload(REGISTRATIONS, STAFF)
    payload["cubes"]["staff"] = {
        "dims": ["center"],
        "measures": ["staff"],
        "rows": [],
        "error": "QueryCanceled",
    }
    payload["blocks"]["registrations"] = {"error": "QueryCanceled"}
    store(payload)
    response = page(client_viewer, "makani", partner="5")
    kpis = response.context["data"]["kpis"]
    assert (kpis["children"], kpis["staff"], kpis["unique"]) == (3, 0, None)  # the others still count
    assert response.context["data"]["errors"] == ["staff", "unique count"]
    assert "Compiler could not count some parts this time (they show as empty): staff, unique count." in (
        response.content.decode()
    )


def test_older_format_shows_an_empty_state(client_viewer):
    store(format_one(makani_payload(REGISTRATIONS)))
    response = page(client_viewer, "makani")
    assert response.context["old_format"] is True
    html = response.content.decode()
    assert "Compiler sends the older format" in html
    assert "Run the education sync after Compiler is updated" in html
    assert 'id="edu-filters"' not in html


def test_pages_without_figures(client_viewer):
    for name in ("makani", "dirasa"):
        response = page(client_viewer, name)
        assert b"Compiler is not connected" in response.content


def test_every_tab_renders(client_viewer, makani, dirasa, polygons):
    for name, tabs in (("makani", ("overview", "education", "health", "maps")),
                       ("dirasa", ("overview", "outreach", "barriers", "map"))):  # fmt: skip
        for tab in tabs:
            response = page(client_viewer, name, tab=tab)
            assert response.context["tab"] == tab
            label = dict(response.context["page"].tabs)[tab]
            assert f'aria-current="page">{label}' in response.content.decode()
    assert page(client_viewer, "makani", tab="nope").context["tab"] == "overview"


def test_sidebar_links_both_dashboards(client_viewer, makani):
    html = page(client_viewer, "makani").content.decode()
    assert "Makani (MSCC)" in html and "Dirasa (Bridging)" in html and reverse("education:dirasa") in html
