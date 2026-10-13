"""Figures computed once and shared by every worker: the shared cache behind pages that load in parts.

A page's data blocks are requested separately and land on different gunicorn workers. Without a shared
store, each worker would compute the same overview build again. :func:`once` keeps a build in the
``shared`` cache (``CACHES["shared"]``: an UNLOGGED database table, ``neurodb_cache``, that migration
core 0026 creates; memory in tests) and lets one request at a time compute it:

1. the request memo: a value this request has already read or computed;
2. the shared cache;
3. otherwise a lock on the key (a PostgreSQL session advisory lock, so it holds across workers and
   containers; a ``threading.Lock`` per key when the cache is in memory, in tests). The request that
   gets it reads the cache again (another request may have just stored the value), computes, stores
   and releases the lock. The others poll the lock (200 ms, backing off to 500 ms), reading the cache
   between polls, so they pick up the value as soon as it is stored.

A waiter does not wait for ever. In a part request (:func:`part_wait`, set by ``PartsMiddleware``) it
gives up after about 2 s and raises :class:`Pending`: the browser asks again a moment later, and the
thread is free for other requests. Anywhere else (a full page, a print, a job) it waits up to ``wait``
seconds, then computes without the lock: a page must not fail because another request is slow.

What is stored is plain data only: dicts, lists, tuples, sets, numbers, strings, dates, None, and
dataclasses of those. A QuerySet would be evaluated in full by the pickling, and a model instance would
query again for every relation a template follows after reading it back; in DEBUG and in tests,
:func:`once` raises ``TypeError`` when it finds one anywhere in the value. Values are pickled, compressed
with zlib above 64 KB, and not stored at all above 4 MB once compressed (they are then kept for the rest
of the request only).

Keys: the caller's own key function stays (the overview's ``_cache_key``), with everything the figures
depend on, the user or the session included when they do: a shared cache that served one person's view
to another would be a privacy defect. :func:`once` adds the active language (labels are translated when
a build is computed) unless ``lang_neutral=True``, and the release, so a new deployment never reads what
an older one stored. :func:`data_stamp` gives the part of a key that moves when a sync finishes or an
indicator flag changes, so a sync shows at once on every worker. A page's frame writes that part, as a
:func:`fingerprint`, into its parts' addresses, so every part of the page reads the one build its first
part used, even when a sync finished before or between them (:func:`once_as_of`).
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime
import decimal
import hashlib
import logging
import pickle
import re
import threading
import time
import types
import zlib
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.db import DatabaseCache
from django.db import DatabaseError, connection
from django.db.models import Model
from django.db.models.manager import BaseManager
from django.db.models.query import QuerySet, RawQuerySet
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import get_language

logger = logging.getLogger(__name__)

CACHE = "shared"  # the CACHES alias
COMPRESS_ABOVE = 64 * 1024  # bytes of pickle
MAX_STORED = 4 * 1024 * 1024  # bytes stored (compressed); a larger value is kept for the request only
POLL_FIRST, POLL_MAX = 0.2, 0.5  # seconds between two looks at a lock someone else holds
PART_WAIT = 2.0  # seconds a part request waits for another request's build before answering "pending"
CLEAN_EVERY = 600  # seconds between two deletions of the expired rows, per process

_PICKLED, _ZIPPED = b"p", b"z"  # the first byte of a stored value: how the rest is encoded
_SAFE_KEY = re.compile(r"[!-~]{1,200}")  # printable ASCII without spaces, short enough for every backend
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")  # what fingerprint() gives
_AS_OF = "as-of:"  # the key space of the builds kept for a frame's fingerprint (once_as_of), read only there
_UNSAFE_CHARS = re.compile(r"[^\w.:|=-]", re.ASCII)

_local = threading.local()  # .memo (dict, or None outside a request) and .part_wait (seconds, or None)
_thread_locks: dict[str, threading.Lock] = {}  # the per-key locks of an in-memory cache (tests)
_thread_locks_guard = threading.Lock()
_cleaned_at = 0.0  # time.monotonic() of this process's last deletion of expired rows
_cleaned_guard = threading.Lock()


class Pending(Exception):
    """Raised by :func:`once` in a part request when another request is still computing the value and
    the part's wait (:func:`part_wait`) is over. ``PartsMiddleware`` answers 202 and the browser asks
    again."""

    def __init__(self, key: str):
        super().__init__(f"still being computed: {key}")
        self.key = key


# ------------------------------------------------------------------------------------ the request memo
def request_started(**kwargs: Any) -> None:
    """Start the memo of the request this thread now serves (``request_started`` signal)."""
    _local.memo = {}


def request_finished(**kwargs: Any) -> None:
    """End it (``request_finished`` signal): outside a request there is no memo, so a long-lived thread
    (the scheduler, a job, the assistant's tools) always reads the shared cache."""
    _local.memo = None


def reset_memo() -> None:
    """Drop the current thread's request memo, as at the end of a request. The tests call it between
    tests: a streamed response a test never reads leaves its request open on the thread."""
    _local.memo = None


def _memo() -> dict | None:
    return getattr(_local, "memo", None)


@contextlib.contextmanager
def part_wait(seconds: float = PART_WAIT) -> Iterator[None]:
    """Inside this block (a part request's view, wrapped by ``PartsMiddleware``), a :func:`once` that
    finds another request computing its value waits at most ``seconds``, then raises :class:`Pending`
    instead of computing it a second time."""
    previous = getattr(_local, "part_wait", None)
    _local.part_wait = seconds
    try:
        yield
    finally:
        _local.part_wait = previous


# ------------------------------------------------------------------------------------------------ keys
def _full_key(key: str, lang_neutral: bool) -> str:
    """The caller's key, plus the release and, unless the value reads the same in every language, the
    active language (``-`` when translation is switched off, as in some commands)."""
    release = str(settings.APP_VERSION)[:12]
    language = "" if lang_neutral else f"|lang={get_language() or '-'}"
    return f"{release}:{key}{language}"


def _storage_key(full: str) -> str:
    """The key in the cache table: the full key when it is short printable ASCII; otherwise a readable
    beginning and a hash of the whole key (a key over 250 characters fails on every backend)."""
    if _SAFE_KEY.fullmatch(full):
        return full
    digest = hashlib.blake2b(full.encode(), digest_size=16).hexdigest()
    return f"{_UNSAFE_CHARS.sub('_', full[:80])}#{digest}"


def _lock_ids(full: str) -> tuple[int, int]:
    """The two 32-bit integers of the key's advisory lock. A stable hash (never Python's ``hash()``,
    which differs in each process), in the two-integer form, so it can never be one of the job locks
    (single 64-bit integer form, ``objsubid = 1`` in ``pg_locks``)."""
    digest = hashlib.blake2b(full.encode(), digest_size=8).digest()
    return int.from_bytes(digest[:4], "big", signed=True), int.from_bytes(digest[4:], "big", signed=True)


# ---------------------------------------------------------------------------------------------- reading
def _cache():
    return caches[CACHE]


def _uses_database(cache) -> bool:
    return isinstance(cache, DatabaseCache) and connection.vendor == "postgresql"


def _in_transaction(cache) -> bool:
    """Inside ``transaction.atomic`` with the database cache (a read-only tool run of Watch, the help or
    the field monitoring chat; an admin action; a test): what is stored is read, but nothing is written,
    deleted or locked there (see :func:`once`)."""
    return _uses_database(cache) and connection.in_atomic_block


def _read(full: str) -> tuple[bool, Any]:
    """(found, value) from the shared cache. A row that cannot be read back (stored by code that has
    changed since) counts as missing."""
    cache = _cache()
    storage_key = _storage_key(full)
    if _in_transaction(cache):
        return _read_in_transaction(cache, storage_key, full)
    try:
        raw = cache.get(storage_key)
    except DatabaseError:
        logger.exception("compute: the shared cache could not be read (%s)", full)
        return False, None
    if raw is None:
        return False, None
    try:
        return True, _decode(raw)
    except Exception:  # any unreadable row is a miss, never an error page
        logger.warning("compute: dropped an unreadable value (%s)", full, exc_info=True)
        with contextlib.suppress(DatabaseError):
            cache.delete(storage_key)
        return False, None


def _read_in_transaction(cache, storage_key: str, full: str) -> tuple[bool, Any]:
    """:func:`_read` inside a transaction: one plain SELECT of a row that has not expired. Django's own
    ``get`` deletes an expired row it reads, and a DELETE in a READ ONLY transaction fails and aborts it
    (every later query of the tool run would fail). Nothing is deleted here, an unreadable row neither: it
    is a miss, dropped by the next read outside a transaction."""
    table = connection.ops.quote_name(cache._table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT value FROM {table} WHERE cache_key = %s AND expires >= %s",  # noqa: S608 - the settings'
            [cache.make_and_validate_key(storage_key), timezone.now()],
        )
        row = cursor.fetchone()
    if row is None:
        return False, None
    try:
        # the value as DatabaseCache keeps it: the pickle of what was set, in base64
        raw = pickle.loads(base64.b64decode(row[0].encode()))  # noqa: S301 - only this module writes the table
        return True, _decode(raw)
    except Exception:  # any unreadable row is a miss, never an error
        logger.warning("compute: an unreadable value, kept inside a transaction (%s)", full, exc_info=True)
        return False, None


def _decode(raw: bytes) -> Any:
    kind, body = raw[:1], raw[1:]
    if kind == _ZIPPED:
        body = zlib.decompress(body)
    elif kind != _PICKLED:
        raise ValueError(f"unknown encoding {kind!r}")
    return pickle.loads(body)  # noqa: S301 - only this module writes the table


def _encode(value: Any) -> bytes:
    body = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(body) > COMPRESS_ABOVE:
        return _ZIPPED + zlib.compress(body, 6)
    return _PICKLED + body


def peek(key: str, *, lang_neutral: bool = False) -> tuple[bool, Any]:
    """``(True, value)`` when the value :func:`once` would return for ``key`` is already stored (in this
    request's memo or in the shared cache), ``(False, None)`` otherwise. It never computes and never
    waits: a page shell uses it to draw a block inline when its figures are ready."""
    full = _full_key(key, lang_neutral)
    memo = _memo()
    if memo is not None and full in memo:
        return True, memo[full]
    found, value = _read(full)
    if found and memo is not None:
        memo[full] = value
    return found, value


# ---------------------------------------------------------------------------------------------- storing
# values that hold nothing else (a datetime is a date)
_PLAIN = (
    str,
    bytes,
    bytearray,
    int,
    float,
    complex,
    decimal.Decimal,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    type(None),
)
_NOT_WALKED = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.MethodType)


