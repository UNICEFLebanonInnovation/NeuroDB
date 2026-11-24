"""Ask NeuroDB's education_figures takes the Makani and Dirasa pages' filters: a filter by its query name or its
label, a choice by its label or its value (case ignored; an exact name first, then a unique part of a name). The
figures are the page's own under the same query, with the registrations and the unique children said apart, the
filters applied echoed, and every list cut at 40 rows giving its real length (every row is stored: a filter
reaches it). A choice that does not match, or matches several, an unknown filter, a filter of another tab and a
round the round argument contradicts are refused with the choices. Counts only: nothing about a child. The
payloads are built as Compiler builds them (tests/education/payloads.py); the partners and centres are
invented."""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from neurodb.assistant import agent, tools
from neurodb.assistant.hub_tools import HUB_TOOLS, education_figures
from neurodb.assistant.tools import ToolInputError
from tests.education.payloads import CENTERS, dirasa_year_payload, makani_payload
from tests.education.test_dashboards import OUTREACH, store
from tests.education.test_dirasa_year import R2, YEAR_REGISTRATIONS, YEAR_TEACHERS
from tests.education.test_unique_children import MAKANI, PARTNERS

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def makani(db):
    return store(makani_payload(MAKANI, lists={"partners": PARTNERS}))


@pytest.fixture
def dirasa(db):
    payload = dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, OUTREACH, lists={"partners": PARTNERS})
    return store(payload)


def shown(client, name, **params):
    """The page's figures under the same query, as the tool returns them (JSON-safe)."""
    response = client.get(reverse(f"education:{name}"), params)
    assert response.status_code == 200
    return tools._clean(response.context["data"])


def keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(keys(v) for v in obj))
    return set()


# --------------------------------------------------------------------------------------- the page's figures
def test_a_partners_boys_are_the_pages_figures_with_both_counts_explained(client_viewer, makani):
    answer = education_figures("makani", filters={"Partner": "example partner one", "child gender": "MALE"})
    assert answer["figures"] == shown(client_viewer, "makani", partner="5", gender="Male")
    assert answer["filters_applied"] == {"Partner": "Example Partner One", "Child gender": "Male"}
    # boy 1 is registered at two of the partner's centres: 3 registrations, 2 children
    assert answer["children"] == {"registrations": 3, "unique_children": 2}
    note = answer["children_note"]
    assert "counts twice" in note and "each child once" in note and "any combination" in note
    assert answer["figures"]["kpis"]["unique"] == 2  # Compiler's (partner, sex) grouping, as the page's card
    # counts only: no child, no cube or block of the payload
    assert not {"child", "person", "cubes", "blocks"} & keys(answer)
    assert answer["filters_not_applied"] is None


def test_a_combination_compiler_does_not_count_gives_the_registrations(makani):
    boys = {"partner": "one", "nationality": "syrian", "gender": "male"}  # no (nationality, partner, sex)
    answer = education_figures("makani", filters=boys)
    assert answer["children"] == {"registrations": 2, "unique_children": None}
    assert "give the registrations and say they are registrations" in answer["children_note"]
    service = education_figures("makani", tab="health", filters={"Education services": "bln"})
    assert service["children"] == {"registrations": 4, "unique_children": None}


def test_names_and_ids_resolve_whatever_their_case(makani):
    by_name = education_figures("makani", filters={"partner": "EXAMPLE PARTNER ONE", "Center": "halba"})
    by_id = education_figures("makani", filters={"PARTNER": 5, "center": "100"})
    assert by_name["figures"] == by_id["figures"]
    applied = {"Partner": "Example Partner One", "Center": "Halba center"}
    assert by_name["filters_applied"] == by_id["filters_applied"] == applied
    # through the assistant's own run, whose check lets an object of filters through
    asked = {"programme": "makani", "filters": {"cwd type": "difficulty hearing"}}
    hearing = tools.run("education_figures", asked)
    assert hearing["filters_applied"] == {"CWD type": "Difficulty hearing"}
    # "Not specified" where the page offers it, by its label or its value
    for blank in ("not specified", "none"):
        answer = education_figures("makani", filters={"disability": blank})
        assert answer["filters_applied"] == {"CWD type": "Not specified"}
        assert answer["children"] == {"registrations": 1, "unique_children": None}


# ------------------------------------------------------------------------------------------- refused, with choices
def test_a_choice_that_does_not_match_or_matches_several_lists_the_choices(makani):
    with pytest.raises(ToolInputError, match="No Partner choice matches 'Nowhere'") as none:
        education_figures("makani", filters={"partner": "Nowhere"})
    assert "Example Partner One, Example Partner Two" in str(none.value)
    with pytest.raises(ToolInputError, match="2 Partner choices match 'example partner'") as several:
        education_figures("makani", filters={"partner": "example partner"})
    assert "Example Partner One, Example Partner Two" in str(several.value)
    with pytest.raises(ToolInputError, match="Unknown filter 'colour'") as unknown:
        education_figures("makani", filters={"colour": "red"})
    assert "partner (Partner)" in str(unknown.value) and "gender (Child gender)" in str(unknown.value)


