"""Reading Power BI's exports (Compare with Power BI: datamart.powerbi_xlsx, powerbi_exports, powerbi_read): every
kind recognised by its columns, never by its file name; both of Power BI's layouts and both string encodings;
the values read by their column's type; codes never cleaned; each refusal in its own sentence; the user's zip
shape; staged copies holding one sheet only. Generated fake exports only (tests/datamart/powerbi_factory.py)."""

from __future__ import annotations

import datetime
import io
import json
import zipfile

import pytest

from neurodb.datamart import powerbi_exports as px
from neurodb.datamart import powerbi_read as pr
from neurodb.datamart import powerbi_words as words
from neurodb.datamart import powerbi_xlsx
from tests.datamart import powerbi_factory as pf

pytestmark = pytest.mark.django_db
NAMES: frozenset[str] = frozenset()


class Upload(io.BytesIO):
    """An uploaded file, as the form hands it over."""

    def __init__(self, name: str, data: bytes) -> None:
        super().__init__(data)
        self.name = name


def _prepare(*files: tuple[str, bytes]) -> pr.Upload:
    return pr.prepare([Upload(name, data) for name, data in files], NAMES)


def _read(data: bytes) -> tuple[px.Match, pr.Read]:
    peek = powerbi_xlsx.peek(data)
    match = px.recognise(peek.header)
    return match, pr.read(data, match, NAMES, (), peek.sheet)


def _rows(found: pr.Read) -> list[dict]:
    return [dict(zip(found.columns, row, strict=True)) for row in pr.rows_of(found.rows)]


# ------------------------------------------------------------------------------------------- recognising
@pytest.mark.parametrize("kind", list(px.KINDS))
@pytest.mark.parametrize("writer", ["powerbi", "excel"])
def test_every_kind_is_recognised_by_its_columns_under_any_file_name(kind, writer):
    found = _prepare(("misleading name (1) .xlsx", pf.export(kind, writer=writer)))
    assert found.refused == [] and [e.kind.key for e in found.exports] == [kind]
    assert found.exports[0].file_name == "misleading name (1) .xlsx"


@pytest.mark.parametrize("kind", list(px.KINDS))
def test_captions_match_whatever_their_spacing_and_case_and_an_optional_column_may_be_missing(kind):
    columns = pf.columns(kind)
    captions = [
        f" {c.caption.upper()}  " if i % 2 else c.caption.lower() + " " for i, c in enumerate(columns)
    ]
    assert px.recognise(captions).kind.key == kind
    optional = [c for c in columns if not c.needed]
    if optional:
        assert px.recognise([c.caption for c in columns if c is not optional[0]]).kind.key == kind
    extra = px.recognise([*[c.caption for c in columns], "Fake column nobody knows"])
    assert extra.kind.key == kind and extra.unknown == 1 and not extra.whole


def test_the_partner_cards_sector_table_and_month_chart_are_told_apart_by_their_columns():
    cards = ["Accepted", "Submitted", "Sent back", "Due", "Overdue"]
    assert px.recognise(cards).kind.key == "partner_cards"
    assert px.recognise(["Section", "Accepted", "Submitted", "Due", "Overdue"]).kind.key == "by_sector"
    month = ["Reporting (mmm yyyy)", "Accepted", "Submitted", "Overdue", "Due", "Not Yet Due"]
    assert px.recognise(month).kind.key == "by_month"
    assert px.recognise(["Accepted", "Submitted", "Due", "Overdue"]).kind.key == "partner_cards"
    # Power BI writes the month chart's status columns only for the statuses in its data: a past year has no
    # report still to come (no Not Yet Due), and any status can be missing
    for statuses in (cards, ["Accepted", "Submitted", "Overdue", "Due"], ["Accepted", "Overdue"]):
        match = px.recognise(["Reporting (mmm yyyy)", *statuses])
        assert match.kind.key == "by_month" and match.whole, statuses
    assert px.recognise(["Reporting (mmm yyyy)", "Accepted"]).kind is None  # one status: not a status chart
    # a file with a grouping column is never taken for the cards
    for grouping in ("Section", "Reporting (mmm yyyy)", "PD #"):
        match = px.recognise([grouping, "Accepted", "Submitted", "Due", "Overdue"])
        assert match.kind is None or match.kind.key != "partner_cards", grouping


