"""The KM Database in the admin (SPEC_km §2.6, §2.8): who opens the upload page, what it refuses, how it starts
the load, the version page (refreshing while it loads, the failed sentence and the upload button), the
documents list, the group, its icons and the admin home's quick action. Fake generated workbooks only."""

from __future__ import annotations

import io

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, VIEWER
from neurodb.integrations import background
from neurodb.km import admin as km_admin
from neurodb.km import loading, workbook
from neurodb.km.models import KMDocument, KMVersion
from neurodb.web import admin_site
from tests.km import factory

pytestmark = pytest.mark.django_db

UPLOAD = "admin:km_kmversion_upload"
LIST = "admin:km_kmversion_changelist"


def _person(username: str, role: str, *, is_staff=True, is_superuser=False) -> User:
    user = User.objects.create_user(
        username=username, password="pass-123456-x", is_staff=is_staff, is_superuser=is_superuser
    )
    user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def superuser(roles):
    return _person("km-root", VIEWER, is_superuser=True)


@pytest.fixture
def client_super(client, superuser):
    client.force_login(superuser)
    return client


@pytest.fixture
def started(monkeypatch):
    """The background start, recorded instead of run."""
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 4242)
    return calls


def _upload(client, data: bytes, name: str = "fake.xlsx", **ticks):
    payload = {"workbook": SimpleUploadedFile(name, data), **{k: "on" for k, v in ticks.items() if v}}
    return client.post(reverse(UPLOAD), payload, follow=True)


# ---------------------------------------------------------------------------------------- who opens it
def test_superusers_and_staff_administrators_open_the_upload_page(client, roles):
    for user in (_person("km-root-2", VIEWER, is_superuser=True), _person("km-admin", ADMIN)):
        client.force_login(user)
        for name in (UPLOAD, LIST, "admin:km_kmversion_template", "admin:km_kmdocument_changelist"):
            assert client.get(reverse(name)).status_code == 200, (user.username, name)
    client.force_login(_person("km-staff-viewer", VIEWER))
    for name in (UPLOAD, LIST, "admin:km_kmversion_template", "admin:km_kmdocument_changelist"):
        assert client.get(reverse(name)).status_code == 403, name
    client.force_login(
        _person("km-plain-admin", ADMIN, is_staff=False)
    )  # no staff status: the admin's sign-in
    response = client.get(reverse(UPLOAD))
    assert response.status_code == 302 and "login" in response["Location"]


def test_the_upload_page_says_what_the_check_asks_for(client_super):
    html = client_super.get(reverse(UPLOAD)).content.decode()
    assert "None yet: the KM Database is empty until a workbook is loaded." in html
    assert "A “Finding ID” for every finding and a “Record ID” for every document, each used once." in html
    assert "Every finding&#x27;s “Record ID” is in the documents sheet." in html
    assert f'href="{reverse("admin:km_kmversion_template")}"' in html
    assert 'enctype="multipart/form-data"' in html and "It has fewer findings on purpose" in html


def test_the_excel_template_downloads(client_super):
    response = client_super.get(reverse("admin:km_kmversion_template"))
    assert response["Content-Disposition"] == 'attachment; filename="km-database-template.xlsx"'
    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    assert {"KM Documents", "KM Findings", "Read Me"} == set(wb.sheetnames)


# --------------------------------------------------------------------------------------------- uploads
def test_a_workbook_with_problems_saves_nothing(client_super, started, monkeypatch):
    html = _upload(client_super, factory.workbook(drop=("Key Finding",))).content.decode()
    assert f"The sheet “{factory.FINDINGS_SHEET}” has no column “Key Finding”." in html
    assert "Nothing was loaded. The KM Database is still empty." in html
    assert KMVersion.objects.count() == 0 and started == []
    html = _upload(client_super, b"a,b", "fake.csv").content.decode()
    assert "This is a CSV file" in html and KMVersion.objects.count() == 0
    shown = factory.load(factory.workbook())
    html = _upload(client_super, factory.workbook(header_row=2)).content.decode()
    assert f"Nothing was loaded. The KM Database still shows the version {shown.label()}." in html
    assert KMVersion.objects.count() == 1
    monkeypatch.setattr(workbook, "MAX_UPLOAD_BYTES", 100)  # a file over the limit is not even read
    html = _upload(client_super, factory.workbook(findings=6)).content.decode()
    assert "the upload takes at most 25 MB." in html and KMVersion.objects.count() == 1 and started == []


