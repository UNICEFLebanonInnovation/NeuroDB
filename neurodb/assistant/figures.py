"""Ask NeuroDB's check of its answer once it is written, before the checked answer replaces the one shown
while it was written and is kept: Monitoring insights' check of a chat answer
(:mod:`neurodb.fmm.ai.citations`), against what the lookups of this answer returned.

- **Links**: a link stays only when a lookup of this answer returned its url (one of NeuroDB's pages under
  a url key: "url", "urls", "..._url", or inside a mapping under one: "urls": {"assurance": "/assurance/"}),
  or an earlier answer gave it in the part the AI was sent (the latest answer whole, the first paragraph of
  older ones); otherwise its text stays and the link goes. The rendered answer gets the same check, so a
  link written another way (a reference link, raw HTML) cannot slip past it.
- **Figures**: as in Monitoring insights, references ("LEB/PCA2026001/PD2026001"), dates ("13 Sep 2026",
  "2026-09-13", "Oct 2026") and whole numbers up to 10 are set aside, and so are years ("2026",
  "2025/26"), page numbers ("p. 41", "pages 3-4"), "100%" and the addresses of links written another way
  (raw HTML, a reference link, a bare address). A figure in the text of a link is checked as any other.
  Every other number must be one of:

  - a number written in a lookup's result (never an id, "partner_id": 412, nor the digits of a link,
    "/partners/412/"), or that number rounded to one decimal or to a whole number; a percentage must be
    one the lookup gives as a percentage ("achieved_percent", "rate", a row whose unit is "%", "82%" in a
    text), not just any count;
  - a figure written with a scale ("12.4 million", "$450K") that is a figure or a total of the lookups at
    that scale, to the precision it is written with;
  - a number of the question, or of an earlier question of the conversation;
  - a number of an earlier answer of the conversation that its own check did not list;
  - a percentage worked out from one lookup: one figure over another of the same small row (up to
    ``ROW_FIGURES`` figures) or the change from one to the other, any figure of a row over its target, a
    row's figure over the total of its column in a list, a count over the sum of its mapping of counts
    ("status_counts") or over a figure of the result that holds it ("indicators": 30), or a fraction given
    as a percentage (0.25 as 25%);
  - a total worked out from one lookup: the figures of a small row added or one taken from another, a
    column of a list or a mapping of counts added up, or the number of items in a list (ids and years are
    never added up);
  - two figures written side by side in the answer (the same sentence or the one before it, a list item
    and the line that leads into it, a table row and its column's total or Total row): one over the other
    or the size of the change from one to the other as a percentage (a fall from 1,500 to 1,200 is 20%),
    the two added or the difference ("1,200 of 1,500 (80.0%)", "1,200 and 450, 1,650 in all", "from
    12,400 to 15,900, a rise of 3,500"); those two figures are checked themselves.

  Only these simple sums and shares count: over every pair of figures of a large lookup, almost any
  percentage could be "worked out", and the check would find nothing. The others are listed under the
  answer as the answer writes them, in Monitoring insights' words: "These figures could not be checked
  against the data: 4,950, 63.5%." The figures are never changed and nothing is held back. A number
  written without a thousands separator that looks like a year ("2000 children") is taken for a year.
- **The question log**: when the check listed a figure or took a link out, what it found
  (:attr:`Checked.entry`) is kept as the last entry of the question's lookups (``AssistantQuestion.tools``):
  ``{"check": "answer", "notice", "unchecked_numbers", "links_removed"}``. A follow-up reads it back
  (:func:`listed`), so a figure listed once is listed again when it is repeated.

People are not looked for in the answer: the question, the earlier turns and every lookup's result reach
the AI with the people NeuroDB can find taken out (:mod:`.privacy`). Help, Monitoring insights and NeuroDB
Watch run the same loop with their own options: their answers are not checked here (Monitoring insights
checks its own).
"""

from __future__ import annotations

