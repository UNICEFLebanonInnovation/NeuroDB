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
    "NeuroDB recognises each export by its columns, reads Power BI's filters, compares every row with its "
    "own pages, and tells you what to do about each difference. People's names in the files are dropped as "
    "they are read and never kept."
)
RAW_LINK = "Compare Power BI's exports with NeuroDB"
XLSX_BUTTON = "Download every difference (Excel)"

# ------------------------------------------------------------------------------------------- how to export
# the second step is HOW_YEAR, filled in on the page (powerbi_admin.how_steps): the year and NeuroDB's history
HOW_STEPS = (
    "On each visual: More options (…) → Export data → Summarized data → .xlsx.",
    "Set PD status to active, and clear Section, Partner, Document type, Office, Emergency indicator, "
    "CP Output and Indicator Title.",
    "Upload every file at once, or the zip your browser made.",
    "A filter NeuroDB does not know is listed as one it cannot apply: clear it and export again, or set it "
    "once on one small visual and send that file to the NeuroDB team so NeuroDB can learn it.",
    "A sensitivity label that encrypts the file stops NeuroDB reading it.",
)

HOW_YEAR = (
    "Set Reporting Year to {year} on every page except Partner Fund Monitoring and Funded Programs "
    "(NeuroDB's funds pages have no year: leave it clear there). NeuroDB holds partner reports from {day}."
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
# the job's process ended without a word: the server restarted, or the comparison ran out of memory
STALE = (
    "The comparison stopped before it finished (the server restarted or ran out of memory): upload the files "
    "again."
)
STALE_AGAIN = (
    "The comparison stopped before it finished (the server restarted or ran out of memory): press Compare "
    "again."
)
TRIMMED_QUEUED = (
    "The details of these files were removed meanwhile (only the last 3 uploads keep them): upload them "
    "again."
)
# a comparison that stopped: the step it stopped at, and what stopped it (``failure``: the exception's class,
# a database error's SQLSTATE code with what it means; never a value, a query or the database's own message)
STOPPED = "The comparison stopped {step}: {what}."  # an export's
STOPPED_UPLOAD = (
    "The comparison stopped {step} ({what}): upload the files again."  # an upload's, a copy not read
)
STOPPED_AGAIN = "The comparison stopped {step} ({what}): press Compare again."  # an upload's, every row kept
# the step an export's comparison stopped at
EXPORT_STEPS = {
    "rows": "while reading its rows",
    "page": "at NeuroDB's page",
    "facts": "while reading the facts behind the reasons",
    "matching": "while matching the rows",
    "saving": "while saving its result",
    "lines": "while writing its differences",
}
# the step an upload's comparison stopped at
UPLOAD_STEPS = {
    "starting": "as it started",
    "reading": "while reading the files",
    "day": "while reading the day and the sync",
    "preparing": "while preparing the comparison",
    "comparing": "while comparing {visual}",
    "lines": "while writing the differences",
    "summary": "while writing the summary",
}
NO_CODE = "{cls} with no code"  # a database error without SQLSTATE: the database client's own
AT_MEMORY_LIMIT = "at the job's memory limit"
# what a database error's SQLSTATE code means (PostgreSQL's codes), else its class (the first two characters)
SQLSTATE_WORDS = {
    "22001": "a text too long for its column",
    "22003": "a number out of range",
    "22007": "a date written in a form the database cannot read",
    "22008": "a date out of range",
    "22012": "a division by zero",
    "22021": "a character the database cannot hold",
    "22P02": "a value written in a form the database cannot read",
    "22P05": "a character the database cannot hold",
    "23502": "a value missing",
    "23505": "the same row twice",
    "25006": "a write in a read-only step",
    "40001": "the data changed meanwhile",
    "40P01": "two jobs waited for each other",
    "53100": "the database's disk is full",
    "53200": "the database ran out of memory",
    "57014": "it took too long",
    "72000": "the data changed meanwhile",
}
SQLSTATE_CLASS_WORDS = {
    "08": "the connection to the database was lost",
    "22": "a value the database cannot take",
    "23": "a rule of the database",
    "40": "the data changed meanwhile",
    "42": "a fault in NeuroDB's query",
    "53": "the database ran short of resources",
    "54": "a limit of the database",
    "57": "the database stopped it",
    "58": "an error of the database server",
}
NOT_AGAIN = "These files cannot be compared again: upload them again."

# ------------------------------------------------------------------------------------------- an export read
ROUND_COUNT = "Power BI may have cut this export at {n:,} rows: check the visual shows no more."
UNREADABLE_VALUES = "{n:,} values NeuroDB could not read are left empty."
UNREADABLE_VALUE = "1 value NeuroDB could not read is left empty."
CUT_TITLES = "{n:,} titles end at 255 characters: Power BI cuts longer ones."
CUT_TITLE = "1 title ends at 255 characters: Power BI cuts longer ones."
WITHHELD_VALUES = (
    "{n:,} values of codes or statuses name a person, an e-mail or a phone: they were not kept, so their "
    "rows are compared without them."
)
WITHHELD_VALUE = (
    "1 value of a code or a status names a person, an e-mail or a phone: it was not kept, so its row is "
    "compared without it."
)
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
    "read": "",
    "compared": "",
    "empty": "Check the visual's filters in Power BI.",
    "not_compared": "Export it again with Country name {country}.",  # the country NeuroDB holds
    "failed": "Export it again.",
}
NOT_UPLOADED = "Not uploaded"
NOT_UPLOADED_ACTION = "Export this visual too (see How to export)."
# an export waiting to be read when its upload failed: its copy was emptied, it never will be
NOT_READ_STATE = "Not read"
NOT_READ_ACTION = "Upload the files again."

