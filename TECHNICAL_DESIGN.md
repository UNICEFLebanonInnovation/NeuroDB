## NeuroDB – Technical Design & Handover Document

### 1. Executive Summary (Non‑Technical)

NeuroDB is a web-based data platform that helps UNICEF and partners analyze programme data in one place. It combines data from core UNICEF systems (especially eTools) with a flexible analytics layer so users can:

- **View dashboards and pivot tables** to understand results by location, partner, sector, and time.
- **Drill down** into specific geographic areas or programme components.
- **Export data** into standard formats (e.g. Excel/CSV) for sharing and offline analysis.

The application is built using the **Django** web framework and runs as a **containerized app on Microsoft Azure**. It uses **PostgreSQL** as its main database. From a client perspective, the most important module is called **`pivoting`**, which powers dashboards, indicators and analytical views that end users interact with.

On an ongoing basis, the application team will mainly:

- Monitor the Azure deployment and database health.
- Ensure data synchronization from upstream systems (e.g. eTools) continues to run correctly.
- Make small adjustments to indicators, dashboards, and filters in the `pivoting` module, as business needs evolve.

> **Important:** The legacy `activityinfo` app in the codebase is **not used** in this deployment and can be ignored for maintenance and future enhancements.

---

### 2. System Overview

- **System name**: NeuroDB  
- **Primary purpose**: Provide an integrated environment for analyzing programme and results data (indicators, population figures, interventions, partners, etc.) with strong support for filtering and pivoting.
- **Technical style**: Monolithic Django web application with a modular internal structure (multiple Django apps).
- **Hosting**: Containerized deployment to **Azure Web App for Containers**, using images built and pushed via **Azure Pipelines**.
- **Primary data store**: **PostgreSQL** database.

High-level layers:

- **Presentation Layer**
  - Server-rendered HTML front end built with Django templates.
  - Visual components use AdminLTE, Bootstrap, jQuery and custom JavaScript for dashboards and pivot tables.
  - Django Admin is styled and organized with `django-jazzmin`.

- **Application Layer (Django Apps)**
  - `pivoting`: Core analytics, indicators, dashboards, and exports.
  - `etools`: Local representation of selected entities from UNICEF eTools (partners, interventions, etc.) and sync logic.
  - `locations`: Administrative location hierarchy (governorate/district/etc.) and related utilities.
  - `users`: Custom user model, sections, offices, and authentication integration.
  - `azureproject`: Project-wide configuration (settings, URLs, WSGI/ASGI, Celery, SSO views).
  - `utils`, `contrib`, and others: Shared helpers and support modules.

- **Integration / Background Layer**
  - Management commands and (optionally) Celery tasks to synchronize data from external systems (primarily eTools).

---

### 3. Key Technologies

- **Language & runtime**
  - Python 3.x
  - Django (3.x series)

- **Web & API framework**
  - Django (views, templates, URL routing)
  - Django REST Framework for selected JSON APIs

- **Database & storage**
  - PostgreSQL for relational data.
  - Local or Azure Blob Storage (via `django-storages`) for static and media files, depending on environment.

- **Frontend**
  - Django templates.
  - AdminLTE-based theme, Bootstrap, FontAwesome.
  - jQuery and various JS libraries for charts and pivot tables.

- **Auth & security**
  - Django’s authentication system with a **custom user model** in the `users` app.
  - Integration with `django-allauth` and optional SSO via Azure AD (implemented in `azureproject/sso_views.py` and settings).

- **Background processing**
  - Celery configuration present (for asynchronous tasks) with recommended use of Redis as a broker in production.

- **Infrastructure & delivery**
  - Docker images built from `compose/production/django/Dockerfile`.
  - Azure Pipelines (`azure-pipelines.yml`) for build and deploy.
  - Azure Web App for Containers as the runtime host.

---

### 4. High-Level Architecture & Main Components

#### 4.1 Django Project Structure

- **`azureproject/`**
  - `settings.py`: Base Django settings shared across environments.
  - `local.py`: Local development settings (e.g. Windows).
  - `production.py`: Production deployment settings (Azure).
  - `urls.py`: Root URL configuration; includes URLs from the main apps.
  - `wsgi.py` / `asgi.py`: Web server entry points.
  - `celery.py`: Celery configuration.
  - `jazzmin_settings.py`, `jazzmin_ui_tweaks.py`: Admin UI customization.
  - `sso_views.py`: SSO login/logout flows for integration with Azure AD (where enabled).

