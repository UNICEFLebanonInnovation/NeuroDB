"""The KM Database's search box: PostgreSQL full text over the KM tables' own search vectors (filled by the
load, :func:`neurodb.km.loading._fill_vectors`), never the authors, the marks or the IDs.

- An exact Finding ID or Document ID opens those rows.
- Otherwise every word must be found (English stemming, and the words as written). UK and US spellings and
  the sector's acronyms and their expansions find each other (:data:`VARIANTS`): "child labor" finds
  "child labour", "GBV" finds "gender-based violence". A quote, a "-" before a word or the word "or" asks for
  the web search syntax instead: a phrase in quotes, ``-word`` to leave a word out, ``or`` between words.
- When that finds nothing, every word typed must be found as letters in one of the texts (the authors
  included: they are on screen).

The assistant (stage 3) searches the narrower ``own_vector`` with :func:`groups`; nothing here reads any
other NeuroDB table.
"""

from __future__ import annotations

import html
import re
from functools import reduce
from operator import and_, or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import Exists, F, OuterRef, Q, QuerySet
from django.utils.safestring import SafeString, mark_safe

from .columns import ID

TEXT_MAX = 200  # characters of the search box read
# the knowledge base's list of words too common to search (copied: the KM Database imports nothing of it)
STOP = set(
    "the and for with what which who how did does about from that this are was were has have any all our "
    "say said tell into there their".split()
)
# words in almost every finding: they only dilute a search
KM_STOP = {
    "evidence", "finding", "findings", "study", "studies", "data", "show", "shows", "lebanon", "lebanese",
    "children", "child",
}  # fmt: skip

# Words that mean the same, written in code (never the workbook's text): UK and US spellings, word forms the
# English stemmer does not join, and the sector's acronyms with their expansions. One group is one set of
# alternatives; an alternative of several words is searched as a phrase.
VARIANTS: tuple[tuple[str, ...], ...] = (
    ("labour", "labor"),
    ("behaviour", "behavior"),
    ("behaviours", "behaviors"),
    ("behavioural", "behavioral"),
    ("programme", "program"),
    ("programmes", "programs"),
    ("centre", "center"),
    ("centres", "centers"),
    ("anaemia", "anemia"),
    ("paediatric", "pediatric"),
    ("marriage", "married"),
    ("dropout", "drop out", "dropping out", "drop-out", "dropouts"),
    ("wellbeing", "well-being", "well being"),
    ("gbv", "gender-based violence", "gender based violence"),
    ("sgbv", "sexual and gender-based violence"),
    ("ecd", "early childhood development"),
    ("ece", "early childhood education"),
    ("oosc", "out-of-school", "out of school"),
    ("mhpss", "mental health and psychosocial support", "psychosocial"),
    ("wash", "water, sanitation and hygiene"),
    ("tvet", "technical and vocational"),
    ("neet", "not in education, employment or training"),
    ("iycf", "infant and young child feeding"),
    ("cwd", "children with disabilities"),
    ("pwd", "persons with disabilities", "people with disabilities"),
    ("uasc", "unaccompanied", "separated children"),
    ("child marriage", "early marriage"),
)
_BY_WORD: dict[str, tuple[str, ...]] = {}
for _group in VARIANTS:
    for _alt in _group:
        _BY_WORD.setdefault(_alt, _group)
# the alternatives of several words, the longest first: found in the text before its single words
_PHRASES = sorted({alt for group in VARIANTS for alt in group if not alt.isalnum()}, key=len, reverse=True)
_ISE = re.compile(r"^(\w{3,})is(e|ed|es|ing|ation|ations)$")
_IZE = re.compile(r"^(\w{3,})iz(e|ed|es|ing|ation|ations)$")
WEBSEARCH = re.compile(r"[\"“”]|(?:^|\s)-\w|\bor\b", re.IGNORECASE)
CONFIGS = ("english", "simple")
LETTERS_FIELDS = (
    "key_finding",
    "analysis",
    "measure",
    "document_title",
    "item_title",
    "population",
    "methods",
    "limitations",
    "entity",
)
DOCUMENT_LETTERS_FIELDS = ("title", "item_title", "entity")


def words(query: str) -> list[str]:
    """The words of ``query`` worth searching, in lower case (the knowledge base's rule, copied)."""
    found = [w for w in re.findall(r"[\w][\w/.\-]*[\w]|\w", query.lower()) if len(w) >= 2 and w not in STOP]
    return list(dict.fromkeys(found))[:20]


def variants(word: str) -> tuple[str, ...]:
    """``word`` and the words that mean the same (:data:`VARIANTS`, and -ise / -ize: "organise",
    "organize")."""
    found = list(_BY_WORD.get(word, (word,)))
    for pattern, other in ((_ISE, "iz"), (_IZE, "is")):
        match = pattern.match(word)
        if match:
            found.append(f"{match.group(1)}{other}{match.group(2)}")
    return tuple(dict.fromkeys(found))


