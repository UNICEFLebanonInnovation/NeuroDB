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
Key Vault secrets: `django-secret-key`, `database-url`, `activityinfo-username` and
`activityinfo-password` (the ActivityInfo account's email and password; or `activityinfo-token` with
`activityInfoAuth = 'token'`), `etools-token`, `etools-username` and `etools-password` (the eTools
Datamart service account), (with SSO)
`entra-client-secret`, (with the AI assistant) `openai-api-key` and (with Compiler youth figures)
`compiler-api-token`. To rotate, set a
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
  off and the search box works as before; `/ask/` then points users to Search and the Management
  brief, and only staff see which setting is missing. Never put the key in a committed file or a Bicep
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
  is sent too. It is organisational data. The summary lookups send field visits as counts. The
  lookups that return eTools records as eTools holds them (`etools_query`, `etools_record`,
  `etools_datasets` and the examples of `etools_search`) treat the field monitoring datasets
  (findings, questions and answers, answer options, programme activities) apart: the keys that hold
  a person (visit lead, team members, monitors, focal points, assignees, user names, contacts,
  comments, any key named like a person, an e-mail or a phone, also inside a nested value) are
  dropped and cannot be filtered, grouped, summed, sorted or picked; texts longer than 80 characters
  (narratives, answers, summaries) are withheld ("text withheld: read it in Monitoring insights");
  shorter values (ratings, statuses, references, place names, "Yes") are sent without e-mail
  addresses, phone numbers, links, the person names NeuroDB knows and any name written after a title
  such as Mrs, Dr or Sheikh (a place named that way, such as Sheikh Zennad, is withheld too); a
  question that names a person finds no field monitoring record, and the search examples show the
  visit reference only. For every other dataset, keys naming an e-mail address, a
  phone or a mobile are removed, as before, and names written in other fields (action point
  assignees, TPM report authors, travellers) are sent. Partner staff contact lists are not synced at
  all. NeuroDB Watch's look-ups never use those four (below). Only signed-in users can ask, and the
  lookups can only read what any signed-in user can already see. Nothing is written back. Requests are sent with
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
  lookups made, tokens used and time taken. Every AI feature on the key (Ask NeuroDB, the daily
  review, What's new, document summaries, periodic figures, country programme reading, NeuroDB
  Watch) is also counted per day in one ledger, Admin → Data and sync → **AI use** (tokens; US
  dollars when `AI_PRICE_INPUT_PER_MTOK`, `AI_PRICE_CACHED_PER_MTOK` and `AI_PRICE_OUTPUT_PER_MTOK`
  are set). The background features stop using AI first when all of them together pass 80% of
  `AI_DAILY_TOKEN_SOFT_CAP` (default 3,000,000 tokens a day), so questions keep being answered. Output tokens include the model's reasoning tokens,
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
- **Background look-ups (NeuroDB Watch)**: every morning NeuroDB Watch runs this same loop on its
  own, with no person asking, to look into at most 3 open critical points (see NeuroDB Watch, *The
  daily look-ups*). It offers only 13 read-only tools of the 40 (`find_anything`, `entity_profile`,
  `connected`, `programme_details`, `partner_details`, `partner_reporting`, `pd_indicator_progress`,
  `funds_overview`, `assurance_overview`, `indicator_forecasts`, `whats_new`, `daily_review`,
  `data_freshness`), passes every result and every tool error through an allow-list (texts only
  under known fields; no people's names, free text, links, Makani centres or the daily review's data
  checks) before the model reads it, runs each tool in a read-only database transaction,
  stops after 4 rounds or 150 seconds and keeps an answer only when its figures are in what was
  looked up. It uses its own prompt cache key and sends no `safety_identifier`; its tokens count in
  the watch's own daily cap, never in anyone's hourly limit. A question asked on `/ask/` is sent
  exactly as before: same instructions, all tools, same cache key.
