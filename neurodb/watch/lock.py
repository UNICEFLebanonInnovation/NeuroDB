"""One run of NeuroDB Watch at a time, whoever started it (the schedule, new data, the admin, a shell).

A PostgreSQL advisory lock held by the run's own database session: it goes with the process, so a run
cut off by a restart never blocks the next one (``background.is_running`` closes its row).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection

LOCK_ID = 7_140_431  # neurodb.integrations.background.WATCH_LOCK_ID


@contextmanager
def hold() -> Iterator[bool]:
    """Take the lock without waiting: yields False when another run holds it (then nothing is done)."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
