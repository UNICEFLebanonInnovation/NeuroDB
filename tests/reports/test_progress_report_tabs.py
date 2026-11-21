"""Progress reports' tab bar: Reporting progress and Partner view only (the owner's decision of 21 Nov 2026).

* The *Partners* tab reads *Partner view* wherever a person reads it as the tab's name: the tab bar, the help
  guide, the figure trail's block and the Power BI comparison's words. Its address stays ``tab=partners``.
* *By location* (``tab=locations``) and *By disaggregation* (``tab=disaggregations``) are hidden: they are in no
  tab bar and no regular page links to them, but their addresses still open them (old bookmarks), with their
  data, and the figure trail and the admin's Power BI comparison still read them.

On the world of tests/reports/test_r4_views.py (``labelled``: one indicator's two progress reports at Akkar and
Bekaa, with their disaggregations).
"""

# ruff: noqa: F401, F811 (the fixtures come from the modules that define them)
from __future__ import annotations

import html as html_module
import re

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.datamart import powerbi_results, powerbi_words, trail
from tests.reports.test_r4_views import labelled
from tests.web.parts import HX, get_part, raw_get

pytestmark = pytest.mark.django_db

PAGE = "/partner-reporting/"  # reports:partner_reporting
SHOWN = ["Reporting progress", "Partner view"]
HIDDEN = ("locations", "disaggregations")
TAB_BAR = re.compile(
    r'<div class="segmented[^"]*" role="group" aria-label="Progress report views">(.*?)</div>', re.S
)
TAB_LINK = re.compile(r'<a href="([^"]*)"([^>]*)>(.*?)</a>', re.S)
TAGS = re.compile(r"<[^>]+>")
# any address on a page (a link, a form, an HTMX request or the address it pushes)
ADDRESS = re.compile(r'\s(?:href|action|hx-get|data-hx-get|hx-push-url|data-hx-push-url)="([^"]*)"')
HIDDEN_ADDRESS = re.compile(r"[?&]tab=(?:locations|disaggregations)(?:&|$)")


def _tabs(html: str) -> list[tuple[str, str, bool]]:
    """The tab bar's links: (label, address, current)."""
    bars = TAB_BAR.findall(html)
    assert len(bars) == 1, bars
    return [
        (" ".join(TAGS.sub("", label).split()), html_module.unescape(href), "aria-current" in attrs)
        for href, attrs, label in TAB_LINK.findall(bars[0])
    ]


def _validator() -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


# --------------------------------------------------------------------------------------------- the tab bar
@pytest.mark.parametrize("tab", ["", "reports", "partners", *HIDDEN])
def test_the_tab_bar_shows_reporting_progress_and_partner_view_only(client_viewer, labelled, tab):
    response = client_viewer.get(PAGE, {"tab": tab} if tab else {})
    assert response.status_code == 200
    tabs = _tabs(response.text)
    assert [label for label, _, _ in tabs] == SHOWN
    reports, partners = (href for _, href, _ in tabs)
    assert "tab=reports" in reports and "tab=partners" in partners  # the address of the tab is unchanged
    current = [label for label, _, on in tabs if on]
    assert current == {"": ["Reporting progress"], "reports": ["Reporting progress"]}.get(
        tab, ["Partner view"] if tab == "partners" else []
    )


@pytest.mark.parametrize("tab", ["reports", "partners"])
def test_a_change_of_filter_draws_the_same_two_tabs(client_viewer, labelled, tab):
    response = raw_get(client_viewer, PAGE, {"tab": tab}, **HX)
    assert response.status_code == 200
    assert [label for label, _, _ in _tabs(response.text)] == SHOWN


def test_the_page_offers_no_address_of_the_hidden_tabs(client_viewer, labelled):
    for tab in ("reports", "partners", *HIDDEN):
        context = client_viewer.get(PAGE, {"tab": tab}).context
        assert list(context["tab_urls"]) == ["reports", "partners"], tab


# ----------------------------------------------------------------------------------- the hidden tabs open
def test_by_location_still_opens_from_its_address(client_viewer, labelled):
    response = client_viewer.get(PAGE, {"tab": "locations"})
    assert response.status_code == 200
    assert 'id="report-locations"' in response.text and "Reports by location" in response.text
    rows = response.context["locations"]["rows"]
    assert sorted((r["report"], r["location"], r["value_in_period"]) for r in rows) == [
        ("QPR1", "Akkar", 20),
        ("QPR1", "Bekaa", 30),
        ("QPR2", "Akkar", 7),
    ]
    # its part, its export and its own filters as before
    assert get_part(client_viewer, f"{PAGE}?tab=locations", "table").status_code == 200
    assert raw_get(client_viewer, PAGE, {"tab": "locations", "export": "csv"}).status_code == 200


def test_by_disaggregation_still_opens_from_its_address_with_its_filters(client_viewer, labelled):
    response = client_viewer.get(PAGE, {"tab": "disaggregations", "location": "Bekaa"})
    assert response.status_code == 200
    html = response.text
    assert 'id="report-disaggregations"' in html and "Reports by disaggregation" in html
    assert 'id="f-disaggregation"' in html and 'id="f-pcode"' in html  # the tab's own filters stay
    blocks = response.context["disaggregations"]["blocks"]
    assert [(b["report"], b["location"], b["total"]) for b in blocks] == [("QPR1", "Bekaa", 30)]
    assert raw_get(client_viewer, PAGE, {"tab": "disaggregations", "export": "xlsx"}).status_code == 200


