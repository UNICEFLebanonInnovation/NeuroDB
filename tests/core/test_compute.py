"""The shared cache behind pages that load in parts (neurodb.core.compute), its table and its button."""

import datetime
import hashlib
import os
import pickle
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from django.conf import settings
from django.core.cache import caches
from django.core.management import call_command
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone, translation

from neurodb.core import compute
from neurodb.core.models import SyncRun

IN_THE_DATABASE = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "shared": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "neurodb_cache",
        "OPTIONS": {"MAX_ENTRIES": 300, "CULL_FREQUENCY": 3},
    },
}


@pytest.fixture(autouse=True)
def _empty_shared_cache():
    caches["shared"].clear()
    compute.reset_memo()
    yield
    compute.reset_memo()
    caches["shared"].clear()


@pytest.fixture
def database_cache(db):
    """The shared cache as in production: Django's DatabaseCache over the table core 0026 creates.

    The test's own connection stays inside a transaction rolled back at its end, where the advisory lock
    is refused: what a worker does therefore runs in a thread of its own (:func:`_as_worker`), on its own
    connection and committed, as in production. The table is emptied the same way before and after."""
    with override_settings(CACHES=IN_THE_DATABASE):
        _as_worker(_empty_table)
        yield caches["shared"]
        _as_worker(_empty_table)


@contextmanager
def _request():
    """What the request_started and request_finished signals do for compute (sending the signals would
    also close the test's database connection, as at the start of a real request)."""
    compute.request_started()
    try:
        yield
    finally:
        compute.request_finished()


def _empty_table():
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '5s'")  # never wait on a row the test's transaction holds
        cursor.execute("DELETE FROM neurodb_cache")


def _rows() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM neurodb_cache")
        return cursor.fetchone()[0]


def _my_advisory_locks() -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT classid::bigint, objid::bigint, objsubid FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        )
        return cursor.fetchall()


def _in_threads(count: int, work) -> list:
    """Run ``work`` in ``count`` threads started together; each closes its own database connection."""
    start = threading.Barrier(count)
    results: list = [None] * count
    errors: list = []

    def run(index):
        try:
            start.wait()
            results[index] = work()
        except BaseException as exc:  # a failed assertion too: reported by the test, not lost in the thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


def _as_worker(work):
    """``work()`` in another thread, as another worker would run it (committed, outside any transaction)."""
    return _in_threads(1, work)[0]


class _Counter:
    def __init__(self, value, hold: float = 0.0):
        self.calls = 0
        self.value = value
        self.hold = hold
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        time.sleep(self.hold)  # holds the key's lock while the other threads wait
        return self.value


# ---------------------------------------------------------------------------------------- single flight
def test_one_thread_computes_and_every_thread_gets_the_value_in_memory():
    build = _Counter({"tiles": [1, 2, 3]}, hold=0.5)
    results = _in_threads(6, lambda: compute.once("overview:x", build, 60))
    assert build.calls == 1
    assert results == [{"tiles": [1, 2, 3]}] * 6


def test_one_worker_computes_and_every_worker_gets_the_value_with_the_database_cache(database_cache):
    seen_locks = []
    calls = []

    def build():
        calls.append(1)
        seen_locks.extend(_my_advisory_locks())  # while computing: the key's lock is held
        time.sleep(0.6)  # the other workers wait meanwhile
        return {"tiles": [1, 2, 3]}

    results = _in_threads(4, lambda: compute.once("overview:x", build, 60))
    assert len(calls) == 1
    assert results == [{"tiles": [1, 2, 3]}] * 4
    assert _rows() == 1

    # the two-integer form (objsubid 2) from a stable hash of the key: never one of the job locks, which
    # take the single-integer form (objsubid 1)
    first, second = compute._lock_ids(compute._full_key("overview:x", False))
    assert seen_locks == [(first & 0xFFFFFFFF, second & 0xFFFFFFFF, 2)]


