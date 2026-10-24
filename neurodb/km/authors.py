"""The KM Database's authors ("Publishing Entity / Authors"): shown on screen as the workbook gives them;
in downloads (and printouts) only the organisations are named, and individual authors are counted, never
named (the user's rule). Never sent to the AI.

The rule (:func:`classify`), piece by piece:

1. Invisible characters and "et al." are taken out.
2. The entry is cut into parts on ";", a new line or "|". A placeholder ("Not reported", "Unknown"...) gives
   nothing.
3. A part an administrator listed under *Organisations named in downloads* (:class:`KMOrganisation`) is
   what they said: an organisation (named), a person (counted) or words (left out).
4. Otherwise the part is cut into pieces on commas and parentheses ("J. Doe (Fakeorg Institute)"), except
   an acronym made of the words before it ("Fake Org (FO)"); initials join the surname before them ("Doe,
   J."). A part that is not an organisation as a whole is also cut on "and" and "&".
5. Each piece is classed by itself: listed by an administrator; a word of an organisation (:data:`ORG_WORDS`)
   → organisation; initials ("J. Doe", "Doe J") → person; a name in capitals ("DOE Jane") → person;
   acronyms ("FAKEORG", "FaSyR") → organisation; a place → joins the organisation before it; capitalised
   words that could be a name → person; anything else (a topic in lower case) → counted, never named.
6. A downloads cell then reads: the organisations ("Fakeorg Institute; Fakeorg University"), "Authors: N",
   or both ("Fakeorg Institute + 2 authors"). An organisation whose text still holds a person's name or a
   citation is counted as an author instead.

When the rule is unsure, it counts the entry as a person: it never names someone by mistake. The version's
page in the admin lists every string named as an organisation, with **Count as a person**.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import connection
from django.db.models import Count, Max

from neurodb.watch import people as watch_people

from . import privacy
from .privacy import People

ORG_WORDS = frozenset(
    "organisation organisations organization organizations ministry ministere ministère university "
    "universite université universities bank council agency office programme program fund institute "
    "institut institution centre center foundation fondation association committee commission network "
    "group partnership initiative coalition society department directorate government republic federation "
    "alliance consortium cluster sector platform observatory authority union trust forum project school "
    "college academy laboratory secretariat mission services international relief rescue watch aid nations "
    "children ngo team board bureau inc ltd llc consulting consultants consultancy research studies "
    "collective movement municipality administration parliament hospital health education development "
    "policy statistics humanitarian refugee refugees women youth global national regional world worldwide "
    "corps cross crescent lebanon lebanese "
    # their plurals, and publications
    "institutes agencies ministries councils foundations associations networks groups offices programmes "
    "programs funds banks centres centers committees commissions initiatives societies departments "
    "authorities unions trusts forums projects schools colleges academies laboratories missions boards "
    "partners partnerships charity charities journal journals".split()
)
JOINING = frozenset({"of", "the", "for", "and", "de", "du", "des", "la", "le", "et", "&"})
PARTICLES = frozenset(
    {"de", "van", "von", "el", "al", "bin", "ibn", "abu", "abou", "bou", "al-", "di", "da", "du", "le", "la"}
    | {"ben"}
)
EMPTY = frozenset(
    {"not reported", "not stated", "not available", "unknown", "anonymous", "n/a", "na", "none", "-", "–"}
    | {"—", ""}
)
_ET_AL = re.compile(r"\bet\.?\s+al\b\.?", re.IGNORECASE)
_PARTS = re.compile(r"[;\n|]")
_PAREN = re.compile(r"\(([^()]*)\)")
_AND = re.compile(r"\s+(?:and|&)\s+|\s*&\s*", re.IGNORECASE)
INITIALS = re.compile(r"^(?:[A-Z]\.?\s?-?){1,4}$")
INITIAL = re.compile(r"\b[A-Z]\.(?=\s|$|,)")
# "Doe J", "Doe, J.": a surname and one initial, apart (so an acronym such as "UNICEF" is not one)
SURNAME_INITIAL = re.compile(r"^[A-Z][\w'’-]+(?:,\s*|\s+)[A-Z]\.?$")
ACRONYM = re.compile(r"^[A-Z][A-Z0-9&-]{1,11}$")
LONG_ACRONYM = re.compile(r"^[A-Z][A-Z0-9&-]{3,11}$")
MIXED_CAPS = re.compile(r"^[A-Za-z][^A-Z]*[A-Z][^A-Z]*[A-Z]")  # "FaSyR"
_TOKEN = re.compile(r"[^\s()\[\],;:]+")
_TRIM = " \t,.;:"
ORGANISATION, PERSON, WORDS = "organisation", "person", "words"


def fold(text: str) -> str:
    """An author entry as it is compared with the administrators' list: folded, spaces collapsed, no
    comma or full stop at either end."""
    return " ".join(watch_people.fold(text).split()).strip(_TRIM)


def _tokens(piece: str) -> list[str]:
    return [t.strip(".'’\"") or t for t in _TOKEN.findall(piece)]


def _folded_words(piece: str) -> list[str]:
    return [watch_people.fold(w) for w in re.findall(r"[^\W\d_]+", piece)]


def org_test(piece: str) -> bool:
    """Whether ``piece`` reads as an organisation: a word of :data:`ORG_WORDS`, or acronyms (all its words
    upper-case acronyms or joining words; an acronym of four letters or more; a word with two capitals after
    its first letter, as "FaSyR")."""
    if any(w in ORG_WORDS for w in _folded_words(piece)):
        return True
    return _acronyms(piece)


def _acronyms(piece: str) -> bool:
    tokens = _tokens(piece)
    if not tokens:
        return False
    if all(ACRONYM.match(t) or t.casefold() in JOINING for t in tokens) and any(
        ACRONYM.match(t) for t in tokens
    ):
        return True
    # a word with two capitals after its first letter ("FaSyR"), each part of a hyphenated name by itself
    mixed = any(MIXED_CAPS.match(p) for t in tokens for p in t.split("-"))
    return mixed or any(LONG_ACRONYM.match(t) and "-" not in t.strip("-") for t in tokens)


def _is_place(piece: str) -> bool:
    found = _folded_words(piece)
    return bool(found) and all(w in privacy.PLACE_WORDS for w in found)


def _capitals_name(piece: str) -> bool:
    """ "DOE Jane", "Jane DOE": two words, one in capitals and one capitalised, neither a word of an
    organisation or a place."""
    tokens = _tokens(piece)
    if len(tokens) != 2:
        return False
    letters = [re.sub(r"['’-]", "", t) for t in tokens]  # "FAKE-DOE", "O'DOE"
    caps = [
        t
        for t, bare in zip(tokens, letters, strict=True)
        if t.isupper() and len(bare) >= 2 and bare.isalpha()
    ]
    titled = [
        t
        for t, bare in zip(tokens, letters, strict=True)
        if t[:1].isupper() and t[1:].islower() and bare.isalpha()
    ]
    if len(caps) != 1 or len(titled) != 1:
        return False
    words = {watch_people.fold(t) for t in tokens}
    return words.isdisjoint(ORG_WORDS) and words.isdisjoint(privacy.PLACE_WORDS)


def _person_shape(piece: str) -> bool:
    """Capitalised words that could be a name: every word starts with a capital letter, or is initials, or
    a particle ("de", "el", "al-"...), and none is a common word."""
    tokens = _tokens(piece)
    if not tokens:
        return False
    for token in tokens:
        low = token.casefold()
        if low in watch_people.COMMON_WORDS:
            return False
        if low in PARTICLES or INITIALS.match(token):
            continue
        if re.match(r"^(?:al|el)-", low) and token[3:4].isupper():
            continue
        if not token[:1].isupper():
            return False
    return True


def _acronym_of(content: str, before: str) -> bool:
    """Whether a parenthesis's ``content`` is the acronym of the words ``before`` it: of their capitalised
    words ("Fake Org (FO)"), or of every word ("Ministry of Fake Affairs (MOFA)")."""
    letters = content.strip()
    if not (2 <= len(letters) <= 12 and letters.isalpha() and letters.isupper()):
        return False
    found = re.findall(r"[^\W\d_][\w'’-]*", before)
    capitals = "".join(w[0] for w in found if w[:1].isupper())
    every = "".join(w[0].upper() for w in found)
    return any(len(initials) >= len(letters) and initials.endswith(letters) for initials in (capitals, every))


def _split_parentheses(part: str) -> str:
    """The part with each parenthesis cut out as pieces of its own (", content ,"), except an acronym of
    the words before it."""
    out, last = [], 0
    for match in _PAREN.finditer(part):
        before = part[last : match.start()]
        out.append(before)
        segment = re.split(r",", before)[-1]
        if _acronym_of(match.group(1), segment):
            out.append(match.group(0))
        else:
            out.append(f", {match.group(1)} ,")
        last = match.end()
    out.append(part[last:])
    return "".join(out)


def pieces(part: str) -> list[str]:
    """The pieces of one part (step 4 of the rule)."""
    found: list[str] = []
    for raw in _split_parentheses(part).split(","):
        piece = " ".join(raw.split()).strip(_TRIM + "-–—")
        if piece.count("(") != piece.count(")"):  # a parenthesis left open: its signs are dropped
            piece = " ".join(piece.replace("(", " ").replace(")", " ").split())
        if not piece:
            continue
        if found and INITIALS.match(piece) and not org_test(found[-1]):
            found[-1] = f"{found[-1]} {piece}"  # "Doe, J." is one piece
            continue
        found.append(piece)
    whole = org_test(part)
    split: list[str] = []
    for piece in found:
        cut = [p.strip(_TRIM) for p in _AND.split(piece) if p.strip(_TRIM)]
        # an organisation's name stays whole ("Fakeorg Institute for Research and Development"), unless a
        # person stands in it ("Jane D. Doe and the Fakeorg Group")
        if not whole or not org_test(piece) or any(_person_like(p) for p in cut):
            for one in cut:
                split += _person_lead(one)
        else:
            split += _person_lead(piece)
    return [p for p in split if p]


def _person_like(piece: str) -> bool:
    """Whether a piece reads as a person by itself (rules c, d and g), not as an organisation."""
    if org_test(piece) or _is_place(piece):
        return False
    return bool(INITIAL.search(piece) or SURNAME_INITIAL.match(piece) or _capitals_name(piece)) or (
        _person_shape(piece) and len(_tokens(piece)) >= 2
    )


_LEAD = re.compile(r"^(?P<lead>.+?)\s+(?:of|for|at|from)\s+(?P<rest>.+)$")


def _person_lead(piece: str) -> list[str]:
    """ "Jane Doe of Fakeorg Systems International" is a person and an organisation; "University of
    Fakeplace" is one organisation."""
    piece = re.sub(r"^(?:and|&)\s+", "", piece, flags=re.IGNORECASE).strip(_TRIM)
    found = _LEAD.match(piece)
    if found and org_test(found.group("rest")) and _person_like(found.group("lead")):
        return [found.group("lead"), found.group("rest")]
    return [piece]


