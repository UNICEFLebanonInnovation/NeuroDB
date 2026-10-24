"""Who reaches the Power BI comparison sheet and the figure trail, and where their links are (SPEC_validation §3.2,
§5.1): the check line offers the sheet under Progress reports and Partner monitoring (and its map), and the trail
under Progress reports (every tab), Partner monitoring (every view but the master indicators, and its map) and
Home's two partner reporting blocks, to validators only, for the filter drawn; the addresses answer validators
(staff status or not) and a 404 to anyone else; a donor never reaches them."""

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


# ------------------------------------------------------------------------------------------- the figure trail
TRAIL = "Check these figures"
TRAIL_LINK = re.compile(r'<a href="([^"]+)" data-check-trail>Check these figures</a>')


@pytest.mark.parametrize(
    ("page", "params", "trail"),
    [
        ("/partner-reporting/", {}, "/check/progress-reports/"),
        ("/partner-reporting/", {"tab": "partners", "section": "Education"}, "/check/progress-reports/"),
        ("/partner-reporting/", {"tab": "locations"}, "/check/progress-reports/"),
        ("/partner-reporting/", {"tab": "disaggregations"}, "/check/progress-reports/"),
        ("/", {"section": ""}, "/check/home/"),  # under Home's Progress reports and Indicator progress blocks
        ("/partner-monitoring/", {}, "/check/partner-monitoring/"),
        ("/partner-monitoring/", {"view": "table", "section": "Education"}, "/check/partner-monitoring/"),
        ("/partner-monitoring/", {"view": "quality"}, "/check/partner-monitoring/"),
        ("/partner-monitoring/map/", {}, "/check/partner-monitoring/"),
        ("/partner-monitoring/", {"view": "masters"}, None),  # the master indicators have no trail
        ("/field-monitoring/", {}, None),
    ],
)
def test_the_trail_s_link_is_for_validators_only(client, roles, more, frozen, page, params, trail):
    client.force_login(_user("viewer", VIEWER))
    assert TRAIL not in client.get(page, params).content.decode()
    client.force_login(_user("checker", VIEWER, VALIDATORS))
    text = client.get(page, params).content.decode()
    found = TRAIL_LINK.findall(text)
    if trail is None:
        assert not found
        return
    if page == "/":
        assert sorted(re.search(r"block=([a-z]+)", link).group(1) for link in found) == [
            "indicators",
            "reporting",
        ]
        assert text.count("Raw data behind this page") <= 1
        for link in found:
            link = html.unescape(link)
            assert link.startswith(trail) and client.get(link).status_code == 200
        return
    (link,) = found
    link = html.unescape(link)
    assert link.startswith(trail)
    if page.startswith("/partner-monitoring/"):
        view = "map" if page.endswith("map/") else params.get("view", "grid")
        assert f"view={view}" in link
        assert "year=" in link and "page=" not in link and "as_of=" not in link
        if "section" in params:
            assert "section=Education" in link
    else:
        assert f"tab={params.get('tab', 'reports')}" in link and "page=" not in link
        if "section" in params:
            assert "section=Education" in link
    assert client.get(link).status_code == 200


@pytest.mark.parametrize(
    ("page", "params"),
    [
        ("progress-reports", {}),
        ("progress-reports", {"what": "kept"}),
        ("home", {"block": "reporting"}),
        ("partner-monitoring", {"year": "2026"}),
        ("partner-monitoring", {"year": "2026", "what": "kept"}),
        ("home", {"block": "indicators"}),
    ],
)
def test_the_trail_answers_validators_only(client, roles, more, frozen, page, params):
    url = reverse("reports:check_rows" if "what" in params else "reports:check_figures", args=[page])
    assert client.get(url, params).status_code == 302  # signed out: the sign-in page
    for name, groups, fields in (
        ("viewer", (VIEWER,), {}),
        ("management", (VIEWER, MANAGEMENT), {}),
        ("administrator", (ADMIN,), {"is_staff": True}),
    ):
        client.force_login(_user(name, *groups, **fields))
        assert client.get(url, params).status_code == 404, name
    for name, groups, fields in (
        ("checker", (VIEWER, VALIDATORS), {}),  # no staff status needed
        ("ticked-administrator", (ADMIN, VALIDATORS), {"is_staff": True}),
    ):
        client.force_login(_user(name, *groups, **fields))
        assert client.get(url, params).status_code == 200, name
    client.force_login(User.objects.create_superuser(username="root", password="root-pass-123456"))
    assert client.get(url, params).status_code == 200


def test_a_donor_never_reaches_the_trail(client, more):
    from tests.donors.test_donor_access import make_donor

    client.force_login(make_donor().user)
    for url in (
        reverse("reports:check_figures", args=["progress-reports"]),
        reverse("reports:check_rows", args=["progress-reports"]) + "?what=kept",
        reverse("reports:check_figures", args=["partner-monitoring"]),
        reverse("reports:check_rows", args=["partner-monitoring"]) + "?what=kept",
    ):
        response = client.get(url)
        assert response.status_code == 302 and response["Location"] == reverse("donors:page")


def test_the_trail_s_csv_refuses_what_it_does_not_know_and_waits_for_a_slot(
    client, roles, more, frozen, monkeypatch
):
    from neurodb.datamart import raw

    client.force_login(_user("checker", VIEWER, VALIDATORS))
    url = reverse("reports:check_rows", args=["progress-reports"])
    assert client.get(url, {"what": "nothing"}).status_code == 404
    assert client.get(url, {"what": "figure:pr.unknown"}).status_code == 404
    assert client.get(url, {"what": "step:q"}).status_code == 404  # no search: no such step
    assert client.get(url, {"what": "kept", "day": "2026-01-01"}).status_code == 400  # over 14 days back
    monkeypatch.setattr(raw, "take_slot", lambda: None)
    response = client.get(url, {"what": "kept"})
    assert response.status_code == 503 and raw.BUSY in response.content.decode()
    monkeypatch.setattr(raw, "CSV_LIMIT", {"long_texts": 2, "other": 2})
    response = client.get(url, {"what": "kept"})
    assert response.status_code == 400 and "more than 2" in response.content.decode()
