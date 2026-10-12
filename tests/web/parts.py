"""Test helpers for pages that load in parts (SPEC7 §3.1).

A converted page answers its shell: the frame with one placeholder (``[data-part]``) per data block. The
browser then asks for each block at the address the placeholder carries (``hx-get``). These helpers do the
same with the Django test client:

- :func:`placeholders`: ``{name: hx-get}`` of a shell's placeholders, in page order;
- :func:`get_part`: one part, asked for at the address the shell gives (so the test also checks the
  resolved query the shell wrote into it), with the HTMX headers the browser sends;
- :func:`assemble`: the shell with every part (strict ones too) put in place of its placeholder;
- :func:`eager`: the page with ``_eager=1``, every part drawn inline by the server;
- :func:`normalise`: HTML reduced to what both ways of drawing a page must agree on.

``tests/conftest.py`` uses :func:`assembling` so that ``client.get`` of a page answers the assembled page,
as a browser shows it, unless the test is marked ``@pytest.mark.shell``. :func:`raw_get` is the plain
``client.get``, without that.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from django.http import HttpResponse
from django.test import Client
from django.test.utils import ContextList

from neurodb.web.parts import part_address

HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_CURRENT_URL": "http://testserver/"}
SERVER = "http://testserver"

# a placeholder's opening tag (components/part.html); data-part-priority and the others never match
PLACEHOLDER = re.compile(r'<div\b[^>]*?\sdata-part="(?P<name>[a-z][a-z0-9-]*)"[^>]*>')


# ------------------------------------------------------------------------------------------- reading
class _StartTags(HTMLParser):
    """The attributes of every opening tag that carries ``attribute``, entities decoded."""

    def __init__(self, attribute: str):
        super().__init__(convert_charrefs=True)
        self.attribute = attribute
        self.found: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        values = {key: value or "" for key, value in attrs}
        if self.attribute in values:
            self.found.append(values)


def _start_tags(html: str, attribute: str) -> list[dict[str, str]]:
    parser = _StartTags(attribute)
    parser.feed(html)
    parser.close()
    return parser.found


def placeholder_attributes(html: str) -> dict[str, dict[str, str]]:
    """``{part name: the placeholder's attributes}``, in page order."""
    return {attrs["data-part"]: attrs for attrs in _start_tags(html, "data-part")}


def placeholders(html: str) -> dict[str, str]:
    """``{part name: hx-get}`` of every placeholder of ``html``, in page order."""
    return {name: attrs.get("hx-get", "") for name, attrs in placeholder_attributes(html).items()}


def text(response: HttpResponse) -> str:
    return response.content.decode(response.charset or "utf-8")


def is_shell(response: HttpResponse) -> bool:
    """An HTML answer with at least one placeholder still to fill (a compressed answer is left as it is)."""
    return (
        not response.streaming
        and response.status_code == 200
        and not response.has_header("Content-Encoding")
        and response.get("Content-Type", "").startswith("text/html")
        and PLACEHOLDER.search(text(response)) is not None
    )


# ------------------------------------------------------------------------------------------- fetching
def raw_get(client: Client, path: str, data: Any = None, **kwargs: Any) -> HttpResponse:
    """``client.get`` as Django defines it: the shell of a page in parts, never assembled."""
    get = getattr(Client.get, "__wrapped__", Client.get)
    return get(client, path, data, **kwargs)


def _passed_on(kwargs: dict[str, Any]) -> dict[str, Any]:
    """What a part request keeps of the page request's arguments: the headers (language, user agent) and
    ``secure``, without the HTMX ones the part request sets itself."""
    kept: dict[str, Any] = {
        key: value
        for key, value in kwargs.items()
        if key.startswith("HTTP_") and not key.startswith("HTTP_HX_")
    }
    if kwargs.get("secure"):
        kept["secure"] = True
    headers = {k: v for k, v in (kwargs.get("headers") or {}).items() if not k.lower().startswith("hx-")}
    if headers:
        kept["headers"] = headers
    return kept


def _ask_part(
    client: Client, address: str, element_id: str, page: str, kwargs: dict[str, Any]
) -> HttpResponse:
    """A part request as parts.js sends it: HTMX, from the page at ``page``, aimed at the placeholder. HTMX
    headers in ``kwargs`` (a test's own ``HTTP_HX_CURRENT_URL``, say) win."""
    headers = {
        "HTTP_HX_REQUEST": "true",
        "HTTP_HX_CURRENT_URL": SERVER + page,
        "HTTP_HX_TARGET": element_id,
        "HTTP_HX_TRIGGER": element_id,
    }
    chosen = {k: v for k, v in kwargs.items() if k.startswith("HTTP_HX_") and k != "HTTP_HX_REQUEST"}
    return raw_get(client, address, **{**_passed_on(kwargs), **headers, **chosen})


def _registered_address(shell: HttpResponse, name: str) -> str | None:
    """A part the shell drew inline (its figures were stored) or that lives inside another part: its
    address from what the page registered (``render_page``)."""
    request = getattr(shell, "wsgi_request", None)
    page = getattr(request, "_parts_page", None)
    if page is None or name not in page.parts:
        return None
    return part_address(request.path, page.query, name)


def get_part(client: Client, url: str, name: str, **extra: Any) -> HttpResponse:
    """Fetch the shell, take the part's address from it (so the resolved query is checked too), ask for the
    part with the HTMX headers, and check it answered 200 with its ``#part-<name>`` wrapper."""
    shell = raw_get(client, url, **_passed_on(extra))
    assert shell.status_code == 200, f"{url}: the shell answered {shell.status_code}"
    attrs = placeholder_attributes(text(shell)).get(name)
    address = attrs.get("hx-get") if attrs else _registered_address(shell, name)
    assert address, f"{url}: no part named {name!r} (placeholders: {list(placeholders(text(shell)))})"
    element_id = attrs.get("id", f"part-{name}") if attrs else f"part-{name}"
    response = _ask_part(client, address, element_id, shell.wsgi_request.get_full_path(), extra)
    assert response.status_code == 200, f"{address}: the part answered {response.status_code}"
    if "_part=" in address and not response.get("Content-Type", "").startswith("application/json"):
        assert f'id="part-{name}"' in text(response), f"{address}: no #part-{name} wrapper"
    return response


def eager(client: Client, url: str, **extra: Any) -> HttpResponse:
    """The page with every part drawn inline (``_eager=1``)."""
    return raw_get(client, url + ("&" if "?" in url else "?") + "_eager=1", **extra)


# ------------------------------------------------------------------------------------------- assembling
@dataclass
class Assembled:
    """A page put together as the browser does: ``html`` (shell plus parts), the ``shell`` response and
    each part's response by name (a part the shell drew inline has none: its figures were stored)."""

    html: str
    shell: HttpResponse
    parts: dict[str, HttpResponse] = field(default_factory=dict)

    def context(self, name: str) -> Any:
        """The template context of one part's response."""
        return self.parts[name].context


def _element_end(html: str, start: int, tag: str = "div") -> int:
    """Where the element whose opening tag starts at ``start`` ends (after its closing tag)."""
    depth = 0
    for match in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(html, start):
        depth += -1 if match.group().startswith("</") else 1
        if depth == 0:
            return match.end()
    raise AssertionError(f"an unclosed <{tag}> at {start}")


def _shown(response: HttpResponse) -> bool:
    """A part answer the browser swaps in: 200 with content, not a sign-in redirect."""
    return response.status_code == 200 and not response.has_header("HX-Redirect")


def _fill(
    client: Client, html: str, *, page: str, strict: bool, kwargs: dict[str, Any]
) -> tuple[str, dict[str, HttpResponse]]:
    """Ask for each placeholder's part and put the answer in its place, again for placeholders that
    arrive inside a part, until none is left. A part that does not answer 200 with content (a 403, a
    404, a 202 still computing, a sign-in redirect) keeps its placeholder. ``strict=False`` leaves the
    ``lazy="strict"`` parts out, as a browser that never scrolls to them."""
    answered: dict[str, HttpResponse] = {}
    left: set[str] = set()
    while True:
        match = next((m for m in PLACEHOLDER.finditer(html) if m["name"] not in left), None)
        if match is None:
            return html, answered
        name = match["name"]
        attrs = _start_tags(match.group(0) + "</div>", "data-part")[0]
        if not strict and attrs.get("data-part-lazy") == "strict":
            left.add(name)
            continue
        assert name not in answered, f"the part {name!r} came back with its own placeholder"
        element_id = attrs.get("id", f"part-{name}")
        response = _ask_part(client, attrs.get("hx-get", ""), element_id, page, kwargs)
        answered[name] = response
        if not _shown(response):
            left.add(name)
            continue
        body = text(response)
        end = _element_end(html, match.start())
        if attrs.get("hx-swap", "outerHTML").startswith("innerHTML"):
            # an existing endpoint answering its element's content: the placeholder stays as the element
            body = (
                f'<div id="{element_id}" class="part is-loaded" data-part-loaded="{name}" tabindex="-1">'
                f"{body}</div>"
            )
        html = html[: match.start()] + body + html[end:]


def assemble(client: Client, url: str, *, strict: bool = True, **extra: Any) -> Assembled:
    """The page at ``url`` as the browser shows it once every part is in (strict ones too by default)."""
    shell = raw_get(client, url, **extra)
    assert shell.status_code == 200, f"{url}: the shell answered {shell.status_code}"
    html, parts = _fill(
        client, text(shell), page=shell.wsgi_request.get_full_path(), strict=strict, kwargs=extra
    )
    return Assembled(html=html, shell=shell, parts=parts)


def _contexts(*responses: HttpResponse) -> ContextList:
    merged = ContextList()
    for response in responses:
        context = getattr(response, "context", None)
        if context is None:
            continue
        if isinstance(context, ContextList):
            merged.extend(context)
        else:
            merged.append(context)
    return merged


def _asks_htmx(headers: dict[str, Any] | None, extra: dict[str, Any]) -> bool:
    if extra.get("HTTP_HX_REQUEST") == "true":
        return True
    return any(key.lower() == "hx-request" and value == "true" for key, value in (headers or {}).items())


def assembling(get):
    """Wrap ``Client.get`` (tests/conftest.py): a page in parts, asked for without HTMX, answers as the
    browser shows it in the end. The shell response is returned with the assembled HTML as its content,
    the parts' template contexts after the shell's in ``response.context`` (and their templates in
    ``response.templates``), and the part responses by name in ``response.parts``."""

    @functools.wraps(get)
    def assembled_get(
        self, path, data=None, follow=False, secure=False, *, headers=None, query_params=None, **extra
    ):
        response = get(self, path, data, follow, secure, headers=headers, query_params=query_params, **extra)
        if _asks_htmx(headers, extra) or not is_shell(response):
            return response
        kwargs = {**extra, "secure": secure, "headers": headers}
        page = response.wsgi_request.get_full_path()
        html, parts = _fill(self, text(response), page=page, strict=True, kwargs=kwargs)
        response.content = html.encode(response.charset or "utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        shown = [part for part in parts.values() if _shown(part)]  # a 403 or 404 page adds nothing
        response.context = _contexts(response, *shown)
        response.templates = [*response.templates, *(t for part in shown for t in part.templates)]
        response.parts = parts
        return response

    return assembled_get


# ------------------------------------------------------------------------------------------- comparing
_DROPPED_ATTRIBUTES = re.compile(
    r"""\s+(?:hx-[\w:.-]+|aria-busy|data-part[\w-]*)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?"""
)
_CSRF_INPUT = re.compile(r'(name="csrfmiddlewaretoken"\s+value=")[^"]*"')
_CSRF_META = re.compile(r'(<meta\s+name="csrf-token"\s+content=")[^"]*"')
_NONCE = re.compile(r'(\snonce=")[^"]*"')
_NONCE_CONFIG = re.compile(r'("inlineScriptNonce":\s*")[^"]*"')
_SPACE = r"(?:\s|&nbsp;|\xa0)+"
_UNIT = r"(?:second|minute|hour|day|week|month|year)s?"
_AGO = re.compile(rf"\b\d+{_SPACE}{_UNIT}(?:,{_SPACE}\d+{_SPACE}{_UNIT})*{_SPACE}ago\b")


def _drop_element(html: str, element_id: str) -> str:
    pattern = re.compile(rf'<(?P<tag>[a-zA-Z][\w-]*)\b[^>]*\sid="{re.escape(element_id)}"[^>]*>')
    while match := pattern.search(html):
        html = html[: match.start()] + html[_element_end(html, match.start(), match["tag"]) :]
    return html


def normalise(html: str, *, exclude: tuple[str, ...] | list[str] = ()) -> str:
    """``html`` without what differs between two drawings of the same page: the ``hx-*``, ``aria-busy``
    and ``data-part*`` attributes, CSRF tokens, CSP nonces, "N minutes ago", and whitespace between and
    inside tags. ``exclude``: ids of elements to leave out altogether (the ``url=`` parts, which eager mode
    leaves to the browser)."""
    for element_id in exclude:
        html = _drop_element(html, element_id)
    html = _DROPPED_ATTRIBUTES.sub("", html)
    html = _CSRF_INPUT.sub(r'\1"', html)
    html = _CSRF_META.sub(r'\1"', html)
    html = _NONCE.sub(r'\1"', html)
    html = _NONCE_CONFIG.sub(r'\1"', html)
    html = _AGO.sub("… ago", html)
    html = re.sub(r">\s+<", "><", html)
    html = re.sub(r"\s+(/?>)", r"\1", html)  # the space a dropped attribute leaves before the end of a tag
    return re.sub(r"\s+", " ", html).strip()
