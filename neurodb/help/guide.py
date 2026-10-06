"""The help guide: the Markdown pages in ``guide/`` (packaged with the code, as ``docs/`` is not in the
image), read once per process.

Each page is cut into **sections** at its headings (``##`` and ``###``; the text before the first one is
the page's introduction, under the page title). A section's ``slug`` is the anchor its heading gets on
the page (``/help/<page>/#<slug>``): the same slugify the Markdown table of contents uses, made unique
within the page as it does. :func:`search` is a small full-text search over the sections (no database):
words folded to lower case and a plain English ending removed, a heading word counting more, every word
of the question that is found adding to the score. :func:`render` turns a page into safe HTML (``nh3``),
with an anchor on every heading; links stay only to NeuroDB's own pages.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import markdown
import nh3
from markdown.extensions.tables import TableExtension
from markdown.extensions.toc import TocExtension, slugify, unique

GUIDE_DIR = Path(__file__).resolve().parent / "guide"
# The pages, in the order of the guide's list (the file name, without .md, is the page's address)
ORDER = (
    "overview",
    "monitoring-insights",
    "action-points",
    "exports",
    "knowledge-and-documents",
    "ask-neurodb",
    "watch",
    "briefs-and-overview",
    "ai-limits",
    "glossary",
)
SECTION_CHARS = 1500  # characters of a section a search result carries
HEADING_WEIGHT = 3  # a word of the heading counts this many times
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_WORD = re.compile(r"[a-z0-9]+")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_STOP = frozenset(
    "a an and are as at be by can do does for from how i in is it its me my of on or the this to was what "
    "when where which who why with you your".split()
)


@dataclass(frozen=True)
class Section:
    page: str
    page_title: str
    heading: str
    slug: str  # "" for a page's introduction
    level: int  # 1 for the introduction, 2 or 3
    text: str  # the section's Markdown, its own heading left out

    @property
    def url(self) -> str:
        return f"/help/{self.page}/" + (f"#{self.slug}" if self.slug else "")

    def plain(self, limit: int | None = None) -> str:
        """The text without Markdown links' addresses and table rules, cut to ``limit`` characters."""
        text = _LINK.sub(r"\1", self.text)
        text = re.sub(r"^\|?[-:| ]+\|?$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if limit is not None and len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0] + " …"
        return text


@dataclass(frozen=True)
class Page:
    key: str
    title: str
    markdown: str
    sections: tuple[Section, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        return f"/help/{self.key}/"


def _split(key: str, text: str) -> Page:
    """A page and its sections (see the module's notes)."""
    lines = text.splitlines()
    title = key.replace("-", " ").capitalize()
    used: set[str] = set()
    parts: list[tuple[str, str, int, list[str]]] = []  # heading, slug, level, lines
    current: list[str] = []
    heading, slug, level = "", "", 1
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            title = match.group(2)
            unique(slugify(title, "-"), used)  # the page title takes its anchor first, as on the page
            continue
        if match:
            parts.append((heading, slug, level, current))
            heading, level = match.group(2), len(match.group(1))
            slug = unique(slugify(heading, "-"), used)
            current = []
            continue
        current.append(line)
    parts.append((heading, slug, level, current))
    sections = tuple(
        Section(key, title, heading or title, slug, level, "\n".join(body).strip())
        for heading, slug, level, body in parts
        if heading or "\n".join(body).strip()
    )
    return Page(key, title, text, sections)


@cache
def pages() -> dict[str, Page]:
    """Every guide page by its key, in ``ORDER`` (read once per process)."""
    found = {}
    for key in ORDER:
        path = GUIDE_DIR / f"{key}.md"
        found[key] = _split(key, path.read_text(encoding="utf-8"))
    return found


def page(key: str) -> Page | None:
    return pages().get(key)


def section(key: str, slug: str) -> Section | None:
    found = page(key)
    if found is None:
        return None
    return next((s for s in found.sections if s.slug == (slug or "")), None)


# ------------------------------------------------------------------------------------------ search
def _stem(word: str) -> str:
    for ending in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(ending) + 2 and word.endswith(ending):
            return word[: -len(ending)] + ("y" if ending == "ies" else "")
    return word


def words(text: str) -> list[str]:
    """The searchable words of ``text``: lower case, short words and common ones left out, a plain
    English ending removed ("scores" and "scored" find "score")."""
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())]


