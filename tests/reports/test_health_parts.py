"""Data health in parts (SPEC7 §3.3-3.4 for data health; STAGEA "Data health").

The page answers its frame at once (the page heading, the *Sync jobs* heading with its staleness note, the heading
and explanation of the datasets panel) with a placeholder per block, each its own computation: ``jobs`` (high),
``databases`` and ``runs`` (normal), ``datasets``, ``power-bi`` and ``readiness`` (low). These tests check the frame without any figure work, each part, who may open the page and
its parts, that the page put together from its parts is the page drawn at once (``_eager=1``), the two quick wins
of ``datamart_quality`` (the latest run of each dataset in one query; the row counts stored for every worker,
keyed on the jobs that write those tables) with the same figures as before, and that an edit shows at once.

On ``world``: one displayed ActivityInfo database of the current year, a failed eTools sync, an ActivityInfo import,
eTools Datamart runs of three datasets (one dataset run twice, its newer run started first, so the latest is told
by its start and not by its id), three grants and two places held, and a machine learning readiness check.
"""

from __future__ import annotations

import datetime
import json
import re

import pytest
from django.apps import apps
from django.core.cache import caches
from django.db import connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import query as dmq
from neurodb.donors.models import DonorAccount
from neurodb.geo.models import Location, LocationType
from neurodb.insights.management.commands.ml_readiness import run as run_readiness
from neurodb.integrations.etools import windows
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS
from neurodb.reports import services
from tests.web.parts import (
    HX,
    assemble,
    eager,
    executable,
    get_part,
    normalise,
    placeholder_attributes,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

PAGE = "/data/health/"  # reports:data_health
PARTS = (
    "jobs",
    "databases",
    "runs",
    "datasets",
    "power-bi",
    "readiness",
)  # the frame's placeholders, in page order
JSON_SCRIPT = re.compile(r'<script id="([^"]+)" type="application/json">(.*?)</script>', re.S)
LINK = re.compile(r'\s(?:href|action|hx-push-url)="([^"]*)"')
# Query ceilings: what was measured on this world, plus 2 (SPEC7 §3.3 6, §3.4). The frame warm (the navigation
# stored, as after the first visit of a page) is 5: the request's floor (session, user, donor check), the current
# year and the user's role (the sidebar, and the datasets placeholder's height); cold it also reads the navigation
# (4). Each part is measured with nothing stored: jobs reads the last success and the latest run of each of its 13
# jobs; datasets the latest run of every dataset (one query), the key of the stored row counts, the counts of the
# datasets that have a table, and the gazetteer's; power-bi the comparisons with Power BI's views, the views they
# name and the card versions (three queries, whatever the number of periods; two in this world, which has none).
# The focused menu has no ActivityInfo block, so its frame does not read the ActivityInfo state: jobs and
# databases then read it themselves (3: the setting, the scheduled jobs, the reporting years), counted here.
SHELL_QUERIES_COLD = (
    16  # 14 measured: a menu with the ActivityInfo block reads its state (3) and the sources (2)
)
SHELL_QUERIES_WARM = 7
PART_QUERIES = {"jobs": 34, "databases": 10, "runs": 6, "datasets": 60, "power-bi": 7, "readiness": 6}


def _place(pk: int, name: str, kind: LocationType) -> Location:
    return Location.objects.create(
        id=pk, name=name, p_code=f"LB{pk}", type=kind, lft=1, rght=2, level=0, tree_id=1
    )


def _run(job, target="", status=SyncRun.Status.SUCCEEDED, ago=0, **fields) -> SyncRun:
    """A finished run that started ``ago`` hours ago."""
    started = timezone.now() - datetime.timedelta(hours=ago)
    details, error = fields.pop("details", {}), fields.pop("error", "")
    run = SyncRun.objects.create(job=job, target=target, started_at=started, **fields)
    run.finish(status, error=error, **details)
    return run


@pytest.fixture
def world(database):
    database.last_monthly_update_date = timezone.now() - datetime.timedelta(days=3)
    database.save()
    _run(SyncRun.Job.ETOOLS, status=SyncRun.Status.FAILED, error="timeout")
    _run(SyncRun.Job.ACTIVITYINFO_DATA, target=str(database.ai_id), rows_in=40, rows_written=40, ago=2)
    datamart = SyncRun.Job.ETOOLS_DATAMART
    _run(datamart, "pd_indicators", rows_in=100, rows_written=100, ago=30)
    _run(
        datamart,
        "pd_indicators",
        status=SyncRun.Status.PARTIAL,
        rows_in=1200,
        rows_written=1180,
        rows_failed=20,
        ago=6,
        details={
            "not_linked": {"programme_document_fm": 7, "location": 3},
            "removed": 4,
            "errors": [{"count": 20, "error": "Upstream row could not be written"}],
        },
    )
    newer = _run(datamart, "grants", rows_in=3, rows_written=3, ago=1)  # created first, started last
    _run(datamart, "grants", rows_in=9, rows_written=9, ago=5)
    _run(datamart, "partners", details={"empty_response": True}, ago=3)
    _run(datamart, "a_dataset_no_longer_synced", ago=0)
    for n in range(3):
        dm.Grant.objects.create(datamart_id=n + 1, name=f"Grant {n}", donor="Donor")
    governorate = LocationType.objects.create(name="Governorate", admin_level=1)
    _place(11, "Akkar", governorate)
    _place(12, "Beirut", governorate)
    run_readiness("test")
    return {"database": database, "grants_run": newer, "governorate": governorate}


@pytest.fixture
def administrator(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    return client


def _heavy_services_refuse(monkeypatch):
    """Every figure service of the page raises: the frame must not need any of them."""

    def refuse(*args, **kwargs):
        raise AssertionError("the frame computed figures")

    for name in (
        "data_health",
        "health_jobs",
        "health_databases",
        "recent_runs",
        "datamart_quality",
        "latest_datamart_runs",
        "dataset_row_counts",
        "_dataset_rows",
        "_dataset_rows_key",
    ):
        monkeypatch.setattr(services, name, refuse)
    monkeypatch.setattr(SyncRun, "last_success", classmethod(refuse))
    monkeypatch.setattr(dmq, "dataset", refuse)


def _json_blocks(html: str) -> dict[str, object]:
    """The JSON data blocks of ``html`` (base.html's own list of script addresses, ``asset-urls``, left out)."""
    found = [(name, body) for name, body in JSON_SCRIPT.findall(html) if name != "asset-urls"]
    ids = [name for name, _ in found]
    assert len(ids) == len(set(ids)), f"a JSON id twice: {ids}"
    return {name: json.loads(body) for name, body in found}


def _dataset(html: str, name: str) -> str:
    """The row of one dataset in the datasets table."""
    row = re.search(rf'<tr>\s*<td><span class="mono">{name}</span>.*?</tr>', html, re.S)
    assert row, f"no row for {name}"
    return row.group(0)


def _rows_held(html: str, name: str) -> str:
    return re.findall(r'<td class="num">([^<]*)</td>', _dataset(html, name))[-1].strip()


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_answers_with_every_figure_service_refusing(client_viewer, world, monkeypatch):
    caches["shared"].clear()  # nothing stored
    _heavy_services_refuse(monkeypatch)
    response = client_viewer.get(PAGE)
    assert response.status_code == 200
    html = text(response)
    assert "<title>Data health" in html and "Sync runs and freshness of every source" in html
    assert (
        '<h2 class="section-heading mt-0" id="jobs-title">Sync jobs' in html
        and "Stale after 30 hours" in html
    )
    assert list(placeholders(html)) == list(PARTS)
    assert _json_blocks(html) == {}  # the page has no chart data, in the frame or anywhere
    for figure in ('id="db-health"', 'id="runs"', 'id="datasets"', 'id="ml-readiness"', "Never succeeded"):
        assert figure not in html, figure


@pytest.mark.shell
def test_the_placeholders_carry_the_resolved_query(client_viewer, world):
    """The page has no filter: each part's address is the page's own with ``_part`` alone, whatever the address
    bar holds (a stray key never reaches a part)."""
    for address in (PAGE, PAGE + "?section=Health&page=2"):
        found = placeholder_attributes(text(client_viewer.get(address)))
        assert list(found) == list(PARTS)
        for name, attrs in found.items():
            assert attrs["hx-get"] == f"{PAGE}?_part={name}", attrs["hx-get"]
    assert {name: attrs["data-part-priority"] for name, attrs in found.items()} == {
        "jobs": "high",
        "databases": "normal",
        "runs": "normal",
        "datasets": "low",
        "power-bi": "low",
        "readiness": "low",
    }
    assert not any("data-part-group" in attrs or "data-part-lazy" in attrs for attrs in found.values())
    # the room each block takes, as measured on the x20 parity world; the jobs' placeholder draws one grey card
    # per job card to come, in the cards' own grid (app.css), and keeps no height of its own
    jobs = found["jobs"]
    assert jobs["aria-labelledby"] == "jobs-title" and jobs["class"] == "part part--tiles"
    count = sum(1 for job in SyncRun.Job if job not in services.HEALTH_SKIPPED_JOBS)
    assert jobs["style"] == f"--part-rows: {count}" and count == len(services.health_jobs()) == 13
    html = text(client_viewer.get(PAGE))
    start = html.index('id="part-jobs"')
    assert html[start : html.index('id="part-databases"')].count("skeleton__block--tile") == count
    assert found["databases"]["style"] == "--part-h: 326.6px; --part-h-sm: 451.2px"
    assert found["runs"]["style"] == "--part-h: 618px"
    # the datasets table, which scrolls inside 560 px at every width, under its Copy and CSV buttons (39 px; its
    # panel's text is the frame's)
    assert found["datasets"]["style"] == "--part-h: 599px"
    assert found["readiness"]["style"] == "--part-h: 546.5px; --part-h-sm: 1431.6px; --part-h-md: 859.4px"


@pytest.mark.shell
def test_the_datasets_panel_s_text_is_in_the_frame_so_its_room_is_the_table_s_alone(
    viewer, admin_user, world
):
    """The datasets panel's explanation wraps at its own widths (and is longer for an administrator, whose copy
    links to the reporting checks): drawn in the frame, it reads the same before and after the table arrives, and
    the placeholder keeps the room of the table and its buttons, the same for everyone (up to round 1 the readiness
    block below moved 20 px when the table came, at windows of 768 to 1,100 px)."""
    reporting_checks = reverse("admin:datamart_reporting_checks")
    admin_user.is_superuser = True
    admin_user.save()
    for user, administrator_s in ((admin_user, True), (viewer, False)):
        client = Client()
        client.force_login(user)
        frame = text(client.get(PAGE))
        panel = frame[frame.index("eTools Datamart datasets") : frame.index('id="part-datasets"')]
        assert "Read this before trusting a number on an eTools page." in panel
        assert (reporting_checks in panel) == administrator_s
        assert f"{len(ENTITY_SYNCS)} rows" in panel  # the table's count, known without reading it
        assert placeholder_attributes(frame)["datasets"]["style"] == "--part-h: 599px"
    part = text(get_part(client, PAGE, "datasets"))
    assert (
        "Read this before trusting" not in part
        and 'max-height: 560px"><table class="table table-sm" id="datasets">' in part
    )


TABLE_BUTTON = re.compile(r'<button\b[^>]*\sdata-table-(copy|csv)\b[^>]*\sdata-table-target="#([^"]+)"')


@pytest.mark.shell
def test_the_datasets_copy_and_csv_buttons_come_with_their_table(client_viewer, world):
    """Copy and CSV look for the table they name when they are pressed (app.js), and do nothing when it is not
    there: drawn in the frame, they would do nothing while the datasets table loads, nor ever when its part fails
    (up to round 2 they did, beside the part's error box). So no Copy or CSV button of the frame names a table the
    frame does not hold; the datasets part brings both buttons, above the table they copy, and the page drawn at
    once has each of them once."""
    frame = text(client_viewer.get(PAGE))
    for kind, target in TABLE_BUTTON.findall(frame):
        assert f'id="{target}"' in frame, f"the frame's {kind} button names #{target}, held by a part"
    part = text(get_part(client_viewer, PAGE, "datasets"))
    assert sorted(TABLE_BUTTON.findall(part)) == [("copy", "datasets"), ("csv", "datasets")]
    assert part.index("data-table-copy") < part.index('<table class="table table-sm" id="datasets">')
    assert 'data-filename="datasets"' in part  # the file is datasets.csv, as before
    page = text(eager(client_viewer, PAGE))
    together = [found for found in TABLE_BUTTON.findall(page) if found[1] == "datasets"]
    assert sorted(together) == [("copy", "datasets"), ("csv", "datasets")]


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(client_viewer, world, django_assert_max_num_queries):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(PARTS)
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        response = client_viewer.get(PAGE)
    assert response.status_code == 200 and list(placeholders(text(response))) == list(PARTS)


# ------------------------------------------------------------------------------------------------- the parts
EXPECTED = {
    "jobs": ("eTools Datamart sync", "Never succeeded", "Latest run"),
    "databases": ('id="db-health"', "Child Protection", "AI 202618"),
    "runs": ('id="runs"', "pd_indicators", "1,200 / 1,180", "20 failed", "timeout"),
    "datasets": (
        'id="datasets"',
        "(field monitoring) 7",
        "4 removed",
        "check the country name",
    ),
    "readiness": ('id="ml-readiness"', "Ready for machine learning?", "Which indicators or activities"),
    # no comparison stored in this world: each programme says it is not compared yet (tests/education/
    # test_power_bi_pages.py draws one)
    "power-bi": ('id="power-bi-mscc"', 'id="power-bi-youth"', "Not yet compared"),
}


@pytest.mark.parametrize("name", PARTS)
def test_each_part_answers_its_wrapper(client_viewer, world, name):
    response = get_part(client_viewer, PAGE, name)  # 200 and the #part-<name> wrapper
    html = text(response)
    assert response["Cache-Control"] == "no-store" and response["X-NeuroDB-Part"] == name
    assert "<html" not in html and 'id="sidebar"' not in html
    for expected in EXPECTED[name]:
        assert expected in html, (name, expected)
    assert _json_blocks(html) == {}  # no chart data in any part
    assert executable(html) == []  # nothing that would run once swapped in (SPEC7 §1.3 rule 3)


def test_the_tables_draw_their_figures(client_viewer, world):
    """The assembled page and the eager page draw the same part templates, so their equivalence cannot catch a
    wrong figure in one: the datasets' failed rows and the databases' last import are checked here."""
    datasets = text(get_part(client_viewer, PAGE, "datasets"))
    row = _dataset(datasets, "pd_indicators")
    assert "1,200 / 1,180" in row and "4 removed" in row  # read / written
    assert '<td class="num small text-danger">20</td>' in row  # failed
    assert '<td class="num small">0</td>' in _dataset(datasets, "grants")  # none failed
    databases = text(get_part(client_viewer, PAGE, "databases"))
    row = re.search(r"<tr data-href=.*?AI 202618.*?</tr>", databases, re.S).group(0)
    assert re.search(r'<td class="small" data-value="[^"]+">3(?:\s|&nbsp;|\xa0)days ago</td>', row), row


def test_a_prp_dataset_names_its_business_area_and_an_empty_read_points_to_the_code(client_viewer, world):
    """The PRP datasets carry no country name: an empty read names the business area code it was read with
    and where to check it, never the country name; one that found only another business area says so; a
    failed one shows its error; a read shorter than the Datamart announced says nothing was removed."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    prp = {"business_area": "2490", "business_area_from": "admin", "read": 0, "documents": 0}
    _run(datamart, "prp_indicator_reports", details={**prp, "empty_response": True})
    _run(
        datamart,
        "prp_programme_documents",
        details={**prp, "read": 2, "empty_response": True, "other_country_skipped": 2},
    )
    error = (
        "IntegrationError: enter the business area code in Admin → Data and sync → eTools Datamart: PRP "
        "business area code (none is set for 'Lebanon', and no Datamart workspace of that name has one), or "
        "ask the hosting team to set ETOOLS_DATAMART_BUSINESS_AREA"
    )
    _run(datamart, "prp_progress_reports", status=SyncRun.Status.FAILED, error=error)
    _run(
        datamart,
        "grants",
        status=SyncRun.Status.PARTIAL,
        rows_in=2,
        rows_written=2,
        details={"removed": 0, "removal_skipped": True, "read": 2, "datamart_count": 4},
    )
    datasets = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    row = _dataset(datasets, "prp_indicator_reports")
    assert "the Datamart returned nothing for business area 2490" in row
    assert "Admin → Data and sync → eTools Datamart: PRP business area code" in row
    assert "press Run a job → Probe a Datamart dataset" in row and "run Run a job" not in row
    assert "business area 2490, entered in the admin" in row and "country name" not in row
    row = _dataset(datasets, "prp_programme_documents")
    assert "every record the Datamart returned names another business area than 2490 (2)" in row
    assert "of another country skipped" not in row and "2 read" in row
    assert error[:200] in _dataset(datasets, "prp_progress_reports")  # the pointer is never cut short
    row = _dataset(datasets, "grants")
    assert "read 2 records of the 4 the Datamart announced: none was removed" in row
    assert "business area" not in row  # a dataset read by country names none
    # the typed datasets keep their country-name wording
    assert "the Datamart returned nothing: check the country name" in _dataset(datasets, "partners")


def test_a_prp_run_that_found_no_code_points_to_the_admin_field_on_data_health_and_the_checks(
    client_viewer, world
):
    """The three PRP runs fail when the hosting team's value is a name and NeuroDB finds no code: Data health
    (240 characters of the error) and checks 8, 9 and 11 (140) still say where to enter the code."""
    from html import unescape

    from neurodb.datamart import checks
    from neurodb.integrations.etools import datamart_sync as sync

    where = "Admin → Data and sync → eTools Datamart: PRP business area code"
    error = "IntegrationError: " + sync._no_code_error(
        sync.AreaCode("", "", {"environment": "Lebanon"}), "Lebanon"
    )
    run = _run(
        SyncRun.Job.ETOOLS_DATAMART, "prp_indicator_reports", status=SyncRun.Status.FAILED, error=error
    )
    datasets = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    assert where in _dataset(datasets, "prp_indicator_reports")
    said, _tone = checks._feed_state({"run": run, "details": {}}, listed=False)
    assert where in said


def test_a_prp_read_warns_of_records_naming_no_business_area_a_read_cut_short_and_records_twice(
    client_viewer, world
):
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    prp = {"business_area": "2490", "business_area_from": "admin", "documents": 3}
    _run(datamart, "prp_progress_reports", status=SyncRun.Status.PARTIAL, rows_in=3, rows_written=3,
         details={**prp, "read": 3, "country_unnamed": 3, "business_areas_seen": {"(none)": 3}})  # fmt: skip
    _run(datamart, "prp_indicator_reports",
         details={**prp, "documents": 0, "read": 0, "empty_response": True, "datamart_count": 500})  # fmt: skip
    _run(datamart, "prp_programme_documents", status=SyncRun.Status.PARTIAL, rows_in=3, rows_written=3,
         details={**prp, "read": 2, "removal_skipped": True, "duplicates_read": 1, "datamart_count": 3})  # fmt: skip
    datasets = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    row = _dataset(datasets, "prp_progress_reports")
    assert "3 records name no business area: they were kept, but nothing confirms" in row
    assert "press Run a job → Probe a Datamart dataset" in row
    row = _dataset(datasets, "prp_indicator_reports")
    assert "announced 500 records for business area 2490 but returned none" in row
    assert "sync the dataset again" in row and "returned nothing for business area" not in row
    row = _dataset(datasets, "prp_programme_documents")
    assert "returned 1 record twice while its pages were read" in row and "none was removed" in row


def test_data_health_says_a_business_area_that_is_not_a_code(client_viewer, world):
    """Production's run: the Datamart was asked for business area "Lebanon" (the hosting team's value): Data
    health says it is a name, not a code, and that the next sync sets it aside. A later run that found its code
    and read nothing points to the admin's code; the value set aside is named; a round count says the Datamart
    limited its answer and nothing was removed."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    _run(datamart, "prp_indicator_reports", details={"business_area": "Lebanon", "business_area_from": "environment",
         "read": 0, "documents": 0, "empty_response": True, "datamart_count": 0})  # fmt: skip
    _run(datamart, "prp_programme_documents", status=SyncRun.Status.PARTIAL,
         details={"business_area": "2490", "business_area_from": "workspace_held", "business_area_found": True,
                  "business_area_set_aside": {"environment": "Lebanon"}, "read": 0, "documents": 0,
                  "empty_response": True, "datamart_count": 0})  # fmt: skip
    _run(datamart, "partner_reports", status=SyncRun.Status.PARTIAL, rows_in=10_000, rows_written=10_000,
         details={"removed": 0, "removal_skipped": True, "cap_suspected": "round_count", "read": 10_000,
                  "datamart_count": 10_000})  # fmt: skip
    datasets = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    row = _dataset(datasets, "prp_indicator_reports")
    assert (
        "the Datamart was asked for business area “Lebanon”, a name, not a business area code (digits, as 2490), "
        "so it found nothing: the next sync sets it aside and uses the code NeuroDB finds, or the one entered in "
        "Admin → Data and sync → eTools Datamart: PRP business area code"
    ) in row
    assert "the Datamart returned nothing for business area" not in row
    row = _dataset(datasets, "prp_programme_documents")
    assert (
        "the Datamart returned nothing for business area 2490, a code NeuroDB found (found in the workspace "
        "NeuroDB holds): enter the right code in Admin → Data and sync → eTools Datamart: PRP business area code "
        "(it comes first), or press Run a job → Probe a Datamart dataset"
    ) in row
    assert "ETOOLS_DATAMART_BUSINESS_AREA “Lebanon” set aside: not a business area code" in row
    row = _dataset(datasets, "partner_reports")
    assert (
        "the Datamart announced exactly 10,000 records, a limit of its answer, not what it holds: none was removed"
    ) in row
    assert "read 10,000 records of the 10,000" not in row


