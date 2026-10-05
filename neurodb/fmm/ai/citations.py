"""The check of a chat answer before it is shown and kept: its visit links, its other links, its figures,
and the people or contacts it may name.

- A link to a visit (``/fmm/visits/<key>/``) stays only when that visit was returned by a look-up of
  this answer or verified in an earlier turn of the conversation (``ctx.seen``); otherwise it becomes
  its plain text followed by "(not checked)".
- Any other link stays only when a look-up returned that url (``ctx.urls``); otherwise its text stays.
- Figures: references and dates are set aside first (``watch.grounding``); every other number must be
  one the look-ups returned (``ctx.numbers``, with one-decimal and whole roundings), or a whole number up
  to 10 (a count of what is cited). The others are listed under the answer as not checked.
- E-mail addresses, phone numbers, links outside NeuroDB, the names NeuroDB knows and any name written
  after a title ("Mrs ...") are replaced by placeholders, line by line, so the answer keeps its Markdown.
- The rendered answer gets the same link check once more, so a link written another way (a reference
  link, raw HTML) cannot slip past it.

The notice under the answer says what was done: "All visit references were checked against the visits
looked up." or "1 visit reference was removed (not among the visits looked up). These figures could not
be checked against the data: 444, 1004."
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from neurodb.assistant import agent
from neurodb.review.services import numbers_in
from neurodb.watch import grounding, people, redact

from .. import privacy

# [text](url), [text](<url>) or [text](url "title"): a Markdown link (the url without spaces)
LINK = re.compile(
    r"\[([^\]\n]{0,300})\]\(\s*<?([^)\s<>]+)>?(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"
)
# <a href="...">...</a> in the rendered answer: a link written another way (a reference link, raw HTML)
ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL)
HREF = re.compile(r'\bhref="([^"]*)"')
VISIT_URL = re.compile(r"^/fmm/visits/([A-Za-z0-9_-]{1,40})/?(?:[?#].*)?$")
# "Visit 1722", "#1722": a visit named in the text, not a figure
VISIT_NAMED = re.compile(r"\b[Vv]isits?\s+#?\d+\b|#\d+\b")
NOT_CHECKED = "(not checked)"
SHOWN_NUMBERS = 20  # unchecked figures listed in the notice at most


@dataclass
class Checked:
    """A chat answer after the check: its text and HTML, the visit keys kept and removed, and the figures
    that could not be checked against the look-ups."""

    text: str
    html: str
    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchecked_numbers: list[str] = field(default_factory=list)

    @property
    def notice(self) -> str:
        """The line under the answer."""
        if self.removed:
            n = len(self.removed)
            parts = [
                ngettext(
                    "%(n)s visit reference was removed (not among the visits looked up).",
                    "%(n)s visit references were removed (not among the visits looked up).",
                    n,
                )
                % {"n": n}
            ]
        else:
            parts = [_("All visit references were checked against the visits looked up.")]
        if self.unchecked_numbers:
            shown = ", ".join(self.unchecked_numbers[:SHOWN_NUMBERS])
            parts.append(
                _("These figures could not be checked against the data: %(numbers)s.") % {"numbers": shown}
            )
        return " ".join(parts)


def _scrub(text: str, names_: frozenset[str]) -> str:
    """People and contacts out of the answer, line by line (the Markdown is kept)."""
    lines = []
    for line in text.split("\n"):
        line = people.EMAIL.sub(people.EMAIL_WITHHELD, line)
        line = redact.LINK.sub(redact.LINK_WITHHELD, line)
        line = privacy.PHONE.sub(privacy.PHONE_WITHHELD, line)
        line = privacy.INTL_PHONE.sub(privacy.PHONE_WITHHELD, line)
        line = privacy.HONORIFIC_NAME.sub(people.NAME_WITHHELD, line)
        lines.append(people.scrub(line, names_))
    return "\n".join(lines)


def _unchecked(text: str, allowed: set[str]) -> list[str]:
    """The figures of ``text`` that no look-up returned (references, dates, visit names and small whole
    numbers set aside)."""
    rest = LINK.sub(" ", text)  # link texts are checked as links
    rest = VISIT_NAMED.sub(" ", rest)
    rest = grounding.REFERENCE_IN_TEXT.sub(" ", rest)
    spans = [match.span() for match, *_ in grounding.dates_in(rest)]
    rest = grounding._without(rest, spans)
    out: list[str] = []
    for written in sorted(numbers_in(rest), key=lambda n: float(n) if _is_number(n) else 0):
        number = grounding._norm(written)
        if number in allowed or (number.isdigit() and int(number) <= grounding.SMALL):
            continue
        if written not in out:
            out.append(written)
    return out


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def verify(answer: str, ctx, names_: frozenset[str] | None = None) -> Checked:
    """The answer checked against what the look-ups returned (``ctx``: a ``tools.ChatContext``)."""
    names_ = privacy.names() if names_ is None else names_
    kept: list[str] = []
    removed: list[str] = []

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        visit = VISIT_URL.match(url)
        if visit:
            key = visit.group(1)
            if key in ctx.seen:
                if key not in kept:
                    kept.append(key)
                return match.group(0)
            if key not in removed:
                removed.append(key)
            return f"{label} {NOT_CHECKED}"
        if url in ctx.urls:
            return match.group(0)
        return label

    def anchor(match: re.Match) -> str:
        # The last guard, on the HTML shown: a link the Markdown check did not see (a reference link,
        # raw HTML) stays only on the same terms.
        href = HREF.search(match.group(1))
        inner = match.group(2)
        url = html.unescape(href.group(1)) if href else ""
        visit = VISIT_URL.match(url)
        if visit:
            key = visit.group(1)
            if key in ctx.seen:
                if key not in kept:
                    kept.append(key)
                return match.group(0)
            if key not in removed:
                removed.append(key)
            return f"{inner} {NOT_CHECKED}"
        return match.group(0) if url and url in ctx.urls else inner

    text = LINK.sub(link, str(answer or ""))
    text = _scrub(text, names_)
    unchecked = _unchecked(text, ctx.numbers)
    shown = ANCHOR.sub(anchor, agent.render(text))
    return Checked(text=text, html=shown, kept=kept, removed=removed, unchecked_numbers=unchecked)
