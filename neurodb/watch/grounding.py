"""Checks on every sentence the AI writes for NeuroDB Watch, before anyone reads it.

The AI writes from facts that code chose (:mod:`neurodb.watch.redact`), and each sentence names the keys
of the facts it rests on. A sentence is kept only when:

- it cites at least one fact, and only facts its audience may read (:func:`validate`);
- every number in it is in the facts it cites; whole numbers up to 10 are allowed, as in the daily
  review ("two reports", "Q3"), and so is a figure rounded to one decimal or to a whole number;
- every date in it (2026-10-15, 15 Oct, October 15, Oct 2026) is a date of a fact it cites: its due
  date, an evidence date, a date written in its title;
- every eTools reference in it (LEB/PCA2026001/PD2026001, PD2026001) is in the facts it cites;
- it has no link, no markdown and no HTML;
- it names no person NeuroDB knows and has no email address (:mod:`neurodb.watch.people`);
- it has at most 400 characters.

Anything else is dropped. When no sentence is left, the note is listed by code instead.
"""

from __future__ import annotations

import datetime
import decimal
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from django.utils import timezone

from neurodb.review.services import numbers_in

from . import people, redact
from .models import WatchItem

MAX_CHARS = 400
MAX_SENTENCES = 6
SMALL = 10  # whole numbers up to this one need no source: counts of what is cited

# Why a sentence was dropped
EMPTY, TOO_LONG, LINK, EMAIL, MARKUP, PERSON = "empty", "too_long", "link", "email", "markup", "person"
NUMBER, DATE, REFERENCE = "number", "date", "reference"
NO_KEYS, UNKNOWN_KEY, MALFORMED = "no_keys", "unknown_key", "malformed"

LINK_IN_TEXT = re.compile(rf"{redact.LINK.pattern}|(?:^|\s)/[\w-]+/", re.IGNORECASE)
MARKUP_IN_TEXT = re.compile(
    r"[*`|~\[\]{}<>]|__|&(?:[a-z]+|#\d+);|^\s*(?:#|[-+]\s|\d+[.)]\s)", re.IGNORECASE | re.MULTILINE
)
REFERENCE_IN_TEXT = re.compile(r"\b[A-Z]{3}(?:/[A-Za-z0-9-]+)+|\b(?:PCA|PD|SPD|HPD|SSFA)\d+(?:-\d+)?\b")

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
DATE_IN_TEXT = re.compile(
    r"\b(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})(?!\d)"  # 2026-10-15
    r"|\b(?P<sd>\d{1,2})/(?P<sm>\d{1,2})/(?P<sy>\d{4})(?!\d)"  # 15/10/2026
    rf"|\b(?P<dd>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<dm>{_MONTH})(?:,?\s+(?P<dy>\d{{4}})(?!\d))?"
    r"(?![A-Za-z])"  # 15 Oct, 15 October 2026
    rf"|\b(?P<md>{_MONTH})\s+(?P<mdd>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)"  # Oct 15, October 15, 2026
    r"(?:,?\s+(?P<mdy>\d{4})(?!\d))?"
    rf"|\b(?P<my>{_MONTH})\s+(?P<myy>\d{{4}})(?!\d)"  # Oct 2026
)


class Verdict(NamedTuple):
    """Whether a sentence may be kept; ``reason`` and ``detail`` (the number, date or reference that
    failed; never a person's name) say why not. False when it may not, so ``if check(...)`` works."""

    ok: bool
    reason: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


KEPT = Verdict(True)