def test_data_health_shows_windows_and_a_cap(client_viewer, world):
    """A partner reports read in windows: how many, what they announced and read, a count the Datamart cut but
    the windows read whole; a read whose window was not whole, a count that changed, an answer cut that could
    not be read in windows, and a large removal held one night."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    _run(datamart, "partner_reports", rows_in=31_240, rows_written=31_240,
         details={"removed": 3, "read": 31_240, "datamart_count": 10_000, "window_bound": "reporting_period_start_date",
                  "windows_read": 14, "windows_announced": 31_240, "windows_not_whole": 0,
                  "whole_count_capped": True, "whole_count_end": 10_000})  # fmt: skip
    _run(datamart, "pd_indicators", status=SyncRun.Status.PARTIAL, rows_in=9_500, rows_written=9_500,
         details={"removed": 0, "removal_skipped": True, "read": 9_500, "datamart_count": 10_000, "window_bound": "id",
                  "windows_read": 3, "windows_announced": 10_000, "windows_not_whole": 2,
                  "windows_not_whole_first": [{"from": "2026-01-01", "to": "2026-04-01", "announced": 5_000,
                                               "read": 4_500}],
                  "whole_count_end": 10_000})  # fmt: skip
    _run(datamart, "fm_questions", status=SyncRun.Status.PARTIAL, rows_in=10_000, rows_written=10_000,
         details={"documents": 10_000, "documents_removed": 0, "removal_skipped": True, "read": 10_000,
                  "datamart_count": 10_000, "cap_suspected": "windows_unproven", "window_bound": ""})  # fmt: skip
    _run(datamart, "action_points", status=SyncRun.Status.PARTIAL, rows_in=6_000, rows_written=6_000,
         details={"removed": 0, "removal_skipped": True, "read": 6_100, "datamart_count": 6_000,
                  "window_bound": "id", "windows_read": 2, "windows_announced": 6_000, "windows_not_whole": 0,
                  "whole_count_end": 6_100, "changed": True})  # fmt: skip
    _run(datamart, "tpm_activities", status=SyncRun.Status.PARTIAL, rows_in=200, rows_written=200,
         details={"removed": 0, "removal_held": 1_800, "datamart_count": 200})  # fmt: skip
    datasets = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    row = _dataset(datasets, "partner_reports")
    assert "read in 14 windows: the Datamart announced 31,240, NeuroDB read 31,240" in row
    assert (
        "the Datamart announced exactly 10,000 records, a limit of its answer, not what it holds: NeuroDB read it in "
        "windows" in row
    )
    assert "none was removed" not in row
    row = _dataset(datasets, "pd_indicators")
    assert (
        "2 windows not read whole (1 Jan 2026 → 1 Apr 2026: 5,000 announced, 4,500 read): none was removed; the "
        "next complete read removes those it no longer returns" in row
    )
    row = _dataset(datasets, "fm_questions")
    assert (
        "the Datamart announced exactly 10,000 records, a limit of its answer, not what it holds: none was removed, "
        "and it could not be read in windows (the Datamart ignores the window filters): tell the eTools Datamart team"
    ) in row
    row = _dataset(datasets, "action_points")
    assert "the Datamart's count changed during the read (6,000, then 6,100): none was removed" in row
    row = _dataset(datasets, "tpm_activities")
    assert (
        "1,800 rows the Datamart no longer returns are kept one more night: more than half of those held; the first "
        "whole read a night later removes them" in row
    )


def test_data_health_says_why_a_read_was_windowed_a_cut_page_and_the_business_area_filter(
    client_viewer, world
):
    """A read windowed because the last one looked cut says why; a page that came back shorter than the first says
    the answer was cut, and that the next read tries windows only when it is of 5,000 records or more (a
    smaller one is read plain again); the ends test of a plain read says the next read tries windows, and only
    a read that tried windows and fell back to one answer says the Datamart ignores the window filters; the
    partner reports and PRP's progress reports say which business area filter they were read with, or why
    none, and the records of another business area a filter let through."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    proven = {
        "key": "business_area",
        "code": "2490",
        "why": "proven",
        "unfiltered": 780_905,
        "held": 9_982,
        "tried": [{"key": "business_area", "verdict": "proven", "count": 9_990}],
        "strays": 0,
    }
    _run(datamart, "partner_reports", rows_in=9_990, rows_written=9_990,
         details={"removed": 0, "read": 9_990, "datamart_count": 9_990, "window_bound": "reporting_period_start_date",
                  "windows_read": 3, "windows_announced": 9_990, "windows_not_whole": 0, "windows_why": "pages_cut",
                  "area_filter": proven})  # fmt: skip
    _run(datamart, "pd_indicators", status=SyncRun.Status.PARTIAL, rows_in=98, rows_written=98,
         details={"removed": 0, "removal_skipped": True, "read": 98, "datamart_count": 100, "pages_cut": 2})  # fmt: skip
    _run(datamart, "fm_questions", status=SyncRun.Status.PARTIAL, rows_in=5_999, rows_written=5_999,
         details={"documents": 5_999, "documents_removed": 0, "removal_skipped": True, "read": 5_999,
                  "datamart_count": 6_000, "pages_cut": 1})  # fmt: skip
    _run(datamart, "action_points", status=SyncRun.Status.PARTIAL, rows_in=7_001, rows_written=7_001,
         details={"removed": 0, "removal_skipped": True, "read": 7_001, "datamart_count": 7_001,
                  "cap_suspected": "ends_missing", "ends_missing": 1})  # fmt: skip
    _run(datamart, "tpm_activities", status=SyncRun.Status.PARTIAL, rows_in=7_001, rows_written=7_001,
         details={"removed": 0, "removal_skipped": True, "read": 7_001, "datamart_count": 7_001,
                  "cap_suspected": "ends_missing", "ends_missing": 1, "window_bound": "",
                  "windows_why": "cap_suspected", "count_requests": 5})  # fmt: skip
    strays = {"key": "business_area_code", "code": "2490", "why": "proven", "strays": 3, "tried": []}
    _run(datamart, "prp_progress_reports", status=SyncRun.Status.PARTIAL, rows_in=1_590, rows_written=1_590,
         details={"documents": 1_590, "documents_removed": 0, "removal_skipped": True, "read": 1_593,
                  "datamart_count": 1_593, "area_filter": strays, "area_strays": 3, "business_area": "2490",
                  "business_area_from": "admin"})  # fmt: skip
    datasets = " ".join(unescape(text(get_part(client_viewer, PAGE, "datasets"))).split())
    row = _dataset(datasets, "partner_reports")
    assert (
        "read in 3 windows: the Datamart announced 9,990, NeuroDB read 9,990 (a page of the last read came back "
        "shorter than the first)" in row
    )
    assert "read by business area 2490 (filter business_area: 9,990 records with it, 780,905 without)" in row
    row = _dataset(datasets, "pd_indicators")
    assert (
        "2 pages came back shorter than the first while the Datamart's answer was read: it was cut and none was "
        "removed; the next complete read removes those it no longer returns" in row
    )  # 100 records: the next read is plain, never windowed
    assert "windows" not in row
    assert "read 98 records of the 100" not in row  # the cut page says why nothing was removed
    row = _dataset(datasets, "fm_questions")
    assert (
        "1 page came back shorter than the first while the Datamart's answer was read: it was cut, none was "
        "removed, and the next read tries windows" in row
    )
    row = _dataset(datasets, "action_points")  # a plain read, with the ends test: no window filter was asked
    assert (
        "the Datamart's last record by id is not among those it returned: its answer was cut, none was removed, "
        "and the next read tries windows" in row
    )
    assert "ignores the window filters" not in row
    row = _dataset(datasets, "tpm_activities")  # windowed, no window filter proven: read once
    assert (
        "the Datamart's last record by id is not among those it returned: its answer was cut, none was removed, "
        "and it could not be read in windows (the Datamart ignores the window filters): tell the eTools Datamart "
        "team" in row
    )
    row = _dataset(datasets, "prp_progress_reports")
    assert (
        "3 records of another business area came through the business_area_code filter: none was written and none "
        "removed; the next read tries the next filter" in row
    )
    assert "read by business area 2490 (filter business_area_code)" in row