# ------------------------------------------------------------------------------- the filter, clause by clause
APPLIED = "applied"
CLAUSE_STATES = {
    "applied": "NeuroDB applies it",
    "as_read": "NeuroDB applies it as it reads it",
    # an earlier comparison's: the records carry the field, NeuroDB's pages do not read it
    "from_records": "NeuroDB applies it as it reads it (its pages do not use the field)",
    "by_comparison": "NeuroDB applies it while comparing (its page has no such filter)",
    "cannot": "NeuroDB cannot apply it: compare by hand, or clear that slicer",
    "unknown_field": "A filter NeuroDB does not know (its value is not kept)",
    "unknown_value": "A value NeuroDB does not know (its value is not kept)",
    "unread": "A filter NeuroDB could not read (its text is not kept)",
    "other_country": "Another country: NeuroDB holds one country",
}
# how NeuroDB reads Power BI's own fields (none of its pages reads them, even where the records carry them)
AS_READ = {
    "is_period_latest": "the newest row of each report and location",
    "is_blueprint_canonical": "each PD indicator counted once",
}
NOT_IN_RECORDS = "Power BI's field {field} is not in the records NeuroDB reads: NeuroDB reads it as {words}."
IN_RECORDS = (
    "Power BI's field {field} is in the records NeuroDB reads, but NeuroDB's pages do not use it: NeuroDB "
    "reads it as {words}."
)
COVERS_EVERY_YEAR = "Every year"
COVERS_ONLY = "{sections} only"
NO_FILTER = "Power BI wrote no filters."

# --------------------------------------------------------------------------------------------- the statuses
COMPARING = "Comparing… {n:,} of {of:,} exports"
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
# an export read, whose comparison stopped: nothing to export again
TODO_NOT_COMPARED = "{n:,} exports could not be compared: press Compare again."
TODO_NOT_COMPARED_ONE = "1 export could not be compared: press Compare again."
TODO_EMPTY = (
    "{n:,} exports are empty in Power BI while NeuroDB's page has rows for their filter: check the visuals' "
    "filters in Power BI and export them again."
)
TODO_EMPTY_ONE = (
    "1 export is empty in Power BI while NeuroDB's page has rows for its filter: check the visual's filters "
    "in Power BI and export it again."
)
TODO_OTHER_COUNTRY = "{n:,} exports are of another country: export them again with Country name {country}."
TODO_OTHER_COUNTRY_ONE = "1 export is of another country: export it again with Country name {country}."
TODO_CUT = (
    "{n:,} exports may be cut by Power BI: check in Power BI that each visual shows no more rows; if it "
    "does, export it again one section or one partner at a time."
)
TODO_CUT_ONE = (
    "1 export may be cut by Power BI: check in Power BI that the visual shows no more rows; if it does, "
    "export it again one section or one partner at a time."
)
TODO_MISSING = "{n:,} visuals are not uploaded: export them too (see How to export)."
TODO_MISSING_ONE = "1 visual is not uploaded: export it too (see How to export)."
TODO_CANNOT = "Clear these in Power BI and export again, or compare these exports by hand: {clauses}."
TODO_NOTHING = "Power BI and NeuroDB agree. Nothing to do."
# a filter TODO_CANNOT names: a slicer by its name on the report (comparison.POWERBI_FIELD_SLICERS), another
# field as Power BI's Filters pane shows it, a clause NeuroDB could not read without its text
TODO_SLICER = "the {slicer} slicer"
TODO_PANE = "the {field} filter in the Filters pane"
TODO_UNREAD = "a filter NeuroDB could not read (see the export's page)"
TODO_UNKNOWN_FIELD = "a filter on a field NeuroDB does not know (see the export's page)"
# a field NeuroDB does not know whose name could be a person's: its name is not kept either
FIELD_WITHHELD = "a field NeuroDB does not know"


