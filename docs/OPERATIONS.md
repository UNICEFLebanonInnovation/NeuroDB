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
  addresses, phone numbers, links, the person names NeuroDB knows (the field monitoring team members
  included, once Monitoring insights has built its visits) and any name written after a title
  such as Mrs, Dr or Sheikh (a place named that way, such as Sheikh Zennad, is withheld too); a
  question that names a person finds no field monitoring record, and the search examples show the
  visit reference only. The four field monitoring look-ups (`fm_summary`, `fm_visits`, `fm_visit`,
  `fm_search`, registered by Monitoring insights when `FMM_ENABLED` is on) give Ask the visits,
  entities, ratings, HACT Q1, report quality, urgency and follow-up of this calendar year in the whole
  country as **structured fields only**: no monitors' note, no checklist answer and no search snippet
  ("narratives are available in Monitoring insights"), and never a visit lead or a team; `fm_search`
  says which visits mention a word, and refuses a search for a person NeuroDB knows. For every other
  dataset, keys naming an e-mail address, a
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
  Watch, Monitoring insights, the Help assistant) is also counted per day in one ledger, Admin → Data and sync → **AI use** (tokens; US
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
  daily look-ups*). It offers only 13 read-only tools of the 44 (`find_anything`, `entity_profile`,
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
run finishes *skipped*. It also needs the AI assistant (`AI_ASSISTANT_ENABLED`, an OpenAI key).

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
the OpenAI credit pause of Monitoring insights is on (a call that meets the credit's end starts the
pause). A part of 12,000 characters is about 4,000 tokens sent and 1,000–3,000 written; a 100-page
report takes roughly 150,000 tokens, so the default cap reads about six such reports a night. A
document that needs more than a whole day's budget (the smaller of the review's cap and 80% of the
shared cap) is not started, as it could never finish: it fails with the reason, and the next nightly
run tries it again once the cap is raised or the document is split. A full run stopped by the budget
leaves the documents it did not reach waiting, so the nightly run goes on with them. The
model is `AI_ASSISTANT_MODEL` at low effort, `store=false`, 180 seconds and one retry per call.

**What goes to OpenAI**: the document's title and text (the part being read), the topic list, and for
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

**The review page** (`/knowledge/review/`, sidebar → Resources → *Document review*). Every signed-in
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
  **Write a paragraph** is the only AI call: one Responses call (`AI_ASSISTANT_MODEL`, low effort,
  `store=false`) on that theme's findings only (at most 60, cleaned of names, e-mail addresses, links and
  phone numbers), cited back as "(Document title, p. n)"; a citation that is not one of them is dropped,
  and an answer citing none is not shown. Each person may write **50 a day** (the chip "n of 50 today";
  refused requests do not count); it is recorded under *Document review* in AI use, counts against the
  review's daily cap and 100% of `AI_DAILY_TOKEN_SOFT_CAP`, and stops when the OpenAI credit pause is on.
  Each request is kept in Admin → Theme paragraphs (who, when, what, tokens).
- **Actions**: cards, open by owner, the table with its filters (*Current*: documents dated within a year
  of the newest document of the collection — their date, issue date or year, else when added; *All
  time*), the status set on the row, CSV and Excel of the filter.
- **Report**: *Download desk review (.docx)* (`desk-review-YYYY-MM-DD.docx`), built when clicked from the
  same figures, without AI, for the chosen batch and minimum of documents.

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

## Help assistant (`/help/`, Ctrl+Shift+H)

An in-app help chat that answers **how NeuroDB works** (what a page shows, what a rule checks, where a
number comes from, why a visit scored what it scored, which job refreshes a page and when), never
questions about the programme data: those go to Ask NeuroDB and Chat with Data, and the panel says so
with a link. Every signed-in person but a donor has it. The code is in `neurodb/help`.

