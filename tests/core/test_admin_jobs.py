"""Every operator command has a button in the admin ("Run a job" on Import and sync runs)."""

from html import unescape

import pytest
from django.urls import reverse

from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.integrations import background

pytestmark = pytest.mark.django_db


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def superadmin(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    return client


def _url(name):
    return reverse(f"admin:core_syncrun_{name}")


def test_the_menu_lists_every_job(superadmin):
    html = superadmin.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert "Run a job" in html and "Sync eTools Datamart now" in html
    for title in [s.menu for s in BACKGROUND_JOBS] + [
        "Population figures",
        "Check freshness",
        "Repair roles",
    ]:
        assert str(title) in html


@pytest.mark.parametrize("spec", BACKGROUND_JOBS, ids=lambda s: s.name)
def test_background_jobs_start_their_command(superadmin, admin_user, started, spec):
    url = _url(spec.name)
    assert superadmin.get(url).status_code == 200 and started == []  # a GET only confirms
    assert superadmin.post(url, {"_form_submitted": "on"}).status_code == 302
    assert started == [(*spec.command, "--triggered-by", admin_user.username)]


def test_a_running_job_is_not_started_twice(superadmin, started, monkeypatch):
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: None)
    SyncRun.objects.create(job=SyncRun.Job.LOCATIONS, status=SyncRun.Status.RUNNING)
    response = superadmin.post(_url("run_sync_locations"), {"_form_submitted": "on"}, follow=True)
    assert started == [] and "already running" in response.content.decode()


def test_quick_jobs_run_in_the_request(superadmin, roles):
    response = superadmin.post(_url("run_bootstrap_roles"), {"_form_submitted": "on"}, follow=True)
    assert "Roles ready" in response.content.decode()

    response = superadmin.post(_url("run_check_freshness"), {"_form_submitted": "on"}, follow=True)
    assert "Out of date" in response.content.decode()  # nothing has ever synced in the test database

    response = superadmin.post(_url("run_reload_population"), {"_form_submitted": "on"}, follow=True)
    assert "Loaded" in response.content.decode() and PopulationFigure.objects.exists()


def test_viewers_cannot_run_jobs(client, viewer, started):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    for spec in BACKGROUND_JOBS:
        client.post(_url(spec.name), {"_form_submitted": "on"})
    client.post(_url("run_reload_population"), {"_form_submitted": "on"})
    assert started == [] and not PopulationFigure.objects.exists()


def test_database_imports_run_in_the_background(superadmin, admin_user, started, database):
    response = superadmin.post(
        reverse("admin:pivoting_database_changelist"),
        {"action": "queue_data_import", "_selected_action": [database.pk]},
    )
    assert response.status_code == 302
    assert started == [
        ("import_activityinfo_data", "--database", str(database.ai_id), "--triggered-by", admin_user.username)
    ]


# What the eTools Datamart answered for its table-refresh log (system/monitor) on the night this was written
SERVER_ERROR = (
    "IntegrationError: GET /api/latest/system/monitor/: HTTP 500 Internal Server Error <!DOCTYPE HTML "
    '<html lang="en"><head><title>Server Error (500)</title></head><body><h1>Server Error (500)</h1>'
)


