"""Reading Power BI's exports for Compare with Power BI (Admin → Data and sync): the upload kept in memory,
the zip opened entry by entry, each export recognised by its columns, and its rows kept as NeuroDB compares
them.

* **Nothing reaches the disk.** :class:`UploadHandler` keeps each uploaded file in memory (never Django's
  temporary file), drops a file above :data:`MAX_FILE_BYTES` and stops past :data:`MAX_UPLOAD_BYTES` in all.
* **The checks in the request** (:func:`prepare`): the zip's entries one at a time (hidden files a Mac adds
  skipped and counted, a declared size checked before an entry is opened), each export's bytes (protected,
  not a zip, too many parts, too much data in the whole upload), then its header (:func:`powerbi_xlsx.peek`,
  a few rows) and its kind (:func:`powerbi_exports.recognise`). Each file is read inside its own ``try``: an
  error keeps only its class name.
* **Where it is read.** A one-sheet export whose every column NeuroDB compares is *staged*: a repacked copy
  holding only that sheet (and its shared strings) waits for the job, which reads it and empties it. Any other
  export (a column that names people, a column NeuroDB does not compare, a second sheet) is read now, in the
  request, keeping only the compared columns: the others are dropped by their position before any value is
  converted, so a person's name is never stored, shown, logged or sent anywhere.
* **What is kept of a read** (:func:`read`): one gzip JSON line per data row with the kind's columns only,
  each read as its type (:func:`coerce`); free texts cleaned of people (:func:`clean`); the parsed filters
  (``powerbi_filters``), never the raw line; the counts and plain notes. Never the file, never the zip.
"""

from __future__ import annotations

import datetime
import gzip
import io
import json
import logging
import posixpath
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler, SkipFile, StopUpload

from . import powerbi_exports as px
from . import powerbi_filters, powerbi_xlsx
from . import powerbi_words as words
from .powerbi_xlsx import ERROR, Unreadable

logger = logging.getLogger(__name__)

MB = 1024 * 1024
MAX_FILE_BYTES = 25 * MB  # one uploaded file, or one entry of a zip (its declared size)
MAX_UPLOAD_BYTES = 50 * MB  # everything uploaded at once
MAX_EXPORTS = 30  # exports in one upload, after unzipping
MAX_ENTRIES = 200  # entries of a zip
MAX_UNPACKED = 400 * MB  # the declared unpacked sizes of every export of an upload, together
MAX_READ_NOW = 20 * MB  # a sheet read in the request (an export with a column NeuroDB does not compare)
MAX_STAGED = 50 * MB  # the repacked copies of one upload, together
ROUND_COUNTS = (1_000, 30_000, 150_000)  # Power BI cuts an export at these
TITLE_CUT = 255  # Power BI cuts titles at this length
MEMO_LIMIT = 5_000  # the free texts cleaned once per read
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_NUMBER_TEXT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%d %B %Y")
STAMP_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p")
MONTH_FORMATS = ("%b %Y", "%B %Y")


# --------------------------------------------------------------------------------------------- the upload
class UploadHandler(FileUploadHandler):
    """Keeps every uploaded file in memory, never in a temporary file: a file above :data:`MAX_FILE_BYTES` is
    dropped (its cleaned name noted), and an upload above :data:`MAX_UPLOAD_BYTES` stops (nothing is kept).
    The form's other fields come before the files, so the request's CSRF check still reads its token."""

    def __init__(self, request=None) -> None:
        super().__init__(request)
        self.too_big: list[str] = []
        self.too_big_upload = False
        self.received = 0
        self.buffer: io.BytesIO | None = None

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):  # noqa: N803
        if content_length and content_length > MAX_UPLOAD_BYTES:
            self.too_big_upload = True  # the first file stops the upload (new_file)
        return None

    def new_file(self, *args, **kwargs) -> None:
        super().new_file(*args, **kwargs)
        if self.too_big_upload:
            raise StopUpload(connection_reset=False)
        self.buffer = io.BytesIO()

    def receive_data_chunk(self, raw_data: bytes, start: int) -> None:
        self.received += len(raw_data)
        if self.received > MAX_UPLOAD_BYTES:
            self.too_big_upload = True
            self.buffer = None
            raise StopUpload(connection_reset=False)
        if start + len(raw_data) > MAX_FILE_BYTES:
            self.too_big.append(file_name(self.file_name))
            self.buffer = None
            raise SkipFile()
        self.buffer.write(raw_data)
        return None

    def file_complete(self, file_size: int):
        if self.buffer is None:
            return None
        self.buffer.seek(0)
        found = InMemoryUploadedFile(
            file=self.buffer,
            field_name=self.field_name,
            name=self.file_name,
            content_type=self.content_type,
            size=file_size,
            charset=self.charset,
            content_type_extra=self.content_type_extra,
        )
        self.buffer = None
        return found

    def upload_interrupted(self) -> None:
        self.buffer = None


