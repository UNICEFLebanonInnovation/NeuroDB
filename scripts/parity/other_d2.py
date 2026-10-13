"""Group D2 of step 3: the knowledge search and the document review (SPEC7 §3.5, STAGE3 group D2).

**Pages.** The triage of step 3 (the x20 world given the knowledge data of ``seed_world.py``, on the base
commit, cold, as an administrator and as a viewer with a section) found over 300 ms: the knowledge search
with common words (1.6-1.8 s), and the document review's documents tab (0.5-0.65 s), findings by document
(0.5-0.75 s), index (0.55-0.6 s), synthesis themes (0.5-0.55 s) and repeated findings (5.2-5.7 s). Measured
again after the quick wins of STAGE3 (``s3/d2/qw_measure.log``), the search (40-140 ms), the documents tab
(130-200 ms) and the index (95-125 ms) are under 300 ms and stay one request; findings by document
(450-850 ms: its template), the themes (360-470 ms) and the repeated findings (5.2-5.7 s) load in parts:
``findings`` and ``synthesis``. Every row of the matrix is checked all the same: the pages that stayed one
request share the frame, the results area and the quick wins of the converted views.

What's new (60-95 ms) and For you (50-200 ms) stay under 300 ms on the x20 world and keep their code, so
they are not in this group.

**Users.** The overview group's administrator (the first active superuser) and viewer with a section, and a
third, ``parity-verified`` (a Viewer of the viewer's section, which ``seed_world.py`` adds): its sessions
have the review's **Verified only** switch on (``knowledge.review_views.VERIFIED_KEY``; a person's switch
lives in the session, not in the address). The review counts only accepted findings for it, so every
stored review build must hold the switch in its key: in the warm pass of ``check`` the cache still holds
the builds the two other users stored for the same addresses, and a key without the switch would show
them. This module wraps ``core.signed_in_client`` so that a client signed in as that user has the switch
on (the matrix itself is addresses only).

**Matrix** (computed when recording, stored with it): the knowledge base's plain list (first load, page 2,
a section) and its search (common words, one word, rare words, with a section, with a year, with both, a
query of small words only, no match, page 2 of the list behind a search); the document review: every tab
and view on every batch, the documents tab per batch (the one with a document being analysed, an
archived one, a batch that does not exist), findings by document (page 2, one batch, one document, a
filter, the Dashboard's "counted" drill, no result), the index (one batch, counted), the synthesis themes
(the minimum of documents 3, challenges only, a search, one batch, no result), repeated findings (one
batch), and one row of each tab that stays as it is.

**The ranking of the search.** STAGE3's quick win ranks the passages on their stored index
(``SearchRank(F("search_vector"), ...)``) instead of an index rebuilt from its text
(``SearchRank("search_vector", ...)`` casts the stored index to text and parses it again: every word once,
weights lost). That change is deliberate and changes which passages come first, hence which 30 documents
and which snippet each search row shows. So the recording is the base's page **with that one change
applied**: :func:`matrix` (which only ``record`` calls) makes the base's ``knowledge.search`` rank on the
stored column (:func:`rank_on_stored_index`), and the check demands the same results, ranks included,
from the head's own code. A self-check of the recording on the base itself (two processes must agree)
sets ``PARITY_D2_STORED_RANK=1``, which applies the same change on the check side; never set it on the head
(the check would then pass without the quick win).

**Context paths.** One function per page lists the old view's ``render`` context names
(:func:`knowledge_paths`, :func:`review_paths`: the review's depend on the tab and view of the address).
Each value is recorded as what the templates read from it (:func:`project`): a model instance becomes a
flat dict of the fields and attributes the page shows (:data:`SHOWN`, never a deferred field, so reading
a value never loads a document's text) plus what the view attached to it (counts, stage chips, a
document's findings...); a paginator page its number, count and number of pages; a dataclass its fields,
the properties the templates read and what the view attached. Everything else is ``core.plain``. A page in
parts that keeps a value as plain data (a dict with the same names) projects to the same thing.

``check`` reads each path from the frame or any part, every holder agreeing (no path moved: :data:`MOVED` is
empty). One deliberate change is taken out by :func:`comparable`: the repeated findings (``groups``) are stored
for every worker as plain data, so the page reads dicts of what it shows (a finding's text, page, link and
document) where it read the findings themselves; both sides are compared on what the page shows of them.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.parity import core
from scripts.parity import overview as _overview

VERIFIED_USER = "parity-verified"  # seed_world.py of group D2
VERIFIED_KEY = "doc_review_verified_only"  # knowledge.review_views.VERIFIED_KEY
STORED_RANK_ENV = "PARITY_D2_STORED_RANK"
NO_MATCH = "zzzyx"
BATCHES = ("Annual reports", "Donor reports", "Evaluations", "Field visit notes")  # seed_world.py
ARCHIVED = "Assessments 2023"
MISSING_BATCH = 999999
KNOWLEDGE, REVIEW = "/knowledge/", "/knowledge/review/"


# ------------------------------------------------------------------------------------------ the users
def _install_verified_client() -> None:
    """Every client ``core`` signs in as :data:`VERIFIED_USER` gets the review's Verified only switch on (its
    session), as the person would by pressing the switch."""
    if getattr(core.signed_in_client, "_d2_verified", False):
        return
    plain_client = core.signed_in_client

    def signed_in_client(user):
        client = plain_client(user)
        if user.get_username() == VERIFIED_USER:
            session = client.session
            session[VERIFIED_KEY] = True
            session.save()
        return client

    signed_in_client._d2_verified = True  # type: ignore[attr-defined]
    core.signed_in_client = signed_in_client


_install_verified_client()


def users() -> dict[str, Any]:
    from neurodb.accounts.models import User

    people = _overview.users()
    verified = User.objects.filter(username=VERIFIED_USER, is_active=True).first()
    if verified is None:
        raise SystemExit(f"the world needs the user {VERIFIED_USER} (group D2's seed_world.py)")
    return {**people, "verified": verified}


# ------------------------------------------------------------------------------------------ the ranking
def rank_on_stored_index() -> bool:
    """Make the base's search rank on the stored index (STAGE3's quick win), when it ranks on an index
    rebuilt from text; True when it changed something. On code that already ranks on the stored column
    it does nothing."""
    from django.contrib.postgres.search import SearchRank
    from django.db.models import F

    from neurodb.knowledge import search

    if getattr(search.SearchRank, "_d2_stored", False):
        return True

    def stored_rank(vector, query, *args, **kwargs):
        if vector == "search_vector":  # the base's text form: the stored column itself instead
            vector = F("search_vector")
        return SearchRank(vector, query, *args, **kwargs)

    stored_rank._d2_stored = True  # type: ignore[attr-defined]
    search.SearchRank = stored_rank
    return True


if os.environ.get(STORED_RANK_ENV) == "1":  # a self-check of the recording on the base itself
    rank_on_stored_index()


# ------------------------------------------------------------------------------------------ the matrix
def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The pages and their filters, as (label, URL). Computed when recording and stored with it; the base's
    search then ranks on the stored index (see the module's notes)."""
    from neurodb.knowledge.models import Document, ReviewBatch

    rank_on_stored_index()
    section = people["viewer"].section_id
    batches = {b.name: b.pk for b in ReviewBatch.objects.filter(name__in=(*BATCHES, ARCHIVED))}
    if len(batches) != len(BATCHES) + 1:
        raise SystemExit("the world needs group D2's review batches (seed_world.py)")
    first, running_batch = batches[BATCHES[0]], batches[BATCHES[1]]
    if not Document.objects.filter(review_batch_id=running_batch, review_status="running").exists():
        raise SystemExit("the world needs the document being analysed of group D2's seed")
    document = (
        Document.objects.filter(review_batch_id=first, review_status="done")
        .order_by("pk")
        .values_list("pk", flat=True)
        .first()
    )

    def kb(*pairs: tuple[str, Any]) -> str:
        return KNOWLEDGE + ("?" + urlencode(pairs) if pairs else "")

    def rv(*pairs: tuple[str, Any]) -> str:
        return REVIEW + ("?" + urlencode(pairs) if pairs else "")

    common = "water school akkar"
    rows = [
        # the knowledge base: the plain list (stays one request) and the search (its results in a part)
        ("knowledge: list", kb()),
        ("knowledge: list, page 2", kb(("page", 2))),
        ("knowledge: list, a section", kb(("section", section))),
        ("knowledge: search", kb(("q", common))),
        ("knowledge: search, one word", kb(("q", "children"))),
        ("knowledge: search, rare words", kb(("q", "chlorination borehole"))),
        ("knowledge: search, a section", kb(("q", common), ("section", section))),
        ("knowledge: search, a year", kb(("q", common), ("year", 2025))),
        ("knowledge: search, section and year", kb(("q", common), ("section", section), ("year", 2025))),
        ("knowledge: search, small words only", kb(("q", "the and of"))),
        ("knowledge: search, no match", kb(("q", NO_MATCH))),
        ("knowledge: search with a page", kb(("q", common), ("page", 2))),
        # the review: documents (in a part)
        ("review: first load", rv()),
        ("review: documents", rv(("tab", "documents"))),
        ("review: documents, a batch", rv(("tab", "documents"), ("batch", first))),
        ("review: documents, a document being analysed", rv(("tab", "documents"), ("batch", running_batch))),
        ("review: documents, archived batch", rv(("tab", "documents"), ("batch", batches[ARCHIVED]))),
        ("review: documents, no such batch", rv(("tab", "documents"), ("batch", MISSING_BATCH))),
        # findings by document and the index (in a part)
        ("review: by document", rv(("tab", "findings"), ("view", "document"))),
        ("review: by document, page 2", rv(("tab", "findings"), ("view", "document"), ("page", 2))),
        ("review: by document, a batch", rv(("tab", "findings"), ("view", "document"), ("batch", first))),
        (
            "review: by document, one document",
            rv(("tab", "findings"), ("view", "document"), ("document", document)),
        ),
        (
            "review: by document, filtered",
            rv(
                ("tab", "findings"),
                ("view", "document"),
                ("category", "challenge"),
                ("verdict", "unreviewed"),
            ),
        ),
        (
            "review: by document, counted",
            rv(("tab", "findings"), ("view", "document"), ("counted", 1), ("batch", first)),
        ),
        ("review: by document, no result", rv(("tab", "findings"), ("view", "document"), ("q", NO_MATCH))),
        ("review: index", rv(("tab", "findings"), ("view", "index"))),
        ("review: index, a batch", rv(("tab", "findings"), ("view", "index"), ("batch", first))),
        ("review: index, counted", rv(("tab", "findings"), ("view", "index"), ("counted", 1))),
        # the synthesis: themes and repeated findings (in a part)
        ("review: themes", rv(("tab", "synthesis"))),
        ("review: themes, 3 documents", rv(("tab", "synthesis"), ("min", 3))),
        ("review: themes, challenges", rv(("tab", "synthesis"), ("challenges", 1))),
        ("review: themes, a search", rv(("tab", "synthesis"), ("q", "education"))),
        ("review: themes, a batch", rv(("tab", "synthesis"), ("batch", first))),
        ("review: themes, no result", rv(("tab", "synthesis"), ("q", NO_MATCH))),
        ("review: repeated", rv(("tab", "synthesis"), ("view", "repeated"))),
        ("review: repeated, a batch", rv(("tab", "synthesis"), ("view", "repeated"), ("batch", first))),
        # the tabs and views that stay one request
        ("review: findings", rv(("tab", "findings"))),
        ("review: findings, page 2", rv(("tab", "findings"), ("page", 2))),
        ("review: key statements", rv(("tab", "findings"), ("view", "statements"))),
        ("review: dashboard", rv(("tab", "dashboard"))),
        ("review: over time", rv(("tab", "synthesis"), ("view", "time"))),
        ("review: coverage", rv(("tab", "synthesis"), ("view", "coverage"))),
        ("review: actions", rv(("tab", "actions"))),
        ("review: report", rv(("tab", "report"))),
    ]
    return rows


