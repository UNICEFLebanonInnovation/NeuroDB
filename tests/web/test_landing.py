"""Public landing page and the PUBLIC_PAGES allowlist."""

import pytest
from django.core.cache import cache
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def test_root_shows_landing_to_visitors(client, hierarchy):
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "in one place." in html
    assert 'id="why"' in html and 'id="faq"' in html
    # Aggregate counts only: 2 master indicators, 4 records, 2 partners, 2 governorates.
    assert 'data-count="4"' in html and 'data-count="2"' in html
    assert "Partner A" not in html  # no partner names leak to the public page


def test_root_shows_overview_once_signed_in(client_viewer, hierarchy):
    html = client_viewer.get("/").content.decode()
    assert "Programme overview" in html
    assert "in one place." not in html


def test_welcome_is_always_the_landing_page(client_viewer, hierarchy):
    html = client_viewer.get(reverse("landing")).content.decode()
    assert "Open NeuroDB" in html


def test_stats_can_be_switched_off(client, hierarchy, settings):
    settings.PUBLIC_LANDING_STATS = False
    html = client.get(reverse("landing")).content.decode()
    assert "data-count" not in html


def test_quick_links_lock_internal_pages(client, hierarchy):
    html = client.get(reverse("landing")).content.decode()
    assert f"{reverse('account_login')}?next={reverse('reports:library')}" in html
    assert "Sign in to open" in html


def test_support_email_adds_request_access(client, hierarchy, settings):
    settings.SUPPORT_EMAIL = "neurodb-support@example.org"
    html = client.get(reverse("landing")).content.decode()
    assert "mailto:neurodb-support@example.org" in html


def test_public_pages_open_only_what_is_listed(client, hierarchy, settings):
    assert client.get(reverse("reports:library")).status_code == 302
    settings.PUBLIC_PAGES = ["reports:library", "reports:library_download"]
    response = client.get(reverse("reports:library"))
    assert response.status_code == 200
    assert b"Sign in for dashboards" in response.content
    # Everything else stays behind sign-in, and public pages stay read-only.
    assert client.get(reverse("reports:population")).status_code == 302
    assert client.get(reverse("reports:programmes")).status_code == 302
    assert client.get(reverse("api:dashboard", args=[hierarchy["database"].id])).status_code in (
        302,
        401,
        403,
    )
    assert client.post(reverse("reports:library")).status_code == 302
    html = client.get(reverse("landing")).content.decode()
    assert f'href="{reverse("reports:library")}"' in html