# ------------------------------------------------------------------------------------- the comparison itself
# Each row shows two values, Power BI's and NeuroDB's (the page's own figure for the same filter). Every
# difference gets the first reason of REASON_ORDER whose test passes, and each reason belongs to one of four
# groups, each with what to do
POWER_BI = "Power BI"
NEURODB = "NeuroDB"
LINE_KINDS = {
    "different": "Different",
    "only_powerbi": "Only in Power BI",
    "only_neurodb": "Only in NeuroDB",
}
GROUP_NAMES = {
    1: "Expected: nothing to fix",
    2: "Wait for the next sync",
    3: "Check the PD in eTools",
    4: "Needs a look",
}
GROUP_ACTIONS = {
    1: "Nothing to fix in NeuroDB.",
    2: "Wait for tonight's sync (20:30), or ask an Administrator to run the eTools Datamart sync, then press "
    "Compare again.",
    3: "Check this PD's status in eTools. If it is active there, wait for the next sync and press Compare "
    "again.",
    4: "Download the Excel file and send it to the NeuroDB team.",
}
# the reasons, in the order they are tried (powerbi_kinds), and the group each belongs to
REASON_ORDER = (
    "before_history",
    "pd_not_running",
    "owed",
    "not_in_read",
    "left_out",
    "older_version",
    "moved_on",
    "rule",
    "status",
    "repeated",
    "see_reports",
    "see_dq_list",
    "cut",
    "list_cut",
    "filter",
    "unreadable",
    "unknown",
)
REASON_GROUPS = {
    "before_history": 1,
    "pd_not_running": 3,
    "owed": 1,
    "not_in_read": 2,  # 4 when NeuroDB's sync ran after Power BI's refresh
    "left_out": 1,
    "older_version": 2,
    "moved_on": 1,
    "rule": 1,
    "status": 1,
    "repeated": 1,
    "see_reports": 4,
    "see_dq_list": 4,
    "cut": 4,
    "list_cut": 4,
    "filter": 4,
    "unreadable": 4,
    "unknown": 4,
}
REASON_WORDS = {
    "before_history": "Not in NeuroDB: before {history}",
    "pd_not_running": "Not in NeuroDB: the PD is not running there",
    "owed": "A report still owed",
    "not_in_read": "Not in NeuroDB: not in the Datamart read",
    "left_out": "NeuroDB's page leaves it out",
    "older_version": "NeuroDB has an older version",
    "moved_on": "Moved on since Power BI's refresh",
    "rule": "A different rule",
    "status": "Status counted differently",
    "repeated": "A repeated value NeuroDB does not add",
    "see_reports": "Upload PD/SPD Report to see why",
    "see_dq_list": "Upload the Data Quality list to see why",
    "cut": "Power BI may have cut its export",
    "list_cut": "Power BI may have cut PD/SPD Report",
    "filter": "A filter NeuroDB cannot apply",
    "unreadable": "A value NeuroDB cannot read",
    "unknown": "Not explained yet",
}
# what to do, beyond the group's sentence
REASON_ACTIONS = {
    "before_history": "NeuroDB's partner reports start on {history}.",
    "owed": "The partner has not sent this report yet.",
    "not_in_read": "Check in PRP that the report exists.",  # when NeuroDB's sync ran after the refresh
    "left_out": "NeuroDB's page uses another rule here.",
    "moved_on": "Power BI shows the report's newer status after its next refresh.",
    "rule": "NeuroDB counts it another way (see Choices for you).",
    "repeated": "NeuroDB never adds a value PRP repeats on every location.",
    "see_reports": "Export PD/SPD Report with the same filters and upload it with this one.",
    "see_dq_list": (
        "Export Indicators without Reached or with Zero Reached with the same filters and upload it too."
    ),
    "cut": (
        "Check in Power BI that the visual shows no more rows. If it does, export it again one section or "
        "one partner at a time and upload each file."
    ),
    # a counted visual's gap whose reports may be beyond the report list's cut: the list is exported again
    "list_cut": (
        "Export PD/SPD Report again one section or one partner at a time and upload the files with this one."
    ),
    "filter": "Or clear that slicer in Power BI and export again.",
    "unreadable": "Its values are left empty: check them in eTools or PRP.",
}
# not in the Datamart read, where NeuroDB holds no such PD: the PD is checked in eTools (a report in PRP)
NOT_IN_READ_PD = "Check in eTools that this PD exists."
# a different rule no choice is offered for (Choices for you would show nothing about it)
RULE_NO_CHOICE = "NeuroDB counts it another way."
COUNTED_NO_CHOICE = "Nothing to fix: NeuroDB counts these another way."
# the reasons whose own step replaces their group's (nothing to send to the NeuroDB team: export again)
OWN_ACTION = frozenset({"cut", "list_cut"})
# the details a reason gives
DETAIL_BEFORE_HISTORY = "The Datamart holds no earlier partner report."
DETAIL_NOT_RUNNING = "NeuroDB has this PD as {status}."
DETAIL_NO_PD = "NeuroDB holds no PD with this number."
DETAIL_OWED = "NeuroDB's partner reports feed does not carry reports still owed (check 11)."
DETAIL_SYNC_BEFORE = "NeuroDB's last sync ran before Power BI's refresh."
DETAIL_SYNC_AFTER = "The Datamart did not send this row."
DETAIL_SYNC_UNKNOWN = "NeuroDB cannot tell whether its last sync ran before Power BI's refresh."
DETAIL_NONE_ACCEPTED = "NeuroDB has this indicator, but none of its reports is Accepted there."
DETAIL_SECTION = "NeuroDB's page reads the section of the indicator itself, not the PD's sections."
DETAIL_LIMIT = "NeuroDB's page reads at most {limit:,} report rows at once."
DETAIL_OLDER_RECORD = "NeuroDB holds {n:,} records of this row; the page used one that is not the newest."
DETAIL_NEWEST_RECORD = "NeuroDB holds {n:,} records of this row; Power BI's value is the newest one's."
DETAIL_EARLIER_STEP = "NeuroDB still has this report as {status}; Power BI has it as {powerbi}."
DETAIL_MOVED_ON = "NeuroDB's sync ran after Power BI's refresh: the report moved on since."
DETAIL_MAY_HAVE_MOVED_ON = "The report may have moved on between Power BI's refresh and NeuroDB's sync."
DETAIL_DUE_COVERS = "Power BI's Due also covers reports not yet due."
DETAIL_OTHER_DAY = "Statuses are read for a different day."
DETAIL_DUE_DAY = (
    "Power BI counts a report as Overdue on its due date; NeuroDB counts it as Due until the day after."
)
DETAIL_OWED_WORDS = "Power BI and NeuroDB read this report's status differently."
DETAIL_NEWEST_WORD = "NeuroDB takes the newest word across a report's rows."
DETAIL_TWICE = "Power BI lists this report twice, with two statuses."
DETAIL_REPEATED = (
    "PRP repeats the report's total on every location: NeuroDB gives this location no value of its own."
)
DETAIL_PARTNER = "Partner written differently."
# a row NeuroDB could not compare: a value beyond any number it can hold (eTools and PRP keep amounts and
# values as text), or one it cannot compute with
DETAIL_UNREADABLE = "A value of this row is too large, or not a number NeuroDB can compute with."
DETAIL_AMENDMENT = "Amendment number differs."
# each value with its side: NeuroDB's records counted Power BI's way, then NeuroDB's own rule
DETAIL_ALTERNATIVE = "Counted Power BI's way ({words}): {alt}. NeuroDB's own rule gives {value}."
DETAIL_ALSO_FITS = "Also fits: {words}."
DETAIL_ALSO_FITS_COUNT = "Also fits {n:,} other rules: see the fit table."
DETAIL_ALSO_FITS_ONE = "Also fits 1 other rule: see the fit table."
DETAIL_CLAUSE = "Power BI's filter: {clause}."
DETAIL_UNLABELLED = "NeuroDB has not read this value's name yet."
DETAIL_FROM_MONITORING = "Reached: from Partner monitoring."
DETAIL_COPIES = "Power BI's copies of one location row differ in {columns}."
DETAIL_GAP = "Of the {n:,} reports behind this gap: {parts}."
DETAIL_GAP_PART = {
    "owed": "{n:,} are reports still owed",
    "not_in_read": "{n:,} were not in the Datamart read",
    "status": "{n:,} are counted under another status",
    "before_history": "{n:,} are before NeuroDB's history",
    "pd_not_running": "{n:,} are of PDs NeuroDB has as not running",
    "older_version": "{n:,} are older versions in NeuroDB",
    "moved_on": "{n:,} moved on since Power BI's refresh",
    "left_out": "{n:,} are left out by NeuroDB's page",
    "rule": "{n:,} are counted another way",
    "filter": "{n:,} are under a filter NeuroDB cannot apply",
    "unreadable": "{n:,} hold a value NeuroDB cannot read",
    "unknown": "{n:,} are not explained yet",
}
DETAIL_GAP_ONE = "Of the 1 report behind this gap: {parts}."
DETAIL_GAP_PART_ONE = {
    "owed": "1 is a report still owed",
    "not_in_read": "1 was not in the Datamart read",
    "status": "1 is counted under another status",
    "before_history": "1 is before NeuroDB's history",
    "pd_not_running": "1 is of a PD NeuroDB has as not running",
    "older_version": "1 is an older version in NeuroDB",
    "moved_on": "1 moved on since Power BI's refresh",
    "left_out": "1 is left out by NeuroDB's page",
    "rule": "1 is counted another way",
    "filter": "1 is under a filter NeuroDB cannot apply",
    "unreadable": "1 holds a value NeuroDB cannot read",
    "unknown": "1 is not explained yet",
}
DETAIL_LIST_CUT = (
    "Power BI's PD/SPD Report list holds exactly {n:,} rows: the reports behind this gap may be beyond its "
    "cut."
)
DETAIL_NO_GAP_LINES = (
    "The report list shows no report behind this gap: Power BI's chart and its own list differ."
)
DETAIL_DQ_GAP = "Of the {n:,} indicators behind this gap: {parts}."
DETAIL_DQ_GAP_ONE = "Of the 1 indicator behind this gap: {parts}."
# the rules a "different rule" names (each also a choice the user may make, see CHOICES)
RULE_WORDS = {
    "reached": "Reached: Power BI {words}; NeuroDB adds Accepted and Submitted reports",
    "target": "Target: Power BI {words}; NeuroDB shows the target in eTools",
    "progress_reports": "Power BI counts progress reports; this NeuroDB page counts indicator reports",
    "emergency": (
        "Power BI's Emergency indicator is Yes for an SPD; NeuroDB's is the indicator's high frequency"
    ),
    "section": "Power BI's Section is the PD's sections; NeuroDB's is the indicator's",
    "cp_output": "Power BI's CP Output is every CP output of the PD; NeuroDB's is the indicator's",
    "joined": "Power BI joins rows that are the same in every column it shows",
    "partial": "NeuroDB takes PRP's cumulative: the PD started before {history}",
    "monthly": "NeuroDB reads the monthly (HR) reports: the indicator has no quarterly one",
    "units": "Power BI shows this percentage indicator as a fraction on this visual",
    "no_report_in_year": "Power BI lists an indicator once a report of the year names it",
    "draft_values": "Power BI counts values in reports not yet submitted",
    "per_row": "Power BI lists an indicator when any one of its report rows has no value or 0",
    "month": "Power BI places Accepted reports in a month by another date",
    "combined": "NeuroDB's page shows this value only combined with another; added up, it is the same",
    "fr_only": "NeuroDB's page lists PDs with a funds reservation only",
    "partner_name": "Power BI and NeuroDB write this partner's name differently",
    "contribution": (
        "Financial contribution: Power BI {words}; NeuroDB takes PRP's on the latest Accepted or Submitted "
        "report"
    ),
    "no_column": "NeuroDB's page has no such column",
    "several_records": (
        "NeuroDB's page reads the largest value of a location's records; Power BI shows the newest record"
    ),
}
# Power BI's possible rules NeuroDB counts from its records, where Power BI's own rule is not known
CANDIDATE_WORDS = {
    "qpr_sum": "adds up the Accepted quarterly reports (0 when the indicator has monthly reports only)",
    "every_sum": "adds up every Accepted report, quarterly and monthly",
    "latest": "shows the latest Accepted report's own value (quarterly or monthly)",
    "prp_cumulative": "shows PRP's cumulative on the latest Accepted report",
    "accepted": "adds up the Accepted reports as NeuroDB combines them",
    "etools": "shows the target in eTools",
    "prp_sum": "adds up PRP's target over the locations of the latest Accepted report",
    "prp_one": "shows PRP's target of one location of the latest Accepted report",
    # Data Quality: how Power BI may read its filter
    "dq_neurodb": "NeuroDB's reading: the indicator's own section, Accepted and Submitted reports, its total",
    "dq_pd_section": "the PD's sections instead of the indicator's",
    "dq_needs_row": "only indicators with a report of the year, whatever its status",
    "dq_every_status": "values of every report status, not yet submitted ones too",
    "dq_per_row": "an indicator with any one report row without a value or 0",
    "dq_all": "all four of these at once",
    # Partner Fund Monitoring's financial contribution, Funded Programs' Outstanding DCT
    "fc_latest_accepted": "PRP's contribution to date on the latest Accepted report",
    "fc_latest_counted": "PRP's contribution to date on the latest Accepted or Submitted report (NeuroDB's)",
    "fc_sum": "PRP's contributions added up over the reports",
    "dct_fr": "the funds reservations' outstanding amounts added up",
    "dct_pd": "the PD's own total outstanding in eTools",
}
# the same rules, short, where a line names the others that fit too (its detail is at most 300 characters)
CANDIDATE_SHORT = {
    "qpr_sum": "the Accepted quarterly reports added up",
    "every_sum": "every Accepted report added up",
    "latest": "the latest Accepted report",
    "prp_cumulative": "PRP's cumulative",
    "accepted": "NeuroDB's Accepted-only figure",
    "etools": "eTools' target",
    "prp_sum": "PRP's targets added up",
    "prp_one": "one location's PRP target",
}
FIT_TITLES = {
    "reached": "Reached: which rule fits Power BI",
    "target": "Target: which rule fits Power BI",
    "dq": "Data Quality: which reading fits Power BI's list",
    "financial_contribution": "Financial contribution: which rule fits Power BI",
    "outstanding_dct": "Outstanding DCT: which rule fits Power BI (NeuroDB's page has no such column)",
}
FIT_LINE = "fits {fits:,} of {of:,} rows, {above:,} of them with Power BI above 0"
FIT_SET_LINE = "agrees on {fits:,} of {of:,} indicators"
FIT_CHOSEN = "fits best"
FIT_SET_ASIDE = "Rows set aside (history before {history}): {n:,}"
REACHED_SENTENCE = (
    "Power BI's Reached matches '{words}' on {fits:,} of {of:,} rows NeuroDB can check. NeuroDB adds "
    "Accepted and Submitted reports. This is a choice of rule, not missing data."
)
REACHED_NONE = "No rule NeuroDB knows fits Power BI's Reached on the rows NeuroDB can check."
# the rule that fits best fits fewer than half the rows where Power BI shows a value: no choice is offered
REACHED_WEAK = (
    "The rule that fits Power BI's Reached best, '{words}', fits only {fits:,} of the {of:,} rows where "
    "Power BI shows a value: no rule NeuroDB knows explains most of them."
)
# notes an export carries once (a rule that applies to every row produces no line)
NOTE_SET_ASIDE = (
    "{n:,} rows are about periods before {history}: NeuroDB holds none, so they were not compared."
)
NOTE_SET_ASIDE_ONE = "1 row is about a period before {history}: NeuroDB holds none, so it was not compared."
# NeuroDB's own rows of those periods, kept from an earlier sync: set aside the same way
NOTE_SET_ASIDE_NEURODB = (
    "NeuroDB still holds {n:,} rows of periods before {history} from an earlier sync: they were not compared "
    "either."
)
NOTE_SET_ASIDE_NEURODB_ONE = (
    "NeuroDB still holds 1 row of a period before {history} from an earlier sync: it was not compared either."
)
NOTE_ZERO = "Power BI shows no value as 0: a 0 against no value in NeuroDB is the same."
NOTE_RULE = "Stated once for every row: {words}."
NOTE_JOINED_SECTIONS = "{n:,} Power BI rows join the same sections written differently."
NOTE_SAME_UNIT = "Same unit as Power BI: progress reports. NeuroDB's own table counts indicator reports."
NOTE_TOTAL_GAP = "Power BI's Total differs from the sum of its own rows in {columns}."
NOTE_TOTAL_NEURODB = "Power BI's Total differs from NeuroDB's in {columns}: see the sections' lines."
NOTE_NO_STATUS_COLUMN = (
    "Power BI's export has no {columns} column (Power BI writes a status only when its data has it): "
    "NeuroDB's reports in that status are compared against 0."
)
NOTE_MONTH_TOTALS = "Accepted reports add up to the same total over the year: only their months differ."
NOTE_CHART_LIST_GAP = "Power BI's own chart and its report list differ: {columns}."
NOTE_COPIES = (
    "Copies of another location row (Power BI repeats it once per CP output of the PD), joined: {n:,}."
)
NOTE_SHARED_TITLE = (
    "Two indicators of a PD share a title: Power BI's export has no PD Output to tell them apart."
)
NOTE_SHARED_LABEL = "Two disaggregations share a value's name: their values are added together on both sides."
NOTE_CROSS_TAB = "Some values are only in NeuroDB's combined table; they were added up to compare."
NOTE_LINES_CUT = "Differences counted but not listed: {n:,}."
NOTE_ON_SCREEN = "The page lists the first {n:,} lines; the Excel file holds them all (up to its own limit)."
NOTE_OTHER_YEARS = "Power BI's filter names several years: NeuroDB reads each and adds them."
NOTE_ACCEPTED_ONLY = (
    "Power BI keeps the indicators with an Accepted report (its filter says so): NeuroDB keeps the same ones "
    "while comparing."
)
NOTE_PAGE = "Compared with NeuroDB's {page}."
NOTE_SAME_ZERO = "Rows that are 0 on one side and have no value on the other, so the same: {n:,}."
NOTE_FOLLOWS = "% columns follow Reached and Target: compared only where both are the same."
NOTE_DQ_CARD = "The card counts the rows of the Data Quality list."
NOTE_DCT = "NeuroDB's page has no Outstanding DCT column: see the fit table."
NOTE_CUT = "Power BI cuts titles at 255 characters: a cut title matches NeuroDB's title that starts with it."
NEURODB_PAGES = {
    "program_performance": "Partner monitoring → Program Performance",
    "partner_cards": "Progress reports → Partners",
    "follow_up": "Progress reports → Partners: reports to follow up",
    "by_sector": "Progress reports: progress reports by section",
    "by_month": "Progress reports: progress reports by month",
    "dq_card": "Partner monitoring → Data quality",
    "dq_list": "Partner monitoring → Data quality: without or with zero Reached",
    "pd_indicators": "Programmes → Indicators",
    "report_list": "Progress reports: the reports' list",
    "locations": "Progress reports → By location",
    "disaggregations": "Progress reports → By disaggregation",
    "fund_monitoring": "Funds → Partner Fund Monitoring",
    "funded_programmes": "Funds → Funded Programs",
}

