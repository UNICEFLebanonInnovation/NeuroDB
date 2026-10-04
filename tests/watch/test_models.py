"""NeuroDB Watch's memory: what is unique, what an item needs, and the admin lists."""

import datetime

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from neurodb.accounts.models import Section
from neurodb.accounts.roles import MANAGEMENT
from neurodb.watch.models import (
    KEY_MAX,
    STORY_MAX,
    DetectorSetting,
    SectionMatch,
    WatchDelivery,
    WatchItem,
    WatchNote,
    WatchReceipt,
    WatchState,
    fit_key,
    hash_evidence,
)

pytestmark = pytest.mark.django_db

DAY = datetime.date(2026, 10, 5)
EVIDENCE = {
    "source": "eTools progress reports",
    "source_job": "etools_datamart",
    "synced_at": "2026-10-04T20:41:00+03:00",
    "records": [{"label": "QPR 3", "date": "2026-10-10", "value": "due", "url": "/programme/1/"}],
    "numbers": {"days_left": 5},
}
CHANGELISTS = ["watchitem", "watchreceipt", "watchnote", "detectorsetting", "sectionmatch"]


def _values(key="due:report:LEB/PCA2026001/PD2026001:7", **fields):
    return {
        "key": key,
        "detector": "report_due_soon",
        "kind": WatchItem.Kind.DEADLINE,
        "severity": WatchItem.Severity.WARNING,
        "title": "Progress report due 10 Oct: LEB/PCA2026001/PD2026001 (Partner A), Q3",
        "due_date": DAY + datetime.timedelta(days=5),
        "evidence": EVIDENCE,
        "first_seen_on": DAY,
        "last_seen_on": DAY,
        "changed_on": DAY,
        **fields,
    }


def _item(key="due:report:LEB/PCA2026001/PD2026001:7", **fields):
    return WatchItem.objects.create(**_values(key, **fields))


def _refused(make):
    with pytest.raises(IntegrityError), transaction.atomic():
        make()


# ---------------------------------------------------------------------------- what is unique
def test_the_item_key_is_unique():
    _item()
    _refused(lambda: _item())


def test_one_receipt_per_person_and_item(viewer):
    item = _item()
    WatchReceipt.objects.create(user=viewer, item=item, first_told_on=DAY, last_told_on=DAY, told_step="new")
    _refused(
        lambda: WatchReceipt.objects.create(
            user=viewer, item=item, first_told_on=DAY, last_told_on=DAY, told_step="7"
        )
    )


def test_one_note_per_day_and_audience():
    WatchNote.objects.create(date=DAY, audience_key=WatchNote.COUNTRY, written_by=WatchNote.TEMPLATE)
    WatchNote.objects.create(date=DAY, audience_key=WatchNote.section_audience(3), written_by="template")
    WatchNote.objects.create(date=DAY + datetime.timedelta(days=1), audience_key="country", written_by="x")
    _refused(lambda: WatchNote.objects.create(date=DAY, audience_key="country", written_by="template"))


def test_one_morning_email_per_person_and_day(viewer):
    WatchDelivery.objects.create(user=viewer, date=DAY)
    _refused(lambda: WatchDelivery.objects.create(user=viewer, date=DAY))


def test_one_row_per_etools_section_name():
    SectionMatch.objects.create(etools_name="Education")
    _refused(lambda: SectionMatch.objects.create(etools_name="Education"))


def test_one_setting_per_check():
    first = DetectorSetting.ensure("report_due_soon")
    assert first.mode == DetectorSetting.Mode.TRIAL and first.on_since is None  # new checks start in trial
    assert DetectorSetting.ensure("report_due_soon", DetectorSetting.Mode.ON).pk == first.pk  # unchanged
    assert DetectorSetting.objects.get(pk=first.pk).mode == DetectorSetting.Mode.TRIAL
    review = DetectorSetting.ensure("review", DetectorSetting.Mode.ON)  # the review import starts on
    assert review.mode == DetectorSetting.Mode.ON and review.on_since is not None
    _refused(lambda: DetectorSetting.objects.create(detector="review"))


# ---------------------------------------------------------------------------- what an item needs
def test_an_item_without_evidence_is_refused():
    for evidence in ({}, {"source": "eTools", "records": []}, {"source": "eTools"}, []):
        item = WatchItem(**_values("other", evidence=evidence))
        with pytest.raises(ValidationError, match="evidence"):
            item.full_clean()
    _item(key="fine").full_clean()  # one record is enough


def test_a_long_key_is_cut_and_hashed_the_same_way_each_time():
    long_key = "due:action_points:" + "x" * 400
    short = fit_key(long_key)
    assert len(short) <= KEY_MAX and short == fit_key(long_key) and short.startswith("due:action_points:")
    assert fit_key(long_key + "y") != short  # two long keys stay apart
    assert fit_key("review:abc") == "review:abc"
    item = _item(key=long_key)
    assert item.key == short and WatchItem.objects.get(key=short).pk == item.pk


def test_the_story_keeps_the_latest_lines():
    item = _item()
    for n in range(STORY_MAX + 5):
        item.add_story(f"line {n}", on=DAY)
    item.save()
    item.refresh_from_db()
    assert len(item.story) == STORY_MAX and item.story[-1] == {
        "on": "2026-10-05",
        "text": f"line {STORY_MAX + 4}",
    }


