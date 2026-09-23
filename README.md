# NeuroDB v3

UNICEF Lebanon's programme analytics portal, rebuilt from scratch on Django 5.2 LTS and PostgreSQL 16.
It mirrors ActivityInfo and eTools data and provides dashboards, pivots, maps, reports and exports.

The **existing NeuroDB database is used as-is**: every v2 table keeps its name and columns
(`neurodb/*/models/legacy.py`, generated from the v2 schema), so all historical data stays in place.
New v3 tables (sync runs, saved views, population figures) live alongside them.

## What v3 offers

- **Programme overview**: status of every master indicator across all databases of the year,
  data freshness per sync job, one card per database with its tracking mix.
- **Database dashboards**: KPI tiles, tracking donut, monthly trend, filterable indicator table with
  progress bars and a sub-indicator drill-down in a modal; printable snapshot; raw data as CSV or Excel.
- **Analytical view**: pivot table over every ActivityInfo record with layout presets
  (month, governorate, partner, sub-indicators, demographics), emergency filter, chart renderers,
  export, and **saved views** that can be shared with everyone.
- **Intervention map**: choropleth by governorate, district or cadaster and site circles
  (MapLibre, OpenStreetMap tiles), with partner, programme document, area and month filters.
- **Neuro Reports and HPM**: values to the end of any month or quarter with the v2 cut-off rule,
  change against the previous period, and section-scoped comments.
- **Partnerships**: programme documents with multi-filters, summary (ending within 90 days), donor
  mapping (funds by donor and year, planned versus actual locations), partner profiles.
- **Resources**: population figures by nationality, area and age; library with search and filters;
  completed map products.
- **Data health**: freshness of every sync job and database import, with recent run history.
- **Everywhere**: command palette (Ctrl K or /) across databases, reports and indicators; dark mode;
  copy or CSV on every table; keyboard-sortable tables; mobile layout; strict Content Security Policy.

## Documents

- `docs/NEURODB_REVIEW_2026.md`: the technical review of v2 that motivated this rebuild.
- `docs/NEURODB_V3_TECHNICAL_DESIGN.md`: the target design.
- `docs/DATA_MIGRATION.md`: how v3 attaches to the v2 database and how to take schema ownership later.
- `docs/OPERATIONS.md`: runbook (deploy, secrets, yearly rollover, sync triage).

## Public landing page and public access

Visitors who are not signed in see a public landing page at `/` (also at `/welcome/` for everyone):
the value of the platform, how it works, what is new in v3, quick links and an FAQ. Signed-in users
land on the programme overview as before.

| Setting | Default | Effect |
|---|---|---|
| `PUBLIC_LANDING_STATS` | `on` | Shows aggregate counts for the current year (indicators, records, partners, governorates, sections). No names or values. |
| `PUBLIC_PAGES` | empty | Comma-separated pages opened without sign-in, read-only. Allowed: `library`, `maps`, `population`. Any other value stops the app at startup. |
| `SUPPORT_EMAIL` | empty | Adds a "Request access" link and a contact address. |
| `USER_GUIDE_URL` | empty | Adds a "User guide" quick link. |

Dashboards, reports, partnerships, data health and the internal API always require sign-in.

## Local development

```bash
cp .env.example .env                     # edit DATABASE_URL to point at a copy of the v2 database
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python manage.py migrate       # creates only the new v3 tables; v2 tables are untouched
.venv/bin/python manage.py bootstrap_roles
.venv/bin/python manage.py runserver
```

Or `docker compose -f docker-compose.dev.yml up`.

### Demo data (no production copy needed)

```bash
createdb neurodb_demo
export DJANGO_ENV=local DJANGO_DEBUG=on LEGACY_TABLES_MANAGED=on DATABASE_URL=postgres://localhost/neurodb_demo
.venv/bin/python manage.py migrate        # LEGACY_TABLES_MANAGED=on creates the v2 tables in this empty database
.venv/bin/python manage.py seed_demo --password <choose-one>
.venv/bin/python manage.py runserver      # sign in as demo-admin, demo-editor or demo-viewer
```

`seed_demo` refuses to run outside `DJANGO_ENV=local`/`test` or on a database that already holds data.
Set `DEBUG_TOOLBAR=off` to hide the Django debug toolbar.

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
neurodb/core       SyncRun, SavedView, PopulationFigure (new tables), seed_demo
neurodb/indicators Reporting years, databases, indicator hierarchy, Neuro Reports (v2 tables), navigation
neurodb/facts      ActivityReportNew fact table (v2), parameterised query layer, import rules
neurodb/geo        Admin areas, locations (v2 tables)
neurodb/library    Resources, maps (v2 tables)
neurodb/partnerships  eTools replicas (v2 tables) and partner/PD/donor services
neurodb/integrations  ActivityInfo and eTools clients, sync commands
neurodb/reports    Page views, internal JSON API, exports
neurodb/web        Base layout, components, static assets (vendored, no CDN), CSP middleware
```

The legacy models keep the **v2 Django app labels** (`users`, `pivoting`, `etools`, `locations`) even
though the Python packages are named by domain. Content types, permissions, admin URLs and the
`django_migrations` history of the existing database therefore stay valid; see `docs/DATA_MIGRATION.md`.

Front end: server-rendered Django templates with Bootstrap 5.3 and HTMX 2 for partial updates, plus
small ES modules loaded on demand (Plotly charts, PivotTable.js, MapLibre, Tom Select). Libraries are
vendored under `neurodb/web/static/vendor` with their versions in `VERSIONS.md`.
