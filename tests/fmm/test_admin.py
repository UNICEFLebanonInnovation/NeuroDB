"""Monitoring insights in the admin: its group, Fields found and the keys pinned there, Questions
found, the quality rules and score settings (saved with a note, previewed, versioned) and the rule
versions with their restore; the chat's questions (read-only). Administrators change them; other staff
read them or are refused."""

import json
import re

import pytest
from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR
from neurodb.core.models import SyncRun
from neurodb.fmm import access, refresh, versions
from neurodb.fmm.models import FieldMapping, RuleSetting, RuleSetVersion, ScoreSetting, Visit
from neurodb.integrations import background
from neurodb.web import admin_site

from .conftest import CANARIES

pytestmark = pytest.mark.django_db

URL = "admin:fmm_fieldmapping_changelist"


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def admin_client(client, admin_user):
    """An Administrator (not a superuser), as the role gives it."""
    client.force_login(admin_user)
    return client


def _page(client) -> str:
    response = client.get(reverse(URL))
    assert response.status_code == 200
    return response.content.decode()


def test_the_group_is_listed_with_fields_found(admin_client):
    html = admin_client.get(reverse("admin:index")).content.decode()
    assert "Monitoring insights" in html and "Fields found" in html
    for name in ("Quality rules", "Score settings", "Rule versions"):
        assert name in html, name
    groups = [(name, keys) for name, _, keys in admin_site.GROUPS if "fmm.FieldMapping" in keys]
    assert [str(name) for name, _ in groups] == ["Monitoring insights"]
    assert {"fmm.RuleSetting", "fmm.ScoreSetting", "fmm.RuleSetVersion"} <= set(groups[0][1])


def test_fmm_icons_are_material_symbols_names_not_site_icons():
    sprite = render_to_string("components/icons.html")
    site_icons = set(re.findall(r'<symbol id="i-([\w-]+)"', sprite))
    icons = {key: icon for key, icon in admin_site.ICONS.items() if key.startswith("fmm.")}
    assert icons == {
        "fmm.FieldMapping": "data_object",
        "fmm.RuleSetting": "rule",
        "fmm.ScoreSetting": "tune",
        "fmm.RuleSetVersion": "history",
        "fmm.PromptVersion": "edit_note",
        "fmm.ModelCapability": "science",
        "fmm.Insight": "auto_awesome",
        "fmm.ChatQuestion": "forum",
        "fmm.Visit": "location_on",
        "fmm.VisitReview": "task_alt",
    }
    for icon in icons.values():
        assert re.fullmatch(r"[a-z0-9_]+", icon) and icon not in site_icons
    from neurodb.core.admin_jobs import BACKGROUND_JOBS

    buttons = {job.name: job.icon for job in BACKGROUND_JOBS if job.name.startswith("run_fmm_")}
    assert buttons == {"run_fmm_refresh": "monitoring", "run_fmm_insights": "auto_awesome"}
    for icon in buttons.values():
        assert re.fullmatch(r"[a-z0-9_]+", icon) and icon not in site_icons


def test_before_the_first_reading_it_says_how_to_start(admin_client):
    html = _page(admin_client)
    assert "The eTools keys have not been read yet" in html and "Refresh now" in html
    assert reverse("admin:fmm_refresh") in html and "manage.py" not in html
    assert "Checklist answers (fm-questions)" in html


def test_fields_found_shows_the_rates_the_choices_and_the_keys(admin_client, fm_world):
    run = refresh.run(triggered_by="test", probe_only=True)
    html = _page(admin_client)
    assert "Keys read on" in html and reverse("admin:core_syncrun_change", args=[run.pk]) in html
    # the rates, in plain words
    assert "of field monitoring finding rows carry an eTools activity id (11 of 12)" in html
    assert "of the rows about a programme document are linked to it" in html
    assert "of the 13 checklist answer records hold an answer" in html
    assert "Unanswered questions seen: 2 of 13 records" in html
    # a field: its key, state, coverage and the candidates found
    assert "<code>answer</code>" in html and "Found" in html
    assert "<code>field_office</code> 25%" in html  # the office key: 3 finding rows of 12 give one
    assert "Not found" in html and "field_monitoring.sections" in html
    assert "The Team column and the visit page" in html
    # the raw keys, with their types and cleaned examples
    assert "<code>narrative_finding</code>" in html and "str 12" in html
    assert "(withheld)" in html
    for canary in CANARIES:
        assert canary not in html, canary
    # the vocabulary seen
    assert "Off Track" in html and "<code>off_track</code>" in html
    assert "Entity types" in html and "<code>cp_output</code>" in html


