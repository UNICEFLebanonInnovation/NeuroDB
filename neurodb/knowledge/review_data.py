"""What the document review page counts (``/knowledge/review/``), with no AI: the findings, statements and
action points each tab reads, the filters, the dashboard's figures, the synthesis (themes ranked by
distinct documents, over time, coverage, repeated findings) and the action points' figures. The Word desk
review (``review_docx``) reads the same functions, so the page and the file never disagree.

**What is counted**: the documents still in a batch and not marked reference only; rejected findings and
statements never; with **Verified only** on, only what a person accepted (an action point counts when at
least one finding it cites was accepted). The Findings tab is where people review, so it lists rejected
findings too (struck through), unless it was opened from a figure (``counted``), when it lists exactly the
rows the figure counts.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Avg, Count, Exists, OuterRef, Prefetch, Q, QuerySet
from django.http import QueryDict
from django.utils import timezone

from . import review
from .models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentStatement,
    FindingCategory,
    ReviewBatch,
    Topic,
    TopicProgramme,
    TopicSubtopic,
    Verdict,
)

HIGH_URGENCY = 70  # a statement at or above it is "high urgency"
MIN_DOCUMENTS = (2, 3, 4, 5)  # the Synthesis tab's choice (5 = five or more)
SIMILAR = 0.6  # two findings' word sets at least this alike are "repeated"
REPEATED_CANDIDATES = 3000  # findings compared for repeats, best evidence first
REPEATED_GROUPS = 50
COMMON_WORD_SHARE = 0.2  # a word in more findings than this share does not pair findings on its own
TOP = 15  # bars of the "top" charts
CURRENT_DAYS = 365  # Actions "Current": documents dated within a year of the newest one
EVIDENCE_BANDS = (
    ("0-19", 0, 19),
    ("20-39", 20, 39),
    ("40-59", 40, 59),
    ("60-79", 60, 79),
    ("80-100", 80, 100),
)
PLACED = ("general", "governorate", "district")
COUNTRY_WIDE = "Lebanon (country-wide)"
GAPS = {
    "untagged": "Not tagged (Other)",
    "unplaced": "No place recognised",
    "undated": "No date",
    "page": "No exact page",
    "unreviewed": "Not reviewed",
}
STATEMENT_GAPS = {"uncited": "Citing no finding"}
_WORD = re.compile(r"[a-z0-9]+")
STOP_WORDS = frozenset(
    "a an and are as at be been by for from has have in into is it its of on or that the their them "
    "there these this those to was were which with will would should could than also more most not no "
    "per our we they he she his her all any some such other".split()
)


# a document's full text (megabytes for a long report) and its summary: never shown by the review page,
# so never loaded with the rows that join their document
HEAVY = ("text", "summary")


def light(qs: QuerySet, path: str = "document") -> QuerySet:
    """``qs`` without the full text of the documents it loads (``path``: the relation; "" for documents)."""
    return qs.defer(*(f"{path}__{name}" if path else name for name in HEAVY))


def cited_findings() -> Prefetch:
    """The findings a statement or an action point cites, with their document (for the page link)."""
    return Prefetch("cites", queryset=light(DocumentFinding.objects.select_related("document")))


# ------------------------------------------------------------------------------------------ what is counted
def in_review_documents(batch: int | None = None) -> QuerySet[Document]:
    """The documents of the review: in a batch (archived ones too) and not a reference."""
    qs = Document.objects.filter(review_batch__isnull=False).exclude(
        review_status=Document.ReviewStatus.REFERENCE
    )
    return qs.filter(review_batch_id=batch) if batch else qs


def all_findings(batch: int | None = None) -> QuerySet[DocumentFinding]:
    """Every finding of the review's documents, rejected ones included (the Findings tab)."""
    qs = DocumentFinding.objects.filter(document__review_batch__isnull=False).exclude(
        document__review_status=Document.ReviewStatus.REFERENCE
    )
    return qs.filter(document__review_batch_id=batch) if batch else qs


def findings(batch: int | None = None, verified: bool = False) -> QuerySet[DocumentFinding]:
    """The findings that are counted: never rejected; with Verified only, accepted only."""
    qs = review.shown_findings()
    if batch:
        qs = qs.filter(document__review_batch_id=batch)
    return qs.filter(verdict=Verdict.ACCEPTED) if verified else qs


