"""The AI monitoring briefs' schedule: daily 05:40, after the 05:25 Monitoring insights refresh, so the
morning briefs read the visits scored that morning. With the AI switched off (FMM_AI) the run writes
nothing and only applies the retention of the briefs."""

from django.db import migrations


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="fmm-insights", defaults={"command": "fmm_insights", "schedule": "40 5 * * *", "enabled": True}
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0020_fmm_refresh_schedule")]

    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
