"""NeuroDB now asks BMA (Compiler) to calculate before reading, so BMA keeps no schedule: the education
sync moves to 02:30 and the wellbeing sync to 03:00, the night times BMA used to calculate at. A
schedule already changed in the admin is left as it is."""

from django.db import migrations

MOVES = {"compiler-education": ("30 6 * * *", "30 2 * * *"), "compiler-wellbeing": ("0 4 * * *", "0 3 * * *")}


def move(apps, schema_editor, back=False):
    ScheduledJob = apps.get_model("core", "ScheduledJob")
    for key, (before, after) in MOVES.items():
        if back:
            before, after = after, before
        ScheduledJob.objects.filter(key=key, schedule=before).update(schedule=after, next_run_at=None)


class Migration(migrations.Migration):
    dependencies = [("core", "0016_compiler_wellbeing_schedule")]

    operations = [migrations.RunPython(move, lambda apps, editor: move(apps, editor, back=True))]
