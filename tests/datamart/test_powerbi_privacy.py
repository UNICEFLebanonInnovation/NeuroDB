"""People in Power BI's exports never reach anything (Compare with Power BI): the "UNICEF Focal Points" and "Focal
Point" columns are dropped by their position as the file is read, in the request, and never staged; a filter on
a field NeuroDB does not know keeps no value; an unknown file's first row (a list of people) is never shown or
kept; a staged copy holds no comment, document property or other sheet. Planted people never appear in the three
tables (read with raw SQL, rows decompressed, copies unzipped), in any log, page, the job's output, the session or
a cookie. Generated fake people only."""

from __future__ import annotations

import gzip
import io
import logging
import re
import zipfile

import pytest
from django.core.management import call_command
from django.db import connection
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.datamart import powerbi_compare as pc
from neurodb.datamart import powerbi_read as pr
from neurodb.datamart.models import PowerBIExport, PowerBIUpload
from neurodb.integrations import background
from tests.datamart import powerbi_factory as pf

pytestmark = pytest.mark.django_db
FORBIDDEN = re.compile(r"\bjane\b|\bdoe\b|fakeperson|unknownperson|\bzq\b|@example\.invalid", re.I)
TABLES = ("datamart_powerbiupload", "datamart_powerbiexport", "datamart_powerbidifference")


@pytest.fixture
def started(monkeypatch):
    found = []
    monkeypatch.setattr(background, "start_command", lambda *args: found.append(args) or 4242)
    return found


@pytest.fixture
def checker(client, roles):
    client.force_login(pf.user("checker"))
    return client


def _clean(text: str, where: str) -> None:
    found = FORBIDDEN.findall(text)
    assert not found, (where, found)


def _tables_hold_no_one(copies: bool = True) -> None:
    """Every column of the three tables, read with raw SQL: texts and JSON as stored, rows decompressed, copies
    unzipped (``copies``: a staged copy waiting for the job holds the sheet as Power BI wrote it, its texts not
    yet cleaned, for a few minutes at most; it is checked for its parts instead)."""
    for table in TABLES:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
            names = [c.name for c in cursor.description]
            for row in cursor.fetchall():
                for name, value in zip(names, row, strict=True):
                    if isinstance(value, memoryview | bytes):
                        data = bytes(value)
                        if name == "rows":
                            data = gzip.decompress(data)
                        elif name == "content":
                            with zipfile.ZipFile(io.BytesIO(data)) as package:
                                assert not any(
                                    n.startswith(("docProps/", "xl/comments")) for n in package.namelist()
                                )
                                assert "xl/worksheets/sheet2.xml" not in package.namelist()
                                if not copies:
                                    continue
                                data = b"".join(package.read(n) for n in package.namelist())
                        _clean(data.decode("utf-8", "replace"), f"{table}.{name}")
                    else:
                        _clean(str(value), f"{table}.{name}")


PEOPLE_FILTERS = (
    "country_name is Lebanon",
    "name is Zq Unknownperson",
    "focal_point is jane.doe@example.invalid",
)


