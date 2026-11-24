"""NeuroDB's own grouping of partners' PD indicators is removed (the owner's decision: the eTools Datamart has no
such grouping). Its old addresses open Partner monitoring's default view, its pages and actions open Partner
monitoring too (a GET and a POST alike), its route names are gone, the figure trail of an old address is Data
quality's, and check 12 is gone from the eTools reporting checks: checks 1 to 11 keep their numbers, and a stored
result that still holds card 12 shows without it. The grouping's old admin lists (a bookmark, or a closed NeuroDB
Watch card that still links to one) open Partner monitoring too, while the rest of the administration is as it was.

Before the removal: ``?view=masters`` answered its own view (or a redirect to its year) and its own exports, the
four addresses under ``/partner-monitoring/masters/`` answered their pages and actions, the trail of an old
address answered 404, and the checks page showed card 12. Right after the removal the old admin lists answered 404.
"""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the modules that define them)
from __future__ import annotations

import re

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.core.models import SyncRun
from neurodb.datamart import checks
from tests.reports.test_pd_monitoring import data
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/partner-monitoring/"  # reports:pd_monitoring
DATA_QUALITY = ["quality-tiles", "quality-lists", "summary", "grid"]
CHECKS_PAGE = "admin:datamart_reporting_checks"


@pytest.fixture
def world(more, frozen):
    return more


def _validator() -> Client:
    user = User.objects.create_user(username="checker", email="checker@example.org", password="x-pass-123456")
    user.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=VALIDATORS))
    client = Client()
    client.force_login(user)
    return client


# --------------------------------------------------------------------------------------------- old addresses
@pytest.mark.parametrize(
    "params",
    [
        {"view": "masters"},
        {"view": "masters", "year": "2026", "section": "Education"},
        {"view": "masters", "year": "all"},
        {"view": "masters", "export": "csv"},
        {"view": "masters", "export": "xlsx"},
        {"view": "masters", "_part": "masters-table"},  # a part asked for by a page left open
    ],
)
def test_an_old_address_of_the_removed_view_opens_the_bare_page(client_viewer, params):
    response = raw_get(client_viewer, PAGE, params)
    assert response.status_code == 302
    assert response["Location"] == reverse("reports:pd_monitoring") == PAGE  # the query dropped
    assert not response.has_header("Content-Disposition")  # never a file, nor Program Performance's


def test_following_it_lands_on_data_quality(client_viewer, world):
    response = client_viewer.get(PAGE, {"view": "masters"}, follow=True)
    assert response.status_code == 200
    assert response.redirect_chain == [(PAGE, 302)]
    assert response.context["view"] == "quality"
    assert list(response.parts)[: len(DATA_QUALITY)] == DATA_QUALITY