@pytest.mark.parametrize(
    ("job", "error", "hint"),
    [
        ("etools", "GET /api/v2/partners/: HTTP 503 Service Unavailable", "eTools was unavailable"),
        ("etools", "GET /api/v2/partners/: HTTP 502 Bad Gateway", "eTools was unavailable"),
        (
            "etools_datamart",
            SERVER_ERROR,
            "eTools Datamart answered with a server error (HTTP 500), even after NeuroDB asked again. If the "
            "next run fails the same way, tell the team that runs eTools Datamart.",
        ),
        (  # not a status NeuroDB asks again for (integrations.http.RETRY_STATUSES)
            "etools_datamart",
            "GET /x: HTTP 501 Not Implemented",
            "eTools Datamart answered with a server error (HTTP 501). If the next run fails the same way, tell "
            "the team that runs eTools Datamart.",
        ),
        ("compiler_youth", "GET /x: HTTP 505 HTTP Version Not Supported", "tell the team that runs the data"),
        ("etools_datamart", "GET /api/latest/datamart/grants/: HTTP 429 Too Many Requests", "too many"),
        ("ai_data", "POST /resources/query: ReadTimeout", "ActivityInfo did not answer in time"),
        ("etools", "GET /api/v2/partners/: ConnectionError", "eTools could not be reached"),
        ("etools", "GET /api/v2/partners/: ConnectTimeout", "eTools could not be reached"),
        (
            "etools_datamart",
            "GET /api/latest/datamart/: HTTP 401 Unauthorized",
            "refused NeuroDB's credentials",
        ),
        ("etools", "a KeyError nobody foresaw", ""),
    ],
)
def test_known_errors_get_a_plain_hint(job, error, hint):
    from neurodb.core.admin import error_hint

    result = str(error_hint(SyncRun(job=job, error=error)))
    assert (hint in result) if hint else result == ""
    if any(f"HTTP {n}" in error for n in (500, 501, 505)):
        # a server error for this one request, not the service down; no blame and no promise it passes
        assert "unavailable" not in result and "its side" not in result and "passes" not in result
    if any(f"HTTP {n}" in error for n in (501, 505)):  # NeuroDB did not ask again for these
        assert "asked again" not in result


def test_triggered_by_reads_as_scheduler_manual_or_command():
    from neurodb.core.admin import triggered_by_label

    assert str(triggered_by_label("schedule")) == "Scheduler"
    assert str(triggered_by_label("command")) == "Command line"
    assert str(triggered_by_label("demo-admin")) == "Manual (demo-admin)"
    assert triggered_by_label("") == "—"


def test_the_run_list_explains_the_two_etools_syncs_and_its_errors(superadmin):
    SyncRun.objects.create(job="etools", status="failed", error="GET /x: HTTP 503 Service Unavailable")
    SyncRun.objects.create(job="population", status="succeeded", triggered_by="demo-admin")
    html = superadmin.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert "Two eTools syncs" in html and "Import and sync runs" in html and "Data and sync" in html
    assert "eTools was unavailable: try again later." in html and "HTTP 503" in html
    assert "Manual (demo-admin)" in html
    assert "Only this dataset" not in html  # the dataset note is the run page's alone


def _meaning(client, run: SyncRun) -> str:
    """The run page's "What the error means", as text."""
    html = unescape(client.get(reverse("admin:core_syncrun_change", args=[run.pk])).content.decode())
    return html[html.index("What the error means") :][:2500]


def test_the_run_page_says_only_this_dataset_stopped(superadmin):
    from neurodb.integrations.background import CUT_OFF

    etl = SyncRun.objects.create(
        job="etools_datamart", target="datamart_etl_status", status="failed", error=SERVER_ERROR
    )
    meaning = _meaning(superadmin, etl)
    assert "server error (HTTP 500)" in meaning and "was unavailable" not in meaning
    assert "Only this dataset (datamart_etl_status) stopped" in meaning
    # the last dataset of the order, and often synced alone: no "next one" is promised
    assert "a failed dataset does not stop the others" in meaning and "next one" not in meaning
    assert (
        "No page, figure or check uses this dataset (only Ask NeuroDB's general eTools look-ups can read it): "
        "it raises no alarm." in meaning
    )
    assert "Nothing in NeuroDB reads" not in meaning
    assert (
        "To sync it alone now, press “Sync this dataset again” above, or “Sync eTools Datamart now” on Import "
        "and sync runs, choose “Only the datasets ticked below, in their usual order” and tick "
        "datamart_etl_status." in meaning
    )

    partners = SyncRun.objects.create(
        job="etools_datamart",
        target="partners",
        status="failed",
        error="GET /x: HTTP 503 Service Unavailable",
    )
    meaning = _meaning(superadmin, partners)
    assert "Only this dataset (partners) stopped" in meaning and "No page, figure or check" not in meaning

    cut_off = SyncRun.objects.create(job="etools_datamart", target="partners", status="failed", error=CUT_OFF)
    rest = SyncRun.objects.create(job="etools", target="partners", status="failed", error=SERVER_ERROR)
    assert "Only this dataset" not in _meaning(superadmin, cut_off)  # the whole sync stopped then
    assert "Only this dataset" not in _meaning(superadmin, rest)  # not a Datamart dataset