import bisect
import html
import itertools
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import privacy
from .charts import _NUMERIC

CHECK = "answer"  # the "check" of the question log's entry
ROW_FIGURES = 4  # a row of up to this many figures: its figures are worked out with each other

# A page number: "p. 41", "pp. 41-43", "page 77", "pages 3 and 4"
PAGE = re.compile(r"\b(?:pp?\.|pages?)\s*\d+(?:\s*(?:[-–]|to|and)\s*\d+)?", re.IGNORECASE)
# A year, or a span of years: "2026", "2025/26", "2025-2026", "2024–25"; never part of a date
# ("2026-10-15", "15/10/2026"), of a figure with a separator ("2,026", "2026.5") or of a longer number
YEAR = re.compile(r"(?<![\d.,/-])(?:19|20)\d\d(?:\s?[-–/]\s?(?:(?:19|20)\d\d|\d\d))?(?![-–/.,]?\d)")
# The address of a link written another way: an HTML tag, a reference link's definition ("[1]: /x/"), a web
# address, a path ("/partners/3/")
ADDRESS = re.compile(
    r"</?[A-Za-z][^<>\n]*>|^ {0,3}\[[^\]\n]+\]:.*$|\b(?:https?://|www\.)\S+|(?:(?<=\s)|^)/[A-Za-z][^\s)\]]*",
    re.MULTILINE,
)
# A figure written as a percentage: "72.7%", "80 percent", "15 per cent"
PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(?:%|percent\b|per cent\b)", re.IGNORECASE)
# A figure written with a scale: "12.4 million", "$450K", "USD 1.2bn", "3 thousand"
SCALED = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(billion|bn|million|mn|m|thousand|k)\b", re.IGNORECASE)
SCALES = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
# A written number, as numbers_in reads it (its thousands separators kept: what the notice shows)
WRITTEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# The end of a sentence inside a line of the answer (a colon or a semicolon does not end one)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A Markdown list item, and a table's separator row
BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
TABLE_RULE = re.compile(r"^\s*:?-{2,}:?\s*$")
# The first cell of a table's total row: "Total", "**All**"
TOTAL_ROW = re.compile(r"^[\s*_]*(?:total|all|overall|sum)\b", re.IGNORECASE)
# A key whose figure is a percentage ("achieved_percent", "percent_of_target", "rate", "share_percent")
PERCENT_WORDS = frozenset(
    {"percent", "percentage", "pct", "rate", "share", "ratio", "proportion", "coverage"}
)
PERCENT_KEYS = frozenset({"achieved", "achieved_in_year"})


