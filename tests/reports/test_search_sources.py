"""The search (SPEC_HOME §5): pages, partners, programme documents, country programme indicators, knowledge base
documents and the ActivityInfo archive of every year, with the year in each archive hint (``?year=`` narrows
them). The Pages group follows the menu's conditions; partner staff are never searched, nor who added a
document; visitors and donors get no search box."""

from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VIEWER
from neurodb.cpd.models import CountryProgramme
from neurodb.cpd.models import Indicator as ProgrammeIndicator
from neurodb.donors.models import DonorAccount
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, NeuroReport, ReportingYear
from neurodb.indicators.services import activityinfo, navigation
from neurodb.knowledge.models import Document
from neurodb.partnerships.models import PCA, PartnerOrganization, PartnerStaffMember
from neurodb.reports import services

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}


def _groups(client, q: str, **params) -> dict[str, list[tuple[str, str, str]]]:
    """{group title: [(label, hint, url)]} of the search's answer."""
    html = client.get(reverse("reports:search"), {"q": q, **params}, **HX).content.decode()
    found: dict[str, list] = {}
    title = None
    for match in re.finditer(
        r'<div class="search-group__title">([^<]+?) <span|'
        r'<a class="search-item" role="option" aria-selected="false" href="([^"]+)"><span>(.*?)</span>'
        r'(?:<span class="search-item__hint">([^<]*)</span>)?</a>',
        html,
        re.S,
    ):
        if match.group(1):
            title = match.group(1)
            found[title] = []
        else:
            label = re.sub(r"<[^>]+>", "", match.group(3))
            found[title].append((label, (match.group(4) or "").replace("&#x27;", "'"), match.group(2)))
    return found


@pytest.fixture
def partner(db):
    return PartnerOrganization.objects.create(
        etl_id="77",
        name="Amel Association International",
        short_name="Amel",
        partner_type="Civil Society Organization",
    )


def test_amel_finds_the_partner(client_viewer, partner):
    """V5: a partner's name (or its short name) finds its page."""
    groups = _groups(client_viewer, "Amel")
    assert groups["Partners"] == [
        ("Amel Association International", "Amel", reverse("reports:partner_profile", args=[partner.id]))
    ]
    hidden = PartnerOrganization.objects.create(
        etl_id="78", name="Amel old", partner_type="Government", hidden=True
    )
    assert reverse("reports:partner_profile", args=[hidden.id]) not in str(_groups(client_viewer, "Amel"))


def test_a_programme_document_by_number_or_title(client_viewer, partner):
    pd = PCA.objects.create(
        etl_id="5",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA2026017",
        title="Child protection",
    )
    for q in ("PCA2026017", "child protec"):
        rows = _groups(client_viewer, q)["Programme documents"]
        assert rows == [
            (
                "LEB/PCA2026017 · Child protection",
                partner.name,
                reverse("reports:programme_detail", args=[pd.id]),
            )
        ], q


def test_a_country_programme_indicator_by_code_or_title(client_viewer, db):
    cycle = CountryProgramme.objects.create(
        name="Lebanon Country Programme 2026-2028", start_year=2026, end_year=2028
    )
    indicator = ProgrammeIndicator.objects.create(programme=cycle, code="1.2.3", title="Children in learning")
    for q in ("1.2.3", "in learning"):
        assert _groups(client_viewer, q)["Country programme indicators"] == [
            ("1.2.3 · Children in learning", cycle.name, reverse("cpd:indicator", args=[indicator.id]))
        ], q


def test_a_knowledge_base_document_never_names_who_added_it(client_viewer, db):
    adder = User.objects.create_user(
        username="owner-placeholder", email="owner@example.org", password="x-12345678"
    )
    adder.first_name, adder.last_name = "Owner", "Placeholder"
    adder.save()
    document = Document.objects.create(title="Annual report 2025", added_by=adder)
    html = client_viewer.get(reverse("reports:search"), {"q": "annual"}, **HX).content.decode()
    rows = _groups(client_viewer, "annual")["Knowledge base"]
    assert rows[0][0] == "Annual report 2025" and rows[0][2] == reverse(
        "knowledge:detail", args=[document.id]
    )
    assert "Added to the knowledge base" in rows[0][1] and "15 Oct 2026" in rows[0][1]
    for name in ("Owner", "Placeholder", "owner-placeholder", "owner@example.org"):
        assert name not in html


