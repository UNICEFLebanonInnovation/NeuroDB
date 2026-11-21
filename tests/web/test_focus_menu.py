"""The focused menu (SPEC_focus_menu §1, §4.1): every role sees Home and Ask NeuroDB, Partner reporting (PRP) with
Progress reports and Partner monitoring, then Field monitoring under Knowledge base until a superuser ticks
otherwise in the Menu page; superusers see the full menu; donors and visitors are unchanged. Only the menu is
shorter: every page still opens from links and bookmarks, and the search still lists them. The choices are kept
in memory (no query on a warm page), and a save is seen by every worker."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, MANAGEMENT, SECTION_EDITOR, VIEWER
from neurodb.indicators.services import navigation
from neurodb.web import context_processors, menu_choices
from neurodb.web.models import MenuChoice
from tests.web.menu_rows import groups, rows, sidebar
from tests.web.parts import raw_get

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}
FOCUSED_ROWS = ["Home", "Ask NeuroDB", "Progress reports", "Partner monitoring", "Field monitoring"]
WITHOUT_ASK = [label for label in FOCUSED_ROWS if label != "Ask NeuroDB"]
KNOWLEDGE_BASE = ("knowledge_base", "Knowledge base")


def _person(username: str, role: str, *, is_staff=False, is_superuser=False, section=None) -> User:
    user = User.objects.create_user(
        username=username,
        password="pass-123456-x",
        is_staff=is_staff,
        is_superuser=is_superuser,
        section=section,
    )
    user.groups.add(Group.objects.get(name=role))
    return user


def _page(client, user, url: str) -> str:
    client.force_login(user)
    return client.get(url).content.decode()


def _labels(html: str) -> list[str]:
    return [row["label"] for row in rows(html)]


@pytest.fixture
def ai_off(settings):
    settings.AI_ASSISTANT_ENABLED = False


# ------------------------------------------------------------------------------------------- by default
@pytest.mark.parametrize(
    ("role", "is_staff"),
    [(ADMIN, True), (ADMIN, False), (SECTION_EDITOR, False), (VIEWER, False), (VIEWER, True)],
)
def test_every_role_sees_the_focused_menu_by_default(client, roles, hierarchy, ai_off, role, is_staff):
    user = _person("focused", role, is_staff=is_staff, section=hierarchy["database"].section)
    html = _page(client, user, reverse("reports:library"))
    # Ask shows to staff even while the AI is off (its page says so), and to no one else then
    expected = FOCUSED_ROWS if is_staff else WITHOUT_ASK
    assert _labels(html) == expected
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]
    side = sidebar(html)
    # Home and Ask NeuroDB come first, with no title; each group holds its rows, labelled by its title
    top = side[: side.index('aria-labelledby="nav-title-prp"')]
    assert ">Home<" in top and (">Ask NeuroDB<" in top) == is_staff and "sidebar__section-title" not in top
    start = side.index('aria-labelledby="nav-title-prp"')
    prp = side[start : side.index('aria-labelledby="nav-title-knowledge_base"')]
    assert (
        prp.index(">Progress reports<") < prp.index(">Partner monitoring<")
        and ">Field monitoring<" not in prp
    )
    knowledge_base = side[side.index('aria-labelledby="nav-title-knowledge_base"') :]
    assert ">Field monitoring<" in knowledge_base.split("</div>", 2)[1]  # in the group, after its title
    # no ActivityInfo block (ActivityInfo has a year here), no Help row, no For you count
    assert "sidebar__archive" not in side and "nav-item-link--help" not in side
    assert reverse("watch:badge") not in html
    # the rows link to their pages
    links = {row["label"]: row["href"] for row in rows(html)}
    assert links["Progress reports"] == reverse("reports:partner_reporting")
    assert links["Partner monitoring"] == reverse("reports:pd_monitoring")
    assert links["Field monitoring"] == reverse("reports:monitoring")


def test_a_row_after_the_group_is_set_apart_in_app_css():
    """A row with no title of its own after a group (For you after Partner reporting, when ticked for the
    role): a line above it says it is not one of the group's rows."""
    from pathlib import Path

    from django.conf import settings as django_settings

    css = (Path(django_settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
    rule = css.split(".nav-group + .nav-item-link:not(.nav-item-link--help)::before {", 1)[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--nd-border)" in rule


def test_a_management_member_sees_the_focused_menu(client, roles, ai_off):
    member = _person("manager", VIEWER)
    member.groups.add(Group.objects.get_or_create(name=MANAGEMENT)[0])
    html = _page(client, member, reverse("reports:library"))
    assert _labels(html) == WITHOUT_ASK


def test_a_superuser_sees_the_full_menu_the_archive_and_help(client, roles, hierarchy, settings):
    settings.AI_ASSISTANT_ENABLED = True
    superuser = _person("root-like", VIEWER, is_staff=True, is_superuser=True)
    html = _page(client, superuser, reverse("reports:library"))
    shown = [str(e["label"]) for g in navigation.menu(superuser) for e in g["items"]]
    assert _labels(html) == [*shown, "Help"]
    assert "For you" in shown and "Funds" in shown  # the full tree, not the focused one
    assert ("prp", "Partner reporting (PRP)") not in groups(html)
    assert "sidebar__archive" in sidebar(html)


def test_the_current_page_is_marked_in_the_focused_menu(client, roles, ai_off):
    viewer = _person("current", VIEWER)
    html = _page(client, viewer, reverse("help:index"))
    assert 'aria-current="page"' not in sidebar(html)  # Help is not in the focused menu
    html = _page(client, viewer, reverse("reports:partner_reporting"))
    current = [r["label"] for r in rows(html) if 'aria-current="page"' in r["attrs"]]
    assert current == ["Progress reports"] and sidebar(html).count('aria-current="page"') == 1


def test_a_donor_and_a_visitor_are_unchanged(client, db, settings):
    from tests.donors.test_donor_access import make_donor

    donor = make_donor(username="menu-donor")
    client.force_login(donor.user)
    assert client.get(reverse("reports:overview"))["Location"] == reverse("donors:page")
    request = RequestFactory().get("/")
    request.user = donor.user
    assert context_processors.navigation(request) == {"is_donor": True}  # no menu at all, as before
    client.logout()
    settings.PUBLIC_PAGES = []
    side = sidebar(client.get("/no-such-page/").content.decode())
    assert "Sign in for dashboards" in side and "Partner reporting" not in side


# ------------------------------------------------------------------------------------- the focused order
def test_home_then_ask_then_partner_reporting_then_knowledge_base(client, roles, settings):
    """A role with no saved choice: Home and Ask NeuroDB at the top with no title, then Partner reporting, then
    Field monitoring under Knowledge base, right after it."""
    settings.AI_ASSISTANT_ENABLED = True
    viewer = _person("new-order", VIEWER)
    drawn = navigation.sidebar(viewer)["groups"]
    assert [(g["title"] and str(g["title"]), [str(i["label"]) for i in g["items"]]) for g in drawn] == [
        (None, ["Home"]),
        (None, ["Ask NeuroDB"]),
        ("Partner reporting (PRP)", ["Progress reports", "Partner monitoring"]),
        ("Knowledge base", ["Field monitoring"]),
    ]
    html = _page(client, viewer, reverse("reports:library"))
    assert _labels(html) == FOCUSED_ROWS
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]


def test_field_monitoring_and_monitoring_insights_share_one_knowledge_base_title(
    client, roles, ai_off, settings, menu_choice
):
    settings.FMM_ENABLED = True
    menu_choice(VIEWER, [*navigation.FOCUSED, "fmm:dashboard"])
    viewer = _person("one-title", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    assert _labels(html) == [*WITHOUT_ASK, "Monitoring insights"]  # Field monitoring first, as in MENU
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]
    side = sidebar(html)
    assert side.count(">Knowledge base<") == 1
    # both rows are in that one group, after its title
    group = side[side.index('aria-labelledby="nav-title-knowledge_base"') :].split("</div>", 2)[1]
    assert group.index(">Field monitoring<") < group.index(">Monitoring insights<")


def test_field_monitoring_unticked_leaves_no_knowledge_base_title(client, roles, settings, menu_choice):
    settings.AI_ASSISTANT_ENABLED = True
    menu_choice(VIEWER, sorted(navigation.FOCUSED - {"reports:monitoring"}))
    viewer = _person("no-field-monitoring", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    assert _labels(html) == ["Home", "Ask NeuroDB", "Progress reports", "Partner monitoring"]
    assert groups(html) == [("prp", "Partner reporting (PRP)")]
    assert "Knowledge base" not in sidebar(html)


def test_a_choice_saved_in_the_old_order_draws_the_new_one(client, roles, settings, menu_choice):
    """The saved choices name the entries by their routes, not their places: a row saved while Field monitoring
    and Ask NeuroDB came after Partner reporting draws them in today's places."""
    settings.AI_ASSISTANT_ENABLED = True
    old = ["reports:overview", "reports:partner_reporting", "reports:pd_monitoring", "reports:monitoring"]
    menu_choice(VIEWER, [*old, "assistant:ask", "reports:funds"])
    viewer = _person("old-row", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    assert _labels(html) == [
        "Home",
        "Ask NeuroDB",
        "Progress reports",
        "Partner monitoring",
        "Funds",
        "Field monitoring",
    ]
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]


def test_a_superuser_sees_ask_neurodb_right_under_home(client, roles, settings):
    settings.AI_ASSISTANT_ENABLED = True
    superuser = _person("ask-second", VIEWER, is_staff=True, is_superuser=True)
    labels = _labels(_page(client, superuser, reverse("reports:library")))
    assert labels[:5] == ["Home", "Ask NeuroDB", "For you", "Daily review", "What's new"]
    assert labels.index("Field monitoring") == labels.index("Assurance") + 1  # under Knowledge base


# ------------------------------------------------------------------------------------------ a saved row
def test_a_saved_row_adds_its_entries_after_the_focused_part(client, roles, hierarchy, ai_off, menu_choice):
    menu_choice(VIEWER, [*navigation.FOCUSED, "reports:funds", navigation.ARCHIVE_KEY, navigation.HELP_KEY])
    viewer = _person("customised", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    # Funds joins Field monitoring under the one Knowledge base title, in the full menu's order
    assert _labels(html) == [
        "Home",
        "Progress reports",
        "Partner monitoring",
        "Funds",
        "Field monitoring",
        "Help",
    ]
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]
    side = sidebar(html)
    assert side.index(">Funds<") < side.index('data-nav-key="archive"') < side.index(">Help<")
    # the other roles keep the focused menu
    editor = _person("editor-still-focused", SECTION_EDITOR)
    assert _labels(_page(client, editor, reverse("reports:library")))[-1] == "Field monitoring"


def test_names_the_menu_no_longer_has_are_ignored(client, roles, ai_off, menu_choice):
    menu_choice(VIEWER, ["reports:overview", "no:such-page", "reports:monitoring"])
    viewer = _person("old-names", VIEWER)
    assert _labels(_page(client, viewer, reverse("reports:library"))) == ["Home", "Field monitoring"]
    assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:monitoring"}


def test_a_row_without_home_draws_no_home(client, roles, ai_off, menu_choice):
    menu_choice(VIEWER, ["reports:partner_reporting", "reports:funds"])
    viewer = _person("no-home", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    assert _labels(html) == ["Progress reports", "Funds"]
    assert "nav-item-link--help" not in sidebar(html)
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE]


def test_a_choice_saved_before_the_merge_shows_the_same_rows_under_knowledge_base(
    client, roles, ai_off, settings, menu_choice
):
    """The saved choices name the entries by their routes, not their groups: a role's choice saved while
    Partners and Programme documents were under Partner progress (eTools) and Funds, Field monitoring and
    Action points under Funds and assurance shows the same rows, in the full menu's order, under the one
    Knowledge base title (Progress reports and Partner monitoring stay in Partner reporting)."""
    settings.FMM_ENABLED = True
    saved = [
        "reports:overview",
        "reports:partner_reporting",
        "reports:pd_monitoring",
        "reports:partners",
        "reports:programmes",
        "reports:action_points",
        "reports:monitoring",
        "reports:funds",
        "fmm:dashboard",
        "knowledge:index",
    ]
    menu_choice(VIEWER, saved)
    assert menu_choices.for_role(VIEWER) == frozenset(saved)  # every saved name is still in the menu
    viewer = _person("saved-before-merge", VIEWER)
    html = _page(client, viewer, reverse("reports:library"))
    assert groups(html) == [("prp", "Partner reporting (PRP)"), KNOWLEDGE_BASE, ("resources", "Resources")]
    drawn = {g["key"]: [str(i["label"]) for i in g["items"]] for g in navigation.sidebar(viewer)["groups"]}
    assert drawn["prp"] == ["Progress reports", "Partner monitoring"]
    assert drawn["knowledge_base"] == [
        "Programme documents",
        "Partners",
        "Funds",
        "Field monitoring",
        "Monitoring insights",
        "Action points",
    ]
    assert drawn["resources"] == ["Documents"]
    side = sidebar(html)
    assert "Partner progress (eTools)" not in side and "Funds and assurance" not in side
    group = side[side.index('aria-labelledby="nav-title-knowledge_base"') :].split("</div>", 2)[1]
    for label in drawn["knowledge_base"]:
        assert f"<span>{label}</span>" in group, label


def test_every_entry_ticked_keeps_the_focused_part_first(client, roles, ai_off, full_menu):
    """One more tick adds a row and moves none: with every entry, the focused part still comes first, then
    the rest of the full menu in its order."""
    viewer = _person("everything", VIEWER)
    labels = _labels(_page(client, viewer, reverse("reports:library")))
    assert labels[:3] == ["Home", "Progress reports", "Partner monitoring"]
    assert labels[3:6] == ["For you", "Daily review", "What's new"]
    assert labels.index("Field monitoring") == labels.index("Assurance") + 1  # in its group, as in MENU
    assert labels.count("Progress reports") == 1 and labels[-1] == "Help"
    # the Menu page lists the entries in this same order
    entries = [str(e["label"]) for e in navigation.entries()]
    assert [label for label in entries if label in labels] == labels


def test_an_entry_added_to_the_menu_later_shows_to_superusers_only(
    client, roles, ai_off, menu_choice, monkeypatch
):
    later = navigation.MenuGroup(
        "later", "Added later", (navigation.MenuItem("Year-end forecasts", "insights:forecasts", "flag"),)
    )
    monkeypatch.setattr(navigation, "MENU", (*navigation.MENU, later))
    superuser = _person("sees-it", VIEWER, is_staff=True, is_superuser=True)
    assert "Year-end forecasts" in _labels(_page(client, superuser, reverse("reports:library")))
    viewer = _person("does-not", VIEWER)
    assert "Year-end forecasts" not in _labels(_page(client, viewer, reverse("reports:library")))
    menu_choice(VIEWER, [*navigation.FOCUSED, "reports:funds"])  # a row saved before it was added
    assert "Year-end forecasts" not in _labels(_page(client, viewer, reverse("reports:library")))
    # the Menu page lists it, unticked for every role
    client.force_login(superuser)
    html = client.get(reverse("admin:web_menuchoice_changelist")).content.decode()
    ticks = [line for line in html.split("<input") if 'value="insights:forecasts"' in line]
    assert len(ticks) == 3 and not any(" checked" in tick.split(">", 1)[0] for tick in ticks)


# ------------------------------------------------------------------------------------- nothing is blocked
def _menu_routes() -> list[str]:
    return [reverse(item.route) for group in navigation.MENU for item in group.items] + [
        reverse("help:index")
    ]


@pytest.mark.parametrize("role", [VIEWER, SECTION_EDITOR, ADMIN])
def test_a_page_left_out_of_the_menu_opens_as_before(client, roles, hierarchy, role):
    """Each page of the full menu answers a focused person as it answers the same person whose menu shows it
    (the same status and the same redirect): the menu blocks nothing. Compared with the same person, not
    with a superuser, as some pages refuse a Viewer by design."""
    user = _person("same-answer", role, is_staff=role == ADMIN, section=hierarchy["database"].section)
    client.force_login(user)
    every = {r: list(navigation.keys()) for r in (VIEWER, SECTION_EDITOR, ADMIN)}
    for url in _menu_routes():
        menu_choices._memo = (0.0, {})
        focused = raw_get(client, url)
        menu_choices._memo = (0.0, every)
        listed = raw_get(client, url)
        assert (focused.status_code, focused.get("Location")) == (
            listed.status_code,
            listed.get("Location"),
        ), url


def test_the_search_still_lists_every_page(client, roles, hierarchy, ai_off):
    """A focused Viewer finds a page their menu leaves out (Funds) under Pages, and a Section editor finds an
    HPM report by its name (the focused menu has no ActivityInfo block, which holds its only menu link) and
    opens it."""
    viewer = _person("searcher", VIEWER)
    client.force_login(viewer)
    html = client.get(reverse("reports:search"), {"q": "Funds"}, **HX).content.decode()
    assert f'href="{reverse("reports:funds")}"' in html
    editor = _person("hpm-editor", SECTION_EDITOR, section=hierarchy["database"].section)
    client.force_login(editor)
    html = client.get(reverse("reports:search"), {"q": "HPM 2026"}, **HX).content.decode()
    hpm = reverse("reports:report_hpm", args=[hierarchy["report"].id])
    assert f'href="{hpm}"' in html
    assert client.get(hpm).status_code == 200
    # every page of the full menu is in the search's Pages, whatever the person's menu shows
    listed = {page["url"] for page in navigation.pages(viewer)}
    assert {e["url"] for g in navigation.menu(viewer) for e in g["items"]} <= listed


def test_the_year_menu_still_draws_for_a_focused_viewer(client, roles, hierarchy):
    viewer = _person("year-viewer", VIEWER)
    for url in (
        reverse("reports:brief"),
        reverse("reports:database_dashboard", args=[hierarchy["database"].id]),
    ):
        assert 'id="year-menu"' in _page(client, viewer, url), url


def test_a_focused_page_reads_nothing_for_the_archive(client, roles, hierarchy, monkeypatch):
    """The archive's links are not built when the person's menu leaves the archive out."""

    def refused(state):
        raise AssertionError("the archive was built")

    monkeypatch.setattr(navigation, "archive", refused)
    viewer = _person("no-archive", VIEWER)
    assert "sidebar__archive" not in _page(client, viewer, reverse("reports:library"))


# --------------------------------------------------------------------------------- kept in memory
def _menu_queries(queries) -> int:
    return sum('"web_menuchoice"' in q["sql"] for q in queries)


def test_the_choices_are_read_once_then_kept(menu_choice, monkeypatch):
    monkeypatch.setattr(menu_choices, "_pinned", False)
    menu_choice(VIEWER, ["reports:overview", "reports:funds"])
    menu_choices.forget()
    with CaptureQueriesContext(connection) as cold:
        assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:funds"}
    assert _menu_queries(cold.captured_queries) == 1
    with CaptureQueriesContext(connection) as warm:
        assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:funds"}
        assert menu_choices.for_role(ADMIN) == navigation.FOCUSED  # no row: the focused menu
    assert len(warm.captured_queries) == 0
    # another worker, whose memo expired, reads the shared cache: no query either
    menu_choices._memo = None
    with CaptureQueriesContext(connection) as shared:
        assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:funds"}
    assert len(shared.captured_queries) == 0


def test_a_page_makes_no_query_for_the_menu(client, roles, menu_choice):
    menu_choice(VIEWER, ["reports:overview", "reports:funds"])
    viewer = _person("warm-page", VIEWER)
    client.force_login(viewer)
    client.get(reverse("reports:library"))  # the choices read once
    with CaptureQueriesContext(connection) as page:
        html = client.get(reverse("reports:library")).content.decode()
    assert _menu_queries(page.captured_queries) == 0 and "Funds" in _labels(html)


def test_a_save_shows_on_the_next_page(client, roles, ai_off, menu_choice):
    viewer = _person("next-page", VIEWER)
    assert "Funds" not in _labels(_page(client, viewer, reverse("reports:library")))
    menu_choice(VIEWER, [*navigation.FOCUSED, "reports:funds"])  # its signal clears the memo
    assert "Funds" in _labels(_page(client, viewer, reverse("reports:library")))
    MenuChoice.objects.filter(role=VIEWER).delete()
    assert "Funds" not in _labels(_page(client, viewer, reverse("reports:library")))


def test_a_save_shares_the_choices_as_committed(menu_choice, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        menu_choice(VIEWER, ["reports:overview", "reports:funds"])
        # another worker reads the table before the save is committed, and shares what it saw
        caches["shared"].set(menu_choices.CACHE_KEY, {}, menu_choices.SHARED_SECONDS)
    assert caches["shared"].get(menu_choices.CACHE_KEY) == {VIEWER: ["reports:overview", "reports:funds"]}
    menu_choices._memo = None  # another worker, once its memo has expired
    assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:funds"}


def test_choices_read_before_a_save_was_committed_never_replace_it(
    menu_choice, monkeypatch, django_capture_on_commit_callbacks
):
    """Another worker misses the shared cache and reads the table just before a superuser's save is committed,
    then shares what it read after the save's own clean-up ran: the save's choices stay shared."""
    monkeypatch.setattr(menu_choices, "_pinned", False)
    menu_choice(VIEWER, ["reports:overview"])
    menu_choices.forget()
    real = menu_choices._read

    def read_then_save():
        seen = real()  # the other worker reads the table as it is
        monkeypatch.setattr(menu_choices, "_read", real)  # only that read is the other worker's
        with django_capture_on_commit_callbacks(execute=True):  # the superuser's save is committed
            menu_choice(VIEWER, ["reports:overview", "reports:funds"])
        return seen

    monkeypatch.setattr(menu_choices, "_read", read_then_save)
    assert menu_choices.for_role(VIEWER) == {"reports:overview"}  # that worker's page, with what it read
    menu_choices._memo = None  # any worker, once its memo has expired
    assert menu_choices.for_role(VIEWER) == {"reports:overview", "reports:funds"}
    assert caches["shared"].get(menu_choices.CACHE_KEY)[VIEWER] == ["reports:overview", "reports:funds"]


def test_without_the_table_every_role_sees_the_focused_menu(db, monkeypatch):
    """A release's first page before its migration ran: the focused menu, nothing kept, the caller's
    transaction intact."""
    monkeypatch.setattr(menu_choices, "_pinned", False)
    menu_choices.forget()
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE web_menuchoice RENAME TO web_menuchoice_later")
    try:
        assert menu_choices.for_role(VIEWER) == navigation.FOCUSED
        assert menu_choices._memo is None and caches["shared"].get(menu_choices.CACHE_KEY) is None
        assert User.objects.count() == 0  # the transaction still answers
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE web_menuchoice_later RENAME TO web_menuchoice")


def test_a_shared_copy_that_is_not_choices_is_read_again(menu_choice, monkeypatch):
    monkeypatch.setattr(menu_choices, "_pinned", False)
    menu_choice(VIEWER, ["reports:overview"])
    menu_choices.forget()
    caches["shared"].set(menu_choices.CACHE_KEY, ["not", "choices"], menu_choices.SHARED_SECONDS)
    assert menu_choices.for_role(VIEWER) == {"reports:overview"}
