# NeuroDB Technical Review and Recommendation

**Date:** 22 September 2026
**Scope:** Source code, libraries, UI/UX, data model, deployment, tests, and documentation of the `UNICEFLebanonInnovation/NeuroDB` repository (branch `main`, single commit `cbacdde`), read against the vendor's Software Design Document v0.5 (Sept 2024) and `TECHNICAL_DESIGN.md` (2026).
**Decision requested:** enhance the existing code in place, or retire it and rebuild from scratch.

---

## 1. Executive summary

**Recommendation: rebuild from scratch (Option B), keeping the PostgreSQL data, the indicator domain model, and the Django framework. Before the rebuild starts, spend two weeks closing the live security holes in the current system, because it must keep running for another five to six months.**

NeuroDB does its job for users today, and the concept behind it is sound: a small analytics portal that mirrors ActivityInfo and eTools data into PostgreSQL and lets programme staff pivot, map, and export it. The problem is everything around that concept. The codebase is not a 2019 project but a 2016 project (the first migration is dated September 2016) that has been forked, half-rewritten, and patched on the production server by a single vendor engineer with no tests, no history, and no build that can be reproduced from the repository.

Ten independent review passes rated every dimension between 2 and 4 out of 10. The findings that decide the question are:

| What we found | Why it decides enhance vs rebuild |
|---|---|
| **Exploitable, unauthenticated SQL injection** on a live JSON endpoint, a second injection reachable by any logged-in user, and **25 data endpoints that answer anonymous requests** with the full ActivityInfo dataset, eTools budgets, partner staff contacts, and raw Excel exports. | Cheap to patch (days), but they are symptoms of an architecture with no data-access layer: SQL is assembled by string replacement inside a 1,841-line views module with the lowest possible maintainability index. Patching leaves the pattern that produced them. |
| **Every credential is in the repository**: the Django secret key, the database password (three places), the eTools API token (13 occurrences), the ActivityInfo token (two copies of the same client), an Azure Redis key, a staff username and password in a comment, and a cPanel password inside the design document. | Must be rotated regardless of the option. Under a rebuild, the new repository starts clean; under enhancement, the single squashed commit still carries them until a history rewrite. |
| **The build cannot be reproduced.** The production requirements file fails to install with a current pip, omits a package the main views module imports at load time, pins two libraries with 11 published CVEs (including the production HTTP server), and leaves Django itself unpinned. Five contradictory deployment paths coexist and none of them works as committed. | Under either option this layer is rebuilt, not repaired. There is no operational asset to preserve. |
| **The frontend has no upgrade path.** All page behaviour is 2,930 lines of inline jQuery inside templates on an end-of-life stack (AdminLTE 3, Bootstrap 4, Chart.js 2, jQuery UI with a known XSS). One live page still loads a script from `polyfill.io`, a domain that was hijacked to serve malware in 2024. Four base templates and 18 copy/old/orig templates coexist; four of five legacy `/etools/` pages crash before touching the database. | Moving to Bootstrap 5 or any modern stack means rewriting every template anyway. |
| **Zero tests for the business apps**, no linter has ever run (a stray `from this import d` prints the Zen of Python on every process start), 13 bare `except:` blocks, and the nightly syncs swallow every error while stamping "last updated" before they run. | A safety net for in-place refactoring costs an estimated 2.5 to 3 months of test-seam work, which is the same order of effort as reimplementing the two core modules cleanly. |
| **The good parts are portable.** The code already boots on Django 5.2 with zero errors. The indicator hierarchy (Reporting Year, Database, Master, Sub, Leaf indicators, Neuro Reports) matches the design document and is worth keeping as-is. The eTools replica tables have clean unique keys and idempotent syncs. The SDD is an accurate functional specification of about 25 pages, 14 JSON feeds, 6 exports, 2 integrations, and 9 admin-managed models. | The rebuild is bounded and well specified: the data, the domain model, and the SQL business rules transfer as a spec. This is what makes a rebuild the lower-risk option rather than the higher-risk one. |

**Why not enhance in place?** Because the honest enhancement plan is a rewrite conducted inside a hostile repository: 92 percent of tracked files are generated or vendored junk, a dead 2019 app with 193 migrations cannot be deleted without migration surgery, the schema stores dates, money, and codes as free text, and the views, sync, and template layers would each be replaced rather than edited. The enhance option costs about the same, takes longer to become safe, and ends with a worse asset.

**What must happen this week, regardless of the decision:** rotate every credential listed in section 4.1; add a login requirement to every data endpoint; fix the two SQL injection sites; remove the `polyfill.io` script; restrict `ALLOWED_HOSTS`. This is roughly two developer-weeks and is the first phase of the roadmap in section 8.

---

## 2. How this review was done

- **Static review** of every Python module (15,802 lines outside migrations), all 153 templates, the settings and deployment files, and both design documents, by ten specialised review passes (pivoting app, etools app, supporting apps, security, dependencies, UI/UX, data model, deployment and operations, tests and quality, documentation versus reality).
- **Executed checks** in an isolated environment: installing the production requirements on Python 3.11, `pip-audit`, Django's `check --deploy` and `makemigrations --check`, template compilation of every routed page, `pyflakes`, `radon` complexity and maintainability, `vulture`, and running the existing test suite.
- **Verification.** Every critical finding cited in this report was re-read at the cited line by the review lead. A second, adversarial verification pass over the high and critical findings was run; its results are recorded in Appendix B. Severity uses the usual scale: critical means exploitable today or blocks the project; high means a real defect with user or security impact; medium is a maintainability or correctness risk; low is hygiene.
- **Not done.** No live system was accessed. Production configuration, the real database schema, the Azure resources, and the cPanel host could not be inspected, so claims about "what runs today" are inferred from the repository and the design document and are flagged as such.

Secret values (keys, passwords, tokens) are deliberately not reproduced in this document. They are identified by file and line only.

---

## 3. Scorecard