- **`pivoting/` (Main App)**
  - Models for:
    - Indicators and sub-indicators.
    - Population figures and related data.
    - Additional entities needed to support complex pivots (e.g. mappings between indicators, locations, and programme attributes).
  - Views and services to:
    - Compute aggregates (sums, counts, percentages) over time, location, partner, and other dimensions.
    - Expose pivot data to templates and selected JSON endpoints.
  - Templates under `templates/pivoting/` for dashboards, filters, and pivot UI.
  - Exports to Excel/CSV using Python libraries (e.g. `pandas`, `openpyxl` or similar).

- **`etools/`**
  - Models mirroring key eTools entities, for example:
    - Partner organizations.
    - Agreements and interventions.
    - Trips, action points, audits.
  - Management commands (under `etools/management/commands/`) to:
    - Pull data from eTools APIs.
    - Map and persist data into local models.
  - Serializers and views (where needed) for internal or external API use.

- **`locations/`**
  - `LocationType` and `Location` models defining a hierarchical tree of locations (e.g. governorate → district, etc.).
  - Management commands to import master location data.
  - Serializers and views exposing locations for filtering and selection in the UI and in `pivoting`.

- **`users/`**
  - Custom `User` model with additional attributes:
    - Section, office, backup user, etc.
    - Optional links to Power BI URLs or other data-visualization resources.
  - Admin configuration for managing users, sections, and offices.
  - Views and templates for user management and profiles.

- **`utils/` and `contrib/`**
  - Helper utilities (e.g. admin mixins, custom form widgets, filters).
  - Shared logic reused by multiple apps.

---

### 5. Data Model & Persistence

- **Database**
  - PostgreSQL is the primary backing store.
  - All data access goes through Django’s ORM.
  - Migrations live under each app’s `migrations/` folder and are applied via:
    - `python manage.py migrate`

- **Core domains**
  - **Analytics & indicators (pivoting)**
    - Master indicator and sub-indicator definitions.
    - Relationships between indicators, time periods, locations, partners, and other dimensions.
    - Population figures and related contextual datasets.
  - **Programme data (etools)**
    - Partners, agreements, interventions, trips, action points, audits and similar.
    - These records are treated as “source-of-truth replicas” from eTools to support analytics.
  - **Locations (locations)**
    - Location types and a hierarchical tree of locations.
    - Used in nearly all filters and aggregations that require a geographic breakdown.
  - **Users & sections (users)**
    - Custom user model with relationships to sections and offices.
    - Used to drive authorization and default filter scopes in `pivoting`.

---

### 6. External Integrations

- **UNICEF eTools**
  - Source of programme and partner data.
  - Integration is implemented via HTTP REST calls in management commands under `etools`.
  - Authentication is configured via tokens/credentials in Django settings or environment variables.
  - Data is periodically synchronized and stored locally in PostgreSQL for analytics.

- **Azure Active Directory (SSO)**
  - Optional login flow using Azure AD.
  - Configured with client ID, client secret and tenant details in environment variables and `azureproject` settings.
  - Implemented using `django-allauth` plus custom views in `azureproject/sso_views.py`.

- **Email**
  - Email deliveries (e.g. notifications) configured using Django’s email backend or `django-anymail` with SendGrid or similar providers.
  - Credentials provided as environment variables in production.

- **Static & media storage**
  - Local filesystem in development.
  - Azure Blob Storage or equivalent in production, when `django-storages` is enabled and configured in settings.

> Note: The unused `activityinfo` app is **not** part of the live data flow and can be completely ignored in operations and future development.

---

### 7. Configuration & Environments

- **Configuration**
  - Centralized in `azureproject/settings.py` with overrides in `local.py` and `production.py`.
  - Sensitive values are expected to come from environment variables (or from a `.env` file in development) using `django-environ`.

- **Key configuration values**
  - Security: `SECRET_KEY`, `ALLOWED_HOSTS`, CSRF/session settings.
  - Database: host, port, name, user, password, SSL.
  - External services: eTools API credentials, email/Anymail keys, Azure AD SSO credentials.
  - Storage: `STATIC_ROOT`, `MEDIA_ROOT`, Azure storage connection string and container names where used.

