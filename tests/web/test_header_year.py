"""The header's year menu (SPEC_HOME §5): drawn only on the pages that show one year. Calendar years there (this
year down to the first year NeuroDB has data for), ActivityInfo years on the ActivityInfo pages; a label that says
what the page shows, "All years" included; the active entry marked for screen readers too."""

from __future__ import annotations

import datetime
import re

import pytest
from django.urls import reverse

from neurodb.indicators.models import ReportingYear
from neurodb.partnerships.models import PCA

pytestmark = pytest.mark.django_db

ITEM = re.compile(
    r'<li><a class="dropdown-item( active)?" href="([^"]*)"( aria-current="true")?>([^<]+)(.*?)</a></li>'
)


def _menu(html: str) -> dict | None:
    if 'id="year-menu"' not in html:
        return None
    button = html.split('id="year-menu"', 1)[1].split("</button>", 1)[0]
    listing = html.split('aria-labelledby="year-menu"', 1)[1].split("</ul>", 1)[0]
    label = re.search(
        r'<span class="year-menu__label"><span class="visually-hidden">Year: </span>([^<]+)</span>', button
    )
    items = [
        {
            "active": bool(active),
            "href": href.replace("&amp;", "&"),
            "current": bool(current),
            "text": text.strip(),
            "note": re.sub(r"<[^>]+>", "", note).strip(),
        }
        for active, href, current, text, note in ITEM.findall(listing)
    ]
    for item in items:
        assert item["active"] == item["current"]  # class="dropdown-item active" href="…" aria-current="true"
    return {
        "label": label.group(1) if label else None,
        "header": re.search(r'<h6 class="dropdown-header">([^<]+)</h6>', listing).group(1),
        "items": items,
        "active": [i["text"] for i in items if i["active"]],
    }


def _get(client, url, **params):
    return _menu(client.get(url, params).content.decode())


@pytest.mark.parametrize(
    "name",
    [
        "reports:overview",
        "reports:partners",
        "education:makani",
        "reports:population",
        "fmm:dashboard",
        "reports:search",
        "watch:for_you",
        "graph:whats_new",
        "help:index",
        "reports:programmes",
    ],
)
def test_no_year_menu_on_pages_that_show_no_year_or_have_their_own(client_viewer, hierarchy, name, settings):
    settings.FMM_ENABLED = True
    assert _get(client_viewer, reverse(name)) is None, name


def test_calendar_years_without_any_reporting_year(client_viewer):
    """V1b: no ReportingYear row at all; the menu still offers calendar years (this year first)."""
    menu = _get(client_viewer, reverse("reports:brief"))
    assert menu["header"] == "Year" and menu["label"] == "2026"
    assert menu["items"][0]["text"] == "2026" and menu["items"][0]["note"] == "(this year)"
    assert menu["active"] == ["2026"]
    assert [i["text"] for i in menu["items"]] == ["2026"]  # nothing older than this year to offer yet


def test_the_years_reach_back_to_the_oldest_programme_document(client_viewer, db):
    PCA.objects.create(etl_id="1", partner_name="P", number="LEB/PCA2022001", start=datetime.date(2022, 3, 1))
    menu = _get(client_viewer, reverse("reports:country_overview"), year="2024")
    assert [i["text"] for i in menu["items"]] == ["2026", "2025", "2024", "2023", "2022"]
    assert menu["label"] == "2024" and menu["active"] == ["2024"]
    assert {i["text"]: i["href"] for i in menu["items"]}[
        "2023"
    ] == f"{reverse('reports:country_overview')}?year=2023"


def test_2027_without_a_reporting_year_row(client, viewer, reporting_year, site_today):
    """V1c: on 5 January 2027 the pages move to 2027 by themselves, whatever reporting year is ticked."""
    site_today.set(datetime.date(2027, 1, 5))
    client.force_login(viewer)  # signed in on that day (a session of October has expired)
    menu = _get(client, reverse("reports:brief"))
    assert menu["label"] == "2027" and menu["items"][0] == {
        "active": True, "href": f"{reverse('reports:brief')}?year=2027", "current": True, "text": "2027",
        "note": "(this year)",
    }  # fmt: skip


def test_an_item_keeps_the_page_and_its_filters(client_viewer, db):
    menu = _get(client_viewer, reverse("reports:assurance"), q="audit", page="3")
    assert {i["text"]: i["href"] for i in menu["items"]}[
        "2026"
    ] == f"{reverse('reports:assurance')}?q=audit&year=2026"


@pytest.mark.parametrize(
    "name", ["knowledge:index", "reports:assurance", "reports:funds", "reports:monitoring"]
)
def test_pages_that_show_every_year_by_default_say_all_years(client_viewer, db, name):
    menu = _get(client_viewer, reverse(name))
    assert menu["label"] == "All years" and menu["active"] == ["All years"]
    assert menu["items"][0]["text"] == "All years" and menu["items"][0]["href"] == reverse(name)
    menu = _get(client_viewer, reverse(name), year="2026")
    assert menu["label"] == "2026" and menu["active"] == ["2026"]
    assert menu["items"][0] == {"active": False, "href": reverse(name), "current": False, "text": "All years",
                                "note": ""}  # fmt: skip


