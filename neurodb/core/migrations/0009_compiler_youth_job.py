"""The Compiler youth figures job, and its schedule (off until Compiler is configured)."""

from django.db import migrations, models


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="compiler-youth", defaults={"command": "compiler_youth", "schedule": "0 21 * * *", "enabled": False}
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0008_syncrun_verbose_name")]

    operations = [
        migrations.AlterField(
            model_name="syncrun",
            name="job",
            field=models.CharField(
                choices=[
                    ("ai_structure", "ActivityInfo structure import"),
                    ("ai_data", "ActivityInfo data import"),
                    ("etools", "eTools sync"),
                    ("etools_datamart", "eTools Datamart sync"),
                    ("locations", "Locations sync"),
                    ("population", "Population figures load"),
                    ("partner_links", "ActivityInfo partner links"),
                    ("daily_review", "Daily AI review"),
                    ("compiler_youth", "Compiler youth figures"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
