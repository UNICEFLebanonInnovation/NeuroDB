# KM Database

## What the KM Database is

The [KM Database](/km/) holds findings from research, evaluations and assessments, with the documents
they come from. An administrator uploads it as one Excel workbook; each finding is shown as the workbook
gives it.

- **Kept apart.** The KM Database is not linked to partners, programme documents, indicators or any other
  NeuroDB data. Ask NeuroDB, the Documents page, the library, NeuroDB Watch and the search at the top of
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
  ("labour" and "labor", "diarrhoea" and "diarrhea") and common acronyms find what they stand for ("GBV"
  and "gender-based violence", "OOSC" and "out of school", "MHPSS"). "Out of school", "drop out" and "well
  being" are found as phrases (never every finding that says "school"). Words joined by a slash are looked
  for one by one ("girls/boys"), and "school-age" also finds "school age". Put a phrase in quotes ("child
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
- The two tabs keep the filter as you move between them, and a filter or search you clear stays cleared
  (the Documents tab has no document filter: it lists each document).

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
- **Excel of these findings** and **CSV of these findings**, under each answer of *Ask the KM Database*,
  download the findings that answer read, with every column and the same rules: those it cites first,
  marked *Yes* in the column `cited_in_answer`, then the others it read. The *About* sheet says which
  question (as it was kept, with names taken out) and when it was asked. Only you can download the
  findings of your own answers, for 90 days, and only while the version the answer read is shown: once a
  newer version is loaded, ask the question again.
- **Printing** (the page's *Print* button, or the browser's) follows the same rule as a download: the
  authors' column and lines are left off paper (the organisations are printed instead), and names of
  people inside the texts are printed "[name withheld]". The screen still shows the texts in full.
- An administrator can correct how an author entry is written in downloads (an organisation the rule
  missed, a person it took for an organisation, or words that are no name at all) in the administration:
  *KM Database* → *Organisations named in downloads*, or from the version's page (*Count as a person*,
  *Not an organisation*, *Not a name*, *Count as an organisation*). Downloads change at once.

## Ask the KM Database

The third tab, **Ask the KM Database**, answers a question in a paragraph or a few (about 250 words, up to
about 400 when you ask for a paragraph, a description or a summary) from the findings of the KM Database,
and from nothing else. Under every answer, **Excel of these findings** and **CSV of these findings**
download the findings it read.

- **What it reads.** Only the findings of the version shown now that match your question: their key
  finding, measure, ages, analysis, limitations and data collection period, with their document's title,
  year, type, population and methods. It looks for your words in each finding's own text (not in the
  research questions or indicators it is filed under), with UK and US spellings and common acronyms
  joined, and reads the best matches (at most 40, and at most 6 from one document). Nothing else from
  NeuroDB is read: no partner, programme document, indicator, document of the Documents page, library
  entry or person.
