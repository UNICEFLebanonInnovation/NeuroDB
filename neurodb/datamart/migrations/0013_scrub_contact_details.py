"""Formerly: remove the contact details earlier syncs stored (privacy).

The sync no longer stores them: the typed eTools tables keep each record without the keys naming an
e-mail address, a phone or a mobile, a UNICEF focal point eTools knows only by an e-mail address is
not kept, and a progress report's ``submitted_by`` is kept only when it is a name. Removing what
earlier syncs stored no longer runs here: it reads every record of every eTools table, which on the
real tables did not fit in App Service's start-up limit (see 0010). The ``datamart_backfill`` job
(``neurodb.datamart.backfill``) does it after the site has started: core migration 0025 schedules it
to start at once. Kept, without operations, so that every database shares one migration history."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0012_disaggregation_labels"),
        ("etools", "0003_partnerlink"),
    ]

    operations = []
