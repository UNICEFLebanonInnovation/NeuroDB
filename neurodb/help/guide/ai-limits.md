# AI limits

Every AI feature of NeuroDB uses one AI through one shared account, paid in advance: Claude
(Anthropic's model), or ChatGPT (OpenAI's GPT model) where the administrators chose it in AI settings;
Claude is the preferred one. To keep the cost under control, each feature has limits per person and for
the whole office. Every limit counts from **local midnight** (Beirut) unless it says per hour. The numbers below
are the defaults; administrators can change them.

## Per person

| Feature | Limit | At the limit |
|---|---|---|
| Ask NeuroDB | 30 questions an hour; at most 2 being answered at the same time | "You have asked 30 questions in the last hour. Please try again later." |
| Help assistant | 20 questions a day; at most 2 at the same time. A question it declines does not count, up to 20 declined a day. | "You have asked 20 help questions today; the count starts again tomorrow." The [help pages](/help/) stay open. |
| Ask the KM Database | 30 questions a day; at most 2 at the same time. A question counts once the AI has worked on it, even when no finding matches; one it declines does not count (up to 30 declined a day). | The tab lists the findings that match your question, without a written answer: "You have asked 30 questions to the KM Database today; the count starts again tomorrow." |
| Chat with Data (Monitoring insights) | 20 questions a day; at most 2 at the same time, and 4 on the whole site | "You have asked 20 questions today; the count starts again tomorrow." or "The chat is busy; please try again in a minute." |
| Regenerate (AI monitoring brief) | 5 a day | Regenerate is refused until midnight; the latest brief stays. A brief found up to date costs nothing and uses no quota. |
| AI content summary (action points) | 5 a day | The summary is refused until midnight. |
| Write a paragraph (Document review, Synthesis) | 50 a day | "You have written today's 50 paragraphs; the count starts again at midnight." A paragraph refused by the limit or the budget does not count. |

The chips next to each feature say how much you have used: "3 of 20 today".

## For the whole office

| Budget | Daily limit | What it covers |
|---|---|---|
| Every AI feature together | 3,000,000 tokens | The background work (nightly briefs, AI checks, reviews, notes) stops at 80% of it, so people's questions keep the last 20%. Chat with Data, Regenerate, the Help assistant and Ask the KM Database stop at 100%; Ask NeuroDB has its hourly limit only. |
| Monitoring insights | 1,200,000 tokens and 400 calls | Briefs, Regenerate, test runs and Chat with Data. With the nightly briefs this leaves room for about 30–40 chat answers and 10 Regenerates a day. |
| AI checks of the quality rules | 2,000,000 tokens | About 700–1,000 checks a night; older visits wait for the next nights. |
| AI review of action points | 300,000 tokens | About 150–250 reviews a night, and the content summaries. |
| NeuroDB Watch | 300,000 tokens and 24 calls | The morning notes and up to 3 look-ups a day. |
| KM Database assistant | 600,000 tokens | About 20–25 questions a day across the office (each reads up to 40 findings; more if an administrator lowers its effort, fewer if they raise it). When it is used, the Ask tab lists the matching findings without a written answer. It stops at 100% of the shared budget too. |
| Document review | 1,000,000 tokens | The documents of the Documents page put in a review batch, read each night; a 100-page report takes roughly 150,000 tokens, so about six such reports a night. What is left waits for the next night. The theme paragraphs of the Synthesis tab count here too, and stop at 100% of the shared budget. |

A **token** is a piece of a word, about four characters of English. What is sent (the question, the
instructions, the data looked up) and what is written (the answer and the model's reasoning, which
Claude calls its thinking) both count.

