"""The schedules the Container Apps jobs used (there in UTC), now in Beirut time and editable."""

from django.db import migrations

DEFAULTS = [
    # key, command, schedule (Beirut time), enabled
    ("locations", "locations", "0 5 * * *", True),
    ("daily-review", "daily_review", "0 6 * * *", True),
    ("activityinfo-data", "ai_data", "0 18 1-22 * *", True),
    ("etools-datamart", "etools_datamart", "30 20 * * *", True),
    ("freshness", "freshness", "15 * * * *", True),
    ("activityinfo-structure", "ai_structure", "0 4 * * 0", False),
]


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    for key, command, schedule, enabled in DEFAULTS:
        ScheduledJob.objects.get_or_create(
            key=key, defaults={"command": command, "schedule": schedule, "enabled": enabled}
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0006_scheduled_jobs")]
    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
