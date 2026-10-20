"""Admin → Data and sync → "eTools Datamart: PRP business area code": one row an administrator sets, read by the
PRP datasets' sync after the hosting team's ETOOLS_DATAMART_BUSINESS_AREA and before the Datamart's workspaces."""

import re

import pytest
from django.urls import reverse

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.integrations.etools.datamart_sync import business_area_setting

pytestmark = pytest.mark.django_db


def _readonly(html: str, label: str) -> str:
    """The text of the read-only field labelled ``label`` on an admin change page."""
    found = re.search(
        rf">{re.escape(label)}</label>.*?<div class=\"readonly[^\"]*\">(.*?)</div>", html, flags=re.DOTALL
    )
    assert found, f"no read-only field {label!r}"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found[1])).strip()


def test_the_code_is_read_after_the_environment_and_normalised(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    assert business_area_setting() == ("", "")  # no row: the workspaces
    DatamartSetting.objects.create(pk=1, prp_business_area="")
    assert business_area_setting() == ("", "")  # an empty code: the workspaces too
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="2490")
    assert business_area_setting() == ("2490", "admin")
    settings.ETOOLS_DATAMART_BUSINESS_AREA = " 2491 "
    assert business_area_setting() == ("2491", "environment")


def test_the_page_opens_its_one_row_and_saves_the_code(admin_client, admin_user, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    response = admin_client.get(reverse("admin:core_datamartsetting_changelist"))
    setting = DatamartSetting.objects.get()  # opening the page makes the row, empty
    assert response.status_code == 302 and setting.prp_business_area == ""
    url = reverse("admin:core_datamartsetting_change", args=[setting.pk])
    assert response["Location"] == url
    html = admin_client.get(url).content.decode()
    assert "The business area code of the Datamart workspace named Lebanon" in _readonly(
        html, "The next sync takes"
    )
    assert admin_client.post(url, {"prp_business_area": " 2490 "}).status_code == 302
    setting.refresh_from_db()
    assert setting.prp_business_area == "2490" and setting.updated_by == admin_user
    assert _readonly(admin_client.get(url).content.decode(), "The next sync takes") == (
        "2490, the code entered here."
    )
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2491"
    said = _readonly(admin_client.get(url).content.decode(), "The next sync takes")
    assert said.startswith("2491, set by the hosting team") and "comes before the code entered here" in said


def test_a_code_with_other_characters_is_refused(admin_client):
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    response = admin_client.post(url, {"prp_business_area": "2490; 2340"})
    assert response.status_code == 200 and "Letters and digits only" in response.content.decode()
    assert DatamartSetting.objects.get().prp_business_area == ""


def test_the_page_lists_the_latest_runs_of_the_prp_datasets(admin_client):
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_indicator_reports")
    run.finish(SyncRun.Status.SUCCEEDED, business_area="2490", business_area_from="admin", read=7)
    failed = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_progress_reports")
    failed.finish(SyncRun.Status.FAILED, error="IntegrationError: GET /x: HTTP 403 Forbidden")
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    listed = _readonly(admin_client.get(url).content.decode(), "Latest runs of the PRP datasets")
    assert (
        "prp_indicator_reports" in listed
        and "business area 2490 (entered in the admin): 0 kept of 7 read" in listed
    )
    assert "HTTP 403 Forbidden" in listed and "prp_programme_documents : not synced" in listed


def test_the_page_has_one_row_is_for_administrators_and_is_in_data_and_sync(admin_client, client, viewer):
    from neurodb.web import admin_site

    assert admin_client.get(reverse("admin:core_datamartsetting_add")).status_code == 403
    setting = DatamartSetting.load()
    delete = reverse("admin:core_datamartsetting_delete", args=[setting.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403
    assert DatamartSetting.objects.count() == 1
    group = next(models for name, _, models in admin_site.GROUPS if "core.SyncRun" in models)
    assert "core.DatamartSetting" in group
    viewer.is_staff = True  # a Viewer in the admin has no permission on it
    viewer.save()
    client.force_login(viewer)
    assert client.get(reverse("admin:core_datamartsetting_changelist")).status_code == 403