def all_statements(batch: int | None = None) -> QuerySet[DocumentStatement]:
    qs = DocumentStatement.objects.filter(document__review_batch__isnull=False).exclude(
        document__review_status=Document.ReviewStatus.REFERENCE
    )
    return qs.filter(document__review_batch_id=batch) if batch else qs


def statements(batch: int | None = None, verified: bool = False) -> QuerySet[DocumentStatement]:
    qs = all_statements(batch).exclude(verdict=Verdict.REJECTED)
    return qs.filter(verdict=Verdict.ACCEPTED) if verified else qs


def action_points(batch: int | None = None, verified: bool = False) -> QuerySet[DocumentActionPoint]:
    """The action points that are counted: those citing no finding, or at least one finding not rejected;
    with Verified only, those citing at least one accepted finding."""
    qs = DocumentActionPoint.objects.filter(document__review_batch__isnull=False).exclude(
        document__review_status=Document.ReviewStatus.REFERENCE
    )
    if batch:
        qs = qs.filter(document__review_batch_id=batch)
    through = DocumentActionPoint.cites.through.objects.filter(documentactionpoint_id=OuterRef("pk"))
    if verified:
        return qs.filter(Exists(through.filter(documentfinding__verdict=Verdict.ACCEPTED)))
    live = through.exclude(documentfinding__verdict=Verdict.REJECTED)
    return qs.filter(Q(Exists(live)) | ~Q(Exists(through)))


def any_verdict() -> bool:
    """A finding or a statement was accepted or rejected: the Verified only switch shows."""
    return (
        DocumentFinding.objects.exclude(verdict=Verdict.UNREVIEWED).exists()
        or DocumentStatement.objects.exclude(verdict=Verdict.UNREVIEWED).exists()
    )


def batch_id(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) < 10 else None


def _int(value: Any, low: int = 0, high: int = 10**9) -> int | None:
    text = str(value or "").strip()
    if not text.lstrip("-").isdigit() or len(text) > 10:
        return None
    return min(max(int(text), low), high)


# ------------------------------------------------------------------------------------------ places, years
def place_label(match: str, governorate: str, district: str) -> str:
    """A finding's place as the charts count it: its district, else its governorate, else country-wide."""
    if match == "general":
        return COUNTRY_WIDE
    return district or governorate or ""


def place_filter(label: str) -> Q:
    if label == COUNTRY_WIDE:
        return Q(place_match="general")
    return Q(district_name=label) | Q(district_name="", governorate_name=label, place_match="governorate")


def document_year(row: dict[str, Any]) -> int | None:
    """The year a document is about: its year, else its date, else its issue date."""
    if row.get("document__year"):
        return row["document__year"]
    for name in ("document__document_date", "document__issued_on"):
        if row.get(name):
            return row[name].year
    return None


def finding_year(row: dict[str, Any]) -> int | None:
    """A finding's year: its own date's, else its document's (``document_year``)."""
    return row["finding_date"].year if row.get("finding_date") else document_year(row)


def as_of(document: Document) -> datetime.date:
    """A document's date for "Current": its date, its issue date, the end of its year, else when added."""
    if document.document_date:
        return document.document_date
    if document.issued_on:
        return document.issued_on
    if document.year:
        return datetime.date(document.year, 12, 31)
    return timezone.localtime(document.created_at).date()


