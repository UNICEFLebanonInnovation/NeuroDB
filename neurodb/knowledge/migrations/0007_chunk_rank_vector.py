"""What the knowledge search ranks on, stored with each passage (``Chunk.rank_vector``).

The search has always ranked a passage on its stored index turned into text and parsed again in English
(``SearchRank("search_vector", ...)``): the stored forms of one word fold into one English stem, counted
once per stored form. Parsing it again for every matching passage took 1.6-1.8 s for one common word over
3,200 passages. The column holds that same parse, kept by the database whenever the index is written (a
generated column), so the search reads it and keeps its results, their order and their ranks.

Adding the column computes it once for every passage while the table is locked: about half a millisecond
a passage (1.6 seconds for the 3,296 passages of the x20 parity world, 17 seconds for ten times as many).
The wait for that lock (an indexing job writing its passages) is bounded to 10 seconds, after which the
migration fails and the deployment can be retried (as core 0027)."""

import django.contrib.postgres.search
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("knowledge", "0006_review_paragraphs")]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '10s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="chunk",
            name="rank_vector",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.contrib.postgres.search.SearchVector("search_vector", config="english"),
                output_field=django.contrib.postgres.search.SearchVectorField(),
            ),
        ),
    ]
