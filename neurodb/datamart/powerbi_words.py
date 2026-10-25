"""Every sentence of Compare with Power BI (Admin → Data and sync), in one place, in plain words for the
people who check the figures (tests/web/test_plain_words.py reads them all).

A sentence names a file only by its cleaned base name (``{name}``), a column only by a caption NeuroDB knows
(:mod:`neurodb.datamart.powerbi_exports`), an error only by its class name (``{cls}``): never a cell's value,
an unknown caption or the text of a filter NeuroDB could not read.
"""

from __future__ import annotations

PAGE_TITLE = "Compare with Power BI"
UPLOAD_BUTTON = "Upload and compare"
AGAIN_BUTTON = "Compare again"
FILES_LABEL = "Power BI's exports"
FILES_HELP = "The zip as downloaded, or the Excel files (.xlsx). Several can be chosen at once."
INTRO = (
    "Upload the files you exported from the PRP Power BI report: the zip as downloaded, or the Excel files. "
    "NeuroDB recognises each export by its columns, reads Power BI's filters and keeps the rows it compares. "
    "People's names in the files are dropped as they are read and never kept."
)
RAW_LINK = "Compare Power BI's exports with NeuroDB"
NOT_YET = "NeuroDB read these rows. Comparing them with NeuroDB's pages is not ready yet."

# ------------------------------------------------------------------------------------------- how to export
HOW_STEPS = (
    "On each visual: More options (…) → Export data → Summarized data → .xlsx.",
    "Set Reporting Year to 2026 on every page: NeuroDB holds partner reports from 1 Jan 2026.",
    "Set PD status to active, and clear Section, Partner, Document type, Office, Emergency indicator, "
    "CP Output and Indicator Title.",
    "Upload every file at once, or the zip your browser made.",
    "A filter NeuroDB does not know is listed as one it cannot apply: clear it and export again, or set it "
    "once on one small visual and send that file to the NeuroDB team so NeuroDB can learn it.",
    "A sensitivity label that encrypts the file stops NeuroDB reading it.",
)

# ------------------------------------------------------------------------------- the upload (in the request)
BUSY = "A comparison is running: wait for it to finish (a few minutes)."
BUSY_LINK = "Open it"
OTHER_JOB = "A Datamart sync or a KM Database load is running: upload the files when it ends."
OTHER_JOB_LATER = "A Datamart sync or a KM Database load started: press Compare again when it ends."
TOO_BIG = "{name} is larger than 25 MB: NeuroDB did not read it."
TOO_BIG_UPLOAD = "The files are larger than 50 MB in all: upload fewer at once."
TOO_MUCH = "The files hold more than 400 MB of data: upload fewer at once."
TOO_MANY = "Choose at most 30 exports at once."
TOO_MANY_ENTRIES = "{name} holds more than 200 files: NeuroDB did not open it."
NOT_EXCEL = "{name} is not an Excel file (.xlsx) or a zip of them."
NESTED_ZIP = "{name} is a zip inside the zip: unzip it first."
PROTECTED = "{name} is protected (a password or a sensitivity label): export it again without protection."
UNKNOWN = (
    "NeuroDB does not recognise {name}. Columns it knows: {columns}{and_others}. Upload exports of the PRP "
    "Power BI report."
)
UNKNOWN_NONE = "none"
AND_OTHERS = ", and {n:,} other columns NeuroDB does not know"
AND_OTHER = ", and 1 other column NeuroDB does not know"
AMBIGUOUS = "{name} could be '{a}' or '{b}': export the visual again with all its columns."
EMPTY_VISUAL = "{name} holds Power BI's filters but no columns: Power BI exported the visual empty."
TOO_LARGE_EXTRA = (
    "{name} has columns NeuroDB does not compare ({columns}) and is too large to read at once: remove those "
    "columns in Power BI and export again."
)
TOO_LARGE_SHEETS = (
    "{name} has more than one sheet and is too large to read at once: export the visual again from Power BI."
)
STAGED_TOO_BIG = "The files to compare are too large together: upload fewer at once."
COULD_NOT_READ = "NeuroDB could not read {name} ({cls})."
NO_EXPORT_IN_ZIP = "The zip holds no Excel export."
NO_FILE = "Choose the files to upload."
HIDDEN = "Skipped {n:,} hidden files a Mac adds to a zip."
HIDDEN_ONE = "Skipped 1 hidden file a Mac adds to a zip."
DROPPED = "Dropped as the file was read: {caption} (people's names). It was never stored."
DROPPED_OTHERS = "Dropped as the file was read: {n:,} other columns that name people. They were never stored."
DROPPED_OTHER = "Dropped as the file was read: 1 other column that names people. It was never stored."
NOT_COMPARED_COLUMNS = "Not compared: {columns}."
NOT_COMPARED_OTHERS = "{n:,} other columns NeuroDB does not know"
NOT_COMPARED_OTHER = "1 other column NeuroDB does not know"
NOTHING_KEPT = "Nothing was kept."
STARTED = "The files are being compared. This page shows the result when it is ready."
START_FAILED = "The comparison could not start: upload the files again."
LOCKED = "Another comparison was running: upload the files again."
LOCKED_AGAIN = "Another comparison was running: press Compare again."
STALE = "The comparison stopped (the server restarted): upload the files again."
STALE_AGAIN = "The comparison stopped (the server restarted): press Compare again."
STOPPED = "The comparison stopped: {cls}."
NOT_AGAIN = "These files cannot be compared again: upload them again."