def test_people_in_the_files_never_reach_a_table_a_log_a_page_or_the_session(checker, started, caplog):
    caplog.set_level(logging.DEBUG)
    User.objects.create_user(
        username="known", first_name="Zq", last_name="Fakeperson", password="pw-1234567-x"
    )
    unknown_sheet = pf.powerbi_xlsx_bytes([["Zq Fakeperson", "zq@example.invalid"], ["Jane Doe", "x"]])
    staged = pf.export("by_sector", writer="excel", core_creator="Zq Fakeperson", comment="Zq Fakeperson")
    two_sheets = pf.export("follow_up", second_sheet="Zq Fakeperson wrote this")
    partner_named = pf.export(
        "program_performance",
        [["Section Alpha", "Fake output", "No", "Fake indicator", 1.0, 2.0, 2.0, "ZQ FAKEPERSON"]],
    )
    files = [
        ("report list.xlsx", pf.export("report_list", people=True, filters=PEOPLE_FILTERS)),
        ("fund monitoring.xlsx", pf.export("fund_monitoring", people=True)),
        (
            "exports.zip",
            pf.archive(
                ("list.xlsx", pf.export("report_list", people=True)),
                ("funds.xlsx", pf.export("fund_monitoring", people=True)),
            ),
        ),
        ("people.xlsx", unknown_sheet),
        ("sector.xlsx", staged),
        ("follow up.xlsx", two_sheets),
        ("performance.xlsx", partner_named),
    ]
    response = pf.post(checker, *files)
    assert response.status_code == 302
    upload = PowerBIUpload.objects.get()
    exports = {e.file_name: e for e in PowerBIExport.objects.filter(upload=upload)}
    # files naming people are read in the request, never staged; so is a workbook with a second sheet
    for name in ("report list.xlsx", "fund monitoring.xlsx", "list.xlsx", "funds.xlsx", "follow up.xlsx"):
        assert exports[name].state == "read" and exports[name].content is None and exports[name].rows, name
    assert exports["sector.xlsx"].state == "staged" and exports["performance.xlsx"].state == "staged"
    _tables_hold_no_one(copies=False)  # a staged copy holds no comment, document property or other sheet
    assert exports["report list.xlsx"].clauses[1:] == [
        {"words": "name is …", "state": "unknown_field"},
        {"words": "focal_point is …", "state": "unknown_field"},
    ]
    assert any("Columns it knows: none" in line for line in upload.refused)
    out = io.StringIO()
    call_command("compare_powerbi", "--upload-id", str(upload.pk), stdout=out)
    _clean(out.getvalue(), "the job's output")
    assert not PowerBIExport.objects.exclude(content=None).exists()  # the job emptied every copy
    _tables_hold_no_one()
    performance = PowerBIExport.objects.get(upload=upload, file_name="performance.xlsx")
    partners = [row[-2] for row in pr.rows_of(performance.rows)]
    assert partners == ["[name withheld]"]  # a known name, whatever its case
    pages = [reverse("admin:datamart_powerbi"), reverse("admin:datamart_powerbi_upload", args=[upload.pk])]
    pages += [reverse("admin:datamart_powerbi_export", args=[upload.pk, e.pk]) for e in exports.values()]
    for url in pages:
        page = checker.get(url)
        assert page.status_code == 200
        _clean(page.content.decode(), url)
    _clean(caplog.text, "logs")
    _clean(repr(dict(checker.session.items())), "session")
    _clean(repr({k: v.value for k, v in checker.cookies.items()}), "cookies")


def test_an_error_while_reading_keeps_only_its_class_name(checker, started, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def broken(column, value, date1904=False):
        raise ValueError("Jane Doe")

    monkeypatch.setattr(pr, "coerce", broken)
    response = pf.post(
        checker,
        ("list.xlsx", pf.export("report_list", people=True)),
        ("cards.xlsx", pf.export("partner_cards")),
    )
    assert response.status_code == 302
    upload = PowerBIUpload.objects.get()
    assert upload.refused == ["NeuroDB could not read list.xlsx (ValueError)."]
    _tables_hold_no_one()
    _clean(caplog.text, "logs")
    pc.run(upload.pk)  # the staged cards fail in the job, the same way
    export = PowerBIExport.objects.get(upload=upload)
    assert export.state == "failed" and export.error == "NeuroDB could not read cards.xlsx (ValueError)."
    _tables_hold_no_one()
    _clean(caplog.text, "logs")


def test_the_copies_are_emptied_whatever_happens(checker, started, monkeypatch):
    upload = PowerBIUpload.objects.create(status=PowerBIUpload.Status.QUEUED)
    PowerBIExport.objects.create(
        upload=upload, order=0, kind="by_sector", file_name="x.xlsx", state="staged", content=b"PK broken"
    )
    pc.run(upload.pk)  # a failed read
    assert PowerBIExport.objects.get(upload=upload).content is None
    upload = PowerBIUpload.objects.create(status=PowerBIUpload.Status.READING)
    PowerBIExport.objects.create(
        upload=upload, order=0, kind="by_sector", file_name="x.xlsx", state="staged", content=b"x"
    )
    pc.close_stale()
    assert PowerBIExport.objects.get(upload=upload).content is None


def test_codes_and_years_survive_the_cleaner_and_a_known_name_does_not():
    User.objects.create_user(
        username="known", first_name="Zq", last_name="Fakeperson", password="pw-1234567-x"
    )
    from neurodb.watch import people

    names = people.known_names(refresh=True)
    memo: dict[str, str] = {}
    for text in ("2025-2026", "FAKE/PCA2026001/PD2026001-2", "LB9_9_9", "FAKEORG PARTNER 1"):
        assert pr.clean(text, names, memo) == text
    assert pr.clean("ZQ FAKEPERSON", names, memo) == "[name withheld]"
    assert pr.file_name("folder/Zq Fakeperson export.xlsx", names) == "[name withheld] export.xlsx"
