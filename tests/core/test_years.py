"""The site's calendar (neurodb.core.years): one date for every page, this year, the years a menu offers."""

import datetime
import zoneinfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from neurodb.core import years
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.partnerships.models import PCA

BEIRUT = zoneinfo.ZoneInfo("Asia/Beirut")


def test_today_is_beirut_and_this_year_follows_it(site_today):
    assert years.today() == timezone.localdate() == datetime.date(2026, 10, 15)
    assert years.this_year() == 2026
    # 23:30 UTC on 31 December is already 1 January in Beirut
    site_today.set(datetime.datetime(2026, 12, 31, 23, 30, tzinfo=datetime.UTC))
    assert years.today() == datetime.date(2027, 1, 1) and years.this_year() == 2027


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026", 2026),
        (2025, 2025),
        (" 2024 ", 2024),
        ("2026-2027", 2026),  # a reporting year's name: its first four digits
        ("2027", 2027),  # next year is allowed (a page about the year to come)
        ("2000", 2000),
        ("2028", None),  # too far
        ("1999", None),
        ("all", None),
        ("", None),
        (None, None),
        ("20x6", None),
        ("٢٠٢٦", None),  # digits, but not ASCII ones
        ("²⁰²⁶", None),
    ],
)
def test_parse(raw, expected):
    assert years.parse(raw) == expected


def test_resolve_falls_back_to_this_year():
    assert years.resolve("2024") == 2024
    assert years.resolve(None) == years.resolve("nonsense") == years.resolve("1990") == 2026


def test_number_of_reads_year_then_name():
    assert years.number_of(ReportingYear(name="Anything", year="2025")) == 2025
    assert years.number_of(ReportingYear(name="2024-2025", year=None)) == 2024
    assert years.number_of(ReportingYear(name="Next one", year="")) is None
    assert years.number_of(None) is None


@pytest.mark.django_db
def test_available_is_this_year_alone_without_any_data():
    assert years.available() == [2026]


@pytest.mark.django_db
def test_available_goes_back_to_the_oldest_programme_document_newest_first():
    PCA.objects.create(etl_id="1", title="Old", start=datetime.date(2021, 3, 1))
    PCA.objects.create(etl_id="2", title="Undated")
    assert years.available() == [2026, 2025, 2024, 2023, 2022, 2021]


@pytest.mark.django_db
def test_available_goes_back_to_the_first_activityinfo_year_at_most_ten_years():
    ry = ReportingYear.objects.create(name="2012", year="2012")
    db = Database.objects.create(ai_id=1, name="Old", username="", password="", reporting_year=ry)
    ActivityReportNew.objects.create(dbase=db, month_name="2012-05-01", indicator_value=1)
    found = years.available()
    assert found == list(range(2026, 2016, -1)) and len(found) == years.YEARS_SHOWN


@pytest.mark.django_db
def test_available_is_kept_per_worker_and_follows_the_calendar(site_today):
    PCA.objects.create(etl_id="1", title="Old", start=datetime.date(2024, 3, 1))
    assert years.available() == [2026, 2025, 2024]
    with CaptureQueriesContext(connection) as queries:
        assert years.available() == [2026, 2025, 2024]
    assert len(queries) == 0
    site_today.set(datetime.date(2027, 1, 5))  # a new year: a new key
    assert years.available() == [2027, 2026, 2025, 2024]


@pytest.mark.parametrize(
    "day, since",
    [
        (datetime.date(2026, 10, 15), datetime.date(2026, 10, 14)),  # Thursday -> Wednesday
        (datetime.date(2026, 10, 20), datetime.date(2026, 10, 19)),  # Tuesday -> Monday
        (datetime.date(2026, 10, 19), datetime.date(2026, 10, 16)),  # Monday -> Friday
        (datetime.date(2026, 10, 18), datetime.date(2026, 10, 16)),  # Sunday -> Friday
        (datetime.date(2026, 10, 17), datetime.date(2026, 10, 16)),  # Saturday -> Friday
        (datetime.date(2027, 1, 1), datetime.date(2026, 12, 31)),  # Friday 1 January -> Thursday
    ],
)
def test_since_working_day(site_today, day, since):
    site_today.set(day)
    start = years.since_working_day()
    assert start == datetime.datetime.combine(since, datetime.time.min, tzinfo=BEIRUT)
    assert timezone.is_aware(start) and timezone.localtime(start).time() == datetime.time.min


def test_since_working_day_counts_a_friday_evening_on_monday(site_today):
    site_today.set(datetime.date(2026, 10, 19))  # Monday
    friday_evening = datetime.datetime(2026, 10, 16, 20, 30, tzinfo=BEIRUT)
    assert years.since_working_day() <= friday_evening