def test_a_month_chart_without_not_yet_due_is_read_as_the_month_chart():
    data = pf.export(
        "by_month",
        [[f"{m} 2025", 1, 2, 3, 4] for m in ("Jan", "Feb", "Mar")],
        drop=("not_yet_due", "sent_back"),
    )
    found = _prepare(("# of Reports by Type over Reporting Date.xlsx", data))
    assert found.refused == [] and [e.kind.key for e in found.exports] == ["by_month"]
    export = found.exports[0]
    assert export.not_compared == [] and export.unknown == 0 and export.state == "staged"
    match, read = _read(data)
    assert match.kind.key == "by_month" and read.rows_read == 3
    assert _rows(read)[0] == {"month": "Jan 2025", "accepted": 1, "submitted": 2, "overdue": 3, "due": 4}


def test_a_file_two_kinds_fit_equally_is_refused_naming_both(monkeypatch):
    twin = px.Kind("twin", px.EXPORT, px.KINDS["by_sector"].columns)
    monkeypatch.setattr(px, "_KINDS", (*px._KINDS, twin))
    match = px.recognise(["Section", "Accepted", "Submitted", "Due", "Overdue"])
    assert match.kind is None and {k.key for k in match.tied} == {"by_sector", "twin"}
    monkeypatch.setitem(px.VISUAL_OF, "twin", "by_month")
    monkeypatch.setitem(px.KINDS, "twin", twin)
    found = _prepare(("sector.xlsx", pf.export("by_sector")))
    assert found.exports == [] and len(found.refused) == 1
    assert "could be 'Reporting Progress / Reporting Progress by Sector' or" in found.refused[0]


def test_a_file_of_no_kind_names_only_the_captions_neurodb_knows():
    data = pf.export("follow_up", captions=["PD #", "Fake A", "Fake B", "Section"], data=[["x", 1, 2, "y"]])
    found = _prepare(("other.xlsx", data))
    assert found.refused == [
        "NeuroDB does not recognise other.xlsx. Columns it knows: PD #, Section, and 2 other columns NeuroDB "
        "does not know. Upload exports of the PRP Power BI report."
    ]


# ---------------------------------------------------------------------------------------------- the reader
@pytest.mark.parametrize("writer", ["powerbi", "excel", "inline"])
def test_both_layouts_and_both_string_encodings_give_the_same_rows(writer):
    data = [
        [
            "FAKE/PCA2026001/PD2026001-2",
            "FAKEORG PARTNER 1",
            "Section Alpha",
            "Overdue",
            datetime.date(2026, 3, 31),
        ]
    ]
    match, found = _read(pf.export("follow_up", data, writer=writer))
    assert match.kind.key == "follow_up" and found.rows_read == 1
    assert _rows(found) == [
        {
            "pd": "FAKE/PCA2026001/PD2026001-2",
            "partner": "FAKEORG PARTNER 1",
            "section": "Section Alpha",
            "status": "Overdue",
            "due_date": "2026-03-31",
        }
    ]
    assert [(c.words, c.state) for c in found.filter.clauses] == [
        ("country_name is Lebanon", "applied"),
        ("status is active", "applied"),
    ]


@pytest.mark.parametrize("writer", ["powerbi", "excel"])
def test_a_chart_s_layout_has_its_filters_first(writer):
    data = [["Jan 2026", 1, 2, 3, 4, 5, 0], ["Feb 2026", 6, 7, 8, 9, 10, None]]
    match, found = _read(pf.export("by_month", data, writer=writer, filters=("reporting_year is 2026",)))
    assert match.kind.key == "by_month" and found.rows_read == 2
    assert _rows(found)[1] == {
        "month": "Feb 2026",
        "accepted": 6,
        "submitted": 7,
        "overdue": 8,
        "due": 9,
        "not_yet_due": 10,
        "sent_back": None,
    }
    assert found.filter.years == [2026]
    if writer == "excel":
        sheet = zipfile.ZipFile(io.BytesIO(pf.export("by_month", writer=writer))).read(
            "xl/worksheets/sheet1.xml"
        )
        assert b'<dimension ref="A1"/>' in sheet


def test_the_card_s_one_value_is_its_one_row():
    match, found = _read(pf.export("dq_card", [[113]]))
    assert match.kind.key == "dq_card" and _rows(found) == [{"indicators": 113}]


def test_ragged_rows_are_padded_and_the_filters_line_is_never_a_data_row():
    grid = [
        ["PD #", "Partner", "Section", "Status", "Report Due Date"],
        ["FAKE/PCA1/PD1"],
        [],
        ["Applied filters:\nstatus is active"],
    ]
    data = pf.powerbi_xlsx_bytes(grid)
    _match, found = _read(data)
    assert _rows(found) == [
        {"pd": "FAKE/PCA1/PD1", "partner": None, "section": None, "status": None, "due_date": None}
    ]
    assert found.filter.pd_statuses == ["active"]


