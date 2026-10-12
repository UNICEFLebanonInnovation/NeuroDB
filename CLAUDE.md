# NeuroDB v3: rules for changes

- **Administrators have no shell.** NeuroDB runs on Azure App Service and its administrators cannot
  run `manage.py`. Every operation an administrator needs must be reachable from the web: a button in
  *Import and sync runs → Run a job* (`neurodb/core/admin_jobs.py` `BACKGROUND_JOBS`, with its
  `neurodb/core/jobs.py` `COMMANDS` entry), a scheduled job (`ScheduledJob`, seeded by a core
  migration), or an action on the model's own admin page. A new management command that an
  administrator would need ships with its button in the same change, and user-facing text and
  `docs/OPERATIONS.md` never tell an administrator to run a command. Developer-only commands
  (fixtures, demo data, deployment steps) are listed as such in OPERATIONS.md "Run a job".
- **Migrations must be quick.** Every web container applies pending migrations before it serves, and
  App Service stops a container that has not started within about four minutes: a long migration is
  cut off and rolled back at every restart, and the site never comes up (503). A migration only
  changes the schema or writes small tables (settings, schedules, prompts). Work over the large eTools
  or Monitoring insights tables is a background job that a schedule starts once the site is up
  (`neurodb/datamart/backfill.py` with its schedule seeded, due at once, by core migration 0025).
  `tests/datamart/test_backfill.py` fails on any new migration that runs code until it is listed
  there as quick.