def _check_plain(value: Any) -> None:
    """Raise ``TypeError`` on a QuerySet, a manager or a model instance anywhere in ``value``."""
    stack: list[tuple[Any, tuple]] = [(value, ())]
    seen: set[int] = set()
    while stack:
        obj, path = stack.pop()
        if isinstance(obj, _PLAIN):
            continue
        if isinstance(obj, (QuerySet, RawQuerySet, BaseManager)):
            raise TypeError(
                f"once() stores plain data only: {_where(path)} is a {type(obj).__name__}. "
                "Store the values it gives (a list of dicts or tuples, a count) instead."
            )
        if isinstance(obj, Model):
            raise TypeError(
                f"once() stores plain data only: {_where(path)} is a {type(obj).__name__} instance. "
                "Store the fields the page reads instead."
            )
        if id(obj) in seen or isinstance(obj, _NOT_WALKED):
            continue
        seen.add(id(obj))
        if isinstance(obj, Mapping):
            for item_key, item in obj.items():
                stack.append((item_key, (*path, "<key>")))
                stack.append((item, (*path, item_key)))
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend((item, (*path, index)) for index, item in enumerate(obj))
        elif dataclasses.is_dataclass(obj):
            stack.extend((getattr(obj, field.name), (*path, field.name)) for field in dataclasses.fields(obj))
        elif hasattr(obj, "__dict__"):
            stack.extend((item, (*path, name)) for name, item in vars(obj).items())


