"""The help pages (``/help/``: the guide, page by page, with a search) and the Help assistant's streamed
answer (``/help/stream/``, the panel every page opens with Ctrl+Shift+H). Every signed-in person but a
donor reads them (donor accounts are kept on their own page by ``donors.middleware``)."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import assistant, guide
from .models import HelpQuestion

SEARCH_CHARS = 100


def _nav(active: str) -> list[dict[str, str | bool]]:
    return [
        {"key": p.key, "title": p.title, "url": p.url, "active": p.key == active}
        for p in guide.pages().values()
    ]


def _context(active: str, title: str) -> dict:
    return {
        "page_title": title,
        "page_subtitle": _("How NeuroDB works: its pages, figures, rules and AI features"),
        "breadcrumbs": [{"label": _("Help"), "url": None if active == "overview" else "/help/"}]
        + ([] if active == "overview" else [{"label": title, "url": None}]),
        "guide_pages": _nav(active),
        "active": active,
    }


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    """The guide's first page, or the sections matching ``?q=``."""
    query = " ".join(request.GET.get("q", "").split())[:SEARCH_CHARS]
    if not query:
        return page(request, "overview")
    found = guide.search(query, limit=12)
    context = _context("", _("Search the help"))
    context["breadcrumbs"] = [{"label": _("Help"), "url": "/help/"}, {"label": _("Search"), "url": None}]
    context.update(
        {
            "query": query,
            "results": [
                {
                    "page_title": s.page_title,
                    "heading": s.heading,
                    "url": s.url,
                    "snippet": guide.snippet(s, query),
                }
                for s in found
            ],
        }
    )
    return render(request, "help/search.html", context)


@require_GET
def page(request: HttpRequest, key: str) -> HttpResponse:
    found = guide.page(key)
    if found is None:
        raise Http404("No such help page")
    context = _context(key, found.title)
    context.update(
        {
            "html": guide.render(key),
            "contents": [s for s in found.sections if s.level == 2],
        }
    )
    return render(request, "help/page.html", context)


@require_GET
def panel(request: HttpRequest) -> HttpResponse:
    """The Help assistant panel's body, loaded the first time the panel opens: whether it can be used
    (and why not), the person's questions today against the daily quota, and the starter questions."""
    reason = assistant.blocked()
    used, allowed = assistant.quota(request.user)
    help_ = {
        "on": not reason,
        "reason": reason,
        "used": used,
        "allowed": allowed,
        "starters": assistant.STARTERS,
    }
    response = render(request, "help/_panel_body.html", {"help": help_})
    response["Cache-Control"] = "no-store"
    return response


def _refused(row: HelpQuestion, reason: str) -> StreamingHttpResponse:
    """NeuroDB's own answer to a question declined before any call, as the one event of a stream."""
    from neurodb.assistant.views import _sse

    event = assistant.refusal_event(reason, row.user)
    row.answer = event["answer"]
    row.save(update_fields=["answer"])
    response = StreamingHttpResponse(iter([_sse(event)]), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    return response


@require_POST
def stream(request: HttpRequest) -> HttpResponse:
    """A question to the Help assistant, answered as Server-Sent Events. Checked in order: the question
    (1-1,000 characters), the assistant switched on (503), a question asking for a secret or a way around
    access (declined at once, no quota used), the person's daily quota (429), their questions being
    answered (at most 2: 429), the pause and the day's shared AI budget (503)."""
    question = (request.POST.get("question") or "").replace("\x00", "").strip()
    if not question:
        return JsonResponse({"error": _("Type a question first.")}, status=400)
    if len(question) > assistant.QUESTION_CHARS:
        return JsonResponse({"error": _("Please keep questions under 1,000 characters.")}, status=400)
    if not assistant.switched_on():
        return JsonResponse({"error": assistant.OFF}, status=503)
    try:
        conversation = uuid.UUID(str(request.POST.get("conversation") or ""))
    except ValueError:
        conversation = uuid.uuid4()
    path = urlsplit(str(request.POST.get("page") or "")[:2000]).path[: assistant.PAGE_CHARS]
    title = assistant.clean(request.POST.get("title") or "", assistant.TITLE_CHARS)
    asked = assistant.clean(question, assistant.QUESTION_CHARS)
    fields = {"user": request.user, "conversation": conversation, "page": path, "page_title": title}

    reason = assistant.screen(question)
    if reason:
        row = HelpQuestion.objects.create(
            **fields,
            question=asked,
            status=HelpQuestion.Status.REFUSED,
            refused=True,
            refusal_reason=reason,
        )
        return _refused(row, reason)

    with transaction.atomic():
        # questions sent at the same time are checked one after the other; the row written here counts
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        used, allowed = assistant.quota(request.user)
        if used >= allowed:
            HelpQuestion.objects.create(
                **fields, question=asked, status=HelpQuestion.Status.LIMITED, error="quota"
            )
            message = _("You have asked %(n)s help questions today; the count starts again tomorrow.") % {
                "n": allowed
            }
            return JsonResponse({"error": message}, status=429)
        if assistant.running(request.user) >= assistant.MAX_RUNNING_PER_USER:
            return JsonResponse({"error": assistant.BUSY}, status=429)
        why = assistant.blocked()
        if why:
            if why == assistant.BUDGET:
                HelpQuestion.objects.create(
                    **fields, question=asked, status=HelpQuestion.Status.LIMITED, error="budget"
                )
            return JsonResponse({"error": why}, status=503)
        turns = assistant.history(request.user, conversation)
        row = HelpQuestion.objects.create(
            **fields, question=asked, status=HelpQuestion.Status.IN_PROGRESS, model=_model()
        )
    line = assistant.page_line(path, title)
    response = StreamingHttpResponse(
        assistant.stream(request, row, turns, line), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering: tokens reach the browser as they arrive
    return response


def _model() -> str:
    from django.conf import settings

    return settings.AI_ASSISTANT_MODEL
