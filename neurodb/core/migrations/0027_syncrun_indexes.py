"""Two indexes over the sync runs: the last finished run (in the cache keys of the overview, the brief and
the shared page figures; read with no index, it scanned every run) and the latest run of each job and
target (Data health).

A plain CREATE INDEX in this migration's transaction, not CREATE INDEX CONCURRENTLY: a concurrent build
waits for every older transaction in the whole database, and a long job still running in the previous
container during a deployment would hold this container's start-up past App Service's limit. The table
holds a few thousand runs, so each build takes milliseconds; the wait for the table's lock (a job writing
its run) is bounded to 10 seconds, after which the migration fails and the deployment can be retried."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0026_shared_cache_table")]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '10s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddIndex(
            model_name="syncrun",
            index=models.Index(
                condition=models.Q(("finished_at__isnull", False)),
                fields=["-finished_at", "-id"],
                name="core_syncrun_finished",
            ),
        ),
        migrations.AddIndex(
            model_name="syncrun",
            index=models.Index(fields=["job", "target", "-started_at"], name="core_syncrun_job_target"),
        ),
    ]