def test_cells_with_references_skip_columns_and_a_column_neurodb_does_not_keep_is_never_looked_up():
    data = pf.export("by_sector", writer="excel", extra_columns=["Fake extra"])
    peek = powerbi_xlsx.peek(data)
    match = px.recognise(peek.header)
    assert match.not_compared == [] and match.unknown == 1
    seen = powerbi_xlsx.rows(data, set(match.positions), peek.sheet)
    first = next(iter(seen))
    assert set(first) <= set(match.positions) and len(match.positions) == 5


def test_peek_reads_the_first_rows_only(monkeypatch):
    """The header, then whether the first data rows' cells hold a value (never their values): the rest of the
    sheet is never read in the request."""
    n = powerbi_xlsx.PEEK_DATA_ROWS + 2  # the first row peek never reaches
    data = pf.patch_sheet(
        pf.export("follow_up", pf.rows("follow_up", n + 3)),
        lambda xml: xml.replace(
            f'</x:row><x:row><x:c t="inlineStr"><x:is><x:t>FAKE/PCA2026{n:03}', "</x:row><x:row><broken"
        ),
    )
    assert b"<broken" in zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    peek = powerbi_xlsx.peek(data)
    assert peek.header[0] == "PD #" and peek.filled == frozenset(range(5))
    with pytest.raises(Exception):  # noqa: B017 - reading the whole sheet meets the broken row
        list(powerbi_xlsx.rows(data, {0}))


def test_a_1904_workbook_s_dates_are_read_in_its_system():
    data = [["FAKE/PCA1/PD1", "FAKEORG PARTNER 1", "Section Alpha", "Due", datetime.date(2026, 5, 4)]]
    _match, found = _read(pf.export("follow_up", data, date1904=True))
    assert _rows(found)[0]["due_date"] == "2026-05-04"


def test_error_cells_are_counted_and_read_as_empty():
    grid = pf.table("partner_cards", [[1, 2, 3, 4, 5]])
    data = pf.patch_sheet(
        pf.powerbi_xlsx_bytes(grid),
        lambda xml: xml.replace('<x:c s="8"><x:v>2</x:v></x:c>', '<x:c t="e"><x:v>#N/A</x:v></x:c>'),
    )
    _match, found = _read(data)
    assert _rows(found)[0]["submitted"] is None and found.unreadable == 1
    assert words.UNREADABLE_VALUE in found.notes


def test_a_part_with_a_doctype_is_refused():
    data = pf.patch_sheet(
        pf.export("by_sector"),
        lambda xml: xml.replace(
            '<?xml version="1.0" encoding="utf-8"?>', '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>'
        ),
    )
    with pytest.raises(powerbi_xlsx.Unreadable) as raised:
        powerbi_xlsx.peek(data)
    assert raised.value.reason == "doctype"
    assert _prepare(("x.xlsx", data)).refused == [words.NOT_EXCEL.format(name="x.xlsx")]


# ------------------------------------------------------------------------------------------------ the types
@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        (px.NUMBER, "1,234", 1234.0),
        (px.NUMBER, 12.5, 12.5),
        (px.COUNT, 3.0, 3),
        (px.FRACTION, "12 %", 0.12),
        (px.MONEY, " 1 000.5 ", 1000.5),
        (px.DATE, 46022.0, datetime.date(2025, 12, 31)),
        (px.DATE, "3/31/26", datetime.date(2026, 3, 31)),
        (px.DATE, "03/31/2026", datetime.date(2026, 3, 31)),
        (px.DATE, "2026-03-31", datetime.date(2026, 3, 31)),
        (px.STAMP, 46319.25, datetime.datetime(2026, 10, 24, 6, 0)),
        (px.MONTH, "jan 2026", "Jan 2026"),
        (px.MONTH, 46023.0, "Jan 2026"),
        (px.TEXT, 400012345.0, "400012345"),
        (px.YES_NO, True, "Yes"),
        (px.NUMBER, None, None),
        (px.TEXT, "", None),
    ],
)
def test_values_are_read_by_their_column_s_type(kind, raw, expected):
    value, ok = pr.coerce(px.Column("Fake", "fake", kind), raw)
    assert ok and value == expected


