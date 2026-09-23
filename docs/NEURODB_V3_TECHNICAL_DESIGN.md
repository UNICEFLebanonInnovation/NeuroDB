# NeuroDB v3: Technical Design

**Version:** 1.0 draft for review
**Date:** 22 September 2026
**Basis:** `docs/NEURODB_REVIEW_2026.md` (the technical review of the current system, referred to below as "v2"), the vendor's Software Design Document v0.5, and the current codebase.
**Purpose:** a complete design for a new NeuroDB built from scratch that preserves the data, the indicator model, the business rules, and the parts of the user experience that work, and leaves behind everything the review found unsafe, dead, or unmaintainable.

---

## 1. Summary

NeuroDB v3 is a Django 5.2 LTS application on PostgreSQL 16, deployed as one container on Azure, with three scheduled jobs that mirror ActivityInfo and eTools data into a typed schema, an aggregation layer expressed once as database views, and a small server-rendered web front end for about 25 pages of dashboards, pivots, maps, and exports. It replaces v2 completely after a one-off data migration and a short parallel run.

What carries over from v2, unchanged in meaning:

- The PostgreSQL data (configuration, fact rows, eTools replicas, locations, users).
- The indicator hierarchy: Reporting Year, Database, Activity, Leaf Indicator, Sub Indicator, Master Indicator with per-link effect, Neuro Report with per-report overrides, and the two admin wizards.
- The aggregation rules encoded in the 18 live SQL constants, re-expressed as views and verified by golden tests.
- The import rules buried in the v2 import function: partner-name normalisation, emergency and COVID keyword detection, the nutrition special case, and the mid-month reporting cut-off.
- The integration contracts with ActivityInfo (structure, form schema, export jobs) and eTools (partners, agreements, interventions, engagements, travels, action points, locations).
- The page inventory and the interaction patterns users value: the pivot table with custom aggregators and preset views, shareable GET-form filters, the dashboard header pattern, DataTables exports.

What is left behind: raw SQL assembled in views, the dead `activityinfo` app and the legacy `/etools/` pages, three of four base layouts, 2,900 lines of inline JavaScript, string-typed dates and money, secrets in source, five deployment paths, cron inside the web container, and the absence of tests.

Sizing: 11 to 16 person-months, 7 to 8 months elapsed, a team of three (section 15). The v2 site must receive the two-week security hardening from the review's Phase 0 before this work starts, because it keeps running until cutover.

---

## 2. Goals, non-goals, constraints

**Goals**

1. Functional parity with the v2 scope checklist (review Appendix A), page by page, verified against the same data.
2. Every data endpoint authenticated; authorisation by role and section; no credential in source; every SQL parameter bound.
3. One reproducible build, one deployment path, one settings module, jobs outside the web process, monitoring and alerts from day one.
4. A typed schema that a BI tool can read directly and that a new developer can understand from the models alone.
5. Tests on every route, every sync, and every aggregation rule, running in CI before every deploy.
6. A front end that a small team can change safely: one layout, one build, no unpinned third-party scripts, accessible by default, translatable.

**Non-goals (explicitly out of scope for v3.0)**

- New reports or new data sources. The scope is frozen to the checklist until cutover.
- Replacing bespoke pages with Power BI. v3 exposes its aggregation views to Power BI; page replacement is a later product decision.
- A single-page application or a public API. The JSON endpoints exist for the app's own pages and are authenticated.
- Arabic and French user interface. v3 is built translatable; translation is a separate decision.

**Constraints**

- Team of two to three developers plus a part-time product owner; finite country-office budget.
- Azure (App Service or Container Apps, Flexible Server, Blob, Key Vault, Entra ID) is the platform.
- The v2 site keeps serving users until cutover; upstream tokens are personal today and service accounts must be requested.
- UNICEF's Policy on Personal Data Protection applies to partner and staff data.

---

## 3. Architecture

```
                           Microsoft Entra ID (UNICEF tenant)
                                        |
   Users ── HTTPS ──> Azure Front Door / App Service TLS
                                        |
                        +---------------v----------------+
                        |  Web container (gunicorn)      |
                        |  Django 5.2 LTS                |
                        |  - templates + HTMX partials   |
                        |  - authenticated JSON views    |
                        |  - admin (indicator config)    |
                        +-------+-----------------+------+
                                |                 |
                    +-----------v----+    +-------v---------+
                    | PostgreSQL 16  |    | Azure Blob      |
                    | Flexible Server|    | - extracts      |
                    | - typed schema |    | - library files |
                    | - agg. views   |    | - HPM PDFs      |
                    +-----------^----+    +-----------------+
                                |
   +----------------------------+-----------------------------+
   |  Scheduled jobs (Container Apps Jobs, same image)        |
   |  05:00 locations  18:00 ActivityInfo  20:30 eTools (Beirut)|
   +-------------+---------------------------+----------------+
                 |                           |
        ActivityInfo API (v4)          eTools API (v2, t2f, audit)

   Cross-cutting: Key Vault (secrets via env), Application Insights
   (traces, exceptions, availability test), CI on every pull request.
```

**Style.** A modular monolith: one Django project, one database, one image used for both the web process and the jobs. This is deliberately not a microservice or SPA architecture; the scope is small and the team is small. Boundaries are enforced by module layout and by tests, not by network.

**Key decisions (recorded as ADRs in Appendix C)**

