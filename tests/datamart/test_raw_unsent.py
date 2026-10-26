"""The partner report rows eTools no longer sends on Raw eTools data (datamart.raw, raw_admin.unsent_view): their
counts, their cell, the "updated" choices, the CSV column, the trail's words, the review list and the superusers'
button that removes them, recorded as the eTools update clean-up, never as a sync. Every value is made up."""

from __future__ import annotations

import csv
import datetime
import io

import psycopg
import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VALIDATORS, VIEWER
from neurodb.core import compute
from neurodb.core.admin import ScheduledJobAdmin
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import backfill, keys, raw, runfacts, trail
from neurodb.datamart import models as dm
from neurodb.integrations import background
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports.services import health_jobs, quality_row

pytestmark = pytest.mark.django_db

NAME = "Person One"  # a person a row names: never on the page
SOURCE = reverse("admin:datamart_raw_source", args=["partner_reports"])
LAST_SENT = datetime.datetime(2026, 9, 20, 10, 0, tzinfo=datetime.UTC)
MARKED = datetime.datetime(2026, 10, 14, 1, 0, tzinfo=datetime.UTC)


def _user(username: str, *, groups: tuple[str, ...], superuser: bool = False) -> User:
    if superuser:
        return User.objects.create_superuser(
            username=username, email=f"{username}@example.org", password="pass-123456-x"
        )
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="pass-123456-x", is_staff=True
    )
    user.groups.set(Group.objects.filter(name__in=groups))
    return user


def _sync(**details) -> SyncRun:
    now = timezone.now()
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        started_at=now - datetime.timedelta(hours=2),
        finished_at=now - datetime.timedelta(hours=1),
        rows_in=1,
        details={"datamart_count": 1, "read": 1, **details},
    )


def _row(pk: int, *, pd=None, ref: str = "LEB/PD1", number: str = "QPR1", end=datetime.date(2026, 3, 31),
         mark=MARKED, sent=LAST_SENT, place: str = "Beirut") -> dm.ReportedIndicator:  # fmt: skip
    row = dm.ReportedIndicator.objects.create(
        datamart_id=pk,
        intervention=pd,
        pd_reference_number=ref,
        progress_report=f"PR-{number}-{end}",
        report_number=number,
        report_type="QPR",
        report_status="Accepted",
        period_start=end - datetime.timedelta(days=89),
        period_end=end,
        indicator="# of children reached",
        location=place,
        submitted_by=NAME,
        data={"id": pk, "submitted_by": NAME, "report_status": "Accepted", "country_name": "Lebanon"},
    )
    dm.ReportedIndicator.objects.filter(pk=row.pk).update(synced_at=sent, not_sent_since=mark)
    row.refresh_from_db()
    return row


@pytest.fixture
def world(roles):
    """PD1 with two marked rows of QPR1 and one row of QPR2 still sent; PD2 with one marked row; the last sync."""
    partner = PartnerOrganization.objects.create(etl_id="1", name="Partner A", vendor_number="V1")
    pd1 = PCA.objects.create(etl_id="11", partner=partner, number="LEB/PD1", title="A", status="active")
    pd2 = PCA.objects.create(etl_id="12", partner=partner, number="LEB/PD2", title="B", status="active")
    run = _sync(removed=0, no_longer_sent=3)
    _row(1, pd=pd1)
    _row(2, pd=pd1, place="Tripoli")
    _row(3, pd=pd1, number="QPR2", end=datetime.date(2026, 6, 30), mark=None, sent=run.started_at)
    _row(4, pd=pd2, ref="LEB/PD2")
    return {"pd1": pd1, "pd2": pd2, "run": run}


@pytest.fixture
def superuser(roles):
    return _user("root", groups=(), superuser=True)


def _rows_shown(client, **query) -> str:
    """The rows the page lists for review under ``query``, as its form posts them."""
    return client.get(SOURCE, query).context["review"]["rows_key"]