# timing.py times these rows: the views that load in parts, on their busiest filter
TIMED = (
    "review: by document",
    "review: themes",
    "review: repeated",
)


def first_figure(url: str) -> str | None:
    """The part whose arrival is ``url``'s first figure (timing.py): the review view's one part; None for the
    pages that stay one request (the page is its own first figure)."""
    if page_of(url) == "knowledge":
        return None
    parts = {
        ("findings", "document"): "findings",
        ("synthesis", "themes"): "synthesis",
        ("synthesis", "repeated"): "synthesis",
    }
    return parts.get(_tab_and_view(url))


# ------------------------------------------------------------------------------------------ the paths
def knowledge_paths(url: str) -> tuple[str, ...]:
    """``/knowledge/`` (``knowledge.views.index``): everything its ``render`` context holds. ``results`` is
    ``search.documents_matching`` (None without ``q``), ``documents`` the plain list's page (None with
    ``q``)."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "q",
        "results",
        "documents",
        "sections",
        "filters",
        "can_add",
        "add_url",
    )


REVIEW_FRAME = (
    "tab",
    "tabs",
    "verified",
    "can_edit",
    "batch_choices",
    "review_on",
    "page_title",
    "page_subtitle",
    "breadcrumbs",
    "header_include",
    "show_verified",
)
REVIEW_TABS: dict[tuple[str, str], tuple[str, ...]] = {
    ("documents", ""): (
        "batches",
        "batch",
        "rows",
        "review_off_reason",
        "addable",
        "working",
        "documents_total",
        "settings_url",
        "upload_url",
        "next",
    ),
    ("findings", "all"): ("f", "chips", "query", "export_url", "page_obj", "findings"),
    ("findings", "document"): (
        "f",
        "chips",
        "query",
        "export_url",
        "documents_page",
        "documents",
        "high_urgency",
    ),
    ("findings", "statements"): (
        "f",
        "page_obj",
        "statements",
        "chips",
        "query",
        "export_url",
        "high_urgency",
    ),
    ("findings", "index"): ("f", "hidden", "chips", "rows", "export_url"),
    ("dashboard", ""): (
        "batch",
        "figures",
        "tiles",
        "tile_links",
        "chart_data",
        "chart_hrefs",
        "high_urgency",
    ),
    ("synthesis", "repeated"): ("groups", "similar"),
    ("synthesis", "themes"): ("synthesis", "trends", "shown_themes"),
    ("synthesis", "time"): ("synthesis", "trends", "over_time"),
    ("synthesis", "coverage"): ("synthesis", "trends", "single", "by_batch"),
    ("actions", ""): (
        "f",
        "figures",
        "cards",
        "page_obj",
        "points",
        "owners",
        "statuses",
        "priorities",
        "query",
        "chart_data",
        "owner_href",
        "downloads",
        "today",
    ),
    ("report", ""): (
        "batch",
        "min_documents",
        "min_choices",
        "tiles",
        "themes",
        "single",
        "download_url",
        "filename",
    ),
}
FINDINGS_COMMON = ("view", "views", "categories", "verdicts", "programmes", "topics", "hidden")
SYNTHESIS_COMMON = (
    "view",
    "views",
    "batch",
    "min_documents",
    "min_choices",
    "challenges",
    "q",
    "paragraph_on",
    "quota",
)
TAB_NAMES = ("documents", "findings", "dashboard", "synthesis", "actions", "report")  # review_views.TABS
FINDING_VIEWS = ("all", "document", "statements", "index")
SYNTHESIS_VIEWS = ("themes", "time", "coverage", "repeated")


def _tab_and_view(url: str) -> tuple[str, str]:
    """The review tab and view an address opens, as ``review_views`` resolves them."""
    query = parse_qs(urlsplit(url).query)
    tab = (query.get("tab") or [""])[-1]
    tab = tab if tab in TAB_NAMES else "documents"
    view = (query.get("view") or [""])[-1]
    if tab == "findings":
        return tab, view if view in FINDING_VIEWS else "all"
    if tab == "synthesis":
        return tab, view if view in SYNTHESIS_VIEWS else "themes"
    return tab, ""


def review_paths(url: str) -> tuple[str, ...]:
    """``/knowledge/review/`` (``knowledge.review_views.page``): the page's own context and the keys its
    tab's function returns (``TAB_CONTEXT``), by tab and view."""
    tab, view = _tab_and_view(url)
    extra = {"findings": FINDINGS_COMMON, "synthesis": SYNTHESIS_COMMON}.get(tab, ())
    keys = (*REVIEW_FRAME, *extra, *REVIEW_TABS[(tab, view)])
    return tuple(dict.fromkeys(keys))


