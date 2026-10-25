"""Compare with Power BI's pages (Admin → Data and sync; datamart.powerbi_admin): who opens them, the sidebar and
the admin home's link, the upload page (what to do now, one line per Power BI page in Power BI's order, the files
not read, the hidden files, the columns dropped, the reload while busy), an export's page, how to export, and
that nothing of these tables reaches the AI or a developer table. Generated fake exports only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.datamart import comparison, powerbi_filters
from neurodb.datamart import powerbi_compare as pc
from neurodb.datamart import powerbi_exports as px
from neurodb.datamart import powerbi_words as words
from neurodb.datamart.models import PowerBIDifference, PowerBIExport, PowerBIUpload
from neurodb.integrations import background
from neurodb.web import admin_site
from tests.datamart import powerbi_factory as pf

pytestmark = pytest.mark.django_db
INDEX = reverse("admin:datamart_powerbi")
ROOT = Path(settings.BASE_DIR) / "neurodb"


@pytest.fixture
def started(monkeypatch):
    found = []
    monkeypatch.setattr(background, "start_command", lambda *args: found.append(args) or 4242)
    return found


@pytest.fixture
def checker(client, roles):
    client.force_login(pf.user("checker"))
    return client


def _done(client, *files) -> PowerBIUpload:
    response = pf.post(client, *files)
    assert response.status_code == 302, response.content[:300]
    upload = PowerBIUpload.objects.order_by("-pk").first()
    pc.run(upload.pk)
    upload.refresh_from_db()
    return upload


# ------------------------------------------------------------------------------------------------- access
def test_only_validators_with_staff_status_open_compare_with_power_bi(client, roles, admin_user, started):
    owner = pf.user("owner")
    client.force_login(owner)
    upload = _done(client, ("cards.xlsx", pf.export("partner_cards")))
    export = PowerBIExport.objects.get(upload=upload)
    other = _done(client, ("sector.xlsx", pf.export("by_sector")))
    client.logout()
    pages = [
        ("get", INDEX),
        ("post", INDEX),
        ("get", reverse("admin:datamart_powerbi_upload", args=[upload.pk])),
        ("get", reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk])),
        ("post", reverse("admin:datamart_powerbi_again", args=[upload.pk])),
    ]

    def statuses():
        return [getattr(client, method)(url).status_code for method, url in pages]

    assert statuses() == [302] * 5  # anonymous: the admin's sign-in
    client.force_login(pf.user("plain-validator", staff=False))
    assert statuses() == [302] * 5  # the admin asks for staff status
    for user in (pf.user("staff-viewer", validator=False), admin_user):  # an Administrator not ticked: no
        client.force_login(user)
        assert statuses() == [404] * 5, user
    for user in (pf.user("staff-validator"), pf.user("root", superuser=True)):
        client.force_login(user)
        found = statuses()
        assert found[0] == 200 and found[2] == 200 and found[3] == 200, (user, found)
        assert found[1] == 200  # a POST without files: "Choose the files to upload."
        assert found[4] in (200, 302)
    # an export of another upload is not found
    assert client.get(reverse("admin:datamart_powerbi_export", args=[other.pk, export.pk])).status_code == 404


def test_the_sidebar_and_the_admin_home_link_it_for_validators(client, roles, admin_user, rf):
    from neurodb.web.admin_site import DATA_AND_SYNC, sidebar_navigation

    def pages(user) -> list[str]:
        request = rf.get("/")
        request.user = user
        groups = [g for g in sidebar_navigation(request) if str(g["title"]) == str(DATA_AND_SYNC)]
        return [str(i["title"]) for g in groups for i in g["items"]][:3]

    checker = pf.user("checker")
    assert pages(checker) == ["eTools reporting checks", "Raw eTools data", "Compare with Power BI"]
    assert "Compare with Power BI" not in pages(admin_user)
    client.force_login(checker)
    html = client.get(reverse("admin:index")).content.decode()
    assert f'href="{INDEX}"' in html
    raw = client.get(reverse("admin:datamart_raw")).content.decode()
    assert f'href="{INDEX}"' in raw and words.RAW_LINK in raw
    client.force_login(admin_user)
    assert f'href="{INDEX}"' not in client.get(reverse("admin:index")).content.decode()


# --------------------------------------------------------------------------------------------- the pages
def test_the_first_page_says_how_to_export_every_visual_in_power_bi_s_order(checker):
    html = checker.get(INDEX).content.decode()
    visuals = re.findall(r"<li>(.*?)</li>", html.split("data-pbi-visuals", 1)[1].split("</ol>", 1)[0])
    assert len(visuals) == 13
    order = [comparison.POWERBI_PAGE_ORDER.index(v.split(" → ")[0]) for v in visuals]
    assert order == sorted(order)
    assert words.UPLOAD_BUTTON in html and 'enctype="multipart/form-data"' in html


def test_the_upload_page_lists_every_power_bi_page_in_order(checker, started):
    upload = _done(
        checker,
        (
            "exports.zip",
            pf.archive(
                ("cards.xlsx", pf.export("partner_cards")),
                ("list.xlsx", pf.export("report_list", people=True)),
            ),
        ),
    )
    html = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    table = html.split("data-pbi-pages", 1)[1].split("</table>", 1)[0]
    pages = re.findall(r'<th scope="row" rowspan="\d+">(.*?)</th>', table)
    assert pages == [p.replace("&", "&amp;") for p in comparison.POWERBI_PAGE_ORDER]
    assert table.count(words.NOT_UPLOADED) == 11  # 13 visuals, 2 uploaded
    assert "cards.xlsx" in table and "list.xlsx" in table
    todo = re.findall(r"<li>(.*?)</li>", html.split("data-pbi-todo", 1)[1].split("</ul>", 1)[0])
    assert 1 <= len(todo) <= 4
    assert todo == [
        "1 file was not read: see why below, then export it again.",
        "11 visuals are not uploaded: export them too (see How to export).",
    ]
    assert words.NOT_EXCEL.format(name="notes.txt") in html
    assert "Skipped 3 hidden files a Mac adds to a zip." in html
    assert (
        "Dropped as the file was read: UNICEF Focal Points (people&#x27;s names). It was never stored."
        in html
    )
    assert 'http-equiv="refresh"' not in html and words.AGAIN_BUTTON in html


def test_the_upload_page_reloads_itself_while_the_job_runs(checker, started):
    pf.post(checker, ("cards.xlsx", pf.export("partner_cards")))
    upload = PowerBIUpload.objects.get()
    html = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    assert '<meta http-equiv="refresh" content="5">' in html and words.STARTED in html
    assert words.AGAIN_BUTTON not in html


def _todo(html: str) -> list[str]:
    if "data-pbi-todo" not in html:
        return []
    return re.findall(r"<li>(.*?)</li>", html.split("data-pbi-todo", 1)[1].split("</ul>", 1)[0])


def test_a_filter_neurodb_cannot_apply_is_said_on_the_upload_and_export_pages(checker, started):
    """What to do now names the slicer as the report shows it (Section, PD status); Power BI's field names stay on
    the export's page, in its filter table."""
    filters = (
        "status is not active",
        "section_name is Fake Section Nobody Has",
        "actual_value is between 1 and 2",
        "name is Zq Unknownperson",
    )
    upload = _done(checker, ("sector.xlsx", pf.export("by_sector", filters=filters)))
    html = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    assert _todo(html) == [
        words.plural(12, words.TODO_MISSING_ONE, words.TODO_MISSING),
        words.TODO_CANNOT.format(
            clauses="the PD status slicer; the Section slicer; the actual_value filter in the Filters pane; "
            "the name filter in the Filters pane"
        ).replace("'", "&#x27;"),
    ]
    assert "section_name" not in html and "status is not" not in html
    export = PowerBIExport.objects.get(upload=upload)
    page = checker.get(reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk])).content.decode()
    assert words.CLAUSE_STATES["cannot"] in page and words.CLAUSE_STATES["unknown_field"] in page
    table = page.split("data-pbi-clauses", 1)[1].split("</table>", 1)[0]
    assert "status is not …" in table and "section_name is …" in table and "name is …" in table
    assert "Columns kept: Section, Accepted, Submitted, Due, Overdue" in page
    assert "Unknownperson" not in page and "Unknownperson" not in html
    assert "Fake Section Nobody Has" not in page and "Fake Section Nobody Has" not in html


