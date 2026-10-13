"""Pages that load in parts: a page answers its frame at once, and each data block comes in its own request.

A converted page's view resolves its filters (cheap: the year, the sections, the user's own section on a
first load) and hands :func:`render_page` three things: the shell's context (a callable, so it is only
computed when the shell is drawn), its named :class:`Part` blocks, and the **resolved** query, which carries
every key that decides the figures. The shell draws one placeholder per part (``{% part %}``, in
``web/templatetags/parts.py``); the browser (``static/js/parts.js``) then asks for each part at the page's own
address plus ``_part=<name>``, with ``HX-Request``, and swaps the answer in place of its placeholder.

Because a part is the same address and the same view, everything that guards the page guards its parts:
sign-in, the public pages, the donor lock-out, ``@require_GET`` and the view's own checks.

:class:`PartsMiddleware` (right after django-htmx's middleware):

1. takes ``_part`` and ``_eager`` out of ``request.GET`` and of the query string, so no link built from the
   request (``qs_replace``, ``get_full_path``, ``year_url``, ``next=``) ever carries them;
2. sets ``request.part`` to the part's name, only for an HTMX request that is not a history restore and
   whose name is valid (otherwise ``None``: a stray ``_part`` link draws the whole page), and
   ``request.parts_eager`` for ``_eager=1`` (the page with every part drawn inline: print, no JavaScript);
3. lets a part request wait at most about 2 s for figures another request is computing
   (``compute.part_wait``), then answers 202 with ``X-NeuroDB-Part-Pending: 1``: the browser asks again;
4. adds ``Vary: HX-Request`` to every answer (one address serves a page or a fragment),
   ``Cache-Control: no-store`` to every part request, and ``X-NeuroDB-Part: <name>`` only to an answer
   :func:`render_page` gave as that part (the part, or its 404, 202 or error). The browser swaps a 2xx
   answer in only with that header (parts.js ``answersPart()``): a view that draws no parts answers a
   ``_part`` request with its whole page, without the header, and that page is never nested in itself
   (a tab left open on a page in parts, while a release where the page has no parts answers it);
5. turns the sign-in redirect of an HTMX request (an expired session) into an ``HX-Redirect`` to the
   sign-in page, coming back to the page the person was on: the sign-in form is never swapped into a
   part. A person who is not signed in asking for a part of a public page (the site root shows the
   landing page to visitors) gets the same redirect, unless the view is marked :func:`public`.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import render, resolve_url
from django.template.loader import get_template, render_to_string
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django_htmx.http import HttpResponseClientRedirect

from neurodb.core import compute

try:  # Application Insights traces each request; a part's name goes on its span
    from opentelemetry import trace as _trace
except ImportError:  # pragma: no cover - installed with azure-monitor-opentelemetry
    _trace = None

PART_KEY = "_part"  # the query key that asks for one part of the page
EAGER_KEY = "_eager"  # the query key that asks for the page with every part drawn inline
PAGE_ONLY_KEYS = (PART_KEY, EAGER_KEY)
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")  # a part's name: it is also the end of an element id
LOADED_TEMPLATE = "components/part_loaded.html"  # the wrapper of a part's answer, around its own drawing


# ------------------------------------------------------------------------------------------- middleware
class PartsMiddleware:
    """See the module docstring. It reads ``request.htmx``, so it comes after django-htmx's middleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        _take_page_keys(request)
        if request.part is None:
            response = self.get_response(request)
        else:
            if _trace is not None:
                span = _trace.get_current_span()
                if span.is_recording():
                    span.set_attribute("neurodb.part", request.part)
            # compute.PART_WAIT is read here, not at import, so the tests can shorten it
            with compute.part_wait(compute.PART_WAIT):
                response = self.get_response(request)
        if _is_htmx(request) and _is_sign_in_redirect(response):
            response = sign_in_redirect(request)
        patch_vary_headers(response, ("HX-Request",))
        if request.part is not None:
            response["Cache-Control"] = "no-store"
            if getattr(request, "_parts_answered", None) == request.part:  # set by render_page
                response["X-NeuroDB-Part"] = request.part
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        """A part of a public page asked for by someone who is not signed in: the sign-in page, never the
        page's anonymous version (the landing page at the site root). Non-public views never get here:
        the sign-in middleware has already redirected, and ``__call__`` turns that into the same answer."""
        if getattr(request, "part", None) is None or getattr(view_func, "parts_public", False):
            return None
        user = getattr(request, "user", None)
        if user is None or user.is_authenticated or not _is_public_view(request, view_func):
            return None
        return sign_in_redirect(request)

    def process_exception(self, request, exception):
        if isinstance(exception, compute.Pending):
            return pending_response()
        return None


