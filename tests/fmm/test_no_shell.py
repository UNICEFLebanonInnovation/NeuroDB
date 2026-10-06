"""Administrators have no shell: every step of Monitoring insights' go-live is a button in the admin
(the refresh, the redacted samples download, matching the eTools section names again), and no admin
screen tells anyone to run a command."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from django.urls import reverse

from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.integrations import background
from neurodb.watch.models import SectionMatch

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2] / "neurodb"


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    monkeypatch.setattr(background, "is_running", lambda job: False)
    return calls


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


def test_the_refresh_is_a_run_a_job_button():
    assert ("fmm_refresh",) in [spec.command for spec in BACKGROUND_JOBS]
    assert ("fmm_insights",) in [spec.command for spec in BACKGROUND_JOBS]


def test_refresh_now_on_fields_found_starts_the_refresh(admin_client, admin_user, started):
    page = admin_client.get(reverse("admin:fmm_fieldmapping_changelist")).content.decode()
    assert reverse("admin:fmm_refresh") in page and reverse("admin:fmm_samples") in page
    response = admin_client.post(reverse("admin:fmm_refresh"))
    assert response.status_code == 302
    assert started == [("fmm_refresh", "--triggered-by", admin_user.username)]


def test_refresh_now_refuses_a_second_start(admin_client, monkeypatch, started):
    monkeypatch.setattr(background, "is_running", lambda job: job == SyncRun.Job.FMM_REFRESH)
    admin_client.post(reverse("admin:fmm_refresh"))
    assert started == []


def test_refresh_now_and_samples_are_for_administrators_only(client, viewer, started):
    client.force_login(viewer)
    assert client.post(reverse("admin:fmm_refresh")).status_code in (302, 404)
    assert client.get(reverse("admin:fmm_samples")).status_code in (302, 404)
    assert started == []


def test_refresh_now_needs_a_post(admin_client, started):
    assert admin_client.get(reverse("admin:fmm_refresh")).status_code == 404
    assert started == []


def test_the_samples_download_holds_the_real_keys_without_people(admin_client):
    dm.MonitoringFinding.objects.create(
        datamart_id=1,
        monitoring_activity="MA-1",
        data={
            "id": 1,
            "monitoring_activity": "MA-1",
            "visit_lead": "Rania Haddad",
            "visit_lead_email": "rania@example.org",
            "narrative_finding": "Met the head teacher; write to rania@example.org for the list.",
        },
    )
    dm.DatamartDocument.objects.create(
        dataset="fm_questions",
        record_key="q1",
        data={"monitoring_activity": "MA-1", "question": "Q1", "team_members": ["Omar Khalil"]},
    )
    response = admin_client.get(reverse("admin:fmm_samples"))
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert {"field_monitoring.json", "fm_questions.json", "README.txt"} <= set(archive.namelist())
    text = " ".join(archive.read(name).decode() for name in archive.namelist())
    for secret in ("Rania", "Haddad", "rania@example.org", "Omar", "Khalil"):
        assert secret not in text
    finding = json.loads(archive.read("field_monitoring.json"))["results"][0]
    assert finding["monitoring_activity"] == "MA-1" and "narrative_finding" in finding  # the keys stay
    assert finding["visit_lead"].startswith("Person ")
    questions = json.loads(archive.read("fm_questions.json"))
    assert questions["dataset"] == "fm_questions" and questions["results"][0]["question"] == "Q1"


def test_match_again_is_a_button_on_etools_section_names(admin_client):
    SectionMatch.objects.create(etools_name="Education", how=SectionMatch.How.NONE, confirmed=False)
    page = admin_client.get(reverse("admin:watch_sectionmatch_changelist")).content.decode()
    url = reverse("admin:watch_sectionmatch_match_again")
    assert url in page
    response = admin_client.get(url, follow=True)
    assert response.status_code == 200 and "matched again" in response.content.decode()


def test_no_admin_screen_tells_anyone_to_run_a_command():
    told = [
        str(path.relative_to(ROOT.parent))
        for path in sorted(ROOT.rglob("templates/**/*.html"))
        if "manage.py" in path.read_text(encoding="utf-8")
    ]
    assert told == [], "an administrator has no shell: give the step a button instead"
