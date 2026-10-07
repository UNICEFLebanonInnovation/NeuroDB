"""The document review's work without AI: cutting a document into the parts the AI reads (with page
markers), finding each finding's quote in the text to pin it to its page, matching its place to the
gazetteer, reading its date, and its evidence score.

- **Parts** (:func:`parts`): about ``chunk_size`` characters, cut between pages when it can (a longer page
  is cut between paragraphs); each page starts with a marker the AI sees (``[Page 4]``, ``[Slide 2]``,
  ``[Sheet 'Budget']``) and the part knows its first and last page. A part whose reply is broken is
  read again as two halves (:func:`halves`).
- **Where** (:class:`TextIndex`): the quote is looked for in the whole text compared as words (case,
  accents, punctuation and line breaks ignored; a quote shortened with "…" by its pieces). Found: its
  exact page ("p. 12", "slide 4", "sheet 'Budget'"). Not found: the range of the part it came from.
- **Places** (:class:`Gazetteer`): the governorates and districts of NeuroDB's maps, whatever the
  spelling ("Baalbek - El Hermel", "Nabatieh Governorate"), also inside a longer phrase ("Halba, Akkar");
  a country-wide mention is *Lebanon (country-wide)*; a name not recognised is kept as written and
  flagged.
- **Dates** (:func:`period`): "2024", "Q3 2025", "H1 2024", "March 2025", "end of 2025", "2024-2025",
  "2025-06-30" or "30/06/2025"; a deadline takes the period's last day, a finding its first.
- **Evidence** (:func:`evidence`): computed, never the model's: the quote found in the text 45, reports
  rather than interprets 25, dated 10, placed 10, tagged (not Other) 10.
"""

from __future__ import annotations

import bisect
import calendar
import datetime
import re
from dataclasses import dataclass, field

from .text import PAGE_BREAK

PAGE, SLIDE, SHEET = "page", "slide", "sheet"
QUOTE_FOUND, REPORTED, DATED, PLACED, TAGGED = 45, 25, 10, 10, 10
MIN_PIECE_WORDS = 5  # a piece of a quote shortened with "…" counts when this long


# ------------------------------------------------------------------------------------------ pages
def page_kind(document) -> str | None:
    """What a document's pages are: slides, sheets or pages; None when it has none (Word, text)."""
    name = (document.file.name if document.file else "").lower()
    if name.endswith(".pptx"):
        return SLIDE
    if name.endswith(".xlsx"):
        return SHEET
    if PAGE_BREAK in (document.text or "") or name.endswith(".pdf"):
        return PAGE
    return None


def sheet_name(page_text: str) -> str:
    """The sheet's name, from the line the text of a workbook starts each sheet with."""
    first = page_text.lstrip().split("\n", 1)[0]
    return first[len("Sheet: ") :].strip() if first.startswith("Sheet: ") else ""


def marker(kind: str | None, number: int | None, page_text: str = "") -> str:
    if kind is None or number is None:
        return ""
    if kind == SLIDE:
        return f"[Slide {number}]"
    if kind == SHEET:
        name = sheet_name(page_text)
        return f"[Sheet '{name}']" if name else f"[Sheet {number}]"
    return f"[Page {number}]"


def label(kind: str | None, first: int | None, last: int | None, sheets: dict[int, str] | None = None) -> str:
    """ "p. 12", "pp. 12-14", "slide 4", "slides 4-6", "sheet 'Budget'"; "" without pages."""
    if kind is None or first is None:
        return ""
    last = last if last is not None else first
    if kind == SHEET:
        names = sheets or {}
        one = f"sheet '{names[first]}'" if names.get(first) else f"sheet {first}"
        if last == first:
            return one
        other = f"'{names[last]}'" if names.get(last) else str(last)
        return f"sheets {one[len('sheet ') :]}–{other}"
    if kind == SLIDE:
        return f"slide {first}" if last == first else f"slides {first}–{last}"
    return f"p. {first}" if last == first else f"pp. {first}–{last}"


def sheet_names(document) -> dict[int, str]:
    if page_kind(document) != SHEET:
        return {}
    return {n: sheet_name(page) for n, page in enumerate(document.text.split(PAGE_BREAK), start=1)}


