"""Apply migrations and default roles under a PostgreSQL advisory lock.

The container runs this before starting the website (RUN_MIGRATIONS=true, the default). When several
containers start at the same time (scale-out, deployment slots, a migrate job), the lock makes one of
them apply the migrations while the others wait, then find nothing left to do.
"""

from __future__ import annotations

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

LOCK_ID = 7140427  # any constant; identical in every NeuroDB container


class Command(BaseCommand):
    help = "migrate + bootstrap_roles + link_partners, one container at a time"

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
        call_command("migrate", interactive=False, verbosity=verbosity)
        call_command("bootstrap_roles", verbosity=verbosity)
        # the ActivityInfo → eTools partner links exist from the first start, not from the first sync
        call_command("link_partners", "--triggered-by", "migrate", verbosity=verbosity)
