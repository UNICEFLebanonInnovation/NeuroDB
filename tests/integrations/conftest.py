"""Shared fixtures for the integrations test-suite (the test database comes from the migrations)."""

from __future__ import annotations

import datetime as dt

import pytest


# ------------------------------------------------------------------ model fixtures
@pytest.fixture
def reporting_year(db):
    from neurodb.indicators.models import ReportingYear

    return ReportingYear.objects.create(name="2025", year="2025", current=True, database_id="ck2yrizmo2")


@pytest.fixture
def database(reporting_year):
    """A sector database shaped like the v2 ``pivoting_database`` rows (ai_id = <year><sector>)."""
    from neurodb.indicators.models import Database

    return Database.objects.create(
        ai_id=202516,
        parent_id="ck2yrizmo2",
        db_id="cfolder16",
        name="Child Protection",
        username="",
        password="",
        reporting_year=reporting_year,
        is_funded_by_unicef=False,
        have_offices=False,
    )


@pytest.fixture
def nutrition_database(reporting_year):
    from neurodb.indicators.models import Database

    return Database.objects.create(
        ai_id=202518,
        parent_id="ck2yrizmo2",
        db_id="cfolder18",
        name="Nutrition",
        username="",
        password="",
        reporting_year=reporting_year,
        is_funded_by_unicef=True,
    )


@pytest.fixture
def today() -> dt.date:
    return dt.date(2025, 6, 15)
