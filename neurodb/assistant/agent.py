"""The AI assistant: an OpenAI GPT model (ChatGPT, through the OpenAI Responses API) answering
questions with NeuroDB's read-only tools.

``answer()`` runs the function-calling loop and yields small event dicts as they happen, so the view
can stream them to the browser:

    {"type": "text", "text": "...", "round": n}   answer text as the model writes it
    {"type": "tool", "label": "...", "round": n}   a data lookup is starting
    {"type": "done", "html": "...", "answer": "..."}
    {"type": "error", "message": "..."}

Requests are stateless (``store=False``, nothing is kept on OpenAI's side for later retrieval): every
round sends the conversation so far, replaying the previous rounds' output items (reasoning items
with their encrypted content, messages and function calls) followed by the tool results.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import markdown
import nh3
import openai
from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac
from markdown.extensions.tables import TableExtension

from . import tools

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 25000  # per model call; reasoning tokens count too, so leave room for them
HISTORY_TURNS = 6  # earlier question/answer pairs sent back for follow-up questions
PROMPT_CACHE_KEY = "neurodb-assistant"  # every request shares the same tools + instructions prefix
CONNECT_TIMEOUT = 10.0  # seconds to reach the API: an unreachable API fails fast
READ_TIMEOUT = 120.0  # longest silence in a stream (the model may reason for a while before it writes)
MIN_WAIT = 60.0  # the SDK only retries a call while each attempt can still wait this long
# Error codes of OpenAI's safety checks: the question is declined, and asking again will not help.
POLICY_BLOCKS = frozenset({"invalid_prompt", "bio_policy", "cyber_policy", "misalignment_policy_violation"})

SYSTEM_PROMPT = """\
You are the NeuroDB assistant for UNICEF Lebanon. NeuroDB holds the country office's programme \
monitoring data: ActivityInfo databases per section and reporting year with master indicators, \
targets and activity reports from partners; Neuro and HPM reports; eTools programme documents, \
partners and donor funding, with funds reservations, grants, PD indicators, HACT assurance (audits, \
spot checks, assessments), action points and field monitoring from the eTools Datamart; population \
figures; and a library of studies and maps.

Answer the user's question from this data using the tools. Look numbers up; never estimate or \
invent them. When the data cannot answer the question, say so plainly and say what the data does \
cover. If a question is ambiguous (which year, which section), choose the most likely reading, say \
which one you used, and answer.

How to work:
- Start from list_databases or find_indicators to get ids, then fetch the detail you need. Several \
lookups can run in parallel.
- Indicator progress and achievement come from database_results (the official aggregation). \
activity_breakdown counts raw activity reports; its summed_value is only a total for one indicator.
- Reporting years are named like "2026". The current reporting year is used when none is given.
- Partner implementation monitoring (is a partner or PD on track, what was reported per month and \
location on a PD indicator) comes from pd_indicator_progress. Partners reported in ActivityInfo \
before eTools; that history is linked to the same partner: partner_details lists it per year and \
database, partner_activityinfo gives the indicators, values and months.
- eTools data (partners, programme documents, funds, audits, monitoring, partner reporting) is all \
linked by partner and programme document. The page tools (programme_details, partner_details, \
funds_overview, partner_reporting, assurance_overview) summarise it; for anything else use \
etools_datasets to find the dataset and its fields, then etools_query to filter, count or add up; \
etools_search finds where a name or reference appears.
- Before a lookup you may say one short sentence about what you are checking.

