# Knowledge base and documents

## The knowledge base

The [knowledge base](/knowledge/) (sidebar: Resources › Knowledge base) holds documents and texts Ask
NeuroDB answers from: reports, evaluations, meeting minutes, guidance and notes. Every signed-in person
reads and searches them.

- **Adding**: Administrators and Section editors use **Add a document or text** (on Ask NeuroDB or on
  the knowledge base page): a file (PDF with a text layer, Word, PowerPoint, Excel, CSV, Markdown or
  text; 50 MB at most) or pasted text, with a title and optionally its source, section and year. Do not
  add personal data of children, beneficiaries or staff.
- **Reading**: once added, the document is read in the background (seconds to a few minutes; the page
  refreshes until it is ready). A scanned PDF without a text layer is refused, as there is no text
  recognition.
- **Search**: the text is cut into passages of about 1,500 characters, with their page, and searched
  in English (and the words as written, for Arabic, place names and reference numbers); the title
  counts most.
- **Links**: the partners (name, short name, vendor number), programme documents (reference number),
  sections, governorates and districts named in the text are linked, word for word. The documents then
  show on the partner's and the programme document's pages.
- **Summary**: when the AI is set up, ChatGPT writes a short summary, key points, the document's date
  and the organisations, places and references it names; names that match NeuroDB records become
  *AI-suggested* links. If this fails, the document is still searchable and says so.
- **Changing or removing**: the person who added a document, or an Administrator, reads it again or
  removes it.

**Library publications and country programme documents** are read into the knowledge base too, so their
text is searched and quoted like an added document; their source reads *Library publication* or the
country programme's name, with *Open the original*. They are changed at their source (the library or
the country programme page), and read again every morning at 07:00 when they changed.

## Periodic reports

Some reports are issued again and again under the same name with a number and a date (for example a
snapshot numbered NUM-37). Each **edition** is a document like any other, and its figures are also kept
by date on [Periodic reports](/knowledge/reports/), so Ask NeuroDB can give the counts of a period,
compare before and after and draw charts.

- Add several editions at once with **Periodic report** ticked; a later edition of a known report is
  recognised by itself. The number comes from "NUM-37", "#37" or "No. 37", the date from the name or
  the text.
- **Charts of values over dates** are read from where their labels sit on the PDF page, without AI.
  Charts saved as pictures cannot be read; the edition names those pages.
- **Other figures** (headline counts, breakdowns, indicators with target) are listed by the AI; a figure
  is kept only when its number is printed on the page it is said to come from.
- **Over time**: when several editions give a value for the same date, the newest edition counts. The
  report's page shows every measure (latest value, the value before, the change, target), a chart and
  the editions with what was read from each. Figures marked *internal use* are flagged.

## The knowledge hub

