#!/bin/bash

set -o errexit
set -o pipefail
set -o nounset


python /app/manage.py collectstatic --noinput --ignore '*.txt'

/usr/local/bin/gunicorn azureproject.wsgi --bind 0.0.0.0:5000 --chdir=/app
