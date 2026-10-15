# Getting around NeuroDB

NeuroDB brings together the programme monitoring data of the UNICEF Lebanon country office: the
monthly results partners report in ActivityInfo, eTools (partners, programme documents, funds,
assurance, field monitoring and action points), the country programme, the Compiler figures of
Makani, Dirasa and the youth programmes, population figures, a library of studies and maps, and a
knowledge base of documents. Every page reads NeuroDB's own copy of these sources, refreshed on a
schedule (see [When the data is refreshed](/help/overview/#when-the-data-is-refreshed)).

This guide explains how NeuroDB works: what a page shows, where a number comes from, what a rule
checks. Questions about the programme data themselves ("how many visits were off track in Akkar?")
are answered by [Ask NeuroDB](/ask/) and, for the visits of a filter, by Chat with Data in
[Monitoring insights](/fmm/).

## The top bar

- **Search** (Ctrl K, or the slash key): finds the ActivityInfo databases, the Neuro and HPM reports
  and the indicators (master indicators, sub-indicators and ActivityInfo indicators) of the year. When Ask NeuroDB is switched on, the search box also offers to ask your words
  as a question; nothing is sent until you press Ask.
- **Year menu**: the reporting year the pages show. Pages that show one year keep you on the same
  page when you change it; other pages take you to the overview.
- **Dark mode** switch, and your **user menu** (your role and section, Data health, sign out;
  Administration for administrators).
- **?**: opens the **Help assistant** (also Ctrl+Shift+H, or Cmd+Shift+H on a Mac). It answers how a
  page, rule, chart or number works, from this guide and from the live settings. Esc, the ×, or the
  shortcut again closes it.

## The sidebar

The sidebar groups the pages:

- **Overview**: the country overview, the signed-in home page
  ([Overview and briefs](/help/briefs-and-overview/#the-country-overview)).
- **Management brief**: the overview read for decisions
  ([Management brief](/help/briefs-and-overview/#the-management-brief)).
- **Country programme**: outcomes, outputs, indicators and their progress.
- **Ask NeuroDB** (marked AI): questions in plain language answered from the data
  ([Ask NeuroDB](/help/ask-neurodb/)).
- **For you**: NeuroDB Watch, what falls due and what needs you ([For you](/help/watch/)).
- **What's new**: changes NeuroDB noticed in any source ([What's new](/help/watch/#whats-new)).
- **Monthly results (ActivityInfo)**: the ActivityInfo databases of the year, the year-end forecasts,
  and each database, Neuro report and HPM report of the year.
- **Partnerships**: programme documents, donors, partners, Makani (MSCC), Makani wellbeing, Dirasa
  (Bridging), youth programmes, funds, assurance, field monitoring with the HACT visit plan
  ([Field monitoring and the HACT visit plan](/help/field-monitoring/)), **Monitoring insights** (marked
  AI; [Monitoring insights](/help/monitoring-insights/)) and **Action points**
  ([Action points](/help/action-points/)).
- **Partner progress (eTools)**: partner monitoring (what each programme document indicator reached,
  by month and location) and the partners' progress reports
  ([Partner progress](/help/partner-progress/)); the Funds page is under Partnerships.
- **Resources**: population, library, knowledge base
  ([Knowledge base and documents](/help/knowledge-and-documents/)), maps and Data health.
- **Help**: this guide.

## Pages that open at once

The country overview, the management brief, each partner's page, Data health, partner monitoring,
the *By location* and *By disaggregation* tabs of Progress reports, the Map tab of Monitoring
insights, the country programme, youth programmes, the donor page and three views of the document
review (*By document*, *Themes* and *Repeated findings*) open at once and fill in as their figures
are ready (the other pages are quick enough to come whole, as before):

- The frame comes first: the page's heading, filters and tabs, with a grey box where each block of
  figures will go, about the size of what will be there, so the page does not jump.
- Each box then fills in on its own, the ones you can see first and the ones further down soon
  after. You can read, scroll and change the filters meanwhile.
- A box still working after 8 seconds says "Still working on this part. Large filters take longer."
- A box that could not load says so in its place and has a **Try again** button; the rest of the page
  keeps working. If it fails again, reload the page; if it still fails, tell an administrator which
  page and which box.
- If the page itself could not start (its script was blocked or did not download), every box says it
  could not load and offers **Show the figures** instead: the same page with all its figures in it,
  which takes a little longer to come. The donor page says instead that its charts could not be
  drawn: reload it.
- A link to a block further down (a tab of the brief, a panel of a partner's page) waits for its box,
  then takes you there.
- On a partner's page, everything from the key figures to the last list fills in as one box; the
  Monitoring insights panel follows it, on partners a field monitoring visit has monitored.
- On partner monitoring, the view tabs and the filters come at once; the key figures (or the section
  tiles, or the data quality tiles) fill in first, then the list. Changing a filter, a sort or a
  section tile refills the boxes under the filters. See [Partner progress](/help/partner-progress/).
- On Progress reports, the *By location* and *By disaggregation* tables fill in once the filters are
  there; the other two tabs come whole.
- On Monitoring insights, the Map tab opened from a link or a bookmark shows the filters, key figures
  and tabs first, then the map and its tables. See [Monitoring insights](/help/monitoring-insights/).
- On the country programme page, the cycle, its documents and how much of it has gone come at once;
  the key figures and the outcomes at a glance fill in first, then each outcome with its indicators. A
  link to an outcome waits for its box, then takes you there. Opened again within 10 minutes, the page
  comes complete at once.
- On the youth programmes page, the filters come with the page; the key figures and the youth
  indicators fill in first, then the breakdowns and the two tables below them. The filters still apply
  when you press **Apply**, as before.
- On the donor page (the page donor accounts see, and administrators preview), the cards come at once
  in grey and the charts are drawn in them as soon as the figures arrive; if they cannot be loaded, a
  box says so with a **Try again** button.
- In the document review, *By document*, *Themes* and *Repeated findings* show their tabs, links and
  filters first, then the documents, the themes or the groups. See [Knowledge base and
  documents](/help/knowledge-and-documents/).
- The management brief's **Print** button waits for every box and its charts ("Preparing the
  page…"), then prints. Printing with Ctrl+P before a page is complete prints a short note in place of
  each box still loading: wait until the page is complete.
- The figures are the same as before: only the order in which they appear has changed. See
  [Overview and briefs](/help/briefs-and-overview/) for the overview and the brief.

## Roles

Every signed-in person has one role:

| Role | What it adds |
|---|---|
| Viewer | Reads every page, asks Ask NeuroDB, the Help assistant and Chat with Data, and exports. Nothing is changed. |
| Section editor | Adds documents to the knowledge base; marks the visits of their section reviewed; verifies the action points of their section; adds NeuroDB action points; writes the section comments of the HACT visit plan on the programme documents of their section. |
| Administrator | Everything a section editor does, for every section, plus the administration pages: settings, quality rules, prompt versions, scheduled jobs and the buttons that run them, and the card versions compared with Power BI. |

Your **section** (set on your user by an administrator) is what several pages show first: the
overview, Monitoring insights and the action points page open on your section, with a chip or a
filter to see every section. Members of the **Management** group (filled by administrators) also get
the whole-country view of For you. A **donor account** sees only its own donor page: none of the
pages in this guide, and not the Help assistant.

## Where the data comes from

| Source | What NeuroDB reads |
|---|---|
| ActivityInfo | The databases of each reporting year: forms, master indicators, targets and the partners' monthly activity reports. |
| eTools Datamart | Partners, programme documents, funds reservations and grants, PD indicators and the partners' progress reports, HACT assurance (assessments, audits, spot checks), TPM visits, field monitoring (the findings of each visit, the checklist answers, the programme activities) and action points. |
| eTools locations | The gazetteer: governorates, districts and cadasters with their P-codes. |
| Compiler | Makani and Dirasa figures, youth programme figures and the Makani wellbeing flags (counts only, never a child). |
| Country programme | The results framework and its progress, uploaded on the country programme page. |
| Knowledge base and library | Documents people add, library publications and country programme documents. |

## Why NeuroDB's figures may differ from Power BI

Power BI's Makani, Dirasa and Youth dashboards read three saved queries ("views") of Compiler's database:
`vw_mscc_data`, `vw_bridging_data` and `vw_youth_data_all`. Compiler now counts the same registrations as
those views for NeuroDB, and a line under the key figures of the Makani, Dirasa and youth pages says how
the figures were counted:

- **"Counted from Power BI's view …"**: the same registrations Power BI reads (deleted ones left out), with
  the time Compiler counted them.
- **"Power BI's view could not be used (reason) …"**: Compiler counted with its own rule, so the figures
  may differ from Power BI. The reason says what is missing (often Compiler's right to read the view, which
  an administrator of Compiler's database gives).
- **"Not yet compared with Power BI's view"**: the figures came from Compiler before this comparison
  existed; the next night's count brings it.

Past years are counted again too (once a week), so a past year no longer keeps the figures counted while
it was the current year. Even on the same registrations, a card can still differ, for three reasons:

- **The time of the count**: Compiler counts at night, Power BI at its own refresh. Compare them right
  after Power BI's refresh.
- **What a card counts**: Power BI's exact formula is not always known (each child once or each
  registration, blank answers counted or not, which health question, which list of staff or teachers).
  NeuroDB counts every possible version; **Data health → Compared with Power BI** shows each version's
  number, and an administrator chooses the one that matches (below).
- **"Not specified"**: the percentage charts leave blank answers out, as Power BI's do; the key figures
  still count those registrations.

Dirasa's *Children enrolled* card counts each child once, which is what Power BI's card appears to count,
with the number of registrations under it. The youth page has **All years**, which also counts the
registrations saved before September 2025 without a year.

**Choosing a card's version** (administrators): open Administration → **Power BI cards**, open the card,
type the number Power BI shows for it, choose its period in the list (the periods Data health compares: for
Dirasa the round's name, such as "Bridging 2026-2027", whatever Power BI's page title says; left empty, the
latest period, the one the page opens on), and save. Data health → *Compared
with Power BI* then marks the version that gives that number ("equals Power BI"); choose that version in
the same card and save. The pages, and the country programme's children enrolled, show it at once. A card
whose versions all differ from Power BI's number says "No version equals Power BI's number": tell the
NeuroDB team. The version marked "default" is the one shown until an administrator chooses another; it is
the card's count before this change except for Dirasa's children (before: "Registrations") and in-school
children and CWD (now the schools with registrations only). **Power BI views** shows the
views' definitions, with names and lists of numbers hidden, to administrators only.

## When the data is refreshed

All times are Beirut time. Administrators can change a schedule; the Help assistant reads the
schedules as they are now.

| What | When |
|---|---|
| eTools Datamart sync | Every day at 20:30 |
| eTools locations | Every day at 05:00 |
| Monitoring insights refresh (visits, rules, scores, urgency) | Every day at 05:25, and after every Datamart sync |
| AI monitoring briefs | Every day at 05:40 |
| AI checks of the quality rules | Every day at 05:50 |
| Daily review | Every day at 06:00 |
| AI review of completed action points | Every day at 06:10 |
| Knowledge hub (and new library and country programme documents) | Every day at 07:00, and after every sync |
| What's new note | Every day at 07:30 |
| NeuroDB Watch (For you) | Every day at 07:45, and shortly after new data arrives |
| ActivityInfo data | 18:00 on days 1 to 22 of each month |
| Year-end forecasts | Mondays at 07:15 |
| Compiler figures (youth, education, Makani wellbeing) | Every night, once Compiler is set up |

The **Data health** page (user menu, or the sidebar) shows when each source last synced and which
runs failed. It opens at once (see [Pages that open at once](/help/overview/#pages-that-open-at-once)):
the sync jobs fill in first, then the ActivityInfo databases and the latest runs, then the eTools
datasets, *Compared with Power BI* and the machine learning readiness check. Pages show the date of their data next to every status ("On track · reported for Jun
2026", "status as of 3 Oct 2026").