# ------------------------------------------------------------------------------------------ the Findings tab
@dataclass
class FindingFilter:
    """The Findings tab's filter, read from the query string."""

    batch: int | None = None
    programme: int | None = None
    subtopic: int | None = None
    topic: int | None = None
    category: str = ""
    evidence: int | None = None
    verdict: str = ""
    q: str = ""
    document: int | None = None
    year: int | None = None
    place: str = ""
    gap: str = ""
    band: str = ""
    counted: bool = False

    @classmethod
    def from_params(cls, params: QueryDict) -> FindingFilter:
        category = params.get("category", "")
        verdict = params.get("verdict", "")
        gap = params.get("gap", "")
        band = params.get("band", "")
        return cls(
            batch=batch_id(params.get("batch")),
            programme=batch_id(params.get("programme")),
            subtopic=batch_id(params.get("subtopic")),
            topic=batch_id(params.get("topic")),
            category=category if category in FindingCategory.values else "",
            evidence=_int(params.get("evidence"), 0, 100),
            verdict=verdict if verdict in Verdict.values else "",
            q=" ".join(params.get("q", "").split())[:200],
            document=batch_id(params.get("document")),
            year=_int(params.get("year"), 1900, 2200),
            place=params.get("place", "").strip()[:100],
            gap=gap if gap in GAPS else "",
            band=band if band in {b[0] for b in EVIDENCE_BANDS} else "",
            counted=params.get("counted") == "1",
        )

    def apply(self, verified: bool) -> QuerySet[DocumentFinding]:
        qs = findings(self.batch, verified) if self.counted else all_findings(self.batch)
        if self.programme:
            qs = qs.filter(topic__subtopic__programme_id=self.programme)
        if self.subtopic:
            qs = qs.filter(topic__subtopic_id=self.subtopic)
        if self.topic:
            qs = qs.filter(topic_id=self.topic)
        if self.category:
            qs = qs.filter(category=self.category)
        if self.evidence is not None:
            qs = qs.filter(evidence__gte=self.evidence)
        if self.verdict:
            qs = qs.filter(verdict=self.verdict)
        if self.document:
            qs = qs.filter(document_id=self.document)
        if self.year:
            qs = qs.filter(finding_date__year=self.year)
        if self.place:
            qs = qs.filter(place_filter(self.place))
        if self.band:
            low, high = next((lo, hi) for name, lo, hi in EVIDENCE_BANDS if name == self.band)
            qs = qs.filter(evidence__gte=low, evidence__lte=high)
        if self.gap:
            qs = qs.filter(gap_filter(self.gap))
        if self.q:
            for word in self.q.split()[:8]:
                qs = qs.filter(
                    Q(text__icontains=word)
                    | Q(quote__icontains=word)
                    | Q(document__title__icontains=word)
                    | Q(place_text__icontains=word)
                    | Q(topic__name__icontains=word)
                )
        return qs

    def chips(self) -> list[tuple[str, str]]:
        """The drill-downs the filter bar does not show: (key, words)."""
        out = []
        if self.counted:
            out.append(("counted", "As counted on the figures (rejected left out)"))
        if self.document:
            title = Document.objects.filter(pk=self.document).values_list("title", flat=True).first()
            out.append(("document", f"Document: {title or self.document}"))
        if self.subtopic:
            found = TopicSubtopic.objects.filter(pk=self.subtopic).first()
            out.append(("subtopic", f"Subtopic: {found or self.subtopic}"))
        if self.year:
            out.append(("year", f"Year: {self.year}"))
        if self.place:
            out.append(("place", f"Place: {self.place}"))
        if self.gap:
            out.append(("gap", GAPS[self.gap]))
        if self.band:
            out.append(("band", f"Evidence {self.band}"))
        return out


def gap_filter(gap: str) -> Q:
    other = Topic.other()
    return {
        "untagged": Q(topic=other),
        "unplaced": ~Q(place_match__in=PLACED),
        "undated": Q(finding_date__isnull=True),
        "page": Q(exact_page=False),
        "unreviewed": Q(verdict=Verdict.UNREVIEWED),
    }[gap]


@dataclass
class StatementFilter:
    batch: int | None = None
    document: int | None = None
    verdict: str = ""
    q: str = ""
    urgent: bool = False
    gap: str = ""
    counted: bool = False

    @classmethod
    def from_params(cls, params: QueryDict) -> StatementFilter:
        verdict = params.get("verdict", "")
        gap = params.get("gap", "")
        return cls(
            batch=batch_id(params.get("batch")),
            document=batch_id(params.get("document")),
            verdict=verdict if verdict in Verdict.values else "",
            q=" ".join(params.get("q", "").split())[:200],
            urgent=params.get("urgent") == "1",
            gap=gap if gap in STATEMENT_GAPS else "",
            counted=params.get("counted") == "1",
        )

    def apply(self, verified: bool) -> QuerySet[DocumentStatement]:
        qs = statements(self.batch, verified) if self.counted else all_statements(self.batch)
        if self.document:
            qs = qs.filter(document_id=self.document)
        if self.verdict:
            qs = qs.filter(verdict=self.verdict)
        if self.urgent:
            qs = qs.filter(urgency__gte=HIGH_URGENCY)
        if self.gap == "uncited":
            qs = qs.filter(cites__isnull=True)
        if self.q:
            for word in self.q.split()[:8]:
                qs = qs.filter(Q(text__icontains=word) | Q(document__title__icontains=word))
        return qs.distinct()