- **The help pages** (`/help/`, sidebar: *Help*, at the bottom): the help guide, written for staff (not
  this runbook), one page per area: *Getting around NeuroDB* (the sidebar, roles, data sources and refresh
  times), *Monitoring insights* (filters, every tab and card and what it counts from which eTools data,
  Not monitored, the quality score's categories, weights and bands, the rules switched on and what each
  checks, the AI checks, urgency, the AI brief, Chat with Data and the map), *Action points*, *Exports*,
  *Knowledge base and documents*, *Ask NeuroDB*, *For you and What's new*, *Overview, management brief and
  daily review*, *AI limits* and a *Glossary*. Each heading has its own address
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
  Administrator, for that rule), `explain_visit_score` (a visit's score, band, deductions per category,
  each rule's result and points lost, and urgency's parts: never a narrative, an answer, a team, a
  monitor, nor an AI check's explanation; the same access as the visit page, nothing while
  `FMM_ENABLED` is off) and `list_jobs` (the scheduled jobs, what each does, its schedule and its last
  run's status and time: never who ran it or its error text). Each look-up runs in a read-only
  transaction. Answers cite the guide sections (`/help/<page>/#<slug>`) and, for an Administrator only,
  the admin page a setting lives on.
- **What it sends to OpenAI**: the question (names NeuroDB knows, e-mail addresses, phone numbers and links
  removed before it is sent and kept), the page's path and cleaned title, up to 6 earlier questions and
  answers of the conversation, and what its look-ups return. Its own prompt (not Ask's), the model
  `AI_ASSISTANT_MODEL` at low effort, at most 4 rounds and 90 seconds, `store=false`, a keyed hash of the
  person (`safety_identifier`).
- **What it refuses** (a refusal costs no quota): passwords, API keys, secrets, connection strings and
  environment variables, and ways around sign-in or access rules are refused before any call (a fixed list
  of words, `help.assistant.screen`); the model declines the subtler ones and questions about programme
  data (pointed to Ask NeuroDB) or not about NeuroDB. Either way NeuroDB's own message is shown.
- **Limits**: `HELP_PER_USER_PER_DAY` (20) questions a day per person, counted from local midnight;
  declined questions do not count, and one being answered does (429 "You have asked 20 help questions
  today; the count starts again tomorrow."). Questions the model declines were calls all the same, so
  only as many as the quota are free each day; past that the person waits for tomorrow (429). At most 2
  being answered per person. The shared
  `AI_DAILY_TOKEN_SOFT_CAP` at 100% (a person asks), and the OpenAI credit pause of Monitoring insights
  (6 hours after OpenAI says the credit ran out; a help answer that meets it starts the pause too). Its
  calls are counted under *Help assistant* in *AI use*. One answer is usually 2-3 model calls of about
  4,000-10,000 tokens each, mostly the cached prompt and look-ups.
- **Switching it off**: `HELP_ENABLED=false` (default: on when `OPENAI_API_KEY` is set; it also needs
  `AI_ASSISTANT_ENABLED`). The help pages stay; the panel says the assistant is switched off and links them.
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
| `compiler-youth` | `sync_compiler_youth` | `0 21 * * *`, daily 21:00; switched off until Compiler is configured |
| `compiler-education` | `sync_compiler_education` | `30 2 * * *`, daily 02:30: asks Compiler (BMA) to count Makani and Bridging, waits, then reads the counts; switched off until Compiler is configured |
| `compiler-wellbeing` | `sync_compiler_wellbeing` | `0 3 * * *`, daily 03:00: asks BMA to work out the Makani wellbeing flags, waits, then reads them; switched off until Compiler is configured |
| `knowledge-hub` | `build_knowledge_hub` | `0 7 * * *`, daily 07:00: reads new library and CPD documents, then rebuilds the knowledge hub (it is also rebuilt after every sync) |
| `whats-new` | `whats_new_digest` | `30 7 * * *`, daily 07:30: the what's new notes, emailed to who asked |
| `watch` | `run_watch --daily` | `45 7 * * *`, daily 07:45: NeuroDB Watch's morning pass, the *For you* pages (see NeuroDB Watch); a quick pass also follows new data |
| `ml-readiness` | `ml_readiness` | `45 6 * * 1`, Mondays 06:45: is the data ready for machine learning (Data health) |
| `forecast` | `forecast_indicators` | `15 7 * * 1`, Mondays 07:15: year-end forecasts of the indicators, back-tested |
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
| Run a job → *Monitoring insights* | `fmm_refresh` | background |
| Run a job → *Monitoring insights (AI)* | `fmm_insights` | background |
| Run a job → *Monitoring insights (AI checks)* | `fmm_ai_checks` | background |
| Score settings → *Re-check carried answers* | (deletes a batch of carried AI check answers; the next `fmm_ai_checks` runs check them) | in the request |
| Run a job → *Action points (AI review)* (also *Run AI review* on the action points page, with a batch size) | `fmm_ap_review` (`--limit N` from the page) | background |
| Run a job → *Review documents (pending)* (also the admin action *Analyse in the document review* on Knowledge documents) | `review_documents --pending` (`--document <id>` for one document) | background |
| Run a job → *Review documents (full)* | `review_documents --full` | background |
| Run a job → *Locate document findings* | `review_documents --locate` | background |
| Run a job → *Population figures* | `load_population_figures --bundled --replace` | in the request (seconds) |
| Run a job → *Check freshness* | `check_sync_freshness` | in the request; changes nothing |
| Run a job → *Repair roles* | `bootstrap_roles` | in the request |

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
| `fm_questions`, `fm_options`, `fm_programme_activities`, `offices`, `sections` | `fm-questions/`, `fm-options/`, `fm-programme-activities/`, `office/`, `reports/sections/` | kept whole in `datamart.DatamartDocument` (below); they feed Monitoring insights: the checklist answers and their options, the programme activities and CP outputs of each visit, and the office and section names (see *Monitoring insights*, *Data and its keys*) |

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
assistant sees every field. The FM questions, options and programme activities, the offices and the
sections also feed Monitoring insights, which reads their keys first (see Monitoring insights).

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
| `fm_follow_up` | Field monitoring visits (Monitoring insights) reported, rated off track or constrained (the worse of the overall rating and HACT Q1), ended 14 (Score settings *follow-up days*) to 120 days ago, with no eTools action point linked and no NeuroDB action point following them up (one added by hand, or one NeuroDB made and someone marked done); critical when off track and ended over 30 days ago. Closes when an action point is linked (or a NeuroDB one follows the visit up) or the rating changes. Reads the Datamart sync and the Monitoring insights refresh; finds nothing while `FMM_ENABLED` is off (switch the check off then, or its source shows as not refreshed) | the visit's sections | trial |
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
visits built, with their rule results, for checking the data) and **Visit reviews**; and for the
action points page **Action point settings**, **AI reviews of action points**, **Action point
verifications**, **AI content summaries** and **NeuroDB action points** (see *Action points*).

The AI is **off at deploy** (`FMM_AI=false`): the page then shows a brief written by NeuroDB from the
figures, and the chat says it is switched off. It is switched on at step 7 of the go-live checklist
below. The partner, programme document, overview, assurance and action points pages link into
Monitoring insights, and its visits are in the knowledge hub, What's new and NeuroDB Watch (*Links into
the rest of NeuroDB*, below).

### What each block shows

Every figure is computed from the visits the last refresh built (`neurodb/fmm/metrics.py`, one function
per block, each taking the page's filter), kept 10 minutes.

| Block | What it shows | Source |
|---|---|---|
| Key figures (every tab) | Monitoring visits (whatever their status, with the breakdown reported / in progress / planned / cancelled / status unknown), monitored entities (rated / not monitored), the average quality of the scored visits, and the visits of high urgency (red, with the amber ones, and *View urgent visits →*) | `kpis`: `fmm.Visit`, `fmm.VisitEntity` |
| Insights › Morning briefing | This year so far (1 January to today, whatever the page's period; its other filters kept), tinted as FMS shows them: critical flags (urgency at or above red), average quality, low quality visits (below the Medium band), critical partners (a visit at or above red), the visits, and the visits in review, submitted, in data collection, assigned and completed; each tile with its definition (ⓘ) and the visits behind it; *Top critical partners*: the five partners with the most critical visits as red chips, each opening those visits; *Quality by governorate*: "NAME · average · visits", the chip tinted by quality band | `briefing` |
| Insights › AI monitoring insights | The parts the published prompt version lists (v2: a coverage and quality summary, key programmatic findings, operational challenges, recommendations, and up to five priority action points written "[PRIORITY: High] Section / Partner — action — responsible — timeframe"), each sentence with the visits it rests on; *Generation settings* (read-only: the published version's model, effort, output token limit, narrative samples, compliance depth, temperature or top-p only when set and used, the sections; *Edit in admin* for administrators); the chips (model, effort, tokens, temperature and top-p, notes and quality flags sent, prompt and rules versions, quota); *Regenerate*, *What was sent* | `fmm.ai.insights.current`: the latest `Insight` of the filter, else the brief written by NeuroDB (`fmm.ai.fallback`) |
| Insights › Critical visits requiring attention | FMS's "Critical items requiring attention": the scored visits at or above red urgency, most urgent first, at most 10 (else the five most urgent amber ones), each with its partner, rating, HIGH/MEDIUM and urgency, and one line per rule it failed (the stored flag, with the AI's explanation for an AI check; R19 as "N monitors not on the staff list of <office>", never who); *Show all N* opens the Visits tab on that urgency band | `critical_items`: `fmm.Visit`, `fmm.VisitRuleResult` |
| Insights › Chat with Data | Questions about the visits of the filter, answered with four look-ups, links to the visits checked; the published prompt version's starter questions | `fmm:chat_stream`, `fmm.ai.chat`, the `fm_*` look-ups |
| Quality › Quality score trends | Two smooth lines per month (by visit date: start, else end): the average quality score (left axis, 0–100, filled) and the reports (right axis); a point opens the month's visits | `monthly_quality` |
| Quality › Monitoring volume over time | Visits per month (by visit date: start, else end) as bars, with their average quality as a line on its own axis (0–100%) | `monthly_volume` |
| Quality › HACT Q1 — Finding rating distribution | Visits per month by their worst HACT Q1 answer, stacked On track / Constrained / Off track in FMS's colours; the overall finding rating instead ("Overall finding rating distribution"), with a note, when no visit of the filter has a Q1 answer; the drill-down box's pills On track, Off track, Constrained and Not Monitored (apart) open their visits | `hact_q1_by_month`, `rating_by_month` |
| Quality › Geographic coverage | The places visited, their governorate, visits and last visit (top 10; *Show all* loads the rest, up to 500) | `locations` |
| Quality › Top recurring issues | Flags grouped by rule and reason, with their visits and mean urgency | `top_issues`: `fmm.VisitRuleResult` |
| Quality › Quality issues summary | Narrative and rating coherence flags (R6, an AI check), Not monitored (planned, not conducted: reported visits with no entity rated), visits with 3 or more flags | `issues_summary` |
| Analysis › Quality score distribution | Scored visits in FMS's five bars of 20 points (0–20 red, 20–40 orange, 40–60 amber, 60–80 light green, 80–100 green, 100 included) along a "Visit count" axis, and the visits not scored | `score_buckets` (summed from the pass's buckets of 10) |
| Analysis › Rule score trends over time | Per rule with points, a smooth line of the share of its maximum points the visits that started each month earned ("% of max score", legend on top, FMS's colours for the first five); a point opens the visits the rule flagged that month (worked out when the panel scrolls into view) | `rule_trends` (with `rule_stats`, from `rule_months`) |
| Analysis › Quality rule analysis | One row per rule that checked visits, in the order of the ids: "N / M visits flagged" and a bar of the share not flagged (green under 25% flagged, amber 25–50%, red over 50%); under it, every rule with what it checks and "not available", "AI check pending", "AI check switched off", "off" or "flag only" | `rule_analysis` |
| Analysis › Quality flag frequency | Every rule that flagged a visit, most first: id, bar and "N (share of the visits it checked)"; the same counts as the rule analysis | `flag_frequency` |
| Analysis › Flag count distribution | Scored visits with 0 flags (green), 1 flag (blue), 2 flags (amber), 3+ flags (red), the share inside the bar | `flag_distribution` |
| Analysis › Highlights | Visits, reported, governorates covered of the gazetteer's, average quality (the key figure's), off-track visits, PSEA-flagged visits of those with a PSEA question, High / Medium / Low shares, entities by type | `highlights` |
| Analysis › Governorates not visited | The gazetteer's governorates no visit of the filter is placed in | `governorate_gaps` |
| Analysis › Field offices | Visits and average quality per field office ("Office not known" too), and where the offices came from | `offices` |
| Analysis › Entity performance | All the monitored entities (FMS's default *All* chip) or one kind (Partner, CP output, PD/SSFA), each with its type badge (CSO partner, CP output, PD/SSFA…), visits, average quality coloured by band, High / Med / Low, top issue and last rating, worst average quality first, unscored last; partners by their full name; a PD shows its planned visits for the year (the table loads when it scrolls into view) | `entities_performance` (`"all"` merges the kinds) |
| Analysis › Quality by field office | One row per office, its scored visits, its flags by rule ("R1: 6/16", amber under half, red from half) | `office_rule_badges` |
| Analysis › Section performance | Per section, its visits, average quality and High / Medium / Low, a bar coloured by band, and its first 10 visits ("#id ENTITY score rating", lowest score first, unscored after), *Show all* for the rest | `sections`, `visit_entities` |
| Analysis › Visit frequency by location | Visits, average quality and coverage (rated ÷ monitored entities) per place (top 10; *Show all* loads the rest) | `locations` |
| Analysis › Quality by finding rating | Visits, average quality and bands per overall rating; Not monitored always has its own bar | `quality_by_rating` |
| Analysis › Points by category | Each score category's points the scored visits kept of its weight on average (its weight less the visit's deductions in it, each at most the weight), weakest first; the rules that are a flag only listed under it | `dimension_breakdown`: `Visit.category_deductions` |
| Analysis › Programmatic visits and HACT | For the partners of the filter with programmatic visits required: required, planned and completed in eTools, NeuroDB's completed programmatic FM visits, and the gap | `hact_programmatic`: `PartnerHACTYear`, `datamart.fm.programmatic_visits_by_partner` |
| Analysis › Follow-up | FM action points of these visits (open, overdue, high priority) and the off-track or constrained visits without one | `action_points`: `fmm.VisitActionPoint`, `datamart.ActionPoint` |
| Visits › Find a visit | An id, "#1722", "Visit 1722", a key, a reference or a reference number | `fmm:lookup` |
| Visits › Monitoring visits — detail & flags | The visits, most urgent first, 50 a page (*Per page* 25, 50 or 100, `?page_size=`), with *All rows (CSV)* | `fmm:visits` |
| Visit page | Everything known of one visit (below) | `fmm:visit` |
| Map › Visit locations map | Each visit with a point against the places its programme documents planned, in FMS's legend: actual visit, matched by coordinates, matched by name only, PD locations not visited; the visits not shown for want of coordinates | `fmm.geo.map_points` |
| Drill-down window | The visits behind a chart bar, a chip or a count: visit, date, entity (the counted row's), partner (full name), PD number, location, section, rating, quality, urgency, flags | `fmm:drill`, `visit_entities` |
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
  ("Beqaa") or its key ("bekaa"). The entity type and partner filters keep a visit when one of its
  entities matches; the entity figure then counts the matching rows only. Links from charts add
  drill-downs (month, HACT Q1, score band, flag, flag count, urgency band, place, recurring issue,
  rule, review), shown as removable chips.
- **Reference line**: the filter, when the field monitoring rows were synced, when the scores were
  computed and with which rules version ("recomputing with rules v8" while a rescore waits), a warning
  when the last refresh failed, and *What does quality mean for Lebanon?* (FMS's methodology panel,
  rendered from the rule set the engine applies: the three bands, the score categories and their
  weights, the core rules with their HACT rule, the additional rules switched on with their category
  and deduction, and urgency; rules switched off are not listed).
- **Data notes**, each only when it applies: the section rule differs from the overview's, visits
  placed in the governorate through their monitoring site only, finding rows without an activity
  reference (counted as their own visits here, not by the overview), visits with no start date
  (dated by their end date), visits with no date at all.
  With no filter, the visits of a year equal the overview's field monitoring visits and the field
  monitoring page's "Monitoring activities", and the monitored entities its "Findings".
- **Morning briefing** (first on the Insights tab): ten tiles over **this year so far**, 1 January to
  today, whatever the page's period (the page's other filters are kept and named): critical flags
  (visits at or above *urgency red*), average quality, low quality visits (scored below the Medium
  band), critical partners (with a visit at or above red; the top five as red chips, each opening the
  partner's critical visits), monitoring visits, and the visits at review status (*Pending report review*: awaiting the
  reviewer's sign-off), submitted, in data collection, assigned and completed. Each tile has its
  definition under ⓘ and opens the visits behind it; *Quality by governorate* gives each
  governorate's average quality and visits this year, each opening its visits.
- **Key figures**: monitoring visits (whatever their status, with the breakdown), monitored entities
  (rated / not monitored), the average quality score of the scored visits, and the visits of high
  urgency (red, with the amber ones, and *View urgent visits →*). Each links to the Visits tab. Figures are kept 10 minutes, and a
  refresh or a new day shows at once.
- **Critical visits requiring attention** (Insights tab, after the AI brief): the scored visits at or
  above red urgency, most urgent first, at most 10 (the five most urgent amber ones when none is red),
  each with the rules it failed; R19 counts the monitors not on the staff list, never naming them.
- **Quality tab**, in FMS's order: the quality score trends (average quality score and reports per
  month, two smooth lines on two axes), the monitoring volume over time (visits per month as bars,
  their average quality as a line), the HACT Q1 finding rating distribution (visits, each counted once
  with its worst Q1 answer; the overall finding rating instead, with a note, when no visit of the
  filter has a Q1 answer; the drill-down pills count each rating, and Not Monitored apart), the places
  visited (top 10, *Show all*); then the top recurring issues (flags grouped by rule and reason, with
  their visits and mean urgency), the quality issues summary (rating-quality flags, Not monitored:
  planned, not conducted, and visits with three or more flags). The quality score distribution, the
  rule score trends, the rule analysis and the flag counts are first on the Analysis tab. Every chart card has *Download PNG* and *PDF* (the card alone, printed or saved as a PDF).
- **Analysis tab**: first, in FMS's order, the quality score distribution (five bars of 20 points in
  FMS's colours, 100 in the top one, and the visits not scored), the rule score trends (per rule, the
  share of its maximum points earned by the visits of each month; worked out when the panel scrolls
  into view), the quality rule analysis (each rule that checked visits: its visits flagged and a bar of
  the share not flagged; every rule under it, "not available" when the checklist answers are missing,
  see Fields found), the quality flag frequency, the flag count distribution, entity performance (All
  by default), quality by field office and section performance (first 10 visits, *Show all*); then
  highlights (visits, reported, governorates covered out of the gazetteer's,
  average quality: the same figure as the key figure, off-track visits, PSEA-flagged visits out of
  those with a PSEA question, the High / Medium / Low shares, the monitored entities by type), the
  governorates not visited, the field offices (a visit to a PD with two offices counts in both), the
  visit frequency and coverage (rated ÷ monitored entities) by place,
  the quality by finding rating (Not monitored always its own bar), the points earned per rule (weakest first; a rule
  at 0 points is a flag only), the HACT programmatic visits of the partners of the filter (required,
  planned and completed in eTools next to NeuroDB's count of completed programmatic FM visits; gap =
  required − completed in eTools) and the follow-up (FM action points of these visits: open, overdue,
  high priority; the off-track or constrained visits without one).
- **Drill-down window** (`/fmm/drill/`): a chart bar, a chip or a count opens the visits behind it,
  most urgent first (50 at most), with *Open in the Visits tab* for the rest. Charts carry codes
  (`month=2026-05`, `bucket=80-90`, `hact_q1=constrained`, `flag=R1`); an address with a label as a
  chart draws it ("May 2026", "80–100", "On track") is refused, so a cut or translated label can never
  open the wrong visits.
- **Visits tab**: *Find a visit* (an id, "#1722", "Visit 1722", a key, a reference or a reference
  number; a miss offers the three nearest ids of the filter), then the table, 50 rows a page, most
  urgent first (sortable by date, partner, quality and urgency). Red rows are at or above the red
  urgency threshold, amber rows between amber and red (Score settings). The Team column shows names
  only, is hidden on phones and is never copied or exported. *All rows (CSV)* gives every visit of the
  filter with its references, dates, status, partner, programme documents, place, sections, offices,
  rating, HACT Q1, quality, flags, urgency, action point counts and review, and never the team, the
  visit lead or a narrative.
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
- **Visit page** (`/fmm/visits/<key>/`, also a window from the table; the address AI answers will
  cite): status and rating with their dates, HACT Q1, quality and its basis, urgency and its parts,
  the follow-up signals (an off-track or constrained visit without an action point, overdue and
  high-priority action points, a late report: shown apart, never part of urgency), links (partner,
  programme documents, HACT assurance, *On the map* when the visit has a point, the visit's action
  points, and *Open in eTools* once `FMM_ETOOLS_ACTIVITY_URL` is set), place and how it was located, the
  monitoring modality and programme areas, the visit goals and objective, sections and offices with
  their source, the team (names only, shown to NeuroDB users only), each entity with its rating, HACT
  Q1, programme document match, the narrative in full and the Q1, Q2 and Q3 answers written on its
  row (e-mail addresses hidden), each rule's
  result, the checklist questions and answers (read from the eTools records when the page opens),
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
| Monitoring visits (key figure) | Every visit of the period, whatever its status, as the overview counts them. |
| Monitored entities | The finding rows of the visits (the partners, programme documents and CP outputs monitored); "Findings" on `/field-monitoring/`. An entity filter counts the matching rows only. |
| Entity rated | Its rating reads On track, Constrained or Off track. Every share of ratings (key figures, charts, highlights, the AI facts, the brief written by NeuroDB, the chat's look-ups) is a share of the rated visits or entities only. |
| Status group | planned (draft, checklist, review, assigned), in progress (data collection, report finalization), reported (submitted, completed), cancelled, status unknown. |
| Visit status | The most advanced status of its rows; rows that disagree are noted on the visit. |
| Visit rating | Its worst rated entity (Off track, then Constrained, then On track); *Not monitored* when none is rated. |
| HACT Q1 | Of an entity: its own Q1 answer, else the one given for its partner, else the one given for the whole visit. Of a visit: the worst of these and of the visit-level answers. Charts count visits. |
| PSEA flag | A PSEA answer coded in *Answers that flag* (Yes, Constrained or Off track by default) flags the visit; asked and answered otherwise: not flagged; no PSEA question: not known. |
| Not monitored | eTools' rating "Not Monitored": the visit was **planned but not conducted** (the monitor did not attend, the partner was unavailable, access was denied), a planning status, not a programme outcome. A visit is Not monitored when it is reported and none of its entities is rated; a planned or in-progress visit with blank ratings is "not rated yet". It is always a count apart, never inside a share of ratings and never a share of all visits. |
| Scored visit | A visit whose status is one of the *scored statuses* (Score settings; report finalization and completed by default). Every other visit is **pending**: no score, no urgency, quality band "Pending" (cancelled ones read "cancelled"). Not monitored visits of a scored status are scored. |
| Quality score | FMS's, per record (an entity row of a visit): 100 less the deductions of the quality rules that fired, each score category's deductions at most its weight, never below 0, rounded half up to one decimal. A visit's is the mean of its records' scores. A record whose AI checks are not all done is *provisional* and counts as not scored, and so does its visit. |
| Average quality | The mean score of the scored visits of the filter (half up, one decimal): the key figure, the Analysis highlight, the AI facts and the chat's `fm_summary` are this one figure. |
| High urgency | Urgency at or above *urgency red* (70); amber (Medium) from *urgency amber* (40) to 69; Low below. A visit without a score has no urgency and is in no urgency figure. |
| Governorates covered | Governorates with a visit, out of the gazetteer's active governorates. |
| Open / overdue action point | Status open or in progress (`in_progress`, "in progress", "in-progress": FMS counts them as open); overdue when also past its due date (the action points page's, the overview's, Monitoring insights' and Watch's definition, `ActionPoint.OPEN_STATUSES`). The page's *Open* status filter keeps the ones in progress. |
| FM programmatic visits (NeuroDB) | The completed, programmatic visits that ended in the year, counted once per visit for each partner of its rows. |

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
  the site's year menu keeps the page on the year chosen. Periods read the visit date: the start
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
- **Caching**: each block is kept 10 minutes in the web process's cache, under the filter, the last
  refresh and its rules version, and the day; a refresh, a new rules version or midnight shows at once.
  A drill into the reviews is never kept (a review saved a minute ago counts at once). Each tab and the
  visit window load on their own; the map, the brief, the entity table, the rule score trends and the
  long place lists load only when shown (the entity table and the rule trends when they scroll into
  view, a place list when *Show all* opens).

### How Monitoring insights differs from the overview

With no filter, the visits of a calendar year equal the overview's *Field monitoring visits* and the
field monitoring page's *Monitoring activities* for that year, and the monitored entities its
*Findings* (tests pin these). Where Monitoring insights counts differently on purpose, the data note
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
- **Overview**: under the assurance card's figures, *Field monitoring visits in Monitoring insights*
  opens the page for the overview's year, sections and governorate (`section=` empty when every
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
6. writes the keys, the visits, their records and the rule results together in one transaction: the
   pages see the old visits until it commits, a visit keeps its id while its key stays, and the visit
   reviews are never touched. The visits and rule results go through a temporary table where the
   database user may create one (PostgreSQL allows it by default; otherwise the same rows are written
   the slower way), and a scores-only pass rewrites only the rule results that changed.

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
  either. Ask NeuroDB and NeuroDB Watch never send them.

The caps and quotas every call checks are under *Costs, limits and quotas*, below.

### AI checks of the quality rules

FMS's narrative rules are **AI checks**, made record by record (`fmm.ai.checks`): R3 (Q2's evidence), R5
(Q1 against Q2), R6 (the narrative against Q1, Q2, the rating and the visit's objective), R7 (Q3 and the
action points), R8 (problems without action points) and R32 (key challenges without action points).

- **One check** is one OpenAI call per record (an entity row of a visit, as FMS checks each record) and
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
  0.30 (sent only when the model accepts it, as for the briefs). Nothing is stored at OpenAI.
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
  made per visit. While any is left, each run first carries them over: for a visit with a single record,
  a verdict still up to date against what the visit sent then becomes that record's answer (*carried* in
  *AI check answers*); a visit with several records inherits nothing (a verdict on merged texts cannot be
  given to one record), so its records are provisional until they are checked, and they come first in the
  job's order. Each visit check dealt with is deleted; `carried` and `legacy_checks_left` in the run
  details say how many (the checks of a rule switched off are kept for when it is back on). Once the
  checks are up to date, *Score settings → Re-check carried answers* deletes up to 500 carried answers,
  the oldest first, so the next runs check those records properly while the budget allows; press it
  again for the next batch.
