"""Shared fixtures. The test database is created from the migrations, v2 tables included.

One clock for the whole suite: every test runs on Thursday 15 October 2026, 09:00 in Beirut (``site_today``),
with the clock ticking from there, so ``timezone.now()``, ``timezone.localdate()``, ``date.today()`` and
``neurodb.core.years.today()`` always agree, and a test means the same on 1 January as in October. A test
moves the clock with ``site_today.set(date)``; one that needs the real clock is marked ``real_clock``. The
database's own clock (``now()``, ``current_date`` in SQL) is not moved.
"""

import datetime
import os
import zoneinfo

import pytest
import time_machine
from django.contrib.auth.models import Group
from django.test import Client

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, VIEWER, ensure_groups
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportMasterIndicator,
    ReportingYear,
    SubIndicator,
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "shell: the test checks a page's shell itself: client.get answers the shell with its placeholders, "
        "not the page assembled with its parts (tests/web/parts.py)",
    )
    config.addinivalue_line(
        "markers",
        "parts_may_fail: a part of a page the test client assembles may answer something other than its "
        "figures (a 403, a 404, a 202); without this mark such a part fails the test (tests/web/parts.py)",
    )
    config.addinivalue_line(
        "markers",
        "real_clock: the test runs on the real clock, not on the suite's fixed day (site_today does nothing)",
    )


BEIRUT = zoneinfo.ZoneInfo("Asia/Beirut")
SITE_NOW = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=BEIRUT)  # a Thursday


class SiteClock:
    """The suite's clock (``site_today``): ``now`` is where it started; ``set`` travels again."""

    def __init__(self, traveller):
        self._traveller = traveller
        self.now = SITE_NOW

    def set(self, when):
        """Travel to ``when``: a date (09:00 in Beirut that day) or a datetime (naive: in Beirut). The clock
        ticks on from there."""
        if not isinstance(when, datetime.datetime):
            when = datetime.datetime.combine(when, datetime.time(9, 0))
        if when.tzinfo is None:
            when = when.replace(tzinfo=BEIRUT)
        self._traveller.move_to(when)
        self.now = when
        return when


@pytest.fixture(autouse=True)
def site_today(request):
    """Every test runs on 15 October 2026 at 09:00 in Beirut, the clock ticking (``tick=True``); yields the
    :class:`SiteClock` (``site_today.set(date)``). A test marked ``real_clock`` keeps the real clock (None)."""
    if request.node.get_closest_marker("real_clock") is not None:
        yield None
        return
    travel = time_machine.travel(SITE_NOW, tick=True)
    traveller = travel.start()
    try:
        yield SiteClock(traveller)
    finally:
        travel.stop()


@pytest.fixture(scope="session", autouse=True)
def _no_anthropic_key():
    """No Anthropic key, token or address from the environment reaches a test (one exported for a live check
    included): the SDK would read them, and settings, computed before any fixture runs, would count the key
    in AI_ASSISTANT_ENABLED. The tests run on OpenAI unless a test chooses Claude (``claude``). The AI
    settings snapshot is pinned for the run (no 10-second expiry): a page never reads the row at a random
    test, whose query counts or lack of a database would then fail. So are the menu's choices
    (``neurodb.web.menu_choices``), for the same reason."""
    from django.conf import settings
    from django.test import override_settings

    from neurodb.assistant import llm
    from neurodb.web import menu_choices

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        os.environ.pop(var, None)
    overrides = override_settings(
        ANTHROPIC_API_KEY="",
        AI_PROVIDER="auto",
        AI_ASSISTANT_ENABLED=bool(settings.AI_ASSISTANT_SWITCH and settings.OPENAI_API_KEY),
    )
    overrides.enable()
    llm._pinned = True
    menu_choices._pinned = True
    yield
    llm._pinned = False
    menu_choices._pinned = False
    overrides.disable()


@pytest.fixture(autouse=True)
def _ai_settings_defaults():
    """Every test starts from AI settings with nothing entered (no row), held in memory: the test database
    has none, and a test that wrote one rolled it back without a signal."""
    from neurodb.assistant import llm

    llm._memo = (0.0, llm.Snapshot())
    yield


@pytest.fixture(autouse=True)
def _menu_defaults():
    """Every test starts with no menu choice saved (no row), held in memory: every role sees the focused
    menu (navigation.FOCUSED), a superuser the full one."""
    from neurodb.web import menu_choices

    menu_choices._memo = (0.0, {})
    yield


@pytest.fixture
def menu_choice(db):
    """For a test that saves menu choices: the choices are read afresh from the table (its saves clear
    them), and the next test starts with none again. ``menu_choice(role, keys)`` saves a role's row."""
    from neurodb.web import menu_choices
    from neurodb.web.models import MenuChoice

    def save(role, keys):
        return MenuChoice.objects.update_or_create(role=role, defaults={"shown": list(keys)})[0]

    menu_choices.forget()
    yield save
    menu_choices.forget()
    menu_choices._memo = (0.0, {})


