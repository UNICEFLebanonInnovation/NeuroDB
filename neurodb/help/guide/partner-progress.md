# Partner progress (eTools)

The partners' progress reports in the Partner Reporting Portal (PRP) reach NeuroDB through the eTools
Datamart every night. Four places read them: [Partner monitoring](/partner-monitoring/) (what each
programme document indicator reached), [Progress reports](/partner-reporting/) (who reported, and what;
the two are the sidebar's *Partner reporting (Results)*, named *Partner reporting (PRP)* until 21 Nov
2026),
the programme document and partner pages, and the [Funds](/funds/) page (budgets, funds reservations
and what partners contributed). They show the views of the eTools Power BI report, with the same
figures on every page for the same filter. [Home](/help/home/) starts with the tiles of the first two
for your section, and the programme documents whose reports need following up.

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
| Not yet due | The reporting period is still running. In the lists of reports to follow up it is one of the Due ones, as in Power BI. | No |

A report eTools stops sending stays, with the status it last had: one still to submit turns Due, then
Overdue, by its dates, until eTools sends it again. So reports still owed can appear here although the
partner reports feed no longer sends them.

### Reports still owed, from PRP's own list

The partner reports feed stops sending a report once its period ends unsent, so the reports partners still
owe are missing from it. PRP's own list of progress reports has them, as Power BI counts them. Home, Progress
reports, its Partner view tab, the programme document and partner pages, Partner monitoring's *Overdue reports*,
the daily review and Ask NeuroDB add them (since 26 Oct 2026), but only while NeuroDB can prove
that PRP's list is tied to the right programme documents:

- the pair of keys that ties them is proven, and the reports both feeds have end on the same day (9 in 10, at
  least 10 of them) and were submitted on the same day (9 in 10 of at least 10 submitted on both sides), on
  two days in a row with the same pair of keys;
- the last read of PRP's list did not fail and received records, at most 7 days ago.

A read that fails one of these hides them again at once. A report counts when PRP says Due or Overdue, its
period has ended, a read returned it in the last 7 days (a report PRP deleted would read Overdue for ever),
its programme document is not a draft or cancelled, and the partner reports have no row of it: when they have one, whatever its period or status, the partner
reports decide. They carry no indicator: the indicators under the tiles, Reached and the indicator pages
leave them out. In the tables they read "from PRP's own list" and do not open (they have no indicator to
show); the exports say their source.

While they are shown and the filter keeps some, a line under Home's tiles says how many Overdue and Due
include. While they are hidden, Progress reports and its Partner view tab show one short warning, *Some reports
still owed may be missing.*, and a line under Home's tiles says why:

- NeuroDB cannot tie PRP's own list to its programme documents yet (the pair of keys is not proven, or the
  reports both feeds have do not agree often enough);
- NeuroDB has not checked the tie yet: the next sync of the list does;
- the last sync could not check the tie (check 11 says so: tell the NeuroDB team);
- the list was tied on one day only: NeuroDB shows it once a read on the next day agrees too (a second read
  the same day does not count);
- the list was last read more than 7 days ago;
- the last sync of the list or of its programme documents failed or received nothing.

Administrators, and validators with staff status, also get a link to check 11 of the eTools reporting
checks, which says which test failed. Watch and the management brief still read the partner reports only: Watch keys its alerts by
PRP's report ids, which PRP's own list does not carry.

## Partner monitoring

[Partner monitoring](/partner-monitoring/) has three tabs on one filter:

- **Data quality**, the first tab, opens when you open the page. At the top, the active and ended
  programme documents and the indicators with and without a target or Reached, and the lists to fix (see
  below). Under them, headed *Every indicator*, the grid's tiles (indicators, on track, off track, not
  reported, partners, overdue reports) and the grid: by section, programme document and output, each
  indicator with its target, Reached, status and the months of the year. Since 21 Nov 2026 Partner
  monitoring has no *Programme documents* tile, neither at the top nor over the grid (Home keeps it, and
  *Check these figures* still gives the count), and the grid no longer shows % Reached, Reached in the chosen year, awaiting acceptance or the
  partner's view: its *Copy* and *CSV* buttons still give those four columns, and Program Performance and
  each programme document's page still show % Reached.
  The lists and the grid use the same filter and the same year: this year when the tab opens, or the year
  you choose (with *All years*, the grid has a column per year instead of the months). The grid was a tab
  of its own until 21 Nov 2026: an old link to it opens this tab.
