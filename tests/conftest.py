"""Shared fixtures. The test database is created from the migrations, v2 tables included."""

import datetime

import pytest
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