def file_name(name: Any, names: frozenset[str] | None = None) -> str:
    """A file's base name as a page shows it: no folder, no control character, at most 255 characters, and
    without a known person's name, an e-mail or a phone number."""
    base = re.split(r"[/\\]", str(name or ""))[-1]
    base = _ILLEGAL.sub("", base).strip()[:255] or "export.xlsx"
    if names is None:
        from neurodb.watch import people

        names = people.known_names()
    return clean(base, names, {})[:255] or "export.xlsx"


# ---------------------------------------------------------------------------------------------- people
def clean(text: str, names: frozenset[str], memo: dict[str, str]) -> str:
    """A free text without the names NeuroDB knows (whatever their case), e-mails and phone numbers: the
    people rule of Raw eTools data (``comparison._clean``), once per distinct text."""
    from . import comparison

    found = memo.get(text)
    if found is None:
        if len(memo) >= MEMO_LIMIT:  # memory never grows with the rows, however many texts differ
            memo.clear()
        found = memo[text] = comparison._clean(text, names)
    return found


# ----------------------------------------------------------------------------------------------- values
def coerce(column: px.Column, value: Any, date1904: bool = False) -> tuple[Any, bool]:
    """A cell's value read as its column's type: (the value, whether it was read). An empty cell is None; a
    value NeuroDB cannot read is None too, and counted (its text is never kept)."""
    if value is None or value is ERROR or (isinstance(value, str) and not value.strip()):
        return None, value is not ERROR
    kind = column.type
    if kind in (px.NUMBER, px.COUNT, px.MONEY, px.FRACTION):
        return _number(value, kind)
    if kind == px.DATE:
        found = _moment(value, date1904, DATE_FORMATS)
        return (found.date(), True) if found else (None, False)
    if kind == px.STAMP:
        found = _moment(value, date1904, STAMP_FORMATS + DATE_FORMATS)
        return (found, True) if found else (None, False)
    if kind == px.MONTH:
        if isinstance(value, float):
            found = powerbi_xlsx.serial(value, date1904)
        else:
            found = _parsed(" ".join(str(value).split()).title(), MONTH_FORMATS)
        return (f"{found:%b %Y}", True) if found else (None, False)
    if isinstance(value, bool):
        return ("Yes" if value else "No"), True
    if isinstance(value, float):
        value = str(int(value)) if value.is_integer() else repr(value)
    return _ILLEGAL.sub("", str(value)), True


def _number(value: Any, kind: str) -> tuple[Any, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, float):
        number = value
    else:
        text = str(value).strip().replace(",", "").replace(" ", "").replace(" ", "")
        percent = text.endswith("%")
        text = text.rstrip("%")
        if not _NUMBER_TEXT.match(text):
            return None, False
        number = float(text)
        if percent and kind == px.FRACTION:
            number /= 100
    if number != number or number in (float("inf"), float("-inf")):
        return None, False
    if kind == px.COUNT and number.is_integer():
        return int(number), True
    return number, True


