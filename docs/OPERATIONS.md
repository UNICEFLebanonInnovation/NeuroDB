# Operations runbook (v3)

## Deploy and rollback
The platform is Azure Container Apps: one image runs the website (`<prefix>-web`) and every job
(`<prefix>-migrate`, `-ai-structure`, `-ai-data`, `-etools`, `-locations`, `-freshness`). Setup and
architecture: `docs/DEPLOYMENT_AZURE.md`.

CI builds `<acr>/neurodb:<commit sha>` on `main` and runs `infra/scripts/deploy.sh` for staging and
then, after approval, production. The script runs migrations as a job first and stops if they fail.
It then rolls out a new web revision, which only takes traffic when `/healthz/` is ready, and
switches the jobs to the new image. Migrations never run at container start.

Rollback: `infra/scripts/rollback.sh` with the previous image tag. Migrations are not reversed, so
every migration must stay compatible with the previous release for one deployment.

## Configuration
All settings are environment variables (`.env.example`). In Azure they are set by
`infra/main.bicep`; secrets are Key Vault references read with the app's managed identity. There
is no configuration file in the image. Change a non-secret setting by editing the Bicep parameters
and re-running the deployment.

## Secrets rotation
Key Vault secrets: `django-secret-key`, `database-url`, `activityinfo-token`, `etools-token` and
(with SSO) `entra-client-secret`. To rotate, set a new version in Key Vault, then restart the active
web revision (`az containerapp revision restart`). Jobs pick the new value up on their next run.
No code change is needed. Rotating the Django secret key signs everyone out.

## Scheduled jobs
Container Apps cron is **UTC**; Beirut is UTC+3 in summer and UTC+2 in winter.

| Job | Command | Schedule (UTC) | Beirut (summer) |
|---|---|---|---|
| `locations` | `manage sync_locations` | `0 2 * * *` | 05:00 daily |
| `ai-data` | `manage import_activityinfo_data --current-year` | `0 15 1-22 * *` | 18:00, days 1–22 |
| `etools` | `manage sync_etools` | `30 17 * * *` | 20:30 daily |
| `freshness` | `manage check_sync_freshness` | `15 * * * *` | hourly; a stale source fails the run |
| `ai-structure` | `manage import_activityinfo_structure --all` | manual | after yearly rollover |
| `migrate` | `migrate` (then `bootstrap_roles`) | manual, run by the pipeline | |

Run one now: `az containerapp job start -n <prefix>-<job> -g <resource group>`.
Every run writes a `SyncRun` row (admin → Core → Sync runs; page `/data/health/`), and the job's
execution history keeps its logs.
Triage a failure: open the run, read `error`, re-run the command with `--database <ai_id>` or
`--only <entity>` (`az containerapp exec … --command "neurodb manage …"`). A `PARTIAL` run lists the
failed item ids in `details`.

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
Azure Database for PostgreSQL point-in-time restore (set retention to at least 14 days) covers the
database; Blob storage keeps deleted files for 30 days (soft delete). Rehearse one restore per quarter
into a staging resource group and record the date here.

## Health
- `GET /healthz/live/`: process is up (liveness and startup probes). No database access.
- `GET /healthz/`: database reachable, last successful run of each job and the running version
  (readiness probe; 503 when the database is unreachable). The pipeline's smoke test waits for the
  new version here.

Logs: `az containerapp logs show -n <prefix>-web -g <rg> --follow`, or Log Analytics
(`ContainerAppConsoleLogs_CL`). Requests, database calls, outgoing calls to ActivityInfo and eTools,
and exceptions are traced in Application Insights.
