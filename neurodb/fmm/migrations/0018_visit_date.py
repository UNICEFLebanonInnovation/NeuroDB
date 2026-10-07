"""Release 2 step 5 (stage F1): periods read the visit's start date.

``Visit.visit_date`` is the start date, else the end date when eTools left the start blank. It is
filled here for the visits already built and set by the build from then on. No rules version change:
periods are not scoring."""

from django.db import migrations, models
from django.db.models.functions import Coalesce


def fill_visit_date(apps, schema_editor):
    Visit = apps.get_model("fmm", "Visit")
    Visit.objects.update(visit_date=Coalesce("start_date", "end_date"))


class Migration(migrations.Migration):
    dependencies = [
        ("fmm", "0017_powerbi_key_hour"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="visit",
            options={
                "ordering": (
                    models.OrderBy(models.F("urgency"), descending=True, nulls_last=True),
                    models.OrderBy(models.F("visit_date"), descending=True, nulls_last=True),
                ),
                "verbose_name": "visit",
                "verbose_name_plural": "visits",
            },
        ),
        migrations.RemoveIndex(
            model_name="visit",
            name="fmm_visit_end_dat_d0c4c7_idx",
        ),
        migrations.AddField(
            model_name="visit",
            name="visit_date",
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name="visit",
            index=models.Index(fields=["visit_date", "status_group"], name="fmm_visit_visit_d_9e28af_idx"),
        ),
        migrations.RunPython(fill_visit_date, migrations.RunPython.noop),
    ]
