"""The panels a Monitoring insights tab loads when they are reached or opened, answered alone (step 3's quick
win; the Analysis and Quality tabs stay one request).

The Analysis tab's rule score trends and entity table load when they scroll into view (``hx-trigger="revealed"``),
the entity chips and "Show all" lists (every rule, a section's records, every place; the Quality tab's places)
when chosen or opened. Each asks the page's address with its target in ``HX-Target`` and keeps only its panel
(``hx-select``). Until step 3 the server drew the whole tab for each (the entity table 492-533 ms on large data,
of which 355 ms its own figures); now it draws the panel alone, from the same helpers and the same template the
tab includes. These tests pin that the panel answered alone is the panel the whole tab draws, under several
filters, and that nothing else of the tab is worked out for it.

On ``fm_world`` built on 5 October 2026 (``built``), with fewer rows shown before "Show all" (``PLACES_TOP`` 2,
``SECTION_TOP`` 1), so that every list has rows to show.
"""

from __future__ import annotations

import datetime
import re

import pytest
from django.core.cache import cache, caches

from neurodb.fmm import metrics, views
from neurodb.fmm import scope as scope_module
from tests.web.parts import _element_end, normalise, raw_get, text

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
PAGE = "/fmm/"
RESULTS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "fmm-results"}
# (tab, what the panel's address adds, its target): as the tab's links and loaders ask for them
PANELS = [
    ("analysis", {"rule_trends": "1"}, "fmm-rule-trends-panel"),
    ("analysis", {"entity_kind": "all"}, "fmm-entities"),
    ("analysis", {"entity_kind": "pd"}, "fmm-entities"),
    ("analysis", {"entity_kind": "partner", "entity_all": "1"}, "fmm-entities"),
    ("analysis", {"rules": "all"}, "fmm-rules-all"),
    ("analysis", {"sections": "all"}, "fmm-section-rest-1"),
    ("analysis", {"places": "all"}, "fmm-frequency-rest"),
    ("quality", {"places": "all"}, "fmm-places-rest"),
]
FILTERS = [{"section": ""}, {"section": "Education"}, {"section": "", "governorate": "Bekaa"}]
# Query ceilings, measured on this world plus 2: the request's floor, the reference counts and stamp of the
# address, then the panel's own figures with nothing stored (the whole tab took 24-26 for each until step 3)
PANEL_QUERIES = {
    "fmm-rule-trends-panel": 10,
    "fmm-entities": 14,
    "fmm-rules-all": 11,
    "fmm-section-rest-1": 13,
    "fmm-frequency-rest": 12,
    "fmm-places-rest": 12,
}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


@pytest.fixture(autouse=True)
def _short_lists(monkeypatch):
    """Fewer rows before "Show all", so the world's places and sections have a rest to show."""
    monkeypatch.setattr(views, "PLACES_TOP", 2)
    monkeypatch.setattr(views, "SECTION_TOP", 1)


def _forget() -> None:
    cache.clear()
    caches["shared"].clear()


def _element(html: str, element_id: str) -> str:
    match = re.search(rf'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*\sid="{re.escape(element_id)}"[^>]*>', html)
    assert match, f"no #{element_id}"
    return html[match.start() : _element_end(html, match.start(), match["tag"])]


@pytest.mark.parametrize("filters", FILTERS, ids=["every-section", "education", "bekaa"])
@pytest.mark.parametrize(("tab", "extra", "target"), PANELS, ids=[f"{p[2]}-{'-'.join(p[1])}" for p in PANELS])
def test_the_panel_answered_alone_is_the_panel_of_the_whole_tab(
    built, client_viewer, tab, extra, target, filters
):
    params = {**filters, "tab": tab, **extra}
    # the tab, as the browser asked for it until step 3 (it kept the panel only)
    whole = text(raw_get(client_viewer, PAGE, params, **RESULTS))
    # a list with nothing past its first rows under this filter is not offered (every section has a rest of each)
    if f'id="{target}"' not in whole and filters != {"section": ""}:
        assert f'"#{target}"' not in whole
        return
    alone = raw_get(client_viewer, PAGE, params, HTTP_HX_REQUEST="true", HTTP_HX_TARGET=target)
    assert alone.status_code == 200
    body = text(alone)
    assert "<html" not in body and 'id="fmm-results"' not in body and "fmm-kpis" not in body
    assert normalise(body) == normalise(_element(whole, target))
    # the browser keeps the element its hx-select names: the answer is that element and nothing else
    assert normalise(_element(body, target)) == normalise(body)


