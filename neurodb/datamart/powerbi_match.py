"""Compare with Power BI: when two values are the same, how a Power BI row finds its NeuroDB row, and the
lines of differences an export's comparison writes (``powerbi_kinds`` compares each export with them).

* **Raw values against raw values.** Power BI's cells hold unrounded values (its number format only changes
  the display), and none of NeuroDB's figures compared is rounded: two numbers are the same within float
  noise only (:data:`TOLERANCE`, relative), two amounts within half a cent. A Power BI 0 against no value in
  NeuroDB is the same in a Reached or % column (Power BI shows no value as 0).
* **Texts** are the same when their spaces and case are (after the people cleaner on NeuroDB's side: Power
  BI's were cleaned as the file was read); a title Power BI cut at 255 characters matches NeuroDB's title
  that starts with it. **Codes** (PD #, Report #, P-codes, grants) are compared as written (one naming someone
  was not kept as the file was read). A PD # that differs only in its amendment suffix ("-2") still pairs,
  and the line says so.
* **Pairing** (:func:`pair`): the rows with the same match values on each side, most similar first; a row
  left over is only in Power BI or only in NeuroDB.
* **A value NeuroDB cannot read** (a number beyond any float, Infinity, NaN: eTools and PRP keep amounts and
  values as text) is left empty in its line, and the line's reason says so (``unreadable``): JSON, and so the
  database, holds no such number, and one would stop the export's lines from being written.
* **Lines** (:class:`Result`): each difference with its reason, group and what to do, kept compactly while
  the export is compared (no database write in the comparison's read-only snapshot) and written once every
  export is compared, group by group across the exports (needs a look first, the expected ones last), at most
  ``powerbi_compare.LINE_LIMIT`` per export and ``UPLOAD_LINE_LIMIT`` per upload (the rest are counted). A
  detail is at most :data:`DETAIL_LIMIT` characters, cut on a word (:func:`cut`).
"""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import io
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import powerbi_words as words

TOLERANCE = 1e-9  # relative: float noise only
MONEY_TOLERANCE = 0.005  # half a cent
PAIR_LIMIT = 60  # rows of one match on a side beyond which they pair in order (no similarity computed)
GROUP_ORDER = (4, 3, 2, 1)  # the lines' groups, needs a look first: the order they are written and kept
DETAIL_LIMIT = 300  # characters of a line's detail (PowerBIDifference.detail)
LINK_LIMIT = 300  # characters of a line's address (PowerBIDifference.link)
GRANTLESS = frozenset({"", "non-grant", "non grant", "nongrant"})
AMENDMENT = re.compile(r"-\d+$")
ETOOLS_SUFFIX = re.compile(r"\s*\(etools\s*\d+\)\s*$", re.I)
SPD = frozenset({"spd", "shpd"})


# ----------------------------------------------------------------------------------------------- normalised
def text(value: Any) -> str:
    """A text as two are compared: its spaces as one, whatever their case."""
    return " ".join(str(value if value is not None else "").split()).casefold()


def iso(value: Any) -> str:
    """A date as ISO text ("" for none); a datetime is reduced to its date."""
    found = as_date(value)
    return found.isoformat() if found else ""


def as_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def pd_number(value: Any) -> str:
    """A PD # as two are matched: stripped, any case."""
    return "".join(str(value or "").split()).casefold()


def pd_base(value: Any) -> str:
    """A PD # without its amendment suffix ("-2")."""
    return AMENDMENT.sub("", pd_number(value))


def report_number(value: Any, report_type: Any = "") -> str:
    """A report number as Power BI writes it: its type first ("QPR3"), added when NeuroDB's number is bare."""
    found = "".join(str(value or "").split()).upper()
    kind = "".join(str(report_type or "").split()).upper()
    if found and kind and found[:1].isdigit():
        return f"{kind}{found}"
    return found


def partner(value: Any) -> str:
    """A partner as two are matched: Power BI writes partners in capitals, NeuroDB may add "(eTools 12)"."""
    from neurodb.partnerships.linking import normalise_name

    return normalise_name(ETOOLS_SUFFIX.sub("", str(value or ""))).casefold()