def _post(
    client, *, count: int, newest: datetime.datetime = MARKED, reviewed: bool = True, rows=None, **query
):
    """The button's post, with the rows the page shows now unless ``rows`` says those of an earlier look."""
    shown = _rows_shown(client, **query) if rows is None else rows
    data = {"count": str(count), "newest": newest.isoformat(), "rows": shown, **query}
    if reviewed:
        data["reviewed"] = "yes"
    return client.post(reverse("admin:datamart_raw_unsent"), data)


def _held() -> set[int]:
    return set(dm.ReportedIndicator.objects.values_list("datamart_id", flat=True))


def _said(response) -> str:
    """The message the request added (the latest: those of earlier requests wait for a page that shows them)."""
    return [str(m) for m in response.wsgi_request._messages][-1]


# ------------------------------------------------------------------------------------------------ the counts
def test_runfacts_count_the_rows_no_longer_sent_inside_those_kept_from_an_earlier_sync(world):
    facts = runfacts.facts("partner_reports")
    assert (facts.no_longer_sent, facts.no_longer_sent_reports, facts.same_number_other_period) == (3, 2, 0)
    assert (facts.last_sent_first, facts.last_sent_last) == (LAST_SENT, LAST_SENT)
    assert facts.from_earlier == 3 and facts.unexplained == 0  # inside "kept from an earlier sync"


def test_the_source_page_says_what_is_kept_and_lists_the_reports_for_review(client, world):
    client.force_login(_user("checker", groups=(VIEWER, VALIDATORS)))
    html = client.get(SOURCE).content.decode()
    assert "no longer sent 3 (kept and counted)" in html
    assert (
        "3 rows of 2 progress reports eTools no longer sends are kept and counted, marked no longer sent (last "
        "sent on 20 Sep 2026)." in html
    )
    assert "of which 3 no longer sent" in html
    assert "eTools no longer sends 3 rows of 2 progress reports of this list" in html
    assert "LEB/PD2" in html and "QPR QPR1" in html
    assert "No: no longer sent since 14 Oct 2026 (last sent 20 Sep 2026)" in html  # the list's cell
    assert "Remove these rows" not in html  # a validator reviews, a superuser removes
    assert NAME not in html
    for words in raw.UNSENT.values():
        assert words in html  # the "updated" choices


def test_the_record_page_says_when_etools_stopped_sending_the_row(client, world):
    """The record page's "Updated by:" reads as a sentence: the earlier sync that last sent it, and since when
    eTools no longer sends it (the list's cell answers its column with "No: ...")."""
    import re

    client.force_login(_user("checker", groups=(VIEWER, VALIDATORS)))
    row = dm.ReportedIndicator.objects.get(datamart_id=1)
    html = client.get(reverse("admin:datamart_raw_record", args=["partner_reports", row.pk])).content.decode()
    (line,) = re.findall(r"<li>Updated by: (.*?)</li>", html)
    assert (
        line
        == "an earlier sync (20 Sep 2026, 13:00); eTools no longer sends it since 14 Oct 2026: kept and counted"
    )


def test_the_review_list_shows_at_most_50_reports_then_says_how_many_more(client, roles, superuser):
    for n in range(55):
        _row(100 + n, number=f"QPR{n + 1}", end=datetime.date(2025, 1, 31) + datetime.timedelta(days=n * 31))
    client.force_login(superuser)
    response = client.get(SOURCE)
    review = response.context["review"]
    assert len(review["lines"]) == 50 and review["more"] == 5
    assert set(review["lines"][0]) == {"pd", "report", "period_end", "last_sent", "rows"}  # no person field
    html = response.content.decode()
    assert "and 5 more" in html and "Remove these rows" in html and NAME not in html


def test_the_three_updated_choices_list_the_marked_rows(world):
    _row(5, pd=world["pd1"], number="QPR2", end=datetime.date(2026, 7, 31))  # QPR2 again, another period
    base = raw.base("partner_reports")

    def listed(choice: str) -> set[int]:
        rows = raw.Filters.from_params({"updated": choice}).apply("partner_reports", base, world["run"])
        return set(rows.values_list("datamart_id", flat=True))

    assert listed("unsent") == {1, 2, 4, 5}
    assert listed("unsent_period") == {5}
    assert raw.updated_choices("partner_reports") == {**raw.UPDATED, **raw.UNSENT}
    assert raw.updated_choices("grants") == raw.UPDATED
    assert listed("earlier") == {1, 2, 4, 5}  # last sent before the last sync