# A page: its name, the address it answers (the path, without the query) and its context paths.
PAGES: dict[str, tuple[re.Pattern[str], Callable[[str], tuple[str, ...]]]] = {
    "knowledge": (re.compile(re.escape(KNOWLEDGE)), knowledge_paths),
    "review": (re.compile(re.escape(REVIEW)), review_paths),
}
# Where the pages in parts keep a path that moved, per page: ``{path: (its name there, (part, ...))}``,
# "shell" naming the frame. A path not listed is read under its own name from the frame or any part, every
# holder agreeing. None is listed: the paths that moved into a part kept their names (by document:
# ``documents_page``, ``documents``, ``high_urgency`` in ``findings``; the themes: ``synthesis``, ``trends``,
# ``shown_themes`` and, in both, ``batch``, ``min_documents``, ``challenges``, ``paragraph_on`` and ``quota``,
# in ``synthesis``; repeated: ``groups`` in ``synthesis``), and the frame keeps the rest.
MOVED: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {"knowledge": {}, "review": {}}
# The old pages' json_script ids renamed by the conversion (none of the converted blocks has one today:
# the review's chart JSON belongs to the dashboard and actions tabs, which stay as they are)
JSON_IDS: dict[str, str] = {}
# Element ids only the pages in parts have: the parts' own wrappers, and the repeated findings' heading (its
# placeholder's aria-labelledby)
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*|review-repeated-title")


