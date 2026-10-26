"""NeuroDB's own reader of Power BI's Excel exports (Compare with Power BI), standard library only:
``zipfile`` and a streaming XML parser, the same in tests and in production. openpyxl never reads an upload.

Power BI writes its exports without a ``<dimension>`` and, for its tables, without cell references (each cell
is the next column) and with inline strings; Excel, saving a chart's export, writes shared strings and a
``<dimension ref="A1"/>``. openpyxl's read-only mode parses a whole sheet with no dimension before it returns
its first row; this reader streams the sheet and stops when asked, so the header is checked in the request
and the rows are read once, in the job.

* **Layouts.** Power BI's tables: the header in row 1, the data, then the filters line ("Applied filters: …")
  alone in column A of the last row. Charts and cards ("Sheet1"): the filters line in A1, an empty row, the
  header, the data. A row whose only value is a column A text starting with "Applied filters:" is the filters
  line, wherever it is: it is never a data row.
* **Only kept columns are read.** A column the caller does not keep is skipped by its position: its shared
  string is never looked up, its text never converted or kept (column A is read alone only on a row whose
  every other cell is empty, to tell the filters line).
* **Memory** does not grow with the rows: each row is dropped from the tree once read.
* **Refused:** an OLE file (encrypted or labelled), a file that is not a zip, more than :data:`MAX_PARTS`
  parts (read from the zip's end record before ``zipfile`` parses its directory: :func:`too_many_entries`),
  a part declared above :data:`MAX_PART_BYTES`, a shared strings part above :data:`MAX_STRINGS_BYTES`, and a
  part with a ``<!DOCTYPE`` before its first element (Python's expat also limits entity expansion).
  :class:`Unreadable` carries a code word, never a cell's text.
"""

from __future__ import annotations

import datetime
import io
import posixpath
import re
import struct
import zipfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

MAX_PARTS = 500
MAX_PART_BYTES = 300 * 1024 * 1024
MAX_STRINGS_BYTES = 50 * 1024 * 1024
HEAD_BYTES = 64 * 1024  # a part's first element must start within this
CHUNK = 256 * 1024
PEEK_ROWS = 5  # non-empty rows read for the header
PEEK_DATA_ROWS = 100  # data rows after it looked at for the columns holding a value (never read)
OLE = b"\xd0\xcf\x11\xe0"
FILTERS_PREFIX = "Applied filters:"
SHEET_NAMES = ("Export", "Sheet1")  # Power BI's sheet, then Excel's
ROOT_ELEMENT = re.compile(rb"<(?![?!])")
DOCTYPE = b"<!doctype"
CELL_REF = re.compile(r"^([A-Za-z]{1,3})\d*$")
EPOCH_1900 = datetime.datetime(1899, 12, 30)  # Excel's day 0 (with its 1900 leap year)
EPOCH_1904 = datetime.datetime(1904, 1, 1)
ENTRY_BYTES = 1024  # a zip's directory may take this much per entry it is allowed (a real one takes ~100)
END_RECORD = b"PK\x05\x06"
END_RECORD_64 = b"PK\x06\x06"
LOCATOR_64 = b"PK\x06\x07"