def grant(value: Any) -> str:
    found = text(value)
    return "" if found in GRANTLESS else found


def document_type(value: Any) -> str:
    found = text(value)
    return "spd" if found in SPD else found


def yes_no(value: Any) -> bool | None:
    found = text(value)
    if found in ("yes", "true", "1"):
        return True
    if found in ("no", "false", "0"):
        return False
    return None


def sections(value: Any, known: Iterable[str] = ()) -> frozenset[str]:
    """A joined list of sections ("Education, Health", "Health ,education ") as a set of names, a name holding
    a comma kept whole when NeuroDB knows it."""
    from .reporting import section_names

    if isinstance(value, list | tuple | set | frozenset):
        found: list[str] = []
        for item in value:
            found.extend(section_names(item, known))
    else:
        found = section_names(value, known)
    return frozenset(text(n) for n in found if text(n))


def sections_key(value: frozenset[str]) -> str:
    return " | ".join(sorted(value))


def cp_outputs_fit(powerbi: Any, outputs: Iterable[str]) -> bool:
    """Power BI's CP Output text holds each of the PD's CP output names whole, with nothing left over (a name
    can hold ", ")."""
    rest = f", {text(powerbi)}, "
    names = sorted({text(o) for o in outputs if text(o)}, key=len, reverse=True)
    if not names:
        return not text(powerbi)
    for name in names:
        piece = f", {name}, "
        if piece not in rest:
            return False
        rest = rest.replace(piece, ", ", 1)
    return not rest.strip(", ")


# --------------------------------------------------------------------------------------------- the same
def same_number(powerbi: Any, neurodb: Any, *, zero_is_none: bool = False) -> bool:
    a, b = number(powerbi), number(neurodb)
    if zero_is_none:
        a = None if a == 0 else a
        b = None if b == 0 else b
    if a is None or b is None:
        return a is None and b is None
    if not (math.isfinite(a) and math.isfinite(b)):  # a value NeuroDB cannot read is never the same (the
        return False  # tolerance of an infinite one is infinite): its line says so (``readable``)
    return abs(a - b) <= TOLERANCE * max(1.0, abs(a), abs(b))


def same_money(powerbi: Any, neurodb: Any) -> bool:
    a, b = number(powerbi), number(neurodb)
    if a is None or b is None:
        return (a or 0.0) == (b or 0.0)  # no amount and 0 are one
    return abs(a - b) <= MONEY_TOLERANCE


def same_count(powerbi: Any, neurodb: Any) -> bool:
    return int(number(powerbi) or 0) == int(number(neurodb) or 0)


def same_text(powerbi: Any, neurodb: Any, *, cut: bool = False) -> bool:
    a, b = text(powerbi), text(neurodb)
    if cut and a:
        return b.startswith(a)
    return a == b


def same_date(powerbi: Any, neurodb: Any) -> bool:
    return iso(powerbi) == iso(neurodb)


def same(column_type: str, powerbi: Any, neurodb: Any, **options: Any) -> bool:
    """Whether Power BI's and NeuroDB's values of a column of this type are the same (see the module
    docstring)."""
    from . import powerbi_exports as px

    if column_type in (px.NUMBER, px.FRACTION):
        return same_number(powerbi, neurodb, zero_is_none=options.get("zero_is_none", False))
    if column_type == px.MONEY:
        return same_money(powerbi, neurodb)
    if column_type == px.COUNT:
        return same_count(powerbi, neurodb)
    if column_type in (px.DATE, px.STAMP):
        return same_date(powerbi, neurodb)
    if column_type == px.CODE:
        return pd_number(powerbi) == pd_number(neurodb)
    if column_type == px.YES_NO:
        return yes_no(powerbi) == (neurodb if isinstance(neurodb, bool) else yes_no(neurodb))
    return same_text(powerbi, neurodb, cut=options.get("cut", False))