def test_the_lock_is_released_after_an_error(database_cache):
    def broken():
        raise ValueError("the build failed")

    def fails_then_holds_nothing():
        with pytest.raises(ValueError, match="the build failed"):
            compute.once("overview:broken", broken, 60)
        return _my_advisory_locks()

    assert _as_worker(fails_then_holds_nothing) == []
    assert _rows() == 0  # nothing stored

    first, second = compute._lock_ids(compute._full_key("overview:broken", False))

    def another_worker_takes_it():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s::int4, %s::int4)", [first, second])
            got = cursor.fetchone()[0]
            cursor.execute("SELECT pg_advisory_unlock(%s::int4, %s::int4)", [first, second])
        return got

    assert _as_worker(another_worker_takes_it) is True
    assert _as_worker(lambda: compute.once("overview:broken", lambda: 7, 60)) == 7


def test_the_lock_is_released_after_an_error_in_memory():
    with pytest.raises(ZeroDivisionError):
        compute.once("overview:broken", lambda: 1 / 0, 60)
    assert compute.once("overview:broken", lambda: 7, 60, wait=0.1) == 7


def test_a_waiter_past_its_wait_computes_or_in_a_part_request_answers_pending(database_cache):
    first, second = compute._lock_ids(compute._full_key("overview:slow", False))
    taken, finished = threading.Event(), threading.Event()

    def another_worker_computing():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s::int4, %s::int4)", [first, second])
            taken.set()
            finished.wait(20)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s::int4, %s::int4)", [first, second])
        finally:
            connection.close()

    def a_part_then_a_page():
        build = _Counter("computed here")
        # a part request gives up after its wait and never computes while the other worker does
        with compute.part_wait(0.3), pytest.raises(compute.Pending):
            compute.once("overview:slow", build, 60)
        calls_in_the_part = build.calls
        # a full page waits up to `wait`, then computes without the lock rather than fail
        return calls_in_the_part, compute.once("overview:slow", build, 60, wait=0.3), build.calls

    holder = threading.Thread(target=another_worker_computing)
    holder.start()
    try:
        assert taken.wait(10)
        assert _as_worker(a_part_then_a_page) == (0, "computed here", 1)
    finally:
        finished.set()
        holder.join(20)


def test_a_waiter_past_its_wait_in_memory():
    full = compute._full_key("overview:slow", False)
    held = compute._ThreadLock(full)
    assert held.acquire()
    try:
        build = _Counter("computed here")
        with compute.part_wait(0.3), pytest.raises(compute.Pending) as pending:
            compute.once("overview:slow", build, 60)
        assert build.calls == 0 and pending.value.key == full
        assert compute.once("overview:slow", build, 60, wait=0.3) == "computed here"
        assert build.calls == 1
    finally:
        held.release()


def test_a_waiter_reads_the_value_the_lock_holder_stored():
    full = compute._full_key("overview:shared", False)
    held = compute._ThreadLock(full)
    assert held.acquire()

    def the_holder_stores_and_releases():
        caches["shared"].set(compute._storage_key(full), compute._encode(5))
        held.release()

    later = threading.Timer(0.3, the_holder_stores_and_releases)
    later.start()
    build = _Counter("not used")
    with compute.part_wait(5):
        assert compute.once("overview:shared", build, 60) == 5
    later.join()
    assert build.calls == 0


def test_part_wait_ends_with_its_block():
    with compute.part_wait(0.5):
        with compute.part_wait(0.1):
            assert compute._local.part_wait == 0.1
        assert compute._local.part_wait == 0.5
    assert compute._local.part_wait is None


def test_inside_atomic_a_missing_value_is_computed_without_the_lock_and_not_stored(database_cache):
    """The database lock is never taken inside transaction.atomic: it would be released before the value
    commits, and a rollback would lose the value. A missing value is computed for the caller alone."""
    assert connection.in_atomic_block  # the test's transaction, as a job's transaction.atomic would be
    locks_while_computing = []

    def build():
        locks_while_computing.extend(_my_advisory_locks())
        return 1

    assert compute.once("overview:atomic", build, 60) == 1
    assert locks_while_computing == [] and _rows() == 0  # no lock, nothing written in the transaction
    assert _as_worker(lambda: compute.peek("overview:atomic")) == (False, None)
    # a value another worker stored is read inside one
    assert _as_worker(lambda: compute.once("overview:atomic", lambda: 2, 60)) == 2
    with transaction.atomic():
        assert compute.once("overview:atomic", lambda: 3, 60) == 2
        assert compute.peek("overview:atomic") == (True, 2)


