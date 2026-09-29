# Operations runbook (v3)

## Deploy and rollback
The platform is Azure Container Apps: one image runs the website (`<prefix>-web`) and every job
(`<prefix>-migrate`, `-ai-structure`, `-ai-data`, `-etools`, `-locations`, `-freshness`, `-daily-review`). Setup and
architecture: `docs/DEPLOYMENT_AZURE.md`.

CI builds `<acr>/neurodb:<commit sha>` on `main` and runs `infra/scripts/deploy.sh` for staging and
then, after approval, production. The script runs migrations as a job first and stops if they fail.
It then rolls out a new web revision, which only takes traffic when `/healthz/` is ready, and
switches the jobs to the new image. In addition, every web container applies pending migrations
before it starts (`RUN_MIGRATIONS=true`, the default), under a database lock so only one container
migrates at a time. On App Service this is what migrates the database on each deployment.

Rollback: `infra/scripts/rollback.sh` with the previous image tag. Migrations are not reversed, so
every migration must stay compatible with the previous release for one deployment.

The release that adds the *PAL* population nationality repairs the population figures by itself:
the first container start reloads the years stored by the older loader (see Yearly rollover, step 5).
Nothing to run by hand.

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
Signed-in users ask questions in plain language on `/ask/` or from the search box (Ctrl K). The
search box's "Ask NeuroDB AI" link opens `/ask/?q=...` with the question filled in; nothing is sent
until the user presses Ask, so a crawler or a page reload never asks it.
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
The periodic jobs are managed in the admin: **Data and sync → Scheduled jobs**. Each row is one
command on one schedule, in **Beirut time** (summer time is followed automatically):

| Job | Runs | Default schedule (Beirut) |
|---|---|---|
| `locations` | `sync_locations` | `0 5 * * *`, daily 05:00 |
| `daily-review` | `daily_review` | `0 6 * * *`, daily 06:00, after the night's syncs |
| `activityinfo-data` | `import_activityinfo_data --current-year` | `0 18 1-22 * *`, 18:00 on days 1–22 |
| `etools-datamart` | `sync_etools_datamart` | `30 20 * * *`, daily 20:30 |
| `freshness` | `check_sync_freshness` | `15 * * * *`, hourly; a stale source is logged as an error |
| `activityinfo-structure` | `import_activityinfo_structure --all` | switched off; switch on or use *Run now* after the yearly rollover |
| (inside `activityinfo-data` and `etools-datamart`) | `link_partners` | at the end of both jobs |

On the page: switch a job on or off (the toggle, then *Save*), open it to change its schedule (five
cron fields: minute hour day-of-month month day-of-week; the form refuses a schedule it cannot read),
*Run now* from the row's **⋯** menu, or *Add scheduled job* to run another listed command on a
schedule (for example the eTools REST sync weekly). Only the commands in that list can be
scheduled. Each row shows its next run, the last run of its command (a link to the run) and the
last outcome ("started", or "skipped: the previous run is still going"). Every change records who
made it (*Updated by*, and the History button).

**How it runs.** The scheduler runs inside the website: each gunicorn worker starts it in a thread
and a database lock lets exactly one of them act, so a job starts once however many workers or
replicas run. The banner above the list says whether it checked in during the last three minutes,
and on which host. A due job starts within a minute, in the background, as the admin buttons do,
and appears in *Import and sync runs* with *schedule* as its trigger. A job whose previous run is
still going is skipped until its next time. Times missed while the site was down (a deployment, a
restart) are caught up once when it comes back. It works the same on App Service and Container
Apps; on **App Service, turn on *Always On*** (Configuration → General settings), otherwise the site
sleeps when nobody uses it and the scheduler with it. `SCHEDULER_ENABLED=false` switches the
scheduler off (the page then says so); `python manage.py run_scheduler [--once]` runs it in the
foreground (local development).

The Container Apps jobs in `infra/main.bicep` have no schedule any more (they stay for manual runs:
`az containerapp job start -n <prefix>-<job> -g <resource group>`), so nothing runs twice. Every run
writes a `SyncRun` row (admin → *Import and sync runs*; page `/data/health/`).
Triage a failure: open the run, read `error`, re-run the job from the admin, or the command with
`--database <ai_id>` or `--only <entity>`. A `PARTIAL` run lists the failed item ids in `details`.