@pytest.fixture
def full_menu():
    """Every role's menu shows every entry (as if a superuser ticked them all in the Menu page), for a test
    about the rows, their conditions and the archive rather than about the focused menu. The layout is still
    a role's (the focused part first): a test of the full menu's order runs as a superuser."""
    from neurodb.accounts.roles import ALL_ROLES
    from neurodb.indicators.services import navigation
    from neurodb.web import menu_choices

    menu_choices._memo = (0.0, {role: list(navigation.keys()) for role in ALL_ROLES})
    yield
    menu_choices._memo = (0.0, {})


@pytest.fixture
def ai_settings(db):
    """The AI settings row, for a test that writes it: the snapshot is read afresh from the row (its saves
    clear it), and the next test starts from the defaults again."""
    from neurodb.assistant import llm
    from neurodb.assistant.models import AISettings

    llm.forget()
    yield AISettings.load()
    llm.forget()
    llm._memo = (0.0, llm.Snapshot())


@pytest.fixture
def claude(monkeypatch):
    """The AI on Claude, through a local stand-in for the Messages API: ``claude(*replies)`` starts one
    (``tests.assistant.anthropic_mock``) and points the SDK at it (ANTHROPIC_BASE_URL). The key is not real
    and no request leaves the machine."""
    from django.test import override_settings

    from tests.assistant.anthropic_mock import MockMessagesServer

    for var in ("NO_PROXY", "no_proxy"):  # reach the local stand-in directly even behind a proxy
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    overrides = override_settings(
        AI_ASSISTANT_ENABLED=True,
        ANTHROPIC_API_KEY="test-key-not-real",
        AI_PROVIDER="claude",
        CLAUDE_MODEL="claude-opus-5-5",
        CLAUDE_EFFORT="medium",
    )
    overrides.enable()
    servers = []

    def install(*replies):
        server = MockMessagesServer(list(replies)).start()
        servers.append(server)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.base_url)
        return server

    yield install
    overrides.disable()
    for server in servers:
        server.stop()


@pytest.fixture(autouse=True)
def _clear_cache():
    """Navigation and landing figures are cached; every test starts from an empty cache."""
    from django.core.cache import cache, caches

    from neurodb.core import compute
    from neurodb.fmm import metrics
    from neurodb.watch import people

    # the Monitoring insights blocks a request keeps for the rest of it, and whether the figures kept per
    # visit are up to date: a streamed response a test never reads leaves its request open, and what it
    # kept would serve the tests after it (outside any request) on the same thread. The page figures
    # shared by every worker (compute.once) have a request memo of their own, for the same reason.
    metrics.request_finished()
    compute.reset_memo()
    cache.clear()
    caches["shared"].clear()
    people.forget()  # NeuroDB Watch keeps the person names it read for a run
    yield
    metrics.request_finished()
    compute.reset_memo()
    cache.clear()
    caches["shared"].clear()


@pytest.fixture(autouse=True)
def _assembled_pages(request):
    """A page that loads in parts answers its shell, and the browser fetches each part. Here the test
    client does the same: ``client.get`` of such a page (without HTMX) answers the assembled page, every
    part in place (strict ones too), with the parts' template contexts in ``response.context`` after the
    shell's. So a test of a page's figures keeps testing them. A test of the shell itself is marked
    ``@pytest.mark.shell``; ``tests.web.parts.raw_get`` always answers the shell. A part that does not
    answer its figures fails the test, unless it is marked ``@pytest.mark.parts_may_fail``: its
    placeholder would stay, and the test's "not on the page" assertions would pass without testing it.

    The client is patched with a ``MonkeyPatch`` of its own, not the test's ``monkeypatch``: a test that calls
    ``monkeypatch.undo()`` keeps the assembled pages (its later ``client.get`` would otherwise answer the bare
    frame, and every "not on the page" assertion after it would test nothing)."""
    if request.node.get_closest_marker("shell") is not None:
        yield
        return
    from tests.web.parts import assembling

    refuse_failed = request.node.get_closest_marker("parts_may_fail") is None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Client, "get", assembling(Client.get, refuse_failed=refuse_failed))
        yield


@pytest.fixture
def roles(db):
    return ensure_groups()


@pytest.fixture
def viewer(db, roles):
    user = User.objects.create_user(
        username="viewer", email="viewer@example.org", password="viewer-pass-123456"
    )
    user.groups.add(Group.objects.get(name=VIEWER))
    return user