def test_the_loaders_and_lists_still_select_their_panel(built, client_viewer):
    """A page opened before step 3 asks the same addresses and keeps the element its ``hx-select`` names: the
    links keep it, and the answer alone holds that element."""
    html = text(raw_get(client_viewer, PAGE, {"section": "", "tab": "analysis"}, **RESULTS))
    for target in ("fmm-rule-trends-panel", "fmm-entities", "fmm-rules-all", "fmm-frequency-rest"):
        assert f'hx-select="#{target}"' in html, target
    assert 'hx-select="#fmm-section-rest-1"' in html
    quality = text(raw_get(client_viewer, PAGE, {"section": "", "tab": "quality"}, **RESULTS))
    assert 'hx-select="#fmm-places-rest"' in quality


@pytest.mark.parametrize(("tab", "extra", "target"), PANELS, ids=[p[2] for p in PANELS])
def test_a_panel_works_out_its_own_figures_only(built, client_viewer, monkeypatch, tab, extra, target):
    """The rest of the tab is not worked out for a panel: the blocks it does not show raise."""
    needed = {
        "fmm-rule-trends-panel": {"rule_trends"},
        "fmm-entities": {"entities_performance", "entity_kinds", "kpis"},
        "fmm-rules-all": {"rule_analysis"},
        "fmm-section-rest-1": {"sections", "section_line_names"},
        "fmm-frequency-rest": {"locations"},
        "fmm-places-rest": {"locations"},
    }[target]

    def refuse(*args, **kwargs):
        raise AssertionError("a panel worked out the whole tab")

    for name in (
        "score_buckets",
        "highlights",
        "offices",
        "flag_frequency",
        "flag_distribution",
        "action_points",
        "hact_programmatic",
        "governorate_gaps",
        "office_rule_badges",
        "quality_by_rating",
        "dimension_breakdown",
        "monthly_quality",
        "monthly_volume",
        "top_issues",
        "issues_summary",
        "notes",
        "rule_trends",
        "rule_analysis",
        "entities_performance",
        "sections",
        "locations",
    ):
        if name not in needed:
            monkeypatch.setattr(metrics, name, refuse)
    response = raw_get(
        client_viewer,
        PAGE,
        {"section": "", "tab": tab, **extra},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET=target,
    )
    assert response.status_code == 200 and f'id="{target}"' in text(response)


@pytest.mark.parametrize(("tab", "extra", "target"), PANELS, ids=[p[2] for p in PANELS])
def test_each_panel_stays_within_its_query_ceiling(
    built, client_viewer, django_assert_max_num_queries, tab, extra, target
):
    _forget()
    with django_assert_max_num_queries(PANEL_QUERIES[target]):
        response = raw_get(
            client_viewer,
            PAGE,
            {"section": "", "tab": tab, **extra},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET=target,
        )
    assert response.status_code == 200


def test_a_section_list_beyond_the_sections_is_404(built, client_viewer):
    response = raw_get(
        client_viewer,
        PAGE,
        {"section": "", "tab": "analysis", "sections": "all"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="fmm-section-rest-99",
    )
    assert response.status_code == 404


def test_other_targets_still_get_the_tab(built, client_viewer):
    """Only the panels' own targets are answered alone: the results area (tab bar, filter bar) and a panel id
    on another tab get the tab, as before."""
    html = text(
        raw_get(
            client_viewer,
            PAGE,
            {"section": "", "tab": "quality"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="fmm-entities",
        )
    )
    assert "Top recurring issues" in html and 'name="tab" value="quality"' in html
    html = text(
        raw_get(client_viewer, PAGE, {"section": "", "tab": "analysis", "entity_kind": "pd"}, **RESULTS)
    )
    assert "Programmatic visits and HACT" in html and 'id="fmm-entities"' in html
