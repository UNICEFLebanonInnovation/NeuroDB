"""Every sentence the KM Database writes, in one place (its plain words are tested: tests/km/test_words.py).

The page's sentences never name a column of the workbook or one of its sheets. The administrator's upload and
load messages do, because they point at what the person who prepared the workbook sees in Excel: they take
the column (``{column}``) from :mod:`neurodb.km.columns` and the sheet (``{sheet}``) from the uploaded file,
so no sentence here holds a header or a sheet's name. :func:`say` fills them in.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _


def say(template, **values) -> str:
    """``template`` translated, with its ``{placeholders}`` filled in."""
    return str(template).format(**values)


def quoted(names) -> str:
    """“A”, “B”, “C”: names as a message lists them."""
    return ", ".join(f"“{name}”" for name in names)


# ------------------------------------------------------------------------------------------- the page
PAGE_TITLE = _("KM Database")
PURPOSE = _(
    "Findings from research, evaluations and assessments, with the documents they come from. Each finding "
    "is shown as the workbook gives it."
)
PREPARED_LINE = _("Prepared on {prepared} · loaded on {loaded} · {documents} documents · {findings} findings")
LOADED_LINE = _("Loaded on {loaded} · {documents} documents · {findings} findings")
REVIEW_PART = _(" · {review} marked for review")
LOADING_INFO = _("A new version is loading. This one stays until it is ready.")
EMPTY_TITLE = _("The KM Database is empty.")
EMPTY_SETUP = _("Upload the workbook in admin → KM Database → Versions.")
EMPTY_OTHERS = _("An administrator uploads it.")
UPLOAD_ACTION = _("Upload a new version")
TAB_FINDINGS = _("Findings")
TAB_DOCUMENTS = _("Documents")
REVIEW_PILL = _("Marked for review")
CHECK_PILL = _("Check this value")
CHECK_TITLE = _("This value is not one of the expected ones; it is shown as the workbook has it.")
NOT_GIVEN = _("Not given")
CONFIDENCE_NEUTRAL = _("Levels A to D, as the workbook grades the evidence.")
CONFIDENCE_STRONGEST = _(
    "Level A: the strongest evidence, then Level B and Level C; Level D: the weakest, as the workbook "
    "grades it."
)
REVIEW_SHOWN = _("Showing the findings and documents marked for review or with a value to check.")
SHOW_ALL = _("Show all")

# ------------------------------------------------------------------------- the upload (admin, in the request)
TOO_BIG = _("This file is {size} MB; the upload takes at most 25 MB.")
NOT_XLSX = _("This is a {ext} file: open it in Excel and save it as Excel Workbook (.xlsx).")
NO_EXTENSION = _("This file has no extension: open it in Excel and save it as Excel Workbook (.xlsx).")
PROTECTED = _(
    "This workbook is protected (a password or a sensitivity label that encrypts it). Save a copy without "
    "the protection, then upload that copy."
)
NOT_EXCEL = _("This file is not an Excel workbook NeuroDB can open.")
NO_SHEET = _("The workbook has no sheet whose name ends with “{last}”.")
TWO_SHEETS = _("Two sheets end with “{last}”: keep one.")
SHEETS_ARE = _("Its sheets are: {sheets}.")
MISSING_COLUMNS = _("The sheet “{sheet}” has no column {columns}.")
UNRECOGNISED = _("Columns in that sheet that NeuroDB does not recognise: {headers}.")
HEADER_ROW = _("The column names must be in the first row of the sheet “{sheet}”; they are in row {row}.")
REPEATED_COLUMN = _("The column “{column}” is twice in the sheet “{sheet}”.")
NO_ROWS = _("The sheet “{sheet}” has no rows under its header.")
MISSING_OPTIONAL = _("The sheet “{sheet}” has no column “{column}”: it is shown empty.")
NOT_READ = _("Columns of the sheet “{sheet}” that are not read: {headers}.")
NO_READ_ME = _("The prepared-on date is not known: the workbook has no Read Me sheet.")

BUSY = _("A version is loading now. Wait until it finishes (about a minute), then upload again.")
BUSY_LINK = _("See the version that is loading")
NOTHING_LOADED = _("Nothing was loaded. The KM Database still shows the version {label}.")
NOTHING_LOADED_EMPTY = _("Nothing was loaded. The KM Database is still empty.")
SAME_FILE = _("This file is the version shown now. Tick “{again}” to load it again.")
STARTED = _("The workbook is being loaded. This page shows when it is ready.")
UPLOAD_LABEL = _("Workbook (.xlsx)")
AGAIN_LABEL = _("Load it even if it is the version shown now")
FEWER_LABEL = _("It has fewer findings on purpose")
TEMPLATE_LINK = _("Download the Excel template to see every column the check expects.")
CHECK_ASKS = (
    _("Two sheets, one whose name ends with “Documents” and one whose name ends with “Findings”."),
    _("The column names in the first row of each sheet (the Excel template has them all)."),
    _("A {finding_id} for every finding and a {record_id} for every document, each used once."),
    _("Every finding's {record_id} is in the documents sheet."),
)

# --------------------------------------------------------------------------------- the load (background job)
ROWS_WITHOUT = _("{n} rows of “{sheet}” have no “{column}”, for example rows {rows}.")
REPEATED_IDS = _("{n} values of “{column}” are repeated in “{sheet}”, for example {examples}.")
REPEATED_EXAMPLE = _("{id} (rows {first} and {second})")
ORPHANS = _("{n} rows of “{sheet}” name a “{column}” that is not in “{documents}”, for example rows {rows}.")
BAD_IDS = _(
    "{n} values of “{column}” in “{sheet}” hold characters an ID may not have (only letters, digits, “.”, "
    "“_” and “-”, at most 40), for example rows {rows}."
)
TOO_MANY = _("The workbook holds more rows than the KM Database takes (5,000 documents, 100,000 findings).")
FEWER_FINDINGS = _(
    "This workbook has {n} findings; the version shown now has {m}. Nothing was loaded. If fewer findings is "
    "expected, upload it again and tick “{fewer}”."
)
FEWER_DOCUMENTS = _(
    "This workbook has {n} documents; the version shown now has {m}. Nothing was loaded. If fewer documents "
    "is expected, upload it again and tick “{fewer}”."
)
FEWER_THAN_STATED = _(
    "This workbook's Read Me sheet says it has {m} {what}, but “{sheet}” has {n} rows. Nothing was loaded. "
    "If that is expected, upload it again and tick “{fewer}”."
)
FINDINGS_WORD = _("findings")
DOCUMENTS_WORD = _("documents")
STATED_DIFFERS = _("The workbook's Read Me sheet says it has {m} {what}; “{sheet}” has {n} rows.")
COUNT_DIFFERS = _(
    "{n} documents give a “{column}” different from their rows in “{sheet}”, for example {ids}."
)
FINDINGS_CHECK = _(
    "{n} findings hold a value to check (shown as given and marked), in {columns}; for example {ids}. See "
    "them all on the page."
)
DOCUMENTS_CHECK = _(
    "{n} documents hold a value to check (shown as given and marked), in {columns}; for example {ids}. See "
    "them all on the page."
)
CHECK_COLUMN = _("“{column}” ({n})")
MARKED_FOR_REVIEW = _(
    "{n} findings and {m} documents are marked for review in the workbook (“{column}”). See them on the page."
)
NO_FINDINGS = _("{n} documents have no rows in “{sheet}”: {ids}.")
SEE_ON_PAGE = (_("See them all on the page."), _("See them on the page."))
LOCKED = _("Another load was running. Upload again in a minute.")
STOPPED = _(
    "The load stopped before it finished ({error}). The KM Database still shows the version it showed before."
)
RESTARTED = _("The load stopped before it finished (the server restarted). Upload the workbook again.")
NOT_STARTED = _("The load did not start. Upload the workbook again.")
REMOVED = _("The version was removed while it loaded.")

# ---------------------------------------------------------------------------------------- the version page
FAILED_LIVE = _(
    "Nothing was loaded. The KM Database still shows the version {label} (loaded on {date}). Correct the "
    "workbook and upload it again."
)
FAILED_EMPTY = _(
    "Nothing was loaded. The KM Database is still empty. Correct the workbook and upload it again."
)
LOADING_PAGE = _("Loading… this page refreshes by itself.")
