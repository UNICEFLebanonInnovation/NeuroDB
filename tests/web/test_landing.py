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


def test_logo_and_favicon_are_the_neurodb_brand(client, hierarchy):
    html = client.get(reverse("landing")).content.decode()
    assert "img/logo.png" in html and "img/favicon.ico" in html and "og-image.png" in html
    response = client.get("/favicon.ico")
    assert response.status_code == 302 and response["Location"].endswith("img/favicon.ico")
    login = client.get(reverse("account_login")).content.decode()
    assert "img/logo.png" in login


def test_probes_bypass_host_check_and_https_redirect(client, db, settings):
    settings.ALLOWED_HOSTS = ["neurodb.example.org"]
    settings.SECURE_SSL_REDIRECT = True
    live = client.get("/healthz/live/", HTTP_HOST="10.0.0.12:8000")
    assert live.status_code == 200 and live.json()["status"] == "ok"
    ready = client.get("/healthz/", HTTP_HOST="10.0.0.12:8000")
    assert ready.status_code == 200 and ready.json()["database"] == "ok"
    # App Service's warm-up request on container start, sent to the internal address.
    warmup = client.get("/robots933456.txt", HTTP_HOST="169.254.137.2:8000")
    assert warmup.status_code == 200 and warmup.content == b""
    # Any other path still gets the normal protections.
    assert client.get("/", HTTP_HOST="10.0.0.12:8000").status_code == 400


def test_readiness_is_503_when_the_database_is_down(client, db, monkeypatch):
    from django.db import connection

    def broken_cursor(*args, **kwargs):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(connection, "cursor", broken_cursor)
    response = client.get("/healthz/")
    assert response.status_code == 503
    assert response.json()["database"] == "error: RuntimeError"
    assert client.get("/healthz/live/").status_code == 200
