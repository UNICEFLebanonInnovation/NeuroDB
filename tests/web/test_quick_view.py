"""Quick view, the server side: any regular page answers as a pop-up fragment when the quick view asks for it
(``X-NeuroDB-Quick: 1``): its title in the window's header with *Open full page* and Close, its content and its
parts, without the menu, the top bar or the page's scripts. A normal request answers the full page as before; the
administration, the sign-in pages, the donor page and the pages marked not quick-viewable never answer as a quick
view (the browser then opens the address as a normal page).

The world (made-up names): one partner and one programme document."""

from __future__ import annotations

import datetime
import html as html_module
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.donors.test_donor_access import make_donor
from tests.web.parts import executable, placeholders, raw_get, text

pytestmark = pytest.mark.django_db

QUICK = {"HTTP_X_NEURODB_QUICK": "1"}
TEMPLATES = Path(settings.BASE_DIR) / "neurodb" / "web" / "templates"
TITLE = re.compile(r"<title>(.*?) · ", re.S)
MODAL_TITLE = re.compile(r'<h2 [^>]*id="modal-title"[^>]*>(.*?)</h2>', re.S)
FULL_LINK = re.compile(r"<a [^>]*data-quick-full[^>]*>", re.S)
HREF = re.compile(r'href="([^"]*)"')
TAGS = re.compile(r"<[^>]+>")


@pytest.fixture
def world(db, reporting_year):
    partner = PartnerOrganization.objects.create(
        etl_id="7101", name="Sample Relief", partner_type="Civil Society Organization", vendor_number="V7101"
    )
    today = datetime.date.today()
    pd = PCA.objects.create(
        etl_id="7201",
        partner=partner,
        partner_name="Sample Relief",
        number="LEB/PCA2026071-1",
        title="Sample protection services",
        status="active",
        document_type="PD",
        start=today - datetime.timedelta(days=120),
        end=today + datetime.timedelta(days=90),
        section_names=["Child Protection"],
        total_budget="5000",
    )
    return {"partner": partner, "pd": pd}


def _pages(world) -> list[str]:
    return [
        reverse("reports:overview"),  # Home
        reverse("reports:programme_detail", args=[world["pd"].id]),
        reverse("reports:partner_profile", args=[world["partner"].id]),
        reverse("reports:partner_reporting") + "?section=Child+Protection&tab=partners",
        reverse("reports:programmes"),
        reverse("reports:programmes") + "?tab=indicators",
        reverse("reports:programmes") + "?tab=summary&scope=all",
        reverse("reports:programmes") + "?tab=overall",
        reverse("reports:pd_monitoring") + "?view=quality&section=Child+Protection",
    ]


def _words(fragment: str) -> str:
    return " ".join(html_module.unescape(TAGS.sub(" ", fragment)).split())


def _full_link(html: str) -> str:
    links = FULL_LINK.findall(html)
    assert len(links) == 1, links
    return html_module.unescape(HREF.search(links[0]).group(1))


def test_a_quick_request_answers_the_page_as_a_fragment(client_viewer, world):
    for url in _pages(world):
        full = client_viewer.get(url)
        assert full.status_code == 200, url
        page_title = html_module.unescape(TITLE.search(text(full)).group(1)).strip()

        quick = client_viewer.get(url, **QUICK)
        assert quick.status_code == 200, url
        assert quick["X-NeuroDB-Quick-View"] == "1", url
        html = text(quick)
        # a fragment for the shared window: no document, no menu, no top bar, no page script
        assert "<html" not in html and "<head" not in html and "<body" not in html, url
        assert 'id="sidebar"' not in html and 'class="topbar"' not in html, url
        assert 'id="help-panel"' not in html and 'id="search-modal"' not in html, url
        assert "skip-link" not in html, url
        assert not executable(html), (url, executable(html))
        # its title in the window's header (the window is labelled by it), Open full page, Close
        assert html.lstrip().startswith("<div") and "data-quick-view" in html.split(">", 1)[0], url
        (title,) = MODAL_TITLE.findall(html)
        assert _words(title) == page_title, url
        assert _full_link(html) == url, url
        assert "Open full page" in html and 'data-bs-dismiss="modal"' in html, url
        assert re.search(r"<button [^>]*data-quick-back[^>]*hidden", html), url  # Back: shown by the script
        assert 'class="modal-body' in html, url


def test_a_normal_request_answers_the_full_page_as_before(client_viewer, world):
    for url in _pages(world):
        response = client_viewer.get(url)
        html = text(response)
        assert response.status_code == 200, url
        assert "<html" in html and 'id="sidebar"' in html and 'class="topbar"' in html, url
        assert "data-quick-view" not in html and "X-NeuroDB-Quick-View" not in response, url
        # one address answers both: caches keep them apart
        assert "X-NeuroDB-Quick" in response["Vary"], url
        assert "X-NeuroDB-Quick" in client_viewer.get(url, **QUICK)["Vary"], url


def test_the_menu_is_not_built_for_a_quick_view(client_viewer, world, monkeypatch):
    from neurodb.indicators.services import navigation

    def refuse(*args, **kwargs):
        raise AssertionError("the menu was built for a quick view")

    url = reverse("reports:programme_detail", args=[world["pd"].id])
    full = client_viewer.get(url)
    assert "nav_menu" in full.context and "nav" in full.context
    monkeypatch.setattr(navigation, "sidebar", refuse)
    monkeypatch.setattr(navigation, "build_navigation", refuse)
    # the quick view's own answer (its parts, asked for later, are not quick views)
    quick = raw_get(client_viewer, url, **QUICK)
    assert quick.status_code == 200 and quick["X-NeuroDB-Quick-View"] == "1"
    assert "nav_menu" not in quick.context and "nav" not in quick.context


