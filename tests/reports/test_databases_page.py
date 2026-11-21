"""The ActivityInfo databases page: the cards that used to close the overview, with the year's totals."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_cards_totals_and_sidebar_link(client_viewer, hierarchy, database, full_menu):
    page = client_viewer.get(reverse("reports:databases"))
    assert page.status_code == 200
    html = page.text
    assert "ActivityInfo databases" in html and "Child Protection" in html
    assert reverse("reports:database_dashboard", args=[database.id]) in html
    assert 'id="databases-chart-data"' in html and "Tracking status across databases" in html
    assert page.context["data"]["totals"]["databases"] == 1
    # A menu item of its own under "Monthly results (ActivityInfo)", marked as the current page.
    assert 'aria-current="page"' in html.split("ActivityInfo databases</span>")[0].rsplit("<a ", 1)[1]


def test_the_menu_item_shows_even_when_the_year_has_no_databases(client_viewer, reporting_year, full_menu):
    html = client_viewer.get(reverse("reports:overview")).text
    assert f'href="{reverse("reports:databases")}"' in html and "ActivityInfo databases" in html


def test_empty_year_and_no_year(client_viewer, reporting_year):
    page = client_viewer.get(reverse("reports:databases"))
    assert page.status_code == 200 and "No databases for this year" in page.text
    reporting_year.delete()
    page = client_viewer.get(reverse("reports:databases"))
    assert page.status_code == 200 and "No reporting year is configured" in page.text


def test_the_overview_neither_links_to_the_page_nor_shows_the_cards(client_viewer, hierarchy, database):
    """The databases are the ActivityInfo archive's: the menu's archive block leads to them, the overview
    does not (the owner's rule, November 2026)."""
    html = client_viewer.get(reverse("reports:overview")).text
    page = html.split('<details class="sidebar__archive"')[0]
    assert reverse("reports:databases") not in page and "db-card" not in html
