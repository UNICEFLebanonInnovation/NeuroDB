# Operations runbook (v3)

## Deploy and rollback
CI builds an image tagged with the git SHA on `main`, deploys it to the `staging` slot, runs the
smoke test on `/healthz/`, then swaps. Rollback = swap the slots back. Migrations run in the
pipeline before the swap (`python manage.py migrate`), never at container start.

## Configuration
All settings come from environment variables (`.env.example`). In Azure they are App Service /
Container Apps settings backed by Key Vault references. There is no configuration file in the image.

## Secrets rotation
`ACTIVITYINFO_TOKEN`, `ETOOLS_TOKEN`, `ENTRA_CLIENT_SECRET`, `DJANGO_SECRET_KEY`, `DATABASE_URL`:
update the Key Vault secret, restart the app and the jobs. No code change. Rotating the Django
secret key signs everyone out.

## Scheduled jobs (Beirut time)
| Job | Command | Schedule |
|---|---|---|
| Locations | `manage.py sync_locations` | 05:00 daily |
| ActivityInfo data | `manage.py import_activityinfo_data --current-year` | 18:00 daily, days 1–22 |
| eTools | `manage.py sync_etools` | 20:30 daily |
| Freshness check | `manage.py check_sync_freshness` | hourly; non-zero exit raises the alert |

Every run writes a `SyncRun` row (admin → Core → Sync runs; page `/data/health/`).
Triage a failure: open the run, read `error`, re-run the command with `--database <ai_id>` or
`--only <entity>`; a `PARTIAL` run lists failed item ids in `details`.

## Yearly rollover (January)
1. Admin → Reporting years: create the new year and tick *current* (only one can be current).
2. Admin → Databases: create one row per ActivityInfo folder with `ai_id`, `db_id`, `parent_id`,
   section and the new reporting year; tick *Show on dashboard*.
3. Select the databases → action *Import structure from ActivityInfo*.
4. Wire master indicators (Add sub-indicators wizard) and Neuro Reports (Add master indicators
   wizard), or copy last year's configuration in the admin.
5. `manage.py load_population_figures --year YYYY <file.json>` with the year's UNICEF figures.
6. Upload the year's HPM PDF tables to the library.
7. Select the databases → action *Import data from ActivityInfo*; check `/data/health/`.

## Backups
Azure Flexible Server point-in-time restore (14 days) plus a nightly `pg_dump` to Blob storage.
Rehearse one restore per quarter and record the date here.

## Health
`GET /healthz/` returns database connectivity and the last successful run of each job; the App
Service health probe and the Application Insights availability test use it.