# ------------------------------------------------------------------------------- read-only transactions
def _a_minute_ago():
    """A minute before now on the clock the cache reads (Python's: the tests move it, not the database's)."""
    return timezone.now() - datetime.timedelta(minutes=1)


def _stored_here(key: str, value, *, expired: bool = False, raw: bytes | None = None) -> None:
    """A value stored in the test's own transaction (where :func:`compute.once` stores nothing), as another
    worker would have stored it; ``expired``: its time is up."""
    full = compute._full_key(key, False)
    caches["shared"].set(compute._storage_key(full), compute._encode(value) if raw is None else raw, 60)
    if expired:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE neurodb_cache SET expires = %s WHERE cache_key LIKE %s",
                [_a_minute_ago(), f"%{compute._storage_key(full)}"],
            )


def test_a_read_only_tool_run_computes_what_is_missing_and_writes_nothing(database_cache):
    """The AI look-ups of NeuroDB Watch, the help and the field monitoring chat run their tools in a read-only
    transaction (``assistant.tools.read_only``): a value missing there is computed, the transaction stays
    usable, and nothing is written."""
    from neurodb.assistant import tools

    with tools.read_only():
        assert compute.once("overview:read-only", lambda: 41, 60) == 41
        assert SyncRun.objects.count() == 0  # the transaction still answers
    assert _rows() == 0


def test_a_read_only_tool_run_reads_what_is_stored(database_cache):
    from neurodb.assistant import tools

    _stored_here("overview:read-only", {"tiles": [1]})
    with tools.read_only():
        assert compute.peek("overview:read-only") == (True, {"tiles": [1]})
        assert compute.once("overview:read-only", lambda: "computed", 60) == {"tiles": [1]}


@pytest.mark.parametrize("read", ["peek", "once"])
@pytest.mark.parametrize("row", ["expired", "unreadable"])
def test_a_read_only_tool_run_leaves_an_expired_or_unreadable_row_alone(database_cache, read, row):
    """Django's database cache deletes an expired row it reads, and compute drops a row it cannot read back.
    A DELETE in a READ ONLY transaction fails and aborts it: every later query of the tool run would fail. There,
    such a row is a miss, and stays for the next read outside a transaction."""
    from neurodb.assistant import tools

    if row == "expired":
        _stored_here("overview:old", 1, expired=True)
    else:
        _stored_here("overview:old", None, raw=b"?not a value compute wrote")
    with tools.read_only():
        if read == "peek":
            assert compute.peek("overview:old") == (False, None)
        else:
            assert compute.once("overview:old", lambda: 3, 60) == 3
        assert SyncRun.objects.count() == 0  # the transaction still answers
    assert _rows() == 1  # left in place


# ------------------------------------------------------------------------------------------ plain data
@pytest.mark.django_db
def test_a_queryset_is_never_stored():
    with pytest.raises(TypeError, match=r"\['runs'\] is a QuerySet"):
        compute.once("bad", lambda: {"runs": SyncRun.objects.all()}, 60)
    assert compute.peek("bad") == (False, None)


def test_a_model_instance_is_never_stored():
    with pytest.raises(TypeError, match=r"\['rows'\]\[1\] is a SyncRun instance"):
        compute.once("bad", lambda: {"rows": [{"n": 1}, SyncRun(job="etools")]}, 60)

    @dataclass
    class Row:
        label: str
        run: object

    with pytest.raises(TypeError, match=r"\['run'\] is a SyncRun instance"):
        compute.once("bad", lambda: [Row("a", 1), Row("b", SyncRun(job="etools"))], 60)
    assert compute.peek("bad") == (False, None)


def test_plain_data_and_dataclasses_of_it_are_stored():
    import datetime
    from decimal import Decimal

    value = {
        "tiles": [("Reached", 12, Decimal("1.5")), None],
        "as_of": datetime.date(2026, 10, 12),
        "rows": [Point(1, "a"), Point(2, "b")],
    }
    assert compute.once("plain", lambda: value, 60) is value
    assert compute.peek("plain") == (True, value)


