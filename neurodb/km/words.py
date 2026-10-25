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
    "Findings from research, evaluations and assessments, with the documents they come from. Search, "
    "filter, open a finding or download what you filtered."
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

# ------------------------------------------------------------------------------- searching and filtering
SEARCH_LABEL = _("Search")
SEARCH_PLACEHOLDER = _("Words, a phrase in quotes, or an ID")
MORE_FILTERS = _("More filters")
HOW_TO_READ = _(
    "Several choices in one filter show findings that match any of them; several filters together show "
    "findings that match all of them. Age groups and themes count a finding when the workbook marks it Yes. "
    "Values marked “Check this value” are shown as the workbook has them."
)
LETTERS_FOUND = _("No exact word matches; showing findings that contain the letters you typed.")
LETTERS_FOUND_DOCUMENTS = _("No exact word matches; showing documents that contain the letters you typed.")
QUESTIONS_DROPPED = _("The question filter was left out: the questions you picked are in another pillar.")
OF_ALL = _("of {total}")
NO_FINDINGS_MATCH = _("No findings match. Try fewer words or clear a filter.")
NO_DOCUMENTS_MATCH = _("No documents match. Try fewer words or clear a filter.")
CLEAR_FILTERS = _("Clear filters")
DOCUMENT_CHIP = _("Document: {title}")
DOCUMENT_CHIP_REMOVE = _("Show the findings of every document")
PER_PAGE_LABEL = _("Rows a page")
ORIGIN_LABELS = {
    "P": _("Collected by the document itself"),
    "O": _("From other sources or the authors' own analysis"),
    "none": _("Not given"),
}
QUALITY_LABELS = {
    "": _("All"),
    "ready": _("Ready"),
    "review": _("Marked for review or with a value to check"),
}
EXCEL_OF_FILTER = _("Excel of the filter")
CSV_OF_FILTER = _("CSV of the filter")
OPEN = _("Open")

# --------------------------------------------------------------------------- a finding and its document
FINDING_TITLE = _("Finding · KM Database")
DOCUMENT_TITLE = _("Document · KM Database")
EMPTY_LEFT_OUT = _("Fields the workbook leaves empty are not shown.")
OLDER_VERSION = _("This answer was written from the version {label}; this finding may have changed since.")
MISSING_FINDING = _(
    "This finding is not in the version of the KM Database shown now ({label}). It may have been removed by "
    "a newer version."
)
MISSING_DOCUMENT = _(
    "This document is not in the version of the KM Database shown now ({label}). It may have been removed "
    "by a newer version."
)
MISSING_EMPTY = _("The KM Database is empty: this page is not in it.")
BACK_TO_TABLE = _("Back to the KM Database")
TYPE_DIFFERS = _("The documents sheet gives “{type}”.")
ROWS_DIFFER = ngettext_lazy("{count} finding in the table", "{count} findings in the table", "n")
NOT_MARKED_YES = _("Not marked Yes")
PRINT_AUTHORS = _("Publishing organisations")

# ------------------------------------------------------------------------------------------- downloads
ABOUT_SHEET = _("About")
ABOUT_PREPARED = _("Prepared on")
ABOUT_LOADED = _("Loaded on")
ABOUT_FILTER = _("Filter")
ABOUT_ROWS = _("Rows")
ABOUT_AUTHORS = _("Publishing organisations")
ABOUT_AUTHORS_RULE = _("Organisations only. Individual authors are counted, never named.")
ABOUT_NAMES = _("Names of people")
ABOUT_NAMES_RULE = _("Names of people inside the texts are withheld.")
NOT_KNOWN = _("not known")
NO_FILTER = _("None: every row")
SEARCH_NOT_WRITTEN = _("Search: (your words are not written here: one of them names an author)")
TYPE_NOT_WRITTEN = _("Document type: (not written here: one of them names a person)")

