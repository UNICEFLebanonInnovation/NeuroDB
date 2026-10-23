# Partner progress (eTools)

The partners' progress reports in the Partner Reporting Portal (PRP) reach NeuroDB through the eTools
Datamart every night. Four places read them: [Partner monitoring](/partner-monitoring/) (what each
programme document indicator reached), [Progress reports](/partner-reporting/) (who reported, and what),
the programme document and partner pages, and the [Funds](/funds/) page (budgets, funds reservations
and what partners contributed). They show the views of the eTools Power BI report, with the same
figures on every page for the same filter.

## Reached: one rule for every page

**Reached** is what a partner reached on one indicator of a programme document, from its progress
reports. Every page, export and Ask NeuroDB answer reads the same rule:

- **Which reports count**: Accepted and Submitted reports. What Submitted reports add is shown as
  *awaiting acceptance*, so that a later send-back is understood. Sent back, Due, Overdue and not yet due
  reports never count, whatever they carry.
- **A location's value in a report** is the total PRP gives for the location (the "()" total of its
  disaggregation). When every location of a report carries the same value, it is the report total
  repeated on each one, and it is not added up over the locations. Two rows for one location are never
  added: the largest is used.
- **Locations and reports are combined the way the indicator says in PRP**: a sum, the largest, an
  average or the latest. A percentage or a ratio is never added: it is the sum of the numerators over
  the sum of the denominators.
- **Different indicators are never added together**, and neither are quarterly and monthly
  humanitarian reports.
- **PRP's own cumulative** is shown beside Reached; an indicator where the two differ is marked
  *Differs from PRP*. A programme document that started before the reports NeuroDB holds (also when the
  Datamart holds no earlier report than the first one NeuroDB has) shows *Partial history*, and its Reached
  is PRP's cumulative.
- **Children reached** on the overview, the brief and the donor page counts one indicator per programme
  document: its *unique beneficiaries indicator* (chosen on the programme document page), else its
  largest children indicator. See [Country overview, management brief and daily review](/help/briefs-and-overview/).

## Report statuses

Every progress report has one of six statuses, the same on every page, so the counts add up to the
total. A progress report is one programme document's report of one type, number and period (QPR2 of
1 April to 30 June); when its rows disagree (some synced before the report changed), the status its rows
were given last is the report's on every page:

| Status | Meaning | Counts in Reached |
|---|---|---|
| Accepted | UNICEF accepted the report. | Yes |
| Submitted | Submitted by the partner, awaiting acceptance. | Yes, shown as awaiting acceptance |
| Sent back | Returned to the partner. | No |
| Due | The period has ended; not submitted yet, and not late. | No |
| Overdue | Not submitted by its due date. | No |
| Not yet due | The reporting period is still running. | No |

## Partner monitoring

[Partner monitoring](/partner-monitoring/) has five views on one filter:

- **Grid**: by section, programme document and output, each indicator with its target, Reached, %
  Reached, Reached in the chosen year, awaiting acceptance, status and the months of the year.
- **Program Performance**: one row per indicator (section, CP output, emergency, target, Reached, %
  Reached, partner, programme document), sorted by % Reached; the section tiles filter it. With a
  reporting year chosen, it shows Reached in that year and its % of the target first (sorted by it),
  as Power BI does, with Reached to date beside them.
- **Data quality**: programme documents and indicators with and without a target or Reached, and the
  lists to fix (missing targets, no or zero Reached, Reached differing from PRP, the same title twice in
  one programme document, partial history). The partner reports carry no PD indicator id, so a report is
  matched to its indicator by title; where two indicators of a programme document share a title, by
  their PD output too; where two indicators of a programme document share a title and an output, NeuroDB
  tells them apart by what the partner reports and eTools both say of them (high frequency, cluster,
  target, type, disaggregations, planned places, active), once the data shows each of these agrees.
  Indicators still alike stay without Reached until told apart in eTools, and so do those whose rows name
  a PD output that matches none of theirs (correct the output name in PRP or eTools): the same-title list
  counts them.