def _figure(value: Any) -> float | None:
    """A lookup's value as a figure: a number, or a text that is only a number ("4,386", "82%")."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= 24 and _NUMERIC.match(text):
            return float(text.replace(",", "").rstrip("%"))
    return None


def _id_key(key: str) -> bool:
    """A key that holds an id, never a figure: "id", "partner_id", "master_id", "document_ids", "key"."""
    key = key.casefold()
    return key in ("id", "ids", "pk", "key", "keys") or key.endswith(("_id", "_ids", "_pk"))


def _year_key(key: str) -> bool:
    """A key that holds a year ("year", "value_year"; not "reached_in_year"): a figure looked up, but never
    added to or shared with the other figures."""
    key = key.casefold()
    return key in ("year", "years") or (key.endswith("_year") and not key.endswith("_in_year"))


def _code_key(key: str) -> bool:
    """A key that holds a code written as a number ("vendor_number", "reference_number"): looked up, but
    never added to or shared with the other figures."""
    key = key.casefold()
    return key == "number" or key.endswith("_number")


def _percent_key(key: str) -> bool:
    key = key.casefold()
    return key in PERCENT_KEYS or bool(PERCENT_WORDS & set(key.split("_")))


def _in_percent(row: Mapping) -> bool:
    """A row whose figures are percentages: its unit says so ("unit": "%", "percentage"; "is_percent")."""
    unit = row.get("unit")
    return row.get("is_percent") is True or (
        isinstance(unit, str) and ("%" in unit or "percent" in unit.casefold())
    )


@dataclass
class Looked:
    """What the lookups of one answer returned, for the check: NeuroDB's page links (``urls``), every
    number written in their results (``numbers``, in ``watch.grounding``'s form with its roundings; never an
    id or a link's digits), the ones that are percentages (``percents``), every figure (``values``), and the
    percentages (``shares``) and totals (``totals``) worked out from their figures."""

    urls: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    percents: set[str] = field(default_factory=set)
    values: set[float] = field(default_factory=set)
    shares: set[float] = field(default_factory=set)
    totals: set[float] = field(default_factory=set)

    def add(self, result: Any) -> None:
        """One lookup's result, as the model reads it: its values walked one by one (a list of figures is
        read figure by figure, whatever the JSON the model is sent)."""
        self._walk(result, "")

    def _number(self, written: str, percent: bool) -> None:
        from neurodb.watch import grounding

        found = grounding._allowed_numbers(written)
        self.numbers |= found
        if percent:
            self.percents |= found

    def _text(self, text: str) -> None:
        """The numbers written in a text (a note, a passage), its link addresses left out."""
        from neurodb.review.services import numbers_in

        text = ADDRESS.sub(" ", text)
        for written in numbers_in(text):
            self._number(written, False)
        for match in PERCENT.finditer(text):
            self._number(match.group(1), True)

    def _walk(
        self,
        value: Any,
        key: str,
        parent: list[float] | None = None,
        links: bool = False,
        percent: bool = False,
    ) -> None:
        if isinstance(value, str) and (links or privacy._url_key(key)):
            if privacy.SITE_PATH.fullmatch(value):
                self.urls.add(value)
            return  # a link's digits are not figures
        if _id_key(key):
            return
        number = _figure(value)
        if number is not None:
            self.values.add(number)
            written = repr(number) if isinstance(value, float) else str(value).replace("%", "")
            self._number(written.strip(), percent or _percent_key(key) or str(value).strip().endswith("%"))
            if 0 < abs(number) < 1:  # a fraction, given as a percentage
                self._share(100 * number)
            return
        if isinstance(value, str):
            self._text(value)
        elif isinstance(value, Mapping):
            self._mapping(value, parent, links or privacy._url_key(key), percent)
        elif isinstance(value, list | tuple):
            for inner in value:
                self._walk(inner, key, None, links, percent)
            if value:
                self._total(len(value))
            columns: dict[str, list[float]] = {}
            for inner in value:
                cells = inner.items() if isinstance(inner, Mapping) else [(key, inner)]
                for column, cell in cells:
                    column = str(column)
                    figure = (
                        None if _id_key(column) or _year_key(column) or _code_key(column) else _figure(cell)
                    )
                    if figure is not None:
                        columns.setdefault(column, []).append(figure)
            for figures in columns.values():
                self._column(figures)

    def _mapping(self, value: Mapping, parent: list[float] | None, links: bool, percent: bool) -> None:
        percent = percent or _in_percent(value)
        row: list[float] = []  # its figures (not its ids or years)
        targets: list[float] = []  # its targets ("target", "pd_target")
        values = [(str(k), v) for k, v in value.items()]
        for inner_key, inner in values:
            self._text(inner_key)  # a key can be a figure too ("6-11": the children aged 6 to 11)
            figure = _figure(inner)
            if figure is None or _id_key(inner_key) or _year_key(inner_key) or _code_key(inner_key):
                continue
            row.append(figure)
            if "target" in inner_key.casefold().split("_"):
                targets.append(figure)
        for inner_key, inner in values:
            self._walk(inner, inner_key, row, links, percent)
        if len(row) <= ROW_FIGURES:
            self._row(row)
        for target in targets:  # a figure over its target, whatever the size of the row
            for figure in row:
                if target:
                    self._share(100 * figure / target)
        counts = len(row) >= 2 and all(_figure(v) is not None for k, v in values if not _id_key(k))
        if counts:  # a mapping of counts ("status_counts", "by_year"): each one's share of their sum and
            self._column(row)  # of the figures of the result that holds it ("indicators": 30)
            for whole in parent or ():
                if whole:
                    for figure in row:
                        self._share(100 * figure / whole)

    def _row(self, row: list[float]) -> None:
        """The figures of a small row: each over another, the change from one to the other, and two or
        more of them added, or one taken from another."""
        for i, a in enumerate(row):
            for j, b in enumerate(row):
                if i != j and b:
                    self._share(100 * a / b)
                    self._share(abs(100 * (a - b) / b))
                if i < j:
                    self._total(abs(a - b))
        for size in range(2, len(row) + 1):
            for chosen in itertools.combinations(row, size):
                self._total(math.fsum(chosen))

    def _column(self, column: list[float]) -> None:
        """A column of a list: its total, and each figure's share of it."""
        if len(column) < 2:
            return
        total = math.fsum(column)
        self._total(total)
        if total:
            for figure in column:
                self._share(100 * figure / total)

    def _share(self, value: float) -> None:
        if math.isfinite(value):
            self.shares.add(value)

    def _total(self, value: float) -> None:
        if math.isfinite(value):
            self.totals.add(float(value))