@dataclass(frozen=True)
class Point:  # at module level, so it can be pickled
    n: int
    label: str


# ------------------------------------------------------------------------------- storing and the keys
def test_values_above_64_kb_are_compressed_and_read_back():
    big = {"rows": [{"label": f"row {i}", "value": i} for i in range(8000)]}
    compute.once("big", lambda: big, 60)
    compute.once("small", lambda: {"n": 1}, 60)

    stored = caches["shared"].get(compute._storage_key(compute._full_key("big", False)))
    assert len(pickle.dumps(big)) > compute.COMPRESS_ABOVE
    assert stored[:1] == b"z" and len(stored) < len(pickle.dumps(big)) / 4
    assert caches["shared"].get(compute._storage_key(compute._full_key("small", False)))[:1] == b"p"
    assert compute.peek("big") == (True, big)
    assert compute.peek("small") == (True, {"n": 1})


def test_a_value_over_4_mb_compressed_is_kept_for_the_request_only():
    assert compute.MAX_STORED == 4 * 1024 * 1024 and compute.COMPRESS_ABOVE == 64 * 1024
    noise = os.urandom(5 * 1024 * 1024)  # does not compress
    build = _Counter(noise)
    with _request():
        assert compute.once("noise", build, 60) == noise
        assert compute.once("noise", build, 60) == noise and build.calls == 1  # the request's memo
    assert compute.peek("noise") == (False, None)
    assert compute.once("noise", build, 60) == noise and build.calls == 2


def test_long_or_unusual_keys_are_hashed_for_the_table():
    sections = "|".join(f"Section number {i} (أقسام)" for i in range(40))
    key = f"overview:v9:2026:{sections}"
    assert compute.once(key, lambda: "long", 60) == "long"
    stored = compute._storage_key(compute._full_key(key, False))
    assert len(stored) < 120 and stored.isascii() and " " not in stored
    assert compute.peek(key) == (True, "long")
    assert compute.peek(key + "x") == (False, None)


def test_the_language_is_in_the_key_unless_the_value_is_language_neutral():
    def labelled():
        return translation.get_language()

    with translation.override("en"):
        assert compute.once("labels", labelled, 60) == "en"
        assert compute.once("counts", lambda: "first", 60, lang_neutral=True) == "first"
    with translation.override("fr"):
        assert compute.once("labels", labelled, 60) == "fr"
        assert compute.once("counts", lambda: "second", 60, lang_neutral=True) == "first"
        assert compute.peek("labels") == (True, "fr")
        assert compute.peek("counts", lang_neutral=True) == (True, "first")
        assert compute.peek("counts") == (False, None)
    with translation.override("ar"):
        assert compute.peek("labels") == (False, None)


def _release_in_the_key(run):
    """The table outlives deployments: a new release never reads what an older one stored, nor a rollback
    what the newer one stored (a value pickled by another release's code may not read the same, or at all).
    The release (``APP_VERSION``, the image's git SHA) starts every key. ``run(work)`` runs as a worker."""
    calls = []

    def build():
        calls.append(settings.APP_VERSION)
        return {"release": settings.APP_VERSION}

    with override_settings(APP_VERSION="aaa"):
        assert run(lambda: compute.once("overview:x", build, 60)) == {"release": "aaa"}
        assert run(lambda: compute.peek("overview:x")) == (True, {"release": "aaa"})
    with override_settings(APP_VERSION="bbb"):  # the next deployment
        assert run(lambda: compute.peek("overview:x")) == (False, None)
        assert run(lambda: compute.once("overview:x", build, 60)) == {"release": "bbb"}
    with override_settings(APP_VERSION="aaa"):  # rolled back: its own value, never the newer release's
        assert run(lambda: compute.peek("overview:x")) == (True, {"release": "aaa"})
        assert run(lambda: compute.once("overview:x", build, 60)) == {"release": "aaa"}
    assert calls == ["aaa", "bbb"]


def test_the_release_is_in_the_key_in_memory():
    _release_in_the_key(lambda work: work())


def test_the_release_is_in_the_key_with_the_database_cache(database_cache):
    _release_in_the_key(_as_worker)