def test_the_evidence_fingerprint_ignores_the_order_of_its_fields():
    assert hash_evidence(EVIDENCE) == hash_evidence(dict(reversed(list(EVIDENCE.items()))))
    assert hash_evidence(EVIDENCE) != hash_evidence({**EVIDENCE, "numbers": {"days_left": 4}})


def test_the_look_up_is_kept_on_the_item():
    item = _item(looked_up={"text": "Two reports are late.", "numbers": [2], "tools": ["programme_details"]})
    item.refresh_from_db()
    assert item.looked_up["tools"] == ["programme_details"] and _item(key="b").looked_up == {}


def test_there_is_one_state_row():
    first = WatchState.get()
    assert first.pk == 1 and WatchState.get().pk == 1
    WatchState(last_change_id=42).save()  # a second row is never made
    assert WatchState.objects.count() == 1 and WatchState.get().last_change_id == 42


# ---------------------------------------------------------------------------- the admin
@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def filled(viewer):
    item = _item()
    item.add_story("first noticed", on=DAY)
    item.save()
    WatchReceipt.objects.create(user=viewer, item=item, first_told_on=DAY, last_told_on=DAY, told_step="new")
    WatchNote.objects.create(
        date=DAY,
        audience_key="country",
        audience_name="Whole country",
        text="Nothing new.",
        written_by="template",
    )
    DetectorSetting.ensure("report_due_soon")
    SectionMatch.objects.create(etools_name="Education")
    return item


@pytest.mark.parametrize("model", CHANGELISTS)
def test_the_lists_render_for_an_administrator(admin_client, filled, model):
    response = admin_client.get(reverse(f"admin:watch_{model}_changelist"))
    assert response.status_code == 200


def test_an_item_shows_its_story_and_evidence_read_only(admin_client, filled):
    response = admin_client.get(reverse("admin:watch_watchitem_change", args=[filled.pk]))
    html = response.content.decode()
    assert response.status_code == 200 and "first noticed" in html and "eTools progress reports" in html
    assert admin_client.post(reverse("admin:watch_watchitem_change", args=[filled.pk]), {}).status_code == 403
    assert admin_client.get(reverse("admin:watch_watchitem_add")).status_code == 403


@pytest.mark.parametrize("model", CHANGELISTS)
def test_the_lists_are_refused_to_a_viewer(client, viewer, filled, model):
    client.force_login(viewer)
    assert client.get(reverse(f"admin:watch_{model}_changelist")).status_code == 302  # not staff: sign-in
    viewer.is_staff = True
    viewer.save()
    viewer.groups.add(Group.objects.get(name=MANAGEMENT))  # Management gives no admin permission
    assert client.get(reverse(f"admin:watch_{model}_changelist")).status_code == 403


def test_the_lists_sit_in_data_and_sync(admin_client, filled):
    html = admin_client.get(reverse("admin:index")).content.decode()
    for title in ("NeuroDB Watch: things followed", "Morning notes (For you)", "NeuroDB Watch: checks"):
        assert title in html
    assert "ETools section names" in html or "eTools section names" in html


def test_an_administrator_switches_a_check_on(admin_client, admin_user):
    setting = DetectorSetting.ensure("report_due_soon")
    DetectorSetting.objects.filter(pk=setting.pk).update(demoted_at="2026-10-01T08:00Z", demoted_reason="45%")
    url = reverse("admin:watch_detectorsetting_change", args=[setting.pk])
    assert admin_client.post(url, {"mode": "on", "_save": "Save"}).status_code == 302
    setting.refresh_from_db()
    assert setting.mode == DetectorSetting.Mode.ON and setting.on_since is not None
    assert setting.demoted_at is None and setting.demoted_reason == ""
    assert setting.updated_by == admin_user.username
    assert admin_client.get(reverse("admin:watch_detectorsetting_add")).status_code == 403


def test_section_names_are_set_by_hand_and_confirmed(admin_client, admin_user):
    education = Section.objects.create(name="Education", code="EDU")
    contained = SectionMatch.objects.create(
        etools_name="Education / Learning", section=education, how=SectionMatch.How.CONTAINS
    )
    unmatched = SectionMatch.objects.create(etools_name="Social Policy")
    response = admin_client.post(
        reverse("admin:watch_sectionmatch_changelist"),
        {"action": "confirm", "_selected_action": [contained.pk, unmatched.pk]},
        follow=True,
    )
    assert "1 name(s) confirmed" in response.content.decode()
    contained.refresh_from_db()
    unmatched.refresh_from_db()
    assert contained.confirmed and contained.how == SectionMatch.How.CONTAINS and not unmatched.confirmed
    url = reverse("admin:watch_sectionmatch_change", args=[unmatched.pk])
    assert admin_client.post(url, {"section": education.pk, "_save": "Save"}).status_code == 302
    unmatched.refresh_from_db()
    assert unmatched.section == education and unmatched.how == SectionMatch.How.MANUAL
    assert unmatched.confirmed and unmatched.updated_by == admin_user.username
