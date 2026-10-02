"""Where the words of a PDF page are, for the periodic reports: the page laid out as on paper (so that
an indicator, its achievement and its target stay on one line) and the charts of values over dates
(each value label paired with the date label under it).

A chart is recognised by its date axis: four or more date labels ("14 April", "29 Sept", "17 Apr…")
side by side. The numbers drawn above an axis are its values, matched to the dates from left to
right; numbers left of the first date are the axis ticks. The chart's name is its legend (the text
repeated within the chart), its group the page heading. Labels drawn twice at the same place (a
shadow) count once.
"""

from __future__ import annotations

import calendar
import datetime
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

MIN_DATES = 4  # date labels side by side that make a chart axis
AXIS_GAP = 30  # points between two axes (charts stacked on a page)
CUT_NEAREST_DAYS = 45  # a cut date at an end of an axis is at most this far from its neighbour
LAYOUT_CHARS = 12000  # of one page, for the AI
PICTURE_WORDS = 12  # fewer words than this besides the header: the page is pictures
MONTHS = {name.lower()[:3]: n for n, name in enumerate(calendar.month_name) if name}
_DATE = re.compile(r"^(\d{1,2})(?:[\s\-/]+([A-Za-z]{3,9})\.?(?:[\s\-/,]+(\d{4}))?)?\s*(…|\.\.\.)?$")
_NUMBER = re.compile(r"^[+-]?\d[\d,]*(?:\.\d+)?\s*[KkMm%]?$")


@dataclass
class Item:
    text: str
    x: float
    y: float
    rotated: bool


@dataclass
class ChartSeries:
    page: int
    group: str
    metric: str
    source: str = ""
    internal: bool = False
    points: list[tuple[datetime.date, Decimal, str]] = field(default_factory=list)  # date, value, label
    dropped: int = 0  # values that could not be given a date


def _mult(m: list[float], n: list[float]) -> list[float]:
    return [
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    ]


def page_items(page) -> list[Item]:
    """The text pieces of a page with their position (points from the bottom left), once each."""
    found: dict[tuple, Item] = {}

    def visit(text, cm, tm, font_dict, font_size):
        text = (text or "").strip()
        if not text:
            return
        m = _mult(list(tm), list(cm))
        x, y = round(m[4], 1), round(m[5], 1)
        found.setdefault((x, y, text), Item(text, x, y, rotated=abs(m[1]) > 0.01 or abs(m[2]) > 0.01))

    try:
        page.extract_text(visitor_text=visit)
    except Exception:  # noqa: BLE001 - an unreadable page has no charts
        logger.warning("could not read the text positions of a page", exc_info=True)
        return []
    return list(found.values())


def open_pdf(data: bytes):
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # "rotated text discovered" on every chart page
    return PdfReader(io.BytesIO(data))


def layout_pages(reader) -> list[str]:
    """Each page's text laid out as on the page (columns kept apart by spaces)."""
    out = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:  # noqa: BLE001 - one unreadable page does not lose the others
            text = page.extract_text() or ""
        lines = [line.rstrip() for line in text.splitlines()]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
        out.append(text[:LAYOUT_CHARS])
    return out


