"""Release 2 step 5 (stage F2): each record (an entity row of a visit) is scored, flagged and given an
urgency on its own, as FMS does; a visit's quality is the mean of its records'.

- ``VisitEntity`` (a record) gains its row's place, the derived columns worked out over the answers that
  apply to it, and its own score fields; ``datamart_id`` is unique (the stable record id).
- ``RecordRuleResult``: each record's rule results (``VisitRuleResult`` is derived from them meanwhile).
- ``AICheckAnswer``: the AI checks' answers kept by what was sent (rule, prompt and inputs), so identical
  inputs share one answer; ``VisitAICheck`` stays until the AI checks job has carried its verdicts over.
- ``Visit`` gains ``lowest_score``, ``records_scored`` and ``records_low``; ``LocalActionPoint`` the
  record that made an automatic one.

When visits exist, the latest rules version is copied as a new one ("Scores per record", so every visit
shows "recomputing with rules vN" until it is rescored) and a full refresh is asked for (the row columns
are worked out by the build): the nightly refresh, or Run a job → Refresh monitoring insights, serves it.
A database without visits has no score to recompute: its first refresh is a full one anyway. Running it
again changes nothing; the reverse leaves the rows."""

import django.contrib.postgres.fields
import django.contrib.postgres.indexes
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone

NOTE = "Scores per record (entity row), as FMS: Release 2 step 5"
BY = "NeuroDB"


def new_rules_version(apps, schema_editor):
    """The latest rules version copied as a new one, so the visits scored per visit are rescored."""
    Visit = apps.get_model("fmm", "Visit")
    RuleSetVersion = apps.get_model("fmm", "RuleSetVersion")
    if not Visit.objects.exists() or RuleSetVersion.objects.filter(note=NOTE).exists():
        return
    latest = RuleSetVersion.objects.order_by("-number").first()
    if latest is None:
        return
    RuleSetVersion.objects.create(
        number=latest.number + 1, snapshot=latest.snapshot, note=NOTE, created_by_name=BY
    )


def ask_full_refresh(apps, schema_editor):
    """A full refresh asked for: the records' row columns are worked out by the build."""
    Visit = apps.get_model("fmm", "Visit")
    RefreshRequest = apps.get_model("fmm", "RefreshRequest")
    if not Visit.objects.exists():
        return
    row, _created = RefreshRequest.objects.get_or_create(pk=1)
    row.full_requested_at = timezone.now()
    row.requested_by = "migration:fmm.0019"
    row.save(update_fields=["full_requested_at", "requested_by"])


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0008_monitoringfinding_pd_link"),
        ("etools", "0003_partnerlink"),
        ("fmm", "0018_visit_date"),
        ("locations", "0002_location_tables_when_missing"),
    ]

    operations = [
        migrations.CreateModel(
            name="AICheckAnswer",
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
                ("prompt_hash", models.CharField(max_length=64)),
                ("input_hash", models.CharField(max_length=64)),
                ("model", models.CharField(blank=True, max_length=64)),
                ("passed", models.BooleanField()),
                ("detail", models.CharField(blank=True, max_length=400)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("checked_at", models.DateTimeField(db_index=True)),
                ("last_used", models.DateField(db_index=True)),
                ("carried", models.BooleanField(db_index=True, default=False)),
            ],
            options={
                "verbose_name": "AI check answer",
                "verbose_name_plural": "AI check answers",
                "ordering": ("-checked_at", "rule"),
            },
        ),
        migrations.CreateModel(
            name="RecordRuleResult",
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
                (
                    "points",
                    models.DecimalField(decimal_places=1, default=0, max_digits=4),
                ),
                (
                    "max_points",
                    models.DecimalField(decimal_places=1, default=0, max_digits=4),
                ),
                ("detail_key", models.CharField(blank=True, max_length=40)),
                ("detail", models.CharField(blank=True, max_length=600)),
                ("measure", models.FloatField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "record rule result",
                "verbose_name_plural": "record rule results",
                "ordering": ("entity", "rule"),
            },
        ),
        migrations.AlterModelOptions(
            name="visitaicheck",
            options={
                "ordering": ("visit_key", "rule"),
                "verbose_name": "AI check made per visit (before records)",
                "verbose_name_plural": "AI checks made per visit (before records)",
            },
        ),
        migrations.AlterModelOptions(
            name="visitentity",
            options={
                "ordering": ("visit", "datamart_id"),
                "verbose_name": "record",
                "verbose_name_plural": "records",
            },
        ),
        migrations.AddField(
            model_name="localactionpoint",
            name="record",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visit",
            name="lowest_score",
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name="visit",
            name="records_low",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="visit",
            name="records_scored",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="ai_pending",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="attachments_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="category_deductions",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="evaluated_rules",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=4), default=list, size=None
            ),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="flag_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="flags",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=4), default=list, size=None
            ),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="fmq_answered_categories",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="fmq_answered_pct",
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="location",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="locations.location",
            ),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="location_pcode",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="method_count",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="not_scored_reason",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="provisional_score",
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="quality_score",
            field=models.DecimalField(blank=True, db_index=True, decimal_places=1, max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="questions_answered",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="questions_asked",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="red_flag_count",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="score_band",
            field=models.CharField(blank=True, max_length=8),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="urgency",
            field=models.PositiveSmallIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="urgency_band",
            field=models.CharField(blank=True, max_length=6),
        ),
        migrations.AddField(
            model_name="visitentity",
            name="urgency_parts",
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name="visitentity",
            name="datamart_id",
            field=models.BigIntegerField(),
        ),
        migrations.AddIndex(
            model_name="visitentity",
            index=models.Index(fields=["visit", "quality_score"], name="fmm_visiten_visit_i_c44923_idx"),
        ),
        migrations.AddIndex(
            model_name="visitentity",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["flags"], name="fmm_visiten_flags_9b4f02_gin"
            ),
        ),
        migrations.AddConstraint(
            model_name="visitentity",
            constraint=models.UniqueConstraint(fields=("datamart_id",), name="fmm_record_datamart_id"),
        ),
        migrations.AddConstraint(
            model_name="aicheckanswer",
            constraint=models.UniqueConstraint(
                fields=("rule", "input_hash", "prompt_hash"), name="fmm_ai_check_answer"
            ),
        ),
        migrations.AddField(
            model_name="recordruleresult",
            name="entity",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rule_results",
                to="fmm.visitentity",
            ),
        ),
        migrations.AddIndex(
            model_name="recordruleresult",
            index=models.Index(fields=["rule", "status"], name="fmm_recordr_rule_6871b3_idx"),
        ),
        migrations.AddConstraint(
            model_name="recordruleresult",
            constraint=models.UniqueConstraint(fields=("entity", "rule"), name="fmm_record_rule"),
        ),
        migrations.RunPython(new_rules_version, migrations.RunPython.noop),
        migrations.RunPython(ask_full_refresh, migrations.RunPython.noop),
    ]
