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
3. what the page leaves out, filter by filter, in the order the page applies them: the rows, and the indicator
   reports and progress reports left out entirely or partly (some of their rows kept). The PD status filter
   gives the three lines of the page's note (another status, not in NeuroDB, names no programme document);
4. the rows the filter keeps, then the report status when one is chosen (one status per progress report, read
   from all its rows).

Each step with rows has *Download these rows (CSV)*. A download holds at most 25,000 rows: past that, the
step links to Raw eTools data, where choosing a year, a section, a partner or a programme document makes it
smaller. **Each figure on the page** (folded) lists every figure as the page names it, with its value on the
page, its value rebuilt from the rows, *Same?*, how it is counted and its rows as CSV. By location and By
disaggregation have folds of their own: their steps (the limit of the rows the table reads, the report
status, the tab's own filters) and their lines.

**A worked example.** The tile *Overdue* on Progress reports, section Education:

> Overdue: 41 indicator reports in 12 progress reports, the same as the page.
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
> 6. With the status Overdue: 41 indicator reports in 12 progress reports.

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
4. the PD indicator rows of the programme documents kept, then those each filter leaves out (a search, a
   tag, emergency, humanitarian reports), with the indicators left out entirely or partly;
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
the day, and the progress reports they make: this is not Progress reports' *Overdue* tile.

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
the row: `report_status_neurodb` (the report's status), `report_status_chosen`, `section_counted`,
`period_end_month`, and one column per unit (`indicator_report_unit`, `progress_report_unit`, `partner_unit`,
`pd_unit`) with `counts_once_*`: 1 on the first row of each unit. Then the record as Raw eTools data writes it
(people as counts).

1. Open the file in Excel and insert a pivot table.
2. Filter `report_status_chosen` on *yes*.
3. Put `report_status_neurodb` in Rows (or `section_counted` with it in Columns, for the section table; or
   `period_end_month`, for the month chart).
4. Put the sum of `counts_once_indicator_report` in Values: the tiles. The sum of
   `counts_once_progress_report` gives the progress reports under them, and the sum of
   `counts_once_partner` the Partners tab's *Partners*. A distinct count of `indicator_report_unit`,
   `progress_report_unit` or `partner_unit` gives the same.

Each figure's own *Rows (CSV)* holds only the rows it counts, with `unit_key` (its unit) and `counts_once`:
their sum is the figure. In a step's CSV, `counts_once` is 1 only on a unit the step leaves out entirely
(`unit_partly_kept` marks the others), so its sum is what the step leaves out entirely.

When the database takes too long, a file ends with the row "The download stopped here: the database took too
long. Download again or narrow the filter." That file is not complete: do not compare it.

## Compare with Power BI

In the administration, *Data and sync → Compare with Power BI*, upload the files you exported from Power BI:
the zip as your browser saved it, or the Excel files, several at once. Today NeuroDB reads each export, shows
Power BI's filters one by one and drops people's names as it reads. Comparing the exports with NeuroDB's pages,
row by row, comes next: until then, check the figures with the Power BI comparison sheet below. It is for
validators with Staff status, like Raw eTools data.

**How to export.** The page lists the 13 visuals of the *UNICEF Program Performance* report, in the order of
its pages. On each: *More options (…) → Export data → Summarized data → .xlsx*. On every page, set *Reporting
Year* to 2026 (NeuroDB holds partner reports from 1 Jan 2026), *PD status* to active, and clear *Section*,
*Partner*, *Document type*, *Office*, *Emergency indicator*, *CP Output* and *Indicator Title*. A file with a
sensitivity label that encrypts it cannot be read: export it again without the label.

**What NeuroDB does with the files.**

- It recognises each export **by its columns**, never by its file name, so a renamed file is still read. A file
  NeuroDB does not recognise is named with the columns it knows. The month chart is recognised whichever report
  statuses it shows (a past year has no *Not Yet Due*).
- It reads Power BI's own filters (the *Applied filters* line Power BI writes at the end of the file) and says,
  filter by filter, how it applies each: applied, applied as NeuroDB reads it, or one it cannot apply (clear
  that slicer in Power BI and export again, or compare that export by hand). A filter on a field NeuroDB does
  not know is listed without its value.
- **People's names are dropped as the files are read.** The *UNICEF Focal Points* column of PD/SPD Report and
  the *Focal Point* column of Partner Fund Monitoring are never kept, shown, written in a file or sent to the
  AI; the page says "Dropped as the file was read". A column with values but no name is dropped too, and so is
  the sheet's name. Partners, programme documents and indicators stay, without the names NeuroDB knows, e-mail
  addresses and phone numbers.
- The hidden files a Mac adds to a zip are skipped and counted.

**The upload's page** opens with *What to do now* (at most four lines: when the comparison stopped, what to do
first, such as "upload the files again" or "press Compare again"; files to export again, visuals not uploaded,
and the filters to clear, each slicer by its name on the report, such as *Section* or *PD status*), then one
line per Power BI page, in Power BI's order: each export with its file, the rows NeuroDB read, what its filter
covers ("2026", "Every year", "Education only") and its result. A visual you did not upload says *Not
uploaded*; an export the stopped comparison never read says *Not read*. Open a file's name to see its filters
one by one, the columns kept and its notes (for instance "Power BI may have cut this export at 1,000 rows:
check the visual shows no more"). While NeuroDB reads the files the page refreshes by itself every few seconds.
The *Last uploads* list counts, for each upload, the exports read and the files not read.

*Compare again* takes the same rows against NeuroDB's newest data, without uploading the files again. Only the
last three uploads keep their rows; older ones keep their summary.

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
  Power BI**: each slicer with its value, and a *Done?* cell to tick; **Clear these in Power BI**: the slicers
  NeuroDB's page has no filter for, and those whose namesake means something else in NeuroDB (Power BI's
  *Emergency indicator* is Yes for an SPD, NeuroDB's is the indicator's high frequency; Power BI's *CP Output*
  search matches every CP output of the programme document, and its *Indicator Title* box the title alone);
  and **Clear these in NeuroDB before comparing**: the filters Power BI has no slicer for (a search, a report
  number, a period, the emergency indicators, a CP output...). Then one
  row per figure: Power BI's page, visual and name for it, NeuroDB's page and figure, NeuroDB's value, and a
  yellow cell for Power BI's value. Type Power BI's number there: *Difference* and *Same?* fill in by
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

**Matching row by row** (on the Progress reports, Partners or Program Performance rows sheet):

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
Performance*. If your report names one differently, tell the NeuroDB team: they correct it in one place. The
sheet also says to set *Country name* to Lebanon (NeuroDB holds one country) and, on Progress reports, which of
Power BI's slicers to clear because the page has no such filter.

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
  date range, a reference or number, a record id, and those the last sync updated or not. For the partner
  reports, also the report type and the programme document link: running, another status, draft or
  cancelled, not in NeuroDB, or names none. These five choices add up to every partner report row.
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
  the eTools reporting checks).
- Draft and cancelled programme documents are never read by NeuroDB.
- A report's status is the newest word across all its rows.
- Reached: Power BI's Program Performance counts Accepted reports only (its filter says so); NeuroDB adds
  Accepted and Submitted reports, never adds a value repeated on every location, and takes PRP's cumulative
  for a partial history.
- An indicator is counted once, although eTools lists it once per location and disaggregation.
- Reporting Progress in Power BI counts progress reports; NeuroDB's tiles and charts count indicator reports.
- Power BI's lists call a report not yet due "Due".
- Power BI joins rows that are the same in every column it shows, and repeats a location's row once per CP
  output.
- Power BI shows a percentage indicator as a fraction on its Location and Disaggregation pages.
