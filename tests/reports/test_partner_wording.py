"""Partner monitoring and the help guide's pages about partners name no master or sub indicator (the owner's
decision of 24 Nov 2026: the eTools Datamart has no such terms, so NeuroDB's own grouping of the partners' PD
indicators was removed, with its view, its pages, its wording and its help).

Every view of Partner monitoring a user can open (Data quality and its grid, every year too, the old Grid
address, Program Performance, the map with its points, an indicator's page and pop-up, an old address of the
removed view once followed), each of their parts asked for alone, their exports, a validator's figure trails
and Power BI comparison sheets, the programme document and partner pages, the eTools reporting checks, and
the help guide's pages about partners: none says "master indicator", "masters", "sub indicator",
"sub-indicator" or "subs", in any case, nor links to the removed view's addresses.

The ActivityInfo archive and the Compiler youth figures keep their own master and sub indicators: their pages
still say so, which also shows the wording check finds them.

On the world of tests/reports/test_pd_monitoring.py (``data``, with ``more`` of test_powerbi_views.py), today
frozen at 1 July 2026.
"""

# ruff: noqa: F401, F811 (the fixtures "data", "more", "frozen" and "etools" come from their own modules)
from __future__ import annotations

import html as html_module
import io
import re

import pytest
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.help import guide
from tests.reports.test_pd_monitoring import data
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import _element_end, get_part, placeholders, raw_get, text
from tests.youth.test_youth import FakeClient, etools

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"  # reports:pd_monitoring
# the words of the removed grouping, in any case: "master indicator(s)", "master(s)" alone,
# "sub indicator(s)", "sub-indicator(s)", "subindicator(s)" and "subs"
WORDING = re.compile(r"\bmasters?\b|\bsub[- ]?indicators?\b|\bsubs\b", re.I)
# the removed view's addresses: its view word and its pages
REMOVED_ADDRESSES = ("view=masters", "/partner-monitoring/masters/")
TAGS = re.compile(r"<[^>]+>")
# Partner monitoring's views as a user opens them: Data quality (the bare address), every year, the old Grid
# tab's address, Program Performance this year and every year
VIEWS = (
    {},
    {"view": "quality", "year": "all"},
    {"view": "grid"},
    {"view": "table"},
    {"view": "table", "year": "all"},
)
# the help guide's pages about partners, their reporting and the figures read from it
PARTNER_HELP = ("partner-progress", "checking-figures", "glossary", "exports", "home", "briefs-and-overview")
TRAIL = re.compile(r'href="(/check/partner-monitoring/\?[^"]*)"')  # *Check these figures*
# the Power BI comparison sheet: a form of hidden fields and the day the statuses are read for
SHEET = re.compile(
    r'<form method="get" action="(/check/partner-monitoring/compare\.xlsx)"[^>]*>(.*?)</form>', re.S
)
FIELD = re.compile(r'<input type="(?:hidden|date)" name="([^"]*)"[^>]*?value="([^"]*)"')


@pytest.fixture
def world(more, frozen):
    return more


@pytest.fixture
def validator(roles) -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


def _words(html: str) -> str:
    """What a reader sees of ``html``: its text without the tags, entities decoded (a script's or a JSON
    block's text is kept, as a chart's labels come from it)."""
    return " ".join(html_module.unescape(TAGS.sub(" ", html)).split())


def _said(where: str, html: str) -> None:
    """``html`` names no master or sub indicator, and links to none of the removed view's addresses."""
    found = sorted({m.group(0) for m in WORDING.finditer(_words(html))})
    assert found == [], (where, found)
    for address in REMOVED_ADDRESSES:
        assert address not in html, (where, address)


def _body(response: HttpResponse) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else response.content


def _cells(response: HttpResponse) -> str:
    """Every cell of every sheet of an Excel answer, as one text."""
    book = load_workbook(io.BytesIO(_body(response)), read_only=True)
    return " ".join(
        str(value) for sheet in book.worksheets for row in sheet.iter_rows(values_only=True) for value in row
    )


# ------------------------------------------------------------------------------------- the views and parts
@pytest.mark.parametrize("params", VIEWS)
def test_no_view_of_partner_monitoring_names_a_master_or_sub_indicator(client_viewer, world, params):
    response = client_viewer.get(PAGE, params)  # the page as the browser shows it: every part in place
    assert response.status_code == 200, params
    html = text(response)
    assert world["girls"] in html_module.unescape(html), params  # its figures are drawn: not an empty page
    _said(f"{PAGE} {params}", html)


@pytest.mark.parametrize("params", VIEWS)
def test_no_part_of_partner_monitoring_names_one(client_viewer, world, params):
    shell = raw_get(client_viewer, PAGE, params)
    assert shell.status_code == 200
    _said(f"the frame of {params}", text(shell))
    names = list(placeholders(text(shell)))
    assert names, params  # the page loads in parts
    url = PAGE + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    for name in names:
        _said(f"the part {name} of {params}", text(get_part(client_viewer, url, name)))


def test_an_old_address_of_the_removed_view_lands_on_a_page_that_names_none(client_viewer, world):
    response = client_viewer.get(PAGE, {"view": "masters", "year": "2026"}, follow=True)
    assert response.redirect_chain == [(PAGE, 302)] and response.status_code == 200
    _said("the landing page of an old address", text(response))