def test_the_figure_trail_and_the_power_bi_comparison_still_read_the_hidden_tabs(labelled, roles):
    validator = _validator()
    for tab in HIDDEN:
        response = validator.get(reverse("reports:check_figures", args=["progress-reports"]), {"tab": tab})
        assert response.status_code == 200
        assert f'id="trail-{tab}" open' in response.text
    for kind in HIDDEN:
        assert powerbi_results.NEURODB_PAGE[kind] == ("reports:partner_reporting", {"tab": kind})
    assert powerbi_words.NEURODB_PAGES["locations"] == "Progress reports → By location"
    assert powerbi_words.NEURODB_PAGES["disaggregations"] == "Progress reports → By disaggregation"


# ---------------------------------------------------------------------------------------- Partner view
def test_the_partner_view_tab_keeps_its_figures(client_viewer, labelled):
    response = client_viewer.get(PAGE, {"tab": "partners"})
    assert response.status_code == 200
    assert response.context["tab"] == "partners"
    assert response.context["data"]["partners"]["partners"] == 1
    assert "Reports to follow up" in response.text


def test_the_trail_and_the_comparison_name_the_partner_view_tab(labelled):
    import datetime

    found = trail.progress_trail("progress-reports", {"pd_status": ""}, datetime.date(2026, 10, 9))
    block = next(b for b in found.blocks if b.key == "partners")
    assert block.title == "Partner view" and block.tab == "partners"
    for kind in (
        "partners_count",
        "partners_reported",
        "partners_not_reported",
        "partner_cards",
        "follow_up",
    ):
        assert powerbi_words.NEURODB_PAGES[kind].startswith("Progress reports → Partner view"), kind
        assert powerbi_results.NEURODB_PAGE[kind] == ("reports:partner_reporting", {"tab": "partners"})


# -------------------------------------------------------------------------------- no link to a hidden tab
def _regular_pages(labelled) -> list[tuple[str, dict]]:
    pd, partner = labelled["pd"], labelled["partner"]
    return [
        (reverse("reports:overview"), {}),
        (PAGE, {}),
        (PAGE, {"tab": "partners"}),
        (PAGE, {"status": "accepted", "year": "2026"}),
        (reverse("reports:pd_monitoring"), {}),
        (reverse("reports:pd_monitoring"), {"view": "table"}),
        (reverse("reports:pd_monitoring"), {"view": "quality"}),
        (reverse("reports:pd_monitoring_map"), {}),
        (reverse("reports:pd_indicator", args=[pd.pk, "500"]), {"year": "all"}),
        (reverse("reports:progress_report"), {"report": "PR-1"}),
        (reverse("reports:programmes"), {}),
        (reverse("reports:programme_detail", args=[pd.pk]), {}),
        (reverse("reports:partner_profile", args=[partner.pk]), {}),
        (reverse("reports:brief"), {}),
        (reverse("reports:country_overview"), {}),
        (reverse("reports:daily_review"), {}),
        (reverse("help:page", args=["partner-progress"]), {}),
        (reverse("help:page", args=["overview"]), {}),
        (reverse("help:page", args=["home"]), {}),
    ]


def test_no_regular_page_links_to_a_hidden_tab(client_viewer, labelled):
    for url, params in _regular_pages(labelled):
        response = client_viewer.get(url, params)
        assert response.status_code == 200, url
        found = [
            address
            for address in (html_module.unescape(a) for a in ADDRESS.findall(response.text))
            if HIDDEN_ADDRESS.search(address)
        ]
        assert found == [], (url, params, found)


def test_no_page_sends_people_to_a_hidden_tab_in_words(client_viewer, labelled):
    """The management brief no longer says PRP's disaggregations are on the By disaggregation tab: they are on
    each indicator's page."""
    for url, params in _regular_pages(labelled):
        if url.startswith("/help/"):
            continue  # the guide says where the two tabs went
        html = client_viewer.get(url, params).text
        assert "By disaggregation" not in html and "By location" not in html, url
    brief = client_viewer.get(reverse("reports:brief")).text
    assert 'id="brief-who"' in brief and "each indicator's page, under Reached by disaggregation" in brief


# ------------------------------------------------------------------------------------------- the guide
def test_the_help_guide_describes_the_two_tabs_and_says_the_others_are_hidden():
    from pathlib import Path

    from neurodb.help import guide

    folder = Path(guide.__file__).parent / "guide"
    every = {path.name: path.read_text(encoding="utf-8") for path in folder.glob("*.md")}
    for name, words in every.items():
        assert "Partners tab" not in words, name
    page = every["partner-progress.md"]
    section = page.split("## Progress reports", 1)[1].split("\n## ", 1)[0]
    assert "has two tabs" in section and "**Reporting progress**" in section and "**Partner view**" in section
    hidden = next(p for p in section.split("\n\n") if "**By location**" in p)
    assert "hidden" in hidden and "**By disaggregation**" in hidden
    # the guide's other pages no longer send people to the two tabs
    assert "By location" not in every["overview.md"] and "By disaggregation" not in every["overview.md"]
