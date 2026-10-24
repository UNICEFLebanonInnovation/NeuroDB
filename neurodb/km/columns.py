"""The KM workbook's sheets and columns, and how each cell is read ("as given": kept as the workbook writes
it, with codes worked out beside it).

Sheets are matched by the last word of their name (the documents sheet, the findings sheet) or, for the
optional Read Me sheet, by "readme" in its name with the spaces removed: the code never holds a sheet's title.
Headers are matched after :func:`fold`. The headers below are the workbook's column names; they are shown
to the administrator who uploads a workbook (its messages point at what that person sees in Excel), never
on the KM Database page, which uses the plain labels (:data:`FLAGS`).
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field

# True once the user confirms that Level A is the strongest evidence: turns on the pill colours, the legend,
# the AI's sentence and the ranking by level. Until then every level looks the same and nothing ranks by it.
CONFIDENCE_A_STRONGEST = False

DOCUMENTS = "documents"  # the last word of the documents sheet's name
FINDINGS = "findings"  # the last word of the findings sheet's name
READ_ME = "readme"  # in the Read Me sheet's name, with its spaces removed

TEXT_MAX = 20_000  # a longer text is cut (and is a value to check)
CHECK_MAX = 300  # a value to check is kept as given up to this many characters
SPILL = 100  # a value this long in a short column (a level, a flag, a year, a type) is a value to check
CODE_SIZE = 8  # a code worked out beside a label ("D3", "RQ12"): a longer one is a value to check
COUNT_MAX = 2_147_483_647  # the largest count a column holds: a larger one is a value to check
ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")

_DASHES = re.compile(r"[‐-―−﹘﹣－]")
_SPACES = re.compile(r"\s+")
_AUTHORS = re.compile(r"\bauthors\b")


def fold(header) -> str:
    """A header as it is matched: case-folded, any dash written "-", spaces collapsed, no space around "/",
    and "authors" written "author" (so "Publishing Entity / Author" matches "Publishing Entity / Authors")."""
    text = _SPACES.sub(" ", _DASHES.sub("-", str(header or "")).casefold()).strip()
    return _AUTHORS.sub("author", re.sub(r"\s*/\s*", "/", text))


def sheet_kind(title: str) -> str:
    """``DOCUMENTS``, ``FINDINGS``, ``READ_ME`` or "" (a sheet the KM Database does not read)."""
    words = _SPACES.sub(" ", str(title or "")).strip().casefold().split(" ")
    if words[-1] in (DOCUMENTS, FINDINGS):
        return words[-1]
    if READ_ME in "".join(words):
        return READ_ME
    return ""


# ------------------------------------------------------------------------------------------------ columns
@dataclass(frozen=True)
class Column:
    field: str  # the model field it fills (a kind may fill more: the code beside the label)
    header: str  # as the workbook writes it
    kind: str = "text"  # text, id, year, flag, confidence, pillar, domain, rq, count, quality, type
    required: bool = False
    size: int = 0  # a CharField's length: a longer value is cut there (and is a value to check)

    @property
    def folded(self) -> str:
        return fold(self.header)


@dataclass(frozen=True)
class Flag:
    field: str
    header: str
    group: str  # the heading the page shows it under
    label: str  # the plain label every chip, filter and finding page shows
    triggers: tuple[
        str, ...
    ] = ()  # generic words of a question that make Ask rank a finding marked Yes first


AGE, SEX, SEM, THEMES = (
    "Age groups",
    "Girls and boys",
    "Level of the social-ecological model",
    "Cross-cutting themes",
)
FLAG_GROUPS = (AGE, SEX, SEM, THEMES)
FLAGS: tuple[Flag, ...] = (
    Flag(
        "prenatal",
        "Prenatal",
        AGE,
        "Before birth (pregnancy)",
        ("pregnant", "pregnancy", "antenatal", "prenatal"),
    ),
    Flag(
        "early_childhood",
        "Early Childhood 0-4",
        AGE,
        "Ages 0-4",
        (
            "under five",
            "under-five",
            "0-4",
            "infants",
            "infant",
            "toddlers",
            "early childhood",
            "preschool",
            "ECD",
        ),
    ),
    Flag("middle_childhood", "Middle Childhood 5-9", AGE, "Ages 5-9", ("5-9", "primary school age")),
    Flag(
        "early_adolescence",
        "Early Adolescence 10-14",
        AGE,
        "Ages 10-14",
        ("adolescents", "adolescent", "teenagers", "10-14"),
    ),
    Flag("late_adolescence", "Late Adolescence 15-19", AGE, "Ages 15-19", ("15-19", "older adolescents")),
    Flag("youth", "Youth 15-24", AGE, "Youth (15-24)", ("youth", "15-24")),
    Flag("young_people", "Young People 10-24", AGE, "Young people (10-24)", ("young people", "10-24")),
    Flag(
        "adult_caregiver",
        "Adult / Caregiver 20+",
        AGE,
        "Adults and caregivers (20+)",
        ("caregivers", "caregiver", "parents", "parent"),
    ),
    Flag(
        "female",
        "Female",
        SEX,
        "Girls and women",
        ("girls", "girl", "women", "woman", "female", "mothers", "mother"),
    ),
    Flag("male", "Male", SEX, "Boys and men", ("boys", "boy", "men", "man", "male", "fathers", "father")),
    Flag("sem_individual", "SEM Individual", SEM, "Individual"),
    Flag("sem_household", "SEM Interpersonal / Household", SEM, "Household and family"),
    Flag("sem_community", "SEM Community", SEM, "Community"),
    Flag("sem_institutional", "SEM Organisational / Institutional", SEM, "Institutions and services"),
    Flag("sem_policy", "SEM Policy / Enabling Environment", SEM, "Policies and laws"),
    Flag("disability", "Disability Inclusion", THEMES, "Disability inclusion"),
    Flag("poverty", "Poverty / Economic Vulnerability", THEMES, "Poverty and economic vulnerability"),
    Flag("geographic", "Geographic Inequalities", THEMES, "Differences between areas"),
    Flag("nationality", "Nationality / Legal Status", THEMES, "Nationality and legal status"),
    Flag("mhpss", "MHPSS", THEMES, "Mental health and psychosocial support (MHPSS)"),
    Flag("sbc", "SBC", THEMES, "Social and behaviour change (SBC)"),
    Flag("climate", "Climate / Environmental Resilience", THEMES, "Climate and the environment"),
    Flag("systems", "Systems Strengthening", THEMES, "Strengthening systems"),
    Flag("foresight", "Foresight / Future Scenarios", THEMES, "Looking ahead (future scenarios)"),
    Flag(
        "information_access",
        "Information Access / Digital Rights / Accountability",
        THEMES,
        "Access to information, digital rights and accountability",
    ),
    Flag("youth_participation", "Youth Participation / Engagement", THEMES, "Youth participation"),
    Flag(
        "palestinian_programme",
        "Palestinian Programme in Lebanon",
        THEMES,
        "Palestinian programme in Lebanon",
    ),
)

DOCUMENT_COLUMNS: tuple[Column, ...] = (
    Column("record_id", "Record ID", "id", required=True),
    Column("item_title", "Item Title"),
    Column("title", "Document Title", required=True),
    Column("entity", "Publishing Entity / Authors"),
    Column("year", "Publication Year", "year"),
    Column("study_type", "Document / Study Type", "type", size=200),
    Column("text_used", "Text Used", size=100),
    Column("confidence", "Evidence Confidence Level", "confidence"),
    Column("findings_stated", "Number of Findings", "count"),
    Column("link", "Document Link"),
    Column("quality", "Data Quality Status", "quality", required=True),
)
FINDING_COLUMNS: tuple[Column, ...] = (
    Column("finding_id", "Finding ID", "id", required=True),
    Column("item_title", "Item Title"),
    Column("record_id", "Record ID", "id", required=True),
    Column("document_title", "Document Title", required=True),
    Column("pillar", "Pillar", "pillar", required=True),
    Column("domain", "Analytical Domain", "domain"),
    Column("research_question", "Research Question", "rq", required=True),
    Column("sub_question", "Sub-question"),
    Column("sub_sub_question", "Sub-sub-question"),
    Column("indicator", "Indicator / Evidence Requirement"),
    Column("indicator_component", "Indicator Component Answered"),
    Column("also_relevant", "Also Relevant To"),
    Column("measure", "Measure"),
    Column("key_finding", "Key Finding", required=True),
    Column("analysis", "Finding Analysis"),
    Column("age_text", "Age Category / Age Bracket"),
    *(Column(flag.field, flag.header, "flag") for flag in FLAGS),
    Column("limitations", "Limitations"),
    Column("evidence_origin", "Evidence Origin", "origin"),
    Column("confidence", "Evidence Confidence Level", "confidence", required=True),
    Column("entity", "Publishing Entity / Author"),
    Column("study_type", "Document / Study Type", "type"),
    Column("year", "Publication Year", "year"),
    Column("period", "Study / Data Collection Period"),
    Column("population", "Population"),
    Column("methods", "Methods and Sample"),
    Column("link", "Source Link"),
    Column("quality", "Data Quality Status", "quality", required=True),
)
COLUMNS = {DOCUMENTS: DOCUMENT_COLUMNS, FINDINGS: FINDING_COLUMNS}
RECORD_ID = DOCUMENT_COLUMNS[0]  # the documents sheet's ID, which every finding names
FINDING_ID = FINDING_COLUMNS[0]
NUMBER_OF_FINDINGS = next(c for c in DOCUMENT_COLUMNS if c.field == "findings_stated")
DATA_QUALITY = DOCUMENT_COLUMNS[-1]

# The Read Me sheet's labels the load reads (column A, the value in column B); the stated row counts are the
# rows whose label ends with the documents or findings sheet's last word (or is that sheet's name).
PREPARED_ON = "Prepared on"
SOURCE_WORKBOOK = "Source workbook"


# ------------------------------------------------------------------------------------------- reading cells
@dataclass
class Cells:
    """One row read: the model fields' values, and the values to check (``{header: value as given}``)."""

    values: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)

    def check(self, column: Column, raw) -> None:
        self.checks[column.header] = as_text(raw)[:CHECK_MAX]


