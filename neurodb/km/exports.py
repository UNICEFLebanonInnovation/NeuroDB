"""The KM Database's downloads: **Excel of the filter** and **CSV of the filter**, on both tabs.

The user's rule for authors: a download names the organisations an author entry names, and counts the
individual authors, never naming them ("Fakeorg Institute + 2 authors", "Authors: 3"; :mod:`.authors`).
Names of people inside every text are withheld too (:func:`.privacy.for_download`): only the Finding ID and
the Document ID are written as they are. The Excel file adds an *About* sheet: the version, the filter in
words and the rule. The search box's words are written there only when they name no author.

The helpers of :mod:`neurodb.reports.exports` (file names, the streamed CSV, the typed Excel writer, the
limit's sentence) keep every text a text (never a formula); they read no other data.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from django.http import HttpResponse
from django.utils.translation import gettext as _

from neurodb.reports import exports as report_exports
from neurodb.watch import people as watch_people

from . import authors, privacy, words
from . import columns as cols
from .filters import NONE, Filters
from .models import Flag, KMVersion, day

XLSX_MAX = 20_000  # findings an Excel file holds (the CSV has no limit)
CHUNK = 2_000
FLAG_WORDS = dict(Flag.choices)
FINDING_COLUMNS: tuple[str, ...] = (
    "finding_id",
    "document_id",
    "item_title",
    "document_title",
    "publishing_organisations",
    "publication_year",
    "study_type",
    "document_type",
    "pillar",
    "analytical_domain",
    "research_question",
    "sub_question",
    "sub_sub_question",
    "indicator_or_evidence",
    "indicator_component_answered",
    "also_relevant_to",
    "measure",
    "key_finding",
    "finding_analysis",
    "age_category",
    *(flag.field for flag in cols.FLAGS),
    "limitations",
    "evidence_origin",
    "evidence_confidence_level",
    "study_period",
    "population",
    "methods_and_sample",
    "source_link",
    "data_quality_status",
    "values_to_check",
)
FINDING_TEXTS = {  # download column → the finding's field, each cleaned of people's names
    "item_title": "item_title",
    "document_title": "document_title",
    "study_type": "study_type",
    "document_type": "document_type",
    "pillar": "pillar",
    "analytical_domain": "domain",
    "research_question": "research_question",
    "sub_question": "sub_question",
    "sub_sub_question": "sub_sub_question",
    "indicator_or_evidence": "indicator",
    "indicator_component_answered": "indicator_component",
    "also_relevant_to": "also_relevant",
    "measure": "measure",
    "key_finding": "key_finding",
    "finding_analysis": "analysis",
    "age_category": "age_text",
    "limitations": "limitations",
    "evidence_origin": "evidence_origin",
    "evidence_confidence_level": "confidence_text",
    "study_period": "period",
    "population": "population",
    "methods_and_sample": "methods",
    "source_link": "link",
    "data_quality_status": "quality",
}
DOCUMENT_COLUMNS: tuple[str, ...] = (
    "document_id",
    "item_title",
    "document_title",
    "publishing_organisations",
    "publication_year",
    "study_type",
    "text_used",
    "evidence_confidence_level",
    "number_of_findings",
    "document_link",
    "data_quality_status",
    "values_to_check",
)
DOCUMENT_TEXTS = {
    "item_title": "item_title",
    "document_title": "title",
    "study_type": "study_type",
    "text_used": "text_used",
    "evidence_confidence_level": "confidence_text",
    "document_link": "link",
    "data_quality_status": "quality",
}
ABOUT_COLUMNS = ("KM Database", "")


class Cleaner:
    """The texts of one download, cleaned once each (a text repeats on every finding of its document)."""

    def __init__(self, people: privacy.People):
        self.people = people
        self._seen: dict[tuple[str, frozenset[str]], str] = {}

    def __call__(self, text: str, document: str | None) -> str:
        """``text`` cleaned, with the surnames of the own authors of ``document`` (its Document ID)."""
        if not text:
            return ""
        own = self.people.own.get(document, frozenset()) if document is not None else frozenset()
        key = (text, own)
        found = self._seen.get(key)
        if found is None:
            found = self._seen[key] = privacy.for_download(text, self.people, own)
        return found


def _year(row, clean: Cleaner, document: str) -> Any:
    return row.year if row.year is not None else clean(row.year_text, document)


def _checks(row) -> str:
    return "; ".join(row.checks) if row.checks else ""


def finding_rows(qs, known: dict[str, str], people: privacy.People) -> Iterator[dict[str, Any]]:
    clean, name = Cleaner(people), authors.Namer(known, people)
    marks = {code: str(label) for code, label in FLAG_WORDS.items()}
    for row in qs:
        document = row.record_id
        out: dict[str, Any] = {
            "finding_id": row.finding_id,
            "document_id": row.record_id,
            "publishing_organisations": name(row.entity),
            "publication_year": _year(row, clean, document),
            "values_to_check": _checks(row),
        }
        for column, field_name in FINDING_TEXTS.items():
            out[column] = clean(getattr(row, field_name), document)
        for flag in cols.FLAGS:
            out[flag.field] = marks.get(getattr(row, flag.field), "")
        yield out


def document_rows(qs, known: dict[str, str], people: privacy.People) -> Iterator[dict[str, Any]]:
    clean, name = Cleaner(people), authors.Namer(known, people)
    for row in qs:
        out: dict[str, Any] = {
            "document_id": row.record_id,
            "publishing_organisations": name(row.entity),
            "publication_year": _year(row, clean, row.record_id),
            "number_of_findings": row.findings_stated,
            "values_to_check": _checks(row),
        }
        for column, field_name in DOCUMENT_TEXTS.items():
            out[column] = clean(getattr(row, field_name), row.record_id)
        yield out


# ------------------------------------------------------------------------------------------- the filter
def filter_words(filters: Filters, facets: dict[str, Any], people: privacy.People) -> str:
    """The filter in words, for the About sheet ("Pillar: P2; Year: 2024; Search: school")."""
    legend = {q["key"]: q["legend"] for q in facets.get("questions", ())}
    labels = {flag.field: flag.label for flag in cols.FLAGS}
    found: list[str] = []

    def add(label: str, values) -> None:
        if values:
            found.append(f"{label}: {', '.join(str(v) for v in values)}")

    add(_("Pillar"), filters.pillar)
    add(_("Research question"), [legend.get(k, k) for k in filters.rq])
    add(_("Confidence"), [_("Not given") if c == NONE else f"Level {c}" for c in filters.confidence])
    add(_("Year"), [_("Not reported") if y == NONE else y for y in filters.year])
    if filters.type:  # a type is the workbook's text: written only when it names nobody
        if all(search_words_safe(t, people) for t in filters.type):
            add(_("Document type"), filters.type)
        else:
            found.append(str(words.TYPE_NOT_WRITTEN))
    if filters.document:
        add(_("Document ID"), [filters.document])
    add(_("Analytical domain"), filters.domain)
    for key, title in (
        ("age", cols.AGE),
        ("sex", cols.SEX),
        ("sem", cols.SEM),
        ("theme", cols.THEMES),
    ):
        add(title, [labels[f] for f in getattr(filters, key)])
    add(_("Evidence"), [str(words.ORIGIN_LABELS[o]) for o in filters.origin])
    if filters.quality:
        add(_("Data quality"), [str(words.QUALITY_LABELS[filters.quality])])
    if filters.q:
        found.append(
            f"{_('Search')}: {filters.q}"
            if search_words_safe(filters.q, people)
            else str(words.SEARCH_NOT_WRITTEN)
        )
    return "; ".join(found) or str(words.NO_FILTER)


def search_words_safe(text: str, people: privacy.People) -> bool:
    """Whether the search box's words (or a document type chosen) may be written in a download: they name no
    author of the KM Database and nothing in them is withheld."""
    if privacy.for_download(text, people) != text:
        return False
    if {watch_people.fold(w) for w in watch_people.WORD.findall(text)} & people.surnames:
        return False
    return not watch_people.mentions(text, names=people.names)


def about_rows(version: KMVersion, filters: Filters, facets: dict[str, Any], people, rows: int):
    first, second = ABOUT_COLUMNS
    lines = [
        (
            str(words.ABOUT_PREPARED),
            day(version.prepared_on) if version.prepared_on else str(words.NOT_KNOWN),
        ),
        (str(words.ABOUT_LOADED), day(version.finished_at or version.uploaded_at)),
        (str(words.ABOUT_FILTER), filter_words(filters, facets, people)),
        (str(words.ABOUT_ROWS), rows),
        (str(words.ABOUT_AUTHORS), str(words.ABOUT_AUTHORS_RULE)),
        (str(words.ABOUT_NAMES), str(words.ABOUT_NAMES_RULE)),
    ]
    return [{first: label, second: value} for label, value in lines]


# ------------------------------------------------------------------------------------------- the files
def findings_export(
    qs, fmt: str, version: KMVersion, filters: Filters, facets: dict[str, Any]
) -> HttpResponse:
    known, people = authors.context(version)
    qs = qs.select_related(None).defer("search_vector", "own_vector")
    if fmt == "csv":
        name = report_exports.export_filename("km-database-findings", "csv")
        return report_exports.stream_csv(
            name, FINDING_COLUMNS, finding_rows(qs.iterator(chunk_size=CHUNK), known, people)
        )
    found = list(qs[: XLSX_MAX + 1])
    if len(found) > XLSX_MAX:
        return report_exports.too_many(qs.count(), XLSX_MAX, _("findings"), _("a pillar"))
    name = report_exports.export_filename("km-database-findings", "xlsx")
    sheets = [
        (str(words.TAB_FINDINGS), FINDING_COLUMNS, finding_rows(found, known, people)),
        (str(words.ABOUT_SHEET), ABOUT_COLUMNS, about_rows(version, filters, facets, people, len(found))),
    ]
    return report_exports.xlsx_response(name, sheets, typed=True)


def documents_export(
    qs, fmt: str, version: KMVersion, filters: Filters, facets: dict[str, Any]
) -> HttpResponse:
    known, people = authors.context(version)
    qs = qs.defer("search_vector")
    if fmt == "csv":
        name = report_exports.export_filename("km-database-documents", "csv")
        return report_exports.stream_csv(
            name, DOCUMENT_COLUMNS, document_rows(qs.iterator(chunk_size=CHUNK), known, people)
        )
    found = list(qs)
    name = report_exports.export_filename("km-database-documents", "xlsx")
    sheets = [
        (str(words.TAB_DOCUMENTS), DOCUMENT_COLUMNS, document_rows(found, known, people)),
        (str(words.ABOUT_SHEET), ABOUT_COLUMNS, about_rows(version, filters, facets, people, len(found))),
    ]
    return report_exports.xlsx_response(name, sheets, typed=True)