| Dimension | Health (1-10) | Headline numbers |
|---|---|---|
| Security | **3** | 2 SQL injection sites (1 anonymous); 25 endpoints without login; 8 distinct committed secrets; 0 CSRF exemptions (good); 24 CDN scripts without integrity hashes |
| Source code: `pivoting` (main app) | **3** | 8,448 lines; `views.py` 1,841 lines with maintainability index 0.00; `queries.py` 1,702 lines of raw SQL, 25% dead; 57% of `utils.py` references models that no longer exist; 7 functions over 100 lines |
| Source code: `etools` | **3** | 4 of 9 routed pages crash on a removed model field or a Python 2 idiom; sync starts at page 45 of the travel API; 1 index in 1,373 model lines; 28 cascade deletes reachable from admin |
| Supporting apps and settings | **3** | Settings chosen by operating system; two half-configured auth systems; dead `activityinfo` app locked in by migration dependencies; admin permission checks overridden to always return True |
| Libraries and dependencies | **3** | Production requirements do not install; Django unpinned; 11 CVEs in the two pinned runtime packages; 7 abandoned packages; ~26 unused packages; frontend stack end-of-life |
| UI/UX | **4** | Consistent live theme and a genuinely useful pivot tool; but 2,930 lines of inline JS, 0 AJAX error handlers, 4 base templates, 18 junk templates, colour-only status badges, 12px forced body text, no i18n |
| Data model and migrations | **4** | Sound indicator hierarchy and fact table; but dates, money, and codes as strings, fact-to-indicator join on an unindexed nullable text column, 345 migrations from 9 Django versions, models already drift from migrations |
| Deployment and operations | **2** | 5 deployment paths, none runnable; image runs as root with SSH; cron inside the web container; no health endpoint, no error tracking; database platform named in the SDD retired by Microsoft |
| Tests and quality gates | **2** | 0 tests in business apps; 2 of 16 boilerplate tests pass; 0 of 9 quality-gate config files; CI has no test or lint step; 184 pyflakes findings including 2 latent NameErrors |
| Documentation versus reality | **3** | The SDD is a good functional spec; the 2026 handover doc is wrong on 21 of 31 material claims a new team would rely on; bus factor of one; one squashed commit |

---

## 4. Findings by area

### 4.1 Security

Findings marked **[verified]** were reproduced by reading the cited code during this review.

| # | Severity | Finding | Where |
|---|---|---|---|
| S1 | Critical **[verified]** | **Unauthenticated SQL injection.** The `emergency` query parameter is spliced into the NeuroReport SQL by string replacement; `.title()` does not neutralise quotes. The view has no login check. An anonymous visitor can read any table, including user password hashes and eTools partner data. | `pivoting/views.py:70-81`, placeholder at `pivoting/queries.py:668` |
| S2 | Critical **[verified]** | **25 data endpoints answer anonymous requests.** All 14 `load_*` JSON feeds, the raw-data Excel export, the library file download, three CSV/Excel table dumps, and the "wrong PCA numbers" list in `pivoting`; the interventions CSV export and donor-locations feed in `etools`; and the locations REST API. Page views are protected with `LoginRequiredMixin`; the data behind them is not. | `pivoting/urls.py:5-18, 41-45`, `pivoting/views.py:59-760, 1190, 1706-1841`, `etools/views.py:145, 441` |
| S3 | Critical **[verified]** | **Live credentials committed.** eTools API token (13 occurrences across `etools/tasks.py`, `locations/tasks.py`, `pivoting/tasks.py`), ActivityInfo token as a default argument in two client copies, Django `SECRET_KEY` and database password in `azureproject/production.py` and `local.py`, a third database password in `compose/production/django/entrypoint-copy`, an Azure Redis access key in a settings comment, and a staff username and password in a comment in `pivoting/utils.py:798`. The SDD v0.5 prints a cPanel login URL with password and the Azure database admin username. | See file references; values withheld |
| S4 | High **[verified]** | **Authenticated SQL injection** through `.extra(where=...)` built from the `donor` query parameter. | `etools/views.py:106` |
| S5 | High **[verified]** | **Unauthenticated write access to locations.** The Django REST Framework viewset includes create and update mixins, and `REST_FRAMEWORK` is never configured, so permissions default to AllowAny. | `locations/views.py:17-24`, no `REST_FRAMEWORK` in `azureproject/` |
| S6 | High | **Partner PII committed and served.** `output.txt` at the repo root is an eTools partner dump (240 organisations, 190 email addresses, 215 phone numbers). `pivoting/AIReports/` holds 49 MB of raw ActivityInfo records served anonymously via `/v2/database-download/`. | `output.txt`, `pivoting/views.py:1190-1222` |
| S7 | High **[verified]** | **Hand-rolled Microsoft SSO** ships with placeholder client id, tenant `common` (any Microsoft account, personal included), auto-creates active users keyed on a mutable email claim, validates no id token or nonce, and ends in `redirect('dashboard')`, a URL name that does not exist. It is routed and linked from the login page. Not exploitable today only because the placeholder id makes Microsoft reject the request. | `azureproject/sso_views.py:9-11, 81-91`, `templates/account/login.html:36` |
| S8 | High | **Compromised CDN.** The donor dashboard loads `https://polyfill.io/v3/polyfill.js`. | `templates/pivoting/donor-dashboard.html:18` |
| S9 | Medium **[verified]** | Settings hardening gaps: `ALLOWED_HOSTS` starts with `"*"`, `SECURE_SSL_REDIRECT=False`, HSTS 60 seconds with preload, no Content Security Policy, `USE_TZ=False`. `production.py`'s star import silently overwrites `CSRF_TRUSTED_ORIGINS`, dropping the Azure slot hostnames the pipeline deploys to. | `azureproject/settings.py:19-21, 146`, `production.py:29-36` |
| S10 | Medium | 24 CDN scripts without Subresource Integrity, Plotly pinned to `latest` on 9 pages, jsPDF 1.5.3 with 15 published advisories, two Google Maps browser keys in 5 templates, three Power BI "publish to web" links (anonymous by design) and Google Tag Manager on every authenticated page. | `templates/pivoting/*.html`, `templates/base*.html` |
| S11 | Medium | 16 template lines render server JSON with `|safe` inside `<script>`, and API data is injected via string concatenation and `innerHTML` on the dashboards. Stored XSS surface from upstream-controlled names and labels. | `templates/etools/interventions.html:173`, `templates/pivoting/database-dashboard.html:164, 194` |
| S12 | Medium | `django.views.static.serve` mounted unconditionally for `/media/` and `/static/`; image runs as root with an SSH daemon config allowing root password login; nginx configs allow TLS 1.0/1.1. | `azureproject/urls.py:25-27`, `compose/production/django/sshd_config`, `compose/production/nginx/*.conf` |
| S13 | Medium | `utils.CustomModelAdmin` overrides `has_change_permission`, `has_delete_permission`, and `has_view_permission` to return True, so any staff account can edit or delete indicators, databases, partners, and sections regardless of assigned permissions. | `utils/custom_model_admin.py:35-45` |

### 4.2 Source code

**`pivoting` (the business-critical app, 8,448 lines).**