def _parsed(text: str, formats: Iterable[str]) -> datetime.datetime | None:
    for fmt in formats:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _moment(value: Any, date1904: bool, formats: Iterable[str]) -> datetime.datetime | None:
    if isinstance(value, float):
        if not 0 < value < 2_958_466:  # Excel's last day, 31 Dec 9999
            return None
        return powerbi_xlsx.serial(value, date1904).replace(microsecond=0)
    text = " ".join(str(value).split())
    try:
        found = datetime.datetime.fromisoformat(text)
        return found.replace(tzinfo=None) if found.tzinfo else found
    except ValueError:
        return _parsed(text, formats)


def _json(value: Any) -> Any:
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    return value


# ------------------------------------------------------------------------------------------- one export
@dataclass
class Read:
    """An export read: its rows (gzip JSON lines of the kept columns, in ``columns``' order), their count, the
    filter, Power BI's refresh, and the plain notes."""

    rows: bytes
    rows_read: int
    columns: list[str]
    filter: powerbi_filters.Filter
    stamp: datetime.datetime | None = None
    unreadable: int = 0
    cut: int = 0
    notes: list[str] = field(default_factory=list)


def read(
    data: bytes,
    match: px.Match,
    names: frozenset[str],
    sections: Iterable[str] = (),
    sheet: str | None = None,
) -> Read:
    """Read every data row of an export, keeping only ``match``'s columns (the others are skipped by their
    position: never looked up, converted or kept)."""
    kind = match.kind
    book = powerbi_xlsx.Book(data)
    columns = px.kept(kind, match.positions)
    position = {c.name: i for i, c in match.positions.items()}
    order = [(position[c.name], c) for c in columns]
    titles = [c for c in columns if c.title]
    names_out = [c.name for c in columns] + [f"{c.name}__cut" for c in titles]
    found = powerbi_xlsx.rows(data, set(position.values()), sheet)
    memo: dict[str, str] = {}
    out = io.BytesIO()
    rows_read = unreadable = cut = 0
    stamp = None
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as stream:
        for values in found:
            line: list[Any] = []
            cuts: list[bool] = []
            for i, column in order:
                raw = values.get(i)
                value, ok = coerce(column, raw, book.date1904)
                unreadable += not ok
                if column.title:
                    long = isinstance(raw, str) and len(raw) >= TITLE_CUT
                    cuts.append(long)
                    cut += long
                if column.free_text and isinstance(value, str):
                    value = clean(value, names, memo)
                if column.type == px.STAMP and value is not None:
                    stamp = value if stamp is None or value > stamp else stamp
                line.append(_json(value))
            if all(v is None for v in line):
                continue  # a row with nothing NeuroDB compares
            rows_read += 1
            stream.write(json.dumps(line + cuts, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            stream.write(b"\n")
    unreadable += found.errors
    parsed = powerbi_filters.parse(found.filters, sections)
    notes = []
    if rows_read in ROUND_COUNTS:
        notes.append(words.ROUND_COUNT.format(n=rows_read))
    if unreadable:
        notes.append(words.plural(unreadable, words.UNREADABLE_VALUE, words.UNREADABLE_VALUES))
    if cut:
        notes.append(words.plural(cut, words.CUT_TITLE, words.CUT_TITLES))
    return Read(out.getvalue(), rows_read, names_out, parsed, stamp, unreadable, cut, notes)


def rows_of(rows: bytes | None) -> Iterable[list[Any]]:
    """The rows kept of an export, one list each (``columns``' order)."""
    if not rows:
        return
    with gzip.GzipFile(fileobj=io.BytesIO(rows), mode="rb") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def repack(data: bytes, sheet: str) -> bytes:
    """The copy of an export the job reads: its one sheet and its shared strings, under a workbook NeuroDB
    writes itself. Other sheets, comments, the document's properties (who made or saved it) and anything else
    the file holds are never copied."""
    book = powerbi_xlsx.Book(data)
    chosen = book.sheet(sheet)
    strings = book.strings_part
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as package:
        package.writestr(
            "[Content_Types].xml", REPACK_TYPES.format(strings=REPACK_STRINGS_TYPE if strings else "")
        )
        package.writestr("_rels/.rels", REPACK_ROOT_RELS)
        package.writestr(
            "xl/workbook.xml",
            REPACK_WORKBOOK.format(pr='<workbookPr date1904="1"/>' if book.date1904 else ""),
        )
        package.writestr(
            "xl/_rels/workbook.xml.rels", REPACK_RELS.format(strings=REPACK_STRINGS_REL if strings else "")
        )
        _copy(book, chosen.part, package, "xl/worksheets/sheet1.xml")
        if strings:
            _copy(book, strings, package, "xl/sharedStrings.xml")
    return out.getvalue()


def _copy(book: powerbi_xlsx.Book, part: str, package: zipfile.ZipFile, name: str) -> None:
    with book.archive.open(part) as source, package.open(name, "w") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REPACK_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "{strings}</Types>"
)
REPACK_STRINGS_TYPE = (
    '<Override PartName="/xl/sharedStrings.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
)
REPACK_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{RELS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
REPACK_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<workbook xmlns="{MAIN}" xmlns:r="{RELS}">{{pr}}'
    '<sheets><sheet name="Export" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
REPACK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{RELS}/worksheet" Target="worksheets/sheet1.xml"/>'
    "{strings}</Relationships>"
)
REPACK_STRINGS_REL = f'<Relationship Id="rId2" Type="{RELS}/sharedStrings" Target="sharedStrings.xml"/>'


# ----------------------------------------------------------------------------------------- the whole upload
@dataclass
class Prepared:
    """One export recognised in the request: staged (its repacked copy) or read (its rows)."""

    kind: px.Kind
    file_name: str
    state: str  # "staged" or "read"
    content: bytes | None = None
    sheet: str = ""
    read: Read | None = None
    dropped: list[str] = field(default_factory=list)  # sentences: the columns that name people, dropped
    not_compared: list[str] = field(default_factory=list)  # known captions not kept
    unknown: int = 0  # captions NeuroDB does not know
    match: px.Match | None = None


@dataclass
class Upload:
    """What the request made of the files: the exports recognised, the plain sentences of the files not
    read, and the hidden files skipped."""

    exports: list[Prepared] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    hidden: int = 0
    unpacked: int = 0
    staged: int = 0
    seen: int = 0  # exports met (after unzipping)
    stopped: str = ""  # TOO_MUCH or TOO_MANY: the rest is refused


def prepare(files: Iterable[Any], names: frozenset[str], sections: Iterable[str] = ()) -> Upload:
    """Every uploaded file (the zip or the exports) checked and recognised, each inside its own ``try``."""
    found = Upload()
    sections = tuple(sections)
    for upload in files:
        name = file_name(getattr(upload, "name", ""), names)
        try:
            data = upload.read()
            if name.lower().endswith(".zip") or _plain_zip(data):
                _archive(found, data, name, names, sections)
            else:
                _export(found, data, name, names, sections)
        except Exception as exc:  # noqa: BLE001 - one file never stops the others; only its class name is kept
            found.refused.append(words.COULD_NOT_READ.format(name=name, cls=type(exc).__name__))
            logger.warning("Compare with Power BI: a file could not be read (%s)", type(exc).__name__)
    return found


def _plain_zip(data: bytes) -> bool:
    """A zip that is not an Excel file (no workbook in it): a zip of exports."""
    if data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, ValueError, OSError):
        return False
    return "xl/workbook.xml" not in names and "[Content_Types].xml" not in names


