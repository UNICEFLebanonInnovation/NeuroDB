"""The people NeuroDB knows by name, so that NeuroDB Watch never sends them to the AI and never shows a
sentence of the AI that names one.

The morning note's input is an allow-list that never reads a person's field
(:mod:`neurodb.watch.redact`). A name can still slip into a text written from upstream data, and the AI
could write one, so the watch also keeps the list of the names it knows and looks for them in what goes
in and what comes out:

- names of **two words or more** (a first name alone is too common to tell from other words) from the
  fields that hold a person: action point assignees, TPM report authors, field monitoring visit leads,
  progress report submitters and staff travellers (eTools Datamart), PD focal points, partner staff;
- NeuroDB users' first and last names, and their full name;
- the owners typed on finding assignments (meant to be a role or a team, but a name may be typed);
- the names other apps register in :data:`EXTRA_SOURCES` (Monitoring insights: the members of the
  field monitoring teams);
- the email addresses found in those fields. Any email address is treated as a person's, listed or not.

A name is compared in lower case and without accents, word by word: "Jane Doe", "JANE DOE" and "Jane
Doé" are the same name; "Doe, Jane" is also looked for as "jane doe", a title (Ms, Dr) is dropped, and
a name of three words or more is also looked for as its first and last word. A name that is also a
section's name is not a person and is left out.

:func:`known_names` reads them once per run (kept for 10 minutes, :func:`forget` empties it);
:func:`mentions` finds them in a text and :func:`scrub` replaces them.
"""

from __future__ import annotations

import functools
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600  # a run reads the names once; a long-lived process reads them again after this
LONGEST = 6  # words of a name compared at most
NAME_WITHHELD = "[name withheld]"
EMAIL_WITHHELD = "[email withheld]"

EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# Phone numbers written inside a text (field monitoring answers and narratives): Lebanese numbers with
# or without +961/00961 and the leading 0 ("03-123456", "+961 3 123 456"), and any international one
PHONE = re.compile(
    r"(?<![\w/.-])(?:(?:\+|00)\s?961[\s.-]?)?0?(?:7[016-9]|81|[1-9])[\s.-]?\d{3}[\s.-]?\d{3}(?![\w/-])"
)
INTL_PHONE = re.compile(r"(?<![\w/])\+\d[\d\s().-]{7,}\d(?![\w/])")
PHONE_WITHHELD = "[phone withheld]"
# A name written after a title ("Mrs Layla Saab", "Dr. Haddad"), known to NeuroDB or not: replaced by
# NAME_WITHHELD in field monitoring texts. It also takes a place named after a title ("Sheikh Zennad").
HONORIFIC_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Eng|Prof|Sheikh)\.?\s+[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?")
# A word: letters, with the accents and Arabic vowel marks that may sit inside it
WORD = re.compile(r"(?:[^\W\d_]|[̀-ًͯ-ٰٟ])+")
HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "eng", "prof", "sir", "madam", "mme", "mlle"})
LIST_SEPARATORS = re.compile(r"[;/&|\n]|\band\b", re.IGNORECASE)

# The fields of the eTools Datamart that hold a person's name (or email)
DATAMART_FIELDS = (
    ("ActionPoint", "assigned_to_name"),
    ("TPMVisit", "author_name"),
    ("MonitoringFinding", "visit_lead"),
    ("ReportedIndicator", "submitted_by"),
    ("ProgrammaticVisit", "primary_traveler"),
)

# More places that hold people's names, added by other apps when they start (Monitoring insights adds
# the field monitoring teams). Each gives raw names or e-mail addresses; one that fails is logged and
# skipped, so a missing table never empties the list.
EXTRA_SOURCES: list[Callable[[], Iterable[str]]] = []

_cache: tuple[float, frozenset[str]] | None = None


