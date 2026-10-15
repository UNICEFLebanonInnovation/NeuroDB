"""What the menu knows of the sources that may be off (navigation.sources()) and who sees "Set up" rows."""

import datetime

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER, can_set_up
from neurodb.cpd.models import CountryProgramme
from neurodb.cpd.services import current_cycle
from neurodb.education.models import EducationFigures
from neurodb.indicators.services import navigation
from neurodb.knowledge.models import Document, DocumentFinding, DocumentReviewSettings, Topic

pytestmark = pytest.mark.django_db

NOTHING = {"cpd_cycle": None, "cpd_live": False, "compiler": False, "doc_review": False, "forecasts": False}


@pytest.fixture(autouse=True)
def _no_compiler(settings):
    settings.COMPILER_API_URL = ""
    settings.COMPILER_API_TOKEN = ""


def _sources():
    cache.delete(navigation.SOURCES_KEY)
    return navigation.sources()


def test_nothing_is_live_on_a_new_site():
    assert _sources() == NOTHING
    assert not DocumentReviewSettings.objects.exists()  # read without writing the settings row


def test_the_country_programme_cycle_and_whether_today_falls_in_it(site_today):
    CountryProgramme.objects.create(name="Old", start_year=2021, end_year=2025)
    assert _sources()["cpd_cycle"] == [2021, 2025] and not _sources()["cpd_live"]  # ended: still history
    CountryProgramme.objects.create(name="Now", start_year=2026, end_year=2028)
    assert (_sources()["cpd_cycle"], _sources()["cpd_live"]) == ([2026, 2028], True)  # the newest
    ticked = CountryProgramme.objects.get(name="Old")
    ticked.current = True
    ticked.save()
    assert current_cycle() == ticked and _sources()["cpd_cycle"] == [2021, 2025]  # the one ticked current
    site_today.set(datetime.date(2029, 1, 2))
    CountryProgramme.objects.filter(name="Old").update(current=False)
    assert _sources()["cpd_cycle"] == [2026, 2028] and not _sources()["cpd_live"]


def test_compiler_is_live_when_connected_or_figures_are_stored(settings):
    settings.COMPILER_API_URL, settings.COMPILER_API_TOKEN = "https://compiler.example.org", "token"
    assert _sources()["compiler"]
    settings.COMPILER_API_URL = ""
    assert not _sources()["compiler"]
    EducationFigures.objects.create(programme="mscc", year="2026", fetched_at=timezone.now(), payload={})
    assert _sources()["compiler"]


def test_document_review_is_live_when_switched_on_or_it_found_anything():
    DocumentReviewSettings.objects.create(pk=1, enabled=True)
    assert _sources()["doc_review"]
    DocumentReviewSettings.objects.filter(pk=1).update(enabled=False)
    assert not _sources()["doc_review"]
    document = Document.objects.create(title="Annual report", text="x")
    DocumentFinding.objects.create(document=document, category="other", topic=Topic.objects.first(), text="t")
    assert _sources()["doc_review"]


def test_kept_two_minutes_per_worker_and_dropped_with_the_menu():
    first = navigation.sources()
    CountryProgramme.objects.create(name="Now", start_year=2026, end_year=2028)
    with CaptureQueriesContext(connection) as queries:
        assert navigation.sources() == first  # kept
    assert len(queries) == 0
    navigation.invalidate()
    assert navigation.sources()["cpd_cycle"] == [2026, 2028]
    assert navigation.CACHE_SECONDS == 120


def test_plain_data():
    CountryProgramme.objects.create(name="Now", start_year=2026, end_year=2028)
    value = _sources()
    assert set(value) == set(NOTHING)
    assert all(v is None or isinstance(v, bool | list) for v in value.values())


@pytest.mark.parametrize(
    "role, staff, superuser, expected",
    [
        (ADMIN, True, False, True),
        (None, False, True, True),  # a superuser
        (ADMIN, False, False, False),  # the admin would refuse them
        (VIEWER, True, False, False),  # nothing to set up
        (SECTION_EDITOR, True, False, False),
        (None, True, False, False),  # no role: a Viewer
    ],
)
def test_who_sees_set_up_rows(roles, role, staff, superuser, expected):
    user = User.objects.create_user(username="someone", password="x-pass-123456", is_staff=staff)
    user.is_superuser = superuser
    user.save()
    if role:
        user.groups.add(Group.objects.get(name=role))
    assert can_set_up(user) is expected
    assert can_set_up(AnonymousUser()) is False
