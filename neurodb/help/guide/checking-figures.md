# Checking the figures

**For validators.** This page is for the people who check NeuroDB's eTools figures against the raw data:
superusers and the members of the *Validators* group. Everyone else sees the pages exactly as before.

## Who can check figures

A superuser or an Administrator ticks the group in the administration: *Users and access → Users*, open
the person, then *Groups*: tick **Validators** and save. Administrators are validators only when they are
ticked too, like anyone else; superusers always are.

Raw eTools data is in the administration, which also asks for **Staff status** (on the same form). A
validator without it sees "Raw data: ask for staff status" where the link would be.

A validator sees one more line under the tabs of [Progress reports](/partner-reporting/),
[Partner monitoring](/partner-monitoring/) (and its map) and [Field monitoring](/field-monitoring/), and on
[Home](/) under *Partner reporting*: **Raw data behind this page**. It opens Raw eTools data on the
sources that page reads. The *eTools reporting checks* (*Data and sync*) open read-only
for validators: only an Administrator runs them.

## Raw eTools data

**Raw eTools data** (in the administration, *Data and sync → Raw eTools data*) shows what NeuroDB received
from eTools, record by record, as it came: every eTools Datamart dataset NeuroDB reads, the partner
reports and PRP's own datasets among them.

- **The first page** lists the pages and the sources each reads, then every source once, in four tables:
  what Progress reports and Partner monitoring read; the other eTools Datamart datasets; the eTools REST
  API (not kept here yet); and what NeuroDB does not read, with why. For each source: the pages that use
  it, how many records are in NeuroDB, its last sync, what eTools said it had, what NeuroDB received, what
  was not kept (of another country, naming no country, records that could not be saved), how many
  records the last sync updated, and **Complete?**: *Yes*, or *No* with the first reason.
- **A source's page** starts with what its last sync received, in one line that adds up: what NeuroDB
  received, less the records of another country, those naming no country and those that could not be
  saved, is what the sync updated. The records in NeuroDB are those the last sync updated, those updated
  since (a sync is running) and those kept from an earlier sync. A remainder the counts do not explain is
  shown in red: tell the NeuroDB team. For the partners, programme documents, agreements, audits and
  locations, the line counts the copies kept here, and another line says how many records could not be
  saved into NeuroDB's own partners, programme documents, locations...: the ones the pages read. Then
  **Complete?** says *No*. For the partner reports it also says from which date NeuroDB holds them, and
  how many running programme documents have no report row (check 10).
- **Fields this source sends** (folded) lists each field of the newest 2,000 records in NeuroDB: how many
  records carry it, how many hold a value, and how NeuroDB uses it (read into one of its columns, names
  people, written by a person, a text, shown as received, or a new field).
- **The filter** keeps the records of a programme document, a partner, a section, a status, a year or a
  date range, a reference or number, a record id, and those the last sync updated or not. For the partner
  reports, also the report type and the programme document link: running, another status, draft or
  cancelled, not in NeuroDB, or names none. These five choices add up to every partner report row.
- **The list** shows 50 records a page, the newest first, each linked to its own page: every field as
  received, then what NeuroDB made of it (its columns, the programme document, partner and location it
  links to; for a partner report row its progress report and the status every page reads for it) and
  the sync that last updated it.

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
a count, and at the end the people named inside the fields shown and any field first met lower in the
file. The file's name carries the source and the moment of the figures (day, hour and minute).

- A download holds at most 25,000 records for the sources with long texts (partner reports, field
  monitoring findings, action points, financial findings, staff trips) and 100,000 for the others. Above
  that the page says which filter to choose first: a year, a section, a partner or a programme document.
- Two large downloads are prepared at once on the whole site; a third waits: "Another large download is
  being prepared: try again in a minute."
- A filter whose count takes too long says so: add a date range or a programme document.

## Why NeuroDB and Power BI can differ

- The days differ: Power BI refreshes on its own schedule, NeuroDB every night.
- NeuroDB holds partner reports from the date the partner reports' page says; Power BI may show older
  ones.
- Draft and cancelled programme documents are never read by NeuroDB.
- A report's status is the newest word across all its rows.
- An indicator is counted once, although eTools lists it once per location and disaggregation.