NeuroDB keeps one index of everything it holds, linked: partners, programme documents, donors and
grants, sections and places, ActivityInfo databases and indicators, the country programme, Compiler
figures, documents, open daily review findings and field monitoring visits (those that started, or
ended when eTools has no start date, in the last 24 months). It holds **names and links**; every figure Ask NeuroDB gives is read live. It is rebuilt
after every sync and every morning at 07:00. [What's new](/help/watch/#whats-new) compares each build
with the one before.

## Document review

The document review reads chosen knowledge base documents (annual reports, donor reports, evaluations,
sector reviews) with the AI and keeps what they say as **findings**, **key statements** and **action
points**, each pointing to the page it comes from. It is switched off until an administrator turns it on,
and only documents put in a **review batch** (a folder for one kind of document) are read, so the cost
stays with what was chosen. A document marked *reference only* stays in its batch and is never read.

How a document is read, in five stages, each noted as done, partly done or failed:

1. **Text**: the text the knowledge base already read (a document still being read waits).
2. **Findings**: the text is read in parts of about 12,000 characters. Each finding is a challenge, a
   recommendation, an observation or an action point, in one to three sentences, with the words of the
   document that support it, the place and date it is about, and a topic from the list (programme →
   subtopic → tag; "Other" when none fits). When a part gets no usable answer it is read again as two
   halves; what still fails is left out and the document is *partly analysed* ("only 80% read").
3. **Locate** (no AI): the supporting words are looked for in the text to give the exact page ("p. 12",
   "slide 4", "sheet 'Budget'"); when they are not found, the pages of the part they came from. The place
   is matched to NeuroDB's governorates and districts; a place not recognised is kept as written.
4. **Key statements**: the main points of the document, each citing its findings, with an urgency from
   0 (background) to 100 (needs action now).
5. **Action points**: at most 15 explicit commitments, with the owner as written ("Unassigned" when the
   document does not say; never a person's name), the deadline (a quarter or a year counts to its last
   day) and the priority.

The **evidence score** (0–100) of a finding is worked out by NeuroDB, never by the AI: the supporting
words found in the text 45, reported by the document rather than interpreted 25, dated 10, placed 10,
given a topic other than "Other" 10.

Documents are read each night (04:40) when they are waiting, failed or partly analysed. A document read
again by the knowledge base with a new text waits to be analysed again. A new analysis replaces what the
AI wrote but keeps people's verdicts on findings whose words did not change, the findings people added
and the status of each action point. [Ask NeuroDB](/help/ask-neurodb/) can search the findings and cite
their pages.

## The document review page

**Knowledge → Document review** (`/knowledge/review/`) has six tabs. Everyone signed in can read it;
administrators and section editors create batches, add documents, start an analysis and review the
findings. The prompts and the switch are in the admin (administrators only).

- **Documents**: the batches (create, rename, archive) and, for the chosen batch, its documents with five
  stage chips (Text, Findings, Locate, Summary, Action points: green done, amber partly, red failed, grey
  not reached; the reason on hover), their counts and "only n% read" when partly analysed. *Upload
  documents into this batch* adds new files; *Or pick knowledge base documents* adds ones already there.
  Per document: **Analyse / Re-analyse** (now, in the background), **Mark as reference** (kept, never
  analysed, out of every count) and **Remove from batch**.
- **Findings**: *All findings* (one row per finding: batch, document and page — a PDF opens at the page —
  date, programme, subtopic, tag, category, place, evidence, the text with its quote on hover, and ✓ ✕ to
  accept or reject, Edit and Delete for editors); *By document* (its key statements, then its findings,
  with **Accept all** and **Reject all**, which change only what is not reviewed yet, and **Add a
  finding** for one the AI missed); *Key statements*; and the *Index* (every document with its stages and
  notes). Filters: batch, programme, tag, category, minimum evidence, review and search. Each view
  downloads as CSV.
- **Dashboard**: documents analysed, findings, key statements, high-urgency statements (urgency 70 or
  more) and open action points; six quality tiles (tagged, located, dated, exact page, reviewed,
  statements cited), each opening the findings that lack it; charts by category, programme, top tags,
  evidence, year and place; and a table by batch. Every tile, bar and batch opens the rows it counts.
- **Synthesis**: themes are topics ranked by how many **different documents** raise them (minimum 2, 3,
  4 or 5 or more; search; challenges only), each with its documents, years, average evidence and best
  findings. *Over time* sorts them into persistent (every year up to the latest), recurring (again after
  a gap), emerging (the latest year only) and no longer raised; *Coverage* lists the themes resting on
  one batch only; *Repeated findings* groups findings of different documents written in nearly the same
  words (at least 60% of their words shared). Nothing here uses AI except **Write a paragraph**: one call
  on one theme's findings, each claim cited "(Document title, p. n)", 50 a day per person.
- **Actions**: open, overdue, high priority, done and derived action points; open ones by owner; the table
  with its filters (Current — documents dated within a year of the newest one — or All time, status,
  overdue only, batch, owner, priority, search). Editors set the status on the row; a new analysis never
  changes it. CSV and Excel downloads.
- **Report**: **Download desk review (.docx)**, a Word file assembled without AI: the scope, the counts,
  the recurring themes with their evidence, the recurring challenges, the repeated findings, the most
  urgent statements, the open action points by owner, the coverage and the method, every claim cited
  "(Document title, p. n)".

**Rejected** findings and statements are left out of every count, chart, synthesis and report (the
Findings tab still lists them, struck through, so a verdict can be changed). The **Verified only** switch
at the top (shown once something was reviewed; yours alone, for as long as you stay signed in) goes further: only
accepted findings and statements feed the Dashboard, the Synthesis, the Actions and the desk review.
Turn it on before circulating the report.
