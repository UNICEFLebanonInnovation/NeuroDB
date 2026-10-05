"""Monitoring insights in the admin: its group, and Fields found (read-only for now)."""

import re

import pytest
from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR
from neurodb.core.models import SyncRun
from neurodb.fmm import access, refresh
from neurodb.fmm.models import FieldMapping
from neurodb.web import admin_site

from .conftest import CANARIES

pytestmark = pytest.mark.django_db

URL = "admin:fmm_fieldmapping_changelist"


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
    groups = [name for name, _, keys in admin_site.GROUPS if "fmm.FieldMapping" in keys]
    assert [str(name) for name in groups] == ["Monitoring insights"]


def test_fmm_icons_are_material_symbols_names_not_site_icons():
    sprite = render_to_string("components/icons.html")
    site_icons = set(re.findall(r'<symbol id="i-([\w-]+)"', sprite))
    icons = {key: icon for key, icon in admin_site.ICONS.items() if key.startswith("fmm.")}
    assert icons == {"fmm.FieldMapping": "data_object"}
    for icon in icons.values():
        assert re.fullmatch(r"[a-z0-9_]+", icon) and icon not in site_icons


def test_before_the_first_reading_it_says_how_to_start(admin_client):
    html = _page(admin_client)
    assert "The eTools keys have not been read yet" in html and "fmm_refresh --probe-only" in html
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


def test_a_failed_later_run_is_pointed_at(admin_client, fm_world):
    refresh.run(triggered_by="test", probe_only=True)
    failed = SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="probe", status=SyncRun.Status.FAILED)
    html = _page(admin_client)
    assert "A later refresh of" in html and reverse("admin:core_syncrun_change", args=[failed.pk]) in html


def test_fields_found_is_read_only(admin_client, fm_world):
    refresh.run(triggered_by="test", probe_only=True)
    mapping = FieldMapping.objects.get(dataset="fm_questions", field="answer")
    html = _page(admin_client)
    assert reverse("admin:fmm_fieldmapping_add") not in html
    assert admin_client.get(reverse("admin:fmm_fieldmapping_add")).status_code == 403
    response = admin_client.post(
        reverse("admin:fmm_fieldmapping_change", args=[mapping.pk]), {"override_key": "method"}
    )
    assert response.status_code == 403
    mapping.refresh_from_db()
    assert mapping.override_key == ""


def test_other_staff_and_signed_out_people_cannot_open_it(client, roles):
    editor = User.objects.create_user(username="editor", password="editor-pass-123456", is_staff=True)
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    client.force_login(editor)
    assert client.get(reverse(URL)).status_code == 403
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
