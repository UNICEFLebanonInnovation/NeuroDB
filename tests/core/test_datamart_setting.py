"""Admin → Data and sync → "eTools Datamart: PRP business area code": one row an administrator sets, read by the
PRP datasets' sync before the hosting team's ETOOLS_DATAMART_BUSINESS_AREA; a value that is not a code (digits)
is set aside, and with no code NeuroDB finds it (the workspace it holds, the Datamart's workspaces, the partner
report rows)."""

import re

import pytest
from django.urls import reverse

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.datamart.models import DatamartDocument, ReportedIndicator
from neurodb.integrations.etools.datamart_sync import AreaCode, business_area_setting

pytestmark = pytest.mark.django_db


def _readonly(html: str, label: str) -> str:
    """The text of the read-only field labelled ``label`` on an admin change page."""
    found = re.search(
        rf">{re.escape(label)}</label>.*?<div class=\"readonly[^\"]*\">(.*?)</div>", html, flags=re.DOTALL
    )
    assert found, f"no read-only field {label!r}"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found[1])).strip()


def _workspace(code="2490", name="Lebanon", key="2"):
    """The country's workspace as the last sync of ``workspaces`` stored it."""
    return DatamartDocument.objects.create(
        dataset="workspaces", record_key=key, data={"name": name, "business_area_code": code}
    )


def test_the_code_entered_comes_first_and_a_name_is_set_aside(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    assert business_area_setting() == AreaCode()  # no row: NeuroDB finds it
    DatamartSetting.objects.create(pk=1, prp_business_area="")
    assert business_area_setting() == AreaCode()  # an empty code: the same
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="2490")
    assert business_area_setting() == AreaCode("2490", "admin")
    settings.ETOOLS_DATAMART_BUSINESS_AREA = " 2491 "  # the admin's code comes first, even over a code
    assert business_area_setting() == AreaCode("2490", "admin")
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="")
    assert business_area_setting() == AreaCode("2491", "environment")
    settings.ETOOLS_DATAMART_BUSINESS_AREA = " 2490.0 "
    assert business_area_setting() == AreaCode("2490", "environment")
    # the hosting team's country name is never a code: set aside, with where it came from
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    assert business_area_setting() == AreaCode("", "", {"environment": "Lebanon"})
    # an old admin value, saved before the validator took digits only, is set aside too
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="LB")
    assert business_area_setting() == AreaCode("", "", {"admin": "LB", "environment": "Lebanon"})
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="2490")
    assert business_area_setting() == AreaCode("2490", "admin", {"environment": "Lebanon"})


