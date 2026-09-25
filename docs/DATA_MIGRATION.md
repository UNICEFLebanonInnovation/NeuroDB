# Attaching NeuroDB v3 to the existing database

v3 does **not** migrate the data. It runs against the existing NeuroDB PostgreSQL database and
maps every v2 table through unmanaged Django models generated from the v2 schema.

## How it works

- `scripts/legacy_schema.json` is a dump of the v2 models (table, columns, many-to-many through
  tables). `scripts/gen_legacy_models.py` turns it into `neurodb/<app>/models/legacy.py`. Class and
  field names are the v2 ones; `db_table` and the many-to-many `db_table` are set explicitly, so the
  auto-generated through tables (`pivoting_subindicator_indicators`, `users_user_groups`, ...) keep
  their names.
- The models keep the **v2 app labels** through `Meta.app_label` and the app configs:
  `neurodb.accounts` is `users`, `neurodb.indicators` (plus the `facts`, `library` and polygon models)
  is `pivoting`, `neurodb.partnerships` is `etools`, `neurodb.geo` is `locations`. Existing content
  types, permissions, group assignments, admin log entries and `AUTH_USER_MODEL = "users.User"` keep
  working without any data change.
- `Meta.managed = True`: the tables belong to Django's migrations (see "Django owns the schema"
  below); the same migrations build the test database.
- Migration history lines up with v2: the v2 database already records `users.0001_initial`,
  `pivoting.0001_initial`, `etools.0001_initial` and `locations.0001_initial`, so v3 treats its own
  `0001_initial` files as applied. The later v2 rows (`pivoting.0067_...` and so on) are ignored because
  v3 has no files with those names. v3's `0002_initial` migrations then run as schema no-ops.
- New v3 tables (`core_syncrun`, `core_savedview`, `core_populationfigure`) are ordinary managed
  models with migrations.
- Tables v3 does not use (`activityinfo_*`, `pivoting_cadasters`, the wizard tables, unused eTools
  clones) are left in place and unmodelled. They can be exported and dropped at any time.

## First connection to a copy of production

1. Restore a copy of the production database (never point a development checkout at production).
2. `DATABASE_URL=postgres://.../neurodb_copy python manage.py migrate` creates only the v3 tables
   (`core_syncrun`, `core_savedview`, `core_populationfigure`). This was rehearsed on a v2-shaped
   database: the column signature of every `users_`, `pivoting_`, `etools_` and `locations_` table was
   identical before and after the migration.
3. `python manage.py bootstrap_roles` and assign existing users to Viewer / Section editor /
   Administrator groups in the admin (superusers are administrators automatically).
4. Compare `python manage.py inspectdb --database default pivoting_activityreportnew` with the
   generated model; a column difference means the production schema drifted from the v2 models
   (the review found drift in `pivoting` and `users`). Regenerate `legacy_schema.json` from the
   live schema if needed and re-run the generator.
5. Run the sync jobs against staging and compare dashboard values with v2 (`docs/DIVERGENCES.md`).

## Django owns the schema

The v2 tables are managed models (`managed = True`) since the ownership step:

- the initial migrations of the legacy apps describe the tables as v2 left them and are recorded
  as applied on production, so Django treats the tables as its own without touching them;
- a table a database never had (production was restored without the `locations` app) is created
  from the model by `manage ensure_legacy_tables`, which `migrate_locked` runs after `migrate`
  at every start; it never alters an existing table;
- from here on a schema change is a normal migration: change the model, `makemigrations`, rehearse
  `migrate` on a restored copy of production, then deploy. The `LEGACY_TABLES_MANAGED` switch is
  gone.