- **Back-fill after the change to records.** The deployment asks for a full refresh and records a new
  rules version ("Scores per record"), so every visit reads "recomputing with rules vN" until it is
  rescored: the morning refresh (05:25) or *Run a job → Monitoring insights* does it, scoring each record
  with its AI checks pending; the AI checks (05:50, or *Run a job → Monitoring insights (AI checks)*)
  then carry over, check this year's records of visits with several records first, and rescore. With the
  defaults this year's records take about one night and the whole history two to three; raise
  `FMM_RULES_DAILY_TOKEN_CAP` for a few days in the App Service configuration to go faster, and watch
  `records_pending` go down in the run details.
- **Costs.** One check uses about 1,500-2,300 tokens (input about 1,100-1,900, output with reasoning at
  most 2,000; a record sends less than a visit of several records did). Six checks per record, once
  (again only when it changes). The checks have their own cap, `FMM_RULES_DAILY_TOKEN_CAP` (2,000,000
  tokens a day, about 900-1,300 checks, counted under *Monitoring insights (AI checks)* in *AI use*), and
  stop at 80% of `AI_DAILY_TOKEN_SOFT_CAP` across every AI feature, so people's questions keep their
  share. When OpenAI says the credit ran out, the AI of Monitoring insights pauses for 6 hours (briefs
  and chat too); 3 failed checks in a row stop the run.
