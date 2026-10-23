# Field monitoring and the HACT visit plan

[Field monitoring](/field-monitoring/) (sidebar: Field monitoring; in the full menu, under Funds and
assurance) shows what eTools holds about monitoring the partners: the findings of field monitoring visits,
the third-party monitoring (TPM) visits and activities, the UNICEF staff trips, and the **HACT visit
plan**: for each programme document, the programmatic visits planned for the year, those completed and
those still needed. Every figure comes from the eTools Datamart, synced every night at 20:30.

The HACT visit plan answers the programme monitoring team's HACT FMM plan e-mail from NeuroDB
itself: the programme documents not visited since 1 January, the visits still needed for the
end-of-year target, the list of programme documents and partners with no visit, and the sections'
comments on the missing visits.

## The tabs

- **Findings and visits** (the page as it opens): the field monitoring findings (one row per partner,
  programme document or CP output assessed on a visit; [Monitoring insights](/help/monitoring-insights/)
  calls them records), the TPM visits and activities, and how many UNICEF staff trip activities eTools
  holds. A one-line strip at the top sums up the HACT visit plan of the year: how many programme
  documents (of how many partners) are not visited yet, how many visits are still needed (for how many
  partners), and *Open the plan →*. It follows the year of the filter (this year when none is chosen)
  and is not shown when eTools has no planned visits for that year.
- **HACT visit plan**: one row per programme document of the year, with its plan, its completed
  visits and what is still needed, the four headline figures above the list, and a *By section*
  table.

Both tabs are plain links that keep the year. Every signed-in staff member can open them; donor
accounts cannot.

## Which visits count

A visit counts for a programme document when eTools marks it **programmatic** and UNICEF has
**approved its report**. There are two kinds:

- **Field monitoring visits** (eTools Field Monitoring): completed, marked programmatic, in the year of
  their **end date**. A visit counts for a programme document when one of its findings is about that
  programme document. A visit with two findings about the same programme document counts once; a
  visit about two programme documents counts once for each. These are the same visits the
  [Assurance](/assurance/) page counts per partner as "FM programmatic visits (NeuroDB)".
- **TPM visits** (eTools Third Party Monitoring): marked programmatic and approved by UNICEF, in the
  year of the activity's **date**. Two activities of one TPM visit count once.

**Completed** adds the two. These are shown under it, so that a zero is explained, but are **not
counted**:

- "awaiting approval": field monitoring reports submitted and TPM reports sent by the monitoring
  company, which UNICEF has not approved yet;
- "FM not marked programmatic": field monitoring visits completed but not ticked "programmatic
  visit" in eTools;
- "staff trips (not counted)": UNICEF staff trip activities typed programmatic. eTools sends them
  without a status, so a planned or cancelled trip cannot be told from one that took place; since
  eTools Field Monitoring, programmatic visits are reported there.
- "FM visits to the partner naming no PD (not counted)": completed programmatic field monitoring
  visits whose findings are only about the partner or a CP output, never one of its programme
  documents. The [Assurance](/assurance/) page counts them for the partner; the plan cannot tell which
  programme document they were for, so it counts them for none and shows them on each of the
  partner's programme documents.

A visit counts as soon as the nightly sync brings its approval: the plan is computed when you open
it, never kept from an earlier day.

## Required, still needed and due

- **Required**: the programmatic visits eTools plans for the programme document in the year, Q1 +
  Q2 + Q3 + Q4 of its planned visits (the programme document page shows the same quarters). It is 0
  when eTools plans none.
- **Q1 to Q4**: the visits planned in each quarter. In the current year, this quarter's heading is
  highlighted.
- **Counted** ("completed out of the minimum"): Completed, at most Required.
- **Still needed** (by 31 December): Required less Counted. It is never negative: a programme
  document visited more often than planned shows 0, never −1. A surplus on one programme document
  never makes up for a gap on another, even of the same partner.
