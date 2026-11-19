"""Ask NeuroDB's check of its answer before it is shown and kept: Monitoring insights' check of a chat
answer (:mod:`neurodb.fmm.ai.citations`), against what the lookups of this answer returned.

- **Links**: a link stays only when a lookup of this answer returned its url (one of NeuroDB's pages under
  a url key: "url", "urls", "..._url"), or an earlier answer of the conversation gave it; otherwise its
  text stays and the link goes. The rendered answer gets the same check, so a link written another way (a
  reference link, raw HTML) cannot slip past it.
- **Figures**: as in Monitoring insights, references ("LEB/PCA2026001/PD2026001"), dates ("13 Sep 2026",
  "2026-09-13", "Oct 2026"), link texts and whole numbers up to 10 are set aside, and so are years ("2026",
  "2025/26"), page numbers ("p. 41", "pages 3-4") and the addresses of links written another way (raw
  HTML, a reference link, a bare address). Every other number must be one of:

  - a number written anywhere in a lookup's result, or that number rounded to one decimal or to a whole
    number;
  - a number of the question, or of an earlier question of the conversation;
  - a number of an earlier answer of the conversation that its own check did not list;
  - a percentage worked out from one lookup: one figure over another of the same small row (up to
    ``ROW_FIGURES`` figures, such as reached and target), a row's figure over the total of its column in
    a list, or a fraction given as a percentage (0.25 as 25%);
  - a total worked out from one lookup: the figures of a small row added, a column of a list added up, or
    the number of items in a list;
  - two figures written in the same sentence or table row of the answer: one over the other or the change
    from one to the other as a percentage, the two added or the difference ("1,200 of 1,500 (80.0%)",
    "1,200 and 450, 1,650 in all", "from 12,400 to 15,900, a rise of 3,500"); those two figures are
    checked themselves.

  Only these simple sums and shares count: over every pair of figures of a large lookup, almost any
  percentage could be "worked out", and the check would find nothing. The others are listed under the
  answer, in Monitoring insights' words: "These figures could not be checked against the data: 4950."
  The figures are never changed and nothing is held back. A number written without a thousands
  separator that looks like a year ("2000 children") is taken for a year.
- **The question log**: when the check listed a figure or took a link out, what it found
  (:attr:`Checked.entry`) is kept as the last entry of the question's lookups (``AssistantQuestion.tools``):
  ``{"check": "answer", "notice", "unchecked_numbers", "links_removed"}``. A follow-up reads it back
  (:func:`listed`), so a figure listed once is listed again when it is repeated.

People are not looked for in the answer: the question, the earlier turns and every lookup's result reach
the AI without them (:mod:`.privacy`). Help, Monitoring insights and NeuroDB Watch run the same loop with
their own options: their answers are not checked here (Monitoring insights checks its own).
"""

from __future__ import annotations

import bisect
import html
import itertools
import json
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
# The end of a sentence: the answer's sentences and table rows are where two figures are worked out
SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n")


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