# ------------------------------------------------------------------------------------------------ verdicts
VERDICT_WORDS = {
    "same": "Same",
    "counted": "Same data, counted another way",
    "owed": "Same, apart from {parts}",
    "wait": "Wait for the next sync: {n:,}",
    "check": "Check {n:,} PDs in eTools",
    "check_one": "Check 1 PD in eTools",
    "look": "Needs a look: {n:,}",
    "cut": "Power BI may have cut this export at {n:,} rows",
    "list_cut": "Power BI may have cut PD/SPD Report: {n:,}",
    "empty": "Empty in Power BI: NeuroDB's page has {n:,} rows for this filter",
    "empty_one": "Empty in Power BI: NeuroDB's page has 1 row for this filter",
    "empty_none": "Empty in Power BI: NeuroDB's page has none either",
    "not_uploaded": NOT_UPLOADED,
    "other_country": "Not compared: another country",
    "not_read": "Not compared: could not be read",
    "failed": "Could not be compared {step}: {what}",
    "failed_before": "Could not be compared: {cls}",  # an export that failed before its step was kept
}
VERDICT_ACTIONS = {
    "same": "Nothing.",
    "counted": "Nothing to fix. See Choices for you if NeuroDB should count Power BI's way.",
    "owed": "Nothing to fix.",
    "wait": GROUP_ACTIONS[2],
    "check": GROUP_ACTIONS[3],
    "check_one": GROUP_ACTIONS[3],
    "look": GROUP_ACTIONS[4],
    "cut": REASON_ACTIONS["cut"],
    "list_cut": REASON_ACTIONS["list_cut"],
    "empty": "Check the visual's filters in Power BI.",
    "empty_one": "Check the visual's filters in Power BI.",
    "empty_none": "Check the visual's filters in Power BI.",
    "not_uploaded": NOT_UPLOADED_ACTION,
    "other_country": "Export it again with Country name {country}.",  # the country NeuroDB holds
    "not_read": "Export it again.",
    "failed": "Press Compare again. If it fails again, tell the NeuroDB team which export it was.",
}
# a trimmed upload keeps its verdicts, not its lines: no Excel file to send
TRIMMED_ACTION = "Upload the files again to see these differences."

