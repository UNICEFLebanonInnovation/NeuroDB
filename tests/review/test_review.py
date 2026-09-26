"""The daily review: checks, diff against the day before, narration, storage, command, admin, page."""

import datetime
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.integrations import background
from neurodb.review import checks, services
from neurodb.review.models import DailyReview, ReviewFinding
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db
NEXT_DAY = TODAY + datetime.timedelta(days=1)


@pytest.fixture
def data(db, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


def run(day=TODAY, **kwargs):
    return services.run(date=day, today=day, **kwargs)


def by_check(review):
    return {f.check_id: f for f in review.findings.all()}


def test_run_stores_the_findings_of_the_fixed_checks(data):
    review = run()
    assert review.status == DailyReview.Status.SUCCEEDED and review.checks_run == len(checks.CHECKS)
    found = by_check(review)
    assert {"not_reported", "tpm_reports_late", "action_points_overdue", "findings_off_track"} <= set(found)
    assert "new_off_track" not in found  # the children indicator is on track
    assert found["action_points_overdue"].severity == ReviewFinding.Severity.CRITICAL
    assert "TPM/2" in found["tpm_reports_late"].title
    assert all(f.state == ReviewFinding.State.NEW for f in review.findings.all())
    ranks = [f.rank for f in review.findings.all()]
    assert ranks == sorted(ranks) and review.findings.first().severity == ReviewFinding.Severity.CRITICAL
    assert review.narrated_by == DailyReview.TEMPLATE and "items to look at" in review.summary
    assert review.stats["status_counts"]["on_track"] == 1 and review.stats["check_errors"] == {}
    sync = SyncRun.objects.get(job=SyncRun.Job.DAILY_REVIEW)
    assert (sync.status, sync.rows_written) == (SyncRun.Status.SUCCEEDED, review.findings.count())


def test_the_next_review_tells_still_open_from_resolved(data):
    run()
    second = run(NEXT_DAY)
    assert by_check(second)["action_points_overdue"].state == ReviewFinding.State.STILL_OPEN
    dm.ActionPoint.objects.filter(reference_number="AP/1").update(status="completed")
    third = run(NEXT_DAY + datetime.timedelta(days=1))
    resolved = [f for f in third.findings.all() if f.state == ReviewFinding.State.RESOLVED]
    assert any(f.check_id == "action_points_overdue" for f in resolved)
    assert all(
        f.severity == ReviewFinding.Severity.GOOD and f.title.startswith("Resolved:") for f in resolved
    )


def test_rerunning_a_date_replaces_its_review(data):
    run()
    run()
    assert DailyReview.objects.filter(date=TODAY).count() == 1


def test_a_failing_check_is_recorded_and_the_others_still_run(data, monkeypatch):
    def broken(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(checks, "CHECKS", [("broken", broken), *checks.CHECKS])
    review = run()
    assert review.status == DailyReview.Status.SUCCEEDED
    assert "boom" in review.stats["check_errors"]["broken"] and review.findings.exists()
    assert SyncRun.objects.get(job=SyncRun.Job.DAILY_REVIEW).status == SyncRun.Status.PARTIAL


def test_the_assistant_writes_the_summary_when_configured(data, settings, monkeypatch):
    from neurodb.assistant import agent

    settings.AI_ASSISTANT_ENABLED, settings.AI_ASSISTANT_MODEL = True, "test-model"
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            output_text="Two things need a person today.",
            usage=SimpleNamespace(input_tokens=90, output_tokens=12),
        )

    monkeypatch.setattr(agent, "client", lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    review = run()
    assert (review.summary, review.narrated_by) == ("Two things need a person today.", "test-model")
    assert (review.model_input_tokens, review.model_output_tokens) == (90, 12)
    assert seen["store"] is False and "Amel Association" in seen["input"]


def test_a_failing_narration_falls_back_to_the_template(data, settings, monkeypatch):
    from neurodb.assistant import agent

    settings.AI_ASSISTANT_ENABLED = True

    def unreachable():
        raise ConnectionError("no network")

    monkeypatch.setattr(agent, "client", unreachable)
    review = run()
    assert review.status == DailyReview.Status.SUCCEEDED and review.narrated_by == DailyReview.TEMPLATE
    assert review.summary


def test_for_page_modes_and_section_filter(data):
    assert services.for_page("today", []) is None
    run()
    run(NEXT_DAY)
    today = services.for_page("today", [])
    assert today["mode"] == "today" and today["review"].date == NEXT_DAY and today["findings"]
    assert services.for_page("yesterday", [])["review"].date == TODAY
    assert services.for_page(TODAY.isoformat(), [])["review"].date == TODAY
    assert services.for_page("1999-01-01", [])["missing"]
    assert services.for_page("week", [])["mode"] == "week"
    education = services.for_page("today", ["Education"])
    assert all(f.section in ("", "Education") for f in education["findings"])
    protection = services.for_page("today", ["Child Protection"])
    assert len(protection["findings"]) > len(education["findings"])


def test_command(data, capsys):
    call_command("daily_review", "--date", TODAY.isoformat(), "--no-narration")
    out = capsys.readouterr().out
    assert f"Daily review {TODAY}" in out and "[critical/new]" in out
    with pytest.raises(CommandError):
        call_command("daily_review", "--date", "yesterday")


def test_admin_starts_the_review_in_the_background(client, admin_user, monkeypatch):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    url = reverse("admin:review_dailyreview_run_review_now")
    assert client.get(url).status_code == 200 and started == []  # a GET only shows the confirmation
    response = client.post(url, {"_form_submitted": "on"})
    assert response.status_code == 302
    assert started == [("daily_review", "--triggered-by", admin_user.username)]
    assert client.get(reverse("admin:review_dailyreview_changelist")).status_code == 200


def test_admin_does_not_start_a_second_run(client, admin_user, monkeypatch):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: True)
    SyncRun.objects.create(job=SyncRun.Job.DAILY_REVIEW, status=SyncRun.Status.RUNNING)
    response = client.post(
        reverse("admin:review_dailyreview_run_review_now"), {"_form_submitted": "on"}, follow=True
    )
    assert started == [] and "already running" in response.content.decode()


def test_viewers_cannot_start_it(client, viewer, monkeypatch):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    client.post(reverse("admin:review_dailyreview_run_review_now"), {"_form_submitted": "on"})
    assert started == []


def test_a_check_that_fails_keeps_its_findings_open(data, monkeypatch):
    run()
    original = dict(checks.CHECKS)

    def broken(ctx):
        raise RuntimeError("timeout")

    monkeypatch.setattr(
        checks,
        "CHECKS",
        [(cid, broken if cid == "action_points_overdue" else fn) for cid, fn in original.items()],
    )
    second = run(NEXT_DAY)
    finding = by_check(second)["action_points_overdue"]
    assert (
        finding.state == ReviewFinding.State.STILL_OPEN
        and finding.severity == ReviewFinding.Severity.CRITICAL
    )
    assert not second.findings.filter(
        state=ReviewFinding.State.RESOLVED, check_id="action_points_overdue"
    ).exists()


def test_a_failed_rerun_keeps_the_days_good_review(data, monkeypatch):
    good = run()
    monkeypatch.setattr(checks, "snapshot", lambda ctx: 1 / 0)
    failed = run()
    assert failed.status == DailyReview.Status.FAILED
    kept = DailyReview.objects.get(date=TODAY)
    assert (kept.pk, kept.status) == (good.pk, DailyReview.Status.SUCCEEDED) and kept.findings.exists()
    assert (
        SyncRun.objects.filter(job=SyncRun.Job.DAILY_REVIEW).latest("started_at").status
        == SyncRun.Status.FAILED
    )


def test_change_findings_are_always_new(data):
    previous = run()
    draft = checks.Draft(
        key="new_off_track:X", check="new_off_track", severity="warning", section="", title="t", detail=""
    )
    ReviewFinding.objects.create(
        review=previous, key="new_off_track:X", check_id="new_off_track", severity="warning", title="t"
    )
    ((_, state),) = services.diff([draft], previous)[:1]
    assert state == ReviewFinding.State.NEW


def test_long_keys_and_titles_fit_their_columns(data):
    long = "x" * 400
    fields = services._fields(
        checks.Draft(
            key=long, check="action_points_overdue", severity="warning", section=long, title=long, detail=""
        )
    )
    assert len(fields["key"]) <= 300 and len(fields["title"]) <= 300 and len(fields["section"]) <= 128
    other = services._fields(
        checks.Draft(key=long + "y", check="c", severity="warning", section="", title="", detail="")
    )
    assert other["key"] != fields["key"]  # the hash keeps two long keys apart


def test_only_one_review_runs_at_a_time(data, monkeypatch):
    monkeypatch.setattr(services, "_lock", lambda: False)
    with pytest.raises(services.ReviewBusy):
        run()
    call_command("daily_review", "--no-narration")  # the command says so and exits cleanly


def test_the_summary_input_names_no_staff(data, settings, monkeypatch):
    from neurodb.assistant import agent

    dm.ActionPoint.objects.update(assigned_to_name="Jane Doe")
    settings.AI_ASSISTANT_ENABLED = True
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(output_text="Summary.", usage=None)

    monkeypatch.setattr(agent, "client", lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    run()
    assert "Jane Doe" not in seen["input"]


def test_the_overview_shows_the_review_card(data, hierarchy, client_viewer):
    run()
    page = client_viewer.get(reverse("reports:overview") + "?review=today")
    assert page.status_code == 200
    text = page.content.decode()
    assert 'id="daily-review"' in text and "How the daily review works" in text and "TPM/2" in text