def as_text(value) -> str:
    """A cell as text: stripped, no NUL, ``\\r\\n`` written ``\\n``; a number as its digits ("2023", never
    "2023.0"); a date ``yyyy-mm-dd``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        return (
            value.date().isoformat()
            if not (value.hour or value.minute or value.second)
            else value.isoformat(" ")
        )
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


FLAG_WORDS = {
    "yes": "Y", "y": "Y", "true": "Y", "1": "Y",
    "no": "N", "n": "N", "false": "N", "0": "N",
    "not reported": "R",
    "not applicable": "A", "n/a": "A", "na": "A",
    "": "",
}  # fmt: skip
_LEVEL = re.compile(r"^(?:level\s*)?([a-d])$", re.IGNORECASE)
_PILLAR = re.compile(r"^\s*(P\d{1,2})\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_RQ = re.compile(r"^\s*RQ\s*(\d+)", re.IGNORECASE)
_YEAR = re.compile(r"^\d{4}$")
NOT_REPORTED = "not reported"


def whole(raw, text: str) -> int | None:
    """A cell's whole number of 0 or more (12, 12.0 or "12"), or None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() and raw >= 0 else None
    return int(text) if text.isdecimal() and len(text) <= 18 else None


def _code(prefix: str, digits: str) -> str:
    """``prefix`` and the number ``digits`` ("D3", "RQ12"), or "" when that is longer than a code can be."""
    number = digits.lstrip("0") or "0"
    return f"{prefix}{int(number)}" if len(prefix) + len(number) <= CODE_SIZE else ""


