"""The KM Database in the admin (SPEC_km §2.6, §2.8): who opens the upload page, what it refuses, how it starts
the load, the version page (refreshing while it loads, the failed sentence and the upload button), the
documents list, the group, its icons and the admin home's quick action. Fake generated workbooks only."""

from __future__ import annotations

import datetime
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
from neurodb.km.models import KMDocument, KMOrganisation, KMVersion
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
    assert "This is a .csv file" in html and KMVersion.objects.count() == 0
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
    assert f"1 row of “{factory.FINDINGS_SHEET}” has no “Finding ID”: row 2." in html
    assert f'href="{reverse(UPLOAD)}"' in html and "refresh" not in html.split("<body", 1)[0]
    shown = factory.load(factory.workbook())
    html = _version_page(client_super, failed)
    assert f"Nothing was loaded. The KM Database still shows the version {shown.label()} (loaded on " in html


def test_the_upload_page_and_a_failed_version_give_the_local_day_once(client_super, settings):
    settings.TIME_ZONE = "Asia/Beirut"
    late = datetime.datetime(2026, 10, 20, 22, 30, tzinfo=datetime.UTC)  # 21 Oct 2026, 01:30 in Beirut
    shown = factory.load(factory.workbook(read_me=False))
    KMVersion.objects.filter(pk=shown.pk).update(finished_at=late)
    html = client_super.get(reverse(UPLOAD)).content.decode()
    assert "The version loaded on 21 Oct 2026, 01:30 (uploaded by —): 3 documents and 4 findings." in html
    failed = factory.load(factory.workbook(drop=("Key Finding",)))
    html = _version_page(client_super, failed)
    assert (
        "Nothing was loaded. The KM Database still shows the version loaded on 21 Oct 2026. Correct the "
        "workbook and upload it again."
    ) in html
    KMVersion.objects.filter(pk=shown.pk).update(prepared_on=datetime.date(2026, 10, 1), documents_count=1)
    html = client_super.get(reverse(UPLOAD)).content.decode()
    assert (
        "The version prepared on 1 Oct 2026, loaded on 21 Oct 2026, 01:30 (uploaded by —): 1 document and 4 "
        "findings."
    ) in html
    assert (
        "The KM Database still shows the version prepared on 1 Oct 2026 (loaded on 21 Oct 2026). Correct"
        in _version_page(client_super, failed)
    )


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
    assert f'<a href="{reverse("km:index")}?quality=review">See it on the page.</a>' in html
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
    assert "Shown in downloads as" in html and "Fakeorg Institute + 2 authors" in html
    assert "Doe, J." not in html  # the list shows how downloads write the authors
    document = KMDocument.objects.get(record_id=factory.record_id(1))
    assert client_super.get(reverse("admin:km_kmdocument_change", args=[document.pk])).status_code == 200
    assert client_super.get(reverse("admin:km_kmdocument_add")).status_code == 403


