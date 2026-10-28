# Checking the figures

**For validators.** This page is for the people who check NeuroDB's eTools figures against the raw data:
superusers and the members of the *Validators* group. Everyone else sees the pages exactly as before.

## Who can check figures

A superuser or an Administrator ticks the group in the administration: *Users and access → Users*, open
the person, then *Groups*: tick **Validators** and save. Administrators are validators only when they are
ticked too, like anyone else; superusers always are.

Raw eTools data, Compare with Power BI and the eTools reporting checks are in the administration, which also
asks for **Staff status** (on the same form). A validator without it sees "Raw data: ask for staff status" where the link
would be.

A validator sees one more line under the tabs of [Progress reports](/partner-reporting/),
[Partner monitoring](/partner-monitoring/) (and its map) and [Field monitoring](/field-monitoring/), and on
[Home](/) under *Partner reporting*: **Raw data behind this page**. It opens Raw eTools data on the
sources that page reads. On Progress reports (every tab), Partner monitoring (the grid, Program Performance
and Data quality) and its map, the line also offers **Check these figures** (the figure trail, below), and so
does a line of its own under the title of each of Home's two blocks, Progress reports and Indicator progress.
On Progress reports and Partner monitoring, the line offers the **Power BI comparison sheet (Excel)** (below).
The master indicators have neither. The *eTools reporting checks* (*Data and sync*) open read-only for
validators with Staff status: only an Administrator runs them.

## The figure trail

*Check these figures* opens the page's **figure trail** for the filter shown: where each figure comes from,
step by step, from what eTools said it had to what the page shows. It needs no staff status. Partner
monitoring's trail is described below, after Progress reports'.

At the top: the filter in words, the day the statuses are read for (the page's day; the comparison sheet's
Trail links open it on the sheet's day), when the eTools figures are from (the end of the last partner reports
sync) and, while an eTools sync runs, a note: the figures can move until it ends. Then the page's own tiles,
drawn as on the page, and one line: **"All N figures rebuilt from the rows: the same."** A figure whose value
on the page and value rebuilt from the rows differ is listed in red above it: tell the NeuroDB team. Each value
is written as the page writes it (a Reached of 33.33% reads 33); point at it to see the exact value.

**Where the rows come from** lists the steps:

1. what eTools said it had, what NeuroDB received, and what it did not keep (of another country, naming no
   country, records that could not be saved): counts only, NeuroDB never keeps these records;
2. what is in NeuroDB (updated by the last sync, updated since, kept from an earlier sync), and from which
   date NeuroDB holds partner reports;
   then **PRP's own reports still owed (a second source)**: whether the page shows them (since when, or the
   line the page says while it does not), how many are still owed for a period that has ended and are not in
   the partner reports (counted once), how many the filter keeps, and how many the last sync left out (no
   longer returned for 7 days, or two versions of one report). *Download these rows (CSV)* lists each of
   them, whether the filter keeps it and why not, its status as the page reads it, then its record (people as
   counts). Each filter step also says how many of them it leaves out;
3. what the page leaves out, filter by filter, in the order the page applies them: the rows, and the indicator
   reports and progress reports left out entirely or partly (some of their rows kept). The PD status filter
   gives the three lines of the page's note (another status, not in NeuroDB, names no programme document);
4. the rows the filter keeps, then the report status when one is chosen (one status per progress report, read
   from all its rows). Their CSVs list PRP's own reports still owed after the rows, and each step says how
   many.

Each step with rows has *Download these rows (CSV)*. A download holds at most 25,000 rows: past that, the
step links to Raw eTools data, where choosing a year, a section, a partner or a programme document makes it
smaller. **Each figure on the page** (folded) lists every figure as the page names it, with its value on the
page, its value rebuilt from the rows, *Same?*, how it is counted and its rows as CSV. By location and By
disaggregation have folds of their own: their steps (the limit of the rows the table reads, the report
status, the tab's own filters) and their lines.

**A worked example.** The tile *Overdue* on Progress reports, section Education:

> Overdue: 12 progress reports, the same as the page.
>
> 1. eTools said it had 780,905 partner report rows, of every country: the Datamart does not filter this
>    list by country. NeuroDB received 780,975: 70 more than it said, records added during the sync.
>    Counts only.
> 2. 770,993 were of another country and were not kept; none could not be saved. Counts only.
> 3. 9,982 rows are in NeuroDB: 9,982 updated by that sync, none kept from an earlier one. NeuroDB holds
>    partner reports from 1 Jan 2026. [CSV]
> 4. Left out by the page, in the order it applies them: not in the section Education (7,100 rows; 2,345
>    indicator reports left out entirely, 3 partly); programme document is a draft or cancelled (12 rows);
>    the PD status filter (300 rows). [CSV for each]
> 5. Kept by the filter: 2,545 rows, 820 indicator reports in 210 progress reports. [Kept rows CSV]
> 6. With the status Overdue: 12 progress reports.

The rows that set a report's status (its newest word, across all its rows, also those the filter leaves out)
download beside step 5: *Rows that set the status (CSV)*.

## Partner monitoring's figure trail

On Partner monitoring the trail follows the indicators, not the report rows. Its top is the same (the filter,
the day, when the eTools figures are from: the end of the last partner reports or PD indicators sync, the
note while a sync runs), then the page's tiles and **"All N figures rebuilt from the indicators kept: the
same."** Below, **Where the indicators come from**:

1. what eTools said it had of the PD indicators, what NeuroDB received, did not keep and holds (eTools lists
   an indicator once per location and disaggregation: these are rows), and the rows naming no programme
   document NeuroDB holds (never read);
2. the programme documents in NeuroDB, by the sync that last wrote them: the eTools Datamart's and the eTools
   REST API's both do, and NeuroDB never removes one; a programme document's status decides whether it is
   running;
3. the programme documents NeuroDB holds, then those each filter leaves out (draft or cancelled, the PD
   status or running, the year, a partner, a programme document, a document type, an office, a partner
   type), each with its PD indicator rows;
4. the PD indicator rows of the programme documents kept, then those of the indicators eTools marks inactive
   that no partner report row names (never listed), then those each filter leaves out (a search, a tag,
   emergency, humanitarian reports), with the indicators left out entirely or partly;
5. one per indicator ("4 PD indicator rows are 3 indicators"), then the indicators each filter leaves out
   (CP output, location, section, tracking status);
6. the indicators kept, and **Programme documents kept with no indicator of this filter**: they count in no
   figure of programme documents. It is the difference a Power BI count of running programme documents
   can show (see check 10).

**Where Reached comes from** (folded) follows every partner report row in NeuroDB, in rows and in indicator
reports: those of a programme document NeuroDB does not hold, of one the filter does not keep, of the other
report type; of the rows read, those placed to an indicator the filter leaves out and those left out, each
reason in check 7's words, then those placed to an indicator of the filter (by its eTools id, title, PD
output or a tie-breaker); the monthly pass, which adds the rows it reads for the indicators with no quarterly
report; the rows left out because two indicator reports of one progress report share the indicator's title;
under a location filter, the rows at other places; then the reports that do not count by status, those
counted with no value, PRP's own report totals (with what eTools said it had of them), the reports that
count, and the indicators with Reached. Each step's count is the one before less what it leaves out (plus
what the monthly pass adds), and from the rows read on only the rows of the indicators kept go on: a step
that does not follow says so in red. **Overdue reports, step by step**
(folded) gives the rows of these programme documents and report type, those not submitted, those due before
the day, and the progress reports they make (O4); then O5 adds PRP's own Overdue reports still owed of these
programme documents and report type that the partner reports do not have, once the page shows them (with its
CSV), or says why it does not. The figure is O4's and O5's together, and its own CSV lists both (`source`
says which). This is not Progress reports' *Overdue* tile.