# ------------------------------------------------------------------------------------------ parts
@dataclass
class Part:
    """What one AI call reads: pieces of pages, each (page number or None, text)."""

    number: int
    kind: str | None
    segments: list[tuple[int | None, str]] = field(default_factory=list)

    @property
    def first(self) -> int | None:
        return self.segments[0][0] if self.segments else None

    @property
    def last(self) -> int | None:
        return self.segments[-1][0] if self.segments else None

    @property
    def chars(self) -> int:
        return sum(len(text) for _page, text in self.segments)

    @property
    def text(self) -> str:
        out, previous = [], object()
        for page, text in self.segments:
            if page != previous and self.kind is not None:
                out.append(marker(self.kind, page, text))
            out.append(text)
            previous = page
        return "\n".join(out)


def _cut(text: str, size: int) -> list[str]:
    """``text`` in pieces of at most about ``size`` characters, cut between paragraphs, lines or
    sentences when one is near."""
    pieces = []
    while len(text) > size:
        window = text[:size]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        cut = cut + 1 if cut > size // 2 else size
        pieces.append(text[:cut].strip())
        text = text[cut:].lstrip()
    if text.strip():
        pieces.append(text.strip())
    return [p for p in pieces if p]


def parts(document, size: int) -> list[Part]:
    """The document in parts of about ``size`` characters, cut between pages when it can."""
    kind = page_kind(document)
    pages = (document.text or "").split(PAGE_BREAK)
    out: list[Part] = []
    current = Part(1, kind)
    for number, page in enumerate(pages, start=1):
        page = page.strip()
        if not page:
            continue
        number_ = number if kind is not None else None
        for piece in _cut(page, size):
            if current.segments and current.chars + len(piece) > size:
                out.append(current)
                current = Part(len(out) + 1, kind)
            current.segments.append((number_, piece))
    if current.segments:
        out.append(current)
    return out