## Running jobs without a command line

Everything an operator runs with `manage.py` has a button in the admin, for administrators, each
with a confirmation step. Admin → *Data and sync* → *Import and sync runs* (also linked from the
admin home page, *Quick actions*):

| Button | Command it runs | How |
|---|---|---|
| **Sync eTools now** | `sync_etools_datamart` (core, all or chosen datasets) | background |
| *Scheduled jobs* page → row **⋯** → *Run now* | that job's command | background |
| Run a job → *eTools REST sync* | `sync_etools` | background |
| Run a job → *Locations sync* | `sync_locations` | background |
| Run a job → *ActivityInfo structure* (current year) | `import_activityinfo_structure --all` | background |
| Run a job → *ActivityInfo data* (current year) | `import_activityinfo_data --current-year` | background |
| Run a job → *Link partners* | `link_partners` | background |
| Run a job → *Daily review* | `daily_review` | background |
| Run a job → *Population figures* | `load_population_figures --bundled --replace` | in the request (seconds) |
| Run a job → *Check freshness* | `check_sync_freshness` | in the request; changes nothing |
| Run a job → *Repair roles* | `bootstrap_roles` | in the request |

One database at a time: *Reporting setup* → *Databases*, select them, action *Import structure* or
*Import data from ActivityInfo* (one background process per database). The daily review and the
population figures also have their button on their own admin pages.

A background job runs as its own process (it survives the web worker that started it), is recorded
as a run in this list like a scheduled run, and does not start while a run of the same job is in
progress. Not available as buttons, on purpose: `migrate_locked` and `ensure_legacy_tables` (every
deployment runs them), `seed_demo` (local databases only) and `record_datamart_samples` (writes test
fixtures into the source code).

## eTools Datamart

**Running the sync now.** Admin → *Data and sync* → *Import and sync runs* → **Sync eTools now**
(administrators): choose *Partners and programme documents* (minutes), *Everything* (up to two
hours) or *Only the datasets ticked below* (for example `locations` alone after a gazetteer fix).
It runs in the background in the web container; each dataset appears in that list as it
finishes. Only one Datamart sync runs at a time (a database lock), whoever starts it. A deployment
restarts the container and kills a sync in progress: its run stays *Running* until the next
**Sync eTools now**, which sees that nobody holds the lock, closes the run as *Failed* ("Cut off")
and starts. The scheduled job `etools-datamart` (admin → Scheduled jobs) runs it every night.
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
cumulative is the one the latest report carries (PRP repeats it on each location row; if the rows
disagree, the largest is used, and two reports ending the same day are ordered by report id, so an
indicator reads the same under every filter). The status compares the cumulative's share of the
PD target with the share of the PD period (start to end) elapsed, ±10 points, the same rule the
ActivityInfo pages use with the calendar year. Gender, age group, nationality and disability tags
are read from the indicator titles (`neurodb/datamart/tags.py`). The programme and partner pages
summarise it; the assistant answers with `pd_indicator_progress`. Per-location targets are not in
any country-filterable Datamart endpoint, so locations are compared on reported values only.

**Statuses.** *On track / Off track / Ahead of schedule* compare the cumulative achievement with
the share of the PD period elapsed (±10 points); an indicator ahead of schedule that has passed 100 %
of its target reads *Over target* (the stored key stays `over_target`); *No target* when the PD sets none; **Not reported**
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

**Partner monitoring map** (*Partner progress (eTools) → Partner monitoring → Map*): every implementation
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
(sidebar *Monthly results (ActivityInfo)*), the eTools reporting has its own pages (sidebar *Partner
progress (eTools)*: *Partner monitoring*, *Progress reports*), and the **partner page** brings the two together
under *Partner reporting*: the eTools implementation monitoring on one side, the ActivityInfo
history per year and database on the other (each database opens the partner's master indicators by
month; *map* shows its sites).