def _where(path: tuple) -> str:
    return "the value" + "".join(f"[{part!r}]" for part in path)


def _store(full: str, value: Any, ttl: int) -> None:
    raw = _encode(value)
    if len(raw) > MAX_STORED:
        logger.warning("compute: %s is %d bytes once compressed; kept for this request only", full, len(raw))
        return
    cache = _cache()
    try:
        cache.set(_storage_key(full), raw, ttl)
    except DatabaseError:
        logger.exception("compute: the shared cache could not be written (%s)", full)
        return
    _clean_expired(cache)


def _clean_expired(cache) -> None:
    """Delete the expired rows of the cache table, at most once every 10 minutes per process (the
    database cache only removes them when it is full, or when one is read)."""
    global _cleaned_at
    if not _uses_database(cache):
        return
    now = time.monotonic()
    with _cleaned_guard:
        if _cleaned_at and now - _cleaned_at < CLEAN_EVERY:
            return
        _cleaned_at = now
    table = connection.ops.quote_name(cache._table)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE expires < %s", [timezone.now()])  # noqa: S608
    except DatabaseError:
        logger.exception("compute: the expired rows of the shared cache could not be deleted")


def clear() -> int | None:
    """Empty the shared cache (the *Empty the stored page figures* button). Returns how many stored
    values were removed, or None when the cache cannot tell (in memory)."""
    cache = _cache()
    if not _uses_database(cache):
        cache.clear()
        return None
    table = connection.ops.quote_name(cache._table)
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table}")  # noqa: S608 - the table name comes from the settings
        return cursor.rowcount