def halves(part: Part) -> list[Part]:
    """A part cut in two (between pages when it holds several, else in its middle), for a second try."""
    if len(part.segments) > 1:
        middle = len(part.segments) // 2
        first, second = part.segments[:middle], part.segments[middle:]
    else:
        page, text = part.segments[0]
        pieces = _cut(text, max(len(text) // 2 + 1, 200))
        if len(pieces) < 2:
            return []
        first, second = [(page, pieces[0])], [(page, " ".join(pieces[1:]))]
    return [Part(part.number, part.kind, first), Part(part.number, part.kind, second)]


# ------------------------------------------------------------------------------------------ the quote
def fold(text: str) -> str:
    """Words only, lower case, without accents (as the quality rules compare texts)."""
    from neurodb.fmm.parse import fold as fold_

    return fold_(text)


class TextIndex:
    """A document's text folded to words, with where each page starts, to find quotes in it."""

    def __init__(self, document):
        self.kind = page_kind(document)
        self.starts: list[int] = []
        chunks = []
        position = 0
        for page in (document.text or "").split(PAGE_BREAK):
            folded = fold(page)
            self.starts.append(position)
            chunks.append(folded)
            position += len(folded) + 1
        self.text = " ".join(chunks)

    def page_at(self, position: int) -> int:
        return bisect.bisect_right(self.starts, position)

    def find(self, quote: str, near: int | None = None) -> int | None:
        """The page the quote is on (1 for a document without pages), or None when it is not in the text.
        ``near``: the first page of the part it came from, where it is looked for first."""
        pieces = [fold(p) for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote or "")]
        pieces = [p for p in pieces if p]
        if not pieces:
            return None
        whole = fold(quote)
        if len(pieces) == 1 or len(whole.split()) < MIN_PIECE_WORDS:
            wanted = whole
        else:  # shortened with "…": the longest piece stands for it, when long enough
            wanted = max(pieces, key=len)
            if len(wanted.split()) < MIN_PIECE_WORDS:
                return None
        if len(wanted) < 12:
            return None
        start = self.starts[near - 1] if near and 0 < near <= len(self.starts) else 0
        at = self._word_find(wanted, start)
        if at is None and start:
            at = self._word_find(wanted, 0)
        return None if at is None else self.page_at(at)

    def _word_find(self, wanted: str, start: int) -> int | None:
        """Where ``wanted`` starts as whole words ("rate of 5" is not in "rate of 50"); None if nowhere."""
        at = self.text.find(wanted, start)
        while at >= 0:
            end = at + len(wanted)
            if (at == 0 or self.text[at - 1] == " ") and (end == len(self.text) or self.text[end] == " "):
                return at
            at = self.text.find(wanted, at + 1)
        return None


# ------------------------------------------------------------------------------------------ places
GENERAL = {
    "lebanon", "national", "nationwide", "nation wide", "country wide", "countrywide", "all lebanon",
    "all of lebanon", "across lebanon", "whole country", "all governorates", "national level",
}  # fmt: skip
_AREA_WORDS = re.compile(r"\b(governorate|mohafaza|mohafazat|district|caza|qada|kada|el|al)\b")
AMBIGUOUS = {"north", "south", "centre", "center", "mount"}


def area_key(name: str) -> str:
    """A governorate's or district's name as compared: "Baalbek - El Hermel" and "Baalbek-Hermel"
    agree, "Nabatieh Governorate" and "El Nabatieh" too."""
    from neurodb.reports.overview import governorate_key

    return governorate_key(_AREA_WORDS.sub(" ", fold(name)))


@dataclass
class Place:
    match: str  # "", general, governorate, district, unmatched
    governorate_id: int | None = None
    governorate_name: str = ""
    district_id: int | None = None
    district_name: str = ""


class Gazetteer:
    """NeuroDB's governorates and districts (the maps' areas), read once per run."""

    def __init__(self):
        from neurodb.geo.models import DistrictLocation, GovernorateLocation

        self.governorates = [
            (pk, name, code, area_key(name))
            for pk, name, code in GovernorateLocation.objects.values_list("pk", "name", "code")
            if name
        ]
        self.by_code = {code: (pk, name) for pk, name, code, _key in self.governorates}
        self.districts = [
            (pk, name, gov_code, area_key(name))
            for pk, name, gov_code in DistrictLocation.objects.values_list("pk", "name", "gov_code")
            if name
        ]

    def _district(self, pk: int, name: str, gov_code: str) -> Place:
        gov_id, gov_name = self.by_code.get(gov_code, (None, ""))
        return Place("district", gov_id, gov_name, pk, name)

    def match(self, written: str) -> Place:
        written = " ".join((written or "").split())
        if not written:
            return Place("")
        folded = fold(written)
        if folded in GENERAL:
            return Place("general")
        key = area_key(written)
        # the whole name first (a district before a governorate of the same name, e.g. Akkar)
        for pk, name, gov_code, district_key in self.districts:
            if key and key == district_key:
                return self._district(pk, name, gov_code)
        for pk, name, _code, gov_key in self.governorates:
            if key and key == gov_key:
                return Place("governorate", pk, name)
        # then a known name inside a longer phrase ("Halba, Akkar"; "North" only as "North Lebanon")
        padded = f" {folded} "
        for pk, name, gov_code, _key in sorted(self.districts, key=lambda d: -len(d[1])):
            words = fold(name)
            if len(words) >= 4 and words not in AMBIGUOUS and f" {words} " in padded:
                return self._district(pk, name, gov_code)
        for pk, name, _code, _key in sorted(self.governorates, key=lambda g: -len(g[1])):
            words = fold(name)
            if len(words) < 4:
                continue
            if words in AMBIGUOUS:
                if re.search(rf" {re.escape(words)} (lebanon|governorate) ", padded):
                    return Place("governorate", pk, name)
            elif f" {words} " in padded:
                return Place("governorate", pk, name)
        if any(f" {general} " in padded for general in ("lebanon", "nationwide", "national")):
            return Place("general")
        return Place("unmatched")


# ------------------------------------------------------------------------------------------ dates
_MONTHS = {name.lower(): n for n, name in enumerate(calendar.month_name) if name} | {
    name.lower(): n for n, name in enumerate(calendar.month_abbr) if name
} | {"sept": 9}  # fmt: skip
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
YEARS = range(1990, 2101)


def _bounds(year: int, first_month: int, last_month: int, end: bool) -> datetime.date | None:
    if year not in YEARS:
        return None
    if end:
        return datetime.date(year, last_month, calendar.monthrange(year, last_month)[1])
    return datetime.date(year, first_month, 1)


def period(text: str, end: bool = False) -> datetime.date | None:
    """The date a text names (``end``: the last day of the period it names, else the first)."""
    text = " ".join((text or "").lower().replace(",", " ").split())
    if not text:
        return None
    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        try:
            return datetime.date(int(m[1]), int(m[2]), int(m[3])) if int(m[1]) in YEARS else None
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text):  # day first, as written in Lebanon
        try:
            return datetime.date(int(m[3]), int(m[2]), int(m[1])) if int(m[3]) in YEARS else None
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})\s+([a-z]{3,9})\s+(\d{4})\b", text):
        month = _MONTHS.get(m[2])
        if month and int(m[3]) in YEARS:
            try:
                return datetime.date(int(m[3]), month, int(m[1]))
            except ValueError:
                return None
    if m := re.search(r"\b([a-z]{3,9})\s+(\d{1,2})\s+(\d{4})\b", text):  # June 30 2025
        month = _MONTHS.get(m[1])
        if month and int(m[3]) in YEARS:
            try:
                return datetime.date(int(m[3]), month, int(m[2]))
            except ValueError:
                return None
    if m := re.search(r"\b(\d{4})-(\d{2})\b(?!-)", text):  # 2025-06
        if 1 <= int(m[2]) <= 12:
            return _bounds(int(m[1]), int(m[2]), int(m[2]), end)
    if m := re.search(r"\bq([1-4])\s*(\d{4})\b|\b(\d{4})\s*q([1-4])\b", text):
        quarter, year = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        return _bounds(year, quarter * 3 - 2, quarter * 3, end)
    if m := re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th) quarter (?:of )?(\d{4})\b", text):
        quarter = _QUARTER_WORDS[m[1]]
        return _bounds(int(m[2]), quarter * 3 - 2, quarter * 3, end)
    if m := re.search(r"\bh([12])\s*(\d{4})\b|\b(first|second) half (?:of )?(\d{4})\b", text):
        half = int(m[1]) if m[1] else (1 if m[3] == "first" else 2)
        return _bounds(int(m[2] or m[4]), 1 if half == 1 else 7, 6 if half == 1 else 12, end)
    if m := re.search(r"\b([a-z]{3,9})\s+(\d{4})\b", text):
        month = _MONTHS.get(m[1])
        if month:
            return _bounds(int(m[2]), month, month, end)
    if m := re.search(r"\b(\d{4})\s*[-–/]\s*(\d{2,4})\b", text):  # 2024-2025, 2024/25
        first = int(m[1])
        second = int(m[2]) if len(m[2]) == 4 else first // 100 * 100 + int(m[2])
        if second >= first:
            return _bounds(second if end else first, 1, 12, end)
    if m := re.search(r"\b(\d{4})\b", text):
        return _bounds(int(m[1]), 1, 12, end)
    return None