def test_peek_never_computes_and_tells_a_stored_none_from_a_missing_value():
    assert compute.peek("nothing") == (False, None)
    compute.once("nothing", lambda: None, 60)
    assert compute.peek("nothing") == (True, None)
    assert compute.peek("never stored") == (False, None)


def test_peek_as_of_reads_only_a_stored_build_of_another_well_formed_fingerprint():
    """A part reads the build its frame was drawn with (``as_of``) when it is still stored and the figures have
    moved since; otherwise the caller reads the figures of the moment. Nothing is ever computed under ``as_of``,
    and anything but a fingerprint is ignored."""
    then, now = compute.fingerprint("41:2:2026-10-01"), compute.fingerprint("42:2:2026-10-01")
    assert len(then) == 16 and then != now and compute.fingerprint("41:2:2026-10-01") == then
    key_of = "brief:v1:2026:{}".format
    assert compute.peek_as_of(key_of, then, now) == (False, None)  # not stored: the caller computes now's
    compute.once(key_of(then), lambda: {"children": 700}, 60)
    assert compute.peek_as_of(key_of, then, now) == (True, {"children": 700})
    compute.once(key_of(now), lambda: {"children": 500}, 60)
    assert compute.peek_as_of(key_of, now, now) == (False, None)  # nothing moved: the usual key, read once
    assert compute.peek_as_of(key_of, "", now) == (False, None)  # an address without it
    looked_up = []

    def spy(stamp: str) -> str:
        looked_up.append(stamp)
        return key_of(stamp)

    for crafted in ("41", then.upper(), then + "0", "brief:v1:2026:" + then, "*"):
        assert compute.peek_as_of(spy, crafted, now) == (False, None)
    assert looked_up == []  # not even looked up
    # stored with the language in its key: a language-neutral build's key is another one
    assert compute.peek_as_of(key_of, then, now, lang_neutral=True) == (False, None)


def test_once_as_of_reads_one_build_for_a_page_whatever_moves_before_or_between_its_parts():
    """The figures move between a page's frame (``then``) and its first part (``first``), and again before the
    second part (``second``): both parts read one build, computed once at the first part's moment and kept
    for the frame's fingerprint (up to round 3 the second part computed a build of its own, and the page showed
    two moments side by side). What is kept for a fingerprint has a key space of its own: the frame's look at
    what is stored (``peek``) and every request of that moment (``once``) never read it."""
    then, first, second = (compute.fingerprint(f"{run}:2:2026-10-01") for run in (41, 42, 43))
    key_of = "brief:v1:2026:{}".format
    build, later = _Counter({"children": 500}), _Counter({"children": 700})
    with _request():  # the first part
        assert compute.once_as_of(key_of, then, first, build, 60) == {"children": 500}
    with _request():  # the second part
        assert compute.once_as_of(key_of, then, second, later, 60) == {"children": 500}
    assert (build.calls, later.calls) == (1, 0)
    assert compute.peek(key_of(first)) == (True, {"children": 500})  # stored for its own moment, as always
    assert compute.peek(key_of(then)) == (False, None) and compute.peek(key_of(second)) == (False, None)
    assert compute.once(key_of(then), lambda: {"children": 600}, 60) == {"children": 600}
    # nothing moved since the frame: the moment's key, as any request
    assert compute.once_as_of(key_of, second, second, later, 60) == {"children": 700} and later.calls == 1
    # an address without a fingerprint, or with anything else, reads the moment too
    for crafted in ("", "41", then.upper(), "brief:v1:2026:" + then):
        assert compute.once_as_of(key_of, crafted, second, later, 60) == {"children": 700}
    assert later.calls == 1


def test_a_part_asked_while_the_first_one_computes_waits_for_the_build_it_keeps():
    """The second part can come while the first still computes the build (it was asked again after a pending
    answer, or the figures moved again meanwhile): it waits for the build kept for the frame's fingerprint
    instead of computing another one for its own moment."""
    then, first, second = (compute.fingerprint(f"{run}:2:2026-10-01") for run in (41, 42, 43))
    key_of = "brief:v1:2026:{}".format
    computing, finish = threading.Event(), threading.Event()
    first_part: list = []

    def slow():
        computing.set()
        finish.wait(10)  # the build takes a while; the second part comes meanwhile
        return {"children": 500}

    worker = threading.Thread(
        target=lambda: first_part.append(compute.once_as_of(key_of, then, first, slow, 60))
    )
    worker.start()
    later = _Counter({"children": 700})
    try:
        assert computing.wait(10)
        threading.Timer(0.3, finish.set).start()
        with compute.part_wait(5):
            assert compute.once_as_of(key_of, then, second, later, 60) == {"children": 500}
    finally:
        finish.set()
        worker.join(10)
    assert first_part == [{"children": 500}] and later.calls == 0