def test_the_hact_visit_plan_tab_shows_one_year(client_viewer, db):
    """Field monitoring shows every year by default, but its HACT visit plan tab shows one year (this year by
    default): the menu names that year and has no "All years" entry there (review R1 spec_shell-1)."""
    PCA.objects.create(etl_id="1", partner_name="P", number="LEB/PCA2025001", start=datetime.date(2025, 3, 1))
    url = reverse("reports:monitoring")
    menu = _get(client_viewer, url, tab="plan")
    assert menu["label"] == "2026" and menu["active"] == ["2026"]
    assert "All years" not in [i["text"] for i in menu["items"]]
    assert {i["text"]: i["href"] for i in menu["items"]}["2025"] == f"{url}?tab=plan&year=2025"
    menu = _get(client_viewer, url, tab="plan", year="2025")
    assert menu["label"] == "2025" and menu["active"] == ["2025"]
    assert "All years" not in [i["text"] for i in menu["items"]]
    # the findings tab still shows every year by default
    assert _get(client_viewer, url, tab="findings")["label"] == "All years"


def test_the_note_of_the_active_entry_is_readable(client_viewer, db):
    """The note "(this year)" sits on the active entry by default, whose background is the dropdown's active
    blue: the note takes the entry's own colour there, never the muted grey (1.27:1 in light, review R1
    a11y_words-A11Y-4)."""
    from django.conf import settings

    html = client_viewer.get(reverse("reports:brief")).content.decode()
    listing = html.split('aria-labelledby="year-menu"', 1)[1].split("</ul>", 1)[0]
    active = re.search(r'<a class="dropdown-item active"[^>]*>(.*?)</a>', listing).group(1)
    assert '<span class="year-menu__note small">(this year)</span>' in active and "text-muted" not in listing
    css = (settings.BASE_DIR / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
    rule = re.search(r"\.dropdown-item\.active \.year-menu__note \{([^}]*)\}", css)
    assert rule is not None and "color: inherit" in rule.group(1) and "opacity" not in rule.group(1)
    note = re.search(r"\.year-menu__note \{([^}]*)\}", css)
    assert note is not None and "var(--nd-muted)" in note.group(1)


def test_all_years_from_the_page_itself(client_viewer, db):
    """Progress reports and partner monitoring tell the menu when they show every year (year_all_active), and
    give its "All years" entry."""
    menu = _get(client_viewer, reverse("reports:partner_reporting"))
    assert menu["label"] == "All years" and menu["active"] == ["All years"]
    menu = _get(client_viewer, reverse("reports:pd_monitoring"))  # this year by default
    assert menu["label"] == "2026" and menu["active"] == ["2026"]
    menu = _get(client_viewer, reverse("reports:pd_monitoring"), year="all")
    assert menu["label"] == "All years" and menu["active"] == ["All years"]


def test_a_year_outside_the_list_has_no_active_item(client_viewer, db):
    menu = _get(client_viewer, reverse("reports:funds"), year="2019")
    assert menu["label"] == "2019" and menu["active"] == []


def test_activityinfo_pages_list_the_reporting_years(client_viewer, hierarchy, activityinfo_in_use):
    ReportingYear.objects.create(name="2025", year="2025")
    menu = _get(client_viewer, reverse("reports:databases"))
    assert menu["header"] == "ActivityInfo year" and menu["label"] == "2026"
    assert [(i["text"], i["note"]) for i in menu["items"]] == [("2026", "(current)"), ("2025", "")]
    assert menu["active"] == ["2026"]
    page = reverse("reports:report_hpm", args=[hierarchy["report"].id])
    menu = _get(client_viewer, page)
    assert {i["text"]: i["href"] for i in menu["items"]}[
        "2025"
    ] == f"{reverse('reports:databases')}?year=2025"


def test_current_only_while_activityinfo_is_in_use(client_viewer, hierarchy, activityinfo_archived):
    menu = _get(client_viewer, reverse("reports:databases"))
    assert [(i["text"], i["note"]) for i in menu["items"]] == [("2026", "")]


def test_page_year_label_and_menu_tags_outside_a_page(rf, db):
    """The tags read only the request and the context: a request without a match draws no menu."""
    from django.contrib.auth.models import AnonymousUser

    from neurodb.web.templatetags.ui import page_year_label, year_menu

    request = rf.get("/", {"year": "2025"})
    request.user = AnonymousUser()
    assert year_menu({"request": request}) is None
    assert page_year_label({"request": request}) == "2025"
    assert page_year_label({"request": rf.get("/", {"year": "all"})}) == "All years"
    assert page_year_label({"request": rf.get("/", {"year": "garbage"})}) == "2026"
