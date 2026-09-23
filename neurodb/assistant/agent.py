"""The AI assistant: Claude (Anthropic Messages API) answering questions with NeuroDB's read-only tools.

``answer()`` runs the tool-use loop and yields small event dicts as they happen, so the view can
stream them to the browser:

    {"type": "text", "text": "...", "round": n}   answer text as Claude writes it
    {"type": "tool", "label": "...", "round": n}   a data lookup is starting
    {"type": "done", "html": "...", "answer": "..."}
    {"type": "error", "message": "..."}

Requests use the beta Messages endpoint for the server-side ``fallbacks="default"`` option: if the
model's safety classifiers decline a (legitimate) question, the API retries it on Anthropic's
recommended fallback model instead of returning a refusal.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anthropic
import markdown
import nh3
from django.conf import settings
from django.utils import timezone
from markdown.extensions.tables import TableExtension

from . import tools

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
HISTORY_TURNS = 6  # earlier question/answer pairs sent back for follow-up questions

SYSTEM_PROMPT = """\
You are the NeuroDB assistant for UNICEF Lebanon. NeuroDB holds the country office's programme \
monitoring data: ActivityInfo databases per section and reporting year with master indicators, \
targets and activity reports from partners; Neuro and HPM reports; eTools programme documents, \
partners and donor funding; population figures; and a library of studies and maps.

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
- Before a lookup you may say one short sentence about what you are checking.

How to answer:
- Lead with the direct answer in the first sentence, then the supporting figures. Keep it brief.
- Use Markdown. Use a table when comparing more than three items. Format numbers with thousands \
separators and give percentages to one decimal place.
- Link to the NeuroDB page the figures come from, using the url values returned by the tools as \
Markdown links, e.g. [Child Protection dashboard](/databases/3/). Never invent URLs.
- Mention the data's date or freshness when it matters (e.g. "as of the last import on 13 Sep").
- Do not include internal or system XML tags in your response.
"""


class AssistantUnavailable(Exception):
    """The assistant is switched off or has no API key."""


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
        if not usage:
            return
        self.input_tokens += (usage.input_tokens or 0) + (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.output_tokens += usage.output_tokens or 0


def client() -> anthropic.Anthropic:
    if not settings.AI_ASSISTANT_ENABLED:
        raise AssistantUnavailable("The AI assistant is not configured.")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120.0, max_retries=2)


def _system() -> list[dict[str, Any]]:
    # The stable instructions (with the tool definitions before them) are cached; the date comes
    # after the cache breakpoint so it never invalidates the cached prefix.
    from neurodb.indicators.services.navigation import current_year

    today = timezone.localdate()
    year = current_year()
    context = (
        f"Today is {today:%A %d %B %Y}. The current reporting year is {year.name if year else 'unknown'}."
    )
    return [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": context},
    ]


def history_messages(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Earlier turns as plain text (tool calls are not replayed; follow-ups re-query as needed)."""
    messages: list[dict[str, Any]] = []
    for turn in history[-HISTORY_TURNS:]:
        if turn.get("question") and turn.get("answer"):
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": turn["answer"]})
    return messages


