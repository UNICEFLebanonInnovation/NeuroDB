# NeuroDB Technical Review and Recommendation

**Date:** 22 September 2026
**Scope:** Source code, libraries, UI/UX, data model, deployment, tests, and documentation of the `UNICEFLebanonInnovation/NeuroDB` repository (branch `main`, single commit `cbacdde`), read against the vendor's Software Design Document v0.5 (Sept 2024) and `TECHNICAL_DESIGN.md` (2026).
**Decision requested:** enhance the existing code in place, or retire it and rebuild from scratch.

---

## 1. Executive summary

**Recommendation: enhance in place (Option A), as a disciplined modernisation programme with hard phase gates, not as incremental patching. Ship the security fixes to production within two weeks, then rebuild the foundation, delete the dead 70 percent, put a test harness around the business rules, and only then restructure the view layer and the frontend.**

NeuroDB does its job for users today, and the concept behind it is sound: a small analytics portal that mirrors ActivityInfo and eTools data into PostgreSQL and lets programme staff pivot, map, and export it. The problem is everything around that concept. The codebase is not a 2019 project but a 2016 one (the first migration is dated September 2016) that has been forked, half-rewritten, and patched on the production server, with no tests, no history, and no build that can be reproduced from the repository. Ten independent review passes rated every dimension between 2 and 4 out of 10.

The choice between the two options is closer than the state of the code suggests, and the review process itself changed the answer. The first synthesis of the findings leaned towards a rebuild. An independent decision panel (two advocates arguing each option as strongly as possible, three judges scoring through engineering-risk, cost, and security lenses, each spot-checking the code) then scored enhancement ahead on all three lenses. The arguments that carried it:

| Fact | Why it favours enhancing |
|---|---|
| **The code already boots on Django 5.2 with zero errors.** The most expensive part of any modernisation, the framework upgrade, has already happened. A rebuild would throw away a working Django 5 codebase to build another Django 5 codebase. | Verified by running Django's deployment check during this review. |
| **The live surface is small and the rest is deletion, not porting.** Only 18 page templates plus one base layout are live (about 7,600 of 29,000 template lines); 4 of 6 base templates, the whole legacy `/etools/` page family, the `activityinfo` app, and 18 copy/old/orig files are residue of an earlier, unfinished v1-to-v2 rewrite. | Deleting is cheap and safe behind smoke tests; a rebuild pays to re-derive scope from a design document that omits 40 percent of routes. |
| **The business rules exist only as code.** The aggregation semantics live in 22 SQL constants with commented-out filters and zero-target guards, the indicator tag parsing in model methods, and the import special cases in a 207-line function. The running system is the only oracle for what "achieved" means. | Enhancement keeps the rules executing while golden tests are written around them; a rebuild must reverse-engineer the same code and then prove equivalence against the same data. |
| **The failure mode is asymmetric.** Enhancement is ordered so that stopping after any phase leaves one system that is strictly safer than today. A rebuild that stalls mid-way leaves two half-systems, which is precisely what this repository already is and how it got that way. | For a small new team with a finite country-office budget, resilience to a funding cut dominates. |
| **Cost and time to value.** Realistic enhancement cost is 11 to 15 person-months over 8 to 10 months, shipping to production every two weeks from week one. A rebuild is 13 to 18 person-months once parallel running, dual syncs, path routing, and a second login are priced in, with the first user-visible page at month four or five. | Similar spend; enhancement delivers earlier, continuously, and without a parallel-run bill. |
| **Knowledge is more in-house than assumed.** The ActivityInfo and eTools import commands and the HTTP helper carry the UNICEF committer's authorship; the vendor-only areas are the v2 pivot SQL, the templates, and the Azure wiring. | The archaeology a rebuild would need is largely the same archaeology enhancement does in Phase 2, except enhancement ships fixes while doing it. |

**The rebuild case was not wrong about the code.** It is right that the presentation layer, the raw-SQL value layer, and the physical schema would all be replaced under either option, that a typed schema with tests from day one is the cleaner end state, and that enhancement's later phases are in substance a rewrite of the view and presentation layers in place. That is why this recommendation adopts the rebuild's disciplines as conditions (section 8): a frozen scope checklist, golden tests captured before any refactor, a written divergence register for ambiguous rules, measurable phase-gate exit criteria, and no feature work before the safety net exists. If the team cannot commit to those gates, the enhancement path degrades into a slow rewrite in the old repository's shape, which is the outcome to avoid.

**What must happen in the first two weeks, regardless of the decision:** rotate every credential listed in section 4.1; add a login requirement to every data endpoint; fix the two SQL injection sites; remove the `polyfill.io` script; restrict `ALLOWED_HOSTS`; and record from the Azure portal how neuro-db.org is actually deployed, because the repository does not describe it. Roughly half a person-month, and the first phase of the roadmap in section 8.

---

## 2. How this review was done

- **Static review** of every Python module (15,802 lines outside migrations), all 153 templates, the settings and deployment files, and both design documents, by ten specialised review passes (pivoting app, etools app, supporting apps, security, dependencies, UI/UX, data model, deployment and operations, tests and quality, documentation versus reality).
- **Executed checks** in an isolated environment: installing the production requirements on Python 3.11, `pip-audit`, Django's `check --deploy` and `makemigrations --check`, template compilation of every routed page, `pyflakes`, `radon` complexity and maintainability, `vulture`, and the existing test suite.
- **Adversarial verification.** The 59 high and critical findings were each handed to a separate verifier instructed to refute them by re-reading the code: 23 were confirmed as written, 36 confirmed in substance with severity or framing corrected, none refuted. A completeness critic then looked for gaps, contradictions between reviewers, and overstated findings; its corrections are folded into this report and listed in Appendix B.
- **Decision panel.** Two advocates built the strongest case for each option; three judges scored both through engineering-risk, cost-and-time-to-value, and security-and-compliance lenses, spot-checking claims against the repository. Section 7 summarises their reasoning.
- **Not done.** No live system was accessed. Production configuration, the real database schema and contents, the Azure resources, and the cPanel host could not be inspected, so claims about "what runs today" are inferred from the repository and the design document and are flagged as such. Section 10 lists what must be checked in the portal and the production database before Phase 1.

Severity uses the usual scale: critical means exploitable today or blocks the project; high means a real defect with user or security impact; medium is a maintainability or correctness risk; low is hygiene. Secret values (keys, passwords, tokens) are deliberately not reproduced in this document; they are identified by file and line only.

---

## 3. Scorecard

