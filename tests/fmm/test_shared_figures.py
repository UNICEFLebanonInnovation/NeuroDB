"""The Monitoring insights blocks kept in the cache every worker shares (step 3, STAGE3 group C).

``metrics.summary``, ``metrics.entity_rows``, ``metrics.rule_counts`` and ``geo.map_points`` are kept with
``metrics.cached(..., shared=True)`` (``core.compute.once``): one request at a time computes them, every worker
reads what it stored. A stored value must be plain data, never show one person's view to another, never answer
one language with another's words, and every input staff edit must be in its key or in the stamp, so that an
edit shows at once. These tests prove each, with two users and through the real admin and job runs.

On ``fm_world`` built on 5 October 2026 (``built``).
"""

from __future__ import annotations

import datetime
import json
from collections import Counter

import pytest
from django.core.cache import cache, caches
from django.test import Client
from django.urls import reverse
from django.utils import translation

from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.fmm import metrics
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import ScoreSetting
from neurodb.fmm.scope import Scope
from neurodb.geo.models import Location, LocationType
from tests.fmm.test_pages import _section_editor
from tests.web.parts import raw_get

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 5)
PAGE = "/fmm/"
SHARED = ("summary:", ":entities:", ":rule_counts", ":map:")  # how each shared block's key reads


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


def _signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _computed(monkeypatch) -> Counter:
    """How many times each shared block was computed (by the part of its key that names it), by language."""
    calls: Counter = Counter()
    real = compute.once

    def once(key, build, ttl, **kwargs):
        def counted():
            for name in SHARED:
                if name in key:
                    calls[(name.strip(":"), translation.get_language())] += 1
            return build()

        return real(key, counted, ttl, **kwargs)

    monkeypatch.setattr(compute, "once", once)
    return calls


def _tab(client, tab: str, **params):
    response = raw_get(
        client, PAGE, {"tab": tab, **params}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="fmm-results"
    )
    assert response.status_code == 200
    return response


def test_the_shared_blocks_are_computed_once_for_every_user(built, fm_world, admin_user, monkeypatch):
    """An editor's request stores them; an administrator's, with this worker's own cache emptied, reads what was
    stored: the same figures, computed once, though the two people differ in role and section."""
    calls = _computed(monkeypatch)
    editor, staff = _signed_in(_section_editor(fm_world.section)), _signed_in(admin_user)
    address = {"section": "Education", "entity_kind": "all"}
    first = _tab(editor, "analysis", **address)
    assert {name for name, _lang in calls} == {"summary", "entities", "rule_counts"}
    assert all(n == 1 for n in calls.values()), calls
    cache.clear()  # this worker's blocks gone: the administrator reads the shared ones
    second = _tab(staff, "analysis", **address)
    assert all(n == 1 for n in calls.values()), calls  # none computed again
    for key in ("highlights", "section_rows", "entity_rows", "rule_rows", "flag_frequency", "places", "gaps"):
        assert first.context[key] == second.context[key], key


def test_what_is_stored_is_plain_data(built):
    """``once()`` refuses a QuerySet or a model instance in the tests; each shared block is checked here too."""
    from neurodb.fmm import geo

    scope = Scope.from_params({"section": "", "year": "2026"})
    when = metrics.stamp()
    limits = metrics.thresholds()
    for value in (
        metrics.summary(scope, when, limits),
        metrics.entity_rows(scope, when),
        metrics.entity_rows(scope, when, "pd"),
        metrics.rule_counts(scope, when),
        geo.map_points(scope, "visited", when=when),
        geo.map_points(scope, "active", when=when),
    ):
        compute._check_plain(value)  # raises TypeError on a QuerySet or a model instance
        assert value