- **Environments**
  - **Local development**
    - Uses `local.py`.
    - Typically connects to a local PostgreSQL instance (or a Dockerized one via `compose/`).
  - **Test / Production**
    - Uses `production.py`.
    - Runs inside Docker containers on Azure App Service, reading configuration from environment variables provided by Azure.

---

### 8. Deployment & CI/CD

- **Containerization**
  - Docker image built from `compose/production/django/Dockerfile`.
  - Installs dependencies from `requirements/production.txt` (and base requirements).
  - Runs Django’s `collectstatic` as part of the build to prepare static assets.

- **Entry point / startup**
  - At container startup (entrypoint script):
    - Optionally runs `python manage.py migrate` when `DJANGO_MIGRATE=on`.
    - Optionally runs `python manage.py collectstatic` when `DJANGO_COLLECTSTATIC=on`.
    - Launches Django via Gunicorn.

- **Azure Pipelines (`azure-pipelines.yml`)**
  - Triggered on changes to the main branch (typically `master`).
  - Steps:
    1. Build the Docker image.
    2. Push image to Azure Container Registry (e.g. `unilebepics.azurecr.io/...`).
    3. Deploy image to Azure Web App for Containers (e.g. `uni-leb-neourodb-tst` in resource group `rs-uni-leb`).

- **Runtime hosting (Azure App Service)**
  - One or more Web Apps (and optionally slots) mapped to domain names such as `neuro-db.org` (production) and Azure-provided URLs for test environments.

---

### 9. Operations & Maintenance

For the receiving team, the most important recurring activities are:

- **Monitoring**
  - Monitor Azure Web App availability, response times, and error rates.
  - Monitor PostgreSQL health (disk usage, connections, slow queries).

- **Data synchronization**
  - Ensure eTools sync commands are running on the desired schedule (e.g. via Azure WebJobs or cron in a companion container).
  - Investigate and resolve any failed sync runs (connection failures, API changes, schema mismatches).

- **User and access management**
  - Create and manage user accounts in Django Admin or via SSO provisioning.
  - Keep section and office metadata up to date so that dashboards show the correct information for each unit.

- **Performance tuning**
  - For slow dashboards or pivots, review:
    - Database indexes on high-usage filter and join fields (year, location, section, partner, indicator).
    - Use of caching (Redis) for expensive computations.
  - Consider materializing particularly heavy aggregations (e.g. nightly summary tables) if needed.

---

### 10. Onboarding a New Developer/Team

Steps for a new technical team to get started:

1. **Set up local environment**
   - Install Python and PostgreSQL.
   - Create a PostgreSQL database for NeuroDB.
   - Clone the repository.
   - Create a `.env` file (or configure environment variables) with at least:
     - `SECRET_KEY` (development-safe value)
     - `DEBUG=on`
     - Database credentials.

2. **Install dependencies & run migrations**
   - Install Python dependencies from `requirements/` (typically `base.txt` and `local.txt`).
   - Run:
     - `python manage.py migrate`
     - `python manage.py createsuperuser` (for initial admin access).

3. **Run the app locally**
   - Start the server with `python manage.py runserver`.
   - Log in to Django Admin and verify the core apps (`pivoting`, `etools`, `locations`, `users`) appear and admin pages load.

4. **Understand the main flows**
   - Focus first on:
     - `pivoting/` for analytics and dashboards.
     - `etools/` for how external data is brought in.
     - `locations/` for the geographic hierarchy.
     - `users/` for sections/offices and access scoping.

5. **Extend or modify functionality**
   - To add a new dashboard or indicator:
     - Update or create models in `pivoting`.
     - Implement or adjust queries/aggregations.
     - Add corresponding templates and any required JS for the UI.
   - To integrate a new external data source:
     - Introduce a new app (or extend `etools`) with models for that source.
     - Implement sync logic (management commands, API clients).
     - Connect that data to `pivoting`’s indicators and dashboards.

With the above understanding, a new team should be able to maintain, operate, and evolve NeuroDB, with a particular focus on the `pivoting` app as the main user-facing and business-critical component. The unused `activityinfo` app can be disregarded.