- **Due by the end of the quarter** (the heading names the quarter: "Due by end of Q4" from October):
  the visits planned up to the end of the current quarter less the completed ones, never negative.
  For a past year it is the whole year (Q4); for a future year nothing is due yet.

The HACT year is the calendar year, 1 January to 31 December, and a quarter is a calendar quarter
(January to March is Q1).

## Remarks

Each programme document gets one remark, the first that applies:

| Remark | When |
|---|---|
| No visit planned | Required is 0. "N completed visits" under it when some visits were completed all the same. These programme documents are never counted as not visited or as needing visits. |
| Completed | Nothing is still needed. |
| PD ended, visits missing | Visits are still needed and the programme document has ended (ended, closed, terminated or expired in eTools) or is past its end date. |
| HACT FMM needed | Visits are still needed. |

"not visited yet" under the remark marks a programme document with visits planned and none
completed this year.

## The headline figures

The four figures above the list count the programme documents of the filter bar. The quick views do
not change them, nor the *By section* table.

- **Not visited in** the year shown: programme documents with visits planned and none
  completed, and how many partners they belong to. Under it: how many of those partners have no
  completed visit on any of their programme documents of the year (those the filter leaves out
  included), and how many of these programme documents have ended. It opens the *Not visited* quick
  view.
- **Visits still needed by 31 Dec**: the visits still needed, added up over the programme documents
  that still need some, with how many programme documents and partners that is, and how many of the
  visits are on programme documents that have ended. Ended ones are included: the visits planned on
  them were not made. It opens the *End-of-year missing visits* quick view.
