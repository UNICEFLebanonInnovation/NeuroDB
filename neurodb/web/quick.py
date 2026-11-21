"""Quick view: a link in a page opens the page it leads to in a pop-up, instead of leaving the page.

The browser (``static/js/quickview.js``) asks for the page at its own address with ``X-NeuroDB-Quick: 1``
(a plain request, not an HTMX one). Every regular page answers it the same way, with no code of its own:
``base.html`` only extends the layout the context processor :func:`layout` names, the full page
(``layouts/page.html``) or the quick view (``layouts/quick.html``): the page's title in the shared window's
header with *Open full page* and Close, then its header line, its content and its parts, without the menu,
the top bar, the footer or the page's scripts. Its parts load inside the window as on the page (each at the
page's address with ``_part``, an HTMX request, never a quick view). The menu is not built for it
(``context_processors.navigation`` gives nothing).

:class:`QuickViewMiddleware` (after ``PartsMiddleware``, which sets ``request.part``):

1. sets ``request.quick_view`` for a quick request a quick view may answer (:func:`allowed`): a signed-in
   person who is not a donor, a GET that is neither an HTMX request nor a part, a page that is not in the
   administration, the sign-in pages or the donor page, and not marked :func:`not_quick`. A quick request
   for any other page is answered 204 with ``X-NeuroDB-Quick-View: refused`` before its view runs (a
   signed-out person is sent to sign in first, by the sign-in middleware); the browser then opens the
   address as a normal page;
2. adds ``X-NeuroDB-Quick-View: 1`` to the answer only when the quick layout was drawn
   (``{% quick_view_drawn %}``): a download, a JSON answer or a page drawn without ``base.html`` is never
   taken for a quick view;
3. adds ``Vary: X-NeuroDB-Quick`` to every answer: one address serves the page and its quick view.

The quick layout leaves out the page's ``scripts`` block: a page that needs a script of its own to work is
marked :func:`not_quick`, as is a page that is only there to be printed; links to it then open the page as
usual. A link is kept out of the quick view in the template with ``data-no-quick`` (on the link or on an
element around it).
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.cache import patch_vary_headers

ASK_HEADER = "X-NeuroDB-Quick"
ANSWER_HEADER = "X-NeuroDB-Quick-View"
FULL_LAYOUT = "layouts/page.html"
QUICK_LAYOUT = "layouts/quick.html"


def skip_prefixes() -> list[str]:
    """The addresses never shown in a quick view, by their beginning: the administration, the sign-in and
    password pages, the donor page, the Power BI feed and the internal API. The browser skips them too
    (``<meta name="nd-quick-skip">``, layouts/page.html)."""
    return [f"/{settings.ADMIN_URL_PATH}", "/accounts/", "/donor/", "/welcome/", "/powerbi/", "/api/"]


def not_quick(view):
    """Mark a view whose page is never shown in a quick view: links to it open the page as usual."""
    view.quick_view = False
    return view


def _is_htmx(request: HttpRequest) -> bool:
    details = getattr(request, "htmx", None)
    if details is not None:
        return bool(details)
    return request.headers.get("HX-Request") == "true"


def asked(request: HttpRequest) -> bool:
    """A quick request: the header, a GET (or HEAD), neither an HTMX request nor a part of a page."""
    return (
        request.headers.get(ASK_HEADER) == "1"
        and request.method in ("GET", "HEAD")
        and not _is_htmx(request)
        and getattr(request, "part", None) is None
    )


def allowed(request: HttpRequest, view_func) -> bool:
    """Whether the page of ``view_func`` may answer as a quick view for this person (module docstring, 1)."""
    from neurodb.donors.middleware import donor_account

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if any(request.path.startswith(prefix) for prefix in skip_prefixes()):
        return False
    match = getattr(request, "resolver_match", None)
    if match is None or match.namespace in ("admin", "donors"):
        return False
    if match.url_name and match.url_name.startswith("account_"):  # the sign-in and password pages (allauth)
        return False
    if getattr(view_func, "quick_view", True) is False or getattr(view_func, "powerbi_feed", False):
        return False
    return donor_account(request) is None


def refused() -> HttpResponse:
    response = HttpResponse(status=204)
    response[ANSWER_HEADER] = "refused"
    response["Cache-Control"] = "no-store"
    return response


class QuickViewMiddleware:
    """See the module docstring."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.quick_view = False
        response = self.get_response(request)
        patch_vary_headers(response, (ASK_HEADER,))
        if request.quick_view and getattr(request, "_quick_view_drawn", False):
            response[ANSWER_HEADER] = "1"
            response["Cache-Control"] = "no-store"
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not asked(request):
            return None
        if not allowed(request, view_func):
            return refused()
        request.quick_view = True
        return None


def layout(request: HttpRequest) -> dict:
    """The context processor: the layout ``base.html`` extends (``page_layout``), and ``in_quick_view``."""
    quick = bool(getattr(request, "quick_view", False))
    return {"page_layout": QUICK_LAYOUT if quick else FULL_LAYOUT, "in_quick_view": quick}