# ---------------------------------------------------------------------------------------------- locking
class _ThreadLock:
    """One ``threading.Lock`` per key, for an in-memory cache: single flight within this process."""

    def __init__(self, full: str):
        with _thread_locks_guard:
            self._lock = _thread_locks.setdefault(full, threading.Lock())

    def acquire(self, timeout: float = 0.0) -> bool:
        return self._lock.acquire(timeout=timeout) if timeout > 0 else self._lock.acquire(blocking=False)

    def release(self) -> None:
        self._lock.release()


class _AdvisoryLock:
    """A PostgreSQL session advisory lock on the key: single flight across workers and containers. A
    worker that dies closes its connection, which releases the lock."""

    def __init__(self, full: str):
        self._ids = _lock_ids(full)

    def acquire(self, timeout: float = 0.0) -> bool:
        if timeout > 0:
            time.sleep(timeout)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s::int4, %s::int4)", list(self._ids))
            return bool(cursor.fetchone()[0])

    def release(self) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s::int4, %s::int4)", list(self._ids))
        except DatabaseError:  # a broken connection: the lock went with its session
            logger.warning("compute: could not release an advisory lock", exc_info=True)


# -------------------------------------------------------------------------------------------------- once
def once[T](
    key: str, compute: Callable[[], T], ttl: int, *, wait: float = 30.0, lang_neutral: bool = False
) -> T:
    """The value stored under ``key`` in the shared cache; otherwise ``compute()``, run by one request at
    a time across every worker, stored for ``ttl`` seconds and returned.

    The value is kept for the rest of the request too, and the same object is returned each time: never
    change it in place. ``wait`` is how long a request waits for another one computing the same key
    before computing it itself (in a part request: before raising :class:`Pending`).

    Inside ``transaction.atomic`` with the database cache (the read-only tool runs, an admin action), a
    stored value is read as anywhere else, but a missing one is computed for this caller (and the request's
    memo) only: no lock, which would be released before the value commits, and nothing stored, which a
    rollback would lose and a READ ONLY transaction cannot write. Nothing is deleted there either (see
    :func:`_read_in_transaction`).
    """
    full = _full_key(key, lang_neutral)
    memo = _memo()
    if memo is not None and full in memo:
        return memo[full]
    found, value = _read(full)
    if not found:
        value = _compute_once(full, compute, ttl, wait)
    if memo is not None:
        memo[full] = value
    return value


def _compute_once[T](full: str, compute: Callable[[], T], ttl: int, wait: float) -> T:
    cache = _cache()
    if _in_transaction(cache):  # computed for this caller only: no lock, nothing stored (see once)
        value = compute()
        if settings.DEBUG or settings.ENV == "test":
            _check_plain(value)
        return value
    if _uses_database(cache):
        lock: _ThreadLock | _AdvisoryLock = _AdvisoryLock(full)
    else:
        lock = _ThreadLock(full)

    in_part = getattr(_local, "part_wait", None)
    budget = min(wait, in_part) if in_part is not None else wait
    deadline = time.monotonic() + budget
    delay = POLL_FIRST
    held = lock.acquire()
    while not held:
        left = deadline - time.monotonic()
        if left <= 0:
            if in_part is not None:
                raise Pending(full)
            logger.info("compute: waited %.1f s for %s; computing it without the lock", budget, full)
            break
        held = lock.acquire(timeout=min(delay, left))
        delay = min(delay * 1.5, POLL_MAX)
        if held:
            break
        found, value = _read(full)  # stored by the request holding the lock
        if found:
            return value
    try:
        if held:
            found, value = _read(full)  # stored while this request was waiting for the lock
            if found:
                return value
        value = compute()
        if settings.DEBUG or settings.ENV == "test":
            _check_plain(value)
        _store(full, value, ttl)
        return value
    finally:
        if held:
            lock.release()


# -------------------------------------------------------------------------------------------- the stamp
def per_request[T](name: str, read: Callable[[], T]) -> T:
    """``read()``, read once per request: the small reads a page's keys are made of (:func:`data_stamp`, the
    last change of a table staff edit), which the frame's :func:`peek` and the part's :func:`once` of the
    same request would otherwise each make. Outside a request it is read each time. The value is never
    stored in the shared cache."""
    memo = _memo()
    entry = ("per_request", name)  # never a cache key: those are strings
    if memo is not None and entry in memo:
        return memo[entry]
    value = read()
    if memo is not None:
        memo[entry] = value
    return value