- **Architecture.** Business logic lives in 1,702 lines of hand-written SQL constants and 1,841 lines of views. Models are anemic; views compute tracking status, month arithmetic, report titles, and even Bootstrap CSS class names (`views.py:877-881`). The four-query dashboard block is copy-pasted between `DatabaseDashboardView` and `DatabaseSnapshotView`.
- **Raw SQL.** 31 execute sites in `views.py`. Most bind parameters correctly; 11 assemble SQL with `str.replace()` or f-strings, one from user input (S1), three from database-sourced PCA numbers (second-order risk, and an empty filter produces an `IN ()` syntax error and a 500).
- **Dead code.** 434 lines (25%) of `queries.py` are unused constants, one containing invalid `==` SQL that proves it never ran. About 57% of `utils.py` references models that no longer exist (`Indicator`, `ActivityReport`, `LiveActivityReport`, `AdminLevelEntities`) yet is still wired to admin actions that will crash. `gistfile.py` is a Python 2 CSV writer that emits `b'...'` byte representations into downloads (verified) and is still used by two export views. A 71-line view named `load_intervention_mapping_dataxxx` is referenced nowhere.
- **Import pipeline.** The nightly ActivityInfo import deletes a database's roughly 52,000 fact rows and re-inserts them one `INSERT` at a time with no transaction; failures are counted and printed, never raised. Admin actions start the same work in daemon threads inside the gunicorn worker. The export-job poll loop has no attempt limit. The "last updated" timestamp is written before the import runs.
- **Robustness.** 9 bare `except:` blocks, 19 `print()` calls, 13 `Model.objects.get(id=id)` calls that return HTTP 500 on any bad id, an unguarded division by `len(items)`, and `.first().attribute` on possibly-empty querysets.
- **Performance.** N+1 loops (one `COUNT` per PCA in the donor feed; one polygon lookup per location), the same queryset evaluated four times, and the analytical feed returns an estimated 60 MB of pretty-printed JSON for the largest database.
- **Hygiene.** Star imports in `urls.py`, `views.py`, and `admin.py`; 87 pyflakes findings; 16 of 17 files with CRLF line endings; zero tests.

**`etools` (3,918 lines).**

- Four of nine routed pages are provably broken: three query a `Location.point` field that was commented out of the model (FieldError), and the trips monitoring page calls `json.dumps(dict.values())`, a Python 2 idiom that raises TypeError on Python 3. The pages that render do so through 20 copy-pasted raw SQL blocks with N+1 loops inside. Three of the pages extend `base2.html`, which fails to compile because it uses unregistered template tags.
- The sync layer uses `http.client` with no timeout, no retry, no pagination handling; the travel sync starts at a hard-coded page 45 and discards a whole 1,000-item page on any bad row; one missing partner aborts every later step of the nightly run; a funding-reservation loop keeps only the last reservation's line items, under-reporting donor funding; `TravelActivity.date` is set from the trip's start date instead of the activity's own date, mis-attributing visits by year.
- The schema mirrors the eTools JSON verbatim: money, dates, and booleans stored as `CharField`, duplicated columns (`end`/`end_date`), one index in 1,373 lines, remote ids reused as local primary keys for three models, 28 cascade deletes reachable from admin delete links. `admin copy.py` (376 lines) is a stale snapshot; seven cloned eTools models are unused; one references a django-tenants attribute that does not exist.
- `etools/admin.py:3` contains `from this import d`, which prints the Zen of Python to stdout on every process start.

**Supporting apps and configuration.**

- Settings are selected by `os.name == 'nt'`, so every Linux developer, CI runner, and container silently gets `production.py`. Only three environment variables are ever read. `asgi.py` points at a different settings module than `manage.py` and `wsgi.py`.
- The `activityinfo` app is confirmed dead at runtime (no imports, empty admin, `urls.py` cannot even import) but cannot be removed by deleting the directory: `etools/migrations/0052` depends on `activityinfo/0097`, and `activityinfo` migrations depend on `etools` and `users`. `pivoting.Database` is a field-for-field copy of `activityinfo.Database`; `pivoting/client.py` is a copy of `activityinfo/client.py` with cosmetic drift and the same embedded token.
- The `users` app is unadapted cookiecutter boilerplate with a duplicate admin form set and tests that cannot run. The `locations` app has two routes that use regex syntax inside `path()` and have been unreachable since the Django 2 migration, and two DRF views that raise `FieldDoesNotExist` on removed GIS fields.
- Celery is pinned but dead (its module is never imported; all settings are commented out); the production cache points at a Docker hostname that does not exist on App Service with exceptions silenced; email goes through SendGrid with an empty API key, and error emails are addressed to the vendor's engineer.
- Django's own deployment check, run during this review on Django 5.2.17, passes with 13 warnings and 0 errors once the missing `openpyxl` package is added. This is the strongest positive signal in the codebase: the Python code is already on modern Django idioms.

### 4.3 Data model and migrations

- **Worth keeping.** The indicator hierarchy (`ReportingYear` -> `Database` -> `Activity` -> `IndicatorNew` -> `SubIndicator` -> `MasterSubIndicator` with effect -> `MasterIndicator` -> `NeuroReportMasterIndicator` -> `NeuroReport`) matches the SDD exactly and is generic; `ActivityReportNew` is a single denormalised fact table, which is the right shape; eTools replica tables carry unique external ids and idempotent syncs; `Location` uses a proper tree model. Zero `RunPython` or `RunSQL` migrations exist, so a fresh migrate has no data-migration landmines.
- **Not worth keeping.** The fact table is "stringly typed": month, location codes, latitude, longitude, partner id, and years are `CharField`, so time series need substring arithmetic in SQL. The fact-to-indicator join runs on an unindexed, nullable `CharField(30)` with no foreign key. Polygons are stored as `ArrayField(CharField(max_length=64500))` loaded from 39 MB of committed CSVs. Library documents are `BinaryField` rows base64-encoded on every request. Per-database ActivityInfo username and password are stored in plaintext columns that the code no longer uses. Money is text; `djmoney` is installed and never used.
- **Migrations.** 345 files generated by nine Django versions between 2016 and 2024; no squashes; `pivoting`'s initial migration created the whole legacy tree and then deleted it (23 `DeleteModel`, 90 `RemoveField`); one `etools` migration was neutered by hand (`operations = []` next to `operationsx`) and re-applied in the next file, which is evidence that a migration failed in production and was patched on the server. `makemigrations --check` already reports drift in `pivoting` and `users`, while `startup.sh` runs `makemigrations` at every boot for an app named `survey` that does not exist. The production schema is effectively unknowable from the repository.
- **Runtime data in the package.** Population figures and the vulnerability list are read from year-stamped files under `pivoting/uploads/` on every request; ActivityInfo extracts are written into the package directory of an ephemeral container.

### 4.4 Libraries and dependencies

**Backend (Python).**