@dataclass
class Authors:
    """One entry, classed: the organisations it names (as written), its person pieces (as written, for the
    name forms), how many pieces are neither (counted, never named) and the pieces an administrator listed
    as words (left out)."""

    orgs: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    unclassed: int = 0
    words: list[str] = field(default_factory=list)

    @property
    def counted(self) -> int:
        return len(self.people) + self.unclassed


def _listed(part_folded: str, known: dict[str, str]) -> str | None:
    """The kind an administrator gave this part (or the start of it, before " (" or ",")."""
    if part_folded in known:
        return known[part_folded]
    for name, kind in known.items():
        if part_folded.startswith((f"{name} (", f"{name},")):
            return kind
    return None


def classify(text: str, known: dict[str, str] | None = None) -> Authors:
    """``text`` (an author entry) classed piece by piece, with ``known`` the administrators' list (folded
    name → kind)."""
    known = known or {}
    found = Authors()
    text = _ET_AL.sub(" ", watch_people.visible(text or ""))
    for raw in _PARTS.split(text):
        part = " ".join(raw.split()).strip(_TRIM)
        folded = fold(part)
        if folded in EMPTY:
            continue
        kind = _listed(folded, known)
        if kind == ORGANISATION:
            found.orgs.append(part)
            continue
        if kind == PERSON:
            found.people.append(part)
            continue
        if kind == WORDS:
            found.words.append(part)
            continue
        last_org = False
        for piece in pieces(part):
            piece_kind = known.get(fold(piece))
            if fold(piece) in EMPTY:
                continue
            if piece_kind == WORDS:
                found.words.append(piece)
                last_org = False
                continue
            if piece_kind == PERSON:
                found.people.append(piece)
                last_org = False
                continue
            if piece_kind == ORGANISATION or any(w in ORG_WORDS for w in _folded_words(piece)):
                found.orgs.append(piece)
                last_org = True
                continue
            if INITIAL.search(piece) or SURNAME_INITIAL.match(piece) or _capitals_name(piece):
                found.people.append(piece)
                last_org = False
                continue
            if _acronyms(piece):
                found.orgs.append(piece)
                last_org = True
                continue
            if _is_place(piece):
                if last_org:
                    found.orgs[-1] = f"{found.orgs[-1]}, {piece}"
                else:
                    found.orgs.append(piece)
                    last_org = True
                continue
            if _person_shape(piece):
                found.people.append(piece)
            else:
                found.unclassed += 1
            last_org = False
    return found


