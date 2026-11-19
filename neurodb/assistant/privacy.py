"""What Ask NeuroDB sends to the AI provider about people: nothing. The owner's rule: a text reaches the
provider only after people's names, e-mail addresses, links and phone numbers are taken out.

- **The question and the earlier turns** (:func:`question`, :func:`history`): the question and the earlier
  questions and answers of the conversation are cleaned each time they are sent. The question log
  (``AssistantQuestion``) keeps what the person typed: it stays in NeuroDB, the person sees their own
  words in the conversation and under Recent questions, and every request cleans them again with the
  names NeuroDB knows at that moment.
- **Every lookup's result** (:func:`result`), before the model reads it and before its numbers count as
  looked up (the chart check reads the cleaned result): every text in it, at any depth, its keys too, a
  lookup's input error included. The eTools Datamart lookups (:data:`CLEANED_BY_LOOKUP`) clean their
  own results with the same cleaner and more (the record's own names, the keys that hold a person):
  they are not cleaned twice.
- **One cleaner** for every text (:func:`text`): the eTools Datamart's (``datamart.query.clean_written``)
  takes out e-mail addresses (whole, cut short, spaced or spelled out: "name at example dot org"),
  links, phone numbers, the person names NeuroDB knows (``watch.people.known_names``: users, partner
  staff, focal points, assignees, the people the eTools records name...) and any name written after a
  title, each replaced by a placeholder ("[name withheld]"), and keeps the line breaks (the Markdown of
  an earlier answer, the paragraphs of a document). A name after Sheikh, Dr or Doctor is kept only in a
  label, the value of a key that names a thing (:func:`_label`: a partner, a place, a section, a title:
  "Dr. Example Social Foundation", "Sheikh Taba"), as the Datamart does; in any other text it is taken
  out. Organisation, section and place names are not people's.
- **Kept as they are**: numbers, yes/no and empty values (never changed), and a link to one of NeuroDB's
  own pages under a url key ("url", "urls", "..._url": a site path such as "/partners/3/"). Ids, dates,
  codes and figures written as text ("LEB/PCA2021123/PD2021456", "2024-03-15", "76.9%") are left as they
  are by the cleaner, except a number with a phone's shape ("71 123 456").

The cleaning is deterministic: the same text and names always give the same bytes (the prompt cache
depends on it), and a cleaned text is not changed again. Help, Monitoring insights and NeuroDB Watch run
the same loop with their own options and filters; this module is Ask NeuroDB's alone (``agent.answer``
without options).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from neurodb.datamart import query as dmq
from neurodb.watch import people

# A link to one of NeuroDB's own pages: a path, not "//host" or "/\host" (both leave the site), with no
# spaces or control characters (browsers drop tabs and newlines inside a URL)
SITE_PATH = re.compile(r"/(?![/\\])[^\x00-\x20\x7f\\]*")
# The lookups that clean their own results (the eTools Datamart's, ``datamart.query``)
CLEANED_BY_LOOKUP = frozenset({"etools_datasets", "etools_query", "etools_search", "etools_record"})
# Keys whose texts are labels of Ask's lookups, the name of a thing (where a name after Sheikh or Dr is
# kept), besides the Datamart's keys of places and organisations (``datamart.query._keeps_titles``)
LABEL_KEYS = frozenset(
    {"name", "names", "short_name", "aliases", "title", "label", "kind_label", "section", "sections"}
    | {"database", "indicator", "report", "document", "batch", "topic", "category", "source", "edition"}
    | {"output", "outcome", "programme", "grant", "centre", "center"}
)


def _known(names: Iterable[str] | None) -> frozenset[str]:
    if names is None:
        return people.known_names()
    return names if isinstance(names, frozenset) else frozenset(names)


def text(value: str, names: Iterable[str] | None = None, label: bool = False) -> str:
    """``value`` as Ask may send it (see the module's notes); ``label``: the name of a thing, where a name
    after Sheikh or Dr is kept; ``names``: the person names NeuroDB knows (read when None)."""
    return dmq.clean_written(str(value), _known(names), keeps=label)


def question(value: str, names: Iterable[str] | None = None) -> str:
    """The question as it is sent."""
    return text(value, names)


def history(turns: Iterable[Mapping[str, Any]], names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """The earlier turns as they are sent: each question and answer cleaned, the rest as it is."""
    known = _known(names)
    sent = []
    for turn in turns:
        said = {key: turn[key] for key in ("question", "answer") if isinstance(turn.get(key), str)}
        sent.append({**turn, **{key: text(value, known) for key, value in said.items()}})
    return sent


def result(tool: str, value: Any, names: Iterable[str] | None = None) -> Any:
    """A lookup's result as the model reads it: every text cleaned (:func:`cleaned`), unless the lookup
    cleans its own (:data:`CLEANED_BY_LOOKUP`)."""
    if tool in CLEANED_BY_LOOKUP:
        return value
    return cleaned(value, names)


def cleaned(value: Any, names: Iterable[str] | None = None) -> Any:
    """``value`` (a lookup's result, or its input error) with every text cleaned, keys too, at any depth;
    numbers, yes/no, empty values and NeuroDB's own links under a url key as they are."""
    return _Walk(_known(names))(value, "")


def _url_key(key: str) -> bool:
    return key in ("url", "urls") or key.endswith("_url")


def _label(key: str) -> bool:
    """A key that holds the name of a thing: one of Ask's labels (:data:`LABEL_KEYS`) or a key the Datamart
    reads as the name of a place or an organisation ("partner", "partner_name", "governorate", "donors")."""
    return key in LABEL_KEYS or dmq._keeps_titles(key)


class _Walk:
    """One result's walk: each text cleaned once (a status or a section's name repeats on every row)."""

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self.done: dict[tuple[str, bool], str] = {}

    def text(self, value: str, label: bool) -> str:
        found = self.done.get((value, label))
        if found is None:
            found = self.done[(value, label)] = text(value, self.names, label)
        return found

    def __call__(self, value: Any, key: str) -> Any:
        if isinstance(value, str):
            if _url_key(key) and SITE_PATH.fullmatch(value):
                return value
            return self.text(value, _label(key))
        if isinstance(value, Mapping):
            return {
                (self.text(k, True) if isinstance(k, str) else k): self(v, str(k)) for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self(v, key) for v in value]
        return value
