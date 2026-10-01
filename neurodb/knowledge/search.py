"""Searching the knowledge base: the passages that best match a question, optionally only in the
documents linked to a partner, a programme document, a section or a year.

Every word must match first (PostgreSQL full-text search, English stemming plus the words as
written); when that finds too little, passages matching any of the words fill up the list.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from functools import reduce
from operator import or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import Q, QuerySet
from django.utils.safestring import SafeString, mark_safe

from .models import Chunk, Document, Link

STOP = set(
    "the and for with what which who how did does about from that this are was were has have any all our "
    "say said tell into there their".split()
)


@dataclass
class Filters:
    partner_id: int | None = None
    programme_id: int | None = None
    section_id: int | None = None
    year: int | None = None
    document_id: int | None = None

    def documents(self) -> QuerySet[Document]:
        qs = Document.objects.filter(status=Document.Status.READY)
        if self.document_id:
            qs = qs.filter(pk=self.document_id)
        for kind, pk in ((Link.Kind.PARTNER, self.partner_id), (Link.Kind.PROGRAMME, self.programme_id)):
            if pk:
                qs = qs.filter(links__kind=kind, links__object_id=pk)
        if self.section_id:
            qs = qs.filter(
                Q(section_id=self.section_id)
                | Q(links__kind=Link.Kind.SECTION, links__object_id=self.section_id)
            )
        if self.year:
            qs = qs.filter(Q(year=self.year) | Q(document_date__year=self.year))
        return qs.distinct()


@dataclass
class Hit:
    chunk: Chunk
    rank: float
    words: list[str] = field(default_factory=list)

    @property
    def document(self) -> Document:
        return self.chunk.document

    def snippet(self, size: int = 320) -> SafeString:
        """A piece of the passage around the first matched word, escaped, matches in <mark>."""
        text = self.chunk.text
        low = text.lower()
        at = min((i for w in self.words if (i := low.find(w)) >= 0), default=0)
        start = max(0, at - size // 3)
        piece = (
            ("…" if start else "") + text[start : start + size] + ("…" if start + size < len(text) else "")
        )
        escaped = html.escape(piece)
        for word in sorted(self.words, key=len, reverse=True):
            escaped = re.sub(f"({re.escape(html.escape(word))})", r"<mark>\1</mark>", escaped, flags=re.I)
        return mark_safe(escaped)  # noqa: S308 - built from escaped text


def words(query: str) -> list[str]:
    found = [w for w in re.findall(r"[\w][\w/.\-]*[\w]|\w", query.lower()) if len(w) >= 2 and w not in STOP]
    return list(dict.fromkeys(found))[:20]


def _all_words(query: str) -> SearchQuery:
    return SearchQuery(query, search_type="websearch", config="english") | SearchQuery(
        query, search_type="websearch", config="simple"
    )


def _any_word(terms: list[str]) -> SearchQuery:
    return reduce(
        or_,
        [SearchQuery(t, search_type="plain", config=c) for t in terms for c in ("english", "simple")],
    )


def search(query: str, filters: Filters | None = None, limit: int = 8) -> list[Hit]:
    filters = filters or Filters()
    terms = words(query)
    if not terms:
        return []
    base = Chunk.objects.filter(document__in=filters.documents()).select_related("document")
    hits: list[Hit] = []
    seen: set[int] = set()
    for tsquery in (_all_words(" ".join(terms)), _any_word(terms)):
        rows = (
            base.filter(search_vector=tsquery)
            .exclude(pk__in=seen)
            .annotate(rank=SearchRank("search_vector", tsquery))
            .order_by("-rank", "document_id", "position")[: limit - len(hits)]
        )
        for chunk in rows:
            seen.add(chunk.pk)
            hits.append(Hit(chunk, float(chunk.rank), terms))
        if len(hits) >= limit:
            break
    return hits


def linked(kind: str, object_id: int, limit: int = 10) -> list[Document]:
    """The ready documents linked to one partner, programme document, section or place, the most
    mentioned first."""
    return list(
        Document.objects.filter(status=Document.Status.READY, links__kind=kind, links__object_id=object_id)
        .order_by("-links__mentions", "-created_at")
        .distinct()[:limit]
    )


def documents_matching(
    query: str, filters: Filters | None = None, limit: int = 30
) -> list[tuple[Document, Hit]]:
    """For the knowledge page: the documents with a matching passage, each with its best passage."""
    best: dict[int, Hit] = {}
    for hit in search(query, filters, limit=limit * 3):
        best.setdefault(hit.document.pk, hit)
    return [(hit.document, hit) for hit in list(best.values())[:limit]]