# ------------------------------------------------------------------------------------------ evidence
def evidence(*, quote_found: bool, reported: bool, dated: bool, placed: bool, tagged: bool) -> int:
    return QUOTE_FOUND * quote_found + REPORTED * reported + DATED * dated + PLACED * placed + TAGGED * tagged


def locate(finding, index: TextIndex, gazetteer: Gazetteer, sheets: dict[int, str], other_id: int) -> None:
    """Fill in where a finding is (page, place, date) and its evidence; no AI, nothing saved."""
    page = index.find(finding.quote, finding.chunk_from)
    paged = index.kind is not None
    finding.quote_found = page is not None
    if page is not None and paged:
        finding.page_from = finding.page_to = page
        finding.exact_page = True
    elif not finding.manual or finding.page_from is None:  # a person's page stays when not found
        finding.page_from, finding.page_to = finding.chunk_from, finding.chunk_to
        finding.exact_page = False
    finding.page_label = label(index.kind, finding.page_from, finding.page_to, sheets)
    place = gazetteer.match(finding.place_text)
    finding.place_match = place.match
    finding.governorate_id, finding.governorate_name = place.governorate_id, place.governorate_name[:100]
    finding.district_id, finding.district_name = place.district_id, place.district_name[:100]
    finding.finding_date = period(finding.date_text)
    finding.evidence = evidence(
        quote_found=finding.quote_found,
        reported=finding.kind == "reported",
        dated=finding.finding_date is not None,
        placed=place.match in ("general", "governorate", "district"),
        tagged=finding.topic_id is not None and finding.topic_id != other_id,
    )
