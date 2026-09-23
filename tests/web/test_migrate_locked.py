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
