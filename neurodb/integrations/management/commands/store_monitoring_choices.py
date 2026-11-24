"""``store_monitoring_choices [--wait]``: compute Partner monitoring's filter choices (its sections, partners,
programme documents, places, years, statuses and tags) and store them for the page's frames and its map
(``monitoring.store_options``), which read them and never the PD indicator rows. The eTools jobs that write
what the choices read store them as they go; this stores them at any time: right after a deployment, or after
a change no job made. Started from *Run a job → Store the partner monitoring filter choices* and by *Empty
the stored page figures*, both with ``--wait``, or by a schedule.

One at a time (``background.PAGE_CHOICES_LOCK_ID``): started while another is storing them, it does
nothing, or with ``--wait`` (the buttons) waits for that one to end (up to :data:`WAIT_SECONDS`), then reads
them again: the running one may have read before the change wanted. One run waits at a time
(``background.PAGE_CHOICES_WAIT_LOCK_ID``, given up just before its read begins): a second one started
meanwhile does nothing, since the waiting one's read begins after it. Never stores older names over newer
ones (``monitoring.store_options``: the read begun last is kept)."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import connection

from neurodb.datamart import monitoring
from neurodb.integrations.background import PAGE_CHOICES_LOCK_ID, PAGE_CHOICES_WAIT_LOCK_ID
from neurodb.integrations.management.commands._base import add_triggered_by

ALREADY_RUNNING = "Partner monitoring's filter choices are already being stored; nothing to do."
ALREADY_WAITING = (
    "A run is already waiting for the one storing Partner monitoring's filter choices and reads them once "
    "it ends; nothing to do."
)
WAITED_TOO_LONG = (
    "Partner monitoring's filter choices were still being stored after 15 minutes; nothing done."
)
WAIT_SECONDS = 15 * 60
POLL_SECONDS = 5


class Command(BaseCommand):
    help = "Store Partner monitoring's filter choices for its frames (they never read the PD indicator rows)"

    def add_arguments(self, parser):
        add_triggered_by(parser)  # accepted as by every job; nothing records a run
        parser.add_argument(
            "--wait",
            action="store_true",
            help="when another run is storing them, wait for it to end, then read them again (the buttons)",
        )

    def handle(self, *args, **options):
        if options["wait"]:
            if not _take(PAGE_CHOICES_WAIT_LOCK_ID):
                self.stdout.write(ALREADY_WAITING)
                return
            try:
                taken = _wait_for(PAGE_CHOICES_LOCK_ID)
            finally:
                _release(PAGE_CHOICES_WAIT_LOCK_ID)  # its read begins now: a run asked from here waits again
            if not taken:
                self.stdout.write(WAITED_TOO_LONG)
                return
        elif not _take(PAGE_CHOICES_LOCK_ID):
            self.stdout.write(ALREADY_RUNNING)
            return
        try:
            found = monitoring.store_options()
        finally:
            _release(PAGE_CHOICES_LOCK_ID)
        if found is None:
            self.stdout.write(monitoring.LATER_READ)
            return
        self.stdout.write(
            f"Partner monitoring filter choices stored: {len(found['sections'])} sections, "
            f"{len(found['partners'])} partners, {len(found['pds'])} programme documents, "
            f"{len(found['locations'])} places, {len(found['years'])} years."
        )


def _wait_for(lock_id: int) -> bool:
    """Take ``lock_id``, trying every :data:`POLL_SECONDS` for at most :data:`WAIT_SECONDS`."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not _take(lock_id):
        if time.monotonic() >= deadline:
            return False
        _pause()
    return True


def _pause() -> None:
    time.sleep(POLL_SECONDS)


def _take(lock_id: int) -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
        return bool(cursor.fetchone()[0])


def _release(lock_id: int) -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