| Dimension | Health (1-10) | Headline numbers |
|---|---|---|
| Security | **3** | 2 SQL injection sites (1 anonymous); 23 reachable data endpoints without login; 8 distinct committed secrets; 0 CSRF exemptions (good); 24 CDN scripts without integrity hashes |
| Source code: `pivoting` (main app) | **3** | 8,448 lines; `views.py` 1,841 lines with maintainability index 0.00 and 30 raw SQL executions; `queries.py` 1,702 lines of raw SQL, 25% dead; 57% of `utils.py` references models that no longer exist; 7 functions over 100 lines |
| Source code: `etools` | **3** | 4 of 9 routed pages crash on a removed model field or a Python 2 idiom; 1 index in 1,373 model lines; 28 cascade deletes reachable from admin; token literal at 10 sites |
| Supporting apps and settings | **3** | Settings chosen by operating system; two half-configured auth systems; dead `activityinfo` app kept in the migration graph; admin permission checks overridden to always return True |
| Libraries and dependencies | **3** | Production requirements fail with any current pip; Django unpinned; 11 CVEs in the two pinned runtime packages; 7 abandoned packages; ~26 unused packages; frontend stack end-of-life |
| UI/UX | **4** | Consistent live theme and a genuinely useful pivot tool; but 2,900 lines of inline JS, 0 AJAX error handlers, 4 base templates, 18 junk templates, colour-only status badges, 12px forced body text, no i18n |
| Data model and migrations | **4** | Sound indicator hierarchy and fact table; but dates, money, and codes as strings, fact-to-indicator join on an unindexed nullable text column, 345 migrations from 9 Django versions, models already drift from migrations |
| Deployment and operations | **2** | 5 deployment paths, none runnable as committed; image runs as root with SSH; cron inside the web container; no health endpoint, no error tracking; production configuration not represented in the repository |
| Tests and quality gates | **2** | 0 tests in business apps; 2 of 16 boilerplate tests pass; 0 of 9 quality-gate config files; CI has no test or lint step; 184 pyflakes findings including 2 latent NameErrors |
| Documentation versus reality | **3** | The SDD is a good functional spec; the 2026 handover doc is wrong on 21 of 31 material claims a new team would rely on; one squashed commit |

---

## 4. Findings by area

### 4.1 Security

Findings marked **[verified]** were reproduced by reading the cited code during this review and confirmed by the adversarial pass.

| # | Severity | Finding | Where |
|---|---|---|---|
| S1 | Critical **[verified]** | **Unauthenticated SQL injection.** The `emergency` query parameter is spliced into the NeuroReport SQL by string replacement; `.title()` does not neutralise quotes (SQL keywords are case-insensitive). The view has no login check. An anonymous visitor can read any table, including user password hashes and eTools partner data. The app connects as the `postgres` superuser, which escalates this to host-level access. | `pivoting/views.py:70-81`, placeholder at `pivoting/queries.py:668`, `azureproject/production.py:22` |
| S2 | Critical **[verified]** | **23 reachable data endpoints answer anonymous requests** (25 routes, 2 of which are syntactically dead). All 14 `load_*` JSON feeds, the raw-data Excel export, the library file download, three CSV/Excel table dumps, and the "wrong PCA numbers" list in `pivoting`; the interventions CSV export and donor-locations feed in `etools`; and the locations REST API. Page views are protected with `LoginRequiredMixin`; the data behind them is not. | `pivoting/urls.py:5-18, 41-45`, `pivoting/views.py:59-760, 1190, 1706-1841`, `etools/views.py:145, 441` |
| S3 | Critical **[verified]** | **Live credentials committed.** eTools API token at 13 sites across `etools/tasks.py`, `locations/tasks.py`, and `pivoting/tasks.py`; two distinct ActivityInfo tokens at 6 sites in 4 files (as default arguments in both client copies, and in `pivoting/utilities.py` and `pivoting/utils.py`); Django `SECRET_KEY` and database password in `azureproject/production.py` and `local.py`; a third database password in `compose/production/django/entrypoint-copy`; an Azure Redis access key in a settings comment; a staff username and password in a comment in `pivoting/utils.py:798`. The SDD v0.5 prints a cPanel login URL with password and the Azure database admin username. Rotating the ActivityInfo tokens today requires code changes in three files and a redeploy. | See file references; values withheld |
| S4 | High **[verified]** | **Authenticated SQL injection** through `.extra(where=...)` built from the `donor` query parameter. | `etools/views.py:106` |
| S5 | High **[verified]** | **Unauthenticated write access to locations.** The Django REST Framework viewset includes create and update mixins, and `REST_FRAMEWORK` is never configured, so permissions default to AllowAny. Writes currently fail later with a 500, so the practical exposure is the anonymous list. | `locations/views.py:17-24`, no `REST_FRAMEWORK` in `azureproject/` |
| S6 | High **[verified]** | **Personal data committed and served.** `output.txt` at the repo root is an eTools partner dump (240 organisations, 190 email addresses, 215 phone numbers). Partner staff names, emails, and phones are synced into `PartnerOrganization.staff_members` and served anonymously by `load_partner_staff`. `pivoting/AIReports/` holds 49 MB of raw ActivityInfo records served anonymously via `/v2/database-download/`. The applicable frame is UNICEF's Policy on Personal Data Protection; no record of a data-protection assessment exists in the repository. | `output.txt`, `pivoting/views.py:97-103, 1190-1222`, `etools/tasks.py:92` |
| S7 | High **[verified]** | **Hand-rolled Microsoft SSO** ships with placeholder client id, tenant `common` (any Microsoft account, personal included), auto-creates active users keyed on a mutable email claim, validates no id token or nonce, and ends in `redirect('dashboard')`, a URL name that does not exist. It is routed and linked from the login page. Not exploitable today only because the placeholder id makes Microsoft reject the request. | `azureproject/sso_views.py:9-11, 81-91`, `templates/account/login.html:36` |
| S8 | High | **Compromised CDN.** The donor dashboard loads `https://polyfill.io/v3/polyfill.js`, a domain hijacked to serve malware in 2024. | `templates/pivoting/donor-dashboard.html:18` |
| S9 | Medium **[verified]** | Settings hardening gaps: `ALLOWED_HOSTS` starts with `"*"`, `SECURE_SSL_REDIRECT=False`, HSTS 60 seconds with preload, no Content Security Policy, `USE_TZ=False`. Behind Azure App Service with HTTPS-only enforced these are hygiene items; the wildcard host also disables host-header validation for absolute URLs. `production.py`'s star import silently overwrites `CSRF_TRUSTED_ORIGINS`, dropping the Azure slot hostnames the pipeline deploys to. | `azureproject/settings.py:19-21, 146`, `production.py:29-36` |
| S10 | Medium | 24 CDN scripts without Subresource Integrity, Plotly pinned to `latest` on 9 pages, jsPDF 1.5.3 with 15 published advisories, two Google Maps browser keys in 5 templates (referrer restriction unverifiable from the repo), and Google Tag Manager on every authenticated page. | `templates/pivoting/*.html`, `templates/base*.html` |
| S11 | Medium | 16 template lines render server JSON with `|safe` inside `<script>`, and API data is injected via string concatenation and `innerHTML` on the dashboards. Stored XSS surface from upstream-controlled names and labels. | `templates/etools/interventions.html:173`, `templates/pivoting/database-dashboard.html:164, 194` |
| S12 | Medium | `django.views.static.serve` mounted unconditionally for `/media/` and `/static/`; image runs as root with an SSH daemon config allowing root password login; nginx configs allow TLS 1.0/1.1. | `azureproject/urls.py:25-27`, `compose/production/django/sshd_config`, `compose/production/nginx/*.conf` |
| S13 | Medium | `utils.CustomModelAdmin` overrides `has_change_permission`, `has_delete_permission`, and `has_view_permission` to return True, so any staff account can edit or delete indicators, databases, partners, and sections regardless of assigned permissions. | `utils/custom_model_admin.py:35-45` |
| S14 | Medium | **Production database identity is unknowable from the repository.** Three different database names appear in `production.py`, the container entrypoint, and the SDD; the SDD's migration recipe shows the production host exposed to the internet as the `postgres` superuser. | `azureproject/production.py:20-22`, `compose/production/django/entrypoint-copy:9`, SDD "Connecting to Azure Database" |