def page_of(url: str) -> str:
    """The name of the page ``url`` opens (a key of :data:`PAGES`)."""
    path = urlsplit(url).path
    for name, (pattern, _paths) in PAGES.items():
        if pattern.fullmatch(path):
            return name
    raise ValueError(f"{url} is not a page of this group")


# ------------------------------------------------------------------------------------------ projections
# What the pages show of each model, by attribute name (a field, a property or an annotation); ``pk``
# always. A deferred field (``review_data.light``) is never read.
SHOWN: dict[str, tuple[str, ...]] = {
    "knowledge.Document": (
        "title",
        "status",
        "source",
        "summary",
        "section_id",
        "year",
        "created_at",
        "added_by_id",
        "review_status",
        "review_batch_id",
        "reviewed_at",
        "document_date",
        "periodic",
    ),
    "knowledge.Chunk": ("document_id", "position", "page"),
    "knowledge.Link": ("kind", "object_id", "label", "mentions"),
    "knowledge.ReviewBatch": ("name", "description", "archived", "created_at"),
    "knowledge.Topic": ("name", "path"),
    "knowledge.DocumentFinding": (
        "document_id",
        "position",
        "category",
        "topic_id",
        "tag_text",
        "text",
        "quote",
        "kind",
        "page_label",
        "page_from",
        "quote_found",
        "exact_page",
        "place_text",
        "place_match",
        "governorate_name",
        "district_name",
        "finding_date",
        "date_text",
        "evidence",
        "verdict",
        "reviewed_by_id",
        "derived",
        "manual",
        "url",
    ),
    "knowledge.DocumentStatement": (
        "document_id",
        "position",
        "text",
        "urgency",
        "category",
        "topic_id",
        "place_text",
        "date_text",
        "verdict",
        "reviewed_by_id",
    ),
    "knowledge.DocumentActionPoint": (
        "document_id",
        "position",
        "action",
        "owner_text",
        "deadline_text",
        "deadline_date",
        "priority",
        "status",
        "derived",
        "topic_id",
    ),
    "users.Section": ("name",),
}
# The many-to-many lists a page shows, read only when the view prefetched them (no query is made here)
PREFETCHED = {"knowledge.Document": ("links",), "knowledge.DocumentStatement": ("cites",)}
PREFETCHED["knowledge.DocumentActionPoint"] = ("cites",)
# The properties of the dataclasses the templates read
PROPERTIES = {
    "Theme": ("n_documents", "n_findings", "average_evidence", "year_list", "year_span"),
    "Repeated": ("documents", "lead"),
}


