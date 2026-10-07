# Monitoring insights

[Monitoring insights](/fmm/) (sidebar: Partnerships › Monitoring insights) turns the eTools field
monitoring data into **visits**: each visit's partners, programme documents, place and sections, how
complete and coherent its report is (the quality rules and a score), how urgent its follow-up is, and
what was done about it (action points). It follows UNICEF's Field Monitoring System (FMS) for Lebanon.

The page reads only what the last refresh built, so it never waits for eTools. The refresh runs every
morning at 05:25 and after every eTools Datamart sync; the reference line under the filters says when
the field monitoring rows were synced and when the scores were computed, with which rules version.

## The tabs

- **Insights**: the morning briefing, the AI monitoring brief, the critical visits requiring attention
  and Chat with Data, in the order of the field monitoring system (FMS).
- **Quality**: how good the reports are month by month, the HACT Q1 ratings and the places visited.
- **Analysis**: in FMS's order, the quality score distribution, the rule score trends, the quality rule
  analysis, the quality flag frequency, the flag count distribution, entity performance, quality by
  field office and section performance; then highlights, places, field offices, quality by rating,
  points by category, HACT programmatic visits and follow-up.
- **Visits**: find a visit, the table of visits (most urgent first) and each visit's page.
- **Map**: the visits against the places their programme documents planned.