@dataclass
class Looked:
    """What the lookups of one answer returned, for the check: NeuroDB's page links (``urls``), every
    number written in their results (``numbers``, in ``watch.grounding``'s form with its roundings), and
    the percentages (``shares``) and totals (``totals``) worked out from their figures."""

    urls: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    shares: set[float] = field(default_factory=set)
    totals: set[float] = field(default_factory=set)

    def add(self, result: Any) -> None:
        """One lookup's result, as the model reads it. Its numbers are read from its JSON written with a space
        between items: the compact text the model is sent runs a list of figures together ("[1200,300]")."""
        from neurodb.watch import grounding

        self.numbers |= grounding._allowed_numbers(json.dumps(result, ensure_ascii=False, default=str))
        self._walk(result, "")

    def _walk(self, value: Any, key: str) -> None:
        if isinstance(value, str) and privacy._url_key(key) and privacy.SITE_PATH.fullmatch(value):
            self.urls.add(value)
            return
        number = _figure(value)
        if number is not None:
            if 0 < abs(number) < 1:  # a fraction, given as a percentage
                self._share(100 * number)
            return
        if isinstance(value, Mapping):
            row = []
            for inner_key, inner in value.items():
                self._walk(inner, str(inner_key))
                figure = _figure(inner)
                if figure is not None:
                    row.append(figure)
            if len(row) <= ROW_FIGURES:
                self._row(row)
        elif isinstance(value, list | tuple):
            for inner in value:
                self._walk(inner, key)
            if value:
                self._total(len(value))
            columns: dict[str, list[float]] = {}
            for inner in value:
                cells = inner.items() if isinstance(inner, Mapping) else [("", inner)]
                for column, cell in cells:
                    figure = _figure(cell)
                    if figure is not None:
                        columns.setdefault(str(column), []).append(figure)
            for figures in columns.values():
                self._column(figures)

    def _row(self, row: list[float]) -> None:
        """The figures of a small row: each over another, and two or more of them added."""
        for i, a in enumerate(row):
            for j, b in enumerate(row):
                if i != j and b:
                    self._share(100 * a / b)
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
    """An answer after the check: its text and HTML, the figures no lookup returned (as written, without
    thousands separators) and the number of links taken out."""

    text: str
    html: str
    unchecked_numbers: list[str] = field(default_factory=list)
    links_removed: int = 0

    @property
    def notice(self) -> str:
        """The line under the answer: the figures no lookup returned; "" when there are none."""
        from neurodb.fmm.ai import citations

        return citations.figures_line(self.unchecked_numbers)

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


def _side_by_side(text: str) -> list[list[tuple[str, float]]]:
    """The figures written in each sentence or table row of ``text``: (as written, value)."""
    from neurodb.review.services import numbers_in

    return [
        [(number, float(number)) for number in numbers_in(part)] for part in SENTENCE_END.split(text) if part
    ]


def _worked_out(
    written: str, percent: bool, found: list[float], sentences: list[list[tuple[str, float]]]
) -> bool:
    """``written`` is a percentage or a total worked out from one lookup's figures (``found``: the
    lookups' shares or totals, sorted), or a share, change, sum or difference of two figures written beside
    it, to the precision it is written with (77% for 76.92; 1,650 for 1,649.6)."""
    value = float(written)
    places = len(written.partition(".")[2])
    within = 0.5 * 10**-places + 1e-9 * max(1.0, abs(value))
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
                if percent and b and min(abs(100 * a / b - value), abs(100 * (a - b) / b - value)) <= within:
                    return True
                if not percent and min(abs(a + b - value), abs(a - b - value)) <= within:
                    return True
    return False


def _earlier(question: str, history: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """The numbers the question and the earlier turns allow (the questions' own, and the earlier answers'
    but those their checks listed), and the NeuroDB links the earlier answers gave."""
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


def check(
    answer: str, looked: Looked, question: str = "", history: Iterable[Mapping[str, Any]] = ()
) -> Checked:
    """``answer`` checked against what this answer's lookups returned (``looked``), the question and the
    earlier turns of the conversation (``history``: each turn's question, answer and, read back from the
    log, the figures its check listed: ``unchecked``)."""
    from neurodb.fmm.ai import citations

    from . import agent

    allowed, earlier_urls = _earlier(question, history)
    allowed |= looked.numbers
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
    percents = {match.group(1).replace(",", "").rstrip(".") for match in PERCENT.finditer(text)}
    shares, totals, sentences = sorted(looked.shares), sorted(looked.totals), _side_by_side(text)
    unchecked = [
        written
        for written in citations._unchecked(text, allowed, aside=_aside)
        if not _worked_out(written, written in percents, shares if written in percents else totals, sentences)
    ]
    return Checked(text=text, html=shown, unchecked_numbers=unchecked, links_removed=removed)