def test_the_parts_of_a_page_still_load_inside_the_fragment(client_viewer, world):
    url = reverse("reports:overview")  # Home loads in parts
    shell = raw_get(client_viewer, url, **QUICK)
    assert shell["X-NeuroDB-Quick-View"] == "1"
    found = placeholders(text(shell))
    assert found, "Home's quick view has no part to load"
    assert any("_part=" in address for address in found.values())
    for name, address in found.items():
        # each part is asked at the page's own address, as on the full page (or at its own endpoint, a url=
        # part such as For you's card): never as a quick view
        if "_part=" in address:
            assert address.startswith(url + "?") and f"_part={name}" in address, address
        part = client_viewer.get(address, HTTP_HX_REQUEST="true", **QUICK)
        assert part.status_code in (200, 202), (name, part.status_code)
        if part.status_code == 200 and part.get("X-NeuroDB-Part"):
            assert part["X-NeuroDB-Part"] == name
            assert "data-quick-view" not in text(part) and "X-NeuroDB-Quick-View" not in part
    # the test client fills the parts in, as the browser does: every one answers inside the fragment
    assembled = client_viewer.get(url, **QUICK)
    assert assembled["X-NeuroDB-Quick-View"] == "1"
    assert not placeholders(text(assembled))
    assert assembled.parts and all(p.status_code == 200 for p in assembled.parts.values())


def test_home_brings_its_own_stylesheet_into_the_quick_view(client_viewer, world):
    html = text(client_viewer.get(reverse("reports:overview"), **QUICK))
    head = re.search(r"<template data-quick-head>(.*?)</template>", html, re.S)
    assert head and "css/home.css" in head.group(1)  # inert: the script adds the stylesheet to the page


def test_admin_sign_in_and_donor_pages_never_answer_as_a_quick_view(client, admin_user, roles):
    def is_quick(response) -> bool:
        return response.get("X-NeuroDB-Quick-View") == "1" or b"data-quick-view" in response.content

    # signed out: the sign-in page, and any other page (sign in first)
    login = client.get(reverse("account_login"), **QUICK)
    assert not is_quick(login)
    assert not is_quick(client.get(reverse("reports:overview"), **QUICK))
    # the administration
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    assert not is_quick(client.get(reverse("admin:index"), **QUICK))
    assert not is_quick(client.get(reverse("account_change_password"), **QUICK))
    # a page marked not quick-viewable (Ask NeuroDB runs a script of its own): refused before its view runs
    refused = client.get(reverse("assistant:ask"), **QUICK)
    assert refused.status_code == 204 and refused["X-NeuroDB-Quick-View"] == "refused"
    client.logout()
    # a donor
    donor = make_donor(username="donor-sample", name="Sample Fund", donors=["Sample Fund"])
    client.force_login(donor.user)
    assert not is_quick(client.get(reverse("donors:page"), **QUICK))
    assert not is_quick(client.get(reverse("reports:overview"), **QUICK))


def test_a_quick_request_for_a_download_or_an_htmx_request_is_not_a_quick_view(client_viewer, world):
    # an htmx request of a page (its results area) keeps answering what it answered
    response = client_viewer.get(reverse("reports:programmes") + "?page=1", HTTP_HX_REQUEST="true", **QUICK)
    assert response.status_code == 200 and "X-NeuroDB-Quick-View" not in response
    assert "data-quick-view" not in text(response)
    # a download answers its file: never the quick view's header
    xlsx = client_viewer.get(reverse("reports:export_etools_locations"), **QUICK)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK" and "X-NeuroDB-Quick-View" not in xlsx


def test_base_html_is_a_thin_template_and_the_full_layout_loads_the_quick_view(client_viewer, world):
    base = (TEMPLATES / "base.html").read_text()
    code = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", base, flags=re.S).strip()
    assert code == '{% extends page_layout|default:"layouts/page.html" %}'
    assert (TEMPLATES / "layouts" / "page.html").is_file() and (
        TEMPLATES / "layouts" / "quick.html"
    ).is_file()
    html = text(client_viewer.get(reverse("reports:programmes")))
    script = re.search(
        r'<script type="module" src="([^"]*quickview[^"]*\.js)" nonce="([^"]+)"></script>', html
    )
    assert script, "the full page does not load the quick view"
    skip = re.search(r'<meta name="nd-quick-skip" content="([^"]*)">', html)
    assert skip
    prefixes = skip.group(1).split()
    assert f"/{settings.ADMIN_URL_PATH}" in prefixes and "/accounts/" in prefixes and "/donor/" in prefixes


def test_signed_out_and_donors_get_no_quick_view_script(client, roles):
    html = text(client.get(reverse("account_login")))
    assert "quickview" not in html and "nd-quick-skip" not in html


def test_the_guide_and_operations_say_how_the_quick_view_works():
    guide = (Path(settings.BASE_DIR) / "neurodb" / "help" / "guide" / "overview.md").read_text()
    assert (
        "Links open in a quick view; use **Open full page**, or Ctrl/Cmd-click to open the page in a new tab."
        in guide
    )
    ops = (Path(settings.BASE_DIR) / "docs" / "OPERATIONS.md").read_text()
    section = ops.split("## Quick view: links open in a pop-up\n", 1)[1].split("\n## ", 1)[0]
    for words in ("data-no-quick", "not_quick", "X-NeuroDB-Quick: 1", "assistant:ask", "fmm:report"):
        assert words in section, words
