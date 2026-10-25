"""Generated fake Power BI exports for the tests of Compare with Power BI (datamart.powerbi_*): obviously fake
values only ("FAKEORG PARTNER 1", "Section Alpha", "Jane Doe", "jane.doe@example.invalid", "Zq Fakeperson").

* ``export(kind, ...)`` writes one export as Power BI does (``writer="powerbi"``: the ``x:`` prefix, a byte order
  mark, inline strings, no cell reference, no ``<dimension>``, an empty cell as ``<x:c s="8" />``), as Excel
  saves it (``"excel"``: shared strings and references; ``<dimension ref="A1"/>`` on a chart's layout, none on a
  table's), or through NeuroDB's own writer (``"inline"``, ``reports/xlsx.py``).
* Power BI's two layouts: a table's ("export": header, rows, an empty row, the filters line alone in column A of
  the last row) and a chart's ("sheet1": the filters line in A1, an empty row, the header in row 3).
* ``archive(*files)`` zips them as a Mac does, with its hidden files.
"""

from __future__ import annotations

import datetime
import io
import zipfile
from collections.abc import Iterable, Iterator
from typing import Any
from xml.sax.saxutils import escape

from neurodb.datamart import powerbi_exports as px
from neurodb.reports import xlsx

PERSON = "Jane Doe <jane.doe@example.invalid>; Zq Fakeperson"
FILTERS = ("country_name is Lebanon", "status is active")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
EPOCH = datetime.datetime(1899, 12, 30)


def value(column: px.Column, n: int) -> Any:
    """A fake value of a column's type for row ``n``."""
    name, kind = column.name, column.type
    if name == "pd":
        return f"FAKE/PCA2026{n:03}/PD2026{n:03}-2"
    if name == "report":
        return f"QPR{n % 4 + 1}"
    if name == "p_code":
        return f"LB9_9_{n}"
    if name in ("grant", "fund_reservation"):
        return f"0400{n:06}"
    if name == "partner":
        return f"FAKEORG PARTNER {n}"
    if kind == px.SECTIONS:
        return "Section Alpha , Section Beta "
    if kind == px.TEXT:
        return f"Fake {column.caption.lower()} {n}"
    if kind == px.LABEL:
        return {"report_status": "Accepted", "status": "Overdue", "pd_status": "Active"}.get(name, "PD")
    if kind == px.YES_NO:
        return "No"
    if kind == px.COUNT:
        return n + 1
    if kind == px.NUMBER:
        return 10.0 * n + 0.5
    if kind == px.MONEY:
        return 1000.1 + n
    if kind == px.FRACTION:
        return 0.25
    if kind == px.DATE:
        return datetime.date(2026, 1, 1) + datetime.timedelta(days=n % 300)
    if kind == px.STAMP:
        return datetime.datetime(2026, 10, 24, 6, 30)
    if kind == px.MONTH:
        return f"{datetime.date(2026, n % 12 + 1, 1):%b %Y}"
    return f"value {n}"


def columns(kind: str, drop: Iterable[str] = ()) -> list[px.Column]:
    dropped = set(drop)
    return [c for c in px.KINDS[kind].columns if c.name not in dropped and c.caption not in dropped]


def rows(kind: str, n: int = 3, drop: Iterable[str] = ()) -> list[list[Any]]:
    cols = columns(kind, drop)
    return [[value(c, i) for c in cols] for i in range(1, n + 1)]


def _serial(moment: datetime.date) -> float:
    if not isinstance(moment, datetime.datetime):
        moment = datetime.datetime.combine(moment, datetime.time())
    delta = moment - EPOCH
    return delta.days + delta.seconds / 86_400


def filters_line(filters: Iterable[str]) -> str:
    return "Applied filters:\n" + "\n".join(filters)


