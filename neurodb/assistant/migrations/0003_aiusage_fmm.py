from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0002_aiusage"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aiusage",
            name="feature",
            field=models.CharField(
                help_text="ask, review, digest, knowledge, periodic, cpd, watch or fmm", max_length=20
            ),
        ),
    ]
