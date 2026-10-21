"""AI assistant pages: the chat page and the streaming answer endpoint (Server-Sent Events)."""

from __future__ import annotations

import datetime
import json
import logging
import math
import time
import uuid
from collections.abc import Iterator

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from neurodb.knowledge.access import can_add as can_add_knowledge

from . import agent, llm, usage
from .llm import openai_backend
from .models import AssistantQuestion

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_RUNNING = 2  # questions one user may have being answered at the same time
IN_PROGRESS = "in progress"  # the error of a question's row until its answer is finished
EXAMPLES = [
    "Which Child Protection programme documents are off track this year?",
    "Which partners have progress reports overdue?",
    "What is the total budget of active programme documents funded by the European Union?",
    "How many Syrian children live in Akkar?",
    "Is the eTools data up to date?",
]


@require_GET
def ask(request: HttpRequest) -> HttpResponse:
    recent = (
        AssistantQuestion.objects.filter(user=request.user, status=AssistantQuestion.Status.ANSWERED)
        .order_by("-created_at")
        .values_list("question", flat=True)[:20]
    )
    seen, recent_unique = set(), []
    for q in recent:
        if q not in seen:
            seen.add(q)
            recent_unique.append(q)
    enabled = llm.enabled()
    context = {
        "page_title": _("Ask NeuroDB"),
        "page_subtitle": _("Questions in plain language, answered from NeuroDB's data by %(ai)s")
        % {"ai": llm.names().label},
        "breadcrumbs": [{"label": _("Ask NeuroDB"), "url": None}],
        "enabled": enabled,
        # for staff only: where the key goes (it names both providers, so it is written here, not in the
        # template, whose words name none)
        "setup_hint": _(
            "For administrators: enter an Anthropic (Claude) or OpenAI API key in the admin (AI settings), "
            "or ask the hosting team to set ANTHROPIC_API_KEY or OPENAI_API_KEY."
        )
        if request.user.is_staff and not enabled
        else "",
        "initial_question": request.GET.get("q", "")[:MAX_QUESTION_CHARS],
        "examples": EXAMPLES,
        "recent": recent_unique[:6],
        "hourly_limit": settings.AI_ASSISTANT_HOURLY_LIMIT,
        "can_add_knowledge": can_add_knowledge(request.user),
    }
    return render(request, "assistant/ask.html", context)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _history(user, conversation: uuid.UUID | None) -> list[dict[str, str]]:
    if not conversation:
        return []
    rows = (
        AssistantQuestion.objects.filter(
            user=user, conversation=conversation, status=AssistantQuestion.Status.ANSWERED
        )
        .order_by("-created_at")
        .values("question", "answer")[: agent.HISTORY_TURNS]
    )
    return list(reversed(rows))


def _over_limit(user) -> bool:
    """The hourly limit is reached. Questions still being answered count too (their row is written
    when they start), so questions sent at the same time cannot all pass."""
    since = timezone.now() - datetime.timedelta(hours=1)
    asked = (
        AssistantQuestion.objects.filter(user=user, created_at__gte=since)
        .exclude(status=AssistantQuestion.Status.LIMITED)
        .count()
    )
    return asked >= settings.AI_ASSISTANT_HOURLY_LIMIT


def _running(user) -> int:
    """Questions of this user being answered now. Each holds a server thread for up to the time
    limit; a row left "in progress" by a stopped server stops counting once that has long passed."""
    since = timezone.now() - datetime.timedelta(seconds=settings.AI_ASSISTANT_TIME_LIMIT_SECONDS + 120)
    return AssistantQuestion.objects.filter(
        user=user, status=AssistantQuestion.Status.FAILED, error=IN_PROGRESS, created_at__gte=since
    ).count()


