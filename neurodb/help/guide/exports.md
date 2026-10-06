# Exports

## The Export menu of Monitoring insights

The page header's **Export** menu exports the filter the page shows: the period, every filter and the
drill-downs (the tab does not matter). Every person who can open the page can export, and nothing is
written. Every figure comes from the same functions that draw the page, so a file and the page never
disagree for the same filter.

| In the menu | What you get |
|---|---|
| CSV (visits) | The visits table as a CSV file. |
| Excel workbook | `monitoring-insights-YYYY-MM-DD.xlsx`, described below. |
| PDF report | A printable A4 report that opens the browser's print dialog: choose *Save as PDF*. |
| Power BI package | A zip of CSV files and a Power Query script for Power BI Desktop. |
| Power BI live connection… | Administrators only: the keys of the live connection. |

## The Excel workbook

Eight sheets, numbers as numbers and dates as dates:

- **About**: the filter in words, *Data as of* (the last refresh), the visit counts, what the columns
  mean (quality score, bands, the urgency formula, Not monitored), the columns left out and the privacy
  note.
- **Visits**: one row per visit with the column names of FMS's export, so FMS's Power BI reports fit:
  the visit, the eTools ids, dates, partner, entity types, programme documents, field offices,
  sections and programme areas (several values separated by `;`), the overall finding rating as the
  page counts it (*Not monitored* only for a reported visit with nothing rated, *Not rated yet* for a
  planned or in-progress visit), status, modality, quality score and band (High, Medium, Low or
  Skipped), urgency, the flags, one score per category (the points the visit kept of the category's
  weight), the place, the narratives and the HACT Q1–Q3 answers (cleaned), the action points (count,
  open, overdue, their descriptions cleaned and their due dates, and how many are assigned, as a
  count), whether an AI check was applied, and the visit's NeuroDB address.
- **Rule results**: one row per visit and rule: passed, flagged, not checked (the data is missing or
  the AI check is pending) or skipped (does not apply, or switched off), the points lost and whether an
  AI check gave it. The detail is given only for rules whose wording NeuroDB writes itself.
- **Partners**, **Field offices**, **Sections**: visits, rated visits, On track / Constrained / Off
  track (counts and shares of the rated visits), average quality and bands, visits flagged and flags,
  and the open action points of the visits. A visit counts in each of its partners, offices and
  sections, as on the page.
- **Flags**: the visits each rule flagged out of those it checked.
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

A zip with the visits, rule results, action points and partners as CSV files (UTF-8, ISO dates, `.`
decimals), a Power Query script that loads them with their types, and a README with the steps. In Power
BI Desktop: unzip it into a folder, *Get data → Blank query → Advanced editor*, paste the script, set
the folder in its first lines, then add each of the seven tables as a query. To refresh, download a new
package into the same folder and press *Refresh*.

## Power BI live connection

For a scheduled refresh in Power BI Service, NeuroDB serves the same tables over every visit, read with
a key instead of a sign-in. An administrator creates a key in the administration (it is shown once,
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
