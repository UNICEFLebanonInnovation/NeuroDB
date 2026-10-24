"""Every sentence the KM Database writes, in one place (its plain words are tested: tests/km/test_words.py).

The page's sentences never name a column of the workbook or one of its sheets. The administrator's upload and
load messages do, because they point at what the person who prepared the workbook sees in Excel: they take
the column (``{column}``) from :mod:`neurodb.km.columns` and the sheet (``{sheet}``) from the uploaded file,
so no sentence here holds a header or a sheet's name. :func:`say` fills them in.
"""

from __future__ import annotations

from django.utils import formats
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy


def say(template, **values) -> str:
    """``template`` translated, with its ``{placeholders}`` filled in. A sentence with a count is written with
    ``ngettext_lazy(one, other, "n")``: the number ``n`` picks its form, and ``{count}`` writes it grouped
    ("14,869")."""
    if isinstance(values.get("n"), int):
        values.setdefault("count", formats.number_format(values["n"], force_grouping=True))
    return template.format(**values)


def counted(n: int, noun) -> str:
    """ "1 finding", "14,869 findings": ``n`` with ``noun`` (``FINDINGS_N``, ``DOCUMENTS_N``, ``ROWS_N``)."""
    return say(noun, n=n)


def quoted(names) -> str:
    """“A”, “B”, “C”: names as a message lists them."""
    return ", ".join(f"“{name}”" for name in names)


# ------------------------------------------------------------------------------------------- counts
FINDINGS_N = ngettext_lazy("{count} finding", "{count} findings", "n")
DOCUMENTS_N = ngettext_lazy("{count} document", "{count} documents", "n")
ROWS_N = ngettext_lazy("{count} row", "{count} rows", "n")
BOTH = _("{first} and {second}")

# ------------------------------------------------------------------------------------------- the page
PAGE_TITLE = _("KM Database")
PURPOSE = _(
    "Findings from research, evaluations and assessments, with the documents they come from. Each finding "
    "is shown as the workbook gives it."
)
PREPARED_LINE = _("Prepared on {prepared} · loaded on {loaded} · {documents} · {findings}")
LOADED_LINE = _("Loaded on {loaded} · {documents} · {findings}")
REVIEW_PART = _(" · {review} marked for review")
LOADING_INFO = _("A new version is loading. This one stays until it is ready.")
FIRST_LOADING_TITLE = _("The KM Database is being loaded for the first time.")
FIRST_LOADING = _("It shows here when it is ready.")
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
NOT_XLSX = _("This is a .{ext} file: open it in Excel and save it as Excel Workbook (.xlsx).")
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
SHOWN_PREPARED = _(
    "The version prepared on {prepared}, loaded on {loaded} (uploaded by {who}): {documents} and {findings}."
)
SHOWN_LOADED = _("The version loaded on {loaded} (uploaded by {who}): {documents} and {findings}.")
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
ROWS_WITHOUT = ngettext_lazy(
    "{count} row of “{sheet}” has no “{column}”: row {rows}.",
    "{count} rows of “{sheet}” have no “{column}”, for example rows {rows}.",
    "n",
)
REPEATED_IDS = ngettext_lazy(
    "{count} value of “{column}” is repeated in “{sheet}”: {examples}.",
    "{count} values of “{column}” are repeated in “{sheet}”, for example {examples}.",
    "n",
)
REPEATED_EXAMPLE = _("{id} (rows {first} and {second})")
ORPHANS = ngettext_lazy(
    "{count} row of “{sheet}” names a “{column}” that is not in “{documents}”: row {rows}.",
    "{count} rows of “{sheet}” name a “{column}” that is not in “{documents}”, for example rows {rows}.",
    "n",
)
BAD_IDS = ngettext_lazy(
    "{count} value of “{column}” in “{sheet}” holds characters an ID may not have (only letters, digits, "
    "“.”, “_” and “-”, at most 40): row {rows}.",
    "{count} values of “{column}” in “{sheet}” hold characters an ID may not have (only letters, digits, "
    "“.”, “_” and “-”, at most 40), for example rows {rows}.",
    "n",
)
TOO_MANY = _("The workbook holds more rows than the KM Database takes (5,000 documents, 100,000 findings).")
FEWER_FINDINGS = ngettext_lazy(
    "This workbook has {count} finding; the version shown now has {m}. Nothing was loaded. If fewer "
    "findings is expected, upload it again and tick “{fewer}”.",
    "This workbook has {count} findings; the version shown now has {m}. Nothing was loaded. If fewer "
    "findings is expected, upload it again and tick “{fewer}”.",
    "n",
)
FEWER_DOCUMENTS = ngettext_lazy(
    "This workbook has {count} document; the version shown now has {m}. Nothing was loaded. If fewer "
    "documents is expected, upload it again and tick “{fewer}”.",
    "This workbook has {count} documents; the version shown now has {m}. Nothing was loaded. If fewer "
    "documents is expected, upload it again and tick “{fewer}”.",
    "n",
)
FEWER_THAN_STATED = _(
    "This workbook's Read Me sheet says it has {stated}, but “{sheet}” has {rows}. Nothing was loaded. If "
    "that is expected, upload it again and tick “{fewer}”."
)
STATED_DIFFERS = _("The workbook's Read Me sheet says it has {stated}; “{sheet}” has {rows}.")
COUNT_DIFFERS = ngettext_lazy(
    "{count} document gives a “{column}” different from its rows in “{sheet}”: {ids}.",
    "{count} documents give a “{column}” different from their rows in “{sheet}”, for example {ids}.",
    "n",
)
FINDINGS_CHECK = ngettext_lazy(
    "{count} finding holds a value to check (shown as given and marked), in {columns}: {ids}. See it on the "
    "page.",
    "{count} findings hold a value to check (shown as given and marked), in {columns}; for example {ids}. "
    "See them all on the page.",
    "n",
)
DOCUMENTS_CHECK = ngettext_lazy(
    "{count} document holds a value to check (shown as given and marked), in {columns}: {ids}. See it on the "
    "page.",
    "{count} documents hold a value to check (shown as given and marked), in {columns}; for example {ids}. "
    "See them all on the page.",
    "n",
)
CHECK_COLUMN = _("“{column}” ({count})")
MARKED_FOR_REVIEW = ngettext_lazy(  # {what}: "41 findings", "1 document", "41 findings and 2 documents"
    "{what} is marked for review in the workbook (“{column}”). See it on the page.",
    "{what} are marked for review in the workbook (“{column}”). See them on the page.",
    "n",
)
NO_FINDINGS = ngettext_lazy(
    "{count} document has no rows in “{sheet}”: {ids}.",
    "{count} documents have no rows in “{sheet}”: {ids}.",
    "n",
)
SEE_ON_PAGE = (_("See them all on the page."), _("See them on the page."), _("See it on the page."))
LOCKED = _("Another load was running. Upload again in a minute.")
STOPPED = _(
    "The load stopped before it finished ({error}). The KM Database still shows the version it showed before."
)
RESTARTED = _("The load stopped before it finished (the server restarted). Upload the workbook again.")
NOT_STARTED = _("The load did not start. Upload the workbook again.")
REMOVED = _("The version was removed while it loaded.")

# ---------------------------------------------------------------------------------------- the version page
FAILED_LIVE = _(
    "Nothing was loaded. The KM Database still shows the version {label}. Correct the workbook and upload it "
    "again."
)
FAILED_EMPTY = _(
    "Nothing was loaded. The KM Database is still empty. Correct the workbook and upload it again."
)
LOADING_PAGE = _("Loading… this page refreshes by itself.")
