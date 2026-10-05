"""Chat with Data: a question about the visits of the page's filter, answered by the AI with the four
field monitoring look-ups (:mod:`.tools`) and streamed to the browser.

The view (``fmm.views.chat_stream``) checks the question and the limits, writes the question's row
(:class:`~neurodb.fmm.models.ChatQuestion`, in progress) and streams :func:`stream`, which:

- sends FMM's own prompt (``compose(version, "chat")``), then the date and the scope line, in place of
  Ask NeuroDB's prompt (``RunOptions.base_prompt``), and offers only the four look-ups;
- binds the page's filter and the answer's limit of texts for each tool call (``RunOptions.tool_context``),
  so a look-up can narrow the filter but never widen it, and passes every result through
  ``privacy.chat_filter`` (cleaned again, last check);
- re-sends the last turns of the same conversation and filter (:func:`history`): the questions (cleaned
  when asked) and the checked answers, cleaned again and cut; the visits those answers cited and the
  figures their look-ups returned count as looked up, so a follow-up may cite them;
- restarts once per sampling parameter the model refuses before it writes anything (``fmm.ai.sampling``);
- checks the answer (:mod:`.citations`) before it is shown and kept, and records the call in the AI use
  ledger (feature ``fmm``).

Ask NeuroDB's question log and its hourly limit are not touched: the chat has its own log, daily quota
and concurrency limits.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import openai
from django.conf import settings

from neurodb.assistant import agent, usage
from neurodb.assistant.views import _friendly, _sse, _storable

from .. import privacy
from ..models import ChatQuestion, PromptVersion
from . import budget, citations, profiles, sampling, tools

logger = logging.getLogger(__name__)

HISTORY_TURNS = agent.HISTORY_TURNS  # earlier turns re-sent with a follow-up (6)
SEED_NUMBERS = 2000  # figures of earlier turns that count as looked up, at most
CACHE_KEY = "neurodb-fmm-chat"
QUESTION_CHARS = 1000
TOKENS = 30_000  # what one answer may need, asked of the day's AI budget before it starts


def clean_question(question: str) -> str:
    """The question as it is sent and kept: names, e-mail addresses, phone numbers and links removed."""
    return privacy.clean(question, QUESTION_CHARS)[0]


def history(
    user, conversation: uuid.UUID, scope_hash: str
) -> tuple[list[dict[str, str]], set[str], set[str]]:
    """The last answered turns of ``conversation`` under the same filter (changing the filter starts a
    fresh history): the questions and the checked answers, cleaned again and cut to
    ``FMM_HISTORY_ANSWER_CHARS``; with the visit keys those answers kept and the figures their look-ups
    returned (at most ``SEED_NUMBERS``), which seed the new answer's context."""
    rows = list(
        ChatQuestion.objects.filter(
            user=user, conversation=conversation, scope_hash=scope_hash, status=ChatQuestion.Status.ANSWERED
        )
        .order_by("-created_at")
        .values("question", "answer", "checks")[:HISTORY_TURNS]
    )
    rows.reverse()
    names_ = privacy.names()
    turns: list[dict[str, str]] = []
    seen: set[str] = set()
    numbers: set[str] = set()
    for row in rows:
        turns.append(
            {
                "question": row["question"],
                "answer": privacy.clean(row["answer"], settings.FMM_HISTORY_ANSWER_CHARS, names_)[0],
            }
        )
        checks = row["checks"] or {}
        seen.update(str(key) for key in checks.get("kept") or ())
        for number in checks.get("numbers") or ():
            if len(numbers) < SEED_NUMBERS:
                numbers.add(str(number))
    return turns, seen, numbers


def context(scope, version: PromptVersion, seen: set[str] = frozenset(), numbers: set[str] = frozenset()):
    """The context of one answer: the page's filter, the texts it may read (``narr``; none while the AI's
    texts are off) and the visit cards per look-up (``comp``), seeded with the earlier turns."""
    return tools.ChatContext(
        scope=scope,
        texts_left=version.narratives_sampled if settings.FMM_AI else 0,
        cards_max=version.comparison_visits or tools.ASK_CARDS,
        seen=set(seen),
        numbers=set(numbers),
    )


def options(version: PromptVersion, ctx: tools.ChatContext, plan: sampling.Plan) -> agent.RunOptions:
    """The chat's run: FMM's prompt in place of Ask's, the four look-ups bound to ``ctx``, the version's
    limits and the sampling parameters the plan sends."""
    return agent.RunOptions(
        tools=tuple(tools.FMM_TOOLS),
        base_prompt=profiles.compose(version, "chat"),
        instructions=profiles.scope_line(ctx.scope),
        max_rounds=version.chat_max_rounds,
        time_limit=version.chat_time_limit,
        effort=version.effort,
        max_output_tokens=version.chat_max_output_tokens,
        cache_key=CACHE_KEY,
        model=profiles.model_of(version),
        tool_filter=privacy.chat_filter(ctx),
        read_only=True,
        tool_context=lambda: tools.bind(ctx),
        sampling=tuple(plan.params.items()),
    )


