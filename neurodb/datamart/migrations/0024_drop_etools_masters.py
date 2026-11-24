"""Removes NeuroDB's own grouping of PD indicators from the models (the owner's decision: eTools has no such
grouping): the grouping, its yearly settings and its PD indicators (models of datamart 0016).

State only: no SQL runs. The three tables (``datamart_etoolsmaster``, ``datamart_etoolsmasteryear``,
``datamart_etoolsmasterlink``), their rows and their content types and permissions stay one release, so an
image of the previous release that is rolled back to works with its data. A later release drops them
(``DROP TABLE IF EXISTS`` of the three, then their content types). Nothing reads them any more, and no table
of eTools data points to them (the PD column of the links had no database constraint)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0023_page_choices"),
        ("core", "0036_drop_etools_masters_job"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="EtoolsMasterLink"),  # it points to the grouping: first
                migrations.DeleteModel(name="EtoolsMasterYear"),  # likewise
                migrations.DeleteModel(name="EtoolsMaster"),
            ],
            database_operations=[],
        ),
    ]