| Decision | Choice | Why |
|---|---|---|
| Framework | Django 5.2 LTS (supported to April 2028) | v2 already runs on Django 5; the team's skills and the SQL rules transfer; long support window |
| Database | PostgreSQL 16 on Azure Flexible Server | v2's data is PostgreSQL; views and window functions carry the aggregation; the Single Server named in the SDD is retired |
| Front end | Server-rendered templates + HTMX partials + one JavaScript bundle (Vite) | Smallest surface a two-person team can maintain; keeps pivottable.js; no SPA build to own |
| Aggregation | SQL views with bound parameters, plus a nightly materialised summary | One definition of "achieved" for the app and for Power BI; testable against v2 with golden fixtures |
| Jobs | Azure Container Apps Jobs running management commands from the same image | Out of the web process, observable, no duplicate runs on scale-out, no cron installed at boot |
| Auth | django-allauth Microsoft provider pinned to the UNICEF tenant; no self-provisioning | Replaces v2's placeholder SSO; accounts are created by an admin or matched to a pre-registered email |
| Files | Azure Blob via django-storages; nothing written into the package directory | v2 stored extracts in the container and documents in database rows |
| Secrets | Environment variables backed by Key Vault references; none in the repository | v2 committed every credential |

---

## 4. Repository and module layout

```
neurodb/                       # new repository, clean history, .gitignore from commit one
  pyproject.toml               # dependencies (prod / dev groups), ruff, pytest config
  uv.lock                      # hash-locked resolution
  Dockerfile                   # multi-stage, non-root, gunicorn CMD
  azure-pipelines.yml          # lint, test, migrations-check, audit, build, deploy, swap
  docs/                        # this design, ADRs, OPERATIONS.md (runbook), DATA_DICTIONARY.md (generated)
  config/
    settings.py                # single module, env-driven (django-environ)
    urls.py  wsgi.py  asgi.py
  neurodb/
    core/                      # ReportingYear, Section, Office, AdminArea, PopulationFigure, SyncRun
    indicators/                # Database, Activity, Indicator, SubIndicator, MasterIndicator, tags, NeuroReport, wizards
    facts/                     # ActivityRecord fact table, import parser, loader, aggregation views (migrations own the SQL)
    integrations/
      activityinfo/            # client, structure import, export-job data import
      etools/                  # client, per-entity sync, locations sync
    partnerships/              # eTools replica models + partner/PCA/donor services
    library/                   # Resource, ResourceType/Topic/Tag, Map
    reports/                   # page views, JSON views, exports (one module per page family)
    accounts/                  # User, roles, section scoping, allauth adapter
    web/                       # base template, components, static source (src/), Vite manifest loader
  tests/                       # mirrors the package; fixtures/ holds sample extract rows and golden outputs
  scripts/
    import_legacy.py           # one-off ETL from the v2 database (idempotent, reconciled)
```

Rules enforced by CI: no raw `cursor.execute` outside `facts/queries.py` and migrations (a test greps for it); no `|safe` in templates except through `json_script`; no third-party script tag without a pinned version and an integrity hash (a template lint); every view under `reports/` and `partnerships/` has a route smoke test.

---

## 5. Domain model

Conventions: every table has `id` (BigAutoField), `created_at`, `updated_at`. Replica tables from upstream systems carry `source_id` (the upstream id, unique) and `raw` (the upstream JSON payload, for provenance and re-parsing). Money is `DecimalField(max_digits=20, decimal_places=2)` with a 3-letter `currency`. Dates are `DateField`; timestamps are timezone-aware (`USE_TZ=True`, `TIME_ZONE='Asia/Beirut'`). Enumerations are `TextChoices`. Foreign keys to replica tables use `PROTECT`; admin delete links are disabled on synced tables.

### 5.1 Core

| Model | Fields | Notes |
|---|---|---|
| `ReportingYear` | `year` (PositiveSmallInteger, unique), `name`, `is_current` (Boolean) | Database constraint: at most one row with `is_current=True` (partial unique index). Replaces v2's text year and `clean()` check. |
| `Section` | `code` (unique), `name`, `logo` (ImageField on Blob), `powerbi_url` (URL, nullable), `has_hpm_indicators` | Carried from v2 `users.Section` |
| `Office` | `code`, `name`, `source_id` | From eTools offices |
| `AdminArea` | `level` (GOVERNORATE / DISTRICT / CADASTER), `code` (unique per level), `name`, `parent` (FK self), `activityinfo_code`, `geometry` (JSONField GeoJSON) | Replaces three v2 tables with polygons as 64,500-character strings. Loaded once from GeoJSON fixtures, not per request. |
| `PopulationFigure` | `year`, `nationality` (LEB/SYR/PRS/PRL/OTH/ALL), `admin_area` (FK), `age_group`, `sex`, `value`, `source`, `vulnerability_level` (nullable) | Replaces the year-stamped JSON and Excel files read per request. Loaded by `load_population_figures --year 2026 file.xlsx`. Unique on (year, nationality, admin_area, age_group, sex, vulnerability_level). |
| `SyncRun` | `job` (ACTIVITYINFO_STRUCTURE / ACTIVITYINFO_DATA / ETOOLS / LOCATIONS), `target` (e.g. database id), `started_at`, `finished_at`, `status` (RUNNING/SUCCEEDED/FAILED/PARTIAL), `rows_in`, `rows_written`, `rows_failed`, `error`, `log_blob` | One row per job execution; the admin and the health page read it; alerts fire on `FAILED` or on staleness. |

### 5.2 Indicators (configuration managed by admins)