- **Program Performance**: one row per indicator (section, CP output, emergency, target, Reached, %
  Reached, partner, programme document), sorted by % Reached; the section tiles filter it. *Emergency*
  (the column) means the programme document is an SPD, as Power BI's Emergency indicator
  does (since 26 Oct 2026). eTools' *high frequency* is another thing: an indicator reported
  monthly, in humanitarian reports. With a
  reporting year chosen, it shows Reached in that year and its % of the target first (sorted by it),
  as Power BI does, with Reached to date beside them.
- **Map**: the implementation locations. Each circle is filled with the colour of the worst status there.
  Since 21 Nov 2026 its ring and its size show the kind of place, so you can tell a
  governorate from a district at a glance. The whole country, when an indicator is planned for it, has the
  largest circle and a dark ring; a governorate the next largest circle and a purple ring; a district a
  middle-sized circle and a blue-green ring; and a smaller place (a cadastre, village or site) the smallest
  circle and a grey ring. In dark mode the rings are lighter, and the whole country's is white. Among places
  of one kind, a bigger circle has more indicators. The legend under the map says the same in words (it
  lists the whole country only when such a place is on the map), and the list beside the map and a place's
  details name the kind of place. A pale circle is placed at the nearest larger area that has coordinates.
  Click a place to list its partners, programme documents and indicators. Since 21 Nov 2026 this list no
  longer shows a programme document's total budget, UNICEF cash, partner
  contribution, donors or agreement, also when you filter by location: a programme document's budget is
  for the whole programme document, not for one place. Its own page still shows them.

*Master indicators* is hidden (since 21 Nov 2026): it is no longer a tab and no page links to
it, but an old link or bookmark still opens it (see [Master indicators](#master-indicators) below).

The filter has no CP output, partner type, office or emergency choice (since 21 Nov 2026); the
columns that show them stay. An old link or bookmark that still carries one is still applied, and a line
above the results names it, with a link that removes it.

**The lists to fix** on Data quality: missing targets, no or zero Reached, Reached differing from PRP, the
same title twice in one programme document, and partial history. The partner reports carry no PD indicator
id, so a report is matched to its indicator by title; where two indicators of a programme document share a
title, by their PD output too; where two indicators of a programme document share a title and an output,
NeuroDB tells them apart by what the partner reports and eTools both say of them (high frequency, cluster,
target, type, disaggregations, planned places, active), once the data shows each of these agrees.
Indicators still alike stay without Reached until told apart in eTools, and so do those whose rows name a
PD output that matches none of theirs (correct the output name in PRP or eTools): the same-title list
counts them.

**Indicators eTools marks inactive.** When a programme document is amended, eTools often keeps the older
version of an indicator beside the new one and marks it inactive. In a programme document partners have
reported on, NeuroDB leaves out an indicator eTools marks inactive in every row that no partner report names
(since 28 Oct 2026; a programme document no partner report names yet keeps all its indicators):
Partner monitoring and its Data quality, Programme documents → Indicators, the programme document pages,
their exports and Ask NeuroDB's indicator progress do not list or count it (Raw eTools data and Ask NeuroDB's
eTools records still show every record eTools sent). An inactive indicator partners reported on stays listed,
as their values sit on it: its pop-up says it is inactive in eTools. The eTools Datamart sync decides this each time it reads the PD
indicators or the partner reports, so an indicator a new report names comes back after the next sync. Raw
eTools data → PD indicators says how many are left out and lists their rows.

Switching tabs keeps the year shown, and the year menu at the top offers *All years*. The
**location** filter shows each indicator's Reached at the chosen place (a place is found by its name and
by its P-code, so "Akkar Governorate" in a partner report is the planned "Akkar"); on the map, each row
then gives the place's own Reached and, beside it, the indicator's Reached over every location.

