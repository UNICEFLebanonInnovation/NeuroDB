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
    sheet_named = pf.export("partner_cards", sheet_name="Zq Fakeperson jane.doe@example.invalid")
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
        ("cards.xlsx", sheet_named),  # a sheet named after a person: its name is never kept
    ]
    response = pf.post(checker, *files)
    assert response.status_code == 302
    upload = PowerBIUpload.objects.get()
    exports = {e.file_name: e for e in PowerBIExport.objects.filter(upload=upload)}
    # files naming people are read in the request, never staged; so is a workbook with a second sheet
    for name in ("report list.xlsx", "fund monitoring.xlsx", "list.xlsx", "funds.xlsx", "follow up.xlsx"):
        assert exports[name].state == "read" and exports[name].content is None and exports[name].rows, name
    for name in ("sector.xlsx", "performance.xlsx", "cards.xlsx"):
        assert exports[name].state == "staged", name
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


@pytest.mark.parametrize("caption", ["", " "])  # no caption, or Power BI's display name of one space
@pytest.mark.parametrize("at", ["last", "middle"])
@pytest.mark.parametrize("kind", ["by_sector", "by_month"])  # a table's layout and a chart's
def test_a_column_without_a_caption_is_dropped_as_the_file_is_read(
    checker, started, caplog, caption, at, kind
):
    """A column with no caption holds values all the same (a list of people): it is counted as a column NeuroDB
    does not know, so the file is read in the request with that column dropped by its position, never staged."""
    caplog.set_level(logging.DEBUG)
    grid = pf.table(kind)
    header = next(i for i, row in enumerate(grid) if row and not str(row[0]).startswith("Applied filters:"))
    where = len(grid[header]) if at == "last" else 1
    grid[header].insert(where, caption)
    for row in grid[header + 1 :]:
        if len(row) > 1:  # a data row (not the empty row or the filters line)
            row.insert(where, pf.PERSON)
    data = pf.powerbi_xlsx_bytes(grid, "Sheet1" if kind == "by_month" else "Export")
    assert pf.post(checker, ("export.xlsx", data)).status_code == 302
    export = PowerBIExport.objects.get()
    assert export.kind == kind and export.state == "read" and export.content is None
    assert export.unknown_columns == 1 and export.rows_read == 3
    _tables_hold_no_one()
    page = checker.get(reverse("admin:datamart_powerbi_export", args=[export.upload_id, export.pk]))
    assert "Not compared: 1 other column NeuroDB does not know." in page.content.decode()
    _clean(page.content.decode(), "the export's page")
    _clean(caplog.text, "logs")


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


def test_people_in_neurodbs_own_records_never_reach_the_comparison(checker, started, caplog, monkeypatch):
    """NeuroDB's side is read through the page functions and kept through ``powerbi_page.shown``: a PD's focal
    points, who submitted a report, its narratives and a record's whole data never reach a line, a page, the
    Excel file or a log; a known person's name in a NeuroDB text is withheld as in the files."""
    import datetime

    from neurodb.datamart import models as dm
    from neurodb.datamart import reached

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        reached,
        "history_from",
        lambda today=None: reached.HistoryFrom(datetime.date(2026, 1, 1), "read_from"),
    )
    User.objects.create_user(
        username="known", first_name="Zq", last_name="Fakeperson", password="pw-1234567-x"
    )
    pd, _ended = pf.world()
    pd.unicef_focal_points = ["Jane Doe", "jane.doe@example.invalid"]
    pd.save(update_fields=["unicef_focal_points"])
    dm.ReportedIndicator.objects.update(
        submitted_by="Zq Fakeperson",
        narrative="Jane Doe wrote this",
        narrative_assessment="zq@example.invalid",
        data={"focal_point": "Jane Doe", "submitted_by_email": "jane.doe@example.invalid"},
    )
    dm.ReportedIndicator.objects.filter(report_number="QPR2").update(
        indicator="Fake indicator of Zq Fakeperson"
    )
    rows = [
        pf.row(
            "report_list",
            pd=pf.WORLD_PD,
            report="QPR1",
            report_status="Submitted",
            period_end=datetime.date(2026, 3, 31),
        )
    ]
    upload = pf.compared(
        checker,
        ("list.xlsx", pf.export("report_list", rows, people=True)),
        ("follow.xlsx", pf.export("follow_up", [pf.row("follow_up", pd="FAKE/PD/1", status="Overdue", due_date=datetime.date(2026, 5, 1))])),
        ("locations.xlsx", pf.export("locations", [pf.row("locations", pd=pf.WORLD_PD, report="QPR9", indicator_title="x", p_code="LB9_9_1", reached=1)])),
        ("performance.xlsx", pf.export("program_performance", [pf.row("program_performance", indicator_title="Fake", partner="FAKEORG PARTNER 1", target=1, reached=1)])),
    )  # fmt: skip
    assert upload.status == PowerBIUpload.Status.DONE
    from neurodb.datamart.models import PowerBIDifference

    assert PowerBIDifference.objects.filter(kind="only_neurodb").exists()  # NeuroDB's own rows are listed
    _tables_hold_no_one()
    pages = [reverse("admin:datamart_powerbi_upload", args=[upload.pk])]
    pages += [
        reverse("admin:datamart_powerbi_export", args=[upload.pk, e.pk])
        for e in PowerBIExport.objects.filter(upload=upload)
    ]
    for url in pages:
        page = checker.get(url)
        assert page.status_code == 200
        _clean(page.content.decode(), url)
    response = checker.get(reverse("admin:datamart_powerbi_xlsx", args=[upload.pk]))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as package:
        for name in package.namelist():
            _clean(package.read(name).decode("utf-8", "replace"), f"the Excel file's {name}")
    _clean(caplog.text, "logs")