def topic_options() -> dict[str, Any]:
    """The programmes, subtopics and tags for the filters (the switched-off ones too: findings keep them)."""
    programmes = list(TopicProgramme.objects.order_by("order", "name").values_list("pk", "name"))
    topics = [
        (t.pk, t.path)
        for t in Topic.objects.select_related("subtopic__programme").order_by(*Topic._meta.ordering)
    ]
    return {"programmes": programmes, "topics": topics}


# ------------------------------------------------------------------------------------------ the Documents tab
STAGE_LABELS = {
    review.TEXT: "Text",
    review.FINDINGS: "Findings",
    review.PLACES: "Locate",
    review.SUMMARY: "Summary",
    review.ENRICHMENT: "Action points",
}
STATE_KEYS = {review.YES: "succeeded", review.PARTLY: "partial", review.FAILED: "failed"}
STATE_WORDS = {review.YES: "Yes", review.PARTLY: "Partly", review.FAILED: "Failed"}


def stages(document: Document) -> list[dict[str, str]]:
    """The five stage chips of a document: green yes, amber partly, red failed, grey not reached; the
    note (the error, or what was read) on hover."""
    notes = document.review_stage_notes or {}
    out = []
    for stage in review.STAGES:
        note = notes.get(stage) or {}
        state = note.get("state", "")
        hover = note.get("error") or ""
        if not hover and stage == review.FINDINGS and state:
            hover = f"{note.get('found', 0)} findings from {note.get('parts', 0)} part(s)"
        if not hover and stage == review.TEXT and state == review.YES:
            hover = f"{note.get('pages') or 0} page(s), {note.get('characters') or 0:,} characters"
        out.append(
            {
                "stage": stage,
                "label": STAGE_LABELS[stage],
                "key": STATE_KEYS.get(state, "unknown"),
                "state": STATE_WORDS.get(state, "No"),
                "note": hover or ("Not reached" if not state else STATE_WORDS[state]),
            }
        )
    return out


def read_share(document: Document) -> int | None:
    """The share of the document read when it was partly analysed ("only n% read")."""
    note = (document.review_stage_notes or {}).get(review.FINDINGS) or {}
    share = note.get("read_share")
    return share if isinstance(share, int) and share < 100 else None


def stopped_note(document: Document) -> str:
    notes = document.review_stage_notes or {}
    return str(notes.get("stopped") or notes.get("error") or "")


ANALYSED = (Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY)


def document_rows(batch: int | None = None, analysed: bool = False) -> list[Document]:
    """The documents of the batch (or of every batch) with their counts, for the Documents tab and the
    Index view; ``analysed``: those the Dashboard's "Documents analysed" counts (done or partly, not a
    reference)."""
    qs = Document.objects.filter(review_batch__isnull=False)
    if batch:
        qs = qs.filter(review_batch_id=batch)
    if analysed:
        qs = qs.filter(review_status__in=ANALYSED)
    rows = list(
        light(qs.select_related("review_batch"), "")
        .annotate(
            n_findings=Count("findings", distinct=True),
            n_statements=Count("statements", distinct=True),
            n_actions=Count("review_action_points", distinct=True),
            n_reviewed=Count("findings", filter=~Q(findings__verdict=Verdict.UNREVIEWED), distinct=True),
        )
        .order_by("review_batch__archived", "review_batch__name", "title", "pk")
    )
    for document in rows:
        document.stage_chips = stages(document)
        document.read_share = read_share(document)
        document.stopped = stopped_note(document)
    return rows


def batches() -> list[ReviewBatch]:
    """Every batch with its documents (references apart), those analysed and their findings."""
    analysed = (Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY)
    return list(
        ReviewBatch.objects.annotate(
            n_documents=Count(
                "documents",
                filter=~Q(documents__review_status=Document.ReviewStatus.REFERENCE),
                distinct=True,
            ),
            n_references=Count(
                "documents", filter=Q(documents__review_status=Document.ReviewStatus.REFERENCE), distinct=True
            ),
            n_analysed=Count("documents", filter=Q(documents__review_status__in=analysed), distinct=True),
        ).order_by("archived", "name")
    )