@pytest.mark.parametrize(
    "path",
    [
        "/partner-monitoring/masters/5/",
        "/partner-monitoring/masters/5/edit/",
        "/partner-monitoring/masters/attach/",
        "/partner-monitoring/masters/confirm/",
        "/partner-monitoring/masters/5/?year=2026&report_type=QPR",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_the_old_pages_and_actions_open_partner_monitoring(client_viewer, path, method):
    if method == "get":
        response = raw_get(client_viewer, path)
    else:
        response = client_viewer.post(path, {"action": "confirm", "link": "1"})
    assert response.status_code == 302
    assert response["Location"] == PAGE


ADMIN = f"/{settings.ADMIN_URL_PATH}"


@pytest.mark.parametrize(
    "path",
    [
        "datamart/etoolsmaster/",
        "datamart/etoolsmaster/?target=listed",  # the Needs attention line's, kept by a closed Watch card
        "datamart/etoolsmaster/add/",
        "datamart/etoolsmaster/5/change/",
        "datamart/etoolsmaster/5/add-pd-indicators/",
        "datamart/etoolsmasteryear/",
        "datamart/etoolsmasteryear/3/change/",
        "datamart/etoolsmasterlink/?status__exact=proposed",
        "datamart/etoolsmasterlink/7/change/",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_the_old_admin_lists_open_partner_monitoring(admin_user, path, method):
    client = Client()
    client.force_login(admin_user)
    response = getattr(client, method)(ADMIN + path)
    assert response.status_code == 302
    assert response["Location"] == PAGE  # the query dropped


def test_the_rest_of_the_administration_is_as_it_was(admin_user):
    client = Client()
    client.force_login(admin_user)
    assert client.get(reverse("admin:app_list", args=["datamart"])).status_code == 200
    assert client.get(reverse(CHECKS_PAGE)).status_code == 200
    # an address of the datamart's administration that never existed is still the admin's own 404
    assert client.get(ADMIN + "datamart/etoolsmasters/").status_code == 404
    for name in ("etoolsmaster", "etoolsmasteryear", "etoolsmasterlink"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:datamart_{name}_changelist")


def test_a_visitor_signs_in_first(client):
    response = client.get(ADMIN + "datamart/etoolsmaster/")
    assert response.status_code == 302 and response["Location"] != PAGE


@pytest.mark.parametrize(
    "name", ["etools_master", "etools_master_edit", "etools_master_attach", "etools_masters_confirm"]
)
def test_the_removed_route_names_are_gone(name):
    with pytest.raises(NoReverseMatch):
        reverse(f"reports:{name}", args=[5] if name in ("etools_master", "etools_master_edit") else [])


def test_an_indicator_address_still_opens_the_indicator_not_the_redirect():
    """The redirect takes only the old addresses: ``partner-monitoring/<pk>/<key>/`` stays the indicator's."""
    assert reverse("reports:pd_indicator", args=[5, "s101"]) == "/partner-monitoring/5/s101/"


def test_the_filter_bar_names_an_indicator_with_every_filter(client_viewer, world):
    page = text(raw_get(client_viewer, PAGE))
    assert '<label class="form-label" for="f-q">Indicator</label>' in page
    assert 'placeholder="Indicator or output"' in page
    assert 'id="f-pd-status"' in page and 'value="all"' in page  # the PD status and "All years", always


def test_the_trail_of_an_old_address_is_data_qualitys(roles, world):
    """Before: a 404 (the grouping had no trail). Now the address opens Data quality, so its trail does too."""
    client = _validator()
    old = raw_get(client, reverse("reports:check_figures", args=["partner-monitoring"]), {"view": "masters"})
    bare = raw_get(client, reverse("reports:check_figures", args=["partner-monitoring"]))
    assert old.status_code == bare.status_code == 200


# --------------------------------------------------------------------------------------------- check 12
def test_check_12_is_gone_and_checks_1_to_11_keep_their_numbers():
    assert [c.key for c in checks.CHECKS] == [str(n) for n in range(1, 12)]
    assert "Indicator grouping" not in {c.title for c in checks.CHECKS}
    assert checks.RUN_FIRST == ("10", "11")
    assert not hasattr(checks, "_grouping") and not hasattr(checks, "GROUPING_STATEMENTS")


def _stored_with_card_12() -> SyncRun:
    """A result the release before the removal stored: this release's eleven cards as they are, and card 12."""
    cards = [
        {
            "key": c.key,
            "title": c.title,
            "question": c.question,
            "neurodb": c.neurodb,
            "tone": checks.OK,
            "verdict": f"Then {c.key}.",
            "figures": [],
        }
        for c in checks.CHECKS
    ]
    cards.append(
        {
            "key": "12",
            "title": "Indicator grouping",
            "question": "A question of the removed check?",
            "neurodb": "What the removed check said.",
            "tone": checks.INFO,
            "verdict": "Verdict of the removed check.",
            "figures": [("Removed figure", 7)],
            "tables": [{"title": "Removed table", "columns": ("Key",), "rows": [("removed_key",)]}],
        }
    )
    return SyncRun.objects.create(
        job=SyncRun.Job.REPORTING_CHECKS,
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        details={"checks": cards},
    )


def test_a_stored_result_holding_card_12_is_read_without_it(db):
    read = checks.read_stored(_stored_with_card_12().details["checks"])
    assert [c["key"] for c in read.cards] == [str(n) for n in range(1, 12)]
    assert (read.missing, read.changed, read.dropped) == ([], [], ["12"])


def test_a_stored_result_holding_card_12_shows_without_it(client, admin_user, monkeypatch):
    _stored_with_card_12()
    monkeypatch.setattr(checks, "run_all", lambda *a, **k: pytest.fail("the page ran the checks itself"))
    client.force_login(admin_user)
    response = client.get(reverse(CHECKS_PAGE))
    assert response.status_code == 200
    page = text(response)
    assert "Check 1 ·" in page and "Check 11 ·" in page and "Then 11." in page
    assert not re.search(r"Checks? 12\b", page)
    for removed in ("Indicator grouping", "removed check", "Removed figure", "Removed table", "removed_key"):
        assert removed not in page
    # stored by checks other than this release's: said so, until the next run replaces it
    assert "This result was stored by an earlier release of NeuroDB" in page
    assert "Run the checks now" in page
