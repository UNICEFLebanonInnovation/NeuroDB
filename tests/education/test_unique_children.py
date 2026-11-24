"""The unique count of children answers every combination of filters Compiler's registrations block groups by
(Compiler's figures/education.py, Makani and Bridging: partner and governorate, alone or together, each alone or
with one other split, plus a few pairs such as nationality and sex), not only the place filters: the children
cards show each child once for more combinations, e.g. a partner's boys. A filter the block has no dimension for
(the education service, a centre active during an emergency, Dirasa's school governorate), a combination Compiler
does not group by or a filter "Not specified" still has none, and the registrations are unchanged. The payloads
are built here as Compiler builds them (tests/education/payloads.py); the partners are invented."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from .payloads import dirasa_payload, format_three, makani_payload
from .test_dashboards import BRIDGING, TEACHERS, store

pytestmark = pytest.mark.django_db

PARTNERS = [
    {"id": 5, "name": "Example Partner One", "short_name": ""},
    {"id": 6, "name": "Example Partner Two", "short_name": ""},
    {"id": 7, "name": "Example Partner Three", "short_name": ""},
]
# Partner One runs centres 100 (Akkar) and 102, Partner Two centre 101 (Beirut): boy 1 is registered at both of
# Partner One's centres
MAKANI = [
    {"child": 1, "center": 100, "sex": "Male", "nationality": 1, "disability": 1, "programme": "BLN Level 1"},
    {"child": 1, "center": 102, "sex": "Male", "nationality": 1, "disability": 1, "programme": "BLN Level 2"},
    {"child": 2, "center": 100, "sex": "Male", "nationality": 5, "disability": 3, "programme": "BLN Level 1"},
    {"child": 3, "center": 100, "sex": "Female", "nationality": 1, "disability": 1},
    {"child": 4, "center": 101, "sex": "Male", "nationality": 1, "disability": 6},
    {"child": 5, "center": 102, "sex": "Female", "nationality": 4},
    {"child": 6, "center": 101, "sex": "Female", "nationality": 1, "disability": 1, "programme": "BLN"},
]
NO_UNIQUE = "Not counted by Compiler for these filters"
TWICE = {"child": 11, "school": 301, "partner": 7, "governorate": 20, "sex": "Female", "nationality": 1}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def page(client, name, **params):
    response = client.get(reverse(f"education:{name}"), params)
    assert response.status_code == 200
    return response


@pytest.mark.parametrize(
    ("params", "children", "unique"),
    [
        ({"partner": "5", "gender": "Male"}, 3, 2),  # Compiler's (partner, sex): boy 1 once
        ({"gender": "Male"}, 4, 3),  # (sex)
        ({"partner": "5", "governorate": "20", "gender": "Male"}, 2, 2),  # (governorate, partner, sex)
        ({"nationality": "1", "gender": "Male"}, 3, 2),  # the extra pair (nationality, sex)
        ({"disability": "1", "gender": "Male"}, 2, 1),  # the extra pair (disability, sex)
        ({"partner": "5", "center": "102"}, 2, 2),  # the place filters, as before
        ({"partner": "5", "nationality": "1", "gender": "Male"}, 2, None),  # no such grouping
        ({"center": "102", "gender": "Male"}, 1, None),  # two splits together: no grouping
        ({"service": "BLN"}, 4, None),  # worked out from the programme: the block has no service
        ({"emergency": "Yes"}, 3, None),  # the centre's emergency status: not a dimension of the block
        ({"disability": "none"}, 1, None),  # "Not specified"
    ],
)
def test_makani_unique_count_answers_every_grouping_compiler_sends(client_viewer, params, children, unique):
    store(makani_payload(MAKANI, lists={"partners": PARTNERS}))
    response = page(client_viewer, "makani", **params)
    data = response.context["data"]
    assert (data["kpis"]["children"], data["kpis"]["unique"]) == (children, unique)
    # the children card holds both counts on every tab, for Ask NeuroDB
    card = data["cards"]["children"]
    assert (card["registrations"], card["unique"]) == (children, unique)
    html = response.content.decode()
    assert (NO_UNIQUE in html) is (unique is None) and ("Each child once" in html) is (unique is not None)
    health = page(client_viewer, "makani", tab="health", **params).context["data"]["cards"]["children"]
    assert (health["registrations"], health["unique"]) == (children, unique)


def test_the_unique_count_is_the_blocks_own_number_never_worked_out(client_viewer):
    payload = makani_payload(MAKANI, lists={"partners": PARTNERS})
    grouping = next(g for g in payload["blocks"]["registrations"]["figures"] if g["by"] == ["partner", "sex"])
    grouping["rows"] = [row[:2] + [7] if row[:2] == [5, "Male"] else row for row in grouping["rows"]]
    store(payload)
    assert page(client_viewer, "makani", partner="5", gender="Male").context["data"]["kpis"]["unique"] == 7


def test_without_the_grouping_there_is_no_unique_count(client_viewer):
    payload = makani_payload(MAKANI, lists={"partners": PARTNERS})
    figures = payload["blocks"]["registrations"]["figures"]
    payload["blocks"]["registrations"]["figures"] = [g for g in figures if g["by"] != ["partner", "sex"]]
    store(payload)
    response = page(client_viewer, "makani", partner="5", gender="Male")
    kpis = response.context["data"]["kpis"]
    assert (kpis["children"], kpis["unique"]) == (3, None)
    assert NO_UNIQUE in response.content.decode()


@pytest.mark.parametrize(
    ("params", "value", "variant"),
    [
        ({"nationality": "1"}, 2, "unique"),  # 3 registrations: child 11 at two schools
        ({"nationality": "1", "gender": "Female"}, 2, "unique"),  # the extra pair (nationality, sex)
        ({"partner": "7", "nationality": "1"}, 2, "unique"),  # (nationality, partner)
        ({"disability": "1", "gender": "Male"}, 1, "unique"),  # the extra pair (disability, sex)
        ({"partner": "7", "nationality": "1", "gender": "Female"}, 3, "registrations"),  # no such grouping
        # the school's governorate chooses the schools; the block's governorate is the child's: no unique count
        ({"governorate": "20"}, 2, "registrations"),
        ({"type": "Private School"}, 2, "registrations"),  # the school's type: not a dimension of the block
    ],
)
def test_dirasa_children_card_counts_each_child_once_by_more_filters(client_viewer, params, value, variant):
    store(format_three(dirasa_payload(BRIDGING + [TWICE], TEACHERS, lists={"partners": PARTNERS})))
    card = page(client_viewer, "dirasa", **params).context["data"]["cards"]["children"]
    assert (card["value"], card["variant"], card["fallback"]) == (value, variant, variant != "unique")
    assert card["unique"] == (value if variant == "unique" else None)
