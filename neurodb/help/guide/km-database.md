# KM Database

## What the KM Database is

The [KM Database](/km/) holds findings from research, evaluations and assessments, with the documents
they come from. An administrator uploads it as one Excel workbook; each finding is shown as the workbook
gives it.

- **Kept apart.** The KM Database is not linked to partners, programme documents, indicators or any other
  NeuroDB data. Ask NeuroDB, the knowledge base, the library, NeuroDB Watch and the search at the top of
  every page do not read it. It has its own assistant, on its **Ask the KM Database** tab, which reads
  the KM Database only (see below).
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
  Findings tab). Under a filter or a search, the number counts the findings that match it ("2", "of 3"),
  and pressing it shows those findings, with the same filter.
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
  2019)", "(Doe 2019, Roe 2020)", "Doe and Roe 2020"), people quoted by their first name and age, a
  document's own authors and names after a title are written "[name withheld]". Links and figures are kept.
- Your search words are written in the *About* sheet only when none of them names an author.
- **Printing** (the page's *Print* button, or the browser's) follows the same rule as a download: the
  authors' column and lines are left off paper (the organisations are printed instead), and names of
  people inside the texts are printed "[name withheld]". The screen still shows the texts in full.
- An administrator can correct how an author entry is written in downloads (an organisation the rule
  missed, a person it took for an organisation, or words that are no name at all) in the administration:
  *KM Database* → *Organisations named in downloads*, or from the version's page. Downloads change at once.

## Ask the KM Database

The third tab, **Ask the KM Database**, answers a question in a few sentences from the findings of the
KM Database, and from nothing else.

- **What it reads.** Only the findings of the version shown now that match your question: their key
  finding, measure, ages, analysis and limitations, with their document's title, year, type, population
  and methods. It looks for your words in each finding's own text, with UK and US spellings and common
  acronyms joined, and reads the best matches (at most 40, and at most 6 from one document). Nothing else
  from NeuroDB is read: no partner, programme document, indicator, knowledge base document, library entry
  or person.
- **Your filters.** Open the tab from the Findings tab after filtering (a pillar, a year, one document...)
  and the answer reads only the findings of that filter; the line over the box names it, with **Clear**.
  The words of the search box are not used: your question is its own search. On a finding's or a
  document's page, **Ask about its document** opens the tab for that document. You can also write a
  code in the question: "Level A", "P2" (a pillar), "RQ3" (a research question), a Finding ID or a
  Document ID.
- **What is sent to the AI.** Your question, the matching findings it reads and your filters in words.
  Never the authors, never Document IDs (one you type goes as "this document"), links or phone numbers,
  and never a name of a person: names of NeuroDB's people and of the KM Database's authors are taken out
  of your question, and names inside the findings (citations, reference lists, people quoted, people
  named with their job) are written "[name withheld]". If a name is still found, nothing is sent.
- **The answer.** Each sentence ends with the findings it rests on, as [references]: each one opens its
  finding, in a new tab. A reference to a finding that was not read for this answer is taken out, and
  the answer says how many. Under the answer, **Findings used for this answer** lists them (with their
  document, year and confidence level), and a fold lists every finding read. The line under the answer
  says "Answered from N of about M matching findings": when more match than were read, narrow the
  filters to read others.
- **When the findings do not answer.** The answer says "The KM Database does not answer this question."
  and what related evidence it holds; "answers this only in part" when part is missing. A sentence that
  cites no finding is counted under the answer: check it against the findings.
- **When the AI is off or a limit is reached** the tab still lists the findings that match your question,
  without a written answer, and says why. Search and the filters work as usual.
- **Limits.** 30 questions a day per person, 2 at the same time, and the KM Database's own share of the
  day's AI budget (see [AI limits](/help/ai-limits/)). A question counts once the AI has worked on it,
  even when no finding matches (the AI first suggests search words). A question over a limit, or one the
  AI declines, does not count.
- **A conversation.** A short follow-up ("and for boys?") keeps the topic of your last question and reads
  the findings its answer cited again. **New conversation** starts afresh. **Your recent questions**
  lists your last ten; press one to ask it again. Your questions are kept 90 days.
- The AI can be wrong: open the findings it cites before you use an answer.

## When a new version is loaded

An administrator uploads a new version of the workbook in the administration: *KM Database* →
*Versions* → **Upload a new version** (or *Upload the KM Database* on the administration's home page).
The workbook is checked first; if something is wrong, nothing is loaded and the page says what to correct,
with the row numbers as Excel shows them. A workbook that passes is loaded in the background (about a
minute). Until it is ready, the page shows the version it showed before, with a line saying a new version
is loading (the very first time, it says the KM Database is being loaded). Once it is ready, the page shows
the new version: every document and finding of the old one is replaced. If the load does not finish, the
version shown stays as it was.

A link to a finding that the new version no longer holds says so. A reference in an older answer opens
the finding with a line saying the answer was written from an older version, so the finding may have
changed since; earlier answers are not sent again once a new version is shown.