def test_a_good_upload_queues_a_version_and_starts_the_load(client_super, superuser, started):
    data = factory.workbook()
    response = _upload(client_super, data, "C:\\fake\\folder\\fake upload.xlsx")
    version = KMVersion.objects.get()
    assert response.redirect_chain[-1][0] == reverse("admin:km_kmversion_change", args=[version.pk])
    assert version.status == "queued" and bytes(version.content) == data
    assert (version.file_name, version.file_size, version.uploaded_by) == (
        "fake upload.xlsx",
        len(data),
        superuser,
    )
    assert started == [("load_km_database", "--version-id", str(version.pk), "--triggered-by", "km-root")]
    html = response.content.decode()
    assert "The workbook is being loaded. This page shows when it is ready." in html
    assert (
        '<meta http-equiv="refresh" content="5">' in html
        and "Loading… this page refreshes by itself." in html
    )


def test_the_fewer_tick_is_passed_to_the_load(client_super, started):
    _upload(client_super, factory.workbook(), fewer=True)
    version = KMVersion.objects.get()
    assert version.fewer_ok and started[0][-1] == "--fewer-ok"


def test_the_file_shown_now_is_refused_unless_ticked(client_super, started):
    data = factory.workbook()
    factory.load(data)
    html = _upload(client_super, data).content.decode()
    assert (
        "This file is the version shown now. Tick “Load it even if it is the version shown now” to load it "
        "again."
    ) in html
    assert started == []
    _upload(client_super, data, again=True)
    assert len(started) == 1


def test_an_upload_while_a_load_runs_is_refused_with_a_link(client_super, started):
    running = factory.version(factory.workbook())  # queued seconds ago: it may still start
    html = _upload(client_super, factory.workbook(findings=5)).content.decode()
    assert "A version is loading now. Wait until it finishes (about a minute), then upload again." in html
    assert f'href="{reverse("admin:km_kmversion_change", args=[running.pk])}"' in html
    assert KMVersion.objects.count() == 1 and started == []