An indicator opens on its own page: a location × report table whose rows end with each location's
Reached and whose columns end with each report's value, the reports that do not count greyed, and
**Reached by disaggregation** (female, male, Lebanese…) for each location, with a subtotal per
disaggregation when the indicator adds up.

The page opens at once: the tabs and the filters come first, with a grey box where each block will go,
and each box fills in as soon as its figures are ready (see [Pages that open at
once](/help/overview/#pages-that-open-at-once)). On Data quality, its tiles come first, then its lists,
then the grid's tiles and list as you scroll down to them. Applying a filter, sorting a column or clicking a
section tile refills the boxes under the filters; the filters stay as they are. The figures are worked
out once for everyone who asks for the same filter and kept for two minutes, so opening a view again, or
another person opening the same filter, is quicker; a new eTools sync shows at once. The map fills in
the same way: the page first, then its locations.

## Master indicators

A **master indicator** brings together the partners' own PD indicators that measure the same thing: for
example "children enrolled in non-formal education" over every
programme document of the section that counts them. Each one is in one eTools section and has, for every
year, its AWP or RWP code and the **target staff type for that year**. The *Master indicators* view of
Partner monitoring is hidden (since 21 Nov 2026): it is no longer a tab and no page links to
it, but an old link or bookmark still opens it, and everything below still works there. It shows one year
(this year first) and opens on your own section.

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
hand (with *Add PD indicators*), merged, or moved after eTools renames
a section. Merging a proposed master indicator into an active one accepts it there: its PD indicators are
confirmed in the one kept.

## Progress reports

[Progress reports](/partner-reporting/) has two tabs on one filter (search, report status, report
type, reporting year, section, report number, partner, PD status, and the period start and end; the
section list offers each section once, and a section chosen also finds reports whose rows list several
sections). The partner type and emergency indicator filters were removed (since 21 Nov 2026): an
old link that still carries one is still applied, and a line above the results names it, with a link that
removes it. It opens on the active programme documents only (since 21 Nov 2026; it opened on the
active, signed and suspended ones before, as Partner monitoring and Home still do): choose more PD statuses
in the filter, or clear the choice to see every status. Home's links to Progress reports choose active,
signed and suspended, so the page opens on the numbers Home shows. Draft and cancelled programme documents
are never listed, nor reports that name no programme document.

- **Reporting progress**: the tiles count *progress reports* per status, as Power BI's Reporting Progress
  does (since 26 Oct 2026). Under each tile, the indicators in those reports (since
  19 Nov 2026): each indicator once, as Partner monitoring places a report row on the indicators of its
  programme document (by its eTools indicator id, else its title, else its PD output and title; a row it
  cannot place counts by its title), however many reports carry it (QPR1 to QPR4, monthly reports, every
  year), of active programme documents only. When you choose signed or suspended programme documents
  too, they count in the progress reports, not in the indicators. If you choose PD statuses without Active,
  the indicators are those of the statuses chosen (the hint's hover text names them). An indicator can be
  in reports of several statuses, so the indicator numbers do not add up to the
  total; the report table's *Indicators* column still counts each report's own. Then the progress reports
  by section (each report once, under its Section as the
  report table shows it) and by month of the end of the reporting period (the month a report is about, as
  Power BI shows it), and the report table with the PD status, document type and UNICEF focal points
  (names only).
- **Partner view**: partners with reports, with at least one submitted or accepted, and without; partners
  per status; the reports to follow up (Overdue, Sent back, Due, Submitted). A report not yet due is one
  of the Due ones there, as in Power BI's follow-up list (since 26 Oct 2026), and the Report
  status *Due* keeps it there, as Power BI's slicer does; Home's programme documents to follow up count it
  the same way. Everywhere else it is *Not yet due*. When the filter keeps no report at all, the list says
  *No progress report in the filter*; *Nothing to follow up* means every report in the filter is Accepted.

**By location** and **By disaggregation** are hidden (since 21 Nov 2026): they are no longer
in the tab bar and no page links to them, but an old link or bookmark still opens them. By location lists
each indicator, report and location with its value. By disaggregation gives each disaggregation's values
(female, male; Lebanese, Syrian…), a subtotal per disaggregation and the location total: values add up
only within one disaggregation (female 5, male 15 and Lebanese 20 are a location total of 20, not 40), and
a subtotal that differs from the location total is marked. Each indicator's page also gives its
**Reached by disaggregation**, location by location.

## Programme documents

The [Programme documents](/programmes/) page has four tabs, each with its own filter bar: *Programme
documents* (the list), *Indicators*, *Funded programme summary info* and *Funded programme overall*.

- **A programme document's page** lists each of its indicators with its target, Reached, % Reached and
  status, read by the same rule. Administrators, and Section editors of one of its sections, can choose
  its *unique beneficiaries indicator* there (*Make it the unique beneficiaries indicator*). The page
  also gives the budget, the partner's contribution (with a link to its share on the Funds page) and
  the funds reserved and disbursed. Under *Partner reporting* it counts every progress report of the
  programme document by status (with PRP's reports still owed while they are shown) and lists the 12
  latest; *All reports* opens every one of them on Progress reports.
- **Open action points and timeline**, at the end of a programme document's page and of a partner's page,
  comes a moment after the rest of the page (see the next section).
- **Programme documents → Indicators** lists the indicators of every programme document in one table
  (PD number and status, partner, document type, dates, office, section, CP output, indicator,
  baseline, target, emergency), with its own filters (no office or emergency filter since
  21 Nov 2026) and an export of the whole filter. Like every page, it
  leaves out an indicator eTools marks inactive that no partner report names (see Partner monitoring above);
  one partners reported on reads *Inactive in eTools* under its title.
- **Programme documents → Funded programme summary info** sums up the programme documents. Choose
  *Active* (the default: those eTools shows as active) or *All* (every one but drafts and cancelled ones, as
  *Funds → Funded Programs* counts them). The choice stays when you move between this tab and the *Programme
  documents* and *Indicators* tabs, which open on *All*. The tiles give the number of programme documents and
  partners (a partner counts once, whatever way its programme documents write its name; a programme document
  with no partner is not counted as one), the total budget, the funds reserved and disbursed, and how
  many active ones end within 90 days. Two charts count them by section and by type. Then come four
  tables: the active programme documents ending within 90 days (with the days left), the summary by
  section, the summary by partner and the totals by currency. Each summary row gives the number of
  programme documents, the UNICEF planned budget, the total budget, the funds reserved and the amount
  disbursed. A programme document counts once for its partner but under each of its sections, so the
  section rows add up to more than the total. *Excel of the summary* downloads the three tables; each
  table also has *Copy* and *CSV*. The old address of the summary page opens this tab.
- **Programme documents → Funded programme overall** lists every programme document but drafts and
  cancelled ones (as *Funds → Funded Programs*), 50 to a page. Filter by PD reference, donor, grant,
  partner, CSO type, section, status and start year (from, to). Only the start years you pick in its two
  lists filter it, and its downloads hold the same rows as the page. Each row gives the partner, type, status, sections, start and end dates, the days to the end (green
  over 120, amber over 60, red below; *Ended* once it has ended), donors and grants, currency, UNICEF
  planned budget, total budget, UNICEF cash, funds reserved and disbursed. The tiles add up the filter.
  *Excel of the filter* and *CSV of the filter* download every row.
- **Amounts on these two tabs.** Budgets and UNICEF cash are in the programme document's currency, and
  amounts in different currencies are never added. Funds reserved and disbursed are in US dollars, from
  the eTools funds reservations as on the Funds page, for the whole programme document whatever donor or
  grant you choose. Neither tab, nor its downloads, names a person: no focal point, e-mail or phone.

## Open action points and timeline

The last block of a programme document's page and of a partner's page.

- **Open action points**: three numbers, *Open*, *Overdue* and *Open, high priority*. Each one opens the
  [Action points](/action-points/) page with exactly those action points, and the number is the one that
  page counts. Check them there.
- **Timeline**: what happened, newest first, by year. For a programme document: when it was signed,
  started and ends, and its status in eTools now; its progress reports (submitted, sent back, accepted, or
  not submitted by the due date); field monitoring visits and their rating; third-party monitoring
  activities; audits and spot checks with how many financial findings they had, and micro-assessments with
  their risk rating; action points raised and completed; and changes to action points (status, priority,
  due date) from the day NeuroDB started recording them. A partner's timeline has the same for all its
  programme documents, with its third-party monitoring visits, names the programme document of each entry,
  and adds its assessments and each finished HACT year.
- **Planned dates.** A date still to come, such as a programme document's end or a visit not made yet, says
  *planned*. Planned dates come first and are not counted with the events that happened.
- **A status is today's.** A status beside an entry is its status in eTools now, not on that date.
- **Only dates, references and statuses.** No names, and nothing partners or monitors wrote. Click a
  reference to open its details.
- **Nothing is cut without saying so.** The timeline shows the last 24 months, 100 entries at most. It
  says how many older entries there are and how many more it leaves out. *Show older* lists up to 500 of
  them, newest first; past that it says how many are left out. *Show every event*, shown when the 100 are
  not enough, does the same. *Show the last 24 months only*, at the end of that list's summary, goes back. It
  also says how many records have no date. Amendments are not listed: NeuroDB does not hold their dates.

## Funds

The [Funds](/funds/) page has three tabs on one filter (search, donor, grant, FR number, FR start year,
PD status, partner):

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

A programme document's budget is the one of the eTools budget record that carries its reference number, and a
funds reservation line belongs to the programme document whose number it carries (a line without a number, to
the one its eTools id names unless its vendor number is another partner's). *Checking the figures* says how,
and where to see the records.

## Exports

Program Performance (the *Table* view of Partner monitoring), every tab of Progress reports and the two
Power BI tabs of Funds have *Excel of the filter* and *CSV of the filter*: every row of the filter, not
only the page shown. The Data quality tab (its lists and its grid) has no file of its own: Program
Performance's file holds the same indicators with their figures, and the grid and each list (at most 500 rows on the
page; its count is complete) can be copied or saved as CSV with the rows it shows. In the
disaggregation export, values, subtotals and location totals are in columns of their own, and in the
funds export the programme document's own amounts are on its first row only, so adding up a column
never counts a figure twice.

> **For validators:** under the tabs of Progress reports and Partner monitoring (and on its map), *Check these figures* shows where each figure of the filter shown comes from, step by step, with the records behind it as CSV; *Power BI comparison sheet (Excel)* downloads the page's figures for the filter shown, to compare with the PRP Power BI dashboard; and *Raw data behind this page* opens the eTools records the page reads: see [Checking the figures](/help/checking-figures/).

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
was proven and the keys PRP's programme documents carry (what to tell the eTools Datamart team), or the keys
left out because a programme document came twice in one read (the next read's copy counts); check 11 counts
each progress report once, as its latest version (Ask NeuroDB still lists an older version until a whole read
removes it). Its last figures say whether the pages could show the reports still owed from PRP's own list:
only when the pair of keys is proven, the reports the partner reports also have end on the same day (9 in 10,
at least 10 of them) and were submitted on the same day (9 in 10 of at least 10 submitted on both sides), on
two days in a row; a read that fails one of these closes it at once. They also count those reports still
owed, the partner report rows whose eTools id and reference number name different programme documents, and
the reports kept although eTools no longer sends them. *PRP's progress reports shown on the pages* says
whether Home and Progress reports show them now (see Reports still owed, from PRP's own list). Check 12 counts, per section, the PD indicators with and
without an eTools id and a CP output, the percentages reported with their numerator and denominator, and
how many master indicators the next night would propose; it changes nothing. Checks 10 and 11 run first. The checks run every night as a job and the page shows their latest
result at once, with when they were checked; **Run the checks now** runs them again in the background
(they can take several minutes on the full data), and the page shows the new result once you reload it.
Print the page or save it as PDF from the browser to share it: every table prints whole, across the
landscape sheet, wrapping between words (reference numbers, dates and counts print whole: check 10's
dates are written with non-breaking spaces), with check 10's lists open; Data health prints its tables
whole too, with each period's details of Compared with Power BI open.