def test_a_value_neurodb_cannot_read_is_counted_and_its_text_never_kept():
    assert pr.coerce(px.Column("Fake", "fake", px.NUMBER), "about twelve") == (None, False)
    assert pr.coerce(px.Column("Fake", "fake", px.DATE), "someday") == (None, False)
    data = [["FAKE/PCA1/PD1", "FAKEORG PARTNER 1", "Section Alpha", "Due", "someday"]]
    _match, found = _read(pf.export("follow_up", data))
    assert _rows(found)[0]["due_date"] is None and found.unreadable == 1
    assert b"someday" not in json.dumps(_rows(found)).encode()


def test_codes_are_kept_as_read_and_never_cleaned():
    """The people cleaner would take a phone-like code for a phone number: two different codes would then read
    the same. Codes are never cleaned. Funded Programs' Fund Reservation is the amount reserved for the grant (a
    number, as Power BI writes it)."""
    order = [c.name for c in pf.columns("funded_programmes")]
    data = []
    for pd in ("FAKE/PCA1/+961 3 123 457", "FAKE/PCA2/+961 3 123 458"):
        row = dict.fromkeys(order)
        row.update({"pd": pd, "grant": "0400012345", "fund_reservation": 5000.1, "donor": "+961 3 123 459"})
        data.append([row[name] for name in order])
    _match, found = _read(pf.export("funded_programmes", data))
    rows = _rows(found)
    assert [r["pd"] for r in rows] == ["FAKE/PCA1/+961 3 123 457", "FAKE/PCA2/+961 3 123 458"]
    assert rows[0]["fund_reservation"] == 5000.1 and rows[0]["grant"] == "0400012345"
    assert rows[0]["donor"] == "[phone withheld]"  # a free text is cleaned


def test_a_title_power_bi_cut_at_255_characters_is_marked():
    long = "Fake indicator " + "x" * 240
    data = [
        [None, None, "No", long, 1.0, 2.0, 0.5, "FAKEORG PARTNER 1"],
        [None, None, "No", "Short", 1.0, 2.0, 0.5, "P"],
    ]
    _match, found = _read(pf.export("program_performance", data))
    assert [r["indicator_title__cut"] for r in _rows(found)] == [True, False] and found.cut == 1


# -------------------------------------------------------------------------------------------- the refusals
def test_each_refusal_is_its_own_sentence():
    found = _prepare(
        ("protected.xlsx", powerbi_xlsx.OLE + b"\x00" * 100),
        ("not a zip.xlsx", b"plain text"),
        ("nested.zip", pf.archive(("inner.zip", b"PK\x05\x06" + b"\x00" * 18), mac=False, junk=False)),
        ("junk only.zip", pf.archive(mac=False)),
    )
    assert found.exports == []
    assert found.refused == [
        words.PROTECTED.format(name="protected.xlsx"),
        words.NOT_EXCEL.format(name="not a zip.xlsx"),
        words.NESTED_ZIP.format(name="inner.zip"),
        words.NO_EXPORT_IN_ZIP,
        words.NOT_EXCEL.format(name="notes.txt"),
        words.NO_EXPORT_IN_ZIP,
    ]
    assert found.hidden == 1  # the .DS_Store