def picture_pages(reader, charts: list[ChartSeries]) -> list[int]:
    """Pages that are pictures with hardly any text besides the report's header, and no chart read:
    e.g. charts saved as images, whose figures cannot be read from the text."""
    read = {c.page for c in charts}
    texts = []
    for page in reader.pages:
        try:
            texts.append(((page.extract_text() or "").splitlines(), len(page.images)))
        except Exception:  # noqa: BLE001 - an unreadable page is not reported as pictures
            logger.warning("could not read a page's text or pictures", exc_info=True)
            texts.append(([], 0))
    counts = Counter(line.strip() for lines, _ in texts for line in set(lines))
    header = {line for line, n in counts.items() if n >= max(2, len(texts) // 2)}
    out = []
    for n, (lines, pictures) in enumerate(texts, start=1):
        words = sum(len(line.split()) for line in lines if line.strip() not in header)
        if pictures and words < PICTURE_WORDS and n not in read:
            out.append(n)
    return out


# ------------------------------------------------------------------------------------- charts
def number(text: str) -> Decimal | None:
    """ "12,345" -> 12345, "82%" -> 82, "13K" -> 13000; None when it is not a number."""
    raw = text.strip().replace(" ", "")
    if not _NUMBER.match(raw):
        return None
    scale = 1
    if raw[-1] in "Kk":
        scale, raw = 1000, raw[:-1]
    elif raw[-1] in "Mm":
        scale, raw = 1_000_000, raw[:-1]
    elif raw[-1] == "%":
        raw = raw[:-1]
    try:
        return Decimal(raw.replace(",", "").lstrip("+")) * scale
    except InvalidOperation:
        return None


def _date_label(text: str) -> tuple[int, int | None, int | None] | None:
    """(day, month or None, year or None) of an axis label; None when it is not one. A day alone
    counts only when the label was cut ("08…")."""
    match = _DATE.match(text.strip())
    if not match:
        return None
    day, month_name, year, cut = match.groups()
    day = int(day)
    if not 1 <= day <= 31:
        return None
    if month_name is None:
        return (day, None, None) if cut else None
    month = MONTHS.get(month_name.lower()[:3])
    if month is None:
        return None
    return day, month, int(year) if year else None


def _year(month: int, issued: datetime.date) -> int:
    """A date without a year is in the twelve months up to the issue date."""
    return issued.year if month <= issued.month else issued.year - 1


def _safe_date(year: int, month: int, day: int) -> datetime.date | None:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _resolve(
    labels: list[tuple[int, int | None, int | None]], issued: datetime.date, known: dict[int, set[int]]
) -> list[datetime.date | None]:
    """The dates of an axis, left to right. A cut label ("08…") takes the month that keeps the dates
    in order, helped by the full labels of the other charts (``known``: day -> months). A cut label
    at an end of the axis, with a date on one side only, takes the month nearest to that date (at
    most CUT_NEAREST_DAYS away). Otherwise it stays unknown."""
    dates: list[datetime.date | None] = [
        _safe_date(year or _year(month, issued), month, day) if month else None for day, month, year in labels
    ]

    def options(i: int) -> tuple[list[datetime.date], datetime.date | None, datetime.date | None]:
        before = next((d for d in reversed(dates[:i]) if d), None)
        after = next((d for d in dates[i + 1 :] if d), None)
        fits = []
        for m in range(1, 13):
            candidate = _safe_date(_year(m, issued), m, labels[i][0])
            if candidate and (not before or candidate > before) and (not after or candidate < after):
                fits.append(candidate)
        return fits, before, after

    progress = True
    while progress:
        progress = False
        for i in (k for k, d in enumerate(dates) if d is None):  # one fitting month (or one known)
            fits, _, _ = options(i)
            preferred = [c for c in fits if c.month in known.get(labels[i][0], set())]
            choice = preferred if len(preferred) == 1 else fits
            if len(choice) == 1:
                dates[i], progress = choice[0], True
        if progress:
            continue
        for i in (k for k, d in enumerate(dates) if d is None):  # an end of the axis: the nearest month
            fits, before, after = options(i)
            side = before if after is None else after if before is None else None
            if side and fits:
                nearest = min(fits, key=lambda c: abs((c - side).days))
                if abs((nearest - side).days) <= CUT_NEAREST_DAYS:
                    dates[i], progress = nearest, True
                    break
    return dates


def _heading(items: list[Item], repeated: set[str]) -> tuple[str, str, bool]:
    """(group, source, internal) from the page heading, e.g. "Trends of IDPs outside Shelters - IOM -
    Internal use" -> ("IDPs outside Shelters", "IOM", True)."""
    texts = [
        i for i in items if not i.rotated and i.text not in repeated and re.search(r"[A-Za-z]{3}", i.text)
    ]
    if not texts:
        return "", "", False
    top = max(texts, key=lambda i: i.y)
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", top.text) if p.strip()]
    internal = any("internal" in p.lower() for p in parts)
    rest = [p for p in parts[1:] if "internal" not in p.lower() and "public" not in p.lower()]
    group = re.sub(r"^trends?\s+(of\s+)?", "", parts[0], flags=re.I).strip() if parts else ""
    return group[:200], " - ".join(p for p in rest if len(p) <= 40)[:200], internal


def repeated_texts(pages: list[list[Item]]) -> set[str]:
    """Texts on most pages (the report's header and footer)."""
    if len(pages) < 2:
        return set()
    counts = Counter(t for items in pages for t in {i.text for i in items})
    return {t for t, n in counts.items() if n >= max(2, len(pages) // 2)}


def _axes(items: list[Item]) -> list[list[tuple[Item, tuple]]]:
    """The date axes of a page, top first: date labels grouped by height."""
    labels = [(i, parsed) for i in items if (parsed := _date_label(i.text))]
    labels.sort(key=lambda p: -p[0].y)
    axes: list[list[tuple[Item, tuple]]] = []
    for label in labels:
        if axes and axes[-1][-1][0].y - label[0].y <= AXIS_GAP:
            axes[-1].append(label)
        else:
            axes.append([label])
    return [sorted(a, key=lambda p: p[0].x) for a in axes if len(a) >= MIN_DATES]


LABEL_OFFSET = 5  # points a value label sits right of its date label


def _ticks_edge(numbers: list[Item], first_date_x: float) -> float:
    """The right edge of the axis ticks: the numbers stacked in one column left of the first date
    (values are never stacked: there is one per date)."""
    left = [i for i in numbers if i.x < first_date_x + 2]
    ticks = [i for i in left if sum(abs(j.x - i.x) <= 1.5 and j.text != i.text for j in left) >= 1]
    return max(i.x for i in ticks) + 1 if ticks else first_date_x - 20


def read_charts(reader, issued: datetime.date) -> list[ChartSeries]:
    """Every chart of values over dates in the document."""
    pages = [page_items(page) for page in reader.pages]
    repeated = repeated_texts(pages)
    known: dict[int, set[int]] = {}
    for items in pages:
        for item in items:
            parsed = _date_label(item.text)
            if parsed and parsed[1]:
                known.setdefault(parsed[0], set()).add(parsed[1])
    out: list[ChartSeries] = []
    for number_, items in enumerate(pages, start=1):
        axes = _axes(items)
        if not axes:
            continue
        group, source, internal = _heading(items, repeated)
        tops = [None, *[axis[0][0].y for axis in axes[:-1]]]  # each chart reaches up to the axis above
        for axis, top in zip(axes, tops, strict=True):
            base = min(i.y for i, _ in axis)
            inside = [i for i in items if i.y > base + 2 and (top is None or i.y < top - 2) and not i.rotated]
            numbers = [i for i in inside if number(i.text) is not None and not _date_label(i.text)]
            values = sorted(
                (i for i in numbers if i.x > _ticks_edge(numbers, axis[0][0].x)), key=lambda i: i.x
            )
            names = Counter(
                i.text
                for i in inside
                if i.text not in repeated
                and number(i.text) is None
                and not _date_label(i.text)
                and re.search(r"[A-Za-z]{3}", i.text)
                and i.text.lower() not in ("date", "dates")
            )
            metric = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] if names else group
            dates = _resolve([p for _, p in axis], issued, known)
            series = ChartSeries(number_, group or metric, metric, source, internal)
            xs = [i.x for i, _ in axis]
            if len(values) == len(axis):
                pairs = list(zip(range(len(axis)), values, strict=True))
            else:  # a value belongs to the nearest date label (labels sit a little right of their date)
                pairs, taken = [], set()
                for value in values:
                    k = min(range(len(xs)), key=lambda k: abs(value.x - xs[k] - LABEL_OFFSET))
                    if k in taken:
                        series.dropped += 1
                        continue
                    taken.add(k)
                    pairs.append((k, value))
            for k, value in pairs:
                if dates[k] is None:
                    series.dropped += 1
                    continue
                series.points.append((dates[k], number(value.text), axis[k][0].text))
            if series.points:
                out.append(series)
    return out
