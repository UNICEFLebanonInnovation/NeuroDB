"""The 13 exports of the PRP Power BI report that Compare with Power BI reads (Admin → Data and sync), and
how one is recognised: **by its columns, never by its file name** (Power BI names its files after the visual,
and people rename them).

Each kind lists Power BI's column captions, the ones it needs and the ones it may have, with the type NeuroDB
reads each as. A file is of a kind when every needed caption is in its header, enough of its optional ones
(``at_least``) and none of the captions that rule it out (``excluded``); when several kinds fit, the one
whose captions cover most of the file's columns wins, and a tie is refused. The partner cards' four status
captions are inside the sector table's and the month chart's headers: a grouping column (Section, the month,
PD #) rules the cards out. Power BI writes the month chart's status columns only for the statuses in its data
(a past year has no Not Yet Due): the month is the one caption it needs, with at least two statuses.

Captions match whatever their spacing and case ("% Partner Cont. ", "status"). A column NeuroDB does not
compare is dropped by its position as the file is read, before any value of it is converted: a column that
names people ("UNICEF Focal Points", "Focal Point", or any caption ``catalogue.holds_person`` finds) is
never stored, shown or sent anywhere, and neither is a column holding values under no caption (Power BI lets
a column's name be a space). Captions are structure, not data: only those of some kind are ever named on a
page; the others are counted.

Power BI's page and visual for each kind are in ``comparison.POWERBI_VISUALS``, the one place for Power BI's
names.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from . import comparison

# the types a column is read as (powerbi_read.coerce)
TEXT = "text"  # a free text or a name: cleaned of people when free_text
# PD #, Report #, a P-code, a grant: kept as read (a cleaned code would match another), unless the people
# cleaner finds a person, an e-mail or a phone in it: then not kept at all (powerbi_read.names_someone)
CODE = "code"
SECTIONS = "sections"  # a joined list ("Education, Health"), compared as a set
NUMBER = "number"
COUNT = "count"
FRACTION = "fraction"  # Power BI's percent cells (0.25 for 25%)
MONEY = "money"
DATE = "date"
STAMP = "stamp"  # a moment: Power BI's refresh, one value for the whole file
LABEL = "label"  # a status word: kept as read, or not at all when it names someone (as a code)
MONTH = "month"  # "Jan 2026"
YES_NO = "yes_no"  # Yes or No only: any other text is read as a value NeuroDB could not read
EXPORT, SHEET1 = "export", "sheet1"  # Power BI's two layouts (powerbi_xlsx)


def normal(caption: object) -> str:
    """A caption as it is matched: its spaces as one, whatever their case."""
    return " ".join(str(caption or "").split()).casefold()


@dataclass(frozen=True)
class Column:
    """A column of an export: Power BI's caption, NeuroDB's name for it (a code word), its type, whether it is
    a free text (cleaned of people; a code or a status word that names someone is not kept at all), whether
    it is a title Power BI cuts at 255 characters, and whether the kind needs it."""

    caption: str
    name: str
    type: str
    free_text: bool = False
    title: bool = False
    needed: bool = True


def _optional(*columns: Column) -> tuple[Column, ...]:
    return tuple(Column(c.caption, c.name, c.type, c.free_text, c.title, needed=False) for c in columns)


@dataclass(frozen=True)
class Kind:
    """One Power BI visual's export: its code word, layout, columns, the captions never kept, the captions
    that rule it out and how many of its optional captions a file needs."""

    key: str
    layout: str
    columns: tuple[Column, ...]
    never: tuple[str, ...] = ()  # columns that name people: dropped by position as the file is read
    excluded: tuple[str, ...] = ()  # a file holding one of these is never of this kind
    at_least: int = 0  # optional captions a file needs (Power BI writes only some of them)
    captions: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captions",
            frozenset({normal(c.caption) for c in self.columns} | {normal(n) for n in self.never}),
        )

    @property
    def needed(self) -> frozenset[str]:
        return frozenset(normal(c.caption) for c in self.columns if c.needed)

    def fits(self, present: set[str]) -> bool:
        """A header with these captions can be of this kind."""
        optional = {normal(c.caption) for c in self.columns if not c.needed}
        return (
            self.needed <= present
            and not present & {normal(c) for c in self.excluded}
            and len(present & optional) >= self.at_least
        )

    @property
    def visual(self) -> tuple[str, str]:
        """(Power BI page, visual), from ``comparison.POWERBI_VISUALS``."""
        return comparison.POWERBI_VISUALS[VISUAL_OF.get(self.key, self.key)]

    @property
    def order(self) -> int:
        """Power BI's page order, then the visual's."""
        return list(KINDS).index(self.key)

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)


def _text(caption: str, name: str, *, title: bool = False) -> Column:
    return Column(caption, name, TEXT, free_text=True, title=title)


def _sections(caption: str = "Section", name: str = "section") -> Column:
    return Column(caption, name, SECTIONS, free_text=True)


def _count(caption: str, name: str) -> Column:
    return Column(caption, name, COUNT)


PD = Column("PD #", "pd", CODE)
PARTNER = _text("Partner", "partner")
INDICATOR = _text("Indicator Title", "indicator_title", title=True)
REPORT = Column("Report #", "report", CODE)
STATUSES = (
    _count("Accepted", "accepted"),
    _count("Submitted", "submitted"),
    _count("Due", "due"),
    _count("Overdue", "overdue"),
)

_KINDS = (
    Kind(
        "program_performance",
        EXPORT,
        (
            _sections(),
            _text("CP Output", "cp_output"),
            Column("Emergency Indicator", "emergency", YES_NO),
            INDICATOR,
            Column("Target", "target", NUMBER),
            Column("Reached", "reached", NUMBER),
            Column("% Reached", "achieved", FRACTION),
            PARTNER,
        ),
    ),
    Kind(
        "partner_cards",
        EXPORT,
        (*STATUSES, *_optional(_count("Sent back", "sent_back"))),
        excluded=("Section", "Reporting (mmm yyyy)", "PD #"),  # a grouping column: another visual's
    ),
    Kind(
        "follow_up",
        EXPORT,
        (
            PD,
            PARTNER,
            _sections(),
            Column("Status", "status", LABEL),
            Column("Report Due Date", "due_date", DATE),
        ),
    ),
    Kind("by_sector", EXPORT, (_sections(), *STATUSES)),
    Kind(
        "by_month",
        SHEET1,
        (
            Column("Reporting (mmm yyyy)", "month", MONTH),
            *_optional(  # Power BI writes a status only when its data has it
                _count("Accepted", "accepted"),
                _count("Submitted", "submitted"),
                _count("Overdue", "overdue"),
                _count("Due", "due"),
                _count("Not Yet Due", "not_yet_due"),
                _count("Sent back", "sent_back"),
            ),
        ),
        at_least=2,
    ),
    Kind("dq_card", SHEET1, (_count("No. of Indicators without Reached", "indicators"),)),
    Kind(
        "dq_list",
        EXPORT,
        (
            _sections(),
            PD,
            _text("CP Output", "cp_output"),
            _text("PD Output", "pd_output"),
            INDICATOR,
            Column("Reached", "reached", NUMBER),
        ),
    ),
    Kind(
        "pd_indicators",
        EXPORT,
        (
            PD,
            Column("PD Status", "pd_status", LABEL),
            PARTNER,
            Column("Document Type", "document_type", LABEL),
            INDICATOR,
            Column("Target", "target", NUMBER),
            *_optional(
                _text("Title", "pd_title", title=True),
                Column("Start Date", "start", DATE),
                Column("End Date", "end", DATE),
                _sections("Office", "offices"),
                _sections(),
                _text("CP Output", "cp_output"),
                Column("Reached", "reached", NUMBER),
            ),
        ),
    ),
    Kind(
        "report_list",
        EXPORT,
        (
            PD,
            REPORT,
            Column("Report Status", "report_status", LABEL),
            Column("Period End", "period_end", DATE),
            *_optional(
                Column("PD Status", "pd_status", LABEL),
                PARTNER,
                _sections(),
                Column("Document Type", "document_type", LABEL),
                Column("Report Due Date", "due_date", DATE),
                Column("Period Start", "period_start", DATE),
            ),
        ),
        never=("UNICEF Focal Points",),
    ),
    Kind(
        "locations",
        EXPORT,
        (
            PD,
            INDICATOR,
            REPORT,
            Column("Location Code", "p_code", CODE),
            Column("Reached", "reached", NUMBER),
            *_optional(
                Column("PD Status", "pd_status", LABEL),
                PARTNER,
                _sections(),
                _text("CP Output", "cp_output"),
                Column("Emergency Indicator", "emergency", YES_NO),
                Column("Report Status", "report_status", LABEL),
                Column("Period Start", "period_start", DATE),
                Column("Period End", "period_end", DATE),
                _text("Location Name", "location"),
                Column("Target", "target", NUMBER),
                _count("Disaggregation #", "disaggregations"),
                Column("Last Updated Date", "refreshed", STAMP),
                Column("Report Submission Date", "submitted_on", DATE),
            ),
        ),
    ),
    Kind(
        "disaggregations",
        EXPORT,
        (
            _text("Indicator", "indicator_title", title=True),
            Column("Location Pcode", "p_code", CODE),
            REPORT,
            _text("Disaggregation", "disaggregation"),
            Column("Reached", "reached", NUMBER),
            Column("Period End", "period_end", DATE),
            *_optional(
                _sections(),
                _text("Location", "location"),
                Column("Period Start", "period_start", DATE),
                Column("Report Due Date", "due_date", DATE),
                Column("Report Status", "report_status", LABEL),
                Column("Report Type", "report_type", LABEL),
                Column("Last Modify Date", "refreshed", STAMP),
            ),
        ),
    ),
    Kind(
        "fund_monitoring",
        EXPORT,
        (
            PD,
            Column("Planned UNICEF Cont.", "unicef_planned", MONEY),
            Column("Total Cash Amount", "cash", MONEY),
            Column("Partner Cont.", "partner_contribution", MONEY),
            *_optional(
                PARTNER,
                Column("Partner Type", "partner_type", LABEL),
                Column("Currency", "currency", LABEL),
                Column("% Partner Cont.", "partner_share", FRACTION),
                Column("Financial contribution during reporting periods", "financial_contribution", MONEY),
            ),
        ),
        never=("Focal Point",),
    ),
    Kind(
        "funded_programmes",
        EXPORT,
        (
            PD,
            Column("Grant #", "grant", CODE),
            Column("Fund Reservation", "fund_reservation", MONEY),  # the funds reserved for the grant
            *_optional(
                Column("status", "pd_status", LABEL),
                _text("Donor", "donor"),
                Column("Start", "start", DATE),
                Column("End", "end", DATE),
                Column("Days to End (-1: Ended)", "days_to_end", NUMBER),
                PARTNER,
                Column("Currency", "currency", LABEL),
                Column("UNICEF Planned Budget", "unicef_planned", MONEY),
                Column("Total Cash Amount", "cash", MONEY),
                Column("Actual Disburs", "disbursed", MONEY),
                Column("Total Partner Contribution", "partner_contribution", MONEY),
                Column("% Partner Cont.", "partner_share", FRACTION),
                Column("Outstanding DCT", "outstanding_dct", MONEY),
            ),
        ),
    ),
)
KINDS: dict[str, Kind] = {k.key: k for k in _KINDS}  # in Power BI's page order, then the visual's
# the kinds whose key is not the one ``comparison.POWERBI_VISUALS`` names their visual by
VISUAL_OF = {"program_performance": "pp_indicators", "by_sector": "by_section", "dq_card": "dq_tiles"}
KNOWN_CAPTIONS: frozenset[str] = frozenset().union(*(k.captions for k in _KINDS))
# a caption NeuroDB knows, as Power BI writes it (the first kind's spelling)
SPELLING: dict[str, str] = {}
for _kind in _KINDS:
    for _caption in (*(c.caption for c in _kind.columns), *_kind.never):
        SPELLING.setdefault(normal(_caption), _caption)


def holds_person(caption: str, kind: Kind | None = None) -> bool:
    """A column that names people: a kind's never-kept caption, or one ``catalogue.holds_person`` finds (a
    caption the kind lists is never one)."""
    from . import catalogue

    key = normal(caption)
    if kind is not None and key in {normal(c.caption) for c in kind.columns}:
        return False
    if any(key == normal(n) for k in _KINDS for n in k.never):
        return True
    return catalogue.holds_person("_".join(key.split()))


@dataclass
class Match:
    """What a file's header says: its kind (None when no kind fits or two tie), where each kept column is,
    and the columns that are not kept, in the words a page may show (captions NeuroDB knows; the others
    counted)."""

    kind: Kind | None
    positions: dict[int, Column] = field(default_factory=dict)  # the file's column -> the kind's column
    people: list[str] = field(default_factory=list)  # known captions of columns that name people
    people_others: int = 0  # other columns that name people (counted, never named)
    not_compared: list[str] = field(default_factory=list)  # known captions not kept
    unknown: int = 0  # columns NeuroDB does not know (counted): unknown captions, values under no caption
    tied: tuple[Kind, Kind] | None = None
    known: list[str] = field(
        default_factory=list
    )  # the file's captions NeuroDB knows, as Power BI spells them

    @property
    def whole(self) -> bool:
        """Every column of the file is one the kind keeps."""
        return not (self.people or self.people_others or self.not_compared or self.unknown)


def recognise(header: list[object], filled: Iterable[int] = ()) -> Match:
    """The kind of a file from its header (its captions by column position). ``filled``: the positions
    holding a value in the header or the first data rows (``powerbi_xlsx.Peek.filled``); one with no caption
    is a column NeuroDB does not know, dropped by its position like the others."""
    captions = {i: normal(c) for i, c in enumerate(header) if normal(c)}
    present = set(captions.values())
    uncaptioned = len(set(filled) - set(captions))
    fits = [k for k in _KINDS if k.fits(present)]
    known = [SPELLING[c] for c in dict.fromkeys(captions.values()) if c in KNOWN_CAPTIONS]
    if not fits:
        unknown = sum(1 for c in captions.values() if c not in KNOWN_CAPTIONS) + uncaptioned
        return Match(None, known=known, unknown=unknown)
    fits.sort(key=lambda k: -len(present & k.captions))
    best = fits[0]
    if len(fits) > 1 and len(present & fits[1].captions) == len(present & best.captions):
        return Match(None, tied=(best, fits[1]), known=known)
    match = Match(best, known=known, unknown=uncaptioned)
    by_caption = {normal(c.caption): c for c in best.columns}
    taken: set[str] = set()
    for i, caption in captions.items():
        column = by_caption.get(caption)
        if column is not None and column.name not in taken:
            match.positions[i] = column
            taken.add(column.name)
        elif holds_person(caption, None if column is not None else best):
            if caption in KNOWN_CAPTIONS:
                match.people.append(SPELLING[caption])
            else:
                match.people_others += 1
        elif caption in KNOWN_CAPTIONS:
            match.not_compared.append(SPELLING[caption])
        else:
            match.unknown += 1
    return match


def kept(kind: Kind, positions: dict[int, Column]) -> list[Column]:
    """The kept columns in the kind's order."""
    names = {c.name for c in positions.values()}
    return [c for c in kind.columns if c.name in names]