def test_each_language_keeps_its_own_blocks(built, client_viewer, monkeypatch):
    """Labels, words and dates are translated as a block is computed: the shared blocks are kept per language
    (SPEC7_review item 10), so French never reads the English ones."""
    calls = _computed(monkeypatch)
    for language in ("en", "fr", "en"):
        raw_get(
            client_viewer,
            PAGE,
            {"tab": "analysis", "section": "", "entity_kind": "all"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="fmm-results",
            HTTP_ACCEPT_LANGUAGE=language,
        )
    assert calls == {
        ("summary", "en"): 1,
        ("summary", "fr"): 1,
        ("entities", "en"): 1,
        ("entities", "fr"): 1,
        ("rule_counts", "en"): 1,
        ("rule_counts", "fr"): 1,
    }


def _score_form(setting: ScoreSetting, **changes) -> dict:
    """The score settings' admin form as it stands, with ``changes``."""
    data = {
        name: json.dumps(getattr(setting, name))
        if isinstance(getattr(setting, name), dict)
        else getattr(setting, name)
        for name in (
            "band_high",
            "band_medium",
            "high_flag_count",
            "urgency_red",
            "urgency_amber",
            "urgency_weights",
            "recency_days",
            "scored_statuses",
            "follow_up_days",
            "report_late_days",
            "question_patterns",
            "role_flag_answers",
            "ai_max_output_tokens",
            "ai_text_chars",
        )
    }
    data.update(categories=json.dumps(setting.categories), ai_checks="on", ai_temperature="0.30")
    return {**data, "change_note": "Fewer flags make a record high-flag", **changes}


def test_a_score_setting_saved_in_the_admin_shows_at_once(built, client, admin_user):
    """Fresh after edit: the number of flags that makes a record "high-flag" (Score settings) is read by the one
    pass over the visits, which every worker shares. Saved in the admin, it shows at once on the Quality tab,
    before the rescore the save asks for has run (until step 3 the pass was kept under a key without it)."""
    client.force_login(admin_user)

    def high_flag() -> dict:
        return _tab(client, "quality", section="").context["issues_summary"]["high_flag"]

    before = high_flag()
    assert before["at"] == 3  # stored now
    setting = ScoreSetting.load()
    response = client.post(
        reverse("admin:fmm_scoresetting_change", args=[setting.pk]), _score_form(setting, high_flag_count=1)
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    assert ScoreSetting.load().high_flag_count == 1
    after = high_flag()
    assert after["at"] == 1 and after["n"] >= before["n"]


def test_the_stamp_moves_with_what_staff_and_jobs_change_without_a_refresh(built):
    """The stamp every block is kept under moves with a run of the eTools sync, of the gazetteer sync or of the
    eTools update clean-up, and with a place or a kind of place edited in the admin; not with another job's run."""
    seen = [metrics.stamp()]

    def moved() -> bool:
        seen.append(metrics.stamp())
        return seen[-1] != seen[-2]

    for job in (SyncRun.Job.WATCH, SyncRun.Job.DAILY_REVIEW):
        SyncRun.objects.create(job=job, target="all", status=SyncRun.Status.RUNNING).finish(
            SyncRun.Status.SUCCEEDED
        )
        assert not moved(), job
    for job in metrics.INPUT_JOBS:
        run = SyncRun.objects.create(job=job, target="all", status=SyncRun.Status.RUNNING)
        assert not moved(), f"{job} running"  # a run still running changes nothing yet
        run.finish(SyncRun.Status.SUCCEEDED)
        assert moved(), job
    place = Location.objects.filter(type__admin_level=3).first()
    place.name = f"{place.name} (renamed)"
    place.save()
    assert moved()
    kind = LocationType.objects.order_by("pk").first()
    kind.name = f"{kind.name} (renamed)"
    kind.save()
    assert moved()
    assert len(set(seen)) == len(metrics.INPUT_JOBS) + 3


def test_a_drill_on_the_reviews_is_never_shared(built, client_viewer, monkeypatch):
    """A scope that drills into the reviews is never kept (a review saved a minute ago counts at once): not in the
    shared cache either."""
    calls = _computed(monkeypatch)
    _tab(client_viewer, "analysis", section="", review="none", entity_kind="all")
    assert calls == {}
    assert not caches["shared"]._cache  # nothing stored (LocMem in the tests)