def test_data_health_says_pages_shifted_or_records_held_twice(client_viewer, world):
    """Records that came twice: with no stable order asked, the counts cannot tell pages that shifted from rows
    the Datamart holds twice, so both are said (21 Oct 2026: PRP's progress reports, 74 twice and as many
    missing); in a stable order, the Datamart likely holds them twice. A run made before it was recorded keeps
    the older words."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    _run(datamart, "prp_progress_reports", status=SyncRun.Status.PARTIAL, rows_in=1_590, rows_written=1_590,
         details={"documents": 1_590, "removal_skipped": True, "read": 150_221, "datamart_count": 150_295,
                  "duplicates_read": 74, "pages_shifted": {"twice": 74, "missing": 74}, "twice_identical": True,
                  "business_area": "2490", "business_area_from": "admin"})  # fmt: skip
    _run(datamart, "partner_reports", status=SyncRun.Status.PARTIAL, rows_in=9_982, rows_written=9_982,
         details={"removed": 0, "removal_skipped": True, "read": 780_902, "datamart_count": 780_905,
                  "duplicates_read": 3, "held_twice": 3, "ordering": {"key": "id"}})  # fmt: skip
    datasets = " ".join(unescape(text(get_part(client_viewer, PAGE, "datasets"))).split())
    row = _dataset(datasets, "prp_progress_reports")
    assert (
        "74 records came twice (identical) while the Datamart's pages were read: rows moved between pages, and "
        "then as many others did not come, or the Datamart holds them twice. Nothing was removed; the next read "
        "asks a stable order." in row
    )
    assert "(rows moving between pages)" not in row  # the older words, for runs before pages_shifted
    row = _dataset(datasets, "partner_reports")
    assert (
        "The Datamart returned 3 records twice in a stable order (by id): it likely holds them twice; tell the "
        "eTools Datamart team. Nothing was removed." in row
    )


def test_data_health_says_the_datamart_s_own_copies_and_a_dataset_it_may_not_read(client_viewer, world):
    """Records the Datamart sent twice, every copy in one answer, are its own copies: the read was whole, so the
    words are a note, not a warning (25 Oct 2026: reports/indicators, 1,939 short night after night). A dataset
    the Datamart does not let NeuroDB's account read (24 Oct 2026: audit/financial-findings-all, 403) says so and
    whom to ask, above the error's text."""
    from html import unescape

    datamart = SyncRun.Job.ETOOLS_DATAMART
    _run(datamart, "cp_indicators", rows_in=6_960, rows_written=6_960,
         details={"documents": 6_960, "datamart_count": 8_899, "duplicates_read": 1_939, "twice_identical": True,
                  "copies_proven": {"twice": 1_939, "same_page": 1_935, "asked_again": 4},
                  "confirm_requests": 4})  # fmt: skip
    error = "IntegrationError: GET /api/latest/datamart/audit/financial-findings-all/: HTTP 403 Forbidden"
    _run(datamart, "audit_findings", status=SyncRun.Status.FAILED, error=error)
    html = unescape(text(get_part(client_viewer, PAGE, "datasets")))
    row = " ".join(_dataset(html, "cp_indicators").split())
    assert (
        '<span class="cell-sub text-muted">The Datamart sent 1,939 records twice (identical), every copy in one of '
        "its answers (1,935 on one page, 4 found together again in one more request): it holds them twice, as one "
        "answer never lists a row twice. NeuroDB keeps one of each.</span>" in row
    )
    assert "none was removed" not in row and "text-warning" not in row
    row = " ".join(_dataset(html, "audit_findings").split())
    assert (
        "The account NeuroDB uses for the eTools Datamart is not allowed to read audit/financial-findings-all: ask "
        "the eTools Datamart team to grant it." in row
    )


