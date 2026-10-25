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
| KM Database assistant | 600,000 tokens | About 25–30 questions a day across the office (each reads up to 40 findings). When it is used, the Ask tab lists the matching findings without a written answer. It stops at 100% of the shared budget too. |
| Document review | 1,000,000 tokens | The knowledge base documents put in a review batch, read each night; a 100-page report takes roughly 150,000 tokens, so about six such reports a night. What is left waits for the next night. The theme paragraphs of the Synthesis tab count here too, and stop at 100% of the shared budget. |

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
  numbers and links.
- **The Help assistant** sends your question, the page you are on and the guide sections and settings
  it looks up; never programme data beyond a visit's score explanation, and never a password or a key.
- **Ask the KM Database** sends your question (cleaned as above, and without the KM Database's authors)
  and the KM Database's findings that match it, with their names of people withheld; never its authors,
  never anything from the rest of NeuroDB. If a name is still found, nothing is sent.
