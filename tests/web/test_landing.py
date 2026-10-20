"""Public landing page and the PUBLIC_PAGES allowlist."""

import pytest
from django.core.cache import cache
from django.urls import reverse

pytestmark = pytest.mark.django_db

LANDING_MARK = "A child is<br>never <em>just<br>a number.</em>"  # the landing page's heading
SECTIONS = ("top", "meaning", "impact", "data", "insights", "journey", "realworld", "access", "trust")
HONESTY_LABELS = (
    "SIMULATED DATA · NOT LIVE",
    "FICTIONAL EXAMPLE",
    "Illustrative UI · no actual internal data",
    "Illustrative interface · no live private data",
    "● DEMO",
    "Privacy by design: this public experience contains no child-level or private programme records.",
    "These are rules, not live counts.",
    "This illustration contains no live programme data or personal records.",
    "Illustrative workflow, not an actual child record or evidence of measured impact.",
    "Photo · UNICEF Lebanon story",
    "These photographs come from UNICEF Lebanon stories.",
    "Photographs from UNICEF Lebanon stories published on unicef.org; each card links to a related story.",
    "must not be presented as an outcome caused or measured by NeuroDB.",
    "© UNICEF2025/Fouad-Choufany/Lebanon",
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def test_root_shows_landing_to_visitors(client, hierarchy, activityinfo_in_use):
    """While ActivityInfo is in use its counts are public figures too (hierarchy's records stop in February:
    on the suite's day, Automatic makes it an archive, without them; see test_landing_archive.py)."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert LANDING_MARK in html
    for section in SECTIONS:
        assert f'id="{section}"' in html, section
    # Aggregate counts only: 2 master indicators, 4 records, 2 partners, 2 governorates.
    assert 'data-count="4"' in html and 'data-count="2"' in html
    assert "partners reporting in ActivityInfo this year" in html
    assert "Partner A" not in html  # no partner names leak to the public page


def test_etools_counts_show_without_activityinfo(client, db):
    """From 2026 partners report in eTools: the figures come from there, still aggregate only."""
    import datetime

    from django.utils import timezone

    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerOrganization

    partner = PartnerOrganization.objects.create(etl_id="1", name="Amel Association", vendor_number="V1")
    pd = PCA.objects.create(etl_id="11", partner=partner, number="LEB/PD1", title="PSS", status="active")
    PCA.objects.create(etl_id="12", partner=partner, number="LEB/PD2", title="Old", status="closed")
    for n, (source, location) in enumerate([(100, "Akkar"), (100, "Bekaa"), (200, "Akkar")], start=1):
        dm.PDIndicator.objects.create(
            datamart_id=n, source_id=source, intervention=pd, title=f"# {source}", location_name=location
        )
    year = timezone.localdate().year
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


def test_root_shows_a_staff_page_once_signed_in(client_viewer, hierarchy):
    """Signed in, the root is the staff Home (its own tests are tests/reports/test_home_page.py), never the
    landing page; the landing's button to it says "Open Home"."""
    html = client_viewer.get("/").content.decode()
    assert LANDING_MARK not in html and 'id="help-panel"' in html  # the staff shell, not the landing
    landing = client_viewer.get(reverse("landing")).content.decode()
    assert "Open Home" in landing and "Open your overview" not in landing


def test_welcome_is_always_the_landing_page(client_viewer, hierarchy):
    html = client_viewer.get(reverse("landing")).content.decode()
    assert "Open NeuroDB" in html


def test_stats_can_be_switched_off(client, hierarchy, settings):
    settings.PUBLIC_LANDING_STATS = False
    html = client.get(reverse("landing")).content.decode()
    assert "data-count" not in html


def test_footer_has_no_mailto_or_page_jump_links(client, hierarchy, settings):
    """The footer holds the logo and the public pages only: no team mailbox (even with SUPPORT_EMAIL set), no
    links back into the page, no UNICEF site link and no concept-page note."""
    settings.SUPPORT_EMAIL = "neurodb-support@example.org"
    html = client.get(reverse("landing")).content.decode()
    footer = html[html.index('<footer class="footer">') : html.index("</footer>")]
    assert "mailto:" not in html and "neurodb-support@example.org" not in html
    assert "Request access" not in footer and 'href="#' not in footer and "unicef.org" not in footer
    assert "Concept landing page" not in html and "<nav" not in footer


def test_user_guide_adds_a_footer_link(client, hierarchy, settings):
    assert "User guide" not in client.get(reverse("landing")).text
    settings.USER_GUIDE_URL = "https://example.org/guide"
    html = client.get(reverse("landing")).text
    assert 'href="https://example.org/guide" rel="noopener noreferrer" target="_blank">' in html
    assert "User guide" in html


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
    assert f'href="{reverse("reports:population")}"' not in html  # only what is listed


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


def test_the_honesty_labels_stay_with_the_ai_on_or_off(client, hierarchy, settings):
    """The design's labels saying what is simulated, illustrative or a preview stay as they are, with the AI
    assistant switched on or off; no fixed claim about a sync, no address of another site's sign-in. With the
    assistant off, the public page never offers it (the old page's guard: it promoted Ask only when on)."""
    for enabled in (False, True):
        settings.AI_ASSISTANT_ENABLED = enabled
        html = client.get(reverse("landing")).text
        for label in HONESTY_LABELS:
            assert label in html, (enabled, label)
        assert "eTools synced last night" not in html
        assert "azurewebsites" not in html
        if not enabled:
            for offer in ("Ask NeuroDB", "What does the AI do", "Ask a question", "/ask/"):
                assert offer not in html, offer


def test_the_floating_sync_label_shows_the_real_last_sync(client, hierarchy):
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="all", status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now() - timezone.timedelta(hours=3),
    )  # fmt: skip
    assert "eTools synced 3\xa0hours ago" in client.get(reverse("landing")).text