@dataclass
class Checked:
    """An answer after the check: its text and HTML, the figures no lookup returned (without thousands
    separators: the form the question log keeps and a follow-up reads back), each as the answer writes it
    (``shown``: what the notice lists) and the number of links taken out."""

    text: str
    html: str
    unchecked_numbers: list[str] = field(default_factory=list)
    links_removed: int = 0
    shown: list[str] = field(default_factory=list)

    @property
    def notice(self) -> str:
        """The line under the answer: the figures no lookup returned, as the answer writes them ("12,345",
        "61.3%"); "" when there are none."""
        from neurodb.fmm.ai import citations

        return citations.figures_line(self.shown or self.unchecked_numbers)

    @property
    def entry(self) -> dict[str, Any]:
        """The question log's entry: what the check found; empty when it found nothing."""
        if not self.unchecked_numbers and not self.links_removed:
            return {}
        return {
            "check": CHECK,
            "notice": self.notice,
            "unchecked_numbers": list(self.unchecked_numbers),
            "links_removed": self.links_removed,
        }


def listed(entries: Any) -> list[str]:
    """The figures an answer's check listed, read back from its lookups in the question log (``tools``)."""
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, Mapping) and entry.get("check") == CHECK:
            return [str(number) for number in entry.get("unchecked_numbers") or ()]
    return []


def _aside(text: str) -> str:
    """Addresses, page numbers and years out of ``text`` (once references and dates are out)."""
    return YEAR.sub(" ", PAGE.sub(" ", ADDRESS.sub(" ", text)))


def _near(value: float, found: list[float], within: float) -> bool:
    """Some figure of ``found`` (sorted) is ``value`` to within ``within``."""
    at = bisect.bisect_left(found, value - within)
    return at < len(found) and found[at] <= value + within


def _within(written: str, scale: float = 1.0) -> float:
    """How far a figure may be from what it was worked out from, to the precision it is written with (77%
    for 76.92; 1,650 for 1,649.6; 12.4 million for 12,412,345)."""
    places = len(written.partition(".")[2])
    return (0.5 * 10**-places + 1e-9 * max(1.0, abs(float(written)))) * scale


def _figures_in(text: str) -> list[tuple[str, float]]:
    from neurodb.review.services import numbers_in

    return [(number, float(number)) for number in numbers_in(text)]


def _sentences(line: str) -> list[list[tuple[str, float]]]:
    """The figures of each sentence of a line, each with the figures of the sentence before it ("In 2025
    it reached 1,200. In 2026 1,500, up 25.0%.")."""
    parts = [_figures_in(part) for part in SENTENCE_END.split(line) if part.strip()]
    return [part + (parts[n - 1] if n else []) for n, part in enumerate(parts)]


