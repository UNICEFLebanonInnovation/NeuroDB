"""The machine learning readiness check runs monthly: on the 1st of each month at 06:45, Beirut time, so that
it is checked again without anyone remembering and *Since the last check* on Data health compares one month
with the next.

Core 0014 made its schedule ``ml-readiness`` weekly (Mondays 06:45). That schedule becomes monthly when it still
reads as 0014 wrote it; one an administrator changed (another time, or switched off) is kept as it is. With no
schedule for the check at all, one is made. Never a second one. The next run is planned by the scheduler's next
pass (``next_run_at`` cleared, as an edit in the admin does): nothing starts at once. One row, so quick.
"""

from django.db import migrations

KEY = "ml-readiness"
COMMAND = "ml_readiness"
WEEKLY = "45 6 * * 1"  # core 0014's
MONTHLY = "45 6 1 * *"


def monthly(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    jobs = ScheduledJob.objects.filter(command=COMMAND)
    if not jobs.exists():
        if not ScheduledJob.objects.filter(key=KEY).exists():
            ScheduledJob.objects.create(key=KEY, command=COMMAND, schedule=MONTHLY, enabled=True)
        return
    jobs.filter(schedule=WEEKLY, enabled=True).update(schedule=MONTHLY, next_run_at=None)


class Migration(migrations.Migration):
    dependencies = [("core", "0033_etools_rest_raw_job")]

    operations = [migrations.RunPython(monthly, migrations.RunPython.noop)]