- **Switching off.** *Score settings → AI checks* unticked: the AI rules count as switched off, no check
  is made and no record is provisional (the answers kept are used again when it is back on).
  `FMM_AI=false` does the same for every AI of Monitoring insights. To stop one check only, switch its
  rule off.

### Chat with Data

Under the brief on the Insights tab, staff ask questions about the visits **of the page's filter**
("Which visits were off track and why?"). ChatGPT answers with four look-ups of Monitoring insights
and nothing else: `fm_summary` (counts, optionally by section, governorate, office, partner, month,
rating, rule, entity type or status), `fm_visits` (visit cards), `fm_visit` (one visit: its entities
and their notes, rule results, urgency, action points, HACT context and checklist answers) and
`fm_search` (visits whose notes or answers hold a word, with a snippet).

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
  resolved) with an action taken written in eTools, one OpenAI call reads the description (the issue)
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
  When OpenAI says the credit ran out, the AI of Monitoring insights pauses for 6 hours.

### Exports (FMS §13): Excel, PDF report, Power BI

The page header's **Export** menu exports the filter the page shows (the period, every filter and the
drill-downs; the tab does not matter). It is read-only: every person who can open the page can export,
and nothing is written. Every figure comes from the functions that draw the page (`fmm.metrics`,
`fmm.scope.Scope`), so a file and the page never disagree for the same filter (tests in
`tests/fmm/test_exports.py` compare them).

