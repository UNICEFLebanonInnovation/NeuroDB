"""The youth figures of every year together, in a table of their own (YouthAllYears): a NeuroDB release
before this one reads every YouthFigures row as a reporting year, so the "all" row must never be there."""

from django.db import migrations, models


def move_all_years(apps, schema_editor):
    """At most one row: the "all" figures a build of this branch may have kept in YouthFigures."""
    YouthFigures = apps.get_model("youth", "YouthFigures")
    YouthAllYears = apps.get_model("youth", "YouthAllYears")
    for row in YouthFigures.objects.filter(year="all"):
        YouthAllYears.objects.update_or_create(
            year="all", defaults={"fetched_at": row.fetched_at, "payload": row.payload}
        )
        row.delete()


class Migration(migrations.Migration):
    dependencies = [("youth", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="YouthAllYears",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("year", models.CharField(default="all", editable=False, max_length=16, unique=True)),
                ("fetched_at", models.DateTimeField()),
                (
                    "payload",
                    models.JSONField(help_text="counts of young people per grouping, as Compiler sent them"),
                ),
            ],
            options={
                "verbose_name": "youth figures of all years",
                "verbose_name_plural": "youth figures of all years",
            },
        ),
        migrations.RunPython(move_all_years, migrations.RunPython.noop),
    ]
