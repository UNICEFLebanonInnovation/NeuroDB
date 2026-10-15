"""Sidebar and landing-page content, generated from the database (v2 hard-coded it)."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.cache import cache

from neurodb.accounts.models import Section
from neurodb.indicators.models import Database, NeuroReport, ReportingYear

CACHE_KEY = "nav:v2:{}"
CACHE_SECONDS = 120
SOURCES_KEY = "nav:v3:sources"  # what the menu shows of the sources that may be off (sources())


@dataclass
class SectionNav:
    section: Section | None
    databases: list[Database] = field(default_factory=list)


@dataclass
class Navigation:
    year: ReportingYear | None
    sections: list[SectionNav]
    neuro_reports: list[NeuroReport]
    hpm_reports: list[NeuroReport]
    years: list[ReportingYear]
    # The year each block lists: the page's year, or the latest year that has any when it has none
    # (a new reporting year starts empty; the menus stay, labelled with the year they show).
    databases_year: ReportingYear | None = None
    neuro_year: ReportingYear | None = None
    hpm_year: ReportingYear | None = None

    @property
    def database_count(self) -> int:
        """Databases listed (a section may hold several), the figure the Databases page counts."""
        return sum(len(s.databases) for s in self.sections)


def current_year() -> ReportingYear | None:
    return (
        ReportingYear.objects.filter(current=True).order_by("-name").first()
        or ReportingYear.objects.order_by("-name").first()
    )


def _with_fallback(qs_for, year: ReportingYear | None, years: list[ReportingYear]):
    """``qs_for(y)`` for the given year, else for the latest year (by name) where it is not empty."""
    for candidate in ([year] if year else []) + [y for y in years if not year or y.id != year.id]:
        rows = list(qs_for(candidate))
        if rows:
            return candidate, rows
    return year, []


def build_navigation(year: ReportingYear | None = None) -> Navigation:
    """The sidebar for ``year`` (default: the current reporting year)."""
    year = year or current_year()
    key = CACHE_KEY.format(year.id if year else "none")
    cached = cache.get(key)
    if cached:
        return cached
    years = list(ReportingYear.objects.order_by("-name"))
    databases_year, databases = _with_fallback(
        lambda y: (
            Database.objects.filter(reporting_year=y, display=True)
            .select_related("section")
            .order_by("section__name", "label", "name")
        ),
        year,
        years,
    )
    by_section: dict[int | None, SectionNav] = {}
    for db in databases:
        by_section.setdefault(db.section_id, SectionNav(section=db.section)).databases.append(db)

    def reports(hpm: bool):
        return lambda y: NeuroReport.objects.filter(ryear=y, is_active=True, is_hpm=hpm).order_by("name")

    neuro_year, neuro_reports = _with_fallback(reports(False), year, years)
    hpm_year, hpm_reports = _with_fallback(reports(True), year, years)
    nav = Navigation(
        year=year,
        sections=sorted(by_section.values(), key=lambda s: s.section.name if s.section else "zzz"),
        neuro_reports=neuro_reports,
        hpm_reports=hpm_reports,
        years=years,
        databases_year=databases_year,
        neuro_year=neuro_year,
        hpm_year=hpm_year,
    )
    cache.set(key, nav, CACHE_SECONDS)
    return nav


def sources() -> dict:
    """Whether the sources the menu shows only when live have anything to show, the same for every person
    (plain data, kept two minutes per worker):

    - ``cpd_cycle``: ``[start year, end year]`` of the country programme cycle the Country programme page
      opens on (any cycle, an ended one too: it is history), or None;
    - ``cpd_live``: today falls inside that cycle;
    - ``compiler``: Compiler is connected, or Makani, Dirasa or youth figures are stored;
    - ``doc_review``: the document review is switched on, or it has found anything;
    - ``forecasts``: year-end forecasts exist.

    Who sees a muted "Set up" row instead of a missing item is the caller's choice
    (:func:`neurodb.accounts.roles.can_set_up`)."""
    cached = cache.get(SOURCES_KEY)
    if cached is not None:
        return cached
    from neurodb.core import years
    from neurodb.cpd.services import current_cycle
    from neurodb.education.models import EducationFigures
    from neurodb.insights.models import IndicatorForecast
    from neurodb.knowledge.models import DocumentFinding, DocumentReviewSettings
    from neurodb.youth import compiler
    from neurodb.youth.models import YouthFigures

    cycle = current_cycle()
    today = years.today()
    review_on = DocumentReviewSettings.objects.filter(pk=1).values_list("enabled", flat=True).first()
    value = {
        "cpd_cycle": [cycle.start_year, cycle.end_year] if cycle else None,
        "cpd_live": bool(cycle and cycle.start_year <= today.year <= cycle.end_year),
        "compiler": bool(
            compiler.configured() or EducationFigures.objects.exists() or YouthFigures.objects.exists()
        ),
        "doc_review": bool(review_on or DocumentFinding.objects.exists()),
        "forecasts": IndicatorForecast.objects.exists(),
    }
    cache.set(SOURCES_KEY, value, CACHE_SECONDS)
    return value


def invalidate() -> None:
    ids = list(ReportingYear.objects.values_list("id", flat=True))
    cache.delete_many([CACHE_KEY.format(i) for i in ids] + [CACHE_KEY.format("none"), SOURCES_KEY])