| In the menu | What you get |
|---|---|
| *CSV (visits)* | The visits table's CSV, as before (`/fmm/visits/?…&export=csv`). |
| *Excel workbook* | `monitoring-insights-YYYY-MM-DD.xlsx` (`/fmm/export.xlsx`), described below. |
| *PDF report* | A printable A4 report (`/fmm/report/`) that opens the browser's print dialog: choose *Save as PDF*. |
| *Power BI package* | `monitoring-insights-powerbi-YYYY-MM-DD.zip` (`/fmm/export-powerbi.zip`): CSV files and a Power Query script. |
| *Power BI live connection…* | Administrators only: the admin's *Power BI keys* (below). |

**The Excel workbook** has eight sheets, numbers as numbers and dates as dates:

- *About*: the filter in words, *Data as of* (the last refresh), how visits are dated (start date, else
  end date), the visit counts and what the columns
  mean (quality score, bands High ≥ 80 and Medium ≥ 50 as set in Score settings, the urgency formula
  and its weights, *Not monitored*), the columns left out and the privacy note.
- *Visits*: one row per visit with the column names of FMS's FMM output (§13.2), so FMS's Power BI
  reports and formulas fit: `id` (the visit key), `country_name` (`FMM_COUNTRY_NAME`), the eTools ids,
  dates, partner (`entity`, `vendor_number`), entity types, programme documents, field offices,
  sections and programme areas (several values separated by `;`), `overall_finding_rating` as the page
  counts it (*Not monitored* only for a reported visit with nothing rated; *Not rated yet* for a planned
  or in-progress visit; blank for a cancelled one), status, modality, `quality_score`, `quality_status`
  (High, Medium, Low or Skipped), `urgency`, `quality_flags`, one score per category (`completeness_score`,
  `_evidence_score`…: the points the visit kept of the category's weight), the place (name, latitude,
  longitude, type, P-code, governorate, district), the narratives and the HACT Q1-Q3 answers (cleaned,
  at most 32,000 characters), the action points (count, open, overdue, their descriptions cleaned and
  their due dates, and `action_points_assigned`, a **count**), `_ai_used` (an AI check of the quality
  rules was applied) and `neurodb_url` (absolute when `SITE_URL` is set).