VERDICT_COUNTED_ALSO = "{verdict}; also {parts}"
# the expected rows NeuroDB cannot hold, in an export's verdict, and what they need
VERDICT_APART = {
    "owed": "reports still owed: {n:,}",
    "before_history": "rows before NeuroDB's history: {n:,}",
    "moved_on": "reports that moved on since Power BI's refresh: {n:,}",
}
VERDICT_APART_ACTIONS = {
    "owed": "The partners have not sent these reports yet.",
    "before_history": "NeuroDB holds no partner report before its history.",
    "moved_on": "Power BI shows their newer status after its next refresh.",
}

# ----------------------------------------------------------------------------------- what to do now (done)
# these count differences (a counted visual's row can differ in several columns, each a difference)
TODO_LOOK = "Needs a look: {n:,} differences."
TODO_LOOK_ONE = "Needs a look: 1 difference."
TODO_UNLISTED = "{n:,} of them are counted but not listed (too many differences at once)."
TODO_UNLISTED_ONE = "1 of them is counted but not listed (too many differences at once)."
TODO_CHECK = "Check {n:,} PDs in eTools: NeuroDB has them as not running."
TODO_CHECK_ONE = "Check 1 PD in eTools: NeuroDB has it as not running."
TODO_WAIT = "Wait for the next sync: {n:,} differences. " + GROUP_ACTIONS[2]
TODO_WAIT_ONE = "Wait for the next sync: 1 difference. " + GROUP_ACTIONS[2]
TODO_EXPECTED = "Expected, nothing to fix: {n:,} differences ({parts})."
TODO_EXPECTED_ONE = "Expected, nothing to fix: 1 difference ({parts})."
TODO_EXPECTED_PART = {
    "before_history": "before NeuroDB's history {n:,}",
    "owed": "reports still owed {n:,}",
    "moved_on": "moved on since Power BI's refresh {n:,}",
    "left_out": "left out by NeuroDB's page {n:,}",
    "rule": "counted another way {n:,}",
    "status": "statuses counted differently {n:,}",
    "repeated": "repeated values {n:,}",
}
TODO_SEE = "See the lines"

