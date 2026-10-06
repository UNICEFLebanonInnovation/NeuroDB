"""A typed Excel workbook written straight to its XML: numbers stay numbers, dates stay dates, a text is
always a text (never a formula), and a large export stays fast.

openpyxl's write-only mode spends about 35 µs on each cell; a year of field monitoring visits with their
rule results is some 2.5 million cells, more than a minute. This writer builds each row's XML as one string
(about 1 µs a cell) and streams the sheets into the ZIP file. It writes only what the exports need: inline
texts, numbers, true/false, dates (``yyyy-mm-dd``) and times (``yyyy-mm-dd hh:mm``), a bold frozen header
row and the column widths of the header. Excel, LibreOffice and openpyxl open it.
"""

from __future__ import annotations

import datetime
import decimal
import io
import math
import re
import zipfile
from collections.abc import Iterable, Sequence
from typing import Any
from xml.sax.saxutils import escape

from django.utils import timezone

EPOCH = datetime.datetime(1899, 12, 30)  # Excel's day 0 (with its 1900 leap year)
# Characters XML 1.0 refuses (Excel refuses them too); tab, line feed and carriage return are kept
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
CELL_CHARS = 32_767  # Excel's limit for one cell
DATE_STYLE, TIME_STYLE, HEADER_STYLE = 1, 2, 3

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "{sheets}</Types>"
)
SHEET_TYPE = (
    '<Override PartName="/xl/worksheets/sheet{n}.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)
WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<sheets>{sheets}</sheets></workbook>"
)
WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "{sheets}"
    '<Relationship Id="rIdStyles" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/></Relationships>'
)
STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
    '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)
SHEET_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<sheetViews><sheetView workbookViewId="0">'
    '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
    '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
    "<cols>{cols}</cols><sheetData>"
)
SHEET_TAIL = "</sheetData></worksheet>"


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _text(value: str) -> str:
    value = _ILLEGAL.sub("", value)[:CELL_CHARS]
    space = ' xml:space="preserve"' if value != value.strip() or "\n" in value else ""
    return f"<is><t{space}>{escape(value)}</t></is>"


def _serial(value: datetime.date | datetime.datetime) -> float:
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value).replace(tzinfo=None)
        delta = value - EPOCH
        return delta.days + delta.seconds / 86_400
    return float((value - EPOCH.date()).days)


def cell(ref: str, value: Any, style: int = 0) -> str:
    """One cell's XML ("" for an empty cell)."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, int | float | decimal.Decimal):
        number = float(value)
        if not math.isfinite(number):
            return ""
        text = repr(int(number)) if number.is_integer() and abs(number) < 1e15 else repr(number)
        return f'<c r="{ref}"><v>{text}</v></c>'
    if isinstance(value, datetime.datetime):
        return f'<c r="{ref}" s="{TIME_STYLE}"><v>{_serial(value)!r}</v></c>'
    if isinstance(value, datetime.date):
        return f'<c r="{ref}" s="{DATE_STYLE}"><v>{_serial(value)!r}</v></c>'
    if isinstance(value, list | tuple):
        value = "; ".join(str(v) for v in value)
    style_attr = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{style_attr}>{_text(str(value))}</c>'


def _sheet_name(title: str, used: set[str]) -> str:
    name = re.sub(r"[\[\]:*?/\\]", " ", title)[:31] or "Sheet"
    base, n = name, 2
    while name.casefold() in used:
        name = f"{base[:28]} {n}"
        n += 1
    used.add(name.casefold())
    return name


def workbook(sheets: Sequence[tuple[str, Sequence[str], Iterable[dict[str, Any]]]]) -> bytes:
    """The ``.xlsx`` file of ``sheets`` ((title, columns, rows of dicts)): a bold, frozen header row
    with the column names, then one row per dict, read as it is written."""
    buffer = io.BytesIO()
    names: list[str] = []
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for n, (title, columns, rows) in enumerate(sheets, start=1):
            names.append(_sheet_name(title, used))
            letters = [column_letter(i) for i in range(len(columns))]
            widths = "".join(
                f'<col min="{i}" max="{i}" width="{min(max(len(str(c)) + 2, 10), 60)}" customWidth="1"/>'
                for i, c in enumerate(columns, start=1)
            )
            with package.open(f"xl/worksheets/sheet{n}.xml", "w") as out:
                out.write(SHEET_HEAD.format(cols=widths).encode("utf-8"))
                header = "".join(
                    cell(f"{x}1", str(c), HEADER_STYLE) for x, c in zip(letters, columns, strict=True)
                )
                parts = [f'<row r="1">{header}</row>']
                for r, row in enumerate(rows, start=2):
                    cells = "".join(
                        cell(f"{x}{r}", row.get(c)) for x, c in zip(letters, columns, strict=True)
                    )
                    parts.append(f'<row r="{r}">{cells}</row>')
                    if len(parts) >= 1000:
                        out.write("".join(parts).encode("utf-8"))
                        parts = []
                parts.append(SHEET_TAIL)
                out.write("".join(parts).encode("utf-8"))
        count = len(names)
        package.writestr(
            "[Content_Types].xml",
            CONTENT_TYPES.format(sheets="".join(SHEET_TYPE.format(n=n) for n in range(1, count + 1))),
        )
        package.writestr("_rels/.rels", ROOT_RELS)
        package.writestr(
            "xl/workbook.xml",
            WORKBOOK.format(
                sheets="".join(
                    f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{n}" r:id="rId{n}"/>'
                    for n, name in enumerate(names, start=1)
                )
            ),
        )
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            WORKBOOK_RELS.format(
                sheets="".join(
                    f'<Relationship Id="rId{n}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    f'Target="worksheets/sheet{n}.xml"/>'
                    for n in range(1, count + 1)
                )
            ),
        )
        package.writestr("xl/styles.xml", STYLES)
    return buffer.getvalue()