| Model | Fields | Notes |
|---|---|---|
| `Database` | `reporting_year` (FK), `section` (FK), `activityinfo_database_id`, `activityinfo_folder_id`, `name`, `label`, `is_funded_by_unicef_only` (Boolean, default True), `has_offices`, `is_hpm`, `dashboard_link` (URL), `last_structure_import` (FK SyncRun), `last_data_import` (FK SyncRun), `is_active` | The v2 per-database ActivityInfo username/password columns are dropped; one service token lives in Key Vault. The 26 commented-out `funded_by` filters in v2 SQL become this one explicit flag. |
| `Activity` | `database` (FK), `form_name`, `name`, `activityinfo_form_id`, `activityinfo_folder_id`, `parent_form_id` | Unique on (database, activityinfo_form_id) |
| `Indicator` (v2 `IndicatorNew`, the leaf) | `activity` (FK), `activityinfo_indicator_id`, `awp_code`, `name`, `units`, `type`, `gender`, `nationality`, `disability`, `programme`, `age_group`, `tags_locked` (Boolean) | Unique on (activity, activityinfo_indicator_id). Tag fields are derived by `parse_indicator_tags(name)` on import unless `tags_locked` (admin override). The v2 age-group table (`<=18`, `0-59 months`, and 60 other spellings) becomes a single mapping module with a unit test per spelling. |
| `SubIndicator` | `database` (FK), `awp_code`, `name`, `target` (Integer, nullable), `aggregation` (SUM/AVERAGE), `indicators` (M2M Indicator) | |
| `MasterIndicator` | `database` (FK), `awp_code`, `name`, `target` (nullable), `ram_result` (nullable), `aggregation` (SUM/AVERAGE/MIN/MAX/COUNT/SUM_OVER_SUM), `reporting_level` (CADASTER/DISTRICT/GOVERNORATE/NATIONAL/INSTITUTIONAL), `tags` (M2M IndicatorTag), `sequence` | v2's `GATEWAY` value displayed as "DISTRICT" is resolved to one name. Target nullable, never 0 by default: the v2 default of 0 caused a division-by-zero crash. |
| `MasterSubIndicator` (through) | `master` (FK), `sub` (FK), `effect` (TOTAL/NO_EFFECT/NUMERATOR/DENOMINATOR), `show_on_dashboard`, `target`, `sequence` | Unique on (master, sub) |
| `IndicatorTag` | `name` (unique) | |
| `NeuroReport` | `reporting_year` (FK), `name`, `is_hpm`, `sequence` | |
| `NeuroReportMasterIndicator` (through) | `report` (FK), `master` (FK), `label_override`, `target_override`, `sequence` | |
| `NeuroReportComment` | `report` (FK), `master` (FK, nullable), `period` (Date), `text`, `author` (FK User) | |

Both admin wizards ("Add Sub Indicators" with bulk effect assignment; "Add Master Indicators" by selection or by tag filter) are re-implemented as admin views using `get_inlines`/`get_fields` (never mutating the ModelAdmin instance, which was thread-unsafe in v2).

### 5.3 Facts

| Model | Fields | Notes |
|---|---|---|
| `ActivityRecord` | `database` (FK), `indicator` (FK Indicator, nullable, indexed), `activityinfo_indicator_id` (kept for records whose indicator is not yet imported), `activityinfo_record_id`, `form_id`, `period` (Date, first day of the reporting month), `value` (Decimal), `partner` (FK Partner, nullable), `partner_name_raw`, `project_code`, `project_name`, `funded_by`, `governorate` / `district` / `cadaster` (FK AdminArea, nullable) plus the raw codes, `site_name`, `latitude` / `longitude` (Float, nullable), `reporting_section`, `is_emergency`, `is_covid`, `last_edited_at` (DateTime), `raw` (JSON) | Replaces the 61-column string-typed `ActivityReportNew`. Indexes: (database, period), (database, indicator, period), (indicator, period), (partner), (project_code). Unique on (database, activityinfo_record_id, activityinfo_indicator_id) so the loader can upsert instead of delete-and-reinsert. |

**Import row rules** (ported from v2 `add_rows`, each a pure function with unit tests on rows from the committed 2025 extract):

- `period` is taken from `month_of_reporting` (the populated column in current extracts), falling back to `month`; rows whose year is outside the database's reporting year plus or minus one are rejected and counted (v2 let a year of 2925 through).
- `partner_name_raw` is normalised by the v2 rules (case, known aliases) and matched to `Partner` by vendor number when available, else by normalised name.
- `is_emergency` and `is_covid` come from the v2 keyword lists, held in `facts/rules.py` with the lists as data.
- The nutrition special case (`year >= 2025 and database code ends in 18`) is carried as a `Database.default_view` setting rather than a hard-coded branch.
- Admin-area codes map to `AdminArea` rows; unknown codes are stored raw and counted.

### 5.4 Aggregation views

The 18 live v2 SQL constants collapse into three views and one summary table. All are created and versioned in migrations under `facts/`.

- `v_subindicator_values(sub_indicator_id, database_id, period, value)`: sum or average of `ActivityRecord.value` over the sub-indicator's linked indicators, per period, honouring `Database.is_funded_by_unicef_only`.
- `v_masterindicator_values(master_indicator_id, database_id, period_to, value, numerator, denominator)`: applies `effect` per link and `aggregation` per master; `SUM_OVER_SUM` returns `NULL` when the denominator is zero (v2 crashed).
- `v_neuroreport_values(report_id, master_indicator_id, period_to, cutoff_at, value, previous_value)`: values to the end of a month or quarter with the "last edited before cut-off" rule (a report for June viewed in September excludes records edited after 17 July), and the previous period for deltas.
- `mv_dashboard_summary`: a materialised view refreshed at the end of every data import, holding master-indicator values, targets, and tracking status per database for the current reporting year. Dashboards read this; the analytical pivot reads the fact table directly.

Tracking status (v2 `setTrackingStatus`) is a SQL function: given value, target, and the fraction of the year elapsed, returns ON_TRACK / OFF_TRACK / OVER_TARGET using the ±10 percentage-point rule from the SDD, and `NO_TARGET` when the target is null.

Golden tests: for every v2 constant, the ETL rehearsal database (section 13) stores v2's output as a fixture; a test asserts that the v3 view reproduces it row for row, and every intentional divergence (zero-target guard, `funded_by` flag, month source) is listed in `docs/DIVERGENCES.md` with the product owner's decision.

### 5.5 Partnerships (eTools replicas)

