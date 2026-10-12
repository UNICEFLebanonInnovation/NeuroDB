"""Apply migrations and default roles under a PostgreSQL advisory lock.

The container runs this before starting the website (RUN_MIGRATIONS=true, the default). When several
containers start at the same time (scale-out, deployment slots, a migrate job), the lock makes one of
them apply the migrations while the others wait, then find nothing left to do.
"""

from __future__ import annotations

import io
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

LOCK_ID = 7140427  # any constant; identical in every NeuroDB container


class WholeLines(io.TextIOBase):
    """Ends every write with a line break. ``migrate`` writes "Applying <migration>..." and ends that
    line only when the migration is done; a container log shows a line once it ends, so a long
    migration would show only when it finished, or when the container was stopped (App Service's
    start-up limit), stamped with that time instead of the time it began."""

    def __init__(self, out):
        self._out = out

    def write(self, text: str) -> int:
        self._out.write(text if text.endswith("\n") else f"{text}\n")
        self._out.flush()
        return len(text)

    def flush(self) -> None:
        self._out.flush()


class Command(BaseCommand):
    help = "migrate + ensure_legacy_tables + bootstrap_roles + link_partners, one container at a time"

    def add_arguments(self, parser):
        parser.add_argument(
            "--wait", type=int, default=600, help="seconds to wait for another container's migration"
        )

    def handle(self, *args, **options):
        verbosity = options["verbosity"]
        if connection.vendor != "postgresql":
            self._migrate(verbosity)
            return
        deadline = time.monotonic() + options["wait"]
        with connection.cursor() as cursor:
            while True:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
                if cursor.fetchone()[0]:
                    break
                if time.monotonic() > deadline:
                    raise CommandError(
                        f"another container held the migration lock for more than {options['wait']} s"
                    )
                self.stdout.write("Waiting for another container to finish migrating...")
                time.sleep(3)
        try:
            self._migrate(verbosity)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])

    def _migrate(self, verbosity: int) -> None:
        call_command("migrate", interactive=False, verbosity=verbosity, stdout=WholeLines(self.stdout))
        call_command("ensure_legacy_tables", verbosity=verbosity)  # a v2 table the database never had
        call_command("bootstrap_roles", verbosity=verbosity)
        # the ActivityInfo → eTools partner links exist from the first start, not from the first sync
        call_command("link_partners", "--triggered-by", "migrate", verbosity=verbosity)