Not defects, but noted: the three Power BI "publish to web" links in the sidebar are anonymous by Microsoft's design and are a publishing decision outside this codebase.

### 4.2 Source code

**`pivoting` (the business-critical app, 8,448 lines).**

- **Architecture.** Business logic lives in 1,702 lines of hand-written SQL constants and 1,841 lines of views. Models are anemic; views compute tracking status, month arithmetic, report titles, and even Bootstrap CSS class names (`views.py:877-881`). The four-query dashboard block is copy-pasted between `DatabaseDashboardView` and `DatabaseSnapshotView`.
- **Raw SQL.** 30 execute sites in `views.py`. Most bind parameters correctly; eight assemble SQL with `str.replace()`, one from user input (S1), three from database-sourced PCA numbers (second-order risk, and an empty filter produces an `IN ()` syntax error and a 500).
- **A live crash path.** The NeuroReport query divides by `awp_target` without a zero guard (`queries.py:141`) while the model default for that field is 0 (`models.py:619`); any master indicator attached to a Neuro Report with the default target and at least one reported value makes the HPM and NeuroReport pages return 500. The sibling query at `queries.py:1274` has the guard.
- **Dead code.** 434 lines (25%) of `queries.py` are unused constants, one containing invalid `==` SQL that proves it never ran. About 57% of `utils.py` references models that no longer exist (`Indicator`, `ActivityReport`, `LiveActivityReport`, `AdminLevelEntities`) yet is still wired to admin actions that will crash; the imports are function-local, so the dead code is inert rather than load-bearing. `gistfile.py` is a Python 2 CSV writer that emits `b'...'` byte representations into downloads (verified) and is still used by two export views. A 71-line view named `load_intervention_mapping_dataxxx` is referenced nowhere.
- **Import pipeline.** The nightly ActivityInfo import deletes a database's roughly 52,000 fact rows and re-inserts them one `INSERT` at a time with no transaction; failures are counted and printed, never raised. Admin actions start the same work in daemon threads inside the gunicorn worker. The export-job poll loop has no attempt limit. The "last updated" timestamp is written before the import runs. The extract is written into the package directory of the container, so the Raw Data download returns a file-not-found error on any fresh instance until that instance has run the import.
- **A data-correctness question that must be checked on the live database.** In the committed 2025 extract the `month` column is empty on all 51,967 rows while `month_of_reporting` is populated; the import stores `month_name` from `month` (`utils.py:342`) and the switch to `month_of_reporting` is commented out (`utils.py:348`). Every HPM, NeuroReport, and analytical query filters on `month_name`. If production imported this shape, the 2025 database's month-based reports are empty or wrong. Sixteen rows also carry a year of 2925 that passes through unvalidated.
- **Robustness.** 9 bare `except:` blocks, 19 `print()` calls, 13 `Model.objects.get(id=id)` calls that return HTTP 500 on any bad id, an unguarded division by `len(items)`, and `.first().attribute` on possibly-empty querysets.
- **Performance.** N+1 loops (one `COUNT` per PCA in the donor feed; one polygon lookup per location), the same queryset evaluated four times, and the analytical feed returns pretty-printed JSON estimated at tens of megabytes for the largest database. For a once-per-session load by a handful of analysts this is a nit rather than a blocker; real timings should be measured before any caching work.
- **Hygiene.** Star imports in `urls.py`, `views.py`, and `admin.py`; 87 pyflakes findings; 16 of 17 files with CRLF line endings; zero tests.

**`etools` (3,918 lines).**

- Four of nine routed pages are provably broken: three query a `Location.point` field that was commented out of the model (FieldError), and the trips monitoring page calls `json.dumps(dict.values())`, a Python 2 idiom that raises TypeError on Python 3. Three of the pages extend `base2.html`, which fails to compile because it uses unregistered template tags, so they die before reaching the database. The pages that render do so through 20 copy-pasted raw SQL blocks with N+1 loops inside. This whole page family is superseded by the `/v2/` pages and linked only from the dead base template.
- The sync layer uses `http.client` with no timeout, no retry, no pagination handling; the travel sync starts at a hard-coded page 45 and discards a whole 1,000-item page on any bad row (partly mitigated: trips from the last 365 days are re-fetched individually, so only older trips go stale); one missing partner aborts every later step of the nightly run; a funding-reservation loop keeps only the last reservation's line items, under-reporting donor funding; `TravelActivity.date` is set from the trip's start date instead of the activity's own date, mis-attributing visits by year.
- The schema mirrors the eTools JSON verbatim: money, dates, and booleans stored as `CharField`, duplicated columns (`end`/`end_date`), one index in 1,373 lines, remote ids reused as local primary keys for three models, 28 cascade deletes reachable from admin delete links. The dashboards that sum money do so in Python from JSON fields, so the text-typed money columns are a nice-to-have fix rather than a blocker. `admin copy.py` (376 lines) is a stale snapshot; seven cloned eTools models are unused; one references a django-tenants attribute that does not exist.
- `etools/admin.py:3` contains `from this import d`, which prints the Zen of Python to stdout on every process start.

**Supporting apps and configuration.**

- Settings are selected by `os.name == 'nt'`, so every Linux developer, CI runner, and container silently gets `production.py`. Only three environment variables are ever read. `asgi.py` points at a different settings module than `manage.py` and `wsgi.py`.
- The `activityinfo` app is confirmed dead at runtime (no imports, empty admin, `urls.py` cannot even import, its only raw-SQL consumer is an unrouted view). It stays in the migration graph through a single dependency line (`etools/migrations/0052` on `activityinfo/0097`), which is a contained piece of migration surgery, not a blocker. `pivoting.Database` is a field-for-field copy of `activityinfo.Database`; `pivoting/client.py` is a copy of `activityinfo/client.py` with cosmetic drift and the same embedded token.
- The `users` app is unadapted cookiecutter boilerplate with a duplicate admin form set and tests that cannot run. The `locations` app has two routes that use regex syntax inside `path()` and have been unreachable since the Django 2 migration, and two DRF views that raise `FieldDoesNotExist` on removed GIS fields. The nightly locations sync swallows a NameError on every iteration and never populates the location tree.
- Celery is pinned but dead (its module is never imported; all settings are commented out); the production cache points at a Docker hostname that does not exist on App Service with exceptions silenced; email goes through SendGrid with an empty API key, and error emails are addressed to the vendor's engineer.
- Django's own deployment check, run during this review on Django 5.2.17, passes with 13 warnings and 0 errors once the missing `openpyxl` package is added. No removed Django API appears outside historical migrations. This is the strongest positive signal in the codebase.

