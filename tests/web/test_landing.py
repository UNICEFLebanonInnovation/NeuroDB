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


def test_etools_counts_show_without_activityinfo(client, db):
    """From 2026 partners report in eTools: the figures come from there, still aggregate only."""
    import datetime

    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.create(etl_id="1", name="Amel Association", vendor_number="V1")
    pd = PCA.objects.create(etl_id="11", partner=partner, number="LEB/PD1", title="PSS", status="active")
    PCA.objects.create(etl_id="12", partner=partner, number="LEB/PD2", title="Old", status="closed")
    for n, (source, location) in enumerate([(100, "Akkar"), (100, "Bekaa"), (200, "Akkar")], start=1):
        dm.PDIndicator.objects.create(
            datamart_id=n, source_id=source, intervention=pd, title=f"# {source}", location_name=location
        )
    year = datetime.date.today().year
    for n, report in enumerate(["QPR1", "QPR1", "QPR2"], start=1):
        dm.ReportedIndicator.objects.create(
            datamart_id=n, intervention=pd, progress_report=report, period_end=datetime.date(year, 3 * n, 28)
        )
    html = client.get(reverse("landing")).content.decode()
    assert "programme documents running this year" in html and "partner progress reports this year" in html
    # As the overview counts them: 1 PD running this year (the closed one has no indicator) and its
    # partner, 2 indicators (one repeated per location), 2 progress reports.
    assert html.count('data-count="1"') == 2 and html.count('data-count="2"') == 2
    assert "ActivityInfo results tracked" not in html  # zero counts are left out
    assert "Amel Association" not in html and "LEB/PD1" not in html


def test_root_shows_overview_once_signed_in(client_viewer, hierarchy):
    html = client_viewer.get("/").content.decode()
    assert "Country overview" in html
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


def test_the_mock_up_is_marked_and_ask_is_promoted_only_when_on(client, hierarchy, settings):
    settings.AI_ASSISTANT_ENABLED = False
    html = client.get(reverse("landing")).text
    assert "Illustration of the overview, not real figures." in html
    assert "eTools synced last night" not in html  # no fixed claim: the real date, or nothing
    assert "Ask NeuroDB" not in html and "What does the AI do" not in html
    settings.AI_ASSISTANT_ENABLED = True
    assert "Ask NeuroDB" in client.get(reverse("landing")).text


def test_the_floating_sync_label_shows_the_real_last_sync(client, hierarchy):
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now() - timezone.timedelta(hours=3),
    )  # fmt: skip
    assert "eTools synced 3\xa0hours ago" in client.get(reverse("landing")).text
