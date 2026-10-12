from django.core.management import call_command
from django.db import connection

from neurodb.core.management.commands.migrate_locked import LOCK_ID


def test_migrate_locked_applies_roles_and_releases_the_lock(db):
    from django.contrib.auth.models import Group

    call_command("migrate_locked", verbosity=0)
    assert Group.objects.filter(name__in=["Viewer", "Section editor", "Administrator"]).count() == 3
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = %s", [LOCK_ID])
        assert cursor.fetchone()[0] == 0


def test_each_migration_shows_in_the_log_when_it_starts():
    """``migrate`` ends the "Applying <migration>..." line only when the migration is done, and a
    container log shows a line once it ends: in the 503 incident the line of a migration that ran four
    minutes appeared only when App Service stopped the container, stamped with that time."""
    import io

    from django.core.management.base import OutputWrapper
    from django.core.management.commands.migrate import Command as Migrate

    from neurodb.core.management.commands.migrate_locked import WholeLines

    out = io.StringIO()
    migrate = Migrate(stdout=WholeLines(OutputWrapper(out)))
    migrate.verbosity = 1
    migrate.migration_progress_callback("apply_start", migration="datamart.0010_fill_reached_columns")
    assert out.getvalue() == "  Applying datamart.0010_fill_reached_columns...\n"  # a whole line, at once
    migrate.migration_progress_callback("apply_success", migration="datamart.0010_fill_reached_columns")
    assert out.getvalue().endswith(" OK\n")


def test_migrate_locked_writes_the_migrations_in_whole_lines(monkeypatch):
    from neurodb.core.management.commands import migrate_locked

    calls = {}
    monkeypatch.setattr(migrate_locked, "call_command", lambda name, *a, **kw: calls.setdefault(name, kw))
    migrate_locked.Command()._migrate(1)
    assert isinstance(calls["migrate"]["stdout"], migrate_locked.WholeLines)


def test_the_app_service_settings_give_the_start_up_room():
    """App Service stops a container that has not started within 230 seconds unless
    WEBSITES_CONTAINER_START_TIME_LIMIT says otherwise; the web container migrates before it serves."""
    import json
    from pathlib import Path

    from django.conf import settings

    env = Path(settings.BASE_DIR) / "infra" / "env"
    app_settings = {s["name"]: s["value"] for s in json.loads((env / "azure-app-settings.json").read_text())}
    catalogue = {
        v["name"]: v for v in json.loads((env / "azure-env-variables.json").read_text())["variables"]
    }
    limit = int(app_settings["WEBSITES_CONTAINER_START_TIME_LIMIT"])
    assert 300 <= limit <= 1800
    assert catalogue["WEBSITES_CONTAINER_START_TIME_LIMIT"]["value"] == str(limit)
