"""One failed eTools Datamart dataset is never the whole sync.

``sync_all`` writes one run per dataset and goes on after a failed one, so the job's latest run is only the
last dataset of the order (``datamart_etl_status``). Every reader of the job as a whole (Needs attention and
NeuroDB Watch, the daily review, the admin home's Data jobs, the Scheduled jobs list, the sync command's exit)
reads it dataset by dataset, from the latest finished run of each, and leaves the diagnostic datasets
(``catalogue.DIAGNOSTIC``, used by no page, figure or check) out of its alarms.
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
PARTIAL = SyncRun.Status.PARTIAL
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
def test_the_diagnostic_datasets_are_documents_no_page_reads():
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


def test_only_a_diagnostic_dataset_run_has_no_status_of_its_own():
    """No dataset counted has a finished run, only the table-refresh log's (its first "Sync this dataset again" on
    an empty database): the state says nothing, never Succeeded, and the readers show that run as it is."""
    SyncRun.objects.create(
        job=DATAMART,
        target="datamart_etl_status",
        status=FAILED,
        error=SERVER_ERROR,
        finished_at=timezone.now(),
    )
    state = datamart_sync_state()
    assert (state.counted, state.failed, state.diagnostic_failed) == (0, (), ("datamart_etl_status",))
    assert (state.status, state.label) == ("", "")


def test_a_dataset_that_wrote_with_errors_reads_succeeded_with_errors():
    """Its latest run partial (rows that could not be written) and none failed: the job is not plain Succeeded.
    A diagnostic dataset's errors are left out, as its failure is; a failed dataset comes first."""
    runs = sync_night()
    SyncRun.objects.filter(pk__in=[runs["partners"].pk, runs["etools_usage"].pk]).update(
        status=PARTIAL, rows_failed=3
    )
    state = datamart_sync_state()
    assert (state.failed, state.partial, state.named) == ((), ("partners",), ("partners",))
    assert (state.status, state.label) == (PARTIAL, "Succeeded with errors")
    assert state.runs_url().endswith("?job__exact=etools_datamart&target__in=partners")
    assert line() is None  # no Needs attention line for rows: Data health and the daily review name them

    again("grants", FAILED, error="boom")
    state = datamart_sync_state()
    assert (state.status, state.label, state.named) == (PARTIAL, "1 dataset failed", ("grants",))


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
    assert found.read == "Datasets of the eTools Datamart sync whose latest run failed"
    assert (found.value, found.numbers) == (1, {"run": runs["partners"].pk, "datasets_failed": 1})
    assert found.on == timezone.localdate(runs["partners"].started_at)


def test_every_dataset_failed():
    sync_night(*NAMES)
    found = line()
    assert found.text == "eTools Datamart sync: every dataset failed at its last run (50 datasets)."
    counted = ",".join(name for name in NAMES if name not in catalogue.DIAGNOSTIC)
    assert found.url == (
        reverse("admin:core_syncrun_changelist") + f"?job__exact=etools_datamart&target__in={counted}"
    )


def test_more_than_three_are_counted():
    runs = sync_night("agreements", "partners", "grants", "interventions")
    found = line()
    assert found.text == (
        "eTools Datamart sync: partners, interventions, agreements and 1 more failed at their last run "
        "(4 of 50 datasets)."
    )
    assert found.numbers == {"run": runs["grants"].pk, "datasets_failed": 4}  # the newest of the four