def render(text: str) -> str:
    """Markdown answer to safe HTML: no images, scripts or styles; links open in a new tab."""
    # align="..." instead of style="text-align: ..." so column alignment survives sanitizing
    html = markdown.markdown(text, extensions=[TableExtension(use_align_attribute=True), "sane_lists"])
    return nh3.clean(
        html,
        tags={
            "p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li", "h3", "h4", "h5",
            "table", "thead", "tbody", "tr", "th", "td", "a", "hr",
        },
        attributes={"a": {"href", "title"}, "th": {"align"}, "td": {"align"}},
        url_schemes={"https", "http", "mailto"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )  # fmt: skip


def _stream_round(api: anthropic.Anthropic, messages: list[dict[str, Any]], round_no: int):
    """One model call. Yields text events while streaming; returns the final message."""
    with api.beta.messages.stream(
        model=settings.AI_ASSISTANT_MODEL,
        max_tokens=MAX_TOKENS,
        system=_system(),
        tools=tools.definitions(),
        messages=messages,
        output_config={"effort": settings.AI_ASSISTANT_EFFORT},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        for event in stream:
            if event.type == "text":
                yield {"type": "text", "text": event.text, "round": round_no}
        return stream.get_final_message()


def _run_tools(blocks: list[Any], outcome: Outcome) -> list[dict[str, Any]]:
    results = []
    for block in blocks:
        started = time.monotonic()
        try:
            data = tools.run(block.name, block.input)
            content, is_error = json.dumps(data, ensure_ascii=False), False
        except tools.ToolInputError as exc:
            # Includes arguments that failed validation (possible with eager input streaming).
            content = json.dumps(
                {"error": str(exc), "received": block.input}, ensure_ascii=False, default=str
            )
            is_error = True
        except Exception:
            logger.exception("assistant tool %s failed", block.name)
            content, is_error = json.dumps({"error": "The lookup failed on the server."}), True
        outcome.tools.append(
            {
                "tool": block.name,
                "input": block.input if isinstance(block.input, dict) else str(block.input),
                "ok": not is_error,
                "ms": int((time.monotonic() - started) * 1000),
            }
        )
        results.append(
            {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
        )
    return results


_BEFORE_FALLBACK_DROP = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def _usable_content(content: list[Any]) -> list[Any]:
    """Content to send back and act on. After a mid-answer fallback to another model, blocks the
    declined model produced before the (last) fallback marker are not replayed or executed; text
    and everything after the marker are kept (API rule for echoing fallback turns)."""
    marks = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not marks:
        return list(content)
    last = marks[-1]
    return [b for i, b in enumerate(content) if i > last or b.type not in _BEFORE_FALLBACK_DROP]


def answer(question: str, history: list[dict[str, str]], outcome: Outcome) -> Iterator[dict[str, Any]]:
    """Run the conversation to a final answer, yielding progress events. Fills ``outcome``."""
    api = client()
    deadline = time.monotonic() + settings.AI_ASSISTANT_TIME_LIMIT_SECONDS
    messages = [*history_messages(history), {"role": "user", "content": question}]
    final_text = ""
    json_retries = 0
    round_no = 0
    while round_no < settings.AI_ASSISTANT_MAX_TOOL_ROUNDS:
        if time.monotonic() > deadline:
            outcome.status, outcome.error = "failed", "time limit"
            yield {"type": "error", "message": "This question took too long. Try asking something narrower."}
            return
        round_no += 1
        try:
            message = yield from _stream_round(api, messages, round_no)
        except ValueError:
            # Tool input the SDK could not parse at all (eager input streaming). There is no
            # tool_use id to answer, so re-issue the round (bounded).
            json_retries += 1
            if json_retries > 2:
                raise
            round_no -= 1
            continue
        json_retries = 0
        outcome.add_usage(message.usage)
        outcome.model = message.model
        content = _usable_content(message.content)
        final_text = "".join(b.text for b in content if b.type == "text")

        if message.stop_reason == "refusal":
            outcome.status = "refused"
            yield {
                "type": "error",
                "message": "The assistant could not answer this question. Try rephrasing it.",
            }
            return
        tool_uses = [b for b in content if b.type == "tool_use"]
        if not tool_uses:
            break
        if message.stop_reason == "max_tokens":
            # A cut-off tool call parses as a valid partial object; never run it.
            outcome.status, outcome.error = "failed", "max_tokens during tool call"
            yield {"type": "error", "message": "The answer was too long. Try a narrower question."}
            return
        for block in tool_uses:
            yield {"type": "tool", "label": tools.label(block.name), "round": round_no}
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": _run_tools(tool_uses, outcome)})
    else:
        outcome.status, outcome.error = "failed", "too many lookups"
        yield {"type": "error", "message": "This question needed too many lookups. Try splitting it up."}
        return

    outcome.answer = final_text.strip()
    yield {"type": "done", "answer": outcome.answer, "html": render(outcome.answer)}