def question_key(pillar_code: str, wording: str) -> str:
    """A research question's key: its pillar and its wording, so a filter link survives a new version whose
    wording is unchanged (the same code is worded differently in each pillar)."""
    folded = _SPACES.sub(" ", wording.casefold()).strip()
    if not folded:
        return ""
    return hashlib.sha256(f"{pillar_code}|{folded}".encode()).hexdigest()[:10]


def read(column: Column, raw, cells: Cells) -> None:
    """Read one cell of ``column`` into ``cells`` (its fields, and a value to check when it is not one of the
    expected ones). The value is always kept as given beside any code worked out from it."""
    text = as_text(raw)
    values = cells.values
    kind = column.kind
    if len(text) > TEXT_MAX:
        cells.check(column, raw)
        text = text[:TEXT_MAX]
    if kind in ("text", "id", "origin", "type"):
        if column.size and len(text) > column.size:
            cells.check(column, raw)
            text = text[: column.size]
        if kind == "type" and len(text) >= SPILL:
            cells.check(column, raw)
        values[column.field] = text
        if kind == "origin":
            values["origin_kind"] = (
                ("P" if text.split()[0].casefold().startswith("primary") else "O") if text else ""
            )
    elif kind == "year":
        values["year_text"] = text[:40]
        year = None
        if isinstance(raw, int | float) and not isinstance(raw, bool) and float(raw).is_integer():
            year = int(raw)
        elif _YEAR.match(text):
            year = int(text)
        elif text.casefold() not in ("", NOT_REPORTED):
            cells.check(column, raw)
        if year is not None and not 1900 <= year <= 2100:
            cells.check(column, raw)
            year = None
        if len(text) > 40 and column.header not in cells.checks:
            cells.check(column, raw)
        values["year"] = year
    elif kind == "flag":
        code = FLAG_WORDS.get(text.casefold())
        if code is None:
            cells.check(column, raw)
            code = ""
        values[column.field] = code
    elif kind == "confidence":
        values["confidence_text"] = text[:200]
        found = _LEVEL.match(text)
        if text and not found:
            cells.check(column, raw)
        values["confidence"] = found.group(1).upper() if found else ""
    elif kind == "pillar":
        found = _PILLAR.match(text)
        if not found:
            cells.check(column, raw)
        values["pillar_code"] = found.group(1).upper() if found else ""
        values["pillar"] = text
    elif kind == "domain":
        found = _NUMBER.search(text)
        code = _code("D", found.group()) if found else ""
        if text and not code:
            cells.check(column, raw)
        values["domain_code"] = code
        values["domain"] = text
    elif kind == "rq":
        found = _RQ.match(text)
        code = f"RQ{found.group(1)}" if found else ""
        if len(code) > CODE_SIZE:
            code = ""
        if not code:
            cells.check(column, raw)
        values["rq_code"] = code
        values["research_question"] = text
    elif kind == "count":
        number = whole(raw, text)
        if (number is None and text) or (number is not None and number > COUNT_MAX):
            cells.check(column, raw)
            number = None
        values[column.field] = number
    elif kind == "quality":
        values["quality"] = text
        values["needs_review"] = not text.casefold().startswith("ready")
    else:  # pragma: no cover - a kind added to the table without its reader
        raise ValueError(f"unknown column kind {kind!r}")


def empty_values(columns: tuple[Column, ...]) -> dict:
    """Every field a sheet's columns fill, empty: what a missing optional column loads as."""
    cells = Cells()
    for column in columns:
        read(column, None, cells)
    return cells.values


def finish(cells: Cells) -> Cells:
    """The fields worked out from several columns once the row is read (the question's key)."""
    if "research_question" in cells.values:
        cells.values["question_key"] = question_key(
            cells.values.get("pillar_code", ""), cells.values.get("research_question", "")
        )
    return cells