def _take_page_keys(request: HttpRequest) -> None:
    """Points 1-3 of the module docstring: strip ``_part`` and ``_eager``, set ``request.part`` and
    ``request.parts_eager``."""
    params = request.GET
    name = params.get(PART_KEY)
    eager = params.get(EAGER_KEY)
    if name is not None or eager is not None:
        kept = params.copy()
        for key in PAGE_ONLY_KEYS:
            kept.pop(key, None)
        encoded = kept.urlencode()
        request.GET = QueryDict(encoded)  # read-only, like the one Django builds
        request.META["QUERY_STRING"] = encoded  # get_full_path() reads META, not GET
    is_part = name is not None and _is_htmx(request) and not _is_history_restore(request)
    request.part = name if is_part and NAME.fullmatch(name) else None
    request.parts_eager = eager == "1"


def _is_htmx(request: HttpRequest) -> bool:
    details = getattr(request, "htmx", None)
    if details is not None:
        return bool(details)
    return request.headers.get("HX-Request") == "true"


def _is_history_restore(request: HttpRequest) -> bool:
    details = getattr(request, "htmx", None)
    if details is not None:
        return bool(details.history_restore_request)
    return request.headers.get("HX-History-Restore-Request") == "true"


def _sign_in_path() -> str:
    return urlsplit(resolve_url(settings.LOGIN_URL)).path


def _is_sign_in_redirect(response: HttpResponse) -> bool:
    if not 300 <= response.status_code < 400 or not response.has_header("Location"):
        return False
    return urlsplit(response["Location"]).path == _sign_in_path()


def _is_public_view(request: HttpRequest, view_func) -> bool:
    """Open to visitors: marked ``@login_not_required`` (the site root) or listed in ``PUBLIC_PAGES``. The
    Power BI feed, read with a key of its own and never with a session, is left to its own check."""
    if getattr(view_func, "powerbi_feed", False):
        return False
    if getattr(view_func, "login_required", True) is False:
        return True
    match = getattr(request, "resolver_match", None)
    return match is not None and match.view_name in settings.PUBLIC_PAGES