### 4.3 Data model and migrations

- **Worth keeping.** The indicator hierarchy (`ReportingYear` -> `Database` -> `Activity` -> `IndicatorNew` -> `SubIndicator` -> `MasterSubIndicator` with effect -> `MasterIndicator` -> `NeuroReportMasterIndicator` -> `NeuroReport`) matches the SDD exactly and is generic; `ActivityReportNew` is a single denormalised fact table, which is the right shape; eTools replica tables carry unique external ids and idempotent syncs; `Location` uses a proper tree model. Zero `RunPython` or `RunSQL` migrations exist, so a fresh migrate has no data-migration landmines.
- **Needs re-typing in place.** The fact table is "stringly typed": month, location codes, latitude, longitude, partner id, and years are `CharField`, so time series need substring arithmetic in SQL. The fact-to-indicator join runs on an unindexed, nullable `CharField(30)` with no foreign key. Both are fixable with small migrations (a nullable FK populated on import, a backfilled period date column, a composite index) without a schema rewrite. Polygons are stored as `ArrayField(CharField(max_length=64500))` loaded from 39 MB of committed CSVs. Library documents are `BinaryField` rows base64-encoded on every request. Per-database ActivityInfo username and password are stored in plaintext columns that the code no longer uses. Money is text; `djmoney` is installed and never used.
- **Migrations.** 345 files generated by nine Django versions between 2016 and 2024; no squashes; `pivoting`'s initial migration created the whole legacy tree and then deleted it (23 `DeleteModel`, 90 `RemoveField`); one `etools` migration was neutered by hand (`operations = []` next to `operationsx`) and re-applied in the next file, which is evidence that a migration failed in production and was patched on the server. `makemigrations --check` already reports drift in `pivoting` and `users`, while `startup.sh` runs `makemigrations` at every boot for an app named `survey` that does not exist. The production schema must be dumped and compared with the migrations before any schema work; the squash is the single riskiest step of the programme and must be rehearsed on a restored copy.
- **Runtime data in the package.** Population figures and the vulnerability list are read from year-stamped files under `pivoting/uploads/` on every request (the data itself is healthy: all district names map correctly); ActivityInfo extracts are written into the package directory of an ephemeral container.

### 4.4 Libraries and dependencies

**Backend (Python).**

| Issue | Evidence |
|---|---|
| `requirements/production.txt` fails to install with pip 24.1 or later, the default in any fresh environment: `celery==4.2.0` (2018) has invalid metadata and Django is never downloaded. The official `python:3.10-slim` base image may still resolve it with its bundled older pip, so the image build is fragile rather than certainly broken; the image's start command is broken regardless (section 4.6). | Reproduced: `pip install -r requirements/production.txt` exits 1 |
| Removing the four dead pins (celery, beat, results, pathlib backport), everything else resolves to Django 5.2.17, DRF 3.18.1, allauth 65.19.4, pandas 3.0.6. On Python 3.12 the same file would resolve to Django 6.1. Nothing is locked. | Reproduced |
| `openpyxl` (imported at load time by `pivoting/views.py:34`) and `requests` are missing from the production file; only the orphan `_requirements.txt` lists `openpyxl`. A container built from the documented file returns HTTP 500 on every request. | Reproduced: `manage.py check` fails with `ModuleNotFoundError: openpyxl` |
| The only pinned runtime packages are the vulnerable ones: `gunicorn==20.1.0` (2 CVEs, request smuggling, fix 22.0.0) and `Werkzeug==2.2.2` (9 CVEs, never imported by the app). | `pip-audit`: 22 advisories, 11 distinct CVEs |
| Abandoned packages still listed: `django-rest-swagger` (2018, ImportErrors against current DRF, in `INSTALLED_APPS` but unrouted), `awesome-slugify` (2015, file-collides with `python-slugify`), `unicodecsv` (2015), `django-google-tag-manager` (2019), `django-sslserver` (2019), `pathlib` backport, `django-fsm` (self-declared unmaintained, warns at startup). | PyPI release dates |
| About 26 listed packages are never imported or configured; development tooling (ipdb, sphinx, black, flake8, debug-toolbar, pytest) is installed into the production image. | Import grep |
| Five near-identical requirements files; the root `requirements.txt` that the README and the zip-deploy pipeline reference does not exist. | Repo listing |

**Frontend (vendored and CDN).**

| Library | In use | Status |
|---|---|---|
| AdminLTE | 3.2.0 | Current is 4.x, rebuilt on Bootstrap 5; no in-place upgrade |
| Bootstrap | 4.6.1 (plus 4.2.1 in admin builder) | End of life since 2023; 4.2.1 has a known XSS |
| jQuery / jQuery UI | 3.6.0 / 1.13.0 (vendored, on every page) | jQuery UI 1.13.0 has CVE-2022-31160 (XSS); point fix to 1.13.2 |
| Chart.js | 2.9.4 | Current 4.x, incompatible API |
| DataTables / Select2 / pivottable.js | 1.11.4 / 4.0.13 / 2.23.0 (2018) | Majors behind |
| Plotly | `plotly-basic-latest` from CDN on 9 pages | Floating major version; has shipped two breaking majors and a critical prototype-pollution fix since the templates were written |
| jsPDF / html2pdf.js / html2canvas | 1.5.3 / 0.10.1 / 0.4.1 (2013) | 15 advisories on jsPDF including two critical; only on the dead donor-mapping page |
| polyfill.io | loaded on the donor dashboard | Domain hijacked in 2024; must be removed |
| Highcharts | unversioned CDN | Loaded only by dead templates (`survey/`, `etools/donor_mapping*`); no live page uses it, so the licence question disappears once those are deleted |
| Duplicates | jQuery vendored 7 times, Select2 9 times, FontAwesome twice | 129 MB `static/` plus 358 MB committed `staticfiles/` |

The exploitable frontend items are point fixes that take days (upgrade jQuery UI, drop `polyfill.io`, pin Plotly, add integrity hashes). The end-of-life framework versions are not an operational emergency for an internal tool with 18 live pages; the real cost driver is the inline-JavaScript architecture (section 4.5). Base templates that reference 12 static files that exist nowhere in the repository are all dead pages; every static reference in the 46 live templates resolves under the production manifest storage (verified).

### 4.5 UI/UX

**What works.** Every live page extends one base (`baseV2.html`) with a consistent header pattern. The analytical pivot experience (pivottable.js with nine custom aggregators, Plotly renderers, preset views, one-click TSV export) is genuinely useful. Filters are labelled Select2 multi-selects, and the PCA, Partnerships, and Library pages submit them as GET forms so results are shareable URLs. The donor dashboard has a proper loading spinner and empty state. Branded 403/404/500 pages exist.