# ---------------------------------------------------------------------------- the facts cited
def facts_of(item: WatchItem, today: datetime.date | None = None) -> dict[str, Any]:
    """What a sentence citing ``item`` may rest on: its key, title, due and closing dates, days left
    and open, evidence numbers and dates, and section names."""
    today = today or timezone.localdate()
    evidence = item.evidence if isinstance(item.evidence, dict) else {}
    records = evidence.get("records") if isinstance(evidence.get("records"), list | tuple) else []
    return {
        "key": item.key,
        "title": item.title,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "closed_on": item.closed_on.isoformat() if item.closed_on else None,
        "days_left": (item.due_date - today).days if item.due_date else None,
        "days_open": (today - item.first_seen_on).days if item.first_seen_on else None,
        "numbers": evidence.get("numbers") or {},
        "evidence_dates": [r.get("date") for r in records if isinstance(r, Mapping) and r.get("date")],
        "sections": list(item.etools_sections or []),
    }


def _blob(cited: Iterable[WatchItem | Mapping[str, Any]], today: datetime.date | None) -> str:
    parts = []
    for fact in cited:
        if isinstance(fact, WatchItem):
            fact = facts_of(fact, today)
        parts.append(json.dumps(fact, default=str, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


# ---------------------------------------------------------------------------- numbers and dates
def _norm(number: str) -> str:
    """A written number in one form: "12,500.00" and "012500" give "12500"."""
    whole, _, fraction = number.replace(",", "").rstrip(".").partition(".")
    whole, fraction = whole.lstrip("0") or "0", fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def _allowed_numbers(blob: str) -> set[str]:
    """The numbers in the facts, each also rounded to one decimal and to a whole number."""
    allowed: set[str] = set()
    for written in numbers_in(blob):
        number = _norm(written)
        allowed.add(number)
        if "." in number:
            value = decimal.Decimal(number)
            for places in ("1", "0.1"):
                rounded = value.quantize(decimal.Decimal(places), rounding=decimal.ROUND_HALF_UP)
                allowed.add(_norm(str(rounded)))
    return allowed


def _month(name: str) -> int:
    return MONTHS[name[:3].lower()]


def dates_in(text: str) -> list[tuple[re.Match, int | None, int, int | None]]:
    """The dates written in ``text``: (match, year or None, month, day or None)."""
    found = []
    for match in DATE_IN_TEXT.finditer(text or ""):
        g = match.groupdict()
        if g["iy"]:
            year, month, day = int(g["iy"]), int(g["im"]), int(g["id"])
        elif g["sy"]:
            year, month, day = int(g["sy"]), int(g["sm"]), int(g["sd"])
        elif g["dm"]:
            year, month, day = (int(g["dy"]) if g["dy"] else None), _month(g["dm"]), int(g["dd"])
        elif g["md"]:
            year, month, day = (int(g["mdy"]) if g["mdy"] else None), _month(g["md"]), int(g["mdd"])
        else:
            year, month, day = int(g["myy"]), _month(g["my"]), None
        found.append((match, year, month, day))
    return found


def _real(year: int | None, month: int, day: int | None) -> bool:
    try:
        datetime.date(year or 2024, month, day or 1)  # 2024: a leap year, so 29 Feb stands without a year
    except ValueError:
        return False
    return True


def _allowed_dates(blob: str) -> tuple[set, set, set]:
    """The dates of the facts: whole dates, (month, day) and (year, month)."""
    whole, day_month, month_year = set(), set(), set()
    for _, year, month, day in dates_in(blob):
        if day is not None:
            day_month.add((month, day))
        if year is not None:
            month_year.add((year, month))
            if day is not None:
                whole.add((year, month, day))
    return whole, day_month, month_year


def _without(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = f"{text[:start]} {text[end:]}"
    return text


# ---------------------------------------------------------------------------- one sentence
def check(
    sentence: str,
    cited: Iterable[WatchItem | Mapping[str, Any]],
    today: datetime.date | None = None,
    names: Iterable[str] | None = None,
) -> Verdict:
    """Whether ``sentence`` may be kept, resting on the ``cited`` facts: items, or the entries the AI
    read (an item, a situation or a section's counts as :mod:`neurodb.watch.redact` writes them).
    ``names`` are the known person names (default :func:`neurodb.watch.people.known_names`)."""
    sentence = " ".join(str(sentence or "").split())
    if not sentence:
        return Verdict(False, EMPTY)
    if len(sentence) > MAX_CHARS:
        return Verdict(False, TOO_LONG, str(len(sentence)))
    if people.EMAIL.search(sentence):
        return Verdict(False, EMAIL)
    if LINK_IN_TEXT.search(sentence):
        return Verdict(False, LINK)
    if MARKUP_IN_TEXT.search(sentence):
        return Verdict(False, MARKUP)
    if people.mentions(sentence, names):
        return Verdict(False, PERSON)
    cited = list(cited)
    if not cited:
        return Verdict(False, NO_KEYS)
    blob = _blob(cited, today)
    folded_blob = blob.casefold()

    spans: list[tuple[int, int]] = []
    for match in REFERENCE_IN_TEXT.finditer(sentence):
        reference = match.group(0)
        if not any(c.isdigit() for c in reference):
            continue
        if reference.casefold() not in folded_blob:
            return Verdict(False, REFERENCE, reference)
        spans.append(match.span())
    rest = _without(sentence, spans)

    whole, day_month, month_year = _allowed_dates(blob)
    spans = []
    for match, year, month, day in dates_in(rest):
        if not _real(year, month, day):
            return Verdict(False, DATE, match.group(0))
        if day is None:
            grounded = (year, month) in month_year
        elif year is None:
            grounded = (month, day) in day_month
        else:
            grounded = (year, month, day) in whole
        if not grounded:
            return Verdict(False, DATE, match.group(0))
        spans.append(match.span())
    rest = _without(rest, spans)

    allowed = _allowed_numbers(blob)
    for written in sorted(numbers_in(rest)):
        number = _norm(written)
        if number not in allowed and not (number.isdigit() and int(number) <= SMALL):
            return Verdict(False, NUMBER, written)
    return KEPT


# ---------------------------------------------------------------------------- an answer
def _cited(keys: list[str], citable: Mapping[str, Any]) -> list[Any]:
    """The facts behind ``keys``; citing a situation also cites its items."""
    facts: list[Any] = []
    for key in keys:
        fact = citable[key]
        facts.append(fact)
        inner = fact.get("items") if isinstance(fact, Mapping) else None
        for item_key in inner if isinstance(inner, list | tuple) else ():
            if isinstance(item_key, str) and item_key in citable and item_key not in keys:
                facts.append(citable[item_key])
    return facts


def validate(
    raw: Any,
    citable: Mapping[str, Any],
    max_sentences: int = MAX_SENTENCES,
    today: datetime.date | None = None,
    names: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The sentences of an answer (``{"sentences": [{"text", "keys"}]}``, or the list) that may be
    kept, as ``[{"text", "keys"}]``, and why each other one was dropped. ``citable`` maps each key the
    audience may read to its fact (an item, or an entry the AI read); any other key drops the
    sentence. At most ``max_sentences`` are looked at."""
    entries = raw.get("sentences") if isinstance(raw, Mapping) else raw
    names = people.known_names() if names is None else frozenset(names)
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for entry in list(entries if isinstance(entries, list | tuple) else [])[:max_sentences]:
        raw_keys = entry.get("keys") if isinstance(entry, Mapping) else None
        if not isinstance(raw_keys, list | tuple) or not isinstance(entry.get("text"), str):
            dropped.append(MALFORMED)
            continue
        keys = list(dict.fromkeys(k for k in raw_keys if isinstance(k, str) and k))
        if not keys:
            dropped.append(NO_KEYS)
            continue
        if any(k not in citable for k in keys):
            dropped.append(UNKNOWN_KEY)
            continue
        verdict = check(entry["text"], _cited(keys, citable), today, names)
        if not verdict:
            dropped.append(verdict.reason)
            continue
        kept.append({"text": " ".join(entry["text"].split()), "keys": keys})
    return kept, dropped