def _table(rows: list[list[str]]) -> list[list[tuple[str, float]]]:
    """A Markdown table's figures, row by row: each row with its column totals (added up, or its total
    row's), so that a cell's share of its column is worked out ("| On track | 12 | 40.0% |" over "| Total |
    30 | 100% |")."""
    cells = [[_figures_in(cell) for cell in row] for row in rows]
    totals = [row for row, text in zip(cells, rows, strict=True) if text and TOTAL_ROW.match(text[0])]
    body = [row for row, text in zip(cells, rows, strict=True) if not (text and TOTAL_ROW.match(text[0]))]
    sums: list[tuple[str, float]] = []
    for column in range(max((len(row) for row in body), default=0)):
        figures = [row[column][0][1] for row in body if column < len(row) and len(row[column]) == 1]
        if len(figures) >= 2:
            total = math.fsum(figures)
            sums.append((repr(total), total))
    given = [figure for row in totals for cell in row for figure in cell]
    return [[figure for cell in row for figure in cell] + given + sums for row in cells]


def _side_by_side(text: str) -> list[list[tuple[str, float]]]:
    """The figures written side by side in ``text``: each sentence with the one before it, each item of a
    list with the line that leads into it ("Of the 30 indicators:"), each row of a table with its column
    totals. A colon or a semicolon does not end a sentence: "a target of 4,000; 2,750 to go"."""
    units: list[list[tuple[str, float]]] = []
    table: list[list[str]] = []
    lead: list[tuple[str, float]] = []  # the figures of the line before a list
    for line in [*text.split("\n"), ""]:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not all(TABLE_RULE.match(cell) for cell in cells):
                table.append(cells)
            continue
        if table:
            units += _table(table)
            table = []
        if not stripped:
            continue
        if BULLET.match(line):
            units += [sentence + lead for sentence in _sentences(BULLET.sub("", line))]
            continue
        found = _sentences(line)
        units += found
        lead = _figures_in(SENTENCE_END.split(stripped)[-1])
    return [unit for unit in units if unit]


def _worked_out(
    written: str, percent: bool, found: list[float], sentences: list[list[tuple[str, float]]]
) -> bool:
    """``written`` is a percentage or a total worked out from one lookup's figures (``found``: the
    lookups' shares or totals, sorted), or a share, change, sum or difference of two figures written beside
    it (:func:`_side_by_side`), to the precision it is written with. A change is its size: a fall from 1,500
    to 1,200 is 20%, however it is written ("a drop of 20.0%", "-20.0%")."""
    value = float(written)
    within = _within(written)
    if _near(value, found, within):
        return True
    for sentence in sentences:
        if written not in (number for number, _ in sentence):
            continue
        others = [figure for number, figure in sentence if number != written]
        for i, a in enumerate(others):
            for j, b in enumerate(others):
                if i == j:
                    continue
                if (
                    percent
                    and b
                    and min(abs(100 * a / b - value), abs(abs(100 * (a - b) / b) - value)) <= within
                ):
                    return True
                if not percent and min(abs(a + b - value), abs(abs(a - b) - value)) <= within:
                    return True
    return False