def readable(value: Any) -> bool:
    """A value a line can keep: never a number beyond a float, Infinity or NaN (JSON holds none of them)."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite() and math.isfinite(float(value))
    if isinstance(value, list | tuple | set | frozenset):
        return all(readable(v) for v in value)
    return True


def shown(value: Any) -> Any:
    """A value as a line keeps it: a number, a text, an ISO date (never an object); empty when it cannot be
    read (:func:`readable`)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | Decimal):
        return float(value) if readable(value) else None
    if isinstance(value, datetime.datetime | datetime.date):
        return iso(value)
    if isinstance(value, list | tuple | set | frozenset):
        return ", ".join(
            str(shown(v)) for v in (sorted(value) if isinstance(value, set | frozenset) else value)
        )
    return str(value)


# ------------------------------------------------------------------------------------------------- pairing
def pair(
    powerbi: list[Any],
    neurodb: list[Any],
    key_powerbi: Callable[[Any], Hashable],
    key_neurodb: Callable[[Any], Hashable],
    score: Callable[[Any, Any], tuple] | None = None,
) -> tuple[list[tuple[Any, Any]], list[Any], list[Any]]:
    """Power BI's rows paired with NeuroDB's of the same match values, the most similar first (``score``:
    higher is more similar); the rows left over on each side. A row whose match values are None pairs with
    nothing."""
    by_key: dict[Hashable, list[Any]] = {}
    for row in neurodb:
        found = key_neurodb(row)
        if found is not None:
            by_key.setdefault(found, []).append(row)
    left_nd = [row for row in neurodb if key_neurodb(row) is None]
    groups: dict[Hashable, list[Any]] = {}
    left_pb: list[Any] = []
    for row in powerbi:
        found = key_powerbi(row)
        if found is None:
            left_pb.append(row)
        else:
            groups.setdefault(found, []).append(row)
    pairs: list[tuple[Any, Any]] = []
    for found, mine in groups.items():
        theirs = by_key.pop(found, [])
        if not theirs:
            left_pb.extend(mine)
            continue
        if len(mine) == 1 and len(theirs) == 1:
            pairs.append((mine[0], theirs[0]))
            continue
        if score is None or len(mine) > PAIR_LIMIT or len(theirs) > PAIR_LIMIT:
            n = min(len(mine), len(theirs))
            pairs.extend(zip(mine[:n], theirs[:n], strict=True))
            left_pb.extend(mine[n:])
            left_nd.extend(theirs[n:])
            continue
        ranked = sorted(
            ((score(p, q), i, j) for i, p in enumerate(mine) for j, q in enumerate(theirs)),
            key=lambda item: (item[0], -item[1], -item[2]),
            reverse=True,
        )
        used_p: set[int] = set()
        used_q: set[int] = set()
        for _score, i, j in ranked:
            if i in used_p or j in used_q:
                continue
            used_p.add(i)
            used_q.add(j)
            pairs.append((mine[i], theirs[j]))
        left_pb.extend(p for i, p in enumerate(mine) if i not in used_p)
        left_nd.extend(q for j, q in enumerate(theirs) if j not in used_q)
    for rest in by_key.values():
        left_nd.extend(rest)
    return pairs, left_pb, left_nd


def closeness(a: Any, b: Any) -> float:
    """How close two numbers are, for a score (higher is closer)."""
    x, y = number(a), number(b)
    if x is None or y is None:
        return -1e18 if (x is None) != (y is None) else 0.0
    return -abs(x - y)


# --------------------------------------------------------------------------------------------- the lines
def cut(text: str, limit: int = DETAIL_LIMIT) -> str:
    """A text of at most ``limit`` characters: whole, or cut after its last whole word with "…"."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if text[limit - 1] != " ":  # the cut falls inside a word: drop it
        head = head.rsplit(" ", 1)[0] if " " in head else head
    return head.rstrip(" ,;:(") + "…"


@dataclass
class Line:
    """One difference: its kind (different, only_powerbi, only_neurodb), reason, group, rule, detail, the
    values it is matched by (as shown), each column's [Power BI, NeuroDB, another reading] and its "Check in
    NeuroDB" address."""

    kind: str
    reason: str
    group: int
    rule: str = ""
    detail: str = ""
    match: list[Any] = field(default_factory=list)
    found: dict[str, list[Any]] = field(default_factory=dict)
    link: str = ""
    pd: str = ""  # the PD to check in eTools (group 3), as Power BI writes it

    def readable(self) -> bool:
        """Every value of the line can be kept (:func:`readable`)."""
        return all(readable(v) for v in self.match) and all(
            readable(x) for values in self.found.values() for x in values
        )

    def as_json(self) -> list[Any]:
        return [
            self.kind,
            self.group,
            self.reason,
            self.rule,
            cut(self.detail),
            [shown(v) for v in self.match],
            {k: [shown(x) for x in v] for k, v in self.found.items()},
            self.link if len(self.link) <= LINK_LIMIT else "",  # a cut address opens the wrong page: none
        ]