def _storable(value):
    """A value Postgres accepts in a text or JSON column: no NUL characters, and no NaN or Infinity,
    which JSON cannot hold (the model's text and function arguments may contain either)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_storable(k): _storable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_storable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


# The messages for a failure of either provider (llm.friendly: the message shown for a known failure, or
# None for an unexpected one). The names stay: Monitoring insights' chat and the Help assistant import them.
BUSY = llm.BUSY
NO_CREDIT = llm.NO_CREDIT
API_ERRORS = openai_backend.ERRORS
STREAM_ERRORS = openai_backend.STREAM_ERRORS
_friendly = llm.friendly


def _stream(request: HttpRequest, row: AssistantQuestion) -> Iterator[str]:
    """Answer the question of ``row`` (written when it was asked) and complete the row at the end."""
    outcome = agent.Outcome()
    started = time.monotonic()
    try:
        history = _history(request.user, row.conversation)
        for event in agent.answer(row.question, history, outcome, user=request.user):
            yield _sse(event)
    except Exception as exc:  # the stream has started: report errors as events, never as a 500
        message = _friendly(exc)
        if message is None:
            logger.exception("AI assistant failed")
            message = "Something went wrong while answering. Please try again."
        else:
            logger.warning("AI assistant: %s: %s", type(exc).__name__, exc)
        outcome.status, outcome.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        yield _sse({"type": "error", "message": message})
    finally:
        if outcome.status == AssistantQuestion.Status.ANSWERED and not outcome.answer:
            # The browser went away (Stop button, closed tab) before the answer was complete.
            outcome.status, outcome.error = AssistantQuestion.Status.FAILED, outcome.error or "stopped"
        row.answer = _storable(outcome.answer)
        row.status = outcome.status
        row.tools = _storable(outcome.tools)
        row.model = outcome.model or llm.model_for("")
        row.input_tokens = outcome.input_tokens
        row.cache_read_tokens = outcome.cache_read_tokens
        row.output_tokens = outcome.output_tokens
        row.duration_ms = int((time.monotonic() - started) * 1000)
        row.error = _storable(outcome.error)
        try:
            row.save()
        except Exception:  # the answer has been sent: a failed log write must not break the stream
            logger.exception("AI assistant: could not log question %s", row.pk)
        # the answer's model calls in the day's AI use of the shared key (never raises)
        llm.record(usage.ASK, llm.model_for(""), outcome)


@require_POST
def ask_stream(request: HttpRequest) -> HttpResponse:
    question = (request.POST.get("question") or "").replace("\x00", "").strip()  # NUL: not storable
    if not question:
        return JsonResponse({"error": _("Type a question first.")}, status=400)
    if len(question) > MAX_QUESTION_CHARS:
        return JsonResponse({"error": _("Please keep questions under 1,000 characters.")}, status=400)
    if not llm.enabled():
        return JsonResponse({"error": _("The AI assistant is not set up yet.")}, status=503)
    try:
        conversation = uuid.UUID(request.POST.get("conversation", ""))
    except ValueError:
        conversation = None
    with transaction.atomic():
        # Lock the user so that questions sent at the same time are checked one after the other,
        # and write the question's row now: it counts towards the limits while it is answered.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        if _over_limit(request.user):
            AssistantQuestion.objects.create(
                user=request.user, question=question, status=AssistantQuestion.Status.LIMITED
            )
            return JsonResponse(
                {
                    "error": _("You have asked %(n)s questions in the last hour. Please try again later.")
                    % {"n": settings.AI_ASSISTANT_HOURLY_LIMIT}
                },
                status=429,
            )
        if _running(request.user) >= MAX_RUNNING:
            return JsonResponse(
                {"error": _("Your other questions are still being answered. Please wait for them.")},
                status=429,
            )
        row = AssistantQuestion.objects.create(
            user=request.user,
            conversation=conversation,
            question=question,
            status=AssistantQuestion.Status.FAILED,  # not ANSWERED: kept out of history and recent
            error=IN_PROGRESS,
            model=llm.model_for(""),  # the model of the provider in use
        )
    response = StreamingHttpResponse(_stream(request, row), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: tokens reach the browser as they arrive
    return response
