#!/bin/sh
# One image, several roles (Azure Container App + Container Apps Jobs):
#   web                 apply migrations (unless RUN_MIGRATIONS=false), then serve the site (default)
#   migrate             apply database migrations only
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
    fi
    exec gunicorn config.wsgi:application --config /app/config/gunicorn.conf.py
    ;;
  migrate)
    exec python manage.py migrate_locked
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