- *Rule results*: one row per visit and rule: passed, flagged, not checked (the data is missing or the
  AI check is pending) or skipped (does not apply, or switched off), the points lost and whether an AI
  check gave it. The detail is given only for rules whose detail NeuroDB writes itself (an AI check's
  explanation may quote a narrative, and the flag of a "text contains" check writes the text it read, so
  both are left out).
- *Partners*, *Field offices*, *Sections*: visits, rated visits, On track / Constrained / Off track
  (counts and shares of the rated visits), average quality and bands, visits flagged and flags, and the
  open action points of the visits. A visit counts in each of its partners, offices and sections, as on
  the page.
- *Flags*: the visits each rule flagged out of those it checked (the flag frequency chart).
- *Action points*: the field monitoring action points linked to the visits: reference, description
  (cleaned), partner, section, office, priority, due date, status, visits, link confidence, AI verdict and
  PME verification. Never who it is assigned to.

**Privacy** (all exports, the report and the live feed): no file holds the team, the visit lead, a
monitor's e-mail address or who an action point is assigned to (`action_points_assigned_to` is never
written; `action_points_assigned` is a count). Narratives, HACT answers and action point descriptions
are cleaned of the person names NeuroDB knows (read afresh for every file), e-mail addresses, links,
phone numbers and names written after a title, as for the AI (`fmm.privacy.clean`). Large filters are
read 500 visits at a time, a fixed number of queries per 500 visits, so a large filter costs time in
proportion to its visits and never one query per visit.