How to answer:
- Lead with the direct answer in the first sentence, then the supporting figures. Keep it brief.
- Use Markdown. Use a table when comparing more than three items. Format numbers with thousands \
separators and give percentages to one decimal place.
- Link to the NeuroDB page the figures come from, using the url values returned by the tools as \
Markdown links, e.g. [Child Protection dashboard](/databases/3/). Never invent URLs.
- Mention the data's date or freshness when it matters (e.g. "as of the last import on 13 Sep").
- End when the question is answered: no offers of further analysis and no follow-up questions.
"""

REFUSED = "The assistant could not answer this question. Try rephrasing it."


class AssistantUnavailable(Exception):
    """The assistant is switched off or has no API key."""


class ServiceError(Exception):
    """The model service ended a response with an error: a failed response, an error event in the
    stream, or a stream that stopped before the response was complete."""

    def __init__(self, code: str | None, message: str = "") -> None:
        self.code = code or "error"
        self.message = message
        super().__init__(f"{self.code}: {message}" if message else self.code)


@dataclass
class Outcome:
    """What happened, for the question log."""

    answer: str = ""
    status: str = "answered"
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    def add_usage(self, usage: Any) -> None:
        """Add one model call's tokens. OpenAI counts cached prompt tokens inside input_tokens; they
        are logged apart, so input_tokens + cache_read_tokens is the whole prompt."""
        if not usage:
            return
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        self.input_tokens += (usage.input_tokens or 0) - cached
        self.cache_read_tokens += cached
        self.output_tokens += usage.output_tokens or 0  # includes the reasoning tokens


def client() -> openai.OpenAI:
    if not settings.AI_ASSISTANT_ENABLED:
        raise AssistantUnavailable("The AI assistant is not configured.")
    # A plain number would also be the connect timeout: an unreachable API would then hold the
    # request for 3 x 120 s. Each model call is further held to the time left (_within).
    return openai.OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=openai.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        max_retries=2,
    )


def _within(api: openai.OpenAI, remaining: float) -> openai.OpenAI:
    """The client for one model call, held to the time left so that a hanging API cannot keep the
    question (and its worker thread) far past the time limit: every attempt may wait at most its
    share of the time left, and the SDK retries only while each attempt can still wait MIN_WAIT
    seconds (so near the end there is no retry, nor a Retry-After sleep)."""
    retries = max(0, min(api.max_retries, int(remaining // MIN_WAIT) - 1))
    wait = min(READ_TIMEOUT, max(remaining / (retries + 1), CONNECT_TIMEOUT))
    return api.with_options(timeout=openai.Timeout(wait, connect=CONNECT_TIMEOUT), max_retries=retries)


def _instructions() -> str:
    # Stable text first and the date last, so the cached prompt prefix (the tools and these
    # instructions) only changes when the day does.
    from neurodb.indicators.services.navigation import current_year

    today = timezone.localdate()
    year = current_year()
    return (
        f"{SYSTEM_PROMPT}\nToday is {today:%A %d %B %Y}. "
        f"The current reporting year is {year.name if year else 'unknown'}."
    )


def safety_identifier(user: Any) -> str | None:
    """A stable per-user id for OpenAI's abuse monitoring: a keyed hash, never the id or email."""
    if user is None or not getattr(user, "pk", None):
        return None
    digest = salted_hmac("neurodb.assistant.safety_identifier", str(user.pk), algorithm="sha256")
    return digest.hexdigest()  # 64 characters, the API's maximum


def _request(user: Any) -> dict[str, Any]:
    """The parameters shared by every model call of one answer (``input`` is added per round)."""
    params: dict[str, Any] = {
        "model": settings.AI_ASSISTANT_MODEL,
        "instructions": _instructions(),
        "tools": tools.definitions(),
        "reasoning": {"effort": settings.AI_ASSISTANT_EFFORT},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],  # reasoning is replayed between rounds
        "prompt_cache_key": PROMPT_CACHE_KEY,
    }
    if identifier := safety_identifier(user):
        params["safety_identifier"] = identifier
    return params