def test_a_datamart_dataset_the_account_may_not_read_is_never_said_as_credentials(superadmin):
    """24 Oct 2026: audit/financial-findings-all answered 403 while the same account read about 40 other datasets:
    signed in (a sign-in refused is 401), not allowed this one. The run page says so, whom to ask, and that the
    next sync fails the same way until then; a 401 keeps the credentials' words."""
    from neurodb.core.admin import error_hint

    error = "IntegrationError: GET /api/latest/datamart/audit/financial-findings-all/: HTTP 403 Forbidden"
    run = SyncRun.objects.create(job="etools_datamart", target="audit_findings", status="failed", error=error)
    meaning = " ".join(_meaning(superadmin, run).split())
    assert (
        "The account NeuroDB uses for the eTools Datamart is not allowed to read audit/financial-findings-all: "
        "ask the eTools Datamart team to grant it." in meaning
    )
    assert "credentials" not in meaning and "renew" not in meaning
    assert (
        "The next sync tries it again, and it fails the same way until the eTools Datamart team grants the "
        "account access to it. Once they have, press “Sync this dataset again” above to sync it alone."
        in meaning
    )
    unauthorised = SyncRun(job="etools_datamart", target="audit_findings", error=error.replace("403", "401"))
    assert "refused NeuroDB's credentials" in str(error_hint(unauthorised))
    rest = SyncRun(job="etools", error="GET /api/v2/partners/: HTTP 403 Forbidden")
    assert "refused NeuroDB's credentials" in str(error_hint(rest))  # an eTools REST token, revoked


@pytest.fixture
def datamart_ready(monkeypatch):
    from neurodb.integrations.etools import datamart

    monkeypatch.setattr(datamart, "configured", lambda: True)
    monkeypatch.setattr(background, "is_running", lambda job: False)


def test_a_dataset_is_synced_again_from_its_run_page(superadmin, admin_user, started, datamart_ready):
    run = SyncRun.objects.create(
        job="etools_datamart", target="datamart_etl_status", status="failed", error=SERVER_ERROR
    )
    url = reverse("admin:core_syncrun_sync_dataset_again", args=[run.pk])
    page = superadmin.get(reverse("admin:core_syncrun_change", args=[run.pk])).content.decode()
    assert "Sync this dataset again" in page and url in page

    response = superadmin.post(url, {"_form_submitted": "on"})
    assert response.status_code == 302 and response["Location"] == reverse("admin:core_syncrun_changelist")
    assert started == [
        ("sync_etools_datamart", "--triggered-by", admin_user.username, "--only", "datamart_etl_status")
    ]


def test_only_a_finished_datamart_dataset_is_synced_again_and_only_by_an_administrator(
    superadmin, client, viewer, started, datamart_ready
):
    rest = SyncRun.objects.create(job="etools", target="partners", status="failed", error="boom")
    running = SyncRun.objects.create(job="etools_datamart", target="partners", status="running")
    gone = SyncRun.objects.create(job="etools_datamart", target="a_dataset_no_longer_synced", status="failed")
    for run in (rest, running, gone):
        page = superadmin.get(reverse("admin:core_syncrun_change", args=[run.pk])).content.decode()
        assert "Sync this dataset again" not in page, run.target
        url = reverse("admin:core_syncrun_sync_dataset_again", args=[run.pk])
        assert superadmin.post(url, {"_form_submitted": "on"}).status_code == 403
    assert started == []

    # an id that is no run's: the page says it does not exist (no error from the button's permission check)
    response = superadmin.get(reverse("admin:core_syncrun_change", args=["abc"]))
    assert response.status_code == 302 and response["Location"] == reverse("admin:index")
    url = reverse("admin:core_syncrun_sync_dataset_again", args=["abc"])
    assert superadmin.post(url, {"_form_submitted": "on"}).status_code == 403
    assert started == []

    failed = SyncRun.objects.create(job="etools_datamart", target="partners", status="failed", error="boom")
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    client.post(reverse("admin:core_syncrun_sync_dataset_again", args=[failed.pk]), {"_form_submitted": "on"})
    assert started == []