def test_every_slicer_what_to_do_now_names_is_on_the_report():
    """Power BI's fields with a slicer, by the slicer's name in comparison.py (the one place for Power BI's
    names)."""
    slicers = {*comparison.POWERBI_ONLY_SLICERS, *comparison.POWERBI_SLICERS["partner-monitoring"].values()}
    assert set(comparison.POWERBI_FIELD_SLICERS.values()) <= slicers
    assert set(comparison.POWERBI_FIELD_SLICERS) <= set(powerbi_filters.FIELDS)


@pytest.mark.parametrize("how", ["stale", "start_failed", "locked", "other_job_later", "stopped"])
def test_a_failed_upload_says_why_first_and_never_that_there_is_nothing_to_do(checker, monkeypatch, how):
    """The error's own instruction comes first; an export a failed upload never read says "Not read" with what to
    do, never "Waiting to be read"."""
    if how == "start_failed":

        def broken(*args):
            raise OSError("no process")

        monkeypatch.setattr(background, "start_command", broken)
    else:
        monkeypatch.setattr(background, "start_command", lambda *args: 4242)
    pf.post(checker, *pf.every_export())
    upload = PowerBIUpload.objects.get()
    if how == "stale":
        PowerBIUpload.objects.filter(pk=upload.pk).update(status=PowerBIUpload.Status.READING)
        assert pc.close_stale() == 1
    elif how == "locked":
        monkeypatch.setattr(pc, "_lock", lambda: False)
        pc.run(upload.pk)
    elif how == "other_job_later":
        monkeypatch.setattr(pc, "heavy_job_running", lambda: True)
        pc.run(upload.pk)
    elif how == "stopped":

        def too_much(upload):
            raise MemoryError

        monkeypatch.setattr(pc, "_read_staged", too_much)
        pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.status == PowerBIUpload.Status.FAILED
    html = checker.get(reverse("admin:datamart_powerbi_upload", args=[upload.pk])).content.decode()
    todo = _todo(html)
    assert todo and todo[0] == upload.error.replace("'", "&#x27;") and words.TODO_NOTHING not in todo
    assert words.STATE_WORDS["staged"] not in html
    table = html.split("data-pbi-pages", 1)[1].split("</table>", 1)[0]
    if how == "other_job_later":  # read already: Compare again
        assert upload.error == words.OTHER_JOB_LATER and words.AGAIN_BUTTON in html
        assert table.count(f"<td>{words.STATE_WORDS['read']}") == 13
    else:  # the copies were emptied: upload the files again
        assert "upload the files again" in upload.error and words.AGAIN_BUTTON not in html
        assert table.count(words.NOT_READ_STATE) == 13 and table.count(words.NOT_READ_ACTION) == 13
        assert words.COVERS_EVERY_YEAR not in table  # nothing of their filters was read