| Issue | Evidence |
|---|---|
| `requirements/production.txt` does not install with pip 24.1 or later: `celery==4.2.0` (2018) has invalid metadata. Django is never downloaded. | Reproduced: `pip install -r requirements/production.txt` exits 1 |
| Removing the four dead pins (celery, beat, results, pathlib backport), everything else resolves to Django 5.2.17, DRF 3.18.1, allauth 65.19.4, pandas 3.0.6. On Python 3.12 the same file would resolve to Django 6.1. Nothing is locked. | Reproduced |
| `openpyxl` (imported at load time by `pivoting/views.py:34`) and `requests` are missing from the production file; only the orphan `_requirements.txt` lists `openpyxl`. A container built from the documented file returns HTTP 500 on every request. | Reproduced: `manage.py check` fails with `ModuleNotFoundError: openpyxl` |
| The only pinned runtime packages are the vulnerable ones: `gunicorn==20.1.0` (2 CVEs, request smuggling, fix 22.0.0) and `Werkzeug==2.2.2` (9 CVEs, never imported by the app). | `pip-audit`: 22 advisories, 11 distinct |
| Abandoned packages still listed: `django-rest-swagger` (2018, now ImportErrors against current DRF, in `INSTALLED_APPS`), `awesome-slugify` (2015, file-collides with `python-slugify`), `unicodecsv` (2015), `django-google-tag-manager` (2019), `django-sslserver` (2019), `pathlib` backport, `django-fsm` (self-declared unmaintained, warns at startup). | PyPI release dates |
| About 26 listed packages are never imported or configured; development tooling (ipdb, sphinx, black, flake8, debug-toolbar, pytest) is installed into the production image. | Import grep |
| Five near-identical requirements files; the root `requirements.txt` that the README and the zip-deploy pipeline reference does not exist. | Repo listing |

**Frontend (vendored and CDN).**

| Library | In use | Status |
|---|---|---|
| AdminLTE | 3.2.0 | Current is 4.x, rebuilt on Bootstrap 5; no in-place upgrade |
| Bootstrap | 4.6.1 (plus 4.2.1 in admin builder) | End of life since 2023; 4.2.1 has a known XSS |
| jQuery / jQuery UI | 3.6.0 / 1.13.0 (vendored, on every page) | jQuery UI 1.13.0 has CVE-2022-31160 (XSS) |
| Chart.js | 2.9.4 | Current 4.x, incompatible API |
| DataTables / Select2 / pivottable.js | 1.11.4 / 4.0.13 / 2.23.0 (2018) | Majors behind |
| Plotly | `plotly-basic-latest` from CDN on 9 pages | Floating major version; has shipped two breaking majors and a critical prototype-pollution fix since the templates were written |
| Highcharts | unversioned CDN on 6 pages (+34 module tags) | Commercial licence required for non-personal use; UNICEF may qualify for a non-profit licence, but no licence is recorded |
| jsPDF / html2pdf.js / html2canvas | 1.5.3 / 0.10.1 / 0.4.1 (2013) | 15 advisories on jsPDF including two critical |
| polyfill.io | loaded on the donor dashboard | Domain hijacked in 2024; must be removed |
| Leaflet, ArcGIS 4.11/4.21, d3 v3 and v5, OwlCarousel, bootstrap-select, moment, SheetJS, jszip, summernote, canvg | assorted | Duplicated: jQuery vendored 7 times, Select2 9 times, FontAwesome twice |

Base templates also reference 12 static files that exist nowhere in the repository (`css/main.css`, `vendors/intro.js/*`, and others); under the manifest static storage used in production those lookups raise `ValueError` and return HTTP 500.

### 4.5 UI/UX

**What works.** Every live page extends one base (`baseV2.html`) with a consistent header pattern. The analytical pivot experience (pivottable.js with nine custom aggregators, Plotly renderers, preset views, one-click TSV export) is genuinely useful. Filters are labelled Select2 multi-selects, and the PCA, Partnerships, and Library pages submit them as GET forms so results are shareable URLs. The donor dashboard has a proper loading spinner and empty state. Branded 403/404/500 pages exist.

**What does not.**

- **Architecture.** 2,930 lines of inline `<script>` across the 18 live pivoting templates (the donor dashboard is 60% JavaScript); no project JavaScript module is loaded by any live page; helpers are re-declared per template. Four base templates coexist; `base.html` is a stale fork still serving the 403 page and user pages with a dead COVID-19 sidebar link. 18 templates carry ` copy`, `_old`, `.orig`, `_new` names; 11 belong to `survey` and `tellme` apps that are not installed; `pca-summary-active` and `pca-summary-all` are 98% identical files.
- **Robustness.** 20 `$.ajax` calls, 20 success handlers, 0 error handlers. An empty dataset leaves the analytical page blank and throws a TypeError. The base template's document-ready block references DataTables unconditionally and throws on six live pages, including the landing page, whenever DataTables is not loaded.
- **Accessibility.** Indicator status is an empty coloured badge (colour-only encoding); rows open modals on click with no button, tab index, or key handler; placeholder alt text; zero `scope=` attributes on table headers; body text forced to 0.75rem with `!important`; `cursor: pointer` on every card.
- **Responsiveness.** The landing page uses `col-6` at every breakpoint with a 72px title and a random 1-of-67 JPEG (largest 968 KB, not lazy-loaded); `min-height: 800px` on all content; 34 of 41 tables lack responsive wrappers.
- **Navigation.** 190 lines of hard-coded sidebar HTML with baked-in report ids, a link to an admin URL for a non-installed app, no active state, no breadcrumbs, 40+ external SharePoint and Power BI links mixed with internal pages. The login page advertises "Use my Microsoft account" (non-functional) and password reset is commented out.
- **Internationalisation.** `USE_I18N=True` but zero translatable strings in any live page, `lang="en"` hard-coded, no RTL provision, in a context where partners work in Arabic and French.
- **Consistency.** Two different filter patterns (GET forms versus AJAX without URL state) across sibling pages, so the Donors Mapping and Intervention Map views cannot be bookmarked or shared.

**Modernisability verdict.** The live surface is small and pattern-based (a table dashboard, a pivot page, a filter-plus-AJAX dashboard, a map, static link lists) and can be rebuilt on a modern stack in weeks, not months. Incremental improvement of the current templates is only worthwhile for the quick wins in Phase 0.

### 4.6 Deployment, operations, and repository hygiene