- **Write the subject, the rest is understood.** Words that say what you want rather than what it is
  about ("I need a paragraph describing", "adjust the text above", "an excel sheet with each indicator and
  value", "only") are not searched while other words remain. A finding is kept when it holds your words: all
  of them when you write one or two, and a third of them (at least two) when you write more. A question
  for a number, a rate or a percentage ("how many", "number of") reads the findings that give figures
  first.
- **Places.** Every governorate (Akkar, Baalbek-Hermel, Beirut, Bekaa, Keserwan-Jbeil, Mount Lebanon,
  Nabatieh, North, South) is understood in its usual spellings ("Mont Lebanon", "Nabatiyeh", "Nabatiye",
  "Beqaa", "Beyrouth"), with its main districts and towns: a question about the South also reads findings
  about Saida, Tyre or Jezzine, and Mount Lebanon also Jbeil and Keserwan, as older reports count them.
  "South Lebanon" and "the south" mean the South and Nabatieh governorates. The answer reads only findings
  that name the places you write; when fewer than 10 do, it also reads the others that match your words,
  and the line under the answer says so ("No finding names Mount Lebanon: findings from all of Lebanon
  were read."). "All governorates", "by governorate" or "all gov" reads the findings that name the most
  governorates first; "all Lebanon", "national" or "nationwide" reads every place.
- **Years.** "In the past 2 years" means this year and the two before it (in 2026: 2024 to 2026), and
  "since 2023", "2019-2021", "in 2023", "this year", "recent" and "latest" work too: they set the Year
  filter, without the findings whose year is not reported, and the line under the answer says which
  years. To change them, use the Year filter of the Findings tab. The years are those the documents were
  published.
- **The same text twice.** A finding whose text is word for word another one's is read once; the answer
  may cite either, and both open their finding.
- **Your filters.** Open the tab from the Findings tab after filtering (a pillar, a year, one document...)
  and the answer reads only the findings of that filter; the line over the box names it, with **Clear**.
  The words of the search box are not used: your question is its own search. On a finding's or a
  document's page, **Ask about its document** opens the tab for that document. You can also write a
  code in the question: "Level A", "P2" (a pillar), "RQ3" (a research question), a Finding ID or a
  Document ID.
- **What is sent to the AI.** Your question, the earlier questions and answers of the conversation, the
  matching findings it reads, your filters in words, the date you asked and what the search understood
  of places, years and a follow-up. Never the authors, never Document IDs (one you type goes as "this
  document"), links or phone numbers, and never a name of a person: names of NeuroDB's people and of the KM Database's authors are taken out
  of your question, in capitals or not, and names inside the findings (citations, reference lists, people
  quoted, people named with their job) are written "[name withheld]". If a name is still found, nothing is
  sent.
- **The answer.** Each sentence ends with the findings it rests on, as [references]: each one opens its
  finding, in a new tab. An opening sentence that sums up the answer may have none. Figures are given
  exactly as each finding states them: never added up or averaged, compared only when each carries its
  reference. A reference to a finding that was not read for this answer is taken out, and
  the answer says how many. Under the answer, **Findings used for this answer** lists them (with their
  document, year and confidence level), a fold lists every finding read, and **Excel of these findings**
  and **CSV of these findings** download them. The line under the answer says "Answered from N of about M
  matching findings": when more match than were read, narrow the filters to read others.
- **Tables and Excel.** Ask for a table, the figures, each indicator or "numbers and percentages", and the
  answer may end with one short table: the indicator or measure, the figure exactly as its finding states
  it (a percentage is never worked out from a number), the year and the finding's reference, at most
  about 15 rows. A row that gives no finding read for this answer is taken out, and the answer says how
  many. On a phone, a wide table scrolls sideways inside the answer. Ask for an Excel or a sheet and the
  answer points to **Excel of these findings** under it: every finding it read, with the figures inside
  their key finding and measure (the KM Database has no separate column of figures).
- **When the findings do not answer.** The answer says "The KM Database does not answer this question."
  and what related evidence it holds; "answers this only in part" when part is missing, with what the
  search could not find when it can say ("no finding names Mount Lebanon", "no finding of 2022", "no
  finding read compares governorates"). A sentence that cites no finding is counted under the answer:
  check it against the findings (the opening sentence, a sentence whose reference ends the next one of
  its paragraph and a sentence saying what the findings lack are not counted).
- **When the AI is off or a limit is reached** the tab still lists the findings that match your question,
  without a written answer, and says why (to download them, use **Excel of the filter** on the Findings
  tab). Search and the filters work as usual.
- **Limits.** 30 questions a day per person, 2 at the same time, and the KM Database's own share of the
  day's AI budget (see [AI limits](/help/ai-limits/)). A question counts once the AI has worked on it,
  even when no finding matches (the AI first suggests search words, also when a word of your question is
  in no finding, such as a word misspelt). A question over a limit, or one the AI declines, does not
  count. How much the AI thinks before it answers is set by an administrator in AI settings (*KM
  Database assistant effort*, medium by default): more is slower and uses more of the day's budget.
- **Long questions** are read whole; the findings that match the end of the question (most often its
  topic) come first, unless the end only asks for a format ("once done, an excel sheet").
- **A conversation.** A follow-up keeps the subject, places and years of your earlier question and reads
  the findings its answer cited again. A question is a follow-up when it is short ("and for boys?"), when
  it refers to the answer ("do it for all Lebanon", "adjust the text above", "the same for Akkar", "redo",
  "instead", "again"), or when it has no subject of its own (only places, years or what you want: "what
  about Akkar since 2024?", "once done I need an excel sheet"). A question that names its subject, even
  in general words ("the situation of children in all governorates"), starts afresh. A follow-up with no
  words of its own ("can you do it for all governorates?") is answered from the earlier subject, and counts
  like any question. Its own place, years, "all Lebanon" (no place) or "all governorates" (findings that
  name a governorate) replace the earlier ones; its own words are angles added to the subject ("with a
  lens on gender, and disability": a finding on either). The line under the answer says what was kept.
  You can ask to rewrite the previous answer (another angle, place, period or length): it is rewritten
  from the findings it cited, read again, keeping their references, and never from the earlier answer
  alone. A short question on a new topic ("Child marriage?") that no finding joins to the earlier subject
  reads its own findings. **New conversation** starts afresh. **Your recent questions** lists your last
  ten; press one to ask it again. Your questions are kept 90 days.
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