# ----------------------------------------------------------------------------------------- choices for you
CHOICES_TITLE = "Choices for you"
CHOICES_INTRO = (
    "NeuroDB keeps its own rules until you decide. Each choice below shows what the comparison found; tell "
    "the NeuroDB team if NeuroDB should count Power BI's way."
)
CHOICE_QUESTIONS = {
    "reached": "Should NeuroDB's Program Performance show Reached Power BI's way?",
    "progress_reports": "Should NeuroDB's Reporting Progress count progress reports, as Power BI does?",
    "due": "Should NeuroDB's follow-up list include reports not yet due, as Power BI does?",
    "emergency": "Should NeuroDB's Emergency indicator mean 'the PD is an SPD', as Power BI's does?",
}
CHOICE_EVIDENCE = {
    "reached": REACHED_SENTENCE,
    "progress_reports": (
        "Reporting Progress: counted as progress reports, NeuroDB's records give Power BI's figure on "
        "{fits:,} of {of:,} rows; NeuroDB's tables count indicator reports."
    ),
    "due": "Partners require follow up: {n:,} of Power BI's 'Due' rows are not yet due in NeuroDB.",
    "emergency": (
        "Program Performance: Power BI's Emergency indicator is 'the PD is an SPD' on {fits:,} of {of:,} "
        "rows."
    ),
}
CHOICE_DEFAULTS = {
    "reached": "Until then NeuroDB adds Accepted and Submitted reports.",
    "progress_reports": "Until then NeuroDB's tables count indicator reports.",
    "due": "Until then NeuroDB's follow-up list leaves out reports not yet due.",
    "emergency": "Until then NeuroDB's Emergency indicator is the indicator's high frequency.",
}

