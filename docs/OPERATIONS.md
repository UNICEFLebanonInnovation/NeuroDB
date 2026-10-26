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

**Migrations must be quick.** A web container serves nothing until its migrations are done, and App
Service stops a container that has not started within its start-up limit (about four minutes). A
longer migration is cut off and started again from the beginning at every restart (an atomic one is
rolled back; a batched one keeps the batches it committed and rewrites them), and the site answers
*503 Service Unavailable* until an image without it is deployed (this happened with the eTools
"Reached" release, whose column fill read every partner report row). The log shows each migration
on its own line as it starts ("Applying ..."), and " OK" on the next line when it is done. So a
migration only changes the
schema or writes small tables (settings, schedules, prompts); work over the large tables (partner
reports, eTools records kept whole, Monitoring insights rule checks) runs once the site is up, as a
background job a schedule starts at once: *Finish the eTools update* (below) is the example. The
test `tests/datamart/test_backfill.py` lists every migration that runs code and fails on a new one
until it is checked and listed as quick. On App Service the application setting
`WEBSITES_CONTAINER_START_TIME_LIMIT` (seconds, at most 1800; `infra/env/azure-app-settings.json`
sets 600) gives a deployment's migrations more room than the default 230 seconds; it is a safety
margin, not a reason to write a long migration.

The two migrations of the pages that load in parts are listed in that test as quick, each with its
reason. Core `0026_shared_cache_table` creates a new, empty table (the shared page figures,
`neurodb_cache`, see *Pages that load in parts*), so nobody ever runs `createcachetable`. Core
`0027_syncrun_indexes` adds two indexes over the sync runs (the last finished run, and the latest run
of each job and target) with a plain `CREATE INDEX` inside the migration's transaction, after
`SET LOCAL lock_timeout = '10s'`. It never uses `CREATE INDEX CONCURRENTLY`: a concurrent build waits
for every older transaction in the whole database, so a long job still running in the previous
container during a deployment would hold the new container's start-up past App Service's limit (the
503 above). The sync runs table holds a few thousand rows and each index is built in milliseconds;
only the wait for the table's lock (a job writing its run) could be long, and it is bounded: after
10 seconds the migration fails, the container stops, and its restart tries again. A new index over a
small table follows the same pattern.

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

## The menu: focused for every role, full for superusers

Management found the full sidebar too long for most people, PD managers above all, who want partner reporting
in numbers and tables in as few taps as possible. So every role sees a **focused menu** by default:
*Home*, *Partner reporting (PRP)* (*Progress reports* and *Partner monitoring*), *Field monitoring* and
*Ask NeuroDB* (`navigation.FOCUS`, `navigation.FOCUSED`). That is Administrators, Section editors and
Viewers, with or without staff status, and the members of the Management group (a group, not a role).
**Superusers** always see the full menu (`navigation.MENU`). Donor accounts are unchanged: they see their
own page only. Visitors are unchanged.

**Choosing what a role sees: Users and access → *Menu*** (`/manage/web/menuchoice/`, also a Quick action on
the dashboard: *Choose what each role sees in the menu*). One tick per entry and role, the entries in the
order a role's sidebar draws them: the focused part first, then the rest of the full menu under its usual
group titles, then *ActivityInfo* (the monthly results or the archive) and *Help*. **Save** stores the
ticks; **Reset every role to the focused menu** removes every saved choice. Each role that changed is in
the audit trail (the entries added and removed), and the page links each saved role's *History*. A role
cannot be saved with nothing ticked. The page is for a **superuser with staff status** (a superuser without
staff status cannot open the administration at all); an Administrator gets a 403 there even though the
Administrator group holds every permission, and only a superuser can make someone a superuser or give a
permission one by one in Users (both are read only to anyone else). A superuser's own account is read only to
anyone else too: its form opens read only, its password form and its delete page answer 403, the list's
set-role, activate and deactivate actions skip it and say so, and *Delete selected* is refused. Otherwise an Administrator could set a
superuser's password and sign in as them, or deactivate every superuser and leave nobody to open the page.

- **No row for a role means the focused menu.** A save that ticks exactly the focused menu for a role removes
  that role's row, so a later change of the focused menu in code reaches every role that was never
  customised. A page added to NeuroDB later shows to superusers at once and stays out of every role's menu
  until a superuser ticks it.
- **A save shows at once** on the worker that saved it, and **within 10 seconds on every other worker**
  (`neurodb.web.menu_choices`: kept in memory 10 s, shared through the database cache, like the AI
  settings). A warm page makes no query for the menu; a worker reads the shared cache at most once every
  10 seconds. Before the release's migration has run, every role gets the focused menu.
- **Nothing is blocked.** Only the sidebar is shorter: every page still opens from links, bookmarks and the
  search, whose *Pages* group lists every page of the full menu under its usual conditions. A row that shows
  only while its source is live (Ask NeuroDB, Monitoring insights, Country programme, the Compiler pages,
  Document review) follows the same rule in a role's menu.
- **What the focused menu leaves out**, and how to get it back with a tick:
  - *ActivityInfo* holds the only menu links to the year's **Neuro reports** and **HPM reports**, where
    section editors write their section comments. While ActivityInfo is in use, tick *ActivityInfo* for
    Section editors; otherwise they find a report by its name in the search.
  - *For you* and *Daily review*: NeuroDB Watch reaches people through For you's count in the sidebar and
    its card on Home. Tick *For you* for a role that should keep the count.
  - *Help*: the "?" panel links to the Help pages (*Help pages*), and the search finds them.
  - *KM Database* is in the full menu only: tick it for a role on the Menu page.
- **Home follows the menu.** Home leads with partner reporting for everyone (see *Home (`/`)*); the rest of
  Home (the Watch card, *Needs a decision*, the daily review, *Since*, the key figures, the sources) is under
  *More on Home*, folded for a role whose menu shows neither *For you* nor *Daily review* (every role by
  default: Administrators and Management members, too, open it for *Needs a decision* and the Watch card)
  and open for superusers. Ticking *For you* or *Daily review* for a role opens it for that role.
- Quick migration `web 0001_menu_choice` (a new, empty table of at most three rows).

## Secrets rotation
Key Vault secrets: `django-secret-key`, `database-url`, `activityinfo-username` and
`activityinfo-password` (the ActivityInfo account's email and password; or `activityinfo-token` with
`activityInfoAuth = 'token'`), `etools-token`, `etools-username` and `etools-password` (the eTools
Datamart service account), (with SSO)
`entra-client-secret`, (with the AI assistant, when the key is not entered in the admin's AI settings)
`anthropic-api-key` or `openai-api-key` and (with Compiler youth figures) `compiler-api-token`. To rotate, set a
new version in Key Vault, then restart the active web revision (`az containerapp revision
restart`). Jobs pick the new value up on their next run.
No code change is needed. Rotating the Django secret key signs everyone out and changes the
per-user identifier sent to the AI provider (Claude's `metadata.user_id`, OpenAI's `safety_identifier`,
below), so the provider's misuse history for each user starts afresh. The API keys entered in AI settings
are sealed with the secret key: after a rotation they can no longer be read, Needs attention says so,
and an administrator enters them again in AI settings (a key set by the hosting team is not affected).

## AI assistant (Ask NeuroDB)
Signed-in users ask questions in plain language on `/ask/` or from the search box (Ctrl K; staff only:
visitors and donor accounts have no search box). The search box's "Ask NeuroDB AI" link opens
`/ask/?q=...` with the question filled in; nothing is sent until the user presses Ask, so a crawler or a
page reload never asks it. The answers start from eTools for this year; the ActivityInfo tools serve the
ActivityInfo years, and the line after the date tells the model whether ActivityInfo is in use or an
archive (see *ActivityInfo: in use or archive*).
The AI answers them: Claude, Anthropic's model, called through the Claude API (the Messages API;
platform.claude.com), the preferred one; or ChatGPT, an OpenAI GPT model called through the OpenAI API
(platform.openai.com, Responses API), not the consumer ChatGPT app or Claude.ai. Which one runs is chosen
in the admin's AI settings (below). The model calls read-only lookups over NeuroDB's own
services (indicator results, activity reports, Neuro reports, programme documents, donors,
partners, every eTools Datamart dataset NeuroDB holds (funds, indicators, assurance, reporting,
monitoring, PD narratives and reviews..., without e-mail addresses or phone numbers),
population, library, data freshness) and links each answer to the pages its figures
come from.

- **Choosing the AI** (Admin → Data and sync → **AI settings**, one page, no command line): every AI
  feature (Ask NeuroDB, the Help assistant, Monitoring insights, NeuroDB Watch, the daily review,
  What's new, the document summaries, review and periodic figures, the country programme reading) runs
  on one provider: **Claude** (Anthropic), the preferred one, or **ChatGPT** (OpenAI). *Automatic*, the
  default, uses Claude when an Anthropic key is available (set by the hosting team or entered on the
  page) and OpenAI otherwise. The page holds the provider, the two API keys (write-only: only their
  last 4 characters are shown again), the model and default effort of each provider, *Retry a declined
  answer on another Claude model* (on by default, see *Refusals and failures*), *In use now*, a table
  of which model each AI feature asks for and runs on now, and **Test the connection** (one tiny
  request, "Reply with the word OK.", with no NeuroDB data; the result is kept on the page). A value
  the hosting team sets comes first: `AI_PROVIDER` for the provider and `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` for the keys (the page shows their last 4 characters and disables the key box); a
  model or default effort chosen on the page comes before `CLAUDE_MODEL`, `CLAUDE_EFFORT`,
  `AI_ASSISTANT_MODEL` and `AI_ASSISTANT_EFFORT`. Keys entered on the page are sealed with the site's
  secret key (`DJANGO_SECRET_KEY`): after that key changes they cannot be read any more, Needs attention
  says so, and an administrator enters them again. A save applies with no restart: the worker that
  saved at once, the others within 10 seconds. A run already going (the AI checks, the action point
  review, the document review) finishes on the provider it started with; if that provider's credit runs
  out meanwhile, the pause names its credit, not the provider chosen since. A model of the other
  provider is never sent: a prompt version, the Score settings or `WATCH_MODEL` naming an OpenAI model
  runs on the Claude model while Claude is in use (and the other way round), and nothing stored is
  rewritten; the table on the page shows which model runs.
- **API key and billing, Claude** (the Claude Console, platform.claude.com, with the organisation's
  account, not a personal one): in the organisation's workspace for NeuroDB, create an API key on the
  API keys page. API usage is prepaid: buy credits under Billing. It is billed separately from any
  Claude.ai subscription, which includes no API credit. Set a spend limit and usage alerts on the
  workspace. Enter the key in AI settings, or give it to the hosting team as `ANTHROPIC_API_KEY`.
- **API key and billing, OpenAI** (platform.openai.com, with an organisation account, not a personal
  one): in the organisation, create a project named `NeuroDB` (only organisation owners can create
  projects), then create an API key owned by that project on the API keys page
  (`https://platform.openai.com/settings/organization/api-keys`). API usage is prepaid: buy credits
  under Billing. It is billed separately from any ChatGPT subscription, which includes no API
  credit. Set a budget and usage alerts on the `NeuroDB` project's limits page.
- **Switch on**: enter the key in AI settings; or store it in Key Vault as `anthropic-api-key` (or
  `openai-api-key`) and reference it as `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) (App Service:
  `@Microsoft.KeyVault(VaultName=neurodb-prod-kv;SecretName=anthropic-api-key)`; Container Apps:
  `anthropicKeyInKeyVault = true` for the Anthropic key, `enableAiAssistant = true` for the OpenAI key,
  in `main.bicepparam`; each needs its secret to exist, or the revision fails), then restart the app. A
  key entered in AI settings needs no restart, and a key not kept in Key Vault has no reference: delete
  its entry from the pasted settings (`docs/DEPLOYMENT_AZURE.md`, *Environment variables*). Without a
  key the assistant is off and the search box ("Search partners, programme documents, indicators")
  works as before; `/ask/` then points users to Search and the Management brief, and only staff see
  where the key goes. Never put a key in a committed file or a Bicep parameter.
- **Settings**: `AI_PROVIDER` (`auto`, the default: the choice in AI settings, else Claude when an
  Anthropic key is available; `claude` or `openai` pins it, before AI settings; any other value stops
  the app at start-up), `ANTHROPIC_API_KEY` (optional: the key can be entered in AI settings instead),
  `CLAUDE_MODEL` (default `claude-opus-5-5`; a Claude model id, starting with `claude-`, else the app
  does not start), `CLAUDE_EFFORT` (how much Claude thinks before it answers: `low`, `medium`, `high`,
  `xhigh` or `max`; default `medium`; a feature's `none` or `minimal` runs as `low`; any other value
  stops the app at start-up), `AI_ASSISTANT_MODEL` (the OpenAI model, default `gpt-5.5`; it can be
  switched to a newer model such as `gpt-6-sol`, released on 2026-09-22, after checking its price on
  OpenAI's pricing page and trying it on staging), `AI_ASSISTANT_EFFORT` (OpenAI's reasoning effort,
  default `medium`; the API's values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh` and
  `max`, but not every model accepts every value: gpt-5.5 takes `none`, `low`, `medium`, `high` and
  `xhigh`. Lower is cheaper and faster, higher reasons longer. Any other value stops the app at
  start-up: the website and the jobs do not start until it is corrected), `AI_ASSISTANT_HOURLY_LIMIT`
  (questions per user per hour, default 30), `AI_ASSISTANT_MAX_TOOL_ROUNDS` (rounds of lookups per
  question, default 8), `AI_ASSISTANT_TIME_LIMIT_SECONDS` (default 180) and
  `AI_ASSISTANT_ENABLED=false` to switch every AI feature off whatever key is set or entered. A
  question counts towards the hourly limit from the moment it is asked, and a user can have at most
  two questions being answered at the same time.
- **Data leaves Azure**: each question, up to six earlier questions and answers of the same
  conversation, and the data looked up to answer it are sent to the provider in use: the Claude API
  (api.anthropic.com) or the OpenAI API (api.openai.com), over HTTPS. That data is the figures plus
  partner, programme-document, donor and library details, such as partner names, vendor numbers, risk
  ratings, HACT and assurance results, programme document titles, library summaries and the free-text comments staff write on Neuro and HPM reports. When a filter
  matches nothing, the list of known partner, donor or office names is sent too. It is organisational
  data. The summary lookups send field visits as counts. The HACT visit plan lookup (`hact_visit_plan`)
  sends counts, section names and the remarks' labels only: never a partner, a programme document
  reference, a section comment or a focal point. The lookups that return eTools records as eTools holds
  them (`etools_query`, `etools_record`, `etools_datasets` and the examples of `etools_search`) read
  only the standard, rating, partner and date of the PSEA assessment answers (`psea_answers`): their
  comments can describe a case of sexual exploitation or abuse, so every other key is left out, and
  those records are never searched by text nor filtered, grouped, summed, sorted or picked on another
  key. They treat the field monitoring datasets (findings, questions and answers, answer options,
  programme activities) apart: the keys that hold a person (visit lead, team members, monitors, focal
  points, assignees, user names, contacts, comments, any key named like a person, an e-mail or a phone,
  also inside a nested value) are dropped and cannot be filtered, grouped, summed, sorted or picked;
  texts longer than 80 characters (narratives, answers, summaries) are withheld ("text withheld: read it
  in Monitoring insights"); shorter values (ratings, statuses, references, place names, "Yes") are sent
  without e-mail addresses, phone numbers (any 7 or 8 digits, also as an answer's `value`,
  `answer.value` or `answer_value`: an answer, never an amount), links, the person names NeuroDB knows
  (the field monitoring team members included, once Monitoring insights has built its visits) and any
  name written after a
  title such as Mrs, Dr or Sheikh (a place named that way, such as Sheikh Zennad, is withheld too); a
  question that names a person finds no field monitoring record, a search or a filter looks in the texts
  as these rules clean them (the long ones too, never sent), and the search examples show the visit
  reference only. The four field monitoring look-ups (`fm_summary`, `fm_visits`, `fm_visit`,
  `fm_search`, registered by Monitoring insights when `FMM_ENABLED` is on) give Ask the visits,
  entities, ratings, HACT Q1, report quality, urgency and follow-up of this calendar year in the whole
  country as **structured fields only**: no monitors' note, no checklist answer and no search snippet
  ("narratives are available in Monitoring insights"), and never a visit lead or a team; `fm_search`
  says which visits mention a word, and refuses a search for a person NeuroDB knows. Every other dataset
  is sent without a person too, by one rule shared with Monitoring insights (`catalogue.holds_person`).
  Keys naming an e-mail address, a phone or a mobile are removed, as before, and so are the keys that
  hold a person, by their words (camelCase and acronyms split: `UNICEFFocalPoints`): assignees, authors,
  travellers, focal points, leads and heads (not a lead office or section), teams, members, users,
  owners, reviewers, supervisors, officers, signatories, approvers, managers, coordinators, assessors,
  chiefs, representatives, directors, contacts and any way to reach someone (WhatsApp, Viber, telephone,
  landline, hotline, cell, fax, tel), anything "by" someone (`submitted_by_name`, `led_by`, `rated_by`;
  not `visits_by_status` nor a party such as `provided_by`), the parts of a name
  (`signatory_first_name`), any other `<role>_name` that is not the name of a thing (`visitor_name`,
  `reporter_name`, `pme_name` are people; `partner_name`, `cp_output_name`, `tpm_name`, `ngo_name`,
  `place_name`, `auditor_name` stay), a name inside an object whose key is not a thing (`approval.name`,
  `review.display_name`, `sent_back.label`; the names in `location`, `sections_data`, `offices_data`,
  `locations_data` and `cp_outputs_data` stay), comments and notes, and a key that is itself an e-mail
  address, a phone or a person's name (a key that is a number by its dataset's rule: `1234567` stays
  outside field monitoring). An object written as text (JSON or Python's quotes, with its
  `Decimal('120.00')`, `datetime.date(2024, 1, 1)` or `UUID(...)` read as their text, "120.00",
  "2024-01-01") is read back first; one that still cannot be read is withheld whole
  ("[object withheld]"), and the names under its person keys are still learnt. A key that holds a
  number, a date or a kind stays (`persons_reached`, `team_members_count`, `approved_cost_traveler`,
  `unicef_cash`, `signed_by_unicef_date`, `submitted_at`, `user_type`; not `staff_on_duty` nor
  `focal_point_at_site`). These keys, and the keys naming an e-mail address, a phone or a mobile, cannot
  be filtered, grouped, summed, sorted or selected on; a key inside an object cannot be grouped, summed
  or sorted on at all. A sort is by the main date, by a field's numbers as Ask is sent them (a withheld
  number sorts with the empty values) or by a field that holds nothing but dates; any other field
  (a text, an object) is sorted by its values as Ask is sent them (at most 100,000 records of at most
  20,000 different values), never by the stored text, which would tell where a withheld name falls. A
  group on a record field is its value as Ask is sent it: values sent alike are one group, counted and
  summed together (three places that differ only by a withheld focal point are one place), ordered by
  their records and then by the value sent (at most 20,000 different values; when more than 20,000
  records match, counted over the whole dataset, never over the records a query found). Every text is
  sent whole (cut at 400 characters) but without e-mail addresses (even cut short, spaced
  "x @ unicef.org", with a full-width or small @, in brackets "x(@)unicef.org", "x [@] unicef.org", with
  spaced dots "x . y @ unicef . org", with an invisible character inside, or spelled out:
  "x at unicef dot org", "x[at]unicef[.]org", "x{at}unicef.org", "x<at>unicef.org", "x -at- unicef.org",
  "x at unicef.org", "x at hotmail.fr", "x at yahoo.co.uk", "x dot y at unicef dot org",
  "x . y at unicef . org", "Layla.Saab at Unicef.Org", or cut short after a space where an address can
  be, before a domain in lower case or after a word in lower case or with a full stop, an underscore or
  a digit, "x @unicef", "R.Haddad @Unicef"; "located at Tyre. Net enrolment rose", "met at school.it
  was", "meeting @10am", "Workshop @Beirut" and "Follow @UNICEFLebanon" stay), links, phone
  numbers, the person names NeuroDB knows (every name the eTools records hold under the keys that name a
  person among them, at any depth and split names put together, read from every record at the end of
  each eTools Datamart sync and stored in a table of their own, `datamart.RecordPeople`, until the next
  sync: not in the page-figure cache, so no cull, no *Empty the stored page figures* and no restart
  loses them; before the first sync after a release, from the latest 500 records of each dataset), the
  names the same record holds there (a name split over first and last name keys put together per person,
  "Layla" and "Abi Saab" too; a known name is also found with up to two middle names,
  "Karim Ahmad Nassar" for Karim Nassar, and where two names overlap), and the names written after a
  title, up to four after a Latin one (Mr, Mrs, Ms, Miss, Mx, Mister, Sir, Madam, Madame, Mme, Mlle,
  Monsieur, Eng, Engineer, Prof, Professor, Hajj, Hajja, Hajjeh, Hajji, Ustaz, Sayyed, Dr, Doctor,
  Sheikh, "Dr.Haddad", any capital letter, "Mr Élie Khoury", "Mr. Mohamad Ali Hassan Khalil", the
  particles el, bou, abi...) and up to three after an Arabic one (السيد, السيدة, الآنسة, الأستاذ, الحاج,
  المهندس, الدكتور, الشيخ, مدام, د., م. and أ., a first, a father's and a family name, "عبد الله" counted as
  one; also written "to Mr", "للسيد", "للسيدة", with ه for ة, "السيده", or before a slash or a colon,
  "السيد/ كريم"); an Arabic name stops before a word no name is (في, على, عن, مع, و, الذي, a few verbs
  such as وافق, and الرسمية) and at a punctuation mark, so "الحاجة إلى المياه" and the words after
  "المهندس المسؤول" stay; a title always starts a name of its own ("Mrs Layla Saab Mrs Hiba Haddad" is
  two names); "M.Sc" and "M. Ed" are degrees, and "M."
  after a figure or in a time, "USD 2.5 M. The", "9 A.M. Teachers", is not a title; the titles that are
  also words, Miss, Sir, Madame, Eng, Engineer, Prof, Professor, Hajj and Doctor, take a name without
  their full stop only before a word that is not a common word of a sentence or a programme's title, so
  "No Child Will Miss Out On Learning", "Eng Language Classes", "Prof Development Sessions",
  "Hajj Season Health Response" and "Doctor Visits" stay). Comments, notes and remarks never teach a
  name. Only a name after Sheikh, Dr or Doctor is ever kept, and only under a key that holds the name of
  a place or an organisation (Sheikh Taba under `location`, "Dr. Khaled Foundation" under
  `partner_name`, `assessment_partner_name`, `partner_name_ar` and in search examples), never in a text
  about one (`site_feedback`, `other_partners_involved`). A number written alone, or a JSON number, with
  a phone's shape is withheld unless its key holds an amount (amount, total, cost, budget, cash,
  contribution, amt, expenditure, value, funds, disbursed, reserved, liquidation, paid, price...);
  `2500000.00` and `1500000` stay. In field monitoring no key holds an amount (an answer's `value` or
  `answer_value` is what a monitor wrote; only PRP's `v`, `d` and `c` are numbers), and a sum, a group,
  a comparison or a search there leaves out what is withheld, as everywhere. In texts a phone needs a
  phone's prefix (the leading 0, 961 or (961), a mobile prefix 70, 71, 76, 78, 79 or 81, a Syrian
  mobile's 093 to 099, "0944 123 456", or "+" or "00" with a space, and nine digits at least; never
  the "00" of an amount's decimals, "1500.00 1200.00"), and a number with one is never taken for an
  amount, next to a currency or not, unless its last six digits are 0 ("LBP 70.000.000", "81 000 000");
  extensions ("03 123456/7"), pairs ("71 12 34 56"), spaced and en dashes ("03 - 123456",
  "03 – 123456"), numbers joined by a slash or a dash ("03123456/71123456"), a number right after "Tel."
  or "Mob." and Arabic-Indic digits ("٠٣١٢٣٤٥٦") are phones; "USD 1250000", "1 500 000 USD",
  "LBP 71,500,000", "+1 250 000 LBP", "+2023-2024", a reference such as "PR 0010123456" or a number
  padded with zeros such as "0900012345", a time range
  ("08.30-10.30") and, outside field monitoring, a figure with six decimals ("76.923077%") stay. Section
  and donor names are never taken for person names. A search, a `__contains` filter and an equality
  (`eq`, `ne`) look only at what Ask is sent: the database narrows first, then each record is checked,
  on the fields that can hold the text, as it would be sent (a place the text is written that no
  cleaning rule can reach, with no address sign, link, title, first word of a known name or of the
  record's own and fewer than seven digits around it, is taken as it is, without cleaning, so that a
  search of a common word over 19,000 PRP reports takes 2 to 3 seconds; else each value cleaned once, a
  long text only around the searched words; an equality on a single plain word is left to the
  database). At most 20,000 records are looked in for a search or a `__contains`, 100,000 for an
  equality such as a status of several words, "Sent back", counted before any text or equality narrows
  them (by the partner, the programme document, the dates and the filters on numbers, dates and single
  words): above that Ask is asked to add one of those, and whether it is asked never depends on what is
  withheld. A sum, a group's sum and a `__gt`/`__lt` on a number leave out the numbers Ask is not sent,
  and texts are compared with `__gt`/`__lt` only as whole dates (YYYY-MM-DD) in a field that holds
  nothing but dates in the records of the partner, programme document and dates asked (never judged on
  the records a search or another filter found). So nothing finds, counts or orders a record by a
  withheld name, part of one, an address, a phone or a person held at any depth; a question that names
  a person NeuroDB knows, or an e-mail address, finds no record of any dataset, and the answer says
  why. `etools_search` looks in the
  latest 1,000 records of each dataset and checks those with the text as they are sent, latest first,
  until 50 hold it: it then says "at least 50", else how many hold it ("at least" when the dataset has
  older records, which `etools_query` looks in too). What it says depends only on what is sent and on
  the dataset's size, never on how many records hold the text only where it is withheld. Partner staff
  contact lists are not synced at all. NeuroDB Watch's look-ups never use those four (below). Only
  signed-in users can ask, and the lookups can only read what any signed-in user can already see.
  Nothing is written back. Claude's Messages API keeps no conversation between requests: NeuroDB sends
  what each call needs. OpenAI requests are sent with `store=false`, so OpenAI does not keep the
  responses for later retrieval through the API. NeuroDB keeps its own question log. Each request a
  person asked for carries a keyed hash (HMAC-SHA-256 with the app's Django secret key) of the
  signed-in user's internal id, never a name or email address: as Claude's `metadata.user_id`
  (Anthropic may use it to help detect abuse) or OpenAI's `safety_identifier` (so OpenAI can flag
  misuse per user). Whether a provider may use the data sent through its API to train its models is set
  by its commercial terms: check Anthropic's (anthropic.com/legal) or OpenAI's (openai.com/policies)
  before switching on. How long each provider keeps requests (for example for abuse monitoring) is set
  by its terms too: check Anthropic's commercial terms and privacy policy (anthropic.com/legal) or
  OpenAI's API data terms (openai.com/policies) before switching on. Clear this with the data
  protection focal point before switching it on; outbound HTTPS to api.anthropic.com (Claude) or
  api.openai.com (OpenAI) must be allowed.
- **Cost and review**: every question is logged in Admin → Data and sync → AI questions with the
  lookups made, tokens used and time taken. Every AI feature on the key (Ask NeuroDB, the daily
  review, What's new, document summaries, periodic figures, country programme reading, NeuroDB
  Watch, Monitoring insights, the Help assistant, the document review, the KM Database assistant, AI
  settings' connection test)
  is also counted per day and model in one ledger, Admin → Data and sync → **AI use**, with the
  provider of each row and the prompt tokens read from and written to the cache, in tokens and in US
  dollars where the model's price is known: Claude's prices are built in (per million tokens on the
  default Claude model, `CLAUDE_MODEL`: $4 input, $0.20 read from the cache, $5 written to it, $20 output; check
  Anthropic's pricing page), OpenAI's come from `AI_PRICE_INPUT_PER_MTOK`, `AI_PRICE_CACHED_PER_MTOK`
  and `AI_PRICE_OUTPUT_PER_MTOK` when they are set. Claude Haiku 5.5 has two prices, chosen by the
  length of each call's whole prompt: $0.10 input and $0.50 output per million tokens up to 100,000
  prompt tokens, $0.50 and $2.50 above; a longer call (a long country programme document, say) is
  counted in a row of its own, its model followed by `:long`, at the higher price. The background
  features stop using AI first when all of them together pass 80% of `AI_DAILY_TOKEN_SOFT_CAP`
  (default 3,000,000 tokens a day), so questions keep being answered. Output tokens include the
  model's reasoning (OpenAI) or thinking (Claude) tokens, which are billed as output. A typical
  question makes 2-4 lookups. The instructions and tool definitions are sent first and unchanged on
  every request: on Claude NeuroDB marks that fixed part for caching, so a repeat reads it from the cache at $0.20 per million tokens on the
  default Claude model instead of $4 (writing the cache costs 1.25 times the input price; prompts under
  512 tokens are not cached); on OpenAI its automatic prompt caching (prompts of 1,024 tokens or more,
  no setup) bills that repeated part at the cheaper cached-input rate. Prices per model are on each
  provider's pricing page.
- **Refusals and failures**: when the model declines a question, it is logged as "Declined by the
  model" and the user is asked to rephrase. On Claude a declined answer arrives as a refusal, and
  refusal fallbacks are on: Anthropic runs the request again on another Claude model it chooses for the
  reason of the decline; the question log, AI use and the daily review, What's new and Watch notes
  (*Narrated by*, *Written by*) then name the model that answered, and the declined attempt's tokens stay
  with the model asked for. Untick *Retry a declined answer on another Claude model* in AI settings to
  switch them off; an Anthropic account that does not accept them gets every call without them for a day
  (or until AI settings are saved again), and Needs attention says so until the option is unticked. A
  refusal's partial text is never kept. On OpenAI there is no automatic retry on another model, and a question blocked by OpenAI's
  safety checks (error codes `invalid_prompt`, `bio_policy`, `cyber_policy`,
  `misalignment_policy_violation`) is logged the same way, with the code in the Error field; asking the
  same question again will not help. If every question fails, use Test the connection in AI settings,
  or open a failed question in the log: its Error field (and the app logs) shows the cause. On Claude:
  an invalid or revoked key (401), used-up credits (402 `billing_error`: buy credits, retrying does not
  help), rate limits (429 `rate_limit_error`), the service overloaded (529 `overloaded_error`) or an
  unknown model id (404). On OpenAI: an invalid or revoked key (401), used-up credits (429
  `insufficient_quota`: buy credits, retrying does not help), rate limits (429) or an unknown model id
  (404). A question still being answered shows as Failed with the Error "in progress" until it ends.
- **Background look-ups (NeuroDB Watch)**: every morning NeuroDB Watch runs this same loop on its
  own, with no person asking, to look into at most 3 open critical points (see NeuroDB Watch, *The
  daily look-ups*). It offers only 13 read-only tools of the 44 (`find_anything`, `entity_profile`,
  `connected`, `programme_details`, `partner_details`, `partner_reporting`, `pd_indicator_progress`,
  `funds_overview`, `assurance_overview`, `indicator_forecasts`, `whats_new`, `daily_review`,
  `data_freshness`), passes every result and every tool error through an allow-list (texts only
  under known fields; no people's names, free text, links, Makani centres or the daily review's data
  checks) before the model reads it, runs each tool in a read-only database transaction,
  stops after 4 rounds or 150 seconds and keeps an answer only when its figures are in what was
  looked up. It uses its own prompt cache key (OpenAI) or cached prompt (Claude), and no person
  identifier; its tokens count in the watch's own daily cap, never in anyone's hourly limit. A question
  asked on `/ask/` is sent exactly as before: same instructions, all tools, same cache key.
- **Azure OpenAI is a different service**: Microsoft's Azure OpenAI hosts OpenAI models inside an
  Azure subscription, with its own endpoint (`https://<resource>.openai.azure.com`), its own keys
  or Entra ID sign-in, and deployment names instead of model ids. A platform.openai.com key does
  not work there. Moving the assistant to Azure OpenAI (for example to keep the traffic in the
  Azure tenant) needs a small code change (the SDK's `AzureOpenAI` client) and different settings.
  NeuroDB calls Anthropic's own API: Claude through Amazon Bedrock, Google Vertex AI or Microsoft Foundry
  would need a code change too, and has no server-side refusal fallbacks there; Claude Platform on AWS
  (run by Anthropic) keeps them but also needs its own client.

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
   names and reference numbers). The search ranks passages as it always has, on that index turned into
   text and parsed again in English (the forms of a word, such as report, reports and reported, count
   together), ties the newest first; that parse is kept with each passage when it is indexed
   (`Chunk.rank_vector`, knowledge migration 0007), where it used to be built at every search. The
   search itself takes 30-50 ms for a common word over 3,300 passages (the x20 parity world), where it
   took 0.7-1.8 s. The migration adds the column empty, so a deployment starts as fast as before
   whatever the size of the knowledge base; the passages indexed before it are given their parse by the
   *Knowledge hub* job, which does that first (after every sync, every morning, or from *Run a job* →
   *Knowledge hub*; its output says how many passages it filled). Until then the search builds the same
   parse for them: the same results and ranks, at the old speed. So `/knowledge/?q=` stays one request (it is not a page in parts). The old search
   parsed with the database server's default text search configuration (`default_text_search_config`:
   English on every database checked here; the production server's value is among its server
   parameters in the Azure portal); the column names English.
3. **Links**: the partners (full, short or alternate name, vendor number), programme documents
   (reference number, with or without the amendment suffix), sections, governorates and districts
   named in the text are linked. Matching is literal and whole-word; "North" and "South" count
   only as "North Lebanon" / "South governorate". The documents then show on the partner's and the
   programme document's pages, and `partner_details` / `programme_details` give them to the AI.
4. **Summary** (when the AI assistant is configured): the first 120,000 characters go to the AI
   provider in use (Claude, or OpenAI with `store=false`) for a short summary, key points, the
   document's date and the organisations, places and reference numbers it names; those names become *AI-suggested* links when they match a
   NeuroDB record. If this step fails, the document is still searchable and says so.

The assistant has two tools for it: `search_knowledge` (best passages for some words, optionally
only in the documents linked to a partner, programme document, section or year) and
`read_knowledge` (a document's summary, links and text in parts of 8,000 characters). It quotes
and links the documents it uses. Their text is sent to the AI provider in use as part of the answer; the
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
  are listed by the AI (the provider in use; OpenAI with `store=false`) from the pages laid out as on
  paper. A figure is kept only when its number is printed on the page it is said to come from; the others are counted
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

### Document review (FMS §9 "Other Reports"): findings with evidence

The document review reads chosen knowledge base documents (annual reports, donor reports, evaluations,
sector reviews, workplans) with the AI and keeps what they say as **findings** (challenges,
recommendations, observations, action points), **key statements** and **action points**, each pointing
to the page it comes from, for people to accept or reject. It is opt-in per document, so the AI's cost
stays with what was chosen. The code is in `neurodb/knowledge/review.py` (the job),
`review_locate.py` (the work without AI) and `review_prompts.py` (the shipped prompts); the page
(`/knowledge/review/`, below) in `review_views.py`, what it counts in `review_data.py`, the Word desk review
in `review_docx.py` and the theme paragraph in `review_paragraph.py`.

**Turning it on.** Admin → Library and maps → **Document review settings** (Administrators only):
tick *Enabled*. It is off when NeuroDB is deployed: until then nothing is analysed and the nightly
run finishes *skipped*. It also needs the AI assistant (`AI_ASSISTANT_ENABLED`, and an Anthropic or
OpenAI key, set by the hosting team or entered in AI settings).

**Batches.** Only documents put in a **review batch** are analysed: a folder for one kind of document
(the review page's Documents tab, or Admin → **Document review batches**). Put a knowledge base document
in a batch from the Documents tab (*Upload documents into this batch*, or *Or pick knowledge base
documents*) or on its admin page (*Review batch*): it waits to be analysed. Taking it out stops
its analysis and leaves what was found out of every view. A document marked *reference only* stays in
its batch and is never analysed; an archived batch is no longer analysed. Admin → Knowledge documents →
action *Analyse in the document review* analyses the chosen documents now, in the background.

**How a document is analysed** (each stage noted *yes*, *partly* or *failed*, with the reason, in the
document's review notes):

1. **Text**: the text the knowledge base read (PDF pages, slides, sheets); a document still being read
   waits for the next run, one whose text could not be read fails.
2. **Findings**: the text in parts of about 12,000 characters (*Chunk size*), cut between pages, each
   page marked (`[Page 4]`, `[Slide 2]`, `[Sheet 'Budget']`); one call per part, its answer held to a
   strict JSON schema: category, a tag from the topic list, 1–3 sentences, the verbatim quote, the place
   and date as written, and whether the document reports it or the AI interpreted it (at most *Max
   findings per chunk*). A tag not in the list becomes **Other**. A part whose answer is broken (not
   JSON, cut off, or the call failed) is asked again once as two halves; what still fails is left out
   and the document is **partly analysed** ("Only 80% read"). Nothing usable at all: **failed**, and
   what an earlier analysis found stays.
3. **Locate** (no AI): each quote is looked for in the whole text, compared as words (case, accents,
   punctuation and line breaks ignored; a quote shortened with "…" by its longest piece), giving the
   exact page ("p. 12", "slide 4", "sheet 'Budget'"); not found, the pages of the part it came from. The
   place is matched to NeuroDB's governorates and districts (the maps' areas, whatever the spelling;
   "Lebanon" or "national" is country-wide); a place not recognised is kept as written and marked *Not
   recognised*. The date is read as a period ("Q3 2025", "H1 2024", "March 2025", "2024-2025").
4. **Key statements**: one call over the findings (numbered): up to *Statements per document* (0: 20;
   fewer for a short document, never under 5), each citing the findings it rests on, with an urgency
   from 0 to 100.
5. **Enrichment**: one call: at most 15 action points, explicit commitments only, each with its owner
   as written (*Unassigned* when not said; names, e-mail addresses and phone numbers removed), deadline
   (a quarter or a year counts to its last day) and priority, citing findings. An action point that no
   *action point* finding states is marked **derived**, and a derived finding (category action point,
   on the quote of the finding it was drawn from) records it.

The **evidence score** (0–100) is computed by NeuroDB, never taken from the model: quote found in the
text 45, reported rather than interpreted 25, dated 10, placed 10, tagged (not Other) 10.

**A new analysis** replaces what the AI wrote but keeps: the verdicts (and a person's edits) of
findings whose AI text and quote are unchanged, the findings people added, the verdicts of statements
with the same words, and the status of each action point (matched on its words, normalised); people
set the status, an analysis never changes it. Rejected findings are not read by the summary or the
enrichment. A document read again by the knowledge base with a new text (also while it is being
analysed) waits to be analysed again.

**Jobs** (Admin → Import and sync runs → Run a job; each run is recorded as *Document review*):

- *Review documents (pending)*: the documents waiting, failed or partly analysed. This is the
  scheduled job `doc-review`, nightly at 04:40, before the morning's other AI jobs.
- *Review documents (full)*: every document of the batches again, as after a change to the prompts or
  the topics; it costs as much as the first analysis.
- *Locate document findings*: the pages, places, dates and evidence again, without AI (after the
  governorates or districts changed); seconds.

One run at a time (an advisory lock): a second run started while one is going does nothing; a single
document's analysis (its *Analyse* button, or the admin action for one document) waits for it. A
document with no progress for 30 minutes (a restart in the middle) is marked failed with the reason at
the next run. The run's details give the documents analysed, partly analysed, failed and waiting, the
calls, the tokens, the parts read again as halves, and why it stopped.

**Costs and limits.** Every call is counted under *Document review* in *AI use*
(`assistant.usage` feature `doc_review`). Before each call NeuroDB estimates its tokens and stops the
run cleanly, leaving the document as it was for the next night, when: the review's own daily cap would
be passed (*Daily token cap* in the settings; 0 = `DOC_REVIEW_DAILY_TOKEN_CAP`, default 1,000,000),
the shared `AI_DAILY_TOKEN_SOFT_CAP` would pass 80% (a background job leaves the rest to people), or
the AI service's credit pause of Monitoring insights is on (a call that meets the credit's end starts
the pause). A part of 12,000 characters is about 4,000 tokens sent and 1,000–3,000 written; a 100-page
report takes roughly 150,000 tokens, so the default cap reads about six such reports a night. A
document that needs more than a whole day's budget (the smaller of the review's cap and 80% of the
shared cap) is not started, as it could never finish: it fails with the reason, and the next nightly
run tries it again once the cap is raised or the document is split. A full run stopped by the budget
leaves the documents it did not reach waiting, so the nightly run goes on with them. The
model is the one of AI settings (`AI_ASSISTANT_MODEL` on OpenAI) at low effort, `store=false` (OpenAI;
Claude keeps no conversation either), 180 seconds and one retry per call.

**What goes to the AI provider in use**: the document's title and text (the part being read), the topic list, and for
the statements and action points the findings' texts. The prompts tell the model that the document is
material, never instructions, and never to write a person's name, e-mail address or phone number; owners
are cleaned of them again before they are kept. Do not put personal data of children, beneficiaries or
staff in a reviewed document.

**Settings** (Admin → Document review settings, Administrators only): the switch; the three prompts
(*findings*, *key statements*, *action points*), each with **Restore the shipped findings prompt** (and
the like) to put NeuroDB's text back, and *Prompts in use* saying which are changed. A prompt must keep
its JSON sentence (`Reply with the JSON object {"findings": [...]} and nothing else.` and the like): a
save without it is refused, as the stage would find nothing. NeuroDB's own rules are sent after every
prompt and cannot be changed. The sizes (chunk size 2,000–60,000 characters, findings per part,
statements per document) and the daily token cap, with today's use. A change applies to the documents
analysed afterwards.

**Topics** (Admin → Topic programmes, Topic subtopics, Topics): three levels, programme → subtopic →
tag, shipped with UNICEF Lebanon's programmes (Education, Child Protection, Health & Nutrition, WASH,
Social Protection & Inclusion, Adolescents & Youth, Gender, Emergency/Humanitarian response,
Partnerships & Funding, Monitoring & Data), a few subtopics and tags each, and **Other**. Rename, reorder,
add or switch off any; a topic switched off is no longer offered to the AI and findings keep it. A topic
that findings use cannot be deleted (switch it off). "Other" is made again if removed.

**What was found** is listed read-only in the admin (Document findings, Document statements, Document
action points, Theme paragraphs; an Administrator may delete a row); people review them on the review
page.

**Ask NeuroDB** has the tool `search_document_findings` (words, optionally a batch's name or id): the
findings of the documents still in a batch, never the rejected ones, each with its document, page,
category, topic, quote, evidence and verdict and a link to the document's file at that page
(`/knowledge/<id>/file/#page=n`, for a PDF) or to the document's findings on the review page. Its
answers cite them as "(Document title, p. n)".

**The review page** (`/knowledge/review/`, full menu → Resources → *Document review*). Every signed-in
user reads it (Viewers read only; donor accounts never reach it). Administrators and Section editors
(`knowledge.access.can_add`) create, rename and archive batches, add documents, start an analysis, mark a
document as reference or take it out, accept or reject findings and statements, edit, add or delete a
finding and set an action point's status; every one of these is a POST refused (403) to anyone else.
The prompts and settings stay in the admin, for Administrators. Its tabs:

- **Documents**: the batches and, per document, the five stage chips (green yes, amber partly, red
  failed, grey not reached; the note on hover), counts, "only n% read", and *Analyse / Re-analyse* (the
  `review_documents --document <id>` run in the background, as the admin action; refused with the reason
  when the review or the AI is off, the document is a reference or its batch is archived), *Mark as
  reference / Unmark reference*, *Remove from batch*. Opening the tab also marks failed a document with no
  progress for 30 minutes; while documents wait or are analysed the list refreshes every 20 seconds.
  *Upload documents into this batch* opens the knowledge base's Add page, which puts the new documents in
  the batch.
- **Findings**: *All findings*, *By document* (key statements, findings, *Accept all* / *Reject all* —
  only what is not reviewed yet — and *Add a finding*: a person's finding is accepted, located like the
  AI's, keeps the page given when its quote is not found, and is kept by every new analysis), *Key
  statements* and the *Index*; filters and a CSV of each view (texts starting with `=`, `+`, `-` or `@`
  are written as text). An edit is located again (page, place, date, evidence) and kept by a new analysis
  with its verdict. Deleting an AI finding does not stop a new analysis from finding it again: reject it.
  Links to a page open the PDF at that page: the knowledge base now serves PDF files to open in the
  browser (other files still download).
- **Dashboard**: five tiles, six quality tiles (each opens its gap), six charts and the table by batch;
  every figure opens the rows it counts (`counted=1`: the same query), so the two always agree.
- **Synthesis**: themes ranked by distinct documents (Other apart), Over time, Coverage and Repeated
  findings (Python, no AI: at most the 3,000 best-evidenced findings compared, 50 groups kept).
  **Write a paragraph** is the only AI call: one call (the model of AI settings, low effort,
  `store=false` on OpenAI; Claude keeps no conversation either) on that theme's findings only (at most
  60, cleaned of names, e-mail addresses, links and phone numbers), cited back as "(Document title, p. n)"; a citation that is not one of them is dropped,
  and an answer citing none is not shown. Each person may write **50 a day** (the chip "n of 50 today";
  refused requests do not count); it is recorded under *Document review* in AI use, counts against the
  review's daily cap and 100% of `AI_DAILY_TOKEN_SOFT_CAP`, and stops when the AI service's credit pause
  is on.
  Each request is kept in Admin → Theme paragraphs (who, when, what, tokens).
- **Actions**: cards, open by owner, the table with its filters (*Current*: documents dated within a year
  of the newest document of the collection — their date, issue date or year, else when added; *All
  time*), the status set on the row, CSV and Excel of the filter.
- **Report**: *Download desk review (.docx)* (`desk-review-YYYY-MM-DD.docx`), built when clicked from the
  same figures, without AI, for the chosen batch and minimum of documents.

**On a large review** (measured on the x20 parity world: 126 documents in 5 batches, 3,270 findings,
654 statements, 654 action points) three views **load in parts** (see *Pages that load in parts*): *By
document* (0.45-0.85 s, most of it drawing ten documents of thirty findings each), *Themes* (0.35-0.47 s)
and *Repeated findings* (5-5.7 s: up to 3,000 findings compared word by word). Their frame comes at once
and their figures in one part, `findings` or `synthesis`. The repeated findings are kept in the shared page
figures for an hour, per batch and *Verified only*, under a key that every change to the review moves, so
they are worked out once for everyone (`review_data.repeated_groups`). The Documents tab counts each
document's findings, statements and action points by subqueries (about 20 ms; one count over all three
joined took 0.4-0.5 s) and the themes read their best findings in one query (one per theme before), so the
Documents tab (0.13-0.2 s), the Index (0.1 s) and the other tabs and views stay one request. The
Documents tab's 20-second refresh is unchanged.

**Rejected and verified.** A rejected finding or statement leaves every count, chart, synthesis and the
desk review (the Findings tab keeps listing it, struck through). **Verified only** (the switch at the top
of the page, shown once a verdict exists; each person's, kept in their session) restricts the Dashboard,
Synthesis, Actions and the desk review to what was accepted; an action point counts when a finding it
cites was accepted.

**Not built in this release**: the weekly document digest (no e-mails in this step), the figures
discrepancy check between documents and NeuroDB's data, snapshots of the collection and their "Changes"
view, the Power BI package of the review, and a separate "Ask the documents" tab (Ask NeuroDB's
`search_document_findings` covers it).

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
| Field monitoring visits | Monitoring insights (visits dated, by their start date else their end date, in the last `FMM_HUB_MONTHS`, 24, months) | the PDs, partners and country programme outputs they are about, their sections, governorate and district |

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
critical review finding that appears or goes, or a finding whose severity changed. A field
monitoring visit is news when it comes in rated off track or constrained and dated (its start date, else its
end date) in the last `FMM_NEWS_DAYS` (30) days, or when its rating changes later; its quality, urgency and status moving
are kept, not told, and the first build that brings visits into the hub tells none of them (so the
24 months of visits added at once are not news). A finding growing
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

- **The What's new page** (in the full menu; every signed-in user opens it, donor accounts cannot): the last 24 hours,
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
  (`store=false` on OpenAI, and Claude keeps no conversation either; names and figures as NeuroDB holds
  them, no personal data); otherwise, or if it
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
of the Data health page (its last block, the part `readiness`, loaded after the others: see *Data
health loads in parts*). Counts only: nothing is predicted and nothing leaves NeuroDB.

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

For each ActivityInfo master indicator of this year's ActivityInfo reporting year (the one whose records
count for this calendar year) that adds up month by month (aggregation SUM; averages, maximums and ratios
are not forecast), where it will likely stand in December, with a range, against its target. Forecasts are
made only while ActivityInfo is in use (see *ActivityInfo: in use or archive*). The method is in `neurodb/insights/forecast.py`; it runs
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

**Where it shows**: the *Year-end forecasts* page (in the ActivityInfo group at the bottom of the
full menu, under *ActivityInfo databases*, once forecasts were made; the user's section first; filter by
section and status), a *Likely to fall short by December* card on the Country overview and on this year's
database dashboards (this year only, while ActivityInfo is in use; the overview's for the chosen sections),
and Ask NeuroDB
(`indicator_forecasts`), which is told to say they are estimates and to give the range.

**Running it**: the job `forecast` (`forecast_indicators`, Mondays 07:15; *Run a job → Year-end
forecast*). Each run replaces the forecasts; the back-test is in the run's details (admin → Import
and sync runs). Reading every year's monthly sums takes a few minutes on the production data.
**Once ActivityInfo is not in use** (an archive, or this year has no ActivityInfo reporting year with
records) the job still runs and succeeds, but skips: its run says `skipped: ActivityInfo is not in use in
<year>` and the last forecasts are kept as they were. The page and the run that pages read (the latest run
that forecast, never a skipped one) then say "These forecasts were made for <year> while ActivityInfo was
in use. They are no longer updated."; Ask's `indicator_forecasts` adds the same note, and Watch closes its
forecast points ("ActivityInfo is archived: its forecasts are no longer followed").

## KM Database (`/km/`, admin → KM Database)

The KM Database shows one evidence workbook, findings from research, evaluations and assessments with the
documents they come from, on its own page with three tabs: **Findings** (the default), **Documents** and
**Ask the KM Database** (its own assistant), with one search box, the filters, a page for each finding (also
a quick view from each row's **Open**) and each document, and **Excel / CSV of the filter**. The code is in
`neurodb/km`.

**What keeps it apart.** It has its own app and tables (`km_version`, `km_document`, `km_finding`,
`km_organisation`, `km_question`); their only relations are to each other and to the user who uploaded a
version, added a correction or asked a question. Nothing else in NeuroDB imports
`neurodb.km` or reads its tables: it is linked to no partner, programme document, indicator or library
resource, and Ask NeuroDB, the knowledge base, the library, NeuroDB Watch, the daily review, Data health
and the site-wide search do not read it (the search's *Pages* group lists the page itself, like every
page of the full menu). Its load is not an import run (`SyncRun`), so it is on no run list, freshness
check or Data health line. Its search uses its own search indexes only. A Help question asked from a KM
page is stored and sent as the page `/km/` titled "KM Database", never with a Finding ID or a workbook
title. The page is under *Resources* in the full menu (see the menu section above); every signed-in UNICEF
role opens its address (its finding and document pages and its downloads too); donor accounts are sent to
their page.

**Uploading a new version** (superusers, and Administrators with staff status):

1. Admin → *KM Database* → *Versions* → **Upload a new version** (or *Upload the KM Database* in the admin
   home's *Quick actions*).
2. Choose the workbook (.xlsx, 25 MB at most) and press **Upload**. *Download the Excel template* on the
   same page gives every column the check expects (required ones in bold) and the Read Me labels it
   reads.
3. The quick check runs at once. If it finds a problem, the page lists it and nothing is saved. If it
   passes, the version's page opens; it refreshes by itself every 5 seconds while the version waits and
   loads.
4. Wait for the status **Shown now** (about a minute; the version page shows the time it took), then open
   the KM Database page.

The same file as the version shown is refused unless *Load it even if it is the version shown now* is
ticked. An upload while another version waits or loads is refused, with a link to that version.

**What the check refuses** (nothing is loaded and the version shown stays; each message names the column
as the workbook writes it and the sheet as the file names it, with the row numbers Excel shows: the header
is row 1):

- in the request: a file over 25 MB; a file that is not .xlsx (named by its ending, e.g. .csv); a protected
  workbook (a password, or a sensitivity label that encrypts it: save a copy without the protection); a
  file that is not a workbook (or a zip of over 500 parts or 300 MB unpacked); no sheet whose name ends
  with "Documents" or "Findings", or two of them; a required column missing (findings: Finding ID, Record
  ID, Document Title, Pillar, Research Question, Key Finding, Evidence Confidence Level, Data Quality
  Status; documents: Record ID, Document Title, Data Quality Status); the column names in another row
  than the first; a column twice; a sheet with no rows;
- by the load: rows with no Finding ID or Record ID, an ID repeated, an ID with characters other than
  letters, digits, ".", "_" and "-" (at most 40), a finding whose Record ID is not in the documents sheet
  (each kind once, with its count and at most five example rows); more than 5,000 documents or 100,000
  findings;
- **the fewer-rows guard**: a workbook with under 90% of the findings or documents of the version shown,
  or fewer rows than its Read Me sheet states, is refused unless the uploader ticks *It has fewer findings
  on purpose* and uploads it again.

An optional column that is missing loads empty, with a note on the version's page; columns the KM
Database does not know are named and not read; without a Read Me sheet the prepared-on date is not known.

**What the load does** (`load_km_database --version-id <pk>`, started in the background by the upload;
one at a time, advisory lock `7140442`):

- pass 1 reads every row once and checks it, with no database access; pass 2 reads the findings again
  and writes them 1,000 at a time; one transaction replaces every document and finding (the old rows stay
  visible until it commits), fills the search indexes and makes the version live. The version it
  replaces keeps its summary only;
- a failed load, or one that stops (the container restarted), leaves the version shown as it was; a
  version left *Loading* while no load holds the lock, or *Waiting to load* for over 2 minutes, is marked
  *Not loaded* at the next view of the versions or of the KM Database page (so the page stops saying a new
  version is loading), with what to do;
- the uploaded file is kept only until its load ends (loaded or not); of the Read Me's source workbook
  only the file name is kept, never its folder;
- measured on the user's workbook (8 MB: about 540 documents and 15,000 findings): about 30 seconds and
  a peak of about 230 MB of memory for the whole process into an empty KM Database, about 35 seconds and
  280 MB when it replaces a version of the same size. The version page shows both for each load.

**Values to check and rows marked for review.** A value that is not one of the expected ones (a
confidence level other than Level A to D, a mark other than Yes, No, Not reported or Not applicable, a
year that is not a year, a number of findings over 2,147,483,647, an analytical domain or research
question whose number is too long for its code (over 8 characters with its "D" or "RQ"), a value of 100
characters or more in a short column) is kept as the workbook gives it and marked *Check this value* on
the page, so one such cell never stops the load; a row whose Data Quality Status does not start with
"Ready" is marked *Marked for review*. Neither is hidden. The version page's notes count them, name up to
20 IDs and link the page's list of them (`/km/?quality=review`).

**The confidence levels** are shown in one colour, as the workbook does not say which level is the
strongest; `neurodb.km.columns.CONFIDENCE_A_STRONGEST` turns on the colours once that is confirmed.

**The page** (`/km/`, `/km/documents/`, `/km/findings/<Finding ID>/`, `/km/documents/<Document ID>/`):

- **Search**: PostgreSQL full text over the KM tables' own indexes (filled by the load): every word must be
  found (English stems and the words as written); UK/US spellings and common sector acronyms are joined
  (`neurodb.km.search.VARIANTS`: labour/labor, GBV/gender-based violence, OOSC/out of school...); quotes,
  `-word` and `or` use the web search syntax; an exact Finding ID or Document ID shows those rows; when no
  word matches, the letters typed are looked for (and the page says so). The authors are searched on screen
  (as letters), never in the indexes.
- **Filters**: pillar, research question (keyed by its pillar and wording, so a link survives a new version
  whose wording is unchanged; a question of another pillar than the pillars chosen is left out), confidence
  level, year, document type, document, and under *More filters* the analytical domain, the age, sex,
  social-ecological and theme marks (a finding counts when marked Yes), the evidence origin and the data
  quality. Values the version does not hold are ignored. The choices are read once per version (kept a
  day under the version's own cache key).
- **Sorting** (the column titles), **Rows a page** (25, 50 or 100). The sort and the page size are links that
  load the page; a filter or a search reloads the table only.
- A finding or document the version shown does not hold answers 404 with "not in the version shown now"
  (an older link); `?v=<version>` on a finding's address says when that version is not the one shown.
- Measured on the user's workbook (about 540 documents, 15,000 findings, in a throwaway database): a page
  of the table, a filter, the documents tab, a finding or a document page 15-100 ms of server time, a
  search 25-300 ms (the slowest: a word found nowhere, which then looks for the letters); Excel of every
  finding about 6 seconds (9 MB), CSV about 5 seconds (50 MB), the documents' Excel well under a second.
  The pages read the same number of queries whatever the number of rows (tested).

**Downloads and the author rule** (the user's decision: authors on screen only):

- *Excel of the filter* and *CSV of the filter* on both tabs (`?export=xlsx|csv`), with every column
  (snake_case names, the marks as Yes / No / Not reported / Not applicable). The Excel file has an *About*
  sheet: prepared on, loaded on, the filter in words, the rows and the rules. The Excel file holds at most
  20,000 findings (`neurodb.km.exports.XLSX_MAX`, a sentence otherwise); the CSV has no limit. The
  browser's own *Copy* and *CSV* buttons are off on the KM tables.
- `publishing_organisations` replaces the authors: the organisations an entry names ("UNICEF; WHO"),
  "Authors: N", or both ("UNICEF + 2 authors"). The rule (`neurodb.km.authors`) cuts an entry into pieces
  (";", commas, parentheses, "by"; and a slash, a spaced dash, a colon, "with", the end of a sentence,
  "and" when a person stands in the piece: "Fakeorg Team: Jane Doe", "author Jane Doe. Published in ...")
  and takes off the words before a name ("consultant", "led by"). It classes each piece: words of an
  organisation, acronyms and places make organisations (not a name such as "Jane Cross"); initials,
  "Surname, X.", a name in capitals or capitalised words that could be a name make a person; anything else
  (a topic in lower case) is counted as one author, never named; "Not reported" and the like give an empty
  cell. When unsure it counts a person. An organisation string that still holds a KM author's name, a
  citation, a name after a title or a name after "by" or "author" is counted as an author instead.
- **Every text column** goes through `neurodb.km.privacy.for_download` (only the Finding ID and Document ID
  are written as they are): e-mail addresses, citation names ("Doe et al.", "(Doe, 2019)", "Roe (2018)",
  "(Doe 2010, Roe 2020)", "(Doe 2002 methodology)", "Doe and Roe 2020", "in Doe 2016"; then the same name
  elsewhere in that text),
  interview speakers ("– Fakename, 34", "Fakename, 11, was", "Fakename (20, male)", "Fakename, a boy"),
  reference lists ("Doe, J.", "Roe Poe, M. C."; "Doe J" for a KM author), "Doe 2020:" and "Doe 2020
  (Lebanon)", a KM author's surname in "(Doe, Lebanon)" or "studies by Doe, Roe and Poe" (with the other
  names of that list), a name with an office ("Minister Jane Doe", "Jane Doe, Director of"), a KM author's
  surname after a name withheld and a first name before one, the document's own authors by surname, the
  KM authors' full names and names after a title are written "[name withheld]". Words that start a sentence
  ("However, 2021") are never taken for a name. Months, places, organisations, survey and plan
  names, and acronyms are kept; links and numbers are kept. NeuroDB's own list of people (Watch's) is never
  used and never learns these names.
- The search box's words are written in the About sheet only when none of them names a KM author.
- A printout follows the download rule: a finding or document page prints "Publishing organisations" (the
  download's value), never the authors (screen only), and every text the download writes otherwise (a name
  withheld: the key finding, the lines, the titles, the types in the tables) is printed as the download
  writes it, while the screen shows it as the workbook gives it (`km/partials/_printed.html`).
- Under a filter or a search, the Documents tab's *Findings* counts the findings of the filter ("2", "of
  3") and links to the Findings tab with the same filter.
- Measured on the user's workbook: about 140 documents name organisations only, 305 list only authors,
  75 mix both, 16 give none; the whole CSV holds no KM author's name form in `publishing_organisations`,
  and the final check finds no name in any cell (only links and numbers, kept by design). An independent
  count of capitalised words before a year ("Name 2019") left in the texts the assistant will read, not
  kept by the rule, fell from 435 cells to 88, nearly all ordinary words ("Total 2019", "Target 2025");
  about a dozen cells still name 4 people in shapes with no year by the name ("(Doe, Lebanon)") or at the
  start of a clause ("; Doe 2020 (Lebanon) relates").

**Organisations named in downloads** (admin → *KM Database*, superusers and Administrators): corrections
of the rule, written as the workbook writes the entry, each of a kind: *An organisation* (named), *A
person* (counted, never named; its name is then withheld in the texts too) or *Not a name or an
organisation* (left out of downloads, kept in the texts). They change downloads at once (the people are
read again when the list changes, with no restart). The **page of the version shown** lists how downloads
show the documents' authors, **Named as organisations in downloads** (every string named, each with *Count
as a person* and *Not an organisation (leave it out)*: a POST, so the name never travels in an address) and
**Look like words, not names** (author entries also found as a phrase in the texts of more than 20
findings, or whose surname the findings write in lower case 5 times or more, as an ordinary word; each with
*Not a name* and *Count as an organisation*; the load warns about them: they stay withheld until marked).
The load keeps those surnames on the version (`ordinary_words`): a question keeps such a word when it is
written in lower case, and keeps the word of an author entry of one bare word (no first name, no initial)
when it is not written as a name ("Fakeword's", "by Fakeword", "Fakeword 2020" are withheld). The people of
a version are read again whenever its rows change (the newest document's key is read at each use), so a
worker never serves a new version's rows with the previous version's authors; each document's own authors
are kept by its Document ID. The documents list shows **Shown in downloads as**. A replaced version keeps
its downloads summary.

**Ask the KM Database** (`/km/ask/`, the page's third tab; the code is `neurodb/km/ask.py`): a question
answered from the KM Database's own findings only, by **one model call with no tools** (never Ask
NeuroDB's run, prompt, tools, date or year lines). In order:

1. The question (1-1,000 characters) is cleaned for sending and keeping: the names NeuroDB knows (the Help
   assistant's rule; that list is only read to take names out), e-mail addresses, links and phone numbers,
   then the KM Database's authors (full names and surnames, in capitals or not: "what did doe find"),
   citations and names after a title. The typed text is searched, never kept or sent. The questions sent
   (and earlier questions of the conversation) are checked again for a surname in any case.
2. The filters it reads within are those of the tab's address (the Findings tab's filter, carried by the
   tab link, or *Ask about its document*): known keys only, never the search box's words, the sort or the
   page size.
3. **Retrieval**, over the KM tables only: codes in the question ("Level A", "P2", "RQ3", a Document ID,
   with a dash or of the shape of the version's own Document IDs, "PM0123") narrow the filters (a code that
   would leave nothing is left out and the answer says so); a Finding ID is read first (also before a full
   stop); the whole question's words (up to 1,000 characters, common words left out first, words joined by
   a slash searched one by one, a hyphenated word also as two words) (UK/US spellings and sector acronyms joined, common words left out)
   are looked for in each finding's own words (`own_vector`: key finding, measure, analysis,
   limitations, the sub-questions and indicator; never the document's methods, population or titles,
   which only break ties). A finding is kept when it matches 2 of 3 or more word groups, else all of them;
   the most groups matched come first, then the findings marked Yes for an age group or girls and boys
   the question names, then the rank. At most 40 findings, 6 a document (no cap when the filters or the
   question name one document), 60,000 characters. A follow-up of under 4 words keeps the earlier
   question's words and reads the previous answer's cited findings first; when no finding matches it
   together with the earlier topic, its own words are searched alone (a new topic). In a long question (more
   than 8 word groups), the findings that match its last clause (after the last sentence or a comma before
   "could", "what", "please"...) come first. When fewer than 40 findings
   match (or the question is not in Latin letters) and less than 15 seconds have passed, a small call
   (20 seconds, no retry) asks for search words from the cleaned question and the conversation's earlier
   questions only, never any data; it fails silently. When it finds the credit gone (the pause starts), no
   answer call is made: the findings are listed with the pause's sentence.
4. **The message**: the question, up to 4 earlier turns of this conversation read from the version shown
   (answers cut at 1,500 characters, citations as plain [ID]), the filters in words, a legend of the
   pillars and research questions, and the findings read, grouped by document, every text and attribute
   (the year as given too) through `privacy.for_ai` and escaped (`&lt;5 years`); a document type chosen as
   a filter is cleaned as a question is. Each finding carries its Finding ID, its legend key, its
   confidence level as "Level B" or "not given", "ready" or "marked for review", and the age and sex marks
   marked Yes. The final check (`privacy.leftover`) leaves out a document whose block still names someone
   (counted in the question's error) and sends nothing when the whole message does.
5. **The call**: the model of AI settings at low effort, 5,000 output tokens, held to the time left of 95
   seconds, no retry, structured JSON (`answer`, `covered`: yes / partly / no), `store=false` (OpenAI;
   Claude keeps nothing either), a keyed hash of the person.
6. **The answer checked**: names withheld again (an answer that still names someone is kept back and the
   findings listed instead); the model's own links taken out; every Finding ID read linked to its finding
   with the version it was read from (`/km/findings/<ID>/?v=<version>`), every other reference of the
   shape of the version's Finding IDs or Document IDs (invented, a Document ID, not read this time) taken
   out and counted, with as many digits as the IDs hold ("PM2.5" and "PM10" stay); the sentences citing no
   finding counted. The rendered answer keeps only links to the findings read.

| Sent to the AI provider | Never sent |
|---|---|
| The question, cleaned; up to 4 earlier questions and answers of this conversation and version; the filters in words (codes, years, types, "one document"); the legend of pillar and research question labels | Publishing entity / authors (either sheet), people's names in texts, e-mail addresses, links, phones; the search box's words |
| Of the findings read (about 30-35, at most 40): Finding ID, legend key, confidence letter, marked for review or not, the age and sex marks marked Yes, ages, measure, key finding, analysis, limitations; of their documents: title, year, type, population, methods | Document IDs (one typed in a question, or any word of their shape, goes as "this document" or "a document", in the question, the earlier questions and the search-words call; the log keeps the question as typed, cleaned of names), short titles, sub-questions, indicators, evidence origin, values to check, the data quality text |
| The number of matching findings | Anything from another NeuroDB table: partners, programme documents, indicators, the knowledge base, the library, users, Ask NeuroDB's prompt and its date and year lines |
| The two fixed prompts (`ask.KM_PROMPT`, `ask.TERMS_PROMPT`) | The workbook, its Read Me, the version's file name |

- **Limits**: `KM_PER_USER_PER_DAY` (30) questions a day per person from local midnight: one being
  answered and one that made a call count (a failed one with tokens too, and one with no matching finding
  once the search-words call ran); one over a limit, with nothing to search (only common words) or kept
  back before a call does not; declined questions are free up to the same number.
  At most 2 at once per person (429). The KM Database's share of the day, `KM_DAILY_TOKEN_CAP` (600,000
  tokens, about 25-30 questions: a question asks for 25,000 before it starts), so that a few heavy users
  cannot use up the shared `AI_DAILY_TOKEN_SOFT_CAP` (3,000,000) for Ask NeuroDB, Help and Monitoring
  insights or stop Watch at 80%; when more roles are ticked, raise both together. The shared budget at
  100% and the credit pause of Monitoring insights apply too (a call that finds the credit gone starts it).
- **When no answer is written** (switched off, the AI resting, the KM share or the shared budget used,
  the person's quota) the tab lists the findings that match, with the sentence, and makes no call; so
  does a failing, refused or cut-off call. Nothing is ever a 500.
- **Switching it off**: `KM_ASK_ENABLED=false` → the tab lists the matching findings without an answer;
  the AI off in AI settings (or by the hosting team) → every assistant, this one included; the tab still
  lists findings. The confidence rule of the prompt follows `neurodb.km.columns.CONFIDENCE_A_STRONGEST`.
- **Cost**: one question is about 20,000-25,000 tokens (findings, history, up to 5,000 out, the search
  words). Every call is counted under *KM Database assistant* (`km`) in *AI use*.
- **The log**: Admin → *KM Database* → **Questions to the KM Database** (read only, superusers and
  Administrators): the cleaned question, status, covered, findings read and cited, sentences citing none,
  tokens and time. Its own rows (`km_question`), never Ask NeuroDB's or the Help assistant's; kept 90
  days (each question deletes the older ones; no job).
- **Donors** get the JSON 403 on `/km/ask/stream/` like the other assistants' streams.
- Measured on the user's workbook (throwaway database, about 15,000 findings): for 20 typical questions the
  search kept 93 to 5,057 matching findings (median about 1,400) and sent 23 to 38 (median 31: the
  60,000-character limit stops before 40), from 12 to 22 documents; the search took at most 0.3 s and the
  message at most 0.4 s. The final check over every finding's texts as sent found nothing left (no name,
  link, phone or e-mail). A few authors' surnames are also ordinary words: a question that writes one with a
  capital sends it as "[name withheld]" (the search still uses the word).

**Deleting.** A superuser can delete a version that is not shown and not loading, from its page. The
version shown is replaced by uploading another.

Quick migrations `km 0001_initial` (new, empty tables with their search indexes), `km 0002_organisation`
(the corrections' new, empty table) and `km 0003_question` (the assistant's questions' new, empty table);
CreateModel only.

## Help assistant (`/help/`, Ctrl+Shift+H)

An in-app help chat that answers **how NeuroDB works** (what a page shows, what a rule checks, where a
number comes from, why a visit scored what it scored, which job refreshes a page and when), never
questions about the programme data: those go to Ask NeuroDB and Chat with Data, and the panel says so
with a link. Every signed-in person but a donor has it. The code is in `neurodb/help`.

- **The help pages** (`/help/`, *Help* at the bottom of the full menu; *Help pages* in the "?" panel for
  everyone): the help guide, written for staff (not
  this runbook), one page per area: *Getting around NeuroDB* (the sidebar, roles, data sources and refresh
  times), *Home* (what it shows, the first load, ActivityInfo in use or archive), *Monitoring insights* (filters, every tab and card and what it counts from which eTools data,
  Not monitored, the quality score's categories, weights and bands, the rules switched on and what each
  checks, the AI checks, urgency, the AI brief, Chat with Data and the map), *Action points*, *Exports*,
  *Knowledge base and documents*, *Ask NeuroDB*, *For you and What's new*, *Country overview, management
  brief and daily review*, *AI limits* and a *Glossary*. Each heading has its own address
  (`/help/monitoring-insights/#urgency`); the search box searches every section. The guide is Markdown in
  `neurodb/help/guide/*.md`, packaged with the code (the `docs` folder is not in the image), rendered and
  sanitised as Ask's answers are (`markdown` + `nh3`, links only to NeuroDB's own pages). It holds no
  command, no setting's name, no address of another site, no secret and no person (a test checks every
  file), and a test checks that its list of the rules switched on, the score categories and weights,
  the bands and urgency's weights and thresholds are those NeuroDB seeds. When a rule or a setting is
  changed in the admin, the guide keeps describing the defaults; the assistant reads the live values.
- **The panel**: the **?** button in the top bar, or **Ctrl+Shift+H** (Cmd+Shift+H on a Mac) on any page,
  opens a small panel at the bottom right headed *Help assistant*; Esc, the × or the shortcut again closes
  it, and the keyboard focus stays inside while it is open. Its body (the quota chip and four starter
  questions) is loaded the first time it opens, so pages carry no extra query. Each question is sent with
  the page's address (path only, no query string) and title, so "this chart" can be resolved; the
  conversation is kept for the browser tab (`sessionStorage`), *Clear chat* starts a new one, and the
  server sends at most the last 6 turns to the model. It streams like Ask NeuroDB (the same `ask.js`).
- **What it reads** (its own look-ups, offered to it alone, never to Ask NeuroDB): `search_help` and
  `read_help` (the guide, searched in memory, no database), `list_quality_rules` and `get_quality_rule`
  (the live quality rules, score settings and urgency settings of Monitoring insights; a rule's reference
  lists only as a count, as R19's hold e-mail addresses; an AI rule's instructions only for an
  Administrator, for that rule), `explain_visit_score` (a visit's score record by record: each record's
  score, band, deductions per category, each rule's result and points lost, and urgency's parts, the
  checks read once for the whole visit, then the visit's mean, lowest record and urgency: never a
  narrative, an answer, a team, a
  monitor, nor an AI check's explanation; the same access as the visit page, nothing while
  `FMM_ENABLED` is off) and `list_jobs` (the scheduled jobs, what each does, its schedule and its last
  run's status and time: never who ran it or its error text). Each look-up runs in a read-only
  transaction. Answers cite the guide sections (`/help/<page>/#<slug>`) and, for an Administrator only,
  the admin page a setting lives on.
- **What it sends to the AI provider in use**: the question (names NeuroDB knows, e-mail addresses, phone numbers and links
  removed before it is sent and kept), the page's path and cleaned title, up to 6 earlier questions and
  answers of the conversation, and what its look-ups return. Its own prompt (not Ask's), the model of
  AI settings at low effort, at most 4 rounds and 90 seconds, `store=false` (OpenAI; Claude keeps no
  conversation either), a keyed hash of the person (OpenAI's `safety_identifier`, Claude's
  `metadata.user_id`).
- **What it refuses** (a refusal costs no quota): passwords, API keys, secrets, connection strings and
  environment variables, and ways around sign-in or access rules are refused before any call (a fixed list
  of words, `help.assistant.screen`); the model declines the subtler ones and questions about programme
  data (pointed to Ask NeuroDB) or not about NeuroDB. Either way NeuroDB's own message is shown.
- **Limits**: `HELP_PER_USER_PER_DAY` (20) questions a day per person, counted from local midnight;
  declined questions do not count, and one being answered does (429 "You have asked 20 help questions
  today; the count starts again tomorrow."). Questions the model declines were calls all the same, so
  only as many as the quota are free each day; past that the person waits for tomorrow (429). At most 2
  being answered per person. The shared
  `AI_DAILY_TOKEN_SOFT_CAP` at 100% (a person asks), and the AI service's credit pause of Monitoring
  insights (6 hours after the provider says the credit ran out; a help answer that meets it starts the
  pause too). Its
  calls are counted under *Help assistant* in *AI use*. One answer is usually 2-3 model calls of about
  4,000-10,000 tokens each, mostly the cached prompt and look-ups.
- **Switching it off**: `HELP_ENABLED=false` (default: on, whenever the AI is on; it also needs the
  AI assistant: a key set by the hosting team or entered in AI settings). The help pages stay; the panel says the assistant is switched off and links them.
- **The log**: Admin → Data and sync → **Help questions** (read-only): who asked, from which page, the
  question as sent, the answer, answered / declined (and why) / failed / over a limit, the look-ups, the
  tokens and the time. Kept 90 days: each run of the daily review job (`daily-review`, 06:00) first
  deletes the older ones; nothing to run by hand.
- **Updating the guide**: edit the Markdown in `neurodb/help/guide/` with the change it describes (a
  developer change, deployed like code). The tests in `tests/help/` check the rules list against the
  seeded rules, every link and anchor, and the words a guide must not hold.

## Scheduled jobs
The periodic jobs are managed in the admin: **Data and sync → Scheduled jobs**. Each row is one
command on one schedule, in **Beirut time** (summer time is followed automatically):

| Job | Runs | Default schedule (Beirut) |
|---|---|---|
| `doc-review` | `review_documents --pending` | `40 4 * * *`, daily 04:40: the document review of the knowledge base documents in a review batch that are waiting, failed or partly analysed, within its daily cap (see Document review); while the review is switched off it analyses nothing |
| `locations` | `sync_locations` | `0 5 * * *`, daily 05:00: the eTools locations from the Datamart (`--source rest` for the older eTools REST API, which needs `ETOOLS_TOKEN`; its location-types endpoint is gone and is skipped) |
| `fmm-refresh` | `fmm_refresh` | `25 5 * * *`, daily 05:25, after the locations: rebuilds and scores the Monitoring insights visits, so that overdue action points, the age of a visit and urgency are recomputed even when no data changed (it also runs after every Datamart sync) |
| `fmm-insights` | `fmm_insights` | `40 5 * * *`, daily 05:40, after the refresh: the AI monitoring briefs of the whole country, each section people land on and the last 90 days (reused at no cost when their data has not changed); with `FMM_AI` off it writes nothing and only applies the briefs' retention |
| `fmm-ai-checks` | `fmm_ai_checks` | `50 5 * * *`, daily 05:50, after the refresh and the briefs: the AI checks of the quality rules on the records not checked yet (this year's first, then those of visits with several records, newest first, within `FMM_RULES_DAILY_TOKEN_CAP`), then the scores are recomputed; with the AI or the AI checks off it checks nothing |
| `daily-review` | `daily_review` | `0 6 * * *`, daily 06:00, after the night's syncs; it first deletes the Help assistant's questions older than 90 days |
| `fmm-ap-review` | `fmm_ap_review` | `10 6 * * *`, daily 06:10, after the AI checks: the AI review of the completed eTools action points not reviewed yet or whose texts changed (most recently completed first, within `FMM_AP_REVIEW_DAILY_TOKEN_CAP`); with the AI or the review off it reviews nothing |
| `activityinfo-data` | `import_activityinfo_data --current-year` | `0 18 1-22 * *`, 18:00 on days 1–22 |
| `etools-datamart` | `sync_etools_datamart` | `30 20 * * *`, daily 20:30 |
| `freshness` | `check_sync_freshness` | `15 * * * *`, hourly; a stale source is logged as an error |
| `activityinfo-structure` | `import_activityinfo_structure --all` | switched off; switch on or use *Run now* after the yearly rollover |
| `compiler-youth` | `sync_compiler_youth` | `0 21 * * *`, daily 21:00: asks Compiler (BMA) to count the youth figures, waits, then reads them; switched off until Compiler is configured |
| `compiler-education` | `sync_compiler_education` | `30 2 * * *`, daily 02:30: asks Compiler (BMA) to count Makani and Bridging, waits, then reads the counts; switched off until Compiler is configured |
| `compiler-wellbeing` | `sync_compiler_wellbeing` | `0 3 * * *`, daily 03:00: asks BMA to work out the Makani wellbeing flags, waits, then reads them; switched off until Compiler is configured |
| `knowledge-hub` | `build_knowledge_hub` | `0 7 * * *`, daily 07:00: reads new library and CPD documents, then rebuilds the knowledge hub (it is also rebuilt after every sync) |
| `whats-new` | `whats_new_digest` | `30 7 * * *`, daily 07:30: the what's new notes, emailed to who asked |
| `watch` | `run_watch --daily` | `45 7 * * *`, daily 07:45: NeuroDB Watch's morning pass, the *For you* pages (see NeuroDB Watch); a quick pass also follows new data |
| `ml-readiness` | `ml_readiness` | `45 6 * * 1`, Mondays 06:45: is the data ready for machine learning (Data health) |
| `forecast` | `forecast_indicators` | `15 7 * * 1`, Mondays 07:15: year-end forecasts of the indicators, back-tested |
| `reporting-checks` | `reporting_checks` | `15 2 * * *`, daily 02:15, after the eTools Datamart sync, and due at once after the deployment that adds it (when the previous container's scheduler, which does not know the command, sees it first, the new release's scheduler starts it as soon as it takes over): the eTools reporting checks, stored for their page (see eTools reporting checks); the release that adds check 12 makes it due a quarter of an hour after its migration, so its checks replace the previous release's result soon; also *Run the checks now* on the page and *Run a job → eTools reporting checks* |
| `etools-masters` | `suggest_master_indicators` | `40 2 * * *`, daily 02:40, after the 20:30 eTools Datamart sync and the 02:15 reporting checks, and due at once after the deployment that adds it: suggests the partners' PD indicators for the eTools master indicators and proposes new ones (see *eTools master indicators*); nothing counts until someone confirms it; also *Suggest now* on the masters' admin list and *Run a job → eTools master indicators* |
| `etools-update-cleanup` | `datamart_backfill` | `*/15 * * * *`, every 15 minutes, due at once after the deployment that adds it (the scheduler starts it usually within a minute of the site starting, at most 15 minutes when the previous container's scheduler sees it first) and until one run has gone through everything; the job then switches it off ("finished: nothing left to do"). A run that finds the eTools Datamart sync going switches it back on to try again ("waiting: the eTools Datamart sync is running"). It fills the new columns of the partner reports already synced and removes the contact details older syncs stored (see eTools Datamart, *Finish the eTools update*) |
| (inside `activityinfo-data` and `etools-datamart`) | `link_partners` | at the end of both jobs |
| (inside `etools-datamart`) | `fmm_refresh` | at the end of the sync, when it synced a dataset Monitoring insights reads (`FMM_REFRESH_AFTER_SYNC`); its failure never fails the sync |

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
job only while its schedule is switched on; the eTools reporting checks only a run that failed, since a
check that could not finish is said on their page; the eTools Datamart sync dataset by dataset, see
eTools Datamart, *One failed dataset is not the whole sync*), a missing daily review, and the eTools master
indicators of this year without a target (see *eTools master indicators*). NeuroDB Watch tells the
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

## ActivityInfo: in use or archive

Partners reported their monthly results in ActivityInfo in past years and now report progress in
eTools/PRP; the full menu lists the years the archive holds. NeuroDB keeps every ActivityInfo year
(databases, dashboards, analytical views, maps, Neuro and HPM reports, the import runs): nothing is
deleted. One state says whether ActivityInfo is still **in use** or an **archive**; the sidebar, Home,
the alarms, the forecasts, search, the landing page and Ask NeuroDB all read it
(`neurodb/indicators/services/activityinfo.py`, with notes at its top). Three rules:

1. **Which years have ActivityInfo figures** (whatever the state). A calendar year is an ActivityInfo year
   when a reporting year of that year (the first four digits of its *year*, else of its name) has a
   database shown on the dashboard with at least one activity record; when several reporting years
   qualify, the one ticked current counts, else the highest name. In such a year the Country overview,
   Home, the brief and the donor page keep the ActivityInfo figures (children reached is the larger of the
   two sources, with the months each covers): they are history, and no setting drops them. A year without
   records shows eTools alone, never an "ActivityInfo 0", and a page shows ActivityInfo figures only where
   the chosen sections have ActivityInfo children. **Show on dashboard** keeps its meaning: it hides a
   database from every page, the archive included, so it is not the way to drop a year's ActivityInfo
   figures.
2. **In use or archive**, set on the admin page *Reporting setup → ActivityInfo: in use or archive*:
   - *Automatic (recommended)*, the default, also while nobody has ever saved the page: in use while the
     ActivityInfo import is switched on in *Scheduled jobs* (any switched-on job that runs
     `import_activityinfo_data`, whatever its key) **and** the year the import serves has records **no
     older than three months** (`RECENT_MONTHS`: the newest `month_name` of its displayed databases, at
     most three months before today's month). The year served is this year, or last year during January,
     when the import still brings in the December results (January reports December). Otherwise
     ActivityInfo is an archive by itself: an import left on over a year partners stopped reporting in
     (records up to April) does not keep it "in use" in August.
   - *In use*: in use whatever the records and the schedule.
   - *Archived*: an archive whatever the records and the schedule.
3. **Whether the import's own alarms are on** (`watched`): when set *In use*, or on *Automatic* while the
   import is switched on and either ActivityInfo is in use or the databases of a returning year wait for
   their first records (the reporting year ticked current is this year, has a database shown on the
   dashboard, and none of them has a record yet). Never when *Archived*. So a comeback is followed from
   the day its databases are added, before any record makes the year "in use", and an import left on over
   a stale year stays silent.

What follows rule 2:

| | In use | Archive |
|---|---|---|
| Sidebar (full menu) | *Monthly results (ActivityInfo)*, at the bottom, open by default | *ActivityInfo archive* with its years ("2019–2026"), at the bottom, closed; a reader who opens it keeps it open |
| Home | *Programmes and sources*: "ActivityInfo 2026: n results tracked against targets · last import …" ("ActivityInfo 2026: records imported · last import …" while no result is tracked yet; "ActivityInfo 2026: in use, waiting for its first records" while the reporting year ticked current has no record yet; a year ticked current that has records, 2025 say, shows its own line); the Section editors' "HPM reports of 2026 to comment on" | "ActivityInfo archive 2019–2026: every database and report, kept as it was" |
| Year-end forecasts | made every Monday; the Country overview's and this year's database dashboards' forecast card (this year only); Watch's forecast points | the weekly job skips ("ActivityInfo is not in use in 2026") and keeps the last table; the page says "These forecasts were made for 2026 while ActivityInfo was in use. They are no longer updated."; no card; Watch's forecast points close ("ActivityInfo is archived: its forecasts are no longer followed"). The forecast job may then be switched off in Scheduled jobs: Watch does not call the forecast source stale, and a stale-source point still open closes with that reason |
| Search | the ActivityInfo groups of every year | the same, prefixed "ActivityInfo archive:" (except *ActivityInfo indicators*) |
| Landing page | ActivityInfo as a current source ("Monthly results — ActivityInfo", "ActivityInfo" among the connected sources), with the ActivityInfo figures | "Historical results — ActivityInfo", "ActivityInfo archive 2019–2026", no ActivityInfo figure |
| Ask NeuroDB | "ActivityInfo is in use (reporting year 2026)" | "no longer updated: its 2026 records stop at Aug 2026" when this year has records, then "NeuroDB's children reached for 2026 still counts them" when they are behind an HPM indicator that counts children (the Country overview's ActivityInfo children), else "they add no children … eTools alone"; without records this year "an archive of 2019–2026 … use the eTools tools" |
| Daily review | ActivityInfo partner names not linked to eTools are a finding | not checked |

What follows rule 3 (the import's own alarms):

| | Followed | Not followed |
|---|---|---|
| The *freshness* job (`check_sync_freshness`, button *Check freshness*) | checks `ai_data` | leaves `ai_data` out: an import 45 days old is not an error |
| Daily review | stale-source and sync-failure findings about the ActivityInfo jobs | none |
| Admin home *Needs attention* (and Watch's *system* points) | no current reporting year, databases never imported or stale, master indicators without a target, failed ActivityInfo runs | none; Watch closes those points |
| Data health | the ActivityInfo jobs as before; the databases of the year served (or of the returning year) | the ActivityInfo jobs read "Not in use (archive)" with a neutral pill; the databases of the last ActivityInfo year, state "Archive", with a note. Once the `activityinfo-data` job is switched off: "ActivityInfo is an archive: these 2026 databases are kept as they were and are no longer imported." While it is still on (archived does not stop it): "… kept as they were. Its import is still switched on in Scheduled jobs, but NeuroDB no longer warns when it fails or falls behind; switch it off once its last year is complete." |
| ActivityInfo databases page (`/databases/?year=`) | a database not imported for 40 days is "Stale", with the reason; the imports box describes the monthly import | no database is called "Stale"; the imports box says "ActivityInfo is an archive: its import is no longer followed. It is still switched on in Scheduled jobs, but NeuroDB no longer warns when it fails or falls behind." (or, once the job is off, "its import is switched off and its databases are kept as they were") |
| Watch's points about the import | open while the import fails or falls behind; closed as fixed when it recovers | the points still open close with "ActivityInfo is an archive: its import is no longer followed" (never "fixed") |
| Country overview freshness strip | lists ActivityInfo data | leaves it out |

**The admin page** (*Reporting setup → ActivityInfo: in use or archive*, the first row of Reporting setup;
one row, no *Add* and no *Delete*; administrators with *Staff status*) has the three choices and, read
only, *Now*, *Years with ActivityInfo records* and *ActivityInfo import*: why the state is what it is, in
plain words. The lines it can show:

- "In use (automatic): 2026 has ActivityInfo records up to Aug 2026 and the ActivityInfo import is
  switched on."
- "In use (automatic): in January the import still brings in the December 2026 results."
- "Archived (automatic): 2027 has no ActivityInfo records. It turns in use by itself when the first 2027
  records are imported."
- "Archived (automatic): the newest ActivityInfo records are for Apr 2026, more than three months ago. It
  turns in use by itself when newer records are imported."
- "Archived (automatic): the ActivityInfo import is switched off in Scheduled jobs."
- "In use (set by an administrator)." or "Archived (set by an administrator)."
- then "Years with ActivityInfo records: 2019–2026." and "The ActivityInfo import is on. It imports the
  reporting year ticked current: 2026.";
- while a returning year's databases wait for their records and the alarms are on: "The 2027
  ActivityInfo databases wait for their first records: the import's alarms stay on."

The admin home's *This year* tile shows the calendar year, the first line under it and a link to the page.
Saving records who changed it (*Updated by*, *History*).

**Within 30 seconds.** The state is computed in four queries and kept 30 seconds by each web worker
(`activityinfo:state:v1:<year>:<month>` in the default cache), read once per request. Saving the page,
switching a scheduled job on or off (its toggle, its form, the *Switch off the selected jobs* action),
deleting a job, saving or deleting a reporting year or a database, and the end of an ActivityInfo import
drop it on the worker that did it; the other workers follow within 30 seconds. The scheduler's own saves
of a job (its next and last run) do not. The landing page's figures (kept an hour per worker) hold this
year's ActivityInfo reporting year and the state in their key, so they change with it.

**Over the year** (Automatic, the import left on):

| When | State |
|---|---|
| 15 October 2026, 2026 records up to August | in use (August is two months back) |
| the same, records stopped in April | archive (not recent): the 2026 figures stay; the import's alarms are silent |
| the same, the import switched off | archive; Data health shows its jobs "Not in use (archive)" |
| 5 January 2027, 2026 records up to November or December | in use until 31 January (the December import); a failed December import is reported |
| 5 January 2027, 2026 records up to August | archive |
| from 1 February 2027 | archive (2027 has no ActivityInfo records) |
| 2027 databases added for a returning ActivityInfo | still an archive until the first recent records, but followed from the day the databases are created ("never imported", failed runs reported); in use within 30 seconds of the end of the import that brings its first recent records |

In 2027 an import left switched on keeps importing the reporting year ticked current (2026) on days 1–22:
in January that is the December import; from February its failures are no longer reported (its runs stay
on Data health), and the admin page's third line says which year it imports. Switch it off after 22
January (step 8 of *Yearly rollover*).

**The yearly reminder** (NeuroDB Watch, `reporting_year_rollover`, 15 December to 15 January,
Administrators): while ActivityInfo is not set to *Archived*, is set *In use* or has its import switched
on over last year's records, and no reporting year of the new year has a displayed database: "ActivityInfo
in 2027: set it up, or set it to Archived", linking to the admin page. It closes when the new year's
databases are added ("ActivityInfo databases for 2027 were added"), ActivityInfo is set to *Archived*, or
its import is switched off; after 15 January as missed ("the rollover period ended; ActivityInfo stays
archived for 2027 until its databases have records"). Otherwise the reminder is "Population figures for
2027" (step 5 of the yearly rollover), until figures for that year or a later one are loaded: archiving
ActivityInfo never removes the yearly population reminder. Administrators who act on these reminders need
*Staff status* to open the admin pages they link to.

**Bringing it back.** Create the year's reporting year (tick it current) and its databases (steps 1–4 and
7 of *Yearly rollover*): the import's alarms come back at once, and ActivityInfo turns in use at the first
recent records. Or set *In use* on the admin page. To undo the whole change: set *In use*, or revert the
release (`migrate core 0027` drops the empty setting table).

## Running jobs without a command line

Everything an operator runs with `manage.py` has a button in the admin, for administrators, each
with a confirmation step. Admin → *Data and sync* → *Import and sync runs* (also linked from the
admin home page, *Quick actions*). Raw eTools data and the figure trails are pages; the REST raw copy is a
button on Raw eTools data (and in Run a job); the fields count is written by the existing syncs.

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
| Run a job → *Monitoring insights* | `fmm_refresh` | background |
| Run a job → *Monitoring insights (AI)* | `fmm_insights` | background |
| Run a job → *Monitoring insights (AI checks)* | `fmm_ai_checks` | background |
| Score settings → *Re-check carried answers* | (deletes a batch of carried AI check answers; the next `fmm_ai_checks` runs check them) | in the request |
| Run a job → *Action points (AI review)* (also *Run AI review* on the action points page, with a batch size) | `fmm_ap_review` (`--limit N` from the page) | background |
| Run a job → *Review documents (pending)* (also the admin action *Analyse in the document review* on Knowledge documents) | `review_documents --pending` (`--document <id>` for one document) | background |
| Run a job → *Review documents (full)* | `review_documents --full` | background |
| Run a job → *Locate document findings* | `review_documents --locate` | background |
| Run a job → *Finish the eTools update* | `datamart_backfill` | background |
| Run a job → *eTools reporting checks* (also *Run the checks now* on the eTools reporting checks page) | `reporting_checks` | background |
| Run a job → *Probe a Datamart dataset* (the dataset chosen in the dialog) | `probe_datamart --dataset <name>` | background (seconds); writes nothing but its run |
| Run a job → *eTools master indicators* (also *Suggest now* on *Reporting setup* → *Master indicators (eTools)*) | `suggest_master_indicators` | background |
| *Reporting setup* → *Master indicators (eTools)*: actions *Accept the proposed master indicators*, *Dismiss the proposed master indicators*, *Confirm all suggestions*, *Confirm suggestions scoring 0.9 or more*, *Merge the selected master indicators*, *Change section (after an eTools rename)*, and *Add PD indicators* on a master's page; *Master indicator links (eTools)*: *Confirm*, *Reject*, *Make "Shown only"*; the same decisions on Partner monitoring's *Master indicators* tab | (no command: they change the master indicators and their links) | in the request |
| *Reporting setup* → *Master indicators* (ActivityInfo): action *Create eTools master indicators from these* | (no command: writes eTools master indicators only, never an ActivityInfo row) | in the request |
| Admin → *KM Database* → *Versions* → **Upload a new version** (superusers and Administrators) | `load_km_database` (reads the uploaded workbook into the KM Database; writes only the KM tables) | background |
| *Data and sync* → *Compare with Power BI* → **Upload and compare**, and an upload's **Compare again** (validators) | `compare_powerbi` (reads Power BI's exports and compares them with NeuroDB's pages, in read-only snapshots; writes the Compare with Power BI tables and nothing else; lock 7140443; not an import run; refused while a Datamart sync or a KM Database load runs) | background |
| Run a job → *Population figures* | `load_population_figures --bundled --replace` | in the request (seconds) |
| *Data and sync* → *Raw eTools data* (validators) | (no command: pages and a streamed CSV; the fields of each source are counted by the existing syncs as they write, `details.keys_kept`; a superuser's *Reviewed: show it* writes one row) | in the request |
| *Raw eTools data* → **Read the eTools REST API now** (superusers and Administrators who are validators), or Run a job → *eTools REST raw copy* | `read_etools_rest_raw` (the raw copy of the eTools REST records; writes no other table) | background |
| *Check these figures* under Progress reports' tabs, Partner monitoring's views and map, and Home's two partner reporting blocks (validators) | (no command: the figure trail is a page in parts, its rows streamed CSVs; it writes nothing but the shared page figures) | in the request |
| Run a job → *Check freshness* | `check_sync_freshness` | in the request; changes nothing |
| Run a job → *Repair roles* | `bootstrap_roles` | in the request |
| Run a job → *Empty the stored page figures* | (no command: empties the shared page figures, the table `neurodb_cache`, and says how many entries it removed; see *Pages that load in parts*) | in the request (a second); changes no data |

**Monitoring insights: refresh first, then the AI checks.** The two buttons do different things and are
pressed in this order when both are wanted: *Monitoring insights* (`fmm_refresh`) rebuilds the visits and
their records and scores them; *Monitoring insights (AI checks)* (`fmm_ai_checks`) then checks the
records whose AI checks are missing or out of date and recomputes the scores. The schedule runs them in
that order every morning (05:25, then 05:50). Each AI checks run stops when the day's budget is used
(`FMM_RULES_DAILY_TOKEN_CAP`, 2,000,000 tokens, about 900-1,300 checks); pressing it again the same day
does nothing more until the next day unless the cap is raised for a few days in the App Service
configuration (no shell needed). Its run details say what is left: `records_pending` (records with a check
still to make: they stay provisional, without a score), `checks_pending` (the checks those need: records
with the same texts share one) and `nights_estimate` (the nights that takes at the pace of the last run
that made checks; 0 when nothing is left), with `checked`, `shared` (records served by an answer made for
another record with the same texts in that run), `up_to_date`, `carried` and `legacy_checks_left` (see
*AI checks of the quality rules*, below).

How to read `records_pending` after the change to records (Release 2 step 5): it is high on the first
morning (every record of a visit with several records waits for its checks; single-record visits keep
the verdicts carried over) and goes down each night by the checks the budget allows; `nights_estimate`
says how many nights are left at that pace. While it is above 0 the records it counts are provisional
(no score, no urgency) and so are their visits, and the page's banner says so. To go faster, raise
`FMM_RULES_DAILY_TOKEN_CAP` for a few days in the App Service configuration and press *Monitoring
insights (AI checks)* again: each press uses what is left of the day's budget, then scores again.
`legacy_checks_left` reads 0 once the checks made per visit before records are all carried over (the
first refresh after the deployment does it); nothing is to be done when it does.

One database at a time: *Reporting setup* → *Databases*, select them, action *Import structure* or
*Import data from ActivityInfo* (one background process per database). The daily review and the
population figures also have their button on their own admin pages.

A background job runs as its own process (it survives the web worker that started it), is recorded
as a run in this list like a scheduled run, and does not start while a run of the same job is in
progress.

**Rule: an administrator never needs the command line.** NeuroDB runs on App Service with no shell
for its administrators, so every operation they need is a button here, a scheduled job, or an action
on its own admin page; a new command that an administrator would have to run gets its button in the
same change. Developer-only commands are the exception, and each has an admin equivalent where it
matters: `migrate_locked` and `ensure_legacy_tables` (every deployment runs them), `seed_demo` (local
databases only), `record_datamart_samples` and `fmm_redact_fixtures` (they write test fixtures into
the source code: the administrator's equivalent is *Download samples (redacted)* on *Fields found*),
`fmm_refresh --probe-only` (the full refresh, *Refresh now* on *Fields found* or *Run a job →
Monitoring insights*, reads the keys too) and `map_watch_sections` (*Match again* on *eTools section
names*).

## Pages that load in parts

A page that loads in parts answers its frame at once and fetches each of its data blocks in the
background. These pages work this way: **Home**, the **country overview**, the **daily review**, the
**management brief**, the **partner page**, **Data health**, **partner monitoring** (its Grid, Program Performance and Data
quality views), **Progress reports** (its *By location* and *By disaggregation* tabs), the Map tab of
**Monitoring insights**, the **country programme**, **youth programmes**, the **donor page** and three
views of the **document review** (*By document*, *Themes* and *Repeated findings*). *The pages in
parts*, below, lists their parts and what each stores; every other page, and the other tabs and views
of these, is still drawn whole, as before.

**What staff see.** The frame comes first: the page's heading and filters, its tabs and the notes
that need no figures (on the overview, the purpose line, the subtitle and the freshness strip) and, in
place of each data block, a grey placeholder of about the block's size, so the page does not jump
when the block arrives. Each block then appears as soon as it is ready, and the
rest of the page can be used meanwhile. A block still loading after 8 seconds adds "Still working on
this part. Large filters take longer." A block that fails shows a message in its place with a **Try
again** button; the rest of the page keeps working, and no error pop-up appears:

| Message | Cause |
|---|---|
| This part could not be loaded. | A server error (500); or a network error, 502, 503 or 504 that failed again after the one automatic retry, 2 seconds later. Shown on every block at once, with *Show the figures* in place of **Try again**, when the page's own script could not run (it failed to download or was blocked): open *Show the figures*, the same page with its blocks drawn in, and reload the page later |
| This part took too long. Try again, or narrow the filter. | No answer within 60 seconds (each block's own limit, below gunicorn's 120 s), twice: the first time out is retried once by itself, 2 seconds later, with 60 seconds again, so the message shows after about two minutes. Also when the server kept answering "still computing" (below) for those 60 seconds, twice |
| You do not have access to this. | 403, for example a donor account |
| This part is no longer on the page. Reload the page. | 404: the page changed under a tab left open (a new release renamed or removed the block) |

The donor page draws its figures with its own script (`donor.js`), so its one box says it in its own
words, with the same **Try again**: "The figures could not be loaded." (a server error, or a network
error, 502, 503 or 504 that failed again after one retry), "The figures took too long to come. Try
again in a moment." (no figures within 60 seconds in all, "still computing" answers included) and "You
do not have access to this." (403).

Screen readers hear one short summary ("Loading the figures…", "All figures are shown.", "1 part could
not be loaded."), never one message per block, and an arriving block never moves the focus. A results
area that can hold blocks (Monitoring insights, the document review, partner monitoring, Progress reports)
is not read out as a whole when a tab, view, filter, sort or page change replaces it, nor when a person
changes something inside it (a page, a sort or the rows per page of a table in it, an entity chip, an
Accept, Reject or Undo, an action point's status, a finding deleted): they hear "The results were updated."
instead, once per change. A block arriving, a list opened, a panel loaded when reached and a refresh on a
timer (the documents being analysed) are not announced that way. Monitoring insights' AI brief has its own
line instead: after *Regenerate* they hear "Writing the AI brief… about 30 seconds.", then, once, "The AI
brief is ready." or why it was not written. Printing
with Ctrl+P while blocks are still loading prints a note in place of each of them ("This part was still
loading when the page was printed. Wait until every part is shown, then print again."). A page's own
**Print** button (the management brief's) waits instead: it asks for every part still to come, the ones
further down too, then waits for the charts in them to be drawn, reading *Preparing the page…*
meanwhile, and prints once everything has arrived or failed (after a minute at most it prints what it
has). Without JavaScript each placeholder offers *Show the figures*, the same page with its blocks drawn
in. Where JavaScript runs but the page's script could not (it failed to download or was blocked), each
block, once the page has loaded, says "This part could not be loaded." with the same *Show the figures*
link instead of grey shapes that never fill. On a phone the filter bars fold behind a **Filters**
button; where the page's script could not run (it failed to download, was blocked, or the browser is
too old), they stay open, as without JavaScript. A block that comes from another address (Home's
*For you* card) is not drawn on that page: it is an empty slot there, with a link to its own
page (*Open this block on its own page*) when it has one. A session that has expired sends the browser
to the sign-in page and back to the page afterwards, never a sign-in form inside a block.

**How it works.** Each block (a *part*) is asked for at the page's own address plus `_part=<name>`, as
an htmx request, with the filters the frame resolved written out in full (the user's own section as
`section=<name>`, every section as `section=`, the year on every page that has one (Home and the daily
review have none), and on Home, the overview and the brief `as_of`, the state of the figures the frame was drawn with, see *The pages in parts* below; a page without
filters, such as the partner page or Data health, adds `_part` alone), so the same view answers it under
the same rules:
sign-in, the donor lock-out and the view's own checks. `PartsMiddleware` (`neurodb/web/parts.py`) takes
`_part` out before any view or link sees it, adds `Vary: HX-Request` (one address serves the page or a
part) and sends parts with `Cache-Control: no-store`. `neurodb/web/static/js/parts.js` asks for at most
3 parts at once per page (2 while the first block of a group computes the figures the group shares), the
visible ones first and the ones further down one at a time afterwards, and swaps each answer in place of
its placeholder. The figures several parts read are computed once across every worker and every user,
and kept in the **shared page figures** (`neurodb/core/compute.py`, `once()`): one request computes them
under a PostgreSQL advisory lock, the others wait and read the stored result. Inside a database
transaction (the read-only tool runs of NeuroDB Watch, the help and the field monitoring chat, or an
admin action) stored figures are read as anywhere else, but missing ones are computed for that caller
only: no lock is taken and nothing is stored or deleted there. A part request that has
waited about 2 seconds answers **202** with `X-NeuroDB-Part-Pending: 1`, which frees its web thread, and
the browser asks again after 1.5 seconds, then less often, within the 60 seconds. When the figures of a
block are already stored, the frame draws that block itself, in the same answer, so a page opened again
within their lifetime asks only for its other blocks (the overview: the tiles and the charts come with
the frame and What's new follows it, one small request; Home: the key figures come with the frame, and
*For you*, the decisions and the daily review, *Since* and *Programmes and sources* follow, four small
requests; the brief: both of its parts come with the frame, one request in all). Each stored value's key holds what
the synced figures depend on (the filter, the day, the release and the runs that feed them: the last
finished sync run and the indicator flags on most pages, the latest run of each eTools job and the flags
on partner monitoring and Progress reports, the Monitoring insights stamp on that page, the runs of the
jobs that write the tables counted for Data health's row counts; the table below gives each page's; the
language too, unless the value reads the same in every language, as the overview's do) and nothing about
the person asking: two people with the same filter read the same figures, and a user's own section, the
document review's *Verified only* switch or a donor's account only enters as the filter. What differs
by person is drawn in each request for the person asking and never stored: the brief's *Assign* links
for staff, the partner page's *Edit the link*, the administrators' link to the reporting checks on Data
health, the document review's buttons for editors. Of what staff edit by hand outside a sync, the
indicator flags are in the key of every value they change, the brief's key also holds its section plans,
finding assignments, ActivityInfo partner links and latest daily review, the country programme's key a
digest of the rows staff enter for its cycle, youth programmes' a digest of the year's links to eTools (both, and
the Makani and Dirasa tabs' cache, also the card versions chosen in Admin → *Power BI cards*), the
document review's repeated findings every verdict and edit, Monitoring insights' figures the NeuroDB
action points, the *Score settings* thresholds and the places of Admin → Locations, and partner
monitoring's map points the gazetteer; the governorates of the overview's filter bar (Admin → Locations)
are read at each visit and Data health counts the gazetteer at each visit, so all of these show at once.
Any other hand edit shows when the stored value it is part of expires, or at once after **Empty the
stored page figures**: a governorate renamed in Admin → Locations is in the filter bar at once, but
keeps its old name in the overview's and the brief's figures for up to their builds' 2 minutes, and so
does a population figure changed by hand in the admin (*Reload population figures* is a run like a sync,
and shows at once).

**The shared page figures (table `neurodb_cache`).** The table is created by core migration 0026 at
start-up (nobody runs `createcachetable`) and read through Django's database cache
(`CACHES["shared"]`: at most 300 entries; when it is full, a third of them are removed). It is
UNLOGGED: it is not written to the database's log nor copied to replicas, and it comes back empty after
a database crash or a restore, which loses nothing, since every page computes its figures again. Each
value lives for its own time (an overview build, a brief build, the brief's lineage, the freshness
strip, partner monitoring's indicators and map points and Progress reports' tables two minutes, the
donor page's figures five, the filter bars' choices, Monitoring insights' figures and the country
programme's and youth programmes' builds ten, Data health's row counts and the document review's
repeated findings an hour), is compressed above 64 KB and is not stored at all above 4 MB once
compressed (it is then kept for that request only). The expired rows are deleted automatically: each
web process does it at most once every 10 minutes, after storing a value, so there is nothing to
schedule. A new release never reads what an older one stored (the release is part of every key). Tests keep the
shared cache in memory.

**Emptying it.** Admin → *Data and sync* → *Import and sync runs* → *Run a job* → **Empty the stored
page figures** (administrators, after a confirmation) empties the table in the request and says how
many entries it removed; each page then computes its figures again at its next visit, which makes
that visit slower.
It is never needed in normal use: a finished sync or a changed indicator flag already makes every
page compute again the figures it changes. Use it when a page keeps showing figures that look out of
date, or before timing a page from a cold start. It changes no data.

**The pages in parts.** Each part says when the browser asks for it: *at once* (it is at the top of
the page), *when near the screen* (within 800 pixels of the part of the page in view), *after
the visible ones*; a part not needed yet still loads once the visible ones are done, one at a time.
In a group, the first part goes alone and computes the figures the group shares; the others wait
until it is in, then read those figures stored. The address of each part carries `as_of`, a short code
(16 letters and digits) for the state of the figures the frame was drawn with: the last finished sync run
and the indicator flags, and for the brief also its section plans, finding assignments, ActivityInfo
partner links and latest daily review. A sync run that finishes (an eTools Datamart sync writes one per
dataset, 52 in all, about one every 20 seconds), or an edit, changes that state, before the first part
of a page or between two of them, once or several times. The parts still show one set of figures: when
the state moved before it, the first part reads or computes the figures of its own moment and keeps them
for the frame's `as_of` as well, and the later parts read what the first one read (the figures of the
frame's moment, or those it kept), as long as they are stored (two minutes). So a page never shows
figures of two moments side by side and they are not computed twice. After that, or on the next page, a
part reads the figures of the moment. What is kept for an `as_of` is only ever read by the parts of a
page carrying it, never by a frame looking for stored figures nor by any other page; the code is keyed
with the application's secret key, so nobody can work out the code of a state to come, and `as_of` only
picks among figures of the last two minutes for the same filter, for everyone; it is never a way to see
more.

| Page | The frame (at once) | Its parts | What it stores in the shared page figures, and when that changes |
|---|---|---|---|
| Home, `/` (signed in) | Title, date and scope line, purpose line, the partner reporting heading and its links, freshness strip; no figure work and no Watch table | `reporting` (at once): Progress reports' tiles, the programme documents to follow up and the partners line. `indicators` (at once): Partner monitoring's tiles for this year. In *More on Home* (when it opens; open for superusers): `for-you` (at once, from Watch's own address; not drawn while Watch is off): the NeuroDB Watch card. `tasks` (at once): *Needs a decision* (Administrators and Management) and the daily review's digest. `since` (when near the screen): *Since <weekday>* and *Your tasks*. `standing` (at once): the Country overview's five key figures for this year and Home's sections, and the *Also* line. `programmes` (after the visible ones): *Programmes and sources* | Nothing of its own: `reporting` is computed at each request, as Progress reports computes it; `indicators` reads Partner monitoring's stored indicators (same key), drawn by the frame when stored; `standing` is the Country overview's build (same key), drawn by the frame when stored; *Programmes and sources* reads the country programme's build (10 minutes) and Compiler's summary. *Needs a decision*, the digest, *Since* and *Your tasks* are computed at each request. See *Home and the country overview* |
| Country overview, `/country-overview/` | Purpose line, filter bar, subtitle, freshness strip, in an ActivityInfo year the link to its databases | `kpis` (at once; first of the group `overview`): the key figure tiles. `figures` (when near the screen, once `kpis` is in): impact, value for money, delivery and assurance and the two progress blocks. `news` (after the visible ones): What's new and the forecast risk card | The overview build, 2 minutes per year, its ActivityInfo reporting year, sections, governorate and day, the same in every language; the filter bar's section names, 10 minutes; the freshness strip, 2 minutes. Each key holds the last finished sync run and the indicator flags, so a sync or a flag changes it at once. When the build is stored, the frame draws `kpis` and `figures` itself. See *Home and the country overview* |
| Daily review, `/daily-review/` | Title, scope line, purpose line with the review's time (from its schedule), section chips | `review` (at once, only when a daily review ever succeeded): the *Daily review* card. `review-more` (only when *Show N more findings* is opened): its folded findings, always those of the card above them | Nothing: the review's findings are read at each request. See *Daily review page* |
| Management brief, `/brief/` | Purpose line, filter bar with *Figures as of* and **Print**, the tab bar, the *Reference* block (lineage and data dictionary) | `brief` (at once; first of the group `brief`): the five tiles, *Things to decide* with the *Assign* links for staff, *Sections at a glance* and *Copy summary*. `sections` (when near the screen, once `brief` is in): ahead or behind, how sure, who and where, partners, money and action, with every chart of the page | The brief build, 2 minutes per year, sections, day and language (its labels are translated as it is computed), under the brief's own key: the section plans, the finding assignments and the ActivityInfo partner links (how many, and the last change), the latest daily review, the last finished sync run and the indicator flags, so an entry in the admin, a new review, a sync or a flag shows at once. It reads two overview builds, each stored as an overview build: the year's, the very one the overview stores for the same year and sections without a governorate, and the year before's, for the comparisons. The frame's lineage, 2 minutes, the same in every language, under the last finished sync run, the flags and the latest daily review; the filter bar's section names are the overview's. When the build is stored, the frame draws both parts itself: a brief opened again within two minutes is one request. See *Management brief* |
| Partner page, `/partners/<id>/` | Header (name, type, breadcrumbs), the *deleted in eTools* notice, the knowledge base documents | `datamart` (at once): everything from the key figures to the last list (profile, visits chart and assurance chips, *Partner reporting* with the eTools and ActivityInfo panels and, for those allowed, *Edit the link*, the eight lists). Inside it, `fmm` (once `datamart` is in, after the visible ones): the Monitoring insights panel, only when Monitoring insights is on and a field monitoring visit ever monitored the partner | Nothing: the partner's figures hold database records, which the shared page figures never store, so they are computed at each visit and an edit or a sync shows at the next one. See *Partner reporting: ActivityInfo and eTools, side by side* |
| Data health, `/data/health/` | Page heading, the *Sync jobs* heading with its *Stale after N hours* note, the heading and explanation of the eTools Datamart datasets panel | `jobs` (at once): one card per sync job. `databases` and `runs` (when near the screen): the ActivityInfo databases of the year the import serves and their last import (once ActivityInfo is an archive, those of its last year, state "Archive", with a note: "no longer imported" once the `activityinfo-data` job is switched off; while it is on, that the import still runs but NeuroDB no longer warns about it), the latest 50 runs. `datasets`, `power-bi` and `readiness` (after the visible ones): the eTools Datamart datasets table with its *Copy* and *CSV* buttons (they come with the table, so they are never offered before it is there), *Compared with Power BI* (its heading, explanation and, for administrators, the links to the admin are in the frame), the machine learning readiness check | Only the rows each eTools Datamart dataset holds (the *Rows held* column): an hour at most, the same in every language and for everyone, under a key made of the latest run of each job that writes those tables (eTools Datamart sync, eTools sync, Locations sync, eTools update clean-up), so a run of one of them that starts or ends has them counted again. The gazetteer (*locations*), which staff also edit in Admin → Locations, is counted at each visit, and so is everything else on the page. Inside a database transaction (NeuroDB Watch's AI look-ups read these figures in a read-only one) the stored counts are read too; when there are none the rows are counted there and nothing is stored. See *Data quality per dataset* |
| Partner monitoring, `/partner-monitoring/` | View tabs (Grid, Program Performance, Data quality, Map), the filter bar with its choices, the "your section" note | Grid: `summary` (at once; first of the group `pm`): the seven tiles, the status donut and the indicators by section; `grid` (at once, once `summary` is in): the indicators by section, PD and output with their months or years, its pages, the note when the list is cut. Program Performance (`view=table`): `sections` (at once; first of the group `pm`): the section tiles; `table` (at once, once `sections` is in): the page of rows in its sort, its pages, *Excel of the filter* and *CSV of the filter*. Data quality (`view=quality`): `quality-tiles` (at once; first of the group `pm`) and `quality-lists` (when near the screen, once the tiles are in). A change of filter, sort or section tile reloads the area under the filter bar (`#monitoring-grid`) with new placeholders; a page of the grid or of the table reloads its own part. The exports are answered whole | The filter's indicators (every row the three views read), 2 minutes per filter and day, the same in every language and for everyone; Program Performance's section tiles and table read the same for the filter without its sections (the table keeps the chosen ones, as before), one computation for both. The filter bar's choices, 10 minutes. Each key holds the latest run of each eTools job (eTools sync, eTools Datamart sync, Datamart backfill, Locations sync) and the indicator flags, so a sync shows at once and an ActivityInfo import does not count. The parts carry `as_of`. When the indicators are kept, the frame draws the view's two parts itself: one request. The Map tab is not in parts; its points (`/api/internal/partner-monitoring/map/`) are kept 2 minutes under the same jobs, the flags and the gazetteer. See *Partner monitoring and Progress reports load in parts* |
| Progress reports, `/partner-reporting/` (*By location* and *By disaggregation*) | Filter bar with its choices, the tab bar, the *By disaggregation* tab's own filters | `table` (at once): the tab's table, its pages, the note when the filter holds too many rows, the Excel and CSV links. A change of filter (HTMX) answers the table at once. *Reporting progress* and *Partners* are drawn whole, as before | The tab's rows (on *By disaggregation*, with the lines of every location), 2 minutes per tab, filter, tab filters and day; the filter bar's choices, 10 minutes; the same in every language and for everyone, under the eTools jobs' latest runs and the indicator flags, as partner monitoring. When the rows are kept, the frame draws the table itself: one request. See *Partner monitoring and Progress reports load in parts* |
| Monitoring insights, Map tab, `/fmm/?tab=map` | Header and Export menu, filter bar, reference line, chips, data notes, the key figure tiles, the tab bar; a filter without visits shows the tab's empty state and no part. The other tabs stay one request each | `map` (at once): the map, its legend, *Visits on the map*, *PD locations not yet visited* and *Visits without coordinates*, with the map's configuration. A swap of the results (tab bar, filter bar, the planned places switch) brings the map drawn in, in one request | The map (`geo.map_points`), 10 minutes, per filter, planned places shown and language (the popups' dates are translated), under the Monitoring insights stamp (last refresh and rules version, the day, the NeuroDB action points, the last eTools, Locations or clean-up run, the gazetteer's places), so a refresh, one of those runs or a place edited in admin → Locations shows at once. Also the one pass over the visits (under the *Score settings* thresholds too), the entity table and the rule counts that the other tabs read. Never drawn by the frame: the map takes about 90 ms to draw even when stored. See *Monitoring insights* |
| Country programme, `/country-programme/` | Cycle selector, the cycle's panel (years, share of the cycle elapsed, summary, documents, *Edit in the admin* for staff), the note on AI-suggested indicators; without a cycle or without indicators, the empty state and no part | `summary` (at once; first of the group `cpd`): the six key figures, *Where the CPD indicators stand* and the outcomes at a glance. `outcomes` (when near the screen, once `summary` is in): each outcome with its outputs, indicators, programme documents and partner reporting; a link to `#outcome-<id>` asks for it first | The cycle's build, 10 minutes per cycle, day and language (the kinds of the linked sources are translated), under a key that holds the cycle and what staff enter by hand that its figures read (the cycle, its outcomes, outputs, indicators, milestones, typed values and linked sources, and how the linked ActivityInfo master indicators are set up, down to the indicators they add up and those indicators' ActivityInfo field and database: one digest of the rows themselves, so an edit in place shows at once), the Makani and Dirasa card versions chosen in Admin → *Power BI cards* (the children enrolled a Compiler source gives) and the last finished sync run and the indicator flags. When the build is stored, the frame draws both parts itself. See *Country programme* |
| Youth programmes, `/youth/` | The filter bar whole (its choices are read in the database from a few lists of the year's figures, never by decoding them, and kept 10 minutes), the line saying when Compiler's figures were read; without figures, the empty state and no part | `summary` (at once; first of the group `youth`): the five key figures, the line saying whether Compiler counted the rows of Power BI's view, and the youth indicators with their eTools indicators. `details` (when near the screen, once `summary` is in): the breakdowns (the registered young people by home governorate among them, from payload format 2), *Compiler and eTools, side by side* and the programme documents in Compiler | The year's build, 10 minutes per year's figures (their row and when they were read), filters, day and language (the breakdowns' names are translated), under a key that holds the year's links to eTools (a digest of the rows: a link confirmed with the admin's action, edited or added shows at once), the card versions chosen in Admin → *Power BI cards*, the last finished sync run and the indicator flags; the filter bar's choices, 10 minutes per year's figures, the same in every language. When the build is stored, the frame draws both parts itself. See *Youth programmes* |
| Donor page, `/donor/` (not a staff page) | The bar with the year, the tabs, every heading and card of both tabs with grey shapes of the size of their charts, the date of the data; for an administrator's preview, the preview banner | One part, `data`, answered as JSON and drawn by the page's script (`donor.js`): every figure of both tabs. Its address carries the year and, in an administrator's preview, the account and the tab; for a donor it is always the donor's own account, whatever the address says | The page's figures, 5 minutes per account (its donors, grants and whether partners are named), year and day, the same in every language, under the last finished sync run and the indicator flags; they were kept per worker before. When they are stored, the frame carries them and nothing is fetched. See *Donor access* |
| Document review, `/knowledge/review/`: the views *By document* (Findings), *Themes* and *Repeated findings* (Synthesis); the other tabs and views are drawn whole | Breadcrumbs, *Verified only*, purpose line, the tab bar, the view's links, filter bar and filter chips; for *Repeated findings* the panel's heading and explanation | `findings` (at once): the page's ten documents, each with its key statements and findings and, for editors, *Accept all*, *Add a finding* and the review buttons; its page links reload this part only. `synthesis` (at once): the *Themes* panel (each theme with its five best findings, its link and, when AI is on, *Write a paragraph* with the person's quota) or the repeated findings' groups. A tab or filter change (htmx) answers with the themes in it, as before; *By document* answers its placeholder and the part follows (drawn in that answer it took twice as long as before), and the repeated findings their placeholder unless they are stored | Only the repeated findings: an hour at most, per batch and *Verified only* (each person's switch is in the key), the same in every language and for everyone, under a key made of what they are read from: the documents in a batch (their batch, review status and analysis time, and the last change of any, a title or a file changed in the admin), the findings (how many, the newest, the last edit, every verdict given), the batches (names, archiving) and the last finished run of a *Document review* job (*Locate document findings again* rewrites the findings' pages, places and evidence without an edit time). A verdict, an edit, *Accept all*, a document taken out or marked reference, a finished analysis or a finished *Locate document findings again* shows at once. When they are stored, the frame draws them itself. *By document* and *Themes* are computed at each request. See *Document review* |

Every other page is still drawn whole. A page gets parts only when it takes over 0.3 seconds to answer
whole, cold, on a copy of the data twenty times today's size (Monitoring insights at production size),
for an administrator and for a viewer with a section; below that, parts would only add requests. Of the
pages that stay whole, the ActivityInfo databases page and each database, the Neuro and HPM reports, the
programme documents' indicators tab, each programme document (page and pop-up) and field monitoring
were measured that way and each answered whole in under 0.3 seconds. Field monitoring's *HACT visit
plan* tab, added later, was measured the same way and stays whole too: its figures take about 30 ms
(40 ms with what the tab adds) and the whole tab 0.2 seconds (median of 7 cold runs; the slowest 0.3
seconds), most of it drawing the list (see *HACT visit plan*).
So were, and stay whole for the same reason (medians, cold): the action points page (0.11-0.13
seconds), the *Reporting progress* and *Partners* tabs of Progress reports (0.11-0.16), the three tabs
of Funds (0.06-0.11; the contributions and programmes tabs now read only the filter's choices, not every
fund line), the *Map* view of partner monitoring (a frame already: 0.07 seconds, its points come from
their own address, see the table), the *Insights*, *Quality*, *Analysis* and *Records* tabs of Monitoring
insights (0.14-0.27; the panels they load when reached or opened, such as the rule score trends, the
entity table or *Show all*, now come alone instead of with the whole tab), Makani (0.18-0.29) and Dirasa
(under 0.1), the knowledge base search (the index it ranks on is now stored with each passage: the search
takes 30-50 ms for a common word, where it took 0.7-1.8 seconds), the document review's *Documents* tab and
*Index* view (0.1-0.2, since their counts are taken by subqueries) and its other tabs and views, What's new
and For you (under 0.21).

**When a part fails.**

1. Press **Try again** on the block. A failure that came from a deployment or a restart (502, 503,
   504) has usually been retried once by itself already.
2. If it fails again, find the request in the web container's log (App Service *Log stream*, or Log
   Analytics). A part request is the page's address with `_part=` in the access log, for example
   `"GET /?section=&year=2026&as_of=5f0c9e1d2a7b4c38&_part=figures HTTP/1.1" 500`,
   `"GET /brief/?section=&year=2026&as_of=a5d1e0b7c3f94286&_part=sections HTTP/1.1" 500`,
   `"GET /partners/12/?_part=datamart HTTP/1.1" 500`,
   `"GET /data/health/?_part=datasets HTTP/1.1" 500`,
   `"GET /partner-monitoring/?section=&year=2026&as_of=3b8e1f0d6a2c9475&_part=grid HTTP/1.1" 500`,
   `"GET /partner-reporting/?tab=locations&_part=table HTTP/1.1" 500`,
   `"GET /fmm/?section=&tab=map&_part=map HTTP/1.1" 500`,
   `"GET /country-programme/?cycle=2&as_of=0c41d2a9e5b7f816&_part=outcomes HTTP/1.1" 500`,
   `"GET /youth/?year=2026&as_of=7e2a90c4d1b3f658&_part=summary HTTP/1.1" 500`,
   `"GET /donor/?year=2026&_part=data HTTP/1.1" 500` or
   `"GET /knowledge/review/?tab=synthesis&view=repeated&batch=3&_part=synthesis HTTP/1.1" 500`,
   and the error with its traceback is logged next to it ("Internal Server Error: /brief/", the path
   without the query). A **202** on a part request is normal: another request was computing the same
   figures and the browser asked again.
3. In Application Insights, each part request carries the custom dimension `neurodb.part` (the part's
   name; the request's `url` tells the pages apart), and the exception of a failed one belongs to the
   same operation, for example
   `requests | where tostring(customDimensions["neurodb.part"]) == "figures" and success == false`.
4. To tell a failing block from a browser or network problem, add `_eager=1` to the page's address:
   the page then draws its blocks itself, in one request (what *Show the figures* does without
   JavaScript; a block that comes from another address, such as Home's *For you* card, is
   not drawn there: with JavaScript the browser still loads it on its own, without JavaScript it stays
   empty), and a block that fails fails the whole page with the same error.
5. Figures that look out of date rather than failing: **Empty the stored page figures** (above).

Nothing here needs a shell: the table comes from a migration that runs at start-up, it is emptied
from the admin and cleaned up by itself, and the logs and Application Insights are read in the Azure
portal.

**Server settings.** NeuroDB compresses its own answers (`CompressMiddleware` in
`neurodb/web/middleware.py`, Django's gzip with its protection against BREACH, right after WhiteNoise):
App Service does not compress what a Linux container sends, and a part can be large (the daily review's
folded findings are about 266 KB). It leaves out event streams (the chat answers, written line by line),
downloads (attachments), streamed answers and what is compressed already (images other than SVG, such as
the library covers, PDF, ZIP, fonts, audio and video), which gzip would only make the server work on for
nothing; static files come from WhiteNoise, which serves its own compressed copies. The pages' own scripts
load under the names collectstatic gives them (with the hash of their content, kept by browsers for good), the
scripts they import from one another too, through an import map in `base.html` (`module_map` in
`neurodb/web/templatetags/ui.py`): a page of a new release never runs with a script of the one before, which
a browser could otherwise keep for a minute under its plain name. A worker of gunicorn
(`config/gunicorn.conf.py`) restarts after `max_requests` = **8000** requests, with a random spread of
up to 800 (`max_requests_jitter`) so the workers do not restart together; it was 2000 and 200. A page in
parts sends several requests per visit (the overview: its frame and up to five parts, plus the folded
findings when they are opened; the brief: its frame and two parts, or the frame alone when its figures
are stored; the partner page: its frame and one or two parts; Data health: its frame and six parts;
partner monitoring: its frame and two parts per view; the other pages of the table: their frame and one
or two parts; fewer when the frame draws stored figures itself, as the table says),
and each restart loses the worker's own caches and pays its slow first request again. The web container
still runs 3 workers × 4 threads; the work is mostly CPU, so more workers (`WEB_CONCURRENCY`) help only
with more vCPU. Each part is its own request in Application Insights: watch the volume of requests there
after a release that converts a page.

**Under load** (measured on a copy of the data twenty times today's size, 4 vCPU, 3 workers × 4 threads,
the shared page figures emptied): with 8 people opening the pages converted in step 3 at the same moment
(seven staff each opening the eleven converted views one after the other, a donor the donor page), no
request failed and the round took 12 seconds, against 39-45 seconds for the old pages. A frame took
90-170 ms at the median but 500-650 ms at the 95th percentile, above the 300 ms aimed for (frames with no
block drawn in: 500-640 ms; the same frames take 20-150 ms for one person). Frames wait behind the blocks
being computed in the same workers: that work is CPU and Python runs one thread of a worker at a time
(the longest: the repeated findings 4-6 s, the donor page 3-5 s, the country programme 2-3 s). With
nothing emptied the 95th percentile is about 500 ms (370 ms for frames with no block drawn in): a frame
that draws stored blocks itself is the whole page. More vCPU, with a worker per vCPU, is what brings it
down.

## eTools Datamart

**Running the sync now.** Admin → *Data and sync* → *Import and sync runs* → **Sync eTools Datamart now**
(administrators): choose *Partners and programme documents* (minutes), *Everything* (up to two
hours) or *Only the datasets ticked below* (for example `locations` alone after a gazetteer fix).
It runs in the background in the web container; each dataset appears in that list as it
finishes. Only one Datamart sync runs at a time (a database lock), whoever starts it. A deployment
restarts the container and kills a sync in progress: its run stays *Running* until the next
**Sync eTools Datamart now**, which sees that nobody holds the lock, closes the run as *Failed* ("Cut off"),
however old it is, and starts. The scheduled job `etools-datamart` (admin → Scheduled jobs) runs it every night.
From a shell with the same settings: `python manage.py sync_etools_datamart [--only a,b]`.

**One failed dataset is not the whole sync.** Each dataset is its own run in *Import and sync runs*, and
a dataset that fails does not stop the others: its last good rows stay as they were. NeuroDB therefore
reads the sync dataset by dataset, from the latest finished run of each. *Needs attention* and NeuroDB
Watch name the datasets whose own latest run failed ("eTools Datamart sync: partners failed at its last
run (1 of 50 datasets)."; past three names, "… and 2 more"; "every dataset failed at its last run" only
when all of them did; the line links to the failed run, or to the runs of the datasets it names), and
the line goes, and the Watch point closes as fixed, only once that dataset itself succeeds again:
another dataset succeeding later does not clear it. The admin home's *Data jobs*, the *Last run* column
of *Scheduled jobs*, the help chat and the Data health job card read the same: *Succeeded*,
*Succeeded with errors* (no dataset failed, but one wrote with row errors; it is named), *2 datasets
failed* or *Failed* (every dataset); while only a diagnostic dataset has run, they show that run as it
is. `datamart_etl_status` (the Datamart's own table-refresh log) and `etools_usage` (eTools users and
logins per month) are **diagnostic**: no page, figure or check uses them, only Ask NeuroDB's general
eTools look-ups can read them. One that fails is shown on Data health (its row in the datasets table,
marked "diagnostic: no page or check uses it", with its error; a line on the job card) and in the list
of runs, but it is never a *Needs attention* line, a Watch point or a daily review finding. A Watch
point opened for the table-refresh log before NeuroDB read the sync dataset by dataset closes as changed
("only a diagnostic eTools Datamart dataset still fails") while the log still fails, not as fixed. To
sync one dataset again, open its run and press **Sync this dataset again**, or press **Sync eTools
Datamart now**, choose *Only the datasets ticked below, in their usual order* and tick it. The failed
run's page says, under *What the error means*, that only this dataset stopped and whether a page uses
it. *What the error means* (also in the list's *Error* column) reads: for HTTP 502, 503 or 504,
"eTools Datamart was unavailable: try again later."; for HTTP 500, "eTools Datamart answered with a
server error (HTTP 500), even after NeuroDB asked again. If the next run fails the same way, tell the
team that runs eTools Datamart." (NeuroDB asks again on 429, 500, 502, 503 and 504 before it gives up;
any other 5xx reads the same without "even after NeuroDB asked again"); for 429, "eTools Datamart
refused more requests for now (too many in a short time): try again later."; for 401, that the credentials
were refused. A 403 is told by what shows the account was signed in (`runfacts.refused`): the answer's body
(Django REST framework's "You do not have permission to perform this action.", or its sign-in words such as
"Invalid username/password."), else the run of another Datamart dataset nearest in time (just before or just
after it): when that one read, the sign-in worked, so the account is signed in but not allowed this dataset:
"The account NeuroDB uses for the eTools Datamart is not allowed to read audit/financial-findings-all: ask the
eTools Datamart team to grant it.", and the next sync fails the same way until they do (24 Oct 2026,
`audit_findings`, while the audits and spot checks read). A body naming the sign-in reads as the credentials
refused. When nothing tells them apart (the nearest run failed too, or there is none), it says both: "The eTools
Datamart refused NeuroDB's sign-in or <path> (HTTP 403): if every dataset fails this way, ask the team that runs
NeuroDB's hosting to renew the credentials; if only this one, ask the eTools Datamart team to grant the account
access to it." Raw eTools data says "(eTools Datamart answered 403: the account NeuroDB uses is not allowed to
read it; ask the eTools Datamart team to grant it)" with *Complete?* "No: not allowed to read it (ask the eTools
Datamart team)" (else "(eTools Datamart answered 403: sign-in refused)" or "(... it refused NeuroDB's sign-in or
this dataset)", and "No: the last sync failed"), and Data health says the same sentence as the run page above
the error's text. Run a job → *Probe a Datamart dataset* tells them apart the same way (the latest run of another
dataset); a 403 on the business area request alone, while the same probe read the dataset with no filter or the
country filter, is the account not let ask for that business area: check the code, then ask the eTools Datamart
team to give the account that business area.

**Finish the eTools update.** The work an update leaves for after the site has started runs as the
job *Finish the eTools update* (`datamart_backfill`, recorded as *eTools update clean-up* in this
list): it copies PRP's indicator report id and the CP output from each partner report row synced
before those columns existed (and gives each PD indicator its CP output), and removes from the
records kept the e-mail addresses, phones and mobiles older syncs stored, the UNICEF focal point
entries that were e-mail addresses and every `submitted_by` holding one. The schedule
`etools-update-cleanup` starts it once the site is up (usually within a minute, at most 15 minutes)
and the job switches that schedule off once a run has gone through everything. It writes only what
is left, in batches committed one by one, reading each table once, so a run cut short (a restart)
carries on next time and *Run a job → Finish the eTools update* can be pressed at any time. It holds
the Datamart sync's lock, so the two never run together. Started while a sync runs, it does nothing
then: its schedule (switched back on if it was off) starts it at the first quarter-hour after the
sync has finished; *Scheduled jobs* shows "waiting: the eTools Datamart sync is running". A sync
started while it runs (the nightly one, or **Sync eTools Datamart now**, which then says so) waits
for it and runs as soon as it has finished. Until it has run, partner report rows synced before the
update show no CP output. The next sync fills those inside its reporting window (`partner_reports`
below); only this job fills the older ones.

The nightly eTools sync reads the **eTools Datamart** (`https://datamart.unicef.io`, the new eTools
APIs) with HTTP basic authentication: `ETOOLS_USERNAME` / `ETOOLS_PASSWORD`, from the Key Vault
secrets `etools-username` / `etools-password`. Nothing else holds the credentials: they are not in
the image, the repository, the logs or the `SyncRun` errors, and the client only sends them to the
Datamart host (a pagination link to any other host is refused). Every request is limited to
`ETOOLS_DATAMART_COUNTRY` (default `Lebanon`; a blank value also means `Lebanon`, never every country).

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
| `partner_reports` | `prp/datareport/` | partner progress reports per indicator and location (partner monitoring, partner reporting, programme and partner pages), periods starting in the last `ETOOLS_DATAMART_REPORTING_YEARS` years and, while `ETOOLS_DATAMART_REPORTING_FROM_PD_START` is on (the default), since the start of every running programme document (`details.read_from` shows the date read from, `details.feed_from` where the rows held start), read by business area once the Datamart proves it applies the filter (*A business area filter is proven before it is used*, below; the Datamart ignores `country_name` here, so with no filter proven every country's rows are read and Lebanon's kept), read in windows of reporting period start when a cut is suspected (*A large read is read in windows*, below; `details.whole_from` the first start held whole); the indicator report id, CP output and eTools CP output id get columns, and each PD indicator its CP output from its latest report naming one (`details.cp_outputs`); then the labels of the disaggregation values the reports name (`details.disaggregation_labels`, below) |
| `tpm_activities` | `tpm-activities/` | the PD, place and date of each TPM visit activity (field monitoring and programme pages) |
| `staff_visits` | `travel-activities/` | UNICEF staff trip activities: programmatic visits, spot checks, meetings (programme and partner pages) |
| `planned_visits` | `interventions-planned-visits/` | programmatic visits planned per PD and quarter (programme page, HACT visit plan) |
| `hact_history` | `hact/history/` | each partner's HACT year: cash transfers, risk rating, visits, spot checks and audits done against required (assurance and partner pages, HACT visit plan) |
| `pd_activities` | `interventions-activities/` | PD workplan activities with UNICEF and partner cash (programme page) |
| `audit_results`, `audits`, `spot_checks`, `micro_assessments`, `special_audits` | `audit/results/`, `audit/audit/`, `audit/spot-check-findings/`, `audit/micro-assessment/`, `audit/special-audit/` | add risk rating, high-priority findings, key control weaknesses and amounts to the engagements, matched by reference number |
| `audit_findings` | `audit/financial-findings/` | financial findings of each engagement (engagement, assurance and partner pages) |
| `fm_questions`, `fm_options`, `fm_programme_activities`, `offices`, `sections` | `fm-questions/`, `fm-options/`, `fm-programme-activities/`, `office/`, `reports/sections/` | kept whole in `datamart.DatamartDocument` (below); they feed Monitoring insights: the checklist answers and their options, the programme activities and CP outputs of each visit, and the office and section names (see *Monitoring insights*, *Data and its keys*) |

Pages: *Funds* (funds reservations, Partner contributions, By PD and grant), *Partner reporting* (each report opens with its indicators by location),
*Assurance* (with HACT compliance per partner and an engagement page with its findings and action
points), *Field monitoring* (with the *HACT visit plan*, below) and *Action points*, plus sections on the programme, partner and donor
pages. The PRP endpoints other than `prp/datareport/` carry no country field, so they are not read.

Rows link to programme documents by eTools id or reference number and to partners by eTools id or
vendor number; `details.not_linked` of a run counts the records whose PD or partner is not in
NeuroDB. A record that names another country than `ETOOLS_DATAMART_COUNTRY` (a `country_name`
filter the Datamart ignored; names compared with case and spaces aside) is never written, in these
tables, the partner and programme tables and the locations alike: `details.other_country_skipped`
counts them. In a `datamart` table, a read that returns this country's records also deletes every row
an earlier run wrote for another country, whatever its date (`details.other_country_removed`); the
partner, programme document, location and site tables keep such rows (only their raw copies kept for
Ask NeuroDB are replaced). The partner and programme tables are updated, never emptied. Each `datamart` table is
replaced by a complete read (rows the Datamart no longer returns are deleted), except when the
Datamart returns nothing at all, or only records of another country (`details.empty_response`, with
`details.other_country_skipped` in the second case; rows an earlier run wrote then stay until a read
returns this country's records), which usually means a wrong country name.
A read that returns fewer distinct records than the Datamart announced on its first page (the `count` of
the paginated list, kept as `details.datamart_count`; records left out as another country's count as read,
and a record returned twice counts once), or that returns a record twice that it cannot prove the Datamart's
own copy (rows moving on the Datamart while its pages were read: a record returned again may stand for one
never returned, whatever the counts, `details.duplicates_read`; *Records the Datamart sends twice*, below),
removes nothing either: it would otherwise delete history until the next complete read. The run then ends *Succeeded with errors* with `details.removal_skipped` and
`details.read`, Data health says "read N records of the M the Datamart announced: none was removed" (or
that the Datamart returned N records twice), and the next complete read removes what the Datamart no
longer returns. A count of 5,000 or more that is a multiple of 1,000 (`CAP_FLOOR`, `CAP_ROUND` in the sync)
reads as a limit of the Datamart's answer, not what it holds: such an answer is read in windows (below), and
one that cannot be removes nothing, whatever it read. (The partner reports reads of 20 and 21 Oct 2026 were
whole: 777,535 and 780,905 records announced, every one read, every country's rows, about 10,000 of them
Lebanon's: the Datamart answers no limit near 10,000.) A failed page fails the run before anything is removed. Every run also records
`details.held_before`, the rows of its scope NeuroDB held before the read: a large `removed` reads
against it. **A large removal waits one night**: a whole read that would remove more than half of the rows
held in its scope, and at least 1,000 (`HOLD_FLOOR`), removes nothing that night (`details.removal_held`, the
rows kept; *Succeeded with errors*; Data health: "N rows the Datamart no longer returns are kept one more
night: more than half of those held; the first whole read a night later removes them"); a run removes them
when its own read is whole and the finished runs of the dataset have held the removal, one after the other,
since 20 hours ago or more (`HOLD_HOURS`): the next night's run. A sync pressed again minutes after the
hold (*Sync this dataset again*, *Sync eTools (Datamart)*), while the table is still reloading, holds it
again. Counts that agree with
what was read cannot catch a Datamart table reloaded while it is read (its ETL empties and refills tables);
one night's wait keeps such a read from emptying NeuroDB's copy. The records kept whole (below), the raw
copies of the partner, programme document, location and audit records among them, follow the same rules.

**Records the Datamart sends twice** (`windows.Reading`, `windows.Copies`). One answer of the Datamart (a
page: one query) never lists a row twice. So a record whose every copy came identical in one answer is held
that many times by the Datamart: its own copies, proven (`details.copies_proven`: `twice`, of which
`same_page` and `asked_again`). The copies of a record that came on two pages side by side are asked again
in one small request in the read's own query and order, whose single page covers both places (`page_size`
the smallest that holds them on one page, at most the read's own; `page` from the first copy's place; at most
20 requests a read, `CONFIRMS`, `details.confirm_requests`), only when they can make the read whole; when its
answer holds as many identical copies, they are proven too. A plain read (not in windows) whose every record
twice is proven is whole when the records it received, copies included, are every one it announced
(`datamart_sync.read_whole`: distinct + proven copies ≥ the count, no copy left unproven, no cut page, no
round count), so what the Datamart no longer returns is removed. It can never remove a record that is
missing: each record received came at most as many times as the Datamart holds it (a page holds no more copies
than there are rows), so a record missing would leave the sum short of the count. Like any whole read, it
cannot see a record deleted and another added during the read with the count unchanged. Raw eTools data
counts them with what NeuroDB received ("Not kept: … 1,939 the Datamart sent twice (identical)"; *Complete?*
Yes), and Data health, the run page and checks 10 and 11 say "The Datamart sent 1,939 records twice
(identical), every copy in one of its answers (on one page): it holds them twice, as one answer never lists a
row twice. NeuroDB keeps one of each." (a note, not a warning; in Data health beside why a read not whole for
another reason removed nothing, as "read 7 records of the 11 the Datamart announced: none was removed"). Such a
read records its distinct records (`details.read`), so "Received" counts each once, and a record of another
country sent twice is left out once (`other_country_skipped`). A record twice that no answer proves (copies on
pages too far apart, copies that differ, a page shift: the request asked again then holds it once) leaves the
read not whole, as below; when such records are as many as those that did not come, Raw eTools data's
*Complete?* says "No: 1,939 were sent twice and may stand for others that did not come; nothing was removed" and
the source's page says why NeuroDB cannot count them as copies. (25 Oct 2026: reports/indicators announced
8,899 and sent 6,960 distinct records, 1,939 short night after night; PRP's progress reports 74 short. Whether
they are proven shows on the next night's run: once they are, the first removal of the rows their hash keys
left behind can be large, waits one night as any removal of more than half of the rows held, and Ask NeuroDB's
counts of these datasets then fall to what eTools sends.)

**Pages that shift during a read** (`windows.Reading`). A read whose records came twice, not proven
(`details.duplicates_read`), records `details.pages_shifted` (`twice`, and `missing`: announced but not read,
the copies proven aside).
The counts cannot tell rows moving between pages (the Datamart's default order is not stable, and as many
others then never come) from rows the Datamart holds twice, so Data health, the run's page and checks 10 and
11 say both: "74 records came twice (identical) while the Datamart's pages were read: rows moved between
pages, and then as many others did not come, or the Datamart holds them twice. Nothing was removed; the next
read asks a stable order." (PRP's progress reports, 21 Oct 2026: 150,295 announced, 150,221 distinct read, 74
identical records twice; "identical" is said for records with no id, told apart by their content). The next
read of the dataset, and every read after one that asked an order, first proves a stable order
(`windows.order_wanted`: a read whose every record twice was proven asks none, as it was whole in the
Datamart's own order; `windows.prove_ordering`, three requests of 50 records: pages 1 and 2 of `ordering=id`
share no record and, when the records carry the key, are in order; page 1 of `ordering=-id` shares none with
page 1 and is in descending order; then the same by the dataset's record identity, below; a dataset of 100
records or fewer is not asked. Pages 1 and 2 that share records are asked once more as one page of 100: when
that one answer lists each record as often as the two pages together, the records they share are copies the
Datamart holds, not a shift, and the order stands, `shared_held`) and asks every page of its read in that order (`details.ordering`: `{"key": "id"}`, or `{"key": "",
"why": ...}` with `ignored`, `refused`, `failed` or `too_small`; a refused or failed request never fails the
run, and the words then say the Datamart takes no stable order). An order proven on records that do not carry
its key (`details.ordering`: `{"key": "id", "carried": false}`: the pages share no record, but nothing in them
shows an order, as a random one would pass too) is asked, but not taken as stable: records that still come
twice are said as pages that shift ("…; the records do not carry id, so the order asked by id cannot be
checked, and the next read is in the Datamart's own order again."), and the next read drops it, back to the
Datamart's own order, where copies side by side on one page are proven (at worst the two orders take turns,
night by night; neither removes a record missing). Records that still come twice, not proven, in a stable order checked on the key the records carry
are most likely held twice (a unique order does not move the rows its ETL rewrites, and with ids that
grow a row added during the read comes last): `details.held_twice` replaces `pages_shifted`, and the words are
"The Datamart returned 74 identical records twice in a stable order (by id): it likely holds them twice; tell
the eTools Datamart team. Nothing was removed." Such a read stays not whole, as before, so the progress
reports keep skipping their removal until the Datamart team removes the copies. A read in a stable order that
comes back whole removes what the Datamart no longer returns, as any whole read; for the progress reports its
first removal can be large (every row a changed hash-keyed record left behind since the last whole read), and
it waits one night when it is more than half of the rows held and at least 1,000, so check 11's counts of
progress reports held can drop on that night. Run a job → *Probe a Datamart dataset* asks the same three
requests after such a read (below). The page of every eTools Datamart run says what its read found in plain
words (*What the read found*, above its details: why it was read in windows, the business area filter it took
or why none, and records that came twice); "—" for any other run.

**A large read is read in windows** (`neurodb/integrations/etools/windows.py`, `Reading`) **only when a cut
is suspected**: its first page announces 5,000 records or more (`WINDOW_RECORDS`) and either a round count
(`details.windows_why` = `round_count`) or the dataset's previous finished run looked cut
(`datamart_sync.cut_sign`, the first that applies: `cap_suspected`, a round count read once, the ends test or
no count; `whole_count_capped`, its windows held more than the Datamart's count; `pages_cut`, a page shorter
than the first of its request, not the last; `windows_cut`, a window read to its end holding fewer than it
announced; `read_short`, a read made once that returned fewer records than announced, the records that came
twice counted with it, as records twice with as many others missing is pages shifting, not a cut, and only
when the run recorded `read`). Size alone never windows a read: every other read is made as before, the first
page and then every `next` link, with the same rule as above. A windowed night that was not whole for a
reason that shows no cut (a count that changed, records twice, a plan stopped short) records no cut sign, so
the next night reads plain; if a cut comes back on every plain read, windowed and plain nights alternate,
and nothing is lost, as a read not whole removes nothing. A plain read of 5,000 records or more also asks,
right after its first page and before the rest, the first and the last record by id (the *ends test*, one
record each): when the Datamart takes the ordering and either was not returned by the read, its answer was cut
with a count that is not round (`cap_suspected` = `ends_missing`, nothing removed, the next night tries
windows; Data health: "the Datamart's last record by id is not among those it returned: its answer was cut,
none was removed, and the next read tries windows", and only after a read that tried windows and was read
once, as no window filter was proven: "…: its answer was cut, none was removed, and it could not be read in
windows (the Datamart ignores the window filters): tell the eTools Datamart team");
records added during the read are never taken for missing ones, and there is no verdict when the records carry
no id or the ordering is ignored. A page that comes back shorter than the first of its request, not the last,
makes the read not whole whatever the counts (`details.pages_cut`; Data health: "N pages came back shorter than
the first while the Datamart's answer was read: it was cut, none was removed, and the next read tries
windows" for a read of 5,000 records or more, and "…: it was cut and none was removed; the next complete read
removes those it no longer returns" for a smaller one, read plain again). A read suspected cut is read in windows, ranges that each announce fewer than 5,000, once the
Datamart is proven to apply the window filter: a request that should find nothing (a start date from 2999 on, before 1900, an id from 10^15 on, below 1:
one record each) finds nothing. The partner reports are windowed by reporting period start (calendar
years from the date they are read from, then one open window, each split at its middle while it announces
5,000 or more, a one-day window split by id), every other dataset by id (from 1 to a top found by asking
whether records lie above it), and a dataset whose Datamart has `__gte` and `__lte` but no `__lt` is
windowed with `__lte` (the day or the id before). A window that announces nothing is not read (its count
answered it). Each record must fall in its window (a start date more than one day outside it: a time zone
does not count), each window must give what it announced with each record once, and its read's first page
must announce what its count did (a row added between the two is caught there); at the end the whole
count is asked again (`details.whole_count_end`): a different count means the Datamart changed during the
read (`details.changed`). When the first count is a round limit, the end count is the same limit whatever
happened, so the windows' own counts are the check (`details.change_checks`). Windows that add up to less
than the whole count are not whole (`details.windows_short`); more, with nothing outside, means the
Datamart's own count was cut (`details.whole_count_capped`) and the read is still whole. Only a read whose
every window is whole removes what it did not return; otherwise nothing is removed and the run ends
*Succeeded with errors*. The plan stops after 100 windows (`MAX_WINDOWS`) or 250 one-record requests
(`MAX_COUNTS`, `details.count_requests`): the windows then planned are read, and the read is not whole.
A dataset whose filters are not proven (the Datamart ignores them, or refuses them) is read once, as
before, with the ends test, and is not whole when its count is round (`details.cap_suspected` =
`windows_unproven`). The partner reports, read country-blind (about 780,000 records of every country), cannot
be planned in 100 windows of fewer than 5,000 (500,000 at most), and on 21 Oct 2026 no window filter, by date
or by id, was proven on them: for them a cut sign buys the proof requests and a plain read with its ends test,
nothing more, unless their business area filter is proven (below). A list with no count whose
answer stops on a full page of 5,000 records or more is suspected too (`no_count`). A dataset whose
records carry no id cannot be windowed by id: it is read once with the cap flag. The run's details keep
`datamart_count` (the whole count of the first page: never the last window's, which `client.last_count`
holds after a windowed read), `read`, `window_bound` (`reporting_period_start_date`, `id`, or "" when none
was proven), `windows` (the first 60: from, to, announced, read, pages, outside, whole), `windows_read`,
`windows_announced`, `windows_not_whole` and the first three of them, `outside_window`, `windows_why`,
`windows_cut` and the flags above (a plain read records none of these, only `datamart_count`, `pages_cut`
and the ends test's `cap_suspected`): counts, dates and ids only. Every dataset's run records them, with the
stable order asked and the records that came twice, the locations, sites, partners, programme documents,
budgets, agreements and the engagements' details (`audit_results`, `audits`, `spot_checks`,
`micro_assessments`, `special_audits`) included, so after a cut their next read of 5,000 records or more
tries windows, and once a stable order is proven they keep asking it. Data health says "read in 14 windows: the
Datamart announced 31,240, NeuroDB read 31,240 (the last read looked cut)", "the Datamart announced exactly 10,000 records, a limit of its answer, not what it
holds: NeuroDB read it in windows" (or, read once: "…: none was removed, and it could not be read in windows
(the Datamart ignores the window filters): tell the eTools Datamart team"), "2 windows not read whole (1
Jan 2026 → 1 Apr 2026: 5,000 announced, 4,500 read): none was removed; the next complete read removes those
it no longer returns" and "the Datamart's count changed during the read (31,240, then 31,300): none was
removed". Datasets that can pass 5,000 records for one country office over the years: `partner_reports`,
`pd_indicators`, `prp_indicator_reports` (once its business area code is a real code, not a country's
name: *Every other endpoint*, below), `fm_questions`, `attachments`, `staff_trips`, `staff_visits`,
`etools_usage`, and, when they grow, `action_points`,
`intervention_locations`, `tpm_activities`, `funds_reservations`, `cp_indicators`, `psea_answers`,
`locations` and `location_sites`; no flag is needed, each windows itself when its first page and its last
run say so. A windowed partner reports read of about 100,000 rows makes about 35 one-record requests besides
its pages; a plain read of any size makes two (the ends test).

**A business area filter is proven before it is used** (`datamart_sync.prove_area_filter`,
`catalogue.AREA_FILTERS`). The Datamart ignores `country_name` on `prp/datareport` (the partner reports: about
780,000 records of every country read for about 10,000 of Lebanon's) and `business_area_code` on
`sources/prp/unicefprogressreport` (PRP's progress reports), so neither dataset takes a business area filter on
trust: each night, the first rule that applies decides (`details.area_filter.why`): no business area code is set
or found (`no_code`; for the partner reports the code as *Every other endpoint* resolves it, without asking the
Datamart's workspaces); a row held in the sync's scope names no code or another one (`held_not_named`: a filter
could never return it, and a whole filtered read would remove it); no read with no filter has finished in the
last 7 days (`weekly`, `UNFILTERED_EVERY`: a read that failed part-way is not one; the seventh night counts
whatever minute its run starts, `UNFILTERED_DUE`, a week less the 20 hours of a night, as `sync_all` runs the
datasets one after the other and a run starts minutes earlier or later from night to night), so a record of
the country that names no code, or whose code changed, is seen at least once a week; the count with no filter fails
(`unfiltered_failed`); else each filter key in turn (`business_area`, then `business_area_code` for the partner
reports; the reverse for the progress reports) is asked on one page of 50 records of the sync's own query
(`AREA_PAGE`), and the first is used (`proven`) when it finds some records but fewer than with no filter, no
fewer than the rows held, and every record on its page names the code; otherwise its verdict is kept
(`nothing`, `ignored`, `fewer_than_held`, `other_codes`, `not_named`, `refused` with its HTTP status, `failed`),
and with none proven the read takes no filter (`none_proven`): an ignored or refused key is never sent. A key
that let a record of another business area through on any finished run of the last 7 days (the same night's
margin) is not tried
(`strays_this_week`). Under a proven filter, a record naming another business area or another country is a
*stray*: never written, counted (`area_filter.strays`, `details.area_strays`, and in `other_country_skipped`),
and the read removes nothing (*Succeeded with errors*); a record naming no code but this country is written
and counted (`area_filter.unnamed`), and the next night reads with no filter. `details.area_filter` keeps the
key, the code, why, the count with no filter, the rows held, those naming no code, each key tried with its
verdict and counts, the strays and the unnamed: key names, codes and counts only. Data health shows it on a
muted line ("read by business area 2490 (filter business_area: 9,990 records with it, 780,905 without)", or
"read with no business area filter: " and why) and warns of strays ("N records of another business area came
through the business_area filter: none was written and none removed; the next read tries the next filter");
check 10 (partner reports) and check 11 (PRP's progress reports) say the same and give the filter as a figure.
A proven filter keeps the same rows (it must find at least the rows held, and only the code), but the counts
read move: on a filtered night the records the Datamart announced and those read drop from about 780,000 to
about 10,000 (Data health, check 10), and they go back up on the weekly night. A held Lebanon record that stops
naming the code on a night when at least as many new records arrive is not returned and its row is removed;
the weekly read writes it back (it is kept by its country name), and every read then stays unfiltered. A new
Lebanon record naming no code likewise arrives up to 7 days late. The proof costs one count request and one
page of 50 per key tried each night (three requests at most), and none on a `weekly`, `no_code` or
`held_not_named` night.
The admin shows these tables read-only under *eTools Datamart*.

### HACT visit plan (Field monitoring page)

`/field-monitoring/?tab=plan` (sidebar *Field monitoring*, tab *HACT visit plan*) answers the
programme monitoring team's quarterly "HACT FMM plan" e-mail and its workbook from NeuroDB itself: one
row per programme document (PD) of a HACT year with its programmatic visits planned, completed and
still needed, the PDs not visited, the end-of-year missing visits and the sections' comments on them.
The default tab, *Findings and visits*, is unchanged apart from a one-line strip of the plan's figures
on top: it shows the findings filter's year (this year when none is chosen) and is left out when
eTools sent no planned visits for that year. Staff read *Field monitoring and the HACT visit plan* in
the help guide (`/help/field-monitoring/`).

The code: `neurodb/datamart/hact_plan.py` (the rows and every figure), `neurodb/datamart/fm.py`
(`completed_programmatic_rows`, `visits_by_pd_for_hact`, `partner_visits_naming_no_pd`,
`search_reference`), `neurodb/reports/views.py` (`monitoring`,
`_monitoring_plan`, `_plan_export`, `visit_plan_comment`) and the templates
`reports/partials/monitoring_plan.html`, `_plan_row.html`, `_plan_filters.html`,
`_monitoring_tabs.html`, `plan_comment_form.html` and `reports/plan_comment.html`.

**The rules** (year Y, as of today):

| Figure | Rule | Read from |
|---|---|---|
| The PDs listed | Not `draft` or `cancelled`, and running at some point in Y (start on or before 31 December or blank, end on or after 1 January or blank, not both blank), or with a plan for Y, or with a counted visit in Y. Sorted by partner, then reference | `etools.PCA` |
| Required | q1 + q2 + q3 + q4 of the PD's planned visits for Y; 0 without a row. A row the sync could not link to a PD (it links by the exact PD reference: `details.not_linked` of the `planned_visits` run) is in no figure; the tab and the About sheet say how many such rows Y has and how many visits they plan (`hact_plan.unlinked_plans`) | `datamart.PlannedVisits` (`planned_visits`) |
| Completed, field monitoring | Distinct visits (`fm.visit_key_expr()`) with a finding row naming the PD (`intervention`), `is_programmatic_visit`, status `completed` (any case), end date in Y. Two rows of one visit about the PD count once; a visit about two PDs counts for each. These are the rows the Assurance page counts per partner as "FM programmatic visits (NeuroDB)" (`fm.completed_programmatic_rows`, one rule for both) | `datamart.MonitoringFinding` (`field_monitoring`) |
| Completed, TPM | Distinct visits (visit reference, else task reference, else the row) of the activities naming the PD, `is_programmatic_visit`, visit status `unicef_approved` (the activity's `visit_status`, else its TPM visit's status), activity date in Y | `datamart.TPMActivity` (`tpm_activities`), `datamart.TPMVisit` |
| Counted | min(Required, Completed) | |
| Still needed by 31 Dec | Required − Counted, never negative. No netting: a surplus on one PD never offsets a gap on another, even of the same partner | |
| Due by end of Q*n* | max(0, planned q1..q*n* − Completed); *n* is today's quarter for the current year, 4 for a past year, 0 for a future one | |
| Not visited (`zero`) | Required > 0 and Completed = 0 | |
| Ended | Status `ended`, `closed`, `terminated` or `expired` (`partnerships.statuses.ENDED`), or an end date before today | |
| Remark | The first that applies: `no_plan` "No visit planned" (Required 0), `completed` "Completed" (nothing still needed), `ended` "PD ended, visits missing", `needed` "HACT FMM needed". The codes are stable (filters, files, the AI) | |
| Not counted, shown under Completed | Awaiting approval (field monitoring `submitted` and TPM `tpm_reported`, programmatic, same year rules); field monitoring visits completed but not marked programmatic; UNICEF staff trip activities typed programmatic (distinct trip reference, date in Y), which eTools sends without a status; the partner's completed programmatic field monitoring visits of Y none of whose finding rows names a PD (only the partner or a CP output: Assurance counts them for the partner), the same figure on each of the partner's PDs and in *By partner* (`fm_partner_level`, file column `fm_partner_visits_naming_no_pd`) | the same, and `datamart.ProgrammaticVisit` (`staff_visits`) |
| TPM yes / no | A TPM activity of the PD dated in Y whose visit status is not `draft` or `cancelled` | `datamart.TPMActivity` |
| Risk | The partner's HACT year of Y (the latest synced row, `-last_modify_date, -datamart_id`, as the Watch reads them), else the partner's rating | `datamart.PartnerHACTYear` (`hact_history`), `etools.PartnerOrganization` |
| UNICEF focal points | A count, after `reporting.focal_point_names` drops any entry holding an e-mail address. Names are read for the page render only (`hact_plan.focal_names`) | `PCA.unicef_focal_points` |

**The figures above the list** count the rows of the filter bar, the quick views left out (the *By
section* table too):

- *Not visited in Y*: the PDs with `zero` and their distinct partners; under it, how many of those
  partners have no completed visit on any of their PDs of Y (every PD of the year, the ones the
  filter leaves out included: `headline(rows, year, every)`) and how many of those PDs ended. A PD
  whose partner the sync could not link counts for no partner in any of the figures.
- *Visits still needed by 31 Dec*: Still needed added up over the PDs that have a gap, with their PDs
  and partners and the visits and PDs among them that ended (ended PDs count: their planned visits
  were not made).
- *Programmatic visits completed*: Σ Counted of Σ Required; Σ Completed and Σ awaiting approval under it.
- *Partners behind their HACT minimum (eTools)*: partners of the filter whose `PartnerHACTYear` of Y
  has `pv_completed` < `pv_required`, of those with a row; it links to `/assurance/?hact_year=Y`. It is
  the partner-level figure, shown beside the PD-level ones so that the two can be compared. It is not
  Assurance's "N partners behind their minimum requirements" (`services.hact_compliance`), which counts
  every `PartnerHACTYear` row of Y, PD in the list or not, and a partner behind on spot checks
  (`sc_gap`) too: on the same data Assurance's count can be higher. The tile's hint and the help page
  say so.

Applied to the workbook's own *Required PV* and *Completed PV* columns, these rules give the e-mail's
two figures ("N PDs for M partners not visited since 1 January", "N visits across M partners still
needed"); NeuroDB's own figures differ only where its sources do (below). The quick views are *Not
visited* (`zero=1`), *End-of-year missing visits* (`remark=needed&remark=ended`, the workbook's EoY
sheet) and *All*. *By partner* (`view=partners`) adds up each partner's PDs of the filter beside its
eTools HACT minimum (required, completed, gap), with two flags: "Partner minimum met in eTools, PD
visits still needed" and "PD plans below the partner minimum". The second compares the minimum with
the plans of **all** the partner's PDs of Y, whatever the filter (a section's own PDs or a quick view
never plan the whole minimum), on the page and in the files. Under a section filter the *By section*
table (and the *By section* sheet, and the AI lookup's `by_section`) keeps only the chosen sections:
the other sections of a PD shared with a chosen one would hold only that PD.

**Year and quarter.** The HACT year is the calendar year and the plan is `PlannedVisits.year`. A field
monitoring visit belongs to the year of its **end date**, as on the Assurance page, so that the partner
and the PD counts never disagree; a TPM activity and a staff trip to the year of their date. A quarter
is a calendar quarter, `(month − 1) // 3 + 1`. Monitoring insights and the findings tab date a visit by
its start date: the *Completed* link opens the PD's reported programmatic visits in Monitoring insights
(`pd=`, `programmatic=1`, `status=reported`, `year=`), which include reports still awaiting approval,
and a note under the list says so; with Monitoring insights off it opens `/field-monitoring/?q=<PCA/PD>&year=Y`.
`<PCA/PD>` is `fm.search_reference`: the PCA/PD number pair the visits are linked by (`PCA2026001/PD2026001`
of `LEB/PCA2026001/PD2026001-2`), so a finding naming the PD with another country prefix or amendment is
found too; the findings tab dates a visit by its start date, so a visit started in December and counted in
January's year is listed under December's (the note under the list says so). Monitoring insights builds its
visits from the field monitoring findings only (`fmm/build.py`), never from `TPMActivity`: with it on, "TPM
m" under the figure (when m > 0) links to `/field-monitoring/?q=<PCA/PD>&year=Y#tpm-activities`, the PD's
TPM activities on the first tab, and so does the figure itself when the PD has TPM visits and no field
monitoring visit or report awaiting approval (`completed_fm` and `awaiting_fm` 0), whose Monitoring
insights list would be empty; the note says TPM visits are not in Monitoring insights.
The year menu offers every year with planned visits or a partner HACT year, and this year. The filter
bar's lists are the year's (`hact_plan.options`): when the year changes, the answer carries the filter
bar again out of band (`#plan-filters`, only then, as htmx's `HX-Current-URL` shows the year the page
came from, so typing in the search box keeps its focus); the breadcrumb names no year. The lists also
keep every value the list is filtered on, chosen (`hact_plan.options(rows, f)["chosen"]`), even one the
year lacks: a partner chosen from its partner page stays chosen in a year it has no PD in, where the
list then says "No programme document matches", so the bar and the list agree and the value can be
taken off. A bar opened on a phone stays open when it is drawn again (`keepFiltersOpen` in `app.js`).

**Section comments.** The model `datamart.VisitPlanComment` (table `datamart_visitplancomment`),
created by `datamart/0014_visit_plan_comment`: a new, empty table and nothing else, quick at start-up
and listed in `QUICK_MIGRATIONS` (`tests/datamart/test_backfill.py`); the release before never reads
it. One comment per PD and year (a unique constraint): the text, at most 500 characters (control
characters removed, spaces and line breaks made one space), `pd_reference` (the PD's reference when
written, for the admin list), `updated_by` (the username) and `updated_at`.

- **Who writes**: Administrators, and Section editors whose section is the confirmed NeuroDB section of
  one of the PD's own eTools section names (*eTools section names*, see *NeuroDB Watch*;
  `hact_plan.can_comment`). The page reads the confirmed names once per request and
  shows *Comment* / *Edit* only to them; `POST /field-monitoring/plan/<pk>/comment/` (CSRF) answers 403
  to anyone else, and 404 for a PD the plan does not list for that year (a draft or cancelled PD, or
  one of another year: its comment would show nowhere). An empty text or *Remove* deletes the comment.
- **Who reads**: every signed-in staff member, on the page, with the date of the last change; never who
  changed it. The Excel and CSV files hold the text and the date.
- **Never sent to the AI**: the table is not a Datamart dataset and is not in `datamart/catalogue.py`,
  so `etools_query` and its kind cannot read it, and the `hact_visit_plan` lookup never reads it.
- **Kept across syncs**: the syncs never write it, and PD rows are updated, never deleted.
- **Admin**: *Partnerships (eTools)* → **Visit plan comments**, for administrators (not only
  developers): reference, year and date of each, a filter by year and a search by reference. Correct or
  remove a comment there; `updated_by` is read-only and set to whoever saves. Every change, on the page
  or in the admin, is written to the audit trail (admin → *Audit trail*) with who made it and when,
  **never the text**: "Written on the HACT visit plan (N characters).", "Removed on the HACT visit
  plan.", or Django's "Changed Text." for an edit in the admin. The audit trail is read-only, so a
  name typed into a comment by mistake would otherwise stay there for good; editing or removing the
  comment removes it for good.
- Not here, on purpose: the workbook's "Action by PMI" tick, and focal point corrections (sections
  correct a PD's UNICEF focal points in eTools; the next sync shows them).

**Privacy.** The rows `hact_plan.build` returns are plain data with the focal points as a count, and
they alone feed the files, the Watch and the AI. The names are read for the page only (`focal_names`,
`for_page`) and shown in a column hidden on phones and marked `data-export="no"`, which the table's
*Copy* and *CSV* skip. *Excel of the filter* (sheets *HACT visit plan*, *By partner*, *By section*,
*About*) and *CSV of the filter* hold every PD of the filter with `unicef_focal_points_count`, never a
focal point's name, `updated_by`, a visit lead, a team member, an assignee, an e-mail address or a phone
number (a test reads every cell of every sheet and every CSV field). The Ask NeuroDB lookup and the
Watch get counts only. The Progress reports export writes them as a count too (`unicef_focal_points_count`).

**Links.** Assurance, *HACT assurance by partner*: "Visit plan per programme document →"
(`?tab=plan&year=<HACT year>`). The partner page, *HACT by year*: the same, on the partner
(`?tab=plan&partner=<id>&year=<this year>`). The PD page panel *Field monitoring visits*: "HACT visit
plan →" (`?tab=plan&q=<reference>&year=`). Monitoring insights, Analysis, HACT block (which lists only
the partners visited in the filter): "see the HACT visit plan →" (`?tab=plan&zero=1&year=`). NeuroDB
Watch's `hact_assurance_gap` (from 1 October) adds the evidence "Programme documents still needing
visits" ("2 programme documents need 3 visits", linking to `?tab=plan&partner=<id>&year=<year>`) when
the partner has a PD with a gap; its key, severity, timing and routing are unchanged (see *What it
follows: the checks*). Ask NeuroDB's `hact_visit_plan(year, section)` returns the same figures as
counts (by remark and by section), the sync date and the link.

**Speed.** The tab is computed live at each request, with no stored figures and no parts, so a sync or
a comment shows at once. `hact_plan.build` makes 7 grouped queries whatever the number of PDs (planned
visits, field monitoring visits, TPM, staff trips, partner HACT years, the PDs, and the partners' field
monitoring visits naming no PD, about 5 ms of it) and took 27–30 ms for the 252 PDs of the x20 test world
(twenty times today's data, with synthetic plans), about 40 ms with what the tab adds (the sync date, the
years, the planned-visit rows matching no PD, the focal point names, the comments, the section map; 9
queries). The whole `?tab=plan` page on that world, cold, 7 runs: an Administrator 197 ms median
(slowest 296 ms, 19 queries), a Viewer with a section 182 ms (slowest 219 ms, 22 queries); the
default tab with its strip 128 ms; Excel and CSV 36–56 ms. Drawing the 252 rows is most of it. The
partners' visits naming no PD and the planned-visit rows matching no PD, added later, cost two queries
(about 6.5 ms; 21 and 24 queries now) and nothing measurable in drawing the rows (the row template timed
before and after on the same rows). Timed again then, with another test run busy on the same machine:
an Administrator 237–294 ms median (slowest 358 ms), a Viewer 206–236 ms (slowest 270 ms). That is
under the 300 ms from which a page is given parts (*Pages that load in parts*), so it stays one
request; if a larger list goes over, the list becomes one part under those rules rather than being
cached. Above 1,000 PDs in the filter, the list asks to choose a section or a partner (the files still
hold them all).

**Where NeuroDB's figures can differ from an eTools HACT dashboard extract** (the help page says the
same to staff):

1. *Required*: the extract takes the dashboard's requirement per PD; NeuroDB the sum of the PD's
   quarterly plan. When the dashboard drops a PD's requirement to 0 because a planned visit can no
   longer be made, NeuroDB keeps the plan and shows the PD as "PD ended, visits missing", counted in
   *still needed*.
2. *Completed*: the extract takes the dashboard's own figure, whose sources NeuroDB cannot see; NeuroDB
   counts the field monitoring and TPM visits above. Left out, and shown under Completed: staff trips,
   reports still submitted, visits not ticked programmatic, and a visit whose findings only name the
   partner or a CP output, never the PD (it still counts for the partner on Assurance; each of the
   partner's PDs shows it).
3. *Dates*: field monitoring by end date, TPM by activity date; the extract is fixed on its day,
   NeuroDB follows the nightly sync (20:30).
4. *Which PDs*: the extract lists the PDs on the dashboard, NeuroDB every PD of the year; PDs without a
   plan never count as not visited or needing visits.
5. *PD or partner*: eTools' partner HACT minimum (`pv_required`, from the risk rating and the cash
   transfers) is another requirement; Assurance, Monitoring insights' HACT block and the Watch keep
   reading it.
6. *Statuses*: NeuroDB shows eTools' status codes (a typed "Closed" is `closed`), and never a negative
   gap where a workbook formula gives −1.

**Checking against an eTools HACT dashboard extract, after the first sync.** Counts only, in the
browser and the Excel file; nothing to run:

1. After the first nightly eTools Datamart sync with this release, check in admin → *Data and sync* →
   *Import and sync runs* that `planned_visits`, `field_monitoring`, `tpm_activities` and
   `hact_history` succeeded. Open `/field-monitoring/?tab=plan&year=<year>`: the line under the quick
   views gives the date of the sync. Use an extract taken as close to that date as possible.
2. Compare three counts with the extract:
   - the PDs with a plan: the *By section* total *PDs with a plan* against the extract's PD rows with a
     requirement above 0 (expect NeuroDB to have more rows in all: point 4 above);
   - *Not visited in Y* (PDs, partners) against the extract's PD rows with a requirement above 0 and
     nothing completed, and their distinct vendor numbers;
   - *Visits still needed by 31 Dec* (visits, partners) against the sum, over the extract's PD rows, of
     requirement − min(requirement, completed) where it is above 0, and their distinct vendor numbers.
3. For the PDs that differ, download *Excel of the filter* and match its rows to the extract by
   reference (the PCA and PD numbers: a `LEBA/` or `LEB/` prefix and a `-2` amendment suffix can
   differ). The row's own columns give the cause:

   | The row shows | Cause | What to do |
   |---|---|---|
   | `required_pv` 0 ("No visit planned") while the extract has a requirement | No planned visits for the PD and year in eTools, or a row not linked to the PD (`details.not_linked` of the `planned_visits` run) | The section checks the PD's planned visits in eTools; the next sync shows them |
   | The tab (and the About sheet) says "eTools sent N planned-visit rows for Y … that match no programme document" | Planned-visit rows whose PD reference matches no PD number in NeuroDB (another amendment suffix or prefix, or a PD synced later): their visits are in no figure, so the headline is that much low | *Data health*, `planned_visits`, shows the rows not linked; the next sync links a row once its reference matches a PD |
   | `required_pv` above 0 but not the extract's | The dashboard's requirement differs from the sum of the quarters (`q1_planned` to `q4_planned`), or the dashboard dropped it to 0 | Nothing: NeuroDB follows the plan (point 1) |
   | `fm_completed_not_programmatic` above 0 | A field monitoring visit not ticked programmatic in eTools | The section ticks it in eTools if it was a programmatic visit |
   | `awaiting_approval` above 0 | A report still submitted (field monitoring) or reported by the TPM company (TPM) | It counts once UNICEF approves it |
   | `staff_trip_activities_not_counted` above 0 | Staff trips, which the dashboard may count | Nothing: not counted, by design |
   | `fm_partner_visits_naming_no_pd` above 0 | A field monitoring visit to the partner whose findings name only the partner or a CP output, never the PD, which the dashboard may count for the PD | The section names the PD in the visit's findings in eTools if it was a visit to that PD |
   | `completed_pv` lower, none of the above | A TPM visit in another status, or a visit dated in another year (end date for field monitoring) | Look the visit up in Monitoring insights or on the findings tab |

4. Write what was found in the next paragraph: counts only, never a partner, a reference, a person or
   a row number of the extract.

**Results of the first check.** Not done yet: at release, NeuroDB had not synced the real eTools
Datamart with this feature. Write here the date of the sync and of the extract, the three counts on each
side, and how many PDs differ for each cause of the table above.

### Data quality per dataset

`/data/health/` lists every Datamart dataset with its last run, records read and written, records
that failed, records whose partner, programme document or location is not in NeuroDB yet
(`not_linked`, per kind), rows removed because the Datamart no longer returns them, and the rows
NeuroDB holds; a dataset read in windows says how many, why (a round count, or the cut sign of the read
before) and what they announced and read, and why a read removed nothing (a window not whole, a page cut,
a count that changed, an answer cut that could not be read in windows, a large removal kept one more night:
*A large read is read in windows*); the partner reports and PRP's progress reports say which business area
filter their read took, or why none, and the records of another business area a filter let through (*A
business area filter is proven before it is used*); a read whose records came twice says whether its pages
shifted or, read in a stable order, the Datamart likely holds them twice (*Pages that shift during a read*).
Read it before trusting a number
on an eTools page; a dataset with many "not
linked" records or an "empty response" is the first thing to fix. The latest run of every dataset is
read in one query that takes, for each dataset, its newest run from the index `core_syncrun_job_target`
of core migration 0027 (a `LATERAL` join), not by reading and sorting every Datamart run (on a year of
runs, about 1 ms instead of 30).

**Data health loads in parts** (see *Pages that load in parts*). The page is open to every signed-in
member of staff, as before, and answers its frame at once: the page heading and the *Sync jobs* heading
with its *Stale after N hours* note. Each block is its own computation and comes in its own request
(`/data/health/?_part=<name>`: the page has no filter):

| Part | What it holds | When it loads |
|---|---|---|
| `jobs` | One card per sync job: fresh, stale or never succeeded (its placeholder draws one grey card per job) | At once |
| `databases` | The displayed ActivityInfo databases of the current year and their last import | When near the screen |
| `runs` | The latest 50 runs of every job | When near the screen |
| `datasets` | This table, the eTools Datamart datasets, under its *Copy* and *CSV* buttons, which come with it (never offered before the table is there, nor beside an error); its heading and explanation (with the link to the reporting checks for administrators) are in the frame, so the placeholder keeps the room of the table and its buttons at any width | After the visible ones |
| `power-bi` | *Compared with Power BI*: per Compiler programme (Makani, Dirasa, youth) its latest periods compared with the view Power BI reads, counts only (see *Education programmes*, *Compared with Power BI*); its heading and explanation, with the links to the card versions and the view definitions for administrators, are in the frame | After the visible ones |
| `readiness` | *Ready for machine learning?*, the last readiness check | After the visible ones |

Print Data health or save it as PDF from the browser: every table (databases, runs, datasets, *Compared with
Power BI*) prints whole, wrapping between words (a figure or a word breaks only when it cannot fit at all, so
"781,014" never prints as "781,0" over "14"), never cut at 560 px or at its right edge; the database imports
and the recent runs print one above the other, each the sheet's width, and each period's details of *Compared
with Power BI* print open (the page opens them before printing and closes them after, a script with the page's
nonce) (`app.css`, stage A section; `data_health.html`).

Only the *Rows held* column is kept in the shared page figures: an hour at most, the same for everyone
and in every language, under a key made of the latest run of each job that writes the counted tables
(eTools Datamart sync, eTools sync, Locations sync, eTools update clean-up), so the rows are counted
again as soon as one of those runs starts or ends (that key is read from the index of each job and
status, so it costs well under a millisecond at every visit). The gazetteer (*locations*), which staff
also edit in Admin → Locations, is counted at each visit, and everything else on the page is read at
each visit. Ask NeuroDB's *data freshness* look-up reads the same figures, and the stored counts with
them; inside a database transaction (NeuroDB Watch's AI look-ups run it in a read-only one) the stored
counts are read too, and when there are none the rows are counted there without storing anything.

### Checking the figures: raw eTools data, figure trails, the Power BI comparison sheet

The management asked that every eTools figure be checked: the PRP pages against the PRP Power BI dashboard (by
hand), and every other page against the raw data NeuroDB pulled from eTools. This section describes what is
built for it: Raw eTools data, the figure trail of Progress reports, Partner monitoring (and its map) and Home's
two partner reporting blocks, and the Power BI comparison sheet.

**Who.** Superusers and the members of the **Validators** group (`accounts.roles.VALIDATORS`, made at every
start by `ensure_groups`, with no permission; setting a role never takes a person out of it). An
Administrator is a validator only when ticked, like anyone else. `roles.can_validate(user)` reads the group in
the one groups query `role_of` already makes (`_groups_held`, per request under `groups:<pk>`), so a page that
asks costs no query more; the context processor gives templates `can_validate` (lazy). To give the right:
*Users and access → Users*, the person, *Groups*: tick **Validators**; Raw eTools data and the eTools reporting
checks are in the admin, so the person also needs *Staff status*.

**Where.** Admin → *Data and sync* → **Raw eTools data** (and a Quick action on the admin home; a staff validator
who holds no table of *Data and sync* still gets the group, with these pages only). Raw eTools data's pages
head with their own title (no `opts`: no link to the Datamart's developer tables, which a Viewer or Section
editor cannot open and which show people). *eTools reporting checks* opens for validators too, read-only: its
*Run the checks now* is for Administrators (a validator's POST answers 403), and so are its links to the
*Reported indicators* table, to a failed run and, in its header, to the Datamart's tables. Data health's datasets
panel links Raw eTools data and the reporting checks for validators with staff status ("eTools reporting checks:
ask for staff status" without it). The admin home a staff validator lands on links its tiles, its *Needs
attention* lines and its *Data jobs* only where the reader may go (`admin_site._opens`: the model admin's view
permission); the others read as plain text. Under the tabs of Progress reports,
Partner monitoring (and its map), Field monitoring and under *Partner reporting* on Home, a validator sees
**Raw data behind this page** (`reports/partials/_check_link.html`), which opens Raw eTools data on that page's
sources (`?page=<navigation key>`, `datamart.raw.PAGE_SOURCES`); without staff status it says "Raw data: ask
for staff status". On Progress reports (every tab) and Partner monitoring (the grid, Program Performance, Data
quality and the map; not the master indicators) the line also offers the **Power BI comparison sheet (Excel)**
with a "statuses read for" day (below), and on both pages and the map **Check these figures**, the figure
trail of the filter drawn (below; not on the master indicators); under the title of each of Home's two blocks
(Progress reports, Indicator progress) a line of its own offers *Check these figures* alone (the raw data link
stays under *Partner reporting*'s heading). The pages draw the
line with their results (Partner monitoring in `_monitoring_results.html`, the map in its frame), so a change of
filter draws it again with the new filter.
Anyone else sees the pages as before; the addresses answer 404.

| Page | Address (under the admin's path) | Name |
|---|---|---|
| Every source | `datamart/raw/` (`?page=<navigation key>`) | `admin:datamart_raw` |
| One source | `datamart/raw/<source>/` | `admin:datamart_raw_source` |
| One record | `datamart/raw/<source>/<pk>/` | `admin:datamart_raw_record` |
| CSV of the filter | `datamart/raw/<source>/rows.csv` | `admin:datamart_raw_csv` |
| Review a field (POST, superusers) | `datamart/raw/<source>/fields/` | `admin:datamart_raw_field` |
| Read the eTools REST API now (POST, superusers and Administrators) | `datamart/raw/rest/read/` | `admin:datamart_raw_rest_read` |

`<source>` is a dataset of the nightly Datamart sync (`ENTITY_SYNCS`): its records are its own table
(`catalogue.TYPED`) or `DatamartDocument` rows (the raw copies of the partners, programme documents,
agreements, locations and engagements included); or an entity of the eTools REST API (`raw.REST_ENTITIES`:
`rest_partners`, `rest_partner_details`, `rest_agreements`, `rest_interventions`, `rest_intervention_details`,
`rest_travels`, `rest_travel_details`, `rest_engagements`, `rest_engagement_details`, `rest_action_points`),
whose records are `RestRecord` rows (below). Any other value answers 404. The pages are `datamart/raw_admin.py`
with `datamart/raw.py`; templates `datamart/templates/admin/datamart/raw/`, plain pages with no script.

**What each sync received** (`datamart/runfacts.py`, from Data health's own row, `reports.services.quality_row`).
The run the records come from is the latest that succeeded, or succeeded with errors, and received something;
a newer run that failed is named beside it, with why in a few words read from its error
(`runfacts.failure_words`: "eTools Datamart answered 500", "eTools answered 403: not allowed", "eTools
Datamart answered 403: the account NeuroDB uses is not allowed to read it; ask the eTools Datamart team to
grant it" (a 403 once the account is shown signed in: the body says so, or the run of another dataset nearest in
time read; "eTools Datamart answered 403: sign-in refused" when the body names the sign-in, "... it refused
NeuroDB's sign-in or this dataset" when nothing tells them apart), "no answer in time", "could not connect", "NeuroDB was restarted during the run", "the partners could not be read", "no PRP
business area code is set"... and "an unexpected error" for any other), never the error's text (the 200 characters of an answer's body come from
eTools or a proxy); for superusers and Administrators the words link to the run, where *What the error means*
says what to do; for anyone else they are plain text. A request that got no answer reads the same on both pages
(`core.admin.unanswered`): `integrations.http` names a read that timed out `ReadTimeout` (requests calls it a
`ConnectionError` once the retries are used up), so "no answer in time" means eTools or the Datamart took the
connection and did not answer, and "could not connect" (a `ConnectTimeout`, a refused connection, no secure
connection) that no connection was made. A diagnostic dataset (`catalogue.DIAGNOSTIC`:
`datamart_etl_status`, whose `system/monitor` answers 500 on the Datamart's side) says so ("diagnostic: no page
or check uses it") and is never shown as an alarm. On distinct records:

    received − of another country − named no country − could not be saved = updated by the sync
    in NeuroDB = updated by the sync + updated since (a later sync under way) + kept from an earlier sync

A record a later run rewrote (one running now, or one that failed after writing) is counted updated since, no
longer by the sync: up to the remainder it is explained too (`runfacts._unexplained`), so a sync under way never
shows a red line or *Complete? No* of its own; a remainder larger than what the later run rewrote stays shown.
"Received" is `runfacts.records_read(run)` (`details.read`, else the rows written and those of another country
left out), the one rule checks 10 and 11 read too. `other_country_skipped` holds the records that named no
country (`unnamed_skipped`, shown on their own line) and those a business area filter let through
(`area_strays`, an "of which"); records sent twice are a note, and those the read proved the Datamart's own
copies (`details.copies_proven`, *Records the Datamart sends twice*) count with what NeuroDB received against
what eTools said it had, and are named in the *Not kept* column ("1,939 the Datamart sent twice (identical)";
the others "1,939 sent twice (counted once)"). "Updated by the sync" is counted in the table (`synced_at`
within the run; for the engagements, up to the end of the first run of each of their detail datasets,
`audit_results`, `audits`, `spot_checks`, `micro_assessments` and `special_audits`, started after it and
before the next engagements run: they save every engagement they match again straight after it, so on 24 Oct
2026 the 633 engagements read "updated by the last sync 0" and "updated since (a sync is running) 633";
`raw.written_until`, as *Programme documents in NeuroDB* counts what the budgets save; never more than the
engagements run kept itself: one a read not whole kept from an earlier sync, saved again by its audit, counts
as kept from an earlier sync). For the raw copies of the sources written by another sync, "could not be saved"
is `details.documents_failed`; the records that could not be saved into the partners, programme documents,
agreements, engagements, locations or sites the pages read (the run's `rows_failed`) are a line of their own,
on the source's page and in the first page's *Not kept* column. A remainder the counts do not explain shows in
red. **Complete?** is *Yes* when the run succeeded, NeuroDB received at least what eTools said it had, nothing
failed (a raw copy, or a partner, programme document, location... of a source written by another sync) and
nothing is unexplained; otherwise the first reason ("No: the last sync failed"; for a 403 of the Datamart, "No:
not allowed to read it (ask the eTools Datamart team)"; "No: eTools said it had N more", less the copies
proven; when records that came twice, not proven, are as many as those that did not come, "No: N were sent twice
and may stand for others that did not come; nothing was removed", and the source's page says that NeuroDB
counts a record sent twice as eTools' own copy only when every copy comes identical in one answer of the
Datamart, so the read is not complete, never that the next sync reads them). An error is shown by its kind
only (`runs._error_key`). On
21 Oct, PRP's progress reports: received 150,221 − of another country 148,631 − could not be saved 0 = 1,590
updated by the sync; 74 came twice, not proven, and may stand for the 74 eTools said it had more. An eTools REST entity's records come from the latest run that
kept its copy (the read below, one run per entity, or the eTools REST sync, `details.raw_copies`; a newer run of
either that failed is named) and, for a list, received something: a list eTools answered empty (eTools down, or
a token whose user lost the country) removes nothing, so the records and the page's counts come from the read
before it, and the newer read is named beside it ("the read of …: eTools sent nothing"); a list read that
received nothing is never complete ("No: eTools sent nothing"). Nothing is of another country: received − could
not be saved = updated by the sync. An entity with no read of its own never reads a bare "Not read yet." once
a read could have reached it: each run of a read names the read (`details.read_started`, `details.read_of`), so
the row says *Being read now (since …)*, *Waiting: the read under way reaches it later*, *The read of … stopped
before it reached it (NeuroDB was restarted during the run)*, *The read of … failed (…)*, *Not read on … (the
partners could not be read)*, or *The read of …: no final engagement to ask for* (`runfacts.rest_reads`: the
newest run that ended, beside the one the records come from; one query more for the read). A read asked from the
button whose process never wrote a run says so after two minutes ("The read asked at … has not started": to
those with the button, press it again; to anyone else, tell the NeuroDB team).

**The people rule** (`datamart/raw_people.py`, `public_record`): every record on screen and in every CSV goes
through it, nothing else writes a raw record out. Contact fields are dropped at any depth; the answers of PSEA
assessments keep only their safeguarding fields (the others one count, `withheld_keys`); a field whose name is
not an identifier (a space, an `@`, longer than 64 characters, shaped like an e-mail, a link or a phone, or a
person's name) is never a column (`(fields naming a person)`), and inside a field, at any depth, a name with a
letter that is not an identifier is dropped and counted the same way (PRP's `"(12, 31)"` stays); a field that
names people (`query._person`, Ask's rule: the new words participants, attendees, interviewees, informants,
beneficiaries, visitors, guests, companions, recipients and `pme` among them; `participants_count` stays a
number) shows how many; a text a person wrote (comments, notes) is withheld; every other text is cleaned by
Ask's own `query._safe` with the record's own names. People inside a field shown are counted under their
dotted path, and in a CSV's `people_inside`. NeuroDB's own columns go through pinned lists: `PERSON_COLUMNS`
(shown as a count), `NEVER_COLUMNS`, `TEXT_COLUMNS` (the free texts, never searched) and `JSON_COLUMNS` (an
engagement's `details`, which holds its audit, spot check... records whole, `RECORD_COLUMNS`: each record read
as its own source's page reads it, a field nobody reviewed as "N values (not reviewed)"); every
text column is cleaned like a text of the record under its own name (a reference, a code or an amount keeps
its value; a field monitoring narrative over 80 characters withheld whole; titles with the record's own
names). A search or partner word that names a person is refused ("eTools records cannot be searched for a
person.").

**Fields not reviewed.** A top-level field is shown as received (through the rule) when it is reviewed: in
`raw.REVIEWED_KEYS` (the test payloads' fields whose words cannot hold a name, pinned by a test), a country
field (`country_name`, `business_area`, `business_area_code`), a field NeuroDB reads (`raw.read_into`: the
sync's fields maps), or a superuser's review (`datamart.RawFieldReview`, migration 0017: a new, empty table).
Any other field shows only numbers, yes/no, dates and times, under names that may be shown; a text in it, or a
name such as `"Test Person": 4`, makes it read "N values (not reviewed)". On a source's page, *Fields this
source sends* gives a superuser *Look at its values* (the developer table, filtered on records carrying the
field; an eTools REST entity has none) and *Reviewed: show it*; *Hide again* takes it back. Review a field after
reading a few of its values.

**The fields each sync counts** (`raw.KeyCount`). Every write path counts the top-level fields of each record
it keeps, on the record as stored (`catalogue.scrub`: no contact field), each record once, into the run's
`details.keys_kept = {"<field>": [records carrying it, records where it holds a value]}` (a value: anything but
empty, `[]` or `{}`): the datasets in their own table (`sync_dataset`, around `upsert_record`), the records kept
whole (`DocumentWriter.write`: the raw copies of the partners, programme documents, locations... included) and
the eTools REST copies (`RestCopy.keep`). At most 400 fields, the most carried first; the others summed under
`(more fields)`. A field name with a space or an `@`, longer than 64 characters, or shaped like an e-mail, a link
or a phone is never stored: it adds to `(other fields)` (the records carrying one, and with a value in one), as
does any field of a PSEA answer outside the safeguarding fields; the run's details are shown to Administrators as
they are. *Fields this source sends* reads them from the run the records come from ("Counted by the sync of …,
on every record it kept"), and the newest 2,000 records only say which fields hold a text; a run before this
release has none, and the page counts the newest 2,000 records as before. A CSV's header starts from them.

**The eTools REST API's raw copy** (`integrations/etools/rest_raw.py`, `datamart.RestRecord`, migration
datamart 0018: a new, empty table). One row per record and entity: `record_key` (its eTools id; for a detail
that names none, the id it was asked by; else a hash of the record), `source_id`, `reference` and `title` (a
programme document's number and title, an agreement's number, a partner's vendor number and name, a trip's
reference number as both: never its purpose, which can name the traveller; an engagement's or an action
point's reference number), `parent_key` (a detail: the list record it details; an action point: its
engagement), `data` (as received, `catalogue.scrub`), `synced_at`; unique on (entity, record key). Two jobs
write it:

- **Read the eTools REST API into Raw eTools data** (`read_etools_rest_raw`, `SyncRun.Job.ETOOLS_REST_RAW`, core
  0033: the job's choice; lock 7140441, one read at a time; one run per entity, target the entity). It reads the
  list endpoints, then each record's detail GET (the partners and programme documents listed, the trips started
  in the last 365 days, the engagements of a type with an endpoint), and the action points of every final
  engagement, into the copy only: it writes no other table, so no page figure changes. It is not one of the
  `keys.ETOOLS_JOBS` (the trail's "a sync is running" note). The button **Read the eTools REST API now** is on
  Raw eTools data, under *eTools REST API*, for superusers and Administrators (who are validators); a validator
  who is not one sees the reads and no button (its POST answers 403); *Run a job → eTools REST raw copy* starts
  it too, for any Administrator. Once pressed, the page says the read is starting until a run of it started
  after the press is there, or for two minutes at most (as the reporting checks' page). Without `ETOOLS_TOKEN` the page says "The eTools REST API token is not set on
  this server: ask the NeuroDB team." and shows no button; started from Run a job, it records one failed run
  saying so. A detail whose list failed in the same read (and the action points, when the engagements failed),
  or, read alone (`--only`), whose list's latest read failed or none ever ended, asks eTools for nothing: its
  run fails with "Not read: the partners could not be read." (or "were never read"), `details.read` 0, and
  removes nothing (`rest_raw.NEEDS`, `unread_because`; before, it succeeded having received 0, and the action
  points ended *Succeeded* with nothing, hidden from the page). The action points record how many final
  engagements they asked about (`details.final_engagements`; none: "no final engagement to ask for"). The read
  closes the runs a stopped read left *Running*, whatever their age, once it holds its lock; the page closes them
  too (`background.is_running`, which now closes every run of a job whose lock no process holds: before, one
  older than four hours stayed *Running* for ever).
- **The eTools REST sync** (`sync.py`) keeps the same copy as it reads: `RestCopy.keep` is the last step of each
  record's savepoint (a record that fails is not kept, its previous copy stays), and each run's
  `details.raw_copies.<entity>` says what it received and kept. It still writes the partners, agreements,
  programme documents (`PCA`, whose status decides "running" on every partner reporting page), trips,
  engagements and action points as before: that sync can move the PRP pages' figures, the copy never does.

**Removal.** After a list read that ended without an exception, the records of that list entity the read did not
receive are deleted, and the details whose `parent_key` was one of them (`details.removed`,
`details.details_removed`). A read that failed, received nothing, or received fewer records than the first page's
`count` said (`EToolsClient.list(..., seen=)`: "eTools said it had", `details.announced`; the t2f list gives none)
removes nothing (`removal_skipped`, `removal_skipped_why`). A detail entity is never removed on its own: the
details of trips started more than a year ago stay. **Names.** At the end of either job the names the copies hold
(`query._own_names`) are read with the Datamart's into `RecordPeople` (`query.remember_people`, which reads
`RestRecord` too): a partner's staff named in its details is then also taken out of an engagement's texts.
Ask NeuroDB never reads the copy (not in the catalogue, no tool queries it). The eTools update clean-up's
contact scrub covers its `data` column (`backfill._record_columns`).

**The filter and the list.** Programme document, partner, section (Partner reports: `reporting.section_q`), status,
year and dates, search (references and numbers, never a person, a text or a trip's title), record id, updated by
the last sync or an earlier one; for the partner reports also the report type and the programme document link,
whose five choices are the rules the pages use (`pd_statuses.RUNNING`, `NOT_A_PARTNERSHIP`,
`reporting.LEFT_OUT_REASONS`) and add up to every row. 50 records a page, newest first; each record's page
shows its fields as received, NeuroDB's columns, its links, for a partner report row its progress report id and
the status every page reads for it (`reporting.progress_reports`), and the sync that last updated it. An eTools
REST record's links are what NeuroDB holds under its eTools ids (`raw.link_rest`, one query for a record page or
a chunk of the CSV): a programme document's record (list or details), the `PCA` of its id, with its status (the
one the REST sync writes, which decides "running") and its partner; a partner's record, the partner of its id;
an agreement's, an engagement's or an action point's, the partner whose id it names. They fill the record page's
*Programme document* and *Partner* lines and the CSV's `pd_number_in_neurodb`, `pd_status_in_neurodb`,
`partner_in_neurodb` and `vendor_number`; a programme document NeuroDB does not hold reads "not in NeuroDB".

**Downloads.** `rows.csv` streams the filter (`exports.stream_csv`: BOM, an apostrophe before a text a
spreadsheet would run), NeuroDB's columns first (`figures_as_of`, the record id, `synced_at`,
`updated_by_last_sync`, the programme document and partner in NeuroDB; for the partner reports `pd_link`,
`progress_report_key`, `indicator_report_key`), then every field (a field that names people as
`<field> (people)`, a count; a field named as one of NeuroDB's columns, such as a record's `vendor_number`, as
`<field> (as received)`), then `people_inside`, `fields_not_shown` and `other_fields` (a field first met after
the header was made). The file is `raw_<source>_<YYYY-MM-DD>_<HHMM>.csv`, the moment being the end of
the source's last sync. Limits (`raw.CSV_LIMIT`): 25,000 records for the sources with long texts (partner
reports, field monitoring findings, action points, financial findings, staff trips), 100,000 for the others;
above, the page and the address (400) say which filter to choose. At most two large downloads at once on the
whole site (`RAW_CSV_MAX_RUNNING`, advisory locks from 7150000; a third answers 503 "Another large download is
being prepared"), freed when the download ends or its response is closed. The count runs in one snapshot
with a 15-second statement limit (400 "This filter takes too long"); the download reads 2,000 records at a time
inside one `REPEATABLE READ READ ONLY` snapshot with a 120-second limit per statement, and a statement that
takes longer ends the file where it is, with one last row whose `figures_as_of` says "The download stopped here:
the database took too long. Download again or narrow the filter." (`raw.STOPPED`, its other cells empty; the
trail's CSVs of records end the same way), so a cut file never looks complete. No background job.

**Printing.** Printed or saved as PDF from the browser, the three pages (every source, one source, one record)
print as the reporting checks do (`admin.css`, `.nd-raw`): every column of every table on an A4 landscape sheet of
their own (a named page, `@page nd-raw`), across the whole sheet (the print dialog reads the page at its
portrait width, where the admin's container would hold it to 640 px), wrapping between words, without the
sidebar, the header, the forms (the filter, the read button, the review buttons) or the review column; *Fields
this source sends* prints open. A number never breaks (`overflow-wrap: break-word`), nor does a sync's date, a
Datamart path or REST endpoint, or a record's cell that is one word as shown, such as a reference
`LEB/PCA2026012-1` or an amount (`white-space: nowrap`, `nd-raw__atom`, chosen from the cell as shown; the notes'
dates hold together by non-breaking spaces). A record's date ("Sept. 30, 2026") wraps between its words, a
field's key in a heading (`financial_contribution_currency`) after its "_" (`raw_admin._heading`, `<wbr>`), and
a long text such as an indicator keeps 7rem: the room comes from those, so PRP indicator reports print all 14
columns (their last three were cut), and 50 partner reports with indicators of 130 characters print their 15 on
11 sheets at about 8 lines a row (they ran 12 lines high, on 17 sheets). On screen a heading keeps one line
(`wbr` is hidden there: Chrome breaks at it even under `nowrap`). A source's records print a little smaller;
only a record id made from its content (64 characters) breaks where it must.

**The Power BI comparison sheet** (`datamart/comparison.py`, `reports/check_views.py`). NeuroDB never reads
Power BI: a validator compares by hand. `/check/progress-reports/compare.xlsx` and
`/check/partner-monitoring/compare.xlsx`, with the page's filter in the address (the check line's form writes
it), answer `can_validate` only (no staff status needed; a 404 for anyone else, `home` and any other page; a
donor never reaches them). `day=` is the day the statuses are read for (Due, Overdue and Not yet due depend on
it): the page's own day by default (`reporting_today()`, `monitoring.today()`), or Power BI's refresh, at most
14 days back (`comparison.DAYS_BACK`; another day answers 400). The sheet takes one of the two download slots
(503 when both are taken) and reads every figure in one `REPEATABLE READ READ ONLY` snapshot, with the page's
own functions: `services.partner_reporting` and the stored `reporting_table` of By location and By
disaggregation; `monitoring.stored_indicators`, `monitoring.program_performance_rows` (the rows of Program
Performance, which the page's table now reads too: the filter without its sections, the chosen sections kept)
and `section_counts` (its section tiles), and `data_quality`. Written with openpyxl in write-only mode, then sent
whole; its name is `powerbi-comparison-<page>_<YYYY-MM-DD>_<HHMM>.xlsx`, the end of the last partner reports
(and PD indicators) sync. Sheets:

- *Compare*: the NeuroDB filter (the page's address), *Statuses read for*, *NeuroDB data as of* (the last
  partner reports sync, and PD indicators for Partner monitoring; PRP's last change in the rows), a yellow
  *Power BI refreshed on*; *Everything NeuroDB holds* whatever the filter (indicator and progress reports of
  every PD status and year, programme documents for Partner monitoring, the earliest reporting period from
  `reached.history_from`); *Set these in Power BI* (every slicer with its value, a yellow *Done?*) and *Clear
  these in NeuroDB before comparing* (the filters Power BI has no slicer for, those set). Then one row per
  figure, in Power BI's page and visual order: `#`, Power BI page, visual, name in Power BI, NeuroDB page and
  figure, NeuroDB value, unit, a yellow *Power BI value*, *Difference* `=IF(ISNUMBER(H),H-F,"")` and *Same?*
  (the only formulas; none on a row with no NeuroDB number, such as By location over its 20,000 rows),
  *Notes* (checks 10 and 11 where they explain a difference) and *Trail*: the figure's trail for the same
  filter and day (`comparison.trail_link`: `figure=<key>`, the tab of By location and By disaggregation; for
  Partner monitoring `view=table` for Program Performance's figures, `view=quality` for Data quality's, and
  the year the sheet read written out; the figure's row as anchor). A
  filter that names a person or a contact (`comparison.names_someone`: a search holding an e-mail, a phone
  number or a known person's name, by the people rule of Raw eTools data) is written cleaned in *Set these in
  Power BI* and *Clear these in NeuroDB*, and none of its addresses is written: the NeuroDB filter and every *Trail* cell say "Not written:
  the filter names a person or a contact" (open the trail from the page).
  Progress reports: the seven status cards, the section table (each section by status, its total, the column
  totals), the month chart (each month by each status drawn), the progress reports listed, the Partners
  tab's cards, the reports to follow up, the lines of By location and the location blocks of By
  disaggregation. Partner monitoring: Program Performance's indicators (every one, not cut at 2,000) and its
  section tiles, then Data quality's ten tiles.
- *Progress reports* (one row per progress report: the export's `_report_row` without its focal points, the
  partner's vendor number, a match key "PD reference + space + report #") and *Partners* (one row per partner
  as the Partners tab counts them, `reporting.partner_key`, its progress reports per status, the match key
  "vendor number, else name"); for Partner monitoring *Program Performance rows* (every indicator, as its export
  writes it, NeuroDB's Reached and % Reached, yellow cells for Power BI's, two difference formulas, the match
  key "PD number | indicator | PD output", ending in #1, #2... for indicators of one PD sharing their title and
  PD output, check 7; a *Trail* cell per indicator: its Reached on the trail, `pp.reached.<PD id>.<key>`). The
  lists' texts (partner, PD reference, report #, section, CP output, indicator, PD output) are cleaned as the
  trail's CSVs clean them (`trail_rows.cleaned` with `REPORT_TEXTS` or `INDICATOR_TEXTS`: a person named as a
  partner, an e-mail or a phone in a title withheld), and each match key is built from the cleaned texts; a
  Program Performance row whose title or PD output lost one says "match this row by PD and target"
  (`comparison.KEY_WITHHELD`): Power BI's own export holds the raw title. The page's own Program Performance
  export (`views._monitoring_export`) still writes these texts as stored.
- *Why they can differ*: eleven reasons, each with where to see it.

Every text from the data is written as a text (`data_type = "s"`), never a formula; no person is written
(partners, programme documents and sections only). Power BI's page, visual, card and slicer names are those of
the shared report (*UNICEF Program Performance*, `comparison.POWERBI_REPORT`), as the user's screenshot shows
them. They are in one place: `comparison.POWERBI_PAGE_ORDER` (its ten pages in order) and `POWERBI_VISUALS` (each
sheet block and each export Compare with Power BI recognises, with its page and visual; `POWERBI_PAGES` and
`POWERBI_PD_PAGE` are made from them), `POWERBI_ITEMS`, `POWERBI_SLICERS` (only the slicers that mean what
NeuroDB's filter means), `POWERBI_ONLY_SLICERS` (*Country name*, written under *Set these in Power BI*),
`POWERBI_TO_CLEAR` (both pages: *Emergency indicator*, *CP Output* and *Indicator Title*; Progress reports also
*Document type* and *Office*; under *Clear these in Power BI*) and `POWERBI_FIELD_SLICERS` (Power BI's fields
`country_name`, `status`, `reporting_year` and `section_name` by their slicer's name, for Compare with Power BI's
*What to do now*), and every sentence of the sheet and of Compare with Power BI that names one reads it there (a
test renames them all). NeuroDB's emergency filter (the indicator's high frequency; Power BI's *Emergency
indicator* is Yes for an SPD), Partner monitoring's search (the title or the output; Power BI's *Indicator Title*
box is the title) and its CP output (the indicator's; Power BI's *CP Output* search matches every CP output of
the PD) are NeuroDB filters to clear (`NEURODB_ONLY`), never mapped to a slicer. A figure is named after a
visual only when that visual shows it (`comparison.VISUAL_FIGURES`): Data quality's block ends with Power BI's
card, *No. of Indicators without Reached* (Without Reached and Reported zero together; its *Trail* opens
Without Reached's), and Partner Performance's five-status visual names its five statuses only; the blocks'
other figures are on the page's cards (`POWERBI_ITEMS["cards"]`). *Clear these in Power BI* says both reasons
(`CLEAR_IN_POWERBI`): the page has no such filter, or its filter of that name means something else. Every field of the page's filter dataclasses (`ReportFilters`, `DisaggregationFilters`,
`monitoring.Filters`) is either a slicer (`POWERBI_SLICERS`) or a NeuroDB filter to clear (`NEURODB_ONLY`); a new
field in neither stops the sheet, and a test fails.

**The figure trail** (`datamart/trail.py`, `datamart/trail_rows.py`, `reports/check_views.py`). For every figure
Progress reports draws (the tiles and their hints, the late sentence, the left-out note, the section table, the
month chart, the report table's count and each row's *Indicators*, the Partners tab's tiles, table, partners not
reported and reports to follow up, By location's lines and By disaggregation's location blocks) and every figure
of Home's Progress reports block (the tiles, the programme documents to follow up and the rows shown, the partners
line): where its rows come from, step by step, and the figure rebuilt from them.

| Page | Address | Name |
|---|---|---|
| The trail | `/check/progress-reports/?<the page's filter>&tab=<the tab>`; `/check/partner-monitoring/?<the resolved filter>&view=<grid, table, quality or map>`; `/check/home/?block=<reporting or indicators>&<Home's filter>` | `reports:check_figures` |
| Its rows | `/check/<page>/rows.csv?<the filter>&what=<rows>` (`&of=performance` or `&of=map` for Partner monitoring's folded parts) | `reports:check_rows` |

Validators only (`can_validate`, no staff status needed; a 404 for anyone else, for the master indicators, and for
Home without one of its two blocks; a donor never reaches them). `day=` is the day the statuses are read
for (the page's `reporting_today()` by default, at most 14 days back); `figure=<key>` opens the figure's fold (the
comparison sheet's Trail links). The page loads in parts: the frame (the filter in words, the day, the moment of
the eTools figures, the end of the last partner reports sync, and while any `keys.ETOOLS_JOBS` job runs "An eTools
sync is running (since 20:31): the figures can move until it ends") at once, then `figures`, and for Progress
reports `locations` and `disaggregations`, folded (asked for when opened; open when the trail was opened from that
tab). Each part is computed in one `REPEATABLE READ READ ONLY` snapshot and kept two minutes for every worker
(`compute.once`, keyed on the page, the filter, the day and `keys.fingerprint()`; in DEBUG at each call).

- **Steps.** D1 to D4b from the run facts of `partner_reports` (what eTools said it had, what NeuroDB received, what
  it did not keep: counts only; what is in NeuroDB, as CSV, or past `raw.csv_limit` a link to its source on Raw
  eTools data, whose filters make a download smaller; from which date NeuroDB holds partner reports,
  `reached.history_from`, and the rows starting before it); then one step per filter of `ReportFilters.queryset`
  that applies, in its order (report type, year, search, section, report #, programme document, partner, partner
  type, draft or cancelled, emergency, the four period dates, overdue, then the PD status filter, which now comes
  last: every filter is ANDed, so the rows and figures are the same, only the order of the SQL's terms changed), each
  with the rows it leaves out and the indicator reports and progress reports it leaves out entirely and partly (some
  of their rows kept); the PD status step's three lines are `left_out`'s own (another status, not in NeuroDB, names
  no programme document), and the indicator reports it leaves out are `by_pd_status + no_pd` (a red line if not);
  K, the rows kept; S, the report status when one is chosen. A filter that kept rows the step before had left out
  shows a red line. Home's block has the same chain with Home's own filter (`home.figure_queries`), and the
  left-out note Home does not draw.
- **The hooks rule.** The trail never filters, keys or counts a row its own way: `ReportFilters.queryset(steps=)`
  appends each filter's rows to the trail's list; `reporting.unit_rows`/`unit_key`/`units` read a row's progress
  report and indicator report by the keys `progress_reports` groups with; `progress_reports(record=)` and
  `_status_words(record=)` hand the trail the status words read and the same rows one by one (`record.status_rows`:
  the row changed last, then synced last, then of the highest Datamart id, of each word of each report);
  `_location_cells`, `location_rows` and `disaggregation_rows(record=)` the rows counted against the limit, what
  the report status and the tab's own filters leave out, and each line with the Datamart ids of its rows and the
  one read (`reached.Cell.record`, `records`). On the pages every hook is None: same code path, same SQL, same
  figures (tests pin it). Each report's sections are `progress_reports`' `indicator_sections` (the PD's joined
  sections once `with_pd_details` moves the indicator reports naming none), its partner `reporting.partner_key`,
  its programme document on Home `reporting.pd_follow_up_key`.
- **On the page and rebuilt.** *On the page* is the page's own call: `services.partner_reporting` and `left_out`
  (Progress reports), `services.reporting_figures` and `home.follow_up_rows` (Home), the stored `reporting_table`
  of the two tabs (or the table just computed when none is stored). *Rebuilt* counts the figure again from the rows
  kept (`trail.TrailRows`: per row its Datamart id, progress report, indicator report, the step that left it out
  and why; per progress report kept its status, sections, month, partner and programme document; never a payload),
  one selection per figure. "All N figures rebuilt from the rows: the same."; a difference is listed in red at the
  top, never hidden. Both cells are written as the page writes its numbers (`ui.number:0`: Program Performance's
  Reached of a percentage, 33.33, reads 33), the unrounded value in the cell's title; *Same?* compares the
  unrounded values. A figure the page does not draw for the filter says why ("not drawn: none left out", "not
  drawn: no progress report": the section table's totals when the page says "No progress reports match.", Home's
  partners line when its scope has no progress report). Every number those pages draw carries `data-figure="<key>"`
  (drawn for everyone: an attribute, nothing visible; the month chart's are its data), the keys the comparison
  sheet uses.
- **CSVs** (`trail_rows`, `what`): `kept` (one pivot table rebuilds the tiles, the section table and the month
  chart), `step:<filter>`, `step:start`, `step:history`, `step:S`, `reason:<why>`, `figure:<key>`, `no_partner`,
  `status` (every row of the reports kept, which set their status: `status_word_from_record_id` names the row that
  gave the report its word: of the rows carrying it, the one changed last, then synced last, then of the highest
  Datamart id), `reports` and `follow_up` (one row per progress report: the export's columns, the vendor number,
  the match key), `lines` and `blocks` (By location's lines and By disaggregation's blocks, computed again with
  their record ids); the texts of these four (partner, PD reference, report #, section, CP output, indicator,
  location, progress report key) are cleaned by the people rule as the partner reports' columns they come from
  (`trail_rows.REPORT_TEXTS`), and the match key is built from them. A row CSV starts with `figures_as_of`, `step`, `reason`, then the units (`unit_key`,
  `indicator_report_unit`, `progress_report_unit`, `partner_unit`, `pd_unit`) and `counts_once*` (by a seen-set on
  the unit, never by row order; in a step's CSV only on a unit the step leaves out entirely, `unit_partly_kept` and
  `progress_report_partly_kept` marking the others; `counts_once_partner` once per partner among the reports of the
  statuses chosen and once among the others, so its sum over `report_status_chosen` *yes* is the page's Partners),
  what NeuroDB read of the report, then the record through `raw.csv_record` (the people rule). Streamed in one
  snapshot 2,000 records at a time, at most `raw.csv_limit("partner_reports")` records (400 above), one of the two
  download slots (503); file `trail_<page>_<what>_<YYYY-MM-DD>_<HHMM>.csv`, the end of the last partner reports
  sync.

**Partner monitoring's trail** (`datamart/trail_monitoring.py`; its CSVs at the end of `trail_rows.py`), for every
figure of the grid (the tiles, their hints, the status legend, the sections chart's data, the list's count),
Program Performance (its indicators, uncut, as the comparison sheet counts them; every section tile and *Every
section*; the table's count; each row's Reached), Data quality (its ten tiles, each list's count, the same-title
note's two counts, each same-title row), the map (its seven tiles and its totals line) and Home's Indicator
progress block (its nine figures: `/check/home/?block=indicators&<keep>`, `home.figure_queries`' `keep`). The
check line carries the resolved filter (`views.monitoring_filters`, which the page reads too: the sections
written out, the year always) and `view=`. Parts: `figures` (the grid, Data quality and the folds), then, folded,
`performance` and `map` (open when the trail was opened from them: `check_views._monitoring_params` keeps the
map's `view=map`, which the page's own views do not know, so its trail opens the Map part and *Open the page*
goes back to the map).

- **Steps.** D1 to D4 of `pd_indicators`, D5 (its rows naming no programme document NeuroDB holds) and the
  programme documents in NeuroDB by the sync that last wrote them (`updated_at` from the start of the last
  Datamart `interventions` run to the end of the `intervention_budgets` run after it, which saves each programme
  document again with its budget; else the same for the REST sync's `interventions` and `intervention_details`;
  else earlier; none is ever removed; the REST sync writes PCA, whose status decides "running"); M1 the programme documents NeuroDB holds and M2 to M10 each filter
  of `monitoring._pd_queryset` that applies, with their PD indicator rows (`D4 − D5 − the PD steps' rows = M11`,
  a red line if not); M11 the rows of the PDs kept and each filter of `_indicator_rows` (search, tags,
  emergency, the HR-only rule), with the indicators left out entirely and partly; M16 one per indicator; the
  filters of `collect` and `indicators` (CP output, locations, sections, tracking status); K, the indicators kept;
  K2, the programme documents kept with no indicator of the filter (no PD indicator row, or every indicator left
  out by a later step: the difference a Power BI count of running PDs shows, check 10). Program Performance's
  part reads the filter without its sections (`indicators(every_section)`), then the sections chosen after the
  tracking status by `program_performance_rows` itself.
- **The Reached fold** (`trail.Recorder`, fed from inside `monitoring._attach_reached`: each row's pass, indicator,
  way and whether the indicator read it; `reached.compute(record=)`: the rows `one_indicator` leaves out and those
  at other places under a location filter; `reached.progress_values(record=)`: PRP's report totals found and
  kept; each indicator's reports as `compute` returns them): D1 to D4 of `partner_reports`, R0 (a PD NeuroDB does
  not hold, or none), R1 (a PD the filter does not keep), R2 (the other report type), R3 (read), R4 (placed to an
  indicator of the filter: by id, title, PD output, a tie-breaker), R5 (placed to one the filter leaves out), R6
  (left out, by `identity.WAY_WORDS`, the words check 7 uses too), R7 (the monthly pass), R8 (`one_indicator`),
  R9 (other places), R10 (reports not counted, by status), R11 (counted, no value), R12 (PRP's totals, with
  `prp_indicator_reports`' counts), R13 (the reports that count, from PRP's total or a total repeated on every
  location), R14 (indicators with Reached, partial history). Each step is the one before less what it leaves out;
  a row read that no outcome explains is a red line. **Overdue reports** (`monitoring.overdue_rows`, which
  `summary` counts with `reporting.report_count`): O1 the rows of these PDs and report type, O2 not submitted, O3
  due before the day, O4 their progress reports (counted again from `reached.report_key`).
- **On the page and rebuilt.** *On the page* is `summary` and `data_quality` of `stored_indicators` (Home's with
  its own filter), `program_performance_rows` and `section_counts`, `stored_map_points`. *Rebuilt* counts each
  figure again from the indicators the steps keep, with the page's own tests: `monitoring.FIGURE_TESTS` (with a
  target, with Reached, reported zero, Data quality's without and zero Reached, the tracking statuses, missing
  target, differs from PRP, partial history, running, active, ended) and `FIGURE_KEYS` (an indicator's PD and its
  linked partner), which `summary` and `data_quality` now count with (same values). The map's place figures are
  counted again from its stored places. **One build, not two:** when the page's indicators are not stored, the
  trail's recorded `indicators()` is the page's, stored under the page's own key once the trail's snapshot
  closes (nothing is stored inside a transaction); only a warm stored value makes the trail build them once
  more, recorded. On the pages every hook (`steps`, `record`) is None: same SQL, same figures (tests pin it).
  `data-figure` is on `_pm_tiles.html` (the grid and Home), the grid's legend and list, Program Performance's
  tiles, count and Reached cells, Data quality's tiles, lists and same-title rows, the map's tiles and the
  totals line `pdmap.js` writes; `kpi_tile.html` now puts it on the value.
- **CSVs** (`trail_rows.pm_selection`): `kept` (one row per indicator kept: `indicator_key` "<PD id>|<its key>",
  `counts_once`, a yes/no column per test, then `monitoring.TABLE_COLUMNS` as the Program Performance export
  writes them), `step:<step>` (programme documents for M1 to M10 and K2: number, title cleaned, partner, vendor
  number, status, type, dates, sections, offices, `unicef_focal_points (people)` as a count, PD indicator rows,
  indicators kept; PD indicator rows for D4, D5, M11 and the rows' steps, through `raw.csv_record`; indicators for
  the indicators' steps), `figure:<key>` (the indicators, programme documents, partners, partner report rows or
  places a figure counts; `counts_once_partner` for *Partners*; the same-title count by title, its `unit_key`
  the PD and a digest of the title, never its words), `reached:<R>` and
  `overdue:<O>` (partner report rows with `placed_to`, `how`, `counted`, through `raw.csv_record`), `no_partner`,
  and with `of=map` the places (one row per place and indicator, from the stored places). Limits: 100,000 PD
  indicator rows, 25,000 partner report rows; the same slots and snapshot as above.

**Tests.** `tests/accounts/test_validators.py` (who validates, one groups query, the pages' queries),
`tests/datamart/test_raw_people.py` (the rule, the pinned lists, `REVIEWED_KEYS`),
`tests/datamart/test_raw_csv_privacy.py` (people planted in every source, the eTools REST entities included,
never reach a CSV; the two exports below), `tests/datamart/test_raw_admin.py` (access, completeness, the run
facts, the filters, the eTools REST entities, the CSV's limits, queries), `tests/integrations/test_raw_copies.py`
(the fields each sync counts, never an odd name or a contact; the REST copy kept as it came, removed when eTools
no longer lists it, nothing else written, a failed or short read removing nothing; the button and the token;
the copy the REST sync keeps; a REST name taken out of another record's texts; checks 10 and 11's one rule), `tests/datamart/test_page_sources.py` (each page reads only its sources) and
`tests/web/test_plain_words.py` (the pages' words), `tests/reports/test_comparison_sheet.py` (each value is the
page's figure under several filters, Power BI's order, the formulas, the filter blocks, the day, the lists) and
`tests/reports/test_check_access.py` (the sheet's and the trail's links and addresses: validators only),
`tests/reports/test_figure_trail.py` (under fifteen filters, each figure is the page's context and its
`data-figure`, rebuilt the same, and the Kept rows CSV and its own CSV give it again; the chain adds up; the PD
status step is the left-out note; the partly kept units of a step's CSV; Home's block and its rows; the two tabs;
the hooks change no SQL and no figure, and no page passes one; the same queries whatever the number of figures;
the trail's words), `tests/reports/test_figure_trail_monitoring.py` (under fourteen filters every grid, Program
Performance and Data quality figure is the parts' context and `data-figure`, rebuilt the same, and given again by
Kept indicators and its own CSV; the chain adds up from the PD indicator rows in NeuroDB; K2; the Reached fold
adds up and says why (an unknown title, a PD output matching none, the monthly pass, other places); Data quality
with a year; Overdue reports; Program Performance's indicators past the patched 2,000; the map against its API;
Home's Indicator progress; the hooks change no SQL and no figure and no page passes one; a cold trail is the page's
one build; the same queries whatever the number of indicators; the sync note; the words) and the trails' part of
`tests/datamart/test_raw_csv_privacy.py`.

### Compare with Power BI (Admin → Data and sync)

The user downloads the PRP Power BI report's visuals as Excel files and uploads them; NeuroDB recognises each
export by its columns, reads Power BI's filters, and compares every row with the NeuroDB page that shows the
same thing, for the same filter (`powerbi_kinds.COMPARERS`, one per kind; below). Each difference gets a reason
and one of four groups, each with what to do. No figure of any page changes: the comparison only reads.

**Who and where.** Validators (`roles.can_validate`) with staff status; a 404 for anyone else, as Raw eTools data.
*Data and sync* lists it third, after Raw eTools data (`admin_site._datamart_pages`); the admin home has a Quick
action and Raw eTools data's first page a link. Pages (`datamart/powerbi_admin.py`, `datamart/admin.py
site_urls`): `datamart/powerbi/` (`datamart_powerbi`: the form, *How to export* with the 13 visuals in Power BI's
order, the last 20 uploads with their exports read and files not read, refused or failed),
`datamart/powerbi/<pk>/` (`datamart_powerbi_upload`: *What to do now*, a failed upload's error first and never
"Nothing to do now", the filters to clear named by their slicer, `comparison.POWERBI_FIELD_SLICERS`; one line per
Power BI page in `POWERBI_PAGE_ORDER`, an export a failed upload never read as *Not read*; the files not read, the
hidden files, the columns dropped; a meta refresh every 5 s while queued, reading or comparing),
`datamart/powerbi/<pk>/<export_pk>/` (`datamart_powerbi_export`: its filters clause by clause, its columns, its
notes; another upload's export is a 404) and `datamart/powerbi/<pk>/again/` (`datamart_powerbi_again`, POST:
*Compare again*). Refusals are written in the response or kept on the upload (`refused`), never in a message. No
ModelAdmin: no developer table shows the rows. Every sentence is in `datamart/powerbi_words.py`.

**The upload, in the request** (`datamart/powerbi_read.py`). The POST view is CSRF-exempt so that it can set
`request.upload_handlers = [powerbi_read.UploadHandler()]` before the body is read; its inner function checks the
CSRF token. The handler keeps every file in memory, never in Django's temporary files: a file above 25 MB is
dropped (named), more than 50 MB in all stops the upload ("Nothing was kept"). A zip is read entry by entry (at
most 200, counted from its end record before `zipfile` parses its directory, `powerbi_xlsx.too_many_entries`: a
directory larger than 1 KB for each entry allowed is refused too, whatever count it declares; `__MACOSX/` and
names starting with "." skipped and counted; a nested zip and a non-Excel entry refused; an entry's declared size
checked before it is opened). Each export (at most 30): an OLE file is "protected", a file that is not a zip,
with more than 500 parts (counted the same way) or a part declared above 300 MB is not Excel, more than 400 MB
declared over the whole upload stops the rest. Then `powerbi_xlsx.peek` reads the header (within five rows) and
which cells of the next 100 rows hold a value (never their values), and `powerbi_exports.recognise` finds its kind
by its captions (whatever their spacing and case; a kind's `excluded` captions rule it out, so a grouping column —
Section, the month, PD # — is never taken for the partner cards; the month chart needs its month and at least two
statuses, since Power BI writes only the statuses in its data; the most captions covered wins; a tie is refused
naming both visuals). A position holding values under no caption (Power BI lets a column's name be a space) is a
column NeuroDB does not know: the file is read in the request with it dropped, never staged. An unknown file is
refused naming only captions NeuroDB knows (its first row may be a list of people). Each file is processed inside
its own `try`: an error keeps only its class name ("NeuroDB could not read x.xlsx (ValueError).").

**Staged or read now.** A one-sheet export whose every column NeuroDB keeps is *staged*: `powerbi_read.repack`
streams the sheet in the request and writes NeuroDB's own copy of it under a workbook NeuroDB writes: the
header's captions, the filters line and the values of the kept columns only, as inline strings and numbers (no
other sheet or column, no shared string, comment, header or footer, link, data validation or document property;
a column with no caption whose values start after the 100 rows the header check looks at never reaches it; at
most 50 MB of copies per upload; 100,000 rows take a few seconds), kept in `PowerBIExport.content` until the job
reads it. Any other export (a column that names people, one NeuroDB does not compare, a second sheet) is read in
the request if its sheet is at most 20 MB, keeping only the kept columns; above that it is refused ("remove those
columns in Power BI and export again"). A staged copy holds the kept columns' values as Power BI wrote them, its
texts not yet cleaned: it is emptied as soon as the job reads it, when the job fails or stops, when it cannot
start, and by `close_stale` (called by every page, `busy()` and the scheduler's `tick()`), so it never outlives a
few minutes.

**The reader** (`datamart/powerbi_xlsx.py`) is NeuroDB's own, standard library only, the same in tests and in
production (openpyxl never reads an upload): `zipfile` and `ElementTree.XMLPullParser`, any namespace prefix,
cells with or without references, shared or inline strings, the 1904 date system; each row is dropped from the
tree once read, so memory does not grow with the rows. A part with a `<!DOCTYPE` before its first element is
refused; production relies on Python's expat limits for entity expansion. A column not kept is skipped by its
position: its shared string is never looked up, its text never converted.

**People.** The *UNICEF Focal Points* (PD/SPD Report) and *Focal Point* (Partner Fund Monitoring) columns, and any
caption `catalogue.holds_person` finds, are dropped by position as the file is read: never stored, shown, logged,
exported or sent to the AI ("Dropped as the file was read: UNICEF Focal Points (people's names). It was never
stored."). Free texts (titles, partners, sections, outputs, locations, donors) pass the people rule of Raw eTools
data (`comparison._clean`: known names whatever their case, e-mails, phones), once per distinct text (a memo of at
most 5,000). Codes (PD #, Report #, P-codes, grants) and status words pass the same rule: one is kept as read
(spaces and all, so it still matches; a cleaned code would read like another) unless the cleaner withholds part
of it (an e-mail, a phone, a known name, a link), and then it is not kept at all (left empty: nothing is matched
or shown on "[email withheld]") and counted in the export's notes (`powerbi_read.names_someone`). A Yes/No column
keeps Yes or No only; any other text is a value NeuroDB could not read. File names are cleaned the same way.

**Power BI's filters** (`datamart/powerbi_filters.py`). The "Applied filters" line is read clause by clause into a
`Filter` (`PowerBIExport.filters`) and one description per clause (`clauses`), rebuilt from what was understood:
the raw line is never kept. Applied: "field is value(s)" for `country_name`, `status`, `reporting_year`,
`section_name` (the PDs' section names), `report_status_label` and Power BI's own `is_period_latest` and
`is_blueprint_canonical`; a comparison (with "or is blank") for `actual_value`, a comparison for `location_value`.
Another shape is "cannot apply"; a field NeuroDB does not know keeps its name and condition words, never its
value, and keeps no name either when the name could be a person's (the people cleaner withholds part of it, a
known name written with "_" or "." too, or it is two words joined by a dot, as an e-mail's first part: "a field
NeuroDB does not know"); a value outside its field's vocabulary is not kept; another country makes the export
"Not compared". Once an export's kind is known, each clause's state is its kind's (`powerbi_kinds.clause_states`,
`APPLIES`): an applied clause on a field the kind's comparer does not apply is one NeuroDB cannot apply to that
export (a year or a section on the funds pages, which have neither; a report status beyond the report pages and
Program Performance's Accepted alone; `actual_value` beyond Data Quality; `location_value` beyond PD/SPD
Report/Location; several years where one year's Reached is read), so its rows say "A filter NeuroDB cannot
apply" and *What to do now* asks to clear it; a year on PD/SPD Indicator (its NeuroDB page has no year) is
applied to Reached while comparing. "What its filter covers" says only what is applied. The
comparison compares with NeuroDB's pages, and none of them reads `is_period_latest` or `is_blueprint_canonical`:
both are "applied as NeuroDB reads it" (the newest row of each report and location; each PD indicator counted
once), and the export's note says whether every partner report record of the last sync carries the field
(`raw.keys_kept`, Raw eTools data's fields) or not.

**What is kept** (`datamart/models.py`, migration `datamart/0019_powerbi_compare`, three new tables):
`PowerBIUpload` (status, who, when, `queued_at`, the day the statuses are read for, Power BI's refresh, the
partner reports sync it reads, the files not read as sentences, a summary, `seconds` and `peak_memory_mb`, an
error of our sentence, the step and a class name only, with a database error's SQLSTATE code), `PowerBIExport` (its kind, cleaned file name, state, the staged
copy until read, its rows as gzip JSON lines of the kept columns only, the parsed filter, the dropped and not
compared columns, notes, its counts (by group, reason and rule, the comparison's notes, the PDs to check), fit
tables and verdict; never the sheet's name, which can name anyone) and `PowerBIDifference` (one line of the
comparison: its kind, group, reason, rule, detail, the values it is matched by, each differing column's [Power BI,
NeuroDB, another reading] and its "Check in NeuroDB" address). Every queryset defers `content` and `rows`.
Retention (`powerbi_compare.KEEP_FULL = 3`): when an upload is created and after every job, older uploads lose
their rows, copies and lines and keep their summary and counts ("Details removed": no link to a line, no Excel
file, "Upload the files again to see these differences"). An upload waiting to be compared or being compared is
never trimmed, nor counted among the newest: its job trims the others when it ends. A queued upload trimmed all
the same fails ("upload them again"), and an export whose rows are gone fails alone: never compared against
nothing. Kept rows of a full set of
exports take a few MB; the lines at most 60,000 an upload.

**The job** (`datamart/powerbi_compare.py`, `compare_powerbi --upload-id <pk> --triggered-by <user>`, started by
*Upload and compare* and *Compare again* through `background.start_command`). It first lowers its CPU priority
(`os.nice(10)`) and caps its data (`RLIMIT_DATA`) at what its container has left (`powerbi_compare.memory_room`:
the cgroup's limit less what the container uses, reclaimable page cache not counted, less 150 MB for the web
workers and the scheduler's jobs), at most 600 MB and at least 250 MB, kept as the upload's
`summary["memory_limit_mb"]` beside `peak_memory_mb`: an overrun ends as "The comparison stopped while comparing
Program Performance (MemoryError): upload the files again." instead of the container being stopped ("press
Compare again" when every row is kept).
Its lock `background.POWERBI_COMPARE_LOCK_ID = 7140443` (not a `SyncRun` job, not in `LOCK_IDS`): a second job
fails its upload ("Another comparison was running"). An upload is refused while a comparison, a Datamart sync or
a KM Database load runs (their locks); the check and the queueing are one step under a transaction lock of their
own (`background.POWERBI_QUEUE_LOCK_ID = 7140444`, held by `create()` and `again()` until their transaction
ends), so two uploads at once, or *Compare again* beside an upload, never queue two comparisons: the second is
told one is running. A sync or a load that starts while the files are read stops the job after reading (the rows
stay: *Compare again*). Steps:
read each staged copy and empty it; the day (Power BI's refresh from "Last Updated Date" or "Last Modify Date",
else the upload's day; today when more than 14 days back, with a note); the partner reports sync it reads (a
note when it ran before Power BI's refresh, their moments compared; `sync_moved` when one finished meanwhile);
each export in Power BI's order. `close_stale` fails an upload reading or comparing while nobody holds the lock,
or queued for more than two minutes since `queued_at` ("The comparison stopped before it finished (the server
restarted or ran out of memory)"; "press Compare again" when every row is kept). A start that fails fails the upload in
the request. Logs carry counts, kinds, steps, class names and SQLSTATE codes only. Each upload keeps its own
`seconds` and `peak_memory_mb`: read them on the first production uploads to size the next steps.

**A comparison that stops says where and why** (`powerbi_compare.failure`, `powerbi_words.failure`), so it can
be fixed without the server's logs: the step (an export's: reading its rows, NeuroDB's page, the facts behind
the reasons, matching the rows, saving its result, writing its differences, found from the modules the error
went through; the upload's: reading the files, the day and the sync, preparing, comparing a named visual,
writing the differences, the summary), the exception's class and, for a database error, its SQLSTATE code with
what it means ("Could not be compared at NeuroDB's page: DataError 22012, a division by zero"). A database
error with no code is the database client's own (libpq's "out of memory for query result" is one): it says
so, and "at the job's memory limit" when the job's memory peak reached 90% of its data limit. Never a value, a
query or the database's own message. An export that fails keeps the step in `counts["failure"]` and nothing
else of its counts or fit tables (no "Needs a look" over lines that were removed). One row whose values NeuroDB
cannot compute with (an arithmetic, value or type error while the row is compared: `powerbi_kinds.RowGuard`)
is a line of its own, "A value NeuroDB cannot read", and the export goes on; a database or memory error still
stops it.

**The comparison** (`datamart/powerbi_kinds.py`, with `powerbi_match.py`, `powerbi_page.py`,
`powerbi_facts.py` and `powerbi_results.py`). Each export is compared in its own `raw.snapshot` (REPEATABLE READ
READ ONLY, 120 s a statement), the PD/SPD Report list and the Data Quality list first (the counted visuals and
the card are explained from them), then Power BI's order.

- **NeuroDB's side** (`powerbi_page`) is the page's own function with Power BI's filter given explicitly: its PD
  statuses (`status is active` is `active` only; no status clause, every status), its reporting years (period
  end; with one year, Program Performance's and PD/SPD Indicator's Reached is the year's, `reached_in_year`, as
  Power BI's Reporting Year reads it), its sections, and on the report pages its report statuses (Power BI's
  Due covering not yet due): `services.partner_reporting` (the report list, the follow-up list, the partner
  cards, the table by sector and the chart by month, counted as **progress reports** as Power BI counts them),
  `monitoring.indicators` (Program Performance with the indicators that have an Accepted report, as Power BI's
  `report_status_label is Accepted` keeps; Data quality's `FIGURE_TESTS["no_reached"]` list, with no cut),
  `reporting.pd_indicators` on `partnerships.programme_documents` (PD/SPD Indicator), `reporting.location_rows`
  with a recorder that only gives each line's chosen record (By location; the record's own target, per
  location), `reporting.disaggregation_rows` + `fill_block` month by month (By disaggregation, read PD by PD
  when a month passes the tab's limit), `funds.partner_contributions` and `funds.funded_programmes`. Why a Program Performance row has no indicator
  on the page reads the indicators of every PD status for the PDs that hold one of the left-over rows' titles
  only (`powerbi_kinds._titles`: each PD's titles first, one DISTINCT query), never for every PD: production's
  273,253 PD indicator rows read whole took the job past its memory (a `DatabaseError` with no code from libpq,
  or a `MemoryError`). Nothing
  cached is read (`compute.once` would write the shared cache in a read-only transaction). Every value kept
  passes `powerbi_page.shown` (explicit field lists): no focal point, no `submitted_by`, no narrative, no whole
  `data`; NeuroDB's free texts pass the same people cleaner as Power BI's.
- **The same** (`powerbi_match`): raw values against raw values (float noise only, half a cent for money); a
  Power BI 0 against no value is the same in a Reached, Target or % column; a % column is compared only when its
  two amounts are the same; texts by their spaces and case, a title Power BI cut at 255 characters as a prefix;
  sections as sets (`reporting.section_names` with the PDs' names); CP outputs whole in Power BI's joined text;
  SPD = SHPD; `NON-GRANT` = no grant; a PD # without its amendment suffix pairs, with a detail. Percentage
  indicators are in Power BI's unit for each visual (×100 on Program Performance and PD/SPD Indicator, a fraction
  on Location and Disaggregation); a factor of 100 left is the rule `units`.
- **Pairing**: by the kind's match values (the report list: PD #, Report #, Period End; Program Performance:
  partner and title, then title, sections and target for a partner written differently; Power BI's row that
  joins two NeuroDB rows when its Reached and Target are their sums), most similar first. Location copies (one
  per CP output of the PD) are joined first; summing kinds (by sector, disaggregations) add up rows with the
  same match values on each side; a value missing on one side is the same when the other is 0. By
  disaggregation is compared over Power BI's months and NeuroDB's own (in Power BI's years, within NeuroDB's
  history): NeuroDB's values of a month Power BI's export has no row for are only in NeuroDB. The month chart
  compares every status: a column Power BI did not write (it writes a status only when its data has it) is its
  0, and a note names it. The table by sector's Total row is checked against Power BI's own rows and NeuroDB's
  total as notes, never as a line (its gaps are the sections'). Rows about a period that starts before
  NeuroDB's history are set aside on both sides of the report list, By location and By disaggregation
  (`Job.after_history`): NeuroDB can still hold some from an earlier sync whose window started earlier, and
  compared they would be "only in NeuroDB, not explained yet"; a note counts them
  (`NOTE_SET_ASIDE_NEURODB`).
- **Reasons** (`powerbi_words.REASON_ORDER`, the first whose test passes; a line whose columns give several takes
  the highest group): before NeuroDB's history (also a Program Performance row whose PD started before it and
  has no Accepted report in NeuroDB: its Accepted reports are older), the PD not running (group 3), a report
  still owed, not in the Datamart read (group 4 when NeuroDB's partner reports sync finished after Power BI's
  refresh, their moments compared, later the same day too; group 2 when it finished before, or when either
  moment is not known: "NeuroDB cannot tell whether its last sync ran before Power BI's refresh"), left out by
  the page, an older version (an earlier step, or several records of a
  location whose newest gives Power BI's value, when the sync ran before the refresh; after it, NeuroDB holds the
  newest record too and the line is the rule `several_records`: the page reads the largest), moved on since
  Power BI's refresh (NeuroDB a later step and its sync not before the refresh; group 1), a different rule
  (`RULES`), a status counted differently ("read for a different day" only when NeuroDB reads the statuses for
  another day than Power BI's refresh; Power BI's Overdue on the due date itself is said so), a repeated value,
  upload the report list (or the Data Quality list) to see why, a row only NeuroDB has beyond an export Power BI
  may have cut (exactly 1,000, 30,000 or 150,000 rows: reason `cut`, group 4, its own step "export it again one
  section or one partner at a time" in place of sending the Excel file; never counted in the report list's gaps),
  a counted visual's gap whose reports may be beyond the report list's cut (reason `list_cut`, group 4, its own
  step "export PD/SPD Report again in parts"; the visual's verdict is "Power BI may have cut PD/SPD Report: n",
  never that the visual itself is cut, and *What to do now* counts the report list among the exports to export
  again, not the visual), a filter NeuroDB cannot apply, a value NeuroDB cannot read (a number beyond any
  float, Infinity or NaN in eTools' or PRP's texts, left empty in its line: JSON and the database hold none;
  group 4), not explained yet. Not in the Datamart read says
  "Check in PRP that the report exists", or "Check in eTools that this PD exists" for a row of a PD NeuroDB
  does not hold and on the PD indicators' and funds exports (`powerbi_results.PD_ROWS`). A different rule
  points to *Choices for you* (its line, and the export's "counted another way" verdict) only when the upload
  offers a choice about it (`RULE_CHOICES`, `STATUS_CHOICES`); else "NeuroDB counts it another way". A candidate that is NeuroDB's own figure (eTools' target) never makes a line "a different
  rule". Facts are one unfiltered query each (`powerbi_facts.Pds`, `Reports`: every PD and progress report NeuroDB
  holds, whatever its status).
- **Fit tables** where Power BI's rule is not known: Program Performance's and PD/SPD Indicator's Reached
  (`qpr_sum`, `every_sum`, `latest`, `prp_cumulative`, `accepted`) and Target (`etools`, `prp_sum`, `prp_one`),
  counted from each indicator's quarterly and monthly reports (`powerbi_facts.indicator_facts`: placed by
  `identity.Idents`, read by `reached.build_reports`; in the one year Power BI's filter names, when it names
  one), on the rows NeuroDB can check whole (the PD started on or after NeuroDB's history, or that year starts
  on or after it), chosen on the rows where Power BI is above 0; a line's detail names each value with its
  side ("Counted Power BI's way (…): 15. NeuroDB's own rule gives 35."); Data Quality's readings of its section
  and `actual_value`; the financial contribution; Outstanding DCT (Funded Programs' page has no such column).
- **Lines** are kept in gzip buffers per group during the snapshot and written once every export is compared,
  group by group across the exports (every export's group 4, then 3, 2 and 1: the limit drops expected lines
  first), at most `LINE_LIMIT` (20,000) per export and `UPLOAD_LINE_LIMIT` (60,000) per upload; the rest are
  counted (`counts["written_groups"]`: those listed, by group). *What to do now* and the pages link only to
  listed lines, and say how many lines that need a look are counted but not listed. A detail is at most 300
  characters by design (the other rules that fit by their short names, or how many), cut on a word if ever
  longer; a Program Performance line's figure trail address leaves Power BI's sections out when they would take
  it past its 300 characters (the indicator's Reached is the same in every section), never no address. Rules that apply to every row are notes, never lines. The upload's summary keeps one verdict per
  Power BI page, the lines by group and the Reached sentence; `choices` keeps *Choices for you* (Reached,
  progress or indicator reports, Due and not yet due, Emergency) with their evidence, each offered only when its
  rule fits more than half the rows (`powerbi_results.clear_majority`; Reached: of the rows where Power BI shows
  a value, else the sentence says the best rule fits only a few).
- **Pages and the Excel file** (`powerbi_results`): *What to do now* (at most four lines, counting
  differences, not rows: a counted visual's row can differ in several columns; the exports to export again, cut,
  empty while NeuroDB has rows, or of another country; the files not read, an export whose comparison stopped
  told to press *Compare again* instead and counted among the exports read; "Power BI and NeuroDB agree" only
  when every export is settled; the same lines in the Excel file's Summary, `powerbi_results.before`, with the
  files not read and why and each page's visuals not uploaded), *How to export* filled in from NeuroDB's
  history and its one country (`settings.ETOOLS_DATAMART_COUNTRY`), one line per Power BI page with its worst
  export's verdict (with expected lines only: "Same data, counted another way" for a rule,
  a status, a repeated value or a row the page leaves out; "Same, apart from reports still owed: n" for reports
  still owed, rows before NeuroDB's history and reports that moved on; both when both); an export's page with
  its notes, fit tables, lines by group and reason and the lines (at most 5,000, 100 a page, filtered by reason
  and kind); `datamart/powerbi/<pk>/differences.xlsx` (`datamart_powerbi_xlsx`, `reports/xlsx.py`, behind
  `raw.take_slot()`: 503 while every slot is taken): a Summary sheet and one sheet per export, a row only on one
  side in wide form, a row that differs in long form, at most 100,000 rows a sheet and 300,000 in the file.

Measured here, comparing 100,000 fake disaggregation rows against an empty NeuroDB took about 35 s with a
tracemalloc peak of about 40 MB, and against a NeuroDB holding the same values (10,000 records, every row the
same) about 5 s (23 s under tracemalloc), 81 queries and a peak of about 43 MB (both tests bound it at
150 MB); read the first production uploads' own `seconds` and `peak_memory_mb` before sizing anything.

**Tests.** `tests/datamart/test_powerbi_read.py` (every kind recognised under any file name, both layouts and
string encodings, types, codes, refusals, the zip shape, staging), `tests/datamart/test_powerbi_filters.py`
(every clause shape, the order of trial, what is kept), `tests/datamart/test_powerbi_job.py` (start, lock,
refusals, stale, the command, retention, nothing on disk, a large export read whole and compared within its
memory and line limits, against an empty and a matching NeuroDB, memory not growing with the rows),
`tests/datamart/test_powerbi_privacy.py` (people planted in the files, and in NeuroDB's own records, never reach
a table, a log, a page, the Excel file, the job's output, the session or a cookie),
`tests/datamart/test_powerbi_admin.py` (access, the sidebar, the pages, the export's lines, the Excel file, kept
apart from the AI) and `tests/datamart/test_powerbi_compare.py` (every kind against a small fake NeuroDB: the
same data gives no line, each reason, the fit tables, the filters read exactly, an empty export, the page
figures unchanged, the upload's line limit, each comparer's queries the same for 3 and 12 PDs), with generated
fake exports only (`tests/datamart/powerbi_factory.py`).

### Recording real records as test fixtures

The sync tests were written from the Swagger; production has shown shapes the Swagger did not.
`python manage.py record_datamart_samples [--limit 25] [--only a,b]` reads a few records of every
dataset from the live Datamart (same credentials as the sync), removes contact details and writes
them to `tests/fixtures/datamart/<dataset>.json`; commit the files. The test
`tests/integrations/test_recorded_samples.py` replays every recorded file through its sync and
fails when a record cannot be written, so a Datamart change is caught by the test suite rather than
by the nightly job. Re-record after a Datamart release.

The field monitoring records also name people (the visit lead, the team) and hold free texts that can
name anyone. Before committing samples of `field_monitoring`, `fm_questions`, `fm_options`,
`fm_programme_activities`, `offices`, `sections`, `action_points`, `intervention_locations` or
`location_sites`, run `python manage.py fmm_redact_fixtures tests/fixtures/datamart/` (`--dry-run`
counts without writing): every text under a key that holds a person (a notes or comments key
included) becomes "Person 1", "Person 2"... (the same person keeps the same number across the files),
every other text loses its e-mail addresses, links, phone numbers, known names, the names found under
the keys that name people (not the words of notes or comments) and names after a title, and a text
over 300 characters is cut. Read the diff before committing: a name NeuroDB does not
know, written without a title, can remain.

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
report, indicator and location). The partner reports carry no PD indicator id: the one key the sync
reads for it, `etools_cp_output_indicators_id`, is empty or missing on every row (20 Oct 2026), its name
says it belongs to the CP output's indicators, and nothing has confirmed it as the PD indicator's
`source_id`; the Datamart's partner report records name no other PD indicator id. A report row belongs to
the PD indicator with that id when it gives one; else to the one indicator of its PD that has its title
(case and spaces aside); else, for a title two or more indicators of the PD share, to the one that also
has the row's PD output (`pd_output`, compared with the indicator's output, `lower_result_name`). A row
none of these places is left out (two indicators are never merged): Data quality's *Same title twice in
one PD* says how many indicators get none of their rows while rows of their title were left out (their
output is shared too and no tie-breaker tells them apart, or the rows' PD output is empty or matches none
of theirs: PRP's output name is not confirmed to be spelled as eTools'), and how many rows were left out
for an output that matches none. The eTools reporting checks (check 7) count the rows matched by id, by
title, by output and title, placed by a tie-breaker, and left out, by cause.

**Indicators that share a title and an output are told apart by tie-breakers** (`neurodb/datamart/identity.py`,
the one place every page, export, check and Ask NeuroDB tool reads it from). Their rows are placed per
*placement group*, never per row: the rows of one progress report with one title, one PD output and one
signature (the row's `high_frequency`, and PRP's `cluster_indicator` and `indicator_type`, read from the
record kept in `data` for these rows only), so a report is never half placed. The tie-breakers, in this
order: **high frequency** (`high_frequency` and `is_high_frequency`; a monthly report also never goes to an
indicator neither high frequency nor a cluster's), **cluster** (`cluster_indicator`, where "none", "null",
"n/a" or a dash say nothing, and whether `cluster_name` is given), **target** (`target`, the same on every
row of the group, and `target_numerator`/`target_denominator`), **indicator type** (`indicator_type` and
the display type and unit the proof learned for it), **disaggregations** (the disaggregations the group's
value ids belong to, when every row names the same ones, and `disaggregation_name`), **planned locations**
(the rows' P-codes and `location_pcode`: only when the latest PD indicators sync read them whole, that is
succeeded, removed what the Datamart no longer holds and its answer was not cut: a round count read in one
answer, not in windows) and **active**
(`is_active`: exactly one of them is). A tie-breaker decides when exactly one indicator matches. The group
goes to it only when every other tie-breaker in use that can say something matches the same one (one that
matches none finds the group unlike every placed one: an amended target, or two indicators in one group),
when no two rows of the group at one location carry different values (both indicators may be in it), when
no row is at a P-code another indicator plans and the chosen one does not (by the places held, whatever the
latest PD indicators sync was: this only keeps rows out), and when the chosen indicator gets no other group
or row of that progress report (Reached would take the larger of two). Otherwise the group stays out, and check 7 says why. An indicator placed by
*active* carries the note "Its programme document also holds an inactive indicator with this title and
output: the partner reports are read as this one's." Means of verification and the section are shown by
check 7 only (free text, and the section may be the PD's). The CP output a PD indicator takes from its
partner reports (`_CP_OUTPUT_BY_TITLE`) is no tie-breaker: every copy of a title takes the same one.

**A tie-breaker is used on the pages only once the data proves it.** After each `partner_reports` and
`pd_indicators` sync, `identity.calibrate()` compares each tie-breaker, on the groups of every partner
report row NeuroDB holds that were placed by id, title or output, with the indicator they were placed
under: it is used when it was compared on 20 groups or more (`CALIBRATION_MIN`) and never differed (the
indicator type when, besides, each type goes with one display type and unit, and no pair with two types).
The proof is stored in the run's details, `details.identity` (`version`, `enabled`, `types`, and per
tie-breaker `compared`, `same`, `differ` and `not_given`: counts and fixed words only); a failure is
`details.identity_error` and never fails the run. Every page reads the proof of the newest finished
`partner_reports` or `pd_indicators` run, once per request, and ignores one stored under another
`identity.CALIBRATION_VERSION` (bumped whenever a rule changes). With no proof (a new install, a run before
this release) no tie-breaker is used, exactly as before. To see what each tie-breaker says and why it is or
is not used, read check 7's Table A (or the run's `details.identity` in Admin → Import and sync runs).

**A progress report is its programme document, report type, number and period** (`reached.report_key`,
`reporting.REPORT_KEY` in the database): every page groups a report's rows, counts it and reads its status
by it, whatever PRP's own ids hold. On the Lebanon rows check 5 found no indicator with several PRP
progress report ids (the id is empty, or one value for many reports: check 7 now counts the rows with
none), and an id could stand for several reports; reports were once told apart by that id first, then by PRP's
indicator report id, then by number and period, across programme documents. PRP's progress report id
still opens a report's pop-up when all its rows are of one report, breaks the tie between two reports
ending the same day, and tells reports apart on a row that names no programme document at all, or no
number and no period. Within a report, one indicator (an *indicator report*) is PRP's indicator report
id, else its eTools id, title and PD output.

What this changed on the pages, with the Lebanon rows (PRP progress report ids that tell no report
apart, no eTools indicator id): figures stay the same wherever PRP's ids told reports apart (proved on a fixed set of reports in
the three shapes PRP's ids can come in, `tests/reports/test_report_identity_figures.py`); they change only
where reports were read as one that are not, or values were left out:

* a programme document's report no longer takes the status of another PD's report of the same number
  and dates changed later (an accepted QPR1 could read Due and stop counting in Reached);
* a report whose rows disagree on its status (a partial re-sync) is one report with its newest word on
  Partner monitoring, Progress reports and Report/Location (it was listed once per word, and each
  indicator read its own rows' word);
* two indicators of a PD that share a title get their Reached when their outputs differ (both were Not
  reported); Report/Location and the tiles count them apart;
* Report/Location gives each programme document's report its own line (rows of one title, number and
  dates were pooled across PDs, showing the largest value);
* the home page's progress reports this year, the brief's quarterly reports, the daily review's
  overdue reports and Partner monitoring's overdue reports count each report (with no PRP id they
  counted none, or one per partner or PD), and every report's pop-up opens from the lists.

What the tie-breakers change, once a sync has stored a proof that enables one, and only for the indicators
a tie-breaker places (each a correction: those rows were left out):

* Partner monitoring's grid and Program Performance (and their CSV and Excel, which mirror Power BI):
  Reached, Reached in the year, % Reached, awaiting acceptance, status, *Reported*, PRP's cumulative and
  *Differs from PRP* of the placed copies (Not reported becomes a value); an indicator that now has
  quarterly rows stops reading its monthly ones (*Read from*); the status tiles and the summary move away
  from Not reported. So does every reader of the same figures: the overview, the brief, the country
  programme, the home page, the daily review, the Monitoring insights panels, youth linking and Ask
  NeuroDB's partner monitoring tool;
* Data quality: fewer indicators without Reached (perhaps more with zero Reached), fewer rows left out;
  checks 5 and 7;
* Report/Location, Report/Disaggregation and the report pop-up: the type, unit and target of a shared copy
  are its placed indicator's;
* unchanged: Progress reports' counts and tiles and check 1 (an indicator report is still told apart by
  PRP's indicator report id, else its eTools id, title and PD output), and the PD/SPD Indicators export.

Two changes apply with no proof at all: Data quality's same-title list counts an indicator as left out only
when it gets none of its rows while rows of its title were left out (two copies sharing a title and an
output that no partner report row names were counted before; they are simply not reported, and *Without
Reached* still counts them); and Report/Location, Report/Disaggregation and the pop-up read an indicator
report under a shared title and output with the type, unit and target its copies have in common, and as a
number when the copies differ (they took whichever copy the database returned first), while one whose
output matches none of its title's indicators reads the first of them in Partner monitoring's order.

To see what is still left out, read Partner monitoring → Data quality → *Same title twice in one PD*
(with every PD status and no filter) and the eTools reporting checks, check 7.

**Reached: one rule for every page** (`neurodb/datamart/reached.py`; the grid, the indicator detail,
the map, the programme and partner pages, the progress report pop-up and Ask NeuroDB all read it):

* **Which reports count.** Accepted and Submitted reports. What Submitted reports add is shown as
  *awaiting acceptance*, so a later send-back is understood. Sent back, Due, Overdue and not-yet-due
  reports never count, whatever they carry; *the latest report* is the latest counted one. The six
  statuses: PRP's word or code decides Accepted, Submitted and Sent back; a report PRP calls Due or
  Overdue never counts (with a submission date it is Due); any other report with a submission date
  is Submitted; a report not submitted is Overdue once its due date has passed, Not yet due while
  its period runs, else Due (the same rule as every "overdue" figure).
* **A location's value in a report** is the disaggregation total `"()"` (its calculated value, else
  numerator over denominator), else the location's running total minus its previous one, else the
  value in the period. When every location row of a report carries the same value in the period,
  it is the report total repeated on each row and is not added up (a data note says so). Two rows
  of one location in one report are never added: the largest is used.
* **A report's value** combines its locations by the indicator's method across locations (sum,
  maximum, average; percentages and ratios as the sum of numerators over the sum of denominators).
  PRP's own report total (`progress_value` of `prp_indicator_reports`, found by the indicator report
  id and used only when that record names the same programme document and indicator) stands in when
  the locations cannot say; when it differs from them a data note says so and the locations are
  used. "The same" allows only for rounding: half a person for a count, half a point for a
  percentage.
* **Reached** combines the counted reports by the method across reporting periods (sum, maximum,
  average, latest; percentages and ratios as numerators over denominators): to date, in a year, per
  month (each report in the month its period ends), per location and per disaggregation key. For an
  indicator summed across locations and periods the months of a year add up to *Reached in* that
  year, the years to Reached, and the locations to Reached; for any other method the page names the
  method next to the figure. Different indicators, percentages, and quarterly (QPR) and monthly
  humanitarian (HR) reports are never added together: one report type is read at a time (QPR by
  default; HR only for high-frequency and cluster indicators), and an indicator with no quarterly
  report at all is read from its monthly ones (marked *From monthly reports*).
* **PRP's own cumulative** (`total_cumulative_progress` on the latest counted report) is shown beside
  Reached; an indicator where the two differ (by more than rounding, as above) is marked *Differs
  from PRP*. A programme document that started before the date the latest partner reports sync
  read from (`details.read_from` of that run, see `partner_reports` above: only closed PDs while the
  setting is on, once a sync has run with it) has a partial history: its Reached is PRP's
  cumulative, marked *Partial history*. A sync read in windows records instead the first start it holds
  whole (`details.whole_from`), read first: the date read from when every window was whole, else the end
  of the latest window not read whole, never one of reports starting after today (the open last window:
  recent reports missing is no history question, and it never hides an older gap). It chains from night
  to night: a window not read whole tonight removed nothing, so the rows the previous run held whole for
  it are still there, and it counts as whole when the previous finished run's `whole_from` is at or
  before its start (a run read before windows, or in id windows every one whole, counts with its
  `read_from`, except when its count was a round limit: its history was cut, and tonight's windows
  decide, also when tonight's read changed as a whole). A run that proves no history whole
  (`datamart_sync.history_cut`: an answer cut at a round count or whose window filters the Datamart
  ignores, an id window not read whole, date windows that proved none) gives instead the earliest period
  start held in the window it read: a cut that drops the oldest reports is seen; one in the middle of the
  history is not. One window failing once never moves every early PD to PRP's cumulative for one night.
  Each partner reports run also records where the rows it holds start, after its removal (or the removal
  held one night: those rows are still held and count), `details.feed_from`; when that is more than 100 days
  (`LATE_FIRST_DAYS`, about the time a PD's first report ends) after the date the run holds whole from
  (`whole_from`, else `read_from`), the Datamart holds no earlier report whichever way it is read, and the
  history is counted from it (`reached.history_from`, source `feed`), whole read or not: a read not whole
  removed nothing, so the rows earlier reads held are still there (on 21 Oct 2026 the read asked from 1 Nov
  2018 and the earliest report held starts 1 Jan 2026: the 21 running PDs that started before it get
  *Partial history*). Data quality's *Partial history* panel then says "the Datamart holds no earlier partner
  report (NeuroDB asked from 1 Nov 2018)". Reached becomes PRP's cumulative only where PRP gives one; the
  other indicators of such a PD still add up the reports held, and the PD is listed as partial. A run made
  before `feed_from` was recorded changes nothing: the figures move at the first partner reports sync after
  the deployment.
  After the window is widened (the setting turned on, a
  deployment), a running PD's early reports count from the next eTools Datamart sync on; until then
  it shows *Partial history*.
* **A location filter** shows each indicator's Reached at the chosen locations; its % is a share of
  the whole PD target. A place is matched by name and by the P-code the PD indicators give that name,
  so a partner report that spells it differently ("Akkar Governorate" for the planned "Akkar", same
  P-code) is still that place. On the map, the chosen places only are listed, each row with the
  location's own Reached beside the indicator's Reached over every location, its % and status.
* **One status per progress report.** A report whose rows disagree on its status (a partial re-sync:
  some indicators' rows still carry the old word) has one status on every page: the word of its rows
  changed last in the Datamart (`last_modify_date`), then synced last, then with the highest Datamart
  id, read from every row of the report (its programme document, type, number and period) whatever the
  filter. One indicator in one progress report (an *indicator report*) is identified by PRP's indicator
  report id, else by its eTools id, title and PD output: two indicators that share a title or an eTools
  id are never read as one, on the pop-up and on *By location* as on the tiles. When a report gives two
  indicator reports that one PD indicator's title matches (two PRP indicators with one title: two PD
  outputs anywhere in the report, whatever locations each reports, or two PRP indicator report ids at
  one location), only those under the indicator's own output count; when the output does not tell them
  apart, that report is left out of the indicator's Reached with a data note, never the largest of them
  nor their sum. Several rows of one indicator report at one location still give its largest value.

**The pages.** The grid lists, per indicator: Target, Reached (as of the latest counted report),
% Reached, Reached in the chosen year, Awaiting acceptance, Status, the partner's own view, and the
months of the year; *All years* in the year menu shows one column per year instead. The indicator
detail is a location × report matrix of every report of the type (every year; not-yet-due reports
left out): each row ends with the location's Reached, each column with the report's value, the
corner is Reached; reports that do not count are greyed and stay out of every total. The progress
report pop-up shows each location's value parsed (never PRP's raw text) and a total row that follows
the method. The programme document page lists Reached on each indicator with its latest counted
report. *Progress reports* counts indicator reports by the six statuses (they add up to the total;
see *The Power BI views* below), and the report lists of the programme document and partner pages
show the same status for each report.

**Statuses.** *On track / Off track / Ahead of schedule* compare Reached's share of the target with
the share of the PD period (start to end) elapsed, ±10 points, the same rule the ActivityInfo pages
use with the calendar year; an indicator ahead of schedule that has passed 100 % of its target reads
*Over target* (the stored key stays `over_target`); *No target* when the PD sets none; **Not
reported** when no counted report carries a value (an indicator nobody reported on, or whose only
reports are due, is never called off track); *Reported zero* is noted under the status. Gender, age
group, nationality and disability tags are read from the indicator titles
(`neurodb/datamart/tags.py`). The assistant answers with `pd_indicator_progress` (Reached fields);
`etools_query` refuses to add up partner report values (`partner_reports`, `prp_indicator_reports`)
and PD indicator targets (`pd_indicators`, one row per location and disaggregation), and points to
it. Per-location targets are not in any country-filterable Datamart endpoint, so
locations are compared on reported values only.

**eTools reporting checks** (admin → *Data and sync* → *eTools reporting checks*, Administrators only;
also in the admin home's quick actions, on *Data health* and on *Progress reports*): twelve read-only checks
of the partner report data the rule depends on (rows per location, whether the value in the period is the
report total repeated, disaggregation keys and the `"()"` total, what reports that do not count carry,
PRP's cumulative against Reached, the calculation methods in use, which PD indicator each report row
belongs to, PRP's report totals, whether the financial contribution repeats on every indicator of a
report, the country and programme document of the rows, the reports owed in PRP's feeds, and how the PD
indicators can be grouped into eTools master indicators), each with a verdict and what NeuroDB does
whatever the answer. Each check measures what NeuroDB reads: PRP's
values are parsed as Reached parses them (`c`, else v over d, else v, given as a number, its text or PRP's
`{v, d, c}`), and a progress report is told apart by its programme document, type, number and period,
whatever PRP's own ids hold; one indicator in it (an indicator report) by PRP's indicator report id, else
its eTools id, title and PD output, as Reached and the tiles tell them apart. Every figure is a count: no
title, name or value of a record is shown (key names, section names, the PD numbers NeuroDB does not hold
and business area codes at most).

* **Check 1** counts the locations on several rows of one indicator report in one report, those whose
  rows carry different values and, of them, those in reports that count (a warning: Reached takes the
  largest); apart, the locations two or more indicator reports of one title give in one report (two PRP
  indicator report ids there, else two PD outputs), and those with different values (Reached keeps the
  one under the PD indicator's own output, else leaves that report out); and the locations Reached reads
  as one report across several (none since a report is its programme document, type, number and period:
  a guard). The same-value and different-values words come from the values themselves.
* **Check 2** counts, per indicator report with several locations, those with the same `"()"` on every
  location (all zero; above zero in a report that counts and adds up its locations: the risk it is for),
  those whose value in the period is the locations' `"()"` combined, and the rows whose `"()"` gives no
  number (the only ones the repeated-total rule reads). Two indicators of one title under different PD
  outputs are two indicator reports here, as on the tiles.
* **Check 3** counts the rows whose `"()"` holds a number (the others Reached reads from the running
  totals or the value in the period), the value in the period read as a number and written as `{v, d, c}`,
  the rows compared and equal (with none compared it says so, never "0 %"), and per indicator report with
  several locations whether the value in the period is each location's own or the report total repeated.
* **Check 4** counts, per PRP status, whether the report counts and whether its period has ended, the
  rows, those with a cumulative of 0, with none, and with a value given. Values are read as Reached reads
  them (PRP's empty `{c: 0, d: 0, v: 0}` is 0 there); *Value given* counts only a value other than zero:
  a zero, `"0"` or `"0.0"` counts as no value in this check.
* **Check 5** reads Partner monitoring's own figures (the running programme documents, quarterly reports
  with the monthly fallback: what the *Data quality* tab lists): the indicators with several counted
  reports, those whose PRP cumulative never goes down from one report to the next or is the same on every
  report, the indicators compared (Reached and PRP's cumulative both given, no partial history) and those
  where Reached differs, with the indicators with a partial history (and, when the Datamart holds no
  earlier report than the first one NeuroDB has, "their Reached is PRP's cumulative: the Datamart holds no
  partner report starting before 1 Jan 2026"), with a table of what they share (PRP above or below, monthly reports, PRP equal to
  every report combined or to the latest one, a cumulative that never moves, locations not read from
  `"()"`). It needs 30 seconds of the checks' time.
* **Check 7** counts the rows of the running programme documents matched to a PD indicator by its id, by
  title, by PD output and title (a title several indicators of the PD share), those of a title and an
  output two or more indicators share (told apart in eTools, or by a tie-breaker), and those left out by
  cause: such a title and output no tie-breaker placed, a title several share with a PD output that is
  empty or matches none of theirs (the output name corrected in PRP or eTools), or a title none has, with
  the indicator reports left out (each its title and PD output); its sentence counts what the pages leave
  out, after the tie-breakers. Of the rows of a shared
  title and output it counts those alone at their place in their report (PRP sends one of the copies),
  those with two or more, the monthly ones, those the tie-breakers place on the pages and those still left
  out (two of the row's fields name different indicators, two indicator reports would go to one indicator, two rows at one
  location carry different values); the titles and outputs shared, their indicators, those with exactly one
  active and those whose indicators agree on every field compared; the PD indicator records the latest
  `pd_indicators` sync did not return (records it did not touch, one per location and disaggregation),
  what it announced and read, and whether
  it skipped its removal. Table A, *What tells apart indicators that share a title and an output*, gives
  each tie-breaker (and the means of verification and the section, shown only) with its report row key and
  PD indicator field, whether the pages use it, the placed indicator reports where it was the same, differed
  or was not given (a proof made afresh on every row held: what the next sync stores), the shared groups
  whose indicators differ on it, and the left-out rows it alone would place, leaves to two or more, or
  matches none; then all of them in their order, as the pages place them (its last three cells: the rows
  placed, those none decides, and those a check keeps out). Table B lists the value pairs
  on the rows already placed (high frequency, cluster, indicator type and report type against the indicator's
  fields, and the rows placed under an inactive indicator; a value longer than 20 characters or not a plain
  word shows "(other text)"); Table C the PD indicator keys that differ inside a shared group (the key and
  the number of groups, never a value); Table D the record keys that differ between the rows of one title
  and output at one location of one report. The verdict says how many of the rows of a shared title and
  output are alone at their place, monthly, placed and still left out. PRP's
  progress report and indicator report ids (empty, how many, how many stand for more than one report, and
  the reports with more than one progress report id: counted, never a problem for the pages, which tell
  reports apart by programme document, type, number and period); and lists the keys the records send
  (rows with the key, with a value, and by kind of value: object, text, number, empty), the keys the sync
  reads first, so a key it reads and no record sends shows 0. With no eTools id on any row it says the
  key read (`etools_cp_output_indicators_id`) is empty everywhere and never confirmed as the PD
  indicator's id.
* **Checks 8, 9 and 11** count the records of PRP's indicator reports held; when none is, they say what
  the last sync of `prp_indicator_reports` did (Data health's run): never synced, still running, failed
  (with its error), asked for the business area and got no record (syncing again changes nothing: check
  the code in Admin → *Data and sync* → *eTools Datamart: PRP business area code*, or Run a job → *Probe a
  Datamart dataset*), got an answer cut short (the first page announced records the read never returned: a
  new sync may help), read only records of another business area (check 11 lists the codes they named), or
  could write none. When the code asked was not a code (a country's name, as "Lebanon"), they say so and
  which code the next sync takes instead (as NeuroDB finds it now: the admin's, the workspace it holds, the
  partner report rows), never that syncing again changes nothing; when it was a code NeuroDB found, they
  point to the admin's code, which comes first; a value set aside is named. Check 11 also says, for PRP's
  own progress reports, the code their last sync asked with and where it came from, what the Datamart
  announced, and the records left out (of another business area, or naming no business area and no PRP
  programme document NeuroDB holds). Check 8 also counts the records with a PRP id and the partner ids that
  are not whole numbers.
* **Check 10** counts the rows per country named in the record and per programme document link (running,
  another status, not in NeuroDB, names none), with their indicator reports (counted as Progress reports
  counts them), their first and last period end, each section name once (a row's list such as "Child
  Protection, Adolescent & Youth" gives each name; the five with the most indicator reports, then
  "(+N more)") and the first PD numbers NeuroDB does not hold; a row of another country is one the sync
  would skip (the same comparison). It also says how much of the running programme documents' reporting is
  held: those with no row, those whose first report held ends over 100 days after their start, those
  without their first report (QPR1 or HR1) (both of the PDs that started on or after the date the history
  is counted from: the partial-history rule covers the others), and what the latest partner reports sync
  did: "The latest partner reports sync (21 Oct 2026, Succeeded) asked for the reports from 1 Nov 2018 on:
  the Datamart announced 780,905, NeuroDB read 780,975 (770,993 of another country), kept 9,982 rows and
  removed none.", then, as they apply, that a round count is a limit of the Datamart's answer (and that the
  window filters are ignored), how many windows it read, why (a round count, or the cut sign of the read
  before) and whether each was whole (and that the Datamart's own count was cut), the pages that came back
  shorter than the first, a count that changed during the read, the business area filter it read with or
  why none ("It read with no business area filter: …"), records that came twice (pages that shifted, or
  records held twice in a stable order: *Pages that shift during a read*), and, when the Datamart holds no report from the date
  asked, "The Datamart holds no partner report starting before 1 Jan 2026 (NeuroDB asked from 1 Nov 2018):
  history is counted from 1 Jan 2026, and the 21 running programme documents that started before it have a
  partial history (Reached is PRP's cumulative)." (information), or, before the first sync that records
  where the feed starts, that the earliest report held starts more than a year after the date asked while
  running programme documents started before it (a warning). Its figures add the date the history is
  counted from and why ("History counted from": the earliest report the Datamart holds, the date the sync
  read from, the first start held whole, the earliest report held after a cut, or the date the next sync
  reads from), the run's status, what was announced, read, kept, skipped as another country's, returned
  twice, removed and held before, the windows read and not whole, the pages shorter than the first, the
  first start held whole, the business area filter and the earliest period start held. Two tables: *The latest partner reports read, window by window* (from, to,
  announced, read, pages, outside the window, whole), and *Running programme documents with no partner
  report row, by what explains it*, the first reason that applies (signed and starting later; first report
  not due yet, one month after the start with a high-frequency indicator, else three; a document type PRP
  does not report; no active PD indicator; end passed with the status still running; started before the
  earliest period start held; and, left, those whose reports should be in the feed), so the rows add up to
  the PDs with no row, each with how many PRP's programme documents hold ("unknown" while that dataset holds
  none, see check 8). Below it, one folded list per reason ("Left: their reports should be in the feed
  (20)") names every one of those PDs (the only place the checks name running PDs): its PD reference number,
  document type, start, end, first five sections, whether PRP's programme documents hold it (yes, no, or
  "unknown (check 8)") and its active PD indicators, by reference number, 500 per reason at most (the counts
  stay complete, and the summary says when the list stops). Of a PD it reads only these fields, never a focal
  point or a partner's staff.
  It is a warning when the answer was cut, nothing was removed, a window was not whole, a page was cut, the
  earliest start held is more than a year late (and the Datamart is not known to hold nothing earlier), or a
  PD with no row is left unexplained; running PDs with no row that every reason explains stay information.
* **Check 11** sets, month by month for this year and the running programme documents, the indicator
  reports per status of the partner reports (`prp/datareport`, what Progress reports and Partner
  monitoring count) beside those of PRP's indicator reports (`prp/indicator-report-v2`, kept whole for
  Ask NeuroDB and the Funds page), both read with the same status rule (for PRP's records, their progress
  report's own status and dates first, as the Funds page reads them, and the year and month by that same
  period end, also for a record with no period end of its own): it names the months where PRP lists more
  reports still owed (the partner reports feed does not carry them, and Power BI's Reporting Progress can
  then show overdue reports NeuroDB does not), those where the partner reports list more, and those where
  the statuses differ otherwise; with nothing to compare, it says why (no record held, records of other
  years or with no period end, none linked to a programme document NeuroDB holds, or none of a running
  one, else what the last sync did). It warns when the partner reports feed carries no report still owed
  for an ended period (every report still to submit is of a period not yet ended), and counts the
  progress reports missing between the report numbers held (a QPR2 between QPR1 and QPR3) whose period
  ended this year: an estimate (its month from the report before it, its indicator reports as many as the
  report next to it), in the column *Expected, not in the feed*; a report owed after the last number held
  cannot be seen this way. A third feed, PRP's own progress reports (`prp_progress_reports`,
  `sources/prp/unicefprogressreport`), is set beside the partner reports in a table of its own, in
  progress reports (never indicator reports): per month and status by the same rule over its status,
  end, due and submission dates, for the running programme documents it links to. It names the months
  where PRP's progress reports list more reports still owed than the partner reports (the partner reports
  feed does not carry them; these may be the overdue reports Power BI counts, which nothing has confirmed
  yet); with none to compare, it says why (never synced,
  running, failed, none returned, none linked to a programme document NeuroDB holds, with the link's
  counts, or none of the year). It also says how many of PRP's programme documents are held for the business
  area and how many are linked to an eTools programme document NeuroDB holds, and warns when records of
  another business area name one of NeuroDB's PDs by its reference number (the business area key may be
  wrong: probe the dataset); then how PRP's progress reports were linked to them: the key pair proven
  ("PRP's progress reports are linked to their programme document by programme_document = prp_id: N of the
  M reports naming one"), with the links that end outside their document's dates, or why no pair was
  proven and the keys the documents carry, what to tell the eTools Datamart team, or the keys a programme
  document that came twice in one read left out (the next read's copy counts); this check counts each progress
  report once, as the latest read wrote it (*… older versions of a report a later read wrote again (not
  counted)*) (figures *PRP programme document records held*, *… linked to an eTools programme document NeuroDB
  holds*, *… records of another business area naming one of NeuroDB's by reference number*, *PRP progress
  reports linked by*, *… of the reports naming one*); and, when their last read had records twice, what that
  read found.
* **Check 12**, *Indicator grouping*, counts for the eTools master indicators (counts only, never a title
  or a partner's or master indicator's name): the PD indicators the nightly `suggest_master_indicators` job
  reads (those of the programme documents running this year or last, per section), with and without an
  eTools id (without one, an indicator is grouped by its title alone) and with a CP output (from its
  reports, or its PD's only one); the keys of the PD indicator records, marking any that may name one
  indicator across programme documents (a cluster indicator's id, a blueprint); the share of percentage and
  ratio report rows with a value that carry their numerator and denominator, read as Reached reads a
  location's value (a row is a percentage or a ratio by its methods, or by the type of the PD indicator it
  is placed under as check 7 places it; a master's percentage leaves the others out); and the job's **dry
  run**, "would propose M master indicators from N PD indicators; K would be left on their own", worked
  out by the job's own code and written nowhere (the job then records the same counts). Once a master is
  active it also counts the PD indicators in a master, suggested (each once, a replacement suggested in two
  masters too), ambiguous and in none, the masters of this year without a target, and the confirmed links
  no longer in eTools or of another type now (a warning while any is above 0). Read it before accepting the
  first proposals: it says how far titles and CP outputs carry them, and how many percentages would be left
  out for lack of their parts.

Checks 10 and 11 run first, so they answer whatever time the others take; the page lists the checks in
their order. Every query is aggregate; the rows a check parses in Python are read 2,000 at a time and stop
at the checks' time limit like a query.

The checks run as a job, `reporting_checks` (`neurodb/datamart/checks.py`, `run_job`): every night at
02:15 (the schedule `reporting-checks`, after the 20:30 eTools Datamart sync; due at once after the
deployment that adds it, so the page has a result soon after; when the previous container's scheduler
sees it first, the new release's scheduler starts it as soon as it takes over), with **Run the checks
now** on the page, or *Run a job → eTools reporting checks*. The job gives each query 10 minutes and
the checks together an hour, and stores the result in its run's details (*Import and sync runs*, job
*eTools reporting checks*); the page shows the latest stored result at once, with when it was checked.
A result an earlier release stored is never shown as the current checks' result: the page says so above the
cards, names the checks changed since, and shows a check added since as *Not run yet*, until a run of the new
release replaces it (*Run the checks now*, or the schedule). The release that adds check 12 (core
`0031_etools_masters_job`) makes the schedule due a quarter of an hour after its migration rather than at the
next 02:15: not at once, since the previous container's scheduler, still running during the deployment, would
run its own earlier checks.
A check that could not finish (a query over its 10 minutes, or the data could not be read) or that the
hour left out says so, and the run ends *Succeeded with errors*, which raises no alarm (neither the
daily review's sync failures nor the admin home's *Needs attention*: the page says it); a run that
failed is named on the page, which keeps the last result, and counts as any failed job. One run at a
time (an advisory lock, checked again before each check: a run whose database connection was lost stops
and stores nothing, since another may have started): the button says when one is already going, and the
page says the checks are running (or starting, for the few seconds a run's process needs after any
start: the button, Run a job or the schedule) instead of offering it. Only while no run has stored a
result yet, and none is running or starting, does the page run the checks itself, within the web
request: each query stopped after 20 seconds and the checks together after 75 seconds (below the web
server's 120-second limit, so the page always answers), a check left out by that limit saying so; the
comparison of Reached with PRP's cumulative (check 5) runs only with 30 seconds left. The checks read
what the eTools Datamart sync stored (Run a job → *Sync eTools (Datamart)* refreshes it).
Print the page or save it as PDF from the browser: every table prints whole, wrapping between words (on screen
the wide ones scroll sideways), on A4 landscape across the whole sheet (the print dialog reads the page at its
portrait width, where the admin's container would hold it to 640 px), without the sidebar, the header or the
run button, and check 10's folded lists print open; a reference number or a count breaks only when it cannot
fit at all, so check 10's PD reference numbers print whole (`admin.css`, `.nd-checks`), and check 10's dates
are written with non-breaking spaces (`checks._cell_day`), so a date never prints as "1 Mar" over "2025" (a
result stored before keeps its dates as they were until the checks next run).

**The Power BI views.** The eTools Power BI report's pages live on NeuroDB's own pages, with the same
figures as everywhere else (they read the rule above; nothing is added across indicators):

* **Programme document statuses** have one definition (`neurodb/partnerships/statuses.py`): *Active*
  is the eTools status active alone (the Programmes page's *Active*, the partner pages' active count,
  the *Active PDs* tile); *Running* is active, signed or suspended (the default of the PD status
  filter on Partner monitoring, and what the overview, the daily review and the watch read); *Ended*
  is ended, closed, terminated or expired. Clearing the PD status filter shows every status.
* **Partner monitoring** has four views on one filter: *Grid* (by section, PD and output, with the
  months), **Program Performance** (one row per indicator: Section, CP output, Emergency, Indicator,
  Target, Reached, % Reached, Partner, PD; sorted by % Reached, highest first; click a heading to
  sort; section tiles with their indicator counts filter the table, the tile "—" lists the indicators
  that name no section), **Data quality** and *Map*.
  Filters: PD status, Section, Partner, Partner type (eTools partner type or CSO type), Programme
  document, Document type, Office, Emergency (the high-frequency indicators, reported monthly),
  CP output (words in it), Indicator (title or output), locations, the title tags, report type,
  Reporting year and status. Program Performance and Data quality open on *All years* (Reached to
  date), the grid on this year; the view links and the map carry the year shown, so changing view
  never changes the year, and the year menu offers *All years* on these pages. With a year chosen,
  Program Performance reads that year as Power BI does (*Reached in* the year, *% Reached in* the year,
  sorted by it), with Reached to date, its % and its status beside them; Data quality counts *with
  Reached in* the year. An indicator's CP output comes from its partner reports; one not reported yet
  shows its PD's CP outputs, joined (as Power BI shows them). *Excel of the filter* / *CSV of the
  filter* (Program Performance) export every indicator of the filter, not only the page shown; the
  grid and Data quality have no file of their own. The grid and Program Performance list at most 2,000 indicators (Program
  Performance the first 2,000 in its sort, with a note); every count (the tiles, the section tiles,
  Data quality, Ask NeuroDB) and the exports read every indicator of the filter, so they agree. The
  view links keep the filter last applied, also when it was changed without reloading the page.
* **Data quality** tiles: programme documents (running, active, ended), indicators, with and without
  Reached ("with Reached": a counted report carries a value; with a year chosen, Reached in that
  year), with and without a target (a target of zero is missing). Lists: indicators without a
  target; without Reached or with zero Reached; and NeuroDB's own checks: Reached differs from PRP's
  cumulative, the same title twice in one PD (kept apart, never merged: the rows are placed by their PD
  output, and where the output is shared too by the tie-breakers the data proves; the list counts the
  indicators that get none of their rows while rows of their title were left out, and the rows left out for
  an output that matches none),
  partial history. Each list shows its first 500 rows; the counts are complete.
* **Progress reports** has four tabs on one filter (search, report status, report type, reporting
  year, section, report #, partner, partner type, PD status, emergency indicator, period start and
  period end between). The Section list offers each section name once: a report row whose section is a
  list ("Child Protection, Adolescent & Youth") gives each of its names, and a name chosen keeps the rows
  that list it (`reporting.section_names`, `section_q`); a section chosen in the address that the list does
  not hold (Home writes the eTools section names of the PD indicators) stays chosen. `pd=<number>` (Home's
  programme document rows) keeps one programme document exactly: the reports whose reference is that
  number, or that are linked to the programme document of that number (the sync links a report by its
  eTools id first, so its reference can differ, or be blank); the page says "Programme document … only ·
  Show every programme document", and its tabs and exports keep it. The PD status filter opens on the running programme documents (active, signed,
  suspended), as Partner monitoring does; with every status chosen, the reports of programme
  documents NeuroDB does not hold are listed too. A report row that names no programme document (no
  link, no PD reference number, no eTools PD id: PRP's reports outside programme documents) is never
  read, whatever the filter, nor by the partner page, the home page or the management brief. Under the tiles, a note says how many indicator reports of the same
  filters (whatever their report status) the PD status filter leaves out, of programme documents with
  another status and of programme documents NeuroDB does not hold, with a link that chooses every PD
  status, and how many name no programme document (`reporting.left_out`, one query over the rows left
  out). Draft and cancelled programme documents are never read:
  * *Reporting progress*: tiles of **indicator reports** (one indicator in one progress report, as
    Power BI counts them) per status, with the number of progress reports under each; indicator
    reports by section and status with a total row (a report counts under its rows' section, else its
    PD's sections, never twice); indicator reports by month of the reporting period's end and
    status, Not yet due included (the month a report is about, as Power BI places it); and the report table (PD, PD status, partner, section, report, status, document type,
    due date, period start and end, submission, UNICEF focal points, indicators); its CSV and Excel files
    write the focal points as a count (`unicef_focal_points_count`), never a name.
  * *Partners* (Partner Performance): partners with a progress report in the filter (whatever its
    status), those with at least one Submitted or Accepted report, those without (listed), the
    partners with a report in each status, and the reports to follow up: Overdue, Sent back, Due and
    Submitted, in that order and by due date. A partner whose PDs have no report in PRP at all is
    not counted.
  * *By location* (Report/Location): one row per indicator, report and location with the value in
    the period (the disaggregation total, read as Reached reads it: a value repeated on every
    location is the report total and is not shown as the location's; two rows of one location are
    never added, the row count is noted) and PRP's figure to date at the location. Reports that do
    not count are greyed. A row keeps its progress report's status (the one the tiles count), also
    under a status filter. It reads at most 20,000 report rows at once, only the "()" total of each (a web worker holds them all
    in memory); above that it asks for a narrower filter, and its export refuses.
  * *By disaggregation* (Report/Disaggregation): see below. It reads at most 10,000 report rows at
    once (each can give many lines), and makes the lines of the 25 locations a page shows only; the
    export writes them location by location.
  The exports hold every row of the tab's filter; the Excel file adds the indicator reports and
  progress reports per status and the section table (and, on *Partners*, partners per status and
  those not reported). The status filter uses the six statuses (an old link with PRP's word, such as
  `status=Accepted`, still works). In every CSV file (these and the others NeuroDB writes), a text
  that a spreadsheet would run as a formula (one starting with `=`, `+`, `-` or `@`, such as a title
  `=HYPERLINK(...)` typed in eTools or PRP) starts with an apostrophe, so it stays text; a number
  written as text ("-12.5") is left as it is. The Excel files keep every text a text already.
* **UNICEF focal points** are shown by name only (`PCA.unicef_focal_points`); an entry that is or
  holds an e-mail address is left out. Partner focal points are never shown.
* **Programmes → Indicators** (PD/SPD Indicators): one row per indicator of the filtered programme
  documents (eTools lists an indicator once per location and disaggregation) with the PD's number,
  status, partner, document type, title, start, end and office, the indicator's own section, CP
  output, PD output, baseline, target and emergency flag; filters add PD reference, indicator title
  and Emergency, and the Section list also offers the sections the indicators name (the tab filters
  on each indicator's own section); exported whole.
* **Progress reports → By disaggregation** (PD/SPD Report/Disaggregation): per indicator report and
  location, PRP's value for each disaggregation value (female, Male; Lebanese, Syrian…) with its label,
  a **subtotal per disaggregation** (and per cross-tab) and the **location total** `"()"`, never one sum
  of every value: values add up only within one disaggregation (female 5 + Male 15 + Lebanese 20 is a
  location total of 20, not 40; `neurodb/datamart/disaggregation.py`). Percentages and ratios are
  subtotalled as numerators over denominators, and not at all without them. A subtotal that differs
  from the location total is marked. The location is read as Reached reads it (two rows of one
  location are never added: the row whose value is used gives the disaggregation; a report total
  repeated on every location gives no location total); when PRP gives no `"()"`, the location's value
  in the period stands in, said so. A value whose label is not read yet is not listed and is counted
  under the total. Extra filters: indicator, location, P-code and disaggregation (words in the
  disaggregation or its value). The export has one line per value, subtotal and total, each in its own
  column (`value_in_period`, `subtotal_of_the_disaggregation`, `location_total`), with the row's last
  change in the Datamart. The indicator page adds **Reached by disaggregation**: per location, each
  value combined over the counted reports as Reached is, with a subtotal per disaggregation only for an
  indicator summed across locations and periods.
* **Disaggregation labels.** The partner reports key their values by PRP ids (`"(12,)"`, a cross-tab
  cell `"(12, 31)"`). PRP's label tables (`sources/prp/indicatordisaggregationvalue` and
  `indicatordisaggregation`) have no country filter, so the `partner_reports` sync reads only the ids the
  reports name, one request per id (`/<id>/`), into `datamart.DisaggregationLabel` (labels only; admin →
  *eTools Datamart → Disaggregation labels*, read-only): ids not held yet, or read more than 30 days ago,
  at most 2,000 requests and 10 minutes per run (the rest the next night,
  `details.disaggregation_labels.left`). A value may name its disaggregation by id or as a nested
  record (whose name is then kept too). *eTools reporting checks* (check 3) says how many of the ids
  the reports name have their label read. An id
  PRP does not have is marked *not found*. When the labels cannot be read at all (ten failures in a row,
  for example the account cannot read PRP's tables), the step stops, says why in
  `details.disaggregation_labels.error` and the run ends *Succeeded with errors*; the reports are kept.
* **Funds → Partner contributions** (Partner Fund Monitoring) and **Funds → By PD and grant** (Funded
  Programs) share the Funds page's filter (search, donor, grant, FR number, FR start year, PD status,
  partner, partner type; the FR filters choose the programme documents their lines fund; no PD status
  chosen means every status but draft and cancelled; `neurodb/datamart/funds.py`). *Partner
  contributions*: one row per programme document with partner and partner type, currency, planned UNICEF
  contribution (`total_unicef_budget`), total budget, partner contribution (`cso_contribution`), its share
  of the total budget, and the **financial contribution during reporting periods**: PRP's
  `financial_contribution_to_date` (`prp_indicator_reports`), repeated on every indicator of a progress
  report, read once per progress report from the latest one that counts (Accepted or Submitted, by the
  six-status rule) and carries it; when its indicators disagree the largest is shown, marked *varies*.
  *By PD and grant*: one row per programme document and grant with the funds reserved on that grant
  (the sum of its FR lines' `overall_amount`, USD), donor, start, end, days to end (end − today, −1 once
  ended) and a status colour (active green, signed grey, ended and the like amber, in review red). The
  budget, the disbursement (the PD's FR headers' `actual_amt`, USD, for the whole PD) and the partner
  contribution are the programme document's: shown once (cells spanning its grants; in the export on
  its first row only), so no total counts them twice. Amounts in different currencies are never added:
  the tiles and the currency table give totals per currency. Both tabs export the whole filter.
* **Privacy.** A UNICEF focal point eTools knows only by an e-mail address is no longer stored under it
  (`person_names`; the eTools REST sync keeps the names the Datamart sync wrote when its list gives
  user ids only, and the report table never shows an id as a name), a progress report's
  `submitted_by` is kept only when it is a name (in its column and in the record kept beside it), and
  the typed eTools tables keep each record without the keys naming an e-mail address, a phone or a
  mobile, as the records kept whole always were (`catalogue.scrub`). Ask NeuroDB also withholds any
  e-mail address left in a text of a record it reads ("[email withheld]"). The job *Finish the eTools
  update* removes what earlier syncs had stored (it starts by itself after the deployment; see eTools
  Datamart). The progress report
  pop-up no longer names the partner's staff member who submitted: only UNICEF focal points are named,
  on the report table.

**Partner monitoring and Progress reports load in parts** (see *Pages that load in parts*). Partner
monitoring answers its frame at once: the view tabs, the filter bar with its choices and the "your section"
note. Each view then fetches its two blocks: the grid its key figures with their two charts (`summary`) and
the list of indicators (`grid`); Program Performance its section tiles (`sections`) and its table
(`table`, with its pages and the *Excel of the filter* / *CSV of the filter* links); Data quality its tiles
(`quality-tiles`) and its lists (`quality-lists`). Every block reads one computation of the filter's
indicators (with each one's Reached, % Reached and status), made once for every user and every web worker
and kept two minutes; Program Performance's section tiles and table read the one of the filter without its
sections (the table keeps the chosen ones, as it always did: `monitoring.program_performance_rows`, which the
Power BI comparison sheet reads too). Applying a filter, clicking a heading to sort
or a section tile reloads the results area under the filter bar with new grey blocks, which then fill in
(at once when the figures are kept already); a page of the grid or of the table reloads only its own block. On
*Progress reports*, the *By location* and *By disaggregation* tabs fetch their table as one block
(`table`), with its pages, its note when the filter holds too many rows and its download links; a change
of filter there answers the table at once, as before. *Reporting progress* and *Partners* are drawn whole,
as before.

What is kept, and when it changes: each kept value's key holds the latest run of each job that writes the
eTools tables (eTools sync, eTools Datamart sync, Datamart backfill, Locations sync: the run's state and its
end, so a run that starts or ends changes it, whatever it wrote) and the indicator flags, never the person
asking or the language (the values hold no translated text). A sync therefore shows at once; an
ActivityInfo import or a NeuroDB Watch run does not make these pages compute again. Nobody edits these
tables by hand (every eTools and Datamart table is read-only in the admin), except the gazetteer, which
the map's key also holds (below). The filter bar's choices are kept ten minutes under the same key, the
indicators two minutes per filter and day, the Progress reports tables two minutes per tab, filter, tab
filters and day. The blocks of one partner monitoring view carry `as_of` like the overview's, so they show
one set of figures when a Datamart sync run ends between them (one ends about every 20 seconds while the
sync runs). When the figures are kept already, the frame draws the blocks itself: a view opened again
within two minutes is one request. The exports are answered whole, as before.

The **Map** tab is not split in parts: its page already draws at once and asks for its points in the
background (`/api/internal/partner-monitoring/map/`). Those points are now computed once for every user and
worker and kept two minutes, under the eTools jobs' runs, the flags and the gazetteer (how many locations
and location types, and the last change of each), so a location renamed or moved in Admin → Locations
shows on the map at once. Its filter bar reads the choices partner monitoring keeps. `pdmap.js` also
frees its map if the page ever swaps it out, as `edumap.js` does; nothing on the page does so today (the
view tabs and the map's filter bar load whole pages). **Funds**: the *Partner contributions* and *By PD and grant* tabs no longer compute the
whole reservations summary just to fill the filter bar (6 queries instead of 13); the page is otherwise
drawn whole, as before.

### eTools master indicators (Partner monitoring → *Master indicators*)

An **eTools master indicator** is a section's indicator over the partners' own PD indicators, as an
ActivityInfo master indicator was over its partners' indicators: one figure a year, worked out from its
**sub-indicators** (one PD indicator of one programme document each) by its method. It is read on the fifth
tab of Partner monitoring, *Master indicators* (`/partner-monitoring/?view=masters`), and set up in the
admin (*Reporting setup* → *Master indicators (eTools)* and *Master indicator links (eTools)*). No figure is
stored: each page works it out from the partner monitoring rows (`neurodb/datamart/masters.py`). No AI is
used anywhere in it: the proposals and suggestions come from structure keys and word scores.

**What is kept** (`datamart.EtoolsMaster`, `EtoolsMasterYear`, `EtoolsMasterLink`; both migrations are
quick):

* a **master**: its eTools section (as the PD indicators name it), name, unit (a label only), CP output,
  keywords that help the suggestions, method (sum, percentage, ratio, average, maximum or minimum), *parts of
  one PD add up*, status (proposed, active, dismissed, archived) and where it came from (by hand, proposed by
  NeuroDB, or from an ActivityInfo master indicator). One active or archived name per section;
* its **years**: each year's AWP/RWP code and the **target staff type** for that year, and whether it is
  shown that year. A master has no year of its own, so nothing is copied at the yearly rollover and the links
  of a programme document running several years carry on by themselves;
* its **links**: one PD indicator each, named by its programme document and its identity there (`s` and its
  eTools id, else `t` and a hash of its title; never the page's key, which changes when the sync rewrites
  the rows), with what it **counts as** (in the total, a numerator, a denominator, or shown only), its status
  (suggested, confirmed, rejected), and its title, PD number and type when it was confirmed. A PD indicator
  counts in one total at most. *Updated by* and *confirmed by* are shown in the admin only: never on a page,
  in an export or to the AI.

**How masters come to exist**, every step a click:

1. **Proposed every night** (the job below): PD indicators in no master that share a section, a CP output, a
   type (number, percentage or ratio) and a title once its counting words, gender, nationality, disability
   and age range are set aside, across two programme documents or more, become one *proposed* master (a sum
   of numbers, a percentage or a ratio, by the type the pages read: a number its reports combine as a ratio
   is a ratio, and a PD indicator with no report yet, whose method is not known, does not decide it), named
   after the shortest such title. An indicator whose CP output is not known joins the one family of its title
   in its section, when there is exactly one. *Accept* (on the tab or in the admin) makes it active with its
   PD indicators confirmed in one click; on the tab it confirms the PD indicators the proposal's row listed,
   and one the night added since stays a suggestion of the master, said so. One of another type (a new
   programme document's, not reported yet, in a ratio) stays a suggestion, said why, and the job suggests it
   there again once its reports name the master's type.
   *Dismiss* keeps it as dismissed, so the same family is never proposed again. *Merge the selected master
   indicators* with an active master accepts the proposal there: its PD indicators are confirmed in the
   master kept, each read again as *Accept* does, and one that cannot count there stays a suggestion, said
   why. A proposal named as a master of its section is not accepted: rename it, or merge it into that
   master (an archived one is made active again first, with its *Status*: the master kept is an active
   one). A proposal keeps what people decided (its name, a year's code or target, a link rejected there, a
   role given to a suggestion) when a report names its family's CP output: it is the same proposal under
   its new key.
2. **Made from ActivityInfo**: *Reporting setup* → *Master indicators* (ActivityInfo), action *Create eTools
   master indicators from these*: name and unit copied, the method mapped (Sum, Sum over sum as a percentage,
   Average, Maximum, Minimum; Count is skipped and said), the AWP code put on the current year with the
   target left blank, in the eTools section of the database's confirmed *eTools section names* (the one with
   the most PD indicators when there are several; refused when there is none). It writes the new tables only,
   never an ActivityInfo row; a master that exists already by section and name is skipped.
3. **By hand** in the admin, with the **Add PD indicators** wizard on a master's page: tick the section's PD
   indicators of this year that are in no confirmed total yet and of a type the method takes; one role for
   all, and they are confirmed at once. When no PD indicator of this year names the master's section (a
   section typed by hand differently from eTools, or one eTools renamed since), the wizard says so: correct
   the section, or use *Change section (after an eTools rename)*.

**How PD indicators are attached.** The job suggests each PD indicator for one master of its own section,
by the first rule that finds one: (a) it **replaces** a confirmed link of the same programme document that
is no longer in eTools and reads the same (1.0; a suggestion only, a link is never moved; it is suggested in
every master that had the old one, with the role it had there, a total in one and a denominator in another,
say, and stays suggested in the others once it is confirmed in one's total); (b)
the **same structure** (section, CP output, type and title) as one of the master's confirmed indicators
(0.95); (c) a **similar title** (0.6 to 0.89, 0.1 more for the same CP output, never another age group).
When two masters fit within 0.05 of each other it gets no suggestion and the tab says "Ambiguous: A or B" (a
replacement only when two masters would count it in their total: it is still suggested in another role,
and its note says both, "Suggested for C (1.00); Ambiguous: A or B").
**Only confirmed links count**: a suggestion is listed under its master, never added. Confirming is one click
for a master (on the tab *Confirm all N suggestions*, in the admin *Confirm all suggestions*) or for a section
(on the tab *Confirm all N strong matches (0.9 or more)*, in the admin *Confirm suggestions scoring 0.9 or
more*). The tab's buttons confirm exactly the suggestions it shows, as their count says: never another
partner's when a partner filter is set, nor those of a master the search hides. A proposal's PD indicators are
confirmed by accepting it, and nothing is confirmed in an archived master. *Reject* keeps the indicator from
being suggested for that master again; attaching it to that master later (*Attach to…*, or *Add PD
indicators*, which says "Rejected for this master before") confirms it there. A role a person gives a
suggestion (*Shown only*, say) is kept by the nightly job until it is confirmed. Several
indicators of one programme document in one master start with sensible roles: a total and its girls and
boys (or other) breakdowns, the breakdowns *shown only*; girls and boys alone, both in the total; otherwise
the one with the largest target in the total.

After each eTools sync nothing is rewritten. A confirmed link stops counting, and says why, when its
programme document runs in the year but the indicator is gone (*No longer in eTools*: it cannot be confirmed
again, and its replacement is suggested the next night; it can still be made *Shown only* on its page or in
its master's list, checked by the type it was linked with), or when its type changed in eTools (*Type changed
in eTools*, until someone clicks *Confirm again*; a number whose reports now combine it as a ratio too, which
the job and check 12 count); an indicator now in another section is still read and says
*Now in section X*. A new PD
indicator, an amendment's included, is suggested the next night.

**How a master's figure is worked out** (the same on the tab, its detail, its export):

* **Each sub brings its own Reached in the year**, read exactly as Program Performance reads it (its own
  location and period methods, each value once, quarterly reports first and monthly ones only for an
  indicator with no quarterly report). A master never adds report rows again.
* **A sum** adds up its subs' Reached in the year. Partners may reach the same people, so it reads **up to**
  the sum, **at least** the largest programme document's figure. Two subs of one programme document are added
  only when they cannot count the same people (one names girls and the other boys, or each one nationality and
  they differ, or one Lebanese and the other non-Lebanese, read from every gender and nationality word of
  their titles: "girls and boys" next to "boys", "Syrian and Lebanese" or "Lebanese and non-Lebanese" next to
  "Syrian", and "non-Lebanese" next to "Palestinian" may count the same children, and so may refugees no
  nationality names; "Palestine refugees", PRS and PRL are Palestinian) or the master says *parts of one PD
  add up*; otherwise only that programme document's largest one counts ("may count the same people; not
  added"). A value too large to be true (above 20 times its own target, or above the country's children
  for a children indicator) is set aside and said so.
* **A percentage or a ratio** is the numerators added up over the denominators added up (× 100 for a
  percentage only); numerator and denominator links of one programme document are paired. Each programme
  document counts once: one counted through its numerator and denominator adds none of its totals ("Same
  programme document: counted through its numerator and denominator"), and of its several totals only the
  one with the largest denominator counts, unless they cannot count the same people or the master says
  *parts of one PD add up* (the sum's rule). **A sub without a numerator and a denominator for the year is
  left out of the figure** and the master says "counted 4 of 6 PDs"; it is never averaged in. Its row says
  why: "Reported without numerator and denominator" when PRP sent the % alone, or "Its PD averages its
  reporting periods" (or "takes its largest reporting period") when its period method is an average or a
  maximum, or "Its PD averages its locations" (or "takes its largest location") when its location method
  is, which keeps no numerator and denominator for the year however the partner reported (its
  *Numerator / denominator* cell reads "not combined", not "not reported").
* **An average, a maximum or a minimum** is taken over the subs' figures, all of one type.
* **Shown only** subs are listed and never counted. Masters are never added together, nor to the children
  headline, the overview, the country programme or the forecasts.
* **Q1, to Q2, to Q3, Year** are Reached from 1 January to the end of each quarter, a report counting in the
  quarter its period ends in, by the same rule.
* **Status**: against the **year's target staff typed** alone, as of the latest counted report (its period
  end, at most 31 December and today, so a quarter's lag does not read off track). A sum of subs that add up
  over the year expects the year's share of the target; a percentage, a ratio or a level the whole target.
  Without a typed target it reads **No target** (*Needs attention* then lists it, below). The programme
  documents' own targets are shown in the detail for reference only, never used for the status.

**On the tab**: it shows one year (this year by default; there is no *All years*) and opens on the user's own
section. The section, year, report type and location filters apply (a sub whose indicator the filter does not
read, not planned at the chosen places or, with *humanitarian reports*, neither high-frequency nor a cluster
indicator, is not listed: it is never called *No longer in eTools*); the status, the search and the CP output
choose master rows only (a master's figure never changes with them); a partner filter shows that partner's
contribution, with its share of a sum only. The tiles count the masters by status, the PD indicators in a
master, the suggestions waiting and the proposals; under them come the masters by section and CP output
(each opens on its subs, with *Details* for the detail), the **Proposed master indicators** (Accept,
Dismiss) and **Not in a master**: every PD indicator of the year with no confirmed place in a master listed
in the year, with NeuroDB's word on it (its suggestion, its proposal, "Ambiguous", why it counts nowhere, or
why it cannot join one) and *Attach to…*. A master whose year is switched off (*shown this year* unticked)
counts nothing that year: it is not listed, its PD indicators of programme documents running in the year are
under *Not in a master* ("In the total of X, which is not shown in 2026: it counts nowhere in 2026…"), the
*PD indicators in a master* tile and reporting check 12 count them out, and *Attach to…* does not offer it.
The figures of an average, a maximum or a minimum of percentages read with "%", as a percentage master's do. The buttons that change anything show to an Administrator and to a Section editor of the master's
section only; a viewer, an editor of another section and a donor are refused. *Excel of the filter* and *CSV
of the filter* give one row per master and one per sub (`level`), never who changed or confirmed what. The
tab loads in parts (`masters-summary`, `masters-table`) from the stored partner monitoring rows: one read of
them per request, plus one by programme document for linked indicators now in another section.

**The job**: `suggest_master_indicators` (`neurodb/datamart/master_links.py`), every night at 02:40 (the
`etools-masters` schedule, after the 20:30 eTools Datamart sync and the 02:15 reporting checks; due at once
after the deployment that adds it), *Suggest now* on the masters' admin list, or *Run a job → eTools master
indicators*. It reads the PD indicators of the programme documents running this year or last, writes
suggestions and proposals in one transaction (confirmed and rejected links and dismissed masters are never
touched, and nothing is written back to a master a person dismissed, merged, archived or deleted while the
run worked; a run again changes nothing more), one run at a time, and records a run with counts only for each
section (proposed, in proposals, suggested, replacements, ambiguous, no longer in eTools, type changed,
unattached).

**Before switching it on, and to follow it**: reporting check 12, *Indicator grouping* (with the other eTools
reporting checks, above), counts what the job would do without writing anything; *Needs attention* on the
admin home lists the masters of this year without a target (`etools_masters_without_target`, linked to the
masters' admin list filtered on *Listed this year, no target*: exactly the masters it counts).

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
target, the location's own Reached (to date, with PRP's running total there, and in the chosen
year), the indicator's Reached over all its locations, status and latest counted period, plus the
TPM activities, field-monitoring findings and open action points recorded at the place. The indicator, programme and partner pages link back to the map for their locations.

### Partner reporting: ActivityInfo and eTools, side by side

Partners reported in ActivityInfo in past years and now report progress in eTools/PRP. Both stay:
the ActivityInfo databases keep their dashboards, analytical views, maps and Neuro/HPM reports
(the ActivityInfo group at the bottom of the full menu: *Monthly results (ActivityInfo)* while ActivityInfo
is in use, *ActivityInfo archive* once it is not), the eTools reporting has its own pages (sidebar
*Partner reporting (PRP)*: *Progress reports*, *Partner monitoring*; in the full menu under *Partner
progress (eTools)*), and the **partner page** brings the two together
under *Partner reporting*: the eTools implementation monitoring on one side, the ActivityInfo
history per year and database on the other (each database opens the partner's master indicators by
month; *map* shows its sites).

**The partner page loads in parts** (see *Pages that load in parts*). It answers its frame at once:
the header, the *deleted in eTools* notice and the knowledge base documents. One part, `datamart`
(`/partners/<id>/?_part=datamart`: the page has no filter), then brings everything from the key figures
to the last list in one request: the tiles, the profile, the visits chart and assurance chips,
*Partner reporting* with the eTools and ActivityInfo panels (*Edit the link* for those allowed to
change partner links), and the eight lists. These blocks stand between one another on the page and
read the same figures (the partner's eTools Datamart records, its programme documents and its
ActivityInfo records), so they come together. Inside it, the Monitoring insights panel is a part of its
own, `fmm`, asked for once `datamart` is in: only when Monitoring insights is on and a field monitoring
visit ever monitored the partner, so a partner without one never shows a grey box that then
disappears. Nothing of this page is stored in the shared page figures: it is computed at each visit,
so a sync or an edit shows at the next one. Links to its panels (`#activityinfo-reporting`, the lists'
`#partner-…` ids) wait for the part and then scroll to the panel.

The ActivityInfo group's *Databases*, *Neuro reports* and *HPM* blocks list the year the page shows:
the year chosen in the year menu (`?year=`), the year of the database or report that is open, or else the
current reporting year. When that year has none of them (a new year before its databases are
set up), each block shows the latest year that has some, and says which year in its title. Above them,
one row per ActivityInfo year (a year with records) opens that year's databases (`/databases/?year=<name>`).
Search covers every year unless a year is chosen (`?year=`), each ActivityInfo result with its year
(prefixed "ActivityInfo archive:" once ActivityInfo is an archive); each kind of result shows its first 8
matches and a *See all* link (`?group=`) when there are more. A Neuro Report or HPM page has an *Other years* menu listing the
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
partner name is ignored. A link changed by hand shows at once on the partner page and on the management
brief (its *How sure* and *Partners* blocks read the links, which are in the key of its stored figures).

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
assistant sees every field. The FM questions, options and programme activities, the offices and the
sections also feed Monitoring insights, which reads their keys first (see Monitoring insights).

The PRP views have no `country_name`: `prp_indicator_reports`, `prp_programme_documents` and
`prp_progress_reports` are filtered by the country office's business area code. A code is digits (after
normalising: spaces, `2490.0`, `{"code": "2490"}` all read `2490`); a value that is not one (a country's
name, as "Lebanon") is set aside and never sent (`details.business_area_set_aside`, with where it came
from). The code is the first of: Admin → *Data and sync* → *eTools Datamart: PRP business area code* (one
row, digits only, set by an administrator: it comes first, also over a code the hosting team set); the
hosting team's `ETOOLS_DATAMART_BUSINESS_AREA` when it is a code; the workspace of the country NeuroDB
holds (the previous sync of `workspaces`); the Datamart's `workspaces` list (asked by the sync only, never
by a page; a failed request goes on to the next step); the code more than half of the partner report rows
held carry (`data.business_area`). A workspace step gives a code only when every workspace of the
country's name that has one names the same code. `details.business_area_from` says which: `admin`,
`environment`, `workspace_held`, `workspaces` or `partner_reports` (the last three found by NeuroDB:
`details.business_area_found`; for the partner rows `details.business_area_rows`, rows carrying it and rows
held). With no code anywhere the run fails, naming what was set aside and where to enter the code. A
found code whose read keeps nothing removes nothing and ends the run *Succeeded with errors*, so Data
health points to the admin's code. The admin page says which code the next sync takes and why, lists the
*Codes NeuroDB finds* (in the workspace held, with its sync date, and on the partner report rows, with how
many carry it), offers **Use 2490** to enter the code found (derived again on the server, never taken
from the request; shown only with the change permission, and a form of its own, so Enter in the code field
saves the code typed), and shows what the latest runs of the three datasets
used and found. A record naming another business area, a code but another country, or no business area
but another country, is dropped (`details.other_country_skipped`); a record naming its business area by
name ("Lebanon") is compared as a country's name (case and spaces aside); one that
names neither is kept, as with `country_name`, and counted (`details.country_unnamed`): Data health and
the admin page of the code then warn that nothing confirms those records are the country's and point to
Run a job → *Probe a Datamart dataset*, and when no record of the read named a code at all the run ends
*Succeeded with errors*. A PRP progress report naming neither is kept only when the PRP programme
document it names is held (those are read by business area, `details.kept_by_programme_document`); the
others are dropped as another country's (`details.unnamed_skipped` counts them apart). PRP's progress reports
are requested with no business area filter unless one is proven that night (*A business area filter is proven
before it is used*, above): the Datamart ignores `business_area_code` there (21 Oct 2026: 150,322 records with
it, 150,320 without), so the request no longer sends it, and the records are kept by the code they name.
A PRP programme document naming no business area is no longer kept unconfirmed: it is kept only when its
reference number names a programme document NeuroDB holds (reference numbers carry the country's prefix;
`details.kept_by_reference`), and linked to that PD by its reference number alone, as its eTools id is not
proven the country's (eTools ids repeat across countries, each country's eTools having its own); the others
are dropped as another country's (`details.unnamed_skipped`), and their rows, with the progress reports kept
through them, leave at the next whole read. Each run of the programme documents also says whether the
records kept are the country's: `details.linked_to_etools` (kept documents linked to an eTools PD NeuroDB
holds, by external id or reference number) and `details.other_code_named_here` (records skipped as another
business area whose reference number names a PD NeuroDB holds: the business area key may be wrong; check 11
then warns and points to the probe). Each run of the
three records, counts only: `business_area` and `business_area_from` (above), `read` (kept and skipped),
`datamart_count`, `business_areas_seen` (the ten most frequent
codes, `(none)` for none), `keys_seen` (how many records carry each key the sync and its readers use:
a key the Datamart does not send reads 0), `id_keys` (the ten id-like keys the records read carry most, and
how many carry each: what a record identity or the progress reports' link can be made of) and `dated` (the
records kept by the year of their date, this year and the years beside it, `other` and `none`); a read that kept nothing sets `empty_response`, and
Data health then says "the Datamart returned nothing for business area N" (or that every record named
another business area) and points to the code, never to the country name; for a code NeuroDB found it
says so and points to the admin's code, which comes first; for a run asked with a value that is not a code
(made before the sync set such values aside) it says the value is a name, not a business area code, and
that the next sync sets it aside; a value set aside is named on a muted line. A record of PRP's indicator
reports without a `source_id` key takes its `id` as its source id (a record with a `source_id` key, even
empty, keeps its own): that the two ids match the partner reports' `indicator_report` is not confirmed,
so the partner reports' totals (`reached.progress_values`) use such a record only when it names the same
programme document and indicator, and check 8 counts those that name another programme document.

PRP's progress reports name no eTools programme document, only PRP's own: after each sync of
`prp_programme_documents` and of `prp_progress_reports` (the catalogue syncs them in that order), each
progress report is linked to the programme document (and its partner) of the PRP programme document it
names, that record being linked to the eTools PD by its external id or reference number. Which key of each
dataset holds that id is not documented, and neither may carry an `id` (21 Oct 2026: no progress report
carried `id` or `source_id`, and none of the 1,590 kept was linked, as the documents are held under a hash),
so the sync proves the pair on what NeuroDB holds (`datamart_sync.prove_pd_link`). The documents' keys tried
are the key each is held under (the lookup used until now, first), every id-like key of their records (a key
whose last word is `id` or `pk`, or whose value is `{"id": ..}`, never one that holds a person) and
`reference_number`. A value several documents carry is the one the latest read wrote (`datamart_sync.written_by`:
the read that last wrote a row, by its `synced_at` and the dataset's run starts): the others are copies an
earlier read wrote, kept while the reads were not whole, as a document that changed then got a row of its own
(one changed copy no longer stops the link for every document). A key whose value two documents one read
wrote carry is left out (`not_unique`: two documents share it, as a partner's id, or one changed during that
read came twice, its rows agreeing on every other key tried, `came_twice`). Another key can still prove the
pair, or fail as `few`, `not_clear` or `dates_disagree`; when no key pair left links a progress report at
all, `why` is `repeated` if a document that came twice left a key out, else `no_link`.
The progress reports' keys are the
document they name (`programme_document_id`, else `programme_document`, first) and every other key naming a
programme document. Pairs that link the same reports to the same documents are one, named by their first key
other than the lookup used until now. A link is dated when the report has an `end_date` and its document a
start and an end (PRP's `start_date` and `end_date`, else its eTools PD's), and agrees when the report ends
no earlier than 31 days before the start and no later than 365 days after the end; a pair fewer than 9 in 10
of whose dated links agree is out (eTools ids and PRP ids are small numbers in the same range: a chance match
links reports to documents of other dates). The pair linking the most reports is used when it links at least
twice as many as any pair that links a report it does not, or to another document, and at least half of the
reports naming a document under its key (9 in 10 when fewer than half of its links are dated); once proven,
every report it matches is linked, the few whose dates disagree too (an amendment can move a document's
dates). With none proven, the lookup used until now is kept: it links what it links. A progress report naming
no business area is kept through the same pair, proven on what is held before the read. The run's details
say how it went, key names and counts only, under `progress_reports_pd_link`: `linked`, `pd_not_named` (the
record names none under the pair's key), `pd_not_held` (a PRP programme document NeuroDB does not hold under
the pair's key), `pd_not_linked` (held, but linked to no eTools PD), `by` (`[report key, document key]`),
`proven`, `pairs` (the five best, with the reports each links, how many of those links are dated and how many
agree), `pd_keys` (the documents' candidate keys and the records carrying each, with `not_unique` for those
whose values repeat among the documents one read wrote, and `came_twice` for those of them a document that
came twice left out), `pd_superseded` (below) and `why` none was proven. Check 11 says which pair linked them
and how many of its links end outside their document's dates, or why none was proven with the keys a document
that came twice in one read left out (then it asks the eTools Datamart team nothing: the key holding the id
may be among them, and the next read's copy counts) or else the keys the documents carry, a key two documents
share among them (what to tell the eTools Datamart team), and reads the linked ones as its third feed. A
progress report the sync rewrites keeps the link it holds until that step runs, so a read that fails part-way
unlinks none. PRP's progress reports carry no id either, so a
report that changes (its status, a date) on a night its read is not whole gets a row of its own and its old
row is kept until a whole read: the row of a report (the programme document it names, its `report_type` and
`report_number`) that an earlier read wrote, when a later read wrote that report again, is an older version
(`datamart_sync.older_versions`). It is linked to none (`pd_superseded`), and check 11 counts it nowhere (its
figure *… older versions of a report a later read wrote again (not counted)*), so a report later accepted
is never also counted as overdue there. Ask NeuroDB is not changed by this: it has no look-up by PD or partner
for PRP's progress reports (their catalogue entry names no PD or partner key, so it lists their records and
filters on their own fields, where `programme_document` is PRP's id, not an eTools PD), and it lists every
row held, an older version among them, until a whole read removes it.

**A record identity for records without an id.** A dataset kept whole whose records carry no `id` was held
under a hash of each record, so a record that changed became a new row. Each read now looks for an id-like
key that every record read without an id carries, kept or skipped, each value once
(`datamart_sync.IdentityTracker`: the records told apart by their content only, so two records of different
content sharing a value drop that key for the night; at most 10 keys tracked; only a hash of each value
kept), and holds those records under `"<key>:<value>"` (`details.record_identity`, "" for none: last night's
first while it still qualifies, then `pk`, `source_id`, then the others by name). They are written once the
read has ended, and the row held under a record's hash, or under last night's identity, is renamed in place
(`DatamartDocument.record_key`, still 64 characters at most), so an unchanged record keeps its row and its
link, and a changed one no longer becomes a new row. The first night renames the hash rows of the records
that did not change; a changed one gets a new row, and its old one goes at the next whole read (or waits one
night). Read country-blind, a key unique only within Lebanon (eTools' `external_id`) never passes, as other
countries' records repeat it; under a proven business area filter no new identity is chosen (last night's is
kept while it still qualifies), so the weekly read with no filter never renames every row. The read counts
its records by last night's identity meanwhile. A read that fails part-way writes none of these records
(they keep last night's rows); records with an id are written as they come, as before.

To see what the Datamart holds when such a read keeps nothing, an administrator presses Run a job →
*Probe a Datamart dataset* (`probe_datamart --dataset <name>`, `prp_indicator_reports` by default). It
makes two or three requests of 50 records each: no filter, the business area code (the code as the sync
resolves it, above, without the request to the Datamart's workspaces; the PRP datasets only), and the
country name (for the partner reports, which take no business area filter, a third: the sync's own query,
from the date they are read from). For the partner reports and PRP's progress reports, with a code, it then
asks each business area filter key on one page of the sync's own query (`details.area_filters`) and judges
it as the sync does: the verdict says "The Datamart applies business_area to prp/datareport (9,990 records
with it, 780,905 without; every record on its page names 2490): the sync reads by it." or "The Datamart
applies none of the business area filters tried for sources/prp/unicefprogressreport: business_area_code is
ignored (as many records with it as without), business_area is refused (HTTP 400). The sync reads every
country's records and keeps 2490's." For a dataset the sync would read in windows tonight (a round count, or
a cut sign in its last run: `details.windows_why`), it then asks what a read in windows needs, up to about
ten more requests of one page each with the sync's own filter (`details.windows`), so such a probe can take
up to a minute: a far future and a far past start (and `__lte` when `__lt` is ignored), the reports starting
before and from this year, ids below 1 and from 10^15, the first page wholly past the count, and for the
partner reports the reports ending before the
earliest period end NeuroDB holds, once a request with an end before 1900 proves the end-date filter
(unproven, no number is given); a large read with no sign of a cut asks none ("It announces 780,905
records, not a round count, and the last sync of partner_reports saw no sign of a cut: NeuroDB reads it in
one answer, as the sync does (no window request asked)."). For each it keeps the count, the page length, the page number and query
keys of its `next` link and the records of the page per start year. The verdict then says, in plain words,
whether the Datamart applies the window filters and so how NeuroDB reads the dataset (date windows, id
windows, or not more than one answer gives: tell the eTools Datamart team), whether it returns at most N
records to one request (a round count, with no page past it: the Datamart refuses every page past the last
one, so a refused page says nothing of a limit when the count is not round), whether the count is cut
(records on the page past it; a page number it ignores is said as such, never as more records), and how
many reports it holds that end before any NeuroDB holds. For a dataset whose last sync had records twice,
or asked a stable order, it also asks the three ordering requests the next sync asks (*Pages that shift
during a read*; `details.ordering_requests`: per key its verdict, the page lengths and the records shared)
and says "The Datamart takes ordering=id for prp/datareport (two pages in order, the reverse on another): the
sync asks for it after a read whose records came twice.", or that it ignores or refuses it. A page past the
count that is refused is an answer (a paging limit), not a refusal. It writes nothing but
its run (job *eTools Datamart probe*, target the dataset): per request the filter sent, the count
announced, the records on the page, how many carry each key, and the business area codes and country
names named (counts). Its `verdict` says in plain words what the answers mean and what to do (enter
another code, tell the Datamart team the data is not loaded or that it ignores the filter; a 403 on every
request: "The account NeuroDB uses for the eTools Datamart is not allowed to read <path>: ask the eTools
Datamart team to grant it." when the body says the account was signed in or the latest run of another dataset
read, the sign-in refused when the body names it, else both, and what to do for each; a 401: the sign-in
refused, ask the hosting team to renew the credentials); for a
PRP dataset it judges the business area answer first: refused (with its status),
ignored (its page names other codes, or it finds as many as no filter while that page names other codes),
or kept to the code. A value set aside is said first ("The hosting team's ETOOLS_DATAMART_BUSINESS_AREA is
“Lebanon”, a name, not a business area code: the probe and the sync set it aside and use 2490, found in
the workspace NeuroDB holds."), and a code that finds nothing is compared with the workspace NeuroDB holds
when that names another. The verdict shows in the run's *Error* cell of Import and sync runs and as *What the
probe found* on the run's page, above its details. A probe
is never an alarm and never stale; one runs at a time. Keys naming
an e-mail address, a phone or a mobile are removed from the records kept whole here, from the records
of the typed eTools tables, and from every assistant answer, which also withholds any e-mail address
left in a text; the field monitoring records reach the assistant without people and without long texts
(see *Data leaves Azure* under Ask NeuroDB).

Not read, and why:

| Endpoint | Reason |
|---|---|
| `datamart/users`, `datamart/partners/contacts`, `sources/prp/unicefperson`, the PRP focal point and officer tables | personal data |
| `rapidpro/*` | RapidPro messaging; contacts are personal data; no country filter |
| `datamart/reports/outcomes`, `outputs`, `activities`; the other `sources/prp/*` tables | global tables with no country filter (of `sources/prp/indicatordisaggregation` and `indicatordisaggregationvalue`, only the labels of the ids the partner reports name are read, by id) |
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
Datamart (`sync_locations`). It keeps a raw copy of every record it reads for Raw eTools data
(`details.raw_copies`; *Checking the figures*), and it writes the programme documents (`PCA`), whose status
decides "running" on every partner reporting page: running it can move the PRP pages' figures. Its partner
details, programme document details and action points ask eTools about the records NeuroDB already holds (its
partners, its programme documents with donors, its final engagements), not about the list it has just read: when
that list fails they still read them. Only when NeuroDB holds none to ask about and the list failed are they not
read (their runs fail: "Not read: the partners could not be read.", `sync.NEEDS`), rather than succeed having
received nothing. An "Invalid token" (HTTP 403) from it means `ETOOLS_TOKEN` was revoked or
expired: ask eTools for a new token and store it in Key Vault (never in a file). Its intervention details step writes the donor amounts in the same shape as the
Datamart sync (one entry per donor and grant), so running it does not blank the donor pages.

## Home and the country overview

`/` for a signed-in user is **Home** (`reports/home_views.py`): how partners are reporting first, in numbers
and tables (Progress reports' and Partner monitoring's own figures: management asked for a Home that PD
managers read at a glance), then, under *More on Home*, what needs the person today, what changed since the
previous working day, and where their section (or the country) stands this year. Every figure
of the year is on the **Country overview** (`/country-overview/`), the findings of the morning check on
the **Daily review** page (`/daily-review/`). Visitors get the landing page at `/`; donor accounts never
reach these pages. Old links keep working: `/?year=…` and `/?governorate=…` are sent on to
`/country-overview/` with the same query, `/?review=…` (and `review_latest`, `review_id`) to
`/daily-review/`, and a part request of the old overview at `/` (`_part=kpis`, `figures`, `news`, `review`,
`review-more`) is still answered, so an open tab keeps loading.

**First load, one rule for the three pages** (`reports/home.default_sections`): members of the Management
group and people without a section see every section; everyone else sees their own section (the user's
section matched to the eTools section names, as on partner monitoring), with *every section* one click
away. Every link Home draws to the Country overview, the daily review and the brief carries Home's
sections (`?section=…`, or `?year=<this year>` for every section), so the next page never applies a first
load default of its own and shows the same figures. So does every link to Progress reports (`?section=…`;
none for every section, the page's own default) and to Partner monitoring (`?section=…&year=<this
year>&scope=year`).

### Home (`/`)

| Block | What it shows | Who |
|---|---|---|
| *Partner reporting: <who>* → *Progress reports* | Progress reports' seven tiles for Home's sections, exactly as on that page (`datamart.services.reporting_figures`, which is the page's own `partner_reporting`: the running programme documents and every reporting period, the page's default, so a January keeps last year's last reports, the overdue ones; on the page's day, `reporting_today`). Each tile counts indicator reports, with the progress reports under it, and opens the page on its status. Then **Programme documents to follow up** (`reporting.pd_follow_up`): the first ten (`home.HOME_PDS`; with more, the hint reads "The first 10 of <n> programme documents, the most overdue first"), one row per programme document with its partner, its progress reports Overdue, Sent back, Due and Submitted (awaiting acceptance), the oldest due date of its Due or Overdue reports (a past date as soon as one is overdue) and a link to its indicators on Program Performance (every section, `section=`: Progress reports counts a programme document under every section its reports or the PD name, while Partner monitoring keeps the indicators of the sections chosen, and a PD of two sections may hold its indicators in one); the most overdue first. A row's reference opens Progress reports on that programme document (`?pd=<its number>`), whose figures are the row's. "Every report to follow up, on Progress reports →" opens the Partners tab. Then the Partners tab's three figures in one line (partners with progress reports here, reported, not yet). Sections with no progress report at all read "No progress reports here." and no partners line; "Nothing to follow up" is for reports that are all Accepted or not yet due | everyone |
| *Partner reporting: <who>* → *Indicator progress in <year>* | Partner monitoring's tiles for Home's sections and this year (every programme document running in it): indicators, on track, off track, not reported, programme documents, partners. They are the grid's own `summary` at the same address (`/partner-monitoring/?section=…&year=…&scope=year`), from the same stored indicators (the `pm-rows` key), so the two never differ; the grid's *Overdue reports* tile is left out (Home's Overdue is Progress reports', which counts it another way). On track, Off track and Not reported open the grid on that status; "Program Performance table →" opens `view=table` with the same filter | everyone |
| *More on Home* (folded) | Everything below, behind one click, with the old purpose line and its links to the Country overview and the management brief. Open for superusers and for a role whose menu shows *For you* or *Daily review* (Users and access → *Menu*); folded, its blocks are not loaded until it opens | everyone |
| *Needs you today*: For you | The NeuroDB Watch card (not drawn while Watch is switched off: the block says "NeuroDB is not checking at the moment: an administrator switched these checks off."). An account without a role is told "For you is for people with a role…", one without a section that For you shows only whole-country points. When the morning check is late (26 hours), the card shows a warning box ("NeuroDB has not checked since Monday. Ask an administrator to look at Scheduled jobs."; before the first check, an administrator is told to open For you and press *Check now*, with a link) and its title is just "For you" | everyone |
| *Needs a decision* | The first decisions of the latest daily review in the sections (`brief.decisions`, as the brief's *Things to decide*): severity, title, section, status and due date; never the owner or who should act. "All decisions: Management brief →" | Administrators and Management members |
| *Daily review · <day, time>* | Counts by severity (Critical, Warning, To note), the open findings that matter most, each with its severity as a worded pill, grouped by check ("and 4 more like it"; those already under *Needs a decision* left out), up to two of the review's "changed" sentences ("Since the review before: 240 new findings, 30 still open."), "Open the daily review →". A review more than two days old starts with its date; before the first review, when it runs (read from the *daily-review* schedule) or that it is switched off | everyone |
| *Since <weekday>* | Up to six notable What's new changes since 00:00 of the previous working day (Monday to Friday, no holiday calendar: on a Monday, since Friday 00:00), in Home's sections; the sections are matched to NeuroDB's sections by the first-load rule, and when none matches the heading says "every section" instead of listing the country under a section's name | everyone |
| *Your tasks* | Counts only: document review findings not reviewed yet (while the document review is on; a Section editor's own section, the country for Administrators), open Makani wellbeing flags (Administrators and Education editors: `WELLBEING_SECTIONS`), and while ActivityInfo is in use the HPM reports of the year whose masters sit in the editor's section. Left out when every count is 0 | Section editors and Administrators |
| *Where <who> stands in <year>* | The Country overview's own five key figure tiles (the same build, key and template, so Home and the overview never differ), then "Also in <year>: field monitoring visits · third-party visits done of those planned · open action points (high priority overdue)", and for editors the programme documents still counted by their largest indicator. "All the figures: Country overview →". With a section chosen, the visits are Home's own count, those to partners with a programme document in the sections ("… field monitoring visits to partners with a programme document in <sections>", `_partners_in_scope`), and are not a link, since Monitoring insights counts a section's visits another way; nor is the third-party pair, since the Field monitoring page shows the whole country; the action points link carries the sections (`points_query`), so that page lists the same open and overdue points. With every section, the visits link to Monitoring insights (to Field monitoring while `FMM_ENABLED` is off) and the third-party pair to Field monitoring. No third-party visit planned: that part is left out (never "0 of 0"). Before the eTools Datamart sync has ever succeeded on the server (this year's figures all 0 and no programme document at all), one line instead of the tiles: "No eTools data yet: the eTools Datamart sync has not run on this server." | everyone |
| *Programmes and sources* (whole country) | The country programme cycle (year k of n, indicators on track; or ended, or not started), Compiler's Makani, Dirasa and youth registrations, and ActivityInfo (in use: its year, results and last import, "records imported" while no result is tracked yet, or "in use, waiting for its first records" while the reporting year ticked current has none, a ticked year with records having its own line; archive: "ActivityInfo archive 2019–2026: every database and report, kept as it was"). A source not set up is left out, except for people who can set it up (superusers, and Administrators with *Staff status*), who read a muted line saying where | everyone |
| Data freshness strip | When each source last changed, "Data health →" (ActivityInfo only while its import is followed) | everyone |
| *Raw data behind this page* | One line under *Partner reporting*'s heading: it opens Admin → *Raw eTools data* on the sources Home reads (*Checking the figures*); "Raw data: ask for staff status" without staff status. Drawn by the frame, read in the role's own groups query | validators |
| *Check these figures* | One line under the title of the Progress reports block and of the Indicator progress block: the block's figure trail, `/check/home/?block=reporting&<Home's sections>` or `/check/home/?block=indicators&<keep>` (*Checking the figures*). Drawn by the part, read in the role's own groups query (no query more) | validators |

**Loads in parts.** The frame (title, date and scope line, purpose, the partner reporting heading and its
links, the freshness strip) does no figure work and reads no Watch table; then each block is a part. The
blocks inside *More on Home* load when it opens (at once for the roles that see it open):

| Part | What it holds | When it loads | Query ceiling (tests) |
|---|---|---|---|
| `reporting` | Progress reports' tiles, the programme documents to follow up and the partners line. Computed at each request, as the page computes its main tab (0.06 s on the x20 parity world, about a tenth of a second on production's ten thousand report rows), and never stored: Home always shows the page's figure of the moment, also while an eTools sync writes | At once | 9 cold |
| `indicators` | Partner monitoring's tiles for this year: the grid's stored indicators of that filter (2 minutes, shared with the page). When they are stored, the frame draws them itself | At once | 17 cold |
| `for-you` | The Watch card, from Watch's own address (`watch:card`) | At once | 11 |
| `tasks` | *Needs a decision* and the daily review's digest | At once | 12 |
| `since` | *Since <weekday>* and *Your tasks* | When near the screen | 10 |
| `standing` | The five key figures and the *Also* line: the overview build of this year and Home's sections. When that build is stored (2 minutes, shared with the Country overview), the frame draws it itself | At once | 44 cold |
| `programmes` | *Programmes and sources* (the country programme build is shared 10 minutes with its page) | After the visible ones | 18 (no cycle) |

The frame stays within 19 queries cold and 13 warm (`tests/reports/test_home_parts.py`; the cold count
includes the menu's sources; the person's role is read once per request, the menu's choices are kept in
memory, and a focused menu has no ActivityInfo block to read). Two of them are the moment of the eTools
figures (their latest jobs and the indicator flags), the key under which the frame looks for Partner
monitoring's stored indicators. **Cost with nothing stored** (x20 parity world): `indicators` 0.4-0.5 s,
`standing` 0.6 s (the overview build, which computes the indicators of its own), `reporting` 0.06 s. A
focused person pays the first and the last only, since *standing* waits in the folded *More on Home*; a
superuser, whose fold is open, pays all three (two indicator computations, one for each block), about a
second of work spread over the parallel part requests, then nothing while they are stored. *Since*, *Your
tasks* and the decisions are computed at each request (small); nothing Home stores holds a person, a name or
anything about a child.
**Privacy**: Home never shows who owns a decision or a finding (no owner, no "who", no assignment link), no
e-mail address, and the wellbeing and document review counts only to the roles above. The partner reporting
blocks show partners and programme documents, never a UNICEF or partner focal point, and nothing of them
reaches Ask NeuroDB (its tools are unchanged).

### Country overview (`/country-overview/`)

Every figure of the year on one page for the whole intervention, read live from the synced eTools
tables and, in a year with ActivityInfo records (rule 1 of *ActivityInfo: in use or archive*), the
ActivityInfo history. Three rows answer three questions, then two progress blocks; the daily review has
its own page now, and the For you card is on Home. The ActivityInfo database cards (one per database:
indicators, status, records, partners, last import) have their own page, `/databases/?year=<name>`, in
the ActivityInfo group at the bottom of the full menu (*ActivityInfo databases*, under *Monthly results
(ActivityInfo)* or *ActivityInfo archive*), with the year's totals and the import runs; the overview links
them ("ActivityInfo databases of the year →") only in an ActivityInfo year:

| Row | Blocks | Source |
|---|---|---|
| Impact on children | Children reached (eTools PRP reports, and ActivityInfo in a year with its records, shown apart), children reached by governorate (tiles), achievement against target by section, monthly reach of both sources (the ActivityInfo line ending at its last month), coverage of the estimated children per governorate | PD indicators + PRP reports (`datamart_pdindicator`, `datamart_reportedindicator`), ActivityInfo facts of the HPM masters, population figures (`category=children`, governorate level) |
| Value for money | Funds disbursed of reserved, cost per child reached by section, spending vs delivery by section, funding by donor, partnerships that need a decision | Funds reservations (`datamart_fundsreservationheader`, lines for donors), the same indicators |
| Delivery and assurance | Indicator status by section, assurance counts (field monitoring, TPM visits of every status, with the planned ones (the figure the field monitoring page shows) beside them, action points, HACT risk), findings by rating, what needs attention (at most eight items; overdue high-priority action points and one never-reported item always among them, "+N more" for the rest) | Partner monitoring rule, `datamart_monitoringfinding`, `datamart_tpmvisit`, `datamart_actionpoint`, partner risk ratings |
| Progress | TPM visits planned, completed and with the report overdue per month; action points due, closed and past due per month, open ones by age | `datamart_tpmvisit` (+ activities for the section), `datamart_actionpoint` |

**Filters.** Year (calendar years: the Year select and the header's year menu list this year down to the
first year NeuroDB has data for, at most ten; `?year=` is four digits), section (multi) and governorate (a
click on a tile). A PD manager lands on their own section (the user's section in the admin, matched to the
eTools section names as on the partner monitoring page); "every section" is one click away; members of the
Management group and people without a section land on the whole country (the first-load rule above).

**Which programme documents.** Every PD running in the selected year (its period overlaps the year),
whatever its status now, except drafts and cancelled ones: a PD that ended in June still counts for
its months. Only *Decisions this quarter* is limited to the PDs still active.

**Which indicators count children.** A PD indicator counts when it is a plain number and its title
names children (the age-group tag: under 5, under 18, adolescents, children). An ActivityInfo master
indicator counts when it is an additive (SUM) master of the year's HPM report, in a database shown
on the dashboard, and its label names children. Sections correct the rule once in admin → Datamart →
*Indicator flags* (eTools indicator id or master indicator id; counts children / does not / the
title decides); the flag survives every sync and shows on the page at once. A flag on an
ActivityInfo master only acts on masters that meet the other conditions (additive, HPM report,
displayed database).

**One indicator per programme document (the headline rule, `neurodb/datamart/headline.py`).** The
indicators of one PD count overlapping children (the child enrolled in non-formal education also
receives the learning materials), so they are never added. Each PD counts once, with its **main
indicator**:

* its **unique beneficiaries indicator**: the indicator that counts each child or person once,
  chosen on the PD page (*Make it the unique beneficiaries indicator*, under the PD's indicators) by
  an Administrator or a Section editor of one of the PD's sections, or in admin → Datamart →
  *Indicator flags* (*Unique beneficiaries* with its programme document; one per PD). Only a plain
  number can be chosen: a percentage or a ratio never counts people;
* until one is chosen, its **largest children indicator**, labelled *largest single indicator*:
  the largest by Reached in the year (then by Reached to date): the year of the overview, the brief
  or the donor page, and the current year on the PD page and in the daily review, so for one year
  every page names the same indicator. A past year's overview may count another indicator of the
  same PD, its largest that year.

An indicator whose value cannot be true (above the country's child population, or more than 20 times
its own target and above 1,000) is never chosen nor added, on any of these pages, even when it is the
unique beneficiaries indicator: the overview and the brief list it so someone checks it in eTools.
The PD page then marks the chosen indicator *Unique beneficiaries, not counted* with the reason (its
value cannot be true, or it has become a percentage or a ratio) and offers *No longer the unique
beneficiaries indicator*; a chosen indicator that eTools no longer lists under the PD is named in a
line under the table, with *Remove that choice*. In both cases the largest single indicator stands in
until the choice is corrected.

A total over several PDs (a section, a partner, a donor, the country) is the **sum of each PD's main
indicator**, labelled so: partners may reach the same children. A PD counts in the section of its
main indicator only (with a section chosen, a PD whose main indicator is in another section adds
nothing), so the sections add up to the country. The overview's tile says how many PDs count by
their unique beneficiaries indicator and how many by their largest single indicator. Values come
from Partner monitoring's Reached (`neurodb/datamart/reached.py`): Accepted and Submitted reports,
the year's value combined by the indicator's method (a sum adds the reports, a max or the latest
does not). The tile (and the brief's) says how much of the eTools figure comes from reports still
awaiting acceptance: what a send-back would take off (for the whole country; not split by
governorate).

**Two sources, never added.** The same children are often reported in both eTools PRP and
ActivityInfo, so in a year with ActivityInfo records the headline *Children reached* is the larger of the
two, with both shown beside it and the months each covers ("eTools 1,266,027 (Jan–Sep) · ActivityInfo
4,469,997 (Jan–Aug): the larger of the two"); the governorate tiles and the coverage table take the larger
of the two per governorate, and the monthly ActivityInfo line stops at its last month. A year without
ActivityInfo records, or a section without ActivityInfo children, shows eTools alone: no "ActivityInfo 0",
no ActivityInfo column or line. From January to April of a year with no eTools children reported yet,
the tile says *Not yet* and links to last year.

**Achievement against target** (per section, and the brief's *Achievement of PD targets (median)*):
the children indicators with a target, how many have a Reached, their statuses, and the **median**
share of the target reached (each capped at 100 %) against the median share of their PD period
elapsed. Targets of different indicators are never added.

**Governorates.** A governorate's eTools figure adds, for each PD's main indicator, the values
reported at the locations inside it, for indicators whose locations add up (PRP "sum"); an indicator
reported as the maximum or the average across locations is counted nationally but not split by
governorate. With a governorate chosen, the monthly bars, the money and the cost per child follow
the same rule: a PD's funds count for its share of the children its main indicator reached there,
and PDs without a children result are left out.

**Cost per child** is what the PDs with a children result disbursed to date (all the years of their
funds reservations, supplies and operating costs included) over the children they reached in the
selected year (each PD's main indicator). A PD with a children result counts its money and its
children in the section of its main indicator; a PD without one splits its money by its indicators'
sections. Compare sections, not absolute values. It is empty when no children indicator reported.

**Loads in parts** (see *Pages that load in parts*, above). The page answers its frame at once: the
purpose line, the filter bar, the subtitle, the freshness strip at the bottom and, in an ActivityInfo
year, the link to the ActivityInfo databases. Then come, each in its own request:

| Part | What it holds | When it loads |
|---|---|---|
| `kpis` | The key figure tiles | At once; it computes the overview build that `figures` then reads |
| `figures` | Impact on children, value for money, delivery and assurance, and the two progress blocks, with their headings (their anchors still work) | Once `kpis` has finished, when near the screen |
| `news` | What's new and the forecast risk card (this year only, and only while ActivityInfo is in use) | After the visible parts |

A part further down loads anyway once the visible ones are done, one at a time. Every part request
carries the filter the frame resolved: the user's own section written out on a first load
(`section=<name>`), `section=` for every section, the year always (four digits) and the governorate when
chosen, and `as_of`: `figures` reads the build `kpis` read for it, even when a sync finished before or
between the two (see *The pages in parts*). There is no empty state for a missing reporting year any more:
a year without ActivityInfo shows its eTools figures. When the build for the chosen filter is already
stored, the frame draws the tiles and the figures itself, so an overview opened again within two minutes
asks only for What's new after its frame. The *For you* card and the daily review card moved to Home and
to the daily review page (below).

**Caching.** The overview build is kept 2 minutes per (year, ActivityInfo reporting year of that year,
sections, governorate, day) in the shared page figures (`overview:v15:…`; Home's key figures are this very
build), read by every web worker (it was each worker's own memory before), and computed by one
request at a time; a new or changed indicator flag (children or unique beneficiaries) and the end of
any sync run change the key, so every worker shows the new figures at once. The build reads the same
in every language, so one stored build serves every language. The filter bar's sections are kept 10
minutes and the freshness strip 2 minutes the same way, both under keys that change with the last
finished sync run and the flags; the filter bar's governorates, which staff can edit by hand in
Admin → Locations, are read at each visit, so an edit shows there at once (the figures, which name the
governorates too, show it when the build expires). Nothing in the build depends on who asks: the
user's own section only enters as the filter. The management brief reads the same stored builds: an
overview and a brief of the same year and sections (the overview without a governorate) compute the
year's figures once between them, and the brief's filter bar shows the same stored sections. The
freshness strip at the bottom shows when each source last changed (ActivityInfo data only while its
import is followed).

### Daily review page (`/daily-review/`)

The daily review's own page: the review card that was on the overview, with its tabs (the day before,
the last seven days), its *Show N more findings* line and its *Data problems* line. Purpose line:
"Fourteen checks run every morning at <time> on the night's data. Findings are prompts to look, never
verdicts on partners or staff." (the time is read from the *daily-review* schedule; switched off, it says
so). It opens on the first-load rule above, with Home's scope line (its sections, *every section* one click
away). Parts: `review` (the card,
`id="daily-review"`, so `/daily-review/#daily-review` leads to it) and
`review-more` (its folded findings, only when opened; always those of the card above them, as described
under *Daily AI review*). Before the first review it says "The daily review has not run yet. It runs every
morning at <time>." Ceilings: frame 18 queries cold and 13 warm, each part 8. Admin → Daily reviews →
*Run the daily review now* links to the page in its message.

## Management brief (`/brief/`)

The overview read for decisions: one page for senior management and the Country Management Team,
in the full menu under *Management brief* (Programme results › Management brief). It reuses the overview service (same programme documents,
same children rule, same cache) for the selected year **and for the year before**, and adds what a
management meeting asks for. Eight blocks, one tab each:

| Block | What it shows | Source |
|---|---|---|
| Brief | Five tiles with a comparison (children reached against the same months of last year, achievement against the elapsed period, disbursed of reserved, cost per child against last year's PDs, indicators on track against the review 30 days ago), *Things to decide* (up to five decisions chosen and written by the AI daily review from its findings, each with one reason, a suggested owner and when, and the findings it rests on (marked *AI-suggested*; the model's name stays on the stored review only); without the assistant, the review's five most important open findings), and *Sections at a glance*: one row per section, weakest first, with five status cells (pace: target reached against time elapsed; indicators on track; spending: disbursed against achieved; reporting; confidence), each with an icon, its value and, on hover, its figures (the rules are under the table). *Copy summary* copies the headline figures and decisions as plain text for minutes | The overview blocks of both years, the latest daily review, the finding assignments |
| Ahead or behind | Per section, the main indicator of each PD summed (Reached to date against those indicators' targets and the expected point), with the Country Programme target; the year's reach of both sources projected to December at the last three months' pace | PD indicators and PRP reports, ActivityInfo HPM masters, **section plans** |
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

**Rules of this page.** The eTools figures are read from Partner monitoring's Reached
(`neurodb/datamart/reached.py`): only Accepted and Submitted reports count, a report's month is the
month its period ends, and its value combines the locations by the indicator's method, never adding
what PRP repeats on each row; an indicator's achievement is its Reached to date against its target.
Children reached is the overview's headline rule (one main indicator per PD, summed over the PDs);
achievement of PD targets is the median over the children indicators, never a sum of targets.
Every comparison is against the same rule: the same months of the year before for children reached
(January to the same month, each PD's main indicator combined by its method, the larger of the two
sources), the elapsed PD period for achievement, the daily
review of 30 days ago for the on-track share (the same formula and the PDs running in the year, only
when that review is of the selected year). The projection is, for each source, the average of its last
three reported months, extended from its own last reported month to December (eTools' partner reports come
quarterly, its last month being the last one whose reports are all in; ActivityInfo's records about two
months late); it is a pace, not a forecast.

**Years and ActivityInfo.** The year is a calendar year (`?year=`, this year by default; the Year select
lists this year down to the first year with data, at most ten), and each year's ActivityInfo figures
come from the reporting year whose records count for it (rule 1 of *ActivityInfo: in use or archive*):
none in a year without ActivityInfo records, so there is no "no reporting year" page any more. Children
reached is compared like with like: the larger of the two sources against the larger of the two only when
both years have ActivityInfo figures covering the same months (ActivityInfo resumed in July is not
compared with a full ActivityInfo year). Its records arrive about two months late, so both years'
ActivityInfo is then summed to this year's last ActivityInfo month, and the hint says so ("ActivityInfo
compared over the same months of both years (Jan–Aug): its 2026 records end in Aug."). Otherwise eTools
against eTools, labelled "vs same period last year, eTools only", with a hint saying why ("ActivityInfo was
not used in 2025", "ActivityInfo's 2025 records count no children for this scope", or "did not cover the
same months"); "Compared on eTools only" appears only when such a change is shown. eTools is compared over
the same months of both years, from January to the last month whose partner reports are all in, never past
the current month. Partners report about two weeks after each quarter, over several weeks, so a quarter with
some of its reports in is never compared with last year's whole quarter (no "−30 %" from one report in ten):
a month's reports are all in when at least 90 % of the programme documents expected for it have sent theirs.
A programme document is expected for a month when it runs then (its start and end dates) and its reports
covered that month last year, or the same month of the quarter before (a quarterly report covers its
quarter's three months, a monthly one its month); one with a later report but none for that month holds the
comparison back (that report is late), one whose reports stopped is no longer waited for two months after
the quarter's end. When a change is shown over fewer months than the current one, the hint says so ("eTools
compared over Jan–Jun of both years: the partner reports from Jul are not all in yet.", or "…: the partner
reports for later months are not in yet." when none has come). An eTools-only comparison is shown only when
the two years' programme documents adding a value over those months are within half of each other;
otherwise there is no comparison and the hint says why: "Not compared: 2025 has eTools partner reports for
only <n> programme documents over Jan–Jun." (no "+900 %" against a pilot year). Nor is there one while not
even the first quarter's reports are all in ("Not compared: 2027's partner reports for Jan–Mar are still
coming in (<n> programme documents so far, <m> in 2026)."; no "−50 %" with half of them in), when either
year has no eTools partner reports ("Not compared: 2025 has no eTools partner reports.", or "2026 has no
eTools partner reports yet.", "yet" only while the year runs), or for a year that has not started (its
period reads "2027 (not started)"). Before
the first partner report of the year (January to April) the tile reads "Not yet", with no change, and the
brief as text says "Children reached: not yet." The line under the tiles says children reached is compared
"against the same months of 2025" only when its tile shows a change, and otherwise that it "is not
compared with 2025 (see its tile)". The chart *Where the year ends at this pace* draws each source as
reported to its own last month (eTools to the last month whose partner reports are all in, as on the tile),
then projected from there at the pace of its own reported months (ActivityInfo's from its first month, so a
year resumed in July is not averaged with empty months): a month a source has not reported is never held
at the total so far. Each line is solid to its own last reported month and dotted after it; that month
hovers as reported, the dotted months as "projected". Only the year in progress is projected, and
ActivityInfo only while it is in use: otherwise (a year that is over, an archive) a line ends at its last
reported month, never drawn flat to December. The shaded band marks the months to come, from the current
month, and a key under the chart says what the solid line, the dotted line and the band are. The
*partners linked across sources* criterion of *How sure* counts only in a year with ActivityInfo figures,
and the lineage lists the ActivityInfo import only in such a year.
Confidence thresholds: reported ≥ 80 %, verified ≥ 30 %, linked ≥ 90 %, sync at most 2 days old;
one short = medium, two or more = low. A grant's unspent balance is each funds reservation's
outstanding amount in the grant's share of the reservation's lines (eTools carries the
outstanding amount per reservation, not per grant). Sex, age, nationality and disability come from
the indicators' titles: a title naming both girls and boys names neither, so most children fall
under "not named" (the brief reads the titles, not PRP's disaggregations, which are on the Progress
reports page's *By disaggregation* tab); they are read from each PD's main indicator, as the children
reached are.

**Values that cannot be right are left out.** A children indicator whose reported value is above
the child population of Lebanon (the latest population figures), or more than 20 times its target
(and above 1,000), is not added to children reached or to achievement: its section's confidence
is Low, the *How sure* block lists it with a link, and the brief as text says how many were left
out. Check such an indicator in eTools with the partner. PRP values that eTools sends as
`{"v": .., "d": .., "c": ..}` are read as their calculated value (else numerator over
denominator); a value holding several numbers (`45/100`, a date) is read as no value, never as
the digits glued together. Section names are trimmed ("PSEA " is "PSEA").

**Filters and caching.** Year and section (every section by default). The filter bar is a plain form:
*Apply* opens the page again with the new filter, frame first. The result is kept 2 minutes per
(year, sections, day, language) in the shared page figures, read by every web worker and computed by
one request at a time, and the key changes with every children or unique beneficiaries flag, section
plan, assignment, ActivityInfo partner link (*How sure* and *Partners* read them), new review or finished
sync run, so an entry in the admin or a sync shows at once (a population figure changed by hand in the
admin shows within the 2 minutes). The build's labels are
translated as it is computed, so each language has its own; nothing in it depends on who asks: the
*Assign* links are drawn for the person asking (staff only), never stored. *Print* prints the page
without the navigation, once every part is in (below).

**Loads in parts** (see *Pages that load in parts*). The page answers its frame at once: the purpose
line, the filter bar with the *Figures as of* line (the last run of each source), *Print*, the tab bar
and the *Reference* block (lineage and data dictionary). The lineage is kept 2 minutes in the shared
page figures, the same for every language, under the last finished sync run, the flags and the latest
daily review. Then come, each in its own request with the resolved filter (`section=` written out, an
empty one for every section, and the year always) and `as_of` (`sections` reads the build `brief` read
for it, even when a sync, a review or an edit came before or between the two; see *The pages in parts*):

| Part | What it holds | When it loads |
|---|---|---|
| `brief` | The *Brief* block: the five tiles, *Things to decide* (with *Assign* for staff), *Sections at a glance*, and the brief as text behind *Copy summary* | At once; it computes the brief build that `sections` then reads |
| `sections` | *Ahead or behind*, *How sure*, *Who and where*, *Partners*, *Money* and *Action*, with every chart of the page | Once `brief` has finished, when near the screen |

The tabs are links to the blocks: a tab whose block has not arrived yet asks for its part first and
scrolls to the block once it is in, unless the reader has scrolled meanwhile (*Reference* is in the
frame). When the build for the chosen filter is already stored, the frame draws both parts itself, so a
brief opened again within two minutes is a single request. **Print** waits for every part still to come
and for the charts in them ("Preparing the page…" on the button), then prints; Ctrl+P on a page still
loading prints a note in place of each part not in yet. *Copy summary* works as soon as the `brief` part
is in.

## Daily AI review

Every morning the `daily-review` job (`manage daily_review`) runs fourteen fixed checks over the
synced data and stores the result as a dated review with its findings (admin → Daily review). Its own
page, **Daily review** (`/daily-review/`, third in the full menu; see *Daily review page*), shows the latest
one in its card, with tabs for the day before and the last seven days; the card follows the page's section
filter (country-wide findings always show). Home shows a digest of it (counts by severity, the findings
that matter most grouped by check, what changed) with a link to the page. The
programme findings are listed (the first twelve; the rest under *Show N more findings*, loaded only
when it is opened); the findings about the data pipeline (`sync_failures`, `data_quality`,
`stale_sources`, `locations_unplaced`) are summed up in one *Data problems* line that links to the
Data health page.

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
| `sync_failures` | Failed or partial sync runs in the last 24 hours (the ActivityInfo jobs only while the import is followed, see *ActivityInfo: in use or archive*; the eTools reporting checks only a run that failed, not one where a check could not finish; the diagnostic eTools Datamart datasets (`datamart_etl_status`, `etools_usage`) never, see *One failed dataset is not the whole sync*) | warning |
| `data_quality` | Datasets whose last run failed rows; ActivityInfo partner names not linked to eTools (only while ActivityInfo is in use) | info |
| `locations_unplaced` | PD indicators whose location is not in the gazetteer | info |
| `stale_sources` | Sources older than `SYNC_STALENESS_HOURS` (the ActivityInfo import only while it is followed, and not after the 22nd, when it does not run) | info |
| `improvements` | Indicators back on track since the previous review | good |

**New, still open, resolved.** Each finding has a stable key (for example the PD and the check);
the next review marks it *still open* when it appears again, and adds a *resolved* entry for every
key that disappeared. When a check fails, its findings of the day before are carried forward as
still open, never resolved. The two checks that describe a change (`new_off_track`, `improvements`)
are always *new*. Findings are ranked by severity, then by the children behind target. Children
behind target are counted on one indicator, never added across indicators (they count the same
children): the PD's main indicator (its unique beneficiaries indicator, else its largest children
indicator this year, as on the PD page and the overview) when it is among the finding's indicators,
else the children indicator furthest behind; the finding names it.

**The summary.** With the AI assistant configured (an Anthropic or OpenAI key), the model reads only the
day's findings and counts (titles, details and numbers; no staff names; `store=false` on OpenAI, and
Claude keeps no conversation either) and writes four to six plain sentences;
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

**What staff see.** *For you* (second in the full menu, under Home; in a role's menu once a superuser ticks
it in *Menu*), with the number of "Needs you"
points told today and not yet seen, and a *For you* card on Home (not drawn while Watch is switched off;
an account without a role is told "For you is for people with a role. Your account has none yet, so it
opens with the Viewer's pages only. Ask an administrator to give you a role."). The page shows, in order:
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
| `hact_assurance_gap` | From 1 October to 31 December: partners whose programmatic visits, spot checks or audits are below what HACT requires (eTools' partner minimum); warning from 15 November, critical from 15 December. When the partner's programme documents are short of their planned visits, the evidence also says how many PDs need how many visits (counts only) and links to them on the HACT visit plan (`/field-monitoring/?tab=plan&partner=<id>&year=<year>`) | the sections of the partner's active PDs, and the country view | trial |
| `assignment_due` | Agreed dates on daily review findings (*Finding assignments*): 3 days before, and once passed | the finding's section, and the country view | trial |
| `fm_follow_up` | Field monitoring visits (Monitoring insights) reported, rated off track or constrained (the worse of the overall rating and HACT Q1), ended 14 (Score settings *follow-up days*) to 120 days ago, with no eTools action point linked and no NeuroDB action point following them up (one added by hand, or one NeuroDB made and someone marked done); critical when off track and ended over 30 days ago. Closes when an action point is linked (or a NeuroDB one follows the visit up) or the rating changes. Reads the Datamart sync and the Monitoring insights refresh; finds nothing while `FMM_ENABLED` is off (switch the check off then, or its source shows as not refreshed) | the visit's sections | trial |
| `forecast_short` | Indicators the year-end forecast of this year says are likely to fall short, only while ActivityInfo is in use and the forecasts are shown; once ActivityInfo is an archive its points close ("ActivityInfo is archived: its forecasts are no longer followed") | the forecast's section | trial |
| `donor_account_expiring` | Donor sign-ins that stop working within 14 days | Administrators | trial |
| `reporting_year_rollover` | 15 December to 15 January, one of two points. While ActivityInfo is not set to Archived, is set In use or has its import switched on over last year's records, and the new year has no displayed database: "ActivityInfo in <year>: set it up, or set it to Archived" (links the admin page *ActivityInfo: in use or archive*; closes when the year's databases are added, ActivityInfo is set to Archived or its import is switched off; missed after 15 January). Otherwise, while no population figures of the new year are loaded: "Population figures for <year>" (links the Population page; closes when they are loaded; missed after 15 January). See *ActivityInfo: in use or archive* | Administrators | trial |
| `daily_review` | The critical and warning findings still open in the latest daily review, under their own keys | the finding's section | on |
| `system` | The admin home's *Needs attention* lines (failed jobs, a silent scheduler, a missing daily review, the watch's own lines below) | Administrators | on |
| `stale_source` | A data source a check needs that has not synced within `SYNC_STALENESS_HOURS` | Administrators | on |
| `openai_quota` | The AI service's credit ran out, Anthropic's or OpenAI's (see the circuit breaker below; the id is the one it always had) | Administrators | on |

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
  *Match again* (top of the list) matches again the names matched automatically and not confirmed yet,
  after a NeuroDB section was added, renamed or deleted (a name set by hand is never changed). Each name shows the *Not mine* its section's staff gave in the last 30 days, per check:
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

**The morning note.** One AI call per audience that has new or changed points that day: each
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

### What goes to the AI provider, and what never does

Sent (to the provider in use, api.anthropic.com or api.openai.com, over HTTPS; on OpenAI with
`store=false`; under the same API terms as Ask NeuroDB):

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
data. No person identifier is sent (OpenAI's `safety_identifier`, Claude's `metadata.user_id`): no
person asked, and one constant identifier would pool every background run.

### Costs, limits and the circuit breaker

Every AI call of every feature on the shared key is counted in one ledger: admin → Data and sync →
**AI use**, per day, feature (Ask NeuroDB, daily review, What's new, document summaries, periodic
figures, country programme reading, NeuroDB Watch, Monitoring insights) and model, in tokens, and in US dollars when the
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
$10 to $30 a month; check the price of the model in use on its provider's pricing page (AI use shows
Claude's cost itself). The cap bounds the worst case at 9 million tokens a month. The provider's own
spend limit or budget and usage alert (the Anthropic workspace's, or the OpenAI project's) stay the hard outer
limit (see *AI assistant* above).

**The circuit breaker.** When the AI provider says the credit ran out (OpenAI's `insufficient_quota`,
Anthropic's `billing_error`), the watch stops using AI for 6 hours, and the administrators get a
critical point and a *Needs attention* line ("The Anthropic credit ran out: Ask NeuroDB is affected
too", or the OpenAI credit). Three failed calls in a row pause it too. The notes are listed by code
meanwhile, and the first call that succeeds closes the point. The pause and the point name the
provider whose call failed, read from its error: a run that started before an administrator chose the
other provider still calls the first one. A pause for one provider's credit ends as soon as the other
provider is in use (AI settings), whose credit did not run out; it holds again if the first provider is
chosen back within the 6 hours. The same goes for the pause of Monitoring insights.

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
| `WATCH_MODEL` | empty: `AI_ASSISTANT_MODEL` | The model of the notes and look-ups. A model of the other AI provider is not used: the provider's own model runs instead (AI settings shows which). |
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
  row; add credit to the Anthropic workspace (or the OpenAI project), choose the other provider in AI
  settings (a pause for one provider's credit ends then), or wait;
- *N eTools section names have no confirmed NeuroDB section*: confirm them (above);
- *The check “...” went back to trial*: look at its points and reactions, then switch it on again,
  keep it in trial or switch it off.

Nothing is lost when a day is missed: what each person was told is compared with what they last
heard, so the next pass tells what is still worth telling. Points closed or gone more than 24 months
ago, what people were told more than 12 months ago, and answered requests are deleted by the morning
pass. What the watch remembers is its own memory, not the knowledge base: nothing it concludes is
saved there or in the hub (see `docs/ICEBOX.md`).

## Monitoring insights (`/fmm/`)

Monitoring insights turns the eTools field monitoring data into **visits**: each visit's partners,
programme documents, place and sections, how complete and coherent its report is (FMS's quality
rules, AI checks among them, and a score), how urgent its follow-up is, and what was done about it (FM action points). Its page,
`/fmm/` (menu: *Monitoring insights*, right after *Field monitoring*, marked "AI"), has five tabs:
**Insights** (the morning briefing, the key figures, an AI monitoring brief and Chat with Data),
**Quality**, **Analysis**,
**Visits** (the table, the visit look-up and each visit's page) and **Map**. A drill-down window lists
the visits behind every chart bar and count.

A refresh (`fmm_refresh`, after every eTools Datamart sync and each morning) builds and scores the
visits from the synced records; the page reads only what it built, so it never waits for eTools. The
admin views under admin → *Monitoring insights* are: **Fields found** (which keys the field monitoring
records hold, and the keys an administrator pins), **Questions found** (which checklist question is
Q1, Q2, Q3 and PSEA), **Quality rules**, **Score settings**, **Rule versions**, **Field office staff
lists** (rule R19), **AI check answers** (kept by what each record sent; the checks made per visit before
records were are carried over once), **Prompt versions** and
**Sampling checks** (the AI's prompts and what the model accepted), **AI briefs** (every brief written,
or why none was), **Chat questions** (every chat question, with what its check found), **Visits** (the
visits built, with their records and each record's score and rule results, for checking the data) and
**Visit reviews**; and for the
action points page **Action point settings**, **AI reviews of action points**, **Action point
verifications**, **AI content summaries** and **NeuroDB action points** (see *Action points*).

The AI is **off at deploy** (`FMM_AI=false`): the page then shows a brief written by NeuroDB from the
figures, and the chat says it is switched off. It is switched on at step 7 of the go-live checklist
below. The partner, programme document, overview, assurance and action points pages link into
Monitoring insights, and its visits are in the knowledge hub, What's new and NeuroDB Watch (*Links into
the rest of NeuroDB*, below).

### What each block shows

Every figure is computed from the visits and records the last refresh built (`neurodb/fmm/metrics.py`,
one function per block, each taking the page's filter), kept 10 minutes. A **visit** is one eTools
monitoring activity; it holds one **record** per entity assessed (partner, CP output or PD/SSFA:
`fmm.VisitEntity`), each with its own rating, narrative and Q1–Q3. As in FMS, each record is scored,
flagged and given an urgency on its own, so quality, ratings, flags, rules and urgency count
**records** (`Scope.records`); *Monitoring visits*, the status tiles, places, PSEA, the monitoring
gaps and the follow-up count **distinct visits** (`Scope.visits`: with a record filter, the visits
of the records counted). Each block says which it counts.

| Block | What it shows | Source |
|---|---|---|
| Key figures (every tab) | Monitoring visits (distinct visits, whatever their status, with the breakdown reported / in progress / planned / cancelled / status unknown; it opens the list grouped by visit), records (rated / not monitored / not rated yet), the average quality score per record (the mean of the scored records), and the records of high urgency (red, with the amber ones, and *View urgent records →*) | `kpis`: `fmm.Visit`, `fmm.VisitEntity` |
| Insights › Morning briefing | This year so far (1 January to today, whatever the page's period; its other filters kept), tinted as FMS shows them: critical flags (records at or above red urgency), average quality (per record), **Low quality records** (scored records below the Medium band; FMS's tile says "visits" for the same records), critical partners (the partners of a record at or above red), monitoring visits (distinct visits, "N records" beside them), and the visits in review, submitted, in data collection, assigned and completed (a status is the visit's); each tile with its definition (ⓘ) and the records behind it; *Top critical partners*: the five partners with the most critical records (each record under its own partner) as red chips, each opening those records; *Quality by governorate*: "NAME · record average · records", the chip tinted by quality band | `briefing` |
| Insights › AI monitoring insights | The parts the published prompt version lists (v2: a coverage and quality summary, key programmatic findings, operational challenges, recommendations, and up to five priority action points written "[PRIORITY: High] Section / Partner — action — responsible — timeframe"), each sentence with the visits it rests on; *Generation settings* (read-only: the published version's model, effort, output token limit, narrative samples, compliance depth, temperature or top-p only when set and used, the sections; *Edit in admin* for administrators); the chips (model, effort, tokens, temperature and top-p, notes and quality flags sent, prompt and rules versions, quota); *Regenerate*, *What was sent* | `fmm.ai.insights.current`: the latest `Insight` of the filter, else the brief written by NeuroDB (`fmm.ai.fallback`) |
| Insights › Critical records requiring attention | FMS's "Critical items requiring attention", one card per record: the scored records at or above red urgency, most urgent first, at most 10 (else the five most urgent amber ones), each "#<activity id> · <entity> (<type>)" with its rating, HIGH/MEDIUM and urgency, and one line per rule it failed (the stored flag, with the AI's explanation for an AI check; R19 as "N monitors not on the staff list of <office>", never who); the card opens its visit's page at the record; *Show all N* opens the records list on that urgency band | `critical_items`: `fmm.VisitEntity`, `fmm.RecordRuleResult` |
| Insights › Chat with Data | Questions about the visits of the filter, answered with four look-ups, links to the visits checked; the published prompt version's starter questions | `fmm:chat_stream`, `fmm.ai.chat`, the `fm_*` look-ups |
| Quality › Quality score trends | Two smooth lines per month (by visit date: start, else end): the average quality score of the scored records (left axis, 0–100, filled) and the reports, the records of reported visits (right axis); a point opens the month's records | `monthly_quality` |
| Quality › Monitoring volume over time | Records per month (by visit date: start, else end) as bars, as FMS counts them, the distinct visits in the tooltip, with their average quality as a line on its own axis (0–100%) | `monthly_volume` |
| Quality › HACT Q1 — Finding rating distribution | Records per month by their own HACT Q1 (the record's answer, else its partner's, else the visit's), stacked On track / Constrained / Off track in FMS's colours; the records' overall finding rating instead ("Overall finding rating distribution"), with a note, when no record of the filter has a Q1 answer; the drill-down box's pills On track, Off track, Constrained and Not Monitored (apart: the records not monitored on a reported visit, or whose Q1 reads "not monitored") open their records | `hact_q1_by_month`, `rating_by_month` |
| Quality › Geographic coverage | The places visited, their governorate, records, visits and last visit (top 10; *Show all* loads the rest, up to 500) | `locations` |
| Quality › Top recurring issues | The records' flags grouped by rule and reason, with their records, their visits and mean urgency, and the visits of the most urgent records | `top_issues`: `fmm.RecordRuleResult` |
| Quality › Quality issues summary | Records flagged by narrative and rating coherence (R6, an AI check), Not monitored (planned, not conducted: reported visits none of whose records is rated, a count of visits), records with 3 or more flags | `issues_summary` |
| Analysis › Quality score distribution | Scored records in FMS's five bars of 20 points (0–20 red, 20–40 orange, 40–60 amber, 60–80 light green, 80–100 green, 100 included) along a "Record count" axis, and the records not scored | `score_buckets` (summed from the pass's buckets of 10) |
| Analysis › Rule score trends over time | Per rule with points, a smooth line of the share of its maximum points the records of the visits that started each month earned ("% of max score", legend on top, FMS's colours for the first five); a point opens the records the rule flagged that month (worked out when the panel scrolls into view) | `rule_trends` (with `rule_stats`, from `rule_months`: `fmm.RecordRuleResult`) |
| Analysis › Quality rule analysis | One row per rule that checked records, in the order of the ids: "N / M records flagged" and a bar of the share not flagged (green under 25% flagged, amber 25–50%, red over 50%); R19 reads the whole visit and counts on each of its records, as FMS counts it; under it, every rule with what it checks and "not available", "AI check pending", "AI check switched off", "off" or "flag only" | `rule_analysis` |
| Analysis › Quality flag frequency | Every rule that flagged a record, most first: id, bar and "N (share of the records it checked)"; the same counts as the rule analysis | `flag_frequency` |
| Analysis › Flag count distribution | Scored records with 0 flags (green), 1 flag (blue), 2 flags (amber), 3+ flags (red), the share inside the bar | `flag_distribution` |
| Analysis › Highlights | Visits, records, reported visits, governorates covered of the gazetteer's, average quality per record (the key figure's), off-track records, PSEA-flagged visits of those with a PSEA question, the scored records by High / Medium / Low band, records by entity type | `highlights` |
| Analysis › Governorates not visited | The gazetteer's governorates no visit of the filter is placed in | `governorate_gaps` |
| Analysis › Field offices | Records, visits and average quality per field office ("Office not known" too; a record counts in each office of its visit), and where the offices came from (per visit) | `offices` |
| Analysis › Entity performance | All the entities assessed (FMS's default *All* chip) or one kind (Partner, CP output, PD/SSFA), each with its type badge (CSO partner, CP output, PD/SSFA…), its records (usually its visits), the average quality of its **own** records coloured by band (a PD no longer takes its visit's worst score), High / Med / Low, top issue (from its records' flags) and last rating, worst average quality first, unscored last; partners by their full name; a PD shows its planned visits for the year (the table loads when it scrolls into view) | `entities_performance` (`"all"` merges the kinds) |
| Analysis › Quality by field office | One row per office, its scored records and records, its flags by rule over its scored records ("R1: 347/782", amber under half, red from half) | `office_rule_badges` |
| Analysis › Section performance | Per section, its records and visits, average quality and High / Medium / Low, a bar coloured by band, and its first 10 records, one line each ("#id ENTITY score rating", lowest score first, unscored after; a line opens the visit at the record), *Show all* for the rest | `sections`, `record_names` |
| Analysis › Visit frequency by location | Visits, records, average quality and coverage (rated records ÷ records) per place (top 10; *Show all* loads the rest) | `locations` |
| Analysis › Quality by finding rating | Records (each under its own rating), average quality and bands per overall rating; Not monitored always has its own bar | `quality_by_rating` |
| Analysis › Points by category | Each score category's points the scored records kept of its weight on average (its weight less the record's deductions in it, each at most the weight), weakest first; the rules that are a flag only listed under it | `dimension_breakdown`: `VisitEntity.category_deductions` |
| Analysis › Programmatic visits and HACT | For the partners of the filter with programmatic visits required: required, planned and completed in eTools, NeuroDB's completed programmatic FM visits, and the gap | `hact_programmatic`: `PartnerHACTYear`, `datamart.fm.programmatic_visits_by_partner` |
| Analysis › Follow-up | FM action points of these visits (open, overdue, high priority) and the off-track or constrained visits without one | `action_points`: `fmm.VisitActionPoint`, `datamart.ActionPoint` |
| Records › Find a visit | An id, "#1722", "Visit 1722", a key, a reference or a reference number | `fmm:lookup` |
| Records › Records — detail and flags | One row per record (as FMS lists them), most urgent first, 50 a page (*Per page* 25, 50 or 100, `?page_size=`); *Group by visit* (`?view=visits`) lists the visits instead, each with its mean, lowest record, records and most urgent record (their number equals *Monitoring visits*); *All rows (CSV)* follows the list shown | `fmm:visits` |
| Visit page | Everything known of one visit, with a card per record (below) | `fmm:visit` |
| Map › Visit locations map | One point per visit (its records share its place; the popup lists them with their scores) against the places its programme documents planned, in FMS's legend: actual visit, matched by coordinates, matched by name only, PD locations not visited; "N visits mapped (M records)"; the visits not shown for want of coordinates | `fmm.geo.map_points` |
| Drill-down window | The records behind a chart bar, a chip or a count (a figure of visits: the records of those visits), with the visits they belong to: visit, entity, type, partner (full name), PD number, location, section, visit date, rating, quality, urgency, flags; *Open in the records list* | `fmm:drill`, `Scope.records` |
| Every chart | *Download PNG* saves the chart as drawn (Plotly's own `downloadImage`; the bar lists drawn in HTML on a canvas, `data-rows-png`; no other library); *PDF* prints its card alone (A4 width; "Save as PDF" in the print window) | `charts.js`, `app.js` (`data-card-pdf`) |

### Using the page

Every signed-in user but a donor can read it; with `FMM_ENABLED` off it answers 404 and its menu item
is hidden.

- **Filters.** Period (this year by default; last year, a calendar year, this or last quarter, the
  last 30 or 90 days, two dates, or *All time*), section, governorate ("Not located" too), field
  office, partner, entity type, rating, status, monitoring modality (UNICEF staff, a third-party
  monitor...; "Modality not known" too), quality band (High from 80, Medium from 50, Low, or *Pending
  (no score)*), urgency band (High: red, Medium: amber, Low: below amber; a visit without a score has
  no urgency and is in none), "Programmatic visits only" and a search on the visit, its references,
  partners, programme documents and place. *All time* and two dates add "Data available from X to Y"
  (the first and last visit dates of the visits the other filters keep) to the reference line. `?year=Y` means 1 January to 31 December Y, so the year
  menu keeps the page. Periods read the visit date: its start date, else its end date when eTools has
  no start date (a data note counts those); a visit with neither is left out and counted in a data
  note. A user with a section sees it by default, as on the overview, but only on a
  bare visit to `/fmm/`: every link the page writes carries `section` (empty for every section), and a
  chip "Your section: … ×" shows every section. A governorate can be given as its gazetteer name
  ("Beqaa") or its key ("bekaa"). The **record filters** (entity type, partner, programme document,
  rating, quality band, urgency band) read each record: they keep the records that match and the
  visits with at least one of them, and the record figures then count the matching records only (a
  data note says so). Links from charts add drill-downs (month, status, visit rating, HACT Q1, score
  band, flag, flag count, urgency band, place, recurring issue, rule, review), shown as removable chips.
- **Reference line**: the filter, when the field monitoring rows were synced, when the scores were
  computed and with which rules version ("recomputing with rules v8" while a rescore waits), a warning
  when the last refresh failed, the release banner of scores per record ("Scores are now per record, as
  in FMS (rules vN). AI checks for N records are still running.", shown while the rules version the
  records migration made is current and the records are not yet all scored and checked), and *What does quality mean for Lebanon?* (FMS's methodology panel,
  rendered from the rule set the engine applies: the three bands, the score categories and their
  weights, the core rules with their HACT rule, the additional rules switched on with their category
  and deduction, and urgency; rules switched off are not listed).
- **Data notes**, each only when it applies: the section rule differs from the overview's, visits
  placed in the governorate through their monitoring site only, records without an activity
  reference (counted as their own visits here, not by the overview), visits with no start date
  (dated by their end date), visits with no date at all, a record filter on (the record figures count
  the matching records only).
  With no filter, the visits of a year equal the overview's field monitoring visits and the field
  monitoring page's "Monitoring activities", and the records its "Findings".
- **Morning briefing** (first on the Insights tab): ten tiles over **this year so far**, 1 January to
  today, whatever the page's period (the page's other filters are kept and named): critical flags
  (records at or above *urgency red*), average quality per record, low quality records (scored
  below the Medium band), critical partners (the partners of a record at or above red; the top five
  as red chips, each opening the partner's critical records), monitoring visits (distinct visits,
  with their records), and the visits at review status (*Pending report review*: awaiting the
  reviewer's sign-off), submitted, in data collection, assigned and completed. Each tile has its
  definition under ⓘ and opens the records behind it; *Quality by governorate* gives each
  governorate's average quality and records this year, each opening its records.
- **Key figures**: monitoring visits (distinct visits, whatever their status, with the breakdown),
  records (rated / not monitored / not rated yet), the average quality score per record (the mean of
  the scored records), and the records of high urgency (red, with the amber ones, and *View urgent
  records →*). Each links to the Records tab (*Monitoring visits* to the list grouped by visit).
  Figures are kept 10 minutes, and a refresh or a new day shows at once.
- **Critical records requiring attention** (Insights tab, after the AI brief): one card per record,
  the scored records at or above red urgency, most urgent first, at most 10 (the five most urgent
  amber ones when none is red), each with the rules it failed; R19 counts the monitors not on the
  staff list, never naming them.
- **Quality tab**, in FMS's order: the quality score trends (average quality score and reports per
  month, two smooth lines on two axes), the monitoring volume over time (records per month as bars,
  their visits in the tooltip, their average quality as a line), the HACT Q1 finding rating
  distribution (records, each with its own Q1 answer; the overall finding rating instead, with a note,
  when no record of the filter has a Q1 answer; the drill-down pills count each rating, and Not
  Monitored apart), the places visited with their records and visits (top 10, *Show all*); then the
  top recurring issues (the records' flags grouped by rule and reason, with their records, visits and
  mean urgency), the quality issues summary (records flagged by R6, Not monitored: reported visits none
  of whose records is rated, and records with three or more flags). The quality score distribution, the
  rule score trends, the rule analysis and the flag counts are first on the Analysis tab. Every chart card has *Download PNG* and *PDF* (the card alone, printed or saved as a PDF).
- **Analysis tab**: first, in FMS's order, the quality score distribution (five bars of 20 points in
  FMS's colours, 100 in the top one, and the records not scored), the rule score trends (per rule, the
  share of its maximum points earned by the records of each month; worked out when the panel scrolls
  into view), the quality rule analysis (each rule that checked records: its records flagged and a bar
  of the share not flagged; every rule under it, "not available" when the checklist answers are
  missing, see Fields found), the quality flag frequency, the flag count distribution, entity
  performance (All by default; each entity's own records), quality by field office and section
  performance (first 10 records, *Show all*); then highlights (visits, records, reported visits,
  governorates covered out of the gazetteer's, average quality per record: the same figure as the key
  figure, off-track records, PSEA-flagged visits out of those with a PSEA question, the scored
  records by band, the records by entity type), the governorates not visited, the field offices (the
  records of a visit to a PD with two offices count in both), the visit frequency, records and
  coverage (rated records ÷ records) by place,
  the quality by finding rating (records; Not monitored always its own bar), the points earned per rule (weakest first; a rule
  at 0 points is a flag only), the HACT programmatic visits of the partners of the filter (required,
  planned and completed in eTools next to NeuroDB's count of completed programmatic FM visits; gap =
  required − completed in eTools) and the follow-up (FM action points of these visits: open, overdue,
  high priority; the off-track or constrained visits without one).
- **Drill-down window** (`/fmm/drill/`): a chart bar, a chip or a count opens the records behind it
  (a figure of visits: the records of those visits), most urgent first (50 at most), with how many
  visits they belong to and *Open in the records list* for the rest. Charts carry codes
  (`month=2026-05`, `bucket=80-90`, `hact_q1=constrained`, `flag=R1`); an address with a label as a
  chart draws it ("May 2026", "80–100", "On track") is refused, so a cut or translated label can never
  open the wrong records.
- **Records tab** (the address keeps `tab=visits`): *Find a visit* (an id, "#1722", "Visit 1722", a
  key, a reference or a reference number; a miss offers the three nearest ids of the filter), then the
  **records list**, one row per record as FMS lists them, 50 rows a page, most urgent first (sortable
  by date, partner, quality and urgency), with what a visit and a record are written above it. *Group
  by visit* (`?view=visits`) lists the visits instead, each with its quality (the mean of its
  records), its lowest record, its records and its most urgent record. Red rows are at or above the
  red urgency threshold, amber rows between amber and red (Score settings). The Team column shows
  names only, is hidden on phones and is never copied or exported. *All rows (CSV)* follows the list
  shown: one row per record (the record id, its visit's references, dates, status, place, sections
  and offices, its entity type, entity, partner, programme document, CP output, rating, HACT Q1,
  quality, flags, urgency and its visit's review), or one row per visit (its references, dates,
  status, partner, programme documents, place, sections, offices, rating, HACT Q1, quality, lowest
  record score, records, records scored, flags, urgency, action point counts and review); never the
  team, the visit lead or a narrative.
- **Map tab** (*Visit locations map*, in FMS's legend wording: actual visit, matched by coordinates,
  matched by name only, PD locations not visited): each visit with a point, set against the places its own programme documents planned
  (`PCA.locations`, placed through the gazetteer). A visit is *matched by coordinates* (green) when
  it is less than `FMM_MATCH_KM` (2 km) from a planned place and **both points are exact**: the visit
  placed by its monitoring site or by its location's own point at the gazetteer's lowest level
  (cadasters), and the planned place a cadaster with its own point. A district's or governorate's
  centre, its own or borrowed from an ancestor, never matches by coordinates; such a visit point is
  drawn fainter with "approximate: placed at …". Otherwise a visit is *matched by name only* (purple:
  the same location or P-code, the planned district or governorate that holds it, or the same name), or
  an *actual visit* not linked to a PD location (blue). A grey ring is a planned place no visit of the filter reached
  (*PD location not yet visited*; it opens the programme document). The rings are the planned places
  of the programme documents the visits went to, or, with *Every active programme document*, those
  of every active programme document of the filter's sections, partners and governorate. The chips
  above the map count each group and the visits without coordinates; the legend's entries show or
  hide their group. At most 1,000 points are drawn (the most urgent visits first), with a note when
  there are more. The table below the map lists every point drawn with its link, the planned places
  not visited and the visits without coordinates: it is the keyboard route, as the map's popups open
  on hover or tap only. `?visit=<key>` centres the map on a visit and opens it (the visit page's *On
  the map* link). Only OpenStreetMap tiles are loaded, and no point carries a team member, a visit
  lead or a narrative.
  **The Map tab loads in parts** (see *Pages that load in parts*): opened from an address (a link, a
  bookmark, the visit page's *On the map*), the page answers at once with the header, the filter bar,
  the reference line, the chips, the data notes, the key figures and the tab bar, and a grey placeholder
  where the map goes; the map, its legend and its three tables (one part, `map`) arrive in a second
  request, with the filter written out (`section=` for every section, the user's own section on a first
  visit), the tab, the planned places shown and the visit centred. Choosing the tab from the tab bar,
  changing a filter or switching between *Programme documents visited* and *Every active programme
  document* still answers the results with the map drawn in, in one request (it takes well under a
  second). The other four tabs stay one request each: on a copy of the data at production size (5,075
  visits, 18,696 records) each answered whole in under 0.3 seconds cold, the Map tab in 0.33-0.45 seconds.
- **Visit page** (`/fmm/visits/<key>/`, also a window from the table; the address AI answers will
  cite): status and rating with their dates, HACT Q1, quality and its basis, urgency and its parts,
  the follow-up signals (an off-track or constrained visit without an action point, overdue and
  high-priority action points, a late report: shown apart, never part of urgency), links (partner,
  programme documents, HACT assurance, *On the map* when the visit has a point, the visit's action
  points, and *Open in eTools* once `FMM_ETOOLS_ACTIVITY_URL` is set), place and how it was located, the
  monitoring modality and programme areas, the visit goals and objective, sections and offices with
  their source, the team (names only, shown to NeuroDB users only), the visit's quality ("Visit
  quality 52.3 · mean of 3 records · lowest 31.0 · most urgent 78"), then a **card per record**
  (`#record-<datamart_id>` opens it): its entity and type, rating, HACT Q1, programme document match,
  score and band, urgency, flags, the points each category lost, the narrative in full and the Q1, Q2
  and Q3 answers written on its row (e-mail addresses hidden) and its own rule results; the checks of
  the whole visit (R19, read from its monitors and field offices) are listed once after the cards,
  as they apply to every record; the checklist questions and answers (read from the eTools records when the page opens),
  programme activities and CP outputs (linked to the country programme when they match), the action
  points, the partner's programmatic visits for HACT and each programme document's planned visits of
  the quarter, then, for each programme document, *what the partner reported, and other visits*: its
  indicators with the tracking status of the latest period the partner reported ("On track · reported
  for Jun 2026"), the TPM activities, UNICEF staff programmatic trips (never the traveller) and other
  FM visits to it within 90 days of the visit, the country programme outputs it contributes to (its
  eTools CP outputs matched to the current country programme), and the knowledge base documents that
  mention it (else its partner); then the review and the data notes.
- **Reviews**: an Administrator, or a Section editor of one of the visit's sections, marks a visit
  *Reviewed*, *Needs follow-up* or *Data issue*, with an optional note (500 characters, kept in
  NeuroDB, never sent to the AI). Reviews are kept by visit key, so a refresh never loses them.
- **Action points of a visit**: the visit page links to `/action-points/?module=fm&visit=<key>`, which
  lists exactly the action points matched to that visit, with a chip "From Visit 1722 ×". The action
  points search also finds an action point by its module reference (a visit reference) and, for a
  number, by its eTools activity id.

### Rules of this page

**One definition per figure.** Each figure has one function, used by every block, the AI facts and the
chat, so the same figure never differs between two places.

| Figure | Definition |
|---|---|
| Visit | One eTools monitoring activity: the finding rows that share an activity id (else an activity reference, else the row alone) (`datamart.fm.visit_key`). |
| Visit date | The earliest start date of its rows, else (eTools left the start blank) the latest end date (`Visit.visit_date`). Every period, month and year reads it: Monitoring insights, the overview's *Field monitoring visits* and `/field-monitoring/` (`datamart.fm.finding_year_q`), so a visit from 30 December to 3 January counts in the year it started on every page. A visit with neither date is left out of every period and counted in a data note. The rating date, urgency recency, the follow-up and late-report signals and the HACT programmatic count still read the end date. |
| Monitoring visits (key figure) | Every visit of the period, whatever its status, as the overview counts them: distinct visits however many records each holds (with a record filter, the visits of the records counted). |
| Record | One finding row of a visit: an entity assessed (partner, CP output or PD/SSFA) with its own rating, narrative and Q1–Q3, scored, flagged and given an urgency on its own (`fmm.VisitEntity`; its stable id is the eTools `datamart_id`). The records of a year are the "Findings" of `/field-monitoring/`. A record follows its visit for the period, status, sections, offices, governorate and modality; a record filter (entity type, partner, programme document, rating, quality, urgency) counts the matching records only. |
| Record rated | Its rating reads On track, Constrained or Off track. Every share of ratings (key figures, charts, highlights, the AI facts, the brief written by NeuroDB, the chat's look-ups) is a share of the rated records (or, where a block counts visits, the rated visits) only. |
| Status group | planned (draft, checklist, review, assigned), in progress (data collection, report finalization), reported (submitted, completed), cancelled, status unknown. |
| Visit status | The most advanced status of its rows; rows that disagree are noted on the visit. |
| Visit rating | Its worst rated record (Off track, then Constrained, then On track); *Not monitored* when none is rated. Watch, the signals and the monitoring gaps read it; the rating figures count records. |
| HACT Q1 | Of a record: its own Q1 answer, else the one given for its partner, else the one given for the whole visit. Of a visit: the worst of these and of the visit-level answers. Charts count records. |
| PSEA flag | A PSEA answer coded in *Answers that flag* (Yes, Constrained or Off track by default) flags the visit; asked and answered otherwise: not flagged; no PSEA question: not known. |
| Not monitored | eTools' rating "Not Monitored": the visit was **planned but not conducted** (the monitor did not attend, the partner was unavailable, access was denied), a planning status, not a programme outcome. A visit is Not monitored when it is reported and none of its entities is rated; a planned or in-progress visit with blank ratings is "not rated yet". It is always a count apart, never inside a share of ratings and never a share of all visits. |
| Scored visit | A visit whose status is one of the *scored statuses* (Score settings; report finalization and completed by default); its records are scored. Every other visit is **pending**: its records have no score and no urgency, quality band "Pending" (cancelled ones read "cancelled"). Not monitored visits of a scored status are scored. |
| Quality score | FMS's, per record (an entity row of a visit): 100 less the deductions of the quality rules that fired, each score category's deductions at most its weight, never below 0, rounded half up to one decimal. A visit's is the mean of its records' scores. A record whose AI checks are not all done is *provisional* and counts as not scored, and so does its visit. |
| Average quality | The mean score of the scored records of the filter (half up, one decimal), as FMS averages: the key figure, the briefing tile, the Analysis highlight, the AI facts and the chat's `fm_summary` are this one figure. A visit's own quality (the records list grouped by visit, the visit page) is the mean of its records. |
| High urgency | Urgency at or above *urgency red* (70); amber (Medium) from *urgency amber* (40) to 69; Low below. Each record has its own urgency (a visit's is its most urgent record's); a record without a score has no urgency and is in no urgency figure. |
| Governorates covered | Governorates with a visit, out of the gazetteer's active governorates. |
| Open / overdue action point | Status open or in progress (`in_progress`, "in progress", "in-progress": FMS counts them as open); overdue when also past its due date (the action points page's, the overview's, Monitoring insights' and Watch's definition, `ActionPoint.OPEN_STATUSES`). The page's *Open* status filter keeps the ones in progress. |
| FM programmatic visits (NeuroDB) | The completed, programmatic visits that ended in the year, counted once per visit for each partner of its records. |

R19 (the monitor is on the staff list of the visit's field office) reads the **field office staff
lists** administrators keep (admin → *Field office staff lists*); it skips a visit silently while its
office has no list.


**Question roles.** The rules need to know which checklist question is Q1 ("Have the activities been
implemented as planned…"), Q2 (activities monitored), Q3 (key observations) and the PSEA question.
*Score settings → Question patterns* finds them by the words of their text (`"^q2"`: starts with
"q2"; `"=…"`: the whole text; anything else: contains). *Questions found* (linked from Fields found)
lists every question of the answers with its records, its HACT flag, the role it has now and the share
answered; *Use as Q1 / Q2 / Q3 / PSEA* pins a question's whole text to a role (a pinned text wins over
the other patterns) and is recorded as a rules version. With no Q1 pattern at all, the question eTools
flags as HACT is Q1. Everything that depends on a role is worked out when the visits are scored, so a
pattern change needs only a scores-only refresh:

- **HACT Q1 of an entity**: its own Q1 answer; else the one given for its partner (on the partner's
  own row, or for the partner as a whole); else the one given for the whole visit. **Of a visit**: the
  worst of these and of its visit-level answers (Off track, then Constrained, then On track).
- **PSEA flag**: a PSEA answer whose code is listed in *Answers that flag* (`yes`, `constrained`,
  `off_track` by default) flags the visit; asked and answered otherwise: not flagged; no PSEA question,
  or the answer keys not found: not known.

**Which visits are scored.** The visits whose status is one of the **scored statuses** (*Score
settings*, a list of eTools statuses; *report finalization* and *completed* by default, as FMS does).
Every other visit is **pending**: no score and no urgency, "Pending" in the quality band filter, and
every rule reads "does not apply" ("Pending: the visit's status is not one of the scored statuses").
Cancelled visits read "cancelled". A Not monitored visit of a scored status is scored. Changing the
list is saved as a rules version and rescored like any other score setting.

**The rules: FMS's model** (admin → *Quality rules*, seeded from FMS Lebanon's rules file,
`fmm/lebanon_rules.json`). Each rule has an id (R1 … R32, never renamed), a name, a **type**, a score
**category**, a group (*core*: FMS's six HACT rules; *additional*), on or off, a **deduction** (the
points it takes off its category when it fires), a **flag template** and its parameters (JSON, as in
FMS's file):

| Type | What it does | Parameters |
|---|---|---|
| Completeness | Each field of the report it lists that is missing takes its own deduction off ("R1: Incomplete monitoring report — missing: Q2 – Activities monitored"). A field NeuroDB cannot read in the data is not checked. | `fields`: `name`, `label`, `deduction` each |
| Deterministic | One column scored by **bands**: the first band that matches, in the order listed, gives the deduction (a band matches from its `min` up to its `max_inclusive`; for a list column, from `min_categories`, and with `required_met` only when the entity type's `required` categories are there). A visit without a value takes `missing_value_deduction` off, or the rule is "not available" when that is 0; a column NeuroDB cannot read at all (the checklist answers not found, a count whose key the records do not have) leaves the rule "not available" on every visit, never deducted. | `field`, `field_type` (numeric or list), `scoring`, `missing_value_deduction`, `list_separator`, `entity_type_scoring` |
| AI check (narrative) | An AI check of the fields it lists (see *AI checks of the quality rules* below): fails when the AI finds the report not coherent, with the AI's explanation in the flag. | `fields`, `ai_prompt_key` |
| Reference check | Compares a value with reference data: `member_in_mapped_list` (R19, the staff lists), `value_in_mapped_list` (R20), `section_in_cp_output` (R21), `pd_reference_locations` (R23), `value_in_list`, `string_contains`. | `check_type`, `field`, `key_field`, `reference_map`, `reference_list`, `contains` |

`entity_type_filter` (Partner, CP Output, PD/SSFA or PD) limits a rule to the records of those types; on
other records it reads "does not apply" (R20 runs on programme document records only, R21 on CP output
records only). A rule switched off keeps no result (the
visit page lists it under "Switched off"); an AI check reads "AI check switched off" in the rule list
while the AI checks are off and none is kept.

The Lebanon set switches on **R1** Report Completeness (narrative 3, rating 2, Q1 2, Q2 2, checklist
categories 2), **R2** Question Answer Completeness (share of checklist questions answered: 80%+ 0,
50%+ 5, below 10; not in the data 3), **R3** Narrative Evidence Quality (AI, 20), **R5** HACT Activities
Alignment Q1↔Q2 (AI, 20), **R6** Narrative and Rating Coherence (AI, 15), **R7** Action Point Quality
(AI, 5), **R8** Action Points Cross-Check (AI, 5), **R32** Challenge-to-Action Alignment (AI, 5),
**R19** Field Office - Team Member Validation (3), **R20** Location - PD/SSFA Site Validation (5), **R21**
Section - CP Output Alignment (3) and **R23** PD Reference Locations (0: a flag only). R4, R9-R18, R22
and R24-R31 are seeded switched off, with their lists empty; an administrator can switch one on once
its category has a weight.

**Reference checks from eTools.** R20 and R23 read the registered locations of the visit's programme
documents (`PCA.location_p_codes` and the PD's locations), R23 falling back to those of the partner's
programme documents running on the visit date; a visit passes when its place's P-code, or that of a
place holding it (the cadaster of a site, the district of a cadaster), is among them. R21 reads the
sections of the programme documents that name the visit's CP output. The rule's own `reference_map`
**adds** entries to what eTools holds (FMS Lebanon's hand lists are seeded there for R20, R21 and R23).
R19 reads the **field office staff lists** (admin → *Field office staff lists*, Administrators only:
one office per list, one e-mail address per line). The monitors' e-mail addresses are read from the
records, compared in code and dropped: never kept, never shown (the list shows only how many addresses
an office has; the flag says "Monitor is not listed as staff for field office 'Zahle'"), never sent to
the AI. R19 skips a visit whose offices have no list, so it does nothing until a list is filled.

**Columns derived at the build** (FMS's processed record, worked out by every full refresh from the
checklist answers and action points; empty when the visit has no value, and a rule then applies its
missing-value deduction or skips; when the records have no key for a column at all, its rules are "not
available" on every visit): the share of checklist questions answered (`fmq_answered_pct`), the
categories with an answer (`fmq_answered_categories`, from the questions' category key, *Fields found*),
the collection methods used (`method_count`), the red-flag answers (`red_flag_count`: an answer of 2 or
less on a 5-point scale, 1 on a 3-point scale, read from the answer options), the attachments
(`attachments_count`, when the records have the key) and the action points assigned (a count, never
who). The action points' texts and due dates are read when a check needs them, never stored.

**Score, per record (Release 2 step 5).** As in FMS, each **record** (an entity row of a visit: one
partner, programme document or CP output assessed) is scored, flagged and given an urgency on its own:
its rules read its own narrative, rating, Q1-Q3 answers (its own, its partner's or the visit's), the
checklist columns worked out over the answers that apply to it, its own programme document, CP output
and place, and the visit's own fields (offices, sections, action points, repeated on each record). R19
(the monitors and the visit's field offices) is checked once per visit and counts on every record. A
record's score is 100 less the deductions of the rules that fired; each **score category**'s
deductions count at most its weight (*Score settings → Score categories*; Lebanon: completeness 30,
evidence 20, alignment 20, coherence 15, Q3 quality 10, actionability 5); never below 0; rounded half up
to one decimal. Bands: High from 80, Medium from 50, else Low. The flags are the rules that fired, R23
included at 0 points; 3 flags or more is a high-flag record. A **visit**'s quality is the mean of its
records' scores (half up), with its lowest record's score, its records' flags together, its most urgent
record's urgency and its records' mean deductions per category (the visit page: "100 less Completeness
19"); its rule list says a rule failed when one of its records failed it. Urgency's recency still counts
from the day the visit ended.

**Provisional records and visits.** While AI checks are on, a record whose checks are not all done (or
out of date) is **provisional**: it has its score so far, but counts as not scored until its checks are
done, so it never gets full marks for checks not made; a visit with a provisional record is provisional
too ("provisional (2 AI checks pending)", counted over its records) and counts as not scored everywhere
(no average, band or urgency). The rule analysis counts the pending checks per rule.

**Category sums and Rebalance.** *Quality rules* shows the sum of the deductions of each category's
rules that are on against the category's weight (⚠ when they differ, or when the weights do not add up to
100). **Rebalance** (a button on the list, with a note) scales each category's rule deductions (a
completeness rule's fields, a deterministic rule's bands) so that they add up to its weight, and the
weights so that they add up to 100; no rule is added, removed or switched on or off. It is saved as a
rules version and rescored, like every other change.

**Urgency** (FMS's formula), 0 to 100, for scored visits only, with every part kept to explain it:

`urgency = quality_gap × (100 − quality score) + recency × recency part + red_flags × flags part`

- the **recency part** is 100 on the day the visit ended, falling in a straight line to 0 at *recency
  days* (180) after it: `100 × max(0, 1 − days since the end ÷ recency days)`;
- the **flags part** is 25 per red flag (a rule that fired, R23 included), at most 100;
- the **weights** are 0.50, 0.30 and 0.20 by default (*Score settings*; each from 0 to 1, and they must
  add up to 1).

The total is rounded half up and kept within 0-100. Red from 70, amber from 40 (both editable). A visit
without a score (pending, cancelled, or provisional) has **no urgency**: "—" in the table,
last when sorting, and in no urgency figure. The daily 05:25 refresh recomputes it, so a visit grows
less urgent as it ages, unless its quality is low and its flags many.

| Urgency part | Weight (Score settings) | Value |
|---|---|---|
| Quality gap | `quality_gap` 0.50 | 100 − the quality score |
| Recency | `recency` 0.30, over *recency days* (180) | 100 × max(0, 1 − days since the visit ended ÷ recency days) |
| Red flags | `red_flags` 0.20 | 25 per failed rule, at most 100 |

Every visit keeps its weighted parts (`urgency_parts`), shown on the visit page ("Why urgency 33:
quality gap 29.8 · recency 12.2 · red flags 15") and as the urgency pill's hover in the table.

**Follow-up signals** (shown on the visit page, never part of urgency since Release 2): an Off track or Constrained reported visit with no action point more than
*follow-up days* (14) after it ended; its overdue, high-priority overdue and high-priority open action
points; and a planned or in-progress visit that ended more than *report late days* (30) ago (a late
report). They are worked out by every refresh, scored or not. The thresholds that are NeuroDB's
proposals rather than the reference dashboard's or FMS's are listed for the user to confirm in the
go-live checklist.

**Changing the rules.** Administrators only: other staff can read the rules, the settings and the
versions. Each rule, the score settings and every pinned key are saved with a required note; the
*Preview effect* button first shows what the change would do over this year's visits ("R2 would flag 7
visits (now 3); average quality 91.2% (now 94.7%); scored visits 29 (now 29)") without saving anything.
Each save records a new rules version (*Rule versions*: who, when, the note, and every setting beside
its value now) and asks for a scores-only refresh in the background (a full one for a pinned key); the
admin never waits for it. *Restore this version* writes an older version back as a new one (the history
only grows) and rebuilds the visits when its pinned keys differ. A rescore asked for while another
refresh runs is served by that refresh, so none is lost (see above); each visit keeps the rules version
it was scored with.

### Filters and caching

- **Period**: this calendar year by default; last year, a calendar year, this or last quarter, the last
  30 or 90 days, two dates, or *All time* (every visit with a date; no earlier period to compare
  with, and the reference line says "Data available from X to Y"). `?year=Y` means 1 January to 31 December Y and wins over any preset, so
  a link with a year (Home's *Also in* line, the Country overview) keeps the page on that year. The header
  has no year menu on this page: the period presets are its own year control. Periods read the visit date: the start
  date, else the end date (a data note counts the visits dated by their end).
- **Section default**: a user with a section sees that section on a bare visit to `/fmm/`, as on the
  overview, with a chip to show every section. Every link the page writes, and every link into it
  from another page, carries `section` (empty for every section), so following a link never applies
  the default again and the figures equal those of the panel the link came from.
- **Entity-level filters** (entity type, partner) keep a visit when one of its entities matches; the
  entity figure then counts the matching rows only, and a note says so.
- **Modality, quality band and urgency band** (`modality`, `quality`, `urgency_level`, each may be
  given more than once): the modality written on the visit's rows ("none": not known); High, Medium,
  Low or Pending (no score) by the score bands; High (red), Medium (amber) or Low (scored, below amber)
  by the bands the last scoring gave. A filter address without them keeps the same cache and AI brief
  as before Release 2.
- **Drill-downs** come from chart clicks and links only, carry codes (`month=2026-05`,
  `bucket=80-90`, `hact_q1=constrained`, `flag=R1`, `visit_status=review`), and show as removable
  chips (the 20-point buckets of Release 1, `bucket=80-100`, still open). A label as a chart
  draws it is refused, so a cut or translated label never opens the wrong visits.
- **Caching**: each block is kept 10 minutes, under the filter, the language and a stamp: the last
  refresh and its rules version, the day, the NeuroDB action points, the last finished run of the jobs
  that write what the page reads but start no refresh (the eTools sync, which writes the programme
  documents, their planned places and the partners' names; the Locations sync; the eTools update
  clean-up), and the gazetteer's places and kinds of place (an administrator may rename or move one in
  admin → Locations). A refresh, a new rules version, midnight, one of those runs or such an edit shows
  at once. Four blocks are kept in the **shared page figures** (see *Pages that load in parts*), so
  every web worker reads what one computed: the one pass over the visits (the Quality and Analysis tabs,
  the AI card's fallback brief and the chat read it), the entity table, the rule counts and the Map
  tab's map. They hold figures only, the same for everyone (the filter holds the sections, a user's own
  section included); the pass over the visits is also kept under the *Score settings* thresholds it reads
  (the bands, the urgency levels, the number of flags that makes a record "high-flag"), so a saved
  setting shows at once, before the rescore it asks for has run. The other blocks are kept in each
  web process's own cache. A drill into the reviews is never kept (a review saved a minute ago counts
  at once). Each tab and the visit window load on their own; the map, the brief, the entity table, the
  rule score trends and the long place lists load only when shown (the entity table and the rule trends
  when they scroll into view, a place list when *Show all* opens), and each of these panels is drawn
  alone, from its own figures, never with the rest of its tab (until step 3 of the page-speed work, the
  server drew the whole tab for each and the browser kept the panel).

### How Monitoring insights differs from the overview

With no filter, the visits of a calendar year equal the overview's *Field monitoring visits* and the
field monitoring page's *Monitoring activities* for that year, and the records its *Findings*
(tests pin these). Where Monitoring insights counts differently on purpose, the data note
under the reference line says so, each line only when it applies, with its count:

- the section is each visit's programme-document section; the overview counts every visit of a partner
  with indicators in the section, so its figure can differ;
- visits placed in a governorate through their monitoring site only are counted there; the overview
  places visits by their location and leaves these out;
- finding rows without an activity reference count as their own visits here; the overview does not
  count them;
- visits with no start date are dated by their end date (a count, not a difference: the overview and
  the field monitoring page date them the same way);
- visits with no date at all are left out of every period.

The existing pages do not move because Monitoring insights exists: `/field-monitoring/`, the overview's
assurance figures and the partner page's visit series are pinned by tests before and after a refresh.

### Links into the rest of NeuroDB

Every link into Monitoring insights carries `section=` (empty when it shows every section), so a user
whose own section would otherwise apply sees the same figures as the panel the link came from.

- **Partner page**: a *Monitoring insights* panel for this calendar year: the partner's FM visits
  (every status), the average quality, the visits rated off track and constrained, the open and
  overdue FM action points of those visits, and the last visit (rating and date). Its link
  `/fmm/?partner=<id>&year=<year>&section=` opens the page with the same figures. The partner page's
  existing visits chart dates a field monitoring visit the same way (start date, else end date). No panel for a partner no visit ever monitored.
- **Programme document page**: the *Programmatic visits* table gains a *Field monitoring* column (FM
  visits that monitored the PD, by the year of their start date, else their end date), and a *Field
  monitoring visits* panel shows this year's FM visits per quarter (by start date, else end date) against the visits eTools plans (`PlannedVisits`),
  the three latest visits with their rating, quality and urgency, a link
  `/fmm/?pd=<id>&year=<year>&section=` with the same count, and the PD's *what the partner reported,
  and other visits* for the year (as on the visit page, with the country programme outputs the PD
  contributes to; the page already lists its knowledge base documents). A TPM activity's status is
  dated by the eTools sync that brought it.
- **Country overview**: under the assurance card's figures, *Field monitoring visits in Monitoring
  insights* opens the page for the overview's year, sections and governorate (`section=` empty when every
  section is shown; the governorate as the overview names it, which the page reads). The overview's
  own figures and links do not change. Its section filter counts every visit of a partner with
  indicators in the section, Monitoring insights each visit's programme document section: the page's
  data note says so.
- **Assurance (HACT by partner)**: a column *FM programmatic visits (NeuroDB)*: the completed
  programmatic FM visits that ended in the HACT year, one per visit for each partner of its rows,
  next to eTools' own done / required. It links to that partner's programmatic visits of the year in
  Monitoring insights.
- **Action points**: an FM action point matched to a visit shows *Visit 1722* under *Raised from* with
  its link confidence, opening the visit; see *Action points* below for the page.
- **Knowledge hub and What's new**: every visit dated (start, else end) in the last `FMM_HUB_MONTHS` (24) months
  is a *Field monitoring visit* (`fm_visit`) in the hub: "Visit 1722 · AMEL · 12 May 2026", with its
  date, status group, rating, quality and urgency band only (never a narrative, an answer or a team),
  linked *about* its programme documents, partners and the country programme outputs its CP outputs
  match, *in section* and *takes place in* its governorate and district; its lookup is `fm_visit`.
  What's new tells a visit only as described under *What's new* above.
- **NeuroDB Watch**: the check `fm_follow_up` (trial; see *What it follows*).
- **Sections**: the section names written on visits themselves join the eTools spellings
  Administrators confirm in *Section matches*.

### Data and its keys

eTools never documented the keys of its field monitoring records: the findings (`fm-ontrack`), the
checklist answers (`fm-questions`), the answer options (`fm-options`), the programme activities of
each visit (`fm-programme-activities`), and the office and section lists. For each field Monitoring
insights reads (a checklist answer, the activity it belongs to, the question's text, the team...) the
code lists candidate keys, most likely first (`neurodb/fmm/fields.py`). The refresh (after every
Datamart sync, each morning at 05:25, *Run a job → Monitoring insights* or *Refresh now* on *Fields
found*):

1. links the findings not linked yet to the programme document their entity names, as the Datamart
   sync does;
2. reads every record of the six datasets (contact keys removed first, as the Datamart store does)
   and counts each key, at the top level and one level down (`parent.child`), with the types of its
   values and up to three examples, cut to 60 characters and cleaned: e-mail addresses, links, phone
   numbers, the names NeuroDB knows, the names written under the records' keys that name people (not
   the words of notes or comments) and names after a title are replaced, and a key that holds a
   person (visit lead, team, monitors, user names, notes and comments...) shows "(withheld)";
3. chooses the key of each field: the first listed key that fills at least `FMM_KEY_MIN_COVERAGE`
   (50%) of the records with a usable value (an id needs a number, a yes/no needs true or false),
   else the fullest one, else none: the field is *Not found* and what needs it will say "not
   available". A later key filling at least 30 points more marks the choice *Found, another key is
   fuller*: look at it;
4. builds the visits: the finding rows of one eTools monitoring activity make one visit (its activity
   id, else its activity reference, else the row alone). Each visit gets its partners, its programme
   documents (the row's own link, else a PD reference in its record or its programme activities, by
   the PCA/PD pair first so that the amendment covering the visit is found), its CP outputs and
   programme activities, its place (the monitoring site, else the location; the governorate and
   district from the gazetteer, through the site's location when the visit has none), its point (the
   site's, else the location's own, else its nearest ancestor's, marked approximate), its sections
   (written on the visit, else its programme documents', else its action points', else, with no
   programme document, the partner's programme documents running on the visit date, marked
   "inferred from the partner"), its field offices (the same order, without the partner step), its
   team (names only, never e-mail addresses), the FM action points raised from it (by the activity
   id, else the activity reference, else its reference number) with their open, overdue and
   high-priority counts, and its checklist answers (joined by the activity id, else the reference;
   each applies to one entity row, to every row of a partner, or to the whole visit). Only whether an
   answer was given, its code (a rating, yes or no) and word counts are kept: the texts of answers and
   narratives stay where eTools put them. Problems (several statuses or places, an unlinked programme
   document, no date, an unknown rating, one reference under several activities) are recorded on the
   visit as counts;
5. scores each record, then each visit from its records, with the quality rules as they are when it
   starts (see *Rules of this page* above): the question roles, HACT Q1, the PSEA flag, the quality
   rules (with the AI checks' answers kept for each record's inputs), the score, its band and flags, and
   urgency. Each visit keeps the rules version it was scored with;
6. writes the keys, the visits, their records and the records' rule results (a visit has none of its
   own: R19, read once per visit, is kept on each of its records) together in one transaction: the
   pages see the old visits until it commits, a visit keeps its id while its key stays, and the visit
   reviews are never touched. The visits and rule results go through a temporary table where the
   database user may create one (PostgreSQL allows it by default; otherwise the same rows are written
   the slower way), and a scores-only pass rewrites only the rule results that changed. In the same
   transaction it writes the figures the pages add up, kept per visit (each visit's records added up,
   its failed checks per issue, its checks per rule and result, and those summed per day: `fmm.tallies`),
   all of them after a full pass and only those of the visits it changed after a scores-only one. A
   page whose filters keep whole visits reads them instead of every record and check (the same figures);
   any other change to the records, their checks or the visits' dates (the admin, a shell, a restore)
   marks them out of date at once (a database trigger, migration `fmm/0021_figures`), and the
   pages add up the records and checks themselves, a little more slowly, until the next refresh. Right
   after the migration that adds them they are out of date until the first refresh.

**The eTools FMM export's own names** (FMS user manual, §13.2) come first in `CANDIDATES` since
Release 2: `hact_q1_answer`, `hact_q2_answer` and `hact_q3_answer` (the Q1, Q2 and Q3 answers written
on a finding row), `field_offices` and `sections_names` (lists written with ";"), `programme_areas`,
`monitoring_modality` (UNICEF staff, TPM - iAPS, TPM - Voluntas...), `location_lat`, `location_lon`,
`location_type`, `location_pcode`, `location_name`, `team_members`, `visit_goals`, `objective`,
`dim_supplies`, `dim_psea` and `status`; the names of Release 1 follow as fallbacks. What the build does
with them:

- **Q1, Q2 and Q3 written on the row take precedence** over the checklist answers of the same
  question for that row's entity; a blank one falls back to the checklist. Only whether each was
  answered, its rating code, its word count and, for a short answer, a hash of its folded text (so
  R5's list of placeholders can be checked without it) are kept (`VisitEntity.row_answers`): the texts stay in eTools' records and are read
  when the visit page opens or a brief is written. R1, R3 and R5 read them; R2 counts checklist
  questions only.
- **Modality and programme areas** are kept on the visit (filter, AI breakdowns, visit page).
- **Coordinates**: after the monitoring site, the point written on the row (`location_lat`,
  `location_lon`) places the visit (*located by location*, counted as "written" in the refresh's
  details). It counts as **precise** for the map's 2 km match when `location_type` names the
  gazetteer's lowest level (its name, e.g. "Cadaster", or "admin level 3"), or when the point is more
  than 100 m from the gazetteer's point of its location (its own, or the one borrowed from an
  ancestor), so it is not a centroid; with no gazetteer location to compare with, only the type
  counts. Otherwise the gazetteer's location, then its nearest ancestor, place it as before.
- **A row whose location eTools did not link** is placed by its `location_pcode` when the gazetteer
  holds that P-code (counted as "by P-code").
- **Entity types**: "PD/SSFA" → programme document, "CP Output" → CP output, "Partner" → partner
  (`datamart.fm.entity_kind`).
- `dim_supplies` and `dim_psea` are shown on the visit page only; `person_responsible_email` is never
  read (contact keys are removed before anything is stored).

Each run is one line in *Import and sync runs* (*Monitoring insights refresh*, target `full`): rows
read are the finding rows and checklist records, rows written the visits. One runs at a time (a
database lock: a second start does nothing). A failure keeps the previous visits and keys and marks
the run *Failed*; a record that cannot be read is skipped and counted as a failed row (*Succeeded with
errors*), and Data health lists both. It runs:

- at the end of every eTools Datamart sync that synced a dataset it reads, in the sync's process
  (`FMM_REFRESH_AFTER_SYNC=inline`; `background` starts it as its own process, `off` leaves it to the
  morning run); its failure never fails the sync;
- every day at 05:25 (`fmm-refresh` on the Scheduled jobs page), so that what changes with the day
  (overdue action points) is recomputed even when no data changed;
- from Run a job → *Monitoring insights*, or a shell.

`fmm_refresh --scores-only` recomputes, from the visits already built and without reading the finding
records, what changes with the day or the rules: the action point counts, the question roles, HACT Q1,
the PSEA flag, the rule results, the scores and urgency. It reads the narratives (that column only, a
thousand visits at a time, never kept) and the Q3 answer records (for R5's list of placeholders);
`--probe-only` runs steps 1-3 alone. A saved rule asks for a scores-only refresh and a pinned key for a
full one:
the request is kept (`RefreshRequest`), and a refresh that is running when it arrives looks at it
before it releases its lock and runs the pass asked for (a scores-only run that finds a full refresh
asked for runs it too), at most `FMM_REFRESH_MAX_PASSES` passes, so a request is never lost; one left
over waits for the next run. `FMM_ENABLED=false` makes the refresh do nothing.

The team names of the visits join the names NeuroDB removes from every text it sends (NeuroDB Watch,
Ask NeuroDB, the AI checks), and the section names written on visits join the eTools section names an
administrator confirms (Section matches).

Fields found shows, above the keys of each dataset:

- the share of findings that carry an eTools activity id: aim for 95% or more (below it, a visit is
  told apart by its activity reference, which is right but changes how visits are cited);
- the share of the findings about a programme document that are linked to it, and how (full
  reference, PCA/PD pair, without the amendment, title): aim for 90% or more;
- the share of checklist answer records that hold an answer, and "Unanswered questions seen: n of N
  records": none among 200 or more means eTools exports answered questions only, so the share of
  questions answered (quality rule R2) cannot be measured;
- from the last full refresh: the share of checklist answer records that matched a visit, and how the
  FM action points were matched to theirs (by the activity id, the activity reference or its reference
  number, or not at all). Under 50% is flagged: for the action points, it means eTools' related
  module id is probably not the activity id;
- the fields not found, the choices to look at, and the rating, status and entity type values the
  findings hold with how NeuroDB reads each ("not recognised" ones need a change in the code).

Per field: the key used, its state, the share of records it fills, every listed key found with its
share, and what the field is needed for. An administrator can pin another key the data shows
(*Change* in the *Pinned key* column: a list of the keys found, or "auto"; a key that holds a person is
offered only for the team). The pin needs a note, is recorded as a new rules version (so it has who,
when and why, and can be rolled back with the rules) and rebuilds the visits in the background. A
pinned key the data no longer shows is flagged (*Set key not in the data*) and the listed keys are used
instead. The last refresh's details (its line in *Import and sync runs*) also give how the visits were
placed, where their sections and offices came from, how the FM action points were matched, how many
checklist records joined a visit, the question roles found, how the Q1 answers applied (to an entity,
a partner or the whole visit), and how many visits each rule passed, flagged, could not evaluate or
did not apply to.

After the first production sync, work through the *Go-live checklist* below.

### The AI brief and its prompts

`FMM_AI` is `false` at deploy and stays so until go-live (below): until then the Insights tab shows a
brief **written by NeuroDB from the figures** ("AI not used: AI is switched off") and no call is made,
and the chat reads "Chat is not available: the AI is switched off."

- **The brief** (Insights tab, loaded after the page): the parts the published prompt version lists
  (*Parts of the brief*, like FMS's insight sections). Version 2, from FMS's Lebanon prompt: *Coverage
  and Quality Summary* (a paragraph of at most 5 sentences), *Key Programmatic Findings* (at most 20
  bullets), *Operational Challenges* (4), *Recommendations* (5) and *Priority Action Points* (5). The
  brief's strict answer format is built from that list, so its keys are exactly the list's. Every
  sentence rests on the facts NeuroDB sends and names them; a sentence with a figure, date, reference
  or person's name the facts it cites do not hold, a link, an e-mail address, markup or the words
  staff pages never use is dropped before anyone reads it. Partner, place and section names pass (a
  test pins it); only people's names are refused. An action point is structured (priority High or
  Medium, programme section, partner or none, action, responsible party, timeframe, the facts it rests
  on) and NeuroDB writes it out as "[PRIORITY: High] Education / AMEL — action — Education section
  lead — within 2 weeks". An action whose responsible party names a person gets "Section lead", one
  whose section the facts do not name gets "All sections", and a partner the facts do not name is
  left out. When nothing passes, or
  the call fails, the code-written brief (or the last brief) is shown. Under each sentence, the
  visits it rests on open their window; the visits table's *AI* column marks them, and the visit page
  lists the sentences of the brief of the user's own landing filter that cite it.
- **What is sent**: the period and filter; the key figures with the rating distribution over the
  **rated** visits and entities (Not monitored a count apart) and the previous period; the rules; the
  `comp` most frequent **quality flags** (rule, reason, visits and up to 3 example visits); per
  section, field office, partner (the 30 most visited), monitoring modality and governorate: visits,
  average quality, rated visits by rating, the share Off track or Constrained of the rated ones and
  Not monitored apart; the follow-up and HACT counts; the 15 most urgent visits as cards (plus the
  flags' example visits); and up to `narr` monitors' notes, each with its row's Q1, Q2 and Q3 answers
  (at most 400 characters each), all cleaned of names, e-mail addresses, phone numbers and links (a
  note or an answer with more than 3 of them removed, or a note under 40 characters, is never
  sent). The team and the visit lead are never sent. A last check refuses a brief whose facts still
  hold a known name, an e-mail address, a phone number or a link (the brief fails with "could not be
  written safely" and an error is logged). *What was sent* on the card shows the facts and notes
  exactly as sent, kept `FMM_PAYLOAD_RETENTION_DAYS` (30) days.
- **When it is written.** Every morning at 05:40 (`fmm-insights`), for the whole country this year,
  each NeuroDB section with active users as they land on the page, and the whole country over the
  last 90 days (at most `FMM_NIGHTLY_MAX_INSIGHTS`), so people see a brief without using their quota.
  *Regenerate* (5 a day per person) never makes the page wait: it starts the brief in its own process
  and the card shows "Writing… about 30 seconds" until it is done; a second click, by anyone, follows
  the same brief. A brief whose facts have not changed since the last one is shown again with no call
  and no quota used ("Up to date"); a brief from older data says "the data has changed since", one
  from an older prompt version "Written with prompt v6 (now v7)". A brief left writing by a stopped
  process is closed after `FMM_INSIGHTS_TIMEOUT_SECONDS` plus a minute ("The last attempt stopped").
- **The chips** say what was really used: model, effort, the output limit and the tokens used,
  temperature and top-p ("applied", "not applied" with why, "not set"), notes and quality flags sent
  of those allowed (`narr`, `comp`), the prompt and rules versions, and the person's quota with the share of
  the office AI budget used today.
- **Retention.** The morning run also closes stopped briefs, blanks payloads older than 30 days,
  deletes refused and skipped briefs after 30 days and every brief after `FMM_RETENTION_DAYS` (180).

- **Prompt versions** (admin → *Monitoring insights* → *Prompt versions*, Administrators only; other
  staff read). A version holds the editable instructions of the brief and of the chat, the parts of
  the brief (key, label, paragraph or bullets, and the limit: the most sentences or bullets; the
  action points are the part keyed `action_points`), the chat's starter questions (one per line, at most 8), the
  model (blank: `FMM_MODEL`), the effort, the output limits (they **include the reasoning tokens**:
  8,000 for a brief in v2, 6,000 per chat call), temperature and top-p (empty: not sent), `narr`,
  `comp` and the per-person limits (5 briefs and 20 questions a day, 4 chat rounds, 120 seconds). v1
  was seeded and published by "NeuroDB (default)" with temperature 0.30 and top-p not set; Release 2
  publishes **v2** (note "FMS Lebanon prompt (Release 2)…"): the editable text adapted from FMS's
  Lebanon prompt (the Lebanon context, what Not Monitored means, the rating and quality thresholds,
  the priority flags and the guidance of each part), FMS's five parts, the four starter questions and
  8,000 output tokens, its model and other settings copied from v1. v1, retired, keeps its own four
  parts, so its briefs show as they were written, and can be published again with *Roll back*. *Add* starts a **draft**
  prefilled from the published version (or `?from=<id>`), with a required note; only drafts can be
  changed or deleted (a draft's test runs are deleted with it). *Publish* retires the published
  version; briefs already written stay until the next night's run or a Regenerate. Published and
  retired versions never change: *Roll back to this version* publishes a new copy of it (with a note),
  so the history only grows ("v9 = rolled back to v6"). The form refuses an e-mail address, a link or
  the name of a person NeuroDB knows in the instructions, and warns (without refusing) about effort
  medium or higher under 2,500 output tokens, about temperature and top-p both set, and about a
  parameter the model refused.
- **`narr`** is the most texts (narratives, checklist answers, search snippets) the AI may read per
  brief and per chat answer, each cleaned of names, e-mail addresses, phone numbers and links first;
  `narr = 0` sends none. **`comp`** is the **compliance depth** (FMS §8.5): how many of the most
  frequent quality flags the AI receives, each with its rule, its visits and up to three example visits
  (their cards are sent too). Higher gives more nuanced findings and a longer prompt. The visit cards
  (dates, partner, programme document, place, sections, rating with its date, HACT Q1, quality, flags,
  urgency, action point counts; never a narrative, a team or a visit lead) are the 15 most urgent
  visits plus the flags' examples; every other visit reaches it as counts only.
- **Preview** (on each version) shows exactly the instructions each call sends: the brief's (the
  editable text, then the fixed part) and the chat's (the same, then the date and the filter's line),
  and the facts a brief would send (redacted), the notes and visit cards sent of those allowed, the
  input tokens and, when `AI_PRICE_*` is set, the most a brief costs, for the whole country, a section
  or a pasted `/fmm/?…` address. No call is made and nothing is saved. Ask NeuroDB's own prompt is not
  sent to the chat. The fixed part (what the data is and the safety rules, last so that the editable
  text cannot override them) is shown greyed out on every version, with the brief's answer format.
- **Test run** (on each version, from its Preview): writes a brief with that version for the filter
  shown, in the background as Regenerate does, and opens it under *AI briefs*, beside the published
  version's latest brief of the same filter. It counts against the office budget only, never against
  a person's quota, and is never shown on the page. Check its chips (sampling applied or not, tokens
  used) before publishing.
- **AI briefs** (admin, read-only): every brief, with its filter, trigger (nightly, Regenerate, test
  run), status (written, partly written, written by NeuroDB, failed, limit reached, not written) and
  reason, version, model, tokens, sampling, what the checks dropped and the notes and visits sent.
  What was sent is shown to Administrators only.
- **Sampling checks** (*Sampling checks*): reasoning models may refuse temperature or top-p. When the
  API refuses one by name, only that one is dropped and the call is made again (at most twice), and the
  refusal is kept per model and effort for `FMM_SAMPLING_RECHECK_DAYS` (30) days; the brief's chips then
  say "not applied". Deleting a row means "check again on the next call". `FMM_SAMPLING=off` never sends
  either. Ask NeuroDB and NeuroDB Watch never send them. Temperature and top-p are OpenAI only: they
  are never sent to Claude (Claude Opus 5.5 refuses them), and the chips then say "not sent".

The caps and quotas every call checks are under *Costs, limits and quotas*, below.

### AI checks of the quality rules

FMS's narrative rules are **AI checks**, made record by record (`fmm.ai.checks`): R3 (Q2's evidence), R5
(Q1 against Q2), R6 (the narrative against Q1, Q2, the rating and the visit's objective), R7 (Q3 and the
action points), R8 (problems without action points) and R32 (key challenges without action points).

- **One check** is one AI call per record (an entity row of a visit, as FMS checks each record) and
  rule. Its instructions are the rule's prompt (the published prompt version's *AI checks'
  instructions*, under the rule's `ai_prompt_key`, seeded from FMS Lebanon's prompt file; editable and
  versioned with the prompt version) followed by NeuroDB's fixed rules (shown nowhere else, the same for
  every check). Its input is the rule's fields for the record: its entity type, rating, narrative and Q1,
  Q2, Q3 answers as the rule lists them, and the visit's own fields (the record's visit goals and
  objective, else the visit's; the visit's action points with their due dates, repeated on each record),
  each text cut to *AI text characters* (1,500) and cleaned of names, e-mail addresses, phone numbers and
  links. **Never sent**: the visit's label and the entity's name (the rules judge the texts, and records
  with the same texts then ask the same question), the team, the visit lead, any monitor's e-mail address,
  the person responsible, and who an action point is assigned to (only "1 of 2 action points assigned").
  Everything is checked once more before the call; a check that would carry a person is not sent.
- The answer has a strict format: passed or not, and one or two sentences why. The explanation is
  cleaned, and left out when it names a figure the record does not hold or a word NeuroDB never shows
  (the verdict stays). A check that fails adds its flag with the explanation ("R3: Q2 lacks specific or
  disaggregated activity evidence — …") and takes the rule's deduction off the record.
- The model is *AI model* (Score settings; empty: `AI_ASSISTANT_MODEL`, the same as the assistant's),
  at low reasoning effort, with *AI max output tokens* (2,000, the reasoning included) and temperature
  0.30 (OpenAI only, sent only when the model accepts it, as for the briefs; never sent to Claude).
  Nothing is stored at the AI provider.
- **Answers kept** (admin → *AI check answers*): each answer is kept by what was asked (the rule, its
  prompt and the record's inputs, hashed), so records with the same texts, on one visit or several,
  share one answer, and a record is checked again only when its narrative, answers or action points, or
  the rule's prompt, change. An answer no scoring has read for 120 days is deleted by the refresh (not
  while the AI checks are switched off). Deleting one makes the records it served be checked again.
- **Provisional records.** A record whose checks are not all done is provisional (see *Rules of this
  page*): no score in any figure until they are, and neither has its visit.
- **The job** (`fmm_ai_checks`, daily at 05:50 after the refresh and the briefs; *Import and sync runs →
  Run a job → Run the AI checks of the quality rules now*): the records of the scored visits, **this
  calendar year's first, those of visits with several records first, newest first**, each rule due, one
  call for every record whose input is the same, while the day's budget allows; then a scores-only
  refresh. One run at a time. The run says how many checks were made, passed, flagged, shared, already up
  to date, the tokens, why it stopped, and what is left (`records_pending`, `checks_pending`,
  `nights_estimate`: see *Run a job*).
- **Carry-over of the checks made per visit (once, automatic).** Before Release 2 step 5 the checks were
  made per visit. While any is left, each refresh (before it scores) and each AI checks run first carry
  them over, without any AI call, so it happens even while the AI is switched off: for a visit with a
  single record, a verdict still up to date against what the visit sent then becomes that record's answer
  (*carried* in *AI check answers*); a visit with several records inherits nothing (a verdict on merged
  texts cannot be given to one record), so its records are provisional until they are checked, and they
  come first in the job's order. A check is carried over for every AI rule whose instructions the
  published prompt version holds, switched on or not (a rule switched off finds its answers when it is
  switched on again; unused, they go after 120 days like any answer). While no published prompt version
  holds the AI checks' instructions, nothing is carried over or dropped: the checks wait, still counted
  in `legacy_checks_left`, until one does. Each visit check dealt with is deleted, so one pass deals with
  them all; `carried` and `legacy_checks_left` in the AI checks run
  details, and `ai_checks_carried` in the refresh's, say how many. Once the
  checks are up to date, *Score settings → Re-check carried answers* deletes up to 500 carried answers,
  the oldest first, so the next runs check those records properly while the budget allows; press it
  again for the next batch.
- **The clean-up of the change to records.** The visits' own rule results (derived from their records'
  while the pages moved to records) are dropped by the deployment's migration: every page, export and
  look-up reads the records'. The checks made per visit are dropped by the same migration when none is
  left (a new database); on a database that still has some at the deployment, their table is kept for
  the carry-over above and dropped by a migration of a later release, once `legacy_checks_left` reads 0
  in the AI checks run details (developers: check it before shipping that migration). Nothing is run by
  hand.
- **Back-fill after the change to records.** The deployment asks for a full refresh and records a new
  rules version ("Scores per record"), so every visit reads "recomputing with rules vN" until it is
  rescored: the morning refresh (05:25) or *Run a job → Monitoring insights* does it, carrying the
  checks of single-record visits over first, so those visits keep their scores, and scoring the records
  of visits with several records with their AI checks pending; the AI checks (05:50, or *Run a job →
  Monitoring insights (AI checks)*) then check this year's records of visits with several records first,
  and rescore. With the
  defaults this year's records take about one night and the whole history two to three; raise
  `FMM_RULES_DAILY_TOKEN_CAP` for a few days in the App Service configuration to go faster, and watch
  `records_pending` go down in the run details.
- **Costs.** One check uses about 1,500-2,300 tokens (input about 1,100-1,900, output with reasoning at
  most 2,000; a record sends less than a visit of several records did). Six checks per record, once
  (again only when it changes). The checks have their own cap, `FMM_RULES_DAILY_TOKEN_CAP` (2,000,000
  tokens a day, about 900-1,300 checks, counted under *Monitoring insights (AI checks)* in *AI use*), and
  stop at 80% of `AI_DAILY_TOKEN_SOFT_CAP` across every AI feature, so people's questions keep their
  share. When the AI provider says the credit ran out, the AI of Monitoring insights pauses for 6 hours (briefs
  and chat too); 3 failed checks in a row stop the run.
- **Switching off.** *Score settings → AI checks* unticked: the AI rules count as switched off, no check
  is made and no record is provisional (the answers kept are used again when it is back on).
  `FMM_AI=false` does the same for every AI of Monitoring insights. To stop one check only, switch its
  rule off.

### Chat with Data

Under the brief on the Insights tab, staff ask questions about the visits **of the page's filter**
("Which visits were off track and why?"). The AI answers with four look-ups of Monitoring insights
and nothing else: `fm_summary` (counts of visits and records, optionally by section, governorate,
office, partner, month, rating, rule, entity type or status: each group gives its records, their visits
and their average quality per record), `fm_visits` (visit cards, each with its records: entity, type,
rating, score, band, urgency and flags), `fm_visit` (one visit: its records with their notes, scores,
urgency and rule results, the checks of the whole visit, action points, HACT context and checklist
answers) and `fm_search` (visits whose notes or answers hold a word, with a snippet). As on the page, a
visit holds one record per entity assessed, each scored on its own; the look-ups and the tool
descriptions say "records" for those and "visits" for distinct visits.

- **The filter is fixed by the page.** Each look-up runs within the filter the question was asked
  from; the model can narrow it (one section, a governorate, a partner, a period) but never widen it,
  and a visit outside it is not read ("Visit 1722 is not in the current filter"). Changing the filter
  starts a fresh conversation.
- **Its own prompt.** The chat sends the published prompt version's chat instructions, then the fixed
  part, the date and the filter's line; Ask NeuroDB's prompt is not sent (Preview shows exactly what is,
  with the four look-ups as they are sent).
- **Texts.** At most `narr` texts (monitors' notes, checklist answers, search snippets) per answer,
  across all its look-ups, each cleaned of names, e-mail addresses, phone numbers and links and cut to
  `FMM_NARRATIVE_CHARS`; a text naming more than 3 people or contacts is not sent. When the limit is
  reached the look-up says so. The team and the visit lead are never sent. The question itself is
  cleaned before it is sent and kept ("Names, emails and phone numbers are removed from your question
  before it is sent"). A follow-up re-sends the conversation's last 6 questions and checked answers,
  each cleaned again and cut to `FMM_HISTORY_ANSWER_CHARS` (1,500); nothing from one conversation or
  filter reaches another.
- **Last check on every look-up.** Every look-up's result is cleaned once more, keys that hold a
  person are dropped, links stay only to NeuroDB's own pages, and a result that still holds a known
  name, an e-mail address, a phone number or a link is not shown to the model ("This look-up could not
  be shared safely"); the answer goes on without it, the question's checks count it and an error is
  logged.
- **Starter questions**: the published version's list (by default "What are the main programmatic
  issues in this period?", "List the reports that mention supply or stock-out issues.", "Which
  partners or governorates have the most quality concerns?", "Tell me more about the low-quality
  visits and why they scored low."), shown under the chat box; a click asks it.
- **Checked answers.** A link to a visit stays only when a look-up of this conversation returned that
  visit; otherwise it becomes "Visit 1722 (not checked)". Figures that no look-up returned (dates,
  references and small counts aside) are listed under the answer ("These figures could not be checked
  against the data: 444."). The answer is kept only as checked.
- **Limits.** 20 questions a day per person (the published version's `chat_per_user_per_day`; refused
  questions do not count; 429 "You have asked 20 questions today"), at most 2 answers being written per
  person and `FMM_CHAT_MAX_RUNNING` (4) on the whole site ("The chat is busy; please try again in a
  minute."), each answer at most `chat_max_rounds` look-up rounds and `chat_time_limit` seconds, and
  the day's AI budget (above). Each answer holds a web thread while it streams: 4 of the 12 (3 gunicorn
  workers × 4 threads) at most, so pages and Ask NeuroDB always have 8; raising `FMM_CHAT_MAX_RUNNING`
  needs more threads (`GUNICORN_THREADS`) or instances. Ask NeuroDB's hourly limit and its question log
  are not touched: the chat has its own log, **Chat questions** (admin, read-only: status, filter,
  visit references kept and removed, figures not checked, tokens and who asked; the question and the
  answer on a question's own page), kept `FMM_RETENTION_DAYS` (180) days. A sampling parameter the
  model refuses before it writes anything is dropped and the answer starts again (Sampling checks).
- **Switching it off.** Untick *chat enabled* in a new prompt version and publish it ("Chat is not
  available: switched off by an administrator"), or set `FMM_AI=false` for every AI call of Monitoring
  insights.

### Action points (`/action-points/`, FMS §10)

The action points page (menu: *Action points*) lists the **eTools action points** (from the Datamart:
audits, spot checks, TPM, trips and field monitoring), and under them the **NeuroDB action points**,
kept in NeuroDB only. What comes from Monitoring insights (the visit link, the AI review, the PME
verification and the NeuroDB action points) shows while `FMM_ENABLED` is on; with it off the page lists
the eTools action points as before.

- **Filters**: search (reference, description, action taken, partner, PD, status, a person's name, a
  visit's reference or an eTools activity id), status, *Raised from* (module), office, section, partner,
  *Assigned to* (part of a name), *Changed in eTools* from / to (the Datamart's last change), AI verdict,
  PME verification, visit link, and *Show only* overdue, high priority or field monitoring. On the first
  visit a person with a section sees that section's action points (as the other pages); *Reset* comes
  back to it, and clearing the Section filter shows every one. The toolbar's **CSV** copies the rows on
  screen; **CSV of the filter** downloads every action point of the filter (all pages) and **CSV of every
  action point** all of them, with the visit, link confidence, AI verdict and PME verification.
- **Columns**: reference and description, partner and PD, office and section, who it is assigned to
  (shown to staff only; never sent to the AI), due date, status and completion date, *Raised from* with
  the **visit** of a field monitoring action point and its **link confidence**: *High* when the refresh
  matched it by the visit's eTools activity id (`related_module_id`), *Medium* by the visit's reference,
  *Unmatched* when no visit matches; blank for the other modules. Then the **AI verdict** and the **PME
  verification**. The reference opens the action point's details: every field, the action taken, the
  AI's verdict and why, and the verifications (with the form, for who may verify).
- **Charts** (over the filter; each has *Download PNG*, and a bar opens its action points): by status;
  the due dates of the open ones (Overdue, due within 30 days, On track, No due date); raised and
  completed by month (the last 24 months; *raised* is the eTools record's creation date); whether the
  completed ones were completed on time (On time, Late 1-30 days, Late 31-90 days, Late > 90 days, No
  dates) with the average days late of the late ones; and the open ones by office and by section. FMS
  ranks the people with the most open action points ("Top assignees"); NeuroDB never ranks staff by
  name, so offices and sections stand in its place.
- **AI adequacy review** (`fmm.ai.ap_review`): for each **completed** action point (completed, closed or
  resolved) with an action taken written in eTools, one AI call reads the description (the issue)
  and the action taken, cleaned of names, e-mail addresses, phone numbers and links, and answers
  *Adequately addressed*, *Partially addressed*, *Not addressed* or *Generic/vague* with one sentence
  why (cleaned, and left out when it names a figure neither text holds). Its instructions are the
  published prompt version's `ap_adequacy_review` (from FMS Lebanon's prompt file; editable and
  versioned in *AI checks' instructions*), followed by NeuroDB's fixed rules. The model, temperature,
  effort and output limit are the AI checks' (Score settings). Each verdict is kept per action point and
  shown while the description, the action taken and the instructions are those it was made with: a
  changed description makes it out of date at once (it is no longer shown, and the next refresh or
  review deletes it). The review runs every morning at 06:10 (`fmm_ap_review`) on the points not
  reviewed yet, most recently completed first, within `FMM_AP_REVIEW_DAILY_TOKEN_CAP`; Administrators
  also start it from the page (**Run AI review**, batch size 20, 50, 100 or 200, default 50) or from
  *Run a job → Action points (AI review)*. The status line says "Last run: n reviewed, n skipped, n
  errors" (skipped: completed with no action taken, or texts that could not be sent safely). Admin →
  *Action point settings* switches the review off.
- **PME verification**: in the details, *Verified*, *Rejected* or *Pending* with an optional note (never
  sent to the AI); who and when are recorded, and every decision is kept (*Earlier verifications*).
  Administrators and the Section editors of the action point's section (its eTools section, as *Section
  matches* confirms it) may verify. The *PME verification* filter finds each state, or *Not verified yet*.
- **AI content summary** (`fmm.ai.ap_summary`): a button that reads the action points of the filter (at
  most *Points read by a summary*, 150, the most recent; their reference and cleaned description, never
  who they are assigned to) and shows up to 5 dominant themes (name, how many action points, one example
  by its reference) and one sentence on the overall pattern, as a card with *Dismiss*. Checked before it
  is shown: a theme whose example was not sent is left out, and a summary whose counts add up to more
  than the action points read is refused. Each person may ask *Summaries per person per day* (5); the
  summary is not cached (another filter, another summary) and never kept (admin → *AI content
  summaries* records who asked, when and the tokens). Its instructions are the prompt version's
  `ap_content_summary` (NeuroDB's own text).
- **NeuroDB action points** (FMS's "local action points"; admin → *NeuroDB action points*): never sent to
  eTools. Administrators and Section editors add one with **New action point** (title, description, an
  optional visit, priority High / Medium / Low, due date, a responsible role or section, or a person in
  NeuroDB), also from a visit's page; the person who added it, an Administrator or a Section editor (of
  the visit's sections) marks it done, dropped or open again. **NeuroDB makes one at each refresh** for a
  visit with a scored record whose quality is Low (below the Medium band, 50) and that has at least one of
  the AI's action point flags (R7, R8 or R32), one per visit, unless one is open on the visit, or NeuroDB
  already made one since the visit last changed in eTools. It points at the lowest such record (its
  eTools record id is kept): below 30 it is *High* and due in 5 working days, else *Medium* and due in 10
  (Monday to Friday); its title names the flag that took the most points off that record and the record
  ("Follow up on R8 — FM/2026/23 · <entity>"), its description lists every such record of the visit with
  its score and flags (never a narrative or a person) and it is assigned to the role "PME focal point". The list has its own search (title, description, assignee), status, priority and
  programme filters (Health, Nutrition, WASH, Education, Child Protection, Cash, MHPSS, Social Policy,
  SBC, read from keywords in the title and description, offered when some action point matches).
- **Follow-up**: a visit counts as followed up by an eTools action point linked to it, or by a NeuroDB
  action point added by hand (not dropped), or one NeuroDB made that someone marked done (an automatic
  one alone is only a reminder). The follow-up block of the Analysis tab (and its count of open NeuroDB
  action points) and the Watch check `fm_follow_up` read it at once.
- **The visit page** lists its eTools action points with their link confidence, AI verdict and PME
  verification (each opens its details) and its NeuroDB action points, with *New NeuroDB action point on
  this visit*.
- **Ask NeuroDB** counts the action points by status, AI verdict and PME verification, and the NeuroDB
  action points by status (`fm_action_points`): counts only, never a text or a person.
- **Costs.** A review uses about 1,000-2,500 tokens (input about 300-900, output with reasoning at most
  2,000): `FMM_AP_REVIEW_DAILY_TOKEN_CAP` (300,000 tokens a day, counted under *Action points (AI review
  and summaries)* in *AI use*) is about 150-250 reviews a night, and a back-log is reviewed over several
  nights; the nightly review also stops at 80% of `AI_DAILY_TOKEN_SOFT_CAP`. A summary of 150 action
  points uses about 10,000-20,000 tokens and counts against the same cap (and 100% of the shared cap).
  When the AI provider says the credit ran out, the AI of Monitoring insights pauses for 6 hours.

### Exports (FMS §13): Excel, PDF report, Power BI

The page header's **Export** menu exports the filter the page shows (the period, every filter and the
drill-downs; the tab does not matter). It is read-only: every person who can open the page can export,
and nothing is written. Every figure comes from the functions that draw the page (`fmm.metrics`,
`fmm.scope.Scope`), so a file and the page never disagree for the same filter (tests in
`tests/fmm/test_exports.py` compare them).

| In the menu | What you get |
|---|---|
| *CSV (records list)* | The records list's CSV, one row per record (`/fmm/visits/?…&export=csv`); grouped by visit (`&view=visits`), one row per visit, as before. |
| *Excel workbook* | `monitoring-insights-YYYY-MM-DD.xlsx` (`/fmm/export.xlsx`), described below. |
| *PDF report* | A printable A4 report (`/fmm/report/`) that opens the browser's print dialog: choose *Save as PDF*. |
| *Power BI package* | `monitoring-insights-powerbi-YYYY-MM-DD.zip` (`/fmm/export-powerbi.zip`): CSV files and a Power Query script. |
| *Power BI live connection…* | Administrators only: the admin's *Power BI keys* (below). |

**The Excel workbook** has nine sheets, numbers as numbers and dates as dates:

- *About*: the filter in words, *Data as of* (the last refresh), how visits are dated (start date, else
  end date), what a record and a visit are, the visit and record counts and what the columns
  mean (quality score, bands High ≥ 80 and Medium ≥ 50 as set in Score settings, the urgency formula
  and its weights, *Not monitored*), the columns left out and the privacy note.
- *Records* (Release 2 step 5): one row per record (an entity assessed in a visit), as FMS's own export,
  with the column names of FMS's FMM output (§13.2): `id` is `e<record id>` (the eTools finding's id) and
  `visit_id` the visit's id on the *Visits* sheet; `entity`, `entity_type`, `vendor_number` and
  `programme_documents` are the record's own (its partner's full name, its CP output or its PD number),
  and so are `overall_finding_rating`, `quality_score`, `quality_status`, `urgency`, `quality_flags`, the
  category scores, `_ai_used`, the narrative and the HACT Q1-Q3 answers (written on its row, else the
  checklist answer given for that record, for its partner, then for the whole visit); the place is its
  own when eTools gave one (its location, else its P-code), else its visit's; the dates, status,
  modality, sections, field offices, programme areas, governorate, district and the action point
  columns are its visit's, repeated on each of its records as FMS does. `neurodb_url` opens the record
  on its visit's page (`#record-<id>`).
- *Visits*: one row per visit with the column names of FMS's FMM output (§13.2), so FMS's Power BI
  reports and formulas fit: `id` (the visit key), `country_name` (`FMM_COUNTRY_NAME`), the eTools ids,
  dates, partner (`entity`, `vendor_number`), entity types, programme documents, field offices,
  sections and programme areas (several values separated by `;`), `overall_finding_rating` as the page
  counts it (*Not monitored* only for a reported visit with nothing rated; *Not rated yet* for a planned
  or in-progress visit; blank for a cancelled one), status, modality, `quality_score`, `quality_status`
  (High, Medium, Low or Skipped), `urgency`, `quality_flags`, then `lowest_score`, `records` and
  `records_scored` (added in Release 2 step 5), one score per category (`completeness_score`,
  `_evidence_score`…: the points the visit kept of the category's weight), the place (name, latitude,
  longitude, type, P-code, governorate, district), the narratives and the HACT Q1-Q3 answers (cleaned,
  at most 32,000 characters), the action points (count, open, overdue, their descriptions cleaned and
  their due dates, and `action_points_assigned`, a **count**), `_ai_used` (an AI check of the quality
  rules was applied) and `neurodb_url` (absolute when `SITE_URL` is set). Since Release 2 step 5 its
  figures come from its records: `quality_score` is the mean of its scored records, `lowest_score` its
  lowest, `overall_finding_rating` its worst record's, `urgency` its most urgent record's, `quality_flags`
  and the category scores its records' (union, means); the narratives and answers are its records',
  joined. Its columns are those of before, so a report built on it keeps working, at visit grain.
- *Rule results*: one row per record and rule (`record_id`, `visit_id`): passed, flagged, not checked (the
  data is missing or the AI check is pending) or skipped (does not apply, or switched off), the points
  the record lost and whether an AI check gave it; R19, read once per visit, is on each of its records
  as it counts. The detail is given only for rules whose detail NeuroDB writes itself (an AI check's
  explanation may quote a narrative, and the flag of a "text contains" check writes the text it read, so
  both are left out).
- *Partners*, *Field offices*, *Sections*: records and their visits, rated records, On track /
  Constrained / Off track (counts and shares of the rated records), average quality per record and
  bands, visits and records flagged and flags, and the open action points of the visits. A record counts
  under its own partner and in each of its visit's offices and sections, as on the page.
- *Flags*: the records each rule flagged out of those it checked (`flagged_records`,
  `evaluated_records`: the flag frequency chart).
- *Action points*: the field monitoring action points linked to the visits: reference, description
  (cleaned), partner, section, office, priority, due date, status, visits, link confidence, AI verdict and
  PME verification. Never who it is assigned to.

**Privacy** (all exports, the report and the live feed): no file holds the team, the visit lead, a
monitor's e-mail address or who an action point is assigned to (`action_points_assigned_to` is never
written; `action_points_assigned` is a count). Narratives, HACT answers and action point descriptions
are cleaned of the person names NeuroDB knows (read afresh for every file), e-mail addresses, links,
phone numbers and names written after a title, as for the AI (`fmm.privacy.clean`). Large filters are
read 500 records or visits at a time, a fixed number of queries per 500, so a large filter costs time in
proportion to its rows and never one query per row.

**The PDF report** follows the "LCO – FMM Analysis" layout: the period, the filters and *Data as of*;
the key figures and the morning briefing; the AI brief of the filter with its parts (or, without one,
the brief NeuroDB writes from the figures, said so); the overall finding ratings and the quality bands;
ratings by section and by field office; the 15 partners with the most visits; the most frequent flags;
the 10 most urgent visits; HACT programmatic visits; the action points (linked, open, overdue, high
priority, NeuroDB's, visits without follow-up, open by section); and the method. Charts are drawn once at
a width that fits A4 portrait and no block is split across pages. The print dialog opens by itself once
the charts are drawn; *Print or save as PDF* opens it again.

**The Power BI package** holds `data/records.csv`, `data/visits.csv`, `data/rule_results.csv`,
`data/action_points.csv` and `data/partners.csv` (the workbook's columns; UTF-8 with a byte-order mark,
ISO dates, `.` decimals), `NeuroDB_monitoring.pq` (a Power Query script that loads the five files with
their types, the records first, and splits the records' `;` columns into `record_sections`,
`record_offices` and `record_flags`) and `README.txt` (the steps below, FMS §13.3's visuals on the records
table, and a "Changed in this release: records" note). **Since Release 2 step 5** `records.csv` is the
table that gives FMS's figures (one row per record, as FMS's export); `visits.csv` keeps its columns (plus
`lowest_score`, `records` and `records_scored`) at visit grain, so a report built on it, and its own
script, keep refreshing; `rule_results.csv` is one row per record and rule, with `record_id` (its
`visit_id` stays); `partners.csv` gains `records` and `flagged_records`. Anyone who wants FMS's figures
switches the report to `records`. There is no `.pbit` template: a template cannot be built or tested
without Power BI, and the script stands in its place. In Power BI Desktop:

1. Unzip the package into a folder, keeping its `data` folder (for example
   `C:\NeuroDB\monitoring-insights\`).
2. *Get data → Blank query*, then *Advanced editor*; paste the whole of `NeuroDB_monitoring.pq`.
3. Set `RootFolder` in its first lines to that folder (ending with `\`) and click *Done*.
4. The query shows eight tables: right-click each → *Add as new query*, then switch off loading of the
   first query; *Close & Apply*. Join `visits[id]` to `records[visit_id]`, and `records[id]` to the
   `record_id` of `rule_results`, `record_sections`, `record_offices` and `record_flags` (cross filter
   *Both* for the last three, so their slicers filter the records).
5. *File → Save as* `.pbix`. To refresh: download a new package, unzip it into the same folder, *Refresh*.

**Power BI live connection** (FMS "Connect Live", for scheduled refresh in Power BI Service). The feed
`/powerbi/fmm/<table>.csv` (`records`, `visits`, `rule_results`, `action_points`, `partners`) serves the
same tables over **every** visit (no person's section is applied), narrowed by `?year=2026` or
`?since=2026-01-01` (visits dated from that day: their start date, else their end date; a record follows
its visit). A report connected before Release 2 step 5 keeps reading `visits.csv` with its own script.
It is read with a key, never with a sign-in:

1. Admin → *Monitoring insights* → *Power BI keys* → *Add*: give the key a name (what it is for, e.g. the
   workspace) and save. The next page shows the key **once** (NeuroDB keeps only its SHA-256 hash and its
   first eight characters) and the ready-to-paste Power Query script with this site's address. Copy both.
2. Power BI Desktop: *Get data → Blank query → Advanced editor*, paste the script, *Done*. When asked how
   to connect, choose **Web API** and paste the key. Add the eight tables as queries as above.
3. Publish to the workspace; in the dataset's settings enter the key again under *Data source
   credentials* (Web API), then switch on *Scheduled refresh*.

The script sends the key with `Web.Contents(…, [ApiKeyName = "key"])`, which adds `?key=` to each request
(the only way a scheduled refresh in Power BI Service sends a key); other tools may send
`Authorization: Bearer <key>` instead. With no key at all (none created, or all revoked) the feed answers
404; a missing, wrong or revoked key gets 401; each key may make `FMM_POWERBI_REQUESTS_PER_HOUR` (120)
requests an hour, then 429 with `Retry-After` (the hour's count is kept on the key in the database, so
the limit holds across every worker and container). Every answer is `Cache-Control: no-store`. Each use
updates the key's *Last used* and *Uses* in the admin; refused requests are logged without the key. The
web server's access log writes the address with `key=[hidden]`, and the feed's addresses are left out of
the Application Insights request traces (added to `OTEL_PYTHON_DJANGO_EXCLUDED_URLS` at start-up), so the
key sent in the address is kept nowhere. A proxy or gateway placed in front of NeuroDB may log addresses
too: leave `/powerbi/fmm/` out of its logs. These addresses alone are left out of the sign-in and of the
donor lock-down; a signed-in person without a key gets nothing from them.

**Key rotation.** Create a new key, put it in Power BI (Desktop: *File → Options and settings → Data
source settings → Edit permissions*; Service: the dataset's *Data source credentials*), check that a
refresh works, then open the old key in the admin and **Revoke this key** (or select keys in the list and
*Revoke the selected keys*). Revoking works at once. Revoked keys stay listed with their last use; they
are never deleted. A key that may have been seen by someone else is revoked at once, and Power BI given a
new one.

**Action points** (`/action-points/`, D1.6): next to the CSV downloads, *Excel of the filter* and *Excel
of every action point* (`?export=xlsx`, `&all=1`) hold the CSV's columns, with the description and the
action taken cleaned as above; both write `assigned_to_count` (1 when the action point is assigned, else 0),
never to whom, and the CSV cleans the description and the action taken as the Excel file does. *PDF report*
(`/action-points/report/?<filter>`) prints the filter in words (a name typed in *Assigned to* is not
repeated), the key figures, the six charts, the action points by module and, with Monitoring insights,
the AI verdicts.

### What goes to the AI provider, and what never does

Nothing goes while `FMM_AI` is off. Once it is on, the brief and the chat send:

| Sent to the AI provider (briefs and chat) | Never sent |
|---|---|
| The period and filter (dates, section, governorate and office names) | The visit lead, team members and monitors (names or e-mail addresses), from any source |
| Figures: visits and records, rated and not monitored, rating counts, quality averages per record and bands, rule results (records flagged), shares worked out by NeuroDB | E-mail addresses, phone numbers and links, also removed from inside every text |
| Up to `comp` quality flags (rule, reason, records and visits); the visits of the 15 most urgent records and the flags' example visits as cards (dates, partner, PD reference, place, sections, rating with its date, HACT Q1, quality: the mean of its records, flags, urgency: its most urgent record's, action point counts); in the chat, a visit's records (entity, type, rating, score, urgency, flags, rule results) | Action point assignees, PD focal points, partner staff, eTools user ids |
| Up to `narr` texts per brief or per chat answer (monitors' notes, checklist answers, search snippets), each cleaned and cut to `FMM_NARRATIVE_CHARS` (600) | The finding records as eTools holds them, raw answers, attachments, coordinates (only place names go) |
| A keyed hash of the person (OpenAI's `safety_identifier`, Claude's `metadata.user_id`) on runs a person starts; `store=False` always on OpenAI (Claude keeps no conversation either) | Child-level or Makani data; the user's name, e-mail address or id |
| Within one chat conversation only: its last 6 questions and checked answers, cleaned again, at most 1,500 characters each | Earlier briefs; answers of other conversations or other filters |

- **Cleaning** (`neurodb/fmm/privacy.py: clean`) removes e-mail addresses, links, the names NeuroDB
  knows (users, partner and eTools staff, and the field monitoring team names of every visit), phone
  numbers (Lebanese and international) and names written after a title (Mrs, Dr, Sheikh...). A text
  with more than 3 of them removed, or under 40 characters, is never sent. Texts are cleaned when they
  are sent, with the names known then; cleaned texts are kept only in the brief's *What was sent* (30
  days), the briefs and the chat log (180 days).
- **A last check** runs on the whole brief before it is sent and on every chat look-up before the
  model reads it: a known name, an e-mail address, a phone number or a link that slipped through stops
  the brief ("could not be written safely") or withholds the look-up ("This look-up could not be
  shared safely"), and an error is logged without the text.
- **The user's own chat question** is cleaned before it is sent and kept.
- **Ask NeuroDB** reads field monitoring without these limits on texts per answer, so it gets none:
  its `fm_*` look-ups return structured fields only (no notes, answers or snippets), and its generic
  eTools look-ups drop person keys and withhold long texts (see *AI assistant*, above).
- **The action points' AI** (review and summary) sends only an action point's description and action
  taken (the review), or the references and descriptions of the action points of the filter (the
  summary), each cleaned and checked like the briefs': never who an action point is assigned to, its
  office or partner, or a PME note.
- **The hub, Watch and the CSV** carry no narrative, answer, visit lead or team: the hub's visits hold
  their date, status, rating, quality and urgency band; Watch's records the visit's label, date and
  rating; the CSV every column but the team.
- An end-to-end test plants names, e-mail addresses, phone numbers and a link wherever eTools could
  write them and checks that none comes out of the brief, the chat, Ask, the hub, the CSV, Watch, the
  log or any table of Monitoring insights (`tests/fmm/test_canary.py`).

**Residual risk.** The name of a beneficiary or of anyone on no NeuroDB list can survive in a monitor's
note. `narr = 0` (a new prompt version) stops every note, answer and snippet; `FMM_AI=false` stops
every call.

### Costs, limits and quotas

Every brief, test run and chat round is counted under *Monitoring insights* in Admin → Data and sync →
*AI use*; the AI checks of the quality rules under *Monitoring insights (AI checks)*, with their own cap
(see *AI checks of the quality rules*); the action points' AI review and summaries under *Action points
(AI review and summaries)*, with their own cap (see *Action points*). Before each call NeuroDB checks, in order: the AI is switched on (`FMM_ENABLED`, `FMM_AI`,
`AI_ASSISTANT_ENABLED` and a published prompt version); it is not paused (6 hours after the AI provider
said the credit ran out, unless the other provider is in use meanwhile); Monitoring insights' own caps for the whole office, `FMM_DAILY_TOKEN_CAP`
(1,200,000 tokens) and `FMM_MAX_CALLS_PER_DAY` (400 calls); and the shared `AI_DAILY_TOKEN_SOFT_CAP`
(3,000,000), of which nightly briefs and test runs may use 80% and people's Regenerates and questions
100%. Per person: 5 *Regenerate* and 20 chat questions a day (set in the prompt version), counted from
local midnight (Beirut). A brief found up to date costs nothing and uses no quota; a test run counts
against the office caps only; a refused question does not count.

**Sizing: the office caps, not the per-person quotas, are what people meet first.**

| Run | Model calls | Tokens (input with the cached part, and output with reasoning) |
|---|---|---|
| One brief (nightly, Regenerate or test run) | 1 | about 12,000-28,000 (facts 8,000-16,000, output at most 8,000 with v2) |
| One chat answer | 1-4 rounds | about 15,000-35,000 (prompt about 1,500 and history at most 3,000 sent again each round, look-ups at most about 6,000, output at most 6,000 a round) |

With the defaults the nightly run uses about 200,000 tokens (12 briefs), which leaves about 1,000,000:
**about 30-40 chat answers and 10 Regenerates a day for the whole office**. Each extra chat answer a day
needs about 30,000 tokens more of `FMM_DAILY_TOKEN_CAP`, and `AI_DAILY_TOKEN_SOFT_CAP` (shared by every
AI feature) must be raised with it. The quota pill says both: "3 of 20 today · office AI budget 62%
used"; when the office cap is reached, the chat and *Regenerate* say "Today's AI budget for Monitoring
insights is used; it resets at midnight."

**Capacity.** The web container runs 3 workers × 4 threads = 12 request threads, shared by every page,
Ask NeuroDB and Monitoring insights. A brief never runs in a web thread (Regenerate and Test run start
it in its own process, and the page polls every 3 seconds). A chat answer holds a thread while it
streams, at most `chat_time_limit` (120 s), with at most `FMM_CHAT_MAX_RUNNING` (4) at once on the
site and 2 per person, so 8 threads always remain. Raising `FMM_CHAT_MAX_RUNNING` needs more threads
(`GUNICORN_THREADS`) or instances.

### Prompt versions and rules versions

Both are edited by Administrators only; other staff can read them. Neither is ever changed in place.

- **Prompt versions** (admin → *Prompt versions*). A version holds the editable instructions of the
  brief and of the chat, the parts of the brief, the chat's starter questions, the model, effort,
  output limits (they include the reasoning tokens), temperature and top-p, `narr`, `comp` and the
  per-person limits. *Add* starts a **draft** from the
  published version, with a note saying what changes and why. *Preview* shows exactly what a call
  sends (the instructions, the facts, the counts, the estimated tokens and cost) without a call.
  *Test run* writes a brief with the draft in the background, beside the published version's latest
  brief, and never shows it on the page. *Publish* retires the published version. Published and retired
  versions never change: *Roll back to this version* publishes a new copy, so the history only grows.
  Only drafts can be deleted (their test runs with them).
- **The chips are honest.** Each brief and chat answer keeps the version it ran with and what was
  really used: "temp 0.30 · not applied" when the model refused temperature (*Sampling checks*),
  "tokens 8000 · used 3,212", "narr 14/20", "comp 9/15" (quality flags sent of those allowed), "prompt
  v7", "rules v4".
- **`narr`** is the most texts (notes, answers, snippets) the AI may read per brief and per chat answer;
  `narr = 0` sends none. **`comp`** is the compliance depth: how many of the most frequent quality flags
  it receives.
- **Rules versions** (admin → *Rule versions*). Every save of a rule, of the score settings, of a
  question pattern, of a pinned key or a Rebalance needs a note and records a new rules version with who, when and
  the whole settings. *Restore this version* writes an older version back as a new one. Each save asks
  for a rescore in the background; every visit and brief keeps the rules version it was computed with,
  and the page says "recomputing with rules v8" until the rescore is done.

### Switching it off, in steps

| To stop | Do |
|---|---|
| Monitors' notes, answers and snippets reaching the AI | A new prompt version with `narr` = 0, published |
| The AI briefs | A new prompt version with *insights enabled* unticked, published (the brief written by NeuroDB is shown) |
| The chat | A new prompt version with *chat enabled* unticked, published |
| The AI checks of the quality rules | *Score settings → AI checks* unticked (the AI rules then count as switched off; no visit is provisional), or one rule switched off |
| The AI review of action points | Admin → *Action point settings* → *AI review* unticked (the verdicts kept stay hidden while out of date) |
| The AI content summary of action points | A new prompt version without `ap_content_summary` in *AI checks' instructions*, published (the button goes) |
| Every AI call of Monitoring insights | `FMM_AI=false` (no restart of the data or pages needed beyond the setting) |
| The refresh after each Datamart sync | `FMM_REFRESH_AFTER_SYNC=off` (or `false`); the 05:25 run still refreshes |
| Everything | `FMM_ENABLED=false`: the page and every `/fmm/` address answer 404, the menu item and panels are hidden, the refresh and the briefs do nothing, the look-ups say Monitoring insights is off (switch off the Watch check `fm_follow_up` too) |

### Speed and size

A test builds a production-size world (5,000 visits of 15,000 finding rows, 100,000 checklist answer
records) and holds these limits (`tests/fmm/test_performance.py`): the full refresh under 60 seconds
with a peak traced memory under 200 MB (it runs inside the Datamart sync's process), a scores-only
refresh under 15 seconds, and each tab under 300 ms of server time on a cold cache. On the build
machine it measured 28-33 s and 92-94 MB for the full refresh, 6-9 s for scores-only, and 40-230 ms
per tab (the Quality and Analysis tabs are the slowest); with Release 2 (the briefing, the rule score
trends, which load when their panel scrolls into view) 36-39 s, 95 MB and 8 s, and 60-280 ms per tab
(Quality 254 ms, Analysis 229 ms; Release 1 measured 282 and 234 ms the same day); the entity table, which loads when it
scrolls into view, about 210 ms more, and a place list's *Show all* about 40 ms. With scores per
record (Release 2 step 5: 15,000 records, some 180,000 record rule results) the same test measured
41-47 s and 126 MB for the full refresh and 12-14 s for scores-only, against 43-47 s, 129 MB and 10 s
just before on the same machine, with the tabs as before; the visits and their records go through
COPY, and a scores-only pass rewrites only the rule results that changed. Counting records made the
tabs slower (Quality about 410 ms, Analysis 450, the AI brief card 680); with the figures kept per visit
(`fmm.tallies`, see *the refresh* above) and covering indexes they measured, side by side on a shared
machine, Insights 76 ms, Quality 207, Analysis 239, Visits 131, Map 257 and the brief card 341, against
104, 282, 328, 155, 297 and 466 before records were counted; the full refresh about 1 s more (38-42 s),
scores-only 10 s. A test (`tests/fmm/test_records_figures_equal.py`) holds every figure of the page,
the map and the brief to those of the plain per-record computation, for some 40 filters, read both
ways. The *Preview effect* of a rule change rescores the year's visits twice in memory inside the admin
request: about 9-10 s at that size, 16-18 s since each record is scored. If the
refresh's memory ever grows past what the sync's process can afford, set
`FMM_REFRESH_AFTER_SYNC=background`.

### Before go-live: confirm the real keys

The checklist answer, option and programme activity records of the demo and of the tests are
invented (shapes A to D of the specification): no production sample could be read when this was
built, so the check of the real keys moved to go-live. Before the Monitoring insights AI is switched
on, and before its figures are trusted:

1. After a nightly Datamart sync in production (the refresh runs after it; *Refresh now* on Fields
   found runs it at once), read Fields found: the activity id coverage; the
   keys chosen for the activity id and reference, the question id and text, the answer, its label and
   summary, the entity and its type and `is_hact`; whether unanswered questions are exported; the Q1,
   Q2, Q3 and PSEA question texts as written; the option labels of Q1; the rating and status values;
   any team, office or section key; the share of checklist records that matched a visit; and whether
   the FM action points' `related_module_id` is the activity id (the share matched by the activity
   id). In Questions found, check that Q1, Q2, Q3 and the PSEA question have their roles (else give
   them with *Use as*), and that "Unanswered questions seen" is not 0 (else R2 cannot be measured).
2. Press *Download samples (redacted)* on Fields found: a ZIP of a few stored records per dataset,
   with people replaced by "Person N" and contact details removed. Read it, then hand it to the
   developers.
3. The developers put the files in `tests/fixtures/datamart/`, put the real key names first in
   `CANDIDATES` (`neurodb/fmm/fields.py`), make the demo's shape mirror the real one, and add the
   recorded shape to the tests (the invented shapes stay as tolerance tests).

### Go-live checklist

1. **Deploy with `FMM_AI=false`.** Let one nightly eTools Datamart sync and the refresh after it run
   (or press *Refresh now* on *Fields found*, or *Run a job → Monitoring insights* in *Import and sync
   runs*, once the sync has finished). **After deploying Release 2**, run that full refresh at once
   (*Run a job → Monitoring insights*): until it runs, the visits keep Release 1's urgency (the
   migration only converts the Score settings' weights to FMS's), have no modality, programme areas or
   row answers, and statuses outside the scored statuses still show their old score. The migration
   also publishes prompt v2 (the brief's five parts from FMS's Lebanon prompt); v1 is retired.
2. **Confirm the real keys in Fields found** (the steps of *Before go-live: confirm the real keys*,
   above: this was not possible while Monitoring insights was built, so it is a go-live step):
   - the activity id coverage (`monitoring_activity_id`) is 95% or more; otherwise visits are told
     apart by their reference, which is right but changes how they are cited;
   - each key chosen is the right one (state *Found*, no *Set key not in the data*); pin any that is
     not (the pin is a rules version, with a note);
   - the Q1, Q2, Q3 and PSEA patterns match the live question texts in Questions found;
   - "Unanswered questions seen" is not 0; if it is, R2 shows "cannot be measured" and the user is told;
   - the eTools FMM export's names are the keys chosen (`hact_q1_answer`, `field_offices`,
     `sections_names`, `monitoring_modality`, `location_lat`/`location_lon`...); the refresh's details
     show how many visits were placed by written coordinates ("written") and by P-code.
3. **Read the refresh's details** (its line in *Import and sync runs*): PD-kind rows linked to their
   programme document (aim for 90% or more), the governorate link rate, how the FM action points were
   matched (decide whether `related_module_id` is the activity id), and any status or rating eTools
   writes that NeuroDB does not recognise (extend the vocabularies in `neurodb/datamart/fm.py`).
4. **Compare the figures**: Monitoring insights this year against the overview and `/field-monitoring/`
   (equal, or different only by the data notes), then against the eTools field monitoring dashboard for
   one month.
5. **Pass on the real samples**: *Download samples (redacted)* on Fields found (step 2 of *Before
   go-live*), read the ZIP, and hand it to the developers, who commit it, put the real keys first in
   `CANDIDATES` and run `tests/integrations/test_recorded_samples.py`. Download it again whenever
   Fields found later shows a key change.
6. **Section matches**: confirm the field monitoring section spellings in *eTools section names*
   (*Match again* there after a NeuroDB section was added or renamed).
7. **Switch the AI on**, as the user chose (by default the user's decision that redacted notes, at most
   `narr` per run, may go to the AI is the clearance; the user may also ask for a written OK from the
   data protection focal point first). Open *Preview* on the published prompt version (the exact
   instructions and the redacted facts), make a *Test run*, check its chips (sampling applied or not,
   tokens used), then set `FMM_AI=true` and leave the `fmm-insights` schedule on.
8. **Confirm with the user** the points left open when this was built: R23 at 0 points (a flag only);
   `comp` as the compliance depth (the quality flags the AI receives); the Team column (names only); this calendar year as the
   default period; how the AI is switched on (step 7); every threshold that is NeuroDB's proposal (bands
   80/50, red 70, amber 40, 14 days for follow-up, 30 days for a late report, the PSEA answers that
   flag); the brief's 8,000-token limit in v2 (the reference showed
   1,500); temperature 0.30 with top-p not set; and the office AI budget (about 30-40 chat answers and 10
   Regenerates a day). Release 2's choices to confirm: the scored statuses (report finalization and
   completed), FMS's urgency weights 0.50 / 0.30 / 0.20 over 180 recency days, the precision rule of
   written coordinates (lowest level, or more than 100 m from the gazetteer's point), the priority
   levels High and Medium of the action points, and v2's wording adapted from FMS's Lebanon prompt.
   Stage B's: FMS Lebanon's rule set as seeded (R23 flags at 0 points; a field NeuroDB cannot read is
   not counted missing; a provisional visit counts as not scored), the eTools-derived reference data of
   R20, R21 and R23 (FMS's hand lists kept as additions), and the AI checks' model, budget and schedule.
9. **Fill the field office staff lists** (admin → *Field office staff lists*: Beirut, Zahle, Tripoli and
   Beirut/Mount Lebanon are there, empty) when R19 should run; it skips until a list has addresses.
10. **Switch the AI checks on** with the AI (Score settings → *AI checks* is on by default): the first
   nights check this year's records within `FMM_RULES_DAILY_TOKEN_CAP`, and the older ones over the next
   nights; until a record's checks are done it is provisional, and so is its visit. *Run the AI checks of the quality rules
   now* (Import and sync runs → Run a job) starts a run at once.
11. **Action points (Release 2, stage C).** The migration publishes a new prompt version with the
   review's and the summary's instructions (the one before it is retired). With the AI on, the 06:10
   review back-fills the completed action points within `FMM_AP_REVIEW_DAILY_TOKEN_CAP`; check the
   first run's status line on the action points page and a few verdicts against the action taken. Read
   which keys the action points' records hold for the action taken and the creation date (`action_taken`
   or `actions_taken`; `created`): a key NeuroDB does not know leaves the review and the monthly trend
   empty. Confirm with the user: the follow-up rule (an automatic NeuroDB action point counts once marked
   done), the role "PME focal point" of the automatic ones, offices and sections in place of FMS's top
   assignees, and the 300,000-token cap.

### Settings

| Setting | Default | What it does |
|---|---|---|
| `FMM_ENABLED` | `true` | Monitoring insights. Off: its page and every address under `/fmm/` answer 404, the menu item is hidden, and its refresh does nothing. |
| `FMM_REFRESH_AFTER_SYNC` | `inline` | At the end of every eTools Datamart sync: `inline` runs the refresh in the sync's process, `background` starts it as its own process, `off` does not run it (the 05:25 run still does; `false` also means `off`). Another value stops the start-up. |
| `FMM_REFRESH_MAX_PASSES` | `3` | Passes one refresh may make to serve the rescores asked for while it runs. |
| `FMM_KEY_MIN_COVERAGE` | `0.5` | The share of a dataset's records a candidate key must fill to be chosen before the keys listed after it (above 0, at most 1; another value stops the start-up). |
| `FMM_ETOOLS_ACTIVITY_URL` | (blank) | The address of an activity in eTools, with `{id}` for its id (e.g. `https://etools.unicef.org/fm/activities/{id}/details`), for the visit page's *Open in eTools*. Blank hides the link until the address is verified. |
| `FMM_MATCH_KM` | `2.0` | Map: the distance in kilometres below which a visit and a planned place of its programme document count as the same place, when both points are exact (a monitoring site, or a cadaster's own point). Above 0; another value stops the start-up. |
| `FMM_HUB_MONTHS` | `24` | The visits dated (start, else end) within this many months are in the knowledge hub. |
| `FMM_NEWS_DAYS` | `30` | A visit rated off track or constrained that comes into the knowledge hub is news in What's new only when it is dated (start, else end) within this many days. |
| `FMM_AI` | `false` | The AI brief and chat. They also need `AI_ASSISTANT_ENABLED` and a published prompt version. Off at deploy; switched on at go-live once the real keys are confirmed (above) and a Preview and a Test run look right. |
| `FMM_MODEL` | (blank) | The model of a prompt version that names none; blank: `AI_ASSISTANT_MODEL`. A model of the other AI provider is not used: the provider's own model runs instead (AI settings shows which). |
| `FMM_SAMPLING` | `auto` | `auto`: send temperature/top-p when a version sets them and the model has not refused them; `off`: never send them. OpenAI only: they are never sent to Claude. Another value stops the start-up. |
| `FMM_SAMPLING_RECHECK_DAYS` | `30` | Days after which a refused temperature or top-p is tried again. |
| `FMM_DAILY_TOKEN_CAP` | `1200000` | Monitoring insights' own tokens a day for the whole office (briefs, test runs and chat): about 30-40 chat answers and 10 Regenerates a day plus the nightly briefs. Raise `AI_DAILY_TOKEN_SOFT_CAP` with it. |
| `FMM_MAX_CALLS_PER_DAY` | `400` | Monitoring insights' own model calls a day (brief calls plus chat rounds). |
| `FMM_CHAT_MAX_RUNNING` | `4` | Chat answers streaming at once on the whole site. Raising it needs more web threads (`GUNICORN_THREADS`) or instances. |
| `FMM_HISTORY_ANSWER_CHARS` | `1500` | Characters of each earlier chat answer re-sent with a follow-up question. |
| `FMM_NIGHTLY_MAX_INSIGHTS` | `12` | Briefs written per night at most. |
| `FMM_NIGHTLY_MIN_VISITS` | `3` | A section gets a nightly brief from this many visits this year. |
| `FMM_MIN_VISITS_FOR_AI` | `3` | No AI call for a filter with fewer visits. |
| `FMM_NARRATIVE_CHARS` | `600` | Characters per text sent to the AI (narrative, answer, snippet). |
| `FMM_INSIGHTS_TIMEOUT_SECONDS` | `90` | Seconds per brief call. |
| `FMM_PAYLOAD_RETENTION_DAYS` | `30` | After this many days a brief's sent payload is blanked. |
| `FMM_RETENTION_DAYS` | `180` | Briefs and chat questions are kept this many days. |
| `FMM_RULES_DAILY_TOKEN_CAP` | `2000000` | The AI checks' own tokens a day (about 900-1,300 checks of one record each); what is left waits for the next night (raise it for a few days to back-fill faster). They also stop at 80% of `AI_DAILY_TOKEN_SOFT_CAP` across every feature. |
| `FMM_RULES_TIMEOUT_SECONDS` | `60` | Seconds per AI check. |
| `FMM_AP_REVIEW_DAILY_TOKEN_CAP` | `300000` | The action points' AI review and summaries: their own tokens a day (about 150-250 reviews); the nightly review also stops at 80% of `AI_DAILY_TOKEN_SOFT_CAP`. |
| `FMM_AP_REVIEW_TIMEOUT_SECONDS` | `60` | Seconds per action point review or summary call. |
| `FMM_COUNTRY_NAME` | `Lebanon` | The `country_name` column of the Excel workbook, the Power BI package and the live feed (FMS §13.2). |
| `FMM_POWERBI_REQUESTS_PER_HOUR` | `120` | Requests one Power BI key may make to the live feed an hour; then 429 with `Retry-After`. |

Release 2's other settings are kept in the admin, versioned with the rules (*Quality rules*, *Score
settings*) or with the prompts (*Prompt versions*):

| Admin setting | Default | What it does |
|---|---|---|
| Score settings › *Scored statuses* | report finalization, completed | The eTools statuses whose visits are scored; every other visit is pending (no score, no urgency). At least one; cancelled cannot be chosen. |
| Score settings › *Urgency weights* | `{"quality_gap": 0.5, "recency": 0.3, "red_flags": 0.2}` | FMS's urgency formula; each from 0 to 1, adding up to 1. |
| Score settings › *Recency days* | `180` | Days over which the recency part falls from 100 to 0 (1 to 3,650). |
| Score settings › *Urgency red / amber* | `70` / `40` | High urgency from red; Medium (amber) from amber. |
| Score settings › *Follow-up days*, *Report late days* | `14`, `30` | The follow-up signals on the visit page (no longer part of urgency). |
| Score settings › *Score categories* | FMS Lebanon's six: completeness 30, evidence 20, alignment 20, coherence 15, Q3 quality 10, actionability 5 | Each rule's category and its weight: a category loses at most its weight. The weights add up to 100 (Rebalance). |
| Score settings › *AI checks* | on | The AI checks of the narrative rules; off: those rules count as switched off. |
| Score settings › *AI model* | (blank: `AI_ASSISTANT_MODEL`) | The model of the AI checks. A model of the other AI provider is not used: the provider's own model runs instead (AI settings shows which). |
| Score settings › *AI max output tokens* | `2000` | Per check, the reasoning included (500-16,000). |
| Score settings › *AI temperature* | `0.30` | Sent only when the model accepts it; blank: not sent. |
| Score settings › *AI text characters* | `1500` | Each text a check sends is cut to this (200-6,000). |
| Quality rules › each rule | FMS Lebanon's 32 rules | On or off, category, group, deduction, flag template and parameters (see *Rules of this page*). |
| Field office staff lists | Beirut, Zahle, Tripoli, Beirut/Mount Lebanon, empty | Rule R19's staff e-mail addresses per field office (Administrators only; never shown or sent). |
| Prompt version › *AI checks' instructions* | FMS Lebanon's six check prompts (v3), and the action points' `ap_adequacy_review` and `ap_content_summary` (v4) | The instructions of each AI check, by prompt key, and of the action points' AI review and content summary. |
| Action point settings › *AI review* | on | The AI review of completed action points; off: it reviews nothing. |
| Action point settings › *Summary points* | `150` | The most action points one AI content summary reads (10-500). |
| Action point settings › *Summaries per person per day* | `5` | AI content summaries one person may ask for a day (0-50). |
| Prompt version › *Parts of the brief* | FMS's five Lebanon parts (v2) | Key, label, paragraph or bullets, and the limit (the most sentences or bullets, 1-30) of each part, at most 8 parts; the brief's answer format is built from it. |
| Prompt version › *Chat starter questions* | the four FMS questions | One per line, at most 8, each at most 200 characters. |
| Prompt version › *Compliance depth* (`comp`) | `15` | How many of the most frequent quality flags the AI receives (0-40). |

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

**The page answers at once and fetches its figures** (see *Pages that load in parts*). The bar, the tabs,
the headings and the cards come first, each card grey and of about the size of its chart; the page's
script (`neurodb/web/static/js/donor.js`) then asks for the figures (the page's address with the year,
plus `_part=data`) and draws them. They are computed once for every worker and kept 5 minutes in the
shared page figures (before, each worker kept its own copy), so a page opened again within that time
carries them and asks for nothing. While they are computed, "Loading the figures…" shows at the top of
the page. If they cannot be loaded (a server error, or no answer within 60 seconds), an error box says
so with a **Try again** button; a network error or a server restarting is retried once by itself, and a
"still computing" answer (202) is asked again until the figures come. If the page's script itself could
not run (it failed to download or was blocked), the grey cards and "Loading the figures…" go once the
page has loaded, and the page says its charts are drawn in the browser by a script that did not run, as
it does without JavaScript. A year without funds shows its "No funding found" card once the figures say
so. The figures' address names the year only; an administrator's preview adds the account and the tab.
A donor always receives the donor's own figures, whatever account the address names, and a donor whose
temporary password is still to change gets 403.
In the log, the figures' request is `"GET /donor/?year=2026&_part=data HTTP/1.1"`.

**How the donor's figures are counted.** The donor's money is the funds reservation lines that name
one of the account's donors (and grants, when the account lists some) on FRs running in the year.
Disbursed is the FR's disbursed amount times the donor's share of that FR's lines. A programme's
children (its main indicator: its unique beneficiaries indicator, else its largest children
indicator, never several indicators added; see the overview's headline rule) are attributed in
proportion to the donor's share of the programme's funds; its progress to target is that indicator's
Reached against its target. The programme table names the main indicator and how it was chosen. A
programme with children reached belongs to the programme area of its main indicator, as on the
overview, so its cost per child is set against the same area's country figure. The page explains
this to the donor under "How to read these figures".

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

**Which registrations: Power BI's view** (Compiler's payload format 2). Power BI's Youth dashboard reads
the view `vw_youth_data_all` of Compiler's database. From format 2 Compiler counts the registrations of that
view (the year from the view's own year column when it has one, else the registration's), or, when the view
cannot be used (missing, no permission, its key not matching the registrations, empty), its own rule, and
says which: the line under the key figures (*Counted from Power BI's view `vw_youth_data_all`...*, or
*Power BI's view could not be used (reason)...*, or *Not yet compared with Power BI's view* for a payload
of an older Compiler) and Data health's *Compared with Power BI* (see *Education programmes*). Format 2 also
sends every counted registration with or without an enrolment (*registered* young people, by partner and
the youth's home address, sex, age group and nationality) and the enrolment rows (a young person enrolled
twice counts twice), and the figures of **all years together**: registrations saved before September 2025
have no year and are in no single year, but they are in *All years*. NeuroDB's *Young people reached* keeps
its meaning (young people enrolled under a youth indicator); Power BI's own measure is not known (no
screenshot of the Youth dashboard exists), so the headline and the *Partners* tile are counted with the
version an administrator chooses in Admin → *Power BI cards* (enrolled, registered, registrations or
enrolments; a blank partner counted or not), today's meaning by default.

**The page** (Education and youth → Youth programmes; filters: year, with *All years* when Compiler sent it,
master indicator, partner, donor, governorate): young people reached (with the registered young people
as a second line, payload format 2; the version chosen in Admin → *Power BI cards*), female and male; each master and sub indicator with young people
reached, the target (sum of the Compiler programme documents' targets; hidden under a governorate
filter, targets are not set per place, and for all years together) and the linked eTools indicator with its Reached to date and
its status; breakdowns by partner, donor, governorate, district, sex, age group and nationality, and
the registered young people by home governorate (format 2; with the partner filter only);
**Compiler and eTools side by side** (per link: young people Compiler counted in the programme
document, the eTools target, what the partner reached on the eTools indicator **in the same year**
(Partner monitoring's Reached in that year, never the PD to date), the difference); and the Compiler
programme documents.

**Loads in parts** (see *Pages that load in parts*). The page answers its frame at once: the filter bar,
whole (its choices are cut out of the year's figures in the database, about 70 ms the first time, then kept
10 minutes; the year's 3 MB of figures are never decoded for it), and the line saying when Compiler's
figures were read. Then come, each in its own request with the year and the filters written out and
`as_of` (`details` reads the build `summary` read, even when a link was confirmed or a sync came between
the two):

| Part | What it holds | When it loads |
|---|---|---|
| `summary` | The five key figures, the line saying whether Compiler counted the rows of Power BI's view, and the youth indicators with their eTools indicators | At once; it computes the build that `details` then reads |
| `details` | The breakdowns (with the registered young people by home governorate, payload format 2), *Compiler and eTools, side by side*, the programme documents in Compiler | Once `summary` has finished, when near the screen |

When the build for the chosen filters is already stored (10 minutes), the frame draws both parts itself:
one request. A link confirmed in the admin (with the *Confirm* action or by saving it), corrected or
added shows at once, and so do a card version chosen in Admin → *Power BI cards* and a new read of
Compiler's figures. The filter bar is a plain form, as
before: changing a filter loads the page again.

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
   `compiler-youth` scheduled job (every night at 21:00, off by default). It first asks Compiler to
   count the youth figures (`POST /api/figures/runs/`, programme `youth`: this year, the years before it
   that NeuroDB already keeps, and all years together) and waits for it, as for the education counts; a
   Compiler without the youth count answers that it does not know the programme, and its youth API counts
   when read, as before. Compiler counts one thing at a time: if it is busy with the education count, the
   sync waits for that one, then asks once more (the run notes `calculation_other`); if it is still busy,
   the run notes "Compiler was busy with another count", the youth API counts each year when read and all
   years together keep their last count. It then reads this year and `COMPILER_YOUTH_YEARS - 1` years
   before it (default 2 in all; a year Compiler does not have is skipped), and all years together
   (`?year=all`, kept apart from the years in a table of its own, `YouthAllYears`, so that it never counts
   as the latest year, even for an earlier NeuroDB after a rollback; it has no targets and no eTools links). A run whose all-years figures could not be read ends *partial*; an older Compiler,
   which has none, is not an error.
4. **Power BI's view** `vw_youth_data_all`: the database grant, the deployment order (NeuroDB first, or
   together, then Compiler), the two jobs to switch on and the way back are those of *Education programmes*
   → *Setting it up* (steps 4-6), and so is Data health's *Compared with Power BI*.

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
- *Which registrations* (Compiler's payload format 3): those of the view Power BI reads,
  `vw_mscc_data` (Makani) and `vw_bridging_data` (Dirasa), for the year (Makani: the rounds of the year;
  Dirasa: *Dirasa by year* below), deleted ones left out,
  with the partner and the centre or school the view gives (see *Compared with Power BI* below). When the
  view cannot be used, Compiler's own rule, corrected for what is certain: Makani counts no TLS
  registration and no registration without a round any more (before format 3 it counted both, and the
  current year's round-less registrations: one reason NeuroDB showed more than Power BI). Every past year
  NeuroDB reads (`COMPILER_EDUCATION_YEARS`) is counted again by the first run after the change (its
  payload is of an older format) and then weekly, so a past year no longer keeps the count made while it
  was the current year.
- *Makani*: registrations not deleted in the rounds of a year; partner = the view's, else the
  registration's partner, else the center's; place = the center's governorate, district and cadaster.
- *Bridging*: registrations not deleted in the rounds of the year (payload format 4: the CLM
  rounds whose Bridging start date is in the year, a round without one by the year in its name; Compiler
  setting `FIGURES_POWER_BI['bridging']['period'] = 'current_rounds'` counts the rounds flagged current
  instead, current year only); place = the registration's governorate, district and cadaster. A child
  counts once per year however many rounds; registrations add up over the rounds. A child who dropped out
  still counts; the learning result shows dropouts.
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
  answer to education status, referral, dropout reason, ID type). From payload format 4 each Bridging cube
  also has the round (the registration's or the teacher's), which the Round slicer and the line under the
  title read. The payload also lists the centers
  and schools (type, emergency status, GPS point; schools with the children numbers they report).

**Compared with Power BI.** Power BI's Makani, Dirasa and Youth dashboards read three views of Compiler's
database: `vw_mscc_data`, `vw_bridging_data` and `vw_youth_data_all`. Their definitions are kept in
production only (no branch of Compiler holds them), so Compiler reads them at each count: from payload
format 3 (education) and 2 (youth) it counts the registrations of the view itself ("view mode") when the
view exists, Compiler's counting role may read it, its key matches the registrations (at least 99% of
its ids, with their round or partner) and it has rows for a period that has registrations; otherwise its
own rule ("rule mode"), and the payload says why: "view missing", "no SELECT permission on vw_mscc_data",
"key check: 62% of the view's ids are registrations", "the view has no rows for 2026 while 41,210
registrations do", "setting FIGURES_POWER_BI base = rule", or a query that took too long ("QueryCanceled:
..."). Compiler's setting `FIGURES_POWER_BI` (per programme:
`base` `auto`/`view`/`rule`, `deleted` `exclude`/`include`, Makani's `period` `round_year`/`current_rounds`,
Bridging's `period` `start_year`/`current_rounds`)
changes this without a code change. Ids never leave Compiler: the payload holds counts, the views' column
names and their definitions with every string literal and long list of numbers masked (and the hash of the
raw text).
- *The line under the key figures* (Makani and Dirasa, every tab but Dirasa's outreach, and youth): "Counted
  from Power BI's view `vw_mscc_data`: the same registrations Power BI reads for 2026 (deleted ones left
  out). Compiler counted at 02:31 on 16 Oct; Power BI shows the time of its own refresh.", with the deleted
  registrations of the view left out and a link to Data health; in rule mode "Power BI's view could not be
  used (reason), so Compiler counted with its own rule; the figures may differ from Power BI."; for a payload
  of an older Compiler "Not yet compared with Power BI's view."; Makani adds the registrations waiting for a
  cycle (in neither dashboard). The two dashboards are counted at different times: compare them right after
  Power BI's refresh.
- *Card versions.* For the cards whose Power BI measure is unknown NeuroDB has every candidate ("version"):
  Makani children (registrations, or the view's rows), children with disabilities (a disability other than
  "No", or anything but "No" blanks included), working children ("Yes", or anything but "No"), immunized and
  screened (which health record field), program staff (every facilitator, active ones, those at centres
  with children, or the rows of `vw_center_program_staff`); Dirasa children (unique children, registrations,
  or the view's rows), CWD in Dirasa (registrations, or the numbers the schools report: those with
  registrations, or every school a Dirasa partner lists), in-school children and CWD (the same two school
  sets), teachers (of the schools chosen, of the schools with registrations, or the rows of
  `vw_teacher_data`); youth reached and partners. Today's definition is the default, except where the
  evidence is certain: Dirasa's children card counts **unique children** (Power BI's card is below its
  nationality chart's total, which counts registrations) and shows the registrations as a second line, and
  Dirasa's in-school children and in-school CWD sum the schools that have registrations under the filters
  (the schools "Dirasa schools" counts; a school listed only for its teachers no longer adds its numbers);
  the country programme's
  Makani and Dirasa sources read the same version. A version a payload cannot count (a measure of a newer
  format, a filter the unique count does not hold) shows the default instead, and the card says so.
- *Choosing a version* (Administrators only): Admin → Compiler (youth and education) → **Power BI cards**
  lists every card. Type the number Power BI shows for a card and choose its period in the list (the
  periods Data health compares: Makani's year, e.g. 2026; Dirasa's year, e.g. 2026 (a round by its name in
  counts before format 4); youth's year or *All years*; left empty, the latest period), save: Data health
  marks the version that gives it; choose that version in the same form. A number kept without a period is
  compared with the latest period only. "The latest period" is the one the page opens on: the newest year
  (before format 4: Dirasa's round that starts last, by its first round's start date, as the page ordered its
  rounds, whatever its name, since round names are free text); Data health and its summary order the
  periods the same way. The pages show it at once
  (the choice is in their caches' keys); nothing else changes. **Power BI views** (Administrators only,
  read-only) shows each view's masked definition, columns, the relations it reads, its hash and the date
  its definition last changed (a change is also named on the sync run, `power_bi_changed`).
- *Data health → Compared with Power BI* (every member of staff; counts only, no definition): per programme
  its latest periods (at most three; youth: all years first): whether the figures were counted from the
  view or from the rule and why, the view's registrations and rows, what NeuroDB counted, the difference
  and its reasons (deleted, not in the view; when the view's rows were not read, e.g. in rule mode, "not
  compared: the view was not read", never "not in the view"; when Compiler counted from the view but its
  comparison failed, e.g. a timeout, "not compared: the comparison could not be made (see the errors)"),
  what NeuroDB counted before this change and
  why it differed
  (TLS, without a round, of another year, without a year...), Compiler's count time; under Makani's and
  Dirasa's table, the registrations of the current year's payload in rounds that belong to no year (*Rounds
  in no year*, *Dirasa by year* below); and per period the
  reasons table, the view's rows per round (or year), the key check as percentages, the related views'
  rows (`vw_teacher_data` counted for the period's rounds only, since it holds the teachers of every CLM
  round, each round's id followed by its name; a list Compiler cut at 50 groups says so, and the version counted from it is then not available)
  and the cards: every version's value at the period total, the number typed from Power BI, the
  version(s) equal to it ("No version equals Power BI's number" otherwise) and the version in use. A
  comparison of an older count than the figures kept (a later payload had none: Compiler gone back to an
  earlier version) says "not compared in the latest count". A view Compiler could not describe (a timeout)
  is left as it was, never taken for a dropped one; a view dropped and created again with the same
  definition is no change. Ask NeuroDB's data freshness look-up and the internal API get a summary of it
  (mode, difference, and `stale` for a comparison of an older count). The daily review receives it too but
  does not yet raise a problem for a programme counted with Compiler's rule or a view whose definition
  changed (not built yet).

**The pages** (Education and youth → **Makani (MSCC)** and **Dirasa (Bridging)**; `/education/` opens
Makani). They follow the programmes' Power BI dashboards: title "Makani Programme Dashboard <year>"
or "Dirasa Programme Dashboard <year>" (older counts: `<round>`), the last data update (Compiler's count)
and a year select (Dirasa, older counts: a round select); one tab per Power BI page (`?tab=`); a slicer bar of single-select dropdowns ("All" by
default, options = the values found in the counts, "Not specified" for unknown values). A slicer
change replaces the results only (HTMX); the tabs keep the filters. A filter chosen on a tab that does
not have it (e.g. gender on the Dirasa map) stays in the links for the other tabs and the page lists it
as "not applied on this tab"; a figure a filter cannot reach says so in its hint (program staff,
in-school children, teachers).
- The percentage charts leave the answers "Not specified" out, as Power BI's do (their bars add up to 100
  with no blank bar); the registrations are still counted in the key figures.
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
- *Dirasa* slicers: round (payload format 4: the rounds of the year), CWD type, nationality, gender, school,
  partner, governorate, school type, school active during emergency. Dirasa (format 4) lists the year's
  rounds under the title and has a Round slicer (not on Outreach). School filters (school, partner,
  governorate = the school's, type, emergency) choose the schools and the registrations counted are those of
  these schools (and of the partner chosen; partner "Not specified": registrations without a partner,
  schools without any); child filters (CWD type, nationality, gender) apply to registrations only. The
  round chooses the registrations, teachers and trainings of that round, so the schools with registrations
  are that round's; the unique children answer the round alone, with the partner or with the gender (the
  card shows the registrations with the school).
  - *Overview*: Dirasa schools (with children under the filters), children enrolled in Dirasa (unique
    children, with the registrations as a second line; the unique count answers the partner, school and
    gender filters, the card shows registrations under the others), in-school children and in-school
    CWD (the numbers reported by the schools that have registrations under the filters, as "Dirasa
    schools" counts them: a school listed only for its teachers is left out), CWD in Dirasa, Dirasa
    teachers (of the chosen schools); charts: nationality (Syrian, Lebanese, Non-Lebanese), gender, type
    of schools (the schools with registrations; schools without a type left out), in-school Lebanese /
    non-Lebanese, governorate (the child's), teacher trainings by topic (the schools with registrations).
  - *Outreach*: its own slicers (interview year, latest by default or all years; partner and
    governorate as typed in Kobo); education status, referrals, initial dropout reasons, child ID
    type. Kobo names and labels of one choice are one key (older forms cut names at 40 characters);
    known keys have a label ("Referred to Dirasa", "UNHCR registered"...), others are shown with
    spaces for "_". Blank answers are left out.
  - *Attendance barriers*: the main barrier of each registration (blank left out).
  - *Schools map*: slicers partner, governorate, school type, emergency; the schools with registrations
    coloured by type (popup: partners, governorate, type, children registered, in-school children) and
    their table.
- Everything is filtered in Python from the stored payload (`neurodb/education/cube.py`); each tab's
  result is cached 10 minutes per stored row, fetch, filters, language and the card versions chosen.
- A year stored from a Compiler without cubes (format 1) shows "Compiler sends the older format":
  run the education sync after Compiler is updated.
- If Compiler could not count a part (e.g. its query hit the timeout), the page says which and the
  other parts still show.

**Dirasa by year (payload format 4).** Power BI's Dirasa page titled "2026" shows the rounds of 2026, not
one round, so from payload format 4 Compiler counts Dirasa per year, as Makani: the CLM rounds whose Bridging
start date is in the year (a round without one, the first four-digit year in its name; a round with neither
is in no year, and Data health lists it "in no year"); with its setting `FIGURES_POWER_BI['bridging']['period']
= 'current_rounds'`, the current year counts instead the Bridging rounds flagged current, as Compiler's own
Bridging list. A child counts once per year however many rounds of the year it is registered in; the
registrations add up over the rounds; a Round slicer still shows one round. *Deployment*: NeuroDB first (or
together), then Compiler (branch `neurodb-power-bi-views`, which now holds `Neuro`): a NeuroDB deployed first
reads Compiler's per-round counts as before until Compiler follows, whereas an older NeuroDB keeps showing the
last per-round Dirasa row it read, never counted again, once Compiler counts by year (its Dirasa select mixes
that round and the years, labelled "Round", and the page, Home, the summary and Ask may open on that round
until NeuroDB is updated). No migration on either side. The first run counts again every year NeuroDB reads (`COMPILER_EDUCATION_YEARS`).
The older per-round figures are kept and hidden as soon as a per-year count is the latest read (they come
back if Compiler goes back to format 3); an old link naming a round (`?round=Bridging 2025-2026`) opens the
year holding it with that round chosen. A round the page cannot show (no stored year lists it, it has no
registration nor teacher in the year, or it is a round of another year kept from the year chosen before)
opens the whole year and the page says so; Ask answers such a round with an error, never with the year's
figures. When Compiler counted Power BI's view, the view decides which registrations are in the year, so it
may count some registrations of a round of another year (or of no year): Compiler then lists that round too,
with its own year and `"in_year": false` (CONTRACT_PBI §9.5), and NeuroDB says it apart, never as a round of
the year. The line under the title ends with "also counted, from rounds of another year: Bridging 2025-2026
(2025, N registrations)" ("in no year" for a round with neither a Bridging start date nor a year in its
name), the Round slicer lists it after the year's rounds as "Bridging 2025-2026 (round of 2025)" (chosen, the
page says that only its registrations the view counts in the year are shown), and an old link naming it
opens its own year when NeuroDB holds it. The line then adds "other rounds or no round (N registrations)"
for registrations whose round the payload does not list, or that have none, so that it adds up to the
registrations shown, and gives no count when Compiler could not count the registrations. A payload without
`in_year` (format 4 before Compiler's review) lists every round as a round of the year, as before.
*Rounds in no year*: a round with neither a Bridging start date nor a year in its name (Makani: neither a year
nor a start date nor a year in its name) belongs to no year, so its registrations are counted in no year,
while Power BI's view may count them. Compiler says how many in the current year's payload (`no_year`), and
Data health → *Compared with Power BI* says so under Makani's or Dirasa's table: "N registrations are in
rounds that belong to no year (no Bridging start date and no year in the name): <round names>. They are
counted in no year." (counts and round names only; Ask's data freshness answer carries the same, `no_year`).
Giving such a round a Bridging start date (Makani: a year) in Compiler places it in a year at the next
count. Makani's and Dirasa's years are listed newest first by their number, whatever their rounds' start
dates (a round's start date may be empty). Power BI numbers typed for a round must be typed again for the year
(the card admin says "no such period: choose one"). Country-programme links to a round keep their value;
new links name a year ("Dirasa 2026 — children enrolled").

**Setting it up.**
1. Compiler: deploy branch `neurodb-education-figures` (migrations `figures.0001` and `0002`) with its
   Celery worker running (no Celery beat or periodic task is needed: NeuroDB asks); the comparison with
   Power BI's views (payload formats 3 and 2) is branch `neurodb-power-bi-views`, made from `local`, which
   adds no migration. On the Compiler server, for its developers only, `python manage.py
   refresh_neurodb_figures` counts at once. Optional settings: `FIGURES_DATABASE` (a read-replica alias),
   `FIGURES_STATEMENT_TIMEOUT_MS` (600000), `FIGURES_WORK_MEM` (32MB), `FIGURES_JIT` (off: PostgreSQL's JIT
   compilation took longer than the counts themselves), `FIGURES_API_RATE` (120/hour), and
   from `neurodb-power-bi-views` `FIGURES_POWER_BI` (see *Compared with Power BI*),
   `FIGURES_POWER_BI_MIN_MATCH` (0.99), `FIGURES_POWER_BI_RELATED` (on), `FIGURES_PAST_MAX_AGE_HOURS` (168)
   and `YOUTH_FIGURES_API_RATE` (120/hour).
2. NeuroDB uses the same Compiler URL and token as the youth figures.
3. Admin → Import and sync runs → Run a job → **Compiler education figures**, then switch on the
   `compiler-education` schedule (02:30; off by default; change the time in the admin). It first
   reads Compiler's list of years, then asks Compiler to count each programme's current year and the
   past years that need it (`targets`: from Compiler's payload format 3, a past year counted in an older
   format, counted more than a week ago, or never counted; an older Compiler counts the current years
   only, as before) and waits for it (checking every `COMPILER_RUN_POLL_SECONDS`, default 60, for at
   most `COMPILER_RUN_TIMEOUT_MINUTES`, default 120); a count that fails or takes longer is written on
   the run, which ends *partial*, and what Compiler has is read all the same. Compiler counts one thing at
   a time: if it is busy with the youth count, the sync waits for that one, then asks once more (the run
   notes `calculation_other`, and "Compiler was busy with another count" if it still is).
   (`sync_compiler_education --no-calculate`, for developers, only reads.) It reads the current year of
   each programme (Dirasa: a round in counts before format 4) and the ones before it
   (`COMPILER_EDUCATION_YEARS`, default 3 in all; from an older Compiler, only the counted ones), Makani
   and Bridging only. Payloads of formats 2, 3 and 4 are read alike; format 3's comparison with Power BI's
   view is kept apart for Data health, and a change of a view's definition is named on the run
   (`power_bi_changed`). A year Compiler has not counted yet is listed as *pending* in the run and
   arrives at the next run.
4. **Deploying the comparison with Power BI's views, in this order.**
   1. *Database*: an administrator of Compiler's database gives Compiler's counting role (the user of
      `FIGURES_DATABASE`, a replica's role if one is used) the right to read the views:
      `GRANT SELECT ON vw_mscc_data, vw_bridging_data, vw_youth_data_all, vw_center_program_staff,
      vw_center_data, vw_teacher_data TO <role>;`. Without it Compiler counts with its own rule and Data
      health says "no SELECT permission on ..." (the views' definitions are read all the same: that needs
      no grant). A grant belongs to the view as it is: `CREATE OR REPLACE VIEW` keeps it, but a view
      dropped and created again (these views are re-created by hand, and a Compiler migration has dropped
      some before) loses it, and Compiler then counts that programme with its own rule again (each period
      at its next count) until the grant is given again. So either count with the role that owns the views
      (it needs no grant), or also give the default privileges of the role that creates them, once:
      `ALTER DEFAULT PRIVILEGES FOR ROLE <creator> IN SCHEMA public GRANT SELECT ON TABLES TO <role>;`,
      and after re-creating a view by hand, look at Data health the next morning.
   2. *NeuroDB first* (or together with Compiler): deploy (migrations `education.0002_power_bi`: three new,
      empty tables; `education.0003_power_bi_figures_counted_at`: one empty column;
      `youth.0002_youth_all_years`: the youth figures of all years together in a new table of their own;
      each applied in a moment). NeuroDB reads payload formats 2, 3 and 4, so until Compiler follows it
      shows the figures as before (from a Compiler without the comparison: "Not yet compared with Power
      BI's view").
   3. *Then Compiler*: deploy `neurodb-power-bi-views` (which now holds `Neuro`; production also runs the
      Makani wellbeing work, `makani-wellbeing-flags`; both are on `local`). Nothing else is needed: the
      next count is in the new formats. Not the other way round: from payload format 4 Compiler counts
      Dirasa by year, and a NeuroDB that reads only formats 2 and 3 keeps showing the last per-round Dirasa
      row it read, never counted again, until it is updated (*Dirasa by year* above). Makani and youth would
      not mind the order: an older NeuroDB reads only the keys it knows, youth is not in the education
      index, and the youth API still answers a year on the spot.
   4. *Jobs*: in Admin → *Scheduled jobs*, make sure `compiler-education` (02:30) and `compiler-youth`
      (21:00) are switched on (both are off when NeuroDB is first deployed), then press *Run now* on both,
      or wait for the night. Pressing both at once is fine: the second waits for the first count, then
      asks for its own. The first runs also count again every past year NeuroDB reads (their payloads are
      of the older format); after that a past year is counted again once a week.
5. **The next morning**, Data health → *Compared with Power BI*: every programme should say "the view"; a
   "Compiler's rule" says why (the reasons are listed under *Compared with Power BI* above, e.g. the
   grant of step 4.1 is missing). *Before this change* shows how much each old cause weighed (TLS, without
   a round...). Right after Power BI's refresh, an administrator opens Admin → **Power BI cards**, types
   for each card the number Power BI shows, chooses its period in the list (Dirasa: a year such as 2026),
   and picks the version Data health
   marks as equal to it; the pages show it at once. A card no version equals stays on its default, and
   Data health says "No version equals Power BI's number": send the screenshot to the developers.
6. **The way back.** Compiler's setting `FIGURES_POWER_BI = {"mscc": {"base": "rule"}, "bridging": {"base":
   "rule"}, "youth": {"base": "rule"}}` (Compiler's developers set it; no code change) makes Compiler count
   with its own corrected rule again, and the pages say so; add `FIGURES_POWER_BI_RELATED = False` when the
   related views (staff, centres, teachers) must not be read either (`base` covers the programme's own view
   only). A change of `base` or `deleted` in `FIGURES_POWER_BI` makes every period's count stale, so the
   next run counts the past periods again too (not a week later); a change of Makani's or Bridging's `period`
   makes only the current year's stale (a past year always counts the rounds of its own year). Likewise a period counted in
   rule mode because the view could not be read (missing, not a view, no SELECT permission) is stale once it
   can be (a grant given after the deployment or again after the view was re-created, or a missing view
   created again by hand; a view dropped and created again loses its grants, step 4.1), so the next run counts
   it from the view, past periods included; one counted in rule mode for what the view's rows hold (key
   check, no rows for the period) follows at its weekly count. Going back to the earlier Compiler branch sends the older formats again, which
   NeuroDB still reads: its pages then say "Not compared with Power BI's view in this count; the last
   comparison, of the count of <date>, is on Data health.", and Data health marks that comparison "not
   compared in the latest count". In NeuroDB nothing needs undoing, even going back to an earlier NeuroDB
   release: the new tables hold only the comparison and the youth figures of all years together, which an
   earlier release never reads, and a card version chosen only changes which candidate a card shows. Each
   card's "default" is the version NeuroDB shows unless an administrator chooses another; for three Dirasa
   cards it is not the count before this change: the children card's count before it is the version
   "Registrations", and in-school children and in-school CWD now sum only the schools with registrations
   (the former set, every school chosen including those listed only for their teachers, is no longer
   offered). Switching off a job in *Scheduled jobs* stops its nightly count; the figures already read
   stay.

**Speed.** Makani and Dirasa are drawn whole, in one request, as before: on a copy of the data with
40,000 Makani registrations each tab answered in about 0.25-0.3 seconds cold (Dirasa about 0.1), under the
0.3 seconds from which a page is given parts. Each tab's figures and the slicers' choices are kept 10
minutes per web worker, so a tab opened again answers in about 40 ms. A much larger Makani year (about two
and a half times the registrations) took 0.55-0.8 seconds a tab: if production's Makani figures grow that
large, the page should load in parts like the youth page.

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
  framework**. The document is read in the background (a minute or two) by the AI provider in use with
  the same key and model as Ask NeuroDB (AI settings), on OpenAI with `store=False`
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

- *Linked sources* (summed for their year: different partners may reach the same people): pick the
  source from the list — the eTools PD indicators of the programme documents under the output
  (partner reporting, read from Partner monitoring's Reached: for an indicator whose cycle value is
  the sum of the years, what the partner reached **in that year**; for one whose cycle value is the
  latest year's, what it reached **to the end of that year**; so a programme document running over
  several years is never counted twice), the ActivityInfo master indicators of the cycle's years (the
  master indicator's result), the Compiler youth indicators (young people reached) or Makani /
  Dirasa (children enrolled, as the programme's page counts its children card: the version chosen in
  Admin → *Power BI cards*, by default the registrations for Makani and the unique children for Dirasa).
  The year fills itself from the source when it can. Untick *Confirmed*
  to keep a link without counting it. The knowledge hub reads the same values.
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

**Loads in parts** (see *Pages that load in parts*). The page answers its frame at once: the cycle
selector, the cycle's panel (its years, the share of the cycle elapsed, its summary and documents, and
*Edit in the admin* for staff) and the note on AI-suggested indicators. Then come, each in its own request
with the cycle written out and `as_of` (`outcomes` reads the build `summary` read, even when an edit or a
sync came between the two):

| Part | What it holds | When it loads |
|---|---|---|
| `summary` | The six key figures, *Where the CPD indicators stand* (the status chart) and the outcomes at a glance | At once; it computes the cycle's build that `outcomes` then reads |
| `outcomes` | Each outcome with its outputs, indicators, programme documents and partner reporting | Once `summary` has finished, when near the screen |

The outcome links (in the summary, and the knowledge graph's `?cycle=N#outcome-<id>`) ask for `outcomes`
first and scroll to the outcome once it is in. When the build is stored (10 minutes), the frame draws both
parts itself: one request. Anything entered in the admin that the figures read (a value, a milestone, a
link confirmed, a target, an output's eTools name, the cycle's eTools country programme, how a linked
ActivityInfo master indicator is set up) shows at once, and so does a finished sync. Computing a build
takes about 1.3 seconds on a copy of the data twenty times today's size (it was 2 seconds): each Makani or
Dirasa year linked is now read once per build, however many links name it (it was read once per link,
4 MB each time), and its children enrolled are kept per web worker until the figures are read from
Compiler again or a card version is chosen, which also speeds up an indicator's own page. Most of what is left is the partner
reporting of the cycle's programme documents.

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
NeuroDB's pages move to the new year by themselves on 1 January. Steps 1–4, 6 and 7 are needed only while
ActivityInfo is in use; step 5 (population figures) and step 9 (the eTools master indicators) every year. NeuroDB Watch reminds the administrators
from 15 December (see *ActivityInfo: in use or archive*, *The yearly reminder*).

1. Admin → Reporting years: create the new year and tick *current* (only one can be current). While
   the December results of the year before are still being imported (until 22 January), keep last year
   ticked current and tick the new year afterwards.
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
8. Once the last ActivityInfo year is complete (its December results are imported by 22 January),
   switch off *activityinfo-data* in Scheduled jobs. On *Automatic*, ActivityInfo then becomes an archive
   by itself within 30 seconds (or set *Archived* on Admin → Reporting setup → *ActivityInfo: in use or
   archive*); its alarms stop, and every ActivityInfo page of past years stays open.
9. eTools master indicators: set each one's code and target for the new year. A master is on Partner
   monitoring's *Master indicators* tab of the new year (open the master, *Code* and *Target*, *Save*) once it
   has a confirmed PD indicator of a programme document running in that year, or a year row for it; a master
   whose programme documents all ended last year is not there until a new one's PD indicator is confirmed in
   it (*Attach to…* lists every active master). Before that, an Administrator types the code and target in its
   admin page's year rows (*Reporting setup* → *Master indicators (eTools)*), which lists it on the tab at once.
   Nothing needs copying: the masters have no year and their PD indicators carry on with their programme
   documents; a master listed in the year without a target reads *No target* and *Needs attention* lists it
   (its link opens exactly those, the filter *Listed this year, no target*). Untick *shown this year* for a
   master that does not go on: it is no longer listed that year, and the PD indicators it counts of
   programme documents still running in it then count nowhere that year (saving says how many; they are
   listed under *Not in a master*, and check 12 counts them). A link has no year, so another master can take
   one in its total only once it is taken out of this one, which takes it out of this one's past years too:
   keep the master shown while its programme documents run, or merge it into its successor (which moves its
   links and years; a year unticked on the master merged is shown on the successor, which goes on, and the
   merge says so).

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
and exceptions are traced in Application Insights. The requests of a page's parts carry `_part=` in
the access log and the custom dimension `neurodb.part` in Application Insights (see *Pages that load
in parts*).