The sidebar's *Databases*, *Neuro reports* and *HPM* blocks list the year the page shows: the year
chosen in the year menu (`?year=`), the year of the database or report that is open, or else the
current reporting year. When that year has none of them (a new year before its databases are
set up), each block shows the latest year that has some, and says which year in its title.
Search covers the same year. A Neuro Report or HPM page has an *Other years* menu listing the
reports with the same report code in other years (set the code in admin → Neuro reports).

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
`sync_locations`. Its intervention details step writes the donor amounts in the same shape as the
Datamart sync (one entry per donor and grant), so running it does not blank the donor pages.

## Country overview (the signed-in home page)

`/` for a signed-in user is the country overview: one page for the whole intervention, read live
from the synced eTools tables and the ActivityInfo history. Three rows answer three questions, then
two progress blocks and the daily AI review. The ActivityInfo database cards of the year (one per
database: indicators, status, records, partners, last import) have their own page, `/databases/`,
in the sidebar as *ActivityInfo databases* (under *Monthly results (ActivityInfo)*, shown even in a year
without databases), with the year's totals and the import runs:

| Row | Blocks | Source |
|---|---|---|
| Impact on children | Children reached (eTools PRP reports and ActivityInfo, shown apart), children reached by governorate (tiles), achievement against target by section, monthly reach of both sources, coverage of the estimated children per governorate | PD indicators + PRP reports (`datamart_pdindicator`, `datamart_reportedindicator`), ActivityInfo facts of the HPM masters, population figures (`category=children`, governorate level) |
| Value for money | Funds disbursed of reserved, cost per child reached by section, spending vs delivery by section, funding by donor, partnerships that need a decision | Funds reservations (`datamart_fundsreservationheader`, lines for donors), the same indicators |
| Delivery and assurance | Indicator status by section, assurance counts (field monitoring, TPM visits of every status as on the field monitoring page, with the planned ones beside them, action points, HACT risk), findings by rating, what needs attention (at most eight items; overdue high-priority action points and one never-reported item always among them, "+N more" for the rest) | Partner monitoring rule, `datamart_monitoringfinding`, `datamart_tpmvisit`, `datamart_actionpoint`, partner risk ratings |
| Progress | TPM visits planned, completed and with the report overdue per month; action points due, closed and past due per month, open ones by age | `datamart_tpmvisit` (+ activities for the section), `datamart_actionpoint` |

**Filters.** Year (the reporting year menu), section (multi) and governorate (a click on a tile).
A PD manager lands on their own section (the user's section in the admin, matched to the eTools
section names as on the partner monitoring page); "every section" is one click away.

**Which programme documents.** Every PD running in the selected year (its period overlaps the year),
whatever its status now, except drafts and cancelled ones: a PD that ended in June still counts for
its months. Only *Decisions this quarter* is limited to the PDs still active.

**Which indicators count children.** A PD indicator counts when it is a plain number and its title
names children (the age-group tag: under 5, under 18, adolescents, children). An ActivityInfo master
indicator counts when it is an additive (SUM) master of the year's HPM report, in a database shown
on the dashboard, and its label names children. Sections correct the rule once in admin → Datamart →
*Children indicator flags* (eTools indicator id or master indicator id, counts / does not count);
the flag survives every sync and shows on the page at once. A flag on an ActivityInfo master only
acts on masters that meet the other conditions (additive, HPM report, displayed database).

**Two sources, never added.** The same children are often reported in both eTools PRP and
ActivityInfo, so the headline *Children reached, at least* is the larger of the two, with both shown
beside it; the governorate tiles and the coverage table take the larger of the two per governorate.

**Governorates.** A governorate's eTools figure adds the values reported at the locations inside
it, for indicators whose locations add up (PRP "sum"); an indicator reported as the maximum or the
average across locations is counted nationally but not split by governorate. With a governorate
chosen, the monthly bars, the money and the cost per child follow the same rule: a PD's funds count
for its share of the children it reached there, and PDs without a children result are left out.

**Cost per child** is what the PDs with a children result disbursed to date (all the years of their
funds reservations, supplies and operating costs included) over the children they reached in the
selected year. A PD's money is split across sections in proportion to the children of each section.
Compare sections, not absolute values. It is empty when no children indicator reported.

**Caching.** The page's data is cached for 2 minutes per (year, sections, governorate, day), in each
web worker's memory; a new children flag and the end of any sync run change the cache key, so every
worker shows the new figures at once. The freshness strip at the bottom shows when each source last
changed.