def cell(orgs: list[str], counted: int) -> str:
    """A downloads cell: the organisations, "Authors: N", or both ("Fakeorg Institute + 2 authors")."""
    named = "; ".join(dict.fromkeys(orgs))
    if named and counted:
        return f"{named} + {counted} author" + ("" if counted == 1 else "s")
    if named:
        return named
    return f"Authors: {counted}" if counted else ""


def safe_orgs(authors: Authors, people: People) -> tuple[list[str], int]:
    """The organisations a download may name, and the count of authors: an organisation whose text still
    holds a person's name or a citation (the final check) is counted as an author instead."""
    orgs, counted = [], authors.counted
    seen: set[str] = set()
    for org in authors.orgs:
        key = fold(org)
        if key in seen:
            continue
        seen.add(key)
        if privacy.for_download(org, people) != org or privacy.leftover(org, people):
            counted += 1
        else:
            orgs.append(org)
    return orgs, counted


def for_downloads(text: str, known: dict[str, str] | None = None, people: People | None = None) -> str:
    """``text`` (an author entry) as a download writes it (see the module's docstring)."""
    authors = classify(text, known)
    orgs, counted = safe_orgs(authors, people or privacy.NOBODY)
    return cell(orgs, counted)


# ------------------------------------------------------------------------------- the people of a version
def _initials(token: str) -> bool:
    """ "J", "J.", "JM", "J.-M.": initials, not a surname in capitals ("DOE")."""
    return bool(INITIALS.match(token)) and (len(re.sub(r"[.\s-]", "", token)) <= 2 or "." in token)