# ---------------------------------------------------------------------------------------------- placement
def test_the_group_its_labels_and_icons(superuser):
    keys = next(keys for name, _description, keys in admin_site.GROUPS if str(name) == "KM Database")
    assert keys == ["km.KMVersion", "km.KMDocument", "km.KMOrganisation", "km.KMQuestion"]
    names = [str(name) for name, _description, _keys in admin_site.GROUPS]
    assert names.index("KM Database") == names.index("Library and maps") + 1
    assert [admin_site.ICONS[key] for key in keys] == [
        "upload_file",
        "description",
        "corporate_fare",
        "forum",
    ]
    request = RequestFactory().get("/")
    request.user = superuser
    group = next(g for g in admin_site.sidebar_navigation(request) if str(g["title"]) == "KM Database")
    assert [(str(i["title"]), i["icon"]) for i in group["items"]] == [
        ("Versions", "upload_file"),
        ("Documents", "description"),
        ("Organisations named in downloads", "corporate_fare"),
        ("Questions to the KM Database", "forum"),
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
    assert ("km", "0002_organisation", "CreateModel") in QUICK_MIGRATIONS
    assert ("km", "0003_question", "CreateModel") in QUICK_MIGRATIONS
    assert ("km", "0004_ordinary_words", "AddField") in QUICK_MIGRATIONS


def test_problem_lines_are_escaped():
    lines = km_admin.lines(["<b>fake</b> See them on the page."])
    assert "&lt;b&gt;fake&lt;/b&gt;" in str(lines[0]) and "<a href=" in str(lines[0])


# ------------------------------------------------------------------------- the authors in downloads
def test_the_page_of_the_version_shown_reviews_the_authors(client_super):
    edits = {("documents", 3, "Publishing Entity / Authors"): "Fakeorg Agency; Jane Doe"}
    shown = factory.load(factory.workbook(edits=edits))
    html = _version_page(client_super, shown)
    assert (
        "Downloads name the organisations of 1 document; 0 list only authors (counted, not named); 2 mix "
        "both; 0 give no author."
    ) in html
    named = html.split("data-km-named", 1)[1].split("</details>", 1)[0]
    assert "Named as organisations in downloads (3)" in named
    for name in ("Fakeorg Agency", "Fakeorg Institute", "Fakeorg University"):
        assert f"<span>{name}</span>" in named
        assert f'<input type="hidden" name="name" value="{name}">' in named
    assert "Jane Doe" not in named and "Doe, J." not in named
    assert named.count(">Count as a person</button>") == 3 and "csrfmiddlewaretoken" in named
    review = reverse("admin:km_kmversion_review", args=[shown.pk])
    assert f'action="{review}"' in named and "?name=" not in html  # the string travels in the POST body


def test_count_as_a_person_changes_the_downloads_at_once(client_super, superuser):
    shown = factory.load(factory.workbook())
    review = reverse("admin:km_kmversion_review", args=[shown.pk])
    response = client_super.post(review, {"name": "Fakeorg University", "kind": "person"}, follow=True)
    assert response.redirect_chain[-1][0] == reverse("admin:km_kmversion_change", args=[shown.pk])
    row = KMOrganisation.objects.get()
    assert (row.name, row.folded, row.kind, row.added_by) == (
        "Fakeorg University",
        "fakeorg university",
        "person",
        superuser,
    )
    body = b"".join(client_super.get(reverse("km:documents"), {"export": "csv"}).streaming_content).decode()
    assert "Fakeorg University" not in body and "Authors: 1" in body
    client_super.post(review, {"name": "Fakeorg University", "kind": "words"})
    assert KMOrganisation.objects.get().kind == "words"  # the same row, its kind changed
    client_super.post(review, {"name": "Fakeorg Agency", "kind": "bogus"})  # not a kind
    assert KMOrganisation.objects.count() == 1
    assert client_super.get(review).status_code == 302  # a POST only


def test_the_review_is_for_those_who_set_up(client, roles):
    shown = factory.load(factory.workbook())
    client.force_login(_person("km-review-viewer", VIEWER))
    response = client.post(
        reverse("admin:km_kmversion_review", args=[shown.pk]), {"name": "x", "kind": "person"}
    )
    assert response.status_code in (302, 403) and not KMOrganisation.objects.exists()


def test_entries_that_look_like_words_have_a_not_a_name_button(client_super):
    edits = {("documents", 2, "Publishing Entity / Authors"): "Fakeword Phrase; Doe, J."}
    edits |= {("findings", n, "Finding Analysis"): "A fakeword phrase is common." for n in range(2, 25)}
    shown = factory.load(factory.workbook(documents=2, findings=23, edits=edits))
    html = _version_page(client_super, shown)
    assert "1 author entry looks like words, not a name: check it on this page." in html
    words = html.split("data-km-words", 1)[1].split("</details>", 1)[0]
    assert "Look like words, not names (1)" in words and "<span>Fakeword Phrase</span>" in words
    assert words.count(">Not a name</button>") == 1


def test_a_replaced_version_keeps_how_its_downloads_showed_the_authors(client_super):
    old = factory.load(factory.workbook())
    factory.load(factory.workbook(findings=5))
    old.refresh_from_db()
    assert old.status == "replaced"
    assert old.download_summary == {"organisations": 2, "authors_only": 0, "mixed": 1, "empty": 0}
    html = _version_page(client_super, old)
    assert "Downloads name the organisations of 2 documents" in html and "data-km-named" not in html


def test_the_organisations_admin(client_super, client, roles, superuser):
    add = reverse("admin:km_kmorganisation_add")
    response = client_super.post(add, {"name": "Fakeorg Collective", "kind": "organisation"})
    assert response.status_code == 302
    row = KMOrganisation.objects.get()
    assert (row.folded, row.added_by) == ("fakeorg collective", superuser)
    change = reverse("admin:km_kmorganisation_change", args=[row.pk])
    html = client_super.get(change).content.decode()
    assert "Corrects how downloads show an author entry." in html
    client_super.post(change, {"kind": "person", "name": "Changed name"})
    row.refresh_from_db()
    assert (row.name, row.kind) == ("Fakeorg Collective", "person")  # its kind only
    listing = client_super.get(reverse("admin:km_kmorganisation_changelist")).content.decode()
    assert "Fakeorg Collective" in listing and "A person: count, never name" in listing
    client_super.post(reverse("admin:km_kmorganisation_delete", args=[row.pk]), {"post": "yes"})
    assert not KMOrganisation.objects.exists()
    client.force_login(_person("km-org-viewer", VIEWER))
    assert client.get(reverse("admin:km_kmorganisation_changelist")).status_code in (302, 403)
    client.force_login(_person("km-org-admin", ADMIN))
    assert client.get(reverse("admin:km_kmorganisation_changelist")).status_code == 200


def test_the_questions_admin_is_read_only_for_those_who_set_up(client_super, client, roles, superuser):
    from neurodb.km.models import KMQuestion

    row = KMQuestion.objects.create(
        user=superuser,
        question="What does the fake evidence say?",
        status="answered",
        covered="partly",
        read=["TST-0001-F00001", "TST-0001-F00002"],
        cited=["TST-0001-F00001"],
        uncited=1,
        input_tokens=1200,
        output_tokens=300,
        duration_ms=4200,
    )
    html = client_super.get(reverse("admin:km_kmquestion_changelist")).content.decode()
    for shown in ("What does the fake evidence say?", "Answered", "In part", "1,200 / 300", "4.2 s"):
        assert shown in html, shown
    assert client_super.get(reverse("admin:km_kmquestion_change", args=[row.pk])).status_code == 200
    assert client_super.get(reverse("admin:km_kmquestion_add")).status_code == 403
    client.force_login(_person("km-admin-q", ADMIN))
    assert client.get(reverse("admin:km_kmquestion_changelist")).status_code == 200
    client.force_login(_person("km-viewer-q", VIEWER))
    assert client.get(reverse("admin:km_kmquestion_changelist")).status_code in (302, 403)


# ------------------------------------------------------------------------------- the review's checks
def test_a_string_named_as_an_organisation_can_be_left_out(client_super):
    edits = {("documents", 3, "Publishing Entity / Authors"): "Fakeorg Agency; Fakeorg Report Annex"}
    shown = factory.load(factory.workbook(edits=edits))
    named = _version_page(client_super, shown).split("data-km-named", 1)[1].split("</details>", 1)[0]
    assert named.count(">Not an organisation (leave it out)</button>") == named.count(
        ">Count as a person</button>"
    )
    assert "press “Not an organisation”: downloads leave it out." in named
    review = reverse("admin:km_kmversion_review", args=[shown.pk])
    client_super.post(review, {"name": "Fakeorg Report Annex", "kind": "words"})
    assert KMOrganisation.objects.get().kind == "words"
    body = b"".join(client_super.get(reverse("km:documents"), {"export": "csv"}).streaming_content).decode()
    assert "Fakeorg Report Annex" not in body and "Fakeorg Agency" in body


def test_entries_the_texts_also_write_as_words_are_listed_with_two_buttons(client_super):
    edits = {("documents", 2, "Publishing Entity / Authors"): "Fakeorg Institute; Fakeword; Jane Fakewing"}
    edits |= {("findings", n, "Finding Analysis"): "The fakeword and fakewing approach." for n in range(2, 8)}
    shown = factory.load(factory.workbook(documents=1, findings=6, edits=edits))
    html = _version_page(client_super, shown)
    assert "2 author entries look like words, not names: check them on this page." in html
    listed = html.split("data-km-words", 1)[1].split("</details>", 1)[0]
    assert "<span>Fakeword</span>" in listed and "<span>Jane Fakewing</span>" in listed
    assert (
        listed.count(">Not a name</button>") == 2 and listed.count(">Count as an organisation</button>") == 2
    )
    review = reverse("admin:km_kmversion_review", args=[shown.pk])
    client_super.post(review, {"name": "Fakeword", "kind": "organisation"})
    assert KMOrganisation.objects.get().kind == "organisation"
    listed = _version_page(client_super, shown).split("data-km-words", 1)[1].split("</details>", 1)[0]
    assert "<span>Fakeword</span>" not in listed and "<span>Jane Fakewing</span>" in listed


def test_the_upload_page_refuses_a_damaged_sheet_plainly(client_super):
    from tests.km.test_workbook import _damaged

    html = _upload(client_super, _damaged()).content.decode()
    assert "This file is not an Excel workbook NeuroDB can open." in html
    assert not KMVersion.objects.exists()