def test_the_page_opens_its_one_row_and_saves_the_code(admin_client, admin_user, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    response = admin_client.get(reverse("admin:core_datamartsetting_changelist"))
    setting = DatamartSetting.objects.get()  # opening the page makes the row, empty
    assert response.status_code == 302 and setting.prp_business_area == ""
    url = reverse("admin:core_datamartsetting_change", args=[setting.pk])
    assert response["Location"] == url
    html = admin_client.get(url).content.decode()
    said = _readonly(html, "The next sync takes")
    assert said.startswith("No code: nothing is entered here, the hosting team set none")
    assert "asks the Datamart&#x27;s list of workspaces; if that has none either, the sync fails" in said
    assert _readonly(html, "Codes NeuroDB finds") == (
        "In the workspace NeuroDB holds: none. On the partner report rows held: none."
    )
    assert admin_client.post(url, {"prp_business_area": " 2490 "}).status_code == 302
    setting.refresh_from_db()
    assert setting.prp_business_area == "2490" and setting.updated_by == admin_user
    assert _readonly(admin_client.get(url).content.decode(), "The next sync takes") == (
        "2490, the code entered here."
    )
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2491"
    said = _readonly(admin_client.get(url).content.decode(), "The next sync takes")
    assert said == (
        "2490, the code entered here. The hosting team&#x27;s ETOOLS_DATAMART_BUSINESS_AREA (2491) is not used "
        "while a code is entered here."
    )
    setting.prp_business_area = ""
    setting.save()
    said = _readonly(admin_client.get(url).content.decode(), "The next sync takes")
    assert (
        said
        == "2491, set by the hosting team (ETOOLS_DATAMART_BUSINESS_AREA). A code entered here comes first."
    )


def test_a_code_with_other_characters_is_refused(admin_client):
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    for value in ("2490; 2340", "Lebanon", "LB"):
        response = admin_client.post(url, {"prp_business_area": value})
        assert response.status_code == 200 and "Digits only, as in 2490" in response.content.decode()
        assert DatamartSetting.objects.get().prp_business_area == ""


def test_the_page_says_a_hosting_name_is_set_aside_and_which_code_the_next_sync_takes(admin_client, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    said = _readonly(admin_client.get(url).content.decode(), "The next sync takes")
    assert said.startswith(
        "No code: nothing is entered here, the hosting team set “Lebanon”, a name, not a business area code, "
        "the workspace NeuroDB holds has none"
    )
    _workspace()
    html = admin_client.get(url).content.decode()
    assert _readonly(html, "The next sync takes") == (
        "2490, found in the workspace NeuroDB holds (nothing is entered here, and the hosting team&#x27;s "
        "ETOOLS_DATAMART_BUSINESS_AREA, “Lebanon”, is a name, not a business area code). Enter it here, or "
        "press Use 2490 below, to keep it whatever the Datamart sends."
    )
    found = _readonly(html, "Codes NeuroDB finds")
    assert found.startswith("In the workspace NeuroDB holds: 2490 (synced ")
    assert found.endswith("On the partner report rows held: none. Use 2490")
    assert 'form="use_found_code_form"' in html
    # with no workspace held, the partner report rows give it, saying how many carry it
    DatamartDocument.objects.filter(dataset="workspaces").delete()
    for pk, area in ((1, "2490"), (2, " 2490.0"), (3, {"code": "2490"}), (4, "2340")):
        ReportedIndicator.objects.create(datamart_id=pk, data={"business_area": area})
    html = admin_client.get(url).content.decode()
    assert _readonly(html, "The next sync takes").startswith(
        "2490, the code most partner report rows carry on 3 of 4 rows (nothing is entered here"
    )
    assert "On the partner report rows held: 2490 on 3 of 4." in _readonly(html, "Codes NeuroDB finds")
    # the code entered comes first: the hosting name is said not to be used, and the button goes
    DatamartSetting.objects.filter(pk=1).update(prp_business_area="2490")
    html = admin_client.get(url).content.decode()
    assert _readonly(html, "The next sync takes") == (
        "2490, the code entered here. The hosting team&#x27;s ETOOLS_DATAMART_BUSINESS_AREA (“Lebanon”) is a "
        "name, not a business area code: it is not used."
    )
    assert 'name="use_found_code"' not in html


def test_enter_in_the_code_field_saves_the_code_typed_never_the_one_found(admin_client, settings):
    """*Use 2490* belongs to a form of its own, after the change form: no submit button of the change form
    comes before Save, so Enter in "PRP business area code" saves the code typed, not the one found."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    _workspace()
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    html = admin_client.get(url).content.decode()
    start = html.index('id="datamartsetting_form"')
    end = html.index("</form>", start)
    inside = [b for b in re.findall(r"<button\b[^>]*>", html[start:end]) if 'type="submit"' in b]
    assert inside and all('form="use_found_code_form"' in b for b in inside)  # none is the change form's
    assert "formaction" not in html[start:end] and 'name="prp_business_area"' in html[start:end]
    use = reverse("admin:core_datamartsetting_use_found_code", args=[1])
    outer = re.search(r'<form id="use_found_code_form" method="post" action="([^"]+)">', html[end:])
    assert outer and outer[1] == use
    response = admin_client.post(url, {"prp_business_area": "2491", "_save": "Save"})  # what Enter sends
    assert response.status_code == 302 and DatamartSetting.objects.get().prp_business_area == "2491"


def test_use_the_found_code_saves_the_code_found_on_the_server(admin_client, admin_user, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    setting = DatamartSetting.load()
    use = reverse("admin:core_datamartsetting_use_found_code", args=[setting.pk])
    change = reverse("admin:core_datamartsetting_change", args=[setting.pk])
    assert admin_client.get(use).status_code == 405  # POST only
    response = admin_client.post(use, {"use_found_code": "1"}, follow=True)  # nothing found: nothing saved
    assert "NeuroDB finds no code" in response.content.decode()
    setting.refresh_from_db()
    assert setting.prp_business_area == ""
    _workspace()
    # the code comes from what NeuroDB holds, never from the request: a forged code in the body is ignored
    response = admin_client.post(use, {"use_found_code": "1", "prp_business_area": "9999", "code": "9999"})
    assert response.status_code == 302 and response["Location"] == change
    setting.refresh_from_db()
    assert setting.prp_business_area == "2490" and setting.updated_by == admin_user
    assert "2490 is now the code entered here." in admin_client.get(change).content.decode()


def test_use_the_found_code_needs_the_change_permission(client, viewer, settings):
    from django.contrib.auth.models import Permission

    settings.ETOOLS_DATAMART_BUSINESS_AREA = ""
    _workspace()
    setting = DatamartSetting.load()
    viewer.is_staff = True  # a Viewer who may see the page, not change it
    viewer.save()
    viewer.user_permissions.add(Permission.objects.get(codename="view_datamartsetting"))
    client.force_login(viewer)
    change = reverse("admin:core_datamartsetting_change", args=[setting.pk])
    response = client.get(change)
    assert response.status_code == 200
    html = response.content.decode()
    assert "In the workspace NeuroDB holds: 2490" in html  # what NeuroDB finds, without the button
    assert 'name="use_found_code"' not in html and "use_found_code_form" not in html
    use = reverse("admin:core_datamartsetting_use_found_code", args=[setting.pk])
    assert client.post(use, {"use_found_code": "1"}).status_code == 403
    setting.refresh_from_db()
    assert setting.prp_business_area == "" and setting.updated_by is None


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
    # records that named no business area were kept unconfirmed: the line says so and what to press
    unnamed = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_programme_documents")
    unnamed.finish(SyncRun.Status.PARTIAL, business_area="2490", business_area_from="admin", read=3,
                   country_unnamed=3)  # fmt: skip
    listed = _readonly(admin_client.get(url).content.decode(), "Latest runs of the PRP datasets")
    assert "3 named no business area, kept unconfirmed" in listed
    assert "press Run a job → Probe a Datamart dataset" in listed


def test_the_latest_runs_warn_of_a_name_asked_as_a_code_and_say_what_was_set_aside(admin_client):
    asked = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_indicator_reports")
    asked.finish(SyncRun.Status.SUCCEEDED, business_area="Lebanon", business_area_from="environment", read=0)
    found = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_programme_documents")
    found.finish(SyncRun.Status.SUCCEEDED, business_area="2490", business_area_from="workspace_held", read=3,
                 business_area_set_aside={"environment": "Lebanon"})  # fmt: skip
    url = reverse("admin:core_datamartsetting_change", args=[DatamartSetting.load().pk])
    html = admin_client.get(url).content.decode()
    assert (
        '<span class="text-warning">business area Lebanon (set by the hosting team), a name, not a business area '
        "code: 0 kept of 0 read</span>"
    ) in html
    listed = _readonly(html, "Latest runs of the PRP datasets")
    assert (
        "business area 2490 (found in the workspace NeuroDB holds): 0 kept of 3 read · the hosting team&#x27;s "
        "“Lebanon” set aside"
    ) in listed


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
