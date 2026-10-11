"""The figures the Monitoring insights pages add up, read faster (the per-record pages at production size).

- The figures kept per visit (``tallies``): each visit's records added up (``VisitFigures``), its failed
  checks per issue (``VisitIssueTally``) and its checks per rule and result (``VisitRuleTally``, also
  summed per day: ``DayRuleTally``), written by the refresh with the records and checks; ``FigureState``
  says whether they are those of the stored records and checks. A trigger marks them out of date on any
  change to the records (``fmm_visitentity``), their checks (``fmm_recordruleresult``) or the visits'
  dates (``fmm_visit``), whatever makes it: the pages then add up the records and checks themselves until
  the next refresh writes the figures again. Nothing is written here: the figures are out of date (no
  ``FigureState`` row) until the first refresh.
- Covering indexes for the figures a page still reads from the records and checks (a record filter): the
  records' figures (``fmm_record_figures``, ``fmm_record_categories``), the checks' points
  (``fmm_result_points``) and flags (``fmm_result_flags``); and for a period's visits
  (``fmm_visit_period``, which takes the place of the index on the visit date alone) and the data notes
  (``fmm_visit_notes``)."""

import django.db.models.deletion
from django.db import migrations, models

# Any change to the records or their checks, or to the visits' dates (the rule figures kept per day), marks
# the figures kept per visit out of date (once per statement, whatever the rows); ``tallies.rebuild``
# marks them as theirs again
OUT_OF_DATE = """
CREATE FUNCTION fmm_figures_out_of_date() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE fmm_figurestate SET valid = false WHERE valid;
    RETURN NULL;
END
$$;
CREATE TRIGGER fmm_records_change_figures
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON fmm_visitentity
    FOR EACH STATEMENT EXECUTE FUNCTION fmm_figures_out_of_date();
CREATE TRIGGER fmm_checks_change_figures
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON fmm_recordruleresult
    FOR EACH STATEMENT EXECUTE FUNCTION fmm_figures_out_of_date();
CREATE TRIGGER fmm_visits_change_figures
    AFTER INSERT OR UPDATE OF visit_date OR DELETE OR TRUNCATE ON fmm_visit
    FOR EACH STATEMENT EXECUTE FUNCTION fmm_figures_out_of_date();
"""
NO_TRIGGER = """
DROP TRIGGER IF EXISTS fmm_visits_change_figures ON fmm_visit;
DROP TRIGGER IF EXISTS fmm_checks_change_figures ON fmm_recordruleresult;
DROP TRIGGER IF EXISTS fmm_records_change_figures ON fmm_visitentity;
DROP FUNCTION IF EXISTS fmm_figures_out_of_date();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0008_monitoringfinding_pd_link"),
        ("etools", "0003_partnerlink"),
        ("fmm", "0020_records_cleanup"),
        ("locations", "0002_location_tables_when_missing"),
    ]

    operations = [
        migrations.CreateModel(
            name="DayRuleTally",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("day", models.DateField(db_index=True)),
                ("rule", models.CharField(max_length=4)),
                ("status", models.CharField(max_length=8)),
                ("records", models.PositiveIntegerField()),
                (
                    "earned",
                    models.DecimalField(blank=True, decimal_places=1, max_digits=11, null=True),
                ),
                (
                    "points_max",
                    models.DecimalField(blank=True, decimal_places=1, max_digits=11, null=True),
                ),
                ("points_n", models.PositiveIntegerField()),
            ],
            options={
                "verbose_name": "day rule tally",
                "verbose_name_plural": "day rule tallies",
            },
        ),
        migrations.CreateModel(
            name="FigureState",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False),
                ),
                ("valid", models.BooleanField(default=False)),
                ("written_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "figure state",
                "verbose_name_plural": "figure state",
            },
        ),
        migrations.CreateModel(
            name="VisitFigures",
            fields=[
                (
                    "visit",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="+",
                        serialize=False,
                        to="fmm.visit",
                    ),
                ),
                ("records", models.PositiveIntegerField()),
                (
                    "q_sum",
                    models.DecimalField(blank=True, decimal_places=1, max_digits=9, null=True),
                ),
                ("q_n", models.PositiveIntegerField()),
                ("high", models.PositiveIntegerField()),
                ("medium", models.PositiveIntegerField()),
                ("low", models.PositiveIntegerField()),
                ("on_track", models.PositiveIntegerField()),
                ("constrained", models.PositiveIntegerField()),
                ("off_track", models.PositiveIntegerField()),
                ("unrated", models.PositiveIntegerField()),
                ("flagged", models.PositiveIntegerField()),
                ("scored_flags", models.TextField(blank=True, null=True)),
                ("unscored_flags", models.TextField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "visit figures",
                "verbose_name_plural": "visit figures",
            },
        ),
        migrations.CreateModel(
            name="VisitIssueTally",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("rule", models.CharField(max_length=4)),
                ("detail_key", models.CharField(blank=True, max_length=40)),
                ("records", models.PositiveIntegerField()),
                ("urgency_sum", models.BigIntegerField(blank=True, null=True)),
                ("urgency_n", models.PositiveIntegerField()),
                (
                    "urgency_max",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("low", models.FloatField(blank=True, null=True)),
                ("high", models.FloatField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "visit issue tally",
                "verbose_name_plural": "visit issue tallies",
            },
        ),
        migrations.CreateModel(
            name="VisitRuleTally",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("rule", models.CharField(max_length=4)),
                ("status", models.CharField(max_length=8)),
                ("records", models.PositiveIntegerField()),
                (
                    "earned",
                    models.DecimalField(blank=True, decimal_places=1, max_digits=9, null=True),
                ),
                (
                    "points_max",
                    models.DecimalField(blank=True, decimal_places=1, max_digits=9, null=True),
                ),
                ("points_n", models.PositiveIntegerField()),
            ],
            options={
                "verbose_name": "visit rule tally",
                "verbose_name_plural": "visit rule tallies",
            },
        ),
        migrations.AlterField(
            model_name="visit",
            name="visit_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="recordruleresult",
            index=models.Index(
                fields=["entity"],
                include=("rule", "status", "points", "max_points"),
                name="fmm_result_points",
            ),
        ),
        migrations.AddIndex(
            model_name="recordruleresult",
            index=models.Index(
                condition=models.Q(("status", "fail")),
                fields=["entity"],
                include=("rule", "detail_key", "measure"),
                name="fmm_result_flags",
            ),
        ),
        migrations.AddIndex(
            model_name="visit",
            index=models.Index(
                fields=["visit_date"],
                include=(
                    "id",
                    "status_group",
                    "status",
                    "quality_score",
                    "urgency",
                    "rules_version",
                    "modality",
                    "governorate_key",
                    "governorate_name",
                ),
                name="fmm_visit_period",
            ),
        ),
        migrations.AddIndex(
            model_name="visit",
            index=models.Index(
                fields=["visit_date"],
                include=(
                    "id",
                    "start_date",
                    "end_date",
                    "location",
                    "site",
                    "reference",
                ),
                name="fmm_visit_notes",
            ),
        ),
        migrations.AddIndex(
            model_name="visitentity",
            index=models.Index(
                fields=["visit"],
                include=(
                    "id",
                    "quality_score",
                    "score_band",
                    "rating",
                    "hact_q1",
                    "flags",
                    "flag_count",
                    "urgency",
                    "kind",
                    "partner",
                ),
                name="fmm_record_figures",
            ),
        ),
        migrations.AddIndex(
            model_name="visitentity",
            index=models.Index(
                condition=models.Q(("quality_score__isnull", False)),
                fields=["visit"],
                include=("id", "category_deductions"),
                name="fmm_record_categories",
            ),
        ),
        migrations.AddField(
            model_name="visitissuetally",
            name="visit",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="fmm.visit",
            ),
        ),
        migrations.AddField(
            model_name="visitruletally",
            name="visit",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="fmm.visit",
            ),
        ),
        migrations.RunSQL(OUT_OF_DATE, NO_TRIGGER),
    ]