When an office budget is used, the feature says so ("Today's AI budget for Monitoring insights is used;
it resets at midnight.") and the pages keep working without AI: the brief written by NeuroDB from the
figures, the rule results already checked, the review verdicts already made.

## When the AI service's credit runs out

When the AI service (Anthropic for Claude, OpenAI for ChatGPT) says the account's credit has run out,
the AI of Monitoring insights, the Help assistant, the KM Database assistant and the document review
pause for **6 hours**, so that no question is sent while it cannot be answered. Ask NeuroDB says
"The AI service's credit for this application has run out. Ask an administrator."

## What is sent, and what never is

Requests are made with nothing kept at the AI service for later use: each one sends what it needs, and
no conversation is stored there. A keyed code stands for the person asking, never a name or an e-mail
address.

- **Never sent**: the team, the visit lead, monitors' e-mail addresses, who an action point is assigned
  to, child-level data, your name or e-mail address.
- **Cleaned before sending**: monitors' notes, checklist answers, action point texts and your question
  to Chat with Data or the Help assistant lose the person names NeuroDB knows, e-mail addresses, phone
  numbers and links. In what you type (a question to any assistant, the page's title, the words of a
  search), an e-mail address goes whole however it is written out: with "at" and "dot" in words, dashes,
  brackets, quotes, stars or slashes ("x [ at ] example [ dot ] org", "x 'at' example 'dot' org"), with an
  at sign and "dot", in capitals, with any ending, with spaces around the dot (also a capital your phone
  puts after it: "x at example. Org"), with letters that only look like "at" or a dot (styled, circled or
  from another alphabet) or with characters nobody sees; the word before "at" and a web address goes too,
  unless it names no one ("children at [link withheld]", "the dashboard at [link withheld]"). A full link
  (one that starts with "https" or "http" and a colon) is never an e-mail address: the word before it stays
  ("the red flag at [link withheld]") unless it is shaped like an address's name ("x.y at" a full link). A
  mistyped ending ("x at example.ogr", "x at example.con") or one that is also a word ("x at
  example.today", "x at example.school before June") goes like any other. A place written short ("at
  mt.lebanon", "at st.elias"), a sentence missing its space after a place ("at Halba.please"), a sentence
  after a place and ". " ("at schools. eg water", "at Akkar. NGO staff", "the enrolment at Halba. Net or
  gross?", "Class 4B at Tyre. Info sessions") or a time before "at" ("9.30am at halba.we met") is no
  address and stays; after ". ", ".net", ".info", ".fr" and the like end an address when nothing of the
  sentence follows them ("x at example. net before June", "x at hotmail. fr") or after "contact", "email"
  or "write to". Your earlier questions are cleaned so again each time they are sent, and come out the
  same (in Chat with Data and the Help assistant, a plain word before a full link goes then, unless it
  names no one). In notes, answers and action point texts, an address goes whole when it is written with
  "dot", in brackets or dashes, with an at sign, in capitals with AT, or with a common ending in lower case
  after any word but a common one, and so does a name with a digit or a full stop before a full link; after
  a plain word, a site's address in capitals, of another ending or with a space after its dot is taken as a
  link only, so "published at" a site's address keeps its words.
- **Documents the AI reads whole** (a document to review or summarise, a report's pages, a Word or text
  CPD, the daily review's findings, the What's new note) lose their e-mail addresses by the same rule as
  notes; links stay in them. A CPD in PDF is sent as the file, as it is.
- **Ask NeuroDB** sends your question, the earlier questions and answers of the conversation and what it
  looks up, after taking out the people it can find: e-mail addresses, phone numbers, links, the full
  names of the people NeuroDB knows and any name after a title (see
  [Ask NeuroDB](/help/ask-neurodb/#what-goes-to-the-ai)).
- **The Help assistant** sends your question, the page you are on (not its address when it holds an
  e-mail address) and the guide sections and settings it looks up; never programme data beyond a visit's
  score explanation, and never a password or a key.
- **Ask the KM Database** sends your question (cleaned as above, an e-mail address written out with "at"
  and "dot" in words, dashes or brackets, or in capitals, taken out whole, and without the KM Database's
  authors),
  the earlier questions and answers of the conversation (each answer as it was shown: table rows that were
  taken out are not sent), the date you asked, what the search understood
  of places and years (and of a follow-up), and the KM Database's findings that match it, with their
  names of people withheld; never its authors, never anything from the rest of NeuroDB. If a name is
  still found, nothing is sent. The *Excel of these findings* under an answer is
  read from the KM Database when you click it: it is never sent to the AI.
