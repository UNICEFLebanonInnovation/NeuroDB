"""AI assistant pages: the chat page and the streaming answer endpoint (Server-Sent Events)."""

from __future__ import annotations

import datetime
import json
import logging
import math
import time
import uuid
from collections.abc import Iterator

import openai
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import agent
from .models import AssistantQuestion

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_RUNNING = 2  # questions one user may have being answered at the same time
IN_PROGRESS = "in progress"  # the error of a question's row until its answer is finished
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
        "page_subtitle": _("Questions in plain language, answered from NeuroDB's data by ChatGPT (OpenAI)"),
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


BUSY = "The AI service is busy right now. Please try again in a minute."
NO_CREDIT = "The AI service's credit for this application has run out. Ask an administrator."
API_ERRORS = (
    # most specific first (APITimeoutError is an APIConnectionError; status errors are APIErrors)
    (agent.AssistantUnavailable, "The AI assistant is not set up. Ask an administrator to add the API key."),
    (openai.AuthenticationError, "The AI assistant's API key was rejected. Ask an administrator."),
    (openai.PermissionDeniedError, "The AI assistant's API key is not allowed to use this model."),
    (openai.NotFoundError, "The AI assistant's model is not available. Ask an administrator."),
    (openai.RateLimitError, BUSY),
    (openai.BadRequestError, "The AI service could not process this question."),
    (openai.APITimeoutError, "The AI service did not answer in time. Please try again."),
    (openai.APIConnectionError, "The AI service could not be reached. Please try again."),
    (openai.APIStatusError, "The AI service is temporarily unavailable. Please try again."),
    (agent.ServiceError, "The AI service could not finish this answer. Please try again."),
    (openai.APIError, "The AI service is temporarily unavailable. Please try again."),  # error mid-stream
)  # fmt: skip


# The same conditions as the HTTP errors above, when they end a response inside the stream.
STREAM_ERRORS = {
    "insufficient_quota": NO_CREDIT,
    "rate_limit_exceeded": BUSY,
    "slow_down": BUSY,
    "server_is_overloaded": BUSY,
}


def _friendly(exc: Exception) -> str | None:
    """The message shown for a known failure, or None for an unexpected one."""
    if isinstance(exc, openai.RateLimitError) and exc.code == "insufficient_quota":
        return NO_CREDIT  # a 429 that retrying will not fix: the account needs credit
    if isinstance(exc, agent.ServiceError) and exc.code in STREAM_ERRORS:
        return STREAM_ERRORS[exc.code]
    return next((m for cls, m in API_ERRORS if isinstance(exc, cls)), None)


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
        row.model = outcome.model or settings.AI_ASSISTANT_MODEL
        row.input_tokens = outcome.input_tokens
        row.cache_read_tokens = outcome.cache_read_tokens
        row.output_tokens = outcome.output_tokens
        row.duration_ms = int((time.monotonic() - started) * 1000)
        row.error = _storable(outcome.error)
        try:
            row.save()
        except Exception:  # the answer has been sent: a failed log write must not break the stream
            logger.exception("AI assistant: could not log question %s", row.pk)


@require_POST
def ask_stream(request: HttpRequest) -> HttpResponse:
    question = (request.POST.get("question") or "").replace("\x00", "").strip()  # NUL: not storable
    if not question:
        return JsonResponse({"error": _("Type a question first.")}, status=400)
    if len(question) > MAX_QUESTION_CHARS:
        return JsonResponse({"error": _("Please keep questions under 1,000 characters.")}, status=400)
    if not settings.AI_ASSISTANT_ENABLED:
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
            model=settings.AI_ASSISTANT_MODEL,
        )
    response = StreamingHttpResponse(_stream(request, row), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: tokens reach the browser as they arrive
    return response