- **Five deployment shapes, none runnable as committed:** (1) the production Dockerfile, whose `CMD` runs `service ssh start` with no SSH server installed and then `honcho start` with no Procfile (exits 1, gunicorn never starts); (2) `azure-pipelines.yml`, which triggers on `master` while the branch is `main`, tags the image `latest` (no rollback), and deploys only to slot `tst`; (3) `azureproject/pipeline.yaml`, a zip deploy that installs a `requirements.txt` that does not exist and archives the whole 600 MB tree; (4) `startup.sh` for App Service code deploy, which runs `apt-get` and installs crontab at every boot, runs `makemigrations` in production for a non-existent app, and has CRLF line endings; (5) `passenger_wsgi.py`, a cPanel remnant that is not valid Python. The SDD (2024) says cPanel; the handover document (2026) says containers.
- **Configuration.** The live configuration is not represented anywhere in the repository: `production.py` points the database at `localhost` with a committed password, so the deployed settings must be edited on the server.
- **Scheduling.** The three nightly syncs run from cron inside the web container: lost on restart until `startup.sh` re-adds them, duplicated on scale-out, competing with request handling, with output going to `print()` and therefore nowhere. Errors are swallowed and the "last updated" timestamp is set before the import runs, so a silently failing sync is indistinguishable from a successful one.
- **Observability.** No health endpoint, no Sentry or Application Insights, console-only logging, 404 and 500 emails routed to the vendor through an email backend with an empty API key.
- **Platform.** The SDD names Azure Database for PostgreSQL Single Server v11, which Microsoft retired on 28 March 2025, and Python 3.9 (end of life October 2025). The Dockerfiles use 3.10 and 3.11-on-buster (Debian buster is end of life). No backup or restore plan beyond cookiecutter scripts.
- **Repository.** 15,956 tracked files, of which 12,845 (80.5%) are collected static output or vendored AdminLTE, and 14,642 (91.8%) once `.pyc`, `media/`, and `AIReports/` are included. 1,771 committed bytecode files are rewritten by any `manage.py` invocation (409 showed as modified during this review). No `.gitignore`, `.dockerignore`, or `.env.example`. `README.md`, `CONTRIBUTING.md`, and `CHANGELOG.md` are the untouched Microsoft sample templates; `screenshot_website.png` is the sample's "Azure Restaurant Review" screenshot.

### 4.7 Tests and quality gates

| Measure | Value |
|---|---|
| Tests in business apps (`pivoting`, `etools`, `locations`, `activityinfo`) | 0 (three 3-line stubs) |
| Only existing suite (`users/tests`, cookiecutter boilerplate) | 16 collected, 2 pass, 9 error on a missing fixture, 1 calls `.delay()` on a plain function |
| Quality-gate config files (`pytest.ini`, `pyproject.toml`, `.pre-commit-config.yaml`, `.flake8`, `.coveragerc`, `.gitignore`, ...) | 0 of 9 |
| CI steps that test, lint, or check migrations | 0 |
| `pyflakes` findings | 184, including 2 genuine `NameError`s (one crashes the polygon import after it has already deleted the table; one is silently swallowed on every iteration of the nightly locations sync) |
| `radon` complexity | average 3.31 (grade A) but the 12 D/E/F blocks are exactly the business core: `add_rows` F/45 at 207 lines, `get_partner_profile_details` E/40 at 508 lines, the HPM and PCA dashboards at E/35 and E/33 |
| Maintainability index of `pivoting/views.py` | 0.00 (the minimum) |
| Error handling | 13 bare `except:`, 15 except-then-pass/continue, 42 `print()` versus 10 logger calls |
| Duplication | `pivoting/client.py` vs `activityinfo/client.py` 91% identical; two partner-profile templates 99%; two PCA summary templates 98%; 24 copy/old/bak/new files outside static |
| Commented-out code | 237 lines, including the staff credential |

As structured, the code is not unit-testable without refactoring: views open database cursors directly, SQL is built by string mutation, sync tasks call a hard-coded HTTP helper with no injection seam, and business logic lives inside 100-to-500-line `get_context_data` methods. A characterisation safety net for the four critical paths (eTools sync, pivot queries, exports, auth) is roughly three to five person-weeks of integration tests; genuinely safe unit-level coverage requires extracting a data-access seam first, roughly 2.5 to 3 months, which is the same order of effort as reimplementing those modules cleanly.

### 4.8 Documentation versus reality

- **The SDD v0.5 is a good functional specification.** Its data model, admin wizards, page-by-page feature descriptions, ActivityInfo and eTools endpoint list, and cron schedule all check out against the code. Its "Back-End Restructuring" section explains why the `activityinfo` app, `base2.html`, and the `/etools/` pages still exist: they are residue of an unfinished v1-to-v2 rewrite. It is, however, an unsigned draft with a TODO, an empty sign-off table, and plaintext credentials.
- **The 2026 handover document is not reliable.** It says the `activityinfo` integration is unused (ActivityInfo is the only source of dashboard data; the *app* is dead but the integration lives in `pivoting`), that Celery, Azure Blob storage, allauth-based SSO, and environment-variable secrets are in place (none are), that Django is 3.x (migrations are 5.0.7), and that the app deploys as a container (the image cannot start). It omits about 40% of the routed surface.
- **Both documents claim all internal pages require authentication and that NeuroDB hosts no APIs.** 25 data endpoints are open.
- **Knowledge-transfer risk is extreme.** One squashed commit; one named vendor engineer who is still the recipient of production error mail; authorship markers from three generations of developers; no runbook, no architecture decision records, no data dictionary.

---

## 5. What is worth keeping

These are the assets any path should preserve, and they are what makes a rebuild bounded:

1. **The PostgreSQL data**: the `pivoting_*` configuration and fact tables, the `etools_*` replica tables, `locations_*`, and `users_*`. All rows are migratable; the eTools tables need type fixes (money, ids).
2. **The indicator domain model** and its admin wizards (Add Sub Indicators, Add Master Indicators), the single-current-year invariant, and the name-based tag parsing for gender, nationality, disability, and age group.
3. **The 22 SQL constants in `pivoting/queries.py`** as the executable specification of the aggregation rules (effect TOTAL/NUMERATOR/DENOMINATOR, SUM_OVER_SUM, the HPM cut-off logic), to be re-expressed once as database views or a nightly aggregate table.
4. **The SDD's functional inventory** (section 4.8 and Appendix A), which is complete enough to serve as the acceptance checklist for a rebuild.
5. **The Django framework itself.** The code already runs on Django 5.2; the team's existing knowledge and the SQL transfer directly. A rebuild on Django 5.2 LTS is a rebuild of the application, not a change of platform.
6. **A few good patterns**: the container entrypoint that gates migrate/collectstatic on environment flags, WhiteNoise with manifest storage, the security-middleware baseline, and the per-entity management commands for syncs.

---

## 6. Functional scope that a rebuild must reproduce

Derived from the SDD and the routing tables (full source-of-truth mapping in Appendix A).