# ---------------------------------------------------------------------------------------- the version page
def _version_page(client, version) -> str:
    response = client.get(reverse("admin:km_kmversion_change", args=[version.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_a_failed_version_says_what_stays_and_offers_the_upload(client_super):
    failed = factory.load(factory.workbook(edits={("findings", 2, "Finding ID"): None}))
    html = _version_page(client_super, failed)
    assert (
        "Nothing was loaded. The KM Database is still empty. Correct the workbook and upload it again."
        in html
    )
    assert f"1 rows of “{factory.FINDINGS_SHEET}” have no “Finding ID”, for example rows 2." in html
    assert f'href="{reverse(UPLOAD)}"' in html and "refresh" not in html.split("<body", 1)[0]
    shown = factory.load(factory.workbook())
    html = _version_page(client_super, failed)
    assert f"Nothing was loaded. The KM Database still shows the version {shown.label()} (loaded on " in html


def test_a_version_stopped_by_an_error_says_so_too(client_super, monkeypatch):
    shown = factory.load(factory.workbook())
    monkeypatch.setattr(loading, "_fill_vectors", lambda: 1 / 0)
    stopped = factory.load(factory.workbook(findings=5))
    html = _version_page(client_super, stopped)
    assert f"The KM Database still shows the version {shown.label()}" in html
    assert "The load stopped before it finished (ZeroDivisionError)." in html


def test_a_loading_version_while_no_load_runs_is_failed_at_the_next_view(client_super):
    stopped = factory.version(b"fake", status="loading")
    html = _version_page(client_super, stopped)
    assert "The load stopped before it finished (the server restarted). Upload the workbook again." in html
    assert '<meta http-equiv="refresh"' not in html


def test_the_warnings_link_the_findings_to_review(client_super):
    edits = {("findings", 2, "Data Quality Status"): "Review required: fake reason"}
    version = factory.load(factory.workbook(edits=edits))
    html = _version_page(client_super, version)
    assert f'<a href="{reverse("km:index")}?quality=review">See them on the page.</a>' in html
    assert "fake_source.xlsx" in html and "C:" not in html  # the Read Me's source workbook: its name only


def test_the_version_list_its_columns_and_who_deletes(client_super, superuser, roles, client):
    shown = factory.load(factory.workbook())
    failed = factory.load(factory.workbook(drop=("Key Finding",)))
    html = client_super.get(reverse(LIST)).content.decode()
    for label in ("Status", "Prepared on", "Findings", "Documents", "Problems", "Uploaded by", "Uploaded at"):
        assert label in html, label
    assert "Shown now" in html and "Not loaded" in html and "Upload a new version" in html
    assert f'href="{reverse(UPLOAD)}"' in html
    assert "delete_selected" not in html
    assert client_super.get(reverse("admin:km_kmversion_delete", args=[shown.pk])).status_code == 403
    client_super.post(reverse("admin:km_kmversion_delete", args=[failed.pk]), {"post": "yes"})
    assert not KMVersion.objects.filter(pk=failed.pk).exists()
    client.force_login(_person("km-admin-2", ADMIN))  # an Administrator does not delete
    assert client.get(reverse("admin:km_kmversion_delete", args=[shown.pk])).status_code == 403


def test_the_admin_never_reads_the_uploaded_file():
    request = RequestFactory().get("/")
    request.user = User(is_superuser=True, is_staff=True)
    from django.contrib import admin

    queryset = admin.site._registry[KMVersion].get_queryset(request)
    assert "content" in queryset.query.deferred_loading[0]


def test_the_documents_list(client_super):
    factory.load(factory.workbook())
    html = client_super.get(reverse("admin:km_kmdocument_changelist")).content.decode()
    for label in ("Document ID", "Title", "Year", "Findings", "Fake document title 1", "TST-0002"):
        assert label in html, label
    document = KMDocument.objects.get(record_id=factory.record_id(1))
    assert client_super.get(reverse("admin:km_kmdocument_change", args=[document.pk])).status_code == 200
    assert client_super.get(reverse("admin:km_kmdocument_add")).status_code == 403


# ---------------------------------------------------------------------------------------------- placement
def test_the_group_its_labels_and_icons(superuser):
    keys = next(keys for name, _description, keys in admin_site.GROUPS if str(name) == "KM Database")
    assert keys == ["km.KMVersion", "km.KMDocument"]
    names = [str(name) for name, _description, _keys in admin_site.GROUPS]
    assert names.index("KM Database") == names.index("Library and maps") + 1
    assert (admin_site.ICONS["km.KMVersion"], admin_site.ICONS["km.KMDocument"]) == (
        "upload_file",
        "description",
    )
    request = RequestFactory().get("/")
    request.user = superuser
    group = next(g for g in admin_site.sidebar_navigation(request) if str(g["title"]) == "KM Database")
    assert [(str(i["title"]), i["icon"]) for i in group["items"]] == [
        ("Versions", "upload_file"),
        ("Documents", "description"),
    ]


def test_the_quick_action_is_for_those_who_hold_the_permission(client, client_super, roles):
    link = f'href="{reverse(UPLOAD)}"'
    html = client_super.get(reverse("admin:index")).content.decode()
    assert link in html and "Upload the KM Database" in html
    client.force_login(_person("km-admin-3", ADMIN))
    assert link in client.get(reverse("admin:index")).content.decode()
    client.force_login(_person("km-staff-viewer-2", VIEWER))
    assert link not in client.get(reverse("admin:index")).content.decode()


def test_its_migration_is_listed_as_quick():
    from tests.datamart.test_backfill import QUICK_MIGRATIONS

    assert ("km", "0001_initial", "CreateModel") in QUICK_MIGRATIONS


def test_problem_lines_are_escaped():
    lines = km_admin.lines(["<b>fake</b> See them on the page."])
    assert "&lt;b&gt;fake&lt;/b&gt;" in str(lines[0]) and "<a href=" in str(lines[0])
