# Exports

## The Export menu of Monitoring insights

The page header's **Export** menu exports the filter the page shows: the period, every filter and the
drill-downs (the tab does not matter). Every person who can open the page can export, and nothing is
written. Every figure comes from the same functions that draw the page, so a file and the page never
disagree for the same filter.

| In the menu | What you get |
|---|---|
| CSV (records list) | The records list as a CSV file, one row per record; grouped by visit, one row per visit. |
| Excel workbook | `monitoring-insights-YYYY-MM-DD.xlsx`, described below. |
| PDF report | A printable A4 report that opens the browser's print dialog: choose *Save as PDF*. |
| Power BI package | A zip of CSV files and a Power Query script for Power BI Desktop. |
| Power BI live connection… | Administrators only: the keys of the live connection. |

## The Excel workbook

Nine sheets, numbers as numbers and dates as dates:

- **About**: the filter in words, *Data as of* (the last refresh), what a visit and a record are, the
  visit and record counts, what the columns mean (quality score, bands, the urgency formula, Not
  monitored), the columns left out and the privacy note.
- **Records**: one row per record (one entity assessed in a visit: a partner, CP output or PD/SSFA), as
  FMS's own export, with FMS's column names: its entity and type, rating, quality score and band,
  urgency, flags, category scores, narrative and HACT Q1–Q3 answers (cleaned) are the record's own; its
  place is its own when eTools gave one, else its visit's; the dates, status, sections, field offices
  and action points are its visit's, repeated on each of its records as FMS does. `visit_id` is the
  visit's id on the Visits sheet and `neurodb_url` opens the record on its visit's page. This is the
  sheet that gives FMS's figures (an average quality over records).
- **Visits**: one row per visit with the column names of FMS's export, so FMS's Power BI reports fit:
  the visit, the eTools ids, dates, partner, entity types, programme documents, field offices,
  sections and programme areas (several values separated by `;`), the overall finding rating as the
  page counts it (*Not monitored* only for a reported visit with nothing rated, *Not rated yet* for a
  planned or in-progress visit), status, modality, quality score and band (High, Medium, Low or
  Skipped), urgency, the flags, one score per category (the points the visit kept of the category's
  weight), the place, the narratives and the HACT Q1–Q3 answers (cleaned), the action points (count,
  open, overdue, their descriptions cleaned and their due dates, and how many are assigned, as a
  count), whether an AI check was applied, and the visit's NeuroDB address. A visit's figures come
  from its records: its quality is the mean of its scored records (with `lowest_score`, `records` and
  `records_scored` beside it), its rating its worst record's and its urgency its most urgent record's.
- **Rule results**: one row per record and rule (`record_id`, `visit_id`): passed, flagged, not checked
  (the data is missing or the AI check is pending) or skipped (does not apply, or switched off), the
  points lost and whether an AI check gave it. The detail is given only for rules whose wording NeuroDB
  writes itself.
- **Partners**, **Field offices**, **Sections**: records and their visits, rated records, On track /
  Constrained / Off track (counts and shares of the rated records), average quality per record and
  bands, visits and records flagged and flags, and the open action points of the visits. A record counts
  under its own partner and in each of its visit's offices and sections, as on the page.
- **Flags**: the records each rule flagged out of those it checked.
- **Action points**: the field monitoring action points linked to the visits, with their description
  cleaned, link confidence, AI verdict and PME verification. Never who an action point is assigned to.

## Privacy of every export

No file holds the team, the visit lead, a monitor's e-mail address or who an action point is assigned
to. Narratives, HACT answers and action point descriptions are cleaned of the person names NeuroDB
knows, e-mail addresses, links, phone numbers and names written after a title (Mrs, Dr...), as for the
AI.

## The PDF report

It follows the "LCO – FMM Analysis" layout: the period, the filters and *Data as of*; the key figures
and the morning briefing; the AI brief of the filter (or the brief NeuroDB writes from the figures, said
so); the overall finding ratings and the quality bands; ratings by section and by field office; the 15
partners with the most visits; the most frequent flags; the 10 most urgent visits; HACT programmatic
visits; the action points; and the method. The print dialog opens by itself once the charts are drawn;
*Print or save as PDF* opens it again.

## The Power BI package

A zip with the records, visits, rule results, action points and partners as CSV files (UTF-8, ISO
dates, `.` decimals), a Power Query script that loads them with their types, the records first, and a
README with the steps. In Power BI Desktop: unzip it into a folder, *Get data → Blank query → Advanced
editor*, paste the script, set the folder in its first lines, then add each of the eight tables as a
query. To refresh, download a new package into the same folder and press *Refresh*.

**Changed with records (Release 2 step 5).** `records.csv` is new: one row per record, as FMS's export;
build on it to get FMS's figures. `visits.csv` keeps its columns, now at visit grain from its records
(its quality is the mean of its records), so a report built on it keeps refreshing.
`rule_results.csv` is one row per record and rule.

## Power BI live connection

For a scheduled refresh in Power BI Service, NeuroDB serves the same tables (records, visits, rule
results, action points, partners) over every visit, read with a key instead of a sign-in. An administrator creates a key in the administration (it is shown once,
with a ready-to-paste Power Query script), and revokes it there. Each key has an hourly limit of
requests. Ask an administrator for a key: keys are never sent by e-mail or chat, and the Help assistant
never shows one.

## Action points exports

On the [action points page](/action-points/): *CSV of the filter*, *CSV of every action point*, *Excel of
the filter*, *Excel of every action point* (without who an action point is assigned to, texts cleaned)
and a *PDF report* of the filter (see [Action points](/help/action-points/#exports)).

## Other pages

Most tables have *Copy* (paste into Excel or a document) and *CSV*; columns marked as not exported (such
as a visit's team) are left out. The charts of Monitoring insights and of the action points page have
*Download PNG*. Pages with a *Print* button print without
the navigation.