def table(
    kind: str,
    data: list[list[Any]] | None = None,
    *,
    filters: Iterable[str] | None = FILTERS,
    layout: str | None = None,
    captions: list[str] | None = None,
    extra_columns: Iterable[str] = (),
    people: bool = False,
    drop: Iterable[str] = (),
) -> list[list[Any]]:
    """The export's rows as Power BI lays them out (header, data, filters line), values not yet written."""
    found = px.KINDS[kind]
    cols = columns(kind, drop)
    header = list(captions) if captions is not None else [c.caption for c in cols]
    body = [list(r) for r in (data if data is not None else rows(kind, 3, drop))]
    for extra in extra_columns:
        header.append(extra)
        body = [[*r, f"extra {i}"] for i, r in enumerate(body)]
    if people:
        header.append(found.never[0] if found.never else "Focal Point")
        body = [[*r, PERSON] for r in body]
    line = filters_line(filters) if filters is not None else None
    layout = layout or found.layout
    if layout == px.SHEET1:
        return [*([[line], []] if line else []), header, *body]
    return [header, *body, *([[], [line]] if line else [])]


def export(
    kind: str,
    data: list[list[Any]] | None = None,
    *,
    filters: Iterable[str] | None = FILTERS,
    layout: str | None = None,
    captions: list[str] | None = None,
    extra_columns: Iterable[str] = (),
    people: bool = False,
    drop: Iterable[str] = (),
    writer: str = "powerbi",
    core_creator: str | None = None,
    second_sheet: str | None = None,
    comment: str | None = None,
    date1904: bool = False,
    sheet_name: str | None = None,
) -> bytes:
    """One fake export of ``kind`` (its bytes)."""
    grid = table(
        kind,
        data,
        filters=filters,
        layout=layout,
        captions=captions,
        extra_columns=extra_columns,
        people=people,
        drop=drop,
    )
    layout = layout or px.KINDS[kind].layout
    name = sheet_name or ("Sheet1" if layout == px.SHEET1 else "Export")
    if writer == "excel":
        data_bytes = excel_xlsx_bytes(grid, name, second_sheet, layout)
    elif writer == "inline":
        data_bytes = _inline(grid, name)
    else:
        data_bytes = powerbi_xlsx_bytes(grid, name, date1904=date1904, second_sheet=second_sheet)
    if core_creator or comment:
        data_bytes = _add_parts(data_bytes, core_creator, comment)
    return data_bytes


# --------------------------------------------------------------------------------------------- the writers
def _pb_cell(v: Any, date1904: bool) -> str:
    if v is None:
        return '<x:c s="8" />'
    if isinstance(v, bool):
        return f'<x:c t="b"><x:v>{int(v)}</x:v></x:c>'
    if isinstance(v, datetime.date):
        serial = _serial(v) - (1462 if date1904 else 0)
        return f'<x:c s="8"><x:v>{serial!r}</x:v></x:c>'
    if isinstance(v, int | float):
        return f'<x:c s="8"><x:v>{v!r}</x:v></x:c>'
    text = str(v)
    space = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return f'<x:c t="inlineStr"><x:is><x:t{space}>{escape(text)}</x:t></x:is></x:c>'


def pb_sheet(grid: Iterable[list[Any]], date1904: bool = False) -> Iterator[str]:
    yield '﻿<?xml version="1.0" encoding="utf-8"?>'
    yield f'<x:worksheet xmlns:x="{MAIN}"><x:sheetData>'
    for r in grid:
        if not r:
            yield "<x:row />"
        else:
            yield "<x:row>" + "".join(_pb_cell(v, date1904) for v in r) + "</x:row>"
    yield "</x:sheetData></x:worksheet>"


