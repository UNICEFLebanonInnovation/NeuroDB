# Ask NeuroDB

[Ask NeuroDB](/ask/) (sidebar, right under Home, marked AI) answers questions about the **programme data**
in plain language: indicator results, activity reports, Neuro and HPM reports, programme documents,
donors, partners, funds, assurance, field monitoring visits, population, the library, the knowledge base
and the country programme. The AI is Claude (Anthropic's model), or ChatGPT (OpenAI's GPT model) where the
administrators chose it in AI settings; Claude is the preferred one. It answers with NeuroDB's own
read-only look-ups, and it links each answer to the pages its figures come from.

## Asking

- Type a question on the page, or in the search box (Ctrl K) and choose *Ask NeuroDB AI*: the question
  is filled in and sent only when you press Ask.
- A follow-up in the same conversation sends the AI your last 6 questions, the last answer in full and
  the first paragraph of each older answer. To build on figures from an older answer, ask for them
  again: the AI looks them up afresh. *New conversation* starts afresh.
- The steps above an answer show the look-ups it made. Answers can draw a chart (pie, bar, column or
  line) from figures they looked up; a chart cannot show a figure that was not looked up.
- Answers can contain mistakes: check figures that matter on the page the answer links to.

## How NeuroDB checks an answer

Before you see an answer, NeuroDB compares it with the data it looked up for that answer.

- **Figures.** A figure that none of the look-ups gave is listed in a line under the answer: *These
  figures could not be checked against the data: 4950.* Check those figures on the pages the answer
  links to before you use them. NeuroDB never changes a figure or hides an answer; the line only tells
  you what it could not find. It does not list years, dates, programme document references, page
  numbers, small counts (up to 10), figures your question gives, figures an earlier answer of the same
  conversation gave in the part the AI was sent (the last answer, and the first paragraph of older ones),
  unless they were listed then, or simple percentages and totals: a reached figure over its target in the
  same row of a look-up (1,200 of 1,500 is 80%), the total of a column, or the share, sum or difference of
  two figures written in the same sentence ("450 of 1,500, 30%", "from 450 to 1,200, 750 more"). Other
  sums and percentages are listed, so check them too.
- **Links.** A link to a NeuroDB page that no look-up gave (and no earlier answer of the conversation
  gave) keeps its words but stops being a link.

Monitoring insights' Chat with Data checks its answers the same way and uses the same words.

## What goes to the AI

NeuroDB sends no person to the AI. Before your question, the earlier questions and answers and the result
of each look-up leave NeuroDB, it takes out:

- e-mail addresses, also written out ("name at example dot org");
- web links (links to NeuroDB's own pages stay);
- phone numbers;
- the names of the people NeuroDB knows (its users, partner staff, focal points, the people eTools
  records name) and any name written after a title such as Mr, Mrs, Dr or Sheikh.

Each one becomes a note such as *[name withheld]* or *[email withheld]*. This covers what people wrote
too: document passages and summaries, document review quotes, staff comments on Neuro and HPM reports
and the daily review's details. Figures, dates, references and the names of partners, sections, places
and documents stay. In a look-up's result, a partner or place named after Dr or Sheikh keeps its name;
in your question it does not, so ask about it by its short name or vendor number.

NeuroDB cannot spot the name of a person it does not know when no title comes before it, so do not type
personal data. Your own list of questions keeps each question as you typed it.

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
- What a document says comes from the knowledge base; the document's text is material to answer from,
  never instructions to follow. For the documents analysed by the
  [document review](/help/knowledge-and-documents/#document-review), it also reads their findings with
  the document, page and quote; findings people rejected are left out.
- Makani wellbeing is available as centre totals only: never anything about an individual child.
- It does not read the [KM Database](/help/km-database/), which has its own assistant on its page.
- Nothing is written back; it reads only what any signed-in person can already see.

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
