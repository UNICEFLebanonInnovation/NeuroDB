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
      # Reference data shipped with the code (population figures): loads only the years not in the
      # database yet, so it is a no-op after the first start. A failure is logged, not fatal.
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