| Model | Key fields | Notes |
|---|---|---|
| `Partner` | `source_id` (unique), `vendor_number` (indexed), `name`, `short_name`, `partner_type`, `cso_type`, `rating`, `hact_min_requirements`, `total_ct_cy` (Decimal), `is_hidden`, `is_deleted_upstream`, `raw` | v2's `staff_members` JSON of names, emails, and phones is **not** replicated unless the data-protection review (section 10.5) approves it; the partner profile links to eTools for staff. |
| `Agreement` | `source_id`, `partner` (FK), `number`, `type`, `status`, `start`, `end`, `raw` | |
| `ProgrammeDocument` (v2 `PCA`) | `source_id`, `partner` (FK), `agreement` (FK nullable), `number` (indexed), `title`, `document_type`, `status`, `start`, `end`, `country_programme`, `sections` (M2M Section), `offices` (M2M Office), `total_budget`, `unicef_cash`, `unicef_supplies`, `currency`, `location_codes` (ArrayField), `raw` | Indexes on status, end, document_type. |
| `FundingLine` | `programme_document` (FK), `reservation_number`, `donor_code`, `donor_name`, `grant`, `amount`, `currency` | Replaces the v2 loop that kept only the last reservation's lines. |
| `Engagement` | `source_id` (unique), `partner` (FK), `type` (audit / spot check / micro-assessment / special audit), `status`, `start`, `end`, `date_of_report`, `findings_count`, `displayed_status`, `raw` | Local surrogate key; v2 reused the remote id as primary key. |
| `ActionPoint` | `source_id`, `engagement` (FK), `status`, `due_date`, `description`, `raw` | |
| `Travel` | `source_id` (unique), `reference_number` (unique), `partner` (FK nullable), `section`, `office`, `traveler_name`, `status`, `start`, `end`, `raw` | Traveller names are UNICEF staff personal data; retention rule in section 10.5. |
| `TravelActivity` | `travel` (FK), `type`, `date` (from the activity, not the trip), `partner` (FK), `programme_document` (FK nullable), `location` (FK Location nullable) | |
| `Location` | `source_id`, `p_code` (unique), `name`, `type` (FK LocationType), `parent` (FK self), `latitude`, `longitude` | Tree via `parent`; MPTT is dropped (v2 never populated it). |

### 5.6 Library and maps

| Model | Notes |
|---|---|
| `Resource` | `title`, `abstract`, `type` (FK), `topic` (FK), `section` (FK), `tags` (M2M), `link` (URL), `file` (FileField on private Blob container), `cover` (ImageField), `year`, `is_published`, `uploaded_by`. Download goes through an authenticated view that returns a short-lived signed URL. |
| `Map` | `title`, `status`, `external_url`, `section`, `thumbnail` |

### 5.7 Accounts

| Model | Notes |
|---|---|
| `User` (custom, from the first migration) | `email` (unique, lower-cased), `entra_object_id` (unique, nullable), `section` (FK nullable), `office` (FK nullable), `role` (VIEWER / SECTION_EDITOR / ADMIN), `is_active`. Username is the email. |
| Groups and permissions | Django's permission system is used as-is; v2's admin base class that returned `True` for every permission check is not carried over. |

---

## 6. Integrations and scheduled jobs

### 6.1 Common client design

`integrations/http.py` provides one `requests.Session` factory with: timeouts (connect 10 s, read 120 s), retry with backoff on 429/5xx (5 attempts), an `Authorization` header from settings, structured logging of method, path, status, and duration (never of bodies or tokens), and a `RateLimiter`. Every upstream call goes through it. Tests use recorded responses (`responses` library) from sanitised fixtures.

Tokens: `ACTIVITYINFO_TOKEN`, `ETOOLS_TOKEN` (and nothing else) read from the environment; rotation is a Key Vault update and a job restart, no code change. Service accounts are requested from both platforms before cutover; until then the personal tokens live only in Key Vault.

### 6.2 ActivityInfo

