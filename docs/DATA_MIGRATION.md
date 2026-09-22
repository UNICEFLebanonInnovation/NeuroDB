# Attaching NeuroDB v3 to the existing database

v3 does **not** migrate the data. It runs against the existing NeuroDB PostgreSQL database and
maps every v2 table through unmanaged Django models generated from the v2 schema.

## How it works

- `scripts/legacy_schema.json` is a dump of the v2 models (table, columns, many-to-many through
  tables). `scripts/gen_legacy_models.py` turns it into `neurodb/<app>/models/legacy.py`. Class and
  field names are the v2 ones; `db_table` and the many-to-many `db_table` are set explicitly, so the
  auto-generated through tables (`pivoting_subindicator_indicators`, `users_user_groups`, ...) keep
  their names.
- `Meta.managed = LEGACY_MANAGED`, driven by `LEGACY_TABLES_MANAGED` (default `False`): Django never
  creates, alters or drops a v2 table. Migrations for these apps contain `managed=False` and are
  no-ops on the schema. Tests set `DJANGO_ENV=test`, which flips the flag and disables the legacy
  migrations so the test database is built from the model definitions.
- New v3 tables (`core_syncrun`, `core_savedview`, `core_populationfigure`) are ordinary managed
  models with migrations.
- Tables v3 does not use (`activityinfo_*`, `pivoting_cadasters`, the wizard tables, unused eTools
  clones) are left in place and unmodelled. They can be exported and dropped at any time.

## First connection to a copy of production

1. Restore a copy of the production database (never point a development checkout at production).
2. `DATABASE_URL=postgres://.../neurodb_copy python manage.py migrate` creates only the v3 tables.
3. `python manage.py bootstrap_roles` and assign existing users to Viewer / Section editor /
   Administrator groups in the admin (superusers are administrators automatically).
4. Compare `python manage.py inspectdb --database default pivoting_activityreportnew` with the
   generated model; a column difference means the production schema drifted from the v2 models
   (the review found drift in `pivoting` and `users`). Regenerate `legacy_schema.json` from the
   live schema if needed and re-run the generator.
5. Run the sync jobs against staging and compare dashboard values with v2 (`docs/DIVERGENCES.md`).

## Taking ownership of the schema later

When the team wants Django to manage the v2 tables (to add typed columns, indexes, constraints):

1. Set `LEGACY_TABLES_MANAGED=on`, run `makemigrations`, and review the generated operations against
   a schema dump of production; fix any mismatch in the models first.
2. On production, `migrate --fake` the initial migrations of the legacy apps so Django records the
   tables as created.
3. From then on schema changes are normal migrations, rehearsed on a restored copy first.