class Unreadable(Exception):
    """The file is not an Excel export NeuroDB reads. ``reason`` is a code word, never a cell's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Error:
    """An error cell (``#N/A``, ``#DIV/0!``): read as empty, counted as a value NeuroDB could not read."""

    def __repr__(self) -> str:
        return "ERROR"


ERROR = _Error()


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _attr(element: ElementTree.Element, name: str) -> str | None:
    """An attribute by its local name, in any namespace (``r:id``)."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def column_index(ref: str) -> int | None:
    """``"B12"`` -> 1; None when the reference has no column."""
    found = CELL_REF.match(ref or "")
    if not found:
        return None
    n = 0
    for letter in found.group(1).upper():
        n = n * 26 + ord(letter) - 64
    return n - 1


def serial(value: float, date1904: bool = False) -> datetime.datetime:
    """An Excel date serial number as a moment."""
    return (EPOCH_1904 if date1904 else EPOCH_1900) + datetime.timedelta(days=value)


@dataclass
class Peek:
    """What the first rows of an export say: its sheet, how many worksheets the workbook has, the header's
    captions by position (None when only a filters line was found), the filters line when it comes first (a
    chart's export), and the positions holding a value in the header or the first :data:`PEEK_DATA_ROWS`
    data rows (whether a cell holds a value is looked at, never the value: a column with no caption is still
    seen)."""

    sheet: str
    worksheets: int
    header: list[str] | None
    filters: str | None
    sheet_bytes: int
    unpacked: int
    filled: frozenset[int] = frozenset()


@dataclass
class _Sheet:
    name: str
    part: str


@dataclass
class Rows:
    """The data rows of a sheet as they are read, with what was seen on the way: the filters line, the error
    cells in the kept columns, the header's captions."""

    header: list[str] | None = None
    filters: str | None = None
    errors: int = 0
    _source: Iterator[dict[int, Any]] | None = field(default=None, repr=False)

    def __iter__(self) -> Iterator[dict[int, Any]]:
        return self._source or iter(())


def too_many_entries(data: bytes, limit: int) -> bool:
    """A zip whose directory holds more than ``limit`` entries, or takes more than :data:`ENTRY_BYTES` each:
    read from its end record (the zip64 one too) before ``zipfile`` parses the directory, whose every entry
    it would build first (``zipfile`` reads as many bytes as the record's size says, whatever its count). A
    file with no end record is not a zip: False (``zipfile`` refuses it)."""
    at = data.rfind(END_RECORD, max(0, len(data) - 65_557))
    if at < 0 or len(data) < at + 22:
        return False
    fields = struct.unpack("<4s4H2LH", data[at : at + 22])
    entries, size = fields[4], fields[5]
    if entries == 0xFFFF or size == 0xFFFFFFFF:  # zip64: the record before the locator says
        locator = at - 20
        record = locator - 56
        found = record >= 0 and data[locator : locator + 4] == LOCATOR_64
        if not found or data[record : record + 4] != END_RECORD_64:
            return True  # a zip64 marker without its record: never parsed
        entries, size = struct.unpack("<QQ", data[record + 32 : record + 48])
    return entries > limit or size > limit * ENTRY_BYTES


class Book:
    """An export opened: its parts checked, its worksheets and shared strings found."""

    def __init__(self, data: bytes) -> None:
        if data[:4] == OLE:
            raise Unreadable("protected")
        if too_many_entries(data, MAX_PARTS):  # before zipfile builds every entry of the directory
            raise Unreadable("too_many_parts")
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
            infos = self.archive.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError) as exc:
            raise Unreadable("not_zip") from exc
        if len(infos) > MAX_PARTS:
            raise Unreadable("too_many_parts")
        if any(i.file_size > MAX_PART_BYTES for i in infos):
            raise Unreadable("part_too_big")
        self.sizes = {i.filename: i.file_size for i in infos}
        self.unpacked = sum(self.sizes.values())
        workbook = self._office_document()
        self.date1904 = False
        sheets: list[tuple[str, str]] = []
        for event, element in self._events(workbook):
            if event != "end":
                continue
            name = _local(element.tag)
            if name == "workbookPr":
                self.date1904 = (element.get("date1904") or "").lower() in ("1", "true")
            elif name == "sheet":
                sheets.append((element.get("name") or "", _attr(element, "id") or ""))
        rels = self._relations(workbook)
        self.sheets = [
            _Sheet(name, rels[rid][1]) for name, rid in sheets if rid in rels and rels[rid][0] == "worksheet"
        ]
        if not self.sheets:
            raise Unreadable("no_sheet")
        found = [target for kind, target in rels.values() if kind == "sharedStrings"]
        self.strings_part = found[0] if found and found[0] in self.sizes else None
        if self.strings_part and self.sizes[self.strings_part] > MAX_STRINGS_BYTES:
            raise Unreadable("strings_too_big")
        self._strings: list[str] | None = None

    # ------------------------------------------------------------------------------------------ parts
    def _events(self, part: str) -> Iterator[tuple[str, ElementTree.Element]]:
        """(event, element) of a part, streamed; a part with a ``<!DOCTYPE`` before its first element is
        refused."""
        if part not in self.sizes:
            raise Unreadable("missing_part")
        parser = ElementTree.XMLPullParser(events=("start", "end"))
        with self.archive.open(part) as stream:
            head = stream.read(HEAD_BYTES)
            root = ROOT_ELEMENT.search(head)
            if root is None or DOCTYPE in head[: root.start()].lower():
                raise Unreadable("doctype")
            chunk = head
            while chunk:
                parser.feed(chunk)
                yield from parser.read_events()
                chunk = stream.read(CHUNK)
        parser.close()
        yield from parser.read_events()

    def _office_document(self) -> str:
        if "_rels/.rels" in self.sizes:
            for _kind, target in self._relations("").values():
                if target.endswith(".xml") and _kind == "officeDocument":
                    return target
        if "xl/workbook.xml" in self.sizes:
            return "xl/workbook.xml"
        raise Unreadable("no_workbook")

    def _relations(self, part: str) -> dict[str, tuple[str, str]]:
        """A part's relations: id -> (type, the part it names, as a path in the zip)."""
        folder, name = posixpath.split(part)
        rels = posixpath.join(folder, "_rels", f"{name}.rels")
        if rels not in self.sizes:
            return {}
        found = {}
        for event, element in self._events(rels):
            if event == "end" and _local(element.tag) == "Relationship":
                target = element.get("Target") or ""
                if target.startswith("/"):
                    path = target.lstrip("/")
                else:
                    path = posixpath.normpath(posixpath.join(folder, target))
                kind = (element.get("Type") or "").rpartition("/")[2]
                found[element.get("Id") or ""] = (kind, path)
        return found

    def strings(self) -> list[str]:
        """The shared strings, read once."""
        if self._strings is None:
            self._strings = []
            if self.strings_part:
                root = None
                for event, element in self._events(self.strings_part):
                    if event == "start" and root is None:
                        root = element
                    elif event == "end" and _local(element.tag) == "si":
                        self._strings.append(_text_of(element))
                        if root is not None:
                            root.clear()
        return self._strings

    # ------------------------------------------------------------------------------------------ sheets
    def sheet(self, name: str | None = None) -> _Sheet:
        """The sheet ``name``, else "Export", else "Sheet1", else the first."""
        by_name = {s.name: s for s in self.sheets}
        for wanted in (name, *SHEET_NAMES):
            if wanted and wanted in by_name:
                return by_name[wanted]
        return self.sheets[0]

    def raw_rows(
        self, sheet: _Sheet, keep: Collection[int] | None
    ) -> Iterator[tuple[dict[int, Any], str | None, list[int]]]:
        """Each non-empty row of ``sheet``: its values by position, the filters line's text when the row is
        one, and the positions of the cells holding a value (read or not). Until the header is passed (the
        first row that is not the filters line) every column is read; after it, only the columns of ``keep``
        (every column when ``keep`` is None)."""
        strings: list[str] | None = None
        data = None
        whole = True
        for event, element in self._events(sheet.part):
            if event == "start":
                if data is None and _local(element.tag) == "sheetData":
                    data = element
                continue
            if _local(element.tag) != "row":
                continue
            values: dict[int, Any] = {}
            filled: list[int] = []
            first = None
            col = -1
            for cell in element:
                if _local(cell.tag) != "c":
                    continue
                found = column_index(cell.get("r") or "")
                col = found if found is not None else col + 1
                if not _filled(cell):
                    continue
                filled.append(col)
                if col == 0:
                    first = cell
                if whole or keep is None or col in keep:
                    if strings is None and cell.get("t") == "s":
                        strings = self.strings()
                    values[col] = _value(cell, strings)
            filters = None
            if filled == [0]:  # column A alone: the filters line, or a row of one value
                if 0 not in values:
                    if strings is None and first.get("t") == "s":
                        strings = self.strings()
                    text = _value(first, strings)
                else:
                    text = values[0]
                if isinstance(text, str) and text.strip().startswith(FILTERS_PREFIX):
                    filters, values = text, {}
            if data is not None:
                data.clear()  # the row read is dropped: memory does not grow with the rows
            if not filled:
                continue
            if filters is None:
                whole = False
            yield values, filters, filled


def _filled(cell: ElementTree.Element) -> bool:
    """A cell holds a value (not looked at: only whether there is one)."""
    for child in cell:
        name = _local(child.tag)
        if name == "v" and child.text:
            return True
        if name == "is" and any(_local(t.tag) == "t" and t.text for t in child.iter()):
            return True
    return False


def _text_of(element: ElementTree.Element) -> str:
    """The text of a shared or inline string: its runs' texts, without phonetic runs."""
    parts = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            parts.append(_text_of(child))
    return "".join(parts)


def _value(cell: ElementTree.Element, strings: list[str] | None) -> Any:
    kind = cell.get("t") or "n"
    v = None
    inline = None
    for child in cell:
        name = _local(child.tag)
        if name == "v":
            v = child.text
        elif name == "is":
            inline = child
    if kind == "s":
        try:
            return strings[int(v)] if strings is not None and v is not None else ERROR
        except (ValueError, IndexError):
            return ERROR
    if kind == "inlineStr":
        return _text_of(inline) if inline is not None else (v or None)
    if kind in ("str", "d"):
        return v if v else None
    if kind == "b":
        return None if v is None else v.strip() in ("1", "true")
    if kind == "e":
        return ERROR
    if v is None or not v.strip():
        return None
    try:
        return float(v)
    except ValueError:
        return ERROR


def peek(data: bytes, recognised: Callable[[list[str]], bool] | None = None) -> Peek:
    """The sheet an export is read from and its first rows: at most :data:`PEEK_ROWS` non-empty rows are read
    (the rest of the sheet never is). The sheet is "Export", else "Sheet1", else the first whose header
    ``recognised`` accepts, else the first."""
    book = Book(data)
    names = {s.name for s in book.sheets}
    chosen = next((book.sheet(n) for n in SHEET_NAMES if n in names), None)
    first = None
    for sheet in [chosen] if chosen else book.sheets:
        header, filters, filled = _head(book, sheet)
        found = Peek(
            sheet.name, len(book.sheets), header, filters, book.sizes[sheet.part], book.unpacked, filled
        )
        if chosen or recognised is None or (header is not None and recognised(header)):
            return found
        first = first or found
    return first


def _head(book: Book, sheet: _Sheet) -> tuple[list[str] | None, str | None, frozenset[int]]:
    """The header's captions, the filters line before it, and the positions holding a value in the header and
    the data rows after it (at most :data:`PEEK_DATA_ROWS`; no value of theirs is read)."""
    filters = header = None
    filled: set[int] = set()
    data = 0
    for n, (values, line, cells) in enumerate(book.raw_rows(sheet, ())):
        if header is None:
            if n >= PEEK_ROWS:
                break
            if line is not None:
                filters = line
                continue
            header = _captions(values)
        elif line is not None or data >= PEEK_DATA_ROWS:
            break  # a table's filters line comes after its data
        else:
            data += 1
        filled.update(cells)
    return header, filters, frozenset(filled)


def _captions(values: dict[int, Any]) -> list[str]:
    width = max(values) + 1 if values else 0
    return [_caption(values.get(i)) for i in range(width)]


def _caption(value: Any) -> str:
    if value is None or value is ERROR:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def rows(data: bytes, keep: Collection[int], sheet: str | None = None) -> Rows:
    """The data rows of an export (after its header, never its filters line): the kept columns' values by
    position. Read as they are iterated; the filters line, the header and the error cells are known once
    they are read through."""
    book = Book(data)
    chosen = book.sheet(sheet)
    found = Rows()
    wanted = set(keep)

    def source() -> Iterator[dict[int, Any]]:
        for values, line, _filled_at in book.raw_rows(chosen, wanted):
            if line is not None:
                found.filters = line
            elif found.header is None:
                found.header = _captions(values)
            else:
                found.errors += sum(1 for v in values.values() if v is ERROR)
                yield {i: (None if v is ERROR else v) for i, v in values.items()}

    found._source = source()
    return found
