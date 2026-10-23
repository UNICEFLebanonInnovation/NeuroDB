"""The Validators group (accounts.roles.can_validate): who checks the eTools figures, read in the groups query the
role already makes, so the pages a viewer opens make no query more."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, ALL_ROLES, VALIDATORS, VIEWER, can_validate, ensure_groups, role_of
from neurodb.core import compute
from tests.web.parts import raw_get

pytestmark = pytest.mark.django_db


def _user(username: str, *groups: str, **fields) -> User:
    user = User.objects.create_user(username=username, password="pass-123456-x", **fields)
    user.groups.set(Group.objects.filter(name__in=groups))
    return user


def test_who_validates(roles):
    assert can_validate(User.objects.create_superuser(username="root", password="root-pass-123456"))
    assert can_validate(_user("checker", VIEWER, VALIDATORS))
    assert not can_validate(_user("administrator", ADMIN))  # an Administrator only when ticked
    assert can_validate(_user("ticked-administrator", ADMIN, VALIDATORS))
    assert not can_validate(_user("viewer", VIEWER))
    assert not can_validate(_user("gone", VIEWER, VALIDATORS, is_active=False))
    assert not can_validate(AnonymousUser())


def test_the_group_is_made_at_start_and_setting_a_role_keeps_it(roles):
    assert Group.objects.filter(name=VALIDATORS).exists()
    assert VALIDATORS not in ensure_groups()  # setting a role removes the role groups only
    assert set(ensure_groups()) == set(ALL_ROLES)


def test_the_role_and_the_group_are_one_query_per_request(roles):
    user = _user("checker", VIEWER, VALIDATORS)
    compute.request_started()
    try:
        with CaptureQueriesContext(connection) as found:
            assert role_of(user) == VIEWER and can_validate(user) and role_of(user) == VIEWER
        assert len(found) == 1
    finally:
        compute.request_finished()


@pytest.mark.shell
@pytest.mark.parametrize(
    "page",
    ["/partner-reporting/", "/partner-monitoring/", "/partner-monitoring/map/", "/field-monitoring/", "/"],
)
def test_a_validator_s_page_makes_no_query_more_than_a_viewer_s(client, roles, page):
    """The line under the tabs reads the group in the role's own query: a viewer's frames keep their ceilings
    (tests/reports/test_home_parts.py, test_partner_reporting_parts.py, test_partner_parts.py)."""
    viewer = _user("viewer", VIEWER)
    client.force_login(viewer)
    raw_get(client, page)  # warm
    with CaptureQueriesContext(connection) as as_viewer:
        html = raw_get(client, page).content.decode()
    assert "Raw data behind this page" not in html
    viewer.groups.add(Group.objects.get(name=VALIDATORS))
    raw_get(client, page)
    with CaptureQueriesContext(connection) as as_validator:
        html = raw_get(client, page).content.decode()
    assert "Raw data: ask for staff status" in html  # a validator without staff status
    assert len(as_validator) == len(as_viewer), page


@pytest.mark.parametrize(
    ("page", "key"),
    [
        ("/partner-reporting/", "reports:partner_reporting"),
        ("/partner-monitoring/", "reports:pd_monitoring"),
        ("/partner-monitoring/map/", "reports:pd_monitoring"),
        ("/field-monitoring/", "reports:monitoring"),
        ("/field-monitoring/?tab=plan", "reports:monitoring"),
        ("/", "reports:overview"),
    ],
)
def test_the_line_opens_the_raw_data_behind_the_page(client, roles, page, key):
    from urllib.parse import quote

    from django.urls import reverse

    checker = _user("checker", VIEWER, VALIDATORS, is_staff=True)
    client.force_login(checker)
    html = client.get(page).content.decode()
    assert f'href="{reverse("admin:datamart_raw")}?page={quote(key)}"' in html
    assert html.count("Raw data behind this page") == 1
