"""The authors' surnames a version's texts also write as ordinary words, worked out by its load: a column with
a default on the table of versions (AddField only, no code runs; the next load fills it)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("km", "0003_question"),
    ]

    operations = [
        migrations.AddField(
            model_name="kmversion",
            name="ordinary_words",
            field=models.JSONField(blank=True, default=list, verbose_name="surnames also written as ordinary words"),
        ),
    ]