def _return_path(request: HttpRequest) -> str:
    """Where sign-in leads back to: the page the browser shows (``HX-Current-URL``), reduced to its path and
    query, when it is on this site; otherwise this request's own address. Never a part's address."""
    details = getattr(request, "htmx", None)
    current = details.current_url if details is not None else request.headers.get("HX-Current-URL")
    if current and url_has_allowed_host_and_scheme(
        current, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        split = urlsplit(current)
        query = changed_query(split.query)
        return (split.path or "/") + (f"?{query}" if query else "")
    return request.get_full_path()


def sign_in_redirect(request: HttpRequest) -> HttpResponseClientRedirect:
    """An ``HX-Redirect`` to the sign-in page, with ``next`` set to the page the person was on."""
    query = QueryDict(mutable=True)
    query[REDIRECT_FIELD_NAME] = _return_path(request)
    return HttpResponseClientRedirect(f"{resolve_url(settings.LOGIN_URL)}?{query.urlencode(safe='/')}")


def pending_response() -> HttpResponse:
    """The answer to a part whose figures another request is still computing: the browser asks again."""
    response = HttpResponse(status=202)
    response["X-NeuroDB-Part-Pending"] = "1"
    response["Cache-Control"] = "no-store"
    return response


def public(view):
    """Mark a public view whose parts visitors may load (no such view yet: without the mark, a visitor
    asking for a part of a public page is sent to sign in)."""
    view.parts_public = True
    return view


# --------------------------------------------------------------------------------------------- the view API
@dataclass(frozen=True)
class Part:
    """One named data block of a page.

    ``template`` is the part's body (``<app>/parts/<page>_<part>.html``); ``context()`` computes its figures
    and is only called when the part is drawn. ``json=True``: ``context()`` is JSON data and the part
    request answers it as JSON (``template`` is not used; pass ``""``).

    Inline when ready: ``ready()`` (or ``peek``, a :func:`neurodb.core.compute.once` key, looked up with
    ``compute.peek``; ``peek_lang_neutral=True`` for a value stored with ``once(..., lang_neutral=True)``)
    says the part's figures are already stored. The shell then draws the part inline, already loaded, so
    the page does not ask for it. Leave both out for a part that is always cheap to ask for, or never
    stored.
    """

    template: str
    context: Callable[[], dict[str, Any]]
    json: bool = False
    ready: Callable[[], bool] | None = None
    peek: str | None = None
    peek_lang_neutral: bool = False

    def is_ready(self) -> bool:
        if self.json:
            return False
        if self.ready is not None:
            return bool(self.ready())
        if self.peek is not None:
            return compute.peek(self.peek, lang_neutral=self.peek_lang_neutral)[0]
        return False


@dataclass(frozen=True)
class PartsPage:
    """What :func:`render_page` keeps on the request (``request._parts_page``) for the ``{% part %}`` tag,
    :func:`render_part` and the tests."""

    parts: dict[str, Part]
    scope: dict[str, Any] = field(default_factory=dict)
    query: str = ""


def first_load(request: HttpRequest) -> bool:
    """A first visit with no choice made yet: no query at all, and not a part request (whose query is the
    resolved one). The rule behind every "the user's own section by default"."""
    return not request.GET and getattr(request, "part", None) is None


def render_page(
    request: HttpRequest,
    template: str,
    *,
    scope: dict[str, Any],
    shell: Callable[[], dict[str, Any]],
    parts: dict[str, Part],
    query: str,
    results_template: str | None = None,
    results_target: str | None = None,
) -> HttpResponse:
    """The answer of a page in parts, in this order:

    1. a part request (``request.part``): that part alone (:func:`render_part`); an unknown name is a 404.
       ``shell()`` is not called: the part has ``scope`` and its own context. Only this answer (or its
       404, 202 or error) carries ``X-NeuroDB-Part``, the browser's proof that it is the part;
    2. an HTMX request whose target is the page's results area (``HX-Target`` equal to ``results_target``,
       with or without the ``#``): ``results_template``, the results area with its placeholders, no figures.
       Any other HTMX request of the page is the view's own business, before it calls this function;
    3. otherwise the shell: ``template`` with ``scope``, ``shell()``, ``parts``, ``parts_query`` (the
       resolved query), ``parts_scope`` and ``parts_eager`` (``_eager=1``: every part drawn inline).

    ``scope`` holds the few resolved values the part templates read (``selected``, ``year`` ...); ``query``
    is the resolved filter, URL-encoded, with every key that decides the figures (a first-load default
    written out: ``section=<own section>``), never the raw ``request.GET``.
    """
    for name in parts:
        if not NAME.fullmatch(name):
            raise ValueError(f"A part's name is lowercase letters, digits and dashes: {name!r}")
    if (results_template is None) != (results_target is None):
        raise TypeError("render_page: results_template and results_target go together")
    query = changed_query(query)  # never _part or _eager, whatever the view passed
    request._parts_page = PartsPage(parts=parts, scope=dict(scope), query=query)
    name = getattr(request, "part", None)
    if name is not None:
        request._parts_answered = name  # the middleware's X-NeuroDB-Part: this answer is that part
        return render_part(request, name, query)
    eager = bool(getattr(request, "parts_eager", False))
    if results_template is not None and not eager and _targets(request, results_target):
        template = results_template
    context = {
        **scope,
        **shell(),
        "parts": parts,
        "parts_scope": scope,
        "parts_query": query,
        "parts_eager": eager,
    }
    return render(request, template, context)


def _targets(request: HttpRequest, target: str | None) -> bool:
    details = getattr(request, "htmx", None)
    if not target or not details:
        return False
    return (details.target or "") == target.lstrip("#")


def render_part(request: HttpRequest, name: str, query: str) -> HttpResponse:
    """One part of the page :func:`render_page` registered on ``request``, as its own answer: a part request
    returns it, and the ``{% part %}`` tag draws it inline (eager mode, inline when ready).

    Both render with a copy of the request whose ``GET`` (and query string) is ``query``, the resolved
    filter, so the links a part builds from the request are the same either way. The part's context is
    computed once per request (``request._parts_ctx``). Errors are not caught: a failing part is a 500,
    logged and traced with its name, and the browser shows that part's error state.

    The part's own template is drawn first, with the request's context, and then put in its wrapper
    (:data:`LOADED_TEMPLATE`, drawn with nothing but the part's name and that drawing): drawn inside the
    wrapper (an ``{% include %}``), the same part took about twice the time (the brief's parts, 5 ms more
    each, at every warm visit).
    """
    page: PartsPage | None = getattr(request, "_parts_page", None)
    if page is None or name not in page.parts:
        raise Http404(f"This page has no part named {name!r}.")
    part = page.parts[name]
    contexts = getattr(request, "_parts_ctx", None)
    if contexts is None:
        contexts = request._parts_ctx = {}
    if name not in contexts:
        contexts[name] = part.context()
    data = contexts[name]
    if part.json:
        return JsonResponse(data, safe=False)
    view = _with_query(request, query, name)
    context = {**page.scope, **data, "parts_query": query, "part_name": name}
    body = render_to_string(part.template, context, view)
    response = HttpResponse(get_template(LOADED_TEMPLATE).render({"part_name": name, "part_body": body}))
    # a CSRF token first asked for while drawing the part: the response must still set its cookie
    for key, value in view.META.items():
        if key.startswith("CSRF_COOKIE"):
            request.META[key] = value
    return response


def _with_query(request: HttpRequest, query: str, name: str) -> HttpRequest:
    view = copy.copy(request)
    view.GET = QueryDict(query)
    view.META = {**request.META, "QUERY_STRING": query}
    view.part = name
    return view


# ------------------------------------------------------------------------------------------------ addresses
def changed_query(query: str, **updates: Any) -> str:
    """``query`` with some keys changed: ``None`` or ``""`` removes a key, a list repeats it (as
    ``qs_replace``). ``_part`` and ``_eager`` never stay in it."""
    if not updates and not any(f"{key}=" in query for key in PAGE_ONLY_KEYS):
        return query
    params = QueryDict(query, mutable=True)
    for key in PAGE_ONLY_KEYS:
        params.pop(key, None)
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        elif isinstance(value, list | tuple):
            params.setlist(key, [str(item) for item in value])
        else:
            params[key] = str(value)
    return params.urlencode()


def part_address(path: str, query: str, name: str, **updates: Any) -> str:
    """The address of a part request: the page's path, its resolved query (some keys changed) and
    ``_part``."""
    if not NAME.fullmatch(name):
        raise ValueError(f"A part's name is lowercase letters, digits and dashes: {name!r}")
    params = changed_query(query, **updates)
    return f"{path}?{params}&{PART_KEY}={name}" if params else f"{path}?{PART_KEY}={name}"


def page_address(path: str, query: str, **updates: Any) -> str:
    """The page's own clean address with its resolved query (some keys changed): what the address bar
    shows after a page change inside a part (``hx-push-url``)."""
    params = changed_query(query, **updates)
    return f"{path}?{params}" if params else path
