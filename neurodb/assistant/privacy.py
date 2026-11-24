"""What Ask NeuroDB sends to the AI provider about people: the owner's rule is that a text reaches the
provider only after people's names, e-mail addresses, links and phone numbers are taken out, and this module
takes out every one it can find.

- **The question and the earlier turns** (:func:`question`, :func:`history`): the question and the earlier
  questions and answers of the conversation are cleaned each time they are sent. The question log
  (``AssistantQuestion``) keeps what the person typed: it stays in NeuroDB, the person sees their own
  words in the conversation and under Recent questions, and every request cleans them again with the
  names NeuroDB knows at that moment.
- **Every lookup's result** (:func:`result`), before the model reads it and before its numbers count as
  looked up (the chart check reads the cleaned result): every text in it, at any depth, its keys too, a
  lookup's input error included. A lookup that cuts a long text cleans it whole first, then cuts it
  (``tools._short``), so a cut never splits a name, a number or an address before it is cleaned. The
  eTools Datamart lookups (:data:`CLEANED_BY_LOOKUP`) clean their own results with the same cleaner and
  more (the record's own names, the keys that hold a person): they are not cleaned twice.
- **One cleaner** for every text (:func:`text`): the eTools Datamart's (``datamart.query.clean_written``)
  takes out e-mail addresses (whole, cut short, spaced or spelled out: "name at example dot org"),
  links, phone numbers, the full names of the people NeuroDB knows (``watch.people.known_names``: users,
  partner staff, focal points, assignees, the people the eTools records name...) and any name written
  after a title, each replaced by a placeholder ("[name withheld]"), and keeps the line breaks (the
  Markdown of an earlier answer, the paragraphs of a document); an address or a phone written with more
  spaces or over two lines is found too. A name after Sheikh, Dr or Doctor is kept only where the value
  names a partner or a place (:func:`_names_a_place_or_org`: "partner", "governorate", "donors", the name of
  an item of a list of partners, of a document's linked partner or place, of partner_details' partner:
  "Dr. Example Social Foundation", "Sheikh Taba"), as the Datamart does; in any other text (a document's
  title or source, a report, a topic, a key) it is taken out. In an earlier answer it is kept too: the
  model wrote it from results already cleaned, so it named a partner or a place. Organisation, section
  and place names are not people's. A first name or a surname written alone, and a person NeuroDB does
  not know named without a title, are not found.
- **Kept as they are**: numbers, yes/no and empty values (never changed), a link to one of NeuroDB's own
  pages under a url key ("url", "urls", "..._url", or inside a mapping under one: a site path such as
  "/partners/3/"), a number written alone under a key that holds an amount ("amount": "71123456"). Ids,
  dates, codes and figures written as text ("LEB/PCA2021123/PD2021456", "2024-03-15", "76.9%") are left
  as they are by the cleaner, except a number with a phone's shape ("71 123 456"), unless a currency or a
  count is written right next to it ("LBP 71 500 000", "81 250 300 people").

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
# Keys of Ask's lookups that hold the name of a place or an organisation (where a name after Sheikh or Dr
# is kept), besides the Datamart's (``datamart.query._keeps_titles``: "partner", "partner_name",
# "governorate", "donors", "location"...), case left aside (a filter's label: "Center", "School")
PLACE_OR_ORG_KEYS = frozenset(
    {
        "centre",
        "center",
        "centres",
        "centers",
        "school",
        "schools",
        "activityinfo_names",
        "partner_label",
        "municipality",
    }
)
# The keys that name the thing their object is ("name", "short_name"...): a partner's or a place's name
# when the object is one (:func:`_names_a_place_or_org`)
NAME_KEYS = frozenset({"name", "names", "short_name", "aliases", "label"})
# The kinds of a knowledge link or a knowledge hub thing ("kind": "partner") that are partners or places
PLACE_OR_ORG_KINDS = frozenset(
    {"partner", "donor", "governorate", "district", "place", "location", "site", "centre", "organisation"}
)
# The lookups whose result is itself one partner: its name and short name are a partner's
PARTNER_RESULTS = frozenset({"partner_details"})


def _known(names: Iterable[str] | None) -> frozenset[str]:
    if names is None:
        return people.known_names()
    return names if isinstance(names, frozenset) else frozenset(names)


def text(value: str, names: Iterable[str] | None = None, label: bool = False) -> str:
    """``value`` as Ask may send it (see the module's notes); ``label``: the name of a partner or a place
    (or a text that names only those), where a name after Sheikh or Dr is kept; ``names``: the person names
    NeuroDB knows (read when None). A number with a phone's prefix right next to a currency or a count is
    an amount."""
    return dmq.clean_written(str(value), _known(names), keeps=label, amounts=True)


def question(value: str, names: Iterable[str] | None = None) -> str:
    """The question as it is sent."""
    return text(value, names)


def history(turns: Iterable[Mapping[str, Any]], names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """The earlier turns as they are sent: each question and answer cleaned, the rest as it is. An answer
    keeps a name after Sheikh or Dr: the model wrote it from lookup results already cleaned, where only a
    partner or a place keeps one ("Dr. Example Social Foundation"), so a follow-up can still name it. A
    question, typed by the person, does not."""
    known = _known(names)
    sent = []
    for turn in turns:
        said = {key: turn[key] for key in ("question", "answer") if isinstance(turn.get(key), str)}
        sent.append({**turn, **{key: text(value, known, key == "answer") for key, value in said.items()}})
    return sent


def result(tool: str, value: Any, names: Iterable[str] | None = None) -> Any:
    """A lookup's result as the model reads it: every text cleaned (:func:`cleaned`), unless the lookup
    cleans its own (:data:`CLEANED_BY_LOOKUP`)."""
    if tool in CLEANED_BY_LOOKUP:
        return value
    return cleaned(value, names, partner=tool in PARTNER_RESULTS)


def cleaned(value: Any, names: Iterable[str] | None = None, partner: bool = False) -> Any:
    """``value`` (a lookup's result, or its input error) with every text cleaned, keys too, at any depth;
    numbers, yes/no, empty values, NeuroDB's own links under a url key and a number under an amount key as
    they are. ``partner``: the result is one partner (:data:`PARTNER_RESULTS`)."""
    return _Walk(_known(names))(value, "", named=partner)


def _url_key(key: str) -> bool:
    return key in ("url", "urls") or key.endswith("_url")


def _place_or_org(key: str) -> bool:
    """A key that holds the name of a place or an organisation: one the Datamart reads so ("partner",
    "partner_name", "governorate", "donors", "location") or one of Ask's (:data:`PLACE_OR_ORG_KEYS`)."""
    return key.casefold() in PLACE_OR_ORG_KEYS or dmq._keeps_titles(key)


def _names_a_place_or_org(value: Mapping, key: str) -> bool:
    """``value``, an object found under ``key``, is a partner or a place, so its name keys
    (:data:`NAME_KEYS`) hold one: an item of a list of partners or places ("partners": [{"name": ...}]), a
    knowledge link or a hub thing of such a kind ("kind": "partner")."""
    kind = value.get("kind")
    if isinstance(kind, str) and kind.strip().casefold() in PLACE_OR_ORG_KINDS:
        return True
    return bool(key) and (dmq._keeps_titles("name", key) or _place_or_org(key))


def _keyed_by_place_or_org(key: str) -> bool:
    """The keys of the object under ``key`` are names of partners or places ("partners", "by_partner",
    "activity_reports_by_governorate")."""
    return bool(key) and (_place_or_org(key) or _place_or_org(key.rsplit("_", 1)[-1]))


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

    def __call__(
        self, value: Any, key: str, label: bool = False, links: bool = False, named: bool = False
    ) -> Any:
        """``value`` found under ``key``. ``label``: it names a partner or a place; ``links``: it is inside
        a mapping under a url key ("urls": {"assurance": "/assurance/"}); ``named``: an object that is a
        partner (``cleaned``'s ``partner``)."""
        if isinstance(value, str):
            if (links or _url_key(key)) and SITE_PATH.fullmatch(value):
                return value
            if dmq.PLAIN_NUMBER.fullmatch(value) and dmq._holds_amount(key):
                return value
            return self.text(value, label)
        if isinstance(value, Mapping):
            named = named or _names_a_place_or_org(value, key)
            keyed = _keyed_by_place_or_org(key)
            inside = links or _url_key(key)
            out = {}
            for k, v in value.items():
                inner = str(k)
                kept = _place_or_org(inner) or (named and inner in NAME_KEYS)
                out[self.text(k, keyed) if isinstance(k, str) else k] = self(v, inner, kept, inside)
            return out
        if isinstance(value, list | tuple):
            return [self(v, key, label, links) for v in value]
        return value