def surname(piece: str) -> str:
    """A person piece's surname: the word before a comma ("Doe, J."), the word in capitals ("DOE Jane"),
    or the last capitalised word ("Jane Doe", "J. Doe"); "" when it is shorter than 3 letters or a word of
    an organisation, a place, a month or a common word."""
    tokens = [t for t in _tokens(piece) if not _initials(t) and t.casefold() not in PARTICLES]
    if not tokens:
        return ""
    if "," in piece:
        word = _tokens(piece.split(",", 1)[0])[-1:] or [""]
        chosen = word[0]
    elif SURNAME_INITIAL.match(piece):
        chosen = tokens[0]
    else:
        caps = [t for t in tokens if t.isupper() and len(t) >= 2]
        titled = [t for t in tokens if t[:1].isupper()]
        chosen = caps[0] if caps and len(caps) < len(tokens) else (titled[-1] if titled else "")
    folded = watch_people.fold(chosen).strip(".'’")
    excluded = (
        ORG_WORDS
        | privacy.PLACE_WORDS
        | privacy.MONTHS
        | privacy.GENERIC_WORDS
        | watch_people.COMMON_WORDS
        | privacy.STOP
    )
    if len(re.sub(r"[^\w]", "", folded)) < 3 or folded in excluded:
        return ""
    return folded


def _gather(rows, known: dict[str, str]) -> People:
    """The People of ``rows`` ((document pk, author entry) pairs)."""
    names: set[str] = set()
    surnames: set[str] = set()
    own: dict[int, set[str]] = {}
    cache: dict[str, Authors] = {}
    for document, entity in rows:
        if not entity:
            continue
        authors = cache.get(entity)
        if authors is None:
            authors = cache[entity] = classify(entity, known)
        for piece in authors.people:
            names |= watch_people.name_forms(piece)
            found = surname(piece)
            if found:
                surnames.add(found)
                own.setdefault(document, set()).add(found)
    for name, kind in known.items():
        if kind == PERSON:
            names |= watch_people.name_forms(name)
            found = surname(name)
            if found:
                surnames.add(found)
    return People(
        names=frozenset(names),
        surnames=frozenset(surnames),
        own={pk: frozenset(found) for pk, found in own.items()},
    )


def known_list() -> dict[str, str]:
    """The administrators' list: folded name → kind (one query)."""
    from .models import KMOrganisation

    return dict(KMOrganisation.objects.values_list("folded", "kind"))