**What does not.**

- **Architecture.** About 2,900 lines of inline `<script>` across the 18 live pivoting templates (the donor dashboard is 60% JavaScript); no shared module; helpers are re-declared per template. Four base templates coexist; `base.html` is a stale fork still serving the 403 page and user pages with a dead COVID-19 sidebar link. 18 templates carry ` copy`, `_old`, `.orig`, `_new` names; 11 belong to `survey` and `tellme` apps that are not installed; `pca-summary-active` and `pca-summary-all` are 98% identical files.
- **Robustness.** 20 `$.ajax` calls, 20 success handlers, 0 error handlers. An empty dataset leaves the analytical page blank and throws a TypeError. The base template's document-ready block references DataTables unconditionally and throws on six live pages, including the landing page, whenever DataTables is not loaded.
- **Accessibility.** Indicator status is an empty coloured badge (colour-only encoding); rows open modals on click with no button, tab index, or key handler; placeholder alt text; zero `scope=` attributes on table headers; body text forced to 0.75rem with `!important`; `cursor: pointer` on every card.
- **Responsiveness.** The landing page uses `col-6` at every breakpoint with a 72px title and a random 1-of-67 JPEG (largest 968 KB, not lazy-loaded); `min-height: 800px` on all content; 34 of 41 tables lack responsive wrappers.
- **Navigation.** 190 lines of hard-coded sidebar HTML with baked-in report ids, a link to an admin URL for a non-installed app, no active state, no breadcrumbs, 40+ external SharePoint and Power BI links mixed with internal pages. The login page advertises "Use my Microsoft account" (non-functional) and password reset is commented out.
- **Internationalisation.** `USE_I18N=True` but zero translatable strings in any live page, `lang="en"` hard-coded, no RTL provision, in a context where partners work in Arabic and French.
- **Consistency.** Two different filter patterns (GET forms versus AJAX without URL state) across sibling pages, so the Donors Mapping and Intervention Map views cannot be bookmarked or shared.

**Modernisability verdict.** The live surface is small and pattern-based (a table dashboard, a pivot page, a filter-plus-AJAX dashboard, a map, static link lists). It can be modernised page by page inside the existing app: extract inline scripts into built modules, pass data through `json_script`, share one fetch-and-error helper, fix the accessibility basics, and leave the Bootstrap 5 step last and optional.

### 4.6 Deployment, operations, and repository hygiene

- **Five deployment shapes, none runnable as committed:** (1) the production Dockerfile, whose `CMD` runs `service ssh start` with no SSH server installed and then `honcho start` with no Procfile (exits 1, gunicorn never starts; reproduced); (2) `azure-pipelines.yml`, which triggers on `master` while the branch is `main`, tags the image `latest` (no rollback), and deploys only to slot `tst`; (3) `azureproject/pipeline.yaml`, a zip deploy that installs a `requirements.txt` that does not exist and archives the whole 600 MB tree; (4) `startup.sh` for App Service code deploy, which runs `apt-get` and installs crontab at every boot, runs `makemigrations` in production for a non-existent app, relies on an `APP_PATH` variable it never sets, and has CRLF line endings; (5) `passenger_wsgi.py`, a cPanel remnant that is not valid Python. The SDD (2024) says cPanel; the handover document (2026) says containers.
- **Configuration.** The live configuration is not represented anywhere in the repository: `production.py` points the database at `localhost` with a committed password, so the deployed settings must be edited on the server. Whether the live path is App Service code deploy, a container, or cPanel cannot be determined from the repository.
- **Scheduling.** The three nightly syncs run from cron inside the web container: lost on restart until `startup.sh` re-adds them, duplicated on scale-out, competing with request handling, with output going to `print()` and therefore nowhere. Errors are swallowed and the "last updated" timestamp is set before the import runs, so a silently failing sync is indistinguishable from a successful one. Schedules and the mid-month reporting cut-off run in UTC, not Beirut time.
- **Observability.** No health endpoint, no Sentry or Application Insights, console-only logging, 404 and 500 emails routed to the vendor through an email backend with an empty API key.
- **Platform.** The SDD names Azure Database for PostgreSQL Single Server v11, which Microsoft retired on 28 March 2025, and Python 3.9 (end of life October 2025). The Dockerfiles use 3.10 and 3.11-on-buster (Debian buster is end of life). Nothing in the repository backs up the live database; the committed backup scripts target a Docker Postgres that is never deployed. Whether platform point-in-time restore is enabled is unknown.
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

As structured, the code is not unit-testable without refactoring: views open database cursors directly, SQL is built by string mutation, sync tasks call a hard-coded HTTP helper with no injection seam, and business logic lives inside 100-to-500-line `get_context_data` methods. A characterisation safety net for the four critical paths (eTools sync, pivot queries, exports, auth) is roughly three to five person-weeks of integration tests against a seeded PostgreSQL; genuinely safe unit-level coverage requires extracting a data-access seam first, roughly 2.5 to 3 months in total. This is the cost the enhancement roadmap pays in Phases 2 and 3, and it is the reason the two options cost about the same.

### 4.8 Documentation versus reality

- **The SDD v0.5 is a good functional specification.** Its data model, admin wizards, page-by-page feature descriptions, ActivityInfo and eTools endpoint list, and cron schedule all check out against the code. Its "Back-End Restructuring" section explains why the `activityinfo` app, `base2.html`, and the `/etools/` pages still exist: they are residue of an unfinished v1-to-v2 rewrite. It is, however, an unsigned draft with a TODO, an empty sign-off table, and plaintext credentials, and it omits about 40% of the routed surface.
- **The 2026 handover document is not reliable.** It says the `activityinfo` integration is unused (ActivityInfo is the only source of dashboard data; the *app* is dead but the integration lives in `pivoting`), that all data access goes through the ORM (1,702 lines of raw SQL do not), that sections drive authorisation (no section-scoped permission exists), that Celery, Azure Blob storage, allauth-based SSO, and environment-variable secrets are in place (none are), that Django is 3.x (migrations are 5.0.7), and that the app deploys as a container (the image cannot start). A team that estimated an enhancement from this document would budget zero for the raw-SQL layer.
- **Both documents claim all internal pages require authentication and that NeuroDB hosts no APIs.** 23 data endpoints are open.
- **Knowledge-transfer risk is high but narrower than "bus factor of one".** One squashed commit; no runbook, no architecture decision records, no data dictionary; production error mail still routed to the vendor's engineer. But the ActivityInfo and eTools import commands and the HTTP helper carry the UNICEF committer's authorship, so first-line knowledge of the data pipeline is in-house. The genuinely vendor-only areas are the v2 pivot SQL, the templates, and the Azure deployment wiring. The two upstream tokens are very likely personal tokens of current staff, which is why service accounts are the first thing to request.

---

## 5. What is worth keeping

These are the assets the programme builds on:

1. **The PostgreSQL data and the Django migration graph** (after the pending migrations are committed and the schema is reconciled with production): no data migration project is needed.
2. **The indicator domain model** and its admin wizards, the single-current-year invariant, and the name-based tag parsing for gender, nationality, disability, and age group.
3. **The 18 live SQL constants in `pivoting/queries.py`** as the executable specification of the aggregation rules, to be moved behind parameterised query objects and re-expressed once as PostgreSQL views.
4. **The ActivityInfo import chain and the eTools sync commands**, hardened rather than replaced: they encode the partner-name normalisation, emergency and COVID keyword rules, the nutrition special case, and the June/July cut-off that nobody has written down.
5. **The 18 live page templates and `baseV2.html`**, the account templates, the error pages, and the AdminLTE assets they actually reference.
6. **The SDD's functional inventory** (Appendix A) as the frozen scope checklist and acceptance list.
7. **A few good patterns**: the container entrypoint that gates migrate/collectstatic on environment flags, WhiteNoise with manifest storage, the security-middleware baseline, the eTools replica keys, and the per-entity management commands for syncs.

---

## 6. Functional scope (the frozen checklist)

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

Roughly 25 pages, 14 data feeds, 6 exports, 2 upstream integrations, 9 admin model families. The legacy `/etools/` page generation is superseded by the `/v2/` pages and is deleted, not migrated; anything else not on this list needs a written decision before it is kept.

---

## 7. The two options compared

The panel's scores (0 to 10, higher is better for the option):

| Lens | Enhance | Rebuild | Verdict |
|---|---|---|---|
| Engineering risk (probability of landing with a small new team while production keeps running) | 6 | 4 | Enhance |
| Cost and time to value (total cost, when users see improvements, cost of parallel running, resilience to a budget cut) | 7 | 5 | Enhance |
| Security and compliance (speed and durability of closing exposures; auditable end state) | 7 | 6 | Enhance |

| Criterion | A. Enhance in place | B. Rebuild from scratch (data, domain model, and Django kept) |
|---|---|---|
| Time until the live exposures are closed | ~2 weeks (Phase 0) | ~2 weeks (same Phase 0 on the live site) |
| Time until users see improvements | Every two weeks from week one | Month 4 or 5, then a split product (two looks, two logins) for about four months |
| What is reused | Data, models, admin, SQL rules as running code, import and sync chains, 18 live templates, deployment continuity | Data, domain model, SQL rules as a spec, SDD, Django skills |
| What is rewritten anyway | Settings, dependency lock, deployment, sync hardening, view/query layer (Phase 3), presentation layer page by page (Phase 4) | Everything except the kept items |
| Framework risk | Already retired: boots on Django 5.2 with zero errors | None, but no gain either: Django 5 to Django 5 |
| Risk of unknown behaviour | Bounded: the running system stays the oracle while golden tests are captured; no rule has to be decided before a safe change can be made | Every golden divergence forces a product decision before a page can go live, on one part-time product owner |
| Parallel running | None | Two databases, dual syncs, path routing, shared login for 4 to 6 months, operated by a three-person team |
| Failure mode if funding stops | One system, strictly safer than today, at every phase gate | Two half-systems: the outcome the last rewrite produced |
| Riskiest single step | The migration squash and `activityinfo` table drop on a live database with server-generated migrations (rehearsed on a restored copy) | Cutover and reconciliation |
| End state | One settings module, one Dockerfile, lockfile, jobs off the web worker, parameterised SQL, aggregation as views, tests on every route, one layout; fact table re-typed by backfill rather than redesign | Typed schema, one migration per app, tests by construction; cleaner, by perhaps 10 to 20 percent lower yearly maintenance |
| Estimated effort (person-months, after Phase 0) | 11 to 15 | 13 to 18 once parallel running and shared login are priced in |
| Elapsed time | 8 to 10 months, two senior developers plus part-time devops and frontend | 7 to 8 months, three people, with the old site frozen after Phase 0 |

**Verdict: Option A.** The costs are comparable, the end state of a rebuild is cleaner but not by a multiple, and the risk shapes are decisive: enhancement is incremental, reversible on a slot swap, and stoppable at every gate; the rebuild depends on funding continuity and product-owner bandwidth that a country office is least able to guarantee. Enhancement only loses if UNICEF also wants to change the product (a new UX, a new data model, a Power-BI-first reporting strategy) rather than make the current one maintainable.

**What the rebuild case got right, adopted as conditions:** freeze the scope checklist as the parity contract; capture golden fixtures from the 18 live SQL constants against a production copy before any refactor; keep a written divergence register signed by the product owner for every ambiguous rule; record sync runs with staleness alerts; fix decommission dates for the dead page families up front; start the new repository from a tree with no secret in it and treat the old history as compromised.

---

## 8. Recommended roadmap

Realistic budget: 11 to 15 person-months over 8 to 10 elapsed months, at a steady two-week release cadence. Phases 3 and 4 are severable; the minimum commitment that leaves a durable result is through Phase 2 (about 4 person-months, 3 months elapsed).

### Phase 0: stop the bleeding (weeks 1-2, about 0.5 person-months, ships to production)

1. **Rotate** the eTools token, both ActivityInfo tokens, the Django secret key, the database password, the Azure Redis key, the SendGrid key, the staff account password in the comment, and the cPanel credential in the SDD. Request service accounts from eTools and ActivityInfo instead of personal tokens. Read every secret from the environment through the `django-environ` object already in `settings.py`, with no defaults in production.
2. **Close the data endpoints**: `@login_required` on every function view in `pivoting/views.py` and `etools/views.py`, `LoginRequiredMixin` on the two `ListView` exports, `published=True` enforced on the library download, `REST_FRAMEWORK` default permissions set to `IsAuthenticated` with the create/update mixins removed from the locations viewset. Add a test that iterates every URL pattern and asserts anonymous requests are redirected, and never remove it.
3. **Fix the two SQL injection sites** (`pivoting/views.py:75-81`, `etools/views.py:106`) with bound parameters and a whitelist; convert the other `str.replace()` sites to `= ANY(%s)` and return early on empty lists.
4. **Remove** the `polyfill.io` script tag, the placeholder SSO link and the three `/sso/` routes, `output.txt`, the commented credentials, and the `/media/` and `/static/` serve routes; upgrade jQuery UI to 1.13.2.
5. **Make the build install**: delete the four dead pins, add `openpyxl` and `requests`, pin `django~=5.2`, upgrade `gunicorn` to 23 or later, drop `Werkzeug` from production.
6. **Tighten settings**: remove `"*"` from `ALLOWED_HOSTS`; connect the application as a least-privilege database role instead of `postgres`.
7. **Record ground truth** in `docs/OPERATIONS.md`: from the Azure portal, how neuro-db.org is deployed (startup command, app settings, container or code), which database server and name, whether point-in-time restore is on, and whether the cPanel host still serves anything. Dump the production schema and `showmigrations` output into the repository. Run the month-column check from section 10 on the production database.

### Phase 1: reproducible build and one deployment path (weeks 3-6, about 1.5 person-months)