@dataclass
class Result:
    """An export's comparison as it goes: its counts, its notes and fit tables, and its lines (kept as gzip
    JSON lines per group, at most ``limit`` each; past it they are counted)."""

    limit: int
    counts: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    rules: Counter = field(default_factory=Counter)
    groups: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    fits: list[dict[str, Any]] = field(default_factory=list)
    pds: set[str] = field(default_factory=set)  # the PDs to check in eTools
    extra: dict[str, Any] = field(default_factory=dict)  # what a later export reads (the report list's gaps)
    _spools: dict[int, Any] = field(default_factory=dict)
    _kept: Counter = field(default_factory=Counter)

    def same(self, n: int = 1, *, zero: bool = False) -> None:
        self.counts["rows"] += n
        self.counts["same_zero" if zero else "same"] += n

    def add(self, line: Line, *, row: bool = True) -> None:
        """One difference; ``row``: it is a row of its own (False: another column of a row counted
        already). A line holding a value NeuroDB cannot read (:func:`readable`) takes that as its reason, the
        value left empty."""
        if not line.readable():
            line = dataclasses.replace(
                line,
                reason="unreadable",
                group=words.REASON_GROUPS["unreadable"],
                rule="",
                detail=words.DETAIL_UNREADABLE,
            )
        if row:
            self.counts["rows"] += 1
            self.counts[line.kind] += 1
        self.reasons[line.reason] += 1
        self.groups[line.group] += 1
        if line.rule:
            self.rules[line.rule] += 1
        if line.group == 3 and line.pd:
            self.pds.add(line.pd)
        if self._kept[line.group] >= self.limit:
            return
        spool = self._spools.get(line.group)
        if spool is None:
            buffer = io.BytesIO()
            spool = self._spools[line.group] = (buffer, gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0))
        # never Infinity or NaN: the database refuses them (every value is readable here)
        kept = json.dumps(line.as_json(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        spool[1].write(kept.encode())
        spool[1].write(b"\n")
        self._kept[line.group] += 1

    def note(self, sentence: str) -> None:
        if sentence and sentence not in self.notes:
            self.notes.append(sentence)

    def close(self) -> None:
        """The comparison is over: each group's buffer is finished (its compressor freed), its lines kept
        until they are taken."""
        for _buffer, stream in self._spools.values():
            stream.close()

    def take(self, group: int) -> Iterator[list[Any]]:
        """The lines kept of one group, in the order they came; the result lets them go at once (whether they
        are read or not)."""
        spool = self._spools.pop(group, None)
        if spool is None:
            return iter(())
        spool[1].close()
        return _kept_lines(spool[0].getvalue())

    def lines(self) -> Iterator[list[Any]]:
        """Every line kept, needs-a-look first (groups 4, 3, 2, 1)."""
        for group in GROUP_ORDER:
            yield from self.take(group)

    def summary(self) -> dict[str, Any]:
        out = {k: int(v) for k, v in self.counts.items()}
        for name in ("rows", "same", "same_zero", "different", "only_powerbi", "only_neurodb"):
            out.setdefault(name, 0)
        out["groups"] = {str(k): int(v) for k, v in sorted(self.groups.items())}
        out["reasons"] = dict(self.reasons.most_common())
        out["rules"] = dict(self.rules.most_common())
        out["check_pds"] = sorted(self.pds)[:500]
        out["lines"] = int(sum(self.groups.values()))
        return out


def _kept_lines(data: bytes) -> Iterator[list[Any]]:
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
        for raw in stream:
            yield json.loads(raw)
