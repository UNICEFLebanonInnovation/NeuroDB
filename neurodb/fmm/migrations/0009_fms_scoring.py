"""Release 2 scoring (FMS): the scored statuses and recency window of the score settings, a visit's
urgency empty when it has no score, and its follow-up and late-report signals.

The score settings' urgency weights become FMS's (0.5 the quality gap, 0.3 recency, 0.2 red flags): the
Release 1 weights (points per part) mean nothing in the new formula. The visits keep their Release 1
urgency until the next refresh rescores them (Run a job → Monitoring insights refresh, or the morning
schedule). The reverse leaves the weights as they are."""

import django.core.validators
from django.db import migrations, models

import neurodb.fmm.models

FMS_WEIGHTS = {"quality_gap": 0.5, "recency": 0.3, "red_flags": 0.2}


def fms_weights(apps, schema_editor):
    ScoreSetting = apps.get_model("fmm", "ScoreSetting")
    for setting in ScoreSetting.objects.all():
        if set(setting.urgency_weights or {}) != set(FMS_WEIGHTS):
            setting.urgency_weights = dict(FMS_WEIGHTS)
            setting.save(update_fields=["urgency_weights"])


class Migration(migrations.Migration):

    dependencies = [
        ("fmm", "0008_fms_export_fields"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="visit",
            options={
                "ordering": (
                    models.OrderBy(
                        models.F("urgency"), descending=True, nulls_last=True
                    ),
                    models.OrderBy(
                        models.F("end_date"), descending=True, nulls_last=True
                    ),
                ),
                "verbose_name": "visit",
                "verbose_name_plural": "visits",
            },
        ),
        migrations.AddField(
            model_name="scoresetting",
            name="recency_days",
            field=models.PositiveSmallIntegerField(
                default=180,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
            ),
        ),
        migrations.AddField(
            model_name="scoresetting",
            name="scored_statuses",
            field=models.JSONField(default=neurodb.fmm.models.default_scored_statuses),
        ),
        migrations.AddField(
            model_name="visit",
            name="signals",
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name="visit",
            name="urgency",
            field=models.PositiveSmallIntegerField(
                blank=True, db_index=True, null=True
            ),
        ),
        migrations.RunPython(fms_weights, migrations.RunPython.noop),
    ]
