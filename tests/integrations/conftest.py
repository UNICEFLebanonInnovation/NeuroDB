"""Shared fixtures for the integrations test-suite.

The test database is built from the unmanaged legacy models (``LEGACY_TABLES_MANAGED=True`` and
``MIGRATION_MODULES[app] = None`` in ``config/settings.py``). Django synchronises unmigrated apps
*before* it applies any migration, so ``users_user_groups`` is created before ``auth_group`` exists
and PostgreSQL refuses the foreign key. The ``_two_phase_sync`` autouse fixture below applies the
``contenttypes`` and ``auth`` migrations first, then synchronises the legacy apps, then lets the
normal migration run continue (skipping what phase one already recorded). Apps that ship an
empty ``migrations`` package (``core`` at the time of writing) are marked unmigrated for the test
database so their tables are synchronised too. All of this is a no-op once the project ships
migrations for those apps. Remove it when ``tests/conftest.py`` handles this.
"""

from __future__ import annotations

import datetime as dt
import importlib
import pkgutil

import pytest
from django.conf import settings
from django.core.management.commands import migrate as migrate_command
from django.db.migrations import executor as executor_module

# ------------------------------------------------------------------ database bootstrap workaround
_ORIGINAL_SYNC_APPS = migrate_command.Command.sync_apps
_ORIGINAL_APPLY = executor_module.MigrationExecutor.apply_migration
_BOOTSTRAP_APPS = ("contenttypes", "auth")


def _sync_apps_after_auth(self, connection, app_labels):
    """Apply contenttypes/auth first so legacy tables can reference auth_group."""
    executor = executor_module.MigrationExecutor(connection)
    targets = [key for key in executor.loader.graph.leaf_nodes() if key[0] in _BOOTSTRAP_APPS]
    if targets:
        executor.migrate(targets)
    _ORIGINAL_SYNC_APPS(self, connection, app_labels)


def _apply_unless_recorded(self, state, migration, fake=False, fake_initial=False):
    """Skip migrations that phase one already applied instead of re-running their SQL."""
    if (migration.app_label, migration.name) in self.recorder.applied_migrations():
        return migration.mutate_state(state, preserve=False)
    return _ORIGINAL_APPLY(self, state, migration, fake=fake, fake_initial=fake_initial)


def _mark_migrationless_apps_unmigrated() -> None:
    """Sync (instead of migrate) project apps whose migrations package holds no migration files."""
    modules = dict(getattr(settings, "MIGRATION_MODULES", {}))
    for dotted in settings.INSTALLED_APPS:
        if not dotted.startswith("neurodb."):
            continue
        label = dotted.rsplit(".", 1)[-1]
        if label in modules:
            continue
        try:
            package = importlib.import_module(f"{dotted}.migrations")
        except ImportError:
            continue
        names = [name for _, name, _ in pkgutil.iter_modules(package.__path__) if not name.startswith("_")]
        if not names:
            modules[label] = None
    settings.MIGRATION_MODULES = modules


migrate_command.Command.sync_apps = _sync_apps_after_auth
executor_module.MigrationExecutor.apply_migration = _apply_unless_recorded
_mark_migrationless_apps_unmigrated()


# ------------------------------------------------------------------ model fixtures
@pytest.fixture
def reporting_year(db):
    from neurodb.indicators.models import ReportingYear

    return ReportingYear.objects.create(name="2025", year="2025", current=True, database_id="ck2yrizmo2")


@pytest.fixture
def database(reporting_year):
    """A sector database shaped like the v2 ``pivoting_database`` rows (ai_id = <year><sector>)."""
    from neurodb.indicators.models import Database

    return Database.objects.create(
        ai_id=202516,
        parent_id="ck2yrizmo2",
        db_id="cfolder16",
        name="Child Protection",
        username="",
        password="",
        reporting_year=reporting_year,
        is_funded_by_unicef=False,
        have_offices=False,
    )


@pytest.fixture
def nutrition_database(reporting_year):
    from neurodb.indicators.models import Database

    return Database.objects.create(
        ai_id=202518,
        parent_id="ck2yrizmo2",
        db_id="cfolder18",
        name="Nutrition",
        username="",
        password="",
        reporting_year=reporting_year,
        is_funded_by_unicef=True,
    )


@pytest.fixture
def today() -> dt.date:
    return dt.date(2025, 6, 15)
