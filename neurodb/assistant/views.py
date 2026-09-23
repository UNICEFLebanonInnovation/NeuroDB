"""AI assistant pages: the chat page and the streaming answer endpoint (Server-Sent Events)."""

from __future__ import annotations

import datetime
import json
import logging
import time
import uuid
from collections.abc import Iterator

import anthropic
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import agent
from .models import AssistantQuestion

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
EXAMPLES = [
    "Which Child Protection indicators are off track this year?",
    "How many activity reports did each partner submit in Education, by governorate?",
    "What is the total budget of active programme documents funded by the European Union?",
    "How many Syrian children live in Akkar?",
    "Is the ActivityInfo data up to date?",
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
    context = {
        "page_title": _("Ask NeuroDB"),
        "page_subtitle": _("Questions in plain language, answered from NeuroDB's data by Claude"),
        "breadcrumbs": [{"label": _("Ask NeuroDB"), "url": None}],
        "enabled": settings.AI_ASSISTANT_ENABLED,
        "initial_question": request.GET.get("q", "")[:MAX_QUESTION_CHARS],
        "examples": EXAMPLES,
        "recent": recent_unique[:6],
        "hourly_limit": settings.AI_ASSISTANT_HOURLY_LIMIT,
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
    since = timezone.now() - datetime.timedelta(hours=1)
    asked = (
        AssistantQuestion.objects.filter(user=user, created_at__gte=since)
        .exclude(status=AssistantQuestion.Status.LIMITED)
        .count()
    )
    return asked >= settings.AI_ASSISTANT_HOURLY_LIMIT


API_ERRORS = (
    # most specific first
    (agent.AssistantUnavailable, "The AI assistant is not set up. Ask an administrator to add the API key."),
    (anthropic.AuthenticationError, "The AI assistant's API key was rejected. Ask an administrator."),
    (anthropic.PermissionDeniedError, "The AI assistant's API key is not allowed to use this model."),
    (anthropic.RateLimitError, "The AI service is busy right now. Please try again in a minute."),
    (anthropic.BadRequestError, "The AI service could not process this question."),
    (anthropic.APITimeoutError, "The AI service did not answer in time. Please try again."),
    (anthropic.APIConnectionError, "The AI service could not be reached. Please try again."),
    (anthropic.APIStatusError, "The AI service is temporarily unavailable. Please try again."),
)  # fmt: skip


def _stream(request: HttpRequest, question: str, conversation: uuid.UUID | None) -> Iterator[str]:
    outcome = agent.Outcome()
    started = time.monotonic()
    try:
        for event in agent.answer(question, _history(request.user, conversation), outcome):
            yield _sse(event)
    except Exception as exc:  # the stream has started: report errors as events, never as a 500
        message = next((m for cls, m in API_ERRORS if isinstance(exc, cls)), None)
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
        AssistantQuestion.objects.create(
            user=request.user,
            conversation=conversation,
            question=question,
            answer=outcome.answer,
            status=outcome.status,
            tools=outcome.tools,
            model=outcome.model or settings.AI_ASSISTANT_MODEL,
            input_tokens=outcome.input_tokens,
            cache_read_tokens=outcome.cache_read_tokens,
            output_tokens=outcome.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=outcome.error,
        )


@require_POST
def ask_stream(request: HttpRequest) -> HttpResponse:
    question = (request.POST.get("question") or "").strip()
    if not question:
        return JsonResponse({"error": _("Type a question first.")}, status=400)
    if len(question) > MAX_QUESTION_CHARS:
        return JsonResponse({"error": _("Please keep questions under 1,000 characters.")}, status=400)
    if not settings.AI_ASSISTANT_ENABLED:
        return JsonResponse({"error": _("The AI assistant is not set up yet.")}, status=503)
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
    try:
        conversation = uuid.UUID(request.POST.get("conversation", ""))
    except ValueError:
        conversation = None
    response = StreamingHttpResponse(
        _stream(request, question, conversation), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: tokens reach the browser as they arrive
    return response