@pytest.mark.parametrize("name", PARTS)
def test_each_part_stays_within_its_query_ceiling(client_viewer, world, name, django_assert_max_num_queries):
    raw_get(client_viewer, PAGE)  # the navigation stored, as after the frame
    caches["shared"].clear()
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(f"{PAGE}?_part={name}", **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


@pytest.mark.parametrize("name", PARTS)
def test_a_donor_is_refused_every_part(client, world, name):
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="European Union", donors=["EU"], must_change_password=False)
    client.force_login(user)
    assert client.get(f"{PAGE}?_part={name}", **HX).status_code == 403
    page = raw_get(client, PAGE)  # and the page itself, as before: back to the donor page
    assert page.status_code == 302 and page["Location"] == reverse("donors:page")


@pytest.mark.parametrize("name", PARTS)
def test_a_visitor_is_sent_to_sign_in(client, world, name):
    page = raw_get(client, PAGE)
    assert page.status_code == 302 and page["Location"].startswith(reverse("account_login"))
    part = client.get(f"{PAGE}?_part={name}", **HX)
    assert part["HX-Redirect"] == reverse("account_login") + "?next=/"  # HX-Current-URL is the site root


def test_every_signed_in_member_of_staff_opens_the_page(client, world, viewer, admin_user):
    from django.contrib.auth.models import Group

    from neurodb.accounts.roles import SECTION_EDITOR

    editor = User.objects.create_user(username="editor", password="editor-pass-123456")
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    no_role = User.objects.create_user(username="no-role", password="no-role-pass-123456")
    for user in (viewer, admin_user, editor, no_role):
        client.force_login(user)
        response = client.get(PAGE)  # assembled: every part answered
        assert response.status_code == 200, user.username
        assert set(response.parts) == set(PARTS) and "Ready for machine learning?" in text(response)


