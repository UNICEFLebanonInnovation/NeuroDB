# NeuroDB v3

UNICEF Lebanon's programme analytics portal, rebuilt from scratch on Django 5.2 LTS and PostgreSQL 16.
It mirrors eTools data, keeps the ActivityInfo results partners reported in past years (an archive once
ActivityInfo is no longer used), and provides dashboards, pivots, maps, reports and exports.

The **existing NeuroDB database is used as-is**: every v2 table keeps its name and columns
(`neurodb/*/models/legacy.py`, generated from the v2 schema), so all historical data stays in place.
New v3 tables (sync runs, saved views, population figures) live alongside them.

## What v3 offers

- **Home** (`/`, the signed-in home page): what needs you today (For you, the decisions for management,
  the daily review's digest), what changed since the previous working day, where your section or the
  country stands this year (the Country overview's own key figures) and the state of each source.
- **Country overview** (`/country-overview/`): the whole intervention live by calendar year: children
  reached by governorate and against targets (eTools, and ActivityInfo in the years it has records,
  shown apart), funds disbursed and cost per child by section, delivery status, TPM visit and action
  point progress, filtered by year, section and governorate.
- **Daily review** (`/daily-review/`): the morning check's findings on a page of their own.
- **ActivityInfo, in use or as an archive**: one admin setting (Automatic by default: in use while its
  import runs and this year has recent records) decides whether ActivityInfo is presented as in use or
  as the archive of its years; every ActivityInfo database, dashboard and report of past years stays
  open, and the import's alarms stop once nobody runs it. See `docs/OPERATIONS.md`, *ActivityInfo: in
  use or archive*.
- **Database dashboards**: KPI tiles, tracking donut, monthly trend, filterable indicator table with
  progress bars and a sub-indicator drill-down in a modal; printable snapshot; raw data as CSV or Excel.
- **Analytical view** (ActivityInfo years): pivot table over every ActivityInfo record with layout presets
  (month, governorate, partner, sub-indicators, demographics), emergency filter, chart renderers,
  export, and **saved views** that can be shared with everyone.
- **Intervention map**: choropleth by governorate, district or cadaster and site circles
  (MapLibre, OpenStreetMap tiles), with partner, programme document, area and month filters.
- **Neuro Reports and HPM**: values to the end of any month or quarter that has ended, with the v2 cut-off rule,
  change against the previous period, and section-scoped comments.
- **Partnerships**: programme documents with multi-filters, summary (ending within 90 days), donor
  mapping (funds by donor and year, planned versus actual locations), partner profiles.
- **Resources**: population figures by nationality, area and age; library with search and filters;
  completed map products.
- **Data health**: freshness of every sync job and database import, with recent run history.
- **Ask NeuroDB (AI assistant)**: questions in plain language ("which Child Protection programme
  documents are off track this year?", "total budget of EU-funded PDs?") answered by ChatGPT (an OpenAI model via the
  OpenAI API) from NeuroDB's own data through read-only lookups, streamed live with links to the
  source pages; also from the search box. Needs `OPENAI_API_KEY`; see `docs/OPERATIONS.md`.
- **Everywhere**: search (Ctrl K or /) across pages, partners, programme documents, country programme
  indicators, knowledge base documents and the ActivityInfo archive of every year; dark mode;
  copy or CSV on every table; keyboard-sortable tables; mobile layout; strict Content Security Policy.
- **Daily AI review**: every morning fourteen checks read the synced data (off-track and unreported
  indicators, overdue reports and action points, late TPM reports, spending ahead of delivery,
  failed syncs) and store dated findings, marked new, still open or resolved, with a short summary.
- **Management brief**: the overview read for decisions, at `/brief/`: comparisons with last year
  and the plan, a confidence signal per section, the two sources per partner, equity of reach,
  district gaps, a partner scorecard, donor-to-child money flows, grants at risk, and whether the
  daily review's findings are owned and closed; the brief as text for the minutes.
- **Monitoring insights**: eTools field monitoring as visits, at `/fmm/`: report quality rules
  and scores, urgency and follow-up, charts with drill-down, a visit page, a map of visits against
  planned locations, and an AI brief and chat over the filter's visits (off until go-live). How it
  counts, what goes to OpenAI, how to switch it off and the go-live checklist are in
  `docs/OPERATIONS.md`, *Monitoring insights*.

## Documents

- `docs/NEURODB_REVIEW_2026.md`: the technical review of v2 that motivated this rebuild.
- `docs/NEURODB_V3_TECHNICAL_DESIGN.md`: the target design.
- `docs/DATA_MIGRATION.md`: how v3 attaches to the v2 database and how to take schema ownership later.
- `docs/DEPLOYMENT_AZURE.md`: running the Docker image on Azure Container Apps (infrastructure, secrets, pipeline).
- `docs/OPERATIONS.md`: runbook (deploy, secrets, yearly rollover, sync triage).

## Public landing page and public access

Visitors who are not signed in see a public landing page at `/` (also at `/welcome/` for everyone): why
NeuroDB exists, four project photographs, the data and the rules behind it (with a sample AI insight to
explore), a programme story to follow, a real-life story, what staff, donors and partners find inside, and
the way in through NeuroDB's own sign-in ("Open NeuroDB" once signed in). The figures it quotes about the
platform (the morning checks, the field visit quality rules switched on, the Watch lead times, the unspent
amount of a grant alert and the start of the HACT season) are read from the code and settings that apply
them. Its examples are labelled simulated or illustrative, and its photos come from UNICEF Lebanon stories
on unicef.org, each card linking to a related story. Its footer holds the logo and the public pages. Its copy
follows ActivityInfo's state: while ActivityInfo is in use it is presented as a current source; once it is an
archive the page names the years it holds ("ActivityInfo archive 2019–2026") and shows no ActivityInfo
figure; with no year of ActivityInfo records it does not mention ActivityInfo. Signed-in users land on Home.

| Setting | Default | Effect |
|---|---|---|
| `PUBLIC_LANDING_STATS` | `on` | Shows aggregate counts for this calendar year, in place of the page's "About data volume" note: eTools programme documents, partners, indicators and progress reports, and, only while ActivityInfo is in use, its results, governorates, records, partners and sections. No names or values. |
| `PUBLIC_PAGES` | empty | Comma-separated pages opened without sign-in, read-only. Allowed: `library`, `maps`, `population`. Any other value stops the app at startup. Each one listed gets a footer link on the landing page. |
| `SUPPORT_EMAIL` | empty | The contact the admin's scheduler notice names for a restart. Never on the landing page. |
| `USER_GUIDE_URL` | empty | Adds a "User guide" footer link. |

Dashboards, reports, partnerships, data health and the internal API always require sign-in.

## Local development

```bash
cp .env.example .env                     # edit DATABASE_URL to point at a copy of the v2 database
python -m venv .venv && .venv/bin/pip install -r requirements-dev.lock
.venv/bin/python manage.py migrate       # creates only the new v3 tables; v2 tables are untouched
.venv/bin/python manage.py bootstrap_roles
.venv/bin/python manage.py runserver
```

Or `docker compose -f docker-compose.dev.yml up`.

### Demo data (no production copy needed)

```bash
createdb neurodb_demo
export DJANGO_ENV=local DJANGO_DEBUG=on DATABASE_URL=postgres://localhost/neurodb_demo
.venv/bin/python manage.py migrate        # creates the v2 tables in this empty database
.venv/bin/python manage.py seed_demo --password <choose-one>
.venv/bin/python manage.py runserver      # sign in as demo-admin, demo-editor or demo-viewer
```

`seed_demo` refuses to run outside `DJANGO_ENV=local`/`test` or on a database that already holds data.
Set `DEBUG_TOOLBAR=off` to hide the Django debug toolbar.

Dependencies: `requirements.lock` pins what the production image installs; `requirements-dev.lock`
adds the test and lint tools. Regenerate both with
`uv pip compile pyproject.toml -o requirements.lock` and
`uv pip compile pyproject.toml --extra dev -c requirements.lock -o requirements-dev.lock`.

## Quality gates (run before every pull request)

```bash
ruff check . && ruff format --check .
DJANGO_ENV=test pytest
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
pip-audit -r requirements.lock -r requirements-dev.lock
docker build -t neurodb .                # the production image
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
neurodb/datamart   eTools Datamart tables (funds, indicators, assurance, monitoring) and their page services
neurodb/integrations  ActivityInfo, eTools and eTools Datamart clients, sync commands
neurodb/reports    Page views, internal JSON API, exports
neurodb/assistant  AI assistant: OpenAI API agent (Responses API), read-only data tools, question log
infra/             Azure Bicep template, deploy and rollback scripts
docker/            Container entrypoint and health check
neurodb/web        Base layout, components, static assets (vendored, no CDN), CSP middleware
```

The legacy models keep the **v2 Django app labels** (`users`, `pivoting`, `etools`, `locations`) even
though the Python packages are named by domain. Content types, permissions, admin URLs and the
`django_migrations` history of the existing database therefore stay valid; see `docs/DATA_MIGRATION.md`.

Branding: the v2 NeuroDB logo (molecule mark and "NEURO-DB" wordmark, `static/images/logo3.png` and
`logo_inverse.png` in v2) is reused as `neurodb/web/static/img/logo.png` and `logo-mark.png`; the
favicons and social preview are generated from it, and the interface colour is its blue `#446ab3`.

Front end: server-rendered Django templates with Bootstrap 5.3 and HTMX 2 for partial updates, plus
small ES modules loaded on demand (Plotly charts, PivotTable.js, MapLibre, Tom Select). Libraries are
vendored under `neurodb/web/static/vendor` with their versions in `VERSIONS.md`.