def test_the_history_counts_the_exports_read_and_the_files_not_read(checker, started):
    pf.post(checker, ("cards.xlsx", pf.export("partner_cards")), ("sector.xlsx", pf.export("by_sector")))
    upload = PowerBIUpload.objects.get()
    PowerBIExport.objects.filter(upload=upload, kind="by_sector").update(content=b"PK broken")
    pc.run(upload.pk)
    upload.refresh_from_db()
    assert upload.status == PowerBIUpload.Status.DONE
    html = checker.get(INDEX).content.decode()
    table = html.split("data-pbi-history", 1)[1].split("</table>", 1)[0]
    headers = re.findall(r'<th scope="col">(.*?)</th>', table)
    cells = re.findall(r"<td>(.*?)</td>", table)
    found = dict(zip(headers[1:], cells, strict=False))
    assert found["Exports read"] == "1" and found["Files not read"] == "1"


def test_an_upload_with_nothing_recognised_keeps_nothing(checker, started):
    response = pf.post(checker, ("notes.txt", b"fake notes"))
    assert response.status_code == 200
    html = response.content.decode()
    assert words.NOT_EXCEL.format(name="notes.txt") in html and words.NOTHING_KEPT in html
    assert PowerBIUpload.objects.count() == 0 and started == []


def test_the_pages_never_read_the_stored_rows_or_copies(checker, started):
    upload = _done(
        checker,
        ("cards.xlsx", pf.export("partner_cards")),
        ("list.xlsx", pf.export("report_list", people=True)),
    )
    export = PowerBIExport.objects.filter(upload=upload).first()
    for url in (
        INDEX,
        reverse("admin:datamart_powerbi_upload", args=[upload.pk]),
        reverse("admin:datamart_powerbi_export", args=[upload.pk, export.pk]),
    ):
        with CaptureQueriesContext(connection) as queries:
            assert checker.get(url).status_code == 200
        selects = [  # the columns read (a filter on whether a copy is still there reads no bytes)
            q["sql"].split(" FROM ", 1)[0]
            for q in queries.captured_queries
            if "datamart_powerbiexport" in q["sql"] and q["sql"].startswith("SELECT")
        ]
        assert all('"content"' not in sql and '"rows"' not in sql for sql in selects), url
    assert {"content", "rows"} <= pc.exports_of(upload).query.deferred_loading[0]