def history_messages(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Earlier turns as plain text (tool calls are not replayed; follow-ups re-query as needed).
    Answers are labelled final answers, as newer models expect the phase of assistant messages."""
    messages: list[dict[str, Any]] = []
    for turn in history[-HISTORY_TURNS:]:
        if turn.get("question") and turn.get("answer"):
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": turn["answer"], "phase": "final_answer"})
    return messages


# A link to one of NeuroDB's own pages: a path, not "//host" or "/\host" (both leave the site),
# with no spaces or control characters (browsers drop tabs and newlines inside a URL).
_SITE_PATH = re.compile(r"/(?![/\\])[^\x00-\x20\x7f\\]*")


def _site_links_only(tag: str, attr: str, value: str) -> str | None:
    """Keep only links to NeuroDB's pages (the tools return relative urls): a link elsewhere, for
    example one planted in partner-entered text, keeps its text but loses its href."""
    if tag == "a" and attr == "href":
        return value if _SITE_PATH.fullmatch(value) else None
    return value


def render(text: str) -> str:
    """Markdown answer to safe HTML: no images, scripts or styles; only links to NeuroDB's own pages,
    which open in a new tab."""
    # align="..." instead of style="text-align: ..." so column alignment survives sanitizing
    html = markdown.markdown(text, extensions=[TableExtension(use_align_attribute=True), "sane_lists"])
    return nh3.clean(
        html,
        tags={
            "p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li", "h3", "h4", "h5",
            "table", "thead", "tbody", "tr", "th", "td", "a", "hr",
        },
        attributes={"a": {"href", "title"}, "th": {"align"}, "td": {"align"}},
        url_schemes={"https", "http"},
        attribute_filter=_site_links_only,
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )  # fmt: skip


def _stream_round(api: openai.OpenAI, request: dict[str, Any], round_no: int):
    """One model call. Yields text events while streaming; returns the finished response, which is
    completed, incomplete or failed (the SDK only builds a final response for a completed one)."""
    try:
        with api.responses.stream(**request) as stream:
            wrote = False  # a message of this round has written text
            for event in stream:
                if event.type == "response.output_text.delta":
                    wrote = True
                    yield {"type": "text", "text": event.delta, "round": round_no}
                elif (
                    event.type == "response.output_item.added"
                    and wrote
                    and getattr(event.item, "type", "") == "message"
                ):
                    # another message (e.g. the answer after a progress note): keep the two apart
                    yield {"type": "text", "text": "\n\n", "round": round_no}
                elif event.type in ("response.incomplete", "response.failed"):
                    return event.response
                elif event.type == "error":  # the SDK hands this event over instead of raising
                    raise ServiceError(event.code, event.message)
            try:
                return stream.get_final_response()
            except RuntimeError as exc:  # the stream ended without a response.completed event
                raise ServiceError("stream_ended", str(exc)) from exc
    except RuntimeError as exc:
        # The SDK's stream helper could not follow the stream, e.g. an error event came before
        # response.created ("Expected to have received `response.created` before `error`").
        raise ServiceError("stream_error", str(exc)) from exc


def _replay(output: list[Any]) -> list[dict[str, Any]]:
    """A response's output items as input items for the next round.

    Built field by field rather than passing the SDK objects back: those carry client-side fields
    (``parsed``, ``async_``) that are not part of the API's input. Reasoning items keep their
    encrypted content (the API keeps nothing with store=False) and messages keep their phase.
    """
    items: list[dict[str, Any]] = []
    for item in output:
        if item.type == "reasoning":
            replay = {
                "type": "reasoning",
                "id": item.id,
                "summary": [{"type": "summary_text", "text": s.text} for s in item.summary],
            }
            if item.encrypted_content:
                replay["encrypted_content"] = item.encrypted_content
        elif item.type == "message":
            replay = {
                "type": "message",
                "role": "assistant",
                "id": item.id,
                "status": item.status or "completed",
                "content": [
                    {"type": "refusal", "refusal": part.refusal}
                    if part.type == "refusal"
                    else {"type": "output_text", "text": part.text, "annotations": []}
                    for part in item.content
                ],
            }
            if getattr(item, "phase", None):
                replay["phase"] = item.phase
        elif item.type == "function_call":
            replay = {
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            }
            if item.id:
                replay["id"] = item.id
        else:
            continue  # only function tools are offered, so no other item types are expected
        items.append(replay)
    return items


def _answer_text(output: list[Any]) -> str:
    """The answer in a response's messages. Newer models label each message with a phase: when one
    is the final answer, progress notes (phase "commentary") are left out. Without a final answer
    (older models without phases, or commentary only) every message counts."""
    messages = [item for item in output if item.type == "message"]
    final = [item for item in messages if getattr(item, "phase", None) == "final_answer"]
    texts = (
        "".join(part.text for part in item.content if part.type == "output_text")
        for item in final or messages
    )
    return "\n\n".join(text for text in texts if text)


def _refused(output: list[Any]) -> bool:
    return any(part.type == "refusal" for item in output if item.type == "message" for part in item.content)


def _arguments(raw: str | None) -> Any:
    """A function call's JSON arguments (an empty string means none)."""
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise tools.ToolInputError(f"The arguments are not valid JSON ({exc}).") from exc


def _run_tools(calls: list[Any], outcome: Outcome) -> list[dict[str, Any]]:
    """Run the model's function calls; each result or error goes back as a function_call_output."""
    results = []
    for call in calls:
        started = time.monotonic()
        args: Any = call.arguments
        try:
            args = _arguments(call.arguments)
            output, ok = json.dumps(tools.run(call.name, args), ensure_ascii=False), True
        except tools.ToolInputError as exc:
            # Unknown tool, unreadable JSON or arguments outside the schema: the model can correct it.
            output = json.dumps({"error": str(exc), "received": args}, ensure_ascii=False, default=str)
            ok = False
        except Exception:
            logger.exception("assistant tool %s failed", call.name)
            output, ok = json.dumps({"error": "The lookup failed on the server."}), False
        outcome.tools.append(
            {
                "tool": call.name,
                "input": args if isinstance(args, dict) else str(args)[:1000],
                "ok": ok,
                "ms": int((time.monotonic() - started) * 1000),
            }
        )
        results.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
    return results


def answer(
    question: str, history: list[dict[str, str]], outcome: Outcome, user: Any = None
) -> Iterator[dict[str, Any]]:
    """Run the conversation to a final answer, yielding progress events. Fills ``outcome``.

    ``user`` (the person asking) is only used for the hashed safety identifier sent to OpenAI.
    """
    api = client()
    deadline = time.monotonic() + settings.AI_ASSISTANT_TIME_LIMIT_SECONDS
    request = _request(user)
    items: list[dict[str, Any]] = [*history_messages(history), {"role": "user", "content": question}]
    final_text = ""
    round_no = 0
    while round_no < settings.AI_ASSISTANT_MAX_TOOL_ROUNDS:
        remaining = deadline - time.monotonic()
        if remaining < 0:
            outcome.status, outcome.error = "failed", "time limit"
            yield {"type": "error", "message": "This question took too long. Try asking something narrower."}
            return
        round_no += 1
        try:
            response = yield from _stream_round(
                _within(api, remaining), {**request, "input": list(items)}, round_no
            )
            outcome.add_usage(response.usage)
            outcome.model = response.model or outcome.model
            if response.status == "failed":
                error = response.error
                raise ServiceError(error.code if error else "failed", error.message if error else "")
        except (ServiceError, openai.APIError) as exc:
            if exc.code not in POLICY_BLOCKS:
                raise
            # Blocked by OpenAI's safety checks (in the stream or as an HTTP error): declined.
            outcome.status, outcome.error = "refused", ": ".join(filter(None, (exc.code, exc.message)))[:2000]
            yield {"type": "error", "message": REFUSED}
            return
        output = list(response.output)
        details = response.incomplete_details if response.status == "incomplete" else None
        reason = details.reason if details else None

        if reason == "content_filter" or _refused(output):
            outcome.status, outcome.error = "refused", reason or ""
            yield {"type": "error", "message": REFUSED}
            return
        if response.status == "incomplete":
            # Cut off: never run a function call from this response, its arguments may be partial.
            if reason == "max_output_tokens":
                outcome.status, outcome.error = "failed", "max_output_tokens"
                yield {"type": "error", "message": "The answer was too long. Try a narrower question."}
            else:
                outcome.status, outcome.error = "failed", f"incomplete: {reason}"
                yield {"type": "error", "message": "The answer could not be completed. Please try again."}
            return
        final_text = _answer_text(output)
        calls = [item for item in output if item.type == "function_call"]
        if not calls:
            break
        for call in calls:
            yield {"type": "tool", "label": tools.label(call.name), "round": round_no}
        items += _replay(output)
        items += _run_tools(calls, outcome)
    else:
        outcome.status, outcome.error = "failed", "too many lookups"
        yield {"type": "error", "message": "This question needed too many lookups. Try splitting it up."}
        return

    outcome.answer = final_text.strip()
    if not outcome.answer:  # e.g. only reasoning came back
        outcome.status, outcome.error = "failed", "empty answer"
        yield {"type": "error", "message": "The assistant did not write an answer. Try rephrasing it."}
        return
    yield {"type": "done", "answer": outcome.answer, "html": render(outcome.answer)}