def test_a_zip_entry_declared_too_big_is_never_opened(monkeypatch):
    data = pf.archive(("big.xlsx", pf.export("by_sector")), junk=False, mac=False)
    monkeypatch.setattr(pr, "MAX_FILE_BYTES", 100)
    opened = []
    real = zipfile.ZipFile.open

    def recorded(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return real(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", recorded)
    found = _prepare(("exports.zip", data))
    assert found.refused == [words.TOO_BIG.format(name="big.xlsx")] and "big.xlsx" not in opened


def test_too_many_parts_too_much_data_and_too_many_exports_are_refused(monkeypatch):
    monkeypatch.setattr(powerbi_xlsx, "MAX_PARTS", 3)
    assert _prepare(("parts.xlsx", pf.export("by_sector"))).refused == [
        words.NOT_EXCEL.format(name="parts.xlsx")
    ]
    monkeypatch.setattr(powerbi_xlsx, "MAX_PARTS", 500)
    monkeypatch.setattr(pr, "MAX_UNPACKED", 3_000)
    found = _prepare(
        ("a.xlsx", pf.export("by_sector")),
        ("b.xlsx", pf.export("follow_up")),
        ("c.xlsx", pf.export("dq_card")),
    )
    assert words.TOO_MUCH in found.refused and len(found.exports) < 3
    monkeypatch.setattr(pr, "MAX_UNPACKED", 400 * pr.MB)
    monkeypatch.setattr(pr, "MAX_EXPORTS", 2)
    found = _prepare(*[(f"{n}.xlsx", pf.export("by_sector")) for n in range(4)])
    assert len(found.exports) == 2 and found.refused == [words.TOO_MANY]


def test_a_large_file_with_a_column_neurodb_does_not_compare_is_refused(monkeypatch):
    monkeypatch.setattr(pr, "MAX_READ_NOW", 100)
    found = _prepare(
        ("report list.xlsx", pf.export("report_list", people=True, extra_columns=["Fake extra"]))
    )
    assert found.exports == []
    assert found.refused == [
        "report list.xlsx has columns NeuroDB does not compare (UNICEF Focal Points, 1 other column NeuroDB does "
        "not know) and is too large to read at once: remove those columns in Power BI and export again."
    ]


def test_a_large_file_with_a_second_sheet_is_refused(monkeypatch):
    monkeypatch.setattr(pr, "MAX_READ_NOW", 100)
    found = _prepare(("two sheets.xlsx", pf.export("by_sector", second_sheet="Fake other sheet")))
    assert found.exports == [] and found.refused == [words.TOO_LARGE_SHEETS.format(name="two sheets.xlsx")]


def test_an_old_excel_file_is_not_said_to_be_protected():
    found = _prepare(("old.xls", powerbi_xlsx.OLE + b"\x00" * 100))
    assert found.refused == [words.NOT_EXCEL.format(name="old.xls")]


def test_the_staged_copies_are_capped_together(monkeypatch):
    monkeypatch.setattr(pr, "MAX_STAGED", 2_000)
    found = _prepare(*[(f"{n}.xlsx", pf.export("by_sector")) for n in range(3)])
    assert words.STAGED_TOO_BIG in found.refused and len(found.exports) < 3


def test_an_export_power_bi_wrote_empty_is_said_so():
    data = pf.powerbi_xlsx_bytes([["Applied filters:\nstatus is active"]])
    assert _prepare(("empty.xlsx", data)).refused == [words.EMPTY_VISUAL.format(name="empty.xlsx")]


def test_a_header_with_no_row_is_an_empty_export():
    match, found = _read(pf.export("follow_up", []))
    assert match.kind.key == "follow_up" and found.rows_read == 0 and pr.rows_of(found.rows) is not None


# --------------------------------------------------------------------------------------- the user's zip shape
def test_the_user_s_zip_shape_gives_every_export_and_counts_the_hidden_files(monkeypatch):
    opened = []
    real = pr._export
    monkeypatch.setattr(
        pr, "_export", lambda found, data, name, *a: opened.append(name) or real(found, data, name, *a)
    )
    data = pf.archive(*pf.every_export())
    found = _prepare(("exports.zip", data))
    assert sorted(e.kind.key for e in found.exports) == sorted(px.KINDS)
    assert found.hidden == 14  # 13 AppleDouble files and a .DS_Store, never opened
    assert all(not name.startswith(".") for name in opened) and len(opened) == 13
    assert found.refused == [words.NOT_EXCEL.format(name="notes.txt")]
    assert (
        words.plural(found.hidden, words.HIDDEN_ONE, words.HIDDEN)
        == "Skipped 14 hidden files a Mac adds to a zip."
    )


def test_the_exports_can_be_uploaded_without_the_zip_too():
    found = _prepare(*pf.every_export(writer="excel"))
    assert sorted(e.kind.key for e in found.exports) == sorted(px.KINDS) and found.refused == []


# ------------------------------------------------------------------------------------------------- staging
def test_a_staged_copy_holds_one_sheet_and_its_strings_only():
    data = pf.export("by_month", writer="excel", core_creator="Fake Author", comment="Fake note")
    found = _prepare(("chart.xlsx", data))
    export = found.exports[0]
    assert export.state == "staged" and export.read is None
    parts = sorted(zipfile.ZipFile(io.BytesIO(export.content)).namelist())
    assert parts == [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/_rels/workbook.xml.rels",
        "xl/sharedStrings.xml",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    ]
    match, read = _read(export.content)
    assert match.kind.key == "by_month" and read.rows_read == 3


def test_a_workbook_with_a_second_sheet_is_never_staged():
    found = _prepare(("two sheets.xlsx", pf.export("by_sector", second_sheet="Fake other sheet")))
    export = found.exports[0]
    assert export.state == "read" and export.content is None and export.read.rows_read == 3


def test_a_round_count_says_power_bi_may_have_cut_the_export():
    _match, found = _read(pf.export("follow_up", pf.rows("follow_up", 1_000)))
    assert found.notes == [words.ROUND_COUNT.format(n=1_000)]
    _match, found = _read(pf.export("follow_up", pf.rows("follow_up", 999)))
    assert found.notes == []