def test_the_review_list_follows_the_list_s_filter(client, world, superuser):
    _row(5, pd=world["pd1"], number="QPR2", end=datetime.date(2026, 7, 31))  # QPR2 again, another period
    client.force_login(superuser)
    review = client.get(SOURCE, {"updated": "unsent_period"}).context["review"]
    assert [line["report"] for line in review["lines"]] == ["QPR QPR2"]
    assert (review["rows"], review["query"]) == (1, {"updated": "unsent_period"})
    assert client.get(SOURCE, {"pd": "LEB/PD2"}).context["review"]["rows"] == 1
    assert client.get(SOURCE, {"updated": "last"}).context["review"] is None  # no marked row in the filter


def test_the_csv_and_the_trail_s_rows_carry_when_a_row_was_no_longer_sent(client, world):
    client.force_login(_user("checker", groups=(VIEWER, VALIDATORS)))
    response = client.get(reverse("admin:datamart_raw_csv", args=["partner_reports"]))
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    found = {r["record_id"]: r["no_longer_sent_since"] for r in csv.DictReader(io.StringIO(text))}
    assert found == {"1": "2026-10-14", "2": "2026-10-14", "3": "", "4": "2026-10-14"}
    from neurodb.datamart import trail_rows

    head = trail_rows.ROW_HEAD
    assert head.index("no_longer_sent_since") == head.index("updated_by_last_sync") + 1


def test_the_trail_says_how_many_of_the_rows_kept_from_an_earlier_sync_etools_no_longer_sends(world):
    facts = runfacts.facts("partner_reports")
    steps = trail.source_steps(facts, "partner_reports", "partner report rows", facts.held)
    d4 = next(step for step in steps if step.key == "D4")
    assert d4.lines[0].words.endswith(
        "3 kept from an earlier sync, of which 3 eTools no longer sends (last sent on 20 Sep 2026): kept and "
        "counted."
    )


def test_data_health_and_check_10_count_the_rows_left_after_a_removal(client, world, superuser):
    """A superuser removes PD1's 2 rows; PD2's 1 row stays kept and counted. Data health and check 10 say 1 row,
    as Raw eTools data does, without waiting for the next sync."""
    from neurodb.datamart import checks
    from neurodb.reports.services import datamart_quality
    from tests.web.parts import get_part, text

    def health() -> int:
        return next(d for d in datamart_quality() if d["name"] == "partner_reports")["no_longer_sent"]

    client.force_login(superuser)
    assert health() == 3
    assert "3 rows eTools no longer sends, kept and counted" in text(
        get_part(client, "/data/health/", "datasets")
    )
    _post(client, count=2, pd="LEB/PD1")
    assert _held() == {3, 4} and raw.no_longer_sent()["rows"] == 1 == health()
    part = text(get_part(client, "/data/health/", "datasets"))
    assert "1 row eTools no longer sends, kept and counted" in part and "3 rows eTools" not in part
    said, figures, _, _ = checks._partner_read(world["run"])
    assert dict(figures)["… rows no longer sent (kept and counted)"] == 1
    assert "; 1 row marked no longer sent stays kept and counted." in said, said


def test_data_health_counts_them_and_never_says_the_next_read_removes_them(world):
    row = quality_row("partner_reports", world["run"], 4)
    assert row["no_longer_sent"] == 3 and row["next_read"] == ""
    assert quality_row("grants", None, 0)["next_read"].startswith("; the next complete read removes")


# ------------------------------------------------------------------------------------------------- the button
def test_the_button_is_for_superusers_only(client, world):
    for user in (
        _user("checker", groups=(VIEWER, VALIDATORS)),
        _user("administrator", groups=(ADMIN, VALIDATORS)),  # an Administrator who is not a superuser
    ):
        client.force_login(user)
        assert _post(client, count=3).status_code == 404
    assert _held() == {1, 2, 3, 4}


