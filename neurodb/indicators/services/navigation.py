"""Sidebar and landing-page content, generated from the database (v2 hard-coded it)."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.cache import cache

from neurodb.accounts.models import Section
from neurodb.indicators.models import Database, NeuroReport, ReportingYear

CACHE_KEY = "nav:v1"
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


def current_year() -> ReportingYear | None:
    return ReportingYear.objects.filter(current=True).order_by("-name").first() or ReportingYear.objects.order_by("-name").first()


def build_navigation(year: ReportingYear | None = None) -> Navigation:
    year = year or current_year()
    cached = cache.get(CACHE_KEY) if year is None or year.current else None
    if cached:
        return cached
    databases = (
        Database.objects.filter(reporting_year=year, display=True).select_related("section").order_by("section__name", "label", "name")
        if year
        else Database.objects.none()
    )
    by_section: dict[int | None, SectionNav] = {}
    for db in databases:
        key = db.section_id
        by_section.setdefault(key, SectionNav(section=db.section)).databases.append(db)
    reports = NeuroReport.objects.filter(ryear=year, is_active=True).order_by("name") if year else NeuroReport.objects.none()
    nav = Navigation(
        year=year,
        sections=sorted(by_section.values(), key=lambda s: (s.section.name if s.section else "zzz")),
        neuro_reports=[r for r in reports if not r.is_hpm],
        hpm_reports=[r for r in reports if r.is_hpm],
        years=list(ReportingYear.objects.order_by("-name")),
    )
    if year and year.current:
        cache.set(CACHE_KEY, nav, CACHE_SECONDS)
    return nav


def invalidate() -> None:
    cache.delete(CACHE_KEY)