def data_stamp() -> str:
    """What the synced figures depend on, for the keys of :func:`once`: the last finished sync run (any
    job) and the indicator flags. It changes as soon as a sync finishes or a flag is edited. Read once
    per request."""

    def read() -> str:
        from neurodb.reports.overview import flags_fingerprint, latest_sync_run

        return f"{latest_sync_run() or ''}:{flags_fingerprint()}"

    return per_request("data_stamp", read)


def fingerprint(text: str) -> str:
    """``text`` (what a build depends on besides its scope: :func:`data_stamp`, the last change of a table
    staff edit) as 16 hex digits: short, the same in every process, safe in a key and in an address. A
    page in parts writes its build's fingerprint into the addresses of its parts (``as_of``,
    :func:`once_as_of`). It is keyed with the secret key (an HMAC, as the assistant's safety identifier):
    the text is easy to guess (the next sync run's number, for one), its fingerprint is not, so an address
    can only carry the fingerprint of a moment the server has drawn a page for."""
    return salted_hmac("neurodb.core.compute.fingerprint", text, algorithm="sha256").hexdigest()[:16]


def peek_as_of(
    key_of: Callable[[str], str], as_of: str, now: str, *, lang_neutral: bool = False
) -> tuple[bool, Any]:
    """The build of the moment a page's frame was drawn at, for one of its parts: ``(True, value)`` when
    ``as_of`` (the fingerprint the frame read, which the part's address carries) is not ``now``, the
    fingerprint of the moment, and a value is still stored under ``key_of(as_of)``; ``(False, None)``
    otherwise.

    The parts of a group come one after another: the first reads or computes the build, the others read
    what it read. A sync that finishes in between moves ``now``: the later parts then find the first one's
    build here, under the frame's moment, while it is stored (its time to live). Nothing is computed or
    stored under ``as_of`` here, and the key holds the scope as before, so a crafted ``as_of`` can only pick
    figures stored a moment ago for the same scope (they hold nothing about the person asking). A part
    reads its build through :func:`once_as_of`, which also covers a sync that finished before the first
    part."""
    if not as_of or as_of == now or not _FINGERPRINT.fullmatch(as_of):
        return False, None
    return peek(key_of(as_of), lang_neutral=lang_neutral)


def once_as_of[T](
    key_of: Callable[[str], str],
    as_of: str,
    now: str,
    compute: Callable[[], T],
    ttl: int,
    *,
    lang_neutral: bool = False,
) -> T:
    """The build of a page in parts, for any of its requests: :func:`once` of ``key_of(now)``, ``now`` being
    the :func:`fingerprint` of the moment, unless ``as_of`` (the fingerprint the page's frame read, which
    the address of each of its parts carries) is a well-formed fingerprint other than ``now``. The figures
    have then moved since the frame (a sync run finished, an edit), and every part of that page reads one
    build, whatever moves before or between them:

    1. the build kept for ``as_of`` by the page's first part, when the figures had already moved before
       it (step 3);
    2. else the build of the frame's moment, while it is stored (:func:`peek_as_of`): the first part read
       or computed it before the figures moved;
    3. else the build of the moment (:func:`once` of ``key_of(now)``, read or computed as usual), kept for
       ``ttl`` under ``as_of`` too, so the later parts read it in step 1. One request at a time does this
       for an ``as_of`` (the lock of :func:`once`): a part asked meanwhile waits for it and reads it.

    What is kept under ``as_of`` has a key space of its own, read only here: the frame's look at what is
    stored (:func:`peek`) and every request without a moved ``as_of`` read the figures of their own moment
    as before, whatever an address asked to keep (a fingerprint can also come back, when an edit is
    undone). An address can only carry the fingerprint of a moment the server drew a page for
    (:func:`fingerprint`), and the key holds the scope, so a crafted ``as_of`` only reads or keeps the
    figures of the same scope from a moment ago, which hold nothing about the person asking.
    """
    if not as_of or as_of == now or not _FINGERPRINT.fullmatch(as_of):
        return once(key_of(now), compute, ttl, lang_neutral=lang_neutral)
    kept = _AS_OF + key_of(as_of)
    found, value = peek(kept, lang_neutral=lang_neutral)
    if not found:
        found, value = peek_as_of(key_of, as_of, now, lang_neutral=lang_neutral)
    if found:
        return value
    return once(
        kept,
        lambda: once(key_of(now), compute, ttl, lang_neutral=lang_neutral),
        ttl,
        lang_neutral=lang_neutral,
    )
