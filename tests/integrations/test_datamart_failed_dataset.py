"""One failed eTools Datamart dataset is never the whole sync.

``sync_all`` writes one run per dataset and goes on after a failed one, so the job's latest run is only the
last dataset of the order (``datamart_etl_status``). Every reader of the job as a whole (Needs attention and
NeuroDB Watch, the daily review, the admin home's Data jobs, the Scheduled jobs list, the sync command's exit)
reads it dataset by dataset, from the latest finished run of each, and leaves the diagnostic datasets
(``catalogue.DIAGNOSTIC``, read by nothing in NeuroDB) out of its alarms.
"""

from __future__ import annotations

import datetime
import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS
from neurodb.integrations.management.commands import sync_etools_datamart
from neurodb.reports.services import datamart_sync_state
from neurodb.watch import memory
from neurodb.watch.detectors import Context, system
from neurodb.watch.models import WatchItem
from neurodb.web import health

pytestmark = pytest.mark.django_db

DATAMART = SyncRun.Job.ETOOLS_DATAMART
SUCCEEDED, FAILED, RUNNING = SyncRun.Status.SUCCEEDED, SyncRun.Status.FAILED, SyncRun.Status.RUNNING
NAMES = list(ENTITY_SYNCS)
COUNTED = len([name for name in NAMES if name not in catalogue.DIAGNOSTIC])  # 50
MINUTE = datetime.timedelta(minutes=1)
KEY = "job_failed:etools_datamart"
ITEM = f"system:{KEY}"
# what the Datamart answered for its table-refresh log (system/monitor) on the night this was written
SERVER_ERROR = (
    "IntegrationError: GET /api/latest/system/monitor/: HTTP 500 Internal Server Error <!DOCTYPE HTML "
    '<html lang="en"><head><title>Server Error (500)</title></head><body><h1>Server Error (500)</h1>'
)


def sync_night(*failed: str, error: str = SERVER_ERROR) -> dict[str, SyncRun]:
    """One nightly sync: a run for every dataset, in sync order and a minute apart, ended before now; the
    datasets named in ``failed`` failed with ``error``, the others succeeded."""
    start = timezone.now() - (len(NAMES) + 5) * MINUTE
    runs = SyncRun.objects.bulk_create(
        SyncRun(
            job=DATAMART,
            target=name,
            status=FAILED if name in failed else SUCCEEDED,
            error=error if name in failed else "",
            started_at=start + n * MINUTE,
            finished_at=start + n * MINUTE + datetime.timedelta(seconds=30),
            triggered_by="schedule",
        )
        for n, name in enumerate(NAMES)
    )
    return {run.target: run for run in runs}


def again(name: str, status: str = SUCCEEDED, error: str = "") -> SyncRun:
    """One dataset synced alone after the night (``Only the datasets ticked below``); a running one has no end."""
    now = timezone.now()
    return SyncRun.objects.create(
        job=DATAMART,
        target=name,
        status=status,
        error=error,
        started_at=now,
        finished_at=None if status == RUNNING else now + datetime.timedelta(seconds=5),
    )


def line() -> health.Warning | None:
    return {found.key: found for found in health.warnings()}.get(KEY)


def watch() -> memory.Outcome:
    return memory.run(Context.make(), [system.SYSTEM_HEALTH])


