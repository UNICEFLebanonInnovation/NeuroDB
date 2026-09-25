"""The location tables exist whatever database the site starts on."""

import importlib

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_the_create_if_missing_sql_runs_on_a_database_that_has_the_tables():
    module = importlib.import_module("neurodb.geo.migrations.0002_location_tables_when_missing")
    with connection.cursor() as cursor:
        cursor.execute(module.SQL)  # IF NOT EXISTS: a no-op here, a parse of every statement
        cursor.execute("SELECT to_regclass('locations_location'), to_regclass('etools_pca_locations')")
        assert all(cursor.fetchone())
