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
  *Differs from PRP*. A programme document that started before the reports NeuroDB holds shows
  *Partial history*, and its Reached is PRP's cumulative.
- **Children reached** on the overview, the brief and the donor page counts one indicator per programme
  document: its *unique beneficiaries indicator* (chosen on the programme document page), else its
  largest children indicator. See [Overview and briefs](/help/briefs-and-overview/).

## Report statuses

Every progress report has one of six statuses, the same on every page, so the counts add up to the
total:

| Status | Meaning | Counts in Reached |
|---|---|---|
| Accepted | UNICEF accepted the report. | Yes |
| Submitted | Submitted by the partner, awaiting acceptance. | Yes, shown as awaiting acceptance |
| Sent back | Returned to the partner. | No |
| Due | The period has ended; not submitted yet, and not late. | No |
| Overdue | Not submitted by its due date. | No |
| Not yet due | The reporting period is still running. | No |

## Partner monitoring

[Partner monitoring](/partner-monitoring/) has four views on one filter:

- **Grid**: by section, programme document and output, each indicator with its target, Reached, %
  Reached, Reached in the chosen year, awaiting acceptance, status and the months of the year.
- **Program Performance**: one row per indicator (section, CP output, emergency, target, Reached, %
  Reached, partner, programme document), sorted by % Reached; the section tiles filter it. With a
  reporting year chosen, it shows Reached in that year and its % of the target first (sorted by it),
  as Power BI does, with Reached to date beside them.
- **Data quality**: programme documents and indicators with and without a target or Reached, and the
  lists to fix (missing targets, no or zero Reached, Reached differing from PRP, the same title twice in
  one programme document, partial history).
- **Map**: the implementation locations, coloured by the worst status there.

Switching views keeps the year shown; the year menu at the top offers *All years* on these pages. The
**location** filter shows each indicator's Reached at the chosen place (a place is found by its name and
by its P-code, so "Akkar Governorate" in a partner report is the planned "Akkar"); on the map, each row
then gives the place's own Reached and, beside it, the indicator's Reached over every location.

An indicator opens on its own page: a location × report table whose rows end with each location's
Reached and whose columns end with each report's value, the reports that do not count greyed, and
**Reached by disaggregation** (female, male, Lebanese…) for each location, with a subtotal per
disaggregation when the indicator adds up.

## Progress reports

[Progress reports](/partner-reporting/) has four tabs on one filter (search, report status, report
type, reporting year, section, report number, partner, partner type, PD status, emergency indicator, and
the period start and end). Like Partner monitoring, it opens on the running programme documents
(active, signed or suspended); clear the PD status choice to see every status. Draft and cancelled
programme documents are never listed.

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

Administrators find **eTools reporting checks** in the administration (linked from Data health): nine
read-only checks of what the partner report data looks like (rows per location, repeated values,
disaggregation keys, what reports that do not count carry, PRP's cumulative, the calculation methods,
indicator ids, PRP's report totals and the financial contribution), each with a verdict and what NeuroDB
does whatever the answer. Nothing needs to be run: the page reads what the nightly sync stored.