def test_without_the_box_or_with_another_count_nothing_is_removed(client, world, superuser):
    client.force_login(superuser)
    response = _post(client, count=3, reviewed=False)
    assert response.status_code == 302 and "I have reviewed them" in _said(response)
    response = _post(client, count=2)
    assert _said(response) == backfill.UNSENT_CHANGED
    assert _held() == {1, 2, 3, 4}
    assert not SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL).exists()


def _other_session(*lock_ids):
    """Another database session (another process) holding ``lock_ids``; close it to release them."""
    s = connection.settings_dict
    other = psycopg.connect(
        dbname=s["NAME"],
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=True,
    )
    for lock_id in lock_ids:
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
    return other


@pytest.mark.parametrize("lock_id", [background.DATAMART_LOCK_ID, background.DATAMART_BACKFILL_LOCK_ID])
def test_nothing_is_removed_while_a_sync_or_a_clean_up_runs(client, world, superuser, lock_id):
    client.force_login(superuser)
    other = _other_session(lock_id)
    try:
        response = _post(client, count=3)
    finally:
        other.close()
    assert _said(response) == backfill.UNSENT_BUSY
    assert _held() == {1, 2, 3, 4}
    assert not background.lock_is_held(background.DATAMART_BACKFILL_LOCK_ID)  # released at once


def test_it_removes_only_the_filter_s_marked_rows_up_to_the_newest_mark_shown(client, world, superuser):
    client.force_login(superuser)
    shown = _rows_shown(client, pd="LEB/PD1")  # the page showed rows 1 and 2, before row 6 was marked
    later = _row(6, pd=world["pd1"], number="QPR3", mark=MARKED + datetime.timedelta(hours=20))
    response = _post(client, count=2, rows=shown, pd="LEB/PD1")
    assert response.status_code == 302 and response["Location"].startswith(SOURCE)
    assert _held() == {3, 4, later.datamart_id}
    assert _said(response) == (
        "Removed 2 rows of 1 progress report eTools no longer sends. The removal is recorded in Import and sync "
        "runs as the eTools update clean-up."
    )


def test_rows_swapped_at_the_same_count_are_refused(client, roles, superuser):
    """The reviewer sees QPR1's old row in "eTools sends their number for another period" (1 row). Before the
    post, a sync marks QPR1's copy for another period (QPR1 leaves that list) and brings QPR5's number for
    another period (QPR5's old row enters it): still 1 row, every mark at or before the newest shown. Nothing is
    removed: the rows are not those the page listed."""
    run = _sync(removed=0)
    _row(1, number="QPR1", end=datetime.date(2026, 3, 31))
    _row(2, number="QPR1", end=datetime.date(2026, 4, 30), mark=None, sent=run.started_at)
    _row(10, number="QPR5", end=datetime.date(2026, 3, 31))
    client.force_login(superuser)
    review = client.get(SOURCE, {"updated": "unsent_period"}).context["review"]
    assert review["rows"] == 1 and [line["report"] for line in review["lines"]] == ["QPR QPR1"]
    later = timezone.now()  # the sync
    dm.ReportedIndicator.objects.filter(datamart_id=2).update(not_sent_since=later)
    _row(11, number="QPR5", end=datetime.date(2026, 4, 30), mark=None, sent=later)
    response = _post(client, count=1, rows=review["rows_key"], updated="unsent_period")
    assert _said(response) == backfill.UNSENT_CHANGED
    assert _held() == {1, 2, 10, 11}
    assert not SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL).exists()