| Area | Pages / features |
|---|---|
| Landing | Home page listing current-year databases, Neuro Reports, HPM reports, partnerships, donor, maps, library, population figures |
| Database (per ActivityInfo folder, per reporting year) | Dashboard (master indicators with tracking status, sub-indicator modal, DataTables export), Analytical View (pivot with quick switch and TSV export), Snapshot (print layout), Intervention Map (map/chart/table by governorate, district, cadaster, site), Raw Data download, reporting-year switch |
| Neuro Reports | Dashboard, Analytical View, HPM report with month/quarter selection, deltas, inline comments, and PDF tables |
| Funded programmes (PCA) | Dashboard with filters and detail popup, Analytical View, Summary (active PDs), Summary (all PDs) |
| Donor Mapping | Filters; ActivityInfo interventions; funds since 2014; reported indicators; planned versus actual locations map; funding history and highlights |
| Partnerships | Partner list with filters; partner profile in tabs with engagement counts, interventions, and staff |
| Population Figures | Total population, children, most vulnerable, with charts and tables |
| Library | Published resources with filters, search, pagination, download |
| Maps | List of completed maps with external links |
| Administration | 9 admin-managed model families with two wizards and import actions; user, section, and office management |
| Integrations | ActivityInfo structure import and monthly data import via export jobs; eTools partners, agreements, interventions, travels, audits, action points; eTools locations; three scheduled jobs |
| Cross-cutting | Login (username/password now, Microsoft SSO intended), CSV/Excel/PDF exports, Power BI links |

Roughly 25 pages, 14 data feeds, 6 exports, 2 upstream integrations, 9 admin model families. The legacy `/etools/` page generation is superseded by the `/v2/` pages and should be retired, not rebuilt.

---

## 7. The two options compared

| Criterion | A. Enhance in place | B. Rebuild from scratch (data, domain model, and Django kept) |
|---|---|---|
| Time until the live security exposures are closed | ~2 weeks (Phase 0) | ~2 weeks (same Phase 0 applied to the live system) |
| What is reused | Everything, including 92% junk in the repo, 345 migrations, dead app coupling, stringly-typed schema, inline-JS templates | Data, domain model, SQL rules as spec, SDD, Django skills |
| What must be rewritten anyway | Settings and secrets, dependency lock, deployment path, sync layer, views/query layer (no test seam), every template (frontend stack has no upgrade path), test suite from zero | Everything except the items above, but on a clean foundation |
| Risk of unknown behaviour | Lower per change, but there are no tests, so every refactor is unverified; the raw SQL business rules must still be reverse-engineered to add tests | Higher at the start; mitigated by the SDD, the SQL constants as spec, a parallel run against the old site, and the small scope |
| Risk of never finishing | High: incremental work inside this codebase tends to stall because each layer depends on the next (cannot delete `activityinfo` without squashing migrations; cannot test views without extracting SQL; cannot upgrade Bootstrap without touching every page) | Medium: bounded scope with a clear acceptance checklist; the main risk is scope creep during the rebuild |
| End state | A cleaner version of the same architecture; schema and migration history still carry 2016 decisions; frontend still template-bound unless also rebuilt | A typed schema, a data-access layer, a tested integration layer, one deployment path, a modern frontend, a repository the new team owns |
| Estimated effort (person-months, excluding Phase 0) | 8 to 11, with a worse end state; realistically converges on a rewrite of 80% of the code | 9 to 12 |
| Elapsed time with a two-person team | 6 to 8 months, with the site partially modernised throughout | 5 to 6 months, with the old site untouched (after Phase 0) until cutover |
| Long-run maintenance cost | Higher: the new team inherits code it did not write and cannot test | Lower: the new team owns a codebase built with tests and a lockfile from day one |
| Security posture | Patches close the known holes; the copy-paste pattern that produced them remains | Structural: authentication and parameterisation enforced by the data-access layer and middleware |

**Verdict: Option B.** The costs are comparable; the risks are comparable but differently shaped; the end states are not comparable. The enhancement path only wins if the organisation cannot fund five to six months of focused work at all, in which case the right answer is Phase 0 alone and a frozen system, not a partial modernisation.

---

## 8. Recommended roadmap

### Phase 0: stabilise the live system (weeks 1-2, one senior developer, applies under either option)

1. **Rotate** the eTools token, the ActivityInfo token, the Django secret key, the database password, the Azure Redis key, the staff account password in the comment, the cPanel credential in the SDD, and review the Google Maps key's referrer restriction. Request service accounts from eTools and ActivityInfo instead of personal tokens.
2. **Close the data endpoints**: add `@login_required` to every function view in `pivoting/views.py` and `etools/views.py`, `LoginRequiredMixin` to the two `ListView` exports, and set `REST_FRAMEWORK` default permissions to `IsAuthenticated` with the create/update mixins removed from the locations viewset. Add a test that iterates every URL pattern and asserts anonymous requests are redirected.
3. **Fix the two SQL injection sites** (`pivoting/views.py:75-81`, `etools/views.py:106`) with bound parameters and a whitelist; convert the other `str.replace()` sites to `= ANY(%s)`.
4. **Remove** the `polyfill.io` script tag, the placeholder SSO link on the login page, `output.txt`, and the `/media/` and `/static/` serve routes.
5. **Make the build reproducible**: delete the four dead pins, add `openpyxl` and `requests`, pin `django~=5.2`, upgrade `gunicorn` to 23 or later, drop `Werkzeug` from production, generate a lockfile, add a `.gitignore`, and un-track `staticfiles/`, `*.pyc`, `media/`, and `AIReports/`.
6. **Tighten settings**: remove `"*"` from `ALLOWED_HOSTS`, set `SECURE_SSL_REDIRECT=True`, one-year HSTS, and move `SECRET_KEY` and `DATABASE_URL` to environment variables.
7. **Record ground truth**: confirm in the Azure portal how neuro-db.org is actually deployed, which database server it uses, and whether the cPanel host still serves anything; write it down as the first page of a runbook.

### Phase 1: foundation for the rebuild (weeks 3-6)

- New repository; Django 5.2 LTS; single settings module driven by environment variables; `pyproject.toml` with locked dependencies; pre-commit with ruff; CI running lint, `check --deploy`, `makemigrations --check`, and pytest against a PostgreSQL service container on every pull request.
- One multi-stage Dockerfile (non-root, no SSH), one pipeline building an immutable image tag on `main`, deploying to a staging slot, and swapping to production; Azure Database for PostgreSQL Flexible Server 16 with point-in-time restore; Application Insights and a health endpoint.
- Authentication through django-allauth's Microsoft provider pinned to the UNICEF tenant, no auto-provisioning, admin behind SSO; `REST_FRAMEWORK` defaults to authenticated.
- Typed schema: the indicator hierarchy as-is; a fact table with a real period date, foreign keys to indicator and admin-area tables, and composite indexes; eTools replicas with decimal money, real dates, and consistent surrogate keys; population figures and vulnerability lists as tables loaded by a command; documents in Azure Blob via django-storages.
- One-off ETL script copying rows from the current database, run repeatedly during the project so cutover is a rehearsed operation.
- Integration clients for ActivityInfo and eTools with timeouts, retries, pagination, structured logging, per-run records, and recorded-HTTP tests; scheduled jobs as Azure Container Apps Jobs (or WebJobs) outside the web container, with alerts on failure or staleness.
- The aggregation rules from `queries.py` expressed once as PostgreSQL views (or a nightly aggregate table) that both Django and Power BI can read, with snapshot tests comparing results against the old queries on the same data.