@pytest.fixture
def admin_user(db, roles):
    user = User.objects.create_user(
        username="admin", email="admin@example.org", password="admin-pass-123456", is_staff=True
    )
    user.groups.add(Group.objects.get(name=ADMIN))
    return user


@pytest.fixture
def client_viewer(client, viewer):
    client.force_login(viewer)
    return client


@pytest.fixture
def reporting_year(db):
    return ReportingYear.objects.create(name="2026", year="2026", current=True)


@pytest.fixture
def section(db):
    return Section.objects.create(name="Child Protection", code="CP")


@pytest.fixture
def database(db, reporting_year, section):
    return Database.objects.create(
        ai_id=202618,
        db_id="ck2yrizmo2",
        name="Child Protection 2026",
        label="Child Protection",
        username="",
        password="",
        section=section,
        reporting_year=reporting_year,
        is_funded_by_unicef=False,
    )


@pytest.fixture
def hierarchy(db, database):
    """One master (SUM) with one TOTAL sub of two leaves, plus a SUM_OVER_SUM master; 2 months of facts."""
    activity = Activity.objects.create(
        database=database, name="Case management", label="Case management", ai_form_id="f1"
    )
    leaf_a = IndicatorNew.objects.create(
        database=database, activity=activity, ai_indicator="i_a", name="Children reached_Male", awp_code="1.1"
    )
    leaf_b = IndicatorNew.objects.create(
        database=database,
        activity=activity,
        ai_indicator="i_b",
        name="Children reached_Female",
        awp_code="1.1",
    )
    sub = SubIndicator.objects.create(
        database=database, name="Children reached", awp_code="1.1", aggregation_method="SUM"
    )
    sub.indicators.set([leaf_a, leaf_b])
    master = MasterIndicator.objects.create(
        database=database,
        name="Children reached (total)",
        awp_code="1",
        aggregation_method="SUM",
        awp_target=1000,
        sequence=1,
    )
    MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=1)
    num = SubIndicator.objects.create(
        database=database, name="Girls", awp_code="2.1", aggregation_method="SUM"
    )
    num.indicators.set([leaf_b])
    den = SubIndicator.objects.create(database=database, name="All", awp_code="2.2", aggregation_method="SUM")
    den.indicators.set([leaf_a, leaf_b])
    ratio = MasterIndicator.objects.create(
        database=database,
        name="Share of girls",
        awp_code="2",
        aggregation_method="SUM_OVER_SUM",
        awp_target=50,
        sequence=2,
    )
    MasterSubIndicator.objects.create(master=ratio, sub=num, effect="NUMERATOR", sequence=1)
    MasterSubIndicator.objects.create(master=ratio, sub=den, effect="DENOMINATOR", sequence=2)
    rows = [
        ("i_a", "2026-01", 100, "Partner A", "Beirut", "BEI"),
        ("i_b", "2026-01", 50, "Partner A", "Beirut", "BEI"),
        ("i_a", "2026-02", 200, "Partner B", "Akkar", "AKK"),
        ("i_b", "2026-02", 150, "Partner B", "Akkar", "AKK"),
    ]
    for ai, month, value, partner, gov, gov_code in rows:
        ActivityReportNew.objects.create(
            dbase=database,
            database_ai_id=str(database.ai_id),
            indicator_id=ai,
            indicator_name=ai,
            indicator_value=value,
            month_name=month + "-01",
            month=month,
            partner_label=partner,
            project_label="LEB/PCA2026001",
            location_adminlevel_governorate=gov,
            location_adminlevel_governorate_code=gov_code,
            location_adminlevel_caza=gov,
            location_adminlevel_caza_code=gov_code + "1",
            location_name=f"Site {gov}",
            location_latitude="33.9",
            location_longitude="35.5",
            funded_by="UNICEF",
            last_edited_time=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
        )
    report = NeuroReport.objects.create(name="HPM 2026", is_hpm=True, ryear=database.reporting_year)
    NeuroReportMasterIndicator.objects.create(
        report=report, master=master, label="Children reached", target=1200
    )
    return {
        "database": database,
        "master": master,
        "ratio": ratio,
        "sub": sub,
        "report": report,
        "leaves": [leaf_a, leaf_b],
    }


def _set_activityinfo(mode):
    from neurodb.core.models import ActivityInfoSetting
    from neurodb.indicators.services import activityinfo

    setting, _ = ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()
    return setting


@pytest.fixture
def activityinfo_in_use(db):
    """ActivityInfo set to "In use" by an administrator (``hierarchy``'s records stop in February, so on
    Automatic it is archived on the suite's day)."""
    return _set_activityinfo("in_use")


@pytest.fixture
def activityinfo_archived(db):
    """ActivityInfo set to "Archived" by an administrator."""
    return _set_activityinfo("archived")