def test_a_fingerprint_cannot_be_worked_out_without_the_secret_key():
    """The text of a fingerprint is easy to guess (the overview's on a world without flags is the next sync
    run's number and ``:0:``); its fingerprint is keyed with the secret key, so an address can only carry
    one the server gave out, never one of a moment to come."""
    text = "4242:0:"
    mine = compute.fingerprint(text)
    assert compute._FINGERPRINT.fullmatch(mine) and compute.fingerprint(text) == mine
    assert mine != hashlib.sha256(text.encode()).hexdigest()[:16]
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
        assert compute.fingerprint(text) != mine


# ----------------------------------------------------------------------------------------- the memo
def test_the_memo_lives_only_inside_a_request():
    build = _Counter({"n": 1})
    compute.once("memo", build, 60)
    caches["shared"].clear()
    compute.once("memo", build, 60)
    assert build.calls == 2  # outside a request every read goes to the shared cache

    with _request():
        first = compute.once("memo", build, 60)
        caches["shared"].clear()
        assert compute.once("memo", build, 60) is first and build.calls == 2
        assert compute.peek("memo") == (True, first)
    assert compute.peek("memo") == (False, None)

    with _request():
        compute.once("memo", build, 60)
        compute.reset_memo()  # as the tests do between tests: no memo until the next request starts
        caches["shared"].clear()
        assert compute.peek("memo") == (False, None)


@pytest.mark.django_db
def test_a_real_request_starts_and_ends_the_memo(client, monkeypatch):
    seen = []
    from neurodb.web import views

    original = views.healthz

    def spy(request):
        seen.append(compute._memo())
        return original(request)

    monkeypatch.setattr(views, "healthz", spy)
    client.get("/healthz/")
    assert seen == [{}] and compute._memo() is None


# ----------------------------------------------------------------------------------------- data_stamp
@pytest.mark.django_db
def test_data_stamp_changes_when_a_sync_run_finishes(django_assert_num_queries):
    before = compute.data_stamp()
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS, status=SyncRun.Status.RUNNING)
    assert compute.data_stamp() == before  # still running
    run.finish(SyncRun.Status.SUCCEEDED)
    after = compute.data_stamp()
    assert after != before and after.startswith(f"{run.pk}:")

    with _request():
        stamp = compute.data_stamp()
        with django_assert_num_queries(0):
            assert compute.data_stamp() == stamp  # read once per request
        SyncRun.objects.create(job=SyncRun.Job.LOCATIONS, status=SyncRun.Status.RUNNING).finish(
            SyncRun.Status.FAILED, error="boom"
        )
        assert compute.data_stamp() == stamp
    assert compute.data_stamp() != stamp


# ----------------------------------------------------------------------------------- the cache table
def test_the_migrated_table_works_as_django_s_database_cache(database_cache):
    with connection.cursor() as cursor:
        cursor.execute("SELECT relpersistence FROM pg_class WHERE relname = 'neurodb_cache'")
        assert cursor.fetchone() == ("u",)  # UNLOGGED
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'neurodb_cache'")
        indexes = " ".join(row[0] for row in cursor.fetchall())
    assert "UNIQUE" in indexes and "(cache_key)" in indexes and "(expires)" in indexes

    cache = database_cache
    cache.set("a", {"x": 1}, 60)
    assert cache.get("a") == {"x": 1} and _rows() == 1
    with connection.cursor() as cursor:
        cursor.execute("UPDATE neurodb_cache SET expires = %s", [_a_minute_ago()])
    assert cache.get("a") is None and _rows() == 0  # expired: missing, and deleted when read

    culled = {**IN_THE_DATABASE, "shared": {**IN_THE_DATABASE["shared"], "OPTIONS": {"MAX_ENTRIES": 5}}}
    with override_settings(CACHES=culled):
        small = caches["shared"]
        for i in range(10):
            small.set(f"k{i}", i, 60)
        assert _rows() == 6  # culled by a third whenever it held more than 5
        assert small.get("k0") is None and small.get("k9") == 9