@dataclass
class _Index:
    sections: list[Section]
    terms: list[Counter]
    frequency: Counter  # in how many sections each word appears


@cache
def _index() -> _Index:
    """The search index, built once per process."""
    sections = [s for p in pages().values() for s in p.sections]
    terms = []
    frequency: Counter = Counter()
    for s in sections:
        counts = Counter(words(s.plain()))
        for word in words(f"{s.heading} {s.page_title if not s.slug else ''}"):
            counts[word] += HEADING_WEIGHT
        terms.append(counts)
        frequency.update(set(counts))
    return _Index(sections, terms, frequency)


def search(query: str, limit: int = 5) -> list[Section]:
    """The sections that best match ``query``: each word of the query found in a section adds to its
    score (more for a rare word, less for a long section); a rule id ("R7") found counts most."""
    index = _index()
    asked = list(dict.fromkeys(words(query)))
    if not asked:
        return []
    total = len(index.sections)
    scored = []
    for position, (s, counts) in enumerate(zip(index.sections, index.terms, strict=True)):
        score = 0.0
        size = sum(counts.values()) or 1
        for word in asked:
            if counts.get(word):
                rarity = math.log(1 + total / index.frequency[word])
                score += rarity * (1 + math.log(counts[word])) / math.sqrt(size / 50 + 1)
        rule_ids = [w for w in asked if re.fullmatch(r"r\d{1,2}", w)]
        if rule_ids and any(re.search(rf"\b{w}\b", s.text, re.IGNORECASE) for w in rule_ids):
            score += 5
        if score:
            scored.append((score, -position, s))
    scored.sort(reverse=True)
    return [s for _score, _pos, s in scored[:limit]]


def snippet(s: Section, query: str, chars: int = 220) -> str:
    """A short extract of ``s`` around the first word of ``query`` it holds."""
    text = " ".join(s.plain().split())
    folded = text.lower()
    at = -1
    for word in _WORD.findall(query.lower()):
        if word in _STOP or len(word) < 2:
            continue
        at = folded.find(word[: max(3, len(word) - 2)])
        if at >= 0:
            break
    start = max(0, at - chars // 3) if at >= 0 else 0
    piece = text[start : start + chars]
    return ("… " if start else "") + piece + (" …" if start + chars < len(text) else "")


# ------------------------------------------------------------------------------------------ rendering
_SITE_PATH = re.compile(r"(/(?![/\\])[^\x00-\x20\x7f\\]*|#[A-Za-z0-9_-]+)")


def _site_links_only(tag: str, attr: str, value: str) -> str | None:
    """Keep only links to NeuroDB's own pages and to anchors of the page."""
    if tag == "a" and attr == "href":
        return value if _SITE_PATH.fullmatch(value) else None
    return value


@cache
def render(key: str) -> str:
    """The page as safe HTML: headings with their anchors (``id`` and a ¶ link), tables, lists, code. The
    page title is left out (the page header shows it)."""
    found = page(key)
    if found is None:
        return ""
    html = markdown.markdown(
        found.markdown,
        extensions=[
            TableExtension(use_align_attribute=True),
            "sane_lists",
            TocExtension(permalink="¶", permalink_title="Link to this section", slugify=slugify),
        ],
    )
    html = re.sub(r"<h1[^>]*>.*?</h1>\s*", "", html, count=1, flags=re.DOTALL)
    return nh3.clean(
        html,
        tags={
            "h1", "h2", "h3", "h4", "p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol",
            "li", "table", "thead", "tbody", "tr", "th", "td", "a", "hr",
        },
        attributes={
            "h1": {"id"}, "h2": {"id"}, "h3": {"id"}, "h4": {"id"},
            "a": {"href", "title", "class"}, "th": {"align"}, "td": {"align"},
        },
        url_schemes=set(),
        attribute_filter=_site_links_only,
        link_rel=None,
    )  # fmt: skip