- **Azure OpenAI is a different service**: Microsoft's Azure OpenAI hosts OpenAI models inside an
  Azure subscription, with its own endpoint (`https://<resource>.openai.azure.com`), its own keys
  or Entra ID sign-in, and deployment names instead of model ids. A platform.openai.com key does
  not work there. Moving the assistant to Azure OpenAI (for example to keep the traffic in the
  Azure tenant) needs a small code change (the SDK's `AzureOpenAI` client) and different settings.

### Knowledge base (`/knowledge/`)

People give Ask NeuroDB documents and texts to answer from: reports, evaluations, meeting minutes,
guidance, notes. Administrators and section editors add them (**Add a document or text** on Ask
NeuroDB or on the Knowledge base page): a file (PDF with a text layer, Word .docx, PowerPoint .pptx,
Excel .xlsx, CSV, Markdown or text; 50 MB at most) or pasted text, with a title and optionally its
source, section and year. Every signed-in user reads and searches them; donor accounts cannot. A
document is read again or removed by the person who added it or an administrator; links are added
or removed by hand in admin → Library and maps → Knowledge documents.

Once added, `manage.py index_knowledge --document <id>` runs in the background (seconds to a few
minutes; the page refreshes until it is ready; several files added at once are read by one
`index_knowledge --pending`):

1. **Text**: read from the file (pypdf for PDFs, page by page; a scanned PDF without text is
   refused with a message, as there is no OCR) and stored in the database; the file itself is kept
   in the media storage (Azure Blob in production, under `knowledge/`).
2. **Index**: the text is cut into passages of about 1,500 characters (with their page) and indexed
   with PostgreSQL full-text search (English stemming, plus the words as written for Arabic, place
   names and reference numbers; the title counts most).
3. **Links**: the partners (full, short or alternate name, vendor number), programme documents
   (reference number, with or without the amendment suffix), sections, governorates and districts
   named in the text are linked. Matching is literal and whole-word; "North" and "South" count
   only as "North Lebanon" / "South governorate". The documents then show on the partner's and the
   programme document's pages, and `partner_details` / `programme_details` give them to the AI.
4. **Summary** (when the AI assistant is configured): the first 120,000 characters go to the OpenAI
   API (`store=false`) for a short summary, key points, the document's date and the organisations,
   places and reference numbers it names; those names become *AI-suggested* links when they match a
   NeuroDB record. If this step fails, the document is still searchable and says so.

The assistant has two tools for it: `search_knowledge` (best passages for some words, optionally
only in the documents linked to a partner, programme document, section or year) and
`read_knowledge` (a document's summary, links and text in parts of 8,000 characters). It quotes
and links the documents it uses. Their text is sent to OpenAI as part of the answer; the
instructions tell the model that it is material, never instructions to follow, and the AI cannot
change or add documents. Do not add personal data of children, beneficiaries or staff.

After new partners or programme documents arrive, `manage.py index_knowledge --all` links the
existing documents to them (it also writes new summaries, at the API's cost).

**Library publications and country programme documents** are read into the knowledge base too, so
their full text is searched, quoted and linked like an added document. Each published library item
(its file, or its title and summary when it has none) and each uploaded CPD document becomes a
knowledge document whose source reads *Library publication* or the country programme's name, with
*Open the original*. Saving or deleting one starts `manage.py index_documents --origin library|cpd --id <id>`
in the background (`KNOWLEDGE_INDEX_ON_SAVE=false` turns that off); the morning knowledge hub job
runs `index_documents` for all of them and reads only the new or changed ones (file name, file size
or summary changed), and removes the ones unpublished or deleted. They are changed or removed at
their source (the library or the country programme page), not on the knowledge base: only an
administrator can read one again. The first run reads every publication, and with the assistant
configured writes a summary of each, at the API's cost (once; later runs only read what changed).

### Periodic reports (`/knowledge/reports/`): figures kept by date

Some reports are issued again and again under the same name with a number and a date, e.g. the
*ESCALATION OF HOSTILITIES - LEBANON 2026 - UNICEF SNAPSHOT - 02 October-2026 - NUM-37*. Each
edition is a knowledge base document like any other (searched, quoted), and its figures are also
kept as data, so Ask NeuroDB can give the counts of a period, compare before and after, work out
differences, follow trends and draw charts.

- **Adding editions**: Knowledge base → *Add a document or text*, choose the files (several at once,
  e.g. editions 22 to 37) and tick **Periodic report**. Each file becomes a document named after it.
  A later edition of a report already here is recognised without the box ticked. Several files are
  read one after the other by one background process (`manage.py index_knowledge --pending`), oldest
  edition first, a few minutes each when the AI is configured.
- **Recognising an edition**: the series is the name without its number and date; the number comes
  from `NUM-37`, `#37`, `No. 37`, the date from `02 October-2026`, `2026-10-02`, `02.10.2026` or
  `October 2, 2026`, else from the text (`# 37 - Issued 02 October 2026`). The series can be renamed
  in admin → Periodic reports.
- **Charts of values over dates** are read from where their labels sit on the PDF page, without AI:
  each value is paired with the date under it, the axis ticks are left out, labels drawn twice
  count once, a cut date ("08…") takes the month that keeps the dates in order (or the one a full
  label of the same day gives elsewhere in the edition; when still ambiguous the value is left out
  and the edition says so). A value drawn a little left of its date still counts: the axis ticks are
  recognised as the column of numbers left of the first date. The page heading gives the group,
  its source (e.g. IOM) and whether it is *internal use*. Charts saved as pictures (e.g. page 2 of
  the older editions such as #23) cannot be read: the edition names those pages. Their history is
  usually in the later editions' charts, which repeat every date since the start.
- **Other figures** (headline counts and their breakdowns, indicators with achievement and target)
  are listed by the AI (OpenAI API, `store=false`) from the pages laid out as on paper. A figure is
  kept only when its number is printed on the page it is said to come from; the others are counted
  in the edition's note. Earlier editions' measures are given to the AI so that the same measure
  keeps the same name. Without the AI configured only the charts' figures are kept.
- **Over time**: the figures of all editions are lined up by the date they are about. When several
  editions give a value for the same date, the newest edition's counts (the snapshots correct and
  re-date earlier points: e.g. #31 gives open shelters on 16 and 25 June that #36 and #37 revise). The report's page shows
  every measure (latest value, the value before, the change, target), a chart over time and the
  editions with what was read from each. Figures can be checked or corrected in admin → Report
  figures (each keeps its page and the words around it).
- **Ask NeuroDB**: `periodic_reports` (reports, editions, measures) and `report_figures` (values
  over time with the changes between dates, or what one edition said). `make_chart` draws a line,
  column, bar or pie chart under the answer; it only accepts numbers the answer's lookups returned,
  so a chart cannot show a figure the model made up. Figures marked internal use are flagged to the
  model, which says so.
- Search ranks the newest edition first when several say the same thing, and gives the assistant
  each passage's date and edition.

### Knowledge hub: everything linked, for questions across sources

So that a question can combine sources ("which donors fund the partners working in Akkar, and what
do the evaluations say about them?"), NeuroDB keeps one index of everything it holds, linked:

| Things | From | Linked to |
|---|---|---|
| Partners | eTools (names, short and alternate names, vendor numbers, ActivityInfo partner labels) | their programme documents, ActivityInfo databases they report in, Makani centres, documents, findings |
| Programme documents | eTools | partner, sections, donors, grants, governorates and districts (from the PD locations), country programme outputs |
| Donors and grants | eTools PDs and Datamart grants | programme documents, grants |
| Sections, governorates, districts | NeuroDB and the eTools gazetteer (same names merge) | everything placed in them |
| ActivityInfo databases, master indicators, Neuro/HPM reports | ActivityInfo | sections, partners, reports |
| Country programme cycles, outcomes, outputs, indicators | Country programme | PDs (eTools CP outputs and confirmed links), master indicators, youth and education figures |
| Youth indicators, Makani and Dirasa, Makani centres | Compiler | PDs, partners, governorates |
| Documents and maps | Knowledge base, library, country programme | the partners, PDs, sections and places they name |
| Open daily review findings | Daily review | the PD or partner they are about, section |

The hub holds **names and links** (plus a few headline figures, kept only to notice what changed): every figure the assistant gives is read live. Each thing carries a *lookup*,
the assistant tool and arguments that give its current figures (e.g. `partner_details`,
`database_results`, `cpd_indicator`, `makani_wellbeing`). The assistant uses it in three steps:
`find_anything` (things matching a name, code or number, with the best document passages),
`entity_profile` (everything linked to one thing, grouped by how) and `connected` (things of one
kind up to two links away, with what connects them), then the lookups, combined in one answer with
each figure's source. Other tools added with it: `country_programme`, `cpd_indicator`,
`youth_figures`, `education_figures`, `makani_wellbeing`, `daily_review` and `management_brief`.

**Child data is not reachable.** For Makani wellbeing the assistant sees centre and partner totals
(`CenterSummary`); flags of individual children (registration numbers) are neither in the hub nor in
any tool.

**Rebuilding.** The hub is rebuilt **whenever new data arrives** and every morning:

- After every sync or import that succeeds (any `SyncRun`: ActivityInfo, eTools, Datamart, Compiler,
  locations, the daily review…) and every document read into the knowledge base, a rebuild is asked
  for. A background process (`build_knowledge_hub --when-requested`) waits
  `KNOWLEDGE_HUB_SETTLE_SECONDS` (60) so that a burst of syncs makes one build, then rebuilds without
  re-reading documents; its run says which syncs asked (*new data: eTools Datamart sync, …*). One build
  runs at a time (a PostgreSQL advisory lock): a request made during a build is answered by the next.
  `KNOWLEDGE_HUB_ON_NEW_DATA=false` turns this off.
- The scheduled job `knowledge-hub` (daily 07:00 Beirut) also reads new library and CPD documents first.

A source that fails is listed in the run's details and the run is *Succeeded with errors*: what it
added last time, and the links to it, are **kept as they were** (a source that could not be read is not
"gone"), the others are rebuilt. *Run now* on the Scheduled jobs page or Run a job → *Knowledge hub* in
the admin; `manage.py build_knowledge_hub [--no-documents]` from a shell. Browse it in admin → Library
and maps → *Knowledge hub entities* and *Knowledge hub links* (read-only). It takes seconds (a few
thousand things); reading documents takes longer the first time.

### What's new (`/whats-new/`)

Every build is compared with the previous one, so anything that reaches the hub, from today's sources
or one added later, is noticed without code of its own. A **change** is one of:

| Change | Example |
|---|---|
| New | a partner, programme document, donor, grant, database, CPD indicator, document, Makani centre |
| Changed | a PD's status, end date, budget, disbursed or outstanding amount; a partner's risk rating; a CPD indicator's value, % achieved or status; a database's number of activity reports and latest month; a centre's latest monthly totals |
| Newly linked / No longer linked | a PD newly funded by a donor, a partner newly reporting in a database, a CPD indicator newly linked to a PD |
| Gone | a PD or document removed at its source |

A change is **notable** (in the daily note, the overview card and the assistant's default answer) when
it is a new or gone thing of the main kinds, a status or date that changed, a figure that moved by at
least 10% (and at least $1,000 for money, 5 for report counts), a funding or reporting link, a
critical review finding that appears or goes, or a finding whose severity changed. A finding growing
older (new, then still open) is not news, and its title counting the days down is kept as a minor
change. The rest (a new district in the gazetteer, a document newly mentioning a place) is kept and
shown with *Include minor changes*. When one build loses more than a fifth of one kind of thing (of at
least 10) with no error from its source, that is more likely a partial read upstream than news: those
removals are kept as minor changes and counted in the build's details as `suspect_removals` (admin →
*Import and sync runs*). The rules are in `neurodb/graph/changes.py`.
Each change is tagged with the sections it concerns: its own (a PD's), else those of what it is linked
to (a partner's or a donor's through their PDs). The first build is the starting point (nothing is
"new"), and figures recorded for the first time are not changes. Youth and education figures are not
tracked yet (their lookups give them live).

Where it shows:

- **The What's new page** (sidebar, every signed-in user; donor accounts cannot): the last 24 hours,
  7 or 30 days, by section (the user's own first), kind of thing, with or without minor changes; each
  line links to the page of the thing. Above the list, the latest daily note of the chosen section.
- **The overview**: a *What's new* card with the notable changes of the last two days in the chosen
  sections.
- **Ask NeuroDB**: `whats_new` (since a date, about one thing and what is linked to it, one kind or one
  section); each change carries the lookup for today's figures. Ask "what changed for Caritas this
  week?" or "what is new in Child Protection since 1 September?".
- **The daily note**: the job `whats-new` (`whats_new_digest`, daily 07:30 Beirut, after the hub) writes
  one note for everyone and one per section concerned, from the notable changes of the last 24 hours.
  With the assistant configured, the model writes two to five sentences from the change lines only
  (`store=false`; names and figures as NeuroDB holds them, no personal data); otherwise, or if it
  fails, the changes are listed. Run again the same day, the note is rewritten. Admin → Library and
  maps → *What's new notes*.
- **By email**, when `EMAIL_URL` is set (SMTP, e.g. `smtp+tls://user:password@smtp.office365.com:587`;
  the password is a secret, kept in the platform's secret store) and `DEFAULT_FROM_EMAIL`, `SITE_URL`
  for the link. People ask for it on the What's new page (*Email me the daily note*) and stop it there;
  each gets their section's note, or the note for everyone when they have no section or their section
  has no note that day. Donor accounts never get it. Without `EMAIL_URL` the button is not shown.
  While NeuroDB Watch's morning email is on, the button reads *Email me the morning note (For you and
  What's new)* and the note goes out inside that one email instead (see NeuroDB Watch).

Changes are kept (admin → *Knowledge hub changes*); a year of them is a few tens of thousands of rows.

### Ready for machine learning? (Data health)

Before any model is built, the job `ml-readiness` (`ml_readiness`, every Monday 06:45; *Run a job →
Machine learning readiness* for now) measures what each source really holds and shows it at the bottom
of the Data health page. Counts only: nothing is predicted and nothing leaves NeuroDB.

| Source | What is measured |
|---|---|
| ActivityInfo reports | years and months reported (the month is read from `month_name`, as on every page); months a partner reported between its first and last month (a missing month is not a zero); values that are 0 or empty; reports with a governorate, district and cadastral code; governorates matching NeuroDB's by name, spelling variant or code, with the names that do not match; days from the end of the month to the last edit in ActivityInfo (late reports and corrections) |
| Master indicators | targets set; indicators found again the year before (same section and AWP code or name) |
| eTools | PDs with locations, CP outputs and a section; PD indicators with targets; partner progress reports (one per PD, report number, type and period), how many have a due and submission date, how many were on time, and how many are for periods not ended yet (eTools creates them in advance); ActivityInfo partner names and records linked to eTools partners |
| Assurance and monitoring | partners with a HACT rating; assessments, audits and spot checks; action points no longer open, by status; programmatic visits |
| Daily review | days run; findings resolved; findings people took on |
| Context | latest population figures by level; Compiler years and periods; days of the change log |

For each decision a model could support, it says **Ready**, **Partly ready** or **Not yet**, and which
checks pass:

| Decision | Method it would use | Needs (thresholds in `neurodb/insights/readiness.py`) |
|---|---|---|
| Indicators or activities falling behind their targets | year-end forecast per indicator from its own monthly pattern | 2 years with 10+ months reported; 60% of indicators found again the year before; 70% with a target; 70% of months reported |
| Patterns across places, partners and periods | unusual reports and similar profiles, explained | 18 months of reports; 90% with a governorate, 70% with a district; 70% of months reported |
| Where partners overlap and where the gaps are | counts by place against need; grouping related activities | 90% of governorates matching; 70% with a district; 70% of records linked to eTools partners; 50% of PDs with locations; population figures 3 years old at most |
| Where to review implementation or data quality | a ranked list with its reasons: rules first, learning from outcomes later | 30 days of daily review; assessments or audits recorded; 30 progress reports with due and submission dates; 50 findings people resolved or took on |

The first model is chosen from these results with the programme teams; a check that fails says what
to improve first (for example, PD locations in eTools, or linking ActivityInfo partner names).

### Year-end forecasts (`/insights/forecasts/`, the first model)

For each ActivityInfo master indicator of the current reporting year that adds up month by month
(aggregation SUM; averages, maximums and ratios are not forecast), where it will likely stand in
December, with a range, against its target. The method is in `neurodb/insights/forecast.py`; it runs
on NeuroDB's servers, with no outside service and no new library.

1. **History**: every year's monthly sums of every additive master indicator (the same monthly sums
   as the dashboards; one query per database). An indicator is recognised across years by its section
   and its AWP code or name.
2. **Pattern**: the share of the year's total reached by the end of each month, averaged over the
   indicator's own past years and blended with its section's pattern (5 indicators or more), else
   with all indicators', else a straight line.
3. **Forecast**: value to date ÷ the share usually reached by then. Only **settled months** count: a
   month once 30 days have passed since it ended (late reports and corrections arrive by then).
4. **Range**: how far past forecasts made at the same month landed from the real year-end, for
   indicators with the same years of own history (0, 1, 2, 3+): the 5th to 95th percentile.
5. **Status**: *On course* when even the low end reaches the target, *Likely to fall short* when even
   the high end does not, *Uncertain* in between; also *Nothing reported yet*, *Too early to tell*
   (before 5% of the year's usual total) and *No target*.

**Back-test, and when forecasts are shown.** Every past year is forecast again at the end of each
month with only what was known then (the history before that year, and ranges from the *other*
years), and compared with the real year-end and with the straight line (value to date × 12 ÷
months). The page shows, at the end of March, June and September: the typical error (median gap as
a share of the real value) against the straight line's, how often the range held the real result,
how often "likely to fall short" was right, and the share of real shortfalls it caught. Forecasts
appear on the dashboards, the overview and in Ask NeuroDB only when, at the end of June, the method
beats the straight line and its range held the real result at least 70% of the time
(`GATE_MONTH`, `GATE_COVERAGE`); until then the page shows the test, and the forecasts to
administrators only, for review.

**Where it shows**: the *Year-end forecasts* page (sidebar, under the ActivityInfo databases; the
user's section first; filter by section and status), a *Likely to fall short by December* card on
the overview (the chosen sections) and on each database dashboard, and Ask NeuroDB
(`indicator_forecasts`), which is told to say they are estimates and to give the range.

**Running it**: the job `forecast` (`forecast_indicators`, Mondays 07:15; *Run a job → Year-end
forecast*). Each run replaces the forecasts; the back-test is in the run's details (admin → Import
and sync runs). Reading every year's monthly sums takes a few minutes on the production data.

## Scheduled jobs
The periodic jobs are managed in the admin: **Data and sync → Scheduled jobs**. Each row is one
command on one schedule, in **Beirut time** (summer time is followed automatically):

| Job | Runs | Default schedule (Beirut) |
|---|---|---|
| `locations` | `sync_locations` | `0 5 * * *`, daily 05:00: the eTools locations from the Datamart (`--source rest` for the older eTools REST API, which needs `ETOOLS_TOKEN`; its location-types endpoint is gone and is skipped) |
| `daily-review` | `daily_review` | `0 6 * * *`, daily 06:00, after the night's syncs |
| `activityinfo-data` | `import_activityinfo_data --current-year` | `0 18 1-22 * *`, 18:00 on days 1–22 |
| `etools-datamart` | `sync_etools_datamart` | `30 20 * * *`, daily 20:30 |
| `freshness` | `check_sync_freshness` | `15 * * * *`, hourly; a stale source is logged as an error |
| `activityinfo-structure` | `import_activityinfo_structure --all` | switched off; switch on or use *Run now* after the yearly rollover |
| `compiler-youth` | `sync_compiler_youth` | `0 21 * * *`, daily 21:00; switched off until Compiler is configured |
| `compiler-education` | `sync_compiler_education` | `30 2 * * *`, daily 02:30: asks Compiler (BMA) to count Makani and Bridging, waits, then reads the counts; switched off until Compiler is configured |
| `compiler-wellbeing` | `sync_compiler_wellbeing` | `0 3 * * *`, daily 03:00: asks BMA to work out the Makani wellbeing flags, waits, then reads them; switched off until Compiler is configured |
| `knowledge-hub` | `build_knowledge_hub` | `0 7 * * *`, daily 07:00: reads new library and CPD documents, then rebuilds the knowledge hub (it is also rebuilt after every sync) |
| `whats-new` | `whats_new_digest` | `30 7 * * *`, daily 07:30: the what's new notes, emailed to who asked |
| `watch` | `run_watch --daily` | `45 7 * * *`, daily 07:45: NeuroDB Watch's morning pass, the *For you* pages (see NeuroDB Watch); a quick pass also follows new data |
| `ml-readiness` | `ml_readiness` | `45 6 * * 1`, Mondays 06:45: is the data ready for machine learning (Data health) |
| `forecast` | `forecast_indicators` | `15 7 * * 1`, Mondays 07:15: year-end forecasts of the indicators, back-tested |
| (inside `activityinfo-data` and `etools-datamart`) | `link_partners` | at the end of both jobs |

On the page: switch a job on or off (the toggle saves at once), open it to change its schedule (five
cron fields: minute hour day-of-month month day-of-week; the form refuses a schedule it cannot read),
*Run now* from the row's **⋯** menu, or *Add scheduled job* to run another listed command on a
schedule (for example the eTools REST sync weekly). Only the commands in that list can be
scheduled. Each row shows its next run (marked *Overdue* in red when its time passed more than five
minutes ago without a start) and the last run of its command (a link to the run, or *Never run*),
with the scheduler's note under it when it could not start the job ("skipped: the previous run is
still going"). Every change records who made it (*Updated by*, and the History button).

**How it runs.** The scheduler runs inside the website: each gunicorn worker starts it in a thread
and a database lock lets exactly one of them act, so a job starts once however many workers or
replicas run. The banner above the list says whether it checked in during the last three minutes,
and on which host. While it does not, **Run due jobs now** (top right) does one scheduler pass from
the browser: it starts every switched-on job whose time has passed; the banner names
`SUPPORT_EMAIL`, when set, as the contact for a restart. The admin home's *Needs attention* lists a
silent scheduler, overdue or never-run jobs, runs that failed or succeeded with errors (a Compiler
job only while its schedule is switched on), and a missing daily review. NeuroDB Watch tells the
administrators the same lines on their *For you* page, and closes each one once it is no longer
listed (`neurodb/web/health.py` builds the list for both). A due job starts within a minute, in the
background, as the admin buttons do, and appears in *Import and sync runs* with *schedule* as its
trigger. A job whose previous run is still going is skipped until its next time. Times missed while the site was down (a deployment, a
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
**Stopping a run**: open it in *Import and sync runs* and press **Stop** (administrators). The run
becomes *Failed* with "Stopped by <user> at <time>", and the job can be started again at once. A
job waiting for the Compiler (BMA) to calculate quits within a minute (BMA's calculation itself
goes on); any other job finishes the work it is doing in the background without changing the
stopped run.

## Running jobs without a command line

Everything an operator runs with `manage.py` has a button in the admin, for administrators, each
with a confirmation step. Admin → *Data and sync* → *Import and sync runs* (also linked from the
admin home page, *Quick actions*):

| Button | Command it runs | How |
|---|---|---|
| **Sync eTools Datamart now** | `sync_etools_datamart` (core, all or chosen datasets) | background |
| *Scheduled jobs* page → row **⋯** → *Run now* | that job's command | background |
| Run a job → *eTools REST sync* | `sync_etools` | background |
| Run a job → *Locations sync* | `sync_locations` | background |
| Run a job → *ActivityInfo structure* (current year) | `import_activityinfo_structure --all` | background |
| Run a job → *ActivityInfo data* (current year) | `import_activityinfo_data --current-year` | background |
| Run a job → *Link partners* | `link_partners` | background |
| Run a job → *Daily review* | `daily_review` | background |
| Run a job → *Compiler youth figures* | `sync_compiler_youth` | background |
| Run a job → *Compiler education figures* | `sync_compiler_education` | background |
| Run a job → *Knowledge hub* | `build_knowledge_hub` | background |
| Run a job → *What's new note* | `whats_new_digest` | background |
| Run a job → *NeuroDB Watch* (also *Check now* on the For you page) | `run_watch --daily` | background |
| Run a job → *Machine learning readiness* | `ml_readiness` | background |
| Run a job → *Year-end forecast* | `forecast_indicators` | background |
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

**Running the sync now.** Admin → *Data and sync* → *Import and sync runs* → **Sync eTools Datamart now**
(administrators): choose *Partners and programme documents* (minutes), *Everything* (up to two
hours) or *Only the datasets ticked below* (for example `locations` alone after a gazetteer fix).
It runs in the background in the web container; each dataset appears in that list as it
finishes. Only one Datamart sync runs at a time (a database lock), whoever starts it. A deployment
restarts the container and kills a sync in progress: its run stays *Running* until the next
**Sync eTools Datamart now**, which sees that nobody holds the lock, closes the run as *Failed* ("Cut off")
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
| `tpm_visits`, `field_monitoring` | `tpm-visits/`, `fm-ontrack/` | TPM visits and field monitoring findings (field monitoring and partner pages); each finding is linked to the programme document its entity names, by the PCA/PD pair of the reference so that the `LEBA/` or `LEB/` prefix and the `-2` amendment do not matter (`pd_match` says how: exact, token, base or title; a PD entity that matches none is counted in `not_linked.programme_document_fm`) |
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
Search covers the same year; each kind of result shows its first 8 matches and a *See all* link
(`?group=`) when there are more. A Neuro Report or HPM page has an *Other years* menu listing the
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
country that an API filter let through is dropped (`details.other_country_skipped`). Keys naming
an e-mail address, a phone or a mobile are removed from the records kept whole here and from every
assistant answer; the field monitoring records reach the assistant without people and without long texts
(see *Data leaves Azure* under Ask NeuroDB).

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
`etools_record` (one record in full; a field monitoring record without people's names and long
texts). The programme and partner lookups also count the linked
records in every dataset.

The older eTools REST sync (`manage sync_etools`, token `ETOOLS_TOKEN`) is still available on
demand, for trips (`--only travels`) and the legacy engagement tables; locations come from the
Datamart (`sync_locations`). An "Invalid token" (HTTP 403) from it means `ETOOLS_TOKEN` was revoked or
expired: ask eTools for a new token and store it in Key Vault (never in a file). Its intervention details step writes the donor amounts in the same shape as the
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
| Delivery and assurance | Indicator status by section, assurance counts (field monitoring, TPM visits of every status, with the planned ones (the figure the field monitoring page shows) beside them, action points, HACT risk), findings by rating, what needs attention (at most eight items; overdue high-priority action points and one never-reported item always among them, "+N more" for the rest) | Partner monitoring rule, `datamart_monitoringfinding`, `datamart_tpmvisit`, `datamart_actionpoint`, partner risk ratings |
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

## NeuroDB Watch (`/for-you/`, the "For you" page)

NeuroDB Watch is the assistant that works in the background, without anyone asking. Every morning,
and shortly after new data arrives, it asks fixed questions of NeuroDB's own tables (code rules, no
AI): what falls due soon, what is still open, how these things connect. It remembers each thing it
follows from one day to the next, and tells each person once, at the right moment, on their **For
you** page. The AI only writes a short morning note and looks into a few critical points a day; it
never decides what is followed, how serious it is, when it is due or who is told. The code is in
`neurodb/watch` (one module per step, each with notes at its top).

**What staff see.** *For you* in the sidebar (under Ask NeuroDB), with the number of "Needs you"
points told today and not yet seen, and a *For you* card on the overview. The page shows, in order:
today's note (the section's, and the whole country's for the country view), *Needs you today* (at
most 5 points), *Coming up (next 30 days)*, *How things connect* (open points that meet on one
partner or grant), *Good to know*, *Everything else open* (collapsed: the open points not listed
above, so each point is on the page once) and *How this works*. Each point says why it is shown, for
as long as that is true: the step told today ("New", "Due in 3 days", "Got worse", "Agreed date
passed"...), a step of the last 7 days with when it was told ("Got worse · told yesterday"), or how a
closed point ended (*Resolved*; *Date passed, not done*; *No longer followed here*). It says how sure
NeuroDB is (*Sure*, *Likely*, *Please check*; forecasts are worded as estimates with their range), how it
knows (the source and when it was last synced, the records, the figures in words and a link), what
it remembers (first noticed, open for, told you...), what it connects to, and, when a look-up was
kept, *What NeuroDB looked up (AI)*. A banner says when the last morning check is more than 26 hours
old. The count and the card list only the points the page shows under *Needs you today*. After an
answer button, the keyboard focus moves to the answer's message, which is also read out to screen
readers. Donor accounts never see the page, the count or the card.

### What it follows: the checks

| Check | What it follows | Told to | Starts |
|---|---|---|---|
| `report_due_soon` | Partner progress reports (QPR) due in the next 14 days and not submitted (the rule every page uses); warning from 3 days before. Once due, it hands over to the daily review's `reports_overdue` | the PD's sections | trial |
| `action_point_due` | Open action points due in the next 14 days, per section and partner; warning from 3 days before | the action point's section | trial |
| `action_points_overdue_normal` | Open normal-priority action points past their date (the daily review keeps the high-priority ones) | the section | trial |
| `pd_ending` | Every active PD ending in the next 60 days, whatever its achievement; warning from 14 days before | the PD's sections | trial |
| `pd_awaiting_closure` | PDs ended in eTools and not closed (final report, funds liquidation); warning when ended over 60 days ago with money outstanding | the PD's sections, and the country view when late | trial |
| `grant_expiring` | Grants expiring in the next 90 days with at least `WATCH_GRANT_MIN_UNSPENT` USD unspent (the management brief's own figure); warning from 30 days, critical from 14 | the sections of the PDs it funds, and the country view | trial |
| `fr_expiring` | Funds reservations ending in the next 30 days with money outstanding; warning from 7 days | the PD's sections | trial |
| `hact_assurance_gap` | From 1 October to 31 December: partners whose programmatic visits, spot checks or audits are below what HACT requires; warning from 15 November, critical from 15 December | the sections of the partner's active PDs, and the country view | trial |
| `assignment_due` | Agreed dates on daily review findings (*Finding assignments*): 3 days before, and once passed | the finding's section, and the country view | trial |
| `forecast_short` | Indicators the year-end forecast says are likely to fall short, only while the forecasts are shown | the forecast's section | trial |
| `donor_account_expiring` | Donor sign-ins that stop working within 14 days | Administrators | trial |
| `reporting_year_rollover` | 15 December to 15 January: the new year is not the current reporting year yet | Administrators | trial |
| `daily_review` | The critical and warning findings still open in the latest daily review, under their own keys | the finding's section | on |
| `system` | The admin home's *Needs attention* lines (failed jobs, a silent scheduler, a missing daily review, the watch's own lines below) | Administrators | on |
| `stale_source` | A data source a check needs that has not synced within `SYNC_STALENESS_HOURS` | Administrators | on |
| `openai_quota` | The OpenAI credit ran out (see the circuit breaker below) | Administrators | on |

Looking ahead belongs to the watch and looking back to the daily review: one thing is never two
points. A point closes at once on positive evidence (a report submitted, a forecast back on course,
an assignment closed). Otherwise it closes only after two fresh morning passes without it, and then
shows as *no longer seen*, never as done: data removed upstream is not taken as work done. A check
whose source is stale is skipped (its points stay as they were) and the administrators are told. A
point that comes back within 14 days reopens with its history. A point that closes says how it ended:
*resolved* (done), *missed* (its date passed and it was not done: a report now overdue, a grant
expired with money unspent, a HACT year ended with assurance not done) or *changed* (no longer
followed here: its date moved, it moved to another section or partner, another point follows it). The checks read the Datamart-synced
eTools tables, finding assignments (only whether someone owns the finding and its status, never the
owner or the note), the latest daily review, the forecasts and the admin's health lines. They read no
child-level data. The watch writes only its own tables, the AI use ledger and its own runs: never
eTools, the knowledge hub, the knowledge base, the assignments or any programme data.

### When it runs

- **The morning pass**: the scheduled job `watch` (`run_watch --daily`, daily 07:45 Beirut), after
  the daily review (06:00), the knowledge hub (07:00) and What's new (07:30). While today's review or
  a hub build is still running it waits, looking every minute, for at most 20 minutes, then goes
  ahead and notes what was not ready. Its steps: match new eTools section names, send back to trial
  the checks people found unhelpful, run the checks and update the memory, connect the open points
  through the hub, decide who is told what, the morning notes, the email, the look-ups (last: they
  use what the notes left of the day's AI budget and time), then keep what it read and delete old
  rows. Each step commits on its own and is safe to repeat; a failing step is recorded and the next
  ones still run. The day counts as done only when its checks and announcements succeeded; otherwise
  the next quick pass catches the morning pass up. A pass that ends without its email (it failed, was
  stopped or ran out of time first) sends the day's What's new note in its place to the people who
  asked for the email, since the What's new email stepped aside for it; and a What's new note
  written after the day's morning email went goes out by itself. New data that lands while the
  morning pass runs gets a quick pass right after it, under the same lock (unless an administrator
  stopped the run). When another run holds the lock, the morning pass waits for it (every minute, at
  most 20 minutes).
- **The quick pass after new data**: when a knowledge hub build finishes (it follows every sync,
  every daily review and every document read), when any other job fails, or when a finding's
  assignment is saved, a quick pass follows about `WATCH_SETTLE_SECONDS` (600) later, so a burst of
  data makes one pass. It looks only at critical points, points due within 3 days and system points;
  it never calls the AI and never sends email. At most `WATCH_QUICK_PASSES_PER_DAY` (6) a day; later
  data waits for the next morning. When the morning pass was missed (the site was down at 07:45, or
  its checks or announcements failed), the next quick pass runs it instead.
- **By hand**: admin → *Import and sync runs* → Run a job → *NeuroDB Watch*; *Scheduled jobs* →
  `watch` → *Run now*; *Check now* on the For you page (administrators); or `python manage.py
  run_watch --daily` (`--when-requested` for a quick pass, `--date YYYY-MM-DD` for the day the checks
  reason from).

One run at a time (a database lock): a second start does nothing. Every pass is a run in *Import and
sync runs* (job *NeuroDB Watch*, target *daily* or *quick*) whose details say what each step did: the
checks' counts, `inputs_not_ready`, who was told what, the notes and why the AI was or was not
used, the look-ups, the model calls and tokens, the emails, `usefulness` (checks sent back to trial)
and, for a step that failed, its error (the run is then *Succeeded with errors*). A run stops between
steps when an administrator presses **Stop** or after `WATCH_TIME_LIMIT_SECONDS` (900).

### Who is told what

- **People**: active users with a role (or superusers), never a donor account. Each person follows
  their own section (*Section* on their user). Administrators and members of the **Management**
  group also get the **whole-country view**: the country's note (written from counts per section, not
  a copy of every section's points), the country-level points (grants, year-end assurance, agreed
  dates passed, PDs long ended with money outstanding, critical findings open a week with no one
  assigned) and every check still in trial. The Management group gives no permission and is not a
  role: fill it in admin → Users and access → Groups → *Management* (for example the Representative,
  the Deputy, section chiefs). Staff without a section get no notes and no count; their page lists
  everything open and asks them to have their section set (someone without a role, to be given one).
- **eTools section names** (admin → Data and sync → *eTools section names*): eTools spells sections
  its own way, so the watch keeps a map from each eTools name to a NeuroDB section. A name that is
  the same as a section's name or code is confirmed by itself; a name only contained in another waits
  for an administrator (select it, action *Confirm the matched section*, or open it and choose the
  section). A point whose eTools section name has no confirmed section goes to the Administrators
  only, never to every section, and *Needs attention* lists the names to confirm and the sections with
  staff that no name points to. Opening the list adds the eTools names not in it yet (so they can be
  confirmed before the first morning check; it says so when no eTools data is synced yet), and
  `python manage.py map_watch_sections [--rematch]` does the matching by hand. Each name shows the *Not mine* its section's staff gave in the last 30 days, per check:
  many of them point to a wrong match.
- **Told once.** A person is told about a point again only when it gets worse than what they were
  last told (kept on their receipt, so a rise later the same day is told the next morning), crosses
  one of its dates (for example 14, 7, 3, 1 and 0 days before a report is due; at most 3 reminders),
  becomes overdue, a snooze ends, it is still open 7 days after they marked it done (once), or it
  closes after they were told about it: as *Resolved*, as *Date passed, not done* (never "resolved"
  when its date was missed), or as *No longer followed here*. A pass never writes over an answer or a
  reminder given while it runs. What already existed before someone could hear of it (the first run of a
  check, the day a check is switched on, a person's first day) is recorded as known and not announced,
  unless it is critical or due within 3 days. Each person gets at most `WATCH_NEEDS_YOU_PER_DAY` (5)
  "Needs you" and `WATCH_GOOD_TO_KNOW_PER_DAY` (10) "Good to know" points a day; the rest stays on the
  page.
- **Reactions**: *Useful*, *Not useful*, *Done*, *Not mine*, *Remind me later* (tomorrow, next week,
  or 3 days before it is due), *Something's wrong* (with an optional short comment that only
  administrators read, in *NeuroDB Watch: what people were told*) and *Look into this* (opens Ask
  NeuroDB with the question typed, under the person's own hourly limit). Done, Not mine and
  Something's wrong hide the point for that person; a Section editor of the point's section or an
  Administrator marking it Done hides it for the whole section. A Section editor's Something's wrong
  hides it for their own section until its evidence changes (the other sections and the country view
  still see it); an Administrator's, or that of an editor whose section is the point's only one, hides
  it for everyone until its evidence changes. Three *Not useful* on one check within 30 days stop that check's
  "to note" and warning points for that person; critical points are still told. Opening the page
  clears the count; nothing else about browsing is recorded.

### Trial, switching checks on, usefulness

Every new check starts in **trial**: its points go to the whole-country view only, labelled as a
trial, and staff are not told. The daily review findings and the system checks are on from the start.
Admin → Data and sync → **NeuroDB Watch: checks** lists each check with its mode and its last 30
days: the people told about its points, *Useful*, *Not useful*, *Something's wrong*, *Not mine*, and
its **usefulness**, useful ÷ (useful + not useful + something's wrong) (*Done* and *Not mine* are left
out: one is about the work, the other about who was told). To switch a check on for staff, open it and
set *Mode* to *On*; the points that already exist that day are recorded as known and not announced,
so staff are not flooded. The same holds for the whole-country view on the day a check goes into
trial (*Trial since*: the day it was first listed, an administrator's change or going back by itself;
opening the list of checks lists every check, in its starting mode, before the first morning check); saving a check
without changing its mode, or *Keep in trial*, changes nothing. *Off* stops the check (its points are
kept, nobody is told).

A check that is on goes **back to trial by itself** during the morning pass when, among at least 20
ratings given since it was switched on (within the last 30 days), more than 40% say *Not useful* or
*Something's wrong*. Points told to the administrators only do not count. The admin home then lists
it under *Needs attention* ("The check “Action points due soon” went back to trial: 45% of 22
reactions said not useful or something's wrong.") until an administrator switches it on again, uses
the action *Keep in trial*, or switches it off. The rule is in `neurodb/watch/precision.py`.

The same page shows the **usefulness gate**, for information: whether at least half of the "Needs
you" points people rated over the last 4 weeks were useful (with at least 20 ratings, once a check has
been on for staff for 4 weeks). The background look-ups do not wait for it.

### The AI: the morning note and the daily look-ups

**The morning note.** One OpenAI call per audience that has new or changed points that day: each
section, and the whole country. The model gets the facts as JSON built by an allow-list and writes 2 to
6 plain sentences, each citing the points it rests on. A sentence is kept only when every number,
date and eTools reference in it is in the points it cites, and it has no link, no markup, no person's
name or email and at most 400 characters. When no sentence passes, when the AI is off, over budget or
paused, or when it fails, the note is listed by code from the points' own titles. A sentence that
uses the system's words (item, detector, receipt, agent, LLM) is dropped too. The page labels
each note *AI wrote this from the facts below* or *Listed by NeuroDB (AI not used today: ...)*. An
unchanged input makes no new call. Low reasoning effort, at most 1,500 output tokens, no tools.

**The daily look-ups.** After the morning notes and the email, the watch asks the AI to look into at most
`WATCH_INVESTIGATE_PER_DAY` (3) open critical points a day, those that became critical or got worse
today first. It runs Ask NeuroDB's own loop with these limits:

- only these read-only tools: `find_anything`, `entity_profile`, `connected`, `programme_details`,
  `partner_details`, `partner_reporting`, `pd_indicator_progress`, `funds_overview`,
  `assurance_overview`, `indicator_forecasts`, `whats_new`, `daily_review`, `data_freshness`. Never
  the raw eTools queries (`etools_query`, `etools_record`, `etools_search`, `etools_datasets`), the
  knowledge base's text (`search_knowledge`, `read_knowledge`), Makani, the management brief or
  charts; a call to another tool goes back to the AI as an error;
- every tool result, and a tool's error message, passes an allow-list before the AI reads it:
  numbers, dates and yes/no under any field, and texts only under the fields listed in
  `neurodb/watch/redact.py` (`TOOL_TEXT_FIELDS`: names, codes, statuses, periods), so a field a tool
  returns later is not sent until someone adds it there; never the fields that hold a person, a free
  text (such as a finding's detail) or a link, never anything about a Makani centre or a daily review
  finding of the knowledge hub, and never a known person's name or email. The `daily_review` result is
  replaced by the watch's own: the review's open programme findings with their title, severity,
  section and state, never its data checks (they carry sync error text);
- the tools run inside a read-only database transaction, so a write fails;
- at most 4 rounds and 150 seconds per look-up, low effort, its own prompt cache key;
- the answer is strict JSON. It is kept only when every number, date and eTools reference in it is in
  what the tools returned or in the point itself, and it names no person and has no link. It is
  stored on the point and shown on its card as *What NeuroDB looked up (AI)*; a result not kept is
  recorded with why. A point is looked into again only after it changed. A look-up the model
  service failed is recorded too: it counts in the day's number, and the point is tried again on a
  later day.

The AI never creates or closes a point, changes a severity or a date, chooses who is told, sends
anything, or writes to the hub or the knowledge base.

### What goes to OpenAI, and what never does

Sent (to api.openai.com over HTTPS, `store=false`, under the same API terms as Ask NeuroDB):

- for each point in a note or a look-up: its key, check, severity and confidence, the title written
  by code, its due date, days left and days open, the dates and numbers of its evidence, the name and
  kind of the hub thing it is about, its section names, whether someone owns it (yes or no), the
  assignment status, how many times it was told and whether its section marked it done;
- the situations (the names of the partner, PD, grant, donor and documents that connect the points),
  up to 15 What's new lines about them (written by code), and for the country note the counts per
  section;
- in a look-up, the allow-listed results of the tools above.

Organisation names, PD numbers and grant, donor, section and document names are sent, as the daily
review already does. Never sent: any person's name or email (the fields that hold one are never read,
and every text is checked against the list of names NeuroDB knows, in and out), the owner and note of
a finding assignment, people's reactions and comments, a point's detail and evidence records, system
points (they carry error text), document summaries or text, earlier notes or anything else the AI
wrote, the daily review's summary and decisions, figures marked for internal use, and child-level
data. No `safety_identifier` is sent: no person asked, and one constant identifier would pool every
background run.

### Costs, limits and the circuit breaker

Every AI call of every feature on the shared key is counted in one ledger: admin → Data and sync →
**AI use**, per day, feature (Ask NeuroDB, daily review, What's new, document summaries, periodic
figures, country programme reading, NeuroDB Watch) and model, in tokens, and in US dollars when the
optional `AI_PRICE_INPUT_PER_MTOK`, `AI_PRICE_CACHED_PER_MTOK` and `AI_PRICE_OUTPUT_PER_MTOK` are set.
Before every call the watch checks that:

- its tokens today (notes and look-ups together) plus the call's room stay within
  `WATCH_DAILY_TOKEN_CAP` (300,000): a note needs about 6,000, a look-up 50,000;
- its calls today stay within `WATCH_MAX_MODEL_CALLS_PER_DAY` (24; a look-up counts its 4 rounds);
- all AI features together stay under 80% of `AI_DAILY_TOKEN_SOFT_CAP` (3,000,000): the watch is the
  first to stop, so Ask NeuroDB keeps working;
- its AI is not paused.

The notes come before the look-ups, so the look-ups only use what the notes left of the day's caps.

Expected use: about 10 notes of 4,000 to 5,000 input tokens and up to 1,500 output tokens, and 3
look-ups of up to 4 rounds (each round resends Ask's instructions and the tools, most of it at the
cheaper cached rate), together roughly 150,000 to 250,000 tokens a day, about 5 to 8 million a month.
At example prices of $1.25 per million input tokens and $10 per million output tokens that is roughly
$10 to $30 a month; check the price of the model in use on OpenAI's pricing page. The cap bounds the
worst case at 9 million tokens a month. The OpenAI project budget and usage alert stay the hard outer
limit (see *AI assistant* above).

**The circuit breaker.** When OpenAI says the credit ran out (`insufficient_quota`), the watch stops
using AI for 6 hours, and the administrators get a critical point and a *Needs attention* line ("The
OpenAI credit ran out: Ask NeuroDB is affected too"). Three failed calls in a row pause it too. The
notes are listed by code meanwhile, and the first call that succeeds closes the point.

### The morning email

Dormant until NeuroDB can send email (`EMAIL_URL`, see *What's new*; set `SITE_URL` too, for the
link). From then on, each person who switched the email on (*Email me the morning note (For you and What's new)* on the
What's new page) gets one plain-text email after the morning pass: up to 5 "Needs you" titles with
their due dates, how many things are coming up, their section's What's new note (or the note for
everyone) and the link to For you. While the `watch` scheduled job is on, it replaces the What's
new email, so people get one email, not two (switch the job off and the What's new email goes out
again). It is sent once per person and day, even when the pass runs again; quick passes never email. It
never holds a point's detail or evidence, an assignment's owner or note, a comment, child-level data
or a figure marked for internal use. `WATCH_EMAIL=false` keeps the What's new email as it was.

### Switching it off, in steps

| To stop | Do |
|---|---|
| One check | NeuroDB Watch: checks → the check → *Mode* *Trial* (country view only) or *Off* |
| The AI only (plain notes, no look-ups) | `WATCH_AI=false` |
| The look-ups only | `WATCH_INVESTIGATE_ENABLED=false` (or `WATCH_INVESTIGATE_PER_DAY=0`) |
| The morning email only | `WATCH_EMAIL=false` |
| The morning pass | Scheduled jobs → `watch` → switch off (quick passes after new data go on; the admin home then stops warning that it did not run) |
| Everything | `WATCH_ENABLED=false`: no pass runs (a started run records only that it is switched off), no new data asks for one, and the For you page says NeuroDB is not checking |

Settings are environment variables (`.env.example`); in Azure, add them to the app's settings and
restart. Their defaults:

| Setting | Default | What it does |
|---|---|---|
| `WATCH_ENABLED` | `true` | The watch as a whole |
| `WATCH_AI` | `true` | The AI for notes and look-ups (also needs the AI assistant switched on) |
| `WATCH_MODEL` | empty: `AI_ASSISTANT_MODEL` | The model of the notes and look-ups |
| `WATCH_DAILY_TOKEN_CAP` | `300000` | The watch's tokens a day, notes and look-ups together |
| `WATCH_MAX_MODEL_CALLS_PER_DAY` | `24` | The watch's model calls a day |
| `WATCH_INVESTIGATE_ENABLED` | `true` | The daily look-ups |
| `WATCH_INVESTIGATE_PER_DAY` | `3` | Look-ups a day |
| `WATCH_SETTLE_SECONDS` | `600` | Wait after new data before a quick pass |
| `WATCH_QUICK_PASSES_PER_DAY` | `6` | Quick passes a day |
| `WATCH_TIME_LIMIT_SECONDS` | `900` | A run stops between steps after this |
| `WATCH_NEEDS_YOU_PER_DAY` | `5` | "Needs you" points per person a day |
| `WATCH_GOOD_TO_KNOW_PER_DAY` | `10` | "Good to know" points per person a day |
| `WATCH_GRANT_MIN_UNSPENT` | `10000` | Expiring grants are followed from this many USD unspent |
| `WATCH_EMAIL` | `true` | The morning email, once `EMAIL_URL` is set |
| `AI_DAILY_TOKEN_SOFT_CAP` | `3000000` | All AI features' tokens a day; background features stop at 80% |

### In the admin, and when something looks wrong

Admin → Data and sync: **NeuroDB Watch: things followed** (each point with its evidence, its dated
story, its connections and its look-up; read-only), **NeuroDB Watch: what people were told** (per
person and point: the step told, the reaction and the comment; read-only), **Morning notes (For
you)** (each note, who wrote it, why the AI was not used, its tokens), **NeuroDB Watch: checks**,
**eTools section names** and **AI use**. Administrators may look at what a section's staff see with
*Show notes for* on the For you page, but never at another person's reactions there.

The admin home's *Needs attention* has four lines of its own, also told to the administrators on
their For you page:

- *NeuroDB Watch has not run for more than 26 hours*: look at the `watch` scheduled job and the last
  runs of *NeuroDB Watch* in *Import and sync runs*;
- *NeuroDB Watch stopped using AI until ...*: the credit ran out or the AI failed three times in a
  row; add credit to the OpenAI project or wait, nothing else to do;
- *N eTools section names have no confirmed NeuroDB section*: confirm them (above);
- *The check “...” went back to trial*: look at its points and reactions, then switch it on again,
  keep it in trial or switch it off.

Nothing is lost when a day is missed: what each person was told is compared with what they last
heard, so the next pass tells what is still worth telling. Points closed or gone more than 24 months
ago, what people were told more than 12 months ago, and answered requests are deleted by the morning
pass. What the watch remembers is its own memory, not the knowledge base: nothing it concludes is
saved there or in the hub (see `docs/ICEBOX.md`).

## Donor access (`/donor/`)

A donor can be given a sign-in that sees **one page and nothing else**: no menu, no other page, no
internal API, no assistant. The page has two tabs.

- **Your contribution**: the donor's funds and what they paid for, filtered in the browser by grant,
  programme area and governorate: committed and disbursed (tiles, by area, by grant with its end
  date), a grant → area → partner flow, a schematic governorate map, children reached against
  targets, girls and boys and age (shown only when the indicators split them), cost per child
  against the country average of the same area, and one row per programme document (a programme
  past its end date shows "Closed" with its final result).
- **UNICEF Lebanon overall**: the whole country for the year as aggregates only: children reached
  (by month, programme area, governorate), results on track or ahead, programme and partner counts,
  field visits and sites visited. No programme, partner or donor is named and no amount of money is
  shown.

"Data as of" is the last successful eTools Datamart sync (funds and indicators); without one, the
latest successful eTools or ActivityInfo data sync.

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

## Youth programmes (`/youth/`, from Compiler)

Compiler is the platform where the youth partners register young people and enrol them in
activities, each under a youth **master indicator** and **sub indicator**, with the partner, the
donor, the Compiler programme document and the place. Compiler counts the young people itself and
NeuroDB reads **only the counts** (never a name, identifier or row about a person) from
`GET <COMPILER_API_URL>/api/youth/indicator-figures/?year=` (Compiler's
`student_registration/youth/indicator_figures.py`).

**How Compiler counts.** A young person counts once per figure, however many activities they joined
(enrolled twice in the same sub indicator, or in two sub indicators of the same master indicator:
once for the master indicator). Deleted registrations are left out. The place is the enrolment's
governorate and district, or the youth's registered address when the activity is "in the same
location". Age groups (under 15, 15-17, 18-24, 25 and over) come from the birth year and the
reporting year. Because the same young person can be in two partners' or donors' programmes, unique
counts cannot be added up: Compiler sends one table per grouping (every combination of master
indicator, partner, donor and governorate, alone or with one detail: sub indicator, programme
document, district, sex, age group or nationality; plus the sub indicators of each programme
document), and the page reads the table that matches its filters.

**The page** (Partnerships → Youth programmes; filters: year, master indicator, partner, donor,
governorate): young people reached, female and male; each master and sub indicator with young people
reached, the target (sum of the Compiler programme documents' targets; hidden under a governorate
filter, targets are not set per place) and the linked eTools indicator with what the partner
reported and its status; breakdowns by partner, donor, governorate, district, sex, age group and
nationality; **Compiler and eTools side by side** (per link: young people Compiler counted in the
programme document, the eTools target, the partner's last cumulative progress, the difference); and
the Compiler programme documents.

**Links to eTools.** After every read, NeuroDB suggests links: a Compiler programme document whose
project code is an eTools reference number (`LEB/PCA2026005` matches `LEB/PCA2026005-1`; an
agreement number matches when exactly one PD under it runs in the year), then within it the eTools
indicator whose title holds at least 60% of the youth indicator's words (a bonus when the targets are
equal; each eTools indicator goes to one youth indicator). Suggestions are replaced at every read.
Admin → Youth (Compiler) → Youth indicator links: select and **Confirm**, or add or edit a link
(saving it confirms it); confirmed links are never changed by a read. Put the eTools PD reference in
the Compiler programme document's project code for links to be suggested.

**Setting it up.**
1. In Compiler: create a service user (not a partner), add it to the group **NeuroDB API** (or the
   group named by Compiler's `YOUTH_FIGURES_API_GROUP` setting) and create its API token (Django
   admin → Auth Token).
2. In Key Vault: secret `compiler-api-token` = that token. Deploy with the bicep parameters
   `enableCompilerYouth=true` and `compilerApiUrl=https://<compiler address>` (locally:
   `COMPILER_API_URL` and `COMPILER_API_TOKEN` in `.env`).
3. Admin → Import and sync runs → Run a job → **Compiler youth figures**; then switch on the
   `compiler-youth` scheduled job (every night at 21:00, off by default). It reads this year and
   `COMPILER_YOUTH_YEARS - 1` years before it (default 2 in all); a year Compiler does not have is
   skipped.

## Education programmes (`/education/makani/`, `/education/dirasa/`, Makani and Bridging from Compiler)

In Compiler the education partners register children in **Makani (MSCC)** and **Bridging (Dirasa)**,
add them to services, and manage centers or schools, facilitators or teachers and daily attendance.
There is no programme document, donor or indicator behind these registrations. NeuroDB reads
**counts only** (never a name, identifier or row about a child).

**How Compiler counts, without weighing on the running system.** NeuroDB decides when Compiler counts:
the `compiler-education` job asks for a count (`POST /api/figures/runs/`), Compiler's Celery worker
counts and stores the result in a snapshot table (`student_registration/figures`), NeuroDB checks
the run every minute (`GET /api/figures/runs/<id>/`) and, once it is done, reads the counts
(`GET /api/figures/` and `/api/figures/<mscc|bridging>/?year=`), which only read the stored snapshot,
one indexed row, and never count during a request. Compiler keeps no schedule of its own. Each part of the count is one read-only SQL query
(`GROUP BY GROUPING SETS`, so each table is read once whatever the number of groupings) with a
statement timeout and a bounded `work_mem`, optionally on a read replica (`FIGURES_DATABASE`). One count
runs at a time: asking while one is going returns that one. A year never counted answers 404 and
is listed as *pending*. The API is limited to the "NeuroDB API" group and rate-limited.

**What is counted.**
- *Makani*: registrations not deleted in the rounds of a year (the current year also counts the
  registrations without a round yet, as the partners' lists do); partner = the registration's
  partner, else the center's; place = the center's governorate, district and cadaster.
- *Bridging*: registrations not deleted in the round; place = the registration's governorate,
  district and cadaster. A child who dropped out still counts; the learning result shows dropouts.
- A child counts once per figure however many registrations, services or partners, so rows do not
  add up to the total. Age groups (under 6, 6-9, 10-14, 15-17, 18 and over) come from the birth year.
- Parts: children (by partner, governorate, district, cadaster, center or school, round, registration
  type or level, sex, age group, nationality, disability, learning result, education status, source
  of identification, center type; Bridging also pre- and post-tested); services received (Makani:
  education, child protection, health and nutrition, digital, youth, follow-up, inclusion,
  recreational, Lego, referral; Bridging: the "yes" answers of the services form); Makani education
  programmes (BLN, ABLN, CBECE...); centers or schools with children; facilitators or teachers;
  attendance (child-days recorded and attended, days off left out, by month).
- *Cubes* (payload format 2, what the dashboards read): records counted per combination of every
  slicer at once, so they add up and any mix of filters is answered by summing rows. Makani
  `enrolment` (one record per registration: center, partner, governorate, sex, nationality, CWD
  type, caregiver, working, education programme and status of the latest education service, package,
  ID type, malnutrition result and development delay of the latest health record; with the flags
  married, IDP, caregiver counselling, immunized, screened, minimum meals, vaccinated) and `staff`
  (today's facilitators: the record has no year); Bridging `enrolment` (school, partner, the child's
  governorate, sex, nationality, CWD type, level, learning result, main attendance barrier),
  `teachers` and `trainings` (teachers per training topic), plus `outreach` (Compiler's Kobo outreach,
  shared by every programme, every year: interview year, partner and governorate as typed, and the
  answer to education status, referral, dropout reason, ID type). The payload also lists the centers
  and schools (type, emergency status, GPS point; schools with the children numbers they report).

**The pages** (Partnerships → **Makani (MSCC)** and **Dirasa (Bridging)**; `/education/` opens
Makani). They follow the programmes' Power BI dashboards: title "Makani Programme Dashboard <year>"
or "Dirasa Programme Dashboard <round>", the last data update (Compiler's count) and a year or round
select; one tab per Power BI page (`?tab=`); a slicer bar of single-select dropdowns ("All" by
default, options = the values found in the counts, "Not specified" for unknown values). A slicer
change replaces the results only (HTMX); the tabs keep the filters. A filter chosen on a tab that does
not have it (e.g. gender on the Dirasa map) stays in the links for the other tabs and the page lists it
as "not applied on this tab"; a figure a filter cannot reach says so in its hint (program staff,
in-school children, teachers).
- *Makani* slicers: CWD type, Education services (the programme without its level: "BLN Level 2" →
  BLN, "Summer RS Grade 3" → Summer RS, "YFS Level 1 - RS Grade 9" and "RS-YFS" → YFS - RS, catch-up
  with its programme), child nationality, child gender, caregiver (mother, father, other), working
  children, partner, governorate, center, center active during emergency.
  - *Overview*: Makani centers (with children under the filters), children enrolled (registrations:
    a child registered twice counts twice, as in the Power BI), children enrolled – unique count
    (from the unique-children counts, shown only when the filters are partner, governorate or center,
    else "—"), children with disabilities (a CWD type other than "No"), working, married, caregiver
    counselling, IDP children, program staff (today's list, place filters only); charts by
    governorate, nationality, gender, package and education programme.
  - *Education*: enrolment per programme and level, child identification ID, education status upon
    enrolment (latest education service; registrations without one left out of those charts).
  - *Health and nutrition*: centers, children, CWD, immunized, screened for malnutrition (a result
    recorded), eating minimum meals, vaccinated; malnutrition results ("No malnutrition screening"
    shown as "No malnutrition") and development delays ("No" left out).
  - *Maps*: children per governorate (choropleth) and the centers (points coloured by governorate;
    popup: partner, governorate, children, emergency), each with a table. Compiler's governorates are
    matched to NeuroDB's polygons by name, English or Arabic, with aliases (Baalbek-Hermel /
    Baalbeck-Hermel / بعلبك-الهرمل, Nabatieh / El Nabatieh / النبطية...); an unmatched one is marked
    "not on the map" in the table. Centers without a GPS point are in the table only.
- *Dirasa* slicers: CWD type, nationality, gender, school, partner, governorate, school type, school
  active during emergency. School filters (school, partner, governorate = the school's, type,
  emergency) choose the schools and the registrations counted are those of these schools (and of the
  partner chosen; partner "Not specified": registrations without a partner, schools without any);
  child filters (CWD type, nationality, gender) apply to registrations only.
  - *Overview*: Dirasa schools (with children under the filters), children enrolled in Dirasa,
    in-school children and in-school CWD (the numbers the chosen schools report; child filters do
    not apply), CWD in Dirasa, Dirasa teachers (of the chosen schools); charts: nationality (Syrian,
    Lebanese, Non-Lebanese), gender, type of schools, in-school Lebanese / non-Lebanese, governorate
    (the child's), teacher trainings by topic.
  - *Outreach*: its own slicers (interview year, latest by default or all years; partner and
    governorate as typed in Kobo); education status, referrals, initial dropout reasons, child ID
    type. Kobo names and labels of one choice are one key (older forms cut names at 40 characters);
    known keys have a label ("Referred to Dirasa", "UNHCR registered"...), others are shown with
    spaces for "_". Blank answers are left out.
  - *Attendance barriers*: the main barrier of each registration (blank left out).
  - *Schools map*: slicers partner, governorate, school type, emergency; schools coloured by type
    (popup: partners, governorate, type, children registered, in-school children) and their table.
- Everything is filtered in Python from the stored payload (`neurodb/education/cube.py`); each tab's
  result is cached 10 minutes per stored row, fetch, filters and language.
- A year stored from a Compiler without cubes (format 1) shows "Compiler sends the older format":
  run the education sync after Compiler is updated.
- If Compiler could not count a part (e.g. its query hit the timeout), the page says which and the
  other parts still show.

**Setting it up.**
1. Compiler: deploy branch `neurodb-education-figures` (migrations `figures.0001` and `0002`) with its
   Celery worker running (no Celery beat or periodic task is needed: NeuroDB asks).
   `python manage.py refresh_neurodb_figures` counts at once on the Compiler server.
   Optional settings: `FIGURES_DATABASE` (a read-replica alias), `FIGURES_STATEMENT_TIMEOUT_MS`
   (600000), `FIGURES_WORK_MEM` (32MB), `FIGURES_API_RATE` (120/hour).
2. NeuroDB uses the same Compiler URL and token as the youth figures.
3. Admin → Import and sync runs → Run a job → **Compiler education figures**, then switch on the
   `compiler-education` schedule (02:30; off by default; change the time in the admin). It first
   asks Compiler to count every programme's current year and waits for it (checking every
   `COMPILER_RUN_POLL_SECONDS`, default 60, for at most `COMPILER_RUN_TIMEOUT_MINUTES`, default 120);
   a count that fails or takes longer is written on the run, which ends *partial*, and what Compiler
   has is read all the same. `--no-calculate` only reads. It reads the
   current year or round of each programme and the counted ones before it
   (`COMPILER_EDUCATION_YEARS`, default 3). A year Compiler has not counted yet is listed as
   *pending* in the run and arrives at the next run.

## Country programme (`/country-programme/`, CPD results and progress)

The page shows whether the country programme is on track with its outcomes and outputs. Everything is
set in admin → **Country programme**.

**1. The cycle.** Add a *country programme cycle*: name, first and last year (a cycle of 2, 3 or 5
years; 7 at most), and tick *Current* for the cycle the page opens on (one at a time; the page offers
the others in a list). *Country programme in eTools* is the country programme as eTools writes it on
the programme documents (`country_programme`); when set, only those programme documents count as the
cycle's interventions, else every programme document running in the cycle's years (not draft or
cancelled).

**2. The documents.** On the cycle, tab *CPD documents*: upload the CPD, the results and resources
framework, annexes, reviews (PDF, Word, text, Excel, PowerPoint; 50 MB at most). They are stored in the
media storage (Azure Blob in production, under `cpd/<cycle>/`), listed on the page, and downloaded by
signed-in users only (donor accounts cannot reach the page).

**3. The results framework** (outcomes → outputs → indicators; an indicator sits under an outcome or
an output), in any of three ways, which can be mixed:

- *Typed in the admin*: Outcomes (with their outputs and indicators inline), Outputs, Indicators.
- *Excel*: on the cycle, **Excel template** downloads the sheet, pre-filled with what is already
  entered (so it is also the export); fill it and **Import from Excel**. One row per outcome, output
  or indicator; *Parent code* ties an output to its outcome and an indicator to its outcome or output;
  one *Milestone <year>* column per year of the cycle. Items are matched by code: importing again
  updates them and never deletes. The whole sheet is checked first; when a row is wrong nothing is
  saved and the page lists the rows to fix.
- *AI-suggested from the CPD*: in *CPD documents*, select the CPD — a PDF, a Word document (.docx)
  or a text file (.txt, .md; an old .doc must be saved as .docx or PDF first) — and run **Propose the results
  framework**. The document is read in the background (a minute or two) by the OpenAI API with the
  same key and model as Ask NeuroDB (`OPENAI_API_KEY`, `AI_ASSISTANT_MODEL`), with `store=False`
  (a PDF is sent as a file, the text of a Word or text document as text);
  the CPD is a public document. The proposal appears in *AI-suggested frameworks*: **Review and
  apply** lists every outcome, output and indicator with its baseline and target; untick what is
  wrong and apply. Nothing enters the framework before that. Applied items are marked
  *AI-suggested* (on the page too) until someone checks them against the document and runs *Mark the
  selected AI-suggested indicators as reviewed* (or sets *Origin* to *Entered in the admin*).

For each indicator: unit (number or percentage), direction (increase, or decrease for a rate to
reduce), *cycle value* (the latest year's value for a level such as a rate; the sum of the years for
people reached), baseline and its year, the end-of-cycle target, and optional yearly milestones.

**4. What measures progress.** On an indicator:

- *Linked sources* (summed for their year): pick the source from the list — the eTools PD indicators
  of the programme documents under the output (partner reporting: the partner's cumulative progress
  in the year), the ActivityInfo master indicators of the cycle's years (the master indicator's
  result), the Compiler youth indicators (young people reached) or Makani / Dirasa (children
  enrolled). The year fills itself from the source when it can. Untick *Confirmed* to keep a link
  without counting it.
- *Values typed here*: a survey, ministry data, an annual report. A typed value replaces the linked
  sources for its year.

For an output, the page also lists the programme documents that contribute to it and their partners,
with the status of their eTools PD indicators this year. A programme document contributes when one of
its CP outputs in eTools is the output: the name set in *eTools CP output* on the output, else the
output's code at the start of the eTools name ("1.1 …", "Output 1.1: …").

**The status rule.** Progress = the share of the way from the baseline to the target that the value
has covered (a decrease counts the same way). Expected = this year's milestone as a share of that way
when one is set, else the share of the cycle elapsed (1 January of the first year to 31 December of
the last). Within 10 points of expected: on track; below: off track; above: ahead of schedule
("Target reached" at 100 %). No target: *no target*; no value yet: *no data yet*. It is the same
tolerance as the other NeuroDB pages.

## Makani wellbeing (`/makani/wellbeing/`, flags worked out in BMA)

BMA (Compiler) works out, when NeuroDB asks (every night by default), which Makani children may need a follow-up and why (absence
streaks, low or falling attendance, a required service not received, a dropout without follow-up,
malnutrition, developmental delay or a protection concern without referral, no learning progress
between tests), and monthly centre summaries. BMA only calculates and serves them; NeuroDB shows
them. The rules, thresholds and the BMA side are described in BMA's
`student_registration/wellbeing/README.md`.

- **Calculating and reading**: the job "Read the Makani wellbeing flags from Compiler"
  (`sync_compiler_wellbeing`; admin → Import and sync runs → Run a job, or the `compiler-wellbeing`
  schedule, daily 03:00; off until Compiler is configured) first asks BMA to work the flags out
  (`POST /api/wellbeing/runs/`) and waits until BMA is done (as for the education counts:
  `COMPILER_RUN_POLL_SECONDS`, `COMPILER_RUN_TIMEOUT_MINUTES`), then reads the flags changed since
  the last run and the last 12 months of centre summaries. BMA keeps no schedule: when and how often
  the flags are worked out is set here, in Scheduled jobs. A calculation that fails or takes too
  long ends the run *partial* with the reason, and what BMA has is read all the same.
  `--no-calculate` only reads; `--full` reads every flag again.
- **BMA side**: deploy branch `makani-wellbeing-flags` (migrations `wellbeing.0001`–`0003` and
  `attendances.0069`) with its Celery worker running; no Celery beat or periodic task is needed. It uses the same `COMPILER_API_URL` / `COMPILER_API_TOKEN` as the youth and education
  figures; the BMA service account must be in BMA's "NeuroDB API" group.
- **No names**: a child is known here only by the BMA registration number, gender, age band and
  nationality. "Open in BMA" opens the child's profile in BMA, with the user's own BMA access.
- **Who sees what**: every signed-in user sees the centre summaries (counts); administrators and
  section editors see **Children to follow up** and record the follow-ups (how, result, date, a note
  without names). A follow-up is sent to BMA, which closes the flag; BMA reopens it only when newer
  data shows the problem continues. When BMA cannot be reached, nothing is saved and the page says so.
- Protection concerns follow the child protection referral procedure straight away; the flag only
  shows that no referral is recorded.

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
