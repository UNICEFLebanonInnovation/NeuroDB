"""What the knowledge search ranks on, stored with each passage (``Chunk.rank_vector``).

The search has always ranked a passage on its stored index turned into text and parsed again in English
(``SearchRank("search_vector", ...)``): the stored forms of one word fold into one English stem, counted
once per stored form. Parsing it again for every matching passage took 1.6-1.8 s for one common word over
3,200 passages. The column keeps that same parse with the passage, so the search reads it and keeps its
results, their order and their ranks.

The column is added empty: adding a column without a default is a change of the table's description only,
whatever the number of passages, so the site starts as fast as before. Indexing writes it from now on; the
passages indexed before are filled by the knowledge hub job (``indexing.fill_rank_vectors``), which runs
after every sync, every morning and from *Run a job*; until then the search parses them as it always did.
The wait for the table's lock (an indexing job writing its passages) is bounded to 10 seconds, after which
the migration fails and the deployment can be retried (as core 0027)."""

import django.contrib.postgres.search
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0006_review_paragraphs")]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '10s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="chunk",
            name="rank_vector",
            field=django.contrib.postgres.search.SearchVectorField(editable=False, null=True),
        ),
    ]
