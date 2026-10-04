"""NeuroDB Watch (the "For you" page): its job, and its schedule: daily 07:45, after the daily review
(06:00), the knowledge hub build (07:00) and the What's new note (07:30)."""

from django.db import migrations, models


def create(apps, schema_editor):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    ScheduledJob.objects.get_or_create(
        key="watch", defaults={"command": "watch", "schedule": "45 7 * * *", "enabled": True}
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_compiler_runs_schedule"),
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
                    ("watch", "NeuroDB Watch"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.RunPython(create, migrations.RunPython.noop),
    ]