- **Map**: the implementation locations, coloured by the worst status there.
- **Master indicators**: each section's eTools master indicators, one figure a year combined from the
  partners' PD indicators (see [Master indicators](#master-indicators) below).

Switching views keeps the year shown; the year menu at the top offers *All years* on these pages (not on
*Master indicators*, which shows one year at a time). The
**location** filter shows each indicator's Reached at the chosen place (a place is found by its name and
by its P-code, so "Akkar Governorate" in a partner report is the planned "Akkar"); on the map, each row
then gives the place's own Reached and, beside it, the indicator's Reached over every location.

An indicator opens on its own page: a location × report table whose rows end with each location's
Reached and whose columns end with each report's value, the reports that do not count greyed, and
**Reached by disaggregation** (female, male, Lebanese…) for each location, with a subtotal per
disaggregation when the indicator adds up.

The page opens at once: the view tabs and the filters come first, with a grey box where the figures and
the list will go, and each box fills in as soon as its figures are ready (see [Pages that open at
once](/help/overview/#pages-that-open-at-once)). Applying a filter, sorting a column or clicking a
section tile refills the boxes under the filters; the filters stay as they are. The figures are worked
out once for everyone who asks for the same filter and kept for two minutes, so opening a view again, or
another person opening the same filter, is quicker; a new eTools sync shows at once. The map fills in
the same way: the page first, then its locations.

## Master indicators

A **master indicator** brings together the partners' own PD indicators that measure the same thing, as an
ActivityInfo master indicator did: for example "children enrolled in non-formal education" over every
programme document of the section that counts them. Each one is in one eTools section and has, for every
year, its AWP or RWP code and the **target staff type for that year**. Open the **Master indicators** view of
[Partner monitoring](/partner-monitoring/): it shows one year (this year first) and opens on your own
section.

**Only what a section confirmed counts.** Every night NeuroDB proposes master indicators where two programme
documents or more share a CP output, a type and a title, and suggests PD indicators for the master indicators
there are. A suggestion is listed under its master indicator and never added until someone confirms it.
Administrators, and Section editors of the master indicator's section, decide on the page:

- **Accept** a proposed master indicator to make it active with the PD indicators its row lists confirmed
  (one NeuroDB added after you opened the page stays a suggestion, and the page says so); **Dismiss** it and
  it is never proposed again. A proposal with the name of a master indicator the section has already is not
  accepted: rename it first, or have an administrator merge it into that one (an archived one is made active
  again first).
- **Confirm all N suggestions** of one master indicator, or **Confirm all N strong matches (0.9 or more)** of
  a section, in one click: they confirm exactly the suggestions the page shows, as their count says.
  **Reject** a suggestion that is wrong, and it is not suggested for that master indicator again (attach it
  to that master indicator later if you change your mind). A role you give a suggestion, *Shown only* say,
  stays until you confirm it.
- In **Not in a master**, tick PD indicators and **Attach to…** a master indicator, as its total, a numerator,
  a denominator or *shown only*. Each one says what NeuroDB thinks of it: its suggestion, the proposal it is
  in, *Ambiguous* when two master indicators fit it as well, or why it counts nowhere (it is in a master
  indicator not shown this year, for example).
- Open a master indicator to change its name, its code and its **target** for the year, or what a PD indicator
  counts as.

**How the figure is worked out.** Each PD indicator brings its own Reached in the year, read exactly as
Program Performance reads it, so no report is ever added twice.

- **Numbers are added up.** Partners may reach the same people, so the figure reads *up to* the sum, with *at
  least* the largest programme document's figure. Two PD indicators of one programme document are added only
  when they cannot count the same people (one counts girls and the other boys, for example); otherwise only
  the larger one counts. "Girls and boys" next to "boys", or "Lebanese and non-Lebanese" next to "Syrian",
  may count the same children, so only the larger one counts there.
- **Percentages and ratios are the numerators added up over the denominators added up**, never an average of
  the percentages, each programme document counted once (its numerator and denominator, or its largest
  total). A partner who reported the percentage alone, without its numerator and denominator, is
  left out of the figure: its row says *Reported without numerator and denominator*, and the master
  indicator says how many programme documents it counted ("counted 4 of 6 PDs"). A programme document that
  averages its reporting periods or its locations (or takes the largest) has no numerator and denominator
  for the year however the partner reported: it is left out too, and its row says so (*not combined*).
- *Shown only* PD indicators are listed and never counted. Master indicators are never added together.
- **Q1, to Q2, to Q3 and Year** are Reached from 1 January to the end of each quarter.
- **Status** compares the figure with the year's target staff typed, as of the latest counted report. Until a
  target is typed it reads **No target**; the programme documents' own targets are shown in the details for
  reference only.

A PD indicator that is no longer in eTools stops counting and says so; its replacement is suggested the next
night. One whose type changed in eTools stops counting until someone clicks **Confirm again**. With
*Humanitarian reports (monthly)* or a location chosen, a PD indicator the filter does not read (one neither
high-frequency nor a cluster indicator, or one not planned at the place) is simply not listed: it is still in
eTools. *Excel of the filter* and *CSV of the filter* give one row per master indicator and
one per PD indicator. In the administration, under Reporting setup, master indicators can also be made by
hand (with *Add PD indicators*), from an ActivityInfo master indicator, merged, or moved after eTools renames
a section. Merging a proposed master indicator into an active one accepts it there: its PD indicators are
confirmed in the one kept.

## Progress reports

[Progress reports](/partner-reporting/) has four tabs on one filter (search, report status, report
type, reporting year, section, report number, partner, partner type, PD status, emergency indicator, and
the period start and end; the section list offers each section once, and a section chosen also finds
reports whose rows list several sections). Like Partner monitoring, it opens on the running programme
documents (active, signed or suspended); clear the PD status choice to see every status. Under the
tiles, a note says how many more indicator reports the PD status filter leaves out (of programme
documents with another status, or not in NeuroDB), with a link that shows every status. Draft and cancelled
programme documents are never listed, nor reports that name no programme document.

- **Reporting progress**: the tiles count *indicator reports* (one indicator in one progress report, as
  Power BI counts them) per status, with the number of progress reports under each; then by section and
  by month of the end of the reporting period (the month a report is about, as Power BI shows it),
  and the report table with the PD status, document type and UNICEF focal
  points (names only).
- **Partners**: partners with reports, with at least one submitted or accepted, and without; partners
  per status; the reports to follow up (Overdue, Sent back, Due, Submitted).
- **By location**: one row per indicator, report and location, with the value in the period and PRP's
  figure to date there.
- **By disaggregation**: per indicator, report and location, the value of each disaggregation (female,
  male; Lebanese, Syrian…), a **subtotal per disaggregation** and the **location total**. Values add up
  only within one disaggregation, never across: female 5, male 15 and Lebanese 20 are a location total
  of 20, not 40. A subtotal that differs from the location total is marked. The labels come from PRP;
  a value whose label the nightly sync has not read yet is not listed (the location total is). Extra
  filters: indicator, location, P-code and disaggregation.

On **By location** and **By disaggregation**, the filters and the tabs come first and the table fills in
as soon as it is ready, in a grey box of its size; a change of filter brings the new table at once, as
before. The table is worked out once for everyone who asks for the same filter and kept for two minutes;
a new eTools sync shows at once.

## Programme documents

- **A programme document's page** lists each of its indicators with its target, Reached, % Reached and
  status, read by the same rule. Administrators, and Section editors of one of its sections, can choose
  its *unique beneficiaries indicator* there (*Make it the unique beneficiaries indicator*). The page
  also gives the budget, the partner's contribution (with a link to its share on the Funds page) and
  the funds reserved and disbursed.
- **Programme documents → Indicators** lists every indicator of every programme document in one table
  (PD number and status, partner, document type, dates, office, section, CP output, indicator,
  baseline, target, emergency), with its own filters and an export of the whole filter.

## Funds

The [Funds](/funds/) page has three tabs on one filter (search, donor, grant, FR number, FR start year,
PD status, partner, partner type):

- **Funds reservations**: the funds reserved, disbursed and outstanding, by donor, grant and year.
- **Partner contributions** (Power BI's Partner Fund Monitoring): one row per programme document with
  its partner and partner type, currency, planned UNICEF contribution, total budget, the partner's
  contribution and its share of the total budget, and the **financial contribution during reporting
  periods** the partner reported in PRP. PRP repeats that figure on every indicator of a progress
  report, so it is read once, from the latest progress report that counts.
- **By PD and grant** (Power BI's Funded Programs): one row per programme document and grant, with the
  funds reserved on that grant, the start and end dates and the days to the end (−1 once ended). The
  budget, the disbursement and the partner's contribution belong to the programme document as a whole:
  they are shown once for it, never on each of its grants, so a total never counts them twice.

Amounts in different currencies are never added together: budgets are in each programme document's
currency, funds reservations and disbursements in US dollars.

## Exports

Program Performance (the *Table* view of Partner monitoring), every tab of Progress reports and the two
Power BI tabs of Funds have *Excel of the filter* and *CSV of the filter*: every row of the filter, not
only the page shown. The grid and the Data quality tab have no file of their own: Program Performance's
file holds the same indicators with their figures, and each Data quality list (at most 500 rows on the
page; its count is complete) can be copied or saved as CSV with the rows it shows. In the
disaggregation export, values, subtotals and location totals are in columns of their own, and in the
funds export the programme document's own amounts are on its first row only, so adding up a column
never counts a figure twice.

## People and privacy

UNICEF focal points are shown by name only; an entry that holds an e-mail address is left out.
Partner focal points and the partner's staff who submitted a report are never shown. The records
NeuroDB stores from eTools keep no e-mail address, phone or mobile number field, and Ask NeuroDB
withholds any e-mail address written in a text.

## Checking the data

Administrators find **eTools reporting checks** in the administration, under Data and sync (also linked
from Data health and Progress reports): twelve read-only checks of what the partner report data looks
like (rows per location, repeated values, disaggregation keys, what reports that do not count carry,
PRP's cumulative, the calculation methods, which PD indicator each report row belongs to, PRP's report
totals, the financial contribution, the country and programme document of the rows and how much of the
running programme documents' reporting is held, the reports still owed in PRP's feeds: the partner
reports, PRP's indicator reports and PRP's own progress reports, and how the PD indicators can be grouped
into master indicators), each with a verdict and what NeuroDB does whatever the answer; they show counts
only. When PRP's indicator reports hold nothing, checks 8, 9 and 11 say what their last sync did and what
to do in the administration. When the business area code the hosting team set is not a code (a country
name), NeuroDB sets it aside, uses the code it finds, and the checks and Data health say so; an
administrator can enter the code in the administration, where it comes first. Check 10 says how the
partner reports were read (in windows when the Datamart's answer looks cut, and whether every window was
read whole) and why running programme documents have no row, and lists, by reason, every running
programme document with no row (its reference number, type, dates, sections, whether PRP holds it and
its active PD indicators); checks 10 and 11 say which business area filter the partner reports and PRP's
progress reports were read with, or why none. Check 11 also says how PRP's own progress reports are linked
to their programme document: by the pair of keys the data proves, with how many it links, or why no pair
was proven and the keys PRP's programme documents carry (what to tell the eTools Datamart team). Check 12 counts, per section, the PD indicators with and
without an eTools id and a CP output, the percentages reported with their numerator and denominator, and
how many master indicators the next night would propose; it changes nothing. Checks 10 and 11 run first. The checks run every night as a job and the page shows their latest
result at once, with when they were checked; **Run the checks now** runs them again in the background
(they can take several minutes on the full data), and the page shows the new result once you reload it.
Print the page or save it as PDF from the browser to share it: every table prints whole, wrapping,
with check 10's lists open; Data health prints its tables whole too.