# --------------------------------------------------------------------------------------- one page, two drawings
MATRIX = [
    ("viewer", ""),
    ("viewer", "?section=Health&page=2"),  # keys the page does not read
    ("administrator", ""),  # the reporting checks link
    ("viewer", "empty"),  # nothing synced yet, no database, no readiness check
]


@pytest.mark.parametrize(("who", "query"), MATRIX)
def test_the_assembled_page_is_the_eager_page(client, viewer, admin_user, world, who, query):
    if query == "empty":
        SyncRun.objects.all().delete()
        world["database"].delete()
        query = ""
    if who == "administrator":
        admin_user.is_superuser = True
        admin_user.save()
        client.force_login(admin_user)
    else:
        client.force_login(viewer)
    assembled = assemble(client, PAGE + query)
    assert list(assembled.parts) == list(PARTS)
    assert all(response.status_code == 200 for response in assembled.parts.values())
    drawn = eager(client, PAGE + query)
    assert drawn.status_code == 200
    # under every entry: no link of a part (nor of the eager page) carries _part, and no part sends anything that
    # would run once swapped in
    for name, response in assembled.parts.items():
        links = LINK.findall(text(response))
        assert all("_part" not in link for link in links), (name, links)
        assert executable(text(response)) == [], name
    assert all("_part" not in link for link in LINK.findall(text(drawn)))
    assert placeholders(text(drawn)) == {}  # every part drawn
    assert normalise(assembled.html) == normalise(text(drawn))
    # no JSON in any part, so none twice either, and the same as the eager page's
    assert _json_blocks(assembled.html) == _json_blocks(text(drawn)) == {}
    reporting_checks = reverse("admin:datamart_reporting_checks")
    assert (reporting_checks in assembled.html) == (who == "administrator")