- New clean repository (the history is one commit anyway) with `.gitignore`, `.gitattributes`, `.dockerignore`; `pyproject.toml` with locked dependencies pinned to Django 5.2 LTS; the ~26 unused packages, `django-rest-swagger`, `awesome-slugify`, and the celery scaffolding removed.
- One multi-stage Dockerfile (Python 3.12, non-root, no SSH daemon, gunicorn as the command, the existing entrypoint retained); `azure-pipelines.yml` triggering on `main`, tagging by git SHA, with a CI stage before the build (ruff, `check --deploy`, `makemigrations --check`, smoke tests), deploying to a staging slot and swapping. Delete `startup.sh`, `pipeline.yaml`, `passenger_wsgi.py`, and the nginx/traefik fragments.
- Commit the two pending migrations; add `/healthz` and Application Insights; point `ADMINS` at a UNICEF alias and remove the broken-link email middleware.
- Move the three cron jobs to Azure Container Apps Jobs or WebJobs running the same management commands, with a sync-run record table, per-run logging, and a staleness alert; set the "last updated" timestamp only on success.
- Verify the database platform and migrate off the retired Single Server to Flexible Server 16 with point-in-time restore, rehearsed with one restore to a throwaway server.

### Phase 2: safety net and demolition (weeks 7-12, about 2 person-months)

- pytest-django against a PostgreSQL service container; fixtures seeded from a sample of the committed extract and the population JSON; recorded HTTP for eTools and ActivityInfo.
- Tests: one smoke test per routed URL (302 anonymous, 200 logged in); one golden test per live SQL constant captured against a frozen copy of production; characterisation tests for the sync functions and the import row parser on sample rows. Divergences found while capturing goldens (the zero-target division, the 26 commented-out `funded_by` filters, the nutrition rule, the month column) go into a written divergence register with a product decision each.
- Then delete, with the tests as guard: the `activityinfo` runtime code (migrations kept as a stub until the squash), the legacy `/etools/` routes and templates, `base.html`, `base2.html`, `base_empty.html`, the `survey` and `tellme` templates, every copy/old/orig file, the dead 470 lines of `utils.py` and the admin actions that call them, the four dead SQL constants, `gistfile.py`, the unreferenced AdminLTE plugins and JavaScript bundles.
- Fix the two NameErrors, the Zen-of-Python import, the star imports, the admin permission overrides, and the trip-sync page range.
- **Gate:** every route exercised in CI, ~40 to 50 percent line coverage on `pivoting` and `etools` via integration tests, one base template, roughly a third less Python and two thirds fewer templates.

### Phase 3: backend restructuring (weeks 13-22, about 3 person-months)

- Introduce a service layer: `queries.py` as parameterised query objects; the copy-pasted dashboard blocks, the polygon lookups (cached), and the four filter parsers extracted; views become thin adapters with `get_object_or_404`. Every moved query is diffed old-versus-new per database, indicator, and month against the production copy before the old path is deleted.
- Extract the shared aggregation subquery (repeated five times in the NeuroReport query) into PostgreSQL views so "achieved", the zero-target rule, and the `funded_by` rule exist once; expose them to Power BI.
- Sync hardening: `transaction.atomic` and `bulk_create` around the delete-and-insert import, a bounded export poll, per-step error isolation and non-zero exit in the eTools command, trip pagination from page one, the activity date taken from the activity, timeouts and retries in a single integrations client.
- Fact-table typing in small migrations: a composite index on database, indicator, and month; a nullable foreign key to the indicator populated on import; a backfilled period date column; float coordinates. Then squash the `etools` and `pivoting` migrations and drop the `activityinfo` tables, rehearsed end-to-end on a restored copy and executed in a maintenance window.
- Replace the hand-rolled SSO with allauth's Microsoft provider pinned to the UNICEF tenant with no auto-provisioning; design a section-level authorisation model and restrict partner staff data to roles that need it.
- **Gate:** `views.py` reduced to thin adapters, zero string-built SQL sites (enforced by a test), nightly import atomic and observable, aggregation semantics in one place.

### Phase 4: frontend cleanup (weeks 23-32, about 3 person-months, severable)

- Page by page across the 18 live templates: inline scripts into modules built with Vite, server data through `json_script`, one shared fetch-with-error-and-empty-state helper, a Content Security Policy with nonces, pinned and vendored chart and map libraries, Maps key from settings with referrer restriction, the sidebar from a context processor with active state and breadcrumbs, one filter pattern with URL state, visible status text beside badges, keyboard-reachable rows, alt text, header scopes, responsive grids, the 12px body override and the global card cursor removed, landing images resized and lazy-loaded.
- Bootstrap 5 and AdminLTE 4 last, one page per pull request behind the smoke tests, with Lighthouse and axe in CI. Decide Arabic/French support as an explicit product question.
- **Gate:** one layout, one vendor bundle, no unpinned third-party script, AJAX failures visible to users, accessibility basics met.

### Team, budget, and gates

- Two senior Django/PostgreSQL developers (at least one comfortable with raw SQL and views, since the business rules live in `queries.py`), a part-time Azure engineer for Phase 1, a part-time frontend engineer from Phase 4, and the UNICEF innovation lead as product owner for 2 to 4 hours a week of acceptance and rule decisions. Interview the former vendor engineer against the scope checklist in the first 30 days if reachable.
- No new feature lands before the Phase 2 gate. If budget tightens, the programme stops at a gate; by construction that leaves a hardened, pinned, single-path, monitored system.
- Decision gates: **Phase 0** all critical findings closed in production and the build reproduces in CI; **Phase 1** any team member can build, test, and deploy from the repository, syncs run outside the web container with alerts, database on a supported platform; **Phase 2** every route under test, golden fixtures captured, dead code gone; **Phase 3** old and new query results match on the production copy, migrations squashed; **Phase 4** page-by-page product-owner acceptance.

---

## 9. Risks of the recommended path and how they are handled

| Risk | Mitigation |
|---|---|
| The programme drifts into "a slow rewrite in the old repository's shape" | Phase gates with measurable exit criteria; a feature freeze until Phase 2; the product owner reviews the gate checklist |
| Refactoring the view layer and the import function without production-shaped data changes dashboard numbers silently | Golden tests captured before any refactor; old and new query paths diffed per database, indicator, and month on a production copy; the old constant stays callable behind a flag until the diff is clean |
| The migration squash and `activityinfo` table drop fail on a live database whose schema contains server-generated migrations | Schema dump and `showmigrations` captured in Phase 0; full rehearsal on a restored copy; maintenance window with point-in-time restore verified first |
| Production ground truth turns out to be cPanel or a hand-edited App Service | Phase 0 item 7 establishes it before any engineering; if it is cPanel, Phase 1 grows by two to three weeks rather than being absorbed silently |
| Service-account tokens from eTools and ActivityInfo take longer than the code | Requested in week 1; the integrations client accepts whatever token exists; escalated as an open compliance item, not absorbed |
| The two people who understand the SQL semantics are unavailable | Golden tests freeze current outputs first; the vendor engineer is interviewed early; the divergence register records every decision |
| The Bootstrap 5 migration has no automated path and slips | Deliberately last and severable; the security and maintainability gains land before it |
| Data-quality surprises in the fact table (empty month column, text coordinates, text partner ids) | Section 10 checks run in Phase 0; the period backfill in Phase 3 reports coercion failures per row instead of guessing |