def test_r2_cannot_be_measured_without_unanswered_questions(admin_client, fm_world, fm_questions_variant):
    from neurodb.datamart import models as dm

    fm_questions_variant("A")
    template = dm.DatamartDocument.objects.filter(dataset="fm_questions").first()
    for n in range(200):
        dm.DatamartDocument.objects.create(
            dataset="fm_questions", record_key=f"x{n}", data={**template.data, "answer": "Yes"}
        )
    dm.DatamartDocument.objects.filter(dataset="fm_questions", data__answer__in=["", "n/a"]).delete()
    refresh.run(triggered_by="test", probe_only=True)
    assert "Unanswered questions seen: 0 of 204 records — R2 cannot be measured" in _page(admin_client)
    # an administrator who turns R2's check off is not told that R2 cannot be measured
    RuleSetting.objects.filter(code="R2").update(params={"require_unanswered_seen": False})
    html = _page(admin_client)
    assert "Unanswered questions seen: 0 of 204 records" in html and "R2 cannot be measured" not in html


def test_a_failed_later_run_is_pointed_at(admin_client, fm_world):
    refresh.run(triggered_by="test", probe_only=True)
    failed = SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="probe", status=SyncRun.Status.FAILED)
    html = _page(admin_client)
    assert "failed: the keys below are those of the reading before it" in html
    assert reverse("admin:core_syncrun_change", args=[failed.pk]) in html


def test_a_later_run_that_read_no_key_is_not_pointed_at(admin_client, fm_world):
    """A refresh of the scores alone reads no key, and one that succeeded is no warning."""
    refresh.run(triggered_by="test", probe_only=True)
    for target, state in (("scores", SyncRun.Status.FAILED), ("scores", SyncRun.Status.SUCCEEDED)):
        SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target=target, status=state)
        html = _page(admin_client)
        assert "Keys read on" in html and "The refresh of" not in html


def test_a_first_reading_that_failed_says_so(admin_client, fm_world, monkeypatch):
    from neurodb.fmm import fields

    monkeypatch.setattr(fields, "resolve_all", lambda probes: 1 / 0)
    failed = refresh.run(triggered_by="test", probe_only=True)
    html = _page(admin_client)
    assert "The eTools keys have not been read yet" in html
    assert "failed before it could read the keys" in html
    assert reverse("admin:core_syncrun_change", args=[failed.pk]) in html


def test_a_key_is_pinned_with_a_note_and_rebuilds_the_visits(
    admin_client, admin_user, fm_world, started, django_capture_on_commit_callbacks
):
    refresh.run(triggered_by="test", probe_only=True)
    mapping = FieldMapping.objects.get(dataset="fm_questions", field="answer")
    url = reverse("admin:fmm_fieldmapping_change", args=[mapping.pk])
    assert url in _page(admin_client)  # the Change link of the Pinned key column
    html = admin_client.get(url).content.decode()
    assert '<option value="summary">summary (13 of 13 records)</option>' in html
    assert 'value="visit_lead"' not in html  # a key that holds a person is never offered for an answer
    # the note is required: nothing is saved without it
    response = admin_client.post(url, {"override_key": "summary", "change_note": ""})
    assert response.status_code == 200 and "This field is required" in response.content.decode()
    mapping.refresh_from_db()
    assert mapping.override_key == "" and RuleSetVersion.objects.count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            url, {"override_key": "summary", "change_note": "Answers are in summary"}
        )
    assert response.status_code == 302
    mapping.refresh_from_db()
    assert (mapping.override_key, mapping.updated_by) == ("summary", admin_user)
    version = RuleSetVersion.objects.get(number=2)
    assert version.note == "Field override: fm_questions.answer → summary: Answers are in summary"
    assert version.snapshot["mappings"] == {"fm_questions.answer": "summary"}
    assert started == [("fmm_refresh", "--triggered-by", f"admin:{admin_user.pk}")]  # a full refresh
    messages = [str(m) for m in admin_client.get(reverse(URL)).context["messages"]]
    assert messages == [
        "Saved as rules v2. The visits will be rebuilt with this key in the background; the page shows "
        "'recomputing with rules v2' until they are."
    ]
    # pinning cannot add or delete a field
    assert admin_client.get(reverse("admin:fmm_fieldmapping_add")).status_code == 403
    assert admin_client.post(reverse("admin:fmm_fieldmapping_delete", args=[mapping.pk])).status_code == 403


