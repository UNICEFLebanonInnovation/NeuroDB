"""The year menu keeps the user on the page they are on when that page shows a year."""

import re

from django.urls import reverse

from neurodb.indicators.models import ReportingYear


def _links(html: str) -> dict[str, str]:
    menu = html.split('aria-labelledby="year-menu"', 1)[1].split("</ul>", 1)[0]
    return {
        name.strip(): href.replace("&amp;", "&")
        for href, name in re.findall(r'href="([^"]+)">\s*([0-9]{4})', menu)
    }


def test_a_page_with_a_year_stays_the_same_page_with_its_filters(client_viewer, database):
    ReportingYear.objects.create(name="2025", year="2025")
    page = client_viewer.get(reverse("reports:databases"), {"q": "child", "page": "2"}).content.decode()
    assert _links(page)["2025"] == f"{reverse('reports:databases')}?q=child&year=2025"


def test_a_database_of_one_year_goes_to_that_years_databases(client_viewer, database):
    ReportingYear.objects.create(name="2025", year="2025")
    page = client_viewer.get(reverse("reports:database_dashboard", args=[database.pk])).content.decode()
    assert _links(page)["2025"] == f"{reverse('reports:databases')}?year=2025"


def test_a_page_without_a_year_has_no_year_menu(client_viewer, database):
    """The menu is drawn only on the pages that show one year (SPEC_HOME §5): picking a year on the partners
    page used to jump to the overview."""
    ReportingYear.objects.create(name="2025", year="2025")
    page = client_viewer.get(reverse("reports:partners")).content.decode()
    assert 'id="year-menu"' not in page