- **Programmatic visits completed**: the visits counted out of the visits required. Under it: every
  completed visit (those beyond a programme document's plan, or on one with no visit planned, too) and
  the visits awaiting approval.
- **Partners behind their HACT minimum (eTools)**: partners of the programme documents of the filter
  whose eTools HACT year has fewer programmatic visits completed than required, out of those with a
  HACT year in eTools. It is the partner-level requirement, shown beside the programme document
  figures so that the two can be compared (see
  [Why a figure can differ](/help/field-monitoring/#why-a-figure-can-differ)). It is not the same
  count as "partners behind their minimum requirements" on the [Assurance](/assurance/) page it links
  to, which can be higher: this one counts programmatic visits only, and only the partners of the
  programme documents in the list; Assurance counts spot checks too, and every partner with a HACT
  year, whether or not it has a programme document in the list.

### The figures of the HACT FMM plan e-mail

| The e-mail says | On the HACT visit plan |
|---|---|
| "N PDs for M partners have not been visited since the beginning of the year (or no FMM/HACT report recorded)" | *Not visited in* the year: programme documents with visits planned and no completed visit counted (a report awaiting approval does not count yet) |
| "N visits across M partners are still needed to reach the end-of-year target" | *Visits still needed by 31 Dec* and its partners |
| The list of programme documents and partners with zero visits | The *Not visited* quick view |
| The end-of-year missing visits, with a column for the sections' comments | The *End-of-year missing visits* quick view, and its *Section comment* column |
| Required PV, Completed PV, Completed PV out of minimum, the visits needed, Q1 to Q4 | Required, Completed, Counted, Still needed, Q1 to Q4 |

## Quick views and filters

- **Quick views**, above the list, each with its count: *Not visited* (visits planned, none
  completed), *End-of-year missing visits* (the remarks "HACT FMM needed" and "PD ended, visits
  missing") and *All*. They keep the rest of the filter.
- **The filter bar**: search (partner, vendor number or reference), year, section, partner, risk
  rating, remark, PD status and document type. It opens on every section, as the programme monitoring
  team works country-wide: choose a section to see yours. *Reset* keeps you on the tab. On a phone the
  filter bar folds. When you change the year, its lists follow the year, and what you chose stays
  chosen, even a partner or a section with no programme document that year: the list then says that
  no programme document matches, until you take it off.
- In the *By section* table, a section's name filters the list on it. A programme document with two
  sections counts under each; the total counts it once. When you choose a section, the table keeps
  only the sections you chose.
- The line under the quick views says which eTools sync the figures are as of. When eTools sent no
  planned visits for the year, a warning says so (Required is then 0 everywhere) with a link to Data
  health. When eTools sent planned visits for the year whose programme document reference matches no
  programme document in NeuroDB, a warning says how many rows and how many visits: they are in no
  figure until the reference matches (see
  [Why a figure can differ](/help/field-monitoring/#why-a-figure-can-differ)).

The list holds every programme document of the year: not a draft or cancelled, and running at some
point in the year, or with visits planned for it, or with a visit counted in it. Above 1,000
programme documents it asks you to choose a section or a partner first; the files still hold them all.

## The list

### By programme document

One row per programme document, sorted by partner then reference, as in the workbook (click a
heading to sort by another column):

- **Partner** (opens the partner's page) with its vendor number, **Reference** (opens the programme
  document), **Status**, **Sections**, **Type**, **Start**, **End**, **Total budget** (USD when eTools
  gives no currency).
- **Risk**: the partner's HACT risk rating of the year in eTools, else the partner's rating (the line
  under it says which).
- **Required**, **Completed**, **Counted**, **Still needed**, **Q1 to Q4**, **Due by the end of the
  quarter**, **Remark** (see above) and **TPM** (Yes when a TPM visit of the year, not a draft or
  cancelled, covers the programme document).
- **Completed** opens the visits behind it: with Monitoring insights on, the programme document's
  reported programmatic field monitoring visits there (they include reports still awaiting approval
  and are dated by their start date, so that count can differ). Monitoring insights holds no TPM
  visit: *TPM* under the figure opens the programme document's TPM activities on the first tab, and
  so does the figure itself when TPM visits are all there is to list. With Monitoring insights off,
  Completed opens the programme document's findings and TPM activities on the first tab, searched by
  its PCA and PD numbers so that a finding naming it with another country prefix or amendment is
  found too. That tab dates a field monitoring visit by its start date: a visit that started in
  December and ended in January is counted in January's year but listed under December's.
- **UNICEF focal points**: their names, as eTools holds them (never an e-mail address), on screen only:
  hidden on a phone, never copied or exported. To correct a focal point, correct it in eTools: the
  next nightly sync shows the change.
- **Section comment**: see below.

### By partner

*By partner* (above the list) shows one row per partner: its programme documents and those not
visited, the sums of Required, Completed and Still needed over its programme documents (no surplus
making up for a gap), its field monitoring visits that name none of its programme documents (not
counted), beside its **eTools HACT minimum**: required, completed and the gap, with a link to
Assurance. Two notes flag the difference between the two levels: "Partner minimum met in eTools,
PD visits still needed", and "PD plans below the partner minimum" (the plans of all the partner's
programme documents of the year, even those the filter leaves out). The number of programme
documents opens them in the list.

## Section comments

The *Section comment* column holds the section's comment on the programme document's missing
visits, one per programme document and year, up to 500 characters (line breaks become spaces). The
date under it says when it was last changed, never who changed it.

- **Who may write**: Administrators, and Section editors of one of the programme document's sections.
  The **Comment** (or **Edit**) button shows only to them. eTools spells sections its own way: an
  eTools section name the same as a NeuroDB section's is matched by itself, any other is matched by an
  administrator, and until then its Section editors do not get the button (ask an administrator).
- Viewers read the comments; nobody else can write them.
- **Save** keeps the comment and the row updates in place; **Remove**, or saving an empty comment,
  removes it.
- **What not to write**: people's names or contact details. Every signed-in staff member sees the
  comment, and it is written into the Excel and CSV files.
- Comments stay across the nightly syncs and are kept separately for each year. They are never sent
  to the AI.

## Export and privacy

The toolbar above the list has:

- **Copy** and **CSV** of the table as shown (the focal points column is left out);
- **Excel of the filter**: every programme document of the filter, even above 1,000, in four sheets:
  *HACT visit plan* (one row per programme document, with the workbook's columns and NeuroDB's
  "not counted" figures), *By partner*, *By section* and *About* (the year, the date of the eTools
  figures, the filter in words and what each column means). Numbers stay numbers and dates stay
  dates.
- **CSV of the filter**: the same rows as the *HACT visit plan* sheet, or the *By partner* rows when
  that view is open.

The files hold the **number** of UNICEF focal points of each programme document
(`unicef_focal_points_count`), never their names, and the section comment's text and date, never who
wrote it. No visit lead, team member, assignee, e-mail address or phone number is ever written.
Ask NeuroDB reads the plan as counts only: never a partner's name, a reference, a comment or a focal
point. For you's reminder about a partner adds only how many of its programme documents need how many
visits.

## Why a figure can differ

The workbook the team circulates is an extract of the eTools HACT dashboard on one day. NeuroDB
computes its figures from the eTools records behind it, so a figure can differ:

1. **Required.** The extract takes the dashboard's required visits for each programme document;
   NeuroDB takes the sum of the programme document's quarterly plan in eTools. The two usually agree,
   but not always. When the dashboard drops a programme document's requirement to 0 because a planned
   visit can no longer be made, NeuroDB keeps the plan: the programme document then shows as "PD
   ended, visits missing" and its visits count in *still needed*. eTools links planned visits to a
   programme document by its reference: planned visits whose reference matches no programme document
   in NeuroDB (another amendment, or a programme document not synced yet) are in no figure, and a
   warning above the list says how many there are.
2. **Completed.** The extract takes the dashboard's own completed figure, whose sources NeuroDB cannot
   see. NeuroDB counts only field monitoring and TPM visits marked programmatic with an approved
   report. Left out, and shown under Completed as "not counted": staff trips, field monitoring reports
   still submitted, visits not ticked "programmatic" in eTools, and a visit whose findings are only
   about the partner or a CP output and never name the programme document (it still counts for the
   partner on the Assurance page; each of the partner's programme documents shows it).
3. **Dates.** NeuroDB dates a field monitoring visit by its end date and a TPM visit by its activity
   date. The extract is fixed on the day it was taken; NeuroDB follows the nightly sync (the line under
   the quick views gives its date).
4. **Which programme documents.** The extract lists the programme documents on the HACT dashboard.
   NeuroDB lists every programme document of the year, so it has more rows; those with no visit planned
   never count as not visited or as needing visits.
5. **Programme document or partner.** The plan adds up each programme document's own plan. eTools'
   partner HACT minimum, set from the risk rating and the cash transfers, is another requirement: a
   partner can meet it while its programme documents still need visits, and the other way round. The
   *By partner* view shows both side by side. Assurance, the HACT block of Monitoring insights and the
   HACT reminder of For you keep reading the partner minimum.
6. **Statuses.** NeuroDB shows eTools' own status names: a status typed "Closed" in the workbook is
   eTools' *closed*.
7. **Negative gaps.** A workbook can show −1 visits needed where more visits were made than required;
   NeuroDB shows 0. Either way such a programme document adds nothing to the visits still needed.

## Where else the plan is linked

- **[Assurance](/assurance/)**, *HACT assurance by partner*: "Visit plan per programme document →".
- **A partner's page**, *HACT by year*: "Visit plan per programme document →", on that partner.
- **A programme document's page**, panel *Field monitoring visits*: "HACT visit plan →", on that
  programme document.
- **Monitoring insights**, Analysis tab, HACT block: it lists only the partners visited in the filter,
  and links to the programme documents not visited on the plan.
- **[For you](/help/watch/)**: from October, the reminder about a partner behind its HACT minimum says
  how many of its programme documents still need how many visits, and links to them on the plan.
- **[Ask NeuroDB](/help/ask-neurodb/)** answers questions such as "how many programme documents were
  not visited this year?" with the plan's counts, and links to the plan.
