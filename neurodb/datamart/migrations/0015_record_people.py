"""The person names the eTools records hold (``datamart.query.datamart_people``), kept in a table of their
own instead of the page-figure cache, where a cull or the Empty button could remove them. A new, empty
table: the next eTools Datamart sync fills it, and until then Ask reads the latest records."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0014_visit_plan_comment"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecordPeople",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("names", models.JSONField(default=list)),
                ("read_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "names the records hold",
                "verbose_name_plural": "names the records hold",
            },
        ),
    ]