# ---------------------------------------------------------------------------------------- kept apart from the AI
def test_nothing_of_these_tables_reaches_the_ai_or_a_developer_table():
    names = ("PowerBIUpload", "PowerBIExport", "PowerBIDifference", "powerbi_compare", "powerbi_read")
    for folder in ("assistant", "watch", "fmm", "knowledge", "graph", "insights"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert not any(name in text for name in names), path
    catalogue = (ROOT / "datamart" / "catalogue.py").read_text(encoding="utf-8")
    assert not any(name in catalogue for name in names)
    for model in (PowerBIUpload, PowerBIExport, PowerBIDifference):
        assert model not in admin.site._registry
        assert f"datamart.{model.__name__}" not in admin_site.DEVELOPER_ONLY_KEEP


def test_every_kind_has_a_power_bi_visual_and_every_page_is_in_power_bi_s_order():
    for kind in px.KINDS.values():
        page, visual = kind.visual
        assert page in comparison.POWERBI_PAGE_ORDER and visual
    assert {page for page, _visual in comparison.POWERBI_VISUALS.values()} <= set(
        comparison.POWERBI_PAGE_ORDER
    )
    assert [k.order for k in px.KINDS.values()] == list(range(13))
    pages = [comparison.POWERBI_PAGE_ORDER.index(k.visual[0]) for k in px.KINDS.values()]
    assert pages == sorted(pages)


def test_the_help_says_what_works_now_and_keeps_the_sheet_until_neurodb_compares():
    """The help's section opens with what Compare with Power BI does today; it says the manual check is no longer
    needed only once NeuroDB compares the exports with its pages (powerbi_compare.COMPARERS)."""
    text = (ROOT / "help" / "guide" / "checking-figures.md").read_text(encoding="utf-8")
    section = text.split("## Compare with Power BI\n", 1)[1].split("\n## ", 1)[0]
    opening = section.strip().split("\n\n", 1)[0]
    if not pc.COMPARERS:
        assert "no longer need" not in section
        assert "comparison sheet" in opening and "people's names" in opening.lower()
