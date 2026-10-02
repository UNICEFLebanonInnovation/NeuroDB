"""The Compiler (BMA) Makani wellbeing job's schedule: daily 04:00, an hour after BMA works out the
flags (03:00); off until Compiler is configured, like the other Compiler jobs."""

from django.db import migrations


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="compiler-wellbeing",
        defaults={"command": "compiler_wellbeing", "schedule": "0 4 * * *", "enabled": False},
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0015_forecast_job")]

    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