def test_a_filter_of_another_tab_names_the_tabs_that_show_it(dirasa):
    with pytest.raises(ToolInputError, match="Gender is not a filter of the map tab") as other:
        education_figures("dirasa", tab="map", filters={"gender": "male"})
    assert "overview, barriers" in str(other.value)
    with pytest.raises(ToolInputError, match="School is not a filter of the outreach tab"):
        education_figures("dirasa", tab="outreach", filters={"school": "akkar"})
    # on Outreach, "partner" is the outreach's own Partner filter
    outreach = education_figures("dirasa", tab="outreach", filters={"partner": "dirasa partner"})
    assert outreach["filters_applied"] == {"Year": "2025", "Partner": "Dirasa Partner"}


def test_the_round_argument_and_a_round_filter_must_agree(dirasa):
    with pytest.raises(ToolInputError, match="disagree"):
        education_figures("dirasa", round="Bridging Round 1 2026", filters={"round": "round 2"})
    with pytest.raises(ToolInputError, match="disagree"):  # a round's name as the period chooses that round
        education_figures("dirasa", period="Bridging Round 1 2026", filters={"Round": str(R2)})
    same = education_figures("dirasa", round="Bridging Round 2 2026", filters={"round": "round 2"})
    assert same["round"] == "Bridging Round 2 2026"
    # the round argument still works alone, and the page's note on a filter not applied is kept
    outreach = education_figures("dirasa", tab="outreach", round="Bridging Round 2 2026")
    assert (outreach["round"], outreach["filters_not_applied"]) == (None, ["Round"])
    assert outreach["children"] is None  # Outreach counts Kobo interviews: no children card


def test_dirasa_by_year_takes_a_round_a_partner_and_a_gender(client_viewer, dirasa):
    answer = education_figures("dirasa", filters={"round": "Round 2", "partner": "three", "gender": "female"})
    assert answer["figures"] == shown(
        client_viewer, "dirasa", year="2026", round=str(R2), partner="7", gender="Female"
    )
    assert answer["round"] == "Bridging Round 2 2026"
    assert answer["filters_applied"] == {
        "Round": "Bridging Round 2 2026",
        "Gender": "Female",
        "Partner": "Example Partner Three",
    }
    # Compiler groups no round with a partner and a sex: the registrations, the card saying so
    assert answer["children"] == {"registrations": 1, "unique_children": None}
    assert answer["figures"]["cards"]["children"]["variant"] == "registrations"
    boys = education_figures("dirasa", round="Bridging Round 2 2026", filters={"gender": "Male"})
    assert boys["children"] == {"registrations": 1, "unique_children": 1}  # the (round, sex) grouping


# ---------------------------------------------------------------------------------------- lists cut at 40 rows
CENTRES = [dict(CENTERS[0], id=500 + n, name=f"Example Centre {n:02d}") for n in range(1, 46)]


def test_a_list_over_40_rows_gives_its_length_and_a_filter_reaches_every_row():
    store(
        makani_payload(
            [{"child": n, "center": 500 + n, "sex": "Male"} for n in range(1, 46)],
            centers=CENTRES,
            lists={"partners": PARTNERS},
        )
    )
    answer = education_figures("makani", tab="maps")
    figures = answer["figures"]
    assert len(figures["center_rows"]) == 40 and figures["center_rows"][-1]["name"] == "Example Centre 40"
    assert (figures["center_rows_total"], figures["center_rows_truncated"]) == (45, True)
    assert len(figures["points"]["points"]) == 40
    assert (figures["points"]["points_total"], figures["points"]["points_truncated"]) == (45, True)
    assert "more" not in json.dumps(figures["center_rows"])  # no "… 5 more" row
    assert "every row is stored" in answer["lists_note"] and "one centre" in answer["lists_note"]
    assert figures["kpis"]["children"] == 45  # every centre counted
    # the 41st row's figures, with a filter
    one = education_figures("makani", tab="maps", filters={"center": "example centre 41"})
    assert [(r["name"], r["children"]) for r in one["figures"]["center_rows"]] == [("Example Centre 41", 1)]
    assert "center_rows_total" not in one["figures"] and one["lists_note"] is None
    # 45 choices: the closest, how many, and to give a longer part of the name
    with pytest.raises(ToolInputError, match="6 Center choices match 'Example Centre 4'") as several:
        education_figures("makani", filters={"center": "Example Centre 4"})
    message = str(several.value)
    assert "45 choices" in message and "Example Centre 45" in message and "longer part of the name" in message
    assert "Example Centre 01" not in message
    with pytest.raises(ToolInputError, match="No Center choice matches 'Centre 99'") as none:
        education_figures("makani", filters={"center": "Centre 99"})
    assert "45 choices" in str(none.value) and "longer part of the name" in str(none.value)


# ------------------------------------------------------------------------------------- the tool and the prompt
def test_the_tool_and_the_prompt_say_the_filters_and_what_each_count_is():
    _function, description, schema, _label = HUB_TOOLS["education_figures"]
    assert schema["properties"]["filters"]["type"] == "object"
    assert "filters" in description and "registrations" in description and "unique children" in description
    line = next(line for line in agent.SYSTEM_PROMPT.splitlines() if "education_figures" in line)
    assert "filters" in line