def test_other_staff_and_signed_out_people_cannot_open_it(client, roles):
    editor = User.objects.create_user(username="editor", password="editor-pass-123456", is_staff=True)
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    client.force_login(editor)
    version = RuleSetVersion.objects.get()
    for name, args in (
        (URL, []),
        ("admin:fmm_rulesetting_changelist", []),
        ("admin:fmm_scoresetting_change", [1]),
        ("admin:fmm_rulesetversion_changelist", []),
    ):
        assert client.get(reverse(name, args=args)).status_code == 403, name
    # the pages of their own raise 404 for anyone but an Administrator
    for name, args in (
        ("admin:fmm_questions", []),
        ("admin:fmm_rulesetversion_restore", [version.pk]),
        ("admin:fmm_rulesetting_preview", ["R2"]),
    ):
        assert client.get(reverse(name, args=args)).status_code == 404, name
        assert client.post(reverse(name, args=args), {"role": "q1"}).status_code == 404, name
    client.logout()
    assert client.get(reverse(URL)).status_code == 302


def test_is_admin_is_the_administrator_role(roles, admin_user, viewer):
    root = User.objects.create_superuser(
        username="root", email="root@example.org", password="root-pass-123456"
    )
    assert access.is_admin(admin_user) and access.is_admin(root)
    assert not access.is_admin(viewer)
    assert admin_user.groups.filter(name=ADMIN).exists()
    from django.contrib.auth.models import AnonymousUser

    assert not access.is_admin(AnonymousUser()) and not access.is_admin(None)


