"""The year-end forecast job, and its schedule: Mondays 07:15, after the weekend's imports."""

from django.db import migrations, models


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="forecast", defaults={"command": "forecast", "schedule": "15 7 * * 1", "enabled": True}
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_ml_readiness_job"),
    ]

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
                    ("compiler_education", "Compiler education figures"),
                    ("compiler_wellbeing", "Compiler Makani wellbeing flags"),
                    ("knowledge_hub", "Knowledge hub"),
                    ("whats_new", "What's new note"),
                    ("ml_readiness", "Machine learning readiness check"),
                    ("forecast", "Year-end indicator forecast"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