# ------------------------------------------------------------------------------------------- an export read
ROUND_COUNT = "Power BI may have cut this export at {n:,} rows: check the visual shows no more."
UNREADABLE_VALUES = "{n:,} values NeuroDB could not read are left empty."
UNREADABLE_VALUE = "1 value NeuroDB could not read is left empty."
CUT_TITLES = "{n:,} titles end at 255 characters: Power BI cuts longer ones."
CUT_TITLE = "1 title ends at 255 characters: Power BI cuts longer ones."
OTHER_COUNTRY = "NeuroDB holds {country}'s data only."
DAY_OLD = (
    "Power BI refreshed on {day}, more than 14 days ago: statuses are read for today, so Due and Overdue may "
    "differ."
)
SYNC_BEFORE = (
    "NeuroDB's last partner reports sync ran before Power BI's refresh: newer reports may be missing from "
    "NeuroDB."
)
SYNC_MOVED = "A partner reports sync finished during the comparison: compare again for a steady result."
DETAILS_REMOVED = "Details removed (only the last 3 uploads keep them)."

# ---------------------------------------------------------------------------------- the states of an export
STATE_WORDS = {
    "staged": "Waiting to be read",
    "read": "Read",
    "compared": "Compared",
    "empty": "Empty in Power BI",
    "not_compared": "Not compared",
    "failed": "Could not be read",
}
STATE_ACTIONS = {
    "staged": "",
    "read": NOT_YET,
    "compared": "",
    "empty": "Check the visual's filters in Power BI.",
    "not_compared": "Export it again with Country name Lebanon.",
    "failed": "Export it again.",
}
NOT_UPLOADED = "Not uploaded"
NOT_UPLOADED_ACTION = "Export this visual too (see How to export)."

# ------------------------------------------------------------------------------- the filter, clause by clause
APPLIED = "applied"
CLAUSE_STATES = {
    "applied": "NeuroDB applies it",
    "as_read": "NeuroDB applies it as it reads it",
    "from_records": "NeuroDB applies it from the records",
    "by_comparison": "NeuroDB applies it while comparing (its page has no such filter)",
    "cannot": "NeuroDB cannot apply it: compare by hand, or clear that slicer",
    "unknown_field": "A filter NeuroDB does not know (its value is not kept)",
    "unknown_value": "A value NeuroDB does not know (its value is not kept)",
    "unread": "A filter NeuroDB could not read (its text is not kept)",
    "other_country": "Another country: NeuroDB holds one country",
}
# how NeuroDB reads Power BI's own fields when its records do not carry them
AS_READ = {
    "is_period_latest": "the newest row of each report and location",
    "is_blueprint_canonical": "each PD indicator counted once",
}
NOT_IN_RECORDS = "Power BI's field {field} is not in the records NeuroDB reads: NeuroDB reads it as {words}."
COVERS_EVERY_YEAR = "Every year"
COVERS_ONLY = "{sections} only"
NO_FILTER = "Power BI wrote no filters."

# --------------------------------------------------------------------------------------------- the statuses
UPLOAD_STATUS = {
    "queued": "Waiting to start…",
    "reading": "Reading the files…",
    "comparing": "Comparing…",
    "done": "Done.",
    "failed": "Stopped.",
}

# ----------------------------------------------------------------------------------------- what to do now
TODO_NOT_READ = "{n:,} files were not read: see why below, then export them again."
TODO_NOT_READ_ONE = "1 file was not read: see why below, then export it again."
TODO_MISSING = "{n:,} visuals are not uploaded: export them too (see How to export)."
TODO_MISSING_ONE = "1 visual is not uploaded: export it too (see How to export)."
TODO_CANNOT = "Clear these slicers in Power BI and export again, or compare these exports by hand: {clauses}."
TODO_NOTHING = "Nothing to do now: NeuroDB read every export."


def plural(n: int, one: str, many: str) -> str:
    """``one`` for 1, ``many`` (with ``{n}``) for any other count."""
    return one if n == 1 else many.format(n=n)