def test_rows_of_programme_documents_whose_status_changed_since_are_refused(client, world, superuser):
    """The reviewer sees the 2 marked rows of running programme documents (PD1's). Before the post PD1 ends and
    PD2, with 2 marked rows, runs again: still 2 rows, other rows. Nothing is removed."""
    _row(5, pd=world["pd2"], ref="LEB/PD2", number="QPR2", end=datetime.date(2026, 6, 30))
    PCA.objects.filter(pk=world["pd2"].pk).update(status="ended")
    client.force_login(superuser)
    shown = _rows_shown(client, pd_link="running")
    PCA.objects.filter(pk=world["pd1"].pk).update(status="ended")
    PCA.objects.filter(pk=world["pd2"].pk).update(status="active")
    response = _post(client, count=2, rows=shown, pd_link="running")
    assert _said(response) == backfill.UNSENT_CHANGED
    assert _held() == {1, 2, 3, 4, 5}


def test_a_form_that_posts_no_rows_removes_nothing(client, world, superuser):
    """A page opened before the form posted its rows (or a form altered) removes nothing."""
    client.force_login(superuser)
    for rows in ("", "not-the-rows", "0" * 63):
        assert _said(_post(client, count=3, rows=rows)) == backfill.UNSENT_CHANGED
    assert _held() == {1, 2, 3, 4}


def test_a_failed_removal_is_said_as_failed(client, world, superuser, monkeypatch):
    def fails(self):
        raise RuntimeError("the database refused")

    monkeypatch.setattr(QuerySet, "delete", fails)
    client.force_login(superuser)
    response = _post(client, count=3)
    found = SyncRun.objects.get(job=SyncRun.Job.DATAMART_BACKFILL)
    assert found.status == SyncRun.Status.FAILED
    level = list(response.wsgi_request._messages)[-1].level_tag
    assert (level, _said(response)) == ("error", backfill.UNSENT_FAILED)
    assert _held() == {1, 2, 3, 4}


def test_a_removal_is_recorded_as_the_clean_up_and_moves_the_figures_stamp(client, world, superuser):
    before, etools = compute.data_stamp(), keys.fingerprint()
    client.force_login(superuser)
    _post(client, count=3)
    found = SyncRun.objects.get(job=SyncRun.Job.DATAMART_BACKFILL)
    assert (found.target, found.status, found.rows_written, found.triggered_by) == (
        backfill.UNSENT_TARGET,
        SyncRun.Status.SUCCEEDED,
        3,
        "root",
    )
    assert found.finished_at is not None
    assert found.details == {"removed": 3, "reports": 2, "filter": "every record"}
    assert compute.data_stamp() != before and keys.fingerprint() != etools
    assert _held() == {3}


def test_a_removal_is_never_a_sync(client, world, superuser):
    cleanup = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    jobs = ScheduledJobAdmin(ScheduledJob, None)
    last_sync = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)

    def etools_row() -> tuple:
        row = next(j for j in health_jobs() if j["job"] == SyncRun.Job.ETOOLS_DATAMART)
        return row["last_success"], row["last_run"], row["state"]

    health, shown = etools_row(), str(jobs.last_run(cleanup))
    client.force_login(superuser)
    _post(client, count=3)
    assert SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL, target=backfill.UNSENT_TARGET).exists()
    assert SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART) == last_sync
    assert etools_row() == health
    assert str(jobs.last_run(cleanup)) == shown  # the clean-up's own last run, never the removal


def test_ask_lists_the_clean_up_s_own_last_run_never_a_removal(client, world, superuser):
    from neurodb.help.tools import list_jobs

    cleanup = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    then = timezone.now() - datetime.timedelta(days=3)
    own = SyncRun.objects.create(
        job=SyncRun.Job.DATAMART_BACKFILL, status=SyncRun.Status.SUCCEEDED, started_at=then, finished_at=then
    )
    client.force_login(superuser)
    _post(client, count=3)
    found = next(job for job in list_jobs()["jobs"] if job["job"] == cleanup.key)
    assert found["last_run"]["started"] == own.started_at.isoformat(timespec="minutes")


def test_the_migration_is_quick_and_accounted_for():
    from tests.datamart.test_backfill import QUICK_MIGRATIONS

    assert ("datamart", "0020_partner_report_not_sent", "AddField") in QUICK_MIGRATIONS
