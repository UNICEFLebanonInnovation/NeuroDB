# AI limits

Every AI feature of NeuroDB uses ChatGPT (OpenAI's GPT model) through one shared OpenAI account, paid
in advance. To keep the cost under control, each feature has limits per person and for the whole
office. Every limit counts from **local midnight** (Beirut) unless it says per hour. The numbers below
are the defaults; administrators can change them.

## Per person

| Feature | Limit | At the limit |
|---|---|---|
| Ask NeuroDB | 30 questions an hour; at most 2 being answered at the same time | "You have asked 30 questions in the last hour. Please try again later." |
| Help assistant | 20 questions a day; at most 2 at the same time. A question it declines does not count. | "You have asked 20 help questions today; the count starts again tomorrow." The [help pages](/help/) stay open. |
| Chat with Data (Monitoring insights) | 20 questions a day; at most 2 at the same time, and 4 on the whole site | "You have asked 20 questions today; the count starts again tomorrow." or "The chat is busy; please try again in a minute." |
| Regenerate (AI monitoring brief) | 5 a day | Regenerate is refused until midnight; the latest brief stays. A brief found up to date costs nothing and uses no quota. |
| AI content summary (action points) | 5 a day | The summary is refused until midnight. |

The chips next to each feature say how much you have used: "3 of 20 today".

## For the whole office

| Budget | Daily limit | What it covers |
|---|---|---|
| Every AI feature together | 3,000,000 tokens | The background work (nightly briefs, AI checks, reviews, notes) stops at 80% of it, so people's questions keep the last 20%. Chat with Data, Regenerate and the Help assistant stop at 100%; Ask NeuroDB has its hourly limit only. |
| Monitoring insights | 1,200,000 tokens and 400 calls | Briefs, Regenerate, test runs and Chat with Data. With the nightly briefs this leaves room for about 30–40 chat answers and 10 Regenerates a day. |
| AI checks of the quality rules | 2,000,000 tokens | About 700–1,000 checks a night; older visits wait for the next nights. |
| AI review of action points | 300,000 tokens | About 150–250 reviews a night, and the content summaries. |
| NeuroDB Watch | 300,000 tokens and 24 calls | The morning notes and up to 3 look-ups a day. |

A **token** is a piece of a word, about four characters of English. What is sent (the question, the
instructions, the data looked up) and what is written (the answer and the model's reasoning) both count.

When an office budget is used, the feature says so ("Today's AI budget for Monitoring insights is used;
it resets at midnight.") and the pages keep working without AI: the brief written by NeuroDB from the
figures, the rule results already checked, the review verdicts already made.

## When the OpenAI credit runs out

When OpenAI says the account's credit has run out, the AI of Monitoring insights and the Help assistant
pause for **6 hours**, so that no question is sent while it cannot be answered. Ask NeuroDB says
"The AI service's credit for this application has run out. Ask an administrator."

## What is sent, and what never is

Requests are made with nothing kept at OpenAI for later use. A keyed code stands for the person asking,
never a name or an e-mail address.

- **Never sent**: the team, the visit lead, monitors' e-mail addresses, who an action point is assigned
  to, child-level data, your name or e-mail address.
- **Cleaned before sending**: monitors' notes, checklist answers, action point texts and your question
  to Chat with Data or the Help assistant lose the person names NeuroDB knows, e-mail addresses, phone
  numbers and links.
- **The Help assistant** sends your question, the page you are on and the guide sections and settings
  it looks up; never programme data beyond a visit's score explanation, and never a password or a key.