# ------------------------------------------------------------------------- the version page (the admin)
DOWNLOAD_SUMMARY = _(
    "Downloads name the organisations of {organisations}; {authors_only} list only authors (counted, not "
    "named); {mixed} mix both; {empty} give no author."
)
AUTHORS_IN_DOWNLOADS = _("Authors in downloads")
NAMED_AS_ORGANISATIONS = _("Named as organisations in downloads ({count})")
NAMED_HELP = _(
    "Each of these is written in downloads as an organisation. If one is a person, press “Count as a "
    "person”: downloads then count it as an author, without naming it."
)
COUNT_AS_PERSON = _("Count as a person")
LOOK_LIKE_WORDS = _("Look like words, not names ({count})")
LOOK_LIKE_WORDS_HELP = _(
    "These author entries are also ordinary words of many findings. Until you say otherwise they are "
    "treated as names: withheld from downloads and from the assistant. Press “Not a name” for an entry that "
    "is not a person."
)
NOT_A_NAME = _("Not a name")
WORDS_WARNING = ngettext_lazy(
    "{count} author entry looks like words, not a name: check it on this page.",
    "{count} author entries look like words, not names: check them on this page.",
    "n",
)
REVIEW_SAVED = _("“{name}” is now listed under Organisations named in downloads: {kind}.")

# ------------------------------------------------------------------------ the filters' and pages' labels
NOT_REPORTED = _("Not reported")
ORIGIN_TITLE = _("Evidence")
FINDING_HEADING = _("Finding")
DOCUMENT_HEADING = _("Document")
OPEN_DOCUMENT = _("Open its document")
ALL_OF_DOCUMENT = _("All findings of this document")
OPEN_IN_TABLE = _("Open in the table with filters")
PRINT = _("Print")
OPEN_AS_PAGE = _("Open as page")
ITS_FINDINGS = _("Its findings")
GROUP_FINDING = _("The finding")
GROUP_FITS = _("Where it fits")
GROUP_WHO = _("Who it is about")
GROUP_EVIDENCE = _("The evidence")
GROUP_DOCUMENT = _("Its document")
ANALYSIS = _("Finding analysis")
MEASURE = _("Measure")
LIMITATIONS = _("Limitations")
PILLAR = _("Pillar")
DOMAIN = _("Analytical domain")
QUESTION = _("Research question")
SUB_QUESTION = _("Sub-question")
SUB_SUB_QUESTION = _("Sub-sub-question")
INDICATOR = _("Indicator or evidence asked for")
INDICATOR_PART = _("Part of the indicator answered")
ALSO_RELEVANT = _("Also relevant to")
AGE_GIVEN = _("Age group (as the workbook gives it)")
POPULATION = _("Population")
ORIGIN = _("Evidence origin")
CONFIDENCE = _("Confidence level")
METHODS = _("Methods and sample")
PERIOD = _("Study or data collection period")
QUALITY = _("Data quality")
DOCUMENT_TITLE_LABEL = _("Title")
DOCUMENT_ID = _("Document ID")
YEAR = _("Year")
TYPE = _("Document or study type")
TEXT_USED = _("Text used")
LINK = _("Link")
AUTHORS_SCREEN = _("Publishing entity / authors (as the workbook gives it)")
NUMBER_OF_FINDINGS = _("Number of findings")

# ------------------------------------------------------------------------- Ask the KM Database (its tab)
TAB_ASK = _("Ask the KM Database")
TAB_ASK_SHORT = _("Ask")
ASK_TITLE = _("Ask · KM Database")
ASK_INTRO = _(
    "Ask a question about the evidence. The answer is written from the findings of the KM Database that "
    "match it, and each sentence names the findings it comes from."
)
ASK_EXAMPLES = (
    _("What does the evidence say about girls' access to secondary school?"),
    _("Which findings with Level A confidence are about household income?"),
    _("What are the gaps in the evidence on climate change and children?"),
)
ASK_LABEL = _("Your question to the KM Database")
ASK_PLACEHOLDER = _("Ask about the findings, e.g. what helps out-of-school children return to school?")
ASK_SCOPE = _("Answers read only the findings that match: {filters}")
ASK_SCOPE_CLEAR = _("Clear")
ASK_SCOPE_HINT = _(
    "To ask about some findings only (a pillar, a year, one document), filter them in the Findings tab, then "
    "open this tab."
)
ASK_OFF = _(
    "The AI is switched off: Ask lists the findings that match your question, without a written answer. "
    "Search and the filters work as usual."
)
ASK_PRIVACY = _(
    "Your question and the findings of the KM Database that match it are sent to {provider}. Nothing else "
    "from NeuroDB is sent. Names of authors and other people, e-mail addresses, links and phone numbers are "
    "taken out first; if a name is still found, nothing is sent."
)
ASK_LIMITS = _(
    "Up to {n} questions a day, 2 at a time. Answers can contain mistakes: open the findings each one cites."
)
ASK_QUOTA = _("{used} of {allowed} today")
ASK_QUOTA_TITLE = _("Your questions to the KM Database today; the count starts again at midnight.")
RECENT_QUESTIONS = _("Your recent questions")
NEW_CONVERSATION = _("New conversation")
ASK_ABOUT_DOCUMENT = _("Ask about its document")
ASK_ABOUT_THIS_DOCUMENT = _("Ask about this document")
ONE_DOCUMENT = _("Document: {title}")

