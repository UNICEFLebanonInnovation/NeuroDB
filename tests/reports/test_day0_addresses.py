"""The two new addresses that exist from the day-0 commit, so the menu and search can link to them before their
pages are built: the Country overview at /country-overview/ (today's overview) and the Daily review at
/daily-review/ (for now, the review card on the overview). G2 replaces these tests with the pages' own."""

import pytest
from django.urls import resolve, reverse

from neurodb.reports import overview as overview_service
from neurodb.reports import views

from .overview_fake import fake_data, fake_freshness, fake_options

pytestmark = pytest.mark.django_db


def test_the_names_and_addresses():
    assert reverse("reports:country_overview") == "/country-overview/"
    assert reverse("reports:daily_review") == "/daily-review/"
    assert reverse("reports:overview") == "/"


def test_the_country_overview_is_todays_overview(client_viewer, reporting_year, monkeypatch):
    monkeypatch.setattr(overview_service, "build", lambda scope, **kwargs: fake_data())
    monkeypatch.setattr(overview_service, "options", lambda year: fake_options())
    monkeypatch.setattr(overview_service, "freshness", fake_freshness)
    assert resolve("/country-overview/").func is views.overview
    response = client_viewer.get("/country-overview/?year=2026")
    assert response.status_code == 200
    assert "Country overview" in response.text and "Children reached" in response.text


def test_the_daily_review_leads_to_the_review_card(client_viewer):
    response = client_viewer.get("/daily-review/")
    assert response.status_code == 302 and response["Location"] == "/#daily-review"


@pytest.mark.parametrize("url", ["/country-overview/", "/daily-review/"])
def test_visitors_are_asked_to_sign_in(client, url):
    response = client.get(url)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
