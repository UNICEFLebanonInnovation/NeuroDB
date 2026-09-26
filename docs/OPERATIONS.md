# Operations runbook (v3)

## Deploy and rollback
The platform is Azure Container Apps: one image runs the website (`<prefix>-web`) and every job
(`<prefix>-migrate`, `-ai-structure`, `-ai-data`, `-etools`, `-locations`, `-freshness`). Setup and
architecture: `docs/DEPLOYMENT_AZURE.md`.

CI builds `<acr>/neurodb:<commit sha>` on `main` and runs `infra/scripts/deploy.sh` for staging and
then, after approval, production. The script runs migrations as a job first and stops if they fail.
It then rolls out a new web revision, which only takes traffic when `/healthz/` is ready, and
switches the jobs to the new image. In addition, every web container applies pending migrations
before it starts (`RUN_MIGRATIONS=true`, the default), under a database lock so only one container
migrates at a time. On App Service this is what migrates the database on each deployment.

Rollback: `infra/scripts/rollback.sh` with the previous image tag. Migrations are not reversed, so
every migration must stay compatible with the previous release for one deployment.

## Configuration
All settings are environment variables (`.env.example`). In Azure they are set by
`infra/main.bicep`; secrets are Key Vault references read with the app's managed identity. There
is no configuration file in the image. Change a non-secret setting by editing the Bicep parameters
and re-running the deployment.

## Secrets rotation
Key Vault secrets: `django-secret-key`, `database-url`, `activityinfo-token`, `etools-token`,
`etools-username` and `etools-password` (the eTools Datamart service account), (with SSO)
`entra-client-secret` and (with the AI assistant) `openai-api-key`. To rotate, set a
new version in Key Vault, then restart the active web revision (`az containerapp revision
restart`). Jobs pick the new value up on their next run.
No code change is needed. Rotating the Django secret key signs everyone out and changes the
per-user identifier sent to OpenAI (`safety_identifier`, below), so OpenAI's misuse history for
each user starts afresh.

## AI assistant (Ask NeuroDB)
Signed-in users ask questions in plain language on `/ask/` or from the search box (Ctrl K).
ChatGPT answers them: an OpenAI GPT model called through the OpenAI API (platform.openai.com,
Responses API), not the consumer ChatGPT app. The model calls read-only lookups over NeuroDB's own
services (indicator results, activity reports, Neuro reports, programme documents, donors,
partners, every eTools Datamart dataset NeuroDB holds (funds, indicators, assurance, reporting,
monitoring, PD narratives and reviews..., without e-mail addresses or phone numbers),
population, library, data freshness) and links each answer to the pages its figures
come from.

- **API key and billing** (platform.openai.com, with an organisation account, not a personal one):
  in the organisation, create a project named `NeuroDB` (only organisation owners can create
  projects), then create an API key owned by that project on the API keys page
  (`https://platform.openai.com/settings/organization/api-keys`). API usage is prepaid: buy credits
  under Billing. It is billed separately from any ChatGPT subscription, which includes no API
  credit. Set a budget and usage alerts on the `NeuroDB` project's limits page.
- **Switch on**: store the key in Key Vault as `openai-api-key` and reference it as
  `OPENAI_API_KEY` (App Service:
  `@Microsoft.KeyVault(VaultName=neurodb-prod-kv;SecretName=openai-api-key)`; Container Apps:
  `enableAiAssistant = true` in `main.bicepparam`). Restart the app. Without a key the assistant is
  off and the search box works as before. Never put the key in a committed file or a Bicep
  parameter.