def test_several_failed_datasets_link_to_their_own_runs_alone(superadmin):
    """Not to every failed run of the job: the table-refresh log fails every night, and an old failure since
    followed by a success is no longer the point."""
    old = timezone.now() - datetime.timedelta(days=3)
    SyncRun.objects.create(job=DATAMART, target="agreements", status=FAILED, started_at=old, finished_at=old)
    runs = sync_night("partners", "grants", "datamart_etl_status")
    found = line()
    assert found.url == (
        reverse("admin:core_syncrun_changelist") + "?job__exact=etools_datamart&target__in=partners,grants"
    )
    listed = superadmin.get(found.url)
    assert listed.status_code == 200
    assert {run.target for run in listed.context["cl"].queryset} == {"partners", "grants"}
    assert {run.pk for run in listed.context["cl"].queryset} == {runs["partners"].pk, runs["grants"].pk}


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
    assert "Failing, but no page or check uses it: datamart_etl_status" in row and "Also" not in row

    again("partners", FAILED, error="boom")
    html = superadmin.get(reverse("admin:index")).content.decode()
    row = _jobs_row(html)
    assert "1 dataset failed" in row and "partners" in row
    box = html.split("Needs attention")[1].split("Quick actions")[0]
    assert "eTools Datamart sync: partners failed at its last run (1 of 50 datasets)." in box


def test_the_admin_home_says_each_note_plainly(superadmin):
    """Both diagnostic datasets failing: "them", not "it"; every dataset failing: "every dataset", not fifty names;
    a dataset that wrote with errors: named under "Succeeded with errors"."""
    runs = sync_night("datamart_etl_status", "etools_usage")
    row = _jobs_row(superadmin.get(reverse("admin:index")).content.decode())
    assert "Failing, but no page or check uses them: etools_usage, datamart_etl_status" in row

    SyncRun.objects.filter(pk=runs["partners"].pk).update(status=PARTIAL, rows_failed=3)
    row = _jobs_row(superadmin.get(reverse("admin:index")).content.decode())
    assert "Succeeded with errors" in row and "partners" in row

    SyncRun.objects.filter(job=DATAMART).update(status=FAILED)
    row = _jobs_row(superadmin.get(reverse("admin:index")).content.decode())
    assert "Failed" in row and "every dataset" in row and "partners" not in row


def test_only_a_diagnostic_dataset_run_shows_as_it_is(superadmin):
    """Never Succeeded when no dataset counted has run: the admin home, Scheduled jobs, the internal API and the
    help chat show the table-refresh log's run as it is."""
    from neurodb.help.tools import list_jobs
    from neurodb.reports import services

    SyncRun.objects.create(
        job=DATAMART,
        target="datamart_etl_status",
        status=FAILED,
        error=SERVER_ERROR,
        finished_at=timezone.now(),
    )
    row = _jobs_row(superadmin.get(reverse("admin:index")).content.decode())
    assert "Failed" in row and "Succeeded" not in row
    row = _scheduled_row(superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode())
    assert "Failed" in row and "Succeeded" not in row
    jobs = services.health_as_dict(services.data_health())["jobs"]
    assert next(j for j in jobs if j["job"] == "etools_datamart")["last_status"] == "failed"
    last = next(job for job in list_jobs()["jobs"] if job["job"] == "etools-datamart")["last_run"]
    assert last["status"] == "Failed" and "datasets_failed" not in last


def test_while_a_dataset_is_being_synced_the_job_reads_running_and_keeps_the_failure(superadmin):
    """The sync's latest run is still going: Data health's card says so above the datasets that failed; the admin
    home, Scheduled jobs and the help chat show that run as it is, Running."""
    from neurodb.help.tools import list_jobs
    from tests.web.parts import get_part, text

    sync_night("partners")
    again("grants", RUNNING)

    html = text(get_part(superadmin, "/data/health/", "jobs"))
    card = html[html.index('<h3 class="panel__title">eTools Datamart sync</h3>') :].split(
        '<div class="panel">'
    )[0]
    card = " ".join(re.sub(r"<[^>]+>", " ", card).split()).replace(" :", ":")
    assert "Latest run: Running" in card and "1 dataset failed partners" in card
    row = _jobs_row(superadmin.get(reverse("admin:index")).content.decode())
    assert "Running" in row and "1 dataset failed" not in row
    row = _scheduled_row(superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode())
    assert "Running" in row and "1 dataset failed" not in row
    last = next(job for job in list_jobs()["jobs"] if job["job"] == "etools-datamart")["last_run"]
    assert last["status"] == "Running" and "datasets_failed" not in last


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
    assert "?job__exact=etools_datamart&amp;target__in=partners" in row and "status__exact" not in row


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
