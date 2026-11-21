"""Monitoring insights without its Field office filter (the owner's decision of 21 Nov 2026: "Remove the filters
in all pages: office, emergency, output, partner type").

* The filter bar has no Field office control; its other filters are unchanged.
* The page builds no link with ``office``: the Analysis tab's *Quality by field office* and *Field offices*
  keep their rows and figures, without a link to an office's records.
* An address that still carries ``office`` (an old bookmark, a link of the page's own chat, whose look-ups keep
  their office argument) is applied, and named by a removable chip, as the page names every filter its bar
  does not show (a programme document, a drill-down).

On ``fm_world`` built on 5 October 2026 (conftest ``built``): visit 1722 is Zahle's, 1724, 1726 and the
reference visit Tripoli's.
"""

from __future__ import annotations

import datetime
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from neurodb.fmm import scope as scope_module

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
LINK = re.compile(r'[?&](?:amp;)?office=|name="office"')
CONTROL = re.compile(r'<select[^>]*\bname="office"|<label[^>]*\bfor="f-office"')
CHIP = re.compile(r'<span class="chip chip--removable">(.*?)</span>', re.S)
TABS = ("insights", "visits", "quality", "analysis", "map")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)


def _tab(client, tab: str, **params) -> str:
    query = {"year": "2026", "section": "", "tab": tab, **params}
    return client.get(PAGE, query, HTTP_HX_REQUEST="true").content.decode()


def test_the_filter_bar_has_no_field_office(built, client_viewer):
    html = client_viewer.get(PAGE).content.decode()
    assert 'id="fmm-filters"' in html
    assert not CONTROL.search(html)
    for name in ("period", "section", "governorate", "partner", "entity_type", "rating", "modality", "q"):
        assert f'name="{name}"' in html, name


@pytest.mark.parametrize("tab", TABS)
def test_no_tab_builds_a_link_with_an_office(built, client_viewer, tab):
    assert not LINK.search(_tab(client_viewer, tab)), tab
    assert not LINK.search(client_viewer.get(PAGE, {"tab": tab}).content.decode()), tab


def test_the_office_rows_keep_their_figures_without_a_link(built, client_viewer):
    html = _tab(client_viewer, "analysis")
    badges = html.split('id="fmm-office-rules-title"', 1)[1].split("</section>", 1)[0]
    assert 'class="fmm-office-row"' in badges and "Zahle" in badges and "<a " not in badges
    offices = html.split('id="fmm-offices-title"', 1)[1].split("</section>", 1)[0]
    assert "Zahle" in offices and "Tripoli" in offices and "/fmm/drill/" not in offices
    # the other blocks keep their links to the records behind a figure
    assert "/fmm/drill/?" in html


def test_an_old_address_with_an_office_is_applied_and_named_by_a_removable_chip(built, client_viewer):
    page = client_viewer.get(PAGE, {"year": "2026", "section": "", "office": "Zahle", "tab": "visits"})
    assert page.context["scope"].offices == ("Zahle",)
    keys = set(page.context["scope"].visits().values_list("key", flat=True))
    assert keys == {"1722"}
    html = page.content.decode()
    chips = [" ".join(unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in CHIP.findall(html)]
    assert any(c.startswith("Field office: Zahle") for c in chips), chips
    chip = next(c for c in CHIP.findall(html) if "Field office: Zahle" in c)
    href = unescape(re.search(r'href="([^"]+)"', chip).group(1))
    query = parse_qs(urlsplit(href).query, keep_blank_values=True)
    assert "office" not in query and query["year"] == ["2026"] and query["tab"] == ["visits"]
    assert not CONTROL.search(html)
    # the filter bar keeps it, so Apply does not drop it without a word
    assert '<input type="hidden" name="office" value="Zahle">' in html
    # removed, every office again
    every = client_viewer.get(href)
    assert every.context["scope"].offices == ()


def test_the_drill_window_of_an_old_link_names_the_office_once(built, client_viewer):
    html = client_viewer.get(
        reverse("fmm:drill"), {"year": "2026", "section": "", "office": "Zahle"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert html.count("Field office: Zahle") == 1
