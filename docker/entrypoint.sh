#!/bin/sh
# One image, several roles (Azure Container App + Container Apps Jobs):
#   web                 serve the site with gunicorn (default)
#   migrate             apply database migrations (run as a job before switching traffic)
#   manage <command>    any management command, e.g. `manage sync_etools`
#   check               Django deployment checks against the real configuration
set -eu
cd /app
role="${1:-web}"
case "$role" in
  web)
    exec gunicorn config.wsgi:application --config /app/config/gunicorn.conf.py
    ;;
  migrate)
    python manage.py migrate --noinput
    exec python manage.py bootstrap_roles
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