def _snapshot(ctx: tools.ChatContext) -> dict[str, Any]:
    return {
        "texts_left": ctx.texts_left,
        "texts_sent": ctx.texts_sent,
        "privacy_blocked": ctx.privacy_blocked,
        "seen": set(ctx.seen),
        "numbers": set(ctx.numbers),
        "urls": set(ctx.urls),
    }


def _restore(ctx: tools.ChatContext, snap: dict[str, Any]) -> None:
    for name, value in snap.items():
        setattr(ctx, name, set(value) if isinstance(value, set) else value)


def stream(
    request,
    row: ChatQuestion,
    ctx: tools.ChatContext,
    version: PromptVersion,
    turns: list[dict[str, str]] | None = None,
) -> Iterator[str]:
    """Answer the question of ``row`` (written when it was asked) as Server-Sent Events, and complete the
    row at the end (see the module's notes). ``turns``: the earlier turns (:func:`history`), read here
    when not given."""
    started = time.monotonic()
    model = profiles.model_of(version)
    plan = sampling.plan(version, model, version.effort)
    params = dict(plan.params)
    states = dict(plan.states)
    seeded = set(ctx.numbers)
    start = _snapshot(ctx)
    outcome = agent.Outcome()
    checked: citations.Checked | None = None
    if turns is None:
        turns = history(request.user, row.conversation, row.scope_hash)[0]
    try:
        retries = 0
        while True:
            outcome = agent.Outcome()
            wrote = False
            run = options(version, ctx, replace(plan, params=dict(params)))
            try:
                for event in agent.answer(row.question, turns, outcome, user=request.user, options=run):
                    if event["type"] == "text":
                        wrote = True
                    if event["type"] == "done":
                        checked = citations.verify(outcome.answer, ctx)
                        outcome.answer = checked.text
                        yield _sse(
                            {
                                "type": "done",
                                "html": checked.html,
                                "answer": checked.text,
                                "notice": checked.notice,
                            }
                        )
                        continue
                    yield _sse(event)
            except openai.BadRequestError as exc:
                parameter = sampling.unsupported_param(exc)
                if wrote or parameter is None or parameter not in params or retries >= sampling.MAX_RETRIES:
                    raise
                # the model refused a sampling parameter before writing: record it, start again without it
                sampling.record(model, version.effort, parameter, accepted=False, detail=str(exc))
                del params[parameter]
                states[parameter] = sampling.NOT_APPLIED
                retries += 1
                _restore(ctx, start)
                continue
            break
        if outcome.calls:
            for parameter in params:
                sampling.record(model, version.effort, parameter, accepted=True)
                states[parameter] = sampling.APPLIED
    except Exception as exc:  # the stream has started: report errors as events, never as a 500
        budget.trip(exc)
        message = _friendly(exc)
        if message is None:
            logger.exception("Monitoring insights: the chat failed")
            message = "Something went wrong while answering. Please try again."
        else:
            logger.warning("Monitoring insights chat: %s: %s", type(exc).__name__, exc)
        outcome.status, outcome.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        yield _sse({"type": "error", "message": message})
    finally:
        if outcome.status == ChatQuestion.Status.ANSWERED and checked is None:
            # The browser went away (Stop button, closed tab) before the answer was complete.
            outcome.status, outcome.error = ChatQuestion.Status.FAILED, outcome.error or "stopped"
        new_numbers = sorted(ctx.numbers - seeded)[:SEED_NUMBERS]
        row.answer = _storable(checked.text if checked is not None else "")
        row.status = outcome.status if outcome.status in ChatQuestion.Status.values else "failed"
        row.checks = {
            "kept": checked.kept if checked else [],
            "removed": checked.removed if checked else [],
            "unchecked_numbers": checked.unchecked_numbers if checked else [],
            "numbers": new_numbers,
            "privacy_blocked": ctx.privacy_blocked,
        }
        row.tools = _storable(outcome.tools)
        row.texts_sent = ctx.texts_sent
        row.model = outcome.model or model
        row.sampling = sampling.summary(version, states, model, version.effort)
        row.input_tokens = outcome.input_tokens
        row.cache_read_tokens = outcome.cache_read_tokens
        row.output_tokens = outcome.output_tokens
        row.duration_ms = int((time.monotonic() - started) * 1000)
        row.error = _storable(outcome.error)
        try:
            row.save()
        except Exception:  # the answer has been sent: a failed log write must not break the stream
            logger.exception("Monitoring insights: could not log chat question %s", row.pk)
        # the answer's model calls in the day's AI use of the shared key (never raises)
        usage.record(usage.FMM, model, outcome, calls=outcome.calls)
