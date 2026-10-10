"""Release 2 step 5 (stage F3), the clean-up once every reader counts records.

- ``VisitRuleResult`` is dropped: every page, export and look-up reads the records' rule results
  (``RecordRuleResult``); a visit's were only derived from them (stage F2).
- ``VisitAICheck`` (the AI checks made per visit before records) is no longer managed by Django. Its table
  is dropped here when no check is left in it (a new database, or one whose checks the AI checks job has
  all carried over: ``legacy_checks_left`` 0 in its run details). Otherwise it is kept: the refresh and the
  AI checks job carry its verdicts over to the records (``ai.checks.carry_over``, with nothing to run by
  hand), and a later migration drops it once ``legacy_checks_left`` reads 0. The code reads it only while
  it is there.

The reverse makes ``VisitAICheck`` managed again (its table made again, empty, when it was dropped) and
``VisitRuleResult`` again, empty: the next refresh fills it."""

from django.db import migrations

LEGACY = "fmm_visitaicheck"


def _has_table(schema_editor, name: str) -> bool:
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        return name in connection.introspection.table_names(cursor)


def drop_legacy_checks_when_done(apps, schema_editor):
    """The table of the checks made per visit dropped when none is left to carry over; kept otherwise."""
    if not _has_table(schema_editor, LEGACY):
        return
    quoted = schema_editor.quote_name(LEGACY)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {quoted})")  # noqa: S608
        if cursor.fetchone()[0]:
            return  # checks left to carry over: the table stays
    schema_editor.execute(f"DROP TABLE {quoted}")


def restore_legacy_checks(apps, schema_editor):
    """The reverse: the table made again (empty) when it was dropped."""
    if _has_table(schema_editor, LEGACY):
        return
    schema_editor.create_model(apps.get_model("fmm", "VisitAICheck"))


class Migration(migrations.Migration):
    dependencies = [
        ("fmm", "0019_records"),
    ]

    operations = [
        migrations.RunPython(drop_legacy_checks_when_done, restore_legacy_checks),
        migrations.AlterModelOptions(
            name="visitaicheck",
            options={
                "managed": False,
                "ordering": ("visit_key", "rule"),
                "verbose_name": "AI check made per visit (before records)",
                "verbose_name_plural": "AI checks made per visit (before records)",
            },
        ),
        migrations.AlterModelTable(
            name="visitaicheck",
            table=LEGACY,  # its name as before, written out: the code reads it by name
        ),
        migrations.DeleteModel(
            name="VisitRuleResult",
        ),
    ]
