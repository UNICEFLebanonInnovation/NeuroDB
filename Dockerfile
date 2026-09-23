# NeuroDB v3 - one image for the web app and the scheduled jobs (Azure Container Apps).
#
#   docker build --build-arg APP_VERSION=$(git rev-parse --short HEAD) -t neurodb:local .
#   docker run --env-file .env -p 8000:8000 neurodb:local            # migrations, then the website
#   docker run --env-file .env neurodb:local migrate                  # migrations only
#   docker run --env-file .env neurodb:local manage sync_etools       # any command
#
# BASE_IMAGE can point at a mirror (for example an ACR import of the same tag) when Docker Hub is
# rate limited in CI. A proxy CA can be passed with `--secret id=pip_ca,src=ca.crt`; it is used for
# pip only and never stored in the image.
ARG BASE_IMAGE=python:3.12-slim-bookworm

# ------------------------------------------------------------------------------------------ build
FROM ${BASE_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi \
 && python -m venv /venv \
 && /venv/bin/pip install -r requirements.lock
COPY manage.py pyproject.toml ./
COPY config ./config
COPY neurodb ./neurodb
# Static files are collected at build time (WhiteNoise serves them with far-future cache headers).
RUN DJANGO_ENV=production DJANGO_SECRET_KEY=build-only DATABASE_URL=postgres://build@localhost/build \
    /venv/bin/python manage.py collectstatic --noinput -v0 \
 && /venv/bin/python -m compileall -q config neurodb

# ---------------------------------------------------------------------------------------- runtime
FROM ${BASE_IMAGE} AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="NeuroDB" \
      org.opencontainers.image.description="UNICEF Lebanon programme monitoring platform" \
      org.opencontainers.image.source="https://github.com/UNICEFLebanonInnovation/NeuroDB" \
      org.opencontainers.image.revision="${APP_VERSION}"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:${PATH}" \
    DJANGO_ENV=production \
    PORT=8000 \
    RUN_MIGRATIONS=true \
    APP_VERSION=${APP_VERSION}
RUN groupadd --system --gid 10001 neurodb \
 && useradd --system --uid 10001 --gid neurodb --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin neurodb
WORKDIR /app
# Code and dependencies stay owned by root: the application user can read but not modify them.
COPY --from=build /venv /venv
COPY --from=build /app /app
COPY docker/entrypoint.sh /usr/local/bin/neurodb
COPY docker/healthcheck.py /usr/local/bin/neurodb-healthcheck
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "/usr/local/bin/neurodb-healthcheck"]
# `web` applies pending migrations (under a database lock) before gunicorn starts, so every deployment
# of a new image migrates the database. Set RUN_MIGRATIONS=false to skip.
ENTRYPOINT ["neurodb"]
CMD ["web"]