## Management brief (`/brief/`)

The overview read for decisions: one page for senior management and the Country Management Team,
in the sidebar under *Management brief*. It reuses the overview service (same programme documents,
same children rule, same cache) for the selected year **and for the year before**, and adds what a
management meeting asks for. Eight blocks, one tab each:

| Block | What it shows | Source |
|---|---|---|
| Brief | Five tiles with a comparison (children reached against the same months of last year, achievement against the elapsed period, disbursed of reserved, cost per child against last year's PDs, indicators on track against the review 30 days ago), *Things to decide* (up to five decisions chosen and written by the AI daily review from its findings, each with one reason, a suggested owner and when, and the findings it rests on (marked *AI-suggested*; the model's name stays on the stored review only); without the assistant, the review's five most important open findings), and *Sections at a glance*: one row per section, weakest first, with five status cells (pace: target reached against time elapsed; indicators on track; spending: disbursed against achieved; reporting; confidence), each with an icon, its value and, on hover, its figures (the rules are under the table). *Copy summary* copies the headline figures and decisions as plain text for minutes | The overview blocks of both years, the latest daily review, the finding assignments |
| Ahead or behind | Achievement against the expected point and the Country Programme target per section; the cumulative reach of both sources projected to December at the last three months' pace | PD indicators and PRP reports, ActivityInfo HPM masters, **section plans** |
| How sure | Confidence per section (indicators reported, PDs verified by a TPM activity or a field monitoring finding, partners linked across sources, days since the eTools sync; High / Medium / Low, with what keeps it from High next to the badge, e.g. "because partners are not linked to ActivityInfo"; Low whenever a value was set aside, see below), the two sources per partner side by side with the gap, quarterly reporting timeliness per partner | PRP reports, TPM activities, monitoring findings, ActivityInfo partner links, sync runs |
| Who and where | Children by sex and age band, by nationality and disability against the population share (from the indicators' titles), coverage per governorate, districts with high need and low coverage | Indicator title tags, population figures (governorate and district level), eTools locations |
| Partners | Scorecard (PDs, reserved, on track, reports on time, action points on time, HACT risk, latest finding, disbursed against achieved), delivery-against-spending bubbles, partnerships that need a decision | The same tables per partner |
| Money | Donor → section → children reached, grants at risk (unspent balance by expiry, red within 90 days), funded against required per section | Funds reservation lines and headers, grants, **section plans** |
| Action | Finding lifecycle over 30 days (raised, acknowledged, assigned, closed, median days per step), open findings by owner, weekly digest of the last seven reviews | Daily reviews, **finding assignments** |
| Reference | Lineage (the runs behind the figures, the population year, the rules version, the latest review) and a data dictionary | Sync runs |

**Three tables are entered by hand**, because eTools does not hold them:

* admin → Reports → *Section plans*: per year and section, the Country Programme target for
  children reached and the funds required (the appeal figure). Without them the bullet chart shows
  no CP mark and the funded-against-required chart is empty; the page says so.
* admin → Daily review → *Finding assignments*: who owns a finding (a role or a team, never a
  person's name), by when, and its status (acknowledged, assigned, closed). Reached from the
  *Assign* link next to a finding (on the brief and in the review admin); the status stamps the
  dates the lifecycle chart reads. A finding the review itself sees resolved counts as closed.

**Rules of this page.** Every comparison is against the same rule: the same months of the year
before for children reached (month to date, the larger of the two sources), the elapsed PD period
for achievement, the daily review of 30 days ago for the on-track share (the same formula and the
PDs running in the year, only when that review is of the selected year). The projection is the
average of the last three reported months, extended to December; it is a pace, not a forecast.
Confidence thresholds: reported ≥ 80 %, verified ≥ 30 %, linked ≥ 90 %, sync at most 2 days old;
one short = medium, two or more = low. A grant's unspent balance is each funds reservation's
outstanding amount in the grant's share of the reservation's lines (eTools carries the
outstanding amount per reservation, not per grant). Sex, age, nationality and disability come from
the indicators' titles: a title naming both girls and boys names neither, so most children fall
under "not named" until PRP disaggregations carry labels.

**Values that cannot be right are left out.** A children indicator whose reported value is above
the child population of Lebanon (the latest population figures), or more than 20 times its target
(and above 1,000), is not added to children reached or to achievement: its section's confidence
is Low, the *How sure* block lists it with a link, and the brief as text says how many were left
out. Check such an indicator in eTools with the partner. PRP values that eTools sends as
`{"v": .., "d": .., "c": ..}` are read as their calculated value (else numerator over
denominator); a value holding several numbers (`45/100`, a date) is read as no value, never as
the digits glued together. Section names are trimmed ("PSEA " is "PSEA").

**Filters and caching.** Year and section (every section by default). The result is cached for
2 minutes per (year, sections, day), and the key changes with every children flag, section plan,
assignment, new review or finished sync run, so an entry in the admin or a sync shows at once. *Print* prints the page without
the navigation.

## Daily AI review

Every morning the `daily-review` job (`manage daily_review`) runs fourteen fixed checks over the
synced data and stores the result as a dated review with its findings (admin → Daily review). The
overview shows the latest one in its *Daily review* card, with tabs for the day before and the last
seven days; the card follows the page's section filter (country-wide findings always show). The
programme findings are listed (the first twelve, the rest under *Show more*); the findings about the
data pipeline (`sync_failures`, `data_quality`, `stale_sources`, `locations_unplaced`) are summed up
in one *Data problems* line that links to the Data health page.

| Check | What it looks for | Severity |
|---|---|---|
| `new_off_track` | PD indicators off track today that were not yesterday (all of them on the first run), per PD | critical when children are behind target |
| `not_reported` | Indicators with no progress report although the PD started more than 120 days ago | warning |
| `reports_overdue` | Progress reports past their due date and not submitted | warning |
| `spending_ahead` | PDs whose disbursed share is more than 25 points ahead of their achieved share (each indicator capped at 100 %), past half their period | warning |
| `under_disbursed` | Active PDs past 60 % of their period with less than 40 % disbursed | warning |
| `pd_ending_soon` | Active PDs ending within 60 days below 70 % of target on average, or with nothing reported | warning |
| `tpm_reports_late` | TPM visits ended more than 14 days ago whose report has not come in | warning |
| `action_points_overdue` | Open high-priority action points past their due date, per partner and section | critical |
| `findings_off_track` | Field monitoring findings rated off track in the last 30 days, per partner | warning |
| `sync_failures` | Failed or partial sync runs in the last 24 hours | warning |
| `data_quality` | Datasets whose last run failed rows; ActivityInfo partner names not linked to eTools | info |
| `locations_unplaced` | PD indicators whose location is not in the gazetteer | info |
| `stale_sources` | Sources older than `SYNC_STALENESS_HOURS` (the ActivityInfo import is not reported after the 22nd, when it does not run) | info |
| `improvements` | Indicators back on track since the previous review | good |

**New, still open, resolved.** Each finding has a stable key (for example the PD and the check);
the next review marks it *still open* when it appears again, and adds a *resolved* entry for every
key that disappeared. When a check fails, its findings of the day before are carried forward as
still open, never resolved. The two checks that describe a change (`new_off_track`, `improvements`)
are always *new*. Findings are ranked by severity, then by the children behind target.

**The summary.** With the AI assistant configured (`OPENAI_API_KEY`), the model reads only the
day's findings and counts (titles, details and numbers; no staff names, `store=false`) and writes four to six plain sentences;
tokens are recorded on the review. Without it, or when the call fails, a template writes the
summary. Findings are prompts to look, never verdicts on partners or staff.

**The decisions.** In a second call the model reads the same facts (the open critical and warning
findings, at most 40, each with its key) and chooses at most five decisions for management, most
important first: what to decide (an action, not a restatement), why (facts from its findings), who
(a role: a section chief, the PD's programme manager, PM&E, Partnerships or Operations; never a
name), when (today, this week, this month) and the findings it rests on. The answer must follow a
fixed JSON schema, and NeuroDB keeps a decision only when every finding it cites exists in the
review and every number it writes appears in those findings (small counts up to 10 excepted);
anything else is dropped and logged. They are stored on the review (*Decisions*, *Decided by*) and
shown in the management brief's *Things to decide* box with the findings they rest on. Without the
assistant, with nothing to decide, or when the call fails, *Decided by* reads *rules* and the box
shows the review's five most important open findings instead (critical first, then the most
children behind target, then new before still open), and says so. Findings assigned in the admin
(*Finding assignments*) show their owner, due date and status on the decision that cites them first.

**Run it now.** Admin → Daily review → *Run the daily review now* (administrators, after a
confirmation), or `python manage.py daily_review [--date YYYY-MM-DD] [--no-narration]`. One review
runs at a time (a database lock); a second start says so and does nothing. Re-running a date
replaces its review only when the new run succeeds. `--date` only names the review: the checks always
read today's data, so use it to re-run today's or yesterday's review, never to rebuild the past. Every run writes a `SyncRun` (job *Daily AI review*); a check that fails is listed on
the review and the run is *partial*, the other checks still report. The scheduled job
`daily-review` (admin → Scheduled jobs) runs it every morning.

## Donor access (`/donor/`)

A donor can be given a sign-in that sees **one page and nothing else**: no menu, no other page, no
internal API, no assistant. The page has two tabs.

- **Your contribution**: the donor's funds and what they paid for, filtered in the browser by grant,
  programme area and governorate: committed and disbursed (tiles, by area, by grant with its end
  date), a grant → area → partner flow, a schematic governorate map, children reached against
  targets, girls and boys, cost per child against the country average of the same area, and one row
  per programme document.
- **UNICEF Lebanon overall**: the whole country for the year as aggregates only: children reached
  (by month, programme area, governorate), results on track, programme and partner counts, field
  visits. No programme, partner or donor is named and no amount of money is shown.

**How the donor's figures are counted.** The donor's money is the funds reservation lines that name
one of the account's donors (and grants, when the account lists some) on FRs running in the year.
Disbursed is the FR's disbursed amount times the donor's share of that FR's lines. A programme's
children (the overview's children rule) are attributed in proportion to the donor's share of the
programme's funds. The page explains this to the donor under "How to read these figures".

**Creating an account** (admin → Users and access → Donor accounts → Add). The sign-in is either:
- **an existing user** ("Existing user"): a user already created in Users, not staff, not an
  administrator or section editor, without a donor account. Linking removes its roles and keeps its
  password; tick "Issue a temporary password" to replace it with one shown once; or
- **a new sign-in** (the donor's email, which must not belong to a NeuroDB user): created with a
  **temporary password shown once**.

Send a temporary password to the donor separately from the sign-in address; the donor must choose
their own at the next sign-in. Then: the page title, the donor names as eTools writes them on funds
reservations (chosen from a list; type a name in "Other donor names" for a donor with no funds
synced yet), optionally only some grant numbers, whether partner names show ("Partner 1 (civil
society)" otherwise), the UNICEF contact shown on the page, and an optional end date.

**Rules enforced for every donor request** (`neurodb/donors/middleware.py`):
- signing in lands on `/donor/`, whatever `next` says; any other page, the admin and unknown
  addresses redirect there; the internal API, the assistant and HTMX calls get 403;
- until the temporary password is changed, every page leads to the password change page;
- a switched-off account or one past its end date is signed out at its next request;
- the page never reads a donor or grant from the address: only the year can be chosen.

**Staff**: "Open the donor's page" in the account list previews exactly what the donor sees
(administrators only; the page does not exist for other staff). "New temporary password" on the
account (or the list action) replaces the password and asks for a new one at the next sign-in;
"Switch off the selected accounts" stops a sign-in at once. Deleting an account deletes a sign-in it
created, and switches off (does not delete) a user it linked: without its donor account that user
would otherwise sign in with a viewer's access.
`seed_demo` creates `demo-donor`, a USAID account, on local databases.

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
   hand: `manage.py load_population_figures <file.json> --year YYYY --replace`. Without server access:
   admin → Population figures → *Reload population figures* (administrators) reloads every bundled
   year from its file. A reload replaces the total and children figures of the year; vulnerable
   population figures entered in the admin are kept. Each load is a *Population figures load* run
   on Data health.
   The start-up load also reloads, once, any year stored by the loader before the Palestinian
   sheet's age bands, sex and children figures became *PAL* (PRL + PRS together) instead of *PRL*
   (such a year has PRL age bands and no PAL row), so that fix needs no manual step.
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