def test_expired_rows_are_deleted_at_most_every_ten_minutes(database_cache, monkeypatch):
    def expired_row(name):
        caches["shared"].set(name, 1, 60)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE neurodb_cache SET expires = %s WHERE cache_key LIKE %s",
                [_a_minute_ago(), f"%:{name}"],
            )

    def present(name):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM neurodb_cache WHERE cache_key LIKE %s", [f"%:{name}"])
            return cursor.fetchone() is not None

    def worker():
        seen = []
        compute._cleaned_at = 0.0  # this process has not cleaned yet
        expired_row("old-1")
        compute.once("a", lambda: 1, 60)
        seen.append(present("old-1"))

        expired_row("old-2")
        compute.once("b", lambda: 2, 60)
        seen.append(present("old-2"))  # cleaned less than 10 minutes ago

        compute._cleaned_at = time.monotonic() - compute.CLEAN_EVERY - 1
        compute.once("c", lambda: 3, 60)
        seen.append(present("old-2"))
        return seen

    monkeypatch.setattr(compute, "_cleaned_at", compute._cleaned_at)  # put back afterwards
    assert _as_worker(worker) == [False, True, False]


@pytest.mark.django_db
def test_the_migrations_create_and_drop_the_table_and_the_indexes():
    # inside the test's transaction: PostgreSQL rolls the schema changes back with it
    def exists():
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('neurodb_cache') IS NOT NULL")
            table = cursor.fetchone()[0]
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'core_syncrun' "
                "AND indexname IN ('core_syncrun_finished', 'core_syncrun_job_target')"
            )
            return table, sorted(row[0] for row in cursor.fetchall())

    assert exists() == (True, ["core_syncrun_finished", "core_syncrun_job_target"])
    call_command("migrate", "core", "0025_etools_update_cleanup", verbosity=0)
    try:
        assert exists() == (False, [])
    finally:
        call_command("migrate", "core", verbosity=0)
    assert exists() == (True, ["core_syncrun_finished", "core_syncrun_job_target"])


@pytest.mark.django_db
def test_the_last_finished_run_is_read_through_its_index():
    query = (
        SyncRun.objects.exclude(finished_at=None)
        .order_by("-finished_at", "-id")
        .values_list("id", flat=True)[:1]
    )
    sql, params = query.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")  # a tiny table: the planner would read it whole
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = " ".join(row[0] for row in cursor.fetchall())
    assert "core_syncrun_finished" in plan


@pytest.mark.django_db
def test_no_model_change_is_left_without_its_migration():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


# ------------------------------------------------------------------------------------------ the button
def _button():
    return reverse("admin:core_syncrun_run_empty_page_figures")


def test_the_button_empties_the_table(database_cache, client, admin_user):
    database_cache.set("overview:a", 1, 60)
    database_cache.set("overview:b", 2, 60)
    assert _rows() == 2
    client.force_login(admin_user)

    assert client.get(_button()).status_code == 200 and _rows() == 2  # a GET only confirms
    response = client.post(_button(), {"_form_submitted": "on"}, follow=True)
    assert "The stored page figures were emptied (2 entries)." in response.content.decode()
    assert _rows() == 0


@pytest.mark.django_db
def test_the_button_is_in_the_job_menu_for_administrators_only(client, admin_user, viewer):
    compute.once("overview:a", lambda: 1, 60)
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    client.post(_button(), {"_form_submitted": "on"})
    assert compute.peek("overview:a") == (True, 1)

    client.force_login(admin_user)
    assert (
        "Empty the stored page figures"
        in client.get(reverse("admin:core_syncrun_changelist")).content.decode()
    )
    response = client.post(_button(), {"_form_submitted": "on"}, follow=True)
    assert "The stored page figures were emptied." in response.content.decode()
    assert compute.peek("overview:a") == (False, None)