- **Structure import** (`import_activityinfo_structure --database <id>` or admin action): `GET /resources/databases/{database_id}` for folders and forms, `GET /resources/form/{form_id}/schema` for fields; upserts `Activity` and `Indicator`, re-derives tags unless locked, marks indicators absent upstream as `is_active=False` (never deletes, because facts reference them).
- **Data import** (`import_activityinfo_data [--database <id>] [--period YYYY-MM]`): creates an export job (`POST /resources/jobs`, `exportDatabaseForms` with a year filter), polls with a bounded loop (maximum 150 attempts, 2 s apart), streams the result to Blob (`extracts/{database}/{date}.csv`), parses rows through the pure rule functions, and loads them into a staging table with `COPY`, then inside one transaction upserts into `ActivityRecord` on the unique key and deletes rows for that database and period that are absent from the extract. On success it refreshes `mv_dashboard_summary` and sets `Database.last_data_import`. On any exception the transaction rolls back, the `SyncRun` is `FAILED` with the error, and nothing is stamped.
- Schedule: daily at 18:00 Beirut time on days 1 to 22 (v2's cadence), plus on demand from the admin.

### 6.3 eTools

One command per entity (`sync_etools_partners`, `_agreements`, `_programme_documents`, `_engagements`, `_travels`, `_action_points`, `_locations`) and an orchestrator `sync_etools` that runs them in dependency order, each in its own transaction and `SyncRun`. Rules ported from v2 with the defects removed: pagination follows `next` links from page 1 (v2 started at page 45); per-item errors are logged with the item id and counted, not swallowed and not fatal to the run; the partner foreign key is resolved for every programme document (v2 set it only for documents with donors); all funding reservations are kept; travel activities take their own date; missing upstream parents are recorded as `PARTIAL` with the ids, and the next run retries them. Items that disappear upstream are tombstoned (`is_deleted_upstream=True`) rather than deleted, and hidden from pages by default.

Schedule: 20:30 Beirut daily for the orchestrator; locations at 05:00.

### 6.4 Job runtime

Azure Container Apps Jobs (or App Service WebJobs if the app stays on App Service) run `python manage.py <command>` from the same image with the same environment. Each job has a concurrency limit of one, a timeout, and a retry policy of zero (a failed run is visible, not silently repeated). A `check_sync_freshness` command runs hourly and raises an alert if the last `SUCCEEDED` run of any job is older than its schedule plus six hours. `/healthz` returns database and Blob connectivity plus the freshness status for the App Service health probe.

---

## 7. Application services and JSON endpoints

Views never touch the database directly. Each page family has a service module returning plain data (dataclasses or dicts) that both the HTML view and the JSON view use:

| Service | Provides | Backed by |
|---|---|---|
| `reports/services/dashboard.py` | master indicators with values, targets, status, sub-indicator breakdown for a database and year | `mv_dashboard_summary`, `v_masterindicator_values` |
| `reports/services/analytical.py` | fact rows for the pivot, filtered by database, period range, partner, area, funded-by | `ActivityRecord` with `select_related`, paginated, gzip streamed as compact JSON (v2 pretty-printed tens of megabytes) |
| `reports/services/map.py` | counts and values per admin area and per site, with GeoJSON from `AdminArea` (cached per level) | fact table aggregates; no per-row lookups |
| `reports/services/neuroreport.py` | report values to period, deltas, comments, HPM PDFs | `v_neuroreport_values` |
| `partnerships/services/*.py` | programme-document dashboards and summaries, donor mapping (interventions, funds since 2014, reported indicators, planned versus actual locations, funding history), partner list and profile | replica tables with `annotate`, no raw SQL |
| `reports/services/population.py` | population, children, most-vulnerable breakdowns | `PopulationFigure` |
| `reports/services/exports.py` | CSV/XLSX for raw data, ActivityInfo summaries, eTools locations, interventions | `csv.writer` on a streaming response; `openpyxl` write-only mode |

JSON endpoints live under `/api/internal/` (Django REST Framework, `IsAuthenticated` default, session authentication only, no browsable API in production, throttled). They are consumed by the pages' own JavaScript and are versioned by path only when a breaking change is unavoidable. The v2 `load_*` names are kept as a mapping table in Appendix A for the migration of bookmarks.

Power BI: a read-only database role `neurodb_bi` with `SELECT` on the three views, `mv_dashboard_summary`, and the replica tables minus personal-data columns; connection details in Key Vault; the Section's `powerbi_url` continues to link to published reports.

---

## 8. Web user interface

### 8.1 Stack

- Django templates with one base layout (`web/templates/base.html`), a component library of includes (page header, filter bar, data table, status pill, empty state, error state, spinner).
- HTMX for partial updates (filter changes, modal content, pagination) so most pages need no page-specific JavaScript.
- One JavaScript bundle built with Vite from `web/src/` (TypeScript optional, ES modules): `pivot.js` (pivottable.js with the nine custom aggregators and preset views ported from v2), `charts.js` (Plotly, pinned, the one charting library; Chart.js and Highcharts are not used), `map.js` (MapLibre GL with OpenStreetMap-style tiles, or Google Maps if the country office prefers, behind one adapter), `filters.js` (URL-state filters), `fetch.js` (one helper with error toast and empty-state rendering).
- Bootstrap 5.3 and a small custom stylesheet with design tokens; AdminLTE is not used. Django admin keeps Jazzmin for the configuration screens.
- Server data reaches JavaScript only through `json_script`; there is no `|safe` and no `innerHTML` with unescaped data.
- Content Security Policy with nonces; every third-party asset is vendored through npm and served by WhiteNoise with a manifest; no CDN scripts.

### 8.2 Page inventory and behaviour

| Area | Pages (v3 path) | Behaviour carried from v2 | Changes |
|---|---|---|---|
| Home | `/` | Sections with their current-year databases, Neuro Reports, HPM reports, partnerships, donor, maps, library, population links; landing image | Navigation generated from the database (sections, databases, reports) by a context processor with active state and breadcrumbs; images optimised and lazy-loaded; no external analytics tag |
| Database | `/databases/<id>/` (dashboard), `/analytical/`, `/snapshot/`, `/map/`, `/raw-data/` | Header with last import, buttons, year switch; master-indicator table with status; sub-indicator modal; pivot with quick switch, presets, TSV export; print snapshot; map/chart/table by area and site | Status has visible text and colour; rows are buttons; raw data streams the latest extract from Blob; filters live in the URL |
| Neuro Reports | `/reports/<id>/`, `/reports/<id>/analytical/`, `/reports/<id>/hpm/` | Values to period, deltas, inline comments, PDF tables | PDFs from Blob; comments carry author and time; period selector in URL |
| Programme documents | `/programmes/`, `/programmes/<id>/analytical/`, `/programmes/summary/?scope=active|all` | Filters (section, office, partner, type, CSO type, status, donor, grant, year), detail popup, summaries | One template with a scope parameter (v2 had two 98%-identical files) |
| Donor mapping | `/donors/` | Interventions, funds since 2014, reported indicators, planned versus actual map, funding history, highlights pivot | Filters in URL (v2 lost them on refresh); all funding lines counted |
| Partnerships | `/partners/`, `/partners/<id>/` | List with filters; tabbed profile with engagements, interventions, visits | Staff tab links to eTools unless the data-protection review approves replication |
| Population | `/population/?view=total|children|vulnerable` | Charts and tables by nationality, area, age group | From the `PopulationFigure` table; year selectable |
| Library | `/library/`, `/library/<id>/download/` | Filters, search, pagination, download | Files on Blob; download authenticated and permission-checked |
| Maps | `/maps/` | Completed maps with external links | unchanged |
| Exports | `/exports/...` | ActivityInfo summaries, eTools locations, interventions, wrong PCA numbers | authenticated; streamed |
| Admin | `/admin/` | Indicator configuration, wizards, import actions, sync history, users | Import actions enqueue a job rather than running in a thread |

### 8.3 Cross-cutting UI rules

- Every AJAX request has success, error, and empty handling through the shared helper; failures show a message and a retry, never a blank page.
- Every filter state is in the query string; every filtered view is a shareable link.
- Accessibility baseline: semantic tables with header scopes, colour never the only encoding, keyboard-reachable interactive rows, labelled controls, alt text, focus styles, contrast checked; `axe-core` runs in CI on every page.
- Responsive: single column below 768 px, tables scroll inside their container, no fixed pixel widths.
- All user-facing strings wrapped for translation from the first template; `LANGUAGES` configured; RTL-capable layout classes reserved.
- Page weight budget: under 600 KB transferred for a dashboard, measured by Lighthouse in CI.

---

## 9. Authentication and authorisation

- Sign-in through Microsoft Entra ID via django-allauth's `microsoft` provider with the UNICEF tenant id pinned; `SOCIALACCOUNT_AUTO_SIGNUP=False`; the adapter links an incoming identity to an existing `User` by `entra_object_id`, or by exact email if the user was pre-registered by an admin, and otherwise refuses with a message. Username/password login remains available only for break-glass admin accounts with MFA enforced by the identity provider.
- Roles: **Viewer** (all pages, exports), **Section editor** (viewer plus comments on Neuro Reports of their section and library uploads for their section), **Admin** (indicator configuration, imports, users). Section scoping is applied in the services (a section editor sees all data but edits only their section's objects); a `NeuroDBPermission` DRF class and a view mixin enforce it.
- Django's permission system governs the admin; no permission bypasses.
- Sessions: secure, HTTP-only, SameSite=Lax cookies, 12-hour lifetime, rotated on login.
- The admin is served at a non-default path from settings and is reachable only to Admin-role users.

---

## 10. Security and data protection

1. **Secrets**: none in the repository; App Service or Container Apps settings reference Key Vault; a secret scanner (gitleaks) runs in CI; the old repository's secrets are treated as compromised and rotated before v3 goes live.
2. **Database access**: the application role owns only its schema and has no superuser rights; the BI role is read-only; the database accepts connections only from the app's virtual network.
3. **Input handling**: all queries through the ORM or bound parameters in `facts/queries.py`; ids validated as integers; enumerations validated against choices; file uploads limited by type and size and stored in a private container.
4. **Headers and transport**: HTTPS only at the edge, `SECURE_SSL_REDIRECT`, HSTS one year, `X-Frame-Options: DENY`, `Referrer-Policy`, nonce-based CSP, `ALLOWED_HOSTS` explicit.
5. **Personal data**: a data-protection record is written before cutover listing each personal-data field (partner staff contacts, traveller names, user accounts), its purpose, retention, and who can see it. Default decisions proposed: do not replicate partner staff contacts (link to eTools instead); keep traveller names only for the current and previous reporting year; purge tombstoned upstream rows after 12 months; no personal data in exports available to Viewers.
6. **Logging**: structured logs without request bodies or tokens; Application Insights with exception capture; audit log of admin changes to indicator configuration and of imports (who, when, what).
7. **Dependencies**: `pip-audit` and `npm audit` in CI fail the build on high or critical advisories; Renovate opens update pull requests; a quarterly review of the lockfile is part of operations.

---

## 11. Infrastructure and deployment

**Azure resources** (one resource group per environment, two environments: staging and production):

| Resource | Size | Purpose |
|---|---|---|
| Container Registry | Basic | Images tagged by git SHA |
| App Service (Linux, P1v3) or Container Apps (0.5 vCPU, 1 GiB, 1 to 2 replicas) | | Web process, staging slot for swaps |
| Container Apps Jobs | Consumption | Three scheduled jobs and on-demand imports |
| Azure Database for PostgreSQL Flexible Server 16 | B2ms to start | Point-in-time restore 14 days, geo-redundant backup optional |
| Storage account | Standard LRS, private containers `extracts`, `library`, `hpm` | Files |
| Key Vault | Standard | Secrets referenced from app settings |
| Application Insights + Log Analytics | Pay-as-you-go | Traces, exceptions, availability test on `/healthz` |
| Front Door or App Service managed certificate | | TLS for neuro-db.org |

Indicative running cost is in the same band as the SDD's figure for v2 (about 150 to 250 USD per month); it must be confirmed from Cost Management once the Flexible Server tier is chosen.

**Build and release**

- `Dockerfile`: multi-stage; stage one installs the locked dependencies and builds the Vite bundle; stage two is `python:3.12-slim` with a non-root user, `collectstatic` at build time, `gunicorn` as the command, no SSH daemon, no package installs at boot.
- `azure-pipelines.yml` on every pull request: `ruff check`, `ruff format --check`, `manage.py check --deploy`, `makemigrations --check`, `pytest` with a PostgreSQL service container, `pip-audit`, `npm audit`, `gitleaks`, template lint, image build. On `main`: push the SHA-tagged image, run migrations against staging, deploy to staging, run the smoke suite and `axe`, swap to production, run the availability test. Rollback is a swap back.
- Configuration: `config/settings.py` reads every value from the environment with no production defaults; `.env.example` documents the contract; a local `docker-compose.dev.yml` provides PostgreSQL and Azurite (Blob emulator) so a new developer runs the app in one command.

**Operations**

- Backups: platform PITR plus a nightly logical dump to Blob (retained 35 days); a restore rehearsal every quarter recorded in the runbook.
- Alerts: job failure or staleness, availability test failure, exception rate, database CPU and storage.
- Runbook (`docs/OPERATIONS.md`): deploy and rollback, secret rotation, yearly rollover (section 14), HPM PDF and population-figure uploads, sync failure triage, restore procedure.

---

## 12. Quality and testing

| Layer | What | Tooling | Gate |
|---|---|---|---|
| Unit | Import row rules, tag parsing (one test per age-group spelling), tracking status, filter parsing, services with factory data | pytest, factory_boy | 100% of `facts/rules.py` and `indicators/tags.py` |
| Golden | Every aggregation view against v2 outputs on the rehearsal database | pytest, fixtures generated by the ETL rehearsal | Row-for-row equality except documented divergences |
| Integration | Each sync command against recorded upstream responses, including pagination, partial failures, and tombstoning | responses, PostgreSQL service | Every command |
| Route smoke | Every URL: anonymous is redirected, viewer gets 200, section editor and admin permissions | pytest-django | Every route, enforced by a test that walks `urlpatterns` |
| Accessibility | Every page rendered with sample data | axe-core via Playwright | No serious violations |
| End to end | Login, dashboard, pivot preset, map filter, export download, admin wizard | Playwright | Ten scenarios, run on staging after deploy |
| Static | Lint, format, type hints on services (`mypy` optional), no raw SQL outside the allowed module, no `|safe` | ruff, custom checks | Every pull request |

Coverage target: 80 percent overall, 100 percent on rules and views. Code review: every change through a pull request with CI green and one reviewer; `CODEOWNERS` names the tech lead for `facts/` and `integrations/`.

---

## 13. Data migration from v2 and cutover

### 13.1 ETL (`scripts/import_legacy.py`, idempotent, re-runnable)

| v2 source | v3 target | Rules |
|---|---|---|
| `users_user`, `users_section`, `users_office` | `User`, `Section`, `Office` | Emails lower-cased; role derived from `is_superuser`/`is_staff` (Admin) else Viewer; passwords not migrated (SSO) except break-glass accounts |
| `pivoting_reportingyear` | `ReportingYear` | Year cast to integer; exactly one `is_current` |
| `pivoting_database` | `Database` | Flags mapped; ActivityInfo credentials dropped; `funded_by` flag defaulted from the v2 query behaviour and recorded per database |
| `pivoting_activity`, `pivoting_indicatornew` | `Activity`, `Indicator` | Tags re-derived and compared with v2 values; differences reported |
| `pivoting_subindicator`, `_masterindicator`, `_mastersubindicator`, `_masterindicatortag`, `_neuroreport*` | corresponding models | Targets of 0 become null (recorded); `GATEWAY` level mapped to DISTRICT |
| `pivoting_activityreportnew` | `ActivityRecord` | `period` from `month_name` where it parses, else re-imported from the Blob extract for that database; coordinates and values cast with failure counts; indicator FK resolved by (database, ActivityInfo id) |
| `etools_*` | `Partner`, `Agreement`, `ProgrammeDocument`, `FundingLine`, `Engagement`, `ActionPoint`, `Travel`, `TravelActivity` | Money and dates parsed from text; remote-id primary keys become `source_id`; partner staff contacts not copied unless approved |
| `locations_location`, `locations_locationtype` | `Location`, `LocationType` | unchanged |
| `pivoting_resource*`, `pivoting_map` | `Resource`, `Map` | Binary blobs written to Blob storage; database rows keep references |
| Governorate/district/cadaster polygon tables | `AdminArea` | Polygon strings converted to GeoJSON once; validated |
| `pivoting/uploads/*.json|xlsx` | `PopulationFigure` | Loaded by the population command |
| `activityinfo_*` (dead v1 tables) | not migrated | Exported to Blob as CSV for the archive, then dropped with the old database |

Every run writes a reconciliation report: row counts per table, coercion failures with sample ids, and the golden-test results. The ETL runs weekly against a fresh copy of production from Phase 1 onward, so cutover is a rehearsed operation of known duration.

### 13.2 Cutover plan

1. **Freeze** v2 after the review's Phase 0 hardening: no configuration changes in the v2 admin during the final week; a banner announces the switch.
2. **Parallel run, two weeks**: v3 jobs sync from upstream into the v3 database while v2 continues; a nightly comparison report (per database and month totals, partner counts, programme-document budgets) is reviewed by the product owner; discrepancies are resolved or recorded as divergences.
3. **Acceptance**: each section's focal point walks their pages on staging against v2 using the scope checklist; sign-off recorded.
4. **Switch**: final ETL of configuration tables (facts and replicas come from the v3 syncs, not from v2), DNS or Front Door switch of neuro-db.org, v2 App Service set to read-only for 30 days behind an internal hostname, then deleted; v2 repository archived; all v2 credentials confirmed rotated.
5. **After**: 30-day watch on alerts and user feedback; the yearly rollover rehearsed with the product owner before January.

The parallel run is kept deliberately short and one-directional (v3 reads upstream, never v2's database) to avoid the dual-operation burden the review warned about.

---

## 14. Yearly rollover and recurring operations

The January procedure that v2 left undocumented becomes an admin checklist page and a runbook section:

1. Create the new `ReportingYear` and mark it current (the constraint prevents two).
2. Create `Database` rows for the year with their ActivityInfo ids; run the structure import (admin action, runs as a job).
3. Wire master indicators and Neuro Reports through the wizards, or copy last year's configuration with the "clone year" admin action (new in v3) and adjust.
4. Upload population figures for the year (`PopulationFigure` loader, admin upload form).
5. Upload the HPM PDF tables to the library's `hpm` container from the admin.
6. Run the first data import; check `SyncRun` and the dashboard summary.

Other recurring operations: monthly review of `SyncRun` failures, quarterly dependency update and restore rehearsal, secret rotation on staff changes.

---

## 15. Delivery plan

| Phase | Weeks | Scope | Exit criteria |
|---|---|---|---|
| 0. Harden v2 (from the review) | 1-2 | Secrets rotated, endpoints gated, injections fixed, build installs, ground truth recorded | v2 safe to run unattended for the project's duration |
| 1. Foundation, schema, ETL | 3-6 | Repository, CI, image, settings, SSO, typed schema, admin with wizards, ETL against a production copy, golden fixtures captured | Empty v3 deploys to staging; ETL loads production in minutes with a reconciliation report; 60 percent unit coverage |
| 2. Integrations and jobs | 7-10 | ActivityInfo and eTools clients, import rules, loaders, `SyncRun`, jobs on schedule, freshness alerts | Nightly syncs run into staging in parallel with v2; comparison report matches within documented divergences |
| 3. Aggregation views | 11-13 | Three views, summary table, tracking function, golden tests, BI role | Golden tests green; Power BI connects |
| 4. Pages wave 1 | 14-21 | Home, database dashboard, analytical, snapshot, map, raw data, Neuro Reports, HPM, admin polish | Section focal points accept the database and report pages |
| 5. Pages wave 2 | 22-29 | Programme documents, donor mapping, partnerships, population, library, maps, exports | Full scope checklist accepted |
| 6. Parallel run and cutover | 30-33 | Comparison report, acceptance, switch, decommission, runbook | neuro-db.org served by v3; v2 retired |

**Team**: a senior Django/PostgreSQL engineer as tech lead (schema, views, integrations), a full-stack engineer (templates, HTMX, bundle, charts, pivot, maps, accessibility), and a half-time Azure/data engineer (jobs, Front Door, Key Vault, Flexible Server, CI, ETL tooling). The UNICEF innovation lead is product owner and parity judge, about one day a week. The former vendor engineer is interviewed against the scope checklist in the first month if reachable.

**Effort**: 11 to 16 person-months, 7 to 8 months elapsed. The estimate assumes the frozen scope and a product owner able to decide ambiguous rules within a week.

**Decision gates**: end of Phase 1 (ETL reproduces production and golden fixtures exist) is the go/no-go for building pages; end of Phase 4 (the most-used pages accepted) is the go/no-go for cutover planning. Funding should be committed through Phase 6 before Phase 1 starts; a stalled rebuild leaves two half-systems, which is the outcome the review warned against.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Undocumented rules in v2 SQL and import code | Golden fixtures and the divergence register; the running v2 site remains the oracle until cutover; vendor interview early |
| Scope creep | Frozen checklist; anything else is a written decision for after cutover |
| Data-quality surprises in v2 facts (empty months, bad coordinates) | Weekly ETL rehearsal with failure counts from Phase 1; period re-derived from Blob extracts where v2 rows are unusable |
| Upstream service accounts delayed | Requested in week 1; clients accept any token; escalated as a compliance item, not absorbed |
| Team continuity | Each phase leaves a shippable, documented increment; the runbook and ADRs are written as work proceeds, not at the end |
| Pivot and map parity | pivottable.js and its preset definitions are kept; per-page acceptance before the path switches |

---

## Appendix A: v2 to v3 route mapping

| v2 | v3 | Notes |
|---|---|---|
| `/` | `/` | |
| `/v2/database-dashboard?id=N` | `/databases/N/` | redirect kept for one year |
| `/v2/database-analytical?id=N` | `/databases/N/analytical/` | |
| `/v2/database-snapshot?id=N` | `/databases/N/snapshot/` | |
| `/v2/database-intervention-map/?id=N` | `/databases/N/map/` | |
| `/v2/database-download/?id=N` | `/databases/N/raw-data/` | authenticated, from Blob |
| `/v2/neuroreport-dashboard/?id=N`, `neuroreport-analytical`, `hpm-neuroreport/` | `/reports/N/`, `/reports/N/analytical/`, `/reports/N/hpm/` | |
| `/v2/pca-dashboard/`, `pca-analytical`, `pca-summary-active`, `pca-summary-all` | `/programmes/`, `/programmes/N/analytical/`, `/programmes/summary/?scope=active|all` | |
| `/v2/donor-dashboard/` | `/donors/` | |
| `/v2/partnerships/`, `partnership-profile/?id=N` | `/partners/`, `/partners/N/` | |
| `/v2/population-figures/` | `/population/` | |
| `/v2/Resources/`, `resource_file/N/` | `/library/`, `/library/N/download/` | |
| `/v2/Maps/` | `/maps/` | |
| `/v2/load_*` (14 feeds) | `/api/internal/...` | authenticated; consumed by the pages |
| `/v2/activityinfo-summary-download/` and the other three dumps | `/exports/...` | authenticated |
| `/etools/*` (12 legacy routes) | retired | superseded by the pages above |
| `/sso/*` | retired | allauth handles `/accounts/microsoft/` |
| `/locations/*` DRF API | retired | locations are internal; an authenticated autocomplete endpoint replaces it |

## Appendix B: settings contract (`.env.example`)

```
DJANGO_SECRET_KEY=            # Key Vault
DATABASE_URL=postgres://neurodb_app:...@...:5432/neurodb?sslmode=require
ALLOWED_HOSTS=neuro-db.org,www.neuro-db.org
CSRF_TRUSTED_ORIGINS=https://neuro-db.org,https://www.neuro-db.org
AZURE_STORAGE_ACCOUNT=  AZURE_STORAGE_KEY=  AZURE_CONTAINER_EXTRACTS=extracts  AZURE_CONTAINER_LIBRARY=library  AZURE_CONTAINER_HPM=hpm
ENTRA_TENANT_ID=  ENTRA_CLIENT_ID=  ENTRA_CLIENT_SECRET=
ACTIVITYINFO_TOKEN=  ETOOLS_TOKEN=  ETOOLS_BASE_URL=https://etools.unicef.org
APPLICATIONINSIGHTS_CONNECTION_STRING=
ADMIN_URL_PATH=manage/
TIME_ZONE=Asia/Beirut
```

## Appendix C: architecture decision records to write in Phase 1

ADR-001 Django 5.2 LTS as the framework; ADR-002 PostgreSQL views as the aggregation layer; ADR-003 HTMX and one bundle instead of a SPA; ADR-004 Container Apps Jobs for scheduling; ADR-005 Entra ID via allauth with pre-registered users; ADR-006 Blob storage for all files; ADR-007 no replication of partner staff contacts pending the data-protection record; ADR-008 upsert with tombstones instead of delete-and-reinsert; ADR-009 one charting and one map library; ADR-010 short one-directional parallel run rather than a long strangler migration.

## Appendix D: open questions carried from the review

These must be answered before Phase 1 closes; each changes the design if the answer is unexpected: the `month` column state in the live 2025 fact table; the real deployment shape and database of v2; backup and restore status; platform cost; the data-protection record; page timings and table sizes; provenance of the landing images; the yearly rollover as actually performed.