def test_the_map_and_its_points_name_none(client_viewer, world):
    page = client_viewer.get(reverse("reports:pd_monitoring_map"), {"year": "2026"})
    assert page.status_code == 200
    _said("the map", text(page))
    points = client_viewer.get(reverse("api:pd_map"), {"year": "2026"})
    assert points.status_code == 200 and points.json()  # it has places to show
    found = sorted({m.group(0) for m in WORDING.finditer(points.content.decode())})
    assert found == [], found


def test_an_indicator_s_page_and_pop_up_name_none(client_viewer, world):
    url = reverse("reports:pd_indicator", args=[world["pd"].id, "100"])
    page = client_viewer.get(url, {"year": "2026"})
    modal = client_viewer.get(url, {"year": "2026"}, headers={"HX-Request": "true"})
    assert page.status_code == modal.status_code == 200 and "modal-header" in text(modal)
    _said("an indicator's page", text(page))
    _said("an indicator's pop-up", text(modal))


@pytest.mark.parametrize("params", [{}, {"view": "table"}, {"view": "quality", "year": "all"}])
def test_the_exports_name_none(client_viewer, world, params):
    csv_file = client_viewer.get(PAGE, {**params, "export": "csv"})
    assert csv_file.status_code == 200 and csv_file.has_header("Content-Disposition")
    content = _body(csv_file).decode("utf-8-sig")
    assert world["girls"] in content
    assert not WORDING.search(content), params
    excel = client_viewer.get(PAGE, {**params, "export": "xlsx"})
    assert excel.status_code == 200
    cells = _cells(excel)
    assert world["girls"] in cells
    assert not WORDING.search(cells), params


def test_the_programme_document_and_partner_pages_name_none(client_viewer, world):
    pd, partner = world["pd"], world["partner"]
    for url in (
        reverse("reports:programme_detail", args=[pd.pk]),
        reverse("reports:partner_profile", args=[partner.pk]),
    ):
        response = client_viewer.get(url)
        assert response.status_code == 200, url
        _said(url, text(response))


# ------------------------------------------------------------------------------- a validator's checks
@pytest.mark.parametrize("params", [{}, {"view": "table"}, {"view": "quality", "year": "all"}])
def test_the_figure_trails_and_power_bi_sheets_name_none(validator, world, params):
    page = text(validator.get(PAGE, params))
    trails = sorted({html_module.unescape(link) for link in TRAIL.findall(page)})
    sheets = SHEET.findall(page)
    assert trails and sheets, params  # *Check these figures* and the comparison sheet are offered
    for link in trails:
        response = validator.get(link)  # the trail with every part in place
        assert response.status_code == 200, link
        _said(link, text(response))
    for action, form in sheets:
        fields = [(name, html_module.unescape(value)) for name, value in FIELD.findall(form)]
        response = validator.get(action, fields)
        assert response.status_code == 200, (action, fields)
        found = sorted({m.group(0) for m in WORDING.finditer(_cells(response))})
        assert found == [], (action, found)


def test_the_map_s_trail_names_none(validator, world):
    response = validator.get(reverse("reports:check_figures", args=["partner-monitoring"]), {"view": "map"})
    assert response.status_code == 200
    _said("the map's trail", text(response))


def test_the_etools_reporting_checks_name_none(client, admin_user, world):
    """The page itself: the administration's menu around it lists the archive's own master and sub indicators
    (*Reporting setup*), which stay."""
    client.force_login(admin_user)
    response = client.get(reverse("admin:datamart_reporting_checks"))
    page = text(response)
    assert response.status_code == 200 and "Check 11 ·" in page
    start = page.index('<div id="content-main"')
    _said("the eTools reporting checks", page[start : _element_end(page, start)])


# ------------------------------------------------------------------------------------------- the guide
@pytest.mark.parametrize("key", PARTNER_HELP)
def test_the_help_pages_about_partners_name_none(validator, key):
    assert key in guide.ORDER
    _said(f"the guide's {key}.md", guide.GUIDE_DIR.joinpath(f"{key}.md").read_text(encoding="utf-8"))
    response = validator.get(reverse("help:page", args=[key]))  # a validator: every line of the page shown
    assert response.status_code == 200
    _said(f"/help/{key}/", text(response))


def test_the_guide_s_search_finds_no_master_or_sub_indicator_on_a_page_about_partners():
    for question in ("master indicator", "sub indicator", "sub-indicators", "masters"):
        for found in guide.search(question, limit=50):
            if found.page in PARTNER_HELP:
                assert not WORDING.search(f"{found.heading} {found.text}"), (question, found.page, found.slug)


# ---------------------------------------------------------------------- the archive and the youth figures
def test_the_activityinfo_archive_keeps_its_master_indicators(client, admin_user, hierarchy):
    """Not the eTools grouping: the archive's master indicators stay, and the wording check above finds
    them."""
    from neurodb.indicators.models import Database

    client.force_login(admin_user)
    response = client.get(reverse("reports:database_dashboard", args=[Database.objects.get().pk]))
    assert response.status_code == 200
    assert WORDING.search(_words(text(response)))


def test_the_youth_figures_keep_their_master_indicators(client, admin_user, etools):
    """Nor Compiler's youth master and sub indicators: they stay, and the wording check above finds them."""
    from neurodb.youth.sync import sync

    sync(triggered_by="test", client=FakeClient(["2026"]))
    client.force_login(admin_user)
    response = client.get(reverse("youth:dashboard"))
    assert response.status_code == 200
    assert WORDING.search(_words(text(response)))
