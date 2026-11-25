"""The words a follow-up of Ask the KM Database searched as its own subject, when none joined the earlier
one: a column with a constant database default on the table of questions (catalogue only on PostgreSQL;
AddField only, no code runs; the next answered follow-up fills it)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("km", "0004_ordinary_words"),
    ]

    operations = [
        migrations.AddField(
            model_name="kmquestion",
            name="new_subject",
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text="a follow-up's own words, searched as its subject when none joined the earlier one",
                verbose_name="new subject searched",
            ),
        ),
    ]