**The PDF report** follows the "LCO – FMM Analysis" layout: the period, the filters and *Data as of*;
the key figures and the morning briefing; the AI brief of the filter with its parts (or, without one,
the brief NeuroDB writes from the figures, said so); the overall finding ratings and the quality bands;
ratings by section and by field office; the 15 partners with the most visits; the most frequent flags;
the 10 most urgent visits; HACT programmatic visits; the action points (linked, open, overdue, high
priority, NeuroDB's, visits without follow-up, open by section); and the method. Charts are drawn once at
a width that fits A4 portrait and no block is split across pages. The print dialog opens by itself once
the charts are drawn; *Print or save as PDF* opens it again.

**The Power BI package** holds `data/visits.csv`, `data/rule_results.csv`, `data/action_points.csv` and
`data/partners.csv` (the workbook's columns; UTF-8 with a byte-order mark, ISO dates, `.` decimals),
`NeuroDB_monitoring.pq` (a Power Query script that loads the four files with their types, and splits the
`;` columns into `visit_sections`, `visit_offices` and `visit_flags`) and `README.txt` (the steps below and
FMS §13.3's visuals adapted to these columns). There is no `.pbit` template: a template cannot be built
or tested without Power BI, and the script stands in its place. In Power BI Desktop:

1. Unzip the package into a folder, keeping its `data` folder (for example
   `C:\NeuroDB\monitoring-insights\`).
2. *Get data → Blank query*, then *Advanced editor*; paste the whole of `NeuroDB_monitoring.pq`.
3. Set `RootFolder` in its first lines to that folder (ending with `\`) and click *Done*.
4. The query shows seven tables: right-click each → *Add as new query*, then switch off loading of the
   first query; *Close & Apply*. Join `visits[id]` to the `visit_id` of the other tables.
5. *File → Save as* `.pbix`. To refresh: download a new package, unzip it into the same folder, *Refresh*.

**Power BI live connection** (FMS "Connect Live", for scheduled refresh in Power BI Service). The feed
`/powerbi/fmm/<table>.csv` (`visits`, `rule_results`, `action_points`, `partners`) serves the same tables
over **every** visit (no person's section is applied), narrowed by `?year=2026` or `?since=2026-01-01`
(visits dated from that day: their start date, else their end date). It is read with a key, never with a sign-in:

1. Admin → *Monitoring insights* → *Power BI keys* → *Add*: give the key a name (what it is for, e.g. the
   workspace) and save. The next page shows the key **once** (NeuroDB keeps only its SHA-256 hash and its
   first eight characters) and the ready-to-paste Power Query script with this site's address. Copy both.
2. Power BI Desktop: *Get data → Blank query → Advanced editor*, paste the script, *Done*. When asked how
   to connect, choose **Web API** and paste the key. Add the seven tables as queries as above.
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
of every action point* (`?export=xlsx`, `&all=1`) hold the CSV's columns without *assigned_to*, with the
description and the action taken cleaned as above. The CSV keeps its established columns, including who
an action point is assigned to (shown to staff on the page already). *PDF report*
(`/action-points/report/?<filter>`) prints the filter in words (a name typed in *Assigned to* is not
repeated), the key figures, the six charts, the action points by module and, with Monitoring insights,
the AI verdicts.

### What goes to OpenAI, and what never does

Nothing goes while `FMM_AI` is off. Once it is on, the brief and the chat send:

| Sent to OpenAI (briefs and chat) | Never sent |
|---|---|
| The period and filter (dates, section, governorate and office names) | The visit lead, team members and monitors (names or e-mail addresses), from any source |
| Figures: visits, entities, rated and not monitored, rating counts, quality averages and bands, rule results, shares worked out by NeuroDB | E-mail addresses, phone numbers and links, also removed from inside every text |
| Up to `comp` quality flags (rule, reason, visits); the 15 most urgent visits and the flags' example visits as cards (dates, partner, PD reference, place, sections, rating with its date, HACT Q1, quality, flags, urgency, action point counts) | Action point assignees, PD focal points, partner staff, eTools user ids |
| Up to `narr` texts per brief or per chat answer (monitors' notes, checklist answers, search snippets), each cleaned and cut to `FMM_NARRATIVE_CHARS` (600) | The finding records as eTools holds them, raw answers, attachments, coordinates (only place names go) |
| A keyed hash of the person (`safety_identifier`) on runs a person starts; `store=False` always | Child-level or Makani data; the user's name, e-mail address or id |
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
`AI_ASSISTANT_ENABLED` and a published prompt version); it is not paused (6 hours after OpenAI said
the credit ran out); Monitoring insights' own caps for the whole office, `FMM_DAILY_TOKEN_CAP`
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
COPY, and a scores-only pass rewrites only the rule results that changed. The *Preview effect* of a
rule change rescores the year's visits twice in memory inside the admin request: about 9-10 s at that
size, 16-18 s since each record is scored. If the
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
| `FMM_MODEL` | (blank) | The model of a prompt version that names none; blank: `AI_ASSISTANT_MODEL`. |
| `FMM_SAMPLING` | `auto` | `auto`: send temperature/top-p when a version sets them and the model has not refused them; `off`: never send them. Another value stops the start-up. |
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
| Score settings › *AI model* | (blank: `AI_ASSISTANT_MODEL`) | The model of the AI checks. |
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