def powerbi_xlsx_bytes(
    grid: Iterable[list[Any]],
    name: str = "Export",
    *,
    date1904: bool = False,
    second_sheet: str | None = None,
) -> bytes:
    """A workbook as Power BI writes it."""
    out = io.BytesIO()
    pr = ' <x:workbookPr date1904="1" />' if date1904 else ""
    sheets = f'<x:sheet name="{name}" sheetId="1" r:id="R1" xmlns:r="{RELS}" />'
    rels = f'<Relationship Type="{RELS}/worksheet" Target="/xl/worksheets/sheet1.xml" Id="R1" />'
    if second_sheet is not None:
        sheets += f'<x:sheet name="Other" sheetId="2" r:id="R2" xmlns:r="{RELS}" />'
        rels += f'<Relationship Type="{RELS}/worksheet" Target="/xl/worksheets/sheet2.xml" Id="R2" />'
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="utf-8"?><x:workbook xmlns:x="{MAIN}">{pr}<x:sheets>{sheets}'
            "</x:sheets></x:workbook>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="utf-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Type="{RELS}/officeDocument" Target="/xl/workbook.xml" Id="R0" /></Relationships>',
        )
        with z.open("xl/worksheets/sheet1.xml", "w") as sheet:
            for part in pb_sheet(grid, date1904):
                sheet.write(part.encode("utf-8"))
        if second_sheet is not None:
            z.writestr("xl/worksheets/sheet2.xml", "".join(pb_sheet([[second_sheet]])).encode("utf-8"))
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="utf-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}<Relationship Type="{RELS}/styles" Target="/xl/styles.xml" Id="RS" /></Relationships>',
        )
        z.writestr(
            "xl/styles.xml", f'<?xml version="1.0" encoding="utf-8"?><x:styleSheet xmlns:x="{MAIN}" />'
        )
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="utf-8"?><Types '
            'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml" />'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
            "</Types>",
        )
    return out.getvalue()


def excel_xlsx_bytes(grid: list[list[Any]], name: str, second_sheet: str | None, layout: str) -> bytes:
    """A workbook as Excel saves a chart's export: shared strings (the filters line a formula's text), cell and
    row references, and ``<dimension ref="A1"/>`` on a chart's layout (none on a table's)."""
    strings: dict[str, int] = {}

    def cell(ref: str, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime.date):
            return f'<c r="{ref}" s="1"><v>{_serial(v)!r}</v></c>'
        if isinstance(v, bool):
            return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
        if isinstance(v, int | float):
            return f'<c r="{ref}"><v>{v!r}</v></c>'
        if str(v).startswith("Applied filters:"):
            return f'<c r="{ref}" t="str"><v>{escape(str(v))}</v></c>'
        n = strings.setdefault(str(v), len(strings))
        return f'<c r="{ref}" t="s"><v>{n}</v></c>'

    body = []
    for r, row in enumerate(grid, start=1):
        cells = "".join(cell(f"{xlsx.column_letter(i)}{r}", v) for i, v in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')
    dimension = '<dimension ref="A1"/>' if layout == px.SHEET1 else ""
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{MAIN}" xmlns:r="{RELS}">'
        f'{dimension}<sheetData xml:space="preserve">{"".join(body)}</sheetData></worksheet>'
    )
    shared = "".join(
        f"<si><t{' xml:space="preserve"' if t != t.strip() else ''}>{escape(t)}</t></si>" for t in strings
    )
    sheets = f'<sheet name="{name}" sheetId="1" r:id="rId1"/>'
    rels = f'<Relationship Id="rId1" Type="{RELS}/worksheet" Target="worksheets/sheet1.xml"/>'
    if second_sheet is not None:
        sheets += '<sheet name="Other" sheetId="2" r:id="rId5"/>'
        rels += f'<Relationship Id="rId5" Type="{RELS}/worksheet" Target="worksheets/sheet2.xml"/>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types '
            'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{RELS}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{MAIN}" xmlns:r="{RELS}">'
            f"<sheets>{sheets}</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{rels}<Relationship Id="rId4" Type="{RELS}/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>",
        )
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if second_sheet is not None:
            z.writestr(
                "xl/worksheets/sheet2.xml",
                f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
                f"{escape(second_sheet)}</t></is></c></row></sheetData></worksheet>",
            )
        z.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="{MAIN}">{shared}</sst>',
        )
    return out.getvalue()