def test_partner_staff_are_never_searched(client_viewer, partner):
    PartnerStaffMember.objects.create(
        partner=partner, title="Officer", first_name="Zelda", last_name="Placeholder", email="", phone="0"
    )
    assert _groups(client_viewer, "Zelda") == {}
    assert "Zelda" not in str(services.search("Zelda"))


def test_pages_by_their_menu_label(client_viewer, db):
    rows = _groups(client_viewer, "monitoring")["Pages"]
    assert ("Partner monitoring", "Partner progress (eTools)", reverse("reports:pd_monitoring")) in rows
    assert ("Field monitoring", "Funds and assurance", reverse("reports:monitoring")) in rows


def test_the_pages_group_follows_the_menus_conditions(client, roles, settings):
    settings.COMPILER_API_URL = settings.COMPILER_API_TOKEN = ""
    settings.AI_ASSISTANT_ENABLED = False
    viewer = User.objects.create_user(username="pages-viewer", password="pass-123456-x")
    viewer.groups.add(Group.objects.get(name=VIEWER))
    admin = User.objects.create_user(username="pages-admin", password="pass-123456-x", is_staff=True)
    admin.groups.add(Group.objects.get(name=ADMIN))

    def pages(user, q):
        client.force_login(user)
        return [label for label, _hint, _url in _groups(client, q).get("Pages", [])]

    # not live: hidden for a Viewer, shown (as a muted menu row) for an Administrator who can set it up
    assert pages(viewer, "country programme") == []
    assert pages(admin, "country programme") == ["Country programme"]
    assert pages(viewer, "makani") == [] and pages(admin, "makani") == ["Makani (MSCC)", "Makani wellbeing"]
    assert pages(viewer, "document review") == [] and pages(admin, "document review") == ["Document review"]
    # Ask: the assistant is off, so only members of staff have it in the menu
    assert pages(viewer, "ask") == [] and pages(admin, "ask") == ["Ask NeuroDB"]
    # live: everyone
    CountryProgramme.objects.create(name="Country programme 2026-2028", start_year=2026, end_year=2028)
    navigation.invalidate()
    assert pages(viewer, "country programme") == ["Country programme"]
    settings.FMM_ENABLED = False
    assert pages(viewer, "insights") == []
    # the help pages, and the archive's pages while there is an archive
    assert pages(viewer, "help") == ["Help"]
    assert pages(viewer, "activityinfo databases") == []


def _year(name: str, current=False) -> ReportingYear:
    return ReportingYear.objects.create(name=name, year=name, current=current)


@pytest.fixture
def two_years(hierarchy, section):
    """2026 (hierarchy: records to February) and 2025, each with a displayed database and a report."""
    older = _year("2025")
    db = Database.objects.create(
        ai_id=202501, db_id="cp25", name="Child Protection 2025", label="Child Protection 2025", username="",
        password="", section=section, reporting_year=older, display=True,
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=db, database_ai_id="202501", indicator_id="x", month_name="2025-12-01"
    )
    report = NeuroReport.objects.create(name="HPM 2025", is_hpm=True, ryear=older)
    activityinfo.invalidate()
    return {"older": older, "database": db, "report": report}


def test_the_activityinfo_archive_of_every_year_with_its_year(client_viewer, hierarchy, two_years):
    groups = _groups(client_viewer, "Child Protection")
    assert "Databases" not in groups  # ActivityInfo is archived: its groups say so
    rows = groups["ActivityInfo archive: Databases"]
    assert [(label, hint) for label, hint, _url in rows] == [
        ("Child Protection", "2026"),  # the section's name is the label: only the year in the hint
        ("Child Protection 2025", "Child Protection · 2025"),
    ]
    reports = _groups(client_viewer, "HPM")["ActivityInfo archive: Reports"]
    assert [(label, hint) for label, hint, _url in reports] == [
        ("HPM 2026", "HPM report · 2026"),
        ("HPM 2025", "HPM report · 2025"),
    ]
    indicators = _groups(client_viewer, "reached")
    assert "ActivityInfo indicators" in indicators  # never prefixed
    assert "ActivityInfo archive: Sub-indicators" in indicators
    assert "ActivityInfo archive: Master indicators" in indicators
    assert all(hint.endswith("· 2026") for _l, hint, _u in indicators["ActivityInfo indicators"])


def test_while_in_use_the_groups_keep_their_names(client_viewer, hierarchy, two_years, activityinfo_in_use):
    groups = _groups(client_viewer, "Child Protection")
    assert "Databases" in groups and "ActivityInfo archive: Databases" not in groups