- **Settings**: `AI_ASSISTANT_MODEL` (default `gpt-5.5`; it can be switched to a newer model such
  as `gpt-6-sol`, released on 2026-09-22, after checking its price on OpenAI's pricing page and
  trying it on staging), `AI_ASSISTANT_EFFORT` (reasoning effort, default `medium`; the API's
  values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh` and `max`, but not every model
  accepts every value: gpt-5.5 takes `none`, `low`, `medium`, `high` and `xhigh`. Lower is cheaper
  and faster, higher reasons longer. Any other value stops the app at start-up: the website and
  the jobs do not start until it is corrected), `AI_ASSISTANT_HOURLY_LIMIT` (questions per user per
  hour, default 30), `AI_ASSISTANT_MAX_TOOL_ROUNDS` (rounds of lookups per question, default 8),
  `AI_ASSISTANT_TIME_LIMIT_SECONDS` (default 180) and `AI_ASSISTANT_ENABLED=false` to switch it
  off with the key still set. A question counts towards the hourly limit from the moment it is
  asked, and a user can have at most two questions being answered at the same time.
- **Data leaves Azure**: each question, up to six earlier questions and answers of the same
  conversation, and the data looked up to answer it are sent to the OpenAI API (api.openai.com
  over HTTPS). That data is the figures plus partner, programme-document, donor and library
  details, such as partner names, vendor numbers, risk ratings, HACT and assurance results,
  programme document titles, library summaries and the free-text comments staff write on Neuro
  and HPM reports. When a filter matches nothing, the list of known partner, donor or office names
  is sent too. It is organisational data: field visits are sent as counts, and no traveller names
  or partner staff contacts are sent. Only signed-in users can ask, and the lookups can only read
  what any signed-in user can already see. Nothing is written back. Requests are sent with
  `store=false`, so OpenAI does not keep the responses for later retrieval through the API;
  NeuroDB keeps its own question log. Each request carries a keyed hash (HMAC-SHA-256 with the
  app's Django secret key) of the signed-in user's internal id (`safety_identifier`), never a name
  or email address, so OpenAI can flag misuse per user. OpenAI's published API data terms say
  that API data is not used to train models unless the organisation opts in, that abuse-monitoring
  logs may be kept for up to 30 days, and that Zero Data Retention is available only to approved,
  eligible customers; check the current terms on openai.com before switching on. Clear this with the
  data protection focal point before switching it on; outbound HTTPS to api.openai.com must be
  allowed.
- **Cost and review**: every question is logged in Admin → Data and sync → AI questions with the
  lookups made, tokens used and time taken. Output tokens include the model's reasoning tokens,
  which are billed as output. A typical question makes 2-4 lookups. The instructions and tool
  definitions are sent first and unchanged on every request, so OpenAI's automatic prompt caching
  (prompts of 1,024 tokens or more, no setup) bills that repeated part at the cheaper cached-input
  rate. Prices per model are on OpenAI's pricing page.
- **Refusals and failures**: when the model declines a question, it is logged as "Declined by the
  model" and the user is asked to rephrase; there is no automatic retry on another model. A
  question blocked by OpenAI's safety checks (error codes `invalid_prompt`, `bio_policy`,
  `cyber_policy`, `misalignment_policy_violation`) is logged the same way, with the code in the
  Error field; asking the same question again will not help. If every question fails, open a
  failed question in the log: its Error field (and the app logs) shows the cause, for example an
  invalid or revoked key (401), used-up credits (429 `insufficient_quota`: buy credits, retrying
  does not help), rate limits (429) or an unknown model id (404). A question still being answered
  shows as Failed with the Error "in progress" until it ends.
- **Azure OpenAI is a different service**: Microsoft's Azure OpenAI hosts OpenAI models inside an
  Azure subscription, with its own endpoint (`https://<resource>.openai.azure.com`), its own keys
  or Entra ID sign-in, and deployment names instead of model ids. A platform.openai.com key does
  not work there. Moving the assistant to Azure OpenAI (for example to keep the traffic in the
  Azure tenant) needs a small code change (the SDK's `AzureOpenAI` client) and different settings.

## Scheduled jobs
Container Apps cron is **UTC**; Beirut is UTC+3 in summer and UTC+2 in winter.

| Job | Command | Schedule (UTC) | Beirut (summer) |
|---|---|---|---|
| `locations` | `manage sync_locations` | `0 2 * * *` | 05:00 daily |
| `ai-data` | `manage import_activityinfo_data --current-year` | `0 15 1-22 * *` | 18:00, days 1–22 |
| `etools` | `manage sync_etools_datamart` | `30 17 * * *` | 20:30 daily |
| `freshness` | `manage check_sync_freshness` | `15 * * * *` | hourly; a stale source fails the run |
| `ai-structure` | `manage import_activityinfo_structure --all` | manual | after yearly rollover |
| (in `ai-data` and `etools`) | `manage link_partners` | runs at the end of both jobs (the `etools` job only when partners or programme documents were synced) | ActivityInfo → eTools partner links |
| `migrate` | `migrate` (then `bootstrap_roles`, which also gives the Administrator group every model permission so new tables show in the admin, and `link_partners`) | manual, run by the pipeline | |

Run one now: `az containerapp job start -n <prefix>-<job> -g <resource group>`.
Every run writes a `SyncRun` row (admin → Core → Sync runs; page `/data/health/`), and the job's
execution history keeps its logs.
Triage a failure: open the run, read `error`, re-run the command with `--database <ai_id>` or
`--only <entity>` (`az containerapp exec … --command "neurodb manage …"`). A `PARTIAL` run lists the
failed item ids in `details`.

## eTools Datamart

**Running the sync now.** Admin → *Data and sync* → *Import and sync runs* → **Sync eTools now**
(administrators): choose *Partners and programme documents* (minutes), *Everything* (up to two
hours) or *Only the datasets ticked below* (for example `locations` alone after a gazetteer fix).
It runs in the background in the web container; each dataset appears in that list as it
finishes. Only one Datamart sync runs at a time (a database lock), whoever starts it. A deployment
restarts the container and kills a sync in progress: its run stays *Running* until the next
**Sync eTools now**, which sees that nobody holds the lock, closes the run as *Failed* ("Cut off")
and starts. On Container
Apps the `etools` job also runs it every night; **App Service has no scheduler**, so there the data
only changes when someone starts it (or through a Container Apps job pointed at the same database).
From a shell with the same settings: `python manage.py sync_etools_datamart [--only a,b]`.

The nightly eTools sync reads the **eTools Datamart** (`https://datamart.unicef.io`, the new eTools
APIs) with HTTP basic authentication: `ETOOLS_USERNAME` / `ETOOLS_PASSWORD`, from the Key Vault
secrets `etools-username` / `etools-password`. Nothing else holds the credentials: they are not in
the image, the repository, the logs or the `SyncRun` errors, and the client only sends them to the
Datamart host (a pagination link to any other host is refused). Every request is limited to
`ETOOLS_DATAMART_COUNTRY` (default `Lebanon`).

`manage sync_etools_datamart [--only a,b]` runs these datasets in order, one `SyncRun` each (job
`etools_datamart`):

| Dataset | Datamart endpoint | Written to |
|---|---|---|
| `locations` | `locations/` | the locations table (`locations.Location`, id = eTools id): name, P-code, admin level (`LocationType`), parent, latitude and longitude — the gazetteer every other eTools record links to |
| `location_sites` | `location-sites/` | `datamart.MonitoringSite`: field-monitoring sites with their point and parent location |
| `partners` | `partners/` | the existing partner table (`etools.PartnerOrganization`, by eTools id) |
| `interventions` | `interventions/` | the existing programme documents (`etools.PCA`) and their agreements, sections, offices, focal points, donors, grants and planned locations |
| `intervention_budgets` | `interventions-budget/` | the budget columns of the programme documents |
| `agreements` | `partners/agreements/` | type and dates of the agreements |
| `funds_reservations` | `funds-reservation/` | `datamart.FundsReservation`, linked to the PD; rebuilds each PD's donor amounts (donor page) |
| `grants` | `funds/grants/` | `datamart.Grant` (expiry dates on the donor page) |
| `pd_indicators` | `pd-indicators/` | `datamart.PDIndicator` (programme page) |
| `assessments`, `psea_assessments` | `partners/assessment/`, `psea/assessments/` | HACT and PSEA assessments (partner and assurance pages) |
| `engagements` | `audit/engagements/` | audits, special audits, spot checks, micro-assessments (assurance, partner and programme pages) |
| `action_points` | `actionpoints/` | action points (action points, partner and programme pages) |
| `tpm_visits`, `field_monitoring` | `tpm-visits/`, `fm-ontrack/` | TPM visits and field monitoring findings (field monitoring and partner pages) |
| `hact` | `hact/aggregate/` | HACT totals per year (assurance page) |
| `funds_reservation_headers` | `funds/fundsreservationheader/` | FRs: reserved, disbursed, outstanding (funds and programme pages) |
| `partner_reports` | `prp/datareport/` | partner progress reports per indicator and location, periods starting in the last `ETOOLS_DATAMART_REPORTING_YEARS` years (partner reporting, programme and partner pages) |
| `tpm_activities` | `tpm-activities/` | the PD, place and date of each TPM visit activity (field monitoring and programme pages) |
| `staff_visits` | `travel-activities/` | UNICEF staff trip activities: programmatic visits, spot checks, meetings (programme and partner pages) |
| `planned_visits` | `interventions-planned-visits/` | programmatic visits planned per PD and quarter |
| `hact_history` | `hact/history/` | each partner's HACT year: cash transfers, risk rating, visits, spot checks and audits done against required (assurance and partner pages) |
| `pd_activities` | `interventions-activities/` | PD workplan activities with UNICEF and partner cash (programme page) |
| `audit_results`, `audits`, `spot_checks`, `micro_assessments`, `special_audits` | `audit/results/`, `audit/audit/`, `audit/spot-check-findings/`, `audit/micro-assessment/`, `audit/special-audit/` | add risk rating, high-priority findings, key control weaknesses and amounts to the engagements, matched by reference number |
| `audit_findings` | `audit/financial-findings/` | financial findings of each engagement (engagement, assurance and partner pages) |

Pages: *Funds*, *Partner reporting* (each report opens with its indicators by location),
*Assurance* (with HACT compliance per partner and an engagement page with its findings and action
points), *Field monitoring* and *Action points*, plus sections on the programme, partner and donor
pages. The PRP endpoints other than `prp/datareport/` carry no country field, so they are not read.

Rows link to programme documents by eTools id or reference number and to partners by eTools id or
vendor number; `details.not_linked` of a run counts the records whose PD or partner is not in
NeuroDB. The partner and programme tables are updated, never emptied. Each `datamart` table is
replaced by a complete read (rows the Datamart no longer returns are deleted), except when the
Datamart returns nothing at all (`details.empty_response`), which usually means a wrong country name.
The admin shows these tables read-only under *eTools Datamart*.

### Data quality per dataset

`/data/health/` lists every Datamart dataset with its last run, records read and written, records
that failed, records whose partner, programme document or location is not in NeuroDB yet
(`not_linked`, per kind), rows removed because the Datamart no longer returns them, and the rows
NeuroDB holds. Read it before trusting a number on an eTools page; a dataset with many "not
linked" records or an "empty response" is the first thing to fix.

### Recording real records as test fixtures

The sync tests were written from the Swagger; production has shown shapes the Swagger did not.
`python manage.py record_datamart_samples [--limit 25] [--only a,b]` reads a few records of every
dataset from the live Datamart (same credentials as the sync), removes contact details and writes
them to `tests/fixtures/datamart/<dataset>.json`; commit the files. The test
`tests/integrations/test_recorded_samples.py` replays every recorded file through its sync and
fails when a record cannot be written, so a Datamart change is caught by the test suite rather than
by the nightly job. Re-record after a Datamart release.

### When a run says "Succeeded with errors"
Open the run: **Why rows failed** lists each error message with the number of records it hit and a
few of their ids (every record is also in the container log). Two causes to know:

* **New rows cannot be inserted into the v2 tables** (`duplicate key value violates unique
  constraint "etools_pca_pkey"` or a permission error), while existing rows update fine. A database
  restored without its sequence values leaves the `id` sequences behind the data; the sync now
  raises them to `max(id)` before writing (`details.sequences_aligned` shows when it did). If it
  reports an error instead, the application login lacks rights on the sequences: as the database
  owner run `GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO <app login>;` and
  `SELECT setval(pg_get_serial_sequence('etools_pca','id'), (SELECT max(id) FROM etools_pca));` for
  `etools_pca`, `etools_agreement`, `etools_partnerorganization` and `etools_pca_locations`.
* **A record's agreement or locations cannot be written**: the programme document is still saved
  and `details.not_linked` counts the agreement or locations left out.

### Partner monitoring (PD indicators by month and location)
*Partner monitoring* replaces the ActivityInfo indicator dashboards for partners reporting in
eTools/PRP. It joins the PD indicators (`pd_indicators`: target, baseline, section, output,
planned locations, disaggregations) with the partners' data reports (`partner_reports`: one row per
report, indicator and location) on the programme document and the indicator title. Quarterly (QPR)
and monthly humanitarian (HR) reports describe the same achievements, so the page shows one report
type at a time (QPR by default; HR exists only for high-frequency and cluster indicators) and never
adds them. Each report's value is the sum of its location rows (or their maximum / average when the
indicator's calculation method says so) and is shown in the month its reporting period ends; the
cumulative is the one the latest report carries. The status compares the cumulative's share of the
PD target with the share of the PD period (start to end) elapsed, ±10 points, the same rule the
ActivityInfo pages use with the calendar year. Gender, age group, nationality and disability tags
are read from the indicator titles (`neurodb/datamart/tags.py`). The programme and partner pages
summarise it; the assistant answers with `pd_indicator_progress`. Per-location targets are not in
any country-filterable Datamart endpoint, so locations are compared on reported values only.

**Statuses.** *On track / Off track / Over target* compare the cumulative achievement with the
share of the PD period elapsed (±10 points); *No target* when the PD sets none; **Not reported**
when no progress report of the selected type and year was read for the indicator — an indicator
nobody reported on is never called off track. A report row is matched to its PD indicator by the
eTools indicator id (`etools_cp_output_indicators_id` = the PD indicator's `source_id`) when the
Datamart gives one, else by its title within the programme document.

### One programme implementation ecosystem: how the eTools records are linked

eTools is the source of the relationships and of the identifiers; the sync keeps them instead of
matching names. Every `datamart` row keeps `datamart_id` (the Datamart record) and `source_id`
(the eTools record), so anything can be traced back.

| Record | Linked to | By |
|---|---|---|
| Programme document (`etools.PCA`) | partner, agreement | eTools ids (`partner_source_id`, `agreement_id`) |
| Programme document | implementation locations (`PCA.locations`, `location_p_codes`) | the P-codes of `locations_data`, resolved in the locations table |
| Programme document | funding: UNICEF cash, partner contribution, total budget, donors, grants, FRs | the interventions and budget datasets by eTools id; FRs by PD reference number |
| PD indicator (`PDIndicator`) | PD, location (`location`) | PD reference number; eTools location id, then P-code |
| Partner report row (`ReportedIndicator`) | partner, PD, location (`location_ref`), reporting period, status | vendor number, eTools PD id / reference number, P-code, the report's period and status fields |
| TPM activity | partner, PD, locations (`location_links`) | vendor number, PD reference number, P-codes of `locations_data` |
| Field monitoring finding | partner, location, site (`monitoring_site`) | vendor number, eTools location id / P-code, the site name inside that location (eTools gives the name only) |
| Action point | partner, PD, location, TPM activity, audit engagement | eTools ids (`partner_source_id`, `intervention_source_id`, `location_source_id` / P-code, `tpm_activity_source_id`, `engagement_source_id`) |
| Audit / spot check / micro-assessment | partner, active PDs | vendor number, `active_pd_data` ids and numbers |
| Staff trip activity | partner, PD, location | eTools ids and P-code |

The run's `details.not_linked` counts, per kind (`partner`, `programme_document`, `location`,
`site`, `tpm_activity`, `engagement`), the records whose target is not in NeuroDB yet; the
`locations` dataset runs first and `link_partners`-style re-runs resolve the rest the next night.

**Partner reporting map** (*eTools partner reporting → PD indicators → Map*): every implementation
location of the filtered indicators, coloured by the worst tracking status there. A location is
placed by its own eTools coordinates; when it has none, by the nearest parent area that has some
(drawn hollow, "approximate"); a name alone never places anything, and locations with no
coordinates anywhere in their hierarchy are listed under the map. Clicking a location lists, per
partner and programme document (with its funding and agreement), the indicators reported there:
target, achieved at that location this year, cumulative there and overall, status and latest
period, plus the TPM activities, field-monitoring findings and open action points recorded at the
place. The indicator, programme and partner pages link back to the map for their locations.

### Partner reporting: ActivityInfo and eTools, side by side

Partners reported in ActivityInfo until 2026 and report in eTools/PRP from then on. Both stay:
the ActivityInfo databases keep their dashboards, analytical views, maps and Neuro/HPM reports
(sidebar *ActivityInfo reporting*), the eTools reporting has its own pages (sidebar *eTools partner
reporting*: *PD indicators*, *Progress reports*), and the **partner page** brings the two together
under *Partner reporting*: the eTools implementation monitoring on one side, the ActivityInfo
history per year and database on the other (each database opens the partner's master indicators by
month; *map* shows its sites).

The bridge is the table *ActivityInfo partner links* (admin → Partnerships): one row per partner
name found in the activity records (`partner_label`, exactly as spelled there), pointing at the
eTools partner. It is refreshed by `manage link_partners`, which `migrate_locked` runs at every
start, the ActivityInfo import and the eTools sync run at their end (job `partner_links` in the
sync runs; a failure there is recorded on that run and does not stop the import), and by **Match
ActivityInfo partners now** on that admin page. A name is linked, in this order, by

1. a decision **taken by hand** in the admin: a partner, or the partner cleared to keep the name
   unlinked — both survive every refresh;
2. the **same name** as an eTools partner's name, short name or alternate name — case, accents,
   underscores, dashes and punctuation ignored, and only when a single partner carries the name;
3. the **programme document** number the records carry (`project_label`, the part before the
   amendment suffix): the partner of the PD under which most of the name's records fall. When two
   partners' PDs tie, the name stays unlinked and the run lists it under `ambiguous_examples`.

Partners deleted in eTools are never linked to. Names that no longer appear in the records lose
their automatic row (a hand-made row stays, with zero records). The run's `details` count each
method and list up to 20 names left unlinked; fix those by hand (the partner list shows, per
partner, whether it reports in eTools and how many ActivityInfo records are linked). `UNICEF` as a
partner name is ignored.

### Every other endpoint: kept whole for the AI assistant
All the other country-level endpoints are stored record by record in `datamart.DatamartDocument`
(admin: *eTools Datamart → Datamart records*), one `SyncRun` per dataset, each linked to its NeuroDB
partner and programme document where the record names one. The list, with a description of each, is
`neurodb/datamart/catalogue.py`: PD locations, ePD narratives, PRC reviews, management budgets,
country programmes, PMP figures, CP indicators, attachments, planned engagements, PSEA answers, FAM
status, FM questions, options and programme activities, staff trips, PRP indicator reports, PRP
programme documents and progress reports (with partner satisfaction), locations, sites, offices,
sections, eTools usage, the workspace and the Datamart ETL status. The raw records behind the
partner, programme document, budget, agreement and audit-detail tables are kept there too, so the
assistant sees every field.

The PRP views have no `country_name`: they are filtered by the country office's business area code,
found from `datamart/workspaces` (or set `ETOOLS_DATAMART_BUSINESS_AREA`). A record from another
country that an API filter let through is dropped (`details.other_country_skipped`). E-mail
addresses and phone numbers are removed from every stored record and every assistant answer.

Not read, and why:

| Endpoint | Reason |
|---|---|
| `datamart/users`, `datamart/partners/contacts`, `sources/prp/unicefperson`, the PRP focal point and officer tables | personal data |
| `rapidpro/*` | RapidPro messaging; contacts are personal data; no country filter |
| `datamart/reports/outcomes`, `outputs`, `activities`; the other `sources/prp/*` tables | global tables with no country filter |
| `prp/indicator-report` | superseded by `prp/indicator-report-v2` |
| `datamart/audit/financial-findings`, `audit/engagement-details`, `partners_hact_active` | subsets or summaries of endpoints that are read |

The assistant reaches all of it through four lookups: `etools_datasets` (what exists, with fields
and example values), `etools_query` (filter by partner, PD, text, field values and dates; group,
count and add up), `etools_search` (which datasets mention a name or reference) and
`etools_record` (one record in full). The programme and partner lookups also count the linked
records in every dataset.

The older eTools REST sync (`manage sync_etools`, token `ETOOLS_TOKEN`) is still available on
demand, for trips (`--only travels`) and the legacy engagement tables; locations still come from
`sync_locations`.

## Yearly rollover (January)
1. Admin → Reporting years: create the new year and tick *current* (only one can be current).
2. Admin → Databases: create one row per ActivityInfo folder with `ai_id`, `db_id`, `parent_id`,
   section and the new reporting year; tick *Show on dashboard*.
3. Select the databases → action *Import structure from ActivityInfo*.
4. Wire master indicators (Add sub-indicators wizard) and Neuro Reports (Add master indicators
   wizard), or copy last year's configuration in the admin.
5. Population figures: add the year's UNICEF file as
   `neurodb/core/data/population/Population_figures_YYYY_NeuroDB.json` (the v2 JSON layout) and deploy;
   the container loads any bundled year that is missing when it starts. To load or reload a file by
   hand: `manage.py load_population_figures <file.json> --year YYYY --replace`.
6. Upload the year's HPM PDF tables to the library.
7. Select the databases → action *Import data from ActivityInfo*; check `/data/health/`.

## Backups
Azure Database for PostgreSQL point-in-time restore (set retention to at least 14 days) covers the
database; Blob storage keeps deleted files for 30 days (soft delete). Rehearse one restore per quarter
into a staging resource group and record the date here.

## Health
- `GET /healthz/live/`: process is up (liveness and startup probes). No database access.
- `GET /healthz/`: database reachable, last successful run of each job and the running version
  (readiness probe; 503 when the database is unreachable). The pipeline's smoke test waits for the
  new version here.

Logs: `az containerapp logs show -n <prefix>-web -g <rg> --follow`, or Log Analytics
(`ContainerAppConsoleLogs_CL`). Requests, database calls, outgoing calls to ActivityInfo and eTools,
and exceptions are traced in Application Insights.