### Phase 2: core reports (weeks 7-14)

- Database Dashboard, Analytical View, Snapshot, Intervention Map, Raw Data export, reporting-year switch; Neuro Reports and the HPM report; the admin model families and both wizards.
- Frontend: server-rendered Django templates with HTMX for partial updates, one JavaScript bundle built with Vite (pivottable.js and a single current charting library kept, since users like them), Bootstrap 5, data passed through `json_script`, shared error and empty states, keyboard-accessible tables with visible status text, responsive layouts, and strings wrapped for translation from the start.
- Acceptance: each page compared against the old site on the same data with the product owner.

### Phase 3: remaining pages (weeks 15-20)

- Funded programmes (dashboard, analytical, both summaries), Donor Mapping, Partnerships and partner profile, Population Figures, Library, Maps, landing page and navigation model.
- Retire the legacy `/etools/` generation explicitly; keep only the two pages (interventions, programmatic visits) if the product owner confirms they are still used.

### Phase 4: parallel run and cutover (weeks 21-24)

- Run both sites against the same database for two sync cycles; verify sync run records and report values match; user acceptance; DNS cutover with the old App Service kept warm for a two-week rollback window; then decommission the old App Service, the cPanel host, and the old repository (archived, not deleted).
- Hand over: runbook (deploy, secrets rotation, yearly population-figures update, HPM PDF drop, sync failure triage), data dictionary generated from the models, and architecture decision records.

### Team and budget

- Two developers (one senior Django full-stack lead, one mid-level), a part-time product owner from UNICEF who knows the reports, and access to the original vendor engineer for two or three structured interviews against the scope checklist early in Phase 1.
- Estimated 9 to 12 person-months of development plus Phase 0. Elapsed 5 to 6 months. The estimate assumes no new features are added during the rebuild; feature requests are queued for Phase 5.

### Decision gates

- **End of Phase 0:** all critical findings closed on the live site; secrets rotated; the build reproduces in CI. If this cannot be achieved in two weeks, the team is not yet ready for Phase 1.
- **End of Phase 1:** the ETL copies the full database into the new schema, and the aggregate views reproduce the old dashboard numbers for at least three databases. This is the go/no-go for the rebuild; failure here means the SQL rules were not fully understood and the vendor interview must be repeated.
- **End of Phase 2:** the product owner signs off the Database Dashboard and Analytical View as equivalent or better.

---

## 9. Risks of the recommended path and how they are handled

| Risk | Mitigation |
|---|---|
| Undocumented business rules hidden in the raw SQL (commented-out filters, HPM cut-off dates, "funded by UNICEF" toggles) are lost | Treat `queries.py` as the spec; snapshot-test the new views against the old queries on the same database in Phase 1; interview the vendor engineer against the scope checklist |
| The live site is compromised during the rebuild | Phase 0 closes the exploitable holes first; secrets rotated; the old code is frozen afterwards |
| Scope creep during the rebuild | Feature freeze; the SDD-derived checklist is the only acceptance list; requests queued for after cutover |
| Team capacity or funding is interrupted mid-rebuild | Phases are ordered so that Phase 1 alone leaves a usable asset (secure foundation, typed schema, tested integrations, working ETL) that a later team can pick up; the old site keeps running throughout |
| The production database schema differs from the repository's migrations | Phase 0 item 7 and the Phase 1 ETL both start from a schema dump of the live database, not from the migrations |
| Highcharts licensing | Replace with the single charting library chosen in Phase 2 (Chart.js 4 or Plotly pinned), or record a non-profit licence |

---

## Appendix A: source-of-truth map for the current functionality

Format: route : view : template : data source.

**Top level (`azureproject/urls.py`)**
- `/` : `HomeView` : `pivoting/home.html` : current `ReportingYear`, databases via template tag, random landing image; sidebar in `baseV2.html:375-481`
- `/admin/` : Jazzmin admin : `Database` (actions: import structure, replicate indicators, import partners, import data, update monthly values, import reports, generate AWP code, set tags, update names), `ReportingYear`, `Activity`, `IndicatorNew`, `SubIndicator`, `MasterIndicator` (+ Add Sub Indicators wizard), `MasterIndicatorTag`, `NeuroReport` (+ Add Master Indicators wizard, comments inline), `Resource`/`ResourceType`/`ResourceTopic`/`ResourceTag`, `Map`, `users.User`/`Section`/`Office`; four custom download links
- `/about/` : template `pages/about.html` is missing (HTTP 500)
- `/accounts/*` : allauth login, logout, password change; sign-up gated by environment
- `/sso/login|callback|logout` : hand-rolled Microsoft OAuth (non-functional placeholders)
- `/users/~redirect/`, `~update/`, `<username>/` : cookiecutter profile pages
- `/locations/locations/`, `locations-light/`, `autocomplete/` : DRF API over `Location` (unauthenticated; list, retrieve, create, update; two routes unreachable due to regex in `path()`)

**Pivoting JSON feeds (`/v2/load_*`, all GET, no auth) : `pivoting/views.py:59-760`**
- `load_database_activityinfo?id` : `DATABASE_ACTIVITYINFO` SQL over the fact table joined to the indicator hierarchy (analytical pivot input)
- `load_neuroreport_activityinfo?id&emergency` : `NEUROREPORT_ACTIVITYINFO` (SQL injection site)
- `load_pca_activityinfo?id` : `PCA_ACTIVITYINFO_PURE`
- `load_donor_activityinfo` : `DONOR_ACTIVITYINFO_PURE`
- `load_sub_indicators?id` : `MASTER_SUB_INDICATORS` (dashboard row modal)
- `load_ry_master_indicators` : master indicators of a reporting year (donor "Reported Indicators")
- `load_partner_staff?id` : `PartnerOrganization.staff_members` JSON (names, emails, phones)
- `load_pca_details?id` : PCA title, sections, offices, donors, intervention count
- `load_donors_mapping_data` : PCA and fact-table aggregates for the donor page
- `load_donors_mapping_locations` : planned versus actual polygons
- `load_database_intervention_locations` : fact table by governorate, caza, cadaster, site with polygon text
- `load_database_snapshot` : same aggregates for the print snapshot
- `load_population_figures` : `pivoting/uploads/Population_figures_<year>_NeuroDB.json` (filename hard-coded per year)
- `load_most_vulnerable` : `pivoting/uploads/list of 332 localities data_2022_08_19.xlsx` via openpyxl per request

