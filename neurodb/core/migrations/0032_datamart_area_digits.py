"""The admin's PRP business area code accepts digits only: its validator and help text. Schema state only: no
SQL, no code runs."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_etools_masters_job"),
    ]

    operations = [
        migrations.AlterField(
            model_name="datamartsetting",
            name="prp_business_area",
            field=models.CharField(
                blank=True,
                help_text="The country office's business area code (digits, as 2490), which the eTools Datamart sync sends to PRP's indicator reports, programme documents and progress reports (they carry no country name). A code entered here comes first, before one the hosting team set (ETOOLS_DATAMART_BUSINESS_AREA). Leave it empty to let NeuroDB find it: in the Datamart workspace it holds, else in the Datamart's list of workspaces, else on the partner report rows. It applies from the next sync of those datasets; Run a job → Probe a Datamart dataset shows first what the Datamart holds for it.",
                max_length=20,
                validators=[
                    django.core.validators.RegexValidator(
                        "^[0-9]+$",
                        "Digits only, as in 2490: the business area code, not the country's name.",
                    )
                ],
                verbose_name="PRP business area code",
            ),
        ),
    ]