@pytest.fixture
def superadmin(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    return client


# ------------------------------------------------------------------------------------- the diagnostic datasets
def test_the_diagnostic_datasets_are_documents_nothing_reads():
    """Kept whole for Ask NeuroDB and the read-only lists, in no table of their own a page reads, and not among
    the datasets Monitoring insights rebuilds from: a page or refresh that starts reading one fails here, and it
    must then come out of DIAGNOSTIC."""
    from neurodb.fmm.refresh import SOURCE_TARGETS

    assert catalogue.DIAGNOSTIC == {"datamart_etl_status", "etools_usage"}
    assert catalogue.DIAGNOSTIC <= set(catalogue.DOCUMENTS) <= set(ENTITY_SYNCS)
    assert catalogue.DIAGNOSTIC.isdisjoint(catalogue.TYPED)
    assert catalogue.DIAGNOSTIC.isdisjoint(SOURCE_TARGETS)
    assert "workspaces" not in catalogue.DIAGNOSTIC  # also the live business-area lookup of the PRP datasets


# --------------------------------------------------------------------------------------------- the state
def test_state_names_failed_datasets_in_sync_order_and_sets_diagnostic_apart():
    assert datamart_sync_state().status == "" and datamart_sync_state().label == ""  # nothing synced yet
    SyncRun.objects.create(job=DATAMART, target="a_dataset_no_longer_synced", status=FAILED)
    sync_night("datamart_etl_status", "grants", "etools_usage", "partners")

    state = datamart_sync_state()
    assert list(state.runs) == NAMES  # in sync order; the dropped dataset left out
    assert state.failed == ("partners", "grants")
    assert state.diagnostic_failed == ("etools_usage", "datamart_etl_status")
    assert state.counted == COUNTED == 50
    assert (state.status, state.label) == (SyncRun.Status.PARTIAL, "2 datasets failed")

    again("partners")
    again("grants")
    state = datamart_sync_state()
    assert state.failed == () and state.diagnostic_failed == ("etools_usage", "datamart_etl_status")
    assert (state.status, state.label) == (SUCCEEDED, "Succeeded")  # a diagnostic one is never a failure


def test_every_dataset_failing_reads_failed():
    sync_night(*NAMES)
    state = datamart_sync_state()
    assert (state.status, state.label) == (FAILED, "Failed") and len(state.failed) == COUNTED


# ------------------------------------------------------------------------- Needs attention and NeuroDB Watch
def test_a_failed_last_dataset_is_not_the_whole_sync():
    """The night's case: the last dataset of the order, the Datamart's own table-refresh log, answered HTTP 500
    and every other dataset was synced. Nothing needs attention and Watch opens nothing."""
    sync_night("datamart_etl_status")
    assert SyncRun.objects.filter(job=DATAMART).order_by("-started_at").first().status == FAILED

    assert line() is None
    assert not any("eTools Datamart sync" in found.text for found in health.warnings())
    watch()
    assert not WatchItem.objects.filter(key=ITEM).exists()


def test_a_failed_dataset_is_named_even_when_later_ones_succeeded():
    runs = sync_night("partners")
    found = line()
    assert found.text == "eTools Datamart sync: partners failed at its last run (1 of 50 datasets)."
    assert found.url == reverse("admin:core_syncrun_change", args=[runs["partners"].pk])
    assert found.read == "Latest run of each dataset of the eTools Datamart sync"
    assert (found.value, found.numbers) == (1, {"run": runs["partners"].pk, "datasets_failed": 1})
    assert found.on == timezone.localdate(runs["partners"].started_at)


def test_every_dataset_failed():
    sync_night(*NAMES)
    found = line()
    assert found.text == "eTools Datamart sync: every dataset failed at its last run (50 datasets)."
    assert found.url == (
        reverse("admin:core_syncrun_changelist") + "?job__exact=etools_datamart&status__exact=failed"
    )


def test_more_than_three_are_counted():
    runs = sync_night("agreements", "partners", "grants", "interventions")
    found = line()
    assert found.text == (
        "eTools Datamart sync: partners, interventions, agreements and 1 more failed at their last run "
        "(4 of 50 datasets)."
    )
    assert found.numbers == {"run": runs["grants"].pk, "datasets_failed": 4}  # the newest of the four


def test_a_dataset_synced_again_alone_clears_its_line():
    sync_night("partners")
    assert ITEM in watch().new
    again("partners")

    assert line() is None
    outcome = watch()
    item = WatchItem.objects.get(key=ITEM)
    assert ITEM in outcome.closed and item.state == WatchItem.State.CLOSED
    assert (item.close_reason, item.close_kind) == (system.FIXED, WatchItem.CloseKind.RESOLVED)


def test_a_running_dataset_keeps_its_last_finished_state():
    runs = sync_night("partners")
    watch()
    again("partners", RUNNING)  # the next sync is on it: its last finished run still failed

    assert line().numbers["run"] == runs["partners"].pk
    assert watch().closed == [] and WatchItem.objects.get(key=ITEM).state == WatchItem.State.OPEN


def test_another_dataset_succeeding_does_not_clear_it():
    sync_night("partners")
    watch()
    again("grants")  # the newest run of the job, and it succeeded

    assert line().text.startswith("eTools Datamart sync: partners failed at its last run")
    assert watch().closed == [] and WatchItem.objects.get(key=ITEM).state == WatchItem.State.OPEN


# ---------------------------------------------------------------------------------------- the daily review
def test_the_daily_review_leaves_out_the_diagnostic_datasets():
    from neurodb.core import years
    from neurodb.review import checks as review

    sync_night("datamart_etl_status", "etools_usage")
    assert review.check_sync_failures(review.Context(today=years.today())) == []

    again(
        "partners", FAILED, error="IntegrationError: GET /api/latest/datamart/partners/: HTTP 502 Bad Gateway"
    )
    (draft,) = review.check_sync_failures(review.Context(today=years.today()))
    assert draft.key == "sync_failures:etools_datamart"
    assert draft.title == "eTools Datamart sync: 1 failed and 0 partial run in the last 24 hours"
    assert "partners" in draft.detail and "datamart_etl_status" not in draft.detail
    assert "<!DOCTYPE" not in draft.detail and "monitor" not in draft.detail  # no HTML error reaches the AI
    assert not any("datamart_etl_status" in record for record in draft.evidence["records"])


# ----------------------------------------------------------------------------- the admin home and its jobs
JOBS_ROW = re.compile(
    r'<th scope="row">\s*<a class="nd-link" href="[^"]*\?job__exact=etools_datamart">.*?</tr>', re.S
)


def _jobs_row(html: str) -> str:
    row = JOBS_ROW.search(html)
    assert row, "no Data jobs row for the eTools Datamart sync"
    return row.group(0)


def test_the_admin_home_reads_the_job_as_its_datasets(superadmin):
    sync_night("datamart_etl_status")
    html = superadmin.get(reverse("admin:index")).content.decode()
    box = html.split("Needs attention")[1].split("Quick actions")[0]
    assert "The last eTools Datamart sync failed" not in box and "eTools Datamart sync: " not in box
    row = _jobs_row(html)
    assert "Succeeded" in row and "Failed" not in row
    assert "Also failing, nothing in NeuroDB reads it: datamart_etl_status" in row

    again("partners", FAILED, error="boom")
    html = superadmin.get(reverse("admin:index")).content.decode()
    row = _jobs_row(html)
    assert "1 dataset failed" in row and "partners" in row
    box = html.split("Needs attention")[1].split("Quick actions")[0]
    assert "eTools Datamart sync: partners failed at its last run (1 of 50 datasets)." in box


def _scheduled_row(html: str) -> str:
    rows = [row for row in re.findall(r"<tr\b.*?</tr>", html, re.S) if ">etools-datamart</a>" in row]
    assert len(rows) == 1, "no Scheduled jobs row for etools-datamart"
    return rows[0]


def test_the_scheduled_jobs_page_reads_the_datamart_job_as_its_datasets(superadmin):
    sync_night("datamart_etl_status")
    row = _scheduled_row(superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode())
    assert "Succeeded" in row and "Failed" not in row
    assert reverse("admin:core_syncrun_changelist") + "?job__exact=etools_datamart" in row

    again("partners", FAILED, error="boom")
    row = _scheduled_row(superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode())
    assert "1 dataset failed" in row and "Failed" not in row
    assert "?job__exact=etools_datamart&amp;status__exact=failed" in row


# ------------------------------------------------------------------------------------------- the command
def test_a_diagnostic_dataset_alone_does_not_fail_the_sync_command(monkeypatch, settings):
    """Nothing in the app reads the exit code, but the container log does: a failing table-refresh log is not
    a failed sync, while any other failed dataset still is."""
    settings.FMM_REFRESH_AFTER_SYNC = "off"
    outcome = {"grants": SUCCEEDED, "datamart_etl_status": FAILED}

    def fake_sync_all(*, only, triggered_by):
        runs = []
        for name in only:
            run = SyncRun.objects.create(job=DATAMART, target=name, triggered_by=triggered_by)
            run.finish(outcome[name], error=SERVER_ERROR if outcome[name] == FAILED else "")
            runs.append(run)
        return runs

    monkeypatch.setattr(sync_etools_datamart, "sync_all", fake_sync_all)
    out = StringIO()
    call_command("sync_etools_datamart", "--only", "grants,datamart_etl_status", stdout=out)
    assert "[datamart_etl_status] failed" in out.getvalue()  # still said in the log

    outcome["grants"] = FAILED
    with pytest.raises(CommandError, match=r"1 run\(s\) failed: etools_datamart\[grants\]"):
        call_command("sync_etools_datamart", "--only", "grants,datamart_etl_status", stdout=StringIO())