def test_a_year_narrows_the_archive_and_drops_the_year_from_the_hints(client_viewer, hierarchy, two_years):
    rows = _groups(client_viewer, "HPM", year="2025")["ActivityInfo archive: Reports"]
    assert rows == [("HPM 2025", "HPM report", reverse("reports:report_hpm", args=[two_years["report"].id]))]
    page = client_viewer.get(reverse("reports:search"), {"q": "HPM", "year": "2025"}).content.decode()
    assert '<input type="hidden" name="year" value="2025">' in page
    assert "Partners, programme documents, indicators, pages and the ActivityInfo archive" in page
    page = client_viewer.get(reverse("reports:search"), {"q": "HPM"}).content.decode()
    assert 'name="year"' not in page.split('role="search"', 1)[1].split("</form>", 1)[0]


def test_see_all_lists_one_group_and_keeps_the_year(client_viewer, hierarchy, two_years):
    for n in range(9):
        PartnerOrganization.objects.create(
            etl_id=f"p{n}", name=f"Child partner {n}", partner_type="Government"
        )
    html = client_viewer.get(reverse("reports:search"), {"q": "child"}, **HX).content.decode()
    assert "8 of 9" in html and "group=partners" in html
    rows = _groups(client_viewer, "child", group="partners")["Partners"]
    assert len(rows) == 9
    html = client_viewer.get(reverse("reports:search"), {"q": "child", "year": "2025"}, **HX).content.decode()
    assert "?year=2025&amp;q=child&amp;group=partners" in html


def test_one_query_per_group(client_viewer, hierarchy, two_years, partner):
    """Every group answers in one query, its first rows with the number of all its matches (a group cut at its
    first items needs no count of its own): nine with the menu's pages made in Python; at most 14 for the whole
    request once the ActivityInfo state and the menu's sources are stored, as on a warm worker."""
    for n in range(12):  # a group cut at 8 of 12
        PartnerOrganization.objects.create(
            etl_id=f"c{n}", name=f"Child partner {n}", partner_type="Government"
        )
    services.search("chi", user=None)  # the state and the sources stored
    with CaptureQueriesContext(connection) as queries:
        found = services.search("Child", user=None)
    partners = next(g for g in found if g["key"] == "partners")
    assert (len(partners["items"]), partners["total"]) == (8, 12)
    assert {g["key"] for g in found} >= {"partners", "databases", "masters"}
    assert len(queries.captured_queries) == 9  # one per group searched in the database, no count
    client_viewer.get(reverse("reports:search"), {"q": "Child"}, **HX)
    with CaptureQueriesContext(connection) as queries:
        client_viewer.get(reverse("reports:search"), {"q": "Child"}, **HX)
    assert len(queries.captured_queries) <= 14


def test_nothing_found(client_viewer, db):
    html = client_viewer.get(reverse("reports:search"), {"q": "zz-nothing"}, **HX).content.decode()
    assert "Nothing matches “zz-nothing”." in html and "No results" in html
    assert "reporting year" not in html


def test_the_search_box_is_for_signed_in_staff_only(client, client_viewer, db, settings):
    settings.AI_ASSISTANT_ENABLED = False
    html = client_viewer.get(reverse("reports:library")).content.decode()
    trigger = html.split('id="search-trigger"', 1)[1].split(">", 1)[0]
    assert 'aria-label="Search partners, programme documents, indicators"' in trigger
    field = re.search(r'<input type="search" id="search-input"[^>]*>', html).group(0)
    assert 'placeholder="Partners, programme documents, indicators…"' in field
    assert "hx-vals" not in field  # every year: no year sent
    client.logout()
    settings.PUBLIC_PAGES = ["reports:library"]
    visitor = client.get(reverse("reports:library")).content.decode()
    assert 'id="search-modal"' not in visitor and 'id="search-trigger"' not in visitor
    # a donor never reaches a page of the site (DonorScopeMiddleware); were an error page drawn for one, it
    # would carry no search box either
    from django.template.loader import render_to_string
    from django.test import RequestFactory

    donor = User.objects.create_user(username="search-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=donor, name="Donor", donors=["EU"], must_change_password=False)
    request = RequestFactory().get("/no-such-page/")
    request.user, request.csp_nonce = donor, "n"
    page = render_to_string("404.html", request=request)
    assert 'id="search-modal"' not in page and "Try the search" not in page
