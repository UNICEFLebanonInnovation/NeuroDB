"""The overview's frame never waits for another request's lock on its own cheap values.

The filter bar's choices (``overview.options``) and the freshness strip (``overview.freshness``) take a few
milliseconds to compute, while the shared cache's lock is looked at every 200 ms. In the load check of
``scripts/parity/timing.py`` (8 people opening the overview at once on the large parity world), the frames
that waited for another request computing them took 300-720 ms; computing them took 3-24 ms. So a frame that
finds them being computed computes them itself, and the frame comes at once.
"""

import concurrent.futures

import pytest
from django.db import connection
from django.utils import translation

from neurodb.core import compute
from neurodb.reports import overview


@pytest.fixture
def every_lock_taken(monkeypatch):
    """As if another request held the lock of every shared value, for as long as anyone waits."""
    monkeypatch.setattr(compute._ThreadLock, "acquire", lambda self, timeout=0.0: False)


# Not transaction=True: its flush at the end would empty the rows the migrations seed (the scheduled jobs)
# in a reused test database. The worker thread reads on its own connection; this test writes nothing.
@pytest.mark.django_db
@pytest.mark.usefixtures("every_lock_taken")
@pytest.mark.parametrize("value", ["options", "freshness"])
def test_the_frame_computes_its_cheap_values_rather_than_wait(value):
    compute_it = (lambda: overview.options(2026)) if value == "options" else overview.freshness

    def call():
        try:
            return compute_it()
        finally:
            connection.close()  # the worker thread's own connection

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        # waiting for the lock (30 s by default) would miss this bound by far
        result = pool.submit(call).result(timeout=10)
    assert result == (overview._options() if value == "options" else overview._freshness())


@pytest.mark.django_db
def test_both_are_still_kept_in_the_shared_cache(monkeypatch):
    asked = []
    real = compute.once

    def once(key, fn, ttl, **kwargs):
        asked.append((key.split(":")[0], kwargs.get("wait")))
        return real(key, fn, ttl, **kwargs)

    monkeypatch.setattr(compute, "once", once)
    overview.options(2026)
    overview.freshness()
    assert asked == [("overview-options", 0), ("overview-freshness", 0)]
    computed = []
    # the filter bar's sections are what options() stores (its governorates are read at each request)
    monkeypatch.setattr(overview, "_sections", lambda: computed.append("options") or [])
    monkeypatch.setattr(overview, "_freshness", lambda: computed.append("freshness") or [])
    overview.options(2026)
    overview.freshness()
    assert computed == []  # read back from the shared cache, not computed again


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["options", "freshness"])
def test_one_stored_value_serves_every_language(monkeypatch, value):
    """The filter bar's sections and the freshness strip hold no translated text (test_overview_parts checks
    the strip), so each is stored once for every language (``lang_neutral``): a visit in Arabic reads what
    a visit in English stored, and never computes it again."""
    name = "_sections" if value == "options" else "_freshness"
    real = getattr(overview, name)
    computed = []
    monkeypatch.setattr(overview, name, lambda: computed.append(value) or real())
    compute_it = (lambda: overview.options(2026)) if value == "options" else overview.freshness
    with translation.override("en"):
        english = compute_it()
    with translation.override("ar"):
        arabic = compute_it()
    assert arabic == english
    assert computed == [value], f"{value}: computed again for another language"
