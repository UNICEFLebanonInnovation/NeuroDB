"""KM Database in the menu and who opens it (SPEC_km §6.1-6.2, the user's decision "full menu only, ticked per
role"): under Resources in the full menu, in no focused menu, so every role starts unticked on the Menu page;
the page opens for every signed-in UNICEF role, ticked or not; donors and signed-out visitors never get it."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER
from neurodb.indicators.services import navigation
from tests.donors.test_donor_access import make_donor
from tests.km import factory
from tests.web.menu_rows import groups, rows

pytestmark = pytest.mark.django_db

PAGES = ("km:index", "km:documents")
# every address of the KM Database: its tabs, a finding, a document and the downloads
ADDRESSES = (
    "/km/",
    "/km/documents/",
    f"/km/findings/{factory.finding_id(1, 1)}/",
    f"/km/documents/{factory.record_id(1)}/",
    "/km/?export=xlsx",
    "/km/?export=csv",
    "/km/documents/?export=xlsx",
)


def _person(username: str, role: str, *, is_staff=False, is_superuser=False) -> User:
    user = User.objects.create_user(
        username=username, password="pass-123456-x", is_staff=is_staff, is_superuser=is_superuser
    )
    user.groups.add(Group.objects.get(name=role))
    return user


def _sidebar(client, user) -> list[dict]:
    client.force_login(user)
    return rows(client.get(reverse("reports:library")).content.decode())


def test_it_is_under_resources_after_the_library_in_the_full_menu(client, roles):
    resources = next(g for g in navigation.MENU if g.key == "resources")
    labels = [str(item.label) for item in resources.items]
    assert labels.index("KM Database") == labels.index("Library") + 1
    item = resources.items[labels.index("KM Database")]
    assert (item.route, item.icon, item.when, item.badge) == ("km:index", "evidence", "", "")
    assert "km:index" not in navigation.FOCUSED
    assert all(part != "km:index" for part in navigation.FOCUS)
    superuser = _person("km-menu-root", VIEWER, is_staff=True, is_superuser=True)
    client.force_login(superuser)
    html = client.get(reverse("reports:library")).content.decode()
    drawn = [(r["label"], r["icon"]) for r in rows(html)]
    assert drawn.index(("KM Database", "evidence")) == drawn.index(("Library", "book")) + 1
    assert ("resources", "Resources") in groups(html)


@pytest.mark.parametrize("role", [VIEWER, SECTION_EDITOR, ADMIN])
def test_no_role_sees_it_until_a_superuser_ticks_it(client, roles, menu_choice, role):
    user = _person(f"km-menu-{role[:4].lower()}", role, is_staff=role == ADMIN)
    assert "KM Database" not in [r["label"] for r in _sidebar(client, user)]
    menu_choice(role, [*navigation.FOCUSED, "km:index"])
    drawn = _sidebar(client, user)
    assert [r["label"] for r in drawn][-1] == "KM Database"
    html = client.get(reverse("km:index")).content.decode()
    assert ("resources", "Resources") in groups(html)
    current = next(r for r in rows(html) if r["label"] == "KM Database")
    assert 'aria-current="page"' in current["attrs"]
    current = next(
        r for r in rows(client.get(reverse("km:documents")).content.decode()) if r["label"] == "KM Database"
    )
    assert 'aria-current="page"' in current["attrs"]  # the Documents tab is its page too


def test_the_menu_page_lists_it_unticked_for_every_role(client, roles, menu_choice):
    client.force_login(_person("km-menu-page-root", VIEWER, is_staff=True, is_superuser=True))
    html = client.get(reverse("admin:web_menuchoice_changelist")).content.decode()
    ticks = [line.split(">", 1)[0] for line in html.split("<input")[1:] if 'value="km:index"' in line]
    assert len(ticks) == 3 and not any(" checked" in tick for tick in ticks)
    entry = next(e for e in navigation.entries() if e["key"] == "km:index")
    assert str(entry["group"]) == "Resources" and entry["note"] == ""


@pytest.mark.parametrize("role", [VIEWER, SECTION_EDITOR, ADMIN])
def test_every_signed_in_unicef_role_opens_it_ticked_or_not(client, roles, role):
    factory.load(factory.workbook())
    client.force_login(_person(f"km-open-{role[:4].lower()}", role))
    for address in ADDRESSES:
        assert client.get(address).status_code == 200, address


def test_donors_are_sent_to_their_page_and_htmx_is_refused(client, roles):
    factory.load(factory.workbook())
    donor = make_donor()
    client.force_login(donor.user)
    for address in ADDRESSES:
        response = client.get(address)
        assert response.status_code == 302 and response["Location"] == reverse("donors:page"), address
    for name in PAGES:
        response = client.get(reverse(name))
        assert response.status_code == 302 and response["Location"] == reverse("donors:page"), name
        refused = client.get(reverse(name), HTTP_HX_REQUEST="true")
        assert refused.status_code == 403 and refused.json() == {
            "detail": "Not available for donor accounts."
        }


def test_signed_out_visitors_are_sent_to_sign_in(client):
    factory.load(factory.workbook())
    for address in ADDRESSES:
        response = client.get(address)
        assert response.status_code == 302 and reverse("account_login") in response["Location"], address