**Program Performance** and **Map** have folds of their own, open when the trail was opened from them.
Program Performance reads the filter without its sections (every section's Reached, then the tracking
status), then keeps the sections chosen: its indicators are the sum of the section tiles chosen, never cut at
2,000. Each indicator's Reached has *Rows (CSV)*: the partner report rows placed to it, used or not, and why.
The map's tiles and its line "N on the map (M approximate) · K without coordinates" come from its places;
*Rows (CSV)* lists each place with its indicators.

**Kept indicators (CSV)** holds one row per indicator the filter keeps: a yes or no column per figure
(`with_target`, `with_reached`, `status_on_track`, `dq_without`, `running`...) and the Program Performance
columns. In Excel, filter a column on *yes* and count the rows (or add up `counts_once`): it gives the
figure. Each step's and each figure's CSV holds the programme documents, the PD indicator rows, the
indicators or the partner report rows it names, with `unit_key` and `counts_once` (1 once per unit: for
*Partners*, `counts_once_partner`); a programme document's focal points as a count. A step holding more
than a download allows (100,000 PD indicator rows, 25,000 partner report rows) says so instead, with a link
to Raw eTools data.

**A worked example** (the numbers are an example). The tile *Off track* on Partner monitoring's grid, section
Education, 2026:

> Off track: 12 indicators, the same as the page.
>
> 1. eTools said it had 273,253 PD indicator rows; NeuroDB received them all. Counts only.
> 2. Programme documents NeuroDB holds: 410 (273,000 PD indicator rows). Not running: 343 left out
>    (180,000 rows). [CSV]
> 3. PD indicator rows of the programme documents kept: 93,000; one per indicator: 1,150 indicators.
> 4. Not in the section Education: 1,020 indicators left out. [CSV]
> 5. Kept: 130 indicators. [Kept indicators CSV]
> 6. With the tracking status Off track: 12 (open *Where Reached comes from* to see which reports each
>    indicator's Reached reads).

To check it in Excel: open *Kept indicators*, filter `status_off_track` on *yes* and count the rows.

## Rebuilding a page in Excel

*Kept rows (CSV)* holds one row per report row the filter keeps. Its first columns are what NeuroDB read of
the row: `report_status_neurodb` (the report's status), `report_status_chosen`, `follow_up_chosen`,
`section_counted`, `period_end_month`, and one column per unit (`indicator_report_unit`, `progress_report_unit`, `partner_unit`,
`pd_unit`) with `counts_once_*`: 1 on the first row of each unit. Then the record as Raw eTools data writes it
(people as counts).

1. Open the file in Excel and insert a pivot table.
2. Filter `report_status_chosen` on *yes*.
3. Put `report_status_neurodb` in Rows (or `section_counted` with it in Columns, for the section table; or
   `period_end_month`, for the month chart).
4. Put the sum of `counts_once_progress_report` in Values: the tiles, the section table and the month chart
   (they count progress reports, as Power BI's Reporting Progress does). The sum of
   `counts_once_indicator_report` gives the indicator reports under each tile, and the sum of
   `counts_once_partner` the Partners tab's *Partners*. A distinct count of `progress_report_unit`,
   `indicator_report_unit` or `partner_unit` gives the same. The reports to follow up are those Overdue, Sent
   back, Due, Not yet due (listed as Due there, as Power BI does) and Submitted: filter `follow_up_chosen` on
   *yes* instead (with the Report status *Due*, it keeps the reports not yet due too, as Power BI's slicer
   does).

While the page shows PRP's own reports still owed, the tiles, the section table, the month chart and the
Partners tab also count them. *Kept rows (CSV)* then lists those the filter keeps after the rows, one line
each (`source` *PRP progress reports*, then their record's fields after the rows' columns), so the same pivot
table counts them: they have no indicator report (`counts_once_indicator_report` 0). The step *PRP's own
reports still owed* has the CSV of every one, with whether the filter keeps it and why not.

Each figure's own *Rows (CSV)* holds only the rows it counts, PRP's own reports still owed among them, with
`unit_key` (its unit) and `counts_once`: their sum is the figure. In a step's CSV, `counts_once` is 1 only on a unit the step leaves out entirely
(`unit_partly_kept` marks the others), so its sum is what the step leaves out entirely.

When the database takes too long, a file ends with the row "The download stopped here: the database took too
long. Download again or narrow the filter." That file is not complete: do not compare it.

## Compare with Power BI

In the administration, *Data and sync → Compare with Power BI*, upload the files you exported from Power BI:
the zip as your browser saved it, or the Excel files, several at once. NeuroDB reads each export, compares
every row with the NeuroDB page that shows the same thing, for the same filters, and tells you what to do
about each difference. You no longer have to match the rows by hand. It is for validators with Staff status,
like Raw eTools data.

**How to export.** The page lists the visuals and cards of the *UNICEF Program Performance* report, in the
order of its pages, with the names the report gives them (a page's cards on one line: Partner Performance's
*# of Partners*, *# of Partners with at least 1 report* and *# of Partners not reported*; Reporting Progress's
*Accepted Report*, *Submitted Report*, *Due Report* and *Overdue Report*; Data Quality's *PD Count*, *Active
PDs*, *Ended PDs*, *No. of Indicators (Total)*, *No. of Indicators with Reached*, *No. of Indicators without
Reached*, *No. of Indicators with Target* and *No. of Indicators without Target*, and its table *Indicators
with Missing Targets*). On each: *More options (…) → Export data → Summarized data → .xlsx*. Set the year
slicer to the current year where a page has one (*Reporting Year* on Program Performance, *Reporting year* on
Reporting Progress, Data Quality, PD/SPD Report, PD/SPD Report/Location and PD/SPD Report/Disaggregation;
Partner Performance, PD/SPD Indicator, Partner Fund Monitoring and Funded Programs have none); the page says
the year and the date NeuroDB's partner reports start from. On every page set *PD status* to active, and
clear its other slicers and boxes (the page names them all: *Section*, *Partner*, *Report status*, *Report
type*, *Report #*, *Emergency indicator*, *PD Reference #* and so on). A file with a sensitivity label that
encrypts it cannot be read: export it again without the label. Upload the PD/SPD Report list and the
PD/SPD Report/Location table with the other visuals: NeuroDB explains the counted visuals and cards (the
partner cards, the table by sector, the chart by month) and the follow-up list from the report list, and
ties each disaggregation row (that export has no PD #) to its programme document through the location
table.

**What NeuroDB does with the files.**

- It recognises each export **by its columns**, never by its file name, so a renamed file is still read. A file
  NeuroDB does not recognise is named with the columns it knows.
- It reads Power BI's own filters (the *Applied filters* line Power BI writes in the file) and applies the same
  ones to its own page: the PD status, the reporting year, the section, and on the report pages the report
  status. With one reporting year, Program Performance's and PD/SPD Indicator's Reached is the year's, as
  Power BI's *Reporting Year* reads it. The export's page lists the filters one by one: applied, applied as
  NeuroDB reads it, or one it cannot apply on that visual (a year or a section on the funds pages, which have
  neither; clear that slicer in Power BI and export again). A filter on a field NeuroDB does not know is listed
  without its value, and without its name when the name could be a person's.
- **People's names are dropped as the files are read.** The *UNICEF Focal Points* column of PD/SPD Report and
  the *Focal Point* column of Partner Fund Monitoring are never kept, shown, written in a file or sent to the
  AI; the page says "Dropped as the file was read". Partners, programme documents and indicators stay, without
  the names NeuroDB knows, e-mail addresses and phone numbers. A code or a status (a PD #, a Report #, a
  location code, a grant, a status, Yes or No) holding a name, an e-mail or a phone is not kept at all, and the
  export's notes count them. NeuroDB's own records are read the same way: no focal point, no person who
  submitted a report, no narrative ever reaches the comparison.
- The hidden files a Mac adds to a zip are skipped and counted.

**Two values per row.** Every row shows **Power BI**'s value and **NeuroDB**'s: the figure NeuroDB's page shows
for the same filter. Rows are matched by what both sides show (for the report list, the PD #, the Report # and
the period end; for Program Performance, the partner and the indicator title). Two values are the same when
they are equal, whatever Power BI's number format shows; a 0 in Power BI and no value in NeuroDB are the same.
Rows about periods before NeuroDB's history are set aside and counted, never listed: Power BI's, and the
rows of those periods NeuroDB still holds from an earlier sync (a note says how many).

**Every difference has a reason and says what to do.** Each reason belongs to one of five groups, listed in this
order: needs a look, check the PD in eTools, wait for the next sync, NeuroDB cannot show this yet, expected.

- **Expected: nothing to fix.** A row before NeuroDB's history (for a programme that started before it, Power
  BI can show reports NeuroDB never holds), a report that moved on after Power BI's refresh (NeuroDB already
  has its newer status), a status Power BI reads another way, a value PRP repeats on every location, or
  *a different rule*: Power BI counts the figure another way (the line and the verdict point to *Choices for
  you* only when it offers a choice about that rule). Power BI's own rules are said so: it keeps one report
  per indicator, location and period end (NeuroDB keeps both); its Data Quality list names an indicator only in
  the year its programme document ends (said only while Power BI lists no indicator of a programme document
  that ends later); it lists one copy of an indicator its programme document holds twice, and one indicator of
  a title two PD outputs share; its Reached can count Accepted reports only. Its location table adds up two
  indicators of a programme document that share a title: NeuroDB adds them up too before comparing (the same
  report, place and title, written exactly alike), and a note counts them. Values Power BI rounds at the sixth
  decimal are the same. Rows Power BI lists under its *Section* filter whose only difference is the section
  (Power BI reads the programme document's sections, NeuroDB the indicator's own) are counted in one note of
  the export, not listed, on Indicators with Missing Targets too; a Data Quality card that Power BI counts so
  says so, with NeuroDB's figure counted Power BI's way. A copy of an indicator is said to be one Power BI
  lists once only when Power BI lists another copy, or another copy has a value in the year. *NeuroDB lists an
  indicator eTools marks inactive*: Power BI lists only the current version of an indicator, while NeuroDB
  keeps an inactive one as long as partner reports name it (partners' values sit on it; your decision of 28 Oct
  2026), and every indicator of a programme document no partner report names yet. An inactive indicator no partner report names is left out of NeuroDB's pages: a Power BI row of one
  says *NeuroDB's page leaves it out*.
- **NeuroDB cannot show this yet.** A report still owed for a period that has ended (Due or Overdue): the
  partner reports feed never sends it, and NeuroDB does not show PRP's own progress reports until their link
  to the programme documents is proven (eTools reporting checks, check 11). A value Power BI shows in a report
  not submitted yet (the partner's draft) is in this group too: the eTools Datamart never sends it. Nothing to
  fix in NeuroDB now; these are never counted as expected. Once the pages show PRP's own reports still owed,
  a report found there compares as NeuroDB's (the export's notes say how many come from PRP's own list). One
  PRP's own list has under another status says so and why (moved on since Power BI's refresh, for one); one
  PRP's own list still has as Due or Overdue, which the pages do not show as NeuroDB counts each report once
  (the partner reports hold its number for another period, or PRP's list a newer version), needs a look; one
  no read of the list returned in the last 7 days is no longer counted (PRP may have deleted it); and one
  neither feed has was not in the Datamart read. A draft value stays in this group (PRP's own list carries no
  value), on the location and disaggregation exports alike. A report Power BI lists as Due whose period has not
  ended yet, and that NeuroDB lacks, was not in the Datamart read: the feed carries those. Uploads compared
  before this group existed keep their reports still owed under *Expected*.
- **Wait for the next sync.** NeuroDB's last sync ran before Power BI's refresh (or NeuroDB cannot tell which
  came first): the row is not in NeuroDB yet, or NeuroDB still has an older version. An inactive indicator no
  partner report names that NeuroDB still lists says the same: no sync has left such indicators out since the
  update that brought the rule. Wait for tonight's sync (20:30), or ask an Administrator to run the eTools
  Datamart sync, then press *Compare again*.
- **Check the PD in eTools.** NeuroDB has the PD under another status (ended, for instance). If it is active in
  eTools, wait for the next sync and press *Compare again*.
- **Needs a look.** Nothing above explains it (a row the Datamart did not send although NeuroDB's sync ran after
  Power BI's refresh, later the same day too, or a difference not explained yet). Download the Excel file and
  send it to the NeuroDB team. A report missing from the Datamart read is checked in PRP; a row of a PD
  NeuroDB does not hold, a PD indicator or a funds row, in eTools. One reason says more: *NeuroDB has its
  funds on another PD, or on none* (the funds reservation lines name this programme document, and NeuroDB
  links them elsewhere: Raw eTools data → Funds reservation lines). An upload compared before 28 Oct 2026 may
  also list here *NeuroDB lists an indicator eTools marks inactive* with no partner report naming it: check in
  eTools whether it is an older version of the programme document's indicator. Since your decision of that
  day NeuroDB leaves such indicators out: press *Compare again* to see the differences as they are now. A line
  whose programme document's
  budget differs says the budget NeuroDB has for it. A gap of a counted visual takes the reason that most
  needs doing among its reports, never the most common one. An export of exactly 1,000 rows (or 30,000,
  or 150,000) may be cut by Power BI: NeuroDB's rows beyond it say so. Check in Power BI that the visual shows
  no more rows; if it does, export it again one section or one partner at a time, and upload each file
  (nothing to send). When the PD/SPD Report list may be cut, the partner cards, the table by sector and the
  chart by month cannot explain their gaps from it: they say "Power BI may have cut PD/SPD Report" and ask
  for that list again in parts, never that they are cut themselves.

**The upload's page** opens with *What to do now* (at most four lines, each with a link to its differences:
what needs a look, the PDs to check in eTools, what waits for the sync, the exports to export again (cut by
Power BI, empty while NeuroDB's page has rows, or of another country), the files not read, the exports to
compare again, then always one line on what NeuroDB cannot show yet, and a last line on what is expected). It
says "Power BI and NeuroDB agree" only when every export is the same. Then one line per Power BI page, in
Power BI's order, with its verdict ("Same", "Same data, counted another way", "Same, apart from rows before
NeuroDB's history: 12", "NeuroDB cannot show 595 rows yet", "Wait for the next sync: 3", "Check 1 PD in
eTools", "Needs a look: 2", "Power BI may have cut PD/SPD Report: 4", "Empty in Power BI", "Not uploaded") and
what to do, and each export with its file, the rows read, what its filter covers ("2026", "Every year",
"Education only") and its counts: rows compared, the same, different, only in Power BI, only in NeuroDB. While
NeuroDB compares, the page refreshes by itself every few seconds. Once it ran, the page says how much memory
the comparison took and how much it may take ("Memory: peak 214 MB of 250 MB allowed."). An export it had no
memory left for says "Could not be compared: not enough memory" with the same two figures, and the others are
still compared; if the comparison stops, the exports it had not reached yet say "Not compared: the comparison
stopped before it": press *Compare again* (or upload the files again once its details were removed).

**An export's page** shows its filters one by one, its notes (rules that apply to every row are said once
there: Power BI's Section and CP Output are the PD's), the
differences by reason (needs a look first, then what NeuroDB cannot show yet; the expected ones on one line,
with the rows counted in a note), and the lines themselves, the ones that need a look
first, 100 a page: each with Power BI's value and NeuroDB's for every column that differs, the reason, what to
do and **Check in NeuroDB** (the figure's trail, the partner report record, Raw eTools data filtered to the PD,
or the PD's page). Filter the lines by reason or by kind (different, only in Power BI, only in NeuroDB).

**Where Power BI's rule is not known**, NeuroDB counts Power BI's possible rules from its own records and shows
how many rows each fits: Program Performance's and PD/SPD Indicator's Reached (the Accepted quarterly reports
added up, every Accepted report, the latest Accepted report, PRP's cumulative) and Target, Data Quality's
reading of its filter, the financial contribution and Outstanding DCT. The rule chosen is the one that fits
most rows where Power BI is above 0; a difference Power BI's way explains says "Counted Power BI's way: 15.
NeuroDB's own rule gives 35." On the upload's page, **Choices for you** lists what the comparison found when a
rule fits most of the rows (for instance "Power BI's Reached matches 'adds up the Accepted quarterly reports'
on 18 of 20 rows NeuroDB can check"), each with one question; a rule that fits only a few rows is said so, and
no choice is offered. NeuroDB's pages keep their own rules until you decide: nothing on them changes.
NeuroDB follows Power BI in three rules you decided on 26 Oct 2026, so the comparison no longer asks them:
Reporting Progress counts progress reports, the reports to follow up count a report not yet due as Due, and the
Emergency indicator means the programme document is an SPD. A comparison made before that day no longer asks
them either: its page and its Excel file say NeuroDB counts Power BI's way since; press *Compare again* to see
the differences as they are now.

**Download every difference (Excel)** gives a *Summary* sheet (what to do now as the page says it, the files
not read and why, one line per Power BI page and export, the visuals not uploaded, the filters, the notes and
the fit tables) and one sheet per export: a row only on one side on one line, a row that differs on one line
per column. *Compare again* takes the same rows against NeuroDB's newest data, without uploading the files
again. Only the last three uploads keep their rows and lines; older ones keep their summary and verdicts (no
lines and no Excel file: upload the files again to see them).

## The Power BI comparison sheet

NeuroDB never reads Power BI: you compare by hand, and the sheet does the rest. *Power BI comparison sheet
(Excel)*, under the tabs of Progress reports and Partner monitoring, downloads the page's figures for the
filter shown, in the order and with the names of the PRP Power BI dashboard. Home, the grid, the map and
the master indicators have no Power BI page: check Home's blocks, the grid and the map with their figure
trail, and the master indicators against Raw eTools data.

**Statuses read for.** Due, Overdue and Not yet due depend on the day a report's status is read for.
The page reads today; Power BI reads the day of its last refresh. Beside the link, choose the day of
Power BI's refresh (at most 14 days back) before you download.

The file has these sheets:

- **Compare.** At the top: the NeuroDB filter (the page's address), the day the statuses are read for,
  the last sync of the data, a yellow cell for the day of Power BI's refresh, and *Everything NeuroDB
  holds* whatever the filter (with the earliest reporting period NeuroDB holds). Then **Set these in
  Power BI**: each slicer with its value, as the report names it on the Power BI pages of the sheet (both
  spellings where they differ: *Reporting Year / Reporting year*), and a *Done?* cell to tick, with a note
  when a Power BI page of the sheet has no such slicer (Partner Performance has no year slicer: with a year
  chosen, the sheet gives its figures and its Partners list for every year, as Power BI does); **Clear these
  in Power BI**: every other slicer and box of those pages (*Report status*, *Report type*, *Report #*,
  *Partner type*, *PD Reference #*...), among them those whose namesake means something else in NeuroDB
  (Power BI's *CP Output* search matches every CP output of the programme document, and its *Indicator Title*
  box the title alone);
  and **Clear these in NeuroDB before comparing**: the filters Power BI has no slicer for (a search, a report
  number, a period, a CP output...), and on Progress reports the emergency indicator (most of the Power BI
  pages it is compared with have no such slicer). Then one
  row per figure: Power BI's page, the card or visual that shows it (or "No card or visual of Power BI" for a
  figure of NeuroDB's own) and its name, NeuroDB's page and figure, NeuroDB's value, and a yellow cell for
  Power BI's value. Type Power BI's number there: *Difference* and *Same?* fill in by
  themselves. *Notes* say why a figure can differ (a report Power BI counts that the partner reports
  feed does not carry: check 11 of the eTools reporting checks; a programme document with no indicator:
  check 10). Each figure's *Trail* cell opens its figure trail for the same filter and day, on that
  figure (Partner monitoring's on Program Performance or Data quality, for the year the sheet read). When
  the search names a person or holds an e-mail or a phone number, the file writes it cleaned and leaves
  out the addresses: open the trail from the page.
- **Progress reports** (Progress reports): one row per progress report of the filter, with a *Match key*
  (the PD reference, a space, the report number). No focal point is written, and a person named as a partner
  is withheld.
- **Partners** (Progress reports): one row per partner, its progress reports per status, and a *Match key*
  (the vendor number, else the partner's name).
- **Program Performance rows** (Partner monitoring): every indicator of the filter, not only the first
  2,000 the page lists, with NeuroDB's Reached and % Reached, yellow cells for Power BI's, their
  differences, and a *Match key* (PD number | indicator | PD output). Two indicators of one programme
  document with the same title and PD output end in #1, #2: match them by their target. A title or PD output
  that holds a name, an e-mail or a phone is written without it, and the row's note says to match it by PD
  and target. Each row's *Trail*
  cell opens the indicator's Reached on its trail: the partner report rows placed to it, used or not.
- **Why they can differ**: eleven reasons, each with where to see it.

**For a visual NeuroDB does not recognise**, match the rows by hand (on the Progress reports, Partners or
Program Performance rows sheet):

1. In Power BI, open the visual that lists the same rows (the sheet gives Power BI's names for its pages
   and visuals), choose *Export data* and open the file in Excel.
2. In Power BI's file, insert a first column holding the same match key, for instance `=B2&" "&C2` for
   the PD reference and the report number.
3. In NeuroDB's sheet, in an empty column, look each row up in Power BI's file:
   `=VLOOKUP(K3, '[Power BI file]Sheet1'!$A:$Z, 5, FALSE)`: K3 is the row's match key, and 5 the column of
   the value to bring, counted from the match key.
4. Filter the rows where the value is `#N/A` (a row Power BI does not have) or differs, and check those
   rows against Raw eTools data.

The names of Power BI's pages, visuals and slicers in the sheet are those of the shared report, *UNICEF Program
Performance*. A figure is named after a visual only when that visual shows it; the others are on the page's
*Cards*. If your report names one differently, tell the NeuroDB team: they correct it in one place. The sheet
also says to set *Country name* to the one country NeuroDB holds and, on both pages, which of Power BI's
slicers to clear: the page has no such filter, or its filter of that name means something else.

## Raw eTools data

**Raw eTools data** (in the administration, *Data and sync → Raw eTools data*) shows what NeuroDB received
from eTools, record by record, as it came: every eTools Datamart dataset NeuroDB reads, the partner
reports and PRP's own datasets among them, and the records of the eTools REST API (partners, agreements,
programme documents, trips, engagements, action points and their details).

- **The first page** lists the pages and the sources each reads, then every source once, in four tables:
  what Progress reports and Partner monitoring read; the other eTools Datamart datasets; the eTools REST
  API; and what NeuroDB does not read, with why. For each source: the pages that use
  it, how many records are in NeuroDB, its last sync, what eTools said it had, what NeuroDB received, what
  was not kept (of another country, naming no country, records that could not be saved, records eTools sent
  twice), how many records the last sync updated, and **Complete?**: *Yes*, or *No* with the first reason.
  For the assurance engagements, the saves their audits, spot checks and micro-assessments make right after
  them, in the same sync, count as updated by the last sync.
- **A sync that failed** says why in a few words beside the last one that worked: "the sync of 21 Oct 2026,
  16:15 failed (eTools Datamart answered 500)", "(eTools answered 403: not allowed)", "(no answer in time)",
  "(could not connect)", "(NeuroDB was restarted during the run)", "(no PRP business area code is set: enter
  it in Admin → Data and sync → eTools Datamart)". "No answer in time" means eTools took the request but did not
  answer; "could not connect" means NeuroDB could not reach it. "(eTools Datamart answered 403: the account
  NeuroDB uses is not allowed to read it; ask the eTools Datamart team to grant it)" means the account signed
  in (the other datasets read then), but may not read that one dataset: ask the eTools Datamart team to give
  it access (syncing again fails the same way until they do; *Complete?* says "No: not allowed to read it").
  "(eTools Datamart answered 403: it refused NeuroDB's sign-in or this dataset)" means nothing showed which: if
  every dataset fails this way, the team that runs NeuroDB's hosting renews the credentials. Superusers and Administrators can click it
  to open the run, where *What the error means* says what to do. The Datamart's own monitoring
  (`system/monitor`) says "diagnostic: no page or check uses it": its failures are known and change nothing.
- **A source's page** starts with what its last sync received, in one line that adds up: what NeuroDB
  received, less the records of another country, those naming no country and those that could not be
  saved, is what the sync updated. The records in NeuroDB are those the last sync updated, those updated
  since (a sync is running) and those kept from an earlier sync. A remainder the counts do not explain is
  shown in red: tell the NeuroDB team. Records a sync running now (or one that failed) rewrote count as
  updated since: they are explained. For the partners, programme documents, agreements, audits and
  locations, the line counts the copies kept here, and another line says how many records could not be
  saved into NeuroDB's own partners, programme documents, locations...: the ones the pages read. Then
  **Complete?** says *No*. For the partner reports it also says from which date NeuroDB holds them, and
  how many running programme documents have no report row (check 10).
- **Rows eTools no longer sends** (partner reports only). When a complete sync no longer gets a partner report
  row, NeuroDB keeps it and counts it on every page, marked "no longer sent" ("no longer sent 312 (kept and
  counted)"; the list's *Updated* cell says since when, and when it was last sent). Its report keeps the
  status it last had. The mark goes as soon as eTools sends the row again. A row is removed by a sync only
  when the sync gets its new copy, a record of the same report and indicator: one PRP gives the same id at the
  same place, also when an earlier sync got it first (as when eTools renumbers its records while NeuroDB
  reads), or one eTools sends under a new record id, at the same place or under a new id PRP gave the
  indicator. The copy of another indicator with the same title and output never removes a row. Data health and
  check 10 count the rows kept so from the rows themselves, as this page does: they stay right after a sync
  that could not read everything or failed, and after a removal (check 10 from its next run). Check 10 also
  says how many the latest complete sync marked. The line under the counts says how many rows of how many
  reports are kept so, and how many of these reports eTools now sends under the same number for another period
  (mostly reports PRP made again after an amendment): review those first.
- **How funds reservations link to their programme document** (funds reservation lines and funds
  reservations). A line goes to the programme document whose reference number it carries: the exact number,
  else another amendment's number of the same PD, else the PD part alone ("PD2020001"); a line with no number
  takes its FR's number. The eTools id decides only when no number is given, or between the PDs the number
  fits. When it names none of them, a line whose exact number several PDs carry goes to the one whose dates
  cover it, else to the one that ends last; a line whose number fits several PDs in another way goes to none,
  and its PD in NeuroDB reads "several PDs fit". A line with no number at all goes to the PD its eTools id
  names only when its vendor number is that PD's partner's, when it carries no vendor number, or when NeuroDB
  does not know that partner's vendor number: with another partner's vendor number it goes to none, and its PD
  in NeuroDB reads "none (its eTools id names another partner's PD)" (the eTools id alone often names another
  partner's PD). The line under the counts says how the last sync linked them: by which kind of number, by
  eTools id only (and how many of those carry no vendor number, and how many are on a PD whose partner's
  vendor number NeuroDB does not know, so could not be checked), not linked (a number
  of a PD NeuroDB does not hold, and on how many of those the eTools id names a PD NeuroDB holds; a number
  several PDs fit; no number and an eTools id of another partner's PD), how many the eTools id would put on
  another PD, and how many this sync moved to another PD. It also says on how many lines of each kind the
  vendor number is the partner's of the PD they sit on: nearly all, for each kind. The filter's *Programme
  document link* choice "Their number names no PD NeuroDB holds" lists the lines left unlinked so, those
  without a number of their own by their FR's number: as many as the line under the counts says; "No number,
  and their eTools id names another partner's PD" lists those left unlinked for that reason (also *List those
  not linked because their eTools id names another partner's PD* under the counts). If they are many, tell the
  NeuroDB team before using the Funds pages.
- **PRP's own progress reports and their programme documents** (PRP progress reports). PRP's own list holds
  the reports partners still owe for a period that has ended, which the partner reports stop sending. The line
  under the counts says, as the last sync recorded it, whether they are tied to their programme document ("yes,
  the data proves it", or why not); of the reports the partner reports also have, how many end on the same day,
  and how many of those submitted on both sides were submitted on the same day; how many reports are still
  owed for a period that has ended (and how many more no read of PRP's list returned for a week: not counted);
  and whether the pages show them: "yes, since 16 Oct 2026", or why not ("their link to the programme
  documents is not proven", "too few of the reports also in the partner reports end on the same day", "the link
  passed every test on 16 Oct 2026 only, and must pass on the next day too", "the last sync of PRP's progress
  reports or of their programme documents failed or received nothing", "no sync of PRP's progress reports has
  checked it yet", "the check failed on the last sync: tell the NeuroDB team"...). The link must pass on two
  days in a row with the same pair of keys before they can be shown (a day with no sync, or whose last read of
  PRP's progress reports failed or received nothing, starts the count again), and closes at once when a read
  fails it, or fails: a later sync of their programme documents does not open it again. Reports of draft or
  cancelled programme documents are never counted, as the pages never show them. Check 11 gives the figures. Once shown, Home, Progress reports, its Partners tab, Partner
  monitoring's Overdue reports, the PD and partner pages, the daily review and Ask NeuroDB count them. The
  first page says the same
  beside the dataset, and the list gives each report's programme document and partner in NeuroDB ("not
  linked" when none), its period end, due date and submission date, never its narrative or the people who
  wrote it.
- **PD indicators eTools marks inactive** (PD indicators). The line under the counts says how many indicators
  eTools marks inactive in every row, of how many programme documents partners have reported on, and how many
  of them have no partner report row: mostly an older wording of a PD's indicator. Those are left out of
  Programmes → Indicators,
  Partner monitoring (Data quality too) and the programme document pages (your decision of 28 Oct 2026); the
  others stay listed, as partners' values sit on them. The PD indicators and partner reports syncs mark them
  each time, so the pages follow the next sync. The filter's *Active* choice "no" lists the rows of every
  inactive indicator, and "no, and left out of the pages" only those the pages leave out.
- **Programme document budgets.** Each budget record writes the budget of the programme document whose
  reference number it carries (its eTools id decides only when it carries no number: the id often names
  another programme document). A number no programme document in NeuroDB carries writes nothing, nor does
  another amendment's number. When several records name one programme document, NeuroDB keeps one that carries
  its number before one named by its eTools id alone, then the one whose eTools id also names it, then the
  newest, then the last one read. A programme document that a
  record's eTools id named against its number (where NeuroDB used to write another programme document's
  budget), and that no record names now, has its budget emptied: it shows none rather than another's. The line
  under the counts says how the last sync wrote them: by number, by eTools id, not written (and why), the
  programme documents several records named, how many records carry an eTools id of another programme
  document than their number, how often each of their two eTools ids agrees with the number, and how many
  budgets were emptied. The list gives each record's programme document as its number names it ("not in
  NeuroDB" when no programme document NeuroDB holds carries it); filter on a programme document to see every
  record that names it. A budget that still differs from Power BI's: check the record's number in eTools.
- **Records eTools sent twice.** One answer of the eTools Datamart (one page) never lists a record twice, so
  when every copy of a record comes identical in one answer, eTools holds it twice: NeuroDB keeps one, counts
  the copy with what it received ("1,939 the Datamart sent twice (identical)" under *Not kept*) and the sync
  is complete. When the copies come on pages far apart, or differ, NeuroDB cannot tell them from records that
  moved between pages while others did not come: *Complete?* says "No: 1,939 were sent twice and may stand for
  others that did not come; nothing was removed", and nothing is removed until a sync can prove them.
- **Fields this source sends** (folded) lists each field the last sync counted on every record it kept
  (before a sync has counted them, the newest 2,000 records in NeuroDB): how many records carry it, how many
  hold a value, and how NeuroDB uses it (read into one of its columns, names people, written by a person, a
  text, shown as received, or a new field).
- **The filter** keeps the records of a programme document, a partner, a section, a status, a year or a
  date range, a reference or number, a record id, and those the last sync updated or not. For the funds
  reservation lines and funds reservations, also those whose number names no PD NeuroDB holds. For the partner
  reports, also the report type and the programme document link: running, another status, draft or
  cancelled, not in NeuroDB, or names none. These five choices add up to every partner report row. Its
  *Updated by* choices add the rows eTools no longer sends: "eTools sends their number for another period"
  (first to review), "eTools no longer sends them" (all of them) and "PRP's progress reports give them another
  status" (those whose report PRP's own list gives another status, as check 11 counts them; none while the
  last sync did not prove PRP's list is tied to its programme documents). For the PD indicators, also
  *Active*: yes or no.
- **The review list** (partner reports, when the filter holds rows eTools no longer sends) lists their
  progress reports, one line each: programme document, report, period end and when it was last sent, at
  most 50 ("and N more"). Send it to the eTools Datamart team and ask which of these reports PRP deleted.
  Under it, a superuser reads "eTools no longer sends 312 rows of 14 progress reports of this list…", ticks
  *I have reviewed them* and presses **Remove these rows**: only once the Datamart team confirms PRP deleted
  them. It removes the filter's rows no longer sent, up to the last one the page showed; if the rows changed
  since, or a sync is running, nothing is removed and the page says why; if the removal fails, nothing is
  removed and the page says so (its run in *Import and sync runs* says why). The removal is recorded in
  *Import and sync runs* as the eTools update clean-up ("partner_reports:no_longer_sent"), never as a sync.
- **The list** shows 50 records a page, the newest first, each linked to its own page: every field as
  received, then what NeuroDB made of it (its columns, the programme document, partner and location it
  links to; for a partner report row its progress report and the status every page reads for it; for an
  engagement, its audit, spot check... records, each shown as its own source's page shows it) and the sync
  that last updated it.

## The eTools REST API

The eTools REST sync keeps a copy of every record it reads, as it came. To fill or refresh that copy without
changing anything else, a superuser or an Administrator presses **Read the eTools REST API now** on the first
page (also in *Import and sync runs → Run a job → eTools REST raw copy*). It runs in the background, a few
minutes or more; reload the page to see what it received. No page figure changes: only the copy is written.
The eTools REST sync itself also updates the programme documents, whose status decides which ones are
running on the partner reporting pages.

- A record eTools no longer lists goes from the copy, with its details, after a read that went to the end.
  A read that stopped, that received nothing, or that received fewer records than eTools said it had,
  removes nothing. When eTools sends nothing, the page goes on showing the read before it.
- A record's page and its CSV say what NeuroDB holds under the same eTools id: for a programme document,
  NeuroDB's programme document, its status (the one the pages read to decide which are running) and its
  partner; for a partner, an agreement, an engagement or an action point, the partner.
- The details of trips started more than a year ago stay as they were last read.
- If the page says "The eTools REST API token is not set on this server: ask the NeuroDB team.", the copy
  cannot be read until the NeuroDB team sets it.
- When a list cannot be read (eTools refused it, or did not answer), its details are not asked for, and
  neither are the action points when the engagements could not be read: their row says "Not read on 24 Oct
  2026, 20:05 (the partners could not be read)." and nothing in the copy is removed. Read again once the list
  works.
- A source never read says why: being read now, waiting for the read under way, not reached by a read that
  stopped (NeuroDB was restarted), the read failed, or no final engagement to ask for action points about.
  "Not read yet." means no read has ever reached it.

## Printing the pages

Print Raw eTools data, a source or a record from the browser, or save it as PDF: every table prints whole
on A4 landscape, wrapping between words, without the menu, the header, the filter and the buttons. Numbers,
reference numbers and eTools addresses are never cut in two; a date may wrap between its words, and a field's
name in a heading after a "_". *Fields this source sends* prints open.

## People are never shown

People's names, e-mail addresses and phone numbers are never shown, on the pages or in the files. A field
that names people shows how many ("3 people (not shown)"); a text a person wrote (comments, notes) reads
"text withheld (written by a person)"; every other text is shown without the names NeuroDB knows, e-mail
addresses, links and phone numbers. A field whose own name is a person's, an e-mail or a phone is never
shown, nor is such a name inside a field: it adds to a count. Partner names, vendor numbers and programme
document numbers stay, so a row can always be matched.

A search for a person's name is refused: "eTools records cannot be searched for a person."

## Fields not reviewed

A field NeuroDB does not read and nobody reviewed shows only its numbers, yes/no and dates; any text in it
reads "N values (not reviewed)". On a source's page a superuser sees two links beside such a field:
*Look at its values* opens the developer table of that source, where a few of its values can be read
first; *Reviewed: show it* then shows its values to every validator, still without people. *Hide again*
takes the review back.

## Downloads

*CSV of these N records* downloads the filter: one row per record, NeuroDB's columns first (the moment of
the eTools figures, the record id, when it was updated and whether by the last sync, its programme
document and partner in NeuroDB; for the partner reports also its programme document link and its
progress report and indicator report ids), then every field of the source, a field that names people as
a count (a field with the name of one of NeuroDB's columns, such as the record's own vendor number, ends in
"(as received)"), and at the end the people named inside the fields shown and any field first met lower in
the file. The file's name carries the source and the moment of the figures (day, hour and minute).

- A download holds at most 25,000 records for the sources with long texts (partner reports, field
  monitoring findings, action points, financial findings, staff trips) and 100,000 for the others. Above
  that the page says which filter to choose first: a year, a section, a partner or a programme document.
- Two large downloads are prepared at once on the whole site; a third waits: "Another large download is
  being prepared: try again in a minute."
- A filter whose count takes too long says so: add a date range or a programme document.

## Why NeuroDB and Power BI can differ

The comparison sheet's last sheet lists these too, each with where to see it.

- The days differ: Power BI refreshes on its own schedule, NeuroDB every night (choose *statuses read
  for* beside the sheet's link).
- NeuroDB holds partner reports from the date the partner reports' page says; Power BI may show older
  ones.
- PRP's other feeds list reports still owed that the partner reports feed does not carry (check 11 of
  the eTools reporting checks). NeuroDB counts those of PRP's own list of progress reports once their link
  to the programme documents is proven, as Power BI does; until then a line on the pages says so.
- Draft and cancelled programme documents are never read by NeuroDB.
- A report's status is the newest word across all its rows.
- Reached: Power BI's Program Performance counts Accepted reports only (its filter says so); NeuroDB adds
  Accepted and Submitted reports, never adds a value repeated on every location, and takes PRP's cumulative
  for a partial history.
- An indicator is counted once, although eTools lists it once per location and disaggregation.
- Reporting Progress counts progress reports, in Power BI and in NeuroDB; NeuroDB's tiles also give the
  indicator reports in them.
- Both call a report not yet due "Due" in their lists of reports to follow up; NeuroDB's own lists and charts
  call it Not yet due.
- Power BI joins rows that are the same in every column it shows, and repeats a location's row once per CP
  output.
- Power BI shows a percentage indicator as a fraction on its Location and Disaggregation pages.