def _get(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _model(obj: Any, depth: int) -> dict[str, Any]:
    label = obj._meta.label
    deferred = obj.get_deferred_fields()
    attnames = {f.attname for f in obj._meta.concrete_fields}
    out: dict[str, Any] = {"pk": obj.pk}
    for name in SHOWN.get(label, ()):
        if name in deferred:
            continue
        out[name] = project(getattr(obj, name, None), depth)
    if label not in SHOWN:
        out["str"] = str(obj)
    cache = getattr(obj, "_prefetched_objects_cache", {})
    for name in PREFETCHED.get(label, ()):
        if name in cache:
            out[name] = [project(item, depth) for item in cache[name]]
    for name, value in vars(obj).items():  # what the view or the query attached: counts, chips, lists
        if name.startswith("_") or name in attnames or name in out:
            continue
        out[name] = project(value, depth)
    return out


def project(value: Any, _depth: int = 0) -> Any:
    """``value`` as what the pages read from it, as JSON-comparable data (see the module's notes)."""
    if _depth > 40:
        return {"__too_deep__": repr(value)[:200]}
    depth = _depth + 1
    from django.core.paginator import Page
    from django.db.models import Model
    from django.db.models.query import QuerySet

    if isinstance(value, Model):
        return _model(value, depth)
    if isinstance(value, Page):
        return {
            "number": value.number,
            "count": value.paginator.count,
            "num_pages": value.paginator.num_pages,
        }
    if isinstance(value, QuerySet):
        return [project(item, depth) for item in value]
    if isinstance(value, Mapping):
        return {core._key(key): project(item, depth) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [project(item, depth) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(core.plain(item) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        out: dict[str, Any] = {"__dataclass__": name}
        names = {f.name for f in dataclasses.fields(value)}
        for field in names:
            out[field] = project(getattr(value, field), depth)
        for prop in PROPERTIES.get(name, ()):
            out[prop] = project(getattr(value, prop), depth)
        if name == "Hit":  # the knowledge search's passage: its snippet is what the page shows
            out["snippet"] = str(value.snippet())
        for attr, item in vars(value).items():  # what the view attached (a theme's top findings, its link)
            if not attr.startswith("_") and attr not in names:
                out[attr] = project(item, depth)
        return out
    return core.plain(value, depth)


def _documents_page(value: Any) -> Any:
    """The knowledge base's plain list: the page and its rows."""
    if value is None:
        return None
    return {**project(value), "object_list": project(list(value.object_list))}


# How each path is projected, when not by :func:`project`
PROJECT: dict[str, dict[str, Callable[[Any], Any]]] = {"knowledge": {"documents": _documents_page}}


# ------------------------------------------------------------------------------------------- deliberate changes
def _shown_finding(finding: Any) -> Any:
    """What the repeated findings view shows of one finding: its id, text, page, link and document. The old
    page's projected finding has ``document_id``; the stored group's finding (plain data) has ``document``."""
    if not isinstance(finding, Mapping):
        return finding
    document = finding.get("document")
    document_id = document.get("pk") if isinstance(document, Mapping) else finding.get("document_id")
    return {
        "pk": finding.get("pk"),
        "text": finding.get("text"),
        "page_label": finding.get("page_label"),
        "url": finding.get("url"),
        "document_id": document_id,
    }


def _shown_groups(groups: Any) -> Any:
    if not isinstance(groups, list):
        return groups
    return [
        {
            "documents": group.get("documents"),
            "lead": _shown_finding(group.get("lead")),
            "findings": [_shown_finding(f) for f in group.get("findings") or []],
        }
        if isinstance(group, Mapping)
        else group
        for group in groups
    ]


def comparable(observation: dict[str, Any], side: str) -> dict[str, Any]:
    """One observation as it is compared (``side`` is "record" or "check"): the repeated findings (``groups``)
    reduced, on both sides, to what the page shows of each finding (see the module's notes). The page's text
    and links (the document's title and address included) are compared as they are."""
    figures = observation["figures"]
    if "groups" not in figures:
        return observation
    return {**observation, "figures": {**figures, "groups": _shown_groups(figures["groups"])}}


# --------------------------------------------------------------------------------------------- the figures
_ABSENT = object()


def _read(context: Any, path: str) -> Any:
    if context is None:
        return _ABSENT
    try:
        if path not in context:
            return _ABSENT
        return context[path]
    except (KeyError, TypeError):
        return _ABSENT


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The page's figures under the old page's context paths, and the problems found on the way (a listed
    path the old page does not have; parts that should agree and do not)."""
    name = page_of(page.url)
    paths = PAGES[name][1](page.url)
    projectors = PROJECT.get(name, {})
    shell = getattr(page.shell, "context", None)
    problems: list[str] = []
    out: dict[str, Any] = {}
    if mode == "record":
        for path in paths:
            value = _read(shell, path)
            if value is _ABSENT:
                if page.shell.status_code == 200:
                    problems.append(f"{path}: not in the old page's context")
                value = None
            out[path] = projectors.get(path, project)(value)
        return out, problems

    contexts = {"shell": shell} | {
        part: getattr(response, "context", None)
        for part, response in page.parts.items()
        if response.status_code == 200
    }
    for path in paths:
        where, names = MOVED[name].get(path, (path, tuple(contexts)))
        found = [
            (holder, projectors.get(path, project)(value))
            for holder in names
            if (value := _read(contexts.get(holder), where)) is not _ABSENT
        ]
        if not found:
            out[path] = None
            if page.shell.status_code == 200:
                problems.append(f"{path}: in no part and not in the shell")
            continue
        first = found[0][1]
        for holder, value in found[1:]:
            if value != first:
                problems.append(f"{path}: {found[0][0]} and {holder} disagree")
        out[path] = first
    return out, problems
