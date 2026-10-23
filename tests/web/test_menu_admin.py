"""The administration's Menu page (SPEC_focus_menu §1.5, §4.2): superusers only tick, per role, the entries its
sidebar shows; a save that ticks exactly the focused menu keeps no row; Reset removes every row; each change is
in the audit trail. Administrators hold every permission, so the page checks for a superuser itself, only a
superuser can make one, and only a superuser changes a superuser's account."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, ALL_ROLES, SECTION_EDITOR, VIEWER
from neurodb.indicators.services import navigation
from neurodb.web import admin_site, menu_choices
from neurodb.web.models import MenuChoice
from tests.web.menu_rows import rows

pytestmark = pytest.mark.django_db

PAGE = "admin:web_menuchoice_changelist"
FIELDS = {ADMIN: "role-administrator", SECTION_EDITOR: "role-section-editor", VIEWER: "role-viewer"}
CSS = Path(django_settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "admin.css"


def _person(username: str, role: str, *, is_staff=True, is_superuser=False) -> User:
    user = User.objects.create_user(
        username=username, password="pass-123456-x", is_staff=is_staff, is_superuser=is_superuser
    )
    user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def superuser(roles):
    return _person("menu-root", VIEWER, is_superuser=True)


@pytest.fixture
def client_super(client, superuser, menu_choice):
    client.force_login(superuser)
    return client


def _ticks(**chosen) -> dict:
    """A save's form: each role's ticks (the focused menu for a role not named)."""
    data = {"save": "1"}
    for field in FIELDS.values():
        data[field] = list(chosen.get(field.removeprefix("role-").replace("-", "_"), navigation.FOCUSED))
    return data


# ---------------------------------------------------------------------------------------- who opens it
def test_a_superuser_gets_the_grid_every_tick_labelled(client_super):
    response = client_super.get(reverse(PAGE))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<caption>What each role sees in the menu</caption>" in html
    assert re.findall(r'<th scope="col">([^<]+)</th>', html) == [
        "Entry",
        "Administrator",
        "Section editor",
        "Viewer",
    ]
    inputs = re.findall(
        r'<input type="checkbox" name="(role-[\w-]+)" value="([^"]+)" id="([\w-]+)"( checked)?>', html
    )
    assert len(inputs) == 3 * len(navigation.keys())
    assert {value for _name, value, _id, _ticked in inputs} == set(navigation.keys())
    for _name, _value, field_id, _ticked in inputs:  # every tick has its own label
        assert re.search(rf'<label class="nd-sr" for="{field_id}">Show [^<]+ to [^<]+</label>', html), (
            field_id
        )
    assert '<label class="nd-sr" for="m-viewer-' in html and "Show Funds to Viewers" in html
    # no row saved: every role's ticks are the focused menu
    for _name, value, _id, ticked in inputs:
        assert bool(ticked) == (value in navigation.FOCUSED), value
    # each group's title heads the rows of its own tbody: the grid has no column group to head
    assert re.findall(r'<th scope="rowgroup" colspan="4">([^<]+)</th>', html)[:4] == [
        "Top of the menu",
        "Partner reporting (PRP)",
        "Below Partner reporting",
        "Programme results",
    ]
    grid = html.split('<table class="nd-jobs nd-menu__grid">', 1)[1].split("</table>", 1)[0]
    assert 'scope="colgroup"' not in grid and "<colgroup" not in grid
    assert "<script" not in html.split('id="content-main"', 1)[1].split("</form>", 1)[0]  # no script


def test_the_grid_lists_the_entries_in_the_order_a_role_sees_them(client_super):
    html = client_super.get(reverse(PAGE)).content.decode()
    values = re.findall(r'name="role-viewer" value="([^"]+)"', html)
    assert values == [entry["key"] for entry in navigation.entries()]
    assert values[:5] == [
        "reports:overview",
        "reports:partner_reporting",
        "reports:pd_monitoring",
        "reports:monitoring",
        "assistant:ask",
    ]
    assert values[-2:] == [navigation.ARCHIVE_KEY, navigation.HELP_KEY]
    assert sorted(values) == sorted(navigation.keys())
    # the archive's note says it holds the only menu links to the Neuro and HPM reports
    assert "Holds the only menu links to the Neuro reports and HPM reports" in html


def test_the_hidden_label_rule_is_in_admin_css():
    css = CSS.read_text()
    rule = css.split(".nd-sr {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in rule and "clip: rect(0, 0, 0, 0)" in rule and "width: 1px" in rule
    # the grid's group titles are styled by the scope they carry, in both themes
    assert '.nd-menu__grid th[scope="rowgroup"] {' in css
    assert '.dark .nd-menu__grid th[scope="rowgroup"]' in css
    assert '.nd-menu__grid th[scope="colgroup"]' not in css


@pytest.mark.parametrize("who", ["staff_admin", "staff_viewer"])
def test_no_one_but_a_superuser_opens_it(client, roles, who):
    """An Administrator holds every permission (ensure_groups) and still gets a 403, on GET as on POST; the
    page is not in their admin sidebar or dashboard."""
    user = _person(who, ADMIN if who == "staff_admin" else VIEWER)
    client.force_login(user)
    assert client.get(reverse(PAGE)).status_code == 403
    assert client.post(reverse(PAGE), _ticks()).status_code == 403
    assert client.post(reverse(PAGE), {"reset": "1"}).status_code == 403
    html = client.get(reverse("admin:index")).content.decode()
    assert reverse(PAGE) not in html and "Choose what each role sees in the menu" not in html
    if who == "staff_admin":
        assert user.has_perm("web.change_menuchoice")  # the group's permission, refused all the same
        request = client.get(reverse("admin:index")).wsgi_request
        links = [item["link"] for group in admin_site.sidebar_navigation(request) for item in group["items"]]
        assert reverse(PAGE) not in links


def test_signed_out_it_leads_to_sign_in(client, db):
    response = client.get(reverse(PAGE))
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_login"))


def test_add_and_delete_are_refused_to_everyone(client_super, menu_choice):
    row = menu_choice(VIEWER, ["reports:overview"])
    assert client_super.get(reverse("admin:web_menuchoice_add")).status_code == 403
    assert client_super.get(reverse("admin:web_menuchoice_delete", args=[row.pk])).status_code == 403
    assert (
        client_super.post(reverse("admin:web_menuchoice_delete", args=[row.pk]), {"post": "yes"}).status_code
        == 403
    )
    assert MenuChoice.objects.filter(pk=row.pk).exists()


def test_a_rows_own_form_is_read_only(client_super, menu_choice):
    row = menu_choice(VIEWER, ["reports:overview", "reports:funds"])
    url = reverse("admin:web_menuchoice_change", args=[row.pk])
    html = client_super.get(url).content.decode()
    assert 'name="shown"' not in html and 'name="role"' not in html
    assert "Home, Funds" in html  # the entries by their labels
    response = client_super.post(url, {"role": ADMIN, "shown": '["reports:overview"]'})
    assert response.status_code == 403
    row.refresh_from_db()
    assert row.role == VIEWER and row.shown == ["reports:overview", "reports:funds"]
    assert client_super.get(reverse("admin:web_menuchoice_history", args=[row.pk])).status_code == 200


# ------------------------------------------------------------------------------------------------ save
def test_ticking_the_focused_menu_keeps_no_row(client_super, superuser, menu_choice):
    menu_choice(VIEWER, ["reports:overview", "reports:funds"])
    response = client_super.post(reverse(PAGE), _ticks(), follow=True)
    assert response.redirect_chain == [(reverse(PAGE), 302)]
    assert not MenuChoice.objects.exists()
    assert LogEntry.objects.filter(action_flag=DELETION, object_repr="Viewer").count() == 1
    assert "Saved. Entries ticked: Administrator 5, Section editor 5, Viewer 5." in response.content.decode()


def test_other_ticks_save_a_row_per_role_with_its_history(client_super, superuser, roles):
    viewers = [*navigation.FOCUSED, "reports:funds", navigation.ARCHIVE_KEY]
    editors = [*navigation.FOCUSED, navigation.ARCHIVE_KEY]
    response = client_super.post(reverse(PAGE), _ticks(viewer=viewers, section_editor=editors), follow=True)
    assert "Entries ticked: Administrator 5, Section editor 6, Viewer 7." in response.content.decode()
    saved = {row.role: row for row in MenuChoice.objects.all()}
    assert set(saved) == {VIEWER, SECTION_EDITOR}  # the Administrators' ticks are the focused menu
    keys = navigation.keys()
    assert saved[VIEWER].shown == [key for key in keys if key in set(viewers)]  # in the menu's order
    assert saved[VIEWER].updated_by == superuser
    added = LogEntry.objects.get(action_flag=ADDITION, object_repr="Viewer")
    assert (
        added.get_change_message()
        == "Entries added: Funds, ActivityInfo (the monthly results or the archive)."
    )
    # a second save changes one role: one more entry in the audit trail, for that role only
    client_super.post(reverse(PAGE), _ticks(viewer=[*viewers, navigation.HELP_KEY], section_editor=editors))
    changed = LogEntry.objects.filter(action_flag=CHANGE)
    assert [entry.object_repr for entry in changed] == ["Viewer"]
    assert changed[0].get_change_message() == "Entries added: Help."
    # the page shows each saved role's history
    html = client_super.get(reverse(PAGE)).content.decode()
    assert reverse("admin:web_menuchoice_history", args=[saved[VIEWER].pk]) in html
    assert reverse("admin:web_menuchoice_history", args=[saved[SECTION_EDITOR].pk]) in html


def test_the_sidebar_follows_on_the_next_page(client, roles, superuser, menu_choice):
    viewer = _person("follows", VIEWER, is_staff=False)
    client.force_login(superuser)
    client.post(reverse(PAGE), _ticks(viewer=[*navigation.FOCUSED, "reports:funds"]))
    client.force_login(viewer)
    assert "Funds" in [row["label"] for row in rows(client.get(reverse("reports:library")).content.decode())]
    client.force_login(superuser)
    client.post(reverse(PAGE), {"reset": "1"})
    client.force_login(viewer)
    assert "Funds" not in [
        row["label"] for row in rows(client.get(reverse("reports:library")).content.decode())
    ]


def test_a_forged_entry_is_refused_and_nothing_is_saved(client_super):
    response = client_super.post(reverse(PAGE), _ticks(viewer=[*navigation.FOCUSED, "admin:index"]))
    assert response.status_code == 200
    assert "Select a valid choice" in response.content.decode()
    assert not MenuChoice.objects.exists() and not LogEntry.objects.exists()


def test_a_role_with_nothing_ticked_is_refused(client_super):
    data = _ticks()
    data.pop(FIELDS[VIEWER])
    response = client_super.post(reverse(PAGE), data)
    html = response.content.decode()
    assert response.status_code == 200 and "Tick at least one entry for Viewers." in html
    assert not MenuChoice.objects.exists()
    # the ticks are drawn as they were sent, the Viewers' column empty
    assert not re.findall(r'name="role-viewer" value="[^"]+" id="[\w-]+" checked', html)


def test_a_save_without_its_csrf_token_is_refused(superuser, menu_choice):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(superuser)
    assert strict.post(reverse(PAGE), _ticks(viewer=["reports:funds"])).status_code == 403
    assert not MenuChoice.objects.exists()


def test_reset_gives_every_role_the_focused_menu_again(client_super, menu_choice):
    for role in ALL_ROLES:
        menu_choice(role, ["reports:overview", "reports:funds"])
    response = client_super.post(reverse(PAGE), {"reset": "1"}, follow=True)
    assert "Every role sees the focused menu again." in response.content.decode()
    assert not MenuChoice.objects.exists()
    assert LogEntry.objects.filter(action_flag=DELETION).count() == 3
    assert all(menu_choices.for_role(role) == navigation.FOCUSED for role in ALL_ROLES)


# ----------------------------------------------------------------------------------- superuser status
def _user_form(target: User, **changes) -> dict:
    data = {
        "username": target.username,
        "first_name": "",
        "last_name": "",
        "email": target.email,
        "section": "",
        "backup_user": "",
        "skype_account": "",
        "is_active": "on",
        "is_staff": "on",
        "groups": [group.pk for group in target.groups.all()],
        "_save": "Save",
    }
    data.update(changes)
    return data


def test_only_a_superuser_makes_a_superuser(client, roles, superuser):
    """An Administrator holds the permission to change users; the superuser status (and the permissions one by
    one) are read only to them, on their own account as on another, so they cannot give themselves the Menu
    page. A superuser still sets it."""
    admin = _person("plain-admin", ADMIN)
    other = _person("other-admin", ADMIN)
    client.force_login(admin)
    html = client.get(reverse("admin:users_user_change", args=[admin.pk])).content.decode()
    assert 'name="is_superuser"' not in html and 'name="user_permissions"' not in html
    for target in (admin, other):
        response = client.post(
            reverse("admin:users_user_change", args=[target.pk]), _user_form(target, is_superuser="on")
        )
        assert response.status_code == 302, response.content.decode()[:500]
        target.refresh_from_db()
        assert not target.is_superuser
    client.force_login(superuser)
    response = client.post(
        reverse("admin:users_user_change", args=[other.pk]), _user_form(other, is_superuser="on")
    )
    assert response.status_code == 302
    other.refresh_from_db()
    assert other.is_superuser


def test_only_a_superuser_changes_a_superuser_s_account(client, roles, superuser):
    """An Administrator who could set a superuser's password, e-mail or Active status could sign in as them
    and open the Menu page, or leave nobody able to open it: a superuser's account is read only to anyone
    else, in its form, its password form, its delete page and the list's actions. A superuser still changes
    another superuser."""
    User.objects.filter(pk=superuser.pk).update(email="menu-root@example.org")
    superuser.refresh_from_db()
    admin = _person("plain-admin", ADMIN)
    viewer = _person("plain-viewer", VIEWER)
    client.force_login(admin)
    password = reverse("admin:auth_user_password_change", args=[superuser.pk])
    assert client.get(password).status_code == 403
    new = {"password1": "taken-over-654321", "password2": "taken-over-654321"}
    assert client.post(password, new).status_code == 403
    change = reverse("admin:users_user_change", args=[superuser.pk])
    form = client.get(change)
    assert form.status_code == 200  # shown, read only
    assert 'name="email"' not in form.content.decode() and 'name="is_active"' not in form.content.decode()
    data = _user_form(superuser, email="taken-over@example.org")
    data.pop("is_active")
    assert client.post(change, data).status_code == 403
    assert client.get(reverse("admin:users_user_delete", args=[superuser.pk])).status_code == 403
    changelist = reverse("admin:users_user_changelist")
    for action in ("deactivate", "make_administrator"):
        response = client.post(
            changelist, {"action": action, "_selected_action": [superuser.pk, viewer.pk]}, follow=True
        )
        assert "1 superuser account(s) skipped" in response.content.decode(), action
    response = client.post(
        changelist, {"action": "delete_selected", "_selected_action": [superuser.pk], "post": "yes"}
    )
    assert response.status_code == 403
    superuser.refresh_from_db()
    assert superuser.check_password("pass-123456-x") and superuser.email == "menu-root@example.org"
    assert superuser.is_active and superuser.is_staff and superuser.is_superuser
    assert list(superuser.groups.values_list("name", flat=True)) == [VIEWER]
    viewer.refresh_from_db()
    assert not viewer.is_active and viewer.groups.filter(name=ADMIN).exists()  # the others are changed
    # the Administrator still changes everyone else, themselves included
    assert client.get(reverse("admin:auth_user_password_change", args=[viewer.pk])).status_code == 200
    assert client.get(reverse("admin:auth_user_password_change", args=[admin.pk])).status_code == 200
    # a superuser changes another superuser
    other = _person("other-root", VIEWER, is_superuser=True)
    client.force_login(superuser)
    other_password = reverse("admin:auth_user_password_change", args=[other.pk])
    assert client.post(other_password, new).status_code == 302
    other.refresh_from_db()
    assert other.check_password("taken-over-654321")
    response = client.post(changelist, {"action": "deactivate", "_selected_action": [other.pk]}, follow=True)
    assert "skipped" not in response.content.decode()
    other.refresh_from_db()
    assert not other.is_active


# -------------------------------------------------------------------------------------------- placement
def test_it_sits_in_users_and_access_with_its_icon_and_a_quick_action(client_super):
    users_and_access = next(
        keys for name, _description, keys in admin_site.GROUPS if str(name) == "Users and access"
    )
    assert users_and_access.index("web.MenuChoice") == users_and_access.index("auth.Group") + 1
    assert admin_site.ICONS["web.MenuChoice"] == "menu"
    html = client_super.get(reverse("admin:index")).content.decode()
    assert f'href="{reverse(PAGE)}"' in html and "Choose what each role sees in the menu" in html
    assert "the menu each role sees" in html  # the group's description


def test_its_migration_is_listed_as_quick():
    from tests.datamart.test_backfill import QUICK_MIGRATIONS

    assert ("web", "0001_menu_choice", "CreateModel") in QUICK_MIGRATIONS