# ----------------------------------------------------------------------------------------------- the pages
EXPORT_COUNTS = (
    "Rows compared: {rows:,}. The same: {same:,}. Different: {different:,}. "
    "Only in Power BI: {only_powerbi:,}. Only in NeuroDB: {only_neurodb:,}."
)
EXPORT_SET_ASIDE = "Set aside: {n:,}"
EXPORT_EMPTY = "Power BI exported this visual empty. NeuroDB's page has {n:,} rows for the same filter."
CHECK_IN_NEURODB = "Check in NeuroDB"
ALTERNATIVE = "Counted Power BI's way: {value}"
ANOTHER_WAY = "counts it another way"
LINK_WITHHELD = "Not written: the filter names a person or a contact. Open it from the page."
NO_LINES = "No differences."
ALL_REASONS = "Every reason"
ALL_KINDS = "Every kind"
SHOW = "Show"
EXPECTED_COLLAPSED = "Expected, nothing to fix"
XLSX_SUMMARY = "Summary"
XLSX_MORE = "Differences counted but not listed here: {n:,}."
XLSX_BUSY = "Another large download is being prepared: try again in a minute."
XLSX_NONE = "This upload holds no differences to download."


def plural(n: int, one: str, many: str) -> str:
    """``one`` for 1, ``many`` (with ``{n}``) for any other count."""
    return one if n == 1 else many.format(n=n)


def either(n: int, one: str, many: str) -> str:
    """``one`` for 1, else ``many``, not yet filled in (for a sentence with other values than ``{n}``)."""
    return one if n == 1 else many


def failure(found: dict) -> str:
    """What stopped a comparison, in words (``found``: ``powerbi_compare.failure``'s): the exception's class;
    a database error's SQLSTATE code and what it means ("DataError 22012, a division by zero"); a database
    error without a code (the database client's own), and whether the job was at its memory limit then."""
    cls = found.get("cls") or "Error"
    if "code" not in found:
        return cls
    code = found["code"]
    if code:
        meaning = SQLSTATE_WORDS.get(code) or SQLSTATE_CLASS_WORDS.get(code[:2], "")
        return f"{cls} {code}, {meaning}" if meaning else f"{cls} {code}"
    text = NO_CODE.format(cls=cls)
    return f"{text}, {AT_MEMORY_LIMIT}" if found.get("memory") else text
