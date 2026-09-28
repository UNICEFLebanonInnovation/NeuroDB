"""Sidebar and landing-page content, generated from the database (v2 hard-coded it)."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.cache import cache

from neurodb.accounts.models import Section
from neurodb.indicators.models import Database, NeuroReport, ReportingYear

CACHE_KEY = "nav:v2:{}"
CACHE_SECONDS = 120


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


def invalidate() -> None:
    ids = list(ReportingYear.objects.values_list("id", flat=True))
    cache.delete_many([CACHE_KEY.format(i) for i in ids] + [CACHE_KEY.format("none")])