@pytest.mark.shell
def test_without_javascript_the_page_drawn_at_once_is_complete(client_viewer, world):
    """*Show the figures* (no JavaScript) leads to the page drawn at once: every block, no grey shapes, no
    placeholder, and no link back to itself."""
    frame = text(client_viewer.get(PAGE))
    shown = re.findall(r'<noscript><a href="([^"]+)">Show the figures</a></noscript>', frame)
    assert shown and set(shown) == {"?_eager=1"}  # one per placeholder, all the same page
    page = text(client_viewer.get(PAGE + shown[0]))
    assert placeholders(page) == {} and "part__skeleton" not in page and "Show the figures" not in page
    assert "_eager=1" not in "".join(LINK.findall(page))
    for name in PARTS:
        assert f'<div id="part-{name}" class="part is-loaded" data-part-loaded="{name}"' in page
    for expected in (
        "Never succeeded",
        'id="db-health"',
        "1,200 / 1,180",
        "4 removed",
        "Ready for machine learning?",
    ):
        assert expected in page, expected


# ------------------------------------------------------------------------------------- the eTools Datamart table
def _quality_before(names=ENTITY_SYNCS) -> list[dict]:
    """``datamart_quality`` as it was before this page loaded in parts (63d9646): every Datamart run read, newest
    first, and every dataset counted at each call; with the keys added since: a failed run's own error, the
    diagnostic mark, a PRP dataset's business area code and its source, what a read got against the count
    the Datamart announced (records returned twice, an empty answer cut short, a round count) and the PRP
    records that named no business area, a code asked that is not one, the values set aside and a code NeuroDB
    found, a read in windows (how many, why, those not whole, a cut count, a changed count, a removal held), a
    page cut (and whether a cut sign tries windows on the next read, or a read tried them already), the business area filter a read took (or why none) with the records it let through, and records
    that came twice (the Datamart's own copies, proven; pages that shifted, or records held twice in a stable
    order) with the order asked, and the words of a dataset the Datamart does not let NeuroDB read (403)."""
    from neurodb.datamart.catalogue import DIAGNOSTIC
    from neurodb.integrations.etools.datamart_sync import (
        FOUND,
        area_words,
        is_area_code,
        not_a_code,
        shift_words,
    )

    last_by_target: dict[str, SyncRun] = {}
    for run in SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART).order_by("-started_at"):
        last_by_target.setdefault(run.target, run)
    rows = []
    for name in names:
        run = last_by_target.get(name)
        details = (run.details or {}) if run else {}
        if name in services.LEGACY_TABLES:
            count = apps.get_model(services.LEGACY_TABLES[name]).objects.count()
        else:
            try:
                count = dmq.dataset(name).base.count()
            except Exception:
                count = None
        not_linked = {
            services.NOT_LINKED_LABELS.get(k, k): v for k, v in (details.get("not_linked") or {}).items()
        }
        code = str(details.get("business_area") or "")
        shifted, ordering = details.get("pages_shifted") or {}, details.get("ordering") or {}
        rows.append(
            {
                "name": name,
                "run": run,
                "rows": count,
                "not_linked": not_linked,
                "not_linked_total": sum(int(v) for v in not_linked.values()),
                "removed": details.get("removed"),
                "empty_response": bool(details.get("empty_response")),
                "other_country_skipped": details.get("other_country_skipped"),
                "other_country_removed": details.get("other_country_removed"),
                "errors": (details.get("errors") or [])[:3],
                "relinked": details.get("relinked"),
                "error": run.error if run and run.status == "failed" and not details.get("errors") else "",
                "not_allowed": "",  # no run of the world failed with a 403 of the Datamart
                "diagnostic": name in DIAGNOSTIC,
                "business_area": details.get("business_area") or "",
                "business_area_from": services.BUSINESS_AREA_FROM.get(details.get("business_area_from"), ""),
                "business_area_not_code": not_a_code(code) if code and not is_area_code(code) else "",
                "business_area_set_aside": "; ".join(
                    f"{services.SET_ASIDE_FROM.get(k, k)} “{v}”"
                    for k, v in (details.get("business_area_set_aside") or {}).items()
                ),
                "business_area_found": details.get("business_area_from") in FOUND,
                "read": details.get("read"),
                "datamart_count": details.get("datamart_count"),
                "removal_skipped": bool(details.get("removal_skipped")),
                "duplicates_read": details.get("duplicates_read"),
                "copies_proven": (details.get("copies_proven") or {}).get("twice"),
                "pages_shifted_twice": shifted.get("twice"),
                "pages_shifted_missing": shifted.get("missing"),
                "held_twice": details.get("held_twice"),
                "ordering_key": ordering.get("key") or "",
                "ordering_why": ordering.get("why") or "",
                "shift_words": shift_words(details),
                "cap_suspected": details.get("cap_suspected") or "",
                "windows_read": details.get("windows_read"),
                "windows_announced": details.get("windows_announced"),
                "windows_not_whole": details.get("windows_not_whole"),
                "windows_not_whole_first": [
                    {
                        "from": services._window_bound(w.get("from")),
                        "to": services._window_bound(w.get("to")),
                        "announced": w.get("announced"),
                        "read": w.get("read"),
                    }
                    for w in (details.get("windows_not_whole_first") or [])[:3]
                ],  # fmt: skip
                "whole_count_capped": bool(details.get("whole_count_capped")),
                "whole_count_end": details.get("whole_count_end"),
                "changed": details.get("whole_count_end") is not None
                and details.get("whole_count_end") != details.get("datamart_count"),
                "windows_why": services.WINDOWS_WHY.get(details.get("windows_why"), ""),
                "pages_cut": details.get("pages_cut"),
                "windows_next": isinstance(details.get("datamart_count"), int)
                and details["datamart_count"] >= windows.WINDOW_RECORDS,
                "windowed": "window_bound" in details,
                "area_filter": details.get("area_filter")
                if isinstance(details.get("area_filter"), dict)
                else {},
                "area_words": area_words(details.get("area_filter")),
                "area_strays": details.get("area_strays"),
                "removal_held": details.get("removal_held"),
                "cut_short": bool(details.get("empty_response"))
                and isinstance(details.get("datamart_count"), int)
                and details["datamart_count"] > (details.get("read") or 0),
                "country_unnamed": details.get("country_unnamed"),
                "unnamed_skipped": details.get("unnamed_skipped"),
            }
        )
    return rows


def _cache_rows() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM neurodb_cache")
        return cursor.fetchone()[0]


