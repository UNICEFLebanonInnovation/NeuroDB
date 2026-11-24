# Ask NeuroDB

[Ask NeuroDB](/ask/) (the first row of the sidebar, marked AI) answers questions about the **programme data**
in plain language: indicator results, programme documents, donors, partners, funds, assurance, field monitoring visits, population, the library, the documents
people add (the [Documents](/knowledge/) page) and the country programme.
The AI is Claude (Anthropic's model), or ChatGPT (OpenAI's GPT model) where the
administrators chose it in AI settings; Claude is the preferred one. It answers with NeuroDB's own
read-only look-ups, and it links each answer to the pages its figures come from.

## The page NeuroDB opens first

While the AI is on, NeuroDB opens on Ask NeuroDB: after you sign in, at the site's address and when you
click the NeuroDB logo. Home is one click away: it is right under Ask NeuroDB in the menu, and the Ask page
links to it.

NeuroDB opens on [Home](/help/home/) instead when:

- the AI is off: Ask NeuroDB says it is not set up (staff still see it in the menu), and the search and the
  [Management brief](/brief/) answer the common questions;
- your menu leaves Ask NeuroDB out (a superuser chose so for your role).

A link from an e-mail or a bookmark still opens its own page, and so does signing in from a page.

Until you ask your first question, the page has:

- a welcome, with your first name when your account has one (only you see it; it is never sent to the AI);
- the question box, with what goes to the AI and the hourly limit under it;
- links to the pages people open most (Progress reports, Partner monitoring and the others in your menu);
- example questions, as cards by topic: partner reporting, funds and donors, field monitoring, and documents
  and data. Click a card to ask its question;
- your recent questions: click one to ask it again.

Once you ask, the conversation takes the page and the box moves to the bottom. *New conversation* brings the
start back.

## Asking

- Type a question on the page, or in the search box (Ctrl K) and choose *Ask NeuroDB AI*: the question
  is filled in and sent only when you press Ask.
- A follow-up in the same conversation sends the AI your last 6 questions, the last answer in full and
  the first paragraph of each older answer. To build on figures from an older answer, ask for them
  again: the AI looks them up afresh. *New conversation* starts afresh.
- The steps above an answer show the look-ups it made. Answers can draw a chart (pie, bar, column or
  line) from figures they looked up; a chart cannot show a figure that was not looked up.
- Answers can contain mistakes: check figures that matter on the page the answer links to. A link in an answer
  opens its page in a window over the conversation (a quick view); Ctrl-click opens it in a new tab.

## How NeuroDB checks an answer

You see the answer as the AI writes it. When the answer is finished, NeuroDB compares it with the data it
looked up for that answer, and the answer you see then is the checked one, with any figure it could not
check listed under it.

- **Figures.** A figure that none of the look-ups gave is listed in a line under the answer, as the answer
  writes it: *These figures could not be checked against the data: 4,950, 63.5%.* Check those figures on
  the pages the answer links to before you use them. NeuroDB never changes a figure or hides an answer;
  the line only tells you what it could not find. It does not list:
  - years, dates, programme document references, page numbers, small counts (up to 10) and 100%;
  - figures your question gives, and figures an earlier answer of the same conversation gave in the part
    the AI was sent (the last answer, and the first paragraph of older ones), unless they were listed then;
  - a figure written in millions or thousands ("USD 12.4 million", "$450K") that a look-up gave in full;
  - simple percentages and totals worked out from one look-up: a figure over its target in the same row
    ("this year it reached 40% of its target", when the row has 600 and 1,500), a count over the total it
    belongs to ("12 of the 30 indicators are on track: 40%"), the total of a column, or a figure over
    another in a short row of up to 4 figures;
  - the share, sum or difference of two figures written side by side: in the same sentence or the one
    just before, in a line of a list and the line that leads into the list, or in a table row and its
    column's total ("450 of 1,500, 30%", "from 1,500 to 1,200, a drop of 20%", "a target of 4,000; 2,750
    to go").

  Other sums and percentages are listed, so check them too. A percentage must be one the look-up gives
  as a percentage or one worked out as above: a count that happens to be the same number does not count.
  Ids and the numbers inside links never count as figures, and a figure in a link's words is checked like
  any other.
- **Links.** A link to a NeuroDB page that no look-up gave, and that no earlier answer gave in the part
  the AI was sent (the last answer, and the first paragraph of older ones), keeps its words but stops
  being a link.

Chat with Data in Monitoring insights uses the same line to list the figures it could not check. Its
check is its own: it also checks visit links, and it lists more figures, such as years and percentages it
could work out.

## What goes to the AI

