"""The table of the shared page figures (``CACHES["shared"]``, read and written by ``core.compute.once``).

Created here, so nobody has to run ``createcachetable``: administrators have no shell. The columns are
those Django's ``DatabaseCache`` expects. The table is UNLOGGED: what it holds can be computed again at
any time, so it is not written to the database's log, not replicated, and emptied if the server crashes.
An empty new table: quick at start-up."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0025_etools_update_cleanup")]

    operations = [
        migrations.RunSQL(
            sql=[
                "CREATE UNLOGGED TABLE IF NOT EXISTS neurodb_cache ("
                "cache_key varchar(255) NOT NULL PRIMARY KEY, "
                "value text NOT NULL, "
                "expires timestamp with time zone NOT NULL)",
                "CREATE INDEX IF NOT EXISTS neurodb_cache_expires ON neurodb_cache (expires)",
            ],
            reverse_sql=["DROP TABLE IF EXISTS neurodb_cache"],
        ),
    ]