def _figures(rows: list[dict]) -> list[dict]:
    return [{**row, "run": (row["run"].pk if row["run"] else None)} for row in rows]


def test_the_datasets_table_reads_the_same_figures_as_before(world):
    before = _figures(_quality_before())
    assert _figures(services.datamart_quality()) == before  # nothing stored yet
    assert _figures(services.datamart_quality()) == before  # the row counts stored
    by_name = {row["name"]: row for row in services.datamart_quality()}
    assert by_name["grants"]["run"] == world["grants_run"]  # the latest by its start, not by its id
    assert by_name["grants"]["rows"] == 3 and by_name["locations"]["rows"] == 2
    assert by_name["pd_indicators"]["run"].rows_in == 1200 and by_name["pd_indicators"]["removed"] == 4
    assert by_name["partners"]["empty_response"] and by_name["agreements"]["run"] is None
    # the other readers of data_health() (the daily review, Ask NeuroDB, the internal API) get what they did
    health = services.data_health()
    assert set(health) == {
        "datasets",
        "jobs",
        "runs",
        "databases",
        "staleness_hours",
        "stale_database_days",
        "year",
        "activityinfo_archived",  # its jobs and databases an archive while its import is not followed
        "activityinfo_import_on",  # its import's scheduled job still on (an archive does not stop it)
        "power_bi",  # per Compiler programme, its latest period compared with Power BI's view (counts only)
    }
    assert _figures(health["datasets"]) == before
    assert [j["job"] for j in health["jobs"]] == [j["job"] for j in services.health_jobs()]
    assert health["runs"] == list(SyncRun.objects.order_by("-started_at")[:50])
    assert [d["database"] for d in health["databases"]] == [world["database"]]


def test_the_latest_run_of_every_dataset_is_one_query(world, django_assert_num_queries):
    with django_assert_num_queries(1):
        latest = services.latest_datamart_runs(ENTITY_SYNCS)
    assert set(latest) == {"pd_indicators", "grants", "partners"}  # a dataset no longer synced is left out


def test_the_row_counts_are_stored_and_read_back(world, monkeypatch):
    counted = []
    real = services._dataset_rows

    def counting(name):
        counted.append(name)
        return real(name)

    monkeypatch.setattr(services, "_dataset_rows", counting)
    first = services.dataset_row_counts()
    assert len(counted) == len(ENTITY_SYNCS)
    counted.clear()
    compute.reset_memo()
    assert services.dataset_row_counts() == first
    assert counted == list(services.DATASET_ROWS_LIVE)  # the gazetteer alone, at each visit
    found, stored = compute.peek(services._dataset_rows_key(), lang_neutral=True)
    # plain numbers, the same for every user and language: nothing in them is anyone's own view
    assert found and set(stored) == set(ENTITY_SYNCS) - set(services.DATASET_ROWS_LIVE)
    assert all(value is None or type(value) is int for value in stored.values())


@pytest.mark.parametrize(
    "job",
    [SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.ETOOLS, SyncRun.Job.LOCATIONS, SyncRun.Job.DATAMART_BACKFILL],
)
def test_a_run_of_a_job_that_writes_the_tables_counts_again(world, job):
    before = services.dataset_row_counts()
    dm.Grant.objects.create(datamart_id=99, name="Grant written by the run", donor="Donor")
    compute.reset_memo()
    assert services.dataset_row_counts()["grants"] == before["grants"] == 3  # stored: no run since
    run = SyncRun.objects.create(job=job, target="grants")  # a run starts ...
    compute.reset_memo()
    assert services.dataset_row_counts()["grants"] == 4
    dm.Grant.objects.create(datamart_id=100, name="Another grant", donor="Donor")
    run.finish(SyncRun.Status.SUCCEEDED)  # ... and ends
    compute.reset_memo()
    assert services.dataset_row_counts()["grants"] == 5


def test_a_run_of_another_job_keeps_the_stored_counts(world, monkeypatch):
    services.dataset_row_counts()
    _run(SyncRun.Job.DAILY_REVIEW, target="2026-10-13")
    compute.reset_memo()
    monkeypatch.setattr(
        services, "_dataset_rows", lambda name: 0 if name in services.DATASET_ROWS_LIVE else None
    )
    assert services.dataset_row_counts()["grants"] == 3  # read back, not counted again


def test_a_visit_in_another_language_reads_the_stored_counts(client_viewer, world, monkeypatch):
    english = _rows_held(text(get_part(client_viewer, PAGE, "datasets", HTTP_ACCEPT_LANGUAGE="en")), "grants")
    counted = []
    real = services._dataset_rows
    monkeypatch.setattr(services, "_dataset_rows", lambda name: counted.append(name) or real(name))
    compute.reset_memo()
    french = text(get_part(client_viewer, PAGE, "datasets", HTTP_ACCEPT_LANGUAGE="fr"))
    assert _rows_held(french, "grants") == english == "3"
    assert counted == list(services.DATASET_ROWS_LIVE)


IN_THE_DATABASE = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "shared": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "neurodb_cache"},
}


@override_settings(CACHES=IN_THE_DATABASE)
def test_inside_a_transaction_the_counts_are_made_without_the_shared_cache(world):
    """NeuroDB Watch's AI look-ups read data health inside a read-only transaction: with nothing stored, the
    rows are counted there and nothing is written (``once`` takes no lock and stores nothing inside a
    transaction, and its read of the cache table deletes nothing); the figures are the same."""
    assert connection.in_atomic_block  # the test's own transaction
    stored = _cache_rows()
    with transaction.atomic():
        rows = services.datamart_quality()
    assert _figures(rows) == _figures(_quality_before())
    assert _cache_rows() == stored  # nothing written (nor deleted)


@override_settings(CACHES=IN_THE_DATABASE)
def test_a_read_only_tool_run_reads_the_stored_counts(world, monkeypatch):
    """The data freshness look-up of NeuroDB Watch runs in a read-only transaction (``assistant.tools.read_only``):
    there it reads the counts a visit stored, instead of counting every table again."""
    from neurodb.assistant import tools

    counts = {name: 99 for name in ENTITY_SYNCS if name not in services.DATASET_ROWS_LIVE}
    full = compute._full_key(services._dataset_rows_key(), True)
    # stored by a visit (another worker), here in the test's own transaction, where once() itself stores nothing
    caches["shared"].set(compute._storage_key(full), compute._encode(counts), 60)
    compute.reset_memo()
    counted = []
    real = services._dataset_rows
    monkeypatch.setattr(services, "_dataset_rows", lambda name: counted.append(name) or real(name))
    with tools.read_only():
        rows = services.dataset_row_counts()
        assert SyncRun.objects.count() > 0  # the transaction still answers
    assert rows["grants"] == 99 and rows["locations"] == 2  # the stored count; the gazetteer counted
    assert counted == list(services.DATASET_ROWS_LIVE)


def _syncrun_rows_read(sql: str) -> int:
    """About how many rows of core_syncrun the query ``sql`` reads (EXPLAIN ANALYZE gives a whole number per loop),
    with the table's statistics up to date and the sequential scan off, as on a table of thousands of runs."""
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE core_syncrun")  # inside the test's transaction: rolled back with it
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
        plan = cursor.fetchone()[0]
        cursor.execute("SET LOCAL enable_seqscan = on")
    plan = json.loads(plan) if isinstance(plan, str) else plan
    read, nodes = 0, [plan[0]["Plan"]]
    while nodes:
        node = nodes.pop()
        nodes.extend(node.get("Plans", []))
        if node.get("Relation Name") == "core_syncrun":
            per_loop = node["Actual Rows"] + node.get("Rows Removed by Filter", 0)
            read += per_loop * node["Actual Loops"]
    return read


