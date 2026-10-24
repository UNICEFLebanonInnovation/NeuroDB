# KM Database

## What the KM Database is

The [KM Database](/km/) holds findings from research, evaluations and assessments, with the documents
they come from. An administrator uploads it as one Excel workbook; each finding is shown as the workbook
gives it.

- **Kept apart.** The KM Database is not linked to partners, programme documents, indicators or any other
  NeuroDB data. Ask NeuroDB, the knowledge base, the library, NeuroDB Watch and the search at the top of
  every page do not read it.
- **Where it is.** In the full menu, under Resources, after Library. Superusers see it there; a superuser
  can add it to a role's menu in the administration's *Menu* page (no role has it until then). Its
  address opens for every signed-in person whose menu does not show it, from links and bookmarks.
  Donor accounts never see it.
- **One version at a time.** The line under the page's title says when the workbook was prepared (or,
  when the workbook does not say, when it was loaded), when it was loaded, and how many documents and
  findings it holds, with the number marked for review.

## The two tables

- **Findings** (the first tab, with its number of findings): each finding's key finding (the first
  lines; **Show all** opens the rest), its Finding ID, its pillar and research question (hold the pointer
  over a code to read it in full), its document with the year, and the confidence level. On a phone the
  table shows the finding only, with its document, pillar, question and level on a line under it.
- **Documents** (the second tab, with its number of documents): each document with its Document ID, its
  publishing entity or authors as the workbook gives them (on screen only: never printed or downloaded),
  its year, its type, its confidence level and the number of its findings (press it to see them in the
  Findings tab).
- Each table shows 25 rows a page; **Rows a page** under it shows 50 or 100. The newest year comes
  first, then the workbook's order; after a search, the best match comes first. Press a column's title to
  sort by it (the pillar, the document, the year, the confidence level; on the Documents tab the title,
  the year and the number of findings).
- **Marked for review**: the workbook itself marks this finding or document for review. It is shown, never
  hidden.
- **Check this value**: a value that is not one of the expected ones (for example a confidence level that
  is not Level A to D). It is shown as the workbook has it; hold the pointer over the mark to read it.
- **Confidence levels**: Levels A to D, as the workbook grades the evidence. The workbook does not say
  which level is the strongest, so every level is shown in the same colour.

## Finding what you need

- **Search** (always in view, also on a phone): type words and the table keeps the findings that hold
  them all, in their key finding, measure, analysis, population, methods, limitations or document title.
  Words are found in their other forms ("school" finds "schools"), UK and US spellings find each other
  ("labour" and "labor", "programme" and "program") and common acronyms find what they stand for ("GBV"
  and "gender-based violence", "OOSC" and "out of school", "MHPSS"). Put a phrase in quotes ("child
  marriage"), a minus before a word to leave it out (-camp), or "or" between words for either. A Finding ID
  or a Document ID opens that finding or that document's findings. When no word matches, the table shows
  the findings that contain the letters you typed, and says so.
- **The filters**: *Pillar*, *Research question* (listed under its pillar: the same code is worded
  differently in each pillar), *Confidence level*, *Year* (or *Not reported*) and *Document type*.
- **More filters**: *Analytical domain*, *Age groups*, *Girls and boys*, *Level of the social-ecological
  model*, *Cross-cutting themes*, where the evidence comes from (collected by the document itself, or
  from other sources) and *Data quality* (ready, or marked for review or with a value to check). Tap a
  choice to add it; tap it again to take it out.
- **How choices combine**: several choices in one filter show findings that match any of them; several
  filters together show findings that match all of them. Age groups and themes count a finding marked Yes
  for them. A research question picked in another pillar than the pillars picked is left out,
  and the table says so.
- On a phone the filters fold behind **Filters**; the search box stays in view. **Reset** clears them all.
- The two tabs keep the filter as you move between them (the Documents tab has no document filter: it
  lists each document).

## A finding and its document

- **Open** (on each row) shows the whole finding in a window, without leaving the table; *Open as page*
  gives it its own page, to share or print.
- A finding's page starts with the key finding, then: *The finding* (its analysis, measure and
  limitations), *Where it fits* (pillar, analytical domain, research question and its parts), *Who it is
  about* (the age group as written, then what is marked Yes, by age groups, girls and boys,
  levels and themes; the rest folds under *Not marked Yes*), *The evidence* (origin, confidence level,
  methods and sample, period, data quality) and *Its document*. Empty fields are not
  shown.
- When a finding's type differs from its document's, the page gives both.
- A document's page lists the document's details, then its findings, 25 a page. *Open in
  the table with filters* shows them in the Findings tab, to filter or download.
- A link to a finding or document that a newer version removed says so, with a link back to the table.

## Downloads and printing

- **Excel of the filter** and **CSV of the filter** (over each table) download every row of the filter
  and the search, with every column. The Excel file has an *About* sheet: when the version
  was prepared and loaded, the filter in words, the number of rows and the rules below. An Excel file
  holds up to 20,000 findings; the CSV file has no limit.
- **Authors are shown on screen only.** The files name the organisations an entry names and count the
  individual authors without naming them: "UNICEF + 2 authors", "Authors: 3", or the organisations alone.
  When it is not sure whether a name is a person or an organisation, it counts it as a person.
- **Names of people inside the texts are withheld** in the files: citations ("Doe et al. (2019)", "(Doe,
  2019)"), people quoted by their first name and age, a document's own authors and names after a title
  are written "[name withheld]". Links and figures are kept.
- Your search words are written in the *About* sheet only when none of them names an author.
- **Printing** (the page's *Print* button, or the browser's) shows the organisations only, as a download
  does: the authors' column and lines are left off paper.
- An administrator can correct how an author entry is written in downloads (an organisation the rule
  missed, a person it took for an organisation, or words that are no name at all) in the administration:
  *KM Database* → *Organisations named in downloads*, or from the version's page. Downloads change at once.

## When a new version is loaded

An administrator uploads a new version of the workbook in the administration: *KM Database* →
*Versions* → **Upload a new version** (or *Upload the KM Database* on the administration's home page).
The workbook is checked first; if something is wrong, nothing is loaded and the page says what to correct,
with the row numbers as Excel shows them. A workbook that passes is loaded in the background (about a
minute). Until it is ready, the page shows the version it showed before, with a line saying a new version
is loading (the very first time, it says the KM Database is being loaded). Once it is ready, the page shows
the new version: every document and finding of the old one is replaced. If the load does not finish, the
version shown stays as it was.