def test_the_visits_admin_is_read_only_and_shows_the_links(admin_client, fm_world):
    refresh.run(triggered_by="test")
    html = admin_client.get(reverse("admin:fmm_visit_changelist") + "?q=amel").content.decode()
    assert "Visit 1722" in html and "Visit 1723" not in html  # by partner name
    visit = Visit.objects.get(key="1722")
    page = admin_client.get(reverse("admin:fmm_visit_change", args=[visit.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Zahle town" in html and "monitored entities" in html.lower() and "FM-2026-022" in html
    assert 'name="_save"' not in html and "View on site" not in html
    for canary in CANARIES[2:]:  # never an e-mail address, phone, link or narrative
        assert canary not in html
    assert admin_client.get(reverse("admin:fmm_visit_add")).status_code == 403
    assert admin_client.get(reverse("admin:fmm_visitreview_changelist")).status_code == 200


def test_fields_found_shows_how_the_records_matched_their_visits(admin_client, fm_world):
    from neurodb.datamart import models as dm

    assert "matched a visit" not in _page(admin_client)  # no full refresh yet
    refresh.run(triggered_by="test")
    html = _page(admin_client)
    assert "100% of the checklist answer records matched a visit (13 of 13)." in html
    assert (
        "FM action points: 50% matched to their visit by the activity id, 25% by the activity reference, "
        "0% by the reference number, 25% not matched (4 in all)." in html
    )
    assert "Under half match by the activity id" not in html
    # few action points match by the activity id: eTools' related module id may be something else
    dm.ActionPoint.objects.filter(datamart_id__in=(8001, 8004)).update(related_module_id=99999)
    refresh.run(triggered_by="test")
    html = _page(admin_client)
    assert "FM action points: 0% matched to their visit by the activity id" in html
    assert "Under half match by the activity id" in html
    # a key probe alone does not hide the last build's rates
    refresh.run(triggered_by="test", probe_only=True)
    assert "FM action points: 0% matched" in _page(admin_client)


# ------------------------------------------------------------------------------------------ rules
def _rule_form(**changes) -> dict:
    rule = RuleSetting.objects.get(code="R4")
    data = {
        "enabled": "on",
        "points": rule.points,
        "threshold": rule.threshold,
        "params": json.dumps(rule.params),
        "description": rule.description,
        "change_note": "",
    }
    return {**data, **changes}


def test_a_rule_is_saved_with_a_note_as_a_new_version(
    admin_client, admin_user, started, django_capture_on_commit_callbacks
):
    url = reverse("admin:fmm_rulesetting_change", args=["R4"])
    html = admin_client.get(url).content.decode()
    assert "Change note" in html and "Preview effect" in html
    start = html.index('id="rulesetting_form"')
    form = html[start : html.index("</form>", start)]  # the button posts the form's own values
    preview = reverse("admin:fmm_rulesetting_preview", args=["R4"])
    assert f'hx-post="{preview}" hx-include="closest form"' in form
    assert "Not computed yet" in html  # the last refresh's counts, once there is one
    # no note, or a parameter that is not valid: nothing is saved
    assert "This field is required" in admin_client.post(url, _rule_form(threshold=10)).content.decode()
    bad = admin_client.post(url, _rule_form(params='{"copy_window_days": 5000}', change_note="x"))
    assert "must be a whole number from 1 to 1095" in bad.content.decode()
    assert RuleSetVersion.objects.count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(url, _rule_form(threshold=10, change_note="Short visits write less"))
    assert response.status_code == 302
    rule = RuleSetting.objects.get(code="R4")
    assert (rule.threshold, rule.updated_by) == (10, admin_user)
    version = RuleSetVersion.objects.get(number=2)
    assert (version.note, version.created_by) == ("Short visits write less", admin_user)
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    messages = [
        str(m) for m in admin_client.get(reverse("admin:fmm_rulesetting_changelist")).context["messages"]
    ]
    assert messages == [
        "Saved as rules v2. Scores will be recomputed in the background; the page shows "
        "'recomputing with rules v2' until they are."
    ]


def test_the_rule_list_and_the_last_refresh_counts(admin_client, fm_world):
    refresh.run(triggered_by="test")
    html = admin_client.get(reverse("admin:fmm_rulesetting_changelist")).content.decode()
    for label in ("Completeness", "Evidence sufficiency", "HACT alignment", "Rating quality"):
        assert label in html, label
    assert 'name="form-0-' not in html  # no editing in the list: every change needs a note
    html = admin_client.get(reverse("admin:fmm_rulesetting_change", args=["R6"])).content.decode()
    assert "evaluated on 2 visits, 1 of them flagged; not available on 4; does not apply to 2" in html
    # the time of the last refresh in the local time zone, as everywhere else
    run = SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).latest("finished_at")
    local = timezone.localtime(run.finished_at)
    assert f"Last refresh ({date_format(local, 'j M Y, H:i')}, rules v1)" in html


def test_the_preview_shows_the_effect_before_saving(admin_client, fm_world):
    refresh.run(triggered_by="test")
    url = reverse("admin:fmm_rulesetting_preview", args=["R4"])
    response = admin_client.post(url, _rule_form(threshold=5))
    html = response.content.decode()
    assert response.status_code == 200 and "R4 would flag 0 visits (now 2)" in html
    assert "Nothing was saved" in html
    assert RuleSetting.objects.get(code="R4").threshold == 25 and RuleSetVersion.objects.count() == 1
    errors = admin_client.post(url, _rule_form(threshold=900)).content.decode()
    assert "Correct these first" in errors and "from 1 to 500" in errors
    assert admin_client.get(url).status_code == 404  # posted by the button only


def test_score_settings_open_their_one_row_and_save_as_a_version(admin_client, admin_user, started):
    response = admin_client.get(reverse("admin:fmm_scoresetting_changelist"))
    assert response.status_code == 302 and response["Location"] == reverse(
        "admin:fmm_scoresetting_change", args=[1]
    )
    html = admin_client.get(response["Location"]).content.decode()
    assert "Urgency weights" in html and "Question patterns" in html and "Preview effect" in html
    setting = ScoreSetting.load()
    data = {
        name: json.dumps(getattr(setting, name))
        if isinstance(getattr(setting, name), dict)
        else getattr(setting, name)
        for name in (
            "min_evaluated_points",
            "band_high",
            "band_medium",
            "high_flag_count",
            "urgency_red",
            "urgency_amber",
            "urgency_weights",
            "recency_days",
            "scored_statuses",
            "follow_up_days",
            "report_late_days",
            "question_patterns",
            "role_flag_answers",
        )
    }
    response = admin_client.post(response["Location"], {**data, "urgency_amber": 75, "change_note": "x"})
    assert "Amber must be below red" in response.content.decode()
    response = admin_client.post(
        reverse("admin:fmm_scoresetting_change", args=[1]),
        {**data, "urgency_red": 75, "change_note": "Fewer reds"},
    )
    assert response.status_code == 302 and ScoreSetting.load().urgency_red == 75
    assert RuleSetVersion.objects.get(number=2).snapshot["score"]["urgency_red"] == 75
    assert admin_client.get(reverse("admin:fmm_scoresetting_add")).status_code == 403


def test_score_settings_take_fms_urgency_weights_and_scored_statuses(admin_client):
    """Release 2 (A3, A4): the weights are three numbers that add up to 1, the scored statuses a choice
    of eTools statuses; both are versioned with the other settings."""
    setting = ScoreSetting.load()
    assert setting.urgency_weights == {"quality_gap": 0.5, "recency": 0.3, "red_flags": 0.2}
    assert setting.scored_statuses == ["report_finalization", "completed"] and setting.recency_days == 180
    url = reverse("admin:fmm_scoresetting_change", args=[setting.pk])
    data = {
        "min_evaluated_points": 30,
        "band_high": 80,
        "band_medium": 50,
        "high_flag_count": 3,
        "urgency_red": 70,
        "urgency_amber": 40,
        "urgency_weights": json.dumps({"quality_gap": 0.5, "recency": 0.3, "red_flags": 0.3}),
        "recency_days": 90,
        "scored_statuses": ["submitted", "completed"],
        "follow_up_days": 14,
        "report_late_days": 30,
        "question_patterns": json.dumps(setting.question_patterns),
        "role_flag_answers": json.dumps(setting.role_flag_answers),
        "change_note": "Submitted reports scored too",
    }
    html = admin_client.post(url, data).content.decode()
    assert "add up to 1" in html
    html = admin_client.post(url, {**data, "scored_statuses": ["cancelled"]}).content.decode()
    assert "cancelled" in html and ScoreSetting.load().recency_days == 180
    weights = {"quality_gap": 0.6, "recency": 0.2, "red_flags": 0.2}
    response = admin_client.post(url, {**data, "urgency_weights": json.dumps(weights)})
    assert response.status_code == 302
    setting = ScoreSetting.load()
    assert setting.urgency_weights == weights and setting.recency_days == 90
    assert setting.scored_statuses == ["submitted", "completed"]
    snapshot = RuleSetVersion.objects.order_by("-number").first().snapshot["score"]
    assert snapshot["scored_statuses"] == ["submitted", "completed"] and snapshot["recency_days"] == 90


def test_staff_who_may_only_view_read_the_rules(client, roles):
    from django.contrib.auth.models import Permission

    reader = User.objects.create_user(username="reader", password="reader-pass-123456", is_staff=True)
    reader.user_permissions.add(Permission.objects.get(codename="view_rulesetting"))
    client.force_login(reader)
    url = reverse("admin:fmm_rulesetting_change", args=["R2"])
    html = client.get(url).content.decode()
    assert "Evidence sufficiency" in html and 'name="_save"' not in html and "Preview effect" not in html
    assert client.post(url, {"points": 1, "change_note": "x"}).status_code == 403
    assert RuleSetting.objects.get(code="R2").points == 20


# ------------------------------------------------------------------------------------------ questions found
def test_questions_found_gives_a_question_its_role(
    admin_client, admin_user, fm_world, started, django_capture_on_commit_callbacks
):
    refresh.run(triggered_by="test")
    url = reverse("admin:fmm_questions")
    assert url == "/manage/fmm/questions/" or url.endswith("/fmm/questions/")
    html = admin_client.get(url).content.decode()
    assert "Are attendance registers kept up to date?" in html and "Use as PSEA" in html
    assert url in _page(admin_client)  # linked from Fields found
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            url, {"role": "psea", "question_text": "Are attendance registers kept up to date?"}
        )
    assert response.status_code == 302
    assert "=are attendance registers kept up to date" in ScoreSetting.load().question_patterns["psea"]
    assert RuleSetVersion.objects.get(number=2).note == "PSEA pattern set from Questions found"
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    html = admin_client.get(url).content.decode()
    assert "(set here)" in html and "Saved as rules v2" in html


# ------------------------------------------------------------------------------------------ versions
def test_a_version_shows_its_settings_beside_today_and_can_be_restored(
    admin_client, admin_user, started, django_capture_on_commit_callbacks
):
    v1 = RuleSetVersion.objects.get(number=1)
    RuleSetting.objects.filter(code="R2").update(threshold=60)
    versions.record_rules(admin_user, "Lower target")
    detail = admin_client.get(reverse("admin:fmm_rulesetversion_change", args=[v1.pk])).content.decode()
    assert "In this version" in detail and "(differs)" in detail and "Restore this version" in detail
    assert 'name="_save"' not in detail  # versions are never edited
    confirm_url = reverse("admin:fmm_rulesetversion_restore", args=[v1.pk])
    html = admin_client.get(confirm_url).content.decode()
    assert "What restoring changes" in html and "threshold" in html and "60" in html
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(confirm_url, {"note": "Back to 80%"})
    v3 = RuleSetVersion.objects.get(number=3)
    assert response.status_code == 302 and response["Location"] == reverse(
        "admin:fmm_rulesetversion_change", args=[v3.pk]
    )
    assert (v3.note, v3.restored_from, RuleSetting.objects.get(code="R2").threshold) == (
        "Restored v1: Back to 80%",
        v1,
        80,
    )
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", f"admin:{admin_user.pk}")]
    assert admin_client.post(reverse("admin:fmm_rulesetversion_delete", args=[v1.pk])).status_code == 403


def test_the_visit_admin_shows_the_rule_results(admin_client, fm_world):
    refresh.run(triggered_by="test")
    visit = Visit.objects.get(key="1726")
    html = admin_client.get(reverse("admin:fmm_visit_change", args=[visit.pk])).content.decode()
    assert "quality rules (pass, fail" in html.lower() and "contradiction" in html and "too_short" in html
    assert "Narrative contradicts the rating (rated On track; it mentions: delayed, suspended)" in html


# ------------------------------------------------------------------------------------------ chat questions
def test_chat_questions_are_read_only_with_the_text_on_their_own_page(admin_client, admin_user):
    import uuid

    from neurodb.fmm.ai import profiles
    from neurodb.fmm.models import ChatQuestion

    row = ChatQuestion.objects.create(
        user=admin_user,
        conversation=uuid.uuid4(),
        scope_hash="s",
        scope={"preset": "year", "year": 2026, "sections": ["Education"], "governorate": ""},
        version=profiles.published(),
        question="Which visits were off track?",
        answer="[Visit 1722](/fmm/visits/1722/) (not checked)",
        status="answered",
        checks={"kept": [], "removed": ["1722"], "unchecked_numbers": ["444"], "privacy_blocked": 1},
        model="gpt-5.5",
        input_tokens=1000,
        output_tokens=200,
    )
    listing = admin_client.get(reverse("admin:fmm_chatquestion_changelist")).content.decode()
    assert "2026 · Education" in listing and "0 / 1 · 1 · 1 look-ups not shared" in listing
    assert "Which visits were off track?" not in listing  # the question on its own page only
    detail = admin_client.get(reverse("admin:fmm_chatquestion_change", args=[row.pk])).content.decode()
    assert "Which visits were off track?" in detail
    assert admin_client.get(reverse("admin:fmm_chatquestion_add")).status_code == 403
    assert "Chat questions" in admin_client.get(reverse("admin:index")).content.decode()