def entity_rows() -> list[tuple[int, str]]:
    """Every author entry of the KM Database, with its document: both sheets', each pair once (one
    query)."""
    from .models import KMDocument, KMFinding

    documents = KMDocument.objects.order_by().exclude(entity="").values_list("pk", "entity")
    findings = KMFinding.objects.order_by().exclude(entity="").values_list("document_id", "entity")
    return list(documents.union(findings))


_MEMO: dict[tuple, tuple[dict[str, str], People]] = {}


def people(version) -> People:
    """The KM Database's own people for the version shown: never None, never NeuroDB's list of the people
    it knows. Kept in this process until the version or the administrators' list changes (one aggregate
    query a use, so a correction changes downloads at once)."""
    return context(version)[1]


def context(version) -> tuple[dict[str, str], People]:
    """The administrators' list and the People of ``version`` (see :func:`people`)."""
    from .models import KMOrganisation

    state = KMOrganisation.objects.aggregate(n=Count("pk"), last=Max("changed_at"))
    key = (getattr(version, "pk", None), state["n"], state["last"])
    found = _MEMO.get(key)
    if found is None:
        known = known_list()
        found = (known, _gather(entity_rows(), known) if version is not None else privacy.NOBODY)
        if len(_MEMO) >= 4:
            _MEMO.clear()
        _MEMO[key] = found
    return found


def forget() -> None:
    _MEMO.clear()


class Namer:
    """``for_downloads`` of one download, kept per distinct entry (an entry repeats on every finding of
    its document)."""

    def __init__(self, known: dict[str, str], people: People):
        self.known, self.people = known, people
        self._seen: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        found = self._seen.get(text)
        if found is None:
            found = self._seen[text] = for_downloads(text, self.known, self.people) if text else ""
        return found


# --------------------------------------------------------------------------- what the admin reviews
def summary(known: dict[str, str], people_: People) -> dict[str, int]:
    """How the downloads show the documents' authors: organisations only, authors only (counted), both,
    none (one query)."""
    from .models import KMDocument

    counts = Counter({"organisations": 0, "authors_only": 0, "mixed": 0, "empty": 0})
    namer = Namer(known, people_)
    for entity in KMDocument.objects.order_by().values_list("entity", flat=True):
        shown = namer(entity)
        if not shown:
            counts["empty"] += 1
        elif shown.startswith("Authors: "):
            counts["authors_only"] += 1
        elif " + " in shown and shown.rsplit(" + ", 1)[1].split(" ")[0].isdigit():
            counts["mixed"] += 1
        else:
            counts["organisations"] += 1
    return dict(counts)


def named_organisations(known: dict[str, str], people_: People) -> list[str]:
    """Every distinct string the downloads name as an organisation, for the uploader to review."""
    found: dict[str, str] = {}
    cache: dict[str, list[str]] = {}
    for _document, entity in entity_rows():
        orgs = cache.get(entity)
        if orgs is None:
            orgs = cache[entity] = safe_orgs(classify(entity, known), people_)[0]
        for org in orgs:
            found.setdefault(fold(org), org)
    return sorted(found.values(), key=str.casefold)


WORDS_SEEN = 20  # a name form found in more findings than this looks like words, not a name


def look_like_words(known: dict[str, str], people_: People | None = None) -> list[str]:
    """The author pieces whose name looks like ordinary words: found as a phrase in the texts of more than
    :data:`WORDS_SEEN` findings (one statement). They stay withheld until an administrator marks them "Not
    a name"."""
    pieces_of: dict[str, set[str]] = {}
    cache: dict[str, Authors] = {}
    for _document, entity in entity_rows():
        authors = cache.get(entity)
        if authors is None:
            authors = cache[entity] = classify(entity, known)
        for piece in authors.people:
            if known.get(fold(piece)):
                continue
            for form in watch_people.name_forms(piece):
                # a form with an initial or a particle ("al s") is found after any "et al."
                if all(len(w) > 1 and w not in PARTICLES for w in form.split(" ")):
                    pieces_of.setdefault(form, set()).add(piece)
    forms = sorted(pieces_of)
    if not forms or connection.vendor != "postgresql":
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT f, (SELECT count(*) FROM km_finding "
            "WHERE search_vector @@ phraseto_tsquery('simple', f)) FROM unnest(%s::text[]) AS f",
            [forms],
        )
        frequent = [form for form, n in cursor.fetchall() if n > WORDS_SEEN]
    found: dict[str, str] = {}
    for form in frequent:
        for piece in pieces_of[form]:
            found.setdefault(fold(piece), piece)
    return sorted(found.values(), key=str.casefold)
