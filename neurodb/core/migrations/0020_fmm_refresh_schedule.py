"""The Monitoring insights refresh's schedule: daily 05:25, after the 05:00 locations sync, so that the
overdue action points, the follow-up age and urgency are recomputed every morning even when no data
changed. It also runs after every eTools Datamart sync."""

from django.db import migrations


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="fmm-refresh", defaults={"command": "fmm_refresh", "schedule": "25 5 * * *", "enabled": True}
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0019_fmm_jobs")]

    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
