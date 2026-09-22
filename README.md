# NeuroDB v3

UNICEF Lebanon's programme analytics portal, rebuilt from scratch on Django 5.2 LTS and PostgreSQL 16.
It mirrors ActivityInfo and eTools data and provides dashboards, pivots, maps, reports and exports.

The **existing NeuroDB database is used as-is**: every v2 table keeps its name and columns
(`neurodb/*/models/legacy.py`, generated from the v2 schema), so all historical data stays in place.
New v3 tables (sync runs, saved views) live alongside them.

## Documents

- `docs/NEURODB_REVIEW_2026.md`: the technical review of v2 that motivated this rebuild.
- `docs/NEURODB_V3_TECHNICAL_DESIGN.md`: the target design.
- `docs/DATA_MIGRATION.md`: how v3 attaches to the v2 database and how to take schema ownership later.
- `docs/OPERATIONS.md`: runbook (deploy, secrets, yearly rollover, sync triage).

## Local development

```bash
cp .env.example .env                     # edit DATABASE_URL to point at a copy of the v2 database
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python manage.py migrate       # creates only the new v3 tables; v2 tables are untouched
.venv/bin/python manage.py bootstrap_roles
.venv/bin/python manage.py runserver
```

Or `docker compose -f docker-compose.dev.yml up`.

## Quality gates (run before every pull request)

```bash
ruff check . && ruff format --check .
DJANGO_ENV=test pytest
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
pip-audit -r requirements.lock
```

## Layout

```
config/            settings (env-driven), urls, wsgi/asgi
neurodb/accounts   User, Section, Office (v2 tables), roles, SSO adapter
neurodb/core       SyncRun, SavedView (new tables)
neurodb/indicators Reporting years, databases, indicator hierarchy, Neuro Reports (v2 tables), navigation
neurodb/facts      ActivityReportNew fact table (v2), parameterised query layer, import rules
neurodb/geo        Admin areas, locations (v2 tables)
neurodb/library    Resources, maps (v2 tables)
neurodb/partnerships  eTools replicas (v2 tables) and partner/PD/donor services
neurodb/integrations  ActivityInfo and eTools clients, sync commands
neurodb/reports    Page views, internal JSON API, exports
neurodb/web        Base layout, components, static assets, middleware
```
