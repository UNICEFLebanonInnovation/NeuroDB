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
  (Bridging), youth programmes, funds, assurance, field monitoring, **Monitoring insights** (marked
  AI; [Monitoring insights](/help/monitoring-insights/)) and **Action points**
  ([Action points](/help/action-points/)).
- **Partner progress (eTools)**: partner monitoring (PD indicators by month and location) and the
  partners' progress reports.
- **Resources**: population, library, knowledge base
  ([Knowledge base and documents](/help/knowledge-and-documents/)), maps and Data health.
- **Help**: this guide.

## Roles

Every signed-in person has one role:

| Role | What it adds |
|---|---|
| Viewer | Reads every page, asks Ask NeuroDB, the Help assistant and Chat with Data, and exports. Nothing is changed. |
| Section editor | Adds documents to the knowledge base; marks the visits of their section reviewed; verifies the action points of their section; adds NeuroDB action points. |
| Administrator | Everything a section editor does, for every section, plus the administration pages: settings, quality rules, prompt versions, scheduled jobs and the buttons that run them. |

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
runs failed. Pages show the date of their data next to every status ("On track · reported for Jun
2026", "status as of 3 Oct 2026").
