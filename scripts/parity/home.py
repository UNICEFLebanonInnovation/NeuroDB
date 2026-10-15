"""SPEC_HOME §11.3: Home (``/``) and the daily review's page (``/daily-review/``), drawn at once against put
together from their parts.

Home is a new page: there is no base page to compare it with. So this group records each page **drawn at once**
(``_eager=1``: every part inline, in one response) and checks it **assembled from its parts** (the shell, then each
part request as parts.js sends it), cold and warm, on the same tree and the same restored world::

    python -m scripts.parity.core record home home.json --checkout .
    python -m scripts.parity.core check home home.json

**Users.** An administrator (the first active superuser), a viewer with a section (the first active Viewer with a
section) and, when the world has one, a Management member (the first active member of the Management group): the
first load shows the viewer's section, the country to the others.

**Matrix.** Home: first load, every section, the viewer's section, another section, two sections, a section with no
data. The daily review: first load, every section, the day before, the last seven days, a date with a review and a
date without one.

**What is compared.** The figures each block was drawn from (the key figures' build, the decisions, the review's
digest, what changed, "Your tasks", the sources' lines, the review card), the visible text and links of each block,
the JSON payloads and the element ids. The For you card is left out on both sides: it comes from another view,
which the page drawn at once leaves to the browser (a quiet slot). So are the ids of the parts' wrappers, which a
part drawn at once keeps and a part put in place of its placeholder does not.
"""

from __future__ import annotations

import re
from typing import Any

from scripts.parity import core

JSON_IDS: dict[str, str] = {}
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*")
FOR_YOU = re.compile(r'<div id="part-for-you"')
CARD = re.compile(r'<section class="panel watch-overview"')  # the card put in place of its slot
PART_ID = re.compile(r' id="part-[a-z][a-z0-9-]*"')  # a part's wrapper: drawn at once it keeps its id
NO_SUCH_SECTION = "No such section"
# timing.py times these rows of matrix() (its labels, exactly)
TIMED = ("home first load", "home every section", "home one section", "review first load")
FIRST_FIGURE = "standing"  # the part whose arrival shows Home's first figure
CONTEXT_KEYS = (
    "data",  # standing: the overview build (the key figures and the "Also" line)
    "decisions",
    "digest",
    "changes",
    "your_tasks",
    "rows",  # programmes
    "review",  # the daily review's card
)


def first_figure(url: str) -> str | None:
    """The part whose arrival is ``url``'s first figure (timing.py): Home's key figures, the review's card."""
    return "review" if url.startswith("/daily-review/") else FIRST_FIGURE


def users() -> dict[str, Any]:
    from neurodb.accounts.models import User
    from neurodb.accounts.roles import MANAGEMENT, VIEWER

    admin = User.objects.filter(is_superuser=True, is_active=True).order_by("username").first()
    viewer = (
        User.objects.filter(groups__name=VIEWER, is_active=True, is_superuser=False, section__isnull=False)
        .order_by("username")
        .first()
    )
    if admin is None or viewer is None:
        raise SystemExit("the world needs an active superuser and an active viewer with a section")
    people = {"admin": admin, "viewer": viewer}
    member = (
        User.objects.filter(groups__name=MANAGEMENT, is_active=True)
        .exclude(pk__in=[admin.pk, viewer.pk])
        .order_by("username")
        .first()
    )
    if member is not None:
        people["management"] = member
    return people


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The pages, as (label, URL). Each is recorded drawn at once (``_eager=1``, :func:`prepare`)."""
    from urllib.parse import urlencode

    from neurodb.core import years
    from neurodb.reports import overview as service
    from neurodb.review.models import DailyReview

    year = years.this_year()
    options = service.options(year)
    own = people["viewer"].section.name
    sections = options["sections"]
    mine = next((s for s in sections if s.casefold() == own.casefold()), own)
    other = next(s for s in sections if s != mine)
    reviews = list(
        DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED)
        .order_by("-date")
        .values_list("date", flat=True)
    )
    no_review = f"{year}-01-01"

    def url(path: str, *pairs: tuple[str, str]) -> str:
        return f"{path}?{urlencode(pairs)}" if pairs else path

    rows = [
        ("home first load", "/"),
        ("home every section", url("/", ("section", ""))),
        ("home one section", url("/", ("section", mine))),
        ("home another section", url("/", ("section", other))),
        ("home two sections", url("/", ("section", other), ("section", mine))),
        ("home no result", url("/", ("section", NO_SUCH_SECTION))),
        ("review first load", "/daily-review/"),
        ("review every section", url("/daily-review/", ("section", ""))),
        ("review yesterday", url("/daily-review/", ("section", ""), ("review", "yesterday"))),
        ("review week, one section", url("/daily-review/", ("section", mine), ("review", "week"))),
    ]
    if len(reviews) > 1:
        rows.append(
            ("review on a date", url("/daily-review/", ("section", ""), ("review", reviews[1].isoformat())))
        )
    rows.append(
        ("review on a date without one", url("/daily-review/", ("section", ""), ("review", no_review)))
    )
    return rows


def _eager(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "_eager=1"


def _without_for_you(html: str) -> str:
    """The page without the For you card: its quiet slot (drawn at once) or the card itself (put together)."""
    for pattern, tag in ((FOR_YOU, "div"), (CARD, "section")):
        match = pattern.search(html)
        if match is not None:
            html = html[: match.start()] + html[core.element_end(html, match.start(), tag) :]
    return html


def prepare(page: core.Page, mode: str) -> None:
    """Recording: the page drawn at once, in place of the page put together (the same request with
    ``_eager=1``, by the page's signed-in client). Both sides: without the For you card."""
    if mode == "record" and page.shell.status_code == 200:
        core.refreeze()
        drawn = page.shell.client.get(_eager(page.url))
        page.eager = drawn
        page.html = drawn.content.decode(drawn.charset or "utf-8")
    page.html = PART_ID.sub("", _without_for_you(page.html))


def _contexts(*responses: Any) -> list[Any]:
    out = []
    for response in responses:
        context = getattr(response, "context", None)
        if context is None:
            continue
        # a ContextList (one context per template drawn) is a list with keys(): each of its contexts, in order
        out.extend(list(context) if isinstance(context, list) else [context])
    return out


def _value(contexts: list[Any], key: str) -> Any:
    """The last value set for ``key`` (another template's empty value under the same name does not count)."""
    found = []
    for context in contexts:
        try:
            if key in context and context[key] is not None:
                found.append(context[key])
        except (KeyError, TypeError):
            continue
    return found[-1] if found else None


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The values the blocks were drawn from: in the page drawn at once (recording), its one response; in the
    page put together (check), the shell and each part (a part the shell drew inline is in the shell's)."""
    if mode == "record":
        contexts = _contexts(getattr(page, "eager", page.shell))
    else:
        contexts = _contexts(page.shell, *[r for name, r in page.parts.items() if name != "for-you"])
    return {key: core.plain(_value(contexts, key)) for key in CONTEXT_KEYS}, []