@pytest.mark.parametrize(
    "reader", ["latest_datamart_runs", "latest_finished_datamart_runs", "_dataset_rows_key"]
)
def test_the_latest_runs_are_read_from_the_index_however_many_runs_there_are(world, reader):
    """Both reads run at every visit of the datasets part: each takes the newest run from an index, never reading
    and sorting every run (a year of Datamart runs is about 19,000). The latest finished run of each dataset (the
    sync read dataset by dataset, ``datamart_sync_state``) is read the same way."""
    started = timezone.now() - datetime.timedelta(days=2)
    SyncRun.objects.bulk_create(
        SyncRun(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target="grants",
            status=SyncRun.Status.SUCCEEDED,
            started_at=started - datetime.timedelta(hours=n),
            finished_at=started - datetime.timedelta(hours=n) + datetime.timedelta(minutes=1),
        )
        for n in range(300)
    )
    with CaptureQueriesContext(connection) as queries:
        if reader == "latest_datamart_runs":
            services.latest_datamart_runs(ENTITY_SYNCS)
        elif reader == "latest_finished_datamart_runs":
            services.latest_datamart_runs(ENTITY_SYNCS, finished=True)
        else:
            services._dataset_rows_key()
    [query] = queries.captured_queries
    assert _syncrun_rows_read(query["sql"]) < 60  # a few per dataset or per job and status, of 300+ runs


def test_runs_started_at_the_same_moment_are_told_apart_by_their_id(world):
    moment = timezone.now() + datetime.timedelta(minutes=1)  # after every run of the world
    finished = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="grants", started_at=moment, status=SyncRun.Status.SUCCEEDED
    )
    running = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="grants", started_at=moment)
    assert running.pk > finished.pk
    assert services.latest_datamart_runs(["grants"])["grants"] == running
    assert f"etools_datamart={running.pk}.running.-" in services._dataset_rows_key()


def test_finished_runs_skip_a_running_one(world):
    """A dataset being synced keeps its last finished state (the sync read dataset by dataset)."""
    moment = timezone.now() + datetime.timedelta(minutes=1)  # after every run of the world
    finished = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="grants", started_at=moment, status=SyncRun.Status.FAILED
    )
    running = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="grants", started_at=moment + datetime.timedelta(minutes=1)
    )
    assert services.latest_datamart_runs(["grants"])["grants"] == running
    assert services.latest_datamart_runs(["grants"], finished=True)["grants"] == finished
    assert services.datamart_sync_state().failed == ("grants",)


SERVER_ERROR = (
    "IntegrationError: GET /api/latest/system/monitor/: HTTP 500 Internal Server Error <!DOCTYPE HTML"
)


def _card(html: str, label: str) -> str:
    """One job's card in the jobs part."""
    start = html.index(f'<h3 class="panel__title">{label}</h3>')
    end = html.find('<div class="panel">', start)
    return html[start : end if end > 0 else None]


def test_a_failed_diagnostic_dataset_is_shown_and_never_called_the_whole_sync(client_viewer, world):
    """The night's case: the Datamart's own table-refresh log, the last dataset of the order, answered HTTP 500.
    Its row says why and that no page uses it; the job card says it apart, with no "Latest run: Failed"; the
    internal API reads the sync by its datasets (the world's pd_indicators wrote with errors: partial). A real
    dataset failing makes the sync partial, by name."""
    _run(SyncRun.Job.ETOOLS_DATAMART, "datamart_etl_status", status=SyncRun.Status.FAILED, error=SERVER_ERROR)

    datasets = text(get_part(client_viewer, PAGE, "datasets"))
    row = _dataset(datasets, "datamart_etl_status")
    assert "HTTP 500 Internal Server Error" in row and "diagnostic: no page or check uses it" in row
    assert "diagnostic" not in _dataset(datasets, "partners") and "HTTP 500" not in _dataset(
        datasets, "grants"
    )
    card = _card(text(get_part(client_viewer, PAGE, "jobs")), "eTools Datamart sync")
    assert "Failing, but no page or check uses it: datamart_etl_status" in card
    assert "Succeeded with errors" in card and "pd_indicators" in card
    assert "Latest run" not in card and "Failed" not in card
    datamart = next(
        j for j in services.health_as_dict(services.data_health())["jobs"] if j["job"] == "etools_datamart"
    )
    assert (datamart["last_status"], datamart["failed_datasets"]) == ("partial", [])

    _run(SyncRun.Job.ETOOLS_DATAMART, "partners", status=SyncRun.Status.FAILED, error="boom")
    card = _card(text(get_part(client_viewer, PAGE, "jobs")), "eTools Datamart sync")
    assert "1 dataset failed" in card and "partners" in card and "pd_indicators" not in card
    datamart = next(
        j for j in services.health_as_dict(services.data_health())["jobs"] if j["job"] == "etools_datamart"
    )
    assert (datamart["last_status"], datamart["failed_datasets"]) == ("partial", ["partners"])
    rest = next(j for j in services.health_as_dict(services.data_health())["jobs"] if j["job"] == "etools")
    assert (rest["last_status"], rest["failed_datasets"]) == ("failed", [])  # other jobs as before

    # every dataset failing (expired credentials): said so, not every name
    SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART).update(status=SyncRun.Status.FAILED)
    caches["shared"].clear()
    card = _card(text(get_part(client_viewer, PAGE, "jobs")), "eTools Datamart sync")
    assert "Failed" in card and "every dataset" in card and "partners" not in card


# ------------------------------------------------------------------------------------------- fresh after edit
def test_a_place_removed_in_the_admin_shows_at_once(administrator, world):
    """The gazetteer is the one counted table staff change by hand (Admin → Locations), outside any job: the
    datasets part counts it at each visit, though the other counts were stored a moment before."""
    assert _rows_held(text(get_part(administrator, PAGE, "datasets")), "locations") == "2"
    akkar = Location.objects.get(name="Akkar")
    response = administrator.post(
        reverse("admin:locations_location_delete", args=[akkar.pk]), {"post": "yes"}
    )
    assert response.status_code == 302 and not Location.objects.filter(pk=akkar.pk).exists()
    assert _rows_held(text(get_part(administrator, PAGE, "datasets")), "locations") == "1"


def test_a_finished_datamart_run_shows_at_once_in_the_datasets_part(client_viewer, world):
    """The row counts are stored for an hour at most, keyed on the jobs that write the tables: a Datamart run that
    ends shows its rows and its figures at once."""
    before = text(get_part(client_viewer, PAGE, "datasets"))
    assert _rows_held(before, "grants") == "3" and "3 / 3" in _dataset(before, "grants")
    for n in range(4):
        dm.Grant.objects.create(datamart_id=50 + n, name=f"Synced grant {n}", donor="Donor")
    _run(SyncRun.Job.ETOOLS_DATAMART, "grants", rows_in=7, rows_written=7)
    after = text(get_part(client_viewer, PAGE, "datasets"))
    assert _rows_held(after, "grants") == "7" and "7 / 7" in _dataset(after, "grants")