# ------------------------------------------------------------------------------------------ the dashboard
def _share(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def dashboard(batch: int | None, verified: bool) -> dict[str, Any]:
    """The Dashboard tab: the five tiles, the six quality tiles, the charts' data (each bar carrying its
    drill value) and the per-batch table."""
    counted = findings(batch, verified)
    said = statements(batch, verified)
    points = action_points(batch, verified)
    other = Topic.other()
    total = counted.count()
    n_statements = said.count()
    analysed = in_review_documents(batch).filter(review_status__in=ANALYSED)
    quality_counts = counted.aggregate(
        tagged=Count("pk", filter=~Q(topic=other)),
        located=Count("pk", filter=Q(place_match__in=PLACED)),
        dated=Count("pk", filter=Q(finding_date__isnull=False)),
        exact=Count("pk", filter=Q(exact_page=True)),
        reviewed=Count("pk", filter=~Q(verdict=Verdict.UNREVIEWED)),
        average=Avg("evidence"),
    )
    cited = said.filter(cites__isnull=False).distinct().count()
    tiles = {
        "documents": analysed.count(),
        "findings": total,
        "statements": n_statements,
        "urgent": said.filter(urgency__gte=HIGH_URGENCY).count(),
        "open_actions": points.filter(status=DocumentActionPoint.Status.OPEN).count(),
    }
    quality = [
        {"key": "untagged", "label": "Tagged", "done": quality_counts["tagged"], "total": total},
        {"key": "unplaced", "label": "Located", "done": quality_counts["located"], "total": total},
        {"key": "undated", "label": "Dated", "done": quality_counts["dated"], "total": total},
        {"key": "page", "label": "Exact page", "done": quality_counts["exact"], "total": total},
        {"key": "unreviewed", "label": "Reviewed", "done": quality_counts["reviewed"], "total": total},
        {"key": "uncited", "label": "Statements cited", "done": cited, "total": n_statements},
    ]
    for tile in quality:
        tile["share"] = _share(tile["done"], tile["total"])
        tile["gap"] = tile["total"] - tile["done"]
    rows = list(
        counted.values(
            "category",
            "topic_id",
            "topic__name",
            "topic__subtopic__programme_id",
            "topic__subtopic__programme__name",
            "evidence",
            "finding_date",
            "place_match",
            "governorate_name",
            "district_name",
        )
    )
    categories = dict(FindingCategory.choices)
    by_category = Counter(r["category"] for r in rows)
    by_programme = Counter(
        (r["topic__subtopic__programme_id"], r["topic__subtopic__programme__name"]) for r in rows
    )
    by_tag = Counter((r["topic_id"], r["topic__name"]) for r in rows if r["topic_id"] != other.pk)
    by_year = Counter(r["finding_date"].year for r in rows if r["finding_date"])
    by_place = Counter(
        label
        for r in rows
        if (label := place_label(r["place_match"], r["governorate_name"], r["district_name"]))
    )
    bands = Counter(
        next(name for name, low, high in EVIDENCE_BANDS if low <= r["evidence"] <= high) for r in rows
    )
    charts = {
        "category": [[str(categories[k]), n, k] for k, n in by_category.most_common()],
        "programme": [[name, n, pk] for (pk, name), n in by_programme.most_common()],
        "tags": [[name, n, pk] for (pk, name), n in by_tag.most_common(TOP)],
        "evidence": [
            {"label": name, "value": bands.get(name, 0), "drill": name} for name, _l, _h in EVIDENCE_BANDS
        ],
        "years": [{"label": str(y), "value": by_year[y], "drill": y} for y in sorted(by_year)],
        "places": [[name, n, name] for name, n in by_place.most_common(TOP)],
    }
    return {
        "tiles": tiles,
        "quality": quality,
        "charts": charts,
        "average_evidence": round(quality_counts["average"])
        if quality_counts["average"] is not None
        else None,
        "undated": total - sum(by_year.values()),
        "per_batch": per_batch(batch, verified),
    }


def per_batch(batch: int | None, verified: bool) -> list[dict[str, Any]]:
    """One row per batch: documents analysed, findings, statements, average evidence, share of
    challenges and the top tag."""
    counted = findings(batch, verified)
    other = Topic.other()
    figures = {
        row["document__review_batch_id"]: row
        for row in counted.values("document__review_batch_id").annotate(
            n=Count("pk"),
            average=Avg("evidence"),
            challenges=Count("pk", filter=Q(category=FindingCategory.CHALLENGE)),
            documents=Count("document", distinct=True),
        )
    }
    said = dict(
        statements(batch, verified)
        .values("document__review_batch_id")
        .annotate(n=Count("pk"))
        .values_list("document__review_batch_id", "n")
    )
    tops: dict[int, tuple[str, int]] = {}
    for row in (
        counted.exclude(topic=other)
        .values("document__review_batch_id", "topic__name")
        .annotate(n=Count("pk"))
        .order_by("document__review_batch_id", "-n", "topic__name")
    ):
        tops.setdefault(row["document__review_batch_id"], (row["topic__name"], row["n"]))
    out = []
    names = ReviewBatch.objects.filter(pk__in=figures.keys() | said.keys()).order_by("archived", "name")
    for found in names:
        row = figures.get(found.pk) or {}
        n = row.get("n", 0)
        out.append(
            {
                "batch": found,
                "documents": row.get("documents", 0),
                "findings": n,
                "statements": said.get(found.pk, 0),
                "average": round(row["average"]) if row.get("average") is not None else None,
                "challenges": _share(row.get("challenges", 0), n),
                "top_tag": tops.get(found.pk, ("", 0))[0],
            }
        )
    return out


# ------------------------------------------------------------------------------------------ synthesis
@dataclass
class Theme:
    """A topic raised by several documents: its documents, batches, years, evidence and findings."""

    topic: Topic
    documents: dict[int, str] = field(default_factory=dict)  # id: title
    batches: dict[int, str] = field(default_factory=dict)
    years: set[int] = field(default_factory=set)
    finding_ids: list[int] = field(default_factory=list)
    evidence: list[int] = field(default_factory=list)
    per_document: Counter = field(default_factory=Counter)
    trend: str = ""

    @property
    def n_documents(self) -> int:
        return len(self.documents)

    @property
    def n_findings(self) -> int:
        return len(self.finding_ids)

    @property
    def average_evidence(self) -> int:
        return round(sum(self.evidence) / len(self.evidence)) if self.evidence else 0

    @property
    def year_list(self) -> str:
        return ", ".join(str(y) for y in sorted(self.years))

    @property
    def year_span(self) -> str:
        if not self.years:
            return ""
        first, last = min(self.years), max(self.years)
        return str(first) if first == last else f"{first}–{last}"


TRENDS = {
    "persistent": "Persistent: raised every year up to the latest",
    "recurring": "Recurring: raised again after a gap",
    "emerging": "Emerging: raised only in the latest year",
    "no_longer": "No longer raised: not in the latest year",
    "undated": "Undated",
}


def trend(years: set[int], latest: int | None) -> str:
    """How a theme moves over the years its findings carry, against the latest year of the collection."""
    if not years or latest is None:
        return "undated"
    if max(years) < latest:
        return "no_longer"
    if len(years) == 1:
        return "emerging"
    first = min(years)
    return "persistent" if years == set(range(first, latest + 1)) else "recurring"


@dataclass
class Synthesis:
    themes: list[Theme]
    latest: int | None
    min_documents: int
    challenges: bool
    q: str
    topics_considered: int = 0

    def over_time(self) -> dict[str, list[Theme]]:
        groups: dict[str, list[Theme]] = {key: [] for key in TRENDS}
        for theme in self.themes:
            groups[theme.trend].append(theme)
        return groups

    def coverage(self) -> list[Theme]:
        """The themes resting on one batch only: one kind of document, no independent corroboration."""
        return [t for t in self.themes if len(t.batches) == 1]


def synthesis(
    batch: int | None, verified: bool, min_documents: int = 2, q: str = "", challenges: bool = False
) -> Synthesis:
    """The themes: topics (Other apart) ranked by the number of distinct documents raising them, then by
    findings; those raised by at least ``min_documents`` (5: five or more) documents."""
    min_documents = min_documents if min_documents in MIN_DOCUMENTS else MIN_DOCUMENTS[0]
    other = Topic.other()
    qs = findings(batch, verified).exclude(topic=other)
    if challenges:
        qs = qs.filter(category=FindingCategory.CHALLENGE)
    rows = list(
        qs.values(
            "pk",
            "topic_id",
            "document_id",
            "document__title",
            "document__review_batch_id",
            "document__review_batch__name",
            "document__year",
            "document__document_date",
            "document__issued_on",
            "finding_date",
            "evidence",
        ).order_by("-evidence", "pk")
    )
    latest = max((y for r in rows if (y := finding_year(r))), default=None)
    by_topic: dict[int, Theme] = {}
    topics = Topic.objects.select_related("subtopic__programme").in_bulk({r["topic_id"] for r in rows})
    for r in rows:
        theme = by_topic.get(r["topic_id"])
        if theme is None:
            theme = by_topic[r["topic_id"]] = Theme(topic=topics[r["topic_id"]])
        theme.documents[r["document_id"]] = r["document__title"]
        theme.batches[r["document__review_batch_id"]] = r["document__review_batch__name"]
        if year := finding_year(r):
            theme.years.add(year)
        theme.finding_ids.append(r["pk"])
        theme.evidence.append(r["evidence"])
        theme.per_document[r["document_id"]] += 1
    words = q.lower().split()
    chosen = [
        t
        for t in by_topic.values()
        if t.n_documents >= min_documents and all(w in t.topic.path.lower() for w in words)
    ]
    for theme in chosen:
        theme.trend = trend(theme.years, latest)
    chosen.sort(key=lambda t: (-t.n_documents, -t.n_findings, t.topic.path))
    return Synthesis(chosen, latest, min_documents, challenges, q, topics_considered=len(by_topic))


def theme_findings(
    topic: int, batch: int | None, verified: bool, challenges: bool = False, limit: int | None = None
) -> list[DocumentFinding]:
    """A theme's findings, best evidence first, one per document before the second of any (so a long
    list shows every document)."""
    qs = light(findings(batch, verified).filter(topic_id=topic).select_related("document", "topic"))
    if challenges:
        qs = qs.filter(category=FindingCategory.CHALLENGE)
    rows = list(qs.order_by("-evidence", "pk"))
    seen: Counter = Counter()
    for finding in rows:
        finding.rank = seen[finding.document_id]
        seen[finding.document_id] += 1
    rows.sort(key=lambda f: (f.rank, -f.evidence, f.pk))
    return rows[:limit] if limit else rows


# ------------------------------------------------------------------------------------------ repeated findings
def word_set(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(review.norm(text)) if len(w) > 2 and w not in STOP_WORDS)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Repeated:
    findings: list[DocumentFinding]

    @property
    def documents(self) -> int:
        return len({f.document_id for f in self.findings})

    @property
    def lead(self) -> DocumentFinding:
        return max(self.findings, key=lambda f: (f.evidence, -f.pk))


def repeated(batch: int | None, verified: bool, limit: int = REPEATED_GROUPS) -> list[Repeated]:
    """Near-identical findings across documents: word sets (normalised, without small words) at least
    ``SIMILAR`` alike (shared words over all words), grouped; only groups spanning two documents or more.
    At most ``REPEATED_CANDIDATES`` findings are compared (best evidence first) and ``limit`` groups
    kept, those spanning the most documents first."""
    candidates = list(
        light(findings(batch, verified).select_related("document", "topic")).order_by("-evidence", "pk")[
            :REPEATED_CANDIDATES
        ]
    )
    sets = [word_set(f.text) for f in candidates]
    postings: dict[str, list[int]] = defaultdict(list)
    for i, words in enumerate(sets):
        for word in words:
            postings[word].append(i)
    common = max(20, int(len(candidates) * COMMON_WORD_SHARE))
    parent = list(range(len(candidates)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, words in enumerate(sets):
        if len(words) < 3:
            continue
        seen: set[int] = set()
        for word in words:
            ids = postings[word]
            if len(ids) > common:
                continue
            for j in ids:
                if j <= i or j in seen:
                    continue
                seen.add(j)
                if candidates[j].document_id == candidates[i].document_id:
                    continue
                if similarity(words, sets[j]) >= SIMILAR:
                    parent[root(j)] = root(i)
    groups: dict[int, list[DocumentFinding]] = defaultdict(list)
    for i, finding in enumerate(candidates):
        groups[root(i)].append(finding)
    found = [Repeated(g) for g in groups.values() if len({f.document_id for f in g}) >= 2]
    found.sort(key=lambda r: (-r.documents, -len(r.findings), -r.lead.evidence))
    return found[:limit]


# ------------------------------------------------------------------------------------------ actions
@dataclass
class ActionFilter:
    period: str = "current"
    status: str = ""
    overdue: bool = False
    batch: int | None = None
    owner: str = ""
    priority: str = ""
    q: str = ""

    @classmethod
    def from_params(cls, params: QueryDict) -> ActionFilter:
        status = params.get("status", "")
        priority = params.get("priority", "")
        return cls(
            period="all" if params.get("period") == "all" else "current",
            status=status if status in DocumentActionPoint.Status.values else "",
            overdue=params.get("overdue") == "1",
            batch=batch_id(params.get("batch")),
            owner=params.get("owner", "").strip()[:200],
            priority=priority if priority in DocumentActionPoint.Priority.values else "",
            q=" ".join(params.get("q", "").split())[:200],
        )

    def base(self, verified: bool) -> QuerySet[DocumentActionPoint]:
        """The action points of the period and batch: what the cards and the chart count."""
        qs = action_points(self.batch, verified)
        if self.period == "current":
            qs = qs.filter(document_id__in=current_documents(self.batch))
        return qs

    def apply(self, verified: bool) -> QuerySet[DocumentActionPoint]:
        qs = self.base(verified)
        if self.status:
            qs = qs.filter(status=self.status)
        if self.overdue:
            qs = qs.filter(overdue_q())
        if self.owner:
            qs = qs.filter(owner_text=self.owner)
        if self.priority:
            qs = qs.filter(priority=self.priority)
        if self.q:
            for word in self.q.split()[:8]:
                qs = qs.filter(
                    Q(action__icontains=word)
                    | Q(owner_text__icontains=word)
                    | Q(document__title__icontains=word)
                )
        return qs


def overdue_q(today: datetime.date | None = None) -> Q:
    """Open, its deadline (a quarter or a year counts to its last day) before today."""
    today = today or timezone.localdate()
    return Q(status=DocumentActionPoint.Status.OPEN, deadline_date__lt=today)


def current_documents(batch: int | None = None) -> list[int]:
    """The documents of "Current": dated within a year of the newest document of the collection
    (``as_of``), so an old workplan's lines are history once a newer one is in."""
    documents = list(
        in_review_documents(batch).only("pk", "document_date", "issued_on", "year", "created_at")
    )
    if not documents:
        return []
    dates = {d.pk: as_of(d) for d in documents}
    since = max(dates.values()) - datetime.timedelta(days=CURRENT_DAYS)
    return [pk for pk, day in dates.items() if day >= since]


def action_figures(base: QuerySet[DocumentActionPoint]) -> dict[str, Any]:
    open_ = DocumentActionPoint.Status.OPEN
    counts = base.aggregate(
        open=Count("pk", filter=Q(status=open_)),
        overdue=Count("pk", filter=overdue_q()),
        high=Count("pk", filter=Q(status=open_, priority=DocumentActionPoint.Priority.HIGH)),
        done=Count("pk", filter=Q(status=DocumentActionPoint.Status.DONE)),
        derived=Count("pk", filter=Q(derived=True)),
    )
    owners = (
        base.filter(status=open_)
        .values("owner_text")
        .annotate(n=Count("pk"))
        .order_by("-n", "owner_text")[:TOP]
    )
    counts["by_owner"] = [[o["owner_text"], o["n"], o["owner_text"]] for o in owners]
    return counts


def owners(base: QuerySet[DocumentActionPoint]) -> list[str]:
    return list(base.order_by("owner_text").values_list("owner_text", flat=True).distinct()[:300])


# ------------------------------------------------------------------------------------------ citations
def cite(finding: DocumentFinding) -> str:
    """ "(Document title, p. n)": where a finding can be checked."""
    title = finding.document.title
    return f"({title}, {finding.page_label})" if finding.page_label else f"({title})"
