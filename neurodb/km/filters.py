"""The KM Database's filters: read from the address (``Filters.from_query``), keeping only the values the
version shown holds (codes and keys of its facets, whole years), then applied to the findings or, through
their findings, to the documents.

Several choices in one filter match any of them; several filters together match all of them. A research
question is keyed by its pillar and its wording (``columns.question_key``): when pillars are chosen, the
questions of other pillars are left out (``dropped_questions``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import reduce
from operator import or_
from typing import Any

from django.core.cache import cache
from django.db.models import Case, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.functions import Length
from django.http import QueryDict

from . import columns as cols
from .models import KMDocument, KMFinding, KMVersion

FACETS_SECONDS = 24 * 3600  # keyed by the version: a new version misses them by itself
PER_PAGE = (25, 50, 100)
NONE = "none"  # "Not given" (confidence), "Not reported" (year), "not given" (evidence origin)
LETTERS = ("A", "B", "C", "D")
ORIGINS = ("P", "O", NONE)
QUALITY = ("ready", "review")
FINDING_SORTS = ("year", "-year", "confidence", "pillar", "document")
DOCUMENT_SORTS = ("year", "-year", "title", "findings")
FLAG_PARAMS = {  # the address's key of each group of marks, and its fields
    "age": tuple(f.field for f in cols.FLAGS if f.group == cols.AGE),
    "sex": tuple(f.field for f in cols.FLAGS if f.group == cols.SEX),
    "sem": tuple(f.field for f in cols.FLAGS if f.group == cols.SEM),
    "theme": tuple(f.field for f in cols.FLAGS if f.group == cols.THEMES),
}
# every key a filter reads from the address (the Ask tab keeps these but q, sort, per and export)
LIST_KEYS = ("pillar", "rq", "confidence", "year", "type", "domain", *FLAG_PARAMS, "origin")
MORE_KEYS = ("domain", *FLAG_PARAMS, "origin", "quality")  # the filters folded under "More filters"
_NUMBER = re.compile(r"\d+")


def _number(code: str) -> int:
    found = _NUMBER.search(code or "")
    return int(found.group()) if found else 0


# ------------------------------------------------------------------------------------------------ facets
def facets(version: KMVersion | None) -> dict[str, Any]:
    """What the version shown holds, for the filters' choices: its pillars (code, label), its research
    questions (pillar, key, code, label and the key the assistant's legend gives it), its years (newest
    first, and whether a finding has none), its document types, its analytical domains and its confidence
    letters. Six queries, once per version (kept a day under the version's own key)."""
    if version is None:
        return {"pillars": [], "questions": [], "years": [], "no_year": False, "types": [], "domains": []}
    key = f"km:facets:{version.pk}"
    found = cache.get(key)
    if found is None:
        found = _facets()
        cache.set(key, found, FACETS_SECONDS)
    return found


def _facets() -> dict[str, Any]:
    findings = KMFinding.objects.order_by()
    pillars = sorted(
        findings.exclude(pillar_code="")
        .order_by("pillar_code", "position")
        .distinct("pillar_code")
        .values_list("pillar_code", "pillar"),
        key=lambda p: (_number(p[0]), p[0]),
    )
    rows = sorted(
        findings.exclude(question_key="")
        .order_by("pillar_code", "question_key", "position")
        .distinct("pillar_code", "question_key")
        .values_list("pillar_code", "question_key", "rq_code", "research_question", "position"),
        key=lambda q: (_number(q[0]), q[0], _number(q[2]), q[4]),
    )
    questions = []
    per_code: dict[tuple[str, str], list[dict]] = {}
    for pillar, key, code, label, _position in rows:
        question = {"pillar": pillar, "key": key, "code": code, "label": label}
        questions.append(question)
        per_code.setdefault((pillar, code), []).append(question)
    for (pillar, code), same in per_code.items():
        for n, question in enumerate(same):
            letter = chr(ord("a") + n) if len(same) > 1 and n < 26 else ""
            question["legend"] = f"{pillar or '?'}-{code or '?'}{letter}"
    years = list(findings.values_list("year", flat=True).distinct())
    return {
        "pillars": pillars,
        "questions": questions,
        "years": sorted((y for y in years if y is not None), reverse=True),
        "no_year": None in years,
        "types": sorted(
            t for t in KMDocument.objects.order_by().values_list("study_type", flat=True).distinct() if t
        ),
        "domains": sorted(
            findings.exclude(domain_code="")
            .order_by("domain_code", "position")
            .distinct("domain_code")
            .values_list("domain_code", "domain"),
            key=lambda d: (_number(d[0]), d[0]),
        ),
        "letters": sorted(
            c for c in findings.values_list("confidence", flat=True).distinct() if c in LETTERS
        ),
    }


# ----------------------------------------------------------------------------------------------- filters
@dataclass
class Filters:
    q: str = ""
    pillar: list[str] = field(default_factory=list)
    rq: list[str] = field(default_factory=list)
    confidence: list[str] = field(default_factory=list)
    year: list[str] = field(default_factory=list)
    type: list[str] = field(default_factory=list)
    document: str = ""
    domain: list[str] = field(default_factory=list)
    age: list[str] = field(default_factory=list)
    sex: list[str] = field(default_factory=list)
    sem: list[str] = field(default_factory=list)
    theme: list[str] = field(default_factory=list)
    origin: list[str] = field(default_factory=list)
    quality: str = ""
    sort: str = ""
    per: int = PER_PAGE[0]
    dropped_questions: bool = False

    @classmethod
    def from_query(
        cls, params: QueryDict, facets_: dict[str, Any] | None = None, *, for_ask: bool = False
    ) -> Filters:
        """The filters of the address, with every value the version does not hold left out. ``for_ask``:
        the assistant's scope, without the search box's words, the sort, the page size and the download."""
        found = facets_ if facets_ is not None else {}
        pillars = {code for code, _ in found.get("pillars", ())}
        questions = {q["key"]: q["pillar"] for q in found.get("questions", ())}
        years = {str(y) for y in found.get("years", ())} | ({NONE} if found.get("no_year") else set())
        types = set(found.get("types", ()))
        domains = {code for code, _ in found.get("domains", ())}

        def chosen(key: str, allowed) -> list[str]:
            return list(dict.fromkeys(v for v in params.getlist(key) if v in allowed))

        document = (params.get("document") or "").strip()

        filters = cls(
            pillar=chosen("pillar", pillars),
            rq=chosen("rq", questions),
            confidence=chosen("confidence", (*LETTERS, NONE)),
            year=chosen("year", years),
            type=chosen("type", types),
            document=document if cols.ID.match(document) else "",
            domain=chosen("domain", domains),
            origin=chosen("origin", ORIGINS),
            quality=params.get("quality") if params.get("quality") in QUALITY else "",
            **{key: chosen(key, fields) for key, fields in FLAG_PARAMS.items()},
        )
        if filters.pillar and filters.rq:
            kept = [key for key in filters.rq if questions[key] in filters.pillar]
            filters.dropped_questions = len(kept) < len(filters.rq)
            filters.rq = kept
        if not for_ask:
            filters.q = (params.get("q") or "").replace("\x00", "").strip()[:200]
            sort = params.get("sort") or ""
            filters.sort = sort if sort in (*FINDING_SORTS, *DOCUMENT_SORTS) else ""
            per = params.get("per") or ""
            filters.per = int(per) if per.isdigit() and int(per) in PER_PAGE else PER_PAGE[0]
        return filters

    # ---------------------------------------------------------------------------------- what is chosen
    @property
    def more(self) -> bool:
        """Whether a filter folded under "More filters" is set (the fold then opens)."""
        return any(getattr(self, key) for key in MORE_KEYS)

    @property
    def any(self) -> bool:
        """Whether the findings shown are fewer than all of them (a filter or a search is set)."""
        return bool(self.q or self.document or any(getattr(self, key) for key in (*LIST_KEYS, "quality")))

    def query(self, *, ask: bool = False, document: bool = True) -> QueryDict:
        """The filters as an address's query (the kept values only): ``ask`` without the search box's words,
        the sort and the page size; ``document=False`` without the document."""
        out = QueryDict(mutable=True)
        if self.q and not ask:
            out["q"] = self.q
        for key in LIST_KEYS:
            values = getattr(self, key)
            if values:
                out.setlist(key, values)
        if self.document and document:
            out["document"] = self.document
        if self.quality:
            out["quality"] = self.quality
        if not ask:
            if self.sort:
                out["sort"] = self.sort
            if self.per != PER_PAGE[0]:
                out["per"] = str(self.per)
        return out

    # ------------------------------------------------------------------------------------------- applying
    def _finding_q(self, *, document_level: bool) -> Q:
        """The filters a finding meets. ``document_level``: also the confidence, year and type (the
        Findings tab; the Documents tab reads those on the document)."""
        found = Q()
        if self.pillar:
            found &= Q(pillar_code__in=self.pillar)
        if self.rq:
            found &= Q(question_key__in=self.rq)
        if self.domain:
            found &= Q(domain_code__in=self.domain)
        for key in FLAG_PARAMS:
            chosen = getattr(self, key)
            if chosen:
                found &= reduce(or_, [Q(**{name: "Y"}) for name in chosen])
        if self.origin:
            found &= Q(origin_kind__in=["" if o == NONE else o for o in self.origin])
        if document_level:
            found &= self._level_q("confidence", "document_type")
            if self.document:
                found &= Q(document__record_id=self.document)
        return found

    def _level_q(self, confidence: str, kind: str) -> Q:
        """The confidence, year, type and quality filters, on a finding's or a document's own fields."""
        found = Q()
        if self.confidence:
            found &= Q(**{f"{confidence}__in": ["" if c == NONE else c for c in self.confidence]})
        if self.year:
            years = [int(y) for y in self.year if y != NONE]
            year = Q(year__in=years) if years else Q(pk__in=[])
            if NONE in self.year:
                year |= Q(year__isnull=True)
            found &= year
        if self.type:
            found &= Q(**{f"{kind}__in": self.type})
        if self.quality == "ready":
            found &= Q(needs_review=False, checks={})
        elif self.quality == "review":
            found &= Q(needs_review=True) | ~Q(checks={})
        return found

    def apply(self, qs: QuerySet[KMFinding]) -> QuerySet[KMFinding]:
        """The findings of ``qs`` that meet every filter (not the search box: :func:`search.find`)."""
        return qs.filter(self._finding_q(document_level=True))

    def documents(self, qs: QuerySet[KMDocument]) -> QuerySet[KMDocument]:
        """The documents of ``qs`` that meet the document's own filters (confidence, year, type, quality) and
        that have a finding meeting the finding filters."""
        qs = qs.filter(self._level_q("confidence", "study_type"))
        finding_q = self._finding_q(document_level=False)
        if finding_q:
            qs = qs.filter(Exists(KMFinding.objects.filter(finding_q, document=OuterRef("pk"))))
        return qs

    # -------------------------------------------------------------------------------------------- sorting
    def finding_order(self, searched: bool) -> tuple:
        """The order of the Findings tab: the sort chosen; else the best match first when the search box
        found words (``searched``), then the newest year, then the workbook's order."""
        newest = (F("year").desc(nulls_last=True), "position")
        if self.sort == "year":
            return (F("year").asc(nulls_last=True), "position")
        if self.sort == "-year":
            return newest
        if self.sort == "confidence":  # Level A to D, not given last
            return (Case(When(confidence="", then=Value(1)), default=Value(0)), "confidence", *newest)
        if self.sort == "pillar":
            return (Length("pillar_code"), "pillar_code", Length("rq_code"), "rq_code", "position")
        if self.sort == "document":
            return ("document__title", "position")
        if searched:
            return (F("rank").desc(nulls_last=True), *newest)
        return newest

    def document_order(self, searched: bool) -> tuple:
        """The order of the Documents tab (``findings``: the most findings first)."""
        if self.sort == "year":
            return (F("year").asc(nulls_last=True), "position")
        if self.sort == "-year":
            return (F("year").desc(nulls_last=True), "position")
        if self.sort == "title":
            return ("title", "position")
        if self.sort == "findings":
            return ("-findings_rows", "position")
        if searched:
            return (F("rank").desc(nulls_last=True), "position")
        return ("position",)