**Pivoting pages (login required) : `pivoting/views.py:826-1841`**
- `/v2/database-dashboard?id` : master indicators via `MASTER_INDICATORS_{SUM,MAXIMUM,AVERAGE,COUNT}` SQL, tracking status, DataTables export, reporting-year switch
- `/v2/database-analytical?id` : pivottable.js + Plotly + Select2 over `load_database_activityinfo`; quick switch; TSV export
- `/v2/database-snapshot?id` : print layout combining dashboard, preset pivots, and map
- `/v2/database-intervention-map/?id` : Google Maps + Plotly + DataTables; filters PD, partner, governorate, caza, month
- `/v2/database-download/?id` : streams `pivoting/AIReports/<ai_id>_ai_data.xlsx` (no auth)
- `/v2/neuroreport-dashboard/?id`, `/v2/neuroreport-analytical?id` : non-HPM Neuro Reports
- `/v2/hpm-neuroreport/?id&month&quarter` : HPM values to period, delta versus previous period, inline comments, PDFs from `static/hpm_files`
- `/v2/pca-dashboard/`, `/v2/pca-analytical?id`, `/v2/pca-summary-active`, `/v2/pca-summary-all` : eTools PCA list with filters, popup, pivot, summaries via `PCA_*` SQL
- `/v2/donor-dashboard/` : filters; ActivityInfo interventions; funds since 2014; reported indicators; planned versus actual map; funding history (Chart.js); funding highlights (pivottable)
- `/v2/partnerships/`, `/v2/partnership-profile/?id` : partner list and tabbed profile with engagement counts and staff
- `/v2/population-figures/` : Chart.js and pivottable views from JSON/xlsx
- `/v2/Resources/`, `/v2/resource_file/<pk>/` : published resources with filters and download (download unauthenticated)
- `/v2/Maps/` : completed maps with external ArcGIS links
- `/v2/activityinfo-summary-download/`, `/v2/activityinfo-pca-summary-download/`, `/v2/etools-summary-excel/`, `/v2/activityinfo-wrong-pca-numbers/` : CSV, Excel, and text dumps (no auth)

**Legacy eTools generation (`/etools/`, linked only from the dead `base2.html`) : `etools/views.py`**
- `partner-profile/`, `partnership/` : partner statistics by year (template fails to compile)
- `donor-mapping/` plus `donor-interventions/`, `donor-programme-results/`, `donor-funding/`, `load-donor-locations/` (no auth) : Highcharts, ArcGIS, jsPDF (template fails to compile; SQL injection site in donor interventions)
- `interventions/`, `interventions-export/` (no auth) : PCAs ending this year (crash on removed field)
- `programmatic-visits-monitoring/` : travel activities by section, office, donor (crash on Python 2 idiom)

**Integrations and jobs**
- ActivityInfo structure: `manage.py import_database_structure` or admin action -> `pivoting/utilities.py` (resources/databases, resources/form schema) -> `Activity` + `IndicatorNew` with tag parsing
- ActivityInfo data: `manage.py import_data_v2` (cron 18:00, days 1-22) -> `pivoting/tasks.py` -> `utils.import_data_via_r_script` -> `exports.py` export jobs -> `pivoting/AIReports/<ai_id>_ai_data.{txt,xlsx}` -> `read_data_from_file` -> `ActivityReportNew` delete and re-insert
- eTools: `manage.py sync_etools_data` (cron 20:30) = partners, partner detail, agreements, interventions, intervention detail, travels and detail (last 365 days), audit engagements and detail, action points; individual `sync_*` commands per entity
- Locations: `manage.py sync_locations_data` (cron 05:00), `sync_locationtype_data`, `sync_simple_locations_data`, `import_polygons` (CSV to polygon text)
- Email: SendGrid via anymail (empty key); analytics: Google Tag Manager; embeds: Power BI links on sections and databases

## Appendix B: verification status of the decision-driving findings

| Finding | How it was verified |
|---|---|
| Unauthenticated SQL injection via `emergency` | Code read at `pivoting/views.py:70-81` and `queries.py:668`; no auth decorator; string building reproduced |
| 25 unauthenticated data endpoints | Every route in the five `urls.py` files mapped to its view; absence of `login_required`, `LoginRequiredMixin`, `is_authenticated`, and `REST_FRAMEWORK` confirmed by grep and read |
| Authenticated SQL injection via `.extra(where=)` | Code read at `etools/views.py:76, 106` |
| Locations API create/update open | `locations/views.py:17-24` read; `REST_FRAMEWORK` absent from settings |
| Committed secrets | Each cited file and line read; values not recorded |
| SSO placeholders, tenant `common`, auto-provisioning, dead redirect | `azureproject/sso_views.py` read in full; `dashboard` URL name absent |
| Production requirements do not install | `pip install -r requirements/production.txt` run on Python 3.11 with pip 25: exit 1 at `celery==4.2.0` |
| Missing `openpyxl` breaks every request | `manage.py check` run after installing the remaining requirements: `ModuleNotFoundError` from `pivoting/views.py:34` |
| Code boots on Django 5.2 | `manage.py check --deploy` after adding `openpyxl` and `requests`: exit 0, 13 warnings |
| Migration drift | `makemigrations --check --dry-run`: pending migrations in `pivoting` and `users` |
| 11 CVEs in pinned packages | `pip-audit` on the resolved set |
| Django version history 2016 to 2024 | `Generated by Django` headers in all migration files |
| `from this import d` | `etools/admin.py:3` read; Zen of Python observed on `manage.py check` output |
| Docker image cannot start | `honcho start` with no Procfile exits 1 (reproduced by the deployment reviewer); no `Procfile` in the repository |
| Template compile failures on `/etools/` pages | Rendered through the Django template engine by the UI reviewer: `TemplateSyntaxError` for unregistered tags |
| Committed bytecode rewritten by any Python run | 409 tracked `.pyc` files showed as modified after `manage.py check` during this review |

A second adversarial verification pass over the high and critical findings of each dimension confirmed the findings it completed; its full output is retained with the review working files.

## Appendix C: commands used for the executed checks

```
python3 -m venv venv && venv/bin/pip install -r requirements/production.txt     # fails at celery==4.2.0
grep -vE '^(celery==|django-celery-beat==|django-celery-results==|pathlib==)' requirements/production.txt > prod.txt
venv/bin/pip install -r prod.txt && venv/bin/pip-audit -r <(venv/bin/pip freeze)
DJANGO_READ_DOT_ENV_FILE=off venv/bin/python manage.py check --deploy
DJANGO_READ_DOT_ENV_FILE=off venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python -m pyflakes pivoting etools locations users utils azureproject activityinfo contrib
venv/bin/radon cc -s -a -e '*/migrations/*' pivoting etools locations users azureproject
venv/bin/radon mi -s -e '*/migrations/*' pivoting etools locations users azureproject
grep -rhoE 'Generated by Django [0-9.]+' */migrations | sort | uniq -c
git ls-files | wc -l; git ls-files | grep -cE '^(staticfiles|static/adminlte|static/admin)/'
```
