"""``{% load parts %}``: the placeholders of a page that loads in parts, and the addresses of its parts.

``{% part "impact" kind="charts" group="overview" height="760px" labelledby="impact-title" %}`` draws the
placeholder the browser fills (``components/part.html``), or, in eager mode (``_eager=1``) and when the part's
figures are already stored (``Part.ready`` / ``Part.peek``), the part itself, already loaded
(``components/part_loaded.html``, through :func:`neurodb.web.parts.render_part`).

``{% part_url "grid" page=3 %}`` is the address of a part request with one key changed (``hx-get``),
``{% page_url page=3 %}`` the page's own clean address with that change (``hx-push-url``). Both start from
the page's resolved query (``parts_query``), never from the address bar.
"""

from __future__ import annotations

import re
from typing import Any

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from neurodb.web import parts as page_parts

register = template.Library()

KINDS = ("tiles", "chart", "charts", "table", "list", "card", "map", "text")
PRIORITIES = ("high", "normal", "low")
LAZY = ("", "strict")
SWAPS = ("outerHTML", "innerHTML")
LIBRARIES = ("plotly", "maplibre")  # what parts.js loads ahead, while the server computes
NEEDS_BY_KIND = {"chart": "plotly", "charts": "plotly", "map": "maplibre"}
HEIGHT = re.compile(r"\d+(\.\d+)?(px|rem|vh)")  # anything else could carry CSS into the style attribute
ELEMENT_ID = re.compile(r"[A-Za-z][\w-]*")
MAX_ROWS = 20
SKELETON_LINES = {"tiles": 1, "table": 6, "list": 4, "card": 3, "text": 2}  # rows of tiles, or of lines
PLACEHOLDER_TEMPLATE = "components/part.html"


def _request(context: template.Context):
    request = context.get("request")
    if request is None:
        raise template.TemplateSyntaxError("{% load parts %} tags need the request in the context")
    return request


def _page(request) -> page_parts.PartsPage | None:
    return getattr(request, "_parts_page", None)


def _query(context: template.Context, request) -> str:
    """The page's resolved query: ``parts_query`` from :func:`render_page` (shell and parts alike), else
    what the page registered, else the request's own query."""
    query = context.get("parts_query")
    if query is not None:
        return str(query)
    page = _page(request)
    if page is not None:
        return page.query
    return request.GET.urlencode()


def _choice(argument: str, value: Any, allowed: tuple[str, ...]) -> str:
    text = "" if value is None else str(value)
    if text not in allowed:
        raise template.TemplateSyntaxError(f"{{% part %}} {argument}={text!r}: one of {', '.join(allowed)}")
    return text


def _rows(value: Any) -> int | None:
    try:
        rows = int(value)
    except (TypeError, ValueError):
        return None
    return rows if 1 <= rows <= MAX_ROWS else None


@register.simple_tag(takes_context=True)
def part(
    context: template.Context,
    name: str,
    *,
    kind: str = "card",
    priority: str = "normal",
    group: str = "",
    lazy: str = "",
    height: str = "",
    height_sm: str = "",
    rows: Any = None,
    url: str = "",
    element_id: str = "",
    anchors: str = "",
    labelledby: str = "",
    needs: str | None = None,
    swap: str = "outerHTML",
) -> SafeString:
    """A part of the page: its placeholder, or the part itself when the page is eager or its figures are
    ready. See SPEC7 §1.3 for the arguments; ``swap="innerHTML"`` is for a ``url=`` endpoint that answers
    the inner content of its element (the placeholder then keeps its id); ``height_sm`` is the height kept
    on phones, where blocks side by side stack (``--part-h-sm``, read by app.css), when it differs."""
    request = _request(context)
    name = str(name)
    if not page_parts.NAME.fullmatch(name):
        raise template.TemplateSyntaxError(
            f"{{% part %}} name {name!r}: lowercase letters, digits and dashes"
        )
    kind = _choice("kind", kind, KINDS)
    priority = _choice("priority", priority, PRIORITIES)
    lazy = _choice("lazy", lazy, LAZY)
    swap = _choice("swap", swap, SWAPS)
    if needs is None:
        needs = NEEDS_BY_KIND.get(kind, "")
    for library in str(needs).split():
        _choice("needs", library, LIBRARIES)
    if element_id and not ELEMENT_ID.fullmatch(str(element_id)):
        raise template.TemplateSyntaxError(f"{{% part %}} element_id={element_id!r} is not an element id")

    page = _page(request)
    query = _query(context, request)
    known = page.parts.get(name) if page is not None else None
    if known is None and not url:
        raise template.TemplateSyntaxError(
            f"{{% part %}} {name!r}: the view passes no part of that name to render_page (and no url=)"
        )
    if known is not None and not known.json:
        eager = bool(context.get("parts_eager") or getattr(request, "parts_eager", False))
        if eager or known.is_ready():
            response = page_parts.render_part(request, name, query)
            return mark_safe(response.content.decode(response.charset))

    rows = _rows(rows)
    style = []
    height = str(height or "").strip()
    if HEIGHT.fullmatch(height):
        style.append(f"--part-h: {height}")
    height_sm = str(height_sm or "").strip()
    if HEIGHT.fullmatch(height_sm):
        style.append(f"--part-h-sm: {height_sm}")
    if rows is not None:
        style.append(f"--part-rows: {rows}")
    html = render_to_string(
        PLACEHOLDER_TEMPLATE,
        {
            "name": name,
            "kind": kind,
            "priority": priority,
            "group": group,
            "lazy": lazy,
            "anchors": anchors,
            "labelledby": labelledby,
            "needs": " ".join(str(needs).split()),
            "swap": swap,
            "rows": rows,
            "lines": range(rows or SKELETON_LINES.get(kind, 1)),
            "style": "; ".join(style),
            "element_id": element_id or f"part-{name}",
            "url": url or page_parts.part_address(request.path, query, name),
            "eager_url": "?" + (f"{query}&" if query else "") + f"{page_parts.EAGER_KEY}=1",
        },
    )
    return mark_safe(html)


@register.simple_tag(takes_context=True)
def part_url(context: template.Context, name: str, **updates: Any) -> str:
    """The address of a part request of this page, from its resolved query with ``updates`` (``None`` or
    ``""`` removes a key): ``hx-get="{% part_url "grid" page=3 %}"``."""
    request = _request(context)
    return page_parts.part_address(request.path, _query(context, request), str(name), **updates)


@register.simple_tag(takes_context=True)
def page_url(context: template.Context, **updates: Any) -> str:
    """This page's clean address, from its resolved query with ``updates``: ``hx-push-url`` of a change
    made inside a part (never the part's own address)."""
    request = _request(context)
    return page_parts.page_address(request.path, _query(context, request), **updates)
