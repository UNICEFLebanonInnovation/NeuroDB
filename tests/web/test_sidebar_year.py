"""The sidebar's Databases, Neuro reports and HPM blocks follow the page's reporting year, and a
new year without any keeps the menus, showing the latest year that has them."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from neurodb.indicators.models import Database, NeuroReport, ReportingYear

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _sidebar(html: str) -> str:
    return html.split('class="sidebar', 1)[1].split("</nav>", 1)[0]


def test_a_new_empty_current_year_keeps_the_menus(client_viewer, hierarchy, reporting_year):
    NeuroReport.objects.create(name="Monthly CP", is_hpm=False, ryear=reporting_year)
    reporting_year.current = False
    reporting_year.save()
    ReportingYear.objects.create(name="2027", year="2027", current=True)
    side = _sidebar(client_viewer.get(reverse("reports:overview")).text)
    assert "Databases 2026" in side and "Neuro reports 2026" in side and "HPM 2026" in side
    assert reverse("reports:database_dashboard", args=[hierarchy["database"].id]) in side
    assert reverse("reports:report_hpm", args=[hierarchy["report"].id]) in side


def test_the_sidebar_follows_the_chosen_year(client_viewer, hierarchy, section):
    older = ReportingYear.objects.create(name="2025", year="2025", current=False)
    old_db = Database.objects.create(
        ai_id=202501, db_id="old", name="CP 2025", label="CP 2025", username="", password="",
        section=section, reporting_year=older, display=True,
    )  # fmt: skip
    old_report = NeuroReport.objects.create(name="HPM 2025", is_hpm=True, ryear=older)
    current_db = reverse("reports:database_dashboard", args=[hierarchy["database"].id])

    side = _sidebar(client_viewer.get(reverse("reports:overview") + "?year=2025").text)
    assert "Databases 2025" in side and reverse("reports:database_dashboard", args=[old_db.id]) in side
    assert current_db not in side and reverse("reports:report_hpm", args=[old_report.id]) in side

    # A database or report page shows its own year's menu, without ?year=.
    side = _sidebar(client_viewer.get(reverse("reports:database_dashboard", args=[old_db.id])).text)
    assert "Databases 2025" in side and current_db not in side
    side = _sidebar(client_viewer.get(reverse("reports:report_hpm", args=[old_report.id])).text)
    assert "HPM 2025" in side

    # Without a year, the current one.
    side = _sidebar(client_viewer.get(reverse("reports:overview")).text)
    assert "Databases 2026" in side and current_db in side
