#!/bin/sh
# One image, several roles (Azure Container App + Container Apps Jobs):
#   web                 apply migrations and bundled reference data (unless RUN_MIGRATIONS=false), then serve
#   migrate             apply database migrations and bundled reference data only
#   manage <command>    any management command, e.g. `manage sync_etools`
#   check               Django deployment checks against the real configuration
set -eu
cd /app
role="${1:-web}"
case "$role" in
  web)
    if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
      echo "Applying database migrations before start (set RUN_MIGRATIONS=false to skip)"
      python manage.py migrate_locked   # a failure stops the container: no traffic on a half-migrated schema
      # Reference data shipped with the code (population figures): loads the years not in the
      # database yet and reloads, once, a year an older loader stored wrongly; a no-op otherwise.
      # A failure is logged, not fatal. Admins can also reload from the admin (Population figures).
      python manage.py load_population_figures --bundled || echo "WARNING: bundled population figures not loaded"
    fi
    exec gunicorn config.wsgi:application --config /app/config/gunicorn.conf.py
    ;;
  migrate)
    python manage.py migrate_locked
    exec python manage.py load_population_figures --bundled
    ;;
  manage)
    shift
    exec python manage.py "$@"
    ;;
  check)
    exec python manage.py check --deploy --fail-level WARNING
    ;;
  *)
    exec "$@"
    ;;
esac