def _earlier(question: str, history: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """The numbers the question and the earlier turns allow (the questions' own, and the earlier answers'
    but those their checks listed), and the NeuroDB links the earlier answers gave (as they were sent: the
    latest whole, the first paragraph of older ones)."""
    from neurodb.fmm.ai import citations
    from neurodb.review.services import numbers_in
    from neurodb.watch import grounding

    written = [question]
    urls: set[str] = set()
    for turn in history:
        written.append(str(turn.get("question") or ""))
        answer = str(turn.get("answer") or "")
        listed_ = {grounding._norm(str(number)) for number in turn.get("unchecked") or ()}
        written += [number for number in numbers_in(answer) if grounding._norm(number) not in listed_]
        for match in citations.LINK.finditer(answer):
            if privacy.SITE_PATH.fullmatch(match.group(2)):
                urls.add(match.group(2))
    return grounding._allowed_numbers(" ".join(written)), urls


def _as_written(text: str, number: str, scale: str = "") -> str:
    """``number`` (without separators) as ``text`` writes it: with its thousands separators, and its % or
    its scale word ("12,345", "61.3%", "13.4 million")."""
    from neurodb.watch import grounding

    for match in WRITTEN.finditer(text):
        written = match.group(0).rstrip(",")
        if grounding._norm(written) != number:
            continue
        percent = PERCENT.match(text, match.start())
        if percent and percent.group(1) == match.group(0):
            return f"{written}%"
        after = text[match.start() + len(written) :].lstrip()
        return f"{written} {scale}" if scale and after.lower().startswith(scale.lower()) else written
    return number


def check(
    answer: str, looked: Looked, question: str = "", history: Iterable[Mapping[str, Any]] = ()
) -> Checked:
    """``answer`` checked against what this answer's lookups returned (``looked``), the question and the
    earlier turns of the conversation (``history``: each turn's question, answer and, read back from the
    log, the figures its check listed: ``unchecked``). A figure in the text of a link that stays is checked
    too; a percentage must be a percentage or a share looked up, never just any count."""
    from neurodb.fmm.ai import citations
    from neurodb.review.services import numbers_in
    from neurodb.watch import grounding

    from . import agent

    earlier, earlier_urls = _earlier(question, history)
    counts, percents_allowed = earlier | looked.numbers, earlier | looked.percents
    urls = looked.urls | earlier_urls
    removed = 0

    def link(match: re.Match) -> str:
        nonlocal removed
        if match.group(2) in urls:
            return match.group(0)
        removed += 1
        return match.group(1)

    def anchor(match: re.Match) -> str:
        # The last guard, on the HTML shown: a link the Markdown check did not see (a reference link, raw
        # HTML) stays only on the same terms. render() has already taken out every link off NeuroDB.
        nonlocal removed
        href = citations.HREF.search(match.group(1))
        url = html.unescape(href.group(1)) if href else ""
        if url and url in urls:
            return match.group(0)
        removed += 1
        return match.group(2)

    text = citations.LINK.sub(link, str(answer or ""))
    shown = citations.ANCHOR.sub(anchor, agent.render(text))
    read = citations.LINK.sub(lambda match: match.group(1), text)  # a kept link's text is checked too
    # the figures written only as percentages ("45 reports, 45%": 45 is a count looked up)
    percents = {match.group(1).replace(",", "").rstrip(".") for match in PERCENT.finditer(read)}
    percents -= numbers_in(PERCENT.sub(" ", read))
    scaled = {match.group(1).replace(",", "").rstrip("."): match.group(2) for match in SCALED.finditer(read)}
    shares, totals, sentences = sorted(looked.shares), sorted(looked.totals), _side_by_side(read)
    values = sorted(looked.values | looked.totals)
    unchecked = []
    for written in citations._unchecked(read, set(), aside=_aside):
        number = grounding._norm(written)
        if written in percents:
            if float(written) == 100 or number in percents_allowed:  # 100%: the whole
                continue
            if _worked_out(written, True, shares, sentences):
                continue
        else:
            if number in counts:
                continue
            word = scaled.get(written)
            if word and _near(
                float(written) * SCALES[word.lower()], values, _within(written, SCALES[word.lower()])
            ):
                continue
            if _worked_out(written, False, totals, sentences):
                continue
        unchecked.append(written)
    return Checked(
        text=text,
        html=shown,
        unchecked_numbers=unchecked,
        links_removed=removed,
        shown=[_as_written(read, grounding._norm(written), scaled.get(written, "")) for written in unchecked],
    )