# ---------------------------------------------------------------------------- comparing names
def fold(text: str) -> str:
    """``text`` in lower case and without accents, for comparing names."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def words(text: str) -> list[str]:
    """The words of ``text``, folded (see :func:`fold`)."""
    return [fold(w) for w in WORD.findall(str(text or ""))]


def name_forms(raw: str) -> set[str]:
    """The forms a written name is looked for in: "Ms Jane Doe (UNICEF)" gives "jane doe"; "Doe,
    Jane" gives "doe jane" and "jane doe"; "Jane Mary Doe" gives "jane mary doe" and "jane doe".
    Several names in one field ("Jane Doe; John Roe") give each. Fewer than two words give nothing."""
    text = re.sub(r"\([^)]*\)", " ", EMAIL.sub(" ", str(raw or "")))
    forms: set[str] = set()
    for piece in LIST_SEPARATORS.split(text):
        parts = [p for p in piece.split(",") if p.strip()]
        candidates = list(parts)
        if len(parts) == 2:  # "Doe, Jane": also both ways round
            candidates += [f"{parts[1]} {parts[0]}", f"{parts[0]} {parts[1]}"]
        for candidate in candidates:
            found = words(candidate)
            while found and found[0] in HONORIFICS:
                found = found[1:]
            if len(found) < 2:
                continue
            forms.add(" ".join(found[:LONGEST]))
            if len(found) >= 3:
                forms.add(f"{found[0]} {found[-1]}")
    return forms


def emails_in(text: str) -> set[str]:
    return {e.lower().strip(".") for e in EMAIL.findall(str(text or ""))}


# ---------------------------------------------------------------------------- the names known
def known_names(refresh: bool = False) -> frozenset[str]:
    """Every person name (folded, two words or more) and email address NeuroDB holds, read once per
    run: kept for ``CACHE_SECONDS``, or read again with ``refresh``."""
    global _cache
    now = time.monotonic()
    if refresh or _cache is None or now - _cache[0] > CACHE_SECONDS:
        _cache = (now, frozenset(_read()))
    return _cache[1]


def forget() -> None:
    """Empty the names read (the next :func:`known_names` reads them again)."""
    global _cache
    _cache = None


def _read() -> set[str]:
    from django.db import transaction

    from neurodb.accounts.models import Section, User
    from neurodb.datamart import models as dm
    from neurodb.partnerships.models import PCA, PartnerStaffMember
    from neurodb.review.models import FindingAssignment

    raw: list[str] = []
    for model_name, field in DATAMART_FIELDS:
        model = getattr(dm, model_name)
        raw += model.objects.exclude(**{field: ""}).order_by().values_list(field, flat=True).distinct()
    for points in (
        PCA.objects.exclude(unicef_focal_points=None).order_by().values_list("unicef_focal_points", flat=True)
    ):
        raw += [p for p in points or () if p]
    for first, last, name, email in User.objects.order_by().values_list(
        "first_name", "last_name", "name", "email"
    ):
        raw += [f"{first or ''} {last or ''}", name or "", email or ""]
    for first, last, email in PartnerStaffMember.objects.order_by().values_list(
        "first_name", "last_name", "email"
    ):
        raw += [f"{first or ''} {last or ''}", email or ""]
    raw += FindingAssignment.objects.exclude(owner="").order_by().values_list("owner", flat=True).distinct()
    for source in EXTRA_SOURCES:
        try:
            with transaction.atomic():  # a failed query must not break the caller's transaction
                raw += [str(name) for name in source() if name]
        except Exception:
            logger.exception("people: the names of %s could not be read", getattr(source, "__name__", source))
    known: set[str] = set()
    for text in set(raw):
        known |= name_forms(text) | emails_in(text)
    sections = {" ".join(words(n)) for n in Section.objects.values_list("name", flat=True)}
    return known - sections


@functools.lru_cache(maxsize=8)
def _longest(names: frozenset[str]) -> int:
    return min(LONGEST, max((len(n.split()) for n in names if "@" not in n), default=0))


def _as_set(names: Iterable[str] | None) -> frozenset[str]:
    if names is None:
        return known_names()
    return names if isinstance(names, frozenset) else frozenset(names)


def _name_spans(text: str, names: frozenset[str]) -> list[tuple[int, int, str]]:
    """Where known names are written in ``text``: (start, end, folded name), longest first."""
    longest = _longest(names)
    if longest < 2:
        return []
    tokens = [(m.start(), m.end(), fold(m.group(0))) for m in WORD.finditer(text)]
    spans, i = [], 0
    while i < len(tokens):
        for size in range(min(longest, len(tokens) - i), 1, -1):
            form = " ".join(t[2] for t in tokens[i : i + size])
            if form in names:
                spans.append((tokens[i][0], tokens[i + size - 1][1], form))
                i += size
                break
        else:
            i += 1
    return spans


# ---------------------------------------------------------------------------- finding and removing
def mentions(text: str, names: Iterable[str] | None = None) -> list[str]:
    """The person names and email addresses written in ``text`` (any email address counts)."""
    text = str(text or "")
    found = sorted(emails_in(text))
    names = _as_set(names)
    return found + [form for _, _, form in _name_spans(EMAIL.sub(" ", text), names)]


def scrub(text: str, names: Iterable[str] | None = None) -> str:
    """``text`` with every email address and known person name replaced by a placeholder."""
    text = EMAIL.sub(EMAIL_WITHHELD, str(text or ""))
    names = _as_set(names)
    for start, end, _ in reversed(_name_spans(text, names)):
        text = f"{text[:start]}{NAME_WITHHELD}{text[end:]}"
    return text
