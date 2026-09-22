# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.lock
COPY . .
RUN /venv/bin/pip install --no-cache-dir --no-deps -e . \
 && DJANGO_SECRET_KEY=build DATABASE_URL=postgres://x@localhost/x /venv/bin/python manage.py collectstatic --noinput

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/venv/bin:$PATH"
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build --chown=app:app /venv /venv
COPY --from=build --chown=app:app /app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/').status==200 else 1)"
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", "--timeout", "120", "--access-logfile", "-"]