def _inline(grid: list[list[Any]], name: str) -> bytes:
    """Through reports/xlsx.py: the header and rows of a table's layout (inline strings)."""
    header, *body = grid
    return xlsx.workbook([(name, list(header), [dict(zip(header, r, strict=False)) for r in body])])


def patch_sheet(data: bytes, change, part: str = "xl/worksheets/sheet1.xml") -> bytes:
    """The workbook with one part's XML changed."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == part:
                content = change(content.decode("utf-8-sig")).encode("utf-8")
            z.writestr(info.filename, content)
    return out.getvalue()


def _add_parts(data: bytes, creator: str | None, comment: str | None) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in source.infolist():
            z.writestr(info.filename, source.read(info.filename))
        if creator:
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
                'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:creator>{escape(creator)}</dc:creator><cp:lastModifiedBy>{escape(creator)}"
                "</cp:lastModifiedBy></cp:coreProperties>",
            )
        if comment:
            z.writestr(
                "xl/comments1.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><comments xmlns="{MAIN}"><authors><author>'
                f'{escape(comment)}</author></authors><commentList><comment ref="A1" authorId="0"><text><t>'
                f"{escape(comment)}</t></text></comment></commentList></comments>",
            )
    return out.getvalue()


# ----------------------------------------------------------------------------------------------- the zip
APPLE_DOUBLE = b"\x00\x05\x16\x07" + b"\x00" * 396


def archive(*files: tuple[str, bytes], junk: bool = True, mac: bool = True) -> bytes:
    """A zip of exports as a Mac makes it: each file with its ``__MACOSX/._<name>`` (not an Excel file), and
    with ``junk`` a ``.DS_Store``, a ``notes.txt`` and a folder entry."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            z.writestr(name, content)
            if mac:
                z.writestr(f"__MACOSX/._{name}", APPLE_DOUBLE)
        if junk:
            z.writestr(".DS_Store", b"\x00\x00\x00\x01Bud1")
            z.writestr("notes.txt", b"fake notes")
            z.writestr(zipfile.ZipInfo("folder/"), b"")
    return out.getvalue()


def every_export(writer: str = "powerbi") -> list[tuple[str, bytes]]:
    """The 13 kinds, each under a misleading file name."""
    return [
        (f"Fake export {n} .xlsx", export(kind, writer=writer)) for n, kind in enumerate(px.KINDS, start=1)
    ]


def big_disaggregations(n: int) -> bytes:
    """A disaggregation export of ``n`` rows, streamed by reports/xlsx.py (the filters line last)."""
    cols = columns("disaggregations")

    def body() -> Iterator[dict[str, Any]]:
        for i in range(n):
            yield {c.caption: value(c, i) for c in cols}
        yield {}
        yield {cols[0].caption: filters_line(FILTERS)}

    return xlsx.workbook([("Export", [c.caption for c in cols], body())])


# ------------------------------------------------------------------------------------- NeuroDB's side (db)
def user(username: str, *, staff: bool = True, validator: bool = True, superuser: bool = False, groups=()):
    """A NeuroDB user (a Viewer; ticked as a validator unless ``validator`` is False)."""
    from django.contrib.auth.models import Group

    from neurodb.accounts.models import User
    from neurodb.accounts.roles import VALIDATORS, VIEWER

    if superuser:
        return User.objects.create_superuser(
            username=username, email=f"{username}@example.org", password="pw-1234567-x"
        )
    found = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="pw-1234567-x", is_staff=staff
    )
    names = (VIEWER, *((VALIDATORS,) if validator else ()), *groups)
    found.groups.set(Group.objects.filter(name__in=names))
    return found


def post(client, *files: tuple[str, bytes]):
    """The upload form posted with ``files`` (each (name, bytes))."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    uploads = [
        SimpleUploadedFile(name, data, content_type="application/octet-stream") for name, data in files
    ]
    return client.post(reverse("admin:datamart_powerbi"), {"files": uploads})
