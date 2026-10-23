"""Who reaches the Power BI comparison sheet, and where its link is (SPEC_validation §3.2, §5.1): the check line
offers it under Progress reports and Partner monitoring (and its map) to validators only, for the filter drawn;
the address answers validators (staff status or not) and a 404 to anyone else; a donor never reaches it."""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the partner monitoring tests)
from __future__ import annotations

import html
import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, MANAGEMENT, VALIDATORS, VIEWER
from tests.reports.test_pd_monitoring import data
from tests.reports.test_powerbi_views import frozen, more

pytestmark = pytest.mark.django_db
SHEET = "Power BI comparison sheet (Excel)"
FORM = re.compile(r'<form method="get" action="([^"]+)"[^>]*data-check-compare>(.*?)</form>', re.S)
HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')


def _user(username: str, *groups: str, **fields) -> User:
    user = User.objects.create_user(username=username, password="pass-123456-x", **fields)
    user.groups.set(Group.objects.filter(name__in=groups))
    return user


def _form(page: str) -> tuple[str, list[tuple[str, str]]] | None:
    """The sheet's form of a page: its address and its hidden fields (the filter), or None."""
    found = FORM.findall(page)
    if not found:
        return None
    assert len(found) == 1
    action, body = found[0]
    return html.unescape(action), [(k, html.unescape(v)) for k, v in HIDDEN.findall(body)]


@pytest.mark.parametrize(
    ("page", "params", "sheet"),
    [
        ("/partner-reporting/", {}, "progress-reports"),
        ("/partner-reporting/", {"tab": "partners"}, "progress-reports"),
        ("/partner-reporting/", {"tab": "locations"}, "progress-reports"),
        ("/partner-reporting/", {"tab": "disaggregations"}, "progress-reports"),
        ("/partner-monitoring/", {}, "partner-monitoring"),
        ("/partner-monitoring/", {"view": "table"}, "partner-monitoring"),
        ("/partner-monitoring/", {"view": "quality"}, "partner-monitoring"),
        ("/partner-monitoring/map/", {}, "partner-monitoring"),
        ("/partner-monitoring/", {"view": "masters"}, None),  # no master indicators in Power BI
        ("/field-monitoring/", {}, None),
        ("/", {}, None),  # Home: no Power BI page
    ],
)
def test_the_sheet_s_link_is_for_validators_only(client, roles, more, frozen, page, params, sheet):
    client.force_login(_user("viewer", VIEWER))
    text = client.get(page, params).content.decode()
    assert SHEET not in text and _form(text) is None
    client.force_login(_user("checker", VIEWER, VALIDATORS))
    text = client.get(page, params).content.decode()
    found = _form(text)
    if sheet is None:
        assert found is None and SHEET not in text
        return
    assert found[0] == reverse("reports:check_compare", args=[sheet])
    assert text.count(SHEET) == 1
    assert 'type="date" name="day"' in text and 'min="2026-06-17" max="2026-07-01"' in text


def test_the_link_carries_the_filter_drawn(client, roles, more, frozen):
    client.force_login(_user("checker", VIEWER, VALIDATORS))
    text = client.get(
        "/partner-reporting/", {"tab": "partners", "page": "2", "section": "Education", "pd_status": ""}
    ).content.decode()
    assert sorted(_form(text)[1]) == [("pd_status", ""), ("section", "Education")]  # never the tab or page
    # Partner monitoring: the resolved filter (the year written out) and the view
    text = client.get("/partner-monitoring/", {"view": "table", "sort": "indicator"}).content.decode()
    assert sorted(_form(text)[1]) == [("section", ""), ("view", "table"), ("year", "all")]
    # a change of filter draws the results, and the line with them, again with the new filter
    answer = client.get(
        "/partner-monitoring/",
        {"view": "table", "section": "Education", "year": "2026"},
        headers={"HX-Request": "true", "HX-Target": "monitoring-grid"},
    ).content.decode()
    assert sorted(_form(answer)[1]) == [("section", "Education"), ("view", "table"), ("year", "2026")]
    text = client.get("/partner-monitoring/map/", {"section": "Education"}).content.decode()
    assert sorted(_form(text)[1]) == [("section", "Education"), ("year", "2026")]


@pytest.mark.parametrize("page", ["progress-reports", "partner-monitoring"])
def test_the_address_answers_validators_only(client, roles, more, frozen, page):
    url = reverse("reports:check_compare", args=[page])
    assert client.get(url).status_code == 302  # signed out: the sign-in page
    for name, groups, fields in (
        ("viewer", (VIEWER,), {}),
        ("management", (VIEWER, MANAGEMENT), {}),
        ("administrator", (ADMIN,), {"is_staff": True}),
    ):
        client.force_login(_user(name, *groups, **fields))
        assert client.get(url).status_code == 404, name
    for name, groups, fields in (
        ("checker", (VIEWER, VALIDATORS), {}),  # no staff status needed
        ("staff-checker", (VIEWER, VALIDATORS), {"is_staff": True}),
        ("ticked-administrator", (ADMIN, VALIDATORS), {"is_staff": True}),
    ):
        client.force_login(_user(name, *groups, **fields))
        assert client.get(url).status_code == 200, name
    client.force_login(User.objects.create_superuser(username="root", password="root-pass-123456"))
    assert client.get(url).status_code == 200


def test_a_donor_never_reaches_the_sheet(client, more):
    from tests.donors.test_donor_access import make_donor

    client.force_login(make_donor().user)
    response = client.get(reverse("reports:check_compare", args=["progress-reports"]))
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