---

## 10. Open questions to answer before Phase 1

None of these can be settled from the repository; each is a one-hour check that changes the plan if the answer is bad.

1. **Is the 2025 fact data missing its month?** On the production database: count rows per database where `month_name` is empty. If the unified 2025 database has empty months, the HPM and NeuroReport pages have been wrong since the import format changed, and fixing the import (`utils.py:342-348`) plus a re-import moves into Phase 0.
2. **What actually runs?** App Service configuration (startup command, app settings, deployment centre source), `crontab -l` and `pip freeze` on the instance, and whether the cPanel host is still live.
3. **Is the database backed up?** The Azure Postgres backup settings, one rehearsed point-in-time restore, and recorded recovery objectives. The database is the only irreplaceable asset.
4. **What does the platform cost?** The SDD's figures (about 160 USD per month for the app plan and the retired database tier) predate the Single Server retirement; price Flexible Server plus a small job runner from Cost Management.
5. **Is there a data-protection record?** Whether a UNICEF Personal Data Protection assessment exists for NeuroDB, whether partner staff contact details are needed at all (they are shown only in a modal), and what the retention rule is for eTools replica rows flagged deleted upstream.
6. **How fast are the pages today?** Row counts of the largest tables and timings of the HPM report and the analytical feed on the live system, so that caching and materialisation work is sized from measurements rather than assumptions.
7. **Who owns the landing images?** 67 photographs on the public landing page have no provenance or consent record.
8. **What does the yearly rollover involve?** Walk the January procedure with the current committer (new reporting year, new database rows, structure import, wizards, the year-stamped population file, the HPM PDFs) and write it into the runbook; missing a step today yields blank dashboards with no error.

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
- `/v2/database-download/?id` : streams `pivoting/AIReports/<ai_id>_ai_data.xlsx` (no auth; file exists only on instances that ran the import)
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

## Appendix B: verification record

**Adversarial verification.** 59 high and critical findings were each re-examined by a separate verifier instructed to refute them: 23 confirmed as written, 36 confirmed in substance with severity or framing adjusted, 0 refuted. The notable adjustments, all reflected in this report:

| Finding | Adjustment |
|---|---|
| "The production Docker image cannot build" | True with any pip 24.1 or newer; the official base image's bundled pip may still resolve the file. Downgraded to medium; the image's start command is broken regardless (confirmed high). |
| "Dead `activityinfo` app is critical and forces a rewrite" | Runtime impact is zero and the migration coupling is a single dependency line; downgraded to low, fix is contained. |
| "Frontend stack cannot be patched incrementally" | Exploitable items are point fixes; framework end-of-life is not an emergency for 18 internal pages; the real driver is inline JavaScript. |
| "Missing static files cause HTTP 500 on live pages" | Affects only dead pages; every static reference in the 46 live templates resolves under manifest storage. |
| Highcharts licence and unpinned CDN | Loaded only by dead templates; moot after deletion. |
| "50 raw cursor sites" in `views.py` | 30 executions (20 cursor creations); the file is live and routed. |
| "Eight SQL sites take request parameters" | One takes user input directly; three take database-sourced values; the rest are constants. |
| eTools token occurrences | 13 sites in three apps (some reviewers counted 7 or 10). |
| ActivityInfo tokens | Two distinct tokens at six sites in four files, not one token in two files. |
| Unauthenticated endpoint counts (20, 22, 25) | Scope differences: 20 in `pivoting`, 2 in `etools`, 3 DRF routes of which 2 are syntactically dead. |
| "Bus factor of one, knowledge with the vendor" | The import and sync commands carry the UNICEF committer's authorship; vendor-only areas are the pivot SQL, templates, and Azure wiring. |
| Power BI publish-to-web links | Not a NeuroDB defect; a publishing decision. |
| Money stored as text | Real, but the dashboards sum money in Python; nice-to-have rather than a rewrite driver. |
| Trip sync starting at page 45 | Partly mitigated by the 365-day detail re-fetch. |
| Personal-data framing | UNICEF's Policy on Personal Data Protection applies, not GDPR. |

**Findings added by the completeness critic** (all verified in the code, none against the live system): the empty month column in the committed 2025 extract; the unguarded division by the default-zero target in the NeuroReport query; the Raw Data download depending on files present only on instances that ran the import; three different production database names and an internet-exposed superuser connection in the SDD; `startup.sh` relying on an unset `APP_PATH` and UTC schedules; token rotation requiring code changes in three files; two further false architectural claims in the handover document (ORM-only access, section-scoped authorisation).

**Directly reproduced by the review lead:**

| Finding | How |
|---|---|
| Unauthenticated SQL injection via `emergency` | Code read at `pivoting/views.py:70-81` and `queries.py:668`; no auth decorator; string building reproduced |
| 23 unauthenticated data endpoints | Every route in the five `urls.py` files mapped to its view; absence of `login_required`, `LoginRequiredMixin`, `is_authenticated`, and `REST_FRAMEWORK` confirmed |
| Authenticated SQL injection via `.extra(where=)` | Code read at `etools/views.py:76, 106` |
| Locations API create/update open | `locations/views.py:17-24` read; `REST_FRAMEWORK` absent from settings |
| Committed secrets | Each cited file and line read; values not recorded |
| SSO placeholders, tenant `common`, auto-provisioning, dead redirect | `azureproject/sso_views.py` read in full; `dashboard` URL name absent |
| Production requirements do not install | `pip install -r requirements/production.txt` on Python 3.11 with pip 25: exit 1 at `celery==4.2.0` |
| Missing `openpyxl` breaks every request | `manage.py check` after installing the remaining requirements: `ModuleNotFoundError` from `pivoting/views.py:34` |
| Code boots on Django 5.2 | `manage.py check --deploy` after adding `openpyxl` and `requests`: exit 0, 13 warnings |
| Migration drift | `makemigrations --check --dry-run`: pending migrations in `pivoting` and `users` |
| 11 CVEs in pinned packages | `pip-audit` on the resolved set |
| Django version history 2016 to 2024 | `Generated by Django` headers in all migration files |
| `from this import d` | `etools/admin.py:3` read; Zen of Python observed on `manage.py check` output |
| Committed bytecode rewritten by any Python run | 409 tracked `.pyc` files showed as modified after `manage.py check` |

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

Suggested first check on the production database (section 10, item 1):

```
SELECT database_ai_id,
       count(*) FILTER (WHERE month_name = '' OR month_name IS NULL) AS empty_month,
       count(*)                                                     AS total
FROM pivoting_activityreportnew
GROUP BY 1 ORDER BY 3 DESC;
```