# the answer, as it streams
TYPE_A_QUESTION = _("Type a question first.")
QUESTION_TOO_LONG = _("Please keep questions under 1,000 characters.")
ASK_BUSY = _("Your other questions to the KM Database are still being answered. Please wait for them.")
ASK_EMPTY = _("The KM Database is empty: there is nothing to ask yet.")
STEP_SEARCHING = _("Searching the KM Database")
STEP_READING = _("Reading {findings} from {documents}")
STEP_WRITING = _("Writing the answer")
REASON_OFF = _("The AI is switched off: here are the findings that match.")
REASON_PAUSED = _("The AI is resting until {time}: here are the findings that match.")
REASON_SHARE = _(
    "The KM Database assistant has used its share of today's AI budget: here are the findings that match. "
    "Search and the filters work as usual."
)
REASON_BUDGET = _("Today's AI budget is used: here are the findings that match.")
REASON_QUOTA = _(
    "You have asked {n} questions to the KM Database today; the count starts again tomorrow. Here are the "
    "findings that match."
)
NO_MATCH = _("The KM Database has no findings that match this question. Try other words.")
NO_MATCH_FILTERED = _(
    "The KM Database has no findings that match this question within your filters. Try other words, or "
    "clear the filters."
)
FOUND_META = _("Found {n} of about {m} matching findings. Narrow with the filters to read others.")
ANSWERED_META = _("Answered from {n} of about {m} matching findings. Narrow with the filters to read others.")
ANSWERED_ALL_META = ngettext_lazy(
    "Answered from {count} matching finding.", "Answered from {count} matching findings.", "n"
)
CODES_USED = _("({codes}, from your question)")
CODES_LEFT_OUT = _("({codes} in your question matched no finding within the filters and was left out)")
NOT_SENT = _(
    "The question could not be sent to the AI safely. These are the findings that match your question:"
)
ANSWER_KEPT_BACK = _(
    "The AI's answer was kept back because it named a person. These are the findings that match your "
    "question:"
)
AI_FAILED = _("The AI could not write an answer. These are the findings that match your question:")
CUT_OFF = _("The AI could not finish this answer. Ask about one part, or add a filter.")
WENT_WRONG = _("Something went wrong while answering. Please try again.")
NOTICE_REFERENCES = _("Each [reference] opens the finding it comes from.")
NOTICE_REMOVED = ngettext_lazy(
    "{count} reference to a finding that was not read for this answer was taken out.",
    "{count} references to findings that were not read for this answer were taken out.",
    "n",
)
NOTICE_NOT_ANSWERED = _("The KM Database does not answer this question.")
NOTICE_PARTLY = _("The KM Database answers this only in part.")
NOTICE_NO_CITATION = _("This answer cites no finding: read it with care.")
NOTICE_UNCITED = ngettext_lazy(
    "{count} sentence cites no finding: check it against the findings below.",
    "{count} sentences cite no finding: check them against the findings below.",
    "n",
)
USED_HEADING = _("Findings used for this answer")
MATCH_HEADING = _("Findings that match your question")
ALL_READ = ngettext_lazy("The finding read", "All {count} findings read", "n")