Every chart card has *Download PNG* and *PDF* at its foot: *PDF* prints that card alone (choose "Save
as PDF" in the print window to keep it as a file). A chart bar, a chip or a count opens the
**drill-down window**: the
visits behind it, most urgent first (50 at most), with *Open in the Visits tab* for the rest. Each row
shows the visit, its date, the entity (the assessed entity of the row that was counted: with an entity
type or partner filter, the matching row), the partner's full name, the PD number, the location, the
section, the rating, the quality score, the urgency and the flags; never the people.

## Filters

- **Period**: this calendar year by default; last year, a calendar year, this or last quarter, the
  last 30 or 90 days, two dates, or *All time*. A period reads the visit's **start date**,
  else its end date when eTools has no start date (a data note counts those): a visit from 30 December
  to 3 January counts in the year it started, here, on the overview and on the field monitoring page. A
  visit with no date at all is left out of every period and counted in a data note.
- **Section, governorate** ("Not located" too), **field office**, **partner**, **entity type**,
  **rating**, **status**, **monitoring modality** (UNICEF staff, a third-party monitor...),
  **quality band** (High, Medium, Low, or Pending: no score), **urgency band** (High: red, Medium:
  amber, Low: below amber), *Programmatic visits only*, and a search on the visit, its references,
  partners, programme documents and place.
- **Your section**: a person with a section sees it on a bare visit to the page, with a chip "Your
  section: … ×" to show every section. Every link the page writes keeps the section chosen.
- The **entity type** and **partner** filters keep a visit when one of its entities matches; the
  monitored entities figure then counts the matching rows only.
- Chart clicks add **drill-downs** (month, HACT Q1, score band, flag, flag count, urgency band, place,
  recurring issue, rule, review), shown as chips you can remove.

Figures are kept 10 minutes; a refresh, a new rules version or midnight shows at once.

## Key figures

Shown on every tab, for the filter:

- **Monitoring visits**: every visit of the period, whatever its status, with the breakdown reported
  / in progress / planned / cancelled / status unknown. With no filter, the visits of a year equal
  the overview's field monitoring visits and the field monitoring page's "Monitoring activities".
- **Monitored entities**: the finding rows of the visits (the partners, programme documents and CP
  outputs monitored), rated or not monitored. The field monitoring page calls them "Findings".
- **Average quality**: the mean [quality score](/help/monitoring-insights/#the-quality-score) of the
  scored visits, rounded half up to one decimal. The same figure is used by the Analysis highlights,
  the AI brief and Chat with Data.
- **High urgency**: the visits at or above the red [urgency](/help/monitoring-insights/#urgency)
  threshold (70), with the amber ones (40 to 69) beside them, and *View urgent visits →*.

## Insights tab

### Morning briefing

Ten tiles over **this year so far** (1 January to today, whatever the page's period; the page's other
filters are kept and named), tinted as FMS shows them: critical flags (visits at or above red urgency,
red), average quality (yellow), low quality visits (scored below the Medium band, 50; red), critical
partners (partners with a visit at or above red), monitoring visits, and the visits at review status
(*pending report review*, blue), submitted (cyan), in data collection (purple), assigned (indigo) and
completed (green). Each tile has its definition under ⓘ and opens the visits behind it.

- **Top critical partners (this year)**: the five partners with the most critical visits (the visits
  the Critical flags tile counts), as red chips "PARTNER (visits)"; a chip opens those visits. Only
  partners are listed, never a programme document or a person.
- **Quality by governorate (this year)**: each governorate as "NAME · average quality · visits", the
  chip green from 80%, amber from 50%, red below (the quality bands of Score settings).

### AI monitoring brief

A brief of the filter written by ChatGPT (OpenAI's GPT model) from figures NeuroDB works out. The
parts are set by the published prompt version; the Lebanon version has a coverage and quality
summary, key programmatic findings, operational challenges, recommendations and up to five priority
action points ("[PRIORITY: High] Education / partner — action — responsible — timeframe"). Every
sentence rests on facts NeuroDB sent and shows the visits it rests on; a sentence with a figure,
date, reference or person's name the facts do not hold is dropped before anyone reads it.

- Briefs are written every morning at 05:40 for the whole country, each section people land on, and
  the last 90 days. A brief whose data has not changed is shown again at no cost ("Up to date").
- *Regenerate* writes a new one (5 a day per person); the card shows "Writing… about 30 seconds".
- *What was sent* shows the facts and notes exactly as sent (kept 30 days).
- **Generation settings**: what the published prompt version writes the next brief with, read-only:
  the model, its reasoning effort, the most output tokens, the narrative samples, the compliance depth
  (the most frequent quality flags the AI reads), temperature or top-p only when the version sets them
  and the model uses them, and the sections to generate. Administrators get *Edit in admin*.
- The chips under them say what this brief used: model, effort, tokens, temperature and top-p
  (applied or not), the notes and quality flags sent, the prompt and rules versions, and your quota.
- When the AI is switched off, the card shows a brief written by NeuroDB from the figures, and says
  so.

### Critical visits requiring attention

FMS's list of the most urgent visits, under its own heading. The scored visits of the filter at or above red
urgency (70), most urgent first, at most 10; when no visit is red, the five most urgent amber ones. Each
visit is a red-tinted card: its number and partner, its rating, HIGH (red) or MEDIUM (amber) with its
urgency, then one line per quality rule it failed: the rule, its flag and, for an AI check, the AI's
explanation. R19 says how many monitors are not on the field office's staff list, never who. *Show all
N* opens the Visits tab with all the visits of that urgency band.

### Chat with Data

Questions about the visits **of the page's filter** ("Which visits were off track and why?"),
answered by ChatGPT with four look-ups of Monitoring insights: counts, lists of visits, one visit, and
a search of the visits' notes. The model can narrow the filter but never widen it; changing the filter
starts a new conversation. Names, e-mail addresses and phone numbers are removed from your question
before it is sent. A link to a visit stays only when a look-up returned that visit ("Visit 1722 (not
checked)" otherwise), and figures no look-up returned are listed under the answer. 20 questions a day
per person. For questions about how NeuroDB works, use the Help assistant instead.

## Quality tab

| Block | What it shows |
|---|---|
| Quality score trends | Two smooth lines per month (by start date): the average quality score of the scored visits (left axis, 0–100) and the reports (right axis). A point opens the month's visits. |
| Monitoring volume over time | Visits per month as bars (left axis), whatever their status, with their average quality as a line (right axis, 0–100%). |
| HACT Q1 — Finding rating distribution | Visits per month by their worst HACT Q1 answer, stacked: On track (green), Constrained (amber), Off track (red). When no visit of the filter has a Q1 answer, the overall finding rating is shown instead ("Overall finding rating distribution"), with a note. Under it, the drill-down pills On track, Off track, Constrained and Not monitored (counted apart) open their visits. |
| Geographic coverage | The places visited, their governorate, visits and last visit (top 10; *Show all* loads the rest). |
| Top recurring issues | Flags grouped by rule and reason, with their visits and mean urgency. |
| Quality issues summary | Narrative and rating coherence flags (R6), Not monitored visits, and visits with 3 or more flags. |

## Analysis tab

| Block | What it shows |
|---|---|
| Quality score distribution | Scored visits in five bars, as FMS draws them: 0–20 (red), 20–40 (orange), 40–60 (amber), 60–80 (light green) and 80–100 (green, 100 included), along a "Visit count" axis; and the visits not scored. A bar opens its visits. |
| Rule score trends over time | Per rule with points, a smooth line of the share of its maximum points the visits of each month kept ("% of max score"), legend on top (loaded when you reach it). A point opens the visits the rule flagged that month. |
| Quality rule analysis | One row per rule that checked visits, in the order of the rule ids: "15 / 55 visits flagged" and a bar of the share of the checked visits not flagged, green when under 25% were flagged, amber from 25% to 50%, red over 50%. A rule opens its flagged visits. Under it, *Every quality rule* lists all the rules with what each checks, and why a rule shows no figure: "not available" when the data it needs is missing, "AI check pending", "AI check switched off", "off" or "flag only". |
| Quality flag frequency | Every rule that flagged a visit, most first: its id, a bar and "50 (90.9%)", the visits it flagged and their share of the visits it checked (the same counts as the quality rule analysis). A row opens its visits. |
| Flag count distribution | Scored visits with 0 flags (green), 1 flag (blue), 2 flags (amber) and 3+ flags (red): the share inside the bar, the visits on the right. A row opens its visits. |
| Entity performance | All the monitored entities by default (FMS's *All* chip), or only partners, CP outputs or PD/SSFAs: each with its type (CSO partner, CP output, PD/SSFA…), visits, average quality (green, amber or red by band), High / Med / Low, top issue and last rating, worst average quality first, unscored last; a programme document shows its planned visits for the year. Partners are written with their full name. |
| Quality by field office | One row per office, its scored visits on the right and its flags by rule ("R1: 6/16"), amber when under half of its scored visits, red from half. |
| Section performance | Per section, its visits, average quality and High / Medium / Low, a bar of the average quality coloured by band, and its first 10 visits, lowest score first then the unscored ones ("#1067 ENTITY 23% Off track"); *Show all* opens the rest. |
| Highlights | Visits, reported visits, governorates covered (of the gazetteer's active governorates), average quality, off-track visits, PSEA-flagged visits of those with a PSEA question, the High / Medium / Low shares and the monitored entities by type. |
| Governorates not visited | The governorates no visit of the filter is placed in. |
| Field offices | Visits and average quality per field office ("Office not known" too). A visit to a programme document with two offices counts in both. |
| Visit frequency by location | Visits, average quality and coverage (rated ÷ monitored entities) per place. |
| Quality by finding rating | Visits, average quality and bands per overall rating; Not monitored always has its own bar. |
| Points by category | Each score category's points the scored visits kept of its weight on average, weakest first. |
| Programmatic visits and HACT | For the partners of the filter that need programmatic visits: required, planned and completed in eTools, NeuroDB's count of completed programmatic field monitoring visits, and the gap (required − completed in eTools). |
| Follow-up | The action points of these visits (open, overdue, high priority) and the off-track or constrained visits without one. |

## Visits tab and the visit page

**Find a visit** takes an id ("1722", "#1722", "Visit 1722"), a key, a reference or a reference
number; a miss offers the three nearest ids of the filter. The table lists the visits 50 a page
(*Per page* sets 25, 50 or 100, kept in the address), most urgent first, sortable by date, partner,
quality and urgency. Red rows are at or above red urgency,
amber rows between amber and red. The Team column shows names only, is hidden on phones and is never
copied or exported.

The **visit page** shows everything known of one visit: status and rating with their dates, HACT Q1,
the quality score and how it was reached (the deductions per category and each rule's result), the
urgency and its parts, the follow-up signals, links (partner, programme documents, assurance, the map,
the visit's action points), the place and how it was located, sections and offices with their source,
each entity with its rating, HACT Q1, narrative and Q1–Q3 answers, the checklist answers, programme
activities and CP outputs, the action points, the partner's programmatic visits, what the partner
reported on each programme document, and the review.

**Reviews**: an Administrator, or a Section editor of one of the visit's sections, marks a visit
*Reviewed*, *Needs follow-up* or *Data issue*, with an optional note (never sent to the AI). Reviews
are kept by visit, so a refresh never loses them.

## Map tab

The **Visit locations map**: each visit with a point, set against the places its own programme
documents planned, with FMS's legend. A visit is **matched by coordinates** (green) when it is less
than 2 km from a planned place and both points are exact (a monitoring site or a cadaster's own
point); **matched by name only** (purple) when it is in the same location or P-code, or in the planned
district or governorate, but its coordinates do not match; otherwise it is an **actual visit** not
linked to a PD location (blue). A ring is a **PD location not visited**: a planned place no visit of
the filter reached. Chips count each kind, and a note under the map says how many visits are not shown
because no location coordinates were recorded. A point placed at a
district's or governorate's centre is drawn fainter ("approximate"). At most 1,000 points are drawn,
most urgent first. The table under the map lists every point: it is the keyboard route.

## Visits, statuses and ratings

- **Visit**: one eTools monitoring activity, that is the finding rows that share an activity id (else
  an activity reference, else the row alone).
- **Visit date**: the start date of its rows, else (no start date in eTools) their latest end date.
  The rating date ("rated 12 May") and urgency recency still count from the end date.
- **Status group**: planned (draft, checklist, review, assigned), in progress (data collection,
  report finalization), reported (submitted, completed), cancelled, status unknown. A visit's status
  is the most advanced status of its rows.
- **Entity rated**: its rating reads On track, Constrained or Off track. Every share of ratings is a
  share of the rated visits or entities only.
- **Visit rating**: its worst rated entity (Off track, then Constrained, then On track).
- **Not monitored**: eTools' rating "Not Monitored" means the visit was **planned but not
  conducted** (the monitor did not attend, the partner was unavailable, access was denied), not a
  programme outcome. A reported visit with no entity rated is Not monitored; a planned or in-progress
  visit with blank ratings is "not rated yet". Not monitored is always a count apart, never inside a
  share of ratings.
- **HACT Q1**: of an entity, its own Q1 answer, else the one given for its partner, else the one given
  for the whole visit; of a visit, the worst of these. The checklist question that is Q1 ("Have the
  activities been implemented as planned…"), Q2 (activities monitored), Q3 (key observations) and
  the PSEA question are found from the words of their text.
- **PSEA flag**: a PSEA answer of Yes, Constrained or Off track flags the visit; asked and answered
  otherwise, not flagged; no PSEA question, not known.

## The quality score

FMS's score: **100 less the deductions of the quality rules that fired**, each score category's
deductions counting at most its weight, never below 0, rounded half up to one decimal. The visit page
writes it out ("100 less Completeness 19").

### Score categories

| Category | Weight |
|---|---|
| Completeness | 30 |
| Evidence | 20 |
| Alignment | 20 |
| Coherence | 15 |
| Q3 quality | 10 |
| Actionability | 5 |

The weights add up to 100. Administrators can change them; the Help assistant reads the live values.

### Bands

**High** from 80, **Medium** from 50, **Low** below 50. A visit with 3 or more flags is a high-flag
visit.

### Which visits are scored

Only the visits whose eTools status is one of the **scored statuses** (report finalization and
completed) get a score. Every other visit is **pending**: no score, no urgency, and the quality band
filter's "Pending". Cancelled visits read "cancelled". A Not monitored visit of a scored status is
scored.

### Provisional visits

While the AI checks are on, a visit whose AI checks are not all done is **provisional**: the visit page
shows its score so far ("provisional (2 AI checks pending)"), but it counts as not scored everywhere
(no average, band or urgency) until its checks are done, so it never gets full marks for checks not
made.

## Quality rules

Each rule has an id (R1 … R32, never renamed), a name, a type, a score category, a group (*core*:
FMS's six HACT rules; *additional*), on or off, and a deduction: the points it takes off its category
when it fires (a **flag**). A rule's result on a visit is passed, flagged, not available (the data it
needs is missing), does not apply, switched off, or pending (an AI check not done yet).

Rule types:

- **Completeness**: each listed field missing from the report takes its own deduction off.
- **Scoring bands**: one figure scored by bands; the first band that matches gives the deduction.
- **AI check**: ChatGPT reads the fields the rule lists and says whether the report is coherent, with
  one or two sentences why (see [AI checks](/help/monitoring-insights/#ai-checks-of-the-rules)).
- **Reference check**: compares a value with reference data (staff lists, a programme document's
  registered locations, a CP output's sections).

### The rules switched on for Lebanon

| Rule | Category | What it checks | Deduction |
|---|---|---|---|
| R1 Report Completeness (core) | Completeness | The narrative (general observation, 3), the overall finding rating (2), Q1 (2), Q2 (2) and the checklist categories answered (2) are present. | up to 11 |
| R2 Question Answer Completeness | Completeness | The share of checklist questions answered: 80% or more takes nothing off, 50% to 79% takes 5, below 50% takes 10; 3 when the share is not in the data. | up to 10 |
| R3 Narrative Evidence Quality (core, AI) | Evidence | Q2 and the narrative show concrete, independently observed evidence, specific enough to support the findings. | 20 |
| R5 HACT Activities Alignment (core, AI) | Alignment | Q1 (implementation status) agrees with Q2 (activities verified): not On track with no real activities, not Off track with only positive results. | 20 |
| R6 Narrative and Rating Coherence (core, AI) | Coherence | Q1, Q2, the narrative, the overall rating and the visit's goals and objective tell one consistent story. | 15 |
| R7 Action Point Quality (core, AI) | Q3 quality | Q3 and the follow-up actions are concrete, assigned and time-bound where possible, and useful for the issues found. | 5 |
| R8 Action Points Cross-Check (core, AI) | Q3 quality | Problems or recommendations in the narrative, Q1 or Q2 have corresponding action points. | 5 |
| R32 Challenge-to-Action Alignment (AI) | Actionability | Each key challenge in Q1, Q2 and the narrative has a matching action point (a clearly positive visit passes). | 5 |
| R19 Field Office - Team Member Validation | Completeness | The monitor is on the staff list of the visit's field office. Skipped while the office has no list. | 3 |
| R20 Location - PD/SSFA Site Validation | Completeness | For programme document visits, the visited place is one of the programme document's registered locations. | 5 |
| R21 Section - CP Output Alignment | Completeness | For CP output visits, the visit's sections are those of its CP output. | 3 |
| R23 PD Reference Locations | Completeness | The visited place is among the planned locations of the visit's or the partner's programme documents. A flag only: it takes no points. | 0 |

The other rules (R4, R9 to R18, R22, R24 to R31) are switched off. An administrator can switch a rule
on, change its deduction or its parameters; every change is saved as a new **rules version** and the
scores are recomputed in the background ("recomputing with rules v8" until it is done). The Help
assistant reads the rules as they are now; *What does quality mean for Lebanon?* on the page shows
them too.

R20 and R23 read the registered locations of the programme documents in eTools; R21 reads the
sections of the programme documents that name the visit's CP output. The monitors' e-mail addresses
R19 compares are read and dropped at once: never kept, never shown, never sent to the AI.

## AI checks of the rules

The AI rules (R3, R5, R6, R7, R8 and R32) are checked visit by visit, every morning at 05:50, newest
visits first, within the day's AI budget; older visits are checked over the next nights. One check is
one ChatGPT call per visit and rule: the rule's instructions and the visit's fields it lists (each
finding row's entity, type, rating, narrative and Q1–Q3 answers; the visit goals, objective and action
points with their due dates), each text cut to 1,500 characters and cleaned of names, e-mail
addresses, phone numbers and links. The team, the visit lead and who an action point is assigned to
are never sent.

The answer is passed or not, with one or two sentences why. A check that fails adds the rule's flag,
with the explanation, and takes its deduction off. Each answer is kept and used again until the
visit's texts or the rule's instructions change. When the AI checks are switched off, the AI rules
count as switched off and no visit is provisional.

## Urgency

FMS's urgency, from 0 to 100, for **scored visits only** (a pending, cancelled or provisional visit has
no urgency and is in no urgency figure):

**urgency = 0.50 × (100 − quality score) + 0.30 × recency + 0.20 × flags**

| Part | Weight | Value |
|---|---|---|
| Quality gap | 0.50 | 100 − the quality score |
| Recency | 0.30 | 100 on the day the visit ended, falling in a straight line to 0 at 180 days after it |
| Red flags | 0.20 | 25 per rule that fired (R23 included), at most 100 |

The total is rounded half up and kept between 0 and 100. **Red** from 70, **amber** from 40. The
weights, the 180-day window and the thresholds are score settings; the Help assistant reads the live
values. Each visit keeps its weighted parts, shown on the visit page ("Why urgency 33: quality gap 29.8
· recency 12.2 · red flags 15") and when you point at the urgency in the table. The 05:25 refresh
recomputes urgency every day, so a visit grows less urgent as it ages, unless its quality is low and
its flags many.

## Follow-up signals

Shown on the visit page, never part of urgency: an Off track or Constrained reported visit with no
action point more than 14 days after it ended; its overdue, high-priority overdue and high-priority
open action points; and a planned or in-progress visit that ended more than 30 days ago (a late
report).

## Where the figures come from

All from the eTools Datamart sync:

- **Field monitoring findings**: one row per entity of a visit: the activity id and reference, dates,
  status, the entity (partner, programme document or CP output) and its type, the rating, the
  narrative (general observation) and the HACT Q1, Q2 and Q3 answers written on the row, the location
  and monitoring site, sections, offices and modality.
- **Checklist answers and answer options**: the questions asked and their answers, which give HACT
  Q1–Q3, PSEA, the share of questions answered, the categories answered, the collection methods and
  the red-flag answers (an answer of 2 or less on a 5-point scale, 1 on a 3-point scale).
- **Programme activities** of each visit, and the **action points** linked to the visit (by its eTools
  activity id, else its reference).
- **Programme documents**: their registered and planned locations (R20, R23 and the map) and their
  sections and CP outputs (R21).

When a field cannot be read in the data at all, its rules read "not available" on every visit rather
than taking points off.
