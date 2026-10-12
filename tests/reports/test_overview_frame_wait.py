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
    monkeypatch.setattr(overview, "_options", lambda: computed.append("options") or {"sections": []})
    monkeypatch.setattr(overview, "_freshness", lambda: computed.append("freshness") or [])
    overview.options(2026)
    overview.freshness()
    assert computed == []  # read back from the shared cache, not computed again