def groups(text: str, stop: set[str] | frozenset[str] = frozenset()) -> list[list[str]]:
    """The groups of alternatives a search asks for, all of them: first the phrases of :data:`VARIANTS`
    found in ``text`` (the longest first, so "child marriage" is found before "child" is left out as too
    common), then each other word, with its variants. Words of ``stop`` (and of STOP and KM_STOP) are left
    out."""
    rest = " " + text.casefold()[:TEXT_MAX] + " "
    found: list[list[str]] = []
    for phrase in _PHRASES:
        pattern = re.compile(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])")
        if pattern.search(rest):
            found.append(list(_BY_WORD[phrase]))
            rest = pattern.sub(" ", rest)
    for word in words(rest):
        if word in KM_STOP or word in stop:
            continue
        group = list(variants(word))
        if group not in found:
            found.append(group)
    return found


def _alternative(alt: str) -> SearchQuery:
    kind = "plain" if alt.isalnum() else "phrase"
    return reduce(or_, [SearchQuery(alt, search_type=kind, config=c) for c in CONFIGS])


def group_query(group: list[str]) -> SearchQuery:
    """One group: any of its alternatives, in English and as written."""
    return reduce(or_, [_alternative(alt) for alt in group])


def query(text: str) -> SearchQuery | None:
    """The search of the box: the web search syntax when ``text`` asks for it, otherwise every group of
    :func:`groups`. None when there is nothing to search."""
    text = text[:TEXT_MAX]
    if WEBSEARCH.search(text):
        return reduce(or_, [SearchQuery(text, search_type="websearch", config=c) for c in CONFIGS])
    found = groups(text)
    if not found:
        return None
    return reduce(and_, [group_query(g) for g in found])


def is_id(text: str) -> bool:
    return bool(ID.match(text)) and any(c.isdigit() for c in text) and "-" in text


def _letters(text: str, fields: tuple[str, ...]) -> Q | None:
    found = words(text[:TEXT_MAX])
    if not found:
        return None
    return reduce(and_, [reduce(or_, [Q(**{f"{f}__icontains": w}) for f in fields]) for w in found])


def find(qs: QuerySet, text: str) -> tuple[QuerySet, str]:
    """Findings of ``qs`` that match the search box's ``text``, and how they were found: "id" (an exact
    Finding ID or Document ID), "words" (annotated with ``rank``), "letters" (the fallback) or "" (nothing to
    search: ``qs`` as it was)."""
    text = (text or "").strip()[:TEXT_MAX]
    if not text:
        return qs, ""
    if is_id(text):
        by_id = qs.filter(Q(finding_id__iexact=text) | Q(record_id__iexact=text))
        if by_id.exists():
            return by_id, "id"
    tsquery = query(text)
    if tsquery is not None:
        matched = qs.filter(search_vector=tsquery)
        if matched.exists():
            return matched.annotate(rank=SearchRank(F("search_vector"), tsquery)), "words"
    letters = _letters(text, LETTERS_FIELDS)
    if letters is None:
        return qs.none(), "letters"
    return qs.filter(letters), "letters"


def find_documents(qs: QuerySet, text: str) -> tuple[QuerySet, str]:
    """Documents of ``qs`` that match: by their Document ID; by their title, their authors (on screen) or
    one of their findings; else by the letters typed in their title or authors."""
    from .models import KMFinding

    text = (text or "").strip()[:TEXT_MAX]
    if not text:
        return qs, ""
    if is_id(text):
        by_id = qs.filter(record_id__iexact=text)
        if by_id.exists():
            return by_id, "id"
    tsquery = query(text)
    if tsquery is not None:
        has = Exists(KMFinding.objects.filter(document=OuterRef("pk"), search_vector=tsquery))
        matched = qs.filter(Q(search_vector=tsquery) | Q(entity__icontains=text) | Q(has))
        if matched.exists():
            return matched.annotate(rank=SearchRank(F("search_vector"), tsquery)), "words"
    letters = _letters(text, DOCUMENT_LETTERS_FIELDS)
    if letters is None:
        return qs.none(), "letters"
    return qs.filter(letters), "letters"


def marked_words(text: str) -> list[str]:
    """The words a shown text marks for a search: each word typed and its variants (not the web search's
    signs)."""
    text = re.sub(r"(?:^|\s)-\w+", " ", text or "")
    found: list[str] = []
    for group in groups(text):
        found += [alt for alt in group if alt.isalnum()]
    return list(dict.fromkeys(found))


def highlight(text: str, marked: list[str]) -> SafeString:
    """``text`` escaped, with the words that start like a searched word in ``<mark>`` (as the knowledge
    base's snippet marks them)."""
    escaped = html.escape(text or "")
    if marked:
        alternation = "|".join(re.escape(html.escape(w)) for w in sorted(marked, key=len, reverse=True))
        escaped = re.sub(rf"(?<![\w&])({alternation})", r"<mark>\1</mark>", escaped, flags=re.IGNORECASE)
    return mark_safe(escaped)  # noqa: S308 - built from escaped text
