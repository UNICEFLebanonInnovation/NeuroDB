"""The label of a morning note's "credit ran out" reason names no provider: the AI may be Claude or OpenAI.
A choice label only: no code runs, nothing in the database changes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("watch", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="watchnote",
            name="ai_skipped_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "AI used"),
                    ("off", "AI switched off"),
                    ("budget", "Daily limit reached"),
                    ("paused", "AI paused"),
                    ("quota", "The AI service's credit ran out"),
                    ("error", "AI did not answer"),
                    ("no_change", "Nothing changed"),
                ],
                default="",
                max_length=16,
            ),
        ),
    ]