def _hidden(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return "__MACOSX" in parts[:-1] or parts[-1].startswith(".")


def _archive(found: Upload, data: bytes, name: str, names: frozenset[str], sections: tuple[str, ...]) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        found.refused.append(words.NOT_EXCEL.format(name=name))
        return
    if len(entries) > MAX_ENTRIES:
        found.refused.append(words.TOO_MANY_ENTRIES.format(name=name))
        return
    exports = 0
    for entry in entries:
        if entry.is_dir():
            continue
        if _hidden(entry.filename):
            found.hidden += 1
            continue
        base = file_name(posixpath.basename(entry.filename.replace("\\", "/")), names)
        lower = base.lower()
        if lower.endswith(".zip"):
            found.refused.append(words.NESTED_ZIP.format(name=base))
            continue
        if not lower.endswith(".xlsx"):
            found.refused.append(words.NOT_EXCEL.format(name=base))
            continue
        exports += 1
        if entry.file_size > MAX_FILE_BYTES:  # declared: the entry is never opened
            found.refused.append(words.TOO_BIG.format(name=base))
            continue
        try:
            with archive.open(entry) as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            _export(found, content, base, names, sections)
        except Exception as exc:  # noqa: BLE001 - one entry never stops the others
            found.refused.append(words.COULD_NOT_READ.format(name=base, cls=type(exc).__name__))
            logger.warning("Compare with Power BI: a zip entry could not be read (%s)", type(exc).__name__)
    if not exports:
        found.refused.append(words.NO_EXPORT_IN_ZIP)


def _export(found: Upload, data: bytes, name: str, names: frozenset[str], sections: tuple[str, ...]) -> None:
    """One export's bytes: checked, recognised, then staged or read now."""
    found.seen += 1
    if found.stopped:
        return
    if found.seen > MAX_EXPORTS:
        found.stopped = words.TOO_MANY
        found.refused.append(words.TOO_MANY)
        return
    try:
        book = powerbi_xlsx.Book(data)
    except Unreadable as exc:
        protected = exc.reason == "protected" and not name.lower().endswith(".xls")  # an old .xls is OLE too
        found.refused.append((words.PROTECTED if protected else words.NOT_EXCEL).format(name=name))
        return
    found.unpacked += book.unpacked
    if found.unpacked > MAX_UNPACKED:
        found.stopped = words.TOO_MUCH
        found.refused.append(words.TOO_MUCH)
        return
    try:
        peek = powerbi_xlsx.peek(data, lambda header: px.recognise(header).kind is not None)
    except Unreadable:
        found.refused.append(words.NOT_EXCEL.format(name=name))
        return
    if peek.header is None:
        if peek.filters:
            found.refused.append(words.EMPTY_VISUAL.format(name=name))
        else:
            found.refused.append(_unknown(name, px.Match(None)))
        return
    match = px.recognise(peek.header)
    if match.tied:
        a, b = (" / ".join(k.visual) for k in match.tied)
        found.refused.append(words.AMBIGUOUS.format(name=name, a=a, b=b))
        return
    if match.kind is None:
        found.refused.append(_unknown(name, match))
        return
    prepared = Prepared(match.kind, name, "staged", sheet=peek.sheet, match=match)
    prepared.not_compared, prepared.unknown = match.not_compared, match.unknown
    prepared.dropped = [words.DROPPED.format(caption=c) for c in match.people]
    if match.people_others:
        prepared.dropped.append(words.plural(match.people_others, words.DROPPED_OTHER, words.DROPPED_OTHERS))
    if match.whole and peek.worksheets == 1:
        content = repack(data, peek.sheet)
        if found.staged + len(content) > MAX_STAGED:
            found.refused.append(words.STAGED_TOO_BIG)
            return
        found.staged += len(content)
        prepared.content = content
    elif peek.sheet_bytes <= MAX_READ_NOW:
        prepared.state = "read"
        prepared.read = read(data, match, names, sections, peek.sheet)
    elif match.whole:  # every column kept, but another sheet: its shared strings may hold anything
        found.refused.append(words.TOO_LARGE_SHEETS.format(name=name))
        return
    else:
        columns = [*match.people, *match.not_compared]
        if match.unknown or match.people_others:
            others = match.unknown + match.people_others
            columns.append(words.plural(others, words.NOT_COMPARED_OTHER, words.NOT_COMPARED_OTHERS))
        found.refused.append(words.TOO_LARGE_EXTRA.format(name=name, columns=", ".join(columns)))
        return
    found.exports.append(prepared)


def _unknown(name: str, match: px.Match) -> str:
    """The refusal of a file of no kind: only captions NeuroDB knows are named, the others counted (the first
    row of an unknown file can be data, a list of people)."""
    others = words.plural(match.unknown, words.AND_OTHER, words.AND_OTHERS) if match.unknown else ""
    return words.UNKNOWN.format(
        name=name, columns=", ".join(match.known) if match.known else words.UNKNOWN_NONE, and_others=others
    )