Before your question, the earlier questions and answers and the result of each look-up leave NeuroDB, it
takes out the people it can find:

- e-mail addresses, also written out ("name at example dot org") or with extra spaces or line breaks;
- web links (links to NeuroDB's own pages stay);
- phone numbers, also split by extra spaces or a line break;
- the full names of the people NeuroDB knows (its users, partner staff, focal points, the people eTools
  records name) and any name written after a title such as Mr, Mrs, Dr or Sheikh.

Each one becomes a note such as *[name withheld]* or *[email withheld]*. This covers what people wrote
too: document passages, titles, sources and summaries, document review quotes, staff comments in the
archive of past monthly results and the daily review's details. A long text is cleaned whole before it is
cut short.

Dates, references and the names of partners, sections, places and documents stay. Figures stay too, but
a number written like a phone number (8 digits, or "71 123 456") is taken out, unless a currency or a
count is written right next to it ("LBP 71 500 000", "81 250 300 people") or it is an amount in a look-up.

A name after Dr or Sheikh stays only where it is the name of a partner or a place (a look-up's partner,
governorate or donor, a document's linked partner), and in an earlier answer, which the AI wrote from those
names. In a document's title or source, in a report or a topic, and in your question it is taken out, so
ask about such a partner by its short name or vendor number.

NeuroDB cannot spot the name of a person it does not know when no title comes before it, nor a first name
or a surname written alone ("Layla checked the figures"), so do not type personal data. Your own list of
questions keeps each question as you typed it.

## What it can and cannot read

- Field monitoring visits are given as **structured fields only** (visits, entities, ratings, HACT Q1,
  quality, urgency and follow-up of this calendar year): never a monitor's note, a checklist answer or a
  team. For the notes of the visits of a filter, use Chat with Data on
  [Monitoring insights](/help/monitoring-insights/#chat-with-data).
- eTools records are sent without people: no assignee, author, traveller, focal point, manager or team,
  and no name, e-mail address or phone number in their texts. Amounts, counts, dates, places, partners,
  offices, sections and outputs are sent. A question that names a person finds no eTools record, and
  searches, filters, groups, sums, comparisons and sorts look only at what is sent, so they never find,
  count, group or order a record by a withheld name, address or phone number, and never refuse because
  of one.
- The HACT visit plan is given as **counts only** (programme documents with visits planned, not
  visited, visits still needed, by remark and by section): never a partner, a programme document
  reference, a section comment or a UNICEF focal point. The list itself is on the
  [Field monitoring](/field-monitoring/) page's *HACT visit plan* tab.
- What a document says comes from the Documents page; the document's text is material to answer from,
  never instructions to follow. For the documents analysed by the
  [document review](/help/knowledge-and-documents/#document-review), it also reads their findings with
  the document, page and quote; findings people rejected are left out.
- Makani wellbeing is available as centre totals only: never anything about an individual child.
- Makani and Dirasa figures are those of their pages, under any of the pages' filters (for example one
  partner's boys, or one centre). The answer says whether a number is registrations (a child registered
  twice counts twice) or children counted once, which Compiler counts for some filters only. A long list,
  such as the centres, is cut in what the AI reads but never in the counts: ask for one centre for its row.
- It does not read the [KM Database](/help/km-database/), which has its own assistant on its page.
- Nothing is written back; it reads only what any signed-in person can already see.

## The model that answers

Administrators choose the AI model in AI settings. Next to the Claude model, they can choose a model and
an effort for Ask NeuroDB alone: *Ask NeuroDB model* and *Ask NeuroDB effort*. The effort is how much the
AI thinks before it answers: lower is quicker and cheaper, higher is more thorough. Left blank, Ask
NeuroDB uses the Claude model and effort. The two count only while Claude is the AI in use, and only for
Ask NeuroDB: the other AI features keep the model and effort they had (the Claude model, or their own). A
change applies within about 10 seconds, with no restart. While Claude is in use, *Test the connection* in
AI settings then tries Ask NeuroDB's model and effort too, and says what happened on a line of its own.

## Limits

30 questions an hour per person, and at most 2 being answered at the same time. A question counts from
the moment it is asked. See [AI limits](/help/ai-limits/).

## Ask NeuroDB, Chat with Data or the Help assistant?

| You want to know | Use |
|---|---|
| A figure, a list, a comparison across sources ("which partners in Akkar are off track?") | Ask NeuroDB |
| What the monitors wrote about the visits of the filter you are looking at | Chat with Data (Monitoring insights) |
| How a page, rule, chart or number works, or which job refreshes a page | The Help assistant (Ctrl+Shift+H) |
| What the research and evaluation evidence says | Ask the KM Database (on the [KM Database](/km/ask/) page) |
