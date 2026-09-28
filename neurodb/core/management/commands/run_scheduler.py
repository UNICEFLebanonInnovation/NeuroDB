"""Run the scheduled jobs loop in the foreground (the web workers run it in a thread already).

For a local runserver, or a host where the website does not run under gunicorn. Safe next to the
web workers: the same database lock lets only one scheduler act.
"""

from django.core.management.base import BaseCommand

from neurodb.core import scheduler


class Command(BaseCommand):
    help = "Run the in-app scheduler in the foreground (admin → Scheduled jobs); Ctrl-C to stop."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="one pass, then exit")

    def handle(self, *args, **options):
        if options["once"]:
            started = scheduler.tick()
            self.stdout.write(f"started: {', '.join(started) or 'nothing due'}")
            return
        self.stdout.write("Scheduler running; Ctrl-C to stop.")
        scheduler.run_forever()
