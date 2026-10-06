"""Monitoring insights' AI: the brief of a filter and the chat with its visits (OpenAI, Responses API).

- :mod:`.prompts`: the fixed texts that close every prompt (what the data is, the safety rules) and how a
  version's editable text is put together with them;
- :mod:`.profiles`: the prompt versions (draft, publish, roll back, delete a draft), what each call sends
  as its instructions, and the model a version uses;
- :mod:`.sampling`: whether temperature and top_p are sent, and what happens when the model refuses one;
- :mod:`.budget`: whether an AI call may start (switched on, not paused, within the day's caps) and the
  per-person quotas;
- :mod:`.facts`: what a brief sends, built by code from the stored visits of a filter;
- :mod:`.sections`: the parts of a brief as each prompt version lists them (FMS's insight sections), the
  strict answer format built from them, and how a priority action point is written out;
- :mod:`.insights`: the brief: where and how it is written (in a background process), its checks, which
  brief a page shows, and the nightly briefs;
- :mod:`.fallback`: the brief NeuroDB writes from the figures when the AI is not used;
- :mod:`.tools`: the four field monitoring look-ups of the chat (also offered to Ask NeuroDB, without
  any text), and the context that holds each answer to the page's filter and its limit of texts;
- :mod:`.chat`: Chat with Data: a question answered with those look-ups, streamed to the page;
- :mod:`.citations`: the check of a chat answer's visit links, links and figures before it is shown.

Nothing here reads an eTools record's raw data or the people who made a visit: the AI is sent only what
:mod:`neurodb.fmm.privacy` lets out.
"""

from neurodb.watch.grounding import NAMED_FIELDS

# The fields of the facts whose words may appear in a sentence even when they are system words ("non-food
# items" in a cited narrative or a partner name): passed to watch.grounding as ``named_fields``.
FMM_NAMED_FIELDS = (
    *NAMED_FIELDS,
    "text",
    "label",
    "partner",
    "place",
    "pd",
    "issue",
    "cp_outputs",
    "programme_activities",
    "q1",
    "q2",
    "q3",
    "rule",
)
